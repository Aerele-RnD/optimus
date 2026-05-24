# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Background-job entry point: analyze a finished session.

Triggered by `api.stop()` via `frappe.enqueue("optimus.analyze.run", ...)`.
Reads all recordings for the session from Redis, runs the six analyzers,
persists the results into the Optimus Session DocType, and publishes a
realtime notification so the UI can navigate to the report.

State transitions on the Optimus Session row:
    Stopping  →  Analyzing  →  Ready    (happy path)
    Stopping  →  Analyzing  →  Failed   (uncaught exception)

The Redis state for the session is cleaned up at the end of a successful
run — the source recordings are deleted from RECORDER_REQUEST_HASH and the
profiler:session:* keys are removed. Failed runs do NOT clean up, so a
developer can manually retry the analyze.
"""

import html
import json
import os
import re
import time
from collections import OrderedDict

import frappe
import sqlparse
from frappe.database.utils import is_query_type
from frappe.recorder import (
	RECORDER_REQUEST_HASH,
	RECORDER_REQUEST_SPARSE_HASH,
	mark_duplicates,
)
from frappe.utils.scheduler import is_scheduler_disabled

from optimus import renderer, safe_commit, session
from optimus.analyzers import (
	call_tree,
	explain_flags,
	frontend_timings,  # v0.5.0
	index_suggestions,
	infra_pressure,  # v0.5.0
	n_plus_one,
	per_action,
	redundant_calls,
	table_breakdown,
	top_queries,
)
from optimus.analyzers.base import SEVERITY_ORDER, AnalyzeContext

# v0.3.0: per-analyzer wall-clock budget. If the cumulative analyze
# elapsed time crosses this threshold, remaining analyzers are skipped
# and the session is finalized as Ready with a partial-completion
# warning. 5 minutes of headroom under the RQ long-queue 25-min timeout.
ANALYZE_TOTAL_BUDGET_SECONDS = 20 * 60

# Per-individual-analyzer soft cap: an analyzer that exceeds this is
# logged as a warning but doesn't halt the pipeline.
ANALYZE_PER_ANALYZER_SOFT_CAP_SECONDS = 60

# v0.6.0: when Optimus Settings ▸ AI Fix Suggestions ▸ "Suggest AI fixes
# by default" is on, the analyze pipeline calls the LLM for the top
# findings. Cap the total wall time spent on those calls so a slow
# provider can't push the analyze job past the RQ 25-min timeout.
AI_AUTO_SUGGEST_TIME_BUDGET_SECONDS = 240

# v0.6.0: same toggle also bakes an LLM-vetted index recommendation onto the
# top N tables in the breakdown — capped tables + its own wall-time budget.
AI_AUTO_INDEX_MAX_TABLES = 3
AI_AUTO_INDEX_TIME_BUDGET_SECONDS = 90

# Tighter budget for the same backfill done from api.regenerate_reports —
# that runs synchronously inside a web request, so it must stay well under
# the gunicorn worker timeout (~120s). Anything not done in this window is
# left for a re-run of Regenerate Reports (or a full Retry Analyze).
AI_BACKFILL_TIME_BUDGET_SECONDS = 60

# v0.3.0: persistence size limits for call_tree_json on Optimus Action.
CALL_TREE_OVERFLOW_THRESHOLD_BYTES = 200_000   # write to file above this
CALL_TREE_HARD_MAX_BYTES = 16_000_000          # last-line sanity guard
CALL_TREE_HARD_TRUNCATE_KEEP_FRAMES = 300      # frames kept on fallback (Phase K: 100 was too aggressive for typical Frappe + middleware stacks)


# per_action is first because it builds the Optimus Action rows that the
# rest of the analyzers reference via action_ref. The remainder are
# independent and could in principle be parallelized.
_BUILTIN_ANALYZERS = [
	per_action.analyze,
	top_queries.analyze,
	n_plus_one.analyze,
	explain_flags.analyze,
	index_suggestions.analyze,
	table_breakdown.analyze,
	call_tree.analyze,        # v0.3.0 — must run after per_action
	redundant_calls.analyze,  # v0.3.0 — independent
	infra_pressure.analyze,   # v0.5.0 — reads rec["infra"]
	frontend_timings.analyze, # v0.5.0 — reads context.frontend_data
]

# Backward-compat alias: the old name is still the public-facing list
# for code that references `analyze.ANALYZERS` directly.
ANALYZERS = _BUILTIN_ANALYZERS


def _get_analyzers() -> list:
	"""Return the analyzer pipeline: builtins + custom hooks.

	Round 2 fix #13. Third-party Frappe apps can add analyzers via:

	    # hooks.py
	    optimus_analyzers = [
	        "my_app.analyzers.custom.analyze",
	    ]

	Custom analyzers run AFTER the builtins so they can read
	context.actions / context.findings built by earlier analyzers.
	A failing custom analyzer logs via the normal error path but
	doesn't abort the pipeline (same as builtins).
	"""
	analyzers = list(_BUILTIN_ANALYZERS)
	try:
		hook_paths = frappe.get_hooks("optimus_analyzers") or []
	except Exception:
		hook_paths = []

	for dotted in hook_paths:
		if not dotted:
			continue
		try:
			fn = frappe.get_attr(dotted)
			if callable(fn):
				analyzers.append(fn)
			else:
				frappe.log_error(
					title="optimus analyzer hook",
					message=f"Custom analyzer {dotted} is not callable",
				)
				try:
					from optimus import telemetry
					telemetry.emit_failure(
						"analyze.custom_hook.not_callable",
						context={"hook": dotted or ""},
					)
				except Exception:
					pass
		except Exception as exc:
			frappe.log_error(
				title="optimus analyzer hook",
				message=f"Failed to load custom analyzer {dotted}",
			)
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.custom_hook.load_failed", exc,
					context={"hook": dotted or ""},
				)
			except Exception:
				pass

	return analyzers


def _publish_progress(percent: float, description: str, session_uuid: str):
	"""Emit a progress update for the floating widget and form UI.

	Best-effort — never raises. Subscribed to in the floating widget JS
	via frappe.realtime.on("optimus_progress"). Round 2 fix #17.
	"""
	try:
		frappe.publish_realtime(
			"optimus_progress",
			{
				"session_uuid": session_uuid,
				"percent": round(percent, 1),
				"description": description,
			},
		)
	except Exception:
		pass


def _publish_session_event(
	event_name: str,
	*,
	session_uuid: str,
	docname: str | None,
	**extra,
) -> None:
	"""Publish a session-state transition event to the session owner's
	Desk tabs.

	Called from the background analyze job, which runs without a
	request-scoped user — so we look the user up from the Profiler
	Session row itself. Mirrors ``api._publish_session_event`` but
	with doctype-driven user resolution.

	v0.5.1: drives the floating widget state machine without HTTP
	polling. Events emitted from analyze.run:

	  optimus_session_analyzing  — right after status becomes Analyzing
	  optimus_session_ready      — success; the widget navigates to the
	                                 report (kept under its original name
	                                 for backward compat with v0.3.0+
	                                 subscribers)
	  optimus_session_failed     — uncaught exception during analyze

	Best-effort and isolated — a publish failure cannot derail the
	analyze pipeline. Realtime is a UX convenience; the state is always
	durable on the Optimus Session row.
	"""
	try:
		user = None
		if docname:
			try:
				user = frappe.db.get_value(
					"Optimus Session", docname, "user"
				)
			except Exception:
				user = None
		payload = {"session_uuid": session_uuid, "docname": docname}
		payload.update(extra)
		frappe.publish_realtime(event_name, payload, user=user)
	except Exception:
		pass


# v0.6.0: hard ceiling on how long analyze waits for the flow's background
# jobs, regardless of the configured `background_job_wait_seconds`.
_MAX_BG_JOB_WAIT_SECONDS = 300
# Throttle between re-enqueue cycles while waiting — keeps a wedged ("deferred"
# forever) job from busy-looping our re-enqueues.
_BG_WAIT_THROTTLE_SECONDS = 2.0

# v0.7.x (M2): global single-flight for the heavy analyze phase. Two analyze
# jobs running at once roughly DOUBLE peak RAM — the OOM trigger on long flows.
# A best-effort Redis flag (NOT a held lock) lets only one session do the heavy
# work at a time; others re-enqueue themselves to yield the worker (mirrors the
# bg-job wait pattern), so a single-worker bench still makes progress.
# v0.12.0: key centralized in optimus.redis_keys. The constant is kept
# for code-local readability — every internal use that references the
# key still does so via this module-level alias.
from optimus import redis_keys as _redis_keys

_SINGLEFLIGHT_KEY = _redis_keys.analyze_inflight()
# Flag TTL. Heartbeated at each progress milestone in run(); if a worker dies
# mid-analyze the flag self-heals within this window (no permanent strand).
_SINGLEFLIGHT_TTL_SECONDS = 300
# Throttle between polite re-enqueue cycles while another session holds the flag.
_SINGLEFLIGHT_THROTTLE_SECONDS = 5.0
# Hard ceiling on how long a session waits for the flag before degrading to the
# pre-M2 behavior (proceed anyway) rather than waiting forever.
_SINGLEFLIGHT_MAX_WAIT_SECONDS = 600


def _apply_nice() -> None:
	"""v0.7.x (M5): best-effort — lower this process's CPU scheduling priority
	so a heavy analyze yields the CPU to live web traffic. A *positive* nice
	increment never requires privilege. No-op when configured to 0 or on a
	platform without ``os.nice``. CPU-politeness only, not a memory lever.

	Caller gates this to the async (RQ-worker) path: ``os.nice`` is sticky
	per-process, so renicing the shared gunicorn worker on the inline path
	would persist for unrelated later requests."""
	try:
		inc = int(frappe.conf.get("optimus_analyze_nice", 5) or 0)
	except Exception:
		inc = 0
	if inc and hasattr(os, "nice"):
		try:
			os.nice(inc)
		except (OSError, ValueError):
			pass


def _touch_singleflight(session_uuid: str) -> None:
	"""(Re)assert ownership of the single-flight flag and refresh its TTL.
	Best-effort — a cache hiccup must never fail analyze."""
	try:
		frappe.cache.set_value(
			_SINGLEFLIGHT_KEY, session_uuid, expires_in_sec=_SINGLEFLIGHT_TTL_SECONDS
		)
	except Exception:
		pass


def _release_singleflight(session_uuid: str) -> None:
	"""Release the flag, but only if we still hold it (compare-then-delete) —
	a TTL-expired-then-reacquired flag belonging to another session must not be
	clobbered. Best-effort."""
	try:
		if frappe.cache.get_value(_SINGLEFLIGHT_KEY) == session_uuid:
			frappe.cache.delete_value(_SINGLEFLIGHT_KEY)
	except Exception:
		pass


def _acquire_singleflight(session_uuid: str, docname: str, deadline) -> bool:
	"""Global single-flight gate for the heavy (memory-hungry) analyze phase.

	Returns:
	  * ``True``  — proceed with analysis. Either we acquired the flag, or
	    single-flight doesn't apply (inline path / disabled), or we waited past
	    the deadline and degrade to the pre-M2 behavior rather than strand.
	  * ``False`` — another session holds the flag; we re-enqueued ourselves
	    (anonymously, carrying ``_singleflight_deadline``) to yield the worker.
	    The caller must ``return`` now.

	Mirrors ``_bg_wait_for_pending_jobs``: re-enqueue + yield, never a held
	lock. Skipped when the scheduler is disabled (analyze runs inline in a web
	request — there's no worker to run our re-enqueued self, and inline peak is
	already bounded by ``optimus_inline_analyze_limit``)."""
	# Inline path: no worker to yield to — proceed.
	try:
		if is_scheduler_disabled():
			return True
	except Exception:
		return True

	try:
		max_wait = int(frappe.conf.get("optimus_singleflight_max_wait_seconds")
			or _SINGLEFLIGHT_MAX_WAIT_SECONDS)
	except Exception:
		max_wait = _SINGLEFLIGHT_MAX_WAIT_SECONDS
	if max_wait <= 0:
		return True  # single-flight disabled by config

	try:
		holder = frappe.cache.get_value(_SINGLEFLIGHT_KEY)
	except Exception:
		return True  # cache unavailable — degrade to pre-M2 behavior

	if not holder or holder == session_uuid:
		# Free, or already ours (our own self-re-enqueue / heartbeat) — take it.
		_touch_singleflight(session_uuid)
		return True

	# Busy with a different session. Respect the wait deadline, else degrade.
	if deadline is None:
		deadline = time.time() + max_wait
	if time.time() >= deadline:
		return True  # waited long enough — proceed rather than strand

	# Keep the UI honest, throttle, and re-enqueue ourselves to yield the worker.
	try:
		frappe.db.set_value("Optimus Session", docname, "status", "Analyzing")
		safe_commit()
	except Exception:
		pass
	_publish_progress(3, "Waiting for another analysis to finish…", session_uuid)
	time.sleep(min(_SINGLEFLIGHT_THROTTLE_SECONDS, max(0.0, deadline - time.time())))
	try:
		frappe.enqueue(
			"optimus.analyze.run",
			queue="long",
			session_uuid=session_uuid,
			_singleflight_deadline=deadline,
		)
	except Exception as exc:
		frappe.log_error(title="optimus single-flight re-enqueue")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.singleflight_reenqueue", exc,
				context={"session_uuid": session_uuid or "", "docname": docname or ""},
			)
		except Exception:
			pass
		return True  # couldn't re-enqueue — just proceed
	return False


def _rq_job_active(job_id: str) -> bool:
	"""True if RQ job ``job_id`` is still queued / started / deferred /
	scheduled. False if it's terminal (finished / failed / stopped /
	canceled) or no longer fetchable (expired / deleted). Any error → not
	active (don't make analyze block on it)."""
	try:
		from frappe.utils.background_jobs import get_redis_conn
		from rq.job import Job

		job = Job.fetch(job_id, connection=get_redis_conn())
		return job.get_status(refresh=True) in ("queued", "started", "deferred", "scheduled")
	except Exception:
		return False


def _short_exc(exc_info) -> str | None:
	"""Last non-empty line of an RQ ``exc_info`` traceback, truncated — the
	one-liner that names the exception (e.g. ``ValueError: bad doc_name``)."""
	if not exc_info:
		return None
	lines = [ln.strip() for ln in str(exc_info).splitlines() if ln.strip()]
	return lines[-1][:500] if lines else None


def _rq_dt_to_db(dt):
	"""RQ exposes ``started_at`` / ``ended_at`` as timezone-aware UTC datetimes;
	``str()`` of one yields ``'...+00:00'`` and MariaDB's DATETIME column rejects
	the tz offset (err 1292 — it crashed the whole report persist). Convert to
	the site's system timezone and format naive (matching ``session.record_job``'s
	``enqueued_at``), so it stores cleanly and reads consistently. Falls back to a
	tz-stripped string if the frappe context can't resolve the system timezone."""
	if not dt:
		return None
	try:
		from frappe.utils import convert_utc_to_system_timezone, get_datetime_str

		return get_datetime_str(convert_utc_to_system_timezone(dt))
	except Exception:
		try:
			return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
		except Exception:
			return None


_TZ_OFFSET_RE = re.compile(r"([+-]\d{2}:?\d{2}|Z)$")


def _db_datetime_str(val):
	"""Make a datetime *string* MariaDB-safe by stripping any trailing tz offset
	(``+00:00`` / ``+0000`` / ``-05:30`` / ``Z``); MariaDB's DATETIME column
	rejects an offset (err 1292). New captures are already clean via
	``_rq_dt_to_db``; this guards the DB-write boundary against values stored in
	Redis by an older build (``str()`` of a tz-aware datetime), so re-analyzing a
	pre-fix session persists instead of crashing again. A naive string (the date
	uses ``-`` only internally, never at the end) passes through unchanged.
	Returns None for an empty value."""
	if not val:
		return None
	s = str(val).strip()
	if not s:
		return None
	return _TZ_OFFSET_RE.sub("", s) or None


def _capture_job_terminal_status(session_uuid: str, job_id: str) -> None:
	"""Read a now-inactive RQ job's terminal status + timing and record it on
	the session's job-meta hash, so analyze can persist it (Completed / Failed /
	Timeout / Stopped). Best-effort — never blocks the wait."""
	try:
		from frappe.utils.background_jobs import get_redis_conn
		from rq.job import Job

		job = Job.fetch(job_id, connection=get_redis_conn())
		rq_status = job.get_status(refresh=True)
		exc = job.exc_info or ""
		if rq_status == "finished":
			status, error = "Completed", None
		elif rq_status == "failed":
			# An RQ JobTimeoutException in the traceback means the worker killed
			# it for exceeding its timeout — report that distinctly.
			status = "Timeout" if "JobTimeoutException" in exc else "Failed"
			error = _short_exc(exc)
		elif rq_status in ("stopped", "canceled"):
			status, error = "Stopped", _short_exc(exc)
		else:
			return  # still active — leave for the running-mark path
		started = getattr(job, "started_at", None)
		ended = getattr(job, "ended_at", None)
		duration_ms = None
		if started and ended:
			try:
				duration_ms = round((ended - started).total_seconds() * 1000, 2)
			except Exception:
				duration_ms = None
		session.set_job_status(
			session_uuid, job_id,
			status=status,
			error=error,
			started_at=_rq_dt_to_db(started),
			ended_at=_rq_dt_to_db(ended),
			duration_ms=duration_ms,
		)
	except Exception:
		pass


def _finalize_pending_statuses(session_uuid: str, job_ids) -> None:
	"""At the wait ceiling: record a terminal status for jobs that finished,
	and mark any still-active job ``Running`` (so it's reported, not vanished —
	the user re-runs Analyze once it finishes to capture its data)."""
	for jid in job_ids:
		if _rq_job_active(jid):
			session.set_job_status(session_uuid, jid, status="Running")
		else:
			_capture_job_terminal_status(session_uuid, jid)


def _bg_wait_for_pending_jobs(session_uuid: str, docname: str, deadline):
	"""Make sure the background jobs the profiled flow enqueued have finished
	before we gather recordings.

	Returns:
	  * ``None`` — there are still-running jobs and we re-enqueued
	    ``analyze.run`` to yield the worker; the caller must ``return`` now.
	  * ``0`` — nothing to wait for / all jobs finished / the wait is disabled
	    or can't run (scheduler off → analyze is inline): proceed with analysis.
	  * ``N > 0`` — the wait cap was hit with N jobs still running: proceed,
	    but the caller should surface a warning.

	Pure best-effort — any failure returns 0 (proceed). Re-enqueuing (rather
	than sleeping the whole window) lets a single worker actually run those
	jobs while we wait.
	"""
	try:
		pending = session.get_pending_jobs(session_uuid)
	except Exception:
		return 0
	if not pending:
		return 0

	try:
		from optimus.settings import get_config

		wait_seconds = int(getattr(get_config(), "background_job_wait_seconds", 0) or 0)
	except Exception:
		wait_seconds = 0
	if wait_seconds <= 0:
		return 0
	wait_seconds = min(wait_seconds, _MAX_BG_JOB_WAIT_SECONDS)

	# When the scheduler is disabled, analyze is running inline in a web
	# request and there's no worker to run the pending jobs (or our
	# re-enqueued self) — better to ship the report now than hang.
	try:
		if is_scheduler_disabled():
			return 0
	except Exception:
		pass

	if deadline is None:
		deadline = time.time() + wait_seconds

	# Prune finished / expired ids so the wait can end. As each job goes
	# inactive, capture its terminal status (Completed / Failed / Timeout /
	# Stopped) before dropping it, so it's reported even if it failed or timed
	# out and produced no recording.
	still_running: list[str] = []
	for jid in pending:
		if _rq_job_active(jid):
			still_running.append(jid)
		else:
			_capture_job_terminal_status(session_uuid, jid)
			try:
				session.clear_pending_job(session_uuid, jid)
			except Exception:
				pass

	if not still_running:
		return 0  # everything finished — proceed
	if time.time() >= deadline:
		_finalize_pending_statuses(session_uuid, still_running)
		return len(still_running)  # cap hit — proceed; caller warns

	# Keep the UI honest while we wait.
	try:
		frappe.db.set_value("Optimus Session", docname, "status", "Analyzing")
		safe_commit()
	except Exception:
		pass
	_publish_progress(
		2, f"Waiting for {len(still_running)} RQ Job(s) to finish…", session_uuid
	)

	# Throttle, then re-enqueue ourselves so the worker can run the pending
	# job(s) meanwhile.
	time.sleep(min(_BG_WAIT_THROTTLE_SECONDS, max(0.0, deadline - time.time())))
	if time.time() >= deadline:
		_finalize_pending_statuses(session_uuid, still_running)
		return sum(1 for jid in still_running if _rq_job_active(jid))

	try:
		frappe.enqueue(
			"optimus.analyze.run",
			queue="long",
			session_uuid=session_uuid,
			_bg_wait_until=deadline,
		)
	except Exception as exc:
		frappe.log_error(title="optimus bg-job wait re-enqueue")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.bg_job_wait_reenqueue", exc,
				context={"session_uuid": session_uuid or ""},
			)
		except Exception:
			pass
		return 0  # couldn't re-enqueue — just proceed
	return None


