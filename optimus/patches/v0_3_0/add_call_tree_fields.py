# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.3.0: add Optimus Action / Session fields and Finding type options.

Reloads three DocTypes:
  - optimus_action: adds call_tree_json, call_tree_size_bytes,
    call_tree_overflow_file
  - optimus_session: adds total_python_ms, total_sql_ms, hot_frames_json,
    session_time_breakdown_json
  - optimus_finding: extends the finding_type Select options with the
    four new v0.3.0 finding types (Slow Hot Path, Hook Bottleneck,
    Repeated Hot Frame, Redundant Call). Without reloading this DocType,
    every analyze run that emits a v0.3.0 finding type fails Frappe's
    Select validation in _persist().

The new columns are nullable; existing rows get NULL for the new fields and
render correctly via the renderer's backward-compat fallbacks.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_action")
	frappe.reload_doc("optimus", "doctype", "optimus_session")
	frappe.reload_doc("optimus", "doctype", "optimus_finding")
