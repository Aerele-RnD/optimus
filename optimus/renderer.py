# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""HTML report renderer for a Optimus Session.

Renders a single admin-scoped report: full data including raw SQL with
literal values, request headers, form data, and complete stack traces.
Gated to System Manager + the recording user via Frappe's File
permission hook (see permissions.py:file_has_permission).

The template is loaded directly from the file system (not via Frappe's
Jinja environment) so the renderer is unit-testable in isolation and
doesn't depend on a running site.
"""

import functools
import json
import os
import re
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from optimus.analyzers.base import SEVERITY_ORDER

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


# v0.6.0 Round 7: safe-mode redaction removed. _safe_url, redact_sensitive,
# _SENSITIVE_FIELD_PATTERNS, and the URL-docname/QS denylist all lived
# here. The product now ships one admin-scoped report, so the
# defense-in-depth redaction layer is gone. See product_thesis_self_hosted.md
# memory for the rationale.


@functools.lru_cache(maxsize=1)
def _get_jinja_env() -> Environment:
	"""Build and cache the Jinja environment.

	Autoescape is on for HTML so user-provided strings (action labels,
	finding titles, etc.) can never inject markup into the report.
	"""
	return Environment(
		loader=FileSystemLoader(_TEMPLATES_DIR),
		autoescape=select_autoescape(["html"]),
		trim_blocks=True,
		lstrip_blocks=True,
	)


def render(
	session_doc: Any,
	recordings: list[dict] | None = None,
	*,
	generated_at: str | None = None,
) -> str:
	"""Render a Optimus Session to standalone HTML.

	v0.6.0 Round 7: collapsed from a two-mode (safe/raw) renderer to a
	single admin-scoped report. Permission gating is the responsibility
	of the caller (download_pdf, file permission hooks).

	Args:
	    session_doc: The Optimus Session DocType row (loaded via
	        frappe.get_doc). Provides totals, summary_html, and the
	        actions/findings child rows.
	    recordings: The in-memory recordings list. Required — provides
	        raw SQL, headers, form_dict, and full stack traces for the
	        per-action drill-down.
	    generated_at: ISO timestamp of when this report was generated;
	        defaults to now() if not provided.

	Returns:
	    Standalone HTML as a string. Inline CSS, no external assets, no
	    JavaScript. Self-contained for emailing or attaching to a ticket.
	"""
	if recordings is None:
		raise ValueError("recordings list is required")

	template = _get_jinja_env().get_template("report.html")

	# Build the per-action drill-down structure. We pair each Profiler
	# Action child row with its source recording so the template can show
	# full SQL / headers / form_dict for that action.
	recordings_by_uuid: dict[str, dict] = {}
	if recordings:
		for r in recordings:
			uid = r.get("uuid")
			if not uid:
				continue
			recordings_by_uuid[uid] = dict(r)

	# `idx` = the action's original position — matches a finding's `action_ref`
	# (which is the action index as a string) so the Background-jobs section
	# can tally findings per job even after the min-duration filter below.
	actions = [
		dict(_action_to_dict(a), idx=i) for i, a in enumerate(session_doc.actions or [])
	]
	# v0.6.0 Round 6: Optimus Settings ▸ Min Action Duration declutters
	# the per-action breakdown by hiding sub-threshold actions from the
	# table only. The DocType row is still persisted (queryable via
	# API), so admins can lift the threshold without losing data.
	try:
		from optimus.settings import get_config as _get_config
		_min_action_ms = float(_get_config().min_action_duration_ms or 0)
	except Exception:
		_min_action_ms = 0.0
	if _min_action_ms > 0:
		actions = [
			a for a in actions
			if (a.get("duration_ms") or 0) >= _min_action_ms
		]
	# v0.6.x: per-action entry-point source location + ±1-line snippet,
	# shown under each row in the per-action breakdown and Background Jobs
	# sections. One shared file-line cache so a cluster of actions in the
	# same source file reads it once. Done before build_background_jobs so
	# the job dicts can copy the resolved callsite off the action dict.
	_entry_src_cache: dict[str, list[str] | None] = {}
	for _a in actions:
		_a["entry_callsite"] = _action_entry_callsite(_a, cache=_entry_src_cache)
	# v0.6.0 Round 2: per-render file cache for the lazy snippet read in
	# _finding_to_dict. A session with a dozen findings clustered in 2-3
	# source files reads each file once instead of once per finding.
	_finding_file_cache: dict[str, list[str] | None] = {}
	all_findings = [
		_finding_to_dict(f, _finding_file_cache)
		for f in (session_doc.findings or [])
	]
	# v0.6.x: SQL "red flag" findings carry no callsite — derive a
	# representative one (the hottest user-app frame that ran the offending
	# query) from the recordings so their smoking-gun block can render too.
	_attach_representative_callsites(all_findings, recordings or [], file_cache=_finding_file_cache)
	# v0.6.x: which document each save/submit action touched, and which
	# doc-event lifecycle hook each slow function fired in.
	_attach_action_context(actions, all_findings, recordings_by_uuid)
	# v0.6.x: "Ignored Apps" — drop findings whose blame app is in the
	# admin's exclusion list, BEFORE anything downstream sees the list
	# (doc-event breakdown, background-jobs tally, exec summary, severity
	# counts, the actionable/observational split, bucketing). The "Issues
	# found" stat card then shows the kept count + a small "(N hidden)"
	# note from the context vars below.
	try:
		from optimus.settings import get_ignored_apps as _get_ignored
		ignored_apps = tuple(sorted({a for a in (_get_ignored() or ()) if a}))
	except Exception:
		ignored_apps = ()
	ignored_findings_count = 0
	if ignored_apps:
		_ignored_set = set(ignored_apps)
		_kept = []
		for _f in all_findings:
			if _app_from_finding(_f) in _ignored_set:
				ignored_findings_count += 1
				continue
			_kept.append(_f)
		all_findings = _kept

	# v0.6.x: re-group the slow lifecycle findings by DocType → event for the
	# "Doc-event lifecycle" section (consumes the hook_events/target_doc that
	# _attach_action_context just attached).
	doc_event_breakdown = _build_doc_event_breakdown(all_findings)

	# v0.6.0: the "Background jobs" section — the captured background-job
	# recordings, surfaced on their own (they also stay in the per-action
	# table). Derived from the persisted action rows; uses all findings
	# (actionable + observational) for the per-job findings tally.
	background_jobs = build_background_jobs(actions, recordings_by_uuid, all_findings)

	try:
		top_queries = json.loads(session_doc.top_queries_json or "[]")
	except Exception:
		top_queries = []
	# The slowest-queries leaderboard is user-app-only and skips
	# trivially-fast queries. New sessions are already filtered by the
	# top_queries analyzer; re-applying it here means sessions captured
	# before that change also get scoped down when regenerated.
	top_queries = _filter_top_queries_for_display(top_queries)
	try:
		table_breakdown = json.loads(session_doc.table_breakdown_json or "[]")
	except Exception:
		table_breakdown = []
	# v0.6.0: an LLM-vetted index recommendation may be stashed on a table
	# entry (by analyze.py's auto step or the "Suggest an index (AI)" button)
	# as ``ai_index = {"suggestion": <markdown>, "model": ..., ...}``. Render
	# the markdown → sanitized HTML here so the template can `| safe` it
	# (same path as the finding AI-fix blocks).
	for _t in table_breakdown:
		if isinstance(_t, dict) and isinstance(_t.get("ai_index"), dict):
			raw = (_t["ai_index"].get("suggestion") or "").strip()
			if raw:
				_t["ai_index"]["suggestion_html"] = _markdown_to_safe_html(raw)

	# v0.6.x: a per-section LLM toggle being off is a hard disable — drop any
	# previously-generated AI output for that section so re-rendering an older
	# session (analyzed while it was on) doesn't show the block. (Humanized
	# notes live in Optimus Session.notes — a plain HTML field — so they're
	# not stripped here; turning that section off stops new generation, but an
	# already-humanized note stays until the session is re-analyzed.)
	try:
		from optimus.settings import get_config as _get_cfg
		_cfg = _get_cfg()
		_ai_findings_on = getattr(_cfg, "ai_suggest_findings", True)
		_ai_indexes_on = getattr(_cfg, "ai_suggest_indexes", True)
		_hide_framework_tables = getattr(_cfg, "hide_framework_tables", True)
		# v0.6.x: snapshot the render-affecting settings so the footer can
		# stamp THIS file with the values that were in effect. Saved HTML
		# only re-renders on Regenerate Reports / Retry Analyze — the stamp
		# means a user opening an old file can immediately tell whether the
		# settings they expect are actually baked in.
		_large_duration_threshold_ms = float(
			getattr(_cfg, "large_duration_threshold_ms", 1000.0) or 0.0
		)
		render_config = {
			"hide_framework_tables": _hide_framework_tables,
			"tracked_apps": tuple(getattr(_cfg, "tracked_apps", ()) or ()),
			"ignored_apps": tuple(getattr(_cfg, "ignored_apps", ()) or ()),
			"ai_suggest_findings": _ai_findings_on,
			"ai_suggest_indexes": _ai_indexes_on,
			"min_action_duration_ms": float(
				getattr(_cfg, "min_action_duration_ms", 0.0) or 0.0
			),
			"large_duration_threshold_ms": _large_duration_threshold_ms,
		}
	except Exception:
		_ai_findings_on = _ai_indexes_on = True
		_hide_framework_tables = True
		_large_duration_threshold_ms = 1000.0
		render_config = {
			"hide_framework_tables": True,
			"tracked_apps": (),
			"ignored_apps": (),
			"ai_suggest_findings": True,
			"ai_suggest_indexes": True,
			"min_action_duration_ms": 0.0,
			"large_duration_threshold_ms": 1000.0,
		}
	# v0.6.x: Jinja-callable that formats a duration with the configured
	# threshold. Closures over the resolved threshold so templates can just
	# write {{ fmt_ms(action.duration_ms) }} (no threshold arg needed).
	def _fmt_ms(v, decimals: int = 0) -> str:
		return _format_duration_ms(v, _large_duration_threshold_ms, decimals)
	if not _ai_findings_on:
		for _f in all_findings:
			_f["llm_fix"] = None
	if not _ai_indexes_on:
		for _t in table_breakdown:
			if isinstance(_t, dict):
				_t.pop("ai_index", None)

	# v0.6.x: drop framework/internal db tables from the "Time spent per
	# database table" section — schema/meta (DocType/DocField/…), user-
	# session bookkeeping (User/Has Role/DefaultValue/…), and information_
	# schema.*. The note under the section's intro reports the count so the
	# total stays honest. Scope is intentional: top-queries leaderboard /
	# per-action drill-down / full recordings keep their raw data.
	hidden_db_tables_count = 0
	if _hide_framework_tables:
		from optimus.analyzers.base import is_framework_db_table
		_kept_tb = []
		for _t in table_breakdown:
			if isinstance(_t, dict) and is_framework_db_table(_t.get("table")):
				hidden_db_tables_count += 1
				continue
			_kept_tb.append(_t)
		table_breakdown = _kept_tb

	# Sort all findings: highest severity first, then highest impact.
	all_findings.sort(
		key=lambda f: (
			SEVERITY_ORDER.get(f["severity"], 3),
			-(f["estimated_impact_ms"] or 0),
		)
	)

	# v0.5.2: split findings into two buckets per user feedback
	# ("In Findings — what to fix, Show only the valid fixes").
	#
	# ACTIONABLE: findings with a concrete fix the user can ship —
	# add an index, refactor a loop, trim a response. These go into
	# the main "Findings — what to fix" section so the list reads
	# as a punchlist.
	#
	# OBSERVATIONS: informational findings that surface signal but
	# don't prescribe a fix the user can act on (framework N+1 where
	# the loop lives inside Frappe, system-level CPU/memory/queue
	# pressure, repeated hot frames that need further investigation).
	# These go into a separate "Observations" section — still
	# visible for users who want the full picture, but no longer
	# cluttering the action list.
	actionable_findings = [
		f for f in all_findings if f["finding_type"] in _ACTIONABLE_FINDING_TYPES
	]
	observational_findings = [
		f for f in all_findings if f["finding_type"] not in _ACTIONABLE_FINDING_TYPES
	]
	# Back-compat: some template paths still reference `findings`.
	# Point it at the actionable list so the main section shows
	# only the punchlist. Observations are exposed separately.
	findings = actionable_findings

	# v0.3.0: load donut + hot frames data from the new fields. Each
	# helper degrades to empty/None if the field is missing (old session).
	try:
		_breakdown = json.loads(getattr(session_doc, "session_time_breakdown_json", None) or "{}")
	except Exception:
		_breakdown = {}
	try:
		_hot_frames_raw = json.loads(getattr(session_doc, "hot_frames_json", None) or "[]")
	except Exception:
		_hot_frames_raw = []

	donut_slices = build_donut_data(_breakdown)
	donut_svg = build_donut_svg(donut_slices)  # v0.4.0: PDF fallback
	# (hot_frames_rows is built below after tracked_apps is read so the
	# raw rows can be split by framework-app first.)

	def _redact_for_template(node):
		return redact_frame_name(node)

	def _from_json(s):
		try:
			return json.loads(s) if s else {}
		except Exception:
			return {}

	# v0.5.0: infra_pressure + frontend_timings aggregates. One JSON field
	# holds both. Empty fallbacks let sessions captured before v0.5.0
	# render cleanly after the upgrade — the new panels just don't appear.
	try:
		v5 = json.loads(getattr(session_doc, "v5_aggregate_json", None) or "{}")
	except Exception:
		v5 = {}

	# v0.5.0: pre-sanitize session.notes before the template uses |safe.
	# The field was upgraded from plain Text to Text Editor in v0.5.0,
	# which means `{{ session.notes | safe }}` would render stored HTML
	# verbatim — a stored-XSS sink if any existing row has script content
	# (plain-text before, live HTML after).
	#
	# CRITICAL: pass always_sanitize=True. Without it, Frappe's
	# sanitize_html has TWO fast-paths that skip bleach:
	#   1. if is_json(html) → returns unchanged  (bypassable with
	#      notes = '{"x":"<script>alert(1)</script>"}' — valid JSON
	#      containing a script tag)
	#   2. if BeautifulSoup.find() returns nothing → returns unchanged
	# Both paths would leak raw input to |safe in the template.
	# always_sanitize=True forces nh3/bleach to run on every input.
	notes_html = getattr(session_doc, "notes", None) or ""
	if notes_html:
		try:
			from frappe.utils.html_utils import sanitize_html
			notes_html = sanitize_html(notes_html, always_sanitize=True)
		except Exception:
			# If sanitize_html blows up for any reason (unexpected input
			# type, nh3/bleach internal error), fall back to HTML-escaping
			# via html.escape so the report NEVER renders unsanitized
			# user input — safe by default.
			import html as html_mod
			notes_html = html_mod.escape(notes_html)

	# v0.5.2: Analyzer warnings are stored as a newline-joined string
	# (see analyze.py). Split into a list of non-empty bullets for the
	# collapsible "Analyzer notes" section at the bottom of the report
	# so they render as a clean <ul> instead of a wall of text.
	warnings_raw = getattr(session_doc, "analyzer_warnings", None) or ""
	analyzer_warnings = [
		line.strip()
		for line in warnings_raw.split("\n")
		if line.strip()
	]

	# v0.5.3: If any warning starts with the TRUNCATED marker, surface
	# it in its own prominent banner at the top of the report rather
	# than burying it in the collapsed Analyzer Notes section. Users
	# read an 8s Submit report without noticing the "566 queries were
	# truncated" warning because it sat below the fold — then debugged
	# based on an incomplete picture. The banner forces the visibility
	# that the severity of the situation deserves.
	truncation_banner = None
	for w in analyzer_warnings:
		if w.startswith("⚠ TRUNCATED:"):
			truncation_banner = w
			break

	# v0.5.2: sub-group findings by top-level app so the report reads
	# "myapp (3 findings, ~420ms)" → 3 cards, instead of a flat list
	# mixing myapp + erpnext + frappe callsites. Tracked-apps order
	# wins (user's mental model: my apps first), then remaining apps
	# by total impact, with "Other (no callsite)" always tail.
	try:
		from optimus.settings import get_tracked_apps
		tracked_apps = get_tracked_apps()
	except Exception:
		tracked_apps = ()
	# v0.6.x: attach a call-tree drill-down chain to each finding that has a
	# callsite + an action_ref. Walks the action's pyinstrument tree from the
	# finding's origin function down to the deepest user-code frame. Lets
	# non-LLM users see the same actionable chain the AI narrative produces.
	_attach_drilldown_chains(all_findings, actions, tracked_apps=tracked_apps)
	findings_by_app = _bucket_findings_by_app(findings, tracked_apps)
	observational_findings_by_app = _bucket_findings_by_app(
		observational_findings, tracked_apps
	)

	# v0.6.x: prioritise custom-app rows in each of the 4 main listing
	# sections (per-action, top-queries, background-jobs, hot-frames).
	# Each list is split into a custom-app primary + a framework-app
	# secondary; the template renders the primary in the main <table>
	# and the framework list inside a collapsed <details class="subsection">.
	# Sort order WITHIN each bucket is preserved (existing duration sort).
	actions, actions_framework = _split_by_framework_app(
		actions,
		lambda a: (a.get("entry_callsite") or {}).get("_abs") or _action_dotted_entry(a),
		tracked_apps,
	)
	top_queries, top_queries_framework = _split_by_framework_app(
		top_queries,
		lambda q: q.get("callsite"),
		tracked_apps,
	)
	_bg_jobs_custom, _bg_jobs_framework = _split_by_framework_app(
		background_jobs.get("jobs") or [],
		lambda j: (j.get("entry_callsite") or {}).get("_abs")
		or (j.get("method") or "").split(".", 1)[0],
		tracked_apps,
	)
	background_jobs["jobs"] = _bg_jobs_custom
	background_jobs["jobs_framework"] = _bg_jobs_framework
	background_jobs["framework_count"] = len(_bg_jobs_framework)
	# Hot-frames classification reads the analyzer's `function` key
	# (shape: ``"<short_path>::<func>"`` from _redacted_module_key), which
	# `build_hot_frames_table` strips on the way to `display_name`. So split
	# the raw rows first, then build each table separately.
	_hf_raw_custom, _hf_raw_framework = _split_by_framework_app(
		_hot_frames_raw,
		lambda r: (r.get("function") or "").split("::", 1)[0],
		tracked_apps,
	)
	hot_frames_rows = build_hot_frames_table(_hf_raw_custom)
	hot_frames_rows_framework = build_hot_frames_table(_hf_raw_framework)

	# v0.5.2 round 3: executive summary — top 3 most-impactful findings
	# stated in plain English, rendered in a card at the top of the
	# report. A non-developer (e.g. a project manager) reading this
	# should be able to decide "do we have a problem" in 30 seconds
	# without scrolling past the first screen.
	executive_summary = _build_executive_summary(
		findings=findings,
		session_doc=session_doc,
		v5=v5,
	)

	context = {
		"session": session_doc,
		"actions": actions,
		# v0.6.x: framework-app actions, rendered in a collapsed sub-block
		# below the primary per-action table. Empty → no sub-block.
		"actions_framework": actions_framework,
		# v0.6.0: background jobs the profiled flow enqueued (focused view;
		# they also appear in `actions`). Falsy `.count` → the template omits
		# the section. Note: ``background_jobs.jobs`` now holds only custom-app
		# jobs; framework-app jobs live in ``background_jobs.jobs_framework``.
		"background_jobs": background_jobs,
		# v0.6.x: slow lifecycle findings re-grouped by DocType → event. Falsy
		# `.count` → the template omits the section.
		"doc_event_breakdown": doc_event_breakdown,
		"analyzer_warnings": analyzer_warnings,
		"truncation_banner": truncation_banner,
		"findings_by_app": findings_by_app,
		"observational_findings_by_app": observational_findings_by_app,
		"executive_summary": executive_summary,
		# v0.5.2: "findings" holds actionable items only (shown in
		# "Findings — what to fix"); "observational_findings" the rest.
		# "all_findings" is the full list — the "Issues found" stat card
		# shows that total and a severity breakdown of it, so its big
		# number, its sub-line, and the Summary prose all agree.
		"findings": findings,
		"observational_findings": observational_findings,
		"all_findings": all_findings,
		"top_queries": top_queries,
		# v0.6.x: framework-callsite top queries (typically empty because
		# top_queries is already filtered at analyze time AND render time;
		# the split is wired for consistency with the other 3 sections).
		"top_queries_framework": top_queries_framework,
		"table_breakdown": table_breakdown,
		"recordings_by_uuid": recordings_by_uuid,
		"generated_at": generated_at or _now_iso(),
		"server_tz": _get_server_timezone(),
		# Format datetimes per the site's System Settings (drops microseconds).
		"fmt_dt": _format_datetime_display,
		# v0.6.x: duration formatter that honours large_duration_threshold_ms
		# from Optimus Settings. Above the threshold → "5.23s"; below → "ms"
		# (with caller-chosen decimals to preserve %.1f / %.2f precision).
		"fmt_ms": _fmt_ms,
		# Severity breakdown of ALL findings — feeds the "Issues found" stat
		# card's sub-line (which sums to the card's total).
		"severity_counts": {
			"High": sum(1 for f in all_findings if f["severity"] == "High"),
			"Medium": sum(1 for f in all_findings if f["severity"] == "Medium"),
			"Low": sum(1 for f in all_findings if f["severity"] == "Low"),
		},
		# v0.6.x: the "Ignored Apps" exclusion list, plus how many findings
		# this render dropped — surfaced as a small note next to the stat
		# card so the missing-bucket count is honest. Empty/zero → no note.
		"ignored_apps": ignored_apps,
		"ignored_findings_count": ignored_findings_count,
		# v0.6.x: how many framework/internal db tables the "Time spent per
		# database table" section dropped — surfaced as a small note in that
		# section. Zero → no note.
		"hidden_db_tables_count": hidden_db_tables_count,
		# v0.3.0 additions
		"donut_slices": donut_slices,
		"hot_frames_rows": hot_frames_rows,
		# v0.6.x: framework-app hot frames, rendered in a collapsed sub-block
		# below the primary hot-frames table. Empty → no sub-block.
		"hot_frames_rows_framework": hot_frames_rows_framework,
		"redact_frame_name": _redact_for_template,
		"from_json": _from_json,
		# v0.4.0 additions
		"donut_svg": donut_svg,
		# v0.5.0 additions
		"infra_timeline": v5.get("infra_timeline") or [],
		"infra_summary": v5.get("infra_summary") or {},
		"frontend_xhr_matched": v5.get("frontend_xhr_matched") or [],
		"frontend_vitals_by_page": v5.get("frontend_vitals_by_page") or {},
		"frontend_orphans": v5.get("frontend_orphans") or [],
		"frontend_summary": v5.get("frontend_summary") or {},
		"notes_html": notes_html,  # sanitized, safe to pass through |safe
		# v0.6.0 phase-2 line profiler — pre-rendered HTML so the existing
		# report.html template only needs a single {{ phase2_html | safe }}
		# include instead of growing by 100+ lines of new markup.
		"phase2_html": _render_phase2_panel(session_doc),
		# v0.6.0 finding-card smoking gun: cross-link a finding's callsite
		# to its hottest phase-2 line when the same function was
		# instrumented. Helper rather than raw dict because Jinja can't
		# build tuple keys for the basename + function lookup.
		"phase2_for_callsite": _make_phase2_lookup(
			_build_phase2_callsite_index(session_doc)
		),
		# v0.6.x: snapshot of the render-affecting settings, stamped in the
		# report footer so a user opening a saved HTML file can immediately
		# tell which toggles were in effect when it was rendered. (Saved
		# files are static; Optimus Settings changes only affect future
		# renders.)
		"render_config": render_config,
	}

	return template.render(**context)


def _e(text: object) -> str:
	"""HTML-escape — small alias to keep the phase-2 builder readable."""
	import html as _html
	return _html.escape("" if text is None else str(text))


def _build_phase2_callsite_index(session_doc: Any) -> dict:
	"""Build a (basename, function_name) → hottest-line lookup from the
	session's phase-2 runs. Used by ``finding_card`` to inject a "Phase 2
	hot line: …" callout whenever a finding's callsite resolves to a
	function that was line-profiled.

	Keyed by file basename (not absolute path) so the lookup survives
	dev-vs-deploy path differences. When the same function appears in
	multiple runs, the entry with the largest single-line ``total_ms``
	wins — that's the most informative callout for the developer.

	Returns an empty dict when the session has no phase-2 runs or the
	results blobs are empty / malformed; the macro then renders no
	callout.
	"""
	import os

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
			qualname = fn.get("qualname") or (
				dotted.rsplit(".", 1)[-1] if dotted else ""
			)
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
			key = (os.path.basename(file_path), qualname)
			existing = index.get(key)
			candidate_ms = hot_line.get("total_ms", 0) or 0
			if existing is None or candidate_ms > (existing.get("total_ms") or 0):
				index[key] = {
					"lineno": hot_line.get("lineno"),
					"content": hot_line.get("content") or "",
					"total_ms": candidate_ms,
					"hits": hot_line.get("hits") or 0,
					"run_uuid": run_uuid,
					"dotted_path": dotted,
				}
	return index


def _make_phase2_lookup(index: dict):
	"""Wrap the phase-2 callsite index in a small lookup callable so
	Jinja can call ``phase2_for_callsite(filename, function_name)`` —
	Jinja cannot index dicts by tuple keys directly.
	"""
	import os

	def lookup(filename, function_name):
		if not filename or not function_name:
			return None
		return index.get((os.path.basename(filename), function_name))

	return lookup


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
	rows = fn.get("lines") or []
	dotted = fn.get("dotted_path", "")
	file_path = fn.get("file", "")
	source = fn.get("source") or "curated"

	# Auto-expanded descendants get a chain-indent + arrow so the report
	# reads top-down as "you picked X; we drilled into ↳ Y; ↳ Z; …".
	is_descendant = source == "auto_expand"
	container_margin_left = "24px" if is_descendant else "0"
	header_prefix = (
		'<span style="color: #9ca3af; margin-right: 6px;">↳</span>'
		if is_descendant
		else ""
	)

	html = [
		f'<div class="phase2-function" '
		f'style="margin: 12px 0 12px {container_margin_left}; padding: 8px; '
		f'border: 1px solid #d1d5db; border-radius: 4px;">',
		f'<div style="font-family: ui-monospace, Menlo, monospace; '
		f'font-weight: 600; margin-bottom: 4px;">{header_prefix}{_e(dotted)}</div>',
		f'<div style="color: #6b7280; font-size: 12px; margin-bottom: 8px;">'
		f'{_e(file_path)}</div>',
	]

	if not rows:
		html.append(
			'<div style="font-style: italic; color: #6b7280;">'
			'Function was instrumented but never invoked during phase 2.'
			'</div></div>'
		)
		return "".join(html)

	html.append(
		'<table style="width: 100%; border-collapse: collapse; '
		'font-family: ui-monospace, Menlo, monospace; font-size: 12px;">'
		'<thead style="background: #f9fafb;">'
		'<tr>'
		'<th style="text-align: right; padding: 4px 8px; '
		'border-bottom: 1px solid #e5e7eb;">#</th>'
		'<th style="text-align: right; padding: 4px 8px; '
		'border-bottom: 1px solid #e5e7eb;">hits</th>'
		'<th style="text-align: right; padding: 4px 8px; '
		'border-bottom: 1px solid #e5e7eb;">total ms</th>'
		'<th style="text-align: right; padding: 4px 8px; '
		'border-bottom: 1px solid #e5e7eb;">per hit µs</th>'
		'<th style="text-align: left; padding: 4px 8px; '
		'border-bottom: 1px solid #e5e7eb;">source</th>'
		'</tr></thead><tbody>'
	)

	# Find the hot line so we can highlight it.
	max_ms = max((r.get("total_ms") or 0) for r in rows)

	for line in rows:
		ms = line.get("total_ms") or 0
		# Highlight rows that account for ≥25% of the function's max line ms
		# AND > 0 — gives the eye a heat-map of where time goes.
		highlight = max_ms > 0 and ms / max_ms >= 0.25
		row_style = (
			'background: #fef3c7;' if highlight and ms > 0 else 'background: transparent;'
		)
		source_cell = (
			f'<code style="white-space: pre;">{_e(line.get("content", ""))}</code>'
		)
		html.append(
			f'<tr style="{row_style}">'
			f'<td style="text-align: right; padding: 2px 8px; color: #6b7280;">{line.get("lineno", "")}</td>'
			f'<td style="text-align: right; padding: 2px 8px;">{line.get("hits", 0)}</td>'
			f'<td style="text-align: right; padding: 2px 8px;">{ms:.2f}</td>'
			f'<td style="text-align: right; padding: 2px 8px;">{(line.get("per_hit_us") or 0):.2f}</td>'
			f'<td style="padding: 2px 8px;">{source_cell}</td>'
			'</tr>'
		)

	html.append('</tbody></table></div>')
	return "".join(html)


def _render_phase2_diff_table(diff_rows: list[dict]) -> str:
	"""Render the cross-run delta table for one function profiled in 2+
	runs — the verify-the-fix view.

	v0.6.0 Round 7: source column always shows full code (was previously
	gated by ``mode == "safe"`` + the safe-source toggle).
	"""
	if not diff_rows:
		return ""

	html = [
		'<table style="width: 100%; border-collapse: collapse; '
		'font-family: ui-monospace, Menlo, monospace; font-size: 12px; '
		'margin-top: 8px;">',
		'<thead style="background: #f9fafb;"><tr>'
		'<th style="text-align: left; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">status</th>'
		'<th style="text-align: right; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">prev #</th>'
		'<th style="text-align: right; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">curr #</th>'
		'<th style="text-align: right; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">prev ms</th>'
		'<th style="text-align: right; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">curr ms</th>'
		'<th style="text-align: right; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">Δ ms</th>'
		'<th style="text-align: left; padding: 4px 8px; border-bottom: 1px solid #e5e7eb;">source</th>'
		'</tr></thead><tbody>',
	]

	for row in diff_rows:
		status = row.get("status", "")
		# Status background: green for matched-faster, red for matched-slower,
		# blue for added, gray for removed.
		bg = "transparent"
		delta = row.get("delta_ms")
		if status == "matched" and delta is not None:
			if delta < -0.5:
				bg = "#dcfce7"  # green-50
			elif delta > 0.5:
				bg = "#fee2e2"  # red-50
		elif status == "added":
			bg = "#dbeafe"  # blue-50
		elif status == "removed":
			bg = "#f3f4f6"  # gray-100

		def _fmt(v):
			return "—" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))

		source_cell = (
			f'<code style="white-space: pre;">{_e(row.get("content", ""))}</code>'
		)

		html.append(
			f'<tr style="background: {bg};">'
			f'<td style="padding: 2px 8px;">{_e(status)}</td>'
			f'<td style="text-align: right; padding: 2px 8px; color: #6b7280;">{_fmt(row.get("lineno_old"))}</td>'
			f'<td style="text-align: right; padding: 2px 8px; color: #6b7280;">{_fmt(row.get("lineno_new"))}</td>'
			f'<td style="text-align: right; padding: 2px 8px;">{_fmt(row.get("ms_old"))}</td>'
			f'<td style="text-align: right; padding: 2px 8px;">{_fmt(row.get("ms_new"))}</td>'
			f'<td style="text-align: right; padding: 2px 8px;">{_fmt(delta)}</td>'
			f'<td style="padding: 2px 8px;">{source_cell}</td>'
			'</tr>'
		)
	html.append('</tbody></table>')
	return "".join(html)


def _render_phase2_panel(session_doc: Any) -> str:
	"""Build the phase-2 section HTML. Returns an empty string when the
	session has no phase-2 runs (the template's ``{% if phase2_html %}``
	guard then skips the section entirely).

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
			p.get("dotted_path"): p.get("source", "curated")
			for p in picks
			if p.get("dotted_path")
		}
		annotated_results = []
		for fn in results:
			annotated_results.append({
				**fn,
				"source": picks_source.get(fn.get("dotted_path"), "curated"),
			})
		parsed_runs.append({
			"run_uuid": child.run_uuid,
			"status": child.status,
			"started_at": child.started_at,
			"ended_at": child.ended_at,
			"total_ms": child.total_ms or 0,
			"picks": picks,
			"functions": annotated_results,
		})

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

	html = [
		'<section style="margin-top: 32px; padding: 16px; '
		'border: 1px solid #d1d5db; border-radius: 6px; background: #ffffff;">',
		'<h2 style="margin: 0 0 8px 0;">Phase 2: Line-Level Drilldown</h2>',
		'<div style="background: #fef3c7; border-left: 4px solid #f59e0b; '
		'padding: 8px 12px; margin-bottom: 16px; font-size: 13px;">'
		'Phase 2 captures only the flow you ran during the line-profile '
		'recording. Make sure your phase-2 reproduction exercises the same '
		'code paths as phase 1 — function-not-invoked warnings indicate '
		"otherwise."
		'</div>',
	]

	for run_idx, run in enumerate(parsed_runs, start=1):
		started = _e(run.get("started_at"))
		picks_summary = ", ".join(
			_e(p.get("dotted_path", "?")) for p in run.get("picks", [])
		)
		status_color = {
			"Ready": "#16a34a",
			"Recording": "#0ea5e9",
			"Analyzing": "#f59e0b",
			"Failed": "#dc2626",
		}.get(run.get("status", ""), "#6b7280")
		html.append(
			f'<div style="margin: 16px 0; padding: 12px; '
			f'background: #f9fafb; border-radius: 4px;">'
			f'<div style="display: flex; justify-content: space-between; '
			f'align-items: baseline; margin-bottom: 8px;">'
			f'<strong>Run {run_idx}</strong> '
			f'<span style="color: {status_color}; font-size: 12px;">'
			f'{_e(run.get("status", ""))} · {run.get("total_ms", 0):.2f}ms · '
			f'{started}</span></div>'
			f'<div style="font-size: 12px; color: #6b7280; margin-bottom: 8px;">'
			f'<em>Picks:</em> {picks_summary or "—"}</div>'
		)

		for fn in run.get("functions", []):
			html.append(_render_phase2_function_table(fn))
		html.append('</div>')

	if diffs:
		html.append(
			'<h3 style="margin-top: 24px;">Cross-Run Comparison</h3>'
			'<div style="font-size: 13px; color: #4b5563; margin-bottom: 8px;">'
			'For functions profiled in two or more runs, the table below shows '
			'a line-by-line delta between the most recent two runs (aligned by '
			'content hash so file edits between runs don\'t break the diff).'
			'</div>'
		)
		for path, diff_meta in diffs.items():
			label = (
				f"{path} — Run {diff_meta['prev_run_idx'] + 1} → "
				f"Run {diff_meta['curr_run_idx'] + 1}"
			)
			html.append(
				f'<div style="margin: 16px 0;">'
				f'<div style="font-family: ui-monospace, Menlo, monospace; '
				f'font-weight: 600; margin-bottom: 4px;">{_e(label)}</div>'
			)
			html.append(_render_phase2_diff_table(diff_meta["rows"]))
			html.append('</div>')

	html.append('</section>')
	return "".join(html)


