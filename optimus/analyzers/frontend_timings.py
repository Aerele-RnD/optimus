# optimus/analyzers/frontend_timings.py
# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: browser-side timing join + Web Vitals (v0.5.0).

Reads ``context.frontend_data`` (populated by analyze.run from
``profiler:frontend:<session_uuid>``). Joins XHR timings to Profiler
Actions by recording_id. Dedupes multi-fire LCP per page. Emits three
finding types:

- Slow Frontend Render    (LCP > 2500ms on any page)
- Network Overhead        (XHR - backend > 500ms AND > backend * 1.5)
- Heavy Response          (single response > 500KB)
"""

import json

from optimus.analyzers.base import SEVERITY_ORDER, AnalyzerResult

LCP_MEDIUM_MS = 2500
LCP_HIGH_MS = 4000
NETWORK_DELTA_MIN_MS = 500
NETWORK_DELTA_MULTIPLIER = 1.5
NETWORK_DELTA_HIGH_MS = 1000
HEAVY_RESPONSE_BYTES = 500_000


def analyze(recordings: list[dict], context) -> AnalyzerResult:
    frontend_data = getattr(context, "frontend_data", None) or {"xhr": [], "vitals": []}
    xhr_entries = frontend_data.get("xhr") or []
    vitals_entries = frontend_data.get("vitals") or []

    # Build a recording_id → action_idx map.
    rec_index = {}
    for idx, rec in enumerate(recordings):
        rec_id = rec.get("uuid") or rec.get("recording_uuid")
        if rec_id:
            rec_index[rec_id] = idx

    # v0.5.1: humanized labels (e.g. "Save Sales Invoice" instead of
    # "POST /api/method/frappe.client.save") are built by
    # per_action._build_action and live on context.actions, NOT on the
    # raw `recordings` list. Pre-v0.5.1 this analyzer read from
    # recordings[action_idx] so action.get("action_label") always
    # returned None and every XHR row showed up as "action_0",
    # "action_1", etc. in the report. Pull the label from
    # context.actions instead, fall back through the recording's
    # raw method+path, and only then to the synthetic "action_N".
    ctx_actions = getattr(context, "actions", None) or []

    def _label_for(idx: int) -> str:
        # 1. Preferred: the humanized label per_action.analyze built
        #    on context.actions.
        if idx < len(ctx_actions):
            lbl = ctx_actions[idx].get("action_label")
            if lbl:
                return lbl
        # 2. Fallback: synthesize from the recording's method + path,
        #    which IS present on the raw recording dict. Still
        #    readable if per_action hasn't run yet or the actions
        #    list is shorter than recordings for any reason.
        if idx < len(recordings):
            rec = recordings[idx]
            method = rec.get("method") or ""
            path = (rec.get("path") or "").split("?", 1)[0]
            if path:
                return f"{method} {path}".strip()
            cmd = rec.get("cmd")
            if cmd:
                return str(cmd)
        # 3. Last resort: synthetic marker so the report never
        #    shows an empty string.
        return f"action_{idx}"

    matched = []
    orphans = []
    total_backend_ms = 0
    total_xhr_ms = 0
    network_overhead_ms = 0
    slowest_xhr = None

    for x in xhr_entries:
        rid = x.get("recording_id")
        if rid is None or rid not in rec_index:
            orphans.append({
                "recording_id": rid,
                "url": x.get("url"),
                "duration_ms": x.get("duration_ms"),
                "timestamp": x.get("timestamp"),
                "reason": "no_matching_recording",
            })
            continue

        action_idx = rec_index[rid]
        action = recordings[action_idx]
        # v0.5.1: backend time lives on the per_action row
        # (context.actions[idx].duration_ms), NOT on the raw
        # recording dict. Pre-v0.5.1 this read `action.get("duration_ms")`
        # from the recording and always got None, so backend_ms was
        # always 0 in production — making every XHR look like 100%
        # network overhead. Prefer context.actions[idx].duration_ms,
        # then fall back through the recording's duration_ms (test
        # fixtures occasionally put it here) and finally the
        # recording's `duration` (ms, per recorder convention).
        backend_ms = 0.0
        if action_idx < len(ctx_actions):
            backend_ms = _num(ctx_actions[action_idx].get("duration_ms")) or 0.0
        if not backend_ms:
            backend_ms = (
                _num(action.get("duration_ms"))
                or _num(action.get("duration"))
                or 0.0
            )
        xhr_ms = _num(x.get("duration_ms")) or 0
        delta = xhr_ms - backend_ms
        if delta < 0:
            delta = 0

        entry = {
            "action_idx": action_idx,
            "action_label": _label_for(action_idx),
            "backend_ms": backend_ms,
            "xhr_ms": xhr_ms,
            "network_delta_ms": delta,
            "response_size_bytes": x.get("response_size_bytes", 0),
            "status": x.get("status"),
            "url": x.get("url"),
            "transport": x.get("transport"),
        }
        matched.append(entry)

        total_backend_ms += backend_ms
        total_xhr_ms += xhr_ms
        network_overhead_ms += delta
        if slowest_xhr is None or xhr_ms > slowest_xhr["duration_ms"]:
            slowest_xhr = {
                "url": x.get("url"),
                "duration_ms": xhr_ms,
                "action_idx": action_idx,
            }

    # Dedupe multi-fire LCP per page. Keep the highest-timestamp LCP entry
    # for each page_url. Group FCP/CLS/navigation timings by page too.
    vitals_by_page: dict[str, dict] = {}
    lcp_last_ts: dict[str, int] = {}

    for v in vitals_entries:
        page = v.get("page_url") or "/"
        bucket = vitals_by_page.setdefault(page, {})
        name = v.get("name")
        ts = v.get("timestamp") or 0

        if name == "lcp":
            if ts >= lcp_last_ts.get(page, -1):
                bucket["lcp_ms"] = v.get("value_ms")
                lcp_last_ts[page] = ts
        elif name == "fcp":
            bucket["fcp_ms"] = v.get("value_ms")
        elif name == "cls":
            # CLS accumulates across entries — keep the max seen per page.
            current = bucket.get("cls", 0) or 0
            val = v.get("value") or 0
            if val > current:
                bucket["cls"] = val
        elif name == "navigation":
            bucket["ttfb_ms"] = v.get("ttfb_ms")
            bucket["dom_content_loaded_ms"] = v.get("dom_content_loaded_ms")
            bucket["load_ms"] = v.get("load_ms")
            bucket["dns_ms"] = v.get("dns_ms")
            bucket["tcp_ms"] = v.get("tcp_ms")

    findings = []

    # ---- Slow Frontend Render -------------------------------------------
    for page, vitals in vitals_by_page.items():
        lcp = vitals.get("lcp_ms")
        if lcp is None or lcp <= LCP_MEDIUM_MS:
            continue
        severity = "High" if lcp > LCP_HIGH_MS else "Medium"
        findings.append({
            "finding_type": "Slow Frontend Render",
            "severity": severity,
            "title": f"LCP {int(lcp)}ms on {page}",
            "customer_description": (
                f"The page '{page}' took {int(lcp)}ms for its largest "
                "content element to paint. Users typically perceive pages "
                "as slow beyond 2.5 seconds."
            ),
            "technical_detail_json": json.dumps({
                "lcp_ms": lcp,
                "fcp_ms": vitals.get("fcp_ms"),
                "cls": vitals.get("cls"),
                "ttfb_ms": vitals.get("ttfb_ms"),
                "page_url": page,
                "fix_hint": (
                    "Look at TTFB: if it's large, the backend is slow "
                    "(see Slow Query / N+1 findings). If TTFB is small, "
                    "the browser spent time downloading or rendering — "
                    "check response size and JavaScript execution."
                ),
            }, default=str),
            "estimated_impact_ms": lcp,
            "affected_count": 1,
            "action_ref": "0",
        })

    # ---- Network Overhead -----------------------------------------------
    for m in matched:
        delta = m["network_delta_ms"]
        backend = m["backend_ms"]
        if delta > NETWORK_DELTA_MIN_MS and delta > backend * NETWORK_DELTA_MULTIPLIER:
            severity = "Medium" if delta > NETWORK_DELTA_HIGH_MS else "Low"
            findings.append({
                "finding_type": "Network Overhead",
                "severity": severity,
                "title": f"{int(delta)}ms network overhead on {m['action_label']}",
                "customer_description": (
                    f"The browser waited {int(delta)}ms longer than the "
                    "server spent processing this request. That extra time "
                    "is network, TLS, serialization, or response download."
                ),
                "technical_detail_json": json.dumps({
                    "backend_ms": backend,
                    "xhr_ms": m["xhr_ms"],
                    "delta_ms": delta,
                    "response_size_bytes": m["response_size_bytes"],
                    "url": m["url"],
                    "fix_hint": (
                        "Large response sizes cause this — check the "
                        "Heavy Response finding. If response is small, "
                        "suspect network path: CDN, TLS handshake, proxy."
                    ),
                }, default=str),
                "estimated_impact_ms": delta,
                "affected_count": 1,
                "action_ref": str(m["action_idx"]),
            })

    # ---- Heavy Response --------------------------------------------------
    for m in matched:
        size = m.get("response_size_bytes") or 0
        if size > HEAVY_RESPONSE_BYTES:
            findings.append({
                "finding_type": "Heavy Response",
                "severity": "Low",
                "title": f"{size // 1024}KB response on {m['action_label']}",
                "customer_description": (
                    f"The server returned {size // 1024}KB for this request. "
                    "Large responses are sometimes correct, sometimes a sign "
                    "of returning more data than the UI needs."
                ),
                "technical_detail_json": json.dumps({
                    "response_size_bytes": size,
                    "url": m["url"],
                    "fix_hint": (
                        "Check whether the UI actually uses all the fields "
                        "returned. Paginate or limit field lists if not."
                    ),
                }, default=str),
                "estimated_impact_ms": 0,
                "affected_count": 1,
                "action_ref": str(m["action_idx"]),
            })

    findings.sort(
        key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"])
    )

    summary = {
        "total_xhrs": len(matched),
        "total_xhr_ms": round(total_xhr_ms, 2),
        "total_backend_ms": round(total_backend_ms, 2),
        "network_overhead_ms": round(network_overhead_ms, 2),
        "slowest_xhr": slowest_xhr,
    }

    return AnalyzerResult(
        findings=findings,
        aggregate={
            "frontend_xhr_matched": matched,
            "frontend_vitals_by_page": vitals_by_page,
            "frontend_orphans": orphans,
            "frontend_summary": summary,
        },
    )


def _num(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
