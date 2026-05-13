# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for optimus.analyzers.per_action."""

from optimus.analyzers import per_action


def test_save_sales_invoice_action_label(n_plus_one_recording, empty_context):
	"""v0.5.1: the per-action table shows the TECHNICAL label (raw
	cmd), not the humanized Steps-to-Reproduce form. A developer
	reading the technical report wants to see 'frappe.client.save'
	— the actual whitelisted method — not a prose summary.
	The Steps-to-Reproduce section uses `humanized_label` instead.
	"""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	assert len(result.actions) == 1
	assert result.actions[0]["action_label"] == "frappe.client.save"


def test_action_aggregates_match_calls(n_plus_one_recording, empty_context):
	"""Per-action totals should match sum of calls."""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	action = result.actions[0]
	# 12 calls in the n_plus_one fixture
	assert action["queries_count"] == 12
	# Sum of durations from the fixture (first call 1.5 + 10 N+1 ~9ms + update 2.1)
	assert 90 < action["query_time_ms"] < 110
	# Slowest should be one of the N+1 queries at ~11.4ms
	assert action["slowest_query_ms"] > 10


def test_non_frappe_client_cmd_falls_back_to_cmd(empty_context):
	"""Unknown cmd should be shown verbatim, not as a 'Save X' label."""
	recording = {
		"uuid": "t1",
		"path": "/api/method/erpnext.accounts.utils.get_balance_on",
		"method": "GET",
		"cmd": "erpnext.accounts.utils.get_balance_on",
		"event_type": "HTTP Request",
		"duration": 15.0,
		"calls": [],
		"form_dict": {},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "erpnext.accounts.utils.get_balance_on"


def test_http_fallback_when_no_cmd(empty_context):
	"""Requests without a cmd should fall back to METHOD + path."""
	recording = {
		"uuid": "t2",
		"path": "/api/resource/Lead/LEAD-001",
		"method": "GET",
		"cmd": None,
		"event_type": "HTTP Request",
		"duration": 8.0,
		"calls": [],
		"form_dict": {},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "GET /api/resource/Lead/LEAD-001"


def test_background_job_label(empty_context):
	"""Background jobs should be labeled with 'Job: <last component>'."""
	recording = {
		"uuid": "j1",
		"path": "erpnext.accounts.doctype.gl_entry.gl_entry.post_gl_entries",
		"method": None,
		"cmd": None,
		"event_type": "Background Job",
		"duration": 320.0,
		"calls": [],
		"form_dict": None,
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "Job: post_gl_entries"
	assert result.actions[0]["event_type"] == "Background Job"


def test_submit_cmd(empty_context):
	"""Per-action table shows the raw cmd, not 'Submit Delivery Note'.
	The humanized form is verified separately in the _humanized_label
	tests below."""
	recording = {
		"uuid": "t3",
		"cmd": "frappe.client.submit",
		"method": "POST",
		"path": "/api/method/frappe.client.submit",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [],
		"form_dict": {"doctype": "Delivery Note"},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "frappe.client.submit"


# ---------------------------------------------------------------------------
# v0.5.1: per_action.humanized_label — Steps-to-Reproduce ONLY
# ---------------------------------------------------------------------------
# These tests target the `humanized_label` helper directly. It's used
# by analyze._build_auto_notes_html to render the Steps-to-Reproduce
# section. The per-action table and frontend XHR panel stay on the
# technical `_label` — per user feedback: "only humanize call name
# in step to reproduce only not on other breakdowns."


def _savedocs(doc, action="Save"):
	"""Build a savedocs recording with the given embedded doc + action."""
	import json
	return {
		"uuid": "sd",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 300,
		"calls": [],
		"form_dict": {
			"doc": json.dumps(doc),
			"action": action,
		},
	}


def test_humanized_savedocs_create_new_doc():
	rec = _savedocs({"doctype": "Sales Invoice", "__islocal": 1}, action="Save")
	assert per_action.humanized_label(rec) == "Create Sales Invoice"


def test_humanized_savedocs_save_existing_doc():
	rec = _savedocs(
		{"doctype": "Sales Invoice", "name": "SINV-00042"},
		action="Save",
	)
	assert per_action.humanized_label(rec) == "Save Sales Invoice"


def test_humanized_savedocs_submit_action():
	rec = _savedocs(
		{"doctype": "Sales Invoice", "name": "SINV-00042"},
		action="Submit",
	)
	assert per_action.humanized_label(rec) == "Submit Sales Invoice"


def test_humanized_savedocs_cancel_action():
	rec = _savedocs(
		{"doctype": "Sales Invoice", "name": "SINV-00042"},
		action="Cancel",
	)
	assert per_action.humanized_label(rec) == "Cancel Sales Invoice"


def test_humanized_savedocs_without_doctype_falls_back():
	"""Malformed payload (no parseable doctype) must fall back to
	the technical label — never emit 'Save <empty>' with trailing
	whitespace."""
	rec = {
		"uuid": "sd",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {"action": "Save"},  # no doc
	}
	label = per_action.humanized_label(rec)
	# Falls through humanization (no doctype) → _label → savedocs
	# disambiguated by :Save (v0.5.2 per-action action-suffix).
	assert label == "frappe.desk.form.save.savedocs:Save"


def test_humanized_desk_getdoc_is_open_doctype():
	rec = {
		"uuid": "gd",
		"cmd": "frappe.desk.form.load.getdoc",
		"method": "GET",
		"path": "/api/method/frappe.desk.form.load.getdoc",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [],
		"form_dict": {"doctype": "Customer", "name": "CUST-001"},
	}
	assert per_action.humanized_label(rec) == "Open Customer CUST-001"


def test_humanized_desk_search_link_is_search_doctype():
	rec = {
		"uuid": "sl",
		"cmd": "frappe.desk.search.search_link",
		"method": "GET",
		"path": "/api/method/frappe.desk.search.search_link",
		"event_type": "HTTP Request",
		"duration": 30,
		"calls": [],
		"form_dict": {"doctype": "Item", "txt": "ITEM-"},
	}
	assert per_action.humanized_label(rec) == "Search Item"


def test_humanized_client_insert_is_create():
	rec = {
		"uuid": "ci",
		"cmd": "frappe.client.insert",
		"method": "POST",
		"path": "/api/method/frappe.client.insert",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [],
		"form_dict": {"doctype": "Lead"},
	}
	assert per_action.humanized_label(rec) == "Create Lead"


# ---------------------------------------------------------------------------
# v0.5.2: per-action label disambiguation for savedocs actions
# ---------------------------------------------------------------------------
# The Desk's savedocs endpoint takes different `action` values (Save,
# Submit, Cancel, Update) that route to semantically different
# behaviors under the same cmd. Pre-v0.5.2 the per-action table
# showed the raw cmd for all of them — indistinguishable. v0.5.2
# suffixes `:<Action>` so Save vs Submit rows aren't merged visually.


def test_savedocs_save_action_appended_to_technical_label(empty_context):
	"""Save → frappe.desk.form.save.savedocs:Save in per-action table."""
	import json
	rec = {
		"uuid": "save",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 320,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Sales Invoice", "__islocal": 1}),
			"action": "Save",
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.desk.form.save.savedocs:Save"
	)


def test_savedocs_submit_action_appended_to_technical_label(empty_context):
	"""Submit → frappe.desk.form.save.savedocs:Submit. Crucially
	DIFFERENT from the Save variant, so the two rows are visually
	distinguishable in the per-action table."""
	import json
	rec = {
		"uuid": "submit",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 410,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Sales Invoice", "name": "SINV-001"}),
			"action": "Submit",
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.desk.form.save.savedocs:Submit"
	)


def test_savedocs_save_and_submit_are_distinguishable(empty_context):
	"""End-to-end regression: user's exact scenario — one session
	with one Save and one Submit on the same Sales Invoice. The
	two Optimus Action rows must have DIFFERENT action_labels
	so they render as distinct entries in the per-action table."""
	import json
	save_rec = {
		"uuid": "save",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 320,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Sales Invoice", "__islocal": 1}),
			"action": "Save",
		},
	}
	submit_rec = {
		"uuid": "submit",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 410,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Sales Invoice", "name": "SINV-001"}),
			"action": "Submit",
		},
	}
	result = per_action.analyze([save_rec, submit_rec], empty_context)
	labels = [a["action_label"] for a in result.actions]
	assert labels[0] != labels[1], (
		f"Save and Submit on the same savedocs cmd must have distinct "
		f"technical labels; got: {labels}"
	)
	assert "Save" in labels[0] and "Submit" in labels[1]


def test_savedocs_unknown_action_falls_back_to_bare_cmd(empty_context):
	"""A weird/unknown action string shouldn't produce a garbage
	label. Fall back to the bare cmd so grouping by action_label
	stays stable."""
	import json
	rec = {
		"uuid": "weird",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Sales Invoice"}),
			"action": "??garbage??",  # not in _SAVEDOCS_ACTIONS
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.desk.form.save.savedocs"
	)


def test_savedocs_missing_action_falls_back_to_bare_cmd(empty_context):
	"""No action field at all (malformed payload) → bare cmd, no
	trailing colon."""
	rec = {
		"uuid": "no-action",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {},  # no action
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.desk.form.save.savedocs"
	)


# ---------------------------------------------------------------------------
# v0.5.2: action-suffix for run_doc_method + apply_workflow
# ---------------------------------------------------------------------------
# Both cmds route multiple semantically-different operations into one
# endpoint (like savedocs). Without disambiguation, 20 distinct custom
# buttons in an ERPNext session merge into a single "run_doc_method"
# row in the per-action table. Same for workflow Approve/Reject/etc.


def test_run_doc_method_appends_method_name_in_technical_label(empty_context):
	"""Custom action buttons call run_doc_method with the real
	method name in form_dict.method. Technical label gets it as
	a suffix."""
	rec = {
		"uuid": "rdm",
		"cmd": "run_doc_method",
		"method": "POST",
		"path": "/api/method/run_doc_method",
		"event_type": "HTTP Request",
		"duration": 420,
		"calls": [],
		"form_dict": {
			"method": "make_payment_entry",
			"dt": "Sales Invoice",
			"dn": "SINV-001",
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "run_doc_method:make_payment_entry"
	)


def test_run_doc_method_variants_all_disambiguate(empty_context):
	"""Frappe routes run_doc_method via three different cmd paths
	depending on the caller. All three must get the suffix."""
	for cmd_variant in (
		"run_doc_method",
		"frappe.handler.run_doc_method",
		"frappe.client.run_doc_method",
	):
		rec = {
			"uuid": cmd_variant.replace(".", "-"),
			"cmd": cmd_variant,
			"method": "POST",
			"path": f"/api/method/{cmd_variant}",
			"event_type": "HTTP Request",
			"duration": 100,
			"calls": [],
			"form_dict": {"method": "send_email"},
		}
		result = per_action.analyze([rec], empty_context)
		assert (
			result.actions[0]["action_label"]
			== f"{cmd_variant}:send_email"
		), f"variant {cmd_variant} failed to get suffix"


def test_apply_workflow_appends_action_name_in_technical_label(empty_context):
	"""Workflow transitions use apply_workflow with form_dict.action
	('Approve', 'Reject', 'Submit for Approval'). Technical label
	gets the action as a suffix."""
	import json
	rec = {
		"uuid": "wf",
		"cmd": "frappe.model.workflow.apply_workflow",
		"method": "POST",
		"path": "/api/method/frappe.model.workflow.apply_workflow",
		"event_type": "HTTP Request",
		"duration": 180,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Leave Application", "name": "LA-001"}),
			"action": "Approve",
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.model.workflow.apply_workflow:Approve"
	)


def test_apply_workflow_allows_multi_word_action(empty_context):
	"""Workflow action names can be multi-word like 'Submit for
	Approval'. The suffix validator permits spaces up to the
	60-char cap."""
	import json
	rec = {
		"uuid": "wf2",
		"cmd": "frappe.model.workflow.apply_workflow",
		"method": "POST",
		"path": "/api/method/frappe.model.workflow.apply_workflow",
		"event_type": "HTTP Request",
		"duration": 180,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Purchase Order"}),
			"action": "Submit for Approval",
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.model.workflow.apply_workflow:Submit for Approval"
	)


def test_run_doc_method_garbage_method_falls_back(empty_context):
	"""Safety rail: a weird method name (non-identifier chars) must
	NOT produce an unbounded suffix. Fall back to bare cmd."""
	rec = {
		"uuid": "weird",
		"cmd": "run_doc_method",
		"method": "POST",
		"path": "/api/method/run_doc_method",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {"method": "<script>alert(1)</script>"},
	}
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "run_doc_method"


def test_apply_workflow_too_long_action_falls_back(empty_context):
	"""Safety rail: >60 chars fails validation, falls back to
	bare cmd."""
	import json
	rec = {
		"uuid": "long",
		"cmd": "frappe.model.workflow.apply_workflow",
		"method": "POST",
		"path": "/api/method/frappe.model.workflow.apply_workflow",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "X"}),
			"action": "A" * 61,
		},
	}
	result = per_action.analyze([rec], empty_context)
	assert (
		result.actions[0]["action_label"]
		== "frappe.model.workflow.apply_workflow"
	)


