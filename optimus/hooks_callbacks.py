# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Frappe lifecycle hook callbacks.

These functions are wired into `before_request` / `after_request` (and in
Phase 2, `before_job` / `after_job`) via `hooks.py`. Their job is to:

1. Decide whether the current request belongs to an active profiler session.
2. If yes, activate `frappe.recorder` for this request only (per-user
   isolation — other users' concurrent traffic is NOT recorded).
3. After the request, register the new recording UUID with the session
   so the analyze pipeline can find all recordings that belong to a flow.

Activation strategy
-------------------
We do NOT call `frappe.recorder.start()`, because that flips a global flag
that records every request from every user. Instead, we directly invoke
`frappe.recorder.record(force=True)` only when the current user has an
active profiler session.

Hook order
----------
Frappe loads `frappe`'s own hooks first, then app hooks. So on each request:

  1. `frappe.recorder.record()`        — frappe's own (no-op without flag)
  2. `optimus.hooks_callbacks.before_request()` — ours, may activate
  3. <request handling>
  4. `frappe.recorder.dump()`          — frappe's own, dumps the recorder
                                          we activated
  5. `optimus.hooks_callbacks.after_request()` — ours, registers
                                          the new UUID with the session

Step 4 happens before step 5 because frappe's `after_request` runs before
ours (loaded first). That ordering is essential — we need the recording
to be dumped to Redis before we ask the analyze pipeline to fetch it later.
"""

import time

import frappe
import frappe.recorder  # imported at module top so function-local `import frappe.recorder` doesn't rebind `frappe` as a local variable (Python scope rule: any `import frappe.X` inside a function makes `frappe` a function-local for the entire scope, breaking earlier `frappe.local` reads — caused by Python 3.14 stricter scope detection on a pre-existing pattern)

from optimus import capture as _capture
from optimus import session

# v0.5.1: Skip-list for instrumentation-noise endpoints. Any HTTP request
# whose ``cmd`` starts with one of these prefixes is NOT recorded against
# the user's active profiler session, even though everything else about
# the request (auth, user, active session) would normally mean "record me".
#
# Why: these are the profiler's own widget-polling calls and Frappe's own
# Recorder doctype whitelisted methods. They fire during an active session
# but represent instrumentation overhead, not application work the user is
# trying to optimize. Capturing them pollutes:
#
#   - per_action rows (N extra "optimus.api.status" rows per session)
#   - top_queries / table_breakdown (queries from status polling / recorder
#     lookups obscure the real hot spots)
#   - the auto-generated "Steps to Reproduce" bullet list — the widget
#     polls every ~2s while Recording, so a 30-second flow ends up with
#     a reproducer list that's 90% status polls
#   - total wall-clock / total query totals on the Optimus Session form
#
# Filtered at capture time (before_request) rather than at display time so
# ALL downstream analyzers see a clean recording list, not just auto-notes.
_IGNORED_CMD_PREFIXES = (
	# The profiler's own whitelisted API — widget poll, metrics submit,
	# retry, fetch, etc. None of these represent real application work.
	"optimus.api.",
	# Frappe's built-in Recorder doctype. If the user has the Recorder UI
	# open in another tab while profiling (not uncommon — devs often have
	# both tools handy), its whitelisted calls (export_data, delete,
	# get_request_details, pluck, start, stop) would otherwise be captured
	# and attributed to the profiling session. They're the recorder's own
	# plumbing, not user code.
	"frappe.core.doctype.recorder.recorder.",
)


def _extract_cmd_from_request() -> str:
	"""Resolve the whitelisted-method name for the current HTTP request,
	or "" if we can't determine one.

	Two sources, checked in order:

	1. ``frappe.local.form_dict.cmd`` — set by ``make_form_dict`` for
	   legacy ``?cmd=foo.bar`` RPC calls. Available at before_request
	   time because ``make_form_dict`` runs BEFORE the hook dispatcher.

	2. ``frappe.local.request.path`` parsed for ``/method/<name>`` — the
	   only source for modern ``/api/method/foo.bar`` and
	   ``/api/v2/method/foo.bar`` URLs, because Frappe's REST API routing
	   (``handle_rpc_call`` in ``frappe/api/v1.py`` and ``v2.py``) only
	   calls ``frappe.form_dict.cmd = method`` AFTER the before_request
	   hooks have already fired. Before v0.5.1 the skip filter missed
	   every modern REST call because it only checked form_dict.cmd,
	   which was empty at hook time for these URLs.

	Works for both v1 and v2 API paths because both route shapes use
	``.../method/<name>`` — we find the substring ``/method/`` and take
	everything after it.

	Returns "" when neither source produces a non-empty cmd — the caller
	treats that as "don't skip" so that request-path-less contexts
	(OPTIONS preflights, health checks, pre-init edge cases) fall
	through to the normal path rather than being filtered.
	"""
	# Source 1: form_dict.cmd (legacy ?cmd=foo.bar, always set if present)
	try:
		form_dict = getattr(frappe.local, "form_dict", None)
		if isinstance(form_dict, dict):
			cmd = form_dict.get("cmd")
			if cmd:
				return cmd
	except Exception:
		pass

	# Source 2: parse out of request.path for /api/method/<foo> and
	# /api/v2/method/<foo>. Both use the substring "/method/".
	try:
		request = getattr(frappe.local, "request", None)
		if request is None:
			return ""
		path = getattr(request, "path", "") or ""
		marker = "/method/"
		idx = path.find(marker)
		if idx < 0:
			return ""
		rest = path[idx + len(marker):]
		# Defensive: strip any trailing slash and query string (werkzeug
		# already strips the query from .path, but trailing slashes are
		# not uncommon on REST URLs).
		rest = rest.split("?", 1)[0].rstrip("/")
		return rest
	except Exception:
		return ""


def _should_skip_request() -> bool:
	"""Return True if the current HTTP request is profiler / Frappe-recorder
	instrumentation noise that should not be captured into the session.

	Delegates to ``_extract_cmd_from_request`` for the method-name
	resolution (handles both legacy ``?cmd=foo`` and modern
	``/api/method/foo`` URL shapes), then does a prefix match against
	``_IGNORED_CMD_PREFIXES``. Non-method URLs (``/app/...``,
	``/api/resource/...``, static files) resolve to "" and fall through
	as 'not noise', which is the intended behavior — we only skip
	endpoints that the profiler / recorder EXPOSES via whitelisted
	methods, not general page loads or REST resource access.

	Defensive: any exception resolving the cmd falls through to False
	rather than raising, because crashing here would take down every
	request for every user with an active profiler session.
	"""
	cmd = _extract_cmd_from_request()
	if cmd:
		for prefix in _IGNORED_CMD_PREFIXES:
			if cmd.startswith(prefix):
				return True

	# v0.6.0 Round 6: also honor user-configured skip patterns from
	# Optimus Settings. Match against the FULL request path (not just
	# the cmd), so admins can skip non-method URLs too (e.g. their
	# health-check endpoint at /healthz).
	try:
		from optimus.settings import get_config
		patterns = get_config().skip_request_paths
		if patterns:
			path = ""
			try:
				request = getattr(frappe.local, "request", None)
				path = (getattr(request, "path", "") or "")
			except Exception:
				path = ""
			if path:
				for pat in patterns:
					if pat and path.startswith(pat):
						return True
	except Exception:
		pass

	return False


def _resolve_sampler_interval_ms() -> float:
	"""Resolve the pyinstrument sampler interval from Optimus Settings,
	falling back to the legacy site_config key, then to the hardcoded
	default. Single function so before_request + before_job stay in
	sync without duplicating the precedence rule.
	"""
	# Setting first (preferred path).
	try:
		from optimus.settings import get_config
		v = float(get_config().pyinstrument_sampler_interval_ms or 0)
		if v > 0:
			return v
	except Exception:
		pass
	# Legacy site_config fallback for pre-v0.6.0 deployments that tuned
	# this knob without saving the Single doc.
	try:
		v = frappe.conf.get("optimus_sampler_interval_ms")
		if v:
			return float(v)
	except Exception:
		pass
	return float(_capture.DEFAULT_SAMPLER_INTERVAL_MS)


def _should_skip_user(user: str | None) -> bool:
	"""Return True if the current user is on the configured skip list.
	Used to exclude system bot users (scheduler, healthchecks) from
	instrumentation even when they have an active session.
	"""
	if not user:
		return False
	try:
		from optimus.settings import get_config
		skip = get_config().skip_users
		return user in skip
	except Exception:
		return False


def before_request(*args, **kwargs):
	"""Activate the recorder if the current user has an active profiler session.

	Runs on every HTTP request. The hot path (no active session) is one
	Redis GET — `get_active_session_for(user)` — and an early return.
	"""
	try:
		# v0.5.3: deferred sidecar-wrap install. The module-level
		# install in optimus/__init__.py skips when frappe
		# isn't fully initialized (to avoid breaking the bench test
		# runner's bootstrap — see the rationale there). By the time
		# the first real before_request fires, frappe is guaranteed
		# to be up, so we lazy-install here. Idempotent via the
		# wrap's own `_profiler_original` marker.
		import optimus
		optimus._try_install_capture_wraps()

		# v0.5.2: master kill-switch. When an admin turns off
		# Optimus Settings ▸ Enabled, we short-circuit before even
		# the active-session lookup. `is_enabled()` is a cached
		# read (same Redis value reused across all requests until a
		# setting change invalidates it), so the hot-path cost is a
		# single Redis GET regardless of state.
		from optimus.settings import is_enabled
		if not is_enabled():
			return

		# Round 2 fix #6: if the analyze pipeline itself is running (it
		# does DB writes to the Optimus Session doctype, which may
		# trigger nested queries), don't recursively activate recording
		# on those internal operations. The flag is set at the top of
		# analyze.run() and cleared in its finally block.
		if getattr(frappe.local, "optimus_analyzing", False):
			return

		user = getattr(frappe.session, "user", None)
		if not user or user == "Guest":
			return

		# v0.6.0 Round 6: admin-configured user skip list. Checked early
		# so a system bot user with an active session (rare but possible
		# via API token) doesn't get profiled.
		if _should_skip_user(user):
			return

		session_uuid = session.get_active_session_for(user)
		if not session_uuid:
			return  # 99.9% of requests exit here

		# v0.5.1: Skip profiler / Frappe-recorder instrumentation noise.
		# Checked AFTER the active-session lookup (two Redis GETs gating a
		# string-prefix check is free) so there's zero cost on the 99.9%
		# path. See _IGNORED_CMD_PREFIXES for the full rationale.
		if _should_skip_request():
			return

		# Tag the request so after_request can pick it up.
		frappe.local.optimus_session_id = session_uuid

		# If frappe's own recorder already activated (someone has the
		# standalone Recorder UI running globally), piggyback on its
		# instance — do NOT create a second Recorder because that would
		# overwrite frappe.local._recorder and orphan the first one's
		# SQL patch, corrupting both recordings.
		if getattr(frappe.local, "_recorder", None) is not None:
			return

		# Force-activate the existing recorder for THIS request only.
		# We pass force=True so the recorder runs regardless of the
		# global RECORDER_INTERCEPT_FLAG, leaving the standalone
		# Recorder UI's flag untouched.
		# (frappe.recorder is imported at module top — see comment there.)
		frappe.recorder.record(force=True)

		# v0.5.1: snapshot infra metrics FIRST, BEFORE pyinstrument starts.
		# Pre-v0.5.1 the order was reversed: _start_pyi_session was called
		# here and THEN infra_capture.snapshot() ran — which meant pyi
		# captured its own ~30ms SHOW GLOBAL STATUS / psutil work as part
		# of the user's action. A production report on a 47ms realtime
		# subscribe request showed 31ms (67%!) attributed to
		# optimus/infra_capture.py::_read_db in the call tree,
		# making it look like the profiler was the bottleneck.
		#
		# Moving the snapshot before _start_pyi_session means pyi never
		# samples the snapshot code path — its first sample lands after
		# before_request returns, in the actual request handler. Fast
		# actions (<50ms) now show true user-code time instead of being
		# dominated by instrumentation overhead.
		try:
			from optimus import infra_capture
			frappe.local.optimus_infra_start = infra_capture.snapshot()
		except Exception:
			frappe.log_error(title="optimus infra start snapshot")

		# v0.3.0: gate the new pyinstrument + sidecar capture on the
		# session's capture_python_tree flag. Setting
		# _profiler_active_session_id is what activates the wraps
		# (they gate on its presence in their hot path). When the
		# flag is False we leave it unset and SQL-only capture proceeds.
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("capture_python_tree", True):
			frappe.local._profiler_active_session_id = session_uuid
			_capture._start_pyi_session(
				local_proxy=frappe.local,
				interval_ms=_resolve_sampler_interval_ms(),
			)
	except Exception:
		# Never let a profiler bug break a customer request. Log and move on.
		frappe.log_error(title="optimus before_request")


def after_request(*args, **kwargs):
	"""Register the dumped recording UUID with the active profiler session.

	Runs after `frappe.recorder.dump()` (which writes the recording into
	`RECORDER_REQUEST_HASH`). We just need to remember which session this
	recording belongs to.
	"""
	try:
		session_uuid = getattr(frappe.local, "optimus_session_id", None)
		if not session_uuid:
			return

		recorder = getattr(frappe.local, "_recorder", None)
		if recorder is None:
			return

		recording_uuid = getattr(recorder, "uuid", None)
		if not recording_uuid:
			return

		# Pass the current user so register_recording can refresh the
		# active-session TTL (Round 2 fix #2) without re-reading meta.
		user = getattr(frappe.session, "user", None)
		registered = session.register_recording(
			session_uuid, recording_uuid, user=user
		)
		if not registered:
			# Cap hit — the recording is in RECORDER_REQUEST_HASH but not
			# registered against our session. The cap_warning is already
			# written to session meta; log here so the drop is visible
			# in the error log / journalctl for debugging.
			frappe.logger().warning(
				f"optimus: recording cap hit for session "
				f"{session_uuid}, dropped {recording_uuid}"
			)
	except Exception:
		frappe.log_error(title="optimus after_request")
	finally:
		# v0.3.0: dump pyinstrument session and sidecar log to Redis under
		# per-recording-UUID keys. Best-effort — failures here log but
		# never break the request.
		recording_uuid_for_dump = getattr(
			getattr(frappe.local, "_recorder", None), "uuid", None
		)
		_dump_capture_state_to_redis(recording_uuid=recording_uuid_for_dump)

		# v0.5.0: write the infra diff to Redis for this recording.
		# Consumed by the infra_pressure analyzer at analyze time.
		try:
			start_snap = getattr(frappe.local, "optimus_infra_start", None)
			if start_snap and recording_uuid_for_dump:
				from optimus import infra_capture
				end_snap = infra_capture.snapshot()
				from optimus import redis_keys
				frappe.cache.set_value(
					redis_keys.infra(recording_uuid_for_dump),
					infra_capture.diff(start_snap, end_snap),
					expires_in_sec=session.SESSION_TTL_SECONDS,
				)
		except Exception:
			frappe.log_error(title="optimus infra end snapshot")

		# v0.5.0: correlation header for optimus_frontend.js. Must set
		# Access-Control-Expose-Headers or browsers will refuse to surface
		# the custom header to JavaScript, even for same-origin requests.
		#
		# Gated on `optimus_session_id` specifically (not just the
		# recorder UUID) — the standalone Frappe Recorder UI may be
		# activated globally on this site, which sets frappe.local._recorder
		# and gives us a recording UUID, but THAT recording doesn't belong
		# to any profiler session. Injecting the header for non-session
		# traffic would:
		#   1. Pollute every response across the site with a useless header
		#   2. Cause optimus_frontend.js to buffer XHR timings tagged to
		#      a recording UUID that has no session to flush them to
		try:
			optimus_session_id = getattr(
				frappe.local, "optimus_session_id", None
			)
			if recording_uuid_for_dump and optimus_session_id:
				# v0.5.3: pass the response object from the hook
				# kwargs (Frappe passes `response=response,
				# request=request` via run_after_request_hooks).
				# Required for v15 compat — v15 doesn't stage
				# headers on frappe.local.response_headers, so
				# we set them directly on response.headers.
				_inject_correlation_header(
					recording_uuid_for_dump,
					response=kwargs.get("response"),
				)
		except Exception:
			frappe.log_error(title="optimus header injection")

		# Clear the per-request markers so they don't leak across requests
		# (frappe.local is per-request anyway, but explicit is good).
		if hasattr(frappe.local, "optimus_session_id"):
			del frappe.local.optimus_session_id
		if hasattr(frappe.local, "optimus_infra_start"):
			try:
				delattr(frappe.local, "optimus_infra_start")
			except AttributeError:
				pass


# ---------------------------------------------------------------------------
# Background job hooks (Phase 2)
# ---------------------------------------------------------------------------
# These mirror the request hooks above but use the `_profiler_session_id`
# kwarg injected by the frappe.enqueue monkey-patch in __init__.py to
# decide whether to activate recording for this job.
#
# Hook order on each job (frappe loads first, our app loads after):
#
#   1. frappe.recorder.record       (frappe's own — no-op without flag)
#   2. frappe.monitor.start         (frappe's own — unrelated)
#   3. optimus.before_job   (ours — may activate via force=True)
#   4. <method runs>
#   5. frappe.recorder.dump         (frappe's own — dumps the recorder we activated)
#   6. frappe.monitor.stop          (frappe's own — unrelated)
#   7. frappe.utils.file_lock.release_document_locks
#   8. optimus.after_job    (ours — registers UUID with session)
#
# The kwargs dict is passed by reference from frappe.utils.background_jobs.execute_job,
# so popping `_profiler_session_id` here removes it from the dict that the
# user's method will receive. This is essential — without popping, methods
# whose signatures don't include **kwargs would crash with an unexpected
# keyword argument error.


def before_job(method=None, kwargs=None, **rest):
	"""Activate the recorder if this job belongs to an active profiler session."""
	try:
		# v0.5.3: same deferred-install path as before_request. A job
		# worker process boots independently of the web request path,
		# so it needs its own chance to finalize wrap installation
		# after frappe is fully ready.
		import optimus
		optimus._try_install_capture_wraps()

		# v0.5.2: honor the master kill-switch here too. If the admin
		# turned the profiler off, we still need to pop our marker
		# from kwargs so the user's method doesn't see an unexpected
		# keyword argument — that happens below after the kill-switch
		# check short-circuits the recorder activation path.
		from optimus.settings import is_enabled
		if not is_enabled():
			# Still pop the marker so the downstream method signature
			# stays clean (see the pop comment below for why).
			if isinstance(kwargs, dict):
				kwargs.pop("_profiler_session_id", None)
			return

		# Round 2 fix #6: don't recurse into our own analyze job. The
		# analyze.run job sets frappe.local.optimus_analyzing = True
		# at its top, so we exit here if this job IS our analyze.
		if getattr(frappe.local, "optimus_analyzing", False):
			return

		if kwargs is None:
			return
		if not isinstance(kwargs, dict):
			# Unexpected — frappe should always pass a dict. Log once so
			# we can debug if it ever happens in practice.
			frappe.log_error(
				title="optimus before_job unexpected kwargs",
				message=f"kwargs type: {type(kwargs).__name__}, method: {method}",
			)
			return

		# Pop our marker so the user's method doesn't see it. This MUST
		# happen regardless of whether we proceed to activate recording —
		# if we leave the marker in kwargs, the user's method will be
		# called with an unexpected keyword argument and crash.
		session_uuid = kwargs.pop("_profiler_session_id", None)
		if not session_uuid:
			return

		# v0.7.x+: record this job's method + Running/started_at in the
		# session's jobs hash BEFORE the user/active-session gates — so the
		# bg-jobs report shows it even when we don't activate the recorder
		# for it (orphan case: ``active != session_uuid`` because the user
		# started a new session after Stop, but the old session's job is
		# still in flight). Also fixes the ``enqueue_after_commit=True``
		# blank-method bug: the enqueue patch can't record the method when
		# ``frappe.enqueue`` returns None for deferred dispatch, so before_job
		# is the first reliable site that has BOTH the method arg and the
		# real RQ job id (via ``rq.get_current_job()``). Stash the session
		# on frappe.local so after_job can find it even if the active-session
		# gate below short-circuits.
		_track_bg_job_started(session_uuid, method)
		frappe.local.optimus_bg_session_uuid = session_uuid

		# frappe.set_user(user) was already called by execute_job before
		# this hook fires, so frappe.session.user is the originating user.
		user = getattr(frappe.session, "user", None)
		if not user or user == "Guest":
			return

		# v0.6.0 Round 6: honor the configured user skip list for jobs
		# too. A bot user that runs scheduled jobs via API token should
		# stay out of every capture path.
		if _should_skip_user(user):
			return

		# Verify the session is still recordable for this user. The active
		# pointer matches while the session is running. After Stop the
		# pointer is cleared, but the session keeps a short "draining"
		# window during which jobs the flow enqueued are still recorded
		# (analyze.run waits for them) — but ONLY when the user has NO
		# active session, so a late job never bleeds into a *different*
		# session the user started after stopping this one. If the user
		# started a brand-new session (active != session_uuid and not
		# None), drop this stale job recording.
		active = session.get_active_session_for(user)
		if active != session_uuid and not (active is None and session.is_draining(session_uuid)):
			return

		# All checks passed — activate the recorder for this job.
		frappe.local.optimus_session_id = session_uuid

		# Same clobber protection as before_request: if the standalone
		# recorder already activated (global flag + frappe's own
		# before_job hook), piggyback on its instance.
		if getattr(frappe.local, "_recorder", None) is not None:
			return

		# (frappe.recorder is imported at module top — see comment there.)
		frappe.recorder.record(force=True)

		# v0.5.1: snapshot BEFORE _start_pyi_session — mirrors the order
		# fix in before_request. See the rationale comment there.
		try:
			from optimus import infra_capture
			frappe.local.optimus_infra_start = infra_capture.snapshot()
		except Exception:
			frappe.log_error(title="optimus infra start snapshot (job)")

		# v0.3.0: gate the new pyinstrument + sidecar capture on the
		# session's capture_python_tree flag. Mirrors before_request.
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("capture_python_tree", True):
			frappe.local._profiler_active_session_id = session_uuid
			_capture._start_pyi_session(
				local_proxy=frappe.local,
				interval_ms=_resolve_sampler_interval_ms(),
			)
	except Exception:
		frappe.log_error(title="optimus before_job")


def after_job(method=None, kwargs=None, result=None, **rest):
	"""Register the dumped recording UUID with the active profiler session."""
	try:
		session_uuid = getattr(frappe.local, "optimus_session_id", None)
		if not session_uuid:
			return

		recorder = getattr(frappe.local, "_recorder", None)
		if recorder is None:
			return

		recording_uuid = getattr(recorder, "uuid", None)
		if not recording_uuid:
			return

		user = getattr(frappe.session, "user", None)
		registered = session.register_recording(
			session_uuid, recording_uuid, user=user
		)
		if not registered:
			frappe.logger().warning(
				f"optimus: recording cap hit for session "
				f"{session_uuid}, dropped job {recording_uuid}"
			)
	except Exception:
		frappe.log_error(title="optimus after_job")
	finally:
		recording_uuid_for_dump = getattr(
			getattr(frappe.local, "_recorder", None), "uuid", None
		)
		_dump_capture_state_to_redis(recording_uuid=recording_uuid_for_dump)

		# v0.5.0: write the infra diff to Redis for this job's recording.
		# No correlation header to inject — background jobs have no HTTP
		# response, and no browser to correlate with.
		try:
			start_snap = getattr(frappe.local, "optimus_infra_start", None)
			if start_snap and recording_uuid_for_dump:
				from optimus import infra_capture
				end_snap = infra_capture.snapshot()
				from optimus import redis_keys
				frappe.cache.set_value(
					redis_keys.infra(recording_uuid_for_dump),
					infra_capture.diff(start_snap, end_snap),
					expires_in_sec=session.SESSION_TTL_SECONDS,
				)
		except Exception:
			frappe.log_error(title="optimus infra end snapshot (job)")

		# v0.6.0: this job ran — drop its RQ id from the session's
		# pending-jobs set so analyze.run's wait can end early. Best-effort.
		# v0.7.x+: also write terminal status + ended_at + duration_ms from
		# here, while the RQ record is still alive. Falls back to the
		# bg-session stash set in before_job so orphan jobs (whose recorder
		# we never activated, leaving ``optimus_session_id`` unset) still
		# get their terminal status recorded on the originating session.
		try:
			from rq import get_current_job

			_rq_job = get_current_job()
			_rq_jid = getattr(_rq_job, "id", None) if _rq_job is not None else None
			_su = getattr(frappe.local, "optimus_session_id", None) or getattr(
				frappe.local, "optimus_bg_session_uuid", None
			)
			if _rq_jid and _su:
				session.clear_pending_job(_su, _rq_jid)
				# v0.7.x: link this job to the recording it produced so the
				# report can join the captured query data to the job's row.
				if recording_uuid_for_dump:
					session.set_job_recording(_su, _rq_jid, recording_uuid_for_dump)
				# v0.7.x+: authoritative terminal-status write — see the
				# helper's docstring for why this beats the analyze-time
				# RQ re-fetch (which silently drops GC'd job records).
				_track_bg_job_finished(_su, _rq_jid)
		except Exception:
			pass

		if hasattr(frappe.local, "optimus_session_id"):
			del frappe.local.optimus_session_id
		if hasattr(frappe.local, "optimus_bg_session_uuid"):
			try:
				delattr(frappe.local, "optimus_bg_session_uuid")
			except AttributeError:
				pass
		if hasattr(frappe.local, "optimus_bg_job_start_mono"):
			try:
				delattr(frappe.local, "optimus_bg_job_start_mono")
			except AttributeError:
				pass
		if hasattr(frappe.local, "optimus_infra_start"):
			try:
				delattr(frappe.local, "optimus_infra_start")
			except AttributeError:
				pass


def _dump_capture_state_to_redis(recording_uuid: str | None) -> None:
	"""Serialize the in-flight pyinstrument session and sidecar list to Redis.

	Called from after_request / after_job after the recorder has dumped
	its own SQL recording. The two new Redis keys are:

	  profiler:tree:<recording_uuid>      → HMAC-signed pickle.dumps(pyi.last_session)
	  profiler:sidecar:<recording_uuid>   → list[dict]

	Both inherit the same TTL semantics as RECORDER_REQUEST_HASH (cleaned
	up at the end of analyze, or expire naturally if analyze never runs).

	Phase K hardening: the pickled tree carries a 32-byte HMAC-SHA256
	prefix derived from the site's ``encryption_key``. ``analyze.py``
	refuses to unpickle blobs whose signature doesn't match - this
	stops a Redis-poisoning attacker from injecting a malicious pickle
	to gain code execution in a worker process.

	Best-effort: failures here log but never break the request.
	"""
	import pickle

	from optimus.session import SESSION_TTL_SECONDS, sign_blob

	if not recording_uuid:
		# No recorder ran on this request — nothing to dump.
		_clear_capture_locals()
		return

	prof = getattr(frappe.local, "optimus_pyinstrument", None)
	if prof is not None:
		try:
			prof.stop()
			tree_blob = sign_blob(pickle.dumps(prof.last_session))
			# Visibility for the picker-diagnostic path: log whether
			# this write was signed or unsigned, plus byte size.
			# Helps operators correlate a "with_tree=0" picker
			# diagnostic with the actual write mode (signed blobs
			# need a stable encryption_key; unsigned blobs round-
			# trip via the fallback).
			try:
				from optimus.session import _has_stable_hmac_secret
				frappe.logger().debug(
					f"optimus pyi dump: {recording_uuid} "
					f"({'signed' if _has_stable_hmac_secret() else 'unsigned'}, "
					f"{len(tree_blob)} bytes)"
				)
			except Exception:
				pass
			from optimus import redis_keys
			frappe.cache.set_value(
				redis_keys.tree(recording_uuid),
				tree_blob,
				expires_in_sec=SESSION_TTL_SECONDS,
			)
		except Exception:
			frappe.log_error(title="optimus pyi dump")

	sidecar = getattr(frappe.local, "optimus_sidecar", None)
	if sidecar:
		try:
			# If the sidecar was truncated, append a marker entry so the
			# analyze pipeline can surface a warning even if the
			# truncation flag itself is lost across the Redis hop.
			payload = list(sidecar)
			if getattr(frappe.local, "optimus_sidecar_truncated", False):
				payload.append({"_truncated": True})
			from optimus import redis_keys
			frappe.cache.set_value(
				redis_keys.sidecar(recording_uuid),
				payload,
				expires_in_sec=SESSION_TTL_SECONDS,
			)
		except Exception:
			frappe.log_error(title="optimus sidecar dump")

	_clear_capture_locals()


def _clear_capture_locals() -> None:
	"""Clear all v0.3.0 capture state from frappe.local. Idempotent."""
	for attr in (
		"optimus_pyinstrument",
		"optimus_sidecar",
		"optimus_sidecar_truncated",
		"_profiler_active_session_id",
		"_profiler_in_wrap",
	):
		if hasattr(frappe.local, attr):
			try:
				delattr(frappe.local, attr)
			except AttributeError:
				pass


# ---------------------------------------------------------------------------
# Background-job status tracking — authoritative writes from the worker
# ---------------------------------------------------------------------------
# Pre-fix, the bg-job tracking pipeline relied on analyze re-reading each
# enqueued job's terminal status from RQ at persist time
# (``analyze._capture_job_terminal_status`` → ``rq.Job.fetch``). Two failure
# modes left rows wrong on the report:
#
#   1. ``enqueue_after_commit=True`` jobs: ``frappe.enqueue`` returns None
#      for deferred dispatch (see ``frappe/utils/background_jobs.py`` 's
#      ``if enqueue_after_commit: ... return``), so the enqueue patch's
#      ``record_job`` call in ``optimus/__init__.py`` is skipped (its
#      ``if job is not None`` guard fails). The method name was never
#      written to the jobs hash → blank ``method`` column on the row.
#
#   2. Short jobs whose RQ record was GC'd before analyze persisted:
#      ``Job.fetch`` raises ``NoSuchJobError``, which the ``except
#      Exception: pass`` in ``_capture_job_terminal_status`` swallows.
#      No status/times were written → row defaulted to status=Running
#      with NULL started_at/ended_at.
#
# These two helpers move the authoritative writes into the hooks, where:
#   * before_job calls ``_track_bg_job_started`` right after popping the
#     marker — has access to the method arg (fixes #1) and the worker's
#     wall-clock time (fixes the "no started_at" half of #2).
#   * after_job calls ``_track_bg_job_finished`` in its finally block —
#     RQ record is still alive, so we capture status + ended_at +
#     duration_ms ourselves (fixes the other half of #2).
#
# analyze's existing ``_capture_job_terminal_status`` stays as the fallback
# for jobs the hooks couldn't cover (worker SIGKILL on timeout, crash before
# after_job fires — frappe runs after_job in a try/finally so this is rare
# but possible). Its persist-time loop only fires for jobs whose status the
# hooks did NOT already write (``if not _jm.get("status"):``), so no
# double-work and no overwrite churn.
#
# Late-finish DocType fallback (v0.7.x+):
# ``analyze._bg_wait_for_pending_jobs`` is hard-capped at
# ``_MAX_BG_JOB_WAIT_SECONDS = 300`` (analyze.py:215). A job that runs longer
# than 5 min ALWAYS exceeds the cap; at the cap, ``_finalize_pending_statuses``
# marks the still-active job as ``status="Running"`` (no end times — analyze
# can't predict when it'll finish), ``_persist`` writes the Optimus
# Background Job child row from that snapshot, and ``_cleanup_redis`` then
# DELETES the jobs hash. When the long job actually finishes minutes later,
# the worker's ``_track_bg_job_finished`` writes to a deleted Redis key
# (orphan) and the persisted row stays ``Running`` forever — there's no
# in-band path to update it because re-analyze isn't viable (recordings /
# meta / jobs hash are all gone).
#
# Closing the gap: after ``set_job_status`` writes to the Redis hash,
# ``_track_bg_job_finished`` ALSO checks the Optimus Session DocType row;
# if the session has finalized (status in {"Ready", "Failed"}) and a child
# row exists at ``status="Running"``, it writes the terminal status +
# ended_at + duration_ms + error directly via ``frappe.db.set_value`` +
# commit. Guarded by an idempotency check (only updates Running placeholders
# — never clobbers a fresher Completed) and a SEPARATE try/except from the
# Redis path so neither failure suppresses the other.
#
# Both helpers are best-effort: any failure is swallowed so a tracking bug
# can never break the user's actual job.


def _track_bg_job_started(session_uuid: str, method) -> None:
	"""Record this RQ job's method and mark it Running with started_at = now.
	Stashes a monotonic baseline on ``frappe.local`` for the duration calc
	in ``_track_bg_job_finished``.

	Called from ``before_job`` once the marker is validated, BEFORE the
	user/active-session gates — so even orphan jobs (whose recorder we
	won't activate) get their status reported in the session's bg-jobs
	list. The ``record_job`` call is idempotent (uses ``setdefault`` for
	method on the Redis hash) so it's safe even when the fast-path in the
	enqueue patch already recorded the method.
	"""
	try:
		from rq import get_current_job

		rq_job = get_current_job()
		job_id = getattr(rq_job, "id", None) if rq_job is not None else None
		if not job_id:
			return
		# frappe.enqueue accepts a callable too — extract its name so the
		# row's Method column doesn't render "<function foo at 0x…>".
		method_str = method if isinstance(method, str) else getattr(method, "__name__", str(method))
		session.record_job(session_uuid, job_id, method_str)
		from frappe.utils import now_datetime

		session.set_job_status(
			session_uuid,
			job_id,
			status="Running",
			started_at=str(now_datetime()),
		)
		# Worker processes are single-job-at-a-time so this can't leak across
		# jobs; the after_job clear is belt-and-braces.
		frappe.local.optimus_bg_job_start_mono = time.monotonic()
	except Exception:
		# Tracking failure must never break the user's job.
		pass


def _track_bg_job_finished(session_uuid: str, job_id: str) -> None:
	"""Write the terminal status + ended_at + duration_ms while the RQ job
	record is still alive in the worker. Two write paths, in order:

	1. **Redis jobs hash** — primary. analyze's ``session.get_jobs(...)``
	   reads from here at persist time. The in-flight case where analyze is
	   still mid-wait gets the authoritative final state via this write.

	2. **Optimus Background Job DocType row** — late-finish fallback. If
	   analyze has already finalized this session (status in {"Ready",
	   "Failed"}) and ``_cleanup_redis`` has deleted the jobs hash, the
	   Redis write above is an orphan; this path updates the persisted row
	   directly so the report eventually reflects the late completion
	   without needing a re-analyze (which isn't viable post-cleanup).

	Failure detection: Frappe runs after_job hooks inside a ``try/finally``
	wrapping the user's method, so an in-flight exception is visible via
	``sys.exc_info()``. RQ's timeout-killer raises ``JobTimeoutException``
	(its class name, regardless of import path) — we map that distinctly so
	the report can flag timeouts separately from generic user-code failures.

	Each write path has its own ``try/except: pass`` so a Redis hiccup
	doesn't suppress the DocType write and vice versa. The status / timing
	computation has its own outer guard so an unexpected failure there exits
	cleanly without raising back into Frappe's hook dispatcher.
	"""
	# Compute terminal status + timing once; both write paths reuse them.
	try:
		import sys

		exc_type, exc_value, _ = sys.exc_info()
		if exc_type is None:
			status, error = "Completed", None
		else:
			# Match on the class name (not isinstance) so we don't have to
			# import rq.timeouts at the worker hot path — and so vendored
			# / re-exported variants still match.
			name = exc_type.__name__
			status = "Timeout" if name == "JobTimeoutException" else "Failed"
			error = f"{name}: {exc_value}"
		from frappe.utils import now_datetime

		ended_at = str(now_datetime())
		start_mono = getattr(frappe.local, "optimus_bg_job_start_mono", None)
		duration_ms = None
		if start_mono is not None:
			try:
				duration_ms = round((time.monotonic() - start_mono) * 1000, 2)
			except Exception:
				duration_ms = None
	except Exception:
		# Couldn't even compute the terminal state — bail rather than write
		# half-baked data anywhere.
		return

	# Primary path: Redis jobs hash. Picked up by analyze if still mid-wait.
	try:
		session.set_job_status(
			session_uuid,
			job_id,
			status=status,
			error=error,
			ended_at=ended_at,
			duration_ms=duration_ms,
		)
	except Exception:
		pass

	# Late-finish fallback: update the persisted DocType row if analyze has
	# already finalized this session and its jobs hash is gone. See the
	# comment block above for the wait-cap rationale.
	try:
		docname = frappe.db.get_value(
			"Optimus Session",
			{"session_uuid": session_uuid},
			"name",
		)
		if not docname:
			return  # session not yet persisted — Redis path is the only path
		session_status = frappe.db.get_value("Optimus Session", docname, "status")
		if session_status not in ("Ready", "Failed"):
			return  # analyze still mid-wait; let it pick up the Redis write
		child_name = frappe.db.get_value(
			"Optimus Background Job",
			{"parent": docname, "job_id": job_id},
			"name",
		)
		if not child_name:
			return  # no row was persisted for this job — don't INSERT a phantom
		cur_status = frappe.db.get_value("Optimus Background Job", child_name, "status")
		if cur_status not in (None, "Running"):
			return  # idempotency: don't clobber a fresher Completed / Failed
		frappe.db.set_value(
			"Optimus Background Job",
			child_name,
			{
				"status": status,
				"ended_at": ended_at,
				"duration_ms": duration_ms,
				"error": error,
			},
		)
		frappe.db.commit()
	except Exception:
		# DocType update failure must NOT break the worker, and must NOT
		# suppress the Redis write (which is in a separate try above).
		pass


# ---------------------------------------------------------------------------
# v0.5.0: correlation header for frontend metrics
# ---------------------------------------------------------------------------
# The X-Optimus-Recording-Id header is read by the browser-side
# optimus_frontend.js shim to tie each XHR timing back to a specific
# server recording. Without the Access-Control-Expose-Headers entry,
# browsers refuse to surface custom response headers to JavaScript
# even for same-origin requests — it's the most common frontend
# instrumentation failure mode.


_CORRELATION_HEADER_NAME = "X-Optimus-Recording-Id"
_EXPOSE_HEADER_NAME = "Access-Control-Expose-Headers"


def _inject_correlation_header(recording_uuid: str, response=None) -> None:
	"""Attach X-Optimus-Recording-Id to the outgoing response + expose it
	via Access-Control-Expose-Headers. Called from after_request during
	an active profiler session. Idempotent and safe to call in non-HTTP
	contexts (no-op when neither a response object nor
	``frappe.local.response_headers`` is available).

	v0.5.3: now supports Frappe v15. v15 does not stage custom headers
	on ``frappe.local.response_headers`` (only v16 does that), so when
	a response object is available we write the headers directly to
	``response.headers``. The v16 staging path is tried first because
	it predates v15 support and some deployments rely on downstream
	hooks reading the staged dict; the v15 direct-write path is the
	fallback.

	Symptom that motivated this fix: on v15, "Per-XHR timings" in the
	Frontend panel rendered empty because ``optimus_frontend.js``
	couldn't read the recording-id header — it was never set. Our
	v16-only staging dict was silently dropped during response build.
	"""
	# v16 path: write to the staged dict if Frappe exposes it.
	headers = getattr(frappe.local, "response_headers", None)
	if headers is not None:
		headers[_CORRELATION_HEADER_NAME] = recording_uuid
		try:
			existing = headers.get(_EXPOSE_HEADER_NAME) or ""
		except Exception:
			existing = ""
		# Token-by-token check, NOT a substring ``in`` check. A naive
		# ``in`` would falsely match when another app has already
		# added "X-Optimus-Recording-Id-Legacy" or similar — our real
		# header would then NOT be appended, the browser would refuse
		# to surface it to JavaScript, and the entire frontend
		# correlation feature would silently break. Split on commas
		# and compare case-insensitively.
		tokens = {t.strip().lower() for t in existing.split(",") if t.strip()}
		if _CORRELATION_HEADER_NAME.lower() not in tokens:
			merged = (
				existing + ", " + _CORRELATION_HEADER_NAME
				if existing
				else _CORRELATION_HEADER_NAME
			)
			headers[_EXPOSE_HEADER_NAME] = merged

	# v15 path (AND harmless belt-and-braces on v16): write directly
	# to response.headers when the Response object is available. This
	# covers v15 where the staging dict does not exist, and doesn't
	# hurt v16 where the staged values will be ``update()``'d onto
	# the same response.headers later anyway (setting twice is a
	# no-op for the same key).
	if response is not None:
		r_headers = getattr(response, "headers", None)
		if r_headers is None:
			return
		try:
			r_headers[_CORRELATION_HEADER_NAME] = recording_uuid
		except Exception:
			return
		try:
			existing = r_headers.get(_EXPOSE_HEADER_NAME) or ""
		except Exception:
			existing = ""
		tokens = {t.strip().lower() for t in existing.split(",") if t.strip()}
		if _CORRELATION_HEADER_NAME.lower() not in tokens:
			merged = (
				existing + ", " + _CORRELATION_HEADER_NAME
				if existing
				else _CORRELATION_HEADER_NAME
			)
			try:
				r_headers[_EXPOSE_HEADER_NAME] = merged
			except Exception:
				pass
