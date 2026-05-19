# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-level tests for the v0.6.x ``large_duration_threshold_ms`` setting.

The threshold controls how durations render in the report HTML: values at
or above the threshold display as seconds (e.g. ``5.23s``); below it, they
stay as milliseconds (``800ms``)."""

import types
from unittest.mock import patch

from optimus import renderer
from optimus.settings import OptimusConfig


def _action(**kw):
	base = {
		"action_label": "",
		"event_type": "HTTP Request",
		"http_method": "",
		"path": "",
		"recording_uuid": "",
		"duration_ms": 0,
		"queries_count": 0,
		"query_time_ms": 0,
		"slowest_query_ms": 0,
	}
	base.update(kw)
	return types.SimpleNamespace(**base)


def _doc(actions):
	return types.SimpleNamespace(
		name="PS-t", session_uuid="t", title="t",
		user="a@example.com", status="Ready",
		started_at="2026-05-13T00:00:00", stopped_at="2026-05-13T00:00:06",
		notes=None, top_severity="Low", summary_html=None,
		total_duration_ms=6034, total_query_time_ms=80,
		total_queries=5, total_requests=2,
		top_queries_json="[]", table_breakdown_json="[]",
		hot_frames_json="[]", session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions, findings=[], phase_2_runs=[],
	)


class TestDefaultThreshold:
	def test_slow_row_renders_in_seconds(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
			_action(action_label="POST /api/method/myapp.fast",
			        http_method="POST", path="/api/method/myapp.fast",
			        recording_uuid="r1", duration_ms=800),
		])
		html = renderer.render_raw(doc, recordings=[])

		# Slow action: 5234ms → "5.23s" appears, the raw ms form does NOT.
		assert ">5.23s<" in html
		assert ">5234ms<" not in html
		# Fast action stays in ms.
		assert ">800ms<" in html
		# Footer stamps the default threshold (1000).
		assert "large_duration_threshold_ms=1000" in html


class TestDisabledThreshold:
	def test_threshold_zero_keeps_everything_in_ms(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=0),
		):
			html = renderer.render_raw(doc, recordings=[])

		assert ">5234ms<" in html
		assert ">5.23s<" not in html
		assert "large_duration_threshold_ms=0" in html


class TestHighThreshold:
	def test_threshold_above_all_values_keeps_everything_in_ms(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=10000),
		):
			html = renderer.render_raw(doc, recordings=[])

		assert ">5234ms<" in html
		assert ">5.23s<" not in html
		assert "large_duration_threshold_ms=10000" in html


class TestLowThreshold:
	def test_threshold_500ms_converts_everything_above(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
			_action(action_label="POST /api/method/myapp.medium",
			        http_method="POST", path="/api/method/myapp.medium",
			        recording_uuid="r1", duration_ms=800),
			_action(action_label="POST /api/method/myapp.fast",
			        http_method="POST", path="/api/method/myapp.fast",
			        recording_uuid="r2", duration_ms=200),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=500),
		):
			html = renderer.render_raw(doc, recordings=[])

		# Both 5234ms (5.23s) and 800ms (0.80s) cross the 500ms threshold.
		assert ">5.23s<" in html
		assert ">0.80s<" in html
		# 200ms stays as ms.
		assert ">200ms<" in html
