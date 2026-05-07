# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Whitelisted HTTP API for the profiler.

These endpoints are how the floating widget (Phase 5) and any custom
integrations talk to the profiler. They are decorated with
`@frappe.whitelist()` so they are reachable as
`/api/method/frappe_profiler.api.<name>`.

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

import frappe
from frappe.utils import now_datetime

from frappe_profiler import session

# Roles allowed to call the profiler API. System Manager is always allowed
# (Frappe's superuser role); Profiler User is our dedicated role created
# on install via install.after_install. Adding Administrator explicitly
# because frappe.get_roles("Administrator") doesn't include "System Manager".
ALLOWED_ROLES = {"System Manager", "Profiler User", "Administrator"}


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
	/api/method/frappe_profiler.api.start and start a session on
	themselves — the role check in the widget JS would be purely
	cosmetic.
	"""
	user = _require_user()
	if user == "Administrator":
		return user
	roles = set(frappe.get_roles(user))
	if not (ALLOWED_ROLES & roles):
		frappe.throw(
			"You need the Profiler User or System Manager role to use the profiler.",
			frappe.PermissionError,
		)
	return user


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
	        the Profiler Session form after the session completes.
	"""
	user = _require_profiler_user()

	# v0.3.0: clear any in-flight capture state from a previous request
	# on this worker BEFORE we look at session state, so leaked state
	# from a concurrent request doesn't influence the new session.
	from frappe_profiler import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: mirror for infra capture.
	from frappe_profiler import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	# If the user is already recording, gracefully stop the previous one.
	previous = session.get_active_session_for(user)
	if previous:
		_stop_session(user, previous)

	session_uuid = frappe.generate_hash(length=16)
	now = now_datetime()
	title = (label or "").strip() or f"Profiling session @ {now.strftime('%Y-%m-%d %H:%M:%S')}"

	# v0.4.0: auto-inherit baseline if one is pinned for this label
	auto_baseline = None
	try:
		auto_baseline = frappe.cache.get_value(_baseline_key(title))
	except Exception:
		pass

	# Create the DocType row in Recording state.
	doc_fields = {
		"doctype": "Profiler Session",
		"session_uuid": session_uuid,
		"title": title,
		"user": user,
		"status": "Recording",
		"started_at": now,
	}
	if auto_baseline:
		doc_fields["compared_to_session"] = auto_baseline
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

	Clears the Redis active pointer, marks the Profiler Session row as
	``Stopping``, and either enqueues the analyze job or runs it inline
	(v0.5.0: scheduler-aware fallback — see ``_enqueue_analyze``).

	Returns:
	    dict with ``stopped``, ``session_uuid``, ``docname``,
	    ``ran_inline``, and — only when ran_inline is True — the final
	    ``status`` from the Profiler Session row (``Ready`` or ``Failed``).

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
				"Profiler Session", docname, "status"
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
	    docname     — the Profiler Session docname, or None if not found
	    ran_inline  — True if the session is already finalized by the time
	                  we return (analyze ran synchronously or was rejected
	                  by the inline cap and marked Failed). False means
	                  analyze will run async.
	"""
	# v0.3.0: stop any in-flight pyinstrument session and clear capture
	# state on this worker before flipping the active flag, so a previous
	# in-flight capture from the same worker doesn't leak into a new
	# session started immediately after.
	from frappe_profiler import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: clear any leaked infra start snapshot from a previous
	# session on the same worker.
	from frappe_profiler import infra_capture

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
		"profiler_session_stopping",
		session_uuid=session_uuid,
		docname=docname,
		user=user,
	)

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

	  profiler_session_stopping    — user clicked Stop
	  profiler_session_analyzing   — analyze.run starting (may be delayed)
	  profiler_session_ready       — analyze finished successfully
	  profiler_session_failed      — analyze crashed

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
	"""Transition the Profiler Session row to the Stopping state.

	Returns the docname on success or None if no matching row exists.
	"""
	docname = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid, "user": user},
		"name",
	)
	if not docname:
		return None

	frappe.db.set_value(
		"Profiler Session",
		docname,
		{"status": "Stopping", "stopped_at": now_datetime()},
	)
	frappe.db.commit()
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
	(``profiler_inline_analyze_limit``, default 50) to stay within
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
	    docname: the Profiler Session docname for cap-exceeded failure
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
		frappe.log_error(title="frappe_profiler scheduler check")

	if run_inline:
		# Inline cap check — refuse huge sessions that would exceed
		# gunicorn's request timeout. Only applied when docname is
		# provided (all production callers provide it).
		if docname:
			cap = frappe.conf.get("profiler_inline_analyze_limit") or 50
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
						"Profiler Session",
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
					frappe.db.commit()
				except Exception:
					frappe.log_error(
						title="frappe_profiler inline cap mark Failed"
					)
				# The session is finalized (Failed). Return True so
				# the caller treats it like any other inline result.
				return True

		frappe.logger().warning(
			f"frappe_profiler: scheduler disabled; running analyze "
			f"inline for session {session_uuid}. Caller will block "
			f"until analyze completes."
		)
		try:
			frappe.enqueue(
				"frappe_profiler.analyze.run",
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
				title=f"frappe_profiler inline analyze {session_uuid}"
			)
		return True

	frappe.enqueue(
		"frappe_profiler.analyze.run",
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


def _frontend_xhr_key(session_uuid: str) -> str:
	return f"profiler:frontend:{session_uuid}:xhr"


def _frontend_vitals_key(session_uuid: str) -> str:
	return f"profiler:frontend:{session_uuid}:vitals"


@frappe.whitelist(methods=["POST"])
def submit_frontend_metrics(payload: str) -> dict:
	"""Receive a batch of frontend metrics (XHR timings + Web Vitals).

	Called by profiler_frontend.js at stop time (via frappe.call) and
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
				frappe.log_error(title="frappe_profiler frontend rpush (xhr)")
		# Tail-preferring trim: keep the last N entries.
		try:
			frappe.cache.ltrim(xhr_key, -SOFT_CAP_FRONTEND_XHR, -1)
			frappe.cache.expire_key(xhr_key, session.SESSION_TTL_SECONDS)
		except Exception:
			frappe.log_error(title="frappe_profiler frontend ltrim (xhr)")

	if vitals:
		for entry in vitals:
			try:
				frappe.cache.rpush(vitals_key, _json.dumps(entry, default=str))
			except Exception:
				frappe.log_error(title="frappe_profiler frontend rpush (vitals)")
		try:
			frappe.cache.ltrim(vitals_key, -SOFT_CAP_FRONTEND_VITALS, -1)
			frappe.cache.expire_key(vitals_key, session.SESSION_TTL_SECONDS)
		except Exception:
			frappe.log_error(title="frappe_profiler frontend ltrim (vitals)")

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

	Permission: any role that can use the profiler (Profiler User or
	System Manager). Doesn't expose session contents — just aggregate
	counts.
	"""
	_require_profiler_user()

	# Count by status
	rows = (
		frappe.db.sql(
			"SELECT status, COUNT(*) FROM `tabProfiler Session` GROUP BY status",
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
		FROM `tabProfiler Session`
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
			FROM `tabProfiler Session`
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
ONBOARDING_CACHE_PREFIX = "profiler:onboarding_seen:"
ONBOARDING_CACHE_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year


@frappe.whitelist()
def check_onboarding_seen() -> dict:
	"""Has the current user dismissed the onboarding toast?

	Also returns True if the user has any existing Ready Profiler Session
	row (they're an experienced user; suppress the toast).
	"""
	user = _require_user()
	# Suppress for experienced users — anyone with at least one Ready session
	try:
		existing = frappe.db.count(
			"Profiler Session",
			filters={"user": user, "status": "Ready"},
		)
		if existing and existing > 0:
			return {"seen": True}
	except Exception:
		pass
	flag = frappe.cache.get_value(f"{ONBOARDING_CACHE_PREFIX}{user}")
	return {"seen": bool(flag)}


@frappe.whitelist()
def mark_onboarding_seen() -> dict:
	"""Mark the onboarding toast as dismissed for the current user."""
	user = _require_user()
	frappe.cache.set_value(
		f"{ONBOARDING_CACHE_PREFIX}{user}",
		"1",
		expires_in_sec=ONBOARDING_CACHE_TTL_SECONDS,
	)
	return {"seen": True}


# v0.4.0: baseline-pinning cache key prefix.
BASELINE_CACHE_PREFIX = "profiler:baseline:"


def _baseline_key(label: str) -> str:
	return f"{BASELINE_CACHE_PREFIX}{label}"


def _require_session_owner_or_sysmanager(session_uuid: str) -> dict:
	"""Permission gate shared by all v0.4.0 baseline / pdf endpoints.

	Returns the session row dict on success; throws on failure.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status", "title"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")
	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only modify your own sessions.", frappe.PermissionError
		)
	return row


