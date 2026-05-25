# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Call-tree panel renderer — the hierarchical "where did wall-clock time go"
section of the safe report.

Sourced from the slowest action's ``call_tree_json`` (built by the
analyzer); rendered as nested ``<details>`` elements with auto-open
breadcrumb down to the user-app's first hot frame and depth-capped
expanders past ``_CALL_TREE_MAX_DEPTH``. Synthetic placeholder nodes
(``[other: N frames]``, ``[N more frames omitted]``) and ``<sql>``
query leaves are dropped from the visible tree per user request — the
queries live in their own table-shaped sections; this panel shows only
the Python hierarchy.

Extracted from ``_internal.py`` in v0.12.8 per the v0.10.0 renderer-
package roadmap (``optimus/renderer/README.md``). Self-contained
cluster: only call-graph dependency is ``optimus.analyzers.base.
FRAMEWORK_APPS`` (lazily imported inside ``_ct_is_user_frame`` to
avoid the import-time cycle through ``optimus.analyzers``).
"""

from __future__ import annotations

import json
import re

# Depth caps for the call-tree panel. The default cap is what the user
# sees without clicking; the hard cap is the absolute runaway-protection
# ceiling beyond which children are silently truncated.
_CALL_TREE_MAX_DEPTH = 12
_CALL_TREE_HARD_CAP = 64

_CT_OTHER_RE = re.compile(
	r"^\[(?:other: \d+ frames?|\d+ more frames? omitted)\]$"
)


def _e(text: object) -> str:
	"""HTML-escape. Local copy of ``_internal._e`` — keeps this
	submodule free of a back-reference into ``_internal.py`` (which
	would create a circular import once ``_internal`` re-imports the
	call-tree symbols)."""
	import html as _html

	return _html.escape("" if text is None else str(text))


def _ct_is_other_frame(fn) -> bool:
	"""A synthetic call-tree collapse node — either ``[other: N frames]`` or the
	analyzer's deep-tree pruning placeholder ``[N more frames omitted]``
	(call_tree.py). Both are dropped from the call tree per user request: they
	carry no callsite to act on, so they're just noise."""
	return bool(_CT_OTHER_RE.match((fn or "").strip()))


def _ct_is_sql_leaf(node) -> bool:
	"""A ``<sql>`` query leaf frame. Dropped from the call-tree display per
	user request — the tree shows only the Python hierarchy; the queries
	themselves live, itemised, in the Slowest-queries / per-action sections,
	so nothing is lost. (The analyzer still keeps these in ``call_tree_json``.)"""
	cn = node or {}
	return cn.get("function") == "<sql>" and not cn.get("children")


def _ct_is_user_frame(node) -> bool:
	"""A real user-app python frame (not framework, not a synthetic
	``<sql>`` / ``[other]`` / ``<root>`` node). Used to auto-open the tree
	down to the first user-app frame."""
	fn = node.get("function") or ""
	if not fn or fn.startswith("<") or fn.startswith("["):
		return False
	fname = (node.get("filename") or "").replace("\\", "/")
	app = fname.split("/", 1)[0] if fname else ""
	if not app:
		return False
	try:
		from optimus.analyzers.base import FRAMEWORK_APPS
	except Exception:
		FRAMEWORK_APPS = frozenset()
	return app not in FRAMEWORK_APPS


