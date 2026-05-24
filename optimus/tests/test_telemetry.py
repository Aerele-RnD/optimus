# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.8.0 opt-in failure telemetry module.

Pure-pytest — no live Frappe site needed. The emit hot path has zero
Frappe imports, so it's exercised directly. The flush path is exercised
with a minimal frappe stub installed via ``sys.modules`` (matching the
pattern in ``test_profiler_settings_validation.py``) so the DocType + JSONL
sinks can be observed without a bench.

Invariants under test (mirror the Risks + Mitigations in the plan):

  * emit is bounded (maxlen=500) and lock-free; thread-safe under
    concurrent emits from 4 threads.
  * Signatures dedupe by ``(event_name, exception class, last 5
    optimus-or-collapsed frames)``.
  * Path scrub never leaks: optimus frames keep their relative path;
    non-optimus frames collapse to ``<user_code>:LINE``.
  * Settings clamp ``telemetry_retention_days`` to its floor (1).
  * Janitor sweep is a no-op when telemetry is disabled (avoids
    deleting rows from an older operator who had it briefly enabled).
"""

import sys
import threading
import types
from datetime import datetime, timedelta

import pytest

from optimus import telemetry

# --------------------------------------------------------------------------
# Buffer hygiene — every test starts with an empty buffer to keep state local.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_buffer():
	telemetry.drain_for_test()
	yield
	telemetry.drain_for_test()


# --------------------------------------------------------------------------
# Emit + buffer
# --------------------------------------------------------------------------


class TestEmit:
	def test_emit_appends_to_buffer(self):
		telemetry.emit_failure("test.basic")
		drained = telemetry.drain_for_test()
		assert len(drained) == 1
		assert drained[0]["event_name"] == "test.basic"
		assert drained[0]["severity"] == "error"

	def test_emit_bounded_at_maxlen(self):
		# Append 600 unique events; only the last 500 should survive.
		for i in range(600):
			telemetry.emit_failure(f"test.bound.{i}")
		drained = telemetry.drain_for_test()
		assert len(drained) == telemetry._BUFFER_MAXLEN
		# The first 100 should have been evicted; the last record should
		# be the highest-numbered emit.
		assert drained[-1]["event_name"] == "test.bound.599"

	def test_emit_with_exc_none(self):
		telemetry.emit_failure("test.no_exc", None, context={"k": "v"})
		drained = telemetry.drain_for_test()
		assert len(drained) == 1
		assert drained[0]["traceback"] == ""
		assert drained[0]["exc_type"] == ""
		assert drained[0]["context"] == {"k": "v"}

	def test_emit_truncates_long_event_name(self):
		long_name = "x" * (telemetry._MAX_EVENT_NAME + 50)
		telemetry.emit_failure(long_name)
		drained = telemetry.drain_for_test()
		assert len(drained) == 1
		assert len(drained[0]["event_name"]) == telemetry._MAX_EVENT_NAME

	def test_emit_silently_drops_bad_inputs(self):
		# Non-string event_name → silent drop, no exception, no row.
		telemetry.emit_failure(None)  # type: ignore[arg-type]
		telemetry.emit_failure("")
		telemetry.emit_failure(42)  # type: ignore[arg-type]
		assert telemetry.drain_for_test() == []

	def test_emit_thread_safe_under_concurrency(self):
		# Four threads each emit 100 events; the buffer ends up with
		# at most maxlen entries and the process never crashes.
		def worker(tag: str) -> None:
			for i in range(100):
				telemetry.emit_failure(f"thread.{tag}.{i}")

		threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b", "c", "d")]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		drained = telemetry.drain_for_test()
		# 400 emits, maxlen=500 — all of them fit.
		assert len(drained) == 400


# --------------------------------------------------------------------------
# Signature dedup
# --------------------------------------------------------------------------


def _raise_in(filename_hint: str, lineno_hint: int) -> ValueError:
	"""Raise ValueError so the caller can capture .__traceback__. The
	caller controls the frame the exception is raised from."""
	try:
		raise ValueError(f"raised from {filename_hint}:{lineno_hint}")
	except ValueError as exc:
		return exc


class TestSignature:
	def test_signature_is_16_hex_chars(self):
		sig = telemetry._make_signature("ev", None)
		assert len(sig) == 16
		assert all(c in "0123456789abcdef" for c in sig)

	def test_signature_stable_for_same_event_no_exc(self):
		s1 = telemetry._make_signature("ev.x", None)
		s2 = telemetry._make_signature("ev.x", None)
		assert s1 == s2

	def test_signature_distinct_for_different_event_names(self):
		s1 = telemetry._make_signature("ev.a", None)
		s2 = telemetry._make_signature("ev.b", None)
		assert s1 != s2

	def test_signature_distinct_for_different_exception_types(self):
		# Two exceptions raised from the same call site, different types
		# → different signatures (the exception class is part of the hash).
		def _v():
			try:
				raise ValueError("v")
			except ValueError as e:
				return e

		def _t():
			try:
				raise TypeError("t")
			except TypeError as e:
				return e

		s1 = telemetry._make_signature("ev", _v())
		s2 = telemetry._make_signature("ev", _t())
		assert s1 != s2


# --------------------------------------------------------------------------
# Scrub — traceback
# --------------------------------------------------------------------------


_SAMPLE_OPTIMUS_FRAME = '  File "/Users/dev/bench/apps/optimus/optimus/foo.py", line 42, in bar'
_SAMPLE_USER_FRAME = '  File "/Users/dev/bench/apps/myapp/myapp/utils.py", line 17, in do_thing'
_SAMPLE_DEPS_FRAME = '  File "/usr/lib/python3.12/site-packages/somelib/x.py", line 9, in helper'


class TestScrubTraceback:
	def test_empty_input_returns_empty_string(self):
		assert telemetry._scrub_traceback("") == ""
		assert telemetry._scrub_traceback(None) == ""  # type: ignore[arg-type]

	def test_optimus_frame_kept_with_bench_prefix(self):
		out = telemetry._scrub_traceback(_SAMPLE_OPTIMUS_FRAME)
		assert "<bench>/apps/optimus/optimus/foo.py" in out
		assert "line 42" in out
		assert "in bar" in out
		# Original absolute prefix must be gone.
		assert "/Users/dev/bench" not in out

	def test_user_app_frame_collapses_to_user_code(self):
		out = telemetry._scrub_traceback(_SAMPLE_USER_FRAME)
		assert out.strip() == "<user_code>:17"
		# Original path AND function name must be gone.
		assert "myapp" not in out
		assert "do_thing" not in out

	def test_stdlib_frame_collapses_to_deps(self):
		out = telemetry._scrub_traceback(_SAMPLE_DEPS_FRAME)
		assert "<deps>/x.py" in out
		assert "line 9" in out
		assert "/usr/lib" not in out

	def test_truncates_oversize_traceback(self):
		# Build a traceback well over 8 KB so the truncation kicks in.
		big = "\n".join(_SAMPLE_OPTIMUS_FRAME for _ in range(1000))
		out = telemetry._scrub_traceback(big)
		assert len(out) <= telemetry._MAX_TB_BYTES
		assert out.endswith("[truncated]")

	def test_non_file_lines_scrub_embedded_paths(self):
		# A chained-exception preamble or "ValueError: ..." line that
		# happens to contain a bench path should still get scrubbed.
		raw = "During handling: /Users/x/bench/apps/optimus/file.py:1 woke up"
		out = telemetry._scrub_traceback(raw)
		assert "<bench>/apps/optimus" in out
		assert "/Users/x/bench" not in out


# --------------------------------------------------------------------------
# Scrub — context
# --------------------------------------------------------------------------


class TestScrubContext:
	def test_drops_keys_beyond_cap(self):
		ctx = {f"k{i}": i for i in range(20)}
		out = telemetry._scrub_context(ctx)
		assert len(out) == telemetry._MAX_CTX_KEYS

	def test_caps_value_length(self):
		ctx = {"long": "x" * 1000}
		out = telemetry._scrub_context(ctx)
		assert len(out["long"]) == telemetry._MAX_CTX_VALUE_CHARS + len("...")
		assert out["long"].endswith("...")

	def test_drops_none_values(self):
		out = telemetry._scrub_context({"a": None, "b": 1, "c": None, "d": "x"})
		assert "a" not in out
		assert "c" not in out
		assert out == {"b": "1", "d": "x"}

	def test_non_dict_input_returns_empty(self):
		assert telemetry._scrub_context(None) == {}
		assert telemetry._scrub_context([]) == {}
		assert telemetry._scrub_context("garbage") == {}

	def test_coerces_values_to_str(self):
		out = telemetry._scrub_context({"n": 42, "f": 1.5, "b": True})
		assert out == {"n": "42", "f": "1.5", "b": "True"}


# --------------------------------------------------------------------------
# Flush — exercised with a minimal frappe stub
# --------------------------------------------------------------------------


def _install_flush_stub(
	monkeypatch,
	*,
	cfg,
):
	"""Install a fresh ``frappe`` module stub for the flush() lazy
	import. Returns the SQL collector (list of (query, params) tuples)
	so tests can assert what was written.

	The cfg arg is the OptimusConfig the flush should resolve.
	"""
	calls: list[tuple[str, dict | tuple]] = []
	log_errors: list[str] = []

	class _DB:
		def sql(self, query, params=None):
			calls.append((query, params))

	stub = types.ModuleType("frappe")
	stub.__version__ = "fake-15.0.0"
	stub.db = _DB()

	def _log_error(title=None, **kwargs):
		log_errors.append(title or "")

	stub.log_error = _log_error
	stub.utils = types.SimpleNamespace(get_bench_path=lambda: "/tmp/fake_bench")

	monkeypatch.setitem(sys.modules, "frappe", stub)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg, raising=True)
	return calls, log_errors, stub


def _cfg(**over):
	defaults = dict(
		telemetry_enabled=False,
		telemetry_sink_doctype=True,
		telemetry_sink_jsonl_file=False,
		telemetry_endpoint_url="",
		telemetry_retention_days=30,
	)
	defaults.update(over)
	return types.SimpleNamespace(**defaults)


class TestFlush:
	def test_no_op_when_master_off(self, monkeypatch):
		calls, _, _ = _install_flush_stub(monkeypatch, cfg=_cfg(telemetry_enabled=False))
		telemetry.emit_failure("ev.a")
		telemetry.emit_failure("ev.b")
		wrote = telemetry.flush()
		assert wrote == 0
		# Buffer is drained either way (so a future toggle-on doesn't
		# unleash a backlog), but nothing was written.
		assert calls == []
		# And the buffer is now empty.
		assert telemetry.drain_for_test() == []

	def test_writes_doctype_when_enabled(self, monkeypatch):
		calls, _, _ = _install_flush_stub(monkeypatch, cfg=_cfg(telemetry_enabled=True))
		telemetry.emit_failure("ev.a")
		wrote = telemetry.flush()
		assert wrote == 1
		assert len(calls) == 1
		query, params = calls[0]
		assert "INSERT INTO `tabOptimus Telemetry Event`" in query
		assert "ON DUPLICATE KEY UPDATE" in query
		assert params["event_name"] == "ev.a"
		assert params["count"] == 1

	def test_groups_by_signature(self, monkeypatch):
		calls, _, _ = _install_flush_stub(monkeypatch, cfg=_cfg(telemetry_enabled=True))
		# Five emits with the same event_name + exc=None → one signature
		# → one INSERT with count=5.
		for _ in range(5):
			telemetry.emit_failure("ev.repeat")
		telemetry.flush()
		assert len(calls) == 1
		_, params = calls[0]
		assert params["count"] == 5
		assert params["event_name"] == "ev.repeat"

	def test_writes_jsonl_when_enabled(self, monkeypatch, tmp_path):
		calls, _, stub = _install_flush_stub(
			monkeypatch,
			cfg=_cfg(telemetry_enabled=True, telemetry_sink_doctype=False, telemetry_sink_jsonl_file=True),
		)
		# Point the bench path at the per-test tmp dir.
		stub.utils.get_bench_path = lambda: str(tmp_path)
		telemetry.emit_failure("ev.jsonl")
		telemetry.flush()
		jsonl = tmp_path / "logs" / "optimus_telemetry.jsonl"
		assert jsonl.exists()
		lines = jsonl.read_text().strip().splitlines()
		assert len(lines) == 1
		import json

		payload = json.loads(lines[0])
		assert payload["event_name"] == "ev.jsonl"
		assert payload["count"] == 1

	def test_swallows_doctype_sql_failure(self, monkeypatch):
		# DocType sink raises on insert; flush() must not propagate.
		stub = types.ModuleType("frappe")
		stub.__version__ = "x"
		log_errors: list[str] = []

		class _DB:
			def sql(self, query, params=None):
				raise RuntimeError("kaboom")

		stub.db = _DB()
		stub.log_error = lambda title=None, **k: log_errors.append(title or "")
		stub.utils = types.SimpleNamespace(get_bench_path=lambda: "/tmp/x")
		monkeypatch.setitem(sys.modules, "frappe", stub)
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(telemetry_enabled=True),
			raising=True,
		)
		telemetry.emit_failure("ev.boom")
		# Must not raise.
		telemetry.flush()


# --------------------------------------------------------------------------
# Settings clamp — the new retention floor
# --------------------------------------------------------------------------


def _settings_stub(monkeypatch):
	"""Mirror the stub from test_profiler_settings_validation.py."""
	stub = types.ModuleType("frappe")
	stub.msgprint = lambda *a, **k: None
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: None,
		get_value=lambda k: None,
		set_value=lambda k, v: None,
	)
	stub.log_error = lambda **k: None

	model_mod = types.ModuleType("frappe.model")
	doc_mod = types.ModuleType("frappe.model.document")

	class Document:
		def __init__(self, **kwargs):
			for k, v in kwargs.items():
				setattr(self, k, v)

		def get(self, k, default=None):
			return getattr(self, k, default)

	doc_mod.Document = Document
	monkeypatch.setitem(sys.modules, "frappe", stub)
	monkeypatch.setitem(sys.modules, "frappe.model", model_mod)
	monkeypatch.setitem(sys.modules, "frappe.model.document", doc_mod)
	for mod in list(sys.modules.keys()):
		if mod.startswith("optimus.optimus.doctype.optimus_settings"):
			monkeypatch.delitem(sys.modules, mod, raising=False)
	from optimus.optimus.doctype.optimus_settings.optimus_settings import (
		OptimusSettings,
	)

	return OptimusSettings


class TestSettings:
	def test_telemetry_defaults_in_optimus_config(self):
		from optimus import settings as s

		cfg = s.OptimusConfig()
		assert cfg.telemetry_enabled is False
		assert cfg.telemetry_sink_doctype is True
		assert cfg.telemetry_sink_jsonl_file is False
		assert cfg.telemetry_endpoint_url == ""
		assert cfg.telemetry_retention_days == 30

	def test_retention_days_floor_clamps_to_one(self, monkeypatch):
		OptimusSettings = _settings_stub(monkeypatch)
		doc = OptimusSettings()
		doc.telemetry_retention_days = 0
		doc._clamp_numeric_floors()
		assert doc.telemetry_retention_days == 1

	def test_retention_days_floor_clamps_negative(self, monkeypatch):
		OptimusSettings = _settings_stub(monkeypatch)
		doc = OptimusSettings()
		doc.telemetry_retention_days = -5
		doc._clamp_numeric_floors()
		assert doc.telemetry_retention_days == 1

	def test_retention_days_floor_leaves_valid_values_alone(self, monkeypatch):
		OptimusSettings = _settings_stub(monkeypatch)
		doc = OptimusSettings()
		doc.telemetry_retention_days = 30
		doc._clamp_numeric_floors()
		assert doc.telemetry_retention_days == 30


# --------------------------------------------------------------------------
# Janitor — opt-in retention sweep
# --------------------------------------------------------------------------


class TestJanitorRetention:
	def _install_janitor_stub(self, monkeypatch, *, cfg):
		calls: list[tuple[str, tuple]] = []

		class _DB:
			def sql(self, query, params=None):
				calls.append((query, params))

			def commit(self):
				pass

			def rollback(self):
				pass

		stub = types.ModuleType("frappe")
		stub.db = _DB()
		stub.log_error = lambda **k: None
		stub.utils = types.SimpleNamespace(
			get_bench_path=lambda: "/tmp",
			add_to_date=__import__("frappe.utils", fromlist=["add_to_date"]).add_to_date
			if "frappe.utils" in sys.modules
			else None,
		)
		monkeypatch.setitem(sys.modules, "frappe", stub)
		monkeypatch.setattr("optimus.settings.get_config", lambda: cfg, raising=True)
		# Janitor pulls add_to_date / now_datetime from frappe.utils at module
		# top — we patch those names directly on the janitor module.
		import optimus.janitor as jm

		monkeypatch.setattr(jm, "frappe", stub, raising=True)
		monkeypatch.setattr(jm, "add_to_date", lambda d, days: d + timedelta(days=days), raising=True)
		monkeypatch.setattr(jm, "now_datetime", lambda: datetime(2026, 5, 24, 12, 0, 0), raising=True)
		monkeypatch.setattr(jm, "safe_commit", lambda: None, raising=True)
		return calls, jm

	def test_sweep_no_op_when_telemetry_disabled(self, monkeypatch):
		calls, jm = self._install_janitor_stub(monkeypatch, cfg=_cfg(telemetry_enabled=False))
		jm._sweep_old_telemetry()
		assert calls == []

	def test_sweep_calls_delete_when_enabled(self, monkeypatch):
		calls, jm = self._install_janitor_stub(
			monkeypatch,
			cfg=_cfg(telemetry_enabled=True, telemetry_retention_days=30),
		)
		jm._sweep_old_telemetry()
		assert len(calls) == 1
		query, params = calls[0]
		assert "DELETE FROM `tabOptimus Telemetry Event`" in query
		assert "last_seen" in query
		assert "LIMIT" in query

	def test_sweep_respects_retention_days(self, monkeypatch):
		calls, jm = self._install_janitor_stub(
			monkeypatch,
			cfg=_cfg(telemetry_enabled=True, telemetry_retention_days=7),
		)
		jm._sweep_old_telemetry()
		assert len(calls) == 1
		_, params = calls[0]
		cutoff, limit = params
		# Cutoff = now (2026-05-24) - 7 days = 2026-05-17.
		assert cutoff == datetime(2026, 5, 17, 12, 0, 0)
		assert limit == jm.MAX_DELETIONS_PER_RUN
