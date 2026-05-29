# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.14.x: Aerele managed AI provider — pre-call balance gate, 402
mapping, post-call balance persistence from response header.

Aerele rides the OpenAI-compatible wire format (no new protocol
handler in ``ai_fix.py``); the integration's value-add is bookkeeping
the customer's pay-as-you-go token bucket:

  1. Pre-call: ``_assert_aerele_balance`` refuses fast when the cached
     balance is below ``aerele_balance_min_threshold``.
  2. On success: Aerele's proxy returns
     ``X-Aerele-Balance-Remaining: <int>`` in response headers;
     ``_maybe_persist_aerele_balance`` writes it to ``Optimus Settings``
     via ``frappe.db.set_value`` (bypassing the cached config envelope).
  3. On 402: ``_http_post`` maps to the same "Top up" message and
     persists the authoritative balance from the response body.

The bench cache is a HINT for fast-fail UX. Aerele's server is the
authoritative source — see docs/AI-FIXING.md §10.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from optimus import ai_fix

# ---------------------------------------------------------------------------
# Local fakes — _FakeResp extends the shape from test_ai_fix.py with the
# ``headers`` attribute the Aerele integration reads.
# ---------------------------------------------------------------------------

class _FakeRespWithHeaders:
	def __init__(self, status_code=200, payload=None, headers=None, text=""):
		self.status_code = status_code
		self._payload = payload if payload is not None else {}
		self.headers = headers or {}
		self.text = text

	def json(self):
		return self._payload


def _post_returning(resp):
	def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
		_fake_post.last = SimpleNamespace(url=url, headers=headers, body=json, timeout=timeout)
		return resp
	_fake_post.last = None
	return _fake_post


def _get_returning(resp):
	def _fake_get(url, headers=None, timeout=None):
		_fake_get.last = SimpleNamespace(url=url, headers=headers, timeout=timeout)
		return resp
	_fake_get.last = None
	return _fake_get


def _stub_persist(monkeypatch):
	"""Replace ``_persist_aerele_balance`` with a recorder so tests can
	assert what (if anything) the integration would write to settings,
	without touching the bench's settings cache or DB."""
	calls = []
	def _record(remaining, *, sync_at=None):
		calls.append({"remaining": int(remaining), "sync_at": sync_at})
	monkeypatch.setattr(ai_fix, "_persist_aerele_balance", _record, raising=False)
	return calls


# ---------------------------------------------------------------------------
# 1. _PROVIDER_DEFAULTS exposes Aerele
# ---------------------------------------------------------------------------

class TestAereleProviderEntry:
	def test_aerele_uses_openai_wire_protocol(self):
		"""Aerele's proxy is OpenAI-compatible. The provider routes
		through ``_call_openai_chat``; no new protocol handler needed."""
		entry = ai_fix._PROVIDER_DEFAULTS["Aerele"]
		assert entry["protocol"] == "openai"
		assert entry["needs_key"] is True
		# Base URL is hardcoded to Aerele's known endpoint — operators
		# can override via ai_base_url if Aerele migrates the domain.
		assert entry["base_url"].startswith("https://")
		assert "aerele" in entry["base_url"]

	def test_balance_header_constant_matches_doc(self):
		"""The header name is part of Aerele's public API contract;
		grep-discoverable from one place keeps prod + docs + tests in
		sync."""
		assert ai_fix._AERELE_BALANCE_HEADER == "X-Aerele-Balance-Remaining"


# ---------------------------------------------------------------------------
# 2. _assert_aerele_balance — pre-call gate
# ---------------------------------------------------------------------------

class TestAssertAereleBalance:
	def _stub_config(self, monkeypatch, *, balance, min_balance, fail=False):
		def _get_config():
			if fail:
				raise RuntimeError("settings unreadable")
			return SimpleNamespace(
				aerele_balance_tokens=balance,
				aerele_balance_min_threshold=min_balance,
			)
		import optimus.settings as settings_mod
		monkeypatch.setattr(settings_mod, "get_config", _get_config, raising=False)

	def test_noop_for_non_aerele_providers(self, monkeypatch):
		"""Anthropic / OpenAI / Kimi / OpenAI-compatible — the gate is
		Aerele-specific, MUST NOT raise for the others even when no
		settings stub is installed."""
		# No stub installed at all; if the gate touched settings we'd
		# get an AttributeError or RuntimeError.
		for name in ("Anthropic", "OpenAI", "Kimi (Moonshot)", "OpenAI-compatible"):
			ai_fix._assert_aerele_balance(name)  # must not raise

	def test_refuses_when_balance_below_threshold(self, monkeypatch):
		self._stub_config(monkeypatch, balance=50, min_balance=100)
		with pytest.raises(ai_fix.AiFixError) as exc:
			ai_fix._assert_aerele_balance("Aerele")
		msg = str(exc.value)
		assert "50" in msg
		assert "100" in msg
		assert "Top up" in msg
		assert "aerele.in" in msg

	def test_allows_at_threshold(self, monkeypatch):
		"""Equal to threshold passes — the boundary is inclusive so an
		operator who sets ``min_threshold = balance`` doesn't see
		instant refusal."""
		self._stub_config(monkeypatch, balance=100, min_balance=100)
		ai_fix._assert_aerele_balance("Aerele")  # no raise

	def test_allows_above_threshold(self, monkeypatch):
		self._stub_config(monkeypatch, balance=5000, min_balance=100)
		ai_fix._assert_aerele_balance("Aerele")  # no raise

	def test_settings_unreadable_lets_call_through(self, monkeypatch):
		"""A settings hiccup MUST NOT block the AI call — Aerele's
		server is the authoritative gate and will return 402 if the
		balance is genuinely insufficient."""
		self._stub_config(monkeypatch, balance=0, min_balance=0, fail=True)
		ai_fix._assert_aerele_balance("Aerele")  # no raise


