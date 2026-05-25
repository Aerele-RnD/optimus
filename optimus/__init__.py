__version__ = "0.12.7"


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
				# v0.7.x: also record the job's identity in the never-pruned
				# jobs hash so analyze can report its terminal status (incl.
				# failed / timed-out jobs that produce no recording).
				_method_str = method if isinstance(method, str) else getattr(
					method, "__name__", str(method)
				)
				_profiler_session.record_job(active, job.id, _method_str)
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


# ---------------------------------------------------------------------------
# frappe.recorder monkey-patch — capture-time redaction (v0.7.x+)
# ---------------------------------------------------------------------------
# The Frappe recorder snapshots ``frappe.local.form_dict`` + ``request.headers``
# at ``Recorder.__init__`` and stores each ``frappe.db.sql`` call's
# post-substitution query string at ``Recorder.register``. Both writes land
# in Redis (``RECORDER_REQUEST_HASH``) verbatim, then get persisted into the
# ``Optimus Session`` DocType's JSON blobs at analyze. Customers who export
# the report or back up their DB have, in effect, exfiltrated raw passwords /
# tokens / cookies.
#
# Pre-v0.7.x+ the renderer ran ``_redact_sensitive`` / ``_redact_sql_literals``
# at render time as a last line of defense. That left a window where raw
# values existed in Redis + on disk. The architecture review (Critical Risk
# #1) called this out; this patch closes it by redacting at the earliest
# point we control — when the Recorder captures.
#
# Patch surface:
#   - ``Recorder.__init__`` — after the original sets ``self.form_dict`` /
#     ``self.headers``, walk them via ``redaction.redact_sensitive`` so the
#     values reach ``dump()`` already scrubbed.
#   - ``Recorder.register(data)`` — before ``_original_register(data)``,
#     replace ``data["query"]`` with its redacted form so the SQL string
#     stored in ``self.calls`` never carried the original literal.
#
# Idempotent via the ``_profiler_patched`` marker (same pattern as
# ``_patch_enqueue``). Wrapped in ``try/except`` at every layer so a patch
# failure never breaks a customer's request — degrades gracefully back to
# the renderer's defense-in-depth redaction.


def _patch_recorder():
	"""Install capture-time redaction on Frappe's recorder. Mirrors
	``_patch_enqueue``: monkey-patch at app-import, idempotent via the
	``_profiler_patched`` marker, best-effort ``try/except`` so a patch
	failure never breaks the request.

	Settings are read INSIDE each wrap (not at install time) so changes to
	``sensitive_sql_columns`` / ``sensitive_form_keys`` take effect on the
	next request without a bench restart.
	"""
	try:
		import frappe.recorder as _rec
	except ImportError:
		# Frappe isn't available — running unit tests in a plain Python
		# interpreter. Match _patch_enqueue's silent no-op.
		return

	Recorder = getattr(_rec, "Recorder", None)
	if Recorder is None:
		return  # frappe shape changed; degrade gracefully

	# Defensive: skip if either method is missing (future Frappe version).
	if not hasattr(Recorder, "register") or not hasattr(Recorder, "__init__"):
		return
	if getattr(Recorder.register, "_profiler_patched", False):
		return  # already patched (idempotent re-import)

	_original_init = Recorder.__init__
	_original_register = Recorder.register

	def _read_extras():
		"""Read the live sensitive-list settings. Best-effort — falls back
		to empty extras (defaults still apply) if settings can't be loaded
		(early-boot, no DB connection, etc.)."""
		try:
			from optimus.settings import get_config

			cfg = get_config()
			return (
				tuple(cfg.sensitive_form_keys or ()),
				tuple(cfg.sensitive_sql_columns or ()),
			)
		except Exception:
			return ((), ())

	def _profiler_init(self, *args, **kwargs):
		_original_init(self, *args, **kwargs)
		try:
			from optimus.redaction import redact_sensitive

			extra_keys, _ = _read_extras()
			if isinstance(getattr(self, "form_dict", None), dict):
				self.form_dict = redact_sensitive(self.form_dict, extra_keys=extra_keys)
			if isinstance(getattr(self, "headers", None), dict):
				self.headers = redact_sensitive(self.headers, extra_keys=extra_keys)
		except Exception:
			# Never break the user's request because OUR scrubber failed.
			# Renderer-side defense-in-depth still catches anything that
			# slipped through.
			pass

	def _profiler_register(self, data):
		try:
			from optimus.redaction import redact_sql_literals

			_, extra_cols = _read_extras()
			if isinstance(data, dict) and isinstance(data.get("query"), str):
				data["query"] = redact_sql_literals(data["query"], extra_columns=extra_cols)
		except Exception:
			pass
		return _original_register(self, data)

	_profiler_init._profiler_patched = True
	_profiler_init.__wrapped__ = _original_init
	_profiler_register._profiler_patched = True
	_profiler_register.__wrapped__ = _original_register

	Recorder.__init__ = _profiler_init
	Recorder.register = _profiler_register