def _auto_arm_phase2(docname: str, context) -> None:
	"""v0.7.x (P3): when ``optimus_phase2_auto_arm`` is set in site_config, arm
	a phase-2 line-profile pass on the recommended hot-path functions right
	after analyze finishes — so the user just re-runs the flow ONCE to get
	line-level data, with no manual picking.

	Opt-in + admin-only (site_config, not a casual UI toggle): arming
	instruments the user's NEXT execution of the flow, so it's only sensible
	for replay-safe flows / non-production. Heavily guarded and fully
	best-effort — it must NEVER fail analyze (the report is already saved)."""
	try:
		if not frappe.conf.get("optimus_phase2_auto_arm"):
			return

		import uuid as _uuid

		from frappe.utils import now_datetime

		from optimus.line_profile import capture as _lp_capture
		from optimus.line_profile import picker as _lp_picker
		from optimus.settings import get_config

		doc = frappe.get_doc("Optimus Session", docname)
		user = getattr(doc, "user", None)
		if not user:
			return

		# Guard: respect the per-session run cap.
		cap = int(getattr(get_config(), "phase2_max_runs_per_session", 10) or 10)
		if len(doc.get("phase_2_runs") or []) >= cap:
			return

		# Guard: don't arm over an active phase-1 or phase-2 pass for this user.
		try:
			if (
				frappe.cache.get_value(_redis_keys.lp_active(user))
				or frappe.cache.get_value(_redis_keys.session_active(user))
			):
				return
		except Exception:
			pass

		# Recommended hot paths, derived from the persisted call trees (same
		# candidate builder the picker uses).
		trees: list = []
		for action in (doc.get("actions") or []):
			raw = getattr(action, "call_tree_json", None)
			if not raw:
				continue
			try:
				tree = json.loads(raw)
			except (TypeError, ValueError):
				continue
			if isinstance(tree, dict) and "root" in tree:
				tree = tree["root"]
			trees.append(tree)

		candidates = _lp_picker._build_tree_indented_candidates(trees)
		picks = [
			{"dotted_path": c["dotted_path"], "source": "curated"}
			for c in candidates
			if c.get("recommended") and c.get("dotted_path")
		]
		if not picks:
			return

		run_uuid = _uuid.uuid4().hex
		resolved = _lp_capture.start_line_profile_pass(
			session_uuid=context.session_uuid,
			run_uuid=run_uuid,
			user=user,
			picks=picks,
		)
		eligible = [r for r in (resolved or []) if r.get("eligible")]
		if not eligible:
			return

		doc.append("phase_2_runs", {
			"run_uuid": run_uuid,
			"status": "Recording",
			"started_at": now_datetime(),
			"picks_json": frappe.as_json([
				{"dotted_path": r["dotted_path"], "source": r.get("source", "curated")}
				for r in eligible
			]),
		})
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)
		safe_commit()

		# Auto-arm runs server-side during analyze, when the user isn't on the
		# form — tell them a pass is armed and what to do next (re-run + Stop),
		# since arming alone does nothing until the flow re-executes.
		functions = [r["dotted_path"].rsplit(".", 1)[-1] for r in eligible][:5]
		try:
			frappe.publish_realtime(
				"optimus_phase2_armed",
				{
					"docname": docname,
					"run_uuid": run_uuid,
					"count": len(eligible),
					"functions": functions,
				},
				user=user,
			)
		except Exception:
			pass
		frappe.logger().info(
			f"optimus: auto-armed phase-2 pass {run_uuid} for {docname} "
			f"({len(eligible)} function(s)) — re-run the flow + Stop to capture line data."
		)
	except Exception as exc:
		# Never let auto-arm break a finished analyze.
		frappe.log_error(title="optimus auto-arm phase 2")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.auto_arm_phase2", exc,
				context={"docname": docname or ""},
			)
		except Exception:
			pass


