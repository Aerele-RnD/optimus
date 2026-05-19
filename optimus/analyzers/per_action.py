# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: per-action breakdown.

Produces one Optimus Action row per recording, with a humanized label
derived best-effort from the recording's path/cmd/form_dict. The action
row is the unit the customer sorts/filters by in the report — "which step
of my flow took the longest?".

Label detection strategy:
    1. RQ Jobs: "Job: <last component of method name>"
    2. frappe.client.{save,insert,submit,cancel,delete}: "<Verb> <DocType>"
    3. frappe.client.get_list: "List <DocType>"
    4. Other cmd: use the cmd verbatim
    5. Fallback: "<METHOD> <path>"
"""

import json

from optimus.analyzers.base import AnalyzerResult


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	actions = [_build_action(r) for r in recordings]
	return AnalyzerResult(actions=actions)


def _build_action(recording: dict) -> dict:
	calls = recording.get("calls") or []
	durations = [c.get("duration", 0) for c in calls]
	return {
		"action_label": _label(recording),
		"event_type": recording.get("event_type") or "HTTP Request",
		"http_method": recording.get("method") or "",
		"path": (recording.get("path") or "")[:500],
		"recording_uuid": recording.get("uuid") or "",
		"duration_ms": round(recording.get("duration", 0), 2),
		"queries_count": len(calls),
		"query_time_ms": round(sum(durations), 2),
		"slowest_query_ms": round(max(durations, default=0), 2),
	}


def _label(recording: dict) -> str:
	"""Technical label for the Per-action table / Frontend XHR panel.

	Intentionally NOT humanized — shows the raw cmd (e.g.
	``frappe.client.save``) or ``METHOD path`` so developers see
	exactly what hit the server. The Steps-to-Reproduce section
	uses ``humanized_label`` instead, which reads like English
	("Save Sales Invoice", "Open Customer CUST-001").

	v0.5.1: cmd falls back to ``_derive_cmd_from_path`` when the
	recording's stored cmd is empty (Frappe's recorder captures
	cmd at hook time, BEFORE the REST routing sets form_dict.cmd
	— see ``_derive_cmd_from_path`` docstring for the full story).

	v0.5.2: ``frappe.desk.form.save.savedocs`` takes an ``action``
	field in form_dict ("Save"|"Submit"|"Cancel"|"Update") that
	routes to semantically different behaviors at the same cmd.
	Pre-v0.5.2 both appeared as the same label in the per-action
	table, making Save and Submit rows indistinguishable. Now the
	action is suffixed to the cmd (``frappe.desk.form.save
	.savedocs:Submit``) so developers can tell them apart without
	re-enabling the full humanization pipeline (which the user
	wanted off in technical breakdowns).
	"""
	if recording.get("event_type") == "RQ Job":
		path = recording.get("path") or "RQ Job"
		# Trim long module paths to the last component for readability
		short = path.split(".")[-1] if "." in path else path
		return f"RQ Job: {short}"

	cmd = recording.get("cmd") or ""
	if not cmd:
		cmd = _derive_cmd_from_path(recording.get("path") or "")

	if cmd:
		suffix = _multiplex_suffix(cmd, recording.get("form_dict") or {})
		if suffix:
			return f"{cmd}:{suffix}"
		return cmd

	method = recording.get("method") or "GET"
	path = recording.get("path") or "/"
	return f"{method} {path}"


# Known savedocs action values from Frappe's Desk form controller.
# Used by _label to append ``:<action>`` so Save vs Submit rows
# in the per-action table aren't indistinguishable.
_SAVEDOCS_ACTIONS = frozenset({"Save", "Submit", "Cancel", "Update"})

# v0.5.2: cmds that route multiple semantically-different operations
# through the SAME whitelisted endpoint. Without a disambiguator,
# every custom action button / workflow transition in a session rolls
# up to a single row in the per-action table, making them useless for
# diagnosing which button was slow.
#
# The three we handle:
#   frappe.desk.form.save.savedocs     → form_dict.action (Save/Submit/...)
#   run_doc_method                     → form_dict.method (any method name)
#   frappe.model.workflow.apply_workflow → form_dict.action (Approve/Reject/...)
#
# run_doc_method is the biggest one for ERPNext — every custom button
# defined via frm.add_custom_button(...) goes through it. Without a
# suffix, 20 distinct buttons in a Sales Invoice session look like one
# row of "run_doc_method × 20".
_RUN_DOC_METHOD_CMDS = frozenset({
	"run_doc_method",
	"frappe.handler.run_doc_method",
	"frappe.client.run_doc_method",
})
_APPLY_WORKFLOW_CMD = "frappe.model.workflow.apply_workflow"

# Validator for suffix values. Prevents garbage payloads from
# producing unbounded/unstable label strings that would fragment
# grouping-by-label. Must start with a letter, up to 60 chars of
# the character class below. Permits spaces because workflow action
# names are sometimes multi-word ("Submit for Approval").
import re as _re  # noqa: E402

_SAFE_SUFFIX_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9._\- ]{0,59}$")


def _multiplex_suffix(cmd: str, form_dict: dict) -> str:
	"""Return the disambiguating suffix for a multiplexed cmd, or "".

	Looks up the right form_dict key per cmd, validates the value
	against ``_SAFE_SUFFIX_RE``, and returns it. Unknown / malformed
	values return "" so the caller falls back to the bare cmd —
	keeps grouping-by-label stable when payloads are weird.
	"""
	if not isinstance(form_dict, dict):
		return ""

	# savedocs: action is one of Save/Submit/Cancel/Update. Strict
	# allowlist because these are the only valid values.
	if cmd == "frappe.desk.form.save.savedocs":
		action = (form_dict.get("action") or "").strip()
		return action if action in _SAVEDOCS_ACTIONS else ""

	# run_doc_method: method is the actual dotted method name, e.g.
	# "make_payment_entry", "send_email", "update_items". Frappe's
	# whitelisting layer already validates the method exists — we
	# just need to pick something that renders cleanly and doesn't
	# include literal user data (most doc-method names are plain
	# Python identifiers, but defensively validate).
	if cmd in _RUN_DOC_METHOD_CMDS:
		method = (form_dict.get("method") or "").strip()
		if method and _SAFE_SUFFIX_RE.match(method):
			return method
		return ""

	# apply_workflow: action is a user-defined Workflow Action name
	# like "Approve", "Reject", "Submit for Approval". Validate
	# shape because workflow admins occasionally name actions with
	# emoji or non-ASCII characters — those round-trip OK through
	# Frappe but look weird in a technical label.
	if cmd == _APPLY_WORKFLOW_CMD:
		action = (form_dict.get("action") or "").strip()
		if action and _SAFE_SUFFIX_RE.match(action):
			return action
		return ""

	return ""


def humanized_label(recording: dict) -> str:
	"""Human-readable label for the Steps-to-Reproduce section.

	Reads like English ("Create Sales Invoice", "Submit Delivery Note",
	"Open Customer CUST-001", "Search Item") rather than the technical
	cmd string. Used exclusively by ``analyze._build_auto_notes_html`` —
	the per-action table and frontend XHR panel continue to show the
	technical label via ``_label``.

	Per user feedback on v0.5.1: "only humanize call name in step to
	reproduce only not on other breakdowns." The Steps-to-Reproduce
	section is a high-level flow summary, so English phrasing reads
	better there. Everywhere else, the raw cmd string is more
	informative for a developer looking at the technical report.

	Falls back to ``_label`` when the cmd doesn't match any of the
	humanization rules — so unknown cmds produce the same technical
	label they would in the per-action table, not an empty string.
	"""
	if recording.get("event_type") == "RQ Job":
		# RQ Jobs use the same label in both views — the
		# "Job: <method>" form is already readable.
		return _label(recording)

	cmd = recording.get("cmd") or ""
	if not cmd:
		cmd = _derive_cmd_from_path(recording.get("path") or "")
	form_dict = recording.get("form_dict") or {}

	# frappe.desk.form.save.savedocs carries an `action` field
	# ("Save"|"Submit"|"Cancel"|"Update") and an embedded `doc` JSON
	# with the __islocal flag for brand-new records.
	if cmd == "frappe.desk.form.save.savedocs":
		action = ""
		if isinstance(form_dict, dict):
			action = form_dict.get("action") or ""
		doctype, is_new = _extract_doc_info(form_dict)
		if doctype:
			if action == "Submit":
				return f"Submit {doctype}"
			if action == "Cancel":
				return f"Cancel {doctype}"
			if action == "Update":
				return f"Update {doctype}"
			# action=Save (or empty). Distinguish Create-vs-Save from
			# __islocal so the reproducer reads "Create Sales Invoice"
			# when the user made a new doc.
			return f"{'Create' if is_new else 'Save'} {doctype}"

	verb_for_cmd = {
		"frappe.client.save": "Save",
		"frappe.client.insert": "Create",
		"frappe.client.insert_many": "Create many",
		"frappe.client.submit": "Submit",
		"frappe.client.cancel": "Cancel",
		"frappe.client.delete": "Delete",
		"frappe.client.set_value": "Update",
	}
	if cmd in verb_for_cmd:
		doctype = _extract_doctype(form_dict)
		if doctype:
			return f"{verb_for_cmd[cmd]} {doctype}"

	if cmd == "frappe.client.get_list":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		if doctype:
			return f"List {doctype}"

	# v0.5.2: run_doc_method (every custom action button) and
	# apply_workflow (workflow transitions) both multiplex multiple
	# operations into one cmd. Humanize by reading the real method
	# / action name + the doc identifiers.
	if cmd in _RUN_DOC_METHOD_CMDS:
		method = ""
		doctype = ""
		name = ""
		if isinstance(form_dict, dict):
			method = (form_dict.get("method") or "").strip()
			doctype = (form_dict.get("dt") or "").strip()
			name = (form_dict.get("dn") or "").strip()
		if method and _SAFE_SUFFIX_RE.match(method):
			# Turn "make_payment_entry" into "Make Payment Entry".
			human_method = method.replace("_", " ").title()
			if doctype and name:
				return f"{human_method} on {doctype} {name}"
			if doctype:
				return f"{human_method} on {doctype}"
			return human_method

	if cmd == _APPLY_WORKFLOW_CMD:
		action = ""
		if isinstance(form_dict, dict):
			action = (form_dict.get("action") or "").strip()
		doctype, _is_new = _extract_doc_info(form_dict)
		if action and _SAFE_SUFFIX_RE.match(action):
			if doctype:
				return f"{action} {doctype}"
			return f"Workflow action: {action}"

	# Desk navigation / form-open operations.
	if cmd == "frappe.desk.form.load.getdoc":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		name = form_dict.get("name") if isinstance(form_dict, dict) else None
		if doctype and name:
			return f"Open {doctype} {name}"
		if doctype:
			return f"Open {doctype}"

	if cmd == "frappe.desk.form.load.getdoctype":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		if doctype:
			return f"Load {doctype} form"

	if cmd == "frappe.desk.search.search_link":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		if doctype:
			return f"Search {doctype}"

	# No humanization rule matched — defer to the technical label so
	# the caller always gets a non-empty string.
	return _label(recording)


def _derive_cmd_from_path(path: str) -> str:
	"""Parse ``<method>`` out of ``/api/method/<method>`` and
	``/api/v2/method/<method>`` URLs so the humanization logic in
	``_label`` can run on recordings whose ``cmd`` field is empty.

	Returns "" for any other URL shape (``/app/...``, ``/api/
	resource/...``, static files) — the caller's fallback to
	``METHOD + path`` still applies.

	Why this is needed: frappe.recorder.Recorder.__init__ reads
	frappe.local.form_dict.cmd, which the REST routing layer only
	populates AFTER the before_request hooks have run. Every
	modern /api/method URL therefore ends up with cmd="" in the
	captured recording dict, so "Save Sales Invoice"-style
	humanization never fires without this path fallback.
	"""
	if not path:
		return ""
	marker = "/method/"
	idx = path.find(marker)
	if idx < 0:
		return ""
	after = path[idx + len(marker):].split("?", 1)[0].rstrip("/")
	return after


def _extract_doc_info(form_dict) -> tuple[str | None, bool]:
	"""Return (doctype, is_new) from a savedocs payload.

	The Desk's savedocs endpoint posts a JSON-encoded `doc` field that
	contains the doctype plus a ``__islocal: 1`` marker for brand-new
	(never-persisted) documents. We use that marker to distinguish
	"Create Sales Invoice" (a new doc) from "Save Sales Invoice"
	(an existing doc). Returns (None, False) when the payload is
	unparseable.
	"""
	if not isinstance(form_dict, dict):
		return None, False
	doctype = form_dict.get("doctype")
	doc = form_dict.get("doc")
	if isinstance(doc, str):
		try:
			doc = json.loads(doc)
		except Exception:
			doc = None
	is_new = False
	if isinstance(doc, dict):
		doctype = doctype or doc.get("doctype")
		# __islocal is sent as 1 for unsaved docs, absent/0 otherwise.
		# Cast defensively — Frappe sends both int 1 and string "1"
		# depending on the client.
		raw_islocal = doc.get("__islocal")
		is_new = bool(raw_islocal) and raw_islocal not in ("0", 0, False)
	return doctype, is_new


def _extract_doctype(form_dict) -> str | None:
	"""Extract a DocType name from form_dict.

	form_dict.doctype takes precedence; otherwise we look at form_dict.doc
	(which may be a JSON-encoded dict).
	"""
	if not isinstance(form_dict, dict):
		return None
	if form_dict.get("doctype"):
		return form_dict["doctype"]
	doc = form_dict.get("doc")
	if isinstance(doc, str):
		try:
			doc = json.loads(doc)
		except Exception:
			return None
	if isinstance(doc, dict) and doc.get("doctype"):
		return doc["doctype"]
	return None
