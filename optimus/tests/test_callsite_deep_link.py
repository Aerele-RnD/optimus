# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.2 round 4 clickable callsite → editor deep-link.

A finding's absolute-path callsite renders as a clickable anchor using
the ``vscode://file`` URL scheme — VS Code, VS Code Insiders, and
Cursor all register this handler on install, so the link jumps the
developer straight to the file:line in their editor with one click.

Bench-relative (non-absolute) paths render as plain text since the
``vscode://file`` URL scheme requires an absolute filesystem path.
"""

import json
import types


def _fake_session_doc(callsite_filename="/abs/path/apps/myapp/foo.py",
                      callsite_lineno=42):
	doc = types.SimpleNamespace()
	doc.title = "Test"
	doc.session_uuid = "t"
	doc.user = "a"
	doc.status = "Ready"
	doc.started_at = "2026-04-14"
	doc.stopped_at = "2026-04-14"
	doc.notes = None
	doc.top_severity = "High"
	doc.total_duration_ms = 2000
	doc.total_query_time_ms = 0
	doc.total_queries = 50
	doc.total_requests = 5
	doc.summary_html = None
	doc.top_queries_json = "[]"
	doc.table_breakdown_json = "[]"
	doc.hot_frames_json = "[]"
	doc.session_time_breakdown_json = "{}"
	doc.total_python_ms = 0
	doc.total_sql_ms = 0
	doc.analyzer_warnings = None
	doc.v5_aggregate_json = "{}"
	doc.actions = []

	row = types.SimpleNamespace()
	row.finding_type = "N+1 Query"
	row.severity = "High"
	row.title = "Same query ran 50× at foo.py:42"
	row.customer_description = "desc"
	row.estimated_impact_ms = 500.0
	row.affected_count = 50
	row.action_ref = "0"
	row.technical_detail_json = json.dumps({
		"callsite": {
			"filename": callsite_filename,
			"lineno": callsite_lineno,
			"function": "bulk_process",
		},
	})
	doc.findings = [row]
	return doc


def test_raw_mode_wraps_callsite_in_vscode_link():
	"""Raw mode + absolute path → clickable ``vscode://file`` anchor.

	URL shape: ``vscode://file{absolute_path}:{lineno}`` — two slashes
	after ``vscode:``, the authority ``file``, then the absolute path
	(which itself starts with ``/``). Matches VS Code's documented
	URL-handler scheme; Cursor + VS Code Insiders honor the same
	scheme.
	"""
	from optimus import renderer

	doc = _fake_session_doc(callsite_filename="/abs/path/apps/myapp/foo.py",
	                        callsite_lineno=42)
	html = renderer.render(doc, recordings=[])

	# The vscode:// href is present with the absolute path + line.
	assert 'href="vscode://file/abs/path/apps/myapp/foo.py:42"' in html, (
		"Raw mode must emit a vscode://file{path}:{line} deep-link"
	)
	# Class marker for the link (used for CSS + future JS hooks).
	assert 'class="callsite-link"' in html
	# Link wraps a <code> block — the actual visible callsite text.
	assert "apps/myapp/foo.py:42" in html


def test_bench_relative_path_does_not_emit_link():
	"""Bench-relative (non-absolute) callsites — e.g. 'frappe/handler.py'
	from pyinstrument's short form — can't be made into a working
	vscode:// URL without an abs path. Render as plain code instead
	of emitting a broken link."""
	from optimus import renderer

	doc = _fake_session_doc(callsite_filename="frappe/handler.py",
	                        callsite_lineno=10)
	html = renderer.render(doc, recordings=[])

	assert 'vscode://file/frappe/handler.py' not in html, (
		"Non-absolute path must NOT be linked as vscode:// — the URL "
		"scheme requires an absolute filesystem path"
	)
	assert "frappe/handler.py:10" in html  # still shown, just as plain code


def test_link_points_to_correct_file_and_line():
	"""Sanity: the URL lineno matches the callsite lineno exactly."""
	from optimus import renderer

	doc = _fake_session_doc(callsite_filename="/home/frappe/bench/apps/myapp/x.py",
	                        callsite_lineno=777)
	html = renderer.render(doc, recordings=[])
	assert 'href="vscode://file/home/frappe/bench/apps/myapp/x.py:777"' in html
