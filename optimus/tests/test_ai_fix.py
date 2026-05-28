# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for optimus.ai_fix — the provider-agnostic LLM client
behind the on-demand "Suggest a fix (AI)" action.

Pure-test path: ``_build_messages`` is a pure function; the HTTP layer is
exercised with ``requests.post`` monkeypatched, so no network and no live
Frappe site. ``_resolve_provider`` / ``is_available`` are tested with
``settings.get_config`` patched.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from optimus import ai_fix

# --------------------------------------------------------------------------
# Fake HTTP response + the canned bodies the two protocols return.
# --------------------------------------------------------------------------

class _FakeResp:
	def __init__(self, status_code=200, payload=None, raise_on_json=False, text=""):
		self.status_code = status_code
		self._payload = payload if payload is not None else {}
		self._raise_on_json = raise_on_json
		self.text = text

	def json(self):  # noqa: F811 — mimics requests.Response.json()
		if self._raise_on_json:
			raise ValueError("not json")
		return self._payload


_OPENAI_OK = {"choices": [{"message": {"content": "**Fix**\n\nuse a join"}}]}
_ANTHROPIC_OK = {"content": [{"type": "text", "text": "**Fix**\n\nadd an index"}]}


def _post_returning(resp):
	def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002, F811
		_fake_post.last = SimpleNamespace(url=url, headers=headers, body=json, timeout=timeout)
		return resp
	_fake_post.last = None
	return _fake_post


def _post_raising(exc):
	def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002, F811
		raise exc
	return _fake_post


# --------------------------------------------------------------------------
# _build_messages — pure
# --------------------------------------------------------------------------

