# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""End-to-end recording lifecycle against a real Frappe bench.

The canonical smoke test for the whole capture → analyze → render pipeline:

  1. ``api.start`` creates an ``Optimus Session`` DocType row AND a
     ``profiler:active:<user>`` Redis pointer.
  2. The session is in ``Recording`` state, addressable by uuid.
  3. ``api.stop`` clears the active pointer + marks status ``Stopping``
     + enqueues (or inline-runs) the analyze job.
  4. Within the polling window, the session lands on a terminal state
     (``Ready`` on success, ``Failed`` if the analyze raised).
  5. On Ready, the report HTML file is attached + the session carries
     plausible totals.

This single test exercises:

  * the recorder monkey-patch installed at app-import time
    (``optimus/__init__.py::_patch_recorder``)
  * the v0.7.x bg-tracking trilogy's per-job meta writes (no bg jobs
    triggered here, but the path stays open)
  * the analyze enqueue + RQ job + the full renderer pipeline
  * the File-attach step that persists the report next to the session

Failure here means the integration layer broke. Pure-pytest can verify
every component in isolation but cannot catch a regression in the
inter-component handoff — that's what this test is for.
"""

from __future__ import annotations

import time

import frappe
from frappe.tests.utils import FrappeTestCase

_POLL_INTERVAL_SECONDS = 0.5
_TERMINAL_STATUSES = ("Ready", "Failed")


def _wait_for_terminal(session_uuid: str, *, timeout_seconds: int = 60) -> str | None:
	"""Poll the session's ``status`` field every 500 ms until terminal
	(``Ready`` / ``Failed``) or until ``timeout_seconds`` elapses.
	Returns the final status, or ``None`` on timeout."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		status = frappe.db.get_value(
			"Optimus Session",
			{"session_uuid": session_uuid},
			"status",
		)
		if status in _TERMINAL_STATUSES:
			return status
		time.sleep(_POLL_INTERVAL_SECONDS)
	return None


class TestRecordingLifecycleE2E(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Run as Administrator so the user-permission gates in api.start
		# / api.stop don't refuse — the test is exercising the capture
		# pipeline, not the auth surface (that's covered by other tests).
		frappe.set_user("Administrator")

	# --- 1: start creates session row + redis pointer ----------------------

	def test_start_creates_session_row_and_redis_pointer(self):
		from optimus import api

		result = api.start(label="integration: start creates session")
		try:
			session_uuid = result["session_uuid"]
			assert session_uuid and isinstance(session_uuid, str)

			row = frappe.db.get_value(
				"Optimus Session",
				{"session_uuid": session_uuid},
				["name", "status", "user", "title"],
				as_dict=True,
			)
			assert row is not None, "start() didn't insert a session row"
			assert row["status"] == "Recording", (
				f"new session should be in Recording, got {row['status']!r}"
			)
			assert row["user"] == "Administrator"
			assert "integration" in row["title"].lower()

			# Redis active pointer is what gates per-request capture; absent =
			# every after-start request runs unrecorded.
			active = frappe.cache.get_value("profiler:active:Administrator")
			assert active == session_uuid, (
				f"active pointer mismatch: cache has {active!r}, expected "
				f"{session_uuid!r}"
			)
		finally:
			# Clean up so the next test starts from a known state — the
			# autouse cleanup_session fixture handles this too, but
			# defence-in-depth keeps the test self-contained.
			try:
				api.stop()
			except Exception:
				pass

	# --- 2: stop clears active + marks Stopping (or finalises inline) ------

	def test_stop_clears_active_and_marks_stopping(self):
		from optimus import api

		start_result = api.start(label="integration: stop semantics")
		session_uuid = start_result["session_uuid"]

		stop_result = api.stop()
		assert stop_result["stopped"] is True
		assert stop_result["session_uuid"] == session_uuid

		# Active pointer cleared regardless of inline-vs-enqueued analyze.
		active = frappe.cache.get_value("profiler:active:Administrator")
		assert active is None, (
			f"active pointer should be cleared after stop, got {active!r}"
		)

		# Status is one of Stopping / Analyzing / Ready / Failed. The
		# v0.5.0 inline-analyze path can land on Ready before stop() returns;
		# the async path stays at Stopping until the RQ worker picks the job up.
		status = frappe.db.get_value(
			"Optimus Session",
			{"session_uuid": session_uuid},
			"status",
		)
		assert status in ("Stopping", "Analyzing", "Ready", "Failed"), (
			f"unexpected post-stop status: {status!r}"
		)

	# --- 3 + 4: analyze completes; report is attached ----------------------

	def test_full_lifecycle_reaches_ready_and_attaches_report(self):
		"""The big-picture smoke: start → stop → wait → Ready + report.

		This is the canonical regression canary. A failure here means
		some part of the capture / analyze / render pipeline broke and
		the unit suite missed it. Read the failure message + the bench
		logs (CI uploads them as ``integration-logs``) to localise.
		"""
		from optimus import api

		start_result = api.start(label="integration: full lifecycle")
		session_uuid = start_result["session_uuid"]
		docname = start_result["docname"]

		# No HTTP traffic in between — a stop() immediately after start()
		# produces a session with zero recordings. analyze.run handles
		# that gracefully (renders a "no recordings" report) so this is
		# still a valid smoke. A future PR can add an actual recorded
		# operation here for richer coverage.
		api.stop()

		final_status = _wait_for_terminal(session_uuid, timeout_seconds=60)
		assert final_status == "Ready", (
			f"session didn't reach Ready within 60 s; final status: "
			f"{final_status!r}. Bench logs may show why (uploaded as "
			f"integration-logs artifact on failure)."
		)

		# Report file is attached. The renderer writes the safe-report
		# HTML as a File row pointing at the session's docname.
		attached = frappe.db.exists(
			"File",
			{
				"attached_to_doctype": "Optimus Session",
				"attached_to_name": docname,
				"file_name": ("like", "%optimus_report%.html"),
			},
		)
		assert attached, (
			"no Optimus report HTML attached to the session — render must "
			"have failed silently"
		)

	# --- 5: sanity-floor — totals are populated ---------------------------

	def test_session_totals_populated_after_analyze(self):
		"""After a successful analyze, the session's persisted totals are
		set to *something* (zero is fine on an empty-recording session).
		This is the floor — if totals are None / missing, a write step
		in analyze got skipped."""
		from optimus import api

		start_result = api.start(label="integration: totals populated")
		session_uuid = start_result["session_uuid"]
		api.stop()
		_wait_for_terminal(session_uuid, timeout_seconds=60)

		row = frappe.db.get_value(
			"Optimus Session",
			{"session_uuid": session_uuid},
			["total_duration_ms", "total_query_time_ms", "total_queries", "total_requests"],
			as_dict=True,
		)
		assert row is not None
		# Each field must be a number (int/float), not None.
		for field in ("total_duration_ms", "total_query_time_ms", "total_queries", "total_requests"):
			assert row[field] is not None, (
				f"{field} is None after analyze — analyze.run skipped the totals write"
			)
			assert row[field] >= 0, f"{field} should be non-negative, got {row[field]!r}"
