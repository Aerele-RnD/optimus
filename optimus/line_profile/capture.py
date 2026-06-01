# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 line-profiler capture core.

Two layers, both in this module:

1. **Pure** — ``aggregate_samples(samples, picks)`` merges per-request
   line_profiler stats into the analyzer's input shape. Tested in
   isolation.

2. **Impure** — ``start_line_profile_pass`` / ``stop_line_profile_pass``
   own the Redis-backed run lifecycle; ``is_active`` is the hot-path
   predicate for hooks; ``make_profiler``, ``serialize_stats`` and
   ``flush_samples`` form the per-request enable/disable cycle;
   ``_get_or_resolve_picks`` is the worker-resident cache of resolved
   function objects so we don't pay ``importlib`` overhead every request.

The ``frappe`` and ``line_profiler`` imports are both guarded so the
module loads cleanly under standalone pytest (where the pure layer can be
exercised) even when neither is installed. Calling an impure function
without its dependency raises ``RuntimeError``.
"""

import importlib
import inspect
import json
import sys
import threading

from optimus import redis_keys as _redis_keys
from optimus.line_profile import diff

# ---------------------------------------------------------------------------
# Optional dependencies — guarded so the pure layer loads everywhere
# ---------------------------------------------------------------------------

try:
	import frappe  # type: ignore[import-not-found]
	_FRAPPE_AVAILABLE = True
except ImportError:
	frappe = None  # type: ignore[assignment]
	_FRAPPE_AVAILABLE = False

try:
	from line_profiler import LineProfiler  # type: ignore[import-not-found]
	_LP_AVAILABLE = True
except ImportError:
	LineProfiler = None  # type: ignore[assignment,misc]
	_LP_AVAILABLE = False


def is_line_profiler_available() -> bool:
	"""Form UI calls this to decide whether to enable the Run button."""
	return _LP_AVAILABLE


def _require_frappe() -> None:
	if not _FRAPPE_AVAILABLE:
		raise RuntimeError(
			"frappe must be importable for this operation — run under bench."
		)


def _require_line_profiler() -> None:
	if not _LP_AVAILABLE:
		raise RuntimeError(
			"line_profiler is not installed — run "
			"`bench pip install line_profiler` to enable phase 2."
		)


# ---------------------------------------------------------------------------
# Redis key shapes (mirroring the phase-1 conventions in session.py)
# ---------------------------------------------------------------------------

# v0.12.20: the per-user active flag + per-run picks/source/samples/
# budget_hit keys are now built via ``optimus.redis_keys`` (the v0.12.0
# centralized source-of-truth). The local ``_active_key`` /
# ``_picks_key`` / ``_source_key`` / ``_samples_key`` /
# ``_budget_hit_key`` helpers below have been retired — call sites use
# ``_redis_keys.lp_active(user)`` etc. directly. Key strings are
# byte-identical to the pre-v0.12.20 local helpers, so on-disk Redis
# values from older bench versions resolve unchanged.

SESSION_TTL_SECONDS = 10 * 60  # match phase-1's session TTL


# ---------------------------------------------------------------------------
# Worker-resident caches
# ---------------------------------------------------------------------------

# After the first request in a worker resolves picks for a run, subsequent
# requests reuse the resolved function objects. Cleared by stop_line_profile_pass.
_resolved_fns_by_run: dict[str, list] = {}


# ---------------------------------------------------------------------------
# Pick resolution helper (lighter than picker.resolve_freeform — just
# returns the function object, used by the worker cache)
# ---------------------------------------------------------------------------


def _resolve_attr(dotted_path: str):
	"""Resolve a dotted path to its underlying function object.

	Mirrors ``picker.resolve_freeform`` but returns just the callable. None
	on any resolution failure — caller decides the surfacing.
	"""
	parts = dotted_path.split(".")
	module = None
	module_parts = 0
	for i in range(len(parts), 0, -1):
		try:
			module = importlib.import_module(".".join(parts[:i]))
			module_parts = i
			break
		except (ImportError, TypeError, ValueError):
			# Mirror picker._resolve_freeform_exact: a malformed prefix —
			# a relative "...pkg" name (TypeError) or an empty name
			# (ValueError) — is just "not importable". Honour this
			# function's documented "None on any resolution failure"
			# contract instead of letting it escape as a 500.
			continue
	if module is None:
		return None
	obj = module
	for attr in parts[module_parts:]:
		try:
			obj = getattr(obj, attr)
		except AttributeError:
			return None
	return obj


def _capture_source_lines(fn) -> list[dict]:
	"""Snapshot the function's source via ``inspect.getsourcelines`` at
	start time so the analyzer can render lines even if the file is edited
	between start and stop. Returns ``[{lineno, content}]``."""
	try:
		source_lines, first_lineno = inspect.getsourcelines(fn)
	except (OSError, TypeError):
		return []
	return [
		{"lineno": first_lineno + i, "content": line.rstrip("\n")}
		for i, line in enumerate(source_lines)
	]


def aggregate_samples(samples: list[list[dict]], picks: list[dict]) -> list[dict]:
	"""Merge per-request line_profiler samples into the analyzer's input shape.

	Inputs:
	  samples — list of per-request batches. Each batch is a list of line
	            records: ``{file, qualname, lineno, hits, total_us}``.
	            One batch per HTTP request or background job that ran with
	            phase-2 instrumentation active.
	  picks   — one entry per picked function with the source-line data
	            captured at start time:
	            ``{dotted_path, qualname, file, first_lineno, source_lines: [{lineno, content}]}``.

	Output: the analyzer's ``results_json`` shape (one entry per pick) with
	per-line ``hits``, ``total_ms``, ``per_hit_us``, and ``content_hash``
	merged in.

	Samples that don't match any pick (stale code, renamed function, hot-
	reload weirdness) are silently dropped. Lines in the sample that no
	longer exist in the picked function's source are likewise dropped —
	the source-of-truth is the source captured at start time.
	"""
	# Build a lookup: (file, qualname, lineno) → cumulative {hits, total_us}
	totals: dict[tuple[str, str, int], dict] = {}
	for batch in samples:
		for record in batch:
			key = (record["file"], record["qualname"], int(record["lineno"]))
			entry = totals.get(key)
			if entry is None:
				totals[key] = {
					"hits": int(record.get("hits") or 0),
					"total_us": int(record.get("total_us") or 0),
				}
			else:
				entry["hits"] += int(record.get("hits") or 0)
				entry["total_us"] += int(record.get("total_us") or 0)

	results = []
	for pick in picks:
		file_path = pick["file"]
		qualname = pick["qualname"]
		lines_out = []
		for src in pick.get("source_lines", []):
			lineno = src["lineno"]
			content = src["content"]
			merged = totals.get((file_path, qualname, lineno))
			hits = merged["hits"] if merged else 0
			total_us = merged["total_us"] if merged else 0
			total_ms = total_us / 1000.0
			per_hit_us = round(total_us / hits, 2) if hits else 0.0
			lines_out.append({
				"lineno": lineno,
				"content": content,
				"content_hash": diff.content_hash(content),
				"hits": hits,
				"total_ms": round(total_ms, 4),
				"per_hit_us": per_hit_us,
			})
		results.append({
			"dotted_path": pick["dotted_path"],
			"qualname": qualname,
			"file": file_path,
			"lines": lines_out,
		})
	return results


# ---------------------------------------------------------------------------
# Lifecycle (impure — frappe + Redis required)
# ---------------------------------------------------------------------------


class CaptureError(Exception):
	"""Raised when start/stop validation fails for reasons the API surface
	should communicate to the customer (e.g. all picks ineligible)."""


def start_line_profile_pass(
	session_uuid: str,
	run_uuid: str,
	user: str,
	picks: list[dict],
) -> list[dict]:
	"""Begin a phase-2 run. Resolves picks, captures source snapshots, persists
	to Redis, and sets the per-user active flag.

	Returns the resolved-picks-meta list (with eligibility) so the API can
	echo it back to the client. Raises ``CaptureError`` if no picks are
	eligible.
	"""
	_require_frappe()
	_require_line_profiler()

	from optimus.line_profile import picker

	resolved: list[dict] = []
	for entry in picks:
		dotted = entry.get("dotted_path") or ""
		try:
			meta = picker.resolve_freeform(dotted)
		except picker.PickerError as exc:
			resolved.append({
				"dotted_path": dotted,
				"source": entry.get("source", "freeform"),
				"eligible": False,
				"ineligible_reason": str(exc),
			})
			continue
		meta["source"] = entry.get("source", "freeform")
		resolved.append(meta)

	eligible = [r for r in resolved if r.get("eligible")]
	if not eligible:
		raise CaptureError(
			"No eligible picks. Resolve errors: "
			+ "; ".join(r.get("ineligible_reason") or r.get("dotted_path") for r in resolved)
		)

	# Snapshot source for each eligible pick. Stored as a dict keyed by
	# dotted_path so aggregate_samples can pull lines per pick.
	source_snapshot: dict[str, list[dict]] = {}
	picks_meta: list[dict] = []
	for r in eligible:
		fn = _resolve_attr(r["dotted_path"])
		source_snapshot[r["dotted_path"]] = _capture_source_lines(fn) if fn else []
		picks_meta.append({
			"dotted_path": r["dotted_path"],
			"qualname": r["qualname"],
			"file": r["file"],
			"first_lineno": r["lineno"],
			"source": r["source"],
		})

	# Persist to Redis. The picks + source keys persist for the run's full
	# lifetime so any worker can resolve them; samples list grows during the
	# run; the active flag scopes the user.
	frappe.cache.set_value(_redis_keys.lp_picks(run_uuid), json.dumps(picks_meta))
	frappe.cache.set_value(_redis_keys.lp_source(run_uuid), json.dumps(source_snapshot))
	frappe.cache.set_value(_redis_keys.lp_active(user), run_uuid, expires_in_sec=SESSION_TTL_SECONDS)

	return resolved


def stop_line_profile_pass(run_uuid: str, user: str) -> None:
	"""Clear the active flag so phase-2 hooks stop instrumenting. The Redis
	picks/source/samples keys persist until ``cleanup_run`` runs at the end
	of the analyze pipeline so the analyzer can read them.

	Also clears ``frappe.local._lp_active`` so the same web request that
	called stop doesn't see a stale cached flag from earlier in the
	request — the enqueue patch in __init__.py reads this to decide
	whether to propagate ``_lp_session_id`` into job kwargs.
	"""
	_require_frappe()
	frappe.cache.delete_value(_redis_keys.lp_active(user))
	# Force-invalidate the per-request is_active cache. Without this,
	# any code path later in this request (e.g. the enqueue patch) sees
	# the stale truthy value and treats phase 2 as still active, leaking
	# _lp_session_id into the analyze job's kwargs.
	try:
		frappe.local._lp_active = None
	except Exception:
		pass


def is_active(user: str) -> str | None:
	"""Return the active phase-2 run_uuid for the user, or None.

	Hot-path predicate from the phase-2 request hook — must be cheap. The
	value is cached on ``frappe.local._lp_active`` for the request lifetime
	to avoid repeated Redis hits inside one request.
	"""
	_require_frappe()
	if not user or user == "Guest":
		return None
	cached = getattr(frappe.local, "_lp_active", None)
	if cached is not None:
		return cached if cached != "" else None
	value = frappe.cache.get_value(_redis_keys.lp_active(user))
	if isinstance(value, bytes):
		value = value.decode()
	frappe.local._lp_active = value or ""  # cache empty string for misses
	return value or None


# ---------------------------------------------------------------------------
# Per-request enable/disable cycle
# ---------------------------------------------------------------------------


def _get_or_resolve_picks(run_uuid: str) -> list:
	"""Return the worker-resident list of resolved function objects for a
	run, populating the cache from Redis on first access in this worker."""
	_require_frappe()
	cached = _resolved_fns_by_run.get(run_uuid)
	if cached is not None:
		return cached

	raw = frappe.cache.get_value(_redis_keys.lp_picks(run_uuid))
	if not raw:
		_resolved_fns_by_run[run_uuid] = []
		return []
	if isinstance(raw, bytes):
		raw = raw.decode()
	pick_metas = json.loads(raw)

	fns = []
	for meta in pick_metas:
		fn = _resolve_attr(meta["dotted_path"])
		if fn is not None:
			fns.append(fn)
	_resolved_fns_by_run[run_uuid] = fns
	return fns


def release_monitoring_tool() -> None:
	"""Guarantee phase-2 leaves no ``sys.monitoring`` line-trace hook behind.

	On Python 3.12+ line_profiler drives the *process-global* ``sys.monitoring``
	``PROFILER_ID`` (tool id 2). If a per-request teardown fails (e.g.
	line_profiler's own ``disable()`` raising ``ValueError: tool 2 is not in
	use``), tool 2's line events stay registered and EVERY subsequent request in
	the worker is line-traced → CPU saturation and a frozen UI. This forcibly
	clears + frees tool 2 so the hook can't leak, regardless of line_profiler's
	(fragile) internal bookkeeping.

	Idempotent and version-safe: a no-op on Python < 3.12 (no ``sys.monitoring``)
	and when tool 2 isn't ours. Only reclaims the tool when it's registered to
	``line_profiler``, so it never stomps a different profiler tool."""
	mon = getattr(sys, "monitoring", None)
	if mon is None:
		return
	try:
		pid = mon.PROFILER_ID
		if mon.get_tool(pid) != "line_profiler":
			return
		mon.set_events(pid, 0)
		mon.free_tool_id(pid)
	except Exception:
		pass


def disengage_monitoring() -> None:
	"""Stop line-trace overhead *without* unseating line_profiler — zero tool 2's
	events but leave the tool registered.

	This is the watchdog's disengage (vs ``release_monitoring_tool``'s full free).
	The distinction is load-bearing: ``free_tool_id`` from the watchdog's *timer
	thread*, while the request thread's profiler is still active, yanks tool 2 out
	from under line_profiler's shared manager. Its own ``disable_by_count`` then
	raises ``ValueError: tool 2 is not in use`` and leaves a half-torn-down
	``LineProfiler`` whose weakref finalizer later fires ``handle_raise_event``
	with the interpreter's ``sys`` torn down → ``'NoneType' object has no
	attribute 'monitoring'``, which PEP 669 can surface into a live request and
	break the user's submit. Zeroing events stops the overhead (observe, don't
	spoil the flow) while keeping the manager consistent, so the request thread's
	``disable_by_count`` still does the real, clean teardown.

	Idempotent + version-safe: no-op on Python < 3.12 and when tool 2 isn't ours."""
	mon = getattr(sys, "monitoring", None)
	if mon is None:
		return
	try:
		pid = mon.PROFILER_ID
		if mon.get_tool(pid) != "line_profiler":
			return
		mon.set_events(pid, 0)
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Overhead budget — observe without spoiling the flow
# ---------------------------------------------------------------------------
# line_profiler does deterministic per-line tracing, so instrumenting a hot
# loop multiplies its runtime and would freeze the user's request. A watchdog
# timer disengages tracing after a wall-clock budget so a profiled request can
# never take more than ~budget longer than its natural time; the partial line
# data still pinpoints the hot line. See feedback_observe_dont_spoil_flow.


