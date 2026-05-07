# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Phase-2 integration tests — exercise capture.start_line_profile_pass /
stop_line_profile_pass / is_active end-to-end with a fake frappe.cache so
we don't need a live Redis to confirm the Redis-backed lifecycle works.

These tests complement the pure-function tests in
test_line_profile_capture.py (aggregate_samples + serialize_stats) by
covering the impure orchestration layer.
"""

import json
import sys
import types
from unittest.mock import patch


class _FakeCache:
	"""Minimal stand-in for frappe.cache. Stores dict + supports get/set/
	delete + the rpush/lrange list ops used by capture.flush_samples."""

	def __init__(self):
		self.kv = {}
		self.lists = {}

	def set_value(self, key, value, expires_in_sec=None):
		self.kv[key] = value

	def get_value(self, key):
		return self.kv.get(key)

	def delete_value(self, key):
		self.kv.pop(key, None)
		self.lists.pop(key, None)

	def rpush(self, key, value):
		self.lists.setdefault(key, []).append(value)

	def lrange(self, key, start, stop):
		items = self.lists.get(key, [])
		# Negative stop=-1 means end.
		if stop == -1:
			return items[start:]
		return items[start : stop + 1]


def _install_fake_frappe():
	"""Inject a minimal frappe stub into sys.modules. Returns the fake
	cache object the tests can introspect."""
	fake_frappe = types.ModuleType("frappe")
	fake_local = types.SimpleNamespace()
	fake_cache = _FakeCache()
	fake_frappe.local = fake_local
	fake_frappe.cache = fake_cache
	fake_frappe.log_error = lambda *a, **k: None  # swallow
	sys.modules["frappe"] = fake_frappe
	return fake_cache


# Pre-populated fake function we can resolve. Lives in this test module so
# the dotted path is stable.
def sample_target():
	x = 1
	y = x + 1
	return y


class TestPhase2Lifecycle:
	def setup_method(self):
		# Fresh fake frappe per test so state doesn't leak.
		_install_fake_frappe()
		# Force-reload capture so its module-level state (the
		# _resolved_fns_by_run dict) and frappe references are clean.
		for name in list(sys.modules):
			if name.startswith("frappe_profiler.line_profile"):
				del sys.modules[name]

	def test_is_active_returns_run_uuid_after_start(self):
		from frappe_profiler.line_profile import capture

		# Skip when line_profiler isn't installed in the venv (CI without
		# the dep). The import-guard returns False; integration coverage
		# runs under bench where the dep is in pyproject.toml.
		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		picks = [{
			"dotted_path": __name__ + ".sample_target",
			"source": "freeform",
		}]

		capture.start_line_profile_pass(
			session_uuid="sess-abc",
			run_uuid="run-xyz",
			user="alice@example.com",
			picks=picks,
		)

		# is_active() reads frappe.local cache first; clear it so we hit
		# the underlying fake cache.
		import frappe
		frappe.local._lp_active = None

		assert capture.is_active("alice@example.com") == "run-xyz"

	def test_is_active_returns_none_after_stop(self):
		from frappe_profiler.line_profile import capture

		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		picks = [{"dotted_path": __name__ + ".sample_target", "source": "freeform"}]
		capture.start_line_profile_pass(
			session_uuid="s", run_uuid="r1", user="a@b.c", picks=picks,
		)
		capture.stop_line_profile_pass("r1", "a@b.c")

		import frappe
		frappe.local._lp_active = None
		assert capture.is_active("a@b.c") is None

	def test_start_persists_picks_and_source_to_redis(self):
		from frappe_profiler.line_profile import capture

		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		picks = [{"dotted_path": __name__ + ".sample_target", "source": "freeform"}]
		capture.start_line_profile_pass(
			session_uuid="s", run_uuid="r1", user="a@b.c", picks=picks,
		)

		import frappe
		picks_blob = frappe.cache.get_value("profiler:lp:r1:picks")
		source_blob = frappe.cache.get_value("profiler:lp:r1:source")
		assert picks_blob is not None
		assert source_blob is not None
		# picks JSON has the dotted path
		assert __name__ + ".sample_target" in picks_blob
		# source snapshot has the function's body lines
		assert "x = 1" in source_blob

	def test_start_rejects_when_no_picks_eligible(self):
		from frappe_profiler.line_profile import capture

		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		# `len` is a C-extension builtin — picker rejects it.
		picks = [{"dotted_path": "builtins.len", "source": "freeform"}]

		import pytest
		with pytest.raises(capture.CaptureError):
			capture.start_line_profile_pass(
				session_uuid="s", run_uuid="r1", user="a@b.c", picks=picks,
			)

	def test_cleanup_run_drops_redis_keys_and_worker_cache(self):
		from frappe_profiler.line_profile import capture

		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		picks = [{"dotted_path": __name__ + ".sample_target", "source": "freeform"}]
		capture.start_line_profile_pass(
			session_uuid="s", run_uuid="r1", user="a@b.c", picks=picks,
		)
		capture._get_or_resolve_picks("r1")  # populate worker cache

		assert "r1" in capture._resolved_fns_by_run

		capture.cleanup_run("r1")

		import frappe
		assert frappe.cache.get_value("profiler:lp:r1:picks") is None
		assert frappe.cache.get_value("profiler:lp:r1:source") is None
		assert "r1" not in capture._resolved_fns_by_run
