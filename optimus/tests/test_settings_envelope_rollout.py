# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.11: ``settings.get_config()`` is the first cache value to migrate
to the v0.12.0 ``wrap_value`` / ``unwrap_value`` envelope.

The rollout has three contract pieces under test:

  1. **Write path** — a fresh ``get_config`` call that misses the cache
     re-resolves via ``_resolve`` and stores the OptimusConfig field dict
     INSIDE the envelope (``{"_v": 1, "data": {...}}``), not as a bare
     dict.
  2. **New-shape read path** — a ``get_config`` call against a cache that
     already holds an enveloped value unwraps cleanly and returns a
     valid OptimusConfig.
  3. **Legacy-compat read path** — a ``get_config`` call against a cache
     that holds a PRE-v0.12.11 bare-dict value (no ``_v`` key) ALSO
     unwraps cleanly. This is the migration-safety guarantee — readers
     that get rolled out before writers (e.g. a worker on the new code
     hits a Redis value left over from a worker on the old code) must
     NOT crash.

Each test injects a tiny fake ``frappe.cache`` whose ``get_value`` /
``set_value`` are dict-backed so we can introspect the exact shape that
the rollout writes / reads. Per the standing
``[[feedback_frappe_db_local_proxy]]`` advice the same pattern applies
to ``frappe.cache`` (replace wholesale, don't patch attributes).
"""

from __future__ import annotations

import sys
import types
from unittest import mock


class _FakeCache:
	"""Dict-backed ``frappe.cache`` substitute. Exposes the slice of API
	the rollout under test actually touches (``get_value`` / ``set_value``
	/ ``delete_value``)."""

	def __init__(self) -> None:
		self.store: dict = {}

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, **_):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


def _fresh_settings_module():
	"""Return a fresh import of ``optimus.settings`` so each test's
	``_CACHE_KEY`` resolution doesn't carry state across tests. Required
	because the module's `_CACHE_KEY` is computed once at import-time."""
	for mod_name in list(sys.modules):
		if mod_name == "optimus.settings" or mod_name.startswith("optimus.settings."):
			del sys.modules[mod_name]
	import optimus.settings as fresh

	return fresh


def _stub_frappe(cache: _FakeCache, *, has_doctype: bool = False):
	"""Build a minimal ``frappe`` stub that ``settings.get_config`` is
	happy with. ``has_doctype=False`` short-circuits ``_read_doctype_row``
	to None so ``_resolve()`` uses ``_DEFAULTS`` (deterministic test
	inputs)."""
	frappe = types.SimpleNamespace()
	frappe.cache = cache
	frappe.conf = {}
	frappe.flags = types.SimpleNamespace(in_test=True)
	# get_cached_doc / db.exists / get_single_value not reached on the
	# happy path when has_doctype=False; provide harmless stubs.
	db = types.SimpleNamespace()
	db.exists = lambda *_a, **_kw: False if not has_doctype else True
	frappe.db = db
	frappe.get_cached_doc = lambda *_a, **_kw: (_ for _ in ()).throw(
		RuntimeError("get_cached_doc should not be called when has_doctype=False")
	)
	return frappe


class TestSettingsEnvelopeWrite:
	"""On a cache miss, ``get_config`` re-resolves and stores the result
	INSIDE the v0.12.0 envelope. Catches a regression where a future
	refactor accidentally reverts the wrap_value call to bare-dict
	writes."""

	def test_fresh_write_stores_envelope_not_bare_dict(self):
		fresh = _fresh_settings_module()
		cache = _FakeCache()
		frappe_stub = _stub_frappe(cache)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		assert cfg is not None
		# The cache now has the envelope, not a bare cfg dict.
		stored = cache.store.get(fresh._CACHE_KEY)
		assert isinstance(stored, dict), f"expected envelope dict in cache; got: {stored!r}"
		assert "_v" in stored, (
			f"settings cache value missing envelope version sentinel ('_v'); got: {stored!r}"
		)
		assert "data" in stored, (
			f"settings cache value missing envelope payload field ('data'); got: {stored!r}"
		)
		# Version pins to the current SCHEMA_VERSION (= 1 in v0.12.0
		# baseline). If the test starts failing on a future bump, that's
		# the migration moment — bump together with redis_schema.
		from optimus.redis_schema import SCHEMA_VERSION

		assert stored["_v"] == SCHEMA_VERSION
		# Payload is the OptimusConfig.__dict__ shape — same keys the
		# OptimusConfig dataclass exposes.
		assert isinstance(stored["data"], dict)
		assert "ai_enabled" in stored["data"]


class TestSettingsEnvelopeReadHappyPath:
	"""A cache HIT against the new-shape envelope returns a valid
	OptimusConfig without re-resolving."""

	def test_hit_on_enveloped_value_returns_config(self):
		fresh = _fresh_settings_module()
		cache = _FakeCache()
		# Pre-seed the cache with a properly-enveloped value.
		from optimus.redis_schema import wrap_value

		seed_payload = fresh.OptimusConfig().__dict__
		cache.store[fresh._CACHE_KEY] = wrap_value(seed_payload)
		frappe_stub = _stub_frappe(cache)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		assert cfg is not None
		# The OptimusConfig was reconstructed from the unwrapped payload.
		# Nothing was re-written to the cache (still the original
		# envelope).
		stored = cache.store.get(fresh._CACHE_KEY)
		assert stored == wrap_value(seed_payload), "cache value changed unexpectedly during a cache HIT"


class TestSettingsEnvelopeLegacyCompat:
	"""A cache HIT against a PRE-v0.12.11 bare-dict value (no envelope)
	STILL returns a valid OptimusConfig. This is the migration-safety
	contract that lets new readers handle stale legacy values left
	behind by old writers."""

	def test_hit_on_legacy_bare_dict_returns_config(self):
		fresh = _fresh_settings_module()
		cache = _FakeCache()
		# Pre-seed with a BARE OptimusConfig field dict — no envelope, no
		# ``_v`` key. This is exactly what pre-v0.12.11 writers stored.
		seed_payload = fresh.OptimusConfig().__dict__
		cache.store[fresh._CACHE_KEY] = seed_payload
		frappe_stub = _stub_frappe(cache)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		assert cfg is not None
		# CRITICAL: the legacy value was NOT discarded — it was used
		# as-is. (Migrating legacy values on read is explicitly OUT of
		# scope for the rollout; the next on_update cache invalidation
		# + re-resolve will produce a new-shape envelope.)
		stored = cache.store.get(fresh._CACHE_KEY)
		assert stored == seed_payload, (
			"legacy value was rewritten on read; the rollout's "
			"compatibility contract is 'pass through unchanged'"
		)


class TestSettingsEnvelopeDriftHandling:
	"""A cache HIT against a value with a MISMATCHED ``_v`` (future
	schema not yet supported) falls through to ``_resolve`` and stores a
	fresh new-shape envelope. The drift event lands in the in-process
	telemetry buffer for operator visibility."""

	def test_drift_falls_through_to_resolve(self):
		fresh = _fresh_settings_module()
		cache = _FakeCache()
		# Seed with an envelope tagged as schema version 999 — a future
		# version this build doesn't recognise.
		cache.store[fresh._CACHE_KEY] = {"_v": 999, "data": {"ai_enabled": True}}
		frappe_stub = _stub_frappe(cache)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		# Drift → fall-through to _resolve → returns defaults; the cache
		# is then re-written with the current envelope shape.
		assert cfg is not None
		stored = cache.store.get(fresh._CACHE_KEY)
		from optimus.redis_schema import SCHEMA_VERSION

		assert isinstance(stored, dict)
		assert stored.get("_v") == SCHEMA_VERSION, (
			f"after drift detection, cache should be re-written with the "
			f"current envelope version; got: {stored!r}"
		)
