# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Source-resolution helpers — dotted-path → ``(abs_file, lineno,
func_name)`` and adjacent display helpers.

Used by the renderer to resolve action entry-points (the ``def`` line
of a Frappe API method or RQ job target) and repeated-hot-frame keys
(the ``short_path::func`` shape that ``call_tree._redacted_module_key``
emits) to a concrete source location + a ±1-line snippet for the
report's "Where this fired" callsite blocks.

Extracted from ``_internal.py`` in v0.12.23 as **prep work for
finding_enrichment phase 3** — the HIGH-coupling finding-enrichment
subset (`_finding_to_dict`, `_attach_representative_callsites`, etc.)
depends on the 6 helpers in this module. With them lifted to a sibling
submodule, the next renderer extraction PR can move that subset cleanly
without dragging in a sprawling helper family.

The 6 functions move as a tight cluster — they only call each other
plus stdlib (`importlib`, `inspect`, `os`, `re`) and lazy imports of
sibling renderer submodules (`source.py`'s `_read_source_snippet` /
`_resolve_source_path`) and `frappe.utils.get_bench_path`. No back-
reference into `_internal.py`.

Public surface (all underscore-prefixed but exposed via the package
``__init__.py`` dir-walk so legacy `renderer.X` resolves):

* ``_action_dotted_entry(action)`` — derive an action's dotted entry-
  point path, or ``None``.
* ``_skip_decorators_to_def(abs_filename, start_lineno, fn_name)`` —
  walk past `@decorator` lines to land on `def <fn_name>`.
* ``_resolve_dotted_to_code(dotted)`` —
  ``(abs_filename, lineno, func_name)`` from a dotted module path.
* ``_bench_relative_display(abs_path)`` — display form
  (``apps/<app>/...``).
* ``_action_entry_callsite(action, *, cache)`` — full resolution:
  action → dotted → code → ``{filename, _abs, lineno, function,
  source_snippet}``.
* ``_resolve_frame_key_to_callsite(function_key, *, cache)`` — same
  but starting from a repeated-hot-frame key (``short_path::func``).
