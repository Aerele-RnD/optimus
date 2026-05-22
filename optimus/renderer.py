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

# Pygments is loaded lazily inside ``_ensure_pygments`` so paths that
# never highlight code (DocType save callbacks, janitor sweeps, light
# API endpoints) don't pay the ~30-50ms import cost at module load.
# Module-level slots are populated on first use and cached.
_PY_LEXER = None
_PY_HTML_FMT = None
_pyg_highlight = None


# Phase K hardening: render-time redaction of sensitive form_dict /
# headers values. Profiler recordings persist raw HTTP request data
# (which can include passwords, API keys, session tokens, CSRF
# tokens) - even though the report is admin-only, exporting one to a
# non-admin teammate must NOT leak credentials. The renderer (and
# ``export_session`` API) call ``_redact_sensitive`` over recording
# form_dict + headers before they reach the rendered HTML / JSON
# payload.
_SENSITIVE_KEY_PATTERNS = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
)


def _is_sensitive_key(key) -> bool:
	if not isinstance(key, str) or not key:
		return False
	lower = key.lower()
	return any(p in lower for p in _SENSITIVE_KEY_PATTERNS)


# Phase K hardening: best-effort SQL parameter redaction. The recorder
# captures raw SQL with parameter values baked in (``WHERE password =
# 'admin123'``); even though Optimus is admin-only, an export shared
# with a teammate must not surface credentials. ``_redact_sql_literals``
# walks SQL via sqlparse and replaces the right-hand-side literal in
# any ``<sensitive_column> = '...'`` comparison. Falls back to a regex
# pass for malformed SQL so a parser hiccup never blocks rendering.
_SQL_SENSITIVE_COLUMNS = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
)
_SQL_LITERAL_REGEX = re.compile(
	r"""(\b(?:""" + "|".join(_SQL_SENSITIVE_COLUMNS) + r""")\b\s*(?:=|LIKE|IN)\s*)("[^"]*"|'[^']*'|\([^)]*\))""",
	re.IGNORECASE,
)


def _redact_sql_literals(sql_str: str) -> str:
	"""Return ``sql_str`` with literal values in
	``<sensitive_column> = '...'`` comparisons replaced by
	``'<REDACTED>'``. Best-effort - imperfect on UPDATE SET clauses
	and obscure column names, but raises the bar significantly.

	Two-pass strategy:
	  1. ``sqlparse``-based walk for well-formed SELECT / WHERE
	     (preserves token spacing).
	  2. Regex fallback for malformed SQL or anything sqlparse
	     refuses (``_SQL_LITERAL_REGEX`` matches a known sensitive
	     column followed by ``= / LIKE / IN`` then a quoted /
	     parenthesised literal).
	"""
	if not sql_str or not isinstance(sql_str, str):
		return sql_str or ""
	# Quick exit: if no sensitive substring appears, skip both passes
	# entirely (saves the parse cost on the 95% of queries that have
	# nothing sensitive).
	lower = sql_str.lower()
	if not any(p in lower for p in _SQL_SENSITIVE_COLUMNS):
		return sql_str
	try:
		return _SQL_LITERAL_REGEX.sub(r"\1'<REDACTED>'", sql_str)
	except Exception:
		return sql_str


def _redact_call_queries(calls):
	"""Apply ``_redact_sql_literals`` over a recording's ``calls`` list
	in place. Touches ``query`` + ``normalized_query`` fields.
	"""
	if not isinstance(calls, list):
		return
	for call in calls:
		if not isinstance(call, dict):
			continue
		if call.get("query"):
			call["query"] = _redact_sql_literals(call["query"])
		if call.get("normalized_query"):
			call["normalized_query"] = _redact_sql_literals(call["normalized_query"])


def _redact_sensitive(payload):
	"""Walk a dict / list and return a copy with values under
	sensitive keys replaced by ``"<REDACTED:keyname>"``. Pure
	function - non-dict scalars pass through unchanged. Used by the
	renderer to scrub ``recording.form_dict`` / ``recording.headers``
	before they reach the rendered HTML, and by ``export_session``
	before serialising to JSON.
	"""
	if isinstance(payload, dict):
		out = {}
		for k, v in payload.items():
			if _is_sensitive_key(k):
				out[k] = f"<REDACTED:{k}>"
			else:
				out[k] = _redact_sensitive(v)
		return out
	if isinstance(payload, list):
		return [_redact_sensitive(item) for item in payload]
	if isinstance(payload, tuple):
		return tuple(_redact_sensitive(item) for item in payload)
	return payload


