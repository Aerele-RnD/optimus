# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""P3: opt-in auto-arm of a Phase-2 line-profile pass after analyze.

When ``optimus_phase2_auto_arm`` is set in site_config, analyze arms a pass on
the recommended hot-path functions so the user just re-runs the flow once to
get line data. Opt-in + admin-only (it instruments the next execution — only
for replay-safe flows). Heavily guarded and best-effort (never fails analyze).
"""

import inspect
import types

import frappe
import pytest

from optimus import analyze


class FakeCache:
	def __init__(self, store=None):
		self.store = dict(store or {})

	def get_value(self, k):
		return self.store.get(k)


class FakeDoc:
	def __init__(self, user="u@x.com", phase_2_runs=None, actions=None):
		self.user = user
		self._tables = {"phase_2_runs": list(phase_2_runs or []), "actions": list(actions or [])}
		self.flags = types.SimpleNamespace()
		self.saved = False

	def get(self, key):
		return self._tables.get(key)

	def append(self, table, row):
		self._tables.setdefault(table, []).append(row)
		return row

	def save(self, *a, **k):
		self.saved = True


@pytest.fixture
def arm_env(monkeypatch):
	"""Wire _auto_arm_phase2's collaborators. Defaults: conf ON, user free,
	one recommended candidate, capture.start returns it eligible."""
	state = types.SimpleNamespace(
		conf={"optimus_phase2_auto_arm": True},
		doc=FakeDoc(),
		cache=FakeCache(),
		candidates=[{"dotted_path": "ugly_code.python.common.bg_recheck_users",
			"recommended": True}],
		start_calls=[],
		published=[],
	)
	monkeypatch.setattr(frappe, "conf",
		types.SimpleNamespace(get=lambda k, d=None: state.conf.get(k, d)), raising=False)
	monkeypatch.setattr(frappe, "cache", state.cache, raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: state.doc, raising=False)
	monkeypatch.setattr(frappe, "as_json", lambda v: "[]", raising=False)
	monkeypatch.setattr(frappe, "publish_realtime",
		lambda *a, **k: state.published.append((a, k)), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(frappe, "logger",
		lambda: types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
		raising=False)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None, raising=False)

	# get_config → phase2_max_runs_per_session cap.
	import optimus.settings as settings_mod
	monkeypatch.setattr(settings_mod, "get_config",
		lambda: types.SimpleNamespace(phase2_max_runs_per_session=10), raising=False)

	# Patch the line_profile collaborators on the REAL (already-imported)
	# modules — `from optimus.line_profile import capture` binds the package
	# attribute, so sys.modules swapping wouldn't take effect under bench.
	import optimus.line_profile.capture as cap_real
	import optimus.line_profile.picker as picker_real

	def _start(session_uuid, run_uuid, user, picks):
		state.start_calls.append({"picks": picks, "user": user})
		# Echo picks back as eligible (the resolved-meta shape).
		return [{"dotted_path": p["dotted_path"], "source": p.get("source", "curated"),
			"eligible": True} for p in picks]
	monkeypatch.setattr(cap_real, "start_line_profile_pass", _start, raising=False)
	monkeypatch.setattr(picker_real, "_build_tree_indented_candidates",
		lambda trees: state.candidates, raising=False)

	# Deterministic now_datetime for the appended row.
	import frappe.utils as futils
	monkeypatch.setattr(futils, "now_datetime", lambda: "2026-05-21 09:00:00", raising=False)

	return state


def _ctx():
	return types.SimpleNamespace(session_uuid="sess-1")


class TestAutoArmPhase2:
	def test_armed_when_enabled_with_recommended_picks(self, arm_env):
		analyze._auto_arm_phase2("PS-1", _ctx())
		assert len(arm_env.start_calls) == 1
		assert arm_env.start_calls[0]["picks"] == [
			{"dotted_path": "ugly_code.python.common.bg_recheck_users", "source": "curated"}
		]
		# A Recording row was appended.
		runs = arm_env.doc.get("phase_2_runs")
		assert len(runs) == 1 and runs[0]["status"] == "Recording"
		assert arm_env.doc.saved is True

	def test_publishes_armed_alert_with_count(self, arm_env):
		"""On arm, the user gets a realtime alert telling them to re-run + Stop
		(auto-arm happens async during analyze, off-form)."""
		analyze._auto_arm_phase2("PS-1", _ctx())
		assert len(arm_env.published) == 1
		args, kwargs = arm_env.published[0]
		event = args[0] if args else kwargs.get("event")
		assert event == "optimus_phase2_armed"
		payload = (args[1] if len(args) > 1 else kwargs.get("message")) or {}
		assert payload.get("count") == 1

	def test_noop_when_disabled(self, arm_env):
		arm_env.conf["optimus_phase2_auto_arm"] = False
		analyze._auto_arm_phase2("PS-1", _ctx())
		assert arm_env.start_calls == []
		assert arm_env.published == []  # no alert when nothing armed

	def test_noop_when_no_recommended_candidates(self, arm_env):
		arm_env.candidates = [{"dotted_path": "x.y", "recommended": False}]
		analyze._auto_arm_phase2("PS-1", _ctx())
		assert arm_env.start_calls == []

	def test_noop_when_over_run_cap(self, arm_env, monkeypatch):
		import optimus.settings as settings_mod
		monkeypatch.setattr(settings_mod, "get_config",
			lambda: types.SimpleNamespace(phase2_max_runs_per_session=2))
		arm_env.doc._tables["phase_2_runs"] = [{"run_uuid": "a"}, {"run_uuid": "b"}]
		analyze._auto_arm_phase2("PS-1", _ctx())
		assert arm_env.start_calls == []

	def test_noop_when_user_busy(self, arm_env):
		arm_env.cache.store["profiler:lp:active:u@x.com"] = "some-run"
		analyze._auto_arm_phase2("PS-1", _ctx())
		assert arm_env.start_calls == []

	def test_never_raises(self, arm_env, monkeypatch):
		# Force an internal error; _auto_arm_phase2 must swallow it.
		monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")), raising=False)
		analyze._auto_arm_phase2("PS-1", _ctx())  # must not raise


def test_run_calls_auto_arm_after_ready():
	src = inspect.getsource(analyze.run)
	assert "_auto_arm_phase2" in src
	# Must be after the Ready status write.
	ready_idx = src.find('"status", "Ready"')
	assert ready_idx != -1 and src.find("_auto_arm_phase2", ready_idx) != -1