def mark_budget_hit(run_uuid: str) -> None:
	"""Record that this run's profiling was cut short by the overhead budget,
	so analyze can flag the line data as partial. Best-effort."""
	if not _FRAPPE_AVAILABLE or not run_uuid:
		return
	try:
		frappe.cache.set_value(_redis_keys.lp_budget_hit(run_uuid), "1", expires_in_sec=3600)
	except Exception:
		pass


def budget_was_hit(run_uuid: str) -> bool:
	if not _FRAPPE_AVAILABLE or not run_uuid:
		return False
	try:
		return bool(frappe.cache.get_value(_redis_keys.lp_budget_hit(run_uuid)))
	except Exception:
		return False


def clear_budget_hit(run_uuid: str) -> None:
	if not _FRAPPE_AVAILABLE or not run_uuid:
		return
	try:
		frappe.cache.delete_value(_redis_keys.lp_budget_hit(run_uuid))
	except Exception:
		pass


def _disengage_run(run_uuid: str) -> None:
	"""Watchdog callback: stop line tracing so the request finishes at its
	natural speed, and flag the run as budget-truncated. Runs on a timer thread,
	so it uses ``disengage_monitoring`` (zero events) — NOT ``release_monitoring_tool``
	(free the tool): freeing tool 2 out from under the request thread's still-active
	profiler desyncs line_profiler's manager and orphans it (see
	``disengage_monitoring``). The request thread's own ``disable_by_count`` does
	the real teardown afterward."""
	disengage_monitoring()
	mark_budget_hit(run_uuid)


