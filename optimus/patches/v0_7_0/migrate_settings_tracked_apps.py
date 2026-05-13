# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.0: rewrite any ``Optimus Tracked App.app_name`` rows that name
the legacy app (``frappe_profiler``) to point at ``optimus``.

If a user explicitly added the profiler app to its OWN tracked-apps
list (e.g. for self-profiling experiments), the row's ``app_name``
column stores the string literal. After the package rename that
string is stale — the FRAMEWORK_APPS classifier and the analyzers'
tracked-app matching look up ``optimus`` now, so any leftover
``frappe_profiler`` entries become orphaned filters.

Runs in ``[post_model_sync]`` after ``rename_doctypes_to_optimus`` —
by then ``Optimus Tracked App`` exists as a DocType and the child
rows are queryable under the new name.

Idempotent: the UPDATE matches zero rows on fresh installs (no legacy
data) and on already-migrated sites (the substitution has already
happened).
"""

import frappe


def execute():
	try:
		frappe.db.sql(
			"""
			UPDATE `tabOptimus Tracked App`
			SET app_name = 'optimus'
			WHERE app_name = 'frappe_profiler'
			"""
		)
	except Exception:
		# Table may not exist yet on a fresh-install path where model
		# sync hasn't created it for some unrelated reason — let the
		# subsequent patches surface the real problem.
		frappe.log_error(title="optimus v0.7.0 patch: migrate_settings_tracked_apps")
		return

	frappe.db.commit()