_patch_recorder()


# ---------------------------------------------------------------------------
# sys.monitoring tool-2 startup probe (v0.7.x+) — Critical Risk #3
# ---------------------------------------------------------------------------
# On Python 3.12+ line_profiler claims sys.monitoring.PROFILER_ID (tool 2).
# If a worker died mid-Phase-2 without running its after_request finally
# (or hit the pre-6f66a43 teardown bug), tool 2 stays owned by
# "line_profiler" process-globally — and the next request to that worker
# inherits the orphan AND gets line-traced. The pre-arm self-heal in
# optimus/line_profile/hooks.py covers Phase 2 paths but only fires when
# the NEXT Phase 2 request runs; every interim request between worker
# boot and that Phase 2 fire pays the line-trace tax.
#
# This probe runs ONCE at app-import (between _patch_recorder and
# _try_install_capture_wraps) and:
#
#   * Auto-reclaims tool 2 when it's owned by "line_profiler" — the
#     worker-respawn-after-crash case — and LOGS the recovery so the
#     event is visible in journalctl + Error Log.
#   * Logs LOUDLY when tool 2 is owned by an unknown component (third-
#     party profiler / debugger / future Python change), but does NOT
#     touch the tool. Phase 2 will conflict; the operator decides.
#   * Silent (and a no-op) on Python < 3.12 (no sys.monitoring) or when
#     nobody owns tool 2.
#
# Best-effort everywhere — never raises out of the import path.


def _startup_probe_tool2() -> None:
	"""Detect a leaked sys.monitoring tool 2 at app-import. See the
	rationale comment above. Best-effort; mirrors the discipline of
	_patch_enqueue / _patch_recorder."""
	try:
		import sys

		mon = getattr(sys, "monitoring", None)
		if mon is None:
			return  # Python < 3.12 — nothing to probe
		pid = mon.PROFILER_ID
		owner = mon.get_tool(pid)
		if owner is None:
			return  # happy path: nobody owns it

		# Resolve a usable logger. Pre-frappe-init contexts (some bench
		# bootstrap modes, plain pytest) won't have frappe.logger ready;
		# degrade gracefully to print so the warning is still visible.
		try:
			import frappe

			log = frappe.logger().warning
		except Exception:
			log = print

		if owner == "line_profiler":
			log(
				"optimus._startup_probe_tool2: reclaiming line_profiler "
				"orphan on sys.monitoring tool 2 (worker died mid-Phase-2). "
				"Auto-released; next Phase 2 run starts clean."
			)
			try:
				mon.set_events(pid, 0)
				mon.free_tool_id(pid)
			except Exception:
				pass
		else:
			log(
				f"optimus._startup_probe_tool2: sys.monitoring tool 2 is "
				f"already owned by {owner!r} at app-import. Phase 2 will "
				f"conflict; check for a third-party profiler / debugger."
			)
	except Exception as exc:
		# Probe failure must never break app load. Best-effort log + return.
		try:
			import frappe

			frappe.log_error(title="optimus._startup_probe_tool2")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure("startup_probe_tool2", exc)
		except Exception:
			pass


_startup_probe_tool2()


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
	except Exception as exc:
		try:
			frappe.log_error(title="optimus capture.install_wraps")
		except Exception:
			pass  # never let a logging failure break app load
		try:
			from optimus import telemetry
			telemetry.emit_failure("capture_install_wraps", exc)
		except Exception:
			pass
		return False


# Best-effort: install now if frappe is ready; otherwise the
# before_request / before_job hooks will trigger the deferred install
# on first request.
_try_install_capture_wraps()


# ---------------------------------------------------------------------------
# v0.12.0: Redis schema-version sentinel
# ---------------------------------------------------------------------------
# Write the current SCHEMA_VERSION to ``optimus:schema_version`` at app
# import so future migration paths can detect upgrades. Idempotent —
# overwrites the sentinel on every boot. Best-effort: a Redis hiccup
# must never break app load, same discipline as ``_startup_probe_tool2``
# and ``_try_install_capture_wraps`` above. See
# :mod:`optimus.redis_schema` for the contract.
def _write_schema_sentinel() -> None:
	try:
		from optimus import redis_schema

		redis_schema.write_schema_sentinel()
	except Exception:
		# Sentinel write isn't strictly required for normal operation
		# (no read path uses it yet). Silently degrade.
		pass


_write_schema_sentinel()
