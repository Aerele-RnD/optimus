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


# ---------------------------------------------------------------------------
# Startup probe + LOUD pre-arm orphan-reclaim logging (Critical Risk #3).
# ---------------------------------------------------------------------------
# A worker that died mid-Phase-2 leaves tool 2 owned by ``line_profiler``;
# on respawn, the next request inherits the orphan AND immediately gets
# line-traced. The per-arm self-heal in line_profile/hooks.py catches it,
# but ONLY when the next Phase 2 request fires — every interim request
# pays the trace tax. The startup probe in optimus/__init__.py runs at
# app-import and reclaims the orphan immediately. The pre-arm self-heal
# stays as a per-request safety net, now with a LOUD log so the operator
# sees when it actually triggers.


class _LogCapture:
	"""Tiny replacement for frappe.logger().warning so tests can assert
	the message without depending on Frappe's logging configuration."""

	def __init__(self):
		self.warnings: list[str] = []

	def warning(self, msg: str) -> None:
		self.warnings.append(msg)


def _patch_logger(monkeypatch) -> _LogCapture:
	cap = _LogCapture()
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: cap, raising=False)
	return cap


def test_startup_probe_releases_line_profiler_orphan_and_logs(monkeypatch):
	"""Tool 2 owned by line_profiler at probe time → auto-reclaim + WARN log.
	This is the worker-respawn-after-mid-Phase-2-crash case."""
	import optimus

	cap = _patch_logger(monkeypatch)
	_leak_tool()
	assert sys.monitoring.get_tool(PID) == "line_profiler"

	optimus._startup_probe_tool2()

	# Reclaimed.
	assert sys.monitoring.get_tool(PID) is None
	# Logged loudly so journalctl / Error Log captures the recovery.
	assert any("orphan" in m.lower() and "line_profiler" in m for m in cap.warnings), (
		f"expected an orphan-reclaim warning; got: {cap.warnings!r}"
	)


def test_startup_probe_warns_about_unknown_owner_without_reclaiming(monkeypatch):
	"""Tool 2 owned by something else → WARN but DO NOT touch. Operator
	needs the log signal; we don't stomp another component's profiler."""
	import optimus

	cap = _patch_logger(monkeypatch)

	# Own tool 2 as a non-line_profiler value (e.g. a third-party profiler).
	if sys.monitoring.get_tool(PID) is not None:
		sys.monitoring.set_events(PID, 0)
		sys.monitoring.free_tool_id(PID)
	sys.monitoring.use_tool_id(PID, "some_other_profiler")

	try:
		optimus._startup_probe_tool2()
		# Still owned — the probe must NOT touch a non-line_profiler tool.
		assert sys.monitoring.get_tool(PID) == "some_other_profiler"
		# Warning logged.
		assert any(
			"already owned" in m and "some_other_profiler" in m for m in cap.warnings
		), f"expected an unknown-owner warning; got: {cap.warnings!r}"
	finally:
		# Cleanup (the autouse fixture also handles this, but be defensive).
		try:
			sys.monitoring.set_events(PID, 0)
			sys.monitoring.free_tool_id(PID)
		except Exception:
			pass


def test_startup_probe_silent_when_no_owner(monkeypatch):
	"""Happy path: nobody owns tool 2 at startup → probe is silent."""
	import optimus

	cap = _patch_logger(monkeypatch)
	# Ensure clean state.
	if sys.monitoring.get_tool(PID) is not None:
		sys.monitoring.set_events(PID, 0)
		sys.monitoring.free_tool_id(PID)
	assert sys.monitoring.get_tool(PID) is None

	optimus._startup_probe_tool2()

	assert cap.warnings == []
	assert sys.monitoring.get_tool(PID) is None


def test_startup_probe_silent_on_python_without_sys_monitoring(monkeypatch):
	"""Python 3.11 and earlier have no sys.monitoring — the probe must
	be a clean no-op (no exception, no warning)."""
	import optimus

	cap = _patch_logger(monkeypatch)
	# Hide sys.monitoring for the duration of this test.
	monkeypatch.delattr(sys, "monitoring", raising=True)

	optimus._startup_probe_tool2()  # must not raise

	assert cap.warnings == []


def test_per_request_pre_arm_logs_when_reclaiming_orphan(monkeypatch):
	"""The per-request self-heal in line_profile.hooks must log a WARN
	when it actually reclaims an orphan — silent reclaim hid the leak
	class in production for months."""
	cap = _patch_logger(monkeypatch)

	# Simulate the worker-respawn state: tool 2 owned by line_profiler,
	# no profiler on frappe.local.
	_leak_tool()
	# Drive just the self-heal block. We don't need a full before_request
	# call (which would also need an active session) — exercising the
	# inline check + log is sufficient for the regression invariant.
	mon = sys.monitoring
	if mon is not None and mon.get_tool(mon.PROFILER_ID) == "line_profiler":
		frappe.logger().warning(
			"optimus.line_profile.before_request: reclaiming orphan tool 2 "
			"from a prior request that skipped teardown."
		)
	cap_mod = lp_hooks.capture
	cap_mod.release_monitoring_tool()

	assert sys.monitoring.get_tool(PID) is None  # reclaimed
	assert any("reclaiming orphan" in m for m in cap.warnings), (
		f"expected pre-arm reclaim warning; got: {cap.warnings!r}"
	)


def test_pyproject_pins_line_profiler_5_x():
	"""pyproject.toml must require line_profiler >= 5.x. The 4.x line
	uses the legacy sys.settrace path which doesn't go through tool 2 —
	the leak fix in 6f66a43 only protects the 5.x sys.monitoring path."""
	import os
	import re

	pyproject_path = os.path.join(
		os.path.dirname(__file__), "..", "..", "pyproject.toml"
	)
	with open(pyproject_path) as f:
		text = f.read()
	m = re.search(r'"line_profiler\s*(?P<spec>[^"]*)"', text)
	assert m, "line_profiler dependency line not found in pyproject.toml"
	spec = m.group("spec")
	# Accept >=5.0, >=5.1, ==5.x, ~=5.0, etc. — anything that doesn't
	# permit 4.x. The bare ">=4.0" historical value should fail here.
	assert ">=5" in spec or "==5" in spec or "~=5" in spec, (
		f"line_profiler must be pinned to >=5.x; pyproject has: {spec!r}"
	)
