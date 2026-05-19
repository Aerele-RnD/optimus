# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.6.0 "RQ Jobs" report section.

`renderer.build_background_jobs` is a pure function (unit-tested directly);
the section rendering is exercised end-to-end via `renderer.render_raw` with
a `SimpleNamespace` fake Optimus Session doc (same pattern as
`test_table_breakdown.py::TestRenderedFrameworkColsNote`).
"""

import json
import types

from optimus import renderer


def _action(**kw):
	base = {
		"action_label": "",
		"event_type": "HTTP Request",
		"http_method": "",
		"path": "",
		"recording_uuid": "",
		"duration_ms": 0,
		"queries_count": 0,
		"query_time_ms": 0,
		"slowest_query_ms": 0,
	}
	base.update(kw)
	return types.SimpleNamespace(**base)


def _action_dict(idx, **kw):
	"""A `_action_to_dict`-shaped dict + the `idx` key render() adds."""
	d = renderer._action_to_dict(_action(**kw))
	d["idx"] = idx
	return d


def _doc(actions, findings=None):
	return types.SimpleNamespace(
		name="PS-bg", session_uuid="bg-uuid", title="bg test", user="a@example.com",
		status="Ready", started_at="2026-05-12T00:00:00", stopped_at="2026-05-12T00:00:05",
		notes=None, top_severity="Low", summary_html=None, total_duration_ms=5000,
		total_query_time_ms=80, total_queries=5, total_requests=2, top_queries_json="[]",
		table_breakdown_json="[]", hot_frames_json=None, session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None, analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions, findings=(findings or []), phase_2_runs=[],
	)


# --------------------------------------------------------------------------
# build_background_jobs — pure
# --------------------------------------------------------------------------

class TestBuildBackgroundJobs:
	def test_filters_to_background_jobs_only(self):
		actions = [
			_action_dict(0, action_label="POST /api/method/save", event_type="HTTP Request",
			             recording_uuid="r0", duration_ms=900),
			_action_dict(1, action_label="Job: myapp.tasks.digest", event_type="RQ Job",
			             path="myapp.tasks.digest", recording_uuid="r1", duration_ms=1200,
			             queries_count=8, query_time_ms=30, slowest_query_ms=12),
			_action_dict(2, action_label="Job: myapp.tasks.stock", event_type="RQ Job",
			             recording_uuid="r2", duration_ms=300, queries_count=2),
		]
		out = renderer.build_background_jobs(actions, {})
		assert out["count"] == 2
		assert {j["method"] for j in out["jobs"]} == {"myapp.tasks.digest", "myapp.tasks.stock"}
		assert out["total_ms"] == 1500
		assert out["total_queries"] == 10

	def test_empty_when_no_background_jobs(self):
		actions = [_action_dict(0, action_label="GET /app", event_type="HTTP Request")]
		out = renderer.build_background_jobs(actions, {})
		assert out["count"] == 0 and out["jobs"] == []
		assert out["total_ms"] == 0 and out["total_queries"] == 0

	def test_method_name_cleanup_and_fallbacks(self):
		actions = [
			_action_dict(0, action_label="Job: myapp.x.run", event_type="RQ Job", recording_uuid="a"),
			_action_dict(1, action_label="", event_type="RQ Job", path="myapp.y.go", recording_uuid="b"),
			_action_dict(2, action_label="", event_type="RQ Job", path="", recording_uuid="c"),
			_action_dict(3, action_label="", event_type="RQ Job", path="", recording_uuid="d"),
		]
		recs = {"c": {"cmd": "myapp.z.cmd"}}
		out = renderer.build_background_jobs(actions, recs)
		by_uuid = {j["recording_uuid"]: j["method"] for j in out["jobs"]}
		assert by_uuid["a"] == "myapp.x.run"      # "Job: " stripped
		assert by_uuid["b"] == "myapp.y.go"        # falls back to path
		assert by_uuid["c"] == "myapp.z.cmd"       # falls back to recording cmd
		assert by_uuid["d"] == "RQ Job"     # generic placeholder

	def test_sorted_by_duration_desc(self):
		actions = [
			_action_dict(0, action_label="Job: a", event_type="RQ Job", recording_uuid="a", duration_ms=100),
			_action_dict(1, action_label="Job: b", event_type="RQ Job", recording_uuid="b", duration_ms=900),
			_action_dict(2, action_label="Job: c", event_type="RQ Job", recording_uuid="c", duration_ms=400),
		]
		out = renderer.build_background_jobs(actions, {})
		assert [j["method"] for j in out["jobs"]] == ["b", "c", "a"]

	def test_top_queries_when_recording_present(self):
		actions = [_action_dict(0, action_label="Job: a", event_type="RQ Job", recording_uuid="a")]
		recs = {"a": {"calls": [
			{"index": 0, "duration": 5.0, "query": "Q0"},
			{"index": 1, "duration": 30.0, "query": "Q1", "exact_copies": 3},
			{"index": 2, "duration": 12.0, "query": "Q2"},
			{"index": 3, "duration": 1.0, "query": "Q3"},
			{"index": 4, "duration": 2.0, "query": "Q4"},
			{"index": 5, "duration": 9.0, "query": "Q5"},
		]}}
		out = renderer.build_background_jobs(actions, recs)
		job = out["jobs"][0]
		assert job["recording_available"] is True
		# Top 5 by duration, descending: Q1(30), Q2(12), Q5(9), Q0(5), Q4(2).
		assert [q["query"] for q in job["top_queries"]] == ["Q1", "Q2", "Q5", "Q0", "Q4"]
		assert job["top_queries"][0]["exact_copies"] == 3

	def test_no_top_queries_when_recording_absent(self):
		actions = [_action_dict(0, action_label="Job: a", event_type="RQ Job", recording_uuid="gone")]
		out = renderer.build_background_jobs(actions, {})  # recording not in the map
		job = out["jobs"][0]
		assert job["recording_available"] is False
		assert job["top_queries"] is None

	def test_findings_counted_per_job_by_action_ref(self):
		actions = [
			_action_dict(0, action_label="GET /app", event_type="HTTP Request", recording_uuid="r0"),
			_action_dict(1, action_label="Job: a", event_type="RQ Job", recording_uuid="r1", duration_ms=200),
			_action_dict(2, action_label="Job: b", event_type="RQ Job", recording_uuid="r2", duration_ms=100),
		]
		findings = [
			{"action_ref": "1"}, {"action_ref": "1"},  # two findings from job at idx 1
			{"action_ref": "0"},                         # one from the HTTP action — not a job
		]
		out = renderer.build_background_jobs(actions, {}, findings)
		assert out["any_findings_counted"] is True
		by_method = {j["method"]: j["findings_count"] for j in out["jobs"]}
		assert by_method["a"] == 2
		assert by_method["b"] == 0  # we did look (findings exist) — 0, not None

	def test_findings_count_none_when_no_action_refs(self):
		actions = [_action_dict(0, action_label="Job: a", event_type="RQ Job", recording_uuid="r1")]
		# Findings with no action_ref → can't be mapped.
		out = renderer.build_background_jobs(actions, {}, [{"action_ref": ""}, {}])
		assert out["any_findings_counted"] is False
		assert out["jobs"][0]["findings_count"] is None


# --------------------------------------------------------------------------
# section rendering (end-to-end via render_raw)
# --------------------------------------------------------------------------

class TestRenderedBackgroundJobsSection:
	def _job_action(self, **kw):
		kw.setdefault("event_type", "RQ Job")
		return _action(**kw)

	def test_section_renders_with_jobs(self):
		doc = _doc([
			_action(action_label="POST /api/method/save", event_type="HTTP Request",
			        http_method="POST", path="/api/method/save", recording_uuid="r0", duration_ms=900),
			self._job_action(action_label="Job: myapp.tasks.digest", path="myapp.tasks.digest",
			                 recording_uuid="r1", duration_ms=1200, queries_count=8,
			                 query_time_ms=30, slowest_query_ms=12),
		])
		recs = [{"uuid": "r1", "calls": [{"index": 0, "duration": 12.0, "query": "SELECT * FROM tabUser"}]}]
		html = renderer.render_raw(doc, recordings=recs)
		assert "<h2>RQ Jobs</h2>" in html
		# v0.7.x Phase E: the BG-job method renders as an
		# `.action-name` div (bold mono) instead of inline `<code>`.
		assert 'class="action-name">myapp.tasks.digest</div>' in html
		# summary line (HTML collapses the inter-token whitespace, so check
		# the pieces rather than an exact phrase).
		assert "1 RQ Job" in html
		assert "ran during this flow" in html
		# 1200ms > default threshold (1000ms) → renders as 1.20s wrapped
		# in the v0.7.x highlight span.
		assert '<span class="time-high">1.20s</span> total' in html
		# caveat about jobs that ran too late / no worker
		assert "Retry Analyze" in html
		# its query made it into the drill-down
		assert "SELECT * FROM tabUser" in html
		# jobs still appear in the per-action breakdown above (technical label)
		assert "Job: myapp.tasks.digest" in html

	def test_section_omitted_when_no_background_jobs(self):
		doc = _doc([
			_action(action_label="GET /app", event_type="HTTP Request", path="/app",
			        recording_uuid="r0", duration_ms=120),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>RQ Jobs</h2>" not in html

	def test_section_renders_without_recordings(self):
		# A re-render long after analyze: recordings expired from Redis. The
		# section still renders from the persisted action rows, with the
		# "recording expired" note instead of a query list.
		doc = _doc([
			self._job_action(action_label="Job: myapp.tasks.cleanup", path="myapp.tasks.cleanup",
			                 recording_uuid="r9", duration_ms=400, queries_count=3),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>RQ Jobs</h2>" in html
		assert "myapp.tasks.cleanup" in html
		assert "has expired from Redis" in html

	def test_findings_column_only_when_mappable(self):
		# With a finding carrying an action_ref that points at the job's
		# original index, the Findings column appears.
		# v0.7.x: the finding must also have a callsite — no-callsite
		# findings are filtered before render, so they wouldn't trigger
		# the Findings column either.
		doc = _doc(
			actions=[self._job_action(action_label="Job: a", path="a", recording_uuid="r1", duration_ms=10)],
			findings=[
				types.SimpleNamespace(
					finding_type="N+1 Query", severity="Medium", title="x",
					customer_description="", estimated_impact_ms=0, affected_count=0,
					action_ref="0",
					technical_detail_json=json.dumps({
						"callsite": {"filename": "apps/myapp/x.py", "lineno": 1, "function": "f"},
					}),
					llm_fix_json=None,
				)
			],
		)
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>RQ Jobs</h2>" in html
		assert "<th class=\"num\">Findings</th>" in html

	def test_smoking_gun_block_not_duplicated_into_bg_job_embed(self):
		"""v0.7.x: the styled smoking-gun panel (file:line header + source
		snippet + drill-down callout) is hidden when ``finding_card`` is
		embedded inside a BG-job row — the row already shows the entry
		callsite as a compact inline link under the method name, so the
		full panel would just duplicate that anchor inside a blue-bordered
		box. The canonical Findings section keeps it.

		Pin: ``class="smoking"`` appears exactly once across the
		whole report (the Findings-section card), never twice (Findings
		card + BG-job embed)."""
		doc = _doc(
			actions=[self._job_action(action_label="Job: a", path="a",
			                          recording_uuid="r1", duration_ms=10)],
			findings=[
				types.SimpleNamespace(
					finding_type="N+1 Query", severity="Medium", title="x",
					customer_description="", estimated_impact_ms=0, affected_count=0,
					action_ref="0",
					technical_detail_json=json.dumps({
						"callsite": {
							"filename": "apps/myapp/x.py", "lineno": 1, "function": "f",
						},
					}),
					llm_fix_json=None,
				)
			],
		)
		html = renderer.render_raw(doc, recordings=[])
		# Exactly one smoking-gun panel — the one in the Findings section.
		assert html.count('class="smoking"') == 1
		# Sanity: the BG-jobs section is rendered, and the related-finding
		# card was embedded under the job (title travels with the card).
		assert "<h2>RQ Jobs</h2>" in html
		# Two card-titles for "x": one in Findings section, one in BG embed.
		# (If embedding broke, the title count would drop to 1.)
		assert html.count(">x<") >= 2


# --------------------------------------------------------------------------
# v0.6.x: entry-point source location + ±1-line snippet under action/job rows
# --------------------------------------------------------------------------

class TestEntryCallsiteInReport:
	# Resolve a real function in this app so there's source to read.
	_DOTTED = "optimus.renderer.render"

	def test_background_job_row_does_not_show_entry_callsite_snippet(self):
		"""v0.7.x: the multi-line entry-callsite snippet PANEL is dropped
		from BG job rows. A compact inline ``file:line (function)`` line
		remains under the job method as a navigation affordance (added
		in a later iteration). The snippet panel — multi-line table,
		the def line itself rendered as a yellow-highlighted row — is
		what's absent."""
		doc = _doc([
			_action(action_label="Job: " + self._DOTTED, event_type="RQ Job",
			        path=self._DOTTED, recording_uuid="r1", duration_ms=500, queries_count=2),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>RQ Jobs</h2>" in html
		# Inline path IS present (compact, useful).
		assert "optimus/renderer.py:" in html
		# But the multi-line snippet PANEL's content is absent: the def
		# line body itself doesn't render anywhere in the row.
		assert "def render(" not in html
		# The "Slowest queries" affordance still renders.
		assert "Slowest queries for this job" in html

	def test_http_api_action_renders_no_entry_callsite_snippet_in_per_action_table(self):
		"""v0.7.x: the per-action table no longer renders the multi-line
		entry-callsite snippet panel under action rows. A compact inline
		file:line line remains under the action label as a navigation
		anchor; the multi-line snippet itself (def body line, yellow-
		highlighted snippet row) is absent."""
		doc = _doc([
			_action(action_label=self._DOTTED, event_type="HTTP Request", http_method="POST",
			        path="/api/method/" + self._DOTTED, recording_uuid="r0", duration_ms=900),
		])
		html = renderer.render_raw(doc, recordings=[])
		# Action label and inline path both present.
		assert self._DOTTED in html
		assert "optimus/renderer.py:" in html
		# But the multi-line snippet panel's body (def line) isn't.
		assert "def render(" not in html

	def test_smoking_gun_block_not_duplicated_into_per_action_embed(self):
		"""Mirror of the BG-job test above, scoped to the per-action
		breakdown's HTTP API row. With a finding carrying ``action_ref``
		pointing at the action's idx, the related finding card embeds
		under the action row. The smoking-gun panel must NOT render
		there — only inside the Findings section."""
		import json
		doc = _doc(
			actions=[_action(action_label=self._DOTTED, event_type="HTTP Request",
			                 http_method="POST", path="/api/method/" + self._DOTTED,
			                 recording_uuid="r0", duration_ms=900)],
			findings=[
				types.SimpleNamespace(
					finding_type="N+1 Query", severity="High", title="duplicated-anchor probe",
					customer_description="", estimated_impact_ms=0, affected_count=0,
					action_ref="0",
					technical_detail_json=json.dumps({
						"callsite": {
							"filename": "apps/myapp/x.py", "lineno": 1, "function": "f",
						},
					}),
					llm_fix_json=None,
				)
			],
		)
		html = renderer.render_raw(doc, recordings=[])
		# Exactly one smoking-gun panel — the canonical Findings section card.
		assert html.count('class="smoking"') == 1
		# Sanity: the embed actually happened — the title travels with the
		# card, so it should appear at least twice (Findings + per-action).
		assert html.count("duplicated-anchor probe") >= 2

	def test_action_row_shows_inline_entry_path(self):
		"""Positive: the inline ``file:line (function)`` line is present
		in the action label cell with a vscode deep-link when the entry
		callsite resolves to an absolute path."""
		doc = _doc([
			_action(action_label=self._DOTTED, event_type="HTTP Request", http_method="POST",
			        path="/api/method/" + self._DOTTED, recording_uuid="r0", duration_ms=900),
		])
		html = renderer.render_raw(doc, recordings=[])
		# Inline path with the function-name separator.
		assert "optimus/renderer.py:" in html
		# v0.7.x Phase E: the parenthetical "(function)" form was
		# replaced by " · function" in the editorial action-meta row.
		assert "&middot; render" in html or "· render" in html
		# vscode deep-link present (absolute path was resolved).
		assert "vscode://file" in html

	def test_unresolvable_action_path_renders_no_sub_row(self):
		# A job whose method path can't be imported → no callsite, no crash.
		doc = _doc([
			_action(action_label="Job: myapp.tasks.nope_xyzq", event_type="RQ Job",
			        path="myapp.tasks.nope_xyzq", recording_uuid="r1", duration_ms=300),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>RQ Jobs</h2>" in html
		assert "myapp.tasks.nope_xyzq" in html  # still listed by method name
		assert "renderer.py:" not in html        # nothing got resolved

	def test_non_api_http_action_renders_no_sub_row(self):
		doc = _doc([
			_action(action_label="GET /app", event_type="HTTP Request", http_method="GET",
			        path="/app/sales-invoice/new", recording_uuid="r0", duration_ms=900),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "renderer.py:" not in html


# --------------------------------------------------------------------------
# v0.6.x: action/finding context — target document (from form_dict) shown in
# the per-action table, on the finding card, and appended to exec-summary bullets
# --------------------------------------------------------------------------

class TestActionContextInReport:
	def test_savedocs_action_shows_target_doc_everywhere(self):
		action = _action(
			action_label="frappe.desk.form.save.savedocs:Submit", event_type="HTTP Request",
			http_method="POST", path="/api/method/frappe.desk.form.save.savedocs",
			recording_uuid="r0", duration_ms=1000, queries_count=3,
		)
		finding = types.SimpleNamespace(
			finding_type="Hook Bottleneck", severity="Medium",
			title="In frappe.desk.form.save.savedocs:Submit, the looped_validate hook consumed 705ms",
			customer_description="…", estimated_impact_ms=705.0, affected_count=1, action_ref="0",
			technical_detail_json=json.dumps({
				"function": "looped_validate", "filename": "ugly_code/python/common.py",
				"lineno": 6, "cumulative_ms": 705, "action_wall_time_ms": 1000, "is_hook": True,
			}),
			llm_fix_json=None,
		)
		doc = _doc([action], findings=[finding])
		recs = [{"uuid": "r0", "calls": [],
		         "form_dict": {"doc": json.dumps({"doctype": "Sales Invoice", "name": "SINV-1"}), "action": "Submit"}}]
		html = renderer.render_raw(doc, recordings=recs)
		# Per-action table: "→ Sales Invoice  SINV-1".
		assert "&rarr; Sales Invoice" in html
		assert "<code>SINV-1</code>" in html
		# Finding card: "Document:" line.
		assert "Document:" in html
		assert "Sales Invoice" in html
		# v0.7.x redesign Phase B: the exec-summary bullet that
		# augmented its text with "— Sales Invoice SINV-1" is gone
		# (exec-summary card replaced by TL;DR hero). Target-doc
		# surfacing now lives in the per-action breakdown + finding
		# card breadcrumb above. Drop the bullet-text assertion.

	def test_action_with_no_doc_in_form_dict_has_no_target_doc_line(self):
		action = _action(
			action_label="frappe.client.get_value", event_type="HTTP Request", http_method="GET",
			path="/api/method/frappe.client.get_value", recording_uuid="r0", duration_ms=400,
		)
		doc = _doc([action], findings=[])
		recs = [{"uuid": "r0", "calls": [], "form_dict": {"fieldname": "name", "filters": "{}"}}]
		html = renderer.render_raw(doc, recordings=recs)
		# Anchor on the breadcrumb's structural form, not the bare arrow —
		# v0.7.x added a Lens promo line in the header that also uses
		# &rarr;, so the previous unanchored assertion no longer
		# distinguishes "no target doc" from "any arrow anywhere".
		assert '<span class="small muted">&rarr;' not in html

	def test_doc_action_without_recording_does_not_crash(self):
		# Recording expired from Redis → no form_dict to read → no target_doc, no crash.
		action = _action(
			action_label="frappe.desk.form.save.savedocs:Save", event_type="HTTP Request",
			http_method="POST", path="/api/method/frappe.desk.form.save.savedocs",
			recording_uuid="gone", duration_ms=800,
		)
		doc = _doc([action], findings=[])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>Per-action breakdown</h2>" in html or "Per-action breakdown" in html


# --------------------------------------------------------------------------
# v0.6.x: "Doc-event lifecycle" section — slow findings grouped by DocType → event
# --------------------------------------------------------------------------

class TestDocEventLifecycleSection:
	def _savedocs_action(self, **kw):
		kw.setdefault("action_label", "frappe.desk.form.save.savedocs:Submit")
		kw.setdefault("event_type", "HTTP Request")
		kw.setdefault("http_method", "POST")
		kw.setdefault("path", "/api/method/frappe.desk.form.save.savedocs")
		kw.setdefault("recording_uuid", "r0")
		kw.setdefault("duration_ms", 1554)
		return _action(**kw)

	def _si_recording(self):
		return {"uuid": "r0", "calls": [],
		        "form_dict": {"doc": json.dumps({"doctype": "Sales Invoice", "name": "SINV-1"}), "action": "Submit"}}

	def _finding(self, finding_type, title, **td_extra):
		td = {"is_hook": td_extra.pop("is_hook", False)}
		td.update(td_extra)
		return types.SimpleNamespace(
			finding_type=finding_type, severity="Medium", title=title, customer_description="…",
			estimated_impact_ms=float(td.get("cumulative_ms") or 100), affected_count=1, action_ref="0",
			technical_detail_json=json.dumps(td), llm_fix_json=None,
		)

	def test_doc_events_hook_finding_renders_grouped_by_doctype(self):
		# A doc_events-hook finding — inject hook_events into the JSON since
		# _attach_action_context can't compute it without a running site (and
		# won't clobber it: _finding_hook_events returns [] with an empty index).
		finding = self._finding(
			"Hook Bottleneck", "In savedocs:Submit, the looped_validate hook consumed 705ms",
			function="looped_validate", filename="ugly_code/python/common.py", lineno=6,
			cumulative_ms=705, is_hook=True, hook_events=[{"doctype": "Sales Invoice", "event": "validate"}],
		)
		doc = _doc([self._savedocs_action()], findings=[finding])
		html = renderer.render_raw(doc, recordings=[self._si_recording()])
		assert "<h2>Doc-event lifecycle</h2>" in html
		assert "Sales Invoice" in html
		assert "saved/submitted directly" in html
		assert "looped_validate" in html
		# v0.7.x Phase F: the kind tag is now an info-blue
		# `.method-tag` pill instead of a bracketed inline label.
		assert 'class="method-tag">doc_events hook</span>' in html
		assert ">Doc events<" in html  # "Jump to:" nav link

	def test_controller_override_finding_and_cascade_note(self):
		# A GLEntry.validate finding (controller override — no hooks needed),
		# action target = Sales Invoice → "GL Entry touched during a SI submit".
		finding = self._finding(
			"Slow Hot Path", "In savedocs:Submit, 30% spent in GLEntry.validate",
			function="GLEntry.validate", filename="erpnext/accounts/doctype/gl_entry/gl_entry.py",
			lineno=50, cumulative_ms=42,
		)
		doc = _doc([self._savedocs_action()], findings=[finding])
		html = renderer.render_raw(doc, recordings=[self._si_recording()])
		assert "<h2>Doc-event lifecycle</h2>" in html
		assert "Gl Entry" in html
		# v0.7.x Phase F: kind tag → `.method-tag` info-blue pill.
		assert 'class="method-tag">controller override</span>' in html
		assert "touched during Sales Invoice" in html

	def test_section_omitted_when_no_lifecycle_findings(self):
		# A generic Slow Hot Path on a helper → no lifecycle binding → no section.
		finding = self._finding(
			"Slow Hot Path", "In savedocs:Submit, 40% spent in compute_totals",
			function="compute_totals", filename="erpnext/controllers/accounts_controller.py",
			lineno=88, cumulative_ms=600,
		)
		doc = _doc([self._savedocs_action()], findings=[finding])
		html = renderer.render_raw(doc, recordings=[self._si_recording()])
		assert "<h2>Doc-event lifecycle</h2>" not in html
		assert ">Doc events<" not in html