@frappe.whitelist()
def pin_baseline(session_uuid: str) -> dict:
	"""Pin this session as the baseline for its label.

	Writes the docname to a site-level cache key
	`profiler:baseline:<label>`. Clears the is_baseline flag on any
	previously-pinned session for the same label. Sets is_baseline=1 on
	the target.
	"""
	row = _require_session_owner_or_sysmanager(session_uuid)
	if row["status"] != "Ready":
		frappe.throw(f"Cannot pin session in '{row['status']}' state")

	label = row["title"] or ""
	key = _baseline_key(label)

	# Clear flag on any previously-pinned session for the same label
	previous = frappe.cache.get_value(key)
	if previous and previous != row["name"]:
		try:
			frappe.db.set_value("Profiler Session", previous, "is_baseline", 0)
		except Exception:
			pass

	# Set the new baseline
	frappe.cache.set_value(key, row["name"])
	frappe.db.set_value("Profiler Session", row["name"], "is_baseline", 1)
	frappe.db.commit()

	# Re-render any dependent sessions in the background (best-effort)
	try:
		frappe.enqueue(
			"frappe_profiler.api._rerender_dependents",
			queue="short",
			label=label,
			baseline_docname=row["name"],
		)
	except Exception:
		pass

	return {"pinned": True, "session_uuid": session_uuid, "docname": row["name"]}


