# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Whitelisted HTTP API for the profiler.

These endpoints are how the floating widget (Phase 5) and any custom
integrations talk to the profiler. They are decorated with
`@frappe.whitelist()` so they are reachable as
`/api/method/optimus.api.<name>`.

Phase 1 surface:
    start(label)         — begin a session for the calling user
    stop()               — end the current user's session
    status()             — is the calling user currently recording?
    get_active_session() — full metadata for the calling user's session

The actual analyze pipeline (read recordings, run analyzers, render
report, attach files) is added in Phase 3+. For now, `stop()` simply
clears the active flag and marks the DocType as `Stopping` — a future
phase will hook the analyze step in.
"""

import json
import time

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

from optimus import safe_commit, session

# Roles allowed to call the profiler API. System Manager is always allowed
# (Frappe's superuser role); Optimus User is our dedicated role created
# on install via install.after_install. Adding Administrator explicitly
# because frappe.get_roles("Administrator") doesn't include "System Manager".
ALLOWED_ROLES = {"System Manager", "Optimus User", "Administrator"}


def _require_user() -> str:
	"""Return the calling user, or throw if Guest."""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw("You must be logged in to use the profiler.")
	return user


def _require_profiler_user() -> str:
	"""Return the calling user, or throw if they lack the profiler role.

	The HTTP-level enforcement that the floating widget's role check is
	mirroring. Without this, any authenticated user could POST to
	/api/method/optimus.api.start and start a session on
	themselves — the role check in the widget JS would be purely
	cosmetic.
	"""
	user = _require_user()
	if user == "Administrator":
		return user
	roles = set(frappe.get_roles(user))
	if not (ALLOWED_ROLES & roles):
		frappe.throw(
			"You need the Optimus User or System Manager role to use the profiler.",
			frappe.PermissionError,
		)
	return user


def _require_session_permission(session_uuid: str, permission_type: str = "read") -> str:
	"""Phase K hardening: per-doc permission gate on endpoints that
	read or mutate a specific ``Optimus Session``. The role check in
	``_require_profiler_user`` allows any Optimus User to call the
	endpoint; this further check ensures the caller actually has
	access to *this* session under Frappe's DocType permission model.

	Returns the resolved docname. Throws ``frappe.PermissionError``
	when:
	  - the ``session_uuid`` doesn't resolve to any row, OR
	  - ``frappe.has_permission`` denies access for ``permission_type``.

	Best-effort fallback: if ``has_permission`` raises (e.g. during a
	mid-migration upgrade where the DocType permission rules aren't
	loaded yet), log a warning and allow the call through - the
	prior ``_require_profiler_user`` role check still applies, so a
	non-Optimus-User can't reach this branch.
	"""
	if not session_uuid:
		frappe.throw("session_uuid is required", frappe.ValidationError)
	docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name"
	)
	if not docname:
		frappe.throw(
			f"No Optimus Session found for uuid {session_uuid}",
			frappe.DoesNotExistError,
		)
	try:
		allowed = frappe.has_permission("Optimus Session", permission_type, docname)
	except Exception:
		try:
			frappe.logger().warning(
				"optimus._require_session_permission: has_permission raised "
				f"for {docname} / {permission_type}; falling through to role-only gate"
			)
		except Exception:
			pass
		return docname
	if not allowed:
		frappe.throw(
			"You do not have permission to access this Optimus Session.",
			frappe.PermissionError,
		)
	return docname


@frappe.whitelist()
def start(
	label: str = "",
	capture_python_tree: bool = True,
	notes: str = "",
) -> dict:
	"""Begin a profiling session for the calling user.

	If the user already has an active session, that session is stopped
	first (its DocType row is marked `Stopping` and its Redis pointer
	cleared). This makes start() idempotent from the user's perspective:
	clicking Start twice never produces two parallel sessions.

	Args:
	    label: Human-readable session label.
	    capture_python_tree: v0.3.0+. When True (default), pyinstrument
	        captures a Python call tree per recording and sidecar wraps
	        capture frappe.get_doc / cache.get_value / has_permission
	        argument identities. When False, only the existing SQL
	        recorder runs — same overhead profile as v0.2.0.
	    notes: v0.5.0+. Free-form "steps to reproduce" / context text
	        rendered at the top of the report. Can also be edited on
	        the Optimus Session form after the session completes.
	"""
	user = _require_profiler_user()

	# v0.3.0: clear any in-flight capture state from a previous request
	# on this worker BEFORE we look at session state, so leaked state
	# from a concurrent request doesn't influence the new session.
	from optimus import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: mirror for infra capture.
	from optimus import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	# If the user is already recording, gracefully stop the previous one.
	previous = session.get_active_session_for(user)
	if previous:
		_stop_session(user, previous)

	session_uuid = frappe.generate_hash(length=16)
	now = now_datetime()
	title = (label or "").strip() or f"Profiling session @ {now.strftime('%Y-%m-%d %H:%M:%S')}"

	# Create the DocType row in Recording state.
	doc_fields = {
		"doctype": "Optimus Session",
		"session_uuid": session_uuid,
		"title": title,
		"user": user,
		"status": "Recording",
		"started_at": now,
	}
	# v0.5.0: persist steps-to-reproduce / notes captured at start time.
	notes_clean = (notes or "").strip()
	if notes_clean:
		doc_fields["notes"] = notes_clean
	doc = frappe.get_doc(doc_fields).insert(ignore_permissions=True)

	# Store metadata in Redis (used by the analyze pipeline later).
	session.set_session_meta(
		session_uuid,
		{
			"session_uuid": session_uuid,
			"docname": doc.name,
			"user": user,
			"label": title,
			"started_at": now.isoformat(),
			"capture_python_tree": bool(capture_python_tree),
		},
	)

	# Flip the user's active flag last — once this is set, the next
	# request from this user will start being recorded.
	session.set_active_session(user, session_uuid)

	return {
		"session_uuid": session_uuid,
		"docname": doc.name,
		"title": title,
		"started_at": now.isoformat(),
	}


@frappe.whitelist()
def stop() -> dict:
	"""End the calling user's active profiling session.

	Clears the Redis active pointer, marks the Optimus Session row as
	``Stopping``, and either enqueues the analyze job or runs it inline
	(v0.5.0: scheduler-aware fallback — see ``_enqueue_analyze``).

	Returns:
	    dict with ``stopped``, ``session_uuid``, ``docname``,
	    ``ran_inline``, and — only when ran_inline is True — the final
	    ``status`` from the Optimus Session row (``Ready`` or ``Failed``).

	    The widget uses both flags to decide its terminal state:
	      ran_inline=False          → transition to "Analyzing…"
	      ran_inline=True, Ready    → transition directly to "Report ready"
	      ran_inline=True, Failed   → transition to "Analyze failed"
	"""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"stopped": False, "reason": "no active session"}

	docname, ran_inline = _stop_session(user, active)

	# v0.5.0: when analyze runs inline, the session is already finalized
	# by the time we return. Read the actual status so the widget can
	# transition to the right terminal state — otherwise a failed
	# inline analyze would show "Report ready" to the user despite the
	# session being marked Failed server-side.
	final_status = None
	if ran_inline and docname:
		try:
			final_status = frappe.db.get_value(
				"Optimus Session", docname, "status"
			)
		except Exception:
			final_status = None

	return {
		"stopped": True,
		"session_uuid": active,
		"docname": docname,
		"ran_inline": ran_inline,
		"status": final_status,
	}


def _stop_session(user: str, session_uuid: str) -> tuple[str | None, bool]:
	"""Internal: clear the active pointer, mark the DocType as Stopping,
	and enqueue (or inline-run) the analyze job.

	Returns a tuple ``(docname, ran_inline)``:
	    docname     — the Optimus Session docname, or None if not found
	    ran_inline  — True if the session is already finalized by the time
	                  we return (analyze ran synchronously or was rejected
	                  by the inline cap and marked Failed). False means
	                  analyze will run async.
	"""
	# v0.3.0: stop any in-flight pyinstrument session and clear capture
	# state on this worker before flipping the active flag, so a previous
	# in-flight capture from the same worker doesn't leak into a new
	# session started immediately after.
	from optimus import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: clear any leaked infra start snapshot from a previous
	# session on the same worker.
	from optimus import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	_clear_active(user)
	docname = _mark_stopping(user, session_uuid)
	if not docname:
		return None, False

	# v0.5.1: notify any open widgets on this user's session that
	# we're transitioning out of Recording. Second, third, fourth
	# tabs will all switch their displays simultaneously without
	# polling. The stop-click itself runs on ONE tab; the others
	# learn about the stop via this event.
	_publish_session_event(
		"optimus_session_stopping",
		session_uuid=session_uuid,
		docname=docname,
		user=user,
	)

	# v0.6.0: if the flow enqueued background jobs, keep the session
	# accepting their recordings for a bounded window after Stop (the
	# active pointer was just cleared). analyze.run waits for these jobs
	# to finish before gathering recordings, so they aren't lost. The
	# draining deadline is the analyze wait + a grace margin covering the
	# analyze run itself. No-op when the wait is disabled (=0) or nothing
	# was enqueued.
	try:
		from optimus.settings import get_config

		wait_seconds = int(getattr(get_config(), "background_job_wait_seconds", 0) or 0)
		if wait_seconds > 0 and session.get_pending_jobs(session_uuid):
			session.set_draining(session_uuid, time.time() + wait_seconds + 60)
	except Exception:
		frappe.log_error(title="optimus set draining window")

	# v0.5.0: inline safety cap + scheduler fallback are both inside
	# _enqueue_analyze now, so every inline-path caller (stop,
	# retry_analyze, janitor) gets the same protection uniformly.
	ran_inline = _enqueue_analyze(session_uuid, docname=docname)
	return docname, ran_inline


def _publish_session_event(
	event_name: str,
	*,
	session_uuid: str,
	docname: str | None,
	user: str,
	**extra,
) -> None:
	"""Publish a session-state realtime event to ALL the user's Desk
	tabs via Frappe's Socket.IO bridge.

	Used to drive the floating widget state machine without HTTP
	polling. The widget in floating_widget.js subscribes to these
	event names via ``frappe.realtime.on(...)``:

	  optimus_session_stopping    — user clicked Stop
	  optimus_session_analyzing   — analyze.run starting (may be delayed)
	  optimus_session_ready       — analyze finished successfully
	  optimus_session_failed      — analyze crashed

	All events carry ``session_uuid`` and ``docname`` so a widget with
	multiple open tabs can match on the session it's currently tracking.

	Best-effort: publish failures log but never interrupt the caller's
	business logic. Frappe's publish_realtime already swallows most
	errors but we double-wrap in case the Socket.IO bridge is down
	(dev environment without a running redis-socketio)."""
	payload = {"session_uuid": session_uuid, "docname": docname}
	payload.update(extra)
	try:
		frappe.publish_realtime(event_name, payload, user=user)
	except Exception:
		# Don't log — realtime is best-effort, the widget falls back
		# to its on-visibility-change status fetch.
		pass


def _clear_active(user: str) -> None:
	"""Remove the user's active session pointer from Redis.

	Idempotent; safe to call even if no session is active. Once this
	returns, no further requests from this user will activate recording.
	"""
	session.clear_active_session(user)


def _mark_stopping(user: str, session_uuid: str) -> str | None:
	"""Transition the Optimus Session row to the Stopping state.

	Returns the docname on success or None if no matching row exists.
	"""
	docname = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid, "user": user},
		"name",
	)
	if not docname:
		return None

	frappe.db.set_value(
		"Optimus Session",
		docname,
		{"status": "Stopping", "stopped_at": now_datetime()},
	)
	safe_commit()
	return docname


def _enqueue_analyze(session_uuid: str, docname: str | None = None) -> bool:
	"""Enqueue analyze on the long queue, or run inline if no worker
	will consume it.

	When `bench disable-scheduler` is in effect (or the "Enable
	Scheduler" toggle in System Settings is off), many deployments
	don't have a `bench worker` processing the RQ queue either, so
	an enqueued analyze job would sit forever and the session would
	hang in the "Stopping" state. In that case we fall back to
	``frappe.enqueue(now=True)`` which executes analyze synchronously
	inside the current request. See v0.5.0 design spec §6.

	Inline analyze has a recording-count safety cap
	(``optimus_inline_analyze_limit``, default 50) to stay within
	gunicorn's ~120s request timeout on heavy sessions. When the cap
	is exceeded, the session is marked Failed with an actionable
	error and analyze is NOT invoked — the user sees the failure
	immediately rather than having gunicorn kill the request
	mid-analyze and strand the session half-analyzed.

	The cap lives here (not in ``_stop_session``) so every caller of
	this function — ``stop``, ``retry_analyze``, ``janitor._sweep_stale``
	— gets the same protection. Earlier versions only applied it in
	``_stop_session``, which meant ``retry_analyze`` on a 200-recording
	failed session on a scheduler-disabled site would hit the gunicorn
	timeout.

	Args:
	    session_uuid: the session to analyze.
	    docname: the Optimus Session docname for cap-exceeded failure
	        handling. When provided, cap violations mark this doc Failed
	        with a user-facing message. When None, the cap is skipped
	        (internal callers only — all production paths pass docname).

	Returns True if the session is already finalized (Ready or Failed)
	by the time this call returns — that is, analyze ran synchronously
	OR was rejected by the inline cap. Returns False when the job was
	pushed to the async queue and the session is still Stopping.

	Inline execution can fail — analyze.run catches its own exceptions
	and marks the session as Failed via frappe.db.set_value, then
	re-raises. We catch the re-raise here so the caller's response
	isn't a 500 error. Returning True with the session already
	marked Failed lets the caller read the final status off the doc
	and transition the widget to a correct terminal state.
	"""
	from frappe.utils.scheduler import is_scheduler_disabled

	run_inline = False
	try:
		run_inline = bool(is_scheduler_disabled())
	except Exception:
		frappe.log_error(title="optimus scheduler check")

	if run_inline:
		# Inline cap check — refuse huge sessions that would exceed
		# gunicorn's request timeout. Only applied when docname is
		# provided (all production callers provide it).
		if docname:
			cap = frappe.conf.get("optimus_inline_analyze_limit") or 50
			try:
				count = session.recording_count(session_uuid)
			except Exception:
				count = 0
			if count > cap:
				# IMPORTANT: the field is `analyzer_warnings` (plural,
				# with -s). An earlier v0.5.0 version wrote to a
				# phantom `analyze_error` field which doesn't exist
				# on the doctype, causing MariaDB to raise 'Unknown
				# column' and the stop API to return 500. The test
				# suite missed this because FakeDB.set_value accepted
				# any field name as a no-op.
				try:
					frappe.db.set_value(
						"Optimus Session",
						docname,
						{
							"status": "Failed",
							"analyzer_warnings": (
								f"Scheduler is disabled and this session "
								f"has {count} recordings, exceeding the "
								f"inline analyze cap of {cap}. "
								f"Re-enable the scheduler "
								f"(bench enable-scheduler) and click "
								f"Retry Analyze on this session's form view."
							),
						},
					)
					safe_commit()
				except Exception:
					frappe.log_error(
						title="optimus inline cap mark Failed"
					)
				# The session is finalized (Failed). Return True so
				# the caller treats it like any other inline result.
				return True

		frappe.logger().warning(
			f"optimus: scheduler disabled; running analyze "
			f"inline for session {session_uuid}. Caller will block "
			f"until analyze completes."
		)
		try:
			frappe.enqueue(
				"optimus.analyze.run",
				queue="long",
				session_uuid=session_uuid,
				now=True,
			)
		except Exception:
			# analyze.run already marked the session Failed and
			# re-raised. Swallow here so the caller returns 200 — the
			# caller reads the final status off the doc and reports
			# it to the widget.
			frappe.log_error(
				title=f"optimus inline analyze {session_uuid}"
			)
		return True

	# Enqueue analyze on the async "long" queue. NOTE (v0.7.x): we deliberately
	# do NOT dedup with a stable job_id + is_job_enqueued guard. That guard
	# stranded sessions at "Stopping": a worker OOM-killed mid-analyze leaves a
	# zombie STARTED job, is_job_enqueued then returns True, the enqueue is
	# skipped, and nothing transitions the session out of "Stopping" (retry hits
	# the same guard; no janitor sweep covered it). Concurrent-analyze RAM is
	# already bounded by analyze.run's single-flight, so a rare duplicate from a
	# double-Stop is harmless; a permanent strand is not. The janitor's
	# _sweep_stale_stopping is the durable backstop.
	frappe.enqueue(
		"optimus.analyze.run",
		queue="long",
		session_uuid=session_uuid,
		now=False,
	)
	return False


@frappe.whitelist()
def status() -> dict:
	"""Return whether the calling user has an active profiling session."""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"active": False}

	meta = session.get_session_meta(active) or {}
	return {
		"active": True,
		"session_uuid": active,
		"docname": meta.get("docname"),
		"label": meta.get("label"),
		"started_at": meta.get("started_at"),
	}


@frappe.whitelist()
def get_active_session() -> dict | None:
	"""Return full metadata for the calling user's active session, or None."""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return None
	return session.get_session_meta(active)


# ---------------------------------------------------------------------------
# v0.5.0: frontend metrics receiver
# ---------------------------------------------------------------------------

SOFT_CAP_FRONTEND_XHR = 1000
SOFT_CAP_FRONTEND_VITALS = 200


# v0.12.0: keys centralized in optimus.redis_keys. The two local helpers
# kept their original names + signatures so call sites don't churn, but
# now delegate to the canonical builders. Future PRs can inline the
# calls; for now this keeps the api.py diff localized.
def _frontend_xhr_key(session_uuid: str) -> str:
	from optimus import redis_keys

	return redis_keys.frontend_xhr(session_uuid)


def _frontend_vitals_key(session_uuid: str) -> str:
	from optimus import redis_keys

	return redis_keys.frontend_vitals(session_uuid)


@frappe.whitelist(methods=["POST"])
def submit_frontend_metrics(payload: str) -> dict:
	"""Receive a batch of frontend metrics (XHR timings + Web Vitals).

	Called by optimus_frontend.js at stop time (via frappe.call) and
	at beforeunload (via navigator.sendBeacon). Payload is a JSON
	string because sendBeacon sends raw Blob — Frappe's dict auto-
	parsing doesn't kick in for sendBeacon, so we take a string and
	parse explicitly.

	**Storage: two Redis lists per session, written via atomic RPUSH
	+ LTRIM.** Earlier versions of this endpoint used a GET-merge-SET
	pattern over a single JSON dict, which had a read-modify-write race:
	two concurrent submits (e.g. stop-time frappe.call colliding with a
	beforeunload beacon) could both read the same existing blob, both
	compute a merged result, and both write — losing one submission's
	contents. RPUSH + LTRIM is atomic in Redis, so concurrent submits
	just append their entries without data loss. LTRIM enforces the
	soft cap (tail-preferring — newest entries survive).

	Redis keys:
	  profiler:frontend:<uuid>:xhr     → list of JSON-encoded XHR entries
	  profiler:frontend:<uuid>:vitals  → list of JSON-encoded Web Vitals entries
	"""
	user = _require_profiler_user()

	try:
		if isinstance(payload, str):
			data = frappe.parse_json(payload)
		else:
			data = payload
	except Exception:
		return {"accepted": False, "reason": "invalid json"}

	if not isinstance(data, dict):
		return {"accepted": False, "reason": "invalid payload"}

	session_uuid = data.get("session_uuid")
	if not session_uuid:
		return {"accepted": False, "reason": "missing session_uuid"}

	# Ownership check: only the user who owns the session can write to
	# its frontend blob. Silent-drop on missing meta because a
	# beforeunload beacon can legitimately arrive after the session has
	# already been stopped and its meta deleted — no log spam.
	meta = session.get_session_meta(session_uuid) or {}
	if not meta or meta.get("user") != user:
		return {"accepted": False, "reason": "session not found"}

	# Client-side tail-preferring cap so a single oversized submit
	# doesn't push MAX entries through Redis only to have them trimmed.
	import json as _json

	xhr = (data.get("xhr") or [])[-SOFT_CAP_FRONTEND_XHR:]
	vitals = (data.get("vitals") or [])[-SOFT_CAP_FRONTEND_VITALS:]

	xhr_key = _frontend_xhr_key(session_uuid)
	vitals_key = _frontend_vitals_key(session_uuid)

	# Atomic append: RPUSH + LTRIM. Each entry is JSON-encoded as a
	# Redis list element. frappe.cache.rpush accepts one value at a
	# time — we loop, which is O(n) round trips but fine at the
	# submission sizes we cap at (≤ 1000 XHRs, ≤ 200 vitals per call).
	if xhr:
		for entry in xhr:
			try:
				frappe.cache.rpush(xhr_key, _json.dumps(entry, default=str))
			except Exception:
				frappe.log_error(title="optimus frontend rpush (xhr)")
		# Tail-preferring trim: keep the last N entries.
		try:
			frappe.cache.ltrim(xhr_key, -SOFT_CAP_FRONTEND_XHR, -1)
			frappe.cache.expire_key(xhr_key, session.SESSION_TTL_SECONDS)
		except Exception:
			frappe.log_error(title="optimus frontend ltrim (xhr)")

	if vitals:
		for entry in vitals:
			try:
				frappe.cache.rpush(vitals_key, _json.dumps(entry, default=str))
			except Exception:
				frappe.log_error(title="optimus frontend rpush (vitals)")
		try:
			frappe.cache.ltrim(vitals_key, -SOFT_CAP_FRONTEND_VITALS, -1)
			frappe.cache.expire_key(vitals_key, session.SESSION_TTL_SECONDS)
		except Exception:
			frappe.log_error(title="optimus frontend ltrim (vitals)")

	# Report the current post-merge sizes so the client can confirm.
	try:
		xhr_count = frappe.cache.llen(xhr_key) or 0
	except Exception:
		xhr_count = 0
	try:
		vitals_count = frappe.cache.llen(vitals_key) or 0
	except Exception:
		vitals_count = 0

	return {
		"accepted": True,
		"xhr_count": xhr_count,
		"vitals_count": vitals_count,
	}


def _read_frontend_data(session_uuid: str) -> dict:
	"""Read the submit_frontend_metrics Redis lists back into a dict
	with the shape the frontend_timings analyzer expects.

	Decodes each list entry from JSON. Bad entries are silently
	skipped — the analyzer can handle partial data.
	"""
	import json as _json

	xhr_key = _frontend_xhr_key(session_uuid)
	vitals_key = _frontend_vitals_key(session_uuid)

	def _decode_list(key):
		try:
			raw = frappe.cache.lrange(key, 0, -1) or []
		except Exception:
			return []
		out = []
		for item in raw:
			if isinstance(item, bytes):
				item = item.decode("utf-8", errors="replace")
			try:
				out.append(_json.loads(item))
			except Exception:
				continue
		return out

	return {
		"xhr": _decode_list(xhr_key),
		"vitals": _decode_list(vitals_key),
	}


@frappe.whitelist()
def health() -> dict:
	"""Lightweight health/metrics endpoint for ops scrapers.

	Returns a small structured dict with counts by session status and
	analyze-pipeline performance over the last 24 hours. Intended to be
	polled from Prometheus/Grafana/Datadog via a custom scraper, or
	called manually by an admin to sanity-check the profiler's health.

	Permission: any role that can use the profiler (Optimus User or
	System Manager). Doesn't expose session contents — just aggregate
	counts.
	"""
	_require_profiler_user()

	# Count by status
	rows = (
		frappe.db.sql(
			"SELECT status, COUNT(*) FROM `tabOptimus Session` GROUP BY status",
			as_list=True,
		)
		or []
	)
	by_status = {row[0]: int(row[1]) for row in rows}

	# Analyze performance over the last 24 hours. Only count Ready
	# sessions — Failed sessions don't have a meaningful analyze time.
	recent = frappe.db.sql(
		"""
		SELECT
			COUNT(*),
			COALESCE(AVG(analyze_duration_ms), 0),
			COALESCE(MAX(analyze_duration_ms), 0)
		FROM `tabOptimus Session`
		WHERE status = 'Ready'
		  AND modified > NOW() - INTERVAL 1 DAY
		"""
	)
	count, avg_ms, max_ms = recent[0] if recent else (0, 0, 0)

	# Count by top severity for Ready sessions (useful signal for
	# "are customers finding issues?")
	severity_rows = (
		frappe.db.sql(
			"""
			SELECT COALESCE(top_severity, 'None'), COUNT(*)
			FROM `tabOptimus Session`
			WHERE status = 'Ready'
			GROUP BY top_severity
			""",
			as_list=True,
		)
		or []
	)
	by_severity = {row[0] or "None": int(row[1]) for row in severity_rows}

	return {
		"by_status": by_status,
		"by_top_severity_ready": by_severity,
		"last_24h": {
			"sessions_ready": int(count or 0),
			"analyze_avg_ms": round(float(avg_ms or 0), 2),
			"analyze_max_ms": round(float(max_ms or 0), 2),
		},
	}


# v0.4.0: onboarding toast state endpoints. Used by floating_widget.js
# to decide whether to render the one-time onboarding toast.
# v0.12.0: key construction moved to optimus.redis_keys.onboarding_seen.
# The TTL constant stays here as the policy lever — bump from the 1-year
# default if a real complaint surfaces. The PREFIX constant is gone; the
# read/write sites below call redis_keys.onboarding_seen(user) directly.
ONBOARDING_CACHE_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year


@frappe.whitelist()
def check_onboarding_seen() -> dict:
	"""Has the current user dismissed the onboarding toast?

	Also returns True if the user has any existing Ready Optimus Session
	row (they're an experienced user; suppress the toast).
	"""
	user = _require_user()
	# Suppress for experienced users — anyone with at least one Ready session
	try:
		existing = frappe.db.count(
			"Optimus Session",
			filters={"user": user, "status": "Ready"},
		)
		if existing and existing > 0:
			return {"seen": True}
	except Exception:
		pass
	# v0.12.13: onboarding_seen is the second value migrated to the
	# v0.12.0 versioned envelope. ``unwrap_value`` returns either the
	# wrapped payload (new-shape writers, v0.12.13+) or the legacy bare
	# value (pre-v0.12.13 writers — strings like "1"). Both shapes
	# are truthy, so ``bool(payload)`` resolves the dismissed flag
	# regardless of which writer set it.
	from optimus import redis_keys, redis_schema

	raw = frappe.cache.get_value(redis_keys.onboarding_seen(user))
	payload, _version = redis_schema.unwrap_value(raw)
	return {"seen": bool(payload)}


@frappe.whitelist()
def mark_onboarding_seen() -> dict:
	"""Mark the onboarding toast as dismissed for the current user."""
	user = _require_user()
	from optimus import redis_keys, redis_schema

	frappe.cache.set_value(
		redis_keys.onboarding_seen(user),
		redis_schema.wrap_value("1"),
		expires_in_sec=ONBOARDING_CACHE_TTL_SECONDS,
	)
	return {"seen": True}


@frappe.whitelist()
@rate_limit(key="optimus_download_pdf", limit=20, seconds=60)
def download_pdf(session_uuid: str) -> dict:
	"""Return the URL of the report PDF, generating it on first call.

	Permission: recording user, System Manager, or Administrator.
	Mirrors retry_analyze / export_session permission gating.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	_require_session_permission(session_uuid, "read")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(f"Cannot generate PDF for session in '{row['status']}' state")

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only download PDFs for your own sessions.",
			frappe.PermissionError,
		)

	from optimus import pdf_export

	url = pdf_export.get_or_generate_pdf(session_uuid)
	return {"file_url": url}


@frappe.whitelist()
@rate_limit(key="optimus_export_session", limit=20, seconds=60)
def export_session(session_uuid: str) -> dict:
	"""Export a Optimus Session as a structured JSON blob.

	Lets dev shops (or automation) consume the profiler's output
	programmatically without parsing the HTML report. Returns the full
	session including all child rows, top queries, table breakdown, and
	finding technical details — everything in the report, in a
	machine-friendly shape.

	Permission model: mirrors the report download gate — only the
	recording user or a System Manager can export. Other Optimus Users
	get a permission error even if they somehow guessed the uuid.
	"""
	import json

	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	_require_session_permission(session_uuid, "read")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		"name",
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")

	doc = frappe.get_doc("Optimus Session", row["name"])

	# Permission gate (same logic as retry_analyze)
	roles = set(frappe.get_roles(user))
	if doc.user != user and "System Manager" not in roles and user != "Administrator":
		frappe.throw("You can only export your own sessions.", frappe.PermissionError)

	def _parse_json_field(value):
		if not value:
			return []
		try:
			return json.loads(value)
		except Exception:
			return []

	return {
		"schema_version": 1,
		"exported_at": frappe.utils.now_datetime().isoformat(),
		"session": {
			"session_uuid": doc.session_uuid,
			"title": doc.title,
			"user": doc.user,
			"status": doc.status,
			"started_at": str(doc.started_at) if doc.started_at else None,
			"stopped_at": str(doc.stopped_at) if doc.stopped_at else None,
			"total_duration_ms": doc.total_duration_ms,
			"total_requests": doc.total_requests,
			"total_queries": doc.total_queries,
			"total_query_time_ms": doc.total_query_time_ms,
			"analyze_duration_ms": getattr(doc, "analyze_duration_ms", None),
			"top_severity": getattr(doc, "top_severity", None),
			"analyzer_warnings": doc.analyzer_warnings,
			# v0.3.0 fields
			"total_python_ms": getattr(doc, "total_python_ms", None),
			"total_sql_ms": getattr(doc, "total_sql_ms", None),
		},
		"actions": [
			{
				"idx": a.idx,
				"action_label": a.action_label,
				"event_type": a.event_type,
				"http_method": a.http_method,
				"path": a.path,
				"recording_uuid": a.recording_uuid,
				"duration_ms": a.duration_ms,
				"queries_count": a.queries_count,
				"query_time_ms": a.query_time_ms,
				"slowest_query_ms": a.slowest_query_ms,
				# v0.3.0: include the call tree (or its overflow marker)
				"call_tree": _parse_json_field(getattr(a, "call_tree_json", None)),
			}
			for a in (doc.actions or [])
		],
		"findings": [
			{
				"idx": f.idx,
				"finding_type": f.finding_type,
				"severity": f.severity,
				"title": f.title,
				"customer_description": f.customer_description,
				"technical_detail": _parse_json_field(f.technical_detail_json),
				"estimated_impact_ms": f.estimated_impact_ms,
				"affected_count": f.affected_count,
				"action_ref": f.action_ref,
			}
			for f in (doc.findings or [])
		],
		"top_queries": _parse_json_field(doc.top_queries_json),
		"table_breakdown": _parse_json_field(doc.table_breakdown_json),
		# v0.3.0 top-level aggregates
		"hot_frames": _parse_json_field(getattr(doc, "hot_frames_json", None)),
		"session_time_breakdown": _parse_json_field(
			getattr(doc, "session_time_breakdown_json", None)
		),
	}


@frappe.whitelist()
@rate_limit(key="optimus_retry_analyze", limit=5, seconds=60)
def retry_analyze(session_uuid: str) -> dict:
	"""Retry the analyze job for a Failed session.

	Allows the recording user or a System Manager to recover from
	transient analyzer errors (worker crash, DB timeout, etc.) without
	dropping into a Frappe console. The session must be in `Failed`
	state — retrying a Ready or Recording session is a no-op.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	_require_session_permission(session_uuid, "write")

	doc = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not doc:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")

	# Permission check: recording user OR System Manager only
	roles = set(frappe.get_roles(user))
	if doc["user"] != user and "System Manager" not in roles and user != "Administrator":
		frappe.throw("You can only retry your own sessions.", frappe.PermissionError)

	if doc["status"] != "Failed":
		return {
			"retried": False,
			"reason": f"Session is in '{doc['status']}' state, not Failed.",
		}

	# Reset to Stopping so the analyze pipeline runs through its usual
	# state transitions.
	frappe.db.set_value(
		"Optimus Session",
		doc["name"],
		{"status": "Stopping", "analyzer_warnings": None},
	)
	safe_commit()

	# v0.4.0: clear the cached PDF so the next download regenerates from
	# the fresh HTML produced by the re-run analyze.
	try:
		from optimus import pdf_export

		pdf_export.clear_cached_pdf(session_uuid)
	except Exception:
		pass

	# v0.5.0: use the scheduler-aware enqueue helper so retry also works
	# on sites where bench disable-scheduler is in effect. Earlier
	# versions called frappe.enqueue directly here, which on scheduler-
	# disabled sites would re-hit the exact hung-forever bug that the
	# v0.5.0 scheduler fallback was designed to fix — Retry would push
	# to a queue no worker consumes and the session would stay stuck
	# in Stopping forever. Passing docname also lets the inline cap
	# check mark this session Failed (with a clear message) if the
	# recording count exceeds the inline limit.
	ran_inline = _enqueue_analyze(session_uuid, docname=doc["name"])

	# Read back the final status if inline analyze ran, so the client
	# can show the right terminal state (same contract as stop()).
	final_status = None
	if ran_inline:
		try:
			final_status = frappe.db.get_value(
				"Optimus Session", doc["name"], "status"
			)
		except Exception:
			final_status = None

	return {
		"retried": True,
		"session_uuid": session_uuid,
		"docname": doc["name"],
		"ran_inline": ran_inline,
		"status": final_status,
	}


@frappe.whitelist()
@rate_limit(key="optimus_regenerate_reports", limit=30, seconds=60)
def regenerate_reports(session_uuid: str) -> dict:
	"""Re-render the HTML report from stored session data.

	Unlike ``retry_analyze``, this does NOT re-run any analyzer. It
	only invokes ``renderer.render_raw`` on the existing Profiler
	Session row. Typical use: the report template or renderer code
	changed (e.g. upgrading optimus to a new version with an
	improved layout) and you want the new UI applied to an already-
	analyzed session without the cost of re-running the entire
	analysis pipeline.

	Characteristics:

	  - **Fast** — milliseconds vs. minutes. No DB-heavy analyzer
	    passes, no EXPLAIN calls. Just template rendering.
	  - **Safe to run repeatedly** — idempotent. Each invocation
	    replaces the existing report File attachment and clears
	    the cached PDF.
	  - **Best-effort recordings** — fetches the original recordings
	    from Redis by UUID. If they've expired (TTL exceeded),
	    per-query drill-down / Full recordings sections render empty
	    but every other section (findings, stats, hot frames, exec
	    summary, analyzer notes) stays intact because those are
	    persisted on the Optimus Session row.
	  - **Allowed on Ready OR Failed sessions** — re-rendering a
	    Failed session whose analyze partially completed is often
	    how this feature pays for itself (unblocks a demo when a
	    render-time bug was fixed).

	Permission: recording user OR System Manager.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	_require_session_permission(session_uuid, "write")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")

	# v0.12.9: enforce the docstring's "Allowed on Ready OR Failed
	# sessions" contract. The pre-v0.12.9 code accepted any status,
	# which would attach an incomplete report to a still-running
	# analyze (Recording / Stopping / Analyzing) that the pipeline
	# would then overwrite — confusing for operators, and surfaced
	# in v0.12.4 by the integration test that exercised this gap.
	if row["status"] not in ("Ready", "Failed"):
		frappe.throw(
			f"regenerate_reports requires the session to be in a terminal "
			f"state (Ready or Failed); this one is '{row['status']}'. "
			f"Wait for analyze to finish, or use retry_analyze to restart "
			f"a stuck pipeline."
		)

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only regenerate reports for your own sessions.",
			frappe.PermissionError,
		)

	# Best-effort recording fetch. The renderer uses DocType fields for
	# everything important; recordings only power the per-query drill-
	# down + Full recordings sections. If Redis dropped them, render
	# with an empty list and the rest of the report is still fine.
	from optimus import analyze as _analyze_mod

	doc = frappe.get_doc("Optimus Session", row["name"])
	recording_uuids = [
		a.recording_uuid
		for a in (doc.actions or [])
		if getattr(a, "recording_uuid", None)
	]
	try:
		recordings = list(_analyze_mod._fetch_recordings(
			recording_uuids, recordings_bundle=_analyze_mod._load_recordings_bundle(doc)
		))
	except Exception:
		frappe.log_error(title="optimus regenerate_reports fetch")
		recordings = []

	# v0.6.0: if "Suggest AI fixes in the report by default" is on, backfill
	# AI suggestions onto the top eligible findings that don't have one yet,
	# so a session analyzed before the switch was flipped picks them up on a
	# Regenerate. Best-effort + tightly time-budgeted (it runs synchronously
	# in this web request). The persisted llm_fix_json is what the renderer
	# below reads to draw the "Suggested fix (AI)" block under each finding.
	try:
		_analyze_mod._backfill_ai_suggestions(doc)
	except Exception:
		frappe.log_error(title="optimus regenerate ai backfill")

	# Invalidate the cached PDF — next /api/method/download_pdf call
	# will regenerate it from the freshly-rendered HTML.
	try:
		from optimus import pdf_export

		pdf_export.clear_cached_pdf(session_uuid)
	except Exception:
		pass

	_analyze_mod._render_and_attach_reports(row["name"], recordings)

	return {
		"regenerated": True,
		"session_uuid": session_uuid,
		"docname": row["name"],
		"recordings_available": len(recordings),
		"actions_total": len(doc.actions or []),
	}


@frappe.whitelist()
@rate_limit(key="optimus_suggest_fix", limit=10, seconds=60)
def suggest_fix(session_uuid: str, finding_ref: str, regenerate=0) -> dict:
	"""Generate (or return the cached) AI-suggested fix for one finding.

	On-demand only — never called during analyze. The finding's context
	(type, callsite, a window of surrounding source, normalized SQL +
	EXPLAIN, the static fix hint) is sent to the LLM configured in
	Optimus Settings ▸ AI Fix Suggestions; the result is stored on the
	Optimus Finding row (``llm_fix_json``) so re-opening returns it
	without another API call, and so the regenerated HTML report carries
	it.

	Args:
	    session_uuid: the Optimus Session.
	    finding_ref:  the child-row ``name`` of the finding (the form's
	        ``frm.doc.findings[i].name``). A bare integer is accepted as a
	        0-based index fallback.
	    regenerate:   truthy → ignore any cached suggestion and re-call.

	Permission: recording user / System Manager / Administrator (mirrors
	``download_pdf``). The AI must be enabled and configured.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	if not finding_ref:
		frappe.throw("finding_ref is required")
	_require_session_permission(session_uuid, "write")
	regenerate = bool(frappe.utils.cint(regenerate))

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(
			f"AI fix suggestions are only available for Ready sessions "
			f"(this one is '{row['status']}')."
		)

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only request AI fixes for your own sessions.",
			frappe.PermissionError,
		)

	from optimus import ai_fix

	if not ai_fix.is_available():
		frappe.throw(
			"AI fix suggestions aren't configured — enable them in Profiler "
			"Settings ▸ AI Fix Suggestions and set a provider, model, and API key."
		)
	from optimus.settings import get_config
	if not get_config().ai_suggest_findings:
		frappe.throw(
			'AI fix suggestions on findings are turned off — enable '
			'"Fix suggestions on findings" under Optimus Settings ▸ AI.'
		)

	doc = frappe.get_doc("Optimus Session", row["name"])
	findings = doc.findings or []
	child = next((f for f in findings if f.name == finding_ref), None)
	if child is None and str(finding_ref).strip().lstrip("-").isdigit():
		idx = int(finding_ref)
		if 0 <= idx < len(findings):
			child = findings[idx]
	if child is None:
		frappe.throw(f"Finding {finding_ref!r} not found on this session.")

	if (child.finding_type or "") not in ai_fix.AI_ELIGIBLE_FINDING_TYPES:
		frappe.throw(
			f"AI fix suggestions aren't offered for '{child.finding_type}' "
			"findings — they don't carry enough code/SQL context."
		)
	# v0.9.0: per-type opt-out (Critical Risk #2). Refuse on-demand calls
	# for types listed in ``ai_excluded_finding_types`` with a clear message
	# pointing the operator at the setting they configured.
	if ai_fix.is_finding_type_excluded(child.finding_type or ""):
		frappe.throw(
			f"'{child.finding_type}' is on the exclusion list in Optimus "
			"Settings ▸ AI ▸ Privacy & Operations ▸ Excluded finding types. "
			"Remove the line if you want AI suggestions for this type."
		)

	# Return the cached suggestion unless the caller asked to regenerate.
	if not regenerate and (child.llm_fix_json or "").strip():
		try:
			cached = json.loads(child.llm_fix_json)
		except Exception:
			cached = None
		if isinstance(cached, dict) and (cached.get("suggestion") or "").strip():
			return {"ok": True, "finding": child.name, "cached": True, **cached}

	# Build the LLM context: the finding dict + a wide source window + (when a
	# Phase-2 line-profile pass instrumented this finding's function) its
	# hottest line + (best-effort) the top-N slowest SQL queries from this
	# action's recording — shared with the analyze-time auto-suggest path.
	from optimus import analyze as _analyze_mod

	# v0.6.x: fetch the single action's recording from Redis so the AI gets
	# verbatim SQL evidence to ground against (the leading cause of bogus AI
	# refactorings was the model inferring SQL shape from Python source
	# instead of reading the actual query). Best-effort — if the recording
	# expired from Redis, fall through with just the source-window context.
	recordings_by_uuid: dict = {}
	actions_by_idx: dict = {}
	try:
		actions_by_idx = {
			int(a.idx) if hasattr(a, "idx") else i: {
				"idx": int(getattr(a, "idx", i)),
				"recording_uuid": a.recording_uuid or "",
			}
			for i, a in enumerate(doc.actions or [])
		}
		ref = (getattr(child, "action_ref", None) or "").strip()
		if ref and ref.lstrip("-").isdigit():
			act = actions_by_idx.get(int(ref))
			rec_uuid = (act or {}).get("recording_uuid")
			if rec_uuid:
				recs = list(_analyze_mod._fetch_recordings([rec_uuid], recordings_bundle=_analyze_mod._load_recordings_bundle(doc)))
				recordings_by_uuid = {r.get("uuid"): r for r in recs if r.get("uuid")}
	except Exception:
		# Never let a recording-fetch error block the AI suggestion — fall
		# through with whatever context we already have.
		recordings_by_uuid = {}

	finding_dict = _analyze_mod._ai_payload_for_finding(
		child, {}, phase2_index=_analyze_mod._phase2_index_for(doc),
		recordings_by_uuid=recordings_by_uuid,
		actions_by_idx=actions_by_idx,
	)

	try:
		_analyze_mod._mark_ai_spend_session(session_uuid)
		result = ai_fix.suggest_fix(finding_dict)
	except ai_fix.AiFixError as e:
		frappe.throw(str(e))

	try:
		frappe.db.set_value(
			"Optimus Finding", child.name, "llm_fix_json", json.dumps(result),
		)
		safe_commit()
	except Exception:
		# Failing to persist isn't fatal — the operator still gets the
		# suggestion in the dialog, just not cached / in the report.
		frappe.log_error(title="optimus suggest_fix persist")

	return {"ok": True, "finding": child.name, "cached": False, **result}


