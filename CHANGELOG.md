# Changelog

All notable changes to the Optimus app.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project follows [SemVer](https://semver.org/) (pre-1.0, so minor
versions may contain breaking changes — see migration notes below).

---

## [0.7.0] — 2026-05-13

**The rename release.** The app rebrands from `frappe_profiler` →
`optimus`, end-to-end: the Python package, the GitHub repo, every
DocType, the auto-installed Role, the realtime channels, the
`X-Profiler-Recording-Id` HTTP correlation header, and every
user-facing string in the report HTML and floating widget.

Also bundles the v0.6.x development cycle that shipped to the main
branch since v0.5.2: line-profile Phase 2 drilldown, the audit-response
patches (DocType title-case, redundant-cache threshold bump, hidden
framework-tables default, safe-mode field deletions, AI fix fields
revamp), and the per-finding drill-down chain that walks pyinstrument
trees down to the first signal-floor leaf.

### Breaking

All existing installs must run `bench migrate` after upgrading. The
migration patches in `optimus.patches.v0_7_0` handle:

  - **`tabPatch Log`** entries rewritten from `frappe_profiler.patches.*`
    → `optimus.patches.*` so historical patches aren't re-run under
    their new module paths.
  - **`tabModule Def`** row renamed: `Frappe Profiler` → `Optimus`,
    `app_name` updated to `optimus`.
  - **DocTypes** (6) renamed via `frappe.rename_doc`:
    - `Profiler Action` → `Optimus Action`
    - `Profiler Finding` → `Optimus Finding`
    - `Profiler Phase Two Run` → `Optimus Phase Two Run`
    - `Profiler Session` → `Optimus Session`
    - `Profiler Settings` → `Optimus Settings`
    - `Profiler Tracked App` → `Optimus Tracked App`
    Under the hood `rename_doc` rewrites every `tab<DocType>` SQL
    table name plus child-table `parenttype` columns.
  - **Role** renamed: `Profiler User` → `Optimus User`. Every
    `tabHas Role` row carrying the old role keeps its user assignment
    through the rename.
  - **`Optimus Tracked App.app_name`** child rows updated:
    `frappe_profiler` → `optimus` if present.
  - **Realtime channels** renamed: `profiler_session_*` →
    `optimus_session_*`, `profiler_progress` → `optimus_progress`.
    Live widget pages reconnect to the new channels on reload.
  - **HTTP header** renamed: `X-Profiler-Recording-Id` →
    `X-Optimus-Recording-Id`. External integrations reading the
    header must update.
  - **`frappe.local.profiler_*` attributes** and **`frappe.conf.get
    ("profiler_*")` keys** renamed to `optimus_*`. Users who set
    `profiler_max_recordings_per_session` / `profiler_sampler_
    interval_ms` / etc. in `site_config.json` need to rename those
    keys manually.

### User-side upgrade (from v0.5.x or v0.6.x)

```bash
bench stop
sed -i.bak 's/^frappe_profiler$/optimus/' sites/apps.txt
mv apps/frappe_profiler apps/optimus
cd apps/optimus && git remote set-url origin https://github.com/Aerele-RnD/optimus.git && git pull origin main
bench start  # in another terminal
bench --site <yoursite> migrate
```

The `git remote set-url` step picks up the renamed GitHub repo;
GitHub auto-redirects the old URL indefinitely so the step is
optional but cleaner.

### Added (rolled in from v0.6.x development)

  - Line-profile Phase 2 drilldown — pick a hot function, profile it
    line-by-line on a second recording, render hit/per-hit timing
    next to the source.
  - Per-finding drill-down chain — walks the pyinstrument tree from
    a finding's origin frame down to the first leaf below the
    signal floor (10% of origin cumulative_ms), rendered as a chain
    of indented call-site links.
  - AI fix suggestions — `Suggest a fix (AI)` action on every finding
    sends the smoking-gun source window + recorded SQL evidence to
    the configured LLM endpoint and renders the response inline.

### Changed

  - DocType name `Profiler Phase 2 Run` → `Profiler Phase Two Run`
    (intermediate v0.6.0 rename) → `Optimus Phase Two Run` (v0.7.0).
  - `redundant_cache_threshold` default bumped 10 → 50 (lab data
    showed the lower default fired on benign hooks).

### Fixed

  - Suite isolation: per-test `sys.modules` fence in `conftest.py`
    snapshots and restores per test, so stub-installer tests don't
    pollute downstream tests in the same pytest session.
  - `frappe.db` is a Werkzeug Local proxy — tests now replace it
    wholesale via `monkeypatch.setattr` instead of patching its
    attributes (which the proxy intercepts inconsistently).

### CI

  - GitHub Actions workflow runs ruff + pytest matrix (Python 3.12 +
    3.14) on every push. The baseline frappe stub in conftest.py
    lets the suite collect on a non-bench Python without installing
    frappe.

---

## [0.5.1] — 2026-04-15

The "architect review" release. After v0.5.0 landed on the branch, we
did seven back-to-back architect-review passes over the entire diff
looking for production bugs, false positives, and bad UX. Each pass
found 2–3 real issues of a different class — surface bugs, tests
mirroring broken production code, end-to-end error path regressions,
HTTP-layer integration gaps, inconsistent helper adoption, and
schema-field typos. This release bundles all of those fixes plus the
user-reported widget bugs that surfaced during manual smoke testing
against a real site. Zero new features — entirely product quality
and correctness work.

**No DocType schema changes.** No migration needed beyond
`bench restart` + hard browser refresh.

### Fixed — security

- **Stored XSS bypass via `sanitize_html` JSON fast-path.** The
  v0.5.0 renderer called Frappe's `sanitize_html` on the `notes`
  field before passing to the template's `|safe` filter — but
  without `always_sanitize=True`, sanitize_html has a JSON fast-path
  that returns the input unchanged when it parses as valid JSON.
  An attacker could set `notes` to `'{"x":"<script>alert(1)</script>"}'`
  (a valid JSON string literal containing a script tag) and the
  fast-path would pass it through, letting the template render a
  live `<script>` tag to anyone viewing the session. Fixed by
  passing `always_sanitize=True` so nh3/bleach runs on every input
  regardless of format detection, with an `html.escape` fallback
  if sanitize_html itself fails.

- **Safe Report URL redaction switched from allowlist to denylist.**
  `_safe_url`'s query-string redactor previously used an allowlist
  of known-PII keys (`source_name`, `filters`, etc.) and passed
  through everything else. A custom filter key added by a third-party
  app would silently leak PII in Safe mode. v0.5.1 redacts every
  query-string value by default and whitelists only schema refs,
  pagination, sort flags, and format hints (`doctype`, `limit`,
  `order_by`, `as_dict`, etc.). Unknown keys now redact, which is
  the safe direction.

- **`_DOCNAME_PATH_RE` now skips Frappe reserved second-segments.**
  `/app/<doctype>/view/list` used to redact `view` as if it were a
  docname, producing `/app/sales-invoice/<name>/list`. Cosmetic
  but semantically wrong. v0.5.1 guards against 13 reserved
  keywords (`view`, `list`, `new`, `edit`, `report`, `tree`,
  `dashboard`, `calendar`, `kanban`, `gantt`, `image`, `inbox`,
  `print`) so only actual docnames get stripped.

- **`_inject_correlation_header` uses tokenwise idempotency check,
  not substring match.** Previously the `X-Optimus-Recording-Id not
  in existing` check was a substring compare. If another app had
  already set `Access-Control-Expose-Headers: X-Optimus-Recording-Id-Legacy`
  (or similar), the substring match would falsely think our header
  was already present and skip appending it — silently breaking
  the entire frontend correlation feature because the browser would
  refuse to surface the real header to JavaScript. Fixed with a
  proper comma-split case-insensitive token comparison.

