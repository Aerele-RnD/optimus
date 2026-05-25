# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Finding enrichment helpers — phase 1 (low-coupling subset).

The README's full ``finding_enrichment`` cluster is ~11 functions
spread non-contiguously across ~1500 LOC of ``_internal.py``,
intermixed with action-rendering + source-resolution helpers. The
HIGH-coupling subset (``_finding_to_dict``, ``_attach_drilldown_chains``,
``_attach_representative_callsites``, ``_expand_self_time_snippets``,
``_retarget_phase1_callsites_to_drilldown_leaf``,
``_find_call_line_in_function_body``) needs a careful coupling-graph
mapping that's out of scope for an autonomous batch.

This phase-1 extraction moves the THREE pure-function helpers that
have minimal back-coupling to ``_internal.py``:

  * ``_root_cause_key(finding)`` — return the ``(basename, function)``
    deepest-user-code anchor used to group findings. Stdlib-only.
  * ``_group_findings_by_root_cause(findings)`` — collapse findings
    sharing a root cause into one primary + ``sub_findings`` list.
    Uses ``_root_cause_key`` + the ``_GROUPING_SEVERITY_RANK``
    constant.
  * ``_normalize_callsite(callsite)`` — normalize the dict-or-string
    callsite shapes to a single ``{filename, lineno, function}`` dict.
    Stdlib-only.

Plus the ``_GROUPING_SEVERITY_RANK`` constant they share.

