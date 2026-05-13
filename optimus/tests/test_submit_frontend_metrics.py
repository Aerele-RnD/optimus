# optimus/tests/test_submit_frontend_metrics.py
# Copyright (c) 2026, Optimus contributors

"""Tests for api.submit_frontend_metrics (v0.5.0).

The endpoint accepts a JSON string payload (because sendBeacon sends
raw Blob, not form-encoded), validates session ownership, merges
against any existing Redis blob, and enforces soft caps server-side.
"""

import json

import pytest


@pytest.fixture
def mock_frappe(monkeypatch):
    """Install the minimal Frappe stubs needed to exercise the endpoint
    without a real site.

    The FakeCache mocks both the v0.4.0 key/value interface and the
    v0.5.1 Redis list interface (rpush/lrange/ltrim/llen/expire_key)
    used by submit_frontend_metrics's atomic write path.
    """
    import frappe

    cache_store = {}  # key -> plain value
    list_store = {}   # key -> list

    class FakeCache:
        def get_value(self, key):
            return cache_store.get(key)

        def set_value(self, key, value, expires_in_sec=None):
            cache_store[key] = value

        def delete_value(self, key):
            cache_store.pop(key, None)
            list_store.pop(key, None)

        # ---- Redis list interface (v0.5.1+) ------------------------
        def rpush(self, key, value):
            list_store.setdefault(key, []).append(value)
            return len(list_store[key])

        def lrange(self, key, start, stop):
            items = list_store.get(key, [])
            # Redis LRANGE is inclusive on both ends; Python slicing
            # is inclusive-exclusive. stop == -1 means "to the end".
            if stop == -1:
                return items[start:]
            return items[start : stop + 1]

        def ltrim(self, key, start, stop):
            items = list_store.get(key, [])
            if stop == -1:
                list_store[key] = items[start:]
            else:
                list_store[key] = items[start : stop + 1]

        def llen(self, key):
            return len(list_store.get(key, []))

        def expire_key(self, key, time, *, user=None, shared=False):
            # No-op for the fake cache; the tests don't exercise TTL.
            pass

    class FakeSession:
        user = "alice@example.com"

    monkeypatch.setattr(frappe, "cache", FakeCache(), raising=False)
    monkeypatch.setattr(frappe, "session", FakeSession(), raising=False)
    monkeypatch.setattr(frappe, "parse_json", json.loads, raising=False)
    monkeypatch.setattr(
        frappe, "get_roles",
        lambda user=None: ["System Manager"],
        raising=False,
    )
    monkeypatch.setattr(
        frappe, "log_error", lambda *a, **k: None, raising=False
    )

    class PermError(Exception):
        pass

    frappe.PermissionError = PermError

    def fake_throw(msg, exc=None):
        raise (exc or Exception)(msg)

    monkeypatch.setattr(frappe, "throw", fake_throw, raising=False)

    return {"cache_store": cache_store, "list_store": list_store}


def test_submit_rejects_invalid_json(mock_frappe):
    from optimus import api

    result = api.submit_frontend_metrics(payload="not-json")
    assert result["accepted"] is False
    assert result["reason"] == "invalid json"


def test_submit_rejects_missing_session_uuid(mock_frappe):
    from optimus import api

    result = api.submit_frontend_metrics(payload=json.dumps({"xhr": []}))
    assert result["accepted"] is False
    assert result["reason"] == "missing session_uuid"


def test_submit_rejects_unknown_session(mock_frappe):
    from optimus import api

    result = api.submit_frontend_metrics(
        payload=json.dumps({"session_uuid": "nope", "xhr": [], "vitals": []})
    )
    assert result["accepted"] is False
    assert result["reason"] == "session not found"


def test_submit_rejects_cross_user_session(mock_frappe):
    from optimus import api

    mock_frappe["cache_store"]["profiler:session:S1:meta"] = {
        "user": "bob@example.com",
        "docname": "PS-0001",
    }

    result = api.submit_frontend_metrics(
        payload=json.dumps({"session_uuid": "S1", "xhr": [], "vitals": []})
    )
    assert result["accepted"] is False
    assert result["reason"] == "session not found"


