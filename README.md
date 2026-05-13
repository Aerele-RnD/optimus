# Optimus

**A flow-aware performance profiler for Frappe and ERPNext.** Records a real business workflow (Sales Invoice save → submit → Delivery Note → submit → …), joins it with server resource state and browser-side timings, and produces two downloadable HTML reports you can actually act on: a **Safe Report** to share with a third-party dev shop without leaking customer data, and a **Raw Report** for internal debugging with full stack traces and SQL literals.

> **Status:** `v0.7.0` — production-ready. MIT-licensed. 1281 tests in CI on every push (ruff + pytest matrix on Python 3.12 / 3.14). See the [CHANGELOG](./CHANGELOG.md) for the full feature history, including the v0.7.0 rename from `frappe_profiler` → `optimus`.
>
> **Design docs:** `apps/frappe_profiler_design/` holds the architecture deep-dive, spec history, and planning notes. The architecture rationale and extension points live in [`ARCHITECTURE.md`](../frappe_profiler_design/ARCHITECTURE.md).

---

## Table of contents

- [What it is](#what-it-is)
- [What it isn't](#what-it-isnt)
- [Install](#install)
- [60-second quickstart](#60-second-quickstart)
- [The customer → partner handoff](#the-customer--partner-handoff)
- [Finding types](#finding-types)
- [How it works](#how-it-works)
- [Dependencies](#dependencies)
- [Comparison with alternatives](#comparison-with-alternatives)
- [Production safety](#production-safety)
- [Scheduler-disabled sites](#scheduler-disabled-sites)
- [Configuration](#configuration)
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
bench get-app https://github.com/<your-org>/optimus.git
bench --site <your-site> install-app optimus
bench --site <your-site> migrate
bench restart
```

Tested on **Frappe v16** with MariaDB and Redis. The app declares `required_apps = ["frappe"]` and has one external dependency (`pyinstrument >= 4.6, < 6`, pure-Python, no compiled extensions).

After install, a **Optimus User** role is created automatically. All existing System Managers are granted this role, and new System Managers get it automatically via a `User.validate` hook.

---

## 60-second quickstart

1. Open Desk. A bright red **Profiler** pill appears in the bottom-right corner.
2. Click it. A dialog asks for a label, an optional "Steps to Reproduce" note, and a `Capture Python call tree` toggle (leave on).
3. Click **Start**. The pill turns green and shows an elapsed timer.
4. Run your flow — save a Sales Invoice, submit it, wait for background jobs, whatever you want profiled.
5. Click the pill again to **Stop**. The pill turns orange ("Analyzing…") while the analyze pipeline runs (typically 2–15 seconds).
6. Click the pill once more (now blue, "Report ready") to jump to the session form.
7. Click **Download Safe Report** or **Download Safe Report (PDF)**. Email it to whoever needs it.

That's the whole workflow. No configuration required.

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

For the full architecture (data-flow diagrams, hook order, edge cases, extension points), see [`ARCHITECTURE.md`](../frappe_profiler_design/ARCHITECTURE.md).

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

## Configuration

All knobs live in `sites/<your-site>/site_config.json`. Every value is optional; defaults are sensible for most deployments.

### Recording & analyze

| Key | Default | Purpose |
|---|---|---|
| `optimus_max_recordings_per_session` | `200` | Soft cap on HTTP requests + background jobs per session. When hit, further recordings are silently dropped and the report shows an `analyzer_warnings` banner. |
| `optimus_session_retention_days` | `90` | Sessions in `Ready` / `Failed` state older than this are deleted by the daily janitor, along with attached HTML report files. |
| `optimus_inline_analyze_limit` | `50` | Max recordings allowed for inline analyze on scheduler-disabled sites. Sessions larger than this are refused with an actionable error. |
| `optimus_explain_cache_ttl_seconds` | `3600` | How long EXPLAIN results are cached in Redis across analyze runs. Set `0` to disable cross-session cache. |

### N+1 detection

| Key | Default | Purpose |
|---|---|---|
| `optimus_n_plus_one_threshold` | `10` | Minimum repetition count before a group is flagged as N+1. Bump higher on legacy ERPNext codebases with many legitimate N-ish patterns. |
| `optimus_n_plus_one_min_total_ms` | `20` | Minimum cumulative time a group must consume before it becomes a finding. Prevents `10 × 0.1 ms` noise. |

### Python call tree (v0.3.0+)

| Key | Default | Purpose |
|---|---|---|
| `optimus_sampler_interval_ms` | `1` | pyinstrument sampling interval in ms. Higher = less overhead, lower fidelity. |
| `optimus_tree_prune_threshold_pct` | `0.005` | Drop frames below this % of action wall time. `0.005` = 0.5%. |
| `optimus_tree_node_cap` | `500` | Max nodes per persisted tree (hot path always preserved). |

### Redundant-call detection (v0.3.0+)

| Key | Default | Purpose |
|---|---|---|
| `optimus_redundant_doc_threshold` | `5` | Min repetitions of `frappe.get_doc(doctype, name)` from the same callsite. |
| `optimus_redundant_cache_threshold` | `10` | Min repetitions of `frappe.cache.get_value(key)`. |
| `optimus_redundant_perm_threshold` | `10` | Min repetitions of `has_permission(...)`. |
| `optimus_redundant_high_multiplier` | `5` | Multiplier above which severity escalates to High. |
| `optimus_safe_extra_allowed_apps` | `[]` | Extra app prefixes whose function names stay un-redacted in Safe mode. |

### Infra pressure (v0.5.0+)

| Key | Default | Purpose |
|---|---|---|
| `optimus_infra_cpu_high_pct` | `85` | CPU% threshold for Resource Contention finding. |
| `optimus_infra_cpu_critical_pct` | `95` | CPU% at which severity escalates to High. |
| `optimus_infra_rss_delta_high_mb` | `200` | Worker RSS growth threshold for Memory Pressure. |
| `optimus_infra_rss_delta_critical_mb` | `500` | RSS delta for High severity. |
| `optimus_infra_swap_warn_mb` | `100` | Swap usage threshold. Any active swap is a yellow flag. |
| `optimus_infra_db_pool_high_ratio` | `0.9` | `threads_connected / max_connections` ratio for DB Pool Saturation. |
| `optimus_infra_rq_backlog_warn` | `50` | RQ queue depth threshold for Background Queue Backlog. |

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

See [`optimus/analyzers/base.py`](./optimus/analyzers/base.py) for the full type contract and [`ARCHITECTURE.md`](../frappe_profiler_design/ARCHITECTURE.md) *Extension Points* for examples.

---

## Verification checklist

After `bench migrate`, verify in this order:

1. **DocTypes exist:**
   ```bash
   bench --site <site> mariadb -e "SHOW TABLES LIKE 'tabProfiler%';"
   ```
   Should list `tabOptimus Session`, `tabOptimus Action`, `tabOptimus Finding`.

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
   '0.5.1'
   ```
   If this returns an older version, `bench restart` didn't land — workers are stale.

4. **Floating widget appears in Desk:** log in as a System Manager, open any Desk page, look bottom-right for the red **Profiler** pill. Hover it — the tooltip should show the current build ID. Open devtools → Console — you should see `[optimus] floating_widget.js LOADED build=... at ...`.

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

326+ tests run in ~5 seconds on a laptop. The suite is **decoupled from Frappe** — most tests use JSON fixtures and mocked `frappe.cache` / `frappe.db`, so you can run them without a site. Tests that do need Frappe import guards are gated via `pytest.importorskip` or stubbed at module level.

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

- Run `pytest optimus/tests/ -v` — all 326+ tests must pass.
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
