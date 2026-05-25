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
from markupsafe import Markup

from optimus.analyzers.base import SEVERITY_ORDER

# Sensitive-data redaction lives in ``optimus/redaction.py`` (pure
# functions, no Frappe imports) so the recorder-patch path in
# ``optimus/__init__.py`` can run them at CAPTURE time — before raw
# values reach Redis or the persisted DocType JSON. The renderer calls
# them as defense-in-depth (catches older sessions written under the
# pre-patch contract, plus any code path where the patch didn't fire).
# Settings-driven extras: ``OptimusConfig.sensitive_sql_columns`` /
# ``sensitive_form_keys`` (additive — never replaces the defaults).
from optimus.redaction import redact_call_queries as _redact_call_queries_base
from optimus.redaction import redact_sensitive as _redact_sensitive_base
from optimus.redaction import redact_sql_literals as _redact_sql_literals_base

# v0.10.0+: Pygments syntax highlighting + the diff-block helpers moved to
# ``optimus/renderer/syntax.py``. They're imported here under the same
# names so existing call sites in this file resolve unchanged. The lazy
# import of Pygments still happens on first call to ``_ensure_pygments``,
# so the import-time cost story is preserved.
from optimus.renderer.syntax import (
	_ensure_pygments,
	_highlight_all_snippets,
	_highlight_diff_html,
	_highlight_python_block_cached,
	_highlight_python_snippet,
)


def _settings_extras() -> tuple[tuple[str, ...], tuple[str, ...]]:
	"""Read the live ``sensitive_form_keys`` / ``sensitive_sql_columns``
	extras from Optimus Settings. Best-effort — falls back to empty
	tuples on any error so a settings hiccup never breaks rendering
	(the defaults inside ``optimus.redaction`` still apply)."""
	try:
		from optimus.settings import get_config

		cfg = get_config()
		return (
			tuple(getattr(cfg, "sensitive_form_keys", ()) or ()),
			tuple(getattr(cfg, "sensitive_sql_columns", ()) or ()),
		)
	except Exception:
		return ((), ())


def _redact_sensitive(payload):
	"""Backward-compatible wrapper kept for any internal callers that
	don't have direct settings access. Reads the live extras list and
	delegates to ``optimus.redaction.redact_sensitive``."""
	extra_keys, _ = _settings_extras()
	return _redact_sensitive_base(payload, extra_keys=extra_keys)


def _redact_sql_literals(sql_str: str) -> str:
	"""Backward-compatible wrapper. Same as ``_redact_sensitive`` —
	reads settings + delegates."""
	_, extra_cols = _settings_extras()
	return _redact_sql_literals_base(sql_str, extra_columns=extra_cols)


def _redact_call_queries(calls) -> None:
	"""Backward-compatible wrapper. Mutates ``calls`` in place via
	``optimus.redaction.redact_call_queries`` with the live extras."""
	_, extra_cols = _settings_extras()
	_redact_call_queries_base(calls, extra_columns=extra_cols)


# v0.10.0+: source-file I/O + the bounded LRU cache moved to
# optimus/renderer/source.py. The imports below re-introduce them under
# the same names so the legacy call sites in this file resolve unchanged.
# v0.12.8+: call-tree rendering moved to its own submodule. The names
# are re-imported here so existing call sites inside ``_internal.py``
# (and the package ``__init__.py``'s dir-walk re-export) keep resolving
# unchanged.
from optimus.renderer.call_tree_renderer import (
	_CALL_TREE_HARD_CAP,
	_CALL_TREE_MAX_DEPTH,
	_CT_OTHER_RE,
	_ct_is_other_frame,
	_ct_is_sql_leaf,
	_ct_is_user_frame,
	_render_call_tree_node,
	_render_call_tree_panel,
)

# v0.12.10+: doc-event lifecycle binding + per-DocType breakdown moved
# to its own submodule. Same shim pattern.
from optimus.renderer.doc_event_renderer import (
	_KIND_CONTROLLER_OVERRIDE,
	_KIND_DOC_EVENTS_HOOK,
	_LIFECYCLE_EVENTS,
	_SEVERITY_RANK,
	_attach_action_context,
	_build_doc_event_breakdown,
	_build_doc_event_hook_index,
	_doc_event_hook_index,
	_doctype_from_controller_path,
	_extract_target_doc,
	_finding_hook_events,
	_finding_lifecycle_bindings,
	_module_from_filename,
)

# v0.12.16+: finding-enrichment phase 1 (low-coupling subset) moved
# to its own submodule. Re-imported here so call sites resolve
# unchanged. The HIGH-coupling subset (_finding_to_dict family +
# AST walker + chain attachers) stays in _internal.py pending a
# focused future PR with proper coupling-graph design.
from optimus.renderer.finding_enrichment import (
	_GROUPING_SEVERITY_RANK,
	_group_findings_by_root_cause,
	_normalize_callsite,
	_root_cause_key,
)

# v0.12.12+: Line-Level Drilldown panel + per-function tables moved
# to its own submodule. ``_build_line_drilldown_callsite_index`` is
# semi-public (analyze.py calls it via ``renderer.X`` through the
# package shim); the rest are internal helpers re-imported so legacy
# resolvers keep working.
from optimus.renderer.line_drilldown import (
	_build_line_drilldown_callsite_index,
	_build_phase2_callsite_index,
	_make_line_drilldown_lookup,
	_make_phase2_lookup,
	_phase2_invoked,
	_render_line_drilldown_panel,
	_render_phase2_diff_table,
	_render_phase2_function_table,
	_render_phase2_panel,
)
from optimus.renderer.source import (
	_FILE_CACHE_MAX_ENTRIES,
	_SNIPPET_TRUNCATE_CHARS,
	_BoundedFileCache,
	_path_within_bench,
	_read_source_snippet,
	_read_source_window,
	_resolve_source_path,
)

# v0.10.0+: duration + datetime formatting helpers moved to
# optimus/renderer/time_format.py. Reintroduced under the same names.
from optimus.renderer.time_format import (
	_format_datetime_display,
	_format_duration_ms,
	_get_server_timezone,
)

# v0.10.0+: donut chart + hot-frames table + frame-name redaction moved to
# optimus/renderer/visualization.py. All four are PUBLIC (passed into the
# template context as helpers) so the package __init__.py also re-exports
# them — the imports here keep the symbols available at this module's
# namespace for callers that resolve via ``optimus.renderer._internal.X``.
from optimus.renderer.visualization import (
	_DONUT_COLORS,
	build_donut_data,
	build_donut_svg,
	build_hot_frames_table,
	redact_frame_name,
)