def render_raw(session_doc: Any, recordings: list[dict]) -> str:
	"""Render the admin-scoped report.

	v0.6.0 Round 7: name kept as ``render_raw`` for back-compat but
	there's no longer a ``render_safe`` counterpart — single rendering
	path. Requires the in-memory recordings list (raw SQL, headers,
	form_dict, and full stack traces are NOT stored on the DocType).
	"""
	return render(session_doc, recordings)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _action_to_dict(child: Any) -> dict:
	"""Flatten a Optimus Action child row to a plain dict."""
	return {
		"action_label": child.action_label or "",
		"event_type": child.event_type or "",
		"http_method": child.http_method or "",
		"path": child.path or "",
		"recording_uuid": child.recording_uuid or "",
		"duration_ms": child.duration_ms or 0,
		"queries_count": child.queries_count or 0,
		"query_time_ms": child.query_time_ms or 0,
		"slowest_query_ms": child.slowest_query_ms or 0,
		# v0.6.x: the per-action pyinstrument tree (Long Text). Carried into
		# the dict so render-time helpers (drill-down walker, etc.) can look
		# up the call chain without re-reading the child row.
		"call_tree_json": getattr(child, "call_tree_json", "") or "",
	}


# ---------------------------------------------------------------------------
# v0.6.0: "Background jobs" report section — render-time only (the pipeline is
# frozen). Derived purely from the persisted Optimus Action rows that have
# event_type == "Background Job" (one per captured job recording), enriched
# with the live recording's SQL when it's still in Redis.
# ---------------------------------------------------------------------------