- **Correlation header gated on active profiler session, not just
  recorder presence.** The `after_request` hook previously injected
  `X-Optimus-Recording-Id` whenever `frappe.local._recorder` had a
  `.uuid` — which is true any time the standalone Frappe Recorder UI
  is running, even for users who have no profiler session. The header
  was leaking onto every recorded response site-wide, and
  `optimus_frontend.js` was buffering XHRs tagged to a recording
  that no session could claim. Now gated on
  `frappe.local.optimus_session_id` which is only set by our own
  `before_request` hook.

### Fixed — production bugs that tests were covering for

These bugs shipped with v0.5.0 because my tests mocked the same
broken pattern the production code used, so the test suite rubber-
stamped the bugs. v0.5.1 includes new regression guards that would
have caught each one via behavioral tests instead of source-string
matching.

- **`infra_capture` tried to access `frappe.cache.redis` as a child
  attribute.** But `frappe.cache` IS a `redis.Redis` subclass
  (`RedisWrapper` at `frappe/utils/redis_wrapper.py`), not a wrapper
  with a `.redis` child. `getattr(frappe.cache, "redis", None)`
  returned `None` in production, silently disabling Redis ops/sec
  and all RQ queue depth metrics. Every production snapshot since
  v0.5.0 landed would have had those keys as None. The `FakeCache`
  mock mirrored the broken code exactly (`FakeCache.redis = ...`)
  so the tests passed without exercising the real access pattern.
  Fixed by calling `frappe.cache.info("stats")` directly and
  passing `frappe.cache` as the RQ connection. New `Tripwire` test
  stub raises on `.redis` access and asserts `info()` is called on
  the root object — behavioral catch instead of string matching.

- **Cap-exceeded failure path wrote to phantom `analyze_error`
  field.** v0.5.0's inline-analyze safety cap (default 50 recordings)
  called `frappe.db.set_value("Optimus Session", docname,
  {"analyze_error": "..."})` — but that field does NOT exist on the
  doctype. The real field is `analyzer_warnings` (plural). On
  scheduler-disabled sites with ≥51 recordings, clicking Stop
  crashed with MariaDB `Unknown column 'analyze_error' in 'field
  list'`, the stop API returned 500, and the widget stranded the
  user on Stopping→Analyzing→hang-forever. Fixed by writing to the
  real field, with a test that explicitly asserts the payload dict
  contains `analyzer_warnings` AND does NOT contain `analyze_error`.

- **Inline analyze failure path stranded the widget.** `analyze.run`
  catches its own exceptions, marks the session Failed, and
  re-raises. When analyze ran inline via `frappe.enqueue(now=True)`,
  the re-raise propagated all the way up through `_enqueue_analyze`
  → `_stop_session` → `stop()` → the client. The widget's error
  callback fired, showed "Failed to stop profiler — try again,"
  and reset the widget to Recording — but the session was actually
  Failed server-side. User clicks again, `status()` says no active
  session, widget falls into "Analyzing…" and hangs forever. Fixed
  by catching the inline-analyze re-raise in `_enqueue_analyze` and
  having `stop()` read the final session status from the DocType
  before returning. The widget now branches on `data.status` when
  `ran_inline` is true to show "Report ready" or "Analyze failed"
  correctly.

- **`submit_frontend_metrics` had a GET-merge-SET race.** Two
  concurrent submits (stop-time `frappe.call` racing a `beforeunload`
  sendBeacon) could both read the same existing blob, both compute
  a merged result, and both write — losing one submission's data.
  v0.5.1 switched to two atomic Redis lists per session
  (`profiler:frontend:<uuid>:xhr` and `:vitals`) written via RPUSH +
  LTRIM. Each submit appends its entries atomically; LTRIM enforces
  the soft cap tail-preferring so the newest entries survive on
  overflow. A new `_read_frontend_data` helper decodes the lists
  back into the dict shape `frontend_timings.analyze` expects.
  Legacy single-blob fallback kept for upgrade safety on sessions
  captured just before the v0.5.1 upgrade.

- **sendBeacon silently dropped every payload.** The endpoint
  signature is `submit_frontend_metrics(payload: str)`, which works
  fine for the stop-time `frappe.call` path (sends `args:{payload: body}`
  via form encoding). But sendBeacon sends the raw JSON body as
  `application/json`, and Frappe's request handler parses JSON
  bodies and flattens their top-level keys into `form_dict` as
  kwargs. So the server was being called with
  `submit_frontend_metrics(session_uuid=..., xhr=..., vitals=...)` —
  mismatching the `payload` signature and failing with `TypeError`
  deep in the request router, logged into Frappe's internal error
  log and never reaching our own. Every `beforeunload` beacon was
  silently failing. Fixed client-side: `optimus_frontend.js` now
  wraps the beacon body as `JSON.stringify({payload: body})` so
  Frappe's flattening produces `{"payload": "..."}` which matches
  the endpoint signature.

### Fixed — false positives in findings

- **Missing Index now verifies the column is actually not indexed.**
  v0.5.0 trusted `frappe.core.doctype.recorder.recorder._optimize_query`
  and emitted a finding for whatever column it suggested. But
  `DBOptimizer` is a heuristic that analyzes WHERE clauses — it
  does NOT check whether an index already exists. Every Frappe
  session would likely produce false positives for pre-indexed
  columns: primary keys (`name`), framework columns (`parent`,
  `owner`, `modified`, `creation`), and any Link/Data field with
  `search_index: 1`. v0.5.1 verifies each suggestion against
  `information_schema` before emitting:

    - `SHOW INDEX FROM <table>` → set of columns that are leftmost
      of at least one index (composite non-leftmost doesn't count,
      because btree can't serve queries filtering on just that col)
    - `information_schema.columns` → per-column data type

  Outcomes:
    - Column already indexed → suppressed, warning added to report
    - Column type is JSON / geometry → suppressed (not btree-indexable)
    - Column type is TEXT / BLOB → kept, but DDL rewritten to include
      a prefix length: `ADD INDEX \`idx_col\` (\`col\`(255))` — the
      plain DDL fails on TEXT with "BLOB/TEXT column used in key
      specification without a key length"
    - Column doesn't exist on table → suppressed (sql_metadata parse
      error hallucination guard)
    - Regular indexable column → kept with the plain DDL, finding
      now carries `verified_not_indexed: true` in technical_detail

  Per-table caching: one `SHOW INDEX` + one `information_schema`
  query per distinct table in the suggestions, not one per column.

- **Repeated Hot Frame used bare function name as the dedup key.**
  User ran v0.5.0 against a real session and reported two findings:
  `wrapper appeared in 11 actions and consumed 3534ms total` and
  `handle appeared in 10 actions and consumed 2984ms total`. Both
  were false positives. The aggregator used `function` alone as the
  cross-action dedup key, so 35 different functions called `wrapper`
  (functools decorator, werkzeug wrapper, `frappe.whitelist` wrapper,
  `RedisWrapper` methods, gunicorn worker wrappers, `cached_property`,
  etc.) all collapsed into a single `wrapper` bucket. The finding's
  customer description read *"optimizing it would help every flow
  that touches it"* — which is useless because there is no single
  function called `wrapper` the user can optimize; it's a name
  shared across dozens of unrelated implementations. v0.5.1 fixes
  by including the filename in the dedup key. Key format is
  `"short/path.py::function"` where `short` is the last two path
  segments — readable without leaking absolute paths.

