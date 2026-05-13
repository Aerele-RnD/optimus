# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.4.0 download_pdf API endpoint."""

import pytest

from optimus import api


class FakeDB:
	def __init__(self, rows):
		self.rows = rows

	def get_value(self, doctype, filters, fields=None, as_dict=False):
		if isinstance(filters, dict):
			uuid = filters.get("session_uuid")
			row = self.rows.get(uuid)
			if not row:
				return None
			if as_dict:
				return {f: row.get(f) for f in fields}
			return row.get(fields) if isinstance(fields, str) else tuple(row.get(f) for f in fields)
		return None


@pytest.fixture
def fake_env(monkeypatch):
	import frappe

	rows = {
		"uuid-001": {"name": "PS-001", "user": "Administrator", "status": "Ready"},
		"uuid-failed": {"name": "PS-failed", "user": "Administrator", "status": "Failed"},
	}

	monkeypatch.setattr(frappe, "db", FakeDB(rows), raising=False)
	monkeypatch.setattr(
		frappe, "session",
		type("S", (), {"user": "Administrator"})(),
		raising=False,
	)
	monkeypatch.setattr(frappe, "get_roles", lambda u: ["System Manager"], raising=False)

	# Stub pdf_export to not actually generate
	def fake_get_or_generate_pdf(session_uuid):
		return f"/private/files/optimus_safe_report_{session_uuid}.pdf"

	import optimus.pdf_export
	monkeypatch.setattr(
		optimus.pdf_export,
		"get_or_generate_pdf",
		fake_get_or_generate_pdf,
		raising=False,
	)


def test_download_pdf_returns_file_url(fake_env):
	result = api.download_pdf(session_uuid="uuid-001")
	assert "file_url" in result
	assert result["file_url"].endswith("uuid-001.pdf")


def test_download_pdf_requires_ready_status(fake_env):
	with pytest.raises(Exception):
		api.download_pdf(session_uuid="uuid-failed")


def test_download_pdf_unknown_session_raises(fake_env):
	with pytest.raises(Exception):
		api.download_pdf(session_uuid="nonexistent")
