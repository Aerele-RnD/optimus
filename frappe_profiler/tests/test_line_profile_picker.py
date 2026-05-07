# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for frappe_profiler.line_profile.picker — candidate generation and
free-form dotted-path resolution for the phase-2 line-profile picker UI.

pyinstrument captures the *bare* function name in ``function`` and the
absolute path in ``filename``; we derive the importable dotted path from
the two via ``_build_dotted_path``. Tests use realistic Frappe-layout
paths (``apps/<app>/<app>/...``) so the module-derivation logic exercises
the real-world shape.
"""

import pytest

from frappe_profiler.line_profile import picker


def _frame(function: str, filename: str, lineno: int, cumulative_ms: float, children=None):
	"""Helper: build a pyinstrument-shaped frame node. ``function`` is the
	bare name as pyinstrument would emit (e.g. ``validate``), not a dotted
	path."""
	return {
		"function": function,
		"filename": filename,
		"lineno": lineno,
		"cumulative_ms": cumulative_ms,
		"self_ms": cumulative_ms,
		"kind": "python",
		"children": children or [],
	}


def _root(*children):
	return {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"cumulative_ms": sum(c.get("cumulative_ms", 0) for c in children),
		"self_ms": 0,
		"kind": "python",
		"children": list(children),
	}


class TestDeriveModulePath:
	def test_frappe_layout_collapses_app_double(self):
		# apps/<app>/<app>/... is the bench convention; the leading "apps"
		# and the duplicate package directory both get stripped.
		path = picker._derive_module_path("apps/erpnext/erpnext/selling/doctype/sales_invoice/sales_invoice.py")
		assert path == "erpnext.selling.doctype.sales_invoice.sales_invoice"

	def test_app_name_differs_from_dir_keeps_both(self):
		# Some apps use a different package name than their bench dir.
		# We only collapse when they match.
		path = picker._derive_module_path("apps/my_app/my_pkg/utils.py")
		assert path == "my_app.my_pkg.utils"

	def test_init_dot_py_drops_to_package(self):
		path = picker._derive_module_path("apps/erpnext/erpnext/utils/__init__.py")
		assert path == "erpnext.utils"

	def test_no_apps_prefix_uses_path_as_is(self):
		# Stdlib / venv paths don't have the apps/ wrapper.
		path = picker._derive_module_path("/usr/lib/python3.14/json/__init__.py")
		# No "apps" segment → walks the path as-is, dropping leading slash
		assert path.endswith("json")

	def test_empty_returns_empty(self):
		assert picker._derive_module_path("") == ""


class TestDeriveApp:
	def test_extracts_app_from_apps_prefix(self):
		assert picker._derive_app("apps/erpnext/erpnext/foo.py") == "erpnext"

	def test_no_apps_prefix_falls_back_to_first_segment(self):
		assert picker._derive_app("frappe/database.py") == "frappe"

	def test_empty_returns_empty(self):
		assert picker._derive_app("") == ""


class TestBuildCandidatesFromTrees:
	def test_single_frame_yields_dotted_path_from_filename(self):
		tree = _root(_frame("heavy_job", "apps/my_app/my_app/tasks.py", 42, 250.0))

		candidates = picker._build_candidates_from_trees([tree], [])

		assert len(candidates) == 1
		c = candidates[0]
		assert c["dotted_path"] == "my_app.tasks.heavy_job"
		assert c["qualname"] == "heavy_job"
		assert c["file"] == "apps/my_app/my_app/tasks.py"
		assert c["lineno"] == 42
		assert c["cumulative_ms"] == 250.0
		assert c["hit_count"] == 1
		assert c["app"] == "my_app"

	def test_same_function_same_file_aggregates(self):
		tree_a = _root(_frame("hash_value", "apps/my_app/my_app/utils.py", 5, 100.0))
		tree_b = _root(_frame("hash_value", "apps/my_app/my_app/utils.py", 5, 60.0))

		candidates = picker._build_candidates_from_trees([tree_a, tree_b], [])

		assert len(candidates) == 1
		assert candidates[0]["cumulative_ms"] == 160.0
		assert candidates[0]["hit_count"] == 2

	def test_same_function_name_different_files_does_not_collapse(self):
		# Two unrelated `validate` methods in different modules must remain
		# separate candidates — that's the whole point of including the
		# filename in the dedup key.
		tree = _root(
			_frame("validate", "apps/erpnext/erpnext/selling/sales_invoice.py", 10, 100.0),
			_frame("validate", "apps/my_app/my_app/lead.py", 5, 50.0),
		)

		candidates = picker._build_candidates_from_trees([tree], [])

		assert len(candidates) == 2
		paths = sorted(c["dotted_path"] for c in candidates)
		assert paths == [
			"erpnext.selling.sales_invoice.validate",
			"my_app.lead.validate",
		]

	def test_synthetic_frames_excluded(self):
		tree = _root(
			_frame("<sql>", "", 0, 50.0),
			_frame("[finalize]", "", 0, 5.0),
			_frame("real_fn", "apps/my_app/my_app/real.py", 1, 200.0),
		)

		candidates = picker._build_candidates_from_trees([tree], [])

		paths = [c["dotted_path"] for c in candidates]
		assert paths == ["my_app.real.real_fn"]

	def test_sorted_by_cumulative_ms_desc(self):
		tree = _root(
			_frame("slow_one", "apps/my_app/my_app/a.py", 1, 100.0),
			_frame("slow_two", "apps/my_app/my_app/b.py", 1, 250.0),
			_frame("slow_three", "apps/my_app/my_app/c.py", 1, 50.0),
		)

		candidates = picker._build_candidates_from_trees([tree], [])

		ms_values = [c["cumulative_ms"] for c in candidates]
		assert ms_values == sorted(ms_values, reverse=True)

	def test_walks_into_children(self):
		nested = _frame("outer", "apps/my_app/my_app/o.py", 1, 100.0, children=[
			_frame("inner", "apps/my_app/my_app/o.py", 50, 90.0),
		])
		tree = _root(nested)

		candidates = picker._build_candidates_from_trees([tree], [])

		paths = {c["dotted_path"] for c in candidates}
		assert paths == {"my_app.o.outer", "my_app.o.inner"}

	def test_caps_at_top_n(self):
		children = [
			_frame(f"fn_{i:02d}", "apps/my_app/my_app/f.py", i, float(i))
			for i in range(50)
		]
		# Each fn_NN is in the same file but with different names → distinct keys
		tree = _root(*children)

		candidates = picker._build_candidates_from_trees([tree], [])

		assert len(candidates) == 30


class TestPureHelperFiltering:
	"""Plumbing / wrapper / dispatch frames that every request passes
	through (frappe.app.application, frappe.handler.handle, recorder,
	typing_validations wrapper, document.save's decorator chain, etc.)
	dominate the leaderboard but aren't actionable to line-profile.

	Reuses the same filter the call_tree analyzer applies to Repeated
	Hot Frame so the picker surfaces the same shape of "real logic" the
	user can actually optimize.
	"""

	def test_frappe_app_application_dropped(self):
		tree = _root(_frame("application", "apps/frappe/frappe/app.py", 1, 400.0))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_frappe_handler_handle_dropped(self):
		tree = _root(_frame("handle", "apps/frappe/frappe/handler.py", 1, 800.0))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_frappe_recorder_record_sql_dropped(self):
		tree = _root(_frame("record_sql", "apps/frappe/frappe/recorder.py", 1, 140.0))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_frappe_utils_typing_validations_wrapper_dropped(self):
		tree = _root(_frame(
			"wrapper", "apps/frappe/frappe/utils/typing_validations.py", 1, 750.0,
		))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_frappe_model_document_save_dropped(self):
		tree = _root(_frame("save", "apps/frappe/frappe/model/document.py", 1, 380.0))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_frappe_model_meta_init_dropped(self):
		tree = _root(_frame("__init__", "apps/frappe/frappe/model/meta.py", 1, 116.0))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_frappe_qb_query_execute_dropped(self):
		tree = _root(_frame("execute", "apps/frappe/frappe/model/qb_query.py", 1, 137.0))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert candidates == []

	def test_bare_wrapper_names_dropped_anywhere(self):
		# Decorator wrapper functions named "wrapper" / "fn" / "runner" /
		# "composer" are always plumbing regardless of file.
		tree = _root(
			_frame("fn", "apps/my_app/my_app/x.py", 1, 100.0),
			_frame("runner", "apps/my_app/my_app/x.py", 1, 100.0),
			_frame("composer", "apps/my_app/my_app/x.py", 1, 100.0),
			_frame("wrapper", "apps/my_app/my_app/x.py", 1, 100.0),
		)
		assert picker._build_candidates_from_trees([tree], []) == []

	def test_real_business_logic_kept(self):
		# This is what users WANT in the picker.
		tree = _root(_frame(
			"validate",
			"apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
			142,
			292.0,
		))
		candidates = picker._build_candidates_from_trees([tree], [])
		assert len(candidates) == 1
		assert candidates[0]["dotted_path"].endswith("sales_invoice.validate")


class TestFrameworkSplit:
	def test_user_app_marked_primary(self):
		# Use a non-wrapper-named function so the pure-helper filter
		# doesn't drop it.
		tree = _root(_frame("compute_total", "apps/my_app/my_app/x.py", 1, 100.0))
		c = picker._build_candidates_from_trees([tree], [])[0]
		assert c["is_framework"] is False
		assert c["app"] == "my_app"

	def test_erpnext_marked_framework(self):
		tree = _root(_frame("make_gl_entries", "apps/erpnext/erpnext/accounts/gl_entry.py", 1, 100.0))
		c = picker._build_candidates_from_trees([tree], [])[0]
		assert c["is_framework"] is True

	def test_frappe_marked_framework(self):
		# frappe/client.py isn't in the pure-helper file list, so this
		# survives filtering.
		tree = _root(_frame("get_value", "apps/frappe/frappe/client.py", 1, 100.0))
		c = picker._build_candidates_from_trees([tree], [])[0]
		assert c["is_framework"] is True


class TestResolveFreeform:
	def test_resolves_stdlib_function(self):
		result = picker.resolve_freeform("json.dumps")

		assert result["eligible"] is True
		assert result["app"] == "json"

	def test_missing_module_raises(self):
		with pytest.raises(picker.PickerError) as exc:
			picker.resolve_freeform("totally_nonexistent_pkg_xyz.foo")
		assert "could not import" in str(exc.value).lower()

	def test_missing_attribute_raises(self):
		with pytest.raises(picker.PickerError) as exc:
			picker.resolve_freeform("json.does_not_exist")
		assert "attribute" in str(exc.value).lower()

	def test_builtin_c_extension_rejected(self):
		result = picker.resolve_freeform("builtins.len")
		assert result["eligible"] is False
		assert (
			"c-extension" in result["ineligible_reason"].lower()
			or "built" in result["ineligible_reason"].lower()
		)

	def test_lambda_rejected(self):
		import sys
		mod_name = "_lp_test_lambda_module"
		mod = type(sys)("dummy")
		mod.my_lambda = lambda x: x * 2
		sys.modules[mod_name] = mod
		try:
			result = picker.resolve_freeform(f"{mod_name}.my_lambda")
			assert result["eligible"] is False
			assert "lambda" in result["ineligible_reason"].lower()
		finally:
			del sys.modules[mod_name]

	def test_empty_path_raises(self):
		with pytest.raises(picker.PickerError):
			picker.resolve_freeform("")

	def test_top_level_module_only_raises(self):
		with pytest.raises(picker.PickerError):
			picker.resolve_freeform("json")


class TestResolveFreeformClassMethodFallback:
	"""When the bare function name only exists on a class inside the
	module (e.g. ``validate`` is on ``SalesInvoice``), ``resolve_freeform``
	should auto-substitute the class qualifier so curated picks resolve
	without forcing the user to type ``Module.Class.method``."""

	def setup_method(self):
		import sys
		import types as _types

		self.mod_name = "_lp_class_method_test_mod"
		self.mod = _types.ModuleType(self.mod_name)

		# A class with a method `do_work`. The class is "owned" by the
		# module via __module__ so the resolver picks it up.
		class Worker:
			def do_work(self):
				return 1

		Worker.__module__ = self.mod_name
		self.mod.Worker = Worker
		sys.modules[self.mod_name] = self.mod

	def teardown_method(self):
		import sys
		sys.modules.pop(self.mod_name, None)

	def test_single_class_owner_substitutes_qualifier(self):
		# Curated picker emits "{module}.{method}" for class methods; the
		# resolver should find Worker and rewrite to "{module}.Worker.do_work".
		result = picker.resolve_freeform(f"{self.mod_name}.do_work")

		assert result["eligible"] is True
		assert result["dotted_path"] == f"{self.mod_name}.Worker.do_work"
		assert result["qualname"] == "Worker.do_work"

	def test_multiple_class_owners_raises_with_options(self):
		# Add a second class with the same method name → ambiguous.
		class OtherWorker:
			def do_work(self):
				return 2

		OtherWorker.__module__ = self.mod_name
		self.mod.OtherWorker = OtherWorker

		with pytest.raises(picker.PickerError) as exc:
			picker.resolve_freeform(f"{self.mod_name}.do_work")
		msg = str(exc.value)
		assert "Worker.do_work" in msg
		assert "OtherWorker.do_work" in msg
