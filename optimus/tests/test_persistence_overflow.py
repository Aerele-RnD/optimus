# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for call_tree_json overflow handling in analyze._apply_overflow_or_pass."""

import json

import pytest

from optimus import analyze


def _big_tree(approx_kb):
	"""Build a JSON-serialized tree of approximately the given KB size."""
	leaves = []
	per_leaf = 200  # approximate bytes
	for i in range(int(approx_kb * 1024 / per_leaf)):
		leaves.append({
			"function": f"app.module.func_{i:05d}_with_some_padding_to_increase_size",
			"filename": f"apps/some_app/some/path/module_{i}.py",
			"lineno": i,
			"self_ms": float(i),
			"cumulative_ms": float(i),
			"kind": "python",
			"children": [],
		})
	return {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0,
		"cumulative_ms": 1000,
		"kind": "python",
		"children": leaves,
	}


def test_apply_overflow_below_threshold_returns_unchanged():
	tree_json = json.dumps({"function": "<root>", "children": [], "cumulative_ms": 0})
	result, overflow_url = analyze._apply_overflow_or_pass(
		tree_json, action_idx=0, docname="PS-001",
		write_file=lambda *a, **kw: "/files/x.json",
	)
	assert result == tree_json
	assert overflow_url is None


def test_apply_overflow_above_threshold_writes_file_and_returns_marker():
	big = _big_tree(approx_kb=300)
	tree_json = json.dumps(big, default=str)
	assert len(tree_json) > 200_000

	def fake_write(filename, content, **kw):
		return f"/files/{filename}"

	result, overflow_url = analyze._apply_overflow_or_pass(
		tree_json, action_idx=0, docname="PS-001", write_file=fake_write,
	)
	parsed = json.loads(result)
	assert parsed.get("_overflow") is True
	assert parsed.get("url") == overflow_url
	assert overflow_url is not None


def test_apply_overflow_falls_back_to_truncation_on_file_failure():
	big = _big_tree(approx_kb=300)
	tree_json = json.dumps(big, default=str)

	def failing_write(*a, **kw):
		raise RuntimeError("disk full")

	warnings = []
	result, overflow_url = analyze._apply_overflow_or_pass(
		tree_json, action_idx=2, docname="PS-001",
		write_file=failing_write, warnings_sink=warnings,
	)
	# Falls back to truncated tree, no overflow URL
	assert overflow_url is None
	parsed = json.loads(result)
	assert parsed.get("_truncated") is True
	# Warning recorded
	assert any("truncated" in w.lower() for w in warnings)


def test_apply_overflow_hard_sanity_guard_at_16mb():
	"""A 17MB JSON gets hard-truncated even before overflow attempt."""
	big_str = "x" * (17 * 1024 * 1024)
	tree_json = json.dumps({
		"_data": big_str,
		"function": "<root>",
		"children": [],
		"cumulative_ms": 0,
	})

	def never_called(*a, **kw):
		pytest.fail("file write should not be attempted past the hard guard")

	warnings = []
	result, overflow_url = analyze._apply_overflow_or_pass(
		tree_json, action_idx=0, docname="PS-001",
		write_file=never_called,
		warnings_sink=warnings,
		hard_max_bytes=16_000_000,
	)
	# Hard-truncated, no file
	parsed = json.loads(result)
	assert parsed.get("_truncated") is True
	assert any("16" in w or "hard guard" in w.lower() for w in warnings)


def test_hard_truncate_tree_keeps_top_n_frames():
	# Build a tree with many frames at varying cumulative_ms
	tree = {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0,
		"cumulative_ms": 1000,
		"kind": "python",
		"children": [
			{
				"function": f"f{i}",
				"filename": "u.py",
				"lineno": i,
				"self_ms": 0,
				"cumulative_ms": float(i),
				"kind": "python",
				"children": [],
			}
			for i in range(150)
		],
	}
	tree_json = json.dumps(tree)
	truncated_str = analyze._hard_truncate_tree(tree_json)
	truncated = json.loads(truncated_str)

	assert truncated["_truncated"] is True
	# Should keep ~100 frames (CALL_TREE_HARD_TRUNCATE_KEEP_FRAMES) plus the root
	assert len(truncated["children"]) <= 101
	# The kept ones are the ones with HIGHEST cumulative_ms
	functions = [c["function"] for c in truncated["children"]]
	# f149 is the highest cumulative; should be kept
	assert "f149" in functions
	# f0 is the lowest; should be dropped
	assert "f0" not in functions