class TestBuildMessages:
	def _finding(self):
		return {
			"finding_type": "N+1 Query",
			"severity": "High",
			"title": "Same query ran 50× at foo.py:42",
			"customer_description": "A query repeats inside a loop.",
			"estimated_impact_ms": 420.0,
			"affected_count": 50,
			"technical_detail": {
				"callsite": {"filename": "apps/myapp/myapp/foo.py", "lineno": 42, "function": "bulk"},
				"normalized_query": "SELECT * FROM `tabUser` WHERE name = ?",
				"fix_hint": "Batch the lookups.",
				"explain_row": "type=ALL rows=10000",
			},
			"source_window": [
				{"lineno": 40, "content": "for u in users:", "is_target": False},
				{"lineno": 42, "content": "    frappe.db.get_value('User', u)", "is_target": True},
			],
		}

	def test_returns_system_and_single_user_message(self):
		system, messages = ai_fix._build_messages(self._finding())
		assert isinstance(system, str)
		low = system.lower()
		assert "frappe" in low and "root cause" in low
		assert len(messages) == 1
		assert messages[0]["role"] == "user"

	def test_system_prompt_is_frappe_idiomatic_and_structured(self):
		system, _ = ai_fix._build_messages(self._finding())
		low = system.lower()
		# Teaches the model the real Frappe APIs / the Customize-Form way to index.
		assert "frappe.get_all" in system and "frappe.qb" in low
		assert "search index" in low and "customize form" in low
		# Anti-hallucination instruction.
		assert "do not invent" in low or "directional recommendation" in low
		# The four output sections, including the new "Verify".
		for h in ("**Diagnosis**", "**Fix**", "**Why it works**", "**Verify**"):
			assert h in system

	def test_system_prompt_forbids_raw_sql_in_fix(self):
		"""Defense-in-depth for the raw-SQL guardrail: the system prompt
		must carry an explicit, emphatic NO RAW SQL rule (not just the
		soft "never hand-built SQL strings" mention buried in the WRITE
		IDIOMATIC FRAPPE paragraph). The post-processing detector in
		``_flag_raw_sql_in_fix`` backstops the prompt — but cutting raw
		SQL at the prompt layer is cheaper (the model never generates
		it) and gives the reader a cleaner suggestion (no appended
		profiler note to wade through).
		"""
		system, _ = ai_fix._build_messages(self._finding())
		# Loud header so the rule is unmissable in the prompt.
		assert "NO RAW SQL IN YOUR PROPOSED FIX" in system
		# Lists every SQL verb the post-processing detector catches —
		# prompt + post-processing scope must match.
		low = system.lower()
		for verb in ("select", "insert", "update", "delete", "replace"):
			assert f"\"{verb} ...\"" in low or f'"{verb} ...' in low or verb in low
		# Names the legitimate exception so the model knows when raw
		# SQL is still acceptable (DDL for indexes when Customize Form
		# isn't an option).
		assert "alter table" in low or "create index" in low

	def test_system_prompt_forbids_inventing_code(self):
		system, _ = ai_fix._build_messages(self._finding())
		low = system.lower()
		# The hard anti-hallucination rules: don't invent code; "before"
		# snippets must be verbatim; no diff when the offending code wasn't shown.
		assert "do not invent code" in low
		assert "verbatim" in low
		assert "no before/after snippet" in low or "no fabricated code block" in low

	def test_system_prompt_enforces_sql_semantic_equivalence(self):
		"""v0.6.x: prompt forbids inventing WHERE / JOIN / LIMIT when
		substituting raw SQL with frappe.get_all etc. Mitigates the
		leading hallucination mode (model copies a variable from
		elsewhere in the function into a bogus `filters=` clause)."""
		system, _ = ai_fix._build_messages(self._finding())
		low = system.lower()
		# The new rule names the exact hallucination shape we want
		# to prevent: list-multiplication into an "in" filter.
		assert "sql substitution discipline" in low or "semantically equivalent" in low
		assert "[some_var] * n" in low or "[some_var] * n`" in low
		assert "frappe.session.user" in system, (
			"prompt must call out the specific variable-copy hallucination "
			"(frappe.session.user) so the model recognises the failure mode"
		)
		# Inverse rule: no WHERE in the SQL → no filters= in the replacement.
		assert "no `filters=`" in low or "no filters=" in low

	def test_system_prompt_includes_counter_example_with_no_filters(self):
		"""A LIMIT-only SQL (no WHERE) maps to frappe.get_all(..., limit=50)
		with NO filters. Counter-example shipped to keep the model from
		assuming every get_all replacement needs filters."""
		system, _ = ai_fix._build_messages(self._finding())
		# The counter-example is recognisable by its content — a raw SQL with
		# no WHERE clause + a frappe.get_all replacement without filters=.
		assert "SELECT name, email FROM `tabUser` LIMIT 50" in system
		assert "frappe.get_all('User', fields=['name', 'email'], limit=50)" in system
		# Extract the diff inside the SECOND EXAMPLE and assert filters= is
		# absent there (the surrounding prose legitimately mentions "NO
		# `filters=`" so we only check the code fence).
		_, _, counter = system.partition("SECOND EXAMPLE")
		assert counter, "second example block missing from prompt"
		_, _, diff_block = counter.partition("```diff")
		diff, _, _ = diff_block.partition("```")
		assert diff.strip(), "diff fence missing in second example"
		assert "filters=" not in diff, (
			"counter-example DIFF must NOT contain `filters=` — that's the "
			"entire point of showing a no-filter substitution"
		)

	def test_system_prompt_warns_off_indexing_frappe_metadata_columns(self):
		system, _ = ai_fix._build_messages(self._finding())
		low = system.lower()
		assert "metadata column" in low
		# Names a couple of the columns and the reason.
		assert "modified" in system and "creation" in system
		assert "write performance" in low or "every save" in low
		# …and the Frappe framework meta tables.
		assert "meta table" in low
		assert "tabcustom field" in low or "tabdoctype" in low

	def test_user_content_includes_the_key_context(self):
		_, messages = ai_fix._build_messages(self._finding())
		c = messages[0]["content"]
		assert "N+1 Query" in c
		assert "Same query ran 50" in c
		assert "apps/myapp/myapp/foo.py:42" in c
		assert "frappe.db.get_value('User', u)" in c
		assert ">> 42:" in c  # the callsite line is marked with ">>"
		assert "SELECT * FROM `tabUser`" in c
		assert "type=ALL rows=10000" in c
		assert "Batch the lookups." in c

	def test_user_content_includes_finding_type_playbook_hint(self):
		# N+1 → the user message carries the "batch it" guidance.
		_, messages = ai_fix._build_messages(self._finding())
		assert "lift it out of the loop and batch" in messages[0]["content"]
		# Redundant Call → its own hint (mentions request-scoped caching).
		_, msgs2 = ai_fix._build_messages({"finding_type": "Redundant Call", "title": "x"})
		assert "frappe.local" in msgs2[0]["content"]

	def test_query_is_truncated_when_huge(self):
		f = self._finding()
		f["technical_detail"]["normalized_query"] = "SELECT " + "x," * 5000 + "1"
		_, messages = ai_fix._build_messages(f)
		assert "(truncated)" in messages[0]["content"]

	def test_handles_minimal_finding_without_detail(self):
		# A finding with no technical_detail / source window must not crash.
		_, messages = ai_fix._build_messages({
			"finding_type": "Hot Line", "severity": "Medium", "title": "x:7 is hot",
		})
		assert "Hot Line" in messages[0]["content"]

	def test_source_window_lead_in_demands_verbatim(self):
		# When code IS shown, the user message must spell out that any
		# "before" snippet has to be a verbatim copy of those lines.
		_, messages = ai_fix._build_messages(self._finding())
		c = messages[0]["content"].lower()
		assert "verbatim" in c
		assert "only code you have" in c

	def test_no_source_notice_when_callsite_but_no_window(self):
		# A finding that has a callsite but no readable source — the profiler
		# couldn't open the file. The user message must say so and tell the
		# model NOT to invent a before/after.
		f = {
			"finding_type": "N+1 Query",
			"title": "x",
			"technical_detail": {
				"callsite": {"filename": "/gone/foo.py", "lineno": 9, "function": "v"},
			},
		}
		_, messages = ai_fix._build_messages(f)
		c = messages[0]["content"]
		assert "NOT AVAILABLE" in c
		assert "without seeing the code" in c.lower()

	def test_hot_path_finding_names_the_hot_function(self):
		# A call-tree (Slow Hot Path) finding: surface the hot function name +
		# its share of the action's time so the model knows which function's
		# body (shown below) to focus on.
		f = {
			"finding_type": "Slow Hot Path", "severity": "High",
			"title": "In Submit Sales Invoice, 62% of the time was spent in looped_validate",
			"technical_detail": {
				"callsite": {"filename": "ugly_code/python/common.py", "lineno": 6, "function": "looped_validate"},
				"function": "looped_validate", "cumulative_ms": 679, "action_wall_time_ms": 1095,
			},
			"source_window": [{"lineno": 6, "content": "def looped_validate(doc, event):", "is_target": True}],
		}
		_, messages = ai_fix._build_messages(f)
		c = messages[0]["content"]
		assert "Hot function" in c and "looped_validate" in c
		assert "679ms" in c and "62%" in c  # 679/1095 ≈ 62%

	def test_phase2_hotline_is_surfaced(self):
		f = {
			"finding_type": "Slow Hot Path", "severity": "High", "title": "x",
			"technical_detail": {"callsite": {"filename": "ugly_code/python/common.py", "lineno": 6, "function": "looped_validate"}},
			"phase2_hotline": {"lineno": 7, "content": "    _run_validations(doc)", "total_ms": 387, "hits": 2},
		}
		_, messages = ai_fix._build_messages(f)
		c = messages[0]["content"]
		assert "hottest line is line 7" in c
		assert "_run_validations(doc)" in c
		assert "387ms" in c and "2 call" in c


