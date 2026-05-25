# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.18: ``janitor._sweep_schema_drift`` runs daily; compares the
persisted ``optimus:schema_version`` sentinel to the current
``SCHEMA_VERSION`` constant; emits a telemetry warning on real drift
(sentinel != current AND sentinel was present).

The sweep is the **visibility complement** to the per-value
``unwrap_value``'s reactive ``redis.schema_drift`` events. Both can
fire on the same operator action (e.g. downgrading the package), but
the sweep's single per-day emit gives operators ONE high-confidence
notification rather than potentially-thousands of per-read events.

Each test verifies one branch of the sweep's decision matrix:

  1. **Sentinel == current** → no-op, no telemetry, no sentinel write.
  2. **Sentinel is None (fresh install / pre-v0.12.0 bench)** →
     write sentinel, NO telemetry (this is the normal startup path).
  3. **Sentinel < current** (post-upgrade) → write sentinel +
     emit telemetry with both versions in context.
  4. **Sentinel > current** (post-downgrade) → write sentinel +
     emit telemetry with both versions in context.
  5. **Sentinel-read failure** → no-op (swallows; outer sweep_old_
     sessions has its own try/except).
"""

from __future__ import annotations

from unittest import mock

import optimus.janitor as janitor


class TestSweepSchemaDriftDecisionMatrix:
	def test_no_op_when_sentinel_matches_current(self):
		"""Happy path: sentinel == current. Neither write_schema_sentinel
		nor emit_failure should fire."""
		from optimus.redis_schema import SCHEMA_VERSION

		with (
			mock.patch(
				"optimus.redis_schema.read_schema_sentinel",
				return_value=SCHEMA_VERSION,
			),
			mock.patch("optimus.redis_schema.write_schema_sentinel") as write,
			mock.patch("optimus.telemetry.emit_failure") as emit,
		):
			janitor._sweep_schema_drift()

		write.assert_not_called()
		emit.assert_not_called()

	def test_fresh_install_writes_sentinel_no_telemetry(self):
		"""Sentinel is None (fresh install or pre-v0.12.0 bench). The
		sweep should write the sentinel but NOT emit telemetry —
		this is the normal startup path, not interesting enough to
		alert on."""
		with (
			mock.patch(
				"optimus.redis_schema.read_schema_sentinel",
				return_value=None,
			),
			mock.patch("optimus.redis_schema.write_schema_sentinel") as write,
			mock.patch("optimus.telemetry.emit_failure") as emit,
		):
			janitor._sweep_schema_drift()

		write.assert_called_once()
		(
			emit.assert_not_called(),
			(
				"missing-sentinel case is the fresh-install path; telemetry "
				"should NOT fire to avoid noise on every fresh deploy"
			),
		)

	def test_post_upgrade_drift_emits_telemetry(self):
		"""Sentinel = N, current = N+1. The sweep writes the new
		sentinel + emits ONE telemetry event with both versions in
		the context."""
		from optimus.redis_schema import SCHEMA_VERSION

		# Simulate the sentinel one version BEHIND.
		old_version = SCHEMA_VERSION - 1
		with (
			mock.patch(
				"optimus.redis_schema.read_schema_sentinel",
				return_value=old_version,
			),
			mock.patch("optimus.redis_schema.write_schema_sentinel") as write,
			mock.patch("optimus.telemetry.emit_failure") as emit,
		):
			janitor._sweep_schema_drift()

		write.assert_called_once()
		emit.assert_called_once()
		args, kwargs = emit.call_args
		assert args[0] == "janitor.schema_sentinel_drift"
		ctx = kwargs.get("context") or {}
		assert ctx.get("persisted_version") == str(old_version)
		assert ctx.get("current_version") == str(SCHEMA_VERSION)
		assert kwargs.get("severity") == "warning"

	def test_post_downgrade_drift_emits_telemetry(self):
		"""Sentinel = N+1, current = N (operator downgraded the
		package). The sweep handles it symmetrically — writes the
		current sentinel + emits drift telemetry."""
		from optimus.redis_schema import SCHEMA_VERSION

		future_version = SCHEMA_VERSION + 1
		with (
			mock.patch(
				"optimus.redis_schema.read_schema_sentinel",
				return_value=future_version,
			),
			mock.patch("optimus.redis_schema.write_schema_sentinel") as write,
			mock.patch("optimus.telemetry.emit_failure") as emit,
		):
			janitor._sweep_schema_drift()

		write.assert_called_once()
		emit.assert_called_once()
		args, kwargs = emit.call_args
		assert args[0] == "janitor.schema_sentinel_drift"
		ctx = kwargs.get("context") or {}
		assert ctx.get("persisted_version") == str(future_version)
		assert ctx.get("current_version") == str(SCHEMA_VERSION)

	def test_sentinel_write_failure_swallowed(self):
		"""If write_schema_sentinel raises, the sweep must NOT
		propagate the error (the outer sweep_old_sessions has its own
		try/except, but the inner sweep should fail soft too)."""
		from optimus.redis_schema import SCHEMA_VERSION

		with (
			mock.patch(
				"optimus.redis_schema.read_schema_sentinel",
				return_value=SCHEMA_VERSION - 1,
			),
			mock.patch(
				"optimus.redis_schema.write_schema_sentinel",
				side_effect=RuntimeError("Redis hiccup"),
			),
			mock.patch("optimus.telemetry.emit_failure") as emit,
		):
			# Must NOT raise.
			janitor._sweep_schema_drift()

		# Telemetry should still fire (the write-failure doesn't gate
		# the drift signal — operators need to see the drift even if
		# the sentinel write failed).
		emit.assert_called_once()


class TestSweepWiringInDailyCron:
	"""The sweep must be invoked from sweep_old_sessions (the daily
	cron entry point). Source-grep canary so a future refactor that
	moves the wiring is forced to update the canary too."""

	def test_sweep_old_sessions_calls_sweep_schema_drift(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		start = src.index("def sweep_old_sessions(")
		# Walk forward to the next top-level def.
		import re

		next_top = re.search(r"\n(?:def )", src[start + 1 :])
		end = start + 1 + (next_top.start() if next_top else len(src) - start - 1)
		body = src[start:end]
		assert "_sweep_schema_drift()" in body, (
			"sweep_old_sessions must invoke _sweep_schema_drift so the "
			"daily cron actually runs the drift check"
		)
		# The call must be wrapped in try/except (every sweep in this
		# module follows that pattern; the test pins it).
		assert "janitor.sweep_schema_drift" in body, (
			"the wrapped try/except for _sweep_schema_drift must emit "
			"telemetry on its own failure (matching the sibling sweep "
			"pattern)"
		)