def run(session_uuid: str, _bg_wait_until: float | None = None,
	_singleflight_deadline: float | None = None):
	"""Background-job entry point. Called from api.stop() via frappe.enqueue.

	``_bg_wait_until`` is set only when ``run`` re-enqueues itself while
	waiting for the flow's background jobs to finish (see
	``_bg_wait_for_pending_jobs``) — external callers never pass it."""
	# Round 2 fix #6: mark this request-context as "analyzing" so our
	# before_request / before_job hooks don't recursively activate the
	# recorder on the DocType writes we're about to do. Without this,
	# if the recording user also has an active profiler session (e.g.
	# multiple sessions started in sequence) we could recurse.
	frappe.local.optimus_analyzing = True

	# v0.7.x (M5): de-prioritize this analyze on the async (worker) path so it
	# doesn't starve live web traffic. Skipped inline (scheduler disabled) —
	# os.nice is sticky per-process and would de-prioritize the shared gunicorn
	# worker for unrelated later requests.
	try:
		if not is_scheduler_disabled():
			_apply_nice()
	except Exception:
		pass

	docname = frappe.db.get_value("Optimus Session", {"session_uuid": session_uuid}, "name")
	if not docname:
		frappe.log_error(
			title="optimus analyze",
			message=f"No Optimus Session found for uuid {session_uuid}",
		)
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.missing_session",
				context={"session_uuid": session_uuid or ""},
			)
		except Exception:
			pass
		return

	analyze_start = time.monotonic()
	bg_jobs_unfinished = 0

	try:
		# v0.6.0: wait for the background jobs the profiled flow enqueued to
		# finish before gathering recordings — so jobs a worker picks up
		# shortly after Stop aren't lost. Re-enqueues self (yielding the
		# worker) between checks; no-op when nothing's pending / the wait is
		# disabled / no async worker is available.
		bg_jobs_unfinished = _bg_wait_for_pending_jobs(session_uuid, docname, _bg_wait_until)
		if bg_jobs_unfinished is None:
			return  # re-enqueued — this invocation is done

		# v0.7.x (M2): global single-flight — only one session does the heavy,
		# memory-hungry analyze at a time so two stops can't stack to ~2× RAM
		# and OOM the box. If another session holds the flag we re-enqueue
		# ourselves and yield (skipped on the inline path). Acquired here, before
		# the big materialization below; heartbeated at the progress milestones;
		# released in the `finally`.
		if not _acquire_singleflight(session_uuid, docname, _singleflight_deadline):
			return  # re-enqueued — this invocation is done

		# Phase: Analyzing
		frappe.db.set_value("Optimus Session", docname, "status", "Analyzing")
		safe_commit()
		# v0.5.1: push "analyzing" to any open widgets on this user's
		# session. Without this the widget would either have to poll
		# status() to learn about the transition, or rely on the inline
		# path (which only applies when scheduler is disabled). Pushing
		# from the background analyze worker covers the enqueued path too.
		_publish_session_event(
			"optimus_session_analyzing",
			session_uuid=session_uuid,
			docname=docname,
		)
		_publish_progress(5, "Fetching recordings", session_uuid)

		recording_uuids = session.get_recordings(session_uuid)
		# v0.3.0: _fetch_recordings is now a generator. Materialize here
		# for the analyzer pipeline (which makes multiple passes), but
		# each recording's pyi_session is dropped after call_tree finishes
		# with it (handled in the analyzer itself).
		recordings = list(_fetch_recordings(recording_uuids))

		if not recordings:
			_finalize_with_empty_session(docname)
			session.delete_session_state(session_uuid)
			_publish_progress(100, "Complete (no data)", session_uuid)
			return

		_touch_singleflight(session_uuid)  # M2 heartbeat before the EXPLAIN burst
		_publish_progress(20, "Running EXPLAIN on queries", session_uuid)
		enrichment_warnings = _enrich_recordings(recordings)

		context = AnalyzeContext(session_uuid=session_uuid, docname=docname)
		context.warnings.extend(enrichment_warnings)

		# v0.6.0: if we hit the wait cap with jobs still running, say so.
		if bg_jobs_unfinished:
			context.warnings.append(
				f"{bg_jobs_unfinished} RQ Job(s) the flow enqueued were still "
				"running when analysis started - they aren't included. Click Retry "
				"Analyze once they finish, or raise 'Wait for RQ Jobs' in "
				"Optimus Settings."
			)

		# Surface any caps the session hit during recording
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("cap_warning"):
			context.warnings.append(meta["cap_warning"])

		# v0.5.0: load the frontend metrics posted by optimus_frontend.js
		# for consumption by the frontend_timings analyzer. Reads from the
		# two atomic Redis lists written by api.submit_frontend_metrics
		# (v0.5.1+). Falls back to the pre-v0.5.1 single-blob format if
		# that's what's in Redis — for upgrade safety on sessions
		# captured just before the update.
		try:
			from optimus import api as _api
			context.frontend_data = _api._read_frontend_data(session_uuid)
			if (
				not context.frontend_data.get("xhr")
				and not context.frontend_data.get("vitals")
			):
				legacy = frappe.cache.get_value(
					_redis_keys.frontend_legacy(session_uuid)
				)
				if legacy and isinstance(legacy, dict):
					context.frontend_data = legacy
		except Exception:
			context.frontend_data = {"xhr": [], "vitals": []}

		# v0.5.0: attach per-recording infra dicts from profiler:infra:<uuid>
		# so infra_pressure.analyze can read them as rec["infra"] without a
		# Redis hop inside the analyzer.
		for rec in recordings:
			rec_uuid = rec.get("uuid")
			if not rec_uuid:
				continue
			infra_blob = frappe.cache.get_value(_redis_keys.infra(rec_uuid))
			if infra_blob:
				rec["infra"] = infra_blob

		_publish_progress(50, "Running analyzers", session_uuid)
		analyzers = _get_analyzers()
		for i, analyzer in enumerate(analyzers):
			# v0.3.0: total wall-clock budget check. If we're over budget,
			# skip remaining analyzers and finalize partial.
			if time.monotonic() - analyze_start > ANALYZE_TOTAL_BUDGET_SECONDS:
				skipped = len(analyzers) - i
				context.warnings.append(
					f"Analyze partially completed (timeout) — "
					f"{skipped} analyzer(s) skipped"
				)
				break

			analyzer_name = getattr(analyzer, "__module__", "<unknown>")
			analyzer_start = time.monotonic()
			try:
				result = analyzer(recordings, context)
				context.merge(result)
			except Exception as exc:
				context.warnings.append(f"Analyzer {analyzer_name} failed (see error log)")
				frappe.log_error(title=f"optimus analyzer {analyzer_name}")
				try:
					from optimus import telemetry
					telemetry.emit_failure(
						"analyze.analyzer_failed", exc,
						context={"analyzer": analyzer_name, "session_uuid": session_uuid or ""},
					)
				except Exception:
					pass

			# v0.3.0: per-analyzer soft cap warning (logged, not fatal)
			analyzer_elapsed = time.monotonic() - analyzer_start
			if analyzer_elapsed > ANALYZE_PER_ANALYZER_SOFT_CAP_SECONDS:
				context.warnings.append(
					f"Analyzer {analyzer_name} took {analyzer_elapsed:.0f}s "
					f"(soft cap is {ANALYZE_PER_ANALYZER_SOFT_CAP_SECONDS}s)"
				)

			# Progress between 50-75% spread across analyzers
			pct = 50 + (25 * (i + 1) / len(analyzers))
			_publish_progress(pct, f"Ran {analyzer_name.split('.')[-1]}", session_uuid)

		# v0.7.x (M1): call_tree freed each recording's raw pyinstrument tree as
		# it consumed it. Force a collection now so that memory is returned to
		# the allocator BEFORE the persist + render phase (the next RAM peak),
		# instead of waiting for a generational GC cycle. Conf-gated so it can be
		# turned off if it ever shows up as latency on small sessions.
		if frappe.conf.get("optimus_analyze_gc_collect", True):
			import gc
			gc.collect()

		# How long did analyze take so far (before report rendering)?
		analyze_elapsed_ms = (time.monotonic() - analyze_start) * 1000

		# v0.6.0: attach ±1-line source snippets to each finding's callsite
		# before persisting, so finding cards can show the offending line
		# without requiring a per-render file read.
		_enrich_findings_with_source_snippets(context.findings)

		# v0.6.0: optionally bake LLM fix suggestions into the report
		# (Optimus Settings ▸ AI Fix Suggestions ▸ "Suggest AI fixes by
		# default"). Best-effort + time-budgeted — and double-wrapped here so
		# even a bug in the AI path can NEVER fail the analyze. If the LLM
		# was unavailable / errored, the session still completes; you can
		# fill the suggestions in afterward via the "Generate AI fixes"
		# button on the form (api.backfill_ai_fixes).
		try:
			_enrich_findings_with_ai_suggestions(context, recordings=recordings)
		except Exception as exc:
			try:
				context.warnings.append(
					"AI auto-suggest was skipped after an unexpected error — "
					"use 'Generate AI fixes' on the session form to fill them in. "
					"(see error log)"
				)
				frappe.log_error(title="optimus ai auto-suggest (outer)")
			except Exception:
				pass
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.ai_auto_suggest_outer", exc,
					context={"session_uuid": session_uuid or ""},
				)
			except Exception:
				pass

		# v0.6.0: same toggle also bakes an LLM-vetted index recommendation
		# onto the top few tables in the breakdown. Best-effort + double-wrapped.
		try:
			_enrich_table_breakdown_with_ai_suggestions(context, recordings)
		except Exception as exc:
			try:
				frappe.log_error(title="optimus ai index-suggest (outer)")
			except Exception:
				pass
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.ai_index_suggest_outer", exc,
					context={"session_uuid": session_uuid or ""},
				)
			except Exception:
				pass

		_publish_progress(80, "Writing session data", session_uuid)
		_persist(docname, context, recordings, analyze_elapsed_ms)

		_touch_singleflight(session_uuid)  # M2 heartbeat before the render phase
		_publish_progress(90, "Rendering reports", session_uuid)
		# Render and attach the HTML report to the DocType.
		# IMPORTANT: this must run BEFORE _cleanup_redis, because raw mode
		# reads raw SQL, headers, form_dict, and full stack traces from the
		# in-memory recordings list (not from the DocType, which only has
		# normalized data).
		_render_and_attach_reports(docname, recordings)

		_cleanup_redis(session_uuid, recording_uuids)

		# Phase: Ready
		frappe.db.set_value("Optimus Session", docname, "status", "Ready")
		safe_commit()
		# v0.7.x (P3): opt-in — arm a phase-2 pass on the hot paths so the user
		# just re-runs the flow once for line data. Best-effort; never fails analyze.
		_auto_arm_phase2(docname, context)
		_publish_progress(100, "Report ready", session_uuid)

		# Notify the UI so the floating widget can navigate the user to
		# the report. v0.5.1: routed through _publish_session_event so
		# it looks up the user consistently with the other state
		# transitions (analyzing / failed).
		_publish_session_event(
			"optimus_session_ready",
			session_uuid=session_uuid,
			docname=docname,
		)

	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(title=f"optimus analyze {session_uuid}")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.run", exc, context={"session_uuid": session_uuid}
			)
		except Exception:
			pass
		try:
			frappe.db.set_value("Optimus Session", docname, "status", "Failed")
			safe_commit()
		except Exception:
			pass
		# v0.5.1: push "failed" to any open widgets so they transition
		# out of "Analyzing…" immediately instead of hanging forever.
		# Best-effort and isolated so a publish failure can't mask the
		# original exception the outer `raise` is about to re-raise.
		try:
			_publish_session_event(
				"optimus_session_failed",
				session_uuid=session_uuid,
				docname=docname,
			)
		except Exception:
			pass
		raise
	finally:
		# v0.7.x (M2): always release the single-flight flag so the next
		# session can analyze. Compare-then-delete (only if we still hold it);
		# a no-op on the early-return paths where we never acquired.
		_release_singleflight(session_uuid)
		# Round 2 fix #6: always clear the analyzing flag so subsequent
		# requests in the same worker process can profile normally.
		if hasattr(frappe.local, "optimus_analyzing"):
			del frappe.local.optimus_analyzing


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_recordings(recording_uuids: list[str]):
	"""Stream recording dicts from Redis, one at a time.

	v0.3.0 changes:
	  - This is now a generator (was: list-returning) so the analyze
	    pipeline can drop unpruned pyi_session blobs from memory between
	    recordings rather than holding all 200 in RAM at once.
	  - For each recording, also loads the per-recording pyi tree pickle
	    from `profiler:tree:<uuid>` and the sidecar log from
	    `profiler:sidecar:<uuid>`. Both are best-effort — failures
	    log a warning and yield None for the missing piece.

	Yields recording dicts shaped like:
	    {
	      ...existing recorder fields (uuid, calls, etc.)...
	      "pyi_session": <pyinstrument.session.Session or dict or None>,
	      "sidecar": <list[dict]>,
	    }
	"""
	import pickle

	for uuid in recording_uuids:
		rec = frappe.cache.hget(RECORDER_REQUEST_HASH, uuid)
		if not rec:
			continue

		# Load the pyinstrument tree pickle (best-effort). Phase K
		# hardening: every blob is HMAC-prefixed by
		# ``hooks_callbacks._dump_capture_state_to_redis`` -
		# ``session.unsign_blob`` rejects any tree whose signature
		# doesn't match the site's encryption_key, so a Redis-
		# poisoning attacker can't slip a malicious pickle in.
		#
		# Transition fallback: blobs written by code that predates the
		# HMAC rollout lack the signature; ``unsign_blob`` returns
		# ``None`` for them. To avoid silently degrading every
		# in-flight session at deploy time, we fall back to raw
		# ``pickle.loads`` on unsigned blobs when
		# ``optimus_allow_unsigned_pickles`` is truthy in
		# site_config.json (default True, since the Redis TTL is 10
		# minutes - admin flips it to False once the keyspace has
		# rolled over post-deploy). See SECURITY.md for the
		# post-deploy hardening note.
		pyi_session = None
		try:
			from optimus.session import unsign_blob
			tree_blob = frappe.cache.get_value(_redis_keys.tree(uuid))
			if tree_blob:
				verified = unsign_blob(tree_blob)
				if verified is not None:
					pyi_session = pickle.loads(verified)
				else:
					# Either (a) blob predates HMAC rollout (raw
					# pickle, no prefix), or (b) the HMAC secret
					# drifted across processes so signed blobs land
					# here as ``32-byte sig + pickle``. Try BOTH
					# shapes - the first ``pickle.loads`` that
					# succeeds wins.
					allow_unsigned = True
					try:
						allow_unsigned = bool(
							frappe.conf.get("optimus_allow_unsigned_pickles", True)
						)
					except Exception:
						pass
					if allow_unsigned and isinstance(tree_blob, (bytes, bytearray)):
						attempts = [bytes(tree_blob)]
						if len(tree_blob) > 32:
							attempts.append(bytes(tree_blob[32:]))
						for payload in attempts:
							try:
								pyi_session = pickle.loads(payload)
								break
							except Exception:
								continue
						if pyi_session is None:
							frappe.log_error(
								title="optimus analyze",
								message=(
									f"Pyi tree load failed under both raw "
									f"and stripped-sig paths for {uuid}"
								),
							)
							try:
								from optimus import telemetry
								telemetry.emit_failure(
									"analyze.pyi_tree.load_failed_both_paths",
									context={"recording_uuid": uuid or ""},
								)
							except Exception:
								pass
						else:
							try:
								frappe.logger().warning(
									f"optimus analyze: loaded pyi tree for "
									f"{uuid} via unsigned-fallback path "
									f"(encryption_key missing in site_config "
									f"or HMAC secret drifted across "
									f"processes)."
								)
							except Exception:
								pass
					else:
						frappe.log_error(
							title="optimus analyze",
							message=(
								f"Pyi tree signature mismatch for {uuid}; "
								f"unsigned fallback disabled by site_config."
							),
						)
						try:
							from optimus import telemetry
							telemetry.emit_failure(
								"analyze.pyi_tree.signature_mismatch",
								context={"recording_uuid": uuid or ""},
							)
						except Exception:
							pass
		except Exception as exc:
			frappe.log_error(
				title="optimus analyze",
				message=f"Failed to deserialize pyi tree for {uuid}",
			)
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.pyi_tree.deserialize", exc,
					context={"recording_uuid": uuid or ""},
				)
			except Exception:
				pass
			pyi_session = None

		# Load the sidecar argument log (best-effort)
		sidecar = []
		try:
			loaded = frappe.cache.get_value(_redis_keys.sidecar(uuid))
			if isinstance(loaded, list):
				sidecar = loaded
		except Exception as exc:
			frappe.log_error(
				title="optimus analyze",
				message=f"Failed to load sidecar for {uuid}",
			)
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.sidecar.load_failed", exc,
					context={"recording_uuid": uuid or ""},
				)
			except Exception:
				pass
			sidecar = []

		rec["pyi_session"] = pyi_session
		rec["sidecar"] = sidecar
		yield rec


# Per-recording enrichment caps. On a session that hit the 200-recording
# cap with 5000 queries each, running EXPLAIN on every query would be
# nearly a million DB calls and would exceed the RQ long-queue timeout.
# We cap at a reasonable upper bound and dedupe EXPLAIN by normalized
# query shape so we only EXPLAIN each distinct query once per session.
#
# v0.5.3: the cap is now configurable via
# ``Optimus Settings ▸ Max Queries per Recording``. The constant
# below is the fallback default used when settings can't be read.
MAX_QUERIES_ENRICHED_PER_RECORDING = 2000

# Cross-session EXPLAIN cache (Round 2 fix #12). On a stable schema, two
# consecutive analyze runs often see the same query shapes. Caching the
# EXPLAIN result for an hour lets the second run skip the DB roundtrip
# entirely. Override per site via:
#   site_config.json: optimus_explain_cache_ttl_seconds (default 3600)
# Set to 0 to disable the cross-session cache entirely (falls back to
# per-session dedup only).
DEFAULT_EXPLAIN_CACHE_TTL = 3600

# Hard cap on the in-memory EXPLAIN cache per analyze run. At ~500B per
# EXPLAIN row × N rows per query, 1000 entries is roughly 5-50 MB - a
# safe ceiling that still hits ~99% of duplicate-shape queries on a
# normal session. Overflow evicts the oldest entry (FIFO).
_EXPLAIN_CACHE_MAX_PER_ANALYZE = 1000


def _cap_explain_cache(cache: OrderedDict, key: str, value):
	"""Insert into the ordered EXPLAIN cache; evict the oldest entry
	once we cross the cap so a session with 10K+ unique queries doesn't
	balloon memory."""
	cache[key] = value
	if len(cache) > _EXPLAIN_CACHE_MAX_PER_ANALYZE:
		cache.popitem(last=False)


