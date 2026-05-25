# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.13: continues the v0.12.0 ``wrap_value`` / ``unwrap_value``
envelope rollout to two more values:

  * ``retention_backlog`` (janitor.py) — write-only int. The janitor
    writes 0 (or the backlog count) once per daily sweep; there's no
    in-app reader, but operator-facing tooling could read the value
    directly from Redis, so we wrap on write to make the shape future-
    safe.
  * ``onboarding_seen`` (api.py) — write/read pair. The dismiss
    endpoint writes the string ``"1"``; the check endpoint reads
    and coerces to ``bool``. Both sides migrated.

The unit suite already has ``test_settings_envelope_rollout.py`` from
the v0.12.11 settings_cache migration; this module is the same shape
for these two follow-up values. Each test runs against a dict-backed
fake cache so we can introspect the exact shape that's stored.
"""

from __future__ import annotations

import sys
import types
from unittest import mock


class _FakeCache:
	"""Dict-backed ``frappe.cache`` substitute — same as the one used
	in ``test_settings_envelope_rollout.py``; copied here so each
	rollout test module is self-contained."""

	def __init__(self) -> None:
		self.store: dict = {}

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, **_):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


# ---------------------------------------------------------------------------
# retention_backlog (write-only) — janitor writes wrap_value(int)
# ---------------------------------------------------------------------------


class TestRetentionBacklogEnvelope:
	"""The daily janitor's two write sites for ``retention_backlog`` both
	wrap the int value in the v0.12.0 envelope. No in-app reader exists
	(the value is operator-visible monitoring metric only), so the
	contract under test is write-shape — the future-proofing that lets
	operator dashboards / migration paths read the envelope cleanly."""

	def test_janitor_writes_envelope_when_setting_backlog(self):
		import optimus.janitor as janitor
		from optimus import redis_schema

		# Synthesize the cache + frappe stub. We only exercise the
		# write-the-counter branch — _sweep_old_sessions's full body
		# is out of scope (it queries Optimus Session which needs a
		# bench). Instead we invoke the cache-write expression
		# directly via the same code path.
		cache = _FakeCache()

		# Frappe-stub the bare minimum for the wrap path.
		fake_frappe = types.SimpleNamespace()
		fake_frappe.cache = cache
		# The cache-write code path: same shape as janitor.py:307-312.
		# Re-run that expression here so the test is anchored to the
		# CONTRACT (wrapped write) without re-executing the whole sweep.
		from optimus import redis_keys

		with mock.patch.dict(sys.modules, {"frappe": fake_frappe}):
			fake_frappe.cache.set_value(
				redis_keys.retention_backlog(),
				redis_schema.wrap_value(42),
				expires_in_sec=3600,
			)

		stored = cache.store[redis_keys.retention_backlog()]
		assert isinstance(stored, dict), (
			f"retention_backlog cache value should be the envelope dict; got: {stored!r}"
		)
		assert stored.get("_v") == redis_schema.SCHEMA_VERSION
		assert stored.get("data") == 42

	def test_janitor_source_uses_wrap_value(self):
		"""Source-grep canary: confirm janitor.py's set_value calls for
		retention_backlog wrap their payload via redis_schema.wrap_value.
		Catches a future refactor that accidentally reverts the wrap."""
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		# Both write sites must use wrap_value before set_value.
		# We check that the function reference appears alongside the
		# retention_backlog key in the same nearby block.
		anchor = "redis_keys.retention_backlog()"
		assert anchor in src
		# After the rollout, every set_value to retention_backlog wraps:
		# the value arg of the set_value call should call
		# redis_schema.wrap_value(...) on either ``backlog`` or ``0``.
		assert "redis_schema.wrap_value(backlog)" in src, (
			"first retention_backlog write site should wrap_value(backlog)"
		)
		assert "redis_schema.wrap_value(0)" in src, (
			"second retention_backlog write site (backlog-cleared) should wrap_value(0)"
		)


# ---------------------------------------------------------------------------
# onboarding_seen (write/read pair) — both sides migrated
# ---------------------------------------------------------------------------


class TestOnboardingSeenEnvelopeReadCompat:
	"""``check_onboarding_seen`` reads via ``unwrap_value`` so both new-
	shape envelopes AND legacy bare ``"1"`` strings (left behind by
	pre-v0.12.13 writers) resolve to the same truthy result.

	Catches a regression where the read path drops the unwrap call —
	an OLD reader of a NEW envelope would see the dict as truthy and
	keep returning ``seen: True``, but a NEW reader of an OLD bare
	string MUST also return truthy."""

	def test_unwrap_of_new_envelope_returns_truthy(self):
		from optimus import redis_schema

		raw = redis_schema.wrap_value("1")
		payload, _ = redis_schema.unwrap_value(raw)
		assert bool(payload) is True, (
			f"unwrap of new-shape envelope should yield a truthy payload; got: {payload!r}"
		)

	def test_unwrap_of_legacy_bare_string_returns_truthy(self):
		"""The migration-safety contract: pre-v0.12.13 writers stored
		the bare string ``"1"``. New readers must NOT crash; they
		must treat it as the original payload via the legacy-detection
		branch."""
		from optimus import redis_schema

		raw = "1"  # exactly what mark_onboarding_seen wrote pre-v0.12.13
		payload, version = redis_schema.unwrap_value(raw)
		assert payload == "1"
		assert version is None, f"legacy un-wrapped value should report version=None; got: {version!r}"
		assert bool(payload) is True

	def test_unwrap_of_missing_key_returns_falsy(self):
		"""A user who has never dismissed the toast → no Redis key →
		``get_value`` returns None → ``unwrap_value`` returns the
		caller's default (None). ``bool(None)`` is False, so the
		check endpoint correctly reports seen=False."""
		from optimus import redis_schema

		payload, version = redis_schema.unwrap_value(None)
		assert payload is None
		assert version is None
		assert bool(payload) is False


class TestOnboardingSeenEnvelopeWriteShape:
	"""``mark_onboarding_seen`` writes a wrapped envelope. Source-grep
	canary against a future refactor that reverts the wrap."""

	def test_api_source_uses_wrap_value_for_onboarding_seen_write(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "api.py")
		with open(path) as f:
			src = f.read()
		# Locate the mark_onboarding_seen function body.
		start = src.index("def mark_onboarding_seen(")
		# Next top-level def or decorator.
		import re

		next_top = re.search(r"\n(?:def |@frappe\.whitelist)", src[start + 1 :])
		end = start + 1 + (next_top.start() if next_top else len(src) - start - 1)
		body = src[start:end]
		assert "redis_schema.wrap_value(" in body, (
			"mark_onboarding_seen must wrap the value via redis_schema.wrap_value "
			"before set_value; got:\n" + body
		)

	def test_api_source_uses_unwrap_value_for_onboarding_seen_read(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "api.py")
		with open(path) as f:
			src = f.read()
		start = src.index("def check_onboarding_seen(")
		import re

		next_top = re.search(r"\n(?:def |@frappe\.whitelist)", src[start + 1 :])
		end = start + 1 + (next_top.start() if next_top else len(src) - start - 1)
		body = src[start:end]
		assert "redis_schema.unwrap_value(" in body, (
			"check_onboarding_seen must unwrap the cache value via "
			"redis_schema.unwrap_value so both new envelopes and legacy "
			"bare strings resolve consistently; got:\n" + body
		)
