# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.5.2 round 4 boot_session hook.

Attaches ``optimus_enabled`` to ``frappe.boot`` so the floating
widget can hide itself when the master kill-switch is off. Prevents
the confusing UX of a disabled-but-visible widget (clicking Start
does nothing because before_request short-circuits).
"""

import sys
import types

import pytest


def _fresh_bootinfo():
	return types.SimpleNamespace()


@pytest.fixture(autouse=True)
def _frappe_stub(monkeypatch):
	"""Install a minimal frappe stub via monkeypatch.setitem so the
	stub is restored at teardown (vs. the leak-via-bare-assignment
	pattern this file used to use)."""
	if "frappe" not in sys.modules:
		monkeypatch.setitem(sys.modules, "frappe", types.ModuleType("frappe"))


class TestBootSession:
	def test_enabled_flag_attached_when_settings_enabled(self, monkeypatch):
		from optimus import boot, settings
		monkeypatch.setattr(settings, "is_enabled", lambda: True)

		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is True

	def test_enabled_flag_false_when_settings_disabled(self, monkeypatch):
		from optimus import boot, settings
		monkeypatch.setattr(settings, "is_enabled", lambda: False)

		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is False

	def test_fails_open_on_settings_read_error(self, monkeypatch):
		"""If settings.is_enabled raises, default to True. Hiding the
		widget due to a settings-read error would be a very confusing
		support issue ('why can't I see the profiler button?')."""
		from optimus import boot, settings

		def boom():
			raise RuntimeError("cache down")

		monkeypatch.setattr(settings, "is_enabled", boom)

		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is True, (
			"boot_session must fail-open — a settings-read error must "
			"NOT hide the widget. Returning False here would silently "
			"break the primary UI."
		)

	def test_returns_bool_not_truthy_value(self, monkeypatch):
		"""The JS guard does strict `=== false` comparison — so this
		must always be a Python bool, not a truthy/falsy value that
		would serialize oddly (e.g. 0, 1, None)."""
		from optimus import boot, settings

		# settings returns 1 (truthy int, but not bool).
		monkeypatch.setattr(settings, "is_enabled", lambda: 1)
		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is True
		assert isinstance(bootinfo.optimus_enabled, bool)


class TestHookWired:
	"""Verify hooks.py actually registers the boot_session handler."""

	def test_boot_session_entry_in_hooks(self):
		import os
		hooks_path = os.path.join(
			os.path.dirname(__file__), "..", "hooks.py"
		)
		with open(hooks_path) as f:
			content = f.read()
		assert 'boot_session = "optimus.boot.boot_session"' in content, (
			"hooks.py must register the boot_session hook — without "
			"it the bootinfo.optimus_enabled flag never reaches the "
			"client and the widget can't hide itself"
		)


class TestWidgetGuard:
	"""Verify the JS guard correctly references the boot flag."""

	def test_widget_checks_profiler_enabled_before_mount(self):
		import os
		js_path = os.path.join(
			os.path.dirname(__file__),
			"..", "public", "js", "floating_widget.js",
		)
		with open(js_path) as f:
			js = f.read()
		# The guard must reference the boot flag.
		assert "frappe.boot.optimus_enabled" in js, (
			"floating_widget.js must check frappe.boot.optimus_enabled "
			"before mounting — otherwise a disabled profiler still "
			"shows the widget"
		)
		# The guard must return/skip mount when the flag is False.
		assert "=== false" in js, (
			"Must use strict === false so a missing/undefined boot "
			"flag (e.g. older boot payload without this field) doesn't "
			"hide the widget — fail-open shape"
		)