def start_overhead_watchdog(run_uuid: str, budget_seconds):
	"""Arm a one-shot timer that disengages line tracing after ``budget_seconds``
	of wall time. Returns the started ``threading.Timer`` (cancel it in the
	after_* hook when the request finishes within budget), or None when the
	budget is disabled (``<= 0``)."""
	try:
		budget = float(budget_seconds or 0)
	except (TypeError, ValueError):
		budget = 0.0
	if budget <= 0:
		return None
	timer = threading.Timer(budget, _disengage_run, args=(run_uuid,))
	timer.daemon = True
	timer.start()
	return timer


def make_profiler(run_uuid: str):
	"""Build a fresh ``LineProfiler`` with the run's picks attached. Returns
	None if line_profiler is unavailable, the run has no resolvable picks,
	or any other defensive failure — phase 2 then becomes a no-op for this
	request rather than breaking the host flow."""
	if not _LP_AVAILABLE:
		return None
	try:
		fns = _get_or_resolve_picks(run_uuid)
	except Exception:
		return None
	if not fns:
		return None
	profiler = LineProfiler()
	for fn in fns:
		try:
			profiler.add_function(fn)
		except Exception:
			# A single bad pick shouldn't sink the whole request.
			continue
	return profiler


def serialize_stats(profiler) -> list[dict]:
	"""Extract per-line records from a ``LineProfiler`` instance.

	Output shape matches the ``samples`` batch element expected by
	``aggregate_samples``: one dict per timed line with file/qualname/lineno
	and (hits, total_us). Returns ``[]`` for None or empty profilers.
	"""
	if profiler is None:
		return []
	stats = profiler.get_stats()
	# line_profiler ≥4.x: stats.timings is a dict keyed by
	# (filename, start_lineno, function_name) → list[(lineno, hits, time)]
	# where ``time`` is in microseconds when stats.unit == 1e-6 (default).
	unit = getattr(stats, "unit", 1e-6) or 1e-6
	# Convert whatever unit `time` is in to microseconds.
	us_factor = unit / 1e-6  # 1.0 when unit is microseconds
	samples: list[dict] = []
	for (filename, _start_lineno, qualname), entries in (stats.timings or {}).items():
		for entry in entries:
			# line_profiler tuples vary across versions; first three fields
			# are always (lineno, hits, time).
			lineno, hits, time_value = entry[0], entry[1], entry[2]
			samples.append({
				"file": filename,
				"qualname": qualname,
				"lineno": int(lineno),
				"hits": int(hits),
				"total_us": int(round(float(time_value) * us_factor)),
			})
	return samples


