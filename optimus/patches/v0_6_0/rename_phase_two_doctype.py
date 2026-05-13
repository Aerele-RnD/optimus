# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.x: rename the ``Profiler Phase 2 Run`` DocType to
``Profiler Phase Two Run`` so the name passes the Frappe Title-Case
convention (digits-in-name was a Lens-audit warning, item 2.6 of the
audit-response plan).

This patch describes a migration that ran BEFORE the v0.7.0 app rename
(``frappe_profiler`` → ``optimus``); the historical DocType names here
are intentionally Profiler-prefixed. v0.7.0's ``rename_doctypes_to_optimus``
runs afterwards and renames ``Profiler Phase Two Run`` to ``Optimus Phase
Two Run`` as part of the broader DocType rename.

Idempotent + defensive:

1. No-op when the old DocType is already gone (fresh installs already
   ship with the new name in JSON — Frappe creates the new DocType
   directly).
2. No-op when the new name already exists alongside the old (someone
   ran a partial migration). In that case the renamed table would
   conflict — bail out and let the operator resolve it manually.
3. Uses ``frappe.rename_doc("DocType", old, new)`` which handles:
     - renaming the row in ``tabDocType``,
     - renaming the underlying SQL table (``tabProfiler Phase 2 Run``
       → ``tabProfiler Phase Two Run``),
     - updating every child row's ``parenttype`` column on the parent
       (``tabProfiler Session``'s ``phase_2_runs`` table),
     - clearing the cached schema.
4. Clears the settings cache as a safety net for any stale references.

Trade-off acknowledged in ``AUDIT_BASELINE.md``: the in-code field name
on the parent (``phase_2_runs``) stays snake_case — the audit's
Title-Case rule applies only to DocType names, not field names.
"""

import frappe

OLD_NAME = "Profiler Phase 2 Run"
NEW_NAME = "Profiler Phase Two Run"


def execute():
	try:
		old_exists = frappe.db.exists("DocType", OLD_NAME)
	except Exception:
		return
	if not old_exists:
		return  # fresh install or already renamed

	# Guard: if BOTH names exist, renaming would clash. Bail out loud.
	try:
		new_exists = frappe.db.exists("DocType", NEW_NAME)
	except Exception:
		return
	if new_exists:
		frappe.logger().warning(
			"optimus: skipping Phase-Two DocType rename — both "
			f"{OLD_NAME!r} AND {NEW_NAME!r} exist. Resolve manually before "
			"re-running migrate (e.g. `bench --site <s> delete-doc DocType "
			f"'{NEW_NAME}'` if the empty one is the duplicate)."
		)
		return

	try:
		frappe.rename_doc("DocType", OLD_NAME, NEW_NAME, force=True)
	except Exception:
		frappe.log_error(title="optimus patch: rename phase-two doctype")
		return

	# Belt + braces: clear any cached doctype meta so the next request
	# resolves the new name cleanly.
	try:
		frappe.clear_cache(doctype=NEW_NAME)
	except Exception:
		pass

	# Drop our own settings cache too — paranoid but cheap. Both the legacy
	# (``profiler_settings_cached``) and the v0.7.0-renamed
	# (``optimus_settings_cached``) keys are cleared since either could
	# be lingering depending on which version the site is upgrading from.
	for key in ("profiler_settings_cached", "optimus_settings_cached"):
		try:
			frappe.cache.delete_value(key)
		except Exception:
			pass

	frappe.db.commit()
