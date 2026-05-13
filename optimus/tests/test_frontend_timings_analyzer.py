# optimus/tests/test_frontend_timings_analyzer.py
# Copyright (c) 2026, Optimus contributors

"""Tests for v0.5.0 frontend_timings analyzer."""

import json
import os

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_session():
    with open(os.path.join(FIXTURES_DIR, "frontend_metrics_session.json")) as f:
        return json.load(f)


def _make_context(frontend_data):
    from optimus.analyzers.base import AnalyzeContext
    ctx = AnalyzeContext(session_uuid="test", docname="test")
    ctx.frontend_data = frontend_data
    return ctx


def test_xhr_join_by_recording_id():
    from optimus.analyzers import frontend_timings

    session = _load_session()
    ctx = _make_context(session["frontend_data"])
    result = frontend_timings.analyze(session["recordings"], ctx)

    matched = result.aggregate["frontend_xhr_matched"]
    assert len(matched) == 2
    ids = [m["action_idx"] for m in matched]
    assert 0 in ids and 1 in ids


def test_orphans_are_separated():
    from optimus.analyzers import frontend_timings

    session = _load_session()
    ctx = _make_context(session["frontend_data"])
    result = frontend_timings.analyze(session["recordings"], ctx)

    orphans = result.aggregate["frontend_orphans"]
    assert len(orphans) == 1
    assert orphans[0]["recording_id"] == "rec-ORPHAN"


def test_lcp_dedup_picks_last_per_page():
    """LCP fires multiple times — analyzer should keep the last value
    per page_url, matching the Web Vitals library convention."""
    from optimus.analyzers import frontend_timings

    session = _load_session()
    ctx = _make_context(session["frontend_data"])
    result = frontend_timings.analyze(session["recordings"], ctx)

    by_page = result.aggregate["frontend_vitals_by_page"]
    assert "/app/sales-invoice/SI-001" in by_page
    assert by_page["/app/sales-invoice/SI-001"]["lcp_ms"] == 2800


def test_slow_frontend_render_fires_on_lcp():
    from optimus.analyzers import frontend_timings

    session = _load_session()
    ctx = _make_context(session["frontend_data"])
    result = frontend_timings.analyze(session["recordings"], ctx)

    slow = [f for f in result.findings if f["finding_type"] == "Slow Frontend Render"]
    assert len(slow) == 1
    assert slow[0]["severity"] == "Medium"  # 2800ms is Medium (2500 < x < 4000)


def test_network_overhead_fires_on_disproportion():
    """rec-B: XHR 1900ms - backend 180ms = 1720ms delta. 1720 > 500 AND
    1720 > 180 * 1.5 = 270 → fires. Severity: delta > 1000 → Medium."""
    from optimus.analyzers import frontend_timings

    session = _load_session()
    ctx = _make_context(session["frontend_data"])
    result = frontend_timings.analyze(session["recordings"], ctx)

    overhead = [f for f in result.findings if f["finding_type"] == "Network Overhead"]
    assert len(overhead) == 1
    assert overhead[0]["severity"] == "Medium"


def test_network_overhead_does_not_fire_on_proportional_delta():
    """Small proportional overhead must NOT fire. Delta 50ms vs 300ms backend
    is 16%, NOT a disproportionate overhead pattern."""
    from optimus.analyzers import frontend_timings

    recordings = [{"uuid": "r1", "action_label": "a", "duration_ms": 300}]
    ctx = _make_context({
        "xhr": [{
            "recording_id": "r1", "url": "/api/method/foo", "method": "GET",
            "duration_ms": 350, "status": 200, "response_size_bytes": 0,
            "transport": "xhr", "timestamp": 0,
        }],
        "vitals": [],
    })
    result = frontend_timings.analyze(recordings, ctx)
    overhead = [f for f in result.findings if f["finding_type"] == "Network Overhead"]
    assert overhead == []


def test_heavy_response_flags_large_payloads():
    from optimus.analyzers import frontend_timings

    session = _load_session()
    ctx = _make_context(session["frontend_data"])
    result = frontend_timings.analyze(session["recordings"], ctx)

    heavy = [f for f in result.findings if f["finding_type"] == "Heavy Response"]
    # rec-B response_size 512000 > 500000 threshold → fires.
    assert len(heavy) == 1
    assert heavy[0]["severity"] == "Low"


