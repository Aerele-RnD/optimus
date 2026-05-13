# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the "Doc-event lifecycle" breakdown — re-grouping the slow
call-tree findings by DocType → lifecycle event (validate / on_submit / …),
tagging each as a registered ``doc_events`` hook vs a controller method
override, and surfacing cascaded DocTypes. All pure functions in
``renderer`` — no running site needed.
"""

from optimus import renderer


class TestDoctypeFromControllerPath:
	def test_app_relative_controller_path(self):
		assert renderer._doctype_from_controller_path(
			"erpnext/accounts/doctype/sales_invoice/sales_invoice.py"
		) == "Sales Invoice"

	def test_absolute_controller_path_titlecase_limitation(self):
		# .title() mangles multi-cap names ("gl_entry" → "Gl Entry") — same as
		# frappe.unscrub. Accepted.
		assert renderer._doctype_from_controller_path(
			"/Users/x/apps/erpnext/erpnext/accounts/doctype/gl_entry/gl_entry.py"
		) == "Gl Entry"

	def test_bench_relative_controller_path(self):
		assert renderer._doctype_from_controller_path(
			"apps/myapp/myapp/sales/doctype/quote/quote.py"
		) == "Quote"

	def test_non_controller_paths(self):
		assert renderer._doctype_from_controller_path("frappe/model/document.py") is None
		assert renderer._doctype_from_controller_path("ugly_code/python/common.py") is None
		assert renderer._doctype_from_controller_path("a/b.py") is None

	def test_empty_or_none_or_doctype_at_end(self):
		assert renderer._doctype_from_controller_path("") is None
		assert renderer._doctype_from_controller_path(None) is None
		assert renderer._doctype_from_controller_path("erpnext/foo/doctype") is None  # nothing after "doctype"


class TestFindingLifecycleBindings:
	def test_from_hook_events(self):
		f = {"technical_detail": {"hook_events": [{"doctype": "Sales Invoice", "event": "validate"}]}}
		assert renderer._finding_lifecycle_bindings(f) == [("Sales Invoice", "validate", "doc_events hook")]

	def test_multiple_hook_events(self):
		f = {"technical_detail": {"hook_events": [
			{"doctype": "Sales Invoice", "event": "validate"},
			{"doctype": "Sales Invoice", "event": "on_submit"},
		]}}
		assert renderer._finding_lifecycle_bindings(f) == [
			("Sales Invoice", "validate", "doc_events hook"),
			("Sales Invoice", "on_submit", "doc_events hook"),
		]

	def test_controller_override_from_callsite(self):
		f = {"technical_detail": {
			"function": "SalesInvoice.on_submit",
			"callsite": {"function": "SalesInvoice.on_submit",
			             "filename": "erpnext/accounts/doctype/sales_invoice/sales_invoice.py", "lineno": 1200},
		}}
		assert renderer._finding_lifecycle_bindings(f) == [("Sales Invoice", "on_submit", "controller override")]

	def test_controller_override_bare_function_name(self):
		# Older Python / pyinstrument may give the bare co_name ("validate")
		# rather than the qualname ("SalesInvoice.validate").
		f = {"technical_detail": {"function": "validate",
		     "callsite": {"function": "validate", "filename": "myapp/x/doctype/foo/foo.py", "lineno": 9}}}
		assert renderer._finding_lifecycle_bindings(f) == [("Foo", "validate", "controller override")]

	def test_hook_and_override_dedupe_to_one(self):
		# Same (doctype, event) from both paths → one binding, hook kind kept
		# (hooks listed first).
		f = {"technical_detail": {
			"hook_events": [{"doctype": "X", "event": "validate"}],
			"function": "validate", "callsite": {"function": "validate", "filename": "app/x/doctype/x/x.py"},
		}}
		assert renderer._finding_lifecycle_bindings(f) == [("X", "validate", "doc_events hook")]

	def test_non_lifecycle_function_name(self):
		f = {"technical_detail": {"function": "get_total",
		     "callsite": {"function": "get_total", "filename": "erpnext/accounts/doctype/sales_invoice/sales_invoice.py"}}}
		assert renderer._finding_lifecycle_bindings(f) == []

	def test_lifecycle_name_but_not_a_controller_path(self):
		f = {"technical_detail": {"function": "validate",
		     "callsite": {"function": "validate", "filename": "myapp/utils.py"}}}
		assert renderer._finding_lifecycle_bindings(f) == []

	def test_bad_inputs(self):
		assert renderer._finding_lifecycle_bindings({}) == []
		assert renderer._finding_lifecycle_bindings({"technical_detail": None}) == []
		assert renderer._finding_lifecycle_bindings("nope") == []
		assert renderer._finding_lifecycle_bindings({"technical_detail": {}}) == []


def _f(finding_type, ms, *, severity="Medium", action_ref="0", function=None, filename=None, lineno=None,
       cumulative_ms=None, hook_events=None, target_doc=None):
	cs = {}
	if function or filename:
		cs = {"function": function, "filename": filename, "lineno": lineno}
	td = {"function": function, "filename": filename, "lineno": lineno, "callsite": cs or None}
	if cumulative_ms is not None:
		td["cumulative_ms"] = cumulative_ms
	if hook_events is not None:
		td["hook_events"] = hook_events
	if target_doc is not None:
		td["target_doc"] = target_doc
	return {"finding_type": finding_type, "severity": severity, "estimated_impact_ms": ms,
	        "action_ref": action_ref, "technical_detail": td}


class TestBuildDocEventBreakdown:
	def test_groups_merges_and_sorts(self):
		findings = [
			# looped_validate (doc_events hook on SI/validate) in two actions → merged ×2.
			_f("Hook Bottleneck", 705, severity="Medium", action_ref="0", function="looped_validate",
			   filename="ugly_code/python/common.py", lineno=6, cumulative_ms=705,
			   hook_events=[{"doctype": "Sales Invoice", "event": "validate"}],
			   target_doc={"doctype": "Sales Invoice", "name": "SINV-1"}),
			_f("Hook Bottleneck", 697, severity="High", action_ref="1", function="looped_validate",
			   filename="ugly_code/python/common.py", lineno=6, cumulative_ms=697,
			   hook_events=[{"doctype": "Sales Invoice", "event": "validate"}],
			   target_doc={"doctype": "Sales Invoice", "name": "SINV-1"}),
			# SalesInvoice.on_submit — controller override, SI/on_submit.
			_f("Slow Hot Path", 450, severity="Medium", action_ref="1", function="SalesInvoice.on_submit",
			   filename="erpnext/accounts/doctype/sales_invoice/sales_invoice.py", lineno=1200, cumulative_ms=450,
			   target_doc={"doctype": "Sales Invoice", "name": "SINV-1"}),
			# GLEntry.validate — controller override on GL Entry, touched during a SI submit.
			_f("Slow Hot Path", 22, severity="Low", action_ref="1", function="GLEntry.validate",
			   filename="erpnext/accounts/doctype/gl_entry/gl_entry.py", lineno=50, cumulative_ms=22,
			   target_doc={"doctype": "Sales Invoice", "name": "SINV-1"}),
			# A non-lifecycle helper → not in the breakdown.
			_f("Slow Hot Path", 99, action_ref="2", function="some_helper", filename="myapp/util.py", lineno=3, cumulative_ms=99),
		]
		bd = renderer._build_doc_event_breakdown(findings)
		assert bd["count"] == 2
		assert bd["method_count"] == 3
		# Sort: save-target (Sales Invoice) before the cascaded one (Gl Entry).
		assert [g["doctype"] for g in bd["doctypes"]] == ["Sales Invoice", "Gl Entry"]

		si = bd["doctypes"][0]
		assert si["is_save_target"] is True and si["touched_during"] == []
		assert si["method_count"] == 2
		# Within SI: validate (1402ms) before on_submit (450ms).
		assert [e["event"] for e in si["events"]] == ["validate", "on_submit"]
		val = si["events"][0]
		assert val["total_ms"] == 1402.0
		m = val["methods"][0]
		assert m["function"] == "looped_validate" and m["count"] == 2 and m["ms"] == 1402.0
		assert m["kind"] == "doc_events hook" and m["severity"] == "High"  # max(Medium, High)
		on_sub = si["events"][1]
		assert on_sub["methods"][0]["function"] == "SalesInvoice.on_submit"
		assert on_sub["methods"][0]["kind"] == "controller override"

		gl = bd["doctypes"][1]
		assert gl["doctype"] == "Gl Entry"
		assert gl["is_save_target"] is False and gl["touched_during"] == ["Sales Invoice"]
		assert gl["events"][0]["methods"][0]["kind"] == "controller override"

	def test_controller_override_supersedes_hook_kind_on_merge(self):
		# Two findings for the same (function, filename) under the same event —
		# one bound via hook_events, one via controller-path → merged record's
		# kind ends up "controller override" (the more specific one).
		findings = [
			_f("Hook Bottleneck", 10, function="validate", filename="app/x/doctype/x/x.py", lineno=1,
			   cumulative_ms=10, hook_events=[{"doctype": "X", "event": "validate"}], target_doc={"doctype": "X"}),
		]
		# Add a second finding with the same fn/file but only the controller-path binding.
		findings.append(_f("Slow Hot Path", 5, function="validate", filename="app/x/doctype/x/x.py", lineno=1,
		                   cumulative_ms=5, target_doc={"doctype": "X"}))
		bd = renderer._build_doc_event_breakdown(findings)
		x = bd["doctypes"][0]
		m = x["events"][0]["methods"][0]
		assert m["count"] == 2 and m["ms"] == 15.0
		assert m["kind"] == "controller override"

	def test_no_action_target_doc_means_no_cascade_note(self):
		findings = [
			_f("Slow Hot Path", 30, function="GLEntry.validate",
			   filename="erpnext/accounts/doctype/gl_entry/gl_entry.py", lineno=50, cumulative_ms=30,
			   action_ref="", target_doc=None),
		]
		bd = renderer._build_doc_event_breakdown(findings)
		assert bd["count"] == 1
		g = bd["doctypes"][0]
		assert g["is_save_target"] is False and g["touched_during"] == []

	def test_empty_or_no_lifecycle_findings(self):
		assert renderer._build_doc_event_breakdown([]) == {"doctypes": [], "count": 0, "method_count": 0}
		assert renderer._build_doc_event_breakdown(None) == {"doctypes": [], "count": 0, "method_count": 0}
		assert renderer._build_doc_event_breakdown([
			_f("Slow Hot Path", 99, function="some_helper", filename="myapp/util.py", lineno=3, cumulative_ms=99),
			_f("N+1 Query", 50, function=None, filename=None),
		]) == {"doctypes": [], "count": 0, "method_count": 0}
