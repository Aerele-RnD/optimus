# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.5.3 callsite-shape normalizer.

Two analyzers produce two different callsite shapes:

  n_plus_one / redundant_calls / explain_flags:
      callsite = {"filename": "...", "lineno": 456, "function": "..."}

  top_queries (Slow Query findings):
      callsite = "apps/myapp/foo.py:456"   # pre-formatted string

Before the normalizer, ``_app_from_finding`` crashed on the string
shape with ``AttributeError: 'str' object has no attribute 'get'``
on any session containing Slow Query findings. Production trigger:
a Purchase Order Approve session on bluspring_customization hit
this and failed to generate any report at all.

_finding_to_dict now normalizes at load time; _app_from_finding
double-checks so direct callers (tests, retry paths, future direct
consumers of _bucket_findings_by_app) can't crash on un-normalized
data either.
"""

import json
import types

from optimus.renderer import (
	_OTHER_APP_LABEL,
	_app_from_finding,
	_finding_to_dict,
	_normalize_callsite,
)


class TestNormalizeCallsite:
	def test_dict_form_passes_through(self):
		cs = {"filename": "apps/myapp/foo.py", "lineno": 42, "function": "f"}
		assert _normalize_callsite(cs) == cs

	def test_string_file_and_lineno(self):
		"""top_queries emits "file.py:lineno"."""
		cs = _normalize_callsite("apps/myapp/foo.py:456")
		assert cs == {
			"filename": "apps/myapp/foo.py",
			"lineno": 456,
			"function": "",
		}

	def test_string_without_lineno(self):
		cs = _normalize_callsite("apps/myapp/foo.py")
		assert cs == {
			"filename": "apps/myapp/foo.py",
			"lineno": None,
			"function": "",
		}

	def test_string_with_absolute_path(self):
		cs = _normalize_callsite("/home/frappe/bench/apps/myapp/foo.py:10")
		assert cs["filename"] == "/home/frappe/bench/apps/myapp/foo.py"
		assert cs["lineno"] == 10

	def test_string_with_windows_drive_letter_keeps_path(self):
		"""Partitioning from the RIGHT means a drive letter path like
		'C:\\x\\foo.py:12' keeps the 'C:' intact."""
		cs = _normalize_callsite("C:\\apps\\myapp\\foo.py:12")
		assert cs["filename"] == "C:\\apps\\myapp\\foo.py"
		assert cs["lineno"] == 12

	def test_empty_string_returns_none(self):
		assert _normalize_callsite("") is None

	def test_none_returns_none(self):
		assert _normalize_callsite(None) is None

	def test_unknown_type_returns_none(self):
		# e.g. an int slipped in from somewhere — don't crash, skip.
		assert _normalize_callsite(12345) is None

	def test_non_numeric_suffix_not_treated_as_lineno(self):
		"""'foo.py:bar' keeps the filename intact and sets lineno=None
		(don't try to parse 'bar' as an int)."""
		cs = _normalize_callsite("foo.py:bar")
		assert cs["filename"] == "foo.py:bar"
		assert cs["lineno"] is None


class TestAppFromFindingRegression:
	"""Regression: the exact production crash that motivated this fix."""

	def test_slow_query_with_string_callsite_does_not_crash(self):
		"""Production crash: top_queries emits a Slow Query finding with
		callsite as a string. _app_from_finding crashed with
		AttributeError: 'str' object has no attribute 'get'."""
		finding = {
			"finding_type": "Slow Query",
			"severity": "High",
			"title": "Slow query: 1374ms",
			"estimated_impact_ms": 1374.14,
			"affected_count": 1,
			"technical_detail": {
				"normalized_query": "SELECT ...",
				# This is the real production shape from top_queries.py
				"callsite": (
					"bluspring_customization/bluspring_customization/"
					"bluspring_customization/doctype/purchase_order/"
					"purchase_order.py:456"
				),
			},
		}
		# Must not raise.
		app = _app_from_finding(finding)
		assert app == "bluspring_customization", (
			f"First-segment app should be extracted from the string "
			f"callsite; got: {app!r}"
		)

	def test_dict_callsite_still_works(self):
		"""Regression boundary: existing dict-shape callers unaffected."""
		finding = {
			"finding_type": "N+1 Query",
			"technical_detail": {
				"callsite": {
					"filename": "apps/myapp/foo.py",
					"lineno": 10,
					"function": "f",
				},
			},
		}
		assert _app_from_finding(finding) == "myapp"

	def test_missing_callsite_routes_to_other(self):
		finding = {"finding_type": "Slow Hot Path", "technical_detail": {}}
		assert _app_from_finding(finding) == _OTHER_APP_LABEL

	def test_empty_string_callsite_routes_to_other(self):
		finding = {
			"finding_type": "Slow Query",
			"technical_detail": {"callsite": ""},
		}
		assert _app_from_finding(finding) == _OTHER_APP_LABEL


class TestFindingToDictNormalizes:
	"""When loading findings from DB, _finding_to_dict must convert
	the string callsite to dict shape before handing downstream."""

	def _row(self, detail):
		row = types.SimpleNamespace()
		row.finding_type = "Slow Query"
		row.severity = "High"
		row.title = "x"
		row.customer_description = "y"
		row.estimated_impact_ms = 100.0
		row.affected_count = 1
		row.action_ref = "0"
		row.technical_detail_json = json.dumps(detail)
		return row

	def test_string_callsite_normalized_to_dict(self):
		row = self._row({"callsite": "apps/myapp/foo.py:42"})
		finding = _finding_to_dict(row)
		cs = finding["technical_detail"]["callsite"]
		assert isinstance(cs, dict)
		assert cs["filename"] == "apps/myapp/foo.py"
		assert cs["lineno"] == 42

	def test_dict_callsite_passes_through_unchanged(self):
		row = self._row({"callsite": {
			"filename": "apps/myapp/foo.py",
			"lineno": 42,
			"function": "f",
		}})
		finding = _finding_to_dict(row)
		cs = finding["technical_detail"]["callsite"]
		assert cs == {
			"filename": "apps/myapp/foo.py",
			"lineno": 42,
			"function": "f",
		}

	def test_no_callsite_field_unchanged(self):
		row = self._row({"normalized_query": "SELECT 1"})
		finding = _finding_to_dict(row)
		assert "callsite" not in finding["technical_detail"]


class TestEndToEndRender:
	"""The real scenario: a session with a Slow Query finding (string
	callsite) + an N+1 finding (dict callsite) must render without
	crashing, and both must land in correct app buckets."""

	def test_mixed_callsite_shapes_render_without_error(self):
		from optimus import renderer

		doc = types.SimpleNamespace()
		doc.title = "T"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-17"
		doc.stopped_at = "2026-04-17"
		doc.notes = None
		doc.top_severity = "High"
		doc.total_duration_ms = 2000
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

		# Slow Query with STRING callsite (top_queries shape).
		slow_row = types.SimpleNamespace()
		slow_row.finding_type = "Slow Query"
		slow_row.severity = "High"
		slow_row.title = "Slow query: 1374ms"
		slow_row.customer_description = "desc"
		slow_row.estimated_impact_ms = 1374.0
		slow_row.affected_count = 1
		slow_row.action_ref = "0"
		slow_row.technical_detail_json = json.dumps({
			"normalized_query": "SELECT 1",
			"callsite": "apps/myapp/foo.py:456",
		})

		# N+1 with DICT callsite.
		n1_row = types.SimpleNamespace()
		n1_row.finding_type = "N+1 Query"
		n1_row.severity = "Medium"
		n1_row.title = "Same query ran 15× at myapp/foo.py:10"
		n1_row.customer_description = "desc"
		n1_row.estimated_impact_ms = 45.0
		n1_row.affected_count = 15
		n1_row.action_ref = "0"
		n1_row.technical_detail_json = json.dumps({
			"callsite": {
				"filename": "apps/myapp/foo.py",
				"lineno": 10,
				"function": "f",
			},
			"occurrences": 15,
			"average_time_ms": 3.0,
			"total_time_ms": 45.0,
		})

		doc.findings = [slow_row, n1_row]

		# The pre-fix crash was here:
		#   AttributeError: 'str' object has no attribute 'get'
		html = renderer.render(doc, recordings=[])

		# Both findings rendered — titles present.
		assert "Slow query: 1374ms" in html
		assert "Same query ran 15× at myapp/foo.py:10" in html
		# Both attributed to myapp — since they're the only app, the
		# bucket-wrapper short-circuits (single-app flat rendering),
		# but the callsite text appears in the details block.
		assert "apps/myapp/foo.py:456" in html
