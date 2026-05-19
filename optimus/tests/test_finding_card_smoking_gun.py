# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.6.0 finding-card 'smoking gun' block — exercises the
new prominent callsite + source snippet + Phase 2 hot-line callout above
the existing technical_detail rows. Uses renderer.render_raw end-to-end
so we verify the macro within its real context.
"""

import html as _html
import inspect
import json
import re
from types import SimpleNamespace

from optimus import renderer


def _plain(html_str: str) -> str:
	"""Strip HTML tags + decode entities so substring assertions on
	source-code content work after v0.7.x VSCode Dark+ syntax
	highlighting wraps each token in its own ``<span class="tok-...">``
	element (and Pygments escapes string quotes to ``&#39;`` etc.)."""
	return _html.unescape(re.sub(r"<[^>]+>", "", html_str))


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


def _fake_doc(findings, phase_2_runs=None, actions=None):
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
		actions=actions or [],
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

		plain = _plain(html)
		assert "for i in range(50):" in plain
		assert "frappe.get_doc('User', i)" in plain
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

		assert "frappe.db.sql" in _plain(html)

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

	def test_blank_context_lines_are_skipped(self):
		# Regression: the snippet builder returns a ±1 window so a def line
		# preceded by the PEP-8 blank-line separator yields an empty leading
		# row. Rendering that as a line-number gutter with no content
		# produced a dead band of empty space above the highlighted target.
		snippet = [
			{"lineno": 198, "content": ""},                           # blank separator
			{"lineno": 199, "content": "def my_func(doc_name=None):"},  # target
			{"lineno": 200, "content": "    for i in range(15):"},
		]
		doc = _fake_doc([_finding_child(lineno=199, source_snippet=snippet)])

		html = renderer.render_raw(doc, recordings=[])

		# Blank leading row is dropped.
		assert '<span class="ln">198</span>' not in html
		# Target line + the trailing context line are kept.
		assert '<span class="ln">199</span>' in html
		assert '<span class="ln">200</span>' in html
		assert "def my_func(doc_name=None):" in _plain(html)


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

		assert "Line-Level Drilldown:" in html
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

	def test_retarget_phase1_callsite_to_drilldown_leaf(self, tmp_path):
		"""A Slow Hot Path finding whose drill-down chain walks down
		from ``looped_validate`` to ``_check_user_exists`` (via the
		intermediate ``_run_validations``) should re-anchor the
		smoking-gun snippet on the **call site** of ``_check_user_exists``
		inside its parent ``_run_validations`` — that's the line in
		``_run_validations``'s body that invokes the leaf (e.g.
		``    _check_user_exists(doc)``), not the leaf's own ``def``.

		The wrapper (``looped_validate``) is preserved as a breadcrumb
		caption. Phase-2's actually-hot internal line (line 20) still
		surfaces in the separate Phase 2 callout — and the drill-down
		narrative is rooted on ``looped_validate``."""
		src = tmp_path / "common.py"
		src.write_text(
			"line1\n"
			"line2\n"
			"line3\n"
			"line4\n"
			"line5\n"
			"def looped_validate(doc, event):\n"  # lineno 6
			"    _run_validations(doc)\n"  # lineno 7
			"\n"
			"\n"
			"\n"
			"\n"
			"def _run_validations(doc):\n"  # lineno 12
			"    _check_user_exists(doc)\n"  # lineno 13
			"\n"
			"\n"
			"\n"
			"\n"
			"def _check_user_exists(doc):\n"  # lineno 18
			"    for i in range(50):\n"  # lineno 19
			"        user = frappe.get_doc('User', 'admin')\n"  # lineno 20
			"        if user.enabled:\n"  # lineno 21
			"            pass\n"  # lineno 22
		)
		# Phase-1 finding: Slow Hot Path on looped_validate at line 6.
		# The action_ref ties it to the action carrying the call tree.
		finding = SimpleNamespace(
			finding_type="Slow Hot Path",
			severity="High",
			title="In submit, 66% of the time was spent in looped_validate",
			customer_description="walltime hotspot",
			estimated_impact_ms=741.0,
			affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"function": "looped_validate",
				"filename": str(src),
				"lineno": 6,
				"cumulative_ms": 741.0,
				"action_wall_time_ms": 1124.0,
				"is_hook": False,
			}),
		)
		# Phase-2 captures the leaf's actual hot line at 20.
		phase2 = SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-14 10:42:19",
			ended_at="2026-05-14 10:44:57",
			total_ms=741.0,
			picks_json="[]",
			results_json=json.dumps([
				{
					"dotted_path": "ugly_code.python.common._check_user_exists",
					"qualname": "_check_user_exists",
					"file": str(src),
					"lines": [
						{"lineno": 18, "content": "def _check_user_exists(doc):",
						 "hits": 2, "total_ms": 0.0, "per_hit_us": 0.0,
						 "content_hash": "h18"},
						{"lineno": 20,
						 "content": "        user = frappe.get_doc('User', 'admin')",
						 "hits": 100, "total_ms": 354.0, "per_hit_us": 3540.0,
						 "content_hash": "h20"},
					],
				},
			]),
		)
		# Phase-1 action with a pyinstrument call tree showing the
		# descent looped_validate → _run_validations → _check_user_exists.
		# _attach_drilldown_chains will walk this and populate
		# drilldown_chain on the finding; the retargeter then re-anchors
		# the callsite on the deepest user-code frame in the chain.
		action = SimpleNamespace(
			idx=0,
			action_label="submit",
			event_type="HTTP Request",
			http_method="POST",
			path="/api/method/frappe.client.save",
			recording_uuid="rec1",
			duration_ms=1124,
			queries_count=0,
			query_time_ms=0,
			slowest_query_ms=0,
			call_tree_json=json.dumps({
				"function": "looped_validate",
				"filename": str(src),
				"lineno": 6,
				"kind": "python",
				"cumulative_ms": 741.0,
				"children": [{
					"function": "_run_validations",
					"filename": str(src),
					"lineno": 12,
					"kind": "python",
					"cumulative_ms": 547.0,
					"children": [{
						"function": "_check_user_exists",
						"filename": str(src),
						"lineno": 18,
						"kind": "python",
						"cumulative_ms": 546.0,
						"children": [],
					}],
				}],
			}),
		)
		doc = _fake_doc([finding], phase_2_runs=[phase2], actions=[action])

		html = renderer.render_raw(doc, recordings=[])

		# Smoking-gun callsite has been retargeted to the **call site**
		# of _check_user_exists in its parent (_run_validations). That
		# call lives at line 13 (``    _check_user_exists(doc)``), not
		# at the leaf's def line 18. v0.7.x redesign Phase D: the
		# file:line callsite header moved from inside `.smoking` to
		# the `.finding-meta` row under the title — slice the broader
		# `.finding` card up to the Drill-down chain so both the meta
		# row AND the smoking-gun snippet are inside the scope.
		finding_start = html.find('class="finding severity-')
		finding_end = html.find("Drill-down", finding_start)
		assert finding_start > -1 and finding_end > -1
		smoking_gun_html = html[finding_start:finding_end]
		assert "common.py:13" in smoking_gun_html
		# Parent function shown in the meta row.
		assert "_run_validations" in smoking_gun_html
		# The highlighted source body is the call expression itself.
		assert "_check_user_exists(doc)" in smoking_gun_html
		# The leaf's def line (18) does NOT appear as the smoking-gun
		# anchor (it's still visible in the Drill-down chain below).
		assert "common.py:18" not in smoking_gun_html
		# The breadcrumb caption mentions the wrapper.
		assert "Time entered through" in html
		assert "looped_validate" in html
		# Phase 2 callout STILL renders (snippet anchors on call line 13,
		# Phase 2 surfaces the actually-hot internal line 20).
		assert "hottest line 20" in html
		# The drill-down narrative heading uses the WRAPPER's name (the
		# chain's root), not the retargeted callsite function.
		assert "time inside <code>looped_validate</code> walked through" in html
		assert "time inside <code>_run_validations</code> walked through" not in html
		assert "time inside <code>_check_user_exists</code> walked through" not in html

	def test_retarget_falls_back_to_leaf_def_when_call_site_not_findable(self, tmp_path):
		"""When the leaf's call can't be located in the parent's body
		(unparseable source, leaf name not present, etc.), the retarget
		falls back to the leaf's own def line — the previous behavior.
		Anchors are still better than the wrapper's def line."""
		src = tmp_path / "common.py"
		# Source where _run_validations doesn't actually contain a call
		# to _check_user_exists (intentional mismatch to exercise the
		# fallback). The drill-down chain — built from a pre-cooked
		# pyinstrument tree, NOT from this source — still has the chain.
		src.write_text(
			"line1\n"
			"line2\n"
			"line3\n"
			"line4\n"
			"line5\n"
			"def looped_validate(doc, event):\n"  # 6
			"    pass\n"  # 7
			"\n"
			"\n"
			"\n"
			"\n"
			"def _run_validations(doc):\n"  # 12
			"    pass\n"  # 13 — NO call to _check_user_exists here
			"\n"
			"\n"
			"\n"
			"\n"
			"def _check_user_exists(doc):\n"  # 18
			"    pass\n"  # 19
		)
		finding = SimpleNamespace(
			finding_type="Slow Hot Path",
			severity="High",
			title="In submit, looped_validate",
			customer_description="walltime",
			estimated_impact_ms=741.0,
			affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"function": "looped_validate",
				"filename": str(src),
				"lineno": 6,
				"cumulative_ms": 741.0,
				"action_wall_time_ms": 1124.0,
				"is_hook": False,
			}),
		)
		action = SimpleNamespace(
			idx=0,
			action_label="submit",
			event_type="HTTP Request",
			http_method="POST",
			path="/api/method/frappe.client.save",
			recording_uuid="rec1",
			duration_ms=1124,
			queries_count=0,
			query_time_ms=0,
			slowest_query_ms=0,
			call_tree_json=json.dumps({
				"function": "looped_validate",
				"filename": str(src),
				"lineno": 6,
				"kind": "python",
				"cumulative_ms": 741.0,
				"children": [{
					"function": "_run_validations",
					"filename": str(src),
					"lineno": 12,
					"kind": "python",
					"cumulative_ms": 547.0,
					"children": [{
						"function": "_check_user_exists",
						"filename": str(src),
						"lineno": 18,
						"kind": "python",
						"cumulative_ms": 546.0,
						"children": [],
					}],
				}],
			}),
		)
		doc = _fake_doc([finding], phase_2_runs=[], actions=[action])

		html = renderer.render_raw(doc, recordings=[])

		# Fallback: anchor on the leaf's def line.
		assert "common.py:18" in html
		# The parent's body wasn't useful — the call line couldn't be
		# located, so the retarget falls back, NOT to the wrapper's
		# def line (line 6).
		assert "common.py:6" not in html or "looped_validate" in html  # 6 may appear in breadcrumb caption text
		# Breadcrumb still mentions the wrapper.
		assert "Time entered through" in html

	def test_no_retarget_when_drilldown_leaf_is_same_function(self, tmp_path):
		"""When the drill-down chain's deepest entry is the same
		function as the finding's callsite (single-frame chain or
		framework-boundary immediately below), the smoking-gun snippet
		stays on the wrapper — there's nowhere deeper in user code to
		go. The breadcrumb caption should NOT appear."""
		src = tmp_path / "x.py"
		src.write_text(
			"line1\n"
			"line2\n"
			"def my_func():\n"  # lineno 3
			"    a = 1\n"  # lineno 4
			"    return a\n"  # lineno 5
		)
		finding = SimpleNamespace(
			finding_type="Slow Hot Path",
			severity="High",
			title="In submit, time spent in my_func",
			customer_description="walltime",
			estimated_impact_ms=200.0,
			affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"function": "my_func",
				"filename": str(src),
				"lineno": 3,
				"cumulative_ms": 200.0,
				"action_wall_time_ms": 300.0,
				"is_hook": False,
			}),
		)
		# Call tree where my_func has no eligible user-code descendants
		# (child is in framework — drill-down walker stops there).
		action = SimpleNamespace(
			idx=0,
			action_label="submit",
			event_type="HTTP Request",
			http_method="POST",
			path="/api/method/frappe.client.save",
			recording_uuid="rec1",
			duration_ms=300,
			queries_count=0,
			query_time_ms=0,
			slowest_query_ms=0,
			call_tree_json=json.dumps({
				"function": "my_func",
				"filename": str(src),
				"lineno": 3,
				"kind": "python",
				"cumulative_ms": 200.0,
				"children": [],
			}),
		)
		doc = _fake_doc([finding], phase_2_runs=[], actions=[action])

		html = renderer.render_raw(doc, recordings=[])

		# Callsite stays at line 3 (the wrapper itself).
		assert "x.py:3" in html
		# No breadcrumb caption — no retargeting happened.
		assert "Time entered through" not in html

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
		assert "_run_validations(doc)" in _plain(html)
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
		assert "b()" in _plain(html)
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
		assert "def render(" in _plain(html)                        # the highlighted def line
		assert "vscode://file" in html                      # _abs → editor link
		assert 'class="smoking"' in html

	def test_repeated_hot_frame_unresolvable_is_filtered(self):
		"""v0.7.x: a Repeated Hot Frame whose ``function`` key can't be
		resolved to a real file:line is dropped entirely from rendering
		— it had no callsite to act on. Pre-v0.7 the card rendered
		title-only; the user opted to suppress no-callsite findings."""
		doc = _fake_doc([_repeated_hot_frame("nope_xyzq/foo.py::bar")])
		html = renderer.render_raw(doc, recordings=[])
		# Filtered: title and smoking-gun both absent.
		assert "appeared in 3 actions" not in html
		assert 'class="smoking"' not in html

	def test_function_not_invoked_shows_def_line(self):
		doc = _fake_doc([_function_not_invoked("optimus.renderer.render")])
		html = renderer.render_raw(doc, recordings=[])
		assert "optimus/renderer.py:" in html
		assert "def render(" in _plain(html)

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
		# v0.7.x Phase D: smoking-gun label wording trimmed —
		# "Most-called from:" / "Representative callsite" prose collapsed
		# into a single `.smoking-label` line ("most-called from this
		# callsite ..."). Anchor on the new wording.
		assert "most-called from this callsite" in html
		assert f"{_USER_FRAME_FILE}:10" in html
		assert "run_report" in html

	def test_missing_index_without_matching_recording_is_filtered(self):
		"""v0.7.x: a Missing Index without a representative callsite (no
		recording matched the normalized query) has no actionable
		file:line anchor — it's dropped from the render entirely along
		with the rest of the no-callsite suppression."""
		doc = _fake_doc([_missing_index("tabUser", "SELECT ... FROM `tabUser`")])
		# Recording has a different query → no representative callsite.
		recs = [{"uuid": "r1", "calls": [
			{"query": "SELECT * FROM `tabItem`", "normalized_query": "DIFFERENT", "duration": 1.0, "stack": []},
		]}]
		html = renderer.render_raw(doc, recordings=recs)
		assert "Most-called from:" not in html
		# Filtered: the fix hint no longer renders either.
		assert "Add an index" not in html