# v0.10.0+: this module lives at ``optimus/renderer/_internal.py``; the
# templates directory is ``optimus/templates/`` (one level up from the
# package). The pre-split path was ``os.path.dirname(__file__) + "/templates"``,
# which worked because the old monolithic ``renderer.py`` lived next to
# ``templates/``. Resolve via the parent dir so the package-aware path
# still points at the same on-disk template files.
_TEMPLATES_DIR = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


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
			rec_copy = dict(r)
			# Phase K hardening: scrub sensitive-keyed values from
			# form_dict + headers before they reach the rendered HTML.
			if isinstance(rec_copy.get("form_dict"), dict):
				rec_copy["form_dict"] = _redact_sensitive(rec_copy["form_dict"])
			if isinstance(rec_copy.get("headers"), dict):
				rec_copy["headers"] = _redact_sensitive(rec_copy["headers"])
			# Phase K hardening (v0.7 GA polish): best-effort SQL
			# parameter redaction over each call's query string. The
			# recorder bakes parameter values into the raw SQL; we
			# strip the literals in ``WHERE password = '...'`` shapes
			# so an exported report doesn't leak credentials.
			if isinstance(rec_copy.get("calls"), list):
				rec_copy["calls"] = [
					dict(c) if isinstance(c, dict) else c
					for c in rec_copy["calls"]
				]
				_redact_call_queries(rec_copy["calls"])
			recordings_by_uuid[uid] = rec_copy

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
	# shown under each row in the per-action breakdown and RQ Jobs
	# sections. One shared file-line cache so a cluster of actions in the
	# same source file reads it once. Done before build_background_jobs so
	# the job dicts can copy the resolved callsite off the action dict.
	_entry_src_cache: dict[str, list[str] | None] = {}
	for _a in actions:
		_a["entry_callsite"] = _action_entry_callsite(_a, cache=_entry_src_cache)
	# v0.6.0 Round 2: per-render file cache for the lazy snippet read in
	# _finding_to_dict. A session with a dozen findings clustered in 2-3
	# source files reads each file once instead of once per finding.
	# Phase K hardening: bounded LRU (cap 50 entries) so a session that
	# touches 100+ unique source files doesn't retain unbounded memory
	# until GC.
	_finding_file_cache = _BoundedFileCache()
	all_findings = [
		_finding_to_dict(f, _finding_file_cache)
		for f in (session_doc.findings or [])
	]
	# v0.6.x: SQL "red flag" findings carry no callsite — derive a
	# representative one (the hottest user-app frame that ran the offending
	# query) from the recordings so their smoking-gun block can render too.
	_attach_representative_callsites(all_findings, recordings or [], file_cache=_finding_file_cache)
	# v0.7.x: drop findings whose callsite has no file:line. They have
	# no actionable anchor for the reader — pre-v0.7 they bucketed as
	# "Other (no callsite)" and the user explicitly opted to suppress
	# that bucket. Filtered AFTER ``_attach_representative_callsites``
	# so SQL red-flag findings that DID get a representative callsite
	# survive. The filter is global — these findings also disappear
	# from the Executive Summary, severity counts, and observations
	# so there's no phantom-row inconsistency between sections.
	def _has_renderable_callsite(f):
		detail = f.get("technical_detail") or {}
		callsite = detail.get("callsite") or {}
		return bool((callsite.get("filename") or "").strip())

	all_findings = [f for f in all_findings if _has_renderable_callsite(f)]
	# v0.7.x: "X was picked but never invoked during phase 2" is non-actionable
	# noise — it just means the replay didn't exercise that pick. The Line-Level
	# Drilldown already notes uninvoked picks in one concise line. Drop these
	# globally at render so existing reports declutter on regenerate too (the
	# analyzer no longer emits them for new runs); global so they also leave the
	# severity counts / observations with no phantom rows.
	all_findings = [f for f in all_findings if f.get("finding_type") != "Function Not Invoked"]
	# v0.7.x: line-drilldown callsite index built once and reused for the
	# Jinja lookup the template uses for cross-link callouts ("Line-Level
	# Drilldown: hottest line N - Mms / X hits"). The smoking-gun
	# retargeting reads drill-down chains (not line-drilldown) and is
	# wired in after _attach_drilldown_chains populates them on the
	# findings.
	_line_drilldown_index = _build_line_drilldown_callsite_index(session_doc)
	# v0.6.x: which document each save/submit action touched, and which
	# doc-event lifecycle hook each slow function fired in.
	_attach_action_context(actions, all_findings, recordings_by_uuid)
	# v0.7.x: VSCode Dark+ syntax highlighting for every source_snippet
	# we'll render. Done once, in place, after all snippets are attached.
	# Adds a ``content_html`` field to each line dict; the template (or
	# the line-prof renderer) uses it via ``| safe`` with a graceful
	# fallback to plain ``content`` if Pygments failed.
	_highlight_all_snippets(actions, all_findings)
	# v0.7.x J.13: render-time em-dash sweep over analyzer-baked prose.
	# The analyzer wrote em dashes into customer_description, llm_fix HTML,
	# session.notes_html, and summary_html during analyse-time (pre-J.12).
	# Replace at render time so existing reports get the hyphen treatment
	# without requiring a Retry Analyze.
	def _strip_em(s):
		if isinstance(s, str) and "—" in s:
			return s.replace("—", "-")
		return s
	for _f in (all_findings or []):
		if not isinstance(_f, dict):
			continue
		_f["customer_description"] = _strip_em(_f.get("customer_description"))
		_f["title"] = _strip_em(_f.get("title"))
		_lf = _f.get("llm_fix")
		if isinstance(_lf, dict):
			for _k in ("diagnosis_html", "patch_html", "rationale_html", "verify_html", "description_html", "code_html", "why_html"):
				if _k in _lf:
					_lf[_k] = _strip_em(_lf[_k])
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

	# v0.7.x J.11: enrich finding callsite function names with the DocType
	# suffix for display. Runs AFTER _build_doc_event_breakdown because
	# that function parses ``callsite.function`` against the lifecycle
	# event vocabulary (``validate``, ``on_submit``, ...) — suffixing
	# before classification would break the match.
	for _f in (all_findings or []):
		if not isinstance(_f, dict):
			continue
		_detail = _f.get("technical_detail")
		if not isinstance(_detail, dict):
			continue
		_td = _detail.get("target_doc")
		_dt = _td.get("doctype") if isinstance(_td, dict) else None
		if not _dt:
			continue
		_cs = _detail.get("callsite")
		if isinstance(_cs, dict):
			_cs_fn = _cs.get("function") or ""
			if _cs_fn and " (" not in _cs_fn:
				_cs["function"] = f"{_cs_fn} ({_dt})"

	# v0.6.0: the "RQ Jobs" section — the captured background-job
	# recordings, surfaced on their own (they also stay in the per-action
	# table). Derived from the persisted action rows; uses all findings
	# (actionable + observational) for the per-job findings tally.
	background_jobs = build_background_jobs(
		actions, recordings_by_uuid, all_findings,
		tracked_jobs=getattr(session_doc, "background_jobs", None),
	)

	try:
		top_queries = json.loads(session_doc.top_queries_json or "[]")
	except Exception:
		top_queries = []
	# The slowest-queries leaderboard is user-app-only and skips
	# trivially-fast queries. New sessions are already filtered by the
	# top_queries analyzer; re-applying it here means sessions captured
	# before that change also get scoped down when regenerated.
	top_queries = _filter_top_queries_for_display(top_queries)
	# Phase K hardening: best-effort SQL parameter redaction over the
	# slow-queries leaderboard (mirrors the recordings-side redaction).
	_redact_call_queries(top_queries)
	# B.DI4 — compute slow-threshold + suppressed-finding count from the
	# loaded leaderboard. Render-time so it picks up the latest Settings
	# value without needing analyze to re-run.
	from optimus.analyzers.top_queries import (
		_resolve_slow_query_threshold as _resolve_slow_q_threshold,
	)
	from optimus.analyzers.top_queries import (
		count_suppressed_findings as _count_suppressed_slow_findings,
	)
	try:
		_top_queries_slow_threshold_ms = float(_resolve_slow_q_threshold()[0])
	except Exception:
		_top_queries_slow_threshold_ms = 200.0
	_top_queries_suppressed_count = _count_suppressed_slow_findings(
		top_queries, _top_queries_slow_threshold_ms
	)
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
			# v0.7.x: which Sensitivity Profile drove the thresholds for this render.
			"config_profile": getattr(_cfg, "config_profile", "Custom"),
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
			"config_profile": "Custom",
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
			# user input - safe by default.
			import html as html_mod
			notes_html = html_mod.escape(notes_html)
		# v0.7.x J.13: strip em dashes the analyzer wrote into auto-notes
		# / humanized-notes prose at analyse-time.
		notes_html = notes_html.replace("—", "-")

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
	# v0.7.x: self-time hot paths with no deeper user frame show the whole
	# function body (Phase-1 can't pinpoint the line; the function is the unit).
	_expand_self_time_snippets(all_findings, file_cache=_finding_file_cache)
	# v0.7.x: re-anchor each finding's smoking-gun snippet on the deepest
	# user-code frame of its drill-down chain (when the chain points at a
	# different function than the wrapper). Must run AFTER
	# _attach_drilldown_chains so the chains exist; the bucketed views
	# below share these dicts by reference so the new callsite is visible
	# everywhere the finding renders.
	_retarget_phase1_callsites_to_drilldown_leaf(
		all_findings, file_cache=_finding_file_cache,
	)
	# v0.7.x: collapse findings that share a deepest-user-code anchor
	# into one primary card with the others attached as sub_findings.
	# Runs AFTER smoking-gun retargeting + drill-down attachment so
	# the root-cause key resolution can use either source. The
	# actionable bucket and the observational bucket are grouped
	# independently (don't merge an observational into an actionable
	# group's sub-list or vice versa). Severity counts and the
	# Executive Summary downstream then see the de-duplicated list.
	findings = _group_findings_by_root_cause(findings)
	actionable_findings = findings
	observational_findings = _group_findings_by_root_cause(observational_findings)
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
	#
	# v0.7.x: the per-action split now keys off the admin's
	# ``Tracked Apps`` allowlist exclusively. When Tracked Apps is
	# configured, only actions whose entry resolves to a tracked app
	# land in the main table — everything else is in the collapsed
	# framework subsection. When Tracked Apps is empty (default), the
	# split is skipped and ALL actions stay in the main table. This
	# fixes a UX regression where every HTTP action that hit a Frappe
	# endpoint (``/api/method/frappe.client.save``,
	# ``/api/method/frappe.desk.form.save.savedocs`` — i.e. the actions
	# the user actually clicked Save / Submit for) was hidden in the
	# framework subsection, leaving only background jobs in the main
	# Per-Action Breakdown table.
	if tracked_apps:
		actions, actions_framework = _split_by_framework_app(
			actions,
			lambda a: (a.get("entry_callsite") or {}).get("_abs") or _action_dotted_entry(a),
			tracked_apps,
		)
	else:
		actions_framework = []
	# v0.7.x: link each action to the findings whose ``action_ref``
	# matches its ``idx``. The per-action row in the template uses
	# this to embed the full finding card (severity badge, smoking
	# gun, drill-down, AI fix, root-cause sub_findings) directly
	# inside the action's sub-row — same structure as the Findings
	# section, scoped to that action. Findings without an action_ref
	# (e.g. infra observations, SQL red flags) are skipped — they
	# still appear in their respective top-level sections.
	_findings_by_action_ref: dict[str, list] = {}
	for _f in actionable_findings:
		_ref = str(_f.get("action_ref") or "").strip()
		if _ref:
			_findings_by_action_ref.setdefault(_ref, []).append(_f)
	for _a in list(actions) + list(actions_framework):
		_a["related_findings"] = _findings_by_action_ref.get(
			str(_a.get("idx", "")), [],
		)
	# v0.7.x: same linkage for RQ Jobs rows. Jobs carry the
	# original action ``idx`` (set in ``build_background_jobs``) so
	# they look up against the same per-action-ref map. With this
	# the BG row macro embeds the same finding cards as action_row,
	# scoped to the job.
	for _job in (
		list(background_jobs.get("jobs", []) or [])
		+ list(background_jobs.get("jobs_framework", []) or [])
	):
		_job["related_findings"] = _findings_by_action_ref.get(
			str(_job.get("idx", "")), [],
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
	hot_frames_rows = build_hot_frames_table(_hf_raw_custom, is_hot=True)
	hot_frames_rows_framework = build_hot_frames_table(_hf_raw_framework, is_hot=False)

	# v0.5.2 round 3: executive summary — top 3 most-impactful findings
	# stated in plain English, rendered in a card at the top of the
	# report. A non-developer (e.g. a project manager) reading this
	# should be able to decide "do we have a problem" in 30 seconds
	# without scrolling past the first screen.
	# v0.7.x Phase H: the v0.5.2 `executive_summary` data layer was
	# orphaned after the TL;DR hero (Phase B) + Action plan (Phase C)
	# took over its on-page responsibilities. The function is left in
	# the module for back-compat (existing test fixtures call it
	# directly) but is no longer wired into render_raw's context.
	tldr = _compose_tldr(
		all_findings,
		session_doc,
		large_duration_threshold_ms=_large_duration_threshold_ms,
		actions=list(actions) + list(actions_framework),
	)
	# Phase K.5: nested-<details> call-tree panel for the slowest
	# action. Empty string when no action carries a call_tree_json.
	call_tree_html = _render_call_tree_panel(list(actions) + list(actions_framework))
	# B.DI2 — aggregate frame-truncation across actions so the Hot Frames
	# banner can show "captured X frames, only top N shown" without making
	# the reader hunt through analyzer_warnings.
	frame_truncation = _aggregate_frame_truncation(
		list(actions) + list(actions_framework)
	)
	# v0.7.x redesign Phase C: Recommended Action plan + waterfall.
	# Action plan: top-3 highest-impact findings, verb-led titles.
	# Waterfall: top-8 actions by duration, horizontal bars scaled
	# to the displayed slice's max so short actions stay visible.
	action_plan = _build_action_plan(
		all_findings,
		large_duration_threshold_ms=_large_duration_threshold_ms,
	)
	# Waterfall spans both tracked-apps + framework actions; the
	# split below is for the per-action breakdown table. The reader
	# wants to see ALL slow actions in the timeline, framework
	# included.
	waterfall_rows = _build_waterfall(
		list(actions) + list(actions_framework),
		all_findings,
		large_duration_threshold_ms=_large_duration_threshold_ms,
	)

	# v0.7.x: build the Summary section's HTML at render time. Pre-v0.7.x
	# this was baked into ``session_doc.summary_html`` at analyze time, so
	# template-shape changes (e.g. <p> → <ul>) only applied to sessions
	# re-analyzed after the change — ``regenerate_reports`` re-renders the
	# template but doesn't re-run analyzers (see the docstring on
	# ``_filter_top_queries_for_display`` below for the same pattern).
	# Building at render time means the bullet shape always matches the
	# current code, on every regenerate. The analyze-time write to
	# ``summary_html`` still happens for backwards compatibility but the
	# template now ignores it.
	from optimus.analyze import _build_summary_html
	from optimus.analyzers.base import AnalyzeContext as _SummaryCtx
	_summary_ctx = _SummaryCtx(
		session_uuid=getattr(session_doc, "session_uuid", "") or "",
		docname=getattr(session_doc, "name", "") or "",
	)
	_summary_ctx.actions = list(actions) + list(actions_framework)
	_summary_ctx.findings = all_findings
	summary_html_rendered = _build_summary_html(
		_summary_ctx,
		int(getattr(session_doc, "total_queries", 0) or 0),
		recordings,
	)
	# v0.7.x J.13: strip em dashes from the render-time summary HTML
	# (analyze.py's prose composer may still produce them on cached doc rows).
	if summary_html_rendered:
		summary_html_rendered = summary_html_rendered.replace("—", "-")

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
		# B.DI4 — surfaced through to report_data so the template can
		# render a "X more slow queries suppressed" banner when the 5-cap
		# clipped legitimate findings out of the list.
		"top_queries_suppressed_count": _top_queries_suppressed_count,
		"top_queries_slow_threshold_ms": _top_queries_slow_threshold_ms,
		# B.DI2 — frame-truncation banner data for the Hot Frames section.
		"frame_truncation": frame_truncation,
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
		# v0.7.x J.16 (renamed from phase2_html): the Line-Level Drilldown
		# panel pre-rendered server-side so the template only needs a
		# single ``{{ line_drilldown_html | safe }}`` include instead of
		# growing by 100+ lines of new markup.
		"line_drilldown_html": _render_line_drilldown_panel(session_doc),
		# v0.7.x J.16 (renamed from phase2_for_callsite): cross-link a
		# finding's callsite to its hottest line-drilldown line when the
		# same function was instrumented. Helper rather than raw dict
		# because Jinja can't build tuple keys for the basename +
		# function lookup.
		"line_drilldown_for_callsite": _make_line_drilldown_lookup(_line_drilldown_index),
		# v0.6.x: snapshot of the render-affecting settings, stamped in the
		# report footer so a user opening a saved HTML file can immediately
		# tell which toggles were in effect when it was rendered. (Saved
		# files are static; Optimus Settings changes only affect future
		# renders.)
		"render_config": render_config,
		# v0.7.x: render-time-built Summary HTML (see note above the
		# ``_build_summary_html`` call). The template prefers this over
		# the stored ``session.summary_html``.
		"summary_html": summary_html_rendered,
		# v0.7.x redesign Phase B: TL;DR hero — single composed headline
		# keyed off the highest-impact finding, with `<span class="hot">`
		# inline emphases (rendered as Markup so Jinja autoescape leaves
		# them intact).
		"tldr": tldr,
		# Phase K.5: nested-<details> call-tree panel for the slowest
		# action. Empty string when no action carries a call_tree_json;
		# template ``{% if %}`` guards the section.
		"call_tree_html": call_tree_html,
		# v0.7.x redesign Phase C: top-3 verb-led action plan steps.
		# Empty list → template hides the section.
		"action_plan": action_plan,
		# v0.7.x redesign Phase C: top-8 actions by duration, scaled to
		# the displayed slice's max. Empty list → template hides.
		"waterfall_rows": waterfall_rows,
	}

	# v0.7.x Phase J.1 — contract-shape adapter. Exposes the 19-key dict
	# per template_variable_contract.md under a single ``report_data``
	# namespace; Phase J.2 migrated every template section to read from
	# it instead of the flat legacy keys.
	from optimus.report_context import build_report_context as _build_report_context
	context["report_data"] = _build_report_context(session_doc, context)

	# v0.7.x Phase J.3 — drop the now-unused legacy top-level keys.
	# Template grep confirms zero remaining references; the adapter has
	# already consumed them (the pop runs AFTER build_report_context).
	# Keys kept: session, fmt_dt/fmt_ms, generated_at/server_tz, severity_counts,
	# ignored_apps, ignored_findings_count, hidden_db_tables_count, donut_*,
	# truncation_banner, analyzer_warnings, recordings_by_uuid,
	# line_drilldown_for_callsite, redact_frame_name, from_json - all
	# directly used by template markup or finding_card cross-link.
	for _legacy_key in (
		"notes_html", "summary_html", "line_drilldown_html",
		"waterfall_rows",
		"hot_frames_rows", "hot_frames_rows_framework",
		"top_queries", "top_queries_framework",
		"top_queries_suppressed_count", "top_queries_slow_threshold_ms",
		"frame_truncation",
		"table_breakdown",
		"infra_summary", "infra_timeline",
		"frontend_vitals_by_page", "frontend_xhr_matched",
		"frontend_orphans", "frontend_summary",
		"doc_event_breakdown",
		"actions", "actions_framework",
		"background_jobs",
		"findings", "findings_by_app",
		"observational_findings", "observational_findings_by_app",
		"all_findings",
		"tldr", "action_plan",
		"render_config",
	):
		context.pop(_legacy_key, None)

	return template.render(**context)


