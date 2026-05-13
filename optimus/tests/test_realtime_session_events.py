# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Source-inspection guards for the v0.5.1 realtime session-event
contract.

v0.5.1 replaces the widget's per-5-second HTTP polling of
``/api/method/optimus.api.status`` with Socket.IO push
events. The contract is:

  Server publishes:
    - optimus_session_stopping    (from api._stop_session)
    - optimus_session_analyzing   (from analyze.run, at the top)
    - optimus_session_ready       (from analyze.run, at success)
    - optimus_session_failed      (from analyze.run, on exception)
    - optimus_progress            (existing — multiple points in analyze.run)

  Client subscribes to all five and also calls status() once at
  page-load + on visibility-change (no setInterval).

Breaking any part of this silently reverts the widget to polling-
timeout lag or leaves state transitions invisible to other tabs.
These source-inspection tests catch regressions at CI time.
"""

import inspect
import os


def test_stop_session_publishes_stopping_event():
	"""api._stop_session must emit optimus_session_stopping so open
	widgets on other tabs transition out of Recording without a poll."""
	from optimus import api

	src = inspect.getsource(api._stop_session)
	assert "optimus_session_stopping" in src, (
		"api._stop_session must publish optimus_session_stopping. "
		"Without this, a user with multiple Desk tabs only sees the "
		"state change on the tab that clicked Stop; the others wait "
		"for their next polling cycle (which no longer exists)."
	)


def test_analyze_run_publishes_analyzing_event():
	"""analyze.run must emit optimus_session_analyzing right after
	it flips status to Analyzing, so the widget leaves its local
	'stopping' placeholder and picks up real progress events."""
	from optimus import analyze

	src = inspect.getsource(analyze.run)
	assert "optimus_session_analyzing" in src, (
		"analyze.run must publish optimus_session_analyzing at the "
		"start of the analyze phase"
	)


def test_analyze_run_publishes_failed_event():
	"""analyze.run's exception handler must emit
	optimus_session_failed. Without this the widget hangs on
	'Analyzing…' forever when a background analyze crashes."""
	from optimus import analyze

	src = inspect.getsource(analyze.run)
	assert "optimus_session_failed" in src, (
		"analyze.run's exception handler must publish "
		"optimus_session_failed so the widget transitions out of "
		"'Analyzing…' when an analyze crashes"
	)


def test_analyze_run_publishes_ready_event():
	"""Backward-compat guard: the existing optimus_session_ready
	emission must remain — this is how the widget navigates the user
	to the report on success."""
	from optimus import analyze

	src = inspect.getsource(analyze.run)
	assert "optimus_session_ready" in src


def test_publish_session_event_helper_exists_in_both_layers():
	"""Both api.py (request-scoped: knows user from the session) and
	analyze.py (background-scoped: looks user up from the DocType)
	need a local _publish_session_event helper. Test that both
	exist and are callable."""
	from optimus import analyze, api

	assert hasattr(api, "_publish_session_event")
	assert callable(api._publish_session_event)
	assert hasattr(analyze, "_publish_session_event")
	assert callable(analyze._publish_session_event)


def test_publish_session_event_catches_exceptions():
	"""publish_realtime can fail (Socket.IO bridge down, dev env
	without redis-socketio running). The helper must swallow those
	exceptions — realtime is a UX convenience, not a hard dependency."""
	from optimus import analyze, api

	for src in (
		inspect.getsource(api._publish_session_event),
		inspect.getsource(analyze._publish_session_event),
	):
		# Must have a try/except around the publish_realtime call.
		assert "try:" in src and "except" in src, (
			"_publish_session_event must swallow publish_realtime "
			"failures — realtime is best-effort"
		)


# ---------------------------------------------------------------------------
# Client-side contract: floating_widget.js
# ---------------------------------------------------------------------------
# No Python import here — the widget is JS. Use text-level checks on
# the file to assert the expected subscribe calls + absence of the
# polling setInterval.


HERE = os.path.dirname(__file__)
WIDGET_JS = os.path.join(HERE, "..", "public", "js", "floating_widget.js")


def _read_widget_source() -> str:
	with open(WIDGET_JS) as f:
		return f.read()


def test_widget_no_longer_polls_status_on_interval():
	"""v0.5.1: the widget must NOT use setInterval to poll status().
	All state transitions come from realtime events + one-shot
	rehydrates on page load / visibility change."""
	src = _read_widget_source()
	# setInterval is used ONCE legitimately for the local elapsed
	# timer (updating the displayed "M:SS" label once a second).
	# Make sure there's no OTHER setInterval call that could restart
	# polling.
	count = src.count("setInterval(")
	assert count == 1, (
		f"Expected exactly 1 setInterval (the elapsed timer); got "
		f"{count}. Extra setInterval calls usually mean someone "
		"reintroduced polling."
	)
	# And the one that remains must be the elapsed timer.
	import re
	matches = re.findall(r"setInterval\(([^,]+),", src)
	assert matches, "setInterval call not found in widget"
	# The only callback should be an inline arrow function (the
	# elapsed-timer body), NOT a reference to refreshStatus.
	assert "refreshStatus" not in matches[0], (
		"setInterval callback must not be refreshStatus — polling "
		"of the status endpoint was removed in v0.5.1"
	)


def test_widget_has_no_polling_helpers():
	"""startPolling / stopPolling / pollHandle were removed in
	v0.5.1. If someone ports them back, the continuous
	/api/method/optimus.api.status traffic returns."""
	src = _read_widget_source()
	assert "startPolling" not in src, (
		"startPolling() helper must not exist — v0.5.1 removed "
		"HTTP polling in favor of realtime events"
	)
	assert "stopPolling" not in src, "stopPolling() helper must not exist"
	assert "pollHandle" not in src, "pollHandle variable must not exist"


def test_widget_subscribes_to_all_realtime_events():
	"""Client must have a frappe.realtime.on() subscription for
	each of the server-side emit points — otherwise state changes
	fire into the void and the widget hangs."""
	src = _read_widget_source()
	expected_events = [
		"optimus_session_stopping",
		"optimus_session_analyzing",
		"optimus_session_ready",
		"optimus_session_failed",
		"optimus_progress",
	]
	for event in expected_events:
		assert f'frappe.realtime.on("{event}"' in src, (
			f"Widget must subscribe to realtime event '{event}' via "
			"frappe.realtime.on(). Missing subscription means state "
			"changes from the server won't update the widget UI."
		)


def test_widget_visibility_handler_only_refreshes_once():
	"""Visibility change must trigger a one-shot refreshStatus,
	NOT start/stop a polling loop."""
	src = _read_widget_source()
	# Check that the visibilitychange handler still exists
	assert "visibilitychange" in src
	# And it calls refreshStatus (the one-shot)
	assert "refreshStatus()" in src
	# But NOT startPolling — that's gone.
	assert "startPolling" not in src


def test_widget_init_calls_status_only_once():
	"""init() must call refreshStatus exactly once for the one-shot
	rehydrate. Any additional call is a regression."""
	src = _read_widget_source()
	# Extract the init() function body by balanced-brace walking — a
	# naive regex like ``function init\(\)\s*\{([^}]+)\}`` would stop at
	# the first nested ``}`` (inside the early-return if-block, etc.),
	# missing the actual refreshStatus call below.
	start = src.index("function init()")
	open_brace = src.index("{", start)
	depth = 1
	i = open_brace + 1
	while i < len(src) and depth > 0:
		c = src[i]
		if c == "{":
			depth += 1
		elif c == "}":
			depth -= 1
		i += 1
	assert depth == 0, "init() function body has unbalanced braces"
	init_body = src[open_brace + 1:i - 1]
	# refreshStatus should appear exactly once in init's body
	assert init_body.count("refreshStatus") == 1, (
		"init() must call refreshStatus exactly once (one-shot "
		"rehydrate on page load). Extra calls suggest polling "
		"was reintroduced."
	)
