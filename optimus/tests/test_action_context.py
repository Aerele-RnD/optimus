# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the render-time action/finding context enrichment —
``renderer._attach_action_context`` and its helpers: which document a
save/submit-style action touched (from the recording's form_dict) and which
doc-event lifecycle hook a slow function fired in (from frappe's doc_events).
"""

import json

from optimus import renderer


class TestModuleFromFilename:
	def test_app_relative_path(self):
		assert renderer._module_from_filename("ugly_code/python/common.py") == "ugly_code.python.common"

	def test_frappe_core_path(self):
		assert renderer._module_from_filename("frappe/model/document.py") == "frappe.model.document"

	def test_strips_leading_slash_and_double_slash(self):
		assert renderer._module_from_filename("/abs/x/y.py") == "abs.x.y"
		assert renderer._module_from_filename("a//b.py") == "a.b"

	def test_non_py_kept_as_is(self):
		assert renderer._module_from_filename("a/b/c") == "a.b.c"

	def test_empty_or_none(self):
		assert renderer._module_from_filename("") == ""
		assert renderer._module_from_filename(None) == ""


class TestExtractTargetDoc:
	def test_savedocs_doc_json_string(self):
		fd = {"doc": json.dumps({"doctype": "Sales Invoice", "name": "SINV-1", "docstatus": 1}), "action": "Submit"}
		assert renderer._extract_target_doc(fd) == {"doctype": "Sales Invoice", "name": "SINV-1"}

	def test_doc_already_a_dict(self):
		assert renderer._extract_target_doc({"doc": {"doctype": "Lead", "name": "LEAD-9"}}) == {"doctype": "Lead", "name": "LEAD-9"}

	def test_dt_dn_run_doc_method(self):
		assert renderer._extract_target_doc({"dt": "Quotation", "dn": "QTN-1", "method": "x"}) == {"doctype": "Quotation", "name": "QTN-1"}

	def test_docs_list_picks_first_with_doctype(self):
		fd = {"docs": json.dumps([{"foo": 1}, {"doctype": "Item", "name": "ITM-1"}])}
		assert renderer._extract_target_doc(fd) == {"doctype": "Item", "name": "ITM-1"}

	def test_bare_doctype_name(self):
		assert renderer._extract_target_doc({"doctype": "User", "name": "a@b.c"}) == {"doctype": "User", "name": "a@b.c"}

	def test_doctype_without_name(self):
		# get_list-style: doctype but no single name → name None.
		assert renderer._extract_target_doc({"doctype": "User", "filters": "{}"}) == {"doctype": "User", "name": None}
		assert renderer._extract_target_doc({"doc": json.dumps({"doctype": "Note"})}) == {"doctype": "Note", "name": None}

	def test_none_when_no_doc_shape(self):
		for fd in (
			{"action": "Submit"},
			{},
			None,
			"not a dict",
			{"doc": "not json"},
			{"doc": json.dumps({"foo": "bar"})},   # JSON but no doctype
			{"doc": json.dumps([1, 2, 3])},          # list of non-dicts
			{"some_unrelated_key": "value"},
		):
			assert renderer._extract_target_doc(fd) is None


class TestBuildDocEventHookIndex:
	def test_flattens_doctype_event_methods(self):
		idx = renderer._build_doc_event_hook_index({
			"Sales Invoice": {"validate": ["ugly_code.x.f"], "on_submit": ["a.b.g", "a.b.g2"]},
			"*": {"validate": "a.b.h"},  # string, not list — must still be handled
		})
		assert idx == {
			"ugly_code.x.f": [("Sales Invoice", "validate")],
			"a.b.g": [("Sales Invoice", "on_submit")],
			"a.b.g2": [("Sales Invoice", "on_submit")],
			"a.b.h": [("*", "validate")],
		}

	def test_same_method_under_multiple_doctype_event_pairs(self):
		idx = renderer._build_doc_event_hook_index({"A": {"validate": ["x.f"]}, "B": {"on_submit": ["x.f"]}})
		assert sorted(idx["x.f"]) == [("A", "validate"), ("B", "on_submit")]

	def test_garbage_inputs(self):
		assert renderer._build_doc_event_hook_index(None) == {}
		assert renderer._build_doc_event_hook_index("nope") == {}
		assert renderer._build_doc_event_hook_index({"A": "not a dict"}) == {}
		assert renderer._build_doc_event_hook_index({"A": {"validate": 123}}) == {}
		assert renderer._build_doc_event_hook_index({"A": {"validate": [123, "x.f"]}}) == {"x.f": [("A", "validate")]}


class TestFindingHookEvents:
	def test_match_module_level_function(self):
		idx = {"ugly_code.python.common.looped_validate": [("Sales Invoice", "validate")]}
		detail = {"function": "looped_validate", "filename": "ugly_code/python/common.py"}
		assert renderer._finding_hook_events(detail, idx) == [{"doctype": "Sales Invoice", "event": "validate"}]

	def test_wildcard_resolved_to_action_doctype(self):
		idx = {"a.b.h": [("*", "on_submit")]}
		detail = {"function": "h", "filename": "a/b.py"}
		assert renderer._finding_hook_events(detail, idx, action_doctype="Quotation") == [{"doctype": "Quotation", "event": "on_submit"}]

	def test_wildcard_kept_when_no_action_doctype(self):
		idx = {"a.b.h": [("*", "on_submit")]}
		assert renderer._finding_hook_events({"function": "h", "filename": "a/b.py"}, idx) == [{"doctype": "*", "event": "on_submit"}]

	def test_class_method_uses_bare_name(self):
		# "SalesInvoice.validate" → bare "validate" → looks up "<module>.validate".
		idx = {"erpnext.controllers.x.validate": [("X", "validate")]}
		detail = {"function": "SalesInvoice.validate", "filename": "erpnext/controllers/x.py"}
		assert renderer._finding_hook_events(detail, idx) == [{"doctype": "X", "event": "validate"}]

	def test_dedupes_concrete_and_wildcard_collapsing_to_same_pair(self):
		idx = {"a.b.f": [("A", "validate"), ("A", "validate"), ("*", "validate")]}
		assert renderer._finding_hook_events({"function": "f", "filename": "a/b.py"}, idx, action_doctype="A") == [{"doctype": "A", "event": "validate"}]

	def test_no_match_or_bad_input(self):
		assert renderer._finding_hook_events({"function": "nope", "filename": "x/y.py"}, {"x.z.q": [("A", "validate")]}) == []
		assert renderer._finding_hook_events({"function": "f"}, {"a.f": [("A", "validate")]}) == []          # no filename
		assert renderer._finding_hook_events({"filename": "a/b.py"}, {"a.b.f": [("A", "validate")]}) == []   # no function
		assert renderer._finding_hook_events({"function": "f", "filename": "a/b.py"}, {}) == []               # empty index
		assert renderer._finding_hook_events("nope", {"a": [("A", "v")]}) == []
		assert renderer._finding_hook_events({"function": "f", "filename": "a/b.py"}, None) == []


class TestAttachActionContext:
	def _act(self, idx, **kw):
		base = {
			"action_label": "", "event_type": "HTTP Request", "http_method": "", "path": "",
			"recording_uuid": "", "duration_ms": 0, "queries_count": 0, "query_time_ms": 0, "slowest_query_ms": 0,
		}
		base.update(kw)
		base["idx"] = idx
		return base

	def test_attaches_target_doc_to_action_and_to_its_finding(self):
		actions = [self._act(0, action_label="frappe.desk.form.save.savedocs:Submit", recording_uuid="r0")]
		findings = [{"finding_type": "Hook Bottleneck", "action_ref": "0",
		             "technical_detail": {"function": "looped_validate", "filename": "ugly_code/python/common.py"}}]
		recs = {"r0": {"form_dict": {"doc": json.dumps({"doctype": "Sales Invoice", "name": "SINV-1"}), "action": "Submit"}}}
		renderer._attach_action_context(actions, findings, recs)
		assert actions[0]["target_doc"] == {"doctype": "Sales Invoice", "name": "SINV-1"}
		assert findings[0]["technical_detail"]["target_doc"] == {"doctype": "Sales Invoice", "name": "SINV-1"}

	def test_action_without_doc_gets_none_and_finding_key_omitted(self):
		actions = [self._act(0, action_label="frappe.client.get_value", recording_uuid="r0")]
		findings = [{"finding_type": "Slow Query", "action_ref": "0", "technical_detail": {"normalized_query": "SELECT 1"}}]
		recs = {"r0": {"form_dict": {"some_key": "x"}}}  # nothing doc-shaped
		renderer._attach_action_context(actions, findings, recs)
		assert actions[0]["target_doc"] is None
		assert "target_doc" not in findings[0]["technical_detail"]

	def test_session_wide_finding_with_no_usable_action_ref(self):
		actions = [self._act(0, recording_uuid="r0")]
		findings = [
			{"finding_type": "Repeated Hot Frame", "action_ref": None, "technical_detail": {"function": "x", "filename": "a/b.py"}},
			{"finding_type": "Repeated Hot Frame", "action_ref": "", "technical_detail": {"function": "y", "filename": "a/b.py"}},
			{"finding_type": "Repeated Hot Frame", "action_ref": "abc", "technical_detail": {"function": "z", "filename": "a/b.py"}},
			{"finding_type": "Repeated Hot Frame", "action_ref": "99", "technical_detail": {"function": "w", "filename": "a/b.py"}},  # no such idx
		]
		recs = {"r0": {"form_dict": {"doc": json.dumps({"doctype": "X", "name": "X-1"})}}}
		renderer._attach_action_context(actions, findings, recs)
		for f in findings:
			assert "target_doc" not in f["technical_detail"]

	def test_missing_recording_or_no_form_dict(self):
		actions = [self._act(0, recording_uuid="gone"), self._act(1, recording_uuid="r1")]
		recs = {"r1": {}}  # recording present but no form_dict
		renderer._attach_action_context(actions, [], recs)
		assert actions[0]["target_doc"] is None and actions[1]["target_doc"] is None

	def test_noop_safe_on_empty_or_none(self):
		renderer._attach_action_context([], [], {})
		renderer._attach_action_context(None, None, None)
