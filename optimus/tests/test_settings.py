# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the optimus.settings cached reader.

The reader is the single place analyzers and hooks call to resolve
configuration — threshold values, the enabled toggle, the tracked-
apps allowlist. Tests here pin the precedence (DocType > site_config
> default), the soft-fail behavior (never crash a request), and the
dataclass immutability that makes caching safe.
"""

import sys
import types
from dataclasses import FrozenInstanceError

import pytest

# settings.py imports ``frappe`` lazily inside each function (see the
# NOTE at the top of optimus/settings.py), so a frappe stub
# isn't needed at module-load time. But a handful of tests below DO
# need ``frappe.cache.get_value`` to exist (so they can patch it). We
# install a per-test stub via an autouse fixture below — that way the
# stub doesn't leak to other test files (was the leading source of the
# "80 failed" pollution: a module-level ``sys.modules["frappe"] = stub``
# replaced the real frappe for the entire pytest session).
from optimus import settings


@pytest.fixture(autouse=True)
def _frappe_stub(monkeypatch):
	"""Install a minimal frappe stub for the duration of each test. The
	conftest.py ``_sys_modules_fence`` would catch the swap and restore
	at teardown anyway, but monkeypatch.setitem makes the contract
	explicit and the auto-restore guaranteed."""
	stub = types.ModuleType("frappe")
	stub.cache = types.SimpleNamespace(
		get_value=lambda k: None,
		set_value=lambda k, v: None,
		delete_value=lambda k: None,
	)
	stub.conf = {}
	stub.db = types.SimpleNamespace(exists=lambda *a, **kw: False)
	stub.get_cached_doc = lambda *a, **kw: None
	monkeypatch.setitem(sys.modules, "frappe", stub)
	yield stub


class TestDefaults:
	def test_config_has_defaults_when_doctype_missing(self, monkeypatch):
		"""No DocType available → hardcoded defaults. This is the
		fresh-install / pre-migrate path, must not raise."""
		monkeypatch.setattr(settings, "_read_doctype_row", lambda: None)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)

		cfg = settings._resolve()
		assert cfg.enabled is True
		assert cfg.session_retention_days == 30
		assert cfg.tracked_apps == ()
		# v0.13.x: defaults seeded with frappe + erpnext (was empty pre-v0.13.x).
		# Install hook also writes these to the DocType on fresh installs.
		assert cfg.ignored_apps == ("frappe", "erpnext")
		assert cfg.hide_framework_tables is True  # v0.6.x: default on.
		assert cfg.redundant_doc_threshold == 5
		# v0.5.2 round 4: bumped from 10 → 50 to cut 0ms "cache
		# looked up 20× from same callsite" noise that we can't
		# time (cache lookups are free individually). 50 matches
		# the High-severity boundary; anything below is too
		# ambiguous to emit at Medium.
		assert cfg.redundant_cache_threshold == 50
		assert cfg.redundant_perm_threshold == 10
		assert cfg.n_plus_one_min_occurrences == 10
		# v0.5.3: per-recording EXPLAIN / enrichment cap. Fallback
		# default is 2000 — comfortable for most flows, with a clear
		# banner when truncation kicks in for heavier flows.
		assert cfg.max_queries_per_recording == 2000

	def test_config_is_frozen(self):
		cfg = settings.OptimusConfig()
		with pytest.raises(FrozenInstanceError):
			cfg.enabled = False


class TestPrecedence:
	"""Precedence: DocType row > site_config.json > hardcoded default."""

	def test_doctype_wins_over_site_config(self, monkeypatch):
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": 7},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 3 if k == "redundant_doc_threshold" else None,
		)
		cfg = settings._resolve()
		assert cfg.redundant_doc_threshold == 7

	def test_site_config_wins_when_doctype_unset(self, monkeypatch):
		"""DocType returns 0 / None for the field → fall through to
		site_config. Tests the pattern of 'admin hasn't populated
		Settings but has legacy site_config overrides'."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": None},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 99 if k == "redundant_doc_threshold" else None,
		)
		cfg = settings._resolve()
		assert cfg.redundant_doc_threshold == 99

	def test_default_fallback_when_both_unset(self, monkeypatch):
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": None},
		)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		cfg = settings._resolve()
		assert cfg.redundant_doc_threshold == 5  # default


