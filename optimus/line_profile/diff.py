# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Content-hash line alignment for cross-run phase-2 diffs.

When the same function is line-profiled in 2+ runs, raw line numbers may
shift between runs (the customer edited the file). Aligning by line number
would misattribute or drop matches. Aligning by content hash of the
stripped source text matches lines by what they say, not where they sit.
"""

import hashlib


def content_hash(source_text: str) -> str:
	"""Return a sha256 hex digest of the line's stripped source text.

	Whitespace-only edits don't change the hash — leading/trailing
	whitespace is stripped before hashing, so re-indenting (or trimming
	a trailing space) keeps the content-identity stable.
	"""
	return hashlib.sha256(source_text.strip().encode("utf-8")).hexdigest()


def align_function(run_a_lines: list[dict], run_b_lines: list[dict]) -> list[dict]:
	"""Align two runs' line dicts for the same function by content hash.

	Each input line dict has at minimum: ``lineno``, ``content``, ``hits``,
	``total_ms``. Output rows have shape::

	    {
	        "status":     "matched" | "added" | "removed",
	        "lineno_old": int | None,
	        "lineno_new": int | None,
	        "content":    str,
	        "ms_old":     float | None,
	        "ms_new":     float | None,
	        "delta_ms":   float | None,  # ms_new - ms_old (only when matched)
	    }

	Order: matched + added rows in ``run_b`` lineno order, then any removed
	rows in ``run_a`` lineno order trailing.
	"""
	# Index run_a by content hash so we can look up while walking run_b.
	# Each hash maps to a list (a line could appear multiple times — a
	# repeated empty line, for example), so we pop from each list to mark
	# consumed entries.
	a_index: dict[str, list[dict]] = {}
	for line in run_a_lines:
		a_index.setdefault(content_hash(line["content"]), []).append(line)

	rows: list[dict] = []
	for b_line in sorted(run_b_lines, key=lambda l: l["lineno"]):
		h = content_hash(b_line["content"])
		bucket = a_index.get(h)
		if bucket:
			a_line = bucket.pop(0)
			rows.append({
				"status": "matched",
				"lineno_old": a_line["lineno"],
				"lineno_new": b_line["lineno"],
				"content": b_line["content"],
				"ms_old": a_line["total_ms"],
				"ms_new": b_line["total_ms"],
				"delta_ms": b_line["total_ms"] - a_line["total_ms"],
			})
		else:
			rows.append({
				"status": "added",
				"lineno_old": None,
				"lineno_new": b_line["lineno"],
				"content": b_line["content"],
				"ms_old": None,
				"ms_new": b_line["total_ms"],
				"delta_ms": None,
			})

	# Anything left in a_index is in run_a but not run_b → removed.
	leftovers: list[dict] = []
	for bucket in a_index.values():
		leftovers.extend(bucket)
	for a_line in sorted(leftovers, key=lambda l: l["lineno"]):
		rows.append({
			"status": "removed",
			"lineno_old": a_line["lineno"],
			"lineno_new": None,
			"content": a_line["content"],
			"ms_old": a_line["total_ms"],
			"ms_new": None,
			"delta_ms": None,
		})

	return rows
