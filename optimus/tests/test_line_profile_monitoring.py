# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 must never leak the process-global ``sys.monitoring`` profiler tool.

On Python 3.12+ line_profiler drives ``sys.monitoring`` tool id 2 (PROFILER_ID).
A botched per-request teardown (the old code paired ``enable_by_count()`` with
``disable()`` and raised ``ValueError: tool 2 is not in use``) left tool 2's
line-trace events registered process-wide, so every subsequent request in that
worker was line-traced → CPU saturation + frozen UI. These tests pin that the
teardown always releases tool 2.
"""

import sys
import types

import pytest

pytest.importorskip("line_profiler")

import frappe  # noqa: E402
from line_profiler import LineProfiler  # noqa: E402

from optimus.line_profile import capture as cap  # noqa: E402
from optimus.line_profile import hooks as lp_hooks  # noqa: E402

HAS_MON = hasattr(sys, "monitoring")
pytestmark = pytest.mark.skipif(not HAS_MON, reason="sys.monitoring requires Python 3.12+")
PID = sys.monitoring.PROFILER_ID if HAS_MON else None


def _hot():
	x = 0
	for i in range(20):
		x = x + i
	return x


def _leak_tool():
	"""Register tool 2 as ``line_profiler`` with line events on — exactly the
	state a botched line_profiler teardown leaves behind. Done via raw
	``sys.monitoring`` (not ``LineProfiler.enable_by_count``) so it can't desync
	line_profiler's process-global manager across tests."""
	if sys.monitoring.get_tool(PID) is not None:
		sys.monitoring.set_events(PID, 0)
		sys.monitoring.free_tool_id(PID)
	sys.monitoring.use_tool_id(PID, "line_profiler")
	sys.monitoring.set_events(PID, sys.monitoring.events.LINE)


@pytest.fixture(autouse=True)
def _guarantee_no_leak_escapes():
	# Belt-and-suspenders: never let a leaked tool 2 from one test poison the
	# rest of the suite (it would silently slow every following test).
	yield
	if HAS_MON and sys.monitoring.get_tool(PID) is not None:
		try:
			sys.monitoring.set_events(PID, 0)
			sys.monitoring.free_tool_id(PID)
		except Exception:
			pass


def test_release_reclaims_leaked_tool():
	_leak_tool()
	assert sys.monitoring.get_tool(PID) == "line_profiler"  # leaked + tracing
	assert sys.monitoring.get_events(PID) != 0

	cap.release_monitoring_tool()

	assert sys.monitoring.get_tool(PID) is None
	assert sys.monitoring.get_events(PID) == 0


def test_release_is_idempotent():
	cap.release_monitoring_tool()  # nothing registered
	cap.release_monitoring_tool()  # still safe
	assert sys.monitoring.get_tool(PID) is None


def _drive_after_request(monkeypatch, profiler, serialize_raises=False):
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(frappe, "local",
		types.SimpleNamespace(_lp_profiler=profiler, _lp_run_uuid="r1"), raising=False)
	if serialize_raises:
		monkeypatch.setattr(cap, "serialize_stats",
			lambda p: (_ for _ in ()).throw(RuntimeError("boom")), raising=True)
	else:
		monkeypatch.setattr(cap, "serialize_stats", lambda p: [], raising=True)
	monkeypatch.setattr(cap, "flush_samples", lambda r, s: None, raising=True)
	lp_hooks.after_request_line_profile()


def test_after_request_releases_tool(monkeypatch):
	p = LineProfiler()
	p.add_function(_hot)
	p.enable_by_count()
	_hot()
	_drive_after_request(monkeypatch, p)
	# The hook must leave NO global line-trace hook behind.
	assert sys.monitoring.get_tool(PID) is None


def test_after_request_releases_even_when_serialize_raises(monkeypatch):
	# If serialize/flush blows up, the tool must STILL be released (finally).
	p = LineProfiler()
	p.add_function(_hot)
	p.enable_by_count()
	_hot()
	_drive_after_request(monkeypatch, p, serialize_raises=True)
	assert sys.monitoring.get_tool(PID) is None


def test_after_request_releases_tool_even_when_disable_fails(monkeypatch):
	# The production bug: line_profiler's teardown raised "tool 2 is not in use"
	# and left tool 2 registered → process-wide tracing leak. Simulate that
	# leaked state, then drive after_request with a profiler whose disable
	# raises; the hook's finally-release must STILL clear tool 2.
	# (Pre-fix this assertion fails — the leak survives.)
	_leak_tool()
	assert sys.monitoring.get_tool(PID) == "line_profiler"

	class BrokenProfiler:
		def disable_by_count(self):
			raise ValueError("tool 2 is not in use")

		def disable(self):
			raise ValueError("tool 2 is not in use")

	_drive_after_request(monkeypatch, BrokenProfiler())
	assert sys.monitoring.get_tool(PID) is None
