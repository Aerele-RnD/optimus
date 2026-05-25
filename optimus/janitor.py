# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Periodic cleanup of stale profiler sessions.

Wired into hooks.py as a scheduler_event running every 5 minutes. Catches
two failure modes:

  1. A user clicked Start, walked away, and never clicked Stop. After 10
     minutes the Redis active pointer auto-expires (TTL on the key) but
     the Optimus Session DocType row is still in `Recording` state with
     no path forward.

  2. A worker crashed mid-analyze, leaving a row in `Analyzing` state with
     no in-flight job. Without the janitor it would sit there forever.

Both cases are handled by force-stopping the session: clear the Redis
state, mark the row as Stopping, and enqueue analyze.run. If the analyze
itself was the failure cause, it will retry once and end up in `Failed`
on the next attempt — at least the row no longer pretends to be live.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from optimus import safe_commit, session

# Sessions stuck in Recording for longer than this are force-stopped.
# This is intentionally longer than the Redis TTL (10 minutes) to give
# the natural auto-stop a chance to work first.
STALE_RECORDING_MINUTES = 11

# Sessions stuck in Analyzing longer than this are assumed to have a
# crashed worker. The analyze job's RQ timeout is 25 minutes (long
# queue), so we wait a bit more before declaring it dead.
STALE_ANALYZING_MINUTES = 30

# Default retention for successfully-analyzed sessions. After this many
# days, sessions in Ready or Failed state are deleted by the daily
# janitor. Overridable via site_config.json: optimus_session_retention_days
DEFAULT_RETENTION_DAYS = 90

# Hard cap on how many sessions we delete per run. Prevents a single
# janitor call from locking up the DB on a site with a huge backlog.
MAX_DELETIONS_PER_RUN = 100


def sweep_stale_sessions():
	"""Run from scheduler every 5 minutes. Force-stop or mark-failed any stuck sessions."""
	try:
		_sweep_stale_recording()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_stale_recording")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_stale_recording", exc)
		except Exception:
			pass

	try:
		_sweep_stuck_analyzing()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_stuck_analyzing")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_stuck_analyzing", exc)
		except Exception:
			pass

	# v0.7.x: sessions stranded in "Stopping" (analyze never ran — no worker,
	# backlog, or an OOM-killed worker left a zombie job) — re-enqueue analyze.
	try:
		_sweep_stale_stopping()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_stale_stopping")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_stale_stopping", exc)
		except Exception:
			pass

	# v0.6.0: phase-2 line-profile runs follow the same staleness model
	# but live on the Optimus Phase Two Run child rows. Reuses the same
	# thresholds (recording → 11min, analyzing → 30min).
	try:
		_sweep_stale_phase2_runs()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_stale_phase2_runs")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_stale_phase2_runs", exc)
		except Exception:
			pass


def sweep_old_sessions():
	"""Run from scheduler daily. Delete old Ready/Failed sessions per retention policy.

	Only deletes sessions in terminal states (Ready or Failed) older than
	the configured retention (default: 90 days). Active sessions are
	never touched here — the 5-minute janitor handles those.

	Also cleans up:
	- Attached report files so MariaDB and file storage shrink together
	- Orphaned profiler:session:* Redis keys whose parent Profiler
	  Session row no longer exists (e.g. from failed analyzes that
	  never retried, or manual DocType deletions)
	"""
	try:
		_sweep_old_sessions()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_old_sessions")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_old_sessions", exc)
		except Exception:
			pass

	try:
		_sweep_orphan_redis_state()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_orphan_redis_state")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_orphan_redis_state", exc)
		except Exception:
			pass

	# v0.8.0: opt-in failure telemetry retention. Only deletes anything
	# when the feature is enabled; safe to call unconditionally.
	try:
		_sweep_old_telemetry()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_old_telemetry")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_old_telemetry", exc)
		except Exception:
			pass

	# v0.12.18: proactive schema-version drift detection. Compares the
	# persisted ``optimus:schema_version`` sentinel against the current
	# ``SCHEMA_VERSION`` constant and emits ONE telemetry event per
	# drift detection so operators see the schema mismatch in the
	# Optimus Telemetry Event DocType before per-value
	# ``redis.schema_drift`` events accumulate on every read.
	try:
		_sweep_schema_drift()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_schema_drift")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_schema_drift", exc)
		except Exception:
			pass

	# v0.12.25: per-value envelope-version census across the
	# session_meta cache. Complements _sweep_schema_drift (sentinel
	# signal) with per-key counts (value signal): an operator who
	# just upgraded sees "5 of 12 session_meta values are still at
	# envelope v0" instead of just "schema bumped". Pure visibility;
	# the v0.12.21 unwrap_value legacy-detection branch handles the
	# actual reading correctly.
	try:
		_sweep_envelope_versions()
	except Exception as exc:
		frappe.log_error(title="optimus janitor sweep_envelope_versions")
		try:
			from optimus import telemetry
			telemetry.emit_failure("janitor.sweep_envelope_versions", exc)
		except Exception:
			pass