@frappe.whitelist()
def unpin_baseline(session_uuid: str) -> dict:
	"""Clear the baseline flag for this session and remove cache entry if active."""
	row = _require_session_owner_or_sysmanager(session_uuid)

	label = row["title"] or ""
	key = _baseline_key(label)

	current = frappe.cache.get_value(key)
	if current == row["name"]:
		frappe.cache.delete_value(key)
	frappe.db.set_value("Profiler Session", row["name"], "is_baseline", 0)
	frappe.db.commit()

	return {"unpinned": True, "session_uuid": session_uuid}


def _rerender_dependents(label: str, baseline_docname: str) -> None:
	"""Re-render reports for any Ready sessions whose label matches AND whose
	compared_to_session is NULL or matches the new baseline.

	Background job triggered by pin_baseline. Best-effort.
	"""
	try:
		dependent_names = frappe.get_all(
			"Profiler Session",
			filters={"title": label, "status": "Ready"},
			pluck="name",
		)
	except Exception:
		return

	for name in dependent_names:
		if name == baseline_docname:
			continue
		try:
			doc = frappe.get_doc("Profiler Session", name)
			if not doc.compared_to_session:
				doc.db_set("compared_to_session", baseline_docname, update_modified=False)
			from frappe_profiler.analyze import _render_and_attach_reports

			_render_and_attach_reports(name, recordings=[])
		except Exception:
			frappe.log_error(title=f"frappe_profiler rerender {name}")


