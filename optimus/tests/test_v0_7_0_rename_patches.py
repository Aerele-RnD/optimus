# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.7.0 app-rename migration patches.

Five patches handle the upgrade from ``frappe_profiler`` → ``optimus``:

  - ``rewrite_patch_log_module_paths``  — rewrites ``tabPatch Log`` entries.
  - ``rename_module_to_optimus``        — Module Def + ``tabDocType.module``.
  - ``rename_doctypes_to_optimus``      — 6 DocTypes via ``rename_doc``.
  - ``rename_role_to_optimus``          — ``Profiler User`` → ``Optimus User``.
  - ``migrate_settings_tracked_apps``   — child rows referencing old app name.

Each is exercised through every important branch (old exists / both
exist / fresh install / rename raises) with a stubbed ``frappe`` that
``monkeypatch.setitem`` swaps into ``sys.modules``.
"""

import sys
import types

# ---------------------------------------------------------------------------
# Stub builders
# ---------------------------------------------------------------------------


def _install_frappe_stub(monkeypatch, *, exists_table=None, rename_raises=False):
	"""Install a minimal ``frappe`` stub. ``exists_table`` is a dict
	keyed by (doctype, name) → bool; missing keys default to False.
	``rename_raises``: if True, ``frappe.rename_doc`` raises."""
	stub = types.ModuleType("frappe")
	stub._rename_calls = []
	stub._sql_calls = []
	stub._set_value_calls = []
	stub._committed = False
	stub._log_calls = []
	stub._warning_calls = []
	stub._cache_deletes = []
	stub._cleared = []

	exists_table = exists_table or {}

	class _DB:
		def exists(self, doctype, name=None):
			return exists_table.get((doctype, name), False)

		def sql(self, query, args=None):
			stub._sql_calls.append((query, args))
			return []

		def set_value(self, doctype, name, fieldname, value):
			stub._set_value_calls.append((doctype, name, fieldname, value))

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


def _reload(module_dotted: str):
	"""Reload a patch module under the current stub so its ``import
	frappe`` binds to the stub."""
	import importlib

	mod = importlib.import_module(module_dotted)
	return importlib.reload(mod)


# ---------------------------------------------------------------------------
# rewrite_patch_log_module_paths
# ---------------------------------------------------------------------------


class TestRewritePatchLogModulePaths:
	def test_runs_single_update_against_patch_log(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		patch = _reload("optimus.patches.v0_7_0.rewrite_patch_log_module_paths")
		patch.execute()

		assert len(stub._sql_calls) == 1, "patch must issue exactly one UPDATE"
		query, _args = stub._sql_calls[0]
		# Confirm shape: REPLACE + WHERE LIKE filter on old prefix
		assert "UPDATE `tabPatch Log`" in query
		assert "REPLACE(patch, 'frappe_profiler.patches.', 'optimus.patches.')" in query
		assert "WHERE patch LIKE 'frappe_profiler.patches.%%'" in query
		assert stub._committed is True

	def test_swallows_sql_errors_and_logs(self, monkeypatch):
		"""If the UPDATE blows up (unlikely — Patch Log is metadata
		Frappe owns), the patch must NOT crash the migrate session."""
		stub = _install_frappe_stub(monkeypatch)

		def _raise(query, args=None):
			raise RuntimeError("db down")

		stub.db.sql = _raise
		patch = _reload("optimus.patches.v0_7_0.rewrite_patch_log_module_paths")
		patch.execute()  # must not raise

		assert stub._log_calls, "swallowed exception must surface via log_error"
		assert stub._committed is False, "no commit on the error path"


# ---------------------------------------------------------------------------
# rename_module_to_optimus
# ---------------------------------------------------------------------------


class TestRenameModuleToOptimus:
	def test_full_path_when_old_module_exists(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch, exists_table={
			("Module Def", "Frappe Profiler"): True,
		})
		patch = _reload("optimus.patches.v0_7_0.rename_module_to_optimus")
		patch.execute()

		# Step 1: UPDATE tabDocType.module
		assert any(
			"UPDATE tabDocType SET module" in q for q, _ in stub._sql_calls
		)
		# Step 2: rename Module Def via rename_doc
		assert ("Module Def", "Frappe Profiler", "Optimus", True) in stub._rename_calls
		# Step 3: set app_name on the renamed row
		assert ("Module Def", "Optimus", "app_name", "optimus") in stub._set_value_calls
		assert stub._committed is True

	def test_noop_on_fresh_install(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)  # no rows exist
		patch = _reload("optimus.patches.v0_7_0.rename_module_to_optimus")
		patch.execute()

		assert stub._rename_calls == []
		assert stub._committed is False

	def test_logs_and_returns_when_rename_raises(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch,
			exists_table={("Module Def", "Frappe Profiler"): True},
			rename_raises=True,
		)
		patch = _reload("optimus.patches.v0_7_0.rename_module_to_optimus")
		patch.execute()  # must not raise

		assert stub._log_calls
		# set_value must NOT run if rename failed
		assert stub._set_value_calls == []
		assert stub._committed is False


# ---------------------------------------------------------------------------
# rename_doctypes_to_optimus
# ---------------------------------------------------------------------------


PAIRS = (
	("Profiler Action", "Optimus Action"),
	("Profiler Finding", "Optimus Finding"),
	("Profiler Phase Two Run", "Optimus Phase Two Run"),
	("Profiler Tracked App", "Optimus Tracked App"),
	("Profiler Settings", "Optimus Settings"),
	("Profiler Session", "Optimus Session"),
)


class TestRenameDoctypesToOptimus:
	def test_renames_all_six_when_only_old_exists(self, monkeypatch):
		exists = {("DocType", old): True for old, _ in PAIRS}
		stub = _install_frappe_stub(monkeypatch, exists_table=exists)
		patch = _reload("optimus.patches.v0_7_0.rename_doctypes_to_optimus")
		patch.execute()

		renamed = [(d, o, n) for (d, o, n, _force) in stub._rename_calls]
		for old, new in PAIRS:
			assert ("DocType", old, new) in renamed, (
				f"missing rename {old!r} → {new!r}"
			)
		assert stub._committed is True

	def test_noop_on_fresh_install(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		patch = _reload("optimus.patches.v0_7_0.rename_doctypes_to_optimus")
		patch.execute()

		assert stub._rename_calls == []

	def test_skips_pair_when_new_already_exists(self, monkeypatch):
		"""If BOTH the old and new DocTypes exist (partial migration),
		skip that pair and warn — don't risk clashing on the SQL table
		rename."""
		exists = {
			("DocType", "Profiler Session"): True,
			("DocType", "Optimus Session"): True,
		}
		stub = _install_frappe_stub(monkeypatch, exists_table=exists)
		patch = _reload("optimus.patches.v0_7_0.rename_doctypes_to_optimus")
		patch.execute()

		# That pair must not be in the rename calls
		renamed_olds = [o for (_d, o, _n, _f) in stub._rename_calls]
		assert "Profiler Session" not in renamed_olds
		assert any("Profiler Session" in msg and "Optimus Session" in msg
		           for msg in stub._warning_calls)


# ---------------------------------------------------------------------------
# rename_role_to_optimus
# ---------------------------------------------------------------------------


class TestRenameRoleToOptimus:
	def test_renames_when_old_role_exists(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch, exists_table={
			("Role", "Profiler User"): True,
		})
		patch = _reload("optimus.patches.v0_7_0.rename_role_to_optimus")
		patch.execute()

		assert ("Role", "Profiler User", "Optimus User", True) in stub._rename_calls
		assert stub._committed is True

	def test_noop_on_fresh_install(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		patch = _reload("optimus.patches.v0_7_0.rename_role_to_optimus")
		patch.execute()

		assert stub._rename_calls == []
		assert stub._committed is False

	def test_skips_when_both_roles_exist(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch, exists_table={
			("Role", "Profiler User"): True,
			("Role", "Optimus User"): True,
		})
		patch = _reload("optimus.patches.v0_7_0.rename_role_to_optimus")
		patch.execute()

		assert stub._rename_calls == []
		assert stub._warning_calls


# ---------------------------------------------------------------------------
# migrate_settings_tracked_apps
# ---------------------------------------------------------------------------


class TestMigrateSettingsTrackedApps:
	def test_runs_update_against_renamed_table(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		patch = _reload("optimus.patches.v0_7_0.migrate_settings_tracked_apps")
		patch.execute()

		assert len(stub._sql_calls) == 1
		query, _ = stub._sql_calls[0]
		# Targets the post-rename table name (Phase C ordering guarantees
		# the DocType rename runs first).
		assert "UPDATE `tabOptimus Tracked App`" in query
		assert "SET app_name = 'optimus'" in query
		assert "WHERE app_name = 'frappe_profiler'" in query
		assert stub._committed is True


# ---------------------------------------------------------------------------
# patches.txt sequencing
# ---------------------------------------------------------------------------


class TestPatchesTxtSequencing:
	def _read_patches_txt(self):
		import os
		path = os.path.join(os.path.dirname(__file__), "..", "patches.txt")
		with open(path) as f:
			return f.read()

	def test_rewrite_patch_log_is_first(self):
		"""The Patch Log rewrite MUST be the first patch in
		[pre_model_sync] — otherwise subsequent patch existence
		checks see the old (frappe_profiler.*) prefix in tabPatch
		Log and re-run already-executed patches."""
		content = self._read_patches_txt()
		pre_idx = content.index("[pre_model_sync]")
		post_idx = content.index("[post_model_sync]")
		rewrite_idx = content.index(
			"optimus.patches.v0_7_0.rewrite_patch_log_module_paths"
		)
		assert pre_idx < rewrite_idx < post_idx, (
			"rewrite_patch_log_module_paths must live in [pre_model_sync]"
		)
		# And first within that section: no other v0_7_0 patch may
		# precede it.
		pre_block = content[pre_idx:post_idx]
		for other in (
			"rename_module_to_optimus",
			"rename_doctypes_to_optimus",
		):
			other_idx_in_block = pre_block.index(other)
			rewrite_idx_in_block = pre_block.index("rewrite_patch_log_module_paths")
			assert rewrite_idx_in_block < other_idx_in_block, (
				f"{other!r} must come AFTER rewrite_patch_log_module_paths"
			)

	def test_module_rename_runs_before_doctype_rename(self):
		"""Module Def rename must precede DocType rename — Frappe's
		model sync uses Module Def names to validate DocType.module
		values."""
		content = self._read_patches_txt()
		module_idx = content.index("rename_module_to_optimus")
		doctype_idx = content.index("rename_doctypes_to_optimus")
		assert module_idx < doctype_idx

	def test_phase_two_rename_runs_before_doctype_to_optimus(self):
		"""v0.6.0 rename (Profiler Phase 2 Run → Profiler Phase Two
		Run) must precede v0.7.0 rename (Profiler Phase Two Run →
		Optimus Phase Two Run)."""
		content = self._read_patches_txt()
		phase_two_idx = content.index("rename_phase_two_doctype")
		optimus_idx = content.index("rename_doctypes_to_optimus")
		assert phase_two_idx < optimus_idx

	def test_settings_migration_runs_post_model_sync(self):
		"""migrate_settings_tracked_apps reads `tabOptimus Tracked
		App` — which only exists after the DocType rename has been
		flushed to disk and model sync has reconciled. Belongs in
		[post_model_sync]."""
		content = self._read_patches_txt()
		post_idx = content.index("[post_model_sync]")
		settings_idx = content.index("migrate_settings_tracked_apps")
		assert post_idx < settings_idx
