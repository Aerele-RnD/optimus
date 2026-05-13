# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.x: turn ON ``hide_framework_tables`` for existing Profiler
Settings rows that have the field stored as 0 / None.

Background: the field shipped without a JSON ``default: "1"``, so the
Single's persisted value initialized to 0 for every install. The
``_DEFAULTS`` dict + dataclass default of True only kicked in when the
DocType row was missing entirely — once the field exists in storage,
the persisted 0 wins. Users who never explicitly touched the checkbox
were left with the filter off, even though the docs and the
description text both say the feature is "default on". A user
regenerated their report, footer stamped ``hide_framework_tables = off``,
and every framework / internal table was visible.

This patch is a one-time correction:

1. Only runs if the Optimus Settings Single exists (fresh installs
   pick up the new JSON default via ``"default": "1"`` — no patch
   needed).
2. Only flips values that are currently 0 / None / empty / unset to 1.
   Any non-falsy stored value is left alone — if a deployment had
   somehow set it to 1 already, the patch is a no-op.
3. Trade-off: if an admin DELIBERATELY unchecked the box and saved
   (stored 0), the patch flips that 0 → 1. We can't distinguish
   "default 0 from missing JSON default" from "user explicitly chose
   0". The risk is mitigated by the trailing footer line ("Rendered
   with: hide_framework_tables = on") which makes the current value
   obvious on the next regenerated report; the admin can re-uncheck +
   re-save if needed.
4. Invalidates the settings cache so the next request sees the new
   value without a bench restart.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Optimus Settings"):
		return
	try:
		current = frappe.db.get_single_value(
			"Optimus Settings", "hide_framework_tables"
		)
	except Exception:
		return

	# Coerce: a stored "0" / 0 / None / "" all read as falsy. Anything
	# truthy (1, "1", True) is left alone.
	if current:
		return

	try:
		frappe.db.set_single_value(
			"Optimus Settings", "hide_framework_tables", 1
		)
	except Exception:
		return

	try:
		frappe.cache.delete_value("optimus_settings_cached")
	except Exception:
		pass
	frappe.db.commit()