@frappe.whitelist()
def test_ai_connection() -> dict:
	"""Probe the configured AI provider. System-Manager-only (config-
	adjacent — Optimus Settings itself is SysMgr-only). Returns
	``{"ok": bool, "message": str, "model": str}``; never raises on a
	provider failure (the detail is in ``message``)."""
	# The settings probe isn't billable to any session — clear the spend marker
	# so its tokens don't land on whatever session this worker last served.
	from optimus import analyze as _analyze_mod

	_analyze_mod._mark_ai_spend_session(None)
	user = _require_profiler_user()
	roles = set(frappe.get_roles(user))
	if "System Manager" not in roles and user != "Administrator":
		frappe.throw(
			"Only a System Manager can test the AI connection.",
			frappe.PermissionError,
		)
	from optimus import ai_fix

	return ai_fix.test_connection()


@frappe.whitelist()
def ai_capabilities() -> dict:
	"""The per-section LLM toggles, for the Optimus Session form to decide
	which AI buttons to show. Any logged-in profiler user — no Profiler
	Settings read permission needed (the server still enforces the toggles).
	Returns ``{enabled, findings, indexes, humanize}`` (all bools)."""
	_require_profiler_user()
	from optimus.settings import get_config
	cfg = get_config()
	return {
		"enabled": bool(getattr(cfg, "ai_enabled", False)),
		"findings": bool(getattr(cfg, "ai_suggest_findings", True)),
		"indexes": bool(getattr(cfg, "ai_suggest_indexes", True)),
		"humanize": bool(getattr(cfg, "ai_humanize_steps", True)),
	}


