__version__ = "0.7.0"


def safe_commit() -> None:
	"""Commit pending changes with an explicit rollback-on-error guard.

	Frappe's ``frappe.db.commit()`` does NOT auto-rollback when the SQL
	COMMIT itself fails (rare but possible — replica lag, write timeout,
	deadlock retry exhausted). Without an explicit rollback, the
	connection is left in a tainted state that breaks the next statement
	with a confusing error far from the original cause. This helper is
	the Frappe-idiomatic guard the Lens audit recommends — wraps the
	commit, rolls back on exception, re-raises so the caller sees the
	failure.

	For best-effort callers (the janitor sweeps, etc.) the outer
	exception handler in the entry function absorbs the re-raise and
	moves on — no behavioural change. For must-succeed callers (analyze
	pipeline, install hook, PDF attachment), the exception now properly
	surfaces and the connection stays clean.
	"""
	import frappe
	try:
		frappe.db.commit()
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			# Rollback failing on top of commit failing is exceptionally
			# rare — typically the connection is already gone. Swallow
			# the rollback exception so the original (more informative)
			# commit exception is what bubbles up.
			pass
		raise


# ---------------------------------------------------------------------------
# frappe.enqueue monkey-patch (Phase 2)
# ---------------------------------------------------------------------------
# We wrap frappe.utils.background_jobs.enqueue so that when a user with an
# active profiler session enqueues a background job, the session UUID rides
# along inside the job's kwargs as `_profiler_session_id`. The before_job
# hook in hooks_callbacks.py reads this key (and pops it before the method
# runs, so the method's signature isn't disturbed) and activates recording
# for the job.
#
# This is the only way to make background-job profiling work without forking
# frappe — the worker process is a fresh interpreter that has no idea who
# enqueued the job, but RQ preserves kwargs verbatim across the queue boundary.
#
# Patched at app-import time. Idempotent via the `_profiler_patched` marker
# so re-imports during dev (e.g. `bench update`) don't double-wrap.
# ---------------------------------------------------------------------------


def _patch_enqueue():
	"""Install the enqueue wrapper. Safe to call in environments without
	frappe installed — will silently no-op (useful for running analyzer
	unit tests from a plain Python interpreter)."""
	try:
		import frappe
		import frappe.utils.background_jobs as _bg
	except ImportError:
		# Frappe isn't available — we're probably running unit tests
		# or a standalone script. Nothing to patch.
		return

	if getattr(_bg.enqueue, "_profiler_patched", False):
		return

	_original_enqueue = _bg.enqueue

	def _profiler_enqueue(method, *args, **kwargs):
		active = None
		try:
			# Lazy import to avoid circular dependency on app init.
			from optimus import session as _profiler_session

			user = getattr(frappe.session, "user", None) if hasattr(frappe, "session") else None
			if user and user != "Guest":
				active = _profiler_session.get_active_session_for(user)
				if active:
					# Inject our marker into the job kwargs. The before_job
					# hook will pop it before the method runs.
					kwargs["_profiler_session_id"] = active

				# Phase-2: independently propagate the line-profile run
				# UUID. A user can only have phase-1 OR phase-2 active at
				# any time (enforced at the API), but the two flags are
				# read-decoupled here so neither layer needs to know about
				# the other.
				try:
					from optimus.line_profile import capture as _lp_capture

					lp_active = _lp_capture.is_active(user)
					if lp_active:
						kwargs["_lp_session_id"] = lp_active
				except Exception:
					# line_profiler not installed, or any other failure —
					# phase 2 stays off for this job; phase 1 (if active)
					# still rides along.
					pass
		except Exception:
			# Never break enqueue. The profiler is best-effort by design.
			pass

		job = _original_enqueue(method, *args, **kwargs)

		# v0.6.0: register the RQ job id with the session so analyze waits
		# for it to finish before gathering recordings — so jobs that get
		# picked up by a worker shortly after Stop aren't lost. `job` is
		# None for `now=True` inline jobs (nothing async to wait for). Never
		# track our own analyze job (it would deadlock the wait on itself).
		try:
			if (
				active
				and job is not None
				and getattr(job, "id", None)
				and method != "optimus.analyze.run"
			):
				from optimus import session as _profiler_session

				_profiler_session.register_pending_job(active, job.id)
		except Exception:
			pass

		return job

	_profiler_enqueue._profiler_patched = True
	_profiler_enqueue.__wrapped__ = _original_enqueue

	# Patch BOTH locations: the canonical module attribute AND the
	# frappe.enqueue re-export at frappe/__init__.py:1590. They reference
	# the same function but Python module imports create separate bindings.
	_bg.enqueue = _profiler_enqueue
	frappe.enqueue = _profiler_enqueue


_patch_enqueue()


# v0.3.0: install sidecar wraps for redundant-call detection.
# Idempotent — safe to call multiple times. Wraps are activation-gated
# at call time so they're no-ops for non-recording users.
#
# Both layers are wrapped in try/except: the install itself, AND the
# logging fallback. In test contexts that stub `frappe` with a minimal
# fake module (e.g. test_enqueue_patch.py), `install_wraps()` may raise
# because `frappe.permissions` / `frappe.utils.redis_wrapper` aren't
# present, AND `frappe.log_error` may not exist either. Both failures
# are silent — the v0.2.0 enqueue patch above uses the same defensive
# pattern (`pass` on any exception) for the same reason.
#
# v0.5.3: we ONLY install at module-import if frappe is already
# fully loaded (i.e. `frappe._` — the translation function — is
# available). Otherwise the install is deferred. This guards against
# the bench test runner importing optimus during its own
# bootstrap, while `frappe/__init__.py` is still executing — in that
# state a later call to `frappe.get_doc(...)` through our wrap hits
# `frappe.utils.nestedset` which does `from frappe import _` at
# module-top and blows up with
# ``ImportError: cannot import name '_' from 'frappe'``. Deferring
# means the wraps install on first hook invocation (before_request /
# before_job) via the installer in ``hooks_callbacks``, by which
# time frappe is fully initialized.


def _try_install_capture_wraps() -> bool:
	"""Attempt to install the sidecar wraps. Returns True if actually
	installed, False if deferred or errored. Idempotent — the
	capture module itself guards against double-wrap.
	"""
	try:
		import frappe
	except ImportError:
		# No frappe at all (unit-test Python interpreter). No-op.
		return False

	# Frappe bootstrap in progress? The `_` translator is the last
	# thing frappe/__init__.py defines that nestedset imports —
	# if it's missing, wrap-install could cascade into a partial-
	# init ImportError. Defer until hooks fire.
	if not hasattr(frappe, "_"):
		return False

	try:
		from optimus import capture

		capture.install_wraps()
		return True
	except Exception:
		try:
			frappe.log_error(title="optimus capture.install_wraps")
		except Exception:
			pass  # never let a logging failure break app load
		return False


# Best-effort: install now if frappe is ready; otherwise the
# before_request / before_job hooks will trigger the deferred install
# on first request.
_try_install_capture_wraps()
