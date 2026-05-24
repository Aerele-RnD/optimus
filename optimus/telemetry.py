# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Opt-in failure telemetry — hot-path emit + scheduled flush.

Closes Critical Risk #4 of the v0.7.x architecture review: ~79
``frappe.log_error`` call sites scattered across the app, plus ~200+
silent ``try/except`` blocks, with no aggregation. An operator could
not tell "this fails 4000×/day from THIS code path" from "this
happened once and never again" — every failure landed in Frappe's
global Error Log as a one-off row.

This module ships visibility without phoning home. Default OFF. When
enabled, the default sink is a local DocType the operator inspects in
their own Desk. A JSONL log file is opt-in for log aggregators. An
HTTPS endpoint field exists in Settings but transport is deferred to
a follow-up PR (per the "Phase B" deferral in the plan).

Architecture (three invariants):

  1. **Hot-path emit is ~free** ([[feedback_observe_dont_spoil_flow]]).
     ``emit_failure`` does one ``collections.deque.append`` + a sha1
     hash. NO Redis I/O, NO DB I/O, NO settings lookup. Settings is
     read only inside :func:`flush`.

  2. **PII-safe by construction**. File paths under bench are rewritten
     to ``<bench>/apps/<app>/file.py:LINE``; frames outside
     ``optimus/`` collapse to ``<user_code>:LINE`` (no function name);
     context dicts cap at 8 keys + 200 chars/value. The caller is
     responsible for not passing user data via ``context`` — the
     in-process emit cannot inspect that contract, only enforce caps.

  3. **Additive, never replacement.** Migrated call sites KEEP their
     existing ``frappe.log_error`` and add ``emit_failure`` beside it.
     Telemetry being misconfigured cannot regress Error Log visibility.

This module imports nothing from Frappe at module top so it stays
testable on a plain Python interpreter (no bench needed).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# The buffer is process-local and bounded. ``deque.append`` is GIL-atomic so
# emit() does not acquire ``_LOCK``; flush() holds the lock only for the
# snapshot-and-clear window so emits during flush land in the next window.

_BUFFER_MAXLEN = 500
_BUFFER: deque[dict] = deque(maxlen=_BUFFER_MAXLEN)
_LOCK = threading.Lock()

_MAX_TB_BYTES = 8 * 1024
_MAX_CTX_KEYS = 8
_MAX_CTX_VALUE_CHARS = 200
_MAX_EVENT_NAME = 140
_MAX_SIGNATURE_FRAMES = 5

# Regex: an absolute POSIX path that passes through ``/apps/<name>/``. The
# leading directory chain (the bench prefix) is matched non-capturing so the
# substitution can remove it entirely. Captures: (1) the app name, (2) the
# rest of the path after ``<app>/``. Deployment-agnostic — handles every
# bench layout (/home/frappe/..., /Users/.../, /opt/bench/...).
_APPS_PATH_RE = re.compile(r"(?:/[^/\s\"]+)*/apps/([^/]+)(/[^\s\":]+)")

# Regex: a traceback ``File "...", line N, in func`` line. The ``in func``
# tail is optional because some stdlib formatters omit it for ``<module>``.
_FILE_LINE_RE = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<lineno>\d+)(?:, in (?P<func>.*))?$')

__all__ = ("emit_failure", "flush", "drain_for_test")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_failure(
	event_name: str,
	exc: BaseException | None = None,
	*,
	context: dict | None = None,
	severity: str = "error",
) -> None:
	"""Append a failure signal to the in-process buffer. Bounded; never blocks.

	Hot-path contract: this function MUST NOT read settings, MUST NOT
	touch Redis or the DB, and MUST NOT raise. A failure storm during
	a Redis hiccup would re-trigger emit storms if emit had to read
	settings → cache → Redis → fallback; settings is checked only at
	:func:`flush` time so emit stays immune.

	When the deque is full (≥ 500 unflushed signatures in a 10-minute
	window) the oldest entry is silently dropped. The deque is per-
	process and bounded, so there is no resource leak.
	"""
	try:
		if not isinstance(event_name, str) or not event_name:
			return
		event_name = event_name[:_MAX_EVENT_NAME]
		tb_text = ""
		exc_type = ""
		if exc is not None:
			try:
				tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
				exc_type = type(exc).__name__
			except Exception:
				tb_text = ""
				exc_type = type(exc).__name__ if exc is not None else ""
		signature = _make_signature(event_name, exc)
		ctx = _scrub_context(context)
		record = {
			"event_name": event_name,
			"signature": signature,
			"severity": severity if severity in ("error", "warning") else "error",
			"exc_type": exc_type,
			"traceback": _scrub_traceback(tb_text),
			"context": ctx,
			"emitted_at": time.time(),
		}
		# deque.append is GIL-atomic; no lock needed. maxlen drops oldest.
		_BUFFER.append(record)
	except Exception:
		# Telemetry must never break the host code path. Drop silently.
		pass


