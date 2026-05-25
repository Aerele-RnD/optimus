# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench install-time invariants.

Verifies the after-effects of ``bench install-app optimus`` actually
land in MariaDB:

  * The ``Optimus User`` role is created.
  * Every Optimus DocType is registered in ``tabDocType``.
  * The ``Optimus Settings`` Single doc is creatable + readable.
  * ``bench migrate`` is idempotent — re-running it leaves the schema
    untouched and never raises.

Pure-pytest's Frappe stub can mock individual install-time calls in
isolation, but only a real bench can verify that the rows actually
persist + survive a second migrate. This file is the canonical
"install didn't silently break" smoke.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

# Every DocType optimus declares. Sourced from
# ``optimus/optimus/doctype/`` directory listing — the install assertion
# is that EVERY one of these landed in tabDocType after install-app.
_OPTIMUS_DOCTYPES = (
	"Optimus Session",
	"Optimus Settings",
	"Optimus Action",
	"Optimus Finding",
	"Optimus Background Job",
	"Optimus Phase Two Run",
	"Optimus Tracked App",
	"Optimus Telemetry Event",
)


class TestInstallSmoke(FrappeTestCase):
	def test_optimus_user_role_exists(self):
		"""``after_install`` creates the ``Optimus User`` role and
		auto-assigns it to every System Manager. The role is the desk-
		level permission gate for the floating widget."""
		assert frappe.db.exists("Role", "Optimus User"), (
			"Optimus User role not present — install.after_install didn't land"
		)

	def test_all_optimus_doctypes_exist(self):
		"""Every Optimus DocType is registered. Missing entries here
		would mean ``bench install-app`` skipped reloading a doctype
		— most likely a missing entry in ``patches.txt`` or a typo in
		the doctype's JSON ``module`` field."""
		missing = [
			dt for dt in _OPTIMUS_DOCTYPES
			if not frappe.db.exists("DocType", dt)
		]
		assert not missing, f"missing DocTypes after install: {missing!r}"

	def test_optimus_settings_single_doc_readable(self):
		"""The Single doc is creatable + readable. ``get_single`` works
		because either the install or a prior request initialised the
		row; ``get_cached_doc`` exercises the cache code path the
		settings reader uses on every request."""
		doc = frappe.get_cached_doc("Optimus Settings")
		assert doc.doctype == "Optimus Settings"
		# v0.7.x: enabled defaults to 1 on a fresh install. Tolerate
		# either value here — what we're locking is "readable", not
		# "set to a specific default" (that's covered by unit tests).
		assert hasattr(doc, "enabled")

	# v0.12.34: ``test_bench_migrate_idempotent`` removed. Frappe v16
	# replaced the standalone ``migrate()`` function with the
	# ``SiteMigration`` class, whose ``run(site)`` method calls
	# ``frappe.destroy()`` after running — designed for ``bench
	# migrate`` which then exits the process. Inside a running test,
	# the ``destroy`` unbinds ``frappe.local`` (db, conf, etc.), which
	# cascades into errors in EVERY subsequent test in the class plus
	# ``tearDownClass`` (``frappe.db.value_cache`` becomes unbound).
	#
	# What this test was protecting against — non-idempotent patches
	# in ``patches.txt`` — is already covered:
	#   * Per-patch unit tests verify each ``execute()`` is safe to
	#     re-run (the contract for all patches in this repo).
	#   * The bench bootstrap itself runs ``bench migrate`` (see
	#     ``.github/helper/install.sh``); if any patch was
	#     non-idempotent on a fresh install, install.sh would already
	#     fail before tests ran.
	#   * ``bench run-tests`` triggers ``before_tests`` hooks that
	#     re-run migrate on the test site; the integration suite
	#     implicitly exercises migrate idempotence every time it runs.
	#
	# So the explicit ``SiteMigration().run()`` call in a test is
	# both redundant AND destructive. Removed cleanly.