def _sweep_orphan_redis_state():
	"""Delete profiler:session:*:* Redis keys with no matching DocType row.

	Round 2 fix #11. A failed analyze that never retries leaves its
	meta and recordings sets in Redis forever. This daily sweep catches
	those orphans — scans for profiler:session:* keys, extracts the
	uuid, checks if the Optimus Session row still exists, and deletes
	if not.

	Safe to run repeatedly. Uses SCAN with small batches so large
	keyspaces don't block Redis.
	"""
	try:
		redis_conn = frappe.cache.get_redis_connection()
	except Exception:
		return

	# Get the site-qualified prefix so we only scan our own keys, not
	# other sites sharing the same Redis instance.
	try:
		site_prefix_bytes = frappe.cache.make_key("")
		site_prefix = (
			site_prefix_bytes.decode()
			if isinstance(site_prefix_bytes, bytes)
			else site_prefix_bytes
		)
	except Exception:
		site_prefix = ""

	pattern = f"{site_prefix}profiler:session:*"

	# Collect session UUIDs from the Redis keyspace
	uuids_in_redis: set[str] = set()
	try:
		cursor = 0
		while True:
			cursor, keys = redis_conn.scan(cursor, match=pattern, count=100)
			for key in keys:
				key_str = key.decode() if isinstance(key, bytes) else key
				# Key shape: "<site_prefix>profiler:session:<uuid>:meta"
				# or "<site_prefix>profiler:session:<uuid>:recordings"
				parts = key_str.split("profiler:session:", 1)
				if len(parts) < 2:
					continue
				suffix = parts[1]  # "<uuid>:meta" or "<uuid>:recordings"
				uuid = suffix.split(":", 1)[0]
				if uuid:
					uuids_in_redis.add(uuid)
			if cursor == 0:
				break
	except Exception:
		return

	if not uuids_in_redis:
		return

	# Batch the existence check via a single IN query
	existing_rows = frappe.db.get_all(
		"Optimus Session",
		filters={"session_uuid": ["in", list(uuids_in_redis)]},
		fields=["session_uuid"],
	)
	existing_uuids = {r["session_uuid"] for r in existing_rows}
	orphan_uuids = uuids_in_redis - existing_uuids

	if not orphan_uuids:
		return

	# Delete the orphan keys. Each orphan uuid corresponds to at most
	# two keys: :meta and :recordings.
	deleted = 0
	for uuid in orphan_uuids:
		for suffix in (":meta", ":recordings"):
			key = f"{site_prefix}profiler:session:{uuid}{suffix}"
			try:
				if redis_conn.delete(key):
					deleted += 1
			except Exception:
				pass

	if deleted:
		try:
			frappe.logger().info(
				f"optimus orphan sweep deleted {deleted} Redis keys "
				f"across {len(orphan_uuids)} orphaned sessions"
			)
		except Exception:
			pass


