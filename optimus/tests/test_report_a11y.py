# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Accessibility / UX render-time guarantees for the report template.

Renders end-to-end via ``renderer.render_raw`` and asserts the a11y pass holds:
darkened mute token, severity text labels on the waterfall (not colour-only),
the AI-fix "unverified" badge, back-to-top anchors, and the self-contained /
offline-safe invariant (no scripts, no external resource loads — aerele.in
*anchor* links are allowed).
"""

import json
import re
from types import SimpleNamespace

from optimus import renderer

_SNIPPET = [{"lineno": 41, "content": "for u in users:"}]


def _finding(**kw):
	detail = {"callsite": {"filename": "/abs/myapp/foo.py", "lineno": 41,
	                       "function": "bulk", "source_snippet": _SNIPPET}}
	base = dict(
		finding_type="N+1 Query", severity="High",
		title="Same query ran 50x at foo.py:41",
		customer_description="A query repeats inside a loop.",
		estimated_impact_ms=420.0, affected_count=50, action_ref="0",
		technical_detail_json=json.dumps(detail), llm_fix_json=None,
	)
	base.update(kw)
	return SimpleNamespace(**base)


def _action(idx, **kw):
	base = dict(action_label=f"action_{idx}", event_type="HTTP Request",
	            http_method="POST", path=f"/api/method/x{idx}", recording_uuid=f"r{idx}",
	            duration_ms=900.0, queries_count=0, query_time_ms=0, slowest_query_ms=0)
	base.update(kw)
	return SimpleNamespace(**base)


def _doc(*, findings=None, actions=None):
	return SimpleNamespace(
		name="PS-a11y", session_uuid="a11y-uuid", title="a11y test",
		user="tester@example.com", status="Ready",
		started_at="2026-05-22T00:00:00", stopped_at="2026-05-22T00:00:05",
		notes=None, top_severity="High", summary_html=None,
		total_duration_ms=5000, total_query_time_ms=0, total_queries=0,
		total_requests=len(actions or []), top_queries_json="[]",
		table_breakdown_json="[]", hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions or [], findings=findings or [], phase_2_runs=[],
	)


def _render(**kw):
	return renderer.render_raw(_doc(**kw), recordings=[])


# --------------------------------------------------------------------------
# FIX 1 — contrast token
# --------------------------------------------------------------------------

def test_ink_mute_darkened_to_aa():
	html = _render(findings=[_finding()])
	assert "--ink-mute: #5f6670;" in html       # AA-compliant value present
	assert "--ink-mute: #8a8580" not in html    # old ~3:1 value no longer assigned
	# the failing hardcoded footer grey is gone too
	assert "color: #9ca3af;" not in html


# --------------------------------------------------------------------------
# FIX 4 — waterfall severity labels (not colour-only)
# --------------------------------------------------------------------------

def test_waterfall_has_text_severity_labels():
	# action 0 carries a High finding -> hot; action 1 is an RQ Job -> bg.
	html = _render(
		actions=[_action(0, duration_ms=900),
		         _action(1, event_type="RQ Job", action_label="job.x", duration_ms=400)],
		findings=[_finding(action_ref="0")],
	)
	assert 'class="bar-label hot">High<' in html
	assert 'class="bar-label bg">BG job<' in html


# --------------------------------------------------------------------------
# Finding impact — show per-hit alongside the consolidated total
# --------------------------------------------------------------------------

def test_finding_impact_shows_per_hit_with_consolidated():
	# 420ms consolidated across 50 hits -> 8.4ms per hit (well above the 0.05 floor).
	html = _render(findings=[_finding(estimated_impact_ms=420.0, affected_count=50)])
	assert "consolidated" in html
	# The "× hits &middot;" middot only renders when the per-hit suffix is shown.
	assert "50× hits &middot;" in html
	assert "8.4ms" in html  # the per-hit value (420 / 50)


def test_finding_impact_suppresses_per_hit_when_it_would_round_to_zero():
	# Huge hit count (a line-hit / sample count, not action runs): 4.5s / 33M ≈ 0ms.
	# The hit count still shows, but the per-hit suffix is suppressed (never "0ms").
	html = _render(findings=[_finding(estimated_impact_ms=4500.0, affected_count=33_000_000)])
	# No per-hit suffix → the hits line ends right after the count.
	assert "33000000× hits</div>" in html
	assert "0ms<small class=\"scope-tag\">per hit" not in html


# --------------------------------------------------------------------------
# FIX 5 — AI-fix unverified badge
# --------------------------------------------------------------------------

def test_ai_fix_unverified_badge_present():
	html = _render(findings=[_finding(llm_fix_json=json.dumps({
		"suggestion": "**Fix**\n\nBatch the query.", "model": "claude-sonnet-4-6",
		"provider": "Anthropic", "generated_at": "2026-05-22T00:00:00+00:00",
	}))])
	assert 'class="fix-badge"' in html
	assert "Unverified" in html and "review before applying" in html


# --------------------------------------------------------------------------
# Back-to-top links removed (per user request); find-in-page note kept
# --------------------------------------------------------------------------

def test_back_to_top_links_removed_find_in_page_note_kept():
	html = _render(findings=[_finding()])
	# The "↑ top" links + their anchor/CSS are gone.
	assert 'href="#top"' not in html
	assert 'id="top"' not in html
	assert 'class="to-top"' not in html
	# …but the find-in-page guidance in "How to read" stays.
	assert "find (Ctrl/Cmd-F)" in html


# --------------------------------------------------------------------------
# Invariant — self-contained / offline-safe
# --------------------------------------------------------------------------

def test_report_is_self_contained_offline():
	html = _render(findings=[_finding(llm_fix_json=json.dumps({
		"suggestion": "Batch it.", "model": "m", "provider": "x", "generated_at": "t",
	}))])
	# No scripts / JS at all.
	assert "<script" not in html.lower()
	# No external RESOURCE loads (these would fetch over the network). Inline
	# `data:` URIs (e.g. the masthead logo) are allowed and stay self-contained.
	assert not re.search(r'src\s*=\s*["\']https?:', html)   # no remote img/script src
	assert not re.search(r'<link\b[^>]*href\s*=\s*["\']https?:', html)  # no remote stylesheet
	assert "@import" not in html
	assert "url(http" not in html.replace(" ", "").replace("'", "").replace('"', "")
	# Anchor links (e.g. aerele.in) ARE allowed — sanity-check one exists so the
	# checks above aren't trivially passing on an empty page.
	assert re.search(r'<a [^>]*href="https?://', html)
