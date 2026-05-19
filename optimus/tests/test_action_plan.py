# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for `renderer._build_action_plan` — the v0.7.x redesign
Phase C punch list.

Top-N findings by severity → impact, each emitted as a step the
template renders as a numbered `.action-step` row. Empty findings
input returns `[]` and the template hides the section."""

from optimus import renderer


def _f(**kw):
	base = {
		"finding_type": "N+1 Query",
		"severity": "High",
		"title": "Same query ran 50×",
		"customer_description": "A query repeats inside a loop.",
		"estimated_impact_ms": 500.0,
		"affected_count": 50,
		"technical_detail": {
			"callsite": {"filename": "apps/myapp/foo.py", "lineno": 42, "function": "f"},
		},
	}
	base.update(kw)
	return base


class TestEmpty:
	def test_no_findings_returns_empty_list(self):
		assert renderer._build_action_plan([]) == []


class TestSizing:
	def test_returns_at_most_three_steps_by_default(self):
		findings = [_f(estimated_impact_ms=float(i * 100)) for i in range(10)]
		out = renderer._build_action_plan(findings)
		assert len(out) == 3

	def test_max_steps_override(self):
		findings = [_f(estimated_impact_ms=float(i * 100)) for i in range(5)]
		out = renderer._build_action_plan(findings, max_steps=5)
		assert len(out) == 5

	def test_returns_fewer_when_input_is_smaller(self):
		out = renderer._build_action_plan([_f(), _f()])
		assert len(out) == 2


class TestSorting:
	def test_high_severity_beats_higher_impact_at_lower_severity(self):
		low_big = _f(severity="Low", estimated_impact_ms=5000, title="LOW")
		high_small = _f(severity="High", estimated_impact_ms=100, title="HIGH")
		out = renderer._build_action_plan([low_big, high_small])
		# High severity comes first.
		assert out[0]["title"].startswith("Eliminate") or "HIGH" in out[0]["title"]
		# More precisely: the first step's gain matches the High finding.
		assert out[0]["gain_ms"] == 100
		assert out[1]["gain_ms"] == 5000

	def test_within_severity_higher_impact_wins(self):
		small = _f(severity="Medium", estimated_impact_ms=50)
		big = _f(severity="Medium", estimated_impact_ms=500)
		out = renderer._build_action_plan([small, big])
		assert out[0]["gain_ms"] == 500
		assert out[1]["gain_ms"] == 50


class TestVerbTitles:
	def test_n_plus_one_uses_eliminate_verb(self):
		out = renderer._build_action_plan([_f(finding_type="N+1 Query")])
		assert out[0]["title"] == "Eliminate the N+1 query"

	def test_hook_bottleneck_uses_speed_up_verb(self):
		out = renderer._build_action_plan([_f(finding_type="Hook Bottleneck")])
		assert out[0]["title"] == "Speed up the doc-event hook"

	def test_missing_index_uses_add_verb(self):
		out = renderer._build_action_plan([_f(finding_type="Missing Index")])
		assert out[0]["title"] == "Add a database index"

	def test_unknown_finding_type_falls_back_to_verbatim_title(self):
		f = _f(finding_type="?", title="Some specific actionable headline")
		out = renderer._build_action_plan([f])
		assert out[0]["title"] == "Some specific actionable headline"


class TestStepShape:
	def test_step_carries_position_gain_and_callsite(self):
		out = renderer._build_action_plan([_f()])
		step = out[0]
		assert step["n"] == 1
		assert step["gain_ms"] == 500.0
		assert step["gain_label"] == "est. saving"
		assert step["callsite"] == "apps/myapp/foo.py:42"

	def test_step_desc_falls_back_to_title_when_description_blank(self):
		f = _f(customer_description="", title="Same query ran 50×")
		out = renderer._build_action_plan([f])
		assert out[0]["desc"] == "Same query ran 50×"

	def test_step_callsite_none_when_no_filename_or_lineno(self):
		f = _f(technical_detail={"callsite": {"filename": "", "lineno": 0}})
		out = renderer._build_action_plan([f])
		assert out[0]["callsite"] is None

	def test_step_callsite_none_when_callsite_missing_entirely(self):
		f = _f(technical_detail={})
		out = renderer._build_action_plan([f])
		assert out[0]["callsite"] is None

	def test_numbers_count_up_from_one(self):
		findings = [_f(estimated_impact_ms=float(i * 100)) for i in range(3)]
		out = renderer._build_action_plan(findings)
		assert [s["n"] for s in out] == [1, 2, 3]
