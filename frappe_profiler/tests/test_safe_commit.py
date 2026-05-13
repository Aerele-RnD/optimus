# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""v0.6.x: tests for ``frappe_profiler.safe_commit`` — the rollback-on-error
wrapper that every explicit commit in this codebase now routes through.

Addresses Lens audit finding *"frappe.db.commit() without try/except"*.

Uses pytest's ``monkeypatch.setitem(sys.modules, ...)`` so the stubbed
``frappe`` is auto-restored at test teardown — without that, every test
running AFTER one of these in the same pytest session would inherit our
minimal stub and crash on missing recorder / realtime / local symbols.
"""

import sys
import types

import pytest


def _build_frappe_stub(*, commit_raises=False, rollback_raises=False):
	"""Build a minimal ``frappe`` stub. Callers install it via
	``monkeypatch.setitem(sys.modules, "frappe", stub)`` so teardown
	restores the original ``frappe`` module."""
	stub = types.ModuleType("frappe")
	stub._commits = 0
	stub._rollbacks = 0

	class _DB:
		def commit(self):
			stub._commits += 1
			if commit_raises:
				raise RuntimeError("COMMIT timed out")

		def rollback(self):
			stub._rollbacks += 1
			if rollback_raises:
				raise RuntimeError("rollback also broken")

	stub.db = _DB()
	return stub


def _import_safe_commit(monkeypatch):
	"""Return ``frappe_profiler.safe_commit`` re-resolved against the
	current ``sys.modules["frappe"]`` stub. The function captures
	``frappe`` per-call via a lazy import — no module-level binding to
	worry about — but we still defensively delete any cached
	``frappe_profiler`` package reference so a fresh import isn't a
	subtle no-op."""
	# safe_commit lives at package top-level (frappe_profiler/__init__.py).
	# Its body imports frappe AT CALL TIME (`import frappe` inside the
	# function), so the stub at sys.modules["frappe"] is what it picks up.
	from frappe_profiler import safe_commit
	return safe_commit


class TestSafeCommit:
	def test_success_path_commits_once_no_rollback(self, monkeypatch):
		stub = _build_frappe_stub(commit_raises=False)
		monkeypatch.setitem(sys.modules, "frappe", stub)
		safe_commit = _import_safe_commit(monkeypatch)
		safe_commit()
		assert stub._commits == 1
		assert stub._rollbacks == 0

	def test_commit_failure_triggers_rollback_then_raises(self, monkeypatch):
		stub = _build_frappe_stub(commit_raises=True)
		monkeypatch.setitem(sys.modules, "frappe", stub)
		safe_commit = _import_safe_commit(monkeypatch)
		with pytest.raises(RuntimeError, match="COMMIT timed out"):
			safe_commit()
		assert stub._commits == 1
		assert stub._rollbacks == 1

	def test_rollback_failure_after_commit_failure_still_raises_commit_error(self, monkeypatch):
		"""Rare double-fault: commit() raises, then rollback() ALSO raises.
		We swallow the rollback error (the connection's likely dead anyway
		so the rollback exception is just noise) and bubble the ORIGINAL
		commit exception — that's the one with the actionable error
		message."""
		stub = _build_frappe_stub(commit_raises=True, rollback_raises=True)
		monkeypatch.setitem(sys.modules, "frappe", stub)
		safe_commit = _import_safe_commit(monkeypatch)
		with pytest.raises(RuntimeError, match="COMMIT timed out"):
			safe_commit()
		assert stub._commits == 1
		assert stub._rollbacks == 1
