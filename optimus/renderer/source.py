# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""On-disk source-file access + a bounded LRU cache for the renderer.

Every finding card that includes a source snippet, every AI-fix payload
that includes a source window, and every Phase-2 hot-line drilldown
ultimately calls back here to read source lines from the bench's app
files. Three responsibilities:

  * **Path resolution** — :func:`_resolve_source_path` turns the
    app-relative paths recorded by the analyzer (``ugly_code/python/
    common.py``) into real absolute paths via ``frappe.get_app_path`` /
    bench fallback. Server Script callsites resolve to a tuple sentinel
    that downstream branches load from the ``tabServer Script`` DocType
    instead of disk.

  * **Bench-boundary check** — :func:`_path_within_bench` is the Phase-K
    hardening guard: a synthetic callsite that points at ``/etc/passwd``
    (analyzer dict tampering, malformed pyinstrument frame) is refused.
    Bypassed under ``frappe.flags.in_test`` so pytest fixtures pointing
    at ``/tmp/...`` paths work.

  * **Per-render file cache** — :class:`_BoundedFileCache` is the bounded
    LRU dict the per-render call sites pass around as ``file_cache=``.
    Caps memory growth on sessions that touch many unique source files
    (the unbounded variant could hold ~50MB of file content on a
    monolithic codebase). 50-entry cap, move-to-end LRU semantics.

The two readers (:func:`_read_source_snippet`, :func:`_read_source_window`)
share the per-line truncation constant (:data:`_SNIPPET_TRUNCATE_CHARS`,
200 chars) so a minified single-line file doesn't blow up the LLM payload
or the finding card. Both readers go through :func:`_resolve_source_path`
so the boundary check and Server Script branching are uniform.