def test_run_doc_method_distinct_buttons_produce_distinct_labels(empty_context):
	"""End-to-end: three different custom buttons on Sales Invoice
	in one session must produce THREE different action_labels.
	This is the readability win over bare 'run_doc_method × 3'."""
	recs = []
	for method_name in ("make_payment_entry", "send_email", "update_items"):
		recs.append({
			"uuid": method_name,
			"cmd": "run_doc_method",
			"method": "POST",
			"path": "/api/method/run_doc_method",
			"event_type": "HTTP Request",
			"duration": 100,
			"calls": [],
			"form_dict": {
				"method": method_name,
				"dt": "Sales Invoice",
				"dn": "SINV-001",
			},
		})
	result = per_action.analyze(recs, empty_context)
	labels = [a["action_label"] for a in result.actions]
	assert len(set(labels)) == 3, (
		f"Three distinct custom-button methods must produce three "
		f"distinct labels; got: {labels}"
	)


# --- humanized_label variants for the Steps-to-Reproduce section ---


def test_humanized_run_doc_method_with_doctype_and_name():
	"""Make Payment Entry button on Sales Invoice SINV-001
	renders as English prose in the Steps-to-Reproduce section."""
	rec = {
		"uuid": "rdm",
		"cmd": "run_doc_method",
		"method": "POST",
		"path": "/api/method/run_doc_method",
		"event_type": "HTTP Request",
		"duration": 420,
		"calls": [],
		"form_dict": {
			"method": "make_payment_entry",
			"dt": "Sales Invoice",
			"dn": "SINV-001",
		},
	}
	assert (
		per_action.humanized_label(rec)
		== "Make Payment Entry on Sales Invoice SINV-001"
	)


