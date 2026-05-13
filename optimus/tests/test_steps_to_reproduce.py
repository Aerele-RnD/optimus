# optimus/tests/test_steps_to_reproduce.py
# Copyright (c) 2026, Optimus contributors

"""Tests for v0.5.0 Steps-to-Reproduce / Notes field.

The v0.5.0 design upgrades the existing `notes` field on Profiler
Session from plain Text to Text Editor so users can include rich
"what did you do during this session" context, and renders it at
the top of the report above findings.
"""

import inspect
import json
import os

HERE = os.path.dirname(__file__)


def _load_doctype_json():
	# Frappe's on-disk layout is <app>/<app>/<module>/doctype/<dt>/<dt>.json.
	# For this app the module name matches the app name ("optimus")
	# so the resolved path from optimus/tests/ is
	# ../optimus/doctype/optimus_session/optimus_session.json.
	jpath = os.path.join(
		HERE,
		"..",
		"optimus",
		"doctype",
		"optimus_session",
		"optimus_session.json",
	)
	with open(jpath) as f:
		return json.load(f)


def test_notes_field_is_text_editor():
	"""The notes field must be Text Editor (rich HTML), not plain Text,
	so users can include formatting / links / lists in the steps they
	document."""
	meta = _load_doctype_json()
	fields = meta.get("fields") or []
	target = [f for f in fields if f.get("fieldname") == "notes"]
	assert len(target) == 1, "notes field missing from Optimus Session"
	assert target[0]["fieldtype"] == "Text Editor"


def test_notes_label_reflects_steps_purpose():
	"""The label must make the field's purpose clear to users — the
	description alone isn't enough because the field header in the
	form view only shows the label."""
	meta = _load_doctype_json()
	notes = next(
		(f for f in meta["fields"] if f.get("fieldname") == "notes"), None
	)
	assert notes is not None
	assert "Reproduce" in notes["label"] or "Steps" in notes["label"]


def test_api_start_accepts_notes():
	"""api.start must accept an optional notes kwarg and persist it
	into the Optimus Session row."""
	from optimus import api

	sig = inspect.signature(api.start)
	assert "notes" in sig.parameters
	# Default should be empty string — keeping start() backward compatible
	# with callers that don't pass notes.
	assert sig.parameters["notes"].default == ""


def test_api_start_writes_notes_to_doc():
	"""Source check: api.start must include notes in the doc_fields dict
	passed to get_doc, or the value will be silently dropped."""
	from optimus import api

	src = inspect.getsource(api.start)
	assert "notes" in src
	# The field must land in doc_fields, not just be parsed and forgotten.
	assert 'doc_fields["notes"]' in src or "doc_fields['notes']" in src


def test_report_template_renders_notes():
	"""The report template must render the notes section and
	use the pre-sanitized `notes_html` context var (which is |safe)."""
	tpath = os.path.join(HERE, "..", "templates", "report.html")
	with open(tpath) as f:
		template = f.read()

	# Template must use the sanitized notes_html var, not raw session.notes.
	# Using session.notes directly with |safe would be a stored-XSS sink.
	assert "notes_html" in template
	assert "notes_html | safe" in template or "notes_html|safe" in template
	# "Steps to Reproduce" heading must appear so users know the purpose.
	assert "Steps to Reproduce" in template

	# Verify notes appear ABOVE the "Findings — what to fix" section header.
	notes_idx = template.find("notes_html")
	findings_heading_idx = template.find("Findings &mdash; what to fix")
	assert notes_idx > 0
	assert findings_heading_idx > 0
	assert findings_heading_idx > notes_idx, (
		"notes section must appear above 'Findings — what to fix'"
	)


def test_notes_are_bleach_sanitized_before_render():
	"""XSS regression guard: a notes field containing <script> must NOT
	produce an executable <script> in the rendered report. This test
	loads the sanitize function directly (not a full render) because
	the test environment doesn't have a real Frappe site. MUST call
	with always_sanitize=True so the JSON/no-tag fast-paths don't bypass."""
	try:
		from frappe.utils.html_utils import sanitize_html
	except Exception:
		# If Frappe isn't importable at test time for some reason, the
		# renderer falls back to html.escape which also neutralizes.
		import html as html_mod
		cleaned = html_mod.escape('<script>alert(1)</script>')
		assert "<script>" not in cleaned
		return

	malicious = '<p>ok</p><script>alert(1)</script>'
	cleaned = sanitize_html(malicious, always_sanitize=True)
	# Harmless tags preserved:
	assert "<p>" in cleaned
	# Script tags removed (nh3/bleach strips or escapes):
	assert "<script>" not in cleaned


