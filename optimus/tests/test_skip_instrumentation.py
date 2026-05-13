# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.1 instrumentation-noise skip list.

The profiler filters its own whitelisted API endpoints and Frappe's
built-in Recorder doctype whitelisted methods out of the recording
stream at `before_request` time. Without this filter, a recording
session captures its own widget-polling calls (`optimus.api
.status` fires every ~2 seconds while Recording), which then pollute:

  - per_action rows
  - top_queries / table_breakdown
  - auto-generated "Steps to Reproduce" bullet list
  - total wall-clock / query totals

The check lives in `_should_skip_request()`, gated on
`frappe.form_dict.cmd` so path-based URLs (e.g. `/app/recorder` page
loads) aren't affected.
"""

import types

import frappe


def _set_fake_local(cmd=None, path=None):
	"""Install a minimal SimpleNamespace as `frappe.local` with a form_dict
	and optional request.path, mirroring what Frappe's ``make_form_dict``
	and ``init_request`` produce at before_request time.

	- ``cmd``: value to stick under ``form_dict["cmd"]``. Simulates a
	  legacy ``?cmd=foo`` RPC call (for which ``make_form_dict`` populates
	  cmd before before_request fires).
	- ``path``: value to expose as ``local.request.path``. Simulates a
	  modern ``/api/method/foo`` URL, for which ``frappe.form_dict.cmd``
	  is NOT set at before_request time (it gets set later inside
	  ``handle_rpc_call``). The path is the ONLY source of the method
	  name at hook time.
	"""
	local = types.SimpleNamespace()
	local.form_dict = {"cmd": cmd} if cmd is not None else {}
	if path is not None:
		local.request = types.SimpleNamespace(path=path)
	frappe.local = local
	return local


def test_should_skip_frappe_profiler_status_poll():
	"""``optimus.api.status`` must always be filtered from
	capture. v0.5.1 removed the widget's 5-second polling loop in
	favor of realtime events, but the endpoint is still called once
	on page load (to rehydrate widget state after navigation/reload)
	and once on tab-visibility return. Those calls MUST NOT be
	captured into the profiler session they belong to — they're
	instrumentation, not user work."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(cmd="optimus.api.status")
	assert _should_skip_request() is True


def test_should_skip_all_frappe_profiler_api_methods():
	"""Any `optimus.api.*` whitelisted method is instrumentation
	noise. Covers start, stop, status, submit_frontend_metrics,
	retry_analyze, analyze_fetch, regenerate_reports, download_pdf — all of
	them match the prefix."""
	from optimus.hooks_callbacks import _should_skip_request

	for cmd in (
		"optimus.api.start",
		"optimus.api.stop",
		"optimus.api.status",
		"optimus.api.submit_frontend_metrics",
		"optimus.api.retry_analyze",
		"optimus.api.analyze_fetch",
		"optimus.api.regenerate_reports",
		"optimus.api.download_pdf",
	):
		_set_fake_local(cmd=cmd)
		assert _should_skip_request() is True, (
			f"{cmd} must be skipped — it's profiler instrumentation, "
			"not application work"
		)


def test_should_skip_frappe_builtin_recorder_methods():
	"""If the user has Frappe's built-in Recorder UI open in another tab
	while profiling, its whitelisted calls should not contaminate the
	profiler session."""
	from optimus.hooks_callbacks import _should_skip_request

	for cmd in (
		"frappe.core.doctype.recorder.recorder.export_data",
		"frappe.core.doctype.recorder.recorder.delete",
		"frappe.core.doctype.recorder.recorder.get_request_details",
		"frappe.core.doctype.recorder.recorder.pluck",
		"frappe.core.doctype.recorder.recorder.start",
		"frappe.core.doctype.recorder.recorder.stop",
	):
		_set_fake_local(cmd=cmd)
		assert _should_skip_request() is True, (
			f"{cmd} must be skipped — it's the Frappe recorder's own "
			"plumbing"
		)


