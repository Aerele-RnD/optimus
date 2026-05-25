# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Structural DOM snapshot of ``renderer.render_raw`` against a synthetic
fixture session — the canary that protects the template contract during
the v0.10.0+ renderer split.

The existing renderer-touching tests (46 files) all assert *content* —
"the string '50× hits' appears in the HTML" — but none of them lock the
*structure* (tag nesting, CSS class names, section IDs, data attributes).
A refactor that quietly renamed ``<div class="finding-card">`` to
``<section class="finding">`` would pass every existing test and break
the (frozen) template's CSS. This file closes that gap.

The fingerprint is structural, not byte-for-byte:

  * ``section_ids`` — sorted list of every ``id="..."`` attribute value
    that appears in the document. Catches a section being silently
    dropped or renamed.

  * ``class_names`` — sorted multiset of every distinct ``class="..."``
    token (split on whitespace). Catches a CSS class being renamed,
    added, or removed.

  * ``tag_counts`` — total count per tag name across the whole
    document. Coarse DOM-shape sanity; catches gross structural drift
    (e.g. ``<div>`` → ``<section>`` mass rename).

Byte-for-byte would be too brittle (Pygments token ordering across
versions, dict iteration in JSON, etc.). The structural shape drifts
slowly and intentionally — when a legitimate template change lands,
the test fails with a focused diff and the contributor regenerates the
snapshot via ``REGENERATE_RENDERER_SNAPSHOT=1 pytest``.

