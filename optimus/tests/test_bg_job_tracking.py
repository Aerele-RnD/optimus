# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Hook-time bg-job status tracking — write Running/Completed + started_at /
ended_at / duration_ms to the session's jobs hash from inside before_job /
after_job, so analyze doesn't have to re-fetch the data from RQ (whose
records may have been GC'd by the time analyze persists).

Companion to ``test_bg_job_status.py`` (which covers the analyze-time
``_capture_job_terminal_status`` fallback path). These tests cover the
authoritative hook-time path.

Background: pre-fix, three failure modes left ``Optimus Background Job``
child rows wrong:

1. ``bg_recompute_aggregates`` (or any ``enqueue_after_commit=True`` job):
   blank ``method`` because the enqueue patch skips ``record_job`` when
   ``frappe.enqueue`` returns None for deferred dispatch
   (``optimus/__init__.py:115`` guard fails). before_job now calls
   ``record_job`` with the method it has access to from kwargs.

2. ``bg_recheck_users`` / ``bg_chained_audit`` (first link): stuck at
   ``status=Running`` with NULL times because the worker finished, RQ
   eventually GC'd its job record, and ``Job.fetch`` in
   ``_capture_job_terminal_status`` raised ``NoSuchJobError`` (silently
   swallowed). after_job now writes the terminal status + times while
   the worker is still running, so analyze doesn't need RQ to be alive.

3. Jobs whose recorder bailed out post-marker (orphan case): tracked
   anyway via a separate ``frappe.local.optimus_bg_session_uuid`` stash
   so the bg-jobs report still shows them.
"""

import os
import re
import sys
import time
import types

import pytest

pytest.importorskip("rq")


# ---------------------------------------------------------------------------
# Test doubles — keep mocks lean so each test owns just what it exercises.
# ---------------------------------------------------------------------------


class FakeJob:
	"""Stand-in for an RQ Job. ``get_current_job()`` returns one of these
	inside the worker; before_job / after_job read .id / .get_status() /
	.exc_info off it."""

	def __init__(self, job_id="J1", status="started", exc_info=None):
		self.id = job_id
		self._status = status
		self.exc_info = exc_info

	def get_status(self, refresh=True):
		return self._status


@pytest.fixture
def captures(monkeypatch):
	"""Capture calls to session.record_job and session.set_job_status without
	hitting Redis. Returns the same dict to all tests; tests inspect it."""
	from optimus import session

	store = {"record_job": [], "set_job_status": []}
	monkeypatch.setattr(
		session,
		"record_job",
		lambda su, jid, m: store["record_job"].append((su, jid, m)),
		raising=False,
	)
	monkeypatch.setattr(
		session,
		"set_job_status",
		lambda su, jid, **kw: store["set_job_status"].append((su, jid, kw)),
		raising=False,
	)
	return store


@pytest.fixture
def fake_local(monkeypatch):
	"""Replace frappe.local with a clean namespace so the SUT can set
	``optimus_bg_job_start_mono`` etc. without touching real state.

	Also stubs ``frappe.utils.now_datetime`` to a fixed string because the
	real implementation reads ``frappe.db`` for the site's system timezone
	— which isn't wired up in a plain-pytest context. The SUT's own
	``try/except: pass`` would swallow the resulting AttributeError and
	silently skip ``set_job_status``, masking the bug behind a green-ish
	test. Stubbing here keeps the helpers exercisable end-to-end.
	"""
	import frappe
	import frappe.utils

	local = types.SimpleNamespace()
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(
		frappe.utils,
		"now_datetime",
		lambda: "2026-05-24 15:20:00.000000",
		raising=False,
	)
	return local


@pytest.fixture
def fake_rq_job(monkeypatch):
	"""Install a FakeJob as the current RQ job. Tests pass status/exc_info."""
	import rq

	holder = {"job": FakeJob("J1", "started")}
	monkeypatch.setattr(rq, "get_current_job", lambda: holder["job"], raising=False)
	# Also patch the symbol on the SUT-side module since the helpers do a
	# fresh ``from rq import get_current_job`` per call — monkeypatching the
	# rq module covers it because the lazy import reads ``rq.__dict__``.
	return holder


# ---------------------------------------------------------------------------
# _track_bg_job_started — fires from before_job once the marker is valid
# ---------------------------------------------------------------------------


class TestTrackBgJobStarted:
	def test_records_method_and_marks_running_with_started_at(self, captures, fake_local, fake_rq_job):
		from optimus import hooks_callbacks

		hooks_callbacks._track_bg_job_started("S1", "myapp.tasks.foo")

		# Method recorded so analyze can populate the bg-job row's Method
		# column even when the enqueue patch couldn't (after_commit path).
		assert captures["record_job"] == [("S1", "J1", "myapp.tasks.foo")]

		# Marked Running with a started_at timestamp.
		assert len(captures["set_job_status"]) == 1
		su, jid, fields = captures["set_job_status"][0]
		assert (su, jid) == ("S1", "J1")
		assert fields["status"] == "Running"
		assert "started_at" in fields and fields["started_at"]

		# Monotonic baseline stashed for the after_job duration calc.
		assert hasattr(fake_local, "optimus_bg_job_start_mono")
		assert isinstance(fake_local.optimus_bg_job_start_mono, float)

	def test_callable_method_uses___name__(self, captures, fake_local, fake_rq_job):
		"""frappe.enqueue accepts a callable too — extract its __name__ so the
		row doesn't render ``<function foo at 0x...>``."""
		from optimus import hooks_callbacks

		def my_callable():
			pass

		hooks_callbacks._track_bg_job_started("S1", my_callable)
		assert captures["record_job"][0][2] == "my_callable"

	def test_no_current_job_is_noop(self, captures, fake_local, monkeypatch):
		"""Outside a worker context (no RQ job) → tracking quietly does
		nothing; never raises."""
		import rq

		monkeypatch.setattr(rq, "get_current_job", lambda: None, raising=False)
		from optimus import hooks_callbacks

		hooks_callbacks._track_bg_job_started("S1", "myapp.tasks.foo")
		assert captures["record_job"] == []
		assert captures["set_job_status"] == []

	def test_swallows_exceptions(self, captures, fake_local, fake_rq_job, monkeypatch):
		"""Tracking is best-effort; a Redis hiccup must NEVER break the job."""
		from optimus import hooks_callbacks, session

		def boom(*a, **kw):
			raise RuntimeError("redis is on fire")

		monkeypatch.setattr(session, "set_job_status", boom, raising=False)
		# Must not raise.
		hooks_callbacks._track_bg_job_started("S1", "myapp.tasks.foo")


