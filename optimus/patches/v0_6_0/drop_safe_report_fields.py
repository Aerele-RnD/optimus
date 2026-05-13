# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.0 Round 7: drop safe-mode reporting.

Removes three fields:
  - Optimus Session.safe_report_file
  - Optimus Session.safe_report_pdf_file
  - Optimus Settings.safe_report_include_source_lines

Frappe's auto-DDL handles dropping the columns when the .json no longer
declares them, but we explicitly nuke any attached File rows pointing at
the old fields so they don't dangle as orphaned private files. The
File records are only deleted after the session row's column is gone,
because deleting the File first would still leave the session row
pointing at a missing URL.

Idempotent: safe to re-run. Each step is wrapped in a try/except so a
partially-applied state (e.g. column already dropped, file already
gone) doesn't break the migration.
"""

import frappe


def execute():
	# Reload the DocType so Frappe's column-drop migration picks up the
	# field removal from optimus_session.json / optimus_settings.json.
	# After this call, the underlying tabOptimus Session table no longer
	# has safe_report_file / safe_report_pdf_file columns, and
	# tabOptimus Settings no longer has safe_report_include_source_lines.
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_session")
	except Exception:
		frappe.log_error(title="v0.6.0 patch: reload optimus_session")

	try:
		frappe.reload_doc("optimus", "doctype", "optimus_settings")
	except Exception:
		frappe.log_error(title="v0.6.0 patch: reload optimus_settings")

	# Best-effort cleanup of orphaned safe-report File rows. Pre-v0.6.0
	# safe reports were attached as private files with their attached_to_field
	# pointing at safe_report_file or safe_report_pdf_file. Those File
	# rows still exist after the column drop; remove them so they don't
	# linger forever.
	for field_name in ("safe_report_file", "safe_report_pdf_file"):
		try:
			rows = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": "Optimus Session",
					"attached_to_field": field_name,
				},
				pluck="name",
			)
			for name in rows:
				try:
					frappe.delete_doc(
						"File", name, force=True, ignore_permissions=True,
					)
				except Exception:
					# Individual file deletion failures are non-fatal —
					# log and move on so a single broken row doesn't
					# block the rest of the migration.
					frappe.log_error(
						title=f"v0.6.0 patch: delete orphaned {field_name} File",
					)
		except Exception:
			frappe.log_error(
				title=f"v0.6.0 patch: enumerate orphaned {field_name} Files",
			)

	frappe.db.commit()
