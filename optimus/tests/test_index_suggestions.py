# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for optimus.analyzers.index_suggestions.

This analyzer imports frappe.core.doctype.recorder.recorder._optimize_query
inside the function. To test in isolation we monkeypatch that import
using pytest's `monkeypatch` fixture, replacing _optimize_query with a
stub that returns a canned DBIndex or None.
"""

import json
import sys
import types

import pytest

from optimus.analyzers import index_suggestions


class _FakeDBIndex:
	def __init__(self, table, column):
		self.table = table
		self.column = column


def _install_fake_recorder_module(monkeypatch, optimize_query_impl):
	"""Register a fake frappe.core.doctype.recorder.recorder module.

	index_suggestions.analyze imports _optimize_query at call time via:
	    from frappe.core.doctype.recorder.recorder import _optimize_query
	We install a minimal fake module hierarchy in sys.modules so the
	import succeeds without a real Frappe site.
	"""
	# Build the module chain only if not already present
	for mod_name in (
		"frappe",
		"frappe.core",
		"frappe.core.doctype",
		"frappe.core.doctype.recorder",
		"frappe.core.doctype.recorder.recorder",
	):
		if mod_name not in sys.modules:
			monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

	# Attach the fake _optimize_query to the recorder submodule
	recorder_mod = sys.modules["frappe.core.doctype.recorder.recorder"]
	monkeypatch.setattr(recorder_mod, "_optimize_query", optimize_query_impl, raising=False)


def test_single_suggestion_per_table_column(monkeypatch, empty_context):
	"""Multiple unique queries suggesting the same index → one finding."""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabLead", "status")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [
			{
				"query": f"SELECT * FROM tabLead WHERE status = '{s}'",
				"normalized_query": f"SELECT * FROM tabLead WHERE STATUS = '{s}'",
				"duration": 30.0,
				"stack": [],
			}
			for s in ("Open", "Closed", "Qualified", "Lost")
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	# 4 unique normalized queries → 4 optimizer calls → 1 deduped suggestion
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1
	assert "tabLead" in missing[0]["title"]
	assert "status" in missing[0]["title"]
	detail = json.loads(missing[0]["technical_detail_json"])
	assert detail["table"] == "tabLead"
	assert detail["column"] == "status"
	assert detail["affected_query_count"] == 4
	assert "ALTER TABLE" in detail["suggested_ddl"]


def test_parser_limitation_valueerror_gets_soft_warning(monkeypatch, empty_context):
	"""v0.5.1: ValueError from sql_metadata is a parser limitation, not
	a bug we should scream about. It must produce the soft "Skipped N
	queries whose shape exceeds the parser" warning — NOT the loud
	"Could not analyze" warning that tells users to check Error Log.
	And it must NOT write to frappe.log_error."""

	def fake_optimize(query: str):
		raise ValueError("synthetic parse error")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	# Track whether log_error was called — parser limitations must NOT
	# end up in Frappe's Error Log.
	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe,
		"log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix2",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT ?",
				"duration": 5.0,
				"stack": [],
			},
			{
				"query": "SELECT 2",
				"normalized_query": "SELECT ??",
				"duration": 5.0,
				"stack": [],
			},
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	# Soft warning, not the loud one.
	assert len(result.warnings) == 1
	assert "exceeds the DBOptimizer heuristic" in result.warnings[0], (
		f"Expected soft parser-limit warning; got: {result.warnings}"
	)
	assert "Could not analyze" not in result.warnings[0], (
		"ValueError is a parser limitation — must not use the "
		"loud 'Could not analyze / see Error Log' phrasing"
	)
	# And crucially: no Error Log entry for parser limitations.
	assert log_error_calls == [], (
		f"Parser limitations must not be written to frappe.log_error. "
		f"Calls recorded: {log_error_calls}"
	)


def test_real_error_still_produces_loud_warning_and_logs(monkeypatch, empty_context):
	"""Unexpected exception types (not ValueError/TypeError) may indicate
	a profiler bug — those still get the loud warning + Error Log entries
	so the team can investigate."""

	def fake_optimize(query: str):
		raise RuntimeError("unexpected internal optimizer error")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe,
		"log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix_real_err",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 10,
		"calls": [{
			"query": "SELECT 1",
			"normalized_query": "SELECT ?",
			"duration": 5.0,
			"stack": [],
		}],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	# Loud warning for real errors.
	loud = [w for w in result.warnings if "Could not analyze" in w]
	assert len(loud) == 1
	assert "RuntimeError" in loud[0]
	# And DID log to Error Log.
	assert len(log_error_calls) >= 1, (
		"Real errors must be written to frappe.log_error so they're "
		"discoverable for investigation."
	)


def test_no_suggestion_when_optimizer_returns_none(monkeypatch, empty_context):
	def fake_optimize(query: str):
		return None

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix3",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT ?",
				"duration": 5.0,
				"stack": [],
			},
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	assert result.warnings == []


def _install_fake_frappe_db(monkeypatch, indexed_columns_by_table, column_types_by_table):
    """Install a fake frappe.db.sql that answers SHOW INDEX and
    information_schema.columns lookups from the provided dicts.

    Args:
        indexed_columns_by_table: {"tabFoo": {"name", "parent", ...}}
        column_types_by_table:    {"tabFoo": {"name": "varchar", "json_col": "json"}}
    """
    import frappe

    def fake_sql(query, params=None, as_dict=False):
        # SHOW INDEX FROM `tabFoo`
        if "SHOW INDEX FROM" in query:
            # Parse the backtick-quoted table name.
            import re
            m = re.search(r"`([^`]+)`", query)
            if not m:
                return []
            table = m.group(1)
            cols = indexed_columns_by_table.get(table, set())
            return [
                {"Column_name": c, "Seq_in_index": 1}
                for c in cols
            ]

        # information_schema.columns lookup
        if "information_schema.columns" in query:
            table = None
            if isinstance(params, (tuple, list)) and params:
                table = params[0]
            elif isinstance(params, dict):
                table = params.get("table_name")
            cols = column_types_by_table.get(table, {})
            return [
                {"column_name": name, "data_type": dtype}
                for name, dtype in cols.items()
            ]

        return []

    class FakeDB:
        def sql(self, query, params=None, as_dict=False):
            return fake_sql(query, params, as_dict)

    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(
        frappe, "log_error", lambda *a, **k: None, raising=False
    )


def test_already_indexed_column_is_suppressed(monkeypatch, empty_context):
    """v0.5.1 architect-review finding: Missing Index must check
    existing indexes before suggesting one. A suggestion for a
    column that's already the leftmost of an existing index is a
    false positive — the DB already has the index the user would
    'add.' Suppress it and surface a warning."""

    def fake_optimize(query):
        return _FakeDBIndex("tabCustomer", "customer_name")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabCustomer": {"name", "customer_name"}},
        column_types_by_table={
            "tabCustomer": {"name": "varchar", "customer_name": "varchar"}
        },
    )

    recording = {
        "uuid": "ix_indexed",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabCustomer WHERE customer_name = 'X'",
            "normalized_query": "SELECT * FROM tabCustomer WHERE customer_name = ?",
            "duration": 50.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    # customer_name is already indexed → no finding.
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert missing == [], (
        "Already-indexed column must NOT produce a Missing Index finding. "
        "The DBOptimizer heuristic doesn't check existing indexes — that's "
        "our job before emitting."
    )
    # And a warning must explain the suppression.
    assert any("already indexed" in w for w in result.warnings), (
        f"Expected a warning about the suppressed suggestion; got: {result.warnings}"
    )


def test_json_column_is_suppressed_as_unindexable(monkeypatch, empty_context):
    """JSON columns can't be btree-indexed directly. Suggesting
    ADD INDEX (json_col) would fail at DDL apply time. Drop it."""

    def fake_optimize(query):
        return _FakeDBIndex("tabThing", "meta")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabThing": {"name"}},
        column_types_by_table={"tabThing": {"name": "varchar", "meta": "json"}},
    )

    recording = {
        "uuid": "ix_json",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabThing WHERE JSON_EXTRACT(meta, '$.k') = 'v'",
            "normalized_query": "SELECT * FROM tabThing WHERE JSON_EXTRACT(meta, ?) = ?",
            "duration": 80.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert missing == []
    assert any("can't be btree-indexed" in w for w in result.warnings)


def test_text_column_gets_prefix_index_ddl(monkeypatch, empty_context):
    """TEXT/BLOB columns require a prefix length on the index.
    The plain DDL 'ADD INDEX idx_col (col)' fails with 'BLOB/TEXT
    column used in key specification without a key length.' The
    analyzer must emit a prefix-index DDL for these types."""

    def fake_optimize(query):
        return _FakeDBIndex("tabDoc", "body")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabDoc": {"name"}},
        column_types_by_table={"tabDoc": {"name": "varchar", "body": "text"}},
    )

    recording = {
        "uuid": "ix_text",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabDoc WHERE body LIKE 'keyword%'",
            "normalized_query": "SELECT * FROM tabDoc WHERE body LIKE ?",
            "duration": 60.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert len(missing) == 1
    detail = json.loads(missing[0]["technical_detail_json"])
    # DDL must include a prefix length — something like `body`(255).
    ddl = detail["suggested_ddl"]
    assert "`body`(" in ddl or "body(" in ddl, (
        f"TEXT column DDL must include prefix length; got: {ddl}"
    )
    # And the prefix must be a non-zero number of characters.
    assert "(255)" in ddl or "(767)" in ddl or "(100)" in ddl, (
        f"TEXT column DDL must specify a non-zero prefix length; got: {ddl}"
    )
    assert detail["verified_not_indexed"] is True


def test_nonexistent_column_is_suppressed(monkeypatch, empty_context):
    """If the SQL parser hallucinates or mis-parses and suggests a
    column that doesn't exist on the table, drop the suggestion."""

    def fake_optimize(query):
        return _FakeDBIndex("tabItem", "nonexistent_column")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabItem": {"name"}},
        column_types_by_table={"tabItem": {"name": "varchar", "item_name": "varchar"}},
    )

    recording = {
        "uuid": "ix_ghost",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabItem WHERE something",
            "normalized_query": "SELECT * FROM tabItem WHERE something",
            "duration": 40.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert missing == []


def test_actionable_findings_carry_verified_flag(monkeypatch, empty_context):
    """Positive case: a column that genuinely lacks an index gets an
    actionable finding with verified_not_indexed=True in the detail."""

    def fake_optimize(query):
        return _FakeDBIndex("tabProject", "project_manager")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabProject": {"name", "status"}},  # project_manager NOT indexed
        column_types_by_table={
            "tabProject": {
                "name": "varchar",
                "status": "varchar",
                "project_manager": "varchar",
            }
        },
    )

    recording = {
        "uuid": "ix_good",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabProject WHERE project_manager = 'alice@x.com'",
            "normalized_query": "SELECT * FROM tabProject WHERE project_manager = ?",
            "duration": 200.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert len(missing) == 1
    detail = json.loads(missing[0]["technical_detail_json"])
    assert detail["verified_not_indexed"] is True
    assert detail["suggested_ddl"] == (
        "ALTER TABLE `tabProject` ADD INDEX `idx_project_manager` (`project_manager`);"
    )


def test_classifier_caches_per_table(monkeypatch, empty_context):
    """Multiple suggestions on the same table must trigger only ONE
    SHOW INDEX and one information_schema lookup per table, not one
    per suggestion."""

    def fake_optimize(query):
        # Two different columns on the same table.
        if "col_a" in query:
            return _FakeDBIndex("tabHot", "col_a")
        return _FakeDBIndex("tabHot", "col_b")

    _install_fake_recorder_module(monkeypatch, fake_optimize)

    import frappe
    show_index_calls = []
    info_schema_calls = []

    def counting_sql(query, params=None, as_dict=False):
        if "SHOW INDEX FROM" in query:
            show_index_calls.append(query)
            return [{"Column_name": "name", "Seq_in_index": 1}]
        if "information_schema.columns" in query:
            info_schema_calls.append(query)
            return [
                {"column_name": "name", "data_type": "varchar"},
                {"column_name": "col_a", "data_type": "varchar"},
                {"column_name": "col_b", "data_type": "varchar"},
            ]
        return []

    class FakeDB:
        def sql(self, query, params=None, as_dict=False):
            return counting_sql(query, params, as_dict)

    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

    recording = {
        "uuid": "ix_cache",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [
            {
                "query": "SELECT * FROM tabHot WHERE col_a = 'X'",
                "normalized_query": "SELECT * FROM tabHot WHERE col_a = ?",
                "duration": 50.0, "stack": [],
            },
            {
                "query": "SELECT * FROM tabHot WHERE col_b = 'Y'",
                "normalized_query": "SELECT * FROM tabHot WHERE col_b = ?",
                "duration": 50.0, "stack": [],
            },
        ],
    }

    result = index_suggestions.analyze([recording], empty_context)
    # Two distinct findings (col_a, col_b on same table).
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert len(missing) == 2
    # But only ONE SHOW INDEX call and ONE information_schema call —
    # the classifier cached per-table.
    assert len(show_index_calls) == 1, (
        f"Expected 1 SHOW INDEX call (cached per table), got {len(show_index_calls)}"
    )
    assert len(info_schema_calls) == 1, (
        f"Expected 1 information_schema call (cached per table), got {len(info_schema_calls)}"
    )


def test_severity_scales_with_savings(monkeypatch, empty_context):
	"""Large cumulative savings → High severity."""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabBig", "account")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix4",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 5000,
		"calls": [
			{
				"query": f"SELECT * FROM tabBig WHERE account = 'A{i}'",
				"normalized_query": f"SELECT * FROM tabBig WHERE ACCOUNT = 'A{i}'",
				"duration": 200.0,
				"stack": [],
			}
			for i in range(5)  # 5 × 200ms = 1000ms
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert len(result.findings) == 1
	# 1000ms > HIGH_IMPACT_MS (500)
	assert result.findings[0]["severity"] == "High"


# ---------------------------------------------------------------------------
# v0.5.1 regression guards: filter non-SELECT statements BEFORE the optimizer.
# A real production session showed 47% "parse failures" (334 of 705) because
# transaction markers (BEGIN/COMMIT/SAVEPOINT/SET) were being fed to sql_metadata.
# None of those benefit from an index suggestion, and most raise ValueError.
# The fix: filter by leading keyword before ever calling _optimize_query.
# ---------------------------------------------------------------------------


def test_non_select_statements_are_skipped_without_failures(monkeypatch, empty_context):
	"""BEGIN/COMMIT/SAVEPOINT/SET/etc. must never reach _optimize_query.

	Pre-v0.5.1, these were counted as 'parse failures' and polluted the
	report with a warning saying 'Could not analyze 47% of your queries',
	which was misleading — the queries weren't optimization targets to
	begin with.
	"""
	optimize_call_count = {"n": 0}

	def fake_optimize(query: str):
		optimize_call_count["n"] += 1
		# If this is called at all on a non-SELECT, the filter is broken.
		return None

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix_non_select",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			# All statement types that caused ValueError in production:
			{"query": "BEGIN", "normalized_query": "BEGIN",
			 "duration": 0.5, "stack": []},
			{"query": "COMMIT", "normalized_query": "COMMIT",
			 "duration": 0.3, "stack": []},
			{"query": "ROLLBACK", "normalized_query": "ROLLBACK",
			 "duration": 0.2, "stack": []},
			{"query": "SAVEPOINT sp1", "normalized_query": "SAVEPOINT sp1",
			 "duration": 0.1, "stack": []},
			{"query": "RELEASE SAVEPOINT sp1",
			 "normalized_query": "RELEASE SAVEPOINT sp1",
			 "duration": 0.1, "stack": []},
			{"query": "SET autocommit = 0",
			 "normalized_query": "SET autocommit = 0",
			 "duration": 0.1, "stack": []},
			{"query": "SHOW TABLES", "normalized_query": "SHOW TABLES",
			 "duration": 1.0, "stack": []},
			{"query": "CALL some_proc()", "normalized_query": "CALL some_proc()",
			 "duration": 5.0, "stack": []},
			{"query": "ALTER TABLE tabFoo ADD COLUMN x INT",
			 "normalized_query": "ALTER TABLE tabFoo ADD COLUMN x INT",
			 "duration": 10.0, "stack": []},
		],
	}

	result = index_suggestions.analyze([recording], empty_context)

	# The optimizer must not have been called at all — everything was
	# filtered by query type first.
	assert optimize_call_count["n"] == 0, (
		f"Non-SELECT statements must be skipped before reaching "
		f"_optimize_query; got {optimize_call_count['n']} optimizer calls"
	)

	# No findings (nothing was analyzed).
	assert result.findings == []

	# The skipped count must surface as an informational warning,
	# separately from 'parse failures'.
	assert any("Skipped" in w and "non-SELECT" in w for w in result.warnings), (
		f"Expected a 'Skipped N non-SELECT' warning; got: {result.warnings}"
	)

	# And crucially — there must NOT be a "Could not analyze" parse-failure
	# warning, because nothing actually failed.
	assert not any("Could not analyze" in w for w in result.warnings), (
		f"Non-SELECT skips must not be reported as parse failures; "
		f"got: {result.warnings}"
	)


