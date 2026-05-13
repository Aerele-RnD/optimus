# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.0: rename the auto-installed Role from ``Profiler User`` to
``Optimus User``.

``frappe.rename_doc("Role", ...)`` rewrites:
  - the ``tabRole`` row (the role's primary key),
  - every ``tabHas Role`` row whose ``role`` column referenced the
    old name (so users keep their access through the rename),
  - any DocPerm rows on DocTypes that named the old role.

Idempotent: skip if the old role is already gone (fresh install) or
the new one already exists.
"""

import frappe

OLD_ROLE = "Profiler User"
NEW_ROLE = "Optimus User"


def execute():
	try:
		old_exists = frappe.db.exists("Role", OLD_ROLE)
	except Exception:
		return
	if not old_exists:
		return  # fresh install or already renamed

	try:
		new_exists = frappe.db.exists("Role", NEW_ROLE)
	except Exception:
		return
	if new_exists:
		frappe.logger().warning(
			f"optimus v0.7.0: skipping Role rename {OLD_ROLE!r} → {NEW_ROLE!r} — "
			f"both names exist. Manually merge the role assignments before "
			f"re-running migrate."
		)
		return

	try:
		frappe.rename_doc("Role", OLD_ROLE, NEW_ROLE, force=True)
	except Exception:
		frappe.log_error(title="optimus v0.7.0 patch: rename Role")
		return

	try:
		frappe.clear_cache()
	except Exception:
		pass

	frappe.db.commit()
