# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class OptimusSettings(Document):
	"""Site-wide configuration for Optimus.

	A Single DocType. The cached reader in ``optimus.settings``
	is what analyzers and hooks actually call — this controller only
	handles validation + cache invalidation when an admin saves.
	"""

	def validate(self):
		self._normalize_tracked_apps()
		self._clamp_numeric_floors()
		self._warn_on_framework_apps_in_tracked()
		self._warn_on_incomplete_ai_config()

	def on_update(self):
		# Settings are read on every request (via the `enabled` gate in
		# hooks_callbacks), so the cache version bumps on every save.
		# The settings module's reader respects the cache version.
		from optimus import redis_keys

		frappe.cache.delete_value(redis_keys.settings_cache())

	# Numeric floors per field — a value below the floor would either
	# break the analyzer at runtime (negative interval, zero retention)
	# or render a useless report (sub-microsecond thresholds). Floors
	# of 0 mean "0 is OK as a 'no filter' / 'always flag' sentinel".
	_NUMERIC_FLOORS = {
		# v0.13.x: floor lowered 1 → 0 to admit the Strict-as-unlimited
		# preset. ``_sweep_old_sessions`` in janitor.py treats <= 0 as
		# "never sweep" (forever retention), so 0 is now a meaningful
		# sentinel rather than a fatal typo. Pre-v0.13.x the janitor
		# used ``or DEFAULT_RETENTION_DAYS`` and silently fell back to
		# 90 on 0.
		"session_retention_days": 0,
		# v0.13.x: floor lowered 1 → 0. analyze.py's enrich loop now
		# treats cap == 0 as "no cap" (enrich every query) instead of
		# falling back to MAX_QUERIES_ENRICHED_PER_RECORDING.
		"max_queries_per_recording": 0,
		"pyinstrument_sampler_interval_ms": 0.1,
		"min_action_duration_ms": 0,
		"large_duration_threshold_ms": 0,
		"background_job_wait_seconds": 0,
		"slow_query_threshold_ms": 1,
		"slow_hot_path_pct_threshold": 0,
		"slow_hot_path_min_ms": 0,
		"hot_line_high_pct": 0,
		"hot_line_high_min_ms": 0,
		"redundant_doc_threshold": 1,
		"redundant_cache_threshold": 1,
		"redundant_perm_threshold": 1,
		"n_plus_one_min_occurrences": 1,
		"ai_auto_suggest_max": 0,
		# v0.9.0: AI request timeout. Below 10s breaks the LLM round-trip
		# entirely; the ceiling 600s is applied in settings.py:_resolve
		# (we can't enforce it from a floor). Clamping below pairs with
		# the doc's "start at 180 for local LLMs" guidance.
		"ai_request_timeout_seconds": 10,
	}

	def _clamp_numeric_floors(self):
		"""Floor each numeric setting at a safe minimum so a typo
		(e.g. ``-1`` for a sampler interval, ``0`` for session
		retention) doesn't break the analyzer at runtime. Silently
		clamps — the operator sees the corrected value on the form
		after save."""
		for fieldname, floor in self._NUMERIC_FLOORS.items():
			current = self.get(fieldname)
			if current is None:
				continue
			try:
				if float(current) < float(floor):
					# setattr instead of self.set so the helper works
					# against a test stub that doesn't subclass Frappe's
					# full Document. setattr matches Document.set's
					# behaviour for non-child-table scalar fields.
					setattr(self, fieldname, floor)
			except (TypeError, ValueError):
				# Non-numeric input — let Frappe's field-type validation
				# handle it; nothing useful for us to do here.
				continue

	def _normalize_tracked_apps(self):
		"""Trim whitespace and deduplicate app names, preserving order.

		Saves the admin from pasting ``myapp`` and ``myapp `` (trailing
		space) and getting two rows that both fail to match.
		"""
		if not self.tracked_apps:
			return
		seen = set()
		normalized = []
		for row in self.tracked_apps:
			name = (row.app_name or "").strip()
			if not name or name in seen:
				continue
			seen.add(name)
			row.app_name = name
			normalized.append(row)
		# Rebuild the child-table list preserving order.
		self.tracked_apps = normalized

	def _warn_on_framework_apps_in_tracked(self):
		"""Flash a non-blocking warning when the admin adds a
		known-framework app (frappe / erpnext / hrms / …) to Tracked
		Apps.

		Most users misread "Tracked Apps" as "apps to monitor" and
		add frappe + erpnext — which has the OPPOSITE effect of what
		they want: it flips the classifier into inclusion mode where
		framework code becomes "user code", and their actionable
		findings list gets flooded with framework noise.

		We don't HARD-block the save (ERPNext contributors may
		legitimately want framework findings as actionable) — just
		flash a clear warning so the common misconfiguration surfaces
		itself.
		"""
		if not self.tracked_apps:
			return
		# Local import to avoid a top-level dependency on analyzers/.
		from optimus.analyzers.base import FRAMEWORK_APPS
		offenders = sorted({
			(row.app_name or "").strip()
			for row in self.tracked_apps
			if (row.app_name or "").strip() in FRAMEWORK_APPS
			and (row.app_name or "").strip() != "optimus"
		})
		if not offenders:
			return
		msg = (
			"<b>Heads up:</b> you added "
			+ ", ".join(f"<code>{a}</code>" for a in offenders)
			+ " to Tracked Apps. These are framework/first-party apps — "
			"adding them here flips the filter into <i>inclusion mode</i>, "
			"so their findings will now show up as <b>actionable</b> "
			"instead of in the collapsed Framework observations section. "
			"<br><br>"
			"If you want the default behavior (frappe + erpnext + stock "
			"apps treated as framework), <b>remove these rows and leave "
			"the table empty</b>. Only add your own custom app here if "
			"you want to narrow the actionable list to just that app."
		)
		frappe.msgprint(
			msg,
			title="Tracked Apps — possible misconfiguration",
			indicator="orange",
		)

	def _warn_on_incomplete_ai_config(self):
		"""Non-blocking warning when AI fix suggestions are enabled but
		the config is incomplete (no model, or no API key for a provider
		that needs one). The feature stays enabled — the operator just
		sees a clear hint instead of a cryptic error on first use."""
		if not self.get("ai_enabled"):
			return
		provider = (self.get("ai_provider") or "Anthropic").strip()
		needs_key = provider != "OpenAI-compatible"
		# ai_model can be blank when a hosted default exists for the
		# provider; only "OpenAI-compatible" truly requires it (no
		# default to fall back to). We still nudge if it's blank for the
		# custom provider.
		missing = []
		if provider == "OpenAI-compatible":
			if not (self.get("ai_base_url") or "").strip():
				missing.append("Base URL")
			if not (self.get("ai_model") or "").strip():
				missing.append("Model")
		if needs_key and not (self.get("ai_api_key") or "").strip():
			missing.append("API Key")
		if not missing:
			return
		frappe.msgprint(
			"AI Fix Suggestions are enabled but " + ", ".join(missing)
			+ (" is" if len(missing) == 1 else " are")
			+ " not set — the <b>Suggest a fix (AI)</b> button will report a "
			"configuration error until you fill these in.",
			title="AI Fix Suggestions — incomplete config",
			indicator="orange",
		)