def _sweep_schema_drift():
	"""v0.12.18: proactive sentinel-based schema-drift detection.

	Compares the persisted ``optimus:schema_version`` sentinel against
	the current ``optimus.redis_schema.SCHEMA_VERSION``. On mismatch:

	  * Writes the current sentinel via ``write_schema_sentinel`` so
	    subsequent sweeps see the new value (single emit per drift).
	  * Emits ONE ``janitor.schema_sentinel_drift`` telemetry event
	    with both versions in the context so operators see the
	    transition in ``Optimus Telemetry Event``.

	Does NOT migrate or purge any cache values — the per-value
	``unwrap_value`` helper handles legacy shapes via its
	legacy-detection branch (returns the bare value as-is), and
	future-shape mismatches emit per-read ``redis.schema_drift``
	events. This sweep is the **visibility** complement to those
	reactive paths.

	No-op on the happy path (sentinel matches current). No-op on a
	fresh install where the sentinel hasn't been written yet AND
	the bench's prior version was also at the current schema — in
	that case ``read_schema_sentinel`` returns None, which we treat
	as "drift from no version → current" and write the sentinel
	without emitting telemetry (this is the normal startup path,
	not a drift event).
	"""
	try:
		from optimus.redis_schema import (
			SCHEMA_VERSION,
			read_schema_sentinel,
			write_schema_sentinel,
		)
	except Exception:
		return

	persisted = read_schema_sentinel()
	if persisted == SCHEMA_VERSION:
		return  # no drift

	# Persisted is missing (fresh install or pre-v0.12.0 bench) or
	# different from the current. Either way, write the current
	# sentinel so subsequent sweeps see the new value. The startup
	# probe in optimus/__init__.py also writes it on every worker
	# boot; this sweep is the "ops-cron-driven" backstop for cases
	# where worker boots got skipped.
	try:
		write_schema_sentinel()
	except Exception:
		pass

	# Only emit telemetry on a REAL drift (sentinel was present but
	# != current). A missing sentinel is the fresh-install path —
	# not interesting enough to fire telemetry every time.
	if persisted is None:
		return

	try:
		from optimus import telemetry
		telemetry.emit_failure(
			"janitor.schema_sentinel_drift",
			context={
				"persisted_version": str(persisted),
				"current_version": str(SCHEMA_VERSION),
			},
			severity="warning",
		)
	except Exception:
		pass


