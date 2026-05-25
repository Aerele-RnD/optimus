# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Line-Level Drilldown panel — the Phase-2 per-line profiling section.

Sourced from the session's ``phase_2_runs`` child table (one row per
line-profile pass); each row carries a ``results_json`` blob shaped as
``[{file, dotted_path, lines: [{lineno, hits, total_ms, ...}]}]`` and a
``picks_json`` blob with the user's pick list + auto-expand flags.

Two public surfaces (called from the render orchestrator in
``_internal.py``):

* ``_build_line_drilldown_callsite_index(session_doc)`` — semi-public:
  ``optimus.analyze`` also calls it (via the renderer-package shim) to
  power the finding-card "Line-Level Drilldown hot line: ..." callout.
  Returns a ``(basename, function_name) → hottest-line`` dict.
* ``_render_line_drilldown_panel(session_doc)`` — the section HTML.
  Empty string when the session has no phase-2 runs.

Plus four internal helpers — ``_make_line_drilldown_lookup`` (Jinja
adapter for tuple-keyed lookups), ``_phase2_invoked`` (per-function
"did it run?" check), ``_render_phase2_function_table``,
``_render_phase2_diff_table`` (per-function HTML pieces) — and two
back-compat aliases (``_build_phase2_callsite_index``,
``_make_phase2_lookup``, ``_render_phase2_panel``) that pre-v0.7.x
renames left behind.

Extracted from ``_internal.py`` in v0.12.12 per the v0.10.0 renderer-
package roadmap. The 840-LOC line_drilldown cluster was the README's
"single biggest remaining chunk." NB: ``_find_call_line_in_function_body``
(an AST-walking helper used by ``_retarget_phase1_callsites_to_drilldown_leaf``
which is part of the still-pending finding_enrichment cluster) stays
in ``_internal.py`` for now — it'll move with that cluster, not this
one. Same for ``_root_cause_key`` / ``_group_findings_by_root_cause``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from optimus.renderer.syntax import _highlight_python_snippet
from optimus.renderer.time_format import _format_duration_ms


def _e(text: object) -> str:
	"""HTML-escape. Local copy of ``_internal._e`` (same pattern as
	``call_tree_renderer.py`` / ``doc_event_renderer.py``) — keeps this
	submodule free of a back-reference into ``_internal.py`` that would
	create a circular import once ``_internal`` re-imports from here."""
	import html as _html

	return _html.escape("" if text is None else str(text))


# ---------------------------------------------------------------------------
# Callsite index — semi-public (analyze.py calls it via the package shim)
# ---------------------------------------------------------------------------


def _build_line_drilldown_callsite_index(session_doc: Any) -> dict:
	"""Build a (basename, function_name) → hottest-line lookup from the
	session's phase-2 runs. Used by ``finding_card`` to inject a
	"Line-Level Drilldown hot line: ..." callout whenever a finding's
	callsite resolves to a function that was line-profiled.

	Keyed by file basename (not absolute path) so the lookup survives
	dev-vs-deploy path differences. When the same function appears in
	multiple runs, the entry with the largest single-line ``total_ms``
	wins — that's the most informative callout for the developer.

	Per-function, own hottest line — no cross-function redirection.
	The cross-link's job is "this function's hottest internal line";
	the smoking-gun snippet's job (handled by
	``_retarget_phase1_callsites_to_drilldown_leaf``) is "land the
	reader on the deepest user-code frame". Keeping them separate
	avoids the cross-link silently re-aiming the user at a different
	function's data, which was confusing.

	Returns an empty dict when the session has no phase-2 runs or the
	results blobs are empty / malformed; the macro then renders no
	callout.
	"""
	runs = list(getattr(session_doc, "phase_2_runs", None) or [])
	index: dict[tuple, dict] = {}
	for child in runs:
		try:
			results = json.loads(getattr(child, "results_json", None) or "[]")
		except Exception:
			continue
		run_uuid = getattr(child, "run_uuid", "") or ""

		for fn in results:
			file_path = fn.get("file") or ""
			dotted = fn.get("dotted_path") or ""
			qualname = fn.get("qualname") or (dotted.rsplit(".", 1)[-1] if dotted else "")
			lines = fn.get("lines") or []
			if not file_path or not qualname or not lines:
				continue
			hot_line = max(
				lines,
				key=lambda ln: ln.get("total_ms", 0) or 0,
				default=None,
			)
			if not hot_line or not hot_line.get("total_ms"):
				continue
			candidate_ms = hot_line.get("total_ms", 0) or 0
			entry = {
				"lineno": hot_line.get("lineno"),
				"content": hot_line.get("content") or "",
				"total_ms": candidate_ms,
				"hits": hot_line.get("hits") or 0,
				"run_uuid": run_uuid,
				"dotted_path": dotted,
			}
			# v0.7.x: key under BOTH the full qualname and its bare last
			# segment. resolve_freeform may emit a prefixed qualname
			# (``common.bg_recheck_users`` / ``SalesInvoice.validate``) while a
			# call_tree finding's callsite carries the bare function name — the
			# callout silently missed when the two disagreed on the prefix even
			# though the function was profiled (and showing in the panel).
			basename = os.path.basename(file_path)
			bare = qualname.rsplit(".", 1)[-1]
			for k in {(basename, qualname), (basename, bare)}:
				existing = index.get(k)
				if existing is None or candidate_ms > (existing.get("total_ms") or 0):
					index[k] = entry
	return index


