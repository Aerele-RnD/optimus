# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the shared is_framework_callsite() classifier.

v0.5.2: extended the framework-app filter to cover every official
Frappe-maintained app (erpnext, hrms, payments, lms, helpdesk,
insights, crm, builder, wiki, drive) so findings rooted inside those
apps route into the collapsed Observations subsection instead of the
actionable Findings list. Triggered by a production Sales Invoice
Save+Submit session that surfaced 10 'Redundant cache lookup'
findings landing in apps/erpnext/.../sales_invoice.py:300 — the
application developer can't patch ERPNext from their bench.

These tests pin the classifier's behavior so regressions (e.g. an
unrelated refactor removing an app from FRAMEWORK_APPS) surface
immediately.
"""

import pytest

from optimus.analyzers.base import (
	FRAMEWORK_APPS,
	is_framework_callsite,
)


class TestFrameworkAppsMembership:
	def test_frappe_core_in_set(self):
		assert "frappe" in FRAMEWORK_APPS
		assert "optimus" in FRAMEWORK_APPS

	def test_all_official_apps_present(self):
		"""Pins the full list so accidentally dropping one (e.g.
		hrms) re-introduces noise for users of that app."""
		expected = {
			"frappe", "optimus",
			"erpnext", "payments", "hrms", "lms",
			"helpdesk", "insights", "crm", "builder",
			"wiki", "drive",
		}
		assert expected <= FRAMEWORK_APPS, (
			f"FRAMEWORK_APPS missing entries: {expected - FRAMEWORK_APPS}"
		)


class TestFrappeCoreDetection:
	@pytest.mark.parametrize("path", [
		"frappe/handler.py",
		"frappe/model/document.py",
		"frappe/query_builder/utils.py",
		"apps/frappe/frappe/handler.py",
		"/Users/x/bench/apps/frappe/frappe/app.py",
		"optimus/capture.py",
		"apps/optimus/optimus/analyze.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True


class TestOfficialAppDetection:
	@pytest.mark.parametrize("path", [
		"apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
		"apps/erpnext/erpnext/stock/doctype/item/item.py",
		"apps/hrms/hrms/payroll/utils.py",
		"apps/payments/payments/utils.py",
		"apps/lms/lms/lms/api.py",
		"apps/helpdesk/helpdesk/api.py",
		"apps/insights/insights/api.py",
		"apps/crm/crm/fcrm/doctype/crm_lead/crm_lead.py",
		"apps/builder/builder/api.py",
		"apps/wiki/wiki/api.py",
		"apps/drive/drive/api.py",
		# Absolute paths also match (bench installs on servers)
		"/home/frappe/bench/apps/erpnext/erpnext/controllers/accounts_controller.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True, (
			f"{path} should be classified as framework"
		)


class TestThirdPartyLibraryDetection:
	@pytest.mark.parametrize("path", [
		"env/lib/python3.14/site-packages/werkzeug/serving.py",
		"env/lib/python3.14/site-packages/gunicorn/workers/base.py",
		"/usr/lib/python3/dist-packages/requests/sessions.py",
		"something/werkzeug/routing.py",
		"something/gunicorn/app.py",
		"something/rq/worker.py",
		"pyinstrument/frame.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True


class TestUserCodeNotMatched:
	@pytest.mark.parametrize("path", [
		"apps/myapp/controllers/bulk_import.py",
		"apps/my_custom_app/handlers.py",
		"apps/jewellery_erpnext/jewellery_erpnext/doctype/foo.py",
		"apps/my_erpnext_fork/custom.py",
		"apps/acme/acme/api.py",
	])
	def test_user_code_passes_through(self, path):
		assert is_framework_callsite(path) is False, (
			f"{path} should NOT be classified as framework"
		)


class TestBoundaryCases:
	"""Boundary-sensitive matching: ``crm/`` must not false-positive
	on ``my_crm/``. These regressions are subtle so they get their
	own class."""

	def test_lookalike_crm(self):
		# Both should be user code.
		assert is_framework_callsite("apps/my_crm/custom.py") is False
		assert is_framework_callsite("apps/custom_crm/foo.py") is False

	def test_lookalike_erpnext(self):
		# Fork of erpnext with renamed top-level should still be user code.
		assert is_framework_callsite("apps/myerpnext_fork/foo.py") is False
		# But the jewellery_erpnext case — which is a DIFFERENT app —
		# must also be user code (it's not in the official list even
		# though its name ends with 'erpnext').
		assert is_framework_callsite("apps/jewellery_erpnext/foo.py") is False

	def test_lookalike_hrms(self):
		assert is_framework_callsite("apps/custom_hrms/foo.py") is False


class TestNilInputs:
	def test_empty_string(self):
		assert is_framework_callsite("") is False

	def test_none(self):
		assert is_framework_callsite(None) is False


class TestInclusionMode:
	"""When tracked_apps is a non-empty tuple (Optimus Settings ▸
	Tracked Apps populated), the classifier flips: framework = NOT
	in the allowlist. This lets a site admin say 'I only care about
	findings in myapp' without enumerating every framework app."""

	def test_in_allowlist_returns_false(self):
		tracked = ("myapp",)
		assert is_framework_callsite(
			"apps/myapp/myapp/controllers/foo.py",
			tracked_apps=tracked,
		) is False

	def test_not_in_allowlist_returns_true(self):
		"""Even erpnext, which is in FRAMEWORK_APPS, still returns
		True in inclusion mode — the inclusion check is the ONLY
		check when tracked_apps is set."""
		tracked = ("myapp",)
		assert is_framework_callsite(
			"apps/erpnext/erpnext/foo.py", tracked_apps=tracked,
		) is True
		# Even myapp2 (not in the allowlist) returns True.
		assert is_framework_callsite(
			"apps/myapp2/foo.py", tracked_apps=tracked,
		) is True

	def test_short_form_filename_matches(self):
		"""Pyinstrument's short-form filenames (no apps/ prefix) must
		still match the allowlist on first segment."""
		tracked = ("myapp",)
		assert is_framework_callsite(
			"myapp/controllers/foo.py", tracked_apps=tracked,
		) is False

	def test_absolute_path_matches(self):
		tracked = ("myapp",)
		assert is_framework_callsite(
			"/home/frappe/bench/apps/myapp/myapp/foo.py",
			tracked_apps=tracked,
		) is False

	def test_empty_tracked_apps_falls_back_to_exclusion(self):
		"""Empty tuple means 'no allowlist configured' — fall back to
		the built-in FRAMEWORK_APPS exclusion list."""
		assert is_framework_callsite(
			"apps/erpnext/erpnext/foo.py", tracked_apps=(),
		) is True
		assert is_framework_callsite(
			"apps/myapp/foo.py", tracked_apps=(),
		) is False

	def test_none_tracked_apps_falls_back_to_exclusion(self):
		assert is_framework_callsite(
			"apps/erpnext/erpnext/foo.py", tracked_apps=None,
		) is True

	def test_multiple_allowlisted_apps(self):
		tracked = ("myapp", "custom_invoicing", "reporting")
		for app in tracked:
			assert is_framework_callsite(
				f"apps/{app}/{app}/foo.py", tracked_apps=tracked,
			) is False
		# Un-listed still framework.
		assert is_framework_callsite(
			"apps/something_else/foo.py", tracked_apps=tracked,
		) is True

	def test_boundary_check_still_sound_in_inclusion_mode(self):
		"""tracked_apps=('crm',) must NOT match apps/my_crm/..."""
		tracked = ("crm",)
		# Real crm app: matches
		assert is_framework_callsite(
			"apps/crm/crm/foo.py", tracked_apps=tracked,
		) is False
		# Look-alike: should be treated as framework (NOT in the allowlist)
		assert is_framework_callsite(
			"apps/my_crm/foo.py", tracked_apps=tracked,
		) is True
