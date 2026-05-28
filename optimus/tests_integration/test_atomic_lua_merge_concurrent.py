# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for the v0.7.x atomic-Lua merge contract
on ``profiler:session:<uuid>:jobs``.

The v0.7.x bg-tracking trilogy (``a356f64`` → ``0e4a270`` → ``f30f44e``)
closed a multi-worker field-loss race. Pre-trilogy, two workers writing
to the same job_id's per-field dict could lose fields under a
read-modify-write race: Worker A reads meta, modifies, writes; Worker B
in the interleave reads the pre-A meta, modifies, writes — and Worker A's
change is gone.

The fix moves the merge SERVER-SIDE via :data:`_MERGE_JOB_META_LUA` (and
its setdefault sibling), atomic in Redis. This test proves the
invariant under genuine concurrent thread contention against real Redis +
real Lua.

A unit-suite test exists for this in ``optimus/tests/test_session_jobs.py``
but it ``pytest.skip``s when Redis or Lua isn't reachable — under the
pure-pytest workflow, that's every run. The integration version is the
first-class CI gate: real Redis + real Lua, always runs, richer
scenarios.

**The thread-vs-process caveat (same as the unit version):** an RQ
worker is a separate PROCESS with its own ``frappe.local`` set up by
the bench. Threads in this test can't replicate that because
``frappe.local.conf`` is per-thread/process and isn't initialised in
non-main Python threads. The mitigation (mirroring the unit test): pre-
compute the prefixed Redis key in the main thread, then have worker
threads call ``frappe.cache.eval(_MERGE_JOB_META_LUA, …)`` directly.
That preserves the load-bearing invariant — Redis Lua serialisation
of HGET + JSON-merge + HSET — which is exactly what the trilogy
protects.

For the fallback path (Python read-modify-write when Lua isn't
available), one test runs entirely in the main thread so the full
``_atomic_merge_job_meta`` wrapper code path is exercised end-to-end.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from optimus import redis_keys, session


def _require_redis_and_lua():
	"""Return ``None`` when Redis + Lua are available, else the
	``self.skipTest`` reason string. Defence-in-depth: the CI workflow
	guarantees both, but a local developer running this against a
	Redis-mock might not."""
	try:
		frappe.cache.ping()
	except Exception:
		return "No bench Redis available — start with `bench start`"
	try:
		frappe.cache.eval("return 1", 0)
	except Exception:
		return "Redis Lua eval not available on this backend"
	return None


def _purge_jobs_key(session_uuid: str) -> None:
	"""Delete the test's session jobs hash; tolerate the key not
	existing. Called from setUp + tearDown for belt-and-suspenders
	cleanup (the autouse ``cleanup_session`` fixture handles Optimus
	Session DocType rows; the jobs hash lives in Redis under a
	test-only fixture UUID that the autouse fixture won't touch)."""
	try:
		frappe.cache.delete_value(redis_keys.session_jobs(session_uuid))
	except Exception:
		pass