# ---------------------------------------------------------------------------
# 3. _maybe_persist_aerele_balance — response header parsing
# ---------------------------------------------------------------------------

class TestMaybePersistAereleBalance:
	def test_persists_when_header_present(self, monkeypatch):
		calls = _stub_persist(monkeypatch)
		headers = {ai_fix._AERELE_BALANCE_HEADER: "8432"}
		ai_fix._maybe_persist_aerele_balance(headers)
		assert calls == [{"remaining": 8432, "sync_at": None}]

	def test_noop_when_header_missing(self, monkeypatch):
		"""Non-Aerele providers don't return the header — the persist
		must be a free no-op so the bookkeeping doesn't tax every AI
		call."""
		calls = _stub_persist(monkeypatch)
		ai_fix._maybe_persist_aerele_balance({"content-type": "application/json"})
		assert calls == []

	def test_noop_when_header_empty(self, monkeypatch):
		calls = _stub_persist(monkeypatch)
		ai_fix._maybe_persist_aerele_balance({})
		ai_fix._maybe_persist_aerele_balance(None)
		assert calls == []

	def test_noop_when_header_not_integer(self, monkeypatch):
		"""A malformed header from a broken proxy MUST NOT raise — log
		nothing, leave the cache untouched."""
		calls = _stub_persist(monkeypatch)
		ai_fix._maybe_persist_aerele_balance({ai_fix._AERELE_BALANCE_HEADER: "not-an-int"})
		ai_fix._maybe_persist_aerele_balance({ai_fix._AERELE_BALANCE_HEADER: ""})
		assert calls == []


# ---------------------------------------------------------------------------
# 4. _http_post — 402 mapping + happy-path header persist
# ---------------------------------------------------------------------------

class TestHttpPostAereleSemantics:
	def test_402_with_body_balance_raises_and_persists(self, monkeypatch):
		"""Aerele's authoritative refusal. The response body carries
		the current balance — ``_http_post`` persists it (cache was
		stale; now we know the truth) and raises a "Top up" error."""
		calls = _stub_persist(monkeypatch)
		resp = _FakeRespWithHeaders(
			status_code=402,
			payload={"balance_tokens": 42, "error": "Insufficient balance"},
		)
		monkeypatch.setattr(requests, "post", _post_returning(resp))
		with pytest.raises(ai_fix.AiFixError) as exc:
			ai_fix._http_post(
				"https://api.aerele.in/optimus/v1/chat/completions",
				{}, {}, provider="openai", where="chat",
			)
		msg = str(exc.value)
		assert "insufficient" in msg.lower()
		assert "42" in msg
		assert "Top up" in msg
		# Authoritative balance persisted.
		assert calls == [{"remaining": 42, "sync_at": None}]

	def test_402_without_body_still_raises(self, monkeypatch):
		"""Aerele's proxy is allowed to omit the body; the error
		message just won't have the exact balance number — but the
		actionable top-up hint is always there."""
		calls = _stub_persist(monkeypatch)
		resp = _FakeRespWithHeaders(status_code=402, payload={})
		monkeypatch.setattr(requests, "post", _post_returning(resp))
		with pytest.raises(ai_fix.AiFixError) as exc:
			ai_fix._http_post(
				"https://api.aerele.in/optimus/v1/chat/completions",
				{}, {}, provider="openai", where="chat",
			)
		assert "Top up" in str(exc.value)
		# No balance to persist when the body didn't include it.
		assert calls == []

	def test_200_with_balance_header_persists(self, monkeypatch):
		"""Every successful call refreshes the cache for free —
		Aerele's proxy includes the remaining balance in the response
		header so the bench doesn't need a separate /balance round-trip
		to stay current."""
		calls = _stub_persist(monkeypatch)
		resp = _FakeRespWithHeaders(
			status_code=200,
			payload={"choices": [{"message": {"content": "OK"}}]},
			headers={ai_fix._AERELE_BALANCE_HEADER: "12345"},
		)
		monkeypatch.setattr(requests, "post", _post_returning(resp))
		data = ai_fix._http_post(
			"https://api.aerele.in/optimus/v1/chat/completions",
			{}, {}, provider="openai", where="chat",
		)
		# Return path is unaffected — the integration is purely an
		# additive side-effect.
		assert data["choices"][0]["message"]["content"] == "OK"
		assert calls == [{"remaining": 12345, "sync_at": None}]

	def test_200_without_balance_header_is_silent(self, monkeypatch):
		"""Non-Aerele providers — the call returns successfully and
		nothing is persisted. The integration is purely additive."""
		calls = _stub_persist(monkeypatch)
		resp = _FakeRespWithHeaders(
			status_code=200,
			payload={"choices": [{"message": {"content": "OK"}}]},
		)
		monkeypatch.setattr(requests, "post", _post_returning(resp))
		ai_fix._http_post(
			"https://api.openai.com/v1/chat/completions",
			{}, {}, provider="openai", where="chat",
		)
		assert calls == []


