# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.5.3 regenerate_reports API.

The endpoint re-invokes renderer.render_raw against an existing
Optimus Session WITHOUT re-running the analyzer pipeline.
Use cases:

  - Report template was upgraded; existing sessions should reflect
    the new layout without paying the analyze cost again.
  - The original render crashed (bug in renderer) and a fix was
    deployed. regenerate_reports re-runs the render on the fixed
    code.
  - An admin wants to re-render after manually editing an analyzer-
    derived field (rare, but possible).

Contract checked here via source-inspection + behavior under
stubbed frappe (the endpoint itself needs a live bench for
integration tests, which the harness doesn't provide).
"""

import os
import re

_API_PATH = os.path.join(
	os.path.dirname(__file__), "..", "api.py"
)


def _read_api_source() -> str:
	with open(_API_PATH) as f:
		return f.read()


def _regenerate_reports_body() -> str:
	"""Return just the function body of regenerate_reports."""
	src = _read_api_source()
	# Grab from `def regenerate_reports(` to the next top-level def.
	start = src.index("def regenerate_reports(")
	# Find the next unindented `def ` after start + 1 line.
	search_from = src.find("\n", start) + 1
	next_def = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (next_def.start() if next_def else len(src) - search_from)
	return src[start:end]


class TestSurface:
	def test_regenerate_reports_is_whitelisted(self):
		"""The endpoint must carry @frappe.whitelist() — otherwise
		the front-end can't call it."""
		src = _read_api_source()
		# Allow optional intermediate decorators (e.g. @rate_limit) between
		# @frappe.whitelist() and the def line.
		match = re.search(
			r"@frappe\.whitelist\(\)\s*\n(?:@\w+(?:\.\w+)*\([^)]*\)\s*\n)*def regenerate_reports",
			src,
		)
		assert match is not None, (
			"regenerate_reports must be decorated with "
			"@frappe.whitelist() so the Optimus Session JS can call it"
		)

	def test_signature_takes_session_uuid(self):
		src = _read_api_source()
		assert "def regenerate_reports(session_uuid:" in src, (
			"regenerate_reports must take session_uuid as its primary arg"
		)

	def test_does_not_re_enqueue_analyze(self):
		"""Guardrail: regenerate_reports must NOT call
		_enqueue_analyze. If it does, it's just a slower
		retry_analyze — the whole point is to skip the analyzer."""
		body = _regenerate_reports_body()
		assert "_enqueue_analyze" not in body, (
			"regenerate_reports must NOT enqueue analyze — that would "
			"defeat the purpose of having a separate endpoint"
		)
		# Positive check: it does call _render_and_attach_reports.
		assert "_render_and_attach_reports" in body, (
			"regenerate_reports must invoke _render_and_attach_reports "
			"directly"
		)

	def test_clears_cached_pdf(self):
		"""After regenerating the safe HTML, the cached PDF is
		stale — must be invalidated so the next download_pdf call
		regenerates from fresh HTML."""
		body = _regenerate_reports_body()
		assert "clear_cached_pdf" in body, (
			"regenerate_reports must clear the cached PDF — otherwise "
			"the download_pdf endpoint would serve a stale PDF rendered "
			"from the old HTML"
		)

	def test_permission_gate_matches_retry_analyze(self):
		"""Must gate on recording user OR System Manager, same as
		retry_analyze. A user shouldn't be able to regenerate
		someone else's session report."""
		body = _regenerate_reports_body()
		assert '"System Manager"' in body or "'System Manager'" in body
		assert "PermissionError" in body

	def test_best_effort_recording_fetch(self):
		"""When recordings have expired from Redis, the endpoint
		must still succeed with empty recordings — the important
		stuff is stored in the DocType."""
		body = _regenerate_reports_body()
		# try/except around the fetch — falls back to [].
		assert "try:" in body and "recordings = []" in body, (
			"regenerate_reports must degrade to empty recordings on "
			"fetch failure — the safe renderer still produces a useful "
			"report without them"
		)

	def test_status_gate_rejects_non_terminal_sessions(self):
		"""v0.12.9: regenerate_reports must refuse Recording / Stopping
		/ Analyzing sessions. Pre-v0.12.9 the code had no status check
		despite the docstring claiming "Allowed on Ready OR Failed
		sessions" — a re-render on an in-flight session would attach
		an incomplete report to a still-running analyze that the
		pipeline would then overwrite. The gate enforces the
		docstring's contract."""
		body = _regenerate_reports_body()
		# Match the actual code, allowing any quote style and
		# whitespace inside the membership tuple.
		assert re.search(
			r'row\[["\']status["\']\]\s+not in\s+\(\s*["\']Ready["\']\s*,\s*["\']Failed["\']\s*\)',
			body,
		), (
			"regenerate_reports must gate on row['status'] not in "
			"('Ready', 'Failed') — pre-v0.12.9 the endpoint accepted "
			"any status and re-rendered mid-analyze sessions"
		)
		# The error message must mention 'retry_analyze' as the
		# operator's recourse for a stuck pipeline.
		assert "retry_analyze" in body, (
			"the status-rejection error message must point operators at "
			"retry_analyze as the recourse for stuck pipelines"
		)


class TestButtonWired:
	"""The server endpoint is useless without a UI trigger. This test
	confirms the Optimus Session form JS actually wires the button
	to the endpoint."""

	def test_button_calls_regenerate_reports_endpoint(self):
		import os
		js_path = os.path.join(
			os.path.dirname(__file__),
			"..", "optimus", "doctype", "optimus_session",
			"optimus_session.js",
		)
		with open(js_path) as f:
			js = f.read()

		# Button added in refresh.
		assert "render_regenerate_report_button" in js, (
			"refresh() must call render_regenerate_report_button"
		)
		# That function calls the right endpoint.
		assert "optimus.api.regenerate_reports" in js, (
			"Button must invoke optimus.api.regenerate_reports"
		)
		# User-facing label.
		assert "Regenerate Reports" in js

	def test_button_visible_on_ready_and_failed(self):
		"""Regeneration is useful for Ready sessions (apply new
		template) AND Failed sessions (recover from a render crash
		that got fixed). Guard check in the button helper must
		include both."""
		import os
		js_path = os.path.join(
			os.path.dirname(__file__),
			"..", "optimus", "doctype", "optimus_session",
			"optimus_session.js",
		)
		with open(js_path) as f:
			js = f.read()

		# Extract the helper function body.
		m = re.search(
			r"function render_regenerate_report_button\(frm\)\s*\{(.*?)\n\}",
			js,
			re.DOTALL,
		)
		assert m is not None, "render_regenerate_report_button not found"
		body = m.group(1)
		# Guard mentions BOTH states.
		assert '"Ready"' in body
		assert '"Failed"' in body