_BG_JOB_TOP_QUERIES = 5


def _clean_job_method(action_label, path, recording) -> str:
	"""The most human-readable name for a background-job action.

	``per_action._label`` writes job labels as ``"Job: <method>"`` — strip
	that prefix. Fall back to the job method path, then the recording's
	``cmd``, then a generic placeholder."""
	label = (action_label or "").strip()
	if label:
		prefix = "Job: "
		return (label[len(prefix):].strip() or label) if label.startswith(prefix) else label
	p = (path or "").strip()
	if p:
		return p
	if recording:
		return (recording.get("cmd") or recording.get("path") or "").strip() or "Background Job"
	return "Background Job"


def build_background_jobs(actions, recordings_by_uuid, findings=None) -> dict:
	"""Build the "Background jobs" section payload from the (already
	min-duration-filtered) action dicts.

	``actions`` items are ``_action_to_dict`` output plus an ``idx`` key
	holding the action's original position (so findings — whose ``action_ref``
	is that index as a string — can be tallied per job). ``recordings_by_uuid``
	enriches each job with its slowest queries when the recording is still in
	Redis (TTL ~10 min; a re-render long after analyze has none → the section
	still renders from the persisted action rows alone). Pure — no I/O (the
	``entry_callsite`` on each job is pre-computed by ``render()`` and copied
	through here).

	Returns ``{jobs, count, total_ms, total_queries, any_findings_counted}``.
	"""
	findings_by_idx: dict[str, int] = {}
	for f in (findings or []):
		ref = (f.get("action_ref") or "").strip()
		if ref:
			findings_by_idx[ref] = findings_by_idx.get(ref, 0) + 1
	any_findings_counted = bool(findings_by_idx)

	jobs: list[dict] = []
	for a in (actions or []):
		if a.get("event_type") != "Background Job":
			continue
		uuid = a.get("recording_uuid") or ""
		rec = recordings_by_uuid.get(uuid) if recordings_by_uuid else None
		idx = a.get("idx")
		if any_findings_counted and idx is not None:
			findings_count = findings_by_idx.get(str(idx), 0)
		else:
			findings_count = None

		top_queries = None
		if rec:
			calls = rec.get("calls") or []
			ranked = sorted(calls, key=lambda c: (c.get("duration") or 0), reverse=True)
			top_queries = [
				{
					"index": c.get("index"),
					"duration": c.get("duration") or 0,
					"query": c.get("query") or "",
					"exact_copies": c.get("exact_copies") or 0,
					"normalized_copies": c.get("normalized_copies") or 0,
				}
				for c in ranked[:_BG_JOB_TOP_QUERIES]
			]

		jobs.append({
			"method": _clean_job_method(a.get("action_label"), a.get("path"), rec),
			"recording_uuid": uuid,
			"duration_ms": a.get("duration_ms") or 0,
			"queries_count": a.get("queries_count") or 0,
			"query_time_ms": a.get("query_time_ms") or 0,
			"slowest_query_ms": a.get("slowest_query_ms") or 0,
			"findings_count": findings_count,
			"top_queries": top_queries,
			"recording_available": rec is not None,
			"entry_callsite": a.get("entry_callsite"),  # pre-computed in render()
		})

	jobs.sort(key=lambda j: -(j.get("duration_ms") or 0))
	return {
		"jobs": jobs,
		"count": len(jobs),
		"total_ms": sum(j.get("duration_ms") or 0 for j in jobs),
		"total_queries": sum(j.get("queries_count") or 0 for j in jobs),
		"any_findings_counted": any_findings_counted,
	}


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


