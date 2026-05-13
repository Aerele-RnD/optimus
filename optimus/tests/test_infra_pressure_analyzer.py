# optimus/tests/test_infra_pressure_analyzer.py
# Copyright (c) 2026, Optimus contributors

"""Tests for v0.5.0 infra_pressure analyzer."""

import json
import os

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_session():
    with open(os.path.join(FIXTURES_DIR, "infra_pressure_session.json")) as f:
        return json.load(f)


def _empty_context():
    from optimus.analyzers.base import AnalyzeContext
    return AnalyzeContext(session_uuid="test", docname="test")


def _synth_infra(**overrides):
    """Return a minimal infra dict with every Balanced-tier key set to a
    non-breaching value, then apply overrides."""
    base = {
        "sys_cpu_percent": 20,
        "worker_rss_bytes": 500_000_000,
        "sys_swap_used_bytes": 0,
        "sys_mem_available_bytes": 1_000_000_000,
        "sys_mem_total_bytes": 16_000_000_000,
        "sys_load_avg_1min": 1.0,
        "db_threads_connected": 5,
        "db_threads_running": 1,
        "db_slow_queries_total": 0,
        "redis_instantaneous_ops_per_sec": 100,
        "rq_queue_default": 0,
        "rq_queue_short": 0,
        "rq_queue_long": 0,
    }
    base.update(overrides)
    return base


def test_resource_contention_fires_on_sustained_cpu():
    from optimus.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    findings = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert len(findings) == 1
    assert findings[0]["severity"] in ("High", "Medium")
    # 2 of 3 actions breached CPU_HIGH_PCT (92, 88 > 85); 35 is fine.
    assert findings[0]["affected_count"] == 2


def test_memory_pressure_does_not_fire_on_small_delta():
    """The fixture session has 520M→680M→520M RSS. End-start delta is 0
    and the intermediate spike (160M) is below the 200MB threshold. Swap
    peaks at 50MB which is below the 100MB warn threshold. So neither
    arm of Memory Pressure should fire."""
    from optimus.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    mp = [f for f in result.findings if f["finding_type"] == "Memory Pressure"]
    assert mp == []


def test_memory_pressure_fires_on_large_rss_delta():
    """Synthetic recordings where RSS grows by 300MB — must fire Medium
    (delta > 200MB threshold but < 500MB critical)."""
    from optimus.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(worker_rss_bytes=400_000_000)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(worker_rss_bytes=700_000_000)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    mp = [f for f in result.findings if f["finding_type"] == "Memory Pressure"]
    assert len(mp) == 1
    assert mp[0]["severity"] == "Medium"


def test_memory_pressure_fires_high_on_swap():
    """Any swap above the warn threshold fires High severity."""
    from optimus.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(sys_swap_used_bytes=200_000_000)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_swap_used_bytes=200_000_000)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    mp = [f for f in result.findings if f["finding_type"] == "Memory Pressure"]
    assert len(mp) == 1
    assert mp[0]["severity"] == "High"


def test_db_pool_saturation_does_not_fire_below_threshold():
    """The fixture recordings have no `db_max_connections` set, which
    triggers the legacy fallback ratio (threads_running/threads_connected).
    Max is 10/15 ≈ 0.67, below the 0.9 threshold, so no finding."""
    from optimus.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    pool = [f for f in result.findings if f["finding_type"] == "DB Pool Saturation"]
    assert pool == []


