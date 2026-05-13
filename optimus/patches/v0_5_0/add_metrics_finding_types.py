# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.5.0: append the seven new metrics Finding Types to the Profiler
Finding doctype's finding_type Select field.

Finding types added (v0.5.0):
  - Resource Contention
  - Memory Pressure
  - DB Pool Saturation
  - Background Queue Backlog
  - Slow Frontend Render
  - Network Overhead
  - Heavy Response

Fresh installs pick these up from the updated doctype JSON automatically.
This patch reloads the doctype definition on existing installs so the
Select field options come from disk rather than the cached DB metadata.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_finding")
	frappe.clear_cache(doctype="Optimus Finding")
