# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.3 query-table layout fix.

The Top Queries and Queries-per-action tables used the default
``<table>`` CSS (auto layout, no column widths) and collapsed badly
when one row had an unusually long callsite path. A single
``frappe/frappe/model/db_query.py:255`` could grab 60-70% of the
row width, squeezing the Query column to a single-character sliver
with horizontal scroll.

Fix: dedicated ``.query-table`` class with fixed layout and
explicit <colgroup> widths. Long callsite code now wraps via
``word-break: break-all``.
"""

import json
import os
import re
import types


def _read_template() -> str:
	here = os.path.dirname(__file__)
	tpath = os.path.join(here, "..", "templates", "report.html")
	with open(tpath) as f:
		return f.read()


class TestTemplateStructure:
	def test_top_queries_table_has_query_table_class(self):
		tpl = _read_template()
		# Find the Top Queries section and verify the table inside
		# it uses the new class.
		m = re.search(
			r"Top \{\{[^}]+\}\}\s+slowest queries.*?</details>",
			tpl,
			re.DOTALL,
		)
		assert m is not None, "Top Queries section not found"
		section = m.group(0)
		assert 'class="query-table' in section, (
			"Top Queries table must carry the .query-table class for "
			"the fixed-layout CSS to apply"
		)

	def test_top_queries_has_colgroup_with_widths(self):
		tpl = _read_template()
		m = re.search(
			r"Top \{\{[^}]+\}\}\s+slowest queries.*?</details>",
			tpl,
			re.DOTALL,
		)
		section = m.group(0)
		# All four column classes must appear in the colgroup.
		for col_class in (
			"col-index", "col-duration", "col-callsite", "col-query",
		):
			assert f'class="{col_class}"' in section, (
				f"Top Queries colgroup missing {col_class!r}"
			)

	def test_queries_per_action_is_flat_table_without_sql(self):
		"""v0.7.x: Queries-per-action is now one flat data table (Server-Resource
		style), not per-action expanders, and the normalized-query column is gone."""
		tpl = _read_template()
		m = re.search(r"Queries per action.*?</section>", tpl, re.DOTALL)
		assert m is not None, "Queries per action section not found"
		section = m.group(0)
		assert 'class="data' in section            # flat data table…
		assert "queries-flat-table" in section      # …with fixed-column alignment
		assert 'class="query-table' not in section  # not the per-action query-table
		assert "<details" not in section            # no per-action expanders
		# The normalized-query column / SQL block is dropped.
		assert "Query (normalized)" not in section
		assert "sql-inline" not in section

	def test_queries_per_action_flat_table_has_action_and_callsite(self):
		"""The flat table carries an Action column + Duration / Copies / Callsite."""
		tpl = _read_template()
		m = re.search(r"Queries per action.*?</section>", tpl, re.DOTALL)
		section = m.group(0)
		assert "<th>Action</th>" in section
		assert "Callsite" in section
		assert "Copies" in section
		assert "col-query" not in section


class TestTemplateCSS:
	def test_fixed_layout_rule_present(self):
		tpl = _read_template()
		assert "table.query-table { table-layout: fixed; }" in tpl, (
			"table-layout: fixed must be set on .query-table — "
			"without it, browsers auto-size columns based on content "
			"and the layout collapses when one row has a long callsite"
		)

	def test_callsite_wrapping_rule_present(self):
		"""Long path segments without word boundaries need break-all
		so the callsite column wraps instead of overflowing."""
		tpl = _read_template()
		# The rule must apply inside .query-table's td.
		assert re.search(
			r"table\.query-table td code[^{]*\{[^}]*word-break:\s*break-all",
			tpl,
			re.DOTALL,
		), "break-all word-break must be set on callsite code inside query-table"

	def test_column_widths_defined(self):
		tpl = _read_template()
		# Each column class must have an explicit width.
		for col_class, expected_pattern in [
			("col-index", r"col\.col-index\s+\{[^}]*width:\s*\d+px"),
			("col-duration", r"col\.col-duration\s+\{[^}]*width:\s*\d+px"),
			("col-copies", r"col\.col-copies\s+\{[^}]*width:\s*\d+px"),
			("col-callsite", r"col\.col-callsite\s+\{[^}]*width:\s*\d+%"),
		]:
			assert re.search(expected_pattern, tpl), (
				f"{col_class} must have an explicit width CSS rule"
			)


class TestEndToEndRender:
	"""End-to-end: render a report with a long callsite path and
	verify the table layout classes / colgroup end up in the HTML."""

	def test_top_queries_renders_with_long_callsite(self):
		from optimus import renderer

		# A deeply-nested callsite path that would previously dominate
		# the column. 90+ chars is typical for a real app. (Must be a
		# custom-app path, not frappe/* — the Top Queries leaderboard is
		# user-app-only now, so a framework callsite would be filtered
		# out and the table wouldn't render at all.)
		long_callsite = (
			"apps/acme_reports/acme_reports/report/aged_payable_summary/"
			"aged_payable_summary.py:255 (get_item_details_from_custom_fields)"
		)

		doc = types.SimpleNamespace()
		doc.title = "T"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-17"
		doc.stopped_at = "2026-04-17"
		doc.notes = None
		doc.top_severity = "Low"
		doc.total_duration_ms = 1000
		doc.total_query_time_ms = 0
		doc.total_queries = 1
		doc.total_requests = 1
		doc.summary_html = None
		doc.top_queries_json = json.dumps([{
			"duration_ms": 1374.14,
			"callsite": long_callsite,
			"normalized_query": (
				"SELECT coalesce(SUM(grand_total), ?) "
				"FROM `tabPurchase Order` WHERE docstatus = ? "
				"AND wbs = ? AND company = ? "
				"AND MONTH(transaction_date) = ? AND name != ?"
			),
		}])
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = "{}"
		doc.actions = []
		doc.findings = []

		html = renderer.render(doc, recordings=[])

		# Table renders with the fixed-layout class.
		assert 'class="query-table' in html
		# Colgroup is emitted with all four columns.
		assert 'class="col-index"' in html
		assert 'class="col-duration"' in html
		assert 'class="col-callsite"' in html
		assert 'class="col-query"' in html
		# Callsite itself is visible.
		assert "aged_payable_summary.py:255" in html

	def test_queries_per_action_flat_table_renders_sorted(self):
		"""End-to-end: the flat Queries-per-action table renders queries from all
		actions, sorted by duration, with Action + Callsite and no SQL text."""
		from optimus import renderer

		action = types.SimpleNamespace(
			action_label="savedocs:Save", event_type="HTTP Request",
			http_method="POST", path="/api/method/x", recording_uuid="r0",
			duration_ms=500.0, queries_count=2, query_time_ms=100.0,
			slowest_query_ms=354.0,
		)
		doc = types.SimpleNamespace(
			name="PS", title="T", session_uuid="t", user="a", status="Ready",
			started_at="2026-04-17", stopped_at="2026-04-17", notes=None,
			top_severity="Low", total_duration_ms=1000, total_query_time_ms=100,
			total_queries=2, total_requests=1, summary_html=None,
			top_queries_json="[]", table_breakdown_json="[]", hot_frames_json="[]",
			session_time_breakdown_json="{}", total_python_ms=0, total_sql_ms=0,
			analyzer_warnings=None, v5_aggregate_json="{}",
			actions=[action], findings=[], phase_2_runs=[],
		)
		recordings = [{
			"uuid": "r0",
			"calls": [
				{"duration": 68.7, "normalized_query": "SELECT two", "normalized_copies": 5,
				 "exact_copies": 1,
				 "stack": [{"filename": "myapp/doc.py", "lineno": 340, "function": "g"}]},
				{"duration": 354.3, "normalized_query": "SELECT one", "normalized_copies": 1,
				 "exact_copies": 1,
				 "stack": [{"filename": "myapp/sales.py", "lineno": 570, "function": "f"}]},
			],
		}]
		html = renderer.render(doc, recordings=recordings)

		assert "savedocs:Save" in html
		assert "myapp/sales.py:570" in html and "myapp/doc.py:340" in html
		assert "354.3ms" in html
		# Sorted by duration desc: the 354ms query renders before the 68ms one.
		assert html.index("354.3ms") < html.index("68.7ms")
		# The normalized-query text is not shown in the Queries-per-action table.
		# (`.sql-inline` is a CSS class in <style>, so absence is asserted on the
		# section markup in TestTemplateStructure, not on the whole document.)
		assert "SELECT one" not in html
