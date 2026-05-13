# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: per-table time, query count, read/write split, and index hints.

Aggregates, per SQL table touched by the session:

  - total time + query count (a multi-table JOIN is counted against each
    table, so the rows sum to MORE than the session total — the renderer
    says so);
  - reads (``SELECT``) vs writes (``INSERT`` / ``UPDATE`` / ``DELETE`` /
    ``REPLACE``) — count and ms each;
  - **index candidates** — columns the session actually filtered, joined, or
    ordered on while *reading* the table, ranked by how often they were
    used. The renderer pairs this with the write count so it's clear that
    every added index also costs write time on a write-heavy table. Frappe's
    framework-managed metadata columns (``creation`` / ``modified`` / ``idx``
    / ``parent`` / ``docstatus`` / …) are never offered as candidates — they
    go into ``framework_cols_filtered`` instead, which the renderer shows as
    "also filtered on … (not suggested for indexing)". Frappe's framework
    "meta" tables (``tabDocType`` / ``tabCustom Field`` / ``tabSingles`` / …)
    get ``is_meta_table: True`` and no candidates at all — ``bench migrate``
    owns their indexes.

Informational only — no findings (``index_suggestions`` / ``explain_flags``
emit the EXPLAIN-driven findings; this is the broad "what does this table
look like" view). Uses ``sql_metadata.Parser`` (a frappe dependency) to
extract tables, query type, and per-clause columns.
"""

import re
from collections import Counter, defaultdict

from optimus.analyzers.base import (
	AnalyzerResult,
	is_frappe_meta_table,
	is_frappe_metadata_column,
	is_write_hot_table,
)

DEFAULT_TOP_N = 15
# Writes are usually cheap (a few sub-ms INSERT/UPDATE rows), so a write-target
# table easily ranks below the time cutoff — pull in the most write-active ones
# that didn't make it so a doc-save's writes are still visible.
DEFAULT_WRITE_TOP_N = 10
MAX_INDEX_CANDIDATES_PER_TABLE = 6
# A composite index wider than this is rarely worth it (the leftmost few
# columns do almost all the work); cap the recommendation.
MAX_RECOMMENDED_INDEX_COLS = 4

# sql_metadata's columns_dict clause keys → short user-facing labels. These
# are the clauses where a column reference benefits from an index.
_INDEX_CLAUSE_LABELS = {
	"where": "WHERE",
	"join": "JOIN",
	"order_by": "ORDER BY",
	"group_by": "GROUP BY",
}
_WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
# First SQL keyword, skipping any leading /* ... */ comments.
_LEADING_VERB_RE = re.compile(r"^\s*(?:/\*.*?\*/\s*)*(\w+)", re.S)
_SOURCE_ORDER = {"WHERE": 0, "JOIN": 1, "ORDER BY": 2, "GROUP BY": 3}


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	try:
		from sql_metadata import Parser  # noqa: F401
	except Exception:
		return AnalyzerResult(
			warnings=["sql_metadata not available — table breakdown skipped"]
		)

	stats: dict[str, dict] = defaultdict(lambda: {
		"duration": 0.0,
		"count": 0,
		"read_count": 0,
		"write_count": 0,
		"read_duration": 0.0,
		"write_duration": 0.0,
		"col_hits": Counter(),       # (column, source_label) -> count, reads only
		"framework_cols": set(),     # Frappe metadata cols seen in WHERE/JOIN/ORDER on reads
		# frozenset(non-metadata WHERE/JOIN columns) -> how many read queries
		# filtered on exactly that set — drives the composite recommendation.
		"combos": Counter(),
	})

	for recording in recordings:
		for call in recording.get("calls") or []:
			query = call.get("normalized_query") or call.get("query") or ""
			if not query:
				continue
			duration = float(call.get("duration", 0) or 0)
			meta = _parse_query(query)
			tables = meta["tables"]
			if not tables:
				continue
			verb = meta["verb"]
			is_read = verb == "SELECT"
			is_write = verb in _WRITE_VERBS
			for table in tables:
				s = stats[table]
				s["duration"] += duration
				s["count"] += 1
				if is_read:
					s["read_count"] += 1
					s["read_duration"] += duration
					# Index candidates come from READ queries only — that's
					# what the developer asked for ("fields to index to
					# improve reads"). Two things are NOT offered as
					# candidates: (a) Frappe's framework-managed metadata
					# columns (`modified` / `idx` / `parent` / … — written on
					# every save, or auto-indexed) — recorded in
					# framework_cols so the report can still surface "also
					# filtered on … (not suggested)"; (b) anything on a Frappe
					# framework "meta" table (tabDocType / tabCustom Field /
					# tabSingles / …) — `bench migrate` owns those tables'
					# indexes, so there's nothing to suggest there at all.
					if not is_frappe_meta_table(table):
						where_join_cols: list[str] = []
						for label, col in meta["index_cols"].get(table, ()):
							if is_frappe_metadata_column(col):
								s["framework_cols"].add(str(col).strip().lower())
								continue
							s["col_hits"][(col, label)] += 1
							if label in ("WHERE", "JOIN") and col not in where_join_cols:
								where_join_cols.append(col)
						if where_join_cols:
							s["combos"][frozenset(where_join_cols)] += 1
				elif is_write:
					s["write_count"] += 1
					s["write_duration"] += duration

	breakdown = [
		{
			"table": t,
			"duration_ms": round(s["duration"], 2),
			"queries": s["count"],
			"read_count": s["read_count"],
			"write_count": s["write_count"],
			"read_time_ms": round(s["read_duration"], 2),
			"write_time_ms": round(s["write_duration"], 2),
			"index_candidates": _rank_candidates(s["col_hits"]),
			"recommended_index": _build_recommended_index(t, s),
			"framework_cols_filtered": sorted(s["framework_cols"]),
			"is_meta_table": is_frappe_meta_table(t),
			"is_write_hot": is_write_hot_table(t),
		}
		for t, s in stats.items()
	]
	breakdown.sort(key=lambda x: x["duration_ms"], reverse=True)
	combined = breakdown[:DEFAULT_TOP_N]
	seen = {t["table"] for t in combined}
	extra_writes = sorted(
		(t for t in breakdown if t["table"] not in seen and t["write_count"] > 0),
		key=lambda x: (-x["write_count"], -x["write_time_ms"]),
	)[:DEFAULT_WRITE_TOP_N]
	combined = combined + extra_writes
	combined.sort(key=lambda x: x["duration_ms"], reverse=True)
	return AnalyzerResult(aggregate={"table_breakdown": combined})


def _build_recommended_index(table: str, s: dict) -> dict | None:
	"""A concrete composite-index recommendation for ``table`` from this
	session's read patterns, or ``None`` when there's nothing to suggest.

	The pick = the most common set of (non-metadata) WHERE/JOIN columns that
	read queries filtered on *together*, with columns ordered by overall
	usage frequency (a rough selectivity proxy) and capped to a sane
	composite width. Only for real Frappe doctype tables (``tab*``, not a
	framework meta table) — those are the ones a developer can actually
	index via a patch.
	"""
	t = str(table or "")
	if not t.lower().startswith("tab") or is_frappe_meta_table(t):
		return None
	combos = s.get("combos") or {}
	if not combos:
		return None
	# Most-used combo; tie-break: wider co-filter wins (more specific
	# pattern), then alphabetically for determinism.
	combo, together = max(
		combos.items(), key=lambda kv: (kv[1], len(kv[0]), tuple(sorted(kv[0])))
	)
	col_total: dict[str, int] = {}
	for (col, _label), n in (s.get("col_hits") or {}).items():
		col_total[col] = col_total.get(col, 0) + n
	columns = sorted(combo, key=lambda c: (-col_total.get(c, 0), c))[:MAX_RECOMMENDED_INDEX_COLS]
	chosen = set(columns)
	also = sorted(
		(c for c in col_total if c not in chosen),
		key=lambda c: (-col_total[c], c),
	)[:MAX_INDEX_CANDIDATES_PER_TABLE]
	return {
		"columns": columns,
		"doctype": t[3:],  # "tabGL Entry" -> "GL Entry"
		"together_count": together,
		"read_count": s.get("read_count", 0),
		"also_filtered": also,
	}


def _rank_candidates(col_hits: "Counter") -> list[dict]:
	"""Collapse ``(column, source_label)`` counts into one entry per column —
	``{column, sources: [...], hits}`` — ranked by total appearances, top
	``MAX_INDEX_CANDIDATES_PER_TABLE``."""
	per_col: dict[str, dict] = {}
	for (col, label), n in col_hits.items():
		entry = per_col.setdefault(col, {"column": col, "sources": set(), "hits": 0})
		entry["sources"].add(label)
		entry["hits"] += n
	ranked = sorted(per_col.values(), key=lambda e: (-e["hits"], e["column"]))
	return [
		{
			"column": e["column"],
			"sources": sorted(e["sources"], key=lambda x: _SOURCE_ORDER.get(x, 9)),
			"hits": e["hits"],
		}
		for e in ranked[:MAX_INDEX_CANDIDATES_PER_TABLE]
	]


def _leading_verb(query: str) -> str:
	m = _LEADING_VERB_RE.match(query or "")
	return m.group(1).upper() if m else ""


def _parse_query(query: str) -> dict:
	"""Parse a query once. Returns ``{tables: [...], verb: "SELECT"|...,
	index_cols: {table: [(source_label, column), ...]}}``.

	Best-effort: if ``sql_metadata`` can't parse it we return no tables, so
	the query simply doesn't appear in the breakdown — same as the
	pre-existing behaviour.
	"""
	verb = _leading_verb(query)
	try:
		from sql_metadata import Parser

		parsed = Parser(query)
		raw_tables = parsed.tables or []
	except Exception:
		return {"tables": [], "verb": verb, "index_cols": {}}

	seen: set[str] = set()
	tables: list[str] = []
	for t in raw_tables:
		if t and t not in seen:
			seen.add(t)
			tables.append(t)
	if not tables:
		return {"tables": [], "verb": verb, "index_cols": {}}

	# Refine the verb from sql_metadata when it can tell us (catches a few
	# shapes the leading-keyword regex misses, e.g. WITH ... SELECT); fall
	# back to the regex result on any hiccup.
	try:
		qt = getattr(parsed.query_type, "value", None) or str(parsed.query_type or "")
		if qt:
			verb = str(qt).upper()
	except Exception:
		pass

	index_cols: dict[str, list] = defaultdict(list)
	if verb == "SELECT":
		try:
			cdict = parsed.columns_dict or {}
		except Exception:
			cdict = {}
		single_table = tables[0] if len(tables) == 1 else None
		for clause, label in _INDEX_CLAUSE_LABELS.items():
			for raw_col in (cdict.get(clause) or []):
				attrib = _attribute_column(raw_col, tables, single_table)
				if attrib:
					tbl, col = attrib
					index_cols[tbl].append((label, col))
	return {"tables": tables, "verb": verb, "index_cols": dict(index_cols)}


def _attribute_column(raw_col, tables: list[str], single_table):
	"""Map a ``sql_metadata`` column reference (``"tabFoo.bar"`` or ``"bar"``)
	to ``(table, column)`` — or ``None`` when it can't be attributed
	confidently (an unresolved alias, an expression, or an ambiguous
	unqualified column in a multi-table query)."""
	if not isinstance(raw_col, str):
		return None
	col = raw_col.strip()
	if not col or "*" in col or "(" in col:  # SELECT *, function expressions, …
		return None
	if "." in col:
		# Frappe table names contain spaces but never dots; column names
		# never contain dots — so the first dot separates table from column.
		prefix, _, rest = col.partition(".")
		if rest and prefix in tables:
			return (prefix, rest)
		return None  # qualified by an alias we didn't resolve — don't guess
	if single_table:
		return (single_table, col)
	return None  # unqualified in a multi-table query — ambiguous, skip
