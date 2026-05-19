# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for `renderer._build_waterfall` — the v0.7.x redesign Phase
C horizontal-bar action timeline.

Top-N actions by duration, scaled to the displayed slice's max so
short actions stay visible. Bar colour:
- `hot` when a High-severity finding points at this action.
- `bg` when the action is a background job.
- otherwise: default (info blue).
"""

from optimus import renderer


def _a(idx, **kw):
	base = {
		"idx": idx,
		"action_label": f"action_{idx}",
		"event_type": "HTTP Request",
		"duration_ms": 100.0,
	}
	base.update(kw)
	return base


class TestEmpty:
	def test_no_actions_returns_empty_list(self):
		assert renderer._build_waterfall([], []) == []

	def test_actions_all_zero_duration_returns_empty(self):
		actions = [_a(0, duration_ms=0), _a(1, duration_ms=0)]
		assert renderer._build_waterfall(actions, []) == []


class TestSizing:
	def test_returns_at_most_eight_rows_by_default(self):
		actions = [_a(i, duration_ms=float(i + 1)) for i in range(20)]
		out = renderer._build_waterfall(actions, [])
		assert len(out) == 8

	def test_max_rows_override(self):
		actions = [_a(i, duration_ms=float(i + 1)) for i in range(10)]
		out = renderer._build_waterfall(actions, [], max_rows=5)
		assert len(out) == 5

	def test_returns_fewer_when_input_is_smaller(self):
		actions = [_a(0, duration_ms=100), _a(1, duration_ms=50)]
		out = renderer._build_waterfall(actions, [])
		assert len(out) == 2


class TestSorting:
	def test_sorted_by_duration_descending(self):
		actions = [
			_a(0, action_label="slow", duration_ms=900),
			_a(1, action_label="fastest", duration_ms=50),
			_a(2, action_label="medium", duration_ms=400),
		]
		out = renderer._build_waterfall(actions, [])
		assert [r["name"] for r in out] == ["slow", "medium", "fastest"]


class TestPctScaling:
	def test_top_row_renders_at_100_percent(self):
		actions = [
			_a(0, duration_ms=1000),
			_a(1, duration_ms=500),
			_a(2, duration_ms=100),
		]
		out = renderer._build_waterfall(actions, [])
		assert out[0]["pct"] == 100.0
		assert out[1]["pct"] == 50.0
		assert out[2]["pct"] == 10.0

	def test_pct_scales_to_displayed_slice_max(self):
		"""When max_rows=2 selects the top-2 from a larger list,
		scaling uses those two's max — not the (unshown) overall
		max — so the second row is comparable visually."""
		actions = [
			_a(0, duration_ms=200),
			_a(1, duration_ms=100),
			_a(2, duration_ms=10),  # would-be 5% on overall, hidden anyway
		]
		out = renderer._build_waterfall(actions, [], max_rows=2)
		assert out[0]["pct"] == 100.0
		assert out[1]["pct"] == 50.0


class TestHotFlag:
	def test_high_severity_finding_marks_action_hot(self):
		actions = [_a(0, duration_ms=900)]
		findings = [{"severity": "High", "action_ref": "0"}]
		out = renderer._build_waterfall(actions, findings)
		assert out[0]["hot"] is True

	def test_medium_severity_does_not_mark_hot(self):
		actions = [_a(0, duration_ms=900)]
		findings = [{"severity": "Medium", "action_ref": "0"}]
		out = renderer._build_waterfall(actions, findings)
		assert out[0]["hot"] is False

	def test_action_ref_mismatch_does_not_mark_hot(self):
		actions = [_a(0, duration_ms=900), _a(1, duration_ms=500)]
		findings = [{"severity": "High", "action_ref": "1"}]
		out = renderer._build_waterfall(actions, findings)
		by_idx = {r["name"]: r for r in out}
		assert by_idx["action_0"]["hot"] is False
		assert by_idx["action_1"]["hot"] is True


class TestBgFlag:
	def test_background_job_event_type_marks_bg(self):
		actions = [
			_a(0, duration_ms=900, event_type="RQ Job"),
			_a(1, duration_ms=500, event_type="HTTP Request"),
		]
		out = renderer._build_waterfall(actions, [])
		by_name = {r["name"]: r for r in out}
		assert by_name["action_0"]["bg"] is True
		assert by_name["action_1"]["bg"] is False

	def test_hot_overrides_bg_visually(self):
		"""Both flags can be True. Template colour priority is `hot`
		over `bg` (red wins). The data layer reports both honestly;
		the template renders the precedence."""
		actions = [_a(0, duration_ms=900, event_type="RQ Job")]
		findings = [{"severity": "High", "action_ref": "0"}]
		out = renderer._build_waterfall(actions, findings)
		assert out[0]["hot"] is True
		assert out[0]["bg"] is True
