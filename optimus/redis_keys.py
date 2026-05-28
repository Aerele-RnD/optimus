# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Single source of truth for every Redis key Optimus writes.

Pre-v0.12.0 the keys were partially centralized — :mod:`optimus.session`
and :mod:`optimus.line_profile.capture` already had private helpers
(``_active_key``, ``_meta_key``, etc.) — but five other modules
(:mod:`optimus.api`, :mod:`optimus.hooks_callbacks`, :mod:`optimus.analyze`,
:mod:`optimus.settings`, :mod:`optimus.janitor`) built keys via inline
f-strings, scattering the schema across the codebase and making future
renames risky.

This module is the canonical inventory. Every key pattern Optimus
writes has a public builder function here; the f-string call sites
have been migrated to call them; the audit test in
:mod:`optimus.tests.test_redis_audit` walks every ``frappe.cache.*``
call and asserts the key argument resolves through this module (or
through the small explicit allowlist of pre-existing centralized
helpers).

**Backward-compatibility:** the strings these builders return are
**identical** to the f-strings they replace. No deployed Redis state
shifts. The migration is purely organisational.

**Schema versioning:** :data:`SCHEMA_VERSION` mirrors the version
stamped on the ``optimus:schema_version`` sentinel key written at app
import (see :mod:`optimus.redis_schema`). Bump it together with the
sentinel whenever a value-shape change ships that the read side needs
to detect. The v0.12.0 baseline is ``1``.

**Namespace conventions:**

* ``profiler:`` prefix — every per-session / per-user / per-recording
  key. Predates the v0.7.0 rename from ``frappe_profiler``; kept for
  backward compatibility with in-flight benches.
* ``optimus:`` prefix — app-level state that's not bound to a session
  (``schema_version``, ``analyze:inflight``, ``retention_backlog``).
* ``optimus_settings_cached`` — legacy single key (no prefix) for the
  cached :class:`OptimusConfig` snapshot. Pre-dates the prefix
  convention; the cost of renaming would be a one-shot cache miss for
  every running bench, so we leave it.

