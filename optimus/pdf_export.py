# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Lazy PDF generation for the report.

Generated on first request via frappe.utils.pdf.get_pdf (wkhtmltopdf),
cached to a private File attachment on the Optimus Session. Subsequent
requests serve from cache. Analyze pipeline is never touched — keeping
PDF generation outside the analyze budget.
"""

import re

import frappe

from optimus import safe_commit

# v0.5.2 round 4: wkhtmltopdf uses old QtWebKit which doesn't reliably
# render collapsed <details> content even under @media print — the
# Observations subsection and Analyzer notes section end up invisible
# in the PDF. We pre-process the HTML to force `open` on every
# <details> before handing it to wkhtmltopdf, so PDF reports contain
# everything the reader sees when they expand sections in the browser.
#
# Pattern matches:  <details class="section">     → <details class="section" open>
#                   <details class="subsection">  → <details class="subsection" open>
#                   <details id="foo">            → <details id="foo" open>
# Already-open details are left alone (idempotent).
_DETAILS_OPEN_RE = re.compile(
	r"<details\b([^>]*?)>",
	re.IGNORECASE,
)


def get_or_generate_pdf(session_uuid: str) -> str:
	"""Return the file_url of the cached PDF, generating on first call.

	Permission check is the caller's responsibility (api.download_pdf).
	Raises on generation failure; does NOT cache failed generations.
	"""
	doc = _load_session(session_uuid)
	if doc.raw_report_pdf_file:
		return doc.raw_report_pdf_file
	html = _load_raw_html(doc)
	pdf_bytes = _html_to_pdf(html)
	url = _save_pdf_attachment(doc.name, session_uuid, pdf_bytes)
	frappe.db.set_value("Optimus Session", doc.name, "raw_report_pdf_file", url)
	safe_commit()
	return url


def clear_cached_pdf(session_uuid: str) -> None:
	"""Clear the cached PDF for a session. Called from retry_analyze.

	Best-effort: if the File row is gone or the field isn't set, no-op.
	"""
	docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name",
	)
	if not docname:
		return
	current_url = frappe.db.get_value(
		"Optimus Session", docname, "raw_report_pdf_file",
	)
	if not current_url:
		return
	try:
		# v0.6.x: was get_doc(...).name; switched to a single get_value to
		# skip loading the full File doc (and its child tables) just to read
		# one scalar — addresses Lens audit "get_doc used only for reading
		# fields" at this site.
		file_name = frappe.db.get_value("File", {"file_url": current_url}, "name")
		if file_name:
			frappe.delete_doc("File", file_name, force=True, ignore_permissions=True)
	except Exception:
		pass
	frappe.db.set_value("Optimus Session", docname, "raw_report_pdf_file", None)
	safe_commit()


def _load_session(session_uuid: str):
	docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name",
	)
	if not docname:
		frappe.throw(f"No Optimus Session found for uuid {session_uuid}")
	return frappe.get_doc("Optimus Session", docname)


def _load_raw_html(doc) -> str:
	if not doc.raw_report_file:
		frappe.throw("This session has no report to convert.")
	# audit: ok (get_doc kept — .get_content() is a doc-instance method
	# that streams the attached bytes; db.get_value can only return field
	# values, not file content. Lens flagged this as "get_doc used only
	# for reading fields" but it isn't reading a field, it's reading the
	# attached binary).
	file_row = frappe.get_doc("File", {"file_url": doc.raw_report_file})
	return file_row.get_content().decode("utf-8")


def _expand_collapsible_sections(html: str) -> str:
	"""Add the ``open`` attribute to every <details> element so the
	PDF renderer shows their content.

	Idempotent — <details open> and <details ... open> are left alone.
	Exposed as module-level for unit-testing.
	"""
	def _inject_open(match: re.Match) -> str:
		attrs = match.group(1).strip()
		# Already open? Preserve as-is.
		if re.search(r"\bopen\b", attrs, re.IGNORECASE):
			return match.group(0)
		if attrs:
			return f"<details {attrs} open>"
		return "<details open>"

	return _DETAILS_OPEN_RE.sub(_inject_open, html)


def _html_to_pdf(html: str) -> bytes:
	"""Run HTML through wkhtmltopdf via frappe.utils.pdf.get_pdf.

	Options tuned for the report's CSS — A4, conservative margins,
	UTF-8, print-media-type so any @media print rules in the report are
	honored (e.g. the SVG donut fallback).

	Pre-processes the HTML to force-open all <details> blocks so the
	Observations subsection and Analyzer notes render in the PDF —
	wkhtmltopdf's QtWebKit doesn't reliably expand collapsed <details>
	via @media print alone.
	"""
	import frappe.utils.pdf

	html = _expand_collapsible_sections(html)

	options = {
		"page-size": "A4",
		"margin-top": "15mm",
		"margin-bottom": "15mm",
		"margin-left": "12mm",
		"margin-right": "12mm",
		"encoding": "UTF-8",
		"print-media-type": "",
		"enable-local-file-access": "",
	}
	return frappe.utils.pdf.get_pdf(html, options)


def _save_pdf_attachment(docname: str, session_uuid: str, pdf_bytes: bytes) -> str:
	"""Insert a private File attached to the Optimus Session.

	Returns the file_url. Raises on failure (caller decides not to cache).
	"""
	filename = f"optimus_report_{session_uuid}.pdf"
	file_doc = frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": "Optimus Session",
		"attached_to_name": docname,
		"attached_to_field": "raw_report_pdf_file",
		"is_private": 1,
		"content": pdf_bytes,
	})
	file_doc.insert(ignore_permissions=True)
	return file_doc.file_url