# ---------------------------------------------------------------------------
# _track_bg_job_finished — fires from after_job's finally block
# ---------------------------------------------------------------------------


class TestTrackBgJobFinished:
	def test_completed_with_duration_no_exception(self, captures, fake_local, fake_rq_job):
		from optimus import hooks_callbacks

		# Simulate before_job having stashed the baseline ~500ms ago.
		fake_local.optimus_bg_job_start_mono = time.monotonic() - 0.5

		hooks_callbacks._track_bg_job_finished("S1", "J1")

		assert len(captures["set_job_status"]) == 1
		su, jid, fields = captures["set_job_status"][0]
		assert (su, jid) == ("S1", "J1")
		assert fields["status"] == "Completed"
		assert fields["error"] is None
		assert "ended_at" in fields and fields["ended_at"]
		# Wide tolerance — test runners vary; we only need to confirm a real
		# value was computed, not jitter-tight precision.
		assert 400 <= fields["duration_ms"] <= 1500

	def test_failed_when_exception_in_flight(self, captures, fake_local, fake_rq_job):
		"""after_job runs in a Frappe ``finally`` block — if the user's method
		raised, ``sys.exc_info()`` reports it. We must capture that as
		``status=Failed`` with a useful error string."""
		from optimus import hooks_callbacks

		fake_local.optimus_bg_job_start_mono = time.monotonic() - 0.1
		try:
			raise ValueError("bad doc_name")
		except ValueError:
			hooks_callbacks._track_bg_job_finished("S1", "J1")

		assert len(captures["set_job_status"]) == 1
		_, _, fields = captures["set_job_status"][0]
		assert fields["status"] == "Failed"
		assert "ValueError" in fields["error"]
		assert "bad doc_name" in fields["error"]

	def test_timeout_when_jobtimeoutexception_in_flight(self, captures, fake_local, fake_rq_job):
		"""RQ kills a job that exceeds its timeout with
		``rq.timeouts.JobTimeoutException`` — distinguish that from a
		generic user-code failure so the report can flag it specifically."""
		from optimus import hooks_callbacks

		# Synthesize the class so we don't depend on rq's internals (its
		# class name is what the SUT keys on, not the import path).
		class JobTimeoutException(Exception):
			pass

		fake_local.optimus_bg_job_start_mono = time.monotonic() - 0.1
		try:
			raise JobTimeoutException("Task exceeded maximum timeout value (180 seconds)")
		except JobTimeoutException:
			hooks_callbacks._track_bg_job_finished("S1", "J1")

		_, _, fields = captures["set_job_status"][0]
		assert fields["status"] == "Timeout"
		assert "JobTimeoutException" in fields["error"]
		assert "180 seconds" in fields["error"]

	def test_no_baseline_means_duration_ms_is_none(self, captures, fake_local, fake_rq_job):
		"""If before_job didn't fire (e.g. the worker was upgraded mid-job),
		we have no baseline → duration_ms is None and analyze can fall back to
		whatever RQ's record still says. Don't fabricate a duration."""
		from optimus import hooks_callbacks

		# Note: NO optimus_bg_job_start_mono on fake_local.
		hooks_callbacks._track_bg_job_finished("S1", "J1")

		_, _, fields = captures["set_job_status"][0]
		assert fields["status"] == "Completed"
		assert fields["duration_ms"] is None

	def test_swallows_exceptions(self, captures, fake_local, fake_rq_job, monkeypatch):
		from optimus import hooks_callbacks, session

		def boom(*a, **kw):
			raise RuntimeError("redis is on fire")

		monkeypatch.setattr(session, "set_job_status", boom, raising=False)
		hooks_callbacks._track_bg_job_finished("S1", "J1")  # must not raise