def _e(text: object) -> str:
	"""HTML-escape — small alias to keep the phase-2 builder readable."""
	import html as _html
	return _html.escape("" if text is None else str(text))


# v0.12.12+: _build_line_drilldown_callsite_index / _make_line_drilldown_lookup
# (and their pre-v0.7.x aliases _build_phase2_callsite_index / _make_phase2_lookup)
# moved to optimus/renderer/line_drilldown.py. Re-imported at the top of
# this module.


def _find_call_line_in_function_body(
	parent_filename: str,
	parent_def_lineno: int,
	callee_function: str,
	*,
	file_cache: dict | None = None,
) -> int | None:
	"""Return the lineno of the first call to ``callee_function`` inside
	the function whose ``def`` begins at ``parent_def_lineno`` in
	``parent_filename``. ``None`` if the source can't be read or no call
	is found.

	AST primary — locates the matching ``FunctionDef`` / ``AsyncFunctionDef``
	node by name/lineno, then walks its body for ``Call`` expressions
	whose target resolves to ``callee_function`` (matches both bare
	``callee_function(...)`` via ``Name`` and ``obj.callee_function(...)``
	via ``Attribute``).

	Regex fallback when AST parse fails (truncated file, syntax error
	elsewhere) — scans lines below the def for ``\\b<callee>\\s*\\(``
	stopping at a same-or-lower indented ``def `` / ``class `` /
	``async def `` or after 200 lines.

	Reused across multiple findings in the same file via ``file_cache``
	(the same per-render cache passed to ``_read_source_snippet``).
	"""
	if not parent_filename or not callee_function or not parent_def_lineno:
		return None

	# Read source through the shared cache so files visited by
	# _read_source_snippet aren't re-read here.
	if file_cache is not None and parent_filename in file_cache:
		lines = file_cache[parent_filename]
	else:
		resolved = _resolve_source_path(parent_filename)
		try:
			with open(resolved, encoding="utf-8") as fh:
				lines = fh.read().splitlines()
		except Exception:
			lines = None
		if file_cache is not None:
			file_cache[parent_filename] = lines

	if not lines:
		return None
	source = "\n".join(lines)

	# Strategy 1: AST parse.
	try:
		import ast as _ast
		tree = _ast.parse(source, filename=parent_filename)
	except Exception:
		tree = None

	if tree is not None:
		# Find the function def closest to parent_def_lineno (defensive
		# against the parent being a nested function — pick by name match
		# AND minimum lineno distance).
		candidates: list[_ast.AST] = []
		for node in _ast.walk(tree):
			if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
				candidates.append(node)
		if candidates:
			# Filter by lineno proximity to the recorded def lineno (within
			# a few lines is usually enough — pyinstrument's lineno can
			# be the def or the first executed line of the body).
			best = min(
				candidates,
				key=lambda n: abs((n.lineno or 0) - parent_def_lineno),
			)
			# Walk only inside the chosen function; collect Call linenos
			# whose func name matches.
			matches: list[int] = []
			for sub in _ast.walk(best):
				if not isinstance(sub, _ast.Call):
					continue
				func = sub.func
				name = None
				if isinstance(func, _ast.Name):
					name = func.id
				elif isinstance(func, _ast.Attribute):
					name = func.attr
				if name == callee_function and sub.lineno:
					matches.append(sub.lineno)
			if matches:
				return min(matches)

	# Strategy 2: regex fallback. Scan from parent_def_lineno+1 until
	# a sibling def/class at the parent's indentation level or 200
	# lines, whichever comes first. Stop at first match.
	import re as _re
	call_re = _re.compile(r"\b" + _re.escape(callee_function) + r"\s*\(")
	# Parent's indentation level (number of leading spaces / tabs).
	if not (1 <= parent_def_lineno <= len(lines)):
		return None
	parent_line = lines[parent_def_lineno - 1]
	parent_indent = len(parent_line) - len(parent_line.lstrip())
	# Body must be MORE indented than the parent def.
	sentinel_re = _re.compile(
		r"^(?P<indent>\s*)(?:async\s+def|def|class)\s",
	)
	max_scan = min(len(lines), parent_def_lineno + 200)
	for i in range(parent_def_lineno, max_scan):
		raw = lines[i]
		# Strip trailing # comments naively (string-aware comment
		# stripping is overkill here — the worst case is a false
		# negative which falls back to the def-line behavior).
		hash_idx = raw.find("#")
		body = raw if hash_idx < 0 else raw[:hash_idx]
		stripped = body.lstrip()
		if not stripped:
			continue
		current_indent = len(body) - len(stripped)
		# Sibling def/class at parent's level (or shallower) → end of body.
		sentinel_match = sentinel_re.match(body)
		if sentinel_match and current_indent <= parent_indent:
			break
		if current_indent <= parent_indent:
			# We've exited the parent function's block.
			break
		if call_re.search(body):
			return i + 1  # convert 0-based index back to lineno

	return None