@frappe.whitelist()
def backfill_ai_fixes(session_uuid: str, regenerate_all=0) -> dict:
	"""Generate AI fix suggestions for eligible findings on the session, then
	re-render the report so they show up.

	- ``regenerate_all`` falsy (default) — "Generate AI fixes": fill only the
	  eligible findings that don't have a suggestion yet. Use this after the
	  LLM was unavailable during analyze (with "Suggest AI fixes by default"
	  on, the analyze still completes — the AI part is just skipped), or to
	  populate suggestions on demand without turning auto-suggest on.
	- ``regenerate_all`` truthy — "Re-evaluate AI fixes": (re)generate the
	  suggestion for EVERY eligible finding, overwriting the existing ones.
	  Use this after changing the AI model or prompt. A failure mid-re-eval
	  leaves the old suggestion in place (only successful runs overwrite).

	Either way it bypasses the ``ai_auto_suggest`` toggle (you're asking for
	it explicitly) but still requires the provider to be configured, and is
	bounded by the same per-call time budget — if it reports findings skipped
	for time, just run it again.

	Permission: recording user / System Manager / Administrator (mirrors
	``regenerate_reports`` / ``download_pdf``).
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	regenerate_all = bool(frappe.utils.cint(regenerate_all))

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(
			f"AI fixes can only be generated for Ready sessions "
			f"(this one is '{row['status']}')."
		)

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only generate AI fixes for your own sessions.",
			frappe.PermissionError,
		)

	from optimus import ai_fix

	if not ai_fix.is_available():
		frappe.throw(
			"AI fix suggestions aren't configured — enable them in Profiler "
			"Settings ▸ AI Fix Suggestions and set a provider, model, and API key."
		)
	from optimus.settings import get_config
	if not get_config().ai_suggest_findings:
		frappe.throw(
			'AI fix suggestions on findings are turned off — enable '
			'"Fix suggestions on findings" under Optimus Settings ▸ AI.'
		)

	from optimus import analyze as _analyze_mod

	doc = frappe.get_doc("Optimus Session", row["name"])
	# cap=0 → do as many target findings as fit in the time budget, ignoring
	# the auto-suggest cap (the operator asked for them explicitly).
	counts = _analyze_mod._run_ai_backfill(doc, cap=0, regenerate_all=regenerate_all)

	# Re-render so the new/updated suggestions land in the HTML report.
	# regenerate_reports re-fetches the doc (seeing the just-committed
	# llm_fix_json), clears the cached PDF, and re-renders; its own
	# auto-suggest-gated backfill is then a no-op.
	regen = regenerate_reports(session_uuid)

	return {
		"ok": True,
		"session_uuid": session_uuid,
		"regenerate_all": regenerate_all,
		"added": counts["added"],
		"failed": counts["failed"],
		"skipped_time": counts["skipped_time"],
		"total_pending": counts["total_pending"],
		"regenerated": bool(regen.get("regenerated")),
	}


@frappe.whitelist()
def humanize_steps(session_uuid: str) -> dict:
	"""(Re)generate the "Steps to Reproduce" note on a Ready session using the
	configured LLM, overwriting whatever is there, then re-render the report.

	Use this on a session whose steps read as a raw list of HTTP calls (e.g.
	one analyzed before AI was enabled, or with humanizing turned off), or to
	redo it after editing/clearing the note. Permission: recording user /
	System Manager / Administrator (mirrors ``download_pdf``).
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status", "title"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(
			f"Steps can only be (re)generated for Ready sessions "
			f"(this one is '{row['status']}')."
		)

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only do this for your own sessions.",
			frappe.PermissionError,
		)

	from optimus import ai_fix

	if not ai_fix.is_available():
		frappe.throw(
			"AI isn't configured — enable it under Optimus Settings ▸ "
			"AI Fix Suggestions and set a provider, model, and API key."
		)
	from optimus.settings import get_config
	if not get_config().ai_humanize_steps:
		frappe.throw(
			'AI-humanized "Steps to Reproduce" is turned off — enable it '
			'under Optimus Settings ▸ AI.'
		)

	doc = frappe.get_doc("Optimus Session", row["name"])
	status = _humanize_steps_core(doc, title=row.get("title") or None)
	if not status.get("updated"):
		# Mirror the legacy endpoint's contract: surface "no actions" as a
		# user-facing error rather than a silent skip.
		reason = status.get("reason") or "Couldn't humanize the steps."
		frappe.throw(reason)

	regen = regenerate_reports(session_uuid)
	return {
		"ok": True,
		"session_uuid": session_uuid,
		"regenerated": bool(regen.get("regenerated")),
	}


