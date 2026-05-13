# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.2 round 4 bump_cache_threshold_default patch.

The JSON default for Optimus Settings.redundant_cache_threshold
changed from 10 → 50. Fresh installs get the new default
automatically, but existing Single rows keep the stored value of
10. This patch bumps existing 10 → 50 on migrate, while respecting
any deliberate tuning (e.g., user set it to 20 or 100).
"""

import sys
import types


def _install_frappe_stub(monkeypatch):
	"""Install a minimal ``frappe`` stub via ``monkeypatch.setitem`` so
	pytest restores the real ``frappe`` at teardown (preventing
	cross-test pollution)."""
	stub = types.ModuleType("frappe")
	stub._single_values = {}
	stub._doctype_exists = True

	class _DB:
		def exists(self, doctype, name=None):
			if doctype == "DocType" and name is None:
				return stub._doctype_exists
			if doctype == "DocType":
				return stub._doctype_exists
			return True

		def get_single_value(self, doctype, field):
			return stub._single_values.get(field)

		def set_single_value(self, doctype, field, value):
			stub._single_values[field] = value
			return True

		def commit(self):
			pass

	stub.db = _DB()
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: None,
		get_value=lambda k: None,
		set_value=lambda k, v: None,
	)
	monkeypatch.setitem(sys.modules, "frappe", stub)
	return stub


def _import_patch():
	"""Reload the patch module under the current ``sys.modules["frappe"]``
	stub. ``importlib.reload`` re-runs top-level code (including the
	``import frappe`` at the top of the patch), which rebinds the
	module's local ``frappe`` to whatever's in sys.modules now."""
	import importlib

	import optimus.patches.v0_5_2.bump_cache_threshold_default as patch_mod
	return importlib.reload(patch_mod)


class TestBumpCacheThreshold:
	def test_bumps_exactly_10_to_50(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["redundant_cache_threshold"] = 10
		patch = _import_patch()
		patch.execute()
		assert stub._single_values["redundant_cache_threshold"] == 50

	def test_does_not_overwrite_deliberate_custom_value(self, monkeypatch):
		"""User set a custom threshold like 20 or 100 — don't touch it."""
		for custom in [5, 20, 30, 75, 100, 500]:
			stub = _install_frappe_stub(monkeypatch)
			stub._single_values["redundant_cache_threshold"] = custom
			patch = _import_patch()
			patch.execute()
			assert stub._single_values["redundant_cache_threshold"] == custom, (
				f"Patch must not overwrite user-tuned value {custom}"
			)

	def test_leaves_50_alone(self, monkeypatch):
		"""Idempotent: if the value is already 50, do nothing."""
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["redundant_cache_threshold"] = 50
		patch = _import_patch()
		patch.execute()
		assert stub._single_values["redundant_cache_threshold"] == 50

	def test_no_op_when_doctype_missing(self, monkeypatch):
		"""On a fresh install where the DocType isn't yet synced,
		patch should be a no-op without raising."""
		stub = _install_frappe_stub(monkeypatch)
		stub._doctype_exists = False
		patch = _import_patch()
		patch.execute()
		# Nothing was set.
		assert stub._single_values == {}

	def test_no_op_when_value_is_none(self, monkeypatch):
		"""Defensive: a DocType row that exists but has no value for
		the field yet must not raise / crash migration."""
		stub = _install_frappe_stub(monkeypatch)
		# Explicitly leave it missing from _single_values.
		patch = _import_patch()
		patch.execute()
		# Missing field → get_single_value returns None → patch returns.
		assert "redundant_cache_threshold" not in stub._single_values

	def test_handles_non_integer_stored_value(self, monkeypatch):
		"""If somehow the stored value is a string (legacy data),
		the patch must not crash."""
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["redundant_cache_threshold"] = "not-a-number"
		patch = _import_patch()
		# Should NOT raise.
		patch.execute()
		# Left unchanged (wasn't exactly 10).
		assert stub._single_values["redundant_cache_threshold"] == "not-a-number"


class TestPatchRegistered:
	def test_patches_txt_lists_patch(self):
		import os
		patches_txt = os.path.join(
			os.path.dirname(__file__), "..", "patches.txt"
		)
		with open(patches_txt) as f:
			entries = f.read()
		assert (
			"optimus.patches.v0_5_2.bump_cache_threshold_default"
			in entries
		), (
			"patches.txt must register the bump_cache_threshold_default "
			"patch — otherwise bench migrate won't run it"
		)