def _finding_to_dict(child: Any, file_cache: dict | None = None) -> dict:
	"""Flatten a Optimus Finding child row, parsing the JSON detail blob.

	v0.6.0 Round 2: synthesize a unified ``callsite`` shape for findings
	that store their location at the top level (call_tree's Slow Hot Path
	/ Hook Bottleneck / Repeated Hot Frame use top-level
	``filename``/``lineno``; line_profile's Hot Line / Function Not
	Invoked use top-level ``file``/``lineno``). Without this, the
	smoking-gun block in finding_card never renders for those types.

	Also lazily attach a ±1 source snippet to the callsite when one isn't
	already persisted — covers (a) sessions analyzed before the
	analyze-time enrichment shipped, (b) the synthesized callsites above.
	The optional ``file_cache`` is shared across all findings in the same
	render so a cluster of findings in one source file reads the file
	once.
	"""
	try:
		detail = json.loads(child.technical_detail_json or "{}")
	except Exception:
		detail = {}

	# v0.6.0 Round 2: synthesize callsite from legacy top-level shape
	# when the analyzer didn't wrap it. ``filename`` is the canonical
	# key; ``file`` is the line-profile alias. Both are accepted.
	if not detail.get("callsite"):
		fname = detail.get("filename") or detail.get("file")
		lineno = detail.get("lineno")
		if fname and lineno is not None:
			detail["callsite"] = {
				"filename": fname,
				"lineno": lineno,
				"function": detail.get("function") or "",
			}

	# v0.5.3: normalize callsite shape so downstream code (app
	# bucketing, template) can assume dict.
	if "callsite" in detail:
		detail["callsite"] = _normalize_callsite(detail.get("callsite"))

	# v0.6.x: findings that name a function but carry no file:line — resolve
	# them so the smoking-gun block can render. Repeated Hot Frame stores a
	# redacted ``"path::func"`` key in ``function``; Function Not Invoked
	# (phase 2) stores a ``dotted_path``. Best-effort, render-time.
	if not (detail.get("callsite") or {}).get("lineno"):
		_ftype = child.finding_type or ""
		if _ftype == "Repeated Hot Frame" and detail.get("function"):
			_cs = _resolve_frame_key_to_callsite(detail["function"], cache=file_cache)
			if _cs:
				detail["callsite"] = _cs
		elif _ftype == "Function Not Invoked" and detail.get("dotted_path"):
			_resolved = _resolve_dotted_to_code(detail["dotted_path"])
			if _resolved:
				_abs, _ln, _name = _resolved
				detail["callsite"] = {
					"filename": _bench_relative_display(_abs),
					"_abs": _abs,
					"lineno": _ln,
					"function": _name or str(detail["dotted_path"]).rsplit(".", 1)[-1],
					"source_snippet": _read_source_snippet(_abs, _ln, cache=file_cache),
				}

	# v0.6.0 Round 2: fold a Hot Line finding's persisted ``line_content``
	# directly into a single-row source_snippet. The text is already in
	# the finding's technical_detail; no file read needed.
	callsite = detail.get("callsite") or {}
	if (
		callsite
		and not callsite.get("source_snippet")
		and detail.get("line_content")
		and callsite.get("lineno") is not None
	):
		callsite["source_snippet"] = [{
			"lineno": callsite["lineno"],
			"content": detail["line_content"],
		}]
		detail["callsite"] = callsite

	# v0.6.0 Round 2: lazy snippet read at render time when nothing has
	# been attached yet (covers older sessions + synthesized callsites
	# without a line_content shortcut).
	if (
		callsite
		and callsite.get("filename")
		and callsite.get("lineno") is not None
		and not callsite.get("source_snippet")
	):
		snippet = _read_source_snippet(
			callsite["filename"], callsite["lineno"], cache=file_cache,
		)
		if snippet:
			callsite["source_snippet"] = snippet
			detail["callsite"] = callsite

	# v0.6.0: AI-suggested fix (on-demand; empty until generated). Stored as
	# JSON on the child row by api.suggest_fix. Convert the Markdown body to
	# sanitized HTML here so the template can `| safe` it (mirrors the
	# notes_html pattern). A parse/convert failure falls back to an escaped
	# <pre> so raw text is never rendered verbatim.
	llm_fix = None
	try:
		raw_llm = json.loads(getattr(child, "llm_fix_json", None) or "{}")
	except Exception:
		raw_llm = {}
	if isinstance(raw_llm, dict) and (raw_llm.get("suggestion") or "").strip():
		llm_fix = {
			"suggestion_html": _markdown_to_safe_html(raw_llm.get("suggestion")),
			"model": raw_llm.get("model") or "",
			"provider": raw_llm.get("provider") or "",
			"generated_at": raw_llm.get("generated_at") or "",
			# Older rows (pre this field) → assume the AI had context, so we
			# don't slap a "directional only" caveat on suggestions that were
			# in fact grounded in source.
			"source_available": raw_llm.get("source_available", True),
		}

	return {
		"finding_type": child.finding_type or "",
		"severity": child.severity or "Low",
		"title": child.title or "",
		"customer_description": child.customer_description or "",
		"estimated_impact_ms": child.estimated_impact_ms or 0,
		"affected_count": child.affected_count or 0,
		"action_ref": child.action_ref or "",
		"technical_detail": detail,
		"llm_fix": llm_fix,
	}


# v0.6.x: SQL "red flag" findings (Missing Index, Full Table Scan, Filesort,
# Temporary Table, Low Filter Ratio) are keyed by (finding_type, table) and
# carry no callsite — the offending query is issued from many places. At
# render time we still have the recordings, so we pick a *representative*
# callsite: the hottest user-app frame among the calls whose normalized query
# matches the finding's. Best-effort — surfaced as "Most-called from:" with a
# "representative callsite" note in the template.
_SQL_REDFLAG_FINDING_TYPES = frozenset({
	"Missing Index", "Full Table Scan", "Filesort", "Temporary Table",
	"Low Filter Ratio",
})