# Back-compat alias — pre-v0.7.x name.
_build_phase2_callsite_index = _build_line_drilldown_callsite_index


def _make_line_drilldown_lookup(index: dict):
	"""Wrap the line-drilldown callsite index in a small lookup callable
	so Jinja can call ``line_drilldown_for_callsite(filename,
	function_name)`` - Jinja cannot index dicts by tuple keys directly.
	"""

	def lookup(filename, function_name):
		if not filename or not function_name:
			return None
		base = os.path.basename(filename)
		# Try the function name as-is, then its bare last segment — mirrors the
		# dual keying in _build_line_drilldown_callsite_index so a prefix
		# mismatch (qualname vs callsite function) can't break the callout.
		return index.get((base, function_name)) or index.get((base, function_name.rsplit(".", 1)[-1]))

	return lookup


# Back-compat alias — pre-v0.7.x name.
_make_phase2_lookup = _make_line_drilldown_lookup


# ---------------------------------------------------------------------------
# Per-function rendering helpers
# ---------------------------------------------------------------------------


def _phase2_invoked(fn: dict) -> bool:
	"""Whether a picked phase-2 function actually ran (≥1 line with hits or
	time). Reuses the analyzer's canonical check so render + analyze agree."""
	from optimus.line_profile.analyzer import _function_invoked

	return _function_invoked(fn)


def _render_phase2_function_table(fn: dict) -> str:
	"""Per-function line table inside one phase-2 run.

	Columns: line number, hit count, total ms, per-hit µs, source.

	v0.6.0 Round 7: previously took ``show_source`` + ``mode`` to gate
	the source-line column. With safe mode removed, source is always
	rendered.

	When ``fn`` carries a ``source == "auto_expand"`` marker (set by the
	renderer from the run's picks_json), the function header is indented
	and prefixed with ``↳`` so the chain reads visually as a stack: the
	user's pick appears flush-left, each auto-expanded descendant a
	level deeper.
	"""
	# v0.7.x Phase F: editorial styling. Replaces inline-styled divs +
	# table with `.phase2-func` + `.line-prof` classes. Auto-expanded
	# descendants get progressive indent (`.indent-1` for now; deeper
	# chains can roll into `.indent-2` / `.indent-3` if a future patch
	# tracks chain depth on the row).
	rows = fn.get("lines") or []
	dotted = fn.get("dotted_path", "")
	file_path = fn.get("file", "")
	source = fn.get("source") or "curated"

	# v0.7.x: a picked function that never ran (no lines, or all hits/total
	# zero) renders nothing — the caller folds it into one "Not exercised in
	# this pass" note instead of a noisy empty per-line table.
	if not _phase2_invoked(fn):
		return ""

	is_descendant = source == "auto_expand"
	indent_cls = " indent-1" if is_descendant else ""
	header_prefix = '<span class="arrow">&#x21B3;</span>' if is_descendant else ""

	html = [
		f'<div class="phase2-func{indent_cls}">',
		f'<div class="fn-name">{header_prefix}{_e(dotted)}</div>',
		f'<div class="fn-path">{_e(file_path)}</div>',
	]

	html.append(
		'<table class="line-prof">'
		"<thead><tr>"
		'<th class="num">#</th>'
		'<th class="num">hits</th>'
		'<th class="num">total</th>'
		'<th class="num">per hit</th>'
		"<th>source</th>"
		"</tr></thead><tbody>"
	)

	# Hot-row threshold: a line is hot when its total_ms is ≥25% of the
	# function's max line ms (the existing heat-map rule).
	max_ms = max((r.get("total_ms") or 0) for r in rows)

	# v0.7.x: VSCode Dark+ syntax highlighting for the per-function
	# source column. Highlighting the whole function's rows together
	# preserves multi-line tokenisation state.
	_highlight_python_snippet(rows)

	for line in rows:
		ms = line.get("total_ms") or 0
		hits = line.get("hits", 0) or 0
		is_hot = max_ms > 0 and ms > 0 and ms / max_ms >= 0.25
		if is_hot:
			tr_cls = ' class="hot"'
		elif hits == 0 and ms == 0:
			# v0.7.x: dim pure-context lines (def/comments/blank/closing parens)
			# that never executed, so the lines that actually ran stand out.
			tr_cls = ' class="zero"'
		else:
			tr_cls = ""
		# `per_hit_us` is microseconds — convert to ms so the timing
		# rule (1s threshold for the `.time-high` highlight) applies.
		per_hit_ms = (line.get("per_hit_us") or 0) / 1000.0
		_src_html = line.get("content_html")
		_src_cell = _src_html if _src_html else _e(line.get("content", ""))
		html.append(
			f"<tr{tr_cls}>"
			f'<td class="ln">{line.get("lineno", "")}</td>'
			f'<td class="num">{line.get("hits", 0)}</td>'
			f'<td class="num">{_format_duration_ms(ms, decimals=2)}</td>'
			f'<td class="num">{_format_duration_ms(per_hit_ms, decimals=2)}</td>'
			f'<td class="src"><code>{_src_cell}</code></td>'
			"</tr>"
		)

	html.append("</tbody></table></div>")
	return "".join(html)


