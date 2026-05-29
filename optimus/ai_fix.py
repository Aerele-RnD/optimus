# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""On-demand LLM-suggested fixes for Optimus Findings.

The profiler already pins each finding to a callsite, a source snippet, and
(for query findings) normalized SQL + EXPLAIN. This module turns that
context into a concrete fix by asking a configured LLM. It is invoked only
from the ``optimus.api.suggest_fix`` whitelisted endpoint (request
context) — never from an analyzer or from ``analyze.py``, so the
pure-analyzer / frozen-capture invariants are untouched.

Provider-agnostic by design (self-hosted thesis): two wire formats —
Anthropic Messages (``/v1/messages``) and OpenAI Chat Completions
(``/chat/completions``) — with a ``ai_provider`` Select that picks the
protocol plus a sensible default endpoint/model. ``ai_base_url`` /
``ai_model`` / ``ai_api_key`` are all overridable in Optimus Settings, so a
local model (Ollama / LM Studio / vLLM) can be used and nothing has to leave
the box.

``frappe`` is imported lazily inside each function (mirrors ``settings.py``)
so the pure helpers — ``_build_messages`` and the ``_call_*`` /
``_http_post`` HTTP layer with ``requests`` mocked — are unit-testable
without a bench. ``requests`` is bundled by Frappe; it's declared explicitly
in ``pyproject.toml`` since this module imports it directly.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import requests


class AiFixError(Exception):
	"""User-facing error from the AI-fix path. The API endpoint converts
	this into ``frappe.throw`` so the message is shown to the operator."""


# Findings that carry enough code / SQL context for the LLM to reason about
# a concrete fix. Infra / frontend / "function not invoked" findings are
# excluded — the LLM would only get a title + a couple of numbers.
#
# v0.7.x: Slow Hot Path / Hook Bottleneck / Repeated Hot Frame removed.
# Their AI suggestions are structurally generic (the LLM only sees a
# function name + percentage + line range) and the actionable insight
# already lives on the embedded N+1 / Hot Line / Redundant Call that
# shares the same chain leaf. Skipping these types saves tokens without
# losing diagnostic signal — the broader hot-path findings still appear
# in the Findings section with their smoking-gun + drill-down; they
# just no longer carry an LLM-rendered "Suggested fix" block.
AI_ELIGIBLE_FINDING_TYPES: frozenset[str] = frozenset({
	"N+1 Query",
	"Framework N+1",
	"Slow Query",
	"Missing Index",
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
	"Redundant Call",
	"Hot Line",
})

# Per-provider protocol + sensible defaults. ``ai_base_url`` / ``ai_model``
# from Optimus Settings override these; the "OpenAI-compatible" provider
# REQUIRES both (no hosted default to fall back to).
_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
	"Anthropic": {
		"protocol": "anthropic",
		"base_url": "https://api.anthropic.com",
		"model": "claude-sonnet-4-6",
		"needs_key": True,
	},
	"OpenAI": {
		"protocol": "openai",
		"base_url": "https://api.openai.com/v1",
		"model": "gpt-4.1-mini",
		"needs_key": True,
	},
	"Kimi (Moonshot)": {
		"protocol": "openai",
		"base_url": "https://api.moonshot.ai/v1",
		"model": "kimi-k2-0905-preview",
		"needs_key": True,
	},
	"OpenAI-compatible": {
		"protocol": "openai",
		"base_url": "",
		"model": "",
		# Local endpoints (Ollama / LM Studio / vLLM) usually need no key.
		"needs_key": False,
	},
}
_DEFAULT_PROVIDER = "Anthropic"

# Fallback timeout when settings can't be read (pure-pytest path with no
# bench, or a settings cache miss during early bootstrap). v0.9.0+ the live
# value comes from cfg.ai_request_timeout_seconds (clamped 10–600s).
_HTTP_TIMEOUT = 60            # seconds; one shot, no retries
_MAX_OUTPUT_TOKENS = 2000
_SOURCE_LINES_BEFORE = 24     # how much code window the caller should gather
_SOURCE_LINES_AFTER = 24
_MAX_SOURCE_WINDOW_LINES = 80
_MAX_QUERY_CHARS = 2400
_MAX_USER_CONTENT_CHARS = 18000
# Low temperature — we want the model to stick to the code it was shown, not
# get "creative". OpenAI's o-series reasoning models reject a non-default
# temperature, so it's omitted for those (see `_is_reasoning_model`).
_TEMPERATURE = 0.1

_ANTHROPIC_VERSION = "2023-06-01"

# Compact "what this finding type means + how it's usually fixed in Frappe"
# line, injected into the user message. Keeps the system prompt general and
# gives the model a strong, type-specific starting point.
_FINDING_TYPE_HINTS = {
	"N+1 Query": "A query repeats once per row of an outer loop. Fix: lift it out of the loop and batch — one `frappe.get_all(<DocType>, filters={'name': ('in', names)}, fields=[...])` (or `frappe.db.get_values`) then build a dict keyed by the join column.",
	"Framework N+1": "Same N+1 pattern, but the loop is inside framework code (frappe/erpnext). Fix: change YOUR calling pattern so the framework isn't invoked per-row — e.g. pass a list of names where the API accepts one, fetch needed fields up front, or avoid `get_doc` in a loop.",
	"Slow Query": "A single SQL statement is slow. Fix: add the right index (Frappe way: Customize Form → the field → tick 'Search Index'; raw `ALTER TABLE … ADD INDEX` only if you can't customize), or restructure the WHERE/ORDER BY so an existing index is usable, or reduce the rows touched (tighter filters, fewer columns).",
	"Missing Index": "A WHERE/JOIN/ORDER BY column has no usable index. Fix: add a Search Index (Customize Form → field → 'Search Index') — prefer a composite index when several columns are filtered together; only fall back to `ALTER TABLE … ADD INDEX (...)` if customization isn't an option.",
	"Full Table Scan": "EXPLAIN shows `type=ALL` — the whole table is read. Fix: index the filtering column (Search Index), or make the WHERE sargable (no functions on the column, no leading-wildcard LIKE).",
	"Filesort": "EXPLAIN shows `Using filesort` — MariaDB sorts the result set in memory/on disk. Fix: a composite index ending in the ORDER BY column(s) so the read returns rows already ordered; or, if the sort isn't needed, drop the ORDER BY.",
	"Temporary Table": "EXPLAIN shows `Using temporary` — usually a GROUP BY / DISTINCT that can't use an index. Fix: a covering composite index on the grouped columns, or pre-aggregate, or drop an unnecessary DISTINCT.",
	"Low Filter Ratio": "EXPLAIN's `filtered` is low — the index (if any) isn't selective; most examined rows are thrown away. Fix: index a more selective column, add a composite index matching the WHERE, or tighten the filter.",
	"Redundant Call": "The same `get_doc` / cache lookup / `has_permission` runs many times for the same arguments from one callsite. Fix: hoist it out of the loop, or memoize for the request — stash on `frappe.local` (request-scoped) or `frappe.cache().get_value(key)` / `set_value(key, val, expires_in_sec=…)` for cross-request.",
	"Slow Hot Path": "A subtree of the call tree dominates the action's wall time. Fix: look at what that function does — fetching data it doesn't need, doing per-row work that could be batched, recomputing something cacheable — and remove/defer/batch it. `frappe.enqueue(...)` if it's work that doesn't need to block the response.",
	"Hook Bottleneck": "A `doc_events` / `before_*` / `after_*` hook is expensive and runs on every save/submit. Fix: make the hook do less (skip when nothing relevant changed — check `doc.has_value_changed(...)`), or move heavy work to `frappe.enqueue(...)` so it doesn't block the save.",
	"Repeated Hot Frame": "The same function shows up many times in the sampled stacks — it's called a lot. Fix: reduce call count (batch / cache) or make each call cheaper.",
	"Hot Line": "A single source line is the dominant time sink inside its function. Fix: optimize that line specifically — hoist invariant work out of a loop, replace an O(n²) pattern, avoid a per-iteration DB/cache hit, or use a set/dict for membership tests.",
}