def flush() -> int:
	"""Scheduler entry. Drains the buffer, groups by signature, writes to
	each enabled sink. Returns the number of distinct (event, signature)
	groups written.

	Reads settings ONCE per call (cached). When master telemetry is OFF,
	the buffer is drained anyway (so a future toggle-on doesn't dump a
	huge stale backlog) but no sinks are written.
	"""
	try:
		# Snapshot + clear under lock. Subsequent emits during this window
		# land in the next flush — acceptable for a 10-minute cadence.
		with _LOCK:
			if not _BUFFER:
				return 0
			snapshot = list(_BUFFER)
			_BUFFER.clear()
	except Exception:
		return 0

	try:
		import frappe  # noqa: E402 — lazy by design

		from optimus import settings
	except Exception:
		# No bench context — drop silently. This path is only hit by tests
		# or by a misconfigured deploy; either way, sinks aren't reachable.
		return 0

	try:
		cfg = settings.get_config()
	except Exception:
		return 0

	# Master gate. Buffer is drained above either way so a later toggle-on
	# doesn't unleash a backlog from before the operator enabled telemetry.
	if not getattr(cfg, "telemetry_enabled", False):
		return 0

	# Group by signature. Last record's traceback + context wins; counts sum.
	grouped: dict[str, dict] = {}
	for rec in snapshot:
		sig = rec["signature"]
		g = grouped.get(sig)
		if g is None:
			grouped[sig] = {
				"event_name": rec["event_name"],
				"signature": sig,
				"severity": rec["severity"],
				"exc_type": rec["exc_type"],
				"count": 1,
				"first_seen": rec["emitted_at"],
				"last_seen": rec["emitted_at"],
				"last_traceback": rec["traceback"],
				"last_context": rec["context"],
			}
		else:
			g["count"] += 1
			g["last_seen"] = rec["emitted_at"]
			# Last wins on traceback/context (newest is the most actionable).
			g["last_traceback"] = rec["traceback"]
			g["last_context"] = rec["context"]
			if rec["severity"] == "error":
				g["severity"] = "error"  # error sticky over warning

	wrote = 0
	if getattr(cfg, "telemetry_sink_doctype", False):
		try:
			_write_doctype(grouped, frappe)
			wrote = len(grouped)
		except Exception:
			# Sink failures must not propagate; we still try other sinks.
			try:
				frappe.log_error(title="optimus.telemetry.flush doctype sink failed")
			except Exception:
				pass

	if getattr(cfg, "telemetry_sink_jsonl_file", False):
		try:
			path = _jsonl_path(frappe)
			_write_jsonl(grouped, path)
		except Exception:
			try:
				frappe.log_error(title="optimus.telemetry.flush jsonl sink failed")
			except Exception:
				pass

	return wrote


def drain_for_test() -> list[dict]:
	"""Test-only helper. Atomically snapshot + clear the buffer and return
	the records. Production code MUST NOT call this — use :func:`flush`."""
	with _LOCK:
		snapshot = list(_BUFFER)
		_BUFFER.clear()
	return snapshot


# ---------------------------------------------------------------------------
# Internals — signature + scrub
# ---------------------------------------------------------------------------