def _sweep_envelope_versions():
	"""v0.12.25: census of envelope versions across the session_meta
	cache. Emits one ``janitor.envelope_version_census`` telemetry
	event per sweep with the per-version counts in context.

	The sentinel sweep (:func:`_sweep_schema_drift`) tells operators
	"schema version on disk differs from the code's"; this sweep
	answers the follow-up question "OK, how many cached values are
	actually stale?". Useful right after a schema bump to plan
	cleanup vs. let-them-age-out.

	Only sweeps ``session_meta`` for now (the v0.12.21 rollout
	target). Future per-value rollouts can add their own keys to the
	scan pattern below.

	Pure visibility — does NOT delete or rewrite values. The
	:func:`optimus.redis_schema.unwrap_value` legacy-detection branch
	handles the actual reading correctly; this sweep is a one-per-day
	count for operator dashboards.
	"""
	try:
		from optimus.redis_schema import SCHEMA_VERSION, unwrap_value
	except Exception:
		return

	try:
		redis_conn = frappe.cache.get_redis_connection()
	except Exception:
		return

	# Same site-prefix scoping as _sweep_orphan_redis_state.
	try:
		site_prefix_bytes = frappe.cache.make_key("")
		site_prefix = (
			site_prefix_bytes.decode()
			if isinstance(site_prefix_bytes, bytes)
			else site_prefix_bytes
		)
	except Exception:
		site_prefix = ""

	pattern = f"{site_prefix}profiler:session:*:meta"

	# Count per envelope version: current, legacy (None), drift (other).
	current = 0
	legacy = 0
	drift = 0
	total = 0

	try:
		cursor = 0
		while True:
			cursor, keys = redis_conn.scan(cursor, match=pattern, count=100)
			for key in keys:
				key_str = key.decode() if isinstance(key, bytes) else key
				# Read via frappe.cache so pickle unwrapping happens.
				# The prefix returned by redis_conn.scan includes the
				# site_prefix; strip it because frappe.cache.get_value
				# re-prefixes via make_key internally.
				if site_prefix and key_str.startswith(site_prefix):
					app_key = key_str[len(site_prefix):]
				else:
					app_key = key_str
				try:
					raw = frappe.cache.get_value(app_key)
				except Exception:
					continue
				if raw is None:
					continue
				total += 1
				_payload, observed = unwrap_value(raw)
				if observed == SCHEMA_VERSION:
					current += 1
				elif observed is None:
					legacy += 1
				else:
					drift += 1
			if cursor == 0:
				break
	except Exception:
		return

	# Skip emitting telemetry on an empty bench (the sweep ran fine,
	# there's just nothing to report). Also skip on the all-current
	# happy path — no need to flood Optimus Telemetry Event with
	# daily clean-bench checkpoints. Only emit when the operator
	# needs to know (legacy + drift > 0). The telemetry severity
	# system only supports "error" / "warning" (anything else gets
	# coerced to "error" — see telemetry.py:128); use "warning" for
	# this informational-but-actionable signal.
	if total == 0 or (legacy + drift) == 0:
		return

	try:
		from optimus import telemetry
		telemetry.emit_failure(
			"janitor.envelope_version_census",
			context={
				"target": "session_meta",
				"total": str(total),
				"current_version": str(current),
				"legacy_unversioned": str(legacy),
				"drift_other": str(drift),
				"schema_version": str(SCHEMA_VERSION),
			},
			severity="warning",
		)
	except Exception:
		pass


def _sweep_old_telemetry():
	"""Delete Optimus Telemetry Event rows older than the configured
	retention window. Capped at :data:`MAX_DELETIONS_PER_RUN` so a
	noisy site can't lock the DB with a single sweep — the next daily
	tick picks up where this one stopped.

	No-op when the feature is OFF (the DocType row still exists, but
	there's nothing to delete because nothing was being written).
	"""
	try:
		from optimus import settings
		cfg = settings.get_config()
	except Exception:
		return
	if not getattr(cfg, "telemetry_enabled", False):
		return

	retention_days = max(1, int(getattr(cfg, "telemetry_retention_days", 30) or 30))
	cutoff = add_to_date(now_datetime(), days=-retention_days)
	try:
		# Direct SQL with the same cap pattern other sweeps use; bypasses
		# the controller (which is a no-op anyway — see the DocType .py).
		frappe.db.sql(
			"""
			DELETE FROM `tabOptimus Telemetry Event`
			WHERE `last_seen` < %s
			ORDER BY `last_seen` ASC
			LIMIT %s
			""",
			(cutoff, MAX_DELETIONS_PER_RUN),
		)
		safe_commit()
	except Exception:
		# Best-effort — the caller has its own try/except wrapper.
		raise