_SYSTEM_PROMPT = (
	"You are a senior Frappe Framework / ERPNext engineer doing a precise code "
	"review of one finding from a performance profiler. Propose the smallest "
	"concrete, Frappe-idiomatic change that fixes the ROOT CAUSE — not generic "
	"advice, not a rewrite.\n\n"

	"WRITE IDIOMATIC FRAPPE. Use `frappe.get_all` / `frappe.get_list` / "
	"`frappe.db.get_value` / `frappe.db.get_values` / `frappe.qb` (the query "
	"builder) — never hand-built SQL strings and never an ORM call inside a "
	"loop. Per-request memoization goes on `frappe.local`; cross-request "
	"caching goes through `frappe.cache().get_value(key)` / "
	"`set_value(key, value, expires_in_sec=...)`. Background work that needn't "
	"block the response goes through `frappe.enqueue(...)`. Adding an index "
	"means: Customize Form → the field → tick **Search Index** (which `bench "
	"migrate` then creates) — only fall back to a raw `ALTER TABLE ... ADD "
	"INDEX (...)` when customization genuinely isn't an option, and prefer a "
	"single composite index over several single-column ones when the same "
	"columns are filtered together.\n\n"

	"NO RAW SQL IN YOUR PROPOSED FIX. `frappe.db.sql(\"SELECT ...\")` / "
	"`\"INSERT ...\"` / `\"UPDATE ...\"` / `\"DELETE ...\"` / `\"REPLACE ...\"` "
	"is forbidden in your `+` lines (proposed code) — even when the `before` "
	"code being REPLACED was raw SQL. The replacement MUST use one of the "
	"framework APIs above: `frappe.get_all` for typical reads / lists; "
	"`frappe.qb` for joins, aggregations, dynamic conditions or anything the "
	"Document API can't express; `frappe.db.get_value` / `frappe.db.get_values` "
	"for single-row / dict-shape lookups. If you genuinely cannot map the SQL "
	"to a framework call without breaking semantics, say so plainly in "
	"**Diagnosis** and leave the SQL in place (recommend caching / hoisting / "
	"adding an index / changing the query shape instead). Narrow exception: "
	"DDL via raw SQL — `ALTER TABLE ... ADD INDEX ...` / `CREATE INDEX ...` "
	"— is acceptable when an index recommendation truly can't go through "
	"Customize Form's Search Index toggle.\n\n"

	"GROUND EVERYTHING IN THE CODE YOU WERE SHOWN — DO NOT INVENT CODE. The "
	"only source you have is what appears under \"Source around the callsite\" "
	"in the user message (if anything appears there at all). Treat every other "
	"line of code as unknown to you. Hard rules:\n"
	"  • If your **Fix** shows a \"before\" snippet — or `-` lines in a "
	"```diff``` block — every one of those lines MUST be copied VERBATIM from "
	"the shown source: identical text, and keep its line number. Do NOT "
	"reconstruct, paraphrase, summarise, or imagine what the code \"probably\" "
	"looks like. A `for … in …:` loop, a `frappe.get_doc(...)` call, a "
	"variable name — if you weren't shown it, you don't get to write it as if "
	"you were.\n"
	"  • If the loop / call / WHERE / line this finding is actually about is "
	"NOT visible in the shown source (or no source was shown at all), then you "
	"do NOT have the offending code. In that case: in **Diagnosis** say so "
	"plainly (\"the offending code isn't in the window I was shown — it's "
	"likely in `<name>`\"), and in **Fix** give ONLY a short directional "
	"recommendation, explicitly framed as \"without seeing the code, the likely "
	"fix is …\". NO before/after snippet, NO diff, NO fabricated code block.\n"
	"  • Never present a guess as a verified fix. If you're not certain a "
	"symbol exists, don't use it — or mark it clearly as an assumption.\n"
	"  • SQL substitution discipline: if your **Fix** replaces a raw SQL "
	"string with `frappe.get_all` / `frappe.get_list` / `frappe.db.get_value` "
	"/ `frappe.db.get_values` / `frappe.qb`, the new call MUST be "
	"semantically equivalent to the SQL it replaces — same table(s), same "
	"WHERE / JOIN / GROUP BY / ORDER BY / LIMIT, same field list. If the SQL "
	"had no WHERE clause, the replacement gets no `filters=`. If the SQL had "
	"`LIMIT N`, the replacement gets `limit=N`. Do NOT invent filters by "
	"copying a variable that appears elsewhere in the function "
	"(e.g. `frappe.session.user`), and NEVER synthesise list shapes like "
	"`[some_var] * N` to fit an `('in', ...)` filter — that is hallucination, "
	"not refactoring. If you cannot preserve semantics, say so plainly in "
	"**Diagnosis** and leave the SQL as-is (recommend caching / hoisting / "
	"adding an index instead).\n\n"

	"NEVER suggest indexing Frappe's standard metadata columns — `name`, "
	"`idx`, `parent`, `parentfield`, `parenttype`, `creation`, `modified`, "
	"`modified_by`, `owner`, `docstatus`, `doctype`, `_user_tags`, "
	"`_comments`, `_assign`, `_liked_by`, `_seen` — nor any of Frappe's "
	"framework meta tables (`tabDocType`, `tabDocField`, `tabCustom Field`, "
	"`tabProperty Setter`, `tabSingles`, `tabSeries`, `tab__global_search`, "
	"workspace/dashboard config tables, …). Frappe writes the former on every "
	"save (or they're already indexed); `bench migrate` owns the latter's "
	"schema. If the only index you can think of targets one of those, say "
	"there's no good index-side fix and propose a query-shape change instead.\n\n"

	"OUTPUT — Markdown, exactly these four headings, nothing before or after:\n"
	"**Diagnosis** — 1-2 sentences: the actual cause, referring to the shown "
	"source by line number when you can (e.g. \"the `frappe.get_doc(...)` on "
	"line 14 runs once per item — N round-trips\"). If the offending code "
	"wasn't shown to you, say that here.\n"
	"**Fix** — the concrete change, using real Frappe APIs. Only if the "
	"offending code is in the source you were shown: present it as a unified "
	"diff in a ```diff fenced block (preferred — it renders with before/after "
	"highlighting; `-` lines = the existing code copied verbatim from the "
	"source above, `+` lines = the replacement). Otherwise: NO snippet/diff — "
	"just the directional recommendation (\"without seeing the code, the likely "
	"fix is …\"). For an index, give the Customize Form path AND (only as a "
	"fallback) the `ALTER TABLE` DDL — that's a config change, not invented "
	"code, so it's fine without a source window.\n"
	"**Why it works** — 1-2 sentences tying the change to the cause.\n"
	"**Verify** — 1 line: how to confirm it worked (re-profile the same flow "
	"and check the relevant number dropped — query count / wall time / EXPLAIN).\n\n"

	"Keep the whole answer focused — roughly 150-350 words. Do not restate the "
	"finding's title or numbers back at the reader.\n\n"

	"EXAMPLE — this is ONLY to show the heading shape and the verbatim-before "
	"discipline. It happens to be an N+1; that does NOT mean your finding is an "
	"N+1 — most aren't. Match YOUR finding type and YOUR shown code, and if "
	"your source window doesn't contain a loop like this one, do NOT produce a "
	"diff like this one:\n"
	"**Diagnosis** — `frappe.db.get_value('Item', d.item_code, 'stock_uom')` "
	"on line 12 runs once per row of `self.items` — that's the N+1.\n"
	"**Fix**\n"
	"```diff\n"
	"-for d in self.items:\n"
	"-    uom = frappe.db.get_value('Item', d.item_code, 'stock_uom')\n"
	"-    ...\n"
	"+uoms = {r.name: r.stock_uom for r in frappe.get_all(\n"
	"+    'Item', filters={'name': ('in', [d.item_code for d in self.items])},\n"
	"+    fields=['name', 'stock_uom'])}\n"
	"+for d in self.items:\n"
	"+    uom = uoms.get(d.item_code)\n"
	"+    ...\n"
	"```\n"
	"**Why it works** — one batched `frappe.get_all` replaces N per-row "
	"queries; the dict lookup is in-memory.\n"
	"**Verify** — re-record the same Save and confirm the `tabItem` query "
	"count for this action dropped from ~N to 1.\n\n"

	"SECOND EXAMPLE — same heading shape, this time showing the "
	"SQL-equivalence rule: a raw SQL with NO WHERE clause maps to a "
	"`frappe.get_all` with NO `filters=`. Notice the replacement preserves "
	"exactly the original table, fields, and LIMIT — nothing is invented:\n"
	"**Diagnosis** — line 207 runs a raw `SELECT name, email FROM `tabUser` "
	"LIMIT 50` which can be replaced with the framework-idiomatic call.\n"
	"**Fix**\n"
	"```diff\n"
	"-users = frappe.db.sql(\"SELECT name, email FROM `tabUser` LIMIT 50\", as_dict=True)\n"
	"+users = frappe.get_all('User', fields=['name', 'email'], limit=50)\n"
	"```\n"
	"**Why it works** — `frappe.get_all` is the framework-idiomatic shape; "
	"same table, same fields, same LIMIT, so the result set is identical.\n"
	"**Verify** — diff the row count returned by the new call vs. the old "
	"`frappe.db.sql` and confirm they match.\n"
)