_FILE_CACHE_MAX_ENTRIES = 50


class _BoundedFileCache:
	"""Bounded LRU dict for ``_read_source_snippet``'s per-render file
	cache. Caps memory growth on sessions that touch many unique
	source files (the unbounded variant could hold ~50MB of file
	content on a monolithic codebase). Supports the same dict-style
	protocol the read site already uses: ``filename in cache``,
	``cache[filename]``, ``cache[filename] = lines``.
	"""

	__slots__ = ("_data", "_max")

	def __init__(self, max_entries: int = _FILE_CACHE_MAX_ENTRIES):
		from collections import OrderedDict
		self._data: OrderedDict = OrderedDict()
		self._max = max_entries

	def __contains__(self, key) -> bool:
		return key in self._data

	def __getitem__(self, key):
		# Touch on read so true LRU semantics apply (recently-read
		# files stay in the cache longer than ones read once and
		# forgotten).
		value = self._data[key]
		self._data.move_to_end(key)
		return value

	def __setitem__(self, key, value):
		self._data[key] = value
		self._data.move_to_end(key)
		while len(self._data) > self._max:
			self._data.popitem(last=False)


def _ensure_pygments() -> bool:
	"""Lazy-init Pygments lexer + formatter. Returns ``False`` if
	Pygments isn't available (graceful degradation - the caller writes
	``content_html = None`` and the template's plain-text fallback
	kicks in)."""
	global _PY_LEXER, _PY_HTML_FMT, _pyg_highlight
	if _pyg_highlight is not None:
		return True
	try:
		from pygments import highlight as _pyg_highlight_fn
		from pygments.formatters import HtmlFormatter
		from pygments.lexers import PythonLexer
		_PY_LEXER = PythonLexer(stripnl=False, ensurenl=False)
		_PY_HTML_FMT = HtmlFormatter(nowrap=True, classprefix="tok-")
		_pyg_highlight = _pyg_highlight_fn
		return True
	except Exception:
		return False


@functools.lru_cache(maxsize=512)
def _highlight_python_block_cached(joined: str) -> str:
	"""Pygments tokenise + format a multi-line Python source block.
	Cached across all calls within the same worker process so N
	overlapping snippets from N findings tokenise the underlying source
	exactly once. ``maxsize=512`` covers reasonable session sizes; LRU
	eviction handles the long-tail.
	"""
	return _pyg_highlight(joined, _PY_LEXER, _PY_HTML_FMT).rstrip("\n")


def _highlight_python_snippet(lines):
	"""Mutate each ``{"lineno", "content"}`` dict in ``lines`` to add a
	``content_html`` field carrying Pygments-highlighted span markup
	(GitHub Light palette via CSS in the template).

	The lines are joined into one source block before highlighting so
	multi-line strings, decorators, and other multi-line constructs
	keep correct tokenisation; the resulting HTML is then split per
	``\\n`` and each chunk assigned back to its source line. Idempotent
	on lines that already carry ``content_html``. Falls back to
	``content_html = None`` (template uses plain ``content``) on any
	Pygments failure or when Pygments isn't available.
	"""
	if not lines:
		return
	if all(isinstance(l, dict) and l.get("content_html") is not None for l in lines):
		return
	if not _ensure_pygments():
		for l in lines:
			if isinstance(l, dict):
				l["content_html"] = None
		return
	try:
		src = "\n".join((l.get("content") or "") for l in lines if isinstance(l, dict))
		out = _highlight_python_block_cached(src)
		chunks = out.split("\n")
		if len(chunks) != len(lines):
			for l in lines:
				if isinstance(l, dict):
					l["content_html"] = None
			return
		for l, chunk in zip(lines, chunks, strict=True):
			if isinstance(l, dict):
				l["content_html"] = chunk
	except Exception:
		for l in lines:
			if isinstance(l, dict):
				l["content_html"] = None


