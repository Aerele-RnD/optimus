# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for optimus.analyzers.table_breakdown."""

import pytest

# sql_metadata is a frappe dependency; skip gracefully if the test env
# doesn't have it (unlikely since it ships with frappe)
sql_metadata = pytest.importorskip("sql_metadata")

from optimus.analyzers import table_breakdown


def test_single_table_aggregated(clean_recording, empty_context):
	result = table_breakdown.analyze([clean_recording], empty_context)
	breakdown = result.aggregate["table_breakdown"]
	# Both queries in clean_recording touch tabCustomer
	tables = [b["table"] for b in breakdown]
	assert "tabCustomer" in tables
	customer_row = next(b for b in breakdown if b["table"] == "tabCustomer")
	assert customer_row["queries"] == 2
	# Sum of 18 + 20 = 38 ms
	assert customer_row["duration_ms"] == pytest.approx(38.0, abs=0.5)


def test_sorted_by_duration_desc(empty_context):
	recording = {
		"uuid": "tb1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": "SELECT * FROM tabSmall",
				"normalized_query": "SELECT * FROM tabSmall",
				"duration": 10.0,
				"stack": [],
			},
			{
				"query": "SELECT * FROM tabBig",
				"normalized_query": "SELECT * FROM tabBig",
				"duration": 200.0,
				"stack": [],
			},
			{
				"query": "SELECT * FROM tabMedium",
				"normalized_query": "SELECT * FROM tabMedium",
				"duration": 50.0,
				"stack": [],
			},
		],
	}
	result = table_breakdown.analyze([recording], empty_context)
	breakdown = result.aggregate["table_breakdown"]
	# Should be sorted by duration desc
	assert breakdown[0]["table"] == "tabBig"
	assert breakdown[1]["table"] == "tabMedium"
	assert breakdown[2]["table"] == "tabSmall"


def test_empty_recordings(empty_context):
	result = table_breakdown.analyze([], empty_context)
	assert result.aggregate["table_breakdown"] == []
	assert result.findings == []


def test_no_findings_emitted(clean_recording, empty_context):
	"""table_breakdown is informational only, no findings."""
	result = table_breakdown.analyze([clean_recording], empty_context)
	assert result.findings == []


# ---------------------------------------------------------------------------
# v0.6.0: read/write split + index candidates
# ---------------------------------------------------------------------------


def _rec(*queries):
	"""Build a one-recording fixture from (sql, duration_ms) tuples."""
	return {
		"uuid": "tb",
		"path": "/",
		"cmd": None,
		"method": "POST",
		"event_type": "HTTP Request",
		"duration": sum(d for _, d in queries),
		"calls": [
			{"query": q, "normalized_query": q, "duration": float(d), "stack": []}
			for q, d in queries
		],
	}


def _row_for(breakdown, table):
	return next(b for b in breakdown if b["table"] == table)


class TestReadWriteSplit:
	def test_counts_reads_and_writes_separately(self, empty_context):
		rec = _rec(
			("SELECT `name` FROM `tabFoo` WHERE `a` = ?", 10),
			("SELECT `name` FROM `tabFoo` WHERE `b` = ?", 20),
			("UPDATE `tabFoo` SET `x` = ? WHERE `name` = ?", 3),
			("INSERT INTO `tabFoo` (`name`) VALUES (?)", 2),
			("DELETE FROM `tabFoo` WHERE `name` = ?", 1),
		)
		row = _row_for(table_breakdown.analyze([rec], empty_context).aggregate["table_breakdown"], "tabFoo")
		assert row["queries"] == 5
		assert row["read_count"] == 2
		assert row["write_count"] == 3
		assert row["read_time_ms"] == pytest.approx(30.0, abs=0.1)
		assert row["write_time_ms"] == pytest.approx(6.0, abs=0.1)
		assert row["duration_ms"] == pytest.approx(36.0, abs=0.1)

	def test_write_only_table_has_zero_reads_and_no_candidates(self, empty_context):
		rec = _rec(
			("INSERT INTO `tabAudit` (`a`,`b`) VALUES (?,?)", 1),
			("UPDATE `tabAudit` SET `c` = ? WHERE `name` = ?", 2),
		)
		row = _row_for(table_breakdown.analyze([rec], empty_context).aggregate["table_breakdown"], "tabAudit")
		assert row["read_count"] == 0
		assert row["write_count"] == 2
		assert row["index_candidates"] == []

	def test_write_target_tables_kept_even_below_the_time_cutoff(self, empty_context):
		# Lots of slow reads on read-only tables push everything past the
		# top-N-by-time cutoff — but the one table that was *written* (a
		# single cheap UPDATE) must still show up, so a doc-save's writes
		# aren't invisible.
		calls = [
			(f"SELECT `name` FROM `tabReadOnly{i}` WHERE `x` = ?", 50)
			for i in range(table_breakdown.DEFAULT_TOP_N + 5)
		]
		calls.append(("UPDATE `tabWrittenOnce` SET `s` = ? WHERE `name` = ?", 1))
		bd = table_breakdown.analyze([_rec(*calls)], empty_context).aggregate["table_breakdown"]
		names = {t["table"] for t in bd}
		assert "tabWrittenOnce" in names, "a write-target table was dropped by the time cutoff"
		assert _row_for(bd, "tabWrittenOnce")["write_count"] == 1


