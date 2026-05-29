# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.14.x: ``Aerele`` is a hosted AI provider option for customers
who don't want to bring their own Anthropic / OpenAI key. The
Optimus integration is intentionally minimal: just a row in
``_PROVIDER_DEFAULTS`` and an entry in the ``ai_provider`` Select.

All token balance bookkeeping, pre-call validation, and metering
happen server-side on Aerele's separate Frappe site (the URL in the
matrix). Optimus is a dumb client — exactly the shape of the
Anthropic / OpenAI / Kimi entries. See docs/AI-FIXING.md §10.

These tests pin the provider entry against accidental removal /
mis-shaped wire protocol.
"""

from __future__ import annotations

from optimus import ai_fix


class TestAereleProviderEntry:
	def test_aerele_uses_openai_wire_protocol(self):
		"""Aerele's Frappe site fronts an OpenAI-shaped wire so the
		existing ``_call_openai_chat`` handler routes correctly — no
		new protocol branch needed."""
		entry = ai_fix._PROVIDER_DEFAULTS["Aerele"]
		assert entry["protocol"] == "openai"
		assert entry["needs_key"] is True
		# Default base URL points at Aerele's hosted endpoint. Operators
		# can override via ``ai_base_url`` if Aerele migrates the domain.
		assert entry["base_url"].startswith("https://")
		assert "aerele" in entry["base_url"]
		# A default upstream model is set (Aerele picks; subject to
		# change). Empty string is the OpenAI-compatible posture for a
		# bring-your-own-model endpoint, which Aerele is NOT.
		assert entry["model"], "Aerele must ship with a default model"

	def test_aerele_in_provider_select_options(self):
		"""The DocType's ``ai_provider`` Select must list ``Aerele``
		alongside the other hosted options so the operator can pick
		it. Kept as a separate test from the matrix entry so a
		mis-aligned UX surface (matrix updated but Select not) is
		caught immediately."""
		import json
		import pathlib

		settings_json = pathlib.Path(__file__).parent.parent / "optimus" / "doctype" / "optimus_settings" / "optimus_settings.json"
		doc = json.loads(settings_json.read_text())
		by_name = {f["fieldname"]: f for f in doc["fields"]}
		options = by_name["ai_provider"]["options"]
		assert "Aerele" in options.split("\n")
