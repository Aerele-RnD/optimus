# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.3 adaptive truncation cap + prominent banner.

Two related changes:

  1. ``max_queries_per_recording`` is now admin-configurable via
     Optimus Settings (default 2000). Long flows (Manufacturing
     Plan Submit with 3000+ queries/recording) can raise it to
     5000 or 10000 to get full-coverage analysis.

  2. When truncation happens, a prominent red banner renders at the
     TOP of the report — above the exec-summary card. Previously
     the warning was buried in the collapsed Analyzer Notes at the
     bottom, and developers read incomplete reports without
     noticing.
"""

import json
import types


def _fake_session_doc(analyzer_warnings=None):
	doc = types.SimpleNamespace()
	doc.title = "Test"
	doc.session_uuid = "t"
	doc.user = "a"
	doc.status = "Ready"
	doc.started_at = "2026-04-17"
	doc.stopped_at = "2026-04-17"
	doc.notes = None
	doc.top_severity = "High"
	doc.total_duration_ms = 2000
	doc.total_query_time_ms = 0
	doc.total_queries = 3000
	doc.total_requests = 10
	doc.summary_html = None
	doc.top_queries_json = "[]"
	doc.table_breakdown_json = "[]"
	doc.hot_frames_json = "[]"
	doc.session_time_breakdown_json = "{}"
	doc.total_python_ms = 0
	doc.total_sql_ms = 0
	doc.analyzer_warnings = analyzer_warnings
	doc.v5_aggregate_json = "{}"
	doc.actions = []
	doc.findings = []
	return doc


class TestTruncationBannerRenders:
	def test_banner_renders_when_truncated_warning_present(self):
		"""A warning line starting with '⚠ TRUNCATED:' must surface as
		a prominent banner at the top of the report, NOT just as a
		line in the collapsed Analyzer Notes."""
		from optimus import renderer

		warning = (
			"⚠ TRUNCATED: 566 queries (17% of the flow) exceeded the "
			"2000-queries-per-recording enrichment cap and were "
			"analyzed without EXPLAIN / normalization. "
			"To get full coverage, raise <b>Optimus Settings ▸ Max "
			"Queries per Recording</b> (default 2000, try 5000-10000) "
			"and re-run the session."
		)
		doc = _fake_session_doc(analyzer_warnings=warning)
		html = renderer.render(doc, recordings=[])

		# Banner CSS class must be present.
		assert 'class="truncation-banner"' in html, (
			"Truncation banner div must render when the warning is set"
		)
		# Banner title is visible.
		assert "Report is partial" in html
		# The warning text is in the banner body.
		assert "566 queries" in html
		assert "17%" in html
		# Pointer to the specific setting so the admin knows exactly
		# what to raise.
		assert "Max Queries per Recording" in html

	def test_banner_renders_above_tldr_hero(self):
		"""Structural: the truncation banner must appear BEFORE the
		TL;DR hero in document order. Otherwise readers who stop
		scrolling at the headline won't see the warning.
		v0.7.x redesign: the exec-summary card was replaced by the
		TL;DR hero (mock spec). The banner-must-come-before-the-most-
		prominent-content rule still applies — the prominent block
		just changed identity."""
		from optimus import renderer

		warning = "⚠ TRUNCATED: 100 queries (5% of the flow) exceeded ..."
		doc = _fake_session_doc(analyzer_warnings=warning)
		# Put ONE finding in so the TL;DR has a finding-keyed headline
		# (clean-session branch would also work for the order check,
		# but exercising the populated path is closer to real reports).
		row = types.SimpleNamespace()
		row.finding_type = "N+1 Query"
		row.severity = "High"
		row.title = "Same query ran 50×"
		row.customer_description = "desc"
		row.estimated_impact_ms = 500.0
		row.affected_count = 50
		row.action_ref = "0"
		# v0.7.x: findings with no callsite are filtered before render.
		row.technical_detail_json = json.dumps({
			"callsite": {"filename": "apps/myapp/foo.py", "lineno": 1, "function": "f"},
		})
		doc.findings = [row]

		html = renderer.render(doc, recordings=[])
		banner_idx = html.find('class="truncation-banner"')
		tldr_idx = html.find('class="tldr"')
		assert banner_idx > 0
		assert tldr_idx > 0
		assert banner_idx < tldr_idx, (
			f"Truncation banner must render BEFORE the TL;DR hero. "
			f"Got banner at {banner_idx}, TL;DR at {tldr_idx}."
		)

	def test_banner_absent_on_clean_session(self):
		"""No warning → no banner. Sanity."""
		from optimus import renderer
		doc = _fake_session_doc(analyzer_warnings=None)
		html = renderer.render(doc, recordings=[])
		assert 'class="truncation-banner"' not in html

	def test_banner_absent_when_warnings_are_not_truncation(self):
		"""Other analyzer warnings (framework-filter, alias-suppression,
		etc.) must NOT trigger the truncation banner — it's specifically
		for the TRUNCATED marker. (The 'Analyzer notes' section that used
		to list these was removed, so the warning isn't rendered at all.)"""
		from optimus import renderer
		warning = (
			"Suppressed SQL findings from 41 call(s) whose callsite was "
			"inside Frappe framework code."
		)
		doc = _fake_session_doc(analyzer_warnings=warning)
		html = renderer.render(doc, recordings=[])
		assert 'class="truncation-banner"' not in html
		# Non-truncation warnings have no home in the report anymore.
		assert "Suppressed SQL findings" not in html

	def test_banner_picks_out_the_truncation_line_among_mixed_warnings(self):
		"""A session with BOTH a truncation line AND other analyzer
		warnings: the banner renders for the truncation line; the other
		(non-truncation) warnings aren't surfaced anywhere (the 'Analyzer
		notes' section that used to list them was removed)."""
		from optimus import renderer
		warnings = "\n".join([
			"⚠ TRUNCATED: 200 queries (10% of the flow) exceeded ...",
			"Suppressed 5 findings from framework callsites.",
			"Skipped 30 non-SELECT statements.",
		])
		doc = _fake_session_doc(analyzer_warnings=warnings)
		html = renderer.render(doc, recordings=[])
		# Banner present, with the truncation line's text.
		assert 'class="truncation-banner"' in html
		assert "200 queries" in html
		# The non-truncation lines are gone.
		assert "5 findings from framework" not in html
		assert "30 non-SELECT" not in html


class TestSettingsDocTypeField:
	"""Make sure the Settings DocType JSON actually exposes the
	max_queries_per_recording field with a sane default."""

	def test_json_has_max_queries_field(self):
		import os
		p = os.path.join(
			os.path.dirname(__file__),
			"..", "optimus", "doctype", "optimus_settings",
			"optimus_settings.json",
		)
		with open(p) as f:
			data = json.load(f)
		fields = {f["fieldname"]: f for f in data["fields"]}
		assert "max_queries_per_recording" in fields, (
			"Optimus Settings must expose max_queries_per_recording"
		)
		field = fields["max_queries_per_recording"]
		assert field["fieldtype"] == "Int"
		assert field["non_negative"] == 1
		assert field["default"] == "2000"
		# Must appear in field_order so it actually shows in the UI.
		assert "max_queries_per_recording" in data["field_order"]


class TestBannerMarkerPrefix:
	"""Guard: the analyze.py warning MUST start with the marker
	prefix that renderer looks for. If someone changes either side
	without updating the other, the banner silently breaks."""

	def test_warning_prefix_matches_renderer_check(self):
		import os
		import re

		# Read the analyze.py that emits the warning.
		analyze_path = os.path.join(
			os.path.dirname(__file__), "..", "analyze.py",
		)
		with open(analyze_path) as f:
			analyze_src = f.read()

		# Read the renderer that looks for the prefix. v0.10.0 split the
		# monolithic renderer.py into a package; the truncation-check still
		# lives inside the legacy bulk content at renderer/_internal.py.
		renderer_path = os.path.join(
			os.path.dirname(__file__), "..", "renderer", "_internal.py",
		)
		with open(renderer_path) as f:
			renderer_src = f.read()

		# Both sides must use the same marker.
		marker = "⚠ TRUNCATED:"
		assert marker in analyze_src, (
			"analyze.py must emit the truncation warning starting "
			f"with {marker!r}"
		)
		# renderer's .startswith() call references the same prefix.
		assert f'startswith("{marker}")' in renderer_src or (
			f"startswith('{marker}')" in renderer_src
		), (
			f"renderer.py must detect truncation via "
			f"startswith({marker!r})"
		)