def test_humanized_run_doc_method_without_docname():
	rec = {
		"uuid": "rdm",
		"cmd": "run_doc_method",
		"method": "POST",
		"path": "/api/method/run_doc_method",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {
			"method": "update_items",
			"dt": "Sales Invoice",
		},
	}
	assert (
		per_action.humanized_label(rec)
		== "Update Items on Sales Invoice"
	)


def test_humanized_apply_workflow_with_doctype():
	import json
	rec = {
		"uuid": "wf",
		"cmd": "frappe.model.workflow.apply_workflow",
		"method": "POST",
		"path": "/api/method/frappe.model.workflow.apply_workflow",
		"event_type": "HTTP Request",
		"duration": 180,
		"calls": [],
		"form_dict": {
			"doc": json.dumps({"doctype": "Leave Application", "name": "LA-001"}),
			"action": "Approve",
		},
	}
	assert per_action.humanized_label(rec) == "Approve Leave Application"


def test_humanization_does_not_leak_into_per_action_table(empty_context):
	"""Guard: the per-action table must stay on the technical label.
	User feedback was explicit: humanize only in Steps to Reproduce.
	This test runs a savedocs recording through the full analyzer
	pipeline and asserts the action row still has the raw cmd."""
	rec = _savedocs(
		{"doctype": "Sales Invoice", "__islocal": 1},
		action="Save",
	)
	result = per_action.analyze([rec], empty_context)
	assert len(result.actions) == 1
	# Per-action table → technical cmd (with :Save suffix for
	# Save-vs-Submit disambiguation), NOT "Create Sales Invoice".
	# Humanization stays strictly in Steps-to-Reproduce.
	assert (
		result.actions[0]["action_label"]
		== "frappe.desk.form.save.savedocs:Save"
	)
	assert "Create Sales Invoice" not in result.actions[0]["action_label"]


