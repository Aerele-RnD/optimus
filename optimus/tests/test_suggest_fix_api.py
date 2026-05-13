# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Surface tests for the v0.6.0 AI-fix API endpoints.

``suggest_fix`` / ``test_ai_connection`` need a live bench for true
integration tests, which the test harness doesn't provide — so, matching
the pattern of ``test_regenerate_reports_api.py``, we source-inspect the
endpoint bodies to pin the contract: whitelisted, gated, AI-enabled
guard, eligible-type guard, cache short-circuit, and persistence shape.
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


class TestSuggestFixSurface:
	def test_whitelisted(self):
		src = _read_api_source()
		assert re.search(r"@frappe\.whitelist\(\)\s*\ndef suggest_fix", src)

	def test_signature(self):
		src = _read_api_source()
		assert "def suggest_fix(session_uuid:" in src
		assert "finding_ref" in src.split("def suggest_fix(", 1)[1].split(")", 1)[0]
		assert "regenerate" in src.split("def suggest_fix(", 1)[1].split(")", 1)[0]

	def test_permission_gate(self):
		body = _fn_body("suggest_fix")
		assert "_require_profiler_user()" in body
		# Mirrors download_pdf: owner / System Manager / Administrator.
		assert 'row["user"] != user' in body
		assert '"System Manager" not in roles' in body
		assert 'user != "Administrator"' in body
		assert "frappe.PermissionError" in body

	def test_requires_ready_session(self):
		body = _fn_body("suggest_fix")
		assert 'row["status"] != "Ready"' in body

	def test_ai_enabled_guard(self):
		body = _fn_body("suggest_fix")
		assert "ai_fix.is_available()" in body
		# A clear, actionable message pointing at Optimus Settings.
		assert "Optimus Settings" in body and "AI Fix Suggestions" in body

	def test_eligible_finding_type_guard(self):
		body = _fn_body("suggest_fix")
		assert "AI_ELIGIBLE_FINDING_TYPES" in body

	def test_caches_unless_regenerate(self):
		body = _fn_body("suggest_fix")
		# Returns the cached suggestion when present and not regenerating.
		assert "not regenerate" in body
		assert "llm_fix_json" in body
		assert '"cached": True' in body or "'cached': True" in body

	def test_persists_via_db_set_value(self):
		body = _fn_body("suggest_fix")
		assert 'frappe.db.set_value(' in body
		assert '"Optimus Finding"' in body
		assert '"llm_fix_json"' in body
		# v0.6.x: explicit commits route through ``safe_commit`` (rollback
		# guard from the audit-response round).
		assert ("frappe.db.commit()" in body) or ("safe_commit()" in body)

	def test_builds_context_via_shared_payload_helper(self):
		body = _fn_body("suggest_fix")
		# The LLM context (finding dict + source window) is built by the same
		# helper the analyze-time auto-suggest path uses, so window size and
		# the "no source available" handling stay in one place.
		assert "_analyze_mod._ai_payload_for_finding(" in body
		assert "ai_fix.suggest_fix(" in body

	def test_converts_ai_fix_error_to_throw(self):
		body = _fn_body("suggest_fix")
		assert "ai_fix.AiFixError" in body
		assert "frappe.throw(str(e))" in body


class TestTestAiConnectionSurface:
	def test_whitelisted(self):
		src = _read_api_source()
		assert re.search(r"@frappe\.whitelist\(\)\s*\ndef test_ai_connection", src)

	def test_system_manager_only(self):
		body = _fn_body("test_ai_connection")
		assert "_require_profiler_user()" in body
		assert '"System Manager" not in roles' in body
		assert "frappe.PermissionError" in body
		assert "ai_fix.test_connection()" in body
