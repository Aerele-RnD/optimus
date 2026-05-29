# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the Sensitivity Profile presets in optimus.settings.

A ``config_profile`` Select (Strict / Recommended / Relaxed / Custom) drives
the detection-sensitivity thresholds. Storage strategy is *resolve at read
time*: only the profile name is stored; ``_resolve()`` maps profile →
thresholds for the named presets, so ``Recommended`` always tracks the current
shipped defaults. Only ``Custom`` reads the per-field stored values (preserving
the pre-profile precedence: DocType row > site_config > default).

Back-compat: an existing Single predating the field has no ``config_profile``
key, which must resolve as ``Custom`` so a previously-tuned threshold keeps
driving analysis (no silent reset to Recommended, no migration patch).
"""

import json
import os
import re
import sys
import types

import pytest

from optimus import settings

# The DocType field descriptions advertise "Reference values — Strict: X ·
# Recommended: Y · Relaxed: Z" to admins. Those numbers MUST equal _PROFILES or
# the UI lies about what a preset does. This path locates the DocType JSON
# relative to this test file (no bench needed).
_SETTINGS_JSON = os.path.join(
	os.path.dirname(os.path.dirname(__file__)),
	"optimus", "doctype", "optimus_settings", "optimus_settings.json",
)
_REFVAL_RE = re.compile(
	# ``\d+(?:\.\d+)?`` matches an optional decimal. The non-greedy
	# ``[\d.]+`` form the v0.7.x test used greedily ate the trailing
	# period from sentence-ending descriptions ("...Relaxed: 5.0.") and
	# broke ``float()``.
	r"Strict:\s*(\d+(?:\.\d+)?).*?Recommended:\s*(\d+(?:\.\d+)?).*?"
	r"Relaxed:\s*(\d+(?:\.\d+)?)"
)

# Every knob whose DocType description advertises a "Reference values —
# Strict / Recommended / Relaxed" triplet is governed by the preset.
# Kept here (not imported) so the test pins the contract independently
# of the implementation's own tuple. v0.13.x expanded the set from 9
# detection-sensitivity knobs to 19 — including capture caps,
# retention, display filters, Phase-2 UI knobs, and the AI auto-suggest
# cap — so the UI's "Reference values" promise is honored everywhere
# it's advertised.
SENSITIVITY_KEYS = (
	# General → Session retention
	"session_retention_days",
	# Capture capacity
	"max_queries_per_recording",
	"pyinstrument_sampler_interval_ms",
	"background_job_wait_seconds",
	# Display filters
	"min_action_duration_ms",
	"large_duration_threshold_ms",
	# Analyzer thresholds (the original nine)
	"redundant_doc_threshold",
	"redundant_cache_threshold",
	"redundant_perm_threshold",
	"n_plus_one_min_occurrences",
	"slow_query_threshold_ms",
	"slow_hot_path_pct_threshold",
	"slow_hot_path_min_ms",
	"hot_line_high_pct",
	"hot_line_high_min_ms",
	# Phase-2 UI knobs
	"phase2_max_runs_per_session",
	"auto_expand_max_depth",
	"auto_expand_min_ms",
	# AI auto-suggest cap
	"ai_auto_suggest_max",
)


@pytest.fixture(autouse=True)
def _frappe_stub(monkeypatch):
	"""Minimal frappe stub, mirroring test_settings.py — settings.py
	imports frappe lazily, but get_config's cache path touches
	frappe.cache, so give it harmless no-ops."""
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


def _no_site_conf(monkeypatch):
	monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)


class TestProfileTable:
	def test_named_profiles_exist(self):
		for name in ("Strict", "Recommended", "Relaxed"):
			assert name in settings._PROFILES, f"missing profile {name}"
			# Each named profile defines all nine sensitivity knobs.
			assert set(settings._PROFILES[name]) == set(SENSITIVITY_KEYS)

	def test_custom_is_not_a_named_profile(self):
		# Custom means "use stored field values", so it must NOT be in
		# the preset table — _resolve keys off `_PROFILES.get(profile)`.
		assert "Custom" not in settings._PROFILES

	def test_recommended_equals_defaults(self):
		"""Drift guard: Recommended must mirror the shipped _DEFAULTS for
		every sensitivity key, or the 'Recommended tracks defaults'
		promise silently breaks."""
		for key in SENSITIVITY_KEYS:
			assert settings._PROFILES["Recommended"][key] == settings._DEFAULTS[key], key

	# ---- Strict-vs-Relaxed semantic ordering ---------------------------
	#
	# For DETECTION thresholds the rule is "lower = catch more" — Strict's
	# number is at-or-below Relaxed's, so a redundant-call loop or a slow
	# query fires under Strict at a hair lower a cost.
	#
	# For RESOURCE / RETENTION knobs the rule inverts — "higher = stricter
	# data preservation" — Strict's number is at-or-above Relaxed's. A
	# Strict deployment keeps sessions for 90 days vs Relaxed's 7,
	# captures 5000 queries per recording vs 1000, allows 25 Phase-2 runs
	# per session vs 5, etc. Stricter monitoring → more data.
	#
	# Categorize each key so the sanity check enforces the right ordering
	# per group. Any new sensitivity key must join one of the two tuples
	# below or this test will tell the engineer which side it falls on.

	# Strict <= Relaxed (lower number = stricter — for detection
	# thresholds, lower = catch more; for ``auto_expand_min_ms``, lower
	# = follow into smaller hot spots; Strict's 0 is the strictest
	# possible value).
	_STRICT_LOWER_KEYS = (
		# v0.13.x: shorter retention = stricter housekeeping (Strict
		# treats old session rows as a liability to clear fast).
		"session_retention_days",
		"pyinstrument_sampler_interval_ms",
		"min_action_duration_ms",
		"large_duration_threshold_ms",
		"auto_expand_min_ms",
		"redundant_doc_threshold",
		"redundant_cache_threshold",
		"redundant_perm_threshold",
		"n_plus_one_min_occurrences",
		"slow_query_threshold_ms",
		"slow_hot_path_pct_threshold",
		"slow_hot_path_min_ms",
		"hot_line_high_pct",
		"hot_line_high_min_ms",
	)
	# Strict >= Relaxed (higher number = stricter, more data / more
	# preservation / more visibility)
	_STRICT_HIGHER_KEYS = (
		"background_job_wait_seconds",
	)
	# v0.13.x: fields whose read site honors ``0 = unlimited``. Strict
	# uses 0 (the strictest possible posture: don't cap, don't drop,
	# don't truncate, don't expire). Relaxed uses a literal positive
	# number, so ``Strict >= Relaxed`` doesn't hold here — the contract
	# is "Strict is 0 AND Relaxed is positive AND Relaxed is positive".
	_STRICT_UNBOUNDED_KEYS = (
		"max_queries_per_recording",
		"phase2_max_runs_per_session",
		"auto_expand_max_depth",
		"ai_auto_suggest_max",
	)

	def test_strict_catches_more_than_relaxed_on_detection_knobs(self):
		"""Detection thresholds: every Strict knob is <= its Relaxed
		counterpart (lower threshold = catch more findings)."""
		for key in self._STRICT_LOWER_KEYS:
			assert settings._PROFILES["Strict"][key] <= settings._PROFILES["Relaxed"][key], key

	def test_strict_keeps_more_data_than_relaxed_on_resource_knobs(self):
		"""Resource / retention knobs: every Strict value is >= its
		Relaxed counterpart (higher = stricter monitoring posture)."""
		for key in self._STRICT_HIGHER_KEYS:
			assert settings._PROFILES["Strict"][key] >= settings._PROFILES["Relaxed"][key], key

	def test_strict_uses_unlimited_sentinel_on_capped_knobs(self):
		"""v0.13.x: every cap that honors 0-as-unlimited at its read site
		uses 0 under Strict (the strictest posture). Relaxed always uses
		a positive literal — the read site code that branches on ``cap
		> 0`` would otherwise act unbounded under Relaxed too, defeating
		the point of the preset."""
		for key in self._STRICT_UNBOUNDED_KEYS:
			assert settings._PROFILES["Strict"][key] == 0, f"{key} Strict must be 0"
			assert settings._PROFILES["Relaxed"][key] > 0, f"{key} Relaxed must be > 0"

	def test_every_sensitivity_key_is_categorised(self):
		"""Drift guard: a newly-added sensitivity key must land in one of
		the three ordering groups above, or the test stops being meaningful."""
		categorised = (
			set(self._STRICT_LOWER_KEYS)
			| set(self._STRICT_HIGHER_KEYS)
			| set(self._STRICT_UNBOUNDED_KEYS)
		)
		uncategorised = set(SENSITIVITY_KEYS) - categorised
		assert not uncategorised, (
			f"new sensitivity key(s) need to join _STRICT_LOWER_KEYS, "
			f"_STRICT_HIGHER_KEYS, or _STRICT_UNBOUNDED_KEYS in this "
			f"test: {uncategorised}"
		)

	def test_profiles_match_doctype_reference_values(self):
		"""_PROFILES must equal the 'Reference values — Strict/Recommended/
		Relaxed' numbers advertised in each sensitivity field's DocType
		description, so the form's help text never contradicts behavior."""
		with open(_SETTINGS_JSON) as fh:
			doc = json.load(fh)
		by_name = {f["fieldname"]: f for f in doc["fields"]}
		for key in SENSITIVITY_KEYS:
			desc = by_name[key].get("description", "")
			m = _REFVAL_RE.search(desc)
			assert m, f"{key} description missing 'Reference values' triplet"
			strict, recommended, relaxed = (float(g) for g in m.groups())
			assert settings._PROFILES["Strict"][key] == strict, f"{key} Strict"
			assert settings._PROFILES["Recommended"][key] == recommended, f"{key} Recommended"
			assert settings._PROFILES["Relaxed"][key] == relaxed, f"{key} Relaxed"


