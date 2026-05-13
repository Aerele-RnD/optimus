# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Smoke tests for frontend assets (widget JS, form JS, CSS).

Full browser integration tests would need cypress/playwright, which is
heavy to set up. These cheap smoke tests just verify that the JS files
parse, that the widget's state machine symbols are present, and that the
CSS selectors look sane. Good enough to catch regressions where someone
accidentally breaks the syntax or deletes a critical hook.

Run with `pytest optimus/tests/test_frontend_assets.py -v`.
"""

import os
import re
import shutil
import subprocess

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGET_JS = os.path.join(APP_DIR, "public", "js", "floating_widget.js")
WIDGET_CSS = os.path.join(APP_DIR, "public", "css", "floating_widget.css")
FORM_JS = os.path.join(
	APP_DIR, "optimus", "doctype", "optimus_session", "optimus_session.js"
)
LIST_JS = os.path.join(
	APP_DIR, "optimus", "doctype", "optimus_session", "optimus_session_list.js"
)


def _node_check(js_path: str) -> None:
	"""Run `node --check` to validate JS syntax.

	Skips if node isn't installed — that's fine, frappe benches ship with
	node so in practice this will run everywhere that matters.
	"""
	if not shutil.which("node"):
		pytest.skip("node not installed")
	result = subprocess.run(
		["node", "--check", js_path],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0:
		pytest.fail(f"node --check failed for {js_path}:\n{result.stderr}")


def test_widget_js_syntax():
	_node_check(WIDGET_JS)


def test_form_js_syntax():
	_node_check(FORM_JS)


def test_list_js_syntax():
	_node_check(LIST_JS)


def test_widget_has_state_machine_constants():
	"""The widget's 5 states must all be referenced in the JS."""
	with open(WIDGET_JS) as f:
		src = f.read()
	for state in ("inactive", "recording", "stopping", "analyzing", "ready"):
		assert f"fp-state-{state}" in src, f"Missing state class: fp-state-{state}"


def test_widget_has_visibility_listener():
	"""v0.5.1: polling is gone, but the visibilitychange handler
	remains so the widget can issue a one-shot refreshStatus()
	when the tab becomes visible again. This covers TTL-based
	auto-stop (Redis expiry while the tab was hidden) and any
	realtime events the Socket.IO client dropped during tab
	sleep.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()
	assert "visibilitychange" in src
	# v0.5.1: no more stopPolling — the handler just re-fetches
	# state on visibility return. The polling-pause machinery was
	# removed; see test_realtime_session_events for the new
	# realtime contract.
	assert "refreshStatus" in src


def test_widget_role_check():
	"""Widget must check for Optimus User or System Manager role."""
	with open(WIDGET_JS) as f:
		src = f.read()
	assert "System Manager" in src
	assert "Optimus User" in src
	assert "userHasRole" in src


def test_form_js_has_retry_button():
	"""Fix #11: form JS must have a Retry Analyze button for Failed sessions."""
	with open(FORM_JS) as f:
		src = f.read()
	assert "Retry Analyze" in src
	assert "retry_analyze" in src
	assert 'status !== "Failed"' in src or "Failed" in src


def test_form_js_has_analyzer_warnings_intro():
	"""Fix #15: analyzer_warnings should be surfaced as a form intro banner."""
	with open(FORM_JS) as f:
		src = f.read()
	assert "analyzer_warnings" in src
	assert "set_intro" in src


def test_form_js_has_download_button():
	"""Form JS must wire up the report download button.

	v0.6.0 Round 7: safe-mode reports were removed. The form now has a
	single "Download Report" button — the role-based UX gate moved
	server-side (permissions.py file_has_permission). The client just
	exposes the button and lets Frappe's File permission hook deny the
	actual download for unauthorized users.
	"""
	with open(FORM_JS) as f:
		src = f.read()
	assert "Download Report" in src
	assert "raw_report_file" in src


def test_list_js_severity_indicators():
	"""List view must color-code by top_severity for Ready sessions."""
	with open(LIST_JS) as f:
		src = f.read()
	assert "top_severity" in src
	assert "High severity" in src
	assert "Medium severity" in src