def _sweep_old_sessions():
	retention_days = (
		frappe.conf.get("optimus_session_retention_days") or DEFAULT_RETENTION_DAYS
	)
	cutoff = add_to_date(now_datetime(), days=-int(retention_days))

	old = frappe.db.get_all(
		"Optimus Session",
		filters={
			"status": ["in", ["Ready", "Failed"]],
			"started_at": ["<", cutoff],
		},
		fields=[
			"name", "title",
			"raw_report_file", "raw_report_pdf_file",
		],
		limit=MAX_DELETIONS_PER_RUN,
		order_by="started_at asc",
	)

	# Phase K hardening: if this batch is at the per-run cap, the
	# retention sweep may be falling behind the recording rate. Count
	# the remaining stale sessions and stash an operator-visible
	# backlog metric so monitoring catches the drift before the
	# Optimus Session table grows unbounded.
	if len(old) >= MAX_DELETIONS_PER_RUN:
		try:
			remaining = frappe.db.count(
				"Optimus Session",
				filters={
					"status": ["in", ["Ready", "Failed"]],
					"started_at": ["<", cutoff],
				},
			)
		except Exception:
			remaining = None
		backlog = max(0, (remaining or 0) - len(old))
		try:
			from optimus import redis_keys, redis_schema
			frappe.cache.set_value(
				redis_keys.retention_backlog(),
				redis_schema.wrap_value(backlog),
				expires_in_sec=3600,
			)
		except Exception:
			pass
		try:
			frappe.logger().warning(
				f"optimus janitor: MAX_DELETIONS_PER_RUN ({MAX_DELETIONS_PER_RUN}) "
				f"hit; ~{backlog} stale session(s) remain after this batch"
			)
		except Exception:
			pass
	else:
		# Backlog cleared - reset the counter so monitoring sees the
		# recovery on the next pass.
		try:
			from optimus import redis_keys, redis_schema
			frappe.cache.set_value(
				redis_keys.retention_backlog(),
				redis_schema.wrap_value(0),
				expires_in_sec=3600,
			)
		except Exception:
			pass

	# v0.6.x: preload File names for every candidate URL in ONE bulk fetch
	# (was a per-URL get_value inside the outer loop → O(rows × urls/row)
	# round-trips). Map url → File doc name; missing entries simply yield
	# None and the deletion path skips them.
	all_urls = {
		u for r in old
		for u in (r.get("raw_report_file"), r.get("raw_report_pdf_file"))
		if u
	}
	file_name_by_url: dict[str, str] = {}
	if all_urls:
		try:
			file_name_by_url = {
				f["file_url"]: f["name"]
				for f in frappe.db.get_all(
					"File",
					filters={"file_url": ("in", list(all_urls))},
					fields=["name", "file_url"],
				)
			}
		except Exception:
			# Defensive: if the bulk fetch fails (DB hiccup, perm), the
			# loop below still runs — it just won't find any File docs to
			# delete and the orphans-cleanup is a no-op for this pass.
			file_name_by_url = {}

	deleted = 0
	for row in old:
		try:
			# Delete attached report files first so we don't leave
			# orphaned File docs behind. v0.6.0 Round 7: dropped the
			# safe_report_file / safe_report_pdf_file slots — single
			# raw report + lazy PDF.
			for file_url in (
				row.get("raw_report_file"),
				row.get("raw_report_pdf_file"),
			):
				if not file_url:
					continue
				file_doc = file_name_by_url.get(file_url)
				if file_doc:
					try:
						frappe.delete_doc(
							"File",
							file_doc,
							ignore_permissions=True,
							delete_permanently=True,
						)
					except Exception:
						pass

			frappe.delete_doc(
				"Optimus Session",
				row["name"],
				ignore_permissions=True,
				delete_permanently=True,
			)
			deleted += 1
		except Exception as exc:
			frappe.log_error(title=f"optimus retention delete {row['name']}")
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"janitor.retention_delete", exc,
					context={"docname": row.get("name") or ""},
				)
			except Exception:
				pass

	if deleted:
		safe_commit()
		frappe.logger().info(
			f"optimus retention janitor deleted {deleted} session(s) "
			f"older than {retention_days} days"
		)


