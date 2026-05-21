# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""D.M-S2 — hot-frames aggregation uses self_ms, not cumulative_ms.

A recursive (or decorator-wrapped) function appears at multiple call-tree
depths. Each node's ``cumulative_ms`` includes its descendants — summing
cumulative across sibling occurrences double-counts nested self-times.

The fix switched the aggregator to ``self_ms`` (exclusive per-frame), so
the cross-action leaderboard's ``total_ms`` is the true time spent in
the function's own body.

This test guards against the aggregator regressing to cumulative_ms.
"""

from optimus.analyzers import call_tree


def _node(function, filename, cumulative, self_ms, children):
	return {
		"function": function,
		"filename": filename,
		"lineno": 1,
		"kind": "python",
		"cumulative_ms": cumulative,
		"self_ms": self_ms,
		"children": children,
	}


def test_recursive_frame_uses_self_ms_not_cumulative():
	"""A recursive function appearing at 3 call-tree depths must
	contribute its self_ms once per occurrence — not its cumulative
	(which would sum nested-self-times multiple times)."""
	# A function `rec` recursing 3 levels deep.
	#   depth 0: cumulative=100, self=40 (inclusive of children's 60ms)
	#   depth 1: cumulative=60,  self=30 (inclusive of depth-2's 30ms)
	#   depth 2: cumulative=30,  self=30 (leaf, no children)
	tree = _node(
		"my_app.work.rec",
		"apps/my_app/work.py",
		cumulative=100,
		self_ms=40,
		children=[
			_node(
				"my_app.work.rec",
				"apps/my_app/work.py",
				cumulative=60,
				self_ms=30,
				children=[
					_node(
						"my_app.work.rec",
						"apps/my_app/work.py",
						cumulative=30,
						self_ms=30,
						children=[],
					),
				],
			),
		],
	)
	_, leaderboard = call_tree._aggregate_hot_frames([tree])
	rec_row = next(
		r for r in leaderboard if r["function"].endswith("rec")
	)
	# Correct sum (self_ms): 40 + 30 + 30 = 100.
	# Buggy sum (cumulative_ms): 100 + 60 + 30 = 190.
	assert rec_row["total_ms"] == 100, (
		"aggregator must sum self_ms, not cumulative_ms — got "
		f"{rec_row['total_ms']} (cumulative-bug would be 190)"
	)
	# v0.7.x: the framework variant uses total_cumulative_ms.
	# It IS the sum-cumulative — 100 + 60 + 30 = 190 — and that's
	# intentional. The framework table accepts the overlap risk in
	# exchange for non-zero, meaningful display values.
	assert rec_row["total_cumulative_ms"] == 190
	assert rec_row["occurrences"] == 3


def test_flat_function_aggregates_self_ms_across_actions():
	"""A non-recursive function appearing once in each of 3 actions
	contributes its self_ms per occurrence; the leaderboard's
	total_ms is the sum across actions."""
	def _t(self_ms):
		return _node(
			"my_app.work.do",
			"apps/my_app/work.py",
			cumulative=self_ms + 5,  # +5ms of nested non-rec work
			self_ms=self_ms,
			children=[
				_node(
					"my_app.helpers.format",
					"apps/my_app/helpers.py",
					cumulative=5,
					self_ms=5,
					children=[],
				),
			],
		)

	per_action_trees = [_t(100), _t(150), _t(200)]
	_, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	row = next(r for r in leaderboard if r["function"].endswith("do"))
	# 100 + 150 + 200 = 450 self_ms total. Cumulative-bug would give
	# (105 + 155 + 205) = 465.
	assert row["total_ms"] == 450
	# v0.7.x: cumulative-sum lands separately for the framework
	# variant display — 105 + 155 + 205 = 465.
	assert row["total_cumulative_ms"] == 465
	assert row["distinct_actions"] == 3


# --------------------------------------------------------------------------
# build_hot_frames_table — per-variant time metric (v0.7.x)
# --------------------------------------------------------------------------

from optimus import renderer  # noqa: E402 — co-located with aggregator tests


def test_framework_variant_displays_cumulative_time():
	"""``build_hot_frames_table(is_hot=False)`` — the framework
	variant — displays ``total_cumulative_ms`` because wrapper
	self-time is sub-sampler-interval and aggregated rows would
	render as 0ms otherwise."""
	raw = [{
		"function": "frappe/model/document.py::run_method",
		"total_ms": 0,               # self-sum: all calls < 1ms
		"total_cumulative_ms": 1200, # cumulative includes user-code children
		"occurrences": 4,
		"distinct_actions": 2,
		"action_refs": [0, 1],
	}]
	out = renderer.build_hot_frames_table(raw, is_hot=False)
	assert out, "expected at least one row"
	assert out[0]["total_ms"] == 1200, (
		"framework variant must display cumulative time, not the "
		f"sub-sampler-interval self-time zero; got {out[0]['total_ms']}"
	)
	assert out[0]["is_hot"] is False


def test_user_app_variant_keeps_self_time_total():
	"""``build_hot_frames_table(is_hot=True)`` — the user-app
	variant — continues to display ``total_ms`` (self-sum) unchanged.
	The A.AE1 correctness fix (immune to recursion double-count)
	is preserved."""
	raw = [{
		"function": "my_app/work.py::do",
		"total_ms": 450,             # self-sum (the A.AE1 invariant)
		"total_cumulative_ms": 800,  # cumulative ignored on this variant
		"occurrences": 3,
		"distinct_actions": 3,
		"action_refs": [0, 1, 2],
	}]
	out = renderer.build_hot_frames_table(raw, is_hot=True)
	assert out[0]["total_ms"] == 450
	assert out[0]["is_hot"] is True


def test_framework_variant_re_sorts_by_displayed_metric():
	"""The aggregator's outer sort is by self_ms; framework rows all
	tie at 0. The framework-variant builder must re-sort by the
	displayed cumulative metric so the table reads top-down by
	actual impact."""
	raw = [
		{
			"function": "frappe/a.py::a",
			"total_ms": 0,
			"total_cumulative_ms": 200,
			"occurrences": 2, "distinct_actions": 1, "action_refs": [0],
		},
		{
			"function": "frappe/b.py::b",
			"total_ms": 0,
			"total_cumulative_ms": 1500,
			"occurrences": 4, "distinct_actions": 2, "action_refs": [0, 1],
		},
		{
			"function": "frappe/c.py::c",
			"total_ms": 0,
			"total_cumulative_ms": 800,
			"occurrences": 3, "distinct_actions": 2, "action_refs": [0, 1],
		},
	]
	out = renderer.build_hot_frames_table(raw, is_hot=False)
	displayed = [r["total_ms"] for r in out]
	assert displayed == [1500, 800, 200], (
		"framework variant must sort by cumulative DESC — got "
		f"{displayed}"
	)


def test_hot_frame_display_name_has_no_placeholder_suffix():
	# v0.7.x: the hot-frame key already encodes "<short_path>::<func>"; the
	# display name must not carry the bogus "(?:0)" (placeholder file + lineno 0)
	# that redact_frame_name used to append.
	from optimus import renderer
	out = renderer.build_hot_frames_table(
		[{"function": "python/common.py::_compute_aggregates",
		  "total_ms": 100, "occurrences": 3, "distinct_actions": 3}],
		is_hot=True,
	)
	assert out[0]["display_name"] == "python/common.py::_compute_aggregates"
	assert "(?:0)" not in out[0]["display_name"]
