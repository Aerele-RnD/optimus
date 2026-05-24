# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: reconcile pyinstrument call tree with SQL recordings.

The single highest-value analyzer added in v0.3.0. Walks a per-recording
pyinstrument frame tree, grafts each captured SQL call onto the deepest
matching user-code frame, and produces a unified tree where every node
knows both its Python self-time AND the SQL queries that fired underneath
it.

Findings emitted:
  - Slow Hot Path (F1): subtree consumes >25% of action wall time AND >200ms
  - Hook Bottleneck (F3): same as F1, but the subtree root is a doc-event hook
  - Repeated Hot Frame (F2): same redacted frame in ≥3 actions, ≥500ms total

Aggregates:
  - hot_frames_json: top-20 leaderboard of hottest frames in the session
  - session_time_breakdown_json: SQL ms + per-app Python ms for the donut

Terminology note (U1): internal field names use ``cumulative_ms``
(pyinstrument's term — self_time + descendant time). The
user-facing report tags the same value as ``consolidated``. This
split is intentional — don't rename one to match the other without
a project-owner sign-off.
"""

import json
from collections import defaultdict

from optimus.analyzers.base import (
	SEVERITY_ORDER,
	AnalyzerResult,
	short_filename,
)

# ---------------------------------------------------------------------------
# Reconciliation primitives (Tasks 14-16)
# ---------------------------------------------------------------------------


def _summarize_explain(explain_result):
	"""Extract the four red-flag fields from an EXPLAIN result list."""
	if not explain_result or not isinstance(explain_result, list):
		return {}
	flags = {}
	for row in explain_result:
		if not isinstance(row, dict):
			continue
		t = (row.get("type") or "").lower()
		if t == "all":
			flags["full_scan"] = True
		extra = (row.get("Extra") or row.get("extra") or "").lower()
		if "using filesort" in extra:
			flags["filesort"] = True
		if "using temporary" in extra:
			flags["temporary"] = True
		filtered = row.get("filtered")
		if isinstance(filtered, (int, float)) and filtered < 10:
			flags["low_filter"] = True
	return flags


def _short_callsite(stack: list) -> str:
	"""Build a short 'file:line' string for the SQL leaf's filename slot."""
	if not stack:
		return ""
	# Walk innermost-to-outermost, return first non-framework frame
	for frame in reversed(stack):
		fn = (frame.get("filename") or "").replace("\\", "/")
		if "/frappe/" in fn or "/optimus/" in fn:
			continue
		return f"{fn}:{frame.get('lineno', 0)}"
	# Fall back to the innermost frame
	last = stack[-1]
	return f"{last.get('filename', '?')}:{last.get('lineno', 0)}"


def _make_sql_leaf(call: dict, partial_match: bool) -> dict:
	"""Build one SQL leaf node for grafting onto the pyi tree."""
	duration = float(call.get("duration") or 0)
	return {
		"function": "<sql>",
		"filename": _short_callsite(call.get("stack") or []),
		"lineno": None,
		"self_ms": duration,
		"cumulative_ms": duration,
		"kind": "sql",
		"query_normalized": call.get("normalized_query") or call.get("query") or "",
		"query_count": 1,
		"explain_flags": _summarize_explain(call.get("explain_result")),
		"partial_match": partial_match,
		"children": [],
	}


def _empty_root() -> dict:
	return {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0.0,
		"cumulative_ms": 0.0,
		"kind": "python",
		"children": [],
	}


def _normalize_dict_tree(node: dict) -> dict:
	"""Recursively ensure dict tree has all required keys."""
	return {
		"function": node.get("function", "<unknown>"),
		"filename": node.get("filename", ""),
		"lineno": node.get("lineno", 0),
		"self_ms": float(node.get("self_ms", 0)),
		"cumulative_ms": float(node.get("cumulative_ms", 0)),
		"kind": node.get("kind", "python"),
		"children": [_normalize_dict_tree(c) for c in node.get("children", [])],
	}


def _walk_pyi_frame(frame) -> dict:
	"""Convert one pyinstrument frame (and its descendants) into our dict shape."""
	# pyinstrument's Frame object exposes:
	#   function, file_path_short, line_no, self_time, time, children
	return {
		"function": getattr(frame, "function", "<unknown>"),
		"filename": (
			getattr(frame, "file_path_short", None)
			or getattr(frame, "file_path", "")
			or ""
		),
		"lineno": getattr(frame, "line_no", 0),
		"self_ms": float(getattr(frame, "self_time", 0)) * 1000,
		"cumulative_ms": float(getattr(frame, "time", 0)) * 1000,
		"kind": "python",
		"children": [_walk_pyi_frame(c) for c in getattr(frame, "children", [])],
	}


def _pyi_to_dict_tree(pyi_session_or_dict) -> dict:
	"""Convert a pyinstrument Session to our dict-tree shape.

	If the input is already a dict (from a JSON fixture), it's returned
	with normalized field names. If it's a pyinstrument Session, we walk
	its `root_frame()` and build the dict tree.

	The returned tree has a single root node with `function="<root>"`
	whose children are the actual top-level frames.
	"""
	# Test fixture path: input is already a dict
	if isinstance(pyi_session_or_dict, dict):
		# Fixture format wraps the root under a "root" key
		if "root" in pyi_session_or_dict:
			return _normalize_dict_tree(pyi_session_or_dict["root"])
		return _normalize_dict_tree(pyi_session_or_dict)

	# Production path: walk a real pyinstrument Session
	try:
		root = pyi_session_or_dict.root_frame()
	except Exception:
		return _empty_root()

	if root is None:
		return _empty_root()

	return _walk_pyi_frame(root)


# ---------------------------------------------------------------------------
# Frame matching and graft-point selection (Task 15)
# ---------------------------------------------------------------------------


# Module path prefixes treated as "framework" — graft points roll back
# past these so SQL leaves attach under user code, not under helpers.
#
# v0.5.1: the fragments used to include leading slashes ("/frappe/",
# "/optimus/") so the substring `in` check would match an
# absolute path like /Users/.../apps/frappe/frappe/handler.py. But
# pyinstrument and our dict-tree normalizer store filenames as
# RELATIVE paths (e.g. "frappe/handler.py"), which means the filter
# was a silent no-op for every frame. Slow Hot Path then emitted
# "99% of time was spent in application" (i.e. frappe/app.py::application)
# instead of descending past the framework frame to the user code
# below. Dropping the leading slashes makes the filter work against
# both relative and absolute filenames — a relative filename like
# "frappe/handler.py" contains "frappe/", and an absolute filename
# like "/Users/.../frappe/handler.py" also contains "frappe/".
_FRAMEWORK_PATH_FRAGMENTS = ("frappe/", "optimus/")
_FRAMEWORK_FUNCTION_PREFIXES = ("frappe.", "optimus.")


def _is_framework_frame(node_or_frame: dict) -> bool:
	"""True if a tree node or stack frame looks like framework code.

	Used by SQL-to-Python reconciliation and Slow Hot Path findings,
	which want to blame user code ABOVE the framework boundary and
	therefore skip aggressively — everything in frappe/* and
	optimus/*.

	For Repeated Hot Frame + hot-frames leaderboard aggregation, use
	the NARROWER ``_is_pure_helper_frame`` instead. That function keeps
	application-layer Frappe code (Document lifecycle, permissions,
	hooks, naming) visible so users can see legitimate optimization
	targets inside frappe/* — which is a different question than
	'whose SQL is this.'
	"""
	fn = node_or_frame.get("function") or ""
	for prefix in _FRAMEWORK_FUNCTION_PREFIXES:
		if fn.startswith(prefix):
			return True
	filename = (node_or_frame.get("filename") or "").replace("\\", "/")
	for frag in _FRAMEWORK_PATH_FRAGMENTS:
		if frag in filename:
			return True
	return False


# ---------------------------------------------------------------------------
# Pure-helper frame detection (v0.5.1 — narrower than framework-frame)
# ---------------------------------------------------------------------------
# The semantic question for the Repeated Hot Frame finding is:
#
#     "If this function shows up as a hot frame, can the user act on it?"
#
# For SQL N+1 / Slow Hot Path, the answer is "walk past frappe/* to blame
# user code." Repeated Hot Frame is different: it asks "is this function
# itself worth optimizing?" Document.run_method, permissions.has_permission,
# naming.make_autoname, and most of frappe/core/* and frappe/model/* are
# INSIDE frappe/* yet legitimately slow optimization targets — they run
# the user's hooks, their permission rules, or their custom naming
# configuration. Surfacing them in the leaderboard is correct.
#
# So we skip only:
#   1. optimus/*                 — always (our own tool)
#   2. /frappe/utils/                    — data conversion, password, redis wrapper
#   3. /frappe/handler.py, /frappe/app.py — request dispatch plumbing
#   4. Infrastructure libraries          — werkzeug, gunicorn, rq, redis
#                                          client, pyinstrument, pytz, dateutil
#
# Everything else — including most of frappe/*, all of erpnext/*, and
# all user apps — is KEPT so users see legitimate optimization targets
# like "Document.run_method consumed 2.4s across 12 actions."

# v0.5.1: suffix matches (via endswith). Specific framework plumbing
# files where every request passes through the same function on its way
# from gunicorn to the user's endpoint. Surfacing these in the Repeated
# Hot Frame leaderboard is meaningless — they're always going to be in
# every action because they ARE the dispatch pipeline. Report on file
# against ERPNext showed 8 Repeated Hot Frame findings, ALL of them
# from this list.
#
# endswith() works regardless of whether the stored filename is
# absolute (/Users/.../apps/frappe/frappe/handler.py) or relative
# (frappe/handler.py) or bench-relative (apps/frappe/frappe/handler.py)
# — all three end with "frappe/handler.py".
_PURE_HELPER_PATH_SUFFIXES = (
	# Frappe WSGI entry + request dispatch
	"frappe/app.py",
	"frappe/handler.py",
	"frappe/middlewares.py",
	# frappe.call, frappe.get_doc (top-level module dispatchers)
	"frappe/__init__.py",
	# REST API routing (v0.5.1 addition — missed before because of the
	# leading-slash bug, but also needs explicit entries for v1/v2 files)
	"frappe/api/__init__.py",
	"frappe/api/v1.py",
	"frappe/api/v2.py",
	# Frappe's built-in SQL recorder hook (frappe/recorder.py). This is
	# the sql() wrapper that sits in front of every database query when
	# frappe.recorder is active. On a profiling session it's always the
	# fattest frame by occurrence count but is 100% instrumentation
	# overhead, not user code.
	"frappe/recorder.py",
	# v0.5.2: meta-loading plumbing. A real production report had 11
	# Repeated Hot Frame entries all from meta.py (Meta.__init__,
	# load_from_db, process, get_meta, ...) — identical cumulative
	# times because they're the same call tree. Users can't optimize
	# "how Meta loads its fields"; they ARE the framework's cold path.
	"frappe/model/meta.py",
	# v0.5.2: entire frappe/model/document.py is framework plumbing.
	# Document.save / insert / submit etc. look user-recognisable but
	# their cumulative time is always just the sum of the validate /
	# on_submit / etc. hooks inside them — which ARE visible as
	# separate hot frames in the user's own app code (erpnext/myapp/
	# validate). Keeping document.py entries in the leaderboard
	# produces double-counting noise: save → _save → run_method →
	# validate all show up with near-identical cumulative times.
	# Initial diagnostic run on a production session had 7 separate
	# document.py entries and only validate/set_missing_values (the
	# actual user code) told the user anything new.
	"frappe/model/document.py",
	# Form-level meta loading: getdoctype, get_meta_bundle — these are
	# what Desk calls when you open a form. Cumulative time is always
	# ~= the doctype cache miss cost, which Frappe handles internally.
	"frappe/desk/form/load.py",
	"frappe/desk/form/meta.py",
	# Query builder internals. Every SQL query the ORM builds passes
	# through these. Cumulative time on a report session is the sum
	# across all queries; the hot frame just means "this session had
	# a lot of SQL" — the top_queries/index_suggestions analyzers
	# already surface that signal more actionably.
	"frappe/query_builder/utils.py",
	"frappe/model/qb_query.py",
	# Low-level DB cursor layer. frappe.db.sql / get_values / execute
	# all land here. Same "every query passes through" story as the
	# query builder — not a user-actionable hot frame.
	"frappe/database/database.py",
	"frappe/database/mariadb/database.py",
	"frappe/database/postgres/database.py",
	# Intentionally NOT filtered: frappe/model/naming.py — a user's
	# naming-series configuration IS optimizable (simpler series,
	# fewer SQL lookups), so make_autoname staying visible in the
	# hot-frames leaderboard is correct.
)

# v0.5.1: substring matches (via `in`). Whole directories of framework
# helpers / infrastructure libraries. Fragment values MUST NOT have
# leading slashes — the stored filenames are typically relative
# (`frappe/utils/foo.py`), and `/frappe/utils/` does not occur as a
# substring of `frappe/utils/foo.py`. See the comment on
# _FRAMEWORK_PATH_FRAGMENTS for the full history of this bug.
_PURE_HELPER_PATH_SUBSTRINGS = (
	"optimus/",
	"frappe/utils/",          # typing_validations, response, redis_wrapper, etc.
	# v0.5.2: frappe/model/utils/ is plumbing too — is_virtual_doctype,
	# get_parent_doc, etc. Called by the document loader and doesn't
	# reflect any user decision. Distinct from frappe/utils/ which
	# wouldn't have matched it because there's a `/model/` in between.
	"frappe/model/utils/",
	"werkzeug/",
	"gunicorn/",
	"/rq/",                   # rq is inside site-packages/; the leading slash
	                          # disambiguates from e.g. apps/rq_something/
	"site-packages/redis/",
	"pyinstrument/",
	"pytz/",
	"dateutil/",
)

_PURE_HELPER_FUNCTION_PREFIXES = (
	"optimus.",
	"frappe.utils.",
	"frappe.handler.",
	"frappe.app.",
	"frappe.api.",             # v0.5.1 addition
	"frappe.recorder.",        # v0.5.1 addition
)

# v0.5.2: exact function names that are ALWAYS decorator wrappers or
# private delegation shims regardless of what file they're in. Classic
# pattern: functools.wraps / frappe.hook / erpnext.naming_decorator
# produce frames named "wrapper", "composer", "runner", "fn". They
# forward to the real function and their cumulative_ms is essentially
# identical to the wrapped function's — so showing BOTH in the hot-
# frame leaderboard is noise: the user sees two rows with the same
# time for every decorated method.
#
# Real production report showed 6 separate Repeated Hot Frame findings
# for Document.save's decorator chain (save → _save → run_method →
# composer → runner → fn), all with near-identical cumulative times.
# Filtering the wrappers leaves the visible-to-user entry points
# (save, run_method) while hiding the functools/hook internals.
_PURE_HELPER_FUNCTION_NAMES = frozenset({
	# functools.wraps + frappe.hook decorator internals
	"wrapper", "composer", "runner", "fn", "hook", "compose",
	"add_to_return_value",
	# Private delegation shims — always called by their non-underscored
	# sibling (save → _save, insert → _insert, etc.). Keep the public
	# entry point in the leaderboard; hide the private one.
	"_save", "_insert",
	# v0.7.x: RQ worker + scheduler wrappers. These carry the full
	# job wall time as cumulative_ms because they sit at the top of
	# every BG job's call tree. Without skipping them, the Slow Hot
	# Path walker emits a finding on the wrapper instead of descending
	# to the real user-code hot frame inside the job.
	"execute_job", "run_job", "worker_main", "perform_job",
	"enqueue", "enqueue_call", "dequeue", "dequeue_any",
	"work", "execute_in_subprocess", "perform",
})

# v0.5.2: bench/site third-party libraries whose frames pyinstrument
# leaves without the site-packages/ prefix. MySQLdb / pymysql /
# requests etc. These aren't Frappe apps — the user can't optimize
# them. Already filtered by the donut's _top_level_app but wasn't
# filtered here, so they leaked into the Repeated Hot Frame
# leaderboard.
_THIRD_PARTY_LIB_SEGMENTS = frozenset({
	"MySQLdb", "pymysql", "psycopg2", "requests", "urllib3",
	"httpx", "boto3", "botocore", "redis", "celery",
	"jinja2", "markupsafe", "bleach", "nh3",
	"pandas", "numpy", "openpyxl", "PIL",
})


def _is_pure_helper_frame(node: dict) -> bool:
	"""Narrower than ``_is_framework_frame``. Returns True only for pure
	plumbing helpers that users can't optimize. Keeps most of frappe/*
	so hot-frame findings remain useful when application-layer Frappe
	code (Document lifecycle, permissions, hooks, naming) is the actual
	bottleneck — and users who ARE Frappe contributors can see those as
	legitimate targets too.

	Used by the Repeated Hot Frame aggregator only. Do NOT use this for
	SQL-to-Python reconciliation or Slow Hot Path — those want the
	broader ``_is_framework_frame`` so SQL attributes blame to user
	code above the framework boundary.
	"""
	fn = node.get("function") or ""

	# v0.5.2: bare function names that are always decorator-wrapper
	# plumbing regardless of file. See _PURE_HELPER_FUNCTION_NAMES
	# for rationale.
	if fn in _PURE_HELPER_FUNCTION_NAMES:
		return True

	for prefix in _PURE_HELPER_FUNCTION_PREFIXES:
		if fn.startswith(prefix):
			return True

	# Angle-bracketed synthetic markers (<built-in>, <string>,
	# <module>, <frozen ...>). Pyinstrument emits these for C-level
	# builtins and compiled-string frames; they're never actionable.
	if fn.startswith("<") and fn.endswith(">"):
		return True
	# v0.5.2: square-bracketed pruning markers ([other: N frames],
	# [omitted: N frames]). These are synthetic nodes the pruner
	# inserts to represent frames that got rolled up during
	# _prune / _soft_cap. They're summaries, not real functions.
	if fn.startswith("["):
		return True

	filename = (node.get("filename") or "").replace("\\", "/")
	if not filename:
		return False

	# Specific files by suffix (works on absolute and relative paths).
	for suffix in _PURE_HELPER_PATH_SUFFIXES:
		if filename.endswith(suffix):
			return True

	# Whole directories by substring.
	for frag in _PURE_HELPER_PATH_SUBSTRINGS:
		if frag in filename:
			return True

	# v0.5.2: synthetic filenames (pyinstrument's <built-in>,
	# <string>) and Python stdlib (single-segment filenames like
	# inspect.py, functools.py, threading.py). A real production
	# report had inspect.py::getouterframes and inspect.py::
	# getframeinfo showing up in the top hot frames because those
	# are called by pyinstrument's own frame-capture path and
	# pyinstrument short-paths stdlib filenames to just the
	# module name.
	stripped = filename.lstrip("/")
	if stripped.startswith("<") and stripped.endswith(">"):
		return True
	if "/" not in stripped:
		# No slash → Python stdlib module or loose script, never
		# a Frappe app.
		return True

	# v0.5.2: third-party libraries by first-segment. pyinstrument
	# sometimes strips the site-packages/ prefix so MySQLdb/cursors.py
	# arrives without any directory context. Catch by name against
	# a frozen set of common libs used in Frappe benches.
	first_segment = stripped.split("/", 1)[0]
	if first_segment in _THIRD_PARTY_LIB_SEGMENTS:
		return True

	return False


def _is_walker_plumbing_frame(node: dict) -> bool:
	"""Walker-specific plumbing predicate. ``True`` when ``_walk_for_findings``
	should descend past this node without emitting a finding on it.

	Union of ``_is_framework_frame`` (frappe/*, optimus/*) and
	``_is_pure_helper_frame`` (RQ wrappers, werkzeug/gunicorn, decorator
	shims, stdlib singletons), with one carve-out: **Server Script bodies
	are not plumbing**. Server Scripts get exec'd through pyinstrument with
	``function=""`` and a synthetic ``<serverscript-N>`` filename, which
	``_is_pure_helper_frame``'s angle-bracket-filename rule classifies as
	plumbing for the Repeated Hot Frame aggregator (one-off scripts don't
	aggregate). For the Slow Hot Path walker, they are legitimate
	user-actionable hot frames.
	"""
	if _is_framework_frame(node):
		return True
	filename = (node.get("filename") or "").replace("\\", "/").lstrip("/")
	# Server Script bodies → user code for the walker, not plumbing.
	if filename.startswith("<serverscript-"):
		return False
	return _is_pure_helper_frame(node)


def _frames_match(node: dict, frame: dict) -> bool:
	"""Fuzzy match: same function name, ignoring filename prefix differences."""
	return (node.get("function") or "") == (frame.get("function") or "")


def _find_graft_point(root: dict, stack: list):
	"""Walk a recorder stack against the pyi tree; return (graft_node, partial).

	The graft node is the deepest node that *matches* a frame in the
	recorder stack. If matching descends past framework frames, the graft
	point is rolled back to the deepest non-framework ancestor.

	Returns (graft_point, partial_match: bool) where partial_match is
	True if the stack had more frames than the tree could match.
	"""
	if not stack:
		return root, False

	current = root
	matched_user_node = root
	last_matched_index = -1

	for i, frame in enumerate(stack):
		# Look for a child of `current` that matches this frame
		next_child = None
		for child in current.get("children", []):
			if _frames_match(child, frame):
				next_child = child
				break
		if next_child is None:
			# No further match — stop descending
			break
		current = next_child
		last_matched_index = i
		# Track the deepest non-framework match — that's where we graft.
		if not _is_framework_frame(current):
			matched_user_node = current

	# partial_match is True if the recorder stack had unconsumed frames
	partial = last_matched_index < (len(stack) - 1)

	# If the only matches were framework frames, fall back to root
	# (matched_user_node remains root in that case).
	return matched_user_node, partial


def _coalesce_sql_siblings(node: dict) -> None:
	"""Recursively merge identical SQL leaf siblings under each parent.

	Coalescing key: (query_normalized, partial_match). Coalesced nodes
	get an `is_n_plus_one_hint=True` flag the renderer uses to show
	`<sql> ×N` instead of N separate rows. The n_plus_one analyzer still
	emits its own finding — this is just a renderer hint.
	"""
	children = node.get("children") or []
	if not children:
		return

	# Bucket SQL children by (normalized_query, partial_match)
	merged: dict = {}
	non_sql: list = []
	for child in children:
		if child.get("kind") == "sql":
			key = (child.get("query_normalized", ""), child.get("partial_match", False))
			if key in merged:
				existing = merged[key]
				existing["self_ms"] += child.get("self_ms", 0)
				existing["cumulative_ms"] += child.get("cumulative_ms", 0)
				existing["query_count"] += child.get("query_count", 1)
				existing["is_n_plus_one_hint"] = True
			else:
				# Shallow copy so we don't mutate the original
				merged[key] = dict(child)
		else:
			non_sql.append(child)

	# Recurse into non-SQL children
	for child in non_sql:
		_coalesce_sql_siblings(child)

	node["children"] = non_sql + list(merged.values())


def reconcile(pyi_session_or_dict, sql_calls: list, action_wall_time_ms: float) -> dict:
	"""Build a unified call tree from a pyinstrument session + SQL calls.

	This is the main reconciliation entry point. Returns a dict tree with
	SQL leaves grafted at the deepest user-code ancestor of each call's
	stack, identical SQL siblings coalesced, and partial-match flags set
	where the recorder stack ran past the pyi tree's visible depth.

	Time-accounting invariant: pyinstrument has already counted SQL time
	in every Python ancestor's cumulative_ms. We DO NOT subtract — the
	leaf's self_ms is informational only. The renderer always reads
	cumulative_ms from the node itself, never sums children.
	"""
	tree = _pyi_to_dict_tree(pyi_session_or_dict)

	for call in sql_calls or []:
		stack = call.get("stack") or []
		graft_point, partial = _find_graft_point(tree, stack)
		leaf = _make_sql_leaf(call, partial_match=partial)
		graft_point.setdefault("children", []).append(leaf)

	_coalesce_sql_siblings(tree)
	return tree


# ---------------------------------------------------------------------------
# Pruning and soft cap (Task 17)
# ---------------------------------------------------------------------------


DEFAULT_PRUNE_THRESHOLD_PCT = 0.005
DEFAULT_TREE_NODE_CAP = 500


def _prune(tree: dict, action_wall_time_ms: float, threshold_pct: float) -> dict:
	"""Drop nodes whose cumulative_ms is below the threshold.

	Pruned siblings under the same parent merge into one
	`[other: N frames]` placeholder with summed time. Operates in-place
	and also returns the tree for chaining.
	"""
	threshold = max(2.0, action_wall_time_ms * threshold_pct)
	_prune_recursive(tree, threshold)
	return tree


def _prune_recursive(node: dict, threshold: float) -> None:
	children = node.get("children", [])
	kept = []
	dropped_count = 0
	dropped_self_ms = 0.0
	dropped_cumulative_ms = 0.0
	for child in children:
		# Never prune SQL leaves — they're always meaningful
		if child.get("kind") == "sql":
			kept.append(child)
			continue
		if child.get("cumulative_ms", 0) < threshold:
			dropped_count += 1
			dropped_self_ms += child.get("self_ms", 0)
			dropped_cumulative_ms += child.get("cumulative_ms", 0)
			continue
		_prune_recursive(child, threshold)
		kept.append(child)
	if dropped_count:
		kept.append({
			"function": f"[other: {dropped_count} frames]",
			"filename": "",
			"lineno": 0,
			"self_ms": dropped_self_ms,
			"cumulative_ms": dropped_cumulative_ms,
			"kind": "python",
			"children": [],
		})
	node["children"] = kept


def _soft_cap_nodes(tree: dict, max_nodes: int) -> dict:
	"""If the tree has more than max_nodes, drop cold siblings to fit.

	Walks depth-first, always descending the highest-cumulative child
	first. Counts every node visited. Once the count reaches max_nodes,
	any unvisited children are replaced with a single `[N more frames omitted]`
	placeholder. The hot path is always preserved.

	Returns the tree (mutated in place).
	"""
	state = {"count": 0}
	_soft_cap_recursive(tree, max_nodes, state)
	return tree


def _soft_cap_recursive(node: dict, max_nodes: int, state: dict) -> None:
	state["count"] += 1
	children = sorted(
		node.get("children", []),
		key=lambda c: c.get("cumulative_ms", 0),
		reverse=True,
	)
	kept = []
	omitted_count = 0
	omitted_ms = 0.0
	for child in children:
		if state["count"] >= max_nodes:
			omitted_count += 1
			omitted_ms += child.get("cumulative_ms", 0)
			continue
		_soft_cap_recursive(child, max_nodes, state)
		kept.append(child)
	if omitted_count:
		kept.append({
			"function": f"[{omitted_count} more frames omitted]",
			"filename": "",
			"lineno": 0,
			"self_ms": 0.0,
			"cumulative_ms": omitted_ms,
			"kind": "python",
			"children": [],
		})
	node["children"] = kept


# ---------------------------------------------------------------------------
# Per-action findings (Task 18)
# ---------------------------------------------------------------------------


# v0.6.0 Round 6: previously-hardcoded thresholds for Slow Hot Path /
# Hook Bottleneck. The "Medium qualifying" pair (PCT + MS) is now read
# from Optimus Settings; the "High severity" pair stays as a multiplier
# of the Medium pair so a single configured threshold drives both.
DEFAULT_HOT_PATH_PCT = 0.25  # fallback when settings unreachable
DEFAULT_HOT_PATH_MS = 200
HOT_PATH_HIGH_PCT_MULTIPLIER = 2.0     # 0.50 / 0.25 — preserves legacy ratio
HOT_PATH_HIGH_MS_MULTIPLIER = 2.5      # 500 / 200
# v0.7.x: absolute-impact escape hatch. A subtree consuming >= this many
# multiples of high_ms is High regardless of relative pct. Picked at 2.0
# (≥1s at default) so a 1.4s subtree at 49% of a 3s action gets the
# severity its absolute impact deserves — without this, the TL;DR
# headline picks smaller High findings over larger Medium ones.
HOT_PATH_HIGH_ABS_MULTIPLIER = 2.0     # 2× high_ms → 1000ms by default
SQL_DOMINANCE_SUPPRESSION_PCT = 0.80


def _resolve_hot_path_thresholds() -> tuple[float, float, float, float]:
	"""Return (med_pct, med_ms, high_pct, high_ms). Reads Profiler
	Settings via the cached config. Falls back to legacy constants
	when settings are unreachable (pure-test path)."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
		# Settings store the percentage as a 0-100 number for UX; the
		# analyzer compares against fractional. Divide by 100 here so
		# downstream comparisons stay as-is.
		med_pct = float(cfg.slow_hot_path_pct_threshold or 25.0) / 100.0
		med_ms = float(cfg.slow_hot_path_min_ms or DEFAULT_HOT_PATH_MS)
	except Exception:
		med_pct = DEFAULT_HOT_PATH_PCT
		med_ms = float(DEFAULT_HOT_PATH_MS)
	return (
		med_pct,
		med_ms,
		med_pct * HOT_PATH_HIGH_PCT_MULTIPLIER,
		med_ms * HOT_PATH_HIGH_MS_MULTIPLIER,
	)


def _largest_sql_child(node: dict):
	"""Return the largest SQL leaf among direct children, or None."""
	sql_children = [c for c in node.get("children", []) if c.get("kind") == "sql"]
	if not sql_children:
		return None
	return max(sql_children, key=lambda c: c.get("cumulative_ms", 0))


# v0.5.3: Uninformative function names pyinstrument emits for dynamically
# executed code. A Server Script body, a `exec()`'d snippet, a module's
# top level, or a list/generator/dict-comp with no function context all
# show up as one of these synthetic names — and plugging them directly
# into a finding title gives nonsensical output like
# "spent in <module>" or "spent in " (empty).
_UNINFORMATIVE_FUNCTION_NAMES = frozenset({
	"",
	"<module>",
	"<lambda>",
	"<listcomp>",
	"<genexpr>",
	"<dictcomp>",
	"<setcomp>",
	"<unknown>",
	"<string>",
})


def _display_name_for_node(node: dict) -> str:
	"""Return a human-readable name for a call-tree node's frame.

	Preference order:

	  1. ``node["function"]`` if it's a real name (not in the
	     uninformative list).
	  2. ``short_filename(filename):lineno`` when we can derive a
	     file location — useful for module-scope or ``<module>``
	     frames so the title reads "spent in myapp/foo.py:42".
	  3. A type-aware label based on what the synthetic name
	     suggests — e.g. a Server Script body becomes
	     "<server-script body>". Better than pass-through because
	     the report reader immediately understands what's slow.
	  4. "<unnamed code>" as last resort.

	Production trigger: a user put a 5M-iteration CPU loop inside a
	Frappe Server Script. pyinstrument recorded the exec()'d body
	with function="" and filename="<serverscript-N>", so the
	finding title rendered as "spent in " (trailing blank). Now it
	renders "spent in <server-script body>".
	"""
	raw_fn = (node.get("function") or "").strip()
	if raw_fn and raw_fn not in _UNINFORMATIVE_FUNCTION_NAMES:
		return raw_fn

	filename = (node.get("filename") or "").strip()
	lineno = node.get("lineno")

	# Detect Server Scripts: Frappe's safe_exec compiles with filename
	# "<serverscript>" (bare) or "<serverscript>: <scrubbed-name>". Surface
	# the script name + lineno so the user can locate the issue — the
	# v0.7.x+ renderer also links these callsites to the Desk Server Script
	# form (see optimus/server_script_source.py).
	if filename.startswith("<serverscript") or filename.startswith("<server-script"):
		from optimus.server_script_source import extract_script_name

		_name = extract_script_name(filename)
		if _name:
			return f"<server-script: {_name}>:{lineno}" if lineno else f"<server-script: {_name}>"
		return "<server-script body>"
	if filename == "<string>":
		return "<exec'd code>"

	# Module-scope / lambda / comprehension with a real filename →
	# use the short path for navigation.
	if filename and filename not in ("<unknown>", "<string>", "?"):
		short = short_filename(filename)
		if lineno:
			return f"{short}:{lineno}"
		return short

	# Last resort: pass through whatever synthetic name pyinstrument
	# gave us rather than go blank. `<module>` tells the reader the
	# hot spot is at module top-level; `<lambda>` is a lambda; etc.
	if raw_fn:
		return raw_fn
	return "<unnamed code>"


def _first_user_code_descendant(
	node: dict, threshold_ms: float
) -> dict | None:
	"""Deepest non-plumbing descendant with cumulative ≥ threshold, or None.

	Walks depth-first so the *deepest* hot user-code frame wins over an
	enclosing wrapper that just relays its work. Skips frames classified
	as framework OR pure-helper plumbing — same predicates the walker
	uses to descend past them.

	Used by the BG-job fallback when the regular Slow Hot Path walker
	emits no finding for a slow job (every frame is plumbing, or the
	user-code frame is below the percentage threshold).
	"""
	candidate = None
	fn = node.get("function") or ""
	if (
		node.get("kind") == "python"
		and fn
		and fn != "<root>"
		and not fn.startswith("[")
		and not _is_walker_plumbing_frame(node)
		and (node.get("cumulative_ms") or 0) >= threshold_ms
	):
		candidate = node
	for c in node.get("children", []) or []:
		if c.get("kind") != "python":
			continue
		deeper = _first_user_code_descendant(c, threshold_ms)
		if deeper is not None:
			candidate = deeper
	return candidate


def _emit_per_action_findings(
	tree: dict,
	action_idx: int,
	action_label: str,
	action_wall_time_ms: float,
) -> list:
	"""Walk the tree and emit Slow Hot Path / Hook Bottleneck findings.

	One finding per qualifying subtree. F3 (Hook Bottleneck) takes
	precedence over F1 (Slow Hot Path). F1 is suppressed when a single
	SQL leaf dominates the subtree.

	For BG jobs (action_label starts with "RQ Job: ") that don't produce
	any per-frame finding but ARE slow, emit a Slow Background Job
	finding rooted at the deepest user-code frame — so the reader has an
	actionable callsite instead of an unexplained 12s job entry.
	"""
	if action_wall_time_ms <= 0:
		return []

	findings = []
	_walk_for_findings(
		tree,
		parent_chain=[],
		action_idx=action_idx,
		action_label=action_label,
		action_wall_time_ms=action_wall_time_ms,
		findings=findings,
	)

	# v0.7.x: BG-job fallback. When the walker emits nothing for a slow
	# job, surface the deepest non-plumbing frame so the finding card
	# still carries an actionable callsite. Gated on the RQ Job label so
	# request actions that simply produce no findings (legitimate fast
	# responses) stay quiet.
	if not findings and action_label.startswith("RQ Job: "):
		med_pct, med_ms, _hi_pct, _hi_ms = _resolve_hot_path_thresholds()
		if action_wall_time_ms >= med_ms:
			deepest = _first_user_code_descendant(tree, med_ms)
			if deepest is not None:
				short_label = action_label[len("RQ Job: "):]
				fn_name = _display_name_for_node(deepest)
				cumulative = deepest.get("cumulative_ms", 0) or 0
				impact_ms = min(cumulative, action_wall_time_ms)
				pct_of_action = min(
					1.0, cumulative / action_wall_time_ms
				) if action_wall_time_ms else 0
				pct_str = f"{pct_of_action * 100:.0f}%"
				severity = "High" if impact_ms >= _hi_ms else "Medium"
				findings.append({
					"finding_type": "Slow Background Job",
					"severity": severity,
					"title": (
						f"In job {short_label}, {fn_name} is the deepest "
						f"hot frame ({impact_ms:.0f}ms, {pct_str} of the job)"
					),
					"customer_description": (
						f"The background job *{short_label}* ran for "
						f"{action_wall_time_ms:.0f}ms but no single subtree "
						f"crossed the Slow Hot Path threshold. **{fn_name}** "
						f"is the deepest user-code frame and accounts for "
						f"{impact_ms:.0f}ms ({pct_str} of the job). Start "
						"there — open the Line-Level Drilldown for a "
						"statement-level breakdown of where the time goes."
					),
					"technical_detail_json": json.dumps({
						"function": fn_name,
						"filename": deepest.get("filename"),
						"lineno": deepest.get("lineno"),
						"cumulative_ms": cumulative,
						"action_wall_time_ms": action_wall_time_ms,
						"is_bg_job_fallback": True,
					}, default=str),
					"estimated_impact_ms": round(impact_ms, 2),
					"affected_count": 1,
					"action_ref": str(action_idx),
				})
	return findings


def _walk_for_findings(
	node: dict,
	parent_chain: list,
	action_idx: int,
	action_label: str,
	action_wall_time_ms: float,
	findings: list,
) -> None:
	cumulative = node.get("cumulative_ms", 0)
	# v0.7.x A.CR1: clamp pct at the gating + severity site too. The
	# raw cumulative_ms from pyinstrument's aggregated tree can sum
	# across actions and produce pct > 100% (impossible per-action),
	# which would tier-promote a Medium finding to High purely
	# because of cross-action aggregation. The absolute-ms gate
	# (cumulative >= med_ms / high_ms) still uses raw cumulative —
	# the user *did* spend that much time across actions; we only
	# refuse to inflate the *percentage* tier.
	raw_pct = cumulative / action_wall_time_ms if action_wall_time_ms else 0
	pct_of_action = min(1.0, raw_pct)

	# v0.6.0 Round 6: thresholds now read from Optimus Settings (via a
	# cached resolver) instead of being module constants.
	med_pct, med_ms, high_pct, high_ms = _resolve_hot_path_thresholds()

	# Qualifies as a hot subtree?
	# Skip framework frames (frappe.* / optimus.*) so we descend
	# through them and emit the finding on the user-code or hook frame
	# inside. Without this, Document.run_method itself would qualify and
	# we'd emit a generic Slow Hot Path on it instead of the actual hook.
	qualifies = (
		pct_of_action >= med_pct
		and cumulative >= med_ms
		and node.get("kind") == "python"
		and not (node.get("function") or "").startswith("[")  # skip [other] / [omitted]
		and node.get("function") != "<root>"
		# v0.7.x: descend past framework AND pure-helper plumbing (RQ
		# worker wrappers, werkzeug/gunicorn dispatch, decorator shims).
		# _is_framework_frame alone only covers frappe/*/optimus/*; the
		# walker also needs to skip /rq/ and bare wrapper function
		# names. _is_walker_plumbing_frame is the union with a
		# Server-Script carve-out (those have synthetic filenames but
		# are user code).
		and not _is_walker_plumbing_frame(node)
	)

	if qualifies:
		# SQL-dominated suppression check
		largest_sql = _largest_sql_child(node)
		sql_dominates = (
			largest_sql is not None
			and (largest_sql.get("cumulative_ms", 0) / cumulative) >= SQL_DOMINANCE_SUPPRESSION_PCT
		)
		if not sql_dominates:
			# Hook detection: any ancestor frame is Document.run_method?
			is_hook = any(
				"Document.run_method" in (p.get("function") or "")
				for p in parent_chain
			)
			severity = (
				"High" if (
					# Relative + absolute dominance (the original rule):
					# subtree owns ≥ high_pct of the action AND ≥ high_ms.
					(pct_of_action >= high_pct and cumulative >= high_ms)
					# OR overwhelming absolute impact: a ≥ 2× high_ms
					# subtree is High regardless of pct_of_action. Without
					# this, a 1.4s subtree at 49% of a 3s action lands as
					# Medium and silently loses TL;DR + Action-plan
					# rankings to smaller High findings. Users care more
					# about the 1.4s code path than a 0.6s "High" one.
					or cumulative >= HOT_PATH_HIGH_ABS_MULTIPLIER * high_ms
				)
				else "Medium"
			)
			# pct_of_action is already clamped at the top of the
			# function (A.CR1) so display_pct is just an alias.
			pct_str = f"{pct_of_action * 100:.0f}%"
			fn_name = _display_name_for_node(node)
			# v0.7.x: BG-job action_label carries an "RQ Job: " prefix
			# for disambiguation in the per-action table. Inside a
			# finding title that already says "In <label>, …", the
			# prefix reads as boilerplate — strip it to "In job
			# <short>" so the hotspot is the visual anchor, not the
			# wrapper noise.
			if action_label.startswith("RQ Job: "):
				_label_for_title = "job " + action_label[len("RQ Job: "):]
			else:
				_label_for_title = action_label
			impact_ms = (
				min(cumulative, action_wall_time_ms)
				if action_wall_time_ms else cumulative
			)
			# v0.7.x U4: intra-action repetition count. Count peer
			# children of the parent that share this node's function
			# name. Pyinstrument's aggregator merges trivially-identical
			# siblings, but distinct call sites stay separate nodes;
			# sharing function name across them means this function was
			# hit from N call sites in one action — useful signal for
			# "called from a loop" diagnoses.
			parent = parent_chain[-1] if parent_chain else None
			intra_action_repetitions = (
				sum(
					1 for c in (parent.get("children") or [])
					if (c.get("function") or "") == (node.get("function") or "")
				)
				if parent
				else 1
			)

			if is_hook:
				findings.append({
					"finding_type": "Hook Bottleneck",
					"severity": severity,
					"title": f"In {_label_for_title}, the {fn_name} hook consumed {impact_ms:.0f}ms",
					"customer_description": (
						f"During *{_label_for_title}*, the **{fn_name}** doc-event hook "
						f"consumed {pct_str} of its wall time ({impact_ms:.0f}ms). "
						"Hook functions run on every save/submit — optimizing this "
						"would speed up every similar action across your site."
					),
					"technical_detail_json": json.dumps({
						"function": fn_name,
						"filename": node.get("filename"),
						"lineno": node.get("lineno"),
						"cumulative_ms": cumulative,
						"action_wall_time_ms": action_wall_time_ms,
						"is_hook": True,
						"intra_action_repetitions": intra_action_repetitions,
					}, default=str),
					"estimated_impact_ms": round(impact_ms, 2),
					"affected_count": 1,
					"action_ref": str(action_idx),
				})
			else:
				# v0.5.2 round 5: when the action label itself ends
				# with the function name (e.g. action_label =
				# "erpnext.accounts.doctype.pricing_rule.pricing_rule
				# .apply_pricing_rule" and fn_name = "apply_pricing_rule"),
				# the classic phrasing "In X, N% of time was spent in
				# X" reads as tautology. The real signal is that the
				# function's own body is the hot spot — not a subcall
				# — so surface THAT. User-reported on a real Sales
				# Invoice Submit report.
				self_referential = (
					action_label == fn_name
					or action_label.endswith(f".{fn_name}")
					or action_label.endswith(f":{fn_name}")
				)
				if self_referential:
					title = (
						f"{fn_name} is a self-time hot path "
						f"({pct_str} of the action, {impact_ms:.0f}ms)"
					)
					desc = (
						f"The body of **{fn_name}** itself consumed "
						f"{pct_str} of the action time ({impact_ms:.0f}ms). "
						"This isn't a slow subcall — the function's own logic "
						"is the bottleneck. To pinpoint the exact hot line, "
						"run a **Line-Level Drilldown** (the *Run Line-Profile "
						"Pass* button on the session) and pick this function "
						"from the candidates — that profiles tight loops, "
						"expensive computations, and string/regex work at the "
						"statement level so you can optimize or cache them."
						"\n\nNote: self-time here is wall-clock (the sampler "
						"measures elapsed time, not CPU). If this function "
						"blocks on I/O — `requests.get`, sync DB calls "
						"outside the recorder, `time.sleep` — that wait "
						"counts as self-time. Check the call tree for "
						"hidden I/O before optimising compute."
					)
				else:
					title = (
						f"In {_label_for_title}, {pct_str} of its wall time "
						f"was spent in {fn_name}"
					)
					desc = (
						f"During *{_label_for_title}*, **{fn_name}** and its "
						f"callees consumed {pct_str} of the action's wall "
						f"time ({impact_ms:.0f}ms). This is the highest-impact "
						"code path for this action."
					)
				findings.append({
					"finding_type": "Slow Hot Path",
					"severity": severity,
					"title": title,
					"customer_description": desc,
					"technical_detail_json": json.dumps({
						"function": fn_name,
						"filename": node.get("filename"),
						"lineno": node.get("lineno"),
						"cumulative_ms": cumulative,
						"action_wall_time_ms": action_wall_time_ms,
						"is_hook": False,
						"is_self_referential": self_referential,
						"intra_action_repetitions": intra_action_repetitions,
					}, default=str),
					"estimated_impact_ms": round(impact_ms, 2),
					"affected_count": 1,
					"action_ref": str(action_idx),
				})
			# When we emit a finding for this node, do NOT recurse into
			# its children — children are already represented in the
			# subtree's cumulative_ms, and recursing would create
			# overlapping nested findings.
			return

	# Recurse into children
	new_chain = parent_chain + [node]
	for child in node.get("children", []):
		if child.get("kind") == "sql":
			continue
		_walk_for_findings(
			child, new_chain, action_idx, action_label, action_wall_time_ms, findings,
		)


# ---------------------------------------------------------------------------
# Cross-action aggregation + leaderboard (Task 19)
# ---------------------------------------------------------------------------


DEFAULT_REPEATED_FRAME_MIN_ACTIONS = 3
DEFAULT_REPEATED_FRAME_MIN_TOTAL_MS = 500
HOT_FRAMES_LEADERBOARD_SIZE = 20
HOT_FRAMES_INTERMEDIATE_CAP = 1000


def _redacted_module_key(function: str, filename: str = "") -> str | None:
	"""Build the dedup key for cross-action aggregation.

	v0.5.1: includes the filename so different functions that share a
	name (e.g. 35 different ``wrapper`` decorators across unrelated
	modules, or 20 different ``handle`` methods) don't all collapse
	into one bucket. Pre-v0.5.1 used the bare function name, which
	produced misleading 'Repeated Hot Frame' findings blaming generic
	decorator wrappers that the user cannot optimize — every functools
	wrapper, werkzeug wrapper, and frappe.whitelist wrapper in the
	session would roll up under a single 'wrapper' key.

	Returns None for synthetic / skipped nodes.
	"""
	if not function or function.startswith("[") or function == "<root>" or function == "<sql>":
		return None

	# Compact filename: keep the last 2 path segments so the key stays
	# readable (e.g. "erpnext/accounts/sales_invoice.py") without
	# leaking absolute paths from the worker's filesystem.
	if filename:
		short = (filename or "").replace("\\", "/")
		parts = [p for p in short.split("/") if p]
		if len(parts) > 2:
			short = "/".join(parts[-2:])
		else:
			short = "/".join(parts)
		if short:
			return f"{short}::{function}"

	return function


def _aggregate_hot_frames(per_action_trees: list):
	"""Build the cross-action hot frames map → (findings, leaderboard).

	Returns:
	  findings   — list of Repeated Hot Frame findings (session-wide,
	               no action_ref)
	  leaderboard — list of top-N dicts for hot_frames_json
	"""
	# {function_key: [(action_idx, self_ms, cumulative_ms), ...]}
	occurrences: dict = defaultdict(list)

	for action_idx, tree in enumerate(per_action_trees):
		_walk_for_aggregation(tree, action_idx, occurrences)
		# Cap intermediate map size to bound memory.
		if len(occurrences) > HOT_FRAMES_INTERMEDIATE_CAP * 2:
			by_total = sorted(
				occurrences.items(),
				key=lambda kv: sum(self_ms for _, self_ms, _ in kv[1]),
				reverse=True,
			)[:HOT_FRAMES_INTERMEDIATE_CAP]
			occurrences = defaultdict(list, dict(by_total))

	findings = []
	leaderboard_rows = []

	for key, occs in occurrences.items():
		distinct_actions = {idx for idx, _, _ in occs}
		total_self_ms = sum(self_ms for _, self_ms, _ in occs)
		total_cum_ms = sum(cum_ms for _, _, cum_ms in occs)
		row = {
			"function": key,
			# A.AE1: self-sum is the precise "time in this function
			# across the whole tree" metric — what the user-app
			# variant displays.
			"total_ms": round(total_self_ms, 2),
			# Cumulative-sum — what the framework variant displays.
			# See _walk_for_aggregation's comment for the why.
			"total_cumulative_ms": round(total_cum_ms, 2),
			"occurrences": len(occs),
			"distinct_actions": len(distinct_actions),
			"action_refs": sorted(distinct_actions),
		}
		leaderboard_rows.append(row)
		total_ms = total_self_ms  # alias for downstream Repeated Hot Frame logic

		if (
			len(distinct_actions) >= DEFAULT_REPEATED_FRAME_MIN_ACTIONS
			and total_ms >= DEFAULT_REPEATED_FRAME_MIN_TOTAL_MS
		):
			severity = "High" if total_ms >= 2000 else "Medium"
			findings.append({
				"finding_type": "Repeated Hot Frame",
				"severity": severity,
				"title": (
					f"{key} appeared in {len(distinct_actions)} actions "
					f"and consumed {total_ms:.0f}ms total"
				),
				"customer_description": (
					f"The function **{key}** ran across {len(distinct_actions)} "
					f"different actions in this session, consuming {total_ms:.0f}ms "
					"in total. Optimizing it would help every flow that touches it."
				),
				"technical_detail_json": json.dumps({
					"function": key,
					"total_ms": round(total_ms, 2),
					"distinct_actions": len(distinct_actions),
					"action_refs": sorted(distinct_actions),
				}, default=str),
				"estimated_impact_ms": round(total_ms, 2),
				"affected_count": len(occs),
				"action_ref": None,
			})

	leaderboard_rows.sort(key=lambda r: r["total_ms"], reverse=True)
	leaderboard = leaderboard_rows[:HOT_FRAMES_LEADERBOARD_SIZE]

	return findings, leaderboard


def _walk_for_aggregation(node: dict, action_idx: int, occurrences: dict) -> None:
	if node.get("kind") == "python":
		# v0.5.1: skip PURE HELPER frames only, not all framework code.
		# This is the narrower _is_pure_helper_frame check — we want to
		# keep Document.run_method, permission checks, naming, and other
		# application-layer Frappe code in the leaderboard because they
		# can be legitimate optimization targets (e.g. a slow doc-event
		# hook bubbles up as Document.run_method). Only frappe/utils/*,
		# frappe/handler.py, frappe/app.py, and infrastructure libs
		# (werkzeug, rq, etc.) are suppressed.
		#
		# An earlier v0.5.1 draft used the broader _is_framework_frame
		# here, which hid ALL frappe/* frames — that was too aggressive
		# and blinded the analyzer to real application-layer bottlenecks
		# inside Frappe that users could act on.
		if not _is_pure_helper_frame(node):
			key = _redacted_module_key(
				node.get("function", ""),
				node.get("filename", ""),
			)
			if key:
				# v0.7.x A.AE1: track self_ms (immune to recursion
				# double-counting) for the user-app variant — that's
				# the exact "time in this function's body across the
				# whole action's tree" answer.
				#
				# v0.7.x: also track cumulative_ms because the
				# framework variant of the hot-frames table needs it.
				# Framework wrapper frames have sub-sampler-interval
				# self_ms (rounds to 0); aggregated → 0 across rows.
				# The framework column displays cumulative_ms instead
				# (renderer.build_hot_frames_table chooses per
				# variant). Risk: nested same-name framework frames
				# would double-count cumulative — acceptable on the
				# collapsed-by-default framework table, where the
				# column is informational rather than decision-grade.
				occurrences[key].append(
					(action_idx, node.get("self_ms", 0), node.get("cumulative_ms", 0))
				)
	for child in node.get("children", []):
		_walk_for_aggregation(child, action_idx, occurrences)


# ---------------------------------------------------------------------------
# Session-wide donut breakdown + analyzer entry point (Task 20)
# ---------------------------------------------------------------------------


def _strip_profiler_frames(node: dict) -> dict:
	"""Recursively remove optimus/* frames from the tree.

	When a profiler frame is found, its CHILDREN are grafted up to the
	parent in place of the profiler frame itself. This preserves any
	user-code subtree that happens to be under a profiler frame
	(pathological but theoretically possible) while removing the
	profiler frame from view.

	Context: a production report on a fast request
	(GET /api/method/frappe.realtime.can_subscribe_doctype, 47 ms)
	showed its call tree as

	    application  (47 ms)
	     └─ init_request  (31 ms)
	         └─ call  (31 ms)
	             └─ before_request  (31 ms)   ← optimus
	                 └─ snapshot  (31 ms)     ← optimus
	                     └─ _read_db  (13 ms) ← optimus

	67% of the action's time was attributed to the profiler's own
	infra snapshot. The root cause was _start_pyi_session being
	called BEFORE infra_capture.snapshot() in before_request — the
	snapshot was still on the call stack when pyinstrument began
	sampling. The primary fix (v0.5.1) reorders the hook so the
	snapshot happens first. This tree-strip pass is belt-and-
	suspenders: even with the correct ordering, pyi can still catch
	a single sample of ``before_request`` returning or a capture
	wrap frame during the action, and the stored tree should never
	show profiler frames regardless.

	Modifies the tree in place for performance (per-action trees
	can be hundreds of kilobytes) and returns the same node for
	chaining in the analyze pipeline.
	"""
	children = node.get("children") or []
	new_children: list = []
	for child in children:
		# Recurse first so grandchildren are rewritten bottom-up.
		_strip_profiler_frames(child)

		filename = (child.get("filename") or "").replace("\\", "/")
		if "optimus/" in filename:
			# Drop this node; promote its (already-rewritten) children
			# up to the current parent.
			new_children.extend(child.get("children") or [])
		else:
			new_children.append(child)

	node["children"] = new_children
	return node


def _top_level_app(function: str, filename: str) -> str:
	"""Return the top-level app name for bucketing the donut.

	v0.5.1: derived from the FILENAME's first path segment (not the
	function name), with additional filters to keep Python stdlib /
	synthetic / third-party frames from polluting the bucket list.

	A real production donut showed these noise buckets — all 0ms each:

	    Python (inspect.py)    — Python stdlib (single-segment filename)
	    Python (functools.py)  — Python stdlib
	    Python (MySQLdb)       — third-party, pyinstrument stripped
	                             "site-packages/" so the substring
	                             filter missed it
	    Python (<built-in>)    — pyinstrument synthetic for C builtins

	All four should collapse to the ``[other]`` catch-all. The rules
	that route them correctly:

	  1. Synthetic function names (``<root>``, ``<sql>``, ``<built-in>``,
	     ``<string>``, anything wrapped in angle brackets) → [other]
	  2. Synthetic filenames wrapped in angle brackets → [other]
	  3. Filenames containing ``site-packages/`` or ``dist-packages/``
	     (third-party libs) → [other]
	  4. Single-segment filenames (``inspect.py``, ``functools.py``) —
	     these are stdlib or loose scripts, not Frappe apps → [other]
	  5. ``apps/<name>/`` marker → ``<name>``
	  6. First path segment of a multi-segment filename, filtered to
	     actual installed Frappe apps via ``frappe.get_installed_apps()``
	     when available — otherwise accept the first segment (legacy
	     behavior, used by unit tests that can't import frappe)

	Handles the common pyinstrument path shapes:
	  - ``frappe/handler.py``                        → "frappe"
	  - ``optimus/capture.py``               → "optimus"
	  - ``erpnext/accounts/tax.py``                  → "erpnext"
	  - ``apps/erpnext/erpnext/foo.py``             → "erpnext"
	  - ``/Users/.../apps/frappe/frappe/handler.py`` → "frappe"
	  - ``env/lib/python3.14/site-packages/werkzeug/wsgi.py`` → "[other]"
	  - ``MySQLdb/connections.py``                   → "[other]"
	  - ``inspect.py``                               → "[other]"
	  - ``<built-in>``                               → "[other]"
	"""
	# Server Scripts: filename identifies the bucket even when the
	# function name is empty (``safe_exec`` compiles with function="").
	# Check before the function-name fallthroughs so Server Script
	# callsites roll up under a meaningful bucket instead of the
	# anonymous ``[other]``. Frappe writes the filename as
	# ``<serverscript>`` (bare) or ``<serverscript>: <scrubbed-name>``;
	# either form belongs here.
	_fn_norm = (filename or "").replace("\\", "/")
	if _fn_norm.startswith("<serverscript") or _fn_norm.startswith("<server-script"):
		return "Server Scripts"

	# Synthetic markers from the tree normalizer
	if not function or function in ("<root>", "<sql>") or function.startswith("["):
		return "[other]"
	# Angle-bracketed function names from pyinstrument: <built-in>,
	# <string>, <module>, <frozen importlib._bootstrap>, etc.
	if function.startswith("<") and function.endswith(">"):
		return "[other]"

	if not filename:
		return "[other]"

	norm = filename.replace("\\", "/")

	# Angle-bracketed synthetic filenames
	if norm.startswith("<") and norm.endswith(">"):
		return "[other]"

	# Third-party libraries live under site-packages / dist-packages
	# (caught when pyinstrument left the prefix intact).
	if "site-packages/" in norm or "dist-packages/" in norm:
		return "[other]"

	# Single-segment filenames like ``inspect.py`` or ``functools.py``
	# are Python stdlib or top-level scripts, NOT Frappe apps.
	stripped = norm.lstrip("/")
	if "/" not in stripped:
		return "[other]"

	# Bench layout: look for "apps/<name>/" anywhere in the path so
	# both relative (``apps/frappe/handler.py``) and absolute
	# (``/Users/.../apps/frappe/frappe/handler.py``) layouts resolve
	# to "frappe" rather than "Users" or some prefix.
	idx = norm.find("apps/")
	if idx >= 0:
		after = norm[idx + len("apps/"):]
		name = after.split("/", 1)[0]
		if name:
			return name

	# Pyinstrument's ``file_path_short`` usually strips bench prefixes
	# already, leaving something like ``frappe/handler.py``. Take the
	# first path segment as the app name — but only if it looks like
	# an actually installed Frappe app. Third-party libs (``MySQLdb/
	# connections.py``) otherwise produce a first-segment bucket like
	# "MySQLdb" that has nothing to do with the user's code.
	parts = [p for p in stripped.split("/") if p]
	if not parts:
		return "[other]"
	first = parts[0]

	# Intersect with installed apps when we're running inside a real
	# Frappe site. When frappe isn't importable (unit tests on an
	# isolated machine) or get_installed_apps fails, fall back to
	# accepting any first-segment — the legacy behavior that keeps
	# the pre-v0.5.1 tests green without requiring a mocked frappe.
	try:
		import frappe
		installed = set(frappe.get_installed_apps() or [])
	except Exception:
		installed = None

	if installed is not None and installed and first not in installed:
		return "[other]"

	return first


def _build_session_breakdown(per_action_trees: list, sql_total_ms: float) -> dict:
	"""Compute the donut data: sql ms + python ms + per-app self ms."""
	by_app: dict = defaultdict(float)
	python_total = 0.0
	for tree in per_action_trees:
		_walk_for_breakdown(tree, by_app)
		python_total += _sum_self_python(tree)
	return {
		"sql_ms": round(sql_total_ms, 2),
		"python_ms": round(python_total, 2),
		"by_app": {k: round(v, 2) for k, v in by_app.items()},
	}


def _walk_for_breakdown(node: dict, by_app: dict) -> None:
	if node.get("kind") == "python" and node.get("function") not in ("<root>",):
		app = _top_level_app(node.get("function", ""), node.get("filename", ""))
		by_app[app] += node.get("self_ms", 0)
	for child in node.get("children", []):
		_walk_for_breakdown(child, by_app)


def _sum_self_python(node: dict) -> float:
	total = 0.0
	if node.get("kind") == "python" and node.get("function") != "<root>":
		total += node.get("self_ms", 0)
	for child in node.get("children", []):
		total += _sum_self_python(child)
	return total


def _conf_float(key: str, default: float) -> float:
	try:
		import frappe

		v = frappe.conf.get(key)
		if v is not None:
			return float(v)
	except Exception:
		pass
	return default


def _conf_int(key: str, default: int) -> int:
	try:
		import frappe

		v = frappe.conf.get(key)
		if v is not None:
			return int(v)
	except Exception:
		pass
	return default


def analyze(recordings: list, context) -> AnalyzerResult:
	"""call_tree analyzer entry point.

	For each recording with a pyi_session, reconcile + prune + cap the tree,
	emit per-action findings, and record the unified tree on the action.
	After all recordings are processed, emit cross-action Repeated Hot
	Frame findings and build the session-wide donut + leaderboard.
	"""
	findings: list = []
	aggregate: dict = {}
	per_action_trees: list = []
	sql_total_ms = 0.0

	# Threshold config (read once)
	prune_pct = _conf_float("optimus_tree_prune_threshold_pct", DEFAULT_PRUNE_THRESHOLD_PCT)
	node_cap = _conf_int("optimus_tree_node_cap", DEFAULT_TREE_NODE_CAP)

	for action_idx, recording in enumerate(recordings):
		# v0.7.x (M1): pop, don't get — call_tree is the ONLY consumer of the
		# raw pyinstrument Session, and holding all recordings' trees in RAM at
		# once was the dominant analyze-time memory spike (OOM on long flows).
		# Popping frees each tree as soon as it's reconciled, so peak drops from
		# "all trees at once" to "one at a time". Covers the pyi-is-None branch
		# below too. (Guarded by a test that no later analyzer reads pyi_session.)
		pyi = recording.pop("pyi_session", None)
		calls = recording.get("calls") or []
		# Track SQL time even if no pyi tree
		sql_total_ms += sum(c.get("duration", 0) for c in calls)

		# Action label / wall time from context if available, else fall back
		action_label = "?"
		action_wall_time = 0
		if action_idx < len(context.actions):
			a = context.actions[action_idx]
			action_label = a.get("action_label") or a.get("path") or "?"
			action_wall_time = a.get("duration_ms") or 0

		if pyi is None:
			per_action_trees.append({
				"function": "<root>", "children": [],
				"cumulative_ms": 0, "self_ms": 0, "kind": "python",
			})
			# Set null call_tree on the action — renderer falls back gracefully
			if action_idx < len(context.actions):
				context.actions[action_idx]["call_tree_json"] = None
			continue

		# Reconcile, prune, cap
		try:
			tree = reconcile(pyi, calls, action_wall_time)
			tree = _prune(tree, action_wall_time or 1, prune_pct)
			tree = _soft_cap_nodes(tree, node_cap)
			# v0.5.1: belt-and-suspenders strip of any residual
			# optimus/* frames that slipped through the hook
			# ordering fix. See _strip_profiler_frames docstring for
			# the full rationale — the short version is that even
			# with _start_pyi_session now running AFTER infra_capture
			# .snapshot(), pyinstrument can still sample a single
			# frame of before_request on its way out, or a capture
			# wrap frame during the action. Removing them from the
			# stored tree here ensures they never appear in the
			# report regardless of sampling luck.
			tree = _strip_profiler_frames(tree)
		except Exception:
			context.warnings.append(
				f"Reconciliation failed for action {action_idx} (see error log)"
			)
			tree = {
				"function": "<root>", "children": [],
				"cumulative_ms": 0, "self_ms": 0, "kind": "python",
			}

		per_action_trees.append(tree)

		# Per-action findings
		findings.extend(_emit_per_action_findings(
			tree,
			action_idx=action_idx,
			action_label=action_label,
			action_wall_time_ms=action_wall_time or tree.get("cumulative_ms", 0),
		))

		# Persist the tree as JSON on the action (overflow handling in
		# Task 23 happens later at _persist time)
		tree_json = json.dumps(tree, default=str)
		if action_idx < len(context.actions):
			context.actions[action_idx]["call_tree_json"] = tree_json
			context.actions[action_idx]["call_tree_size_bytes"] = len(tree_json)

	# Cross-action aggregation
	repeated_findings, leaderboard = _aggregate_hot_frames(per_action_trees)
	findings.extend(repeated_findings)

	# Donut breakdown
	breakdown = _build_session_breakdown(per_action_trees, sql_total_ms=sql_total_ms)

	aggregate["hot_frames"] = leaderboard
	aggregate["session_time_breakdown"] = breakdown
	aggregate["total_python_ms"] = breakdown["python_ms"]
	aggregate["total_sql_ms"] = breakdown["sql_ms"]

	return AnalyzerResult(findings=findings, aggregate=aggregate)