def _sweep_stale_recording():
	"""Find Recording rows older than STALE_RECORDING_MINUTES and force-stop them."""
	cutoff = add_to_date(now_datetime(), minutes=-STALE_RECORDING_MINUTES)
	stale = frappe.db.get_all(
		"Optimus Session",
		filters={"status": "Recording", "started_at": ["<", cutoff]},
		fields=["name", "session_uuid", "user"],
	)
	if not stale:
		return

	# v0.6.x: single batched UPDATE for every stale row instead of one
	# UPDATE+COMMIT per row (was N round-trips on a backlog).
	frappe.db.set_value(
		"Optimus Session",
		{"name": ("in", [r["name"] for r in stale])},
		{"status": "Stopping", "stopped_at": now_datetime()},
	)
	safe_commit()

	for row in stale:
		# The user's Redis active pointer should already be expired (TTL),
		# but call clear to be defensive.
		try:
			session.clear_active_session(row["user"])
		except Exception:
			pass

		# Enqueue analyze for whatever recordings did get captured before
		# the user walked away. The analyze job handles empty sessions
		# gracefully — it will mark the session Ready with a "no traffic
		# was recorded" summary.
		try:
			frappe.enqueue(
				"optimus.analyze.run",
				queue="long",
				session_uuid=row["session_uuid"],
			)
		except Exception as exc:
			frappe.log_error(title=f"optimus janitor enqueue {row['session_uuid']}")
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"janitor.enqueue_analyze", exc,
					context={"session_uuid": row.get("session_uuid") or ""},
				)
			except Exception:
				pass


def _sweep_stuck_analyzing():
	"""Find Analyzing rows older than STALE_ANALYZING_MINUTES and mark Failed."""
	cutoff = add_to_date(now_datetime(), minutes=-STALE_ANALYZING_MINUTES)
	# We use modified as a proxy for "when did the analyze start", since
	# analyze.run sets status to Analyzing first thing.
	stuck = frappe.db.get_all(
		"Optimus Session",
		filters={"status": "Analyzing", "modified": ["<", cutoff]},
		fields=["name"],
	)
	if not stuck:
		return

	# v0.6.x: single batched UPDATE.
	frappe.db.set_value(
		"Optimus Session",
		{"name": ("in", [r["name"] for r in stuck])},
		{
			"status": "Failed",
			"analyzer_warnings": "Analyze job timed out or crashed. Manually retry from a Frappe console: optimus.analyze.run('<session_uuid>')",
		},
	)
	safe_commit()


def _sweep_stale_stopping():
	"""Find rows stuck in ``Stopping`` longer than STALE_RECORDING_MINUTES and
	re-enqueue analyze.

	``Stopping`` is meant to last only the instant between ``api._mark_stopping``
	and ``analyze.run`` setting ``Analyzing``. Lingering there means the analyze
	job never ran — no worker on the ``long`` queue, a queue backlog, or a
	worker OOM-killed mid-analyze that left a zombie job. Re-enqueue so the
	session self-heals once a worker is available (analyze is idempotent and
	handles empty sessions). We bump ``modified`` (re-affirming the status) so a
	still-stuck row backs off ~one window between retries instead of stacking a
	job every sweep; if the re-enqueued analyze then wedges in ``Analyzing``,
	``_sweep_stuck_analyzing`` is the next backstop."""
	cutoff = add_to_date(now_datetime(), minutes=-STALE_RECORDING_MINUTES)
	stale = frappe.db.get_all(
		"Optimus Session",
		filters={"status": "Stopping", "modified": ["<", cutoff]},
		fields=["name", "session_uuid"],
	)
	if not stale:
		return

	# Re-affirm the status to bump ``modified`` → next sweep waits another
	# window before re-enqueuing the same row.
	frappe.db.set_value(
		"Optimus Session",
		{"name": ("in", [r["name"] for r in stale])},
		{"status": "Stopping"},
	)
	safe_commit()

	for row in stale:
		try:
			frappe.enqueue(
				"optimus.analyze.run",
				queue="long",
				session_uuid=row["session_uuid"],
			)
		except Exception as exc:
			frappe.log_error(
				title=f"optimus janitor stopping re-enqueue {row['session_uuid']}"
			)
			try:
				from optimus import telemetry
				telemetry.emit_failure(
					"janitor.stopping_reenqueue", exc,
					context={"session_uuid": row.get("session_uuid") or ""},
				)
			except Exception:
				pass