def test_select_statements_still_analyzed_alongside_skips(monkeypatch, empty_context):
	"""Positive case: mix SELECTs with non-SELECTs. The SELECTs get
	analyzed; the non-SELECTs get skipped; the counts are reported
	separately."""

	def fake_optimize(query: str):
		# Only reachable for the SELECT statements.
		assert query.strip().upper().startswith("SELECT"), (
			f"Only SELECTs should reach the optimizer; got: {query!r}"
		)
		return _FakeDBIndex("tabLead", "status")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix_mixed",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			{"query": "BEGIN", "normalized_query": "BEGIN",
			 "duration": 0.5, "stack": []},
			{"query": "SELECT * FROM tabLead WHERE status = 'Open'",
			 "normalized_query": "SELECT * FROM tabLead WHERE status = ?",
			 "duration": 50.0, "stack": []},
			{"query": "COMMIT", "normalized_query": "COMMIT",
			 "duration": 0.3, "stack": []},
		],
	}

	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1, (
		f"SELECT should still produce a finding; got {missing}"
	)
	# 2 non-SELECTs skipped.
	assert any("Skipped 2" in w for w in result.warnings), (
		f"Expected 'Skipped 2 non-SELECT' warning; got: {result.warnings}"
	)


def test_select_with_leading_comment_is_still_recognised(monkeypatch, empty_context):
	"""Frappe prepends `/* comment */` to some queries for tracing.
	The query-type filter must see through comments and recognise the
	underlying SELECT, otherwise we'd skip legitimate optimization
	targets."""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabCommented", "key_col")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix_comment",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [{
			"query": "/* app: frappe */ SELECT * FROM tabCommented WHERE key_col = ?",
			"normalized_query": "/* app: frappe */ SELECT * FROM tabCommented WHERE key_col = ?",
			"duration": 50.0, "stack": [],
		}],
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1, (
		f"SELECT prefixed with a /* comment */ must still be analyzed; "
		f"got findings={missing}, warnings={result.warnings}"
	)