This file also enumerates the renderer package's public API and asserts
each name still resolves after the split. The five 'public + semi-public'
symbols (``render_raw``, ``_finding_to_dict``, ``build_donut_svg``,
``build_hot_frames_table``, ``_BoundedFileCache``) are the contract
``analyze.py`` and external callers rely on.
"""

from __future__ import annotations

import html.parser
import json
import os
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import renderer

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "renderer_structure.json"
_REGENERATE_ENV = "REGENERATE_RENDERER_SNAPSHOT"


# --------------------------------------------------------------------------
# Fixture — covers as many of the 14 conditional sections as practical
# --------------------------------------------------------------------------


def _snippet(lineno: int = 41) -> list[dict]:
	return [{"lineno": lineno, "content": f"for u in users:  # line {lineno}"}]


def _finding(
	finding_type: str = "N+1 Query",
	severity: str = "High",
	impact: float = 420.0,
	count: int = 50,
	action_ref: str = "0",
	title: str | None = None,
	llm_fix_json: str | None = None,
) -> SimpleNamespace:
	detail = {
		"callsite": {
			"filename": "/abs/myapp/forms/invoice.py",
			"lineno": 41,
			"function": "bulk",
			"source_snippet": _snippet(),
		}
	}
	return SimpleNamespace(
		finding_type=finding_type,
		severity=severity,
		title=title or f"{finding_type} at invoice.py:41",
		customer_description="Detailed description.",
		estimated_impact_ms=impact,
		affected_count=count,
		action_ref=action_ref,
		technical_detail_json=json.dumps(detail),
		llm_fix_json=llm_fix_json,
	)


def _action(idx: int, **kw) -> SimpleNamespace:
	base = dict(
		action_label=f"action_{idx}",
		event_type="HTTP Request",
		http_method="POST",
		path=f"/api/method/x{idx}",
		recording_uuid=f"r{idx}",
		duration_ms=900.0,
		queries_count=2,
		query_time_ms=200.0,
		slowest_query_ms=150.0,
	)
	base.update(kw)
	return SimpleNamespace(**base)


def _v5_aggregate() -> dict:
	"""v5_aggregate_json shape that triggers server-resource + frontend
	panels. Numbers picked to look plausible (matches v0.5.0 test fixtures)."""
	return {
		"infra_timeline": [
			{
				"action_idx": 0,
				"action_label": "POST /api/method/x0",
				"cpu": 78.5,
				"rss": 480_000_000,
				"load_1min": 3.1,
				"swap": 0,
				"db_threads_running": 6,
				"db_threads_connected": 10,
				"rq_default": 2,
				"rq_short": 0,
				"rq_long": 1,
			},
		],
		"infra_summary": {
			"cpu_avg": 78.5,
			"cpu_peak": 78.5,
			"rss_delta": 15_000_000,
			"load_peak": 3.1,
			"swap_peak_mb": 0,
			"rq_peak_depth": {"default": 2, "short": 0, "long": 1},
		},
		"frontend_xhr_matched": [
			{
				"action_idx": 0,
				"action_label": "POST /api/method/x0",
				"backend_ms": 280,
				"xhr_ms": 380,
				"network_delta_ms": 100,
				"response_size_bytes": 12000,
				"status": 200,
				"url": "/app/sales-invoice/SI-2026-00001",
				"transport": "xhr",
			},
		],
		"frontend_vitals_by_page": {
			"/app/sales-invoice/SI-2026-00001": {
				"fcp_ms": 410,
				"lcp_ms": 2650,
				"cls": 0.03,
				"ttfb_ms": 175,
				"dom_content_loaded_ms": 870,
			},
		},
		"frontend_orphans": [],
		"frontend_summary": {
			"total_xhrs": 1,
			"total_xhr_ms": 380,
			"total_backend_ms": 280,
			"network_overhead_ms": 100,
		},
	}


def _snapshot_doc() -> SimpleNamespace:
	"""Synthetic Optimus Session that exercises the major conditional
	sections — findings (actionable + observational), actions (HTTP +
	RQ Job → waterfall + background-jobs), top_queries, table_breakdown,
	and the v5 server-resource + frontend panels."""
	findings = [
		_finding("N+1 Query", "High", 420.0, 50, action_ref="0"),
		_finding(
			"Slow Query",
			"Medium",
			180.0,
			3,
			action_ref="0",
			title="Slow Query at invoice.py:120",
		),
	]
	actions = [
		_action(0, duration_ms=900.0),
		_action(
			1,
			event_type="RQ Job",
			action_label="bg_job.dispatch",
			duration_ms=400.0,
			http_method=None,
			path=None,
		),
	]
	top_queries = [
		{
			"normalized_query": "SELECT * FROM `tabSales Invoice Item` WHERE parent = ?",
			"total_ms": 320.0,
			"count": 12,
			"avg_ms": 26.7,
			"slowest_ms": 95.0,
			"sample_query": "SELECT * FROM `tabSales Invoice Item` WHERE parent = 'SI-1'",
		},
	]
	table_breakdown = [
		{
			"table": "tabSales Invoice Item",
			"doctype": "Sales Invoice Item",
			"read_count": 24,
			"write_count": 0,
			"total_ms": 320.0,
			"is_write_hot": False,
		},
	]
	return SimpleNamespace(
		name="PS-snap",
		session_uuid="snap-uuid",
		title="snapshot test",
		user="snapshot@example.com",
		status="Ready",
		started_at="2026-05-22T00:00:00",
		stopped_at="2026-05-22T00:00:05",
		notes=None,
		top_severity="High",
		summary_html=None,
		total_duration_ms=1300,
		total_query_time_ms=200,
		total_queries=2,
		total_requests=1,
		top_queries_json=json.dumps(top_queries),
		table_breakdown_json=json.dumps(table_breakdown),
		hot_frames_json=None,
		session_time_breakdown_json=None,
		total_python_ms=None,
		total_sql_ms=None,
		analyzer_warnings=None,
		v5_aggregate_json=json.dumps(_v5_aggregate()),
		actions=actions,
		findings=findings,
		phase_2_runs=[],
	)


# --------------------------------------------------------------------------
# Fingerprint extraction
# --------------------------------------------------------------------------


class _StructuralExtractor(html.parser.HTMLParser):
	"""Walks a rendered HTML document and accumulates the three structural
	counters described in the module docstring. Cheap (one pass, stdlib
	only, no DOM build)."""

	def __init__(self):
		super().__init__()
		self.section_ids: list[str] = []
		self.class_names: Counter = Counter()
		self.tag_counts: Counter = Counter()

	def handle_starttag(self, tag, attrs):
		self.tag_counts[tag] += 1
		attrs_dict = dict(attrs)
		id_val = (attrs_dict.get("id") or "").strip()
		if id_val:
			self.section_ids.append(id_val)
		cls_val = (attrs_dict.get("class") or "").strip()
		if cls_val:
			for token in cls_val.split():
				self.class_names[token] += 1


def _fingerprint(html_text: str) -> dict:
	p = _StructuralExtractor()
	p.feed(html_text)
	return {
		"section_ids": sorted(p.section_ids),
		"class_names": dict(sorted(p.class_names.items())),
		"tag_counts": dict(sorted(p.tag_counts.items())),
	}


def _diff_message(expected: dict, actual: dict) -> str:
	"""Return a focused human-readable diff so a snapshot mismatch points
	straight at what drifted. Cheap to compute — only used on failure."""
	msgs = []
	e_ids = set(expected.get("section_ids", []))
	a_ids = set(actual.get("section_ids", []))
	if e_ids != a_ids:
		removed = sorted(e_ids - a_ids)
		added = sorted(a_ids - e_ids)
		msgs.append(f"section_ids: removed={removed}, added={added}")
	e_cls = set(expected.get("class_names", {}))
	a_cls = set(actual.get("class_names", {}))
	if e_cls != a_cls:
		removed = sorted(e_cls - a_cls)[:20]
		added = sorted(a_cls - e_cls)[:20]
		msgs.append(f"class_names: removed={removed!r}, added={added!r}")
	e_tags = expected.get("tag_counts", {})
	a_tags = actual.get("tag_counts", {})
	tag_diffs = []
	for t in sorted(set(e_tags) | set(a_tags)):
		ev, av = e_tags.get(t, 0), a_tags.get(t, 0)
		if ev != av:
			tag_diffs.append(f"<{t}>: {ev} → {av}")
	if tag_diffs:
		msgs.append("tag_counts changed: " + ", ".join(tag_diffs[:15]))
	return "\n  ".join(msgs) or "fingerprint mismatch (no obvious diff — re-read both)"


# --------------------------------------------------------------------------
# The canary test — fingerprint matches golden (or regenerates it)
# --------------------------------------------------------------------------


class TestStructureSnapshot:
	def test_fingerprint_matches_golden(self):
		doc = _snapshot_doc()
		html_text = renderer.render_raw(doc, recordings=[])
		actual = _fingerprint(html_text)

		if os.environ.get(_REGENERATE_ENV) == "1":
			GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
			with open(GOLDEN_PATH, "w") as f:
				json.dump(actual, f, indent=2, sort_keys=True)
				f.write("\n")
			pytest.skip(
				f"regenerated structural snapshot at {GOLDEN_PATH}; "
				"unset REGENERATE_RENDERER_SNAPSHOT to run in compare mode"
			)

		assert GOLDEN_PATH.exists(), (
			f"Snapshot fixture missing: {GOLDEN_PATH}. "
			f"Regenerate with: {_REGENERATE_ENV}=1 pytest "
			"optimus/tests/test_renderer_structure_snapshot.py"
		)
		with open(GOLDEN_PATH) as f:
			expected = json.load(f)
		assert actual == expected, "\n  " + _diff_message(expected, actual)

	def test_self_containment_invariant(self):
		"""Duplicates the canary assertion from test_report_a11y so the
		snapshot fixture is itself locked to never grow a remote-fetch URL —
		even if the per-section extractions add a section that accidentally
		imports something with a network side-effect."""
		doc = _snapshot_doc()
		html_text = renderer.render_raw(doc, recordings=[])
		assert "<script" not in html_text.lower(), "snapshot fixture must not produce <script>"
		assert not re.search(r'src\s*=\s*["\']https?:', html_text), (
			"snapshot fixture must not produce a remote src=… load"
		)
		assert not re.search(
			r'<link\b[^>]*href\s*=\s*["\']https?:', html_text
		), "snapshot fixture must not produce a remote stylesheet link"
		assert "@import" not in html_text, "snapshot fixture must not use @import"
		stripped = html_text.replace(" ", "").replace("'", "").replace('"', "")
		assert "url(http" not in stripped, (
			"snapshot fixture must not use a remote url(...) in CSS"
		)

	def test_minimum_sections_present(self):
		"""Independent of the byte-match — at least the sections triggered
		by this fixture must appear. Guards against a future extraction
		that silently drops a section from the template-context dict (the
		``{% if report_data.X %}`` gate stays open but the data is missing)."""
		doc = _snapshot_doc()
		html_text = renderer.render_raw(doc, recordings=[])
		# These IDs / labels are anchors the existing test suite already
		# leans on; documenting them here makes the dependency explicit.
		expected_substrings = [
			'id="actions"',          # per-action breakdown alias
			'id="per-action"',        # per-action section proper
			'id="findings"',          # actionable findings
			'id="top-queries"',       # slowest-queries section
			'id="db-tables"',         # table breakdown
			'id="server-resource"',   # v5 infra panel
			'id="frontend"',          # v5 frontend panel
			'id="waterfall"',         # action timeline
			'id="background-jobs"',   # RQ jobs section (RQ Job action in fixture)
		]
		missing = [s for s in expected_substrings if s not in html_text]
		assert not missing, (
			f"snapshot fixture missing expected section anchors: {missing}"
		)


# --------------------------------------------------------------------------
# Import-surface tests — the contract that survives the split
# --------------------------------------------------------------------------


class TestPublicAPIPreserved:
	"""The names ``analyze.py``, ``api.py``, and the existing tests rely on
	MUST keep resolving after the file → package split. Re-export shim
	regressions surface here."""

	def test_render_raw_resolves(self):
		assert callable(getattr(renderer, "render_raw", None))

	def test_render_resolves(self):
		assert callable(getattr(renderer, "render", None))

	def test_finding_to_dict_resolves(self):
		# Semi-public — used by analyze.py for the AI auto-suggest payload.
		assert callable(getattr(renderer, "_finding_to_dict", None))

	def test_build_donut_svg_resolves(self):
		# Public — passed into the template context.
		assert callable(getattr(renderer, "build_donut_svg", None))

	def test_build_hot_frames_table_resolves(self):
		assert callable(getattr(renderer, "build_hot_frames_table", None))

	def test_redact_frame_name_resolves(self):
		assert callable(getattr(renderer, "redact_frame_name", None))

	def test_bounded_file_cache_resolves(self):
		# Used by analyze.py to pre-warm the cache before bulk enrichment.
		assert getattr(renderer, "_BoundedFileCache", None) is not None

	def test_read_source_snippet_resolves(self):
		# Heavy semi-public — many analyze + test callers.
		assert callable(getattr(renderer, "_read_source_snippet", None))

	def test_markdown_to_safe_html_resolves(self):
		# Used by analyze.py for AI auto-suggest rendering.
		assert callable(getattr(renderer, "_markdown_to_safe_html", None))

	def test_build_line_drilldown_callsite_index_resolves(self):
		assert callable(getattr(renderer, "_build_line_drilldown_callsite_index", None))


class TestNoCircularImports:
	"""Defensive: confirm a fresh interpreter can load the renderer
	package, then analyze, then api, with no circular-import deadlock or
	ImportError. Re-running this with `importlib.reload` after a split
	would catch a regression where a sibling submodule accidentally
	imports back from `_internal.py`."""

	def test_renderer_package_imports_clean(self):
		import importlib

		# Force a re-import in case prior tests cached the package state.
		for name in ("optimus.renderer", "optimus.analyze", "optimus.api"):
			if name in __import__("sys").modules:
				importlib.reload(__import__("sys").modules[name])
		import optimus.analyze  # noqa: F401
		import optimus.renderer  # noqa: F401
		# api.py is heavier; tolerate ImportError caused by missing optional
		# Frappe modules but never circular-import related errors.
		try:
			import optimus.api  # noqa: F401
		except Exception as exc:
			# Surface circular-import problems but tolerate the rest.
			assert "circular" not in str(exc).lower() and "partially initialized" not in str(
				exc
			).lower(), f"circular import on api: {exc!r}"
