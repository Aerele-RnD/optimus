# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Smoke tests for the fixture builders — verify they produce shapes
that match the analyzers' expectations and can replace hand-written
JSON fixtures in most cases.
"""

from optimus.analyzers import n_plus_one
from optimus.tests.fixture_builders import (
	build_call,
	build_explain_row,
	build_recording,
)


def test_build_call_produces_analyzer_ready_dict():
	call = build_call(
		query="SELECT name FROM tabUser WHERE name = 'x'",
		duration=5.0,
		stack=[("apps/myapp/module.py", 100, "f")],
	)
	# Has all the fields the analyzers read
	assert "query" in call
	assert "normalized_query" in call
	assert "duration" in call
	assert "stack" in call
	assert call["stack"][0]["filename"] == "apps/myapp/module.py"
	assert call["stack"][0]["lineno"] == 100
	assert call["stack"][0]["function"] == "f"


def test_build_recording_defaults():
	r = build_recording()
	assert r["queries"] == 0
	assert r["calls"] == []
	assert r["event_type"] == "HTTP Request"


def test_build_recording_computes_totals():
	r = build_recording(
		calls=[
			build_call(duration=10.0),
			build_call(duration=20.0),
		]
	)
	assert r["queries"] == 2
	assert r["time_queries"] == 30.0


def test_builder_output_flows_through_n_plus_one(empty_context):
	"""Feed a builder-constructed recording through the N+1 analyzer."""
	recording = build_recording(
		calls=[
			build_call(
				query="SELECT name FROM tabItem WHERE item_code = 'X'",
				normalized_query="SELECT NAME FROM tabItem WHERE item_code = ?",
				duration=5.0,
				stack=[("apps/myapp/loop.py", 50, "iterate")],
			)
		]
		* 15
	)
	result = n_plus_one.analyze([recording], empty_context)
	# 15 copies of the same query from the same callsite → one N+1 finding
	assert len(result.findings) == 1
	assert result.findings[0]["affected_count"] == 15
	# v0.5.1: title uses short_filename (last 2 segments), not the full
	# path. Full path is still in customer_description and technical
	# detail for navigation.
	assert "myapp/loop.py" in result.findings[0]["title"]
	assert "apps/myapp/loop.py" in result.findings[0]["customer_description"]


def test_build_explain_row_with_all_flags():
	row = build_explain_row(
		table="tabGL Entry",
		type="ALL",
		rows=50000,
		filtered=2.0,
		extra="Using filesort; Using temporary",
	)
	assert row["table"] == "tabGL Entry"
	assert row["type"] == "ALL"
	assert row["rows"] == 50000
	assert row["filtered"] == 2.0
	assert "filesort" in row["Extra"]
