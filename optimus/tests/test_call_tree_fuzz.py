# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Property-based fuzz tests for the reconciliation algorithm.

Uses Hypothesis to generate random pyi tree + SQL stack pairs and assert
algorithmic invariants. If Hypothesis is not installed, the whole module
is skipped.
"""

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st

from optimus.analyzers import call_tree


def _frame_strategy():
	return st.fixed_dictionaries({
		"function": st.text(min_size=1, max_size=20).filter(lambda s: "<" not in s),
		"filename": st.sampled_from([
			"apps/frappe/x.py",
			"apps/erpnext/y.py",
			"apps/my_app/z.py",
		]),
		"lineno": st.integers(min_value=1, max_value=1000),
	})


@st.composite
def _node_strategy(draw, max_depth=4):
	if max_depth == 0:
		children = []
	else:
		n = draw(st.integers(min_value=0, max_value=3))
		children = [draw(_node_strategy(max_depth=max_depth - 1)) for _ in range(n)]
	frame = draw(_frame_strategy())
	cumulative = draw(st.floats(min_value=0.1, max_value=1000.0))
	return {
		"function": frame["function"],
		"filename": frame["filename"],
		"lineno": frame["lineno"],
		"self_ms": draw(st.floats(min_value=0.0, max_value=cumulative)),
		"cumulative_ms": cumulative,
		"kind": "python",
		"children": children,
	}


@given(
	tree=_node_strategy(),
	calls=st.lists(
		st.fixed_dictionaries({
			"query": st.text(min_size=1, max_size=20),
			"duration": st.floats(min_value=0.0, max_value=100.0),
			"stack": st.lists(_frame_strategy(), max_size=5),
		}),
		max_size=10,
	),
)
@settings(max_examples=200, deadline=None)
def test_reconcile_invariants(tree, calls):
	result = call_tree.reconcile(tree, calls, action_wall_time_ms=1000)

	# Invariant 1: tree depth is finite (no infinite recursion)
	def depth(node, d=0):
		if not node.get("children"):
			return d
		return max(depth(c, d + 1) for c in node["children"])

	assert depth(result) < 100

	# Invariant 2: every node has the required keys
	def assert_keys(node):
		for key in ("function", "self_ms", "cumulative_ms", "children"):
			assert key in node
		for child in node["children"]:
			assert_keys(child)

	assert_keys(result)

	# Invariant 3: every SQL call lands as exactly one leaf, even after
	# coalescing (coalescing only merges identical query_normalized, and
	# Hypothesis is unlikely to generate identical queries — but if it
	# does, the coalesced count should still equal the original input).
	def count_sql_query_count(node):
		c = node.get("query_count", 0) if node.get("kind") == "sql" else 0
		for child in node.get("children", []):
			c += count_sql_query_count(child)
		return c

	assert count_sql_query_count(result) == len(calls)
