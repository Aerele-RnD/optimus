# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Fluent helpers for constructing test recordings without huge JSON files.

Before these helpers existed, each new analyzer test needed a hand-
written 100+ line JSON fixture copied from an existing one. These
builders reduce that to ~10 lines of Python:

    recording = build_recording(
        calls=[
            build_call(
                query="SELECT name FROM `tabItem` WHERE item_code = ?",
                duration=5.0,
                stack=[("erpnext/module.py", 200, "loop")],
            )
        ] * 15
    )

Keep the hand-written JSON fixtures in tests/fixtures/ for integration-
style tests — they exercise the full recording shape. Use these builders
for narrow unit tests that care only about one aspect (e.g. "does the
N+1 analyzer handle this stack?").
"""

from typing import Any


def build_call(
	*,
	query: str = "SELECT 1",
	normalized_query: str | None = None,
	duration: float = 1.0,
	stack: list[tuple[str, int, str]] | list[dict] | None = None,
	explain_result: list[dict] | None = None,
	exact_copies: int = 1,
	normalized_copies: int = 1,
	index: int = 0,
) -> dict:
	"""Build a single SQL call dict matching the recorder's shape.

	`stack` can be a list of (filename, lineno, function) tuples for
	brevity, or pre-built dicts if you need extra fields.
	"""
	if normalized_query is None:
		normalized_query = query

	frames: list[dict] = []
	for frame in stack or []:
		if isinstance(frame, dict):
			frames.append(frame)
		else:
			filename, lineno, function = frame
			frames.append(
				{"filename": filename, "lineno": lineno, "function": function}
			)

	return {
		"query": query,
		"normalized_query": normalized_query,
		"duration": duration,
		"stack": frames,
		"explain_result": explain_result or [],
		"exact_copies": exact_copies,
		"normalized_copies": normalized_copies,
		"index": index,
	}


def build_recording(
	*,
	uuid: str = "test-uuid",
	path: str = "/test",
	method: str = "GET",
	cmd: str | None = None,
	event_type: str = "HTTP Request",
	duration: float = 100.0,
	calls: list[dict] | None = None,
	form_dict: dict | None = None,
	headers: dict | None = None,
) -> dict:
	"""Build a recording dict matching what frappe.recorder produces."""
	calls = calls or []
	return {
		"uuid": uuid,
		"path": path,
		"method": method,
		"cmd": cmd,
		"event_type": event_type,
		"time": "2026-04-09 10:00:00",
		"duration": duration,
		"queries": len(calls),
		"time_queries": sum(c.get("duration", 0) for c in calls),
		"headers": headers or {},
		"form_dict": form_dict or {},
		"calls": calls,
	}


def build_explain_row(
	*,
	table: str = "tabTest",
	type: str = "ref",
	key: str | None = None,
	rows: int = 10,
	filtered: float | None = None,
	extra: str = "",
	**kwargs: Any,
) -> dict:
	"""Build an EXPLAIN row dict matching MariaDB's output shape."""
	row: dict[str, Any] = {
		"table": table,
		"type": type,
		"rows": rows,
		"Extra": extra,
	}
	if key is not None:
		row["key"] = key
	if filtered is not None:
		row["filtered"] = filtered
	row.update(kwargs)
	return row