def _enrich_recordings(recordings: list[dict]) -> list[str]:
	"""Mirror frappe.recorder.post_process for our recordings only.

	The vanilla post_process operates on every recording in
	RECORDER_REQUEST_HASH globally and starts a read-only DB transaction
	that would block our subsequent DocType writes. This version is
	scoped to our session's recordings and leaves the transaction state
	alone.

	Idempotent: safe to call on already-enriched recordings.

	Returns a list of warning strings that should be surfaced in the
	report — things like "we truncated X queries because the session hit
	the per-recording cap".
	"""
	# Flush any pending transaction state so EXPLAIN sees a consistent
	# snapshot (Round 2 fix #4). The caller commits right before invoking
	# us so this is usually a no-op, but it's a cheap defensive check
	# that protects against future refactors that might leave dirty
	# state in the transaction.
	try:
		safe_commit()
	except Exception:
		pass

	warnings: list[str] = []
	truncated_queries = 0
	total_queries_seen = 0   # for the truncation-banner percentage

	# v0.7.x (M6): optional throttle of the EXPLAIN/sqlparse burst so a long
	# flow doesn't hammer the DB/CPU continuously (and so GC gets breathing
	# room). Off by default (every=0) — exact current behavior, zero overhead.
	# Counts ACTUAL EXPLAIN executions (not cache hits); sleeping changes only
	# timing, never which queries are EXPLAINed or any result.
	try:
		throttle_every = int(frappe.conf.get("optimus_enrich_throttle_every") or 0)
	except Exception:
		throttle_every = 0
	try:
		throttle_sleep_s = float(
			frappe.conf.get("optimus_enrich_throttle_sleep_ms") or 5
		) / 1000.0
	except Exception:
		throttle_sleep_s = 0.005
	explains_issued = 0
	# Cache EXPLAIN results by normalized query shape so we don't re-run
	# EXPLAIN on the same query hundreds of times within a single session.
	# This is the in-memory first tier; the second tier is the
	# cross-session frappe.cache lookup below. Bounded with FIFO
	# eviction at _EXPLAIN_CACHE_MAX_PER_ANALYZE to keep a 10K-unique-
	# query session from balloning memory.
	explain_cache: OrderedDict[str, list] = OrderedDict()

	# Cross-session EXPLAIN cache config
	cache_ttl = int(
		frappe.conf.get("optimus_explain_cache_ttl_seconds")
		if frappe.conf.get("optimus_explain_cache_ttl_seconds") is not None
		else DEFAULT_EXPLAIN_CACHE_TTL
	)
	use_shared_cache = cache_ttl > 0

	# v0.5.3: per-recording cap is admin-configurable. Fall back to
	# the hardcoded default if settings read fails for any reason —
	# we must never let a settings hiccup starve the analyze pipeline.
	try:
		from optimus.settings import get_config
		cap = int(get_config().max_queries_per_recording)
	except Exception:
		cap = MAX_QUERIES_ENRICHED_PER_RECORDING
	if cap <= 0:
		cap = MAX_QUERIES_ENRICHED_PER_RECORDING

	for recording in recordings:
		calls = recording.get("calls") or []
		total_queries_seen += len(calls)
		if len(calls) > cap:
			truncated_queries += len(calls) - cap
			calls = calls[:cap]
			recording["calls"] = calls

		for call in calls:
			query = call.get("query", "")
			if not query:
				continue

			# sqlparse format (idempotent on already-formatted SQL)
			try:
				call["query"] = sqlparse.format(
					query.strip(),
					keyword_case="upper",
					reindent=True,
					strip_comments=True,
				)
			except Exception:
				pass

			# Skip EXPLAIN if already populated
			if call.get("explain_result"):
				continue

			if not is_query_type(call["query"], ("select", "update", "delete")):
				call["explain_result"] = []
				continue

			# Dedupe EXPLAIN by normalized query shape. The recorder
			# hasn't computed normalized_query yet (mark_duplicates runs
			# below), so compute a temporary shape key here.
			cache_key = _shape_key(call["query"])

			# First tier: in-memory cache for this analyze run
			if cache_key in explain_cache:
				call["explain_result"] = explain_cache[cache_key]
				continue

			# Second tier: cross-session frappe.cache with TTL. Two
			# analyze runs on a stable schema will hit this cache.
			shared_key = _redis_keys.explain_cache(cache_key)
			if use_shared_cache:
				cached = frappe.cache.get_value(shared_key)
				if cached is not None:
					call["explain_result"] = cached
					_cap_explain_cache(explain_cache, cache_key, cached)
					continue

			try:
				result = frappe.db.sql(
					f"EXPLAIN {call['query']}", as_dict=True
				)
				call["explain_result"] = result
				_cap_explain_cache(explain_cache, cache_key, result)
				if use_shared_cache:
					try:
						frappe.cache.set_value(
							shared_key, result, expires_in_sec=cache_ttl
						)
					except Exception:
						pass  # shared cache is best-effort
			except Exception:
				call["explain_result"] = []
				_cap_explain_cache(explain_cache, cache_key, [])

			if "explain_result" not in call:
				call["explain_result"] = []

			# M6: we just issued a real EXPLAIN (cache hits / non-SELECTs
			# `continue` above and don't reach here). Yield periodically.
			explains_issued += 1
			if throttle_every and explains_issued % throttle_every == 0:
				time.sleep(throttle_sleep_s)

		# mark_duplicates adds normalized_query, exact_copies, normalized_copies, index
		try:
			mark_duplicates(recording)
		except Exception:
			pass

	if truncated_queries:
		pct = (
			round(truncated_queries / total_queries_seen * 100)
			if total_queries_seen else 0
		)
		# v0.5.3: the renderer reads this structured marker and shows
		# a prominent banner at the top of the report, in addition to
		# the Analyzer Notes entry. Users were missing truncation
		# warnings when they only appeared in the collapsed bottom
		# section — "166 queries truncated" buried below stats cards
		# and findings led to developers reading an incomplete report
		# without noticing.
		warnings.append(
			f"⚠ TRUNCATED: {truncated_queries} queries "
			f"({pct}% of the flow) exceeded the {cap}-queries-per-"
			"recording enrichment cap and were analyzed without "
			"EXPLAIN / normalization. The report covers the first "
			f"{cap} queries per recording; the rest are visible in "
			"Top Queries but not in index-suggestion / full-scan / "
			"filter-ratio findings. "
			"To get full coverage, raise "
			"<b>Optimus Settings ▸ Max Queries per Recording</b> "
			"(default 2000, try 5000-10000) and re-run the session "
			"— OR profile a shorter flow."
		)
	return warnings


def _shape_key(query: str) -> str:
	"""Cheap query-shape key for EXPLAIN dedup.

	Lower-cases, collapses whitespace, and truncates. This is NOT the
	proper sqlparse normalization — we just need "are these two queries
	shaped the same" for caching purposes. The REAL normalization happens
	in mark_duplicates afterward.
	"""
	import re

	return re.sub(r"\s+", " ", query.lower().strip())[:500]


# ---------------------------------------------------------------------------
# v0.3.0: call_tree_json overflow handling
# ---------------------------------------------------------------------------


def _apply_overflow_or_pass(
	tree_json: str,
	action_idx: int,
	docname: str,
	write_file=None,
	warnings_sink: list = None,
	hard_max_bytes: int = CALL_TREE_HARD_MAX_BYTES,
) -> tuple[str, str | None]:
	"""Decide whether to inline, overflow-to-file, or hard-truncate a tree blob.

	Returns:
	    (json_to_persist, overflow_file_url_or_None)

	  - If `tree_json` is < CALL_TREE_OVERFLOW_THRESHOLD_BYTES → return as-is.
	  - If `tree_json` is between the threshold and hard_max_bytes → call
	    write_file(filename, content) to create a private File attachment;
	    return a one-line marker JSON pointing at the URL. On write failure,
	    fall back to a hard-truncated tree.
	  - If `tree_json` is > hard_max_bytes → hard-truncate immediately
	    without attempting an overflow file.
	"""
	import json as _json

	size = len(tree_json)
	warnings = warnings_sink if warnings_sink is not None else []

	# Path 1: small enough to inline
	if size < CALL_TREE_OVERFLOW_THRESHOLD_BYTES:
		return tree_json, None

	# Path 2: hard sanity guard — truncate immediately, never attempt file
	if size > hard_max_bytes:
		warnings.append(
			f"Action {action_idx}: tree exceeded hard guard "
			f"({size} > {hard_max_bytes}); hard-truncated"
		)
		return _hard_truncate_tree(tree_json), None

	# Path 3: try to write overflow file
	if write_file is None:
		warnings.append(
			f"Action {action_idx}: no overflow writer; tree hard-truncated"
		)
		return _hard_truncate_tree(tree_json), None

	filename = f"optimus_call_tree_{docname}_action_{action_idx}.json"
	try:
		url = write_file(filename, tree_json.encode("utf-8"))
		marker = _json.dumps({"_overflow": True, "url": url})
		return marker, url
	except Exception:
		warnings.append(
			f"Action {action_idx}: overflow file write failed; "
			"tree hard-truncated"
		)
		return _hard_truncate_tree(tree_json), None


def _hard_truncate_tree(tree_json: str) -> str:
	"""Build a top-100-frames truncated tree JSON as a fallback."""
	import json as _json

	try:
		tree = _json.loads(tree_json)
	except Exception:
		return _json.dumps({
			"_truncated": True, "_parse_failed": True,
			"function": "<root>", "children": [],
		})

	# Walk and collect the top-N frames by cumulative_ms
	all_nodes: list = []

	def collect(n):
		all_nodes.append(n)
		for c in n.get("children", []):
			collect(c)

	collect(tree)
	all_nodes.sort(key=lambda n: n.get("cumulative_ms", 0), reverse=True)
	kept = all_nodes[:CALL_TREE_HARD_TRUNCATE_KEEP_FRAMES]

	flat_children = [
		{
			"function": n.get("function"),
			"filename": n.get("filename"),
			"lineno": n.get("lineno"),
			"self_ms": n.get("self_ms", 0),
			"cumulative_ms": n.get("cumulative_ms", 0),
			"kind": n.get("kind", "python"),
			"children": [],
		}
		for n in kept
	]
	out = {
		"_truncated": True,
		# B.DI2 — preserve the original frame count so the renderer can
		# show a "captured X frames, only top N shown" banner. Without
		# these, the truncation is invisible after persistence.
		"_captured_frames": len(all_nodes),
		"_kept_frames": len(kept),
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0,
		"cumulative_ms": tree.get("cumulative_ms", 0),
		"kind": "python",
		"children": flat_children,
	}
	return _json.dumps(out, default=str)


