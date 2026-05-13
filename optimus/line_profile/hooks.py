# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 hook callbacks.

Registered in ``optimus/hooks.py`` alongside the phase-1 callbacks
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

from optimus import hooks_callbacks
from optimus.line_profile import capture

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


def before_job_line_profile(method=None, kwargs=None, **rest) -> None:
	"""Phase-2 equivalent of ``hooks_callbacks.before_job``. Reads
	``_lp_session_id`` injected by the extended enqueue patch (see
	``optimus/__init__.py:_patch_enqueue``).

	**Critical**: ``_lp_session_id`` is popped from the job's kwargs
	dict *unconditionally*, even if we end up not instrumenting (run
	already stopped, line_profiler unavailable, user is Guest, etc.).
	The kwargs dict is the same one Frappe's ``execute_job`` will splat
	into the user's method via ``method(**kwargs)`` — leaving our
	marker in there crashes the method with an unexpected-keyword-
	argument error.

	Hook signature mirrors phase-1's ``hooks_callbacks.before_job`` so
	Frappe's hook dispatcher passes ``method`` + ``kwargs`` as named
	parameters.
	"""
	# Always pop our marker first — before any control-flow that might
	# return early. The mutation propagates because ``kwargs`` is a
	# reference to the dict execute_job will use.
	if isinstance(kwargs, dict):
		run_uuid = kwargs.pop("_lp_session_id", None)
	else:
		run_uuid = None

	if not run_uuid:
		return

	try:
		user = getattr(frappe.session, "user", None)
		if not user or user == "Guest":
			return

		# Confirm the run is still active (user may have stopped it
		# between enqueue and the worker picking up the job).
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


def after_job_line_profile(method=None, kwargs=None, result=None, **rest) -> None:
	"""Phase-2 equivalent of ``hooks_callbacks.after_job``. Same as
	``after_request_line_profile`` but called from the job lifecycle.
	Signature mirrors phase-1's ``hooks_callbacks.after_job``.
	"""
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