# ---------------------------------------------------------------------------
# v0.5.1 regression guard: exact production payload from ERPNext item search
# ---------------------------------------------------------------------------
# ERPNext's item search dialog issues a query sql_metadata can't parse:
# correlated subquery in WHERE IN, ORDER BY with if(locate(...), locate(...))
# expressions. DBOptimizer's tuple unpacking then raises:
#
#   ValueError: too many values to unpack (expected 2, got 4)
#
# Pre-fix: this showed up as an Error Log entry ("optimus optimizer
# failure") AND a loud "Could not analyze 1 of N queries — see Error Log"
# warning. Neither is actionable — the user cannot rewrite the ERPNext
# core query and cannot add an index to fix a parse failure. v0.5.1
# classifies these as parser limitations (soft informational warning, no
# Error Log noise).


_ERPNEXT_ITEM_SEARCH_QUERY = """
SELECT tabItem.name,
       item_name,
       item_group,
       customer_code,
       if(length(tabItem.description) > ?, concat(substr(tabItem.description, ?, ?), ?), description) AS description
FROM tabItem
WHERE tabItem.docstatus < ?
  AND tabItem.disabled=?
  AND tabItem.has_variants=?
  AND (tabItem.end_of_life > ?
       OR coalesce(tabItem.end_of_life, ?)=?)
  AND (item_name like ?
       OR description like ?
       OR item_group like ?
       OR customer_code like ?
       OR name like ?
       OR item_code like ?
       OR tabItem.item_code IN
         (SELECT parent
          FROM `tabItem Barcode`
          WHERE barcode LIKE ?)
       OR tabItem.description LIKE ?)
  AND `tabItem`.`is_sales_item`=?
  AND `tabItem`.`has_variants`=?
ORDER BY if(locate(?, name), locate(?, name), ?),
         if(locate(?, item_name), locate(?, item_name), ?),
         idx DESC,
         name,
         item_name
LIMIT ?,
      ?
"""


