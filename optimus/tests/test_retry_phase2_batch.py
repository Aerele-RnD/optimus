# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.x: surface tests for ``api.retry_phase2_analyzes_batch`` — the
batched retry endpoint that replaces the per-run ``frappe.call(...)`` loop
in ``optimus_session.js``. Pattern: source-inspect (mirrors
``test_suggest_fix_api.py``)."""

import os
import re

_API_PATH = os.path.join(os.path.dirname(__file__), "..", "api.py")
_JS_PATH = os.path.join(
	os.path.dirname(__file__), "..", "optimus", "doctype",
	"optimus_session", "optimus_session.js",
)


def _read(path):
	with open(path) as f:
		return f.read()


def _fn_body(name: str) -> str:
	src = _read(_API_PATH)
	start = src.index(f"def {name}(")
	search_from = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (nxt.start() if nxt else len(src) - search_from)
	return src[start:end]


class TestRetryPhase2BatchEndpoint:
	def test_whitelisted(self):
		src = _read(_API_PATH)
		assert re.search(r"@frappe\.whitelist\(\)\s*\ndef retry_phase2_analyzes_batch", src)

	def test_gated_by_require_profiler_user(self):
		body = _fn_body("retry_phase2_analyzes_batch")
		# Permission gate must be the FIRST thing the function does (before
		# any input validation), so an unauthenticated caller gets a 403
		# instead of an argument-validation error.
		assert "_require_profiler_user()" in body
		# It must come BEFORE the input validation.
		gate_idx = body.index("_require_profiler_user()")
		validation_idx = body.find("must be a")
		assert gate_idx < validation_idx, (
			"permission gate must come before input validation"
		)

	def test_accepts_json_string_or_python_list(self):
		"""Frappe whitelisted-API marshalling stringifies list args across
		the request boundary; the endpoint must accept either shape."""
		body = _fn_body("retry_phase2_analyzes_batch")
		assert "isinstance(run_uuids, str)" in body
		assert "_json.loads(run_uuids)" in body

	def test_rejects_empty_or_non_list(self):
		body = _fn_body("retry_phase2_analyzes_batch")
		assert "not isinstance(run_uuids, (list, tuple))" in body
		assert "non-empty list" in body

	def test_isolates_per_row_failures(self):
		"""One bad run_uuid must NOT abort the rest of the batch — that's
		the whole point of moving the loop server-side."""
		body = _fn_body("retry_phase2_analyzes_batch")
		# Each iteration is wrapped in try/except.
		assert "try:" in body and "except Exception as exc:" in body
		# And the failed row is recorded with its error message.
		assert "\"error\": str(exc)" in body

	def test_returns_aggregate_tallies(self):
		body = _fn_body("retry_phase2_analyzes_batch")
		assert "tallies" in body
		# Specifically the statuses the UI checks.
		assert "Ready" in body
		assert "Failed" in body

	def test_delegates_to_retry_phase2_analyze(self):
		"""The batched endpoint should reuse the single-run logic, not
		duplicate it — otherwise drift between the two paths is inevitable."""
		body = _fn_body("retry_phase2_analyzes_batch")
		assert "retry_phase2_analyze(run_uuid)" in body


class TestRetryPhase2BatchJsCall:
	"""The form-script side of the change: a SINGLE batched frappe.call
	fires when 2+ runs are stuck. Per-run buttons stay for the
	single-run case."""

	def test_js_calls_batch_endpoint(self):
		js = _read(_JS_PATH)
		assert 'method: "optimus.api.retry_phase2_analyzes_batch"' in js

	def test_js_threshold_is_two_or_more_stuck_runs(self):
		"""The batch button must only appear when batching actually
		saves round-trips — i.e. 2+ stuck runs. One stuck run still
		uses the per-run button."""
		js = _read(_JS_PATH)
		assert "stuck_runs.length >= 2" in js
