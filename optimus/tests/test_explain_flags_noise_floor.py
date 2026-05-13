# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.2 round 3 noise-floor + framework-DocType filters
in explain_flags.

Production report had ~85 'Full table scan on tab<DocType>' findings
at 0-1ms impact, most on stock Frappe DocTypes (tabDocField,
tabWorkspace, tabCustom Field, ...). App developers can't add an
index to those, and the ~0ms impact means even if they could the
cost wouldn't be measurable. Two filters now suppress these:

1. Noise floor — findings with impact < 5ms AND count < 5 drop
2. Framework DocType filter — scans on stock DocTypes drop
"""

from optimus.analyzers import explain_flags
from optimus.analyzers.base import AnalyzeContext


def _build_call(table, query_duration=50.0, explain_type="ALL", rows=1000,
                extra="", filtered=None, user_stack=True):
	"""Build a recording call that triggers a Full Table Scan finding."""
	stack = (
		[{"filename": "apps/myapp/controllers/foo.py", "lineno": 10,
		  "function": "f"}]
		if user_stack else
		[{"filename": "frappe/model/document.py", "lineno": 500,
		  "function": "save"}]
	)
	row = {"table": table, "type": explain_type, "rows": rows, "Extra": extra}
	if filtered is not None:
		row["filtered"] = filtered
	return {
		"query": f"SELECT * FROM `{table}`",
		"normalized_query": f"SELECT * FROM `{table}`",
		"duration": query_duration,
		"stack": stack,
		"explain_result": [row],
		"exact_copies": 1,
		"normalized_copies": 1,
	}


def _recording_with(calls):
	return {
		"uuid": "t", "path": "/", "method": "GET", "cmd": None,
		"event_type": "HTTP Request", "duration": 100.0, "calls": calls,
	}


def test_below_noise_floor_single_occurrence_tiny_impact_is_dropped():
	"""A Full Scan on a custom app's DocType that ran ONCE for 0.5ms
	is not worth reporting — the user can't measurably improve it."""
	recording = _recording_with([
		_build_call("tabMyAppThing", query_duration=0.5),
	])
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = explain_flags.analyze([recording], ctx)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert scans == [], (
		"Sub-noise-floor finding must be suppressed. "
		f"Got: {[f['title'] for f in scans]}"
	)
	# Warning surfaces the suppression count.
	warnings_blob = " ".join(result.warnings)
	assert "noise floor" in warnings_blob


def test_high_count_above_noise_floor_is_kept():
	"""Same tiny-duration Full Scan repeated 10 times → aggregate
	count of 10 clears the noise floor (count >= 5)."""
	recording = _recording_with([
		_build_call("tabMyAppThing", query_duration=0.5)
		for _ in range(10)
	])
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = explain_flags.analyze([recording], ctx)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1, (
		"10 occurrences (>= 5 count floor) must produce a finding "
		"even with tiny impact. A persistent pattern is actionable."
	)


def test_high_impact_low_count_is_kept():
	"""One full scan that cost 100ms is actionable even though it
	only ran once — the single query is slow enough to investigate."""
	recording = _recording_with([
		_build_call("tabMyAppThing", query_duration=100.0),
	])
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = explain_flags.analyze([recording], ctx)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1


def test_framework_doctype_scan_is_dropped_regardless_of_impact(monkeypatch):
	"""A Full Scan on a stock Frappe DocType is NOT actionable
	regardless of impact. The user can't add an index to
	tabDocField — that requires an upstream patch.

	Monkeypatch the DocType-app cache so the test doesn't need a
	live bench."""
	# Prime the cache with a set that includes our target table.
	monkeypatch.setattr(
		explain_flags, "_framework_doctypes_cache",
		frozenset({"DocField", "Workspace"}),
	)
	recording = _recording_with([
		_build_call("tabDocField", query_duration=100.0),  # big impact
	])
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = explain_flags.analyze([recording], ctx)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert scans == [], (
		"Scan on framework-owned tabDocField must be suppressed even "
		"with high impact — the user can't add an upstream index. "
		f"Got: {[f['title'] for f in scans]}"
	)
	# Warning explains the suppression.
	warnings_blob = " ".join(result.warnings)
	assert "framework-owned" in warnings_blob or "stock Frappe" in warnings_blob


def test_user_app_doctype_scan_is_kept(monkeypatch):
	"""Mirror of above — a scan on `tabMyAppCustomDocType` (NOT in
	the framework set) must NOT be suppressed by the DocType filter."""
	monkeypatch.setattr(
		explain_flags, "_framework_doctypes_cache",
		frozenset({"DocField", "Workspace"}),
	)
	recording = _recording_with([
		_build_call("tabMyAppCustomDocType", query_duration=100.0),
	])
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = explain_flags.analyze([recording], ctx)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1


def test_both_filters_can_coexist(monkeypatch):
	"""Seed a mix: framework-doctype (dropped) + user-app sub-noise
	(dropped) + user-app actionable (kept). Exactly one survives."""
	monkeypatch.setattr(
		explain_flags, "_framework_doctypes_cache",
		frozenset({"DocField"}),
	)
	recording = _recording_with([
		_build_call("tabDocField", query_duration=50.0),            # drop: framework
		_build_call("tabMyQuietThing", query_duration=0.5),         # drop: noise floor
		_build_call("tabMyHotThing", query_duration=100.0),         # keep
	])
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = explain_flags.analyze([recording], ctx)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	assert "tabMyHotThing" in scans[0]["title"]