_STEPS_SYSTEM_PROMPT = (
	"You are a senior ERPNext / Frappe Framework functional + technical expert "
	"— you know every standard ERPNext document flow cold and you know exactly "
	"which Desk UI gesture produces which HTTP call. Your job: write the "
	"\"Steps to Reproduce\" section of a performance report. You're given the "
	"ordered list of HTTP actions a user performed during a profiling session "
	"(a humanized label, the raw `cmd`/path, the DocType when known, and how "
	"long each took). Infer what the user was actually DOING and rewrite it as "
	"clear, friendly steps a developer or QA could follow to reproduce the "
	"same flow in the Desk UI.\n\n"

	"WHAT THE RAW CALLS MEAN (use this to decode the trace):\n"
	"  • `frappe.desk.form.save.savedocs` / `frappe.client.save` / `.insert` — "
	"the user clicked **Save** on a form. If the action is \"Submit\" it was "
	"the **Submit** button; \"Cancel\" → **Cancel**; a new (`__islocal`) doc → "
	"they had clicked **New** first. `frappe.client.submit` / `.cancel` / "
	"`.delete` are the same buttons hit programmatically.\n"
	"  • `run_doc_method` / `runserverobj` — the user clicked a button on a "
	"form: a **Create ▸ <Target>** mapping (e.g. Sales Order → Delivery Note / "
	"Sales Invoice, Purchase Order → Purchase Receipt, Quotation → Sales "
	"Order), or a custom Action button. The humanized label tells you which "
	"(\"Make Delivery Note on Sales Order SO-0001\" → they clicked Create ▸ "
	"Delivery Note on that Sales Order).\n"
	"  • `frappe.model.workflow.apply_workflow` — the user clicked a **workflow "
	"action** button (Approve / Reject / Submit for Approval / …).\n"
	"  • `frappe.desk.search.search_link` / `frappe.client.get_list` from a "
	"form — the user was typing into a Link field (picking a Customer, Item, "
	"etc.) — that's part of \"fill in the form\", not its own step.\n"
	"  • `frappe.desk.reportview.get` / `frappe.client.get_count` — opening a "
	"**List view** of that DocType. `frappe.desk.query_report.run` — running a "
	"**Query/Script Report**. `frappe.desk.form.load.getdoc` — **opening an "
	"existing record**.\n\n"

	"ERPNEXT FLOWS YOU KNOW (recognise these chains and name them):\n"
	"  • Selling: Lead → Opportunity → Quotation → Sales Order → Delivery Note "
	"→ Sales Invoice → Payment Entry.\n"
	"  • Buying: Material Request → Request for Quotation → Supplier Quotation → "
	"Purchase Order → Purchase Receipt → Purchase Invoice → Payment Entry.\n"
	"  • Stock: Stock Entry (Material Receipt / Issue / Transfer / Manufacture), "
	"Stock Reconciliation, Pick List, Delivery Note, Purchase Receipt.\n"
	"  • Manufacturing: BOM → Work Order → Job Card → Stock Entry "
	"(Manufacture) → completion.\n"
	"  • Accounts: Journal Entry, Payment Entry, Sales/Purchase Invoice, "
	"Bank Reconciliation, Period Closing Voucher.\n"
	"  • HR/Payroll: Employee → Attendance / Leave Application → Salary "
	"Structure Assignment → Payroll Entry → Salary Slip.\n"
	"  • Projects: Project → Task → Timesheet → Sales Invoice.\n"
	"If the trace walks one of these, say so (\"Create a Sales Order from the "
	"Quotation, then make a Delivery Note from it\").\n\n"

	"RULES:\n"
	"  • Collapse mechanical multi-call sequences into ONE human step — a form "
	"load + a few Link-field lookups + a save is just \"Create a Sales Invoice "
	"with a customer and at least one item, then Save\", not five steps. "
	"Background / polling calls (realtime permission checks, notification "
	"counts, list counters, asset loads, bare form-metadata loads) are noise — "
	"ignore them entirely.\n"
	"  • Use Desk UI language: \"Go to the <DocType> list\", \"Click New\", "
	"\"Fill in <fields> and Save\", \"Submit it\", \"Open <DocType> <name>\", "
	"\"Click **Create ▸ <Target>**\", \"Click the <Action> button\", \"Run the "
	"<Report> report\". Name the DocType whenever you can tell what it was.\n"
	"  • Do NOT invent data you weren't given — write \"with at least one item "
	"row\", not \"with item WIDGET-001\". If an action's purpose genuinely "
	"isn't clear from the label/cmd, describe it neutrally (\"Call the "
	"<method> endpoint on <DocType>\") rather than guessing a UI gesture.\n"
	"  • Keep it tight — usually 2 to 6 steps. Don't restate timings; the "
	"report shows those separately. Don't add commentary about performance or "
	"what's slow.\n\n"

	"OUTPUT — a Markdown ordered list of the steps to reproduce, then a blank "
	"line, then ONE sentence beginning \"**Summary:**\" that says what the "
	"session profiled (e.g. \"**Summary:** creating a Sales Order and then "
	"making a Delivery Note from it.\"). Nothing before the list, nothing "
	"after the summary line, no headings, no code fences."
)

_MAX_STEPS_ACTIONS = 60
_MAX_STEPS_USER_CHARS = 8000


