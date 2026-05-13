# optimus/tests/test_correlation_header.py
# Copyright (c) 2026, Optimus contributors

"""Tests for the X-Optimus-Recording-Id response header injection (v0.5.0).

The correlation header is read by the browser-side optimus_frontend.js
shim to tie each XHR timing back to a specific server recording. The
Access-Control-Expose-Headers entry is load-bearing: without it, browsers
refuse to surface the custom header to JavaScript, even for same-origin
requests. That's the #1 failure mode in frontend instrumentation.
"""

import types

import frappe


def _set_fake_local(headers=None):
    """Replace frappe.local with a minimal SimpleNamespace holding a
    dict-like response_headers. Returns the namespace so tests can
    assert on it."""
    local = types.SimpleNamespace()
    if headers is not None:
        local.response_headers = headers
    frappe.local = local
    return local


def test_inject_correlation_header_sets_both_headers():
    from optimus.hooks_callbacks import _inject_correlation_header

    headers = {}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    assert headers["X-Optimus-Recording-Id"] == "rec-uuid-abc123"
    assert "X-Optimus-Recording-Id" in headers.get("Access-Control-Expose-Headers", "")


def test_inject_correlation_header_merges_with_existing_expose():
    """If another app has already set Access-Control-Expose-Headers,
    our value must be appended, not overwritten."""
    from optimus.hooks_callbacks import _inject_correlation_header

    headers = {"Access-Control-Expose-Headers": "X-Some-Other-Header"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    assert "X-Some-Other-Header" in expose
    assert "X-Optimus-Recording-Id" in expose


def test_inject_correlation_header_idempotent():
    """Calling the injector twice must not duplicate the expose entry."""
    from optimus.hooks_callbacks import _inject_correlation_header

    headers = {}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-1")
    _inject_correlation_header("rec-uuid-2")

    expose = headers["Access-Control-Expose-Headers"]
    assert expose.count("X-Optimus-Recording-Id") == 1


def test_inject_correlation_header_tokenwise_match_not_substring():
    """Pass-4 regression guard: the idempotency check must split on
    commas and compare tokens, not do a substring `in` match.

    If another app already set
    Access-Control-Expose-Headers: X-Optimus-Recording-Id-Legacy
    a substring `in` check falsely sees our header as present and
    skips adding it. The REAL header is then never exposed and the
    browser refuses to surface it to JavaScript, silently breaking
    the entire frontend correlation feature.
    """
    from optimus.hooks_callbacks import _inject_correlation_header

    headers = {"Access-Control-Expose-Headers": "X-Optimus-Recording-Id-Legacy"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    # Both tokens must be present — the legacy one preserved, the
    # real one appended as a new token.
    tokens = [t.strip() for t in expose.split(",")]
    assert "X-Optimus-Recording-Id-Legacy" in tokens
    assert "X-Optimus-Recording-Id" in tokens


def test_correlation_header_gated_on_profiler_session_id():
    """Pass-5 regression guard: the correlation header must only be
    injected when an active profiler session is present — not merely
    when there's a recording UUID. The standalone Frappe Recorder UI
    sets frappe.local._recorder for non-session traffic, and leaking
    X-Optimus-Recording-Id onto those responses would cause
    optimus_frontend.js to buffer XHR timings tagged to a recording
    UUID that has no session to flush them to.

    Source-inspection check on the after_request hook's correlation
    header block.
    """
    import inspect

    from optimus import hooks_callbacks

    src = inspect.getsource(hooks_callbacks.after_request)
    # The correlation header injection must check optimus_session_id
    # (from frappe.local), not just the recording UUID.
    assert "optimus_session_id" in src
    # And the check must guard _inject_correlation_header.
    correlation_idx = src.find("_inject_correlation_header")
    assert correlation_idx > 0
    # Look for optimus_session_id reference within ~30 lines before
    # the correlation header call.
    preamble = src[:correlation_idx]
    assert "optimus_session_id" in preamble, (
        "_inject_correlation_header must be gated on optimus_session_id, "
        "not just recording_uuid_for_dump — otherwise non-session "
        "traffic (e.g. from the standalone Recorder UI) leaks the header"
    )

def test_inject_correlation_header_case_insensitive_idempotency():
    """HTTP header names are case-insensitive. The token check must
    be too — if another app set the expose header in lowercase, we
    shouldn't add our (same) header with different casing and create
    a duplicate."""
    from optimus.hooks_callbacks import _inject_correlation_header

    headers = {"Access-Control-Expose-Headers": "x-profiler-recording-id"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    # Must not add a second entry just because our spelling differs.
    assert expose.lower().count("x-profiler-recording-id") == 1


def test_inject_correlation_header_noop_without_response_headers():
    """If frappe.local has no response_headers attribute (non-HTTP context
    like a script shell), the injector must no-op, not raise."""
    from optimus.hooks_callbacks import _inject_correlation_header

    _set_fake_local()  # no response_headers attribute

    # Must not raise.
    _inject_correlation_header("rec-uuid-abc123")


def test_hooks_callbacks_invokes_snapshot():
    """Source-inspection regression guard: before/after hooks must call
    infra_capture. Source inspection is an established pattern in this
    codebase (see test_api_start_kwargs.py)."""
    import inspect

    from optimus import hooks_callbacks

    before = inspect.getsource(hooks_callbacks.before_request)
    after = inspect.getsource(hooks_callbacks.after_request)
    before_job = inspect.getsource(hooks_callbacks.before_job)
    after_job = inspect.getsource(hooks_callbacks.after_job)

    assert "infra_capture" in before
    assert "infra_capture" in after
    assert "infra_capture" in before_job
    assert "infra_capture" in after_job
    assert "profiler:infra:" in after
    assert "profiler:infra:" in after_job


def test_stop_session_force_stops_infra_inflight():
    import inspect

    from optimus import api

    src = inspect.getsource(api._stop_session)
    assert "infra_capture._force_stop_inflight" in src


def test_after_request_injects_correlation_header():
    import inspect

    from optimus import hooks_callbacks

    src = inspect.getsource(hooks_callbacks.after_request)
    assert "_inject_correlation_header" in src
