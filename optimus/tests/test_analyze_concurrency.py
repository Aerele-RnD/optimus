# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Concurrency guards on analyze (v0.7.x).

Two analyze jobs running at once roughly double peak RAM — the OOM trigger on
long flows. M2 adds a global single-flight (only one session does the heavy
phase at a time) built on the existing re-enqueue/yield pattern, NOT a held
lock, so it can't deadlock with run()'s self-re-enqueue and self-heals if a
worker dies. M3 dedups duplicate analyze enqueues for the same session.
"""

import inspect
import types

import frappe
import pytest

from optimus import analyze


class FakeCache:
	def __init__(self, initial=None):
		self.store = dict(initial or {})

	def get_value(self, k):
		return self.store.get(k)

	def set_value(self, k, v, expires_in_sec=None):
		self.store[k] = v

	def delete_value(self, k):
		self.store.pop(k, None)


class FakeDB:
	def __init__(self):
		self.set_calls = []

	def set_value(self, doctype, name, field_or_dict, value=None):
		self.set_calls.append((doctype, name, field_or_dict, value))

	def commit(self):
		pass


@pytest.fixture
def sf_env(monkeypatch):
	"""Wire the single-flight helper's collaborators with fakes."""
	cache = FakeCache()
	db = FakeDB()
	enqueued = []

	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "db", db, raising=False)
	monkeypatch.setattr(frappe, "conf", types.SimpleNamespace(get=lambda k, d=None: d), raising=False)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: enqueued.append((a, k)), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	# Collaborators bound on the analyze module.
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: False, raising=False)
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None, raising=False)
	monkeypatch.setattr(analyze.time, "sleep", lambda *a, **k: None)
	return types.SimpleNamespace(cache=cache, db=db, enqueued=enqueued)


_KEY = "optimus:analyze:inflight"


class TestAcquireSingleFlight:
	def test_free_acquires_and_marks_holder(self, sf_env):
		assert analyze._acquire_singleflight("A", "PS-A", None) is True
		assert sf_env.cache.get_value(_KEY) == "A"
		assert sf_env.enqueued == []  # no re-enqueue when acquired

	def test_own_uuid_reenters(self, sf_env):
		sf_env.cache.set_value(_KEY, "A")
		assert analyze._acquire_singleflight("A", "PS-A", None) is True
		assert sf_env.enqueued == []

	def test_busy_reenqueues_and_returns_false(self, sf_env):
		sf_env.cache.set_value(_KEY, "OTHER")
		assert analyze._acquire_singleflight("B", "PS-B", None) is False
		# Re-enqueued itself, carrying a single-flight deadline.
		assert len(sf_env.enqueued) == 1
		_, kwargs = sf_env.enqueued[0]
		assert kwargs.get("session_uuid") == "B"
		assert "_singleflight_deadline" in kwargs
		# Did NOT steal the holder.
		assert sf_env.cache.get_value(_KEY) == "OTHER"

	def test_deadline_passed_degrades_to_proceed(self, sf_env):
		sf_env.cache.set_value(_KEY, "OTHER")
		# Deadline already in the past → proceed rather than strand.
		assert analyze._acquire_singleflight("B", "PS-B", 1.0) is True
		assert sf_env.enqueued == []

	def test_skipped_when_scheduler_disabled(self, sf_env, monkeypatch):
		monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: True)
		sf_env.cache.set_value(_KEY, "OTHER")
		# Inline path: no worker to yield to → proceed, don't touch cache/enqueue.
		assert analyze._acquire_singleflight("B", "PS-B", None) is True
		assert sf_env.enqueued == []


class TestReleaseSingleFlight:
	def test_releases_only_when_owner(self, sf_env):
		sf_env.cache.set_value(_KEY, "A")
		analyze._release_singleflight("A")
		assert sf_env.cache.get_value(_KEY) is None

	def test_does_not_clobber_other_holder(self, sf_env):
		sf_env.cache.set_value(_KEY, "OTHER")
		analyze._release_singleflight("A")
		assert sf_env.cache.get_value(_KEY) == "OTHER"


class TestRunWiring:
	def test_run_releases_singleflight_in_finally(self):
		src = inspect.getsource(analyze.run)
		# Release must be in the finally so a crash never strands the flag.
		assert "_release_singleflight" in src
		finally_idx = src.rfind("finally:")
		assert finally_idx != -1 and "_release_singleflight" in src[finally_idx:]

	def test_run_gates_on_singleflight_before_fetch(self):
		src = inspect.getsource(analyze.run)
		assert "_acquire_singleflight" in src

	def test_self_reenqueues_stay_anonymous(self):
		"""Neither the bg-wait nor the single-flight re-enqueue may carry a
		stable job_id — that would deadlock the running job against its own
		continuation."""
		for fn in (analyze._bg_wait_for_pending_jobs, analyze._acquire_singleflight):
			src = inspect.getsource(fn)
			assert "job_id" not in src, f"{fn.__name__} must re-enqueue anonymously"


def _stub_scheduler(monkeypatch, disabled):
	import sys
	mod = types.ModuleType("frappe.utils.scheduler")
	mod.is_scheduler_disabled = lambda: disabled
	monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", mod)


class TestEnqueueAnalyzeAsync:
	"""The async enqueue must NOT use a stable job_id + is_job_enqueued dedup.
	That guard stranded sessions at 'Stopping' when an OOM-killed worker left a
	zombie STARTED job: the guard skipped the enqueue and nothing transitioned
	the session. Concurrent-analyze RAM is bounded by analyze.run's
	single-flight, so we always enqueue (anonymously)."""

	def test_async_enqueue_is_anonymous_and_always_fires(self, monkeypatch):
		from optimus import api
		_stub_scheduler(monkeypatch, disabled=False)
		captured = {}
		monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: captured.update(k))
		monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

		ran_inline = api._enqueue_analyze("uuid-1", docname="PS-1")
		assert ran_inline is False  # async path, session still mid-flight
		assert captured.get("now") is False
		assert captured.get("session_uuid") == "uuid-1"
		# No stable job_id → no is_job_enqueued strand.
		assert "job_id" not in captured

	def test_enqueue_analyze_has_no_is_job_enqueued_dedup(self):
		# Source guard: the dedup that stranded sessions must stay gone. Check
		# the import + the call (not prose — the comment explains the removal).
		from optimus import api
		src = inspect.getsource(api._enqueue_analyze)
		assert "import is_job_enqueued" not in src
		assert "is_job_enqueued(" not in src
