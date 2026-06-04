# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase J adapter — transform our flat render-context into the 19-key
contract shape per ``template_variable_contract.md`` (in the reference
design package).

This module is the adapter half of the reference architecture: ``renderer.py``
keeps producing its existing 45-key flat context, and ``build_report_context``
turns that into the canonical nested-dict shape the reference template
expects. Phase J.1 (this module) exposes the contract data under a
``report_data`` namespace so template references can migrate section-by-
section in Phase J.2 without breaking. Phase J.3 will unpack the namespace
and drop the legacy duplicates.
"""

from __future__ import annotations

import html as _html
import json as _json
from typing import Any

from markupsafe import Markup

# ---------------------------------------------------------------------------
# Helpers — small pure functions called by sub-builders below
# ---------------------------------------------------------------------------


def _web_vital_class(value, good_threshold, poor_threshold) -> str:
	"""Map a web-vital value to its rating class per web.dev thresholds.

	Returns ``vital-good`` / ``vital-meh`` / ``vital-poor`` / ``vital-none``
	(the four CSS classes the template uses to colour the cell).
	"""
	if value is None or value == 0:
		return "vital-none"
	if value < good_threshold:
		return "vital-good"
	if value > poor_threshold:
		return "vital-poor"
	return "vital-meh"


def _bar_kind_for(duration_ms) -> str | None:
	"""Per-action / per-job bar colour key.

	``duration ≥ 1000ms`` → ``None`` (red, contract's default).
	``300 ≤ duration < 1000`` → ``"warn"`` (amber).
	``duration < 300`` → ``"ok"`` (green).
	"""
	if duration_ms is None:
		return "ok"
	if duration_ms >= 1000:
		return None
	if duration_ms >= 300:
		return "warn"
	return "ok"


def _is_user_code(function_or_path, ignored_apps: tuple[str, ...] = ()) -> bool:
	"""``True`` if the function/path does not start with any ignored-app prefix.

	Used to highlight hot-frames rows that live in user code (vs framework
	internals where the user can't act).
	"""
	if not function_or_path:
		return False
	text = str(function_or_path)
	for prefix in ignored_apps or ():
		if text.startswith(prefix + "/") or text.startswith(prefix + "."):
			return False
	return True


def _ms_display(ms) -> str:
	"""Format milliseconds for display per the contract's mixed-unit style:
	below 1000ms → integer ms; at-or-above → seconds with 2 decimals."""
	if ms is None:
		return ""
	if ms < 1000:
		return f"{ms:.0f} ms"
	return f"{ms / 1000:.2f} s"


# ---------------------------------------------------------------------------
# Sub-builders — one per contract key, in contract order
# ---------------------------------------------------------------------------


def _build_session(session_doc, ctx) -> dict:
	"""Contract ``session`` = {id, recorded_by, started_at, ended_at, timezone, generated_at}."""
	fmt_dt = ctx.get("fmt_dt") or (lambda v: str(v) if v else "")
	return {
		"id": getattr(session_doc, "session_uuid", None) or getattr(session_doc, "name", "") or "",
		"recorded_by": getattr(session_doc, "user", "") or "",
		"started_at": fmt_dt(getattr(session_doc, "started_at", None)),
		"ended_at": fmt_dt(
			getattr(session_doc, "stopped_at", None) or getattr(session_doc, "ended_at", None)
		),
		"timezone": ctx.get("server_tz") or "",
		"generated_at": ctx.get("generated_at") or "",
	}


def _build_tldr(tldr) -> dict:
	"""Contract ``tldr`` = {headline_html, subline_html}.

	Our internal tldr has ``headline_markup`` / ``sub_markup`` (Markup
	objects). Stringify and rename.
	"""
	if not tldr:
		return {"headline_html": "", "subline_html": ""}
	return {
		"headline_html": str(tldr.get("headline_markup") or tldr.get("headline_html") or ""),
		"subline_html": str(tldr.get("sub_markup") or tldr.get("subline_html") or ""),
	}


def _build_kpis(session_doc, ctx) -> list[dict]:
	"""Contract ``kpis`` = exactly 4 items: total time, queries, ops, findings.

	Labels and sub-line wording mirror the pre-J.2 template literally so the
	migration is a binding-only change (no tested-text surface moves). The
	severity breakdown comes from ``ctx.severity_counts`` (computed in
	``renderer.render``) and the danger thresholds from ``render_config``.
	"""
	fmt_ms = ctx.get("fmt_ms") or (lambda v, **kw: f"{v:.0f}ms")
	total_ms = getattr(session_doc, "total_duration_ms", 0) or 0
	total_query_ms = getattr(session_doc, "total_query_time_ms", 0) or 0
	total_queries = getattr(session_doc, "total_queries", 0) or 0
	total_requests = getattr(session_doc, "total_requests", 0) or 0

	render_config = ctx.get("render_config") or {}
	threshold_ms = render_config.get("large_duration_threshold_ms") or 1000

	all_findings = ctx.get("all_findings") or ctx.get("findings") or []
	total_findings = len(all_findings)
	severity_counts = ctx.get("severity_counts") or {}
	high = severity_counts.get("High", 0) or 0
	medium = severity_counts.get("Medium", 0) or 0
	low = severity_counts.get("Low", 0) or 0

	# "X high · Y medium · Z low" — only non-zero buckets, separator-joined.
	sev_parts = []
	if high:
		sev_parts.append(f"{high} high")
	if medium:
		sev_parts.append(f"{medium} medium")
	if low:
		sev_parts.append(f"{low} low")
	findings_sub = " · ".join(sev_parts) if sev_parts else "none detected"

	server_ms = total_ms - total_query_ms
	# Markup-aware concat — fmt_ms returns Markup for ≥1s values
	# (`<span class="time-high">…</span>`). f-string would coerce to plain
	# str and Jinja autoescape would HTML-encode the span.
	total_time_sub = (
		fmt_ms(server_ms) + Markup(" server · ") + fmt_ms(total_query_ms) + Markup(" DB")
	)

	ops_plural = "s" if total_requests != 1 else ""

	# C.UT2 — Total-time KPI carries a caveat about wall-clock sampling.
	# The interval is dynamic (Optimus Settings); read once for the sub-line.
	try:
		from optimus.settings import get_config as _get_cfg
		_sampler_ms = float(_get_cfg().pyinstrument_sampler_interval_ms or 1.0)
	except Exception:
		_sampler_ms = 1.0

	return [
		{
			"label": "Total time",
			"value": fmt_ms(total_ms),
			"unit": "",
			"sub": total_time_sub,
			"caveat": (
				f"Call-tree timings sampled at ~{_sampler_ms:g}ms intervals; "
				"sub-interval functions can be under-counted."
			),
			"is_danger": total_ms >= threshold_ms,
		},
		{
			"label": "Database queries",
			"value": str(total_queries),
			"unit": "",
			"sub": f"across {total_requests} operation{ops_plural}",
			"caveat": None,
			"is_danger": False,
		},
		{
			"label": "Operations",
			"value": str(total_requests),
			"unit": "",
			"sub": "page loads · saves · jobs",
			"caveat": None,
			"is_danger": False,
		},
		{
			"label": "Issues found",
			"value": str(total_findings),
			"unit": "",
			"sub": findings_sub if total_findings else "none detected",
			"caveat": None,
			"is_danger": high > 0,
		},
	]


def _build_repro(notes_html) -> dict | None:
	"""Contract ``repro`` = {title, steps}. Defer parsing to J.2.

	For J.1 we expose the sanitized markup as ``raw_html`` plus empty
	``title``/``steps``. The template migration in J.2 will decide whether
	to parse the ``<ol><li>`` into the structured fields or render the
	raw markup; the contract permits either.
	"""
	if not notes_html:
		return None
	return {"title": "", "steps": [], "raw_html": notes_html}


def _build_summary(summary_html) -> dict | None:
	"""Contract ``summary`` = {paragraphs_html: [...]}.

	Our existing summary is one HTML chunk (``<ul>`` of bullets). Wrap in
	a single-paragraph list — the contract permits HTML inside each entry.
	"""
	if not summary_html:
		return None
	return {"paragraphs_html": [summary_html]}


def _build_smoking_code_html(snippet, target_lineno) -> str:
	"""Wrap a ``source_snippet`` list in contract-shaped HTML.

	Skips blank context lines (PEP-8 spacers); the target line is always
	rendered, wrapped in ``<span class="hot-line">``. Matches the Phase
	I.5 contract from the smoking-gun panel.
	"""
	if not snippet:
		return ""
	parts = []
	for sl in snippet:
		lineno = sl.get("lineno")
		content = sl.get("content", "") or ""
		if not content.strip() and lineno != target_lineno:
			continue
		escaped = _html.escape(content)
		if lineno == target_lineno:
			parts.append(f'<span class="hot-line"><span class="ln">{lineno}</span>{escaped}</span>')
		else:
			parts.append(f'<span class="ln">{lineno}</span>{escaped}')
	return "\n".join(parts)


def _build_findings(findings, ctx) -> list[dict]:
	"""Contract ``findings`` = per-finding {severity, title_html, file_line,
	impact_display, impact_sub, smoking_label, smoking_code_html,
	smoking_footnote_html, chain, ai_fix}.
	"""
	severity_map = {"high": "high", "medium": "med", "low": "low"}
	result = []
	for f in findings or []:
		detail = f.get("technical_detail") or {}
		callsite = detail.get("callsite") or {}
		impact_ms = f.get("estimated_impact_ms") or 0
		affected = f.get("affected_count") or 0
		severity_raw = (f.get("severity") or "").lower()
		severity = severity_map.get(severity_raw, severity_raw or "low")

		file_line_parts = []
		if callsite.get("filename"):
			file_line_parts.append(f"{callsite['filename']}:{callsite.get('lineno', '')}")
		if callsite.get("function"):
			file_line_parts.append(f"· {callsite['function']}")
		file_line = " ".join(file_line_parts)

		impact_sub_parts = []
		if affected:
			impact_sub_parts.append(f"{affected}× hits")
		finding_type = f.get("finding_type") or ""
		if finding_type:
			impact_sub_parts.append(finding_type.lower())
		impact_sub = " · ".join(impact_sub_parts)

		chain = []
		chain_steps = detail.get("call_chain") or detail.get("drilldown_chain") or []
		for step in chain_steps:
			label = f"{step.get('function') or step.get('qualname', '')}"
			if step.get("lineno"):
				label = f"{label}:{step['lineno']}"
			chain.append({"label": label, "terminal": False})
		if chain:
			chain[-1]["terminal"] = True

		ai_fix = f.get("llm_fix")
		ai_fix_dict = None
		if ai_fix:
			ai_fix_dict = {
				"model": ai_fix.get("model", ""),
				"diagnosis_html": ai_fix.get("diagnosis_html", "") or ai_fix.get("description_html", ""),
				"patch_html": ai_fix.get("patch_html", "") or ai_fix.get("code_html", ""),
				"rationale_html": ai_fix.get("rationale_html", "") or ai_fix.get("why_html", ""),
				"verify_html": ai_fix.get("verify_html", "") or ai_fix.get("verify", ""),
			}

		entry = dict(f)  # preserve all original finding fields
		entry.update({
			"severity": severity,
			"title_html": f.get("title", "") or "",
			"file_line": file_line,
			"impact_display": _ms_display(impact_ms) if impact_ms else "",
			"impact_sub": impact_sub,
			"smoking_label": "The hot line",
			"smoking_code_html": _build_smoking_code_html(
				callsite.get("source_snippet", []), callsite.get("lineno")
			),
			"smoking_footnote_html": None,
			"chain": chain,
			"ai_fix": ai_fix_dict,
		})
		result.append(entry)
	return result


def _build_line_drilldown_runs(session_doc) -> list[dict]:
	"""Contract ``line_drilldown_runs`` (J.16 renamed from
	``phase2_runs``) = list of {number, status, total_ms_display,
	timestamp, picks, functions}.
	"""
	runs = getattr(session_doc, "phase_2_runs", None) or []
	result = []
	number = 0
	for run in runs:
		status = getattr(run, "status", "") or ""
		if status != "Ready":
			continue
		number += 1
		total_ms = getattr(run, "total_ms", 0) or 0
		picks_json = getattr(run, "picks_json", "") or "[]"
		try:
			picks_list = _json.loads(picks_json) if picks_json else []
		except (ValueError, TypeError):
			picks_list = []
		results_json = getattr(run, "results_json", "") or "[]"
		try:
			functions_raw = _json.loads(results_json) if results_json else []
		except (ValueError, TypeError):
			functions_raw = []

		pick_source_by_path = {
			p.get("dotted_path", ""): p.get("source", "curated") for p in picks_list
		}

		functions = []
		for fn in functions_raw:
			dotted = fn.get("dotted_path", "")
			source = pick_source_by_path.get(dotted, "curated")
			indent = 1 if source == "auto_expand" else 0
			raw_lines = fn.get("lines") or []
			hot_idx = None
			if raw_lines:
				hot_idx = max(range(len(raw_lines)), key=lambda i: raw_lines[i].get("total_ms", 0))
				if raw_lines[hot_idx].get("total_ms", 0) <= 0:
					hot_idx = None
			lines = []
			for i, line in enumerate(raw_lines):
				per_hit_us = line.get("per_hit_us", 0) or 0
				lines.append({
					"lineno": line.get("lineno", 0),
					"hits": line.get("hits", 0),
					"total_display": f"{line.get('total_ms', 0):.2f} ms",
					"per_hit_display": (
						f"{per_hit_us / 1000:.4f} ms" if per_hit_us else "0.00 ms"
					),
					"source": line.get("content", ""),
					"is_hot": i == hot_idx,
				})
			functions.append({
				"indent": indent,
				"qualified_name": dotted,
				"path": fn.get("file", ""),
				"lines": lines,
			})

		result.append({
			"number": number,
			"status": status,
			"total_ms_display": f"{total_ms:.2f} ms",
			"timestamp": str(getattr(run, "started_at", "") or ""),
			"picks": [p.get("dotted_path", "") for p in picks_list],
			"functions": functions,
		})
	return result


def _build_action_plan(action_plan, fmt_ms=None) -> list[dict]:
	"""Contract ``action_plan`` = {number, title_html, description_html,
	savings_display, savings_label}.

	We also expose ``callsite`` (a non-contract field) so the template can
	render a one-line ``file:line`` anchor under each step; the contract
	folds this into ``description_html`` but our markup keeps it separate.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	result = []
	for step in action_plan or []:
		gain_ms = step.get("gain_ms", 0) or 0
		result.append({
			"number": step.get("n", 0),
			"title_html": step.get("title", "") or "",
			"description_html": step.get("desc", "") or "",
			# Use Markup so the `<span class="time-high">…</span>` that
			# ``_format_duration_ms`` returns for ≥1s values isn't escaped
			# when the template renders it. f-string would coerce Markup to
			# plain str and lose the safe-flag.
			"savings_display": Markup("−") + fmt(gain_ms) if gain_ms else "",
			"savings_label": step.get("gain_label", "") or "",
			"callsite": step.get("callsite") or "",
		})
	return result


def _build_waterfall(waterfall_rows, fmt_ms=None) -> list[dict]:
	"""Contract ``waterfall`` = {name, width_pct, duration_display, kind, is_hot_text}."""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	result = []
	for row in waterfall_rows or []:
		is_hot = bool(row.get("hot", False))
		is_bg = bool(row.get("bg", False))
		result.append({
			"name": row.get("name", "") or "",
			"width_pct": row.get("pct", 0),
			"duration_display": fmt(row.get("duration_ms", 0)),
			"kind": "hot" if is_hot else ("bg" if is_bg else "normal"),
			"is_hot_text": is_hot,
		})
	return result


def _build_actions(actions, findings, fmt_ms=None) -> list[dict]:
	"""Contract ``actions`` = {number, name, meta, kind, duration_display,
	duration_pct, duration_is_hot, bar_kind, queries, db_time_display,
	finding_inline_html}.

	J.2.3: We *spread the original action dict* so the existing
	``action_row`` macro (which reads ``action_label``, ``http_method``,
	``path``, ``entry_callsite``, ``target_doc``, ``related_findings`` and
	more) keeps working unchanged. Contract fields are added on top — they
	never collide with the legacy keys.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	findings_by_ref: dict[str, list] = {}
	for f in findings or []:
		ref = str(f.get("action_ref") or "")
		if ref:
			findings_by_ref.setdefault(ref, []).append(f)

	max_ms = max((a.get("duration_ms", 0) for a in (actions or [])), default=1) or 1

	result = []
	for idx, action in enumerate(actions or []):
		duration_ms = action.get("duration_ms", 0) or 0
		event_type = action.get("event_type", "")
		kind = "bg" if event_type == "RQ Job" else "http"

		meta_parts = []
		if action.get("http_method"):
			meta_parts.append(action["http_method"])
		if action.get("path"):
			meta_parts.append(action["path"])
		meta = " · ".join(meta_parts)

		ref_findings = findings_by_ref.get(str(idx + 1)) or findings_by_ref.get(str(idx), [])
		finding_inline_html = None
		if ref_findings:
			f0 = ref_findings[0]
			finding_inline_html = (
				f"⚠ <strong>{len(ref_findings)} finding linked:</strong> "
				f"{_html.escape(f0.get('title', '') or '')}"
			)

		entry = dict(action)  # preserve original keys
		entry.update({
			"number": idx + 1,
			"name": action.get("action_label", "") or "",
			"meta": meta,
			"kind": kind,
			"duration_display": fmt(duration_ms),
			"duration_pct": (duration_ms / max_ms) * 100 if max_ms else 0,
			"duration_is_hot": duration_ms >= 1000,
			"bar_kind": _bar_kind_for(duration_ms),
			"queries": action.get("queries_count", 0) or 0,
			"db_time_display": fmt(action.get("query_time_ms", 0) or 0),
			"finding_inline_html": finding_inline_html,
		})
		result.append(entry)
	return result


def _build_background_jobs(jobs, fmt_ms=None) -> list[dict]:
	"""Contract ``background_jobs`` = list of {number, name, meta,
	duration_display, duration_pct, duration_is_hot, bar_kind, queries,
	db_time_display, finding_count}.

	J.2.3: spreads the original job dict (preserves ``method``,
	``entry_callsite``, ``related_findings``, ``top_queries``, etc.) so the
	existing ``bg_job_row`` macro reads from the same fields it used to.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	jobs = jobs or []
	max_ms = max((j.get("duration_ms", 0) for j in jobs), default=1) or 1
	result = []
	for idx, job in enumerate(jobs):
		duration_ms = job.get("duration_ms", 0) or 0
		callsite = job.get("entry_callsite") or {}
		meta = ""
		if callsite.get("filename"):
			meta = f"{callsite['filename']}:{callsite.get('lineno', '')}"
		entry = dict(job)  # preserve original keys
		entry.update({
			"number": idx + 1,
			"name": job.get("method", "") or "",
			"meta": meta,
			"duration_display": fmt(duration_ms),
			"duration_pct": (duration_ms / max_ms) * 100 if max_ms else 0,
			"duration_is_hot": duration_ms >= 1000,
			"bar_kind": _bar_kind_for(duration_ms),
			"queries": job.get("queries_count", 0) or 0,
			"db_time_display": fmt(job.get("query_time_ms", 0) or 0),
			"finding_count": job.get("findings_count", 0) or 0,
			# Also coerce the ORIGINAL ``findings_count`` (with 's') key —
			# the existing line above only adds a new ``finding_count``
			# (without 's'). Jobs that Failed before producing findings
			# carry ``findings_count: None`` from analyze; the bg_jobs
			# section template iterates with ``map(attribute='findings_count')
			# | sum`` and Jinja's ``map('default', 0)`` filter only handles
			# Undefined (not None), so an uncoerced None crashes the whole
			# render with ``unsupported operand type(s) for +: 'int' and
			# 'NoneType'``. The fix is render-time so it applies on
			# regenerate_reports too — no re-analyze needed.
			"findings_count": int(job.get("findings_count") or 0),
		})
		result.append(entry)
	return result


def _build_doc_events(doc_event_breakdown, fmt_ms=None) -> list[dict]:
	"""Contract ``doc_events`` = list of {name, summary, methods}.

	We extend the contract with ``is_save_target`` / ``touched_during`` on
	each doctype and ``vscode_link`` / ``count`` on each hook so our
	existing markup (which surfaces those distinctions) keeps working
	without going back to ``doc_event_breakdown``. ``summary`` stays
	contract-conformant for tooling that reads it.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	result = []
	for entry in (doc_event_breakdown or {}).get("doctypes", []) or []:
		methods_out = []
		for ev in entry.get("events", []) or []:
			hooks = []
			for m in ev.get("methods", []) or []:
				hooks.append({
					"name": m.get("function", "") or "",
					"path": f"{m.get('filename', '')}:{m.get('lineno', '')}",
					"vscode_link": m.get("_abs"),
					"lineno": m.get("lineno", 0),
					"filename": m.get("filename", ""),
					"kind": m.get("kind", "doc_events hook"),
					"time_display": fmt(m.get("ms", 0) or 0),
					"count": m.get("count", 1) or 1,
				})
			methods_out.append({
				"name": ev.get("event", "") or "",
				"time_display": fmt(ev.get("total_ms", 0) or 0),
				"hooks": hooks,
			})
		method_count = entry.get("method_count", 0) or 0
		total_ms = entry.get("total_ms", 0) or 0
		# Markup-aware so the `<span class="time-high">…</span>` that
		# ``fmt`` returns for ≥1s totals survives autoescape intact.
		summary = Markup(
			f"{method_count} hot method{'s' if method_count != 1 else ''} · ~"
		) + fmt(total_ms)
		result.append({
			"name": entry.get("doctype", "") or "",
			"summary": summary,
			"is_save_target": bool(entry.get("is_save_target", False)),
			"touched_during": entry.get("touched_during") or [],
			"method_count": method_count,
			"total_ms_display": fmt(total_ms),
			"methods": methods_out,
		})
	return result


def _build_resource(infra_summary, infra_timeline) -> dict | None:
	"""Contract ``resource`` = {cards: list of 4 KPI cards}.

	Cards: CPU avg/peak, RSS delta, Swap peak, Load peak. Each has
	{label, value, unit, sub_html, value_kind, sub_is_warn}.
	"""
	infra_summary = infra_summary or {}
	if not infra_summary and not infra_timeline:
		return None

	cards = []

	cpu_avg = infra_summary.get("cpu_avg")
	cpu_peak = infra_summary.get("cpu_peak")
	if cpu_avg is not None or cpu_peak is not None:
		peak = cpu_peak or 0
		value_kind = "danger" if peak >= 80 else ("warn" if peak >= 50 else "normal")
		cards.append({
			"label": "CPU avg / peak",
			"value": f"{cpu_avg or 0:.0f}",
			"unit": "%",
			"sub_html": f"peak {peak:.0f}%",
			"value_kind": value_kind,
			"sub_is_warn": peak >= 50,
		})

	rss_delta = infra_summary.get("rss_delta")
	if rss_delta is not None:
		delta_mb = rss_delta / (1024 * 1024)
		cards.append({
			"label": "RSS delta",
			"value": f"{delta_mb:+.0f}",
			"unit": "MB",
			"sub_html": "process memory change",
			"value_kind": "warn" if abs(delta_mb) > 200 else "normal",
			"sub_is_warn": False,
		})

	swap_peak_mb = infra_summary.get("swap_peak_mb")
	if swap_peak_mb is not None:
		cards.append({
			"label": "Swap peak",
			"value": f"{swap_peak_mb:.0f}",
			"unit": "MB",
			"sub_html": "swap in use" if swap_peak_mb > 0 else "no swap activity",
			"value_kind": "warn" if swap_peak_mb > 0 else "normal",
			"sub_is_warn": swap_peak_mb > 0,
		})

	load_peak = infra_summary.get("load_peak")
	if load_peak is not None:
		cards.append({
			"label": "Load peak",
			"value": f"{load_peak:.2f}",
			"unit": "",
			"sub_html": "",
			"value_kind": "warn" if load_peak >= 2 else "normal",
			"sub_is_warn": False,
		})

	if not cards:
		return None
	# J.2.4 non-contract additions: keep the raw summary + timeline reachable
	# under ``report_data.resource`` so the existing per-action infra table
	# and the inline rc-card markup migrate as a straight key rename.
	# J.13: normalise the timeline's snapshotted action labels — the
	# rows were stamped by infra_pressure.analyze at analyse-time and
	# carry the pre-J.12 ``"Job: <method>"`` prefix on bg jobs. Rewrite
	# at render time so the Server Resource per-action timeline reads
	# ``RQ Job: …`` consistent with the rest of the report.
	_normalised_timeline = []
	for _row in (infra_timeline or []):
		_lab = (_row or {}).get("action_label") or ""
		if _lab.startswith("Job: "):
			_row = dict(_row)
			_row["action_label"] = "RQ " + _lab
		_normalised_timeline.append(_row)
	return {
		"cards": cards,
		"summary": infra_summary or {},
		"timeline": _normalised_timeline,
	}


def _build_frontend(ctx) -> dict | None:
	"""Contract ``frontend`` = {kpis, xhrs, web_vitals}.

	Bundles the four flat ``frontend_*`` keys in our existing context.
	"""
	vitals_by_page = ctx.get("frontend_vitals_by_page") or {}
	xhrs = ctx.get("frontend_xhr_matched") or []
	summary = ctx.get("frontend_summary") or {}

	if not vitals_by_page and not xhrs and not summary:
		return None

	# web_vitals — per-page row with computed *_class fields
	web_vitals = []
	for page, v in vitals_by_page.items():
		fcp = v.get("fcp_ms")
		lcp = v.get("lcp_ms")
		cls = v.get("cls")
		ttfb = v.get("ttfb_ms")
		dcl = v.get("dom_content_loaded_ms")
		web_vitals.append({
			"url": page,
			"fcp_display": f"{fcp:.0f} ms" if fcp else "—",
			"fcp_class": _web_vital_class(fcp, 1800, 3000),
			"lcp_display": f"{lcp:.0f} ms" if lcp else "—",
			"lcp_class": _web_vital_class(lcp, 2500, 4000),
			"cls_display": f"{cls:.3f}" if cls is not None else "—",
			"cls_class": _web_vital_class(cls, 0.1, 0.25) if cls is not None else "vital-none",
			"ttfb_display": f"{ttfb:.0f} ms" if ttfb else "—",
			"ttfb_class": _web_vital_class(ttfb, 800, 1800),
			"dcl_display": f"{dcl:.0f} ms" if dcl else "—",
			"dcl_class": _web_vital_class(dcl, 1500, 3000),
		})

	# xhrs — display-formatted
	xhrs_out = []
	for x in xhrs:
		backend_ms = x.get("backend_ms", 0) or 0
		xhr_ms = x.get("xhr_ms", 0) or 0
		network_ms = x.get("network_delta_ms", 0) or 0
		size_bytes = x.get("response_size_bytes", 0) or 0
		xhrs_out.append({
			"name": x.get("action_label", "") or "",
			"meta": x.get("url", "") or "",
			"backend_display": _ms_display(backend_ms),
			"browser_display": _ms_display(xhr_ms),
			"network_display": f"{network_ms:.0f} ms",
			"status": x.get("status", 0) or 0,
			"size_display": (
				f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
			),
			"backend_is_hot": backend_ms >= 1000,
			"browser_is_hot": xhr_ms >= 1000,
		})

	# kpis — 4-item summary
	kpis = []
	if summary:
		total_xhrs = summary.get("total_xhrs", 0) or 0
		total_xhr_ms = summary.get("total_xhr_ms", 0) or 0
		total_backend_ms = summary.get("total_backend_ms", 0) or 0
		net_overhead = summary.get("network_overhead_ms", 0) or 0
		slowest = summary.get("slowest_xhr") or {}
		slowest_sub = (
			f"slowest {slowest.get('duration_ms', 0) or 0:.0f} ms" if slowest else ""
		)
		kpis = [
			{
				"label": "XHRs",
				"value": str(total_xhrs),
				"unit": "",
				"sub_html": "",
				"value_kind": "normal",
				"sub_is_warn": False,
			},
			{
				"label": "XHR total",
				"value": f"{total_xhr_ms:.0f}",
				"unit": "ms",
				"sub_html": "",
				"value_kind": "normal",
				"sub_is_warn": False,
			},
			{
				"label": "Backend total",
				"value": f"{total_backend_ms:.0f}",
				"unit": "ms",
				"sub_html": "",
				"value_kind": "normal",
				"sub_is_warn": False,
			},
			{
				"label": "Network overhead",
				"value": f"{net_overhead:.0f}",
				"unit": "ms",
				"sub_html": slowest_sub,
				"value_kind": "warn" if net_overhead > 500 else "normal",
				"sub_is_warn": False,
			},
		]

	# J.2.4 non-contract additions: pass-through the raw summary +
	# xhr_matched + orphans so the existing rc-card markup, XHR-table
	# columns, and orphans details-block migrate as a straight key rename.
	return {
		"kpis": kpis,
		"xhrs": xhrs_out,
		"web_vitals": web_vitals,
		"summary": summary or {},
		"xhr_matched": xhrs or [],
		"orphans": ctx.get("frontend_orphans") or [],
	}


def _build_hot_frames(hot_frames_rows, ignored_apps, fmt_ms=None) -> list[dict]:
	"""Contract ``hot_frames`` = list of {name, total_time_display,
	is_hot_time, occurrences, distinct_actions, is_user_code}.

	J.2.5: spreads the original row dict so the existing ``hot_frame_row``
	macro keeps reading ``display_name`` / ``total_ms`` / ``is_hot``.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	ignored = tuple(ignored_apps or ())
	result = []
	for row in hot_frames_rows or []:
		name = row.get("display_name") or row.get("function", "") or ""
		total_ms = row.get("total_ms", 0) or 0
		path_part = name.split("::", 1)[0] if "::" in name else name
		entry = dict(row)  # preserve original keys
		entry.update({
			"name": name,
			"total_time_display": fmt(total_ms),
			"is_hot_time": bool(row.get("is_hot", False)) or total_ms >= 1000,
			"occurrences": row.get("occurrences", 0) or 0,
			"distinct_actions": row.get("distinct_actions", 0) or 0,
			"is_user_code": _is_user_code(path_part, ignored),
		})
		result.append(entry)
	return result


def _build_slow_queries(top_queries, fmt_ms=None) -> list[dict]:
	"""Contract ``slow_queries`` = list of {sql_excerpt, total_time_display,
	call_count, avg_display, callsite}.

	J.2.5: spreads the original query dict so ``top_query_row`` keeps
	reading ``duration_ms`` / ``callsite`` / ``normalized_query``. Empty
	list → contract template renders the empty-state card.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	result = []
	for q in top_queries or []:
		# v0.7.x M5 rename: accept the new ``query_duration_ms`` and the
		# legacy ``duration_ms`` / ``total_ms`` so pre-rename sessions
		# still render correctly through regenerate_reports.
		total_ms = (
			q.get("query_duration_ms")
			or q.get("duration_ms")
			or q.get("total_ms", 0)
			or 0
		)
		count = q.get("count") or q.get("call_count", 0) or 0
		avg = (total_ms / count) if count else 0
		entry = dict(q)
		entry.update({
			"sql_excerpt": q.get("normalized_query") or q.get("sql", "") or "",
			"total_time_display": fmt(total_ms),
			"call_count": count,
			"avg_display": f"{avg:.2f} ms",
			"callsite": q.get("callsite") or "",
		})
		result.append(entry)
	return result


def _build_db(table_breakdown, fmt_ms=None) -> dict | None:
	"""Contract ``db`` = {tables, index_recommendations}.

	``tables`` per-entry {name, time_display, queries, reads, writes,
	is_hot, note_html}; ``index_recommendations`` per-entry {table_name,
	stats, recommendation_html, sql, has_writes, verdict_text}.

	J.2.5: spreads each table dict so the existing index-recommendation
	block (which reads ``read_time_ms``, ``write_time_ms``,
	``index_candidates``, ``is_meta_table``, ``framework_cols_filtered``,
	``ai_index``, ``recommended_index``) keeps working without going
	back to ``table_breakdown``.
	"""
	fmt = fmt_ms or (lambda v, **kw: _ms_display(v))
	if not table_breakdown:
		return None

	tables = []
	index_recs = []
	for t in table_breakdown:
		is_hot = bool(t.get("is_write_hot", False)) or (t.get("queries", 0) or 0) >= 100
		# v0.7.x C.AM2: prefer the new ``consolidated_time_ms`` key; fall
		# back to legacy ``duration_ms`` for pre-rename sessions.
		_t_ms = t.get("consolidated_time_ms")
		if _t_ms is None:
			_t_ms = t.get("duration_ms", 0) or 0
		entry = dict(t)  # preserve original keys
		# Expose the canonical name to the template so it can stop reading
		# either of the legacy keys.
		entry["consolidated_time_ms"] = _t_ms
		entry.update({
			"name": t.get("table", "") or "",
			"time_display": fmt(_t_ms),
			"queries": t.get("queries", 0) or 0,
			"reads": t.get("read_count", 0) or 0,
			"writes": t.get("write_count", 0) or 0,
			"is_hot": is_hot,
			"note_html": None,
		})
		tables.append(entry)

		rec = t.get("recommended_index")
		if rec and isinstance(rec, dict) and rec.get("columns"):
			cols = rec.get("columns", []) or []
			cols_display = ", ".join(cols)
			doctype = rec.get("doctype") or t.get("table", "").replace("tab", "")
			cols_sql = ", ".join(repr(c) for c in cols)
			stats = (
				f"{t.get('read_count', 0) or 0} reads · "
				f"{t.get('write_count', 0) or 0} writes · "
				f"{_t_ms:.0f} ms"
			)
			index_recs.append({
				"table_name": t.get("table", "") or "",
				"stats": stats,
				"recommendation_html": f"Most-used filter: <code>({cols_display})</code>",
				"sql": f'frappe.db.add_index("{doctype}", [{cols_sql}])',
				"has_writes": (t.get("write_count", 0) or 0) > 0,
				"verdict_text": None,
			})

	return {"tables": tables, "index_recommendations": index_recs}


def _build_footer(render_config) -> dict:
	"""Contract ``footer`` = {framework, settings}.

	``settings`` lists every render-time toggle that affects the report. The
	contract example shows 3 toggles; our config has 6 — we include all of
	them so the legacy footer's full disclosure carries over post-migration.
	"""
	rc = render_config or {}
	tracked = rc.get("tracked_apps") or ()
	ignored = rc.get("ignored_apps") or ()
	parts = [
		# v0.7.x: which Sensitivity Profile drove the thresholds for this render.
		f"config_profile={rc.get('config_profile') or 'Custom'}",
		f"hide_framework_tables={'on' if rc.get('hide_framework_tables') else 'off'}",
		f"tracked_apps={', '.join(tracked) if tracked else '(none)'}",
		f"ignored_apps={', '.join(ignored) if ignored else '(none)'}",
		f"ai_suggest_findings={'on' if rc.get('ai_suggest_findings') else 'off'}",
		f"ai_suggest_indexes={'on' if rc.get('ai_suggest_indexes') else 'off'}",
		f"min_action_duration_ms={int(rc.get('min_action_duration_ms') or 0)}",
		f"large_duration_threshold_ms={int(rc.get('large_duration_threshold_ms') or 0)}",
	]
	return {
		"framework": "Frappe v16",
		"settings": " · ".join(parts),
	}


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


def build_report_context(session_doc: Any, ctx: dict) -> dict:
	"""Return the contract-shaped context dict per template_variable_contract.md.

	Reads from ``ctx`` (the already-built render context dict) so the
	adapter is a pure transformation and does not duplicate analysis work.
	``session_doc`` is the OptimusSession document used for fields not
	carried in ``ctx`` (phase-2 child table, user, etc.).

	Phase J.1: this function's output is exposed under a ``report_data``
	namespace key in the render context so template references can migrate
	section-by-section in J.2 without breaking. J.3 will unpack the
	namespace and drop legacy keys.
	"""
	# v0.7.x B.DI1: surface the sampler interval onto report_data so
	# every disclosure (KPI caveat, "How to read", finding desc) reads
	# the same number. Defaults to 1.0ms when settings aren't reachable
	# (pure-test path).
	try:
		from optimus.settings import get_config
		_sampler_ms = float(get_config().pyinstrument_sampler_interval_ms or 1.0)
	except Exception:
		_sampler_ms = 1.0
	# AI token-usage transparency: total tokens every AI feature consumed this
	# session — fix suggestions (per finding), index suggestions (per table),
	# and the Steps-to-Reproduce humanization (one per session). Derived at
	# render time so re-generated reports stay in sync — never baked at
	# analyze time. (The connectivity probe is a Settings test, not session
	# work, so it's deliberately excluded.)
	_ai_tokens_total = 0

	def _add_tokens(_obj):
		nonlocal _ai_tokens_total
		_tok = _obj.get("tokens") if isinstance(_obj, dict) else None
		if isinstance(_tok, dict):
			_ai_tokens_total += int(_tok.get("total_tokens") or 0)

	for _bucket in ctx.get("findings_by_app", []) or []:
		for _f in (_bucket.get("findings") if isinstance(_bucket, dict) else []) or []:
			_add_tokens(_f.get("llm_fix") if isinstance(_f, dict) else None)
	for _t in ctx.get("table_breakdown", []) or []:
		_add_tokens(_t.get("ai_index") if isinstance(_t, dict) else None)
	_steps_tokens = int(getattr(session_doc, "ai_steps_tokens", 0) or 0)
	_ai_tokens_total += _steps_tokens
	return {
		"session": _build_session(session_doc, ctx),
		"tldr": _build_tldr(ctx.get("tldr")),
		"kpis": _build_kpis(session_doc, ctx),
		"sampler_interval_ms": _sampler_ms,
		"ai_tokens_total": _ai_tokens_total,
		"ai_steps_tokens": _steps_tokens,
		"repro": _build_repro(ctx.get("notes_html")),
		"summary": _build_summary(ctx.get("summary_html")),
		"findings": _build_findings(ctx.get("findings", []), ctx),
		# J.2.7 pragmatic additions: the Findings section iterates app-
		# bucketed views and feeds the legacy ``finding_card`` macro which
		# reads OLD-shape finding dicts (severity="High" Title-case,
		# untransformed technical_detail, etc.). Pass these through verbatim
		# so the macros keep working; the contract ``findings`` list above
		# uses the new shape and is available for a future macro rewrite.
		"findings_by_app": ctx.get("findings_by_app") or [],
		"observational_findings_by_app": ctx.get("observational_findings_by_app") or [],
		"line_drilldown_runs": _build_line_drilldown_runs(session_doc),
		# J.2.6 pragmatic addition (renamed in J.16): the server-side
		# _render_line_drilldown_panel produces ~100 lines of editorial
		# markup including the cross-run diff that the structured
		# ``line_drilldown_runs`` list above doesn't yet expose. The
		# template renders this directly; a future iteration can replace
		# it by iterating ``line_drilldown_runs`` and a structured diff
		# representation.
		"line_drilldown_html": ctx.get("line_drilldown_html") or "",
		"action_plan": _build_action_plan(ctx.get("action_plan", []), ctx.get("fmt_ms")),
		"waterfall": _build_waterfall(ctx.get("waterfall_rows", []), ctx.get("fmt_ms")),
		"actions": _build_actions(
			ctx.get("actions", []), ctx.get("findings", []), ctx.get("fmt_ms")
		),
		# J.2.3 non-contract addition — pragmatic extension so the framework
		# sub-block keeps rendering. May be folded into ``actions`` with an
		# ``is_framework`` flag in J.3.
		"actions_framework": _build_actions(
			ctx.get("actions_framework", []), ctx.get("findings", []), ctx.get("fmt_ms")
		),
		"background_jobs": _build_background_jobs(
			(ctx.get("background_jobs") or {}).get("jobs", []) or [], ctx.get("fmt_ms")
		),
		# J.2.3 non-contract addition — see actions_framework note above.
		"background_jobs_framework": _build_background_jobs(
			(ctx.get("background_jobs") or {}).get("jobs_framework", []) or [], ctx.get("fmt_ms")
		),
		"doc_events": _build_doc_events(ctx.get("doc_event_breakdown") or {}, ctx.get("fmt_ms")),
		"resource": _build_resource(
			ctx.get("infra_summary") or {}, ctx.get("infra_timeline") or []
		),
		"frontend": _build_frontend(ctx),
		"hot_frames": _build_hot_frames(
			ctx.get("hot_frames_rows", []), ctx.get("ignored_apps") or (), ctx.get("fmt_ms")
		),
		# J.2.5 non-contract addition for framework split.
		"hot_frames_framework": _build_hot_frames(
			ctx.get("hot_frames_rows_framework", []), ctx.get("ignored_apps") or (), ctx.get("fmt_ms")
		),
		"slow_queries": _build_slow_queries(ctx.get("top_queries", []), ctx.get("fmt_ms")),
		# J.2.5 non-contract addition for framework split.
		"slow_queries_framework": _build_slow_queries(ctx.get("top_queries_framework", []), ctx.get("fmt_ms")),
		# B.DI4 — how many user-app slow queries the analyzer truncated
		# out of the findings list (5-cap). 0 when nothing was suppressed.
		"top_queries_suppressed_count": int(ctx.get("top_queries_suppressed_count") or 0),
		"top_queries_slow_threshold_ms": float(ctx.get("top_queries_slow_threshold_ms") or 0),
		# B.DI2 — Hot Frames section truncation banner data:
		# {captured, kept, actions_affected, keep_limit}. actions_affected=0 hides it.
		"frame_truncation": ctx.get("frame_truncation") or {
			"captured": 0, "kept": 0, "actions_affected": 0, "keep_limit": 0,
		},
		"db": _build_db(ctx.get("table_breakdown", []), ctx.get("fmt_ms")),
		"how_to_read_items": None,
		"footer": _build_footer(ctx.get("render_config")),
		# J.3.1 non-contract additions — the remaining two passthroughs
		# the template needs before the legacy top-level keys can be
		# dropped from renderer.py's context dict.
		"large_duration_threshold_ms": (
			(ctx.get("render_config") or {}).get("large_duration_threshold_ms") or 1000
		),
		"background_jobs_summary": _build_background_jobs_summary(ctx.get("background_jobs") or {}),
	}


def _build_background_jobs_summary(background_jobs) -> dict:
	"""Aggregate fields the BG-jobs section heading + bg_job_row macro need.

	The legacy ``background_jobs`` dict carries ``count`` / ``total_ms`` /
	``total_queries`` / ``any_findings_counted`` / ``framework_count``
	alongside the ``jobs`` / ``jobs_framework`` lists. After J.3 these
	aggregates are the only legacy-dict-shaped data the template still
	reads; expose them under a dedicated namespace so the rest of the
	dict can be dropped.
	"""
	bj = background_jobs or {}
	# v0.7.x: per-status tally so the section heading can break down "N jobs —
	# X completed, Y failed, Z timed out, W running" and flag failures. Ordered
	# worst-first; zero categories are dropped so the line stays terse.
	sc = bj.get("status_counts") or {}
	_order = [
		("Failed", "failed"), ("Timeout", "timed out"), ("Stopped", "stopped"),
		("Running", "running"), ("Queued", "queued"), ("Started", "started"),
		("Completed", "completed"),
	]
	status_breakdown = [
		{"status": key, "label": label, "count": sc[key]}
		for key, label in _order if sc.get(key)
	]
	return {
		"count": bj.get("count", 0) or 0,
		"total_ms": bj.get("total_ms", 0) or 0,
		"total_queries": bj.get("total_queries", 0) or 0,
		"any_findings_counted": bool(bj.get("any_findings_counted", False)),
		"framework_count": bj.get("framework_count", 0) or 0,
		"status_counts": sc,
		"status_breakdown": status_breakdown,
		"has_failures": bool(sc.get("Failed") or sc.get("Timeout")),
	}