# ---------------------------------------------------------------------------
# Source-inspection: confirm before_job / after_job actually call the helpers
# ---------------------------------------------------------------------------
# Mirrors the existing ``test_before_job_honours_the_draining_window`` pattern
# in test_background_job_capture.py — these hooks have too much real-Frappe
# dependency to call directly in a unit test, but we can prove the call site
# is wired up correctly via source-inspection.


def _src(rel_path: str) -> str:
	here = os.path.dirname(__file__)
	with open(os.path.join(here, "..", rel_path)) as f:
		return f.read()


def _fn_body(src: str, name: str) -> str:
	start = src.index(f"def {name}(")
	after = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist|class )", src[after:])
	end = after + (nxt.start() if nxt else len(src) - after)
	return src[start:end]


class TestHookWiring:
	def test_before_job_calls_track_bg_job_started_after_marker_pop(self):
		"""The tracking call must come AFTER the marker pop (we need a valid
		session_uuid) and BEFORE the user/active-session gates (so orphan
		jobs whose recorder we won't activate still get tracked)."""
		body = _fn_body(_src("hooks_callbacks.py"), "before_job")
		assert "_track_bg_job_started(session_uuid" in body, (
			"before_job must call _track_bg_job_started so jobs are recorded "
			"in the session's jobs hash regardless of after_commit timing"
		)
		# Ordering: tracking comes after the marker pop, before the user/active
		# checks. We assert the relative position of the substring matches.
		pop_idx = body.index('kwargs.pop("_profiler_session_id"')
		track_idx = body.index("_track_bg_job_started(session_uuid")
		# The active-session check is the gate we want to be AFTER tracking.
		gate_idx = body.index("session.get_active_session_for(user)")
		assert pop_idx < track_idx < gate_idx, (
			"_track_bg_job_started must fire after the marker pop and before "
			"the active-session gate, so orphan jobs are still tracked"
		)

	def test_after_job_calls_track_bg_job_finished_in_finally(self):
		"""after_job's existing finally block — where clear_pending_job +
		set_job_recording already fire — must also call _track_bg_job_finished
		so the worker writes terminal status while the RQ record is still
		alive (covers the GC'd-record case)."""
		body = _fn_body(_src("hooks_callbacks.py"), "after_job")
		assert "_track_bg_job_finished(" in body, (
			"after_job must call _track_bg_job_finished in its finally so "
			"the terminal status is recorded while the worker is still running"
		)
		# Must be inside the same try block as clear_pending_job (read off
		# get_current_job().id once, write all bg-tracking from one site).
		clear_idx = body.index("session.clear_pending_job(")
		track_idx = body.index("_track_bg_job_finished(")
		# Tracking comes AFTER the recording link so we don't overwrite the
		# recording_uuid stash with our status-only write.
		recording_idx = body.index("session.set_job_recording(")
		assert clear_idx < recording_idx < track_idx, (
			"_track_bg_job_finished must come after set_job_recording so the recording_uuid link is preserved"
		)

	def test_before_job_stashes_bg_session_uuid_for_after_job(self):
		"""after_job needs to find the session even when the recorder was
		never activated (orphan case: ``active != session_uuid``). before_job
		stashes the bg-session uuid on frappe.local so after_job can fall
		back to it if optimus_session_id wasn't set."""
		body = _fn_body(_src("hooks_callbacks.py"), "before_job")
		assert "optimus_bg_session_uuid" in body, (
			"before_job must stash optimus_bg_session_uuid so after_job can "
			"track orphan jobs (active != session_uuid)"
		)

	def test_after_job_reads_bg_session_uuid_with_fallback(self):
		body = _fn_body(_src("hooks_callbacks.py"), "after_job")
		assert "optimus_bg_session_uuid" in body
		# Falls back to optimus_session_id (the existing stash) for the
		# normal happy-path job whose recorder did activate.
		assert "optimus_session_id" in body