def flush_samples(run_uuid: str, samples: list[dict]) -> None:
	"""RPUSH a per-request batch into the run's samples list. No-op for
	empty samples to keep the list tight."""
	if not samples:
		return
	_require_frappe()
	# frappe.cache delegates to the underlying Redis client for list ops.
	# We use rpush via the redis client when available.
	try:
		frappe.cache.rpush(_redis_keys.lp_samples(run_uuid), json.dumps(samples))
	except AttributeError:
		# Fallback: keep batches as a JSON-list-of-batches under one key.
		# Less efficient but works without rpush.
		key = _redis_keys.lp_samples(run_uuid)
		raw = frappe.cache.get_value(key) or "[]"
		if isinstance(raw, bytes):
			raw = raw.decode()
		batches = json.loads(raw)
		batches.append(samples)
		frappe.cache.set_value(key, json.dumps(batches))


# ---------------------------------------------------------------------------
# Read-side helpers (analyze.run_analyze + janitor)
# ---------------------------------------------------------------------------


def read_all_samples(run_uuid: str) -> list[list[dict]]:
	"""Drain the per-run samples list. Each element is one per-request batch."""
	_require_frappe()
	try:
		raw_list = frappe.cache.lrange(_redis_keys.lp_samples(run_uuid), 0, -1) or []
		batches = []
		for raw in raw_list:
			if isinstance(raw, bytes):
				raw = raw.decode()
			batches.append(json.loads(raw))
		return batches
	except AttributeError:
		# Fallback to the JSON-list-of-batches stored by flush_samples.
		raw = frappe.cache.get_value(_redis_keys.lp_samples(run_uuid)) or "[]"
		if isinstance(raw, bytes):
			raw = raw.decode()
		return json.loads(raw)