def test_negative_network_delta_clamps_to_zero():
    """Clock skew between browser and server can produce a negative
    delta. Clamp to 0 so we don't emit misleading findings."""
    from optimus.analyzers import frontend_timings

    recordings = [{"uuid": "r1", "action_label": "a", "duration_ms": 500}]
    ctx = _make_context({
        "xhr": [{
            "recording_id": "r1",
            "url": "/api/method/foo",
            "method": "GET",
            "duration_ms": 300,  # browser says 300ms, backend says 500ms
            "status": 200,
            "response_size_bytes": 0,
            "transport": "xhr",
            "timestamp": 0,
        }],
        "vitals": [],
    })
    result = frontend_timings.analyze(recordings, ctx)
    matched = result.aggregate["frontend_xhr_matched"]
    assert matched[0]["network_delta_ms"] == 0


def test_empty_frontend_data_is_safe():
    from optimus.analyzers import frontend_timings

    ctx = _make_context({"xhr": [], "vitals": []})
    result = frontend_timings.analyze([], ctx)
    assert result.findings == []
    assert result.aggregate["frontend_xhr_matched"] == []
    assert result.aggregate["frontend_vitals_by_page"] == {}


def test_missing_frontend_data_attribute_is_safe():
    """If analyze.run forgot to set context.frontend_data, don't crash."""
    from optimus.analyzers import frontend_timings
    from optimus.analyzers.base import AnalyzeContext

    ctx = AnalyzeContext(session_uuid="test", docname="test")
    # Deliberately do not set ctx.frontend_data.
    result = frontend_timings.analyze([], ctx)
    assert result.findings == []


# ---------------------------------------------------------------------------
# v0.5.1 regression guards: action_label + backend_ms come from
# context.actions, not from the raw recording dict. Real production
# recordings don't have either field — they have `path`, `method`,
# `cmd`, `duration` — so the pre-v0.5.1 code fell through to the
# synthetic "action_N" label and `backend_ms = 0` every time.
# ---------------------------------------------------------------------------


def _production_shape_recordings():
	"""Mimics the dict shape Frappe's recorder actually produces — no
	action_label, no duration_ms, just path/method/cmd/duration."""
	return [
		{
			"uuid": "rec-a",
			"path": "/api/method/frappe.desk.form.save.savedocs",
			"method": "POST",
			"cmd": "frappe.desk.form.save.savedocs",
			"duration": 180.0,  # NOT duration_ms
			"event_type": "HTTP Request",
			"calls": [],
		},
		{
			"uuid": "rec-b",
			"path": "/api/resource/Sales Invoice/SI-001",
			"method": "GET",
			"cmd": "",
			"duration": 95.0,
			"event_type": "HTTP Request",
			"calls": [],
		},
	]


def _context_with_per_action_output(recordings):
	"""Simulate what context.actions looks like AFTER per_action.analyze
	has run. v0.5.2: action_label on context.actions is the TECHNICAL
	label (raw cmd with optional :Action suffix, or METHOD+path) —
	humanization moved to per_action.humanized_label() and is used
	only by the Steps-to-Reproduce section. The frontend XHR panel
	mirrors whatever context.actions holds, so these fixtures match
	the current technical-label format.
	"""
	from optimus.analyzers.base import AnalyzeContext
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.frontend_data = {"xhr": [], "vitals": []}
	ctx.actions = [
		{
			"action_label": "frappe.desk.form.save.savedocs:Save",
			"duration_ms": 180.0,
			"path": "/api/method/frappe.desk.form.save.savedocs",
		},
		{
			"action_label": "GET /api/resource/Sales Invoice/SI-001",
			"duration_ms": 95.0,
			"path": "/api/resource/Sales Invoice/SI-001",
		},
	]
	return ctx


