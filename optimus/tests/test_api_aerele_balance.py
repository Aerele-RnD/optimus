# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.14.x: ``optimus.api.refresh_aerele_balance`` (whitelisted,
SysMgr-gated) and ``optimus.api.refresh_aerele_balance_silent``
(daily cron wrapper).

These are thin API-surface wrappers — the integration logic lives in
``ai_fix.refresh_aerele_balance``, which is exhaustively covered by
``test_ai_aerele_provider.py``. The tests below pin the API surface:
the permission gate, the call delegation, and the silent variant's
non-Aerele short-circuit.
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture
def api_env(monkeypatch):
	"""Install a minimal frappe stub + capture frappe.throw raises +
	stub ai_fix.refresh_aerele_balance with a recorder so we can assert
	the api.py wrapper actually calls through to the real impl."""
	import optimus.ai_fix as ai_fix_mod
	import optimus.api as api_mod

	throws = []

	class _PermErr(Exception):
		pass

	def _throw(msg, exc_type=None):
		throws.append({"msg": msg, "exc_type": exc_type})
		raise (exc_type or RuntimeError)(msg)

	import frappe
	monkeypatch.setattr(frappe, "throw", _throw, raising=False)
	monkeypatch.setattr(frappe, "PermissionError", _PermErr, raising=False)

	refresh_calls = []
	def _fake_refresh():
		refresh_calls.append(True)
		return {
			"ok": True, "balance_tokens": 4242, "as_of": "2026-05-29T12:00:00Z",
			"message": "Balance refreshed: 4,242 tokens.",
		}
	monkeypatch.setattr(ai_fix_mod, "refresh_aerele_balance", _fake_refresh, raising=False)

	monkeypatch.setattr(
		api_mod, "_require_profiler_user",
		lambda: "alice@example.com",
		raising=False,
	)
	monkeypatch.setattr(
		frappe, "get_roles",
		lambda user: ["System Manager"],
		raising=False,
	)
	return types.SimpleNamespace(
		throws=throws, refresh_calls=refresh_calls,
		api=api_mod, ai_fix=ai_fix_mod, frappe=frappe,
	)


class TestRefreshAereleBalanceEndpoint:
	def test_system_manager_can_call(self, api_env):
		result = api_env.api.refresh_aerele_balance()
		assert result == {
			"ok": True, "balance_tokens": 4242, "as_of": "2026-05-29T12:00:00Z",
			"message": "Balance refreshed: 4,242 tokens.",
		}
		assert api_env.refresh_calls == [True]
		assert api_env.throws == []

	def test_administrator_can_call(self, api_env, monkeypatch):
		monkeypatch.setattr(
			api_env.api, "_require_profiler_user",
			lambda: "Administrator", raising=False,
		)
		monkeypatch.setattr(
			api_env.frappe, "get_roles",
			lambda user: ["Optimus User"],
			raising=False,
		)
		result = api_env.api.refresh_aerele_balance()
		assert result["ok"] is True
		assert api_env.throws == []

	def test_non_sysmgr_is_refused(self, api_env, monkeypatch):
		monkeypatch.setattr(
			api_env.api, "_require_profiler_user",
			lambda: "bob@example.com", raising=False,
		)
		monkeypatch.setattr(
			api_env.frappe, "get_roles",
			lambda user: ["Optimus User"], raising=False,
		)
		with pytest.raises(Exception) as exc:
			api_env.api.refresh_aerele_balance()
		assert api_env.throws
		assert "System Manager" in api_env.throws[0]["msg"]
		assert api_env.refresh_calls == []
		assert isinstance(exc.value, api_env.frappe.PermissionError)


class TestRefreshAereleBalanceSilent:
	def _stub_config(self, monkeypatch, *, provider, enabled):
		import optimus.settings as settings_mod
		def _get_config():
			return types.SimpleNamespace(
				ai_provider=provider, ai_enabled=enabled,
			)
		monkeypatch.setattr(settings_mod, "get_config", _get_config, raising=False)

	def test_noop_when_provider_is_not_aerele(self, api_env, monkeypatch):
		"""Customers on Anthropic / OpenAI / Kimi / OpenAI-compatible
		MUST NOT get an outbound call to ``api.aerele.in`` on every
		daily cron — that'd be a wasted round-trip + a confusing
		auth-failure in the Error Log."""
		self._stub_config(monkeypatch, provider="Anthropic", enabled=True)
		api_env.api.refresh_aerele_balance_silent()
		assert api_env.refresh_calls == []

	def test_noop_when_ai_disabled(self, api_env, monkeypatch):
		"""If AI is master-off, even an Aerele-configured customer
		isn't expecting calls — skip the daily probe."""
		self._stub_config(monkeypatch, provider="Aerele", enabled=False)
		api_env.api.refresh_aerele_balance_silent()
		assert api_env.refresh_calls == []

	def test_calls_when_provider_is_aerele(self, api_env, monkeypatch):
		self._stub_config(monkeypatch, provider="Aerele", enabled=True)
		api_env.api.refresh_aerele_balance_silent()
		assert api_env.refresh_calls == [True]

	def test_swallows_exception_and_logs(self, api_env, monkeypatch):
		"""Cron MUST NOT raise — a network blip or a misconfigured
		provider on one site can't strand the scheduler. log_error
		captures the detail for the operator."""
		self._stub_config(monkeypatch, provider="Aerele", enabled=True)
		def _explode():
			raise RuntimeError("aerele unreachable")
		monkeypatch.setattr(
			api_env.ai_fix, "refresh_aerele_balance",
			_explode, raising=False,
		)
		log_errors = []
		monkeypatch.setattr(
			api_env.frappe, "log_error",
			lambda **kw: log_errors.append(kw), raising=False,
		)
		api_env.api.refresh_aerele_balance_silent()
		assert log_errors
		assert log_errors[0]["title"] == "optimus aerele balance daily sync"

	def test_settings_failure_is_swallowed(self, api_env, monkeypatch):
		"""A get_config() exception (settings cache corrupt, DocType
		not migrated yet) must also be swallowed — cron stays alive."""
		import optimus.settings as settings_mod
		def _broken():
			raise RuntimeError("settings broken")
		monkeypatch.setattr(settings_mod, "get_config", _broken, raising=False)
		monkeypatch.setattr(
			api_env.frappe, "log_error",
			lambda **kw: None, raising=False,
		)
		api_env.api.refresh_aerele_balance_silent()
		assert api_env.refresh_calls == []