def read_picks_meta(run_uuid: str) -> list[dict]:
	"""Return the picks list with source_lines populated from the snapshot.
	Shape matches what ``aggregate_samples`` expects."""
	_require_frappe()
	picks_raw = frappe.cache.get_value(_redis_keys.lp_picks(run_uuid)) or "[]"
	if isinstance(picks_raw, bytes):
		picks_raw = picks_raw.decode()
	picks_meta = json.loads(picks_raw)

	source_raw = frappe.cache.get_value(_redis_keys.lp_source(run_uuid)) or "{}"
	if isinstance(source_raw, bytes):
		source_raw = source_raw.decode()
	source_snapshot = json.loads(source_raw)

	return [
		{**p, "source_lines": source_snapshot.get(p["dotted_path"], [])}
		for p in picks_meta
	]


def cleanup_run(run_uuid: str) -> None:
	"""DEL all Redis keys for a run + drop the worker-resident pick cache.
	Called at the end of analyze.run_analyze (success or failure) and from
	the janitor for stale runs."""
	_require_frappe()
	for key_fn in (
		_redis_keys.lp_picks,
		_redis_keys.lp_source,
		_redis_keys.lp_samples,
		_redis_keys.lp_budget_hit,
	):
		try:
			frappe.cache.delete_value(key_fn(run_uuid))
		except Exception:
			# Best-effort — janitor will retry. Don't break analyze on
			# Redis hiccups.
			pass
	_resolved_fns_by_run.pop(run_uuid, None)
