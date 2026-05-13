# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.6.0 finding-card 'smoking gun' block — exercises the
new prominent callsite + source snippet + Phase 2 hot-line callout above
the existing technical_detail rows. Uses renderer.render_raw end-to-end
so we verify the macro within its real context.
"""

import inspect
import json
from types import SimpleNamespace

from optimus import renderer


def _finding_child(
	*,
	filename="/abs/path/to/myapp/x.py",
	lineno=42,
	function="my_func",
	source_snippet=None,
	finding_type="Slow Hot Path",
	severity="High",
	title="something is slow",
	description="we noticed a thing",
	impact_ms=100.0,
	affected=1,
	action_ref="0",
	extra_detail=None,
):
	detail = {
		"callsite": {"filename": filename, "lineno": lineno, "function": function},
	}
	if source_snippet is not None:
		detail["callsite"]["source_snippet"] = source_snippet
	if extra_detail:
		detail.update(extra_detail)
	return SimpleNamespace(
		finding_type=finding_type,
		severity=severity,
		title=title,
		customer_description=description,
		estimated_impact_ms=impact_ms,
		affected_count=affected,
		action_ref=action_ref,
		technical_detail_json=json.dumps(detail),
	)


def _fake_doc(findings, phase_2_runs=None):
	return SimpleNamespace(
		name="PS-test",
		session_uuid="test-uuid",
		title="test",
		user="tester@example.com",
		status="Ready",
		started_at="2026-05-08T00:00:00",
		stopped_at="2026-05-08T00:00:05",
		total_duration_ms=5000,
		total_requests=1,
		total_queries=0,
		total_query_time_ms=0,
		analyze_duration_ms=100,
		top_severity="High",
		summary_html="<p>summary</p>",
		top_queries_json="[]",
		table_breakdown_json="[]",
		analyzer_warnings=None,
		actions=[],
		findings=findings,
		hot_frames_json=None,
		session_time_breakdown_json=None,
		total_python_ms=None,
		total_sql_ms=None,
		phase_2_runs=phase_2_runs or [],
	)


class TestSourceSnippetRendering:
	def test_snippet_lines_appear_in_raw_mode(self):
		snippet = [
			{"lineno": 41, "content": "    if not user:"},
			{"lineno": 42, "content": "        for i in range(50):"},
			{"lineno": 43, "content": "            frappe.get_doc('User', i)"},
		]
		doc = _fake_doc([_finding_child(source_snippet=snippet)])

		html = renderer.render_raw(doc, recordings=[])

		assert "for i in range(50):" in html
		assert "frappe.get_doc(&#39;User&#39;, i)" in html or "frappe.get_doc('User', i)" in html
		# Lineno labels should appear next to the snippet rows.
		assert ">41<" in html
		assert ">42<" in html
		assert ">43<" in html

	def test_snippet_always_rendered(self):
		# v0.6.0 Round 7: safe-mode source toggle removed. The snippet
		# is always shown when present (no admin opt-out for redacted
		# rendering).
		snippet = [{"lineno": 42, "content": "        frappe.db.sql('SELECT 1')"}]
		doc = _fake_doc([_finding_child(source_snippet=snippet)])

		html = renderer.render_raw(doc, recordings=[])

		assert "frappe.db.sql" in html

	def test_finding_without_source_snippet_still_renders_callsite(self):
		# Older sessions / sessions where the file couldn't be read.
		doc = _fake_doc([_finding_child(source_snippet=None)])

		html = renderer.render_raw(doc, recordings=[])

		# Callsite line still present even when source_snippet absent.
		assert "my_func" in html
		assert ":42" in html
		# No <source omitted> placeholder when there's no snippet field
		# at all (the placeholder only shows when we *had* a snippet but
		# the safe-mode toggle suppressed it).
		assert "&lt;source omitted&gt;" not in html


class TestSmokingGunBlockHoisting:
	def test_callsite_appears_above_other_detail_rows(self):
		"""The smoking-gun block sits between the description and the
		technical_detail block, so file:lineno appears BEFORE the
		fix_hint / normalized_query rows in the rendered HTML."""
		snippet = [{"lineno": 42, "content": "x = 1"}]
		doc = _fake_doc([
			_finding_child(
				source_snippet=snippet,
				extra_detail={
					"normalized_query": "SELECT * FROM `tabUser`",
					"fix_hint": "Add an index on tabUser.email",
				},
			),
		])

		html = renderer.render_raw(doc, recordings=[])

		# Both elements present.
		callsite_pos = html.find(":42")
		fix_hint_pos = html.find("Add an index on")
		query_pos = html.find("SELECT * FROM")
		assert callsite_pos > -1 and fix_hint_pos > -1 and query_pos > -1
		# Callsite block (smoking gun) appears BEFORE both.
		assert callsite_pos < fix_hint_pos
		assert callsite_pos < query_pos


class TestPhase2Crosslink:
	def _phase2_run(self, dotted_path, qualname, file_path, lines):
		return SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-08 11:25:36",
			ended_at="2026-05-08 11:25:40",
			total_ms=200.0,
			picks_json="[]",
			results_json=json.dumps([
				{
					"dotted_path": dotted_path,
					"qualname": qualname,
					"file": file_path,
					"lines": lines,
				},
			]),
		)

	def test_phase2_callout_renders_when_function_match(self):
		"""When a finding's callsite function was instrumented in Phase 2,
		the card shows 'Phase 2: hottest line N — Mms / X hits'."""
		snippet = [{"lineno": 42, "content": "    do_thing()"}]
		finding = _finding_child(
			filename="/different/path/myapp/x.py",  # different prefix, same basename
			function="my_func",
			source_snippet=snippet,
		)
		phase2 = self._phase2_run(
			dotted_path="myapp.x.my_func",
			qualname="my_func",
			file_path="/some/other/path/myapp/x.py",
			lines=[
				{"lineno": 42, "content": "    do_thing()", "hits": 50, "total_ms": 160.5,
				 "per_hit_us": 3210.0, "content_hash": "h"},
				{"lineno": 43, "content": "    pass", "hits": 50, "total_ms": 0.5,
				 "per_hit_us": 10.0, "content_hash": "h2"},
			],
		)
		doc = _fake_doc([finding], phase_2_runs=[phase2])

		html = renderer.render_raw(doc, recordings=[])

		assert "Phase 2:" in html
		assert "hottest line 42" in html
		assert "160ms" in html or "160.5ms" in html
		assert "50 hit" in html

	def test_no_callout_when_function_not_in_phase2(self):
		"""A finding whose function is not in any Phase 2 run gets no
		callout (would be misleading). The Phase 2 panel itself still
		renders, so we check for the distinctive callout phrase
		'hottest line' rather than just 'Phase 2:'."""
		snippet = [{"lineno": 42, "content": "    do_thing()"}]
		finding = _finding_child(function="my_func", source_snippet=snippet)
		phase2 = self._phase2_run(
			dotted_path="myapp.x.different_function",
			qualname="different_function",
			file_path="/some/path/x.py",
			lines=[
				{"lineno": 5, "content": "x", "hits": 1, "total_ms": 100.0,
				 "per_hit_us": 100000.0, "content_hash": "h"},
			],
		)
		doc = _fake_doc([finding], phase_2_runs=[phase2])

		html = renderer.render_raw(doc, recordings=[])

		assert "hottest line" not in html

	def test_no_callout_when_session_has_no_phase2_runs(self):
		snippet = [{"lineno": 42, "content": "    do_thing()"}]
		finding = _finding_child(function="my_func", source_snippet=snippet)
		doc = _fake_doc([finding], phase_2_runs=[])

		html = renderer.render_raw(doc, recordings=[])

		assert "hottest line" not in html
		# Also: with no phase-2 runs, the panel itself shouldn't render.
		assert "Phase 2: Line-Level Drilldown" not in html

	def test_picks_hottest_line_across_runs(self):
		"""When the same function was instrumented across multiple runs,
		the callout reports the line with the highest single-line
		total_ms (most informative)."""
		snippet = [{"lineno": 42, "content": "    do_thing()"}]
		finding = _finding_child(function="my_func", source_snippet=snippet)
		# Run 1: line 42 dominates at 100ms
		run1 = self._phase2_run(
			dotted_path="myapp.x.my_func",
			qualname="my_func",
			file_path="/p/x.py",
			lines=[
				{"lineno": 42, "content": "    do_thing()", "hits": 1, "total_ms": 100.0,
				 "per_hit_us": 100000.0, "content_hash": "h1"},
			],
		)
		# Run 2: line 50 dominates at 200ms (different line of same fn)
		run2 = self._phase2_run(
			dotted_path="myapp.x.my_func",
			qualname="my_func",
			file_path="/p/x.py",
			lines=[
				{"lineno": 50, "content": "    other_thing()", "hits": 1, "total_ms": 200.0,
				 "per_hit_us": 200000.0, "content_hash": "h2"},
			],
		)
		doc = _fake_doc([finding], phase_2_runs=[run1, run2])

		html = renderer.render_raw(doc, recordings=[])

		assert "hottest line 50" in html
		assert "200ms" in html or "200.0ms" in html
		assert "hottest line 42" not in html


# ---------------------------------------------------------------------------
# v0.6.0 Round 2: cross-analyzer callsite shape
# ---------------------------------------------------------------------------


def _slow_hot_path_finding(filename, lineno, function):
	"""Mimic call_tree's Slow Hot Path finding shape: top-level
	filename/lineno/function, no `callsite` wrapper."""
	return SimpleNamespace(
		finding_type="Slow Hot Path",
		severity="High",
		title=f"In some_action, {function} consumed time",
		customer_description="walltime hotspot",
		estimated_impact_ms=500.0,
		affected_count=1,
		action_ref="0",
		technical_detail_json=json.dumps({
			"function": function,
			"filename": filename,
			"lineno": lineno,
			"cumulative_ms": 500.0,
			"action_wall_time_ms": 1000.0,
			"is_hook": False,
		}),
	)


def _hot_line_finding(file_path, lineno, line_content):
	"""Mimic line_profile.analyzer's Hot Line finding shape: top-level
	`file` (not `filename`) + lineno + line_content, no `callsite`
	wrapper."""
	return SimpleNamespace(
		finding_type="Hot Line",
		severity="High",
		title=f"some.module:{lineno} consumed 100ms (1 hits) — single hottest line",
		customer_description="dominant time sink",
		estimated_impact_ms=100.0,
		affected_count=1,
		action_ref=None,
		technical_detail_json=json.dumps({
			"dotted_path": "some.module.fn",
			"file": file_path,
			"lineno": lineno,
			"line_content": line_content,
			"total_ms": 100.0,
			"hits": 1,
			"per_hit_us": 100000.0,
		}),
	)


class TestSlowHotPathLegacyShape:
	"""call_tree's Slow Hot Path / Hook Bottleneck / Repeated Hot Frame
	store filename/lineno at top level — the renderer must synthesize a
	callsite from those so the smoking-gun block renders."""

	def test_smoking_gun_renders_for_top_level_filename(self, tmp_path):
		src = tmp_path / "legacy.py"
		src.write_text("alpha\nbeta\ngamma\ndelta\n")
		finding = _slow_hot_path_finding(str(src), 2, "beta_fn")
		doc = _fake_doc([finding])

		html = renderer.render_raw(doc, recordings=[])

		# Smoking-gun block visible: file:lineno + ±1 source rendered.
		assert "legacy.py:2" in html
		assert "beta" in html
		# Lineno labels for the snippet rows.
		assert ">1<" in html and ">2<" in html and ">3<" in html


class TestHotLineLegacyShape:
	"""line_profile.analyzer's Hot Line finding stores `file` (not
	`filename`) at top level — synthesize and use line_content as the
	source snippet (no file read needed)."""

	def test_smoking_gun_renders_with_line_content(self):
		finding = _hot_line_finding(
			"/abs/path/myapp/common.py", 7, "    _run_validations(doc)",
		)
		doc = _fake_doc([finding])

		html = renderer.render_raw(doc, recordings=[])

		# Callsite line appears with file:lineno.
		assert "common.py:7" in html
		# line_content rendered as the snippet body.
		assert "_run_validations(doc)" in html
		# The lineno label for the single-row snippet appears.
		assert ">7<" in html

	def test_phase2_callout_suppressed_for_hot_line(self):
		"""Hot Line findings are themselves the phase-2 hot line. The
		'Phase 2: hottest line N' callout would be self-referential, so
		it must be suppressed for finding_type == 'Hot Line'."""
		finding = _hot_line_finding(
			"/abs/myapp/x.py", 7, "    do_thing()",
		)
		# Stage a phase-2 run where the same function would otherwise
		# match the smoking-gun lookup.
		phase2 = SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-08 11:25:36",
			ended_at="2026-05-08 11:25:40",
			total_ms=100.0,
			picks_json="[]",
			results_json=json.dumps([
				{
					"dotted_path": "myapp.x.fn",
					"qualname": "fn",
					"file": "/abs/myapp/x.py",
					"lines": [
						{"lineno": 7, "content": "    do_thing()", "hits": 1,
						 "total_ms": 100.0, "per_hit_us": 100000.0,
						 "content_hash": "h"},
					],
				},
			]),
		)
		doc = _fake_doc([finding], phase_2_runs=[phase2])

		html = renderer.render_raw(doc, recordings=[])

		# The callout's distinctive HTML — `<strong …>Phase 2:</strong>` —
		# must NOT appear (the title text "single hottest line" is part
		# of the Hot Line finding's own title, so we can't filter on
		# that phrase alone).
		assert "Phase 2:</strong>" not in html


class TestLazySnippetRead:
	"""Findings persisted before analyze-time enrichment shipped have
	no source_snippet attached. The renderer reads the file at render
	time so older sessions render correctly without re-running analyze."""

	def test_callsite_with_filename_lineno_but_no_snippet_reads_file(self, tmp_path):
		src = tmp_path / "old.py"
		src.write_text("a()\nb()\nc()\nd()\n")
		# Finding shape: dict-style callsite with NO source_snippet
		# field (mimics pre-Round-1 persisted data).
		finding = SimpleNamespace(
			finding_type="N+1 Query",
			severity="Medium",
			title="repeated query",
			customer_description="loop",
			estimated_impact_ms=50.0,
			affected_count=10,
			action_ref="0",
			technical_detail_json=json.dumps({
				"callsite": {
					"filename": str(src),
					"lineno": 2,
					"function": "loop_fn",
				},
			}),
		)
		doc = _fake_doc([finding])

		html = renderer.render_raw(doc, recordings=[])

		# Snippet was read lazily at render time and inserted.
		assert "b()" in html
		assert ">1<" in html and ">2<" in html and ">3<" in html

	def test_missing_file_no_snippet_no_crash(self, tmp_path):
		finding = SimpleNamespace(
			finding_type="N+1 Query",
			severity="Medium",
			title="repeated query",
			customer_description="loop",
			estimated_impact_ms=50.0,
			affected_count=10,
			action_ref="0",
			technical_detail_json=json.dumps({
				"callsite": {
					"filename": str(tmp_path / "does_not_exist.py"),
					"lineno": 5,
					"function": "loop_fn",
				},
			}),
		)
		doc = _fake_doc([finding])

		# Must not crash — best-effort silent skip.
		html = renderer.render_raw(doc, recordings=[])

		# Callsite line still rendered (file:lineno + function visible).
		assert "does_not_exist.py:5" in html
		assert "loop_fn" in html


# ---------------------------------------------------------------------------
# v0.6.x: findings that carry no callsite get one resolved at render time —
# Repeated Hot Frame (from its "path::func" key), Function Not Invoked (from
# its dotted_path), and SQL red-flag findings (a representative callsite from
# the recordings).
# ---------------------------------------------------------------------------

# Same constraint as test_action_entry_callsite: walk_callsite excludes any
# path containing "frappe/" or "optimus/", so the "user code" frame
# must be a path with neither — a stdlib module's absolute file works.
_USER_FRAME_FILE = inspect.__file__


def _repeated_hot_frame(function_key, total_ms=679.0):
	return SimpleNamespace(
		finding_type="Repeated Hot Frame",
		severity="High",
		title=f"{function_key} appeared in 3 actions and consumed {total_ms:.0f}ms total",
		customer_description=f"The function **{function_key}** ran across 3 actions.",
		estimated_impact_ms=total_ms,
		affected_count=12,
		action_ref=None,
		technical_detail_json=json.dumps({
			"function": function_key, "total_ms": total_ms,
			"distinct_actions": 3, "action_refs": [0, 1, 2],
		}),
	)


def _function_not_invoked(dotted_path):
	return SimpleNamespace(
		finding_type="Function Not Invoked",
		severity="Low",
		title=f"{dotted_path} was picked but never invoked during phase 2",
		customer_description=f"The function **{dotted_path}** was instrumented but never ran.",
		estimated_impact_ms=0.0,
		affected_count=0,
		action_ref=None,
		technical_detail_json=json.dumps({"dotted_path": dotted_path, "file": None}),
	)


def _missing_index(table, normalized_query, fix_hint="Add an index"):
	return SimpleNamespace(
		finding_type="Missing Index",
		severity="High",
		title=f"Queries on `{table}` would benefit from an index",
		customer_description=f"`{table}` is scanned without an index.",
		estimated_impact_ms=200.0,
		affected_count=40,
		action_ref="0",
		technical_detail_json=json.dumps({
			"table": table, "normalized_query": normalized_query, "fix_hint": fix_hint,
		}),
	)


class TestRenderTimeCallsiteResolution:
	def test_repeated_hot_frame_resolves_to_file_line_and_snippet(self):
		# "path::func" with a shallow user-app-style path resolves via the
		# dotted strategy. Use this very app's renderer.render.
		doc = _fake_doc([_repeated_hot_frame("optimus/renderer.py::render")])
		html = renderer.render_raw(doc, recordings=[])
		assert "optimus/renderer.py:" in html      # resolved callsite line
		assert "def render(" in html                        # the highlighted def line
		assert "vscode://file" in html                      # _abs → editor link
		assert 'class="smoking-gun"' in html

	def test_repeated_hot_frame_unresolvable_renders_no_block_no_crash(self):
		doc = _fake_doc([_repeated_hot_frame("nope_xyzq/foo.py::bar")])
		html = renderer.render_raw(doc, recordings=[])
		# Card still renders (title visible), but no smoking-gun block for it.
		assert "appeared in 3 actions" in html
		assert 'class="smoking-gun"' not in html

	def test_function_not_invoked_shows_def_line(self):
		doc = _fake_doc([_function_not_invoked("optimus.renderer.render")])
		html = renderer.render_raw(doc, recordings=[])
		assert "optimus/renderer.py:" in html
		assert "def render(" in html

	def test_missing_index_gets_representative_callsite_from_recordings(self):
		nq = "SELECT ... FROM `tabUser` WHERE x = ?"
		doc = _fake_doc([_missing_index("tabUser", nq)])
		recs = [{
			"uuid": "r1",
			"calls": [{
				"query": "SELECT name FROM `tabUser` WHERE x = 1",
				"normalized_query": nq,
				"duration": 5.0,
				"stack": [
					{"filename": "frappe/app.py", "lineno": 1, "function": "handle"},
					{"filename": _USER_FRAME_FILE, "lineno": 10, "function": "run_report"},
					{"filename": "frappe/database/database.py", "lineno": 2, "function": "sql"},
				],
			}],
		}]
		html = renderer.render_raw(doc, recordings=recs)
		assert "Most-called from:" in html
		assert "Representative callsite" in html
		assert f"{_USER_FRAME_FILE}:10" in html
		assert "run_report" in html

	def test_missing_index_without_matching_recording_renders_no_block(self):
		doc = _fake_doc([_missing_index("tabUser", "SELECT ... FROM `tabUser`")])
		# Recording has a different query → no representative callsite.
		recs = [{"uuid": "r1", "calls": [
			{"query": "SELECT * FROM `tabItem`", "normalized_query": "DIFFERENT", "duration": 1.0, "stack": []},
		]}]
		html = renderer.render_raw(doc, recordings=recs)
		assert "Most-called from:" not in html
		# Card still renders (the fix_hint is in the technical_detail block).
		assert "Add an index" in html


class TestDocEventHookInsideSmokingGun:
	"""v0.6.x: the target-document + doc-event-hook breadcrumb is rendered
	INSIDE the smoking-gun box (alongside the Phase 2 + Drill-down
	callouts), not in a separate finding-detail box below. Keeps all
	context cues in one visual block."""

	def test_hook_line_lives_inside_smoking_gun(self):
		finding = _finding_child(
			filename="/apps/ugly_code/ugly_code/python/common.py",
			lineno=6,
			function="looped_validate",
			source_snippet=[
				{"lineno": 6, "content": "def looped_validate(doc, event):"},
			],
			extra_detail={
				"target_doc": {"doctype": "Sales Invoice", "name": "SI-0001"},
				"hook_events": [{"doctype": "Sales Invoice", "event": "validate"}],
			},
		)
		doc = _fake_doc([finding])
		html = renderer.render_raw(doc, recordings=[])

		hook_pos = html.find("Doc-event hook:")
		sg_open = html.find('class="smoking-gun"', 0)
		# The first finding-detail block lives RIGHT AFTER the smoking-gun
		# for this finding — locate it from sg_open onwards.
		fd_open = html.find('class="finding-detail"', sg_open)
		assert hook_pos > 0, "Doc-event hook line missing from output"
		assert sg_open > 0
		assert fd_open > 0
		assert sg_open < hook_pos < fd_open, (
			"Doc-event hook breadcrumb must render INSIDE the smoking-gun "
			f"block (between {sg_open} and {fd_open}), got {hook_pos}"
		)

	def test_target_doc_appears_with_hook(self):
		"""When both target_doc + hook_events are set, both render on the
		same line, separated by a middot."""
		finding = _finding_child(
			filename="/apps/ugly_code/ugly_code/python/common.py",
			lineno=6,
			function="looped_validate",
			source_snippet=[{"lineno": 6, "content": "def looped_validate(doc, event):"}],
			extra_detail={
				"target_doc": {"doctype": "Sales Invoice", "name": "SI-0001"},
				"hook_events": [{"doctype": "Sales Invoice", "event": "validate"}],
			},
		)
		doc = _fake_doc([finding])
		html = renderer.render_raw(doc, recordings=[])

		# Both the document chip and the hook chip render — single span line.
		assert "Document:" in html
		assert "<strong>Sales Invoice</strong>" in html
		assert "SI-0001" in html
		assert "Doc-event hook:" in html
		assert "Sales Invoice &#9656; validate" in html

	def test_finding_without_callsite_renders_hook_as_fallback(self):
		"""For the rare finding that has no callsite (so no smoking-gun
		block), the hook breadcrumb still renders as a fallback in the
		.finding-detail block — we don't drop user-relevant context."""
		# Build a finding manually so callsite has no filename/lineno.
		finding = SimpleNamespace(
			finding_type="Slow Hot Path",
			severity="Medium",
			title="callsite-less finding",
			customer_description="",
			estimated_impact_ms=50.0,
			affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"hook_events": [{"doctype": "Sales Invoice", "event": "on_submit"}],
			}),
		)
		doc = _fake_doc([finding])
		html = renderer.render_raw(doc, recordings=[])

		# Hook still appears, but now inside .finding-detail (not smoking-gun
		# — there's no callsite to attach to).
		assert "Doc-event hook:" in html
		assert "Sales Invoice &#9656; on_submit" in html

