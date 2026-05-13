# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.0: rename the ``Frappe Profiler`` Module Def to ``Optimus`` and
re-point every ``tabDocType.module`` row that referenced it.

Runs in ``[pre_model_sync]`` so model sync — which builds new Module
Def rows from the JSON ``module`` field — finds the rename already
applied and doesn't create a duplicate ``Optimus`` row alongside the
legacy ``Frappe Profiler`` one.

Idempotent:
  - Fresh install: the ``Frappe Profiler`` Module Def never exists →
    the UPDATE matches zero rows.
  - Already-migrated: the rename has been applied → the UPDATE matches
    zero rows on the second run.

Sequencing inside this patch:
  1. Update ``tabDocType.module`` first (rows reference the Module Def
     by name; updating them before renaming the Module Def itself
     avoids a brief inconsistency window).
  2. Rename the Module Def row (``Frappe Profiler`` → ``Optimus``) and
     update its ``app_name`` (``frappe_profiler`` → ``optimus``).
"""

import frappe

OLD_MODULE = "Frappe Profiler"
NEW_MODULE = "Optimus"
OLD_APP = "frappe_profiler"
NEW_APP = "optimus"


def execute():
	try:
		old_exists = frappe.db.exists("Module Def", OLD_MODULE)
	except Exception:
		return
	if not old_exists:
		return  # fresh install or already renamed

	# Step 1: re-point every DocType row that lists the old module name.
	try:
		frappe.db.sql(
			"UPDATE tabDocType SET module = %s WHERE module = %s",
			(NEW_MODULE, OLD_MODULE),
		)
	except Exception:
		frappe.log_error(title="optimus v0.7.0 patch: rename_module step 1 (tabDocType)")
		return

	# Step 2: rename the Module Def row itself. ``rename_doc`` updates
	# ``tabModule Def.name`` (the primary key) AND any child references
	# correctly.
	try:
		frappe.rename_doc("Module Def", OLD_MODULE, NEW_MODULE, force=True)
	except Exception:
		frappe.log_error(title="optimus v0.7.0 patch: rename_module step 2 (Module Def)")
		return

	# Step 3: update the app_name column on the renamed Module Def.
	try:
		frappe.db.set_value("Module Def", NEW_MODULE, "app_name", NEW_APP)
	except Exception:
		frappe.log_error(title="optimus v0.7.0 patch: rename_module step 3 (app_name)")
		return

	# Drop any cached module/doctype meta so the next request resolves
	# against the renamed rows.
	try:
		frappe.clear_cache()
	except Exception:
		pass

	frappe.db.commit()
