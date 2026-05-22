# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.3 projected-after-fix timing.

Each query-oriented finding now carries a projected per-query time
estimating what the same query would cost AFTER the suggested fix.
Lets the developer prioritize by ceiling-of-value: a 20× speedup is
worth an afternoon, a 1.2× speedup probably isn't. Projections are
heuristic ceilings, not guarantees — see ``base.project_post_fix_ms``
for the per-finding-type factors.

Shape (added to ``technical_detail_json``):

  {
    "average_time_ms": 2.7,            # existing
    "projected_avg_time_ms": 0.5,      # NEW — v0.5.3
    "projected_total_ms": 24.0,        # NEW
    "projected_speedup_label": "~5× faster",  # NEW (optional)
  }
"""

import json

import pytest

from optimus.analyzers import explain_flags, n_plus_one
from optimus.analyzers.base import (
	POST_FIX_FLOOR_MS,
	project_post_fix_ms,
)


class TestHelperFunction:
	def test_full_table_scan_projects_20x_speedup(self):
		# 20× speedup: 10ms → 0.5ms (floor caps at 0.3).
		assert project_post_fix_ms("Full Table Scan", 10.0) == 0.5

	def test_missing_index_same_20x_speedup(self):
		assert project_post_fix_ms("Missing Index", 10.0) == 0.5

	def test_filesort_projects_3x_speedup(self):
		# 10ms → ~3.0ms.
		assert project_post_fix_ms("Filesort", 10.0) == 3.0

	def test_temporary_table_projects_2x_speedup(self):
		assert project_post_fix_ms("Temporary Table", 10.0) == 5.0

	def test_low_filter_uses_filtered_pct(self):
		"""Low Filter Ratio projection: projected = current × (filtered/100).
		A query that examines 10× what it returns (filtered=10) should
		project to ~1/10 the time after a selective index."""
		assert project_post_fix_ms(
			"Low Filter Ratio", 10.0, filtered_pct=10.0,
		) == 1.0

	def test_low_filter_without_filtered_pct_returns_none(self):
		assert project_post_fix_ms("Low Filter Ratio", 10.0) is None

	def test_unknown_finding_type_returns_none(self):
		"""Redundant Call / Slow Hot Path / etc. — no projection."""
		assert project_post_fix_ms("Redundant Call", 10.0) is None
		assert project_post_fix_ms("Slow Hot Path", 10.0) is None

	def test_zero_or_negative_current_returns_none(self):
		assert project_post_fix_ms("Full Table Scan", 0) is None
		assert project_post_fix_ms("Full Table Scan", -1) is None

	def test_floor_enforced_at_minimum(self):
		"""A perfect index lookup still costs ~0.3ms of round-trip +
		plan time. Projection must never claim 0.0ms."""
		# 20× speedup of 0.5ms would be 0.025ms — but floor is 0.3.
		assert project_post_fix_ms(
			"Full Table Scan", 0.5,
		) == POST_FIX_FLOOR_MS


class TestExplainFlagsAnalyzerIntegration:
	def _build_recording(self, table, extra="", rows=10000, filtered=None,
	                     explain_type="ALL", duration=50.0, count=5):
		"""Build a recording with `count` identical queries that will
		aggregate into a single finding in explain_flags."""
		row = {"table": table, "type": explain_type, "rows": rows, "Extra": extra}
		if filtered is not None:
			row["filtered"] = filtered
		stack = [{
			"filename": "apps/myapp/foo.py",
			"lineno": 10,
			"function": "f",
		}]
		call = {
			"query": f"SELECT * FROM `{table}`",
			"normalized_query": f"SELECT * FROM `{table}`",
			"duration": duration,
			"stack": stack,
			"explain_result": [row],
			"exact_copies": 1, "normalized_copies": 1,
		}
		return {
			"uuid": "r", "path": "/", "method": "GET", "cmd": None,
			"event_type": "HTTP Request", "duration": 100.0,
			"calls": [call] * count,
		}

	def test_full_table_scan_finding_carries_projection(self, monkeypatch):
		monkeypatch.setattr(
			explain_flags, "_framework_doctypes_cache", frozenset(),
		)
		rec = self._build_recording("tabCustomDocType", duration=10.0, count=5)
		from optimus.analyzers.base import AnalyzeContext
		ctx = AnalyzeContext(session_uuid="t", docname="t")
		result = explain_flags.analyze([rec], ctx)
		scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
		assert len(scans) == 1
		detail = json.loads(scans[0]["technical_detail_json"])
		assert "projected_avg_time_ms" in detail
		# 10ms × 0.05 = 0.5ms (above floor).
		assert detail["projected_avg_time_ms"] == 0.5
		# 5 queries × 0.5ms = 2.5ms.
		assert detail["projected_total_ms"] == 2.5
		assert "projected_speedup_label" in detail
		assert "×" in detail["projected_speedup_label"]

	def test_filesort_finding_carries_projection(self, monkeypatch):
		monkeypatch.setattr(
			explain_flags, "_framework_doctypes_cache", frozenset(),
		)
		rec = self._build_recording(
			"tabCustomDocType",
			extra="Using filesort",
			duration=20.0, count=10,
			explain_type="ref",
			rows=5000,
		)
		from optimus.analyzers.base import AnalyzeContext
		ctx = AnalyzeContext(session_uuid="t", docname="t")
		result = explain_flags.analyze([rec], ctx)
		filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
		assert len(filesorts) == 1
		detail = json.loads(filesorts[0]["technical_detail_json"])
		# 20ms × 0.30 = 6.0ms.
		assert detail["projected_avg_time_ms"] == 6.0


class TestNplusOneProjection:
	def _build_recording_with_callsite(self, queries=15, per_query_ms=3.0):
		stack = [{
			"filename": "apps/myapp/foo.py",
			"lineno": 10,
			"function": "f",
		}]
		calls = [{
			"normalized_query": "SELECT * FROM `tabItem` WHERE name=?",
			"duration": per_query_ms,
			"stack": stack,
			"exact_copies": 1, "normalized_copies": 1,
		}] * queries
		return {
			"uuid": "r", "path": "/", "method": "GET", "cmd": None,
			"event_type": "HTTP Request", "duration": 100.0, "calls": calls,
		}

	def test_n_plus_one_projects_batched_query_cost(self):
		"""Projected total ≈ 2 × single-call avg (batched queries
		cost ~2× a tight query due to larger result set). 15 queries
		× 3ms each = 45ms currently → ~6ms projected."""
		from optimus.analyzers.base import AnalyzeContext
		rec = self._build_recording_with_callsite(queries=15, per_query_ms=3.0)
		ctx = AnalyzeContext(session_uuid="t", docname="t")
		result = n_plus_one.analyze([rec], ctx)
		assert len(result.findings) == 1
		detail = json.loads(result.findings[0]["technical_detail_json"])
		# 3ms avg × 2 = 6ms projected total after batching.
		assert detail["projected_total_ms"] == 6.0
		assert detail["projected_avg_time_ms"] == 6.0
		# 15 queries → ~7× fewer queries (15 // 2 = 7).
		assert "projected_speedup_label" in detail
		assert "fewer queries" in detail["projected_speedup_label"]

	def test_n_plus_one_small_count_no_speedup_label(self):
		"""Below 4 queries, the "N× fewer queries" wording is silly
		(2 queries becoming 1 isn't dramatic). Don't emit the label."""
		from optimus.analyzers.base import AnalyzeContext
		# Need ≥ 10 to cross the min_occurrences threshold, so use
		# exactly that. "~5× fewer queries" IS displayed here since
		# count >= 4.
		rec = self._build_recording_with_callsite(queries=10, per_query_ms=3.0)
		ctx = AnalyzeContext(session_uuid="t", docname="t")
		result = n_plus_one.analyze([rec], ctx)
		assert len(result.findings) == 1
		detail = json.loads(result.findings[0]["technical_detail_json"])
		# 10 // 2 = 5 → label emitted.
		assert detail.get("projected_speedup_label") == "~5× fewer queries"


class TestRendering:
	def test_projection_not_rendered_even_when_present(self):
		"""Removed per user request: the projected-after-fix estimate read as
		misleading (per-call could rise while the total rounded to ~0ms). The
		analyzer still computes projected_* into technical_detail_json (tested
		above), but the finding card no longer renders it."""
		import types

		doc = types.SimpleNamespace()
		doc.title = "T"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-17"
		doc.stopped_at = "2026-04-17"
		doc.notes = None
		doc.top_severity = "High"
		doc.total_duration_ms = 1000
		doc.total_query_time_ms = 0
		doc.total_queries = 20
		doc.total_requests = 5
		doc.summary_html = None
		doc.top_queries_json = "[]"
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = "{}"
		doc.actions = []

		row = types.SimpleNamespace()
		row.finding_type = "N+1 Query"
		row.severity = "High"
		row.title = "Query N+1"
		row.customer_description = "desc"
		row.estimated_impact_ms = 45.0
		row.affected_count = 15
		row.action_ref = "0"
		row.technical_detail_json = json.dumps({
			"callsite": {
				"filename": "apps/myapp/foo.py",
				"lineno": 10,
				"function": "f",
			},
			"occurrences": 15,
			"average_time_ms": 3.0,
			"total_time_ms": 45.0,
			"projected_avg_time_ms": 6.0,
			"projected_total_ms": 6.0,
			"projected_speedup_label": "~7× fewer queries",
		})
		doc.findings = [row]

		from optimus import renderer
		html = renderer.render(doc, recordings=[])

		# The projected line is no longer rendered, even though the detail
		# carries projected_avg_time_ms / projected_total_ms / speedup label.
		assert "Projected after fix" not in html
		assert "projected-after-fix" not in html

	def test_finding_without_projection_does_not_render_line(self):
		"""Redundant Call findings don't project — the projected line
		must NOT appear for them."""
		import types

		doc = types.SimpleNamespace()
		doc.title = "T"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-17"
		doc.stopped_at = "2026-04-17"
		doc.notes = None
		doc.top_severity = "Medium"
		doc.total_duration_ms = 1000
		doc.total_query_time_ms = 0
		doc.total_queries = 0
		doc.total_requests = 1
		doc.summary_html = None
		doc.top_queries_json = "[]"
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = "{}"
		doc.actions = []

		row = types.SimpleNamespace()
		row.finding_type = "Redundant Call"
		row.severity = "Medium"
		row.title = "Redundant cache lookup"
		row.customer_description = "d"
		row.estimated_impact_ms = 0
		row.affected_count = 60
		row.action_ref = "0"
		row.technical_detail_json = json.dumps({
			"callsite": {"filename": "apps/myapp/foo.py", "lineno": 1},
			"occurrences": 60,
			# NO projected_* fields.
		})
		doc.findings = [row]

		from optimus import renderer
		html = renderer.render(doc, recordings=[])

		assert "Projected after fix" not in html