This module imports nothing from Frappe — every function is a pure
string-building helper. Safe to call from any code path Optimus runs.
"""

from __future__ import annotations

# v0.12.0: foundation release for Redis schema versioning. Bumping this
# constant signals to ``redis_schema.unwrap_value`` that any persisted
# value missing or carrying a different ``_v`` field is from a different
# era — the unwrapper either migrates it (when a migration path exists)
# or returns the caller's default.
SCHEMA_VERSION = 1

# Namespace prefixes — documented here, not used in the builders below
# (the f-strings already encode them inline). Useful for the audit test
# and for grep-discovery of the namespace surface.
KEY_PREFIX = "profiler:"
APP_KEY_PREFIX = "optimus:"


# ---------------------------------------------------------------------------
# Phase-1 session lifecycle
# ---------------------------------------------------------------------------


def session_active(user: str) -> str:
	"""Per-user "active session" pointer. Value: the session UUID, or
	absent when no recording is in flight. TTL: 10 minutes (the
	``SESSION_TTL_SECONDS`` constant in :mod:`optimus.session`),
	refreshed on every ``register_recording`` call.

	Reads gate ``before_request`` / ``before_job`` per-user activation."""
	return f"profiler:active:{user}"


def session_meta(session_uuid: str) -> str:
	"""Session metadata hash. Value: a Frappe-pickled dict
	(``session_uuid``, ``docname``, ``user``, ``label``, ``started_at``,
	``capture_python_tree``, optional ``cap_warning``, optional
	``draining_until``). No TTL — explicitly deleted by
	``delete_session_state`` on analyze completion."""
	return f"profiler:session:{session_uuid}:meta"


def session_recordings(session_uuid: str) -> str:
	"""Set of recording UUIDs belonging to a session. Members are raw
	UUID strings; SADD via :func:`optimus.session.register_recording`.
	No TTL — deleted by ``delete_session_state``."""
	return f"profiler:session:{session_uuid}:recordings"


def session_pending_jobs(session_uuid: str) -> str:
	"""Set of RQ job IDs the session's flow enqueued and analyze is
	still waiting on. Members SADD'd by
	:func:`optimus.session.register_pending_job`, SREM'd by
	:func:`clear_pending_job`. No TTL — deleted by
	``delete_session_state``."""
	return f"profiler:session:{session_uuid}:pending_jobs"


def session_jobs(session_uuid: str) -> str:
	"""Per-job tracking hash (the v0.7.x atomic-Lua-merged structure).
	Fields are job IDs; values are JSON-encoded dicts written by the
	``_MERGE_JOB_META_LUA`` Lua script via :func:`_raw_redis` (NOT
	Frappe's pickle-wrapped ``hset``). See REDIS-SCHEMA.md "Dual-encoding
	hazard" for details. No TTL — deleted by ``delete_session_state``."""
	return f"profiler:session:{session_uuid}:jobs"


# ---------------------------------------------------------------------------
# Per-recording artefacts
# ---------------------------------------------------------------------------


def tree(recording_uuid: str) -> str:
	"""HMAC-signed pickle of the pyinstrument ``Profiler.last_session``.
	Envelope: 32-byte signature prefix + pickled tree (see
	:func:`optimus.session.sign_blob`). TTL: ``SESSION_TTL_SECONDS``."""
	return f"profiler:tree:{recording_uuid}"


def sidecar(recording_uuid: str) -> str:
	"""List of sidecar argument-log entries captured during a recording.
	Frappe-pickled ``list[dict]``; an optional ``{"_truncated": True}``
	marker appears at the tail when the sidecar exceeded its cap. TTL:
	``SESSION_TTL_SECONDS``."""
	return f"profiler:sidecar:{recording_uuid}"


def infra(recording_uuid: str) -> str:
	"""Per-recording infra metric delta (CPU / RAM / DB / RQ). Frappe-
	pickled dict produced by :func:`optimus.infra_capture.diff`. TTL:
	``SESSION_TTL_SECONDS``."""
	return f"profiler:infra:{recording_uuid}"


# ---------------------------------------------------------------------------
# Frontend metrics (v0.5.0+ split lists)
# ---------------------------------------------------------------------------


def frontend_xhr(session_uuid: str) -> str:
	"""List of XHR timing entries from the browser shim. Each list entry
	is a JSON-encoded dict (method, url, duration_ms, status, timestamp,
	transport). LTRIM'd to ``SOFT_CAP_FRONTEND_XHR = 1000``. TTL:
	``SESSION_TTL_SECONDS`` (set via ``expire_key`` after every push)."""
	return f"profiler:frontend:{session_uuid}:xhr"


def frontend_vitals(session_uuid: str) -> str:
	"""List of Web Vitals (FCP / LCP / CLS / TTFB / etc.) per page,
	JSON-encoded entries. LTRIM'd to ``SOFT_CAP_FRONTEND_VITALS = 200``.
	TTL: ``SESSION_TTL_SECONDS``."""
	return f"profiler:frontend:{session_uuid}:vitals"


def frontend_legacy(session_uuid: str) -> str:
	"""Pre-v0.5.1 combined frontend dict — a single key holding both XHR
	and vitals data merged via GET-modify-SET. No code path writes this
	any more; only :func:`optimus.session.delete_session_state` reads it
	(as a defensive cleanup target for in-flight benches that still
	carry the legacy shape). No TTL on legacy writes — that's exactly
	the upgrade-leak this PR documents."""
	return f"profiler:frontend:{session_uuid}"


# ---------------------------------------------------------------------------
# Phase-2 line-profile run state
# ---------------------------------------------------------------------------


def lp_active(user: str) -> str:
	"""Phase-2 active-run pointer per user. Value: the run UUID. TTL:
	``SESSION_TTL_SECONDS``. Mutually exclusive with
	:func:`session_active` at the API level (the start endpoint refuses
	one while the other is active)."""
	return f"profiler:lp:active:{user}"


def lp_picks(run_uuid: str) -> str:
	"""Hash of the run's picked functions. Keys are dotted-function
	paths; values are Frappe-pickled metadata dicts (budget, source
	file, etc.). No TTL — deleted by
	:func:`optimus.line_profile.capture.cleanup_run`."""
	return f"profiler:lp:{run_uuid}:picks"


def lp_source(run_uuid: str) -> str:
	"""Hash of per-function source lines captured at picks time. Keys
	are dotted-function paths; values are
	``list[{lineno, content}]``. No TTL — cleaned by ``cleanup_run``."""
	return f"profiler:lp:{run_uuid}:source"


def lp_samples(run_uuid: str) -> str:
	"""List of per-line samples flushed from each request/job under the
	active Phase-2 pass. Each entry is a dict
	(``lineno``, ``count``, ``wall_ms``, ``function``). No TTL — cleaned
	by ``cleanup_run``."""
	return f"profiler:lp:{run_uuid}:samples"


def lp_budget_hit(run_uuid: str) -> str:
	"""Per-run flag set when the per-pass sample budget is hit. Value:
	the literal string ``"1"``. TTL: 3600s (explicit cap so a forgotten
	flag doesn't outlive the run). Cleared by ``clear_budget_hit``."""
	return f"profiler:lp:budget_hit:{run_uuid}"


# ---------------------------------------------------------------------------
# Cross-session / app-level state
# ---------------------------------------------------------------------------


def onboarding_seen(user: str) -> str:
	"""Per-user "dismissed the onboarding toast" marker. Value: any
	truthy string. TTL: 90 days (matches the
	``session_retention_days`` default; documented in REDIS-SCHEMA.md
	§ "TTL discipline"). Pre-v0.12.0 had NO TTL — keys accumulated
	indefinitely; this release fixes the leak."""
	return f"profiler:onboarding_seen:{user}"


def explain_cache(cache_key: str) -> str:
	"""Cache of ``EXPLAIN`` enrichment results, keyed by the analyzer's
	hash of the normalized query. Persists across sessions so identical
	queries don't re-EXPLAIN. No explicit TTL — survives indefinitely
	(this is a pure read-through cache and the value is small)."""
	return f"profiler:explain:{cache_key}"


def analyze_inflight() -> str:
	"""Single-flight guard for ``analyze.run``. Set by the first runner;
	cleared on completion. Prevents two RQ workers from analyzing the
	same session concurrently (the v0.7.x M2 fix)."""
	return "optimus:analyze:inflight"


def retention_backlog() -> str:
	"""Janitor backlog counter — written when the daily sweep hits its
	per-run cap and can't finish in one tick. Read by the operator
	dashboard / Optimus Settings to surface "the bench is producing
	sessions faster than retention can delete them"."""
	return "optimus:retention_backlog"


def settings_cache() -> str:
	"""Cached :class:`OptimusConfig` snapshot. Invalidated by the
	Optimus Settings DocType's ``on_update`` hook. Pre-dates the
	``optimus:`` prefix convention — renaming would force a one-shot
	cache miss for every running bench on upgrade, so the legacy name
	stays."""
	return "optimus_settings_cached"


def schema_version() -> str:
	"""v0.12.0+ schema-version sentinel. Value: an integer literal (the
	current :data:`SCHEMA_VERSION`). Written at app import by
	:func:`optimus.redis_schema.write_schema_sentinel`; read by
	:func:`optimus.redis_schema.read_schema_sentinel`. A version mismatch
	at boot signals an upgrade — a future PR can drive proactive
	migration off this signal."""
	return "optimus:schema_version"


# ---------------------------------------------------------------------------
# KEY_PATTERNS — used by the audit test + REDIS-SCHEMA.md drift check
# ---------------------------------------------------------------------------
#
# Every key pattern Optimus writes, in the canonical placeholder form.
# The audit test asserts:
#   1. Every frappe.cache.* call site in the codebase uses one of these
#      builders (or is on the small allowlist of pre-existing helpers in
#      session.py + line_profile/capture.py).
#   2. The set of patterns here equals the inventory documented in
#      docs/REDIS-SCHEMA.md. Drift in either direction fails CI.
#
# Order matches the function definitions above (for readability + diff
# stability when adding new keys — append, don't reorder).
KEY_PATTERNS: tuple[str, ...] = (
	"profiler:active:<user>",
	"profiler:session:<session_uuid>:meta",
	"profiler:session:<session_uuid>:recordings",
	"profiler:session:<session_uuid>:pending_jobs",
	"profiler:session:<session_uuid>:jobs",
	"profiler:tree:<recording_uuid>",
	"profiler:sidecar:<recording_uuid>",
	"profiler:infra:<recording_uuid>",
	"profiler:frontend:<session_uuid>:xhr",
	"profiler:frontend:<session_uuid>:vitals",
	"profiler:frontend:<session_uuid>",
	"profiler:lp:active:<user>",
	"profiler:lp:<run_uuid>:picks",
	"profiler:lp:<run_uuid>:source",
	"profiler:lp:<run_uuid>:samples",
	"profiler:lp:budget_hit:<run_uuid>",
	"profiler:onboarding_seen:<user>",
	"profiler:explain:<cache_key>",
	"optimus:analyze:inflight",
	"optimus:retention_backlog",
	"optimus_settings_cached",
	"optimus:schema_version",
)
