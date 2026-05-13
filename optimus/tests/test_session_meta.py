# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for capture_python_tree handling in session.set_session_meta.

These tests use a fake cache backend so we don't depend on Redis.
"""

import pytest

from optimus import session


class FakeCache:
	def __init__(self):
		self.store = {}

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def get_value(self, key):
		return self.store.get(key)

	def delete_value(self, key):
		self.store.pop(key, None)


@pytest.fixture
def fake_cache(monkeypatch):
	import frappe

	cache = FakeCache()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	return cache


def test_set_session_meta_persists_capture_python_tree_true(fake_cache):
	session.set_session_meta(
		"test-uuid",
		{
			"session_uuid": "test-uuid",
			"docname": "PS-001",
			"user": "admin@example.com",
			"label": "test",
			"started_at": "2026-04-13T00:00:00",
			"capture_python_tree": True,
		},
	)
	meta = session.get_session_meta("test-uuid")
	assert meta["capture_python_tree"] is True


def test_set_session_meta_persists_capture_python_tree_false(fake_cache):
	session.set_session_meta(
		"test-uuid",
		{"session_uuid": "test-uuid", "capture_python_tree": False},
	)
	meta = session.get_session_meta("test-uuid")
	assert meta["capture_python_tree"] is False


def test_set_session_meta_default_when_not_specified(fake_cache):
	session.set_session_meta("test-uuid", {"session_uuid": "test-uuid"})
	meta = session.get_session_meta("test-uuid")
	# capture_python_tree absent means consumers should default to True
	assert "capture_python_tree" not in meta
