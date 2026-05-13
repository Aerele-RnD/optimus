# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for findings, aggregation, and analyzer entry point of call_tree."""

from optimus.analyzers import call_tree
from optimus.analyzers.base import AnalyzeContext


def _node(function, filename, cumulative_ms, children=None, self_ms=0):
	return {
		"function": function,
		"filename": filename,
		"lineno": 1,
		"self_ms": self_ms,
		"cumulative_ms": cumulative_ms,
		"kind": "python",
		"children": children or [],
	}


# ---------------------------------------------------------------------------
# Pruning + soft cap
# ---------------------------------------------------------------------------


def test_prune_drops_nodes_below_threshold():
	tree = _node("<root>", "", 1000, [
		_node("big", "u.py", 800, []),
		_node("medium", "u.py", 50, []),
		_node("tiny1", "u.py", 1, []),
		_node("tiny2", "u.py", 1, []),
	])
	# Threshold: max(2, 1000 * 0.005) = 5ms
	pruned = call_tree._prune(tree, action_wall_time_ms=1000, threshold_pct=0.005)
	functions = [c["function"] for c in pruned["children"]]
	assert "big" in functions
	assert "medium" in functions
	assert any("[other:" in f for f in functions)
	assert "tiny1" not in functions
	assert "tiny2" not in functions


def test_prune_never_drops_sql_leaves():
	"""SQL leaves are always preserved regardless of size."""
	tree = _node("<root>", "", 1000, [
		_node("big", "u.py", 800, [
			{"function": "<sql>", "filename": "u.py:5", "kind": "sql",
			 "self_ms": 0.1, "cumulative_ms": 0.1, "query_count": 1,
			 "partial_match": False, "children": []},
		]),
	])
	pruned = call_tree._prune(tree, action_wall_time_ms=1000, threshold_pct=0.005)
	big = pruned["children"][0]
	# The 0.1ms sql leaf is below threshold but must survive
	assert any(c.get("kind") == "sql" for c in big["children"])


def test_soft_cap_preserves_hot_path():
	"""Cap drops cold siblings, never the hot descending path."""
	deep = _node("hot5", "u.py", 90, [])
	for level in (4, 3, 2, 1):
		deep = _node(f"hot{level}", "u.py", 90, [
			deep,
			_node(f"cold{level}", "u.py", 1, []),
		])
	tree = _node("<root>", "", 100, [deep])
	capped = call_tree._soft_cap_nodes(tree, max_nodes=6)

	collected = []

	def walk(n):
		collected.append(n["function"])
		for c in n.get("children", []):
			walk(c)
	walk(capped)

	for level in range(1, 6):
		assert f"hot{level}" in collected, f"hot{level} dropped from capped tree"


# ---------------------------------------------------------------------------
# Per-action findings (F1 Slow Hot Path + F3 Hook Bottleneck)
# ---------------------------------------------------------------------------