def _highlight_all_snippets(actions, all_findings):
	"""Walk findings + actions and apply VSCode Dark+ syntax highlighting
	to every ``source_snippet`` list reachable from the data shapes the
	template + line-prof builder iterate. Mutates in place.
	"""
	for f in all_findings or []:
		if not isinstance(f, dict):
			continue
		td = f.get("technical_detail")
		if not isinstance(td, dict):
			continue
		cs = td.get("callsite")
		if isinstance(cs, dict):
			_highlight_python_snippet(cs.get("source_snippet") or [])
		for chain_key in ("drilldown_chain", "frame_chain", "call_chain"):
			chain = td.get(chain_key)
			if isinstance(chain, list):
				for step in chain:
					if isinstance(step, dict):
						_highlight_python_snippet(step.get("source_snippet") or [])
	for a in actions or []:
		if not isinstance(a, dict):
			continue
		ec = a.get("entry_callsite")
		if isinstance(ec, dict):
			_highlight_python_snippet(ec.get("source_snippet") or [])

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


_build_phase2_callsite_index = _build_line_drilldown_callsite_index


def _make_line_drilldown_lookup(index: dict):
	"""Wrap the line-drilldown callsite index in a small lookup callable
	so Jinja can call ``line_drilldown_for_callsite(filename,
	function_name)`` - Jinja cannot index dicts by tuple keys directly.
	"""
	import os

	def lookup(filename, function_name):
		if not filename or not function_name:
			return None
		base = os.path.basename(filename)
		# Try the function name as-is, then its bare last segment — mirrors the
		# dual keying in _build_line_drilldown_callsite_index so a prefix
		# mismatch (qualname vs callsite function) can't break the callout.
		return (
			index.get((base, function_name))
			or index.get((base, function_name.rsplit(".", 1)[-1]))
		)

	return lookup


