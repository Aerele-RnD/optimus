# optimus/tests/test_scheduler_inline_fallback.py
# Copyright (c) 2026, Optimus contributors

"""Tests for v0.5.0 scheduler-aware analyze enqueue.

When bench disable-scheduler is in effect (is_scheduler_disabled() returns
True), _enqueue_analyze must pass now=True to frappe.enqueue so analyze
runs synchronously instead of being pushed to a queue no worker will
consume. Also verifies the recording-count safety cap prevents inline
analyze from exceeding the gunicorn request timeout on huge sessions.
"""

import inspect

from optimus import api


def test_enqueue_analyze_checks_scheduler():
    src = inspect.getsource(api._enqueue_analyze)
    assert "is_scheduler_disabled" in src
    assert "now=" in src


def test_stop_returns_ran_inline_flag():
    src = inspect.getsource(api.stop)
    # stop() must propagate whether analyze ran inline so the widget
    # can transition directly to Ready without passing through Analyzing.
    assert "ran_inline" in src


def test_enqueue_analyze_honors_inline_analyze_limit():
    # v0.5.1: cap check lives inside _enqueue_analyze now, not
    # _stop_session. Every inline-path caller (stop, retry_analyze,
    # janitor) gets the same protection uniformly.
    src = inspect.getsource(api._enqueue_analyze)
    assert "optimus_inline_analyze_limit" in src
    # And must NOT have the cap check still in _stop_session — that
    # would be a duplicate that could diverge.
    stop_src = inspect.getsource(api._stop_session)
    assert "optimus_inline_analyze_limit" not in stop_src