# --------------------------------------------------------------------------
# _call_openai_chat / _call_anthropic — HTTP layer with requests mocked
# --------------------------------------------------------------------------

class TestOpenAiCall:
	def test_extracts_choice_content(self, monkeypatch):
		fp = _post_returning(_FakeResp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fp)
		text = ai_fix._call_openai_chat("https://x/v1", "key", "m", "sys", [{"role": "user", "content": "hi"}])
		assert text == "**Fix**\n\nuse a join"
		# URL + auth header shaped correctly.
		assert fp.last.url == "https://x/v1/chat/completions"
		assert fp.last.headers["authorization"] == "Bearer key"
		# System prompt is prepended as the first message.
		assert fp.last.body["messages"][0] == {"role": "system", "content": "sys"}

	def test_omits_auth_header_without_key(self, monkeypatch):
		fp = _post_returning(_FakeResp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fp)
		ai_fix._call_openai_chat("http://localhost:11434/v1", "", "m", "s", [{"role": "user", "content": "x"}])
		assert "authorization" not in fp.last.headers

	def test_content_as_list_of_parts(self, monkeypatch):
		payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, payload)))
		assert ai_fix._call_openai_chat("u", "k", "m", "s", [{"role": "user", "content": "x"}]) == "ab"

	def test_no_text_in_response_raises(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, {"choices": []})))
		with pytest.raises(ai_fix.AiFixError):
			ai_fix._call_openai_chat("u", "k", "m", "s", [{"role": "user", "content": "x"}])


class TestAnthropicCall:
	def test_extracts_text_block(self, monkeypatch):
		fp = _post_returning(_FakeResp(200, _ANTHROPIC_OK))
		monkeypatch.setattr(requests, "post", fp)
		text = ai_fix._call_anthropic("https://api.anthropic.com", "key", "claude", "sys", [{"role": "user", "content": "hi"}])
		assert text == "**Fix**\n\nadd an index"
		assert fp.last.url == "https://api.anthropic.com/v1/messages"
		assert fp.last.headers["x-api-key"] == "key"
		assert fp.last.headers["anthropic-version"]
		assert fp.last.body["system"] == "sys"


class TestHttpErrorMapping:
	def _call(self):
		return ai_fix._call_openai_chat("u", "k", "m", "s", [{"role": "user", "content": "x"}])

	def test_timeout(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_raising(requests.exceptions.Timeout()))
		with pytest.raises(ai_fix.AiFixError, match="didn't respond"):
			self._call()

	def test_connection_error(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_raising(requests.exceptions.ConnectionError()))
		with pytest.raises(ai_fix.AiFixError, match="reach the AI provider"):
			self._call()

	def test_auth_rejected(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(401, {})))
		with pytest.raises(ai_fix.AiFixError, match="rejected the API key"):
			self._call()

	def test_rate_limited(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(429, {})))
		with pytest.raises(ai_fix.AiFixError, match="rate-limit"):
			self._call()

	def test_404_points_at_the_base_url_with_a_v1_hint(self, monkeypatch):
		# The classic Ollama misconfig: Base URL without '/v1' → 404 on
		# /chat/completions. The error must name the URL and the '/v1' fix.
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(404, {})))
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._call_openai_chat("http://localhost:11434", "", "m", "s", [{"role": "user", "content": "x"}])
		msg = str(ei.value)
		assert "404" in msg
		assert "/chat/completions" in msg
		assert "/v1" in msg

	def test_generic_http_error_includes_body_detail(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(
			_FakeResp(500, {}, text='{"error":"model not found"}')))
		with pytest.raises(ai_fix.AiFixError, match="HTTP 500"):
			self._call()
		# And the provider's body text is surfaced (capped) so "model not
		# found" / "context too long" etc. reach the operator.
		monkeypatch.setattr(requests, "post", _post_returning(
			_FakeResp(400, {}, text="bad model name")))
		with pytest.raises(ai_fix.AiFixError, match="bad model name"):
			self._call()

	def test_non_json_body(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, raise_on_json=True)))
		with pytest.raises(ai_fix.AiFixError, match="non-JSON"):
			self._call()