class TestProfileResolution:
	@pytest.mark.parametrize("profile", ["Strict", "Recommended", "Relaxed"])
	def test_named_profile_drives_all_keys(self, monkeypatch, profile):
		"""A named profile resolves every sensitivity key to its preset
		number, IGNORING any conflicting stored field value."""
		row = {"config_profile": profile}
		# Poison every key with a value that is neither the preset nor a default.
		for key in SENSITIVITY_KEYS:
			row[key] = 9999
		monkeypatch.setattr(settings, "_read_doctype_row", lambda: row)
		_no_site_conf(monkeypatch)

		cfg = settings._resolve()
		for key in SENSITIVITY_KEYS:
			assert getattr(cfg, key) == settings._PROFILES[profile][key], key

	def test_custom_reads_stored_values(self, monkeypatch):
		"""Custom preserves the pre-profile behavior: stored DocType value wins."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"config_profile": "Custom", "redundant_doc_threshold": 7},
		)
		_no_site_conf(monkeypatch)
		assert settings._resolve().redundant_doc_threshold == 7

	def test_custom_still_honors_site_config_fallback(self, monkeypatch):
		"""Under Custom, the legacy site_config fallback still applies when
		the DocType value is unset."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"config_profile": "Custom", "redundant_doc_threshold": None},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 99 if k == "redundant_doc_threshold" else None,
		)
		assert settings._resolve().redundant_doc_threshold == 99

	def test_named_profile_bypasses_site_config(self, monkeypatch):
		"""A named profile wins over site_config — the preset is authoritative."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"config_profile": "Strict"},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 99 if k == "redundant_doc_threshold" else None,
		)
		assert settings._resolve().redundant_doc_threshold == \
			settings._PROFILES["Strict"]["redundant_doc_threshold"]


class TestBackCompat:
	def test_missing_profile_key_resolves_as_custom(self, monkeypatch):
		"""An existing Single predating the field has no config_profile key.
		It must resolve as Custom (preserve stored values), NOT Recommended."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": 7},  # no config_profile
		)
		_no_site_conf(monkeypatch)
		cfg = settings._resolve()
		assert cfg.config_profile == "Custom"
		assert cfg.redundant_doc_threshold == 7  # stored value preserved

	def test_dataclass_default_is_custom(self):
		"""The no-frappe / pre-bench path (OptimusConfig()) must default to
		Custom so threshold dataclass defaults (= Recommended numbers) are
		used as-is, not overridden by an absent profile."""
		assert settings.OptimusConfig().config_profile == "Custom"
		assert settings._DEFAULTS["config_profile"] == "Custom"