@frappe.whitelist()
def set_comparison(session_uuid: str, compared_to: str) -> dict:
	"""Set compared_to_session on a single session for a one-off comparison.

	The current session must be Ready; the compared_to session is looked up
	by docname.
	"""
	row = _require_session_owner_or_sysmanager(session_uuid)
	if not compared_to:
		frappe.throw("compared_to is required")
	target_status = frappe.db.get_value("Profiler Session", compared_to, "status")
	if not target_status:
		frappe.throw(f"No Profiler Session found with name {compared_to}")
	if target_status != "Ready":
		frappe.throw(f"Compared-to session must be Ready, got '{target_status}'")

	frappe.db.set_value("Profiler Session", row["name"], "compared_to_session", compared_to)
	frappe.db.commit()
	return {"set": True, "session_uuid": session_uuid, "compared_to": compared_to}


@frappe.whitelist()
def download_pdf(session_uuid: str) -> dict:
	"""Return the URL of the safe-report PDF, generating it on first call.

	Permission: recording user, System Manager, or Administrator.
	Mirrors retry_analyze / export_session permission gating.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")
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

	from frappe_profiler import pdf_export

	url = pdf_export.get_or_generate_pdf(session_uuid)
	return {"file_url": url}


@frappe.whitelist()
def export_session(session_uuid: str) -> dict:
	"""Export a Profiler Session as a structured JSON blob.

	Lets dev shops (or automation) consume the profiler's output
	programmatically without parsing the HTML report. Returns the full
	session including all child rows, top queries, table breakdown, and
	finding technical details — effectively everything from the safe
	report in a machine-friendly shape.

	Permission model: mirrors the "raw report" gate — only the recording
	user or a System Manager can export. Other Profiler Users get a
	permission error even if they somehow guessed the uuid.
	"""
	import json

	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		"name",
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")

	doc = frappe.get_doc("Profiler Session", row["name"])

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

	doc = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not doc:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")

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
		"Profiler Session",
		doc["name"],
		{"status": "Stopping", "analyzer_warnings": None},
	)
	frappe.db.commit()

	# v0.4.0: clear the cached PDF so the next download regenerates from
	# the fresh HTML produced by the re-run analyze.
	try:
		from frappe_profiler import pdf_export

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
				"Profiler Session", doc["name"], "status"
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
def regenerate_reports(session_uuid: str) -> dict:
	"""Re-render the safe + raw HTML reports from stored session data.

	Unlike ``retry_analyze``, this does NOT re-run any analyzer. It
	only invokes ``renderer.render_safe/render_raw`` on the existing
	Profiler Session row. Typical use: the report template or
	renderer code changed (e.g. upgrading frappe_profiler to a new
	version with an improved layout) and you want the new UI applied
	to an already-analyzed session without the cost of re-running
	the entire analysis pipeline.

	Characteristics:

	  - **Fast** — milliseconds vs. minutes. No DB-heavy analyzer
	    passes, no EXPLAIN calls. Just template rendering.
	  - **Safe to run repeatedly** — idempotent. Each invocation
	    replaces the existing safe/raw File attachments and clears
	    the cached PDF.
	  - **Best-effort recordings** — fetches the original recordings
	    from Redis by UUID. If they've expired (TTL exceeded),
	    per-query drill-down / Full recordings sections render empty
	    but every other section (findings, stats, hot frames, exec
	    summary, analyzer notes) stays intact because those are
	    persisted on the Profiler Session row.
	  - **Allowed on Ready OR Failed sessions** — re-rendering a
	    Failed session whose analyze partially completed is often
	    how this feature pays for itself (unblocks a demo when a
	    render-time bug was fixed).

	Permission: recording user OR System Manager.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")

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

	# Best-effort recording fetch. Safe renderer uses DocType fields for
	# everything important; recordings only power the per-query drill-
	# down + Full recordings sections. If Redis dropped them, render
	# with an empty list and the rest of the report is still fine.
	from frappe_profiler import analyze as _analyze_mod

	doc = frappe.get_doc("Profiler Session", row["name"])
	recording_uuids = [
		a.recording_uuid
		for a in (doc.actions or [])
		if getattr(a, "recording_uuid", None)
	]
	try:
		recordings = list(_analyze_mod._fetch_recordings(recording_uuids))
	except Exception:
		frappe.log_error(title="frappe_profiler regenerate_reports fetch")
		recordings = []

	# Invalidate the cached PDF — next /api/method/download_pdf call
	# will regenerate it from the freshly-rendered safe HTML.
	try:
		from frappe_profiler import pdf_export

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
def get_installed_apps_for_tracking() -> list[str]:
	"""Return the bench's installed apps for the Profiler Settings
	▸ Tracked Apps Autocomplete field.

	Excludes ``frappe_profiler`` (the profiler's own callsites are
	already filtered regardless of user config, and listing it here
	would suggest tracking the tool that's doing the tracking).

	Restricted to System Manager since Profiler Settings itself is.
	"""
	if "System Manager" not in (frappe.get_roles() or []):
		frappe.throw(
			"Only System Manager can list installed apps for the "
			"Profiler Settings picker."
		)
	apps = frappe.get_installed_apps() or []
	return [app for app in apps if app != "frappe_profiler"]