# --------------------------------------------------------------------------
# _resolve_provider — defaults + overrides
# --------------------------------------------------------------------------

def _cfg(**kw):
	base = {"ai_enabled": True, "ai_provider": "Anthropic", "ai_base_url": "", "ai_model": ""}
	base.update(kw)
	return SimpleNamespace(**base)


class TestResolveProvider:
	def test_anthropic_defaults(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_provider="Anthropic")):
			p = ai_fix._resolve_provider()
		assert p["name"] == "Anthropic"
		assert p["protocol"] == "anthropic"
		assert p["base_url"] == "https://api.anthropic.com"
		assert p["model"]  # a non-empty default
		assert p["needs_key"] is True

	def test_openai_compatible_protocol_and_no_key(self):
		with patch("optimus.settings.get_config",
		           return_value=_cfg(ai_provider="OpenAI-compatible", ai_base_url="http://localhost:11434/v1", ai_model="llama3")):
			p = ai_fix._resolve_provider()
		assert p["protocol"] == "openai"
		assert p["base_url"] == "http://localhost:11434/v1"
		assert p["model"] == "llama3"
		assert p["needs_key"] is False

	def test_kimi_uses_openai_protocol_with_moonshot_default(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_provider="Kimi (Moonshot)")):
			p = ai_fix._resolve_provider()
		assert p["protocol"] == "openai"
		assert "moonshot" in p["base_url"]

	def test_overrides_win_over_defaults(self):
		with patch("optimus.settings.get_config",
		           return_value=_cfg(ai_provider="OpenAI", ai_base_url="https://router.example/v1", ai_model="my-model")):
			p = ai_fix._resolve_provider()
		assert p["base_url"] == "https://router.example/v1"
		assert p["model"] == "my-model"

	def test_unknown_provider_raises(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_provider="Bogus")):
			with pytest.raises(ai_fix.AiFixError):
				ai_fix._resolve_provider()


# --------------------------------------------------------------------------
# is_available — truth table
# --------------------------------------------------------------------------

class TestIsAvailable:
	def test_false_when_disabled(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_enabled=False)):
			assert ai_fix.is_available() is False

	def test_false_when_no_model(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_enabled=True)), \
		     patch("optimus.ai_fix._resolve_provider",
		           return_value={"name": "OpenAI", "protocol": "openai", "base_url": "u", "model": "", "needs_key": True, "api_key": "k"}):
			assert ai_fix.is_available() is False

	def test_false_when_key_needed_but_missing(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_enabled=True)), \
		     patch("optimus.ai_fix._resolve_provider",
		           return_value={"name": "OpenAI", "protocol": "openai", "base_url": "u", "model": "m", "needs_key": True, "api_key": ""}):
			assert ai_fix.is_available() is False

	def test_true_when_local_no_key_needed(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_enabled=True)), \
		     patch("optimus.ai_fix._resolve_provider",
		           return_value={"name": "OpenAI-compatible", "protocol": "openai", "base_url": "u", "model": "m", "needs_key": False, "api_key": ""}):
			assert ai_fix.is_available() is True

	def test_true_when_fully_configured(self):
		with patch("optimus.settings.get_config", return_value=_cfg(ai_enabled=True)), \
		     patch("optimus.ai_fix._resolve_provider",
		           return_value={"name": "Anthropic", "protocol": "anthropic", "base_url": "u", "model": "m", "needs_key": True, "api_key": "sk-..."}):
			assert ai_fix.is_available() is True


# --------------------------------------------------------------------------
# suggest_fix — end to end with provider + requests patched
# --------------------------------------------------------------------------

