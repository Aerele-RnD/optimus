# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.8.0: register the opt-in failure telemetry feature.

Additive only:
  - Optimus Settings: telemetry_tab + telemetry_enabled / telemetry_sink_doctype /
    telemetry_sink_jsonl_file / telemetry_endpoint_url / telemetry_retention_days
  - NEW DocType: Optimus Telemetry Event (one row per (event_name, signature))

``bench migrate`` already auto-adds the new fields from the updated
``optimus_settings.json`` and creates the new ``Optimus Telemetry Event``
table from its .json; this patch just reloads both deterministically inside
the patch run (matching the pattern of the other ``add_*_fields`` patches
in this app). Idempotent — safe to re-run.
"""

import frappe


def execute():
	for doctype in ("optimus_settings", "optimus_telemetry_event"):
		try:
			frappe.reload_doc("optimus", "doctype", doctype)
		except Exception:
			frappe.log_error(
				title=f"v0.8.0 patch: reload {doctype} (add telemetry fields)"
			)
	frappe.db.commit()
