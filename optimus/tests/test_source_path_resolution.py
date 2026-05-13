# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for resolving a finding's app-relative callsite path to a real file.

Call-tree / pyinstrument callsites are stored as `<app>/<module-path>` (e.g.
`ugly_code/python/common.py` for `<bench>/apps/ugly_code/ugly_code/python/
common.py`). A bare `open()` fails because the Frappe worker cwd is
`<bench>/sites` — so the AI-fix prompt and the report's "smoking gun" snippet
never got the source. `renderer._resolve_source_path` fixes that; the source
readers and the analyze-time enrichment route through it.
"""

import json
import os
import types

from optimus import analyze, renderer

# A real, always-present app-relative path: this very test file's package home.
_APP_REL = "optimus/renderer.py"
_ABS_REAL = renderer.__file__  # the resolved absolute path of the same file


class TestResolveSourcePath:
	def test_resolves_app_relative_path(self):
		# In production (cwd = <bench>/sites) this returns the absolute path via
		# frappe.get_app_path; under pytest (cwd = apps/<repo>) the cwd-relative
		# short form already exists and is returned as-is. Either way it must
		# point at the real file under the renamed ``optimus`` package. The
		# outer checkout directory may be ``apps/optimus/`` (post-repo-rename)
		# or ``apps/frappe_profiler/`` (legacy bench installs upgrading
		# in-place), so the assertion only pins the inner package trailing
		# path.
		resolved = renderer._resolve_source_path(_APP_REL)
		assert resolved is not None and os.path.exists(resolved)
		assert os.path.abspath(resolved).replace("\\", "/").endswith(
			"/optimus/renderer.py"
		)

	def test_resolves_a_frappe_core_relative_path(self):
		# frappe/__init__.py → <bench>/apps/frappe/frappe/__init__.py via
		# frappe.get_app_path. (In the full suite another test may have left a
		# stub `frappe` in sys.modules — then this branch can't resolve and
		# returns None; that's tolerated. It must never return a bogus path.)
		resolved = renderer._resolve_source_path("frappe/__init__.py")
		assert resolved is None or os.path.exists(resolved)

	def test_passes_through_existing_absolute_path(self):
		assert renderer._resolve_source_path(_ABS_REAL) == _ABS_REAL

	def test_none_for_synthetic_or_empty(self):
		for v in ("<string>", "<frozen importlib._bootstrap>", "", None, "  "):
			assert renderer._resolve_source_path(v) is None

	def test_none_for_nonexistent_app_relative(self):
		assert renderer._resolve_source_path("optimus/this_does_not_exist_xyzq.py") is None

	def test_none_for_missing_absolute(self):
		assert renderer._resolve_source_path("/tmp/definitely/not/here/foo_xyzq.py") is None


class TestReadersUseTheResolver:
	def test_read_source_window_works_on_app_relative_path(self):
		window = renderer._read_source_window(_APP_REL, 1, before=2, after=5)
		assert window, "source window must be readable via the app-relative path"
		# line 1 is the target.
		assert any(row.get("is_target") and row.get("lineno") == 1 for row in window)

	def test_read_source_snippet_works_on_app_relative_path(self):
		snippet = renderer._read_source_snippet(_APP_REL, 5)
		assert snippet and any(row.get("lineno") == 5 for row in snippet)

	def test_unreadable_path_still_returns_none(self):
		assert renderer._read_source_window("nope/nope_xyzq.py", 3) is None
		assert renderer._read_source_snippet("<string>", 3) is None


class TestAnalyzeEnrichmentResolves:
	def test_enriches_call_tree_finding_with_app_relative_callsite(self):
		# call_tree findings store the location at the TOP level (no `callsite`
		# wrapper). _enrich_findings_with_source_snippets must synthesize the
		# callsite AND attach a snippet — resolving the relative path.
		findings = [{
			"title": "In X, 60% of the time was spent in render",
			"technical_detail_json": json.dumps({
				"filename": _APP_REL, "lineno": 1, "function": "render",
				"cumulative_ms": 100, "action_wall_time_ms": 200,
			}),
		}]
		analyze._enrich_findings_with_source_snippets(findings)
		detail = json.loads(findings[0]["technical_detail_json"])
		cs = detail.get("callsite") or {}
		assert cs.get("filename") == _APP_REL and cs.get("function") == "render"
		assert cs.get("source_snippet"), "a ±1-line snippet should have been attached"
		assert any(s.get("lineno") == 1 for s in cs["source_snippet"])

	def test_unreadable_callsite_left_alone(self):
		findings = [{
			"title": "x",
			"technical_detail_json": json.dumps({"filename": "nope/nope_xyzq.py", "lineno": 5, "function": "f"}),
		}]
		analyze._enrich_findings_with_source_snippets(findings)
		detail = json.loads(findings[0]["technical_detail_json"])
		# No crash, no bogus snippet (the callsite may get synthesized for
		# _finding_to_dict's benefit — that's fine).
		cs = detail.get("callsite") or {}
		assert not cs.get("source_snippet")


class TestAiPayloadForFinding:
	def test_gets_source_window_and_phase2_hotline_for_hot_path_finding(self):
		child = types.SimpleNamespace(
			finding_type="Slow Hot Path", severity="High",
			title="In Submit X, 62% of the time was spent in render",
			customer_description="In Submit X, 62% of the time was spent in render",
			estimated_impact_ms=679, affected_count=0, action_ref="1",
			technical_detail_json=json.dumps({
				"filename": _APP_REL, "lineno": 1, "function": "render",
				"cumulative_ms": 679, "action_wall_time_ms": 1095,
			}),
			llm_fix_json=None,
		)
		phase2_index = {("renderer.py", "render"): {
			"lineno": 1, "content": "first line", "total_ms": 387, "hits": 2,
			"run_uuid": "r", "dotted_path": "optimus.renderer.render",
		}}
		payload = analyze._ai_payload_for_finding(child, {}, phase2_index=phase2_index)
		assert payload.get("source_window"), "a wider source window should be attached"
		assert any(row.get("is_target") for row in payload["source_window"])
		assert payload.get("phase2_hotline", {}).get("lineno") == 1
		# source_available True (we have both a window and a phase2 hotline).
		from optimus import ai_fix
		assert ai_fix._had_concrete_context(payload) is True

	def test_no_phase2_hotline_when_function_not_instrumented(self):
		child = types.SimpleNamespace(
			finding_type="Slow Hot Path", severity="High", title="x", customer_description="",
			estimated_impact_ms=10, affected_count=0, action_ref="0",
			technical_detail_json=json.dumps({"filename": _APP_REL, "lineno": 1, "function": "render"}),
			llm_fix_json=None,
		)
		payload = analyze._ai_payload_for_finding(child, {}, phase2_index={("other.py", "other"): {"lineno": 9}})
		assert "phase2_hotline" not in payload


class TestAiPayloadRecordedQueries:
	"""v0.6.x: pass actual recorded SQL queries to the AI as evidence so
	the model has the verbatim query text to ground against, instead of
	inferring SQL shape from the Python source — which was the leading
	cause of bogus refactorings (e.g. inventing filters that copy a
	variable from elsewhere in the function)."""

	def _child(self, action_ref="0"):
		return types.SimpleNamespace(
			finding_type="Slow Hot Path", severity="High",
			title="In Job: bg_recheck_users, 75% spent in bg_recheck_users",
			customer_description="",
			estimated_impact_ms=575, affected_count=0, action_ref=action_ref,
			technical_detail_json=json.dumps({
				"filename": _APP_REL, "lineno": 1, "function": "render",
				"cumulative_ms": 575, "action_wall_time_ms": 770,
			}),
			llm_fix_json=None,
		)

	def test_top_queries_attached_from_recording(self):
		recordings_by_uuid = {"r0": {
			"uuid": "r0",
			"calls": [
				{"duration": 5.0, "query": "SELECT 1"},
				{"duration": 320.0, "query": "SELECT name, email FROM `tabUser` LIMIT 50"},
				{"duration": 12.0, "query": "SELECT * FROM `tabRole`"},
				{"duration": 80.0, "query": "SELECT name FROM `tabSales Invoice`"},
			],
		}}
		actions_by_idx = {0: {"idx": 0, "recording_uuid": "r0"}}
		payload = analyze._ai_payload_for_finding(
			self._child(action_ref="0"), {},
			recordings_by_uuid=recordings_by_uuid,
			actions_by_idx=actions_by_idx,
		)
		examples = payload["technical_detail"].get("example_queries") or []
		# Top-3 by duration, descending: 320ms tabUser, 80ms Sales Invoice, 12ms tabRole.
		assert examples == [
			"SELECT name, email FROM `tabUser` LIMIT 50",
			"SELECT name FROM `tabSales Invoice`",
			"SELECT * FROM `tabRole`",
		]

	def test_no_attach_when_recordings_missing(self):
		"""On-demand path with expired Redis: no recordings → no crash, no
		example_queries key (falls back to existing context)."""
		payload = analyze._ai_payload_for_finding(self._child(), {})
		assert "example_queries" not in (payload["technical_detail"] or {})

	def test_existing_example_queries_not_overwritten(self):
		"""SQL red-flag findings already carry analyzer-picked example
		queries (the offending ones). Recordings-based fallback must not
		overwrite them."""
		child = types.SimpleNamespace(
			finding_type="Missing Index", severity="High",
			title="x", customer_description="",
			estimated_impact_ms=200, affected_count=1, action_ref="0",
			technical_detail_json=json.dumps({
				"callsite": {"filename": _APP_REL, "lineno": 1, "function": "f"},
				"example_queries": ["SELECT * FROM tabFoo WHERE bar = ?"],
			}),
			llm_fix_json=None,
		)
		recordings_by_uuid = {"r0": {
			"uuid": "r0",
			"calls": [{"duration": 500.0, "query": "SELECT * FROM tabBaz"}],
		}}
		actions_by_idx = {0: {"idx": 0, "recording_uuid": "r0"}}
		payload = analyze._ai_payload_for_finding(
			child, {},
			recordings_by_uuid=recordings_by_uuid,
			actions_by_idx=actions_by_idx,
		)
		# Analyzer-picked queries win.
		assert payload["technical_detail"]["example_queries"] == [
			"SELECT * FROM tabFoo WHERE bar = ?"
		]

	def test_sub_threshold_queries_dropped(self):
		"""Trivial sub-half-ms queries (cache hits etc.) are noise — don't
		surface them as 'examples' the AI tries to optimise."""
		recordings_by_uuid = {"r0": {
			"uuid": "r0",
			"calls": [
				{"duration": 0.1, "query": "SELECT 1"},
				{"duration": 0.2, "query": "SELECT 2"},
				{"duration": 50.0, "query": "SELECT name FROM `tabUser`"},
			],
		}}
		actions_by_idx = {0: {"idx": 0, "recording_uuid": "r0"}}
		payload = analyze._ai_payload_for_finding(
			self._child(), {},
			recordings_by_uuid=recordings_by_uuid,
			actions_by_idx=actions_by_idx,
		)
		examples = payload["technical_detail"].get("example_queries") or []
		assert examples == ["SELECT name FROM `tabUser`"]

	def test_unknown_action_ref_skipped(self):
		"""Finding's action_ref doesn't match any action → no recordings
		attachment, no crash."""
		recordings_by_uuid = {"r0": {"uuid": "r0", "calls": []}}
		actions_by_idx = {0: {"idx": 0, "recording_uuid": "r0"}}
		payload = analyze._ai_payload_for_finding(
			self._child(action_ref="42"), {},
			recordings_by_uuid=recordings_by_uuid,
			actions_by_idx=actions_by_idx,
		)
		assert "example_queries" not in (payload["technical_detail"] or {})

	def test_non_numeric_action_ref_skipped(self):
		"""Defensive: non-numeric / empty action_ref → no attachment."""
		recordings_by_uuid = {"r0": {"uuid": "r0", "calls": [
			{"duration": 100.0, "query": "SELECT 1"}
		]}}
		actions_by_idx = {0: {"idx": 0, "recording_uuid": "r0"}}
		payload = analyze._ai_payload_for_finding(
			self._child(action_ref=""), {},
			recordings_by_uuid=recordings_by_uuid,
			actions_by_idx=actions_by_idx,
		)
		assert "example_queries" not in (payload["technical_detail"] or {})
