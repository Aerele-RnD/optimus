# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for `renderer._compose_tldr` — the v0.7.x redesign Phase B
TL;DR hero composer.

The hero picks the single highest-impact finding (severity desc, then
impact desc) and emits a one-sentence headline keyed on the finding's
category. Key phrases are wrapped in `<span class="hot">` via
markupsafe.Markup so the inline red-italic survives Jinja autoescape
when the template renders `{{ tldr.headline_markup }}`.
"""

from types import SimpleNamespace

from markupsafe import Markup

from optimus import renderer


def _doc(**kw):
	base = {
		"total_duration_ms": 4783,
		"total_queries": 1868,
		"total_requests": 20,
	}
	base.update(kw)
	return SimpleNamespace(**base)


def _finding(**kw):
	base = {
		"finding_type": "N+1 Query",
		"severity": "High",
		"title": "Same query ran 100×",
		"estimated_impact_ms": 449.0,
		"affected_count": 100,
	}
	base.update(kw)
	return base


class TestCategoryMap:
	def test_known_finding_types_map_to_categories(self):
		assert renderer._category_for("N+1 Query") == "n_plus_one"
		assert renderer._category_for("Framework N+1") == "n_plus_one"
		assert renderer._category_for("Hook Bottleneck") == "slow_hook"
		assert renderer._category_for("Hot Line") == "hot_line"
		assert renderer._category_for("Missing Index") == "missing_index"

	def test_unknown_or_blank_falls_through_to_other(self):
		assert renderer._category_for(None) == "other"
		assert renderer._category_for("") == "other"
		assert renderer._category_for("Brand New Finding") == "other"


class TestHeadlineByCategory:
	def test_n_plus_one_uses_loop_template(self):
		tldr = renderer._compose_tldr([_finding()], _doc())
		head = str(tldr["headline_markup"])
		assert "One line of code is responsible for" in head
		assert "100× inside a loop" in head
		# Highlight spans applied to the impact + loop count.
		assert head.count('<span class="hot">') >= 2

	def test_slow_hook_uses_hook_template(self):
		f = _finding(
			finding_type="Hook Bottleneck",
			title="In Submit, looped_validate hook consumed 705ms",
			estimated_impact_ms=705.0,
		)
		tldr = renderer._compose_tldr([f], _doc())
		head = str(tldr["headline_markup"])
		assert "doc-event hook" in head
		assert "slowest hook" in head
		assert "looped_validate" in head

	def test_slow_query_branch(self):
		f = _finding(
			finding_type="Slow Query",
			title="SELECT * FROM tabUser took 1234ms",
			estimated_impact_ms=1234.0,
		)
		tldr = renderer._compose_tldr([f], _doc())
		head = str(tldr["headline_markup"])
		assert "A single query took" in head
		assert "tabUser" in head

	def test_redundant_call_with_count(self):
		f = _finding(
			finding_type="Redundant Call",
			title="frappe.session.user fetched 50×",
			estimated_impact_ms=200.0,
			affected_count=50,
		)
		tldr = renderer._compose_tldr([f], _doc())
		head = str(tldr["headline_markup"])
		assert "Same call repeated" in head
		assert "50×" in head

	def test_fallback_branch_uses_verbatim_title(self):
		f = _finding(
			finding_type="Some Brand New Finding",
			title="Custom title goes here",
			estimated_impact_ms=300.0,
		)
		tldr = renderer._compose_tldr([f], _doc())
		head = str(tldr["headline_markup"])
		assert "Custom title goes here" in head
		# Even fallback emphasises the impact.
		assert '<span class="hot">' in head


class TestSorting:
	def test_picks_highest_severity_first(self):
		# Use the fallback finding_type so the headline includes the
		# title verbatim — sorting test doesn't care about the
		# category-specific prose, just which finding wins.
		low = _finding(finding_type="?", severity="Low", estimated_impact_ms=5000, title="LOW_FINDING")
		high = _finding(finding_type="?", severity="High", estimated_impact_ms=100, title="HIGH_FINDING")
		# Low has more impact, but High severity wins.
		tldr = renderer._compose_tldr([low, high], _doc())
		assert "HIGH_FINDING" in str(tldr["headline_markup"])
		assert "LOW_FINDING" not in str(tldr["headline_markup"])

	def test_breaks_severity_ties_by_impact(self):
		small = _finding(finding_type="?", severity="Medium", estimated_impact_ms=50, title="SMALL_FINDING")
		big = _finding(finding_type="?", severity="Medium", estimated_impact_ms=500, title="BIG_FINDING")
		tldr = renderer._compose_tldr([small, big], _doc())
		# big impact wins at same severity.
		assert "BIG_FINDING" in str(tldr["headline_markup"])
		assert "SMALL_FINDING" not in str(tldr["headline_markup"])


class TestEmptyState:
	def test_no_findings_yields_clean_session_branch(self):
		tldr = renderer._compose_tldr([], _doc())
		head = str(tldr["headline_markup"])
		assert tldr["label"] == "Clean session"
		assert "Nothing to fix" in head
		# No signal red in the clean branch.
		assert '<span class="hot">' not in head

	def test_empty_sub_line_drops_severity_phrase(self):
		tldr = renderer._compose_tldr([], _doc())
		sub = str(tldr["sub_markup"])
		assert "severity finding" not in sub
		# But session totals stay.
		assert "Session total" in sub
		assert "20 operations" in sub
		assert "1868 DB queries" in sub


class TestSubLine:
	def test_sub_line_counts_same_severity_findings(self):
		findings = [
			_finding(severity="High"),
			_finding(severity="High"),
			_finding(severity="Medium"),
		]
		tldr = renderer._compose_tldr(findings, _doc())
		sub = str(tldr["sub_markup"])
		# Top finding is High; sub line counts other High findings (2).
		assert "2 high-severity findings" in sub

	def test_sub_line_singular_for_one_finding(self):
		tldr = renderer._compose_tldr([_finding()], _doc())
		sub = str(tldr["sub_markup"])
		assert "1 high-severity finding." in sub
		assert "findings." not in sub  # singular, no plural 's'


class TestMarkupSafety:
	def test_returns_markup_so_jinja_does_not_escape_spans(self):
		tldr = renderer._compose_tldr([_finding()], _doc())
		assert isinstance(tldr["headline_markup"], Markup)
		assert isinstance(tldr["sub_markup"], Markup)

	def test_user_supplied_title_is_html_escaped(self):
		"""Finding titles come from analyzer output but can carry
		user-controlled content (e.g. DocType names, action labels).
		Markup.format() escapes plain-string args — confirm a stray
		<script> in the title is escaped, not interpolated raw."""
		f = _finding(
			finding_type="Hook Bottleneck",
			title="<script>alert('xss')</script>",
			estimated_impact_ms=500.0,
		)
		tldr = renderer._compose_tldr([f], _doc())
		head = str(tldr["headline_markup"])
		assert "<script>alert" not in head
		assert "&lt;script&gt;" in head


class TestThresholdRespected:
	def test_below_threshold_keeps_ms_unit(self):
		f = _finding(estimated_impact_ms=800, affected_count=50)
		tldr = renderer._compose_tldr(
			[f], _doc(total_duration_ms=800),
			large_duration_threshold_ms=1000.0,
		)
		head = str(tldr["headline_markup"])
		assert "800ms" in head
		assert "time-high" not in head

	def test_above_threshold_renders_seconds(self):
		f = _finding(estimated_impact_ms=2000, affected_count=50)
		tldr = renderer._compose_tldr(
			[f], _doc(total_duration_ms=5000),
			large_duration_threshold_ms=1000.0,
		)
		head = str(tldr["headline_markup"])
		assert '<span class="time-high">2.00s</span>' in head