def _find_node_in_tree(tree: dict, basename: str, function: str) -> dict | None:
	"""Depth-first walk a pyinstrument call tree looking for a node that matches
	``(basename(filename), function)``. Returns the first hit, or ``None``.

	The basename match (rather than full path) survives bench-relative vs
	absolute path differences between where the analyzer ran and where the
	render is happening — same trick ``_build_phase2_callsite_index`` uses
	(``renderer.py:552``).
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
	if not isinstance(tree, dict) or not isinstance(callsite, dict):
		return []
	filename = callsite.get("filename") or ""
	function = callsite.get("function") or ""
	if not function:
		return []
	from optimus.analyzers.base import is_framework_callsite

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
		# Pick the hottest child by cumulative_ms.
		hottest = max(
			(c for c in children if isinstance(c, dict)),
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
		if chain:
			detail["drilldown_chain"] = chain
			finding["technical_detail"] = detail


def _attach_representative_callsites(findings, recordings, *, file_cache: dict | None = None) -> None:
	"""Attach a representative ``callsite`` (+ ``is_representative``) to SQL
	red-flag findings by matching their normalized query against the recording
	calls and picking the hottest user-app frame. Mutates ``findings`` (the
	``_finding_to_dict`` output dicts) in place. No-op when there are no such
	findings, no recordings, or nothing matches — those cards just render
	without the block.
	"""
	if not findings or not recordings:
		return
	wanted: list[dict] = []
	for f in findings:
		if (f.get("finding_type") or "") not in _SQL_REDFLAG_FINDING_TYPES:
			continue
		detail = f.get("technical_detail") or {}
		if (detail.get("callsite") or {}).get("lineno"):
			continue  # already has one
		nq = (detail.get("normalized_query") or "").strip()
		if not nq:
			continue
		wanted.append({
			"finding": f,
			"nq": nq,
			"table": (detail.get("table") or "").strip(),
			"tally": {},  # (filename, lineno, function) → weight
		})
	if not wanted:
		return

	try:
		from optimus.analyzers.base import walk_callsite
	except Exception:
		return

	for rec in recordings:
		if not isinstance(rec, dict):
			continue
		for call in rec.get("calls") or []:
			if not isinstance(call, dict):
				continue
			cnq = (call.get("normalized_query") or "").strip()
			if not cnq:
				continue
			cquery = call.get("query") or ""
			for w in wanted:
				# Equality or prefix either way (survives truncation), plus
				# the table name must appear in the raw query.
				if not (cnq == w["nq"] or cnq.startswith(w["nq"]) or w["nq"].startswith(cnq)):
					continue
				if w["table"] and w["table"] not in cquery:
					continue
				frame = walk_callsite(call.get("stack"))
				if not frame or not frame.get("filename") or frame.get("lineno") is None:
					continue
				k = (frame.get("filename"), frame.get("lineno"), frame.get("function") or "")
				w["tally"][k] = w["tally"].get(k, 0) + (call.get("duration") or 0) + 1

	for w in wanted:
		if not w["tally"]:
			continue
		(filename, lineno, function), _weight = max(w["tally"].items(), key=lambda kv: kv[1])
		abs_path = _resolve_source_path(filename)
		w["finding"]["technical_detail"]["callsite"] = {
			"filename": _bench_relative_display(abs_path) if abs_path else filename,
			"_abs": abs_path,
			"lineno": lineno,
			"function": function,
			"source_snippet": _read_source_snippet(abs_path or filename, lineno, cache=file_cache),
			"is_representative": True,
		}


# ---------------------------------------------------------------------------
# v0.6.x: action / finding context — which document a save/submit action
# touched, and which doc-event lifecycle hook a slow function fired in. All
# derived at render time from the in-memory recordings + frappe.get_hooks.
# ---------------------------------------------------------------------------


def _module_from_filename(filename) -> str:
	"""``ugly_code/python/common.py`` → ``ugly_code.python.common`` (pyinstrument's
	short app-relative filename → its module dotted path). Empty on bad input."""
	if not filename:
		return ""
	name = str(filename).replace("\\", "/")
	if name.endswith(".py"):
		name = name[:-3]
	return ".".join(p for p in name.split("/") if p)


def _extract_target_doc(form_dict) -> dict | None:
	"""Best-effort: pull ``{"doctype", "name"}`` out of a request's form_dict
	for doc-mutating endpoints — ``savedocs`` / ``frappe.client.save|insert|submit``
	(a ``doc`` JSON string or dict), ``run_doc_method`` (``dt``/``dn`` or a
	``docs`` JSON), ``apply_workflow`` (``doc``), or bare ``doctype``/``name``
	fields. ``name`` may be a temp name ("new-…") or ``None`` for an unsaved
	doc. Returns ``None`` when nothing doc-shaped is present. Never raises."""
	if not isinstance(form_dict, dict) or not form_dict:
		return None
	try:
		dt = form_dict.get("dt") or form_dict.get("doctype")
		dn = form_dict.get("dn") or form_dict.get("name") or form_dict.get("docname")
		if isinstance(dt, str) and dt.strip():
			return {"doctype": dt.strip(), "name": (dn.strip() if isinstance(dn, str) and dn.strip() else None)}
		for key in ("doc", "docs"):
			raw = form_dict.get(key)
			if raw is None:
				continue
			parsed = raw
			if isinstance(raw, str):
				try:
					parsed = json.loads(raw)
				except Exception:
					continue
			if isinstance(parsed, list):
				parsed = next((d for d in parsed if isinstance(d, dict) and d.get("doctype")), None)
			if isinstance(parsed, dict) and parsed.get("doctype"):
				nm = parsed.get("name")
				return {"doctype": str(parsed["doctype"]), "name": (str(nm) if nm else None)}
	except Exception:
		return None
	return None


def _build_doc_event_hook_index(doc_events) -> dict:
	"""Flatten Frappe's ``doc_events`` map — ``{doctype: {event: [paths]}}``
	(``doctype`` may be ``"*"``) — into ``{dotted_path: [(doctype, event), …]}``.
	Pure. Empty on bad input."""
	index: dict[str, list[tuple[str, str]]] = {}
	if not isinstance(doc_events, dict):
		return index
	for doctype, events in doc_events.items():
		if not isinstance(events, dict):
			continue
		for event, methods in events.items():
			if isinstance(methods, str):
				methods = [methods]
			if not isinstance(methods, (list, tuple)):
				continue
			for m in methods:
				if isinstance(m, str) and m:
					index.setdefault(m, []).append((str(doctype), str(event)))
	return index


def _doc_event_hook_index() -> dict:
	"""``_build_doc_event_hook_index(frappe.get_hooks("doc_events"))`` — or ``{}``
	when frappe isn't available (e.g. unit tests with no running site)."""
	try:
		import frappe
		return _build_doc_event_hook_index(frappe.get_hooks("doc_events"))
	except Exception:
		return {}


def _finding_hook_events(detail, hook_index, *, action_doctype: str | None = None) -> list[dict]:
	"""For a call-tree finding's ``technical_detail`` (``function`` + ``filename``),
	return the doc-event lifecycle hook(s) the function is registered for, as
	``[{"doctype", "event"}, …]``. A ``"*"`` (all-doctypes) hook is reported
	against ``action_doctype`` when known, else ``"*"``. Empty when the function
	isn't a registered doc-event hook (or its dotted path can't be rebuilt)."""
	if not isinstance(detail, dict) or not hook_index:
		return []
	func = str(detail.get("function") or "").strip()
	filename = detail.get("filename") or ""
	if not func or not filename:
		return []
	bare = func.rsplit(".", 1)[-1].rsplit(":", 1)[-1].strip()
	module = _module_from_filename(filename)
	if not module or not bare:
		return []
	pairs = hook_index.get(f"{module}.{bare}")
	if not pairs:
		return []
	out: list[dict] = []
	seen = set()
	for hd, ev in pairs:
		shown_dt = action_doctype if (hd == "*" and action_doctype) else hd
		if (shown_dt, ev) in seen:
			continue
		seen.add((shown_dt, ev))
		out.append({"doctype": shown_dt, "event": ev})
	return out


def _attach_action_context(actions, findings, recordings_by_uuid) -> None:
	"""Enrich ``actions`` and ``findings`` (in place):

	  * ``action["target_doc"]`` — the document a save/submit-style action
	    touched (from the recording's form_dict), or ``None``.
	  * ``finding["technical_detail"]["target_doc"]`` — same, via the finding's
	    ``action_ref`` → action (key omitted when there's no doc).
	  * ``finding["technical_detail"]["hook_events"]`` — the doc-event lifecycle
	    hook(s) the finding's hot function fired in (``[{doctype,event}, …]``);
	    key omitted when the function isn't a registered ``doc_events`` hook.
	"""
	recordings_by_uuid = recordings_by_uuid or {}
	for a in (actions or []):
		if not isinstance(a, dict):
			continue
		rec = recordings_by_uuid.get(a.get("recording_uuid") or "")
		a["target_doc"] = _extract_target_doc(rec.get("form_dict") if isinstance(rec, dict) else None)
	by_idx = {a.get("idx"): a for a in (actions or []) if isinstance(a, dict)}
	hook_index = _doc_event_hook_index()
	for f in (findings or []):
		if not isinstance(f, dict):
			continue
		detail = f.get("technical_detail")
		if not isinstance(detail, dict):
			continue
		ref = (f.get("action_ref") or "").strip()
		td = None
		if ref.isdigit():
			act = by_idx.get(int(ref))
			td = act.get("target_doc") if isinstance(act, dict) else None
		if td:
			detail["target_doc"] = td
		hevs = _finding_hook_events(detail, hook_index, action_doctype=(td or {}).get("doctype"))
		if hevs:
			detail["hook_events"] = hevs


# ---------------------------------------------------------------------------
# v0.6.x: "Doc-event lifecycle" section — re-group the slow call-tree findings
# by DocType → lifecycle event (validate / on_submit / …), tagging each as a
# registered ``doc_events`` hook vs a controller method override, and surfacing
# cascaded DocTypes (e.g. GL Entry touched during a Sales Invoice submit). Pure,
# render-time, derived from the findings already enriched by _attach_action_context.
# ---------------------------------------------------------------------------

# Frappe's doc-event lifecycle method names — a function whose bare name is one
# of these AND whose file is a controller (``.../doctype/<scrub>/<scrub>.py``)
# is a lifecycle override.
_LIFECYCLE_EVENTS = frozenset({
	"before_naming", "autoname", "before_insert", "after_insert",
	"before_validate", "validate", "before_save", "after_save", "on_update",
	"before_submit", "on_submit", "before_update_after_submit", "on_update_after_submit",
	"before_cancel", "on_cancel", "before_change", "on_change",
	"on_trash", "after_delete", "before_rename", "after_rename", "before_print",
})

# Doc-event "kinds" surfaced in the breakdown.
_KIND_DOC_EVENTS_HOOK = "doc_events hook"
_KIND_CONTROLLER_OVERRIDE = "controller override"

_SEVERITY_RANK = {"High": 3, "Medium": 2, "Low": 1}


def _doctype_from_controller_path(filename) -> str | None:
	"""``erpnext/accounts/doctype/sales_invoice/sales_invoice.py`` → ``"Sales Invoice"``
	(the segment right after ``doctype/``, un-scrubbed). Works on app-relative,
	bench-relative, and absolute paths. ``None`` for non-controller paths. NB:
	``.title()`` mangles multi-cap names ("gl_entry" → "Gl Entry") — same as
	``frappe.unscrub``; accepted."""
	if not filename:
		return None
	parts = [p for p in str(filename).replace("\\", "/").strip("/").split("/") if p]
	try:
		i = parts.index("doctype")
	except ValueError:
		return None
	if i + 1 >= len(parts):
		return None
	slug = parts[i + 1].strip()
	if not slug:
		return None
	return slug.replace("_", " ").replace("-", " ").title()


def _finding_lifecycle_bindings(finding) -> list[tuple[str, str, str]]:
	"""Return ``[(doctype, event, kind), …]`` — the doc-event lifecycle slots a
	finding belongs to (usually 0 or 1). ``kind`` is ``_KIND_DOC_EVENTS_HOOK``
	(from the finding's already-resolved ``technical_detail.hook_events``) or
	``_KIND_CONTROLLER_OVERRIDE`` (function name is a lifecycle event AND its
	file is a controller). Deduped by ``(doctype, event)`` — hook bindings first.
	Empty when the finding isn't a lifecycle method (a generic Slow Hot Path on
	a helper, an N+1 with no controller callsite, …)."""
	if not isinstance(finding, dict):
		return []
	detail = finding.get("technical_detail") or {}
	if not isinstance(detail, dict):
		return []
	cs = detail.get("callsite") or {}
	out: list[tuple[str, str, str]] = []
	seen: set[tuple[str, str]] = set()

	for he in (detail.get("hook_events") or []):
		if not isinstance(he, dict):
			continue
		dt = (he.get("doctype") or "").strip()
		ev = (he.get("event") or "").strip()
		if dt and ev and (dt, ev) not in seen:
			seen.add((dt, ev))
			out.append((dt, ev, _KIND_DOC_EVENTS_HOOK))

	fn = (cs.get("function") if isinstance(cs, dict) else None) or detail.get("function") or ""
	ev = str(fn).rsplit(".", 1)[-1].rsplit(":", 1)[-1].strip()
	if ev in _LIFECYCLE_EVENTS:
		fname = (cs.get("filename") if isinstance(cs, dict) else None) or detail.get("filename") or ""
		dt = _doctype_from_controller_path(fname)
		if dt and (dt, ev) not in seen:
			seen.add((dt, ev))
			out.append((dt, ev, _KIND_CONTROLLER_OVERRIDE))
	return out


