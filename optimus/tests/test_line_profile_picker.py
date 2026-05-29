# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for optimus.line_profile.picker — candidate generation and
free-form dotted-path resolution for the phase-2 line-profile picker UI.

pyinstrument captures the *bare* function name in ``function`` and the
absolute path in ``filename``; we derive the importable dotted path from
the two via ``_build_dotted_path``. Tests use realistic Frappe-layout
paths (``apps/<app>/<app>/...``) so the module-derivation logic exercises
the real-world shape.
"""

import pytest

from optimus.line_profile import picker


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

	def test_doubled_app_prefix_fallback(self, monkeypatch):
		# Apps importable only via the doubled app name (e.g.
		# ajanta_bottles.ajanta_bottles.custom...): the picker derives the
		# collapsed single-prefix form, so resolve_freeform must retry with the
		# app name doubled before giving up.
		seen = []
		good = {"dotted_path": "ajanta_bottles.ajanta_bottles.custom.x.validate",
		        "eligible": True}

		def fake_exact(path):
			seen.append(path)
			if path == "ajanta_bottles.ajanta_bottles.custom.x.validate":
				return good
			raise picker.PickerError(f"attribute 'custom' not found while resolving '{path}'")

		monkeypatch.setattr(picker, "_resolve_freeform_exact", fake_exact)
		result = picker.resolve_freeform("ajanta_bottles.custom.x.validate")
		assert result is good
		assert seen == [
			"ajanta_bottles.custom.x.validate",                  # tried as-is
			"ajanta_bottles.ajanta_bottles.custom.x.validate",   # then doubled
		]

	def test_reraises_single_prefix_error_when_doubled_also_fails(self, monkeypatch):
		def fake_exact(path):
			raise picker.PickerError(f"attribute 'custom' not found while resolving '{path}'")

		monkeypatch.setattr(picker, "_resolve_freeform_exact", fake_exact)
		with pytest.raises(picker.PickerError) as exc:
			picker.resolve_freeform("ajanta_bottles.custom.x.validate")
		# The user sees the error for the path THEY gave, not the doubled retry.
		assert "resolving 'ajanta_bottles.custom.x.validate'" in str(exc.value)


class TestExpandHotChain:
	"""Walks down phase-1's call tree from the picked function, following
	the hottest user-code child at each level. Stops at pure-helper /
	ORM / wrapper boundary, depth cap, or min_ms floor.
	"""

	def test_picked_not_in_tree_returns_empty(self):
		tree = _root(_frame("other", "apps/my_app/my_app/x.py", 1, 100.0))

		chain = picker.expand_hot_chain([tree], "my_app.tasks.heavy_job")

		assert chain == []

	def test_picked_with_no_children_returns_self_only(self):
		tree = _root(_frame(
			"compute_total", "apps/my_app/my_app/x.py", 10, 100.0,
		))

		chain = picker.expand_hot_chain([tree], "my_app.x.compute_total")

		assert len(chain) == 1
		assert chain[0]["dotted_path"] == "my_app.x.compute_total"
		assert chain[0]["depth"] == 0
		assert chain[0]["cumulative_ms"] == 100.0

	def test_descends_through_user_code_chain(self):
		# Simulates the smoke-test scenario:
		#   validate (sales_invoice) → set_missing_values → _get_party_details
		gp = _frame(
			"_get_party_details", "apps/erpnext/erpnext/accounts/party.py", 50, 125.0,
		)
		smv = _frame(
			"set_missing_values",
			"apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
			88,
			188.0,
			children=[gp],
		)
		validate = _frame(
			"validate",
			"apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
			142,
			292.0,
			children=[smv],
		)
		tree = _root(validate)

		chain = picker.expand_hot_chain(
			[tree],
			"erpnext.accounts.doctype.sales_invoice.sales_invoice.validate",
		)

		paths = [c["dotted_path"] for c in chain]
		assert paths == [
			"erpnext.accounts.doctype.sales_invoice.sales_invoice.validate",
			"erpnext.accounts.doctype.sales_invoice.sales_invoice.set_missing_values",
			"erpnext.accounts.party._get_party_details",
		]
		assert [c["depth"] for c in chain] == [0, 1, 2]

	def test_stops_at_pure_helper_boundary(self):
		# Hot chain ends exactly when descent would cross into framework
		# plumbing (frappe.recorder, frappe.db, document.py, etc.).
		sql = _frame(
			"record_sql", "apps/frappe/frappe/recorder.py", 1, 140.0,
		)
		gp = _frame(
			"_get_party_details", "apps/erpnext/erpnext/accounts/party.py", 50, 125.0,
			children=[sql],
		)
		validate = _frame(
			"validate",
			"apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
			142,
			292.0,
			children=[gp],
		)
		tree = _root(validate)

		chain = picker.expand_hot_chain(
			[tree],
			"erpnext.accounts.doctype.sales_invoice.sales_invoice.validate",
		)

		paths = [c["dotted_path"] for c in chain]
		# record_sql is a pure helper → not included; descent stops at gp
		assert paths[-1].endswith("_get_party_details")
		assert all("record_sql" not in p for p in paths)

	def test_stops_at_min_ms_floor(self):
		# Child below 50ms threshold not included.
		fast = _frame(
			"trivial_helper", "apps/my_app/my_app/x.py", 5, 10.0,
		)
		validate = _frame(
			"compute_total", "apps/my_app/my_app/x.py", 1, 200.0,
			children=[fast],
		)
		tree = _root(validate)

		chain = picker.expand_hot_chain(
			[tree], "my_app.x.compute_total", min_ms=50,
		)

		assert len(chain) == 1
		assert chain[0]["dotted_path"] == "my_app.x.compute_total"

	def test_stops_at_max_depth(self):
		# Build a 5-deep linear chain; cap at 2 levels of descent.
		leaf = _frame("level5", "apps/my_app/my_app/x.py", 5, 100.0)
		l4 = _frame("level4", "apps/my_app/my_app/x.py", 4, 110.0, children=[leaf])
		l3 = _frame("level3", "apps/my_app/my_app/x.py", 3, 120.0, children=[l4])
		l2 = _frame("level2", "apps/my_app/my_app/x.py", 2, 130.0, children=[l3])
		l1 = _frame("level1", "apps/my_app/my_app/x.py", 1, 140.0, children=[l2])
		tree = _root(l1)

		chain = picker.expand_hot_chain(
			[tree], "my_app.x.level1", max_depth=2,
		)

		# 0 (the pick) + 2 descendants = 3 entries
		assert len(chain) == 3
		assert [c["depth"] for c in chain] == [0, 1, 2]

	def test_max_depth_zero_walks_to_leaf(self):
		"""v0.13.x: ``max_depth = 0`` means unbounded — walk to the leaf
		instead of the legacy "0 = no descent" sentinel that the pre-
		v0.13.x ``while depth < max_depth`` loop accidentally
		implemented. The Strict Sensitivity Profile uses 0 here so
		auto-expand follows the entire hot chain in one shot."""
		# Same 5-deep linear chain as the cap test above.
		leaf = _frame("level5", "apps/my_app/my_app/x.py", 5, 100.0)
		l4 = _frame("level4", "apps/my_app/my_app/x.py", 4, 110.0, children=[leaf])
		l3 = _frame("level3", "apps/my_app/my_app/x.py", 3, 120.0, children=[l4])
		l2 = _frame("level2", "apps/my_app/my_app/x.py", 2, 130.0, children=[l3])
		l1 = _frame("level1", "apps/my_app/my_app/x.py", 1, 140.0, children=[l2])
		tree = _root(l1)

		chain = picker.expand_hot_chain(
			[tree], "my_app.x.level1", max_depth=0,
		)

		# 0 (pick) + 4 descendants — walked all the way to ``level5``.
		assert len(chain) == 5
		assert [c["depth"] for c in chain] == [0, 1, 2, 3, 4]

	def test_min_ms_zero_includes_every_measurable_child(self):
		"""v0.13.x: ``min_ms = 0`` means no minimum — every measurable
		child is eligible. Closes the gap where pre-v0.13.x the
		``or 50.0`` fallback in api.py silently re-applied the default
		floor and skipped any child < 50ms even when the operator
		wanted the deepest possible coverage."""
		# Two-step chain where the second hop is below the legacy 50ms
		# floor but still measurable. Under ``min_ms = 0`` it MUST be
		# included.
		tiny = _frame("tiny_helper", "apps/my_app/my_app/x.py", 5, 5.0)
		root_fn = _frame(
			"compute", "apps/my_app/my_app/x.py", 1, 100.0,
			children=[tiny],
		)
		tree = _root(root_fn)

		chain = picker.expand_hot_chain(
			[tree], "my_app.x.compute", min_ms=0,
		)

		paths = [c["dotted_path"] for c in chain]
		assert paths == ["my_app.x.compute", "my_app.x.tiny_helper"]

	def test_picks_hottest_child_among_siblings(self):
		# Two siblings; chain follows the slower one (we want the bigger
		# time sink). Both above the min_ms floor.
		hot = _frame("slow_branch", "apps/my_app/my_app/x.py", 10, 200.0)
		warm = _frame("warm_branch", "apps/my_app/my_app/x.py", 20, 80.0)
		root_fn = _frame(
			"compute", "apps/my_app/my_app/x.py", 1, 300.0,
			children=[warm, hot],
		)
		tree = _root(root_fn)

		chain = picker.expand_hot_chain([tree], "my_app.x.compute")

		paths = [c["dotted_path"] for c in chain]
		assert paths == ["my_app.x.compute", "my_app.x.slow_branch"]

	def test_skips_synthetic_frames_in_descent(self):
		# <sql> / [finalize] in children must be ignored when picking the
		# hottest user-code child.
		synthetic = _frame("<sql>", "", 0, 500.0)  # bigger but synthetic
		real_child = _frame(
			"compute_helper", "apps/my_app/my_app/x.py", 5, 100.0,
		)
		root_fn = _frame(
			"compute", "apps/my_app/my_app/x.py", 1, 600.0,
			children=[synthetic, real_child],
		)
		tree = _root(root_fn)

		chain = picker.expand_hot_chain([tree], "my_app.x.compute")

		paths = [c["dotted_path"] for c in chain]
		assert paths == ["my_app.x.compute", "my_app.x.compute_helper"]

	def test_finds_hottest_match_across_trees(self):
		# Same function appears in two action trees with different
		# cumulative_ms — picker should pick the hotter instance and
		# walk its children.
		hot_child = _frame(
			"hot_descendant", "apps/my_app/my_app/x.py", 5, 100.0,
		)
		t1 = _root(_frame(
			"compute", "apps/my_app/my_app/x.py", 1, 50.0,  # cold instance
		))
		t2 = _root(_frame(
			"compute", "apps/my_app/my_app/x.py", 1, 300.0,  # hot instance
			children=[hot_child],
		))

		chain = picker.expand_hot_chain([t1, t2], "my_app.x.compute")

		paths = [c["dotted_path"] for c in chain]
		assert paths == ["my_app.x.compute", "my_app.x.hot_descendant"]

	def test_each_chain_entry_has_required_fields(self):
		child = _frame(
			"helper", "apps/my_app/my_app/x.py", 10, 100.0,
		)
		root_fn = _frame(
			"compute", "apps/my_app/my_app/x.py", 1, 200.0,
			children=[child],
		)
		tree = _root(root_fn)

		chain = picker.expand_hot_chain([tree], "my_app.x.compute")

		for entry in chain:
			assert "dotted_path" in entry
			assert "qualname" in entry
			assert "file" in entry
			assert "lineno" in entry
			assert "cumulative_ms" in entry
			assert "depth" in entry


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


class TestRecommendedFlag:
	"""v0.7.x (P2): candidates carry a ``recommended`` flag so the picker can
	pre-tick the real hot paths (non-framework user code above a time
	threshold) — one click to line-profile them, no manual hunting."""

	def test_user_hot_frame_recommended(self):
		tree = _root(
			_frame("bg_recheck_users", "ugly_code/python/common.py", 199, 500.0, children=[
				_frame("compute_totals", "erpnext/controllers/taxes.py", 50, 300.0),
			]),
			_frame("tiny_helper", "ugly_code/python/common.py", 300, 10.0),
		)
		candidates = picker._build_tree_indented_candidates([tree])
		by_fn = {c["qualname"]: c for c in candidates}
		# Every candidate exposes the flag.
		assert all("recommended" in c for c in candidates)
		# Hot user-app frame → recommended.
		assert by_fn["bg_recheck_users"]["recommended"] is True
		# Cold user-app frame (below the ms threshold) → not recommended.
		assert by_fn["tiny_helper"]["recommended"] is False
		# Framework frames are never recommended.
		for c in candidates:
			if c["is_framework"]:
				assert c["recommended"] is False
