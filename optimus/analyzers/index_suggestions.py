# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: aggregated index suggestions across the session.

Wraps the existing
`frappe.core.doctype.recorder.recorder._optimize_query` (which uses the
`DBOptimizer` heuristic) to identify a single best missing index per query.
We run it across the union of unique normalized queries in the session,
then dedupe and aggregate suggestions by (table, column) so a customer
sees one suggestion per missing index — with the cumulative time it would
save and the queries that would benefit.

This is the per-session aggregation that the existing per-request
optimizer doesn't do. Per-request, you only see the index for THAT
request's queries; per-session, you see the index that would help across
the whole flow.
"""

import json
import re
from collections import Counter, defaultdict

from optimus.analyzers.base import (
	FRAPPE_METADATA_COLUMNS,
	SEVERITY_ORDER,
	AnalyzerResult,
	is_frappe_meta_table,
)


def _scrub_literals(text: str) -> str:
	"""Best-effort removal of SQL literals from a query before logging.

	The error log is typically admin-only but can sometimes be exposed
	to non-admins via permission misconfig or support workflows. Since
	this query string is going into a log that might be shared, we
	replace obvious string literals and long numeric sequences with ?
	placeholders.

	This is paranoid belt-and-suspenders — the upstream query should
	already be normalized by mark_duplicates by the time we see it, but
	if normalization was skipped or the query contained an unusual
	pattern, we scrub here too.
	"""
	if not text:
		return text
	# Replace 'single-quoted' strings
	text = re.sub(r"'[^']*'", "'?'", text)
	# Replace "double-quoted" strings (but not the identifier quotes `)
	text = re.sub(r'"[^"]*"', '"?"', text)
	# Replace long numeric sequences (likely IDs or amounts)
	text = re.sub(r"\b\d{4,}\b", "?", text)
	return text

HIGH_IMPACT_MS = 500
MEDIUM_IMPACT_MS = 100
MAX_EXAMPLE_QUERIES = 3

# v0.5.1: minimum average savings per query before a suggestion is worth
# emitting. Without this floor, the analyzer reports cases like:
#
#   "Adding an index to tabDocType.modified would speed up 1526 queries
#    in this session, saving roughly 892ms total."
#
# 892 / 1526 = 0.58ms per query — below MariaDB's per-query overhead.
# On a small framework table (tabDocType is typically ~800 rows) the
# table already fits in memory, the scan is effectively free, and
# adding a btree index yields no measurable improvement. The user
# spends time on an index migration that saves nothing.
#
# The severity thresholds (HIGH_IMPACT_MS, MEDIUM_IMPACT_MS) gate
# severity based on CUMULATIVE time, but a 2ms-per-query × 1000-query
# suggestion still crosses the 500ms cumulative threshold while being
# individually actionable — so we need a separate per-query minimum
# to distinguish "many tiny queries" from "a few slow queries."
#
# 2ms is the floor: below this, query overhead (network roundtrip +
# MariaDB dispatch) dominates over whatever time the index could save.
MIN_AVG_SAVINGS_PER_QUERY_MS = 2.0
# Log the first N failures to the Frappe Error Log; beyond that, we just
# count. Prevents the Error Log from being flooded by one bad session
# with thousands of unparseable queries.
MAX_LOGGED_FAILURES = 3

# v0.5.1: Classify exceptions from DBOptimizer / sql_metadata.
#
# sql_metadata is a third-party SQL parser with well-known limitations: it
# trips on correlated subqueries, complex ORDER BY expressions containing
# functions (if/locate/coalesce), window functions, CTEs, and a few other
# shapes Frappe apps legitimately emit. When it can't parse a query, it
# raises either ``ValueError: too many values to unpack`` (from an internal
# tuple unpacking) or ``TypeError`` (from an unexpected None in its
# token stream). These are PARSER LIMITATIONS — the user cannot rewrite
# their query to make sql_metadata happy, and they cannot add an index to
# fix a parse failure anyway.
#
# Pre-v0.5.1 the analyzer logged every parse failure to Frappe's Error
# Log and emitted a loud "Could not analyze N of M queries" warning.
# For a production session running ERPNext's item search dialog (which
# has the exact offending query shape above), this filled the Error Log
# with TypeError / ValueError tracebacks that looked like profiler bugs,
# and the warning made the user think they were missing optimization
# opportunities they could act on.
#
# v0.5.1: parser-limitation exceptions are counted but NOT logged to
# the Error Log, and the user-facing warning is softer — it explains
# that sql_metadata can't parse these shapes and there's nothing to fix.
# Real errors (AttributeError, ProgrammingError, RuntimeError, etc.)
# still go to the Error Log and still produce the loud warning, because
# those might indicate a profiler bug worth investigating.
_PARSER_LIMITATION_EXCEPTIONS = (ValueError, TypeError)

# v0.5.1: only these SQL statement types get fed to DBOptimizer. Anything
# else (BEGIN/COMMIT/SAVEPOINT/RELEASE SAVEPOINT, SET autocommit, SHOW,
# DDL, stored-procedure CALL) does not benefit from index suggestions,
# and sql_metadata raises ValueError on many of them. The pre-v0.5.1
# version fed everything to the optimizer and reported ~47% 'parse
# failures' on real sessions — most of which were transaction markers
# and connection-state statements, noise the user couldn't act on.
_OPTIMIZABLE_QUERY_TYPES = frozenset({"SELECT"})

# Leading-keyword regex tolerant of whitespace and C-style /* ... */
# comments at the start of the statement (Frappe prepends comments in
# some paths). DOTALL handles multi-line comments.
_SQL_LEADING_KEYWORD_RE = re.compile(
	r"^\s*(?:/\*.*?\*/\s*)*(\w+)",
	re.IGNORECASE | re.DOTALL,
)


def _get_query_type(sql: str) -> str:
	"""Return the leading SQL keyword in uppercase, or empty string if
	the statement doesn't start with a parseable keyword."""
	if not sql:
		return ""
	m = _SQL_LEADING_KEYWORD_RE.match(sql)
	if not m:
		return ""
	return m.group(1).upper()