def test_db_pool_saturation_fires_on_pool_exhaustion():
    """When db_max_connections is set and db_threads_connected is close
    to it, the new (correct) ratio fires. 145/151 = 0.96 > 0.9."""
    from optimus.analyzers import infra_pressure

    recordings = [
        {
            "uuid": "r1",
            "action_label": "a",
            "infra": _synth_infra(
                db_threads_connected=145,
                db_threads_running=30,
                db_max_connections=151,
            ),
        },
        {
            "uuid": "r2",
            "action_label": "b",
            "infra": _synth_infra(
                db_threads_connected=148,
                db_threads_running=25,
                db_max_connections=151,
            ),
        },
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    pool = [f for f in result.findings if f["finding_type"] == "DB Pool Saturation"]
    assert len(pool) == 1
    assert pool[0]["severity"] == "High"


def test_db_pool_saturation_healthy_pool_no_false_positive():
    """5 connections open against a 500-slot pool is 1% usage. The
    old proxy (threads_running/threads_connected) would misfire here
    if all 5 were busy; the new proxy (connected/max) correctly ignores
    it."""
    from optimus.analyzers import infra_pressure

    recordings = [
        {
            "uuid": "r1",
            "action_label": "a",
            "infra": _synth_infra(
                db_threads_connected=5,
                db_threads_running=5,  # all current connections busy
                db_max_connections=500,  # but pool has plenty of room
            ),
        },
        {
            "uuid": "r2",
            "action_label": "b",
            "infra": _synth_infra(
                db_threads_connected=5,
                db_threads_running=5,
                db_max_connections=500,
            ),
        },
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    pool = [f for f in result.findings if f["finding_type"] == "DB Pool Saturation"]
    assert pool == [], "Healthy pool (1% usage) should not fire DB Pool Saturation"


def test_aggregate_includes_timeline_and_summary():
    from optimus.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    agg = result.aggregate
    assert "infra_timeline" in agg
    assert "infra_summary" in agg
    assert len(agg["infra_timeline"]) == 3
    assert agg["infra_summary"]["cpu_peak"] == 92.0
    assert agg["infra_summary"]["cpu_avg"] == pytest.approx((92.0 + 88.0 + 35.0) / 3, abs=0.01)
    assert agg["infra_summary"]["load_peak"] == 5.1
    assert agg["infra_summary"]["rq_peak_depth"]["default"] == 8


def test_min_actions_affected_guard():
    """A single spiky action must not fire Resource Contention on its own."""
    from optimus.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(sys_cpu_percent=95)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_cpu_percent=20)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert rc == []


def test_severity_escalates_on_critical_cpu():
    from optimus.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(sys_cpu_percent=97)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_cpu_percent=99)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert len(rc) == 1
    assert rc[0]["severity"] == "High"


def test_empty_recordings_is_safe():
    from optimus.analyzers import infra_pressure

    result = infra_pressure.analyze([], _empty_context())
    assert result.findings == []
    assert result.aggregate["infra_timeline"] == []


def test_recordings_with_non_dict_infra_are_skipped():
    """Pass-5 regression guard: a corrupt Redis blob or unexpected
    data type that ends up as rec['infra'] as a list/string (instead
    of dict) would pass the falsy check but then crash on .get(),
    breaking analyze.run for the whole session.
    """
    from optimus.analyzers import infra_pressure

    recordings = [
        # Non-dict truthy values that the old code would have crashed on.
        {"uuid": "r1", "action_label": "a", "infra": ["not", "a", "dict"]},
        {"uuid": "r2", "action_label": "b", "infra": "definitely not a dict"},
        {"uuid": "r3", "action_label": "c", "infra": _synth_infra(sys_cpu_percent=95)},
    ]
    # Must NOT raise.
    result = infra_pressure.analyze(recordings, _empty_context())
    # Only the one valid recording made it into the timeline.
    assert len(result.aggregate["infra_timeline"]) == 1
    # And no Resource Contention because only one action breached
    # (MIN_ACTIONS_AFFECTED = 2).
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert rc == []


def test_recordings_without_infra_are_ignored():
    """Not every recording has an infra dict (e.g. if the session was
    started before v0.5.0 rolled out). The analyzer should skip them
    cleanly rather than crashing."""
    from optimus.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a"},  # no infra
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_cpu_percent=95)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    # Only one action had infra — no sustained-breach finding possible.
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert rc == []
    # Timeline should only include the action with infra.
    assert len(result.aggregate["infra_timeline"]) == 1