_INDEX_SYSTEM_PROMPT = (
	"You are a senior Frappe Framework / ERPNext DBA reviewing index candidates "
	"for ONE database table flagged by a performance profiler. You're given the "
	"table, the columns the profiled session filtered / joined / ordered on (how "
	"often, and which appeared together), a few of the actual queries, and the "
	"table's CURRENT indexes (`SHOW INDEX` output). Recommend the SMALLEST set of "
	"indexes that actually helps — almost always ONE composite, columns ordered "
	"equality-then-range-then-ORDER-BY, leftmost = the most selective / always-"
	"present one.\n\n"

	"RULES:\n"
	"  • If an existing index already covers a candidate as a leftmost prefix, do "
	"NOT recommend it — say it's already covered.\n"
	"  • Never index Frappe's metadata columns (`name`, `creation`, `modified`, "
	"`modified_by`, `owner`, `parent`, `parentfield`, `parenttype`, `idx`, "
	"`docstatus`, …) — they're written on every save or already indexed.\n"
	"  • Adding an index to a write-hot table (GL Entry, Stock Ledger Entry, Bin, "
	"Payment Ledger Entry, Serial and Batch Bundle, …) slows every submitted "
	"document in production — only recommend it if a query that filters this way "
	"is genuinely slow, and say so.\n"
	"  • Customize Form ▸ field ▸ Search Index makes only SINGLE-column indexes; a "
	"composite needs a patch with `frappe.db.add_index('<DocType>', "
	"['col_a', 'col_b'])`.\n\n"

	"OUTPUT — Markdown, exactly these headings, nothing before or after:\n"
	"**Recommendation** — the one index to add (e.g. `(against_voucher_type, "
	"against_voucher_no)` on `GL Entry`), OR \"nothing — the existing indexes "
	"already cover these read patterns\".\n"
	"**Why** — 1-2 sentences tying it to the queries / explaining the column order.\n"
	"**How to add** — the `frappe.db.add_index(\"<DocType>\", [\"col_a\", "
	"\"col_b\"])` patch line (for a single column you may instead say Customize "
	"Form ▸ field ▸ Search Index). Omit this heading entirely if the "
	"Recommendation is \"nothing\".\n"
	"**Skip** — one line per candidate column or combo you're NOT recommending and "
	"why (already covered by `<index name>` / a Frappe metadata column / not worth "
	"the write cost). If there's nothing to skip, write \"—\".\n\n"

	"Keep it tight — roughly 120-300 words. Don't restate the table's read/write "
	"numbers back at the reader."
)

_MAX_INDEX_SAMPLE_QUERIES = 4
_MAX_INDEX_USER_CHARS = 10000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# v0.6.x: per-section "use the LLM for X" toggle → the config attribute.
_AI_SECTION_FLAGS = {
	"findings": "ai_suggest_findings",
	"indexes": "ai_suggest_indexes",
	"humanize": "ai_humanize_steps",
}


def is_finding_type_excluded(finding_type: str | None) -> bool:
	"""Return True when ``finding_type`` is in ``cfg.ai_excluded_finding_types``.

	v0.9.0 per-type opt-out (Critical Risk #2 of the architecture review).
	Pure-function read of the configured exclusion list, exact case-sensitive
	match. Empty / unknown / non-eligible type → False (an inert exclude is
	safer than a partial-match exclude that would let data through when the
	operator intended to block it).

	Reads settings via the cached ``get_config()`` reader — adds at most one
	Redis lookup per call, and that's a single cached hit. Returning False on
	any read error (no bench, settings cache wedged) is the safe direction:
	if the operator's intent is to block, the master gates (``ai_enabled``,
	``ai_auto_suggest``) already give them coarser control; if the operator
	hasn't configured an exclusion list at all, returning False is correct.
	"""
	if not finding_type or not isinstance(finding_type, str):
		return False
	try:
		from optimus.settings import get_config
		excluded = get_config().ai_excluded_finding_types
	except Exception:
		return False
	return finding_type in (excluded or ())


def _resolve_timeout_seconds() -> int:
	"""Return the configured HTTP timeout for outbound LLM calls, falling
	back to :data:`_HTTP_TIMEOUT` when settings can't be read.

	v0.9.0 (Critical Risk #2): hosted providers answer in seconds, but a
	cold-start local LLM (ollama / vLLM / LM Studio) routinely exceeds 60s
	on first call. The clamp to ``[10, 600]`` is applied in
	``settings._resolve``; this helper is defensive against a settings cache
	miss during bootstrap or a pure-pytest call path with no Frappe.
	"""
	try:
		from optimus.settings import get_config
		v = get_config().ai_request_timeout_seconds
		return max(10, min(600, int(v or _HTTP_TIMEOUT)))
	except Exception:
		return _HTTP_TIMEOUT


