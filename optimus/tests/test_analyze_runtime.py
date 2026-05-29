# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Runtime smoothing for analyze (v0.7.x).

M5: lower the analyze worker's CPU priority (os.nice) so a heavy analyze
doesn't starve live web traffic — gated to the async path (sticky per-process).
M6: optionally throttle the EXPLAIN/sqlparse burst in _enrich_recordings so the
DB/CPU aren't hammered continuously. Both default to inert/near-zero and are
result-invariant.
"""

import inspect
import os
import types

import frappe
import pytest

from optimus import analyze

# ---------------------------------------------------------------------------
# M5 — os.nice
# ---------------------------------------------------------------------------

class TestApplyNice:
	def test_calls_os_nice_with_conf_value(self, monkeypatch):
		monkeypatch.setattr(frappe, "conf",
			types.SimpleNamespace(get=lambda k, d=None: 7 if k == "optimus_analyze_nice" else d),
			raising=False)
		recorded = []
		monkeypatch.setattr(os, "nice", lambda inc: recorded.append(inc))
		analyze._apply_nice()
		assert recorded == [7]

	def test_zero_is_noop(self, monkeypatch):
		monkeypatch.setattr(frappe, "conf",
			types.SimpleNamespace(get=lambda k, d=None: 0), raising=False)
		recorded = []
		monkeypatch.setattr(os, "nice", lambda inc: recorded.append(inc))
		analyze._apply_nice()
		assert recorded == []

	def test_swallows_oserror(self, monkeypatch):
		monkeypatch.setattr(frappe, "conf",
			types.SimpleNamespace(get=lambda k, d=None: 5), raising=False)
		def boom(inc):
			raise PermissionError("nope")
		monkeypatch.setattr(os, "nice", boom)
		analyze._apply_nice()  # must not raise

	def test_run_applies_nice_gated_to_async(self):
		src = inspect.getsource(analyze.run)
		assert "_apply_nice" in src
		# Must be guarded by the inline check (os.nice is sticky per-process).
		assert "is_scheduler_disabled" in src


# ---------------------------------------------------------------------------
# M6 — enrich throttle
# ---------------------------------------------------------------------------

class FakeCache:
	def __init__(self):
		self.store = {}

	def get_value(self, k):
		return self.store.get(k)

	def set_value(self, k, v, expires_in_sec=None):
		self.store[k] = v

	def delete_value(self, k):
		self.store.pop(k, None)


def _recording(queries):
	return {"uuid": "r", "calls": [{"query": q, "duration": 1} for q in queries]}


@pytest.fixture
def enrich_env(monkeypatch):
	"""Fakes so _enrich_recordings runs without a bench: every distinct query
	shape issues one fake EXPLAIN; sleeps and conf knobs are observable."""
	explain_queries = []
	slept = []

	class FakeDB:
		def sql(self, q, as_dict=False):
			explain_queries.append(q)
			return [{"id": 1}]

	conf_values = {"optimus_explain_cache_ttl_seconds": 0}  # shared cache off

	monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "cache", FakeCache(), raising=False)
	monkeypatch.setattr(frappe, "conf",
		types.SimpleNamespace(get=lambda k, d=None: conf_values.get(k, d)), raising=False)
	monkeypatch.setattr(analyze, "is_query_type", lambda q, t: True, raising=False)
	monkeypatch.setattr(analyze, "mark_duplicates", lambda rec: None, raising=False)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None, raising=False)
	monkeypatch.setattr(analyze.time, "sleep", lambda s: slept.append(s))

	import optimus.settings as settings_mod
	monkeypatch.setattr(settings_mod, "get_config",
		lambda: types.SimpleNamespace(max_queries_per_recording=2000), raising=False)

	return types.SimpleNamespace(
		explain_queries=explain_queries, slept=slept, conf=conf_values,
	)


# Five distinct query shapes → five EXPLAINs.
_FIVE = [f"SELECT * FROM tab{i} WHERE x = {i}" for i in range(5)]


class TestEnrichThrottle:
	def test_disabled_by_default_never_sleeps(self, enrich_env):
		analyze._enrich_recordings([_recording(_FIVE)])
		assert len(enrich_env.explain_queries) == 5
		assert enrich_env.slept == []

	def test_sleeps_every_n_explains(self, enrich_env):
		enrich_env.conf["optimus_enrich_throttle_every"] = 2
		enrich_env.conf["optimus_enrich_throttle_sleep_ms"] = 5
		analyze._enrich_recordings([_recording(_FIVE)])
		# 5 EXPLAINs, sleep after the 2nd and 4th → 2 sleeps of ~5ms.
		assert len(enrich_env.slept) == 2
		assert all(abs(s - 0.005) < 1e-9 for s in enrich_env.slept)

	def test_no_sleep_on_cache_hits(self, enrich_env):
		enrich_env.conf["optimus_enrich_throttle_every"] = 1
		# Same shape three times → only ONE real EXPLAIN → one sleep, not three.
		same = ["SELECT * FROM tabFoo WHERE x = 1"] * 3
		analyze._enrich_recordings([_recording(same)])
		assert len(enrich_env.explain_queries) == 1
		assert len(enrich_env.slept) == 1

	def test_results_invariant_to_throttle(self, enrich_env):
		off = _recording(_FIVE)
		analyze._enrich_recordings([off])
		results_off = [c.get("explain_result") for c in off["calls"]]

		enrich_env.conf["optimus_enrich_throttle_every"] = 2
		on = _recording(_FIVE)
		analyze._enrich_recordings([on])
		results_on = [c.get("explain_result") for c in on["calls"]]

		assert results_off == results_on


class TestMaxQueriesPerRecordingZeroIsUnlimited:
	"""v0.13.x: ``max_queries_per_recording = 0`` (Strict preset) means
	no cap — every query gets enriched. Pre-v0.13.x the ``if cap <= 0:
	cap = DEFAULT`` reset silently re-applied the 2000 default, so the
	operator's "I want full coverage" intent was clobbered."""

	def test_zero_cap_does_not_truncate_large_recording(self, enrich_env, monkeypatch):
		# Override the fixture's max_queries default.
		import optimus.settings as settings_mod
		monkeypatch.setattr(
			settings_mod, "get_config",
			lambda: types.SimpleNamespace(max_queries_per_recording=0),
			raising=False,
		)

		# A recording with 3000 queries (way over the legacy 2000 default).
		# With cap = 0, every one must be enriched — nothing truncated.
		big = _recording([
			f"SELECT * FROM tab{i} WHERE x = {i}" for i in range(3000)
		])
		warnings = analyze._enrich_recordings([big])

		# All 3000 calls remain in the recording (no slice off).
		assert len(big["calls"]) == 3000
		# No truncation warning emitted.
		truncation_warnings = [w for w in warnings if "TRUNCATED" in w]
		assert truncation_warnings == [], (
			f"cap = 0 must not truncate, got {truncation_warnings!r}"
		)

	def test_positive_cap_still_truncates(self, enrich_env, monkeypatch):
		"""Sanity: a positive cap still truncates as before. Guards
		against an over-eager rewrite that accidentally unbounded
		every deployment."""
		import optimus.settings as settings_mod
		monkeypatch.setattr(
			settings_mod, "get_config",
			lambda: types.SimpleNamespace(max_queries_per_recording=100),
			raising=False,
		)

		big = _recording([
			f"SELECT * FROM tab{i} WHERE x = {i}" for i in range(250)
		])
		warnings = analyze._enrich_recordings([big])

		# Sliced to the cap.
		assert len(big["calls"]) == 100
		truncation_warnings = [w for w in warnings if "TRUNCATED" in w]
		assert truncation_warnings, "positive cap must emit a truncation warning"
