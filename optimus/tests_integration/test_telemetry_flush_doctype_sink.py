# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for v0.8.0 telemetry's DocType sink.

The unit suite (``optimus/tests/test_telemetry.py``) covers the emit
hot path, bounded deque, signature dedup, path/context scrub, settings
clamps, and ``flush()`` against a **mocked** ``frappe.db.sql``. It
cannot prove:

  * That ``flush()`` actually writes a row that lands in
    ``tabOptimus Telemetry Event`` with the right columns.
  * That the DocType's deterministic ``name`` (derived from
    ``sha1(event_name + '|' + signature)[:10]``) provides the
    composite-uniqueness the ``INSERT … ON DUPLICATE KEY UPDATE``
    relies on.
  * That toggling ``telemetry_enabled`` via the Optimus Settings doc
    actually invalidates the cached config so the next ``flush()``
    sees the new value.
  * That the PII-scrubbed traceback survives the round-trip through
    MariaDB (encoding, NULL-handling, truncation).

That gap is what this integration test fills. The 5 test methods
together cover the canonical write path, the signature-dedup count
accumulation, the master-off guard, the scrub-through-persistence
contract, and the multi-flush UPSERT (``count = count + VALUES(count)``).

Each test owns a unique ``event_name`` prefix derived from
``frappe.generate_hash`` so concurrent runs / sibling tests can't
collide. ``setUp`` snapshots the operator's current
``telemetry_enabled`` setting and forces it ON for the test;
``tearDown`` restores the original value AND deletes the test's rows
via an explicit ``frappe.db.delete`` (the writes go through direct SQL
and auto-commit past the per-test ``FrappeTestCase`` transaction).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from optimus import redis_keys, telemetry

# Optimus Settings DocType name.
_SETTINGS_DOCTYPE = "Optimus Settings"
# Optimus Telemetry Event DocType name.
_EVENT_DOCTYPE = "Optimus Telemetry Event"


