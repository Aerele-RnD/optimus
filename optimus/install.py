# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Install / uninstall hooks for optimus.

Currently the only install-time action is creating the `Optimus User`
role used by Phase 1's permission model. Customers grant this role to
non-admin users who should be able to record their own profiling sessions.
"""

import frappe

from optimus import safe_commit

PROFILER_USER_ROLE = "Optimus User"


def _refuse_legacy_install():
	"""Phase K hardening: refuse to install on top of the legacy
	``frappe_profiler`` module. v0.7.0 is fresh-deploy-only per the
	CHANGELOG; silently coexisting with the old module would leave
	stale ``Profiler Session`` rows + duplicate DocType definitions
	in an unpredictable hybrid state.
	"""
	try:
		has_module = bool(frappe.db.exists("Module Def", "Frappe Profiler"))
	except Exception:
		has_module = False
	try:
		has_doctype = bool(frappe.db.exists("DocType", "Profiler Session"))
	except Exception:
		has_doctype = False
	if has_module or has_doctype:
		frappe.throw(
			"Optimus 0.7.x is a fresh-deploy-only release and cannot run "
			"alongside the legacy ``frappe_profiler`` install. Uninstall "
			"the old app first: ``bench --site <site> uninstall-app frappe_profiler``, "
			"then re-run ``bench --site <site> install-app optimus``. "
			"See CHANGELOG.md for migration notes.",
			frappe.ValidationError,
		)


def after_install():
	"""Create the Optimus User role and auto-assign it to System Managers."""
	_refuse_legacy_install()
	if not frappe.db.exists("Role", PROFILER_USER_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": PROFILER_USER_ROLE,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)
		safe_commit()

	# v0.4.0: auto-assign the Optimus User role to every existing
	# System Manager. Idempotent — users who already have it are skipped.
	# Wrapped in try/except so a failure can't abort the install.
	try:
		_assign_profiler_user_to_system_managers()
	except Exception as exc:
		try:
			frappe.log_error(title="optimus after_install auto-role")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure("install.after_install.auto_role", exc)
		except Exception:
			pass

	# v0.5.2 round 3: pre-populate Optimus Settings ▸ Tracked Apps
	# with the site's custom apps (anything not in the built-in
	# FRAMEWORK_APPS set). Admin gets a sensible default inclusion-
	# mode allowlist on day one — they don't have to remember that
	# Tracked Apps exists for the framework filter to actually match
	# their mental model of "user code".
	try:
		_seed_tracked_apps_from_installed_apps()
	except Exception as exc:
		try:
			frappe.log_error(title="optimus after_install tracked-apps seed")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure("install.after_install.tracked_apps_seed", exc)
		except Exception:
			pass

	# v0.13.x: pre-populate Optimus Settings ▸ Ignored Apps with the two
	# framework apps operators most commonly can't (or won't) patch —
	# ``frappe`` and ``erpnext``. Mirrors the tracked-apps seed: writes
	# only when the table is empty (idempotent, never clobbers an
	# operator's existing configuration). Operators who DO want frappe /
	# erpnext findings (e.g. core contributors) can remove the rows
	# after install.
	try:
		_seed_ignored_apps_with_framework_apps()
	except Exception as exc:
		try:
			frappe.log_error(title="optimus after_install ignored-apps seed")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure("install.after_install.ignored_apps_seed", exc)
		except Exception:
			pass


# v0.13.x: the Frappe-organization-maintained apps seeded into Ignored
# Apps on fresh install. Mirrors ``FRAMEWORK_APPS`` in
# ``optimus.analyzers.base`` minus ``optimus`` itself (we're a third-
# party app, not Frappe-org). Operators who actively contribute to one
# of these (ERPNext core devs, HRMS maintainers, etc.) remove the rows
# they care about post-install — the seed is a sensible default that
# trades a tiny first-time setup step for a clean day-one report on
# the typical custom-app stack.
#
# Kept hardcoded (not derived from ``FRAMEWORK_APPS`` at import time)
# so future additions to FRAMEWORK_APPS don't silently change what's
# hidden by default on every new install. The two lists overlap
# heavily but track different intents: FRAMEWORK_APPS controls the
# actionable-vs-observation routing inside the report (architectural
# distinction); this tuple decides what to hide ENTIRELY on day one
# (UX choice). Bump it together with FRAMEWORK_APPS only when a new
# Frappe-org app is clearly noise for the typical custom-app
# developer. Sorted alphabetically so the seeded rows have a stable,
# predictable order in the Settings form.
_DEFAULT_IGNORED_APPS = (
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


def _seed_ignored_apps_with_framework_apps():
	"""Populate Optimus Settings.ignored_apps with the Frappe-organization
	apps operators most commonly can't (or won't) patch on day one — all
	the apps in ``_DEFAULT_IGNORED_APPS`` (frappe + erpnext + the nine
	other apps maintained by Frappe Technologies).

	Idempotent: if ``ignored_apps`` already has any rows (either from a
	previous install run on the same site, or from manual operator
	configuration before re-running migrate), we do NOT touch it. The
	seed is a fresh-install convenience, never a retroactive rewrite.
	"""
	if not frappe.db.exists("DocType", "Optimus Settings"):
		# Migration hasn't created the Single yet — skip silently
		# (mirror the tracked-apps seed's early-return).
		return

	settings = frappe.get_single("Optimus Settings")
	if settings.ignored_apps:
		# Respect existing config — never overwrite.
		return

	for app_name in _DEFAULT_IGNORED_APPS:
		settings.append("ignored_apps", {"app_name": app_name})
	settings.save(ignore_permissions=True)
	safe_commit()


def _seed_tracked_apps_from_installed_apps():
	"""Populate Optimus Settings.tracked_apps with every installed
	app that's NOT in the built-in framework allowlist.

	Idempotent: if tracked_apps is already populated (user configured
	it manually before running migrate again), we do nothing.
	"""
	from optimus.analyzers.base import FRAMEWORK_APPS

	if not frappe.db.exists("DocType", "Optimus Settings"):
		# Migration hasn't created the Single yet — skip silently.
		return

	settings = frappe.get_single("Optimus Settings")
	if settings.tracked_apps:
		# Respect existing config — never overwrite.
		return

	installed = frappe.get_installed_apps() or []
	custom_apps = [a for a in installed if a not in FRAMEWORK_APPS]
	if not custom_apps:
		return

	for app_name in custom_apps:
		settings.append("tracked_apps", {"app_name": app_name})
	settings.save(ignore_permissions=True)
	safe_commit()


def _assign_profiler_user_to_system_managers():
	"""Add Optimus User role to every user who has System Manager.

	Idempotent: existing Optimus Users are left untouched. Never removes
	roles. Safe to call repeatedly.

	v0.6.x: was an N+1 — one ``get_doc("User", name)`` per user in the
	system. Now uses a single ``Has Role`` query to find users with
	System Manager (and read their existing roles in the same fetch), so
	we only ``get_doc`` + ``save`` the subset that actually needs the new
	role added. On a site with 500 users where 5 are System Managers,
	this drops from 500 → ~5 doc loads.
	"""
	# Pull every user-role pair in one query, grouped by user. We only
	# care about two roles, but fetching them both in one round-trip is
	# faster than a per-user introspection.
	role_rows = frappe.get_all(
		"Has Role",
		filters={"role": ("in", ["System Manager", PROFILER_USER_ROLE])},
		fields=["parent", "role"],
	)
	roles_by_user: dict[str, set[str]] = {}
	for row in role_rows:
		roles_by_user.setdefault(row["parent"], set()).add(row["role"])

	for name, role_names in roles_by_user.items():
		if "System Manager" not in role_names:
			continue
		if PROFILER_USER_ROLE in role_names:
			continue
		# Only the users that need a NEW role assigned get loaded as docs
		# (so save() fires the right lifecycle events / hooks).
		user = frappe.get_doc("User", name)
		user.append("roles", {"role": PROFILER_USER_ROLE})
		user.save(ignore_permissions=True)


def on_user_role_change(doc, method=None):
	"""validate hook on User: auto-add Optimus User when System Manager
	is present.

	Wired via hooks.py: doc_events["User"]["validate"]. Silent — never
	raises and never produces a user-facing message. Idempotent.
	"""
	try:
		role_names = {r.role for r in (doc.roles or [])}
		if "System Manager" not in role_names:
			return
		if PROFILER_USER_ROLE in role_names:
			return
		doc.append("roles", {"role": PROFILER_USER_ROLE})
	except Exception as exc:
		try:
			frappe.log_error(title="optimus on_user_role_change")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure(
				"install.on_user_role_change", exc,
				context={"user": getattr(doc, "name", "") or ""},
			)
		except Exception:
			pass


def before_uninstall():
	"""Best-effort cleanup on uninstall.

	Clears any profiler:* keys from Redis so a reinstall starts with a
	clean state. Also restores the v0.3.0 monkey-patched wraps on
	frappe.get_doc / RedisWrapper.get_value / frappe.permissions.has_permission
	so subsequent code on this worker uses the originals.

	Does NOT delete:
	- The `Optimus User` role (users may still be assigned to it; a
	  re-install would lose those assignments).
	- The `Optimus Session` MariaDB rows (frappe's uninstall flow
	  drops the DocType tables naturally).
	- The attached report files (same — frappe's File doctype cleanup
	  handles these).
	"""
	# v0.3.0: restore the three monkey-patched functions on uninstall.
	try:
		from optimus import capture

		capture.uninstall_wraps()
	except Exception as exc:
		try:
			frappe.log_error(title="optimus before_uninstall capture")
		except Exception:
			pass
		try:
			from optimus import telemetry
			telemetry.emit_failure("install.before_uninstall.capture", exc)
		except Exception:
			pass

	try:
		_clear_redis_state()
	except Exception as exc:
		# Never fail the uninstall on a Redis hiccup.
		frappe.log_error(title="optimus uninstall cleanup")
		try:
			from optimus import telemetry
			telemetry.emit_failure("install.uninstall.cleanup", exc)
		except Exception:
			pass


def _clear_redis_state():
	"""Scan-and-delete all profiler:* keys from the site's Redis namespace."""
	try:
		redis_conn = frappe.cache.get_redis_connection()
	except Exception:
		return

	# Frappe's cache wrapper auto-prefixes keys with the site namespace
	# via make_key(). To SCAN for our patterns we need to use the raw
	# Redis connection with the fully-qualified site key.
	try:
		site_prefix = frappe.cache.make_key("").decode() if hasattr(
			frappe.cache.make_key(""), "decode"
		) else frappe.cache.make_key("")
	except Exception:
		site_prefix = ""

	patterns = (
		f"{site_prefix}profiler:active:*",
		f"{site_prefix}profiler:session:*",
		f"{site_prefix}profiler:explain:*",
	)

	deleted_count = 0
	for pattern in patterns:
		try:
			cursor = 0
			while True:
				cursor, keys = redis_conn.scan(cursor, match=pattern, count=100)
				if keys:
					redis_conn.delete(*keys)
					deleted_count += len(keys)
				if cursor == 0:
					break
		except Exception:
			# Continue with other patterns even if one fails
			continue

	if deleted_count:
		try:
			frappe.logger().info(
				f"optimus uninstall cleared {deleted_count} Redis keys"
			)
		except Exception:
			pass
