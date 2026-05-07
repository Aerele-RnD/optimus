# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for frappe_profiler.line_profile.diff — content-hash line alignment
across phase-2 runs."""

from frappe_profiler.line_profile import diff


class TestContentHash:
	def test_same_text_same_hash(self):
		assert diff.content_hash("a = 1") == diff.content_hash("a = 1")

	def test_trailing_whitespace_ignored(self):
		assert diff.content_hash("a = 1") == diff.content_hash("a = 1   ")

	def test_leading_whitespace_ignored(self):
		# Indentation changes (e.g. function moved into nested block) should
		# not break content-based identity.
		assert diff.content_hash("a = 1") == diff.content_hash("    a = 1")

	def test_different_text_different_hash(self):
		assert diff.content_hash("a = 1") != diff.content_hash("a = 2")

	def test_returns_hex_string(self):
		h = diff.content_hash("a = 1")
		assert isinstance(h, str)
		# sha256 hex digest is 64 chars
		assert len(h) == 64
		assert all(c in "0123456789abcdef" for c in h)


def _line(lineno: int, content: str, total_ms: float, hits: int = 1) -> dict:
	"""Helper for building line dicts in tests."""
	return {
		"lineno": lineno,
		"content": content,
		"hits": hits,
		"total_ms": total_ms,
	}


class TestAlignFunction:
	def test_identical_runs_all_matched(self):
		a = [_line(1, "x = 1", 5.0), _line(2, "y = 2", 10.0)]
		b = [_line(1, "x = 1", 4.0), _line(2, "y = 2", 8.0)]

		rows = diff.align_function(a, b)

		assert len(rows) == 2
		assert all(r["status"] == "matched" for r in rows)
		assert rows[0]["delta_ms"] == -1.0  # 4 - 5
		assert rows[1]["delta_ms"] == -2.0  # 8 - 10

	def test_whitespace_only_edit_matches(self):
		a = [_line(1, "x = 1", 5.0)]
		b = [_line(1, "    x = 1   ", 4.0)]  # reindented + trailing

		rows = diff.align_function(a, b)

		assert len(rows) == 1
		assert rows[0]["status"] == "matched"
		assert rows[0]["delta_ms"] == -1.0

	def test_new_line_in_run_b_is_added(self):
		a = [_line(1, "x = 1", 5.0)]
		b = [_line(1, "x = 1", 4.0), _line(2, "z = 99", 12.0)]

		rows = diff.align_function(a, b)

		statuses = [(r["status"], r.get("content")) for r in rows]
		assert ("matched", "x = 1") in statuses
		assert ("added", "z = 99") in statuses

	def test_removed_line_in_run_a_flagged(self):
		a = [_line(1, "x = 1", 5.0), _line(2, "obsolete = True", 7.0)]
		b = [_line(1, "x = 1", 4.0)]

		rows = diff.align_function(a, b)

		statuses = [(r["status"], r.get("content")) for r in rows]
		assert ("matched", "x = 1") in statuses
		assert ("removed", "obsolete = True") in statuses

	def test_modified_line_appears_as_removed_plus_added(self):
		# Same lineno but different content → old removed, new added.
		a = [_line(1, "x = 1", 5.0)]
		b = [_line(1, "x = 2", 4.0)]

		rows = diff.align_function(a, b)

		statuses = sorted(r["status"] for r in rows)
		assert statuses == ["added", "removed"]

	def test_empty_inputs_empty_output(self):
		assert diff.align_function([], []) == []

	def test_matched_row_has_full_shape(self):
		a = [_line(10, "compute()", 50.0, hits=3)]
		b = [_line(12, "compute()", 30.0, hits=3)]  # same content, moved down

		rows = diff.align_function(a, b)

		assert len(rows) == 1
		row = rows[0]
		assert row["status"] == "matched"
		assert row["lineno_old"] == 10
		assert row["lineno_new"] == 12
		assert row["content"] == "compute()"
		assert row["ms_old"] == 50.0
		assert row["ms_new"] == 30.0
		assert row["delta_ms"] == -20.0

	def test_added_row_has_no_old_lineno(self):
		a = []
		b = [_line(1, "new_line()", 7.0)]

		rows = diff.align_function(a, b)

		assert len(rows) == 1
		assert rows[0]["status"] == "added"
		assert rows[0]["lineno_old"] is None
		assert rows[0]["lineno_new"] == 1
		assert rows[0]["ms_old"] is None
		assert rows[0]["ms_new"] == 7.0

	def test_removed_row_has_no_new_lineno(self):
		a = [_line(5, "gone()", 9.0)]
		b = []

		rows = diff.align_function(a, b)

		assert len(rows) == 1
		assert rows[0]["status"] == "removed"
		assert rows[0]["lineno_old"] == 5
		assert rows[0]["lineno_new"] is None
		assert rows[0]["ms_old"] == 9.0
		assert rows[0]["ms_new"] is None
