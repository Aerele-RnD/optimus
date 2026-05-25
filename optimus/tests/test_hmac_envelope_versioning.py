# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.14: ``sign_blob`` / ``unsign_blob`` now embed a 1-byte scheme
version (``_HMAC_SCHEME_V1``) between the HMAC signature and the
payload. The signature covers the version-tagged body so tampering
with the marker is detected; legacy pre-v0.12.14 blobs (no marker)
still verify on read via the backward-compat branch.

The redis_schema.wrap_value envelope (v0.12.0) lives one layer up at
the cache-value boundary. This module is the equivalent migration-
safety net at the bytes-on-the-wire boundary — the layer where
``unsign_blob`` produces the payload that gets pickle.loads'd or
JSON.loads'd downstream.

Each test verifies one piece of the contract:

  1. New-shape round-trip works (sign → unsign returns original).
  2. Legacy-shape (pre-v0.12.14) round-trip works (a hand-crafted
     v0 blob still verifies cleanly).
  3. New-shape blob has the expected byte layout
     (``[32-byte HMAC] + \\x01 + payload``).
  4. Tampered signature → None.
  5. Tampered version byte → None (HMAC covers it).
  6. Tampered payload → None.
  7. Truncated (< 32 bytes) → None.
"""

from __future__ import annotations

import hashlib
import hmac

from optimus import session


# Force the HMAC fallback path (per-process random secret) so each
# test runs deterministically without needing a real frappe.conf.
# session._hmac_secret() initialises the global _FALLBACK_SECRET on
# first call when frappe.conf isn't available.
def _ensure_test_secret() -> bytes:
	"""Trigger _hmac_secret to seed _FALLBACK_SECRET; return the
	process-local secret bytes for hand-crafting legacy v0 blobs."""
	# Trigger the fallback initialisation.
	_ = session._hmac_secret()
	# Pull the actual secret back out so we can mint v0 blobs.
	return session._FALLBACK_SECRET


# Each test runs in this module's pure-pytest context where frappe.conf
# is unconfigured, so `_has_stable_hmac_secret()` returns False — which
# would cause sign_blob to skip signing entirely. Force the stable-secret
# path by monkey-patching the helper.
def _force_stable_secret(monkeypatch) -> bytes:
	"""Monkey-patch _has_stable_hmac_secret to True so sign/unsign run
	their HMAC path. Returns the process-local secret bytes."""
	monkeypatch.setattr(session, "_has_stable_hmac_secret", lambda: True)
	return _ensure_test_secret()


class TestSignUnsignRoundTrip:
	def test_v1_round_trip_returns_original_payload(self, monkeypatch):
		"""New writer + new reader: sign then unsign yields back the
		original bytes verbatim."""
		_force_stable_secret(monkeypatch)
		payload = b"hello world"
		signed = session.sign_blob(payload)
		assert session.unsign_blob(signed) == payload

	def test_v1_round_trip_empty_payload(self, monkeypatch):
		"""Edge case: zero-length payload still round-trips. The body
		after signature is just the 1-byte version marker; ``body[1:]``
		yields the empty payload."""
		_force_stable_secret(monkeypatch)
		signed = session.sign_blob(b"")
		assert session.unsign_blob(signed) == b""

	def test_v1_round_trip_pickle_shaped_payload(self, monkeypatch):
		"""A realistic payload — pickle bytes starting with \\x80
		(protocol 2-5 PROTO opcode). Confirms the marker doesn't
		collide with the most common payload shape."""
		import pickle

		_force_stable_secret(monkeypatch)
		payload = pickle.dumps({"recording": "uuid", "calls": []})
		assert payload[:1] == b"\x80"  # pickle protocol 2+
		signed = session.sign_blob(payload)
		assert session.unsign_blob(signed) == payload


class TestLegacyShapeBackwardCompat:
	"""Pre-v0.12.14 blobs were ``[32-byte HMAC of payload] + payload``
	with NO version marker between. New ``unsign_blob`` must still
	verify those blobs and return the original payload."""

	def test_legacy_v0_blob_verifies_and_returns_payload(self, monkeypatch):
		"""Hand-craft a v0 blob (no version marker) and verify the
		new reader handles it."""
		secret = _force_stable_secret(monkeypatch)
		payload = b"legacy payload from a pre-v0.12.14 writer"
		legacy_sig = hmac.new(secret, payload, hashlib.sha256).digest()
		legacy_signed = legacy_sig + payload

		out = session.unsign_blob(legacy_signed)
		assert out == payload, f"v0 legacy blob should round-trip unchanged; got: {out!r}"

	def test_legacy_v0_blob_starting_with_pickle_opcode_works(self, monkeypatch):
		"""The most common legacy payload: pickle bytes. First byte is
		\\x80, which is NOT the v1 marker (\\x01), so the new reader
		correctly identifies it as a v0 blob and returns the payload
		verbatim (no stripped first byte)."""
		import pickle

		secret = _force_stable_secret(monkeypatch)
		payload = pickle.dumps([1, 2, 3])
		assert payload[:1] == b"\x80"
		legacy_sig = hmac.new(secret, payload, hashlib.sha256).digest()
		legacy_signed = legacy_sig + payload

		out = session.unsign_blob(legacy_signed)
		assert out == payload, f"pickle bytes should round-trip unchanged through the v0 branch; got: {out!r}"


class TestNewShapeByteLayout:
	"""The v1 signed blob has shape ``[32-byte HMAC] + \\x01 + payload``.
	Locks the byte layout so a future refactor that accidentally
	changes the marker or its position fails loudly."""

	def test_signed_blob_inserts_version_marker_between_hmac_and_payload(self, monkeypatch):
		_force_stable_secret(monkeypatch)
		payload = b"abc"
		signed = session.sign_blob(payload)
		assert len(signed) == session._SIG_LEN + 1 + len(payload), (
			f"signed blob length should be 32 + 1 + len(payload) = "
			f"{session._SIG_LEN + 1 + len(payload)}; got: {len(signed)}"
		)
		# Byte 32 (immediately after signature) is the version marker.
		assert signed[session._SIG_LEN] == session._HMAC_SCHEME_V1, (
			f"byte at offset {session._SIG_LEN} should be the v1 marker "
			f"({session._HMAC_SCHEME_V1!r}); got: {signed[session._SIG_LEN]!r}"
		)
		# Bytes after the marker are the original payload.
		assert signed[session._SIG_LEN + 1 :] == payload


class TestTamperingDetection:
	def test_tampered_signature_returns_none(self, monkeypatch):
		_force_stable_secret(monkeypatch)
		signed = bytearray(session.sign_blob(b"abc"))
		# Flip a bit in the signature.
		signed[0] ^= 0xFF
		assert session.unsign_blob(bytes(signed)) is None

	def test_tampered_version_byte_returns_none(self, monkeypatch):
		"""HMAC covers the version byte → tampering with it must fail
		verification (returns None), NOT silently degrade to legacy
		interpretation."""
		_force_stable_secret(monkeypatch)
		signed = bytearray(session.sign_blob(b"abc"))
		# Flip the version byte (offset = _SIG_LEN).
		signed[session._SIG_LEN] = 0xFF
		assert session.unsign_blob(bytes(signed)) is None, (
			"tampering with the version byte must invalidate the HMAC; "
			"otherwise an attacker could rewrite the marker and the reader "
			"would silently mis-interpret the payload"
		)

	def test_tampered_payload_returns_none(self, monkeypatch):
		_force_stable_secret(monkeypatch)
		signed = bytearray(session.sign_blob(b"abcdef"))
		# Flip a bit in the payload (last byte).
		signed[-1] ^= 0xFF
		assert session.unsign_blob(bytes(signed)) is None


class TestEdgeCases:
	def test_too_short_to_have_signature_returns_none(self, monkeypatch):
		_force_stable_secret(monkeypatch)
		# < 32 bytes: can't even have a signature.
		assert session.unsign_blob(b"x" * 10) is None

	def test_non_bytes_input_returns_none(self, monkeypatch):
		_force_stable_secret(monkeypatch)
		assert session.unsign_blob("not bytes") is None
		assert session.unsign_blob(None) is None

	def test_sign_blob_rejects_non_bytes(self, monkeypatch):
		_force_stable_secret(monkeypatch)
		import pytest

		with pytest.raises(TypeError):
			session.sign_blob("not bytes")

	def test_no_secret_path_passes_through_unsigned(self, monkeypatch):
		"""When _has_stable_hmac_secret returns False (no encryption_key
		in frappe.conf), sign_blob returns the raw blob unchanged —
		preserves the existing Phase K behaviour."""
		monkeypatch.setattr(session, "_has_stable_hmac_secret", lambda: False)
		payload = b"raw unsigned"
		assert session.sign_blob(payload) == payload
