# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Finding enrichment helpers — extracted from ``_internal.py``
incrementally to keep the renderer-package boundary clean.

Two phases shipped so far:

  * **Phase 1 (v0.12.16)** — three pure-function helpers with
    minimal back-coupling to ``_internal.py``:
    - ``_root_cause_key(finding)`` — ``(basename, function)``
      deepest-user-code anchor for finding grouping.
    - ``_group_findings_by_root_cause(findings)`` — collapse
      same-root-cause findings into one primary +
      ``sub_findings`` list.
    - ``_normalize_callsite(callsite)`` — dict-or-string callsite
      shape normalization.

  * **Phase 2 (v0.12.19)** — the drill-down chain attachers (a
    self-contained sub-cluster of the larger finding-enrichment
    family that depends only on stdlib + ``call_tree_renderer.
    _ct_is_other_frame`` + a lazy ``optimus.analyzers.base.
    is_framework_callsite`` import):
    - ``_find_node_in_tree(tree, basename, function)`` — DFS for
      a node by (basename, function).
    - ``_walk_drilldown_chain(tree, callsite, ...)`` — hottest-
      child traversal below a finding's origin frame.
    - ``_attach_drilldown_chains(findings, actions, ...)`` —
      in-place attachment of the chain onto each finding's
      ``technical_detail``.

Plus the ``_GROUPING_SEVERITY_RANK`` constant phase 1 shares.

Still in ``_internal.py`` (the HIGH-coupling subset that needs the
larger source-resolution helper family to move with it, or an
expanded back-import design):

  * ``_finding_to_dict`` (~200 LOC, the main render-dict builder).
  * ``_attach_representative_callsites`` (calls
    ``_action_dotted_entry``, ``_skip_decorators_to_def``,
    ``_resolve_dotted_to_code``, ``_action_entry_callsite``,
    ``_resolve_frame_key_to_callsite``, ``_bench_relative_display``).
  * ``_expand_self_time_snippets`` (calls ``_read_function_body_snippet``).
  * ``_retarget_phase1_callsites_to_drilldown_leaf`` + its AST
    helper ``_find_call_line_in_function_body``.