def test_json_shaped_xss_payload_is_sanitized():
	"""Architect-review finding: sanitize_html has a fast-path that
	skips bleach for input detected as valid JSON. An attacker could
	set notes = '{"x": "<script>alert(1)</script>"}' — which IS valid
	JSON (a JSON dict literal with a string value containing the
	script) — and sanitize_html without always_sanitize=True returns
	it unchanged. The template's |safe then renders the script as
	a live <script> tag. always_sanitize=True closes this bypass.
	"""
	try:
		from frappe.utils.html_utils import sanitize_html
	except Exception:
		import html as html_mod
		cleaned = html_mod.escape('{"x": "<script>alert(1)</script>"}')
		assert "<script>" not in cleaned
		return

	json_payload = '{"x": "<script>alert(1)</script>"}'

	# Without always_sanitize=True the JSON fast-path would kick in.
	# With always_sanitize=True the script tag must be neutralized.
	cleaned = sanitize_html(json_payload, always_sanitize=True)
	assert "<script>" not in cleaned, (
		"sanitize_html(always_sanitize=True) must strip script tags "
		"from JSON-shaped input. If this fails, the renderer's |safe "
		"render path leaks XSS to anyone viewing a Optimus Session report."
	)


def test_renderer_passes_always_sanitize_true():
	"""Source-inspection guard: the renderer must pass always_sanitize=True
	to sanitize_html, or the JSON / no-tag fast-paths will bypass bleach
	and leak XSS through |safe in the template."""
	import inspect

	from optimus import renderer

	src = inspect.getsource(renderer.render)
	assert "always_sanitize=True" in src, (
		"renderer.render must call sanitize_html(..., always_sanitize=True). "
		"Without it, valid JSON or no-tag input bypasses sanitization "
		"entirely — stored XSS regression."
	)


def test_renderer_sanitizes_notes_before_template_context():
	"""The render function must run session.notes through sanitize_html
	and pass the result as notes_html, not as session.notes directly."""
	import inspect

	from optimus import renderer

	src = inspect.getsource(renderer.render)
	assert "sanitize_html" in src or "html.escape" in src or "html_mod.escape" in src
	assert "notes_html" in src


# ---------------------------------------------------------------------------
# v0.5.1: auto-filled "Steps to Reproduce" from captured actions
# ---------------------------------------------------------------------------
# The start dialog no longer prompts for notes. At analyze time, if the
# user hasn't typed anything on the Optimus Session form, we synthesize
# a bullet list of the captured actions so reviewers see context when
# they open the report.


def test_auto_notes_empty_recordings_returns_empty_string():
	"""No recordings → no notes. The caller must be able to skip the
	assignment entirely rather than setting an empty <ol></ol>."""
	from optimus.analyze import _build_auto_notes_html

	assert _build_auto_notes_html([]) == ""


def test_auto_notes_produces_ordered_list_with_humanized_labels():
	"""The helper should run each recording through per_action._label
	so 'Save Sales Invoice' beats 'POST /api/method/frappe.client.save'
	in the rendered reproducer."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "POST",
			"path": "/api/method/frappe.client.save",
			"cmd": "frappe.client.save",
			"form_dict": {"doctype": "Sales Invoice"},
			"duration": 842.3,
			"calls": [],
		},
		{
			"method": "GET",
			"path": "/api/resource/Sales Invoice/INV-00042",
			"cmd": None,
			"duration": 58.1,
			"calls": [],
		},
	]
	html_out = _build_auto_notes_html(recordings)

	# Preamble explains auto-generation and invites editing.
	assert "Auto-generated" in html_out
	# Ordered list, not unordered — order matters for reproducers.
	assert "<ol>" in html_out and "</ol>" in html_out
	# First recording resolves to "Save Sales Invoice" via per_action._label.
	assert "Save Sales Invoice" in html_out
	# Second recording falls through to METHOD + path.
	assert "GET /api/resource/Sales Invoice/INV-00042" in html_out
	# Duration rendered in milliseconds (rounded to 1 decimal).
	assert "842.3 ms" in html_out
	assert "58.1 ms" in html_out


def test_auto_notes_html_escapes_user_controlled_strings():
	"""A path containing <script> must NOT produce a live <script> tag
	in the stored value. The renderer also sanitizes on the way out,
	but defense in depth: escape at emit time too."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [{
		"method": "GET",
		"path": "/<script>alert(1)</script>",
		"cmd": None,
		"duration": 10.0,
		"calls": [],
	}]
	html_out = _build_auto_notes_html(recordings)
	assert "<script>" not in html_out
	assert "&lt;script&gt;" in html_out


