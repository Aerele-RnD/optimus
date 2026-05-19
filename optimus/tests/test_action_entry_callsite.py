# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for ``renderer._action_entry_callsite`` and the dotted-entry
derivation that feeds it.

The per-action breakdown and the RQ Jobs section identify an action
only by a dotted module path (``ugly_code.python.common.bg_recheck_users``)
or a URL. This resolves that entry point to ``file:line`` + a ±1-line source
snippet. The tests resolve a real function in *this* app
(``optimus.renderer.render``) so they're hermetic — no running site,
no dependence on frappe core's layout.
"""

import inspect
import os

from optimus import renderer

_DOTTED = "optimus.renderer.render"


def _expected_lineno():
	return inspect.unwrap(renderer.render).__code__.co_firstlineno


class TestActionDottedEntry:
	def test_background_job_path_is_the_dotted_entry(self):
		assert renderer._action_dotted_entry(
			{"event_type": "RQ Job", "path": "ugly_code.python.common.bg_recheck_users"}
		) == "ugly_code.python.common.bg_recheck_users"

	def test_http_api_method_path_strips_prefix(self):
		assert renderer._action_dotted_entry(
			{"event_type": "HTTP Request", "path": "/api/method/frappe.desk.form.save.savedocs"}
		) == "frappe.desk.form.save.savedocs"

	def test_http_api_method_strips_query_and_trailing_segments(self):
		assert renderer._action_dotted_entry(
			{"event_type": "HTTP Request", "path": "/api/method/foo.bar.baz?x=1&y=2"}
		) == "foo.bar.baz"
		assert renderer._action_dotted_entry(
			{"event_type": "HTTP Request", "path": "/api/method/foo.bar.baz/extra/seg"}
		) == "foo.bar.baz"

	def test_non_api_http_has_no_dotted_entry(self):
		assert renderer._action_dotted_entry(
			{"event_type": "HTTP Request", "path": "/app/sales-invoice/new"}
		) is None
		assert renderer._action_dotted_entry(
			{"event_type": "HTTP Request", "path": "/api/resource/Lead/LEAD-001"}
		) is None

	def test_empty_missing_or_bad_input(self):
		assert renderer._action_dotted_entry({"event_type": "RQ Job", "path": ""}) is None
		assert renderer._action_dotted_entry({"event_type": "RQ Job"}) is None
		assert renderer._action_dotted_entry({"event_type": "HTTP Request", "path": ""}) is None
		assert renderer._action_dotted_entry({}) is None
		assert renderer._action_dotted_entry(None) is None
		assert renderer._action_dotted_entry("not a dict") is None


class TestResolveDottedToCode:
	def test_resolves_a_module_level_function(self):
		out = renderer._resolve_dotted_to_code(_DOTTED)
		assert out is not None
		abs_path, lineno, name = out
		assert os.path.isabs(abs_path)
		assert abs_path.replace("\\", "/").endswith("optimus/renderer.py")
		assert lineno == _expected_lineno()
		assert name == "render"

	def test_none_for_no_dot_or_empty(self):
		assert renderer._resolve_dotted_to_code("render") is None
		assert renderer._resolve_dotted_to_code("") is None
		assert renderer._resolve_dotted_to_code(None) is None

	def test_none_when_path_is_a_module_not_a_function(self):
		assert renderer._resolve_dotted_to_code("optimus.renderer") is None

	def test_none_for_unimportable_prefix(self):
		assert renderer._resolve_dotted_to_code("nope_xyzq.does.not.exist") is None

	def test_none_for_missing_attribute(self):
		assert renderer._resolve_dotted_to_code("optimus.renderer.no_such_func_xyzq") is None

	def test_none_for_builtin_without_code_object(self):
		assert renderer._resolve_dotted_to_code("builtins.len") is None


class TestActionEntryCallsite:
	def _expect_renderer_py(self, cs):
		assert cs is not None
		assert cs["function"] == "render"
		assert cs["lineno"] == _expected_lineno()
		assert os.path.isabs(cs["_abs"])
		assert cs["_abs"].replace("\\", "/").endswith("optimus/renderer.py")
		# Display path is bench-relative (apps/...), not absolute, for in-bench files.
		fn = cs["filename"].replace("\\", "/")
		assert fn.endswith("optimus/renderer.py")
		assert not os.path.isabs(cs["filename"])
		# ±1-line snippet: 3 rows, the target row is "def render(".
		assert cs["source_snippet"] and len(cs["source_snippet"]) == 3
		target = [r for r in cs["source_snippet"] if r["lineno"] == cs["lineno"]]
		assert target and target[0]["content"].lstrip().startswith("def render(")

	def test_resolves_background_job_action(self):
		self._expect_renderer_py(renderer._action_entry_callsite(
			{"event_type": "RQ Job", "path": _DOTTED}
		))

	def test_resolves_http_api_method_action(self):
		self._expect_renderer_py(renderer._action_entry_callsite(
			{"event_type": "HTTP Request", "path": "/api/method/" + _DOTTED}
		))

	def test_display_path_matches_bench_relpath(self):
		cs = renderer._action_entry_callsite({"event_type": "RQ Job", "path": _DOTTED})
		try:
			from frappe.utils import get_bench_path
			expected = os.path.relpath(cs["_abs"], get_bench_path()).replace("\\", "/")
		except Exception:
			expected = cs["_abs"]
		assert cs["filename"] == expected

	def test_none_for_non_api_http_action(self):
		assert renderer._action_entry_callsite(
			{"event_type": "HTTP Request", "path": "/app/sales-invoice/new"}
		) is None

	def test_none_for_unresolvable_dotted_path(self):
		assert renderer._action_entry_callsite(
			{"event_type": "RQ Job", "path": "nope_xyzq.does.not.exist"}
		) is None

	def test_none_for_empty_path(self):
		assert renderer._action_entry_callsite(
			{"event_type": "RQ Job", "path": ""}
		) is None

	def test_none_when_dotted_resolves_to_a_module(self):
		assert renderer._action_entry_callsite(
			{"event_type": "RQ Job", "path": "optimus.renderer"}
		) is None

	def test_none_for_builtin_no_code_object(self):
		assert renderer._action_entry_callsite(
			{"event_type": "RQ Job", "path": "builtins.len"}
		) is None

	def test_forwards_file_cache(self):
		cache: dict = {}
		cs = renderer._action_entry_callsite(
			{"event_type": "RQ Job", "path": _DOTTED}, cache=cache,
		)
		assert cs is not None and cs["source_snippet"]
		# _read_source_snippet memoizes the file's split lines under the path
		# it was handed (the absolute path here).
		assert cs["_abs"] in cache and cache[cs["_abs"]]


# ---------------------------------------------------------------------------
# v0.6.x: _resolve_frame_key_to_callsite — Repeated Hot Frame's "path::func" key
# ---------------------------------------------------------------------------

class TestResolveFrameKeyToCallsite:
	def test_resolves_via_dotted_strategy(self):
		cs = renderer._resolve_frame_key_to_callsite("optimus/renderer.py::render")
		assert cs is not None
		assert cs["filename"].replace("\\", "/").endswith("optimus/renderer.py")
		assert os.path.isabs(cs["_abs"]) and cs["_abs"].endswith("renderer.py")
		assert cs["function"] == "render"
		assert cs["lineno"] == _expected_lineno()
		assert cs["source_snippet"] and len(cs["source_snippet"]) == 3
		target = [r for r in cs["source_snippet"] if r["lineno"] == cs["lineno"]]
		assert target and target[0]["content"].lstrip().startswith("def render(")

	def test_resolves_a_module_level_private_function(self):
		cs = renderer._resolve_frame_key_to_callsite("optimus/renderer.py::_read_source_snippet")
		assert cs is not None and cs["function"] == "_read_source_snippet"
		assert cs["lineno"] == inspect.unwrap(renderer._read_source_snippet).__code__.co_firstlineno

	def test_none_for_bare_name_no_module_context(self):
		assert renderer._resolve_frame_key_to_callsite("looped_validate") is None
		assert renderer._resolve_frame_key_to_callsite("render") is None

	def test_none_for_unresolvable_path(self):
		assert renderer._resolve_frame_key_to_callsite("nope_xyzq/foo.py::bar") is None

	def test_none_when_func_missing_in_resolvable_file(self):
		# dotted resolution fails (no such attr) AND grep finds no def → None.
		assert renderer._resolve_frame_key_to_callsite(
			"optimus/renderer.py::no_such_func_xyzq"
		) is None

	def test_none_for_empty_or_malformed(self):
		assert renderer._resolve_frame_key_to_callsite("") is None
		assert renderer._resolve_frame_key_to_callsite(None) is None
		assert renderer._resolve_frame_key_to_callsite("::") is None
		assert renderer._resolve_frame_key_to_callsite("a/b.py::") is None
		assert renderer._resolve_frame_key_to_callsite("::func") is None


# ---------------------------------------------------------------------------
# v0.6.x: _attach_representative_callsites — SQL red-flag findings ← recordings
# ---------------------------------------------------------------------------

# A real, readable, in-bench-but-not-frappe-app .py for the "user code" frame
# would be ideal, but walk_callsite treats any path containing "frappe/" or
# "optimus/" as framework/profiler-own. So use a stdlib module's file
# (absolute, readable, neither substring) — _resolve_source_path passes
# absolute paths straight through, so the snippet still reads.
_USER_FRAME_FILE = inspect.__file__


def _rec(calls):
	return {"uuid": "r-test", "calls": calls}


def _sql_finding(finding_type, table, nq):
	return {"finding_type": finding_type, "technical_detail": {"table": table, "normalized_query": nq}}


class TestAttachRepresentativeCallsites:
	def test_attaches_hottest_user_frame_for_missing_index(self):
		nq = "SELECT ... FROM `tabUser` WHERE x = ?"
		findings = [_sql_finding("Missing Index", "tabUser", nq)]
		recs = [_rec([
			{"query": "SELECT name FROM `tabUser` WHERE x = 1", "normalized_query": nq, "duration": 5.0,
			 "stack": [
				{"filename": "frappe/app.py", "lineno": 1, "function": "handle"},
				{"filename": _USER_FRAME_FILE, "lineno": 10, "function": "run_report"},
				{"filename": "frappe/database/database.py", "lineno": 2, "function": "sql"},
			 ]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		cs = findings[0]["technical_detail"].get("callsite")
		assert cs is not None
		assert cs["lineno"] == 10
		assert cs["function"] == "run_report"
		assert cs["_abs"] == _USER_FRAME_FILE
		assert cs["is_representative"] is True
		assert cs["source_snippet"] and any(r["lineno"] == 10 for r in cs["source_snippet"])

	def test_picks_the_hotter_callsite_on_ties(self):
		nq = "SELECT ... FROM `tabItem`"
		findings = [_sql_finding("Full Table Scan", "tabItem", nq)]
		recs = [_rec([
			{"query": "SELECT * FROM `tabItem`", "normalized_query": nq, "duration": 1.0,
			 "stack": [{"filename": _USER_FRAME_FILE, "lineno": 5, "function": "cold"}]},
			{"query": "SELECT * FROM `tabItem`", "normalized_query": nq, "duration": 99.0,
			 "stack": [{"filename": _USER_FRAME_FILE, "lineno": 20, "function": "hot"}]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		cs = findings[0]["technical_detail"]["callsite"]
		assert cs["lineno"] == 20 and cs["function"] == "hot"

	def test_unresolvable_user_path_still_sets_callsite_without_snippet(self):
		nq = "SELECT ... FROM `tabLead`"
		findings = [_sql_finding("Filesort", "tabLead", nq)]
		recs = [_rec([
			{"query": "SELECT * FROM `tabLead` ORDER BY name", "normalized_query": nq, "duration": 2.0,
			 "stack": [{"filename": "myapp/reports/leads.py", "lineno": 42, "function": "leads_report"}]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		cs = findings[0]["technical_detail"]["callsite"]
		assert cs["filename"] == "myapp/reports/leads.py"
		assert cs["lineno"] == 42 and cs["function"] == "leads_report"
		assert cs["_abs"] is None and cs["source_snippet"] is None
		assert cs["is_representative"] is True

	def test_matches_on_query_prefix_when_one_side_is_truncated(self):
		full_nq = "SELECT a, b, c, d FROM `tabUser` WHERE x = ? AND y = ?"
		findings = [_sql_finding("Missing Index", "tabUser", full_nq[:30])]  # truncated finding
		recs = [_rec([
			{"query": "SELECT a, b, c, d FROM `tabUser` WHERE x = 1 AND y = 2", "normalized_query": full_nq, "duration": 3.0,
			 "stack": [{"filename": _USER_FRAME_FILE, "lineno": 7, "function": "f"}]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		assert findings[0]["technical_detail"]["callsite"]["lineno"] == 7

	def test_requires_table_name_in_raw_query(self):
		# Same normalized query text, but the raw query targets a different
		# table → no match (defensive against accidental collisions).
		nq = "SELECT ... FROM ?"
		findings = [_sql_finding("Missing Index", "tabUser", nq)]
		recs = [_rec([
			{"query": "SELECT name FROM `tabContact`", "normalized_query": nq, "duration": 1.0,
			 "stack": [{"filename": _USER_FRAME_FILE, "lineno": 9, "function": "g"}]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		assert "callsite" not in findings[0]["technical_detail"]

	def test_non_sql_finding_untouched(self):
		findings = [
			{"finding_type": "N+1 Query", "technical_detail": {"normalized_query": "SELECT ... FROM `tabUser`"}},
		]
		recs = [_rec([{"query": "SELECT name FROM `tabUser`", "normalized_query": "SELECT ... FROM `tabUser`",
		               "duration": 1.0, "stack": [{"filename": _USER_FRAME_FILE, "lineno": 1, "function": "x"}]}])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		assert "callsite" not in findings[0]["technical_detail"]

	def test_already_has_callsite_untouched(self):
		findings = [{
			"finding_type": "Missing Index",
			"technical_detail": {"table": "tabUser", "normalized_query": "Q",
			                     "callsite": {"filename": "x.py", "lineno": 3, "function": "y"}},
		}]
		recs = [_rec([{"query": "Q on `tabUser`", "normalized_query": "Q", "duration": 1.0,
		               "stack": [{"filename": _USER_FRAME_FILE, "lineno": 99, "function": "z"}]}])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		assert findings[0]["technical_detail"]["callsite"]["lineno"] == 3  # unchanged

	def test_no_recordings_or_no_match_is_noop(self):
		findings = [_sql_finding("Missing Index", "tabUser", "Q")]
		renderer._attach_representative_callsites(findings, [], file_cache={})
		assert "callsite" not in findings[0]["technical_detail"]
		renderer._attach_representative_callsites(findings, [_rec([
			{"query": "SELECT * FROM `tabOther`", "normalized_query": "DIFFERENT", "duration": 1.0, "stack": []},
		])], file_cache={})
		assert "callsite" not in findings[0]["technical_detail"]

	def test_framework_only_stack_falls_back_to_frappe_frame(self):
		# A query issued purely from frappe core: walk_callsite falls back to
		# the innermost frame (so we never silently drop a legit framework
		# finding). A callsite IS attached, pointing at frappe — still useful.
		nq = "SELECT ... FROM `tabUser`"
		findings = [_sql_finding("Missing Index", "tabUser", nq)]
		recs = [_rec([
			{"query": "SELECT name FROM `tabUser`", "normalized_query": nq, "duration": 1.0,
			 "stack": [{"filename": "frappe/database/database.py", "lineno": 1, "function": "sql"},
			           {"filename": "frappe/model/document.py", "lineno": 2, "function": "load"}]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		cs = findings[0]["technical_detail"].get("callsite")
		assert cs is not None and cs["is_representative"] is True
		assert cs["filename"].replace("\\", "/").endswith("frappe/model/document.py")

	def test_profiler_own_stack_yields_no_callsite(self):
		# A profiler-own query (its deepest non-frappe frame is inside
		# optimus/) → walk_callsite returns None → no callsite.
		nq = "SHOW GLOBAL STATUS"
		findings = [_sql_finding("Missing Index", "tabUser", nq)]
		recs = [_rec([
			{"query": "SHOW GLOBAL STATUS on `tabUser`", "normalized_query": nq, "duration": 1.0,
			 "stack": [{"filename": "frappe/database/database.py", "lineno": 1, "function": "sql"},
			           {"filename": "optimus/infra_capture.py", "lineno": 9, "function": "snapshot"}]},
		])]
		renderer._attach_representative_callsites(findings, recs, file_cache={})
		assert "callsite" not in findings[0]["technical_detail"]


class TestSkipDecoratorsToDef:
	"""v0.7.x: on CPython 3.11+ ``code.co_firstlineno`` for a decorated
	function points at the first decorator line. The renderer advances
	to the ``def`` line so the per-action entry-callsite snippet lands
	on the signature rather than ``@frappe.whitelist(...)``."""

	def test_single_decorator_is_skipped_to_def(self, tmp_path):
		src = tmp_path / "fake_module.py"
		src.write_text(
			"line1\n"
			"line2\n"
			"@my_decorator(arg=1)\n"  # 3
			"def target_fn(x):\n"  # 4
			"    return x\n"
		)
		new_lineno = renderer._skip_decorators_to_def(
			str(src), 3, "target_fn",
		)
		assert new_lineno == 4

	def test_multi_line_decorator_args_walked_over(self, tmp_path):
		src = tmp_path / "fake_module.py"
		src.write_text(
			"line1\n"
			"@my_decorator(\n"  # 2
			"    arg1=1,\n"  # 3
			"    arg2=2,\n"  # 4
			")\n"  # 5
			"def target_fn(x):\n"  # 6
			"    return x\n"
		)
		new_lineno = renderer._skip_decorators_to_def(
			str(src), 2, "target_fn",
		)
		assert new_lineno == 6

	def test_stacked_decorators_walked_over(self, tmp_path):
		src = tmp_path / "fake_module.py"
		src.write_text(
			"@a\n"  # 1
			"@b\n"  # 2
			"@c\n"  # 3
			"def target_fn():\n"  # 4
			"    pass\n"
		)
		new_lineno = renderer._skip_decorators_to_def(
			str(src), 1, "target_fn",
		)
		assert new_lineno == 4

	def test_async_def_recognised(self, tmp_path):
		src = tmp_path / "fake_module.py"
		src.write_text(
			"@my_decorator\n"  # 1
			"async def target_fn():\n"  # 2
			"    pass\n"
		)
		new_lineno = renderer._skip_decorators_to_def(
			str(src), 1, "target_fn",
		)
		assert new_lineno == 2

	def test_non_decorated_lineno_unchanged(self, tmp_path):
		"""When the line at start_lineno doesn't start with ``@``, the
		early exit returns it unchanged — no scan, no false advance."""
		src = tmp_path / "fake_module.py"
		src.write_text(
			"line1\n"
			"def target_fn():\n"  # 2 — already the def line
			"    pass\n"
		)
		assert renderer._skip_decorators_to_def(
			str(src), 2, "target_fn",
		) == 2

	def test_no_def_found_falls_back_to_start_lineno(self, tmp_path):
		"""When the start line IS a decorator but no matching def is
		found within the scan window (mangled source, generated code),
		fall back to the original lineno."""
		src = tmp_path / "fake_module.py"
		src.write_text(
			"@my_decorator\n"  # 1
			"def different_name():\n"  # 2 — name mismatch
			"    pass\n"
		)
		assert renderer._skip_decorators_to_def(
			str(src), 1, "target_fn",
		) == 1

	def test_missing_file_falls_back(self, tmp_path):
		"""Unreadable file → returns start_lineno (no crash)."""
		assert renderer._skip_decorators_to_def(
			str(tmp_path / "does_not_exist.py"), 5, "target_fn",
		) == 5

	def test_cache_reused_across_calls(self, tmp_path):
		"""Two resolutions on the same file should share the cache."""
		src = tmp_path / "fake_module.py"
		src.write_text(
			"@a\n"
			"def fn_one():\n"
			"    pass\n"
			"@b\n"
			"def fn_two():\n"
			"    pass\n"
		)
		cache: dict = {}
		assert renderer._skip_decorators_to_def(str(src), 1, "fn_one", cache=cache) == 2
		# Same file path is now in the cache.
		assert str(src) in cache
		# Second call resolves the next function using the cached file.
		assert renderer._skip_decorators_to_def(str(src), 4, "fn_two", cache=cache) == 5

	def test_zero_or_negative_lineno_is_passthrough(self, tmp_path):
		src = tmp_path / "fake_module.py"
		src.write_text("@d\ndef target_fn():\n    pass\n")
		assert renderer._skip_decorators_to_def(str(src), 0, "target_fn") == 0
		assert renderer._skip_decorators_to_def(str(src), -1, "target_fn") == -1
