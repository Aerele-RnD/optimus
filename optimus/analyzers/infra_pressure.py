# optimus/analyzers/infra_pressure.py
# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: server infrastructure pressure (v0.5.0).

Reads per-recording infra snapshots written by
hooks_callbacks.after_request/after_job (stored under
profiler:infra:<recording_uuid>). analyze.run injects each recording's
infra dict as recording["infra"] before calling this analyzer, so the
analyzer itself is pure.

Emits four finding types:
  - Resource Contention        (system CPU sustained > CPU_HIGH_PCT)
  - Memory Pressure            (worker RSS growth or swap active)
  - DB Pool Saturation         (threads_running / threads_connected > 0.9)
  - Background Queue Backlog   (any RQ queue depth > RQ_BACKLOG_WARN)
"""

import json

from optimus.analyzers.base import SEVERITY_ORDER, AnalyzerResult

# Thresholds — tunable via site_config.json: optimus_infra_*
DEFAULT_CPU_HIGH_PCT = 85
DEFAULT_CPU_CRITICAL_PCT = 95
DEFAULT_RSS_DELTA_HIGH_MB = 200
DEFAULT_RSS_DELTA_CRITICAL_MB = 500
DEFAULT_SWAP_WARN_MB = 100
DEFAULT_DB_POOL_HIGH_RATIO = 0.9
DEFAULT_RQ_BACKLOG_WARN = 50
MIN_ACTIONS_AFFECTED = 2


def _conf(key: str, default):
    try:
        import frappe

        v = frappe.conf.get(key)
        if v is not None:
            return v
    except Exception:
        pass
    return default


def analyze(recordings: list[dict], context) -> AnalyzerResult:
    cpu_high = _conf("optimus_infra_cpu_high_pct", DEFAULT_CPU_HIGH_PCT)
    cpu_crit = _conf("optimus_infra_cpu_critical_pct", DEFAULT_CPU_CRITICAL_PCT)
    rss_high = _conf("optimus_infra_rss_delta_high_mb", DEFAULT_RSS_DELTA_HIGH_MB) * 1_000_000
    rss_crit = _conf("optimus_infra_rss_delta_critical_mb", DEFAULT_RSS_DELTA_CRITICAL_MB) * 1_000_000
    swap_warn = _conf("optimus_infra_swap_warn_mb", DEFAULT_SWAP_WARN_MB) * 1_000_000
    pool_high = _conf("optimus_infra_db_pool_high_ratio", DEFAULT_DB_POOL_HIGH_RATIO)
    rq_warn = _conf("optimus_infra_rq_backlog_warn", DEFAULT_RQ_BACKLOG_WARN)

    # v0.5.2: per_action builds humanized action_labels on
    # context.actions, NOT on the raw recording dict. Pre-v0.5.2 the
    # infra timeline read rec.get("action_label") — which is always
    # None on real recordings — so every row in the "Server Resource"
    # table rendered as the synthetic "action_0", "action_1", ...
    # fallback. Same bug pattern the frontend_timings analyzer had
    # earlier; same fix: read from context.actions[idx].action_label
    # when available, fall back to the recording's own method/path
    # for a readable last-resort, only produce synthetic "action_N"
    # if all else fails.
    ctx_actions = getattr(context, "actions", None) or []

    def _label_for(idx: int) -> str:
        if idx < len(ctx_actions):
            lbl = ctx_actions[idx].get("action_label")
            if lbl:
                return lbl
        if idx < len(recordings):
            r = recordings[idx]
            method = r.get("method") or ""
            path = (r.get("path") or "").split("?", 1)[0]
            if path:
                return f"{method} {path}".strip()
            cmd = r.get("cmd")
            if cmd:
                return str(cmd)
        return f"action_{idx}"

    timeline = []
    cpu_values = []
    load_values = []
    rss_values = []
    swap_values = []
    rq_peaks = {"default": 0, "short": 0, "long": 0}

    cpu_breaches = []
    cpu_critical = False
    swap_active = False
    db_pool_breaches = []
    rq_backlog_breaches = []

    actions_with_infra = 0

    for idx, rec in enumerate(recordings):
        infra = rec.get("infra") or {}
        # Defensive: a truthy non-dict value (e.g. corrupt Redis data
        # returning a list or string) would pass the falsy check but
        # then crash on .get(). Skip non-dicts cleanly.
        if not infra or not isinstance(infra, dict):
            continue
        actions_with_infra += 1

        cpu = _num(infra.get("sys_cpu_percent"))
        rss = _num(infra.get("worker_rss_bytes"))
        load = _num(infra.get("sys_load_avg_1min"))
        swap = _num(infra.get("sys_swap_used_bytes"))
        dbc = _num(infra.get("db_threads_connected"))
        dbr = _num(infra.get("db_threads_running"))
        db_max = _num(infra.get("db_max_connections"))
        rq_def = _num(infra.get("rq_queue_default")) or 0
        rq_short = _num(infra.get("rq_queue_short")) or 0
        rq_long = _num(infra.get("rq_queue_long")) or 0

        timeline.append({
            "action_idx": idx,
            "action_label": _label_for(idx),
            "cpu": cpu,
            "rss": rss,
            "load_1min": load,
            "swap": swap,
            "db_threads_running": dbr,
            "db_threads_connected": dbc,
            "db_max_connections": db_max,
            "rq_default": rq_def,
            "rq_short": rq_short,
            "rq_long": rq_long,
        })

        if cpu is not None:
            cpu_values.append(cpu)
            if cpu > cpu_high:
                cpu_breaches.append(idx)
                if cpu >= cpu_crit:
                    cpu_critical = True
        if rss is not None:
            rss_values.append(rss)
        if load is not None:
            load_values.append(load)
        if swap is not None:
            swap_values.append(swap)
            if swap > swap_warn:
                swap_active = True

        # DB pool saturation: ratio of open connections to max_connections.
        # When max_connections is unknown (pre-v0.5.0 infra blobs or a DB
        # that refused the SHOW VARIABLES), fall back to the weaker
        # threads_running/threads_connected proxy rather than skipping —
        # the fallback is noisier but still catches the obvious cases.
        if db_max and db_max > 0 and dbc is not None:
            ratio = dbc / db_max
            if ratio > pool_high:
                db_pool_breaches.append(idx)
        elif dbc and dbr is not None and dbc > 0:
            # Legacy fallback for pre-v0.5.1 infra blobs without db_max_connections.
            ratio = dbr / dbc
            if ratio > pool_high:
                db_pool_breaches.append(idx)

        # RQ backlog: any queue above the warning threshold this action.
        for qname, val in (("default", rq_def), ("short", rq_short), ("long", rq_long)):
            if val > rq_peaks[qname]:
                rq_peaks[qname] = val
            if val > rq_warn and idx not in rq_backlog_breaches:
                rq_backlog_breaches.append(idx)

    findings = []

    # ---- Resource Contention --------------------------------------------
    if len(cpu_breaches) >= MIN_ACTIONS_AFFECTED:
        total_actions = max(actions_with_infra, 1)
        pct_affected = len(cpu_breaches) / total_actions
        if cpu_critical or pct_affected > 0.5:
            severity = "High"
        elif pct_affected >= 0.2:
            severity = "Medium"
        else:
            severity = "Low"

        findings.append({
            "finding_type": "Resource Contention",
            "severity": severity,
            "title": f"System CPU > {cpu_high}% on {len(cpu_breaches)} of {total_actions} actions",
            "customer_description": (
                f"The server's CPU was above {cpu_high}% during "
                f"{len(cpu_breaches)} of {total_actions} steps in your flow "
                f"(peak {max(cpu_values):.0f}%). This usually means the box is "
                "overloaded — either your own flow is CPU-bound, or another "
                "process on the server is competing for CPU while you profile."
            ),
            "technical_detail_json": json.dumps({
                "breached_action_indices": cpu_breaches,
                "cpu_peak": max(cpu_values),
                "cpu_avg": sum(cpu_values) / len(cpu_values),
                "threshold_pct": cpu_high,
                "critical_threshold_pct": cpu_crit,
                "fix_hint": (
                    "If your own code is hot (check the Call Tree / N+1 "
                    "findings), optimize there first. If the call tree looks "
                    "idle but CPU is still high, something else on the server "
                    "is using CPU — look at other workers, cron jobs, or a "
                    "noisy neighbor."
                ),
            }, default=str),
            "estimated_impact_ms": 0,
            "affected_count": len(cpu_breaches),
            "action_ref": str(cpu_breaches[0]),
        })

    # ---- Memory Pressure -------------------------------------------------
    mem_delta = (rss_values[-1] - rss_values[0]) if len(rss_values) >= 2 else 0
    # Also consider the max intra-session delta in case the session ended
    # lower than it peaked (e.g. gc after a spike).
    max_intra_delta = 0
    if rss_values:
        base = rss_values[0]
        for v in rss_values:
            if v - base > max_intra_delta:
                max_intra_delta = v - base
    effective_delta = max(mem_delta, max_intra_delta)

    if effective_delta > rss_high or swap_active:
        if effective_delta > rss_crit or swap_active:
            severity = "High"
        else:
            severity = "Medium"
        findings.append({
            "finding_type": "Memory Pressure",
            "severity": severity,
            "title": (
                f"Worker memory grew by {effective_delta / 1_000_000:.0f}MB during session"
                + (" (swap active)" if swap_active else "")
            ),
            "customer_description": (
                f"Worker memory (RSS) grew by {effective_delta / 1_000_000:.0f}MB "
                f"during this session"
                + (". Swap was active on the host, which slows everything "
                   "down significantly." if swap_active else ".")
                + " Growing memory usually indicates cached docs or query "
                "results piling up per request."
            ),
            "technical_detail_json": json.dumps({
                "rss_delta_bytes": mem_delta,
                "max_intra_session_delta_bytes": max_intra_delta,
                "rss_start_bytes": rss_values[0] if rss_values else None,
                "rss_end_bytes": rss_values[-1] if rss_values else None,
                "swap_active": swap_active,
                "swap_peak_bytes": max(swap_values) if swap_values else 0,
                "fix_hint": (
                    "Investigate cache growth (frappe.cache, doc caches) and "
                    "long-lived object references. If swap is active, the box "
                    "is undersized — add RAM or move the profiled workload."
                ),
            }, default=str),
            "estimated_impact_ms": 0,
            "affected_count": actions_with_infra,
            "action_ref": "0",
        })

    # ---- DB Pool Saturation ---------------------------------------------
    if len(db_pool_breaches) >= MIN_ACTIONS_AFFECTED:
        findings.append({
            "finding_type": "DB Pool Saturation",
            "severity": "High",
            "title": f"MariaDB running/connected ratio > {pool_high} on {len(db_pool_breaches)} actions",
            "customer_description": (
                "MariaDB had nearly all of its active connections executing "
                "queries during parts of your flow. This usually means too "
                "many concurrent requests for the connection pool size. "
                "Consider raising max_connections or reducing worker count."
            ),
            "technical_detail_json": json.dumps({
                "breached_action_indices": db_pool_breaches,
                "threshold_ratio": pool_high,
                "fix_hint": (
                    "Raise MariaDB max_connections, or reduce Frappe gunicorn "
                    "workers to match. The pool should have headroom."
                ),
            }, default=str),
            "estimated_impact_ms": 0,
            "affected_count": len(db_pool_breaches),
            "action_ref": str(db_pool_breaches[0]),
        })

    # ---- Background Queue Backlog ----------------------------------------
    if rq_backlog_breaches:
        max_peak = max(rq_peaks.values())
        severity = "Medium" if max_peak > 50 else "Low"
        findings.append({
            "finding_type": "Background Queue Backlog",
            "severity": severity,
            "title": f"RQ queue depth peaked at {max_peak} during session",
            "customer_description": (
                f"An RQ Job queue had more than {rq_warn} pending jobs "
                f"during your flow (peak {max_peak}). If your flow enqueues "
                "work, it's waiting behind other jobs."
            ),
            "technical_detail_json": json.dumps({
                "peaks": rq_peaks,
                "breached_action_indices": rq_backlog_breaches,
                "fix_hint": (
                    "Check whether your worker count matches expected load. "
                    "Consider dedicated queues for high-priority work."
                ),
            }, default=str),
            "estimated_impact_ms": 0,
            "affected_count": len(rq_backlog_breaches),
            "action_ref": str(rq_backlog_breaches[0]),
        })

    findings.sort(
        key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["affected_count"])
    )

    summary = {
        "cpu_avg": (sum(cpu_values) / len(cpu_values)) if cpu_values else 0,
        "cpu_peak": max(cpu_values) if cpu_values else 0,
        "rss_start": rss_values[0] if rss_values else 0,
        "rss_end": rss_values[-1] if rss_values else 0,
        "rss_delta": mem_delta,
        "load_peak": max(load_values) if load_values else 0,
        "swap_peak_mb": (max(swap_values) // 1_000_000) if swap_values else 0,
        "rq_peak_depth": rq_peaks,
    }

    return AnalyzerResult(
        findings=findings,
        aggregate={
            "infra_timeline": timeline,
            "infra_summary": summary,
        },
    )


def _num(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
