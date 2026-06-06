# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Regression guards for the post-review fixes (analyze + api need a live
bench for true integration, so the contract is pinned by source-inspection,
matching test_suggest_fix_api.py)."""

import os
import re

_HERE = os.path.dirname(__file__)
_API_PATH = os.path.join(_HERE, "..", "api.py")
_ANALYZE_PATH = os.path.join(_HERE, "..", "analyze.py")


def _read(path: str) -> str:
	with open(path, encoding="utf-8") as f:
		return f.read()


def _fn_body(src: str, name: str) -> str:
	start = src.index(f"def {name}(")
	search_from = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (nxt.start() if nxt else len(src) - search_from)
	return src[start:end]


# --- HIGH-1: steps tokens captured on the auto-analyze path -----------------


class TestAutoAnalyzeStepsTokens:
	def test_build_humanized_notes_threads_usage_out(self):
		body = _fn_body(_read(_ANALYZE_PATH), "_build_humanized_notes_html")
		assert "usage_out: dict | None = None" in body
		assert "usage_out=usage_out" in body  # forwarded to ai_fix.humanize_steps

	def test_persist_records_steps_tokens(self):
		src = _read(_ANALYZE_PATH)
		# The _persist caller passes a usage dict and writes ai_steps_tokens.
		assert "usage_out=_steps_usage" in src
		assert "doc.ai_steps_tokens = int(_steps_usage" in src


# --- HIGH-2: drain phase stays on "Capturing Background Jobs" ----------------


class TestDrainKeepsCapturingStatus:
	def test_bg_wait_does_not_flip_to_analyzing(self):
		body = _fn_body(_read(_ANALYZE_PATH), "_bg_wait_for_pending_jobs")
		assert '"status", "Capturing Background Jobs"' in body
		assert '"status", "Analyzing"' not in body  # the real Analyzing is in run()


# --- MEDIUM-6 + the stop-time status set ------------------------------------


class TestStopSetsCapturingStatus:
	def test_stop_session_sets_status_and_commits(self):
		body = _fn_body(_read(_API_PATH), "_stop_session")
		assert '"status", "Capturing Background Jobs"' in body
		assert "safe_commit()" in body


# --- drain_progress endpoint contract ---------------------------------------


class TestDrainProgressEndpoint:
	def test_permission_gated_and_window_math(self):
		src = _read(_API_PATH)
		assert re.search(r"@frappe\.whitelist\(\)\s*\ndef drain_progress", src)
		body = _fn_body(src, "drain_progress")
		assert "_require_session_permission(session_uuid)" in body
		assert "session.get_pending_jobs(session_uuid)" in body
		assert "- 60" in body  # window remaining = draining_until - now - 60-grace
		assert '"remaining_seconds"' in body
		assert '"window_seconds"' in body


# --- test_ai_connection must not bill the probe to a prior session -----------


class TestProbeClearsSpendMarker:
	def test_ai_connection_clears_marker(self):
		body = _fn_body(_read(_API_PATH), "test_ai_connection")
		assert "_mark_ai_spend_session(None)" in body


# --- export_session parity --------------------------------------------------


class TestExportSessionTokenParity:
	def test_export_includes_token_fields(self):
		body = _fn_body(_read(_API_PATH), "export_session")
		assert '"ai_tokens_spent"' in body
		assert '"ai_refresh_count"' in body
		assert '"ai_steps_tokens"' in body


# --- behavioral: corrupt bundle degrades to None ----------------------------


class TestLoadRecordingsBundleCorrupt:
	def test_corrupt_gzip_returns_none(self, monkeypatch, tmp_path):
		import frappe

		from optimus import analyze

		bad = tmp_path / "bad.json.gz"
		bad.write_bytes(b"this is definitely not gzip")

		class _FileDoc:
			def get_full_path(self):
				return str(bad)

		monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: _FileDoc(), raising=False)
		monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)

		class Doc:
			recordings_file = "/private/files/bad.json.gz"

		assert analyze._load_recordings_bundle(Doc()) is None