def is_available(section: str | None = None) -> bool:
	"""True when AI fix suggestions are turned on and minimally configured:
	``ai_enabled`` set, a model resolvable for the chosen provider, and an
	API key present unless the provider needs none (local endpoints).

	When ``section`` is one of ``"findings"`` / ``"indexes"`` / ``"humanize"``,
	additionally require the matching per-section toggle (``ai_suggest_findings``
	/ ``ai_suggest_indexes`` / ``ai_humanize_steps``) — turning a section off in
	Optimus Settings is a hard disable. Fails soft: an unknown ``section`` (or
	a config attr we couldn't read) doesn't block here — the master
	``ai_enabled`` check has already passed."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return False
	if not getattr(cfg, "ai_enabled", False):
		return False
	try:
		provider = _resolve_provider()
	except AiFixError:
		return False
	if not provider.get("model") or not provider.get("base_url"):
		return False
	if provider.get("needs_key") and not provider.get("api_key"):
		return False
	if section:
		flag = _AI_SECTION_FLAGS.get(section)
		if flag and not getattr(cfg, flag, True):
			return False
	return True


def suggest_fix(finding: dict) -> dict:
	"""Ask the configured LLM for a fix for ``finding``.

	``finding`` is the shape produced by ``renderer._finding_to_dict`` plus
	an optional ``source_window`` (list of ``{lineno, content, is_target}``)
	that the caller gathered around the callsite.

	Returns ``{"suggestion": <markdown>, "model": str, "provider": str,
	"generated_at": <iso>, "source_available": bool}`` — ``source_available``
	is ``False`` when the LLM got neither a source window nor a SQL statement
	(only the finding's title + numbers), so the UI can mark the result as
	directional rather than a verified code fix. Raises ``AiFixError``
	(user-facing) on a configuration problem, a network / auth / rate-limit
	error, or an empty response.

	v0.9.0: ``ai_excluded_finding_types`` is consulted first. When the
	finding's type is on the operator's exclusion list, this returns
	immediately with ``AiFixError("excluded by ai_excluded_finding_types")``
	— the payload is never built and no request leaves the host.
	"""
	if is_finding_type_excluded(finding.get("finding_type")):
		raise AiFixError("excluded by ai_excluded_finding_types")
	provider = _resolve_provider()
	if not provider.get("model"):
		raise AiFixError(
			"No AI model is configured — set 'Model' under Optimus Settings ▸ "
			"AI Fix Suggestions."
		)
	if not provider.get("base_url"):
		raise AiFixError(
			"No AI base URL is configured — set 'Base URL' under Profiler "
			"Settings ▸ AI Fix Suggestions."
		)
	if provider.get("needs_key") and not provider.get("api_key"):
		raise AiFixError(
			"No API key is configured for this AI provider — set it under "
			"Optimus Settings ▸ AI Fix Suggestions."
		)

	system, messages = _build_messages(finding)

	if provider["protocol"] == "anthropic":
		text = _call_anthropic(
			provider["base_url"], provider.get("api_key") or "",
			provider["model"], system, messages,
		)
	else:
		text = _call_openai_chat(
			provider["base_url"], provider.get("api_key") or "",
			provider["model"], system, messages,
		)

	text = (text or "").strip()
	if not text:
		raise AiFixError("The AI provider returned an empty response.")
	text = _flag_metadata_column_index_advice(text)
	text = _flag_raw_sql_in_fix(text)

	return {
		"suggestion": text,
		"model": provider["model"],
		"provider": provider["name"],
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"source_available": _had_concrete_context(finding),
	}


def humanize_steps(actions: list[dict], *, session_title: str | None = None) -> str:
	"""Ask the configured LLM to turn the recorded actions into a friendly
	"Steps to Reproduce" narrative (Markdown). ``actions`` is a list of
	``{label, cmd, path, method, doctype, duration_ms}`` dicts (best-effort —
	missing keys are fine). Raises ``AiFixError`` on a config / network
	problem or an empty response."""
	if not actions:
		raise AiFixError("There are no recorded actions to summarise.")
	provider = _resolve_provider()
	if not provider.get("model") or not provider.get("base_url"):
		raise AiFixError(
			"AI is not fully configured — set the provider, model and base URL "
			"under Optimus Settings ▸ AI Fix Suggestions."
		)
	if provider.get("needs_key") and not provider.get("api_key"):
		raise AiFixError("No API key is configured for this AI provider.")

	system, messages = _build_steps_messages(actions, session_title)
	if provider["protocol"] == "anthropic":
		text = _call_anthropic(
			provider["base_url"], provider.get("api_key") or "",
			provider["model"], system, messages,
		)
	else:
		text = _call_openai_chat(
			provider["base_url"], provider.get("api_key") or "",
			provider["model"], system, messages,
		)
	text = (text or "").strip()
	if not text:
		raise AiFixError("The AI provider returned an empty response.")
	return text


def suggest_index(table_payload: dict) -> dict:
	"""Ask the configured LLM to vet/refine an index recommendation for one
	table. ``table_payload`` keys: ``table`` / ``doctype`` / ``read_count`` /
	``write_count`` / ``is_write_hot`` / ``recommended_index`` (the heuristic
	pick) / ``candidates`` (column→clauses→hits) / ``framework_cols_filtered`` /
	``existing_indexes`` (``[{name, columns, unique}]`` from SHOW INDEX) /
	``sample_queries``. Returns ``{"suggestion": <markdown>, "model", "provider",
	"generated_at"}``. Raises ``AiFixError`` on a config / network problem or an
	empty response."""
	if not table_payload or not table_payload.get("table"):
		raise AiFixError("No table to analyse for an index suggestion.")
	provider = _resolve_provider()
	if not provider.get("model") or not provider.get("base_url"):
		raise AiFixError(
			"AI is not fully configured — set the provider, model and base URL "
			"under Optimus Settings ▸ AI Fix Suggestions."
		)
	if provider.get("needs_key") and not provider.get("api_key"):
		raise AiFixError("No API key is configured for this AI provider.")

	system, messages = _build_index_messages(table_payload)
	if provider["protocol"] == "anthropic":
		text = _call_anthropic(
			provider["base_url"], provider.get("api_key") or "",
			provider["model"], system, messages,
		)
	else:
		text = _call_openai_chat(
			provider["base_url"], provider.get("api_key") or "",
			provider["model"], system, messages,
		)
	text = (text or "").strip()
	if not text:
		raise AiFixError("The AI provider returned an empty response.")
	# Same guardrail as suggest_fix: if the model recommended indexing a Frappe
	# metadata column, append a correction note. Plus the raw-SQL guardrail —
	# the index-suggestion path doesn't usually emit code, but a model can
	# still volunteer a ``frappe.db.sql("ALTER ...")`` fallback that should
	# be flagged (DDL verbs are excluded from the detector anyway, so this
	# guard fires only on the broader anti-pattern).
	text = _flag_metadata_column_index_advice(text)
	text = _flag_raw_sql_in_fix(text)
	return {
		"suggestion": text,
		"model": provider["model"],
		"provider": provider["name"],
		"generated_at": datetime.now(timezone.utc).isoformat(),
	}


def _had_concrete_context(finding: dict) -> bool:
	"""True when the LLM was given something concrete to reason about — a
	source window / snippet, or a SQL statement. ``False`` means it only had
	the finding's title + numbers, so the suggestion is necessarily
	directional (and the UI should say so)."""
	detail = (finding.get("technical_detail") or {})
	callsite = detail.get("callsite") or {}
	if finding.get("source_window") or callsite.get("source_snippet"):
		return True
	if finding.get("phase2_hotline"):
		return True  # has the per-line numbers even if the source couldn't be read
	return bool(detail.get("normalized_query") or detail.get("example_queries"))


def test_connection() -> dict:
	"""Send a tiny probe to the configured provider. Returns
	``{"ok": bool, "message": str, "model": str}`` — never raises (the
	failure detail goes in ``message``)."""
	try:
		provider = _resolve_provider()
	except AiFixError as e:
		return {"ok": False, "message": str(e), "model": ""}

	if not provider.get("model") or not provider.get("base_url"):
		return {
			"ok": False,
			"message": "Provider/model/base URL not fully configured.",
			"model": provider.get("model") or "",
		}
	if provider.get("needs_key") and not provider.get("api_key"):
		return {"ok": False, "message": "No API key configured.", "model": provider["model"]}

	messages = [{"role": "user", "content": "Reply with exactly: OK"}]
	try:
		if provider["protocol"] == "anthropic":
			text = _call_anthropic(
				provider["base_url"], provider.get("api_key") or "",
				provider["model"], "You are a connectivity probe. Reply with exactly: OK",
				messages, max_tokens=16,
			)
		else:
			text = _call_openai_chat(
				provider["base_url"], provider.get("api_key") or "",
				provider["model"], "You are a connectivity probe. Reply with exactly: OK",
				messages, max_tokens=16,
			)
	except AiFixError as e:
		return {"ok": False, "message": str(e), "model": provider["model"]}

	return {
		"ok": True,
		"message": f"Reachable. Model replied: {(text or '').strip()[:60]!r}",
		"model": provider["model"],
	}


# ---------------------------------------------------------------------------
# Config / provider resolution
# ---------------------------------------------------------------------------

def _resolve_provider() -> dict:
	"""Resolve the active provider config: protocol, base_url, model,
	needs_key, api_key (decrypted), and the provider display name. Raises
	``AiFixError`` on an unknown provider or a custom provider missing its
	required base_url/model.

	The API key is fetched via ``frappe.utils.password.get_decrypted_password``
	on every call — it is never cached in ``OptimusConfig`` and never
	returned to the client.
	"""
	from optimus.settings import get_config
	cfg = get_config()
	name = (getattr(cfg, "ai_provider", "") or _DEFAULT_PROVIDER).strip()
	if name not in _PROVIDER_DEFAULTS:
		raise AiFixError(f"Unknown AI provider {name!r}. Pick one in Optimus Settings.")

	defaults = _PROVIDER_DEFAULTS[name]
	base_url = (getattr(cfg, "ai_base_url", "") or "").strip().rstrip("/") or defaults["base_url"]
	model = (getattr(cfg, "ai_model", "") or "").strip() or defaults["model"]

	# Always fetch the key (harmless if unset) — some OpenAI-compatible
	# routers (OpenRouter, Together, Groq) need one even though local
	# endpoints don't, so we let the user set it for any provider.
	api_key = ""
	try:
		from frappe.utils.password import get_decrypted_password
		api_key = get_decrypted_password(
			"Optimus Settings", "Optimus Settings", "ai_api_key",
			raise_exception=False,
		) or ""
	except Exception:
		api_key = ""

	return {
		"name": name,
		"protocol": defaults["protocol"],
		"base_url": base_url,
		"model": model,
		"needs_key": bool(defaults["needs_key"]),
		"api_key": api_key,
	}


# ---------------------------------------------------------------------------
# Output guardrail — never let an "index a metadata column" recommendation
# through, even if the model ignored the system prompt. Frappe metadata
# columns (`name`, `idx`, `parent`, `creation`, `modified`, `docstatus`, …)
# are written on every save (or already indexed), so indexing them is a
# write-cost trap; the profiler never suggests it anywhere — including here.
# ---------------------------------------------------------------------------

# "add an index on <col>", "Search Index … <col>", "index the <col> column",
# "ADD INDEX (`<col>`)" — captures the column token that follows the
# index-action phrase, skipping connector words ("on", "the", …). The hit is
# discarded if it's negated ("do NOT index …") — see `_NEGATION_RE`.
_INDEX_ADVICE_RE = re.compile(
	r"(?:add\s+(?:an?\s+)?index|search\s+index|index)\b"
	r"[\s(]*(?:(?:on|the|a|an|for|to|of|column|field)\s+)*"
	r"[`'\"]?(?P<col>[A-Za-z_][\w]*)",
	re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"(?:not|n['’]t|never|avoid|without|no need to|don['’]t)\W*$", re.IGNORECASE)


def _metadata_columns() -> frozenset:
	"""The Frappe standard-metadata column set, from the analyzer base
	module (single source of truth). Empty set if unimportable — the
	guardrail then simply does nothing."""
	try:
		from optimus.analyzers.base import FRAPPE_METADATA_COLUMNS
		return FRAPPE_METADATA_COLUMNS
	except Exception:
		return frozenset()


# ---------------------------------------------------------------------------
# Output guardrail: raw `frappe.db.sql(...)` in suggested fix code
# ---------------------------------------------------------------------------
# The system prompt at the top of this module tells the LLM "never hand-built
# SQL strings" and lists ``frappe.get_all`` / ``frappe.get_list`` /
# ``frappe.db.get_value`` / ``frappe.db.get_values`` / ``frappe.qb`` as the
# idiomatic alternatives. The few-shot examples reinforce that. But a
# sufficiently confident model still occasionally leaks raw SQL into its
# proposed fix code — and the system-prompt instruction alone is a soft
# nudge with no backstop.
#
# This guardrail mirrors ``_flag_metadata_column_index_advice``: detect the
# anti-pattern in the LLM's output, append a clearly-marked profiler note,
# never rewrite (markdown is fragile). The note is advisory, not blocking —
# a fix that legitimately needs raw SQL (DDL, vendor-specific MariaDB
# extensions) can be acted on with the operator's judgement.

# ``frappe.db.sql(…, "SELECT …"…)``. The literal can be a regular string,
# f-string, or raw string; the verb that follows is case-insensitive. The
# verbs covered are the ones a model is most likely to suggest as a "fix"
# (DDL like CREATE / ALTER is intentionally outside the scope — those are
# legit administrative paths and the prompt already rarely produces them).
_RAW_SQL_IN_FIX_RE = re.compile(
	r'frappe\.db\.sql\s*\(\s*[fr]?["\']\s*'
	r'(?:SELECT|INSERT|UPDATE|DELETE|REPLACE)\b',
	re.IGNORECASE,
)

# Markdown code-fence detector. Group 1 captures the info-string
# (``diff`` / ``python`` / ``py`` / empty for un-tagged fences).
_CODE_FENCE_RE = re.compile(r'^```(\w*)\s*$')


def _flag_raw_sql_in_fix(text: str) -> str:
	"""If the model's proposed fix contains a raw ``frappe.db.sql(...)`` call
	with a SELECT / INSERT / UPDATE / DELETE / REPLACE literal, append a
	correction note. Same contract as ``_flag_metadata_column_index_advice``
	— returns the text unchanged when the fix is clean, otherwise with the
	profiler note appended.

	Detection scope:

	* Only matches inside markdown code-fenced blocks (``\\`\\`\\`diff`` /
	  ``\\`\\`\\`python`` / ``\\`\\`\\`py`` / un-tagged fences). Prose
	  mentions like "instead of ``frappe.db.sql`` use ..." are
	  intentionally EXCLUDED — the guardrail must not fire on the LLM's
	  own commentary.
	* Inside a ``diff`` block, only ADDITION lines (starting with ``+``
	  but not ``+++`` — the diff file-header) count as "the proposed
	  fix". REMOVAL lines (``-``) are the BEFORE code being replaced;
	  flagging them would invert the guardrail (the model is rightly
	  showing the bad pattern being removed).
	* Inside non-diff code blocks, every line counts (the whole block
	  is the proposal).

	Known scope limits:

	* Only detects ``frappe.db.sql``. Other invocations
	  (``frappe.db.multisql``, future Frappe SQL helpers) are not
	  detected — widen the pattern in a follow-up if production
	  surfaces them.
	* DDL verbs (CREATE / ALTER / DROP) are excluded — DDL via raw SQL
	  is sometimes the right answer (e.g. ``ADD INDEX`` when the
	  Customize-Form route isn't an option).
	"""
	if not text:
		return text

	flagged = False
	in_fence = False
	fence_kind = ""
	for line in text.splitlines():
		fence_match = _CODE_FENCE_RE.match(line.strip())
		if fence_match:
			if not in_fence:
				in_fence = True
				fence_kind = (fence_match.group(1) or "").lower()
			else:
				in_fence = False
				fence_kind = ""
			continue
		if not in_fence:
			continue

		# Inside a code block. Diff blocks restrict scanning to addition
		# lines; non-diff blocks scan every line.
		if fence_kind == "diff":
			if not line.startswith("+") or line.startswith("+++"):
				continue
			# Strip the leading "+" so the regex sees actual code, not
			# the diff-marker prefix.
			line_to_scan = line[1:]
		else:
			line_to_scan = line

		if _RAW_SQL_IN_FIX_RE.search(line_to_scan):
			flagged = True
			break

	if not flagged:
		return text

	return text.rstrip() + (
		"\n\n> **Profiler note:** the fix above includes a raw "
		"`frappe.db.sql(\"SELECT …\")` call. The recommended Frappe pattern "
		"is `frappe.get_all` / `frappe.get_list` / `frappe.db.get_value` / "
		"`frappe.db.get_values` (Document API for typical reads) or "
		"`frappe.qb` (query builder for joins / aggregations / dynamic "
		"conditions). Use raw SQL only when none of those API surfaces fit "
		"(rare — e.g. DDL, vendor-specific MariaDB extensions)."
	)


def _flag_metadata_column_index_advice(text: str) -> str:
	"""If the model recommended indexing a Frappe metadata column, append a
	correction note. We don't rewrite the body (markdown is fragile) — we
	add a clearly-marked profiler note so the reader doesn't act on it."""
	meta = _metadata_columns()
	if not meta or not text:
		return text
	hits = []
	for m in _INDEX_ADVICE_RE.finditer(text):
		col = m.group("col").strip("`'\"() ").lower()
		if col not in meta or col in hits:
			continue
		# Skip negated mentions ("do NOT index `modified`") — no correction needed.
		if _NEGATION_RE.search(text[max(0, m.start() - 16):m.start()]):
			continue
		hits.append(col)
	if not hits:
		return text
	cols = ", ".join(f"`{c}`" for c in hits)
	plural = len(hits) > 1
	return text.rstrip() + (
		"\n\n> **Profiler note:** disregard any suggestion above to index "
		+ cols
		+ (" — these are Frappe framework-managed columns" if plural
		   else " — that is a Frappe framework-managed column")
		+ " (Frappe writes "
		+ ("them" if plural else "it")
		+ " on every save, or "
		+ ("they're" if plural else "it's")
		+ " already indexed). Index a business column from the WHERE / JOIN "
		"instead, or change the query shape."
	)


# ---------------------------------------------------------------------------
# Prompt construction (pure)
# ---------------------------------------------------------------------------

def _truncate(text: Any, limit: int) -> str:
	s = "" if text is None else str(text)
	if len(s) <= limit:
		return s
	return s[:limit] + "\n…(truncated)"


def _build_steps_messages(
	actions: list[dict], session_title: str | None
) -> tuple[str, list[dict]]:
	"""Build ``(system_prompt, [user_message])`` for the Steps-to-Reproduce
	humanizer. Pure — no Frappe, no I/O. ``actions`` items use the keys
	``label`` / ``cmd`` / ``path`` / ``method`` / ``doctype`` /
	``duration_ms`` (all optional)."""
	lines: list[str] = []
	title = (str(session_title).strip() if session_title else "")
	if title:
		lines.append(f"Session title (what the user named this run): {title}")
		lines.append("")
	lines.append("Recorded actions, in order:")
	for i, a in enumerate(actions[:_MAX_STEPS_ACTIONS], 1):
		label = (a.get("label") or "").strip() or "(unnamed action)"
		bits: list[str] = []
		cmd = (a.get("cmd") or "").strip()
		if cmd:
			bits.append(f"cmd={cmd}")
		else:
			endpoint = " ".join(p for p in (
				(a.get("method") or "").strip(), (a.get("path") or "").strip(),
			) if p)
			if endpoint:
				bits.append(endpoint)
		doctype = (a.get("doctype") or "").strip()
		if doctype:
			bits.append(f"doctype={doctype}")
		dur = a.get("duration_ms")
		if dur:
			try:
				bits.append(f"{float(dur):.0f}ms")
			except (TypeError, ValueError):
				pass
		suffix = f"  ({'; '.join(bits)})" if bits else ""
		lines.append(f"{i}. {label}{suffix}")
	extra = len(actions) - _MAX_STEPS_ACTIONS
	if extra > 0:
		lines.append(f"… and {extra} more action(s).")
	content = _truncate("\n".join(lines), _MAX_STEPS_USER_CHARS)
	return _STEPS_SYSTEM_PROMPT, [{"role": "user", "content": content}]


def _build_index_messages(payload: dict) -> tuple[str, list[dict]]:
	"""Build ``(system_prompt, [user_message])`` for the per-table index
	suggestion. Pure — no Frappe, no I/O. See ``suggest_index`` for the
	``payload`` shape."""
	t = payload.get("table") or "?"
	dt = (payload.get("doctype") or "").strip()
	parts: list[str] = []
	parts.append(f"Table: `{t}`" + (f"  (DocType: \"{dt}\")" if dt else ""))
	rc = int(payload.get("read_count") or 0)
	wc = int(payload.get("write_count") or 0)
	parts.append(f"This profiling session: {rc} read(s), {wc} write(s) on this table.")
	if payload.get("is_write_hot"):
		parts.append(
			"This is a write-hot core table — in production it takes many "
			"INSERT/UPDATE rows per submitted document."
		)
	rec = payload.get("recommended_index") or {}
	if rec.get("columns"):
		parts.append(
			"Profiler's heuristic pick (most-used filter combination): ("
			+ ", ".join(rec["columns"])
			+ f") — those columns were filtered together in {int(rec.get('together_count') or 0)} of {rc} read(s)."
		)
	cands = payload.get("candidates") or []
	if cands:
		parts.append(
			"Columns this session filtered / joined / ordered on (column — clauses — times):\n"
			+ "\n".join(
				f"  - {c.get('column')} — {', '.join(c.get('sources') or [])} — {int(c.get('hits') or 0)}×"
				for c in cands
			)
		)
	fw = payload.get("framework_cols_filtered") or []
	if fw:
		parts.append("Also filtered on Frappe metadata columns (do NOT index): " + ", ".join(fw))
	ex = payload.get("existing_indexes") or []
	if ex:
		parts.append(
			"CURRENT indexes on this table (from `SHOW INDEX`):\n"
			+ "\n".join(
				f"  - {i.get('name')}: (" + ", ".join(i.get("columns") or []) + ")"
				+ (" — UNIQUE" if i.get("unique") else "")
				for i in ex
			)
		)
	else:
		parts.append(
			f"CURRENT indexes on this table: not available — be cautious about "
			f"redundancy; the operator should run `SHOW INDEX FROM `{t}`` to check."
		)
	sq = payload.get("sample_queries") or []
	if sq:
		shown = [_truncate(q, _MAX_QUERY_CHARS) for q in sq[:_MAX_INDEX_SAMPLE_QUERIES]]
		parts.append("A few of the actual read queries:\n```sql\n" + "\n---\n".join(shown) + "\n```")
	content = _truncate("\n\n".join(p for p in parts if p).strip(), _MAX_INDEX_USER_CHARS)
	return _INDEX_SYSTEM_PROMPT, [{"role": "user", "content": content}]


def _build_messages(finding: dict) -> tuple[str, list[dict]]:
	"""Build ``(system_prompt, [user_message])`` from a finding dict.

	Pure — no Frappe, no I/O. ``finding`` keys used: ``finding_type``,
	``severity``, ``title``, ``customer_description``, ``estimated_impact_ms``,
	``affected_count``, ``technical_detail`` (``callsite``, ``function``,
	``cumulative_ms``, ``action_wall_time_ms``, ``normalized_query``,
	``suggested_ddl``, ``explain_row``, ``fix_hint``, ``validation_note``,
	``example_queries``), ``source_window`` (``[{lineno, content, is_target}]``),
	and ``phase2_hotline`` (``{lineno, content, total_ms, hits}`` — the hottest
	line from a Phase-2 line-profile pass over the finding's function).
	"""
	detail = finding.get("technical_detail") or {}
	callsite = detail.get("callsite") or {}

	parts: list[str] = []
	ftype = finding.get("finding_type") or "Unknown"
	parts.append(f"Finding type: {ftype}")
	type_hint = _FINDING_TYPE_HINTS.get(ftype)
	if type_hint:
		parts.append(f"What this finding type means / how it's usually fixed in Frappe: {type_hint}")
	parts.append(f"Severity: {finding.get('severity') or 'Unknown'}")
	if finding.get("title"):
		parts.append(f"Title: {finding['title']}")
	if finding.get("customer_description"):
		parts.append(f"Description: {finding['customer_description']}")
	impact = finding.get("estimated_impact_ms")
	if impact:
		parts.append(f"Estimated impact: ~{float(impact):.0f}ms")
	if finding.get("affected_count"):
		parts.append(f"Affected occurrences: {finding['affected_count']}")

	had_callsite = bool(callsite.get("filename") and callsite.get("lineno") is not None)
	if had_callsite:
		fn = f" ({callsite['function']})" if callsite.get("function") else ""
		parts.append(
			f"Callsite (the closest non-framework frame to the cost — the "
			f"offending loop/call may be in a function this points into, not "
			f"necessarily AT this line): {callsite['filename']}:{callsite['lineno']}{fn}"
		)

	# For call-tree (hot-path) findings: name the hot function + its share of
	# the action's time, so the model knows exactly which function's body
	# (shown below) to look at.
	hot_fn = (detail.get("function") or "").strip()
	cum_ms = detail.get("cumulative_ms")
	wall_ms = detail.get("action_wall_time_ms")
	if hot_fn and cum_ms:
		share = ""
		try:
			if wall_ms:
				share = f" — {round(float(cum_ms) / float(wall_ms) * 100)}% of this action's {float(wall_ms):.0f}ms wall time"
		except (TypeError, ValueError, ZeroDivisionError):
			share = ""
		parts.append(
			f"Hot function (the call-tree subtree that dominates this action): "
			f"`{hot_fn}` — ~{float(cum_ms):.0f}ms{share}. Its source is below; "
			"point at the specific lines/loop/call inside it that cost the time."
		)

	window = finding.get("source_window") or callsite.get("source_snippet") or []
	if window:
		lines_out: list[str] = []
		for sl in window[:_MAX_SOURCE_WINDOW_LINES]:
			marker = ">> " if sl.get("is_target") else "   "
			lines_out.append(f"{marker}{sl.get('lineno')}: {sl.get('content', '')}")
		parts.append(
			"Source around the callsite — THIS IS THE ONLY CODE YOU HAVE; any "
			"\"before\" snippet / diff `-` line in your answer must be copied "
			"verbatim from here, with its line number. `>>` marks the callsite "
			"line. This is a window, not necessarily the whole function — if the "
			"loop/call this finding is about isn't in these lines, say so and "
			"give a directional fix only (no diff):\n```python\n"
			+ "\n".join(lines_out) + "\n```"
		)
	elif had_callsite:
		parts.append(
			"Source around the callsite: NOT AVAILABLE — the profiler couldn't "
			"read this file, so you have NO source code for this finding. Do not "
			"write a before/after snippet or a diff (you'd be inventing the "
			"\"before\"). Give a short directional fix only, framed as \"without "
			"seeing the code, the likely fix is …\"."
		)

	hot = finding.get("phase2_hotline") or {}
	if isinstance(hot, dict) and hot.get("lineno") is not None:
		hl_content = str(hot.get("content") or "").strip()
		hl_ms = hot.get("total_ms") or 0
		hl_hits = hot.get("hits") or 0
		parts.append(
			f"Line-profile (Phase 2) over this function found its hottest line is "
			f"line {hot['lineno']}"
			+ (f" — `{hl_content}`" if hl_content else "")
			+ (f" ({float(hl_ms):.0f}ms" + (f" over {int(hl_hits)} call(s)" if hl_hits else "") + ")"
			   if hl_ms else "")
			+ ". Start your fix there."
		)

	if detail.get("normalized_query"):
		parts.append("Query (normalized):\n```sql\n"
			+ _truncate(detail["normalized_query"], _MAX_QUERY_CHARS) + "\n```")
	if detail.get("suggested_ddl"):
		parts.append("Profiler's suggested DDL:\n```sql\n"
			+ _truncate(detail["suggested_ddl"], _MAX_QUERY_CHARS) + "\n```")
	if detail.get("explain_row"):
		parts.append(f"EXPLAIN row: {_truncate(detail['explain_row'], 800)}")
	if detail.get("fix_hint"):
		parts.append(f"Profiler's static fix hint: {detail['fix_hint']}")
	if detail.get("validation_note"):
		parts.append(f"Note: {detail['validation_note']}")
	examples = detail.get("example_queries") or []
	if examples:
		shown = [_truncate(q, _MAX_QUERY_CHARS) for q in examples[:2]]
		parts.append("Example affected queries:\n```sql\n" + "\n---\n".join(shown) + "\n```")

	content = "\n\n".join(p for p in parts if p).strip()
	content = _truncate(content, _MAX_USER_CONTENT_CHARS)
	return _SYSTEM_PROMPT, [{"role": "user", "content": content}]


_REASONING_MODEL_RE = re.compile(r"^o[0-9]")  # OpenAI o1/o3/o4… — reject `temperature`


def _is_reasoning_model(model: str) -> bool:
	return bool(_REASONING_MODEL_RE.match((model or "").strip().lower()))


# ---------------------------------------------------------------------------
# HTTP layer (uses `requests`; `frappe` only for best-effort logging)
# ---------------------------------------------------------------------------

def _log_http_error(provider: str, where: str, status: int | None, detail: str = "") -> None:
	"""Best-effort error log. NEVER includes the prompt, the source code, or
	the API key — only the provider name, the call site, and the HTTP
	status."""
	try:
		import frappe
		frappe.log_error(
			message=f"AI fix call failed: provider={provider} at={where} "
			        f"status={status} {detail}".strip(),
			title="optimus ai_fix",
		)
	except Exception:
		pass


def _http_post(url: str, headers: dict, body: dict, *, provider: str, where: str) -> dict:
	"""POST JSON, return the parsed response dict. Maps transport / HTTP /
	decode errors to ``AiFixError`` with operator-friendly messages."""
	timeout = _resolve_timeout_seconds()
	try:
		resp = requests.post(url, headers=headers, json=body, timeout=timeout)
	except requests.exceptions.Timeout:
		_log_http_error(provider, where, None, "timeout")
		raise AiFixError(f"The AI provider didn't respond within {timeout}s.")
	except requests.exceptions.RequestException as e:
		_log_http_error(provider, where, None, type(e).__name__)
		raise AiFixError(f"Couldn't reach the AI provider: {type(e).__name__}.")

	status = resp.status_code
	if status in (401, 403):
		_log_http_error(provider, where, status)
		raise AiFixError("The AI provider rejected the API key — check it in Optimus Settings.")
	if status == 404:
		# Almost always a wrong Base URL — the path segment is missing.
		# OpenAI-compatible servers (Ollama, LM Studio, vLLM, OpenRouter,
		# Together, Groq) expose chat completions under `/v1`, so the Base
		# URL has to include it.
		_log_http_error(provider, where, status, f"url={url}")
		hint = (
			" OpenAI-compatible endpoints serve this under '/v1' — set the Base URL to e.g. "
			"http://localhost:11434/v1 (Ollama), http://localhost:1234/v1 (LM Studio)."
			if provider == "openai" else ""
		)
		raise AiFixError(
			f"The AI provider returned 404 (Not Found) for {url} — the Base URL in "
			f"Optimus Settings is probably missing a path segment.{hint}"
		)
	if status == 429:
		_log_http_error(provider, where, status)
		raise AiFixError("The AI provider is rate-limiting requests — try again shortly.")
	if status >= 400:
		_log_http_error(provider, where, status)
		# Surface the response body's error text if the provider gave one —
		# helpful for "model not found", "context too long", etc. Capped.
		detail = ""
		try:
			body_text = (resp.text or "").strip()
			if body_text:
				detail = " — " + body_text[:300]
		except Exception:
			detail = ""
		raise AiFixError(f"The AI provider returned an error (HTTP {status}){detail}")

	try:
		return resp.json()
	except ValueError:
		_log_http_error(provider, where, status, "non-JSON body")
		raise AiFixError("The AI provider returned an unexpected (non-JSON) response.")


def _call_anthropic(
	base_url: str, api_key: str, model: str, system: str, messages: list[dict],
	*, max_tokens: int = _MAX_OUTPUT_TOKENS,
) -> str:
	url = base_url.rstrip("/") + "/v1/messages"
	headers = {
		"content-type": "application/json",
		"anthropic-version": _ANTHROPIC_VERSION,
	}
	if api_key:
		headers["x-api-key"] = api_key
	body = {
		"model": model,
		"max_tokens": max_tokens,
		"temperature": _TEMPERATURE,
		"system": system,
		"messages": messages,
	}
	data = _http_post(url, headers, body, provider="anthropic", where="messages")
	try:
		blocks = data.get("content") or []
		for b in blocks:
			if isinstance(b, dict) and b.get("type") == "text":
				return b.get("text") or ""
		# Fall back to the first block's text if no explicit type.
		if blocks and isinstance(blocks[0], dict):
			return blocks[0].get("text") or ""
	except Exception:
		pass
	raise AiFixError("The AI provider's response didn't contain any text.")


def _call_openai_chat(
	base_url: str, api_key: str, model: str, system: str, messages: list[dict],
	*, max_tokens: int = _MAX_OUTPUT_TOKENS,
) -> str:
	url = base_url.rstrip("/") + "/chat/completions"
	headers = {"content-type": "application/json"}
	if api_key:
		headers["authorization"] = f"Bearer {api_key}"
	body = {
		"model": model,
		"max_tokens": max_tokens,
		"messages": [{"role": "system", "content": system}, *messages],
	}
	if not _is_reasoning_model(model):
		body["temperature"] = _TEMPERATURE
	data = _http_post(url, headers, body, provider="openai", where="chat/completions")
	try:
		choices = data.get("choices") or []
		if choices:
			msg = choices[0].get("message") or {}
			content = msg.get("content")
			if isinstance(content, str):
				return content
			# Some servers return content as a list of parts.
			if isinstance(content, list):
				return "".join(
					p.get("text", "") for p in content if isinstance(p, dict)
				)
	except Exception:
		pass
	raise AiFixError("The AI provider's response didn't contain any text.")