def _make_signature(event_name: str, exc: BaseException | None) -> str:
	"""Return a stable 16-char hex digest identifying this failure class.

	Material: ``event_name`` + the exception class name + the last
	:data:`_MAX_SIGNATURE_FRAMES` frames of the traceback. Frames inside
	``optimus/`` contribute ``<rel_path>:LINE``; frames outside collapse
	to ``<user_code>:LINE`` so an identical user-driven failure still
	hashes the same regardless of which user app triggered it.

	For ``exc=None`` (a logical failure without an exception), the
	signature is ``sha1(event_name)`` — every call to the same event
	name hashes the same.
	"""
	h = hashlib.sha1()
	h.update(event_name.encode("utf-8"))
	if exc is None:
		return h.hexdigest()[:16]
	try:
		h.update(b"|")
		h.update(type(exc).__name__.encode("utf-8"))
		tb = traceback.extract_tb(exc.__traceback__)
		for frame in tb[-_MAX_SIGNATURE_FRAMES:]:
			path = frame.filename or ""
			lineno = frame.lineno or 0
			app_m = _APPS_PATH_RE.search(path)
			if app_m and app_m.group(1) == "optimus":
				key = f"|{app_m.group(2)}:{lineno}"
			else:
				key = f"|<user_code>:{lineno}"
			h.update(key.encode("utf-8"))
	except Exception:
		# Can't extract — fall back to event_name + exc type only.
		pass
	return h.hexdigest()[:16]


def _scrub_traceback(tb_text: str) -> str:
	"""Return ``tb_text`` with PII-sensitive material rewritten.

	* ``File "..."`` lines inside ``/apps/optimus/`` keep their relative
	  path + line + function name, prefixed ``<bench>/apps/optimus``.
	* ``File "..."`` lines in any other ``/apps/<name>/`` collapse to
	  ``<user_code>:LINE`` (no path, no function name — only the line
	  number is signal-preserving).
	* ``File "..."`` lines in stdlib / site-packages collapse to
	  ``<deps>/<basename>:LINE``.
	* Non-file lines have any embedded ``/apps/<name>/`` paths rewritten
	  via :data:`_APPS_PATH_RE` (covers the chained-exception preamble).
	* Result is capped at :data:`_MAX_TB_BYTES` with a ``... [truncated]``
	  marker.
	"""
	if not tb_text:
		return ""
	out: list[str] = []
	for raw in tb_text.splitlines():
		m = _FILE_LINE_RE.match(raw)
		if m:
			out.append(_scrub_file_line(m.group("path"), m.group("lineno"), m.group("func")))
		else:
			out.append(_APPS_PATH_RE.sub(r"<bench>/apps/\1\2", raw))
	result = "\n".join(out)
	if len(result) > _MAX_TB_BYTES:
		marker = "\n... [truncated]"
		result = result[: _MAX_TB_BYTES - len(marker)] + marker
	return result


def _scrub_file_line(path: str, lineno: str, func: str | None) -> str:
	"""Rewrite one traceback ``File "..."`` line per the rules in
	:func:`_scrub_traceback`."""
	app_m = _APPS_PATH_RE.search(path)
	if app_m:
		app, rest = app_m.group(1), app_m.group(2)
		if app == "optimus":
			tail = f", in {func}" if func else ""
			return f'  File "<bench>/apps/optimus{rest}", line {lineno}{tail}'
		# User app — collapse, no function name, no path tail.
		return f"  <user_code>:{lineno}"
	if "site-packages" in path or "/python" in path.lower() or "/lib/" in path:
		base = path.rsplit("/", 1)[-1]
		tail = f", in {func}" if func else ""
		return f'  File "<deps>/{base}", line {lineno}{tail}'
	return f"  <external>:{lineno}"


def _scrub_context(ctx: Any) -> dict:
	"""Coerce ``ctx`` to a PII-safe flat dict.

	Drops non-dict input. Caps to :data:`_MAX_CTX_KEYS` keys. Drops
	``None`` values. Coerces all remaining values to str and caps each
	at :data:`_MAX_CTX_VALUE_CHARS` chars. No recursion — context is
	intentionally flat; nested dicts/lists get ``str()``'d.
	"""
	if not isinstance(ctx, dict):
		return {}
	out: dict[str, str] = {}
	for k, v in ctx.items():
		if len(out) >= _MAX_CTX_KEYS:
			break
		if v is None:
			continue
		try:
			key = str(k)
		except Exception:
			continue
		try:
			val = str(v)
		except Exception:
			continue
		if len(val) > _MAX_CTX_VALUE_CHARS:
			val = val[:_MAX_CTX_VALUE_CHARS] + "..."
		out[key] = val
	return out


# ---------------------------------------------------------------------------
# Internals — sinks
# ---------------------------------------------------------------------------


def _versions(frappe) -> tuple[str, str, str]:
	"""Return ``(optimus_version, python_version, frappe_version)`` —
	included in every persisted row so the operator can correlate failures
	with deploys."""
	try:
		from optimus import __version__ as opt_v
	except Exception:
		opt_v = ""
	py_v = ".".join(str(c) for c in sys.version_info[:3])
	try:
		frappe_v = getattr(frappe, "__version__", "") or ""
	except Exception:
		frappe_v = ""
	return (opt_v, py_v, frappe_v)


