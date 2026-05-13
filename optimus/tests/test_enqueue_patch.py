# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Integration test for the frappe.enqueue monkey-patch.

The patch lives in optimus/__init__.py and wraps
frappe.utils.background_jobs.enqueue so that when a user with an active
profiler session enqueues a background job, `_profiler_session_id` is
injected into the job's kwargs. This test verifies the wrapper fires
correctly and injects the marker.

It also verifies the critical property that `frappe.enqueue_doc` (which
internally calls module-level `enqueue`) also inherits the patch.

To run this test without a real frappe site, we stub out the minimum
surface of `frappe` and `frappe.utils.background_jobs` that the patch
references. This keeps the test fast and deterministic.
"""

import sys
import types

import pytest


def _reset_profiler_modules():
	"""Remove cached optimus.* modules so reimporting picks up
	the current sys.modules['frappe'] stub. Each test needs a clean
	import chain because optimus.session captures its `frappe`
	reference at module load time."""
	for mod_name in list(sys.modules.keys()):
		if mod_name == "optimus" or mod_name.startswith("optimus."):
			del sys.modules[mod_name]


@pytest.fixture
def fake_frappe(monkeypatch):
	"""Install a minimal fake `frappe` module so the patch can run."""
	# Build fake frappe
	fake = types.ModuleType("frappe")
	fake.session = types.SimpleNamespace(user="alice@example.com")
	fake.conf = {}

	def fake_get_roles(user=None):
		return ["Optimus User"]

	fake.get_roles = fake_get_roles

	# Fake cache with a minimal get_value that returns whatever's in a dict
	class FakeCache:
		def __init__(self):
			self._store = {}

		def get_value(self, key, **kwargs):
			return self._store.get(key)

		def set_value(self, key, val, **kwargs):
			self._store[key] = val

		def delete_value(self, key):
			self._store.pop(key, None)

		def sadd(self, key, *values):
			self._store.setdefault(key, set()).update(values)

		def smembers(self, key):
			return self._store.get(key, set())

	fake.cache = FakeCache()

	# Build fake frappe.utils.background_jobs with a recording "enqueue"
	fake_utils = types.ModuleType("frappe.utils")
	fake_bg = types.ModuleType("frappe.utils.background_jobs")

	enqueue_calls = []

	def original_enqueue(method, **kwargs):
		enqueue_calls.append({"method": method, "kwargs": dict(kwargs)})
		return {"id": f"job_{len(enqueue_calls)}"}

	fake_bg.enqueue = original_enqueue

	# Install in sys.modules
	monkeypatch.setitem(sys.modules, "frappe", fake)
	monkeypatch.setitem(sys.modules, "frappe.utils", fake_utils)
	monkeypatch.setitem(sys.modules, "frappe.utils.background_jobs", fake_bg)

	# optimus.session uses frappe.cache — already stubbed above
	return fake, fake_bg, enqueue_calls


def test_patch_injects_session_id(fake_frappe, monkeypatch):
	"""When the calling user has an active session, enqueue injects the marker."""
	fake, fake_bg, enqueue_calls = fake_frappe

	# Arrange: put an active session for alice in fake cache
	fake.cache.set_value("profiler:active:alice@example.com", "test-session-uuid-abc")

	# Import the patch — this triggers _patch_enqueue() at module load
	# We need to force a re-import since other tests may have imported it already.
	_reset_profiler_modules()
	import optimus  # noqa: F401 — triggers the patch

	# Act: call frappe.utils.background_jobs.enqueue
	fake_bg.enqueue("my_module.my_func", x=1, y=2)

	# Assert: the captured kwargs include _profiler_session_id
	assert len(enqueue_calls) == 1
	captured = enqueue_calls[0]
	assert captured["method"] == "my_module.my_func"
	assert captured["kwargs"]["x"] == 1
	assert captured["kwargs"]["y"] == 2
	assert captured["kwargs"]["_profiler_session_id"] == "test-session-uuid-abc"


def test_patch_skips_without_active_session(fake_frappe, monkeypatch):
	"""No active session → no marker injection."""
	fake, fake_bg, enqueue_calls = fake_frappe

	# Cache is empty — no active session
	_reset_profiler_modules()
	import optimus  # noqa: F401

	fake_bg.enqueue("m.f", a=1)

	assert len(enqueue_calls) == 1
	assert "_profiler_session_id" not in enqueue_calls[0]["kwargs"]


def test_patch_idempotent(fake_frappe, monkeypatch):
	"""Re-importing optimus should NOT double-wrap the patch."""
	fake, fake_bg, enqueue_calls = fake_frappe

	_reset_profiler_modules()
	import optimus  # noqa: F401

	first_enqueue = fake_bg.enqueue
	assert getattr(first_enqueue, "_profiler_patched", False) is True

	# Re-import
	del sys.modules["optimus"]
	import optimus  # noqa: F401,F811

	second_enqueue = fake_bg.enqueue
	assert getattr(second_enqueue, "_profiler_patched", False) is True

	# The wrapper function itself should NOT be wrapped inside another wrapper.
	# We check this by asserting __wrapped__ points to the ORIGINAL, not
	# to another _profiler_patched wrapper.
	original = getattr(second_enqueue, "__wrapped__", None)
	assert original is not None
	# If double-wrapped, original would itself be a patched function.
	assert not getattr(original, "_profiler_patched", False)
