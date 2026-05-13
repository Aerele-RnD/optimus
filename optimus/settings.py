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
	# v0.6.x: exclusion list — findings whose blame app falls in this tuple
	# are dropped from the report (both Findings and Observations sections).
	# Default empty (nothing dropped). Typical use: ("frappe", "optimus").
	"ignored_apps": (),
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
	"background_job_wait_seconds": 60,
	"auto_expand_max_depth": 10,
	"auto_expand_min_ms": 50.0,
	"skip_request_paths": (),  # tuple of stripped, comment-free lines
	"skip_users": (),
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
	ignored_apps: tuple[str, ...] = field(default_factory=tuple)
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
	background_job_wait_seconds: int = 60
	auto_expand_max_depth: int = 10
	auto_expand_min_ms: float = 50.0
	# Tuples (immutable, hashable, safe to cache). Reader parses the
	# Small Text fields by splitting on newlines and dropping comments.
	skip_request_paths: tuple[str, ...] = field(default_factory=tuple)
	skip_users: tuple[str, ...] = field(default_factory=tuple)
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


_CACHE_KEY = "optimus_settings_cached"


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

	return OptimusConfig(
		enabled=bool(row.get("enabled", _DEFAULTS["enabled"])),
		session_retention_days=int(
			row.get("session_retention_days") or _DEFAULTS["session_retention_days"]
		),
		tracked_apps=tuple(row.get("tracked_apps") or ()),
		ignored_apps=tuple(row.get("ignored_apps") or ()),
		hide_framework_tables=bool(
			row.get("hide_framework_tables")
			if "hide_framework_tables" in row
			else _DEFAULTS["hide_framework_tables"]
		),
		max_queries_per_recording=_threshold("max_queries_per_recording"),
		redundant_doc_threshold=_threshold("redundant_doc_threshold"),
		redundant_cache_threshold=_threshold("redundant_cache_threshold"),
		redundant_perm_threshold=_threshold("redundant_perm_threshold"),
		n_plus_one_min_occurrences=_threshold("n_plus_one_min_occurrences"),
		slow_query_threshold_ms=_float("slow_query_threshold_ms"),
		slow_hot_path_pct_threshold=_float("slow_hot_path_pct_threshold"),
		slow_hot_path_min_ms=_float("slow_hot_path_min_ms"),
		hot_line_high_pct=_float("hot_line_high_pct"),
		hot_line_high_min_ms=_float("hot_line_high_min_ms"),
		pyinstrument_sampler_interval_ms=_float("pyinstrument_sampler_interval_ms"),
		# min_action_duration_ms allows 0 — read directly without the
		# zero-falls-through helper.
		min_action_duration_ms=float(
			row.get("min_action_duration_ms")
			if row.get("min_action_duration_ms") is not None
			else _DEFAULTS["min_action_duration_ms"]
		),
		large_duration_threshold_ms=_float("large_duration_threshold_ms"),
		phase2_max_runs_per_session=_int_with_default("phase2_max_runs_per_session"),
		phase2_default_auto_expand=bool(
			row.get("phase2_default_auto_expand")
			if "phase2_default_auto_expand" in row
			else _DEFAULTS["phase2_default_auto_expand"]
		),
		# 0 = don't wait; clamp to [0, 300] (300 = hard ceiling regardless of config).
		background_job_wait_seconds=max(0, min(300, int(
			row.get("background_job_wait_seconds")
			if row.get("background_job_wait_seconds") is not None
			else _DEFAULTS["background_job_wait_seconds"]
		))),
		auto_expand_max_depth=_int_with_default("auto_expand_max_depth"),
		auto_expand_min_ms=_float("auto_expand_min_ms"),
		skip_request_paths=tuple(row.get("skip_request_paths") or ()),
		skip_users=tuple(row.get("skip_users") or ()),
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
		# Allows 0 (= every eligible finding) — read directly, no
		# zero-falls-through helper.
		ai_auto_suggest_max=int(
			row.get("ai_auto_suggest_max")
			if row.get("ai_auto_suggest_max") is not None
			else _DEFAULTS["ai_auto_suggest_max"]
		),
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
		cached = frappe.cache.get_value(_CACHE_KEY)
		if cached is not None:
			return OptimusConfig(**cached)
	except Exception:
		pass

	try:
		cfg = _resolve()
	except Exception:
		return OptimusConfig()

	try:
		frappe.cache.set_value(_CACHE_KEY, cfg.__dict__)
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
