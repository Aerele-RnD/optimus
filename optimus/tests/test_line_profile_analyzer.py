# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for optimus.line_profile.analyzer.analyze — the pure
function that turns aggregated line-profile results into findings + per-
function aggregate."""

import json as _json

from optimus.line_profile import analyzer


def _line(lineno, content, hits, total_ms):
	return {
		"lineno": lineno,
		"content": content,
		"content_hash": f"hash_{lineno}",
		"hits": hits,
		"total_ms": total_ms,
		"per_hit_us": (total_ms * 1000.0 / hits) if hits else 0,
	}


def _function(dotted_path, lines, file="/fake/path.py"):
	return {
		"dotted_path": dotted_path,
		"qualname": dotted_path.rsplit(".", 1)[-1],
		"file": file,
		"lines": lines,
	}


class TestAnalyzeEmpty:
	def test_no_functions_returns_empty_result(self):
		result = analyzer.analyze([])

		assert result.findings == []
		assert result.aggregate == {"phase2_functions": []}
		assert result.warnings == []


class TestHotLineFinding:
	def test_hot_line_title_uses_short_qualname_and_fits_140_chars(self):
		# Regression: a deeply-nested dotted_path must not overflow the
		# Optimus Finding.title Data(140) field while stopping phase 2.
		long_path = (
			"ajanta_bottles.ajanta_bottles.custom.sales_order.sales_order."
			"ABSalesOrder.send_sr_sales_manager_alerts"
		)
		fn = _function(long_path, [
			_line(1, "x = setup()", 5, 20.0),
			_line(170, "send_alert()", 13, 251.0),  # dominant hot line
			_line(3, "return", 1, 0.0),
		])
		result = analyzer.analyze([fn])
		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		title = hot[0]["title"]
		assert len(title) <= 140
		assert long_path not in title                       # full path dropped
		assert "send_sr_sales_manager_alerts" in title      # short qualname kept
		assert "consumed 251ms (13 hits)" in title
		# the full path still travels in technical_detail for navigation
		import json
		assert json.loads(hot[0]["technical_detail_json"])["dotted_path"] == long_path

	def test_one_line_dominates_high_severity(self):
		# 80% of total time on one line, total > 100ms → High
		fn = _function("my_app.x", [
			_line(1, "x = setup()", 5, 20.0),
			_line(2, "y = compute_heavy()", 100, 480.0),  # 480/500 = 96%
			_line(3, "return y", 100, 0.0),
		])

		result = analyzer.analyze([fn])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		assert hot[0]["severity"] == "High"

	def test_one_line_30_percent_medium_severity(self):
		# Total 200ms, hot line 60ms = 30% → Medium (>25% AND >50ms)
		fn = _function("my_app.x", [
			_line(1, "a = 1", 1, 70.0),
			_line(2, "b = compute()", 1, 60.0),  # 30%
			_line(3, "c = 3", 1, 70.0),
		])

		result = analyzer.analyze([fn])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		assert hot[0]["severity"] == "Medium"

	def test_no_concentration_no_finding(self):
		# Time evenly distributed → no Hot Line
		fn = _function("my_app.x", [
			_line(1, "a = 1", 1, 20.0),
			_line(2, "b = 2", 1, 20.0),
			_line(3, "c = 3", 1, 20.0),
			_line(4, "d = 4", 1, 20.0),
			_line(5, "e = 5", 1, 20.0),
		])

		result = analyzer.analyze([fn])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert hot == []

	def test_below_threshold_total_ms_no_finding(self):
		# Even though one line is 100% of the function's time, total is
		# only 30ms — below the 50ms Medium floor → no finding.
		fn = _function("my_app.x", [
			_line(1, "trivial()", 1, 30.0),
			_line(2, "more = 2", 1, 0.0),
		])

		result = analyzer.analyze([fn])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert hot == []

	def test_finding_has_callsite_and_impact(self):
		fn = _function("my_app.heavy.compute", [
			_line(10, "for x in items:", 1000, 10.0),
			_line(11, "    cache[x] = expensive(x)", 1000, 800.0),
		])

		result = analyzer.analyze([fn])

		hot = result.findings[0]
		assert hot["estimated_impact_ms"] >= 500
		# technical_detail should locate the offending line
		import json as _json
		td = _json.loads(hot["technical_detail_json"])
		assert td["dotted_path"] == "my_app.heavy.compute"
		assert td["lineno"] == 11
		assert "cache" in td["line_content"]


class TestFunctionNotInvoked:
	# v0.7.x: a picked-but-uninvoked function no longer emits a per-function
	# "Function Not Invoked" finding (it cluttered the Findings list). It's
	# folded into ONE consolidated warning instead — the report stays clean.
	def test_empty_lines_emits_warning_not_finding(self):
		fn = _function("my_app.never_runs", [])

		result = analyzer.analyze([fn])

		assert not [f for f in result.findings if f["finding_type"] == "Function Not Invoked"]
		assert any("never_runs" in w for w in result.warnings)

	def test_all_zero_hits_emits_warning_not_finding(self):
		# Some line_profiler versions still report the lines but with hits=0.
		fn = _function("my_app.never_runs", [
			_line(1, "x = 1", 0, 0.0),
			_line(2, "return x", 0, 0.0),
		])

		result = analyzer.analyze([fn])

		assert not [f for f in result.findings if f["finding_type"] == "Function Not Invoked"]
		assert any("never_runs" in w for w in result.warnings)

	def test_multiple_uninvoked_share_one_warning(self):
		result = analyzer.analyze([
			_function("my_app.a", []),
			_function("my_app.b", [_line(1, "x = 1", 0, 0.0)]),
		])

		assert not [f for f in result.findings if f["finding_type"] == "Function Not Invoked"]
		# One consolidated warning names every uninvoked pick.
		consolidated = [w for w in result.warnings if "my_app.a" in w and "my_app.b" in w]
		assert len(consolidated) == 1


class TestAggregate:
	def test_per_function_summary_in_aggregate(self):
		fn1 = _function("my_app.a", [_line(1, "x = 1", 1, 50.0), _line(2, "y = 2", 1, 30.0)])
		fn2 = _function("my_app.b", [_line(1, "z = 3", 1, 200.0)])

		result = analyzer.analyze([fn1, fn2])

		assert "phase2_functions" in result.aggregate
		summaries = result.aggregate["phase2_functions"]
		paths = {s["dotted_path"]: s for s in summaries}
		assert "my_app.a" in paths
		assert "my_app.b" in paths
		assert paths["my_app.a"]["total_ms"] == 80.0
		assert paths["my_app.b"]["total_ms"] == 200.0

	def test_aggregate_lists_top_5_hot_lines(self):
		# 7 lines; aggregate should keep top 5 by total_ms
		lines = [_line(i, f"line_{i}", 1, float(i * 10)) for i in range(1, 8)]
		fn = _function("my_app.many_lines", lines)

		result = analyzer.analyze([fn])

		summary = result.aggregate["phase2_functions"][0]
		hot = summary["hot_lines"]
		assert len(hot) == 5
		# Should be sorted desc by total_ms; top is line 7 (70ms), bottom is line 3 (30ms)
		assert hot[0]["lineno"] == 7
		assert hot[-1]["lineno"] == 3

	def test_summary_omits_lines_for_not_invoked(self):
		fn = _function("my_app.nope", [])

		result = analyzer.analyze([fn])

		summary = result.aggregate["phase2_functions"][0]
		assert summary["dotted_path"] == "my_app.nope"
		assert summary["total_ms"] == 0
		assert summary["hot_lines"] == []


class TestLeafPickAndChain:
	"""When auto_expand instruments a chain of functions and the outer
	function's hot line is just a call into a deeper instrumented one,
	the outer finding should be suppressed and the deeper (leaf) finding
	should carry a ``call_chain`` breadcrumb."""

	def test_pass_through_finding_is_suppressed_when_callee_is_instrumented(self):
		# Outer: hot line at lineno 7 is a call into _check_user_exists.
		# Inner: hot line at lineno 20 is the real leaf (the get_doc call).
		outer = _function("my_app.common.looped_validate", [
			_line(1, "def looped_validate(doc):", 1, 0.0),
			_line(7, "    _check_user_exists(doc)", 2, 370.0),  # pass-through
		])
		inner = _function("my_app.common._check_user_exists", [
			_line(15, "def _check_user_exists(doc):", 2, 0.0),
			_line(20, "        user = frappe.get_doc('User', frappe.session.user)", 100, 340.0),
			_line(21, "        if user.enabled:", 100, 5.0),
		])

		result = analyzer.analyze([outer, inner])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		# Only one finding — the leaf.
		assert len(hot) == 1
		td = _json.loads(hot[0]["technical_detail_json"])
		assert td["dotted_path"] == "my_app.common._check_user_exists"
		assert td["lineno"] == 20
		# Chain present, outer → leaf.
		chain = td["call_chain"]
		assert len(chain) == 2
		assert chain[0]["qualname"] == "looped_validate"
		assert chain[0]["lineno"] == 7
		assert chain[-1]["qualname"] == "_check_user_exists"
		assert chain[-1]["lineno"] == 20
		# Customer description prefixed with breadcrumb.
		assert "Time enters through" in hot[0]["customer_description"]
		assert "looped_validate:7" in hot[0]["customer_description"]
		assert "_check_user_exists:20" in hot[0]["customer_description"]

	def test_three_level_chain_breadcrumb(self):
		# A → B → C, leaf is C with a real hot line at 100.
		a = _function("my_app.x.a", [
			_line(1, "def a():", 1, 0.0),
			_line(2, "    b()", 1, 500.0),  # pass-through to b
		])
		b = _function("my_app.x.b", [
			_line(10, "def b():", 1, 0.0),
			_line(11, "    c()", 1, 490.0),  # pass-through to c
		])
		c = _function("my_app.x.c", [
			_line(20, "def c():", 1, 0.0),
			_line(21, "    for x in range(1000): expensive_op(x)", 1000, 480.0),
		])

		result = analyzer.analyze([a, b, c])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		td = _json.loads(hot[0]["technical_detail_json"])
		assert td["dotted_path"] == "my_app.x.c"
		chain = td["call_chain"]
		assert [step["qualname"] for step in chain] == ["a", "b", "c"]
		assert [step["lineno"] for step in chain] == [2, 11, 21]

	def test_leaf_only_function_finding_unchanged(self):
		# Single function with a real leaf hot line — no chain, no hint.
		fn = _function("my_app.x", [
			_line(1, "for x in items:", 1000, 10.0),
			_line(2, "    cache[x] = expensive(x)", 1000, 800.0),
		])

		result = analyzer.analyze([fn])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		td = _json.loads(hot[0]["technical_detail_json"])
		assert "call_chain" not in td
		assert "phase1_hint" not in td

	def test_recursive_self_call_is_not_suppressed(self):
		# A function that calls itself recursively — hot line is the
		# recursive call. Self-recursion shouldn't trigger pass-through
		# suppression; the finding stands.
		fn = _function("my_app.recur", [
			_line(1, "def recur(n):", 1, 0.0),
			_line(2, "    if n <= 0: return", 100, 5.0),
			_line(3, "    recur(n - 1)", 100, 600.0),  # self-call, NOT pass-through
		])

		result = analyzer.analyze([fn])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		td = _json.loads(hot[0]["technical_detail_json"])
		assert td["dotted_path"] == "my_app.recur"
		assert td["lineno"] == 3
		assert "call_chain" not in td

	def test_phase1_hint_attached_when_callee_not_instrumented(self):
		# Single function whose hot line calls into something we DIDN'T
		# instrument. Provide a phase-1 tree containing that callee with
		# cumulative_ms above the 50ms floor. Expect: phase1_hint
		# populated on the finding.
		# File path is chosen so _derive_module_path yields "my_app.common".
		fn = _function(
			"my_app.common.looped_validate",
			[
				_line(1, "def looped_validate(doc):", 1, 0.0),
				_line(7, "    _check_user_exists(doc)", 2, 370.0),
			],
			file="my_app/common.py",
		)
		# Minimal pyinstrument-shaped tree: the looped_validate frame
		# with one user-code child _check_user_exists at 340ms.
		tree = {
			"function": "looped_validate",
			"filename": "my_app/common.py",
			"lineno": 1,
			"kind": "python",
			"cumulative_ms": 370.0,
			"children": [
				{
					"function": "_check_user_exists",
					"filename": "my_app/common.py",
					"lineno": 15,
					"kind": "python",
					"cumulative_ms": 340.0,
					"children": [],
				},
			],
		}

		result = analyzer.analyze([fn], call_trees=[tree])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		td = _json.loads(hot[0]["technical_detail_json"])
		hint = td.get("phase1_hint")
		assert hint is not None
		assert "_check_user_exists" in hint["next_hot_callee"]
		assert hint["phase1_cumulative_ms"] == 340.0
		# Customer description appended with the hint sentence.
		# v0.7.x A.AE2: reworded for clarity about cross-call aggregation.
		assert "In phase 1, this descendant" in hot[0]["customer_description"]
		assert "_check_user_exists" in hot[0]["customer_description"]

	def test_no_phase1_hint_when_no_eligible_descendant(self):
		# Hot line content doesn't look like a call site — no hint.
		fn = _function("my_app.common.heavy_arith", [
			_line(1, "def heavy_arith(n):", 1, 0.0),
			_line(2, "    return sum(i*i for i in range(n))", 1, 600.0),  # not a call site
		])
		tree = {
			"function": "heavy_arith",
			"filename": "/fake/path.py",
			"lineno": 1,
			"kind": "python",
			"cumulative_ms": 600.0,
			"children": [],
		}

		result = analyzer.analyze([fn], call_trees=[tree])

		hot = [f for f in result.findings if f["finding_type"] == "Hot Line"]
		assert len(hot) == 1
		td = _json.loads(hot[0]["technical_detail_json"])
		# Content doesn't match the call-site shape, so no hint attempt.
		assert "phase1_hint" not in td
