# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.5.2 round 4: Bump redundant_cache_threshold on the Profiler
Settings Single from the old default of 10 to the new default of 50.

Rationale: cache lookups aren't individually timed (sidecar captures
identifier + args, not wall time), so every Redundant Cache finding
reports impact=0ms. At threshold=10, a 10-20× cache loop at 0ms
impact was indistinguishable from framework background noise and
flooded the actionable findings list on real sessions.

This patch runs on migrate. It's conservative:

1. Only runs if the Optimus Settings Single exists (fresh installs
   pick up the new default from JSON; nothing to patch).
2. Only bumps the value from **exactly 10** — the prior default.
   Any other value (user-tuned 5, 20, 100, etc.) is left alone so we
   never silently overwrite a deliberate configuration choice.
3. Invalidates the settings cache so the next request sees the new
   value without a bench restart.
"""

import frappe

OLD_DEFAULT = 10
NEW_DEFAULT = 50


def execute():
	if not frappe.db.exists("DocType", "Optimus Settings"):
		return
	try:
		current = frappe.db.get_single_value(
			"Optimus Settings", "redundant_cache_threshold"
		)
	except Exception:
		return

	# Only flip the exact old default — respect any deliberate tuning.
	try:
		current_int = int(current) if current is not None else None
	except (TypeError, ValueError):
		return

	if current_int != OLD_DEFAULT:
		return

	frappe.db.set_single_value(
		"Optimus Settings", "redundant_cache_threshold", NEW_DEFAULT
	)
	# Blow the settings cache so analyzers pick up the new value
	# without waiting for a bench restart.
	try:
		frappe.cache.delete_value("optimus_settings_cached")
	except Exception:
		pass
	frappe.db.commit()
