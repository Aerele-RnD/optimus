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


def test_all_sql_leaves_collapsed_into_summary():
	# ALL <sql> leaf siblings collapse into one expandable line — including
	# ones above 1ms (the call tree is the Python hierarchy, not a query list).
	tree = _node("handle", "frappe/handler.py", 100, [
		_node("looped_validate", "ugly_code/common.py", 50),
		_node("<sql>", "ugly_code/common.py", 40),   # 40ms — collapsed too
		_node("<sql>", "frappe/db.py", 0.3),
		_node("<sql>", "frappe/db.py", 0.2),
	])
	html = renderer._render_call_tree_node(tree, parent_ms=100, depth=0)
	assert "3 SQL queries" in html
	assert "click to expand" in html
	assert "looped_validate" in html  # the real frame is NOT collapsed
	# all three still present (one click away), not deleted
	assert html.count("&lt;sql&gt;") >= 3


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