def _retarget_phase1_callsites_to_drilldown_leaf(
	findings: list[dict],
	file_cache: dict | None = None,
) -> None:
	"""Re-aim phase-1 finding callsites at the **call site** of the
	deepest user-code frame in their drill-down chain, in place.

	A Slow Hot Path / Hook Bottleneck / Repeated Hot Frame finding's
	default callsite is its **wrapper's** entry frame (the ``def`` line
	of the function at which the slow path was first flagged, e.g.
	``looped_validate:6``). The drill-down chain already walks down to
	the deepest user-code frame (e.g. ``_check_user_exists:18``), but
	even that frame's ``def`` line is a function header rather than the
	expensive call. The reader's eye lands on the most actionable info
	when the snippet shows the **call expression** for the deepest
	leaf — typically the line inside the **parent** of the deepest
	frame that invokes it. For the user's case the chain is
	``looped_validate → _run_validations → _check_user_exists`` and
	the relevant line is ``    _check_user_exists(doc)`` at
	``_run_validations:13``.

	Phase-1 only — no phase-2 dependency. The drill-down chain is
	gated on ``action_ref + tree + walk produces ≥1 step`` and is
	already populated by ``_attach_drilldown_chains`` (called earlier
	in the render flow). Most Slow Hot Path findings carry one;
	findings without a chain are left as-is.

	Hot Line / Function Not Invoked findings are phase-2 native — their
	callsite is already at the leaf — so they're skipped. SQL "red
	flag" findings whose callsite is a representative one (a query hot
	spot) are skipped too: their snippet should keep pointing at the
	query line.

	The original wrapper location is preserved on the new callsite as
	``original_wrapper`` (``{filename, lineno, function}``) so the
	template can render a "Time entered through …" caption beneath
	the relocated callsite header.

	Fallback chain when the call site can't be located:
	  1. The parent's body is parsed via AST; if a matching ``Call`` is
	     found, anchor on its lineno.
	  2. A regex fallback scans the parent's body for ``<callee>(``.
	  3. If neither finds the call line, fall back to the leaf's own
	     ``def`` lineno (previous behavior — still better than the
	     wrapper's def line).
	"""
	if not findings:
		return

	for finding in findings:
		ftype = finding.get("finding_type") or ""
		if ftype in {"Hot Line", "Function Not Invoked"}:
			continue
		detail = finding.get("technical_detail")
		if not isinstance(detail, dict):
			continue
		chain = detail.get("drilldown_chain") or []
		if not chain:
			continue
		callsite = detail.get("callsite") or {}
		# Skip representative SQL callsites — they're query hot spots,
		# not function frames; re-targeting them would lose the query
		# line they exist to point at.
		if callsite.get("is_representative"):
			continue
		fname = callsite.get("filename") or ""
		fn_name = callsite.get("function") or ""
		if not fn_name:
			continue
		leaf = chain[-1] if isinstance(chain[-1], dict) else None
		if not leaf:
			continue
		leaf_function = leaf.get("function") or ""
		leaf_lineno = leaf.get("lineno")
		leaf_filename = leaf.get("filename") or ""
		if not leaf_function or leaf_lineno is None or not leaf_filename:
			continue
		# Single-frame chain (or leaf == wrapper) — nothing to relocate.
		if leaf_function == fn_name:
			continue

		# Determine the leaf's caller in the chain: chain[-2] when the
		# chain has an intermediate, else the wrapper (the finding's
		# original callsite).
		if len(chain) >= 2 and isinstance(chain[-2], dict):
			parent_filename = chain[-2].get("filename") or ""
			parent_lineno = int(chain[-2].get("lineno") or 0)
			parent_function = chain[-2].get("function") or ""
		else:
			parent_filename = fname
			parent_lineno = int(callsite.get("lineno") or 0)
			parent_function = fn_name

		call_lineno = _find_call_line_in_function_body(
			parent_filename, parent_lineno, leaf_function,
			file_cache=file_cache,
		)

		# Choose the anchor based on what we found:
		#   - call site in the parent → ``(parent_filename, call_lineno, parent_function)``
		#   - fallback: leaf's own def line ``(leaf_filename, leaf_lineno, leaf_function)``
		if call_lineno is not None:
			anchor_filename = parent_filename
			anchor_lineno = call_lineno
			anchor_function = parent_function
		else:
			anchor_filename = leaf_filename
			anchor_lineno = leaf_lineno
			anchor_function = leaf_function

		snippet = _read_source_snippet(
			anchor_filename, anchor_lineno, cache=file_cache,
		)

		display_filename = (
			_bench_relative_display(anchor_filename)
			if anchor_filename.startswith("/")
			else anchor_filename
		)
		new_callsite = {
			"filename": display_filename,
			"_abs": (
				anchor_filename
				if anchor_filename.startswith("/")
				else callsite.get("_abs")
			),
			"lineno": anchor_lineno,
			"function": anchor_function,
			"source_snippet": snippet,
			"original_wrapper": {
				"filename": fname,
				"lineno": callsite.get("lineno"),
				"function": fn_name,
			},
			# v0.7.x: the Phase-2 cross-link callout's lookup keys on
			# ``(basename, function)`` — preserve the leaf's identity
			# here so the template can target the leaf's phase-2 hot
			# line even when the smoking-gun callsite was retargeted
			# to the parent's call site (different function name).
			"phase2_lookup_filename": leaf_filename,
			"phase2_lookup_function": leaf_function,
		}
		detail["callsite"] = new_callsite


