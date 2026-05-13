# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for optimus.analyzers.explain_flags.

Exercises all four red-flag patterns: Full Table Scan, Filesort,
Temporary Table, and Low Filter Ratio (the one we just added in fix #2).
"""

from optimus.analyzers import explain_flags


def test_full_table_scan_detected(full_scan_recording, empty_context):
	result = explain_flags.analyze([full_scan_recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) >= 1
	# Fixture rows examined is 50000 > HIGH_ROWS_EXAMINED, so severity High
	assert any(f["severity"] == "High" for f in scans)


def test_filesort_detected(full_scan_recording, empty_context):
	result = explain_flags.analyze([full_scan_recording], empty_context)
	filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
	assert len(filesorts) >= 1


def test_temporary_table_detected(full_scan_recording, empty_context):
	result = explain_flags.analyze([full_scan_recording], empty_context)
	temps = [f for f in result.findings if f["finding_type"] == "Temporary Table"]
	assert len(temps) >= 1


def test_low_filter_ratio_detected(empty_context):
	"""Fix #2: filtered < 10 check must now fire."""
	recording = {
		"uuid": "lf1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 300,
		"calls": [
			{
				"query": "SELECT * FROM tabLead WHERE status = ?",
				"normalized_query": "SELECT * FROM tabLead WHERE STATUS = ?",
				"duration": 250.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabLead",
						"type": "ref",
						"key": "status",
						"rows": 5000,
						"filtered": 2.0,  # Only 2% of scanned rows match
						"Extra": "Using where",
					}
				],
			}
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	low_filter = [f for f in result.findings if f["finding_type"] == "Low Filter Ratio"]
	assert len(low_filter) == 1
	assert "tabLead" in low_filter[0]["title"]


def test_low_filter_ratio_ignores_small_result_sets(empty_context):
	"""If only 50 rows were examined, low filter ratio isn't worth flagging."""
	recording = {
		"uuid": "lf2",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 10,
		"calls": [
			{
				"query": "SELECT * FROM tabSmallTable WHERE x = ?",
				"normalized_query": "SELECT * FROM tabSmallTable WHERE x = ?",
				"duration": 2.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabSmallTable",
						"type": "ALL",
						"rows": 50,  # below LOW_FILTERED_MIN_ROWS=100
						"filtered": 5.0,
						"Extra": "",
					}
				],
			}
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	low_filter = [f for f in result.findings if f["finding_type"] == "Low Filter Ratio"]
	assert low_filter == []


def test_clean_query_no_findings(clean_recording, empty_context):
	"""A query using an index with good selectivity should emit nothing."""
	result = explain_flags.analyze([clean_recording], empty_context)
	assert result.findings == []


def test_findings_deduplicated_by_table(empty_context):
	"""Multiple queries with the same full-scan pattern on the same table
	should collapse to one finding with aggregated impact.
	"""
	recording = {
		"uuid": "dedup1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": f"SELECT * FROM tabBigTable WHERE name = '{i}'",
				"normalized_query": "SELECT * FROM tabBigTable WHERE NAME = ?",
				"duration": 80.0,
				"stack": [],
				"explain_result": [
					{"table": "tabBigTable", "type": "ALL", "rows": 50000, "Extra": ""}
				],
			}
			for i in range(5)
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1  # deduplicated
	assert scans[0]["affected_count"] == 5
	assert scans[0]["estimated_impact_ms"] == 400.0  # 5 × 80ms


def test_empty_recordings(empty_context):
	assert explain_flags.analyze([], empty_context).findings == []


def test_rows_as_string_does_not_crash_analyzer(empty_context):
	"""v0.5.1 regression guard: production report showed
	'Analyzer optimus.analyzers.explain_flags failed' because
	some MariaDB drivers return `rows` as a string (Decimal-to-str
	through certain adapter versions). The `rows_examined > N` int
	comparison then crashed with TypeError in Python 3, taking out
	the whole analyzer and dropping all Full Table Scan / Filesort
	/ Temporary Table / Low Filter Ratio findings for the session.

	_to_int coercion + per-row try/except means a string `rows` is
	either coerced cleanly (if parseable) or treated as 0.
	"""
	recording = {
		"uuid": "str-rows",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 300,
		"calls": [
			{
				"query": "SELECT * FROM tabBig WHERE x = ?",
				"normalized_query": "SELECT * FROM tabBig WHERE x = ?",
				"duration": 250.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabBig",
						"type": "ALL",
						"rows": "50000",  # STRING, not int
						"Extra": "",
					},
				],
			},
		],
	}

	# Must NOT raise. The old code would crash at the string-vs-int
	# comparison in _inspect_row.
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	# Coerced correctly → 50000 > HIGH_ROWS_EXAMINED → High severity
	assert scans[0]["severity"] == "High"