def _humanize_steps_core(doc, *, title: str | None = None) -> dict:
	"""Validation-free rewrite of the session's "Steps to Reproduce" via the
	configured LLM. Caller is responsible for the permission / status / AI-
	available / toggle gates and for the final re-render. Returns
	``{"updated": bool, "reason": str|None}`` so the composite endpoint can
	report per-step outcomes without raising.
	"""
	from optimus import ai_fix
	from optimus import analyze as _analyze_mod

	_analyze_mod._mark_ai_spend_session(getattr(doc, "session_uuid", None))

	recording_uuids = [
		a.recording_uuid for a in (doc.actions or [])
		if getattr(a, "recording_uuid", None)
	]
	try:
		recordings = list(_analyze_mod._fetch_recordings(
			recording_uuids, recordings_bundle=_analyze_mod._load_recordings_bundle(doc)
		))
	except Exception:
		frappe.log_error(title="optimus humanize_steps fetch")
		recordings = []

	actions = _analyze_mod._actions_for_humanizer(recordings)
	if not actions:
		return {
			"updated": False,
			"reason": (
				"This session has no user actions to summarise — it was all "
				"background / polling traffic (or the recordings have expired)."
			),
		}
	_steps_usage: dict = {}
	try:
		steps_md = ai_fix.humanize_steps(actions, session_title=title, usage_out=_steps_usage)
	except ai_fix.AiFixError as e:
		return {"updated": False, "reason": str(e)}

	frappe.db.set_value(
		"Optimus Session", doc.name, {
			"notes": _analyze_mod._assemble_humanized_notes(steps_md),
			# Tokens for this Steps-to-Reproduce humanization. The report's
			# session total rolls it in alongside fix + index suggestions
			# (notes is markdown, so the count needs its own field).
			"ai_steps_tokens": int(_steps_usage.get("total_tokens") or 0),
		},
	)
	safe_commit()
	return {"updated": True, "reason": None}


