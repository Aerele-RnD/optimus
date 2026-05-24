# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Redis state for the profiler session lifecycle.

This module is intentionally pure state-management — no business logic, no
DocType I/O, no recorder coupling. It owns three Redis key shapes:

    profiler:active:<user_email>          → string, value=session_uuid, TTL
    profiler:session:<uuid>:meta          → hash with {started_at, user, label}
    profiler:session:<uuid>:recordings    → set of recording UUIDs

The active key has a TTL that matches the recorder's auto-disable so a
forgotten Stop button can never run forever. The meta and recordings keys
have no TTL — they live until the analyze pipeline finalizes the session
into a `Optimus Session` DocType row and explicitly deletes them.
"""

import hashlib
import hmac
import json
import time

import frappe

# Match frappe.recorder.RECORDER_AUTO_DISABLE so a forgotten session
# auto-stops at the same point as the underlying recorder would.
SESSION_TTL_SECONDS = 10 * 60

# Hard cap on the number of recordings registered against a single session.
# Prevents pathological flows from filling Redis. Configurable per site
# via site_config.json: optimus_max_recordings_per_session
MAX_RECORDINGS_PER_SESSION = 200

# Phase K hardening: every pickled pyinstrument tree we stash in
# Redis is preceded by a 32-byte HMAC-SHA256 signature computed with
# the site's encryption_key. ``unsign_blob`` rejects any blob whose
# signature doesn't match - so a Redis-poisoning attacker can't slip
# a malicious pickle in and trigger a deserialization RCE.
_SIG_LEN = 32  # SHA-256 digest size in bytes.


def _has_stable_hmac_secret() -> bool:
	"""Phase K v0.7 GA: True only when ``frappe.conf.encryption_key``
	is set. That's the only secret guaranteed to be the same across
	HTTP / RQ / analyze workers on the same site. When False, callers
	skip signing (and accept unsigned blobs on read) - signing with
	the per-process random fallback produces blobs no other process
	can verify, which breaks the recorder → analyze handoff silently.
	"""
	try:
		return bool(frappe.conf and frappe.conf.get("encryption_key"))
	except Exception:
		return False


def _hmac_secret() -> bytes:
	"""Site-local secret for blob signing. Uses ``encryption_key`` if
	available (the same secret Frappe uses to encrypt password fields,
	rotated only on conscious operator action). Falls back to a
	process-local random key in test environments where
	``frappe.conf`` isn't wired up - signed blobs are then valid only
	within the current process, which matches the test isolation
	model. Callers that need cross-process round-trip should check
	``_has_stable_hmac_secret`` first."""
	try:
		key = frappe.conf.get("encryption_key") if frappe.conf else None
	except Exception:
		key = None
	if not key:
		# Test / unconfigured environment - synthesise a per-process
		# secret so sign/verify still round-trips within one worker.
		global _FALLBACK_SECRET
		try:
			return _FALLBACK_SECRET  # type: ignore[name-defined]
		except NameError:
			import os
			_FALLBACK_SECRET = os.urandom(32)
			return _FALLBACK_SECRET
	return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def sign_blob(blob: bytes) -> bytes:
	"""Prepend an HMAC-SHA256 signature so ``unsign_blob`` can verify
	integrity on read. Use for any opaque payload (pickle / msgpack /
	binary) that we'll later trust enough to deserialize.

	Phase K v0.7 GA: skip signing entirely when there's no stable
	shared secret - returning the raw blob means the unsigned-blob
	fallback path on read handles it cleanly. Signing with a
	per-process random key would produce blobs that fail HMAC
	verification in any other process and break the recorder →
	analyze handoff.
	"""
	if not isinstance(blob, (bytes, bytearray)):
		raise TypeError(f"sign_blob expects bytes, got {type(blob).__name__}")
	if not _has_stable_hmac_secret():
		return bytes(blob)
	sig = hmac.new(_hmac_secret(), bytes(blob), hashlib.sha256).digest()
	return sig + bytes(blob)


def unsign_blob(signed: bytes) -> bytes | None:
	"""Verify the HMAC-SHA256 prefix and return the payload, or
	``None`` if the signature is missing / mismatched. Constant-time
	comparison via ``hmac.compare_digest`` so signature-stripping
	attacks don't leak timing info."""
	if not isinstance(signed, (bytes, bytearray)) or len(signed) < _SIG_LEN:
		return None
	sig, blob = bytes(signed[:_SIG_LEN]), bytes(signed[_SIG_LEN:])
	expected = hmac.new(_hmac_secret(), blob, hashlib.sha256).digest()
	if not hmac.compare_digest(sig, expected):
		return None
	return blob


