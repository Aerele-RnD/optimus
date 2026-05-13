# optimus/tests/test_infra_capture.py
# Copyright (c) 2026, Optimus contributors

"""Tests for v0.5.0 server infra capture primitives."""

import sys
import types


def test_redis_source_uses_frappe_cache_directly(monkeypatch):
	"""Regression guard (v0.5.1 architect review): frappe.cache IS a
	redis.Redis subclass (RedisWrapper), not a wrapper with a .redis
	child attribute. An earlier version of _read_redis used
	getattr(frappe.cache, 'redis', None) which silently returned None
	in production, disabling Redis metrics entirely. The production
	code must call .info() directly on frappe.cache.

	We check by actually running _read_redis against a stub that
	rejects the broken access pattern — more robust than source-string
	matching which can match explanatory comments.
	"""
	import frappe

	from optimus import infra_capture

	# Use instance state (not a class attribute) so parallel or
	# repeated test runs don't leak state through the class.
	class Tripwire:
		"""A stand-in for frappe.cache that records info() being called
		on the root object (good) and fires on .redis access (broken)."""

		def __init__(self):
			self.info_called_on_root = False

		def info(self, section=None):
			self.info_called_on_root = True
			return {"instantaneous_ops_per_sec": 99}

		def __getattr__(self, name):
			if name == "redis":
				raise AssertionError(
					"_read_redis must not access frappe.cache.redis — "
					"frappe.cache IS the redis.Redis instance. Call "
					"frappe.cache.info() directly."
				)
			raise AttributeError(name)

	# Use monkeypatch for automatic cleanup even on interpreter
	# errors or keyboard interrupts mid-test.
	tripwire = Tripwire()
	monkeypatch.setattr(frappe, "cache", tripwire, raising=False)

	out = {"redis_instantaneous_ops_per_sec": None}
	infra_capture._read_redis(out)
	assert tripwire.info_called_on_root, (
		"_read_redis never called frappe.cache.info() — the metric "
		"is silently missing from every production snapshot"
	)
	assert out["redis_instantaneous_ops_per_sec"] == 99


def test_rq_source_uses_frappe_cache_directly():
	"""Companion guard for _read_rq — it must pass frappe.cache as the
	rq.Queue connection, not getattr(frappe.cache, 'redis', None) which
	would pass None and fall through to rq's default connection logic."""
	import inspect

	from optimus import infra_capture

	read_rq_src = inspect.getsource(infra_capture._read_rq)
	# Strip docstring + comments for a more robust code-only check.
	code_lines = [
		ln for ln in read_rq_src.splitlines()
		if ln.strip() and not ln.strip().startswith("#")
	]
	code_only = "\n".join(code_lines)

	# Must pass frappe.cache directly as the connection arg.
	assert "connection=frappe.cache" in code_only, (
		"_read_rq must pass frappe.cache as rq.Queue(connection=...)"
	)


def test_snapshot_returns_expected_keys(monkeypatch):
    """snapshot() should return a dict with every Balanced-tier metric key,
    each with a numeric or None value. Missing values (e.g. getloadavg on
    Windows, unreachable DB) must degrade to None, never raise.
    """
    _install_infra_stubs(monkeypatch)
    from optimus import infra_capture

    snap = infra_capture.snapshot()

    expected_keys = {
        "worker_rss_bytes",
        "worker_vms_bytes",
        "sys_cpu_percent",
        "sys_mem_available_bytes",
        "sys_mem_total_bytes",
        "sys_swap_used_bytes",
        "sys_load_avg_1min",
        "db_threads_connected",
        "db_threads_running",
        "db_max_connections",
        "db_slow_queries_total",
        "redis_instantaneous_ops_per_sec",
        "rq_queue_default",
        "rq_queue_short",
        "rq_queue_long",
    }
    assert expected_keys.issubset(snap.keys())
    for k, v in snap.items():
        assert v is None or isinstance(v, (int, float)), f"{k}={v!r} not numeric"


def test_diff_treats_counters_as_deltas():
    """diff() must subtract counter-style metrics (slow_queries_total) so
    the per-action value is the delta, not the cumulative total. Gauges
    (cpu_percent, rss_bytes) must use the end value directly."""
    from optimus import infra_capture

    start = {
        "sys_cpu_percent": 40.0,
        "worker_rss_bytes": 500_000_000,
        "db_slow_queries_total": 100,
        "sys_load_avg_1min": 1.0,
    }
    end = {
        "sys_cpu_percent": 78.0,
        "worker_rss_bytes": 520_000_000,
        "db_slow_queries_total": 103,
        "sys_load_avg_1min": 1.6,
    }
    d = infra_capture.diff(start, end)

    assert d["sys_cpu_percent"] == 78.0  # gauge → end value
    assert d["worker_rss_bytes"] == 520_000_000  # gauge → end value
    assert d["sys_load_avg_1min"] == 1.6  # gauge → end value
    assert d["db_slow_queries_total"] == 3  # counter → delta

    # Clamp negative counter deltas (caused by counter rollover or the
    # source going backwards) to zero rather than surfacing noise.
    negative_end = {**end, "db_slow_queries_total": 99}
    d2 = infra_capture.diff(start, negative_end)
    assert d2["db_slow_queries_total"] == 0


