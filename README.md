# Optimus

**A flow-aware performance profiler for Frappe and ERPNext.** Records a real business workflow (Sales Invoice save → submit → Delivery Note → submit → …), joins it with server resource state and browser-side timings, and produces two downloadable HTML reports you can actually act on: a **Safe Report** to share with a third-party dev shop without leaking customer data, and a **Raw Report** for internal debugging with full stack traces and SQL literals.

> **Status:** `v0.12.26` — production-ready. MIT-licensed. 1820 unit tests + 39 integration tests in CI on every push (ruff + pytest on Python 3.14; bench-driven integration suite on Frappe v16). See the [CHANGELOG](./CHANGELOG.md) for the full feature history, including the v0.7.0 rename from `frappe_profiler` → `optimus`.

---

## Table of contents

- [What it is](#what-it-is)
- [What it isn't](#what-it-isnt)
- [Install](#install)
- [60-second quickstart](#60-second-quickstart)
- [Using Optimus](#using-optimus)
- [The customer → partner handoff](#the-customer--partner-handoff)
- [Finding types](#finding-types)
- [How it works](#how-it-works)
- [Dependencies](#dependencies)
- [Comparison with alternatives](#comparison-with-alternatives)
- [Production safety](#production-safety)
- [Scheduler-disabled sites](#scheduler-disabled-sites)
- [Configuration — Optimus Settings (DocType)](#configuration--optimus-settings-doctype)
  - [General tab](#general-tab)
  - [Analysis tab](#analysis-tab)
  - [AI Fix Suggestions tab](#ai-fix-suggestions-tab)
- [Configuration — site_config.json (operator knobs)](#configuration--site_configjson-operator-knobs)
- [Runtime flags](#runtime-flags)
- [Custom analyzers](#custom-analyzers)
- [Verification checklist](#verification-checklist)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

---

## What it is

- **On-demand profiler for specific slow flows.** You press Start, run your slow flow, press Stop, and get a report. No always-on overhead. No data egress. No external service.
- **Flow-aware.** Automatically captures the entire chain of HTTP requests **and** background jobs triggered by one business operation — e.g. a single Sales Invoice `submit` that enqueues GL posting, stock updates, and GST calculation shows up as one session, not four disconnected transactions. The only profiler that does this for Frappe.
- **Customer-safe report.** Safe mode replaces SQL literals with `?`, strips docnames and filters from URLs, redacts headers and form data, and bleach-sanitizes any user-typed notes. Shareable with a third-party dev shop over email without leaking PII.
- **ERPNext-native findings.** N+1 detection blames `erpnext/accounts/gl_entry.py:211` instead of `frappe/database/database.py:sql`. Findings know about Document hooks, permission queries, naming series, and child table patterns.
- **Server + browser + infra in one report.** v0.5.0 adds per-action CPU/RSS/DB pool/RQ queue snapshots and per-XHR timings with Web Vitals, joined to the matching recording by correlation header. You can tell *code-slow* from *server-slow*, and *backend-slow* from *network-slow*.

## What it isn't

- **Not always-on monitoring.** If you need *"alert me when production p95 regresses,"* use New Relic / Datadog / Sentry. We're opt-in and session-scoped by design.
- **Not distributed tracing.** We only see Frappe. A microservices architecture spanning Python + Go + Node needs OpenTelemetry.
- **Not a replacement for `frappe.recorder`.** We *extend* it — we reuse its SQL capture and stack walker unchanged, and add session tracking, multi-request joining, resource/frontend capture, and the analyze pipeline on top.

---

## Install

```bash
cd ~/frappe-bench
bench get-app https://github.com/Aerele-RnD/optimus.git
bench --site <your-site> install-app optimus
bench restart
```

Tested on **Frappe v16** with MariaDB and Redis. The app declares `required_apps = ["frappe"]`. Runtime dependencies (installed automatically by `bench get-app`) are listed in [`pyproject.toml`](./pyproject.toml) — currently `pyinstrument`, `line_profiler`, `requests`, `sqlparse`, and `Jinja2`, all pure-Python with no compiled extensions (`line_profiler` ships a small C extension; pre-built wheels exist for cpython 3.10–3.14).

After install, an **Optimus User** role is created automatically. All existing System Managers are granted this role, and new System Managers get it automatically via a `User.validate` hook.

> **After every upgrade:** run `bench restart` so the new sign / verify code loads. Sessions captured **before** the restart have null call trees on their actions — their Phase-2 picker dialog opens with a yellow "No curated functions available" callout pointing this out. Capture a fresh session after the restart for a working picker.

---

## 60-second quickstart

1. Open Desk. A bright red **Optimus** pill appears in the bottom-right corner.
2. Click it. A dialog asks for a label, an optional "Steps to Reproduce" note, and a `Capture Python call tree` toggle (leave on).
3. Click **Start**. The pill turns green and shows an elapsed timer.
4. Run your flow — save a Sales Invoice, submit it, wait for background jobs, whatever you want profiled.
5. Click the pill again to **Stop**. The pill turns orange ("Analyzing…") while the analyze pipeline runs (typically 2–15 seconds).
6. Click the pill once more (now blue, "Report ready") to jump to the session form.
7. Click **Download Safe Report** or **Download Safe Report (PDF)**. Email it to whoever needs it.

That's the whole workflow. No configuration required.

---

## Using Optimus

### The floating widget

The bottom-right widget is the only control surface a regular user needs.
Its color reflects state:

| Color | State | Meaning |
|---|---|---|
| Red | Idle | No active session for this user. Click to start one. |
| Green | Recording | A session is live. Elapsed timer is shown. Click to stop. |
| Orange | Analyzing | Stop was clicked; the analyze worker is generating the report. |
| Blue | Report ready | Analyze finished. Click to jump to the Optimus Session form. |
| Gray | Disabled | The master kill-switch in Optimus Settings is off, or your user lacks the Optimus User role. |

The widget is only shown to users with the **Optimus User** role. System
Managers receive it automatically; you can grant it to other users via
**User → Roles**.

The Start dialog lets you set:

- **Label** — what shows up in the session list. Pick something
  searchable (`SI submit, 100 items` beats `test`).
- **Steps to Reproduce / Notes** — a rich-text block. Rendered at the
  top of both reports. Use it to give context to whoever reads the
  report ("Customer says save takes 8s; happens on every SI ≥ 50
  items").
- **Capture Python call tree** — leave on for actionable findings.
  Turning it off gives you the v0.2.0 SQL-only behavior (~10–30%
  overhead instead of 1.5–2×), useful only when you specifically
  want to verify a SQL-level hypothesis without Python overhead.

### The Optimus Session form

Every session lands as an **Optimus Session** row. The form has three
read-only sections:

1. **Status + timing** — Recording / Analyzing / Ready / Failed; start
   time; analyze wall-clock duration; counts of actions and findings.
2. **Reports** — two file links plus action buttons:
   - **Download Safe Report** / **Open Safe Report** — the customer-
     safe HTML. Email-shareable.
   - **Download Raw Report** / **Open Raw Report** — the un-redacted
     version. Hidden from non-admins; the link itself is permission-
     gated server-side, so guessing the file name does not bypass it.
   - **Re-analyze** — re-runs the analyze pipeline from the captured
     recordings (which live in Redis for 10 minutes after Stop).
     Useful when a session ends `Failed` because of a transient
     error. After 10 minutes the recordings are gone — re-analyze
     can still re-render reports from the persisted DocType data,
     but cannot recompute findings.
   - **Regenerate Reports** — re-renders both HTML files from the
     persisted Action/Finding rows. Cheaper than a full re-analyze;
     useful after an Optimus upgrade that improves the renderer.
   - **Phase 2 → Line Profile** — opens the picker dialog (see
     below).
   - **Pin as Baseline** / **Unpin Baseline** — see _Baseline
     comparison_ below.
3. **Captured actions + findings** — child tables you can drill into
   without opening the report.

### Report modes — Safe vs Raw

| Aspect | Safe Report | Raw Report |
|---|---|---|
| Audience | Customer, third-party dev shop, any external party | Internal engineers with prod access |
| SQL literals | Replaced with `?` | Preserved verbatim |
| URLs | `/app/sales-invoice/SI-2026-00123/edit` becomes `/app/sales-invoice/<name>/edit`; filters / source_name redacted | Preserved verbatim |
| Headers + form data | `Authorization`, `Cookie`, CSRF, anything matching `password\|secret\|token\|api[-_]?key\|...` → `[REDACTED]` | Preserved verbatim |
| User notes | bleach-sanitized HTML (strips `<script>`, `onclick`, `javascript:` URLs) | Same — bleach runs in both modes for XSS safety |
| Custom-app function names | Hashed to `<app>:<short>` | Preserved verbatim |
| Permission gate | None (anyone with the URL can open) | System Manager or the user who recorded the session |
| Self-contained | Yes — no CDN fonts, no external scripts, no `@import` (canary test in CI) | Yes |

Both reports render from the same `templates/report.html` and the same
persisted data — Safe mode is a renderer-time redaction pass, not a
separate capture.

### Phase 2 — Line profiling

The default capture (Phase 1) tells you _which functions_ are hot. Phase
2 tells you _which lines inside those functions_ are hot. Use it when
Phase 1 surfaces a hot path and you can't tell from reading the function
which line is doing the work.

Workflow:

1. Open a Ready session, click **Phase 2 → Line Profile**.
2. The picker dialog shows curated candidates from Phase 1's call tree.
   Functions consuming ≥ 50 ms are pre-ticked as `recommended`.
3. **Auto-expand hot chain** (ticked by default) walks each pick down
   the call tree to instrument the full chain, not just the entry
   frame.
4. Click **Start Phase 2** — the widget switches back to recording mode.
5. Re-execute the same flow you profiled in Phase 1.
6. Click Stop. Phase 2 analyze runs; the report adds a **Line-Level
   Drilldown** section with per-line hit-count and time.

Phase 2 has a per-request overhead budget (default 10s; tunable via
`optimus_phase2_overhead_budget_seconds` in site_config). If the budget
expires mid-request, line profiling disengages so the request finishes
at natural speed, and the run is flagged "partial data". This stops a
hot-loop pick from freezing the UI.

### AI fix suggestions (optional)

Optimus can call an LLM (Anthropic, OpenAI, Kimi, or any OpenAI-
compatible endpoint including local ones like Ollama or LM Studio) to
suggest concrete fixes for each finding. Off by default — no traffic
leaves your bench until you enable it.

Three feature toggles, all under **Optimus Settings → AI Fix
Suggestions → Use the LLM for**:

- **Fix suggestions on findings** — adds a "Suggest a fix (AI)" button
  per finding and an auto-suggest pass during analyze (when the
  auto-suggest checkbox below is on).
- **Index recommendations** — adds a "Suggest an index (AI)" button to
  the per-table breakdown.
- **Humanized "Steps to Reproduce"** — rewrites the auto-captured
  action list into a friendly flow ("Open Sales Invoice list, click
  New, …").

Turning any one of these off is a hard disable — the button is hidden,
the API refuses, and re-rendered reports omit the AI block. See
[`docs/AI-FIXING.md`](./docs/AI-FIXING.md) for the per-pathway data
inventory and local-LLM recipes.

### Baseline comparison

You're not done until the customer agrees the fix worked. Optimus's
baseline comparison gives you a side-by-side report:

1. On a Ready "before" session, click **Pin as Baseline**.
2. Capture a "after" session of the same flow (same label is
   convenient but not required — the comparison matches actions by
   label, falling back to path).
3. Open the after-session's Safe or Raw report. Three new sections
   appear at the top:
   - **Session-level delta** — total wall time, query count, SQL/Python
     ms, with old → new.
   - **Per-action comparison** — matched actions with before/after
     stats.
   - **Findings compared to baseline** — Fixed / New / Unchanged with
     delta.
4. The customer signs off on concrete numbers, not "it feels faster".

To swap the baseline, unpin the old one and pin the new one.

---

## The customer → partner handoff

This is the primary use case and the feature set is built around it.

**Traditional workflow** — how 90% of ERPNext performance debugging happens today:

> Customer: *"Saving a Sales Invoice is slow."*
> Partner: *"Can you check the slow query log?"*
> Customer: *"..."*
> Partner: *"Send me a screenshot."*
> *(30 minutes of back-and-forth follow)*

**With optimus:**

1. **Customer records** the slow flow (one dialog, one click, no technical knowledge required).
2. **Customer downloads Safe Report** from the session form. Safe mode redacts:
   - SQL literals → `?`
   - Request headers (`Authorization`, `Cookie`, CSRF tokens, anything matching `password|secret|token|api[-_]?key|card_number|cvv|ssn|aadhar|pan_number`) → `[REDACTED]`
   - Form data → same redaction
   - URLs: `/app/sales-invoice/SI-2026-00123/edit` → `/app/sales-invoice/<name>/edit`; `?filters=[...]`, `?source_name=X` → `?filters=?, ?source_name=?`
   - User-typed notes → bleach-sanitized HTML (strips `<script>`, `onclick`, `javascript:` URLs)
   - Python function names from custom apps → `my_acme_app:discounts` (app-level, not module-level)
   - SQL identifiers (table/column/function names) are NOT redacted — they're code, not customer data
3. **Customer emails the .html or .pdf** to the partner. No VPN, no SSH, no shared credentials.
4. **Partner opens the file on their laptop offline.** The report is fully self-contained — no CDN fonts, no external scripts, no `@import`. Tested in CI via `test_safe_report_self_contained.py`.
5. **Partner diagnoses and fixes.** Every finding has a plain-language `customer_description`, a technical detail with callsite + query + suggested DDL, and an estimated impact in ms.
6. **Partner re-records** the fixed flow and pins the original slow session as a baseline. The new report auto-renders three comparison sections:
   - **Session-level delta** — total wall time, query count, SQL/Python ms (old vs new)
   - **Per-action comparison** — matched actions (by label, fallback to path) with before/after stats
   - **Findings compared to baseline** — which findings were **Fixed**, which are **New** regressions, which are **Unchanged** with delta
7. **Customer signs off** with concrete numbers instead of "it feels faster."

---

## Finding types

v0.5.1 emits 18 finding types across 10 analyzers. Every finding has a severity (High/Medium/Low) and an estimated impact in ms.

### Database / SQL (6 types — from v0.2.0)

| Type | Trigger | Actionable detail |
|---|---|---|
| **N+1 Query** | Same normalized query fired from the same Python callsite ≥10 times, ≥20 ms total | Filename:line, the query, and *"refactor to `frappe.get_all` with a name-IN filter or a JOIN"* |
| **Slow Query** | Single query > 200 ms | Normalized query + callsite + *"run EXPLAIN ANALYZE for actual cost"* |
| **Full Table Scan** | `EXPLAIN.type = ALL` | Row count scanned + suggested index |
| **Filesort** | `EXPLAIN.Extra` contains "Using filesort" | Query + ORDER BY column |
| **Temporary Table** | `EXPLAIN.Extra` contains "Using temporary" | Query + recommendation |
| **Low Filter Ratio** | `EXPLAIN.filtered < 10%` AND `rows > 100` | Query + *"WHERE clause selectivity is low"* |
| **Missing Index** | `DBOptimizer` suggests an index AND the column exists AND is not already indexed AND is btree-compatible (v0.5.1 verifies against `information_schema` before emitting) | Table, column, verified DDL (with prefix length for TEXT/BLOB), example queries |

### Python call tree (4 types — v0.3.0)

| Type | Trigger | Actionable detail |
|---|---|---|
| **Slow Hot Path** | A Python subtree consumes > 25% of action wall time AND > 200 ms | Function name + full call tree + SQL leaves grafted under the right frame |
| **Hook Bottleneck** | Same shape as Slow Hot Path, but the subtree root is a doc-event hook (called via `Document.run_method`) | Names the specific hook function so the user knows which hook to optimize |
| **Repeated Hot Frame** | The same `file::function` appears in ≥3 actions and consumes ≥500 ms across the session | User-actionable: optimizing this function helps every flow that touches it. v0.5.1 filter skips plumbing (werkzeug, frappe.handler, frappe.utils) but keeps `Document.run_method`, `has_permission`, `make_autoname`, etc. |
| **Redundant Call** | The same `frappe.get_doc(doctype, name)` / `frappe.cache.get_value(key)` / `has_permission(...)` fired N times from the same callsite (thresholds 5/10/10, configurable) | Callsite + arg hash + *"cache or hoist this call"* |

### Infrastructure (4 types — v0.5.0)

| Type | Trigger | Actionable detail |
|---|---|---|
| **Resource Contention** | System CPU sustained > 85% across ≥2 actions (→ High if any sample ≥ 95% or > 50% of actions affected) | *"Is your code CPU-bound, or is something else on the box competing?"* |
| **Memory Pressure** | Worker RSS grew > 200 MB OR swap active > 100 MB | RSS start/end/delta, swap state, *"check cache growth, long-lived references"* |
| **DB Pool Saturation** | `threads_connected / max_connections > 0.9` across ≥2 actions (v0.5.1 uses the correct ratio after an earlier version used the wrong one) | *"raise `max_connections` or reduce gunicorn workers to match"* |
| **Background Queue Backlog** | Any RQ queue (`default`/`short`/`long`) peaked > 50 during the session | *"your worker count is too low for the load; check if your flow enqueues work"* |

### Frontend (3 types — v0.5.0)

| Type | Trigger | Actionable detail |
|---|---|---|
| **Slow Frontend Render** | Largest Contentful Paint (LCP) > 2500 ms on any page (Medium), > 4000 ms (High) | Page URL, LCP/FCP/CLS/TTFB, *"check TTFB vs render time split"* |
| **Network Overhead** | `xhr_duration - backend_duration > 500 ms` AND `> backend × 1.5` | XHR duration vs backend duration, response size, *"large response, CDN, or TLS handshake issue"* |
| **Heavy Response** | Single XHR response > 500 KB | URL, size, *"paginate or limit field lists"* |

---

## How it works

Five sentences:

1. **Don't fork the recorder.** We reuse `frappe.recorder.Recorder`, `record(force=True)`, and `dump()` for SQL capture. Our app adds session tracking, per-user activation, background-job inheritance, resource/frontend capture, and the analyze pipeline on top.
2. **Per-user activation via hook ordering.** Frappe's `before_request` runs `frappe.recorder.record()` first (no-op without a global flag); our `before_request` runs second and calls `record(force=True)` only if the current user has an active session in our Redis pointer.
3. **Background-job session inheritance via a `frappe.enqueue` patch.** We wrap the canonical `enqueue` to inject `_profiler_session_id` into job kwargs whenever the calling user has an active session; the worker's `before_job` hook pops the marker (so the user's method never sees it) and activates recording for the job.
4. **Frontend capture wraps WHATWG primitives, not Frappe APIs.** `optimus_frontend.js` hooks `window.fetch` and `XMLHttpRequest.prototype.open/send` directly — the same approach every production APM library uses. Survives future Frappe upgrades because `fetch` and `XHR` are stable web platform standards, while jQuery `ajaxComplete` hooks would break when Frappe drops jQuery.
5. **Ten analyzers, all pure functions.** Per-action breakdown, top-N slow queries, N+1 (by callsite), EXPLAIN flags, index suggestions (verified against schema), per-table breakdown, Python call tree (v0.3.0), redundant calls (v0.3.0), infra pressure (v0.5.0), frontend timings (v0.5.0). Each is independently testable from JSON fixtures with no Frappe DB access.

For the full architecture (data-flow diagrams, hook order, edge cases, extension points), read the inline docstrings in [`optimus/analyze.py`](./optimus/analyze.py) and [`optimus/renderer.py`](./optimus/renderer.py).

---

## Dependencies

Deliberately minimal. **Only one non-Frappe dependency is declared** in `pyproject.toml`; everything else rides on Frappe, the standard library, or MariaDB's own EXPLAIN output. This keeps installs lightweight and avoids fighting anyone else's package pins.

### Declared (installed by `bench get-app`)

| Package | Version | What it powers |
|---|---|---|
| **[`pyinstrument`](https://pypi.org/project/pyinstrument/)** | `>=4.6,<6` | Statistical Python call-tree sampler. Produces the per-recording call tree that drives the **Hot Frames** leaderboard, **Slow Hot Path** findings, **Hook Bottleneck** detection, the **Time Breakdown** donut, and the self-referential hot-path phrasing. Without this, the profiler would only see SQL — no Python context. |

### Inherited from Frappe (no extra install)

| Package | Role in the profiler |
|---|---|
| **`frappe.recorder`** | Frappe's built-in SQL recorder. Captures every query + Python stack during a request. We reuse it unchanged for SQL capture; session tracking and analyze pipeline live on top. |
| **[`sqlparse`](https://pypi.org/project/sqlparse/)** | SQL tokenizer / pretty-printer. Formats queries in the Raw report and normalizes whitespace for the **Top Queries** leaderboard. |
| **[`sql_metadata`](https://pypi.org/project/sql-metadata/)** | SQL parser used only by `index_suggestions.py` to extract WHERE/JOIN columns for the **Missing Index** finding's suggested DDL. Parser limitations are caught and downgraded to Analyzer Notes warnings — never a hard failure. |
| **[`psutil`](https://pypi.org/project/psutil/)** | CPU %, worker RSS, load average, swap. Powers the **Server Resource** panel + **Memory Pressure** / **Resource Contention** findings. |
| **[`rq`](https://pypi.org/project/rq/)** | Redis Queue — reads queue depth (default/short/long) for the **Background Queue Backlog** finding. |
| **`redis`** (via `frappe.cache`) | Storage of recordings, sidecar argument logs, pyinstrument session pickles. |
| **[`Jinja2`](https://pypi.org/project/Jinja2/)** | Report template (`templates/report.html`) — the single source of truth for both Safe and Raw modes. |

### Standard-library workhorses (no install, always present)

| Module | Role |
|---|---|
| `sys._getframe` | Cheap caller-stack capture in the sidecar wraps on `frappe.get_doc` / `cache.get_value` / `has_permission`. The instrumentation backbone for **Redundant Call** findings. |
| `hashlib` | SHA-256 of `identifier_raw` → `identifier_safe` so PII never ends up in Safe-mode finding titles (see `capture.py`). |
| `pickle` | pyinstrument session tree serialization in Redis. |
| `dataclasses`, `collections.Counter` / `defaultdict`, `re`, `json`, `urllib` | Analyzer plumbing. |

### Test-time only (not shipped with the app)

| Package | Role |
|---|---|
| **`pytest`** | Test runner. 472+ tests in the suite. |
| **[`hypothesis`](https://pypi.org/project/hypothesis/)** | Property-based testing for the call-tree pruner — fuzzes its invariants (hot-path preservation, soft-cap floor, SQL-leaf preservation). |

### Written in-house (no library)

These do real analytical work without pulling a dependency:

- **EXPLAIN-based findings** — Full Table Scan / Filesort / Temporary Table / Low Filter Ratio are derived from MariaDB's own EXPLAIN output dict (no SQL-planning library).
- **N+1 detection** — groups the recorder's captured stacks by `(filename, lineno)` and collapses multi-variant loops (v0.5.2 callsite dedup).
- **Framework classifier** — pure path-boundary matching against the `FRAMEWORK_APPS` frozenset (frappe, erpnext, hrms, lms, helpdesk, insights, crm, builder, wiki, drive, payments) + third-party lib heuristics.
- **Post-fix timing projections** — per-finding-type speedup factors (20× for full-scan, 3× for filesort, 2× for temp-table, `filtered_pct/100` for low filter, 2× avg for N+1 batching). See `analyzers/base.project_post_fix_ms`.
- **Per-app bucketing, executive summary, analyzer notes, collapsible sections** — pure Python in the renderer + Jinja macros.

---

## Comparison with alternatives

| Dimension | frappe.recorder | New Relic / Datadog | Scout APM / Rails Bullet | **optimus** |
|---|---|---|---|---|
| SQL capture per request | ✓ | ✓ | ✓ | ✓ (via frappe.recorder) |
| N+1 detection strictness | No callsite attribution | Loose | **Strict** | **Strict (callsite-grouped)** |
| Python call tree | ✗ | ✓ (sampler) | ✓ | ✓ (pyinstrument) |
| Flow-aware session (HTTP + bg jobs) | ✗ | Manual trace context | ✗ | **✓ (automatic)** |
| Infra metrics per action | ✗ | ✓ (always-on) | Basic | ✓ (per-action snapshots) |
| Browser XHR + Web Vitals | ✗ | ✓ | ✗ | ✓ (v0.5.0) |
| ERPNext-native findings | ✗ | Generic | Generic | **✓ (native)** |
| Customer-safe redacted export | ✗ | ✗ | ✗ | **✓ (unique)** |
| On-prem / no data egress | ✓ | ✗ | ✗ | ✓ |
| Always-on monitoring | ✗ | ✓ | ✓ | ✗ (opt-in) |
| Alerting / pager integration | ✗ | ✓ | ✓ | ✗ |
| Historical trending | ✗ | ✓✓✓ | ✓✓ | ✗ |
| Cost | Free | $50–400/host/mo | $100+/mo | Free |

**Positioning:** commercial APMs are always-on monitoring for *"something regressed, find it."* optimus is on-demand debugging for *"this specific customer flow is slow, what should my dev shop fix."* They're complementary, not competitive. Most ERPNext shops run only optimus because Datadog is expensive and leaks customer data off-site.

For the specific job of *"debug a slow ERPNext workflow and hand the report to a partner shop,"* optimus produces a better report than any commercial APM — because of callsite-grouped N+1, framework-native findings, flow-aware session, and customer-safe export. None of those exist anywhere else at any price.

---

## Production safety

This app is **designed** to run on production because the whole point is to measure with real data volumes. That said, recording is not free.

### Overhead budget

| Capture path | Overhead |
|---|---|
| SQL only (v0.2.0 baseline) | ~10–30% per query (mostly frappe.recorder's stack capture + EXPLAIN) |
| SQL + Python call tree (v0.3.0+) | ~1.5–2× wall clock during active recording |
| Infra snapshot (v0.5.0) | ~0.8 ms per action boundary |
| Frontend capture (v0.5.0) | ~5 µs per XHR (one fetch wrap + one XHR prototype wrap) |

**When not recording**, cost is a single Redis `GET` per request to check the active-session flag — sub-millisecond on local Redis. Users who are not recording pay essentially nothing.

**Reports should be read as relative, not absolute.** *"This step took 5× longer than that step"* is accurate. *"This step took exactly 4.2 seconds"* is inflated by the recording overhead.

### Per-user isolation

Only the user who clicked Start gets recorded. Other users on the same site at the same time are **not** captured. Cross-session data leaks are prevented at multiple layers:
- Widget role check
- Server-side `_require_profiler_user()` on every whitelisted endpoint
- `api.submit_frontend_metrics` has a session-ownership check that prevents users from writing to a session they don't own

### Background job inheritance

Background jobs spawned by the recording user's actions are automatically captured under the same session. ERPNext's submission path enqueues several jobs (GL postings, stock updates) — without this, the report would miss huge chunks of work.

### Hard caps

| Cap | Default | Configurable via |
|---|---|---|
| Max recordings per session | 200 | `optimus_max_recordings_per_session` |
| Session duration | 10 minutes | (matches recorder TTL, not configurable) |
| Analyze total wall clock | 20 minutes | (5-min headroom under RQ long-queue 25-min timeout) |
| Per-analyzer soft cap | 60 seconds | (soft warning, doesn't halt) |
| Inline-analyze recording count (scheduler-disabled path) | 50 | `optimus_inline_analyze_limit` |
| Frontend XHR entries per session | 1000 | (tail-preferring, hardcoded) |
| Frontend Web Vitals entries per session | 200 | (tail-preferring, hardcoded) |
| Call tree size per action before file overflow | 200 KB | (overflows to private File attachment) |
| Call tree hard-truncate ceiling | 16 MB | (last-resort sanity guard) |

If a session hits the recordings cap, the analyze report shows a warning under `analyzer_warnings`. Subsequent recordings are silently dropped until the customer restarts.

### Memory cleanup

When a session moves to `Ready`, the source recordings in Redis (`RECORDER_REQUEST_HASH`, `RECORDER_REQUEST_SPARSE_HASH`), the per-session keys (`profiler:session:*`, `profiler:infra:*`, `profiler:frontend:*`), and the pyinstrument tree blobs are deleted. Redis returns to baseline. The `Optimus Session` DocType row and the attached HTML report files are the durable record.

### Two report modes

- **`safe_report_file`** — Normalized SQL, redacted URLs/headers/form data, sanitized notes, redacted custom-app function names. Safe to email to a third-party.
- **`raw_report_file`** — Full data: raw SQL with literals, request headers, form data, complete stack traces. **Gated at two layers:**
  1. The "Download Raw Report" button is hidden in the form UI unless the user has `System Manager` role or recorded the session themselves.
  2. A `File.has_permission` hook (`optimus.permissions.file_has_permission`) blocks direct URL access even if the user guesses the file name.

---

## Scheduler-disabled sites

On sites where `bench disable-scheduler` is in effect — common on dev, demo, and Frappe Cloud trial instances — the analyze RQ queue has no worker consuming it. v0.5.0+ detects this via `frappe.utils.scheduler.is_scheduler_disabled()` and falls back to `frappe.enqueue(now=True)`, which runs analyze **synchronously inside the stop request**.

Consequences:

- **The stop API blocks for the analyze duration** (typically 2–20 seconds). The widget transitions from "Stopping…" directly to "Report ready" or "Analyze failed" — skipping the intermediate "Analyzing…" state — because the session is already finalized by the time the stop response arrives.
- **A safety cap (`optimus_inline_analyze_limit`, default 50) refuses inline analyze on huge sessions** to avoid gunicorn's 120-second request timeout. When a session exceeds the cap, it's marked `Failed` with an actionable error pointing the user to `bench enable-scheduler` and the **Retry Analyze** button.
- **`retry_analyze` and the janitor's auto-stop path also use the scheduler-aware enqueue** — you can't accidentally get stuck with a Failed session that won't retry.

---

## Configuration — Optimus Settings (DocType)

Almost every knob a regular admin needs lives in the **Optimus Settings**
Single doc (go to **Desk → search "Optimus Settings"**, or
`/app/optimus-settings`). Three tabs: **General**, **Analysis**, **AI Fix
Suggestions**. The two ops-only knobs not in the UI (memory caps, lock
behaviour, etc.) live in `site_config.json` — see the next section.

Resolution order for any field with both a DocType row and a
site_config fallback (n+1, redundant-call, sampler-interval): **DocType
row → site_config.json → hardcoded default**. The DocType wins if
populated.

### General tab

#### General section

| Field | Default | Purpose |
|---|---|---|
| **Profiler Enabled** | ✓ on | Master kill-switch. When off, no requests or background jobs are profiled even when users have active sessions. Use it to pause site-wide instrumentation without touching individual widgets. |
| **Session Retention (days)** | `30` | Optimus Session rows older than this — only in `Ready` or `Failed` state — are deleted by the daily housekeeping job, along with their attached HTML reports and `File` rows. `0` = keep forever. _Reference: Strict 90 · Recommended 30 · Relaxed 7._ |

#### Apps section

Two child tables that control which Frappe apps Optimus treats as
"user code" (actionable) vs framework noise.

| Field | Purpose |
|---|---|
| **Tracked Apps** | Allowlist. When populated, **only** these apps' findings are actionable; everything else routes to the report's _Framework-level observations_ block. Leave it empty to keep the built-in defaults (`frappe`, `erpnext`, `hrms`, `lms`, `helpdesk`, `insights`, `crm`, `builder`, `wiki`, `drive`, `payments` + pip libs are framework code). **Do NOT add `frappe` or `erpnext` here** — that flips them to "user code" and floods actionable findings with framework noise. |
| **Ignored Apps** | Exclusion list. Findings whose blame-app is in this list are **dropped entirely** (both Findings and Observations sections). Use it for apps you can't or won't patch — typically `frappe`, `optimus`, sometimes `erpnext`. The "Issues found" stat card surfaces a "N findings hidden" note so the total stays honest. |

#### Skip Rules section

Patterns and users to exclude from instrumentation entirely (no
recording captured in the first place — cheaper than dropping at
analyze time).

| Field | Format | Purpose |
|---|---|---|
| **Skip Request Paths** | One URL prefix per line. `#` starts a comment. | Requests starting with any of these prefixes are not profiled even with an active session. Useful for healthcheck endpoints, polling APIs. The profiler's own admin endpoints are always skipped — these extend the built-in list. |
| **Skip Users** | One user email per line. `#` comments. | Requests / jobs running as one of these users are not profiled. Useful for system bot users (scheduler, health-checks). |

#### Redaction section

Optimus redacts sensitive values **at capture time** — passwords, API
keys, tokens, CSRF, cookies, authorization headers never reach Redis or
the persisted report. Defaults cover 12 canonical patterns. The fields
below extend the defaults — they are **never removed**. Substring match,
case-insensitive.

| Field | Format | Purpose |
|---|---|---|
| **Sensitive SQL Columns** | One name per line, `#` comments | Extra column names whose literal values in `WHERE` / `LIKE` / `IN` clauses are redacted to `<REDACTED>`. Example: `recovery_code`, `bank_account`, `otp_seed`. |
| **Sensitive Form / Header Keys** | One name per line, `#` comments | Extra key names whose values in `form_dict` / headers are replaced with `<REDACTED:keyname>`. Example: `x-customer-id`, `recovery_code`. |

#### Capture Capacity section

| Field | Default | Purpose |
|---|---|---|
| **Max Queries per Recording** | `2000` | Per-recording cap on queries enriched (`sqlparse` + `EXPLAIN` + normalization) by analyze. Anything beyond is truncated with a banner at the top of the report. Each query costs one `EXPLAIN` round-trip — raising the cap scales analyze time roughly linearly. Raise to `5000` / `10000` for legitimately heavy flows (e.g. Manufacturing Plan Submit creating 100+ child orders). _Reference: Strict 5000 · Recommended 2000 · Relaxed 1000._ |
| **Sampler Interval (ms)** | `1.0` | pyinstrument statistical-sampler interval. Lower = finer call-tree resolution but higher overhead. `1ms` is recommended; raise to `5–10ms` for prod-like profiling. Floor: `0.1`. _Reference: Strict 0.5 · Recommended 1.0 · Relaxed 5.0._ |
| **Hide framework / internal database tables** | ✓ on | When on, the "Time spent per database table" section drops Frappe schema/meta tables (`tabDocType`, `tabSingles`, `tabPatch Log`, etc.), framework-internal tables, and `information_schema.*`. Touched by every request via framework machinery — not user-actionable. Other sections (top-queries leaderboard, per-action drill-down, recordings) are unaffected. Uncheck to see them all. |
| **Wait for Background Jobs (seconds)** | `300` | How long the analyze job watches the bg jobs the profiled flow enqueued, waiting for each to reach a terminal state (Completed / Failed / Timeout). Hard-capped at `300`. `0` = don't wait. On a single-worker bench the analyze job yields the worker between checks so those jobs can run; if the scheduler is disabled, the wait is skipped. Jobs still running at the ceiling appear as `Running` in the report — click _Retry Analyze_ once they finish to capture their data. _Reference: Strict 300 · Recommended 300 · Relaxed 60._ |

#### Display Filters section

These shape how reports **render** — they don't change what's captured
or analyzed.

| Field | Default | Purpose |
|---|---|---|
| **Min Action Duration to Show (ms)** | `0` | Drop actions shorter than this from the per-action breakdown. `0` = show everything. Useful for declutter when a flow generates many sub-millisecond background polls. _Reference: Strict 0 · Recommended 0 · Relaxed 50._ |
| **Render durations in seconds above (ms)** | `1000` | Durations above this render as seconds (e.g. `5234ms` → `5.23s`); below it, ms is preserved. Set to a very large value (e.g. `99999999`) to effectively disable. _Reference: Strict 500 · Recommended 1000 · Relaxed 99999999._ |

---

### Analysis tab

#### Sensitivity Profile section

| Field | Default | Purpose |
|---|---|---|
| **Sensitivity Profile** | `Recommended` | One-knob preset for the 9 detection thresholds below. **Strict** catches more (lower thresholds, more findings, more noise). **Relaxed** catches less. **Recommended** is the shipped default and automatically tracks future tuning across upgrades. **Custom** lets you hand-tune the individual fields — they stay locked under the named presets. Display filters, Phase-2, capture, retention, and AI settings are **not** affected by the profile. |

#### Analyzer Thresholds section

Repetition thresholds for the redundancy + N+1 analyzers. Lower = more
findings (and more noise); higher = fewer findings.

| Field | Default | Purpose |
|---|---|---|
| **Redundant `get_doc` Threshold** | `5` | Min `get_doc(doctype, name)` calls from the same callsite before a Redundant Call finding is emitted. _Strict 3 · Recommended 5 · Relaxed 10._ |
| **Redundant Cache Lookup Threshold** | `50` | Min cache lookups for the same key from the same callsite. Cache lookups aren't individually timed; `50` matches the high-severity threshold and cuts 0ms noise. _Strict 20 · Recommended 50 · Relaxed 100._ |
| **Redundant Permission Check Threshold** | `10` | Min `has_permission(doctype, name, ptype)` calls from the same callsite. _Strict 5 · Recommended 10 · Relaxed 25._ |
| **N+1 Minimum Occurrences** | `10` | Min identical queries from the same callsite before an N+1 finding is emitted. _Strict 5 · Recommended 10 · Relaxed 25._ |

#### Severity Thresholds section

Tune what counts as a high-severity finding. Sites with intentionally
slow analytical flows want higher numbers (suppress false positives);
sites with strict latency budgets want lower numbers.

| Field | Default | Purpose |
|---|---|---|
| **Slow Query Threshold (ms)** | `200` | A single query slower than this is flagged as a Slow Query finding. _Strict 100 · Recommended 200 · Relaxed 500._ |
| **Slow Hot Path Threshold (% of action wall time)** | `25` | A Python subtree consuming ≥ this % AND ≥ the min-ms below qualifies. _Strict 15 · Recommended 25 · Relaxed 40._ |
| **Slow Hot Path Minimum (ms)** | `200` | Absolute minimum for Slow Hot Path. _Strict 100 · Recommended 200 · Relaxed 500._ |
| **Hot Line High Severity (% of function time)** | `50` | Phase 2: a single line consuming ≥ this % of the function's total AND ≥ the min-ms below is High. _Strict 35 · Recommended 50 · Relaxed 70._ |
| **Hot Line High Severity Min (ms)** | `100` | Phase 2 Hot Line absolute minimum for High. _Strict 50 · Recommended 100 · Relaxed 250._ |

#### Phase 2 Defaults section

Defaults for the line-profile picker dialog. The dialog still lets the
user override these per run.

| Field | Default | Purpose |
|---|---|---|
| **Max Runs per Session** | `10` | Cap on how many Phase 2 runs are retained per Optimus Session. When exceeded, the oldest run's full results are archived to a private `File` and only row metadata is kept. _Strict 25 · Recommended 10 · Relaxed 5._ |
| **Default Auto-Expand Hot Chain** | ✓ on | When on, the picker dialog's `Auto-expand hot chain` checkbox starts ticked. Auto-expansion walks each pick down phase-1's call tree to instrument the full hot chain in one shot. |
| **Auto-Expand: Max Depth** | `10` | How many levels deep auto-expand will follow. _Strict 15 · Recommended 10 · Relaxed 5._ |
| **Auto-Expand: Minimum Child Time (ms)** | `50` | Auto-expand stops descending when the next-hottest child consumes less than this. Lower = follow deeper into smaller hot spots. _Strict 25 · Recommended 50 · Relaxed 100._ |

---

### AI Fix Suggestions tab

Off by default — no traffic leaves your bench until you turn it on.

#### AI Fix Suggestions section (provider wiring)

| Field | Default | Purpose |
|---|---|---|
| **Enable AI Fix Suggestions** | ✗ off | Master switch for the entire AI feature. When off, every AI button on the Session form is hidden and the API refuses. |
| **Provider** | `Anthropic` | Wire format. `Anthropic`, `OpenAI`, `Kimi`, and `OpenAI-compatible` (which requires Base URL + Model — covers Ollama, LM Studio, vLLM, OpenRouter, Together, Groq). |
| **Base URL** | _empty_ | Leave blank to use the hosted default. Required for `OpenAI-compatible` — e.g. `http://localhost:11434/v1` (Ollama), `http://localhost:1234/v1` (LM Studio). |
| **Model** | _empty_ | Leave blank for the provider default. Examples: `claude-sonnet-4-6` (Anthropic), `gpt-4.1-mini` (OpenAI), `kimi-k2-0905-preview` (Kimi), or your local model name. |
| **API Key** | _empty_ | Per-site, stored encrypted (Frappe `Password` field). Most local OpenAI-compatible endpoints don't need one; hosted ones do. |

#### Use the LLM for (section toggles)

Three independent on/off switches. Turning any one off is a **hard
disable** — the section is never auto-generated, the matching button is
hidden, the API refuses, and re-rendered reports omit the block.

| Field | Default | Purpose |
|---|---|---|
| **Fix suggestions on findings** | ✓ on | "Suggest a fix (AI)" / "Generate AI fixes" / "Re-evaluate AI fixes" buttons on findings. |
| **Index recommendations (DB-tables breakdown)** | ✓ on | "Suggest an index (AI)" button in the per-table breakdown. |
| **Humanized "Steps to Reproduce"** | ✓ on | LLM rewrites the auto-captured action list into a friendly flow at analyze time (and on demand). Falls back to the raw action list on any failure. |

#### Automatic Suggestions section

| Field | Default | Purpose |
|---|---|---|
| **Suggest AI fixes in the report by default** | ✗ off | When on, the analyze pipeline auto-generates a fix for each eligible finding so suggestions are already in the report — no need to click per finding. Costs LLM tokens (and a little analyze time) on every session. Keep off unless you want suggestions baked in. |
| **Max auto-suggested findings per session** | `5` | Cap on how many findings get an automatic suggestion — highest-severity, highest-impact first. `0` = every eligible finding (can be slow + costly on big sessions). _Strict 10 · Recommended 5 · Relaxed 3._ |

#### Privacy & Operations section

| Field | Default | Purpose |
|---|---|---|
| **Excluded finding types** | _empty_ | One finding type per line. Those types are skipped in both auto-suggest and on-demand — the payload is never built (no data ever sent for them). Exact-match, case-sensitive. `#` comments. Canonical names: `Filesort`, `Framework N+1`, `Full Table Scan`, `Hot Line`, `Low Filter Ratio`, `Missing Index`, `N+1 Query`, `Redundant Call`, `Slow Query`, `Temporary Table`. |
| **Request timeout (seconds)** | `60` | HTTP timeout for outbound LLM calls. `60s` fits hosted providers (Anthropic / OpenAI reply in 2–10s typically). For local LLMs (Ollama / LM Studio / vLLM) first-token cold-start can exceed 60s — start at `180` and tune once warm-call P99 is known. Clamped to `10–600`. |

---

## Configuration — site_config.json (operator knobs)

Knobs that don't have a UI yet — usually because they're emergency
levers, performance trade-offs, or security hardening that an admin
should not be flipping casually. All live in
`sites/<your-site>/site_config.json`; all are optional; defaults are
inert.

A handful of them (the threshold ones — `optimus_redundant_*_threshold`,
`optimus_n_plus_one_threshold`, `optimus_sampler_interval_ms`) also work
as pre-DocType fallbacks. The DocType row wins if both are set.

### Recording capacity

| Key | Default | Purpose |
|---|---|---|
| `optimus_max_recordings_per_session` | `200` | Soft cap on HTTP requests + bg jobs per session. When hit, further recordings are silently dropped and the report shows a truncation banner. |
| `optimus_inline_analyze_limit` | `50` | Max recordings allowed for inline analyze on scheduler-disabled sites. Sessions larger than this are refused with an actionable error pointing at `bench enable-scheduler` + the Retry Analyze button. |

### Analyze pipeline knobs

| Key | Default | Purpose |
|---|---|---|
| `optimus_explain_cache_ttl_seconds` | `3600` | How long `EXPLAIN` results are cached in Redis across analyze runs. `0` disables the cross-session cache. |
| `optimus_analyze_gc_collect` | `True` | After analyze frees the pyinstrument session blob, call `gc.collect()` to return RAM to the OS. Safe-on default. Set `False` only if you've measured and the collect pause matters. |
| `optimus_analyze_nice` | `5` | `os.nice` increment for the async analyze worker — lower CPU priority so it doesn't fight live requests. `0` disables. Linux only; ignored on macOS. |
| `optimus_singleflight_max_wait_seconds` | `600` | When two re-analyzes race for the same session, the second waits up to this long (with polite re-enqueue) for the first to finish. `0` disables single-flight. |
| `optimus_enrich_throttle_every` | `0` | Sleep every N enriched queries during analyze (for EXPLAIN). `0` = no throttle (default). Raise to e.g. `200` on shared MariaDB instances where back-to-back EXPLAINs cause noisy-neighbor issues. |
| `optimus_enrich_throttle_sleep_ms` | `5` | Sleep length when the throttle above fires. |

### Phase 2

| Key | Default | Purpose |
|---|---|---|
| `optimus_phase2_overhead_budget_seconds` | `10` | Per-request line-profile overhead budget. On expiry, line tracing disengages and the run is flagged "partial data". Stops a hot-loop pick from freezing the UI. `0` = unbounded (NOT recommended on production). |
| `optimus_phase2_auto_arm` | `False` | When set, after each Ready session the analyze pipeline auto-arms a Phase 2 pass on the recommended hot paths. Opt-in + admin-only by design (the next user-flow gets instrumented automatically). |

### Security / signing

| Key | Default | Purpose |
|---|---|---|
| `optimus_allow_unsigned_pickles` | `True` | Whether to accept legacy un-signed pyinstrument blobs in Redis during analyze. Defaults to `True` to keep pre-v0.7 sessions analyzable. Set `False` on hardened sites to refuse unsigned blobs entirely (requires `encryption_key` in site_config). |

### Infra-pressure analyzer

| Key | Default | Purpose |
|---|---|---|
| `optimus_infra_cpu_high_pct` | `85` | CPU % at which Resource Contention is Medium. |
| `optimus_infra_cpu_critical_pct` | `95` | CPU % at which severity escalates to High. |
| `optimus_infra_rss_delta_high_mb` | `200` | Worker RSS growth threshold for Memory Pressure (Medium). |
| `optimus_infra_rss_delta_critical_mb` | `500` | RSS delta for High severity. |
| `optimus_infra_swap_warn_mb` | `100` | Swap-active threshold. Any active swap is a yellow flag. |
| `optimus_infra_db_pool_high_ratio` | `0.9` | `threads_connected / max_connections` ratio for DB Pool Saturation. |
| `optimus_infra_rq_backlog_warn` | `50` | RQ queue depth threshold for Background Queue Backlog. |

### N+1 / redundancy fallbacks (DocType wins if set)

These are pre-DocType compatibility — the DocType field with the same
purpose is the recommended surface. Listed here for sites still tuned
through site_config.

| Key | Default | Maps to DocType field |
|---|---|---|
| `optimus_n_plus_one_threshold` | `10` | N+1 Minimum Occurrences |
| `optimus_n_plus_one_min_total_ms` | `20` | _(no DocType equivalent — min cumulative ms for an N+1 group)_ |
| `optimus_redundant_doc_threshold` | `5` | Redundant `get_doc` Threshold |
| `optimus_redundant_cache_threshold` | `50` | Redundant Cache Lookup Threshold |
| `optimus_redundant_perm_threshold` | `10` | Redundant Permission Check Threshold |
| `optimus_redundant_high_multiplier` | `5` | _(no DocType equivalent — multiplier above which severity escalates to High)_ |
| `optimus_sampler_interval_ms` | `1` | Sampler Interval (ms) |

### Retention (DocType wins)

| Key | Default | Maps to DocType field |
|---|---|---|
| `optimus_session_retention_days` | `30` | Session Retention (days) |

---

## Runtime flags

Set per session via the widget's start dialog or `api.start(...)`:

| Flag | Default | Purpose |
|---|---|---|
| `label` (str, required) | — | Human-readable session label. |
| `capture_python_tree` (bool) | `True` | Capture pyinstrument call tree + sidecar wraps for redundant-call detection. Disable to get v0.2.0 SQL-only behavior with ~10–30% overhead instead of 1.5–2×. |
| `notes` (str) | `""` | Free-form "Steps to Reproduce / Notes" Text Editor content. Rendered at the top of both Safe and Raw reports. Bleach-sanitized before render — safe to include rich formatting but `<script>` tags are stripped. |

Example Python call:

```python
from optimus import api

api.start(
    label="Sales Invoice with 50 items",
    capture_python_tree=True,
    notes="<p>Click New Sales Invoice, add 50 items, hit Save.</p>",
)
# run your flow in another browser tab
api.stop()
```

---

## Custom analyzers

Third-party Frappe apps can contribute analyzers without forking. In your app's `hooks.py`:

```python
optimus_analyzers = [
    "my_app.performance.analyzers.orders.analyze",
    "my_app.performance.analyzers.payments.analyze",
]
```

Custom analyzers run **after** the 10 builtins and share the same `AnalyzeContext`. Each must be a pure function with signature:

```python
def analyze(
    recordings: list[dict],
    context: optimus.analyzers.base.AnalyzeContext,
) -> optimus.analyzers.base.AnalyzerResult:
    ...
```

Contract:
- **No Frappe DB access** inside the function — analyzers are pure transformations over the recording data. This makes them unit-testable from JSON fixtures with no running site.
- Exceptions are caught by `analyze.run` and logged; a failing custom analyzer never halts the pipeline, but any findings it would have emitted are lost for that session.
- Custom analyzers can read earlier analyzers' output from `context.actions`, `context.findings`, and `context.aggregate`.
- A 60-second soft cap per analyzer logs a warning; the 20-minute total budget aborts remaining analyzers with a partial-completion warning.

See [`optimus/analyzers/base.py`](./optimus/analyzers/base.py) for the full type contract and the analyzers under [`optimus/analyzers/`](./optimus/analyzers/) for working examples (each is a self-contained module — `n_plus_one.py`, `call_tree.py`, `redundant_calls.py`, and `infra_pressure.py` are good starting points).

---

## Verification checklist

After `bench migrate`, verify in this order:

1. **DocTypes exist:**
   ```bash
   bench --site <site> mariadb -e "SHOW TABLES LIKE 'tabOptimus%';"
   ```
   Should list `tabOptimus Session`, `tabOptimus Action`, `tabOptimus Finding`, `tabOptimus Settings`, `tabOptimus Tracked App`, `tabOptimus Phase Two Run`.

2. **Enqueue monkey-patch is active:**
   ```bash
   bench --site <site> console
   >>> import frappe
   >>> frappe.enqueue._profiler_patched
   True
   ```

3. **Version matches the running code:**
   ```bash
   bench --site <site> console
   >>> import optimus
   >>> optimus.__version__
   '0.12.26'
   ```
   If this returns an older version, `bench restart` didn't land — workers are stale.

4. **Floating widget appears in Desk:** log in as a System Manager, open any Desk page, look bottom-right for the red **Optimus** pill. Hover it — the tooltip should show the current build ID. Open devtools → Console — you should see `[optimus] floating_widget.js LOADED build=... at ...`.

5. **Correlation header is set:** start a session, open devtools → Network, click any link in Desk, inspect the response headers. You should see `X-Optimus-Recording-Id` AND `Access-Control-Expose-Headers: X-Optimus-Recording-Id` (without the second header, browsers hide the custom header from JavaScript — this is the single most common frontend instrumentation failure mode).

6. **Full end-to-end smoke test:**
   ```python
   >>> from optimus import api
   >>> api.start(label="smoke", notes="quick verification")
   >>> # in another browser tab, open a Sales Invoice list
   >>> api.stop()
   >>> # wait 5–10 seconds for the analyze worker
   >>> doc = frappe.get_last_doc("Optimus Session")
   >>> doc.status
   'Ready'
   >>> len(doc.actions), len(doc.findings)
   ```

7. **Safe Report is self-contained:** open `doc.safe_report_file` in a browser with network disabled. It must render fully — no missing fonts, no broken layout. Tested in CI via `test_safe_report_self_contained.py`.

8. **Scheduler-disabled fallback:** `bench --site <site> disable-scheduler`, reload Desk, run a session, click Stop. The widget should transition straight from "Stopping…" to "Report ready" (no intermediate "Analyzing…"). Re-enable: `bench --site <site> enable-scheduler`.

9. **Baseline comparison:** pin a Ready session as baseline, record a second session with the same label, verify the second report has three comparison sections at the top.

10. **PDF export:** open a Ready session, click "Download Safe Report (PDF)". First click generates in ~2 seconds and caches; subsequent clicks are instant.

---

## Troubleshooting

### The widget is still showing "Recording" after I clicked Stop

**Most likely cause:** browser is serving cached JS. The `app_include_js` cache-buster rotates on `__version__` bumps, and if you've been testing across dev iterations without a full restart, the browser is still running the first version it loaded.

**Fix (in order):**

1. `bench restart` — reloads the Python workers so they see the updated `__version__`.
2. Hard-refresh Desk in the browser: `Cmd+Shift+R` (Mac) / `Ctrl+Shift+R` (Windows/Linux).
3. Verify in devtools → Console: you should see `[optimus] floating_widget.js LOADED build=<current build> at ...`. Hover the widget pill — the tooltip should show the same build ID.
4. If the build ID matches and the bug still reproduces, open devtools → Console, click Stop, and check the `[optimus] stop callback: {...}` log. Paste it with a bug report.

### Stop button is silently doing nothing

**Most likely cause:** `api.start` or `api.stop` is returning a server error and `frappe.call` is not invoking the success callback. The widget has explicit error handlers for this case (added in v0.5.1) — they show a red toast in the top-right corner. Look there first. Also check Frappe's error log:

```bash
bench --site <site> mariadb -e "SELECT method, error FROM \`tabError Log\` WHERE method LIKE 'optimus%' ORDER BY creation DESC LIMIT 5;"
```

### "No active session" after clicking Stop

The session was already cleared server-side — usually because the auto-stop TTL expired (10 minutes of inactivity) or the janitor swept it. v0.5.1 handles this cleanly: the widget resets to inactive with a gray toast *"Session already stopped."* If you see the widget stuck on "Analyzing…" after this, you're on pre-v0.5.1 JS (see cache troubleshooting above).

### Missing Index finding suggests a column that's already indexed

**Shouldn't happen** in v0.5.1 — the analyzer verifies against `information_schema` before emitting. If you do see this, check the session's `analyzer_warnings`: suppressed suggestions are reported there with their reason. If a genuinely false-positive finding is still reaching the report, please file a bug with the full `technical_detail_json` attached.

### Repeated Hot Frame shows generic names like `wrapper` or `handle`

**Shouldn't happen** in v0.5.1 — the aggregator now groups by `file::function` instead of the bare function name, and skips pure plumbing (werkzeug, `frappe.handler`, `frappe.utils`). If you still see this, verify the widget build ID is `2026-04-15-stop-fix-v3` or later.

### Scheduler is disabled and stop is taking forever

On scheduler-disabled sites, analyze runs inline inside the stop request. A session with many recordings can take 10–30 seconds; the widget shows "Stopping…" the whole time. If it exceeds ~60 seconds, gunicorn's request timeout is at risk — lower `optimus_inline_analyze_limit` in site_config or re-enable the scheduler.

### Call tree is huge and slows down the Optimus Session form load

v0.5.0 caps `v5_aggregate_json` at 200 timeline entries + 500 XHR matches + 100 orphans with tail-preferring truncation. If you're still seeing slow form loads, check `analyzer_warnings` for the truncation count and the per-action `call_tree_json` field — trees larger than 200 KB overflow to a private File attachment rather than inlining.

---

## Development

### Running the test suite

```bash
cd ~/frappe-bench/apps/optimus
python -m pytest optimus/tests/ -v
```

1820+ unit tests run in ~7 seconds on a laptop. The suite is **decoupled from Frappe** — most tests use JSON fixtures and mocked `frappe.cache` / `frappe.db`, so you can run them without a site. Tests that do need Frappe import guards are gated via `pytest.importorskip` or stubbed at module level.

A second tier of 39 **integration tests** under `optimus/tests_integration/` exercises a real Frappe v16 bench provisioned by `.github/helper/install.sh` and runs via `bench --site <site> run-tests --app optimus`. These cover install-time invariants, scheduler/cron paths, the atomic Lua merge under multi-worker load, and other behaviors that can't be proven from pure-pytest stubs. See `optimus/tests_integration/README.md` for the local-run recipe.

### Test organization

- `tests/test_<analyzer>_*.py` — per-analyzer unit tests with JSON fixtures
- `tests/fixtures/*.json` — recording blobs (sanitized) used across analyzer tests
- `tests/test_frontend_assets.py` — JS syntax + widget structure regression guards (uses `node --check`)
- `tests/test_*_v5_*.py` — v0.5.0 integration tests (infra + frontend end-to-end)
- `tests/test_analyze_run_*_wiring.py` — source-inspection regression guards for orchestration changes

### Adding an analyzer

1. Create `optimus/analyzers/my_analyzer.py` with a pure `analyze(recordings, context) -> AnalyzerResult` function.
2. Add it to `_BUILTIN_ANALYZERS` in `analyze.py` OR publish a site-config / `hooks.py` `optimus_analyzers` entry.
3. Write a test in `tests/test_my_analyzer.py` using existing fixtures under `tests/fixtures/`.
4. If the analyzer produces new finding types, add them to the enum in `doctype/optimus_finding/optimus_finding.json` and write a patch under `patches/v0_X_Y/` that reloads the doctype.

See `optimus/analyzers/infra_pressure.py` for a recent example including the `_conf()` pattern for site-configurable thresholds.

---

## Contributing

MIT-licensed Frappe app. Contributions welcome via PR.

**Before submitting:**

- Run `pytest optimus/tests/ -v` — all 1820+ unit tests must pass.
- Run `node --check` on any JS changes.
- Bump `__version__` in `optimus/__init__.py` for any user-visible change so the asset cache-buster rotates.
- Add a CHANGELOG entry under the current unreleased section.
- For new analyzers: see the interface contract in [`optimus/analyzers/base.py`](./optimus/analyzers/base.py).

**Bug reports:** please include:

- `__version__`
- Browser console output (widget is noisy on purpose, look for `[optimus]` lines)
- Relevant `Error Log` entries from the site
- If it's an analyzer false positive, attach the `technical_detail_json` from the finding

---

## License

MIT — see [`license.txt`](./license.txt).