def _render_phase2_diff_table(diff_rows: list[dict]) -> str:
	"""Render the cross-run delta table for one function profiled in 2+
	runs — the verify-the-fix view.

	v0.6.0 Round 7: source column always shows full code (was previously
	gated by ``mode == "safe"`` + the safe-source toggle).
	"""
	# v0.7.x Phase F: cross-run diff uses the same `.line-prof` base
	# class as the per-function table, with extra `.added` / `.removed`
	# row tints for matched-faster / matched-slower / added / removed
	# statuses (CSS overlays in the `.line-prof` block).
	if not diff_rows:
		return ""

	html = [
		'<table class="line-prof line-prof-diff">'
		"<thead><tr>"
		"<th>status</th>"
		'<th class="num">prev #</th>'
		'<th class="num">curr #</th>'
		'<th class="num">prev ms</th>'
		'<th class="num">curr ms</th>'
		'<th class="num">&Delta; ms</th>'
		"<th>source</th>"
		"</tr></thead><tbody>",
	]

	# v0.7.x: VSCode Dark+ syntax highlighting for the cross-run diff
	# source column. Highlight all diff rows together so multi-line
	# constructs across the diff window stay correctly tokenised.
	_highlight_python_snippet(diff_rows)

	for row in diff_rows:
		status = row.get("status", "")
		delta = row.get("delta_ms")
		# Row-class: `added` (green tint) for matched-faster + new
		# rows; `removed` (red tint) for matched-slower + dropped
		# rows. Matches the `.line-prof` CSS overlays.
		tr_cls = ""
		if status == "matched" and delta is not None:
			if delta < -0.5:
				tr_cls = ' class="added"'
			elif delta > 0.5:
				tr_cls = ' class="removed"'
		elif status == "added":
			tr_cls = ' class="added"'
		elif status == "removed":
			tr_cls = ' class="removed"'

		def _fmt(v):
			return "—" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))

		_src_html = row.get("content_html")
		_src = _src_html if _src_html else _e(row.get("content", ""))
		source_cell = f"<code>{_src}</code>"

		html.append(
			f"<tr{tr_cls}>"
			f"<td>{_e(status)}</td>"
			f'<td class="num">{_fmt(row.get("prev_lineno"))}</td>'
			f'<td class="num">{_fmt(row.get("curr_lineno"))}</td>'
			f'<td class="num">{_fmt(row.get("prev_ms"))}</td>'
			f'<td class="num">{_fmt(row.get("curr_ms"))}</td>'
			f'<td class="num">{_fmt(delta)}</td>'
			f'<td class="src">{source_cell}</td>'
			"</tr>"
		)
	html.append("</tbody></table>")
	return "".join(html)


# ---------------------------------------------------------------------------
# Top-level panel renderer
# ---------------------------------------------------------------------------


