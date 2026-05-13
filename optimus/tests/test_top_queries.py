# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for optimus.analyzers.top_queries."""

from optimus.analyzers import top_queries


def test_top_queries_sorted_by_duration_desc(full_scan_recording, empty_context):
	result = top_queries.analyze([full_scan_recording], empty_context)
	top = result.aggregate["top_queries"]
	assert len(top) == 3
	# Sorted desc
	assert top[0]["duration_ms"] >= top[1]["duration_ms"] >= top[2]["duration_ms"]


def test_slow_query_finding_for_long_queries(full_scan_recording, empty_context):
	"""Queries > 200ms should emit Slow Query findings."""
	result = top_queries.analyze([full_scan_recording], empty_context)
	# The full_scan fixture has 850ms and 920ms queries → two slow findings
	slow = [f for f in result.findings if f["finding_type"] == "Slow Query"]
	assert len(slow) >= 2
	# Severity High for >500ms
	assert all(f["severity"] == "High" for f in slow)


def test_top_callsite_from_business_code(full_scan_recording, empty_context):
	"""Top queries should report the business-logic callsite, not frappe internals."""
	result = top_queries.analyze([full_scan_recording], empty_context)
	top = result.aggregate["top_queries"]
	# All fixture frames are in acme_reports (custom app) — should see
	# those paths, not frappe. v0.5.2: renamed from erpnext because
	# erpnext is now classified as framework.
	for q in top:
		if q["callsite"]:
			assert "acme_reports/" in q["callsite"]


def test_clean_recording_emits_no_slow_query_findings(clean_recording, empty_context):
	"""Normal fast queries shouldn't generate findings."""
	result = top_queries.analyze([clean_recording], empty_context)
	assert result.findings == []


def test_empty_recordings(empty_context):
	result = top_queries.analyze([], empty_context)
	assert result.aggregate.get("top_queries") == []
	assert result.findings == []


def test_framework_callsite_queries_excluded_from_leaderboard(empty_context):
	"""v0.6.0: the slowest-queries leaderboard is scoped to the user's
	own app. A slow query whose blame frame is inside frappe/ or
	erpnext/ must not appear in ``top_queries`` even though it's slower
	than the user-app queries — it's noise the developer can't act on.
	The per-action breakdown still carries it."""
	recording = {
		"uuid": "r1",
		"calls": [
			{  # slowest, but framework-internal → excluded
				"query": "SELECT * FROM `tabSingles`",
				"normalized_query": "SELECT * FROM `tabSingles`",
				"duration": 900.0,
				"stack": [{"filename": "frappe/model/document.py", "lineno": 50}],
			},
			{  # erpnext-internal → also excluded
				"query": "SELECT name FROM `tabGL Entry`",
				"normalized_query": "SELECT name FROM `tabGL Entry`",
				"duration": 600.0,
				"stack": [{"filename": "erpnext/accounts/general_ledger.py", "lineno": 30}],
			},
			{  # user app → kept
				"query": "SELECT * FROM `tabSales Invoice` WHERE customer = 'X'",
				"normalized_query": "SELECT * FROM `tabSales Invoice` WHERE customer = ?",
				"duration": 120.0,
				"stack": [{"filename": "acme_app/acme_app/api.py", "lineno": 12}],
			},
		],
	}
	result = top_queries.analyze([recording], empty_context)
	top = result.aggregate["top_queries"]
	assert len(top) == 1
	assert top[0]["duration_ms"] == 120.0
	assert "acme_app/" in (top[0]["callsite"] or "")
	# The only query that survived the user-app filter is under the
	# 200ms slow-query threshold → no Slow Query finding either.
	assert [f for f in result.findings if f["finding_type"] == "Slow Query"] == []


def test_query_without_callsite_excluded_from_leaderboard(empty_context):
	"""A query the recorder couldn't attribute to a frame (None callsite)
	can't be tied to the user's app, so it's left out of the leaderboard."""
	recording = {
		"uuid": "r1",
		"calls": [
			{"query": "SELECT 1", "normalized_query": "SELECT 1",
			 "duration": 700.0, "stack": None},
			{"query": "SELECT 2 FROM `tabFoo`", "normalized_query": "SELECT 2 FROM `tabFoo`",
			 "duration": 80.0, "stack": [{"filename": "acme_app/x.py", "lineno": 3}]},
		],
	}
	result = top_queries.analyze([recording], empty_context)
	top = result.aggregate["top_queries"]
	assert len(top) == 1
	assert top[0]["duration_ms"] == 80.0


def test_trivially_fast_queries_excluded_from_leaderboard(empty_context):
	"""When every user-app query is sub-floor (a few ms each), the
	leaderboard stays empty rather than padding itself with queries that
	aren't worth singling out — there's no "reasonable" slowest query."""
	recording = {
		"uuid": "r1",
		"calls": [
			{"query": f"SELECT {i} FROM `tabFoo`", "normalized_query": "SELECT ? FROM `tabFoo`",
			 "duration": float(d), "stack": [{"filename": "acme_app/api.py", "lineno": i}]}
			for i, d in enumerate([3.1, 1.0, 6.5, 2.2, 9.9])  # all below the 10ms floor
		],
	}
	result = top_queries.analyze([recording], empty_context)
	assert result.aggregate["top_queries"] == []
	assert result.findings == []


def test_floor_keeps_queries_at_or_above_threshold(empty_context):
	"""A query exactly at the floor is kept; one just below is dropped."""
	recording = {
		"uuid": "r1",
		"calls": [
			{"query": "SELECT a FROM `tabFoo`", "normalized_query": "SELECT a FROM `tabFoo`",
			 "duration": 9.9, "stack": [{"filename": "acme_app/api.py", "lineno": 1}]},
			{"query": "SELECT b FROM `tabFoo`", "normalized_query": "SELECT b FROM `tabFoo`",
			 "duration": 10.0, "stack": [{"filename": "acme_app/api.py", "lineno": 2}]},
		],
	}
	result = top_queries.analyze([recording], empty_context)
	top = result.aggregate["top_queries"]
	assert len(top) == 1
	assert top[0]["duration_ms"] == 10.0
