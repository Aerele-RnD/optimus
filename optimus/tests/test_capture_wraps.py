# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the sidecar wrap factory in capture.py.

These tests exercise the wrap mechanics in isolation by passing in a
fake `frappe_local_proxy` object — they don't require a real Frappe
runtime. The integration with frappe.local happens in test_capture_pipeline.py.
"""

import pytest

from optimus import capture


class FakeLocal:
	"""Stand-in for frappe.local — supports getattr/setattr."""
	pass


@pytest.fixture
def fake_local():
	return FakeLocal()


def test_wrap_passthrough_when_no_active_session(fake_local):
	calls = []

	def orig(*args, **kwargs):
		calls.append(("orig", args, kwargs))
		return "result"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	result = wrapped("User", "x@y.com")

	assert result == "result"
	assert calls == [("orig", ("User", "x@y.com"), {})]
	assert not hasattr(fake_local, "optimus_sidecar")


def test_wrap_records_entry_when_active(fake_local):
	fake_local._profiler_active_session_id = "test-session"
	fake_local.optimus_sidecar = []

	def orig(doctype, name):
		return "result"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	wrapped("User", "x@y.com")

	assert len(fake_local.optimus_sidecar) == 1
	entry = fake_local.optimus_sidecar[0]
	assert entry["fn_name"] == "get_doc"
	assert entry["identifier_raw"] == ("User", "x@y.com")
	assert entry["identifier_safe"][0] == "User"
	assert len(entry["identifier_safe"][1]) == 12


def test_wrap_records_even_on_exception(fake_local):
	fake_local._profiler_active_session_id = "test-session"
	fake_local.optimus_sidecar = []

	def orig(doctype, name):
		raise ValueError("boom")

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)

	with pytest.raises(ValueError, match="boom"):
		wrapped("User", "x@y.com")

	# Entry must be recorded even though the call raised
	assert len(fake_local.optimus_sidecar) == 1


def test_wrap_reentrancy_guard(fake_local):
	"""A wrapped call from inside another wrapped call must not double-record."""
	fake_local._profiler_active_session_id = "test-session"
	fake_local.optimus_sidecar = []

	inner_calls = []

	def inner_orig(key):
		inner_calls.append(key)
		return "inner"

	wrapped_inner = capture._make_wrap(inner_orig, "cache_get", local_proxy=fake_local)

	def outer_orig(doctype, name):
		# Outer wrap calls inner wrap from inside its execution
		return wrapped_inner("user_lang:" + name)

	wrapped_outer = capture._make_wrap(outer_orig, "get_doc", local_proxy=fake_local)
	wrapped_outer("User", "x@y.com")

	# Outer recorded one entry. Inner ran (call went through) but recorded
	# no entry because the re-entrancy flag was set.
	assert len(fake_local.optimus_sidecar) == 1
	assert fake_local.optimus_sidecar[0]["fn_name"] == "get_doc"
	assert inner_calls == ["user_lang:x@y.com"]


def test_wrap_caps_sidecar_at_50000_entries(fake_local):
	fake_local._profiler_active_session_id = "test-session"
	fake_local.optimus_sidecar = [{"placeholder": True}] * 50_000

	def orig(doctype, name):
		return "result"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	wrapped("User", "x@y.com")  # this should be dropped

	assert len(fake_local.optimus_sidecar) == 50_000
	assert getattr(fake_local, "optimus_sidecar_truncated", False) is True


def test_wrap_preserves_original_via_attribute(fake_local):
	def orig(doctype, name):
		return "ok"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	assert wrapped._profiler_original is orig


def test_wrap_chains_preexisting_wrap(fake_local):
	"""If orig has _profiler_original, our wrap chains via it (no double-wrap)."""
	def true_orig(doctype, name):
		return "true"

	def existing_wrap(*args, **kwargs):
		return true_orig(*args, **kwargs)

	existing_wrap._profiler_original = true_orig

	wrapped = capture._make_wrap(existing_wrap, "get_doc", local_proxy=fake_local)
	# Our _profiler_original points to the existing wrap (not double-deep)
	assert wrapped._profiler_original is existing_wrap


def test_wrap_identify_args_failure_does_not_break_orig_call(fake_local, monkeypatch):
	"""If _identify_args raises, orig must still be called and return normally.

	Observability code must never break the host call. A bug in the
	profiler's identifier-builder is logged-and-skipped, not propagated.
	"""
	fake_local._profiler_active_session_id = "test-session"
	fake_local.optimus_sidecar = []

	calls = []

	def orig(doctype, name):
		calls.append((doctype, name))
		return "result"

	def broken_identify(fn_name, args, kwargs):
		raise RuntimeError("boom in identify_args")

	monkeypatch.setattr(capture, "_identify_args", broken_identify)

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	result = wrapped("User", "x@y.com")

	assert result == "result"
	assert calls == [("User", "x@y.com")]
	# No sidecar entry recorded (best-effort skipped)
	assert fake_local.optimus_sidecar == []
	# Re-entrancy flag must be cleared even on the skip path
	assert getattr(fake_local, "_profiler_in_wrap", False) is False


def test_start_pyi_session_returns_none_when_unavailable(monkeypatch, fake_local):
	monkeypatch.setattr(capture, "_PYINSTRUMENT_AVAILABLE", False)
	result = capture._start_pyi_session(local_proxy=fake_local, interval_ms=1)
	assert result is None
	assert getattr(fake_local, "optimus_pyinstrument", None) is None


def test_start_pyi_session_starts_when_available(fake_local):
	if not capture._PYINSTRUMENT_AVAILABLE:
		pytest.skip("pyinstrument not installed")
	prof = capture._start_pyi_session(local_proxy=fake_local, interval_ms=10)
	assert prof is not None
	assert fake_local.optimus_pyinstrument is prof
	# Cleanup so the test runner doesn't leak the profiler
	prof.stop()


def test_force_stop_inflight_capture_clears_state(fake_local):
	fake_local._profiler_active_session_id = "session-123"
	fake_local.optimus_sidecar = [{"x": 1}]
	fake_local.optimus_sidecar_truncated = True

	if capture._PYINSTRUMENT_AVAILABLE:
		fake_local.optimus_pyinstrument = capture._start_pyi_session(
			local_proxy=FakeLocal(), interval_ms=10
		)

	capture._force_stop_inflight_capture(local_proxy=fake_local)

	assert getattr(fake_local, "_profiler_active_session_id", None) is None
	assert getattr(fake_local, "optimus_sidecar", None) is None
	assert getattr(fake_local, "optimus_sidecar_truncated", None) is None
	assert getattr(fake_local, "optimus_pyinstrument", None) is None


def test_install_wraps_idempotent():
	"""Calling install_wraps twice does not double-wrap."""
	import frappe

	# Reset to a clean state — optimus/__init__.py runs
	# install_wraps() at app-import time, so without this reset
	# `frappe.get_doc` is already wrapped when this test starts.
	capture.uninstall_wraps()

	orig_get_doc = frappe.get_doc  # the TRUE original now
	# Install once
	capture.install_wraps()
	first_wrap = frappe.get_doc
	# Install again
	capture.install_wraps()
	second_wrap = frappe.get_doc
	# First install produced a wrap whose _profiler_original is the true original
	assert first_wrap is not orig_get_doc
	assert first_wrap._profiler_original is orig_get_doc
	# Second install was a no-op (idempotent)
	assert second_wrap is first_wrap
	# Restore for other tests
	capture.uninstall_wraps()
	assert frappe.get_doc is orig_get_doc

	# Re-install so the rest of the test session has the wraps in place
	# (matches the state set up by optimus/__init__.py).
	capture.install_wraps()


def test_uninstall_wraps_restores_originals():
	import frappe

	# Reset to clean state first (see comment in test_install_wraps_idempotent)
	capture.uninstall_wraps()

	orig_get_doc = frappe.get_doc
	capture.install_wraps()
	assert frappe.get_doc is not orig_get_doc
	capture.uninstall_wraps()
	assert frappe.get_doc is orig_get_doc

	# Re-install so the rest of the test session has the wraps in place
	capture.install_wraps()