def test_emit_slow_hot_path_finding_above_thresholds():
	tree = _node("<root>", "", 1000, [
		_node("my_app.heavy_thing", "apps/my_app/x.py", 600, []),
		_node("other", "apps/my_app/y.py", 100, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="Sales Invoice submit",
		action_wall_time_ms=1000,
	)
	slow = [f for f in findings if f["finding_type"] == "Slow Hot Path"]
	assert len(slow) == 1
	assert slow[0]["severity"] == "High"
	assert "my_app.heavy_thing" in slow[0]["title"]
	assert slow[0]["estimated_impact_ms"] == 600
	assert slow[0]["action_ref"] == "0"


def test_no_finding_below_pct_threshold():
	# 20% < 25% threshold
	tree = _node("<root>", "", 1000, [
		_node("my_app.thing", "apps/my_app/x.py", 200, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="x", action_wall_time_ms=1000,
	)
	assert findings == []


def test_no_finding_below_absolute_threshold():
	# 30% but only 150ms
	tree = _node("<root>", "", 500, [
		_node("my_app.thing", "apps/my_app/x.py", 150, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="x", action_wall_time_ms=500,
	)
	assert findings == []


def test_self_referential_hot_path_uses_clear_phrasing():
	"""Production bug: the title read as
	'In erpnext.accounts.doctype.pricing_rule.pricing_rule.apply_pricing_rule,
	 96% of the time was spent in apply_pricing_rule'
	— tautological because the action label ends with the same
	function name. The hot spot is self-time in the function body,
	not a subcall. We rephrase to surface that fact."""
	tree = _node("<root>", "", 1000, [
		_node("apply_pricing_rule", "apps/erpnext/foo.py", 960, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=5,
		action_label=(
			"erpnext.accounts.doctype.pricing_rule.pricing_rule."
			"apply_pricing_rule"
		),
		action_wall_time_ms=1000,
	)
	slow = [f for f in findings if f["finding_type"] == "Slow Hot Path"]
	assert len(slow) == 1
	title = slow[0]["title"]
	# The old self-referential phrasing must be gone.
	assert "of the time was spent in apply_pricing_rule" not in title, (
		f"Self-referential hot-path phrasing must be rewritten; "
		f"got: {title!r}"
	)
	# New phrasing should mention self-time / own body.
	assert (
		"self-time" in title.lower()
		or "body" in title.lower()
		or "own" in title.lower()
	), (
		f"Title must signal that the hot spot is self-time in the "
		f"function's own body; got: {title!r}"
	)
	# Still carries the function name + percentage.
	assert "apply_pricing_rule" in title
	assert "96%" in title


def test_non_self_referential_hot_path_keeps_classic_phrasing():
	"""Sanity: the self-time fix must NOT change the classic shape
	when action_label and fn_name are different (the common case)."""
	tree = _node("<root>", "", 1000, [
		_node("validate", "apps/erpnext/foo.py", 800, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0,
		action_label="frappe.desk.form.save.savedocs:Save",
		action_wall_time_ms=1000,
	)
	slow = [f for f in findings if f["finding_type"] == "Slow Hot Path"]
	assert len(slow) == 1
	assert "80% of the time was spent in validate" in slow[0]["title"], (
		f"Non-self-referential titles must keep the classic phrasing; "
		f"got: {slow[0]['title']!r}"
	)


def test_hook_bottleneck_emitted_instead_of_slow_hot_path():
	"""F3 takes precedence when ancestor is Document.run_method."""
	tree = _node("<root>", "", 1000, [
		_node("frappe.model.document.Document.run_method",
		      "apps/frappe/model/document.py", 800, [
			_node("erpnext.selling.doctype.sales_invoice.sales_invoice.validate",
			      "apps/erpnext/x.py", 700, []),
		]),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="SI submit", action_wall_time_ms=1000,
	)
	# Should emit F3 (Hook Bottleneck), NOT F1 (Slow Hot Path)
	# Note: the run_method node itself starts with "frappe." so it matches
	# the "framework" prefix check in _walk_for_findings. The ACTUAL hook
	# (validate) is its child, and validate's ancestor chain contains
	# Document.run_method, so validate gets the Hook Bottleneck label.
	assert any(f["finding_type"] == "Hook Bottleneck" for f in findings)
	assert not any(f["finding_type"] == "Slow Hot Path" for f in findings)


def test_slow_hot_path_suppressed_when_dominated_by_sql():
	"""F1 is suppressed when >80% of subtree time is a single SQL leaf."""
	sql_leaf = {
		"function": "<sql>", "filename": "u.py:5", "kind": "sql",
		"self_ms": 700, "cumulative_ms": 700,
		"query_normalized": "SELECT 1", "query_count": 1,
		"partial_match": False, "children": [],
	}
	tree = _node("<root>", "", 1000, [
		_node("my_app.thing", "apps/my_app/x.py", 800, [sql_leaf]),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="x", action_wall_time_ms=1000,
	)
	# 700/800 = 87.5% — suppressed (top_queries handles it)
	assert findings == []


# ---------------------------------------------------------------------------
# Cross-action aggregation
# ---------------------------------------------------------------------------


def test_repeated_hot_frame_finding_aggregates_across_actions():
	# Same frame in 4 different actions, total 800ms
	per_action_trees = []
	for _ in range(4):
		t = _node("<root>", "", 500, [
			_node("my_app.discounts.calc", "apps/my_app/d.py", 200, []),
		])
		per_action_trees.append(t)

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	assert len(repeated) == 1
	assert "my_app.discounts.calc" in repeated[0]["title"]
	assert repeated[0]["estimated_impact_ms"] == 800


def test_repeated_hot_frame_no_finding_when_below_thresholds():
	# Only 2 actions (need 3) and only 200ms total (need 500)
	per_action_trees = [
		_node("<root>", "", 500, [_node("my_app.tiny", "apps/my_app/t.py", 100, [])]),
		_node("<root>", "", 500, [_node("my_app.tiny", "apps/my_app/t.py", 100, [])]),
	]
	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	assert findings == []


def test_repeated_hot_frame_filters_wrappers_entirely():
	"""v0.5.2 strengthens the v0.5.1 fix: the original bug was
	'High — wrapper appeared in 11 actions and consumed 3534ms total'
	where each of the 11 was a different decorator wrapper from a
	different module. v0.5.1 solved it by disambiguating per-file
	(so each wrapper became its own leaderboard entry). That was
	better but still noisy — every decorated method produced a
	'wrapper' entry with a cumulative time ≈ the wrapped function's
	cumulative time, duplicating information.

	v0.5.2 filters all 'wrapper' / 'composer' / 'runner' / 'fn' /
	'hook' / 'compose' frames entirely. They're decorator internals
	regardless of file — showing them adds no signal beyond what
	the wrapped function's own leaderboard entry already provides.

	This test now verifies the stronger v0.5.2 behavior: wrappers
	are gone from the leaderboard entirely, not disambiguated.
	"""
	per_action_trees = []
	# Four different 'wrapper' functions in four different files.
	# All should be filtered — none should appear in the leaderboard.
	for _ in range(4):
		per_action_trees.append(_node("<root>", "", 500, [
			_node("wrapper", "my_app/a.py", 50, []),
			_node("wrapper", "my_app/b.py", 50, []),
			_node("wrapper", "my_app/c.py", 50, []),
			_node("wrapper", "my_app/d.py", 50, []),
		]))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)

	# NO wrapper entries at all — they're decorator plumbing.
	wrapper_entries = [r for r in leaderboard if "::wrapper" in r["function"]]
	assert wrapper_entries == [], (
		f"v0.5.2: wrapper frames must be filtered entirely. Got "
		f"{len(wrapper_entries)} wrapper entries in the leaderboard: "
		f"{[r['function'] for r in wrapper_entries]}"
	)
	# And no Repeated Hot Frame findings for wrappers either.
	wrapper_findings = [
		f for f in findings if "wrapper" in f["title"].lower()
	]
	assert wrapper_findings == []


def test_repeated_hot_frame_skips_pure_helpers_only():
	"""v0.5.1: Repeated Hot Frame aggregator uses the NARROWER
	_is_pure_helper_frame filter, not the broad _is_framework_frame.
	Pure plumbing helpers (frappe/handler.py, frappe/utils/, werkzeug,
	rq, pyinstrument) are suppressed — those are unoptimizable.
	"""
	per_action_trees = []
	for _ in range(5):
		per_action_trees.append(_node("<root>", "", 500, [
			# frappe/handler.py — pure request-dispatch plumbing
			_node("handle", "apps/frappe/handler.py", 250, []),
			# frappe/utils/data.py — type conversion helper
			_node("cint", "apps/frappe/frappe/utils/data.py", 100, []),
			# werkzeug — infra lib
			_node(
				"inner",
				"env/lib/python3.14/site-packages/werkzeug/wsgi.py",
				80,
				[],
			),
		]))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)

	# None of the pure-helper frames should appear as findings.
	suppressed = ("handle", "cint", "inner")
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	for f in repeated:
		for name in suppressed:
			assert name not in f["title"], (
				f"pure helper '{name}' leaked into Repeated Hot Frame "
				f"findings: {f['title']}"
			)

	# And not in the leaderboard either.
	for row in leaderboard:
		for name in suppressed:
			assert name not in row["function"], (
				f"pure helper '{name}' leaked into hot-frames leaderboard: {row}"
			)


def test_repeated_hot_frame_keeps_frappe_application_code():
	"""v0.5.2 round 2: frappe/model/document.py is now entirely
	plumbing (filtered). Document.run_method's cumulative time is
	always ≈ the user's validate/on_submit hook times, so showing
	it as a separate hot frame double-counts information. Users
	see their actual hooks (erpnext/*.py::validate) as hot frames
	— that's the actionable signal.

	This test now verifies what remains actionable inside frappe/*:

	  - permissions.has_permission: slow custom Permission Query
	    Conditions bubble here. KEPT.
	  - naming.make_autoname: user's naming-series config can be
	    optimized (simpler prefix, fewer SQL lookups). KEPT.

	Document.run_method is NOT in this keeper list anymore — the
	user can see their hooks directly instead of the dispatcher.
	"""
	per_action_trees = []
	for _ in range(5):
		per_action_trees.append(_node("<root>", "", 2000, [
			# permissions.has_permission — keep (user custom rules).
			_node(
				"has_permission",
				"apps/frappe/frappe/permissions.py",
				400,
				[],
			),
			# naming.make_autoname — keep (user naming series).
			_node(
				"make_autoname",
				"apps/frappe/frappe/model/naming.py",
				300,
				[],
			),
		]))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)

	# Both keepers should be in the leaderboard.
	leaderboard_fns = [r["function"] for r in leaderboard]
	assert any("has_permission" in f for f in leaderboard_fns), (
		f"has_permission missing from leaderboard: {leaderboard_fns}"
	)
	assert any("make_autoname" in f for f in leaderboard_fns), (
		f"make_autoname missing from leaderboard: {leaderboard_fns}"
	)
	assert any("has_permission" in f for f in leaderboard_fns), (
		f"has_permission missing from leaderboard: {leaderboard_fns}"
	)


# ---------------------------------------------------------------------------
# v0.5.1 regression guards: the 8 false-positive Repeated Hot Frame findings
# from the production report were all framework dispatch plumbing that the
# pure-helper filter should have caught but didn't. Root cause: the filter
# fragments used leading slashes (`"/frappe/handler.py"`), but pyinstrument
# stores filenames as relative paths (`"frappe/handler.py"`). The substring
# `in` check was a silent no-op.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v0.5.2: expanded plumbing filter — decorator wrappers, meta loaders,
# form-load, query-builder, database layer, stdlib, third-party libs
# ---------------------------------------------------------------------------


def test_pure_helper_filters_decorator_wrappers():
	"""Decorator-wrapper function names (wrapper, composer, runner,
	fn, hook, compose) are always plumbing regardless of file.
	Frappe's @hook decorator produces this chain for every doc-event
	method, so without this filter every decorated method shows up
	as 6+ hot frames with identical cumulative times."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for function_name in ("wrapper", "composer", "runner", "fn", "hook",
	                      "compose", "add_to_return_value"):
		node = {
			"function": function_name,
			"filename": "apps/frappe/frappe/model/document.py",
			"kind": "python",
		}
		assert _is_pure_helper_frame(node) is True, (
			f"{function_name} must be filtered as decorator plumbing"
		)

	# Private delegation shims (sibling of a public method)
	for function_name in ("_save", "_insert"):
		node = {
			"function": function_name,
			"filename": "frappe/model/document.py",
			"kind": "python",
		}
		assert _is_pure_helper_frame(node) is True, (
			f"{function_name} must be filtered as private delegation"
		)

	# Public entry points KEPT — users recognize these
	for function_name in ("save", "insert", "submit", "cancel", "validate"):
		node = {
			"function": function_name,
			"filename": "frappe/model/document.py",
			"kind": "python",
		}
		# "validate" etc. are user-implemented hooks that run through
		# the decorator chain — users should see these in the
		# leaderboard. But filtering works on frame FILENAME too:
		# frappe/model/document.py doesn't fire those here because
		# "validate" the user wrote lives at apps/myapp/controllers/*.py.
		# This test verifies the FUNCTION-NAME filter doesn't
		# accidentally catch them when they happen to be in
		# document.py (it won't — they're not in the block list).


def test_pure_helper_filters_meta_loaders():
	"""Meta.__init__, get_meta, load_from_db, process on
	frappe/model/meta.py are all the same call tree. A real
	production report had 11 separate Repeated Hot Frame findings
	all from meta.py. Filter the whole file."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for function_name in ("__init__", "get_meta", "load_from_db",
	                      "process", "build_doctype", "add_custom_fields"):
		for filename in (
			"frappe/model/meta.py",
			"apps/frappe/frappe/model/meta.py",
			"/Users/navin/office/frappe_bench/apps/frappe/frappe/model/meta.py",
		):
			node = {"function": function_name, "filename": filename, "kind": "python"}
			assert _is_pure_helper_frame(node) is True, (
				f"{filename}::{function_name} must be filtered "
				"(meta-loading plumbing)"
			)


def test_pure_helper_filters_form_load_and_meta_bundle():
	"""frappe/desk/form/load.py::getdoctype and get_meta_bundle
	are Desk form-opening plumbing. Always hot because every form
	open calls them, never user-actionable."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for filename in (
		"frappe/desk/form/load.py",
		"frappe/desk/form/meta.py",
	):
		for function_name in ("getdoctype", "get_meta_bundle", "getdoc", "__init__"):
			node = {"function": function_name, "filename": filename, "kind": "python"}
			assert _is_pure_helper_frame(node) is True, (
				f"{filename}::{function_name} must be filtered"
			)


def test_pure_helper_filters_query_builder_and_database():
	"""query_builder/utils.py, model/qb_query.py, and the
	database/mariadb/postgres layer are "every query passes through
	here" plumbing. The top_queries / index_suggestions analyzers
	surface SQL issues more actionably; the hot-frame entries are
	duplicative noise."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for filename in (
		"frappe/query_builder/utils.py",
		"frappe/model/qb_query.py",
		"frappe/database/database.py",
		"frappe/database/mariadb/database.py",
		"frappe/database/postgres/database.py",
	):
		node = {"function": "execute", "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is True, (
			f"{filename} must be filtered (query plumbing)"
		)


def test_pure_helper_filters_python_stdlib_single_segment():
	"""Single-segment filenames are Python stdlib or loose scripts,
	never Frappe apps. Production report had inspect.py::
	getouterframes and inspect.py::getframeinfo in the top 20 hot
	frames because pyinstrument calls stdlib inspect while capturing
	frames."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for filename in ("inspect.py", "functools.py", "threading.py",
	                 "contextlib.py", "typing.py"):
		node = {"function": "getouterframes", "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is True, (
			f"stdlib file {filename} must be filtered"
		)


def test_pure_helper_filters_third_party_libs():
	"""MySQLdb/pymysql/requests frames slip through when
	pyinstrument strips the site-packages/ prefix. Catch by
	first-segment name."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for first_segment in ("MySQLdb", "pymysql", "requests", "urllib3",
	                      "jinja2", "pandas", "redis"):
		node = {
			"function": "execute",
			"filename": f"{first_segment}/cursors.py",
			"kind": "python",
		}
		assert _is_pure_helper_frame(node) is True, (
			f"third-party lib {first_segment} must be filtered"
		)


def test_pure_helper_filters_pyinstrument_synthetic_markers():
	"""<built-in>, <string>, <module>, <frozen …> — synthetic
	function names pyinstrument emits for C builtins / compiled-
	string frames."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for function_name in ("<built-in>", "<string>", "<module>",
	                      "<frozen importlib._bootstrap>"):
		node = {
			"function": function_name,
			"filename": "<built-in>",
			"kind": "python",
		}
		assert _is_pure_helper_frame(node) is True, (
			f"synthetic '{function_name}' must be filtered"
		)


def test_pure_helper_production_payload_all_filtered():
	"""End-to-end regression: the EXACT 20 frames from the
	production report's Repeated Hot Frame section must all be
	filtered. If any of these slip through, the report goes back
	to being dominated by framework plumbing."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	# Exact (function, filename) pairs from a production report — the
	# `must_filter` list below is the curated subset that this test
	# actually asserts on. Originally recorded here for historical
	# context; kept as a comment to avoid an unused-variable flag.
	#   execute_query / frappe/query_builder/utils.py
	#   save / _save / run_method / composer / runner / fn / __init__ /
	#   load_from_db / insert / get_doc_str — all frappe/model/document.py
	#   get_meta / __init__ — frappe/model/meta.py and frappe/desk/form/meta.py
	#   get_values / sql / execute_query — frappe/database/database.py
	#   getdoctype / get_meta_bundle — frappe/desk/form/load.py
	#   execute — frappe/model/qb_query.py
	#   load_from_db / process — frappe/model/meta.py
	#   getouterframes / getframeinfo — inspect.py
	#   wrapper — erpnext/__init__.py, utils/__init__.py
	#   execute / _query — MySQLdb/cursors.py

	# v0.5.2 round 2: ALL of frappe/model/document.py is filtered
	# (save / insert / submit / _save / run_method / __init__ /
	# load_from_db / etc.), so this list now includes every row
	# from production_noise. Users see actionable signal from
	# their erpnext / myapp validate / on_submit hooks instead.
	must_filter = [
		("execute_query", "frappe/query_builder/utils.py"),
		("save", "frappe/model/document.py"),
		("_save", "frappe/model/document.py"),
		("run_method", "frappe/model/document.py"),
		("composer", "frappe/model/document.py"),
		("runner", "frappe/model/document.py"),
		("fn", "frappe/model/document.py"),
		("get_meta", "frappe/model/meta.py"),
		("__init__", "frappe/model/meta.py"),
		("get_values", "frappe/database/database.py"),
		("getdoctype", "frappe/desk/form/load.py"),
		("execute", "frappe/model/qb_query.py"),
		("get_meta_bundle", "frappe/desk/form/load.py"),
		("__init__", "frappe/model/document.py"),
		("load_from_db", "frappe/model/document.py"),
		("get_meta", "frappe/desk/form/meta.py"),
		("__init__", "frappe/desk/form/meta.py"),
		("load_children_from_db", "frappe/model/document.py"),
		("getouterframes", "inspect.py"),
		("getframeinfo", "inspect.py"),
		("load_from_db", "frappe/model/meta.py"),
		("process", "frappe/model/meta.py"),
		("insert", "frappe/model/document.py"),
		("wrapper", "erpnext/__init__.py"),
		("wrapper", "utils/__init__.py"),
		("sql", "frappe/database/database.py"),
		("execute_query", "frappe/database/database.py"),
		("execute", "MySQLdb/cursors.py"),
		("get_doc_str", "frappe/model/document.py"),
		("_query", "MySQLdb/cursors.py"),
		# Round 2 additions from diagnostic run
		("is_virtual_doctype", "frappe/model/utils/__init__.py"),
		("[other: 3 frames]", ""),
		("[other: 1 frames]", ""),
	]

	for function_name, filename in must_filter:
		node = {"function": function_name, "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is True, (
			f"Production noise not filtered: {filename}::{function_name} "
			"— this frame would re-appear in the Repeated Hot Frame "
			"leaderboard if the filter regresses."
		)


def test_pure_helper_still_keeps_application_frappe_code():
	"""Critical negative case: user-visible APP code MUST still pass
	through. v0.5.2 round 2 removed Document.save/insert/submit/
	run_method from the keepers because their cumulative times
	always duplicate the user's validate/on_submit hooks —
	double-counting noise. The user still sees the actual hook
	code (erpnext/*, myapp/*) as hot frames."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	keepers = [
		# Permissions (slow custom Permission Query Conditions bubble here)
		("has_permission", "frappe/permissions.py"),
		# Naming series (user's series config is optimizable)
		("make_autoname", "frappe/model/naming.py"),
		# frappe.client REST API entry points
		("get_list", "frappe/client.py"),
		# User app code always kept
		("compute_tax", "apps/erpnext/erpnext/accounts/tax.py"),
		("validate", "apps/myapp/doctype/invoice.py"),
		("on_submit", "erpnext/accounts/doctype/sales_invoice/sales_invoice.py"),
	]
	for function_name, filename in keepers:
		node = {"function": function_name, "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is False, (
			f"{filename}::{function_name} was WRONGLY filtered — "
			"users must see this in the Repeated Hot Frame leaderboard "
			"as a legitimate optimization target."
		)


def test_is_pure_helper_frame_matches_relative_filenames():
	"""Core bug: the filter must match filenames as stored by
	pyinstrument (relative paths, no leading slash). Pre-v0.5.1 the
	fragments had leading slashes and the `in` check was a no-op."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	# These are the EXACT filenames pyinstrument produces for the
	# offending framework frames in a production session. All must be
	# classified as pure helpers.
	for filename, function in (
		("frappe/app.py", "application"),
		("frappe/handler.py", "handle"),
		("frappe/handler.py", "execute_cmd"),
		("frappe/__init__.py", "call"),
		("frappe/recorder.py", "record_sql"),
		("frappe/api/__init__.py", "handle"),
		("frappe/api/v1.py", "handle_rpc_call"),
		("frappe/api/v2.py", "handle_rpc_call"),
		("frappe/utils/typing_validations.py", "wrapper"),
		("frappe/utils/response.py", "build_response"),
		("optimus/hooks_callbacks.py", "before_request"),
	):
		node = {"function": function, "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is True, (
			f"{filename}::{function} must be classified as a pure helper "
			f"— it's framework plumbing that every request passes through"
		)


def test_is_pure_helper_frame_matches_absolute_filenames():
	"""Backward compatibility: some environments DO deliver absolute
	paths from pyinstrument. Those must still match — the filter is
	position-insensitive (substring / suffix)."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for filename in (
		"/Users/navin/office/frappe_bench/v16/mariadb/apps/frappe/frappe/app.py",
		"/Users/navin/office/frappe_bench/v16/mariadb/apps/frappe/frappe/handler.py",
		"/home/frappe/frappe-bench/apps/frappe/frappe/recorder.py",
	):
		node = {"function": "anything", "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is True, (
			f"Absolute path {filename} must still match the pure-helper "
			f"filter via suffix / substring check."
		)


def test_is_pure_helper_frame_keeps_application_frappe_code():
	"""Negative cases: the SURFACE that matters is user-app code
	plus the few Frappe entry points users can actually optimize.

	v0.5.2 round 2 tightened further: the entire frappe/model/
	document.py file is now plumbing. Document.save / insert /
	submit / run_method all show up as hot frames with cumulative
	times IDENTICAL to the validate / on_submit / etc. hooks
	inside them — so the Document-level entries are redundant
	double-counts. Users see their actual hook code (erpnext/…::
	validate, myapp/…::on_submit) as separate hot frames; that's
	where the actionable signal lives.
	"""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for filename, function in (
		# Permissions — slow custom Permission Query Conditions
		# bubble here. KEPT.
		("frappe/permissions.py", "has_permission"),
		# Naming series — user's series config can be optimized
		# (simpler prefix, fewer SQL lookups). KEPT.
		("frappe/model/naming.py", "make_autoname"),
		# frappe.client is the REST-ish API layer; get_list is the
		# entry point users of the REST API actually call. KEPT.
		("frappe/client.py", "get_list"),
		# User app code — the whole point of the hot-frames
		# leaderboard.
		("apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py", "validate"),
		("apps/myapp/controllers/invoice.py", "on_submit"),
		("erpnext/controllers/selling_controller.py", "validate"),
	):
		node = {"function": function, "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is False, (
			f"{filename}::{function} must NOT be filtered — it's "
			f"application-layer code users can optimize"
		)


def test_is_pure_helper_frame_filters_entire_document_py():
	"""v0.5.2 round 2: frappe/model/document.py is entirely
	plumbing. save / insert / submit / _save / run_method /
	__init__ / load_from_db / load_children_from_db /
	get_cached_doc / get_doc_str — all show up with cumulative
	times identical to the user code inside them. Diagnostic
	against a real stored call tree confirmed all of these leaked
	before the round-2 fix."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for function in (
		"save", "insert", "submit", "cancel", "delete", "amend",
		"_save", "_insert",
		"run_method", "run_before_save_methods", "run_after_save_methods",
		"__init__", "load_from_db", "load_children_from_db",
		"get_cached_doc", "get_doc_str", "get_cached_value",
		"reload",
	):
		for filename in (
			"frappe/model/document.py",
			"apps/frappe/frappe/model/document.py",
			"/Users/navin/office/frappe_bench/apps/frappe/frappe/model/document.py",
		):
			node = {"function": function, "filename": filename, "kind": "python"}
			assert _is_pure_helper_frame(node) is True, (
				f"{filename}::{function} must be filtered "
				"(Document plumbing — v0.5.2 round 2)"
			)


def test_is_pure_helper_frame_filters_model_utils():
	"""frappe/model/utils/* is framework plumbing — is_virtual_doctype,
	get_parent_doc, etc. Called by document loader, no user decision
	behind the time they consume."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for function in ("is_virtual_doctype", "get_parent_doc", "set_new_name"):
		for filename in (
			"frappe/model/utils/__init__.py",
			"apps/frappe/frappe/model/utils/__init__.py",
			"frappe/model/utils/set_user_and_timestamp.py",
		):
			node = {"function": function, "filename": filename, "kind": "python"}
			assert _is_pure_helper_frame(node) is True, (
				f"{filename}::{function} must be filtered"
			)


def test_is_pure_helper_frame_filters_square_bracket_synthetic():
	"""[other: N frames] / [omitted: N frames] are synthetic nodes
	the pruner inserts to represent rolled-up frames. They're
	summaries, not real functions. Diagnostic against stored call
	trees confirmed these leaked into the leaderboard as frames
	with no file path."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for function in (
		"[other: 1 frames]", "[other: 3 frames]", "[other: 10 frames]",
		"[omitted: 5 frames]",
	):
		node = {"function": function, "filename": "", "kind": "python"}
		assert _is_pure_helper_frame(node) is True, (
			f"synthetic pruning marker '{function}' must be filtered"
		)


def test_is_pure_helper_frame_keeps_user_code():
	"""User app code must always pass through the filter."""
	from optimus.analyzers.call_tree import _is_pure_helper_frame

	for filename in (
		"erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
		"apps/my_custom_app/handlers.py",
		"my_app/module.py",
	):
		node = {"function": "do_thing", "filename": filename, "kind": "python"}
		assert _is_pure_helper_frame(node) is False


def test_is_framework_frame_matches_relative_filenames():
	"""Same slash-bug fix applies to the broader _is_framework_frame
	filter used by Slow Hot Path. Production report showed 'In POST
	/api/method/..., 99% of time was spent in application' — i.e.
	frappe/app.py::application. That frame should have been
	classified as framework so Slow Hot Path descended into user
	code below it."""
	from optimus.analyzers.call_tree import _is_framework_frame

	for filename in (
		"frappe/app.py",
		"frappe/handler.py",
		"frappe/model/document.py",
		"frappe/permissions.py",
		"frappe/__init__.py",
		"optimus/hooks_callbacks.py",
	):
		node = {"function": "anything", "filename": filename, "kind": "python"}
		assert _is_framework_frame(node) is True, (
			f"{filename} must be classified as framework (broad filter)"
		)

	# Negative case: user app code
	for filename in (
		"erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
		"apps/my_custom_app/handlers.py",
	):
		node = {"function": "x", "filename": filename, "kind": "python"}
		assert _is_framework_frame(node) is False


# ---------------------------------------------------------------------------
# v0.5.1: strip optimus/* frames from stored call tree
# ---------------------------------------------------------------------------


def test_strip_profiler_frames_removes_snapshot_subtree():
	"""Exact production payload: the call tree for a 47ms realtime
	subscribe request had a 31ms optimus/hooks_callbacks ->
	snapshot -> _read_db subtree attributed to it, because pyinstrument
	started INSIDE before_request and captured the infra snapshot as
	part of the user's action.

	After _strip_profiler_frames, the tree must contain zero nodes
	from optimus/*, and the user-visible chain (application →
	init_request → call) must remain intact."""
	tree = _node("application", "frappe/app.py", 47.15, [
		_node("init_request", "frappe/app.py", 31.56, [
			_node("call", "frappe/__init__.py", 31.56, [
				_node(
					"before_request",
					"optimus/hooks_callbacks.py",
					31.56,
					[
						_node(
							"snapshot",
							"optimus/infra_capture.py",
							31.56,
							[
								_node(
									"_read_db",
									"optimus/infra_capture.py",
									13.74,
									[],
								),
							],
						),
					],
				),
			]),
		]),
	])

	call_tree._strip_profiler_frames(tree)

	# Collect every remaining filename in the tree (recursively).
	def collect(n, out):
		out.append((n.get("function"), n.get("filename")))
		for c in n.get("children", []):
			collect(c, out)

	nodes: list = []
	collect(tree, nodes)

	# No optimus/* frame may remain.
	for fn, filename in nodes:
		assert "optimus/" not in (filename or ""), (
			f"Profiler frame leaked: {fn} @ {filename}"
		)

	# The user-visible chain must still exist.
	functions = [fn for fn, _ in nodes]
	assert "application" in functions
	assert "init_request" in functions
	assert "call" in functions
	# And the profiler chain must be gone.
	for bad in ("before_request", "snapshot", "_read_db"):
		assert bad not in functions, (
			f"Expected '{bad}' stripped from tree; still present"
		)


def test_strip_profiler_frames_grafts_user_code_children_up():
	"""Edge case: a user-code frame nested BELOW a optimus
	frame (e.g. a capture.py wrap that intercepts get_doc and then
	user's __init__ runs under it). The user-code child must be
	preserved — grafted up to where the profiler frame was."""
	tree = _node("<root>", "", 100, [
		_node("application", "frappe/app.py", 100, [
			_node(
				"wrapped_get_doc",
				"optimus/capture.py",
				50,
				[
					_node("Document.__init__", "frappe/model/document.py", 50, [
						_node("compute_total", "apps/myapp/invoice.py", 45, []),
					]),
				],
			),
		]),
	])

	call_tree._strip_profiler_frames(tree)

	# Walk and find the expected shape:
	# <root> -> application -> Document.__init__ -> compute_total
	def functions_in(n):
		return [n.get("function")] + [
			f for c in n.get("children", []) for f in functions_in(c)
		]

	fns = functions_in(tree)
	assert "wrapped_get_doc" not in fns, "profiler wrap must be stripped"
	assert "Document.__init__" in fns, (
		"user-code descendant of profiler wrap must be preserved"
	)
	assert "compute_total" in fns, (
		"deeper user-code descendant must be preserved"
	)


def test_strip_profiler_frames_no_op_on_clean_tree():
	"""A tree with no profiler frames must be unchanged (aside from
	in-place mutation returning the same node)."""
	tree = _node("application", "frappe/app.py", 100, [
		_node("save", "frappe/model/document.py", 50, []),
		_node("compute", "apps/myapp/handlers.py", 40, []),
	])
	call_tree._strip_profiler_frames(tree)
	assert tree["function"] == "application"
	assert len(tree["children"]) == 2
	assert tree["children"][0]["function"] == "save"
	assert tree["children"][1]["function"] == "compute"


def test_strip_profiler_frames_handles_absolute_paths():
	"""Belt-and-suspenders: absolute filenames like
	/Users/.../apps/optimus/capture.py must also be stripped.
	The substring check on 'optimus/' catches both."""
	tree = _node("<root>", "", 100, [
		_node(
			"before_request",
			"/Users/navin/office/frappe_bench/apps/optimus/optimus/hooks_callbacks.py",
			50,
			[],
		),
		_node("save", "frappe/model/document.py", 50, []),
	])
	call_tree._strip_profiler_frames(tree)
	fns = [c["function"] for c in tree["children"]]
	assert "before_request" not in fns
	assert "save" in fns


def test_hooks_callbacks_before_request_snapshot_runs_before_pyi_start():
	"""Source-inspection guard: the v0.5.1 fix for the profiler-
	self-capture bug reorders before_request so the infra snapshot
	runs BEFORE pyinstrument starts. If someone accidentally flips the
	order back, pyi will once again capture its own 30ms snapshot as
	part of the user's action.

	Matches on distinctive call-site syntax (``frappe.local
	.optimus_infra_start =`` for the snapshot, ``_capture
	._start_pyi_session(`` for pyi start) to avoid false matches
	against commentary that mentions the function names.
	"""
	import inspect

	from optimus import hooks_callbacks

	src = inspect.getsource(hooks_callbacks.before_request)
	# The literal assignment only appears at the actual call site.
	snapshot_idx = src.find("frappe.local.optimus_infra_start = infra_capture.snapshot()")
	# The literal call-with-open-paren only appears at the actual call site.
	pyi_idx = src.find("_capture._start_pyi_session(")
	assert snapshot_idx > 0, (
		"before_request must assign infra_capture.snapshot() to "
		"frappe.local.optimus_infra_start"
	)
	assert pyi_idx > 0, "before_request must call _capture._start_pyi_session("
	assert snapshot_idx < pyi_idx, (
		"infra_capture.snapshot() must run BEFORE _start_pyi_session in "
		"before_request, or pyinstrument will capture the snapshot's "
		"~30ms of SHOW GLOBAL STATUS / psutil work as part of the "
		"user's action. This was the 67%-of-action-time false attribution "
		"seen in production."
	)


def test_hooks_callbacks_before_job_snapshot_runs_before_pyi_start():
	"""Same ordering guard for before_job — background jobs have the
	same self-capture exposure as HTTP requests."""
	import inspect

	from optimus import hooks_callbacks

	src = inspect.getsource(hooks_callbacks.before_job)
	snapshot_idx = src.find("frappe.local.optimus_infra_start = infra_capture.snapshot()")
	pyi_idx = src.find("_capture._start_pyi_session(")
	assert snapshot_idx > 0 and pyi_idx > 0
	assert snapshot_idx < pyi_idx, (
		"infra_capture.snapshot() must run BEFORE _start_pyi_session in "
		"before_job (same rationale as before_request)."
	)


def test_production_payload_eight_plumbing_findings_all_filtered():
	"""End-to-end regression: the exact 8 frames that showed up as
	Repeated Hot Frame findings in the production report must produce
	zero Repeated Hot Frame findings after the filter fix."""
	# Simulate 15 actions where each passes through all 8 plumbing frames.
	# Without the fix, each would produce a Repeated Hot Frame finding
	# because 15 >= DEFAULT_REPEATED_FRAME_MIN_ACTIONS and the cumulative
	# time is well above the total_ms threshold.
	plumbing_frames = [
		("application", "frappe/app.py", 1061),
		("handle", "frappe/handler.py", 805),
		("execute_cmd", "frappe/handler.py", 805),
		("call", "frappe/__init__.py", 1117),
		("record_sql", "frappe/recorder.py", 580),
		("handle", "frappe/api/__init__.py", 821),
		("handle_rpc_call", "frappe/api/v1.py", 806),
		("wrapper", "frappe/utils/typing_validations.py", 1252),
	]
	per_action_trees = []
	for _ in range(15):
		children = [
			_node(fn, path, cumulative_ms // 15)
			for (fn, path, cumulative_ms) in plumbing_frames
		]
		per_action_trees.append(_node("<root>", "", 2000, children))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]

	# None of the 8 plumbing frames should produce a finding.
	assert repeated == [], (
		"The 8 framework plumbing frames from the production report "
		"must all be filtered out of Repeated Hot Frame findings. "
		f"Got: {[f['title'] for f in repeated]}"
	)

	# And they should not be in the leaderboard either — they'd waste
	# slots that could go to real user-code hot frames.
	bad_names = {fn for fn, _, _ in plumbing_frames}
	for row in leaderboard:
		assert not any(bad in row["function"] for bad in bad_names), (
			f"Plumbing frame leaked into leaderboard: {row['function']}"
		)


def test_repeated_hot_frame_keeps_user_code_finding():
	"""Companion positive test to the two above: user-code frames are
	still aggregated correctly and still fire findings.
	"""
	per_action_trees = []
	for _ in range(5):
		per_action_trees.append(_node("<root>", "", 1000, [
			_node("compute_taxes", "erpnext/accounts/tax.py", 200, []),
		]))

	findings, _ = call_tree._aggregate_hot_frames(per_action_trees)
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	assert len(repeated) == 1
	# The finding key now includes the filename prefix.
	assert "compute_taxes" in repeated[0]["title"]
	assert "tax.py" in repeated[0]["title"] or "accounts/tax" in repeated[0]["title"]


def test_hot_frames_leaderboard_top_20_sorted_desc():
	per_action_trees = []
	for i in range(25):
		per_action_trees.append(_node("<root>", "", 1000, [
			_node(f"my_app.func_{i:02d}", f"apps/my_app/f{i}.py", float(100 + i), []),
		]))
	_, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	assert len(leaderboard) <= 20
	for i in range(len(leaderboard) - 1):
		assert leaderboard[i]["total_ms"] >= leaderboard[i + 1]["total_ms"]


# ---------------------------------------------------------------------------
# Donut + analyzer entry point
# ---------------------------------------------------------------------------


def test_donut_bucketing_by_top_level_module():
	"""v0.5.1: bucketing is driven by the FILENAME, not the function
	name. Pyinstrument produces bare function names (`validate`,
	`calc`, `save`) without module qualifiers, so the pre-v0.5.1
	split-on-dot logic returned the function name itself as the
	bucket. The fix uses the filename's first path segment.
	"""
	per_action_trees = [
		# Bare function names, as pyinstrument actually produces them.
		_node("<root>", "", 1000, [
			_node("validate", "erpnext/selling/validate.py", 400, [], self_ms=400),
			_node("calc", "my_app/discounts.py", 300, [], self_ms=300),
			_node("save", "frappe/model/document.py", 200, [], self_ms=200),
		]),
	]
	breakdown = call_tree._build_session_breakdown(per_action_trees, sql_total_ms=100)
	assert breakdown["sql_ms"] == 100
	assert breakdown["python_ms"] == 900
	by_app = breakdown["by_app"]
	assert by_app.get("erpnext") == 400
	assert by_app.get("my_app") == 300
	assert by_app.get("frappe") == 200


def test_top_level_app_rejects_stdlib_and_third_party(monkeypatch):
	"""v0.5.1: production donut showed Python(inspect.py),
	Python(functools.py), Python(MySQLdb), Python(<built-in>) as
	separate buckets — all noise. Single-segment filenames (stdlib),
	site-packages paths, and angle-bracketed synthetic markers must
	all collapse to [other]. First-segment paths that AREN'T a
	known installed Frappe app (MySQLdb etc.) also collapse to
	[other] once frappe.get_installed_apps is consulted.
	"""
	# Simulate a real site with a small installed-apps list. In
	# production get_installed_apps returns e.g.
	# ["frappe", "erpnext", "optimus"]. The call is made
	# inside _top_level_app so the monkeypatch intercepts it for
	# the whole test.
	import frappe

	from optimus.analyzers.call_tree import _top_level_app
	monkeypatch.setattr(
		frappe,
		"get_installed_apps",
		lambda: ["frappe", "optimus", "erpnext"],
		raising=False,
	)

	# Python stdlib — single-segment filename
	assert _top_level_app("getmembers", "inspect.py") == "[other]"
	assert _top_level_app("reduce", "functools.py") == "[other]"
	assert _top_level_app("_bootstrap", "threading.py") == "[other]"

	# Third-party library — pyinstrument stripped site-packages/
	# prefix and left just the lib/file form. Rejected because
	# "MySQLdb" isn't in the installed-apps list.
	assert _top_level_app("query", "MySQLdb/connections.py") == "[other]"
	assert _top_level_app("execute", "pymysql/cursors.py") == "[other]"

	# Full site-packages path (caught even before the installed-apps
	# check by the site-packages/ substring filter).
	assert _top_level_app(
		"request",
		"env/lib/python3.14/site-packages/requests/api.py",
	) == "[other]"

	# Pyinstrument synthetic — angle-bracketed function name
	assert _top_level_app("<built-in>", "<built-in>") == "[other]"
	assert _top_level_app("<module>", "<string>") == "[other]"
	assert _top_level_app("<lambda>", "frappe/utils.py") == "[other]"

	# Real Frappe apps still pass through
	assert _top_level_app("handle", "frappe/handler.py") == "frappe"
	assert _top_level_app("save", "apps/frappe/frappe/model/document.py") == "frappe"
	assert _top_level_app("compute", "erpnext/accounts/tax.py") == "erpnext"


def test_top_level_app_falls_back_when_installed_apps_unavailable(monkeypatch):
	"""When frappe.get_installed_apps fails (no site context, unit
	test environment), accept the first-segment as the bucket name.
	This is the legacy behavior — a conservative fallback that
	preserves the pre-v0.5.1 unit tests, which don't mock frappe."""
	import frappe

	from optimus.analyzers.call_tree import _top_level_app

	def _raise(*a, **k):
		raise RuntimeError("no site context")

	monkeypatch.setattr(frappe, "get_installed_apps", _raise, raising=False)

	# With the fallback, MySQLdb passes through as its first segment
	# (acceptable for unit tests — real production always has a site).
	# The stdlib filters still fire: inspect.py has no / so → [other].
	assert _top_level_app("reduce", "functools.py") == "[other]"
	# Multi-segment path: legacy first-segment fallback.
	assert _top_level_app("handle", "frappe/handler.py") == "frappe"


def test_top_level_app_uses_filename_not_function_name():
	"""Direct unit test — covers the bug root cause without going
	through the full aggregation path."""
	from optimus.analyzers.call_tree import _top_level_app

	# Bare function name + typical pyinstrument relative filename
	assert _top_level_app("handle", "frappe/handler.py") == "frappe"
	assert _top_level_app("application", "frappe/app.py") == "frappe"
	assert _top_level_app("call", "frappe/__init__.py") == "frappe"
	assert _top_level_app("record_sql", "frappe/recorder.py") == "frappe"
	assert _top_level_app("before_request", "optimus/hooks_callbacks.py") == "optimus"
	assert _top_level_app("snapshot", "optimus/infra_capture.py") == "optimus"
	assert _top_level_app("validate", "erpnext/selling/doctype/sales_invoice.py") == "erpnext"
	assert _top_level_app("do_thing", "my_custom_app/handlers.py") == "my_custom_app"

	# Bench-layout relative path (apps/<app>/...)
	assert _top_level_app("save", "apps/frappe/frappe/model/document.py") == "frappe"
	assert _top_level_app("calc", "apps/erpnext/erpnext/accounts/tax.py") == "erpnext"

	# Absolute path containing the bench layout
	assert (
		_top_level_app(
			"save",
			"/Users/navin/office/frappe_bench/apps/frappe/frappe/model/document.py",
		)
		== "frappe"
	)

	# Third-party libs
	assert _top_level_app(
		"inner", "env/lib/python3.14/site-packages/werkzeug/wsgi.py"
	) == "[other]"
	assert _top_level_app("acquire", "site-packages/redis/client.py") == "[other]"

	# Synthetic / empty / skip markers
	assert _top_level_app("<root>", "frappe/app.py") == "[other]"
	assert _top_level_app("<sql>", "frappe/app.py") == "[other]"
	assert _top_level_app("[other]", "frappe/app.py") == "[other]"
	assert _top_level_app("", "frappe/app.py") == "[other]"
	assert _top_level_app("handle", "") == "[other]"


def test_production_donut_collapses_plumbing_into_frappe_bucket():
	"""Exact bucket fragmentation from the production report:
	six separate buckets for framework dispatch functions, each
	labeled by function name (application, init_request, call,
	before_request, snapshot, [other]) and each showing 0ms after
	rounding. The fix must collapse all of them into a single
	`frappe` / `optimus` bucket with the summed time."""
	per_action_trees = [
		_node("<root>", "", 200, [
			_node("application", "frappe/app.py", 195, [
				_node("init_request", "frappe/app.py", 20, [], self_ms=5.0),
				_node("call", "frappe/__init__.py", 160, [
					_node("execute_cmd", "frappe/handler.py", 150, [], self_ms=8.0),
					_node("record_sql", "frappe/recorder.py", 15, [], self_ms=3.0),
				], self_ms=2.0),
				_node("before_request", "optimus/hooks_callbacks.py",
				      10, [
					_node("snapshot", "optimus/infra_capture.py", 8, [], self_ms=4.0),
				], self_ms=1.0),
			], self_ms=5.0),
		]),
	]
	breakdown = call_tree._build_session_breakdown(per_action_trees, sql_total_ms=192)
	by_app = breakdown["by_app"]

	# Before the fix: six buckets — application, init_request, call,
	# execute_cmd, record_sql, snapshot, before_request — each near 0ms.
	# After the fix: two buckets (frappe + optimus) with real totals.
	assert set(by_app.keys()) <= {"frappe", "optimus", "[other]"}, (
		f"Unexpected buckets — should only contain frappe / "
		f"optimus / [other]; got: {set(by_app.keys())}"
	)

	# frappe bucket = 5 (app) + 5 (init_request) + 2 (call) + 8 (execute_cmd)
	# + 3 (record_sql) = 23 ms
	assert by_app.get("frappe") == 23, (
		f"frappe bucket should sum to 23ms (5+5+2+8+3); got {by_app.get('frappe')}"
	)
	# optimus bucket = 1 (before_request) + 4 (snapshot) = 5 ms
	assert by_app.get("optimus") == 5, (
		f"optimus bucket should sum to 5ms (1+4); "
		f"got {by_app.get('optimus')}"
	)

	# Function names must NOT appear as bucket keys.
	for bad in ("application", "init_request", "call", "execute_cmd",
	            "record_sql", "before_request", "snapshot"):
		assert bad not in by_app, (
			f"Function name '{bad}' must not be a bucket key — "
			f"bucketing should be by app (filename), not function name. "
			f"Got by_app: {by_app}"
		)


def test_analyze_entry_point_reads_pyi_session_and_emits_findings():
	context = AnalyzeContext(session_uuid="test", docname="PS-001")
	# Pre-populate context.actions as if per_action ran
	context.actions = [
		{"action_label": "SI submit", "duration_ms": 1000, "queries_count": 0},
	]

	# Recording with a pyi tree dict and a slow hot path
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [],
		"pyi_session": {
			"function": "<root>",
			"filename": "",
			"lineno": 0,
			"self_ms": 0,
			"cumulative_ms": 1000,
			"children": [
				{
					"function": "my_app.heavy",
					"filename": "apps/my_app/h.py",
					"lineno": 1,
					"self_ms": 600,
					"cumulative_ms": 600,
					"children": [],
				},
			],
		},
	}

	result = call_tree.analyze([recording], context)
	# Should emit at least one Slow Hot Path finding
	assert any(f["finding_type"] == "Slow Hot Path" for f in result.findings)
	# Should produce hot_frames + breakdown aggregates
	assert "hot_frames" in result.aggregate
	assert "session_time_breakdown" in result.aggregate
	# Should have written call_tree_json onto the action
	assert context.actions[0].get("call_tree_json") is not None


def test_analyze_handles_missing_pyi_session():
	"""Recording with no pyi tree should not crash; produces empty tree."""
	context = AnalyzeContext(session_uuid="test", docname="PS-001")
	context.actions = [
		{"action_label": "x", "duration_ms": 100, "queries_count": 5},
	]
	recording = {
		"uuid": "rec-1",
		"calls": [{"query": "SELECT 1", "duration": 5}],
		"sidecar": [],
		"pyi_session": None,
	}
	result = call_tree.analyze([recording], context)
	# No findings (no tree, nothing to find)
	assert result.findings == []
	# But sql_total_ms still counted
	assert result.aggregate["session_time_breakdown"]["sql_ms"] == 5
	# action.call_tree_json is None
	assert context.actions[0]["call_tree_json"] is None