def test_erpnext_item_search_valueerror_is_soft_skip(monkeypatch, empty_context):
	"""Exact production payload: the ERPNext item-search query triggers
	``ValueError: too many values to unpack (expected 2, got 4)`` inside
	sql_metadata. The analyzer must:

	  1. Not crash (continue processing other queries)
	  2. Not write to frappe.log_error — it's not a profiler bug
	  3. Emit the soft 'Skipped N queries whose shape exceeds…' warning
	  4. Still produce index suggestions for OTHER queries in the same
	     session that DO parse cleanly
	"""

	def fake_optimize(query: str):
		# The complex query raises exactly the production error.
		if "tabItem.item_code IN" in query:
			raise ValueError("too many values to unpack (expected 2, got 4)")
		# A simpler query succeeds so we can assert mixed-behavior works.
		return _FakeDBIndex("tabLead", "status")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabLead": {"name"}},
		column_types_by_table={
			"tabLead": {"name": "varchar", "status": "varchar"},
		},
	)

	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe,
		"log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix_erpnext_item_search",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": _ERPNEXT_ITEM_SEARCH_QUERY,
				"normalized_query": _ERPNEXT_ITEM_SEARCH_QUERY,
				"duration": 180.0,
				"stack": [],
			},
			# A clean query that DOES produce a suggestion, alongside
			# the problematic one — verifies mixed behavior.
			{
				"query": "SELECT * FROM tabLead WHERE status = 'Open'",
				"normalized_query": "SELECT * FROM tabLead WHERE status = ?",
				"duration": 50.0,
				"stack": [],
			},
		],
	}

	result = index_suggestions.analyze([recording], empty_context)

	# 1. No crash, the clean query still produced its suggestion.
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1
	assert "tabLead" in missing[0]["title"]

	# 2. No Error Log entry for the ValueError.
	assert log_error_calls == [], (
		f"Parser-limit ValueError must not log to frappe.log_error. "
		f"Calls recorded: {log_error_calls}"
	)

	# 3. Soft warning present, loud warning absent.
	soft = [w for w in result.warnings if "exceeds the DBOptimizer heuristic" in w]
	loud = [w for w in result.warnings if "Could not analyze" in w]
	assert len(soft) == 1, (
		f"Expected soft parser-limit warning; got warnings={result.warnings}"
	)
	assert loud == [], (
		f"Loud 'Could not analyze' warning must not fire for parser "
		f"limitations; got: {loud}"
	)
	# And the soft warning must count exactly one skipped query.
	assert "Skipped 1 " in soft[0]