class TestSuggestFix:
	_PROVIDER = {"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
	             "model": "gpt-4.1-mini", "needs_key": True, "api_key": "sk-test"}

	def test_happy_path_returns_payload(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, _OPENAI_OK)))
		with patch("optimus.ai_fix._resolve_provider", return_value=dict(self._PROVIDER)):
			out = ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "slow", "technical_detail": {}})
		assert out["suggestion"] == "**Fix**\n\nuse a join"
		assert out["model"] == "gpt-4.1-mini"
		assert out["provider"] == "OpenAI"
		assert out["generated_at"]  # iso timestamp

	def test_anthropic_dispatch(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, _ANTHROPIC_OK)))
		prov = {"name": "Anthropic", "protocol": "anthropic", "base_url": "https://api.anthropic.com",
		        "model": "claude-sonnet-4-6", "needs_key": True, "api_key": "k"}
		with patch("optimus.ai_fix._resolve_provider", return_value=prov):
			out = ai_fix.suggest_fix({"finding_type": "Missing Index", "title": "x", "technical_detail": {}})
		assert out["suggestion"] == "**Fix**\n\nadd an index"
		assert out["provider"] == "Anthropic"

	def test_empty_response_raises(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, {"choices": [{"message": {"content": "   "}}]})))
		with patch("optimus.ai_fix._resolve_provider", return_value=dict(self._PROVIDER)):
			with pytest.raises(ai_fix.AiFixError, match="empty"):
				ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "x", "technical_detail": {}})

	def test_missing_model_raises_before_any_http(self, monkeypatch):
		called = {"n": 0}
		monkeypatch.setattr(requests, "post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
		bad = dict(self._PROVIDER, model="")
		with patch("optimus.ai_fix._resolve_provider", return_value=bad):
			with pytest.raises(ai_fix.AiFixError, match="model"):
				ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "x"})
		assert called["n"] == 0

	def test_missing_key_raises_before_any_http(self, monkeypatch):
		called = {"n": 0}
		monkeypatch.setattr(requests, "post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
		bad = dict(self._PROVIDER, api_key="")
		with patch("optimus.ai_fix._resolve_provider", return_value=bad):
			with pytest.raises(ai_fix.AiFixError, match="API key"):
				ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "x"})
		assert called["n"] == 0


class TestSourceAvailableFlag:
	_PROVIDER = {"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
	             "model": "gpt-4.1-mini", "needs_key": True, "api_key": "sk-test"}

	def _suggest(self, monkeypatch, finding):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, _OPENAI_OK)))
		with patch("optimus.ai_fix._resolve_provider", return_value=dict(self._PROVIDER)):
			return ai_fix.suggest_fix(finding)

	def test_true_when_source_window_present(self, monkeypatch):
		out = self._suggest(monkeypatch, {
			"finding_type": "N+1 Query", "title": "x", "technical_detail": {},
			"source_window": [{"lineno": 1, "content": "for x in y:", "is_target": True}],
		})
		assert out["source_available"] is True

	def test_true_when_only_sql_present(self, monkeypatch):
		out = self._suggest(monkeypatch, {
			"finding_type": "Slow Query", "title": "x",
			"technical_detail": {"normalized_query": "SELECT 1"},
		})
		assert out["source_available"] is True

	def test_false_when_only_title_and_numbers(self, monkeypatch):
		out = self._suggest(monkeypatch, {
			"finding_type": "Slow Hot Path", "title": "x", "technical_detail": {},
		})
		assert out["source_available"] is False

	def test_true_when_only_phase2_hotline(self, monkeypatch):
		# A hot-path finding whose source couldn't be read but that WAS
		# line-profiled — it has the per-line numbers, so don't show the
		# "no source" caveat.
		out = self._suggest(monkeypatch, {
			"finding_type": "Slow Hot Path", "title": "x", "technical_detail": {},
			"phase2_hotline": {"lineno": 7, "content": "_run_validations(doc)", "total_ms": 387, "hits": 2},
		})
		assert out["source_available"] is True


class TestHumanizeSteps:
	_ACTIONS = [
		{"label": "Open Sales Invoice form", "cmd": "frappe.desk.form.load.getdoctype",
		 "doctype": "Sales Invoice", "duration_ms": 120},
		{"label": "Create Sales Invoice", "cmd": "frappe.desk.form.save.savedocs",
		 "doctype": "Sales Invoice", "duration_ms": 780},
		{"label": "Submit Sales Invoice", "cmd": "frappe.desk.form.save.savedocs",
		 "doctype": "Sales Invoice", "duration_ms": 310},
	]

	def test_build_steps_messages_shape(self):
		system, messages = ai_fix._build_steps_messages(self._ACTIONS, "Save SI flow")
		low = system.lower()
		assert "steps to reproduce" in low
		assert "**summary:**" in low
		# It's primed with ERPNext domain knowledge — the standard flows and
		# how to decode the raw cmds.
		assert "erpnext" in low
		assert "sales order" in low and "delivery note" in low
		assert "savedocs" in low or "run_doc_method" in low
		assert "apply_workflow" in low
		assert len(messages) == 1 and messages[0]["role"] == "user"
		c = messages[0]["content"]
		assert "Save SI flow" in c
		assert "Create Sales Invoice" in c
		assert "cmd=frappe.desk.form.save.savedocs" in c
		assert "doctype=Sales Invoice" in c

	def test_build_steps_messages_handles_endpoint_only_action(self):
		_, messages = ai_fix._build_steps_messages(
			[{"label": "GET /api/method/foo", "method": "GET", "path": "/api/method/foo"}], None
		)
		assert "GET /api/method/foo" in messages[0]["content"]

	def test_humanize_steps_happy_path(self, monkeypatch):
		out = {"choices": [{"message": {"content": "1. Create a Sales Invoice and save it.\n2. Submit it.\n\n**Summary:** saving and submitting a Sales Invoice."}}]}
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, out)))
		prov = {"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
		        "model": "gpt-4.1-mini", "needs_key": True, "api_key": "sk-test"}
		with patch("optimus.ai_fix._resolve_provider", return_value=prov):
			text = ai_fix.humanize_steps(self._ACTIONS, session_title="x")
		assert "Create a Sales Invoice" in text and "**Summary:**" in text

	def test_humanize_steps_empty_actions_raises(self):
		with pytest.raises(ai_fix.AiFixError):
			ai_fix.humanize_steps([])

	def test_humanize_steps_empty_response_raises(self, monkeypatch):
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, {"choices": [{"message": {"content": "  "}}]})))
		prov = {"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
		        "model": "m", "needs_key": True, "api_key": "k"}
		with patch("optimus.ai_fix._resolve_provider", return_value=prov):
			with pytest.raises(ai_fix.AiFixError, match="empty"):
				ai_fix.humanize_steps(self._ACTIONS)


