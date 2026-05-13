# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.0: rename the six product DocTypes from ``Profiler X`` →
``Optimus X``.

Runs in ``[pre_model_sync]`` after ``rename_module_to_optimus`` so the
Module Def has already been renamed; model sync then sees the JSON
``name`` field is ``Optimus X`` and the existing DB row is also
``Optimus X`` — no duplicate-creation, no conflict.

Each rename uses ``frappe.rename_doc("DocType", old, new, force=True)``
which atomically:
  - updates the ``tabDocType`` row,
  - renames the underlying SQL table (e.g. ``tabProfiler Session`` →
    ``tabOptimus Session``),
  - rewrites the ``parenttype`` column on every child-row in tables
    that referenced the renamed DocType,
  - clears the cached schema.

Idempotent + defensive (mirrors ``v0_6_0/rename_phase_two_doctype.py``):
  - Skip a rename if the old name is already gone (fresh install or
    already migrated).
  - Abort loudly if BOTH old and new exist — that's a partial
    migration that the operator must resolve manually.
"""

import frappe

# Order matters slightly: rename the parent (Profiler Session) AFTER
# its child tables, because rename_doc on the parent rewrites every
# child row's ``parenttype`` — which is faster if there are fewer
# child rows still under the old name.
RENAME_PAIRS = (
	("Profiler Action", "Optimus Action"),
	("Profiler Finding", "Optimus Finding"),
	("Profiler Phase Two Run", "Optimus Phase Two Run"),
	("Profiler Tracked App", "Optimus Tracked App"),
	("Profiler Settings", "Optimus Settings"),
	("Profiler Session", "Optimus Session"),
)


def execute():
	for old, new in RENAME_PAIRS:
		try:
			old_exists = frappe.db.exists("DocType", old)
		except Exception:
			continue
		if not old_exists:
			continue  # fresh install or already renamed

		try:
			new_exists = frappe.db.exists("DocType", new)
		except Exception:
			continue
		if new_exists:
			frappe.logger().warning(
				f"optimus v0.7.0: skipping DocType rename {old!r} → {new!r} — "
				f"both names exist. Resolve manually before re-running migrate "
				f"(typically: `bench --site <s> delete-doc DocType '{new}'` if "
				f"the new one is the empty duplicate)."
			)
			continue

		try:
			frappe.rename_doc("DocType", old, new, force=True)
		except Exception:
			frappe.log_error(title=f"optimus v0.7.0 patch: rename DocType {old!r}")
			continue

		try:
			frappe.clear_cache(doctype=new)
		except Exception:
			pass

	# Drop our own settings cache too (both the legacy and the renamed
	# keys, defensively).
	for key in ("profiler_settings_cached", "optimus_settings_cached"):
		try:
			frappe.cache.delete_value(key)
		except Exception:
			pass

	frappe.db.commit()
