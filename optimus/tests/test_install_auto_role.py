# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.4.0 auto-role-assignment on install."""

import pytest

from optimus import install


class FakeRole:
	def __init__(self, role):
		self.role = role


class FakeUser:
	def __init__(self, name, roles):
		self.name = name
		self.roles = [FakeRole(r) for r in roles]
		self.added_roles = []

	def append(self, table, value):
		assert table == "roles"
		self.roles.append(FakeRole(value["role"]))
		self.added_roles.append(value["role"])

	def save(self, ignore_permissions=False):
		pass


def test_auto_assign_role_adds_to_system_managers(monkeypatch):
	"""v0.6.x: the install hook fetches roles via a single ``Has Role``
	query (was an N+1 over every user). Only users that ACTUALLY need
	Optimus User added get loaded as full docs."""
	import frappe

	users_in_db = {
		"alice@example.com": FakeUser("alice@example.com", ["System Manager"]),
		"bob@example.com": FakeUser("bob@example.com", ["Sales User"]),
		"carol@example.com": FakeUser("carol@example.com", ["System Manager", "Optimus User"]),
	}

	# Has Role rows: each (parent=user, role=role_name) pair across all users.
	has_role_rows = [
		{"parent": user_name, "role": r.role}
		for user_name, user in users_in_db.items()
		for r in user.roles
	]
	# We only care about the two roles the install hook filters on.
	has_role_rows = [r for r in has_role_rows if r["role"] in ("System Manager", "Optimus User")]

	get_doc_calls = []

	def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kwargs):
		assert doctype == "Has Role", f"expected single Has Role query, got {doctype!r}"
		# Filter must be {"role": ("in", [...])} with both roles listed.
		assert filters and "role" in filters
		op, roles = filters["role"]
		assert op == "in"
		assert set(roles) == {"System Manager", "Optimus User"}
		# Return only the matching rows from our seeded data.
		want = set(roles)
		return [r for r in has_role_rows if r["role"] in want]

	def fake_get_doc(doctype, name):
		assert doctype == "User"
		get_doc_calls.append(name)
		return users_in_db[name]

	monkeypatch.setattr(frappe, "get_all", fake_get_all, raising=False)
	monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)

	install._assign_profiler_user_to_system_managers()

	# Alice (System Manager, no Optimus User yet) → Optimus User added
	assert "Optimus User" in users_in_db["alice@example.com"].added_roles
	# Bob (Sales User only) → not touched
	assert users_in_db["bob@example.com"].added_roles == []
	# Carol (already has both) → not touched
	assert users_in_db["carol@example.com"].added_roles == []
	# Critical perf assertion: only Alice was loaded as a full doc — NOT
	# Bob (doesn't qualify) and NOT Carol (already has the role).
	assert get_doc_calls == ["alice@example.com"], (
		f"expected ONLY alice loaded as doc, got {get_doc_calls!r}"
	)


def test_on_user_role_change_adds_profiler_user_when_sysmanager_present(monkeypatch):
	user = FakeUser("dave@example.com", ["System Manager"])
	install.on_user_role_change(user, method=None)
	assert "Optimus User" in user.added_roles


def test_on_user_role_change_skips_non_sysmanager(monkeypatch):
	user = FakeUser("eve@example.com", ["Sales User"])
	install.on_user_role_change(user, method=None)
	assert user.added_roles == []


def test_on_user_role_change_skips_already_has_profiler_user(monkeypatch):
	user = FakeUser("frank@example.com", ["System Manager", "Optimus User"])
	install.on_user_role_change(user, method=None)
	assert user.added_roles == []


# ---------------------------------------------------------------------------
# v0.13.x: ignored-apps seed on fresh install
# ---------------------------------------------------------------------------


class _FakeSettings:
	"""Minimal Optimus Settings Single stand-in. Tracks appends + save calls
	so the seeder's idempotency contract is observable from the test."""

	def __init__(self, ignored_apps_rows=None):
		# ignored_apps mirrors how Frappe represents a child table on a
		# Single — a list (truthy if rows exist, falsy if empty).
		self.ignored_apps = list(ignored_apps_rows or [])
		self.appended = []
		self.save_count = 0

	def append(self, table, value):
		assert table == "ignored_apps", f"unexpected table {table!r}"
		self.appended.append(value)
		self.ignored_apps.append(value)

	def save(self, ignore_permissions=False):
		self.save_count += 1


def _stub_seeder_frappe(monkeypatch, *, settings, doctype_exists=True, installed_apps=None):
	"""Wire just enough frappe surface for ``_seed_ignored_apps_with_framework_apps``
	to run end-to-end without a real bench.

	``frappe.db`` is a Werkzeug ``LocalProxy`` ([[feedback_frappe_db_local_proxy]])
	— patching attributes on it raises ``RuntimeError: object is not bound``
	outside a request context. Replace it wholesale with a SimpleNamespace
	carrying just the ``exists`` method the seeder calls.

	``installed_apps`` defaults to the full ``_DEFAULT_IGNORED_APPS`` tuple
	so the "all defaults installed" seeded-rows test stays meaningful
	without spelling the list out twice. Pass an explicit list to exercise
	the intersection filter introduced when the seeder stopped blindly
	appending apps that aren't installed."""
	import types

	import frappe

	monkeypatch.setattr(
		frappe, "db",
		types.SimpleNamespace(
			exists=lambda doctype, name=None: (
				doctype_exists and doctype == "DocType"
			),
		),
		raising=False,
	)
	monkeypatch.setattr(
		frappe, "get_single",
		lambda doctype: settings if doctype == "Optimus Settings" else None,
		raising=False,
	)
	if installed_apps is None:
		installed_apps = list(install._DEFAULT_IGNORED_APPS)
	monkeypatch.setattr(
		frappe, "get_installed_apps",
		lambda: list(installed_apps),
		raising=False,
	)
	# safe_commit is imported into install.py at module top — patch the
	# local binding so the seeder doesn't reach the real one.
	monkeypatch.setattr(install, "safe_commit", lambda: None, raising=False)