# v0.12.16+: _GROUPING_SEVERITY_RANK / _root_cause_key /
# _group_findings_by_root_cause moved to
# optimus/renderer/finding_enrichment.py. Re-imported at the top of
# this module so call sites resolve unchanged.
# v0.12.12+: _phase2_invoked / _render_phase2_function_table /
# _render_phase2_diff_table / _render_line_drilldown_panel (+ alias
# _render_phase2_panel) moved to optimus/renderer/line_drilldown.py.
# Re-imported at the top of this module.

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
	"""Flatten a Optimus Action child row to a plain dict.

	v0.7.x J.12.1: normalise legacy ``event_type = "Background Job"`` to
	the new ``"RQ Job"`` label at read time so reports rendered from
	sessions captured before the J.12 rename still surface the RQ-Jobs
	section. New recordings already carry the new label.
	"""
	_event_type = child.event_type or ""
	if _event_type == "Background Job":
		_event_type = "RQ Job"
	_label = child.action_label or ""
	# Same normalisation for the analyzer-baked label prefix.
	if _label.startswith("Job: "):
		_label = "RQ " + _label
	# v0.7.x: BG-job recordings captured before per_action._label
	# learned the "RQ Job: <short>" form fall through to the HTTP
	# path and end up with action_label = "GET <dotted.python.path>".
	# When the action IS classified as an RQ Job (here, or via J.12
	# normalisation above), the leaked HTTP verb misleads the METHOD
	# column AND finding titles. Recover the canonical form at render
	# time from the path's last segment.
	if _event_type == "RQ Job" and not _label.startswith("RQ Job:"):
		_path = child.path or ""
		if _path:
			_short = _path.split(".")[-1] if "." in _path else _path
			_label = f"RQ Job: {_short}"
	return {
		"action_label": _label,
		"event_type": _event_type,
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
# v0.6.0: "RQ Jobs" report section — render-time only (the pipeline is
# frozen). Derived purely from the persisted Optimus Action rows that have
# event_type == "RQ Job" (one per captured job recording), enriched
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
		# Accept both the new ``"RQ Job: <method>"`` prefix and the legacy
		# ``"Job: <method>"`` from sessions captured before the J.12 rename.
		for prefix in ("RQ Job: ", "Job: "):
			if label.startswith(prefix):
				return label[len(prefix):].strip() or label
		return label
	p = (path or "").strip()
	if p:
		return p
	if recording:
		return (recording.get("cmd") or recording.get("path") or "").strip() or "RQ Job"
	return "RQ Job"


def _tracked_row_get(row, key, default=None):
	"""Read a key from a tracked-job row, tolerating both a plain dict (test
	fixtures) and a Frappe child Document (``session_doc.background_jobs``)."""
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def build_background_jobs(actions, recordings_by_uuid, findings=None, tracked_jobs=None) -> dict:
	"""Build the "RQ Jobs" section payload from the (already
	min-duration-filtered) action dicts, merged with the persisted per-job
	terminal-status rows so failed / timed-out / still-running jobs are
	reported instead of silently vanishing.

	``actions`` items are ``_action_to_dict`` output plus an ``idx`` key
	holding the action's original position (so findings — whose ``action_ref``
	is that index as a string — can be tallied per job). ``recordings_by_uuid``
	enriches each job with its slowest queries when the recording is still in
	Redis (TTL ~10 min; a re-render long after analyze has none → the section
	still renders from the persisted action rows alone). ``tracked_jobs`` are
	the ``Optimus Background Job`` child rows analyze persisted (one per RQ job
	the flow enqueued, carrying ``status`` / ``error`` / timing); they link to a
	captured action by ``recording_uuid``. A job that ran with profiling has
	both an action (rich query data) and a tracked row (status); a job that
	failed / timed out / ran past the wait has only a tracked row and still
	appears, with its status + error but no query data. Pure — no I/O (the
	``entry_callsite`` on each job is pre-computed by ``render()`` and copied
	through here).

	Returns ``{jobs, count, total_ms, total_queries, any_findings_counted,
	status_counts}``.
	"""
	findings_by_idx: dict[str, int] = {}
	for f in (findings or []):
		ref = (f.get("action_ref") or "").strip()
		if ref:
			findings_by_idx[ref] = findings_by_idx.get(ref, 0) + 1
	any_findings_counted = bool(findings_by_idx)

	tracked_list = list(tracked_jobs or [])
	tracked_by_uuid: dict[str, Any] = {}
	for row in tracked_list:
		ruuid = (_tracked_row_get(row, "recording_uuid") or "").strip()
		if ruuid:
			tracked_by_uuid[ruuid] = row

	jobs: list[dict] = []
	matched_uuids: set[str] = set()
	for a in (actions or []):
		if a.get("event_type") != "RQ Job":
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

		# A captured RQ-Job action ran and produced a recording → its terminal
		# status is Completed unless the tracked row says otherwise (older
		# sessions recorded before job-tracking have no tracked row).
		trow = tracked_by_uuid.get(uuid) if uuid else None
		if uuid:
			matched_uuids.add(uuid)

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
			# v0.7.x: keep the original action index so render() can
			# attach ``related_findings`` (the actual list of finding
			# objects, not just the count) for embedding in the row.
			"idx": idx,
			"status": (_tracked_row_get(trow, "status") or "Completed") if trow else "Completed",
			"error": _tracked_row_get(trow, "error") if trow else None,
		})

	# Append every tracked job that produced no captured action (failed, timed
	# out, still running, or below the action threshold): status + error +
	# timing, but no query data. The user requires that no enqueued job is
	# missed from the report.
	for row in tracked_list:
		ruuid = (_tracked_row_get(row, "recording_uuid") or "").strip()
		if ruuid and ruuid in matched_uuids:
			continue
		jobs.append({
			"method": _tracked_row_get(row, "method") or "RQ Job",
			"recording_uuid": ruuid,
			"duration_ms": _tracked_row_get(row, "duration_ms") or 0,
			"queries_count": 0,
			"query_time_ms": 0,
			"slowest_query_ms": 0,
			"findings_count": None,
			"top_queries": None,
			"recording_available": False,
			"entry_callsite": None,
			"idx": None,
			"status": _tracked_row_get(row, "status") or "Running",
			"error": _tracked_row_get(row, "error"),
		})

	jobs.sort(key=lambda j: -(j.get("duration_ms") or 0))
	status_counts: dict[str, int] = {}
	for j in jobs:
		st = j.get("status") or "Running"
		status_counts[st] = status_counts.get(st, 0) + 1
	return {
		"jobs": jobs,
		"count": len(jobs),
		"total_ms": sum(j.get("duration_ms") or 0 for j in jobs),
		"total_queries": sum(j.get("queries_count") or 0 for j in jobs),
		"any_findings_counted": any_findings_counted,
		"status_counts": status_counts,
	}