def test_typeerror_from_optimizer_is_also_a_soft_skip(monkeypatch, empty_context):
	"""TypeError is another sql_metadata failure mode (unexpected None
	in its token stream). Must be classified as a parser limitation
	alongside ValueError."""

	def fake_optimize(query: str):
		raise TypeError("'NoneType' object is not subscriptable")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe, "log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix_type_err",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 10,
		"calls": [{
			"query": "SELECT 1",
			"normalized_query": "SELECT ?",
			"duration": 5.0,
			"stack": [],
		}],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert log_error_calls == []
	soft = [w for w in result.warnings if "exceeds the DBOptimizer heuristic" in w]
	assert len(soft) == 1


def test_modified_column_is_never_suggested(monkeypatch, empty_context):
	"""'modified' is a Frappe metadata column — updated on every save.
	Indexing it causes write amplification that outweighs any read-side
	gain. Must never be suggested, even when the DBOptimizer heuristic
	points at it. (Origin: a production report surfaced 'Add index on
	tabDocType(modified)' — user feedback: 'Modified fields can't be
	indexed as it would affect the system performance.' We use a non-meta
	table here so it's the *column* blacklist being exercised, not the
	separate meta-table rule that also covers tabDocType.)

	Each query is slow enough (50 ms) to clear the per-query-savings
	floor; the only thing that should suppress this is the column blacklist.
	"""

	def fake_optimize(query):
		return _FakeDBIndex("tabSales Invoice", "modified")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabSales Invoice": {"name", "customer"}},
		column_types_by_table={
			"tabSales Invoice": {
				"name": "varchar",
				"customer": "varchar",
				"modified": "datetime",
			},
		},
	)

	recording = {
		"uuid": "ix_modified_lifecycle",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 500,
		"calls": [
			{
				"query": "SELECT * FROM `tabSales Invoice` ORDER BY modified DESC LIMIT 20",
				"normalized_query": "SELECT * FROM `tabSales Invoice` ORDER BY modified DESC LIMIT ?",
				"duration": 50.0,  # well above per-query floor
				"stack": [],
			}
		] * 10,
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert missing == [], (
		"Suggestion on tabSales Invoice.modified must be suppressed — "
		f"modified is a Frappe metadata column. Got: {missing}"
	)

	# The warning must explicitly name the blacklist reason and include
	# the table.column for user verification.
	blacklist_warnings = [
		w for w in result.warnings
		if "metadata column" in w or "every save" in w
	]
	assert len(blacklist_warnings) == 1, (
		f"Expected blacklist warning; got warnings={result.warnings}"
	)
	assert "tabSales Invoice.modified" in blacklist_warnings[0], (
		f"Warning must name the suppressed column; got: {blacklist_warnings[0]}"
	)


def test_modified_by_column_is_never_suggested(monkeypatch, empty_context):
	"""Same rule applies to modified_by (also updated on every save)."""

	def fake_optimize(query):
		return _FakeDBIndex("tabUser", "modified_by")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabUser": {"name"}},
		column_types_by_table={
			"tabUser": {"name": "varchar", "modified_by": "varchar"},
		},
	)

	recording = {
		"uuid": "ix_modified_by",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			{
				"query": "SELECT * FROM tabUser WHERE modified_by = 'x'",
				"normalized_query": "SELECT * FROM tabUser WHERE modified_by = ?",
				"duration": 20.0,
				"stack": [],
			}
		] * 10,
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert missing == []