def _build_doc_event_breakdown(findings) -> dict:
	"""Group the slow call-tree findings by DocType → lifecycle event. Pure.

	Returns ``{"doctypes": [ {doctype, is_save_target, touched_during,
	total_ms, method_count, events: [{event, total_ms, methods:
	[{function, filename, _abs, lineno, ms, count, kind, severity,
	finding_type}]}]} … ], "count": int, "method_count": int}``. Empty
	``{"doctypes": [], "count": 0, "method_count": 0}`` when nothing binds."""
	groups: dict[str, dict] = {}
	for f in (findings or []):
		bindings = _finding_lifecycle_bindings(f)
		if not bindings:
			continue
		detail = f.get("technical_detail") or {}
		cs = detail.get("callsite") or {}
		if not isinstance(cs, dict):
			cs = {}
		fn_name = cs.get("function") or detail.get("function") or "?"
		fname = cs.get("filename") or detail.get("filename") or ""
		abs_path = cs.get("_abs")
		lineno = cs.get("lineno") or detail.get("lineno")
		try:
			ms = float(detail.get("cumulative_ms") or f.get("estimated_impact_ms") or 0)
		except (TypeError, ValueError):
			ms = 0.0
		severity = f.get("severity") or "Low"
		ftype = f.get("finding_type") or ""
		action_dt = (detail.get("target_doc") or {}).get("doctype") if isinstance(detail.get("target_doc"), dict) else None

		for dt, ev, kind in bindings:
			g = groups.setdefault(dt, {"doctype": dt, "is_save_target": False, "touched_during": set(), "events": {}})
			if action_dt:
				if action_dt == dt:
					g["is_save_target"] = True
				else:
					g["touched_during"].add(action_dt)
			ev_bucket = g["events"].setdefault(ev, {})
			key = (fn_name, fname)
			rec = ev_bucket.get(key)
			if rec is None:
				ev_bucket[key] = {
					"function": fn_name, "filename": fname, "_abs": abs_path,
					"lineno": lineno, "ms": ms, "count": 1, "kind": kind,
					"severity": severity, "finding_type": ftype,
				}
			else:
				rec["ms"] += ms
				rec["count"] += 1
				if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(rec["severity"], 0):
					rec["severity"] = severity
				# A controller override is the more specific label — prefer it.
				if kind == _KIND_CONTROLLER_OVERRIDE:
					rec["kind"] = kind

	out_doctypes: list[dict] = []
	for g in groups.values():
		events_out: list[dict] = []
		for ev, bucket in g["events"].items():
			methods = sorted(bucket.values(), key=lambda m: -(m["ms"] or 0))
			events_out.append({"event": ev, "total_ms": sum(m["ms"] or 0 for m in methods), "methods": methods})
		events_out.sort(key=lambda e: -(e["total_ms"] or 0))
		out_doctypes.append({
			"doctype": g["doctype"],
			"is_save_target": g["is_save_target"],
			"touched_during": sorted(g["touched_during"]),
			"total_ms": sum(e["total_ms"] or 0 for e in events_out),
			"method_count": sum(len(e["methods"]) for e in events_out),
			"events": events_out,
		})
	# Sort: save-targets first, then by total time.
	out_doctypes.sort(key=lambda d: (0 if d["is_save_target"] else 1, -(d["total_ms"] or 0)))
	return {
		"doctypes": out_doctypes,
		"count": len(out_doctypes),
		"method_count": sum(d["method_count"] for d in out_doctypes),
	}


def _markdown_to_safe_html(text) -> str:
	"""Render Markdown → sanitized HTML for embedding in the report.

	Mirrors the notes sanitization path (``frappe.utils.markdown`` +
	``sanitize_html(..., always_sanitize=True)``). On ANY failure — Frappe
	not importable, markdown/bleach hiccup — falls back to an HTML-escaped
	``<pre>`` block so the report NEVER renders un-sanitized model output.

	After sanitizing, fenced ``diff`` code blocks (which the AI-fix prompt
	asks the model to use for before/after) get per-line ``dh-add`` /
	``dh-del`` / ``dh-meta`` span wrappers so the report CSS can colour them
	like a real diff. We only add ``<span>`` wrappers around already-escaped
	text — nothing that could re-introduce unsafe markup.
	"""
	raw = "" if text is None else str(text)
	try:
		from frappe.utils import markdown as _md
		from frappe.utils.html_utils import sanitize_html
		return _highlight_diff_html(sanitize_html(_md(raw), always_sanitize=True))
	except Exception:
		import html as _html
		return _highlight_diff_html(
			'<pre style="white-space:pre-wrap;">' + _html.escape(raw) + "</pre>"
		)


# <pre> block, optionally wrapping a <code>...</code> (with or without a class).
_PRE_BLOCK_RE = re.compile(
	r'<pre[^>]*>(?:\s*<code([^>]*)>)?(.*?)(?:</code>\s*)?</pre>', re.S
)


def _looks_like_diff(code_attrs: str, lines: list[str]) -> bool:
	if "diff" in (code_attrs or ""):
		return True
	if any(ln.startswith("@@") for ln in lines):
		return True
	has_add = any(ln.startswith("+") for ln in lines)
	has_del = any(ln.startswith("-") for ln in lines)
	return has_add and has_del


def _diff_line_class(line: str) -> str | None:
	if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
		return "dh-meta"
	if line.startswith("+"):
		return "dh-add"
	if line.startswith("-"):
		return "dh-del"
	return None


def _highlight_diff_html(html: str) -> str:
	"""Wrap +/-/@@ lines inside diff-looking ``<pre>`` blocks in classed
	spans. Pure string transform over already-sanitized HTML — only adds
	``<span class="dh-…">`` wrappers around existing escaped text."""

	def _wrap(match: re.Match) -> str:
		code_attrs = match.group(1) or ""
		inner = match.group(2) or ""
		lines = inner.split("\n")
		# A trailing "" from the markdown renderer's final newline — drop it
		# so we don't emit an empty trailing block-span.
		if lines and lines[-1] == "":
			lines = lines[:-1]
		if not lines or not _looks_like_diff(code_attrs, lines):
			return match.group(0)
		out: list[str] = []
		for ln in lines:
			cls = _diff_line_class(ln)
			label = f"dh-line {cls}" if cls else "dh-line dh-ctx"
			out.append(f'<span class="{label}">{ln or "&#8203;"}</span>')
		code_open = f"<code{code_attrs}>" if code_attrs else "<code>"
		return f'<pre class="dh">{code_open}' + "".join(out) + "</code></pre>"

	return _PRE_BLOCK_RE.sub(_wrap, html)


# Per-line truncation for source snippets/windows — keeps a single
# multi-kilobyte minified line out of technical_detail_json / the LLM prompt.
# (Kept here, with the readers, rather than imported from analyze.py — so the
# readers don't pull in analyze.py, which imports frappe.recorder.)
_SNIPPET_TRUNCATE_CHARS = 200


def _resolve_source_path(filename) -> str | None:
	"""Map a finding's callsite ``filename`` to a real file on disk.

	Call-tree / pyinstrument callsites are stored in app-relative form
	(``<app>/<module-path-within-the-app-dir>`` — e.g. ``ugly_code/python/
	common.py`` for ``<bench>/apps/ugly_code/ugly_code/python/common.py``,
	or ``frappe/handler.py``). A bare ``open()`` fails because the Frappe
	process cwd is ``<bench>/sites``. Resolve via ``frappe.get_app_path``
	(``frappe.get_app_path("ugly_code", "python", "common.py")`` →
	``<bench>/apps/ugly_code/ugly_code/python/common.py``), with fallbacks
	for absolute / cwd-relative / ``apps/…``-prefixed forms. Returns ``None``
	for synthetic names (``<string>``, ``<frozen …>``), unresolvable paths,
	or when ``frappe`` isn't importable (unit tests)."""
	if not filename:
		return None
	name = str(filename).strip()
	if not name or name.startswith("<"):
		return None
	try:
		if os.path.isabs(name):
			return name if os.path.exists(name) else None
		if os.path.exists(name):
			return name
		parts = [p for p in name.replace("\\", "/").split("/") if p]
		if not parts:
			return None
		import frappe

		candidates = []
		try:
			candidates.append(frappe.get_app_path(parts[0], *parts[1:]))
		except Exception:
			pass
		try:
			bench = frappe.get_bench_path()
			candidates.append(os.path.join(bench, name))
			candidates.append(os.path.join(bench, "apps", name))
		except Exception:
			pass
		for cand in candidates:
			if cand and os.path.exists(cand):
				return cand
	except Exception:
		return None
	return None


def _read_source_snippet(
	filename: str,
	lineno,
	*,
	cache: dict | None = None,
) -> list[dict] | None:
	"""Return a ±1-line source snippet for ``(filename, lineno)``, or
	``None`` when the file isn't readable / lineno is out of range. The
	(possibly app-relative) ``filename`` is resolved via
	``_resolve_source_path`` before opening."""
	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	if cache is not None and filename in cache:
		lines = cache[filename]
	else:
		resolved = _resolve_source_path(filename)
		try:
			with open(resolved, encoding="utf-8") as fh:
				lines = fh.read().splitlines()
		except Exception:
			lines = None
		if cache is not None:
			cache[filename] = lines

	if not lines:
		return None

	limit = _SNIPPET_TRUNCATE_CHARS
	snippet: list[dict] = []
	for n in (ln - 1, ln, ln + 1):
		if 1 <= n <= len(lines):
			content = lines[n - 1]
			if len(content) > limit:
				content = content[:limit] + "..."
			snippet.append({"lineno": n, "content": content})
	return snippet or None


def _action_dotted_entry(action) -> str | None:
	"""Derive an action's dotted entry-point path, or ``None``.

	- Background Job: ``action["path"]`` is already the job method (Frappe's
	  recorder stores ``frappe.job.method`` there — e.g.
	  ``ugly_code.python.common.bg_recheck_users``).
	- HTTP Request whose path is ``/api/method/<dotted>``: the ``<dotted>``
	  segment, with any ``?query`` and trailing ``/...`` stripped.
	- anything else (non-``/api/method`` HTTP, empty/missing path, non-dict
	  input): ``None``.
	"""
	if not isinstance(action, dict):
		return None
	event_type = (action.get("event_type") or "").strip()
	path = (action.get("path") or "").strip()
	if not path:
		return None
	if event_type == "Background Job":
		return path.split("?", 1)[0].strip() or None
	if event_type == "HTTP Request" and path.startswith("/api/method/"):
		rest = path[len("/api/method/"):]
		rest = rest.split("?", 1)[0].split("/", 1)[0].strip().strip(".")
		return rest or None
	return None


def _resolve_dotted_to_code(dotted) -> tuple[str, int, str] | None:
	"""Resolve a dotted module path to ``(abs_filename, lineno, func_name)``.

	Uses ``importlib`` directly — NOT ``frappe.get_attr`` — because the
	latter needs a running site (it touches ``frappe.local``), which the unit
	tests don't have. Mirrors ``line_profile.picker.resolve_freeform``'s
	import strategy (longest importable leading prefix, then ``getattr`` the
	rest), minus its eligibility checks. ``inspect.unwrap`` sees through
	``functools.wraps`` decorators (e.g. ``@frappe.whitelist``). Returns
	``None`` on any failure — never raises.
	"""
	if not dotted or "." not in str(dotted):
		return None
	try:
		import importlib
		import inspect

		parts = str(dotted).split(".")
		module = None
		mod_parts = 0
		for i in range(len(parts), 0, -1):
			try:
				module = importlib.import_module(".".join(parts[:i]))
				mod_parts = i
				break
			except Exception:
				continue
		if module is None or mod_parts == len(parts):
			return None  # nothing imported, or it's a module not a callable
		obj = module
		for attr in parts[mod_parts:]:
			obj = getattr(obj, attr)
		obj = inspect.unwrap(obj)
		code = getattr(obj, "__code__", None)
		if code is None:
			return None  # builtin / C func / not a plain Python function
		filename = code.co_filename or ""
		lineno = code.co_firstlineno or 0
		if not filename or filename.startswith("<") or lineno <= 0:
			return None  # Server Script / eval'd code / bogus
		return (
			os.path.abspath(filename),
			int(lineno),
			getattr(obj, "__name__", "") or "",
		)
	except Exception:
		return None


def _bench_relative_display(abs_path: str) -> str:
	"""Display form of an absolute source path: ``apps/<app>/.../file.py``
	(relative to the bench root). Falls back to the absolute path when the
	file is outside the bench or the bench path can't be determined."""
	try:
		from frappe.utils import get_bench_path

		rel = os.path.relpath(abs_path, get_bench_path())
		if rel and not rel.startswith(".."):
			return rel.replace("\\", "/")
	except Exception:
		pass
	return abs_path


