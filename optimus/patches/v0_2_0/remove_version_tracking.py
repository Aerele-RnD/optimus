# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Round 2 fix #5: clean up tabVersion rows for Optimus Session.

Optimus Session originally had track_changes=1 (set in Phase 0 out of
habit). Each analyze writes 10+ fields, so every session was creating
10+ rows in tabVersion. On a site with 1000 profiler sessions, that's
15,000+ version rows of no audit value.

Round 2 fix sets track_changes=0 in the DocType JSON. This patch runs
on bench migrate to delete existing tabVersion rows for Profiler
Session so the cleanup is complete.

Safe to run multiple times (the DELETE is idempotent — any new rows
created between migrations are also cleaned up).
"""

import frappe


def execute():
	if not frappe.db.table_exists("tabVersion"):
		return

	# Count before deletion for the log
	count = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabVersion`
		WHERE ref_doctype = 'Optimus Session'
		"""
	)[0][0]

	if not count:
		return

	frappe.db.sql(
		"""
		DELETE FROM `tabVersion`
		WHERE ref_doctype = 'Optimus Session'
		"""
	)
	frappe.db.commit()

	try:
		frappe.logger().info(
			f"optimus patch v0_2_0 removed {count} Optimus Session version rows"
		)
	except Exception:
		pass
