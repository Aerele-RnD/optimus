# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the reconciliation algorithm in analyzers/call_tree.py.

These use plain dict-shaped pyi tree fixtures (not real pyinstrument
Session objects) so the tests are pure-function and don't depend on
pyinstrument being installed.
"""

import json
import os

from optimus.analyzers import call_tree

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_pyi_fixture(name):
	with open(os.path.join(FIXTURES, f"{name}.json")) as f:
		return json.load(f)


def _make_node(function, filename, cumulative_ms=10, children=None):
	"""Helper to build a tree node for tests."""
	return {
		"function": function,
		"filename": filename,
		"lineno": 1,
		"self_ms": 0,
		"cumulative_ms": cumulative_ms,
		"kind": "python",
		"children": children or [],
	}


def test_pyi_to_dict_tree_passes_through_dict_input():
	"""When given a dict already in our shape, _pyi_to_dict_tree returns it."""
	fixture = load_pyi_fixture("pyi_session_simple")
	result = call_tree._pyi_to_dict_tree(fixture)
	assert result["function"] == "<root>"
	assert len(result["children"]) == 1
	assert result["children"][0]["function"].endswith("sales_invoice.validate")


def test_make_sql_leaf_basic_shape():
	call = {
		"query": "SELECT * FROM tabSales Invoice WHERE name = ?",
		"normalized_query": "SELECT * FROM `tabSales Invoice` WHERE `name` = ?",
		"duration": 47.5,
		"explain_result": [{"type": "ALL", "Extra": "Using filesort"}],
		"stack": [],
	}
	leaf = call_tree._make_sql_leaf(call, partial_match=False)
	assert leaf["kind"] == "sql"
	assert leaf["self_ms"] == 47.5
	assert leaf["cumulative_ms"] == 47.5
	assert leaf["query_count"] == 1
	assert leaf["partial_match"] is False
	assert leaf["children"] == []
	assert "explain_flags" in leaf
	assert leaf["explain_flags"].get("full_scan") is True
	assert leaf["explain_flags"].get("filesort") is True


def test_make_sql_leaf_carries_partial_match_flag():
	call = {"query": "SELECT 1", "duration": 1.0, "stack": []}
	leaf = call_tree._make_sql_leaf(call, partial_match=True)
	assert leaf["partial_match"] is True


def test_find_graft_point_exact_match_descends_to_deepest():
	tree = _make_node("<root>", "", 100, [
		_make_node("erpnext.selling.validate", "apps/erpnext/x.py", 80, [
			_make_node("my_app.discounts.calc", "apps/my_app/d.py", 60, []),
		]),
	])
	stack = [
		{"function": "erpnext.selling.validate", "filename": "apps/erpnext/x.py", "lineno": 1},
		{"function": "my_app.discounts.calc", "filename": "apps/my_app/d.py", "lineno": 1},
	]
	graft, partial = call_tree._find_graft_point(tree, stack)
	assert graft["function"] == "my_app.discounts.calc"
	assert partial is False


def test_find_graft_point_partial_match_returns_deepest_visible():
	"""When pyi tree is missing a frame the recorder saw, graft at the deepest match."""
	tree = _make_node("<root>", "", 100, [
		_make_node("erpnext.selling.validate", "apps/erpnext/x.py", 80, []),
	])
	stack = [
		{"function": "erpnext.selling.validate", "filename": "apps/erpnext/x.py", "lineno": 1},
		{"function": "my_app.discounts.calc", "filename": "apps/my_app/d.py", "lineno": 1},
	]
	graft, partial = call_tree._find_graft_point(tree, stack)
	assert graft["function"] == "erpnext.selling.validate"
	assert partial is True


def test_find_graft_point_skips_framework_frames_for_graft_point():
	"""Even when pyi tree has frappe.db.sql, graft at the user-code parent."""
	tree = _make_node("<root>", "", 100, [
		_make_node("my_app.do_thing", "apps/my_app/d.py", 80, [
			_make_node("frappe.db.sql", "apps/frappe/database.py", 40, []),
		]),
	])
	stack = [
		{"function": "my_app.do_thing", "filename": "apps/my_app/d.py", "lineno": 1},
		{"function": "frappe.db.sql", "filename": "apps/frappe/database.py", "lineno": 1},
	]
	graft, partial = call_tree._find_graft_point(tree, stack)
	# Should land on my_app.do_thing, NOT frappe.db.sql
	assert graft["function"] == "my_app.do_thing"


def test_coalesce_sql_siblings_merges_identical_queries():
	parent = _make_node("user_code", "u.py", 100, [
		{
			"function": "<sql>", "filename": "u.py:5", "kind": "sql",
			"query_normalized": "SELECT * FROM `tabItem` WHERE `name` = ?",
			"self_ms": 5.0, "cumulative_ms": 5.0, "query_count": 1,
			"partial_match": False, "children": [],
		},
		{
			"function": "<sql>", "filename": "u.py:5", "kind": "sql",
			"query_normalized": "SELECT * FROM `tabItem` WHERE `name` = ?",
			"self_ms": 4.0, "cumulative_ms": 4.0, "query_count": 1,
			"partial_match": False, "children": [],
		},
	])
	tree = _make_node("<root>", "", 100, [parent])
	call_tree._coalesce_sql_siblings(tree)
	# Two identical SQL siblings collapsed into one
	user_code = tree["children"][0]
	sql_children = [c for c in user_code["children"] if c.get("kind") == "sql"]
	assert len(sql_children) == 1
	merged = sql_children[0]
	assert merged["query_count"] == 2
	assert merged["self_ms"] == 9.0
	assert merged.get("is_n_plus_one_hint") is True


def test_coalesce_sql_siblings_does_not_merge_different_queries():
	parent = _make_node("user_code", "u.py", 100, [
		{
			"function": "<sql>", "filename": "u.py:5", "kind": "sql",
			"query_normalized": "SELECT 1", "self_ms": 1.0, "cumulative_ms": 1.0,
			"query_count": 1, "partial_match": False, "children": [],
		},
		{
			"function": "<sql>", "filename": "u.py:5", "kind": "sql",
			"query_normalized": "SELECT 2", "self_ms": 1.0, "cumulative_ms": 1.0,
			"query_count": 1, "partial_match": False, "children": [],
		},
	])
	tree = _make_node("<root>", "", 100, [parent])
	call_tree._coalesce_sql_siblings(tree)
	user_code = tree["children"][0]
	sql_children = [c for c in user_code["children"] if c.get("kind") == "sql"]
	assert len(sql_children) == 2


def test_reconcile_grafts_sql_under_user_code():
	tree_dict = _make_node("<root>", "", 100, [
		_make_node("my_app.calculate_total", "apps/my_app/calc.py", 60, []),
	])
	calls = [
		{
			"query": "SELECT * FROM tabItem",
			"normalized_query": "SELECT * FROM `tabItem`",
			"duration": 12.5,
			"stack": [
				{"function": "my_app.calculate_total", "filename": "apps/my_app/calc.py", "lineno": 5},
			],
		},
	]
	result = call_tree.reconcile(tree_dict, calls, action_wall_time_ms=100)
	# The SQL leaf is grafted under my_app.calculate_total
	user_code = result["children"][0]
	sql_leaves = [c for c in user_code["children"] if c.get("kind") == "sql"]
	assert len(sql_leaves) == 1
	assert sql_leaves[0]["self_ms"] == 12.5


def test_reconcile_with_no_calls_returns_tree_unchanged():
	tree_dict = _make_node("<root>", "", 100, [])
	result = call_tree.reconcile(tree_dict, [], action_wall_time_ms=100)
	assert result == call_tree._normalize_dict_tree(tree_dict)


def test_reconcile_invariant_no_python_node_exceeds_parent():
	"""Property: cumulative_ms of any python node ≤ cumulative_ms of its parent.

	SQL leaves are excluded from the invariant — their grafted self_ms is
	informational and may push a parent's *summed* children above the parent.
	The renderer never sums children — it always uses the parent's own
	cumulative_ms.
	"""
	tree_dict = _make_node("<root>", "", 100, [
		_make_node("user", "u.py", 80, [
			_make_node("inner", "u.py", 40, []),
		]),
	])
	calls = [
		{"query": "SELECT 1", "duration": 5,
		 "stack": [{"function": "inner", "filename": "u.py", "lineno": 1}]},
	]
	result = call_tree.reconcile(tree_dict, calls, action_wall_time_ms=100)

	def assert_invariant(node, parent_ms):
		if node.get("kind") != "sql":
			assert node["cumulative_ms"] <= parent_ms + 0.01
		for child in node.get("children", []):
			assert_invariant(child, node["cumulative_ms"])

	for child in result.get("children", []):
		assert_invariant(child, result["cumulative_ms"] or 1e9)