class TestSoftFail:
	def test_get_config_never_raises(self, monkeypatch):
		"""No matter what _resolve raises, get_config returns defaults."""
		def boom():
			raise RuntimeError("db gone")
		monkeypatch.setattr(settings, "_resolve", boom)
		# Defeat the cache path too.
		import frappe
		monkeypatch.setattr(
			frappe.cache, "get_value",
			lambda k: None, raising=False,
		)
		cfg = settings.get_config()
		assert cfg.enabled is True  # fail-open default

	def test_is_enabled_defaults_true_on_error(self, monkeypatch):
		"""If the settings read crashes, is_enabled must return True.
		Returning False would silently disable the profiler — a very
		confusing support issue ('why isn't it recording anything?')."""
		def boom():
			raise RuntimeError("cache down")
		monkeypatch.setattr(settings, "get_config", boom)
		assert settings.is_enabled() is True

	def test_get_tracked_apps_returns_empty_tuple_on_error(self, monkeypatch):
		def boom():
			raise RuntimeError("cache down")
		monkeypatch.setattr(settings, "get_config", boom)
		assert settings.get_tracked_apps() == ()


class TestTrackedApps:
	def test_tracked_apps_normalized_to_tuple(self, monkeypatch):
		"""tracked_apps must be a tuple in the dataclass so it's
		hashable / immutable. Input from the DocType is a list — the
		reader must convert."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"tracked_apps": ("myapp", "another")},
		)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		cfg = settings._resolve()
		assert isinstance(cfg.tracked_apps, tuple)
		assert cfg.tracked_apps == ("myapp", "another")


class TestIgnoredApps:
	"""v0.6.x: 'Ignored Apps' — exclusion list whose findings are dropped from
	the report. Mirrors the tracked_apps wiring."""

	def test_default_seeds_framework_apps(self):
		# v0.13.x: default seeded with frappe + erpnext so a typical
		# custom-app operator doesn't see framework noise on day one.
		# The dataclass default + _DEFAULTS must agree (the no-bench
		# fallback path reads the dataclass; the resolve path reads
		# _DEFAULTS).
		assert settings.OptimusConfig().ignored_apps == ("frappe", "erpnext")
		assert settings._DEFAULTS["ignored_apps"] == ("frappe", "erpnext")

	def test_resolves_from_row(self, monkeypatch):
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"ignored_apps": ("frappe", "optimus")},
		)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		cfg = settings._resolve()
		assert isinstance(cfg.ignored_apps, tuple)
		assert cfg.ignored_apps == ("frappe", "optimus")

	def test_falls_through_to_defaults_when_absent_in_row(self, monkeypatch):
		# v0.13.x: when the DocType row hasn't populated ignored_apps
		# (pre-v0.6.x Single, or a row from before the install hook ran),
		# resolve falls through to the seeded defaults so the no-bench /
		# pre-migrate path matches the post-install behaviour.
		monkeypatch.setattr(settings, "_read_doctype_row", lambda: {"enabled": True})
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		assert settings._resolve().ignored_apps == ("frappe", "erpnext")

	def test_get_ignored_apps_returns_empty_on_error(self, monkeypatch):
		def _boom():
			raise RuntimeError("no config")
		monkeypatch.setattr(settings, "get_config", _boom)
		assert settings.get_ignored_apps() == ()


class TestHideFrameworkTables:
	"""v0.6.x: 'Hide framework / internal database tables' Check (default
	True). When on, the renderer drops framework/internal tables from the
	'Time spent per database table' section. Default-True Check pattern
	mirrors ai_humanize_steps."""

	def test_default_is_true(self):
		assert settings.OptimusConfig().hide_framework_tables is True
		assert settings._DEFAULTS["hide_framework_tables"] is True

	def test_resolves_explicit_false(self, monkeypatch):
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"hide_framework_tables": False},
		)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		assert settings._resolve().hide_framework_tables is False

	def test_resolves_to_default_when_absent_in_row(self, monkeypatch):
		# Pre-v0.6.x Single (field doesn't exist yet) → defaults to True.
		monkeypatch.setattr(settings, "_read_doctype_row", lambda: {"enabled": True})
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		assert settings._resolve().hide_framework_tables is True