def test_rows_as_none_does_not_crash_analyzer(empty_context):
	"""None is a valid value for EXPLAIN.rows in some edge cases
	(subquery materialization, const access, etc.). Must coerce to
	0 and continue, not crash."""
	recording = {
		"uuid": "none-rows",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT 1",
				"duration": 10.0,
				"stack": [],
				"explain_result": [
					{"table": "tabAny", "type": "ALL", "rows": None, "Extra": ""},
				],
			},
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	# rows=0 still triggers Full Table Scan (since type=ALL) but
	# severity drops to Medium (0 < HIGH_ROWS_EXAMINED).
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	assert scans[0]["severity"] == "Medium"


def test_malformed_row_is_isolated_not_crashing(empty_context):
	"""A single unparseable row must not kill the whole session's
	explain_flags output. The per-row try/except catches the error,
	counts it, logs a sample to the warnings list, and continues
	with the remaining rows.
	"""
	import unittest.mock as mock

	recording = {
		"uuid": "bad-row",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 200,
		"calls": [
			{
				"query": "SELECT * FROM tabOk WHERE x = ?",
				"normalized_query": "SELECT * FROM tabOk WHERE x = ?",
				"duration": 100.0,
				"stack": [],
				"explain_result": [
					# A legit full-scan row that should still produce a finding
					{"table": "tabOk", "type": "ALL", "rows": 50000, "Extra": ""},
				],
			},
		],
	}

	# Monkeypatch _inspect_row to raise on the first call only,
	# simulating a weird row shape we can't foresee. The per-row
	# try/except should catch it and continue.
	original = explain_flags._inspect_row
	call_count = {"n": 0}

	def sometimes_fail(*args, **kwargs):
		call_count["n"] += 1
		if call_count["n"] == 1:
			raise TypeError("simulated: '<' not supported between str and int")
		return original(*args, **kwargs)

	with mock.patch.object(explain_flags, "_inspect_row", side_effect=sometimes_fail):
		result = explain_flags.analyze([recording, recording], empty_context)

	# Row 1 failed, row 2 succeeded. One Full Table Scan finding from
	# the second recording's row.
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	# And the failure was surfaced as a warning.
	assert any("explain_flags: could not parse" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# v0.5.1: row floor for Filesort / Temporary Table findings
# ---------------------------------------------------------------------------
# A real production run flagged "Filesort on tabCustom DocPerm" from a
# single-row parent lookup:
#   SELECT * FROM `tabCustom DocPerm` WHERE `parent`=? ORDER BY `creation` ASC
# with explain rows=1, type=ref, key=parent. The filesort is on ONE row —
# free in practice, and the user can't act on it anyway because `parent` is
# already the ref key. Flagging it was pure noise. These tests cover the
# MIN_ROWS_TO_FLAG_SORT row floor that suppresses the false positive.


def test_filesort_on_single_row_is_not_flagged(empty_context):
	"""Regression guard — exact payload from the production report:
	a ref lookup on tabCustom DocPerm.parent with rows=1 that happens
	to filesort the result. Must NOT emit a Filesort finding."""
	recording = {
		"uuid": "filesort-tiny",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 5,
		"calls": [{
			"query": "SELECT * FROM `tabCustom DocPerm` WHERE `parent`=? ORDER BY `creation` ASC",
			"normalized_query": "SELECT * FROM `tabCustom DocPerm` WHERE `parent`=? ORDER BY `creation` ASC",
			"duration": 1.2,
			"stack": [],
			"explain_result": [{
				"id": 1,
				"select_type": "SIMPLE",
				"table": "tabCustom DocPerm",
				"type": "ref",
				"possible_keys": "parent",
				"key": "parent",
				"key_len": "563",
				"ref": "const",
				"rows": "1",  # string, as seen in production
				"Extra": "Using index condition; Using where; Using filesort",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
	assert filesorts == [], (
		"Filesort on a 1-row result is free — must not emit a finding. "
		f"Got: {filesorts}"
	)


def test_temporary_table_on_tiny_result_is_not_flagged(empty_context):
	"""Same row floor applies to Temporary Table. Materializing a 5-row
	intermediate is free."""
	recording = {
		"uuid": "temp-tiny",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 5,
		"calls": [{
			"query": "SELECT a, SUM(b) FROM tabSmall GROUP BY a",
			"normalized_query": "SELECT a, SUM(b) FROM tabSmall GROUP BY a",
			"duration": 2.0,
			"stack": [],
			"explain_result": [{
				"table": "tabSmall",
				"type": "ALL",
				"rows": 5,
				"Extra": "Using temporary; Using filesort",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	temps = [f for f in result.findings if f["finding_type"] == "Temporary Table"]
	filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
	assert temps == [], f"Temporary Table on 5 rows is free; got: {temps}"
	assert filesorts == [], f"Filesort on 5 rows is free; got: {filesorts}"


def test_filesort_at_exactly_the_floor_is_flagged(empty_context):
	"""Boundary: rows == MIN_ROWS_TO_FLAG_SORT (100) must still be
	flagged. Sorting 100 rows without an index starts to matter.
	The condition is `>= MIN_ROWS_TO_FLAG_SORT`, not strict >."""
	from optimus.analyzers.explain_flags import MIN_ROWS_TO_FLAG_SORT
	recording = {
		"uuid": "filesort-floor",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 20,
		"calls": [{
			"query": "SELECT * FROM tabMid WHERE x = ? ORDER BY y",
			"normalized_query": "SELECT * FROM tabMid WHERE x = ? ORDER BY y",
			"duration": 15.0,
			"stack": [],
			"explain_result": [{
				"table": "tabMid",
				"type": "ref",
				"key": "x",
				"rows": MIN_ROWS_TO_FLAG_SORT,  # exactly at floor
				"Extra": "Using where; Using filesort",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
	assert len(filesorts) == 1, (
		f"Filesort at exactly MIN_ROWS_TO_FLAG_SORT={MIN_ROWS_TO_FLAG_SORT} "
		f"must still fire (>=, not >); got: {filesorts}"
	)


def test_filesort_above_floor_still_flagged(empty_context):
	"""Positive case: a filesort on 5000 rows (well above the floor)
	must still produce a finding, so the floor doesn't accidentally
	suppress legitimate ones."""
	recording = {
		"uuid": "filesort-real",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 200,
		"calls": [{
			"query": "SELECT * FROM tabBig WHERE org = ? ORDER BY created",
			"normalized_query": "SELECT * FROM tabBig WHERE org = ? ORDER BY created",
			"duration": 150.0,
			"stack": [],
			"explain_result": [{
				"table": "tabBig",
				"type": "ref",
				"key": "org",
				"rows": 5000,
				"Extra": "Using where; Using filesort",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
	assert len(filesorts) == 1


# ---------------------------------------------------------------------------
# v0.5.2: callsite + alias filtering
# ---------------------------------------------------------------------------
# A production report had 70+ "Full table scan on <tabSomething>"
# findings and dozens of "Full table scan on a / c / p / addr" findings.
# The framework-table ones came from Frappe/ERPNext internal queries the
# user can't patch; the single-letter ones were SQL JOIN aliases the user
# can't index at all. Both classes now get filtered.


def test_full_scan_from_framework_callsite_is_suppressed(empty_context):
	"""The query lives inside frappe/model/document.py — user can't
	add an index to fix a Frappe framework query. Skip the findings
	for this call entirely (Full Scan + whatever else)."""
	recording = {
		"uuid": "fw-scan",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 500,
		"calls": [{
			"query": "SELECT * FROM `tabAccounting Dimension`",
			"normalized_query": "SELECT * FROM `tabAccounting Dimension`",
			"duration": 150.0,
			"stack": [
				{"filename": "frappe/app.py", "lineno": 120, "function": "app"},
				{"filename": "frappe/model/document.py", "lineno": 300, "function": "load_from_db"},
				{"filename": "frappe/cache_manager.py", "lineno": 50, "function": "get_doc"},
			],
			"explain_result": [{
				"table": "tabAccounting Dimension",
				"type": "ALL",
				"rows": 50000,
				"Extra": "",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert scans == [], (
		"Full Table Scan finding from a frappe/* callsite must be "
		"suppressed — user can't add an index to fix a framework "
		f"query. Got: {[f['title'] for f in scans]}"
	)
	# And the suppression surfaces as a warning so the user knows
	# why they don't see this in the findings list.
	assert any(
		"Frappe framework code" in w for w in result.warnings
	), f"Expected framework-callsite warning; got: {result.warnings}"


def test_alias_table_name_is_suppressed(empty_context):
	"""EXPLAIN rows for JOIN queries often have the table field as
	a single-letter alias ('a', 'c', 'ap'). 'Full table scan on a'
	isn't actionable — the user can't index an alias."""
	recording = {
		"uuid": "alias-scan",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 200,
		"calls": [{
			"query": "SELECT * FROM tabItem a JOIN tabItem_Price c ON a.name = c.item",
			"normalized_query": (
				"SELECT * FROM tabItem a JOIN tabItem_Price c "
				"ON a.name = c.item"
			),
			"duration": 80.0,
			"stack": [
				{"filename": "apps/myapp/controllers/sync.py", "lineno": 42,
				 "function": "sync_prices"},
			],
			"explain_result": [
				# Alias "a" and "c" — both should be dropped.
				{"table": "a", "type": "ALL", "rows": 50000, "Extra": ""},
				{"table": "c", "type": "ALL", "rows": 50000, "Extra": ""},
			],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert scans == [], (
		"Single-letter aliases 'a' / 'c' must be suppressed as "
		f"un-indexable. Got: {[f['title'] for f in scans]}"
	)
	# Warning should mention the alias suppression.
	assert any("alias" in w.lower() for w in result.warnings), (
		f"Expected alias-suppression warning; got: {result.warnings}"
	)


def test_alias_helper_distinguishes_real_tables_from_aliases():
	"""Direct unit test of the _is_likely_alias helper."""
	from optimus.analyzers.explain_flags import _is_likely_alias

	# REAL tables — kept
	for real in (
		"tabItem", "tabSales Invoice", "tabCustom Field", "tabDocType",
		"MyCustomTable",       # capital letters → real
		"some_log_table",      # has underscore → real
	):
		assert _is_likely_alias(real) is False, (
			f"{real!r} is a real table and must NOT be classified as alias"
		)

	# ALIASES — filtered
	for alias in ("a", "c", "p", "ap", "cd", "addr", "d"):
		assert _is_likely_alias(alias) is True, (
			f"{alias!r} is a SQL alias and should be filtered"
		)

	# MariaDB synthetic derived/subquery markers
	for synthetic in ("<derived2>", "<subquery1>", "<union3,4>"):
		assert _is_likely_alias(synthetic) is True

	# v0.5.2 round 4: INFORMATION_SCHEMA / MariaDB metadata views —
	# they're real tables but the user can't add indexes to them. A
	# production report showed "Full table scan on columns — ~129ms"
	# and "Full table scan on tables — ~21ms" cluttering actionable
	# findings; these are INFORMATION_SCHEMA.columns / .tables, not
	# user-addressable. Treat them as aliases (suppressed).
	for system_table in (
		"columns", "tables", "schemata", "statistics", "routines",
		"triggers", "views", "processlist", "key_column_usage",
		"referential_constraints", "table_constraints",
	):
		assert _is_likely_alias(system_table) is True, (
			f"{system_table!r} is an INFORMATION_SCHEMA view; user "
			"cannot add an index — must be filtered as alias/noise"
		)

	# Edge cases
	assert _is_likely_alias("") is True
	assert _is_likely_alias(None) is True


def test_user_code_callsite_still_emits_findings(empty_context):
	"""Positive case: a Full Scan from genuine user-app code
	(apps/myapp/...) must still produce a finding. The filter is
	narrow — it only removes framework + alias noise, not real
	findings."""
	recording = {
		"uuid": "user-scan",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 500,
		"calls": [{
			"query": "SELECT * FROM tabCustomInvoice WHERE status = ?",
			"normalized_query": "SELECT * FROM tabCustomInvoice WHERE status = ?",
			"duration": 200.0,
			"stack": [
				{"filename": "apps/myapp/reports/overdue.py", "lineno": 55,
				 "function": "collect"},
			],
			"explain_result": [{
				"table": "tabCustomInvoice",
				"type": "ALL",
				"rows": 50000,
				"Extra": "",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1, (
		"User-code Full Scan must still produce a finding. "
		f"Got: {[f['title'] for f in result.findings]}"
	)
	assert "tabCustomInvoice" in scans[0]["title"]


def test_no_stack_falls_through_to_legacy_behavior(empty_context):
	"""Pre-v0.5.2 recordings might not have a `stack` on each call.
	The framework-callsite filter must gracefully pass these
	through (emit findings as before) rather than dropping them."""
	recording = {
		"uuid": "nostack",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 500,
		"calls": [{
			"query": "SELECT * FROM tabItem WHERE name = ?",
			"normalized_query": "SELECT * FROM tabItem WHERE name = ?",
			"duration": 150.0,
			# NOTE: no "stack" field — pre-v0.5.2 recording.
			"explain_result": [{
				"table": "tabItem",
				"type": "ALL",
				"rows": 50000,
				"Extra": "",
			}],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1, (
		"Missing stack must not drop findings — legacy recordings "
		"should emit findings as before. "
		f"Got: {[f['title'] for f in result.findings]}"
	)


def test_filtered_as_string_does_not_crash(empty_context):
	"""`filtered` can also arrive as a string from some drivers.
	Must coerce cleanly, not crash."""
	recording = {
		"uuid": "str-filtered",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 300,
		"calls": [
			{
				"query": "SELECT * FROM tabLead WHERE status = ?",
				"normalized_query": "SELECT * FROM tabLead WHERE status = ?",
				"duration": 250.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabLead",
						"type": "ref",
						"key": "status",
						"rows": 5000,
						"filtered": "2.5",  # STRING, not float
						"Extra": "Using where",
					},
				],
			},
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	low_filter = [f for f in result.findings if f["finding_type"] == "Low Filter Ratio"]
	assert len(low_filter) == 1
