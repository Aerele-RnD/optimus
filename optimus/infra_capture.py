# optimus/infra_capture.py
# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Server-side infra capture primitives (v0.5.0).

Balanced-tier metric set (14 keys, ~0.8ms per snapshot). Called from
hooks_callbacks.before_request/before_job/after_request/after_job to
take snapshots before and after each recorded action. The diff is
stored under ``profiler:infra:<recording_uuid>`` and consumed by the
infra_pressure analyzer at analyze time.

Design invariants:
- Best-effort. A broken metric source must degrade to None, never raise.
- No background threads. All work happens on the calling request path.
- No capture state persists past force_stop. _force_stop_inflight clears
  ``frappe.local.optimus_infra_start``, mirroring how
  ``capture._force_stop_inflight_capture`` clears pyinstrument state.
"""

import os

import frappe

# Counter-style metrics — diff() subtracts start from end to produce deltas.
# Everything else is a gauge that passes through as the end value.
_COUNTER_KEYS = {
    "db_slow_queries_total",
}

_EXPECTED_KEYS = (
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
)

# max_connections is a MariaDB server config and doesn't change between
# snapshots during a session. Cache at module level on first read so we
# don't pay the SHOW VARIABLES cost on every snapshot.
_db_max_connections_cached: int | None = None

# psutil.cpu_percent(interval=None) returns 0.0 on first call because it
# has no baseline to diff against. Prime it once per worker the first
# time snapshot() runs so subsequent calls return real values.
_cpu_primed = False


def snapshot() -> dict:
    """Return a point-in-time dict of all Balanced-tier infra metrics.

    ~0.8ms total cost on a typical Linux/Mac host. Every key in
    ``_EXPECTED_KEYS`` is present in the returned dict; failed sources
    yield None values.
    """
    out = {k: None for k in _EXPECTED_KEYS}
    try:
        _read_process(out)
    except Exception:
        frappe.log_error(title="optimus infra_capture process")
    try:
        _read_system(out)
    except Exception:
        frappe.log_error(title="optimus infra_capture system")
    try:
        _read_loadavg(out)
    except Exception:
        frappe.log_error(title="optimus infra_capture loadavg")
    try:
        _read_db(out)
    except Exception:
        frappe.log_error(title="optimus infra_capture db")
    try:
        _read_redis(out)
    except Exception:
        frappe.log_error(title="optimus infra_capture redis")
    try:
        _read_rq(out)
    except Exception:
        frappe.log_error(title="optimus infra_capture rq")
    return out


def diff(start: dict, end: dict) -> dict:
    """Compute per-action metrics from two snapshots.

    Counter-style metrics (see ``_COUNTER_KEYS``) are subtracted; everything
    else passes through as the end value. Negative deltas (which should
    never happen but can on counter rollover or a source going backwards)
    clamp to 0.
    """
    out = {}
    for key in _EXPECTED_KEYS:
        end_val = end.get(key) if end else None
        if key in _COUNTER_KEYS:
            start_val = start.get(key) if start else None
            if end_val is None or start_val is None:
                out[key] = None
            else:
                delta = end_val - start_val
                out[key] = delta if delta >= 0 else 0
        else:
            out[key] = end_val
    return out


def _force_stop_inflight(local_proxy) -> None:
    """Clear any ``frappe.local.optimus_infra_start`` so a killed session's
    start snapshot can't leak into the next session on the same worker.
    Idempotent. Called from api._stop_session (v0.5.0)."""
    for attr in ("optimus_infra_start",):
        if hasattr(local_proxy, attr):
            try:
                delattr(local_proxy, attr)
            except AttributeError:
                pass


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------


def _read_process(out: dict) -> None:
    import psutil

    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    out["worker_rss_bytes"] = int(mem.rss)
    out["worker_vms_bytes"] = int(mem.vms)


def _read_system(out: dict) -> None:
    global _cpu_primed
    import psutil

    if not _cpu_primed:
        try:
            psutil.cpu_percent(interval=None)
        finally:
            _cpu_primed = True
    out["sys_cpu_percent"] = float(psutil.cpu_percent(interval=None))

    vm = psutil.virtual_memory()
    out["sys_mem_available_bytes"] = int(vm.available)
    out["sys_mem_total_bytes"] = int(vm.total)

    swap = psutil.swap_memory()
    out["sys_swap_used_bytes"] = int(swap.used)


def _read_loadavg(out: dict) -> None:
    try:
        load = os.getloadavg()
        out["sys_load_avg_1min"] = float(load[0])
    except (AttributeError, OSError):
        # Windows lacks getloadavg; OSError can happen in containers
        # without /proc/loadavg.
        out["sys_load_avg_1min"] = None


def _read_db(out: dict) -> None:
    global _db_max_connections_cached

    rows = frappe.db.sql(
        "SHOW GLOBAL STATUS WHERE Variable_name IN "
        "('Threads_connected', 'Threads_running', 'Slow_queries')"
    )
    status = {name: value for name, value in rows}
    out["db_threads_connected"] = _to_int(status.get("Threads_connected"))
    out["db_threads_running"] = _to_int(status.get("Threads_running"))
    out["db_slow_queries_total"] = _to_int(status.get("Slow_queries"))

    # MariaDB pool size. Cached at module level because max_connections
    # is a server config value — doesn't change between snapshots within
    # a session. Cheap SHOW VARIABLES on first read, free thereafter.
    if _db_max_connections_cached is None:
        try:
            var_rows = frappe.db.sql(
                "SHOW VARIABLES WHERE Variable_name = 'max_connections'"
            )
            if var_rows:
                _db_max_connections_cached = _to_int(var_rows[0][1])
        except Exception:
            pass
    out["db_max_connections"] = _db_max_connections_cached


def _read_redis(out: dict) -> None:
    # frappe.cache IS a redis.Redis subclass (RedisWrapper at
    # frappe/utils/redis_wrapper.py:38). There is no `.redis` child
    # attribute — call info() directly on frappe.cache. An earlier
    # v0.5.0 version used `getattr(frappe.cache, "redis", None)` which
    # silently returned None in production (tests masked the bug
    # because the FakeCache mock matched the broken code exactly).
    info = frappe.cache.info("stats") or {}
    ops = info.get("instantaneous_ops_per_sec")
    if ops is not None:
        out["redis_instantaneous_ops_per_sec"] = int(ops)


def _read_rq(out: dict) -> None:
    import rq

    # frappe.cache is the redis client directly (see _read_redis comment).
    # rq.Queue accepts a redis.Redis instance as its `connection` arg.
    for name in ("default", "short", "long"):
        try:
            q = rq.Queue(name, connection=frappe.cache)
            out[f"rq_queue_{name}"] = int(q.count)
        except Exception:
            out[f"rq_queue_{name}"] = None


def _to_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
