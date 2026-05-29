# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Cached reader for Optimus Settings.

Every request goes through ``hooks_callbacks.before_request`` which
checks ``is_enabled()`` — reading the Single doc directly on every
request would be a DB hit per request. We cache the resolved config
in Redis with a version key; the DocType controller bumps the key
on save.

Falls back to ``frappe.conf`` for thresholds when the setting is
unset or the DocType row doesn't exist yet (fresh install, pre-
migration). That preserves the pre-v0.5.2 behavior where thresholds
lived in ``site_config.json``.
"""

from dataclasses import dataclass, field

# NOTE: frappe is imported lazily inside each function rather than at
# module top. Importing at top forces every unit test that touches
# ``from optimus import settings`` to run under bench, which
# would exclude pure-logic tests from the plain-pytest path. All
# runtime callers (hooks, analyzers) import inside a Frappe request
# context, so the lazy import is free there.


# Frozen default values. Matches the constants that USED to live in
# individual analyzers before v0.5.2 centralized configuration. Kept
# in this module so analyzers never have to know about DocType shape.
#
# v0.5.2 round 4: cache threshold bumped from 10 → 50. We don't time
# individual cache lookups (sidecar captures name + args, not wall
# time), so every Redundant Cache finding reports impact=0ms. A 10×
# loop at 0ms impact is indistinguishable from framework background
# noise; a 50× loop is a clear pattern. 50 also matches the
# severity=High multiplier, so low-count loops that DID previously
# emit at Medium (with unknown impact) were the worst kind of noise.
_DEFAULTS = {
	"enabled": True,
	"session_retention_days": 30,
	"tracked_apps": (),  # tuple, not list — immutable for caching
	# Exclusion list — findings whose blame app falls in this tuple are
	# dropped from the report (both Findings and Observations sections).
	# v0.13.x: default seeded with every Frappe-organization-maintained
	# app (frappe, erpnext, hrms, lms, helpdesk, insights, crm, builder,
	# wiki, drive, payments). The install hook
	# (``optimus.install._seed_ignored_apps_with_framework_apps``) writes
	# these as initial rows into the Optimus Settings DocType on fresh
	# installs (idempotent — never overwrites an existing configuration).
	# Pre-v0.13.x sites stay at whatever they configured manually; only
	# new sites pick up the seed. Operators who actively contribute to
	# one of these apps (ERPNext core devs, HRMS maintainers, etc.)
	# remove the rows they care about post-install.
	"ignored_apps": (
		"builder",
		"crm",
		"drive",
		"erpnext",
		"frappe",
		"helpdesk",
		"hrms",
		"insights",
		"lms",
		"payments",
		"wiki",
	),
	# v0.6.x: when True, the "Time spent per database table" section drops
	# Frappe schema/meta tables, framework-internal tables (User, Has Role,
	# DefaultValue, …), and information_schema.* — framework noise the app
	# developer can't act on. Default on; admins can uncheck.
	"hide_framework_tables": True,
	# v0.5.3: per-recording EXPLAIN / enrichment cap. Long flows (bulk
	# Submit chains producing 5000+ queries per recording) would exceed
	# the RQ job timeout if we ran EXPLAIN on every one. Capped so
	# analyze stays bounded. Most sessions are well under 2000; raise
	# to 5000/10000 for legitimately-heavy flows.
	"max_queries_per_recording": 2000,
	"redundant_doc_threshold": 5,
	"redundant_cache_threshold": 50,
	"redundant_perm_threshold": 10,
	"n_plus_one_min_occurrences": 10,
	# v0.6.0 Round 6: previously-hardcoded analyzer + capture knobs.
	"slow_query_threshold_ms": 200.0,
	"slow_hot_path_pct_threshold": 25.0,
	"slow_hot_path_min_ms": 200.0,
	"hot_line_high_pct": 50.0,
	"hot_line_high_min_ms": 100.0,
	"pyinstrument_sampler_interval_ms": 1.0,
	"min_action_duration_ms": 0.0,
	# v0.6.x: durations above this threshold (in ms) are rendered as seconds
	# in the report (e.g. 5234ms → 5.23s). Below it, ms is preserved. Set to
	# a very large value to effectively disable the conversion.
	"large_duration_threshold_ms": 1000.0,
	"phase2_max_runs_per_session": 10,
	"phase2_default_auto_expand": True,
	# v0.6.0: how long the analyze job waits (seconds, capped at 300) for the
	# background jobs the profiled flow enqueued to finish before gathering
	# recordings — so jobs that a worker picks up shortly after Stop aren't
	# lost. 0 = don't wait (pre-v0.6.0 behavior). On a single-worker bench
	# the analyze job yields the worker between checks (it re-enqueues
	# itself) so those jobs can actually run; if no worker / scheduler is
	# disabled, the wait is skipped.
	"background_job_wait_seconds": 300,
	"auto_expand_max_depth": 10,
	"auto_expand_min_ms": 50.0,
	"skip_request_paths": (),  # tuple of stripped, comment-free lines
	"skip_users": (),
	# v0.7.x+: additive lists for capture-time redaction (see
	# optimus/redaction.py). The defaults already cover the 12
	# canonical patterns (password, api_key, token, …); these extend
	# them with customer-specific names (recovery_code, bank_account,
	# otp_seed, …). Parsed the same way as skip_request_paths: one
	# entry per line, # comments, blank lines dropped.
	"sensitive_sql_columns": (),
	"sensitive_form_keys": (),
	# v0.6.0: opt-in LLM "suggest a fix" feature. The API key is NOT here —
	# it's secret, stored in a Password field, and read on demand by
	# ai_fix.py via frappe.utils.password.get_decrypted_password.
	"ai_enabled": False,
	"ai_provider": "Anthropic",
	"ai_base_url": "",
	"ai_model": "",
	# When True, the analyze pipeline auto-generates a fix for the top
	# ai_auto_suggest_max eligible findings (0 = all).
	"ai_auto_suggest": False,
	"ai_auto_suggest_max": 5,
	# When True (and ai_enabled), the analyze pipeline rewrites the
	# auto-generated "Steps to Reproduce" note into a friendly, human-
	# readable flow via the LLM (falls back to the raw action list on any
	# failure). Also available on-demand from the Optimus Session form.
	"ai_humanize_steps": True,
	# v0.6.x: per-section "use the LLM for X" toggles — hard off (no auto-
	# bake, the form buttons hide, the API refuses, re-rendered reports omit
	# the block). Default on, so the master ai_enabled switch alone turns
	# everything on. (ai_humanize_steps above is the third one.)
	"ai_suggest_findings": True,
	"ai_suggest_indexes": True,
	# v0.7.x: Sensitivity Profile. "Custom" means "read the per-field stored
	# values" (the pre-profile behavior). The named presets below override the
	# nine detection-sensitivity knobs at resolve time. Default is "Custom" so a
	# pre-profile Single (no config_profile key) keeps its stored thresholds —
	# see _read_doctype_row's coalesce and the back-compat note in _resolve.
	"config_profile": "Custom",
	# v0.9.0: AI privacy hardening (closes Critical Risk #2). Exclusion
	# list is empty by default (no types skipped); the timeout default of
	# 60s matches the pre-v0.9.0 hardcoded constant so existing setups are
	# behavior-preserving. See docs/AI-FIXING.md for the per-pathway data
	# inventory and local-LLM recipes.
	"ai_excluded_finding_types": (),
	"ai_request_timeout_seconds": 60,
}

# v0.13.x: every knob whose DocType description advertises a
# "Reference values — Strict: X · Recommended: Y · Relaxed: Z" triplet
# joins the Sensitivity Profile. Pre-v0.13.x this was nine detection-
# sensitivity thresholds only (the comment block here used to say
# "display filters / Phase-2 UI / capture caps / retention / AI are
# deployment choices, not detection sensitivity") — but every one of
# those fields ALSO advertises a triplet in its operator-facing
# description, which made the UI promise something the resolve path
# wasn't keeping. The fix: honor the triplet everywhere it's
# advertised. A field has a triplet iff its tuning matters enough for
# a deployment to want a one-knob preset; that's the right test for
# inclusion regardless of which capture-vs-detection-vs-display axis
# it lives on.
#
# Order mirrors the DocType's field_order so the JS-side
# get_config_profiles API returns presets in the same order operators
# see them in the form.
_SENSITIVITY_KEYS = (
	# General → Session retention
	"session_retention_days",
	# Capture capacity
	"max_queries_per_recording",
	"pyinstrument_sampler_interval_ms",
	"background_job_wait_seconds",
	# Display filters
	"min_action_duration_ms",
	"large_duration_threshold_ms",
	# Analyzer thresholds (detection — the original nine)
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

# Named Sensitivity Profiles — the single source of truth for preset numbers
# (the DocType JS reads this via the get_config_profiles API; tests assert
# Recommended == _DEFAULTS). "Recommended" mirrors the shipped _DEFAULTS, so a
# user on Recommended automatically tracks any future retune. "Strict" catches
# more (lower count/ms thresholds, lower %); "Relaxed" catches less. All values
# respect the controller's _NUMERIC_FLOORS. "Custom" is intentionally absent —
# _resolve keys off ``_PROFILES.get(profile)`` so Custom falls through to the
# stored-value logic. Build Recommended from _DEFAULTS to prevent drift.
#
# Numbers below mirror the "Reference values — Strict: X · Recommended: Y ·
# Relaxed: Z" triplet baked into each field's DocType description, so the
# operator-facing form text and the resolve-path behaviour stay in sync.
# ``test_profiles_match_doctype_reference_values`` regression-guards the
# pair.
_PROFILES = {
	"Strict": {
		# General + capture + display + Phase-2 + AI extras (v0.13.x)
		"session_retention_days": 90,
		"max_queries_per_recording": 5000,
		"pyinstrument_sampler_interval_ms": 0.5,
		"background_job_wait_seconds": 300,
		"min_action_duration_ms": 0.0,
		"large_duration_threshold_ms": 500.0,
		"phase2_max_runs_per_session": 25,
		"auto_expand_max_depth": 15,
		"auto_expand_min_ms": 25.0,
		"ai_auto_suggest_max": 10,
		# Analyzer thresholds (the original nine)
		"redundant_doc_threshold": 3,
		"redundant_cache_threshold": 20,
		"redundant_perm_threshold": 5,
		"n_plus_one_min_occurrences": 5,
		"slow_query_threshold_ms": 100.0,
		"slow_hot_path_pct_threshold": 15.0,
		"slow_hot_path_min_ms": 100.0,
		"hot_line_high_pct": 35.0,
		"hot_line_high_min_ms": 50.0,
	},
	"Recommended": {key: _DEFAULTS[key] for key in _SENSITIVITY_KEYS},
	"Relaxed": {
		# General + capture + display + Phase-2 + AI extras (v0.13.x)
		"session_retention_days": 7,
		"max_queries_per_recording": 1000,
		"pyinstrument_sampler_interval_ms": 5.0,
		"background_job_wait_seconds": 60,
		"min_action_duration_ms": 50.0,
		"large_duration_threshold_ms": 99999999.0,
		"phase2_max_runs_per_session": 5,
		"auto_expand_max_depth": 5,
		"auto_expand_min_ms": 100.0,
		"ai_auto_suggest_max": 3,
		# Analyzer thresholds (the original nine)
		"redundant_doc_threshold": 10,
		"redundant_cache_threshold": 100,
		"redundant_perm_threshold": 25,
		"n_plus_one_min_occurrences": 25,
		"slow_query_threshold_ms": 500.0,
		"slow_hot_path_pct_threshold": 40.0,
		"slow_hot_path_min_ms": 500.0,
		"hot_line_high_pct": 70.0,
		"hot_line_high_min_ms": 250.0,
	},
}

# Keys we also accept from site_config.json for backwards compatibility
# with the pre-v0.5.2 pattern of tuning thresholds without a DocType.
# The DocType wins if both are set.
_SITE_CONFIG_FALLBACK = {
	"redundant_doc_threshold": "optimus_redundant_doc_threshold",
	"redundant_cache_threshold": "optimus_redundant_cache_threshold",
	"redundant_perm_threshold": "optimus_redundant_perm_threshold",
	"n_plus_one_min_occurrences": "optimus_n_plus_one_threshold",
}


@dataclass(frozen=True)
class OptimusConfig:
	"""Snapshot of resolved profiler configuration.

	Frozen so a single instance can be safely cached and handed to
	multiple analyzers without copy-on-read concerns.
	"""

	enabled: bool = True
	session_retention_days: int = 30
	tracked_apps: tuple[str, ...] = field(default_factory=tuple)
	# v0.6.x: drop findings whose blame app is in this tuple (both sections).
	# v0.13.x: dataclass default mirrors ``_DEFAULTS`` — fresh installs
	# get every Frappe-organization-maintained app seeded into the DocType,
	# and the no-bench / pre-migrate fallback path returns the same tuple
	# so pure-Python unit tests see the same behaviour as the bench.
	ignored_apps: tuple[str, ...] = field(
		default_factory=lambda: (
			"builder",
			"crm",
			"drive",
			"erpnext",
			"frappe",
			"helpdesk",
			"hrms",
			"insights",
			"lms",
			"payments",
			"wiki",
		)
	)
	# v0.6.x: drop framework/internal db tables from the "Time spent per
	# database table" section. Default on.
	hide_framework_tables: bool = True
	max_queries_per_recording: int = 2000
	redundant_doc_threshold: int = 5
	# v0.5.2 round 4: bumped to 50 alongside ``_DEFAULTS["redundant_cache_threshold"]``.
	# The dataclass default is the fallback ``get_config()`` returns when
	# Frappe isn't importable (unit-test path / pre-bench-init); keeping
	# it in sync with ``_DEFAULTS`` avoids a silent two-defaults drift
	# that masked low-count cache loops in pure-Python tests.
	redundant_cache_threshold: int = 50
	redundant_perm_threshold: int = 10
	n_plus_one_min_occurrences: int = 10
	# v0.6.0 Round 6: severity tuning + capture / phase-2 / skip-rule
	# knobs that used to be hardcoded constants in their consumers.
	slow_query_threshold_ms: float = 200.0
	slow_hot_path_pct_threshold: float = 25.0
	slow_hot_path_min_ms: float = 200.0
	hot_line_high_pct: float = 50.0
	hot_line_high_min_ms: float = 100.0
	pyinstrument_sampler_interval_ms: float = 1.0
	min_action_duration_ms: float = 0.0
	# v0.6.x: durations >= this threshold render as seconds in the report;
	# below the threshold, render as ms. Falsy → use _DEFAULTS via _float.
	large_duration_threshold_ms: float = 1000.0
	phase2_max_runs_per_session: int = 10
	phase2_default_auto_expand: bool = True
	background_job_wait_seconds: int = 300
	auto_expand_max_depth: int = 10
	auto_expand_min_ms: float = 50.0
	# Tuples (immutable, hashable, safe to cache). Reader parses the
	# Small Text fields by splitting on newlines and dropping comments.
	skip_request_paths: tuple[str, ...] = field(default_factory=tuple)
	skip_users: tuple[str, ...] = field(default_factory=tuple)
	# v0.7.x+ — additive lists for capture-time redaction. Defaults
	# carry the 12 canonical patterns inside optimus.redaction; these
	# extend them. Never replace — a config typo can't disable
	# redaction of a known-sensitive key.
	sensitive_sql_columns: tuple[str, ...] = field(default_factory=tuple)
	sensitive_form_keys: tuple[str, ...] = field(default_factory=tuple)
	# v0.6.0: AI "suggest a fix" config. Non-secret only — the API key is
	# never cached here (see _DEFAULTS note + ai_fix._resolve_provider).
	ai_enabled: bool = False
	ai_provider: str = "Anthropic"
	ai_base_url: str = ""
	ai_model: str = ""
	ai_auto_suggest: bool = False
	ai_auto_suggest_max: int = 5
	ai_humanize_steps: bool = True
	# v0.6.x: per-section "use the LLM for X" toggles (hard off).
	ai_suggest_findings: bool = True
	ai_suggest_indexes: bool = True
	# v0.7.x: Sensitivity Profile name. "Custom" → the threshold fields above
	# carry the authoritative values; a named preset (Strict/Recommended/
	# Relaxed) overrides the nine _SENSITIVITY_KEYS at resolve time. Defaults to
	# "Custom" so the no-frappe / pre-bench path uses the dataclass threshold
	# defaults (= Recommended numbers) as-is.
	config_profile: str = "Custom"
	# v0.9.0: AI privacy hardening (Critical Risk #2). Exclusion list is a
	# tuple (immutable, hashable, safe to cache) of finding-type names.
	# Timeout default matches the pre-v0.9.0 hardcoded constant.
	ai_excluded_finding_types: tuple[str, ...] = field(default_factory=tuple)
	ai_request_timeout_seconds: int = 60


# v0.12.0: key centralized in optimus.redis_keys. The module-level
# constant alias is kept so existing internal references resolve without
# touching every call site.
def _settings_cache_key() -> str:
	from optimus import redis_keys

	return redis_keys.settings_cache()


_CACHE_KEY = _settings_cache_key()


def _read_doctype_row() -> dict | None:
	"""Load the Single doc's field dict, or None if the DocType doesn't
	exist yet (fresh install / pre-migration).

	We use ``get_single_value`` per-field instead of ``get_single`` so
	we can degrade cleanly when ``Optimus Settings`` isn't yet in the
	schema — some deployments install the app but haven't migrated.
	"""
	import frappe
	try:
		# Short-circuit: if the DocType row doesn't exist, fall back
		# to defaults instead of raising from inside get_single_value.
		if not frappe.db.exists("DocType", "Optimus Settings"):
			return None
	except Exception:
		# frappe.db unavailable (e.g. schema still loading) — defaults.
		return None

	try:
		doc = frappe.get_cached_doc("Optimus Settings")
	except Exception:
		return None

	return {
		"enabled": bool(doc.get("enabled", 1)),
		"session_retention_days": int(doc.get("session_retention_days") or 30),
		"tracked_apps": tuple(
			(row.app_name or "").strip()
			for row in (doc.get("tracked_apps") or [])
			if (row.app_name or "").strip()
		),
		"ignored_apps": tuple(
			(row.app_name or "").strip()
			for row in (doc.get("ignored_apps") or [])
			if (row.app_name or "").strip()
		),
		"hide_framework_tables": bool(doc.get("hide_framework_tables", 1)),
		"max_queries_per_recording": int(doc.get("max_queries_per_recording") or 0) or None,
		"redundant_doc_threshold": int(doc.get("redundant_doc_threshold") or 0) or None,
		"redundant_cache_threshold": int(doc.get("redundant_cache_threshold") or 0) or None,
		"redundant_perm_threshold": int(doc.get("redundant_perm_threshold") or 0) or None,
		"n_plus_one_min_occurrences": int(doc.get("n_plus_one_min_occurrences") or 0) or None,
		# v0.6.0 Round 6 fields. Floats use ``or None`` so 0/0.0/unset
		# all fall through to the default rather than silently zeroing
		# out the threshold.
		"slow_query_threshold_ms": float(doc.get("slow_query_threshold_ms") or 0) or None,
		"slow_hot_path_pct_threshold": float(doc.get("slow_hot_path_pct_threshold") or 0) or None,
		"slow_hot_path_min_ms": float(doc.get("slow_hot_path_min_ms") or 0) or None,
		"hot_line_high_pct": float(doc.get("hot_line_high_pct") or 0) or None,
		"hot_line_high_min_ms": float(doc.get("hot_line_high_min_ms") or 0) or None,
		"pyinstrument_sampler_interval_ms": float(
			doc.get("pyinstrument_sampler_interval_ms") or 0
		) or None,
		# min_action_duration_ms intentionally allows 0 (= show all
		# actions, the default). Coerce, don't fall through.
		"min_action_duration_ms": float(doc.get("min_action_duration_ms") or 0),
		# Falsy (0/None/missing) → None so _float falls through to
		# _DEFAULTS["large_duration_threshold_ms"] = 1000.
		"large_duration_threshold_ms": (
			float(doc.get("large_duration_threshold_ms"))
			if doc.get("large_duration_threshold_ms") else None
		),
		"phase2_max_runs_per_session": int(doc.get("phase2_max_runs_per_session") or 0) or None,
		# 0 is legitimate (= don't wait) — don't fall through to the default.
		"background_job_wait_seconds": int(
			doc.get("background_job_wait_seconds", _DEFAULTS["background_job_wait_seconds"]) or 0
		),
		# Phase-2 default auto-expand is a Check; bool() handles the
		# 1/0 from Frappe's storage. We can't use ``or None`` here
		# because False is a legitimate value.
		"phase2_default_auto_expand": bool(doc.get("phase2_default_auto_expand", 1)),
		"auto_expand_max_depth": int(doc.get("auto_expand_max_depth") or 0) or None,
		"auto_expand_min_ms": float(doc.get("auto_expand_min_ms") or 0) or None,
		"skip_request_paths": _parse_skip_list(doc.get("skip_request_paths")),
		"skip_users": _parse_skip_list(doc.get("skip_users")),
		"sensitive_sql_columns": _parse_skip_list(doc.get("sensitive_sql_columns")),
		"sensitive_form_keys": _parse_skip_list(doc.get("sensitive_form_keys")),
		# v0.6.0 AI fix config (non-secret). ``ai_enabled`` /
		# ``ai_auto_suggest`` are Checks — can't use ``or None`` because
		# False is legitimate. ``ai_auto_suggest_max`` allows 0 (= all).
		"ai_enabled": bool(doc.get("ai_enabled")),
		"ai_provider": (doc.get("ai_provider") or "").strip() or None,
		"ai_base_url": (doc.get("ai_base_url") or "").strip() or None,
		"ai_model": (doc.get("ai_model") or "").strip() or None,
		"ai_auto_suggest": bool(doc.get("ai_auto_suggest")),
		"ai_auto_suggest_max": int(doc.get("ai_auto_suggest_max") or 0),
		# Default-on (when AI is enabled) — pass a default to .get() so a
		# Single row predating this field still reads as True.
		"ai_humanize_steps": bool(doc.get("ai_humanize_steps", 1)),
		"ai_suggest_findings": bool(doc.get("ai_suggest_findings", 1)),
		"ai_suggest_indexes": bool(doc.get("ai_suggest_indexes", 1)),
		# v0.7.x: Sensitivity Profile. Empty / missing (a pre-profile Single
		# that predates the field, or an unsaved fresh Single) coalesces to
		# "Custom" so existing stored thresholds keep driving analysis — no
		# migration patch, no silent reset to Recommended.
		"config_profile": (doc.get("config_profile") or "Custom"),
		# v0.9.0: AI privacy. Exclusion list parsed with the same skip-list
		# semantics as skip_request_paths / sensitive_sql_columns (line-per-
		# entry, # comments, blanks stripped). Timeout: 0/None falls through
		# to _DEFAULTS via _int_with_default.
		"ai_excluded_finding_types": _parse_skip_list(doc.get("ai_excluded_finding_types")),
		"ai_request_timeout_seconds": int(doc.get("ai_request_timeout_seconds") or 0) or None,
	}


def _parse_skip_list(raw: str | None) -> tuple[str, ...]:
	"""Parse a Small Text field as one entry per line. Strips trailing
	whitespace and drops blank lines + lines starting with '#' so users
	can comment their skip lists.
	"""
	if not raw:
		return ()
	out: list[str] = []
	for line in str(raw).splitlines():
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			continue
		out.append(stripped)
	return tuple(out)


def _site_conf_fallback(key: str) -> int | None:
	"""Return the site_config.json override for a threshold, if set."""
	conf_key = _SITE_CONFIG_FALLBACK.get(key)
	if not conf_key:
		return None
	import frappe
	try:
		v = frappe.conf.get(conf_key)
		if v is None:
			return None
		return int(v)
	except (TypeError, ValueError, AttributeError):
		return None


def _resolve() -> OptimusConfig:
	"""Build a fresh config snapshot from DocType + site_config + defaults.

	Precedence: DocType row > site_config.json > hardcoded default.
	"""
	row = _read_doctype_row() or {}

	def _threshold(key: str) -> int:
		# DocType wins if non-zero.
		v = row.get(key)
		if v:
			return int(v)
		# Fallback to site_config.json.
		sc = _site_conf_fallback(key)
		if sc is not None:
			return sc
		# Hardcoded default.
		return int(_DEFAULTS[key])

	def _float(key: str) -> float:
		v = row.get(key)
		if v:
			return float(v)
		return float(_DEFAULTS[key])

	def _int_with_default(key: str) -> int:
		v = row.get(key)
		if v:
			return int(v)
		return int(_DEFAULTS[key])

	# v0.7.x: Sensitivity Profile. A named preset (Strict/Recommended/Relaxed)
	# is authoritative for the nine _SENSITIVITY_KEYS — it overrides both the
	# stored field value and the site_config fallback. "Custom" (or any
	# unknown/absent value) → preset is None → fall through to the existing
	# per-field precedence (DocType row > site_config > default).
	profile = row.get("config_profile") or "Custom"
	preset = _PROFILES.get(profile)

	def _sens_int(key: str) -> int:
		if preset is not None:
			return int(preset[key])
		return _threshold(key)

	def _sens_float(key: str) -> float:
		if preset is not None:
			return float(preset[key])
		return _float(key)

	# v0.13.x: zero-allowed sensitivity fields use this helper instead of
	# the generic ``_sens_int`` so an operator who wrote 0 in the form
	# (under Custom) gets 0 — not the _DEFAULTS fallback the truthy-check
	# in ``_threshold`` would trigger.
	def _sens_int_zero_ok(key: str) -> int:
		if preset is not None:
			return int(preset[key])
		v = row.get(key)
		if v is not None:
			return int(v)
		return int(_DEFAULTS[key])

	def _sens_float_zero_ok(key: str) -> float:
		if preset is not None:
			return float(preset[key])
		v = row.get(key)
		if v is not None:
			return float(v)
		return float(_DEFAULTS[key])

	return OptimusConfig(
		enabled=bool(row.get("enabled", _DEFAULTS["enabled"])),
		# v0.13.x: profile-aware. Was ``_threshold``-style truthy-check; the
		# preset wins under Strict/Recommended/Relaxed.
		session_retention_days=_sens_int("session_retention_days"),
		tracked_apps=tuple(row.get("tracked_apps") or ()),
		# v0.13.x: fall through to ``_DEFAULTS["ignored_apps"]`` (frappe +
		# erpnext) when the DocType row hasn't populated this field —
		# i.e. when ``_read_doctype_row`` returned ``None`` (fresh install
		# / pre-migrate). On a real bench the install hook seeds the
		# rows themselves, so the row read returns the configured tuple
		# and this fallback is bypassed; the no-bench / pure-pytest path
		# is what depends on this default.
		ignored_apps=tuple(
			row.get("ignored_apps") or _DEFAULTS["ignored_apps"]
		),
		hide_framework_tables=bool(
			row.get("hide_framework_tables")
			if "hide_framework_tables" in row
			else _DEFAULTS["hide_framework_tables"]
		),
		# v0.13.x: now profile-aware (was ``_threshold`` only).
		max_queries_per_recording=_sens_int("max_queries_per_recording"),
		# Original nine detection-sensitivity knobs — profile-aware
		# (see _sens_* above). Still here for clarity.
		redundant_doc_threshold=_sens_int("redundant_doc_threshold"),
		redundant_cache_threshold=_sens_int("redundant_cache_threshold"),
		redundant_perm_threshold=_sens_int("redundant_perm_threshold"),
		n_plus_one_min_occurrences=_sens_int("n_plus_one_min_occurrences"),
		slow_query_threshold_ms=_sens_float("slow_query_threshold_ms"),
		slow_hot_path_pct_threshold=_sens_float("slow_hot_path_pct_threshold"),
		slow_hot_path_min_ms=_sens_float("slow_hot_path_min_ms"),
		hot_line_high_pct=_sens_float("hot_line_high_pct"),
		hot_line_high_min_ms=_sens_float("hot_line_high_min_ms"),
		# v0.13.x: profile-aware (was ``_float``).
		pyinstrument_sampler_interval_ms=_sens_float("pyinstrument_sampler_interval_ms"),
		# v0.13.x: profile-aware. ``min_action_duration_ms`` allows 0 as a
		# legitimate "show everything" sentinel, so we use the
		# zero-OK variant — under Custom, a stored 0 doesn't fall
		# through to the default.
		min_action_duration_ms=_sens_float_zero_ok("min_action_duration_ms"),
		# v0.13.x: profile-aware (was ``_float``).
		large_duration_threshold_ms=_sens_float("large_duration_threshold_ms"),
		# v0.13.x: profile-aware (was ``_int_with_default``).
		phase2_max_runs_per_session=_sens_int("phase2_max_runs_per_session"),
		phase2_default_auto_expand=bool(
			row.get("phase2_default_auto_expand")
			if "phase2_default_auto_expand" in row
			else _DEFAULTS["phase2_default_auto_expand"]
		),
		# v0.13.x: profile-aware. 0 = don't wait (sentinel); clamp to
		# [0, 300] (hard ceiling regardless of profile choice). Under
		# Custom, an operator's stored 0 is preserved via the zero-OK
		# variant.
		background_job_wait_seconds=max(
			0, min(300, _sens_int_zero_ok("background_job_wait_seconds"))
		),
		# v0.13.x: profile-aware (was ``_int_with_default`` / ``_float``).
		auto_expand_max_depth=_sens_int("auto_expand_max_depth"),
		auto_expand_min_ms=_sens_float("auto_expand_min_ms"),
		skip_request_paths=tuple(row.get("skip_request_paths") or ()),
		skip_users=tuple(row.get("skip_users") or ()),
		sensitive_sql_columns=tuple(row.get("sensitive_sql_columns") or ()),
		sensitive_form_keys=tuple(row.get("sensitive_form_keys") or ()),
		ai_enabled=bool(
			row.get("ai_enabled")
			if "ai_enabled" in row
			else _DEFAULTS["ai_enabled"]
		),
		ai_provider=row.get("ai_provider") or _DEFAULTS["ai_provider"],
		ai_base_url=row.get("ai_base_url") or _DEFAULTS["ai_base_url"],
		ai_model=row.get("ai_model") or _DEFAULTS["ai_model"],
		ai_auto_suggest=bool(
			row.get("ai_auto_suggest")
			if "ai_auto_suggest" in row
			else _DEFAULTS["ai_auto_suggest"]
		),
		# v0.13.x: profile-aware. Allows 0 (= every eligible finding) —
		# the zero-OK variant keeps a stored 0 under Custom.
		ai_auto_suggest_max=_sens_int_zero_ok("ai_auto_suggest_max"),
		ai_humanize_steps=bool(
			row.get("ai_humanize_steps")
			if "ai_humanize_steps" in row
			else _DEFAULTS["ai_humanize_steps"]
		),
		ai_suggest_findings=bool(
			row.get("ai_suggest_findings")
			if "ai_suggest_findings" in row
			else _DEFAULTS["ai_suggest_findings"]
		),
		ai_suggest_indexes=bool(
			row.get("ai_suggest_indexes")
			if "ai_suggest_indexes" in row
			else _DEFAULTS["ai_suggest_indexes"]
		),
		config_profile=profile,
		# v0.9.0: AI privacy. Tuple straight through (already parsed in
		# _read_doctype_row). Timeout clamped to [10, 600] — below 10s
		# breaks the LLM round-trip; above 600s holds the analyze worker
		# longer than the time budget is willing to tolerate anyway.
		ai_excluded_finding_types=tuple(row.get("ai_excluded_finding_types") or ()),
		ai_request_timeout_seconds=max(10, min(600, _int_with_default("ai_request_timeout_seconds"))),
	)


def get_config() -> OptimusConfig:
	"""Return the resolved config, cached in Redis until the Single is
	saved (controller's on_update deletes the cache key).

	Fails soft — on ANY exception during lookup (including Frappe not
	being importable in unit-test contexts), returns the hardcoded
	defaults. The profiler must never crash a request because of a
	settings read, especially on bench startup before Redis is warm.
	"""
	try:
		import frappe
	except ImportError:
		# Unit-test path — no bench context.
		return OptimusConfig()

	try:
		# v0.12.11: ``settings_cache`` is the first value to migrate to the
		# v0.12.0 versioned envelope. ``unwrap_value`` returns ``(payload,
		# version)``; the payload is the OptimusConfig field dict in either
		# the new-shape envelope (``{"_v": 1, "data": {...}}``) or the
		# legacy bare-dict shape (pre-v0.12.11 writes still flow through
		# unchanged via the legacy-detection branch). On a schema-version
		# bump WITHOUT a migration, ``unwrap_value`` returns ``(default=
		# None, observed_version)`` — the request falls through to the slow
		# path (``_resolve``) and re-writes a fresh envelope.
		from optimus import redis_schema

		cached_raw = frappe.cache.get_value(_CACHE_KEY)
		payload, _version = redis_schema.unwrap_value(cached_raw)
		if isinstance(payload, dict) and payload:
			return OptimusConfig(**payload)
	except Exception:
		pass

	try:
		cfg = _resolve()
	except Exception:
		return OptimusConfig()

	try:
		from optimus import redis_schema

		frappe.cache.set_value(
			_CACHE_KEY, redis_schema.wrap_value(cfg.__dict__)
		)
	except Exception:
		pass

	return cfg


def is_enabled() -> bool:
	"""Convenience wrapper — hot-path entry point from hooks_callbacks."""
	try:
		return get_config().enabled
	except Exception:
		# Fail open: if we can't read the setting, don't silently
		# disable the profiler — that would be a very confusing
		# support issue ("why isn't recording working"). Default to
		# on, matching the DocType default.
		return True


def get_tracked_apps() -> tuple[str, ...]:
	"""Allowlist of user apps. Empty tuple → no override (use the
	built-in FRAMEWORK_APPS exclusion list).

	Called by ``is_framework_callsite`` to flip the classifier from
	exclusion-mode (framework = frappe/erpnext/…) to inclusion-mode
	(user code = exactly the tracked apps).
	"""
	try:
		return get_config().tracked_apps
	except Exception:
		return ()


def get_ignored_apps() -> tuple[str, ...]:
	"""v0.6.x: exclusion list — apps whose findings are dropped from the
	report entirely (both ``Findings — what to fix`` and ``Framework-level
	observations``). Empty tuple → no findings dropped."""
	try:
		return get_config().ignored_apps
	except Exception:
		return ()
