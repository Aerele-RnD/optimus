# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for v0.9.0 AI-privacy exclusion at the API boundary.

The unit suite (``optimus/tests/test_ai_privacy.py``) covers the
parser, the ``is_finding_type_excluded()`` helper, the inner
``ai_fix.suggest_fix(finding)`` refusal, and a
``requests.post``-never-called spy. It cannot prove:

  * That the **whitelisted endpoint** ``api.suggest_fix`` honours the
    exclusion before any AI dispatch — there's a separate refusal
    site at ``api.py:1333-1347`` (lifted from ``ai_fix.suggest_fix``
    so the endpoint can shape a user-readable ``frappe.throw`` that
    points at the exact setting).
  * That the live ``Optimus Settings`` cache invalidation (the doc's
    ``on_update`` deletes ``redis_keys.settings_cache()``) propagates
    so the endpoint sees the operator's edit without a restart.
  * That the v0.8.0 telemetry event ``ai.fix_call_refused_by_exclusion``
    actually lands in ``tabOptimus Telemetry Event`` after a refusal,
    with the right severity + context payload.
  * That the exclusion is case-sensitive at the API boundary (a
    lowercase entry doesn't accidentally block a capitalised real type).

That gap is what this integration test fills. The 5 test methods
together cover the refusal-with-message contract, the
telemetry-event round-trip, the case-sensitivity guarantee, the
empty-list bypass, and the cache-invalidation propagation when the
operator saves a new exclusion value.

Each test uses a synthetic ``Optimus Session`` with one
``Optimus Finding`` child row at index 0 (per-test unique
``session_uuid`` for isolation). The session is built directly via
``frappe.get_doc(...).insert(ignore_permissions=True)`` — we
deliberately avoid the full ``api.start → analyze`` pipeline because
the refusal gate only needs ``status="Ready"`` and
``findings[0].finding_type IN exclusion-list``; standing up a real
workload would be extra surface area for no test value.
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from optimus import ai_fix, api, redis_keys, telemetry

_SETTINGS_DOCTYPE = "Optimus Settings"
_SESSION_DOCTYPE = "Optimus Session"
_FINDING_DOCTYPE = "Optimus Finding"
_EVENT_DOCTYPE = "Optimus Telemetry Event"
_REFUSAL_EVENT_NAME = "ai.fix_call_refused_by_exclusion"

# A type that's in the v0.9.0 AI_ELIGIBLE_FINDING_TYPES set — the
# endpoint refuses ineligible types before the exclusion gate fires,
# so we MUST use a real eligible type for the gate-under-test path.
_ELIGIBLE_TYPE = "Slow Query"

# Stub payload returned from ``ai_fix.suggest_fix`` when the
# non-refusal tests need to traverse past the gate. The endpoint
# wraps this into ``{"ok": True, "finding": ..., "cached": False,
# **stub}``.
_STUB_SUGGESTION = {
	"suggestion": "stub suggestion (integration-test only)",
	"model": "stub-model",
	"provider": "stub",
	"generated_at": "2026-05-25T00:00:00+00:00",
	"source_available": False,
}


class TestAiPrivacyExclusionOnApi(FrappeTestCase):
	"""End-to-end: api.suggest_fix honours ai_excluded_finding_types."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Admin so the doc-save + DocType permission checks pass.
		frappe.set_user("Administrator")
		# Patch ``ai_fix.is_available`` for the lifetime of the test
		# class. Without this, ``api.suggest_fix`` short-circuits with
		# "AI fix suggestions aren't configured" before reaching the
		# exclusion gate we're testing. Production is unaffected — the
		# patch lives only for this TestCase.
		cls._is_available_patcher = mock.patch.object(ai_fix, "is_available", return_value=True)
		cls._is_available_patcher.start()

	@classmethod
	def tearDownClass(cls):
		try:
			cls._is_available_patcher.stop()
		except Exception:
			pass
		super().tearDownClass()

	# --- setUp / tearDown ---------------------------------------------

	def setUp(self):
		super().setUp()
		self._uuid = f"test-{frappe.generate_hash(length=12)}"
		# In-process emit-buffer hygiene.
		telemetry.drain_for_test()
		# DocType-sink hygiene — wipe any prior refusal rows so test 2
		# can assert exactly-one. Safe because test_site is a test bench
		# and this event_name only exists in test runs (v0.9.0 default
		# exclusion list is empty).
		self._wipe_refusal_events()

		self._original = self._snapshot_settings()
		# Force the env we need: telemetry ON + DocType sink ON; AI gate
		# ON + per-section toggle ON; exclusion = "Slow Query" (the
		# canonical v0.9.0 example). Per-test cases override exclusion
		# in their body.
		self._set_settings(
			{
				"telemetry_enabled": 1,
				"telemetry_sink_doctype": 1,
				"ai_enabled": 1,
				"ai_suggest_findings": 1,
				"ai_excluded_finding_types": _ELIGIBLE_TYPE,
			}
		)
		self._session_doc = self._create_minimal_session(session_uuid=self._uuid, finding_type=_ELIGIBLE_TYPE)

	def tearDown(self):
		try:
			frappe.delete_doc(
				_SESSION_DOCTYPE,
				self._session_doc.name,
				force=1,
				ignore_permissions=True,
			)
			frappe.db.commit()
		except Exception:
			pass
		try:
			self._set_settings(self._original)
		except Exception:
			pass
		self._wipe_refusal_events()
		telemetry.drain_for_test()
		super().tearDown()

	# --- Settings helpers ---------------------------------------------

	def _snapshot_settings(self) -> dict:
		return {
			k: frappe.db.get_single_value(_SETTINGS_DOCTYPE, k)
			for k in (
				"telemetry_enabled",
				"telemetry_sink_doctype",
				"ai_enabled",
				"ai_suggest_findings",
				"ai_excluded_finding_types",
			)
		}

	def _set_settings(self, fields: dict) -> None:
		"""Mutate the Single doc and save. The DocType's ``on_update``
		hook deletes ``redis_keys.settings_cache()`` so the next
		``get_config()`` call re-reads the live row."""
		doc = frappe.get_single(_SETTINGS_DOCTYPE)
		for k, v in fields.items():
			doc.set(k, v)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		# Belt-and-suspenders: same pattern as test_telemetry_flush_doctype_sink.py.
		try:
			frappe.cache.delete_value(redis_keys.settings_cache())
		except Exception:
			pass

	# --- Session-doc helper -------------------------------------------

	def _create_minimal_session(self, *, session_uuid: str, finding_type: str):
		"""Insert a real ``Optimus Session`` doc with the minimum reqd
		fields + ONE ``Optimus Finding`` child at index 0.

		``Optimus Session`` requires: session_uuid, title, user,
		status, started_at. Defaults for everything else."""
		doc = frappe.get_doc(
			{
				"doctype": _SESSION_DOCTYPE,
				"session_uuid": session_uuid,
				"title": f"integration test {session_uuid}",
				"user": "Administrator",
				"status": "Ready",
				"started_at": now_datetime(),
				"stopped_at": now_datetime(),
				"findings": [
					{
						"doctype": _FINDING_DOCTYPE,
						"finding_type": finding_type,
						"severity": "High",
						"title": f"test finding ({finding_type})",
						# Explicitly empty so the cache-bypass at
						# api.py:1350 doesn't short-circuit the AI
						# dispatch path in the non-refusal tests.
						"llm_fix_json": "",
					}
				],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	# --- Telemetry helpers --------------------------------------------

	def _wipe_refusal_events(self) -> None:
		try:
			frappe.db.delete(_EVENT_DOCTYPE, {"event_name": _REFUSAL_EVENT_NAME})
			frappe.db.commit()
		except Exception:
			pass

	def _refusal_rows(self) -> list[dict]:
		return frappe.get_all(
			_EVENT_DOCTYPE,
			filters={"event_name": _REFUSAL_EVENT_NAME},
			fields=["name", "event_name", "severity", "count", "last_context"],
			order_by="last_seen desc",
		)

	# --- The 5 tests --------------------------------------------------

	def test_excluded_finding_type_throws_with_clear_error_message(self):
		"""api.suggest_fix(finding_ref='0') on a 'Slow Query' finding
		while 'Slow Query' is in the exclusion list → frappe.throw with
		an operator-friendly message that points at the exact setting."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			api.suggest_fix(self._uuid, "0")
		msg = str(ctx.exception)
		# The verbatim message at api.py:1343-1346.
		assert "exclusion list" in msg, f"error message should reference 'exclusion list'; got: {msg!r}"
		assert "Optimus Settings" in msg, f"error message should point at Optimus Settings; got: {msg!r}"

	def test_excluded_finding_type_emits_telemetry_refusal_event(self):
		"""Same setup as the message test → after the refusal raises,
		telemetry.flush() lands a row in Optimus Telemetry Event with
		event_name=ai.fix_call_refused_by_exclusion, severity=warning,
		context contains the refused finding_type."""
		with self.assertRaises(frappe.ValidationError):
			api.suggest_fix(self._uuid, "0")

		wrote = telemetry.flush()
		assert wrote >= 1, f"expected ≥ 1 flushed group, got {wrote}"

		rows = self._refusal_rows()
		assert len(rows) == 1, f"expected exactly 1 refusal row, got {len(rows)}: {rows!r}"
		row = rows[0]
		assert row["severity"] == "warning", f"expected severity=warning, got {row['severity']!r}"
		assert row["count"] >= 1
		ctx = row["last_context"] or ""
		assert _ELIGIBLE_TYPE in ctx, f"expected last_context to mention {_ELIGIBLE_TYPE!r}; got: {ctx!r}"

	def test_exclusion_is_case_sensitive_at_api_boundary(self):
		"""Set exclusion = 'slow query' (lowercase). Finding type is
		'Slow Query' (capitalised). The gate must NOT fire — the
		endpoint should call ai_fix.suggest_fix (stubbed) and return
		its payload. Also asserts zero refusal telemetry rows."""
		# Override the setUp's exclusion list to the lowercase form.
		self._set_settings({"ai_excluded_finding_types": "slow query"})

		with mock.patch.object(ai_fix, "suggest_fix", return_value=dict(_STUB_SUGGESTION)) as stub:
			result = api.suggest_fix(self._uuid, "0")

		assert stub.call_count == 1, (
			f"ai_fix.suggest_fix should have been called exactly once "
			f"(gate must NOT fire on case mismatch); call_count={stub.call_count}"
		)
		assert result.get("ok") is True, f"expected ok=True; got: {result!r}"
		assert result.get("suggestion") == _STUB_SUGGESTION["suggestion"]

		# Sanity: no refusal event was emitted.
		telemetry.flush()
		assert self._refusal_rows() == [], "no refusal event should have been emitted on a case mismatch"

	def test_empty_exclusion_list_does_not_refuse(self):
		"""Set exclusion = '' (empty Small Text). Endpoint should
		proceed past the gate and call ai_fix.suggest_fix (stubbed).
		Also asserts zero refusal telemetry rows."""
		self._set_settings({"ai_excluded_finding_types": ""})

		with mock.patch.object(ai_fix, "suggest_fix", return_value=dict(_STUB_SUGGESTION)) as stub:
			result = api.suggest_fix(self._uuid, "0")

		assert stub.call_count == 1
		assert result.get("ok") is True
		assert result.get("suggestion") == _STUB_SUGGESTION["suggestion"]

		telemetry.flush()
		assert self._refusal_rows() == [], (
			"no refusal event should have been emitted on an empty exclusion list"
		)

	def test_settings_save_invalidates_cache_so_api_picks_up_new_exclusion(self):
		"""Start with exclusion = '' (empty). First call → succeeds
		(stubbed). Save the Settings doc with exclusion=Slow Query →
		on_update deletes the cached settings key. Second call →
		raises the exclusion error. Confirms the cache-invalidation
		contract across the integration boundary."""
		# Step 1: empty exclusion list → call proceeds via stub.
		self._set_settings({"ai_excluded_finding_types": ""})
		with mock.patch.object(ai_fix, "suggest_fix", return_value=dict(_STUB_SUGGESTION)) as stub:
			result = api.suggest_fix(self._uuid, "0")
		assert stub.call_count == 1
		assert result.get("ok") is True

		# Step 2: operator saves the doc with the type now excluded.
		# The DocType's on_update hook deletes the cached config key.
		self._set_settings({"ai_excluded_finding_types": _ELIGIBLE_TYPE})

		# Step 3: same endpoint call now refuses without invoking AI.
		with mock.patch.object(ai_fix, "suggest_fix") as never_call:
			with self.assertRaises(frappe.ValidationError) as ctx:
				api.suggest_fix(self._uuid, "0")
			assert never_call.call_count == 0, (
				f"ai_fix.suggest_fix should NOT have been called after the "
				f"exclusion list was updated; call_count={never_call.call_count}"
			)
		msg = str(ctx.exception)
		assert "exclusion list" in msg, f"error message should reference 'exclusion list'; got: {msg!r}"