def _persist(
	docname: str,
	context: AnalyzeContext,
	recordings: list[dict],
	analyze_elapsed_ms: float = 0,
) -> None:
	"""Write the analyzed data into the Optimus Session DocType row."""
	total_requests = len(recordings)
	total_queries = sum(len(r.get("calls") or []) for r in recordings)
	total_query_time_ms = sum(
		sum(c.get("duration", 0) for c in r.get("calls") or []) for r in recordings
	)
	total_duration_ms = sum(r.get("duration", 0) for r in recordings)

	doc = frappe.get_doc("Optimus Session", docname)

	# v0.5.1: auto-fill "Steps to Reproduce" from the captured actions,
	# but ONLY if the user hasn't already written notes on the form. The
	# start dialog no longer prompts for notes (it added friction and
	# most users just skipped it), so the common path is: field is
	# empty → we populate it here. Power users who typed something into
	# the notes field on the doc form between start and stop get their
	# text left alone.
	if not (doc.notes or "").strip():
		# v0.6.0: when AI is enabled, draft a friendly human-readable flow
		# (with the raw action list kept below); otherwise — or if the LLM
		# call fails — fall back to the plain labelled list.
		notes_html = _build_humanized_notes_html(
			recordings, session_title=(doc.title or None)
		) or _build_auto_notes_html(recordings)
		if notes_html:
			doc.notes = notes_html

	doc.total_requests = total_requests
	doc.total_queries = total_queries
	doc.total_query_time_ms = round(total_query_time_ms, 2)
	doc.total_duration_ms = round(total_duration_ms, 2)
	doc.analyze_duration_ms = round(analyze_elapsed_ms, 2)
	doc.top_severity = _compute_top_severity(context.findings)
	doc.summary_html = _build_summary_html(context, total_queries, recordings)
	doc.top_queries_json = json.dumps(
		context.aggregate.get("top_queries", []), default=str
	)
	doc.table_breakdown_json = json.dumps(
		context.aggregate.get("table_breakdown", []), default=str
	)
	# v0.5.0: infra_pressure and frontend_timings aggregates. Capped to
	# prevent unbounded growth on pathological 200-recording sessions —
	# without caps, v5_aggregate_json could balloon to 1 MB+ and slow
	# the form load for the Optimus Session record. Truncation is
	# tail-preferring (keep the last N entries) with a warning surfaced
	# in analyzer_warnings so operators can see the drop.
	V5_INFRA_TIMELINE_CAP = 200
	V5_FRONTEND_XHR_CAP = 500
	V5_FRONTEND_ORPHANS_CAP = 100

	infra_timeline = context.aggregate.get("infra_timeline", [])
	if len(infra_timeline) > V5_INFRA_TIMELINE_CAP:
		context.warnings.append(
			f"v5_aggregate: infra_timeline truncated from "
			f"{len(infra_timeline)} to {V5_INFRA_TIMELINE_CAP} entries (tail kept)"
		)
		infra_timeline = infra_timeline[-V5_INFRA_TIMELINE_CAP:]

	frontend_xhr = context.aggregate.get("frontend_xhr_matched", [])
	if len(frontend_xhr) > V5_FRONTEND_XHR_CAP:
		context.warnings.append(
			f"v5_aggregate: frontend_xhr_matched truncated from "
			f"{len(frontend_xhr)} to {V5_FRONTEND_XHR_CAP} entries (tail kept)"
		)
		frontend_xhr = frontend_xhr[-V5_FRONTEND_XHR_CAP:]

	frontend_orphans = context.aggregate.get("frontend_orphans", [])
	if len(frontend_orphans) > V5_FRONTEND_ORPHANS_CAP:
		context.warnings.append(
			f"v5_aggregate: frontend_orphans truncated from "
			f"{len(frontend_orphans)} to {V5_FRONTEND_ORPHANS_CAP} entries"
		)
		frontend_orphans = frontend_orphans[-V5_FRONTEND_ORPHANS_CAP:]

	doc.v5_aggregate_json = json.dumps(
		{
			"infra_timeline": infra_timeline,
			"infra_summary": context.aggregate.get("infra_summary", {}),
			"frontend_xhr_matched": frontend_xhr,
			"frontend_vitals_by_page": context.aggregate.get("frontend_vitals_by_page", {}),
			"frontend_orphans": frontend_orphans,
			"frontend_summary": context.aggregate.get("frontend_summary", {}),
		},
		default=str,
	)
	# v0.3.0: call_tree analyzer outputs
	doc.hot_frames_json = json.dumps(
		context.aggregate.get("hot_frames", []), default=str
	)
	doc.session_time_breakdown_json = json.dumps(
		context.aggregate.get("session_time_breakdown", {}), default=str
	)
	doc.total_python_ms = round(context.aggregate.get("total_python_ms", 0), 2)
	doc.total_sql_ms = round(context.aggregate.get("total_sql_ms", 0), 2)
	doc.analyzer_warnings = "\n".join(context.warnings) if context.warnings else None

	# v0.3.0: apply overflow / hard-truncate to each action's call_tree_json.
	def _writer(filename, content, _docname=docname):
		file_doc = frappe.get_doc({
			"doctype": "File",
			"file_name": filename,
			"attached_to_doctype": "Optimus Session",
			"attached_to_name": _docname,
			"is_private": 1,
			"content": content,
		})
		file_doc.insert(ignore_permissions=True)
		return file_doc.file_url

	for action in context.actions:
		tree_json = action.get("call_tree_json")
		if not tree_json:
			continue
		new_json, overflow_url = _apply_overflow_or_pass(
			tree_json,
			action_idx=action.get("idx", 0),
			docname=docname,
			write_file=_writer,
			warnings_sink=context.warnings,
		)
		action["call_tree_json"] = new_json
		action["call_tree_size_bytes"] = len(new_json)
		action["call_tree_overflow_file"] = overflow_url

	# Reset and re-populate child tables (in case of re-run)
	doc.set("actions", [])
	for action in context.actions:
		doc.append("actions", action)

	# v0.5.1: safety net for Optimus Finding.title's 140-char Frappe
	# limit. Individual analyzers already shorten filenames in titles
	# (see n_plus_one + call_tree), but pathological inputs — unusual
	# function names, very high occurrence counts, unexpected formats
	# from future analyzers — can still push past the limit and crash
	# the whole persist with CharacterLengthExceededError. We clamp
	# here so a single too-long title never destroys the entire
	# analyze run.
	# v0.7.x: collapse per-action duplicates of the same code path
	# (Slow Hot Path / Hook Bottleneck / Self-Time Hot Path / Repeated
	# Hot Frame). Runs BEFORE truncation so the dominant title is what
	# gets trimmed.
	_dedupe_findings_across_actions(context.findings, context.actions)
	_truncate_finding_titles(context.findings)

	doc.set("findings", [])
	for finding in context.findings:
		doc.append("findings", finding)

	# v0.7.x: one Optimus Background Job row per RQ job the flow enqueued, with
	# its terminal status — so failed / timed-out jobs are reported instead of
	# vanishing. Belt-and-suspenders: capture a status for any job the wait
	# didn't (e.g. the wait was skipped/disabled), then read the final hash.
	try:
		for _jm in session.get_jobs(context.session_uuid):
			if not _jm.get("status"):
				_capture_job_terminal_status(context.session_uuid, _jm["job_id"])
		doc.set("background_jobs", [])
		for _jm in session.get_jobs(context.session_uuid):
			try:
				doc.append("background_jobs", {
					# method / job_id are Data(140) and come from external sources
					# (the enqueue call, RQ): cap them so an unusually long value
					# can't raise a "Data too long" insert error and sink the whole
					# report save (the datetimes are already DB-safe via _rq_dt_to_db).
					"job_id": (_jm.get("job_id") or "")[:140],
					"method": (_jm.get("method") or "")[:140],
					"status": _jm.get("status") or "Running",
					"error": (_jm.get("error") or "") or None,
					# DB-safety guard: strip any tz offset a pre-fix build may have
					# stored in Redis, so MariaDB's DATETIME column accepts them.
					"enqueued_at": _db_datetime_str(_jm.get("enqueued_at")),
					"started_at": _db_datetime_str(_jm.get("started_at")),
					"ended_at": _db_datetime_str(_jm.get("ended_at")),
					"duration_ms": _jm.get("duration_ms") or 0,
					"recording_uuid": _jm.get("recording_uuid") or "",
				})
			except Exception as exc:
				frappe.log_error(title="optimus persist background_job row")
				try:
					from optimus import telemetry
					telemetry.emit_failure(
						"analyze.bg_job.persist_row", exc,
						context={"session_uuid": getattr(context, "session_uuid", "") or ""},
					)
				except Exception:
					pass
	except Exception as exc:
		frappe.log_error(title="optimus persist background_jobs")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.bg_job.persist_batch", exc,
				context={"session_uuid": getattr(context, "session_uuid", "") or ""},
			)
		except Exception:
			pass

	doc.save(ignore_permissions=True)
	safe_commit()


# v0.7.x: per-action analyzers can emit the same (code-path) finding
# once per action that exercises it. A recording with N actions
# touching the same hot function used to produce N near-identical
# cards (same drill-down, same Phase 2 callout, same AI fix). We
# collapse them into a single card here.
_DEDUPE_FINDING_TYPES = frozenset({
	"Slow Hot Path",
	"Hook Bottleneck",
	"Self-Time Hot Path",
	"Repeated Hot Frame",
})


def _dedupe_findings_across_actions(
	findings: list[dict],
	actions: list[dict] | None,
) -> None:
	"""Collapse per-action duplicates of the same code path in place.

	Groups findings by ``(finding_type, filename, lineno, function)``
	for the types listed in ``_DEDUPE_FINDING_TYPES``. When a group has
	2+ findings, the highest-impact one becomes the "dominant"; the
	others are dropped after their action context is folded into the
	dominant's ``technical_detail_json`` (under ``merged_action_refs``
	and friends) and a one-sentence "Also affects …" note is appended
	to the dominant's ``customer_description``.

	The dominant's ``estimated_impact_ms`` becomes the SUM across the
	group, ``affected_count`` becomes the number of merged findings.
	Its severity / title / action_ref / drilldown_chain stay
	unchanged so downstream renderers resolve the same way they did
	before dedup.

	Findings outside the dedup-eligible types pass through untouched
	(N+1 / SQL red flags / Hot Line have their own dedup logic or
	aren't per-action by construction).
	"""
	if not findings:
		return

	# Build action_ref → label lookup for the "Also affects" sentence.
	action_label_by_ref: dict[str, str] = {}
	for a in (actions or []):
		if not isinstance(a, dict):
			continue
		idx = a.get("idx")
		label = a.get("action_label") or a.get("path") or ""
		if idx is not None and label:
			action_label_by_ref[str(idx)] = label

	# Bucket by dedup key. Findings outside the dedupe set get a
	# unique passthrough key so they never collide.
	buckets: dict[tuple, list[int]] = {}
	passthrough_counter = 0
	keyed_indices: list[tuple[tuple, int]] = []

	for i, f in enumerate(findings):
		ftype = f.get("finding_type") or ""
		if ftype not in _DEDUPE_FINDING_TYPES:
			# Stable unique key so this finding passes through unchanged.
			passthrough_counter += 1
			key = ("__passthrough__", passthrough_counter)
			keyed_indices.append((key, i))
			continue
		try:
			detail = json.loads(f.get("technical_detail_json") or "{}")
		except (TypeError, ValueError):
			detail = {}
		filename = detail.get("filename") or ""
		lineno = detail.get("lineno")
		function = detail.get("function") or ""
		# Findings missing any of the key parts pass through (can't
		# group what we can't identify).
		if not filename or lineno is None or not function:
			passthrough_counter += 1
			key = ("__passthrough__", passthrough_counter)
		else:
			key = (ftype, filename, int(lineno) if isinstance(lineno, int | float) else lineno, function)
		keyed_indices.append((key, i))

	for key, i in keyed_indices:
		buckets.setdefault(key, []).append(i)

	# For each group with 2+ entries: pick dominant, fold others in,
	# remove dropped indices from `findings` at the end.
	to_drop: set[int] = set()
	for _key, indices in buckets.items():
		if len(indices) < 2:
			continue
		# Sort by impact desc; the head is the dominant.
		indices_sorted = sorted(
			indices,
			key=lambda i: -(findings[i].get("estimated_impact_ms") or 0),
		)
		dom_idx = indices_sorted[0]
		dom = findings[dom_idx]
		merged_impacts: list[dict] = []
		merged_refs: list[str] = []
		merged_labels: list[str] = []
		total_impact = 0.0
		for i in indices_sorted:
			f = findings[i]
			ms = float(f.get("estimated_impact_ms") or 0)
			total_impact += ms
			ref = str(f.get("action_ref") or "")
			label = action_label_by_ref.get(ref, "")
			merged_refs.append(ref)
			merged_labels.append(label)
			merged_impacts.append({"action_ref": ref, "label": label, "ms": round(ms, 2)})
			if i != dom_idx:
				to_drop.add(i)

		# Mutate dominant in place.
		dom["estimated_impact_ms"] = round(total_impact, 2)
		dom["affected_count"] = len(indices_sorted)
		try:
			dom_detail = json.loads(dom.get("technical_detail_json") or "{}")
		except (TypeError, ValueError):
			dom_detail = {}
		dom_detail["merged_count"] = len(indices_sorted)
		dom_detail["merged_action_refs"] = merged_refs
		dom_detail["merged_action_labels"] = merged_labels
		dom_detail["merged_impact_ms"] = merged_impacts
		dom["technical_detail_json"] = json.dumps(dom_detail, default=str)

		# Append "Also affects …" note to customer_description.
		other_entries = [e for e in merged_impacts[1:] if e.get("label") or e.get("ms")]
		if other_entries:
			if all(e.get("label") for e in other_entries):
				bits = ", ".join(
					f"**{e['label']}** ({e['ms']:.0f}ms)" for e in other_entries
				)
				suffix = (
					f" Also affects {len(other_entries)} other "
					f"action{'s' if len(other_entries) != 1 else ''}: {bits}."
				)
			else:
				# Labels missing — generic count.
				suffix = (
					f" Also affects {len(other_entries)} other "
					f"action{'s' if len(other_entries) != 1 else ''}."
				)
			existing = dom.get("customer_description") or ""
			dom["customer_description"] = existing.rstrip() + "\n\n" + suffix.lstrip()

	if to_drop:
		# Compact the list in place. Iterating backwards so we don't
		# shift indices we still need to drop.
		for i in sorted(to_drop, reverse=True):
			findings.pop(i)


# Optimus Finding.title is a Frappe Data field — VARCHAR(140). Titles
# that exceed this length raise CharacterLengthExceededError at save
# time, taking down the whole analyze pipeline. We clamp in-place as a
# safety net: 137 visible chars + the "..." ellipsis marker fits the
# limit exactly, and the full information remains in the finding's
# technical_detail_json for navigation.
_FINDING_TITLE_MAX_CHARS = 140
_FINDING_TITLE_ELLIPSIS = "..."


def _truncate_finding_titles(findings: list[dict]) -> None:
	"""Clamp every finding's title to <= _FINDING_TITLE_MAX_CHARS chars.

	Mutates the findings list in place. Intended as a defense-in-depth
	guard: analyzers should produce short titles to begin with (see
	analyzers/base.short_filename), but pathological data can still
	produce over-long titles on corner cases the analyzers didn't
	anticipate. Rather than crash the whole persist, we clamp and
	keep the full information in technical_detail_json.
	"""
	for finding in findings:
		title = finding.get("title") or ""
		if len(title) > _FINDING_TITLE_MAX_CHARS:
			keep = _FINDING_TITLE_MAX_CHARS - len(_FINDING_TITLE_ELLIPSIS)
			finding["title"] = title[:keep].rstrip() + _FINDING_TITLE_ELLIPSIS


# v0.6.0: ±1-line source snippet attached to each finding's callsite so
# the report's finding card can show the actual offending line without the
# reader having to jump into the codebase. Truncate per-line so a single
# multi-kilobyte minified string can't blow the technical_detail_json blob.
_FINDING_SNIPPET_TRUNCATE_CHARS = 200


def _enrich_findings_with_source_snippets(findings: list[dict]) -> None:
	"""Mutate findings in-place: attach a ±2-line source snippet to each
	finding whose technical_detail.callsite resolves to a readable file.

	Best-effort: missing files, decoding errors, out-of-range linenos,
	and malformed technical_detail_json all yield no snippet (and no
	warning). The renderer just skips the snippet block when absent.

	Files are cached per-call so a session with 30 N+1 findings clustered
	in a handful of source files reads each file once.
	"""
	file_cache: dict[str, list[str] | None] = {}

	for finding in findings:
		if not isinstance(finding, dict):
			# Defence-in-depth: the public surface is typed as list[dict]
			# but a corrupted upstream payload (legacy session, partial
			# migration) could plant a non-dict entry. Silent skip matches
			# the function's best-effort contract.
			continue
		raw = finding.get("technical_detail_json")
		if not raw:
			continue
		try:
			detail = json.loads(raw)
		except Exception:
			continue
		if not isinstance(detail, dict):
			# Valid JSON that isn't an object — e.g. a stray "string", a
			# bare ``null``, or a top-level list left by a stale persistence
			# path. ``.get()`` below would AttributeError; silent skip.
			continue
		callsite = detail.get("callsite") or {}
		if not isinstance(callsite, dict):
			# Slow Query findings (optimus/analyzers/top_queries.py) store
			# callsite as a "path:lineno" string, not the canonical dict
			# shape every other finding type uses. Skip analyze-time
			# enrichment here — renderer._normalize_callsite converts the
			# string at render time and renderer._finding_to_dict attaches
			# the snippet lazily, so no functionality is lost.
			continue
		if not callsite.get("filename"):
			# call_tree (Slow Hot Path / Hook Bottleneck / Repeated Hot Frame)
			# and line_profile (Hot Line) store the location at the top level —
			# synthesize a callsite dict so the snippet lands where
			# renderer._finding_to_dict expects it (and stop _finding_to_dict
			# from having to re-synthesize at render time).
			fname = detail.get("filename") or detail.get("file")
			if fname and detail.get("lineno") is not None:
				callsite = {
					"filename": fname,
					"lineno": detail.get("lineno"),
					"function": detail.get("function") or "",
				}
				detail["callsite"] = callsite
				finding["technical_detail_json"] = json.dumps(detail, default=str)
		filename = callsite.get("filename")
		try:
			lineno = int(callsite.get("lineno"))
		except (TypeError, ValueError):
			continue
		if not filename or lineno <= 0 or callsite.get("source_snippet"):
			continue
		# renderer._read_source_snippet resolves app-relative callsite paths
		# (e.g. "ugly_code/python/common.py") to real files; a bare open()
		# would fail because the worker cwd is <bench>/sites.
		snippet = renderer._read_source_snippet(filename, lineno, cache=file_cache)
		if not snippet:
			continue
		callsite["source_snippet"] = snippet
		detail["callsite"] = callsite
		finding["technical_detail_json"] = json.dumps(detail, default=str)