@frappe.whitelist()
def suggest_index(session_uuid: str, table_name: str) -> dict:
	"""Generate (or regenerate) the LLM-vetted index recommendation for one
	table in the session's "Time spent per database table" breakdown, then
	re-render the report so it shows up there.

	The deterministic "index candidate" (most-used filter combination +
	`frappe.db.add_index` patch) is always in the report; this adds the AI's
	take on top — which composite, whether your existing indexes already
	cover it, and the write-cost call. Requires AI to be configured.
	Permission: recording user / System Manager / Administrator (mirrors
	``download_pdf``).
	"""
	user = _require_profiler_user()
	if not session_uuid or not table_name:
		frappe.throw("session_uuid and table_name are required")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(
			f"Index suggestions are only available for Ready sessions "
			f"(this one is '{row['status']}')."
		)

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only request index suggestions for your own sessions.",
			frappe.PermissionError,
		)

	from optimus import ai_fix

	if not ai_fix.is_available():
		frappe.throw(
			"AI isn't configured — enable it under Optimus Settings ▸ "
			"AI Fix Suggestions and set a provider, model, and API key."
		)
	from optimus.settings import get_config
	if not get_config().ai_suggest_indexes:
		frappe.throw(
			'AI index recommendations are turned off — enable '
			'"Index recommendations (DB-tables breakdown)" under Optimus Settings ▸ AI.'
		)

	from optimus import analyze as _analyze_mod

	doc = frappe.get_doc("Optimus Session", row["name"])
	try:
		out = _analyze_mod._run_table_index_ai_backfill(doc, table_name=table_name)
	except ai_fix.AiFixError as e:
		frappe.throw(str(e))
	if not out.get("ok"):
		frappe.throw(out.get("reason") or "Couldn't generate an index suggestion for that table.")

	regen = regenerate_reports(session_uuid)
	return {
		"ok": True,
		"session_uuid": session_uuid,
		"table": out.get("table"),
		"regenerated": bool(regen.get("regenerated")),
	}


