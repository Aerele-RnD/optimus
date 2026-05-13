# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for analyze._enrich_findings_with_ai_suggestions — the optional
"bake AI fixes into the report" step (Optimus Settings ▸ AI Fix
Suggestions ▸ "Suggest AI fixes by default").

Pure-test path: ``settings.get_config`` / ``ai_fix.is_available`` /
``ai_fix.suggest_fix`` are patched, so no network and no live site. The
function is best-effort and time-budgeted; here we pin the gating,
ordering, capping, and warning behaviour.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from optimus import analyze


def _cfg(**kw):
	base = {
		"ai_enabled": True,
		"ai_auto_suggest": True,
		"ai_auto_suggest_max": 5,
	}
	base.update(kw)
	return SimpleNamespace(**base)


def _finding(finding_type, severity, impact, *, title="t", file="/abs/myapp/foo.py", lineno=10):
	return {
		"finding_type": finding_type,
		"severity": severity,
		"title": title,
		"customer_description": "desc",
		"estimated_impact_ms": impact,
		"affected_count": 1,
		"action_ref": "0",
		"technical_detail_json": json.dumps({
			"callsite": {"filename": file, "lineno": lineno, "function": "fn"},
			"normalized_query": "SELECT 1",
		}),
	}


def _ctx(findings):
	return SimpleNamespace(session_uuid="u", findings=findings, warnings=[])


_FAKE_RESULT = {"suggestion": "**Fix**\n\ndo X", "model": "m", "provider": "OpenAI-compatible",
                "generated_at": "2026-05-11T00:00:00+00:00"}


def _patches(cfg, *, available=True, suggest=None):
	"""Common patch context: config + ai_fix.is_available + ai_fix.suggest_fix."""
	suggest = suggest if suggest is not None else (lambda payload: dict(_FAKE_RESULT))
	return (
		patch("optimus.settings.get_config", return_value=cfg),
		patch("optimus.ai_fix.is_available", return_value=available),
		patch("optimus.ai_fix.suggest_fix", side_effect=suggest),
	)


class TestGating:
	def test_no_op_when_ai_disabled(self):
		findings = [_finding("N+1 Query", "High", 500)]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(ai_enabled=False))
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert "llm_fix_json" not in findings[0]
		assert ctx.warnings == []

	def test_no_op_when_auto_suggest_off(self):
		findings = [_finding("N+1 Query", "High", 500)]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest=False))
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert "llm_fix_json" not in findings[0]

	def test_no_op_when_findings_section_toggle_off(self):
		# v0.6.x per-section hard off: ai_suggest_findings=False short-circuits
		# even with ai_enabled + ai_auto_suggest both on.
		findings = [_finding("N+1 Query", "High", 500)]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(ai_suggest_findings=False))
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert "llm_fix_json" not in findings[0]
		assert ctx.warnings == []

	def test_warns_when_enabled_but_provider_unavailable(self):
		findings = [_finding("N+1 Query", "High", 500)]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(), available=False)
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert "llm_fix_json" not in findings[0]
		assert ctx.warnings and "isn't fully configured" in ctx.warnings[0]

	def test_no_op_with_no_findings(self):
		ctx = _ctx([])
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert ctx.warnings == []


class TestSelectionAndPersistence:
	def test_populates_llm_fix_json_for_eligible_findings(self):
		findings = [_finding("N+1 Query", "High", 500), _finding("Slow Query", "Medium", 250)]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		for f in findings:
			stored = json.loads(f["llm_fix_json"])
			assert stored["suggestion"] == "**Fix**\n\ndo X"
			assert stored["model"] == "m"

	def test_skips_ineligible_finding_types(self):
		findings = [
			_finding("N+1 Query", "High", 500),
			_finding("Memory Pressure", "High", 999),       # not in AI_ELIGIBLE_FINDING_TYPES
			_finding("Background Queue Backlog", "High", 1),  # ditto
		]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert "llm_fix_json" in findings[0]
		assert "llm_fix_json" not in findings[1]
		assert "llm_fix_json" not in findings[2]

	def test_cap_limits_to_top_by_severity_then_impact(self):
		# 3 eligible findings; cap = 1 → only the High one gets a suggestion.
		findings = [
			_finding("Slow Query", "Low", 9999, title="low-but-huge"),
			_finding("N+1 Query", "High", 100, title="the-high-one"),
			_finding("Missing Index", "Medium", 5000, title="med"),
		]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest_max=1))
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		got = [f for f in findings if "llm_fix_json" in f]
		assert len(got) == 1
		assert got[0]["title"] == "the-high-one"

	def test_cap_zero_means_all_eligible(self):
		findings = [_finding("N+1 Query", "High", 500), _finding("Slow Query", "Medium", 200),
		            _finding("Missing Index", "Low", 50)]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest_max=0))
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		assert all("llm_fix_json" in f for f in findings)

	def test_per_finding_error_is_isolated_and_warned(self):
		# First eligible finding errors; the second still gets a suggestion.
		calls = {"n": 0}

		def _suggest(payload):
			calls["n"] += 1
			if calls["n"] == 1:
				raise RuntimeError("provider blew up")
			return dict(_FAKE_RESULT)

		findings = [
			_finding("N+1 Query", "High", 500, title="errors"),
			_finding("Slow Query", "Medium", 200, title="ok"),
		]
		ctx = _ctx(findings)
		p1, p2, p3 = _patches(_cfg(), suggest=_suggest)
		with p1, p2, p3:
			analyze._enrich_findings_with_ai_suggestions(ctx)
		# The one that errored has no suggestion; the other does.
		by_title = {f["title"]: f for f in findings}
		assert "llm_fix_json" not in by_title["errors"]
		assert "llm_fix_json" in by_title["ok"]
		assert ctx.warnings and "couldn't get a suggestion" in ctx.warnings[0]


