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


# ---------------------------------------------------------------------------
# _atomic_merge_job_meta — closes the multi-worker read-modify-write race.
# ---------------------------------------------------------------------------
# In multi-long-worker production, the previous read-modify-write (HGET +
# Python merge + HSET via two Redis calls) can drop fields when two workers
# update the SAME job_id's meta concurrently — e.g. Worker A's after_job
# writes recording_uuid while Worker B's analyze.run hits the wait cap and
# writes status="Running". Whoever writes second clobbers the other's
# modifications.
#
# The fix moves the merge server-side via a Redis Lua script (atomic). The
# wrapper falls back to the existing read-modify-write for cache backends
# that reject ``.eval()`` — exercised by these tests through FakeCache,
# which doesn't implement eval. The concurrent-write proof against real
# Redis (TestAtomicMergeJobMetaConcurrent below) is the regression test
# that the Lua path actually fires and is lossless.


class TestAtomicMergeJobMeta:
	"""Unit tests of _atomic_merge_job_meta via the fallback path (FakeCache
	has no .eval(), so the helper degrades to the legacy read-modify-write
	for these tests). The Lua-side semantics are verified by the threaded
	test against real Redis below."""

	def test_merge_preserves_existing_fields(self, fake_cache):
		"""Worker A's earlier writes (method, recording_uuid) must survive a
		later worker's set_job_status call. This is the race-safety contract."""
		session.record_job("s1", "job-1", "app.tasks.heavy")
		session.set_job_recording("s1", "job-1", "rec-abc")
		session._atomic_merge_job_meta("s1", "job-1", {"status": "Completed", "ended_at": "t"})
		j = session.get_jobs("s1")[0]
		assert j["method"] == "app.tasks.heavy"
		assert j["recording_uuid"] == "rec-abc"
		assert j["status"] == "Completed"
		assert j["ended_at"] == "t"

	def test_merge_filters_none_values(self, fake_cache):
		"""Passing error=None must NOT clobber an existing real error string.
		Callers pass None for absent error/duration_ms in the happy path."""
		session.record_job("s1", "job-1", "app.tasks.heavy")
		session.set_job_status("s1", "job-1", error="real error")
		session._atomic_merge_job_meta("s1", "job-1", {"status": "Completed", "error": None})
		j = session.get_jobs("s1")[0]
		assert j["status"] == "Completed"
		assert j["error"] == "real error"  # preserved

	def test_setdefault_preserves_first_write(self, fake_cache):
		"""record_job uses setdefault=True so the enqueue-time enqueued_at
		isn't clobbered by before_job's later record_job call. Verify."""
		session._atomic_merge_job_meta(
			"s1", "job-1", {"method": "first", "enqueued_at": "t1"}, setdefault=True
		)
		session._atomic_merge_job_meta(
			"s1", "job-1", {"method": "second", "enqueued_at": "t2"}, setdefault=True
		)
		j = session.get_jobs("s1")[0]
		assert j["method"] == "first"
		assert j["enqueued_at"] == "t1"

	def test_setdefault_writes_when_field_absent(self, fake_cache):
		"""First call with setdefault=True must actually write the fields
		(setdefault should NOT mean 'never write')."""
		session._atomic_merge_job_meta(
			"s1", "job-1", {"method": "foo", "enqueued_at": "t1"}, setdefault=True
		)
		j = session.get_jobs("s1")[0]
		assert j["method"] == "foo"
		assert j["enqueued_at"] == "t1"

	def test_fallback_when_eval_raises(self, fake_cache, monkeypatch):
		"""If frappe.cache.eval exists but raises (e.g. NoScriptError on a
		Redis variant), the helper must still complete via the non-atomic
		path — best-effort, never break the caller."""
		import frappe

		def boom(*a, **kw):
			raise RuntimeError("Lua disabled")

		# Inject eval onto the FakeCache so it exists but raises.
		monkeypatch.setattr(frappe.cache, "eval", boom, raising=False)

		session._atomic_merge_job_meta("s1", "job-1", {"status": "Completed"})
		j = session.get_jobs("s1")
		assert j and j[0]["status"] == "Completed"

	def test_empty_fields_is_safe(self, fake_cache):
		"""Empty / all-None fields → no-op, no exception."""
		session._atomic_merge_job_meta("s1", "job-1", {})
		session._atomic_merge_job_meta("s1", "job-1", {"error": None})
		assert session.get_jobs("s1") == []  # never wrote anything

	def test_empty_session_or_job_id_is_safe(self, fake_cache):
		"""Defensive: empty session_uuid or job_id silently no-ops."""
		session._atomic_merge_job_meta("", "job-1", {"status": "x"})
		session._atomic_merge_job_meta("s1", "", {"status": "x"})
		assert session.get_jobs("s1") == []