def test_should_not_skip_regular_application_calls():
	"""The common case: a real application whitelisted method. Must NOT
	be skipped, or the profiler would stop capturing anything useful."""
	from optimus.hooks_callbacks import _should_skip_request

	for cmd in (
		"frappe.client.save",
		"frappe.client.get_list",
		"frappe.client.set_value",
		"erpnext.selling.doctype.sales_invoice.sales_invoice.make_sales_return",
		"my_custom_app.api.do_thing",
	):
		_set_fake_local(cmd=cmd)
		assert _should_skip_request() is False, (
			f"{cmd} must NOT be skipped — it's real application work"
		)


def test_should_not_skip_page_loads_without_cmd():
	"""Page loads (`/app/recorder`, `/api/resource/Sales Invoice/…`) have
	no cmd in form_dict. They must fall through as 'not noise' even
	when the URL happens to contain the word 'recorder' — the skip
	list matches cmd prefixes, not URL substrings."""
	from optimus.hooks_callbacks import _should_skip_request

	# Empty form_dict entirely (page load with no POST body)
	_set_fake_local()
	assert _should_skip_request() is False

	# form_dict with some other field but no cmd
	local = _set_fake_local()
	local.form_dict = {"doctype": "Recorder", "name": "REC-001"}
	assert _should_skip_request() is False


def test_should_skip_is_defensive_against_missing_local():
	"""Edge case: `frappe.local` might not have `form_dict` during
	startup / health checks / OPTIONS preflights. Must return False
	(don't crash the request) rather than raising."""
	from optimus.hooks_callbacks import _should_skip_request

	# frappe.local with no form_dict attribute at all
	frappe.local = types.SimpleNamespace()
	assert _should_skip_request() is False

	# frappe.local.form_dict is not a dict (unusual but possible)
	frappe.local = types.SimpleNamespace(form_dict="not-a-dict")
	assert _should_skip_request() is False


def test_should_skip_is_prefix_match_not_exact():
	"""Prefix match, so a future method like `optimus.api
	.retry_analyze_background` would still be caught. Exact match
	would miss that."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(cmd="optimus.api.some_future_method")
	assert _should_skip_request() is True

	_set_fake_local(cmd="frappe.core.doctype.recorder.recorder.new_method")
	assert _should_skip_request() is True


def test_should_not_match_similar_but_different_prefixes():
	"""Guard against over-matching. A hypothetical
	`frappe_profiler_extensions.api.foo` starts with `optimus`
	but is NOT our API — the prefix ends with a dot for a reason.
	Same for `frappe.core.doctype.recorder_archive.*`."""
	from optimus.hooks_callbacks import _should_skip_request

	# Almost-but-not-quite prefixes must NOT match
	_set_fake_local(cmd="frappe_profiler_extras.api.foo")
	assert _should_skip_request() is False

	_set_fake_local(cmd="frappe.core.doctype.recorder_archive.foo")
	assert _should_skip_request() is False


# ---------------------------------------------------------------------------
# v0.5.1 follow-up: resolving cmd from request.path (the real-world case)
# ---------------------------------------------------------------------------
# The original v0.5.1 filter checked only frappe.form_dict.cmd. That caught
# the legacy /?cmd=foo.bar RPC path but missed every modern /api/method/
# foo.bar URL, because Frappe's handle_rpc_call sets form_dict.cmd AFTER
# before_request hooks have already run. Widget polls go through
# /api/method/optimus.api.status, so the initial filter was a no-op
# in production. This second layer parses the method name out of
# request.path, which IS populated by the time hooks fire.


def test_path_based_skip_for_v1_method_url():
	"""/api/method/<foo> is the most common URL shape. At before_request
	time, frappe.form_dict.cmd is NOT set for these — Frappe's routing
	sets it later, inside handle_rpc_call. The path is the only source."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/api/method/optimus.api.status")
	assert _should_skip_request() is True


def test_path_based_skip_for_v2_method_url():
	"""/api/v2/method/<foo> also routes through the same handle_rpc_call.
	Both v1 and v2 paths use `/method/<name>` so the parser finds
	the marker regardless of API version."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/api/v2/method/optimus.api.submit_frontend_metrics")
	assert _should_skip_request() is True


def test_path_based_skip_for_frappe_recorder_methods():
	"""The Frappe Recorder doctype methods also reach us via
	/api/method/<name>, so both sources must see them through the path."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/api/method/frappe.core.doctype.recorder.recorder.export_data")
	assert _should_skip_request() is True