# ---------------------------------------------------------------------------
# 5. refresh_aerele_balance — manual + cron entry point
# ---------------------------------------------------------------------------

class TestRefreshAereleBalance:
	def _stub_resolve_provider(self, monkeypatch, *, name="Aerele", api_key="k1"):
		def _resolve():
			return {
				"name": name,
				"protocol": "openai",
				"base_url": "https://api.aerele.in/optimus/v1",
				"model": "claude-sonnet-4-6",
				"api_key": api_key,
				"needs_key": True,
			}
		monkeypatch.setattr(ai_fix, "_resolve_provider", _resolve, raising=False)

	def test_happy_path_returns_balance_and_persists(self, monkeypatch):
		self._stub_resolve_provider(monkeypatch)
		calls = _stub_persist(monkeypatch)
		resp = _FakeRespWithHeaders(
			status_code=200,
			payload={"balance_tokens": 9876, "as_of": "2026-05-29T11:30:00Z"},
		)
		fake_get = _get_returning(resp)
		monkeypatch.setattr(requests, "get", fake_get)

		result = ai_fix.refresh_aerele_balance()

		assert result["ok"] is True
		assert result["balance_tokens"] == 9876
		assert result["as_of"] == "2026-05-29T11:30:00Z"
		assert "9,876" in result["message"]
		# URL + auth correctly shaped.
		assert fake_get.last.url == "https://api.aerele.in/optimus/v1/balance"
		assert fake_get.last.headers["authorization"] == "Bearer k1"
		# Persisted.
		assert calls == [{"remaining": 9876, "sync_at": None}]

	def test_refuses_when_provider_is_not_aerele(self, monkeypatch):
		self._stub_resolve_provider(monkeypatch, name="Anthropic")
		# No persist stub needed — the endpoint must short-circuit
		# before any persist call.
		result = ai_fix.refresh_aerele_balance()
		assert result["ok"] is False
		assert "not Aerele" in result["message"]
		assert result["balance_tokens"] is None

	def test_refuses_when_api_key_missing(self, monkeypatch):
		self._stub_resolve_provider(monkeypatch, api_key="")
		result = ai_fix.refresh_aerele_balance()
		assert result["ok"] is False
		assert "API key" in result["message"]

	def test_auth_failure_returns_ok_false(self, monkeypatch):
		self._stub_resolve_provider(monkeypatch)
		resp = _FakeRespWithHeaders(status_code=401, payload={})
		monkeypatch.setattr(requests, "get", _get_returning(resp))
		# log_error must not break the test (no Frappe bench).
		import optimus.ai_fix as ai_fix_mod
		monkeypatch.setattr(ai_fix_mod, "_log_http_error", lambda *a, **k: None, raising=False)
		result = ai_fix.refresh_aerele_balance()
		assert result["ok"] is False
		assert "rejected" in result["message"]

	def test_network_error_returns_ok_false(self, monkeypatch):
		self._stub_resolve_provider(monkeypatch)
		def _post_raising(*a, **k):
			raise requests.exceptions.ConnectionError("boom")
		monkeypatch.setattr(requests, "get", _post_raising)
		monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **k: None, raising=False)
		result = ai_fix.refresh_aerele_balance()
		assert result["ok"] is False
		assert "Couldn't reach" in result["message"]

	def test_non_json_body_returns_ok_false(self, monkeypatch):
		self._stub_resolve_provider(monkeypatch)
		class _BadJsonResp(_FakeRespWithHeaders):
			def json(self):
				raise ValueError("not json")
		resp = _BadJsonResp(status_code=200)
		monkeypatch.setattr(requests, "get", _get_returning(resp))
		monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **k: None, raising=False)
		result = ai_fix.refresh_aerele_balance()
		assert result["ok"] is False
		assert "non-JSON" in result["message"]

	def test_missing_balance_key_returns_ok_false(self, monkeypatch):
		"""Aerele's contract is ``{"balance_tokens": int, "as_of":
		str}``. A response without ``balance_tokens`` is a protocol
		bug; refuse rather than guess."""
		self._stub_resolve_provider(monkeypatch)
		resp = _FakeRespWithHeaders(status_code=200, payload={"as_of": "x"})
		monkeypatch.setattr(requests, "get", _get_returning(resp))
		result = ai_fix.refresh_aerele_balance()
		assert result["ok"] is False
		assert "balance_tokens" in result["message"]