"""

from __future__ import annotations

import os
import re

from optimus.renderer.source import _read_source_snippet, _resolve_source_path


def _action_dotted_entry(action) -> str | None:
	"""Derive an action's dotted entry-point path, or ``None``.

	- RQ Job: ``action["path"]`` is already the job method (Frappe's
	  recorder stores ``frappe.job.method`` there — e.g.
	  ``ugly_code.python.common.bg_recheck_users``).
	- HTTP Request whose path is ``/api/method/<dotted>``: the ``<dotted>``
	  segment, with any ``?query`` and trailing ``/...`` stripped.
	- anything else (non-``/api/method`` HTTP, empty/missing path, non-dict
	  input): ``None``.
	"""
	if not isinstance(action, dict):
		return None
	event_type = (action.get("event_type") or "").strip()
	path = (action.get("path") or "").strip()
	if not path:
		return None
	if event_type == "RQ Job":
		return path.split("?", 1)[0].strip() or None
	if event_type == "HTTP Request" and path.startswith("/api/method/"):
		rest = path[len("/api/method/"):]
		rest = rest.split("?", 1)[0].split("/", 1)[0].strip().strip(".")
		return rest or None
	return None


def _skip_decorators_to_def(
	abs_filename: str,
	start_lineno: int,
	fn_name: str,
	*,
	cache: dict | None = None,
) -> int:
	"""Return the lineno of ``def <fn_name>`` / ``async def <fn_name>``
	at or after ``start_lineno`` in ``abs_filename``. Returns
	``start_lineno`` unchanged when the line at ``start_lineno`` isn't
	a decorator OR no matching def is found within 30 lines.

	On CPython 3.11+, ``code.co_firstlineno`` for a decorated function
	points at the first **decorator** line rather than the ``def``
	line. Every Frappe API endpoint is ``@frappe.whitelist``-decorated,
	so without this skip the Per-Action breakdown's entry-callsite
	snippets all highlight ``@frappe.whitelist(...)`` instead of the
	actual signature below it.

	Reads through ``cache`` (the shared per-render file_cache) when
	available so multiple decorated functions in the same file don't
	each re-read the source.
	"""
	if not abs_filename or start_lineno <= 0 or not fn_name:
		return start_lineno
	# Read source (cache-aware).
	if cache is not None and abs_filename in cache:
		lines = cache[abs_filename]
	else:
		try:
			with open(abs_filename, encoding="utf-8") as fh:
				lines = fh.read().splitlines()
		except Exception:
			lines = None
		if cache is not None:
			cache[abs_filename] = lines
	if not lines or start_lineno > len(lines):
		return start_lineno
	# Cheap early exit: the line at start_lineno isn't a decorator →
	# nothing to skip.
	first = lines[start_lineno - 1].lstrip()
	if not first.startswith("@"):
		return start_lineno
	# Scan forward (≤ 30 lines) for the def line.
	pat = re.compile(
		r"^\s*(?:async\s+)?def\s+" + re.escape(fn_name) + r"\b"
	)
	last = min(len(lines), start_lineno + 30)
	for i in range(start_lineno, last):
		if pat.match(lines[i]):
			return i + 1  # convert 0-indexed to 1-indexed lineno
	return start_lineno  # no def found — fall back to original


def _resolve_dotted_to_code(
	dotted,
	*,
	file_cache: dict | None = None,
) -> tuple[str, int, str] | None:
	"""Resolve a dotted module path to ``(abs_filename, lineno, func_name)``.

	Uses ``importlib`` directly — NOT ``frappe.get_attr`` — because the
	latter needs a running site (it touches ``frappe.local``), which the unit
	tests don't have. Mirrors ``line_profile.picker.resolve_freeform``'s
	import strategy (longest importable leading prefix, then ``getattr`` the
	rest), minus its eligibility checks. ``inspect.unwrap`` sees through
	``functools.wraps`` decorators (e.g. ``@frappe.whitelist``). Returns
	``None`` on any failure — never raises.

	v0.7.x: when ``code.co_firstlineno`` points at a decorator line
	(CPython 3.11+ behavior for decorated functions), the lineno is
	advanced to the ``def`` line via ``_skip_decorators_to_def`` so
	the callsite snippet anchors on the function signature rather
	than ``@frappe.whitelist(...)``.
	"""
	if not dotted or "." not in str(dotted):
		return None
	try:
		import importlib
		import inspect

		parts = str(dotted).split(".")
		module = None
		mod_parts = 0
		for i in range(len(parts), 0, -1):
			try:
				module = importlib.import_module(".".join(parts[:i]))
				mod_parts = i
				break
			except Exception:
				continue
		if module is None or mod_parts == len(parts):
			return None  # nothing imported, or it's a module not a callable
		obj = module
		for attr in parts[mod_parts:]:
			obj = getattr(obj, attr)
		obj = inspect.unwrap(obj)
		code = getattr(obj, "__code__", None)
		if code is None:
			return None  # builtin / C func / not a plain Python function
		filename = code.co_filename or ""
		lineno = code.co_firstlineno or 0
		if not filename or filename.startswith("<") or lineno <= 0:
			return None  # Server Script / eval'd code / bogus
		abs_path = os.path.abspath(filename)
		fn_name = getattr(obj, "__name__", "") or ""
		lineno = _skip_decorators_to_def(
			abs_path, int(lineno), fn_name, cache=file_cache,
		)
		return (abs_path, int(lineno), fn_name)
	except Exception:
		return None


def _bench_relative_display(abs_path: str) -> str:
	"""Display form of an absolute source path: ``apps/<app>/.../file.py``
	(relative to the bench root). Falls back to the absolute path when the
	file is outside the bench or the bench path can't be determined."""
	try:
		from frappe.utils import get_bench_path

		rel = os.path.relpath(abs_path, get_bench_path())
		if rel and not rel.startswith(".."):
			return rel.replace("\\", "/")
	except Exception:
		pass
	return abs_path


