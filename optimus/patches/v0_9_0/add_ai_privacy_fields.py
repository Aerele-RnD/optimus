# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.9.0: register the AI privacy-hardening fields.

Additive only:
  - Optimus Settings: ai_privacy_section + ai_excluded_finding_types
    (Small Text, multi-line, # comments) + ai_request_timeout_seconds (Int)

``bench migrate`` already auto-adds the new fields from the updated
``optimus_settings.json``; this patch reloads the DocType deterministically
during the patch run (matching the pattern of the other ``add_*_fields``
patches in this app). Idempotent — safe to re-run.

Closes Critical Risk #2 of the v0.7.x architecture review (no per-type
opt-out, no configurable timeout for local LLMs). See docs/AI-FIXING.md
for the data-flow inventory and local-LLM recipes.
"""

import frappe


def execute():
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_settings")
	except Exception:
		frappe.log_error(
			title="v0.9.0 patch: reload optimus_settings (add AI privacy fields)"
		)
	frappe.db.commit()