class TestTelemetryFlushDocTypeSink(FrappeTestCase):
	"""End-to-end: emit → flush → row visible in the DocType."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Admin user so any DocType permission gate doesn't refuse the
		# Optimus Settings doc save (System Manager only by default).
		frappe.set_user("Administrator")

	def setUp(self):
		super().setUp()
		# Per-test event_name prefix — every row this test writes has
		# its event_name start with this value, so the tearDown delete
		# can scope to just our rows.
		self._evt_prefix = f"test.{frappe.generate_hash(length=10)}"
		# Drain the in-process emit buffer so a leftover emit from an
		# earlier test in the same process can't bleed into our flush().
		telemetry.drain_for_test()
		# Snapshot the operator's current telemetry settings so tearDown
		# can restore them (we don't want to leave the bench in a
		# different state than we found it).
		self._original = {
			"telemetry_enabled": self._get_setting("telemetry_enabled"),
			"telemetry_sink_doctype": self._get_setting("telemetry_sink_doctype"),
		}
		# Force telemetry ON + DocType sink ON for this test.
		self._set_telemetry({"telemetry_enabled": 1, "telemetry_sink_doctype": 1})

	def tearDown(self):
		# Wipe every Optimus Telemetry Event row this test created.
		try:
			frappe.db.delete(_EVENT_DOCTYPE, {"event_name": ("like", f"{self._evt_prefix}%")})
			frappe.db.commit()
		except Exception:
			pass
		# Restore the original settings (whatever they were before the test).
		try:
			self._set_telemetry(self._original)
		except Exception:
			pass
		telemetry.drain_for_test()
		super().tearDown()

	# --- Settings helpers --------------------------------------------

	def _get_setting(self, field: str):
		return frappe.db.get_single_value(_SETTINGS_DOCTYPE, field)

	def _set_telemetry(self, fields: dict) -> None:
		"""Mutate the Single doc and save. The DocType's ``on_update``
		hook (in ``optimus_settings.py``) deletes the cached config key,
		which forces ``settings.get_config()`` to re-read on the next
		call — that's how ``flush()`` sees the new ``telemetry_enabled``
		value without a restart."""
		doc = frappe.get_single(_SETTINGS_DOCTYPE)
		for k, v in fields.items():
			doc.set(k, v)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		# Belt-and-suspenders: the on_update hook deletes the cache key,
		# but we explicitly clear it too in case the settings module
		# has an in-memory snapshot somewhere (it shouldn't, but the cost
		# is one extra Redis DEL).
		try:
			frappe.cache.delete_value(redis_keys.settings_cache())
		except Exception:
			pass

	# --- DocType-row helpers -----------------------------------------

	def _events_for_prefix(self) -> list[dict]:
		"""Fetch all rows whose event_name starts with this test's
		prefix. Sorted by event_name + signature for deterministic
		assertions."""
		rows = frappe.get_all(
			_EVENT_DOCTYPE,
			filters={"event_name": ("like", f"{self._evt_prefix}%")},
			fields=[
				"name",
				"event_name",
				"signature",
				"count",
				"severity",
				"first_seen",
				"last_seen",
				"last_traceback",
				"last_context",
				"optimus_version",
				"python_version",
				"frappe_version",
			],
			order_by="event_name asc, signature asc",
		)
		return rows

	# --- The 5 tests --------------------------------------------------

	def test_emit_then_flush_persists_doctype_row(self):
		"""Canonical round-trip: emit once, flush, assert one row with
		the right shape lands in Optimus Telemetry Event."""
		event = f"{self._evt_prefix}.basic"
		telemetry.emit_failure(event, exc=None, context={"role": "smoke"})

		wrote = telemetry.flush()
		assert wrote == 1, f"flush returned {wrote}, expected 1"

		rows = self._events_for_prefix()
		assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows!r}"
		row = rows[0]
		assert row["event_name"] == event
		assert row["count"] == 1
		assert row["severity"] == "error"
		assert row["first_seen"] is not None
		assert row["last_seen"] is not None
		# v0.8.0 stamps every row with optimus / python / frappe versions.
		assert row["optimus_version"], "optimus_version unstamped"
		assert row["python_version"], "python_version unstamped"
		# frappe_version may be empty on some installs where frappe.__version__
		# isn't set; tolerate but log via assertion message.
		assert row["frappe_version"] is not None, "frappe_version should at least be empty string, not NULL"

	def test_repeated_emits_dedup_to_single_row_with_count(self):
		"""5 emits with the same event_name + exc=None → one signature
		→ one DocType row with count=5. The v0.8.0 signature-dedup
		(sha1 of event_name + frames) collapses identical emits at
		flush time."""
		event = f"{self._evt_prefix}.dedup"
		for _ in range(5):
			telemetry.emit_failure(event, exc=None)

		wrote = telemetry.flush()
		assert wrote == 1, f"flush returned {wrote}, expected 1 group"

		rows = self._events_for_prefix()
		assert len(rows) == 1, f"expected 1 row (dedup'd), got {len(rows)}"
		assert rows[0]["count"] == 5, f"expected count=5, got {rows[0]['count']}"

	def test_flush_no_op_when_master_disabled(self):
		"""With ``telemetry_enabled`` OFF, ``flush()`` drains the buffer
		but writes nothing to the DocType. The master gate is checked
		AT flush time (not emit time) — emit always appends to the
		deque so a future toggle-on doesn't lose history."""
		# Toggle off (overrides the setUp ON).
		self._set_telemetry({"telemetry_enabled": 0})

		event = f"{self._evt_prefix}.gated"
		telemetry.emit_failure(event, exc=None)

		wrote = telemetry.flush()
		assert wrote == 0, f"flush returned {wrote}, expected 0 (master off)"

		rows = self._events_for_prefix()
		assert rows == [], f"expected no rows when master toggle is OFF, got {rows!r}"

	def test_persisted_row_has_scrubbed_traceback(self):
		"""Emit with a real exception whose traceback contains an
		absolute path. After flush, the persisted ``last_traceback``
		field must (a) contain the ``<bench>/apps/optimus/`` scrubbed
		marker for the optimus frame, (b) NOT contain raw ``/Users/`` /
		``/home/`` absolute prefixes."""
		event = f"{self._evt_prefix}.scrub"

		try:
			raise ValueError("intentional — testing traceback scrub")
		except ValueError as exc:
			telemetry.emit_failure(event, exc=exc, severity="error")

		telemetry.flush()
		rows = self._events_for_prefix()
		assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
		tb = rows[0]["last_traceback"] or ""
		assert tb, "last_traceback is empty — scrub or persistence dropped it"
		# The scrubbed marker should appear for the optimus frame
		# (this test file lives under optimus/tests_integration/, which
		# the scrubber treats as a non-optimus user_code path — so the
		# trace's optimus.telemetry frame is what carries the
		# <bench>/apps/optimus/ marker).
		assert "<bench>/apps/optimus/" in tb, (
			f"expected '<bench>/apps/optimus/' scrubbed marker in traceback; got: {tb!r}"
		)
		# Hard-pin: absolute install-prefix paths MUST NOT leak through.
		for raw_prefix in ("/Users/", "/home/", "/private/"):
			assert raw_prefix not in tb, (
				f"raw absolute prefix {raw_prefix!r} leaked into persisted traceback: {tb!r}"
			)

	def test_second_flush_increments_count_via_upsert(self):
		"""Two emit-flush cycles with the same signature → ONE DocType
		row, count accumulates (3 + 4 = 7). Confirms the v0.8.0
		``INSERT … ON DUPLICATE KEY UPDATE count = count + VALUES(count)``
		SQL is what executes, not a naive ``INSERT``-only path that
		would either duplicate-key-error or write two rows."""
		event = f"{self._evt_prefix}.upsert"

		# Cycle 1 — 3 emits.
		for _ in range(3):
			telemetry.emit_failure(event, exc=None)
		wrote_1 = telemetry.flush()
		assert wrote_1 == 1

		# Cycle 2 — 4 more emits, same event_name + signature.
		for _ in range(4):
			telemetry.emit_failure(event, exc=None)
		wrote_2 = telemetry.flush()
		assert wrote_2 == 1, (
			f"second flush returned {wrote_2}; expected 1 (upsert into the existing row, not a new insert)"
		)

		rows = self._events_for_prefix()
		assert len(rows) == 1, (
			f"expected 1 row after two flushes (upsert), got {len(rows)}: {[r['event_name'] for r in rows]!r}"
		)
		assert rows[0]["count"] == 7, f"expected count=7 (3 + 4), got {rows[0]['count']}"
