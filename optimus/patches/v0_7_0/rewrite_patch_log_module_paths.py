# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.0: rewrite ``tabPatch Log`` entries from ``frappe_profiler.patches.*``
→ ``optimus.patches.*``.

Why this runs FIRST (top of ``[pre_model_sync]`` in ``patches.txt``):

The v0.7.0 rename moves the package directory ``frappe_profiler/`` →
``optimus/`` and rewrites every entry in ``patches.txt`` to the new
import path. But existing installs already have rows in ``tabPatch
Log`` recording the *old* module path for every patch that has ever
run on the site. If we did nothing, the next ``bench migrate`` would
scan ``patches.txt``, see entries like ``optimus.patches.v0_5_0.
add_metrics_finding_types`` that aren't present in ``tabPatch Log``,
and re-run every historical patch under their new names.

Most of those patches are idempotent (defensive ``if exists`` checks)
so re-running them wouldn't corrupt data — but it would lengthen
``bench migrate`` substantially and risks any non-idempotent edge
case. Rewriting the path strings up-front means the existence-check
inside Frappe's patch runner sees ``optimus.patches.X`` already
present in ``tabPatch Log`` and correctly skips.

Idempotent: a fresh install has no matching rows; a second run finds
the rows already rewritten and the UPDATE is a no-op.
"""

import frappe


def execute():
	try:
		# UPDATE all log entries that still carry the old import prefix.
		# Direct SQL is intentional — ``tabPatch Log`` is a metadata table
		# Frappe owns and the migration is a flat string substitution.
		frappe.db.sql(
			"""
			UPDATE `tabPatch Log`
			SET patch = REPLACE(patch, 'frappe_profiler.patches.', 'optimus.patches.')
			WHERE patch LIKE 'frappe_profiler.patches.%%'
			"""
		)
	except Exception:
		frappe.log_error(title="optimus v0.7.0 patch: rewrite_patch_log_module_paths")
		return
	frappe.db.commit()