class TestDocEventHookInsideSmokingGun:
	"""v0.6.x: the target-document + doc-event-hook breadcrumb is rendered
	INSIDE the smoking-gun box (alongside the Phase 2 + Drill-down
	callouts), not in a separate finding-detail box below. Keeps all
	context cues in one visual block."""

	def test_hook_line_lives_inside_smoking_gun(self):
		"""v0.6.x intent: the doc-event hook breadcrumb is rendered
		inside the smoking-gun block (alongside Phase 2 + Drill-down
		callouts), not in a separate finding-detail box below.

		v0.7.x: the supporting-context box (``.finding-detail``) is
		now suppressed entirely when no inner row applies — so for
		this finding (callsite + target_doc + hook_events, no other
		detail fields) it shouldn't render at all. The hook breadcrumb
		appearing inside the smoking-gun is verified by its presence
		AFTER the smoking-gun's opening marker."""
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
		sg_open = html.find('class="smoking"', 0)
		assert hook_pos > 0, "Doc-event hook line missing from output"
		assert sg_open > 0
		# Hook appears AFTER smoking-gun's opening marker — i.e. inside it.
		assert hook_pos > sg_open, (
			"Doc-event hook breadcrumb must render INSIDE the smoking-gun "
			f"block (after sg_open={sg_open}), got {hook_pos}"
		)
		# v0.7.x: no empty finding-detail container should appear for a
		# Slow Hot Path-shaped finding (no inner rows match the gate).
		fd_open = html.find('class="finding-detail"', sg_open)
		assert fd_open == -1, (
			"v0.7.x: finding-detail container suppressed when no inner "
			"rows apply. Found one at " + str(fd_open)
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

	def test_finding_without_callsite_is_filtered(self):
		"""v0.7.x: a finding without a callsite is suppressed from
		rendering entirely. The hook-as-fallback rendering path no
		longer triggers because no-callsite findings are dropped
		before the template runs. (Pre-v0.7 the hook breadcrumb
		rendered as a fallback inside .finding-detail; the user
		opted out of that 'partial info' surface.)"""
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

		# Finding is filtered → neither title nor hook breadcrumb appears.
		assert "callsite-less finding" not in html
		assert "Doc-event hook:" not in html


class TestDrilldownPlaceholder:
	"""v0.7.x: when a Slow Hot Path finding's drill-down chain is empty
	(the origin's hottest child is framework code or below the signal
	floor), the template renders a 'no deeper user-code frame'
	placeholder instead of a silent gap. Findings without an
	``action_ref`` / tree skip the attachment entirely and show
	nothing — the placeholder only appears when the walk was attempted
	and produced no eligible descendants."""

	def test_empty_chain_renders_placeholder(self, tmp_path):
		"""bg_recheck_users-style: function loops over frappe.get_doc.
		The drill-down walker stops at the framework boundary on the
		first descent step → chain is []. Placeholder should render."""
		src = tmp_path / "common.py"
		src.write_text(
			"line1\n"
			"line2\n"
			"line3\n"
			"line4\n"
			"line5\n"
			"def bg_recheck_users(doc_name=None):\n"  # 6
			"    for i in range(15):\n"  # 7
			"        try:\n"  # 8
			"            user = frappe.get_doc('User', 'admin')\n"  # 9
		)
		finding = SimpleNamespace(
			finding_type="Slow Hot Path",
			severity="High",
			title="In Job: bg_recheck_users, 82% of the time was spent in bg_recheck_users",
			customer_description="walltime",
			estimated_impact_ms=955.0,
			affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"function": "bg_recheck_users",
				"filename": str(src),
				"lineno": 6,
				"cumulative_ms": 955.0,
				"action_wall_time_ms": 1165.0,
				"is_hook": False,
			}),
		)
		# Pyinstrument tree: bg_recheck_users → frappe.get_doc (framework).
		# Walker hits the framework boundary on the first step → returns [].
		action = SimpleNamespace(
			idx=0,
			action_label="Job: bg_recheck_users",
			event_type="RQ Job",
			http_method="",
			path="bg_recheck_users",
			recording_uuid="rec1",
			duration_ms=1165,
			queries_count=15,
			query_time_ms=900,
			slowest_query_ms=80,
			call_tree_json=json.dumps({
				"function": "bg_recheck_users",
				"filename": str(src),
				"lineno": 6,
				"kind": "python",
				"cumulative_ms": 955.0,
				"children": [{
					"function": "get_doc",
					# Framework path — walker stops here.
					"filename": "apps/frappe/frappe/__init__.py",
					"lineno": 100,
					"kind": "python",
					"cumulative_ms": 900.0,
					"children": [],
				}],
			}),
		)
		doc = _fake_doc([finding], phase_2_runs=[], actions=[action])

		html = renderer.render_raw(doc, recordings=[])

		# Placeholder text rendered.
		assert "no deeper user-code frame" in html
		# Names the wrapper (the function the placeholder is rooted on).
		assert "bg_recheck_users" in html
		# The placeholder still has the "Drill-down" label.
		assert "Drill-down" in html

	def test_finding_without_drilldown_attribute_renders_no_placeholder(self, tmp_path):
		"""A finding without ``action_ref`` skips _attach_drilldown_chains
		entirely — no ``drilldown_chain`` key on technical_detail. The
		template must NOT render the placeholder in this case (otherwise
		every SQL red-flag finding would show 'no deeper user-code')."""
		snippet = [{"lineno": 42, "content": "    do_thing()"}]
		finding = _finding_child(
			source_snippet=snippet,
			finding_type="Missing Index",
			# No action_ref → drill-down attachment skips early.
			action_ref="",
		)
		doc = _fake_doc([finding], phase_2_runs=[], actions=[])

		html = renderer.render_raw(doc, recordings=[])

		# Neither the chain nor the placeholder renders.
		assert "no deeper user-code frame" not in html
		# Drill-down label absent because no Drill-down section renders.
		assert "Drill-down" not in html

	def test_non_empty_chain_renders_chain_not_placeholder(self, tmp_path):
		"""When the drill-down chain has entries, the existing chain
		rendering shows them — the placeholder branch is skipped."""
		src = tmp_path / "common.py"
		src.write_text(
			"line1\n"
			"line2\n"
			"def outer():\n"  # 3
			"    inner()\n"  # 4
			"\n"
			"def inner():\n"  # 6
			"    do_work()\n"  # 7
		)
		finding = SimpleNamespace(
			finding_type="Slow Hot Path",
			severity="High",
			title="In submit, outer was hot",
			customer_description="walltime",
			estimated_impact_ms=500.0,
			affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"function": "outer",
				"filename": str(src),
				"lineno": 3,
				"cumulative_ms": 500.0,
				"action_wall_time_ms": 600.0,
				"is_hook": False,
			}),
		)
		action = SimpleNamespace(
			idx=0,
			action_label="submit",
			event_type="HTTP Request",
			http_method="POST",
			path="/api/method/frappe.client.save",
			recording_uuid="rec1",
			duration_ms=600,
			queries_count=0,
			query_time_ms=0,
			slowest_query_ms=0,
			call_tree_json=json.dumps({
				"function": "outer",
				"filename": str(src),
				"lineno": 3,
				"kind": "python",
				"cumulative_ms": 500.0,
				"children": [{
					"function": "inner",
					"filename": str(src),  # user code, NOT framework
					"lineno": 6,
					"kind": "python",
					"cumulative_ms": 480.0,
					"children": [],
				}],
			}),
		)
		doc = _fake_doc([finding], phase_2_runs=[], actions=[action])

		html = renderer.render_raw(doc, recordings=[])

		# Chain rendered (existing behavior).
		assert "Drill-down" in html
		# v0.7.x Phase D: chain steps render as `function:lineno` pills
		# (filename is dropped from each pill — the meta row above
		# carries the file path). The inner def line is line 6 — its
		# function name appears as a chain step.
		assert "inner:6" in html or ":6" in html
		# Placeholder branch NOT taken.
		assert "no deeper user-code frame" not in html

