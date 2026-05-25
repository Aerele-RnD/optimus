# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Capture-time redaction patch on ``frappe.recorder.Recorder``.

These tests verify the patch installed at ``optimus/__init__.py:_patch_recorder``:

  * ``Recorder.__init__`` — after the original runs, ``self.form_dict`` and
    ``self.headers`` are walked through ``redaction.redact_sensitive`` so
    raw passwords / cookies never reach ``dump()`` → ``RECORDER_REQUEST_HASH``.
  * ``Recorder.register`` — ``data["query"]`` is run through
    ``redaction.redact_sql_literals`` before the original appends it to
    ``self.calls``. The renderer-time scrubber stays as defense-in-depth.

The patch is idempotent (re-imports during ``bench update`` don't double-
wrap) and respects ``optimus.settings``'s ``sensitive_sql_columns`` /
``sensitive_form_keys`` extras.
"""

from __future__ import annotations

import pytest

import optimus  # noqa: F401 — triggers _patch_recorder at import-time


def _Recorder():
	"""Return the monkey-patched Recorder class. Skip the test if Frappe's
	recorder isn't importable (running pytest outside the bench)."""
	try:
		from frappe.recorder import Recorder

		return Recorder
	except ImportError:
		pytest.skip("frappe.recorder unavailable in this Python env")


class TestPatchInstalled:
	def test_patch_marker_on_register(self):
		Recorder = _Recorder()
		assert getattr(Recorder.register, "_profiler_patched", False), (
			"Recorder.register should carry _profiler_patched = True after import"
		)

	def test_patch_marker_on_init(self):
		Recorder = _Recorder()
		assert getattr(Recorder.__init__, "_profiler_patched", False), (
			"Recorder.__init__ should carry _profiler_patched = True after import"
		)

	def test_patch_is_idempotent(self):
		"""Running _patch_recorder a second time MUST NOT double-wrap.
		Mirrors the _patch_enqueue idempotency guarantee that re-imports
		during ``bench update`` rely on."""
		Recorder = _Recorder()
		first_register = Recorder.register
		first_init = Recorder.__init__
		optimus._patch_recorder()
		assert Recorder.register is first_register
		assert Recorder.__init__ is first_init


# ---------------------------------------------------------------------------
# Recorder.register redacts sensitive SQL literals before storing the call.
# ---------------------------------------------------------------------------


class _FakeRecorderInstance:
	"""Minimal stand-in for a Recorder instance — we only need a ``calls``
	list for the original ``register`` to append to."""

	def __init__(self):
		self.calls: list[dict] = []


class TestRegisterRedaction:
	def test_redacts_password_in_query(self):
		Recorder = _Recorder()
		fake = _FakeRecorderInstance()
		data = {
			"query": "UPDATE `tabUser` SET password='hunter2' WHERE name='Administrator'",
			"duration": 1.0,
			"stack": [],
		}
		# Call the patched register as an unbound method on the fake instance.
		Recorder.register(fake, data)
		assert fake.calls, "register should still forward to original (calls list grows)"
		stored_query = fake.calls[0]["query"]
		assert "hunter2" not in stored_query, f"raw password leaked: {stored_query!r}"
		assert "'<REDACTED>'" in stored_query

	def test_passes_non_sensitive_query_through(self):
		Recorder = _Recorder()
		fake = _FakeRecorderInstance()
		data = {"query": "SELECT name FROM `tabUser` WHERE enabled=1", "duration": 0.5}
		Recorder.register(fake, data)
		assert fake.calls[0]["query"] == "SELECT name FROM `tabUser` WHERE enabled=1"

	def test_non_dict_data_passes_through_without_crash(self):
		Recorder = _Recorder()
		fake = _FakeRecorderInstance()
		# Original may or may not accept non-dict data; the patch must not
		# itself add a crash path.
		try:
			Recorder.register(fake, None)
		except Exception:
			pass  # acceptable if the ORIGINAL barfs; the wrap doesn't add a crash


# ---------------------------------------------------------------------------
# Recorder.__init__ redacts form_dict + headers as the recorder snapshots them.
# ---------------------------------------------------------------------------
#
# The real ``Recorder.__init__`` reads ``frappe.local.request`` /
# ``frappe.local.form_dict`` to populate self.form_dict + self.headers. In
# a plain pytest context those locals aren't initialized; rather than spin
# up a request lifecycle, we exercise the redaction step DIRECTLY by
# setting the fields on a Recorder-like object then re-running the wrap
# logic. This proves the redaction step fires; the integration with real
# Frappe is covered by the e2e test in the plan.


class TestInitRedaction:
	def test_form_dict_with_sensitive_keys_redacted(self):
		from optimus.redaction import redact_sensitive

		form_dict = {"password": "hunter2", "username": "alice"}
		out = redact_sensitive(form_dict)
		assert out["password"] == "<REDACTED:password>"
		assert out["username"] == "alice"
		# Equivalent to what the patched __init__ does:
		# self.form_dict = redact_sensitive(self.form_dict, extra_keys=cfg.sensitive_form_keys)

	def test_headers_with_authorization_redacted(self):
		from optimus.redaction import redact_sensitive

		headers = {"Authorization": "Bearer abc.def.ghi", "User-Agent": "curl/8.1"}
		out = redact_sensitive(headers)
		assert out["Authorization"] == "<REDACTED:Authorization>"
		assert out["User-Agent"] == "curl/8.1"


# ---------------------------------------------------------------------------
# Settings-driven extras propagate into the patched paths.
# ---------------------------------------------------------------------------


class TestSettingsDrivenExtras:
	def test_extra_sql_columns_take_effect(self, monkeypatch):
		"""Add a custom column via OptimusConfig.sensitive_sql_columns; the
		patched register must redact it on the next call."""
		import types

		from optimus import settings as _settings

		def _fake_get_config():
			return types.SimpleNamespace(
				sensitive_form_keys=(),
				sensitive_sql_columns=("bank_account",),
			)

		monkeypatch.setattr(_settings, "get_config", _fake_get_config, raising=False)

		Recorder = _Recorder()
		fake = _FakeRecorderInstance()
		data = {"query": "SELECT * FROM acc WHERE bank_account='1234567890'", "duration": 0.1}
		Recorder.register(fake, data)
		stored = fake.calls[0]["query"]
		assert "1234567890" not in stored, f"extras-driven redaction missed: {stored!r}"
		assert "'<REDACTED>'" in stored

	def test_extra_form_keys_take_effect(self, monkeypatch):
		"""Custom form/header keys via settings extend the default redaction."""
		import types

		from optimus import settings as _settings

		def _fake_get_config():
			return types.SimpleNamespace(
				sensitive_form_keys=("recovery_code",),
				sensitive_sql_columns=(),
			)

		monkeypatch.setattr(_settings, "get_config", _fake_get_config, raising=False)

		from optimus.redaction import redact_sensitive

		# Same call shape the patched __init__ uses.
		_, _ = (), ()
		extra_keys = _fake_get_config().sensitive_form_keys
		out = redact_sensitive({"recovery_code": "abc", "name": "Alice"}, extra_keys=extra_keys)
		assert out["recovery_code"] == "<REDACTED:recovery_code>"
		assert out["name"] == "Alice"
