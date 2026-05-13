# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Performance regression gates (Spec §11.6).

Threshold constants are intentionally module-level so a future PR can
relax them with explicit signoff. Failure blocks merge.
"""

import time

import pytest

from optimus import capture

# ---- Thresholds ----------------------------------------------------------

WRAP_OVERHEAD_BASELINE_NS_PER_CALL_MAX = 1500
"""Wrap fast path: a single getattr + early return must average <1.5μs."""


def test_wrap_fast_path_microbenchmark():
	"""Microbenchmark: wrapped function with no active session must be cheap.

	This is the load-bearing performance gate for non-recording users.
	The wrap is installed on every running worker process; if its no-op
	path is slow, every site running our app pays the cost.
	"""
	def orig(doctype, name):
		return "ok"

	class FakeLocal:
		pass

	local = FakeLocal()
	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=local)

	# Warm up
	for _ in range(1000):
		wrapped("User", "x")

	# Baseline: unwrapped
	t0 = time.perf_counter_ns()
	for _ in range(100_000):
		orig("User", "x")
	baseline_ns = time.perf_counter_ns() - t0

	# Wrapped (no active session — should hit fast path)
	t0 = time.perf_counter_ns()
	for _ in range(100_000):
		wrapped("User", "x")
	wrapped_ns = time.perf_counter_ns() - t0

	per_call_overhead_ns = (wrapped_ns - baseline_ns) / 100_000
	overhead_pct = (wrapped_ns - baseline_ns) / baseline_ns

	# Print so failures show useful context in CI logs
	print(
		f"\nbaseline: {baseline_ns / 100_000:.0f}ns/call   "
		f"wrapped: {wrapped_ns / 100_000:.0f}ns/call   "
		f"overhead: {per_call_overhead_ns:.0f}ns/call ({overhead_pct * 100:.1f}%)"
	)

	assert per_call_overhead_ns < WRAP_OVERHEAD_BASELINE_NS_PER_CALL_MAX, (
		f"Wrap overhead {per_call_overhead_ns:.0f}ns/call exceeds "
		f"{WRAP_OVERHEAD_BASELINE_NS_PER_CALL_MAX}ns budget"
	)


@pytest.mark.skipif(
	not capture._PYINSTRUMENT_AVAILABLE,
	reason="pyinstrument not available",
)
def test_pyinstrument_start_stop_cycle_under_10ms():
	"""Starting and stopping pyinstrument should be well under 10ms."""
	class FakeLocal:
		pass

	local = FakeLocal()

	t0 = time.perf_counter()
	for _ in range(10):
		capture._start_pyi_session(local_proxy=local, interval_ms=1)
		local.optimus_pyinstrument.stop()
		delattr(local, "optimus_pyinstrument")
	elapsed_ms = (time.perf_counter() - t0) * 1000 / 10

	print(f"\npyinstrument start+stop: {elapsed_ms:.2f}ms/cycle")
	assert elapsed_ms < 10