def _render_call_tree_node(node, parent_ms, depth=0, unlimited=False, breadcrumb=True):
	"""Phase K.5: recursive nested-``<details>`` emit for a single
	call_tree node. Auto-opens the hottest path down to the first user-app
	frame (``breadcrumb``); deeper branches start collapsed so the panel
	doesn't unfurl into thousands of frames on first paint.

	Past ``_CALL_TREE_MAX_DEPTH`` the remaining subtree is wrapped in
	a click-to-expand ``<details>`` so users can traverse deeper when
	they want to, with ``unlimited=True`` flipped on for that subtree
	so we don't keep nesting expanders at every level. ``_CALL_TREE_
	HARD_CAP`` is the absolute ceiling for runaway protection.
	"""
	if not isinstance(node, dict):
		return ""
	fn = node.get("function") or "<?>"
	# v0.7.x: drop synthetic "[other: N frames]" collapse nodes entirely (user
	# request — accepts that a branch's visible children may not sum to its total).
	if _ct_is_other_frame(fn):
		return ""
	file = node.get("filename") or ""
	lineno = node.get("lineno") or ""
	cum_ms = float(node.get("cumulative_ms") or 0)
	self_ms = float(node.get("self_ms") or 0)
	children = node.get("children") or []

	pct = (cum_ms / parent_ms * 100.0) if parent_ms else 0.0
	cls = "call-tree-node"
	if parent_ms and cum_ms / parent_ms >= 0.5:
		cls += " call-tree-hot"

	# v0.7.x: auto-open the hottest path down to the first user-app frame so the
	# tree "opens at" the user's code; collapse below it.
	is_user = _ct_is_user_frame(node)
	open_attr = " open" if breadcrumb else ""
	meta_lineno = f":{lineno}" if lineno else ""
	pct_label = f" &middot; {pct:.0f}%" if parent_ms else ""
	self_label = ""
	if self_ms and cum_ms - self_ms > 1:
		self_label = f" &middot; self {self_ms:.0f}ms"

	out = [
		f'<details class="{cls}"{open_attr}>',
		'<summary>',
		f'<span class="frame-name">{_e(fn)}</span>',
		f'<span class="frame-meta">{_e(file)}{meta_lineno} &middot; '
		f'{cum_ms:.0f}ms{pct_label}{self_label}</span>',
		'</summary>',
	]
	if children:
		# Drop [other: N frames] synthetic nodes AND <sql> query leaves (the
		# call tree is the Python hierarchy; per-query rows belong in the
		# Slowest-queries / DB-tables sections), then order hottest-first.
		main = sorted(
			[
				c for c in children
				if not _ct_is_other_frame((c or {}).get("function"))
				and not _ct_is_sql_leaf(c)
			],
			key=lambda c: float((c or {}).get("cumulative_ms") or 0),
			reverse=True,
		)

		within_default = unlimited or depth < _CALL_TREE_MAX_DEPTH
		within_hard = depth < _CALL_TREE_HARD_CAP

		if within_default and within_hard:
			out.append('<div class="call-tree-children">')
			for idx, c in enumerate(main):
				# Continue the auto-open breadcrumb down the single hottest path
				# until we reach a user-app frame; collapse once we're there.
				child_bc = (
					breadcrumb and not is_user and idx == 0
					and depth < _CALL_TREE_MAX_DEPTH
				)
				out.append(_render_call_tree_node(
					c, cum_ms, depth + 1, unlimited, breadcrumb=child_bc,
				))
			out.append('</div>')
		elif within_hard:
			# Past default cap — click-to-expand the rest of the
			# subtree. ``unlimited=True`` prevents further wrapping
			# at every nested level.
			out.append(
				'<div class="call-tree-children call-tree-deeper">'
				'<details class="call-tree-deeper-toggle">'
				'<summary>'
				f'<em>show {len(main)} deeper frame(s) &middot; '
				f'depth {depth + 1}+</em>'
				'</summary>'
				'<div class="call-tree-children">'
			)
			for c in main:
				out.append(_render_call_tree_node(
					c, cum_ms, depth + 1, unlimited=True, breadcrumb=False,
				))
			out.append('</div></details></div>')
		else:
			# depth >= HARD_CAP; absolute truncation as safety net.
			out.append(
				'<div class="call-tree-children call-tree-truncated">'
				f'<em>... {len(children)} child frame(s) hidden '
				f'(hard cap {_CALL_TREE_HARD_CAP} reached) ...</em>'
				'</div>'
			)
	out.append('</details>')
	return "".join(out)


def _render_call_tree_panel(actions):
	"""Phase K.5: render the call-tree panel sourced from the slowest
	action's ``call_tree_json``. Empty string when no action carries
	a tree (the template's ``{% if %}`` guard hides the section).
	"""
	if not actions:
		return ""
	# Pick the action with the largest duration_ms that also has a tree.
	candidates = [a for a in actions if isinstance(a, dict) and a.get("call_tree_json")]
	if not candidates:
		return ""
	top = max(candidates, key=lambda a: float(a.get("duration_ms") or 0))
	try:
		tree = json.loads(top.get("call_tree_json") or "{}")
	except Exception:
		return ""
	if not isinstance(tree, dict):
		return ""
	root_children = tree.get("children") or []
	if not root_children:
		return ""

	total_ms = float(tree.get("cumulative_ms") or 0) or float(top.get("duration_ms") or 0)
	action_label = top.get("action_label") or ""

	parts = [
		'<section class="section" id="call-tree">',
		'<div class="section-head">',
		'<h2>Call tree (top action)</h2>',
		f'<span class="section-tag">{_e(action_label)}</span>',
		'</div>',
		'<p class="section-intro">'
		'Hierarchical breakdown of where wall-clock time went inside the slowest '
		'action. The tree auto-opens down to your app\'s first hot frame; click any '
		'frame to expand or collapse it. Numbers are cumulative time (including '
		'children) and percentage of the parent. Branches consuming &ge;50% of their '
		'parent are highlighted as hot.'
		'</p>',
		'<div class="call-tree">',
	]
	root_children_sorted = sorted(
		root_children,
		key=lambda c: float((c or {}).get("cumulative_ms") or 0),
		reverse=True,
	)
	# The profiler attributes SQL queries as root-level siblings of the entry
	# frame, so the panel renders them directly (bypassing the per-node child
	# loop). Drop [other] nodes and <sql> query leaves here too.
	for c in root_children_sorted:
		cn = c or {}
		if _ct_is_other_frame(cn.get("function")) or _ct_is_sql_leaf(cn):
			continue
		parts.append(_render_call_tree_node(c, total_ms, depth=0))
	parts.append('</div>')
	parts.append('</section>')
	return "".join(parts)