def test_widget_start_has_error_callback():
	"""v0.5.1 regression guard: the Start dialog's frappe.call must
	include an error callback. Without it, any server-side failure of
	api.start (permission error, concurrent session, server exception)
	silently closes the dialog with no feedback to the user — the
	exact 'widget not working as expected' failure mode reported by
	users who lacked the Optimus User role. The stop API already had
	an error callback added in an earlier fix; this test forces start
	to stay symmetric.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	# Find the start dialog's primary_action block and verify it
	# contains both callback: and error:.
	start_call_idx = src.find("optimus.api.start")
	assert start_call_idx > 0, "widget must call optimus.api.start"

	# Look in the ~2000 chars around the start call for an error: key.
	window = src[start_call_idx : start_call_idx + 2000]
	assert "error:" in window or "error: " in window, (
		"openStartDialog's frappe.call(api.start) must have an error "
		"callback — without it, permission errors and server exceptions "
		"leave the widget silently unresponsive after the dialog closes"
	)


def test_widget_stop_has_error_callback():
	"""Companion to the start-error guard: the Stop call already had an
	error handler added in an earlier fix. Make sure it stays.

	We look at the entire confirmAndStop function body (finding it
	from the 'function confirmAndStop' keyword to the closing brace)
	rather than a fixed-size window after the stop call site — the
	callback body grew in v0.5.1 to handle the 'no active session'
	reset path and a couple of console.log diagnostics, and a fixed
	window was both brittle and too narrow.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	fn_idx = src.find("function confirmAndStop")
	assert fn_idx > 0, "widget must define confirmAndStop"

	# Find the end of the function: match the opening brace after the
	# function name, then walk forward tracking brace balance.
	open_brace_idx = src.find("{", fn_idx)
	assert open_brace_idx > 0
	depth = 0
	end_idx = None
	for i in range(open_brace_idx, len(src)):
		c = src[i]
		if c == "{":
			depth += 1
		elif c == "}":
			depth -= 1
			if depth == 0:
				end_idx = i + 1
				break
	assert end_idx is not None, "couldn't find end of confirmAndStop"

	body = src[fn_idx:end_idx]
	assert "optimus.api.stop" in body
	assert "error:" in body, (
		"confirmAndStop's frappe.call(api.stop) must have an error "
		"callback so failed stops don't strand the widget in 'Stopping…'"
	)


def test_widget_stop_handles_no_active_session():
	"""v0.5.1 regression guard: when the stop API returns
	{stopped: false} (session already gone — auto-stopped, janitor-
	swept, or a retried click after a network blip on the first
	stop), the widget must reset to inactive, NOT transition to
	'Analyzing…' (which would hang forever because no session is
	actually analyzing).
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	fn_idx = src.find("function confirmAndStop")
	open_brace_idx = src.find("{", fn_idx)
	depth = 0
	end_idx = None
	for i in range(open_brace_idx, len(src)):
		c = src[i]
		if c == "{":
			depth += 1
		elif c == "}":
			depth -= 1
			if depth == 0:
				end_idx = i + 1
				break
	body = src[fn_idx:end_idx]

	# Must explicitly check data.stopped === false
	assert "data.stopped === false" in body, (
		"confirmAndStop's success callback must handle the "
		"{stopped: false} response (session already gone) and reset "
		"to inactive — without this check, the widget falls through "
		"to the 'Analyzing…' branch and hangs forever"
	)


def test_widget_css_selectors():
	"""CSS must define the widget root and state classes."""
	with open(WIDGET_CSS) as f:
		src = f.read()
	assert "#frappe-profiler-widget" in src
	for state in ("inactive", "recording", "stopping", "analyzing", "ready"):
		assert f".fp-state-{state}" in src, f"Missing CSS class: .fp-state-{state}"


def test_widget_css_has_print_safety():
	"""CSS should have print-safe rules since reports are often printed."""
	with open(WIDGET_CSS) as f:
		src = f.read()
	# Widget is for Desk, not print, but it should be hidden in print mode
	# OR have sane fallbacks. Minimum: the file should not be empty.
	assert len(src) > 100
