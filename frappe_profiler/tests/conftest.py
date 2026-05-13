# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Pytest configuration for frappe_profiler analyzer tests.

These tests are deliberately decoupled from Frappe — each analyzer is
a pure function over a list of recording dicts, so we can exercise them
with JSON fixtures and no running site. Run with:

    cd apps/frappe_profiler
    python -m pytest frappe_profiler/tests/ -v

(or just `pytest frappe_profiler/tests/ -v` if pytest is installed globally).
"""

import json
import os
import sys

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# v0.6.x: sys.modules auto-restore fence.
#
# Several tests install fake ``frappe`` / ``frappe.recorder`` /
# ``frappe_profiler.*`` stubs into ``sys.modules`` to exercise pure-Python
# code without a running bench. Historically they did it via bare
# ``sys.modules[name] = stub`` assignments without restoring at teardown,
# so every test running AFTER one of them in the same pytest session
# inherited the stub and crashed on missing attributes (Frappe internals
# the stub didn't model). That's the source of the 80+ "failures" in the
# full-suite tally that all pass in isolation.
#
# This autouse fixture snapshots ``sys.modules`` BEFORE each test and
# restores it AFTER, so any test's mutations are contained to that test
# regardless of whether the test itself remembered to clean up. Cost per
# test: a shallow ``dict()`` of ~hundreds of keys — well under 1ms.
#
# Modules a test legitimately ADDS during its run (e.g. importing a fresh
# patch module to ``importlib.reload`` it) are dropped from sys.modules
# at teardown ONLY when they're one of the known pollution targets — so
# we don't churn the import cache for unrelated tests, but we also don't
# let stub-installed shims leak.
_POLLUTION_PRONE_MODULES = frozenset({
	"frappe", "frappe.recorder", "frappe.utils",
	"frappe_profiler.session", "frappe_profiler.capture",
	"frappe_profiler.line_profile.capture",
})

# frappe_profiler modules that ``import frappe`` at module top. Their
# top-level binding captures whatever was in ``sys.modules["frappe"]``
# AT IMPORT TIME — so if they get imported while a test has installed a
# stub-frappe (and then the fence restores the real frappe at teardown),
# their captured reference is now stale. We evict these from
# ``sys.modules`` when we detect the frappe-swap so the next test
# re-imports them against the restored real frappe.
_FRAPPE_DEPENDENT_LEAVES = frozenset({
	"frappe_profiler.api",
	"frappe_profiler.analyze",
	"frappe_profiler.hooks_callbacks",
	"frappe_profiler.infra_capture",
	"frappe_profiler.install",
	"frappe_profiler.janitor",
	"frappe_profiler.pdf_export",
	"frappe_profiler.permissions",
	"frappe_profiler.session",
})


@pytest.fixture(autouse=True)
def _sys_modules_fence():
	snapshot = dict(sys.modules)
	original_frappe = sys.modules.get("frappe")
	try:
		yield
	finally:
		# Detect frappe-swap BEFORE we restore sys.modules — that's the
		# signal that cached frappe_profiler.* modules now hold stale refs.
		frappe_was_swapped = (
			"frappe" in sys.modules
			and sys.modules["frappe"] is not original_frappe
		)

		current = set(sys.modules.keys())
		original = set(snapshot.keys())
		# Drop modules the test added that match the pollution-prone set
		# (or are patch modules a test re-imported).
		for added in current - original:
			if added in _POLLUTION_PRONE_MODULES or added.startswith(
				"frappe_profiler.patches."
			):
				del sys.modules[added]
		# Restore any module whose value was swapped out.
		for k, original_mod in snapshot.items():
			if sys.modules.get(k) is not original_mod:
				sys.modules[k] = original_mod

		# If frappe was swapped during this test, evict the cached
		# frappe-dependent leaf modules. They captured the stub at module
		# top during their import — restoring sys.modules doesn't repair
		# that. Eviction forces a fresh import on next use, which rebinds
		# their ``import frappe`` to the now-restored real module.
		if frappe_was_swapped:
			for leaf in _FRAPPE_DEPENDENT_LEAVES:
				sys.modules.pop(leaf, None)


def load_fixture(name: str) -> dict:
	"""Load a JSON fixture from tests/fixtures/<name>.json."""
	path = os.path.join(FIXTURES_DIR, f"{name}.json")
	with open(path, encoding="utf-8") as f:
		return json.load(f)


@pytest.fixture
def n_plus_one_recording():
	return load_fixture("n_plus_one_recording")


@pytest.fixture
def full_scan_recording():
	return load_fixture("full_scan_recording")


@pytest.fixture
def clean_recording():
	return load_fixture("clean_recording")


@pytest.fixture
def empty_context():
	"""Minimal AnalyzeContext — just enough to satisfy analyzer signatures."""
	from frappe_profiler.analyzers.base import AnalyzeContext

	return AnalyzeContext(session_uuid="test-uuid", docname="test-docname")
