# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for optimus.line_profile.analyzer.analyze — the pure
function that turns aggregated line-profile results into findings + per-
function aggregate."""

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


def _function(dotted_path, lines):
	return {
		"dotted_path": dotted_path,
		"qualname": dotted_path.rsplit(".", 1)[-1],
		"file": "/fake/path.py",
		"lines": lines,
	}


class TestAnalyzeEmpty:
	def test_no_functions_returns_empty_result(self):
		result = analyzer.analyze([])

		assert result.findings == []
		assert result.aggregate == {"phase2_functions": []}
		assert result.warnings == []


class TestHotLineFinding:
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
	def test_empty_lines_emits_warning_and_finding(self):
		fn = _function("my_app.never_runs", [])

		result = analyzer.analyze([fn])

		not_invoked = [f for f in result.findings if f["finding_type"] == "Function Not Invoked"]
		assert len(not_invoked) == 1
		assert not_invoked[0]["severity"] == "Low"
		assert "my_app.never_runs" in not_invoked[0]["title"]

		assert any("never_runs" in w for w in result.warnings)

	def test_all_zero_hits_emits_warning(self):
		# Some line_profiler versions still report the lines but with hits=0
		fn = _function("my_app.never_runs", [
			_line(1, "x = 1", 0, 0.0),
			_line(2, "return x", 0, 0.0),
		])

		result = analyzer.analyze([fn])

		not_invoked = [f for f in result.findings if f["finding_type"] == "Function Not Invoked"]
		assert len(not_invoked) == 1


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