# v0.5.1: prefix length for text/blob column indexes. MariaDB requires
# a length hint for TEXT/BLOB columns and for VARCHARs that would
# exceed the 767-byte index-key limit. 255 is a safe default for
# common Frappe Data / Small Text columns; developers can adjust the
# prefix length based on the actual column's selectivity profile.
TEXT_INDEX_PREFIX_LENGTH = 255

# Column types that CANNOT be usefully indexed with a simple
# single-column btree index. JSON requires a functional or generated-
# column approach, which is outside the scope of a heuristic suggestion.
_UNINDEXABLE_TYPES = frozenset({"json", "geometry"})

# Column types that REQUIRE a prefix length when indexing.
_PREFIX_REQUIRED_TYPES = frozenset({
	"text", "tinytext", "mediumtext", "longtext",
	"blob", "tinyblob", "mediumblob", "longblob",
})

# v0.5.1 → v0.6.0: never suggest indexing any of Frappe's standard
# metadata columns, even when the DBOptimizer heuristic thinks one would
# help a query. The optimizer only sees read patterns (``ORDER BY modified
# DESC`` looks like it wants an index) — it can't see the write cost.
#
# These columns are updated on every save (`modified`, `modified_by`,
# `idx`), on insert (`creation`, `owner`), on submit/cancel (`docstatus`),
# or are already auto-indexed (`name` is the PK; `parent` is auto-indexed
# on child tables). On any non-trivially-written table the maintenance cost
# of an index there dwarfs the read-side gain — and even where the per-
# insert cost would equal any other index, surfacing these nudges the
# developer toward a class of change they should make deliberately, not on
# a tool's say-so. The query's EXPLAIN row is still in the report if
# someone wants to make that call themselves.
#
# (Origin: a production report surfaced ``Add index on tabDocType(modified)``
# and the user corrected with "Modified fields can't be indexed as it would
# affect the system performance" — then later asked us to extend the rule to
# `idx`, `parent`, `parentfield`, `parenttype`, `creation`, etc.)
#
# Mirrors ``optimus.analyzers.base.FRAPPE_METADATA_COLUMNS``
# (which mirrors ``frappe.model.default_fields`` + ``optional_fields``).
_NEVER_SUGGEST_COLUMNS = FRAPPE_METADATA_COLUMNS