def _enrich_findings_with_ai_suggestions(context, *, recordings: list | None = None) -> None:
	"""Mutate ``context.findings`` in place: when Optimus Settings has
	``ai_enabled`` AND ``ai_auto_suggest``, ask the configured LLM for a
	fix for the top ``ai_auto_suggest_max`` eligible findings (0 = all),
	highest-severity / highest-impact first, and store the result on each
	finding's ``llm_fix_json`` so it shows up in the report (and is what
	the on-demand "Suggest a fix (AI)" button returns from cache).

	Best-effort + bounded: a misconfigured / unreachable provider, an
	individual finding that errors, or hitting ``AI_AUTO_SUGGEST_TIME_
	BUDGET_SECONDS`` just means fewer (or no) suggestions — never a failed
	analyze. The network I/O lives here in the orchestrator, never in an
	analyzer (the pure-analyzer contract is untouched).
	"""
	findings = context.findings or []
	if not findings:
		return

	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return
	if not (getattr(cfg, "ai_enabled", False) and getattr(cfg, "ai_suggest_findings", True)
	        and getattr(cfg, "ai_auto_suggest", False)):
		return

	from optimus import ai_fix

	if not ai_fix.is_available(section="findings"):
		context.warnings.append(
			"AI auto-suggest is on but the AI provider isn't fully configured — "
			"no suggestions were generated (see Optimus Settings ▸ AI Fix Suggestions)."
		)
		return

	eligible = [
		f for f in findings
		if (f.get("finding_type") or "") in ai_fix.AI_ELIGIBLE_FINDING_TYPES
	]
	# v0.9.0: per-type opt-out (Critical Risk #2). Filter BEFORE the loop so
	# the operator's exclusion list short-circuits payload-building too, not
	# just the network send. Visibility is via the v0.8.0 telemetry pipeline
	# — one event per auto-suggest run when the filter actually drops
	# findings, so the operator can see in the Optimus Telemetry Event
	# DocType that exclusion is taking effect.
	pre_exclusion_count = len(eligible)
	eligible = [f for f in eligible if not ai_fix.is_finding_type_excluded(f.get("finding_type"))]
	excluded_count = pre_exclusion_count - len(eligible)
	if excluded_count > 0:
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"ai.auto_suggest_skipped_by_exclusion",
				severity="warning",
				context={"excluded_count": excluded_count},
			)
		except Exception:
			pass
	if not eligible:
		return
	eligible.sort(key=lambda f: (
		SEVERITY_ORDER.get(f.get("severity") or "Low", 3),
		-(f.get("estimated_impact_ms") or 0),
	))
	cap = int(getattr(cfg, "ai_auto_suggest_max", 0) or 0)
	if cap > 0:
		eligible = eligible[:cap]

	from types import SimpleNamespace

	file_cache: dict = {}
	phase2_index = _phase2_index_for(getattr(context, "docname", None))
	# v0.6.x: when recordings are in scope (analyze-time path), build the
	# lookup maps once so each finding's payload can carry verbatim SQL
	# evidence (top-N queries from its action's recording).
	recordings_by_uuid = {
		(r.get("uuid") or ""): r for r in (recordings or []) if r.get("uuid")
	}
	actions_by_idx = {a["idx"]: a for a in (getattr(context, "actions", None) or []) if "idx" in a}
	started = time.monotonic()
	failures = 0
	skipped_for_time = 0
	total = len(eligible)
	for idx, f in enumerate(eligible):
		if time.monotonic() - started > AI_AUTO_SUGGEST_TIME_BUDGET_SECONDS:
			skipped_for_time = total - idx
			break
		# Live progress per finding — the floating widget / form headline
		# show movement during the (potentially minute-long) LLM round
		# trips instead of a frozen "Analyzing 78%". Range 78→80 leads into
		# the next milestone ("Writing session data").
		try:
			_publish_progress(
				78 + (idx / total) * 2.0,
				f"Asking the AI for fix suggestions ({idx + 1}/{total})…",
				context.session_uuid,
			)
		except Exception:
			pass
		try:
			ns = SimpleNamespace(
				finding_type=f.get("finding_type") or "",
				severity=f.get("severity") or "Low",
				title=f.get("title") or "",
				customer_description=f.get("customer_description") or "",
				estimated_impact_ms=f.get("estimated_impact_ms") or 0,
				affected_count=f.get("affected_count") or 0,
				action_ref=f.get("action_ref") or "",
				technical_detail_json=f.get("technical_detail_json") or "{}",
				llm_fix_json=None,
			)
			result = ai_fix.suggest_fix(_ai_payload_for_finding(
				ns, file_cache, phase2_index=phase2_index,
				recordings_by_uuid=recordings_by_uuid,
				actions_by_idx=actions_by_idx,
			))
			f["llm_fix_json"] = json.dumps(result, default=str)
		except Exception as exc:
			failures += 1
			try:
				frappe.log_error(title="optimus ai auto-suggest")
			except Exception:
				pass
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.ai_auto_suggest", exc,
					context={"finding_type": f.get("finding_type") or ""},
				)
			except Exception:
				pass

	if failures:
		context.warnings.append(
			f"AI auto-suggest: {failures} finding(s) couldn't get a suggestion "
			"(provider error / timeout — see error log)."
		)
	if skipped_for_time:
		context.warnings.append(
			f"AI auto-suggest: {skipped_for_time} finding(s) skipped — hit the "
			f"{AI_AUTO_SUGGEST_TIME_BUDGET_SECONDS}s budget for AI suggestions."
		)


def _ai_payload_for_finding(
	child,
	file_cache: dict,
	*,
	phase2_index: dict | None = None,
	recordings_by_uuid: dict | None = None,
	actions_by_idx: dict | None = None,
) -> dict:
	"""Build the dict ``ai_fix.suggest_fix`` expects from a finding-like
	object — a ``Optimus Finding`` child row, or a ``SimpleNamespace``
	shaped like one (``finding_type`` / ``severity`` / ``title`` /
	``customer_description`` / ``estimated_impact_ms`` / ``affected_count`` /
	``action_ref`` / ``technical_detail_json`` / ``llm_fix_json``). It's the
	renderer's normalized finding dict plus a wider source-code window around
	the callsite, plus — when a Phase-2 line-profile pass instrumented this
	finding's function — the hottest line from it (number / content / ms /
	hits). ``phase2_index`` is a
	``renderer._build_line_drilldown_callsite_index`` result,
	``{(basename, function): hotline}``.

	v0.6.x: when both ``recordings_by_uuid`` (``{recording_uuid: recording}``)
	and ``actions_by_idx`` (``{idx: action_dict}``) are provided AND the
	finding has an ``action_ref``, the top-N slowest queries from that
	action's recording are attached to ``technical_detail.example_queries``.
	This gives the AI **verbatim SQL evidence** for Slow-Hot-Path / N+1
	findings whose hot function ran raw SQL — without it the model has to
	infer the query shape from the Python source, which is the leading
	cause of nonsense substitutions (e.g. inventing ``filters={"name":
	("in", [some_var] * N)}`` to fit an unrelated example pattern).
	Already-set ``example_queries`` (e.g. from SQL red-flag analyzers) wins
	— this only fills the gap."""
	from optimus import ai_fix

	payload = renderer._finding_to_dict(child, file_cache=file_cache)
	callsite = (payload.get("technical_detail") or {}).get("callsite") or {}
	if callsite.get("filename") and callsite.get("lineno") is not None:
		try:
			window = renderer._read_source_window(
				callsite["filename"], callsite["lineno"],
				before=ai_fix._SOURCE_LINES_BEFORE, after=ai_fix._SOURCE_LINES_AFTER,
				cache=file_cache,
			)
		except Exception:
			window = None
		if window:
			payload["source_window"] = window

	fn = (callsite.get("function") or "").strip()
	fname = (callsite.get("filename") or "").strip()
	if phase2_index and fn and fname:
		base = fname.replace("\\", "/").rsplit("/", 1)[-1]
		hot = phase2_index.get((base, fn))
		if isinstance(hot, dict) and hot.get("lineno") is not None:
			payload["phase2_hotline"] = {
				"lineno": hot.get("lineno"),
				"content": hot.get("content") or "",
				"total_ms": hot.get("total_ms") or 0,
				"hits": hot.get("hits") or 0,
			}

	_maybe_attach_recorded_queries(
		payload,
		action_ref=getattr(child, "action_ref", None) or (child.get("action_ref") if isinstance(child, dict) else None),
		recordings_by_uuid=recordings_by_uuid,
		actions_by_idx=actions_by_idx,
	)
	return payload


_AI_EXAMPLE_QUERIES_MAX = 3
_AI_EXAMPLE_QUERY_MIN_MS = 0.5  # drop sub-half-ms queries (cache hits, etc.)


def _maybe_attach_recorded_queries(
	payload: dict,
	*,
	action_ref,
	recordings_by_uuid: dict | None,
	actions_by_idx: dict | None,
) -> None:
	"""When recordings + actions are available AND the finding has an
	``action_ref``, attach the top-N slowest SQL queries from that action's
	recording to ``payload.technical_detail.example_queries`` (best-effort).

	Skipped silently when any of the inputs is missing or the finding's
	technical_detail already carries example_queries (a SQL red-flag analyzer
	set them — those are the most relevant queries by definition; don't
	overwrite)."""
	if not recordings_by_uuid or not actions_by_idx or action_ref in (None, ""):
		return
	try:
		idx = int(action_ref)
	except (TypeError, ValueError):
		return
	action = actions_by_idx.get(idx)
	if not action:
		return
	recording_uuid = action.get("recording_uuid") if isinstance(action, dict) else getattr(action, "recording_uuid", None)
	if not recording_uuid:
		return
	recording = recordings_by_uuid.get(recording_uuid)
	if not recording:
		return
	calls = recording.get("calls") if isinstance(recording, dict) else None
	if not calls:
		return
	detail = payload.setdefault("technical_detail", {}) or {}
	if detail.get("example_queries"):
		# Analyzer (SQL red flag) set these; respect — they're the most relevant.
		return
	top = []
	for c in sorted(calls, key=lambda c: -(c.get("duration") or c.get("duration_ms") or 0)):
		dur = c.get("duration") or c.get("duration_ms") or 0
		if dur < _AI_EXAMPLE_QUERY_MIN_MS:
			continue
		q = (c.get("query") or "").strip()
		if not q:
			continue
		top.append(q)
		if len(top) >= _AI_EXAMPLE_QUERIES_MAX:
			break
	if top:
		detail["example_queries"] = top
		payload["technical_detail"] = detail


def _phase2_index_for(doc_or_docname) -> dict:
	"""``renderer._build_line_drilldown_callsite_index`` for a session
	doc / docname, or ``{}`` on any error (no phase-2 runs yet, doc
	gone, etc.).
	"""
	try:
		doc = doc_or_docname
		if isinstance(doc, str):
			doc = frappe.get_doc("Optimus Session", doc)
		return renderer._build_line_drilldown_callsite_index(doc) or {}
	except Exception:
		return {}


def _run_ai_backfill(doc, *, cap: int | None = None,
                     time_budget: float = AI_BACKFILL_TIME_BUDGET_SECONDS,
                     regenerate_all: bool = False) -> dict:
	"""Generate AI fix suggestions for eligible findings on a persisted
	Optimus Session ``doc``, persist them (``frappe.db.set_value`` + update
	the in-memory rows so a subsequent ``_render_and_attach_reports``
	re-fetch sees them), and report counts.

	By default this only touches eligible findings that DON'T have a
	suggestion yet — the "fill the gaps" case (``api.backfill_ai_fixes``,
	the auto-suggest backfill, the analyze pipeline). With
	``regenerate_all=True`` it (re)generates the suggestion for EVERY
	eligible finding, overwriting existing ones — the "re-evaluate the whole
	report" case (e.g. after changing the AI model/prompt). On a failure
	mid-re-eval the OLD suggestion is left in place (we only write on
	success), so there's no data loss.

	The CALLER decides whether to invoke this — the analyze pipeline / plain
	``regenerate_reports`` only do so when Optimus Settings has
	``ai_auto_suggest`` on (via ``_backfill_ai_suggestions``); the explicit
	"Generate AI fixes" / "Re-evaluate AI fixes" buttons call it whenever the
	provider is configured (via ``api.backfill_ai_fixes``). Requires
	``ai_fix.is_available()`` — returns all-zeros if not.

	``cap``: max findings to do this run. ``None`` → use Optimus Settings'
	``ai_auto_suggest_max``; ``0`` → no cap (do as many as fit in
	``time_budget``). Best-effort + time-budgeted (the callers run inside a
	web request, so this must stay well under the gunicorn worker timeout) —
	a provider error on one finding doesn't stop the rest.

	Returns ``{"added": int, "failed": int, "skipped_time": int,
	"total_pending": int}`` — ``total_pending`` is the number of findings
	this run targeted (before the cap): the missing ones, or — with
	``regenerate_all`` — all eligible ones.
	"""
	out = {"added": 0, "failed": 0, "skipped_time": 0, "total_pending": 0}

	from optimus import ai_fix

	if not ai_fix.is_available(section="findings"):
		return out

	rows = list(getattr(doc, "findings", None) or [])
	chosen = [
		r for r in rows
		if (getattr(r, "finding_type", "") or "") in ai_fix.AI_ELIGIBLE_FINDING_TYPES
		and (regenerate_all or not ((getattr(r, "llm_fix_json", None) or "").strip()))
	]
	out["total_pending"] = len(chosen)
	if not chosen:
		return out
	chosen.sort(key=lambda r: (
		SEVERITY_ORDER.get(getattr(r, "severity", None) or "Low", 3),
		-(getattr(r, "estimated_impact_ms", 0) or 0),
	))
	if cap is None:
		try:
			from optimus.settings import get_config
			cap = int(getattr(get_config(), "ai_auto_suggest_max", 0) or 0)
		except Exception:
			cap = 0
	if cap and cap > 0:
		chosen = chosen[:cap]

	file_cache: dict = {}
	phase2_index = _phase2_index_for(doc)
	started = time.monotonic()
	for idx, r in enumerate(chosen):
		if time.monotonic() - started > time_budget:
			out["skipped_time"] = len(chosen) - idx
			break
		try:
			result = ai_fix.suggest_fix(_ai_payload_for_finding(r, file_cache, phase2_index=phase2_index))
			blob = json.dumps(result, default=str)
			frappe.db.set_value("Optimus Finding", r.name, "llm_fix_json", blob)
			r.llm_fix_json = blob
			out["added"] += 1
		except Exception as exc:
			out["failed"] += 1
			try:
				frappe.log_error(title="optimus ai backfill")
			except Exception:
				pass
			try:
				from optimus import telemetry
				telemetry.emit_failure("analyze.ai_backfill", exc)
			except Exception:
				pass
	if out["added"]:
		try:
			safe_commit()
		except Exception:
			pass
	return out


