# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""v0.6.x: tests for ``patches/v0_6_0/rename_phase_two_doctype.py`` — the
one-time DocType rename from ``Profiler Phase 2 Run`` →
``Profiler Phase Two Run`` (audit item 2.6).

Stubs ``frappe`` so we can drive the patch through every branch (old
exists, both exist, neither exists, rename fails) without a real bench."""

import sys
import types


OLD = "Profiler Phase 2 Run"
NEW = "Profiler Phase Two Run"


def _install_frappe_stub(monkeypatch, *, old_exists=False, new_exists=False, rename_raises=False):
	"""Install a minimal ``frappe`` stub via ``monkeypatch.setitem`` so
	the real ``frappe`` is restored at teardown (preventing cross-test
	pollution of subsequent tests in the same pytest session)."""
	stub = types.ModuleType("frappe")
	stub._rename_calls = []
	stub._cleared = []
	stub._committed = False
	stub._log_calls = []
	stub._warning_calls = []
	stub._cache_deletes = []

	class _DB:
		def exists(self, doctype, name=None):
			if doctype == "DocType":
				if name == OLD:
					return old_exists
				if name == NEW:
					return new_exists
			return False

		def commit(self):
			stub._committed = True

	stub.db = _DB()

	def _rename_doc(doctype, old, new, force=False):
		stub._rename_calls.append((doctype, old, new, force))
		if rename_raises:
			raise RuntimeError("rename blew up")

	stub.rename_doc = _rename_doc
	stub.clear_cache = lambda doctype=None: stub._cleared.append(doctype)
	stub.log_error = lambda **kw: stub._log_calls.append(kw)
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: stub._cache_deletes.append(k),
	)

	logger = types.SimpleNamespace(
		warning=lambda msg: stub._warning_calls.append(msg),
	)
	stub.logger = lambda: logger

	monkeypatch.setitem(sys.modules, "frappe", stub)
	return stub


def _import_patch():
	"""Reload the patch module under the current ``sys.modules["frappe"]``
	stub. ``importlib.reload`` re-runs top-level code so the patch's
	``import frappe`` binds to the stub, not whatever was cached before."""
	import importlib
	import frappe_profiler.patches.v0_6_0.rename_phase_two_doctype as patch_mod
	return importlib.reload(patch_mod)


class TestRenamePhaseTwoDoctypePatch:
	def test_renames_when_only_old_exists(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch, old_exists=True, new_exists=False)
		patch = _import_patch()
		patch.execute()
		assert stub._rename_calls == [("DocType", OLD, NEW, True)]
		assert NEW in stub._cleared
		assert "profiler_settings_cached" in stub._cache_deletes
		assert stub._committed is True

	def test_no_op_when_neither_exists(self, monkeypatch):
		"""Fresh install: the new DocType is synced from JSON directly.
		Patch must not call rename_doc."""
		stub = _install_frappe_stub(monkeypatch, old_exists=False, new_exists=False)
		patch = _import_patch()
		patch.execute()
		assert stub._rename_calls == []
		assert stub._committed is False

	def test_no_op_when_new_already_exists_alone(self, monkeypatch):
		"""Already migrated install: only the new DocType remains. Patch
		runs but finds no old DocType → no-op."""
		stub = _install_frappe_stub(monkeypatch, old_exists=False, new_exists=True)
		patch = _import_patch()
		patch.execute()
		assert stub._rename_calls == []

	def test_bails_when_both_exist(self, monkeypatch):
		"""Conflict guard: both DocTypes present means a previous partial
		migration. Logging a warning + bailing is safer than guessing."""
		stub = _install_frappe_stub(monkeypatch, old_exists=True, new_exists=True)
		patch = _import_patch()
		patch.execute()
		assert stub._rename_calls == []
		assert stub._committed is False
		assert any("both" in msg.lower() for msg in stub._warning_calls), (
			"Expected a warning naming the duplicate-doctype situation"
		)

	def test_rename_failure_logs_and_does_not_commit(self, monkeypatch):
		"""If rename_doc raises (e.g. table-lock during migrate), the
		patch must NOT commit and must NOT raise — let the operator
		retry migrate."""
		stub = _install_frappe_stub(monkeypatch, old_exists=True, rename_raises=True)
		patch = _import_patch()
		patch.execute()  # must not raise
		assert stub._committed is False
		assert stub._log_calls, "rename failure must be logged"


class TestPatchRegistered:
	def test_patches_txt_lists_in_pre_model_sync(self):
		"""The rename MUST run before model sync — otherwise model sync
		creates a fresh new-name DocType row alongside the old one."""
		import os
		patches_txt = os.path.join(
			os.path.dirname(__file__), "..", "patches.txt"
		)
		with open(patches_txt) as f:
			content = f.read()
		# Confirm the patch name appears.
		assert "frappe_profiler.patches.v0_6_0.rename_phase_two_doctype" in content
		# And specifically in [pre_model_sync] (above [post_model_sync]).
		pre_idx = content.index("[pre_model_sync]")
		post_idx = content.index("[post_model_sync]")
		patch_idx = content.index("rename_phase_two_doctype")
		assert pre_idx < patch_idx < post_idx, (
			"rename_phase_two_doctype must be registered in [pre_model_sync]"
		)