class TestAtomicLuaMergeConcurrent(FrappeTestCase):
	"""Five tests covering the v0.7.x trilogy's invariants under real
	Redis + Lua + threading. Each test owns its own ``session_uuid``
	to avoid cross-test pollution; the jobs hash is purged in
	setUp/tearDown."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Admin user so any frappe-internal permission gate (e.g.
		# rate limiters, cache helpers that read user context) doesn't
		# refuse mid-test.
		frappe.set_user("Administrator")

	def setUp(self):
		super().setUp()
		# A per-test fixture UUID so concurrent runs (or a re-run after
		# a previous flake) can't collide.
		self._session_uuid = f"optimus-int-merge-{frappe.generate_hash(length=10)}"
		_purge_jobs_key(self._session_uuid)

	def tearDown(self):
		_purge_jobs_key(self._session_uuid)
		super().tearDown()

	# ----------------------------------------------------------------
	# 1. The exact v0.7.x race — pair threads writing recording_uuid +
	#    status to the same job_id, looped across 50 distinct job_ids.
	#    This is the canonical regression test.
	# ----------------------------------------------------------------

	def test_recording_uuid_and_status_race_is_lossless(self):
		skip = _require_redis_and_lua()
		if skip:
			self.skipTest(skip)

		sid = self._session_uuid
		prefixed = frappe.cache.make_key(redis_keys.session_jobs(sid))
		N_JOBS = 50

		def write_recording(job_id, recording_uuid):
			frappe.cache.eval(
				session._MERGE_JOB_META_LUA,
				1,
				prefixed,
				job_id,
				json.dumps({"recording_uuid": recording_uuid}),
			)

		def write_status(job_id, status):
			frappe.cache.eval(
				session._MERGE_JOB_META_LUA,
				1,
				prefixed,
				job_id,
				json.dumps({"status": status}),
			)

		# Use a Barrier so all threads release at the exact same moment —
		# maximises the race-window overlap. Without it, t.start() loops
		# can sequence threads on a fast runner.
		barrier = threading.Barrier(2 * N_JOBS)
		threads: list[threading.Thread] = []
		for i in range(N_JOBS):
			jid = f"job-{i}"
			rec_uuid = f"rec-{i}"

			def _rec(jid=jid, rec_uuid=rec_uuid):
				barrier.wait()
				write_recording(jid, rec_uuid)

			def _stat(jid=jid):
				barrier.wait()
				write_status(jid, "Running")

			threads.append(threading.Thread(target=_rec))
			threads.append(threading.Thread(target=_stat))

		for t in threads:
			t.start()
		for t in threads:
			t.join()

		# Every job_id should have BOTH fields present.
		missing: list[str] = []
		for i in range(N_JOBS):
			jid = f"job-{i}"
			meta = session._read_job(sid, jid)
			if not meta:
				missing.append(f"{jid}: meta absent")
				continue
			if meta.get("recording_uuid") != f"rec-{i}":
				missing.append(f"{jid}: recording_uuid={meta.get('recording_uuid')!r}")
			if meta.get("status") != "Running":
				missing.append(f"{jid}: status={meta.get('status')!r}")
		assert not missing, (
			f"Field loss under concurrent recording_uuid+status writes; "
			f"the Lua atomic-merge path isn't holding. "
			f"{len(missing)}/{2 * N_JOBS} writes corrupted:\n  "
			+ "\n  ".join(missing[:20])
		)

	# ----------------------------------------------------------------
	# 2. Distinct job_ids — N threads, N distinct hash fields. The
	#    per-field cjson encode within ONE Lua script should isolate
	#    each thread's write.
	# ----------------------------------------------------------------

	def test_concurrent_distinct_job_ids_dont_clobber(self):
		skip = _require_redis_and_lua()
		if skip:
			self.skipTest(skip)

		sid = self._session_uuid
		prefixed = frappe.cache.make_key(redis_keys.session_jobs(sid))
		N = 20
		barrier = threading.Barrier(N)

		def worker(i):
			barrier.wait()
			frappe.cache.eval(
				session._MERGE_JOB_META_LUA,
				1,
				prefixed,
				f"job-{i}",
				json.dumps({"recording_uuid": f"rec-{i}", "status": "Completed"}),
			)

		threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()

		missing = []
		for i in range(N):
			meta = session._read_job(sid, f"job-{i}")
			if not meta or meta.get("recording_uuid") != f"rec-{i}":
				missing.append(f"job-{i}: meta={meta!r}")
		assert not missing, (
			f"{len(missing)}/{N} distinct-job-id writes lost — "
			f"per-field cjson isolation broken. Missing: {missing!r}"
		)

	# ----------------------------------------------------------------
	# 3. setdefault — two threads race to set ``method``; the second
	#    writer's value MUST NOT clobber the first's. The trilogy's
	#    _SETDEFAULT_JOB_META_LUA is what protects this on the enqueue
	#    path (multiple callers can race ``record_job`` if the same
	#    job somehow re-enters the queue).
	# ----------------------------------------------------------------

	def test_setdefault_first_writer_wins(self):
		skip = _require_redis_and_lua()
		if skip:
			self.skipTest(skip)

		sid = self._session_uuid
		prefixed = frappe.cache.make_key(redis_keys.session_jobs(sid))
		jid = "job-setdefault-race"

		# Pre-seed with a known method so we can detect whether a racing
		# writer overwrites it.
		frappe.cache.eval(
			session._SETDEFAULT_JOB_META_LUA,
			1,
			prefixed,
			jid,
			json.dumps({"method": "first.writer"}),
		)

		# Now race many threads — each tries setdefault with a different
		# method. None should win; the original "first.writer" stays.
		N = 20
		barrier = threading.Barrier(N)

		def worker(i):
			barrier.wait()
			frappe.cache.eval(
				session._SETDEFAULT_JOB_META_LUA,
				1,
				prefixed,
				jid,
				json.dumps({"method": f"loser.{i}"}),
			)

		threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()

		meta = session._read_job(sid, jid)
		assert meta is not None, "meta absent after setdefault race"
		assert meta.get("method") == "first.writer", (
			f"setdefault clobbered the first writer's value; meta now: {meta!r}"
		)

	# ----------------------------------------------------------------
	# 4. Fallback path — when Lua eval raises, _atomic_merge_job_meta
	#    must still write via the Python read-modify-write fallback.
	#    Single-threaded so we can exercise the FULL wrapper (which
	#    needs frappe.local set up — only the main thread has it).
	# ----------------------------------------------------------------

	def test_fallback_path_writes_when_lua_unavailable(self):
		# The fallback path doesn't need real Lua; it just needs real
		# Redis to read/write the hash. Gate only on Redis.
		try:
			frappe.cache.ping()
		except Exception:
			self.skipTest("No bench Redis available")

		sid = self._session_uuid
		jid = "job-fallback"

		# Patch frappe.cache.eval to raise — the wrapper must catch and
		# fall through to _read_job → merge → _write_job.
		def _eval_raises(*args, **kwargs):
			raise RuntimeError("Lua eval disabled for fallback test")

		with patch.object(frappe.cache, "eval", side_effect=_eval_raises):
			session._atomic_merge_job_meta(
				sid, jid, {"recording_uuid": "rec-fallback", "status": "Completed"}
			)

		# Lua is restored; read back via the SUT.
		meta = session._read_job(sid, jid)
		assert meta is not None, "fallback didn't write the meta"
		assert meta.get("recording_uuid") == "rec-fallback"
		assert meta.get("status") == "Completed"

	# ----------------------------------------------------------------
	# 5. Sanity: with Lua disabled, _atomic_merge_job_meta doesn't
	#    raise — the wrapper catches the eval failure and falls
	#    through silently. Defensive lock-in for the contract that
	#    "atomic-merge must NEVER break the host code".
	# ----------------------------------------------------------------

	def test_atomic_merge_does_not_raise_when_lua_unavailable(self):
		try:
			frappe.cache.ping()
		except Exception:
			self.skipTest("No bench Redis available")

		sid = self._session_uuid
		jid = "job-no-raise"

		def _eval_raises(*args, **kwargs):
			raise RuntimeError("simulated Lua failure")

		with patch.object(frappe.cache, "eval", side_effect=_eval_raises):
			# Must not raise. The fallback handles it silently.
			session._atomic_merge_job_meta(sid, jid, {"status": "Running"})
		# And the field must have landed via the fallback.
		assert session._read_job(sid, jid).get("status") == "Running"