This module imports nothing from ``optimus.renderer._internal`` — the
``_internal`` module is the consumer. ``frappe`` is lazy-imported inside
the functions so the pure-pytest tests don't need a bench.
"""

from __future__ import annotations

import os
from collections import OrderedDict

# Per-line truncation for source snippets/windows — keeps a single
# multi-kilobyte minified line out of technical_detail_json / the LLM
# prompt. Kept here (with the readers) rather than imported from
# analyze.py — so the readers don't pull in analyze.py, which imports
# frappe.recorder.
_SNIPPET_TRUNCATE_CHARS = 200

_FILE_CACHE_MAX_ENTRIES = 50


class _BoundedFileCache:
	"""Bounded LRU dict for ``_read_source_snippet``'s per-render file
	cache. Caps memory growth on sessions that touch many unique
	source files (the unbounded variant could hold ~50MB of file
	content on a monolithic codebase). Supports the same dict-style
	protocol the read site already uses: ``filename in cache``,
	``cache[filename]``, ``cache[filename] = lines``.
	"""

	__slots__ = ("_data", "_max")

	def __init__(self, max_entries: int = _FILE_CACHE_MAX_ENTRIES):
		self._data: OrderedDict = OrderedDict()
		self._max = max_entries

	def __contains__(self, key) -> bool:
		return key in self._data

	def __getitem__(self, key):
		# Touch on read so true LRU semantics apply (recently-read
		# files stay in the cache longer than ones read once and
		# forgotten).
		value = self._data[key]
		self._data.move_to_end(key)
		return value

	def __setitem__(self, key, value) -> None:
		if key in self._data:
			self._data.move_to_end(key)
		self._data[key] = value
		while len(self._data) > self._max:
			self._data.popitem(last=False)


def _path_within_bench(path: str) -> bool:
	"""Phase-K-hardening boundary check: return True only when the
	absolute ``path`` lies inside the bench directory tree. Used by
	``_resolve_source_path`` to refuse callsite filenames that resolve
	to ``/etc/passwd`` or other locations outside the bench.

	Returns ``True`` (bypass) when:
	  - ``frappe.flags.in_test`` is set (pytest fixtures legitimately
	    point at /tmp/... paths the boundary would otherwise reject);
	  - ``frappe.utils.get_bench_path`` isn't importable / fails (the
	    check is a defence-in-depth layer, not a hard requirement).
	"""
	try:
		import frappe
		if getattr(frappe.flags, "in_test", False):
			return True
		import frappe.utils
		bench = frappe.utils.get_bench_path()
	except Exception:
		return True
	if not bench:
		return True
	try:
		bench_abs = os.path.abspath(bench)
		path_abs = os.path.abspath(path)
	except Exception:
		return False
	return path_abs == bench_abs or path_abs.startswith(bench_abs + os.sep)


def _resolve_source_path(filename):
	"""Map a finding's callsite ``filename`` to a real file on disk — OR to a
	Server Script sentinel for synthetic ``<serverscript>`` filenames.

	Return shapes:
	  - ``str`` — a real on-disk path (for app code / framework code).
	  - ``("server_script", scrubbed_name)`` — Server Script tuple sentinel.
	    Snippet readers branch on ``isinstance(resolved, tuple)`` and load
	    the script body from the ``tabServer Script`` DocType via
	    ``optimus.server_script_source.get_server_script_lines``. The
	    template/callsite-builder side branches similarly to render a Desk
	    link instead of a ``vscode://file`` editor link.
	  - ``None`` — unresolvable (truly synthetic frames like ``<string>`` /
	    ``<frozen …>``, missing files, or paths that escape the bench).

	Call-tree / pyinstrument callsites are stored in app-relative form
	(``<app>/<module-path-within-the-app-dir>`` — e.g. ``ugly_code/python/
	common.py`` for ``<bench>/apps/ugly_code/ugly_code/python/common.py``,
	or ``frappe/handler.py``). A bare ``open()`` fails because the Frappe
	process cwd is ``<bench>/sites``. Resolve via ``frappe.get_app_path``
	(``frappe.get_app_path("ugly_code", "python", "common.py")`` →
	``<bench>/apps/ugly_code/ugly_code/python/common.py``), with fallbacks
	for absolute / cwd-relative / ``apps/…``-prefixed forms.

	Phase K hardening: every resolved path is finally checked against
	the bench-directory boundary (``_path_within_bench``). A filename
	that points outside the bench (e.g. ``/etc/passwd`` via a
	malicious analyzer dict) returns ``None``.
	"""
	if not filename:
		return None
	name = str(filename).strip()
	if not name:
		return None
	# Server Script special case: bridge to DB-stored script body via the
	# tuple sentinel; downstream branches load + link to the Desk form.
	if name.startswith("<serverscript") or name.startswith("<server-script"):
		from optimus.server_script_source import extract_script_name

		_scrubbed = extract_script_name(name)
		if _scrubbed:
			return ("server_script", _scrubbed)
		# Bare ``<serverscript>`` — no script to look up; treat as
		# unresolvable so the renderer falls back to plain-text display
		# without a broken link.
		return None
	if name.startswith("<"):
		return None
	resolved: str | None = None
	try:
		if os.path.isabs(name):
			resolved = name if os.path.exists(name) else None
		elif os.path.exists(name):
			resolved = name
		else:
			parts = [p for p in name.replace("\\", "/").split("/") if p]
			if not parts:
				return None
			import frappe

			candidates = []
			try:
				candidates.append(frappe.get_app_path(parts[0], *parts[1:]))
			except Exception:
				pass
			try:
				import frappe.utils
				bench = frappe.utils.get_bench_path()
				candidates.append(os.path.join(bench, name))
				candidates.append(os.path.join(bench, "apps", name))
			except Exception:
				pass
			for cand in candidates:
				if cand and os.path.exists(cand):
					resolved = cand
					break
	except Exception:
		return None
	if resolved and not _path_within_bench(resolved):
		# Defence-in-depth: refuse paths that escape the bench tree.
		# Log at warning level (best-effort - frappe may not be
		# importable in unit-test contexts).
		try:
			import frappe
			frappe.logger().warning(
				f"optimus._resolve_source_path: rejected out-of-bench path {resolved!r}"
			)
		except Exception:
			pass
		return None
	return resolved


def _read_source_snippet(
	filename: str,
	lineno,
	*,
	cache: dict | None = None,
) -> list[dict] | None:
	"""Return a ±1-line source snippet for ``(filename, lineno)``, or
	``None`` when the file isn't readable / lineno is out of range. The
	(possibly app-relative) ``filename`` is resolved via
	``_resolve_source_path`` before opening. Server Script filenames
	(``<serverscript>: name``) resolve to a tuple sentinel and are read
	from the ``tabServer Script`` DocType instead of disk."""
	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	if cache is not None and filename in cache:
		lines = cache[filename]
	else:
		resolved = _resolve_source_path(filename)
		if isinstance(resolved, tuple) and resolved[0] == "server_script":
			from optimus.server_script_source import get_server_script_lines

			lines = get_server_script_lines(resolved[1], cache=cache)
		else:
			try:
				with open(resolved, encoding="utf-8") as fh:
					lines = fh.read().splitlines()
			except Exception:
				lines = None
		if cache is not None:
			cache[filename] = lines

	if not lines:
		return None

	limit = _SNIPPET_TRUNCATE_CHARS
	snippet: list[dict] = []
	# v0.7.x: read a ±2-line window around the anchor (compromise
	# between ±1 — too tight, body invisible — and ±4 — included
	# preceding-function noise). The template's blank-line filter
	# drops empties (except the callsite itself), so the visible
	# snippet ends up at ~3-4 lines: the anchor `def` + a couple of
	# body lines. For the exact hot line inside the function, the
	# Slow-Hot-Path description points to the Line-Level Drilldown.
	for n in range(max(1, ln - 2), ln + 3):
		if 1 <= n <= len(lines):
			content = lines[n - 1]
			if len(content) > limit:
				content = content[:limit] + "..."
			snippet.append({"lineno": n, "content": content})
	return snippet or None


def _read_source_window(
	filename: str,
	lineno,
	*,
	before: int = 12,
	after: int = 12,
	cache: dict | None = None,
	max_line_chars: int | None = None,
) -> list[dict] | None:
	"""Return a wider source window around ``(filename, lineno)`` for the
	AI-fix prompt: a list of ``{lineno, content, is_target}`` covering
	``lineno - before`` … ``lineno + after`` (clamped to the file). Same
	per-line truncation as ``_read_source_snippet`` unless ``max_line_chars``
	overrides it. Returns ``None`` when the file isn't readable / the lineno
	is out of range. The (possibly app-relative) ``filename`` is resolved via
	``_resolve_source_path`` before opening.
	"""
	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	if cache is not None and filename in cache:
		lines = cache[filename]
	else:
		resolved = _resolve_source_path(filename)
		try:
			with open(resolved, encoding="utf-8") as fh:
				lines = fh.read().splitlines()
		except Exception:
			lines = None
		if cache is not None:
			cache[filename] = lines

	if not lines:
		return None

	limit = max_line_chars or _SNIPPET_TRUNCATE_CHARS
	start = max(1, ln - max(0, before))
	end = min(len(lines), ln + max(0, after))
	window: list[dict] = []
	for n in range(start, end + 1):
		content = lines[n - 1]
		if len(content) > limit:
			content = content[:limit] + "..."
		window.append({"lineno": n, "content": content, "is_target": n == ln})
	return window or None
