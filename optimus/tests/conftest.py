# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pytest configuration for optimus analyzer tests.

These tests are deliberately decoupled from Frappe — each analyzer is
a pure function over a list of recording dicts, so we can exercise them
with JSON fixtures and no running site. Run with:

    cd apps/optimus
    python -m pytest optimus/tests/ -v

(or just `pytest optimus/tests/ -v` if pytest is installed globally).
"""

import json
import os
import sys

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# --- CI-friendly baseline frappe stub ---------------------------------------
# On a bench host frappe is importable and we use the real module. On CI
# (the GitHub Actions runner, or any pure pip venv) frappe is NOT installed,
# so module-top imports like ``from frappe.utils import now_datetime`` in
# app code (api.py, analyze.py, hooks_callbacks.py, …) fail at COLLECTION
# time and pytest exits with code 2 before any test runs. This baseline
# stub satisfies those imports just enough for collection to succeed; the
# per-test ``_sys_modules_fence`` below still snapshots sys.modules per
# test, so tests that need richer stubs install them via monkeypatch and
# the baseline is restored at teardown.
try:
	import frappe  # noqa: F401
except ImportError:
	import datetime as _dt
	import types as _types

	class _StubValidationError(Exception):
		pass

	def _stub_throw(msg=None, exc=None, **kw):
		raise (exc or _StubValidationError)(msg or "stub frappe.throw")

	def _stub_logger():
		return _types.SimpleNamespace(
			warning=lambda *a, **kw: None,
			info=lambda *a, **kw: None,
			debug=lambda *a, **kw: None,
			error=lambda *a, **kw: None,
		)

	def _stub_get_bench_path():
		# Two levels above the package root, so a relpath of files inside
		# the package against this value yields a tidy display like
		# ``<package>/<package>/<file>.py`` — that's the shape the
		# ``_action_entry_callsite`` tests assert on (``endswith
		# 'optimus/renderer.py'``).
		here = os.path.abspath(__file__)  # .../optimus/tests/conftest.py
		for _ in range(4):
			here = os.path.dirname(here)
		return here

	def _mk_module(name, **attrs):
		m = _types.ModuleType(name)
		# Mark every stub module as a package so submodule attribute access
		# (e.g. ``frappe.utils.X`` after ``import frappe.utils.X``) works.
		m.__path__ = []  # arbitrary — Python only needs the attribute to exist
		for k, v in attrs.items():
			setattr(m, k, v)
		sys.modules[name] = m
		# Link as an attribute on the parent module so ``frappe.utils`` style
		# access works after ``import frappe.utils`` — Python's import machinery
		# normally does this, but only for real packages on disk.
		if "." in name:
			parent_name, leaf = name.rsplit(".", 1)
			parent = sys.modules.get(parent_name)
			if parent is not None:
				setattr(parent, leaf, m)
		return m

	_mk_module(
		"frappe",
		local=_types.SimpleNamespace(),
		db=_types.SimpleNamespace(),
		session=_types.SimpleNamespace(user="Administrator"),
		flags=_types.SimpleNamespace(),
		cache=_types.SimpleNamespace(),
		_dict=dict,
		whitelist=lambda *a, **kw: (lambda f: f),
		throw=_stub_throw,
		log_error=lambda *a, **kw: None,
		msgprint=lambda *a, **kw: None,
		get_doc=lambda *a, **kw: None,
		get_roles=lambda *a, **kw: [],
		get_all=lambda *a, **kw: [],
		get_app_path=lambda *a, **kw: "",
		get_hooks=lambda *a, **kw: {},
		enqueue=lambda *a, **kw: None,
		logger=_stub_logger,
		PermissionError=type("PermissionError", (Exception,), {}),
		ValidationError=_StubValidationError,
		DoesNotExistError=type("DoesNotExistError", (Exception,), {}),
	)
	_mk_module(
		"frappe.utils",
		now_datetime=_dt.datetime.now,
		add_to_date=lambda d, **kw: d,
		cint=int,
		cstr=str,
		flt=float,
		get_datetime=lambda *a, **kw: _dt.datetime.now(),
		time_diff_in_seconds=lambda *a, **kw: 0,
		get_bench_path=_stub_get_bench_path,
	)
	_mk_module("frappe.utils.scheduler", is_scheduler_disabled=lambda: False)
	_mk_module("frappe.utils.background_jobs", enqueue=lambda *a, **kw: None)
	_mk_module("frappe.utils.password", get_decrypted_password=lambda *a, **kw: "")
	_mk_module("frappe.utils.pdf", get_pdf=lambda *a, **kw: b"")
	_mk_module(
		"frappe.utils.redis_wrapper",
		RedisWrapper=type("RedisWrapper", (object,), {
			"get_value": lambda self, *a, **kw: None,
		}),
	)
	_mk_module("frappe.database")
	_mk_module("frappe.database.utils", is_query_type=lambda *a, **kw: False)
	_mk_module(
		"frappe.recorder",
		RECORDER_REQUEST_HASH="recorder:request",
		RECORDER_REQUEST_SPARSE_HASH="recorder:sparse",
		mark_duplicates=lambda *a, **kw: None,
		record=lambda *a, **kw: None,
		dump=lambda *a, **kw: None,
	)
	_mk_module("frappe.model")
	_mk_module("frappe.model.document", Document=type("Document", (object,), {}))
	_mk_module("frappe.client", get_value=lambda *a, **kw: None)
	_mk_module("frappe.permissions", has_permission=lambda *a, **kw: True)
# --- end baseline frappe stub -----------------------------------------------


# v0.6.x: sys.modules auto-restore fence.
#
# Several tests install fake ``frappe`` / ``frappe.recorder`` /
# ``optimus.*`` stubs into ``sys.modules`` to exercise pure-Python
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
	"optimus.session", "optimus.capture",
	"optimus.line_profile.capture",
})

# optimus modules that ``import frappe`` at module top. Their
# top-level binding captures whatever was in ``sys.modules["frappe"]``
# AT IMPORT TIME — so if they get imported while a test has installed a
# stub-frappe (and then the fence restores the real frappe at teardown),
# their captured reference is now stale. We evict these from
# ``sys.modules`` when we detect the frappe-swap so the next test
# re-imports them against the restored real frappe.
_FRAPPE_DEPENDENT_LEAVES = frozenset({
	"optimus.api",
	"optimus.analyze",
	"optimus.hooks_callbacks",
	"optimus.infra_capture",
	"optimus.install",
	"optimus.janitor",
	"optimus.pdf_export",
	"optimus.permissions",
	"optimus.session",
})


@pytest.fixture(autouse=True)
def _sys_modules_fence():
	snapshot = dict(sys.modules)
	original_frappe = sys.modules.get("frappe")
	try:
		yield
	finally:
		# Detect frappe-swap BEFORE we restore sys.modules — that's the
		# signal that cached optimus.* modules now hold stale refs.
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
				"optimus.patches."
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
	from optimus.analyzers.base import AnalyzeContext

	return AnalyzeContext(session_uuid="test-uuid", docname="test-docname")