def _backfill_ai_suggestions(doc) -> bool:
	"""Auto-suggest-gated AI backfill: run ``_run_ai_backfill`` only when
	Optimus Settings has ``ai_enabled`` AND ``ai_auto_suggest``. Used by
	``analyze.run`` (to retry any auto-suggested finding that errored before
	persistence) and by ``api.regenerate_reports`` (so flipping the
	"Suggest AI fixes by default" switch and re-rendering an existing
	session backfills it). Returns True if any suggestion was added.

	The explicit "Generate AI fixes" button bypasses this gate — it calls
	``_run_ai_backfill`` directly via ``api.backfill_ai_fixes``, so it works
	even when ``ai_auto_suggest`` is off.
	"""
	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return False
	if not (getattr(cfg, "ai_enabled", False) and getattr(cfg, "ai_suggest_findings", True)
	        and getattr(cfg, "ai_auto_suggest", False)):
		return False
	return _run_ai_backfill(doc)["added"] > 0


# ---------------------------------------------------------------------------
# v0.6.0: LLM-vetted per-table index recommendation. Auto (gated by the
# "Suggest AI fixes by default" toggle, for the top N tables that have a
# heuristic recommendation) and on-demand (the "Suggest an index (AI)" button
# → api.suggest_index). The result is stashed on the table's breakdown entry
# as ``ai_index = {suggestion, model, provider, generated_at}`` — the renderer
# turns the markdown into safe HTML. Network I/O lives here in the
# orchestrator / the API endpoint, never in an analyzer.
# ---------------------------------------------------------------------------


def _table_index_sample_queries(recordings: list[dict], table: str, limit: int = 4) -> list[str]:
	"""A few distinct normalized SELECT queries from the session that touched
	``table`` — best context for the LLM's index advice. Best-effort."""
	out: list[str] = []
	seen: set[str] = set()
	for recording in recordings or []:
		for call in recording.get("calls") or []:
			q = (call.get("normalized_query") or call.get("query") or "").strip()
			if not q or q in seen:
				continue
			try:
				meta = table_breakdown._parse_query(q)
			except Exception:
				continue
			if meta.get("verb") == "SELECT" and table in (meta.get("tables") or []):
				seen.add(q)
				out.append(q)
				if len(out) >= limit:
					return out
	return out


def _table_existing_indexes(table: str) -> list[dict]:
	"""``SHOW INDEX FROM `table`` → ``[{name, columns:[...by seq], unique}]``.
	Best-effort: returns ``[]`` if the table doesn't exist / on any error."""
	try:
		rows = frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True) or []
	except Exception:
		return []
	by_name: dict[str, dict] = {}
	for r in rows:
		name = r.get("Key_name")
		if not name:
			continue
		entry = by_name.setdefault(name, {"name": name, "_cols": [], "unique": not r.get("Non_unique")})
		entry["_cols"].append((int(r.get("Seq_in_index") or 0), r.get("Column_name")))
	out = []
	for e in by_name.values():
		cols = [c for _seq, c in sorted(e["_cols"]) if c]
		out.append({"name": e["name"], "columns": cols, "unique": bool(e["unique"])})
	return out


def _ai_payload_for_table(t_entry: dict, recordings: list[dict]) -> dict:
	"""Build the dict ``ai_fix.suggest_index`` expects from a breakdown entry."""
	table = t_entry.get("table") or ""
	rec = t_entry.get("recommended_index") or {}
	return {
		"table": table,
		"doctype": rec.get("doctype") or (table[3:] if table.lower().startswith("tab") else ""),
		"read_count": t_entry.get("read_count") or 0,
		"write_count": t_entry.get("write_count") or 0,
		"is_write_hot": bool(t_entry.get("is_write_hot")),
		"recommended_index": rec,
		"candidates": t_entry.get("index_candidates") or [],
		"framework_cols_filtered": t_entry.get("framework_cols_filtered") or [],
		"existing_indexes": _table_existing_indexes(table),
		"sample_queries": _table_index_sample_queries(recordings, table),
	}


def _enrich_table_breakdown_with_ai_suggestions(context, recordings: list[dict]) -> None:
	"""When Optimus Settings has ``ai_enabled`` AND ``ai_auto_suggest``, ask
	the LLM for an index recommendation on the top ``AI_AUTO_INDEX_MAX_TABLES``
	tables that have a heuristic ``recommended_index``, and stash it on the
	breakdown entry's ``ai_index``. Best-effort + time-budgeted — failures /
	a slow provider just mean fewer (or no) AI blocks, never a failed analyze."""
	breakdown = (context.aggregate or {}).get("table_breakdown") or []
	eligible = [t for t in breakdown if isinstance(t, dict) and t.get("recommended_index")]
	if not eligible:
		return
	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return
	if not (getattr(cfg, "ai_enabled", False) and getattr(cfg, "ai_suggest_indexes", True)
	        and getattr(cfg, "ai_auto_suggest", False)):
		return
	from optimus import ai_fix
	if not ai_fix.is_available(section="indexes"):
		return  # the findings auto-suggest step already warned about this

	eligible = eligible[:AI_AUTO_INDEX_MAX_TABLES]
	started = time.monotonic()
	total = len(eligible)
	for idx, t in enumerate(eligible):
		if time.monotonic() - started > AI_AUTO_INDEX_TIME_BUDGET_SECONDS:
			break
		try:
			_publish_progress(
				79 + (idx / total) * 1.0,
				f"Asking the AI to review index candidates ({idx + 1}/{total})…",
				context.session_uuid,
			)
		except Exception:
			pass
		try:
			t["ai_index"] = ai_fix.suggest_index(_ai_payload_for_table(t, recordings))
		except Exception as exc:
			try:
				frappe.log_error(title="optimus ai index-suggest")
			except Exception:
				pass
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"analyze.ai_index_suggest", exc,
					context={"table": (t.get("table") if isinstance(t, dict) else "") or ""},
				)
			except Exception:
				pass


def _run_table_index_ai_backfill(doc, *, table_name: str) -> dict:
	"""Generate (or regenerate) the LLM index recommendation for one table on
	a persisted Optimus Session ``doc`` and write it into
	``table_breakdown_json``. Ungated (the "Suggest an index (AI)" button asks
	for it explicitly) — but ``ai_fix.suggest_index`` still needs a configured
	provider. Returns ``{"ok": bool, "table": str, "reason"?: str}``; lets
	``ai_fix.AiFixError`` propagate (the API turns it into ``frappe.throw``)."""
	if not table_name:
		return {"ok": False, "reason": "no table specified"}
	from optimus import ai_fix
	if not ai_fix.is_available(section="indexes"):
		return {"ok": False, "table": table_name, "reason": "AI index recommendations not available"}
	try:
		breakdown = json.loads(doc.table_breakdown_json or "[]")
	except Exception:
		breakdown = []
	t_entry = next((t for t in breakdown if isinstance(t, dict) and t.get("table") == table_name), None)
	if t_entry is None:
		return {"ok": False, "table": table_name, "reason": "table not in the breakdown"}
	if not t_entry.get("recommended_index"):
		return {"ok": False, "table": table_name, "reason": "no index candidate for this table"}

	recording_uuids = [
		a.recording_uuid for a in (doc.actions or []) if getattr(a, "recording_uuid", None)
	]
	try:
		recordings = list(_fetch_recordings(recording_uuids))
	except Exception:
		recordings = []

	result = ai_fix.suggest_index(_ai_payload_for_table(t_entry, recordings))
	t_entry["ai_index"] = result
	frappe.db.set_value(
		"Optimus Session", doc.name, "table_breakdown_json",
		json.dumps(breakdown, default=str),
	)
	safe_commit()
	return {"ok": True, "table": table_name}


# v0.5.1: auto-generated "Steps to Reproduce" from captured actions. The
# dialog no longer asks the user to type notes at start time because (a) it
# added friction to the one-click "start profiling" flow, and (b) the user
# already performed the steps — the profiler captured them. We synthesize
# a bullet list from the recordings and write it to the `notes` field ONLY
# when the user hasn't already provided their own text via the DocType
# form. The developer can then edit the auto-generated list to add
# business context (what the user was *trying* to do, not what endpoint
# was hit) before sharing the report. The template still runs notes_html
# through sanitize_html(always_sanitize=True), so any HTML we emit is
# re-sanitized at render time — but we still escape labels here because
# the stored value is also what appears when someone edits the doc.
_AUTO_NOTES_MAX_ENTRIES = 50
_AUTO_NOTES_PREAMBLE = (
	"<p><em>Auto-generated from captured actions. Edit to add business "
	"context (what you were trying to accomplish, any steps taken before "
	"recording started, expected vs. actual behavior).</em></p>"
)

# v0.5.1: recordings whose cmd or path matches any of these is
# considered background/polling noise and excluded from the Steps to
# Reproduce list. These ARE still shown in the per-action table (the
# full picture), they just don't belong in a human-readable reproducer.
#
# Driven by a real user report whose reproducer read:
#
#   GET /api/method/frappe.realtime.has_permission — 25 ms
#   POST /api/method/frappe.desk.form.save.savedocs — 774.8 ms
#   GET /api/method/frappe.realtime.has_permission — 6 ms
#
# Of those three, only the savedocs is a user action. The two
# has_permission entries are the Desk polling for realtime
# subscription permissions and should be filtered.
_REPRODUCER_NOISE_CMD_PREFIXES = (
	# Desk polling / realtime subscription permission checks. Fire
	# 2-3x per second while the Desk has a doctype page open.
	"frappe.realtime.",
	# Form-metadata loading issued on every form open. Useful in the
	# per-action table for timing but clutters the reproducer —
	# "Load Sales Invoice form" says nothing about user intent.
	"frappe.desk.form.load.getdoctype",
	"frappe.desk.form.load.getdocinfo",
	# Background list counters.
	"frappe.client.get_count",
	# Frappe's internal doctype hooks endpoint
	"frappe.desk.notifications.get_open_count",
	# Build / reload assets
	"frappe.core.doctype.system_settings.system_settings.load",
)

_REPRODUCER_NOISE_PATH_PREFIXES = (
	# Static asset requests
	"/assets/",
	"/favicon",
	# Frappe's built-in recorder desk page (not a user action)
	"/app/recorder",
)


def _is_reproducer_noise(rec: dict) -> bool:
	"""Return True when a recording shouldn't appear in the auto-notes
	reproducer list. Still appears in the per-action breakdown — just
	excluded from the high-level human-readable flow."""
	cmd = (rec.get("cmd") or "").strip()
	if cmd:
		for prefix in _REPRODUCER_NOISE_CMD_PREFIXES:
			if cmd.startswith(prefix):
				return True
	path = (rec.get("path") or "").strip()
	if path:
		for prefix in _REPRODUCER_NOISE_PATH_PREFIXES:
			if path.startswith(prefix):
				return True
	return False


def _recordings_for_reproducer(recordings: list[dict]) -> list[dict]:
	"""The signal (non-noise) recordings, in order. Shared by the raw
	auto-notes list and the AI humanizer — see ``_is_reproducer_noise``."""
	return [r for r in (recordings or []) if not _is_reproducer_noise(r)]


def _build_auto_notes_list_html(recordings: list[dict]) -> str:
	"""The ordered-list body of the "Steps to Reproduce" note (no preamble) —
	``<ol><li><label> — <ms></li>…</ol>`` plus a "N background requests
	filtered" footer. Returns "" when there are no signal recordings.

	Labels come from ``per_action.humanized_label`` (English: "Create Sales
	Invoice", "Submit Delivery Note"); HTML-escaped before wrapping so a
	cmd/path with <, >, or & can't corrupt the markup.
	"""
	if not recordings:
		return ""
	signal_recordings = _recordings_for_reproducer(recordings)
	if not signal_recordings:
		return ""

	items: list[str] = []
	for rec in signal_recordings[:_AUTO_NOTES_MAX_ENTRIES]:
		label = per_action.humanized_label(rec) or "(unnamed action)"
		duration_ms = round(rec.get("duration") or 0, 1)
		items.append(f"<li>{html.escape(label)} — {duration_ms:g} ms</li>")

	overflow = len(signal_recordings) - _AUTO_NOTES_MAX_ENTRIES
	if overflow > 0:
		items.append(f"<li><em>… and {overflow} more action(s) not shown.</em></li>")

	noise_count = len(recordings) - len(signal_recordings)
	footer = ""
	if noise_count > 0:
		footer = (
			f"<p class='muted' style='color:#6b7280;font-size:0.85rem'>"
			f"{noise_count} background / polling request(s) filtered "
			"out (permission checks, form-metadata loads, static assets)."
			"</p>"
		)
	return "<ol>" + "".join(items) + "</ol>" + footer


def _build_auto_notes_html(recordings: list[dict]) -> str:
	"""Auto-generated "Steps to Reproduce" — preamble + the raw labelled
	action list. The fallback when AI humanizing is off or fails. Returns ""
	when there's nothing to list (no recordings, or all noise) so the caller
	leaves ``doc.notes`` in its default empty state."""
	body = _build_auto_notes_list_html(recordings)
	if not body:
		return ""
	return _AUTO_NOTES_PREAMBLE + body


# v0.6.0: LLM-humanized "Steps to Reproduce" — just the friendly narrative.
# (The raw labelled action list isn't appended; the per-action breakdown in
# the report already shows every action with its technical label + timing.)
_HUMANIZED_NOTES_PREAMBLE = (
	"<p><em>Steps to Reproduce — drafted by AI from the captured actions. "
	"Edit to add business context (what you were trying to accomplish, any "
	"steps taken before recording started, expected vs. actual behavior).</em></p>"
)