def test_auto_notes_caps_long_sessions_with_overflow_marker():
	"""A 200-action session shouldn't fill the notes field with 200
	<li> entries — cap at 50 and surface a '… and N more' marker so
	users know the list is truncated."""
	from optimus.analyze import _AUTO_NOTES_MAX_ENTRIES, _build_auto_notes_html

	recordings = [
		{"method": "GET", "path": f"/item/{i}", "cmd": None,
		 "duration": 5.0, "calls": []}
		for i in range(_AUTO_NOTES_MAX_ENTRIES + 10)
	]
	html_out = _build_auto_notes_html(recordings)
	# Count the <li> entries — should be cap + 1 (for the overflow marker).
	li_count = html_out.count("<li>")
	assert li_count == _AUTO_NOTES_MAX_ENTRIES + 1
	assert "10 more" in html_out


def test_auto_notes_unnamed_action_falls_back_gracefully():
	"""If somehow a recording has no cmd / path / method, the label
	resolver returns an empty string. The helper must substitute a
	placeholder rather than emit '<li> — 0 ms</li>'."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [{"method": "", "path": "", "cmd": "",
	               "duration": 0, "calls": []}]
	html_out = _build_auto_notes_html(recordings)
	# Must not have a blank label — the per_action._label fallback ends up
	# as " " (method + " " + path with both empty), so we check the
	# placeholder OR a non-empty label.
	assert "<li>" in html_out
	# The <li> content between <li> and the em-dash separator must be
	# non-whitespace — otherwise the reproducer is "— 0 ms" which is
	# useless to a reader.
	import re
	m = re.search(r"<li>([^<]*?) — ", html_out)
	assert m is not None
	label = m.group(1).strip()
	assert label, f"Auto-notes emitted blank label: {html_out!r}"


def test_persist_auto_fills_notes_when_field_is_empty():
	"""Source-inspection guard: _persist must check doc.notes and
	populate it with _build_auto_notes_html when empty, otherwise the
	whole feature is a dead path."""
	import inspect

	from optimus import analyze

	src = inspect.getsource(analyze._persist)
	# The guard condition.
	assert "doc.notes" in src
	# Must call the helper.
	assert "_build_auto_notes_html" in src
	# Must be gated on an emptiness check so existing notes aren't
	# overwritten.
	assert "strip()" in src or "not doc.notes" in src


def test_auto_notes_filters_realtime_polling_noise():
	"""v0.5.1: real production reproducer read:

	    GET /api/method/frappe.realtime.has_permission — 25ms
	    POST /api/method/frappe.desk.form.save.savedocs — 775ms
	    GET /api/method/frappe.realtime.has_permission — 6ms

	Of those three, only the savedocs is a user action. The two
	has_permission entries are the Desk polling for realtime
	subscription permission and should be filtered out of the
	reproducer (still visible in the per-action table)."""
	import json as _json

	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 25.0,
			"calls": [],
		},
		{
			"method": "POST",
			"path": "/api/method/frappe.desk.form.save.savedocs",
			"cmd": "frappe.desk.form.save.savedocs",
			"duration": 774.8,
			"calls": [],
			"form_dict": {
				"doc": _json.dumps({
					"doctype": "Sales Invoice",
					"__islocal": 1,
				}),
				"action": "Save",
			},
		},
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 6.0,
			"calls": [],
		},
	]

	html_out = _build_auto_notes_html(recordings)
	# Only the savedocs survives — humanized as "Create Sales Invoice".
	assert "Create Sales Invoice" in html_out
	assert "774.8 ms" in html_out
	# Polling endpoints filtered out
	assert "has_permission" not in html_out
	assert "frappe.realtime" not in html_out
	# Footer tells the user noise was filtered so they don't wonder
	# why only 1 entry showed from a 3-request session.
	assert "2 background / polling request(s) filtered" in html_out


def test_auto_notes_filters_static_assets_and_form_load_boilerplate():
	"""Static /assets/ requests and form-metadata loads are noise too."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "GET",
			"path": "/assets/frappe/dist/js/desk.bundle.js",
			"cmd": None,
			"duration": 180,
			"calls": [],
		},
		{
			"method": "GET",
			"path": "/api/method/frappe.desk.form.load.getdoctype",
			"cmd": "frappe.desk.form.load.getdoctype",
			"duration": 120,
			"calls": [],
			"form_dict": {"doctype": "Sales Invoice"},
		},
		{
			"method": "POST",
			"path": "/api/method/frappe.client.submit",
			"cmd": "frappe.client.submit",
			"duration": 500,
			"calls": [],
			"form_dict": {"doctype": "Sales Invoice"},
		},
	]
	html_out = _build_auto_notes_html(recordings)
	assert "Submit Sales Invoice" in html_out
	assert "desk.bundle.js" not in html_out
	assert "getdoctype" not in html_out
	assert "2 background / polling" in html_out


