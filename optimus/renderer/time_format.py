# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Duration + datetime formatting helpers for the renderer.

Three small functions the template's context dict exposes as callables
(``fmt_ms`` / ``fmt_dt``) plus the server-timezone label resolver:

  * :func:`_format_duration_ms` — turns milliseconds into ``"<n>ms"`` (with
    configurable decimal places) below ``threshold_ms``, or ``"<n.nn>s"``
    above it. The seconds branch wraps the result in a
    ``<span class="time-high">`` so the report's eye-catch CSS draws the
    reader to slow values. Returns ``markupsafe.Markup`` so the wrapper
    isn't escaped when rendered through Jinja.

  * :func:`_format_datetime_display` — formats a datetime per the site's
    System Settings (Date Format + Time Format), dropping microseconds.
    Falls back to a microsecond-stripped string when Frappe isn't
    importable (pure-pytest path).

  * :func:`_get_server_timezone` — best-effort server timezone label:
    System Settings → Python's datetime tzname → "UTC". Used by the
    footer to disambiguate "what does '2026-05-24 18:12:53' mean".

Frappe is lazy-imported inside each function so a pure-pytest call path
without a bench gets the fallback behaviour without an ImportError.
"""

from __future__ import annotations

import re

from markupsafe import Markup


def _format_duration_ms(ms, threshold_ms: float = 1000.0, decimals: int = 0):
	"""Render a duration as ``"<n>ms"`` (with ``decimals`` digits) — or, if it
	crosses ``threshold_ms``, as ``"<n.nn>s"`` (always 2 decimals). The
	``decimals`` arg controls only the ms branch so the existing ``%.1f`` /
	``%.2f`` callsites (sub-ms query timings) keep their resolution below the
	threshold. ``threshold_ms = 0`` disables the conversion.

	Defensive on input: ``None`` / non-numeric → ``"0ms"``; honours sign.

	v0.7.x: returns ``markupsafe.Markup`` so the seconds branch can
	emit a ``<span class="time-high">`` wrapper without being escaped
	when rendered via ``{{ fmt_ms(...) }}`` in Jinja. The wrapper draws
	the reader's eye to values slow enough to roll over into seconds —
	the timing rule itself is unchanged, just the visual emphasis is
	new. ``Markup`` subclasses ``str`` so Python callers that compare /
	concat the return value still work.
	"""
	try:
		v = float(ms) if ms is not None else 0.0
	except (TypeError, ValueError):
		return Markup("0ms")
	if threshold_ms and abs(v) >= threshold_ms:
		return Markup(f'<span class="time-high">{v / 1000:.2f}s</span>')
	return Markup(f"{v:.{decimals}f}ms")


def _format_datetime_display(value) -> str:
	"""Format a datetime (or datetime-string) for display in the report using
	the site's System Settings (Date Format + Time Format) — which also drops
	the microseconds. Falls back to the value with any trailing microseconds
	stripped when Frappe isn't available (standalone / tests)."""
	if not value:
		return ""
	try:
		from frappe.utils import format_datetime

		return format_datetime(value)
	except Exception:
		return re.sub(r"\.\d+", "", str(value))


def _get_server_timezone() -> str:
	"""Return a human-readable server timezone label.

	Tries frappe's system settings first (more accurate than Python's
	local tz guess). Falls back to the Python datetime tzname.
	"""
	try:
		import frappe

		tz = frappe.db.get_single_value("System Settings", "time_zone")
		if tz:
			return str(tz)
	except Exception:
		pass
	try:
		from datetime import datetime

		name = datetime.now().astimezone().tzname()
		if name:
			return name
	except Exception:
		pass
	return "UTC"
