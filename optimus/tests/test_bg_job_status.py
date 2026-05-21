# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""analyze captures each enqueued RQ job's terminal status (Completed / Failed
/ Timeout / Stopped) from RQ, and marks jobs still active at the wait ceiling
as Running — so failed/timed-out jobs are reported instead of vanishing.
"""

import datetime

import pytest

pytest.importorskip("rq")

from optimus import analyze, session


class FakeJob:
	def __init__(self, status, exc_info=None, started_at=None, ended_at=None):
		self._status = status
		self.exc_info = exc_info
		self.started_at = started_at
		self.ended_at = ended_at

	def get_status(self, refresh=True):
		return self._status


@pytest.fixture
def cap(monkeypatch):
	captured = {}
	monkeypatch.setattr(session, "set_job_status",
		lambda su, jid, **kw: captured.__setitem__(jid, kw), raising=False)
	# get_redis_conn is imported inside the helper; stub it.
	import frappe.utils.background_jobs as bj
	monkeypatch.setattr(bj, "get_redis_conn", lambda: object(), raising=False)
	return captured


def _set_job(monkeypatch, job):
	import rq.job
	monkeypatch.setattr(rq.job.Job, "fetch",
		staticmethod(lambda jid, connection=None: job))


class TestCaptureTerminalStatus:
	def test_finished_is_completed(self, cap, monkeypatch):
		# RQ exposes timing as tz-aware UTC datetimes (rq.utils.utcparse).
		utc = datetime.timezone.utc
		start = datetime.datetime(2026, 5, 21, 12, 0, 0, tzinfo=utc)
		end = datetime.datetime(2026, 5, 21, 12, 0, 2, tzinfo=utc)
		_set_job(monkeypatch, FakeJob("finished", started_at=start, ended_at=end))
		analyze._capture_job_terminal_status("s1", "j1")
		assert cap["j1"]["status"] == "Completed"
		assert cap["j1"]["error"] is None
		assert cap["j1"]["duration_ms"] == 2000.0

	def test_tz_aware_timing_stored_without_offset(self, cap, monkeypatch):
		# Regression: RQ's started_at/ended_at are tz-aware UTC. str() of them
		# yields '...+00:00', which MariaDB's DATETIME column rejects (err 1292)
		# and crashed the whole report persist. The stored value must be a
		# naive 'YYYY-MM-DD HH:MM:SS.ffffff' string (no tz offset).
		utc = datetime.timezone.utc
		start = datetime.datetime(2026, 5, 21, 8, 0, 39, 714484, tzinfo=utc)
		end = datetime.datetime(2026, 5, 21, 8, 0, 40, 5323, tzinfo=utc)
		_set_job(monkeypatch, FakeJob("finished", started_at=start, ended_at=end))
		analyze._capture_job_terminal_status("s1", "j1")
		for key in ("started_at", "ended_at"):
			val = cap["j1"][key]
			assert "+" not in val and not val.endswith("Z"), f"{key} carries a tz offset: {val!r}"
			# MariaDB-safe: parses cleanly as a naive datetime.
			datetime.datetime.strptime(val, "%Y-%m-%d %H:%M:%S.%f")

	def test_failed_with_timeout_is_timeout(self, cap, monkeypatch):
		_set_job(monkeypatch, FakeJob(
			"failed",
			exc_info="Traceback...\nrq.timeouts.JobTimeoutException: Task exceeded maximum timeout value (180 seconds)",
		))
		analyze._capture_job_terminal_status("s1", "j1")
		assert cap["j1"]["status"] == "Timeout"
		assert "JobTimeoutException" in cap["j1"]["error"]

	def test_failed_other_is_failed(self, cap, monkeypatch):
		_set_job(monkeypatch, FakeJob("failed", exc_info="Traceback...\nValueError: bad doc_name"))
		analyze._capture_job_terminal_status("s1", "j1")
		assert cap["j1"]["status"] == "Failed"
		assert cap["j1"]["error"] == "ValueError: bad doc_name"

	def test_stopped_is_stopped(self, cap, monkeypatch):
		_set_job(monkeypatch, FakeJob("stopped"))
		analyze._capture_job_terminal_status("s1", "j1")
		assert cap["j1"]["status"] == "Stopped"

	def test_active_job_not_captured(self, cap, monkeypatch):
		# A still-active job must not be classified as terminal here.
		_set_job(monkeypatch, FakeJob("started"))
		analyze._capture_job_terminal_status("s1", "j1")
		assert "j1" not in cap


class TestFinalizePending:
	def test_active_marked_running_inactive_captured(self, cap, monkeypatch):
		# j-active stays active → Running; j-done is inactive → captured terminal.
		monkeypatch.setattr(analyze, "_rq_job_active", lambda jid: jid == "j-active")
		_set_job(monkeypatch, FakeJob("finished",
			started_at=datetime.datetime(2026, 5, 21, 12, 0, 0),
			ended_at=datetime.datetime(2026, 5, 21, 12, 0, 1)))
		analyze._finalize_pending_statuses("s1", ["j-active", "j-done"])
		assert cap["j-active"]["status"] == "Running"
		assert cap["j-done"]["status"] == "Completed"


def test_db_datetime_str_strips_tz_offset():
	# DB-write guard: MariaDB DATETIME rejects a tz offset. Strip it from values
	# a pre-fix build stored in Redis (str() of a tz-aware datetime); leave naive
	# strings (whose only '-' is inside the date) untouched.
	f = analyze._db_datetime_str
	assert f("2026-05-21 08:00:39.714484+00:00") == "2026-05-21 08:00:39.714484"
	assert f("2026-05-21 13:30:39.714484+05:30") == "2026-05-21 13:30:39.714484"
	assert f("2026-05-21 08:00:39.714484-05:00") == "2026-05-21 08:00:39.714484"
	assert f("2026-05-21 08:00:39.714484Z") == "2026-05-21 08:00:39.714484"
	# naive values pass through unchanged (no offset to strip)
	assert f("2026-05-21 13:30:39.714484") == "2026-05-21 13:30:39.714484"
	assert f("2026-05-21 13:30:39") == "2026-05-21 13:30:39"
	assert f(None) is None
	assert f("") is None


def test_short_exc_last_line():
	assert analyze._short_exc("a\n\nValueError: x\n") == "ValueError: x"
	assert analyze._short_exc("") is None
	assert analyze._short_exc(None) is None