_make_phase2_lookup = _make_line_drilldown_lookup


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
	header_prefix = (
		'<span class="arrow">&#x21B3;</span>' if is_descendant else ""
	)

	html = [
		f'<div class="phase2-func{indent_cls}">',
		f'<div class="fn-name">{header_prefix}{_e(dotted)}</div>',
		f'<div class="fn-path">{_e(file_path)}</div>',
	]

	html.append(
		'<table class="line-prof">'
		'<thead><tr>'
		'<th class="num">#</th>'
		'<th class="num">hits</th>'
		'<th class="num">total</th>'
		'<th class="num">per hit</th>'
		'<th>source</th>'
		'</tr></thead><tbody>'
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
			f'<tr{tr_cls}>'
			f'<td class="ln">{line.get("lineno", "")}</td>'
			f'<td class="num">{line.get("hits", 0)}</td>'
			f'<td class="num">{_format_duration_ms(ms, decimals=2)}</td>'
			f'<td class="num">{_format_duration_ms(per_hit_ms, decimals=2)}</td>'
			f'<td class="src"><code>{_src_cell}</code></td>'
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
	# v0.7.x Phase F: cross-run diff uses the same `.line-prof` base
	# class as the per-function table, with extra `.added` / `.removed`
	# row tints for matched-faster / matched-slower / added / removed
	# statuses (CSS overlays in the `.line-prof` block).
	if not diff_rows:
		return ""

	html = [
		'<table class="line-prof line-prof-diff">'
		'<thead><tr>'
		'<th>status</th>'
		'<th class="num">prev #</th>'
		'<th class="num">curr #</th>'
		'<th class="num">prev ms</th>'
		'<th class="num">curr ms</th>'
		'<th class="num">&Delta; ms</th>'
		'<th>source</th>'
		'</tr></thead><tbody>',
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
		source_cell = f'<code>{_src}</code>'

		html.append(
			f'<tr{tr_cls}>'
			f'<td>{_e(status)}</td>'
			f'<td class="ln">{_fmt(row.get("lineno_old"))}</td>'
			f'<td class="ln">{_fmt(row.get("lineno_new"))}</td>'
			f'<td class="num">{_fmt(row.get("ms_old"))}</td>'
			f'<td class="num">{_fmt(row.get("ms_new"))}</td>'
			f'<td class="num">{_fmt(delta)}</td>'
			f'<td class="src">{source_cell}</td>'
			'</tr>'
		)
	html.append('</tbody></table>')
	return "".join(html)


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

	# v0.7.x Phase F: editorial section head with per-run cards +
	# per-function blocks. Uses the shared `.section` / `.section-head` /
	# `.phase2-run` / `.phase2-func` / `.line-prof` classes.
	n_runs = len(parsed_runs)
	html = [
		'<section class="section" id="line-drilldown">',
		'<div class="section-head">'
		'<h2>Line-Level Drilldown</h2>'
		f'<span class="section-tag">{n_runs} run{"s" if n_runs != 1 else ""}</span>'
		'</div>',
		'<p class="section-intro">'
		'Line-Level Drilldown captures only the code paths you actually ran '
		'during the line-profile pass. Any picked function that was not '
		'exercised is listed under "Not exercised in this pass" below - re-run '
		'the flow that calls it to capture its lines.'
		'</p>',
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
			f'<strong>Run {run_idx}</strong>'
			'<span class="meta">'
			f'<span class="status-badge status-{status}">{status}</span>'
			f'{_format_duration_ms(total_ms)} &middot; {started}'
			'</span>'
			'</div>'
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
				+ '</div>'
			)
		html.append('</div>')

	if diffs:
		html.append(
			'<h3 class="frontend-h3">Cross-Run Comparison</h3>'
			'<p class="section-intro">'
			'For functions profiled in two or more runs, the table below shows '
			'a line-by-line delta between the most recent two runs (aligned by '
			"content hash so file edits between runs don't break the diff)."
			'</p>'
		)
		for path, diff_meta in diffs.items():
			label = (
				f"{path} — Run {diff_meta['prev_run_idx'] + 1} → "
				f"Run {diff_meta['curr_run_idx'] + 1}"
			)
			html.append(
				'<div class="phase2-func">'
				f'<div class="fn-name">{_e(label)}</div>'
			)
			html.append(_render_phase2_diff_table(diff_meta["rows"]))
			html.append('</div>')

	html.append('</section>')
	return "".join(html)


_render_phase2_panel = _render_line_drilldown_panel


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
	# Build a per-action finding index so the fallback path (below) can
	# infer doctype from a related finding's controller-path callsite when
	# the recording's form_dict isn't available (Redis TTL expired).
	_findings_by_action_idx: dict[int, list] = {}
	for _f in (findings or []):
		if not isinstance(_f, dict):
			continue
		_ref = (_f.get("action_ref") or "").strip()
		if _ref.isdigit():
			_findings_by_action_idx.setdefault(int(_ref), []).append(_f)
	for a in (actions or []):
		if not isinstance(a, dict):
			continue
		rec = recordings_by_uuid.get(a.get("recording_uuid") or "")
		td = _extract_target_doc(rec.get("form_dict") if isinstance(rec, dict) else None)
		# v0.7.x J.13: fallback when the recording isn't available
		# (Redis TTL expired between record-time and regenerate-time) —
		# infer the action's DocType from any related finding's callsite
		# filepath via the controller-path mapping
		# (e.g. ``erpnext/.../sales_invoice/sales_invoice.py`` → "Sales Invoice").
		# Bench-side reports for old sessions get the DocType suffix
		# without needing the recording or a fresh capture.
		if not td:
			for _rf in _findings_by_action_idx.get(a.get("idx"), []):
				_detail = _rf.get("technical_detail") or {}
				_cs = _detail.get("callsite") if isinstance(_detail, dict) else None
				_fname = _cs.get("filename") if isinstance(_cs, dict) else None
				_inferred = _doctype_from_controller_path(_fname or "")
				if _inferred:
					td = {"doctype": _inferred, "name": None}
					break
		a["target_doc"] = td
		# v0.7.x J.8: enrich the action_label with the DocType suffix so
		# every downstream consumer (TLDR composer, action-plan composer,
		# per-action table cell, waterfall row, queries-per-action heading,
		# related-finding inline link) shows the doctype inline without
		# requiring template edits. Idempotent: the `" (" not in label`
		# guard means re-renders pass through untouched.
		_td_doctype = (a.get("target_doc") or {}).get("doctype") if isinstance(a.get("target_doc"), dict) else None
		_a_label = a.get("action_label") or ""
		if _td_doctype and _a_label and " (" not in _a_label:
			a["action_label"] = f"{_a_label} ({_td_doctype})"
		# v0.7.x J.9: same suffix on the URL path so the per-action
		# `.action-meta` line ("POST /api/method/...") and the
		# queries-per-action heading carry the doctype too. Idempotent on
		# `" (" not in path` for re-renders.
		_a_path = a.get("path") or ""
		if _td_doctype and _a_path and " (" not in _a_path:
			a["path"] = f"{_a_path} ({_td_doctype})"
		# v0.7.x J.11: enrich the entry-callsite `function` so the small
		# `.action-meta` line under each action row reads
		# `…/save.py:16 · savedocs (Sales Invoice)` — keeps every filepath
		# surface in the report consistently tagged with its DocType.
		_ec = a.get("entry_callsite")
		if _td_doctype and isinstance(_ec, dict):
			_ec_fn = _ec.get("function") or ""
			if _ec_fn and " (" not in _ec_fn:
				_ec["function"] = f"{_ec_fn} ({_td_doctype})"
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
			# v0.7.x J.7: inject the DocType inline at the end of the
			# italicised action-label phrase so the reader sees
			# "During *frappe.desk.form.save.savedocs:Save (Sales Invoice)*"
			# instead of "During *frappe.desk.form.save.savedocs:Save*" —
			# keeps the technical label and the doctype emphasised together
			# inside one self-contained italic phrase. Guards on the
			# `"During *…*"` prose shape so self-referential / non-action
			# descriptions pass through untouched.
			_doctype = td.get("doctype") if isinstance(td, dict) else None
			if _doctype:
				_desc = f.get("customer_description") or ""
				if _desc.startswith("During *"):
					_close_idx = _desc.find("*", len("During *"))
					if _close_idx > 0:
						_action_label = _desc[len("During *"):_close_idx]
						_original_chunk = f"*{_action_label}*"
						_new_chunk = f"*{_action_label} ({_doctype})*"
						f["customer_description"] = _desc.replace(
							_original_chunk, _new_chunk, 1
						)
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


def _path_within_bench(path: str) -> bool:
	"""Phase-K-hardening boundary check: return True only when the
	absolute ``path`` lies inside the bench directory tree. Used by
	``_resolve_source_path`` to refuse callsite filenames that resolve
	to ``/etc/passwd`` or other locations outside the bench.

	Returns ``True`` (bypass) when:
	  - ``frappe.flags.in_test`` is set (pytest fixtures legitimately
	    point at /tmp/... paths the boundary would otherwise reject);
	  - ``frappe.utils.get_bench_path`` isn't importable / fails (the
	    check is a defence-in-depth layer, not a hard requirement).
	"""
	try:
		import frappe
		if getattr(frappe.flags, "in_test", False):
			return True
		import frappe.utils
		bench = frappe.utils.get_bench_path()
	except Exception:
		return True
	if not bench:
		return True
	try:
		bench_abs = os.path.abspath(bench)
		path_abs = os.path.abspath(path)
	except Exception:
		return False
	return path_abs == bench_abs or path_abs.startswith(bench_abs + os.sep)


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
	or when ``frappe`` isn't importable (unit tests).

	Phase K hardening: every resolved path is finally checked against
	the bench-directory boundary (``_path_within_bench``). A filename
	that points outside the bench (e.g. ``/etc/passwd`` via a
	malicious analyzer dict) returns ``None``.
	"""
	if not filename:
		return None
	name = str(filename).strip()
	if not name or name.startswith("<"):
		return None
	resolved: str | None = None
	try:
		if os.path.isabs(name):
			resolved = name if os.path.exists(name) else None
		elif os.path.exists(name):
			resolved = name
		else:
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
				import frappe.utils
				bench = frappe.utils.get_bench_path()
				candidates.append(os.path.join(bench, name))
				candidates.append(os.path.join(bench, "apps", name))
			except Exception:
				pass
			for cand in candidates:
				if cand and os.path.exists(cand):
					resolved = cand
					break
	except Exception:
		return None
	if resolved and not _path_within_bench(resolved):
		# Defence-in-depth: refuse paths that escape the bench tree.
		# Log at warning level (best-effort - frappe may not be
		# importable in unit-test contexts).
		try:
			import frappe
			frappe.logger().warning(
				f"optimus._resolve_source_path: rejected out-of-bench path {resolved!r}"
			)
		except Exception:
			pass
		return None
	return resolved


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
	# v0.7.x: read a ±2-line window around the anchor (compromise
	# between ±1 — too tight, body invisible — and ±4 — included
	# preceding-function noise). The template's blank-line filter
	# drops empties (except the callsite itself), so the visible
	# snippet ends up at ~3-4 lines: the anchor `def` + a couple of
	# body lines. For the exact hot line inside the function, the
	# Slow-Hot-Path description points to the Line-Level Drilldown.
	for n in range(max(1, ln - 2), ln + 3):
		if 1 <= n <= len(lines):
			content = lines[n - 1]
			if len(content) > limit:
				content = content[:limit] + "..."
			snippet.append({"lineno": n, "content": content})
	return snippet or None


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
	(empty ``drilldown_chain``), replace the ±2-line callsite snippet with the
	whole function body so the developer can read the hot code directly.

	Runs AFTER ``_attach_drilldown_chains`` (which populates ``drilldown_chain``)
	and mutates findings in place. Best-effort: only the empty-list case (a
	deeper chain means the ±2 window + chain is enough; a missing key means the
	chain was never computed); leaves the existing snippet when the body can't
	be read."""
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
		body = _read_function_body_snippet(fn, ln, cache=file_cache)
		if body:
			callsite["source_snippet"] = body
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


_CALL_TREE_MAX_DEPTH = 12  # default cap shown without a click
_CALL_TREE_HARD_CAP = 64   # absolute ceiling — runaway protection


_CT_OTHER_RE = re.compile(r"^\[other: \d+ frames?\]$")


def _ct_is_other_frame(fn) -> bool:
	"""A synthetic ``[other: N frames]`` collapse node — dropped from the
	call tree per user request (v0.7.x)."""
	return bool(_CT_OTHER_RE.match((fn or "").strip()))


def _ct_is_sql_leaf(node) -> bool:
	"""A ``<sql>`` query leaf frame. Dropped from the call-tree display per
	user request — the tree shows only the Python hierarchy; the queries
	themselves live, itemised, in the Slowest-queries / per-action sections,
	so nothing is lost. (The analyzer still keeps these in ``call_tree_json``.)"""
	cn = node or {}
	return cn.get("function") == "<sql>" and not cn.get("children")


def _ct_is_user_frame(node) -> bool:
	"""A real user-app python frame (not framework, not a synthetic
	``<sql>`` / ``[other]`` / ``<root>`` node). Used to auto-open the tree
	down to the first user-app frame."""
	fn = node.get("function") or ""
	if not fn or fn.startswith("<") or fn.startswith("["):
		return False
	fname = (node.get("filename") or "").replace("\\", "/")
	app = fname.split("/", 1)[0] if fname else ""
	if not app:
		return False
	try:
		from optimus.analyzers.base import FRAMEWORK_APPS
	except Exception:
		FRAMEWORK_APPS = frozenset()
	return app not in FRAMEWORK_APPS


def _render_call_tree_node(node, parent_ms, depth=0, unlimited=False, breadcrumb=True):
	"""Phase K.5: recursive nested-``<details>`` emit for a single
	call_tree node. Auto-opens the hottest path down to the first user-app
	frame (``breadcrumb``); deeper branches start collapsed so the panel
	doesn't unfurl into thousands of frames on first paint.

	Past ``_CALL_TREE_MAX_DEPTH`` the remaining subtree is wrapped in
	a click-to-expand ``<details>`` so users can traverse deeper when
	they want to, with ``unlimited=True`` flipped on for that subtree
	so we don't keep nesting expanders at every level. ``_CALL_TREE_
	HARD_CAP`` is the absolute ceiling for runaway protection.
	"""
	if not isinstance(node, dict):
		return ""
	fn = node.get("function") or "<?>"
	# v0.7.x: drop synthetic "[other: N frames]" collapse nodes entirely (user
	# request — accepts that a branch's visible children may not sum to its total).
	if _ct_is_other_frame(fn):
		return ""
	file = node.get("filename") or ""
	lineno = node.get("lineno") or ""
	cum_ms = float(node.get("cumulative_ms") or 0)
	self_ms = float(node.get("self_ms") or 0)
	children = node.get("children") or []

	pct = (cum_ms / parent_ms * 100.0) if parent_ms else 0.0
	cls = "call-tree-node"
	if parent_ms and cum_ms / parent_ms >= 0.5:
		cls += " call-tree-hot"

	# v0.7.x: auto-open the hottest path down to the first user-app frame so the
	# tree "opens at" the user's code; collapse below it.
	is_user = _ct_is_user_frame(node)
	open_attr = " open" if breadcrumb else ""
	meta_lineno = f":{lineno}" if lineno else ""
	pct_label = f" &middot; {pct:.0f}%" if parent_ms else ""
	self_label = ""
	if self_ms and cum_ms - self_ms > 1:
		self_label = f" &middot; self {self_ms:.0f}ms"

	out = [
		f'<details class="{cls}"{open_attr}>',
		'<summary>',
		f'<span class="frame-name">{_e(fn)}</span>',
		f'<span class="frame-meta">{_e(file)}{meta_lineno} &middot; '
		f'{cum_ms:.0f}ms{pct_label}{self_label}</span>',
		'</summary>',
	]
	if children:
		# Drop [other: N frames] synthetic nodes AND <sql> query leaves (the
		# call tree is the Python hierarchy; per-query rows belong in the
		# Slowest-queries / DB-tables sections), then order hottest-first.
		main = sorted(
			[
				c for c in children
				if not _ct_is_other_frame((c or {}).get("function"))
				and not _ct_is_sql_leaf(c)
			],
			key=lambda c: float((c or {}).get("cumulative_ms") or 0),
			reverse=True,
		)

		within_default = unlimited or depth < _CALL_TREE_MAX_DEPTH
		within_hard = depth < _CALL_TREE_HARD_CAP

		if within_default and within_hard:
			out.append('<div class="call-tree-children">')
			for idx, c in enumerate(main):
				# Continue the auto-open breadcrumb down the single hottest path
				# until we reach a user-app frame; collapse once we're there.
				child_bc = (
					breadcrumb and not is_user and idx == 0
					and depth < _CALL_TREE_MAX_DEPTH
				)
				out.append(_render_call_tree_node(
					c, cum_ms, depth + 1, unlimited, breadcrumb=child_bc,
				))
			out.append('</div>')
		elif within_hard:
			# Past default cap — click-to-expand the rest of the
			# subtree. ``unlimited=True`` prevents further wrapping
			# at every nested level.
			out.append(
				'<div class="call-tree-children call-tree-deeper">'
				'<details class="call-tree-deeper-toggle">'
				'<summary>'
				f'<em>show {len(main)} deeper frame(s) &middot; '
				f'depth {depth + 1}+</em>'
				'</summary>'
				'<div class="call-tree-children">'
			)
			for c in main:
				out.append(_render_call_tree_node(
					c, cum_ms, depth + 1, unlimited=True, breadcrumb=False,
				))
			out.append('</div></details></div>')
		else:
			# depth >= HARD_CAP; absolute truncation as safety net.
			out.append(
				'<div class="call-tree-children call-tree-truncated">'
				f'<em>... {len(children)} child frame(s) hidden '
				f'(hard cap {_CALL_TREE_HARD_CAP} reached) ...</em>'
				'</div>'
			)
	out.append('</details>')
	return "".join(out)


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


def _render_call_tree_panel(actions):
	"""Phase K.5: render the call-tree panel sourced from the slowest
	action's ``call_tree_json``. Empty string when no action carries
	a tree (the template's ``{% if %}`` guard hides the section).
	"""
	if not actions:
		return ""
	# Pick the action with the largest duration_ms that also has a tree.
	candidates = [a for a in actions if isinstance(a, dict) and a.get("call_tree_json")]
	if not candidates:
		return ""
	top = max(candidates, key=lambda a: float(a.get("duration_ms") or 0))
	try:
		tree = json.loads(top.get("call_tree_json") or "{}")
	except Exception:
		return ""
	if not isinstance(tree, dict):
		return ""
	root_children = tree.get("children") or []
	if not root_children:
		return ""

	total_ms = float(tree.get("cumulative_ms") or 0) or float(top.get("duration_ms") or 0)
	action_label = top.get("action_label") or ""

	parts = [
		'<section class="section" id="call-tree">',
		'<div class="section-head">',
		'<h2>Call tree (top action)</h2>',
		f'<span class="section-tag">{_e(action_label)}</span>',
		'</div>',
		'<p class="section-intro">'
		'Hierarchical breakdown of where wall-clock time went inside the slowest '
		'action. The tree auto-opens down to your app\'s first hot frame; click any '
		'frame to expand or collapse it. Numbers are cumulative time (including '
		'children) and percentage of the parent. Branches consuming &ge;50% of their '
		'parent are highlighted as hot.'
		'</p>',
		'<div class="call-tree">',
	]
	root_children_sorted = sorted(
		root_children,
		key=lambda c: float((c or {}).get("cumulative_ms") or 0),
		reverse=True,
	)
	# The profiler attributes SQL queries as root-level siblings of the entry
	# frame, so the panel renders them directly (bypassing the per-node child
	# loop). Drop [other] nodes and <sql> query leaves here too.
	for c in root_children_sorted:
		cn = c or {}
		if _ct_is_other_frame(cn.get("function")) or _ct_is_sql_leaf(cn):
			continue
		parts.append(_render_call_tree_node(c, total_ms, depth=0))
	parts.append('</div>')
	parts.append('</section>')
	return "".join(parts)


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


def _format_duration_ms(ms, threshold_ms: float = 1000.0, decimals: int = 0):
	"""Render a duration as ``"<n>ms"`` (with ``decimals`` digits) — or, if it
	crosses ``threshold_ms``, as ``"<n.nn>s"`` (always 2 decimals). The
	``decimals`` arg controls only the ms branch so the existing ``%.1f`` /
	``%.2f`` callsites (sub-ms query timings) keep their resolution below the
	threshold. ``threshold_ms = 0`` disables the conversion.

	Defensive on input: ``None`` / non-numeric → ``"0ms"``; honours sign.

	v0.7.x: returns ``markupsafe.Markup`` so the seconds branch can
	emit a ``<span class="time-high">`` wrapper without being escaped
	when rendered via ``{{ fmt_ms(...) }}`` in Jinja. The wrapper draws
	the reader's eye to values slow enough to roll over into seconds —
	the timing rule itself is unchanged, just the visual emphasis is
	new. ``Markup`` subclasses ``str`` so Python callers that compare /
	concat the return value still work.
	"""
	try:
		v = float(ms) if ms is not None else 0.0
	except (TypeError, ValueError):
		return Markup("0ms")
	if threshold_ms and abs(v) >= threshold_ms:
		return Markup(f'<span class="time-high">{v / 1000:.2f}s</span>')
	return Markup(f"{v:.{decimals}f}ms")


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


def build_hot_frames_table(rows: list, is_hot: bool = False) -> list:
	"""Build the hot-frames leaderboard rows.

	``is_hot`` (Phase E): caller-controlled flag attached to each
	emitted dict so the template can apply `tr.is-hot` styling on the
	user-code rows (yellow tint) and leave the framework rows
	unmarked. The renderer always splits hot frames into user-vs-
	framework lists before calling this builder, so the flag is a
	per-list constant — True for the user-code table, False for the
	framework sibling.

	v0.7.x: ``is_hot`` also selects WHICH time metric the row
	displays. User-app frames (``is_hot=True``) keep the self-sum
	``total_ms`` (precise per A.AE1). Framework frames
	(``is_hot=False``) display ``total_cumulative_ms`` because
	wrapper self-time is sub-sampler-interval and rounds to 0 across
	every row. Framework rows are re-sorted by the cumulative metric
	so the column reads top-down by impact.
	"""
	out = []
	for row in rows or []:
		# The hot-frame key already encodes "<short_path>::<func>" — use it
		# directly. Routing through redact_frame_name appended a bogus "(?:0)"
		# (placeholder file "?" + lineno 0, since no file/line is known here).
		display = row.get("function") or "<unknown>"
		if is_hot:
			display_ms = row.get("total_ms", 0)
		else:
			display_ms = row.get(
				"total_cumulative_ms", row.get("total_ms", 0)
			)
		out.append({
			"display_name": display,
			"total_ms": display_ms,
			"occurrences": row.get("occurrences", 0),
			"distinct_actions": row.get("distinct_actions", 0),
			"action_refs": row.get("action_refs", []),
			"is_hot": is_hot,
		})
	# Framework variant ranks by the displayed (cumulative) metric —
	# the aggregator's outer sort was by self_ms, which produces
	# all-zero ties on framework rows.
	if not is_hot:
		out.sort(key=lambda r: r.get("total_ms", 0), reverse=True)
	return out
