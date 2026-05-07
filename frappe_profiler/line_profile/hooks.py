# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Phase-2 hook callbacks.

Registered in ``frappe_profiler/hooks.py`` alongside the phase-1 callbacks
in ``hooks_callbacks.py``. Phase-1 and phase-2 are mutually exclusive for
a single user — they read separate Redis flags
(``profiler:active:<user>`` vs ``profiler:lp:active:<user>``) and the API
layer rejects starting one while the other is active.

Each request / job that runs while phase-2 is active for the current user
gets:

  • a fresh ``LineProfiler`` with the run's picked functions attached
  • ``enable_by_count()`` before the request body runs
  • ``disable()`` + per-line stats RPUSH'd to Redis after the body returns

No SQL recording, no pyinstrument, no sidecar wraps. Phase 2 captures
only line-level timings on the picked functions.
"""

import frappe

from frappe_profiler import hooks_callbacks
from frappe_profiler.line_profile import capture


# ---------------------------------------------------------------------------
# Request hooks
# ---------------------------------------------------------------------------


def before_request_line_profile(*args, **kwargs) -> None:
	"""If phase-2 is active for this user, build a per-request LineProfiler
	and enable it. Returns silently otherwise.

	Best-effort: any exception is swallowed and logged so the host request
	is never broken by profiler instrumentation.
	"""
	try:
		user = frappe.session.user
		run_uuid = capture.is_active(user)
		if not run_uuid:
			return

		# Skip the profiler's own endpoints — same logic phase-1 uses to
		# avoid recording its own admin API calls.
		if hooks_callbacks._should_skip_request():
			return

		profiler = capture.make_profiler(run_uuid)
		if profiler is None:
			return

		profiler.enable_by_count()
		frappe.local._lp_profiler = profiler
		frappe.local._lp_run_uuid = run_uuid
	except Exception as exc:
		frappe.log_error(
			title="phase 2 before_request failed",
			message=f"{type(exc).__name__}: {exc}",
		)


def after_request_line_profile(*args, **kwargs) -> None:
	"""Disable the per-request profiler, serialize per-line stats, and
	push the batch to Redis. Cleared even if profiler was never enabled,
	to keep frappe.local clean."""
	profiler = getattr(frappe.local, "_lp_profiler", None)
	run_uuid = getattr(frappe.local, "_lp_run_uuid", None)
	# Always clear locals before doing I/O so a Redis hiccup doesn't leave
	# stale state on a recycled gunicorn worker.
	frappe.local._lp_profiler = None
	frappe.local._lp_run_uuid = None
	frappe.local._lp_active = None  # invalidate the per-request is_active cache

	if profiler is None or not run_uuid:
		return

	try:
		profiler.disable()
		samples = capture.serialize_stats(profiler)
		capture.flush_samples(run_uuid, samples)
	except Exception as exc:
		frappe.log_error(
			title="phase 2 after_request failed",
			message=f"{type(exc).__name__}: {exc}",
		)


# ---------------------------------------------------------------------------
# Background job hooks (mirror request hooks; gated by _lp_session_id kwarg)
# ---------------------------------------------------------------------------


def before_job_line_profile(*args, **kwargs) -> None:
	"""Phase-2 equivalent of ``hooks_callbacks.before_job``. Reads
	``_lp_session_id`` injected by the extended enqueue patch (see
	``frappe_profiler/__init__.py:_patch_enqueue``). When present and the
	user's flag still points to that run, attach line_profiler.
	"""
	try:
		# The enqueue patch injects _lp_session_id into job kwargs at
		# enqueue time so the job knows whether to record. Pop it before
		# the job's own function runs so we don't disturb its signature.
		job = kwargs.get("job") or (args[0] if args else None)
		if job is None:
			return
		job_kwargs = getattr(job, "kwargs", None) or {}
		run_uuid = job_kwargs.pop("_lp_session_id", None)
		if not run_uuid:
			return

		# Confirm the run is still active (user may have stopped it before
		# the job dequeued).
		user = frappe.session.user
		if capture.is_active(user) != run_uuid:
			return

		profiler = capture.make_profiler(run_uuid)
		if profiler is None:
			return

		profiler.enable_by_count()
		frappe.local._lp_profiler = profiler
		frappe.local._lp_run_uuid = run_uuid
	except Exception as exc:
		frappe.log_error(
			title="phase 2 before_job failed",
			message=f"{type(exc).__name__}: {exc}",
		)


def after_job_line_profile(*args, **kwargs) -> None:
	"""Phase-2 equivalent of ``hooks_callbacks.after_job``. Same as
	``after_request_line_profile`` but called from the job lifecycle."""
	profiler = getattr(frappe.local, "_lp_profiler", None)
	run_uuid = getattr(frappe.local, "_lp_run_uuid", None)
	frappe.local._lp_profiler = None
	frappe.local._lp_run_uuid = None
	frappe.local._lp_active = None

	if profiler is None or not run_uuid:
		return

	try:
		profiler.disable()
		samples = capture.serialize_stats(profiler)
		capture.flush_samples(run_uuid, samples)
	except Exception as exc:
		frappe.log_error(
			title="phase 2 after_job failed",
			message=f"{type(exc).__name__}: {exc}",
		)
