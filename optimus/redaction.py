# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Sensitive-data redaction — pure functions, no Frappe imports.

Two responsibilities:

  * :func:`redact_sensitive` — walks a dict / list and replaces values
    under keys whose name contains a sensitive substring
    (``password``, ``api_key``, ``token``, …) with ``"<REDACTED:keyname>"``.
    Used for ``form_dict``, ``headers``, and any nested envelope.

  * :func:`redact_sql_literals` — replaces literal RHS values in
    ``<sensitive_column> = '...'`` SQL comparisons with ``'<REDACTED>'``.
    Best-effort regex; misses UPDATE SET clauses and obscure shapes,
    but covers the >95% case (``WHERE password = 'admin123'`` is the
    canonical leak vector).

Pre-v0.7.x+ these helpers lived inside ``renderer.py`` and ran ONLY at
render time, which meant raw values still landed in Redis + the
persisted DocType JSON blobs (``top_queries_json``, ``technical_detail_json``,
``Optimus Background Job.error``). Customers who shared their report or
backed up their DB exfiltrated data they didn't realize they had.

v0.7.x+ the recorder-patch path in ``optimus/__init__.py`` calls these
at CAPTURE time so raw values never enter Redis. The renderer still
calls them as defense-in-depth (catches anything the patch didn't —
e.g. older sessions written under the previous contract).

Settings-driven extension: every function accepts an ``extra_keys`` /
``extra_columns`` tuple so customers can add domain-specific patterns
(``recovery_code``, ``otp_seed``, ``bank_account``, …) via Optimus
Settings without forking. Extension is ADDITIVE — there's no API to
remove a default pattern; a config typo can't accidentally disable
redaction of a known-sensitive key.

This module imports nothing from Frappe so the recorder-patch path
(which runs at app-import, before some Frappe internals are ready)
can rely on it.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Canonical default patterns. These match the historical renderer-side
# values 1:1 so the relocation is behavior-preserving; the test suite
# locks them in.
DEFAULT_SENSITIVE_KEYS: tuple[str, ...] = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
)
DEFAULT_SENSITIVE_SQL_COLUMNS: tuple[str, ...] = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
)


def is_sensitive_key(key, *, extra: tuple[str, ...] = ()) -> bool:
	"""Return True when ``key`` looks like a sensitive identifier.

	Substring (case-insensitive) match against the default patterns
	plus any caller-supplied extras. Substring (not equality) so that
	``recovery_password`` or ``user_token_v2`` still match.
	"""
	if not isinstance(key, str) or not key:
		return False
	lower = key.lower()
	patterns = DEFAULT_SENSITIVE_KEYS + tuple(extra or ())
	return any(p in lower for p in patterns)


def redact_sensitive(payload, *, extra_keys: tuple[str, ...] = ()):
	"""Walk a dict / list / tuple and return a deep copy with values
	under sensitive keys replaced by ``"<REDACTED:keyname>"``. Non-
	container scalars pass through unchanged.

	Pure — never mutates the input.
	"""
	if isinstance(payload, dict):
		out = {}
		for k, v in payload.items():
			if is_sensitive_key(k, extra=extra_keys):
				out[k] = f"<REDACTED:{k}>"
			else:
				out[k] = redact_sensitive(v, extra_keys=extra_keys)
		return out
	if isinstance(payload, list):
		return [redact_sensitive(item, extra_keys=extra_keys) for item in payload]
	if isinstance(payload, tuple):
		return tuple(redact_sensitive(item, extra_keys=extra_keys) for item in payload)
	return payload


@lru_cache(maxsize=16)
def _sql_literal_regex(columns: tuple[str, ...]) -> re.Pattern:
	"""Compile the SQL-literal regex for a given column tuple.

	Caching keeps the patched-recorder hot path fast even when an
	``extra_columns`` tuple is passed (each unique extras-tuple compiles
	once per process). The cache size is intentionally small — most
	deployments have ONE extras list (from Optimus Settings) so the
	cache holds the default pattern + at most one per setting variant.
	"""
	return re.compile(
		r"""(\b(?:""" + "|".join(re.escape(c) for c in columns) + r""")\b\s*(?:=|LIKE|IN)\s*)("[^"]*"|'[^']*'|\([^)]*\))""",
		re.IGNORECASE,
	)


def redact_sql_literals(sql_str: str, *, extra_columns: tuple[str, ...] = ()) -> str:
	"""Return ``sql_str`` with literal values in
	``<sensitive_column> = '...'`` comparisons replaced by ``'<REDACTED>'``.

	Best-effort regex pass:

	  * Quick exit when no sensitive substring appears (the 95% path —
	    avoids the regex cost on every benign query).
	  * Regex replacement on ``column (=|LIKE|IN) literal`` where
	    ``literal`` is single-quoted, double-quoted, or parenthesised.

	Imperfect on ``UPDATE … SET password = 'x'`` (covered) but misses
	multi-line CTEs and computed values; the recorder-time application
	+ render-time backup mean a single miss doesn't leak.
	"""
	if not sql_str or not isinstance(sql_str, str):
		return sql_str or ""
	columns = DEFAULT_SENSITIVE_SQL_COLUMNS + tuple(extra_columns or ())
	# Fast-path: skip the regex entirely if no sensitive name appears.
	# Saves the ~3-5µs regex cost per call on the 95% of queries that
	# touch nothing sensitive (a real hot path in capture-time use).
	lower = sql_str.lower()
	if not any(p.lower() in lower for p in columns):
		return sql_str
	try:
		return _sql_literal_regex(columns).sub(r"\1'<REDACTED>'", sql_str)
	except Exception:
		return sql_str


def redact_call_queries(calls, *, extra_columns: tuple[str, ...] = ()) -> None:
	"""Apply :func:`redact_sql_literals` over a recording's ``calls``
	list in place. Touches ``query`` + ``normalized_query`` fields.

	Mutates input — different shape from the dict/list redactor because
	calls lists are large and copying is wasteful when the caller
	already owns the recording dict.
	"""
	if not isinstance(calls, list):
		return
	for call in calls:
		if not isinstance(call, dict):
			continue
		if call.get("query"):
			call["query"] = redact_sql_literals(call["query"], extra_columns=extra_columns)
		if call.get("normalized_query"):
			call["normalized_query"] = redact_sql_literals(
				call["normalized_query"], extra_columns=extra_columns
			)
