# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""End-to-end reproduction of Phase-2 line capture for a BACKGROUND-JOB
function — the path P2/P3 assumed works but was never tested.

Drives the real capture functions in-process (no bench/worker): arm a pass,
run the picked function through the before/after_job hooks, aggregate, and
confirm the (basename, qualname) drilldown lookup the finding card uses returns
the hot line. If this passes, capture+linking are sound and any field failure
is environmental/lifecycle; if it fails, the bug is right here.
"""

import sys
import types

import pytest

pytest.importorskip("line_profiler")

import frappe  # noqa: E402

from optimus.line_profile import capture as cap  # noqa: E402
from optimus.line_profile import hooks as lp_hooks  # noqa: E402


class FakeCache:
	"""Dict-backed cache without rpush/lrange, so flush/read use the
	JSON-list fallback (exercises the same aggregate path as production)."""

	def __init__(self):
		self.store = {}

	def get_value(self, k):
		return self.store.get(k)

	def set_value(self, k, v, expires_in_sec=None):
		self.store[k] = v

	def delete_value(self, k):
		self.store.pop(k, None)


@pytest.fixture
def lp_env(monkeypatch, tmp_path):
	# A real importable module with a deliberate hot line.
	mod_dir = tmp_path
	(mod_dir / "ugly_hot.py").write_text(
		"def bg_recheck_users(doc_name=None):\n"
		"    total = 0\n"
		"    for i in range(20000):\n"
		"        total += (i * i) % 7   # the hot line\n"
		"    return total\n"
	)
	monkeypatch.syspath_prepend(str(mod_dir))
	sys.modules.pop("ugly_hot", None)

	cache = FakeCache()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="u@x.com"), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	# _should_skip_request is only used by the request hook; not needed here.
	cap._resolved_fns_by_run.clear()
	return types.SimpleNamespace(cache=cache, user="u@x.com")


def test_bg_job_line_capture_end_to_end(lp_env):
	run_uuid = "run-abc"
	# 1. Arm: resolve + store picks + set active flag.
	resolved = cap.start_line_profile_pass(
		session_uuid="sess-1", run_uuid=run_uuid, user=lp_env.user,
		picks=[{"dotted_path": "ugly_hot.bg_recheck_users", "source": "curated"}],
	)
	assert any(r.get("eligible") for r in resolved), resolved

	# 2. Simulate the job running with phase-2 active.
	import ugly_hot
	job_kwargs = {"_lp_session_id": run_uuid, "doc_name": "X"}
	lp_hooks.before_job_line_profile(method="ugly_hot.bg_recheck_users", kwargs=job_kwargs)
	# Marker must be popped so it doesn't crash the real method call.
	assert "_lp_session_id" not in job_kwargs
	ugly_hot.bg_recheck_users(**job_kwargs)
	lp_hooks.after_job_line_profile(method="ugly_hot.bg_recheck_users", kwargs=job_kwargs, result=None)

	# 3. Aggregate — THE capture assertion: at least one line has real timing.
	samples = cap.read_all_samples(run_uuid)
	picks = cap.read_picks_meta(run_uuid)
	results = cap.aggregate_samples(samples, picks)
	assert len(results) == 1
	lines = results[0]["lines"]
	hot = [ln for ln in lines if (ln["hits"] or 0) > 0 and (ln["total_ms"] or 0) > 0]
	assert hot, (
		"Phase-2 captured NO per-line timing for the bg-job function — "
		f"samples={samples!r} results={results!r}"
	)

	# 4. Linking — the (basename, qualname) lookup the finding card uses.
	from optimus import renderer
	doc = types.SimpleNamespace(phase_2_runs=[
		types.SimpleNamespace(status="Ready", results_json=__import__("json").dumps(results)),
	])
	index = renderer._build_line_drilldown_callsite_index(doc)
	# The bg_recheck_users finding's callsite: basename common-style + bare function.
	import os
	key = (os.path.basename(results[0]["file"]), "bg_recheck_users")
	assert key in index, f"drilldown index missing {key}; keys={list(index)}"
	assert index[key].get("lineno")
