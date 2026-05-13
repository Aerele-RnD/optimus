# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Optimus Settings controller's warning-on-framework-
apps-in-tracked-apps validation.

Production bug: user populated Tracked Apps with ``frappe`` and
``erpnext`` — misreading the field as "apps to monitor". Inclusion-
mode semantics kicked in and flooded their actionable findings list
with framework noise (a 1078-query query_builder N+1 that should
have gone to Observations landed in Findings). The controller now
flashes a clear warning when framework apps are added so the
misconfiguration surfaces at save time instead of in a bad report.
"""

import sys
import types

import pytest


def _install_frappe_stub(monkeypatch):
	"""Install a frappe stub with the minimum surface the controller
	needs — msgprint, log_error, cache.delete_value, Document parent
	class. Each test gets a fresh ``msgprint`` Mock that collects calls.
	"""
	stub = types.ModuleType("frappe")
	stub.msgprint_calls = []

	def _msgprint(msg, title=None, indicator=None, **kwargs):
		stub.msgprint_calls.append({
			"msg": msg,
			"title": title,
			"indicator": indicator,
		})
	stub.msgprint = _msgprint
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: None,
		get_value=lambda k: None,
		set_value=lambda k, v: None,
	)
	stub.log_error = lambda **kwargs: None

	model_mod = types.ModuleType("frappe.model")
	doc_mod = types.ModuleType("frappe.model.document")

	class Document:
		def __init__(self, **kwargs):
			for k, v in kwargs.items():
				setattr(self, k, v)

		def get(self, k, default=None):
			return getattr(self, k, default)

	doc_mod.Document = Document
	monkeypatch.setitem(sys.modules, "frappe", stub)
	monkeypatch.setitem(sys.modules, "frappe.model", model_mod)
	monkeypatch.setitem(sys.modules, "frappe.model.document", doc_mod)
	return stub


def _fresh_controller(monkeypatch):
	"""Return a fresh OptimusSettings instance with msgprint-capturing
	frappe stub. All sys.modules mutations route via monkeypatch so the
	real frappe is restored at test teardown — no pollution of
	subsequent test files."""
	stub = _install_frappe_stub(monkeypatch)
	# Force re-import so the controller picks up the fresh stub's msgprint.
	for mod in list(sys.modules.keys()):
		if mod.startswith(
			"optimus.optimus.doctype.optimus_settings"
		):
			monkeypatch.delitem(sys.modules, mod, raising=False)
	from optimus.optimus.doctype.optimus_settings.optimus_settings import (
		OptimusSettings,
	)
	return OptimusSettings, stub


def _row(app_name):
	return types.SimpleNamespace(app_name=app_name)


class TestFrameworkAppWarning:
	def test_warns_when_frappe_added(self, monkeypatch):
		OptimusSettings, stub = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = [_row("frappe")]
		doc._warn_on_framework_apps_in_tracked()
		assert len(stub.msgprint_calls) == 1
		assert "frappe" in stub.msgprint_calls[0]["msg"]
		assert "misconfiguration" in stub.msgprint_calls[0]["title"].lower()
		assert stub.msgprint_calls[0]["indicator"] == "orange"

	def test_warns_when_erpnext_added(self, monkeypatch):
		OptimusSettings, stub = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = [_row("erpnext")]
		doc._warn_on_framework_apps_in_tracked()
		assert len(stub.msgprint_calls) == 1
		assert "erpnext" in stub.msgprint_calls[0]["msg"]

	def test_warns_once_for_both_framework_apps(self, monkeypatch):
		"""frappe + erpnext + custom_app → single warning listing both
		framework apps (not two separate warnings)."""
		OptimusSettings, stub = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = [
			_row("frappe"), _row("erpnext"), _row("my_custom_app"),
		]
		doc._warn_on_framework_apps_in_tracked()
		assert len(stub.msgprint_calls) == 1
		assert "frappe" in stub.msgprint_calls[0]["msg"]
		assert "erpnext" in stub.msgprint_calls[0]["msg"]
		# Custom app shouldn't be in the warning.
		assert "my_custom_app" not in stub.msgprint_calls[0]["msg"]

	def test_no_warning_for_custom_apps_only(self, monkeypatch):
		OptimusSettings, stub = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = [
			_row("my_custom_app"), _row("jewellery_erpnext"),
		]
		doc._warn_on_framework_apps_in_tracked()
		assert stub.msgprint_calls == []

	def test_no_warning_for_empty_tracked_apps(self, monkeypatch):
		OptimusSettings, stub = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = []
		doc._warn_on_framework_apps_in_tracked()
		assert stub.msgprint_calls == []

	def test_frappe_profiler_itself_does_not_trigger_warning(self, monkeypatch):
		"""optimus is in FRAMEWORK_APPS (its own code paths
		should be filtered out of findings) but it's not a
		'framework app' in the UX sense — adding it to Tracked Apps
		is odd but not actively wrong."""
		OptimusSettings, stub = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = [_row("optimus")]
		doc._warn_on_framework_apps_in_tracked()
		# No warning — optimus is meta/self and shouldn't
		# trip the "you probably misread the field" heuristic.
		assert stub.msgprint_calls == []

	def test_all_framework_stock_apps_are_detected(self, monkeypatch):
		"""Every app in FRAMEWORK_APPS (except optimus) must
		trigger the warning. Pins the full list so a future addition
		to FRAMEWORK_APPS is covered by the warning automatically."""
		_install_frappe_stub(monkeypatch)
		from optimus.analyzers.base import FRAMEWORK_APPS

		for app in FRAMEWORK_APPS - {"optimus"}:
			OptimusSettings, stub = _fresh_controller(monkeypatch)
			doc = OptimusSettings()
			doc.tracked_apps = [_row(app)]
			doc._warn_on_framework_apps_in_tracked()
			assert len(stub.msgprint_calls) == 1, (
				f"Adding {app!r} must trigger a warning"
			)


class TestNormalization:
	def test_strips_whitespace_and_dedupes(self, monkeypatch):
		OptimusSettings, _ = _fresh_controller(monkeypatch)
		doc = OptimusSettings()
		doc.tracked_apps = [
			_row("myapp"),
			_row("myapp "),         # trailing space
			_row(" myapp"),         # leading space
			_row("second"),
			_row(""),               # empty — drop
		]
		doc._normalize_tracked_apps()
		names = [r.app_name for r in doc.tracked_apps]
		assert names == ["myapp", "second"]