def test_action_label_comes_from_context_actions(monkeypatch):
	"""v0.5.1 fix, still valid in v0.5.2: per-XHR rows read
	action_label from context.actions rather than from the raw
	recording dict (which never carries it in production). The
	specific label format evolved across versions (v0.5.1
	humanized, v0.5.2 technical with :Action suffix), but the
	routing through context.actions is invariant — that's what
	this test guards.
	"""
	from optimus.analyzers import frontend_timings

	recordings = _production_shape_recordings()
	ctx = _context_with_per_action_output(recordings)
	ctx.frontend_data = {
		"xhr": [
			{
				"recording_id": "rec-a",
				"url": "/api/method/frappe.desk.form.save.savedocs",
				"duration_ms": 250,
				"status": 200,
				"response_size_bytes": 500,
				"transport": "xhr",
				"timestamp": 0,
			},
			{
				"recording_id": "rec-b",
				"url": "/api/resource/Sales Invoice/SI-001",
				"duration_ms": 130,
				"status": 200,
				"response_size_bytes": 800,
				"transport": "xhr",
				"timestamp": 0,
			},
		],
		"vitals": [],
	}

	result = frontend_timings.analyze(recordings, ctx)
	matched = result.aggregate["frontend_xhr_matched"]
	labels = [m["action_label"] for m in matched]

	# Labels come from context.actions (v0.5.2 technical format),
	# NOT the synthetic action_0 / action_1 fallback.
	assert "frappe.desk.form.save.savedocs:Save" in labels
	assert "GET /api/resource/Sales Invoice/SI-001" in labels
	assert not any(lbl.startswith("action_") for lbl in labels), (
		f"synthetic action_N labels leaked: {labels}"
	)


def test_backend_ms_comes_from_context_actions():
	"""v0.5.1 fix: backend_ms is context.actions[idx].duration_ms, not
	recording.duration_ms (which doesn't exist). Pre-v0.5.1 this
	field was always 0 in production, which made
	network_delta_ms == xhr_ms and every XHR looked like it had
	100% network overhead."""
	from optimus.analyzers import frontend_timings

	recordings = _production_shape_recordings()
	ctx = _context_with_per_action_output(recordings)
	ctx.frontend_data = {
		"xhr": [
			{
				"recording_id": "rec-a",
				"url": "/api/method/foo",
				"duration_ms": 220,
				"status": 200,
				"response_size_bytes": 100,
				"transport": "xhr",
				"timestamp": 0,
			},
		],
		"vitals": [],
	}
	result = frontend_timings.analyze(recordings, ctx)
	matched = result.aggregate["frontend_xhr_matched"]
	assert len(matched) == 1
	# context.actions[0].duration_ms == 180, xhr_ms == 220, delta == 40
	assert matched[0]["backend_ms"] == 180.0
	assert matched[0]["xhr_ms"] == 220
	assert matched[0]["network_delta_ms"] == 40


def test_falls_back_to_method_and_path_when_context_actions_missing():
	"""If per_action hasn't run (context.actions empty), don't crash
	and don't emit the ugly synthetic 'action_N'. Build a readable
	label from the recording's own method + path."""
	from optimus.analyzers import frontend_timings
	from optimus.analyzers.base import AnalyzeContext

	recordings = _production_shape_recordings()
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	# Deliberately do NOT set ctx.actions.
	ctx.frontend_data = {
		"xhr": [
			{
				"recording_id": "rec-a",
				"url": "/api/method/foo",
				"duration_ms": 220,
				"status": 200,
				"response_size_bytes": 100,
				"transport": "xhr",
				"timestamp": 0,
			},
		],
		"vitals": [],
	}
	result = frontend_timings.analyze(recordings, ctx)
	matched = result.aggregate["frontend_xhr_matched"]
	# Fallback: "POST /api/method/frappe.desk.form.save.savedocs"
	assert matched[0]["action_label"] == (
		"POST /api/method/frappe.desk.form.save.savedocs"
	)
	# backend_ms also falls back to the recording's `duration` field.
	assert matched[0]["backend_ms"] == 180.0


def test_lcp_below_threshold_does_not_fire():
    from optimus.analyzers import frontend_timings

    recordings = [{"uuid": "r1", "action_label": "a", "duration_ms": 100}]
    ctx = _make_context({
        "xhr": [],
        "vitals": [
            {"name": "lcp", "value_ms": 1800, "page_url": "/app", "timestamp": 0},
        ],
    })
    result = frontend_timings.analyze(recordings, ctx)
    slow = [f for f in result.findings if f["finding_type"] == "Slow Frontend Render"]
    assert slow == []