def test_never_suggest_skips_schema_lookup(monkeypatch, empty_context):
	"""Performance guard: the blacklist check must run BEFORE the
	information_schema lookups, so we don't waste DB roundtrips on
	a suggestion we already know to drop."""

	def fake_optimize(query):
		return _FakeDBIndex("tabLead", "modified")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	sql_calls: list[str] = []
	import frappe

	class FakeDB:
		def sql(self, query, params=None, as_dict=False):
			sql_calls.append(query)
			return []

	monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

	recording = {
		"uuid": "ix_no_schema_on_blacklist",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 200,
		"calls": [
			{
				"query": "SELECT ... ORDER BY modified",
				"normalized_query": "SELECT ... ORDER BY modified",
				"duration": 20.0,
				"stack": [],
			}
		] * 10,
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	# Classifier's schema lookups must not have run for blacklisted
	# columns — we can drop without checking anything else.
	for sql in sql_calls:
		assert "SHOW INDEX" not in sql, (
			f"Classifier ran SHOW INDEX for a blacklisted column: {sql}"
		)
		assert "information_schema" not in sql, (
			f"Classifier ran information_schema for a blacklisted column: {sql}"
		)


def test_frappe_metadata_columns_are_never_suggested(monkeypatch, empty_context):
	"""v0.6.0: the blacklist now covers the WHOLE Frappe standard-metadata
	column set — `creation`, `owner`, `idx`, `parent`, `docstatus`, … —
	not just the per-save-mutated `modified`/`modified_by`. Even though
	`creation`/`owner` are insert-only (no write amplification), surfacing
	them nudges developers toward a class of change they should make
	deliberately; the EXPLAIN row stays in the report for that. The
	optimizer's suggestion is dropped and a single "metadata columns"
	warning names the suppressed table.column pairs."""

	def fake_optimize(query):
		if "creation" in query:
			return _FakeDBIndex("tabSomething", "creation")
		if "idx" in query:
			return _FakeDBIndex("tabSomething", "idx")
		if "docstatus" in query:
			return _FakeDBIndex("tabSomething", "docstatus")
		return _FakeDBIndex("tabSomething", "owner")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabSomething": {"name"}},
		column_types_by_table={
			"tabSomething": {
				"name": "varchar", "creation": "datetime", "owner": "varchar",
				"idx": "int", "docstatus": "int",
			},
		},
	)

	def _calls(nq):
		return [{"query": nq, "normalized_query": nq, "duration": 50.0, "stack": []}] * 10

	recording = {
		"uuid": "ix_metadata_cols",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 500,
		"calls": (
			_calls("SELECT * FROM tabSomething WHERE creation > ?")
			+ _calls("SELECT * FROM tabSomething WHERE owner = ?")
			+ _calls("SELECT * FROM tabSomething ORDER BY idx ASC")
			+ _calls("SELECT * FROM tabSomething WHERE docstatus = ?")
		),
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert missing == [], (
		f"Suggestions on Frappe metadata columns must be suppressed; got: {missing}"
	)
	# One "metadata columns" warning naming the suppressed columns.
	meta_warnings = [w for w in result.warnings if "metadata column" in w]
	assert len(meta_warnings) == 1, f"Expected a metadata-columns warning; got {result.warnings}"
	for col in ("creation", "owner", "idx", "docstatus"):
		assert f"tabSomething.{col}" in meta_warnings[0]


def test_frappe_meta_tables_are_never_suggested(monkeypatch, empty_context):
	"""v0.6.0: no index suggestion is emitted on a Frappe framework "meta"
	table (tabDocType / tabCustom Field / tabSingles / …) — `bench migrate`
	owns those tables' schema, so a hand-added index is pointless. The
	suggestion is dropped before it's even bucketed (no schema lookup), and
	a single warning names the tables."""

	def fake_optimize(query):
		if "tabDocType" in query:
			return _FakeDBIndex("tabDocType", "module")
		if "tabCustom Field" in query:
			return _FakeDBIndex("tabCustom Field", "dt")
		return _FakeDBIndex("tabSingles", "doctype")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	# A fake DB so a stray classify lookup would be visible (it must NOT
	# happen — the meta-table check runs before classify).
	import frappe
	sql_calls: list[str] = []

	class FakeDB:
		def sql(self, query, params=None, as_dict=False):
			sql_calls.append(query)
			return []

	monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

	def _calls(nq):
		return [{"query": nq, "normalized_query": nq, "duration": 50.0, "stack": []}] * 10

	recording = {
		"uuid": "ix_meta_tables",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 500,
		"calls": (
			_calls("SELECT * FROM `tabDocType` WHERE module = ?")
			+ _calls("SELECT * FROM `tabCustom Field` WHERE dt = ?")
			+ _calls("SELECT value FROM `tabSingles` WHERE doctype = ?")
		),
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert missing == [], f"Suggestions on Frappe meta tables must be suppressed; got: {missing}"
	# No schema lookup should have run for the dropped (meta-table) suggestions.
	for s in sql_calls:
		assert "SHOW INDEX" not in s and "information_schema" not in s
	# One "framework meta tables" warning naming the tables.
	mt_warnings = [w for w in result.warnings if "framework meta table" in w]
	assert len(mt_warnings) == 1, f"Expected a meta-tables warning; got {result.warnings}"
	for tbl in ("tabDocType", "tabCustom Field", "tabSingles"):
		assert tbl in mt_warnings[0]


def test_low_per_query_savings_suggestion_is_suppressed(monkeypatch, empty_context):
	"""A business-column suggestion with 892ms cumulative across 1526
	queries = 0.58ms/query, below the 2ms per-query floor. Must suppress
	the finding entirely and surface a soft warning explaining why.
	(Non-meta table + non-metadata column so it's the per-query *floor*
	being exercised, not the meta-table or column blacklists.)"""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabSales Invoice", "customer")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	# Feed 1526 raw calls with the same normalized_query; the analyzer
	# folds them into one bucket (count=1526, duration=892ms) before
	# running the optimizer once.
	calls = [
		{
			"query": f"SELECT * FROM `tabSales Invoice` WHERE customer = 'c{i}'",
			"normalized_query": "SELECT * FROM `tabSales Invoice` WHERE customer = ?",
			"duration": 892.0 / 1526,  # ~0.585ms per query
			"stack": [],
		}
		for i in range(1526)
	]
	recording = {
		"uuid": "ix_low_avg",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 892.0,
		"calls": calls,
	}

	result = index_suggestions.analyze([recording], empty_context)

	# No Missing Index finding despite 892ms cumulative — per-query savings
	# are below the floor.
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert missing == [], (
		"A suggestion with 0.58ms average savings per query must be "
		f"suppressed — individual queries are already fast enough. "
		f"Got findings: {[f['title'] for f in missing]}"
	)

	# Soft warning explains the suppression.
	assert any(
		"Suppressed 1 index suggestion(s) with average savings below" in w
		for w in result.warnings
	), f"Expected per-query-floor warning; got: {result.warnings}"


def test_low_per_query_skips_classify_lookup(monkeypatch, empty_context):
	"""The per-query floor check must run BEFORE classify_column, so
	we don't waste SHOW INDEX / information_schema lookups on
	suggestions we're going to drop anyway."""

	def fake_optimize(query):
		return _FakeDBIndex("tabSales Invoice", "customer")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	sql_calls = []
	import frappe

	class FakeDB:
		def sql(self, query, params=None, as_dict=False):
			sql_calls.append(query)
			return []

	monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

	calls = [
		{
			"query": f"SELECT ... {i}",
			"normalized_query": "SELECT ? FROM `tabSales Invoice`",
			"duration": 0.5,  # way below 2ms floor
			"stack": [],
		}
		for i in range(200)
	]
	recording = {
		"uuid": "ix_no_schema_calls",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": calls,
	}
	result = index_suggestions.analyze([recording], empty_context)
	# No findings, and no SHOW INDEX / information_schema call was made.
	assert result.findings == []
	for sql in sql_calls:
		assert "SHOW INDEX" not in sql, (
			f"Classifier shouldn't run for dropped suggestions; got: {sql}"
		)
		assert "information_schema" not in sql, (
			f"Classifier shouldn't run for dropped suggestions; got: {sql}"
		)


def test_high_per_query_savings_still_emits(monkeypatch, empty_context):
	"""Positive case: a suggestion with a meaningful per-query
	average (50ms each × 10 queries = 500ms total) must still fire.
	The floor suppresses only marginal cases."""

	def fake_optimize(query):
		return _FakeDBIndex("tabReport", "department")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabReport": {"name"}},
		column_types_by_table={
			"tabReport": {"name": "varchar", "department": "varchar"},
		},
	)

	calls = [
		{
			"query": f"SELECT * FROM tabReport WHERE department = 'd{i}'",
			"normalized_query": "SELECT * FROM tabReport WHERE department = ?",
			"duration": 55.0,  # 55ms per query — well above 2ms floor
			"stack": [],
		}
		for i in range(10)
	]
	recording = {
		"uuid": "ix_high_avg",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 550,
		"calls": calls,
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1
	# 550ms > HIGH_IMPACT_MS (500) → High severity
	assert missing[0]["severity"] == "High"


def test_per_query_floor_boundary_at_exactly_2ms(monkeypatch, empty_context):
	"""Boundary: exactly MIN_AVG_SAVINGS_PER_QUERY_MS (2ms) must
	still fire. The condition is `< MIN`, not `<=`."""
	from optimus.analyzers.index_suggestions import (
		MIN_AVG_SAVINGS_PER_QUERY_MS,
	)

	def fake_optimize(query):
		return _FakeDBIndex("tabAtFloor", "col")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabAtFloor": {"name"}},
		column_types_by_table={
			"tabAtFloor": {"name": "varchar", "col": "varchar"},
		},
	)

	calls = [
		{
			"query": f"SELECT * FROM tabAtFloor WHERE col = 'v{i}'",
			"normalized_query": "SELECT * FROM tabAtFloor WHERE col = ?",
			"duration": MIN_AVG_SAVINGS_PER_QUERY_MS,  # exactly at floor
			"stack": [],
		}
		for i in range(50)
	]
	recording = {
		"uuid": "ix_boundary",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 200,
		"calls": calls,
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1, (
		"Suggestion at exactly MIN_AVG_SAVINGS_PER_QUERY_MS must still "
		"fire (< floor, not <= floor); got: "
		f"{[f['title'] for f in missing]}"
	)


def test_get_query_type_helper_direct():
	"""Direct unit test of the _get_query_type helper — covers the
	regex edge cases without the full analyzer path."""
	from optimus.analyzers.index_suggestions import _get_query_type

	assert _get_query_type("SELECT 1") == "SELECT"
	assert _get_query_type("  select *  from foo") == "SELECT"
	assert _get_query_type("BEGIN") == "BEGIN"
	assert _get_query_type("commit") == "COMMIT"
	assert _get_query_type("SAVEPOINT sp1") == "SAVEPOINT"
	assert _get_query_type("RELEASE SAVEPOINT sp1") == "RELEASE"
	assert _get_query_type("SET autocommit = 0") == "SET"
	assert _get_query_type("SHOW TABLES") == "SHOW"
	assert _get_query_type("/* comment */ SELECT 1") == "SELECT"
	assert _get_query_type("/* multi\nline\ncomment */ select 1") == "SELECT"
	# Empty / None / garbage:
	assert _get_query_type("") == ""
	assert _get_query_type(None) == ""
	assert _get_query_type("   ") == ""
	assert _get_query_type("/* only comment */") == ""


def test_classify_column_blacklists_every_frappe_metadata_column():
	"""v0.6.0: `_classify_column` returns `never_suggest` for every column in
	the Frappe standard-metadata set — checked before any DB lookup, so it
	works in a no-site test env."""
	from optimus.analyzers.base import FRAPPE_METADATA_COLUMNS
	from optimus.analyzers.index_suggestions import _classify_column

	for col in FRAPPE_METADATA_COLUMNS:
		status, reason = _classify_column("tabFoo", col, {}, {})
		assert status == "never_suggest", f"{col!r} should be never_suggest, got {status}"
		assert "framework column" in reason
	# Case-insensitive.
	assert _classify_column("tabFoo", "Creation", {}, {})[0] == "never_suggest"
	assert _classify_column("tabFoo", "  MODIFIED ", {}, {})[0] == "never_suggest"
	# A business column is NOT blacklisted — in a no-DB env it falls through
	# to the legacy "unknown" path (plain DDL kept, not suppressed).
	assert _classify_column("tabFoo", "customer", {}, {})[0] == "unknown"


class TestIsSafeTableName:
	"""v0.6.x: ``_is_safe_table_name`` whitelists the only two table-name
	shapes ``_get_indexed_columns`` is allowed to interpolate into raw
	``SHOW INDEX FROM`` SQL. Everything else routes to an empty-set
	fallback before the DB is touched — fixes the Lens audit's SQL-
	injection finding at index_suggestions.py:198."""

	def test_accepts_tab_doctype_names(self):
		from optimus.analyzers.index_suggestions import _is_safe_table_name
		# Normal Frappe DocType-derived tables.
		assert _is_safe_table_name("tabUser") is True
		assert _is_safe_table_name("tabSales Invoice") is True
		assert _is_safe_table_name("tabHas Role") is True
		assert _is_safe_table_name("tabDocType Action") is True
		assert _is_safe_table_name("tabCustom_Field") is True
		assert _is_safe_table_name("tab__global_search") is True
		# Hyphenated DocType names (rare but valid).
		assert _is_safe_table_name("tabPick-List") is True

	def test_accepts_information_schema_views(self):
		from optimus.analyzers.index_suggestions import _is_safe_table_name
		assert _is_safe_table_name("information_schema.columns") is True
		assert _is_safe_table_name("information_schema.tables") is True
		assert _is_safe_table_name("information_schema.statistics") is True

	def test_rejects_sql_injection_payloads(self):
		from optimus.analyzers.index_suggestions import _is_safe_table_name
		# Classic injection shapes the audit was worried about.
		assert _is_safe_table_name("tabUser`; DROP TABLE x; --") is False
		assert _is_safe_table_name("tabUser` UNION SELECT 1") is False
		assert _is_safe_table_name("tabUser'; --") is False
		assert _is_safe_table_name('tabUser" OR "1"="1') is False
		# Newlines / nulls / backslash escape attempts.
		assert _is_safe_table_name("tabUser\n; DROP") is False
		assert _is_safe_table_name("tabUser\x00 hidden") is False
		assert _is_safe_table_name("tabUser\\") is False

	def test_rejects_non_tab_table_names(self):
		from optimus.analyzers.index_suggestions import _is_safe_table_name
		# Anything outside the tab<…>/information_schema.<…> shapes — no
		# legitimate caller passes these, so they must be rejected.
		assert _is_safe_table_name("user") is False  # no tab prefix
		assert _is_safe_table_name("mysql.user") is False  # other schema
		assert _is_safe_table_name("performance_schema.events_statements_summary") is False
		assert _is_safe_table_name("foo.tabUser") is False  # schema-prefixed
		# Unicode oddities — conservative reject.
		assert _is_safe_table_name("tabUser™") is False

	def test_defensive_against_non_string_input(self):
		from optimus.analyzers.index_suggestions import _is_safe_table_name
		assert _is_safe_table_name(None) is False
		assert _is_safe_table_name("") is False
		assert _is_safe_table_name(123) is False
		assert _is_safe_table_name(["tabUser"]) is False
		assert _is_safe_table_name({"name": "tabUser"}) is False


class TestGetIndexedColumnsGuard:
	"""The SHOW INDEX FROM call routes through ``_is_safe_table_name``
	before touching the DB. An unsafe name must not reach ``frappe.db.sql``
	at all — proven by raising loudly from the patched stub."""

	def test_unsafe_table_name_returns_empty_set_without_sql(self):
		import sys
		import types
		from unittest.mock import patch

		stub = types.ModuleType("frappe")
		def _explode(*args, **kwargs):
			raise AssertionError(
				"SHOW INDEX FROM reached frappe.db.sql with an unsafe name — "
				"the guard at index_suggestions._get_indexed_columns is broken"
			)
		stub.db = types.SimpleNamespace(sql=_explode)
		with patch.dict(sys.modules, {"frappe": stub}):
			from optimus.analyzers.index_suggestions import _get_indexed_columns
			# Each of these must short-circuit to an empty set.
			assert _get_indexed_columns("tabUser`; DROP TABLE x; --") == set()
			assert _get_indexed_columns("") == set()
			assert _get_indexed_columns(None) == set()
			assert _get_indexed_columns("mysql.user") == set()