def _refill_indexes_for_doc(doc) -> dict:
	"""Walk the session's table breakdown and run the per-table index AI
	helper for every table that has a heuristic ``recommended_index`` but
	no ``ai_index`` yet. Returns ``{"added": N, "failed": N, "skipped": N}``.
	Caller is responsible for permission / status / AI-available gates and
	for the final re-render.
	"""
	import json as _json

	from optimus import analyze as _analyze_mod

	try:
		breakdown = _json.loads(doc.table_breakdown_json or "[]")
	except Exception:
		breakdown = []

	eligible = [
		t for t in (breakdown or [])
		if isinstance(t, dict)
		and (t.get("recommended_index") or {}).get("columns")
		and not t.get("ai_index")
	]
	added = failed = skipped = 0
	for t in eligible:
		table_name = t.get("table")
		if not table_name:
			skipped += 1
			continue
		try:
			out = _analyze_mod._run_table_index_ai_backfill(doc, table_name=table_name)
		except Exception:
			frappe.log_error(title=f"optimus refill_indexes {table_name}")
			failed += 1
			continue
		if out.get("ok"):
			added += 1
		else:
			# Helper returned a reason (e.g. provider missing for one call) —
			# treat as skipped, not failed, since the doc state is unchanged.
			skipped += 1
	return {"added": added, "failed": failed, "skipped": skipped}


