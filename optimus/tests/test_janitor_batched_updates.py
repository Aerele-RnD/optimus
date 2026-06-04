# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.x: tests for the janitor sweeps after the Lens-audit performance
fixes. Every sweep that previously called ``frappe.db.set_value`` inside
a loop now calls it ONCE with a ``{"name": ("in", [...])}`` filter, and
``_sweep_old_sessions`` preloads every File-doc name in a single
``frappe.get_all`` instead of looking each up per row.

We stub ``frappe`` so these tests run pure (no bench / no MariaDB) — via
``monkeypatch.setitem(sys.modules, ...)`` so the real ``frappe`` (plus
the lazy submodules) is restored at teardown. Without that, every test
running AFTER one of these in the same pytest session inherits our
minimal stub and crashes on missing attributes."""

import importlib
import sys
import types


def _install_frappe_stub(monkeypatch):
	"""Build a minimal ``frappe`` stub the janitor module can import.

	All ``sys.modules`` mutations go through ``monkeypatch.setitem`` so
	pytest restores the originals at teardown — that's the entire point
	of routing through this helper instead of bare assignments."""
	stub = types.ModuleType("frappe")
	stub._set_value_calls = []
	stub._get_all_calls = []
	stub._delete_doc_calls = []
	stub._committed = 0
	stub._logger = types.SimpleNamespace(info=lambda msg: None)
	stub._get_all_return = {}  # keyed by doctype → list of rows
	stub._enqueue_calls = []

	class _DB:
		def get_all(self, doctype, filters=None, fields=None, **kwargs):
			stub._get_all_calls.append((doctype, filters, tuple(fields or ())))
			return list(stub._get_all_return.get(doctype, []))

		def set_value(self, doctype, name_or_filters, fieldname_or_dict, *args, **kwargs):
			stub._set_value_calls.append((doctype, name_or_filters, fieldname_or_dict))
			return True

		def commit(self):
			stub._committed += 1

		def get_value(self, *args, **kwargs):
			return None

		def sql(self, *args, **kwargs):
			return []

	stub.db = _DB()
	# Allow `frappe.db.get_all` (used by janitor) AND `frappe.get_all`
	# (used by other modules) to share the recorder.
	stub.get_all = stub.db.get_all
	stub.delete_doc = lambda *a, **kw: stub._delete_doc_calls.append((a, kw))
	stub.enqueue = lambda *a, **kw: stub._enqueue_calls.append((a, kw))
	stub.log_error = lambda **kw: None
	stub.logger = lambda: stub._logger
	stub.as_json = lambda v: '["stub"]'
	stub.conf = {}
	monkeypatch.setitem(sys.modules, "frappe", stub)

	# Helper utils janitor.py imports.
	fu = types.ModuleType("frappe.utils")
	def _now():
		from datetime import datetime
		return datetime(2026, 5, 13, 12, 0, 0)
	def _add_to_date(d, **kw):
		# Cheap shim: return a sentinel string the test inspects via filters.
		return f"cutoff({kw})"
	fu.now_datetime = _now
	fu.add_to_date = _add_to_date
	monkeypatch.setitem(sys.modules, "frappe.utils", fu)

	# Session-helper stubs.
	session_mod = types.ModuleType("optimus.session")
	session_mod.clear_active_session = lambda user: None
	monkeypatch.setitem(sys.modules, "optimus.session", session_mod)

	# Phase-2 capture stub.
	lp_capture = types.ModuleType("optimus.line_profile.capture")
	lp_capture.cleanup_run = lambda run_uuid: None
	monkeypatch.setitem(sys.modules, "optimus.line_profile.capture", lp_capture)

	return stub


def _reload_janitor(monkeypatch):
	"""Re-import janitor.py under the fresh frappe stub. We rely on
	``importlib.reload`` rather than ``monkeypatch.delitem`` because
	the delitem-then-import dance interacted badly with monkeypatch's
	teardown ordering (surfaced as ``ImportError: module not in
	sys.modules`` mid-suite). Plain reload re-runs the top-level code
	cleanly under the current stub. ``monkeypatch`` is kept in the
	signature for parity with ``_install_frappe_stub`` and so future
	additions have it on hand."""
	import optimus.janitor as janitor
	return importlib.reload(janitor)


# --------------------------------------------------------------------------
# Stale Recording sweep — single batched UPDATE.
# --------------------------------------------------------------------------

class TestSweepStaleRecording:
	def test_one_set_value_call_for_N_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-1", "session_uuid": "u1", "user": "a@b.com"},
			{"name": "PS-2", "session_uuid": "u2", "user": "c@d.com"},
			{"name": "PS-3", "session_uuid": "u3", "user": "e@f.com"},
		]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()

		# EXACTLY ONE set_value call, filtered by name IN [...].
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Session"]
		assert len(set_value_calls) == 1
		doctype, filters, fields = set_value_calls[0]
		assert doctype == "Optimus Session"
		assert filters == {"name": ("in", ["PS-1", "PS-2", "PS-3"])}
		assert fields["status"] == "Stopping"
		assert "stopped_at" in fields
		# Per-row enqueue side effects still fire — once per row.
		assert len(stub._enqueue_calls) == 3

	def test_no_set_value_when_no_stale_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()
		assert stub._set_value_calls == []


# --------------------------------------------------------------------------
# Stuck Analyzing sweep — single batched UPDATE.
# --------------------------------------------------------------------------

class TestSweepStuckAnalyzing:
	def test_one_set_value_call_for_N_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-A"},
			{"name": "PS-B"},
		]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stuck_analyzing()
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Session"]
		assert len(set_value_calls) == 1
		_, filters, fields = set_value_calls[0]
		assert filters == {"name": ("in", ["PS-A", "PS-B"])}
		assert fields["status"] == "Failed"
		assert "analyzer_warnings" in fields

	def test_no_set_value_when_no_stuck_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stuck_analyzing()
		assert stub._set_value_calls == []


# --------------------------------------------------------------------------
# Stale Stopping sweep — re-enqueue analyze for sessions stranded at Stopping.
# --------------------------------------------------------------------------

class TestSweepStaleStopping:
	def test_reenqueues_each_stale_stopping_row(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-1", "session_uuid": "u1"},
			{"name": "PS-2", "session_uuid": "u2"},
		]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_stopping()

		# One batched set_value re-affirming Stopping (bumps modified → backoff).
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Session"]
		assert len(set_value_calls) == 1
		_, filters, fields = set_value_calls[0]
		assert filters == {"name": ("in", ["PS-1", "PS-2"])}
		assert fields["status"] == "Stopping"
		# Analyze re-enqueued once per stranded row.
		assert len(stub._enqueue_calls) == 2
		methods = [c[0][0] for c in stub._enqueue_calls]
		assert methods == ["optimus.analyze.run", "optimus.analyze.run"]

	def test_filters_on_stopping_status(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_stopping()
		# Selected by status IN (Stopping, Capturing Background Jobs).
		ga = [c for c in stub._get_all_calls if c[0] == "Optimus Session"]
		assert ga and ga[0][1].get("status") == ["in", ["Stopping", "Capturing Background Jobs"]]
		assert stub._enqueue_calls == []

	def test_wired_into_sweep_stale_sessions(self, monkeypatch):
		import inspect
		_install_frappe_stub(monkeypatch)
		janitor = _reload_janitor(monkeypatch)
		assert "_sweep_stale_stopping" in inspect.getsource(janitor.sweep_stale_sessions)


# --------------------------------------------------------------------------
# Stale Phase-2 sweep — one batched UPDATE per branch (Recording + Analyzing).
# --------------------------------------------------------------------------

class TestSweepStalePhase2Runs:
	def test_one_set_value_call_per_branch(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		# The janitor calls get_all twice on "Optimus Phase Two Run" (formerly
		# "Optimus Phase 2 Run" before the v0.6.x Title-Case rename) — once for
		# Recording rows, once for Analyzing. We return DIFFERENT row sets on
		# each call, so the side-effect channel needs an index counter.
		rec_rows = [
			{"name": "PR-rec-1", "parent": "PS-x", "run_uuid": "rrec1"},
			{"name": "PR-rec-2", "parent": "PS-x", "run_uuid": "rrec2"},
		]
		ana_rows = [
			{"name": "PR-ana-1", "parent": "PS-y", "run_uuid": "rana1"},
		]
		# Override get_all to alternate per call.
		state = {"i": 0, "sequence": [rec_rows, ana_rows]}
		def _get_all(doctype, filters=None, fields=None, **kwargs):
			stub._get_all_calls.append((doctype, filters, tuple(fields or ())))
			if doctype == "Optimus Phase Two Run":
				out = state["sequence"][state["i"]] if state["i"] < len(state["sequence"]) else []
				state["i"] += 1
				return list(out)
			return []
		stub.db.get_all = _get_all
		stub.get_all = _get_all

		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_phase2_runs()

		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Phase Two Run"]
		# Exactly 2 batched set_values (one per branch).
		assert len(set_value_calls) == 2
		# Recording branch — first call.
		assert set_value_calls[0][1] == {"name": ("in", ["PR-rec-1", "PR-rec-2"])}
		# Analyzing branch — second call.
		assert set_value_calls[1][1] == {"name": ("in", ["PR-ana-1"])}

	def test_no_set_value_when_no_phase2_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Phase Two Run"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_phase2_runs()
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Phase Two Run"]
		assert set_value_calls == []


# --------------------------------------------------------------------------
# _delete_older_than_days (a.k.a. _sweep_old_sessions) — ONE bulk File fetch.
# --------------------------------------------------------------------------

class TestSweepOldSessionsBulkFileFetch:
	def test_one_get_all_for_files_regardless_of_session_count(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		# 3 sessions, each with 2 file URLs → 6 candidate URLs total.
		sessions = [
			{"name": f"PS-{i}", "title": f"t{i}",
			 "raw_report_file": f"/private/files/r{i}.html",
			 "raw_report_pdf_file": f"/private/files/r{i}.pdf"}
			for i in range(3)
		]
		# get_all sequence: first call → Optimus Session list; second call → File lookup.
		state = {"i": 0}
		def _get_all(doctype, filters=None, fields=None, **kwargs):
			stub._get_all_calls.append((doctype, filters, tuple(fields or ())))
			state["i"] += 1
			if doctype == "Optimus Session":
				return list(sessions)
			if doctype == "File":
				# Return matching File rows for half the URLs to exercise both
				# "found" and "not found" branches.
				urls = filters["file_url"][1] if isinstance(filters, dict) else []
				return [{"name": f"File-{u}", "file_url": u} for u in urls if "0" in u or "1" in u]
			return []
		stub.db.get_all = _get_all
		stub.get_all = _get_all

		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_old_sessions()

		# EXACTLY one get_all call against "File" — not 6.
		file_calls = [c for c in stub._get_all_calls if c[0] == "File"]
		assert len(file_calls) == 1, (
			f"expected ONE batched File lookup, got {len(file_calls)}: {file_calls}"
		)
		# And the in-list matches every candidate URL.
		filters = file_calls[0][1]
		assert set(filters["file_url"][1]) == {
			"/private/files/r0.html", "/private/files/r0.pdf",
			"/private/files/r1.html", "/private/files/r1.pdf",
			"/private/files/r2.html", "/private/files/r2.pdf",
		}
		# Session deletion fires per row (that part isn't batched — it's a
		# delete with permissions + lifecycle events).
		ps_delete_calls = [c for c in stub._delete_doc_calls if c[0][0] == "Optimus Session"]
		assert len(ps_delete_calls) == 3


# --------------------------------------------------------------------------
# v0.13.x: ``session_retention_days = 0`` means keep forever — the daily
# sweep early-returns without deleting anything. Closes the gap where the
# pre-v0.13.x ``or DEFAULT_RETENTION_DAYS`` silently fell back to 90 and
# made the field description's "Set to 0 to keep forever" promise a lie.
# --------------------------------------------------------------------------

class TestSweepOldSessionsForeverRetention:
	def _stub_get_config(self, monkeypatch, retention_days):
		"""Install a fake ``optimus.settings`` whose ``get_config()``
		returns an object with the chosen ``session_retention_days``.
		Wholesale-replaces the module so the janitor's lazy import
		picks up the fake rather than the real settings reader (which
		needs frappe.cache + DocType wiring this test deliberately
		doesn't provide)."""
		fake = types.ModuleType("optimus.settings")
		fake.get_config = lambda: types.SimpleNamespace(
			session_retention_days=retention_days,
		)
		monkeypatch.setitem(sys.modules, "optimus.settings", fake)

	def test_zero_retention_is_a_silent_noop(self, monkeypatch):
		"""``session_retention_days = 0`` → the sweep doesn't query
		Optimus Session at all (early-return before the get_all). No
		deletions, no File lookups."""
		stub = _install_frappe_stub(monkeypatch)
		self._stub_get_config(monkeypatch, retention_days=0)
		janitor = _reload_janitor(monkeypatch)

		janitor._sweep_old_sessions()

		# No queries at all — the early-return fired before the
		# Optimus Session get_all.
		session_calls = [c for c in stub._get_all_calls if c[0] == "Optimus Session"]
		assert session_calls == [], (
			f"forever-retention must skip the session query, got {session_calls!r}"
		)
		# And no deletions.
		assert stub._delete_doc_calls == []

	def test_positive_retention_sweeps_normally(self, monkeypatch):
		"""Sanity: a positive value still sweeps. Guards against an
		over-eager ``<= 0`` check that would also skip the legit case."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		self._stub_get_config(monkeypatch, retention_days=30)
		janitor = _reload_janitor(monkeypatch)

		janitor._sweep_old_sessions()

		# get_all WAS called on Optimus Session this time.
		session_calls = [c for c in stub._get_all_calls if c[0] == "Optimus Session"]
		assert len(session_calls) >= 1
