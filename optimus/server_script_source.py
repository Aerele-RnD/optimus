# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Resolve Server Script source code from the Frappe database at render time.

Frappe's ``safe_exec`` (apps/frappe/frappe/utils/safe_exec.py:49,118) compiles
Server Script bodies with a synthetic filename:

    <serverscript>                    # bare (no script name passed)
    <serverscript>: <scrubbed-name>   # with ``frappe.scrub(script_filename)``

These never resolve to an on-disk file. ``renderer._resolve_source_path``
rejects them by the ``startswith("<")`` rule, so callsites in Server Script
bodies render without a code snippet and without an editor link — the user
sees ``<server-script body>`` with no actionable context.

This module is the render-time bridge that loads the Server Script's stored
``script`` field from the ``tabServer Script`` DocType so the renderer can
show inline code + link to the Desk form (``/app/server-script/<name>``).
Best-effort: every public function returns ``None`` / a safe default on any
error so a DB hiccup at render time never breaks the report.

Pipeline:
    filename (from pyinstrument)  ──extract_script_name──▶  scrubbed name
                                                                │
                                       get_server_script_record▼
                                                          {name, script}
                                                                │
                                          ┌─────────────────────┼─────────────┐
                                          ▼                     ▼             ▼
                              get_server_script_lines      desk_url(name)   display
"""

from __future__ import annotations

import re

# The exact safe-exec prefix Frappe writes into the compiled filename. Kept
# explicit so a future Frappe rename surfaces as a test failure here rather
# than silent degradation across the report.
_SERVER_SCRIPT_PREFIX = "<serverscript>"
_SERVER_SCRIPT_FILENAME_RE = re.compile(r"^<serverscript>(?:\s*:\s*(?P<name>[^<>]+?))?\s*$")


def extract_script_name(filename) -> str | None:
	"""Parse the scrubbed Server Script name out of a synthetic ``safe_exec``
	filename. Returns the scrubbed name (e.g. ``"my_script"``) or ``None`` when:

	  - ``filename`` doesn't match the ``<serverscript>[: name]`` shape;
	  - the filename is bare ``<serverscript>`` (no name attached — no script
	    to look up).

	Examples (matching ``apps/frappe/frappe/utils/safe_exec.py:118``)::

	    extract_script_name("<serverscript>: my_script")        → "my_script"
	    extract_script_name("<serverscript>")                   → None
	    extract_script_name("apps/frappe/frappe/handler.py")    → None
	    extract_script_name(None)                                → None
	"""
	if not filename or not isinstance(filename, str):
		return None
	m = _SERVER_SCRIPT_FILENAME_RE.match(filename.strip())
	if not m:
		return None
	name = (m.group("name") or "").strip()
	return name or None


def is_server_script_filename(filename) -> bool:
	"""``True`` for any ``<serverscript>`` filename — named or bare. Lets
	callers branch on the family before bothering with name extraction."""
	if not filename or not isinstance(filename, str):
		return False
	return filename.strip().startswith(_SERVER_SCRIPT_PREFIX)


def get_server_script_record(scrubbed_name: str, *, cache: dict | None = None) -> dict | None:
	"""Look up a Server Script DocType row by its scrubbed name (the form
	stored in the synthetic ``safe_exec`` filename) and return
	``{"name": <actual>, "script": <body>}`` or ``None``.

	Frappe's ``scrub`` lowercases + replaces non-alphanumerics with ``_``, so
	the synthetic filename uses a lossy form. We resolve via a SQL ``LOWER``
	+ scrub-equivalent comparison so the original-cased ``Server Script.name``
	(which is what the Desk URL needs) round-trips.

	``cache`` (when provided) memoizes the lookup result per render to avoid
	repeated DB hits for the same script in multi-finding reports.
	"""
	if not scrubbed_name:
		return None
	if cache is not None and scrubbed_name in cache:
		return cache[scrubbed_name]

	record: dict | None = None
	try:
		import frappe

		# Frappe's scrub() is the canonical transform; replicate it in SQL so
		# we can match against the original name in one query. scrub lowercases
		# + replaces non-alphanumeric (excluding ``_``) with ``_``. SQL
		# REPLACE chains approximate the common cases (`` `` / ``-``).
		rows = frappe.db.sql(
			"""
			SELECT name, script
			FROM `tabServer Script`
			WHERE LOWER(name) = %(s)s
			   OR LOWER(REPLACE(name, ' ', '_')) = %(s)s
			   OR LOWER(REPLACE(REPLACE(name, ' ', '_'), '-', '_')) = %(s)s
			LIMIT 1
			""",
			{"s": scrubbed_name.lower()},
			as_dict=True,
		)
		if rows:
			record = {"name": rows[0]["name"], "script": rows[0]["script"] or ""}
	except Exception:
		record = None

	if cache is not None:
		cache[scrubbed_name] = record
	return record


def get_server_script_lines(scrubbed_name: str, *, cache: dict | None = None) -> list[str] | None:
	"""Return the Server Script's ``script`` field split into 1-based lines,
	or ``None`` if the script can't be resolved. Reuses
	``get_server_script_record`` so a single cache covers both calls."""
	record = get_server_script_record(scrubbed_name, cache=cache)
	if not record:
		return None
	body = record.get("script") or ""
	if not body:
		return None
	return body.splitlines()


def desk_url(scrubbed_name: str, *, cache: dict | None = None) -> str:
	"""Build a Desk URL for the Server Script. When ``scrubbed_name`` resolves
	to an actual Server Script (via the DB lookup), the URL points at the
	specific form (``/app/server-script/<actual-name>``). When it can't be
	resolved, falls back to the list page (``/app/server-script``) so the
	link is still useful for a one-click jump.

	Names in the Frappe Desk URL keep their original casing + spaces (Frappe
	URL-encodes server-side). We pass the actual name through as-is and let
	the browser handle escaping."""
	record = get_server_script_record(scrubbed_name, cache=cache)
	if record and record.get("name"):
		return f"/app/server-script/{record['name']}"
	return "/app/server-script"
