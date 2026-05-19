# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.2 round 3 executive summary card.

Goal: a non-developer reading the first screen of the report must be
able to decide "do we have a problem" in 30 seconds, pointing at (1)
session pace, (2) top impactful findings, (3) infra state (swap /
memory growth).
"""

import json
import types

from optimus.renderer import _build_executive_summary


def _doc(total_ms=3000, queries=50, actions=5):
	doc = types.SimpleNamespace()
	doc.total_duration_ms = total_ms
	doc.total_queries = queries
	doc.total_requests = actions
	return doc


def _finding(impact=100.0, severity="High", title="Some finding"):
	return {
		"severity": severity,
		"title": title,
		"estimated_impact_ms": impact,
	}


class TestHeadlinePace:
	def test_fast_session(self):
		es = _build_executive_summary(
			findings=[], session_doc=_doc(total_ms=500), v5={},
		)
		assert es["pace"] == "fast"

	def test_moderate_session(self):
		es = _build_executive_summary(
			findings=[], session_doc=_doc(total_ms=2500), v5={},
		)
		assert es["pace"] == "moderate"

	def test_slow_session(self):
		es = _build_executive_summary(
			findings=[], session_doc=_doc(total_ms=6000), v5={},
		)
		assert es["pace"] == "slow"

	def test_headline_includes_seconds_queries_operations(self):
		es = _build_executive_summary(
			findings=[], session_doc=_doc(total_ms=1200, queries=30, actions=3),
			v5={},
		)
		# Plain-English wording for a non-developer: "operations" not "actions",
		# "database queries" not bare "queries".
		# v0.7.x: 1200ms is above the default 1000ms threshold, so the
		# duration renders as seconds wrapped in the .time-high highlight
		# span (the timing rule applied everywhere else in the report).
		assert '<span class="time-high">1.20s</span>' in es["headline"]
		assert "30 database queries" in es["headline"]
		assert "3 operation" in es["headline"]
		assert "action" not in es["headline"]

	def test_headline_below_threshold_keeps_ms(self):
		"""Below the threshold the duration must stay as plain ms — no
		highlight span. Same behaviour as fmt_ms() everywhere else."""
		es = _build_executive_summary(
			findings=[], session_doc=_doc(total_ms=800, queries=10, actions=2),
			v5={},
		)
		assert "800ms" in es["headline"]
		assert '<span class="time-high">' not in es["headline"]

	def test_headline_respects_custom_threshold(self):
		"""Admins can raise the threshold via Optimus Settings; the
		exec-summary headline must honour their setting, not hardcoded
		1000ms. With threshold=5000, a 2000ms session still reads as ms."""
		es = _build_executive_summary(
			findings=[], session_doc=_doc(total_ms=2000, queries=10, actions=2),
			v5={}, large_duration_threshold_ms=5000.0,
		)
		assert "2000ms" in es["headline"]
		assert '<span class="time-high">' not in es["headline"]


class TestTopBullets:
	def test_top_three_by_impact(self):
		es = _build_executive_summary(
			findings=[
				_finding(impact=10, title="A"),
				_finding(impact=500, title="B"),
				_finding(impact=50, title="C"),
				_finding(impact=200, title="D"),
				_finding(impact=1, title="E"),
			],
			session_doc=_doc(),
			v5={},
		)
		assert [b["text"] for b in es["bullets"]] == ["B", "D", "C"]

	def test_less_than_three_findings_shows_what_exists(self):
		es = _build_executive_summary(
			findings=[_finding(impact=100, title="Only")],
			session_doc=_doc(),
			v5={},
		)
		assert len(es["bullets"]) == 1

	def test_no_findings_no_bullets(self):
		es = _build_executive_summary(
			findings=[], session_doc=_doc(), v5={},
		)
		assert es["bullets"] == []

	def test_bullets_carry_severity(self):
		es = _build_executive_summary(
			findings=[_finding(impact=10, severity="High", title="x")],
			session_doc=_doc(),
			v5={},
		)
		assert es["bullets"][0]["severity"] == "High"


class TestInfraSignal:
	def test_large_memory_growth_flagged(self):
		es = _build_executive_summary(
			findings=[],
			session_doc=_doc(),
			v5={"infra_summary": {"rss_delta": 80_000_000}},  # 80MB
		)
		assert es["infra_note"] is not None
		assert "80MB" in es["infra_note"]
		assert "grew" in es["infra_note"]

	def test_memory_shrink_still_flagged(self):
		es = _build_executive_summary(
			findings=[],
			session_doc=_doc(),
			v5={"infra_summary": {"rss_delta": -60_000_000}},
		)
		assert "shrank" in es["infra_note"]

	def test_swap_flagged(self):
		es = _build_executive_summary(
			findings=[],
			session_doc=_doc(),
			v5={"infra_summary": {"swap_peak_mb": 500}},
		)
		assert "Swap" in es["infra_note"]
		assert "500MB" in es["infra_note"]

	def test_small_memory_not_flagged(self):
		es = _build_executive_summary(
			findings=[],
			session_doc=_doc(),
			v5={"infra_summary": {"rss_delta": 10_000_000}},  # 10MB, noise
		)
		assert es["infra_note"] is None


class TestShowFlag:
	def test_clean_session_no_infra_signal_hides_card(self):
		es = _build_executive_summary(
			findings=[], session_doc=_doc(), v5={},
		)
		assert es["show"] is False

	def test_findings_present_shows_card(self):
		es = _build_executive_summary(
			findings=[_finding()], session_doc=_doc(), v5={},
		)
		assert es["show"] is True

	def test_only_infra_signal_still_shows_card(self):
		es = _build_executive_summary(
			findings=[],
			session_doc=_doc(),
			v5={"infra_summary": {"rss_delta": 100_000_000}},
		)
		assert es["show"] is True


class TestEndToEndRender:
	def test_card_renders_into_html(self):
		from optimus import renderer

		doc = types.SimpleNamespace()
		doc.title = "Test"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-14"
		doc.stopped_at = "2026-04-14"
		doc.notes = None
		doc.top_severity = "High"
		doc.total_duration_ms = 6000
		doc.total_query_time_ms = 0
		doc.total_queries = 100
		doc.total_requests = 10
		doc.summary_html = None
		doc.top_queries_json = "[]"
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = json.dumps({
			"infra_summary": {"rss_delta": 90_000_000}
		})
		doc.actions = []

		finding_row = types.SimpleNamespace()
		finding_row.finding_type = "N+1 Query"
		finding_row.severity = "High"
		finding_row.title = "Same query ran 50× at myapp/foo.py:10"
		finding_row.customer_description = "desc"
		finding_row.estimated_impact_ms = 500.0
		finding_row.affected_count = 50
		finding_row.action_ref = "0"
		finding_row.technical_detail_json = json.dumps({
			"callsite": {"filename": "apps/myapp/foo.py", "lineno": 10}
		})
		doc.findings = [finding_row]

		html = renderer.render(doc, recordings=[])

		# v0.7.x redesign Phase B: the "At a glance" exec-summary card
		# was replaced by the TL;DR hero (single composed headline).
		# The exec_summary data layer still computes the pace + bullets
		# (used by the Action plan section), so the data assertions
		# below hold via the TL;DR composition + the KPI strip.
		assert 'class="tldr"' in html, "TL;DR hero must render"
		# Duration crosses the 1000ms threshold — rendered as seconds
		# with .time-high (timing rule applies everywhere).
		assert '<span class="time-high">6.00s</span>' in html
		assert "operation" in html
		# Top finding title surfaced — TL;DR fallback branch wraps it.
		assert "Same query ran 50× at myapp/foo.py:10" in html
		# Infra note no longer rendered as a separate exec-summary line
		# (moved into the Server resource section in a later phase).
		# Sanity: it's still computed (data layer) — just not shown here.
		# v0.7.x: KPI strip (replaces stat cards) — plainly-labelled cells,
		# and the "Issues found" cell's big number is the TOTAL finding
		# count (not the High count), with a severity breakdown that sums
		# to it.
		assert "Issues found" in html and "Database queries" in html
		import re as _re
		m = _re.search(
			r'Issues found</div>\s*<div class="kpi-value[^"]*">(.*?)</div>\s*<div class="kpi-sub">(.*?)</div>',
			html, _re.S,
		)
		assert m, "could not locate the 'Issues found' KPI cell"
		assert _re.sub(r"<[^>]+>", "", m.group(1)).strip() == "1"  # one finding total
		assert "1 high" in _re.sub(r"<[^>]+>", "", m.group(2))

	def test_clean_session_no_card(self):
		from optimus import renderer

		doc = types.SimpleNamespace()
		doc.title = "Clean"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-14"
		doc.stopped_at = "2026-04-14"
		doc.notes = None
		doc.top_severity = "Low"
		doc.total_duration_ms = 500
		doc.total_query_time_ms = 0
		doc.total_queries = 5
		doc.total_requests = 1
		doc.summary_html = None
		doc.top_queries_json = "[]"
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = "{}"
		doc.actions = []
		doc.findings = []

		html = renderer.render(doc, recordings=[])

		# v0.7.x redesign Phase B: the exec-summary card was replaced
		# by the TL;DR hero. The hero ALWAYS renders — clean sessions
		# get a "Nothing to fix" branch instead of being hidden.
		assert 'class="exec-summary' not in html, (
			"Old exec-summary card markup must not be present"
		)
		assert 'class="tldr"' in html, "TL;DR hero must render even when clean"
		assert "Nothing to fix" in html, (
			"Clean-session TL;DR must use the 'Nothing to fix' headline"
		)
