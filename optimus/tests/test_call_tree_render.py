# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.x call-tree refinements (renderer._render_call_tree_node / _panel):
hide [other: N frames] nodes, collapse the sub-1ms <sql> tail into one
expandable summary, auto-open the hottest path down to the first user-app
frame, and the reworded intro.
"""

import json
import re

from optimus import renderer


def _node(fn, file, ms, children=None, self_ms=0, lineno=1):
	return {
		"function": fn, "filename": file, "lineno": lineno,
		"cumulative_ms": ms, "self_ms": self_ms, "children": children or [],
	}


def _tree():
	# framework spine (handle) → first user frame (looped_validate) → user
	# children + a synthetic [other] node + two sub-1ms <sql> leaves.
	return _node("handle", "frappe/handler.py", 100, [
		_node("looped_validate", "ugly_code/python/common.py", 95, [
			_node("_run_validations", "ugly_code/python/common.py", 90, []),
			{"function": "[other: 50 frames]", "filename": "", "lineno": 0,
			 "cumulative_ms": 4, "self_ms": 0, "children": []},
			_node("<sql>", "ugly_code/common.py", 0.3),
			_node("<sql>", "frappe/db.py", 0.2),
		]),
	])


def _open_state(html, fn):
	"""True/False whether the <details> for frame `fn` is rendered open;
	None if the frame isn't present."""
	m = re.search(
		r'<details class="[^"]*?"( open)?><summary><span class="frame-name">'
		+ re.escape(fn) + "<",
		html,
	)
	if not m:
		return None
	return bool(m.group(1))


def test_other_frames_node_is_dropped():
	html = renderer._render_call_tree_node(_tree(), parent_ms=100, depth=0)
	assert "[other:" not in html
	assert "50 frames" not in html


def test_more_frames_omitted_node_is_dropped():
	# The analyzer's deep-tree pruning placeholder "[N more frames omitted]" is a
	# synthetic collapse node with no callsite — drop it like [other: N frames].
	tree = _node("handle", "frappe/handler.py", 100, [
		_node("looped_validate", "ugly_code/python/common.py", 95),
		{"function": "[208 more frames omitted]", "filename": "", "lineno": 0,
		 "cumulative_ms": 14, "self_ms": 0, "children": []},
	])
	html = renderer._render_call_tree_node(tree, parent_ms=100, depth=0)
	assert "more frames omitted" not in html
	assert "208" not in html
	assert "looped_validate" in html


def test_ct_is_other_frame_matches_both_synthetic_formats():
	assert renderer._ct_is_other_frame("[other: 50 frames]")
	assert renderer._ct_is_other_frame("[1 more frames omitted]")
	assert renderer._ct_is_other_frame("[208 more frames omitted]")
	# real frames / other synthetic leaves are not matched
	assert not renderer._ct_is_other_frame("looped_validate")
	assert not renderer._ct_is_other_frame("<sql>")


def test_sql_leaves_dropped_from_tree():
	# ALL <sql> leaf siblings are dropped from the call-tree display — no
	# summary line, no rows. The call tree shows only the Python hierarchy;
	# the queries themselves live in the Slowest-queries / per-action sections.
	tree = _node("handle", "frappe/handler.py", 100, [
		_node("looped_validate", "ugly_code/common.py", 50),
		_node("<sql>", "ugly_code/common.py", 40),   # 40ms — still dropped
		_node("<sql>", "frappe/db.py", 0.3),
		_node("<sql>", "frappe/db.py", 0.2),
	])
	html = renderer._render_call_tree_node(tree, parent_ms=100, depth=0)
	assert "looped_validate" in html         # the real frame still renders
	assert "SQL quer" not in html            # no collapsed summary line
	assert "click to expand" not in html
	assert "&lt;sql&gt;" not in html         # no <sql> rows at all