class TestMetadataIndexGuardrail:
	def test_flags_alter_table_on_metadata_column(self):
		out = ai_fix._flag_metadata_column_index_advice("ALTER TABLE `tabFoo` ADD INDEX (`modified`);")
		assert "Profiler note" in out
		assert "`modified`" in out.split("Profiler note", 1)[1]

	def test_flags_search_index_on_metadata_column(self):
		out = ai_fix._flag_metadata_column_index_advice("Add a Search Index on the `creation` field.")
		assert "Profiler note" in out and "creation" in out

	def test_flags_plain_index_on_phrase(self):
		out = ai_fix._flag_metadata_column_index_advice("add index on parent")
		assert "Profiler note" in out and "parent" in out

	def test_does_not_flag_business_columns(self):
		txt = "Add an index on `customer` and `posting_date`."
		assert ai_fix._flag_metadata_column_index_advice(txt) == txt

	def test_does_not_flag_negated_mention(self):
		txt = "Do NOT index `modified` — Frappe writes it on every save."
		assert ai_fix._flag_metadata_column_index_advice(txt) == txt

	def test_no_index_advice_is_unchanged(self):
		txt = "**Diagnosis** — N+1.\n**Fix** — batch with frappe.get_all."
		assert ai_fix._flag_metadata_column_index_advice(txt) == txt

	def test_suggest_fix_applies_the_guardrail(self, monkeypatch):
		bad = {"choices": [{"message": {"content": "**Fix**\n\nALTER TABLE `tabX` ADD INDEX (`docstatus`);"}}]}
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, bad)))
		prov = {"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
		        "model": "gpt-4.1-mini", "needs_key": True, "api_key": "sk-test"}
		with patch("optimus.ai_fix._resolve_provider", return_value=prov):
			out = ai_fix.suggest_fix({"finding_type": "Missing Index", "title": "x", "technical_detail": {}})
		assert "Profiler note" in out["suggestion"]
		assert "docstatus" in out["suggestion"]


# ---------------------------------------------------------------------------
# Raw `frappe.db.sql` guardrail — the model is told via system prompt to
# "never hand-built SQL strings" and use the Document API or frappe.qb
# instead. This guardrail backstops that instruction: detect raw SQL in
# the LLM's proposed fix and append an advisory profiler note. Append-
# only, never rewrites — same posture as the metadata-column guardrail.
# ---------------------------------------------------------------------------