def _action_entry_callsite(action, *, cache: dict | None = None) -> dict | None:
	"""Resolve an action's entry-point source location + a ±1-line snippet.

	Returns ``{"filename": <bench-relative display path>, "_abs": <absolute>,
	"lineno": <def line>, "function": <name>, "source_snippet": [...] | None}``
	— or ``None`` when there's no clean dotted entry point / it can't be
	resolved / the callable has no real source. ``source_snippet`` may itself
	be ``None`` if the file can't be read (the template guards on it).

	``cache`` (shared across all actions in one render) is forwarded to
	``_read_source_snippet`` so a cluster of actions in one source file reads
	it once. Resolution itself isn't memoized — it's cheap (``importlib`` on
	already-imported modules) and reports have only tens of actions.
	"""
	dotted = _action_dotted_entry(action)
	if not dotted:
		return None
	resolved = _resolve_dotted_to_code(dotted)
	if not resolved:
		return None
	abs_path, lineno, name = resolved
	return {
		"filename": _bench_relative_display(abs_path),
		"_abs": abs_path,
		"lineno": lineno,
		"function": name,
		"source_snippet": _read_source_snippet(abs_path, lineno, cache=cache),
	}


def _resolve_frame_key_to_callsite(function_key, *, cache: dict | None = None) -> dict | None:
	"""Resolve a Repeated Hot Frame's ``function`` value to a callsite + a
	±1-line snippet, or ``None``.

	The key is ``call_tree._redacted_module_key``'s output:
	``f"{short_path}::{func}"`` where ``short_path`` is the last ≤2 path
	segments of the original file (e.g. ``ugly_code/python/common.py``) and
	``func`` is the bare frame name (occasionally ``Class.method``); or just
	``func`` when there was no filename. Best-effort, render-time:

	  1. ``short_path`` → dotted module + ``.func`` → ``_resolve_dotted_to_code``
	     (works for shallow user-app paths; deep paths where 2 segments can't
	     rebuild the real module just fall through).
	  2. fallback: resolve ``short_path`` to a real file and grep for the first
	     ``def <func>`` line.

	A bare ``func`` (no ``::``) can't be resolved without a module → ``None``.
	Returns ``{"filename","_abs","lineno","function","source_snippet"}`` or
	``None``. Wrapped in try/except — never raises.
	"""
	if not function_key:
		return None
	try:
		key = str(function_key)
		if "::" not in key:
			return None
		short_path, _, func = key.partition("::")
		short_path = short_path.strip()
		func = func.strip()
		if not short_path or not func:
			return None

		# (1) "ugly_code/python/common.py" + "looped_validate"
		#     → "ugly_code.python.common.looped_validate"
		norm = short_path.replace("\\", "/")
		if norm.endswith(".py"):
			norm = norm[:-3]
		dotted = norm.replace("/", ".").strip(".")
		if dotted:
			resolved = _resolve_dotted_to_code(f"{dotted}.{func}")
			if resolved:
				abs_path, lineno, name = resolved
				return {
					"filename": _bench_relative_display(abs_path),
					"_abs": abs_path,
					"lineno": lineno,
					"function": name or func,
					"source_snippet": _read_source_snippet(abs_path, lineno, cache=cache),
				}

		# (2) grep the resolved file for "def <last component of func>"
		abs_path = _resolve_source_path(short_path)
		if abs_path:
			bare = func.rsplit(".", 1)[-1]
			pat = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+" + re.escape(bare) + r"\b")
			try:
				with open(abs_path, encoding="utf-8") as fh:
					for i, line in enumerate(fh, start=1):
						if pat.match(line):
							return {
								"filename": _bench_relative_display(abs_path),
								"_abs": abs_path,
								"lineno": i,
								"function": func,
								"source_snippet": _read_source_snippet(abs_path, i, cache=cache),
							}
			except Exception:
				pass
	except Exception:
		return None
	return None


def _read_source_window(
	filename: str,
	lineno,
	*,
	before: int = 12,
	after: int = 12,
	cache: dict | None = None,
	max_line_chars: int | None = None,
) -> list[dict] | None:
	"""Return a wider source window around ``(filename, lineno)`` for the
	AI-fix prompt: a list of ``{lineno, content, is_target}`` covering
	``lineno - before`` … ``lineno + after`` (clamped to the file). Same
	per-line truncation as ``_read_source_snippet`` unless ``max_line_chars``
	overrides it. Returns ``None`` when the file isn't readable / the lineno
	is out of range. The (possibly app-relative) ``filename`` is resolved via
	``_resolve_source_path`` before opening.
	"""
	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	if cache is not None and filename in cache:
		lines = cache[filename]
	else:
		resolved = _resolve_source_path(filename)
		try:
			with open(resolved, encoding="utf-8") as fh:
				lines = fh.read().splitlines()
		except Exception:
			lines = None
		if cache is not None:
			cache[filename] = lines

	if not lines:
		return None

	limit = max_line_chars or _SNIPPET_TRUNCATE_CHARS
	start = max(1, ln - max(0, before))
	end = min(len(lines), ln + max(0, after))
	window: list[dict] = []
	for n in range(start, end + 1):
		content = lines[n - 1]
		if len(content) > limit:
			content = content[:limit] + "..."
		window.append({"lineno": n, "content": content, "is_target": n == ln})
	return window or None


# ---------------------------------------------------------------------------
# v0.5.2 round 3: Executive summary
# ---------------------------------------------------------------------------
# A one-paragraph TL;DR at the top of the report that a non-developer
# (product manager, ops lead, customer) can read in 30 seconds and walk
# away knowing (1) is the session slow, (2) what's the biggest problem,
# (3) how much of the time it accounts for. Surfaces the top 3 most-
# impactful actionable findings as plain-English bullets.


def _build_executive_summary(
	*,
	findings: list[dict],
	session_doc: Any,
	v5: dict,
) -> dict:
	"""Return a dict shaped for the template's exec-summary card.

	Shape: ``{"headline": str, "bullets": list[str], "show": bool}``

	``show`` is False when there's nothing meaningful to summarize —
	e.g. a clean session with no findings. The template renders the
	card only when ``show`` is True.
	"""
	total_ms = getattr(session_doc, "total_duration_ms", 0) or 0
	total_queries = getattr(session_doc, "total_queries", 0) or 0
	total_actions = getattr(session_doc, "total_requests", 0) or 0

	# Headline — describes the session at a glance.
	if total_ms >= 5000:
		pace = "slow"
	elif total_ms >= 2000:
		pace = "moderate"
	else:
		pace = "fast"

	queries_per_action = (
		round(total_queries / total_actions, 1) if total_actions else 0
	)
	headline = (
		f"This session took {int(total_ms)}ms across {total_actions} "
		f"operation{'s' if total_actions != 1 else ''} "
		f"— {int(total_queries)} database queries, ~{queries_per_action} per operation."
	)

	# Pull the top 3 findings by estimated_impact_ms (already sorted
	# globally by severity+impact, but we want PURE impact order for
	# the exec view). No finding = no bullet = no card.
	top_findings = sorted(
		findings,
		key=lambda f: -(f.get("estimated_impact_ms") or 0),
	)[:3]

	bullets = []
	total_impact = sum(f.get("estimated_impact_ms") or 0 for f in top_findings)
	for f in top_findings:
		impact = f.get("estimated_impact_ms") or 0
		title = f.get("title") or "Finding"
		# v0.6.x: append the target document and the doc-event lifecycle hook
		# when known, so the bullet says e.g. "… — Sales Invoice SINV-1
		# (during the validate hook)" instead of just the action name.
		_detail = f.get("technical_detail") or {}
		_td = _detail.get("target_doc") or {}
		_hevs = _detail.get("hook_events") or []
		if _td.get("doctype"):
			title += " — " + _td["doctype"] + (" " + _td["name"] if _td.get("name") else "")
		if _hevs:
			title += " (during the " + str(_hevs[0].get("event") or "") + " hook)"
		bullets.append({
			"text": title,
			"impact_ms": round(impact, 0),
			"severity": f.get("severity") or "Low",
		})

	# Infra signal — if swap was active or memory grew >50MB, call it out.
	infra_summary = v5.get("infra_summary") or {}
	rss_delta_mb = round((infra_summary.get("rss_delta") or 0) / 1_000_000, 0)
	swap_mb = infra_summary.get("swap_peak_mb") or 0
	infra_note = None
	if rss_delta_mb and abs(rss_delta_mb) >= 50:
		direction = "grew" if rss_delta_mb > 0 else "shrank"
		infra_note = f"Worker memory {direction} by {abs(int(rss_delta_mb))}MB during the session."
	if swap_mb and swap_mb >= 100:
		s = f"Swap was active ({int(swap_mb)}MB)"
		infra_note = f"{infra_note} {s}." if infra_note else s + "."

	show = bool(bullets or infra_note)
	return {
		"headline": headline,
		"bullets": bullets,
		"infra_note": infra_note,
		"total_impact_ms": round(total_impact, 0),
		"pace": pace,
		"show": show,
	}


# ---------------------------------------------------------------------------
# v0.5.2: Per-app sub-grouping inside Findings and Observations
# ---------------------------------------------------------------------------
# Each finding carries a ``technical_detail.callsite.filename`` set by the
# analyzers (when they can resolve a blame frame). We bucket findings by
# their top-level app segment so the report reads as:
#
#   Findings — what to fix
#     ▸ myapp (3 findings, ~420ms)
#         N+1 in ...
#         Missing index on ...
#     ▸ custom_invoicing (1 finding, ~60ms)
#         Full table scan on ...
#
# This is what the user asked for so "the framework and other 1 party
# app's scripts can be easily avoided and focus on their custom app".


_OTHER_APP_LABEL = "Other (no callsite)"

# v0.5.2 round 4: finding types that legitimately have no code-location
# callsite because they describe scope-level timing ("56% of savedocs
# Submit was in on_submit") rather than a specific line. When the no-
# callsite bucket is made up ENTIRELY of these, we rename it from the
# undersell "Other (no callsite)" → "Request hotspots" so the user
# understands it's where request time actually went.
_HOTPATH_FINDING_TYPES: frozenset[str] = frozenset({
	"Slow Hot Path",
	"Hook Bottleneck",
	"Slow Frontend Render",
})

_HOTPATH_BUCKET_LABEL = "Request hotspots"


def _filter_top_queries_for_display(queries: list) -> list:
	"""Trim the slowest-queries leaderboard to what's worth showing:
	user-app callsites only, and only queries that cleared the
	"actually did some work" floor (``TOP_QUERY_FLOOR_MS``).

	Mirrors what ``analyzers.top_queries`` does at analyze time so that
	re-rendering a session captured before this filter shipped (via
	``regenerate_reports``, which re-renders but doesn't re-analyze)
	gets the same scoping. The per-action breakdown still shows every
	query, fast and framework ones included.
	"""
	from optimus.analyzers.base import is_framework_callsite_str
	from optimus.analyzers.top_queries import TOP_QUERY_FLOOR_MS

	try:
		from optimus.settings import get_tracked_apps
		tracked = tuple(get_tracked_apps() or ())
	except Exception:
		tracked = ()

	out: list = []
	for q in queries or []:
		if not isinstance(q, dict):
			continue
		if (q.get("duration_ms") or 0) < TOP_QUERY_FLOOR_MS:
			continue
		if not is_framework_callsite_str(q.get("callsite"), tracked):
			out.append(q)
	return out


def _is_framework_app(filename_or_app, tracked_apps: tuple[str, ...] = ()) -> bool:
	"""Tiny adapter around ``analyzers.base.is_framework_callsite`` that accepts
	any of: (a) a callsite filename (passed through), (b) a bare app name like
	``"frappe"``, or (c) a dotted Python module/method like
	``"frappe.desk.form.save.savedocs"`` — both (b) and (c) are normalised to
	``"<app>/x.py"`` so the boundary-sensitive substring checks in
	``is_framework_callsite`` fire. Falsy/missing input → ``False`` (treat as
	user code so unattributable rows aren't penalised).

	Used by the four "Split: custom apps prominent, framework collapsed"
	sections (per-action, top-queries, background-jobs, hot-frames) to route
	rows. ``tracked_apps`` flips the classifier to inclusion mode (framework
	= anything NOT in the allowlist) when populated."""
	if not filename_or_app:
		return False
	val = str(filename_or_app).strip()
	if not val:
		return False
	norm = val.replace("\\", "/")
	if "/" not in norm:
		# Bare app name OR dotted module path — take the first dotted
		# segment (the top-level package) and synthesise a path so the
		# substring checks against ``<app>/`` fire.
		first = norm.split(".", 1)[0]
		val = f"{first}/x.py"
	from optimus.analyzers.base import is_framework_callsite
	return is_framework_callsite(val, tracked_apps=tracked_apps or None)


