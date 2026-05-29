# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.9.0 AI privacy-hardening additions.

Pure-pytest — no live Frappe site needed. Mirrors the mock patterns in
``test_ai_fix.py`` (FakeResp + ``_post_capturing``) and
``test_settings_round6.py`` (direct ``OptimusConfig`` instantiation +
``settings.get_config`` patching).

Invariants under test (mirror the plan's Risks + Mitigations):

  * ``ai_excluded_finding_types`` parses with the same skip-list semantics
    as the existing ``skip_request_paths``: one-per-line, ``#`` comments,
    blanks dropped, exact match.
  * ``is_finding_type_excluded`` is case-sensitive — an inert exclude
    (typo, wrong case) is safer than a partial-match exclude.
  * ``suggest_fix`` short-circuits with ``AiFixError`` before any HTTP
    call when the finding's type is on the exclusion list — the payload
    is never built and no request leaves the host.
  * ``_http_post`` reads the configured timeout via
    ``_resolve_timeout_seconds``; clamped to ``[10, 600]``.
  * ``OptimusSettings._clamp_numeric_floors`` clamps
    ``ai_request_timeout_seconds`` below 10 up to 10.
  * The doc's enumeration of eligible finding types stays byte-for-line
    aligned with ``ai_fix.AI_ELIGIBLE_FINDING_TYPES`` — drift fails CI.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, settings

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _cfg(**over):
	"""Return a SimpleNamespace shaped like OptimusConfig with our defaults
	overridden by kwargs. Used as the return value of a patched get_config."""
	defaults = dict(
		ai_excluded_finding_types=(),
		ai_request_timeout_seconds=60,
	)
	defaults.update(over)
	return SimpleNamespace(**defaults)


# --------------------------------------------------------------------------
# TestExcludeParsing — line-per-entry, # comments, blanks dropped
# --------------------------------------------------------------------------


class TestExcludeParsing:
	def test_parses_one_type_per_line(self):
		raw = "Slow Query\nFull Table Scan\nN+1 Query"
		out = settings._parse_skip_list(raw)
		assert out == ("Slow Query", "Full Table Scan", "N+1 Query")

	def test_strips_comments_and_blanks(self):
		raw = "# block our pricing-rule SQL\nSlow Query\n\n   # indented comment\nFull Table Scan\n\n"
		out = settings._parse_skip_list(raw)
		assert out == ("Slow Query", "Full Table Scan")

	def test_idempotent_re_parse(self):
		raw = "Slow Query\nHot Line"
		assert settings._parse_skip_list(raw) == settings._parse_skip_list(raw)


# --------------------------------------------------------------------------
# TestExcludeApplied — is_finding_type_excluded semantics
# --------------------------------------------------------------------------


class TestExcludeApplied:
	def test_returns_true_for_excluded_type(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_excluded_finding_types=("Slow Query", "Full Table Scan")),
			raising=True,
		)
		assert ai_fix.is_finding_type_excluded("Slow Query") is True
		assert ai_fix.is_finding_type_excluded("Full Table Scan") is True

	def test_returns_false_for_unlisted_type(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_excluded_finding_types=("Slow Query",)),
			raising=True,
		)
		assert ai_fix.is_finding_type_excluded("N+1 Query") is False
		assert ai_fix.is_finding_type_excluded("Hot Line") is False

	def test_is_case_sensitive(self, monkeypatch):
		# Typo / wrong case is inert — safer than partial-match leaking data.
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_excluded_finding_types=("Slow Query",)),
			raising=True,
		)
		assert ai_fix.is_finding_type_excluded("slow query") is False
		assert ai_fix.is_finding_type_excluded("SLOW QUERY") is False

	def test_handles_empty_or_none_input(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_excluded_finding_types=("Slow Query",)),
			raising=True,
		)
		assert ai_fix.is_finding_type_excluded("") is False
		assert ai_fix.is_finding_type_excluded(None) is False
		assert ai_fix.is_finding_type_excluded(0) is False  # type: ignore[arg-type]

	def test_returns_false_when_get_config_raises(self, monkeypatch):
		# Settings cache wedged / no bench. Safe-default to False (the master
		# gates ai_enabled + ai_auto_suggest already give coarser control).
		def _raise():
			raise RuntimeError("no bench")

		monkeypatch.setattr("optimus.settings.get_config", _raise, raising=True)
		assert ai_fix.is_finding_type_excluded("Slow Query") is False


