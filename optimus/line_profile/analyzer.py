# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 results processor.

Turns the aggregated `results_json` blob (one entry per picked function with
per-line timings) into report-friendly findings + per-function aggregate.

Two functions, mirroring the phase-1 ``analyze.run`` ↔ ``analyzers/*``
split:

- ``analyze(results_json)`` is the **pure** classifier — testable in
  isolation, no Frappe / Redis access.
- ``run_analyze(session_uuid, run_uuid)`` is the **impure** RQ entry
  point — pulls samples from Redis, calls aggregate_samples, calls
  analyze, persists to the Optimus Phase Two Run row, propagates findings
  to the parent Session, triggers re-render, publishes realtime events.
"""

import json
import re
import traceback

from optimus import safe_commit
from optimus.analyzers.base import AnalyzerResult

try:
	import frappe  # type: ignore[import-not-found]
	_FRAPPE_AVAILABLE = True
except ImportError:
	frappe = None  # type: ignore[assignment]
	_FRAPPE_AVAILABLE = False

# v0.6.0 Round 6: previously-hardcoded thresholds. The High pair is
# read from Optimus Settings; the Medium pair derives from it via a
# fixed multiplier so a single configured threshold drives both.
HOT_LINE_HIGH_FRACTION_FALLBACK = 0.50  # used when settings unreachable
HOT_LINE_HIGH_MIN_MS_FALLBACK = 100.0
HOT_LINE_MEDIUM_FRACTION_MULTIPLIER = 0.5   # Medium = 50% of High pct (preserves 0.25/0.50 ratio)
HOT_LINE_MEDIUM_MIN_MS_MULTIPLIER = 0.5     # Medium = 50% of High ms

# Per-function summary keeps the top-N lines by total_ms in the aggregate
# so the renderer can show a compact "where the time went" panel without
# pulling the full per-line data on the form.
HOT_LINES_IN_SUMMARY = 5


def _resolve_hot_line_thresholds() -> tuple[float, float, float, float]:
	"""Return (high_pct, high_ms, med_pct, med_ms) from Profiler
	Settings (cached). Falls back to legacy constants when settings
	can't be read (pure-test path, fresh install before migrate)."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
		# Settings store percentage as 0-100 for UX; convert here.
		high_pct = float(cfg.hot_line_high_pct or 50.0) / 100.0
		high_ms = float(cfg.hot_line_high_min_ms or HOT_LINE_HIGH_MIN_MS_FALLBACK)
	except Exception:
		high_pct = HOT_LINE_HIGH_FRACTION_FALLBACK
		high_ms = HOT_LINE_HIGH_MIN_MS_FALLBACK
	return (
		high_pct,
		high_ms,
		high_pct * HOT_LINE_MEDIUM_FRACTION_MULTIPLIER,
		high_ms * HOT_LINE_MEDIUM_MIN_MS_MULTIPLIER,
	)


def _classify_hot_line(line_ms: float, total_ms: float) -> str | None:
	"""Return 'High', 'Medium', or None for a candidate hot line.

	The line must concentrate enough fraction AND have enough absolute time
	to be worth flagging — a single line that's 100% of a 30ms function
	isn't actionable noise, but 60% of a 300ms function is.
	"""
	if total_ms <= 0:
		return None
	fraction = line_ms / total_ms
	high_pct, high_ms, med_pct, med_ms = _resolve_hot_line_thresholds()
	if fraction >= high_pct and line_ms >= high_ms:
		return "High"
	if fraction >= med_pct and line_ms >= med_ms:
		return "Medium"
	return None


def _function_invoked(fn: dict) -> bool:
	"""A function is 'invoked' if at least one line has hits > 0 OR
	total_ms > 0. line_profiler may report empty stats (no lines), or the
	full line list with hits=0 — both mean the picked function was never
	executed during the recording."""
	lines = fn.get("lines") or []
	if not lines:
		return False
	return any((line.get("hits") or 0) > 0 or (line.get("total_ms") or 0) > 0 for line in lines)


def _strip_line_comment(content: str) -> str:
	"""Drop a trailing ``# …`` comment from a single Python source line.

	Naively tracks single/double-quoted string state so a ``#`` inside a
	literal doesn't get treated as a comment boundary. Triple-quoted
	strings aren't recognised — line_profiler's per-line ``content``
	carries one source line, so multi-line literals shouldn't appear
	here in practice. A miss only costs a false negative (regex sees
	the commented call and we don't suppress); never drops a real leaf.
	"""
	in_s: str | None = None
	for i, ch in enumerate(content):
		if in_s is not None:
			if ch == in_s and content[i - 1 : i] != "\\":
				in_s = None
			continue
		if ch in ("'", '"'):
			in_s = ch
		elif ch == "#":
			return content[:i]
	return content


def _detect_pass_through_callee(
	content: str,
	self_qualname: str,
	instrumented: set[str],
) -> str | None:
	"""If the hot line is a call into another **instrumented** function,
	return that callee's qualname; otherwise None.

	The hot line content is matched against ``\\bQ\\s*\\(`` for each
	other instrumented qualname Q (self-recursion is excluded so a
	function that calls itself isn't treated as pass-through to itself).
	Line comments are stripped before matching to avoid false positives
	on commented-out code like ``# helper(x)``.

	When multiple instrumented qualnames appear on the same source line
	(e.g. ``a(b(c()))``), the **last** one in the iteration order is
	returned. The caller resolves chains by walking the per-function
	classifications transitively, so picking any one of the present
	qualnames suffices to anchor the chain — the leaf is found by
	following ``passthrough_to`` until ``None``.
	"""
	stripped = _strip_line_comment(content or "")
	if not stripped:
		return None
	last_match: str | None = None
	for q in instrumented:
		if q == self_qualname or not q:
			continue
		if re.search(r"\b" + re.escape(q) + r"\s*\(", stripped):
			last_match = q
	return last_match


def _looks_like_call_site(content: str) -> bool:
	"""True if ``content`` begins with an identifier-call shape like
	``foo(`` or ``module.attr(``. Used to decide whether to attempt a
	phase-1 fallback hint for a leaf finding whose hot line is still
	a call into uninstrumented code.
	"""
	stripped = _strip_line_comment(content or "").lstrip()
	return bool(re.match(r"^[\w.]+\s*\(", stripped))


def _compute_phase1_hint(
	leaf_fn: dict,
	leaf_line: dict,
	call_trees: list[dict] | None,
) -> dict | None:
	"""When the leaf's hot line still looks like a call but no instrumented
	callee matched, look up the leaf frame in phase-1's pyinstrument
	trees and surface the hottest user-code descendant as a hint.

	Returns ``{next_hot_callee, phase1_cumulative_ms, suggested_action}``
	or None when there's no eligible descendant. Reuses
	``picker._find_hottest_match`` / ``_eligible_descent_children`` so
	the framework-boundary stop matches auto_expand's behavior.
	"""
	if not call_trees:
		return None
	content = leaf_line.get("content") or ""
	if not _looks_like_call_site(content):
		return None
	try:
		from optimus.line_profile import picker
	except ImportError:
		return None

	dotted_path = leaf_fn.get("dotted_path") or ""
	if not dotted_path:
		return None
	root = picker._find_hottest_match(call_trees, dotted_path)
	if root is None:
		return None
	children = picker._eligible_descent_children(root, min_ms=50.0)
	if not children:
		return None
	hottest = max(children, key=lambda c: float(c.get("cumulative_ms") or 0))
	callee_qualname = hottest.get("function") or ""
	callee_file = hottest.get("filename") or ""
	callee_dotted = picker._build_dotted_path(callee_file, callee_qualname)
	if not callee_dotted:
		return None
	return {
		"next_hot_callee": callee_dotted,
		"phase1_cumulative_ms": round(float(hottest.get("cumulative_ms") or 0), 2),
		"suggested_action": (
			f"Re-run Phase 2 with {callee_dotted} picked for line-level breakdown."
		),
	}


def _hot_line_finding(fn: dict, line: dict, severity: str) -> dict:
	dotted_path = fn["dotted_path"]
	lineno = line["lineno"]
	content = line["content"]
	total_ms = line["total_ms"]
	hits = line.get("hits") or 0

	return {
		"finding_type": "Hot Line",
		"severity": severity,
		# Title uses the short qualname, not the full dotted_path: deeply-nested
		# module paths overflow Optimus Finding.title's Data(140) field. The full
		# dotted_path stays in the description + technical_detail below.
		"title": (
			f"{_qualname_of(fn)}:{lineno} consumed {total_ms:.0f}ms "
			f"({hits} hits) — single hottest line"
		),
		"customer_description": (
			f"The line **{dotted_path}:{lineno}** is the dominant time sink in "
			f"this function ({total_ms:.0f}ms across {hits} executions). "
			"Optimizing it directly will move the needle on the function's "
			"total cost — line-level timing makes the fix targetable."
		),
		"technical_detail_json": json.dumps({
			"dotted_path": dotted_path,
			"file": fn.get("file"),
			"lineno": lineno,
			"line_content": content,
			"total_ms": round(total_ms, 2),
			"hits": hits,
			"per_hit_us": round(line.get("per_hit_us") or 0, 2),
		}, default=str),
		"estimated_impact_ms": round(total_ms, 2),
		"affected_count": hits,
		"action_ref": None,
	}


def _summary_for_function(fn: dict, invoked: bool) -> dict:
	lines = fn.get("lines") or []
	total_ms = sum((line.get("total_ms") or 0) for line in lines)
	hot_lines: list[dict] = []
	if invoked:
		ranked = sorted(lines, key=lambda l: (l.get("total_ms") or 0), reverse=True)
		for line in ranked[:HOT_LINES_IN_SUMMARY]:
			hot_lines.append({
				"lineno": line["lineno"],
				"content": line.get("content", ""),
				"total_ms": round(line.get("total_ms") or 0, 2),
				"hits": line.get("hits") or 0,
			})
	return {
		"dotted_path": fn["dotted_path"],
		"qualname": fn.get("qualname") or fn["dotted_path"].rsplit(".", 1)[-1],
		"file": fn.get("file"),
		"total_ms": round(total_ms, 2),
		"hot_lines": hot_lines,
	}


def _qualname_of(fn: dict) -> str:
	"""Resolve fn's qualname, falling back to the last segment of its
	dotted_path. Used to key the per-function classification map."""
	return fn.get("qualname") or fn["dotted_path"].rsplit(".", 1)[-1]


def _build_call_chain(
	leaf_qualname: str,
	classifications: dict,
	callers_to: dict,
) -> list[dict]:
	"""Walk upward from a leaf qualname to assemble its call chain.

	Returns an ordered list (outermost → leaf) of step dicts shaped like:
	``{dotted_path, qualname, file, lineno, content, total_ms, hits}``.
	Each step is the *hottest line* of the function at that level of
	the chain (which is the call into the next-deeper instrumented
	function for every non-leaf step, and the actual leaf hot line at
	the end).

	Empty chain (length 0) is returned when the leaf has no upstream
	pass-through caller — i.e. it's a standalone function with no
	wrapper finding to merge into. Single-step "chain" makes no sense
	to render either, so we return [] in that case.

	When the leaf has multiple callers (two functions whose hot lines
	both call into this leaf), the chain follows the **hottest** caller
	at each step — measured by the caller's function-total ``total_ms``.
	"""
	seen: set[str] = {leaf_qualname}
	chain_qualnames: list[str] = [leaf_qualname]
	cursor = leaf_qualname
	while True:
		callers = [q for q in callers_to.get(cursor, []) if q not in seen]
		if not callers:
			break

		def _caller_total_ms(q: str) -> float:
			lines = classifications[q]["fn"].get("lines") or []
			return sum((line.get("total_ms") or 0) for line in lines)

		chosen = max(callers, key=_caller_total_ms)
		chain_qualnames.append(chosen)
		seen.add(chosen)
		cursor = chosen

	if len(chain_qualnames) == 1:
		return []

	# Reverse to outer→leaf order.
	chain_qualnames.reverse()
	chain: list[dict] = []
	for q in chain_qualnames:
		entry = classifications[q]
		fn = entry["fn"]
		hottest = entry.get("hottest") or {}
		chain.append({
			"qualname": q,
			"dotted_path": fn.get("dotted_path") or "",
			"file": fn.get("file"),
			"lineno": hottest.get("lineno"),
			"content": hottest.get("content", ""),
			"total_ms": round(hottest.get("total_ms") or 0, 2),
			"hits": hottest.get("hits") or 0,
		})
	return chain


def _attach_call_chain(finding: dict, chain: list[dict]) -> None:
	"""Inject ``chain`` into the finding's technical_detail_json and
	prepend a one-line breadcrumb to its customer_description."""
	if not chain:
		return
	try:
		td = json.loads(finding["technical_detail_json"])
	except (json.JSONDecodeError, TypeError):
		td = {}
	td["call_chain"] = chain
	finding["technical_detail_json"] = json.dumps(td, default=str)

	breadcrumb = " → ".join(
		f"{step['qualname']}:{step['lineno']}" for step in chain
	)
	prefix = (
		f"Time enters through {breadcrumb}. The deepest line below is "
		"where the cost is actually incurred."
	)
	finding["customer_description"] = prefix + "\n\n" + finding["customer_description"]


def _attach_phase1_hint(finding: dict, hint: dict) -> None:
	"""Inject ``hint`` into the finding's technical_detail_json and append
	a one-line note to its customer_description."""
	if not hint:
		return
	try:
		td = json.loads(finding["technical_detail_json"])
	except (json.JSONDecodeError, TypeError):
		td = {}
	td["phase1_hint"] = hint
	finding["technical_detail_json"] = json.dumps(td, default=str)

	finding["customer_description"] = finding["customer_description"] + (
		f"\n\nIn phase 1, this descendant **{hint['next_hot_callee']}** "
		f"accumulated {hint['phase1_cumulative_ms']:.0f}ms across all calls "
		f"of the parent function (not a single-action wall time). "
		f"{hint['suggested_action']}"
	)


def analyze(
	results_json: list[dict],
	call_trees: list[dict] | None = None,
) -> AnalyzerResult:
	"""Pure: turn the merged phase-2 ``results_json`` into an AnalyzerResult.

	Input shape (one entry per picked function)::

	    [{
	        "dotted_path": "my_app.tasks.heavy",
	        "qualname":    "heavy",
	        "file":        "/abs/path.py",
	        "lines": [{"lineno", "content", "content_hash", "hits",
	                   "total_ms", "per_hit_us"}, ...],
	    }, ...]

	``call_trees`` is the optional list of phase-1 pyinstrument trees
	for the parent session (one per recorded action). When provided, a
	leaf finding whose hot line still looks like a call into
	uninstrumented code receives a ``phase1_hint`` pointing at the
	hottest user-code descendant in phase-1's tree, so the developer
	knows which function to re-pick for a deeper drill-in.

	Output:
	  - findings: ``Hot Line`` (High/Medium) per **leaf** function whose
	    hot line is the real cost driver. Functions whose hot line is
	    just a call into another instrumented function are suppressed —
	    their position is preserved as a breadcrumb on the deeper finding.
	    ``Function Not Invoked`` (Low) per pick that recorded nothing.
	  - aggregate: ``{phase2_functions: [per-function summary, ...]}`` —
	    each summary carries top-5 lines by ``total_ms``.
	  - warnings: human-readable strings for the report's warnings panel.
	"""
	findings: list[dict] = []
	summaries: list[dict] = []
	warnings: list[str] = []

	instrumented_qualnames = {_qualname_of(fn) for fn in results_json}

	# Phase A: classify each function's hot line as leaf or pass-through.
	classifications: dict[str, dict] = {}
	for fn in results_json:
		qualname = _qualname_of(fn)
		invoked = _function_invoked(fn)
		summaries.append(_summary_for_function(fn, invoked))
		if not invoked:
			classifications[qualname] = {"invoked": False, "fn": fn}
			continue
		lines = fn["lines"]
		hottest = max(lines, key=lambda l: l.get("total_ms") or 0)
		passthrough_to = _detect_pass_through_callee(
			hottest.get("content") or "",
			qualname,
			instrumented_qualnames,
		)
		# Phase-1 ancestry fallback: the wrapper's hot line might call
		# an uninstrumented intermediate (regex misses it) whose own
		# descendant IS instrumented. Walking phase-1's call tree
		# bridges that gap.
		if passthrough_to is None and call_trees:
			try:
				from optimus.line_profile.picker import (
					deepest_instrumented_descendant,
				)
			except ImportError:
				deepest_instrumented_descendant = None  # type: ignore[assignment]
			if deepest_instrumented_descendant is not None:
				for tree in call_trees:
					candidate = deepest_instrumented_descendant(
						tree, qualname, instrumented_qualnames,
					)
					if candidate and candidate != qualname:
						passthrough_to = candidate
						break
		classifications[qualname] = {
			"invoked": True,
			"fn": fn,
			"hottest": hottest,
			"passthrough_to": passthrough_to,
		}

	# Reverse map: callee qualname → list of caller qualnames whose hot
	# line passes through to it. Drives chain-building from the leaf up.
	callers_to: dict[str, list[str]] = {}
	for caller_q, c in classifications.items():
		callee = c.get("passthrough_to") if c.get("invoked") else None
		if callee:
			callers_to.setdefault(callee, []).append(caller_q)

	# Phase B: emit findings.
	# v0.7.x: uninvoked picks no longer each emit a "Function Not Invoked"
	# finding (they cluttered the Findings list). Collect them and fold into a
	# single consolidated warning after the loop.
	uninvoked_paths: list[str] = []
	for qualname, c in classifications.items():
		if not c["invoked"]:
			uninvoked_paths.append(c["fn"].get("dotted_path") or qualname)
			continue
		if c["passthrough_to"]:
			# Suppress — defer to the deeper leaf's finding. The chain is
			# rebuilt when we visit that leaf below.
			continue
		fn = c["fn"]
		hottest = c["hottest"]
		total_ms = sum((line.get("total_ms") or 0) for line in fn["lines"])
		severity = _classify_hot_line(hottest.get("total_ms") or 0, total_ms)
		if not severity:
			continue
		finding = _hot_line_finding(fn, hottest, severity)
		chain = _build_call_chain(qualname, classifications, callers_to)
		if chain:
			_attach_call_chain(finding, chain)
		else:
			# No upstream pass-through caller — try the phase-1 fallback
			# hint for the rarer case where this finding's hot line is
			# itself a call into uninstrumented code.
			hint = _compute_phase1_hint(fn, hottest, call_trees)
			if hint:
				_attach_phase1_hint(finding, hint)
		findings.append(finding)

	if uninvoked_paths:
		warnings.append(
			f"{len(uninvoked_paths)} picked function(s) weren't exercised in this "
			f"pass: {', '.join(uninvoked_paths)}. Re-run the flow that calls them "
			"to capture their lines."
		)

	return AnalyzerResult(
		findings=findings,
		aggregate={"phase2_functions": summaries},
		warnings=warnings,
	)


# ---------------------------------------------------------------------------
# Orchestrator (impure — frappe required)
# ---------------------------------------------------------------------------


def _publish(event: str, payload: dict) -> None:
	"""Best-effort realtime event for the floating widget + form."""
	try:
		frappe.publish_realtime(event, payload, user=payload.get("user"))
	except Exception:
		pass


def run_analyze(session_uuid: str, run_uuid: str) -> None:
	"""RQ entry point. Reads phase-2 samples from Redis, builds the
	results_json, persists findings to the parent Optimus Session, marks
	the Phase 2 Run as Ready (or Failed), and triggers re-render.

	On any uncaught exception: rollback, mark Failed, publish failed event,
	re-raise so RQ logs it.
	"""
	if not _FRAPPE_AVAILABLE:
		raise RuntimeError("frappe not importable — run under bench")

	from optimus.line_profile import capture

	# Lookup the Phase 2 Run row + parent Session docname
	run_row = _find_run_row(session_uuid, run_uuid)
	if run_row is None:
		raise RuntimeError(
			f"Phase 2 Run {run_uuid} not found on session {session_uuid}"
		)
	parent_docname = run_row.parent

	try:
		_publish("phase_2_run_analyzing", {
			"session_uuid": session_uuid,
			"run_uuid": run_uuid,
		})

		# Drain samples + load picks meta with source snapshot.
		samples = capture.read_all_samples(run_uuid)
		picks = capture.read_picks_meta(run_uuid)

		# Aggregate raw samples into the analyzer's input shape, then run
		# the pure classifier. Phase-1's pyinstrument call trees feed the
		# ancestry-based pass-through detection so wrappers/leaves
		# instrumented across a thin uninstrumented intermediate still
		# merge into a single deepest finding.
		results_json = capture.aggregate_samples(samples, picks)
		call_trees: list[dict] = []
		try:
			parent_session = frappe.get_doc("Optimus Session", parent_docname)
			for action in (parent_session.actions or []):
				raw_tree = getattr(action, "call_tree_json", None)
				if not raw_tree:
					continue
				try:
					tree = json.loads(raw_tree)
				except (TypeError, ValueError):
					continue
				if isinstance(tree, dict) and "root" in tree:
					tree = tree["root"]
				if isinstance(tree, dict):
					call_trees.append(tree)
		except Exception:
			# Loading phase-1 trees is best-effort — without them we
			# fall back to regex-only pass-through detection.
			call_trees = []
		result = analyze(results_json, call_trees=call_trees or None)
		# Observe, don't spoil: if the overhead watchdog cut tracing short to
		# protect the user's flow, the line data is partial — say so (read the
		# flag before cleanup_run clears it below).
		if capture.budget_was_hit(run_uuid):
			result.warnings.append(
				"Line profiling was time-budgeted to avoid freezing the flow, so "
				"results are partial for the longest-running call(s). Tune "
				"optimus_phase2_overhead_budget_seconds in site_config (lower = "
				"snappier flow, higher = fuller data; 0 = unlimited)."
			)
		total_ms = sum(s.get("total_ms") or 0 for s in result.aggregate.get("phase2_functions", []))

		# Persist to the run row + propagate findings to the parent session.
		_persist_run(parent_docname, run_uuid, results_json, result, total_ms)

		# Re-render the parent session's report so the new phase-2
		# panel appears. Reuses the existing regenerate_reports code
		# path (which expects session_uuid, not docname).
		_regenerate_parent_reports(session_uuid)

		# Done — drop ephemeral Redis state.
		capture.cleanup_run(run_uuid)

		_publish("phase_2_run_ready", {
			"session_uuid": session_uuid,
			"run_uuid": run_uuid,
			"parent": parent_docname,
		})

	except Exception as exc:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		_mark_run_failed(parent_docname, run_uuid, str(exc), traceback.format_exc())
		_publish("phase_2_run_failed", {
			"session_uuid": session_uuid,
			"run_uuid": run_uuid,
			"error": str(exc),
		})
		raise


def _find_run_row(session_uuid: str, run_uuid: str):
	"""Return the Optimus Phase Two Run child row whose parent session
	matches session_uuid. None if not found."""
	parent_docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name"
	)
	if not parent_docname:
		return None
	matches = frappe.get_all(
		"Optimus Phase Two Run",
		filters={"parent": parent_docname, "run_uuid": run_uuid},
		fields=["name", "parent"],
		limit=1,
	)
	if not matches:
		return None
	# Return a tiny shape with .parent for the caller's convenience.
	row = matches[0]

	class _Row:
		pass

	r = _Row()
	r.name = row["name"]
	r.parent = row["parent"]
	return r


def _persist_run(
	parent_docname: str,
	run_uuid: str,
	results_json: list,
	result: AnalyzerResult,
	total_ms: float,
) -> None:
	"""Write results back to the run row + append findings to the parent
	session's findings child table."""
	parent = frappe.get_doc("Optimus Session", parent_docname)

	# Update the matching child row in place.
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.results_json = json.dumps(results_json, default=str)
			child.warnings_json = json.dumps(result.warnings, default=str)
			child.total_ms = round(total_ms, 2)
			child.status = "Ready"
			child.ended_at = frappe.utils.now_datetime()
			break

	# Promote findings into the unified Session.findings table so the
	# existing finding rendering / filtering picks them up alongside
	# phase-1 findings.
	for finding in result.findings:
		# Optimus Finding.title is a Data(140) field. Phase-2 findings are
		# appended directly here (bypassing analyze._truncate_finding_titles),
		# so clamp defensively — a long title must not trip Frappe's truncation
		# warning while stopping phase 2. Full context lives in the description /
		# technical_detail_json.
		_title = finding.get("title") or ""
		if len(_title) > 140:
			finding["title"] = _title[:137].rstrip() + "..."
		parent.append("findings", finding)

	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	safe_commit()


def _mark_run_failed(parent_docname: str, run_uuid: str, error: str, tb: str) -> None:
	"""Best-effort: set the run row's status to Failed with the error
	message in warnings_json. Tolerant of missing parent / row so the
	caller can still re-raise cleanly."""
	if not parent_docname:
		return
	try:
		parent = frappe.get_doc("Optimus Session", parent_docname)
		for child in (parent.phase_2_runs or []):
			if child.run_uuid == run_uuid:
				child.status = "Failed"
				child.warnings_json = json.dumps([
					f"phase 2 analyze failed: {error}",
					tb,
				], default=str)
				child.ended_at = frappe.utils.now_datetime()
				break
		parent.flags.ignore_validate_update_after_submit = True
		parent.save(ignore_permissions=True)
		safe_commit()
	except Exception:
		# Truly best-effort — don't mask the original exception.
		try:
			frappe.db.rollback()
		except Exception:
			pass


def _regenerate_parent_reports(session_uuid: str) -> None:
	"""Trigger re-render of the parent Optimus Session's HTML reports.

	Phase 1's existing ``api.regenerate_reports(session_uuid)`` does the
	same job for phase-1 findings; reuse it so phase-2 doesn't get a
	bespoke re-render path that drifts from the canonical one.
	"""
	try:
		from optimus import api as optimus_api

		optimus_api.regenerate_reports(session_uuid)  # type: ignore[attr-defined]
	except Exception as exc:
		# Re-render failure is non-fatal: data is persisted, the customer
		# just needs to click "Regenerate Reports" manually. Surface the
		# error in the run row for debuggability.
		frappe.log_error(
			title="phase 2 re-render failed",
			message=f"{session_uuid}: {exc}\n{traceback.format_exc()}",
		)
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"phase2.rerender_failed", exc,
				context={"session_uuid": session_uuid or ""},
			)
		except Exception:
			pass