def test_enqueue_analyze_passes_now_when_scheduler_disabled(monkeypatch):
    """Full-stack check: when is_scheduler_disabled() is True, frappe.enqueue
    should be called with now=True.

    Assumes _enqueue_analyze does NOT import `is_scheduler_disabled` at
    module-level in api.py. The sys.modules monkeypatch below is applied
    inside the test body, so a name bound at api.py import time would
    bypass it. If that pattern changes, update this test to use
    monkeypatch.setattr on the bound symbol instead.
    """
    import sys
    import types

    import optimus.api as api_mod

    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    def fake_is_disabled():
        return True

    fake_mod = types.ModuleType("frappe.utils.scheduler")
    fake_mod.is_scheduler_disabled = fake_is_disabled
    monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", fake_mod)

    fake_frappe_utils = types.ModuleType("frappe.utils")
    fake_frappe_utils.scheduler = fake_mod
    monkeypatch.setitem(sys.modules, "frappe.utils", fake_frappe_utils)

    # Patch the frappe.enqueue that the module will call
    import frappe
    monkeypatch.setattr(frappe, "enqueue", fake_enqueue)
    # Defensive stub: cover every logger level the implementation might
    # reach for, not just .warning, so an unexpected .info/.debug call
    # doesn't mask the real assertion failure.
    monkeypatch.setattr(
        frappe,
        "logger",
        lambda: types.SimpleNamespace(
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
    )

    ran_inline = api_mod._enqueue_analyze("test-uuid-123")

    assert captured["kwargs"].get("now") is True
    assert captured["kwargs"].get("session_uuid") == "test-uuid-123"
    assert ran_inline is True


def test_retry_analyze_uses_scheduler_aware_enqueue():
	"""Pass-5 regression guard: retry_analyze used to call
	frappe.enqueue directly, bypassing the v0.5.0 scheduler-aware
	fallback. On sites with bench disable-scheduler, clicking 'Retry
	Analyze' on a Failed session would re-hit the exact hung-forever
	bug the v0.5.0 fix was designed to prevent.

	Source-inspection check: retry_analyze must use _enqueue_analyze,
	not a bare frappe.enqueue call with the analyze.run method string.
	"""
	import inspect

	from optimus import api

	src = inspect.getsource(api.retry_analyze)
	# Must reference the scheduler-aware helper.
	assert "_enqueue_analyze" in src, (
		"retry_analyze must call _enqueue_analyze (not frappe.enqueue "
		"directly) or it won't honor the scheduler-disabled fallback"
	)


def test_enqueue_analyze_swallows_inline_failure(monkeypatch):
	"""Regression guard (v0.5.1 architect review pass 3): when analyze
	runs inline and raises (analyze.run catches its own exception, marks
	the session Failed, and re-raises), _enqueue_analyze must catch the
	re-raise and return True.

	If we let the exception propagate up to stop(), the stop API
	returns a 500 and the widget shows "Failed to stop profiler —
	try again" — which is wrong, because the stop DID work; only
	analyze failed. The session is already marked Failed in the DB.
	"""
	import sys
	import types

	import frappe

	from optimus import api as api_mod

	# Stub is_scheduler_disabled → True so we take the inline branch.
	fake_sched_mod = types.ModuleType("frappe.utils.scheduler")
	fake_sched_mod.is_scheduler_disabled = lambda: True
	monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", fake_sched_mod)
	fake_frappe_utils = types.ModuleType("frappe.utils")
	fake_frappe_utils.scheduler = fake_sched_mod
	monkeypatch.setitem(sys.modules, "frappe.utils", fake_frappe_utils)

	# frappe.enqueue(..., now=True) raises, simulating analyze.run's
	# re-raise after it marked the session Failed.
	def raising_enqueue(*args, **kwargs):
		if kwargs.get("now") is True:
			raise RuntimeError("analyze.run marked session Failed and re-raised")
		raise AssertionError("should not reach the async path")

	monkeypatch.setattr(frappe, "enqueue", raising_enqueue)
	monkeypatch.setattr(
		frappe, "logger",
		lambda: types.SimpleNamespace(warning=lambda *a, **k: None),
	)
	monkeypatch.setattr(
		frappe, "log_error",
		lambda *a, **k: None,
		raising=False,
	)

	# Must NOT raise — the failure has been absorbed so stop() can
	# return 200 and report the Failed status to the widget.
	ran_inline = api_mod._enqueue_analyze("test-uuid-fail")
	assert ran_inline is True


def test_stop_returns_final_status_when_inline(monkeypatch):
	"""Source-inspection guard: stop() must read the final status off
	the Optimus Session doc after inline analyze runs, so a failed
	inline analyze doesn't report Ready to the widget."""
	import inspect

	from optimus import api

	src = inspect.getsource(api.stop)
	assert "final_status" in src or "\"status\"" in src
	# Must read the status from the doc via frappe.db.get_value.
	assert "get_value" in src
	assert "ran_inline" in src
	# And the response dict must include "status"
	assert '"status"' in src


def test_enqueue_analyze_blocks_huge_inline_session(monkeypatch):
    """Behavioral test for the inline-analyze recording cap.

    When scheduler is disabled AND recording_count > optimus_inline_analyze_limit,
    _enqueue_analyze must:
      1. Mark the Optimus Session as Failed
      2. Write an actionable message to analyzer_warnings
         (NOT 'analyze_error' — that field doesn't exist on the doctype,
         writing to it would crash with MariaDB 'Unknown column')
      3. NOT invoke frappe.enqueue
      4. Return True so the caller treats it like any other inline
         result and reads the final status off the doc

    v0.5.1: the cap check moved from _stop_session into _enqueue_analyze
    so ALL inline callers (stop, retry_analyze, janitor) get the same
    protection uniformly. Earlier versions only applied the cap in
    _stop_session, leaving retry_analyze vulnerable to the gunicorn
    timeout on re-runs of large failed sessions.
    """
    import sys
    import types

    import frappe

    from optimus import api as api_mod
    from optimus import session as session_mod

    # Stub scheduler_disabled = True
    fake_sched_mod = types.ModuleType("frappe.utils.scheduler")
    fake_sched_mod.is_scheduler_disabled = lambda: True
    monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", fake_sched_mod)

    fake_frappe_utils = types.ModuleType("frappe.utils")
    fake_frappe_utils.scheduler = fake_sched_mod
    monkeypatch.setitem(sys.modules, "frappe.utils", fake_frappe_utils)

    # frappe.conf is accessed via .get() in production. FakeConf mirrors
    # that so a silent switch to attribute access would be caught.
    class FakeConf:
        def get(self, key, default=None):
            return {"optimus_inline_analyze_limit": 50}.get(key, default)

    set_calls = []

    class FakeDB:
        def set_value(self, doctype, name, field_or_dict, value=None):
            set_calls.append((doctype, name, field_or_dict, value))
            return None

        def commit(self):
            pass

        def get_value(self, *a, **kw):
            return "PS-0001"

    monkeypatch.setattr(frappe, "conf", FakeConf(), raising=False)
    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(
        frappe, "log_error", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        frappe, "logger",
        lambda: types.SimpleNamespace(warning=lambda *a, **k: None),
        raising=False,
    )
    monkeypatch.setattr(
        session_mod, "recording_count", lambda session_uuid: 75
    )

    # frappe.enqueue must NOT be called at all when the cap fires.
    enqueue_calls = []
    def fake_enqueue(*args, **kwargs):
        enqueue_calls.append((args, kwargs))
    monkeypatch.setattr(frappe, "enqueue", fake_enqueue)

    ran_inline = api_mod._enqueue_analyze("test-uuid-huge", docname="PS-0001")

    # Return contract: cap-exceeded counts as "session is finalized".
    assert ran_inline is True
    # And frappe.enqueue must NOT have been called — the cap check
    # fires BEFORE the enqueue.
    assert enqueue_calls == []

    # Session must be marked Failed via set_value.
    status_calls = [
        c for c in set_calls
        if c[0] == "Optimus Session"
        and isinstance(c[2], dict)
        and c[2].get("status") == "Failed"
    ]
    assert len(status_calls) == 1

    # CRITICAL: the error message must be in `analyzer_warnings`, NOT
    # `analyze_error`. The doctype has no `analyze_error` field, so
    # writing to it would crash with MariaDB 'Unknown column' in
    # production. FakeDB accepts any field name so the test would
    # silently pass if the code regressed to the wrong field name —
    # assert explicitly.
    payload = status_calls[0][2]
    assert "analyzer_warnings" in payload, (
        "Cap-exceeded failure must write to analyzer_warnings, not "
        "analyze_error. analyze_error is NOT a field on Optimus Session "
        "and writing to it crashes production with Unknown column."
    )
    assert "analyze_error" not in payload

    error_msg = payload["analyzer_warnings"]
    assert "75" in error_msg
    assert "50" in error_msg
    assert "enable-scheduler" in error_msg


def test_enqueue_analyze_cap_is_called_by_stop_session():
    """Source-inspection regression guard: _stop_session must pass
    docname to _enqueue_analyze so the cap check has the doc to
    update. Earlier versions had the cap check inline in
    _stop_session — the refactor moved it to _enqueue_analyze, and
    if _stop_session forgets to pass docname, the cap silently
    skips.
    """
    import inspect

    from optimus import api

    stop_src = inspect.getsource(api._stop_session)
    assert "_enqueue_analyze(session_uuid, docname=" in stop_src or \
           "_enqueue_analyze(\n\t\tsession_uuid," in stop_src or \
           "docname=docname" in stop_src, (
        "_stop_session must pass docname to _enqueue_analyze so the "
        "inline cap check has the doc to mark Failed"
    )


def test_enqueue_analyze_cap_is_called_by_retry_analyze():
    """Same guard for retry_analyze. v0.5.1 fix ensures retry
    applies the same inline cap as stop() — a 200-recording Failed
    session on a scheduler-disabled site must not run inline
    without the cap check.
    """
    import inspect

    from optimus import api

    retry_src = inspect.getsource(api.retry_analyze)
    assert "_enqueue_analyze" in retry_src
    assert "docname=" in retry_src, (
        "retry_analyze must pass docname to _enqueue_analyze"
    )
