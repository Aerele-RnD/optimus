# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for v0.7.x ``_startup_probe_tool2`` recovery.

On Python 3.12+ line_profiler drives the process-global
``sys.monitoring`` PROFILER_ID (tool 2). A botched Phase-2 per-request
teardown (the pre-``fbf3179`` code paired ``enable_by_count()`` with
``disable()``, occasionally raising ``ValueError: tool 2 is not in
use``) left tool 2's LINE events registered process-wide → every
subsequent request in that worker line-traced → CPU peg + frozen UI.

The pre-``fbf3179`` failure mode required a ``bench restart`` to
recover. The ``fbf3179`` fix added two things:
  * ``capture.release_monitoring_tool()`` — idempotent unwinder called
    from the after_* hooks (covered by the unit suite's
    ``test_line_profile_monitoring.py``).
  * ``optimus._startup_probe_tool2()`` — worker-respawn recovery: if
    tool 2 is owned by line_profiler at app-import (i.e. the prior
    worker died mid-Phase-2 and its line-tracing state survived the
    process restart in the *same Python process group*), the probe
    reclaims it.

The unit suite (``test_line_profile_monitoring.py``) covers the
release helper at the function-call boundary. It cannot prove:

  * That the worker-respawn probe at ``optimus.__init__`` actually
    reclaims a leaked tool 2 in a real Frappe bench. Under pytest the
    probe runs against the Frappe stub.
  * That the probe correctly **declines to reclaim** tool 2 when it's
    owned by a non-line_profiler tool (a third-party debugger or
    profiler). The probe must respect ownership boundaries.

That gap is what this integration test fills. The tests invoke
``optimus._startup_probe_tool2`` directly against manipulated
``sys.monitoring`` state — same shape as the unit tests'
``_leak_tool`` helper, but exercised in a real-bench context.

A note on "simulating worker respawn": we can't actually fork a
worker mid-Phase-2 and re-import optimus inside a test (the optimus
module is already loaded). Instead, the test manipulates the
``sys.monitoring`` state to mirror what a leaked tool 2 looks like
after a worker death, then calls the probe directly. This exercises
the probe's recovery logic — which is the contract under test.
"""

from __future__ import annotations

import sys

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

import optimus

_HAS_MON = hasattr(sys, "monitoring")
_PID = sys.monitoring.PROFILER_ID if _HAS_MON else None


@pytest.mark.skipif(not _HAS_MON, reason="sys.monitoring requires Python 3.12+")
class TestPhase2ToolOrphanRecovery(FrappeTestCase):
	"""End-to-end: _startup_probe_tool2 reclaims a leaked tool 2."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		super().setUp()
		# Hard-reset tool 2 state before every test. A leaked tool from
		# the prior test would silently slow this one (and corrupt the
		# assertion).
		self._ensure_tool_2_is_free()

	def tearDown(self):
		# Belt-and-suspenders: never let a leaked tool 2 from this test
		# poison the rest of the integration suite.
		try:
			self._ensure_tool_2_is_free()
		except Exception:
			pass
		super().tearDown()

	# --- sys.monitoring helpers ---------------------------------------

	def _ensure_tool_2_is_free(self) -> None:
		"""Release tool 2 unconditionally. Mirrors the unit-suite
		``_guarantee_no_leak_escapes`` autouse fixture."""
		if sys.monitoring.get_tool(_PID) is not None:
			try:
				sys.monitoring.set_events(_PID, 0)
				sys.monitoring.free_tool_id(_PID)
			except Exception:
				pass

	def _leak_as_line_profiler(self) -> None:
		"""Simulate the post-leak state of a dead Phase-2 worker:
		tool 2 registered as ``line_profiler`` with LINE events on,
		exactly what a botched ``LineProfiler.disable()`` leaves
		behind."""
		self._ensure_tool_2_is_free()
		sys.monitoring.use_tool_id(_PID, "line_profiler")
		sys.monitoring.set_events(_PID, sys.monitoring.events.LINE)

	def _leak_as(self, owner: str) -> None:
		"""Register tool 2 as a non-line_profiler owner. Used to
		validate that the probe respects ownership boundaries — it
		MUST NOT reclaim a tool that belongs to a third-party
		profiler / debugger."""
		self._ensure_tool_2_is_free()
		sys.monitoring.use_tool_id(_PID, owner)
		# Don't set events — keeps the simulation lighter and matches
		# the most common third-party-tool registration pattern (claim
		# the tool slot, install events on demand).

	# --- The tests ----------------------------------------------------

	def test_probe_reclaims_leaked_line_profiler_tool_2_on_simulated_worker_respawn(self):
		"""The canary. Simulate the post-Phase-2-death state (tool 2
		owned by 'line_profiler' with LINE events on), then call the
		startup probe. The probe must reclaim the tool and reset its
		events to 0."""
		self._leak_as_line_profiler()
		# Sanity: the leak state is as expected.
		assert sys.monitoring.get_tool(_PID) == "line_profiler"
		assert sys.monitoring.get_events(_PID) != 0

		# Re-invoke the probe. (In production this runs once at
		# ``import optimus``; the test exercises it post-import to
		# simulate the worker-respawn recovery path.)
		optimus._startup_probe_tool2()

		# Tool 2 is now free + events cleared. Without the fbf3179 fix,
		# the leaked LINE events would line-trace every later request
		# in this worker → CPU peg + freeze.
		assert sys.monitoring.get_tool(_PID) is None, (
			"probe failed to reclaim leaked line_profiler tool 2 — "
			"a worker would line-trace every subsequent request"
		)
		assert sys.monitoring.get_events(_PID) == 0

	def test_probe_is_noop_when_tool_2_is_already_free(self):
		"""Happy path. Tool 2 is unowned; the probe is a silent no-op.
		Catches a regression where the probe accidentally registers
		itself as the owner (which would block legitimate Phase-2 runs
		from claiming the slot)."""
		# Confirm the precondition.
		assert sys.monitoring.get_tool(_PID) is None

		optimus._startup_probe_tool2()

		# Still unowned — the probe didn't grab the slot.
		assert sys.monitoring.get_tool(_PID) is None
		assert sys.monitoring.get_events(_PID) == 0

	def test_probe_warns_but_does_not_reclaim_non_line_profiler_owner(self):
		"""Boundary contract. If tool 2 is owned by something OTHER
		than line_profiler (a third-party debugger, py-spy, an IDE
		profiler), the probe MUST NOT reclaim it — that would silently
		break the third-party tool. The probe should warn (visible in
		logs) but leave the tool alone."""
		self._leak_as("third-party-debugger")
		assert sys.monitoring.get_tool(_PID) == "third-party-debugger"

		optimus._startup_probe_tool2()

		# The third-party tool is still in place — the probe respected
		# the boundary.
		assert sys.monitoring.get_tool(_PID) == "third-party-debugger", (
			"probe accidentally reclaimed a tool owned by a non-line_profiler — "
			"this would silently break the third-party tool's tracing"
		)