class TestSeedIgnoredAppsWithFrameworkApps:
	def test_empty_table_gets_seeded_with_frappe_org_apps(self, monkeypatch):
		settings = _FakeSettings(ignored_apps_rows=[])
		_stub_seeder_frappe(monkeypatch, settings=settings)

		install._seed_ignored_apps_with_framework_apps()

		# All Frappe-organization-maintained apps appended in the
		# documented (alphabetical) order so the seeded rows show up in
		# a predictable order in the Settings form.
		assert settings.appended == [
			{"app_name": "builder"},
			{"app_name": "crm"},
			{"app_name": "drive"},
			{"app_name": "erpnext"},
			{"app_name": "frappe"},
			{"app_name": "helpdesk"},
			{"app_name": "hrms"},
			{"app_name": "insights"},
			{"app_name": "lms"},
			{"app_name": "payments"},
			{"app_name": "wiki"},
		]
		# Single save call — the seeder must not save per-row.
		assert settings.save_count == 1

	def test_non_empty_table_is_NOT_overwritten(self, monkeypatch):
		# Operator already configured ignored_apps before this install
		# hook ran (e.g. re-running migrate on an existing site).
		existing = [{"app_name": "frappe"}, {"app_name": "my_custom_app"}]
		settings = _FakeSettings(ignored_apps_rows=existing)
		_stub_seeder_frappe(monkeypatch, settings=settings)

		install._seed_ignored_apps_with_framework_apps()

		# Idempotency contract: nothing appended, nothing saved.
		assert settings.appended == []
		assert settings.save_count == 0
		# Existing config preserved.
		assert settings.ignored_apps == existing

	def test_no_doctype_yet_is_a_silent_noop(self, monkeypatch):
		# Migration hasn't run yet — DocType doesn't exist. Seeder must
		# return cleanly (mirrors the tracked-apps seed's early-return).
		settings = _FakeSettings(ignored_apps_rows=[])
		_stub_seeder_frappe(monkeypatch, settings=settings, doctype_exists=False)

		install._seed_ignored_apps_with_framework_apps()

		assert settings.appended == []
		assert settings.save_count == 0

	def test_only_installed_apps_get_seeded(self, monkeypatch):
		# The seeder intersects _DEFAULT_IGNORED_APPS with what's actually
		# installed — seeding an app that isn't installed is pure UI
		# clutter (it can't produce findings). Typical custom-app stack:
		# frappe + the operator's own app.
		settings = _FakeSettings(ignored_apps_rows=[])
		_stub_seeder_frappe(
			monkeypatch, settings=settings,
			installed_apps=["frappe", "ugly_code"],
		)

		install._seed_ignored_apps_with_framework_apps()

		# Only frappe (the only _DEFAULT_IGNORED_APPS member that's
		# installed) lands. The custom app ``ugly_code`` is not in the
		# defaults so it's left alone — that's the tracked-apps seeder's
		# job.
		assert settings.appended == [{"app_name": "frappe"}]
		assert settings.save_count == 1

	def test_no_default_apps_installed_is_a_silent_noop(self, monkeypatch):
		# Bench installed only custom apps (none of the Frappe-org ones).
		# Nothing in the intersection → no save, no rows.
		settings = _FakeSettings(ignored_apps_rows=[])
		_stub_seeder_frappe(
			monkeypatch, settings=settings,
			installed_apps=["ugly_code", "another_custom_app"],
		)

		install._seed_ignored_apps_with_framework_apps()

		assert settings.appended == []
		assert settings.save_count == 0

	def test_partial_install_preserves_alphabetical_order(self, monkeypatch):
		# A handful of defaults installed — the seeded rows preserve the
		# _DEFAULT_IGNORED_APPS tuple order (alphabetical), not the order
		# of frappe.get_installed_apps's return value.
		settings = _FakeSettings(ignored_apps_rows=[])
		_stub_seeder_frappe(
			monkeypatch, settings=settings,
			# Deliberately scrambled to prove the seed re-sorts via the
			# _DEFAULT_IGNORED_APPS iteration order.
			installed_apps=["hrms", "erpnext", "frappe", "wiki"],
		)

		install._seed_ignored_apps_with_framework_apps()

		assert settings.appended == [
			{"app_name": "erpnext"},
			{"app_name": "frappe"},
			{"app_name": "hrms"},
			{"app_name": "wiki"},
		]

	def test_default_list_matches_documented_constant(self):
		# Locks the public-facing default. If a future change wants to
		# add / remove an app, this assertion + the description text in
		# optimus_settings.json + the ``ignored_apps`` defaults in
		# ``settings.py`` (both ``_DEFAULTS`` and the dataclass default
		# factory) must move together.
		assert install._DEFAULT_IGNORED_APPS == (
			"builder",
			"crm",
			"drive",
			"erpnext",
			"frappe",
			"helpdesk",
			"hrms",
			"insights",
			"lms",
			"payments",
			"wiki",
		)
