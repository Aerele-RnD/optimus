# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: detect redundant frappe.get_doc / cache.get_value / has_permission calls.

Reads the per-recording sidecar argument log captured by the wraps in
capture.py, buckets entries by (fn_name, identifier_safe), and emits
one Redundant Call finding per bucket whose count exceeds a configurable
threshold.

Bucket key uses identifier_safe (sha256 hash) for redundancy
detection so equivalent values cluster regardless of literal text.
The finding's technical_detail_json carries BOTH identifier_safe AND
identifier_raw — the renderer uses identifier_raw.
"""

import json
from collections import Counter, defaultdict

from optimus.analyzers.base import (
	AnalyzerResult,
	is_framework_callsite,
	walk_callsite,
)

DEFAULT_REDUNDANT_HIGH_MULTIPLIER = 5


def _conf_int(key: str, default: int) -> int:
	"""site_config.json fallback for the non-threshold knob (high
	multiplier) that isn't surfaced on the Settings DocType yet."""
	try:
		import frappe

		v = frappe.conf.get(key)
		if v is not None:
			return int(v)
	except Exception:
		pass
	return default


def _threshold_for(fn_name: str, cfg) -> int:
	"""Return the count threshold for a given sidecar fn_name.

	Resolved from Optimus Settings (cached) with site_config.json
	and hardcoded defaults as fallbacks — see settings.get_config()
	for the precedence chain.
	"""
	if fn_name == "get_doc":
		return cfg.redundant_doc_threshold
	if fn_name == "cache_get":
		return cfg.redundant_cache_threshold
	if fn_name == "has_permission":
		return cfg.redundant_perm_threshold
	return 999_999


def _title_for(fn_name: str, identifier_safe, count: int) -> str:
	if fn_name == "get_doc":
		doctype, name_hash = identifier_safe
		return f"Redundant doc fetch: {doctype} {name_hash} ({count} times)"
	if fn_name == "cache_get":
		return f"Redundant cache lookup: {identifier_safe} ({count} times)"
	if fn_name == "has_permission":
		doctype, name_hash, ptype = identifier_safe
		return f"Redundant permission check: {doctype} {name_hash} {ptype} ({count} times)"
	return f"Redundant call: {fn_name} ({count} times)"


def _customer_description_for(fn_name: str, count: int, callsite: dict | None = None) -> str:
	"""Build the customer description, appending the callsite when
	available. v0.5.2 requires the callsite (file:line) for the user
	to actually navigate to the loop — pre-v0.5.2 the description
	said 'the same callsite' without revealing where."""
	site_hint = ""
	if callsite:
		fn_site = callsite.get("filename") or ""
		ln = callsite.get("lineno")
		if fn_site and ln:
			site_hint = f" The loop is at **{fn_site}:{ln}**."

	if fn_name == "get_doc":
		return (
			f"The same document was fetched **{count} times** from the same "
			"line of code. This is almost always a loop that reloads a "
			"document inside its body — caching the result outside the loop "
			"would eliminate the redundant fetches."
			f"{site_hint}"
		)
	if fn_name == "cache_get":
		return (
			f"The same cache key was looked up **{count} times** from the "
			"same callsite. Cache lookups are cheap individually but add up "
			"in a hot loop; reading once and re-using is the fix."
			f"{site_hint}"
		)
	if fn_name == "has_permission":
		return (
			f"The same permission check ran **{count} times** from the same "
			"callsite. Permission checks involve role lookups and DocType "
			"validation — caching the result for the duration of the action "
			"is the standard fix."
			f"{site_hint}"
		)
	return f"A function was called {count} times redundantly.{site_hint}"


def _to_hashable(value):
	"""Convert nested lists to nested tuples so the value can be a dict key."""
	if isinstance(value, list):
		return tuple(_to_hashable(v) for v in value)
	if isinstance(value, tuple):
		return tuple(_to_hashable(v) for v in value)
	return value


