# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for ``optimus.report_context.build_report_context`` — the Phase J.1
adapter that turns our flat 45-key render-context into the 19-key contract
shape per ``template_variable_contract.md`` (reference design package).

Phase J.1 verification: presence of all 19 top-level keys + minimal per-key
shape conformance. Deep correctness (display strings, edge cases) is verified
in Phase J.2 when the template starts consuming the new shape.
"""

import json
from types import SimpleNamespace

from optimus import report_context
from optimus.report_context import (
	_bar_kind_for,
	_is_user_code,
	_web_vital_class,
	build_report_context,
)

# Top-level keys the contract specifies, in document order.
CONTRACT_KEYS = (
	"session", "tldr", "kpis", "repro", "summary", "findings",
	"line_drilldown_runs", "action_plan", "waterfall", "actions",
	"background_jobs", "doc_events", "resource", "frontend",
	"hot_frames", "slow_queries", "db", "how_to_read_items", "footer",
)


def _doc(**overrides):
	defaults = dict(
		name="PS-test",
		session_uuid="uuid-0017",
		user="navin@aerele.in",
		started_at="2026-05-14T18:12:59",
		stopped_at="2026-05-14T18:13:15",
		total_duration_ms=4780,
		total_queries=1868,
		total_query_time_ms=778,
		phase_2_runs=[],
	)
	defaults.update(overrides)
	return SimpleNamespace(**defaults)


def _ctx(**overrides):
	defaults = dict(
		fmt_dt=lambda v: str(v) if v else "",
		fmt_ms=lambda v, **kw: f"{v:.0f} ms",
		server_tz="IST",
		generated_at="2026-05-15 13:12",
		actions=[],
		findings=[],
		tldr=None,
		notes_html=None,
		summary_html=None,
		action_plan=[],
		waterfall_rows=[],
		background_jobs={"jobs": []},
		doc_event_breakdown={},
		infra_summary={},
		infra_timeline=[],
		frontend_vitals_by_page={},
		frontend_xhr_matched=[],
		frontend_summary={},
		frontend_orphans=[],
		hot_frames_rows=[],
		ignored_apps=("frappe", "erpnext"),
		top_queries=[],
		table_breakdown=[],
		render_config={},
	)
	defaults.update(overrides)
	return defaults


# ----- helpers ------------------------------------------------------------


class TestWebVitalClass:
	def test_none_or_zero_returns_vital_none(self):
		assert _web_vital_class(None, 1800, 3000) == "vital-none"
		assert _web_vital_class(0, 1800, 3000) == "vital-none"

	def test_below_good_threshold_returns_vital_good(self):
		assert _web_vital_class(420, 1800, 3000) == "vital-good"

	def test_between_thresholds_returns_vital_meh(self):
		assert _web_vital_class(2000, 1800, 3000) == "vital-meh"

	def test_above_poor_threshold_returns_vital_poor(self):
		assert _web_vital_class(3500, 1800, 3000) == "vital-poor"

	def test_cls_thresholds(self):
		# CLS uses fractional thresholds.
		assert _web_vital_class(0.05, 0.1, 0.25) == "vital-good"
		assert _web_vital_class(0.15, 0.1, 0.25) == "vital-meh"
		assert _web_vital_class(0.3, 0.1, 0.25) == "vital-poor"


class TestBarKindFor:
	def test_above_one_second_returns_none_default_red(self):
		assert _bar_kind_for(1500) is None
		assert _bar_kind_for(1000) is None

	def test_between_300_and_1000_returns_warn(self):
		assert _bar_kind_for(300) == "warn"
		assert _bar_kind_for(800) == "warn"

	def test_below_300_returns_ok(self):
		assert _bar_kind_for(0) == "ok"
		assert _bar_kind_for(299) == "ok"

	def test_none_returns_ok(self):
		assert _bar_kind_for(None) == "ok"


class TestIsUserCode:
	def test_user_code_returns_true(self):
		assert _is_user_code("ugly_code/python/common.py", ("frappe", "erpnext"))
		assert _is_user_code("ugly_code.python.common.bg_recheck_users", ("frappe", "erpnext"))

	def test_framework_code_returns_false(self):
		assert not _is_user_code("frappe/desk/form/save.py", ("frappe", "erpnext"))
		assert not _is_user_code("erpnext.accounts.doctype.x", ("frappe", "erpnext"))

	def test_empty_input_returns_false(self):
		assert not _is_user_code("", ("frappe",))
		assert not _is_user_code(None, ("frappe",))


# ----- top-level shape ----------------------------------------------------


class TestTopLevelKeys:
	def test_all_19_contract_keys_present(self):
		# Subset rather than equality — pragmatic non-contract extensions
		# like ``actions_framework`` / ``background_jobs_framework`` from
		# J.2.3 are permitted; the contract just specifies a minimum.
		out = build_report_context(_doc(), _ctx())
		missing = set(CONTRACT_KEYS) - set(out.keys())
		assert not missing, f"Missing contract keys: {missing}"

	def test_empty_session_renders_without_error(self):
		# All keys produced, optional ones may be None.
		out = build_report_context(_doc(), _ctx())
		assert out["session"]["id"] == "uuid-0017"
		assert out["kpis"] is not None and len(out["kpis"]) == 4
		assert out["footer"]["framework"] == "Frappe v16"


# ----- per-key shape conformance ------------------------------------------


class TestSessionShape:
	def test_session_has_contract_fields(self):
		out = build_report_context(
			_doc(session_uuid="uuid-X", user="alice@example.com"), _ctx(server_tz="UTC")
		)
		s = out["session"]
		assert s["id"] == "uuid-X"
		assert s["recorded_by"] == "alice@example.com"
		assert s["timezone"] == "UTC"
		assert "started_at" in s
		assert "ended_at" in s
		assert "generated_at" in s


class TestTldrShape:
	def test_renames_markup_keys_to_html(self):
		tldr_in = {
			"label": "headline",
			"headline_markup": "<span class='hot'>449 ms</span> hot",
			"sub_markup": "Session total 4.78s",
		}
		out = build_report_context(_doc(), _ctx(tldr=tldr_in))
		assert "<span class='hot'>" in out["tldr"]["headline_html"]
		assert "Session total" in out["tldr"]["subline_html"]

	def test_missing_tldr_returns_empty_strings(self):
		out = build_report_context(_doc(), _ctx(tldr=None))
		assert out["tldr"]["headline_html"] == ""
		assert out["tldr"]["subline_html"] == ""


class TestKpisShape:
	def test_kpis_are_exactly_4(self):
		out = build_report_context(_doc(), _ctx())
		assert len(out["kpis"]) == 4

	def test_kpi_items_have_required_fields(self):
		out = build_report_context(_doc(), _ctx())
		for kpi in out["kpis"]:
			assert "label" in kpi
			assert "value" in kpi
			assert "sub" in kpi
			assert "is_danger" in kpi

	def test_kpi_labels_match_template_convention(self):
		# Labels mirror the pre-J.2 template strings so the tested-text
		# surface doesn't move under us.
		out = build_report_context(_doc(), _ctx())
		labels = [k["label"] for k in out["kpis"]]
		assert labels == ["Total time", "Database queries", "Operations", "Issues found"]

	def test_total_time_kpi_uses_fmt_ms(self):
		# fmt_ms comes from the ctx so the contract honours the renderer's
		# threshold-aware ms/s formatting.
		out = build_report_context(_doc(total_duration_ms=4780), _ctx(
			fmt_ms=lambda v, **kw: "4.78s" if v == 4780 else f"{v:.0f}ms",
		))
		first = out["kpis"][0]
		assert first["value"] == "4.78s"
		assert "server" in first["sub"] and "DB" in first["sub"]

	def test_issues_found_sub_renders_severity_breakdown(self):
		findings = [
			{"severity": "High", "title": "x"},
			{"severity": "High", "title": "y"},
			{"severity": "Low", "title": "z"},
		]
		out = build_report_context(_doc(), _ctx(
			findings=findings,
			all_findings=findings,
			severity_counts={"High": 2, "Medium": 0, "Low": 1},
		))
		assert "2 high" in out["kpis"][3]["sub"]
		assert "1 low" in out["kpis"][3]["sub"]
		assert out["kpis"][3]["is_danger"]

	def test_issues_found_sub_falls_back_to_none_detected(self):
		out = build_report_context(_doc(), _ctx(
			findings=[], all_findings=[], severity_counts={"High": 0, "Medium": 0, "Low": 0},
		))
		assert out["kpis"][3]["sub"] == "none detected"
		assert not out["kpis"][3]["is_danger"]


class TestReproShape:
	def test_none_when_no_notes(self):
		assert build_report_context(_doc(), _ctx(notes_html=None))["repro"] is None

	def test_exposes_raw_html_when_present(self):
		out = build_report_context(_doc(), _ctx(notes_html="<ol><li>step1</li></ol>"))
		assert out["repro"]["raw_html"] == "<ol><li>step1</li></ol>"


class TestSummaryShape:
	def test_none_when_no_summary_html(self):
		assert build_report_context(_doc(), _ctx(summary_html=None))["summary"] is None

	def test_wraps_summary_html_as_paragraph(self):
		out = build_report_context(_doc(), _ctx(summary_html="<ul><li>foo</li></ul>"))
		assert out["summary"]["paragraphs_html"] == ["<ul><li>foo</li></ul>"]


class TestFindingsShape:
	def _f(self, **kw):
		base = {
			"finding_type": "Slow Hot Path",
			"severity": "High",
			"title": "x is slow",
			"customer_description": "...",
			"estimated_impact_ms": 449,
			"affected_count": 100,
			"action_ref": "1",
			"technical_detail": {
				"callsite": {
					"filename": "ugly_code/python/common.py",
					"lineno": 20,
					"function": "_check_user_exists",
					"source_snippet": [
						{"lineno": 19, "content": "    for i in range(50):"},
						{"lineno": 20, "content": "        user = frappe.get_doc('User', user)"},
					],
				},
			},
			"llm_fix": None,
		}
		base.update(kw)
		return base

	def test_severity_normalized_to_lowercase_short_codes(self):
		findings = [self._f(severity="High"), self._f(severity="Medium"), self._f(severity="Low")]
		out = build_report_context(_doc(), _ctx(findings=findings))
		assert out["findings"][0]["severity"] == "high"
		assert out["findings"][1]["severity"] == "med"
		assert out["findings"][2]["severity"] == "low"

	def test_finding_has_contract_fields(self):
		out = build_report_context(_doc(), _ctx(findings=[self._f()]))
		f = out["findings"][0]
		expected_keys = {
			"severity", "title_html", "file_line", "impact_display",
			"impact_sub", "smoking_label", "smoking_code_html",
			"smoking_footnote_html", "chain", "ai_fix",
		}
		assert expected_keys.issubset(set(f.keys()))

	def test_smoking_code_html_wraps_target_line_in_hot_line_span(self):
		out = build_report_context(_doc(), _ctx(findings=[self._f()]))
		smoking = out["findings"][0]["smoking_code_html"]
		assert '<span class="hot-line">' in smoking
		assert '<span class="ln">20</span>' in smoking

	def test_smoking_code_html_drops_blank_context_lines(self):
		# Regression: Phase I.5 blank-line skip should be preserved.
		finding = self._f()
		finding["technical_detail"]["callsite"]["source_snippet"] = [
			{"lineno": 18, "content": ""},
			{"lineno": 19, "content": "    for i in range(50):"},
			{"lineno": 20, "content": "        user = frappe.get_doc(...)"},
		]
		out = build_report_context(_doc(), _ctx(findings=[finding]))
		smoking = out["findings"][0]["smoking_code_html"]
		assert '<span class="ln">18</span>' not in smoking
		assert '<span class="ln">19</span>' in smoking


class TestPhase2RunsShape:
	def _phase2_run(self, *, status="Ready", picks=None, results=None):
		return SimpleNamespace(
			run_uuid="r1",
			status=status,
			started_at="2026-05-14T18:15:21",
			total_ms=1585.22,
			picks_json=json.dumps(picks or []),
			results_json=json.dumps(results or []),
		)

	def test_empty_when_no_runs(self):
		out = build_report_context(_doc(phase_2_runs=[]), _ctx())
		assert out["line_drilldown_runs"] == []

	def test_skips_non_ready_runs(self):
		out = build_report_context(
			_doc(phase_2_runs=[self._phase2_run(status="Failed")]), _ctx()
		)
		assert out["line_drilldown_runs"] == []

	def test_ready_run_has_contract_fields(self):
		picks = [{"dotted_path": "x.y.fn", "source": "curated"}]
		results = [{
			"dotted_path": "x.y.fn",
			"qualname": "fn",
			"file": "/abs/x.py",
			"lines": [
				{"lineno": 1, "content": "def fn():", "hits": 1, "total_ms": 10.0, "per_hit_us": 10000},
			],
		}]
		out = build_report_context(
			_doc(phase_2_runs=[self._phase2_run(picks=picks, results=results)]), _ctx()
		)
		run = out["line_drilldown_runs"][0]
		assert run["number"] == 1
		assert run["status"] == "Ready"
		assert run["picks"] == ["x.y.fn"]
		assert len(run["functions"]) == 1
		fn = run["functions"][0]
		assert fn["qualified_name"] == "x.y.fn"
		assert fn["indent"] == 0
		assert len(fn["lines"]) == 1


class TestActionPlanShape:
	def test_step_has_contract_fields(self):
		ap_in = [{
			"n": 1, "title": "Optimise X", "desc": "Loop fix",
			"gain_ms": 1414.18, "gain_label": "est. saving",
			"callsite": "x.py:13", "finding_type": "Slow Hot Path",
		}]
		out = build_report_context(_doc(), _ctx(action_plan=ap_in))
		step = out["action_plan"][0]
		assert step["number"] == 1
		assert step["title_html"] == "Optimise X"
		assert step["description_html"] == "Loop fix"
		assert step["savings_display"] == "−1414 ms"
		assert step["savings_label"] == "est. saving"


class TestWaterfallShape:
	def test_row_has_contract_fields(self):
		# Real fmt_ms switches to seconds at threshold; fixture uses one
		# that mirrors that behaviour so the display test is meaningful.
		rows = [{"name": "savedocs", "duration_ms": 1430, "pct": 100.0, "hot": True, "bg": False}]
		out = build_report_context(_doc(), _ctx(
			waterfall_rows=rows,
			fmt_ms=lambda v, **kw: f"{v / 1000:.2f}s" if v >= 1000 else f"{v:.0f}ms",
		))
		row = out["waterfall"][0]
		assert row["name"] == "savedocs"
		assert row["width_pct"] == 100.0
		assert row["kind"] == "hot"
		assert row["is_hot_text"] is True
		assert row["duration_display"] == "1.43s"

	def test_bg_kind_when_bg_flag_set(self):
		rows = [{"name": "job", "duration_ms": 500, "pct": 50, "hot": False, "bg": True}]
		out = build_report_context(_doc(), _ctx(waterfall_rows=rows))
		assert out["waterfall"][0]["kind"] == "bg"


class TestActionsShape:
	def test_action_has_contract_fields(self):
		actions = [{
			"action_label": "save", "event_type": "HTTP Request", "http_method": "POST",
			"path": "/api/method/save", "duration_ms": 1430, "queries_count": 828,
			"query_time_ms": 322,
		}]
		out = build_report_context(_doc(), _ctx(actions=actions))
		a = out["actions"][0]
		assert a["number"] == 1
		assert a["name"] == "save"
		assert a["kind"] == "http"
		assert a["bar_kind"] is None  # ≥1000ms → default red
		assert a["duration_is_hot"]

	def test_bg_kind_for_background_job_event(self):
		actions = [{"action_label": "j", "event_type": "RQ Job", "duration_ms": 100}]
		out = build_report_context(_doc(), _ctx(actions=actions))
		assert out["actions"][0]["kind"] == "bg"

	def test_finding_inline_html_when_action_linked(self):
		findings = [{"severity": "High", "title": "Slow loop", "action_ref": "1"}]
		actions = [{"action_label": "save", "duration_ms": 1430}]
		out = build_report_context(_doc(), _ctx(actions=actions, findings=findings))
		assert "Slow loop" in (out["actions"][0]["finding_inline_html"] or "")


class TestBackgroundJobsShape:
	def test_job_has_contract_fields(self):
		jobs = {"jobs": [{
			"method": "bg_recheck_users",
			"duration_ms": 799,
			"queries_count": 111,
			"query_time_ms": 61,
			"findings_count": 1,
			"entry_callsite": {"filename": "x.py", "lineno": 199},
		}]}
		out = build_report_context(_doc(), _ctx(background_jobs=jobs))
		j = out["background_jobs"][0]
		assert j["number"] == 1
		assert j["name"] == "bg_recheck_users"
		assert j["queries"] == 111
		assert j["finding_count"] == 1
		assert j["bar_kind"] == "warn"  # 799ms in [300, 1000)


class TestDocEventsShape:
	def test_doctype_has_contract_fields(self):
		breakdown = {"doctypes": [{
			"doctype": "Sales Invoice",
			"method_count": 1,
			"total_ms": 737,
			"events": [{
				"event": "validate",
				"total_ms": 737,
				"methods": [{
					"function": "looped_validate",
					"filename": "x.py", "lineno": 6,
					"ms": 737, "kind": "doc_events hook",
				}],
			}],
		}]}
		out = build_report_context(_doc(), _ctx(doc_event_breakdown=breakdown))
		de = out["doc_events"][0]
		assert de["name"] == "Sales Invoice"
		assert "hot method" in de["summary"]
		assert de["methods"][0]["name"] == "validate"
		assert de["methods"][0]["hooks"][0]["name"] == "looped_validate"


class TestResourceShape:
	def test_none_when_no_infra(self):
		assert build_report_context(_doc(), _ctx())["resource"] is None

	def test_cpu_card_with_peak(self):
		infra = {"cpu_avg": 15, "cpu_peak": 62.5}
		out = build_report_context(_doc(), _ctx(infra_summary=infra))
		card = out["resource"]["cards"][0]
		assert card["label"] == "CPU avg / peak"
		assert card["value_kind"] == "warn"  # 62.5 ≥ 50

	def test_swap_card_with_activity(self):
		infra = {"swap_peak_mb": 1149}
		out = build_report_context(_doc(), _ctx(infra_summary=infra))
		# Find the Swap card
		swap_card = next(c for c in out["resource"]["cards"] if c["label"] == "Swap peak")
		assert swap_card["value_kind"] == "warn"
		assert swap_card["sub_is_warn"]


class TestFrontendShape:
	def test_none_when_no_frontend(self):
		assert build_report_context(_doc(), _ctx())["frontend"] is None

	def test_web_vital_classes_computed(self):
		vitals = {"/p": {"fcp_ms": 420, "lcp_ms": 5000, "cls": 0.15, "ttfb_ms": 180, "dom_content_loaded_ms": 890}}
		out = build_report_context(_doc(), _ctx(frontend_vitals_by_page=vitals))
		row = out["frontend"]["web_vitals"][0]
		assert row["fcp_class"] == "vital-good"
		assert row["lcp_class"] == "vital-poor"
		assert row["cls_class"] == "vital-meh"
		assert row["ttfb_class"] == "vital-good"

	def test_partial_vitals_gets_none_class(self):
		# Regression of the Phase I.5 production crash data shape.
		vitals = {"/p": {"cls": 0.135}}
		out = build_report_context(_doc(), _ctx(frontend_vitals_by_page=vitals))
		row = out["frontend"]["web_vitals"][0]
		assert row["fcp_class"] == "vital-none"
		assert row["fcp_display"] == "—"
		assert row["cls_class"] == "vital-meh"


class TestHotFramesShape:
	def test_user_code_flag_set_for_app_code(self):
		rows = [{"display_name": "ugly_code/python/common.py::looped_validate", "total_ms": 1414, "occurrences": 2, "distinct_actions": 2}]
		out = build_report_context(_doc(), _ctx(hot_frames_rows=rows, ignored_apps=("frappe", "erpnext")))
		assert out["hot_frames"][0]["is_user_code"]

	def test_user_code_flag_false_for_framework_code(self):
		rows = [{"display_name": "frappe/desk/form/save.py::savedocs", "total_ms": 2045, "occurrences": 2, "distinct_actions": 2}]
		out = build_report_context(_doc(), _ctx(hot_frames_rows=rows, ignored_apps=("frappe", "erpnext")))
		assert not out["hot_frames"][0]["is_user_code"]


class TestSlowQueriesShape:
	def test_empty_list_when_no_queries(self):
		assert build_report_context(_doc(), _ctx())["slow_queries"] == []

	def test_query_has_contract_fields(self):
		queries = [{"normalized_query": "SELECT * FROM tabUser", "total_ms": 65, "count": 163, "callsite": "x.py:20"}]
		out = build_report_context(_doc(), _ctx(top_queries=queries))
		q = out["slow_queries"][0]
		assert q["sql_excerpt"] == "SELECT * FROM tabUser"
		assert q["call_count"] == 163
		assert q["callsite"] == "x.py:20"


class TestDbShape:
	def test_none_when_no_tables(self):
		assert build_report_context(_doc(), _ctx())["db"] is None

	def test_table_has_contract_fields(self):
		tables = [{
			"table": "tabUser", "duration_ms": 65, "queries": 163,
			"read_count": 163, "write_count": 0,
			"recommended_index": None,
		}]
		out = build_report_context(_doc(), _ctx(table_breakdown=tables))
		t = out["db"]["tables"][0]
		assert t["name"] == "tabUser"
		assert t["queries"] == 163
		assert t["is_hot"]  # 163 ≥ 100

	def test_index_recommendation_built_when_present(self):
		tables = [{
			"table": "tabCommunication", "duration_ms": 15,
			"queries": 2, "read_count": 2, "write_count": 0,
			"recommended_index": {
				"columns": ["communication_type", "reference_doctype"],
				"doctype": "Communication",
			},
		}]
		out = build_report_context(_doc(), _ctx(table_breakdown=tables))
		recs = out["db"]["index_recommendations"]
		assert len(recs) == 1
		assert recs[0]["table_name"] == "tabCommunication"
		assert "communication_type" in recs[0]["recommendation_html"]
		assert 'frappe.db.add_index("Communication"' in recs[0]["sql"]


class TestFooterShape:
	def test_footer_settings_compose_from_render_config(self):
		rc = {
			"hide_framework_tables": True,
			"ignored_apps": ("frappe", "erpnext"),
			"ai_suggest_findings": True,
			"ai_suggest_indexes": True,
		}
		out = build_report_context(_doc(), _ctx(render_config=rc))
		s = out["footer"]["settings"]
		assert "hide_framework_tables=on" in s
		assert "ignored_apps=frappe, erpnext" in s
		assert "ai_suggest_findings=on" in s

	def test_footer_framework_label(self):
		out = build_report_context(_doc(), _ctx())
		assert out["footer"]["framework"] == "Frappe v16"


class TestHowToReadItems:
	def test_omitted_for_now(self):
		# J.1 leaves how_to_read_items=None; template falls back to default.
		out = build_report_context(_doc(), _ctx())
		assert out["how_to_read_items"] is None
