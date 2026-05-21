# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 overhead budget: profiling must observe without spoiling the flow.

A time-budget watchdog auto-disengages line tracing mid-request so a hot-loop
function can't freeze the user's flow. These tests pin the watchdog wiring and
the budget-hit flag deterministically (fake profiler for the hooks; raw
sys.monitoring registration for the disengage path — no real line_profiler
enable, so line_profiler's process-global manager can't desync across tests).
"""

import sys
import time
import types

import frappe
import pytest

from optimus.line_profile import capture as cap
from optimus.line_profile import hooks as lp_hooks

HAS_MON = hasattr(sys, "monitoring")
PID = sys.monitoring.PROFILER_ID if HAS_MON else None


class FakeCache:
	def __init__(self):
		self.store = {}

	def get_value(self, k):
		return self.store.get(k)

	def set_value(self, k, v, expires_in_sec=None):
		self.store[k] = v

	def delete_value(self, k):
		self.store.pop(k, None)


class FakeProfiler:
	"""Stand-in so hook tests exercise the watchdog wiring without a real
	LineProfiler (which would touch the global sys.monitoring manager)."""

	def __init__(self):
		self.enabled = 0

	def enable_by_count(self):
		self.enabled += 1

	def disable_by_count(self):
		self.enabled -= 1

	def get_stats(self):
		return types.SimpleNamespace(timings={}, unit=1e-6)


@pytest.fixture
def env(monkeypatch):
	cache = FakeCache()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="u@x.com"), raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
	monkeypatch.setattr(
		frappe, "conf",
		types.SimpleNamespace(get=lambda k, d=None: {"optimus_phase2_overhead_budget_seconds": 10}.get(k, d)),
		raising=False,
	)
	yield types.SimpleNamespace(cache=cache, monkeypatch=monkeypatch)
	# Never let a leaked tool 2 escape to other tests.
	if HAS_MON and sys.monitoring.get_tool(PID) is not None:
		try:
			sys.monitoring.set_events(PID, 0)
			sys.monitoring.free_tool_id(PID)
		except Exception:
			pass


# --------------------------------------------------------------------------
# budget-hit flag + disengage
# --------------------------------------------------------------------------

def test_budget_flag_roundtrip(env):
	assert cap.budget_was_hit("r1") is False
	cap.mark_budget_hit("r1")
	assert cap.budget_was_hit("r1") is True
	cap.clear_budget_hit("r1")
	assert cap.budget_was_hit("r1") is False


def test_disengage_stops_events_without_freeing_tool(env):
	"""The watchdog disengages line tracing by zeroing events — it must NOT
	free_tool_id. Freeing the tool from the timer thread, mid-request, yanks it
	out from under line_profiler's still-active manager; its own
	``disable_by_count`` then fails ("tool 2 is not in use"), leaving an
	orphaned profiler whose weakref finalizer fires ``handle_raise_event`` at
	teardown with ``sys`` torn down → ``'NoneType' object has no attribute
	'monitoring'``. Disengage = events 0, tool stays registered for
	line_profiler's own disable to clean up."""
	if HAS_MON:
		sys.monitoring.use_tool_id(PID, "line_profiler")
		sys.monitoring.set_events(PID, sys.monitoring.events.LINE)
	cap._disengage_run("r2")
	if HAS_MON:
		# Still ours (not freed) → line_profiler.disable_by_count stays valid…
		assert sys.monitoring.get_tool(PID) == "line_profiler"
		# …but line tracing is disengaged so the flow runs at full speed.
		assert sys.monitoring.get_events(PID) == 0
	assert cap.budget_was_hit("r2") is True


@pytest.mark.skipif(
	not HAS_MON or not cap._LP_AVAILABLE,
	reason="needs sys.monitoring + line_profiler",
)
def test_disengage_does_not_desync_real_line_profiler(env):
	"""Regression: the watchdog firing mid-request must leave line_profiler's
	own teardown working. With the old free_tool_id disengage, the line below
	raised ``ValueError: tool 2 is not in use`` and orphaned the profiler."""
	from line_profiler import LineProfiler

	def hot():
		total = 0
		for i in range(1000):
			total += i
		return total

	prof = LineProfiler()
	prof.add_function(hot)
	prof.enable_by_count()
	try:
		cap._disengage_run("rX")  # watchdog fires while the profiler is live
		# line_profiler's own count-guarded teardown must still succeed.
		prof.disable_by_count()
		assert sys.monitoring.get_tool(PID) is None  # cleanly freed by disable
	finally:
		cap.release_monitoring_tool()


def test_start_watchdog_none_when_budget_zero(env):
	assert cap.start_overhead_watchdog("r", 0) is None
	t = cap.start_overhead_watchdog("r", 10)
	assert t is not None
	t.cancel()


@pytest.mark.skipif(not HAS_MON, reason="sys.monitoring requires Python 3.12+")
def test_started_watchdog_disengages_after_budget(env):
	# A real Timer fires the disengage, which zeroes events on a registered
	# tool 2 (stops the overhead) without freeing it.
	sys.monitoring.use_tool_id(PID, "line_profiler")
	sys.monitoring.set_events(PID, sys.monitoring.events.LINE)
	cap.start_overhead_watchdog("r3", 0.05)
	time.sleep(0.25)
	assert sys.monitoring.get_tool(PID) == "line_profiler"  # not freed
	assert sys.monitoring.get_events(PID) == 0  # tracing disengaged mid-request
	assert cap.budget_was_hit("r3") is True


# --------------------------------------------------------------------------
# hook wiring (fake profiler — deterministic)
# --------------------------------------------------------------------------

def _arm_before(env, budget):
	env.monkeypatch.setattr(cap, "is_active", lambda u: "run-1", raising=False)
	env.monkeypatch.setattr(lp_hooks.hooks_callbacks, "_should_skip_request", lambda: False, raising=False)
	env.monkeypatch.setattr(cap, "make_profiler", lambda r: FakeProfiler(), raising=False)
	env.monkeypatch.setattr(
		frappe, "conf",
		types.SimpleNamespace(get=lambda k, d=None: {"optimus_phase2_overhead_budget_seconds": budget}.get(k, d)),
		raising=False,
	)


def test_before_request_starts_watchdog_and_after_cancels(env):
	_arm_before(env, budget=10)
	lp_hooks.before_request_line_profile()
	wd = getattr(frappe.local, "_lp_watchdog", None)
	assert wd is not None and wd.is_alive()  # observing, with a budget timer armed

	lp_hooks.after_request_line_profile()
	assert getattr(frappe.local, "_lp_watchdog", None) is None
	# Cancelled (Timer.cancel sets `finished`): a within-budget request keeps
	# full data and the timer won't disengage a later request.
	assert wd.finished.is_set()
	assert cap.budget_was_hit("run-1") is False


def test_before_request_no_watchdog_when_budget_zero(env):
	_arm_before(env, budget=0)
	lp_hooks.before_request_line_profile()
	assert getattr(frappe.local, "_lp_watchdog", None) is None  # unlimited = no timer
	lp_hooks.after_request_line_profile()