def test_path_based_does_not_skip_regular_method_urls():
	"""A real app call like /api/method/frappe.client.save must pass
	through. If the path parser over-matches, the profiler stops
	capturing anything useful."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/api/method/frappe.client.save")
	assert _should_skip_request() is False

	_set_fake_local(
		path="/api/method/erpnext.selling.doctype.sales_invoice.sales_invoice.make_sales_return"
	)
	assert _should_skip_request() is False


def test_path_based_ignores_rest_resource_urls():
	"""/api/resource/Sales Invoice/INV-00042 has no /method/ segment —
	it's a REST resource call, not a whitelisted method. Must fall
	through as 'not noise'."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/api/resource/Sales Invoice/INV-00042")
	assert _should_skip_request() is False


def test_path_based_ignores_desk_app_urls():
	"""/app/recorder is the Frappe Recorder UI — a page load, not a
	whitelisted method call. Even though the URL contains 'recorder',
	the path parser must not match because there's no /method/ marker."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/app/recorder")
	assert _should_skip_request() is False


def test_path_based_handles_trailing_slash():
	"""Some clients append a trailing slash to method URLs. The path
	parser must strip it before the prefix check, otherwise
	'optimus.api.status/' won't match
	'optimus.api.' via startswith (which would still match,
	actually — but a follow-on exact comparison wouldn't). Defense-
	in-depth check for cleanup."""
	from optimus.hooks_callbacks import _should_skip_request

	_set_fake_local(path="/api/method/optimus.api.status/")
	assert _should_skip_request() is True


def test_path_based_handles_missing_request():
	"""frappe.local has no `request` attribute at all — edge case during
	startup or health checks. Must fall through as False."""
	from optimus.hooks_callbacks import _should_skip_request

	# Empty namespace, no request, no form_dict
	local = types.SimpleNamespace()
	local.form_dict = {}
	frappe.local = local
	assert _should_skip_request() is False


def test_form_dict_cmd_still_wins_over_path():
	"""Legacy /?cmd=foo.bar RPC path: make_form_dict sets
	form_dict.cmd before before_request fires. If both sources are
	present (e.g. request.path happens to contain /method/ too), the
	form_dict value is the canonical one and should win."""
	from optimus.hooks_callbacks import _extract_cmd_from_request

	_set_fake_local(
		cmd="optimus.api.status",
		path="/api/method/frappe.client.save",
	)
	# form_dict.cmd wins — returns the legacy value.
	assert _extract_cmd_from_request() == "optimus.api.status"


def test_extract_cmd_returns_empty_string_on_non_method_url():
	"""A non-method URL resolves to empty string, which the caller
	treats as 'no cmd available, don't skip'. This is how
	/app/home and /api/resource/... pass through."""
	from optimus.hooks_callbacks import _extract_cmd_from_request

	_set_fake_local(path="/app/home")
	assert _extract_cmd_from_request() == ""

	_set_fake_local(path="/api/resource/User/Administrator")
	assert _extract_cmd_from_request() == ""

	_set_fake_local(path="/private/files/foo.pdf")
	assert _extract_cmd_from_request() == ""


def test_before_request_early_exits_on_skipped_cmd():
	"""Source-inspection check: before_request must call
	_should_skip_request AND return early BEFORE setting
	`frappe.local.optimus_session_id`. If the flag gets set, the
	after_request hook will register the recording even though we
	meant to skip it."""
	import inspect

	from optimus import hooks_callbacks

	src = inspect.getsource(hooks_callbacks.before_request)

	# The skip call must appear in before_request.
	assert "_should_skip_request()" in src

	# The skip check must occur BEFORE the optimus_session_id assignment —
	# setting the flag first would cause after_request to register the
	# recording anyway, defeating the filter.
	skip_idx = src.find("_should_skip_request()")
	flag_idx = src.find("frappe.local.optimus_session_id = session_uuid")
	assert skip_idx > 0, "before_request must call _should_skip_request()"
	assert flag_idx > 0, "before_request must set optimus_session_id"
	assert skip_idx < flag_idx, (
		"_should_skip_request() must be checked BEFORE "
		"optimus_session_id is set, otherwise after_request will "
		"register the filtered recording anyway"
	)