def analyze(recordings: list, context) -> AnalyzerResult:
	# Read settings once for this analyze pass — avoids N cache
	# lookups for an N-bucket analysis.
	from optimus.settings import get_config
	cfg = get_config()
	tracked_apps = cfg.tracked_apps  # may be empty (→ exclusion mode)

	# Bucket: (fn_name, identifier_safe_tuple) → list of
	# (action_idx, raw, caller_stack)
	buckets: dict = defaultdict(list)
	truncation_seen = False
	skipped_unhashable = 0

	for action_idx, recording in enumerate(recordings):
		sidecar = recording.get("sidecar") or []
		for entry in sidecar:
			if not isinstance(entry, dict):
				continue
			if entry.get("_truncated"):
				truncation_seen = True
				continue
			fn_name = entry.get("fn_name")
			safe = entry.get("identifier_safe")
			raw = entry.get("identifier_raw")
			caller_stack = entry.get("caller_stack") or []
			if fn_name is None or safe is None:
				continue
			try:
				key = (fn_name, _to_hashable(safe))
				buckets[key].append((action_idx, raw, caller_stack))
			except TypeError:
				skipped_unhashable += 1
				continue

	if skipped_unhashable:
		context.warnings.append(
			f"redundant_calls: skipped {skipped_unhashable} sidecar entries "
			"with unhashable identifiers (likely dict-arg get_doc on unsaved docs)."
		)

	if truncation_seen:
		context.warnings.append(
			"Sidecar argument log was truncated for at least one recording — "
			"redundant call detection may be incomplete."
		)

	findings: list = []
	# v0.5.2: track how many buckets we dropped as framework-only so we
	# can surface a soft warning (same pattern as index_suggestions'
	# drop counts).
	drop_framework_callsite = 0
	# And how many had no caller stack at all (sidecars captured before
	# v0.5.2 when caller_stack wasn't recorded).
	drop_no_caller_stack = 0
	# v0.5.2 round 2: buckets whose count threshold was only reached by
	# summing ACROSS many actions (e.g. "25 calls" that turned out to
	# be 1 call in each of 25 requests — not a loop, just a call that
	# naturally fires once per request).
	drop_cross_request_spread = 0

	for (fn_name, safe_key), occurrences in buckets.items():
		threshold = _threshold_for(fn_name, cfg)
		count = len(occurrences)
		if count < threshold:
			continue

		# v0.5.2 round 2: a "redundant call" is a LOOP, meaning the
		# threshold must be reached WITHIN a single action. Cross-
		# request aggregation (25 separate requests each calling cache
		# once) isn't a loop — it's a framework call that naturally
		# fires once per request. Production report had 3 "Redundant
		# cache lookup: … (25 times)" / "(36 times)" findings from
		# werkzeug/serving.py:370 — each was 1 call per request across
		# 25/36 requests, not a repeated in-loop lookup.
		action_counts = Counter(idx for idx, _, _ in occurrences)
		max_in_any_action = action_counts.most_common(1)[0][1]
		if max_in_any_action < threshold:
			drop_cross_request_spread += 1
			continue

		# v0.5.2: callsite-based filtering. Use the first occurrence's
		# stack as the representative (all occurrences of the same
		# (fn_name, identifier) are by definition from the same cache
		# key, and we flag them BECAUSE they all fire from the same
		# repeated loop — so first-occurrence stack is canonical).
		first_stack = occurrences[0][2]
		if not first_stack:
			# Recording captured before v0.5.2 OR stack capture failed.
			# Drop the finding rather than emit a hashed-cache-key-
			# with-no-context row that the user can't act on.
			drop_no_caller_stack += 1
			continue

		callsite = walk_callsite(first_stack)
		if callsite is None or is_framework_callsite(
			callsite.get("filename") or "", tracked_apps=tracked_apps
		):
			# Pure framework stack. walk_callsite returns None for
			# profiler-own stacks; for pure frappe/* stacks it falls
			# back to the deepest frame (so legitimate migration /
			# background-task findings don't disappear). Here we
			# ADDITIONALLY filter any callsite that resolves to an
			# official Frappe-maintained app (frappe, erpnext, hrms,
			# …) or a pip-installed third-party lib — the loop inside
			# those isn't actionable for application developers.
			# Same rationale as the Framework N+1 filter.
			drop_framework_callsite += 1
			continue

		# Callsite IS user code (or at least contains a user frame).
		# Emit the finding with the callsite in the detail so users
		# can navigate to the loop.

		high_multiplier = _conf_int(
			"optimus_redundant_high_multiplier", DEFAULT_REDUNDANT_HIGH_MULTIPLIER
		)
		# v0.5.2 round 2: severity based on max-in-any-action, not
		# total count. Because count was established above to reflect
		# loop density within a single action, using it for severity
		# misleads ("100 cross-request cache calls" looks worse than
		# "100 cache calls in one loop in one request"). Use
		# max_in_any_action instead.
		severity = (
			"High"
			if max_in_any_action >= threshold * high_multiplier
			else "Medium"
		)

		# Action ref = the action containing the most occurrences
		# (already computed in action_counts above as part of the
		# per-action threshold check).
		top_action_idx, _ = action_counts.most_common(1)[0]

		identifier_safe = safe_key
		identifier_raw = occurrences[0][1]

		findings.append({
			"finding_type": "Redundant Call",
			"severity": severity,
			"title": _title_for(fn_name, identifier_safe, count),
			"customer_description": _customer_description_for(
				fn_name, count, callsite=callsite
			),
			"technical_detail_json": json.dumps({
				"fn_name": fn_name,
				"identifier_safe": (
					list(identifier_safe) if isinstance(identifier_safe, tuple) else identifier_safe
				),
				"identifier_raw": (
					list(identifier_raw) if isinstance(identifier_raw, tuple) else identifier_raw
				),
				"count": count,
				"distinct_actions": len(action_counts),
				# v0.5.2: surface the callsite so developers can
				# actually navigate to the loop. Pre-v0.5.2 the only
				# identifier was a sha256 hash of the cache key —
				# useless for finding the offending code.
				"callsite": {
					"filename": callsite.get("filename"),
					"lineno": callsite.get("lineno"),
					"function": callsite.get("function"),
				},
			}, default=str),
			"estimated_impact_ms": 0,
			"affected_count": count,
			"action_ref": str(top_action_idx),
		})

	if drop_cross_request_spread:
		context.warnings.append(
			f"Suppressed {drop_cross_request_spread} Redundant Call "
			"candidate(s) where the threshold was reached only by "
			"summing across multiple requests (e.g. one cache lookup "
			"per request × 25 requests). That's not a loop — it's a "
			"call that naturally fires once per request. A real "
			"redundant loop has the threshold met WITHIN a single "
			"action."
		)
	if drop_framework_callsite:
		context.warnings.append(
			f"Suppressed {drop_framework_callsite} Redundant Call "
			"finding(s) whose loop lives inside Frappe framework code "
			"or a third-party library (users can't act on those). "
			"The hot ones still show up in the Repeated Hot Frame "
			"leaderboard if they represent significant time."
		)

	if drop_no_caller_stack:
		context.warnings.append(
			f"Skipped {drop_no_caller_stack} Redundant Call candidate(s) "
			"with no captured caller stack. Re-run the session on the "
			"v0.5.2+ profiler to enable callsite-based filtering."
		)

	return AnalyzerResult(findings=findings)
