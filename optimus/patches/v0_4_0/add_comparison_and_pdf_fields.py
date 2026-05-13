# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.4.0: add comparison and PDF fields to Optimus Session.

Three new fields:
  - compared_to_session (Link): baseline pointer for comparison rendering
  - is_baseline (Check): flag for sessions currently pinned as baseline
  - safe_report_pdf_file (Attach): lazy-generated PDF cache

All nullable / default 0. Existing rows remain unchanged; comparison
sections are skipped when compared_to_session is NULL.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_session")