# --------------------------------------------------------------------------
# _backfill_ai_suggestions — the regenerate-time path for existing sessions
# --------------------------------------------------------------------------

class _Row(SimpleNamespace):
	"""A stand-in for a Optimus Finding child row."""


def _row(name, finding_type, severity, impact, *, llm_fix_json=None, title="t"):
	# No callsite → _finding_to_dict won't touch the (pollution-flaky)
	# source-read path; we only care about selection + persistence here.
	return _Row(
		name=name,
		finding_type=finding_type,
		severity=severity,
		title=title,
		customer_description="d",
		estimated_impact_ms=impact,
		affected_count=1,
		action_ref="0",
		technical_detail_json=json.dumps({"normalized_query": "SELECT 1"}),
		llm_fix_json=llm_fix_json,
	)


class _FakeDB:
	def __init__(self):
		self.writes = []

	def set_value(self, dt, name, field, val):
		self.writes.append((dt, name, field, val))

	def commit(self):
		pass


def _fake_frappe():
	"""A stand-in for analyze.py's module-global ``frappe`` — just enough
	for _backfill_ai_suggestions (``frappe.db.set_value`` / ``.commit`` and
	``frappe.log_error``). Patching ``analyze.frappe`` directly sidesteps
	the suite's ``sys.modules['frappe']`` reload pollution."""
	return SimpleNamespace(db=_FakeDB(), log_error=lambda *a, **k: None)


class TestBackfillAiSuggestions:
	def test_no_op_when_setting_off(self):
		doc = SimpleNamespace(findings=[_row("F1", "N+1 Query", "High", 500)])
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest=False))
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			changed = analyze._backfill_ai_suggestions(doc)
		assert changed is False
		assert fk.db.writes == []

	def test_no_op_when_provider_unavailable(self):
		doc = SimpleNamespace(findings=[_row("F1", "N+1 Query", "High", 500)])
		p1, p2, p3 = _patches(_cfg(), available=False)
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			changed = analyze._backfill_ai_suggestions(doc)
		assert changed is False
		assert fk.db.writes == []

	def test_backfills_eligible_rows_without_a_suggestion(self):
		rows = [
			_row("F1", "N+1 Query", "High", 500),
			_row("F2", "Slow Query", "Medium", 200, llm_fix_json='{"suggestion":"already there"}'),
			_row("F3", "Memory Pressure", "High", 999),  # ineligible type
		]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			changed = analyze._backfill_ai_suggestions(doc)
		assert changed is True
		# Only F1 gets a new suggestion: F2 already has one, F3 is ineligible.
		assert [w[1] for w in fk.db.writes] == ["F1"]
		assert fk.db.writes[0][0] == "Optimus Finding" and fk.db.writes[0][2] == "llm_fix_json"
		# And the in-memory row is updated too.
		assert json.loads(rows[0].llm_fix_json)["suggestion"] == "**Fix**\n\ndo X"
		assert rows[1].llm_fix_json == '{"suggestion":"already there"}'
		assert rows[2].llm_fix_json is None

	def test_returns_false_when_nothing_to_do(self):
		# All eligible rows already have suggestions.
		rows = [_row("F1", "N+1 Query", "High", 500, llm_fix_json='{"suggestion":"x"}')]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			changed = analyze._backfill_ai_suggestions(doc)
		assert changed is False
		assert fk.db.writes == []

	def test_cap_applies_highest_severity_first(self):
		rows = [
			_row("low", "Slow Query", "Low", 9999),
			_row("high", "N+1 Query", "High", 100),
			_row("med", "Missing Index", "Medium", 5000),
		]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest_max=1))
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			changed = analyze._backfill_ai_suggestions(doc)
		assert changed is True
		assert [w[1] for w in fk.db.writes] == ["high"]


