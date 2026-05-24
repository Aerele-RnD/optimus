# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure-function tests for ``optimus/redaction.py``.

These cover the relocated render-time helpers (now also called from
the recorder patch at capture time). The tests don't depend on Frappe;
they exercise the regex + substring patterns directly so the same code
is provably equivalent in both call paths.
"""

from optimus import redaction

# ---------------------------------------------------------------------------
# is_sensitive_key + redact_sensitive
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
	def test_default_patterns_match_case_insensitively(self):
		for key in (
			"password", "Password", "PWD", "api_key", "apiKey", "TOKEN",
			"secret_key", "CSRF", "Authorization", "Cookie",
			"encryption_key", "PRIVATE_KEY", "session_id",
		):
			assert redaction.is_sensitive_key(key), f"{key!r} should be flagged sensitive"

	def test_non_string_input_is_not_sensitive(self):
		for key in (None, 0, [], {}, b"password"):
			assert redaction.is_sensitive_key(key) is False

	def test_extra_keys_extend_defaults(self):
		assert redaction.is_sensitive_key("recovery_code", extra=("recovery_code",))
		# Default still matches even when extras supplied.
		assert redaction.is_sensitive_key("password", extra=("recovery_code",))

	def test_benign_keys_pass_through(self):
		# Note: substring match means "csrf_disabled_flag" WOULD match
		# ("csrf" substring); the only safe benign keys are those whose
		# names share NO substring with any sensitive pattern.
		for key in ("name", "email", "title", "user", "enabled", "created_at"):
			assert redaction.is_sensitive_key(key) is False, f"{key!r} unexpectedly flagged"


class TestRedactSensitive:
	def test_flat_dict_redacts_default_keys(self):
		out = redaction.redact_sensitive({"password": "hunter2", "name": "Alice"})
		assert out == {"password": "<REDACTED:password>", "name": "Alice"}

	def test_nested_dict_and_list_are_walked(self):
		payload = {
			"form": {"username": "alice", "password": "hunter2"},
			"headers": [{"Authorization": "Bearer xyz"}, {"X-Custom": "ok"}],
		}
		out = redaction.redact_sensitive(payload)
		assert out["form"]["password"] == "<REDACTED:password>"
		assert out["form"]["username"] == "alice"
		assert out["headers"][0]["Authorization"] == "<REDACTED:Authorization>"
		assert out["headers"][1]["X-Custom"] == "ok"

	def test_extra_keys_extend_redaction(self):
		out = redaction.redact_sensitive(
			{"recovery_code": "abc123", "name": "Alice"},
			extra_keys=("recovery_code",),
		)
		assert out["recovery_code"] == "<REDACTED:recovery_code>"
		assert out["name"] == "Alice"

	def test_extra_keys_are_additive_not_replacement(self):
		"""A customer config that adds ``recovery_code`` must not
		accidentally stop redacting ``password``."""
		out = redaction.redact_sensitive(
			{"password": "x", "recovery_code": "y"},
			extra_keys=("recovery_code",),
		)
		assert out["password"] == "<REDACTED:password>"
		assert out["recovery_code"] == "<REDACTED:recovery_code>"

	def test_scalar_passes_through(self):
		assert redaction.redact_sensitive("hello") == "hello"
		assert redaction.redact_sensitive(42) == 42
		assert redaction.redact_sensitive(None) is None

	def test_input_is_not_mutated(self):
		original = {"password": "x", "name": "Alice"}
		out = redaction.redact_sensitive(original)
		assert original == {"password": "x", "name": "Alice"}, "input must be unchanged"
		assert out is not original


# ---------------------------------------------------------------------------
# redact_sql_literals + redact_call_queries
# ---------------------------------------------------------------------------


class TestRedactSqlLiterals:
	def test_default_columns_redact_equality_literal(self):
		out = redaction.redact_sql_literals(
			"SELECT * FROM `tabUser` WHERE password = 'hunter2'"
		)
		assert "hunter2" not in out
		assert "'<REDACTED>'" in out

	def test_redacts_like_and_in(self):
		assert "'<REDACTED>'" in redaction.redact_sql_literals(
			"SELECT 1 WHERE api_key LIKE 'sk-%'"
		)
		assert "'<REDACTED>'" in redaction.redact_sql_literals(
			"SELECT 1 WHERE token IN ('a', 'b', 'c')"
		)

	def test_redacts_double_quoted_literal(self):
		out = redaction.redact_sql_literals('UPDATE x SET secret = "shh" WHERE id = 1')
		assert "shh" not in out

	def test_non_sensitive_query_passes_through(self):
		query = "SELECT name, email FROM `tabUser` WHERE enabled = 1 ORDER BY name"
		assert redaction.redact_sql_literals(query) == query

	def test_empty_or_invalid_input(self):
		assert redaction.redact_sql_literals("") == ""
		assert redaction.redact_sql_literals(None) == ""
		# Non-string passes through too (we type-check defensively).
		assert redaction.redact_sql_literals(42) == 42 or redaction.redact_sql_literals(42) == ""

	def test_extra_columns_extend_redaction(self):
		query = "SELECT * FROM acc WHERE bank_account = '1234567890'"
		# Default: bank_account NOT in defaults → no redaction.
		assert "1234567890" in redaction.redact_sql_literals(query)
		# With extra: redacted.
		out = redaction.redact_sql_literals(query, extra_columns=("bank_account",))
		assert "1234567890" not in out
		assert "'<REDACTED>'" in out

	def test_extra_columns_are_additive(self):
		"""Custom config can't accidentally turn off the default password redaction."""
		query = "SELECT * FROM u WHERE password='x' AND bank_account='y'"
		out = redaction.redact_sql_literals(query, extra_columns=("bank_account",))
		assert "'x'" not in out
		assert "'y'" not in out


class TestRedactCallQueries:
	def test_mutates_calls_list_in_place(self):
		calls = [
			{"query": "SELECT 1 WHERE password='x'", "duration": 1.0},
			{"normalized_query": "WHERE token=?", "duration": 2.0},
		]
		redaction.redact_call_queries(calls)
		assert "'x'" not in calls[0]["query"]
		# Non-sensitive normalized_query (with placeholder, not literal) passes through.
		assert calls[1]["normalized_query"] == "WHERE token=?"

	def test_ignores_non_dict_entries(self):
		# Must not crash on malformed calls (e.g. mid-deploy garbage in Redis).
		redaction.redact_call_queries([None, "not a dict", {"query": "ok"}])

	def test_ignores_non_list_input(self):
		# Must not crash on dict-as-calls (defensive).
		redaction.redact_call_queries({"not": "a list"})
		redaction.redact_call_queries(None)