def _active_key(user: str) -> str:
	return f"profiler:active:{user}"


def _meta_key(session_uuid: str) -> str:
	return f"profiler:session:{session_uuid}:meta"


def _recordings_key(session_uuid: str) -> str:
	return f"profiler:session:{session_uuid}:recordings"


def _pending_jobs_key(session_uuid: str) -> str:
	return f"profiler:session:{session_uuid}:pending_jobs"


def _jobs_key(session_uuid: str) -> str:
	# v0.7.x: per-job metadata hash (job_id -> JSON). Distinct from the
	# pending-jobs SET, which is pruned as jobs go inactive to drive the wait;
	# this hash is the NEVER-pruned full record so analyze can report every
	# enqueued job's terminal status (completed / failed / timeout / running),
	# including jobs that failed or timed out and produced no recording.
	return f"profiler:session:{session_uuid}:jobs"


# ----- active session pointer (per-user) -----------------------------------


def get_active_session_for(user: str) -> str | None:
	"""Return the active profiler session UUID for the given user, or None."""
	if not user or user == "Guest":
		return None
	value = frappe.cache.get_value(_active_key(user))
	if isinstance(value, bytes):
		return value.decode()
	return value


def set_active_session(user: str, session_uuid: str) -> None:
	"""Mark the user as currently recording into the given session.

	The active key carries a TTL so a forgotten Stop button auto-clears.
	"""
	frappe.cache.set_value(
		_active_key(user),
		session_uuid,
		expires_in_sec=SESSION_TTL_SECONDS,
	)


def clear_active_session(user: str) -> None:
	"""Clear the active session pointer for the user.

	Idempotent — safe to call when no session is active.
	"""
	frappe.cache.delete_value(_active_key(user))


# ----- session metadata ----------------------------------------------------


def set_session_meta(session_uuid: str, meta: dict) -> None:
	"""Store session metadata.

	Recognized keys (consumers may add more, but these are the canonical):
	  - session_uuid, docname, user, label, started_at  (set by api.start)
	  - cap_warning                                     (set by register_recording)
	  - capture_python_tree (bool)                      (v0.3.0+, set by api.start)

	The v0.3.0 capture_python_tree flag is read by hooks_callbacks
	before_request/before_job to decide whether to set
	frappe.local._profiler_active_session_id. When False, the new
	pyinstrument capture and sidecar wraps stay inert; SQL recording
	via frappe.recorder proceeds as usual.
	"""
	frappe.cache.set_value(_meta_key(session_uuid), meta)


def get_session_meta(session_uuid: str) -> dict | None:
	return frappe.cache.get_value(_meta_key(session_uuid))


# ----- session → recording UUIDs (set, append-only during recording) ------


def register_recording(
	session_uuid: str,
	recording_uuid: str,
	user: str | None = None,
) -> bool:
	"""Append a recording UUID to the session's set of recordings.

	Atomic via Redis SADD. Safe to call from multiple workers concurrently.

	Enforces MAX_RECORDINGS_PER_SESSION as a soft cap: if the cap is hit,
	the new recording is dropped and a flag is set on the session meta so
	the analyze pipeline can surface a warning to the customer. Returns
	True if registered, False if capped.

	Also refreshes the user's active-session TTL (see Round 2 fix #2):
	without this refresh, a long flow (e.g. 45 minutes of profiling)
	would silently stop at the 10-minute TTL boundary because the
	profiler:active:<user> key expired. By bumping the TTL on every
	register_recording, an actively-used session stays alive as long as
	there's traffic. If the user stops making requests, the key expires
	naturally 10 minutes later and the janitor cleans up.
	"""
	import frappe

	cap = frappe.conf.get("optimus_max_recordings_per_session") or MAX_RECORDINGS_PER_SESSION

	if recording_count(session_uuid) >= cap:
		# Set a one-time warning flag on the session meta
		meta = get_session_meta(session_uuid) or {}
		if not meta.get("cap_warning"):
			meta["cap_warning"] = (
				f"Hit the session recording cap ({cap}). "
				"Some recordings were dropped. Restart with a shorter flow."
			)
			set_session_meta(session_uuid, meta)
		return False

	frappe.cache.sadd(_recordings_key(session_uuid), recording_uuid)

	# Refresh the active-session TTL so long flows don't silently expire.
	# If the caller didn't pass a user, fall back to reading it from the
	# session meta — one extra Redis roundtrip in exchange for a safer
	# default.
	if not user:
		meta = get_session_meta(session_uuid) or {}
		user = meta.get("user")
	if user:
		# v0.7.x: use Redis EXPIRE (no-op when the key has been
		# deleted) instead of SET, which would re-create a pointer
		# that the user just cleared via Stop. The pre-v0.7 bug:
		# an in-flight request whose ``after_request`` fired *just
		# after* Stop would re-create the active pointer with this
		# call, causing subsequent HTTP requests on the same worker
		# to keep being recorded into the (now-stopped) session and
		# the widget to silently flip back to Recording state.
		# EXPIRE returns 0 for a missing key — no key resurrected.
		frappe.cache.expire_key(_active_key(user), SESSION_TTL_SECONDS)

	return True


