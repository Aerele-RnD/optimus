# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.21: continues the v0.12.0 ``wrap_value`` / ``unwrap_value``
envelope rollout to ``session_meta`` (the per-session metadata dict
in ``optimus.session``).

The previous phases covered settings_cache (v0.12.11),
retention_backlog + onboarding_seen (v0.12.13), and explain_cache
(v0.12.17). This phase adds ``session_meta`` — the session-scoped
context dict written by ``api.start`` (and updated throughout the
session's life) and read on every recording's before_request /
before_job hook.

The contract under test:

  1. **Write path** — ``set_session_meta`` wraps via ``wrap_value``.
  2. **Read path** — ``get_session_meta`` unwraps via ``unwrap_value``.
  3. **Legacy compat** — pre-v0.12.21 cached values (bare dict) still
     return cleanly through the legacy-detection branch.
  4. **Defensive non-dict guard** — if a future-version envelope or
     a corrupt write returns a non-dict payload, ``get_session_meta``
     returns ``None`` rather than crashing downstream callers.
"""

from __future__ import annotations

import sys
import types
from unittest import mock


class _FakeCache:
	"""Dict-backed ``frappe.cache`` substitute — same shape as the
	other envelope-rollout test modules use."""

	def __init__(self) -> None:
		self.store: dict = {}

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, **_):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


def _make_frappe_stub(cache: _FakeCache):
	"""Build a minimal frappe stub that ``session.set_session_meta`` /
	``get_session_meta`` need (just ``frappe.cache``)."""
	frappe = types.SimpleNamespace()
	frappe.cache = cache
	return frappe


class TestSessionMetaEnvelopeWrite:
	def test_set_session_meta_stores_envelope(self):
		"""On set, the cache value is the wrapped envelope dict, not a
		bare meta dict."""
		from optimus import redis_schema, session

		cache = _FakeCache()
		frappe_stub = _make_frappe_stub(cache)
		uuid = "test-uuid-write"
		payload = {
			"session_uuid": uuid,
			"docname": "test-docname",
			"user": "test@example.com",
			"label": "smoke test",
		}

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			# session.set_session_meta uses module-level `frappe` so we
			# stub it too via direct attribute assignment.
			with mock.patch.object(session, "frappe", frappe_stub):
				session.set_session_meta(uuid, payload)

		stored = cache.store[session._meta_key(uuid)]
		assert isinstance(stored, dict)
		assert stored.get("_v") == redis_schema.SCHEMA_VERSION
		assert stored.get("data") == payload


class TestSessionMetaEnvelopeRead:
	def test_get_session_meta_unwraps_new_envelope(self):
		"""Cache contains a new-shape envelope → returns the payload
		dict."""
		from optimus import redis_schema, session

		cache = _FakeCache()
		frappe_stub = _make_frappe_stub(cache)
		uuid = "test-uuid-read-new"
		payload = {"session_uuid": uuid, "user": "alice"}
		cache.store[session._meta_key(uuid)] = redis_schema.wrap_value(payload)

		with mock.patch.object(session, "frappe", frappe_stub):
			result = session.get_session_meta(uuid)

		assert result == payload

	def test_get_session_meta_handles_legacy_bare_dict(self):
		"""Cache contains a pre-v0.12.21 bare dict → returns the dict
		via unwrap_value's legacy-detection branch. This is the
		migration-safety contract that makes new readers safe against
		stale values from old writers."""
		from optimus import session

		cache = _FakeCache()
		frappe_stub = _make_frappe_stub(cache)
		uuid = "test-uuid-read-legacy"
		# Pre-v0.12.21 writers stored bare dicts.
		legacy_payload = {"session_uuid": uuid, "user": "bob", "label": "from-legacy"}
		cache.store[session._meta_key(uuid)] = legacy_payload

		with mock.patch.object(session, "frappe", frappe_stub):
			result = session.get_session_meta(uuid)

		assert result == legacy_payload, (
			"legacy bare-dict cache values must resolve unchanged so "
			"new readers don't drop pre-v0.12.21 session metadata"
		)

	def test_get_session_meta_returns_none_for_missing_key(self):
		"""Cache miss → None (no crash)."""
		from optimus import session

		cache = _FakeCache()
		frappe_stub = _make_frappe_stub(cache)

		with mock.patch.object(session, "frappe", frappe_stub):
			assert session.get_session_meta("never-written") is None

	def test_get_session_meta_returns_none_for_corrupt_non_dict(self):
		"""Defensive guard — if a corrupt write stored a non-dict /
		non-envelope value, get_session_meta returns None rather than
		passing it through to callers (which would crash on
		``.get(...)``)."""
		from optimus import session

		cache = _FakeCache()
		frappe_stub = _make_frappe_stub(cache)
		uuid = "test-uuid-corrupt"
		# A raw string corrupt value (not a dict, not an envelope).
		cache.store[session._meta_key(uuid)] = "corrupted-not-a-dict"

		with mock.patch.object(session, "frappe", frappe_stub):
			result = session.get_session_meta(uuid)
		# unwrap_value passes the string through (legacy branch); the
		# defensive isinstance(dict) check in get_session_meta then
		# normalises to None.
		assert result is None, (
			"non-dict corrupt cache value should normalise to None, not propagate to callers"
		)