def _actions_for_humanizer(recordings: list[dict]) -> list[dict]:
	"""Compact per-action dicts (label / cmd / path / method / doctype /
	duration_ms) for ``ai_fix.humanize_steps`` — noise-filtered and capped
	the same way the raw auto-notes list is."""
	out: list[dict] = []
	for rec in _recordings_for_reproducer(recordings)[:_AUTO_NOTES_MAX_ENTRIES]:
		fd = rec.get("form_dict") or {}
		doctype = ""
		if isinstance(fd, dict):
			doctype = (fd.get("doctype") or fd.get("dt") or fd.get("doc_type") or "").strip()
			if not doctype:
				# savedocs embeds the doctype in a `doc` JSON blob; client.*
				# uses `doc` / `dt`. Reuse per_action's extractors.
				try:
					doctype = (per_action._extract_doc_info(fd)[0] or "").strip()
				except Exception:
					doctype = ""
				if not doctype:
					try:
						doctype = (per_action._extract_doctype(fd) or "").strip()
					except Exception:
						doctype = ""
		out.append({
			"label": per_action.humanized_label(rec) or "",
			"cmd": (rec.get("cmd") or "").strip(),
			"path": (rec.get("path") or "").strip(),
			"method": (rec.get("method") or "").strip(),
			"doctype": doctype,
			"duration_ms": round(rec.get("duration") or 0, 1),
		})
	return out


def _assemble_humanized_notes(steps_markdown: str) -> str:
	"""The HTML stored in ``doc.notes`` for an AI-humanized "Steps to
	Reproduce": the preamble + the LLM's Markdown steps, rendered + sanitized.
	No raw captured-actions appendix — the per-action breakdown in the report
	already lists every action with its technical label and timing."""
	return _HUMANIZED_NOTES_PREAMBLE + renderer._markdown_to_safe_html(steps_markdown)


def _build_humanized_notes_html(
	recordings: list[dict], *, session_title: str | None = None
) -> str:
	"""LLM-humanized "Steps to Reproduce" HTML, or "" when AI isn't
	enabled/available, there's nothing to summarise, or the LLM call fails
	(the caller then falls back to ``_build_auto_notes_html``). Best-effort —
	never raises."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return ""
	if not (getattr(cfg, "ai_enabled", False) and getattr(cfg, "ai_humanize_steps", True)):
		return ""
	from optimus import ai_fix
	if not ai_fix.is_available(section="humanize"):
		return ""
	actions = _actions_for_humanizer(recordings)
	if not actions:
		return ""
	try:
		steps_md = ai_fix.humanize_steps(actions, session_title=session_title)
	except Exception as exc:
		try:
			frappe.log_error(title="optimus humanize_steps")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure("analyze.humanize_steps", exc)
		except Exception:
			pass
		return ""
	if not (steps_md or "").strip():
		return ""
	return _assemble_humanized_notes(steps_md)


def _compute_top_severity(findings: list[dict]) -> str:
	"""Return the highest severity present in the findings list.

	Populated on each session so the list view can show a color-coded
	"Top Severity" column without loading the child rows.
	"""
	if not findings:
		return "None"
	severities = {f.get("severity") for f in findings}
	for level in ("High", "Medium", "Low"):
		if level in severities:
			return level
	return "None"


_PRIORITY_WORD = {"High": "high", "Medium": "medium", "Low": "low"}


def _humanize_action_label(action: dict, recordings: list[dict]) -> str:
	"""Plain-English label for an action ("Submit Sales Invoice" rather than
	"frappe.desk.form.save.savedocs:Submit"). Looks up the recording by uuid
	and runs it through ``per_action.humanized_label``; falls back to the raw
	``action_label`` when the recording isn't to hand (TTL'd out, etc.)."""
	raw = str(action.get("action_label") or "?")
	uid = action.get("recording_uuid")
	if uid:
		rec = next((r for r in recordings if r.get("uuid") == uid), None)
		if rec:
			try:
				h = per_action.humanized_label(rec)
				if h and h not in ("?", ""):
					return h
			except Exception:
				pass
	return raw


def _build_summary_html(
	context: AnalyzeContext, total_queries: int, recordings: list[dict] | None = None
) -> str:
	"""Plain-language customer summary, generated from the analyzer findings.

	Written for a non-developer: "operations" not "actions", humanized action
	names not raw cmds, "high priority" not "high-severity", and a finding's
	raw ``cmd:action`` reference swapped for the humanized form.
	"""
	recordings = recordings or []
	n_actions = len(context.actions)
	findings = context.findings
	high = sum(1 for f in findings if f.get("severity") == "High")
	medium = sum(1 for f in findings if f.get("severity") == "Medium")
	low = sum(1 for f in findings if f.get("severity") == "Low")

	# v0.7.x: emit each summary point as a bullet (joined into one <ul>
	# at the end of the function). Each ``parts`` entry is the bullet's
	# inner HTML (no <li> wrapper here — added in the final join).
	parts = [
		f"This session covered <strong>{n_actions} operation"
		f"{'s' if n_actions != 1 else ''}</strong> (page loads, saves and "
		f"RQ Jobs) with <strong>{total_queries} database "
		f"quer{'ies' if total_queries != 1 else 'y'}</strong>."
	]

	if context.actions:
		slowest_idx = max(
			range(len(context.actions)),
			key=lambda i: context.actions[i].get("duration_ms", 0),
		)
		slowest = context.actions[slowest_idx]
		slowest_ms = slowest.get("duration_ms", 0)
		slowest_label = _humanize_action_label(slowest, recordings)
		slowest_label_esc = html.escape(slowest_label)

		# raw cmd / "Job: x" label  ->  plain-English label, for every action,
		# so finding titles that reference an action read cleanly too.
		label_map = {
			str(a.get("action_label") or ""): _humanize_action_label(a, recordings)
			for a in context.actions if a.get("action_label")
		}

		def _finding_phrase(f: dict) -> str:
			"""Finding title with any raw action references swapped for plain
			labels (and a leading "In <slowest>, " trimmed since the sentence
			already names it), + a plain-language impact/priority parenthetical."""
			title = (f.get("title") or "").strip()
			for raw, human in label_map.items():
				if raw and human and raw != human:
					title = title.replace(raw, human)
			prefix = f"In {slowest_label}, "
			if title.startswith(prefix):
				title = title[len(prefix):]
			pri = _PRIORITY_WORD.get(f.get("severity") or "", "")
			impact = f.get("estimated_impact_ms") or 0
			tail = f" (~{impact:.0f}ms" + (f" - {pri} priority" if pri else "") + ")"
			return f"<strong>{html.escape(title)}</strong>{tail}"

		# Prefer a finding tied to this specific action (via action_ref);
		# else the highest-impact finding overall (session-wide ones like a
		# missing index don't attribute to one action).
		tied_finding = None
		for f in findings:
			ref = f.get("action_ref")
			if ref and str(ref) == str(slowest_idx):
				if not tied_finding or (f.get("estimated_impact_ms") or 0) > (
					tied_finding.get("estimated_impact_ms") or 0
				):
					tied_finding = f
		overall_finding = max(
			findings, key=lambda f: f.get("estimated_impact_ms") or 0
		) if findings else None

		if tied_finding:
			# v0.7.x: the "See the Findings section below …" pointer lives once,
			# on the issue-count sentence below — don't repeat it here.
			parts.append(
				f"The slowest one was <strong>{slowest_label_esc}</strong> at "
				f"{slowest_ms:.0f}ms - and most of its time went into "
				f"{_finding_phrase(tied_finding)}."
			)
		elif overall_finding:
			parts.append(
				f"The slowest one was <strong>{slowest_label_esc}</strong> at "
				f"{slowest_ms:.0f}ms. The biggest issue this session "
				f"(it affects several operations) was {_finding_phrase(overall_finding)}."
			)
		else:
			parts.append(
				f"The slowest one was <strong>{slowest_label_esc}</strong> at "
				f"{slowest_ms:.0f}ms."
			)

	if not findings:
		# Two-bullet reassurance (was one block of two <p>s pre-v0.7.x).
		parts.append(
			"We checked your flow for the usual culprits - "
			"<strong>repeated queries (N+1 patterns)</strong>, "
			"<strong>full table scans</strong>, "
			"<strong>filesort operations</strong>, "
			"<strong>temporary table creation</strong>, "
			"<strong>low filter ratios</strong>, "
			"<strong>missing indexes</strong>, and "
			"<strong>individually slow queries</strong> (&gt;200ms) - "
			"and nothing significant turned up."
		)
		parts.append(
			"Either your flow is already well-optimized, or the data "
			"volume is too small to surface bottlenecks at this scale. "
			"Try running the profiler again with a larger dataset for "
			"more insight."
		)
	else:
		total_issues = high + medium + low
		bits = []
		if high:
			bits.append(f"<strong>{high} high priority</strong>")
		if medium:
			bits.append(f"{medium} medium")
		if low:
			bits.append(f"{low} minor")
		breakdown = (" - " + ", ".join(bits)) if bits else ""
		parts.append(
			f"We found <strong>{total_issues} potential issue"
			f"{'s' if total_issues != 1 else ''}</strong>{breakdown}. See the "
			"Findings section below for the ones to ask your developer to fix first."
		)

	# Wrap into a single <ul> — each parts entry becomes one <li>. Inline
	# style mirrors the How-to-read list's pattern for visual consistency
	# with the rest of the report.
	bullets = "\n".join(f"<li>{p}</li>" for p in parts)
	return (
		'<ul class="small" style="margin: 4px 0 0 18px; padding: 0; '
		'line-height: 1.7;">\n' + bullets + "\n</ul>"
	)


def _finalize_with_empty_session(docname: str) -> None:
	"""Mark a session Ready when it had no recordings."""
	doc = frappe.get_doc("Optimus Session", docname)
	doc.status = "Ready"
	doc.total_requests = 0
	doc.total_queries = 0
	# v0.7.x: same bullet shape as the populated Summary for visual
	# consistency — a single <li> inside <ul> reads as a deliberate
	# summary line rather than dangling prose.
	doc.summary_html = (
		'<ul class="small" style="margin: 4px 0 0 18px; padding: 0; '
		'line-height: 1.7;">\n'
		"<li>No traffic was recorded during this session. Either no requests "
		"were made, or the session was stopped before any flow was performed.</li>\n"
		"</ul>"
	)
	doc.save(ignore_permissions=True)
	safe_commit()


def _render_and_attach_reports(docname: str, recordings: list[dict]) -> None:
	"""Render the HTML report and attach it to the DocType.

	Stored as a PRIVATE attachment on the Optimus Session. Frappe
	enforces "user must have read permission on attached_to_doctype"
	for private files — combined with the ``if_owner=1`` permission
	rule on Optimus Session for the Optimus User role and the
	additional gate in ``permissions.file_has_permission``, non-admin
	users can only download reports for their own sessions.
	"""
	# Re-fetch the doc so child rows persisted by _persist are visible.
	doc = frappe.get_doc("Optimus Session", docname)

	# v0.6.0 Round 7: safe-mode reporting removed. Single admin-scoped
	# raw report only — see product_thesis_self_hosted.md memory for
	# the rationale (PII redaction was a moat the user opted to drop in
	# favor of single-rendering-path simplicity).
	try:
		raw_html = renderer.render_raw(doc, recordings)
		raw_url = _save_report_file(
			docname=docname,
			filename=f"optimus_raw_report_{doc.session_uuid}.html",
			attached_to_field="raw_report_file",
			content=raw_html,
		)
		if raw_url:
			frappe.db.set_value("Optimus Session", docname, "raw_report_file", raw_url)
	except Exception as exc:
		frappe.log_error(title="optimus render raw report")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.render_raw_report", exc,
				context={"session_uuid": getattr(doc, "session_uuid", "") or "", "docname": docname or ""},
			)
		except Exception:
			pass

	safe_commit()


def _save_report_file(*, docname: str, filename: str, attached_to_field: str, content: str) -> str | None:
	"""Insert a private File attached to the Optimus Session.

	Returns the file_url for the new file, or None on failure.

	v0.5.2: wrapped in a narrow no-request context so Frappe's
	``File.validate_file_extension`` uses its designed bypass for
	code-generated files. The validator explicitly skips when
	``frappe.request`` is falsy (intent comment in frappe source:
	"Only validate uploaded files, not generated by code/
	integrations."). That bypass works correctly when analyze
	runs as a background RQ job (no request). But when the site
	has the scheduler disabled, analyze runs INLINE inside
	api.stop()'s HTTP handler — frappe.request is set, the
	bypass doesn't fire, and File's before_insert throws
	FileTypeNotAllowed when the site's allowed_file_extensions
	list (System Settings → File Settings) doesn't include HTML.

	Our report IS a code-generated file, not a user upload. The
	no-request bypass is exactly the intended path. We temporarily
	clear frappe.local.request around the insert to trigger it,
	then restore the original value in a finally so downstream
	request-handling code (e.g. response building in the caller)
	sees the real request object unchanged.
	"""
	try:
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"attached_to_doctype": "Optimus Session",
				"attached_to_name": docname,
				"attached_to_field": attached_to_field,
				"content": content.encode("utf-8"),
				"is_private": 1,
			}
		)
		saved_request = getattr(frappe.local, "request", None)
		try:
			# Temporarily stash the request so File's
			# validate_file_extension hits its no-request bypass.
			# Narrow window — only the insert() call, which doesn't
			# touch request-scoped state.
			try:
				frappe.local.request = None
			except Exception:
				# frappe.local might be a werkzeug Local proxy on some
				# versions; setting via attribute assignment works but
				# guard defensively.
				pass
			file_doc.insert(ignore_permissions=True)
		finally:
			# Restore unconditionally. A failed insert STILL needs the
			# original request object back so the caller's response-
			# building code isn't broken.
			try:
				frappe.local.request = saved_request
			except Exception:
				pass
		return file_doc.file_url
	except Exception as exc:
		frappe.log_error(title=f"optimus save_report_file {filename}")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.save_report_file", exc,
				context={"filename": filename or "", "docname": docname or ""},
			)
		except Exception:
			pass
		return None


def _cleanup_redis(session_uuid: str, recording_uuids: list[str]) -> None:
	"""Delete Redis state for this finalized session.

	The Optimus Session DocType row is now the durable record. Redis is
	freed so subsequent sessions can use it. Best-effort: a failure here
	does not abort the analyze.
	"""
	try:
		session.delete_session_state(session_uuid)
	except Exception as exc:
		frappe.log_error(title="optimus cleanup session_state")
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"analyze.cleanup_session_state", exc,
				context={"session_uuid": session_uuid or ""},
			)
		except Exception:
			pass

	for uuid in recording_uuids:
		try:
			frappe.cache.hdel(RECORDER_REQUEST_HASH, uuid)
		except Exception:
			pass
		try:
			frappe.cache.hdel(RECORDER_REQUEST_SPARSE_HASH, uuid)
		except Exception:
			pass
		# v0.3.0: also delete the per-recording tree and sidecar keys.
		try:
			frappe.cache.delete_value(_redis_keys.tree(uuid))
		except Exception:
			pass
		try:
			frappe.cache.delete_value(_redis_keys.sidecar(uuid))
		except Exception:
			pass
