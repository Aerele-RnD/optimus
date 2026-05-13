# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pins the removal of the 'Analyzer notes' section from the report.

History: pre-v0.5.2 the analyzer suppression warnings ("Suppressed N SQL
findings from framework code", "Skipped M non-SELECT statements", …) were
concatenated into a single <p> in the header — a wall of text. v0.5.2 moved
them to a collapsed-by-default section at the bottom with a short header
pointer. v0.6.x dropped the header pointer; then the section itself was
removed entirely (the suppression bookkeeping is debug noise the report
doesn't need to surface). ``session_doc.analyzer_warnings`` is still computed
and stored on the DocType — it's just no longer rendered. Truncation, the one
warning users *do* need to see, has its own prominent banner at the top
(``truncation_banner``) — independent of this.

This test makes sure a future edit can't accidentally re-introduce the
section, the header pointer, or the "Notes" link in the "Jump to:" nav.
"""

import types


def _fake_session_doc(warnings_str=None):
	doc = types.SimpleNamespace()
	doc.title = "Test session"
	doc.session_uuid = "test-uuid"
	doc.user = "alice@example.com"
	doc.status = "Ready"
	doc.started_at = "2026-04-14 10:00:00"
	doc.stopped_at = "2026-04-14 10:02:00"
	doc.notes = None
	doc.top_severity = "Medium"
	doc.total_duration_ms = 2000
	doc.total_query_time_ms = 500
	doc.total_queries = 20
	doc.total_requests = 2
	doc.summary_html = None
	doc.top_queries_json = "[]"
	doc.table_breakdown_json = "[]"
	doc.hot_frames_json = "[]"
	doc.session_time_breakdown_json = "{}"
	doc.total_python_ms = 100
	doc.total_sql_ms = 500
	doc.analyzer_warnings = warnings_str
	doc.v5_aggregate_json = "{}"
	doc.actions = []
	doc.findings = []
	return doc


def test_analyzer_notes_section_is_not_rendered_even_with_warnings():
	from optimus import renderer

	warnings_str = "\n".join([
		"Suppressed 52 SQL findings from framework code.",
		"Skipped 127 non-SELECT statements for index suggestions.",
		"⚠ TRUNCATED: 8 queries truncated.",
	])
	html = renderer.render(_fake_session_doc(warnings_str), recordings=[])

	# No section, no anchor, no nav link, no heading.
	assert 'id="analyzer-notes"' not in html
	assert 'href="#analyzer-notes"' not in html
	assert "<h2>Analyzer notes</h2>" not in html
	# Nor the old header shapes (pre-v0.5.2 wall of text / v0.5.2 pointer).
	assert "<strong>Warnings:</strong>" not in html
	assert "<strong>Analyzer notes:</strong>" not in html
	assert "suppressions recorded" not in html and "suppression recorded" not in html
	# The suppression explanations themselves are nowhere in the report.
	assert "Suppressed 52 SQL findings from framework code." not in html
	assert "Skipped 127 non-SELECT statements for index suggestions." not in html


def test_clean_session_also_renders_no_section():
	from optimus import renderer

	html = renderer.render(_fake_session_doc(None), recordings=[])
	assert 'id="analyzer-notes"' not in html
	assert 'href="#analyzer-notes"' not in html