- **Repeated Hot Frame was also suppressing legitimate Frappe
  application-layer targets.** The first fix of the above used the
  broad `_is_framework_frame` filter, which skipped ALL of `frappe/*`
  to remove the framework wrappers. But that's too aggressive:
  `Document.run_method` runs the user's own doc-event hooks,
  `has_permission` evaluates user-defined permission rules (including
  custom Permission Query Conditions), `make_autoname` runs the user's
  chosen naming series — all legitimate optimization targets inside
  `frappe/*`. v0.5.1 introduces a narrower `_is_pure_helper_frame`
  filter that only skips pure plumbing (`frappe/utils/`, `frappe/handler.py`,
  `frappe/app.py`, werkzeug, gunicorn, rq, pyinstrument itself,
  pytz, dateutil). Most of `frappe/*` is KEPT so findings remain
  useful when application-layer Frappe is the actual bottleneck.
  `_is_framework_frame` is unchanged and still used by SQL-to-Python
  reconciliation and Slow Hot Path findings, where the aggressive
  skip is correct.

- **DB Pool Saturation used the wrong ratio.** v0.5.0 computed
  `threads_running / threads_connected` — which measures *"of the
  currently open connections, what % are executing queries"* —
  and fired when that ratio exceeded 0.9. On a dev box with 5
  connections and 5 of them busy, that's 1.0 → fires the finding,
  even though MariaDB has 495 pool slots unused. The correct
  metric is `threads_connected / max_connections`. v0.5.1 reads
  `max_connections` from `SHOW VARIABLES` (cached at module level
  since it's a config value) and uses the correct ratio, with a
  legacy fallback to the old proxy for pre-v0.5.1 infra blobs.

- **`infra_pressure` crashed on non-dict `infra` value.** The
  guard was `infra = rec.get("infra") or {}; if not infra: continue`
  — which handles None and empty-dict but not a truthy non-dict
  (list, string) that could come from corrupt Redis data. Any
  such value would pass the falsy check and then crash on
  `infra.get(...)`, killing analyze.run for the entire session.
  Added `isinstance(infra, dict)` guard.

### Fixed — user-reported widget bugs

- **Widget stuck on "Recording" after clicking Stop.** Two
  compounding causes:

  1. **Cache buster inertia.** The `app_include_js` cache-buster
     uses `?v={__version__}`, and `__version__` stayed at `0.5.0`
     through a lot of JS edits. Browsers that loaded Desk once
     early in testing served cached JS from that first visit,
     invisible to every subsequent fix. v0.5.1 bumps to `0.5.1`
     and adds a hardcoded `WIDGET_BUILD_ID` constant
     (`2026-04-15-stop-fix-v3`) logged at script load and exposed
     on the widget element's `title` + `data-build-id` attributes
     so users can verify from devtools which JS is actually
     running without guessing. Longer-term, the cache-buster
     should hash file contents instead of relying on manual
     version bumps; flagged as a v0.6 followup.

  2. **Stop callback didn't handle `{stopped: false}` response.**
     When the stop API returns `{stopped: false, reason: "no active
     session"}` — which happens on auto-stop, janitor sweep, or a
     retried click after a network blip on the first stop — the
     callback fell into the else branch and transitioned the
     widget to "Analyzing…" despite nothing being analyzed
     server-side. No `optimus_session_ready` realtime event would
     ever fire, so the widget hung on Analyzing forever. v0.5.1
     checks `data.stopped === false` explicitly and resets the
     widget to inactive with a gray toast, clearing
     `currentState.session_uuid` and removing the
     `data-session-uuid` DOM attribute.

- **Stop error callback was too naive.** The previous error handler
  unconditionally reverted the widget to Recording and restarted
  the elapsed timer. But that's wrong when the stop actually
  succeeded server-side and the client only got a network error
  — the widget would show Recording despite the session being
  gone. v0.5.1 error handler calls `status()` to ask the server
  what actually happened: if active → revert to Recording, if
  inactive → reset to inactive with a "Session already stopped"
  toast, if status() also errors → true network failure with a
  "Network error" toast.

- **Start dialog silently failed on server error.** The
  `openStartDialog` `frappe.call(api.start)` had no error callback.
  Any server-side failure — permission denied, concurrent session
  conflict, server exception — made `frappe.call` silently skip
  the success callback and do nothing. Dialog closed, widget
  stayed inactive, no feedback. v0.5.1 adds an error handler that
  surfaces a red toast with actionable text, and the success path
  also surfaces an orange toast if the response came back without
  a `session_uuid` (unexpected 200).

- **Diagnostic logging added.** `confirmAndStop` now logs at entry,
  in the success callback (with the full response dict), and in
  the error callback (with the full error object). Log lines use
  the `[optimus]` prefix so they're easy to filter in
  devtools. Makes future "widget doesn't work" reports debuggable
  without adding ad-hoc logging after the fact.

### Fixed — inconsistent helper adoption

- **`retry_analyze` now uses `_enqueue_analyze` for the scheduler-
  aware fallback.** v0.5.0 added the scheduler fallback to
  `stop()` but left `retry_analyze` calling `frappe.enqueue`
  directly. On scheduler-disabled sites, clicking **Retry Analyze**
  on a Failed session would push to a queue no worker consumes,
  re-hitting the original hung-forever bug the v0.5.0 fallback
  was designed to fix. v0.5.1 threads `docname` through
  `_enqueue_analyze` so `retry_analyze` gets the same inline
  fallback and the same recording-count safety cap that `stop()`
  gets. Fixes the class of "I added a helper but didn't migrate
  the siblings" bug.

- **Inline-analyze cap moved from `_stop_session` into
  `_enqueue_analyze`.** Previously the cap was inline in
  `_stop_session`, so `retry_analyze` (and, in theory, the janitor
  auto-stop path) didn't get the protection. v0.5.1 moves the cap
  check inside `_enqueue_analyze` so every caller gets it
  uniformly, and consolidates what was a duplicate
  `is_scheduler_disabled()` call in `_stop_session` + `_enqueue_analyze`
  into a single call path.

### Fixed — miscellaneous correctness and polish

- **Widget poll-callback race.** The pass-1 fix added a guard at
  the top of `refreshStatus` to skip polling during `stopping`/
  `analyzing` states, but only prevented NEW polls from firing.
  An in-flight poll whose `frappe.call` was already dispatched
  before the user clicked Stop would have its callback arrive
  late and overwrite the `stopping` display back to `recording`,
  clobbering the transition. v0.5.1 repeats the transient-state
  check INSIDE the status callback: late observations early-
  return without touching state.

- **`v5_aggregate_json` tail-preferring caps.** On a 200-recording
  session with rich frontend data, the v0.5.0 aggregate JSON
  could balloon to 1 MB+, slowing Optimus Session form loads
  for every viewer. v0.5.1 adds tail-preferring caps in
  `_persist`: `infra_timeline` at 200, `frontend_xhr_matched` at
  500, `frontend_orphans` at 100. Truncation surfaces a warning
  via `analyzer_warnings` so operators can see the drop.

- **`optimus_frontend.js` watchdog is a no-op when inactive.**
  Previously the 60-second watchdog interval checked
  `xhrBuffer.length > 200` every tick regardless of session
  state. v0.5.1 adds an early `if (!currentSessionUuid()) return;`
  so the inactive path is a single DOM attribute read (~1 µs)
  per tick. Still O(n) when a session IS active and the buffer
  is over threshold, but that's the correct behavior.

- **`response_size_bytes` uses TextEncoder for accurate byte
  count.** The XHR fallback path was using
  `xhr.responseText.length` which is a UTF-16 code-unit count
  — undercounts multi-byte characters (emoji, non-ASCII).
  v0.5.1 uses `new TextEncoder().encode(str).length` with a Blob
  fallback and a char-count fallback for legacy browsers.

- **Missing wiring test for `analyze.run`.** v0.5.0 integration
  lacked a regression guard that someone removing
  `infra_pressure` from `_BUILTIN_ANALYZERS` or dropping the
  `context.frontend_data` load would be caught. Added
  `test_analyze_run_v5_wiring.py` with 5 source-inspection
  guards covering imports, analyzer list membership, context
  loading, per-recording infra attachment, and `_persist`
  aggregate serialization.

### Changed

- `__version__` bumped from `0.5.0` to `0.5.1`. Cache-buster
  rotates; browsers re-fetch `floating_widget.js` and
  `optimus_frontend.js` on the next Desk load.
- Widget now exposes a `WIDGET_BUILD_ID` constant, logged to the
  browser console at script load and set as `title` +
  `data-build-id` attributes on the widget element so users can
  confirm which JS is running from devtools.
- README.md rewritten top-to-bottom. Previously stuck at v0.1.0
  status with outdated runtime flag docs (`capture_stack`,
  `explain` — neither exists; real flags are `capture_python_tree`
  and `notes`). The new README covers all 18 finding types, the
  full configuration surface, scheduler-disabled operation, a
  troubleshooting section with every v0.5.1-era failure mode,
  and an honest comparison matrix against frappe.recorder / New
  Relic / Scout / Bullet.
- Custom `_is_pure_helper_frame` helper in `call_tree.py` for
  Repeated Hot Frame aggregation. Narrower than the pre-existing
  `_is_framework_frame`. Both helpers are live — the broad
  filter is used for SQL-to-Python reconciliation and Slow Hot
  Path findings (where it's correct), the narrow filter is used
  for hot-frame aggregation (where the broad filter was too
  aggressive).

### Migration notes

No DocType schema changes. No patches. Running
`bench --site <site> migrate` is a no-op for v0.5.1 specifically,
but `bench restart` is REQUIRED so the Python workers reload
`hooks.py` with the new `__version__` cache-buster — otherwise
browsers continue serving cached JS and none of the widget /
frontend_frontend fixes take effect.

**Browser-side**: all active Desk users must hard-refresh
(Cmd+Shift+R / Ctrl+Shift+R) after `bench restart` to discard
cached `floating_widget.js` and `optimus_frontend.js`.

**Verification**: after restart + refresh, open devtools →
Console and look for
`[optimus] floating_widget.js LOADED build=2026-04-15-stop-fix-v3`.
If the build ID is different or the log line is missing, the
browser is still serving cached JS and more cache invalidation
is needed.

### Known limitations (unchanged from v0.5.0)

- `navigator.sendBeacon` delivery depends on Frappe v16's CSRF
  middleware accepting cookie-authenticated POSTs without a
  custom `X-Frappe-CSRF-Token` header. The SameSite cookie
  strategy is expected to work, but only the `beforeunload` path
  is affected — the stop-time `frappe.call` flush (the primary
  delivery mechanism) is unchanged.
- Inline analyze pollutes `RECORDER_REQUEST_HASH` with an orphan
  recording containing analyze's own query activity. Operational
  noise only; the orphan self-cleans via 10-minute Redis TTL.
  Flagged as a v0.6 cleanup.
- The cache-buster pattern (`?v={__version__}`) requires manual
  version bumps between dev iterations. v0.6 will switch to a
  content-hash or file-mtime scheme so every JS edit
  auto-invalidates the browser cache.

---

## [0.5.0] — 2026-04-14

The "Is it my code or my server?" release. Closes two competitive gaps
with other profilers: there's no way to tell *code-slow* from
*server-slow*, and there's no way to tell *backend-slow* from
*network-slow* or *page-paint-slow*. v0.5.0 captures the server-side
resource state at every action boundary and the browser-side transport
timing for every XHR, joins them to the matching recording, and
renders them as two new report panels alongside the existing findings.
Also bundles a scheduler-disabled safety fix that affected v0.4.0 and
earlier.

### Added

- **Server infrastructure capture** — new `infra_capture.py` module
  snapshots CPU, worker RSS, system memory, swap, load average, MariaDB
  thread counts and slow-query counter, Redis ops/sec, and RQ queue
  depths at the start and end of every recorded action. Balanced tier
  (14 metrics, ~0.8ms per snapshot). Runs in-line on the request path
  — no background sampler thread. Every source is wrapped in its own
  try/except so a broken source degrades to `None` rather than
  breaking recording.
- **`infra_pressure` analyzer** — emits four new finding types:
  - **Resource Contention** — sustained system CPU > 85% across ≥2
    actions. Severity escalates to High if any sample hits 95% or if
    >50% of actions are affected. Distinguishes "your own flow is
    CPU-bound" from "something else on the box is hogging CPU."
  - **Memory Pressure** — worker RSS grew by > 200MB during the
    session OR swap > 100MB during any action. High severity if
    delta > 500MB or swap is active.
  - **DB Pool Saturation** — `threads_running / threads_connected`
    > 0.9 across ≥2 actions. Points at gunicorn worker count vs.
    MariaDB `max_connections` mismatch.
  - **Background Queue Backlog** — any RQ queue (`default`, `short`,
    `long`) peaked above 50 during the session. Signals that the
    flow enqueued work that's waiting behind other jobs.
- **Browser-side metrics shim** — new `optimus_frontend.js` wraps
  `window.fetch` and `XMLHttpRequest.prototype.open/send` to capture
  per-XHR timings (URL, method, duration, status, response size)
  whenever the server returns an `X-Optimus-Recording-Id` response
  header. Uses `PerformanceObserver` with `buffered: true` to capture
  Web Vitals (FCP, LCP, CLS, navigation timing). Wraps WHATWG
  primitives instead of application-level APIs so instrumentation
  survives future Frappe upgrades — jQuery `$.ajax` is caught via XHR
  automatically. This is the approach every production APM library
  uses (OpenTelemetry JS, Sentry Browser, Datadog RUM).
- **`X-Optimus-Recording-Id` correlation header** — `after_request`
  injects the recording UUID as a custom response header AND appends
  it to `Access-Control-Expose-Headers` so browsers actually surface
  it to JavaScript. The expose header is load-bearing: without it,
  `xhr.getResponseHeader("X-Optimus-Recording-Id")` returns `null`
  even for same-origin requests.
- **`optimus.api.submit_frontend_metrics` endpoint** —
  receives batched XHR + Web Vitals payloads from the browser shim
  at stop time (via `frappe.call`) or at `beforeunload` (via
  `navigator.sendBeacon`). Accepts a JSON string payload because
  sendBeacon sends raw `Blob`, not form-encoded. Validates session
  ownership so a cross-user write is rejected. Soft caps (1000 XHRs,
  200 vitals) with tail-preferring truncation so end-of-flow data
  wins on overflow. Idempotent — multiple submits merge into one
  Redis blob.
- **`frontend_timings` analyzer** — joins XHR timings to Profiler
  Actions by recording UUID, dedupes multi-fire LCP per page (last
  value before next navigation, matching the Web Vitals library
  convention), and emits three finding types:
  - **Slow Frontend Render** — LCP > 2500ms → Medium, > 4000ms → High.
  - **Network Overhead** — `xhr_duration - backend_duration > 500ms`
    AND `> backend * 1.5`. The multiplier is the key insight: a 500ms
    delta is disproportionate on a 1ms backend call but proportional
    on a 5s one. Only the disproportionate case flags.
  - **Heavy Response** — single response > 500KB (Low, informational).
- **Server Resource panel in the report template** — renders the
  `infra_timeline` + `infra_summary` aggregates from `infra_pressure`
  as stat cards (CPU avg/peak, RSS delta, load peak, swap peak) and
  a per-action timeline table (CPU, RSS, load, DB pool ratio, RQ
  queue depths).
- **Frontend panel in the report template** — renders the
  `frontend_xhr_matched`, `frontend_vitals_by_page`, `frontend_orphans`,
  and `frontend_summary` aggregates from `frontend_timings`. Per-action
  XHR table with backend/browser/network-delta/status/size columns,
  Web Vitals table by page (FCP, LCP, CLS, TTFB, DCL), and a
  collapsed orphans section for diagnostic use (hidden entirely in
  Safe mode).
- **`_safe_url` helper in `renderer.py`** — strips docname segments
  from `/app/<doctype>/<name>/...` paths and redacts PII query string
  keys (`source_name`, `filters`, `name`, `doctype`, `reference_name`,
  `parent`, `customer`, `supplier`) to `?`. Method URLs
  (`/api/method/frappe.client.save`) pass through — method names are
  code identifiers, not PII. Applied to every URL rendered in the
  Frontend panel when `mode == "safe"`. Mirrors SQL normalization:
  full text stored, redacted form emitted.
- **Seven new `Optimus Finding.finding_type` Select options**:
  Resource Contention, Memory Pressure, DB Pool Saturation,
  Background Queue Backlog, Slow Frontend Render, Network Overhead,
  Heavy Response.
- **Upgraded `notes` field on Optimus Session** from plain `Text` to
  `Text Editor` (rich HTML), relabeled as **"Steps to Reproduce /
  Notes"**. Rendered at the top of the report above findings so any
  reviewer reads the reproduction context before the technical
  detail. Also added to the floating widget's Start dialog as an
  optional Text Editor field so users can document "what I'm about
  to do" at the moment they start. The existing `notes` field already
  covered this use case (its description literally said "reproduction
  steps"); v0.5.0 upgrades it in place rather than adding a duplicate
  `steps_to_reproduce` field, avoiding DB schema bloat and data
  migration.
- **`v5_aggregate_json` field on Optimus Session** — hidden Long
  Text field that serializes the v0.5.0 `infra_pressure` and
  `frontend_timings` aggregates as a single JSON dict. Persisted by
  `_persist` alongside the existing `top_queries_json` and
  `table_breakdown_json`, read by `renderer.render()`.
- **`data-session-uuid` attribute on the floating widget DOM element**
  — set when a session is active, cleared when it ends. Read by
  `optimus_frontend.js` to tag its flush payloads, keeping the two
  modules loosely coupled without a shared global.
- **Test coverage:** 65+ new tests across:
  - `test_scheduler_inline_fallback.py` — 5 tests
  - `test_infra_capture.py` — 6 tests (snapshot, diff, force_stop,
    psutil defensive behavior, getloadavg fallback, idempotency)
  - `test_correlation_header.py` — 7 tests
  - `test_submit_frontend_metrics.py` — 7 tests
  - `test_infra_pressure_analyzer.py` — 10 tests
  - `test_frontend_timings_analyzer.py` — 11 tests
  - `test_safe_url.py` — 9 tests
  - `test_steps_to_reproduce.py` — 5 tests
  - `test_v5_panels_render.py` — 5 end-to-end panel render tests
  - `test_end_to_end_metrics.py` — 2 full-chain integration tests
  - Two new fixture files (`infra_pressure_session.json`,
    `frontend_metrics_session.json`)
  - Full suite: **277 tests passing**, zero regressions against v0.4.0.

### Changed

- **Scheduler-aware `_enqueue_analyze` fallback (also fixes a latent
  v0.4.x bug).** When `bench disable-scheduler` is in effect —
  common on dev, demo, and Frappe Cloud trial instances — no
  `bench worker` process consumes the RQ queue on many deployments,
  so an enqueued analyze job would sit forever and the session would
  hang in the **"Stopping"** state. v0.5.0 detects
  `is_scheduler_disabled()` and passes `now=True` to `frappe.enqueue`
  so analyze runs synchronously inside the stop request. A new
  `optimus_inline_analyze_limit` site config (default 50) hard-caps
  the recording count for inline analyze — sessions above the cap
  are marked Failed with an actionable error directing the user to
  `bench enable-scheduler` and the **Retry Analyze** button. Prevents
  gunicorn's 120s worker timeout from killing a 200-recording inline
  analyze mid-flight.
- **`api.stop()` response now includes `ran_inline: bool`** — the
  floating widget reads this to decide whether to transition through
  the "Analyzing…" state or jump straight to "Ready" (when analyze
  already completed inline, the report is attached by the time stop
  returns).
- **`api.start()` accepts an optional `notes` kwarg** (default `""`)
  and persists it into the new Optimus Session row. Backward
  compatible with callers that don't pass notes.
- **`floating_widget.js:confirmAndStop`** now calls
  `window.optimus_frontend.flush()` before firing the stop
  API so buffered browser metrics land in Redis before analyze runs.
  Best-effort — a failed flush never blocks stop.
- **`_stop_session` signature changed** from `(user, session_uuid) -> str | None`
  to `(user, session_uuid) -> tuple[str | None, bool]`. Callers
  that discarded the return value still work; the only other
  internal caller (`start()`'s idempotent restart path) also
  discards it.
- **`before_request` / `before_job` / `after_request` / `after_job`
  hooks** now take an infra snapshot into
  `frappe.local.optimus_infra_start` at the start of the action and
  diff it against an end snapshot in the `finally` block, writing
  the result under `profiler:infra:<recording_uuid>` with the same
  TTL as other session keys. All work happens inside the existing
  try/except blocks — a broken snapshot logs and falls through but
  never breaks the customer's request.
- **`capture._force_stop_inflight_capture`** is now accompanied by
  `infra_capture._force_stop_inflight` in both `api.start()` and
  `api._stop_session()` so leaked state from a previous session on
  the same worker can't poison the next one.
- **`session.delete_session_state`** now also removes
  `profiler:frontend:<session_uuid>`. Per-recording
  `profiler:infra:<recording_uuid>` keys are cleaned up alongside
  `RECORDER_REQUEST_HASH` entries when analyze walks the recording
  list.
- **`hooks.py:app_include_js`** converted from a string to a list
  and now includes `optimus_frontend.js` alongside `floating_widget.js`.
  Both entries carry the version cache-buster.
- **`analyze.run`** now loads `profiler:frontend:<session_uuid>`
  into `context.frontend_data` and attaches per-recording infra
  dicts as `rec["infra"]` before the analyzer loop runs, so
  `infra_pressure` and `frontend_timings` can read them inline
  without a Redis hop inside each analyzer. Also appends the two
  new analyzers to `_BUILTIN_ANALYZERS`. Order is irrelevant — both
  are independent of every existing analyzer.

### Fixed

- **Widget stop-button race condition** (backported to v0.4.0
  `handoff-ux` branch as `e620a57`). `confirmAndStop()` set the DOM
  display to "Stopping…" but left `currentState.display` as
  `"recording"`, so the 5-second polling guard in `refreshStatus()`
  — which checks `currentState.display` — never tripped. If polling
  raced the stop API and the status call returned `active=true`
  (because the server hadn't processed stop yet), the widget would
  flip back to "Recording" mid-stop. Also added an `error` callback
  on the stop `frappe.call` so a failed stop reverts to "Recording"
  with a red toast instead of stranding the user on "Stopping…"
  forever.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply `optimus.patches.v0_5_0.add_metrics_finding_types`
   which reloads the Optimus Finding DocType so the seven new
   `finding_type` Select options become available. Idempotent.
2. Apply `optimus.patches.v0_5_0.upgrade_notes_to_text_editor`
   which reloads the Optimus Session DocType to pick up the
   upgraded `notes` field (now Text Editor) and the new
   `v5_aggregate_json` Long Text field. Existing `notes` values
   carry over unchanged because plain-text content is valid Text
   Editor input — no data migration needed, only the metadata
   changes.

No breaking API changes:

- `api.start` accepts a new optional `notes` kwarg with a
  backward-compatible default (`""`).
- `api.stop` adds a new `ran_inline` key to its return dict;
  existing consumers that ignore unknown keys work unchanged.
- `_stop_session` signature changed internally but is not part of
  the public API surface.

Existing v0.4.0 sessions render unchanged in v0.5.0 because
`v5_aggregate_json` is NULL for pre-v0.5.0 rows and the renderer
skips the Server Resource and Frontend panels when the aggregates
are empty.

**Known v0.5.0 operational notes:**

- On sites where the scheduler is disabled, the stop API will block
  for the full analyze duration (typically 2–20s). Widget transitions
  through "Stopping…" → "Ready" directly (no intermediate "Analyzing…"
  state) because the report is already attached when stop returns.
- If `navigator.sendBeacon` is rejected by Frappe v16's CSRF
  middleware (unverified), the `beforeunload` flush path will fail
  silently and the user loses their frontend metrics for that
  session. The server-side recording is unaffected. Mitigation: the
  stop-time flush path uses `frappe.call` (standard cookie auth)
  and is the primary delivery path. The beacon is a best-effort
  hedge for tab-close scenarios.
- `navigator.sendBeacon` calls made from *other* apps are not
  captured (beacons can't return response headers, so our shim
  can't see the `X-Optimus-Recording-Id`).

To disable the new pyinstrument + infra capture for a specific
session, uncheck **"Capture Python call tree"** in the start
dialog as before — the v0.3.0 flag continues to gate the heaviest
capture paths. Infra capture is unconditional because it costs
~0.8ms per action and runs only while the user's session is active.

---

## [0.4.0] — 2026-04-14

The "Make it usable" release. Sands down the rough edges between
"customer installs the app" and "customer hands a useful report to
their software company". The product thesis is unchanged; the
handoff workflow is faster and the report is more actionable.

### Added

- **Session comparison / baseline pinning** — pin any Ready session as
  the baseline for its label. Subsequent recordings with the same label
  auto-render three comparison sections in the safe + raw reports:
  session-level delta, per-action diff, and finding-level diff
  (fixed / new / unchanged buckets). Lets the dev shop prove "the fix
  worked" by recording a before/after.
- **`Pin as baseline` and `Compare with...` buttons** on the Profiler
  Session form view. Pinning is per-session-label and persists in
  Redis under `profiler:baseline:<label>`.
- **Auto-inheritance of baseline** at recording start — `api.start`
  checks the baseline cache for the label and pre-populates
  `compared_to_session` on the new session.
- **`comparison.py` module** — pure-function action and finding
  matchers, fixture-testable, no Frappe DB access.
- **PDF export of the safe report** — lazy-generated on first
  download click via `frappe.utils.pdf.get_pdf` (wkhtmltopdf), cached
  to a private File attachment on the Optimus Session. Subsequent
  downloads serve from cache. Generation cost is kept out of the
  analyze pipeline.
- **`pdf_export.py` module** — `get_or_generate_pdf` and
  `clear_cached_pdf` helpers.
- **PDF download button** on the Optimus Session form (lazy generation
  with progress alert).
- **Auto-assign `Optimus User` role to System Managers** on install
  via `after_install`. Also wires a `User.validate` doc_event so new
  System Managers automatically get the Optimus User role.
- **One-time onboarding toast** on first Desk visit after install,
  pointing the user at the floating Profiler pill. Suppressed for
  experienced users (anyone with a Ready Optimus Session row).
  Tracked via `profiler:onboarding_seen:<user>` in Redis.
- **Version-driven asset cache buster** — `app_include_js` and
  `app_include_css` now read `?v={__version__}` so every release
  automatically invalidates browser caches.
- **6 new whitelisted API endpoints** —
  `check_onboarding_seen`, `mark_onboarding_seen`, `pin_baseline`,
  `unpin_baseline`, `set_comparison`, `download_pdf`.
- **3 new fields on Optimus Session** — `compared_to_session`
  (Link), `is_baseline` (Check), `safe_report_pdf_file` (Attach).
- **SVG donut fallback for PDF mode** — wkhtmltopdf doesn't handle
  `conic-gradient` reliably; the renderer now produces an inline SVG
  pie chart that's hidden in HTML mode (via `@media print` CSS) and
  shown in PDF rendering.
- **Janitor cascade** — `sweep_old_sessions` clears the baseline
  cache key before deleting a baseline session and cascades the
  v0.4.0 `safe_report_pdf_file` attachment.
- **`retry_analyze` clears the cached PDF** so the next download
  regenerates from the freshly-analyzed report.
- **Self-contained safe report regression gate** — new test
  `test_safe_report_self_contained.py` asserts the rendered HTML
  contains no external URL fetches. Catches accidental introductions
  of CDN references at CI time.

### Changed

- **No changes to the v0.3.0 capture or analyze pipelines.**
  `capture.py`, `hooks_callbacks.py`, `analyze.py`, and the analyzer
  modules are frozen for this release.
- **`api.start(label, ...)` accepts the same kwargs as v0.3.0** —
  `capture_python_tree` is unchanged. The new auto-inheritance of
  `compared_to_session` is transparent to callers.
- **Renderer adds a comparison computation block** when
  `compared_to_session` is set on the session being rendered.
  Backward-compat: sessions with the field unset render exactly as
  in v0.3.0.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply patch `optimus.patches.v0_4_0.add_comparison_and_pdf_fields`
   which reloads the Optimus Session DocType.
2. Add 3 new fields to `tabOptimus Session`: `compared_to_session`,
   `is_baseline`, `safe_report_pdf_file`. All nullable / default 0.

No breaking API changes. v0.3.0 sessions render unchanged in v0.4.0
because all new fields default to NULL/0 and the renderer skips the
comparison and PDF paths when fields are unset.

To pin a session as a baseline, open it in the Optimus Session form
view (Status: Ready) and click **Pin as baseline**. Subsequent
recordings of the same flow (matching session title) will auto-include
comparison sections in their safe + raw reports.

---

## [0.3.0] — 2026-04-13

Adds a Python call tree capture and analysis layer on top of the existing
SQL-only profiler. Customers reading a safe report now see where their
flow spent time across SQL **and** Python, with hot-path detection,
hook bottleneck findings, and redundant-call detection. See
`apps/frappe_profiler_design/specs/2026-04-13-call-tree-and-redundant-calls-design.md`
for the full design spec.

### Added

- **pyinstrument-based Python call tree capture** per recording. Sampled
  at 1ms intervals (configurable via
  `site_config.json: optimus_sampler_interval_ms`). Scoped per request
  so non-recording users are unaffected.
- **Reconciled unified call tree** — each captured SQL call is grafted
  onto the deepest user-code frame in the pyinstrument tree, so the
  customer sees "your `discounts.calculate` function spent 320ms — 280ms
  of that in 14 SQL queries (children below)".
- **Four new finding types:**
  - `Slow Hot Path` — a Python subtree consumes >25% of an action AND >200ms.
  - `Hook Bottleneck` — same shape, but the subtree is a doc-event hook
    (called via `Document.run_method`); the finding names the hook function.
  - `Repeated Hot Frame` — the same frame appears in ≥3 actions and
    consumes ≥500ms total across the session.
  - `Redundant Call` — the same `frappe.get_doc(doctype, name)` /
    `frappe.cache.get_value(key)` / `frappe.permissions.has_permission(...)`
    fired N times from the same callsite (thresholds: 5/10/10 by default,
    all configurable).
- **Session-wide time-attribution donut** in the safe report — at-a-glance
  "this session was 38% SQL, 22% erpnext, 18% your custom code, …".
- **Hot frames leaderboard** in the safe report — top 20 hottest function
  paths across the whole session, sortable.
- **`api.start(label, capture_python_tree=True)`** — new kwarg lets
  customers opt out per session (falls back to v0.2.0 SQL-only capture).
  Surfaced in the floating widget's start dialog as a checkbox.
- **Auto-promote of large per-action call trees** to private File
  attachments when the inline JSON exceeds 200KB. Hard-truncation
  fallback if the file write fails. 16MB hard guard against pathological
  trees.
- **Sidecar wraps** for redundant-call detection on `frappe.get_doc`,
  `RedisWrapper.get_value` (the underlying class behind `frappe.cache`),
  and `frappe.permissions.has_permission`. Idempotent install at app
  load; restored on uninstall.
- **PII safety on sidecar arguments:** values that may contain user data
  (doc names, cache keys) are sha256-hashed (`identifier_safe`) for
  safe-mode display. Raw values stored only in raw-mode-visible
  technical details. Doctype names and ptypes are NOT hashed (schema,
  not data).
- **`pyinstrument >= 4.6, < 6` dependency** added to `pyproject.toml`.
  Pure-Python, MIT, no compiled extensions.
- **Streaming `_fetch_recordings`** — converted from list-returning to
  generator so the analyze pipeline holds bounded memory across large
  sessions.
- **Per-analyzer wall-clock budget tracker** — analyzers exceeding 60s
  are flagged; total analyze budgeted at 20 min (5-min headroom under
  RQ long-queue timeout). Past the cap, remaining analyzers are skipped
  with a partial-completion warning.
- **`api.export_session()` v0.3.0 fields** — JSON output now includes
  `call_tree`, `hot_frames`, `session_time_breakdown`, `total_python_ms`,
  `total_sql_ms`.
- **New site config keys:**
  - `optimus_sampler_interval_ms` — pyinstrument sample interval (default 1).
  - `optimus_tree_prune_threshold_pct` — drop frames below N% of action time (default 0.005).
  - `optimus_tree_node_cap` — max nodes per persisted tree (default 500, hot path always preserved).
  - `optimus_redundant_doc_threshold` (default 5).
  - `optimus_redundant_cache_threshold` (default 10).
  - `optimus_redundant_perm_threshold` (default 10).
  - `optimus_redundant_high_multiplier` (default 5).
  - `optimus_safe_extra_allowed_apps` — extra app prefixes whose function names are kept un-redacted in safe mode.

### Changed

- **Per-flow recording overhead** climbs from "10–30% per query" to
  roughly "1.5–2× wall clock during recording" when `capture_python_tree=True`.
  Non-recording users on the same site are still unaffected — the
  activation gate is per-user, and the wraps' hot-path check is a single
  attribute lookup with **<100ns overhead** measured against an unwrapped
  baseline.
- **`health()` `last_24h.analyze_avg_ms`** will rise after this ships.
  Customers monitoring it will see a step change at upgrade time.
- **Renderer adds donut + hot frames sections** to both safe and raw
  reports. Old v0.2.0 sessions render with the old layout (no v0.3.0
  fields → sections skipped).
- **R2 redaction policy** — function names in safe-mode reports collapse
  custom-app frames to `<app>:<top-level-module>` (e.g.
  `my_acme_app.discounts.pricing.calc_secret` → `my_acme_app:discounts`).
  Frappe / ERPNext / payments / hrms keep full names.

### Fixed (caught during v0.3.0 development)

- **`pyproject.toml` empty `authors = [{ name = "", email = ""}]`**
  broke `flit_core` on Python 3.14 with `email.errors.HeaderParseError`.
  Removed the empty entry.
- **`__init__.py` `frappe.log_error` fallback** in the
  `capture.install_wraps` except handler crashed when test code stubs
  `frappe` with a minimal fake module that lacks `log_error`. Now
  bulletproofed with a nested try/except.
- **Best-effort sidecar entry build** — a failure inside `_identify_args`
  (e.g. an arg with a broken `__str__`) used to propagate out and break
  the user's `frappe.get_doc` call. Now caught locally; the wrap skips
  the entry but always calls `orig`.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply patch `optimus.patches.v0_3_0.add_call_tree_fields`,
   which reloads `Optimus Action` and `Optimus Session` to pick up
   the new nullable columns.
2. Add 3 new fields to `tabOptimus Action`: `call_tree_json`,
   `call_tree_size_bytes`, `call_tree_overflow_file`.
3. Add 4 new fields to `tabOptimus Session`: `total_python_ms`,
   `total_sql_ms`, `hot_frames_json`, `session_time_breakdown_json`.

No breaking API changes — `start`, `stop`, `status`, `get_active_session`,
`health`, `export_session`, `retry_analyze` all keep their existing
signatures (`start` accepts a new optional kwarg with backward-compatible
default).

Existing v0.2.0 sessions continue to render with the old layout
(NULL v0.3.0 fields → donut/hot frames sections skipped via the
backward-compat fallbacks).

To opt out of the new pyinstrument capture per-session, uncheck
**"Capture Python call tree"** in the start dialog or pass
`capture_python_tree=False` to `api.start`.

---

## [0.2.0] — 2026-04-09

Round 2 improvements. 28 items across correctness, operations, UX,
extensibility, and housekeeping. See
`apps/frappe_profiler_design/ARCHITECTURE.md` for the full design rationale.

### Added

- **JSON export endpoint** — `optimus.api.export_session(uuid)`
  returns a structured blob (session + actions + findings + top queries +
  table breakdown) for programmatic consumption by dev-shop tools.
- **Health / metrics endpoint** — `optimus.api.health()` returns
  counts by status and analyze-pipeline performance over the last 24 hours.
  Intended for Prometheus/Grafana/Datadog scrapers.
- **Custom analyzer hook** — third-party Frappe apps can contribute
  analyzers via `hooks.py: optimus_analyzers = ["my_app.analyzers.custom.analyze"]`.
  Hooks run after the builtins and share the same `AnalyzeContext`.
- **Cross-session EXPLAIN cache** — EXPLAIN results are now cached in
  Redis with a 1-hour TTL (configurable via
  `site_config.json: optimus_explain_cache_ttl_seconds`). Two consecutive
  analyze runs on a stable schema skip the DB roundtrip entirely.
- **Notes field on Optimus Session** — customers can annotate sessions
  with reproduction steps, ticket refs, context. Editable even on Ready
  sessions. Rendered in the HTML report header.
- **Progress updates during analyze** — the analyze pipeline emits
  `frappe.publish_realtime("optimus_progress", ...)` events at each
  phase (5% fetching, 20% EXPLAIN, 50% analyzers, 80% persist, 90%
  render, 100% done). The floating widget subscribes and displays a live
  percentage instead of a bare "Analyzing…".
- **Retention-policy cleanup** — daily janitor deletes Ready/Failed
  sessions older than 90 days (configurable via
  `site_config.json: optimus_session_retention_days`).
- **Orphan Redis cleanup** — the daily janitor also sweeps
  `profiler:session:*` Redis keys whose parent Optimus Session row no
  longer exists (e.g. failed analyzes that never retried).
- **Sensitive-field redactor** — raw report now redacts known-sensitive
  fields from headers and form_dict before rendering. Redacts: password,
  secret, token, api_key, authorization, cookie, csrf, otp, card_number,
  cvv, ssn, aadhar, pan_number, and similar. Defense-in-depth against
  download-and-share leaks.
- **Session TTL refresh on activity** — long flows (45+ minutes) no
  longer silently stop at the 10-minute TTL. Every
  `register_recording` call refreshes the user's active-session key so
  an actively-used session stays alive as long as there's traffic.
- **Server timezone in report header** — the report now labels times
  with an explicit server timezone so distributed teams don't get
  confused about UTC vs. local.
- **Retry Analyze button** — Failed sessions now have a "Retry Analyze"
  custom button in the form view that re-enqueues the analyze job. New
  `optimus.api.retry_analyze(session_uuid)` whitelisted endpoint.
- **Fixture builder helpers** — `optimus.tests.fixture_builders`
  provides `build_call`, `build_recording`, `build_explain_row` to
  reduce boilerplate in analyzer tests.

### Fixed

- **N+1 attribution blamed frappe framework code** — `_callsite()` now
  walks the stack skipping `frappe/` and `optimus/` prefixes so
  N+1 findings point at customer business logic (e.g.
  `erpnext/accounts/sales_invoice.py:212`) instead of framework helpers
  (`frappe/database/database.py:742`). Single most impactful fix in
  the round-1 review.
- **`explain_flags` documented a `filtered < 10` check that wasn't
  implemented** — the new check fires on queries where MariaDB's
  `filtered` column < 10 AND rows_examined > 100, emitting a new
  `Low Filter Ratio` finding type.
- **`before_request`/`before_job` could clobber an existing recorder** —
  if the standalone Recorder UI is active globally, frappe's own hook
  creates a Recorder first; our hook now checks
  `frappe.local._recorder` and piggybacks instead of overwriting it.
- **`api.start()` had no role check** — any authenticated user could
  POST to the endpoint and start a session on themselves. Now requires
  `Optimus User` or `System Manager` role (enforced at the HTTP level,
  not just the UI).
- **N+1 threshold of 5 was too low** — raised default to 10 with a
  `optimus_n_plus_one_threshold` site config override. Also requires
  minimum total time (default 20ms) so 10×0.1ms queries no longer
  trigger false positives.
- **`_enrich_recordings` had no EXPLAIN cap** — now caps at 2000
  queries per recording and dedupes EXPLAIN by query shape. Prevents
  the analyze job from running millions of EXPLAINs on pathological
  sessions.
- **DB indexes missing on `status` and `started_at`** — the janitor
  query was a table scan at scale. Added `search_index: 1` on both.
- **`index_suggestions` silently swallowed errors** — now logs the
  first 3 per-query failures and surfaces a `"Could not analyze X queries"`
  warning in the report.
- **Multi-line SQL rendered as single line in top-N table** — switched
  from `<code>` to `<pre class="sql-inline">` with bounded height.
- **`before_job` left `_profiler_session_id` in kwargs on malformed
  kwargs** — defensive type check + error log.
- **Widget polled forever in hidden tabs** — now pauses polling on
  `visibilitychange` and resumes when the tab becomes visible again.
- **Cap warning not surfaced in UI** — `analyzer_warnings` now renders
  as an orange `frm.set_intro` banner at the top of the form.
- **"Top contributor" summary missed session-wide findings** — the
  two-step fallback now picks the highest-impact finding overall when
  there's no action-specific match.
- **Session list view had no severity indicator** — new `top_severity`
  field populated by analyze, color-coded in the list view via a custom
  `listview_settings.get_indicator`.
- **`track_changes=1` on Optimus Session caused storage bloat** —
  every analyze created 10+ tabVersion rows. Disabled track_changes;
  patch `v0_2_0.remove_version_tracking` cleans up existing rows on
  `bench migrate`.
- **Potential recursive analyze** — `analyze.run()` now sets
  `frappe.local.optimus_analyzing = True` so hooks skip activation on
  the analyze pipeline's own DocType writes.
- **`_optimize_query` errors could leak query literals in the error
  log** — added a paranoia scrub (`'foo'` → `'?'`, long numbers → `?`)
  before logging.
- **Uninstall didn't clean Redis state** — `before_uninstall` now
  SCAN+DELETEs all `profiler:*` keys for the site.

### Changed

- **Analyzer unit tests** — 50+ new tests covering per_action, top_queries,
  n_plus_one (with callsite attribution assertions), explain_flags (all
  4 red flags including the new filtered check), index_suggestions (with
  `monkeypatch` for `_optimize_query`), table_breakdown, the enqueue
  patch (idempotency + session id injection), and frontend asset smoke
  tests (JS syntax + content assertions). All 67 tests pass in < 1s.
- **Shared `SEVERITY_ORDER` and `walk_callsite`** — moved from
  per-module copies to `analyzers/base.py`.
- **Refactored `_stop_session`** — split into `_clear_active`,
  `_mark_stopping`, `_enqueue_analyze` for clarity.
- **README overhauled** — operational caveats, hard-cap table, config
  knobs, verification checklist.
- **Version bumped** from 0.0.1 to 0.2.0.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply the `optimus_session.status` and `optimus_session.started_at`
   database indexes.
2. Run `patches.v0_2_0.remove_version_tracking` to delete existing
   `tabVersion` rows for Optimus Session (freeing storage; no data loss
   because these versions weren't useful anyway).
3. Add the new `notes`, `top_severity`, `analyze_duration_ms` fields to
   `tabOptimus Session`.
4. Add the new `Low Filter Ratio` value to the `Optimus Finding.finding_type`
   select.

No breaking API changes — existing calls to `start`, `stop`, `status`,
`get_active_session`, `retry_analyze` are unchanged.

---

## [0.1.0] — 2026-04-08

Initial feature-complete v1. All 8 phases from the design doc plus 21
fixes from the first-review pass. See `ARCHITECTURE.md` for the design
rationale.

### Added

- Scaffold (Phase 0): installable Frappe app with three DocTypes
  (`Optimus Session`, `Optimus Action`, `Optimus Finding`).
- Session lifecycle (Phase 1): whitelisted `start`/`stop`/`status`/
  `get_active_session` API, Redis-backed per-user session tracking,
  before/after request hooks that activate the recorder only for users
  with an active session.
- Background job inheritance (Phase 2): `frappe.enqueue` monkey-patch
  injects `_profiler_session_id` into job kwargs; before/after job
  hooks pop the marker and activate recording.
- Six analyzers (Phase 3): per-action breakdown, top-N slowest queries,
  true N+1 detection, EXPLAIN red flags (full scan, filesort,
  temporary table), aggregated index suggestions, per-table breakdown.
- HTML report renderer (Phase 4): safe and raw modes from a single
  Jinja template. Self-contained HTML with inline CSS.
- UI (Phase 5): floating start/stop widget, Optimus Session form
  customization with status indicator, download buttons, findings
  dashboard.
- Production hardening (Phase 6): 200-recording cap per session, stale
  session janitor every 5 minutes, raw report permission gate,
  comprehensive README.

---

## [0.0.1] — 2026-04-08

Initial scaffold. Empty app with no logic — just the DocType structure.
