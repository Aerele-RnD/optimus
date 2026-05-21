# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The finding-card "Line-Level Drilldown" callout links a finding to its
phase-2 hot line via (file-basename, function-name). The index is keyed on the
phase-2 result's ``qualname``; the lookup uses the finding's callsite
``function``. These can disagree on the prefix — ``resolve_freeform`` may emit
``common.bg_recheck_users`` (module/class walked) while call_tree stores the
bare ``bg_recheck_users`` — which silently breaks the callout even though the
function WAS profiled (and shows in the panel). Matching must be robust to that
on both sides; this pins it.
"""

import json
from types import SimpleNamespace

from optimus import renderer


def _doc(qualname, file_path="/abs/apps/ugly_code/ugly_code/python/common.py", total_ms=300.0):
	return SimpleNamespace(phase_2_runs=[SimpleNamespace(
		run_uuid="r1", status="Ready",
		results_json=json.dumps([{
			"dotted_path": "ugly_code.python.common.bg_recheck_users",
			"qualname": qualname,
			"file": file_path,
			"lines": [{"lineno": 204, "content": "x", "total_ms": total_ms, "hits": 100}],
		}]),
	)])


def _lookup(doc, filename, function):
	idx = renderer._build_line_drilldown_callsite_index(doc)
	return renderer._make_line_drilldown_lookup(idx)(filename, function)


class TestDrilldownMatchRobustness:
	def test_bare_function_matches_prefixed_index_qualname(self):
		# Index qualname carries a module prefix; finding looks up the bare name.
		hit = _lookup(_doc("common.bg_recheck_users"),
			"ugly_code/python/common.py", "bg_recheck_users")
		assert hit and hit["lineno"] == 204

	def test_prefixed_function_matches_bare_index_qualname(self):
		# Reverse: index has the bare name, finding looks up a prefixed one.
		hit = _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/common.py", "common.bg_recheck_users")
		assert hit and hit["lineno"] == 204

	def test_exact_match_still_works(self):
		hit = _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/common.py", "bg_recheck_users")
		assert hit and hit["lineno"] == 204

	def test_different_function_does_not_match(self):
		assert _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/common.py", "something_else") is None

	def test_different_basename_does_not_match(self):
		assert _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/other.py", "bg_recheck_users") is None