@frappe.whitelist()
def refill_ai_suggestions(session_uuid: str) -> dict:
	"""Single-button entry point. Re-fills every AI-generated section of the
	report in one round-trip:

	1. ``_run_ai_backfill(doc, cap=0, regenerate_all=True)`` — overwrites
	   every eligible finding's fix suggestion.
	2. ``_humanize_steps_core(doc)`` — rewrites Steps to Reproduce.
	3. ``_refill_indexes_for_doc(doc)`` — runs the per-table index helper
	   for tables with a candidate but no AI advice yet.
	4. ``regenerate_reports(session_uuid)`` — one final re-render.

	Each step is gated by its per-section toggle (``ai_suggest_findings``,
	``ai_humanize_steps``, ``ai_suggest_indexes``); a toggle-off step is
	skipped, not errored, so the user gets a single button that does
	whatever's currently enabled. Permission and provider-availability
	gates run once at the top.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status", "title"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(
			f"AI suggestions can only be refreshed for Ready sessions "
			f"(this one is '{row['status']}')."
		)

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only refresh AI suggestions for your own sessions.",
			frappe.PermissionError,
		)

	from optimus import ai_fix

	if not ai_fix.is_available():
		frappe.throw(
			"AI isn't configured — enable it under Optimus Settings ▸ "
			"AI Fix Suggestions and set a provider, model, and API key."
		)

	from optimus import analyze as _analyze_mod
	from optimus.settings import get_config

	cfg = get_config()
	doc = frappe.get_doc("Optimus Session", row["name"])

	# v0.13: count this refresh (cumulative; only ever increases).
	frappe.db.sql(
		"update `tabOptimus Session` "
		"set ai_refresh_count = coalesce(ai_refresh_count, 0) + 1 "
		"where session_uuid = %s",
		(session_uuid,),
	)

	fixes = {"added": 0, "failed": 0, "skipped_time": 0, "skipped": None}
	if cfg.ai_suggest_findings:
		counts = _analyze_mod._run_ai_backfill(doc, cap=0, regenerate_all=True)
		fixes = {
			"added": counts.get("added", 0),
			"failed": counts.get("failed", 0),
			"skipped_time": counts.get("skipped_time", 0),
			"skipped": None,
		}
	else:
		fixes["skipped"] = "toggle_off"

	steps = {"updated": False, "reason": None}
	if cfg.ai_humanize_steps:
		# Re-fetch the doc — the backfill above may have mutated rows.
		doc = frappe.get_doc("Optimus Session", row["name"])
		steps = _humanize_steps_core(doc, title=row.get("title") or None)
	else:
		steps["reason"] = "toggle_off"

	indexes = {"added": 0, "failed": 0, "skipped": 0, "skipped_reason": None}
	if cfg.ai_suggest_indexes:
		doc = frappe.get_doc("Optimus Session", row["name"])
		indexes = _refill_indexes_for_doc(doc)
		indexes["skipped_reason"] = None
	else:
		indexes["skipped_reason"] = "toggle_off"

	regen = regenerate_reports(session_uuid)
	return {
		"ok": True,
		"session_uuid": session_uuid,
		"fixes": fixes,
		"steps": steps,
		"indexes": indexes,
		"regenerated": bool(regen.get("regenerated")),
	}


@frappe.whitelist()
def get_installed_apps_for_tracking() -> list[str]:
	"""Return the bench's installed apps for the Optimus Settings
	▸ Tracked Apps Autocomplete field.

	Excludes ``optimus`` (the profiler's own callsites are
	already filtered regardless of user config, and listing it here
	would suggest tracking the tool that's doing the tracking).

	Restricted to System Manager since Optimus Settings itself is.
	"""
	if "System Manager" not in (frappe.get_roles() or []):
		frappe.throw(
			"Only System Manager can list installed apps for the "
			"Optimus Settings picker."
		)
	apps = frappe.get_installed_apps() or []
	return [app for app in apps if app != "optimus"]


@frappe.whitelist()
def get_config_profiles() -> dict:
	"""Return the named Sensitivity Profile presets for the Optimus Settings
	form. The ``config_profile`` change handler reads this so the threshold
	field values it fills come from the single source of truth in
	``optimus.settings._PROFILES`` (never hardcoded in JS).

	Restricted to System Manager since Optimus Settings itself is.
	"""
	if "System Manager" not in (frappe.get_roles() or []):
		frappe.throw(
			"Only System Manager can read the Optimus sensitivity profiles."
		)
	from optimus import settings
	# Plain dict of dicts — JSON-serializable as-is for the whitelist response.
	return {name: dict(values) for name, values in settings._PROFILES.items()}


# ---------------------------------------------------------------------------
# Phase-2 line profiler API
# ---------------------------------------------------------------------------
# Three whitelisted endpoints that the Optimus Session form's "Phase 2:
# Line Profile" section calls into:
#
#   get_phase2_candidates(session_uuid) — populate the curated picker
#   start_line_profile_pass(session_uuid, picks) — begin a phase-2 run
#   stop_line_profile_pass(run_uuid) — end the run, enqueue analyze
#
# Phase-2 implementation lives in optimus.line_profile.* —
# this surface is the thin transport layer.


def _picker_empty_hint(action_count, with_tree, parsed_ok, candidate_count):
	"""Phase K diagnostic: map the picker's empty-state counter triple
	to a human-readable reason. Surfaced in the dialog when the
	curated list is empty so the operator can self-diagnose
	('did the session capture any call trees?' / 'should I bench
	restart?')."""
	if action_count == 0:
		return (
			"This session has no recorded actions. Nothing to "
			"line-profile - capture a session that exercises the "
			"slow flow first."
		)
	if with_tree == 0:
		return (
			"None of this session's actions carry a captured call "
			"tree. Most common cause: this session was analyzed "
			"before the Sprint-1 HMAC fix loaded. Run ``bench "
			"restart`` and capture a new session. Existing Ready "
			"sessions can be repaired only by re-running analyze."
		)
	if parsed_ok == 0:
		return (
			"Actions carry call trees but none parsed as valid "
			"JSON. Check the bench log for 'Failed to deserialize "
			"pyi tree' or 'optimus analyze' error entries."
		)
	if candidate_count == 0:
		return (
			"Call trees loaded but every frame was filtered out as "
			"framework plumbing. The session may be too short or "
			"hit only framework code. Use the textbox below to type "
			"the function you want."
		)
	return ""


@frappe.whitelist()
def get_phase2_candidates(session_uuid: str) -> dict:
	"""Return the curated candidate list for the phase-2 picker UI.

	Reads the parent Optimus Session's actions, parses each action's
	``call_tree_json``, and builds a top-30 list of frames from user-app
	code (with framework apps surfaced separately under "observations").

	Phase K v0.7 GA: the response also carries a ``diagnostic`` dict
	the picker dialog renders as a yellow callout when both
	``candidates`` and ``observations`` are empty - so the operator
	sees WHY (no actions / no call trees / all filtered) instead of
	a silently empty dialog.
	"""
	import json as _json

	from optimus.line_profile import capture as _lp_capture
	from optimus.line_profile import picker as _lp_picker

	_require_profiler_user()

	parent_docname = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		"name",
	)
	if not parent_docname:
		frappe.throw(f"Optimus Session {session_uuid!r} not found.")

	doc = frappe.get_doc("Optimus Session", parent_docname)

	trees = []
	for action in (doc.actions or []):
		raw = action.call_tree_json
		if not raw:
			continue
		try:
			tree = _json.loads(raw)
		except (TypeError, ValueError):
			continue
		# pyinstrument trees are stored either as the full session shape
		# (``{root: {...}}``) or just the root node.
		if isinstance(tree, dict) and "root" in tree:
			tree = tree["root"]
		trees.append(tree)

	# Phase K v0.7 GA: tree-indented DFS picker - parents land in the
	# list before their children, each row tagged with ``depth`` so
	# the JS dialog can render hierarchy via indented labels.
	candidates = _lp_picker._build_tree_indented_candidates(trees)

	# v0.6.0 Round 6: surface the configured auto-expand default so the
	# picker dialog ticks/un-ticks its checkbox per Optimus Settings.
	try:
		from optimus.settings import get_config
		default_auto_expand = bool(get_config().phase2_default_auto_expand)
	except Exception:
		default_auto_expand = True

	action_count = len(doc.actions or [])
	actions_with_tree = sum(
		1 for a in (doc.actions or []) if a.call_tree_json
	)
	trees_parsed_ok = len(trees)
	raw_candidate_count = len(candidates)

	return {
		"session_uuid": session_uuid,
		"docname": parent_docname,
		"candidates": [c for c in candidates if not c["is_framework"]],
		"observations": [c for c in candidates if c["is_framework"]],
		"line_profiler_available": _lp_capture.is_line_profiler_available(),
		"default_auto_expand": default_auto_expand,
		"diagnostic": {
			"action_count": action_count,
			"actions_with_call_tree_json": actions_with_tree,
			"trees_parsed_ok": trees_parsed_ok,
			"raw_candidates_before_filter": raw_candidate_count,
			"hint": _picker_empty_hint(
				action_count,
				actions_with_tree,
				trees_parsed_ok,
				raw_candidate_count,
			),
		},
	}


@frappe.whitelist()
def start_line_profile_pass(session_uuid: str, picks, auto_expand=True) -> dict:
	"""Begin a phase-2 line-profile run on a finished session.

	``picks`` is a JSON-encoded (or already-parsed) list of
	``{dotted_path, source}`` entries the customer ticked / typed.

	When ``auto_expand`` is true (the default), each curated pick is
	walked down phase-1's call tree via ``picker.expand_hot_chain`` so
	the run instruments the full hot chain in one shot — the developer
	doesn't have to re-pick descendants. Free-form picks pass through
	unchanged (we have no chain to walk for them).
	"""
	import json as _json
	import uuid as _uuid

	from optimus.line_profile import capture as _lp_capture
	from optimus.line_profile import picker as _lp_picker

	user = _require_profiler_user()

	# The picks arg often arrives as a string from JS — accept both shapes.
	if isinstance(picks, str):
		try:
			picks_list = _json.loads(picks)
		except _json.JSONDecodeError:
			frappe.throw("picks must be a JSON list of {dotted_path, source} entries.")
	else:
		picks_list = picks
	if not isinstance(picks_list, list) or not picks_list:
		frappe.throw("Provide at least one function to line-profile.")

	# Coerce auto_expand from the JS payload (frappe.call sends "true"/"false"
	# strings; whitelisted view fns accept Python types when available).
	if isinstance(auto_expand, str):
		auto_expand = auto_expand.lower() in ("true", "1", "yes")
	auto_expand = bool(auto_expand)

	# Phase-1 must not be active for the same user — phase 1 and phase 2
	# read separate Redis flags but only one can be active at a time.
	if session.get_active_session_for(user):
		frappe.throw(
			"You currently have a phase-1 session recording. Stop it before "
			"starting a phase-2 line-profile run.",
		)

	# Look up the parent Optimus Session and verify it's Ready.
	parent_docname = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		"name",
	)
	if not parent_docname:
		frappe.throw(f"Optimus Session {session_uuid!r} not found.")
	parent_status = frappe.db.get_value("Optimus Session", parent_docname, "status")
	if parent_status != "Ready":
		frappe.throw(
			f"Phase-2 requires a finished session (status=Ready); current "
			f"status is {parent_status!r}.",
		)

	# Reject if the user already has a phase-2 run in flight elsewhere.
	if _lp_capture.is_active(user):
		frappe.throw("You already have a phase-2 line-profile run active.")

	# Auto-expand curated picks via phase-1's call tree. Curated picks come
	# in as {dotted_path, source: "curated"}; the expansion adds their hot
	# user-code descendants up to the framework boundary. Free-form picks
	# (source != "curated") aren't expanded — we don't know if they appeared
	# in phase 1 at all.
	expansions: list[dict] = []
	if auto_expand:
		# Load the phase-1 call trees once so expand_hot_chain can search
		# across all action recordings for the hottest match.
		parent_doc = frappe.get_doc("Optimus Session", parent_docname)
		trees: list[dict] = []
		for action in (parent_doc.actions or []):
			raw = action.call_tree_json
			if not raw:
				continue
			try:
				tree = _json.loads(raw)
			except (TypeError, ValueError):
				continue
			if isinstance(tree, dict) and "root" in tree:
				tree = tree["root"]
			trees.append(tree)

		seen: set[str] = set()
		# v0.6.0 Round 6: auto-expand depth + min-ms thresholds now read
		# from Optimus Settings (cached) rather than baked into the
		# helper's defaults. Resolved once outside the loop.
		from optimus.settings import get_config as _get_config
		try:
			_cfg = _get_config()
			# v0.13.x: 0 is the "unlimited" sentinel (depth = walk to
			# leaves; min_ms = no minimum to follow). The ``or DEFAULT``
			# fallback only fires when the cfg attr is genuinely missing
			# (legacy pickled OptimusConfig from a pre-v0.13.x bench), so
			# read with a sentinel and check explicitly.
			_max_depth = getattr(_cfg, "auto_expand_max_depth", None)
			_max_depth = int(_max_depth) if _max_depth is not None else 10
			_min_ms = getattr(_cfg, "auto_expand_min_ms", None)
			_min_ms = float(_min_ms) if _min_ms is not None else 50.0
		except Exception:
			_max_depth, _min_ms = 10, 50.0

		expanded_picks: list[dict] = []
		for entry in picks_list:
			dotted = entry.get("dotted_path") or ""
			source = entry.get("source", "freeform")
			# Free-form picks pass through unchanged; we keep them as-is so
			# the resolver can still flag import errors inline.
			if source != "curated":
				if dotted and dotted not in seen:
					seen.add(dotted)
					expanded_picks.append(entry)
				continue
			chain = _lp_picker.expand_hot_chain(
				trees, dotted, max_depth=_max_depth, min_ms=_min_ms,
			)
			if not chain:
				# Picked function wasn't in any phase-1 call tree (rare —
				# the picker UI sources from those same trees). Pass it
				# through so the resolver can still attempt it.
				if dotted and dotted not in seen:
					seen.add(dotted)
					expanded_picks.append(entry)
				continue
			# Track that the chain came from this curated pick so the form
			# can show "instrumented N functions: validate → ... → ..."
			expansions.append({
				"original": dotted,
				"chain": [c["dotted_path"] for c in chain],
			})
			for chain_entry in chain:
				cdp = chain_entry["dotted_path"]
				if cdp and cdp not in seen:
					seen.add(cdp)
					expanded_picks.append({
						"dotted_path": cdp,
						"source": "curated" if chain_entry["depth"] == 0 else "auto_expand",
					})
		picks_list = expanded_picks
		if not picks_list:
			frappe.throw("Provide at least one function to line-profile.")

	run_uuid = _uuid.uuid4().hex

	# Resolve picks + persist Redis state. Raises CaptureError if no pick
	# is eligible.
	try:
		resolved = _lp_capture.start_line_profile_pass(
			session_uuid=session_uuid,
			run_uuid=run_uuid,
			user=user,
			picks=picks_list,
		)
	except _lp_capture.CaptureError as exc:
		frappe.throw(str(exc))

	# Append the Phase 2 Run row in Recording status.
	parent = frappe.get_doc("Optimus Session", parent_docname)
	parent.append("phase_2_runs", {
		"run_uuid": run_uuid,
		"status": "Recording",
		"started_at": now_datetime(),
		"picks_json": frappe.as_json([
			{"dotted_path": r["dotted_path"], "source": r.get("source", "freeform")}
			for r in resolved if r.get("eligible")
		]),
	})
	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	safe_commit()

	frappe.publish_realtime("phase_2_run_recording", {
		"session_uuid": session_uuid,
		"run_uuid": run_uuid,
	}, user=user)

	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"docname": parent_docname,
		"resolved_picks": resolved,
		"expansions": expansions,
		"auto_expanded": bool(auto_expand and expansions),
	}