# ---------------------------------------------------------------------------
# End-to-end: analyze persist uses what the hooks wrote
# ---------------------------------------------------------------------------
# Proves the WHOLE chain works: data written by hooks via set_job_status →
# read by session.get_jobs → persisted as Optimus Background Job rows. Uses
# the same FakeCache pattern as test_session_jobs.py.


class _FakeCache:
	def __init__(self):
		self._store = {}

	def get_value(self, key, **kw):
		return self._store.get(key)

	def set_value(self, key, val, **kw):
		self._store[key] = val

	def delete_value(self, key):
		self._store.pop(key, None)

	def hget(self, key, field):
		import json as _json

		h = self._store.get(key) or {}
		v = h.get(field)
		return _json.dumps(v) if v is not None else None

	def hset(self, key, field, val):
		h = self._store.setdefault(key, {})
		import json as _json

		h[field] = _json.loads(val) if isinstance(val, str) else val

	def hgetall(self, key):
		import json as _json

		h = self._store.get(key) or {}
		return {k: _json.dumps(v) for k, v in h.items()}


class TestEndToEndPersist:
	def test_hook_written_data_round_trips_via_session_get_jobs(self, monkeypatch):
		"""What the worker hooks write via set_job_status must surface through
		session.get_jobs in the shape analyze.py:1598-1617 expects (so the
		persisted Optimus Background Job row has Method/Status/Duration)."""
		import frappe

		from optimus import session

		cache = _FakeCache()
		monkeypatch.setattr(frappe, "cache", cache, raising=False)

		# Simulate the full hook lifecycle.
		session.record_job("S1", "J1", "myapp.tasks.foo")
		session.set_job_status("S1", "J1", status="Running", started_at="2026-05-24 15:20:00.000000")
		session.set_job_recording("S1", "J1", "REC-1")
		session.set_job_status(
			"S1",
			"J1",
			status="Completed",
			error=None,
			ended_at="2026-05-24 15:20:01.500000",
			duration_ms=1500.0,
		)

		jobs = session.get_jobs("S1")
		assert len(jobs) == 1
		j = jobs[0]
		assert j["job_id"] == "J1"
		assert j["method"] == "myapp.tasks.foo"
		assert j["status"] == "Completed"  # latest status wins (after_job)
		assert j["started_at"] == "2026-05-24 15:20:00.000000"
		assert j["ended_at"] == "2026-05-24 15:20:01.500000"
		assert j["duration_ms"] == 1500.0
		assert j["recording_uuid"] == "REC-1"
		# error=None must NOT clobber an existing (non-None) error — set_job_status
		# filters None values out (see session.set_job_status implementation).
		# Here error never got set to a string, so it's just absent.
		assert "error" not in j or j["error"] is None