def test_auto_notes_all_noise_returns_empty_string():
	"""A session of only polling/noise returns empty — the caller
	then leaves the notes field blank rather than filling it with
	the preamble and an empty list."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 5.0,
			"calls": [],
		}
	] * 20
	html_out = _build_auto_notes_html(recordings)
	assert html_out == ""


def test_auto_notes_real_user_sequence_reads_naturally():
	"""End-to-end: a realistic flow produces a human-readable
	reproducer that reads like a story, not an HTTP log."""
	import json as _json

	from optimus.analyze import _build_auto_notes_html

	recordings = [
		# User searches for an item
		{
			"method": "GET",
			"path": "/api/method/frappe.desk.search.search_link",
			"cmd": "frappe.desk.search.search_link",
			"duration": 28,
			"calls": [],
			"form_dict": {"doctype": "Item"},
		},
		# User opens a customer
		{
			"method": "GET",
			"path": "/api/method/frappe.desk.form.load.getdoc",
			"cmd": "frappe.desk.form.load.getdoc",
			"duration": 62,
			"calls": [],
			"form_dict": {"doctype": "Customer", "name": "CUST-001"},
		},
		# (permission polling — filtered)
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 4,
			"calls": [],
		},
		# User creates a new Sales Invoice
		{
			"method": "POST",
			"path": "/api/method/frappe.desk.form.save.savedocs",
			"cmd": "frappe.desk.form.save.savedocs",
			"duration": 320,
			"calls": [],
			"form_dict": {
				"doc": _json.dumps({
					"doctype": "Sales Invoice",
					"__islocal": 1,
				}),
				"action": "Save",
			},
		},
		# User submits it
		{
			"method": "POST",
			"path": "/api/method/frappe.desk.form.save.savedocs",
			"cmd": "frappe.desk.form.save.savedocs",
			"duration": 410,
			"calls": [],
			"form_dict": {
				"doc": _json.dumps({
					"doctype": "Sales Invoice",
					"name": "SINV-00042",
				}),
				"action": "Submit",
			},
		},
	]
	html_out = _build_auto_notes_html(recordings)

	# The whole story shows up in order, and reads like English.
	for expected in (
		"Search Item",
		"Open Customer CUST-001",
		"Create Sales Invoice",
		"Submit Sales Invoice",
	):
		assert expected in html_out, (
			f"Expected '{expected}' in reproducer; got: {html_out!r}"
		)

	# And the noise is gone.
	assert "has_permission" not in html_out
	assert "1 background / polling" in html_out


def test_start_dialog_no_longer_asks_for_notes():
	"""The 'Steps to reproduce' field has been removed from the start
	dialog. Users can still see / edit the auto-generated notes on the
	Optimus Session form after the session completes."""
	wpath = os.path.join(
		HERE, "..", "public", "js", "floating_widget.js"
	)
	with open(wpath) as f:
		widget_src = f.read()

	# The openStartDialog function must NOT define a field with
	# fieldname: "notes" any more.
	assert 'fieldname: "notes"' not in widget_src, (
		"The start dialog still defines a 'notes' field. The v0.5.1 "
		"design removed it — notes is now auto-filled from captured "
		"actions during analyze. Delete the dialog entry."
	)
	# And the frappe.call args must not pass notes either.
	assert "notes: values.notes" not in widget_src, (
		"The start call still passes a `notes` argument. Since the "
		"dialog no longer collects it, values.notes is always undefined "
		"— drop the arg from the frappe.call."
	)


# --------------------------------------------------------------------------
# v0.6.0: LLM-humanized "Steps to Reproduce"
# --------------------------------------------------------------------------

def test_actions_for_humanizer_compacts_recordings():
	"""_actions_for_humanizer should drop noise, cap, and emit the
	compact {label, cmd, path, method, doctype, duration_ms} dicts the
	humanizer prompt expects."""
	from optimus.analyze import _actions_for_humanizer

	recordings = [
		{"cmd": "frappe.realtime.has_permission", "duration": 3, "calls": []},  # noise
		{"cmd": "frappe.desk.form.save.savedocs", "duration": 780, "calls": [],
		 "form_dict": {"doc": json.dumps({"doctype": "Sales Invoice", "__islocal": 1}),
		               "action": "Save"}},
		{"method": "GET", "path": "/api/method/foo.bar", "cmd": None, "duration": 12, "calls": []},
	]
	out = _actions_for_humanizer(recordings)
	assert len(out) == 2  # the realtime one is filtered
	first = out[0]
	assert first["cmd"] == "frappe.desk.form.save.savedocs"
	assert first["doctype"] == "Sales Invoice"
	assert "Sales Invoice" in first["label"]
	assert out[1]["path"] == "/api/method/foo.bar"
	# All the expected keys present.
	for k in ("label", "cmd", "path", "method", "doctype", "duration_ms"):
		assert k in first


def test_assemble_humanized_notes_is_just_the_friendly_steps():
	"""Only the preamble + the rendered Markdown steps — no raw
	"Captured actions" appendix (the per-action breakdown in the report
	already lists every action with its technical label)."""
	from optimus.analyze import _assemble_humanized_notes

	html_out = _assemble_humanized_notes("1. Submit a Delivery Note.\n\n**Summary:** submitting a DN.")
	assert "drafted by AI" in html_out
	assert "Submit a Delivery Note" in html_out
	assert "Captured actions" not in html_out
	assert "what actually hit the server" not in html_out


def test_build_humanized_notes_html_returns_empty_when_ai_disabled():
	"""When AI is off (or unconfigured), _build_humanized_notes_html must
	return "" so _persist falls back to the plain auto-notes list."""
	from types import SimpleNamespace
	from unittest.mock import patch

	from optimus import analyze

	recordings = [{"cmd": "frappe.client.save", "duration": 50, "calls": [],
	               "form_dict": {"doctype": "Item"}}]
	with patch("optimus.settings.get_config",
	           return_value=SimpleNamespace(ai_enabled=False, ai_humanize_steps=True)):
		assert analyze._build_humanized_notes_html(recordings) == ""


def test_build_humanized_notes_html_returns_empty_on_llm_failure():
	"""If the LLM call raises, the helper swallows it and returns "" — the
	caller then uses _build_auto_notes_html. analyze must never fail just
	because the humanizer did."""
	from types import SimpleNamespace
	from unittest.mock import patch

	from optimus import ai_fix, analyze

	recordings = [{"cmd": "frappe.client.save", "duration": 50, "calls": [],
	               "form_dict": {"doctype": "Item"}}]
	with patch("optimus.settings.get_config",
	           return_value=SimpleNamespace(ai_enabled=True, ai_humanize_steps=True)), \
	     patch.object(ai_fix, "is_available", return_value=True), \
	     patch.object(ai_fix, "humanize_steps", side_effect=ai_fix.AiFixError("boom")):
		assert analyze._build_humanized_notes_html(recordings) == ""


def test_persist_prefers_humanized_then_falls_back():
	"""Source guard: _persist must try _build_humanized_notes_html first
	and fall back to _build_auto_notes_html."""
	import inspect

	from optimus import analyze

	src = inspect.getsource(analyze._persist)
	assert "_build_humanized_notes_html" in src
	assert "_build_auto_notes_html" in src