"""

from __future__ import annotations

import json

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


# ---------------------------------------------------------------------------
# Phase 2 (v0.12.19): drill-down chain attachers
# ---------------------------------------------------------------------------


def _find_node_in_tree(tree: dict, basename: str, function: str) -> dict | None:
	"""Depth-first walk a pyinstrument call tree looking for a node that matches
	``(basename(filename), function)``. Returns the first hit, or ``None``.

	The basename match (rather than full path) survives bench-relative vs
	absolute path differences between where the analyzer ran and where the
	render is happening - same trick ``_build_line_drilldown_callsite_index``
	uses.
	"""
	if not isinstance(tree, dict):
		return None
	want = (basename or "").strip()
	want_fn = (function or "").strip()
	if not want_fn:
		return None
	stack = [tree]
	while stack:
		node = stack.pop()
		if not isinstance(node, dict):
			continue
		node_file = (node.get("filename") or "").rsplit("/", 1)[-1]
		node_fn = (node.get("function") or "")
		if node_fn == want_fn and (not want or node_file == want):
			return node
		children = node.get("children") or []
		# DFS in-order via reverse-append so leftmost children pop first.
		stack.extend(reversed(children))
	return None


def _walk_drilldown_chain(
	tree: dict,
	callsite: dict,
	tracked_apps: tuple[str, ...] = (),
	max_depth: int = 4,
	signal_floor_pct: float = 10.0,
) -> list[dict]:
	"""Build a *Drill-down* chain below the finding's origin frame.

	Given a per-action pyinstrument tree (dict form from
	``analyzers/call_tree._walk_pyi_frame``) and a finding's callsite
	(``{"filename": ..., "function": ...}``), locate the origin node then walk
	hottest-child links downward until one of:

	- the next child's filename is in framework code, OR
	- depth reaches ``max_depth``, OR
	- no children remain, OR
	- the next child's ``cumulative_ms`` is below ``signal_floor_pct`` % of the
	  origin's ``cumulative_ms`` (drops noisy near-leaf frames).

	Returns a list of ``{filename, lineno, function, cumulative_ms,
	pct_of_origin}`` dicts — one per level *below* the origin. The origin
	itself is omitted (already rendered in the smoking-gun block).

	Defensive: any malformed input → ``[]``.
	"""
	# Lazy imports — these belong to sibling modules; importing at
	# module-top would create a circular if call_tree_renderer ever
	# grew a dependency on this submodule.
	from optimus.analyzers.base import is_framework_callsite
	from optimus.renderer.call_tree_renderer import _ct_is_other_frame

	if not isinstance(tree, dict) or not isinstance(callsite, dict):
		return []
	filename = callsite.get("filename") or ""
	function = callsite.get("function") or ""
	if not function:
		return []

	# If the finding's own callsite is already in framework code, the chain
	# below would be even further from user-actionable code — skip.
	if is_framework_callsite(filename, tracked_apps=tracked_apps or None):
		return []

	origin = _find_node_in_tree(tree, filename.rsplit("/", 1)[-1], function)
	if origin is None:
		return []

	origin_ms = float(origin.get("cumulative_ms") or 0)
	if origin_ms <= 0:
		return []

	floor_ms = origin_ms * (signal_floor_pct / 100.0)
	chain: list[dict] = []
	node = origin
	for _ in range(max(0, max_depth)):
		children = node.get("children") or []
		if not children:
			break
		# Pick the hottest child by cumulative_ms, skipping synthetic
		# "[other: N frames]" collapse nodes (v0.7.x: not shown in chains —
		# you can't drill into a collapsed bucket).
		hottest = max(
			(c for c in children
			 if isinstance(c, dict) and not _ct_is_other_frame(c.get("function"))),
			key=lambda c: float(c.get("cumulative_ms") or 0),
			default=None,
		)
		if hottest is None:
			break
		child_ms = float(hottest.get("cumulative_ms") or 0)
		if child_ms < floor_ms:
			break
		child_file = hottest.get("filename") or ""
		if is_framework_callsite(child_file, tracked_apps=tracked_apps or None):
			break
		chain.append({
			"filename": child_file,
			"lineno": int(hottest.get("lineno") or 0),
			"function": hottest.get("function") or "",
			"cumulative_ms": child_ms,
			"pct_of_origin": int(round(child_ms / origin_ms * 100)) if origin_ms else 0,
		})
		node = hottest

	return chain


def _attach_drilldown_chains(findings, actions, tracked_apps: tuple[str, ...] = ()) -> None:
	"""Walk each finding's representative call tree and attach a
	``drilldown_chain`` to its ``technical_detail`` dict. Mutates findings in
	place — same pattern as ``_attach_representative_callsites``.

	Tree JSON parses are cached per ``action_idx`` so a session with several
	findings on the same slow action only deserialises the tree once.
	"""
	if not findings or not actions:
		return

	# Index actions by their original ``idx`` so action_ref lookups survive the
	# min_action_duration_ms filter (which preserves idx but reshapes the list).
	actions_by_idx: dict[int, dict] = {}
	for a in actions:
		try:
			actions_by_idx[int(a.get("idx"))] = a
		except (TypeError, ValueError):
			continue

	tree_cache: dict[int, dict] = {}
	for finding in findings:
		detail = finding.get("technical_detail") or {}
		callsite = detail.get("callsite") or {}
		if not callsite.get("function"):
			continue
		ref = finding.get("action_ref")
		if ref in (None, ""):
			continue
		try:
			idx = int(ref)
		except (TypeError, ValueError):
			continue
		action = actions_by_idx.get(idx)
		if not action:
			continue
		if idx not in tree_cache:
			try:
				tree_cache[idx] = json.loads(action.get("call_tree_json") or "{}")
			except (TypeError, ValueError):
				tree_cache[idx] = {}
		tree = tree_cache.get(idx) or {}
		if not tree:
			continue
		chain = _walk_drilldown_chain(tree, callsite, tracked_apps=tracked_apps)
		# v0.7.x: always attach the chain — even empty. The template
		# distinguishes "key absent" (no callsite/action/tree, never
		# attempted) from "empty list" (attempted but no eligible
		# user-code descendants) to decide whether to render a "no
		# deeper user-code frame" placeholder in place of the chain.
		detail["drilldown_chain"] = chain
		finding["technical_detail"] = detail
