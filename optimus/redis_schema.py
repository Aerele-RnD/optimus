# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Versioned value envelope + schema-version sentinel for Optimus's
Redis state.

The v0.12.0 release establishes the foundation: a single source-of-truth
for the schema version (:data:`SCHEMA_VERSION`), a sentinel key written at
app import (:func:`write_schema_sentinel`), and a pair of opt-in
envelope helpers (:func:`wrap_value`, :func:`unwrap_value`) that future
schema changes can use to keep new code safe against in-flight Redis
values from older releases.

**The contract for future schema changes:**

1. Bump :data:`SCHEMA_VERSION` (in this module AND in
   :data:`optimus.redis_keys.SCHEMA_VERSION` — they must stay in sync).
2. On the WRITE side, wrap the new-shape payload via :func:`wrap_value`
   before passing to ``frappe.cache.set_value`` / ``hset`` / etc.
3. On the READ side, unwrap via :func:`unwrap_value`. The helper
   returns ``(payload, version)``:
   - ``version == SCHEMA_VERSION`` → use ``payload`` directly.
   - ``version is None`` → legacy un-enveloped value; treat as bare
     payload (backward-compat path; eventually migrate it on next write).
   - ``version != SCHEMA_VERSION`` → unknown / future schema; the helper
     returns the caller's ``default`` AND emits a ``redis.schema_drift``
     telemetry event so the operator sees the drift in
     ``Optimus Telemetry Event``.

**What this release does NOT do:**

* It does NOT wrap any existing value. Every current ``set_value`` /
  ``hset`` / ``rpush`` call writes the same bytes it always did. The
  unwrap helper is opt-in for new code; legacy values without ``_v``
  flow through the unchanged-read path.
* It does NOT change the HMAC envelope (``sign_blob`` / ``unsign_blob``
  in :mod:`optimus.session`). Adding a version tag inside the signed
  envelope is a follow-up with its own migration story.
* It does NOT drive proactive janitor sweeps off the sentinel value.
  Reactive cleanup-on-read is sufficient for the foundation. A future
  release can add a janitor pass that enumerates ``profiler:*`` keys
  and migrates / purges those with mismatched versions.

This module imports nothing from Frappe at module top — the helpers
lazy-import inside their functions so pure-pytest tests can exercise
the envelope logic without a bench. The sentinel-write path requires
Frappe (it writes to ``frappe.cache``) but degrades silently when
unavailable.
"""

from __future__ import annotations

from typing import Any

# v0.12.0 baseline. Bumping this constant signals to downstream readers
# that any in-flight value with a different ``_v`` (or no ``_v``) field
# may need migration. Bump together with
# :data:`optimus.redis_keys.SCHEMA_VERSION`.
SCHEMA_VERSION = 1

# Sentinel field name used inside :func:`wrap_value` envelopes. Short
# to keep storage overhead negligible; underscored to avoid clashing
# with payload keys.
_ENVELOPE_VERSION_FIELD = "_v"
_ENVELOPE_PAYLOAD_FIELD = "data"


# ---------------------------------------------------------------------------
# Envelope helpers — opt-in for new schema-change code paths
# ---------------------------------------------------------------------------


def wrap_value(payload: Any, *, version: int = SCHEMA_VERSION) -> dict:
	"""Return a versioned envelope around ``payload`` suitable for
	storage in Redis via ``frappe.cache.set_value`` (or any other
	pickleable-value sink). Shape: ``{"_v": <int>, "data": <payload>}``.

	The wrapper is the explicit signal that THIS value's shape is
	version-controlled. Callers writing legacy un-wrapped values are
	unaffected — they continue to write the bare payload and readers
	detect the legacy shape by absence of the ``_v`` key.
	"""
	return {_ENVELOPE_VERSION_FIELD: int(version), _ENVELOPE_PAYLOAD_FIELD: payload}


def unwrap_value(
	value: Any,
	*,
	expected: int = SCHEMA_VERSION,
	default: Any = None,
) -> tuple[Any, int | None]:
	"""Inspect ``value`` and return ``(payload, version)``:

	  * ``value is None`` (missing key) → ``(default, None)``.
	  * ``value`` is a dict with ``_v == expected`` → ``(value["data"], expected)``.
	  * ``value`` is a dict with ``_v != expected`` → ``(default, <observed>)``
	    AND a ``redis.schema_drift`` telemetry event is emitted.
	  * ``value`` is anything else (legacy un-wrapped) → ``(value, None)``.

	The legacy-detection branch is the migration-safety net: any value
	written before v0.12.0 flows through unchanged. A future PR can
	add a rewrite-on-read step that re-writes legacy values inside an
	envelope on the next access (out of scope for the foundation PR).
	"""
	if value is None:
		return default, None
	if isinstance(value, dict) and _ENVELOPE_VERSION_FIELD in value:
		try:
			observed = int(value.get(_ENVELOPE_VERSION_FIELD) or 0)
		except (TypeError, ValueError):
			observed = 0
		if observed == int(expected):
			return value.get(_ENVELOPE_PAYLOAD_FIELD), observed
		# Drift — emit telemetry but return the caller's default so the
		# host code path can degrade gracefully. We DON'T try to migrate
		# inline; that would couple this helper to every value shape's
		# migration rules. Operators see the drift in Optimus Telemetry
		# Event and a future PR can ship the migration.
		try:
			from optimus import telemetry

			telemetry.emit_failure(
				"redis.schema_drift",
				context={
					"observed_version": str(observed),
					"expected_version": str(expected),
				},
				severity="warning",
			)
		except Exception:
			pass
		return default, observed
	# Legacy un-wrapped value (or a non-dict like a raw string). Pass
	# through; the caller treats it as the bare payload.
	return value, None


# ---------------------------------------------------------------------------
# Sentinel key — written at app import, read by future migration paths
# ---------------------------------------------------------------------------


def write_schema_sentinel() -> None:
	"""Write the current :data:`SCHEMA_VERSION` to the sentinel key (see
	:func:`optimus.redis_keys.schema_version`). Idempotent — overwrites
	whatever was there. Best-effort; a Redis hiccup at app-import must
	never break app load (the same discipline as
	:func:`optimus._startup_probe_tool2`).

	Runs once per worker boot. On a bench upgrading from a pre-v0.12.0
	release, this is the first write of the sentinel; the previous
	value (returned by :func:`read_schema_sentinel` BEFORE this call)
	is ``None``, which a future migration path can detect.
	"""
	try:
		import frappe

		from optimus import redis_keys

		frappe.cache.set_value(
			redis_keys.schema_version(),
			SCHEMA_VERSION,
		)
	except Exception:
		# Sentinel-write failure is non-fatal — app continues to function;
		# the next worker boot retries.
		pass


def read_schema_sentinel() -> int | None:
	"""Return the persisted schema version, or ``None`` when the
	sentinel is missing (fresh install, pre-v0.12.0 bench, or Redis
	flush). Best-effort; any read failure returns ``None``.

	Future PRs that drive proactive migration off the sentinel call this
	at startup, compare against :data:`SCHEMA_VERSION`, and dispatch
	per-version migrators. The foundation release only writes the
	sentinel; nothing reads it yet (except the test).
	"""
	try:
		import frappe

		from optimus import redis_keys

		raw = frappe.cache.get_value(redis_keys.schema_version())
	except Exception:
		return None
	if raw is None:
		return None
	try:
		return int(raw)
	except (TypeError, ValueError):
		# Sentinel got corrupted (manually set to a non-int?). Treat as
		# missing so the next write_schema_sentinel() overwrites it.
		return None