def _split_by_framework_app(rows, app_key, tracked_apps: tuple[str, ...] = ()):
	"""Split a list into ``(custom, framework)`` preserving order within each.
	``app_key`` is a callable ``row → str | None`` that returns either a
	filename or a bare app name; ``_is_framework_app`` classifies."""
	custom, framework = [], []
	for r in (rows or []):
		try:
			val = app_key(r)
		except Exception:
			val = None
		(framework if _is_framework_app(val, tracked_apps) else custom).append(r)
	return custom, framework


def _app_from_finding(finding: dict) -> str:
	"""Return the top-level app name for a finding, or ``_OTHER_APP_LABEL``.

	Inspects ``technical_detail.callsite.filename`` using the same
	boundary-sensitive split as the framework classifier — the goal is
	that the app name shown in the sub-section header matches what
	``is_framework_callsite`` would see.

	Defensive: accepts both the dict form (n_plus_one/redundant_calls/
	explain_flags) and the legacy string form (top_queries Slow Query
	findings). _finding_to_dict already normalizes these at load time,
	but we double-check here so direct callers (tests, retry paths)
	don't crash on an un-normalized finding.
	"""
	from optimus.analyzers.base import _extract_app_segment

	detail = finding.get("technical_detail") or {}
	callsite_raw = detail.get("callsite")
	callsite = _normalize_callsite(callsite_raw) or {}
	filename = (callsite.get("filename") or "").replace("\\", "/")
	app = _extract_app_segment(filename)
	return app or _OTHER_APP_LABEL


def _bucket_findings_by_app(
	findings: list[dict],
	tracked_apps: tuple[str, ...] = (),
) -> list[dict]:
	"""Group findings by app and return an ordered list of buckets.

	Each bucket is a dict:
	``{"app": str, "findings": list, "count": int, "total_impact_ms": float}``

	Ordering rules:
	1. Tracked apps first, in the order the admin listed them in
	   Optimus Settings (user's mental model: "my apps first").
	2. Any other apps next, sorted by total estimated impact desc.
	3. ``_OTHER_APP_LABEL`` (no resolvable callsite) last — always the
	   tail bucket because its contents are less actionable.
	"""
	if not findings:
		return []

	buckets: dict[str, list[dict]] = {}
	for f in findings:
		app = _app_from_finding(f)
		buckets.setdefault(app, []).append(f)

	# v0.5.2 round 4: if the no-callsite bucket is entirely hot-path
	# findings (Slow Hot Path / Hook Bottleneck / Slow Frontend Render),
	# re-bucket it under the "Request hotspots" label. Mixed buckets
	# keep the "Other (no callsite)" label so we're never misleading
	# about what's inside.
	no_callsite = buckets.get(_OTHER_APP_LABEL)
	if no_callsite and all(
		f.get("finding_type") in _HOTPATH_FINDING_TYPES
		for f in no_callsite
	):
		buckets[_HOTPATH_BUCKET_LABEL] = buckets.pop(_OTHER_APP_LABEL)

	# Preserve tracked-apps ordering at the top.
	seen = set()
	ordered: list[str] = []
	for app in tracked_apps:
		if app in buckets and app not in seen:
			ordered.append(app)
			seen.add(app)

	# Remaining apps sorted by total impact (most painful first),
	# then alphabetically for stable ordering when impacts tie.
	def _impact(app: str) -> float:
		return sum(f.get("estimated_impact_ms") or 0 for f in buckets[app])

	_TAIL_BUCKETS = (_OTHER_APP_LABEL, _HOTPATH_BUCKET_LABEL)
	remainder = [
		a for a in buckets
		if a not in seen and a not in _TAIL_BUCKETS
	]
	remainder.sort(key=lambda a: (-_impact(a), a))
	ordered.extend(remainder)

	# Tail buckets always last — user-app findings come first.
	# Hotspots before Other because they're at least typed.
	if _HOTPATH_BUCKET_LABEL in buckets:
		ordered.append(_HOTPATH_BUCKET_LABEL)
	if _OTHER_APP_LABEL in buckets:
		ordered.append(_OTHER_APP_LABEL)

	out = []
	for app in ordered:
		bucket_findings = buckets[app]
		# Findings inside each bucket keep severity/impact ordering
		# from the caller (they've already been sorted globally).
		out.append({
			"app": app,
			"findings": bucket_findings,
			"count": len(bucket_findings),
			"total_impact_ms": sum(
				f.get("estimated_impact_ms") or 0 for f in bucket_findings
			),
		})
	return out


def _now_iso() -> str:
	from datetime import datetime

	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_duration_ms(ms, threshold_ms: float = 1000.0, decimals: int = 0) -> str:
	"""Render a duration as ``"<n>ms"`` (with ``decimals`` digits) — or, if it
	crosses ``threshold_ms``, as ``"<n.nn>s"`` (always 2 decimals). The
	``decimals`` arg controls only the ms branch so the existing ``%.1f`` /
	``%.2f`` callsites (sub-ms query timings) keep their resolution below the
	threshold. ``threshold_ms = 0`` disables the conversion.

	Defensive on input: ``None`` / non-numeric → ``"0ms"``; honours sign.
	"""
	try:
		v = float(ms) if ms is not None else 0.0
	except (TypeError, ValueError):
		return "0ms"
	if threshold_ms and abs(v) >= threshold_ms:
		return f"{v / 1000:.2f}s"
	return f"{v:.{decimals}f}ms"


def _format_datetime_display(value) -> str:
	"""Format a datetime (or datetime-string) for display in the report using
	the site's System Settings (Date Format + Time Format) — which also drops
	the microseconds. Falls back to the value with any trailing microseconds
	stripped when Frappe isn't available (standalone / tests)."""
	if not value:
		return ""
	try:
		from frappe.utils import format_datetime

		return format_datetime(value)
	except Exception:
		return re.sub(r"\.\d+", "", str(value))


def _get_server_timezone() -> str:
	"""Return a human-readable server timezone label.

	Tries frappe's system settings first (more accurate than Python's
	local tz guess). Falls back to the Python datetime tzname.
	"""
	try:
		import frappe

		tz = frappe.db.get_single_value("System Settings", "time_zone")
		if tz:
			return str(tz)
	except Exception:
		pass
	try:
		from datetime import datetime

		name = datetime.now().astimezone().tzname()
		if name:
			return name
	except Exception:
		pass
	return "UTC"


# ---------------------------------------------------------------------------
# v0.3.0: call tree, donut, and hot frames helpers
# ---------------------------------------------------------------------------

HARDCODED_ALLOWED_PREFIXES = ("frappe.", "erpnext.", "payments.", "hrms.")


# v0.5.2: finding types that carry a concrete, user-actionable fix.
# These render in the main "Findings — what to fix" section.
# Everything else (framework-level, system-level, informational)
# renders in a separate "Observations" section below so the action
# list stays tight.
#
# Rule of thumb: if the customer_description ends with a specific
# next step the user can ship in a single PR (add THIS index,
# refactor THIS loop, trim THIS response), it belongs here. If the
# finding is an observation about the system or framework where the
# user has no direct code change to make, it's an Observation.
_ACTIONABLE_FINDING_TYPES = frozenset({
	# SQL — all have concrete DDL / refactor guidance
	"N+1 Query",
	"Missing Index",
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
	"Slow Query",
	# Python hot paths in user code
	"Slow Hot Path",       # narrowed by call_tree filter to user frames
	"Hook Bottleneck",     # user's own doc-event hook is slow
	"Redundant Call",      # v0.5.2: framework callsites already filtered
	# Frontend — user can trim responses / optimize JS
	"Slow Frontend Render",
	"Heavy Response",
	# v0.6.0 phase-2 line profiler
	"Hot Line",            # one source line concentrates the function's time
})
# Observation-only finding types (informational, no direct fix):
#   Framework N+1            — loop inside frappe/*
#   Repeated Hot Frame       — function repeated across actions; needs
#                               investigation, not a shippable fix
#   Resource Contention      — system CPU sustained high
#   Memory Pressure          — worker RSS growth / swap
#   DB Pool Saturation       — infra-level
#   Background Queue Backlog — infra-level
#   Network Overhead         — client/proxy territory, not user code


def redact_frame_name(node: dict) -> str:
	"""Build a tree node's display name. Always emits the full function
	name plus its short filename and line number — single admin-scoped
	report has no need for the safe-mode app collapse this used to do.
	"""
	if not isinstance(node, dict):
		return "<unknown>"

	function = node.get("function") or "<unknown>"
	filename = node.get("filename") or ""
	lineno = node.get("lineno") or 0

	short_file = filename.split("/")[-1] if filename else "?"
	return f"{function} ({short_file}:{lineno})"


# Donut color palette (8 colors; rolls over for more).
_DONUT_COLORS = [
	"#ff6b6b", "#4ecdc4", "#ffd93d", "#6c5ce7",
	"#a8e6cf", "#ff8b94", "#95e1d3", "#ffaaa5",
]


def build_donut_data(breakdown: dict) -> list:
	"""Convert session_time_breakdown_json into ordered (label, ms, color) tuples.

	v0.5.1: hides slices that round to 0ms in display. A session with
	148ms of SQL and only a handful of sub-ms Python self-times was
	rendering seven "Python (…) — 0ms" entries, all noise.
	"""
	if not breakdown:
		return []

	DONUT_DISPLAY_MIN_MS = 1.0

	slices = []
	sql_ms = breakdown.get("sql_ms", 0)
	if sql_ms >= DONUT_DISPLAY_MIN_MS:
		slices.append(("SQL", sql_ms, _DONUT_COLORS[0]))

	by_app = breakdown.get("by_app", {})
	for app, ms in by_app.items():
		if ms < DONUT_DISPLAY_MIN_MS:
			continue
		color = _DONUT_COLORS[(len(slices)) % len(_DONUT_COLORS)]
		slices.append((f"Python ({app})", ms, color))

	return slices


def build_donut_svg(slices: list) -> str:
	"""Render the donut as an inline SVG pie for PDF mode.

	wkhtmltopdf does not handle conic-gradient reliably; this SVG
	fallback always renders correctly. Each slice becomes a <path>
	element with a precomputed arc.
	"""
	if not slices:
		return ""
	import math

	total = sum(s[1] for s in slices) or 1
	cx, cy, r = 80, 80, 70
	parts = ['<svg width="160" height="160" xmlns="http://www.w3.org/2000/svg">']
	angle_start = -math.pi / 2  # start at 12 o'clock

	for _label, ms, color in slices:
		fraction = ms / total
		angle_end = angle_start + fraction * 2 * math.pi
		x1 = cx + r * math.cos(angle_start)
		y1 = cy + r * math.sin(angle_start)
		x2 = cx + r * math.cos(angle_end)
		y2 = cy + r * math.sin(angle_end)
		large_arc = 1 if fraction > 0.5 else 0
		path = (
			f'<path d="M {cx} {cy} L {x1:.1f} {y1:.1f} '
			f'A {r} {r} 0 {large_arc} 1 {x2:.1f} {y2:.1f} Z" '
			f'fill="{color}" stroke="#fff" stroke-width="1"/>'
		)
		parts.append(path)
		angle_start = angle_end

	parts.append("</svg>")
	return "".join(parts)


def build_hot_frames_table(rows: list) -> list:
	"""Build the hot-frames leaderboard rows."""
	out = []
	for row in rows or []:
		display = redact_frame_name(
			{"function": row.get("function"), "filename": "", "lineno": 0},
		)
		out.append({
			"display_name": display,
			"total_ms": row.get("total_ms", 0),
			"occurrences": row.get("occurrences", 0),
			"distinct_actions": row.get("distinct_actions", 0),
			"action_refs": row.get("action_refs", []),
		})
	return out