@frappe.whitelist()
def force_stop_phase2() -> dict:
	"""Recovery endpoint: clears the calling user's phase-2 active flag and
	marks any of their in-flight Phase 2 Run rows as Failed.

	Idempotent — safe to call when nothing is stuck. Use this when the
	form rejects ``start_line_profile_pass`` with "phase-2 already
	active" and the previous run never reached Stop (worker crash, tab
	close, or interrupted reproduction).
	"""
	from optimus.line_profile import capture as _lp_capture

	user = _require_profiler_user()

	cleared_run = _lp_capture.is_active(user)
	# Always clear the flag, even if is_active returned None (defensive
	# against stale frappe.local caches mid-test or after worker recycle).
	_lp_capture.stop_line_profile_pass(cleared_run or "_unknown_", user)

	# Mark any Recording rows the user owns as Failed so the form's child
	# table reflects the recovery. We scope to rows where parent.user ==
	# the calling user so a System Manager hitting this doesn't sweep
	# other users' active runs.
	stuck_rows = frappe.db.sql(
		"""
		SELECT pp2r.name, pp2r.parent, pp2r.run_uuid
		FROM `tabOptimus Phase Two Run` pp2r
		JOIN `tabOptimus Session` ps ON ps.name = pp2r.parent
		WHERE pp2r.status = 'Recording'
		  AND ps.user = %s
		""",
		(user,),
		as_dict=True,
	)

	# v0.6.x: group stuck rows by their parent Optimus Session so each
	# parent doc is loaded + saved EXACTLY ONCE per batch (was N loads + N
	# saves when one session held multiple stuck runs — the common case
	# for a user spamming the picker).
	rows_by_parent: dict[str, list[dict]] = {}
	for row in stuck_rows:
		rows_by_parent.setdefault(row["parent"], []).append(row)

	failed = 0
	for parent_name, rows in rows_by_parent.items():
		try:
			parent = frappe.get_doc("Optimus Session", parent_name)
			matched_in_parent = 0
			wanted_uuids = {r["run_uuid"] for r in rows}
			for child in (parent.phase_2_runs or []):
				if child.run_uuid in wanted_uuids:
					child.status = "Failed"
					child.warnings_json = frappe.as_json([
						"Force-stopped by user via api.force_stop_phase2.",
					])
					child.ended_at = now_datetime()
					try:
						_lp_capture.cleanup_run(child.run_uuid)
					except Exception:
						frappe.log_error(
							title="force_stop_phase2 redis cleanup",
							message=f"{parent_name}/{child.run_uuid}",
						)
					matched_in_parent += 1
			if matched_in_parent:
				parent.flags.ignore_validate_update_after_submit = True
				parent.save(ignore_permissions=True)
				failed += matched_in_parent
		except Exception as exc:
			frappe.log_error(
				title="force_stop_phase2 parent save",
				message=f"{parent_name}: {exc}",
			)
	safe_commit()

	return {
		"cleared_active_flag": bool(cleared_run),
		"prior_run_uuid": cleared_run,
		"rows_marked_failed": failed,
	}


@frappe.whitelist()
def stop_line_profile_pass(run_uuid: str) -> dict:
	"""End a phase-2 run, mark it Analyzing, enqueue the analyzer."""
	from optimus.line_profile import capture as _lp_capture

	user = _require_profiler_user()

	# Find the run row + parent session. ``frappe.db.get_value`` with a
	# filter dict + ``as_dict=True`` is the right primitive for a single
	# child-table lookup on v16 — Frappe's ``get_list`` against child
	# DocTypes (``istable: 1``) occasionally returns rows where the
	# requested ``Select`` columns are stripped, which surfaced as a
	# ``KeyError: 'status'`` in production. ``_require_profiler_user()``
	# already gates the endpoint; we don't need ``get_list``'s
	# permission filter on top.
	row = frappe.db.get_value(
		"Optimus Phase Two Run",
		{"run_uuid": run_uuid},
		["name", "parent", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"Phase 2 run {run_uuid!r} not found.")
	if row.status != "Recording":
		frappe.throw(f"Phase 2 run is in status {row.status!r}, not Recording.")

	parent_docname = row.parent
	session_uuid = frappe.db.get_value("Optimus Session", parent_docname, "session_uuid")

	# Clear the active flag (capture won't instrument further requests).
	_lp_capture.stop_line_profile_pass(run_uuid, user)

	# Mark run Analyzing.
	parent = frappe.get_doc("Optimus Session", parent_docname)
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.status = "Analyzing"
			child.ended_at = now_datetime()
			break
	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	safe_commit()

	frappe.publish_realtime("phase_2_run_analyzing", {
		"session_uuid": session_uuid,
		"run_uuid": run_uuid,
	}, user=user)

	# When the scheduler is disabled (e.g. dev sites without
	# `bench start`), no RQ worker will pick up the long-queue job and
	# the run gets stuck in Analyzing. Mirror api.stop's inline fallback:
	# run the analyzer in-process so the request completes with results.
	from frappe.utils.scheduler import is_scheduler_disabled

	run_inline = False
	try:
		run_inline = bool(is_scheduler_disabled())
	except Exception:
		frappe.log_error(title="optimus phase-2 scheduler check")

	if run_inline:
		frappe.logger().warning(
			f"optimus: scheduler disabled; running phase-2 "
			f"analyze inline for run {run_uuid}. Caller will block."
		)
		from optimus.line_profile import analyzer as _lp_analyzer

		try:
			_lp_analyzer.run_analyze(session_uuid, run_uuid)
		except Exception as exc:
			# run_analyze marks the run Failed itself; surface the error
			# in the API response so the caller isn't silently puzzled.
			return {
				"run_uuid": run_uuid,
				"session_uuid": session_uuid,
				"status": "Failed",
				"error": str(exc),
				"ran_inline": True,
			}
		return {
			"run_uuid": run_uuid,
			"session_uuid": session_uuid,
			"status": "Ready",
			"ran_inline": True,
		}

	# Otherwise enqueue normally.
	frappe.enqueue(
		"optimus.line_profile.analyzer.run_analyze",
		queue="long",
		timeout=25 * 60,
		session_uuid=session_uuid,
		run_uuid=run_uuid,
	)

	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"status": "Analyzing",
	}


@frappe.whitelist()
def retry_phase2_analyze(run_uuid: str) -> dict:
	"""Re-trigger run_analyze for a Phase 2 Run row stuck in Analyzing or
	Failed. Useful when the original RQ enqueue never landed (no worker)
	or when the analyzer crashed and the user wants another shot.

	Resets the row to Analyzing, then runs inline so the response carries
	the final status (Ready or Failed) — no waiting for a worker to come
	online.
	"""
	from optimus.line_profile import analyzer as _lp_analyzer

	_require_profiler_user()

	# See ``stop_line_profile_pass`` for why this uses ``db.get_value``
	# instead of ``get_list`` (child-table ``get_list`` strips ``Select``
	# columns in v16, producing ``KeyError: 'status'`` at runtime).
	row = frappe.db.get_value(
		"Optimus Phase Two Run",
		{"run_uuid": run_uuid},
		["name", "parent", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"Phase 2 run {run_uuid!r} not found.")
	parent_docname = row.parent
	session_uuid = frappe.db.get_value("Optimus Session", parent_docname, "session_uuid")

	# Reset to Analyzing so the realtime event flow still makes sense.
	parent = frappe.get_doc("Optimus Session", parent_docname)
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.status = "Analyzing"
			break
	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	safe_commit()

	try:
		_lp_analyzer.run_analyze(session_uuid, run_uuid)
	except Exception as exc:
		return {
			"run_uuid": run_uuid,
			"session_uuid": session_uuid,
			"status": "Failed",
			"error": str(exc),
		}
	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"status": "Ready",
	}


@frappe.whitelist()
def retry_phase2_analyzes_batch(run_uuids) -> dict:
	"""Batch variant of ``retry_phase2_analyze`` — accepts a list of
	``run_uuid``s and retries each in a single server round-trip.

	v0.6.x: addresses the Lens-audit *"frappe.call(...) inside a loop"*
	finding on the form's "Retry Phase 2" affordance — instead of N
	client→server round-trips (one per stuck run, which is the common
	case when a worker died and every Analyzing row needs a kick), the
	UI fires ONE call and the loop runs server-side.

	Per-run failures are isolated: one bad retry doesn't abort the
	rest. The response carries a per-run status list so the UI can
	report a useful aggregate (``"3 of 5 ran Ready, 2 Failed"``)."""
	import json as _json

	_require_profiler_user()

	# Accept JSON-encoded list (Frappe's whitelisted-API arg marshalling
	# stringifies lists when they cross the request boundary) OR a real
	# Python list when called from another server-side helper.
	if isinstance(run_uuids, str):
		try:
			run_uuids = _json.loads(run_uuids)
		except (TypeError, ValueError):
			frappe.throw("run_uuids must be a JSON array of run-uuid strings.")
	if not isinstance(run_uuids, (list, tuple)) or not run_uuids:
		frappe.throw("run_uuids must be a non-empty list of run-uuid strings.")

	results: list[dict] = []
	for run_uuid in run_uuids:
		if not isinstance(run_uuid, str) or not run_uuid.strip():
			results.append({"run_uuid": run_uuid, "status": "Skipped",
			                "error": "empty / non-string run_uuid"})
			continue
		try:
			results.append(retry_phase2_analyze(run_uuid))
		except Exception as exc:
			# Don't let one bad row abort the rest of the batch.
			results.append({
				"run_uuid": run_uuid,
				"status": "Failed",
				"error": str(exc),
			})

	# Quick aggregate for the UI to render a single message.
	tallies = {"Ready": 0, "Failed": 0, "Analyzing": 0, "Skipped": 0}
	for r in results:
		st = r.get("status") or "Failed"
		tallies[st] = tallies.get(st, 0) + 1

	return {
		"count": len(results),
		"tallies": tallies,
		"results": results,
	}
