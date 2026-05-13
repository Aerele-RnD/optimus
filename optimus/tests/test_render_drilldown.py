# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-level test for the v0.6.x call-tree drill-down on finding cards.

Builds a SimpleNamespace doc with one slow action carrying a hand-crafted
``call_tree_json`` and a Slow-Hot-Path finding whose callsite is the
``looped_validate`` frame. Rendered HTML must contain a *Drill-down:*
block that walks one or two user-code frames below and STOPS at the
framework boundary."""

import json
import types


def _tree_for_screenshot():
	"""Mirrors the user's screenshot scenario."""
	return {
		"function": "<root>", "filename": "", "lineno": 0,
		"self_ms": 0.0, "cumulative_ms": 689.0,
		"children": [
			{
				"function": "looped_validate",
				"filename": "apps/ugly_code/ugly_code/python/common.py",
				"lineno": 6, "self_ms": 5.0, "cumulative_ms": 689.0,
				"children": [
					{
						"function": "_run_validations",
						"filename": "apps/ugly_code/ugly_code/python/common.py",
						"lineno": 15, "self_ms": 10.0, "cumulative_ms": 620.0,
						"children": [
							{
								"function": "_check_user_exists",
								"filename": "apps/ugly_code/ugly_code/python/common.py",
								"lineno": 19, "self_ms": 30.0, "cumulative_ms": 530.0,
								"children": [
									{
										"function": "get_doc",
										"filename": "apps/frappe/frappe/model/document.py",
										"lineno": 42, "self_ms": 500.0, "cumulative_ms": 520.0,
										"children": [],
									},
								],
							},
						],
					},
				],
			},
		],
	}


def _finding(callsite_filename, callsite_function, callsite_lineno, action_ref="0"):
	return types.SimpleNamespace(
		finding_type="Slow Hot Path",
		severity="High",
		title="In frappe.desk.form.save.savedocs:Save, 67% of the time was spent in looped_validate",
		customer_description="",
		estimated_impact_ms=689.0,
		affected_count=1,
		action_ref=action_ref,
		technical_detail_json=json.dumps({
			"callsite": {
				"filename": callsite_filename,
				"lineno": callsite_lineno,
				"function": callsite_function,
			},
			"cumulative_ms": 689.0,
		}),
		llm_fix_json=None,
	)


def _action(action_label, recording_uuid, duration_ms, call_tree):
	return types.SimpleNamespace(
		action_label=action_label,
		event_type="HTTP Request",
		http_method="POST",
		path="/api/method/frappe.desk.form.save.savedocs",
		recording_uuid=recording_uuid,
		duration_ms=duration_ms,
		queries_count=0,
		query_time_ms=0,
		slowest_query_ms=0,
		call_tree_json=json.dumps(call_tree),
	)


def _doc(actions, findings):
	return types.SimpleNamespace(
		name="PS-dd", session_uuid="dd-uuid", title="drill-down test",
		user="a@example.com", status="Ready",
		started_at="2026-05-13T00:00:00", stopped_at="2026-05-13T00:00:01",
		notes=None, top_severity="High", summary_html=None,
		total_duration_ms=1199, total_query_time_ms=600,
		total_queries=50, total_requests=1,
		top_queries_json="[]", table_breakdown_json="[]",
		hot_frames_json="[]", session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions, findings=findings, phase_2_runs=[],
	)


class TestDrilldownRender:
	def test_drilldown_block_appears_under_finding_card(self):
		from optimus import renderer

		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/frappe.desk.form.save.savedocs",
				recording_uuid="r0", duration_ms=1199,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[_finding(
				callsite_filename="apps/ugly_code/ugly_code/python/common.py",
				callsite_function="looped_validate",
				callsite_lineno=6,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])

		# The drill-down label appears on the card.
		assert '<strong style="color: #1d4ed8;">Drill-down:</strong>' in html
		# Both user-code frames below looped_validate are surfaced.
		assert "_run_validations" in html
		assert "_check_user_exists" in html
		# Framework frame (frappe.../get_doc) does NOT appear in the drill-down.
		# (It might appear elsewhere in the report if framework data is
		# rendered, but for the drill-down block we just verify the chain
		# stops before it.)
		# The drill-down block lives inside the smoking-gun container, so
		# locate it and assert the framework path isn't inside that span.
		dd_idx = html.find("Drill-down:")
		assert dd_idx > 0
		dd_segment = html[dd_idx:dd_idx + 4000]
		assert "frappe/frappe/model/document.py" not in dd_segment, (
			"framework frame leaked into the drill-down block"
		)
		# Percentages render as "% of parent".
		assert "% of parent" in html

	def test_finding_without_matching_tree_node_skips_drilldown(self):
		from optimus import renderer

		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/foo",
				recording_uuid="r0", duration_ms=500,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[_finding(
				callsite_filename="apps/myapp/other.py",
				callsite_function="totally_different_function",
				callsite_lineno=1,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])

		# Drill-down label is absent — no matching origin in the tree.
		assert "Drill-down:" not in html

	def test_finding_without_action_ref_skips_drilldown(self):
		from optimus import renderer

		f = _finding(
			callsite_filename="apps/ugly_code/ugly_code/python/common.py",
			callsite_function="looped_validate",
			callsite_lineno=6,
			action_ref="",  # no action to look up
		)
		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/foo",
				recording_uuid="r0", duration_ms=500,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[f],
		)
		html = renderer.render_raw(doc, recordings=[])

		assert "Drill-down:" not in html

	def test_action_without_call_tree_skips_drilldown(self):
		from optimus import renderer

		action = _action(
			action_label="POST /api/method/foo",
			recording_uuid="r0", duration_ms=500,
			call_tree={},  # empty tree
		)
		# Override to make call_tree_json empty string (more realistic for
		# pre-v0.3.0 sessions where the field didn't exist).
		action.call_tree_json = ""
		doc = _doc(
			actions=[action],
			findings=[_finding(
				callsite_filename="apps/ugly_code/ugly_code/python/common.py",
				callsite_function="looped_validate",
				callsite_lineno=6,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])
		assert "Drill-down:" not in html