# ---------------------------------------------------------------------------
# v0.5.2: infra_timeline action labels from context.actions
# ---------------------------------------------------------------------------
# Pre-v0.5.2 this analyzer read rec.get("action_label") from the raw
# recording dict, but per_action._build_action writes action_label on
# context.actions[idx], NOT on the recording. So every row in the
# Server Resource table rendered as the synthetic "action_0",
# "action_1", ... fallback. Same bug shape as frontend_timings.


def _production_shape_recording(idx, **overrides):
    """Real recorder output shape: path/cmd/method, NO action_label
    on the dict."""
    base = {
        "uuid": f"rec-{idx}",
        "path": "/api/method/frappe.desk.form.save.savedocs",
        "cmd": "frappe.desk.form.save.savedocs",
        "method": "POST",
        "event_type": "HTTP Request",
        "duration": 100.0,
        "calls": [],
        "infra": _synth_infra(),
    }
    base.update(overrides)
    return base


def test_infra_timeline_uses_context_actions_for_labels():
    """Exact production shape: recordings have no action_label, but
    context.actions (populated by per_action.analyze upstream) does.
    Timeline rows must pull from there."""
    from optimus.analyzers import infra_pressure
    from optimus.analyzers.base import AnalyzeContext

    recordings = [
        _production_shape_recording(0),
        _production_shape_recording(1),
    ]
    ctx = AnalyzeContext(session_uuid="t", docname="t")
    ctx.actions = [
        {"action_label": "frappe.desk.form.save.savedocs:Save", "duration_ms": 100},
        {"action_label": "run_doc_method:make_payment_entry", "duration_ms": 100},
    ]
    result = infra_pressure.analyze(recordings, ctx)
    timeline = result.aggregate["infra_timeline"]
    labels = [row["action_label"] for row in timeline]
    assert labels == [
        "frappe.desk.form.save.savedocs:Save",
        "run_doc_method:make_payment_entry",
    ], f"Expected humanized labels; got: {labels}"
    assert not any(lbl.startswith("action_") for lbl in labels), (
        f"synthetic action_N labels leaked into the Server Resource "
        f"table: {labels}"
    )


def test_infra_timeline_falls_back_to_method_path_without_context():
    """If context.actions is empty (per_action didn't run first, or the
    actions list is shorter than recordings), derive a readable label
    from the recording's own method + path rather than emitting the
    synthetic 'action_N' noise."""
    from optimus.analyzers import infra_pressure
    from optimus.analyzers.base import AnalyzeContext

    rec = _production_shape_recording(
        0,
        path="/api/resource/Sales Invoice/SI-001",
        cmd="",
        method="GET",
    )
    ctx = AnalyzeContext(session_uuid="t", docname="t")
    # Deliberately leave ctx.actions empty.
    result = infra_pressure.analyze([rec], ctx)
    timeline = result.aggregate["infra_timeline"]
    assert timeline[0]["action_label"] == (
        "GET /api/resource/Sales Invoice/SI-001"
    ), f"Expected METHOD+path fallback; got: {timeline[0]}"


def test_infra_timeline_synthetic_only_as_last_resort():
    """If the recording truly has no method/path/cmd AND no matching
    context.actions entry, fall back to synthetic 'action_N'. This
    is the only path that should produce the old noise — and only
    when the analyzer has zero useful data to work with."""
    from optimus.analyzers import infra_pressure
    from optimus.analyzers.base import AnalyzeContext

    rec = {
        "uuid": "bare",
        "event_type": "HTTP Request",
        "duration": 50.0,
        "calls": [],
        "infra": _synth_infra(),
        # no method, no path, no cmd, no action_label
    }
    ctx = AnalyzeContext(session_uuid="t", docname="t")
    result = infra_pressure.analyze([rec], ctx)
    timeline = result.aggregate["infra_timeline"]
    assert timeline[0]["action_label"] == "action_0"