def test_submit_accepts_and_stores(mock_frappe):
    from optimus import api

    mock_frappe["cache_store"]["profiler:session:S2:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-0002",
    }

    payload = json.dumps({
        "session_uuid": "S2",
        "xhr": [
            {"recording_id": "r1", "url": "/api/method/foo",
             "method": "GET", "duration_ms": 120, "status": 200,
             "response_size_bytes": 512, "transport": "fetch",
             "timestamp": 1700000000000},
        ],
        "vitals": [
            {"name": "lcp", "value_ms": 1800, "page_url": "/app",
             "timestamp": 1700000000000},
        ],
    })

    result = api.submit_frontend_metrics(payload=payload)
    assert result["accepted"] is True
    assert result["xhr_count"] == 1
    assert result["vitals_count"] == 1

    # v0.5.1+: two atomic Redis lists instead of a merged dict.
    xhr_list = mock_frappe["list_store"]["profiler:frontend:S2:xhr"]
    vitals_list = mock_frappe["list_store"]["profiler:frontend:S2:vitals"]
    assert len(xhr_list) == 1
    assert len(vitals_list) == 1


def test_submit_appends_to_existing_list(mock_frappe):
    """Two sequential submits must accumulate into the Redis list
    atomically — RPUSH semantics preserve both.
    """
    from optimus import api

    mock_frappe["cache_store"]["profiler:session:S3:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-0003",
    }

    first = json.dumps({
        "session_uuid": "S3",
        "xhr": [{"recording_id": "r0", "url": "/api/prev",
                 "method": "POST", "duration_ms": 50, "status": 200,
                 "response_size_bytes": 100, "transport": "xhr",
                 "timestamp": 1}],
        "vitals": [],
    })
    second = json.dumps({
        "session_uuid": "S3",
        "xhr": [{"recording_id": "r1", "url": "/api/next",
                 "method": "GET", "duration_ms": 200, "status": 200,
                 "response_size_bytes": 999, "transport": "fetch",
                 "timestamp": 2}],
        "vitals": [],
    })

    assert api.submit_frontend_metrics(payload=first)["accepted"] is True
    result = api.submit_frontend_metrics(payload=second)
    assert result["accepted"] is True

    xhr_list = mock_frappe["list_store"]["profiler:frontend:S3:xhr"]
    assert len(xhr_list) == 2
    # List entries are JSON-encoded strings; decode to verify order.
    decoded = [json.loads(e) for e in xhr_list]
    assert decoded[0]["url"] == "/api/prev"
    assert decoded[1]["url"] == "/api/next"


def test_atomic_append_survives_simulated_race(mock_frappe):
    """Regression guard for the pre-v0.5.1 GET-merge-SET race: two
    'concurrent' submits in a row must not lose each other's data.
    We can't truly interleave in a single-threaded test, but we can
    assert that the list grows monotonically under sequential calls
    (which is the race-free property RPUSH gives us).
    """
    from optimus import api

    mock_frappe["cache_store"]["profiler:session:Srace:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-RACE",
    }

    for i in range(5):
        payload = json.dumps({
            "session_uuid": "Srace",
            "xhr": [{"recording_id": f"r{i}", "url": f"/api/{i}",
                     "method": "GET", "duration_ms": i * 10, "status": 200,
                     "response_size_bytes": 0, "transport": "xhr",
                     "timestamp": i}],
            "vitals": [],
        })
        result = api.submit_frontend_metrics(payload=payload)
        assert result["accepted"] is True
        assert result["xhr_count"] == i + 1  # strict monotonic growth

    xhr_list = mock_frappe["list_store"]["profiler:frontend:Srace:xhr"]
    assert len(xhr_list) == 5
    decoded = [json.loads(e) for e in xhr_list]
    assert [d["recording_id"] for d in decoded] == ["r0", "r1", "r2", "r3", "r4"]


