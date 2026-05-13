# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: true N+1 query detection by callsite.

The single highest-leverage insight in the report. Groups queries by
(normalized_query, callsite_file, callsite_line) — i.e. queries that have
the same SQL shape AND were issued from the same line of Python code.
A group with more than `MIN_OCCURRENCES` queries is almost always a Python
loop fetching its data one row at a time.

This is what makes the recorder's stack capture worth its overhead. The
existing recorder counts "exact_copies" and "normalized_copies" but doesn't
attribute them to a callsite, so it can't tell the difference between
"the same query ran 50 times because it's in a loop" and "the same query
ran 50 times from 50 different places in the code". Our grouping by
callsite makes the distinction.
"""

import json
from collections import defaultdict

from optimus.analyzers.base import (
	FRAMEWORK_PREFIXES,  # noqa: F401  (kept for any external importers)
	SEVERITY_ORDER,
	AnalyzerResult,
	is_framework_callsite,
	short_filename,
	walk_callsite,
)

# A group is also required to spend at least this much total time before
# being flagged. Prevents tiny (<1ms each) queries from generating noisy
# findings even when they repeat many times. The per-occurrence
# threshold now lives in Optimus Settings as
# ``n_plus_one_min_occurrences`` (default 10) — see
# ``optimus.settings``.
DEFAULT_MIN_TOTAL_TIME_MS = 20

# Severity heuristics
HIGH_OCCURRENCES = 50
HIGH_TOTAL_TIME_MS = 200
MEDIUM_OCCURRENCES = 20

def _get_min_total_time() -> float:
	try:
		import frappe

		v = frappe.conf.get("optimus_n_plus_one_min_total_ms")
		if v is not None:
			return float(v)
	except Exception:
		pass
	return DEFAULT_MIN_TOTAL_TIME_MS


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	# Single settings read per analyze pass (see redundant_calls for
	# the same pattern).
	from optimus.settings import get_config
	cfg = get_config()
	tracked_apps = cfg.tracked_apps

	# v0.5.2 round 3: group by (filename, lineno) instead of
	# (normalized_query, filename, lineno). A single callsite that
	# generates 10 different query shapes in the same loop (e.g.
	# frappe/query_builder/utils.py:131 resolving DocField / DocPerm
	# / Custom Field metadata within one iteration) was emitting 10
	# separate findings — same fix, 10 rows — which spammed the
	# report. Collapsing at callsite level gives ONE finding per
	# loop with the query variants listed in the detail.
	#
	# Structure: {(filename, lineno): {"variants": {normalized_q:
	# [occurrence]}, "function_name": str}}
	callsite_groups: dict[tuple, dict] = defaultdict(
		lambda: {"variants": defaultdict(list), "function_name": ""}
	)
	min_occurrences = cfg.n_plus_one_min_occurrences
	min_total_time = _get_min_total_time()

	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query") or ""
			callsite = walk_callsite(call.get("stack"))
			if not normalized or not callsite:
				continue
			key = (callsite["filename"], callsite["lineno"])
			bucket = callsite_groups[key]
			bucket["variants"][normalized].append({
				"duration": call.get("duration", 0),
				"action_idx": action_idx,
			})
			if not bucket["function_name"]:
				bucket["function_name"] = callsite.get("function") or ""

	findings = []
	for (filename, lineno), bucket in callsite_groups.items():
		variants: dict = bucket["variants"]
		# Total count across ALL variants at this callsite.
		total_count = sum(len(occ) for occ in variants.values())
		# N+1 signal: the MOST-repeated query variant must clear the
		# threshold. If 10 different queries each ran once from this
		# callsite, it's not an N+1 — it's a fan-out call site.
		max_variant_count = max(
			(len(occ) for occ in variants.values()), default=0
		)
		if max_variant_count < min_occurrences:
			continue

		total_time = sum(
			o["duration"]
			for occ in variants.values()
			for o in occ
		)
		# Minimum total time so we don't flag 10 × 0.1 ms queries as
		# an N+1 — those are not worth reporting.
		if total_time < min_total_time:
			continue

		# For the finding's "canonical" representative, use the query
		# variant with the most occurrences (highest-impact loop).
		top_variant = max(variants.items(), key=lambda kv: len(kv[1]))
		canonical_query, canonical_occurrences = top_variant
		function_name = bucket["function_name"]
		action_idx = canonical_occurrences[0]["action_idx"]
		variant_count = len(variants)

		short_fn = short_filename(filename)
		is_framework = is_framework_callsite(filename, tracked_apps=tracked_apps)
		builder = _build_framework_finding if is_framework else _build_user_finding
		findings.append(builder(
			short_fn=short_fn,
			filename=filename,
			lineno=lineno,
			function_name=function_name,
			normalized=canonical_query,
			count=total_count,
			total_time=total_time,
			action_idx=action_idx,
			# v0.5.2 round 3: expose variant list so the detail can
			# show "10 query variants observed" with sample queries.
			all_variants=sorted(
				variants.keys(),
				key=lambda q: -len(variants[q]),
			),
			variant_count=variant_count,
		))

	# Sort: highest severity first, then highest impact within severity
	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	return AnalyzerResult(findings=findings)


def _severity(count: int, total_time_ms: float) -> str:
	if count >= HIGH_OCCURRENCES or total_time_ms > HIGH_TOTAL_TIME_MS:
		return "High"
	if count >= MEDIUM_OCCURRENCES:
		return "Medium"
	return "Low"


def _title_for_callsite(short_fn, lineno, count, variant_count) -> str:
	"""Format the N+1 title. Single variant: 'Same query ran N× at …'.
	Multi variant: 'Callsite ran N queries in M variants at …' so
	the user knows the loop generates different SQL shapes, not
	literally the same string."""
	if variant_count <= 1:
		return f"Same query ran {count}× at {short_fn}:{lineno}"
	return (
		f"Callsite ran {count} queries ({variant_count} variants) "
		f"at {short_fn}:{lineno}"
	)


def _build_user_finding(
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	count: int,
	total_time: float,
	action_idx: int,
	all_variants: list[str] | None = None,
	variant_count: int = 1,
) -> dict:
	"""Build the classic user-code N+1 finding — High/Medium/Low
	severity by count & impact, actionable fix hint."""
	all_variants = all_variants or [normalized]
	multi = variant_count > 1

	desc = (
		f"We noticed the same query was repeated {count} times in a row "
		f"from the same line of code ({filename}:{lineno}), costing about "
		f"{total_time:.0f}ms in total. This is usually a Python loop that "
		"should fetch its data in one query instead of one-at-a-time. "
		"Typical fix: a few hours of dev work."
	) if not multi else (
		f"The loop at **{filename}:{lineno}** issued **{count} queries** "
		f"in {variant_count} different shapes, costing {total_time:.0f}ms. "
		"Even though the queries differ, they all come from the same "
		"line — a loop iterating over inputs and running one query per "
		"iteration. The fix is the same as a classic N+1: batch the "
		"data into a single query."
	)

	return {
		"finding_type": "N+1 Query",
		"severity": _severity(count, total_time),
		"title": _title_for_callsite(short_fn, lineno, count, variant_count),
		"customer_description": desc,
		"technical_detail_json": json.dumps(
			{
				"callsite": {
					"filename": filename,
					"lineno": lineno,
					"function": function_name,
				},
				"normalized_query": normalized,
				"occurrences": count,
				"variant_count": variant_count,
				# Up to 5 sample variants for the detail block —
				# enough to identify the loop, capped so we don't
				# blow out the 140-char title limit / DocType blob.
				"sample_queries": all_variants[:5],
				"total_time_ms": round(total_time, 2),
				"average_time_ms": round(total_time / count, 2) if count else 0,
				# v0.5.3: projected post-fix timing. Batching N loop
				# queries into ONE collapses the wall-clock cost to
				# roughly a single query. Empirically, a batched query
				# with an IN (…) filter or a JOIN costs ~2× a single
				# tight query (the work still scans the same rows, just
				# once, and returns a bigger result set). We use that
				# 2× multiplier as the ceiling so the user sees the
				# realistic savings, not the idealized floor. A 74-
				# query 85ms loop projects to ~2.2ms after batching.
				"projected_total_ms": (
					round((total_time / count) * 2, 2) if count else 0
				),
				"projected_avg_time_ms": (
					round((total_time / count) * 2, 2) if count else 0
				),
				"projected_speedup_label": (
					f"~{max(1, count // 2)}× fewer queries" if count >= 4 else None
				),
				"fix_hint": (
					"This is a classic N+1 pattern. The Python code at "
					f"{filename}:{lineno} is running the same query in a loop. "
					"Refactor to fetch all needed data in a single query — for "
					"Frappe specifically, that's usually frappe.get_all() with a "
					"name-IN filter, or a JOIN against the source table — instead "
					"of one row at a time."
				),
			},
			default=str,
		),
		"estimated_impact_ms": round(total_time, 2),
		"affected_count": count,
		"action_ref": str(action_idx),
	}


def _build_framework_finding(
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	count: int,
	total_time: float,
	action_idx: int,
	all_variants: list[str] | None = None,
	variant_count: int = 1,
) -> dict:
	"""Build the framework-level N+1 finding — always Low severity,
	description acknowledges the user can rarely fix framework code.

	Still includes the technical detail (callsite, query, impact) so
	contributors who WANT to optimize the framework can find it.
	"""
	all_variants = all_variants or [normalized]
	title = (
		f"Framework query repeated {count}× at {short_fn}:{lineno}"
		if variant_count <= 1
		else f"Framework callsite ran {count} queries ({variant_count} variants) at {short_fn}:{lineno}"
	)

	return {
		"finding_type": "Framework N+1",
		"severity": "Low",
		"title": title,
		"customer_description": (
			f"Frappe's own code at **{filename}:{lineno}** issued "
			f"{count} queries in this session, totalling "
			f"{total_time:.0f}ms. This is typically the framework "
			"resolving metadata, permissions, or building queries for "
			"different inputs — it's rarely something you can change "
			"in your application code. Listed here for transparency, "
			"not as an action item. If the cumulative cost is high, "
			"the fix usually lives in the Frappe codebase itself."
		),
		"technical_detail_json": json.dumps(
			{
				"callsite": {
					"filename": filename,
					"lineno": lineno,
					"function": function_name,
				},
				"normalized_query": normalized,
				"occurrences": count,
				"variant_count": variant_count,
				"sample_queries": all_variants[:5],
				"total_time_ms": round(total_time, 2),
				"average_time_ms": round(total_time / count, 2) if count else 0,
				"is_framework": True,
				"fix_hint": (
					"This repetition is inside Frappe framework code at "
					f"{filename}:{lineno}. Application developers can "
					"rarely change it. If this is a hot spot in your "
					"profile, consider (1) whether your usage pattern is "
					"triggering unnecessary framework work (e.g. loading "
					"DocType meta in a loop instead of once), (2) whether "
					"a Frappe upgrade has already optimized it, or (3) "
					"raising it as an upstream issue."
				),
			},
			default=str,
		),
		"estimated_impact_ms": round(total_time, 2),
		"affected_count": count,
		"action_ref": str(action_idx),
	}