def _sweep_stale_phase2_runs():
	"""Force-stop Optimus Phase Two Run rows stuck in Recording (>11min) or
	Analyzing (>30min). Mirrors the phase-1 sweep logic but operates on
	the child rows.

	Stale Recording rows: clear the per-user Redis active flag (so future
	requests don't keep instrumenting), mark the row Failed with a note.
	Stale Analyzing rows: mark Failed with the same retry-from-console
	guidance the phase-1 sweep uses.

	Both cases also cleanup the Redis picks/source/samples keys via
	line_profile.capture.cleanup_run so storage doesn't drift.
	"""
	from optimus.line_profile import capture as _lp_capture

	rec_cutoff = add_to_date(now_datetime(), minutes=-STALE_RECORDING_MINUTES)
	rec_stale = frappe.db.get_all(
		"Optimus Phase Two Run",
		filters={"status": "Recording", "modified": ["<", rec_cutoff]},
		fields=["name", "parent", "run_uuid"],
	)
	if rec_stale:
		# v0.6.x: single batched UPDATE; the per-row Redis cleanup stays
		# per-row below since it touches Redis, not the DB.
		try:
			frappe.db.set_value(
				"Optimus Phase Two Run",
				{"name": ("in", [r["name"] for r in rec_stale])},
				{
					"status": "Failed",
					"warnings_json": frappe.as_json([
						"Phase 2 run expired before any line data was captured "
						"(no flow re-run within the window) — auto-stopped by "
						"janitor. To retry: click \"Run Line-Profile Pass\", "
						"re-run your flow, then \"Stop Phase 2 Run\".",
					]),
					"ended_at": now_datetime(),
				},
			)
			safe_commit()
		except Exception as exc:
			frappe.log_error(title="optimus janitor stale phase-2 recording")
			try:
				from optimus import telemetry
				telemetry.emit_failure("janitor.phase2_stale_recording.batch_update", exc)
			except Exception:
				pass
		for row in rec_stale:
			try:
				_lp_capture.cleanup_run(row["run_uuid"])
			except Exception as exc:
				frappe.log_error(title="optimus janitor stale phase-2 recording cleanup")
				try:
					from optimus import telemetry
					telemetry.emit_failure(
						"janitor.phase2_stale_recording.redis_cleanup", exc,
						context={"run_uuid": row.get("run_uuid") or ""},
					)
				except Exception:
					pass

	ana_cutoff = add_to_date(now_datetime(), minutes=-STALE_ANALYZING_MINUTES)
	ana_stuck = frappe.db.get_all(
		"Optimus Phase Two Run",
		filters={"status": "Analyzing", "modified": ["<", ana_cutoff]},
		fields=["name", "parent", "run_uuid"],
	)
	if ana_stuck:
		try:
			frappe.db.set_value(
				"Optimus Phase Two Run",
				{"name": ("in", [r["name"] for r in ana_stuck])},
				{
					"status": "Failed",
					"warnings_json": frappe.as_json([
						"Phase 2 analyze timed out or crashed. Retry from a "
						"Frappe console: "
						"optimus.line_profile.analyzer.run_analyze("
						"'<session_uuid>', '<run_uuid>')",
					]),
					"ended_at": now_datetime(),
				},
			)
			safe_commit()
		except Exception as exc:
			frappe.log_error(title="optimus janitor stuck phase-2 analyzing")
			try:
				from optimus import telemetry
				telemetry.emit_failure("janitor.phase2_stuck_analyzing.batch_update", exc)
			except Exception:
				pass
		for row in ana_stuck:
			try:
				_lp_capture.cleanup_run(row["run_uuid"])
			except Exception as exc:
				frappe.log_error(title="optimus janitor stuck phase-2 analyzing cleanup")
				try:
					from optimus import telemetry
					telemetry.emit_failure(
						"janitor.phase2_stuck_analyzing.redis_cleanup", exc,
						context={"run_uuid": row.get("run_uuid") or ""},
					)
				except Exception:
					pass