class TestRunAiBackfillCore:
	"""_run_ai_backfill is the ungated core behind both the auto-suggest
	backfill and the explicit "Generate AI fixes" button."""

	def test_returns_counts_and_fills_pending(self):
		rows = [
			_row("F1", "N+1 Query", "High", 500),
			_row("F2", "Slow Query", "Medium", 200, llm_fix_json='{"suggestion":"already"}'),
			_row("F3", "Missing Index", "Low", 50),
			_row("F4", "Memory Pressure", "High", 9),  # ineligible type
		]
		doc = SimpleNamespace(findings=rows)
		# No ai_auto_suggest gate here — _run_ai_backfill only needs
		# is_available(). cap=0 → no cap.
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest=False))
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0)
		assert out == {"added": 2, "failed": 0, "skipped_time": 0, "total_pending": 2}
		assert sorted(w[1] for w in fk.db.writes) == ["F1", "F3"]
		assert json.loads(rows[0].llm_fix_json)["suggestion"] == "**Fix**\n\ndo X"
		# Already-suggested / ineligible rows untouched.
		assert rows[1].llm_fix_json == '{"suggestion":"already"}'
		assert rows[3].llm_fix_json is None

	def test_zero_added_when_provider_unavailable(self):
		rows = [_row("F1", "N+1 Query", "High", 500)]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg(), available=False)
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0)
		assert out == {"added": 0, "failed": 0, "skipped_time": 0, "total_pending": 0}
		assert fk.db.writes == []

	def test_counts_per_finding_failures(self):
		calls = {"n": 0}

		def _suggest(payload):
			calls["n"] += 1
			if calls["n"] == 1:
				raise RuntimeError("provider blew up")
			return dict(_FAKE_RESULT)

		rows = [_row("a", "N+1 Query", "High", 500), _row("b", "Slow Query", "Medium", 200)]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg(), suggest=_suggest)
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0)
		assert out["added"] == 1 and out["failed"] == 1 and out["total_pending"] == 2
		assert [w[1] for w in fk.db.writes] == ["b"]  # only the one that succeeded

	def test_cap_from_config_when_cap_is_none(self):
		rows = [_row(f"F{i}", "N+1 Query", "High", 500 - i) for i in range(5)]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest_max=2))
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc)  # cap=None → use config (2)
		assert out["added"] == 2 and out["total_pending"] == 5
		assert len(fk.db.writes) == 2

	def test_no_pending_returns_zeros(self):
		rows = [_row("F1", "N+1 Query", "High", 500, llm_fix_json='{"suggestion":"x"}')]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0)
		assert out == {"added": 0, "failed": 0, "skipped_time": 0, "total_pending": 0}
		assert fk.db.writes == []

	def test_regenerate_all_overwrites_existing(self):
		# Two eligible findings — one already has a (stale) suggestion. With
		# regenerate_all=True BOTH get re-generated and overwritten.
		rows = [
			_row("F1", "N+1 Query", "High", 500, llm_fix_json='{"suggestion":"STALE"}'),
			_row("F2", "Slow Query", "Medium", 200),
			_row("F3", "Memory Pressure", "High", 9),  # ineligible — still skipped
		]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg(ai_auto_suggest=False))
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0, regenerate_all=True)
		assert out["added"] == 2 and out["total_pending"] == 2 and out["failed"] == 0
		assert sorted(w[1] for w in fk.db.writes) == ["F1", "F2"]
		# The stale one was overwritten with the fresh suggestion.
		assert json.loads(rows[0].llm_fix_json)["suggestion"] == "**Fix**\n\ndo X"
		assert rows[2].llm_fix_json is None  # ineligible type untouched

	def test_default_mode_does_not_overwrite_existing(self):
		rows = [_row("F1", "N+1 Query", "High", 500, llm_fix_json='{"suggestion":"KEEP"}')]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg())
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0)  # regenerate_all defaults to False
		assert out["total_pending"] == 0 and out["added"] == 0
		assert fk.db.writes == []
		assert rows[0].llm_fix_json == '{"suggestion":"KEEP"}'

	def test_regenerate_all_keeps_old_suggestion_when_one_fails(self):
		# F1 errors during re-eval → its old suggestion must survive; F2 ok.
		calls = {"n": 0}

		def _suggest(payload):
			calls["n"] += 1
			if calls["n"] == 1:
				raise RuntimeError("provider blew up")
			return dict(_FAKE_RESULT)

		rows = [
			_row("F1", "N+1 Query", "High", 500, llm_fix_json='{"suggestion":"OLD"}'),
			_row("F2", "Slow Query", "Medium", 200, llm_fix_json='{"suggestion":"OLD"}'),
		]
		doc = SimpleNamespace(findings=rows)
		p1, p2, p3 = _patches(_cfg(), suggest=_suggest)
		with p1, p2, p3, patch.object(analyze, "frappe", _fake_frappe()) as fk:
			out = analyze._run_ai_backfill(doc, cap=0, regenerate_all=True)
		assert out["added"] == 1 and out["failed"] == 1 and out["total_pending"] == 2
		# F1 failed → kept "OLD"; F2 succeeded → overwritten + persisted.
		assert json.loads(rows[0].llm_fix_json)["suggestion"] == "OLD"
		assert json.loads(rows[1].llm_fix_json)["suggestion"] == "**Fix**\n\ndo X"
		assert [w[1] for w in fk.db.writes] == ["F2"]