class TestRawSqlGuardrail:
	# --- Clean inputs that must NOT trip the guardrail --------------------

	def test_clean_qb_suggestion_returns_unchanged(self):
		txt = (
			"**Fix**\n\n"
			"```python\n"
			"User = frappe.qb.DocType('User')\n"
			"rows = frappe.qb.from_(User).select(User.name).run(as_dict=True)\n"
			"```"
		)
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	def test_clean_get_all_suggestion_returns_unchanged(self):
		txt = (
			"**Fix**\n\n"
			"```python\n"
			"rows = frappe.get_all('User', fields=['name', 'email'])\n"
			"```"
		)
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	def test_empty_input_returns_unchanged(self):
		assert ai_fix._flag_raw_sql_in_fix("") == ""

	def test_text_with_no_code_blocks_returns_unchanged(self):
		# No code fences anywhere — the guardrail must not fire on prose
		# alone. (Real LLM output almost always has a code block, but a
		# diagnosis-only response with no fix block is valid.)
		txt = "**Diagnosis** — N+1 in the loop.\n**Fix** — batch the lookup."
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	# --- Inputs that SHOULD trip the guardrail ----------------------------

	def test_raw_sql_select_in_addition_line_flagged(self):
		txt = (
			"**Fix**\n\n"
			"```diff\n"
			"-rows = frappe.get_all('User', fields=['name'])\n"
			"+rows = frappe.db.sql(\"SELECT name FROM `tabUser`\", as_dict=True)\n"
			"```"
		)
		out = ai_fix._flag_raw_sql_in_fix(txt)
		assert out != txt
		assert "Profiler note" in out
		assert "`frappe.qb`" in out and "`frappe.get_all`" in out

	def test_raw_sql_update_in_addition_line_flagged(self):
		txt = (
			"```diff\n"
			"+frappe.db.sql(\"UPDATE `tabUser` SET enabled = 1 WHERE name = %s\", (n,))\n"
			"```"
		)
		out = ai_fix._flag_raw_sql_in_fix(txt)
		assert "Profiler note" in out

	def test_raw_sql_insert_in_addition_line_flagged(self):
		txt = (
			"```diff\n"
			"+frappe.db.sql(\"INSERT INTO `tabLog` (msg) VALUES (%s)\", (m,))\n"
			"```"
		)
		assert "Profiler note" in ai_fix._flag_raw_sql_in_fix(txt)

	def test_raw_sql_delete_in_addition_line_flagged(self):
		txt = (
			"```diff\n"
			"+frappe.db.sql(\"DELETE FROM `tabLog` WHERE name = %s\", (n,))\n"
			"```"
		)
		assert "Profiler note" in ai_fix._flag_raw_sql_in_fix(txt)

	def test_raw_sql_case_insensitive_verb(self):
		# Models often produce mixed-case keywords. The detector regex is
		# case-insensitive on the verb — pin that.
		txt = (
			"```diff\n"
			"+frappe.db.sql(\"select email from `tabUser` where name=%s\")\n"
			"```"
		)
		assert "Profiler note" in ai_fix._flag_raw_sql_in_fix(txt)

	def test_raw_sql_in_plain_python_code_block_flagged(self):
		# Non-diff code block — every line is "proposed code".
		txt = (
			"```python\n"
			"def replace_old_call():\n"
			"    return frappe.db.sql(\"SELECT name FROM `tabUser`\")\n"
			"```"
		)
		assert "Profiler note" in ai_fix._flag_raw_sql_in_fix(txt)

	def test_raw_sql_in_untagged_code_block_flagged(self):
		# Fence with no info string still treated as code.
		txt = (
			"```\n"
			"frappe.db.sql(\"SELECT * FROM `tabUser`\")\n"
			"```"
		)
		assert "Profiler note" in ai_fix._flag_raw_sql_in_fix(txt)

	# --- Tricky cases: must NOT trip ---------------------------------------

	def test_raw_sql_in_removal_line_NOT_flagged(self):
		# The "-" lines are the BEFORE code being replaced. Flagging them
		# would invert the guardrail (the model is rightly REMOVING the
		# bad pattern).
		txt = (
			"```diff\n"
			"-rows = frappe.db.sql(\"SELECT name FROM `tabUser`\", as_dict=True)\n"
			"+rows = frappe.get_all('User', fields=['name'])\n"
			"```"
		)
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	def test_raw_sql_in_prose_NOT_flagged(self):
		# Inline-code mention in a paragraph — the model is talking ABOUT
		# the anti-pattern, not proposing it. Guardrail must stay silent.
		txt = (
			"**Diagnosis** — the code uses `frappe.db.sql(\"SELECT ...\")` "
			"inside a loop. **Fix** — batch via `frappe.get_all`."
		)
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	def test_diff_file_header_NOT_flagged(self):
		# ``+++ filename`` is the unified-diff file header, not an
		# addition line. Even if a fake header somehow contained the
		# raw-SQL token, it must not trip.
		txt = (
			"```diff\n"
			"+++ b/path/to/file.py\n"
			"@@ -1,3 +1,3 @@\n"
			" def x():\n"
			"-    return None\n"
			"+    return frappe.get_all('User', fields=['name'])\n"
			"```"
		)
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	def test_ddl_verb_NOT_flagged(self):
		# CREATE / ALTER / DROP are intentionally outside scope —
		# legit administrative use (e.g. ADD INDEX when Customize Form
		# isn't an option).
		txt = (
			"```diff\n"
			"+frappe.db.sql(\"ALTER TABLE `tabUser` ADD INDEX (`email`)\")\n"
			"```"
		)
		assert ai_fix._flag_raw_sql_in_fix(txt) == txt

	# --- End-to-end via suggest_fix --------------------------------------

	def test_suggest_fix_appends_correction_note(self, monkeypatch):
		bad = {"choices": [{"message": {"content":
			"**Fix**\n\n"
			"```diff\n"
			"-for u in users:\n"
			"-    e = frappe.db.get_value('User', u, 'email')\n"
			"+rows = frappe.db.sql(\"SELECT name, email FROM `tabUser`\", as_dict=True)\n"
			"```"
		}}]}
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, bad)))
		prov = {"name": "OpenAI", "protocol": "openai",
		        "base_url": "https://api.openai.com/v1",
		        "model": "gpt-4.1-mini", "needs_key": True, "api_key": "sk-test"}
		with patch("optimus.ai_fix._resolve_provider", return_value=prov):
			out = ai_fix.suggest_fix({
				"finding_type": "N+1 Query", "title": "x", "technical_detail": {},
			})
		assert "Profiler note" in out["suggestion"]
		# The note carries the alternative-API hint.
		assert "frappe.get_all" in out["suggestion"]