def test_auto_opens_down_to_first_user_frame():
	html = renderer._render_call_tree_node(_tree(), parent_ms=100, depth=0)
	# framework root + the first user-app frame are auto-opened…
	assert _open_state(html, "handle") is True
	assert _open_state(html, "looped_validate") is True
	# …but a frame below the first user frame is collapsed.
	assert _open_state(html, "_run_validations") is False


def test_panel_intro_reworded():
	action = {
		"call_tree_json": json.dumps({"cumulative_ms": 100, "children": [_tree()]}),
		"duration_ms": 100,
		"action_label": "savedocs:Submit",
	}
	panel = renderer._render_call_tree_panel([action])
	assert "auto-open" in panel.lower()
	assert "Click any frame to expand its children" not in panel


def _act(label, dur, tree=None):
	return {
		"call_tree_json": json.dumps(
			{"cumulative_ms": dur, "children": [tree or _tree()]}
		),
		"duration_ms": dur,
		"action_label": label,
	}


def test_panel_single_action_keeps_legacy_layout():
	# One action → unchanged singular heading, label in the section-tag, and
	# no per-action header (byte-compatible with the pre-v0.13 panel).
	panel = renderer._render_call_tree_panel([_act("solo", 900)])
	assert "Call tree (top action)" in panel
	assert "Call trees (top actions)" not in panel
	assert "call-tree-action-head" not in panel
	assert "solo" in panel


def test_panel_renders_top_three_slowest_actions():
	# v0.13: the panel surfaces the top-3 slowest actions, each as its own
	# labeled sub-tree; the 4th-slowest is dropped by the cap.
	acts = [
		_act("act_a", 5000),
		_act("act_b", 4000),
		_act("act_c", 3000),
		_act("act_d", 1000),
	]
	panel = renderer._render_call_tree_panel(acts)
	assert "Call trees (top actions)" in panel
	assert "3 slowest" in panel
	assert "act_a" in panel and "act_b" in panel and "act_c" in panel
	assert "act_d" not in panel  # 4th-slowest dropped by _CALL_TREE_MAX_ACTIONS
	assert panel.count('class="call-tree-action"') == 3
	assert "#1" in panel and "#2" in panel and "#3" in panel


def test_flat_top_action_does_not_hide_deep_action():
	# The core fix: a flat #1 action (an RQ-style loop) no longer hides the
	# deep framework→user hierarchy of the #2 action.
	flat = _node("bg_loop", "ugly_code/python/common.py", 6000, [
		_node("worker", "ugly_code/python/common.py", 200),
		_node("worker", "ugly_code/python/common.py", 190),
	])
	acts = [
		{"call_tree_json": json.dumps({"cumulative_ms": 6000, "children": [flat]}),
		 "duration_ms": 6000, "action_label": "flat_top"},
		{"call_tree_json": json.dumps({"cumulative_ms": 100, "children": [_tree()]}),
		 "duration_ms": 100, "action_label": "deep_second"},
	]
	panel = renderer._render_call_tree_panel(acts)
	assert "flat_top" in panel and "deep_second" in panel
	# the deep action's user frame renders — its structure is no longer hidden
	assert "looped_validate" in panel


def test_drilldown_chain_skips_other_frames():
	# v0.7.x: the finding call-chain breadcrumb must not walk into a synthetic
	# "[other: N frames]" node (you can't drill into a collapsed bucket).
	tree = _node("looped_validate", "ugly_code/common.py", 100, [
		_node("_check_user_exists", "ugly_code/common.py", 95, [
			{"function": "[other: 450 frames]", "filename": "", "lineno": 0,
			 "cumulative_ms": 90, "self_ms": 0, "children": []},
			_node("_maybe_log_user", "ugly_code/common.py", 5),
		], lineno=20),
	], lineno=8)
	chain = renderer._walk_drilldown_chain(
		tree,
		{"filename": "ugly_code/common.py", "lineno": 8, "function": "looped_validate"},
		tracked_apps=("ugly_code",),
	)
	fns = [c["function"] for c in chain]
	assert not any("[other" in f for f in fns), f"chain leaked [other]: {fns}"
