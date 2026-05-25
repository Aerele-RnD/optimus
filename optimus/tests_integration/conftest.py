# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pytest fixtures for the real-bench integration suite.

This module is **only imported under ``bench --site … run-tests``** — the
pure-pytest workflow at ``.github/workflows/tests.yml`` invokes
``pytest optimus/tests/`` and never traverses this directory, so the
Frappe stub in ``optimus/tests/conftest.py`` doesn't apply here.

The fixtures assume a live Frappe site is initialised (``frappe.init`` +
``frappe.connect`` have already run — Frappe's test runner does this
before importing the test modules). Direct Frappe access (``frappe.db``,
``frappe.cache``, ``frappe.get_doc`` etc.) is the whole point — the unit
suite stubs them, the integration suite uses them.

Fixtures:

* :func:`test_site` — yields the site name. Tests rarely need it
  explicitly (frappe.local already knows), but it's useful for assertions
  and for shelling out to ``bench --site {test_site} …``.
* :func:`cleanup_session` (autouse) — defence-in-depth teardown that
  hard-deletes any ``Optimus Session`` rows left behind plus their
  ``profiler:*`` Redis keys. ``FrappeTestCase`` already rolls back the
  per-test transaction, but the analyze pipeline writes through a
  background-worker connection that escapes the transaction in production
  flows — and the Redis state isn't transactional either way.
* :func:`seeded_session` — convenience wrapper that calls ``api.start``,
  yields the session_uuid, and on teardown calls ``api.stop`` and waits
  for analyze to finalise. Used by the lifecycle test.
"""

from __future__ import annotations

import time

import frappe
import pytest


@pytest.fixture(scope="session")
def test_site() -> str:
	"""The name of the Frappe site the test runner is connected to. Returns
	``frappe.local.site`` so tests stay site-agnostic — the helper script
	creates ``test_site`` but a local developer running this suite against
	their own bench site sees that site name."""
	return frappe.local.site


@pytest.fixture(autouse=True)
def cleanup_session():
	"""Defensive teardown that runs after every integration test.

	FrappeTestCase wraps each test in a DB transaction that's rolled back
	at teardown, but the analyze pipeline runs in a background worker via
	RQ — its writes are in a separate connection that escapes that
	transaction in real flows. Same for Redis: the active-session pointer
	and the per-session metadata hash aren't transactional either way.

	This fixture is best-effort. A failed delete is logged and tolerated
	(the next test will see the row, which is annoying but not a
	correctness problem on the ephemeral CI runner). The cleanup runs in
	a *new* transaction so it can commit even when the test transaction
	already rolled back.
	"""
	yield
	# Lazy imports — analyze.py / session.py do their own frappe imports
	# at module top, but the conftest shouldn't reach them at module load
	# (it would slow down test-collection in the bench runner).
	try:
		_purge_test_sessions()
	except Exception as exc:  # pragma: no cover — best-effort path
		frappe.log_error(
			title="optimus integration: cleanup_session",
			message=f"{type(exc).__name__}: {exc}",
		)


def _purge_test_sessions() -> None:
	"""Delete every ``Optimus Session`` row whose user is the current
	session user, plus their associated Redis state. Safe to call on a
	bench that holds OTHER sessions (different users) — the user-scoped
	filter prevents collateral damage."""
	user = getattr(frappe.session, "user", None) or "Administrator"
	rows = frappe.get_all(
		"Optimus Session",
		filters={"user": user},
		pluck="name",
	)
	for name in rows:
		try:
			frappe.delete_doc(
				"Optimus Session",
				name,
				ignore_permissions=True,
				force=True,
			)
		except Exception:
			pass
	# Clear the user's active-session pointer + any per-session metadata
	# hashes. The keys are scoped by Frappe's cache.make_key which
	# prefixes by site name, so we won't clobber state on a sibling site.
	try:
		frappe.cache.delete_value(f"profiler:active:{user}")
		# Per-uuid meta hashes are harder to enumerate without a SCAN;
		# the next janitor cron sweeps them via TTL anyway.
	except Exception:
		pass
	frappe.db.commit()


@pytest.fixture
def seeded_session(test_site):
	"""Start an Optimus profiling session as Administrator, yield the
	session_uuid, and tear down by calling ``api.stop`` + waiting for the
	analyze job to finish (capped at 60 s).

	Tests that need a fully-finalized session use this fixture; tests
	that need to assert the start-time state mid-flight ignore it and
	call ``api.start`` directly so they can interleave assertions."""
	frappe.set_user("Administrator")
	from optimus import api

	result = api.start(label="integration test seeded_session")
	session_uuid = result["session_uuid"]
	yield session_uuid

	# Teardown: stop the session if it's still active, then wait for
	# analyze to finalise. ``api.stop`` returns ran_inline=True when the
	# inline-analyze cap (v0.5.0) fires; in that case the session is
	# already in a terminal state and the poll loop below short-circuits.
	try:
		api.stop()
	except Exception:
		pass
	_wait_for_terminal_status(session_uuid, timeout_seconds=60)


def _wait_for_terminal_status(session_uuid: str, *, timeout_seconds: int = 60) -> str | None:
	"""Poll the Optimus Session row's ``status`` field every 500 ms until
	it lands on ``Ready`` or ``Failed`` (the terminal states), or until
	``timeout_seconds`` elapses. Returns the final status, or ``None`` on
	timeout."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		try:
			status = frappe.db.get_value(
				"Optimus Session",
				{"session_uuid": session_uuid},
				"status",
			)
		except Exception:
			status = None
		if status in ("Ready", "Failed"):
			return status
		time.sleep(0.5)
	return None