def test_snapshot_handles_missing_getloadavg(monkeypatch):
    """On systems without os.getloadavg (Windows), the key must be present
    and None, not missing and not raising AttributeError."""
    _install_infra_stubs(monkeypatch)

    import os as os_mod

    def raising_getloadavg():
        raise AttributeError("getloadavg not available")

    monkeypatch.setattr(os_mod, "getloadavg", raising_getloadavg)

    from optimus import infra_capture

    snap = infra_capture.snapshot()
    assert "sys_load_avg_1min" in snap
    assert snap["sys_load_avg_1min"] is None


def test_snapshot_is_defensive_against_psutil_errors(monkeypatch):
    """If psutil or any downstream call raises, snapshot() must still
    return a dict with the expected keys (None values), not propagate."""
    _install_infra_stubs(monkeypatch, break_psutil=True)
    from optimus import infra_capture

    snap = infra_capture.snapshot()
    assert isinstance(snap, dict)
    assert "worker_rss_bytes" in snap


def test_force_stop_clears_local_start():
    from optimus import infra_capture

    class FakeLocal:
        pass

    local = FakeLocal()
    # Set as instance attribute to match how frappe.local (werkzeug Local
    # proxy) actually stores per-request values.
    local.optimus_infra_start = {"sys_cpu_percent": 42}
    assert hasattr(local, "optimus_infra_start")

    infra_capture._force_stop_inflight(local)
    assert not hasattr(local, "optimus_infra_start")


def test_force_stop_idempotent():
    """Calling force_stop when the attribute was never set must not raise."""
    from optimus import infra_capture

    class FakeLocal:
        pass

    local = FakeLocal()
    infra_capture._force_stop_inflight(local)  # must not raise
    assert not hasattr(local, "optimus_infra_start")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _install_infra_stubs(monkeypatch, break_psutil=False):
    """Install deterministic stubs for every external call in snapshot().

    Keeps the tests runnable without a real psutil/Frappe/MariaDB/Redis.
    Also resets the infra_capture module-level cache so max_connections
    and cpu_primed state from a previous test don't bleed through.
    """
    # Reset module-level caches so each test starts fresh.
    from optimus import infra_capture as _ic
    monkeypatch.setattr(_ic, "_db_max_connections_cached", None, raising=False)
    monkeypatch.setattr(_ic, "_cpu_primed", False, raising=False)
    # ---- psutil stub ------------------------------------------------------
    if break_psutil:
        fake_psutil = types.ModuleType("psutil")

        def _raise(*a, **k):
            raise RuntimeError("psutil broken for test")

        fake_psutil.Process = _raise
        fake_psutil.cpu_percent = _raise
        fake_psutil.virtual_memory = _raise
        fake_psutil.swap_memory = _raise
    else:
        fake_psutil = types.ModuleType("psutil")

        class FakeMemInfo:
            rss = 512_000_000
            vms = 1_024_000_000

        class FakeProc:
            def memory_info(self):
                return FakeMemInfo()

        class FakeVM:
            available = 4_000_000_000
            total = 16_000_000_000

        class FakeSwap:
            used = 0

        fake_psutil.Process = lambda *a, **k: FakeProc()
        fake_psutil.cpu_percent = lambda *a, **k: 45.0
        fake_psutil.virtual_memory = lambda: FakeVM()
        fake_psutil.swap_memory = lambda: FakeSwap()

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    # ---- frappe stubs -----------------------------------------------------
    import frappe

    class FakeDB:
        def sql(self, query, *args, **kwargs):
            # Return SHOW STATUS-style two-column rows based on query hint.
            if "Threads_" in query or "Slow_queries" in query:
                return [
                    ("Threads_connected", "5"),
                    ("Threads_running", "2"),
                    ("Slow_queries", "42"),
                ]
            if "max_connections" in query:
                return [("max_connections", "151")]
            return []

    # frappe.cache IS a redis.Redis subclass in production (RedisWrapper),
    # not a wrapper with a .redis child. The stub mirrors this — info()
    # is a method directly on the cache instance, not on a child object.
    class FakeCache:
        def info(self, section=None):
            return {"instantaneous_ops_per_sec": 120}

    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(frappe, "cache", FakeCache(), raising=False)
    monkeypatch.setattr(
        frappe, "log_error",
        lambda *a, **k: None,
        raising=False,
    )

    # ---- rq stub ----------------------------------------------------------
    fake_rq = types.ModuleType("rq")

    class FakeQueue:
        def __init__(self, name, connection=None):
            self.name = name
            self.count = {"default": 2, "short": 0, "long": 1}.get(name, 0)

    fake_rq.Queue = FakeQueue
    monkeypatch.setitem(sys.modules, "rq", fake_rq)
