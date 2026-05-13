# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.6.0 AI-fix config fields on Optimus Settings.

Pure-test path. Verifies the new fields default sensibly, resolve from a
DocType row, and — importantly — that the secret API key is NOT part of
the cached ``OptimusConfig`` (it's read on demand by ai_fix.py via
Frappe's encrypted-password store).
"""

from unittest.mock import patch

from optimus import settings


class TestAiConfigDefaults:
	def test_defaults(self):
		cfg = settings.OptimusConfig()
		assert cfg.ai_enabled is False
		assert cfg.ai_provider == "Anthropic"
		assert cfg.ai_base_url == ""
		assert cfg.ai_model == ""
		# Auto-suggest is off by default; cap is 5.
		assert cfg.ai_auto_suggest is False
		assert cfg.ai_auto_suggest_max == 5
		# "Humanize Steps to Reproduce" is on by default (only takes effect
		# once ai_enabled is also turned on).
		assert cfg.ai_humanize_steps is True
		# v0.6.x: per-section "use the LLM for X" toggles default on (only
		# take effect once ai_enabled is also turned on).
		assert cfg.ai_suggest_findings is True
		assert cfg.ai_suggest_indexes is True

	def test_api_key_is_not_part_of_the_config(self):
		# The key is sensitive — never cached on the config snapshot.
		assert not hasattr(settings.OptimusConfig(), "ai_api_key")
		assert "ai_api_key" not in settings._DEFAULTS

	def test_defaults_dict_has_the_ai_keys(self):
		for k in ("ai_enabled", "ai_provider", "ai_base_url", "ai_model",
		          "ai_auto_suggest", "ai_auto_suggest_max", "ai_humanize_steps",
		          "ai_suggest_findings", "ai_suggest_indexes"):
			assert k in settings._DEFAULTS


class TestAiConfigResolution:
	def _row(self, **overrides):
		# A complete-enough row so _resolve's threshold helpers never
		# reach _site_conf_fallback (which would need a live bench).
		row = {
			"enabled": True,
			"session_retention_days": 30,
			"tracked_apps": (),
			"max_queries_per_recording": 2000,
			"redundant_doc_threshold": 5,
			"redundant_cache_threshold": 50,
			"redundant_perm_threshold": 10,
			"n_plus_one_min_occurrences": 10,
			"slow_query_threshold_ms": 200.0,
			"slow_hot_path_pct_threshold": 25.0,
			"slow_hot_path_min_ms": 200.0,
			"hot_line_high_pct": 50.0,
			"hot_line_high_min_ms": 100.0,
			"pyinstrument_sampler_interval_ms": 1.0,
			"min_action_duration_ms": 0.0,
			"phase2_max_runs_per_session": 10,
			"phase2_default_auto_expand": True,
			"auto_expand_max_depth": 10,
			"auto_expand_min_ms": 50.0,
			"skip_request_paths": (),
			"skip_users": (),
			"ai_enabled": False,
			"ai_provider": None,
			"ai_base_url": None,
			"ai_model": None,
			"ai_auto_suggest": False,
			"ai_auto_suggest_max": 5,
		}
		row.update(overrides)
		return row

	def test_resolve_uses_defaults_when_unset(self):
		with patch.object(settings, "_read_doctype_row", return_value=self._row()), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			cfg = settings._resolve()
		assert cfg.ai_enabled is False
		assert cfg.ai_provider == "Anthropic"
		assert cfg.ai_base_url == ""
		assert cfg.ai_model == ""

	def test_resolve_picks_up_configured_values(self):
		row = self._row(
			ai_enabled=True,
			ai_provider="Kimi (Moonshot)",
			ai_base_url="https://api.moonshot.ai/v1",
			ai_model="kimi-k2-0905-preview",
		)
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			cfg = settings._resolve()
		assert cfg.ai_enabled is True
		assert cfg.ai_provider == "Kimi (Moonshot)"
		assert cfg.ai_base_url == "https://api.moonshot.ai/v1"
		assert cfg.ai_model == "kimi-k2-0905-preview"

	def test_blank_provider_falls_through_to_default(self):
		# A row whose ai_provider is a blank string (cleared field) must
		# resolve back to the default, not to "".
		row = self._row(ai_provider="", ai_base_url="", ai_model="")
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			cfg = settings._resolve()
		assert cfg.ai_provider == "Anthropic"

	def test_auto_suggest_resolves(self):
		row = self._row(ai_auto_suggest=True, ai_auto_suggest_max=12)
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			cfg = settings._resolve()
		assert cfg.ai_auto_suggest is True
		assert cfg.ai_auto_suggest_max == 12

	def test_auto_suggest_max_zero_means_all(self):
		# 0 is a legitimate value ("every eligible finding") — must not
		# fall through to the default of 5.
		row = self._row(ai_auto_suggest=True, ai_auto_suggest_max=0)
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			cfg = settings._resolve()
		assert cfg.ai_auto_suggest_max == 0

	def test_humanize_steps_resolves(self):
		# Explicitly off in the row → off.
		row = self._row(ai_humanize_steps=False)
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().ai_humanize_steps is False
		# Not present in the row (pre-field Single) → defaults on.
		row2 = self._row()
		row2.pop("ai_humanize_steps", None)
		with patch.object(settings, "_read_doctype_row", return_value=row2), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().ai_humanize_steps is True

	def test_suggest_findings_resolves(self):
		row = self._row(ai_suggest_findings=False)
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().ai_suggest_findings is False
		# Field absent on the Single (e.g. saved before v0.6.x) → defaults on.
		row2 = self._row()  # base _row() doesn't carry the new fields
		with patch.object(settings, "_read_doctype_row", return_value=row2), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().ai_suggest_findings is True

	def test_suggest_indexes_resolves(self):
		row = self._row(ai_suggest_indexes=False)
		with patch.object(settings, "_read_doctype_row", return_value=row), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().ai_suggest_indexes is False
		row2 = self._row()
		with patch.object(settings, "_read_doctype_row", return_value=row2), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().ai_suggest_indexes is True