class TestTemperature:
	def test_openai_call_sets_low_temperature(self, monkeypatch):
		fp = _post_returning(_FakeResp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fp)
		ai_fix._call_openai_chat("https://x/v1", "k", "gpt-4.1-mini", "s", [{"role": "user", "content": "x"}])
		assert fp.last.body["temperature"] == ai_fix._TEMPERATURE

	def test_openai_call_omits_temperature_for_o_series(self, monkeypatch):
		fp = _post_returning(_FakeResp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fp)
		ai_fix._call_openai_chat("https://x/v1", "k", "o3-mini", "s", [{"role": "user", "content": "x"}])
		assert "temperature" not in fp.last.body

	def test_anthropic_call_sets_low_temperature(self, monkeypatch):
		fp = _post_returning(_FakeResp(200, _ANTHROPIC_OK))
		monkeypatch.setattr(requests, "post", fp)
		ai_fix._call_anthropic("https://api.anthropic.com", "k", "claude-sonnet-4-6", "s", [{"role": "user", "content": "x"}])
		assert fp.last.body["temperature"] == ai_fix._TEMPERATURE

	def test_is_reasoning_model_truth_table(self):
		for m in ("o1", "o1-mini", "o3", "o3-mini", "o4-mini", "O3"):
			assert ai_fix._is_reasoning_model(m) is True
		for m in ("gpt-4.1-mini", "gpt-4o", "claude-sonnet-4-6", "llama3", "qwen3-coder:30b", "", "ollama3"):
			assert ai_fix._is_reasoning_model(m) is False


def test_eligible_finding_types_is_a_frozenset_of_known_types():
	assert isinstance(ai_fix.AI_ELIGIBLE_FINDING_TYPES, frozenset)
	# Spot-check: the high-context types are in, the infra ones are out.
	for t in ("N+1 Query", "Slow Query", "Missing Index", "Hot Line", "Redundant Call"):
		assert t in ai_fix.AI_ELIGIBLE_FINDING_TYPES
	for t in ("Memory Pressure", "Background Queue Backlog", "Slow Frontend Render", "Function Not Invoked"):
		assert t not in ai_fix.AI_ELIGIBLE_FINDING_TYPES


# --------------------------------------------------------------------------
# v0.6.x: is_available(section=...) — per-section LLM toggles (hard off)
# --------------------------------------------------------------------------

from optimus import settings as _settings

_PROVIDER_OK = {"model": "m", "base_url": "http://x", "needs_key": False, "api_key": ""}


def _cfg_ai_on(**overrides):
	"""OptimusConfig with the master switch + provider config valid; section
	toggles default to on. Tests override per-section flags via kwargs."""
	base = {
		"ai_enabled": True, "ai_model": "m", "ai_base_url": "http://x",
		"ai_suggest_findings": True, "ai_suggest_indexes": True, "ai_humanize_steps": True,
	}
	base.update(overrides)
	return _settings.OptimusConfig(**base)


class TestIsAvailableSection:
	def test_all_sections_on_when_config_valid(self):
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on()), \
		     patch.object(ai_fix, "_resolve_provider", return_value=_PROVIDER_OK):
			assert ai_fix.is_available() is True
			assert ai_fix.is_available(section="findings") is True
			assert ai_fix.is_available(section="indexes") is True
			assert ai_fix.is_available(section="humanize") is True

	def test_findings_section_off_blocks_only_findings(self):
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on(ai_suggest_findings=False)), \
		     patch.object(ai_fix, "_resolve_provider", return_value=_PROVIDER_OK):
			assert ai_fix.is_available() is True
			assert ai_fix.is_available(section="findings") is False
			assert ai_fix.is_available(section="indexes") is True
			assert ai_fix.is_available(section="humanize") is True

	def test_indexes_section_off_blocks_only_indexes(self):
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on(ai_suggest_indexes=False)), \
		     patch.object(ai_fix, "_resolve_provider", return_value=_PROVIDER_OK):
			assert ai_fix.is_available(section="findings") is True
			assert ai_fix.is_available(section="indexes") is False
			assert ai_fix.is_available(section="humanize") is True

	def test_humanize_section_off_blocks_only_humanize(self):
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on(ai_humanize_steps=False)), \
		     patch.object(ai_fix, "_resolve_provider", return_value=_PROVIDER_OK):
			assert ai_fix.is_available(section="findings") is True
			assert ai_fix.is_available(section="indexes") is True
			assert ai_fix.is_available(section="humanize") is False

	def test_master_switch_off_blocks_everything(self):
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on(ai_enabled=False)), \
		     patch.object(ai_fix, "_resolve_provider", return_value=_PROVIDER_OK):
			assert ai_fix.is_available() is False
			for s in ("findings", "indexes", "humanize"):
				assert ai_fix.is_available(section=s) is False

	def test_unknown_section_does_not_block(self):
		# Unknown section name → fail-soft; the master + provider checks passed.
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on()), \
		     patch.object(ai_fix, "_resolve_provider", return_value=_PROVIDER_OK):
			assert ai_fix.is_available(section="bogus") is True

	def test_provider_unconfigured_blocks_regardless_of_section(self):
		# Provider missing → is_available False even with sections on.
		from optimus.ai_fix import AiFixError
		with patch("optimus.settings.get_config", return_value=_cfg_ai_on()), \
		     patch.object(ai_fix, "_resolve_provider", side_effect=AiFixError("not configured")):
			assert ai_fix.is_available() is False
			assert ai_fix.is_available(section="findings") is False