def get_recordings(session_uuid: str) -> list[str]:
	"""Return all recording UUIDs that belong to this session."""
	members = frappe.cache.smembers(_recordings_key(session_uuid)) or set()
	return sorted(m.decode() if isinstance(m, bytes) else m for m in members)


def recording_count(session_uuid: str) -> int:
	"""Return the count of recordings registered to this session."""
	return len(get_recordings(session_uuid))


# ----- background jobs the flow enqueued (v0.6.0) --------------------------
# When a profiled flow calls frappe.enqueue, the __init__.py monkey-patch
# registers the returned RQ job id here. analyze.run waits (capped) for these
# to finish before gathering recordings, and before_job keeps recording them
# even after Stop (see `draining_until` below) so they aren't lost.


def register_pending_job(session_uuid: str, job_id: str) -> None:
	"""Record that the flow enqueued RQ job ``job_id``. Best-effort."""
	if not session_uuid or not job_id:
		return
	try:
		frappe.cache.sadd(_pending_jobs_key(session_uuid), job_id)
	except Exception:
		pass


def clear_pending_job(session_uuid: str, job_id: str) -> None:
	"""Drop a finished/expired job id from the pending set. Best-effort."""
	if not session_uuid or not job_id:
		return
	try:
		frappe.cache.srem(_pending_jobs_key(session_uuid), job_id)
	except Exception:
		pass


def get_pending_jobs(session_uuid: str) -> set[str]:
	"""Return the set of RQ job ids the flow enqueued (and that haven't been
	cleared as finished). Empty set if none / on any error."""
	if not session_uuid:
		return set()
	try:
		members = frappe.cache.smembers(_pending_jobs_key(session_uuid)) or set()
	except Exception:
		return set()
	return {m.decode() if isinstance(m, bytes) else m for m in members}


# ----- per-job terminal-status tracking (v0.7.x) ---------------------------
# A flow's enqueued jobs are tracked in two places: the pending-jobs SET above
# (pruned as jobs finish, drives analyze's wait) and the jobs HASH below
# (job_id -> JSON metadata, never pruned until session cleanup). analyze reads
# the hash to persist one Optimus Background Job row per enqueued job with its
# terminal status, so failed / timed-out jobs are reported instead of vanishing.
#
# Multi-worker safety (v0.7.x+): updates go through ``_atomic_merge_job_meta``,
# which uses a Redis Lua script to do HGET → decode → merge → encode → HSET in
# a single server-side step. Without that, two workers updating the same
# job_id's meta concurrently (e.g. after_job's ``set_job_recording`` racing
# analyze's ``_finalize_pending_statuses`` at the wait cap) can DROP fields
# via interleaved read-modify-write. The helper falls back to non-atomic
# read-modify-write for cache backends without ``.eval()`` (FakeCache in
# tests, exotic Redis variants) — preserves existing behavior, sheds the
# multi-worker guarantee in those contexts.


# Lua scripts. ``cjson`` ships with Redis's standard Lua sandbox; no extra
# setup needed. KEYS[1] = jobs hash key (pre-prefixed via make_key), ARGV[1]
# = job_id field, ARGV[2] = JSON-encoded dict of fields to merge.
_MERGE_JOB_META_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
local meta = {}
if current then meta = cjson.decode(current) end
local new_fields = cjson.decode(ARGV[2])
for k, v in pairs(new_fields) do meta[k] = v end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(meta))
return 1
"""

_SETDEFAULT_JOB_META_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
local meta = {}
if current then meta = cjson.decode(current) end
local new_fields = cjson.decode(ARGV[2])
for k, v in pairs(new_fields) do
    if meta[k] == nil or meta[k] == '' then meta[k] = v end
end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(meta))
return 1
"""