class TestIndexCandidates:
	def test_extracts_where_join_order_columns_from_reads(self, empty_context):
		q = (
			"SELECT `tabSI`.`name` FROM `tabSI` "
			"LEFT JOIN `tabCust` ON `tabCust`.`territory` = `tabSI`.`customer` "
			"WHERE `tabSI`.`status` = ? AND `tabSI`.`company` = ? "
			"ORDER BY `tabSI`.`posting_date` DESC"
		)
		row = _row_for(table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"], "tabSI")
		cands = {c["column"]: c for c in row["index_candidates"]}
		assert "status" in cands and cands["status"]["sources"] == ["WHERE"]
		assert "company" in cands and cands["company"]["sources"] == ["WHERE"]
		assert "customer" in cands and cands["customer"]["sources"] == ["JOIN"]
		assert "posting_date" in cands and cands["posting_date"]["sources"] == ["ORDER BY"]
		# `name` is the JOIN column on the OTHER side (attributed to tabCust) AND
		# a Frappe metadata column — either way it's not a tabSI candidate.
		assert "name" not in cands

	def test_candidates_ranked_by_frequency(self, empty_context):
		rec = _rec(
			("SELECT `name` FROM `tabFoo` WHERE `hot` = ? AND `cold` = ?", 1),
			("SELECT `name` FROM `tabFoo` WHERE `hot` = ?", 1),
			("SELECT `name` FROM `tabFoo` WHERE `hot` = ?", 1),
		)
		row = _row_for(table_breakdown.analyze([rec], empty_context).aggregate["table_breakdown"], "tabFoo")
		cols = [c["column"] for c in row["index_candidates"]]
		assert cols[0] == "hot"  # 3 hits beats 1
		hot = next(c for c in row["index_candidates"] if c["column"] == "hot")
		assert hot["hits"] == 3

	def test_candidates_only_from_reads_not_writes(self, empty_context):
		# The UPDATE's WHERE column must NOT become an index candidate here —
		# the breakdown's candidates are explicitly "to speed up reads".
		rec = _rec(("UPDATE `tabFoo` SET `x` = ? WHERE `only_in_update` = ?", 5))
		row = _row_for(table_breakdown.analyze([rec], empty_context).aggregate["table_breakdown"], "tabFoo")
		assert row["index_candidates"] == []

	def test_unqualified_column_in_multitable_query_is_skipped(self, empty_context):
		# `b` isn't table-qualified and the query has two tables → ambiguous,
		# so it shouldn't be attributed to either.
		q = "SELECT `tabA`.`name` FROM `tabA` JOIN `tabB` ON `tabB`.`a_id` = `tabA`.`name` WHERE b = ?"
		breakdown = table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"]
		for row in breakdown:
			assert all(c["column"] != "b" for c in row["index_candidates"])

	def test_candidates_capped(self, empty_context):
		# 10 distinct WHERE columns on tabFoo → capped at MAX_INDEX_CANDIDATES_PER_TABLE.
		conds = " AND ".join(f"`c{i}` = ?" for i in range(10))
		rec = _rec((f"SELECT `name` FROM `tabFoo` WHERE {conds}", 5))
		row = _row_for(table_breakdown.analyze([rec], empty_context).aggregate["table_breakdown"], "tabFoo")
		assert len(row["index_candidates"]) == table_breakdown.MAX_INDEX_CANDIDATES_PER_TABLE


class TestFrappeMetadataColumnsExcluded:
	def test_metadata_columns_never_suggested_but_recorded(self, empty_context):
		q = (
			"SELECT `name` FROM `tabFoo` "
			"WHERE `creation` > ? AND `status` = ? AND `parent` = ? "
			"ORDER BY `modified` DESC"
		)
		row = _row_for(table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"], "tabFoo")
		cand_cols = {c["column"] for c in row["index_candidates"]}
		# Only the business column is a candidate.
		assert cand_cols == {"status"}
		# The Frappe metadata columns that were filtered on are recorded
		# separately (so the report can say "also filtered on … not suggested").
		assert set(row["framework_cols_filtered"]) == {"creation", "modified", "parent"}

	def test_table_filtering_only_on_metadata_has_no_candidates(self, empty_context):
		q = "SELECT * FROM `tabAudit` WHERE `name` = ? AND `idx` = ? AND `docstatus` = ?"
		row = _row_for(table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"], "tabAudit")
		assert row["index_candidates"] == []
		assert set(row["framework_cols_filtered"]) == {"docstatus", "idx", "name"}

	def test_no_metadata_filter_leaves_framework_list_empty(self, empty_context):
		q = "SELECT `name` FROM `tabFoo` WHERE `customer` = ?"
		row = _row_for(table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"], "tabFoo")
		assert [c["column"] for c in row["index_candidates"]] == ["customer"]
		assert row["framework_cols_filtered"] == []


def test_is_frappe_metadata_column_truth_table():
	from optimus.analyzers.base import is_frappe_metadata_column
	for c in ("creation", "modified", "modified_by", "idx", "parent",
	          "parentfield", "parenttype", "owner", "docstatus", "name",
	          "doctype", "_assign", "_user_tags", "_comments", "_liked_by", "_seen"):
		assert is_frappe_metadata_column(c) is True
	# Case-insensitive.
	assert is_frappe_metadata_column("CREATION") is True
	assert is_frappe_metadata_column("  Modified  ") is True
	# Not metadata.
	for c in ("posting_date", "customer", "status", "company", "amount"):
		assert is_frappe_metadata_column(c) is False
	# Falsy / weird inputs.
	assert is_frappe_metadata_column("") is False
	assert is_frappe_metadata_column(None) is False


def test_is_frappe_meta_table_truth_table():
	from optimus.analyzers.base import is_frappe_meta_table
	for t in ("tabDocType", "tabDocField", "tabCustom Field", "tabProperty Setter",
	          "tabClient Script", "tabServer Script", "tabSingles", "tabSeries",
	          "tab__global_search", "tabModule Def", "tabWorkspace", "tabDashboard Chart",
	          "tabPrint Format", "tabPatch Log", "tabRole"):
		assert is_frappe_meta_table(t) is True
	# Case-insensitive + tolerates backticks.
	assert is_frappe_meta_table("tabdoctype") is True
	assert is_frappe_meta_table("`tabCustom Field`") is True
	# Real data tables — NOT meta.
	for t in ("tabSales Invoice", "tabUser", "tabFile", "tabVersion",
	          "tabEmail Queue", "tabCommunication", "tabError Log", "tabItem"):
		assert is_frappe_meta_table(t) is False
	assert is_frappe_meta_table("") is False
	assert is_frappe_meta_table(None) is False


class TestFrappeMetaTablesExcluded:
	def test_meta_table_gets_flag_and_no_candidates(self, empty_context):
		# tabSingles reads filter on `doctype`/`field` — but it's a meta
		# table, so no candidates and no framework_cols list either.
		q = "SELECT `value` FROM `tabSingles` WHERE `doctype` = ? AND `field` = ?"
		row = _row_for(
			table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"],
			"tabSingles",
		)
		assert row["is_meta_table"] is True
		assert row["index_candidates"] == []
		assert row["framework_cols_filtered"] == []
		# Still counted in the breakdown (time/reads aren't suppressed).
		assert row["read_count"] == 1 and row["duration_ms"] == 5.0

	def test_non_meta_table_flag_is_false(self, empty_context):
		q = "SELECT `name` FROM `tabSales Invoice` WHERE `customer` = ?"
		row = _row_for(
			table_breakdown.analyze([_rec((q, 5))], empty_context).aggregate["table_breakdown"],
			"tabSales Invoice",
		)
		assert row["is_meta_table"] is False
		assert [c["column"] for c in row["index_candidates"]] == ["customer"]

	def test_render_shows_meta_table_note(self):
		# v0.6.x: with the default "Hide framework / internal database tables"
		# toggle on, meta tables are filtered out of the section entirely.
		# Disable the toggle so the row reaches the template and the
		# "Frappe framework meta table" disclaimer renders as it used to.
		from unittest.mock import patch

		from optimus import renderer
		from optimus.settings import OptimusConfig
		breakdown = [{
			"table": "tabCustom Field", "duration_ms": 8.0, "queries": 2,
			"read_count": 2, "write_count": 0, "read_time_ms": 8.0, "write_time_ms": 0.0,
			"index_candidates": [], "framework_cols_filtered": [], "is_meta_table": True,
		}]
		doc = TestRenderedFrameworkColsNote()._doc(breakdown)  # reuse the fake-doc builder
		with patch("optimus.settings.get_config",
		           return_value=OptimusConfig(hide_framework_tables=False)):
			html = renderer.render_raw(doc, recordings=[])
		assert "tabCustom Field" in html
		assert "Frappe framework meta table" in html
		assert "bench migrate</code> owns its schema" in html
		# Must NOT fall through to the generic "no single indexable column" line.
		assert "no single indexable column" not in html


class TestRenderedFrameworkColsNote:
	def _doc(self, breakdown):
		import json
		import types
		return types.SimpleNamespace(
			name="PS", session_uuid="u", title="t", user="a", status="Ready",
			started_at="2026-05-11", stopped_at="2026-05-11", notes=None,
			top_severity="Low", summary_html=None, total_duration_ms=100,
			total_query_time_ms=80, total_queries=5, total_requests=1,
			top_queries_json="[]", table_breakdown_json=json.dumps(breakdown),
			hot_frames_json=None, session_time_breakdown_json=None,
			total_python_ms=None, total_sql_ms=None, analyzer_warnings=None,
			v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
		)

	def test_footnote_when_candidates_and_metadata_both_present(self):
		from optimus import renderer
		breakdown = [{
			"table": "tabFoo", "duration_ms": 50.0, "queries": 3,
			"read_count": 3, "write_count": 0, "read_time_ms": 50.0, "write_time_ms": 0.0,
			"index_candidates": [{"column": "status", "sources": ["WHERE"], "hits": 3}],
			"framework_cols_filtered": ["creation", "modified"],
		}]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert ">status</code>" in html
		assert "Also filtered on Frappe metadata columns" in html
		assert ">creation</code>" in html and ">modified</code>" in html
		# Section footer caveat names the exclusions (metadata cols + meta tables).
		assert "never suggested" in html.lower()
		assert "framework meta tables" in html.lower()

	def test_metadata_only_table_shows_not_safe_targets_line(self):
		from optimus import renderer
		breakdown = [{
			"table": "tabAudit", "duration_ms": 12.0, "queries": 2,
			"read_count": 2, "write_count": 1, "read_time_ms": 10.0, "write_time_ms": 2.0,
			"index_candidates": [],
			"framework_cols_filtered": ["docstatus", "idx", "name"],
		}]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "Reads here only filter on Frappe metadata columns" in html
		# It should NOT claim "no single indexable column" (that's the other branch).
		assert "no single indexable column" not in html


class TestRenderedSection:
	def _doc(self, breakdown):
		import json
		import types
		return types.SimpleNamespace(
			name="PS", session_uuid="u", title="t", user="a", status="Ready",
			started_at="2026-05-11", stopped_at="2026-05-11", notes=None,
			top_severity="Low", summary_html=None, total_duration_ms=100,
			total_query_time_ms=80, total_queries=5, total_requests=1,
			top_queries_json="[]", table_breakdown_json=json.dumps(breakdown),
			hot_frames_json=None, session_time_breakdown_json=None,
			total_python_ms=None, total_sql_ms=None, analyzer_warnings=None,
			v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
		)

	def test_render_shows_reads_writes_and_candidates(self):
		from optimus import renderer
		breakdown = [{
			"table": "tabFoo", "duration_ms": 50.0, "queries": 4,
			"read_count": 2, "write_count": 2, "read_time_ms": 40.0, "write_time_ms": 10.0,
			"index_candidates": [
				{"column": "docstatus", "sources": ["WHERE"], "hits": 3},
				{"column": "company", "sources": ["WHERE", "ORDER BY"], "hits": 1},
			],
		}]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "Time spent per database table" in html
		# Reads / Writes columns rendered.
		assert "<th class=\"num\">Reads</th>" in html
		assert "<th class=\"num\">Writes</th>" in html
		# Candidate columns + their source tooltip + the write-impact note.
		assert "<code title=\"used in WHERE" in html
		assert ">docstatus</code>" in html
		assert "every index added also slows those writes" in html

	def test_render_shows_tables_written_to_line(self):
		from optimus import renderer
		breakdown = [
			{"table": "tabReadHeavy", "duration_ms": 500.0, "queries": 50, "read_count": 50,
			 "write_count": 0, "read_time_ms": 500.0, "write_time_ms": 0.0, "index_candidates": []},
			{"table": "tabSales Invoice", "duration_ms": 8.0, "queries": 3, "read_count": 2,
			 "write_count": 1, "read_time_ms": 7.0, "write_time_ms": 1.0, "index_candidates": []},
			{"table": "tabVersion", "duration_ms": 1.0, "queries": 1, "read_count": 0,
			 "write_count": 1, "read_time_ms": 0.0, "write_time_ms": 1.0, "index_candidates": []},
		]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "Tables written to this session" in html
		assert ">tabSales Invoice</code>" in html and ">tabVersion</code>" in html
		# the explanation that writes are cheap so they rank low by time
		assert "Writes are usually cheap" in html

	def test_no_tables_written_line_when_nothing_written(self):
		from optimus import renderer
		breakdown = [{"table": "tabFoo", "duration_ms": 50.0, "queries": 2, "read_count": 2,
		              "write_count": 0, "read_time_ms": 50.0, "write_time_ms": 0.0, "index_candidates": []}]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "Tables written to this session" not in html

	def test_render_low_risk_note_when_no_writes(self):
		from optimus import renderer
		breakdown = [{
			"table": "tabUser", "duration_ms": 12.0, "queries": 1,
			"read_count": 1, "write_count": 0, "read_time_ms": 12.0, "write_time_ms": 0.0,
			"index_candidates": [{"column": "email", "sources": ["WHERE"], "hits": 1}],
		}]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "adding an index here is low-risk" in html
		assert "every index added also slows" not in html

	def test_render_handles_old_session_without_new_fields(self):
		from optimus import renderer
		# Pre-v0.6.0 shape: just table / duration_ms / queries.
		breakdown = [{"table": "tabLegacy", "duration_ms": 30.0, "queries": 3}]
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "tabLegacy" in html
		# The Reads/Writes cells fall back to an em-dash; no crash.
		assert "Time spent per database table" in html


# --------------------------------------------------------------------------
# v0.6.x: is_framework_db_table + the "Hide framework / internal database
# tables" toggle that drops noisy tables from the table-breakdown section.
# --------------------------------------------------------------------------

def test_is_framework_db_table_truth_table():
	"""Schema/meta tables + framework-internal (user/session bookkeeping) +
	information_schema.* are all flagged. Real user-data tables are not."""
	from optimus.analyzers.base import is_framework_db_table

	# Schema/meta (already in FRAPPE_META_TABLES).
	for t in ("tabDocType", "tabDocField", "tabSingles", "tabPatch Log",
	          "tabDocType Link", "tabDocType Action", "tabDocType State",
	          "tabWorkspace", "tabRole"):
		assert is_framework_db_table(t) is True

	# Framework-internal (new — user/session bookkeeping; deliberately
	# excludes tabUser, which is a real user-data table some apps query
	# meaningfully).
	for t in ("tabHas Role", "tabDefaultValue", "tabUser Social Login",
	          "tabUser Role Profile", "tabBlock Module", "tabUser Email"):
		assert is_framework_db_table(t) is True

	# MySQL system tables — any information_schema.* name.
	for t in ("information_schema.columns", "information_schema.tables",
	          "information_schema.statistics", "INFORMATION_SCHEMA.COLUMNS"):
		assert is_framework_db_table(t) is True

	# Case-insensitive + backtick-tolerant.
	assert is_framework_db_table("tabhas role") is True
	assert is_framework_db_table("`tabHas Role`") is True
	assert is_framework_db_table("TABDOCTYPE") is True

	# Real user-data tables (incl. tabUser) and arbitrary app tables → False.
	for t in ("tabSales Invoice", "tabItem", "tabFile", "tabCommunication",
	          "tabError Log", "tabUser", "myapp_table", "informationschema.columns"):
		assert is_framework_db_table(t) is False

	# Edge cases.
	assert is_framework_db_table(None) is False
	assert is_framework_db_table("") is False
	assert is_framework_db_table("tab") is False


class TestHideFrameworkTablesToggle:
	"""The "Hide framework / internal database tables" Check (default on)
	filters the "Time spent per database table" section by ``is_framework_db_table``
	and renders a small "(N hidden)" note. Other sections are unaffected."""

	def _doc(self, breakdown):
		import json
		import types
		return types.SimpleNamespace(
			name="PS", session_uuid="u", title="t", user="a", status="Ready",
			started_at="2026-05-13", stopped_at="2026-05-13", notes=None,
			top_severity="Low", summary_html=None, total_duration_ms=100,
			total_query_time_ms=80, total_queries=5, total_requests=1,
			top_queries_json="[]", table_breakdown_json=json.dumps(breakdown),
			hot_frames_json=None, session_time_breakdown_json=None,
			total_python_ms=None, total_sql_ms=None, analyzer_warnings=None,
			v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
		)

	def _four_tables(self):
		# One user-app table + three framework/internal ones (a schema-meta,
		# a framework-internal session table, and information_schema.*).
		return [
			{"table": "tabSales Invoice", "duration_ms": 140.0, "queries": 840,
			 "read_count": 4, "write_count": 836, "read_time_ms": 2.0, "write_time_ms": 138.0,
			 "index_candidates": [], "framework_cols_filtered": [], "is_meta_table": False},
			{"table": "tabHas Role", "duration_ms": 50.0, "queries": 20,
			 "read_count": 20, "write_count": 0, "read_time_ms": 50.0, "write_time_ms": 0.0,
			 "index_candidates": [], "framework_cols_filtered": [], "is_meta_table": False},
			{"table": "tabDocField", "duration_ms": 30.0, "queries": 10,
			 "read_count": 10, "write_count": 0, "read_time_ms": 30.0, "write_time_ms": 0.0,
			 "index_candidates": [], "framework_cols_filtered": [], "is_meta_table": True},
			{"table": "information_schema.columns", "duration_ms": 15.0, "queries": 5,
			 "read_count": 5, "write_count": 0, "read_time_ms": 15.0, "write_time_ms": 0.0,
			 "index_candidates": [], "framework_cols_filtered": [], "is_meta_table": False},
		]

	def test_default_on_filters_framework_tables(self):
		from optimus import renderer
		# Default toggle is True (no patching needed — OptimusConfig() default).
		html = renderer.render_raw(self._doc(self._four_tables()), recordings=[])
		# Only the user-app table survives in the table-breakdown section.
		assert "tabSales Invoice" in html
		assert "<code>tabHas Role</code>" not in html
		assert "<code>tabDocField</code>" not in html
		assert "<code>information_schema.columns</code>" not in html
		# The "(N hidden)" note renders (3 framework tables filtered).
		assert "framework/internal tables hidden" in html
		assert "<strong>3</strong>" in html

	def test_toggle_off_shows_every_table(self):
		from unittest.mock import patch

		from optimus import renderer
		from optimus.settings import OptimusConfig

		with patch("optimus.settings.get_config",
		           return_value=OptimusConfig(hide_framework_tables=False)):
			html = renderer.render_raw(self._doc(self._four_tables()), recordings=[])

		# All four tables visible.
		assert "tabSales Invoice" in html
		assert "<code>tabHas Role</code>" in html
		assert "<code>tabDocField</code>" in html
		assert "<code>information_schema.columns</code>" in html
		# No "(N hidden)" note when nothing was filtered.
		assert "framework/internal tables hidden" not in html
		assert "framework/internal table hidden" not in html

	def test_singular_word_when_one_table_hidden(self):
		from optimus import renderer
		# Just one framework table to drop → "1 framework/internal table hidden".
		breakdown = [self._four_tables()[0], self._four_tables()[1]]  # SI + Has Role
		html = renderer.render_raw(self._doc(breakdown), recordings=[])
		assert "<code>tabHas Role</code>" not in html
		# Singular: "1 framework/internal table hidden" (no plural "s").
		assert "<strong>1</strong> framework/internal table hidden" in html
		assert "framework/internal tables hidden" not in html

	def test_only_filters_table_breakdown_not_other_sections(self):
		# tabUser also appears in the per-action drill-down, top-queries
		# leaderboard, full recordings, etc. The filter must NOT touch those.
		# Here we use a doc with an action whose path mentions tabUser as a
		# smoke test that other sections still render the name.
		import json
		import types
		breakdown = [self._four_tables()[0]]  # only tabSales Invoice in the breakdown
		from optimus import renderer
		action = types.SimpleNamespace(
			action_label="frappe.client.get_value:tabUser", event_type="HTTP Request",
			http_method="GET", path="/api/method/frappe.client.get_value",
			recording_uuid="r0", duration_ms=50, queries_count=1, query_time_ms=5,
			slowest_query_ms=5,
		)
		doc = self._doc(breakdown)
		doc.actions = [action]
		html = renderer.render_raw(doc, recordings=[{"uuid": "r0", "calls": [], "form_dict": {}}])
		# Per-action breakdown keeps the action_label (containing "tabUser") intact.
		assert "frappe.client.get_value:tabUser" in html
		# But the db-tables section still hides tabUser (the breakdown didn't list it
		# here — so the note shouldn't render either, since nothing was hidden).
		assert "framework/internal tables hidden" not in html
		assert "framework/internal table hidden" not in html


class TestRenderConfigFooter:
	"""v0.6.x: the report footer stamps the render-affecting settings that
	were in effect at render time. Without this stamp, users who toggle a
	Optimus Settings flag and re-open an existing (un-regenerated) HTML
	file see no change and assume a bug — when in fact the saved file is
	frozen at its rendered-time settings."""

	def _doc(self):
		import json
		import types
		return types.SimpleNamespace(
			name="PS", session_uuid="u", title="t", user="a", status="Ready",
			started_at="2026-05-13", stopped_at="2026-05-13", notes=None,
			top_severity="Low", summary_html=None, total_duration_ms=100,
			total_query_time_ms=80, total_queries=5, total_requests=1,
			top_queries_json="[]", table_breakdown_json="[]",
			hot_frames_json=None, session_time_breakdown_json=None,
			total_python_ms=None, total_sql_ms=None, analyzer_warnings=None,
			v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
		)

	def test_footer_stamps_default_on_values(self):
		from optimus import renderer
		# Default OptimusConfig() → hide_framework_tables on, no tracked/
		# ignored apps, AI section toggles on, min_action_duration_ms=0.
		html = renderer.render_raw(self._doc(), recordings=[])
		# Anchor label is unambiguous.
		assert "<strong>Rendered with:</strong>" in html
		assert "hide_framework_tables=on" in html
		assert "tracked_apps=(none)" in html
		assert "ignored_apps=(none)" in html
		assert "ai_suggest_findings=on" in html
		assert "ai_suggest_indexes=on" in html
		assert "min_action_duration_ms=0" in html
		assert "large_duration_threshold_ms=1000" in html
		# The nudge phrase explains why the user might be looking at stale
		# data — the whole point of the stamp.
		assert "Regenerate Reports" in html

	def test_footer_reflects_patched_settings(self):
		from unittest.mock import patch

		from optimus import renderer
		from optimus.settings import OptimusConfig

		cfg = OptimusConfig(
			hide_framework_tables=False,
			tracked_apps=("myapp", "ugly_code"),
			ignored_apps=("frappe",),
			ai_suggest_findings=False,
			ai_suggest_indexes=False,
			min_action_duration_ms=42.0,
			large_duration_threshold_ms=2500.0,
		)
		with patch("optimus.settings.get_config", return_value=cfg):
			html = renderer.render_raw(self._doc(), recordings=[])

		assert "hide_framework_tables=off" in html
		assert "tracked_apps=myapp, ugly_code" in html
		assert "ignored_apps=frappe" in html
		assert "ai_suggest_findings=off" in html
		assert "ai_suggest_indexes=off" in html
		assert "min_action_duration_ms=42" in html
		assert "large_duration_threshold_ms=2500" in html
