# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for frappe_profiler.line_profile.capture.aggregate_samples — the
pure merge step that turns per-request line_profiler stats into the
analyzer's input shape."""

from frappe_profiler.line_profile import capture, diff


def _pick(dotted_path: str, file: str, lines: list[tuple[int, str]]) -> dict:
	return {
		"dotted_path": dotted_path,
		"qualname": dotted_path.rsplit(".", 1)[-1],
		"file": file,
		"first_lineno": lines[0][0] if lines else 0,
		"source_lines": [{"lineno": ln, "content": txt} for ln, txt in lines],
	}


def _sample(file: str, qualname: str, lineno: int, hits: int, total_us: int) -> dict:
	return {
		"file": file,
		"qualname": qualname,
		"lineno": lineno,
		"hits": hits,
		"total_us": total_us,
	}


class TestAggregateSamplesShape:
	def test_no_samples_yields_zeroed_lines_per_pick(self):
		picks = [_pick("my_app.x", "/p/x.py", [(10, "def x():"), (11, "    return 1")])]

		results = capture.aggregate_samples(samples=[], picks=picks)

		assert len(results) == 1
		entry = results[0]
		assert entry["dotted_path"] == "my_app.x"
		assert entry["file"] == "/p/x.py"
		assert len(entry["lines"]) == 2
		for line in entry["lines"]:
			assert line["hits"] == 0
			assert line["total_ms"] == 0.0
			assert line["per_hit_us"] == 0.0

	def test_lines_carry_content_and_content_hash(self):
		picks = [_pick("my_app.x", "/p/x.py", [(10, "    return 1   ")])]

		results = capture.aggregate_samples(samples=[], picks=picks)

		line = results[0]["lines"][0]
		assert line["lineno"] == 10
		assert line["content"] == "    return 1   "
		# hash uses stripped content so trailing whitespace is normalized
		assert line["content_hash"] == diff.content_hash("    return 1   ")
		assert line["content_hash"] == diff.content_hash("return 1")


class TestAggregateSamplesMerging:
	def test_single_sample_attaches_to_matching_line(self):
		picks = [_pick("my_app.x", "/p/x.py", [(10, "def x():"), (11, "    a = 1")])]
		batch = [_sample("/p/x.py", "x", 11, hits=5, total_us=2000)]

		results = capture.aggregate_samples(samples=[batch], picks=picks)

		lines = {l["lineno"]: l for l in results[0]["lines"]}
		assert lines[10]["hits"] == 0
		assert lines[10]["total_ms"] == 0.0
		assert lines[11]["hits"] == 5
		assert lines[11]["total_ms"] == 2.0  # 2000us / 1000 = 2ms
		assert lines[11]["per_hit_us"] == 400.0  # 2000us / 5

	def test_multiple_batches_sum_hits_and_us(self):
		picks = [_pick("my_app.x", "/p/x.py", [(11, "    a = 1")])]
		batch1 = [_sample("/p/x.py", "x", 11, hits=5, total_us=2000)]
		batch2 = [_sample("/p/x.py", "x", 11, hits=3, total_us=900)]

		results = capture.aggregate_samples(samples=[batch1, batch2], picks=picks)

		line = results[0]["lines"][0]
		assert line["hits"] == 8
		assert line["total_ms"] == 2.9  # (2000+900)/1000
		assert line["per_hit_us"] == round(2900 / 8, 2)

	def test_sample_for_line_not_in_source_is_ignored(self):
		# Stale sample referencing a line that no longer exists in current source
		picks = [_pick("my_app.x", "/p/x.py", [(11, "    a = 1")])]
		batch = [_sample("/p/x.py", "x", 99, hits=1, total_us=100)]

		results = capture.aggregate_samples(samples=[batch], picks=picks)

		assert len(results[0]["lines"]) == 1
		assert results[0]["lines"][0]["lineno"] == 11
		assert results[0]["lines"][0]["hits"] == 0  # nothing attached

	def test_sample_for_unknown_function_is_ignored(self):
		picks = [_pick("my_app.x", "/p/x.py", [(11, "    a = 1")])]
		batch = [_sample("/p/other.py", "unknown", 1, hits=10, total_us=5000)]

		results = capture.aggregate_samples(samples=[batch], picks=picks)

		assert results[0]["lines"][0]["hits"] == 0

	def test_zero_hit_line_per_hit_us_is_zero(self):
		picks = [_pick("my_app.x", "/p/x.py", [(11, "    a = 1")])]

		results = capture.aggregate_samples(samples=[], picks=picks)

		# avoid division-by-zero
		assert results[0]["lines"][0]["per_hit_us"] == 0.0


class TestAggregateSamplesMultiplePicks:
	def test_each_pick_gets_its_own_entry(self):
		picks = [
			_pick("my_app.alpha", "/p/a.py", [(1, "def alpha(): pass")]),
			_pick("my_app.beta", "/p/b.py", [(1, "def beta(): pass")]),
		]
		batch = [
			_sample("/p/a.py", "alpha", 1, hits=2, total_us=200),
			_sample("/p/b.py", "beta", 1, hits=5, total_us=500),
		]

		results = capture.aggregate_samples(samples=[batch], picks=picks)

		assert len(results) == 2
		by_path = {r["dotted_path"]: r for r in results}
		assert by_path["my_app.alpha"]["lines"][0]["hits"] == 2
		assert by_path["my_app.beta"]["lines"][0]["hits"] == 5

	def test_results_order_matches_picks_order(self):
		picks = [
			_pick("my_app.beta", "/p/b.py", [(1, "x")]),
			_pick("my_app.alpha", "/p/a.py", [(1, "x")]),
		]

		results = capture.aggregate_samples(samples=[], picks=picks)

		assert [r["dotted_path"] for r in results] == ["my_app.beta", "my_app.alpha"]