# Frappe's RedisWrapper.hget/hset wrap values in pickle.dumps / pickle.loads.
# Lua can't do pickle, so the Lua merge script stores plain JSON bytes; we
# need to read/write the bg-jobs hash on the SAME byte format. ``_raw_redis``
# returns a redis-py client that shares frappe.cache's connection pool but
# bypasses the pickle wrapper. Returns None in test contexts where
# ``frappe.cache`` is a FakeCache stand-in (no connection_pool); _read_job /
# _write_job then fall back to ``frappe.cache.hget`` / ``.hset`` directly,
# which is fine because FakeCache stores values as-is (no pickling) — the
# encoding stays consistent within either environment.
_RAW_REDIS = None


def _raw_redis():
	global _RAW_REDIS
	if _RAW_REDIS is not None:
		return _RAW_REDIS
	try:
		import redis

		pool = getattr(frappe.cache, "connection_pool", None)
		if pool is None:
			return None
		_RAW_REDIS = redis.Redis(connection_pool=pool)
		return _RAW_REDIS
	except Exception:
		return None


def _read_job(session_uuid: str, job_id: str) -> dict | None:
	try:
		r = _raw_redis()
		if r is not None:
			prefixed = frappe.cache.make_key(_jobs_key(session_uuid))
			raw = r.hget(prefixed, job_id)
		else:
			raw = frappe.cache.hget(_jobs_key(session_uuid), job_id)
	except Exception:
		return None
	if not raw:
		return None
	if isinstance(raw, bytes):
		raw = raw.decode()
	try:
		return json.loads(raw)
	except Exception:
		return None


def _write_job(session_uuid: str, job_id: str, meta: dict) -> None:
	try:
		r = _raw_redis()
		if r is not None:
			prefixed = frappe.cache.make_key(_jobs_key(session_uuid))
			r.hset(prefixed, job_id, json.dumps(meta))
		else:
			frappe.cache.hset(_jobs_key(session_uuid), job_id, json.dumps(meta))
	except Exception:
		pass


def _atomic_merge_job_meta(
	session_uuid: str, job_id: str, fields: dict, *, setdefault: bool = False
) -> None:
	"""Atomic Redis-side merge of fields onto the per-job meta hash field.
	Closes the multi-worker read-modify-write race that drops fields when
	two workers update the same job_id's meta concurrently.

	``setdefault=True`` preserves any existing value for each field (used by
	``record_job`` so before_job's later call doesn't clobber the original
	enqueue timestamp).

	Filters None values so callers can pass ``error=None`` without nuking a
	real error already in meta. Falls back to the legacy non-atomic
	read-modify-write on any backend that rejects ``.eval()`` (FakeCache in
	tests, exotic Redis variants) — best-effort, never raises."""
	if not session_uuid or not job_id:
		return
	fields = {k: v for k, v in fields.items() if v is not None}
	if not fields:
		return
	script = _SETDEFAULT_JOB_META_LUA if setdefault else _MERGE_JOB_META_LUA
	try:
		# Frappe's RedisWrapper prefixes every key with ``<db_name>|`` via
		# ``make_key`` in its overridden hset / hget methods. ``.eval`` is
		# inherited from redis-py and does NOT prefix automatically, so the
		# script would write to an unprefixed key that ``_read_job`` /
		# ``_write_job`` can never find. Pre-prefix here so Lua and the
		# fallback path target the same Redis key.
		prefixed = frappe.cache.make_key(_jobs_key(session_uuid))
		frappe.cache.eval(script, 1, prefixed, job_id, json.dumps(fields))
		return
	except Exception:
		# Fall through to the non-atomic path. Loses the multi-worker safety
		# guarantee, but preserves existing behavior on backends without Lua
		# (FakeCache in tests, any Redis variant that rejects scripting).
		pass
	meta = _read_job(session_uuid, job_id) or {}
	if setdefault:
		for k, v in fields.items():
			if not meta.get(k):  # treats empty string + None as "absent"
				meta[k] = v
	else:
		meta.update(fields)
	_write_job(session_uuid, job_id, meta)


def record_job(session_uuid: str, job_id: str, method: str) -> None:
	"""Record an enqueued RQ job's identity + method so analyze can report its
	terminal status later. Idempotent: a later call (e.g. before_job's
	defensive re-record after the enqueue patch) won't clobber the original
	method or enqueued_at. Best-effort."""
	if not session_uuid or not job_id:
		return
	try:
		from frappe.utils import now_datetime

		enqueued_at = str(now_datetime())
	except Exception:
		enqueued_at = None
	fields = {"method": method or "", "enqueued_at": enqueued_at}
	_atomic_merge_job_meta(session_uuid, job_id, fields, setdefault=True)


