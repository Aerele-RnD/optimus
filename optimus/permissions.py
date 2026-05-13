# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Server-side permission gates for profiler artifacts.

Currently exposes one gate: a `has_permission` for the File DocType that
double-checks downloads of the profiler report. The UI hides the
"Download Report" button from non-admin/non-owner users, but a malicious
user who guessed the file URL could try to fetch it directly — this
gate makes that fail.

v0.6.0 Round 7: safe-mode reports were removed. The single remaining
report (raw_report_file + raw_report_pdf_file) is admin-scoped via
this gate.
"""

import frappe

PROFILER_SESSION_DOCTYPE = "Optimus Session"
_GATED_FIELDS = frozenset({"raw_report_file", "raw_report_pdf_file"})


def file_has_permission(doc, ptype=None, user=None):
	"""Gate downloads of the profiler report (HTML + PDF).

	Allows access to:
	  - Anyone who passes the underlying parent permission check (handled
	    by Frappe's built-in private file logic — Optimus User with
	    if_owner=1, System Manager always)
	  - Plus an additional check for the report files specifically: only
	    System Manager OR the recording user, even if some other role
	    accidentally got read access to the parent.

	Return None to defer to Frappe's standard permission logic; return
	False to deny.
	"""
	if not doc:
		return None

	# Only intercept files attached to a Optimus Session.
	if doc.attached_to_doctype != PROFILER_SESSION_DOCTYPE:
		return None

	# Only intercept the report files (HTML + lazy PDF).
	if doc.attached_to_field not in _GATED_FIELDS:
		return None

	user = user or frappe.session.user
	roles = frappe.get_roles(user)

	if "System Manager" in roles or "Administrator" in roles:
		return None  # defer to standard checks

	# Otherwise the user must be the recording user
	if not doc.attached_to_name:
		return False
	recording_user = frappe.db.get_value(
		PROFILER_SESSION_DOCTYPE,
		doc.attached_to_name,
		"user",
	)
	if recording_user != user:
		return False

	return None  # passed our gate; let standard checks run