# ---------------------------------------------------------------------------
# v0.5.1: cmd field is empty in real recordings — derive from path
# ---------------------------------------------------------------------------
# frappe.recorder.Recorder.__init__ captures cmd at hook time, BEFORE
# frappe's REST routing sets form_dict.cmd inside handle_rpc_call. So
# every /api/method/<foo> URL ends up with cmd="" in the stored
# recording. _label must fall back to the path to preserve the
# cmd-based humanization.


def test_label_derives_cmd_from_path_when_cmd_is_empty(empty_context):
	"""Exact production recording shape: cmd="", path holds the
	/api/method/<cmd> URL. The per-action table must recover
	the raw cmd via path-derivation (NOT render the raw URL).
	The Steps-to-Reproduce humanization is verified separately
	against ``humanized_label``."""
	import json as _json
	recording = {
		"uuid": "prod",
		"cmd": "",  # EMPTY — matches production
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 774.8,
		"calls": [],
		"form_dict": {
			"doc": _json.dumps({"doctype": "Sales Invoice", "__islocal": 1}),
			"action": "Save",
		},
	}
	result = per_action.analyze([recording], empty_context)
	# Per-action table gets the technical cmd, disambiguated by
	# :Save (v0.5.2). Still technical — NOT the humanized form.
	assert (
		result.actions[0]["action_label"]
		== "frappe.desk.form.save.savedocs:Save"
	)
	# humanized_label — used only in Steps-to-Reproduce — still
	# produces the English form for this same recording.
	assert per_action.humanized_label(recording) == "Create Sales Invoice"