def _action_entry_callsite(action, *, cache: dict | None = None) -> dict | None:
	"""Resolve an action's entry-point source location + a ±1-line snippet.

	Returns ``{"filename": <bench-relative display path>, "_abs": <absolute>,
	"lineno": <def line>, "function": <name>, "source_snippet": [...] | None}``
	— or ``None`` when there's no clean dotted entry point / it can't be
	resolved / the callable has no real source. ``source_snippet`` may itself
	be ``None`` if the file can't be read (the template guards on it).

	``cache`` (shared across all actions in one render) is forwarded to
	``_read_source_snippet`` so a cluster of actions in one source file reads
	it once. Resolution itself isn't memoized — it's cheap (``importlib`` on
	already-imported modules) and reports have only tens of actions.
	"""
	dotted = _action_dotted_entry(action)
	if not dotted:
		return None
	resolved = _resolve_dotted_to_code(dotted, file_cache=cache)
	if not resolved:
		return None
	abs_path, lineno, name = resolved
	return {
		"filename": _bench_relative_display(abs_path),
		"_abs": abs_path,
		"lineno": lineno,
		"function": name,
		"source_snippet": _read_source_snippet(abs_path, lineno, cache=cache),
	}


def _resolve_frame_key_to_callsite(function_key, *, cache: dict | None = None) -> dict | None:
	"""Resolve a Repeated Hot Frame's ``function`` value to a callsite + a
	±1-line snippet, or ``None``.

	The key is ``call_tree._redacted_module_key``'s output:
	``f"{short_path}::{func}"`` where ``short_path`` is the last ≤2 path
	segments of the original file (e.g. ``ugly_code/python/common.py``) and
	``func`` is the bare frame name (occasionally ``Class.method``); or just
	``func`` when there was no filename. Best-effort, render-time:

	  1. ``short_path`` → dotted module + ``.func`` → ``_resolve_dotted_to_code``
	     (works for shallow user-app paths; deep paths where 2 segments can't
	     rebuild the real module just fall through).
	  2. fallback: resolve ``short_path`` to a real file and grep for the first
	     ``def <func>`` line.

	A bare ``func`` (no ``::``) can't be resolved without a module → ``None``.
	Returns ``{"filename","_abs","lineno","function","source_snippet"}`` or
	``None``. Wrapped in try/except — never raises.
	"""
	if not function_key:
		return None
	try:
		key = str(function_key)
		if "::" not in key:
			return None
		short_path, _, func = key.partition("::")
		short_path = short_path.strip()
		func = func.strip()
		if not short_path or not func:
			return None

		# (1) "ugly_code/python/common.py" + "looped_validate"
		#     → "ugly_code.python.common.looped_validate"
		norm = short_path.replace("\\", "/")
		if norm.endswith(".py"):
			norm = norm[:-3]
		dotted = norm.replace("/", ".").strip(".")
		if dotted:
			resolved = _resolve_dotted_to_code(f"{dotted}.{func}", file_cache=cache)
			if resolved:
				abs_path, lineno, name = resolved
				return {
					"filename": _bench_relative_display(abs_path),
					"_abs": abs_path,
					"lineno": lineno,
					"function": name or func,
					"source_snippet": _read_source_snippet(abs_path, lineno, cache=cache),
				}

		# (2) grep the resolved file for "def <last component of func>"
		abs_path = _resolve_source_path(short_path)
		if abs_path:
			bare = func.rsplit(".", 1)[-1]
			pat = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+" + re.escape(bare) + r"\b")
			try:
				with open(abs_path, encoding="utf-8") as fh:
					for i, line in enumerate(fh, start=1):
						if pat.match(line):
							return {
								"filename": _bench_relative_display(abs_path),
								"_abs": abs_path,
								"lineno": i,
								"function": func,
								"source_snippet": _read_source_snippet(abs_path, i, cache=cache),
							}
			except Exception:
				pass
	except Exception:
		return None
	return None
