# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.5.0: upgrade Optimus Session.notes from Text to Text Editor.

The existing `notes` field was plain Text. v0.5.0 upgrades it to
Text Editor so users can include rich formatting (lists, links,
code blocks) in their "Steps to Reproduce" context — rendered at
the top of the report above findings.

Fresh installs pick this up from the updated doctype JSON automatically.
This patch reloads the doctype definition so existing installs see the
new fieldtype and label without needing a manual bench migrate --rebuild.

Existing note values carry over unchanged: plain-text content is valid
Text Editor input, so no data migration is needed — the DB column
stays, only the metadata changes.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_session")
	frappe.clear_cache(doctype="Optimus Session")