# ---------------------------------------------------------------------------
# Phase-2 line profiler API
# ---------------------------------------------------------------------------
# Three whitelisted endpoints that the Profiler Session form's "Phase 2:
# Line Profile" section calls into:
#
#   get_phase2_candidates(session_uuid) — populate the curated picker
#   start_line_profile_pass(session_uuid, picks) — begin a phase-2 run
#   stop_line_profile_pass(run_uuid) — end the run, enqueue analyze
#
# Phase-2 implementation lives in frappe_profiler.line_profile.* —
# this surface is the thin transport layer.


@frappe.whitelist()
def get_phase2_candidates(session_uuid: str) -> dict:
	"""Return the curated candidate list for the phase-2 picker UI.

	Reads the parent Profiler Session's actions, parses each action's
	``call_tree_json``, and builds a top-30 list of frames from user-app
	code (with framework apps surfaced separately under "observations").
	"""
	import json as _json

	from frappe_profiler.line_profile import capture as _lp_capture
	from frappe_profiler.line_profile import picker as _lp_picker

	_require_profiler_user()

	parent_docname = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		"name",
	)
	if not parent_docname:
		frappe.throw(f"Profiler Session {session_uuid!r} not found.")

	doc = frappe.get_doc("Profiler Session", parent_docname)

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

	candidates = _lp_picker._build_candidates_from_trees(trees, doc.findings or [])

	return {
		"session_uuid": session_uuid,
		"docname": parent_docname,
		"candidates": [c for c in candidates if not c["is_framework"]],
		"observations": [c for c in candidates if c["is_framework"]],
		"line_profiler_available": _lp_capture.is_line_profiler_available(),
	}


