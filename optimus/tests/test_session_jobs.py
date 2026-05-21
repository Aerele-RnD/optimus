# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Per-job terminal-status tracking (v0.7.x).

A flow's enqueued RQ jobs are recorded in a never-pruned hash so analyze can
report each one's terminal status (completed / failed / timeout / running) —
including jobs that failed or timed out and produced no recording. Uses a fake
cache backend so we don't depend on Redis.
"""

import pytest

from optimus import session


class FakeCache:
	def __init__(self):
		self.store = {}

	def delete_value(self, key):
		self.store.pop(key, None)

	# Hash ops (the jobs metadata hash).
	def hset(self, key, field, value):
		self.store.setdefault(key, {})[field] = value

	def hget(self, key, field):
		return (self.store.get(key) or {}).get(field)

	def hgetall(self, key):
		return dict(self.store.get(key) or {})


@pytest.fixture
def fake_cache(monkeypatch):
	import frappe

	cache = FakeCache()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	# Deterministic, env-independent timestamp for record_job.
	import frappe.utils
	monkeypatch.setattr(frappe.utils, "now_datetime", lambda: "2026-05-21 12:00:00", raising=False)
	return cache


def test_record_job_then_get_jobs(fake_cache):
	session.record_job("s1", "job-1", "app.tasks.heavy")
	jobs = session.get_jobs("s1")
	assert len(jobs) == 1
	j = jobs[0]
	assert j["job_id"] == "job-1"
	assert j["method"] == "app.tasks.heavy"
	assert j["enqueued_at"] == "2026-05-21 12:00:00"


def test_record_job_does_not_clobber_status(fake_cache):
	session.record_job("s1", "job-1", "app.tasks.heavy")
	session.set_job_status("s1", "job-1", status="Completed", duration_ms=120.5)
	# A second record_job (e.g. a retry path) must not wipe the status.
	session.record_job("s1", "job-1", "app.tasks.heavy")
	j = session.get_jobs("s1")[0]
	assert j["status"] == "Completed"
	assert j["duration_ms"] == 120.5


def test_set_job_recording_and_status_merge(fake_cache):
	session.record_job("s1", "job-1", "app.tasks.heavy")
	session.set_job_recording("s1", "job-1", "rec-abc")
	session.set_job_status("s1", "job-1", status="Failed", error="boom")
	j = session.get_jobs("s1")[0]
	assert j["recording_uuid"] == "rec-abc"
	assert j["status"] == "Failed"
	assert j["error"] == "boom"
	assert j["method"] == "app.tasks.heavy"  # earlier fields preserved


def test_get_jobs_returns_all(fake_cache):
	session.record_job("s1", "job-1", "a")
	session.record_job("s1", "job-2", "b")
	ids = {j["job_id"] for j in session.get_jobs("s1")}
	assert ids == {"job-1", "job-2"}


def test_delete_session_state_clears_jobs(fake_cache):
	session.record_job("s1", "job-1", "a")
	session.delete_session_state("s1")
	assert session.get_jobs("s1") == []


def test_empty_session_uuid_is_safe(fake_cache):
	session.record_job("", "job-1", "a")
	session.set_job_status("s1", "", status="X")
	assert session.get_jobs("") == []
