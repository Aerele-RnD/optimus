# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.4.0 onboarding state API endpoints."""

import pytest

from optimus import api


class FakeCache:
	def __init__(self):
		self.store = {}

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def get_value(self, key):
		return self.store.get(key)

	def delete_value(self, key):
		self.store.pop(key, None)


class FakeDB:
	def __init__(self, count_value=0):
		self._count = count_value

	def count(self, doctype, filters=None):
		return self._count


@pytest.fixture
def fake_env(monkeypatch):
	import frappe

	cache = FakeCache()
	fake_db = FakeDB(count_value=0)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "db", fake_db, raising=False)
	monkeypatch.setattr(
		frappe, "session",
		type("S", (), {"user": "alice@example.com"})(),
		raising=False,
	)
	monkeypatch.setattr(frappe, "get_roles", lambda u: ["Optimus User"], raising=False)
	return cache, fake_db


def test_check_onboarding_seen_returns_false_initially(fake_env):
	result = api.check_onboarding_seen()
	assert result == {"seen": False}


def test_mark_onboarding_seen_persists(fake_env):
	api.mark_onboarding_seen()
	result = api.check_onboarding_seen()
	assert result == {"seen": True}


def test_check_onboarding_seen_suppressed_when_user_has_existing_sessions(fake_env):
	cache, fake_db = fake_env
	# User has a Ready Optimus Session — they're an experienced user
	fake_db._count = 3
	result = api.check_onboarding_seen()
	assert result == {"seen": True}