@frappe.whitelist()
def start_line_profile_pass(session_uuid: str, picks) -> dict:
	"""Begin a phase-2 line-profile run on a finished session.

	``picks`` is a JSON-encoded (or already-parsed) list of
	``{dotted_path, source}`` entries the customer ticked / typed.
	"""
	import json as _json
	import uuid as _uuid

	from frappe_profiler.line_profile import capture as _lp_capture

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

	# Phase-1 must not be active for the same user — phase 1 and phase 2
	# read separate Redis flags but only one can be active at a time.
	if session.get_active_session_for(user):
		frappe.throw(
			"You currently have a phase-1 session recording. Stop it before "
			"starting a phase-2 line-profile run.",
		)

	# Look up the parent Profiler Session and verify it's Ready.
	parent_docname = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		"name",
	)
	if not parent_docname:
		frappe.throw(f"Profiler Session {session_uuid!r} not found.")
	parent_status = frappe.db.get_value("Profiler Session", parent_docname, "status")
	if parent_status != "Ready":
		frappe.throw(
			f"Phase-2 requires a finished session (status=Ready); current "
			f"status is {parent_status!r}.",
		)

	# Reject if the user already has a phase-2 run in flight elsewhere.
	if _lp_capture.is_active(user):
		frappe.throw("You already have a phase-2 line-profile run active.")

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
	parent = frappe.get_doc("Profiler Session", parent_docname)
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
	frappe.db.commit()

	frappe.publish_realtime("phase_2_run_recording", {
		"session_uuid": session_uuid,
		"run_uuid": run_uuid,
	}, user=user)

	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"docname": parent_docname,
		"resolved_picks": resolved,
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
	from frappe_profiler.line_profile import capture as _lp_capture

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
		FROM `tabProfiler Phase 2 Run` pp2r
		JOIN `tabProfiler Session` ps ON ps.name = pp2r.parent
		WHERE pp2r.status = 'Recording'
		  AND ps.user = %s
		""",
		(user,),
		as_dict=True,
	)

	failed = 0
	for row in stuck_rows:
		try:
			parent = frappe.get_doc("Profiler Session", row["parent"])
			for child in (parent.phase_2_runs or []):
				if child.run_uuid == row["run_uuid"]:
					child.status = "Failed"
					child.warnings_json = frappe.as_json([
						"Force-stopped by user via api.force_stop_phase2.",
					])
					child.ended_at = now_datetime()
					_lp_capture.cleanup_run(row["run_uuid"])
					break
			parent.flags.ignore_validate_update_after_submit = True
			parent.save(ignore_permissions=True)
			failed += 1
		except Exception as exc:
			frappe.log_error(
				title="force_stop_phase2 row save",
				message=f"{row['name']}: {exc}",
			)
	frappe.db.commit()

	return {
		"cleared_active_flag": bool(cleared_run),
		"prior_run_uuid": cleared_run,
		"rows_marked_failed": failed,
	}


@frappe.whitelist()
def stop_line_profile_pass(run_uuid: str) -> dict:
	"""End a phase-2 run, mark it Analyzing, enqueue the analyzer."""
	from frappe_profiler.line_profile import capture as _lp_capture

	user = _require_profiler_user()

	# Find the run row + parent session.
	rows = frappe.get_all(
		"Profiler Phase 2 Run",
		filters={"run_uuid": run_uuid},
		fields=["name", "parent", "status"],
		limit=1,
	)
	if not rows:
		frappe.throw(f"Phase 2 run {run_uuid!r} not found.")
	row = rows[0]
	if row["status"] != "Recording":
		frappe.throw(f"Phase 2 run is in status {row['status']!r}, not Recording.")

	parent_docname = row["parent"]
	session_uuid = frappe.db.get_value("Profiler Session", parent_docname, "session_uuid")

	# Clear the active flag (capture won't instrument further requests).
	_lp_capture.stop_line_profile_pass(run_uuid, user)

	# Mark run Analyzing.
	parent = frappe.get_doc("Profiler Session", parent_docname)
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.status = "Analyzing"
			child.ended_at = now_datetime()
			break
	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	frappe.db.commit()

	# Enqueue the analyzer.
	frappe.enqueue(
		"frappe_profiler.line_profile.analyzer.run_analyze",
		queue="long",
		timeout=25 * 60,
		session_uuid=session_uuid,
		run_uuid=run_uuid,
	)

	frappe.publish_realtime("phase_2_run_analyzing", {
		"session_uuid": session_uuid,
		"run_uuid": run_uuid,
	}, user=user)

	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"status": "Analyzing",
	}