def _write_doctype(grouped: dict[str, dict], frappe) -> None:
	"""UPSERT one row per ``(event_name, signature)`` into
	``Optimus Telemetry Event``.

	Uses ``INSERT … ON DUPLICATE KEY UPDATE`` (atomic in InnoDB) so
	multiple worker processes flushing the same signature converge
	without locking. ``count`` accumulates; ``last_traceback`` and
	``last_context`` are last-writer-wins.
	"""
	opt_v, py_v, frappe_v = _versions(frappe)
	now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	for sig, g in grouped.items():
		try:
			# Frappe stores Datetime values without tz offset; we format
			# naive system-local strings ([[feedback_mariadb_datetime_no_tz_offset]]).
			first_seen = datetime.fromtimestamp(g["first_seen"]).strftime("%Y-%m-%d %H:%M:%S")
			last_seen = datetime.fromtimestamp(g["last_seen"]).strftime("%Y-%m-%d %H:%M:%S")
			ctx_json = json.dumps(g["last_context"] or {}, ensure_ascii=False)[:2048]
			row_name = hashlib.sha1(f"{g['event_name']}|{sig}".encode()).hexdigest()[:10]
			frappe.db.sql(
				"""
				INSERT INTO `tabOptimus Telemetry Event`
					(`name`, `creation`, `modified`, `modified_by`, `owner`,
					 `event_name`, `signature`, `severity`, `count`,
					 `first_seen`, `last_seen`, `last_traceback`, `last_context`,
					 `optimus_version`, `python_version`, `frappe_version`)
				VALUES (%(name)s, %(now)s, %(now)s, 'Administrator', 'Administrator',
					%(event_name)s, %(signature)s, %(severity)s, %(count)s,
					%(first_seen)s, %(last_seen)s, %(last_traceback)s, %(last_context)s,
					%(optimus_version)s, %(python_version)s, %(frappe_version)s)
				ON DUPLICATE KEY UPDATE
					`count` = `count` + VALUES(`count`),
					`last_seen` = VALUES(`last_seen`),
					`last_traceback` = VALUES(`last_traceback`),
					`last_context` = VALUES(`last_context`),
					`severity` = VALUES(`severity`),
					`optimus_version` = VALUES(`optimus_version`),
					`python_version` = VALUES(`python_version`),
					`frappe_version` = VALUES(`frappe_version`),
					`modified` = VALUES(`modified`)
				""",
				{
					"name": row_name,
					"now": now,
					"event_name": g["event_name"],
					"signature": sig,
					"severity": g["severity"],
					"count": g["count"],
					"first_seen": first_seen,
					"last_seen": last_seen,
					"last_traceback": g["last_traceback"],
					"last_context": ctx_json,
					"optimus_version": opt_v,
					"python_version": py_v,
					"frappe_version": frappe_v,
				},
			)
		except Exception:
			# Per-row failure — try the next; the outer caller logs once.
			continue


def _jsonl_path(frappe) -> str:
	"""Resolve ``<bench>/logs/optimus_telemetry.jsonl``. The bench path
	is read from :func:`frappe.utils.get_bench_path` at flush time (no
	module-level Frappe import)."""
	try:
		bench = frappe.utils.get_bench_path()
	except Exception:
		bench = os.getcwd()
	return os.path.join(bench, "logs", "optimus_telemetry.jsonl")


def _write_jsonl(grouped: dict[str, dict], path: str) -> None:
	"""Append one JSON line per group. Best-effort: an IOError (full
	disk, permission, missing logs/ dir) is silently dropped — the
	DocType sink remains the canonical source."""
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
	except Exception:
		return
	try:
		with open(path, "a", encoding="utf-8") as fh:
			for sig, g in grouped.items():
				payload = {
					"event_name": g["event_name"],
					"signature": sig,
					"severity": g["severity"],
					"count": g["count"],
					"first_seen": datetime.fromtimestamp(g["first_seen"]).isoformat(),
					"last_seen": datetime.fromtimestamp(g["last_seen"]).isoformat(),
					"exc_type": g["exc_type"],
					"last_traceback": g["last_traceback"],
					"last_context": g["last_context"],
				}
				fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
	except Exception:
		# Sink failure must not propagate.
		pass