# ---------------------------------------------------------------------------
# Real-Redis threaded proof: the Lua path is lossless under contention.
# ---------------------------------------------------------------------------
# This is the regression test for the multi-worker race itself. It needs
# the bench's actual Redis up (with Lua scripting); skipped otherwise so
# it doesn't crash in plain-pytest contexts. With the pre-fix
# read-modify-write, 10 concurrent threaded writes routinely lose
# 1-3 fields; with the Lua path, all 10 land.


class TestAtomicMergeJobMetaConcurrent:
	"""Stand-in for the production multi-worker race using threads. In real
	RQ deployments each worker is a SEPARATE PROCESS with its own
	``frappe.local`` (Werkzeug Local is per-thread/process), so the full
	``_atomic_merge_job_meta`` wrapper — including ``make_key`` — works
	end-to-end in each worker. Threads can't replay that exactly because
	``frappe.local.conf`` isn't set in non-main threads; the test pre-computes
	the prefixed key in the main thread and calls the Lua script directly
	from each thread. That still proves the load-bearing invariant: Lua
	serialises HGET+HSET on Redis, so concurrent merges can't drop fields."""

	def test_concurrent_threaded_lua_merges_are_lossless(self):
		import json
		import threading

		import frappe

		# Gate: real Redis with Lua available?
		try:
			frappe.cache.ping()
		except Exception:
			pytest.skip("No bench Redis available — start with `bench start`")
		try:
			frappe.cache.eval("return 1", 0)
		except Exception:
			pytest.skip("Redis Lua eval not available on this backend")

		sid = "optimus-test-atomic-merge-concurrent"
		jid = "job-concurrent-test"
		prefixed = frappe.cache.make_key(session._jobs_key(sid))
		# Clean any prior run state so we measure a fresh merge.
		frappe.cache.delete_value(session._jobs_key(sid))
		try:
			N = 10

			def worker(i):
				# Direct Lua call (skip the SUT wrapper because make_key needs
				# frappe.local.conf which Werkzeug Local doesn't carry across
				# threads). The Lua INVARIANT is what we're proving.
				frappe.cache.eval(
					session._MERGE_JOB_META_LUA,
					1,
					prefixed,
					jid,
					json.dumps({f"field_{i}": f"value_{i}"}),
				)

			threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
			for t in threads:
				t.start()
			for t in threads:
				t.join()

			# Read via the SUT (which uses raw redis-py — bypasses the
			# RedisWrapper's pickle wrapper, matches the Lua write encoding).
			meta = session._read_job(sid, jid)
			assert meta is not None, "meta missing entirely — Lua write failed"
			missing = [f"field_{i}" for i in range(N) if meta.get(f"field_{i}") != f"value_{i}"]
			assert not missing, (
				f"{len(missing)}/{N} fields dropped under concurrent writes; "
				f"the Lua atomic-merge path isn't holding. Missing: {missing}. "
				f"Final meta keys: {sorted(meta.keys())}"
			)
		finally:
			frappe.cache.delete_value(session._jobs_key(sid))