# v0.6.x: MariaDB doesn't accept parameterised identifiers in
# ``SHOW INDEX FROM ?`` (DDL/SCHEMA statements expect a bare token, not a
# bind value), so the only safe pattern is **strict input validation** at
# the boundary. Allowed shapes:
#   1. ``tab<DocType name>`` — the Frappe convention; DocType names may
#      contain letters, digits, spaces, underscores, hyphens.
#   2. ``information_schema.<simple-name>`` — schema views the analyzer
#      legitimately introspects.
# Anything else is rejected outright and the index-introspection helper
# returns an empty set (its "I couldn't read indexes" fall-through).
_SAFE_TAB_TABLE_RE = re.compile(r"^tab[A-Za-z0-9 _\-]+$")
_SAFE_INFOSCHEMA_RE = re.compile(r"^information_schema\.[A-Za-z0-9_]+$")


def _is_safe_table_name(name) -> bool:
	"""True iff ``name`` is one of the table-name shapes the indexer is
	allowed to inspect via raw SQL. See ``_SAFE_TAB_TABLE_RE`` /
	``_SAFE_INFOSCHEMA_RE`` for the exact grammar. Any other shape (an
	attempt at SQL injection via crafted DocType names, a stray space,
	a backtick, a semicolon, …) returns False and short-circuits the
	caller back to its empty-set fallback."""
	if not isinstance(name, str) or not name:
		return False
	# Defence in depth: reject backticks, quotes, semicolons even though
	# the regexes wouldn't match them — make the intent obvious to readers.
	if any(c in name for c in ("`", "'", '"', ";", "\\", "\n", "\r", "\x00")):
		return False
	return bool(_SAFE_TAB_TABLE_RE.match(name) or _SAFE_INFOSCHEMA_RE.match(name))


def _get_indexed_columns(table: str) -> set[str]:
	"""Return the set of columns on ``table`` that already have at
	least one index WHERE this column is the leftmost of the index key.

	Non-leftmost composite columns are NOT counted — MariaDB's btree
	can't use the index when a query filters on just that column, so
	a Missing Index suggestion is still actionable.

	Returns an empty set on any DB error (table missing, access denied,
	no real Frappe site), which conservatively keeps all suggestions.

	The table name is **interpolated** into the SQL (MariaDB doesn't
	accept parameterised identifiers in DDL), so it must pass the
	``_is_safe_table_name`` whitelist first. Anything outside the
	``tab<DocType>`` / ``information_schema.<simple>`` grammar is
	rejected without ever reaching the database.
	"""
	if not _is_safe_table_name(table):
		return set()
	try:
		import frappe
		rows = frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True) or []
	except Exception:
		return set()

	indexed: set[str] = set()
	for row in rows:
		# SHOW INDEX returns "Seq_in_index" — 1-indexed. We only count
		# columns at position 1 because a composite index
		# (a, b, c) only accelerates queries filtering on a, (a,b),
		# or (a,b,c); a query on just b or c wouldn't use it.
		try:
			seq = int(row.get("Seq_in_index") or row.get("seq_in_index") or 0)
		except (TypeError, ValueError):
			seq = 0
		if seq == 1:
			col = row.get("Column_name") or row.get("column_name")
			if col:
				indexed.add(col)
	return indexed


def _get_column_types(table: str) -> dict[str, str]:
	"""Return ``{column_name: data_type_lower}`` for ``table`` or an
	empty dict on DB error. Used to check indexability and to pick
	the right DDL shape (plain index vs. prefix index)."""
	try:
		import frappe
		rows = frappe.db.sql(
			"""
			SELECT column_name, data_type
			FROM information_schema.columns
			WHERE table_schema = DATABASE() AND table_name = %s
			""",
			(table,),
			as_dict=True,
		) or []
	except Exception:
		return {}

	out: dict[str, str] = {}
	for r in rows:
		name = r.get("column_name") or r.get("COLUMN_NAME")
		dtype = (r.get("data_type") or r.get("DATA_TYPE") or "").lower()
		if name:
			out[name] = dtype
	return out


