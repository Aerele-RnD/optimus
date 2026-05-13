# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Surface tests for the v0.6.0 ``backfill_ai_fixes`` API endpoint — the
manual "retry the LLM" action that fills AI fix suggestions on every
eligible finding that doesn't have one yet, then re-renders the report.

Like ``test_regenerate_reports_api.py`` / ``test_suggest_fix_api.py`` this
is a source-inspection test (the endpoint needs a live bench for a true
integration run): we pin the contract — whitelisted, gated, AI-enabled
guard, ungated backfill core, re-render via ``regenerate_reports``.
"""

import os
import re

_API_PATH = os.path.join(os.path.dirname(__file__), "..", "api.py")


def _read_api_source() -> str:
	with open(_API_PATH) as f:
		return f.read()


def _fn_body(name: str) -> str:
	src = _read_api_source()
	start = src.index(f"def {name}(")
	search_from = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (nxt.start() if nxt else len(src) - search_from)
	return src[start:end]


def test_whitelisted_and_signature():
	src = _read_api_source()
	assert re.search(r"@frappe\.whitelist\(\)\s*\ndef backfill_ai_fixes", src)
	assert "def backfill_ai_fixes(session_uuid:" in src


def test_permission_gate_mirrors_download_pdf():
	body = _fn_body("backfill_ai_fixes")
	assert "_require_profiler_user()" in body
	assert 'row["user"] != user' in body
	assert '"System Manager" not in roles' in body
	assert 'user != "Administrator"' in body
	assert "frappe.PermissionError" in body


def test_requires_ready_session():
	body = _fn_body("backfill_ai_fixes")
	assert 'row["status"] != "Ready"' in body


def test_ai_enabled_guard():
	body = _fn_body("backfill_ai_fixes")
	assert "ai_fix.is_available()" in body
	# (the "Optimus Settings" hint is wrapped across two source lines, so
	# match on contiguous fragments)
	assert "aren't configured" in body and "AI Fix Suggestions" in body


def test_calls_ungated_backfill_core_with_no_cap():
	body = _fn_body("backfill_ai_fixes")
	# Uses the ungated core (NOT the auto-suggest-gated _backfill_ai_suggestions),
	# with cap=0 (do all targeted findings, bounded only by the time budget).
	assert "_run_ai_backfill(doc, cap=0" in body
	assert "_backfill_ai_suggestions" not in body


def test_supports_regenerate_all_passthrough():
	src = _read_api_source()
	# The `regenerate_all` flag is part of the signature, coerced, and
	# plumbed straight through to _run_ai_backfill.
	assert "def backfill_ai_fixes(session_uuid: str, regenerate_all=0)" in src
	body = _fn_body("backfill_ai_fixes")
	assert "cint(regenerate_all)" in body
	assert "regenerate_all=regenerate_all" in body


def test_re_renders_via_regenerate_reports_and_returns_counts():
	body = _fn_body("backfill_ai_fixes")
	assert "regenerate_reports(session_uuid)" in body
	for key in ('"added"', '"failed"', '"skipped_time"', '"total_pending"'):
		assert key in body