def set_job_recording(session_uuid: str, job_id: str, recording_uuid: str) -> None:
	"""Link a job to the recording it produced (so the report can join to the
	captured query data). Best-effort."""
	if not session_uuid or not job_id or not recording_uuid:
		return
	_atomic_merge_job_meta(session_uuid, job_id, {"recording_uuid": recording_uuid})


def set_job_status(session_uuid: str, job_id: str, **fields) -> None:
	"""Merge terminal-status fields (status, error, started_at, ended_at,
	duration_ms, …) onto a tracked job. Best-effort."""
	_atomic_merge_job_meta(session_uuid, job_id, fields)


def get_jobs(session_uuid: str) -> list[dict]:
	"""Return every tracked job as a dict (each carries ``job_id``). Empty list
	if none / on any error."""
	if not session_uuid:
		return []
	try:
		r = _raw_redis()
		if r is not None:
			prefixed = frappe.cache.make_key(_jobs_key(session_uuid))
			raw = r.hgetall(prefixed) or {}
		else:
			raw = frappe.cache.hgetall(_jobs_key(session_uuid)) or {}
	except Exception:
		return []
	jobs: list[dict] = []
	for k, v in raw.items():
		jid = k.decode() if isinstance(k, bytes) else k
		if isinstance(v, bytes):
			v = v.decode()
		try:
			meta = json.loads(v)
		except Exception:
			meta = {}
		meta["job_id"] = jid
		jobs.append(meta)
	return jobs


# ----- post-Stop "draining" window (v0.6.0) --------------------------------
# Stop clears the active-session pointer immediately (so the UI shows
# "stopped/analyzing"), but a draining deadline on the session keeps before_job
# accepting recordings until the flow's background jobs finish (capped).


def set_draining(session_uuid: str, until_ts: float) -> None:
	"""Keep accepting job recordings for this session until ``until_ts``
	(a unix timestamp). Stored on the session meta dict (no separate TTL —
	meta lives until the analyze pipeline deletes it)."""
	if not session_uuid:
		return
	meta = get_session_meta(session_uuid) or {}
	try:
		meta["draining_until"] = float(until_ts)
	except (TypeError, ValueError):
		return
	set_session_meta(session_uuid, meta)


def is_draining(session_uuid: str) -> bool:
	"""True while the session is in its post-Stop draining window."""
	if not session_uuid:
		return False
	meta = get_session_meta(session_uuid) or {}
	until = meta.get("draining_until")
	if not until:
		return False
	try:
		return time.time() < float(until)
	except (TypeError, ValueError):
		return False


# ----- cleanup -------------------------------------------------------------


def delete_session_state(session_uuid: str) -> None:
	"""Delete all Redis state for a finalized session.

	Called by the analyze pipeline once the session has been persisted to
	the `Optimus Session` DocType. Idempotent.
	"""
	frappe.cache.delete_value(_meta_key(session_uuid))
	frappe.cache.delete_value(_recordings_key(session_uuid))
	# v0.6.0: pending-jobs set (the draining_until flag lives inside the
	# meta hash, deleted above).
	frappe.cache.delete_value(_pending_jobs_key(session_uuid))
	# v0.7.x: per-job metadata hash (terminal statuses) — persisted to the
	# Optimus Background Job child table by analyze before this runs.
	frappe.cache.delete_value(_jobs_key(session_uuid))
	# v0.5.0: clean up the frontend metrics Redis lists written by
	# api.submit_frontend_metrics. Pre-v0.5.1 used a single JSON dict
	# at profiler:frontend:<uuid> (deleted below for forward compat with
	# sessions created just before this upgrade). v0.5.1+ uses two
	# atomic Redis lists to avoid a GET-merge-SET race.
	#
	# Per-recording infra keys (profiler:infra:<recording_uuid>) are
	# cleaned up alongside RECORDER_REQUEST_HASH entries when analyze
	# walks the recording UUIDs, so no separate sweep here.
	frappe.cache.delete_value(f"profiler:frontend:{session_uuid}")
	frappe.cache.delete_value(f"profiler:frontend:{session_uuid}:xhr")
	frappe.cache.delete_value(f"profiler:frontend:{session_uuid}:vitals")