# --------------------------------------------------------------------------
# TestSuggestFixRefuses — early-return before payload build
# --------------------------------------------------------------------------


class TestSuggestFixRefuses:
	def test_suggest_fix_short_circuits_for_excluded_type(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_excluded_finding_types=("Slow Query",)),
			raising=True,
		)
		# If we got past the guard, _resolve_provider would fail (no config).
		# The assertion is that the AiFixError fires with the exclusion
		# message, NOT the provider-not-configured message.
		with pytest.raises(ai_fix.AiFixError) as excinfo:
			ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "x"})
		assert "excluded by ai_excluded_finding_types" in str(excinfo.value)

	def test_suggest_fix_no_http_call_when_excluded(self, monkeypatch):
		# Belt + suspenders: even if the operator's network were intercepted,
		# requests.post must not be called for an excluded type.
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_excluded_finding_types=("N+1 Query",)),
			raising=True,
		)
		import requests as _requests

		calls: list = []

		def _spy_post(*args, **kwargs):
			calls.append((args, kwargs))
			raise AssertionError("requests.post should not be called for excluded types")

		monkeypatch.setattr(_requests, "post", _spy_post, raising=True)
		with pytest.raises(ai_fix.AiFixError):
			ai_fix.suggest_fix({"finding_type": "N+1 Query", "title": "x"})
		assert calls == []


# --------------------------------------------------------------------------
# TestTimeoutHonored — _http_post reads cfg.ai_request_timeout_seconds
# --------------------------------------------------------------------------


class _CaptureResp:
	"""Minimal Response stand-in. Returns 200 + a valid Anthropic-shaped body
	so _call_anthropic can parse it cleanly."""

	status_code = 200
	text = ""

	def json(self):
		return {"content": [{"type": "text", "text": "ok"}]}


def _capture_post():
	"""Return (post_fake, captured) — when the fake is called, it stashes
	the kwargs into captured[0] for assertion."""
	captured: list[dict] = []

	def _fake(url, headers=None, json=None, timeout=None):
		captured.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
		return _CaptureResp()

	return _fake, captured


class TestTimeoutHonored:
	def test_http_post_uses_configured_timeout(self, monkeypatch):
		# Pure ai_fix._http_post call — patch requests.post + settings.
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_request_timeout_seconds=180),
			raising=True,
		)
		fake, captured = _capture_post()
		import requests as _requests

		monkeypatch.setattr(_requests, "post", fake, raising=True)
		ai_fix._http_post(
			"https://localhost/v1/chat/completions",
			{},
			{"model": "x"},
			provider="openai",
			where="test",
		)
		assert captured[0]["timeout"] == 180

	def test_http_post_clamps_below_floor(self, monkeypatch):
		# _resolve_timeout_seconds clamps below 10 → 10.
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_request_timeout_seconds=5),
			raising=True,
		)
		fake, captured = _capture_post()
		import requests as _requests

		monkeypatch.setattr(_requests, "post", fake, raising=True)
		ai_fix._http_post(
			"https://localhost/v1/chat/completions",
			{},
			{"model": "x"},
			provider="openai",
			where="test",
		)
		assert captured[0]["timeout"] == 10

	def test_http_post_clamps_above_ceiling(self, monkeypatch):
		# _resolve_timeout_seconds clamps above 600 → 600.
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_request_timeout_seconds=9999),
			raising=True,
		)
		fake, captured = _capture_post()
		import requests as _requests

		monkeypatch.setattr(_requests, "post", fake, raising=True)
		ai_fix._http_post(
			"https://localhost/v1/chat/completions",
			{},
			{"model": "x"},
			provider="openai",
			where="test",
		)
		assert captured[0]["timeout"] == 600

	def test_http_post_falls_back_when_settings_unreadable(self, monkeypatch):
		# No bench / pure-pytest path — fallback to _HTTP_TIMEOUT (60).
		def _raise():
			raise RuntimeError("no bench")

		monkeypatch.setattr("optimus.settings.get_config", _raise, raising=True)
		fake, captured = _capture_post()
		import requests as _requests

		monkeypatch.setattr(_requests, "post", fake, raising=True)
		ai_fix._http_post(
			"https://localhost/v1/chat/completions",
			{},
			{"model": "x"},
			provider="openai",
			where="test",
		)
		assert captured[0]["timeout"] == ai_fix._HTTP_TIMEOUT


