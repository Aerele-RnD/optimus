# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""End-to-end smoke test: seed the v0.5.0 aggregate into a fake session
doc and render the full template. Verifies the Server Resource and
Frontend panels actually render without template errors and contain
the expected content.
"""

import json
import types


def _fake_session_doc(v5_aggregate):
	"""Minimal stub of a Optimus Session row good enough for renderer.render."""
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
	doc.analyzer_warnings = None
	doc.v5_aggregate_json = json.dumps(v5_aggregate)
	doc.actions = []
	doc.findings = []
	return doc


def test_safe_mode_renders_server_resource_panel():
	from optimus import renderer

	v5 = {
		"infra_timeline": [
			{
				"action_idx": 0,
				"action_label": "POST /api/method/save",
				"cpu": 92.0,
				"rss": 520_000_000,
				"load_1min": 4.2,
				"swap": 0,
				"db_threads_running": 8,
				"db_threads_connected": 12,
				"rq_default": 4,
				"rq_short": 0,
				"rq_long": 2,
			},
		],
		"infra_summary": {
			"cpu_avg": 92.0,
			"cpu_peak": 92.0,
			"rss_delta": 20_000_000,
			"load_peak": 4.2,
			"swap_peak_mb": 0,
			"rq_peak_depth": {"default": 4, "short": 0, "long": 2},
		},
		"frontend_xhr_matched": [],
		"frontend_vitals_by_page": {},
		"frontend_orphans": [],
		"frontend_summary": {},
	}

	doc = _fake_session_doc(v5)
	html = renderer.render(doc, recordings=[])

	assert "Server Resource" in html
	assert "POST /api/method/save" in html
	assert "92%" in html  # CPU peak rendered


def test_frontend_panel_renders_full_urls():
	# v0.6.0 Round 7: safe-mode URL redaction was removed. The Frontend
	# panel now shows full URLs as captured (admin-scoped report).
	from optimus import renderer

	v5 = {
		"infra_timeline": [],
		"infra_summary": {},
		"frontend_xhr_matched": [
			{
				"action_idx": 0,
				"action_label": "POST /api/method/save",
				"backend_ms": 320,
				"xhr_ms": 420,
				"network_delta_ms": 100,
				"response_size_bytes": 14200,
				"status": 200,
				"url": "/app/sales-invoice/SI-2026-00123/edit",
				"transport": "xhr",
			},
		],
		"frontend_vitals_by_page": {
			"/app/sales-invoice/SI-2026-00123": {
				"fcp_ms": 420,
				"lcp_ms": 2800,
				"cls": 0.02,
				"ttfb_ms": 180,
				"dom_content_loaded_ms": 890,
			},
		},
		"frontend_orphans": [],
		"frontend_summary": {
			"total_xhrs": 1,
			"total_xhr_ms": 420,
			"total_backend_ms": 320,
			"network_overhead_ms": 100,
		},
	}

	doc = _fake_session_doc(v5)
	html = renderer.render(doc, recordings=[])

	assert "Frontend" in html
	assert "SI-2026-00123" in html


def test_raw_mode_keeps_docname_in_urls():
	from optimus import renderer

	v5 = {
		"infra_timeline": [],
		"infra_summary": {},
		"frontend_xhr_matched": [
			{
				"action_idx": 0,
				"action_label": "save",
				"backend_ms": 100,
				"xhr_ms": 150,
				"network_delta_ms": 50,
				"response_size_bytes": 1000,
				"status": 200,
				"url": "/app/sales-invoice/SI-2026-00999",
				"transport": "xhr",
			},
		],
		"frontend_vitals_by_page": {},
		"frontend_orphans": [],
		"frontend_summary": {"total_xhrs": 1, "total_xhr_ms": 150,
		                    "total_backend_ms": 100, "network_overhead_ms": 50},
	}

	doc = _fake_session_doc(v5)
	html = renderer.render(doc, recordings=[])

	# Raw mode keeps full URLs.
	assert "SI-2026-00999" in html


def test_missing_v5_aggregate_degrades_cleanly():
	"""Sessions recorded before v0.5.0 have v5_aggregate_json unset.
	The renderer must fall back to empty values and skip the new panels
	without raising."""
	from optimus import renderer

	doc = _fake_session_doc({})
	doc.v5_aggregate_json = None  # simulate pre-v0.5.0 row

	html = renderer.render(doc, recordings=[])
	# The v0.5.0 section headings should NOT appear when there's no data.
	assert "Server Resource" not in html
	# Confirm the rest of the report still rendered.
	assert "Test session" in html


def test_orphans_section_appears_when_present():
	# v0.6.0 Round 7: safe-mode hide-orphans guard was removed; the
	# Orphaned XHRs section now always appears when frontend_orphans
	# is non-empty (single admin-scoped report).
	from optimus import renderer

	v5 = {
		"infra_timeline": [],
		"infra_summary": {},
		"frontend_xhr_matched": [
			{"action_idx": 0, "action_label": "save", "backend_ms": 100,
			 "xhr_ms": 150, "network_delta_ms": 50, "response_size_bytes": 0,
			 "status": 200, "url": "/api/method/foo", "transport": "xhr"},
		],
		"frontend_vitals_by_page": {},
		"frontend_orphans": [
			{"url": "/api/method/stale", "duration_ms": 80, "reason": "no_matching_recording"},
		],
		"frontend_summary": {"total_xhrs": 1},
	}
	doc = _fake_session_doc(v5)
	html = renderer.render(doc, recordings=[])

	assert "Orphaned XHRs" in html


def test_orphans_section_hidden_when_empty():
	from optimus import renderer

	v5 = {
		"infra_timeline": [],
		"infra_summary": {},
		"frontend_xhr_matched": [
			{"action_idx": 0, "action_label": "save", "backend_ms": 100,
			 "xhr_ms": 150, "network_delta_ms": 50, "response_size_bytes": 0,
			 "status": 200, "url": "/api/method/foo", "transport": "xhr"},
		],
		"frontend_vitals_by_page": {},
		"frontend_orphans": [],
		"frontend_summary": {"total_xhrs": 1},
	}
	doc = _fake_session_doc(v5)
	html = renderer.render(doc, recordings=[])

	assert "Orphaned XHRs" not in html
