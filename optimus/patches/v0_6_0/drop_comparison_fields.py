# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.0: drop the session-comparison / baseline-pinning feature.

Removes two fields from Optimus Session:
  - compared_to_session
  - is_baseline

Frappe's auto-DDL drops the columns once optimus_session.json no longer
declares them; reloading the DocType here makes that happen during
migrate. We also clear any lingering ``profiler:baseline:<label>`` cache
keys so they don't dangle pointing at deleted sessions.

Idempotent: safe to re-run. Each step is wrapped in try/except so a
partially-applied state doesn't break the migration.
"""

import frappe


def execute():
	# Reload the DocType so Frappe's column-drop migration picks up the
	# field removal from optimus_session.json. After this, the underlying
	# tabOptimus Session table no longer has compared_to_session /
	# is_baseline columns.
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_session")
	except Exception:
		frappe.log_error(title="v0.6.0 patch: reload optimus_session (drop comparison)")

	# Best-effort: clear baseline-pinning cache keys. These were stored as
	# `profiler:baseline:<label>` -> docname. Redis SCAN-style deletion
	# isn't available through frappe.cache's portable API, so we just drop
	# the keys we can derive from existing session titles — anything else
	# expires on its own (cache, not durable state).
	try:
		titles = frappe.get_all("Optimus Session", pluck="title") or []
		for title in set(titles):
			try:
				frappe.cache.delete_value(f"profiler:baseline:{title or ''}")
			except Exception:
				pass
	except Exception:
		frappe.log_error(title="v0.6.0 patch: clear baseline cache keys")

	frappe.db.commit()