# --------------------------------------------------------------------------
# TestSettings — defaults + retention-style floor clamp
# --------------------------------------------------------------------------


def _settings_stub(monkeypatch):
	"""Install a minimal frappe stub (mirrors
	test_profiler_settings_validation.py) and return the OptimusSettings
	controller class freshly re-imported."""
	stub = types.ModuleType("frappe")
	stub.msgprint = lambda *a, **k: None
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: None,
		get_value=lambda k: None,
		set_value=lambda k, v: None,
	)
	stub.log_error = lambda **k: None

	model_mod = types.ModuleType("frappe.model")
	doc_mod = types.ModuleType("frappe.model.document")

	class Document:
		def __init__(self, **kwargs):
			for k, v in kwargs.items():
				setattr(self, k, v)

		def get(self, k, default=None):
			return getattr(self, k, default)

	doc_mod.Document = Document
	monkeypatch.setitem(sys.modules, "frappe", stub)
	monkeypatch.setitem(sys.modules, "frappe.model", model_mod)
	monkeypatch.setitem(sys.modules, "frappe.model.document", doc_mod)
	for mod in list(sys.modules.keys()):
		if mod.startswith("optimus.optimus.doctype.optimus_settings"):
			monkeypatch.delitem(sys.modules, mod, raising=False)
	from optimus.optimus.doctype.optimus_settings.optimus_settings import (
		OptimusSettings,
	)

	return OptimusSettings


class TestSettings:
	def test_defaults_in_optimus_config(self):
		cfg = settings.OptimusConfig()
		assert cfg.ai_excluded_finding_types == ()
		assert cfg.ai_request_timeout_seconds == 60

	def test_timeout_floor_clamps_zero(self, monkeypatch):
		OptimusSettings = _settings_stub(monkeypatch)
		doc = OptimusSettings()
		doc.ai_request_timeout_seconds = 0
		doc._clamp_numeric_floors()
		assert doc.ai_request_timeout_seconds == 10

	def test_timeout_floor_clamps_negative(self, monkeypatch):
		OptimusSettings = _settings_stub(monkeypatch)
		doc = OptimusSettings()
		doc.ai_request_timeout_seconds = -1
		doc._clamp_numeric_floors()
		assert doc.ai_request_timeout_seconds == 10

	def test_timeout_floor_leaves_valid_values_alone(self, monkeypatch):
		OptimusSettings = _settings_stub(monkeypatch)
		doc = OptimusSettings()
		doc.ai_request_timeout_seconds = 180
		doc._clamp_numeric_floors()
		assert doc.ai_request_timeout_seconds == 180


# --------------------------------------------------------------------------
# TestDocStaysFresh — the doc's eligible-types enumeration matches the code
# --------------------------------------------------------------------------


class TestDocStaysFresh:
	def test_doc_eligible_types_match_frozenset(self):
		"""The doc's § 5 lists the eligible types as a bullet list. This
		test asserts the bullet list (alphabetised) matches the frozenset.
		Drift in either direction (a new type added in code without doc
		update, or a doc edit that misspelled a type) fails here."""
		import os

		doc_path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "AI-FIXING.md")
		with open(doc_path) as f:
			text = f.read()

		# Locate the bullet list after the "## 5. Eligible finding types"
		# heading. The next markdown heading is "### 5.1" so we read between
		# those markers.
		import re

		m = re.search(
			r"## 5\. Eligible finding types\b(.*?)### 5\.1",
			text,
			re.DOTALL,
		)
		assert m, "could not find § 5 in docs/AI-FIXING.md"
		bullets = re.findall(r"^- (.+)$", m.group(1), re.MULTILINE)
		doc_types = sorted(b.strip() for b in bullets)
		code_types = sorted(ai_fix.AI_ELIGIBLE_FINDING_TYPES)
		assert doc_types == code_types, (
			f"docs/AI-FIXING.md § 5 has {doc_types!r} but "
			f"AI_ELIGIBLE_FINDING_TYPES has {code_types!r} — keep them in sync."
		)
