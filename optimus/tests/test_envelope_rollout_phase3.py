# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.17: continues the v0.12.0 ``wrap_value`` / ``unwrap_value``
envelope rollout to ``explain_cache`` (the cross-session EXPLAIN-
result cache in ``analyze.py``).

The previous phases shipped settings_cache (v0.12.11),
retention_backlog + onboarding_seen (v0.12.13). This phase adds
``explain_cache``, whose payload is a list of dicts (EXPLAIN row
results) — the most complex shape rolled out so far. Exercising the
envelope on a non-primitive collection validates that ``unwrap_value``
preserves nested structure cleanly.
"""

from __future__ import annotations


class TestExplainCacheEnvelopeRoundTrip:
	"""End-to-end on the envelope helpers alone — the analyze.py call
	site uses the same two-line pattern (wrap on write, unwrap on
	read), so a unit test on the helpers + a source-grep canary
	together cover the rollout."""

	def test_list_of_dicts_roundtrip(self):
		"""EXPLAIN results are list[dict] — wrap + unwrap must preserve
		the shape, including the inner dict's value types."""
		from optimus import redis_schema

		payload = [
			{"id": 1, "select_type": "SIMPLE", "table": "tabUser", "rows": 100},
			{"id": 2, "select_type": "SIMPLE", "table": "tabSession", "rows": 5},
		]
		wrapped = redis_schema.wrap_value(payload)
		unwrapped, version = redis_schema.unwrap_value(wrapped)
		assert unwrapped == payload, f"list-of-dicts payload should round-trip unchanged; got: {unwrapped!r}"
		assert version == redis_schema.SCHEMA_VERSION

	def test_empty_list_roundtrip(self):
		"""An EXPLAIN query against a query that fails returns []; the
		empty-list shape must round-trip."""
		from optimus import redis_schema

		wrapped = redis_schema.wrap_value([])
		unwrapped, version = redis_schema.unwrap_value(wrapped)
		assert unwrapped == []
		assert version == redis_schema.SCHEMA_VERSION

	def test_legacy_bare_list_passes_through(self):
		"""Pre-v0.12.17 cached EXPLAIN results are stored as bare
		list[dict] with no envelope. New readers must accept the
		legacy shape unchanged via ``unwrap_value``'s legacy-detection
		branch."""
		from optimus import redis_schema

		legacy_payload = [
			{"select_type": "SIMPLE", "table": "tabRole", "rows": 50},
		]
		unwrapped, version = redis_schema.unwrap_value(legacy_payload)
		assert unwrapped == legacy_payload, (
			"legacy bare-list shape must pass through unchanged so "
			"new readers don't drop pre-v0.12.17 cached EXPLAIN results"
		)
		assert version is None, f"legacy un-wrapped value should report version=None; got: {version!r}"


class TestAnalyzeSourceUsesEnvelope:
	"""Source-grep canary: confirm analyze.py's explain_cache write +
	read both use the envelope helpers. Catches a future refactor that
	accidentally reverts the wrap / unwrap."""

	def test_explain_cache_write_wraps_via_redis_schema(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "analyze.py")
		with open(path) as f:
			src = f.read()
		# Locate the explain_cache shared-cache write block. Anchor on
		# the shared_key variable that the explain branch builds, then
		# look for wrap_value nearby.
		assert "_redis_keys.explain_cache(" in src, (
			"analyze.py should reference _redis_keys.explain_cache for the shared-cache key"
		)
		# The set_value call must wrap its payload via _redis_schema.
		assert "_redis_schema.wrap_value(result)" in src, (
			"explain_cache write must wrap the EXPLAIN result via _redis_schema.wrap_value before set_value"
		)

	def test_explain_cache_read_unwraps_via_redis_schema(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "analyze.py")
		with open(path) as f:
			src = f.read()
		assert "_redis_schema.unwrap_value(raw_cached)" in src, (
			"explain_cache read must unwrap the cached value via "
			"_redis_schema.unwrap_value so both new envelopes and legacy "
			"bare lists resolve consistently"
		)