def test_label_derives_v2_api_method(empty_context):
	"""/api/v2/method/<foo> parse works too — per-action table
	shows the raw cmd, humanized_label produces the English form."""
	recording = {
		"uuid": "v2",
		"cmd": "",
		"method": "POST",
		"path": "/api/v2/method/frappe.client.submit",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [],
		"form_dict": {"doctype": "Delivery Note"},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "frappe.client.submit"
	assert per_action.humanized_label(recording) == "Submit Delivery Note"


def test_label_non_method_url_falls_back_to_method_path(empty_context):
	"""URLs that aren't /method/<cmd> (REST resource, static files,
	Desk pages) have no cmd to derive. Fall back cleanly."""
	recording = {
		"uuid": "rest",
		"cmd": "",
		"method": "GET",
		"path": "/api/resource/Sales Invoice/SI-001",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [],
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "GET /api/resource/Sales Invoice/SI-001"


def test_derive_cmd_from_path_helper_unit():
	"""Direct unit test of the cmd-from-path helper."""
	from optimus.analyzers.per_action import _derive_cmd_from_path

	assert _derive_cmd_from_path(
		"/api/method/frappe.client.save"
	) == "frappe.client.save"
	assert _derive_cmd_from_path(
		"/api/v2/method/frappe.client.submit"
	) == "frappe.client.submit"
	# Trailing slash tolerated
	assert _derive_cmd_from_path(
		"/api/method/foo.bar/"
	) == "foo.bar"
	# Query string stripped
	assert _derive_cmd_from_path(
		"/api/method/foo.bar?doctype=Item&name=X"
	) == "foo.bar"
	# Non-method URLs return empty string
	assert _derive_cmd_from_path("/api/resource/Item/X") == ""
	assert _derive_cmd_from_path("/app/home") == ""
	assert _derive_cmd_from_path("/assets/foo.js") == ""
	# Empty / None
	assert _derive_cmd_from_path("") == ""
	assert _derive_cmd_from_path(None) == ""


def test_empty_recordings_list(empty_context):
	result = per_action.analyze([], empty_context)
	assert result.actions == []
	assert result.findings == []


def test_action_emits_no_findings(n_plus_one_recording, empty_context):
	"""per_action only builds Optimus Action rows, no findings."""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	assert result.findings == []
