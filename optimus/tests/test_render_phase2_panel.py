# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for renderer._render_phase2_panel — exercises the phase-2 HTML
without spinning up Frappe / Jinja. We feed a minimal session-doc-shaped
object directly to the helper.
"""

import json
import re
from types import SimpleNamespace

from optimus import renderer


def _run(run_uuid, status, results, picks=None, total_ms=0):
	return SimpleNamespace(
		run_uuid=run_uuid,
		status=status,
		started_at="2026-05-07 12:00:00",
		ended_at="2026-05-07 12:01:00",
		total_ms=total_ms,
		picks_json=json.dumps(picks or []),
		results_json=json.dumps(results),
	)


def _line(lineno, content, hits, total_ms):
	return {
		"lineno": lineno,
		"content": content,
		"content_hash": f"hash_{lineno}",
		"hits": hits,
		"total_ms": total_ms,
		"per_hit_us": (total_ms * 1000.0 / hits) if hits else 0.0,
	}


def _function(dotted_path, lines):
	return {
		"dotted_path": dotted_path,
		"qualname": dotted_path.rsplit(".", 1)[-1],
		"file": "/fake/path.py",
		"lines": lines,
	}


class TestRenderPhase2PanelEmpty:
	def test_no_phase2_runs_returns_empty_string(self):
		session = SimpleNamespace(phase_2_runs=[])
		assert renderer._render_phase2_panel(session) == ""

	def test_phase_2_runs_attribute_missing_returns_empty(self):
		session = SimpleNamespace()
		assert renderer._render_phase2_panel(session) == ""


class TestRenderPhase2PanelSingleRun:
	def _session(self, results):
		return SimpleNamespace(phase_2_runs=[_run("r1", "Ready", results)])

	def test_function_dotted_path_appears_in_output(self):
		session = self._session([
			_function("my_app.x.compute", [_line(1, "x = 1", 5, 10.0)]),
		])

		html = renderer._render_phase2_panel(session)

		assert "my_app.x.compute" in html
		# v0.7.x Phase I.2: heading dropped the "Phase 2" prefix —
		# now reads just "Line-Level Drilldown" (the section's own
		# content makes the line-level drilldown nature self-evident
		# without the internal-phase jargon).
		assert "Line-Level Drilldown" in html

	def test_source_always_rendered(self):
		# v0.6.0 Round 7: safe-mode source toggle removed. Source is
		# always rendered now.
		session = self._session([
			_function("my_app.x", [_line(1, "literal_value = 'foo'", 1, 5.0)]),
		])

		html = renderer._render_phase2_panel(session)

		assert "literal_value" in html

	def test_zero_invocation_function_listed_in_not_exercised_note(self):
		# v0.7.x: no per-function "never invoked" panel (it bloated the
		# drilldown). Uninvoked picks collapse into one concise note.
		session = self._session([_function("my_app.never_runs", [])])

		html = renderer._render_phase2_panel(session)

		assert "never invoked" not in html.lower()
		assert "not exercised" in html.lower()
		assert "my_app.never_runs" in html

	def test_run_duration_over_one_second_renders_in_seconds(self):
		# Per the timing rule, a run total >= 1000ms renders as "N.NNs", not raw ms.
		session = SimpleNamespace(phase_2_runs=[
			_run("r1", "Ready",
			     [_function("my_app.x", [_line(1, "x = 1", 5, 10.0)])],
			     total_ms=27978.07),
		])
		html = renderer._render_phase2_panel(session)
		assert "27.98s" in html
		assert "27978.07ms" not in html


class TestRenderPhase2PanelDiff:
	def test_function_in_two_runs_shows_diff_section(self):
		fn_run1 = _function("my_app.x", [_line(11, "    a = compute()", 100, 800.0)])
		fn_run2 = _function("my_app.x", [_line(11, "    a = compute()", 100, 200.0)])

		session = SimpleNamespace(phase_2_runs=[
			_run("r1", "Ready", [fn_run1], total_ms=800),
			_run("r2", "Ready", [fn_run2], total_ms=200),
		])

		html = renderer._render_phase2_panel(session)

		assert "Cross-Run Comparison" in html
		# Delta should be -600 (faster after fix); shown on a row
		assert "-600.00" in html or "-600" in html

	def test_function_in_one_run_no_diff_section(self):
		fn = _function("my_app.x", [_line(11, "compute()", 100, 100.0)])
		session = SimpleNamespace(phase_2_runs=[_run("r1", "Ready", [fn])])

		html = renderer._render_phase2_panel(session)

		assert "Cross-Run Comparison" not in html


class TestRenderPhase2PanelAutoExpandChain:
	"""When a curated pick was auto-expanded into a chain, the run's
	picks_json marks descendant functions with source='auto_expand'.
	The renderer should indent those function headers and prefix with
	an arrow so the chain reads top-down as a stack."""

	def _run_with_chain(self, root_path, descendant_path):
		# picks_json captures the source of each pick.
		picks = [
			{"dotted_path": root_path, "source": "curated"},
			{"dotted_path": descendant_path, "source": "auto_expand"},
		]
		results = [
			_function(root_path, [_line(1, "self.descendant()", 1, 100.0)]),
			_function(descendant_path, [_line(5, "compute()", 1, 95.0)]),
		]
		return SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-07 12:00:00",
			ended_at="2026-05-07 12:01:00",
			total_ms=195.0,
			picks_json=json.dumps(picks),
			results_json=json.dumps(results),
		)

	def test_root_pick_renders_flush_left(self):
		session = SimpleNamespace(phase_2_runs=[
			self._run_with_chain("my_app.x.root_fn", "my_app.x.descendant"),
		])

		html = renderer._render_phase2_panel(session)

		# Anchor on the root pick's function-table header.
		root_idx = html.rfind("my_app.x.root_fn")
		assert root_idx > -1
		nearby = html[max(0, root_idx - 200):root_idx]
		# v0.7.x Phase F: indent now expressed via `.indent-1` /
		# `.indent-2` class names on the `.phase2-func` block, not
		# inline margin. Anchor on the class instead.
		assert "indent-1" not in nearby
		assert "↳" not in nearby

	def test_auto_expanded_descendant_renders_indented(self):
		session = SimpleNamespace(phase_2_runs=[
			self._run_with_chain("my_app.x.root_fn", "my_app.x.descendant"),
		])

		html = renderer._render_phase2_panel(session)

		desc_idx = html.rfind("my_app.x.descendant")
		assert desc_idx > -1
		nearby = html[max(0, desc_idx - 300):desc_idx]
		# v0.7.x Phase F: indent now `.indent-1` class on the
		# `.phase2-func` block + `&#x21B3;` arrow span before the
		# function name.
		assert "indent-1" in nearby
		assert "&#x21B3;" in nearby

	def test_no_picks_json_falls_back_to_curated_no_indent(self):
		# Older runs may not carry source markers; renderer should treat
		# everything as curated (no indent) rather than break.
		results = [_function("my_app.x.fn", [_line(1, "x = 1", 1, 100.0)])]
		run = SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-07 12:00:00",
			ended_at="2026-05-07 12:01:00",
			total_ms=100.0,
			picks_json="",
			results_json=json.dumps(results),
		)
		session = SimpleNamespace(phase_2_runs=[run])

		html = renderer._render_phase2_panel(session)

		fn_idx = html.rfind("my_app.x.fn")
		assert fn_idx > -1
		nearby = html[max(0, fn_idx - 200):fn_idx]
		assert "↳" not in nearby


class TestRenderPhase2PanelSelfContainment:
	def test_no_external_urls_in_output(self):
		# Critical: safe-report self-containment invariant. The phase-2
		# panel must not introduce any http:// / https:// references or
		# external <script>/<link> elements that would make the safe
		# report fetch resources at view time.
		fn = _function("my_app.x", [_line(11, "compute()", 100, 100.0)])
		session = SimpleNamespace(phase_2_runs=[_run("r1", "Ready", [fn])])

		html = renderer._render_phase2_panel(session)

		# No protocol-prefixed URLs (excluding xmlns-style namespaces, none
		# of which we use in this panel).
		assert not re.search(r"https?://", html), "phase-2 panel must not introduce external URLs"
		# No <script src=...> or <link href=...> with external URLs
		assert "<script src=" not in html
		assert "<link " not in html


class TestRenderPhase2PanelPosition:
	"""v0.6.x: Line-Level Drilldown is hoisted above the Findings section
	in the report. A render-level check that the rendered HTML places
	the section anchor (``id="phase2"`` legacy alias or
	``id="line-drilldown"`` canonical) before the
	``<h2>Findings - what to fix</h2>`` heading.
	"""

	def _session_doc(self, *, with_phase2=True):
		"""Build a minimal SimpleNamespace doc that ``render_raw`` accepts."""
		phase_2_runs = []
		if with_phase2:
			fn = _function("my_app.foo", [_line(10, "x = 1", 50, 100.0)])
			phase_2_runs.append(_run("r1", "Ready", [fn]))
		return SimpleNamespace(
			name="PS-pos", session_uuid="pos-uuid", title="phase2 position test",
			user="a@example.com", status="Ready",
			started_at="2026-05-13T00:00:00", stopped_at="2026-05-13T00:00:01",
			notes=None, top_severity="Low", summary_html=None,
			total_duration_ms=100, total_query_time_ms=10,
			total_queries=1, total_requests=1,
			top_queries_json="[]", table_breakdown_json="[]",
			hot_frames_json="[]", session_time_breakdown_json=None,
			total_python_ms=None, total_sql_ms=None,
			analyzer_warnings=None, v5_aggregate_json="{}",
			actions=[], findings=[], phase_2_runs=phase_2_runs,
		)

	def test_phase2_anchor_renders_before_findings_h2(self):
		html = renderer.render_raw(self._session_doc(with_phase2=True), recordings=[])
		phase2_idx = html.find('id="phase2"')
		findings_h2 = html.find("<h2>Findings - what to fix</h2>")
		assert phase2_idx > 0, "id=\"phase2\" wrapper missing from rendered HTML"
		assert findings_h2 > 0, "Findings <h2> missing from rendered HTML"
		assert phase2_idx < findings_h2, (
			"Phase 2 panel must render before the Findings <h2> — it is the "
			"showcase section, hoisted above the actionable list"
		)

	def test_phase2_jump_nav_link_appears_when_runs_present(self):
		html = renderer.render_raw(self._session_doc(with_phase2=True), recordings=[])
		# The jump-nav link is the visible affordance for the hoisted section.
		# v0.7.x Phase J.16: nav anchor renamed from "#phase2" to
		# "#line-drilldown"; the legacy "#phase2" anchor element still
		# precedes the panel so external links resolve.
		assert 'href="#line-drilldown"' in html
		assert ">Line-Level Drilldown</a>" in html

	def test_phase2_omitted_when_no_runs(self):
		html = renderer.render_raw(self._session_doc(with_phase2=False), recordings=[])
		# Conditional both ways: no panel + no jump link when the session
		# had no phase-2 runs.
		assert 'id="phase2"' not in html
		assert 'id="line-drilldown"' not in html
		assert 'href="#line-drilldown"' not in html
		assert ">Line-Level Drilldown</a>" not in html


class TestRenderPhase2PanelRefinements:
	"""v0.7.x refinements: reworded intro, dimmed 0-hit lines, dropped Picks line."""

	def _panel(self, results, picks=None):
		return renderer._render_phase2_panel(
			SimpleNamespace(phase_2_runs=[_run("r1", "Ready", results, picks=picks or [])])
		)

	def test_intro_points_to_not_exercised_note(self):
		out = self._panel([_function("my_app.x", [_line(2, "y = 1", 5, 10.0)])])
		assert "not exercised in this pass" in out.lower()
		# the old stale phrasing is gone
		assert "function-not-invoked warnings" not in out.lower()

	def test_zero_hit_lines_get_dim_class(self):
		results = [_function("my_app.x", [
			_line(1, "def x():", 0, 0.0),        # never ran → dim
			_line(2, "# comment", 0, 0.0),        # never ran → dim
			_line(3, "y = compute()", 5, 50.0),   # ran → not dim
		])]
		out = self._panel(results)
		assert 'class="zero"' in out  # the 0-hit context rows are dimmed

	def test_no_picks_line(self):
		out = self._panel(
			[_function("my_app.x", [_line(1, "y = 1", 5, 10.0)])],
			picks=[{"dotted_path": "my_app.x", "source": "curated"}],
		)
		# the redundant "Picks:" summary line is gone (function tables +
		# "Not exercised" note already enumerate the picks)
		assert "Picks:" not in out