Extracted in v0.12.16. The remaining 8 functions (the
``_finding_to_dict`` family + AST walker + chain attachers) will move
in a focused future PR with a proper coupling-graph design pass.
"""

from __future__ import annotations

# v0.7.x: severity rank for picking the dominant finding within a
# root-cause group. Lower number = higher severity. Mirrors
# ``SEVERITY_ORDER`` in ``analyzers.base`` but lives here so the
# renderer doesn't drag an analyzer import into hot paths.
_GROUPING_SEVERITY_RANK = {"High": 0, "Medium": 1, "Low": 2}


def _root_cause_key(finding: dict) -> tuple | None:
	"""Return a ``(filename_basename, function)`` tuple identifying the
	deepest user-code anchor for this finding, or ``None`` if there's
	nothing we can group on.

	Resolution order:

	1. If the finding has a non-empty ``drilldown_chain``, use the
	   chain's last entry — that's the deepest user-code frame the
	   drill-down walker found. Stable across all per-action analyzer
	   findings (Slow Hot Path, Hook Bottleneck, ...).

	2. Else use the callsite's own ``(filename, function)``. This is
	   the path most analyzer findings (Hot Line, Redundant Call,
	   N+1, etc.) take — their callsite already names the leaf.

	Returns ``None`` only when the finding has no usable callsite at
	all (e.g. infra/system observations) — those don't group.

	Match is by ``(os.path.basename(filename), function)`` so
	dev-vs-deploy absolute path differences don't fragment groups.
	"""
	import os as _os

	detail = finding.get("technical_detail") or {}
	if not isinstance(detail, dict):
		return None

	chain = detail.get("drilldown_chain") or []
	if isinstance(chain, list) and chain:
		leaf = chain[-1]
		if isinstance(leaf, dict):
			leaf_file = leaf.get("filename") or ""
			leaf_fn = leaf.get("function") or ""
			if leaf_file and leaf_fn:
				return (_os.path.basename(leaf_file), leaf_fn)

	callsite = detail.get("callsite") or {}
	if not isinstance(callsite, dict):
		return None
	fname = callsite.get("filename") or ""
	fn_name = callsite.get("function") or ""
	if not fname or not fn_name:
		return None
	return (_os.path.basename(fname), fn_name)


def _group_findings_by_root_cause(findings: list[dict]) -> list[dict]:
	"""Collapse findings that share a ``(file, function)`` deepest-user-
	code anchor into ONE primary card with the others attached as
	``sub_findings``.

	One root cause (e.g. a hot get_doc call inside ``_check_user_exists``)
	commonly triggers several different finding types — a Slow Hot Path
	at the wrapper, a Hot Line on the exact line, a Redundant Call for
	the doc, a Redundant Permission Check for its read perm. Today each
	renders as its own card; the dev only has ONE fix to make and the
	five cards crowd the report. After grouping, the highest-severity /
	highest-impact finding becomes the visible card; the others appear
	as collapsible sub-rows beneath it.

	Returns a NEW list — primaries kept (with ``sub_findings`` attached
	when applicable), grouped non-primaries dropped, ungrouped findings
	(no resolvable root cause) passed through as-is.

	Within each group the primary is chosen by severity then by
	``estimated_impact_ms`` (higher wins). Sub-findings are sorted the
	same way so the most informative is first in the collapsed list.
	"""
	if not findings:
		return findings

	def _rank(f: dict) -> tuple:
		sev_rank = _GROUPING_SEVERITY_RANK.get(f.get("severity") or "Low", 2)
		impact = -(f.get("estimated_impact_ms") or 0)
		return (sev_rank, impact)

	# Bucket by root-cause key. None-key findings stream through as
	# singletons (no grouping).
	groups: dict[tuple, list[dict]] = {}
	passthrough: list[dict] = []
	for f in findings:
		key = _root_cause_key(f)
		if key is None:
			passthrough.append(f)
			continue
		groups.setdefault(key, []).append(f)

	result: list[dict] = []
	# Walk the input list in order; emit the primary at the first
	# position its key appears at. Skip already-emitted keys and the
	# None-key findings (handled below).
	seen_keys: set[tuple] = set()
	for f in findings:
		key = _root_cause_key(f)
		if key is None:
			continue
		if key in seen_keys:
			continue
		seen_keys.add(key)
		bucket = groups[key]
		if len(bucket) <= 1:
			result.append(bucket[0])
			continue
		ordered = sorted(bucket, key=_rank)
		primary = ordered[0]
		subs = ordered[1:]
		# Attach a compact, render-time-only sub_findings list onto
		# the primary. The template reads this to render the collapsed
		# `<details>` rows; the sub findings are NOT re-emitted as
		# standalone cards elsewhere in the bucket.
		primary["sub_findings"] = [
			{
				"finding_type": s.get("finding_type") or "",
				"severity": s.get("severity") or "Low",
				"title": s.get("title") or "",
				"customer_description": s.get("customer_description") or "",
				"estimated_impact_ms": s.get("estimated_impact_ms") or 0,
				"affected_count": s.get("affected_count") or 0,
			}
			for s in subs
		]
		result.append(primary)

	result.extend(passthrough)
	return result


def _normalize_callsite(callsite) -> dict | None:
	"""Normalize the two callsite shapes the analyzers produce into a
	single dict: ``{"filename": str, "lineno": int|None, "function": str}``.

	Historical context: ``n_plus_one`` / ``redundant_calls`` /
	``explain_flags`` emit a dict ``{filename, lineno, function}``,
	while ``top_queries`` emits a pre-formatted string like
	``"apps/myapp/foo.py:456"`` (via ``walk_callsite_str``). Before
	this normalizer, ``_app_from_finding`` crashed on Slow Query
	findings with ``AttributeError: 'str' object has no attribute
	'get'`` because it assumed dict-only.

	Normalizing here means the template and app-bucketing see a
	consistent shape regardless of which analyzer produced the
	finding, without needing to rewrite the analyzers.

	Returns ``None`` when the input is falsy/unrecognized so callers
	can short-circuit with ``if not callsite: ...``.
	"""
	if not callsite:
		return None
	if isinstance(callsite, dict):
		return callsite
	if isinstance(callsite, str):
		# Shape: "file.py:lineno" — split from the RIGHT so Windows
		# paths ("C:\\foo\\bar.py:12") keep their drive letter.
		filename = callsite
		lineno: int | None = None
		if ":" in callsite:
			head, _, tail = callsite.rpartition(":")
			if tail.isdigit():
				filename = head
				try:
					lineno = int(tail)
				except ValueError:
					lineno = None
		return {"filename": filename, "lineno": lineno, "function": ""}
	# Unknown shape — log-worthy but don't crash. Return None so the
	# template skips the Callsite block entirely.
	return None