def test_frontend_js_wraps_beacon_body_for_frappe_routing():
    """Pass-4 regression guard: the sendBeacon path must wrap its
    inner JSON body as {"payload": "<json string>"} so the server
    sees a `payload` kwarg matching the endpoint signature.

    Without the wrap, Frappe's request handler parses the raw body
    as JSON and flattens the top-level keys into form_dict, so
    submit_frontend_metrics is called with session_uuid=..., xhr=...,
    vitals=... — which does NOT match the (payload: str) signature
    and fails with TypeError. The beacon path silently drops every
    payload in that case.

    This is a source-inspection check on optimus_frontend.js since
    we can't actually exercise sendBeacon in a unit test.
    """
    import os

    here = os.path.dirname(__file__)
    jspath = os.path.join(
        here, "..", "public", "js", "optimus_frontend.js"
    )
    with open(jspath) as f:
        src = f.read()

    # Must construct a beaconBody distinct from the raw frappe.call body.
    assert "beaconBody" in src, (
        "optimus_frontend.js must construct a beaconBody wrapped as "
        "{payload: body} for sendBeacon — see comment explaining the "
        "server contract"
    )
    # Must wrap as {payload: body}.
    assert "JSON.stringify({ payload: body })" in src or \
           'JSON.stringify({"payload": body})' in src, (
        "beaconBody must be JSON.stringify({ payload: body })"
    )
    # The Blob constructor used with sendBeacon must reference
    # beaconBody, not the unwrapped body. Find `new Blob([beaconBody]`
    # specifically — this string only appears in actual code, not in
    # the explanatory comment block.
    assert "new Blob([beaconBody]" in src, (
        "sendBeacon must send new Blob([beaconBody], ...), not the "
        "raw body — otherwise Frappe's JSON body flattening mismatches "
        "the endpoint signature"
    )


def test_read_frontend_data_roundtrip(mock_frappe):
    """_read_frontend_data must decode the list entries back into the
    dict shape the frontend_timings analyzer expects."""
    from optimus import api

    mock_frappe["cache_store"]["profiler:session:Sroundtrip:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-RT",
    }

    payload = json.dumps({
        "session_uuid": "Sroundtrip",
        "xhr": [
            {"recording_id": "r1", "url": "/api/method/one", "method": "GET",
             "duration_ms": 100, "status": 200, "response_size_bytes": 0,
             "transport": "fetch", "timestamp": 1},
            {"recording_id": "r2", "url": "/api/method/two", "method": "POST",
             "duration_ms": 200, "status": 200, "response_size_bytes": 42,
             "transport": "xhr", "timestamp": 2},
        ],
        "vitals": [
            {"name": "lcp", "value_ms": 1500, "page_url": "/app/foo",
             "timestamp": 3},
        ],
    })
    assert api.submit_frontend_metrics(payload=payload)["accepted"] is True

    data = api._read_frontend_data("Sroundtrip")
    assert len(data["xhr"]) == 2
    assert data["xhr"][0]["url"] == "/api/method/one"
    assert data["xhr"][1]["url"] == "/api/method/two"
    assert len(data["vitals"]) == 1
    assert data["vitals"][0]["name"] == "lcp"


def test_read_frontend_data_handles_corrupt_entry(mock_frappe):
    """A badly-encoded list entry (e.g. truncated or non-JSON) must be
    silently skipped, not break the whole read."""
    from optimus import api

    mock_frappe["list_store"]["profiler:frontend:Scorrupt:xhr"] = [
        json.dumps({"recording_id": "r1", "url": "/ok"}),
        "not-json-at-all",
        json.dumps({"recording_id": "r2", "url": "/ok2"}),
    ]
    mock_frappe["list_store"]["profiler:frontend:Scorrupt:vitals"] = []

    data = api._read_frontend_data("Scorrupt")
    assert len(data["xhr"]) == 2
    assert data["xhr"][0]["url"] == "/ok"
    assert data["xhr"][1]["url"] == "/ok2"


def test_submit_enforces_soft_cap_tail_prefer(mock_frappe):
    """Over-cap submissions must keep the most-recent entries (tail),
    not the oldest, because end-of-flow is where slow things happen."""
    from optimus import api

    mock_frappe["cache_store"]["profiler:session:S4:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-0004",
    }

    # Push 1500 XHRs — 500 over the cap of 1000.
    xhrs = [
        {"recording_id": f"r{i}", "url": f"/api/{i}",
         "method": "GET", "duration_ms": 10, "status": 200,
         "response_size_bytes": 0, "transport": "fetch",
         "timestamp": i}
        for i in range(1500)
    ]
    payload = json.dumps({"session_uuid": "S4", "xhr": xhrs, "vitals": []})

    result = api.submit_frontend_metrics(payload=payload)
    assert result["accepted"] is True

    xhr_list = mock_frappe["list_store"]["profiler:frontend:S4:xhr"]
    assert len(xhr_list) == 1000
    # Tail preferred — the last entry's timestamp should be 1499.
    # (Client-side slicing first drops the first 500, then LTRIM is a
    # no-op because the list is already at cap.)
    last = json.loads(xhr_list[-1])
    assert last["timestamp"] == 1499
