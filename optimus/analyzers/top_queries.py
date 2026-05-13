# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: top N slowest queries across the session + slow-query findings.

Builds the `top_queries` aggregate (used by the report renderer to draw
the slowest-queries leaderboard) and emits a `Slow Query` finding for a
slow single query in that leaderboard (> 200ms by default).

The leaderboard is scoped to the user's *own* app code: queries whose
blame callsite resolves to framework / third-party code are dropped
before truncating to top N — they're noise the developer can't act on
and they crowd real application queries out of the list. The complete,
unfiltered per-query list is still available in the per-action breakdown.
"""

import json

from optimus.analyzers.base import (
	AnalyzerResult,
	is_framework_callsite_str,
	is_profiler_own_query,
	walk_callsite_str,
)

DEFAULT_TOP_N = 20
# v0.6.0 Round 6: was a hardcoded constant, now Optimus Settings
# `slow_query_threshold_ms` (read at analyze time). The constant
# stays as the floor when settings are unavailable (unit tests
# without bench, fresh install).
SLOW_QUERY_MS_FALLBACK = 200
HIGH_SEVERITY_MULTIPLIER = 2.5  # High when query > threshold * 2.5 (matches old 200/500 ratio)
MAX_FINDINGS = 5  # only flag the top 5 slow queries to avoid noise

# Don't pad the "slowest queries" leaderboard with queries that took
# essentially no time — a panel of 1-3ms queries is noise that reads as
# "here are your problem queries" when there aren't any. A query has to
# clear this floor to qualify; if nothing does, the section renders an
# empty state instead of a list of trivially-fast queries.
TOP_QUERY_FLOOR_MS = 10.0


def _resolve_slow_query_threshold() -> tuple[float, float]:
	"""Return (slow_threshold_ms, high_severity_threshold_ms). Reads
	Optimus Settings via the cached config; falls back to the legacy
	constants when settings aren't reachable (pure-test path)."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
		slow = float(cfg.slow_query_threshold_ms or SLOW_QUERY_MS_FALLBACK)
	except Exception:
		slow = float(SLOW_QUERY_MS_FALLBACK)
	return slow, slow * HIGH_SEVERITY_MULTIPLIER


def _resolve_tracked_apps() -> tuple[str, ...]:
	"""``Optimus Settings ▸ Tracked Apps`` allowlist, or ``()`` when
	settings aren't reachable (pure-test path). Passed to
	``is_framework_callsite_str`` — an empty tuple makes that classifier
	fall back to its built-in ``FRAMEWORK_APPS`` exclusion heuristic."""
	try:
		from optimus.settings import get_tracked_apps
		return tuple(get_tracked_apps() or ())
	except Exception:
		return ()


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	slow_threshold, high_threshold = _resolve_slow_query_threshold()
	tracked_apps = _resolve_tracked_apps()
	all_queries = []
	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			# v0.5.1: drop profiler's own instrumentation queries
			# (infra_capture SHOW GLOBAL STATUS etc.) from the top-
			# queries leaderboard. Without this, a slow infra snapshot
			# could rank above real application queries in the "slowest
			# queries" panel, wasting the user's attention.
			if is_profiler_own_query(call.get("stack")):
				continue
			all_queries.append(
				{
					"normalized_query": (call.get("normalized_query") or call.get("query") or "")[:500],
					"duration_ms": round(call.get("duration", 0), 2),
					"action_idx": action_idx,
					"recording_uuid": recording.get("uuid"),
					"callsite": walk_callsite_str(call.get("stack")),
				}
			)

	all_queries.sort(key=lambda q: q["duration_ms"], reverse=True)

	# Scope the leaderboard to the user's own app code — framework /
	# third-party queries are dropped here, BEFORE truncating to top N,
	# so a session full of framework-internal queries still surfaces the
	# slowest application queries instead of a top-N of un-actionable
	# noise. (The per-action breakdown in the report keeps every query.)
	# Also drop trivially-fast queries: a "slowest queries" panel padded
	# with sub-10ms rows reads as "here are your problem queries" when
	# there aren't any — if nothing clears the floor, the panel stays
	# empty rather than listing noise.
	top = [
		q for q in all_queries
		if q["duration_ms"] >= TOP_QUERY_FLOOR_MS
		and not is_framework_callsite_str(q["callsite"], tracked_apps)
	][:DEFAULT_TOP_N]

	findings = []
	for q in top[:MAX_FINDINGS]:
		if q["duration_ms"] <= slow_threshold:
			break  # the rest are below threshold; sorted desc so we can break
		findings.append(
			{
				"finding_type": "Slow Query",
				"severity": "High" if q["duration_ms"] > high_threshold else "Medium",
				"title": f"Slow query: {q['duration_ms']:.0f}ms",
				"customer_description": (
					f"A single query took {q['duration_ms']:.0f}ms to run. "
					"This is one of the slowest queries in the session and is "
					"a likely candidate for optimization."
				),
				"technical_detail_json": json.dumps(
					{
						"normalized_query": q["normalized_query"],
						"callsite": q["callsite"],
						"recording_uuid": q["recording_uuid"],
						"fix_hint": (
							"Investigate this query — it may need an index, a "
							"refactored WHERE clause, or a different access pattern. "
							"Run EXPLAIN ANALYZE on a representative production query "
							"to see the actual cost."
						),
					},
					default=str,
				),
				"estimated_impact_ms": q["duration_ms"],
				"affected_count": 1,
				"action_ref": str(q["action_idx"]),
			}
		)

	return AnalyzerResult(findings=findings, aggregate={"top_queries": top})


# Callsite walking is shared across analyzers via base.walk_callsite_str.
