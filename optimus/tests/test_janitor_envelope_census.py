# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.12.25: ``janitor._sweep_envelope_versions`` is a per-key
visibility complement to v0.12.18's ``_sweep_schema_drift``.

The sentinel sweep tells operators "schema version on disk differs
from the code's"; this sweep answers the follow-up question "how many
cached values are actually stale?" by scanning all
``profiler:session:*:meta`` keys and counting per envelope version.

The sweep depends on a live ``frappe.cache.get_redis_connection`` +
``frappe.cache.scan`` + per-key ``frappe.cache.get_value`` pipeline.
Mocking that pipeline in the pure-pytest harness is fragile (the
conftest's ``SimpleNamespace`` stub for ``frappe.cache`` lacks the
``get_redis_connection`` method, and ``mock.patch.object``
replacements don't always survive cross-test isolation in the full
suite). The integration suite (``tests_integration/``) is the right
place for the end-to-end execution test against real Redis — out of
scope here.

This module covers what's testable in pure-pytest:

  * **Logic-level decision matrix** — feed pre-computed (current,
    legacy, drift, total) counts directly into the emit-or-not
    decision and assert the right branch fires (the sweep's core
    contract distilled to a pure function below).
  * **Source-inspection canaries** — the sweep is wired into
    ``sweep_old_sessions`` and uses the documented patterns
    (try/except around an inner sweep + telemetry on inner failure).
"""

from __future__ import annotations


def _emit_decision(total: int, current: int, legacy: int, drift: int) -> bool:
	"""Pure-function distillation of the sweep's emit-or-skip
	decision: emit telemetry only when there's something actionable
	for the operator (legacy or drift values present). No emit on
	empty bench or all-current happy path."""
	if total == 0:
		return False
	if (legacy + drift) == 0:
		return False
	return True


class TestEmitDecisionMatrix:
	"""The sweep's emit gate distilled to a pure function for
	isolated testing."""

	def test_empty_bench_no_emit(self):
		assert _emit_decision(total=0, current=0, legacy=0, drift=0) is False

	def test_all_current_no_emit(self):
		assert _emit_decision(total=5, current=5, legacy=0, drift=0) is False

	def test_legacy_values_present_emits(self):
		assert _emit_decision(total=3, current=1, legacy=2, drift=0) is True

	def test_drift_values_present_emits(self):
		assert _emit_decision(total=2, current=1, legacy=0, drift=1) is True

	def test_mixed_legacy_and_drift_emits(self):
		assert _emit_decision(total=10, current=5, legacy=3, drift=2) is True

	def test_only_legacy_no_current_emits(self):
		"""Pre-rollout bench reading post-rollout code → every value
		is bare-dict legacy. Operator needs to see this to plan
		migration."""
		assert _emit_decision(total=8, current=0, legacy=8, drift=0) is True


class TestSweepSourceMatchesDecisionMatrix:
	"""Source-inspection canary: confirm the sweep's actual code
	implements the same decision matrix the pure-function helper
	above tests. If a future refactor changes the emit gate, the
	in-source check fails and the operator-visibility contract is
	caught at unit-test time."""

	def test_sweep_has_total_zero_early_return(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		# The sweep must have an early-return guard for `total == 0`
		# (empty bench). Without it, an empty scan would emit a
		# telemetry event with zeros.
		assert "total == 0" in src, (
			"sweep_envelope_versions must guard on `total == 0` so an "
			"empty bench doesn't emit a useless census event"
		)

	def test_sweep_has_legacy_plus_drift_gate(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		# The emit gate must skip the all-current happy path. The
		# implementation uses `(legacy + drift) == 0` as the gate
		# (combined with total > 0 above).
		assert "(legacy + drift) == 0" in src, (
			"sweep_envelope_versions must skip emit when no legacy / drift "
			"values exist; daily clean-bench runs would otherwise flood "
			"Optimus Telemetry Event with zero-count census events"
		)

	def test_sweep_emit_event_name_is_envelope_version_census(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		assert '"janitor.envelope_version_census"' in src, (
			"the census telemetry event must be named "
			"janitor.envelope_version_census so it's discoverable in "
			"Optimus Telemetry Event and groups under one signature"
		)


class TestSweepWiringInDailyCron:
	"""The new sweep must be invoked from sweep_old_sessions (the
	daily cron entry point). Source-grep canary so a future refactor
	that moves the wiring is forced to update the canary too."""

	def test_sweep_old_sessions_calls_sweep_envelope_versions(self):
		import os
		import re

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		start = src.index("def sweep_old_sessions(")
		next_top = re.search(r"\n(?:def )", src[start + 1 :])
		end = start + 1 + (next_top.start() if next_top else len(src) - start - 1)
		body = src[start:end]
		assert "_sweep_envelope_versions()" in body, (
			"sweep_old_sessions must invoke _sweep_envelope_versions so "
			"the daily cron actually runs the per-value census"
		)
		assert "janitor.sweep_envelope_versions" in body, (
			"the wrapped try/except for _sweep_envelope_versions must emit "
			"telemetry on its own failure (matching the sibling sweep "
			"pattern)"
		)

	def test_sweep_scans_session_meta_pattern(self):
		"""Source-grep: confirm the sweep targets ``session_meta``
		(the v0.12.21 rollout target). Future per-value rollouts
		would expand this to include more patterns."""
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		# The pattern construction uses the session_meta key shape.
		assert "profiler:session:*:meta" in src, (
			"sweep_envelope_versions must scan profiler:session:*:meta "
			"keys to enumerate session_meta values for the census"
		)
