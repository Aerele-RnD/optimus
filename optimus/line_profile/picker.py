# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 picker: turn phase-1 results into a candidate function list and
resolve free-form dotted paths typed by the customer.

The picker has two responsibilities:

1. **Curated list** — walk the pyinstrument call trees attached to phase-1
   actions, aggregate Python frames by dotted path, and return the top-N
   candidates with cumulative_ms + hit_count + app + framework-membership
   metadata. The form UI uses this for its multi-select.

2. **Free-form resolution** — given a dotted path the customer typed
   (``my_app.tasks.heavy_job``), import the module, walk attribute access,
   and validate eligibility for ``line_profiler``. C-extensions / builtins
   lack ``__code__`` and cannot be line-profiled; lambdas and Server
   Scripts (filename starts with ``<``) are similarly out-of-scope.

This module is **pure** — no Frappe DB / Redis access. The API endpoint
(``api.py``) loads the parsed call trees from the Optimus Session and
passes them to ``_build_candidates_from_trees``.
"""

import importlib

from optimus.analyzers.base import FRAMEWORK_APPS
from optimus.analyzers.call_tree import _is_pure_helper_frame

CANDIDATE_CAP = 30


class PickerError(Exception):
	"""Raised when free-form resolution fails (bad import, unknown attribute,
	empty path, or top-level-module-only). The form surfaces the message to
	the customer inline."""


def _is_synthetic_frame(function: str) -> bool:
	"""pyinstrument synthesizes nodes for ``<root>``, ``<sql>``, and
	bracketed pseudo-frames like ``[finalize]``. None of these are real
	Python functions and they cannot be line-profiled."""
	if not function:
		return True
	return function.startswith("<") or function.startswith("[")


def _derive_module_path(filename: str) -> str:
	"""Build a Python module dotted path from a captured filename.

	pyinstrument captures filenames like
	``apps/erpnext/erpnext/selling/doctype/sales_invoice/sales_invoice.py``
	(Frappe convention: the Python package directory matches the app name
	and lives one level deeper than ``apps/<app>/``). This helper strips
	the ``apps/<app>/`` wrapper and the ``.py`` suffix, then joins the
	remaining segments with dots.

	Returns "" when the filename can't be parsed (synthetic frames,
	stdlib paths, etc.).
	"""
	if not filename:
		return ""
	parts = [p for p in filename.replace("\\", "/").split("/") if p]
	if not parts:
		return ""
	# Strip leading "apps" (or "/apps") — Frappe convention. Subsequent
	# duplicate (`apps/<app>/<app>/`) is the package-name double we want
	# to collapse.
	if "apps" in parts:
		idx = parts.index("apps")
		parts = parts[idx + 1 :]
	if len(parts) >= 2 and parts[0] == parts[1]:
		parts = parts[1:]
	# Strip .py from the last segment (handles .pyx and __init__.py too).
	if parts and parts[-1].endswith(".py"):
		parts[-1] = parts[-1][: -len(".py")]
	if parts and parts[-1] == "__init__":
		parts.pop()
	return ".".join(parts)


def _derive_app(filename: str) -> str:
	"""Extract the Frappe app name from a filename.

	``apps/erpnext/erpnext/...`` → ``erpnext``. Falls back to the first
	non-empty segment when the path doesn't follow the bench layout.
	"""
	if not filename:
		return ""
	parts = [p for p in filename.replace("\\", "/").split("/") if p]
	if "apps" in parts:
		idx = parts.index("apps")
		if idx + 1 < len(parts):
			return parts[idx + 1]
	return parts[0] if parts else ""


def _build_dotted_path(filename: str, function: str) -> str:
	"""Combine module path + function name. Function may already include
	a class qualifier (e.g. ``SalesInvoice.validate``); pass it through
	unchanged in that case.
	"""
	module = _derive_module_path(filename)
	if not module:
		# Fall back to just the function name; resolve_freeform will reject
		# it as a top-level module candidate so the user knows to type a
		# fuller path in the freeform textbox.
		return function or ""
	if not function:
		return module
	return f"{module}.{function}"


def _walk_tree(node: dict, hits: dict) -> None:
	"""Recursively walk a pyinstrument tree, accumulating per-function
	cumulative_ms and hit count into ``hits``.

	pyinstrument captures the bare function name in ``function`` and the
	source path in ``filename``; we combine the two via
	``_build_dotted_path`` to get something the picker can attempt to
	import. The aggregation key uses the derived dotted path so frames
	with the same name in different modules don't collide.
	"""
	if not isinstance(node, dict):
		return

	function = node.get("function") or ""
	filename = node.get("filename") or ""
	kind = node.get("kind", "python")

	if (
		kind == "python"
		and not _is_synthetic_frame(function)
		# Drop framework plumbing/wrapper frames — every request goes
		# through frappe.app.application, frappe.handler.handle,
		# frappe.utils.typing_validations.wrapper, frappe.recorder.record_sql,
		# frappe.model.document.save / fn / runner / composer, etc. These
		# always dominate the leaderboard but line-profiling them is
		# pointless for the user. Reuses the same filter the call_tree
		# analyzer applies to the Repeated Hot Frame leaderboard so the
		# picker shows the same shape of "actually optimizable" frames.
		and not _is_pure_helper_frame(node)
	):
		dotted = _build_dotted_path(filename, function)
		# Use the (filename, function) tuple as the dedup key so two
		# unrelated functions sharing a name (e.g. multiple ``validate``
		# methods across different modules) don't collapse into one
		# entry. The dotted_path is what the picker UI displays.
		key = (filename, function)
		entry = hits.get(key)
		if entry is None:
			hits[key] = {
				"dotted_path": dotted,
				"qualname": function,
				"file": filename,
				"lineno": int(node.get("lineno") or 0),
				"app": _derive_app(filename),
				"cumulative_ms": float(node.get("cumulative_ms") or 0),
				"hit_count": 1,
			}
		else:
			entry["cumulative_ms"] += float(node.get("cumulative_ms") or 0)
			entry["hit_count"] += 1

	for child in node.get("children") or []:
		_walk_tree(child, hits)


def _build_candidates_from_trees(trees: list[dict], findings: list[dict]) -> list[dict]:
	"""Aggregate candidates from per-action pyinstrument trees.

	``findings`` is currently a placeholder for future enrichment (pulling
	additional callsites from N+1/Slow-Query findings). The v1 candidate
	list is purely tree-derived.

	Each output candidate carries:
	  - ``dotted_path`` derived from filename + function name (best effort
	    — class methods may need a freeform correction at pick time)
	  - ``qualname`` the bare function name as captured (e.g. ``validate``)
	  - ``file`` / ``lineno`` the captured source location
	  - ``app`` extracted from filename's ``apps/<app>/`` prefix
	  - ``cumulative_ms`` / ``hit_count`` summed across the input trees
	  - ``is_framework`` from FRAMEWORK_APPS membership
	"""
	hits: dict = {}
	for tree in trees:
		_walk_tree(tree, hits)

	candidates = []
	for entry in hits.values():
		app = entry["app"] or (
			entry["dotted_path"].split(".", 1)[0] if "." in entry["dotted_path"] else ""
		)
		candidates.append({
			"dotted_path": entry["dotted_path"],
			"qualname": entry["qualname"],
			"file": entry["file"],
			"lineno": entry["lineno"],
			"app": app,
			"cumulative_ms": round(entry["cumulative_ms"], 2),
			"hit_count": entry["hit_count"],
			"is_framework": app in FRAMEWORK_APPS,
		})

	candidates.sort(key=lambda c: c["cumulative_ms"], reverse=True)
	return candidates[:CANDIDATE_CAP]


def _find_hottest_match(call_trees: list[dict], target_dotted_path: str) -> dict | None:
	"""Walk every node in every tree; among nodes whose derived dotted
	path equals ``target_dotted_path``, return the one with the highest
	``cumulative_ms``. ``None`` when no match exists (free-form pick that
	never appeared in phase 1, or wrong path).
	"""
	best: dict | None = None
	best_ms = -1.0

	def walk(node):
		nonlocal best, best_ms
		if not isinstance(node, dict):
			return
		function = node.get("function") or ""
		filename = node.get("filename") or ""
		kind = node.get("kind", "python")
		if kind == "python" and not _is_synthetic_frame(function):
			derived = _build_dotted_path(filename, function)
			if derived == target_dotted_path:
				ms = float(node.get("cumulative_ms") or 0)
				if ms > best_ms:
					best = node
					best_ms = ms
		for child in node.get("children") or []:
			walk(child)

	for tree in call_trees:
		walk(tree)
	return best


def _eligible_descent_children(node: dict, min_ms: float) -> list[dict]:
	"""Filter a node's children to those eligible for hot-chain descent.
	Drops synthetic frames, non-Python frames, pure-helper / ORM /
	wrapper boundaries (so the chain ends at framework code), and frames
	below the ms floor.
	"""
	out = []
	for child in node.get("children") or []:
		if not isinstance(child, dict):
			continue
		fn = child.get("function") or ""
		if (child.get("kind", "python") != "python"):
			continue
		if _is_synthetic_frame(fn):
			continue
		if _is_pure_helper_frame(child):
			continue
		if float(child.get("cumulative_ms") or 0) < min_ms:
			continue
		out.append(child)
	return out


def expand_hot_chain(
	call_trees: list[dict],
	picked_dotted_path: str,
	max_depth: int = 10,
	min_ms: float = 50.0,
) -> list[dict]:
	"""Return the hottest user-code descent path from ``picked_dotted_path``.

	Walks down phase-1's call tree from the picked frame, following the
	single hottest user-code child at each level. Stops descending when:

	  • the next hottest child would cross a ``_is_pure_helper_frame``
	    boundary (frappe ORM / recorder / typing wrappers / document.py),
	  • the next hottest child has ``cumulative_ms < min_ms``,
	  • there is no Python child remaining,
	  • or the chain has reached ``max_depth``.

	Output rows::

	    {
	        "dotted_path", "qualname", "file", "lineno",
	        "cumulative_ms", "depth"
	    }

	The picked frame is depth=0; each descendant carries depth=1, 2, ...
	Returns ``[]`` when the picked path doesn't appear in any tree (e.g.
	a free-form pick that wasn't called in phase 1).
	"""
	root = _find_hottest_match(call_trees, picked_dotted_path)
	if root is None:
		return []

	chain: list[dict] = [{
		"dotted_path": picked_dotted_path,
		"qualname": root.get("function") or "",
		"file": root.get("filename") or "",
		"lineno": int(root.get("lineno") or 0),
		"cumulative_ms": round(float(root.get("cumulative_ms") or 0), 2),
		"depth": 0,
	}]

	current = root
	depth = 0
	while depth < max_depth:
		eligible = _eligible_descent_children(current, min_ms)
		if not eligible:
			break
		hottest = max(
			eligible, key=lambda c: float(c.get("cumulative_ms") or 0)
		)
		depth += 1
		chain.append({
			"dotted_path": _build_dotted_path(
				hottest.get("filename") or "",
				hottest.get("function") or "",
			),
			"qualname": hottest.get("function") or "",
			"file": hottest.get("filename") or "",
			"lineno": int(hottest.get("lineno") or 0),
			"cumulative_ms": round(float(hottest.get("cumulative_ms") or 0), 2),
			"depth": depth,
		})
		current = hottest

	return chain


def _check_eligibility(obj) -> tuple[bool, str | None]:
	"""Return (eligible, reason). line_profiler can attach to functions
	with a real ``__code__`` object that aren't lambdas or runtime-eval'd
	code."""
	code = getattr(obj, "__code__", None)
	if code is None:
		return False, "C-extension or builtin (no __code__ attribute)"

	name = getattr(obj, "__name__", "")
	if name == "<lambda>":
		return False, "lambda functions cannot be line-profiled"

	filename = getattr(code, "co_filename", "") or ""
	if filename.startswith("<"):
		# Server Scripts compile via compile(source, '<server_script_...>', ...).
		return False, "Server Scripts cannot be line-profiled (no stable file)"

	return True, None


def resolve_freeform(dotted_path: str) -> dict:
	"""Resolve a free-form dotted path to a function metadata dict.

	Returns a dict with the same shape as ``_build_candidates_from_trees``
	entries, plus ``eligible`` and (when ineligible) ``ineligible_reason``.

	Raises ``PickerError`` if the path is empty, points only at a module,
	or any prefix of it can't be imported / walked.
	"""
	if not dotted_path:
		raise PickerError("dotted path is empty")

	parts = dotted_path.split(".")
	if len(parts) < 2:
		raise PickerError(
			f"'{dotted_path}' looks like a top-level module — need at least "
			"module.function (e.g. 'my_app.tasks.heavy_job')"
		)

	# Find the longest leading prefix that imports as a module.
	module = None
	module_parts = 0
	last_import_error: str | None = None
	for i in range(len(parts), 0, -1):
		candidate = ".".join(parts[:i])
		try:
			module = importlib.import_module(candidate)
			module_parts = i
			break
		except ImportError as exc:
			last_import_error = str(exc)
			continue

	if module is None:
		raise PickerError(
			f"could not import any prefix of '{dotted_path}' "
			f"(last error: {last_import_error})"
		)

	if module_parts == len(parts):
		raise PickerError(
			f"'{dotted_path}' resolved to a module, not a function — "
			"include the function name (e.g. 'json.dumps' rather than 'json')"
		)

	# Walk the remaining parts as attribute access on the resolved module.
	# Special-case the common pyinstrument shape: filename → module +
	# bare function name from a class method (e.g. validate is on
	# SalesInvoice). When direct getattr on the module fails for a
	# single-part suffix, scan classes in the module and substitute the
	# matching one. This lets the curated picker show "module.method"
	# labels and still resolve to the right callable when the user picks.
	obj = module
	remaining = parts[module_parts:]
	resolved_qualname_parts: list[str] = []

	for idx, attr in enumerate(remaining):
		try:
			obj = getattr(obj, attr)
			resolved_qualname_parts.append(attr)
		except AttributeError:
			# Try class-method search ONLY for the very next attribute on
			# a fresh module, and only when there's exactly one matching
			# class — anything else is too ambiguous to pick automatically
			# and the user should use the freeform textbox to disambiguate.
			if idx == 0 and len(remaining) == 1:
				import inspect as _inspect

				owners = []
				for member_name, member in vars(module).items():
					if not _inspect.isclass(member):
						continue
					if member.__module__ != module.__name__:
						continue  # imported, not defined here
					if attr in vars(member):
						owners.append((member_name, member))
				if len(owners) == 1:
					class_name, class_obj = owners[0]
					try:
						obj = getattr(class_obj, attr)
						resolved_qualname_parts.extend([class_name, attr])
						continue
					except AttributeError:
						pass
				if len(owners) > 1:
					choices = ", ".join(f"{cn}.{attr}" for cn, _ in owners)
					raise PickerError(
						f"'{attr}' is defined on multiple classes in "
						f"{module.__name__}: {choices}. Type the full "
						"path you want in the freeform textbox to disambiguate."
					)
			raise PickerError(
				f"attribute '{attr}' not found while resolving '{dotted_path}'"
			)

	qualname = ".".join(resolved_qualname_parts) if resolved_qualname_parts else ""
	# Rewrite dotted_path so downstream (LineProfiler.add_function et al.)
	# use the actual resolved path, not the user's possibly-class-omitting
	# input.
	rewritten = ".".join(parts[:module_parts] + resolved_qualname_parts)
	eligible, reason = _check_eligibility(obj)

	code = getattr(obj, "__code__", None)
	file_path = code.co_filename if code is not None else "<unknown>"
	lineno = code.co_firstlineno if code is not None else 0

	return {
		"dotted_path": rewritten or dotted_path,
		"qualname": qualname,
		"file": file_path,
		"lineno": lineno,
		"app": parts[0],
		"cumulative_ms": 0.0,
		"hit_count": 0,
		"is_framework": parts[0] in FRAMEWORK_APPS,
		"eligible": eligible,
		"ineligible_reason": reason,
	}