# v0.12.16+: _normalize_callsite moved to
# optimus/renderer/finding_enrichment.py. Re-imported at the top of
# this module so call sites resolve unchanged.


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
			_resolved = _resolve_dotted_to_code(
				detail["dotted_path"], file_cache=file_cache,
			)
			if _resolved:
				_abs, _ln, _name = _resolved
				detail["callsite"] = {
					"filename": _bench_relative_display(_abs),
					"_abs": _abs,
					"lineno": _ln,
					"function": _name or str(detail["dotted_path"]).rsplit(".", 1)[-1],
					"source_snippet": _read_source_snippet(_abs, _ln, cache=file_cache),
				}

	# v0.6.0 Round 2 / v0.7.x: a Hot Line finding carries the exact profiled
	# line in ``line_content``. Show it WITH ±2 context lines (read from the
	# file, like call-tree findings) instead of a lone row — but keep the
	# profiled text authoritative for the hot line itself, so a file that
	# drifted since the run can't misrepresent what was actually profiled.
	# Falls back to the single stored line when the file can't be read at
	# render (offline / regenerated without the source) or the line is gone.
	callsite = detail.get("callsite") or {}
	if (
		callsite
		and not callsite.get("source_snippet")
		and detail.get("line_content")
		and callsite.get("lineno") is not None
	):
		hot_ln = callsite["lineno"]
		window = _read_source_snippet(
			callsite.get("filename"), hot_ln, cache=file_cache,
		)
		if window and any(r.get("lineno") == hot_ln for r in window):
			for r in window:
				if r.get("lineno") == hot_ln:
					r["content"] = detail["line_content"]  # profiled text wins
			callsite["source_snippet"] = window
		else:
			callsite["source_snippet"] = [{
				"lineno": hot_ln,
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

	# v0.7.x+: Server Script findings carry the synthetic ``<serverscript>:
	# <name>`` filename. Set ``_abs`` to the Desk URL (``/app/server-script/
	# <actual-name>``) and tag ``_link_kind = "desk"`` so the template renders
	# a click-to-Desk link instead of building a broken vscode://file URL
	# over the synthetic path. Bare ``<serverscript>`` (no name) falls
	# through with no link — same fallback as any other unresolvable callsite.
	if callsite and callsite.get("filename"):
		_fn = str(callsite["filename"])
		if _fn.startswith("<serverscript") or _fn.startswith("<server-script"):
			from optimus.server_script_source import desk_url, extract_script_name

			_scrubbed = extract_script_name(_fn)
			if _scrubbed:
				callsite["_abs"] = desk_url(_scrubbed, cache=file_cache)
				callsite["_link_kind"] = "desk"
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


# v0.12.10+: doc-event lifecycle binding + per-DocType breakdown moved to
# optimus/renderer/doc_event_renderer.py. The functions (_extract_target_doc,
# _attach_action_context, _build_doc_event_breakdown, _doctype_from_controller_path,
# _module_from_filename, _doc_event_hook_index, _finding_hook_events,
# _finding_lifecycle_bindings, _build_doc_event_hook_index) + constants
# (_LIFECYCLE_EVENTS, _KIND_*, _SEVERITY_RANK) are re-imported at the top of
# this module so call sites resolve unchanged.


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


# v0.10.0+: _PRE_BLOCK_RE, _looks_like_diff, _diff_line_class, and
# _highlight_diff_html moved to optimus/renderer/syntax.py with the rest
# of the Pygments + diff-highlighting helpers. The imports at the top of
# this file re-introduce ``_highlight_diff_html`` under the same name.


# v0.10.0+: _SNIPPET_TRUNCATE_CHARS, _path_within_bench, _resolve_source_path,
# _read_source_snippet, _read_source_window all moved to
# optimus/renderer/source.py — imported at the top of this file.
# (Dead code that was moved to optimus/renderer/source.py has been removed here.
# _resolve_source_path / _read_source_snippet / _read_source_window / _path_within_bench
# / _SNIPPET_TRUNCATE_CHARS / _BoundedFileCache / _FILE_CACHE_MAX_ENTRIES now live in
# optimus/renderer/source.py and are re-imported at the top of this file.)


def _read_function_body_snippet(
	filename: str,
	def_lineno,
	*,
	cache: dict | None = None,
	max_lines: int = 40,
) -> list[dict] | None:
	"""v0.7.x: read a whole function body — from its ``def`` line to the end
	of the function — as ``[{lineno, content}]`` (same shape as
	``_read_source_snippet``). Used for self-time hot-path findings with no
	deeper user-code frame: Phase-1 sampling can't pinpoint the hot line, but
	the function is the relevant unit, so show all of it rather than a ±2-line
	peek. The function ends at the first non-blank line indented at or below
	the ``def``'s indentation (next def/class/module statement); capped at
	``max_lines``. Returns ``None`` when the file isn't readable / lineno is
	out of range."""
	try:
		ln = int(def_lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	if cache is not None and filename in cache:
		lines = cache[filename]
	else:
		resolved = _resolve_source_path(filename)
		if isinstance(resolved, tuple) and resolved[0] == "server_script":
			from optimus.server_script_source import get_server_script_lines

			lines = get_server_script_lines(resolved[1], cache=cache)
		else:
			try:
				with open(resolved, encoding="utf-8") as fh:
					lines = fh.read().splitlines()
			except Exception:
				lines = None
		if cache is not None:
			cache[filename] = lines

	if not lines or ln > len(lines):
		return None

	limit = _SNIPPET_TRUNCATE_CHARS

	def _row(idx: int) -> dict:
		content = lines[idx - 1]
		if len(content) > limit:
			content = content[:limit] + "..."
		return {"lineno": idx, "content": content}

	def_line = lines[ln - 1]
	def_indent = len(def_line) - len(def_line.lstrip())

	out = [_row(ln)]
	n = ln + 1
	while n <= len(lines) and len(out) < max_lines:
		raw = lines[n - 1]
		if raw.strip():
			indent = len(raw) - len(raw.lstrip())
			if indent <= def_indent:
				break  # dedented to def level — function (and decorators) ended
		out.append(_row(n))
		n += 1
	return out or None


def _expand_self_time_snippets(findings, *, file_cache: dict | None = None) -> None:
	"""v0.7.x: for self-time hot-path findings with no deeper user-code frame
	(empty ``drilldown_chain``), narrow the smoking-gun snippet to the function's
	signature line and flag it ``self_time_no_pinpoint``. Phase-1 sampling can't
	pinpoint a single hot line inside the function, so dumping the whole body
	(highlighting only the def) was a misleading wall of code — the card now
	shows just the def + a note pointing the developer at a Line-Level Drilldown
	on the function (which CAN give per-line timing).

	Runs AFTER ``_attach_drilldown_chains`` (which populates ``drilldown_chain``)
	and mutates findings in place. Best-effort: only the empty-list case (a
	deeper chain means the ±2 window + chain is enough; a missing key means the
	chain was never computed)."""
	for finding in findings or []:
		if (finding.get("finding_type") or "") != "Slow Hot Path":
			continue
		detail = finding.get("technical_detail") or {}
		# Only when a chain was attempted AND came back empty (no deeper frame).
		if detail.get("drilldown_chain") != []:
			continue
		callsite = detail.get("callsite") or {}
		fn = callsite.get("filename")
		ln = callsite.get("lineno")
		if not fn or ln is None:
			continue
		# Flag drives the "no single hot line — run a Line-Level Drilldown" note.
		callsite["self_time_no_pinpoint"] = True
		body = _read_function_body_snippet(fn, ln, cache=file_cache)
		if body:
			callsite["source_snippet"] = body[:1]  # signature/def line only
		detail["callsite"] = callsite
		finding["technical_detail"] = detail


def _action_dotted_entry(action) -> str | None:
	"""Derive an action's dotted entry-point path, or ``None``.

	- RQ Job: ``action["path"]`` is already the job method (Frappe's
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
	if event_type == "RQ Job":
		return path.split("?", 1)[0].strip() or None
	if event_type == "HTTP Request" and path.startswith("/api/method/"):
		rest = path[len("/api/method/"):]
		rest = rest.split("?", 1)[0].split("/", 1)[0].strip().strip(".")
		return rest or None
	return None


def _skip_decorators_to_def(
	abs_filename: str,
	start_lineno: int,
	fn_name: str,
	*,
	cache: dict | None = None,
) -> int:
	"""Return the lineno of ``def <fn_name>`` / ``async def <fn_name>``
	at or after ``start_lineno`` in ``abs_filename``. Returns
	``start_lineno`` unchanged when the line at ``start_lineno`` isn't
	a decorator OR no matching def is found within 30 lines.

	On CPython 3.11+, ``code.co_firstlineno`` for a decorated function
	points at the first **decorator** line rather than the ``def``
	line. Every Frappe API endpoint is ``@frappe.whitelist``-decorated,
	so without this skip the Per-Action breakdown's entry-callsite
	snippets all highlight ``@frappe.whitelist(...)`` instead of the
	actual signature below it.

	Reads through ``cache`` (the shared per-render file_cache) when
	available so multiple decorated functions in the same file don't
	each re-read the source.
	"""
	if not abs_filename or start_lineno <= 0 or not fn_name:
		return start_lineno
	# Read source (cache-aware).
	if cache is not None and abs_filename in cache:
		lines = cache[abs_filename]
	else:
		try:
			with open(abs_filename, encoding="utf-8") as fh:
				lines = fh.read().splitlines()
		except Exception:
			lines = None
		if cache is not None:
			cache[abs_filename] = lines
	if not lines or start_lineno > len(lines):
		return start_lineno
	# Cheap early exit: the line at start_lineno isn't a decorator →
	# nothing to skip.
	first = lines[start_lineno - 1].lstrip()
	if not first.startswith("@"):
		return start_lineno
	# Scan forward (≤ 30 lines) for the def line.
	pat = re.compile(
		r"^\s*(?:async\s+)?def\s+" + re.escape(fn_name) + r"\b"
	)
	last = min(len(lines), start_lineno + 30)
	for i in range(start_lineno, last):
		if pat.match(lines[i]):
			return i + 1  # convert 0-indexed to 1-indexed lineno
	return start_lineno  # no def found — fall back to original


def _resolve_dotted_to_code(
	dotted,
	*,
	file_cache: dict | None = None,
) -> tuple[str, int, str] | None:
	"""Resolve a dotted module path to ``(abs_filename, lineno, func_name)``.

	Uses ``importlib`` directly — NOT ``frappe.get_attr`` — because the
	latter needs a running site (it touches ``frappe.local``), which the unit
	tests don't have. Mirrors ``line_profile.picker.resolve_freeform``'s
	import strategy (longest importable leading prefix, then ``getattr`` the
	rest), minus its eligibility checks. ``inspect.unwrap`` sees through
	``functools.wraps`` decorators (e.g. ``@frappe.whitelist``). Returns
	``None`` on any failure — never raises.

	v0.7.x: when ``code.co_firstlineno`` points at a decorator line
	(CPython 3.11+ behavior for decorated functions), the lineno is
	advanced to the ``def`` line via ``_skip_decorators_to_def`` so
	the callsite snippet anchors on the function signature rather
	than ``@frappe.whitelist(...)``.
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
		abs_path = os.path.abspath(filename)
		fn_name = getattr(obj, "__name__", "") or ""
		lineno = _skip_decorators_to_def(
			abs_path, int(lineno), fn_name, cache=file_cache,
		)
		return (abs_path, int(lineno), fn_name)
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
	resolved = _resolve_dotted_to_code(dotted, file_cache=cache)
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
			resolved = _resolve_dotted_to_code(f"{dotted}.{func}", file_cache=cache)
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


# (_read_source_window duplicate definition removed — the function now
# lives in optimus/renderer/source.py and is re-imported at the top of
# this file.)


# ---------------------------------------------------------------------------
# v0.5.2 round 3: Executive summary
# ---------------------------------------------------------------------------
# A one-paragraph TL;DR at the top of the report that a non-developer
# (product manager, ops lead, customer) can read in 30 seconds and walk
# away knowing (1) is the session slow, (2) what's the biggest problem,
# (3) how much of the time it accounts for. Surfaces the top 3 most-
# impactful actionable findings as plain-English bullets.
#
# v0.7.x: ``_build_executive_summary`` stays as the data layer (pace,
# top-3 bullets, infra_note — feeds the upcoming Action plan section);
# the *headline* portion of the original "At a glance" card was
# replaced template-side by ``_compose_tldr`` below — a hero block
# composing one sentence keyed on the single highest-impact finding.


# Mapping from analyzer finding_type to a short category slug used by
# _compose_tldr for branch selection. Anything not in the map falls
# through to the verbatim-title branch — safe default.
_CATEGORY_FOR_FINDING_TYPE: dict[str, str] = {
	"N+1 Query": "n_plus_one",
	"Framework N+1": "n_plus_one",
	"Hook Bottleneck": "slow_hook",
	"Slow Hot Path": "slow_path",
	"Hot Line": "hot_line",
	"Slow Query": "slow_query",
	"Missing Index": "missing_index",
	"Full Table Scan": "full_table_scan",
	"Redundant Call": "redundant_call",
	"Repeated Hot Frame": "repeated_hot_frame",
	"Filesort": "filesort",
	"Temporary Table": "temp_table",
	"Low Filter Ratio": "low_filter",
}

# Ascending order for sorted() — High first. NOT the same as the
# `> max` _SEVERITY_RANK constant defined earlier in this module
# (which uses {High:3, Medium:2, Low:1} for max-of comparisons).
# Named distinctly to avoid the shadowing collision that broke
# `_build_doc_event_breakdown`'s severity-max merge.
_TLDR_SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def _category_for(finding_type: str | None) -> str:
	"""Map a finding_type string to a TL;DR category slug, or 'other'."""
	return _CATEGORY_FOR_FINDING_TYPE.get(finding_type or "", "other")


# Verb-led action titles per finding category. Strings are short
# imperative phrases — the user reads the title alone and knows what
# to do next. Fallback: the finding's own title (verbatim).
_ACTION_VERB_FOR_FINDING_TYPE: dict[str, str] = {
	"N+1 Query": "Eliminate the N+1 query",
	"Framework N+1": "Eliminate the N+1 query (framework code)",
	"Hook Bottleneck": "Speed up the doc-event hook",
	"Slow Hot Path": "Optimise the hot call path",
	"Hot Line": "Tune the single hottest line",
	"Missing Index": "Add a database index",
	"Slow Query": "Speed up the slow query",
	"Full Table Scan": "Eliminate the full-table scan",
	"Filesort": "Avoid the filesort",
	"Temporary Table": "Avoid the temporary table",
	"Low Filter Ratio": "Narrow the WHERE clause",
	"Redundant Call": "Cache the repeated call",
	"Repeated Hot Frame": "Investigate the recurring hot frame",
}


def _action_verb_for(finding_type: str | None) -> str | None:
	"""Return a short verb-led action title for a finding_type, or
	None when the verb isn't known (callers fall back to the finding's
	own title)."""
	return _ACTION_VERB_FOR_FINDING_TYPE.get(finding_type or "")


def _build_action_plan(
	findings: list[dict],
	large_duration_threshold_ms: float = 1000.0,
	max_steps: int = 3,
) -> list[dict]:
	"""Top-N action plan steps from the highest-impact findings.

	Returns a list of dicts shaped for the template's
	`.action-step` row::

	    {"n": int, "title": str, "desc": str, "gain_ms": float,
	     "gain_label": str, "callsite": "file:lineno" or None}

	Sort: severity DESC, then estimated_impact_ms DESC. Findings with
	zero impact are still eligible (they may not have a measurable
	cost but still warrant attention). Empty input → empty list, and
	the template hides the section.

	The Action plan is conceptually the same top-3 as the old exec-
	summary bullets; this function replaces that data layer.
	"""
	if not findings:
		return []

	def _sort_key(f: dict):
		return (
			_TLDR_SEVERITY_ORDER.get(f.get("severity") or "", 9),
			-float(f.get("estimated_impact_ms") or 0),
		)
	ranked = sorted(findings, key=_sort_key)[:max_steps]

	out: list[dict] = []
	for i, f in enumerate(ranked, start=1):
		ftype = f.get("finding_type") or ""
		verb = _action_verb_for(ftype)
		title = verb or (f.get("title") or "Investigate this finding")
		desc = (f.get("customer_description") or "").strip()
		if not desc:
			# Fall back to the finding's title prose when no
			# customer description is available — better than empty.
			desc = (f.get("title") or "").strip()
		callsite = None
		detail = f.get("technical_detail") or {}
		cs = detail.get("callsite") if isinstance(detail, dict) else None
		if isinstance(cs, dict):
			fname = cs.get("filename")
			lineno = cs.get("lineno")
			if fname and lineno:
				callsite = f"{fname}:{lineno}"
		# v0.7.x J.11: append the DocType suffix to the action-plan step
		# callsite so the `.callsite` line under each step reads
		# `ugly_code/python/common.py:13 (Sales Invoice)`. The
		# detail.target_doc was attached by _attach_action_context earlier
		# in the render flow; falls back to bare callsite when no doctype.
		_td_dt = None
		_td_obj = (detail.get("target_doc") if isinstance(detail, dict) else None) or {}
		if isinstance(_td_obj, dict):
			_td_dt = _td_obj.get("doctype")
		if _td_dt and callsite and " (" not in callsite:
			callsite = f"{callsite} ({_td_dt})"
		out.append({
			"n": i,
			"title": title,
			"desc": desc,
			"gain_ms": float(f.get("estimated_impact_ms") or 0),
			"gain_label": "est. saving",
			"callsite": callsite,
			"finding_type": ftype,
		})
	return out


def _build_waterfall(
	actions: list[dict],
	findings: list[dict],
	large_duration_threshold_ms: float = 1000.0,
	max_rows: int = 8,
) -> list[dict]:
	"""Top-N actions by duration, formatted as a horizontal-bar
	waterfall.

	Returns a list of dicts::

	    {"name": str, "duration_ms": float, "pct": float,
	     "hot": bool, "bg": bool}

	`pct` is scaled to the displayed slice's max duration — so the
	longest row always renders at 100% and shorter rows are visible.
	Scaling to the session total would make sub-second actions
	invisible.

	`hot` = the action has any High-severity linked finding (via
	`action_ref`). `bg` = the action is a background job (event_type
	== "RQ Job").

	When ``actions`` is empty, returns an empty list and the template
	hides the section.
	"""
	if not actions:
		return []

	# Build action_ref → high-severity-count once.
	high_refs: set[str] = set()
	for f in (findings or []):
		if (f.get("severity") or "") == "High":
			ref = str(f.get("action_ref") or "").strip()
			if ref:
				high_refs.add(ref)

	def _duration(a: dict) -> float:
		return float(a.get("duration_ms") or 0)

	sorted_actions = sorted(
		(a for a in actions if _duration(a) > 0),
		key=_duration,
		reverse=True,
	)[:max_rows]
	if not sorted_actions:
		return []

	max_ms = _duration(sorted_actions[0]) or 1.0

	out: list[dict] = []
	for a in sorted_actions:
		dur = _duration(a)
		idx = str(a.get("idx", "")).strip()
		is_bg = (a.get("event_type") or "") == "RQ Job"
		is_hot = idx in high_refs
		# Name: action_label or the dotted path.
		name = (
			a.get("action_label")
			or a.get("path")
			or a.get("method")
			or "?"
		)
		out.append({
			"name": str(name),
			"duration_ms": dur,
			"pct": round((dur / max_ms) * 100.0, 1) if max_ms else 0.0,
			"hot": is_hot,
			"bg": is_bg,
		})
	return out


_ORM_DOCTYPE_PATTERNS = [
	# frappe.get_doc("User", ...) / frappe.get_cached_doc("User", ...) /
	# frappe.db.get_value("User", ...) / frappe.get_all("User", ...) / frappe.db.exists("User", ...) etc.
	re.compile(r"""frappe\.(?:db\.)?(?:get_doc|get_cached_doc|get_value|get_all|get_list|exists|get_single)\s*\(\s*['"]([A-Z][A-Za-z _]+)['"]"""),
]


def _doctype_from_orm_call(src_line: str | None) -> str | None:
	"""Pull a DocType name out of common Frappe ORM call literals.

	``frappe.get_doc("User", ...)`` -> ``"User"``. Returns None when the
	line isn't a recognised ORM call. Used by ``_compose_tldr`` to write
	"a User document fetched 100 times" instead of a generic "a document".
	"""
	if not src_line:
		return None
	s = str(src_line)
	for pat in _ORM_DOCTYPE_PATTERNS:
		m = pat.search(s)
		if m:
			return m.group(1).strip() or None
	return None


def _action_verb_from_label(action_label: str | None) -> str | None:
	"""``"frappe.desk.form.save.savedocs:Submit"`` -> ``"Submit"``.

	Returns the part after the final colon when present; ``None`` otherwise.
	Used by ``_compose_tldr`` to write "Sales Invoice Submit" naturally.
	"""
	if not action_label or ":" not in action_label:
		return None
	tail = action_label.rsplit(":", 1)[-1].strip()
	# Strip any J.8/J.9 doctype suffix "(Sales Invoice)" off the verb.
	if " (" in tail:
		tail = tail.split(" (", 1)[0].strip()
	return tail or None


def _savings_phrase(pct: float) -> str:
	"""Fuzzy round of an impact percentage into a human phrase.

	``pct`` is impact_ms / action_duration_ms (range 0..1). Returns
	"by roughly half" / "by roughly two-thirds" / "by roughly a third"
	near common fractions; falls back to "by roughly N%" outside those
	bands so the prose stays accurate for awkward ratios.
	"""
	if pct is None or pct <= 0:
		return ""
	if pct >= 0.65:
		return "by roughly two-thirds"
	if 0.40 <= pct < 0.65:
		return "by roughly half"
	if 0.20 <= pct < 0.40:
		return "by roughly a third"
	return f"by roughly {pct * 100:.0f}%"


# v0.12.8: call-tree rendering moved to ``optimus.renderer.call_tree_renderer``.
# The names (_CALL_TREE_*, _CT_OTHER_RE, _ct_is_*, _render_call_tree_node,
# _render_call_tree_panel) are re-imported at the top of this module so call
# sites resolve unchanged. The original definitions used to live here.


def _aggregate_frame_truncation(actions: list[dict]) -> dict:
	"""B.DI2 — sum captured / kept frames across actions whose call-tree
	hit ``CALL_TREE_HARD_TRUNCATE_KEEP_FRAMES``.

	Returns ``{"captured": int, "kept": int, "actions_affected": int,
	"keep_limit": int}``; ``actions_affected == 0`` means no truncation
	occurred (template hides the banner).
	"""
	captured = 0
	kept = 0
	affected = 0
	keep_limit = 0
	for a in actions or []:
		raw = a.get("call_tree_json") if isinstance(a, dict) else None
		if not raw:
			continue
		try:
			tree = json.loads(raw)
		except Exception:
			continue
		if not isinstance(tree, dict) or not tree.get("_truncated"):
			continue
		c = int(tree.get("_captured_frames") or 0)
		k = int(tree.get("_kept_frames") or 0)
		if c <= 0:
			continue
		captured += c
		kept += k
		affected += 1
		if k > keep_limit:
			keep_limit = k
	return {
		"captured": captured,
		"kept": kept,
		"actions_affected": affected,
		"keep_limit": keep_limit,
	}


def _compose_tldr(
	findings: list[dict],
	session_doc: Any,
	large_duration_threshold_ms: float = 1000.0,
	actions: list[dict] | None = None,
) -> dict:
	"""Compose the TL;DR hero block.

	Picks the single highest-impact finding (severity desc, then impact
	desc), looks up its category, and builds a one-sentence headline
	with the impact / loop-count / hook-name highlighted via
	``<span class="hot">…</span>``. The sub-line is session totals.

	Returns a dict the template renders verbatim:
	``{"label": str, "headline_markup": Markup, "sub_markup": Markup}``.

	When ``findings`` is empty, returns the clean-session branch
	(no <span class="hot"> — nothing's wrong, no signal red needed).

	The Markup-aware composition mirrors the recently-fixed
	executive-summary headline: f-strings would flatten Markup back
	to str and Jinja would HTML-escape the spans, so we use
	``Markup.format(...)`` — it escapes plain-string args and passes
	Markup args through untouched.
	"""
	def _fmt(v):
		return _format_duration_ms(v, large_duration_threshold_ms)

	total_ms = float(getattr(session_doc, "total_duration_ms", 0) or 0)
	total_queries = int(getattr(session_doc, "total_queries", 0) or 0)
	total_actions = int(getattr(session_doc, "total_requests", 0) or 0)

	if not findings:
		# Clean-session branch — no signal red.
		return {
			"label": "Clean session",
			"headline_markup": Markup(
				"Nothing to fix in this session - total {duration} across "
				"{actions} operation{plural}, all under threshold."
			).format(
				duration=_fmt(total_ms),
				actions=total_actions,
				plural="s" if total_actions != 1 else "",
			),
			"sub_markup": Markup(
				"Total active time: {duration} "
				"<small class=\"scope-tag\">consolidated &middot; session</small> "
				"&middot; {actions} operations &middot; {queries} DB queries."
			).format(
				duration=_fmt(total_ms),
				actions=total_actions,
				queries=total_queries,
			),
		}

	# Sort by severity DESC then impact_ms DESC — be defensive even
	# though the upstream sort usually already has this order.
	def _sort_key(f: dict):
		return (
			_TLDR_SEVERITY_ORDER.get(f.get("severity") or "", 9),
			-float(f.get("estimated_impact_ms") or 0),
		)
	top = sorted(findings, key=_sort_key)[0]

	finding_type = top.get("finding_type") or ""
	category = _category_for(finding_type)
	impact_ms = float(top.get("estimated_impact_ms") or 0)
	affected = int(top.get("affected_count") or 0)
	title = top.get("title") or ""
	severity = top.get("severity") or "?"

	impact_html = _fmt(impact_ms)

	# v0.7.x J.15: rich Hot Line branch — narrative two-sentence
	# headline that names the target DocType being fetched, the loop
	# count, the action it's slowing, and the expected savings.
	# Falls through to a slimmer sentence when any of those are missing.
	if category == "hot_line" and affected:
		_detail = (top.get("technical_detail") or {})
		_cs = _detail.get("callsite") or {}
		_target_doctype = None
		# Find the hot line in the source snippet and parse a DocType
		# literal off it.
		_target_lineno = _cs.get("lineno") if isinstance(_cs, dict) else None
		for _sl in (_cs.get("source_snippet") or []):
			if isinstance(_sl, dict) and _sl.get("lineno") == _target_lineno:
				_target_doctype = _doctype_from_orm_call(_sl.get("content"))
				break
		# Find the action this finding belongs to + compute savings.
		_action_label_human = None
		_savings = None
		_ref = (top.get("action_ref") or "").strip()
		if _ref.isdigit() and actions:
			_idx = int(_ref)
			for _a in actions:
				if isinstance(_a, dict) and _a.get("idx") == _idx:
					_a_dt = (_a.get("target_doc") or {}).get("doctype") if isinstance(_a.get("target_doc"), dict) else None
					_verb = _action_verb_from_label(_a.get("action_label"))
					if _a_dt and _verb:
						_action_label_human = f"{_a_dt} {_verb}"
					elif _verb:
						_action_label_human = _verb
					_a_ms = float(_a.get("duration_ms") or 0)
					if _a_ms > 0:
						_savings = _savings_phrase(impact_ms / _a_ms)
					break
		if _target_doctype and _action_label_human and _savings:
			headline = Markup(
				"One line of code is responsible for "
				"<span class=\"hot\">~{impact}</span> of this session "
				"&mdash; a <strong>{target}</strong> document fetched "
				"<span class=\"hot\">{n} times inside a loop</span> that "
				"should only run once. Fix it and the "
				"<strong>{label}</strong> drops {savings}."
			).format(
				impact=impact_html,
				target=_target_doctype,
				n=affected,
				label=_action_label_human,
				savings=_savings,
			)
		else:
			# Slimmer fallback when ORM target / action context isn't
			# available (e.g. legacy data, framework call).
			headline = Markup(
				"One line of code is responsible for "
				"<span class=\"hot\">~{impact}</span> of this session "
				"&mdash; same line ran <span class=\"hot\">{n} times "
				"inside a loop</span>. Tune that line and most of the "
				"cost goes away."
			).format(impact=impact_html, n=affected)
	elif category == "n_plus_one" and affected:
		headline = Markup(
			"One line of code is responsible for <span class=\"hot\">~"
			"{impact}</span> of this session &mdash; same query ran "
			"<span class=\"hot\">{n}× inside a loop</span>. "
			"Removing the redundant round-trips is the single biggest "
			"win here."
		).format(impact=impact_html, n=affected)
	elif category == "slow_hook":
		headline = Markup(
			"<span class=\"hot\">{impact}</span> is spent inside a "
			"doc-event hook — the slowest hook this session. {title}"
		).format(impact=impact_html, title=title)
	elif category in ("slow_query", "missing_index", "full_table_scan"):
		headline = Markup(
			"A single query took <span class=\"hot\">{impact}</span> "
			"— {title}"
		).format(impact=impact_html, title=title)
	elif category == "redundant_call":
		if affected:
			headline = Markup(
				"Same call repeated <span class=\"hot\">{n}×</span> "
				"— {impact} of this session. {title}"
			).format(n=affected, impact=impact_html, title=title)
		else:
			headline = Markup(
				"<span class=\"hot\">{impact}</span> in redundant work — "
				"{title}"
			).format(impact=impact_html, title=title)
	else:
		# Fallback — verbatim title with the impact called out.
		headline = Markup(
			"<span class=\"hot\">{impact}</span> &middot; {title}"
		).format(impact=impact_html, title=title)

	same_sev_count = sum(
		1 for f in findings if (f.get("severity") or "") == severity
	)
	# v0.7.x J.15: when every finding of the top severity points at the
	# same callsite filename, append "all in <file>" to the sub-line so
	# the reader sees the single hot file at a glance. Multi-file or
	# missing-callsite cases fall through with no suffix.
	_sev_filenames = set()
	for _f in findings:
		if (_f.get("severity") or "") != severity:
			continue
		_d = _f.get("technical_detail")
		if not isinstance(_d, dict):
			continue
		_c = _d.get("callsite")
		if isinstance(_c, dict):
			_fname = _c.get("filename")
			if _fname:
				_sev_filenames.add(_fname)
	_all_in = ""
	if len(_sev_filenames) == 1:
		_only = next(iter(_sev_filenames))
		_all_in = Markup(", all in <code>{file}</code>").format(file=_only)
	sub = Markup(
		"Total active time: {duration} "
		"<small class=\"scope-tag\">consolidated &middot; session</small> "
		"&middot; {actions} operations &middot; {queries:,} DB queries "
		"&middot; {n} {severity_lc}-severity finding{plural}{all_in}."
	).format(
		duration=_fmt(total_ms),
		actions=total_actions,
		queries=total_queries,
		n=same_sev_count,
		severity_lc=severity.lower(),
		plural="s" if same_sev_count != 1 else "",
		all_in=_all_in,
	)

	return {
		"label": "The headline",
		"headline_markup": headline,
		"sub_markup": sub,
	}


def _build_executive_summary(
	*,
	findings: list[dict],
	session_doc: Any,
	v5: dict,
	large_duration_threshold_ms: float = 1000.0,
) -> dict:
	"""Return a dict shaped for the template's exec-summary card.

	Shape: ``{"headline": Markup, "bullets": list[str], "show": bool}``

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
	# v0.7.x: honour the timing rule for the headline duration. The
	# helper returns Markup (always — for ms / s / "0ms" branches), so
	# we build the headline as Markup.format(...) — that escapes the
	# plain-string args while passing the Markup duration through
	# unchanged, keeping the <span class="time-high">…</span> intact
	# under Jinja autoescape.
	duration_html = _format_duration_ms(total_ms, large_duration_threshold_ms)
	headline = Markup(
		"This session took {duration} across {actions} operation{plural} "
		"— {queries} database queries, ~{qpa} per operation."
	).format(
		duration=duration_html,
		actions=total_actions,
		plural="s" if total_actions != 1 else "",
		queries=int(total_queries),
		qpa=queries_per_action,
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
		# v0.7.x M5 renamed ``duration_ms`` → ``query_duration_ms`` on the
		# top-queries leaderboard. Accept both so a session captured before
		# the rename still rehydrates correctly.
		_dur = q.get("query_duration_ms") or q.get("duration_ms") or 0
		if _dur < TOP_QUERY_FLOOR_MS:
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

	# Tail buckets — user-app findings come first.
	# "Request hotspots" (hot-path findings that lost their callsite to
	# pyinstrument's collapsing but are still typed) is kept; the
	# generic "Other (no callsite)" bucket is suppressed entirely
	# (v0.7.x). Findings binned into that label are typically the
	# residue of analyzer paths that couldn't attach a representative
	# callsite — surfacing them as a generic tail bucket added noise
	# without an actionable file:line for the developer.
	if _HOTPATH_BUCKET_LABEL in buckets:
		ordered.append(_HOTPATH_BUCKET_LABEL)

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


# v0.10.0+: duration + datetime formatting moved to
# optimus/renderer/time_format.py — see top-of-file import.


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
	"Slow Background Job", # BG-job fallback finding (v0.7.x)
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


# v0.10.0+: redact_frame_name + build_donut_data + build_donut_svg +
# build_hot_frames_table + the _DONUT_COLORS palette moved to
# optimus/renderer/visualization.py. Imported at the top of this file.
# (Dead code that was moved to optimus/renderer/visualization.py has been removed here.
# _DONUT_COLORS / redact_frame_name / build_donut_data / build_donut_svg /
# build_hot_frames_table now live in optimus/renderer/visualization.py and are
# re-imported at the top of this file.)


# (build_donut_svg / build_hot_frames_table removed — they live in
# optimus/renderer/visualization.py and are re-imported at the top
# of this file.)