def _classify_column(
	table: str,
	column: str,
	indexed_cache: dict[str, set[str]],
	type_cache: dict[str, dict[str, str]],
) -> tuple[str, str | None]:
	"""Classify a suggested (table, column) pair.

	Returns ``(status, ddl_or_reason)`` where status is one of:

	  - ``"actionable"``   — column exists, is not indexed, can be
	                         indexed; second element is the DDL string
	  - ``"already_indexed"`` — column exists and is already the
	                         leftmost of at least one index; drop the
	                         suggestion, second element explains
	  - ``"unindexable"``  — column is JSON / geometry or doesn't exist
	                         on the table; drop the suggestion, second
	                         element explains
	  - ``"never_suggest"`` — column is one of Frappe's standard metadata
	                         columns (``modified``, ``idx``, ``parent``,
	                         ``creation``, ``docstatus``, …) — written on
	                         every save / submit, or already auto-indexed;
	                         drop the suggestion, second element explains
	  - ``"unknown"``      — information_schema lookup failed (likely
	                         no real DB, dev environment); KEEP the
	                         suggestion with a plain DDL (legacy
	                         behavior) so we don't silently suppress
	                         the old pipeline
	"""
	# v0.5.1 → v0.6.0: hard blacklist — Frappe's standard metadata columns
	# should never be suggested, regardless of read-side heuristics. Checked
	# BEFORE the information_schema lookups so we don't waste DB roundtrips
	# on a suggestion we already know to drop. Case-insensitive defensively
	# (the optimizer normally hands us lowercase column names).
	if (column or "").strip().lower() in _NEVER_SUGGEST_COLUMNS:
		return "never_suggest", (
			f"column `{column}` on `{table}` is a Frappe framework column "
			f"(written on every save or submit, or already auto-indexed) — "
			f"not a safe index target. The query's EXPLAIN row is in the "
			f"report if you want to make this call deliberately."
		)

	# Memoize per-table lookups so a suggestion with N columns on
	# the same table only pays one round trip.
	if table not in indexed_cache:
		indexed_cache[table] = _get_indexed_columns(table)
	if table not in type_cache:
		type_cache[table] = _get_column_types(table)

	indexed = indexed_cache[table]
	types = type_cache[table]

	# Fallback: both lookups empty → we probably have no real DB
	# (unit test, pre-migrate site). Return "unknown" so the caller
	# keeps the suggestion as-is using the legacy plain-index DDL.
	if not types and not indexed:
		return "unknown", (
			f"ALTER TABLE `{table}` ADD INDEX IF NOT EXISTS `{column}_index` (`{column}`);"
		)

	# Column doesn't exist on the table.
	if types and column not in types:
		return "unindexable", (
			f"column `{column}` does not exist on table `{table}` "
			f"— the optimizer's suggestion is likely a parse error; "
			f"verify the query and try again"
		)

	# Column is already the leftmost of at least one index.
	if column in indexed:
		return "already_indexed", (
			f"column `{column}` on `{table}` is already indexed "
			f"(leftmost of an existing btree); no action needed"
		)

	dtype = types.get(column, "") if types else ""

	if dtype in _UNINDEXABLE_TYPES:
		return "unindexable", (
			f"column `{column}` on `{table}` has type `{dtype}`, "
			f"which cannot be indexed with a simple btree. Consider "
			f"a functional index or a generated column."
		)

	if dtype in _PREFIX_REQUIRED_TYPES:
		return "actionable", (
			f"ALTER TABLE `{table}` ADD INDEX IF NOT EXISTS `{column}_index` "
			f"(`{column}`({TEXT_INDEX_PREFIX_LENGTH}));"
		)

	# Regular indexable type (varchar, int, datetime, etc.) — and
	# even if we don't recognise the dtype (empty string because
	# information_schema lookup returned no row), fall through to
	# plain DDL rather than silently drop.
	return "actionable", (
		f"ALTER TABLE `{table}` ADD INDEX IF NOT EXISTS `{column}_index` (`{column}`);"
	)


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	try:
		from frappe.core.doctype.recorder.recorder import _optimize_query
	except Exception:
		return AnalyzerResult(
			warnings=["Could not import frappe.core.doctype.recorder.recorder._optimize_query"]
		)

	# Aggregate by unique normalized query so we only ask the optimizer once
	# per shape, regardless of how many times the shape was executed.
	#
	# v0.5.1: filter to SELECT statements only at aggregation time, before
	# we ever reach _optimize_query. Non-SELECT statements (BEGIN, COMMIT,
	# SAVEPOINT, SET, DDL, CALL, SHOW) can't benefit from a single-column
	# btree index suggestion and make sql_metadata raise ValueError, which
	# shows up in the report as 'Could not analyze N of M queries' noise.
	# Counting those as 'failures' misled users into thinking half their
	# queries had parse issues when actually half their queries weren't
	# optimization targets to begin with.
	unique_queries: dict[str, dict] = {}
	skipped_by_type: int = 0

	for recording in recordings:
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query")
			if not normalized:
				continue

			raw = call.get("query") or normalized
			qtype = _get_query_type(raw) or _get_query_type(normalized)
			if qtype not in _OPTIMIZABLE_QUERY_TYPES:
				skipped_by_type += 1
				continue

			entry = unique_queries.setdefault(
				normalized,
				{"duration": 0.0, "count": 0, "raw": raw},
			)
			entry["duration"] += call.get("duration", 0)
			entry["count"] += 1

	# Run the optimizer on each unique query and aggregate suggestions.
	# Track per-exception-type failures so we can surface a warning if
	# the optimizer couldn't analyze a lot of queries — otherwise the
	# customer reads "no index suggestions" as "no missing indexes"
	# when the real answer might be "we couldn't analyze your queries".
	suggestion_buckets: dict[tuple, dict] = defaultdict(
		lambda: {"duration": 0.0, "count": 0, "queries": []}
	)
	parser_limit_failures: Counter = Counter()
	real_failures: Counter = Counter()
	# Frappe framework "meta" tables (tabDocType, tabCustom Field, tabSingles,
	# tab__global_search, …) — `bench migrate` owns their schema (indexes
	# included) and they're tiny / write-on-every-customization, so we never
	# suggest an index on them. Skip before we even bucket the suggestion.
	dropped_meta_table_tables: set[str] = set()
	logged_real = 0
	for normalized, info in unique_queries.items():
		try:
			# DBOptimizer parses with sql_metadata which doesn't care about
			# literal values, so passing the normalized query is fine.
			index = _optimize_query(info["raw"])
		except _PARSER_LIMITATION_EXCEPTIONS as e:
			# Expected path for complex queries sql_metadata can't parse.
			# Count but don't log — the user can't act on these.
			parser_limit_failures[type(e).__name__] += 1
			continue
		except Exception as e:
			# Unexpected path — this might indicate a real profiler bug.
			# Count, log the first few to Error Log (so they're discoverable
			# for investigation), and continue.
			real_failures[type(e).__name__] += 1
			if logged_real < MAX_LOGGED_FAILURES:
				try:
					import frappe

					# Scrub literals defensively — the query should already
					# be normalized, but if it isn't (edge case), we don't
					# want literal customer data landing in the Error Log.
					scrubbed = _scrub_literals(normalized[:1000])
					frappe.log_error(
						title="optimus optimizer failure",
						message=f"{type(e).__name__}: {e}\n\nQuery: {scrubbed}",
					)
					try:
						from optimus import telemetry
						telemetry.emit_failure(
							"analyzers.index_suggestions.optimizer_failure", e,
							context={"exception_type": type(e).__name__},
						)
					except Exception:
						pass
				except Exception:
					pass
				logged_real += 1
			continue
		if not index or not index.table or not index.column:
			continue
		if is_frappe_meta_table(index.table):
			dropped_meta_table_tables.add(str(index.table).strip().strip("`"))
			continue
		key = (index.table, index.column)
		bucket = suggestion_buckets[key]
		bucket["duration"] += info["duration"]
		bucket["count"] += info["count"]
		if len(bucket["queries"]) < MAX_EXAMPLE_QUERIES:
			bucket["queries"].append(normalized[:300])

	# v0.5.1: verify each suggested (table, column) against
	# information_schema before emitting a finding. Pre-v0.5.1 the
	# analyzer blindly trusted the DBOptimizer heuristic and produced
	# false-positive findings for:
	#   1. columns already indexed (primary keys like `name`, framework
	#      columns `parent`/`owner`/`modified`/`creation`, and every
	#      Link / Data field with search_index: 1)
	#   2. columns with types that can't be btree-indexed (JSON, geometry)
	#   3. TEXT/BLOB columns where plain `ADD INDEX (col)` fails in
	#      MariaDB because the column exceeds the 767-byte key limit
	#   4. columns that don't exist on the table (rare, usually a
	#      sql_metadata parse error)
	#
	# We now classify each suggestion and drop / adjust accordingly.
	indexed_cache: dict[str, set[str]] = {}
	type_cache: dict[str, dict[str, str]] = {}
	drop_already_indexed = 0
	drop_unindexable = 0
	drop_low_per_query = 0
	drop_never_suggest = 0
	never_suggest_columns: list[str] = []
	unindexable_reasons: list[str] = []

	findings = []
	for (table, column), bucket in suggestion_buckets.items():
		# v0.5.1: per-query savings floor. A suggestion's total impact
		# might be high in aggregate but pointless in practice if each
		# individual query is already fast. A real production case had
		# tabDocType.modified flagged with 892ms / 1526 queries =
		# 0.58ms per query — below MariaDB's per-query overhead.
		# Indexing would not measurably help the user, so we suppress
		# the finding entirely. Checked BEFORE classify_column so we
		# don't waste SHOW INDEX / information_schema queries on
		# cases we're going to drop anyway.
		count = bucket["count"]
		if count > 0:
			avg_savings_per_query = bucket["duration"] / count
			if avg_savings_per_query < MIN_AVG_SAVINGS_PER_QUERY_MS:
				drop_low_per_query += 1
				continue

		status, ddl_or_reason = _classify_column(
			table, column, indexed_cache, type_cache
		)

		if status == "never_suggest":
			drop_never_suggest += 1
			never_suggest_columns.append(f"{table}.{column}")
			continue
		if status == "already_indexed":
			drop_already_indexed += 1
			continue
		if status == "unindexable":
			drop_unindexable += 1
			unindexable_reasons.append(ddl_or_reason)
			continue

		# status is "actionable" or "unknown" — both keep the
		# suggestion. "unknown" uses the legacy plain-DDL template,
		# matching pre-v0.5.1 behavior for environments without a
		# live DB connection.
		suggested_ddl = ddl_or_reason
		impact_ms = bucket["duration"]
		findings.append(
			{
				"finding_type": "Missing Index",
				"severity": _severity(impact_ms),
				"title": f"Add index on {table}({column})",
				"customer_description": (
					f"Adding an index to the **{column}** column of the "
					f"**{table}** table would speed up {bucket['count']} "
					f"queries in this session, saving roughly "
					f"{impact_ms:.0f}ms total. Ask your developer to add this "
					"index in a database migration."
				),
				"technical_detail_json": json.dumps(
					{
						"table": table,
						"column": column,
						"suggested_ddl": suggested_ddl,
						"affected_query_count": bucket["count"],
						"estimated_savings_ms": round(impact_ms, 2),
						"example_queries": bucket["queries"],
						"verified_not_indexed": status == "actionable",
						"validation_note": (
							"The developer must validate this suggestion against "
							"the actual schema and production query patterns "
							"before applying. The optimizer uses heuristics; an "
							"actual EXPLAIN ANALYZE on a representative query is "
							"recommended."
						),
					},
					default=str,
				),
				"estimated_impact_ms": round(impact_ms, 2),
				"affected_count": bucket["count"],
				"action_ref": "",
			}
		)

	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	warnings: list[str] = []

	# v0.5.1: Real errors (unexpected exception types from DBOptimizer) get
	# the loud "you should investigate" warning + Error Log entries.
	total_real = sum(real_failures.values())
	if total_real:
		failure_summary = ", ".join(
			f"{n}× {name}" for name, n in real_failures.most_common(3)
		)
		warnings.append(
			f"Could not analyze {total_real} of {len(unique_queries)} "
			f"SELECT statements for index suggestions ({failure_summary}). "
			"The report may be missing some optimization opportunities — "
			"see Error Log for the first few failed queries."
		)

	# v0.5.1: Parser limitations (sql_metadata can't parse this shape) get
	# a soft informational line, no Error Log noise, and explicit language
	# telling the user there's nothing to fix. Production sessions on
	# ERPNext frequently hit this for the item search dialog's complex
	# query (correlated subquery + ORDER BY if/locate/coalesce expression)
	# and a few reporting queries with window functions — neither of
	# which is user-actionable.
	total_parser_limit = sum(parser_limit_failures.values())
	if total_parser_limit:
		warnings.append(
			f"Skipped {total_parser_limit} query(ies) whose shape "
			"exceeds the DBOptimizer heuristic's sql_metadata parser "
			"(correlated subqueries, complex ORDER BY expressions, window "
			"functions). These aren't actionable — index suggestions "
			"require a simpler WHERE/JOIN shape the parser can analyze."
		)
	# v0.5.1: separate informational line about non-SELECT statements that
	# were deliberately skipped, so users understand the distinction between
	# 'we tried to analyze and failed' (above) and 'we didn't try because
	# the statement type isn't optimizable' (below). Not a warning — more
	# of a 'here's what the 705-query number breaks down to.'
	if skipped_by_type:
		warnings.append(
			f"Skipped {skipped_by_type} non-SELECT statement(s) for index "
			"suggestions (BEGIN/COMMIT/SAVEPOINT/SET/DDL/CALL/SHOW). "
			"These don't benefit from single-column btree index suggestions "
			"and aren't counted as analysis failures."
		)
	if drop_low_per_query:
		warnings.append(
			f"Suppressed {drop_low_per_query} index suggestion(s) with "
			f"average savings below {MIN_AVG_SAVINGS_PER_QUERY_MS:g}ms "
			"per query. These queries are already running near MariaDB's "
			"per-query overhead floor — an index wouldn't measurably help."
		)
	if drop_never_suggest:
		# Include the specific columns in the warning so the user can
		# confirm it matches their expectations and doesn't look like
		# the analyzer silently dropped useful suggestions.
		sample = ", ".join(sorted(set(never_suggest_columns))[:5])
		overflow = max(0, len(set(never_suggest_columns)) - 5)
		more = f" (+{overflow} more)" if overflow else ""
		warnings.append(
			f"Suppressed {drop_never_suggest} index suggestion(s) on "
			f"Frappe metadata columns ({sample}{more}). These columns are "
			"written on every save or submit (or are already auto-indexed), "
			"so an index there is a write-cost trap — not suggested."
		)
	if dropped_meta_table_tables:
		sample = ", ".join(sorted(dropped_meta_table_tables)[:5])
		overflow = max(0, len(dropped_meta_table_tables) - 5)
		more = f" (+{overflow} more)" if overflow else ""
		warnings.append(
			f"Skipped index suggestion(s) on Frappe framework meta tables "
			f"({sample}{more}). `bench migrate` owns those tables' schema "
			"(indexes included) — a hand-added index there is pointless and "
			"would be clobbered on the next migrate."
		)
	if drop_already_indexed:
		warnings.append(
			f"Suppressed {drop_already_indexed} index suggestion(s) whose "
			"target column was already indexed (would have been a false "
			"positive — the DBOptimizer heuristic doesn't check existing "
			"indexes)."
		)
	if drop_unindexable:
		# Keep the warning short but log the reasons so curious users
		# can check Error Log for the exact column/type mismatches.
		warnings.append(
			f"Suppressed {drop_unindexable} index suggestion(s) whose "
			"target column can't be btree-indexed directly (JSON / "
			"nonexistent / etc.)."
		)
		try:
			import frappe
			for reason in unindexable_reasons[:5]:
				frappe.log_error(
					title="optimus index_suggestions dropped",
					message=reason,
				)
				try:
					from optimus import telemetry
					telemetry.emit_failure(
						"analyzers.index_suggestions.dropped",
						context={"reason": (reason or "")[:200]},
					)
				except Exception:
					pass
		except Exception:
			pass

	return AnalyzerResult(findings=findings, warnings=warnings)


def _severity(impact_ms: float) -> str:
	if impact_ms > HIGH_IMPACT_MS:
		return "High"
	if impact_ms > MEDIUM_IMPACT_MS:
		return "Medium"
	return "Low"