def _render_line_drilldown_panel(session_doc: Any) -> str:
	"""Build the Line-Level Drilldown section HTML. Returns an empty
	string when the session has no phase-2 runs (the template's
	``{% if line_drilldown_html %}`` guard then skips the section
	entirely).

	v0.6.0 Round 7: source-line text is always rendered (was previously
	gated by the ``safe_report_include_source_lines`` setting in safe
	mode). With safe mode removed the toggle is gone and the report
	always shows full code.
	"""
	from optimus.line_profile import diff as _lp_diff

	runs = list(getattr(session_doc, "phase_2_runs", None) or [])
	if not runs:
		return ""

	# Parse each run's stored JSON into the shape we render against.
	# Annotate each function entry with its pick ``source`` (curated vs
	# auto_expand) by looking up dotted_path in the run's picks_json so
	# the per-function table can render auto-expanded descendants with
	# the chain-indent visual.
	parsed_runs: list[dict] = []
	for child in runs:
		try:
			results = json.loads(child.results_json or "[]")
		except Exception:
			results = []
		try:
			picks = json.loads(child.picks_json or "[]")
		except Exception:
			picks = []
		picks_source: dict[str, str] = {
			p.get("dotted_path"): p.get("source", "curated") for p in picks if p.get("dotted_path")
		}
		annotated_results = []
		for fn in results:
			annotated_results.append(
				{
					**fn,
					"source": picks_source.get(fn.get("dotted_path"), "curated"),
				}
			)
		parsed_runs.append(
			{
				"run_uuid": child.run_uuid,
				"status": child.status,
				"started_at": child.started_at,
				"ended_at": child.ended_at,
				"total_ms": child.total_ms or 0,
				"picks": picks,
				"functions": annotated_results,
			}
		)

	# Cross-run diff: when a function appears in 2+ runs, align the latest
	# two by content hash and render the delta panel.
	function_history: dict[str, list] = {}
	for idx, run in enumerate(parsed_runs):
		for fn in run["functions"]:
			function_history.setdefault(fn["dotted_path"], []).append((idx, fn))

	diffs: dict[str, dict] = {}
	for path, history in function_history.items():
		if len(history) < 2:
			continue
		prev_idx, prev_fn = history[-2]
		curr_idx, curr_fn = history[-1]
		diffs[path] = {
			"prev_run_idx": prev_idx,
			"curr_run_idx": curr_idx,
			"rows": _lp_diff.align_function(prev_fn["lines"], curr_fn["lines"]),
		}

	# v0.7.x Phase F: editorial section head with per-run cards +
	# per-function blocks. Uses the shared `.section` / `.section-head` /
	# `.phase2-run` / `.phase2-func` / `.line-prof` classes.
	n_runs = len(parsed_runs)
	html = [
		'<section class="section" id="line-drilldown">',
		'<div class="section-head">'
		"<h2>Line-Level Drilldown</h2>"
		f'<span class="section-tag">{n_runs} run{"s" if n_runs != 1 else ""}</span>'
		"</div>",
		'<p class="section-intro">'
		"Line-Level Drilldown captures only the code paths you actually ran "
		"during the line-profile pass. Any picked function that was not "
		'exercised is listed under "Not exercised in this pass" below - re-run '
		"the flow that calls it to capture its lines."
		"</p>",
	]

	for run_idx, run in enumerate(parsed_runs, start=1):
		started = _e(run.get("started_at"))
		status = _e(run.get("status", ""))
		total_ms = run.get("total_ms", 0)
		# v0.7.x: the "Picks:" line is dropped — the per-function tables below
		# enumerate the picks that ran, and the "Not exercised in this pass" note
		# lists the rest, so listing all picks again here is redundant.
		html.append(
			'<div class="phase2-run">'
			'<div class="phase2-run-head">'
			f"<strong>Run {run_idx}</strong>"
			'<span class="meta">'
			f'<span class="status-badge status-{status}">{status}</span>'
			f"{_format_duration_ms(total_ms)} &middot; {started}"
			"</span>"
			"</div>"
		)
		# Render only functions that actually ran; collapse the rest into one
		# concise note so the drilldown isn't padded with empty zero-hit tables.
		not_exercised = []
		for fn in run.get("functions", []):
			if _phase2_invoked(fn):
				html.append(_render_phase2_function_table(fn))
			else:
				not_exercised.append(fn.get("dotted_path", "?"))
		if not_exercised:
			html.append(
				'<div class="picks"><em>Not exercised in this pass:</em> '
				+ ", ".join(_e(p) for p in not_exercised)
				+ "</div>"
			)
		html.append("</div>")

	if diffs:
		html.append(
			'<h3 class="frontend-h3">Cross-Run Comparison</h3>'
			'<p class="section-intro">'
			"For functions profiled in two or more runs, the table below shows "
			"a line-by-line delta between the most recent two runs (aligned by "
			"content hash so file edits between runs don't break the diff)."
			"</p>"
		)
		for path, diff_meta in diffs.items():
			label = f"{path} — Run {diff_meta['prev_run_idx'] + 1} → Run {diff_meta['curr_run_idx'] + 1}"
			html.append(f'<div class="phase2-func"><div class="fn-name">{_e(label)}</div>')
			html.append(_render_phase2_diff_table(diff_meta["rows"]))
			html.append("</div>")

	html.append("</section>")
	return "".join(html)


# Back-compat alias — pre-v0.7.x name.
_render_phase2_panel = _render_line_drilldown_panel
