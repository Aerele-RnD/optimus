# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.3.0 renderer output: donut + hot frames sections.

Also covers the backward-compat path: a v0.2.0-shaped session row
(no new fields) must still render without errors.
"""

import json
from types import SimpleNamespace

from optimus import renderer


def _fake_doc(**fields):
	"""Build a SimpleNamespace mimicking a Optimus Session doc."""
	defaults = {
		"name": "PS-test",
		"session_uuid": "test-uuid",
		"title": "test",
		"user": "tester@example.com",
		"status": "Ready",
		"started_at": "2026-04-13T00:00:00",
		"stopped_at": "2026-04-13T00:00:05",
		"total_duration_ms": 5000,
		"total_requests": 1,
		"total_queries": 0,
		"total_query_time_ms": 0,
		"analyze_duration_ms": 100,
		"top_severity": "None",
		"summary_html": "<p>summary</p>",
		"top_queries_json": "[]",
		"table_breakdown_json": "[]",
		"analyzer_warnings": None,
		"actions": [],
		"findings": [],
		# v0.3.0 fields
		"hot_frames_json": None,
		"session_time_breakdown_json": None,
		"total_python_ms": None,
		"total_sql_ms": None,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def test_render_raw_with_donut_and_hot_frames():
	doc = _fake_doc(
		session_time_breakdown_json=json.dumps({
			"sql_ms": 200,
			"python_ms": 800,
			"by_app": {"erpnext": 600, "my_acme_app": 200},
		}),
		hot_frames_json=json.dumps([
			{"function": "erpnext.selling.validate", "total_ms": 600,
			 "occurrences": 1, "distinct_actions": 1, "action_refs": [0]},
			{"function": "my_acme_app.discounts.calc", "total_ms": 200,
			 "occurrences": 1, "distinct_actions": 1, "action_refs": [0]},
		]),
		total_python_ms=800,
		total_sql_ms=200,
		total_duration_ms=1000,
		total_query_time_ms=200,
	)
	html = renderer.render_raw(doc, recordings=[])
	# v0.6.0: time breakdown was folded into the Total-time stat card.
	# v0.7.x Phase A: stat card → KPI strip, sub-label now reads
	# `<server>ms server · <db>ms DB` (tighter editorial copy).
	assert "ms server" in html
	assert "ms DB" in html
	# Hot frames section rendered with full app names (v0.6.0 Round 7
	# removed safe-mode app collapse).
	assert "Hot frames" in html
	assert "erpnext.selling.validate" in html
	assert "my_acme_app.discounts.calc" in html


def test_render_raw_shows_full_app_names():
	doc = _fake_doc(
		session_time_breakdown_json=json.dumps({
			"sql_ms": 100,
			"python_ms": 500,
			"by_app": {"my_acme_app": 500},
		}),
		hot_frames_json=json.dumps([
			{"function": "my_acme_app.discounts.calc", "total_ms": 500,
			 "occurrences": 1, "distinct_actions": 1, "action_refs": [0]},
		]),
	)
	html = renderer.render_raw(doc, recordings=[])
	# Raw mode: full app name visible in hot frames (donut legend was
	# removed in v0.6.0 when Time breakdown got folded into At a glance).
	assert "my_acme_app.discounts.calc" in html


def test_render_raw_old_session_no_v3_fields():
	"""A v0.2.0 session without donut/hot-frames data must still render."""
	doc = _fake_doc()  # all v0.3.0 fields are None
	html = renderer.render_raw(doc, recordings=[])
	# Hot frames section skipped when no data; donut was removed entirely.
	assert "Hot frames" not in html
	# But the rest of the report still renders
	assert "Summary" in html
	assert "Per-action breakdown" in html


def test_render_raw_with_invalid_json_in_v3_fields():
	"""Malformed JSON in the new fields must not crash the renderer."""
	doc = _fake_doc(
		session_time_breakdown_json="not-valid-json",
		hot_frames_json="also-not-valid",
	)
	html = renderer.render_raw(doc, recordings=[])
	# Renderer still produces some HTML
	assert "<html" in html or "<!DOCTYPE" in html.lower()
