# Changelog

All notable changes to the Optimus app.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project follows [SemVer](https://semver.org/) (pre-1.0, so minor
versions may contain breaking changes — see migration notes below).

---

## [0.12.25] — 2026-05-25

**Janitor proactive envelope-version census — per-key visibility
complement to v0.12.18's sentinel sweep.**

The v0.12.18 release shipped `_sweep_schema_drift` (sentinel-level
signal: "schema version on disk differs from the code's"). This
release adds the follow-up signal: "OK how many cached values are
actually stale?". Useful immediately after a schema bump — operators
see "5 of 12 session_meta values are still at envelope v0" and can
plan cleanup vs. let-them-age-out without waiting for per-read
`redis.schema_drift` events to accumulate one-by-one.

### Added

- **NEW `janitor._sweep_envelope_versions()`** — per-key census
  across the `session_meta` cache (the v0.12.21 rollout target).
  Scans `profiler:session:*:meta`, reads each value via
  `frappe.cache.get_value`, unwraps via `redis_schema.unwrap_value`,
  counts per envelope version (current / legacy-unversioned /
  drift-other). Emits one `janitor.envelope_version_census`
  telemetry event with the breakdown in context, severity
  `warning`.
- **Emit gates**: skips telemetry on empty bench (`total == 0`)
  AND on the all-current happy path (`legacy + drift == 0`). Daily
  clean-bench runs stay silent.
- **Wired into `sweep_old_sessions`** as the fifth try/except'd
  inner sweep (after retention, orphan-redis, schema-drift,
  old-telemetry sweeps). Wrapped in its own try/except that emits
  `janitor.sweep_envelope_versions` on its own failure (matching
  the sibling-sweep pattern).
- **NEW `optimus/tests/test_janitor_envelope_census.py`** (~150
  LOC, 11 tests across 3 classes):
  - `TestEmitDecisionMatrix` (6 tests) — the emit-or-skip decision
    distilled to a pure function (`_emit_decision`). Empty bench,
    all-current, legacy-only, drift-only, mixed, only-legacy.
  - `TestSweepSourceMatchesDecisionMatrix` (3 tests) — source-grep
    canaries: `total == 0` guard present, `(legacy + drift) == 0`
    gate present, event name is `janitor.envelope_version_census`.
  - `TestSweepWiringInDailyCron` (2 tests) — source-grep canaries
    on the wiring and the scan pattern.

### Why source-inspection tests (no in-process mocking)

The sweep depends on a live `frappe.cache.get_redis_connection` +
`frappe.cache.scan` + per-key `frappe.cache.get_value` pipeline.
Mocking that pipeline in pure-pytest is fragile (the conftest's
`SimpleNamespace` stub lacks `get_redis_connection`, and
`mock.patch.object` replacements don't survive cross-test isolation
in the full suite when other tests have left state behind). The
integration suite (`tests_integration/`) is the right place for the
end-to-end execution test against real Redis — out of scope today.

The pure-function `_emit_decision` covers the logic-level contract;
the source-grep canaries cover the wiring contract. Together they
catch any regression in either the emit gate or the cron wiring.

### Compatibility

Pure additive change. No reads of any pre-existing value shape; only
adds a new daily-cron pass. Unit suite stays at 1859 + 11 new = 1870.

---

## [0.12.24] — 2026-05-25

**`session.py` local key helpers retired in favour of
`optimus.redis_keys.session_*` aliases.**

`optimus/session.py` was the last module still defining inline
`profiler:*` f-string key helpers. The v0.12.0 release centralised
every Redis key in `optimus/redis_keys.py` and added
`test_redis_audit.py` to catch new inline f-strings; the session
helpers were exempted from the audit and stayed inline as deferred
cleanup. This release closes that gap. Same pattern as v0.12.20 for
LP capture.

### Changed

- **`optimus/session.py`** — 5 local key helpers (`_active_key`,
  `_meta_key`, `_recordings_key`, `_pending_jobs_key`, `_jobs_key`)
  replaced with module-level aliases to the v0.12.0 redis_keys
  equivalents:
  ```python
  from optimus import redis_keys as _redis_keys

  _active_key = _redis_keys.session_active
  _meta_key = _redis_keys.session_meta
  _recordings_key = _redis_keys.session_recordings
  _pending_jobs_key = _redis_keys.session_pending_jobs
  _jobs_key = _redis_keys.session_jobs
  ```

### Why aliases (not call-site rewrites)

Unlike v0.12.20's LP cleanup which migrated every call site to
`_redis_keys.lp_*(...)`, session.py has 27 internal call sites + 7
test references via `session._meta_key(uuid)` / `session._jobs_key(sid)`.
Replacing 34 call sites would be churn-heavy; aliasing preserves
the existing API surface. The end result is identical (every key
string is built via `redis_keys.session_*`); the difference is
zero call-site churn and zero test churn.

### Byte-identical keys

- `_active_key(user)` → `f"profiler:active:{user}"` ✓ matches `redis_keys.session_active`.
- `_meta_key(sid)` → `f"profiler:session:{sid}:meta"` ✓.
- `_recordings_key(sid)` → `f"profiler:session:{sid}:recordings"` ✓.
- `_pending_jobs_key(sid)` → `f"profiler:session:{sid}:pending_jobs"` ✓.
- `_jobs_key(sid)` → `f"profiler:session:{sid}:jobs"` ✓.

On-disk Redis values from pre-v0.12.24 benches resolve unchanged.

### Unchanged

- The v0.12.0 `test_redis_audit.py` drift checker — keys built via
  the alias are reached through `redis_keys.session_*`, which the
  audit's allow-list already covers.
- Behaviour identical — every cache write/read produces the same
  key, just resolved through the centralized module.

### Compatibility

Pure cleanup. No behaviour change. Unit suite stays at 1859.

---

## [0.12.23] — 2026-05-25

**Renderer extraction — NEW `source_resolution.py` submodule. Prep work
for finding_enrichment phase 3.**

The HIGH-coupling finding-enrichment subset (`_finding_to_dict`,
`_attach_representative_callsites`, etc.) depends on 6 source-
resolution helpers that have been intermixed with action-rendering
code in `_internal.py`. Lifting those 6 helpers to a sibling submodule
NOW means the next renderer-extraction PR can move the finding-
enrichment subset cleanly without dragging in a sprawling helper
family.

### Moved

Six functions from `_internal.py` → `optimus/renderer/source_resolution.py`:

- `_action_dotted_entry(action)` — derive an action's dotted entry-
  point path (RQ Job: method; HTTP `/api/method/<dotted>`: dotted).
- `_skip_decorators_to_def(abs_filename, start_lineno, fn_name)` —
  walk past `@decorator` lines to land on `def <fn_name>` (Python
  3.11+ `co_firstlineno` points at the first decorator, not the def).
- `_resolve_dotted_to_code(dotted)` — `(abs_filename, lineno,
  func_name)` from a dotted module path. Uses `importlib` (NOT
  `frappe.get_attr` — no live-site dependency).
- `_bench_relative_display(abs_path)` — `apps/<app>/...` display form
  via `frappe.utils.get_bench_path`. Falls back to absolute.
- `_action_entry_callsite(action, *, cache)` — full resolution:
  action → dotted → code → `{filename, _abs, lineno, function,
  source_snippet}`.
- `_resolve_frame_key_to_callsite(function_key, *, cache)` — same
  but starting from a repeated-hot-frame key (`short_path::func`).

Out of `optimus/renderer/_internal.py` (now ~2,919 LOC; was ~3,170),
into NEW `optimus/renderer/source_resolution.py` (~260 LOC).

### Implementation

- All 6 functions form a tight cluster (only call each other +
  stdlib + sibling renderer submodules' `_read_source_snippet` /
  `_resolve_source_path` from `source.py`).
- Lazy import of `frappe.utils.get_bench_path` inside
  `_bench_relative_display` preserved — keeps the module importable
  in non-bench contexts (pure-pytest).
- Standard back-import block at the top of `_internal.py` re-imports
  all 6 names so call sites (including ~6 in-`_internal.py` callers
  + tests via `renderer.X`) resolve unchanged.
- Structural-snapshot canary stays byte-identical without fixture
  regeneration.

### Why now

`finding_enrichment` phase 3 needs these helpers as a stable
dependency. Two paths were possible:

1. **Move them with phase 3** — would put 6 helpers + 5 finding-
   enrichment functions in the same PR, 11 moves at once.
2. **Move them first** (this PR) — clean 6-function extraction
   today; phase 3 becomes a clean 5-function extraction tomorrow.

Option 2 wins on reviewability and on testability (any regression in
option 1 could be blamed on either move; option 2's halves are
independently validated).

### Docs

- `optimus/renderer/README.md` — current-layout block bumped to 9
  submodules; new `source_resolution.py` row added with the 6-
  function inventory.

### Unchanged

- Behaviour identical — every call site resolves the same function
  through the back-import shim.
- 59 unit tests across `test_action_entry_callsite.py` (which
  exercises 5 of the 6 moved helpers via `renderer.X`) stay green
  without modification.
- `test_renderer_structure_snapshot.py` stays green (no fixture
  update).

### Compatibility

No behaviour change. Pure refactor. Unit suite stays at 1859.

---

## [0.12.22] — 2026-05-25

**Docs — `docs/HMAC-ENVELOPE.md` specifies the future-scheme design
(v2 HMAC-SHA512, v3 AES-SIV, v4+ key rotation) using v0.12.14's
1-byte version marker as the extension point.**

The v0.12.14 release shipped the 1-byte HMAC scheme marker without
specifying what future schemes look like — the marker was an extension
point but the extension semantics weren't documented. A future
operator who needs SHA-512 (FIPS 140-3 compliance) or AES-SIV
(encryption at rest in Redis) would have had to re-derive the design
from scratch. This release pre-designs those paths.

### Added

- **NEW `docs/HMAC-ENVELOPE.md`** — documents:
  - The v0 (legacy, pre-v0.12.14) + v1 (current) byte layouts.
  - The backward-compat read path's single-HMAC-step + first-byte
    disambiguation, with the rationale for `\x01` as the marker
    (pickle never emits it; producer-compat table for msgpack /
    JSON / raw bytes).
  - **Scheme v2 (HMAC-SHA512) implementation sketch** — handles
    the 32 → 64 byte signature length change via two-attempt
    verify, preserves v0/v1 compat. Includes a defence-in-depth
    guard against attacker-crafted v0 blobs whose body starts with
    a future scheme marker.
  - **Scheme v3 (AES-SIV authenticated encryption) sketch** — same
    envelope shape as v1; SIV tag replaces HMAC; deterministic-
    encryption fits the multi-read Redis pattern.
  - **Scheme v4+ (key rotation)** — marker low-bits carry a
    key-id; operator-driven extension requiring per-key-id secret
    resolution in `_hmac_secret`.
  - The existing v0.12.14 canary-test suite (~13 tests) + the
    additional `TestSchemeV2RoundTrip` / `TestV0V1V2Coexistence`
    /`TestV1ReaderRejectsV2Blob` tests a future v2 PR should add.
  - Operator notes: how to inspect a blob's scheme from
    `frappe.cache.hget` output; the four-step re-sign procedure
    (no inline migration today); when the janitor's daily cron
    could automate the re-sign post-bump.
  - **Out-of-scope deferrals**: nonce-based AEAD (AES-GCM /
    ChaCha20-Poly1305 don't fit the deterministic-retrieval
    pattern); asymmetric signatures (Ed25519 — overkill for the
    intra-bench trust model).

### Why pre-design

When the real need for scheme v2 lands, the operator doing the work
will have:
* The byte layout pre-decided (no design rounds with reviewers).
* The backward-compat semantics pre-specified (no risk of
  inadvertently breaking v0/v1 read paths).
* The canary-test additions enumerated.
* A defence-in-depth guard baked in (the attacker-crafted-v0
  edge case).

The v0.12.14 PR itself was straightforward because the v0 → v1
transition was simple. v1 → v2 involves a signature-length change;
the design needs to live somewhere readable before the implementation
PR lands.

### Unchanged

No code change. Documentation-only PR. Unit suite stays at 1859.

---

## [0.12.21] — 2026-05-25

**Redis-schema rollout continues — `session_meta` is the fifth value
migrated to the v0.12.0 `wrap_value` / `unwrap_value` envelope.**

The session metadata dict (set by `api.start`, updated through the
session's life, read on every recording's `before_request` /
`before_job` hook) is now wrapped on write and unwrapped on read.

Previous phases: `settings_cache` (v0.12.11), `retention_backlog` +
`onboarding_seen` (v0.12.13), `explain_cache` (v0.12.17).

### Changed

- **`optimus/session.py:set_session_meta`** now wraps the meta dict
  via `redis_schema.wrap_value` before `frappe.cache.set_value`.
- **`optimus/session.py:get_session_meta`** now unwraps via
  `redis_schema.unwrap_value` and returns the payload dict. Also
  adds a **defensive `isinstance(dict)` normalisation**: if the
  unwrapped value isn't a dict (corrupt write, future-version
  envelope where `unwrap_value` returned the `default=None`), the
  function returns `None` rather than passing through to callers
  which would crash on `.get(...)`.

### Why session_meta

- **Hot path** — read on every recording's request/job hook;
  envelope overhead must be invisible. Single Redis GET +
  isinstance check.
- **Stable dict shape** — fields documented in the docstring; the
  shape is the same v0.3.0+ contract, perfect for envelope.
- **Single writer / single reader pair** — both inside `session.py`,
  no cross-module coordination needed.

### Added

- **NEW `optimus/tests/test_envelope_rollout_phase4.py`** (~155
  LOC, 5 tests):
  - `test_set_session_meta_stores_envelope` — write-path canary.
  - `test_get_session_meta_unwraps_new_envelope` — happy path.
  - `test_get_session_meta_handles_legacy_bare_dict` — the
    migration-safety contract: pre-v0.12.21 bare dicts resolve
    unchanged.
  - `test_get_session_meta_returns_none_for_missing_key` — cache
    miss case.
  - `test_get_session_meta_returns_none_for_corrupt_non_dict` —
    defensive non-dict normalisation.

### Docs

- `docs/REDIS-SCHEMA.md` — `profiler:session:<uuid>:meta` row
  updated to reflect the envelope shape + the defensive
  normalisation note.

### Compatibility

- **Forward** (new readers, old writers): supported via
  `unwrap_value`'s legacy-detection branch.
- **Backward** (old readers, new writers): NOT supported — an old
  reader would call `.get("session_uuid")` on the envelope dict and
  get `None` (the envelope dict has `_v` + `data`, not the meta
  fields), then treat the session as inactive. The session's
  recording hooks would skip the request; downstream analyze would
  see fewer recordings than expected. Bench restart cycles workers
  atomically so the mixed window is bounded; in-flight sessions
  during the gap would lose some recordings.

### Unchanged

- `optimus/redis_schema.py` — envelope helpers ship as-is.
- `session_recordings` (the per-session set of recording UUIDs) —
  stays unmigrated. Sets aren't a natural fit for the value envelope
  (the envelope wraps a single value; sets are member collections).
  Per-member wrapping would add overhead per recording without
  obvious benefit (members are opaque UUIDs).

Unit suite: 1854 → 1859 (+5 from the new phase-4 test module).
Integration suite unchanged at 39.

---

## [0.12.20] — 2026-05-25

**LP key-helper unification — Phase-2 line_profile capture now uses
`optimus.redis_keys.lp_*` instead of its local key helpers.**

The v0.12.0 release centralised every Redis key pattern in
`optimus/redis_keys.py` and added drift-detection (`test_redis_audit.py`),
but `optimus/line_profile/capture.py` kept its own `_active_key` /
`_picks_key` / `_source_key` / `_samples_key` / `_budget_hit_key`
helpers — deferred from v0.12.0 as cleanup. This release closes that
gap. Pure cleanup; zero behaviour change.

### Changed

- **`optimus/line_profile/capture.py`** — top-of-module
  `from optimus import redis_keys as _redis_keys` added; the 5 local
  key helpers retired; ~16 call sites migrated to
  `_redis_keys.lp_active(user)` / `_redis_keys.lp_picks(run_uuid)` /
  etc. The `cleanup_run` iterable that walked the local helpers as
  function references now walks the `_redis_keys.lp_*` references
  instead.

### Why this matters

- **Single source of truth** — `optimus/redis_keys.py` is the
  documented "every key built here, every key found via
  `test_redis_audit.py`" location. The LP local helpers were the
  last surviving exception (per `[[reference_optimus_redis_schema]]`).
- **Drift protection** — the v0.12.0 audit test's inline-key regex
  scanned for f-string `profiler:*` patterns; the LP local helpers
  were f-strings, which is why they tripped the audit and got
  flagged on the deferred-work list.
- **Byte-identical keys** — `_redis_keys.lp_active(user)` produces
  the EXACT same string (`profiler:lp:active:<user>`) the local
  helper did. On-disk Redis values from pre-v0.12.20 benches resolve
  unchanged.

### Unchanged

- All v0.12.0 unit tests (`test_redis_audit.py` etc.) — they didn't
  need to change because the key strings stayed identical.
- `redis_keys.lp_*` functions in `optimus/redis_keys.py` — the
  receiving side stays as the v0.12.0 documented shape.
- Behaviour identical — every cache write/read produces the same
  key, just resolved through the centralized module.

### Compatibility

Pure cleanup. No behaviour change. Unit suite stays at 1854.

---

## [0.12.19] — 2026-05-25

**Renderer extraction — `finding_enrichment` phase 2 (drill-down chain
attachers) ships alongside the existing phase 1. Same submodule.**

The drill-down chain cluster — `_find_node_in_tree`,
`_walk_drilldown_chain`, `_attach_drilldown_chains` — is a self-
contained sub-cluster of the larger finding_enrichment family that
the v0.12.16 docstring documented as "still in `_internal.py`". Today
it moves into `finding_enrichment.py` alongside the phase 1 helpers.
Same submodule because their architectural identity matches; the
"phase" distinction is about extraction batches, not boundaries.

### Moved

- **`_find_node_in_tree(tree, basename, function)`** — depth-first
  walk a pyinstrument call tree for a `(basename, function)` match.
  Stdlib-only.
- **`_walk_drilldown_chain(tree, callsite, ...)`** — hottest-child
  traversal below a finding's origin frame. Uses
  `_find_node_in_tree` + lazy imports of
  `optimus.renderer.call_tree_renderer._ct_is_other_frame` and
  `optimus.analyzers.base.is_framework_callsite`.
- **`_attach_drilldown_chains(findings, actions, tracked_apps)`** —
  in-place attachment of the chain onto each finding's
  `technical_detail`. Uses `_walk_drilldown_chain` + `json.loads` for
  tree deserialisation.

Out of `optimus/renderer/_internal.py` (now ~3,158 LOC; was ~3,320),
into existing `optimus/renderer/finding_enrichment.py` (extended from
~210 LOC → ~375 LOC).

### Implementation

- The `_ct_is_other_frame` lazy import inside `_walk_drilldown_chain`
  avoids the circular import risk: `call_tree_renderer.py` could
  theoretically grow a dependency on finding_enrichment in the
  future; the lazy form makes that direction safe.
- `is_framework_callsite` already lazy-loaded inside the original
  function body — preserved.
- The standard back-import block at the top of `_internal.py`
  re-imports `_find_node_in_tree`, `_walk_drilldown_chain`,
  `_attach_drilldown_chains` so call sites in `render()` resolve
  unchanged through the same shim path.

### What stays in _internal.py (the still-deferred subset)

The HIGH-coupling subset depends on source-resolution helpers
(`_action_dotted_entry`, `_skip_decorators_to_def`,
`_resolve_dotted_to_code`, `_action_entry_callsite`,
`_resolve_frame_key_to_callsite`, `_bench_relative_display`) which
are themselves intermixed with action-rendering code. Phase 3 needs
either a sibling `source_resolution.py` extraction first (clean) or
an expanded back-import design (messy). Per-batch scope decision:
ship phase 2 today (clean), document phase 3 for later.

Still in `_internal.py` pending phase 3:
- `_finding_to_dict` (~200 LOC, the main render-dict builder).
- `_attach_representative_callsites` (calls 6 source-resolution
  helpers above).
- `_expand_self_time_snippets` (calls `_read_function_body_snippet`).
- `_retarget_phase1_callsites_to_drilldown_leaf` (uses
  `_find_call_line_in_function_body`).
- `_find_call_line_in_function_body` (AST walker).

### Docs

- `optimus/renderer/README.md` — current-layout block updated
  (`finding_enrichment.py` now lists phase 1+2 contents); roadmap
  table marks `finding_enrichment` as ◐ phase 1+2 done, ~365 LOC
  remaining.

### Unchanged

- Behaviour identical — every call site through `_internal.py`'s
  shim resolves the function the same way.
- All 62 unit tests across `test_drilldown_chain.py` +
  `test_finding_card_smoking_gun.py` (which exercise the moved
  functions via the `renderer.X` package surface) stay green without
  modification.
- `test_renderer_structure_snapshot.py` (14 tests) stays green
  without fixture regeneration — output HTML is byte-equivalent.

### Compatibility

No behaviour change. Pure refactor. Unit suite stays at 1854.

---

## [0.12.18] — 2026-05-25

**Janitor proactive schema-drift sweep — the daily cron now compares
the persisted `optimus:schema_version` sentinel against the current
`SCHEMA_VERSION` and emits one telemetry event per drift detection.**

The v0.12.0 release introduced the `optimus:schema_version` sentinel
written at app-import as the foundation for future migration paths.
The sentinel was deliberately not READ by anything except the test —
"reactive cleanup-on-read is sufficient for the foundation. A future
release can add a janitor pass that enumerates `profiler:*` keys and
migrates / purges those with mismatched versions" (v0.12.0 docstring
in `redis_schema.py`). This release ships the **visibility** half of
that future-release plan; **migration** stays deferred (per-value
shape would need per-value migrators).

### Added

- **NEW `janitor._sweep_schema_drift()`** — pure-function sweep that
  reads the sentinel via `read_schema_sentinel`, compares against
  `SCHEMA_VERSION`, and on real drift:
  - Writes the current sentinel via `write_schema_sentinel` so the
    NEXT sweep sees the new value (single emit per drift).
  - Emits one `janitor.schema_sentinel_drift` telemetry event with
    severity `warning` and context `{persisted_version,
    current_version}`.
  - No-ops on the happy path (sentinel == current).
  - No-ops with sentinel-write only (no telemetry) on the fresh-
    install path (sentinel is None) — that's the normal startup
    transition, not interesting enough to alert on.
- **`sweep_old_sessions` wired** to call `_sweep_schema_drift` after
  the existing `_sweep_old_telemetry`. Wrapped in its own try/except
  that emits `janitor.sweep_schema_drift` on its own failure
  (matching the sibling-sweep pattern).
- **NEW `optimus/tests/test_janitor_schema_drift.py`** (~200 LOC,
  6 tests):
  - `test_no_op_when_sentinel_matches_current` — happy path.
  - `test_fresh_install_writes_sentinel_no_telemetry` — fresh
    install transition silently writes the sentinel.
  - `test_post_upgrade_drift_emits_telemetry` — sentinel < current
    fires the telemetry warning.
  - `test_post_downgrade_drift_emits_telemetry` — sentinel >
    current fires symmetrically (catches operator downgrade).
  - `test_sentinel_write_failure_swallowed` — Redis hiccup during
    sentinel write doesn't propagate; telemetry still fires.
  - `test_sweep_old_sessions_calls_sweep_schema_drift` — wiring
    canary on the cron entry point.

### Why visibility (not migration)

Per-value migration would need a per-value migrator since each value
shape has its own evolution semantics. The reactive cleanup-on-read
(via `unwrap_value`'s legacy-detection branch + per-read
`redis.schema_drift` telemetry) already handles the data-correctness
path. The proactive sweep's job is **operator visibility**: one
per-day high-confidence notification when the schema actually shifts,
so operators can plan migrations / rollbacks before per-read drift
events flood the telemetry table.

### Unchanged

- `optimus/redis_schema.py` — `read_schema_sentinel` /
  `write_schema_sentinel` / `SCHEMA_VERSION` / `wrap_value` /
  `unwrap_value` all ship as-is.
- `_sweep_orphan_redis_state`, `_sweep_old_telemetry` — the existing
  daily sweeps stay unchanged.

### Compatibility

No behaviour change at the cache-value layer. Pure visibility
addition. On the first daily-cron run after deploying v0.12.18, sites
that have been on the v0.12.0 schema (SCHEMA_VERSION=1) see no drift;
sites that were on an older version (sentinel missing) silently write
the sentinel.

Unit suite: 1848 → 1854 (+6 from the new schema-drift test module).
Integration suite unchanged at 39.

---

## [0.12.17] — 2026-05-25

**Redis-schema rollout continues — `explain_cache` is the fourth
value migrated to the v0.12.0 `wrap_value` / `unwrap_value` envelope.**

Previous phases covered `settings_cache` (v0.12.11),
`retention_backlog` + `onboarding_seen` (v0.12.13). This release adds
`explain_cache` — the cross-session cache of EXPLAIN query results
that powers the analyzer's slow-query / missing-index detection.

### Changed

- **`optimus/analyze.py`** — the EXPLAIN cache write/read pair around
  line 1352-1380 now uses the envelope:
  - WRITE: `frappe.cache.set_value(shared_key,
    _redis_schema.wrap_value(result), expires_in_sec=cache_ttl)`.
  - READ: `raw_cached = frappe.cache.get_value(shared_key);
    cached, _version = _redis_schema.unwrap_value(raw_cached)`.

  Top-of-module `from optimus import redis_schema as _redis_schema`
  added alongside the existing `redis_keys as _redis_keys` import.

### Why explain_cache

- **Highest-frequency cache by call count** — every analyze run that
  touches a slow query checks here; the hit rate determines whether
  re-analyze is fast (~5s) or slow (~30s, runs EXPLAIN again).
- **Most complex shape rolled out so far** — payload is
  `list[dict]` (EXPLAIN row results from MariaDB); validates that
  the envelope preserves nested structure cleanly.
- **TTL-bounded** — pre-v0.12.17 cached entries fall off the cache
  naturally within `optimus_explain_cache_ttl_seconds` (default
  3600s); the legacy-shape backward-compat branch covers the brief
  window after deploy.

### Added

- **NEW `optimus/tests/test_envelope_rollout_phase3.py`** (~110
  LOC, 5 tests):
  - `TestExplainCacheEnvelopeRoundTrip` (3 tests) —
    list-of-dicts round-trip, empty-list round-trip, legacy
    bare-list pass-through (the migration-safety contract).
  - `TestAnalyzeSourceUsesEnvelope` (2 tests) — source-grep canaries
    on the write + read sites.

### Docs

- `docs/REDIS-SCHEMA.md` — `profiler:explain:<cache_key>` row updated
  to reflect the new envelope shape + legacy-compat note.

### Compatibility

- **Forward** (new readers, old writers): supported via
  `unwrap_value`'s legacy-detection branch. Pre-v0.12.17 cached
  EXPLAIN results stored as bare list[dict] still resolve cleanly.
- **Backward** (old readers, new writers): NOT supported. An old
  worker reading a new envelope would try to use the dict as the
  EXPLAIN result, likely crashing downstream when an analyzer
  accesses `result[0]["select_type"]`. Bench restart cycles workers
  atomically; the TTL on the cache (default 1h) means stale-new
  values age out within an hour anyway.

### Unchanged

- `optimus/redis_schema.py` — envelope helpers ship as-is.
- The remaining bare-shaped values (`analyze_inflight`, LP
  `picks`/`source`/`samples`/`active`, the session hash family, the
  Frontend buckets) — future PRs roll out one at a time.

Unit suite: 1843 → 1848 (+5 from the new phase-3 test module).
Integration suite unchanged at 39.

---

## [0.12.16] — 2026-05-25

**Renderer extraction — `finding_enrichment` phase 1 (the low-coupling
subset) is the eighth submodule out of `_internal.py`.**

The full `finding_enrichment` cluster spans ~11 functions
non-contiguously across ~1500 LOC of `_internal.py`. The previous
batch documented this as a "defer to focused PR" item because of the
high coupling. This release ships the **tight subset** — the 3 pure-
function helpers that have minimal back-coupling — leaving the
HIGH-coupling subset (`_finding_to_dict` family + AST walker + chain
attachers) for a future PR.

### Moved

- **`_root_cause_key(finding)`** — `(basename, function)` deepest-
  user-code anchor for a finding. Stdlib-only.
- **`_group_findings_by_root_cause(findings)`** — collapse findings
  sharing a root cause into one primary + `sub_findings` list. Uses
  `_root_cause_key` + `_GROUPING_SEVERITY_RANK`.
- **`_normalize_callsite(callsite)`** — normalize dict-or-string
  callsite shapes to a single `{filename, lineno, function}` dict.
  Stdlib-only.
- **Constant `_GROUPING_SEVERITY_RANK`** — severity-rank lookup
  (different from `doc_event_renderer._SEVERITY_RANK`; different
  values + use).

Out of `optimus/renderer/_internal.py` (now ~3,316 LOC; was ~3,482),
into NEW `optimus/renderer/finding_enrichment.py` (~210 LOC).

### Implementation

- Standard `from optimus.renderer.finding_enrichment import …` shim
  block at the top of `_internal.py`. Re-imports `_GROUPING_SEVERITY_RANK`,
  `_root_cause_key`, `_group_findings_by_root_cause`, `_normalize_callsite`
  so call sites resolve unchanged.
- Structural-snapshot canary stays green without fixture regeneration.

### Deferred (the remaining HIGH-coupling subset)

The following stay in `_internal.py` pending a focused future PR with
proper coupling-graph design:

- `_finding_to_dict` (~200 LOC, the renderer's main finding-to-dict
  builder — calls many internal helpers).
- `_walk_drilldown_chain` + `_attach_drilldown_chains`.
- `_attach_representative_callsites` (calls source-resolution
  helpers `_action_dotted_entry`, `_skip_decorators_to_def`,
  `_resolve_dotted_to_code`, `_action_entry_callsite`,
  `_resolve_frame_key_to_callsite`, `_bench_relative_display`).
- `_expand_self_time_snippets`.
- `_retarget_phase1_callsites_to_drilldown_leaf` (uses
  `_find_call_line_in_function_body`).
- `_find_call_line_in_function_body` (AST walker).

A future "finding_enrichment phase 2" PR would need to either move
the source-resolution helper family with the cluster (extracting a
new `source_resolution.py`-style sibling submodule), or leave the
helpers in `_internal.py` and use the back-import pattern at
sufficient scale that the cycle-detection becomes worth designing
for. Neither is a one-batch task.

### Docs

- `optimus/renderer/README.md` — current-layout block bumped to 8
  submodules; roadmap table marks `finding_enrichment` as ◐ (phase 1
  done; rest deferred). Updated cluster-size estimate.

### Unchanged

- Behaviour identical — every call site through `_internal.py`'s
  shim resolves the function the same way.
- All unit tests pass without modification (the 3 moved functions
  weren't directly imported by name in any test file outside
  `_internal.py` itself).

### Compatibility

No behaviour change. Pure refactor. Unit suite stays at 1843.

---

## [0.12.15] — 2026-05-25

**Workflow cleanup — `.github/workflows/integration.yml`'s 9 hard-coded
`bench run-tests --module` invocations replaced with a single shell
loop driven by an `INTEGRATION_MODULES` env list.**

The v0.11.0 → v0.12.7 deferred-tests roadmap grew the integration
workflow from 3 to 9 modules. Each was a separate ~4-line stanza
copied + edited from its predecessor. Adding a future module meant
remembering the boilerplate; reviewers had to verify the per-stanza
shape stayed consistent.

### Changed

- **`.github/workflows/integration.yml`** — the `Run the integration
  suite` step now declares its modules in one place:
  ```yaml
  env:
    INTEGRATION_MODULES: |-
      test_install_smoke
      test_recording_lifecycle_e2e
      test_atomic_lua_merge_concurrent
      test_telemetry_flush_doctype_sink
      test_ai_privacy_exclusion_on_api
      test_regenerate_reports_idempotent
      test_phase2_tool_orphan_recovery
      test_safe_report_self_contained_on_real_bench
      test_janitor_sweeps_actually_delete
  ```
  A shell `while read` loop iterates the list and invokes `bench
  run-tests` per module. Adding a future module is a one-line edit
  to the env list.
- **Failure accumulation** — the pre-v0.12.15 implementation used
  `set -e`, which made the FIRST failing module abort all siblings
  (despite the YAML comment claiming otherwise). The new accumulator
  pattern runs EVERY module then fails the workflow if any failed —
  a single push surfaces every failure at once. **Behaviour
  improvement**: more signal per CI run when a refactor breaks two
  unrelated test modules.

### Unchanged

- Per-module log artifacts (`integration-<module>.log`) still upload
  on failure via the existing artifact-upload step. No log path
  changes.
- The 9 module names — same set, same order, exact same module paths.
- All other workflow steps (services, caches, install, summary,
  artifact upload) — untouched.
- Frappe v16 bench bootstrap path via `.github/helper/install.sh` —
  unchanged.

### Compatibility

CI-only change. Local `bench run-tests --app optimus
--module optimus.tests_integration.<name>` invocations are unaffected.

Unit suite unchanged at 1843. Integration suite unchanged at 39.

---

## [0.12.14] — 2026-05-25

**HMAC envelope versioning — `sign_blob` / `unsign_blob` now embed a
1-byte scheme version between the signature and the payload.**

The v0.12.0 `wrap_value` rollout addressed value-shape versioning at
the cache-value boundary. The HMAC envelope at the bytes-on-the-wire
boundary (`optimus/session.py:sign_blob` / `unsign_blob`, signing the
pickled pyinstrument trees that recordings stash in Redis) had no
equivalent versioning hook. A future signing-scheme bump (HMAC-SHA512,
AES-SIV, key-rotation tag) would have required a hard cutover; this
release adds the extension point so future bumps land cleanly.

### Changed

- **`optimus/session.py:sign_blob`** now inserts a 1-byte scheme
  marker (`_HMAC_SCHEME_V1 = 0x01`) between the 32-byte HMAC and the
  payload. The HMAC covers the version-tagged body
  (`HMAC(\\x01 + payload)`), so tampering with the marker is detected
  on read.
- **`optimus/session.py:unsign_blob`** handles two body shapes after
  the 32-byte signature:
  - **v1 (current, v0.12.14+)** — body starts with
    `_HMAC_SCHEME_V1`; strip the marker, return `body[1:]`.
  - **v0 (legacy, pre-v0.12.14)** — body is the raw payload; return
    `body` as-is.

  The single HMAC verification step handles both cases — for v1 the
  HMAC was computed over `\\x01 + payload` AND that's exactly what the
  body is; for v0 the HMAC was computed over the payload AND the body
  is just the payload. Post-verify, the body's first byte
  disambiguates.

### Why `\x01` as the marker

Pickle (the only producer of payloads for `sign_blob` today) never
uses `\x01` as a leading opcode:

- Pickle protocols 2-5 start with `\x80` (PROTO opcode).
- Pickle protocol 0 starts with printable ASCII opcodes (`(`, `c`,
  `[`, etc.).

So a legacy pre-v0.12.14 pickle payload's first byte can never
accidentally trigger the v1 strip branch in `unsign_blob`. Future
producers (msgpack, JSON, raw binary) just need to avoid `\x01` as a
leading byte — or write through the v1 path so the marker is
explicit.

### Added

- **NEW `optimus/tests/test_hmac_envelope_versioning.py`** (~220
  LOC, 13 tests):
  - **`TestSignUnsignRoundTrip`** (3 tests): v1 round-trip for
    typical payload, empty payload, pickle-shaped payload.
  - **`TestLegacyShapeBackwardCompat`** (2 tests): hand-crafted
    pre-v0.12.14 v0 blobs (no version marker) still verify and
    return the original payload; pickle-shaped legacy blobs work
    too (the `\x80` first byte stays as-is, no incorrect strip).
  - **`TestNewShapeByteLayout`** (1 test): canary on the byte
    layout — `[32-byte HMAC] + \\x01 + payload`, length =
    `32 + 1 + len(payload)`.
  - **`TestTamperingDetection`** (3 tests): tampered signature →
    None; tampered version byte → None (HMAC covers it); tampered
    payload → None.
  - **`TestEdgeCases`** (4 tests): too-short input → None; non-bytes
    input → None; `sign_blob` rejects non-bytes; the
    no-stable-secret path passes through unsigned (preserves Phase
    K behaviour when `frappe.conf.encryption_key` is absent).

### Compatibility

- **Forward** (new readers, old writers): supported. The v0 branch
  in `unsign_blob` handles pre-v0.12.14 blobs cleanly.
- **Backward** (old readers, new writers): NOT supported. A
  pre-v0.12.14 reader computes HMAC over the body bytes (which now
  include the version marker) and gets a verification mismatch →
  returns None → the recording / pyinstrument tree is silently
  dropped on that worker. Bench restart cycles all workers
  atomically, so the mixed window is small. On a multi-day rollout
  where workers don't all restart together, in-flight recordings
  during the gap would be lost (same constraint as the
  `wrap_value` rollout in v0.12.11+).

### Unchanged

- `_has_stable_hmac_secret()` and `_hmac_secret()` — secret
  resolution unchanged.
- `_SIG_LEN = 32` — signature length unchanged (still SHA-256).
- The Phase K no-secret pass-through path (returns raw blob when
  `frappe.conf.encryption_key` is absent) — unchanged. The version
  marker is only inserted when the HMAC actually fires.

Unit suite: 1830 → 1843 (+13 from the new envelope-versioning test
module). Integration suite unchanged at 39.

---

## [0.12.13] — 2026-05-25

**Redis-schema rollout continues — `retention_backlog` and
`onboarding_seen` are the second and third values migrated to the
v0.12.0 `wrap_value` / `unwrap_value` envelope.**

v0.12.11 shipped the first migration (`settings_cache`). This release
continues the rollout to two more values, both simple-shape (int +
string) — which exercises the legacy-detection branch in
`unwrap_value` more thoroughly than the dict-shaped `settings_cache`
did.

### Changed

- **`optimus/janitor.py`** — both `retention_backlog` write sites
  (after-cap + after-clear) now wrap their int payload via
  `redis_schema.wrap_value(backlog)` / `redis_schema.wrap_value(0)`.
  No in-app reader exists yet (operator-facing metric only), so the
  rollout is write-only for now; the future-safety value is that any
  consumer reading directly from Redis gets the envelope shape.
- **`optimus/api.py`** — `mark_onboarding_seen` writes via
  `wrap_value("1")` and `check_onboarding_seen` reads via
  `unwrap_value(...)`. Both shapes (new envelope + legacy bare string)
  resolve to a truthy `bool(payload)` so the toast-dismissed semantics
  are unchanged.

### Added

- **NEW `optimus/tests/test_envelope_rollout_phase2.py`** (~200 LOC,
  7 tests):
  - `TestRetentionBacklogEnvelope` (2 tests): write-shape assertion
    + source-grep canary against a regression that reverts the wrap.
  - `TestOnboardingSeenEnvelopeReadCompat` (3 tests): unwrap of new
    envelope → truthy; unwrap of legacy bare string `"1"` → truthy
    (the migration-safety contract); unwrap of missing key → falsy.
  - `TestOnboardingSeenEnvelopeWriteShape` (2 tests): source-grep
    canaries on `mark_onboarding_seen` (writes via `wrap_value`)
    and `check_onboarding_seen` (reads via `unwrap_value`).

### Docs

- `docs/REDIS-SCHEMA.md` — `optimus:retention_backlog` and
  `profiler:onboarding_seen:<user>` rows updated to reflect the new
  envelope shape + legacy-compat note.

### Compatibility

- **Forward** (new readers, old writers): supported for
  `onboarding_seen` via `unwrap_value`'s legacy-detection branch.
  Not relevant for `retention_backlog` (write-only).
- **Backward** (old readers, new writers): NOT supported for
  `onboarding_seen` — an old reader would do `bool(envelope_dict)`
  which is always truthy (dicts are truthy in Python). The
  practical effect on a mid-deploy bench is that some users might
  see "onboarding already dismissed" even when the underlying value
  was just set; the toast stays hidden either way, so no user
  visible issue. For `retention_backlog`, old code reading via
  `int(envelope_dict)` would crash — but there's no in-app reader.

### Unchanged

- `optimus/redis_schema.py` — envelope helpers ship as-is.
- All other Redis values — the session hash family, the LP pick
  keys, `explain_cache`, `analyze_inflight` — all still bare-shaped.
  Future PRs roll out one at a time as the cost/benefit warrants.

Unit suite: 1823 → 1830 (+7 from the new envelope-rollout-phase-2
test module). Integration suite unchanged at 39.

---

## [0.12.12] — 2026-05-25

**Renderer extraction — `line_drilldown` is the seventh submodule out
of `_internal.py`. Structural snapshot stays byte-identical.**

The line_drilldown cluster was the README's "single biggest remaining
chunk" (Internal coupling, 840 LOC nominal). The extraction landed at
416 LOC by scoping NARROWER than the README's original outline:
`_find_call_line_in_function_body` (AST-walking helper) and the
finding-enrichment helpers it serves (`_retarget_phase1_callsites_to_drilldown_leaf`,
`_root_cause_key`, `_group_findings_by_root_cause`) stay in
`_internal.py` for the still-pending finding_enrichment extraction.

### Moved

- **Semi-public surface** (called by `analyze.py` via the
  package shim):
  - `_build_line_drilldown_callsite_index(session_doc)` — builds
    the `(basename, function_name) → hottest-line` lookup powering
    the finding-card "Line-Level Drilldown hot line: …" callout.
- **Public render entry-point**:
  - `_render_line_drilldown_panel(session_doc)` — the section HTML
    builder. Empty string when the session has no phase-2 runs.
- **Internal helpers**:
  - `_make_line_drilldown_lookup` — Jinja adapter for tuple-keyed
    lookups.
  - `_phase2_invoked` — per-function "did it run?" check (delegates
    to `optimus.line_profile.analyzer._function_invoked`).
  - `_render_phase2_function_table` — per-function HTML table.
  - `_render_phase2_diff_table` — cross-run delta HTML table.
- **Back-compat aliases** (pre-v0.7.x renames):
  - `_build_phase2_callsite_index = _build_line_drilldown_callsite_index`
  - `_make_phase2_lookup = _make_line_drilldown_lookup`
  - `_render_phase2_panel = _render_line_drilldown_panel`

Out of `optimus/renderer/_internal.py` (now ~3,470 LOC; was ~3,886),
into NEW `optimus/renderer/line_drilldown.py` (~416 LOC).

### Implementation

- The new submodule imports `_highlight_python_snippet` from
  `optimus.renderer.syntax` (the v0.10.0 extraction) and
  `_format_duration_ms` from `optimus.renderer.time_format` (also
  v0.10.0). Lazy imports of `optimus.line_profile.diff` and
  `optimus.line_profile.analyzer` stay intact.
- Standard `from optimus.renderer.line_drilldown import …` block at
  the top of `_internal.py` re-imports every name (public + aliases)
  so package-level dir-walk re-export still surfaces them under
  legacy `optimus.renderer.X` paths.
- Output HTML byte-equivalent: `test_renderer_structure_snapshot.py`
  (14 tests) stays green without fixture regeneration.

### Why narrower than the README

The README's 840-LOC estimate bundled `_find_call_line_in_function_body`
(AST-walking helper) and `_retarget_phase1_callsites_to_drilldown_leaf`
(callsite-rewriting helper) into the line_drilldown cluster. In
practice, both are called by code that stays in `_internal.py` (the
`render()` orchestrator's finding-enrichment phase). Moving them now
would require a back-import that's then immediately re-imported into
the orchestrator — defeating the goal of a clean cluster boundary.
The finding_enrichment extraction will own those helpers.

### Docs

- `optimus/renderer/README.md` — current-layout block bumped to 7
  submodules; roadmap table marks `line_drilldown` as ✓ done.
  Updated `finding_enrichment` row to note the additional helpers it
  now owns. 1 cluster remains (plus `render()` orchestrator which is
  flagged "keep integrated").

### Unchanged

- Behaviour identical — every call site through `_internal.py`'s
  shim resolves the function the same way.
- `analyze.py` still calls `renderer._build_line_drilldown_callsite_index`
  unchanged via the package re-export.
- All unit tests that resolve via `renderer.X` (test_render_phase2_panel.py,
  test_line_drilldown_matching.py, test_line_profile_job_capture.py,
  test_renderer_structure_snapshot.py:test_build_line_drilldown_callsite_index_resolves)
  stay green without changes.

### Compatibility

No behaviour change. Pure refactor. Unit suite stays at 1823.

---

## [0.12.11] — 2026-05-25

**Redis-schema versioning — first value migrated to the `wrap_value`
/ `unwrap_value` envelope.**

The v0.12.0 release shipped the versioning foundation
(`optimus/redis_schema.py`: `wrap_value`, `unwrap_value`,
`SCHEMA_VERSION`, sentinel-write at app-import) but deliberately did
not wrap any existing value. This release starts the rollout:
**`settings_cache`** is the first value to migrate.

### Why settings_cache first

* **Hot path** — `get_config()` is called from every request hook;
  the envelope overhead has to be invisible.
* **Single writer + single reader** — both inside
  `optimus/settings.py`, no cross-module coordination.
* **Stable dict shape** — `OptimusConfig.__dict__` is well-defined.
  Future schema bumps would touch the dataclass fields, which is
  exactly the situation the envelope was designed for.
* **Cache invalidation already in place** — the Optimus Settings
  DocType controller's `on_update` deletes the key, so the next
  request rebuilds the value with the new envelope shape
  automatically. Rollout proves itself live on the first save after
  upgrade.

### Changed

- **`optimus/settings.py:get_config()`**:
  - WRITE: `frappe.cache.set_value(_CACHE_KEY,
    redis_schema.wrap_value(cfg.__dict__))` instead of bare
    `set_value(_CACHE_KEY, cfg.__dict__)`.
  - READ: `payload, _version = redis_schema.unwrap_value(
    frappe.cache.get_value(_CACHE_KEY))`; new-shape envelopes
    unwrap to the dict, legacy pre-v0.12.11 bare-dict values pass
    through unchanged via `unwrap_value`'s legacy-detection branch.

### Added

- **NEW `optimus/tests/test_settings_envelope_rollout.py`** (~200 LOC,
  4 tests, all pure-pytest with a dict-backed `_FakeCache`):
  - `test_fresh_write_stores_envelope_not_bare_dict` — write-path
    canary; asserts the post-write cache value is shaped
    `{"_v": SCHEMA_VERSION, "data": {...}}`.
  - `test_hit_on_enveloped_value_returns_config` — new-shape read
    path; cache HIT reconstructs OptimusConfig without re-resolving.
  - `test_hit_on_legacy_bare_dict_returns_config` — **the
    migration-safety contract**. Pre-v0.12.11 writers stored bare
    dicts; new readers must NOT crash. Catches a regression where
    `unwrap_value`'s legacy-detection branch goes away.
  - `test_drift_falls_through_to_resolve` — a future-schema
    envelope (`_v=999`) triggers fall-through to `_resolve` AND a
    `redis.schema_drift` telemetry event; the next write produces a
    fresh current-version envelope so the cache doesn't stay broken.

### Docs

- `docs/REDIS-SCHEMA.md` — `optimus_settings_cached` row updated to
  reflect the new envelope shape; legacy-compat note added.

### Compatibility

- **Forward** (new readers, old writers): supported. The legacy
  bare-dict path stays open in `unwrap_value` for as long as readers
  might encounter pre-v0.12.11 values.
- **Backward** (old readers, new writers): NOT supported. An old
  worker reading a new-shape value would try
  `OptimusConfig(**{"_v": 1, "data": {...}})` and fail on the
  unrecognised `_v` keyword arg. Bench restart cycles all workers
  at once, so the mixed-state window is the bounded few-second
  gap between worker stop + worker start. Per
  `[[reference_optimus_redis_schema]]` the rollout was always going
  to require a single-bench restart cadence; this is documented in
  the schema-bump contract.

### Unchanged

- `optimus/redis_schema.py` — the envelope helpers ship as-is; no
  edits to the wrap/unwrap logic. Only the call-site code changed.
- All other Redis values — `retention_backlog`, `analyze_inflight`,
  `onboarding_seen`, `explain_cache`, the session hash, etc. — all
  still bare-shaped. Future PRs roll out one at a time.

Unit suite: 1819 → 1823 (+4 from the new envelope-rollout test
module). Integration suite unchanged at 39.

---

## [0.12.10] — 2026-05-25

**Renderer extraction — `doc_event_renderer` is the sixth submodule
out of `_internal.py`. Structural snapshot stays byte-identical.**

Per the v0.10.0 renderer-package roadmap, the doc-event lifecycle
cluster was the next target — Moderate coupling per the README
(touches lifecycle-binding logic) but cleanly module-import-time
self-contained.

### Moved

- **Public surfaces** (called from the render orchestrator in
  `_internal.py`):
  - `_extract_target_doc(form_dict)` — best-effort pull of
    `{doctype, name}` from a request's form_dict.
  - `_attach_action_context(actions, findings, recordings_by_uuid)`
    — in-place enrichment of `target_doc` + `hook_events`.
  - `_build_doc_event_breakdown(findings)` — pure-function transform
    that groups findings by DocType → lifecycle event.
- **Internal helpers** (only called within the cluster):
  - `_module_from_filename`, `_doctype_from_controller_path`,
    `_build_doc_event_hook_index`, `_doc_event_hook_index`,
    `_finding_hook_events`, `_finding_lifecycle_bindings`.
- **Constants**: `_LIFECYCLE_EVENTS`, `_KIND_DOC_EVENTS_HOOK`,
  `_KIND_CONTROLLER_OVERRIDE`, `_SEVERITY_RANK`.

Out of `optimus/renderer/_internal.py` (now ~3,869 LOC; was ~4,213),
into NEW `optimus/renderer/doc_event_renderer.py` (~423 LOC).

### Implementation

- `_SEVERITY_RANK` is a local constant in the new submodule (the
  same name exists in `_internal.py` as `_GROUPING_SEVERITY_RANK`
  with different values — unrelated; renamed-in-original avoided
  collision). The new submodule's `_SEVERITY_RANK` matches the
  pre-extraction local-scope name + values.
- Standard `from optimus.renderer.doc_event_renderer import …`
  shim block at the top of `_internal.py` re-imports every public
  name + every constant, so package-level dir-walk re-export still
  surfaces them under legacy `optimus.renderer.X` paths.
- Output HTML byte-equivalent: `test_renderer_structure_snapshot.py`
  (14 tests) stays green without fixture regeneration.

### Docs

- `optimus/renderer/README.md` — current-layout block bumped to 6
  submodules; follow-up roadmap table marks `doc_event_renderer` as
  ✓ done. 3 clusters remain (`line_drilldown`, `finding_enrichment`,
  `render()` orchestrator).

### Unchanged

- Behaviour identical — every call site through `_internal.py`'s
  shim resolves the function the same way.
- All 57 unit tests across `test_doc_event_lifecycle.py` +
  `test_action_context.py` (which resolve via `renderer.X`) stay
  green without test changes.

### Compatibility

No behaviour change. Pure refactor. Unit suite stays at 1819.

---

## [0.12.9] — 2026-05-25

**Bug fix — `api.regenerate_reports` now enforces its documented
"Ready or Failed sessions only" contract.**

v0.12.4 surfaced (via the new integration test) that
`api.regenerate_reports` had **no `status` gate** in its
implementation. The docstring claimed "Allowed on Ready OR Failed
sessions" but the code accepted any status — Recording, Stopping,
Analyzing all re-rendered. Re-rendering an in-flight session would
attach an incomplete report to a still-running analyze, which the
pipeline would then overwrite on completion. Confusing for
operators; load-bearing only for the few who had a Failed session
they wanted to re-render after a renderer fix.

### Changed

- **`api.regenerate_reports` now throws `ValidationError`** for any
  status outside `{Ready, Failed}` with a message pointing the
  operator at `retry_analyze` as the recourse for stuck pipelines
  (`api.py:1167-1180`). The gate fires after the
  ``session_uuid`` / row-existence checks but before any other side
  effect, so refused calls produce zero attachment churn.

### Added

- **`optimus/tests/test_regenerate_reports_api.py::test_status_gate_rejects_non_terminal_sessions`**
  — pure-pytest source-inspection test that confirms the gate is
  present and the error message points at `retry_analyze`.
- **`optimus/tests_integration/test_regenerate_reports_idempotent.py::test_regenerate_refuses_non_terminal_status`**
  — integration test that demotes a session to `Analyzing` and
  asserts the endpoint raises with an operator-friendly message.

### Compatibility

Soft breaking. Any external automation that called
`regenerate_reports` against an Analyzing / Recording / Stopping
session would now see a `ValidationError`. In practice no operator
flow does this — the UI button is only visible on Ready / Failed
sessions per the existing `test_button_visible_on_ready_and_failed`
test. Anyone shelling out to the endpoint directly (rare) would have
been getting incomplete / overwritten reports before this fix.

### Discovery → fix lineage

- v0.12.4 CHANGELOG documented the gap as "out of scope for this PR".
- v0.12.9 (this PR) closes it.

Unit suite: 1819 (1818 + 1 new source-inspection test). Integration
suite: 39 (38 + 1 new test in the existing module).

---

## [0.12.8] — 2026-05-25

**Renderer extraction — `call_tree_renderer` is the fifth submodule
out of `_internal.py`. Structural snapshot stays byte-identical.**

Per the v0.10.0 renderer-package roadmap
(`optimus/renderer/README.md`), the call-tree panel cluster was the
next lowest-coupling target. Moves:

* `_CALL_TREE_MAX_DEPTH`, `_CALL_TREE_HARD_CAP` — depth constants.
* `_CT_OTHER_RE` — synthetic-placeholder regex.
* `_ct_is_other_frame`, `_ct_is_sql_leaf`, `_ct_is_user_frame` — node
  classifiers.
* `_render_call_tree_node`, `_render_call_tree_panel` — the rendering
  pair.

Out of `optimus/renderer/_internal.py` (now ~4,213 LOC; was ~4,420),
into NEW `optimus/renderer/call_tree_renderer.py` (~225 LOC). The 4
extracted functions only call each other plus a lazy
`optimus.analyzers.base.FRAMEWORK_APPS` import inside `_ct_is_user_frame`
— self-contained cluster as flagged in the README's coupling table.

### Implementation

- The new submodule carries a local copy of the tiny `_e` HTML-escape
  helper (4 lines) rather than importing `_internal._e`. Avoids a
  circular import: `_internal.py` re-imports the call-tree names so
  call sites resolve unchanged, and importing back from `_internal`
  would close the cycle.
- The standard `from optimus.renderer.call_tree_renderer import …`
  block lives at the top of `_internal.py` (right after the
  v0.10.0 visualization-module import block). Every name in the
  extracted cluster is re-imported — including the constants and
  the regex — so package-level `__init__.py`'s dir-walk re-export
  still surfaces them under the legacy `optimus.renderer.X` paths
  the unit suite uses (`test_call_tree_render.py`).
- Output HTML is byte-equivalent: the structural-snapshot canary
  (`test_renderer_structure_snapshot.py`) stays green without
  regenerating the golden fixture.

### Docs

- `optimus/renderer/README.md` — current-layout block updated;
  follow-up roadmap table marks `call_tree_renderer` as ✓ done in
  v0.12.8; 4 clusters remain (`line_drilldown`, `doc_event_renderer`,
  `finding_enrichment`, `render()` orchestrator).

### Unchanged

- The render path itself — every caller of `_render_call_tree_panel`
  / `_render_call_tree_node` (test code + the one call site in
  `_internal.py`'s `render` function) keeps working through the
  re-import shim.
- `optimus/tests/test_call_tree_render.py` — pure-pytest unit tests
  resolve the names via `renderer._render_call_tree_panel` etc.
  through the package `__init__.py` re-export; no change needed.

### Compatibility

No behaviour change. Pure refactor. Unit suite stays at 1818.
Structural snapshot stays byte-identical (no fixture update).

---

## [0.12.7] — 2026-05-25

**Integration test — `janitor.sweep_old_sessions` actually deletes.
Seventh and final row of the v0.11.0 deferred-tests table is now
ticked. The integration extraction roadmap is complete.**

The janitor's `sweep_old_sessions` (janitor.py:96-139) is the daily
cron that enforces the retention policy: Ready / Failed sessions older
than `DEFAULT_RETENTION_DAYS` (90, configurable via
`site_config.optimus_session_retention_days`) get hard-deleted. The
unit suite covers individual sweep functions in isolation but mocks
the deletion. It can't prove that the cron actually deletes the
DocType row + cascades to attached File rows, or that the
terminal-state filter is correct.

### Added

- **NEW `optimus/tests_integration/test_janitor_sweeps_actually_delete.py`**
  (~250 LOC, 4 tests). Each test seeds an Optimus Session with a
  controlled `started_at` + status, calls
  `janitor.sweep_old_sessions()`, and asserts the post-sweep state.
  `tearDown` tracks every UUID created so anything the sweep DIDN'T
  delete (negative controls) gets wiped:
  - `test_sweep_deletes_session_older_than_retention` — the canary.
    100-day-old Ready session → deleted. Without this, the Optimus
    Session table grows unbounded.
  - `test_sweep_keeps_session_within_retention` — negative control.
    30-day-old Ready session → kept. Catches over-aggressive
    deletion.
  - `test_sweep_keeps_active_sessions_regardless_of_age` —
    terminal-state contract. 100-day-old Analyzing session → kept.
    The daily sweep filters `status IN (Ready, Failed)` exclusively;
    non-terminal states are the 5-minute `sweep_stale_sessions`'s
    job.
  - `test_sweep_cascades_attached_file_deletion` — disk-hygiene
    contract. Deletes the session AND its `raw_report_file` File
    row. Orphan File rows would inflate disk usage forever even
    though the parent session is gone.
- Workflow line in `.github/workflows/integration.yml`.

### Per-test isolation

- Per-test unique `session_uuid` tracked in `self._uuids` list;
  tearDown wipes any UUID that survived the sweep (negative-control
  rows) plus their attached File rows.
- The synthetic File-row insertion clears `frappe.local.request`
  before insert so Frappe's `validate_file_extension` hits its
  no-request bypass — same pattern `analyze._save_report_file`
  uses for the same reason.

### Docs

- `optimus/tests_integration/README.md` — row 7 of the extraction
  roadmap ticked. **All 7 deferred-tests rows now complete.**

### Unchanged

- `optimus/janitor.py` — function under test stays as-is.
- All unit-suite janitor tests (`test_janitor.py`,
  `test_janitor_telemetry.py`, etc.) — they stay as the pure-pytest
  backstop.

### Compatibility

No behaviour change. Pure test addition. Integration-suite total:
34 → 38 tests. Unit suite stays at 1818.

### Milestone

The v0.11.0 deferred-tests roadmap is now fully complete:

1. ✓ v0.12.1 — `test_atomic_lua_merge_concurrent.py`
2. ✓ v0.12.2 — `test_telemetry_flush_doctype_sink.py`
3. ✓ v0.12.3 — `test_ai_privacy_exclusion_on_api.py`
4. ✓ v0.12.4 — `test_regenerate_reports_idempotent.py`
5. ✓ v0.12.5 — `test_phase2_tool_orphan_recovery.py`
6. ✓ v0.12.6 — `test_safe_report_self_contained_on_real_bench.py`
7. ✓ v0.12.7 — `test_janitor_sweeps_actually_delete.py`

Every high-impact integration scenario the v0.7.x architecture
review identified now has a real-bench canary.

---

## [0.12.6] — 2026-05-25

**Integration test — safe-report self-containment on the real bench.
Sixth row of the v0.11.0 deferred-tests table is now ticked.**

The safe-report HTML is the **dev-shop interchange format**: a
self-contained file an operator can email / attach / archive without
needing a live Frappe bench to view it. Per
`[[feedback_safe_report_self_contained]]`: "no CDN/remote fetches;
load-bearing offline guarantee with a canary acceptance test." The
unit suite (`test_report_a11y.py::test_report_is_self_contained_offline`)
covers this against the in-memory render. It can't cover the on-disk
file after Frappe's `file_manager` writes it.

### Added

- **NEW `optimus/tests_integration/test_safe_report_self_contained_on_real_bench.py`**
  (~200 LOC, 3 tests). Each test creates a minimal synthetic Optimus
  Session, calls `api.regenerate_reports(uuid)`, reads
  `raw_report_file` via the File doc, and asserts:
  - `test_on_disk_report_has_no_remote_resource_urls` — mirrors the
    unit-suite canary at the integration boundary: no `src=https?:`,
    no `<link href=https?:`, no `@import`, no `url(http`. Includes
    a positive sanity check (at least one human-facing anchor link
    exists) so the negative checks don't trivially pass on an empty
    page.
  - `test_on_disk_report_has_no_inline_or_external_javascript` —
    no `<script` tag in any form (the safe report is JS-free, which
    is part of why it's safe to open in an arbitrary browser).
  - `test_on_disk_report_does_not_reference_live_bench_asset_urls`
    — no `/assets/`, no `/files/`, no `/api/method/` in
    `src`/`href` positions. Bench-local asset references would
    render in a live-bench browser but break the moment the file is
    moved off-bench.
- Workflow line in `.github/workflows/integration.yml`.

### Per-test isolation

- Unique `session_uuid` per test; explicit File-row + Session-row
  cleanup in tearDown.
- `setUp` does the regenerate call so each test gets fresh on-disk
  HTML (independent of sibling-test side effects).

### Docs

- `optimus/tests_integration/README.md` — row 6 of the extraction
  roadmap ticked. 1 row remains
  (`test_janitor_sweeps_actually_delete.py`).

### Unchanged

- `optimus/api.py` `regenerate_reports` — endpoint under test stays
  as-is.
- `optimus/analyze.py` `_save_report_file` — file-write path
  unchanged.
- Unit-suite self-containment canaries
  (`test_report_a11y.py`, `test_lens_promo.py`, etc.) stay as the
  pure-pytest backstop.

### Compatibility

No behaviour change. Pure test addition. Integration-suite total:
31 → 34 tests. Unit suite stays at 1818.

---

## [0.12.5] — 2026-05-25

**Integration test — Phase-2 tool-2 orphan recovery. Fifth row of the
v0.11.0 deferred-tests table is now ticked.**

On Python 3.12+ line_profiler drives the process-global
`sys.monitoring` PROFILER_ID (tool 2). A pre-`fbf3179` failure mode
left tool 2's LINE events registered process-wide after a botched
teardown → every subsequent request in that worker line-traced → CPU
peg + frozen UI. The `fbf3179` fix added the worker-respawn recovery
path: `optimus._startup_probe_tool2()` reclaims tool 2 at app-import
if it's owned by `line_profiler` (i.e. a prior worker died
mid-Phase-2 and its state survived). The unit suite
(`test_line_profile_monitoring.py`) covers `release_monitoring_tool()`
at the function-call boundary. It can't cover the startup probe in a
real bench with `frappe.logger()` reachable and the v0.8.0 telemetry
path live.

### Added

- **NEW `optimus/tests_integration/test_phase2_tool_orphan_recovery.py`**
  (~210 LOC, 4 tests). Each test manipulates `sys.monitoring` tool 2
  state directly (via `use_tool_id` / `set_events` / `free_tool_id`),
  invokes `optimus._startup_probe_tool2()`, and asserts the
  post-probe state. `setUp` + `tearDown` hard-reset tool 2 to free
  so a leak from one test cannot poison the rest of the suite.
  - `test_probe_reclaims_leaked_line_profiler_tool_2_on_simulated_worker_respawn`
    — the canary. Register tool 2 as `"line_profiler"` with LINE
    events on, call probe, assert tool 2 is now unowned + events
    cleared. Without the probe, this leak would line-trace every
    later request → CPU peg + freeze.
  - `test_probe_is_noop_when_tool_2_is_already_free` — happy path.
    Probe does NOT grab the tool slot itself (catches a regression
    where the probe might accidentally register as owner).
  - `test_probe_warns_but_does_not_reclaim_non_line_profiler_owner`
    — boundary contract. Register tool 2 as
    `"third-party-debugger"`, call probe, assert ownership is
    PRESERVED. Without this constraint, the probe would silently
    break a third-party profiler / IDE debugger.
  - `test_probe_emits_no_telemetry_on_happy_path` — quiet-on-success
    contract. The probe runs at every worker import; emitting
    telemetry on the no-op path would flood
    `Optimus Telemetry Event` with worker-restart noise. Confirms
    `emit_failure` only fires when the probe itself raises (the
    error branch).
- Workflow line in `.github/workflows/integration.yml`.

### Per-test isolation

- **`setUp` + `tearDown` hard-reset tool 2** via
  `sys.monitoring.set_events(PID, 0)` + `free_tool_id(PID)` — same
  pattern as the unit suite's `_guarantee_no_leak_escapes` autouse
  fixture. A leaked tool from one test silently slows every later
  test in the suite, so this guard is non-negotiable.
- **`setUp` + `tearDown` wipe `Optimus Telemetry Event` rows for
  `event_name="startup_probe_tool2"`** so the no-telemetry happy-path
  assertion is tight.
- **Skips on Python < 3.12** via `pytest.mark.skipif(not _HAS_MON)`
  — `sys.monitoring` is the 3.12+ entry point that drives the whole
  pathway under test.

### Docs

- `optimus/tests_integration/README.md` — row 5 of the extraction
  roadmap ticked. 2 rows remain
  (`test_safe_report_self_contained_on_real_bench.py`,
  `test_janitor_sweeps_actually_delete.py`).

### Unchanged

- `optimus/__init__.py` `_startup_probe_tool2` — the function under
  test stays as-is.
- `optimus/line_profile/capture.py` `release_monitoring_tool` —
  unchanged.
- All v0.7.x unit-suite tool-2 tests
  (`optimus/tests/test_line_profile_monitoring.py`) — they stay as
  the pure-pytest backstop.

### Compatibility

No behaviour change. Pure test addition. Integration-suite total:
27 → 31 tests. Unit suite stays at 1818.

---

## [0.12.4] — 2026-05-25

**Integration test — `api.regenerate_reports` is byte-stable across
consecutive calls. Fourth row of the v0.11.0 deferred-tests table is
now ticked.**

`api.regenerate_reports(session_uuid)` (api.py:1121-1241) re-renders
the safe-report HTML from an already-analyzed session without
re-running the analyze pipeline. Its purpose is to let an operator
pick up a renderer / template upgrade on a historical session without
the cost of re-analysis. That makes it **load-bearing for upgrades**:
v0.7.0 template polish, v0.10.0 renderer split, every later round of
finding-card refinement — all shipped on the assumption that
operators could regenerate old sessions and see the new UI.

The pure-pytest unit test
(`optimus/tests/test_regenerate_reports_api.py`) does source
inspection only: whitelisted, takes session_uuid, doesn't enqueue
analyze, calls clear_cached_pdf, gates on permissions. It says
nothing about the output. What it can't prove:

* That two consecutive `regenerate_reports` calls produce
  **byte-identical** HTML. Future non-determinism (a fresh UUID, a
  dict-iteration order change, a `time.time()` snapshot) would
  silently start producing diff'd HTML — breaking `regenerate` as a
  way to roll forward and breaking any safe-report diffing workflow.
* That the endpoint actually attaches HTML to
  `Optimus Session.raw_report_file` and the URL resolves.
* That a session-data change produces a different HTML (the canary's
  complement; catches silent caching).
* That the documented "Allowed on Ready OR Failed sessions" claim
  holds.

This integration test fills that gap.

### Added

- **NEW `optimus/tests_integration/test_regenerate_reports_idempotent.py`**
  (~260 LOC, 4 tests). Patches
  `optimus.renderer._internal._now_iso` to a fixed string at the
  test-class level so the embedded "Generated at" stamp is
  deterministic (otherwise two consecutive renders would differ by
  their stamp alone). Each test uses a unique session_uuid +
  minimal synthetic Optimus Session (reqd fields only) + explicit
  cleanup of attached File rows in tearDown.
  - `test_two_consecutive_regenerates_produce_byte_identical_html`
    — the canary. `api.regenerate_reports(uuid)` twice, no changes
    in between; the two `file_doc.get_content()` results must be
    byte-identical. Catches future non-determinism in the renderer.
  - `test_regenerate_attaches_html_to_raw_report_file_and_url_resolves`
    — the side-effect contract: `raw_report_file` URL set,
    File row is private + attached_to_doctype="Optimus Session" +
    content starts with `<!DOCTYPE` or `<html`.
  - `test_regenerate_byte_diff_when_session_field_changes` — the
    canary's complement. Render once, snapshot. Mutate `title` via
    `frappe.db.set_value`. Render again, snapshot. The two HTMLs
    must DIFFER, and the new title must appear in HTML 2 but not
    HTML 1. Catches silent caching.
  - `test_regenerate_works_on_failed_status_session` — sets
    `status="Failed"`, calls regenerate, asserts it succeeds +
    attaches a fresh file. Validates the docstring claim that
    Failed sessions are explicitly allowed ("unblocks a demo when
    a render-time bug was fixed" — api.py:1147-1150).
- Workflow line in `.github/workflows/integration.yml` to run the new
  module against the bench-bootstrapped `test_site`.

### Per-test isolation

- **Unique `session_uuid` per test** so attached File rows scope
  cleanly.
- **`setUpClass` patches `optimus.renderer._internal._now_iso`** to
  a fixed timestamp via `mock.patch`. Module-attribute lookup at
  render time means the patch takes effect immediately for every
  render through the API endpoint.
- **`tearDown` explicit cleanup** — `frappe.db.delete("File",
  {attached_to_doctype: "Optimus Session", attached_to_name: name})`
  to wipe every File row regenerate created (one per call), then
  `frappe.delete_doc("Optimus Session", name, force=1)`.

### Discovery

- `api.regenerate_reports` has **no `status` gate** — any session
  status (Ready, Failed, Analyzing, etc.) re-renders. The docstring
  says "Allowed on Ready OR Failed" but the code doesn't enforce it.
  Out of scope for this PR; documented in the test that exercises
  the Failed branch.

### Docs

- `optimus/tests_integration/README.md` — row 4 of the extraction
  roadmap ticked. 3 rows remain
  (`test_phase2_tool_orphan_recovery.py`,
  `test_safe_report_self_contained_on_real_bench.py`,
  `test_janitor_sweeps_actually_delete.py`).

### Unchanged

- `optimus/api.py` — the endpoint under test stays as-is.
- `optimus/renderer/_internal.py` — `_now_iso` is patched per-test;
  production behaviour unchanged.
- `optimus/analyze.py` — `_render_and_attach_reports` /
  `_save_report_file` stay as-is.
- `optimus/report_context.py` — `build_report_context` stays as-is.
- All v0.7.0 unit-suite regenerate tests
  (`optimus/tests/test_regenerate_reports_api.py`) — they stay as
  the pure-pytest source-inspection backstop.

### Compatibility

No behaviour change. Pure test addition. Integration-suite total:
23 → 27 tests. Unit suite stays at 1818.

---

## [0.12.3] — 2026-05-25

**Integration test — `api.suggest_fix` honours
`ai_excluded_finding_types`. Third row of the v0.11.0 deferred-tests
table is now ticked.**

The v0.9.0 release (`043fa66`) shipped AI privacy hardening: the
`ai_excluded_finding_types` Settings field (Small Text, multi-line,
exact case-sensitive match) backed by `ai_fix.is_finding_type_excluded()`.
The unit-suite tests (`optimus/tests/test_ai_privacy.py`) cover the
parser, the helper, the inner `ai_fix.suggest_fix(finding)` refusal,
and a `requests.post`-never-called spy. They can't prove:

* That the **whitelisted endpoint** `api.suggest_fix(session_uuid,
  finding_ref, regenerate)` actually honours the exclusion before any
  AI dispatch — there's a separate refusal site at `api.py:1333-1347`
  with its own user-readable error message.
* That the live Optimus Settings cache invalidation (the doc's
  `on_update` deletes `redis_keys.settings_cache()`) propagates so the
  endpoint sees the operator's edit without a restart.
* That the v0.8.0 telemetry event `ai.fix_call_refused_by_exclusion`
  (severity `warning`, context `{finding_type: ...}`) actually lands
  in `tabOptimus Telemetry Event` after a refusal.
* That the exclusion is case-sensitive at the API boundary.

That gap is what this integration test fills.

### Added

- **NEW `optimus/tests_integration/test_ai_privacy_exclusion_on_api.py`**
  — 5 tests covering the v0.9.0 exclusion gate at the live API
  boundary against real MariaDB + the live Optimus Settings cache:
  - `test_excluded_finding_type_throws_with_clear_error_message` —
    `api.suggest_fix(session_uuid, "0")` on a "Slow Query" finding
    while "Slow Query" is in the exclusion list → raises
    `frappe.ValidationError` with a message that contains both
    "exclusion list" and "Optimus Settings" (verbatim substrings
    from the user-facing message at api.py:1343-1346).
  - `test_excluded_finding_type_emits_telemetry_refusal_event` —
    after the refusal raises, `telemetry.flush()` lands exactly one
    row in `Optimus Telemetry Event` with
    `event_name="ai.fix_call_refused_by_exclusion"`,
    `severity="warning"`, and `last_context` mentioning the
    refused finding type.
  - `test_exclusion_is_case_sensitive_at_api_boundary` — exclusion
    = "slow query" (lowercase), finding type = "Slow Query"
    (capitalised). With `ai_fix.suggest_fix` patched to a benign
    stub, the endpoint reaches the stub (gate doesn't fire on case
    mismatch) and returns its payload. Zero refusal telemetry rows
    afterwards.
  - `test_empty_exclusion_list_does_not_refuse` — exclusion = ""
    (empty Small Text). Endpoint reaches the stubbed AI dispatch
    and returns its payload. Zero refusal telemetry rows.
  - `test_settings_save_invalidates_cache_so_api_picks_up_new_exclusion`
    — start with exclusion = ""; first call succeeds (stub
    invoked). Save Optimus Settings with exclusion = "Slow Query"
    → the doc's `on_update` deletes `redis_keys.settings_cache()`.
    Second call → raises the exclusion error WITHOUT invoking
    `ai_fix.suggest_fix`. Confirms the cache-invalidation contract
    across the integration boundary.
- Workflow line in `.github/workflows/integration.yml` to run the new
  module against the bench-bootstrapped `test_site`.

### Per-test isolation

- **Unique `session_uuid` per test** (`test-{frappe.generate_hash
  (length=12)}`) so the synthesised Optimus Session doesn't collide
  with sibling tests in the same process.
- **`setUpClass` patches `ai_fix.is_available` to True** so the
  endpoint's early "AI fix suggestions aren't configured" guard
  doesn't preempt the exclusion gate. Production is unaffected — the
  patch is scoped to this TestCase only.
- **`setUp` snapshots `telemetry_enabled`, `telemetry_sink_doctype`,
  `ai_enabled`, `ai_suggest_findings`, `ai_excluded_finding_types`**
  from the live Settings doc, then forces the test env. `tearDown`
  restores whatever the original values were.
- **`tearDown` does `frappe.delete_doc("Optimus Session", name, force=1)`
  + `frappe.db.delete("Optimus Telemetry Event", {event_name:
  "ai.fix_call_refused_by_exclusion"})`** so the bench is left clean.
- **Non-refusal tests patch `optimus.ai_fix.suggest_fix`** to a
  benign stub (`unittest.mock.patch.object`) so the test never
  reaches a real LLM HTTP call.

### Synthetic Optimus Session

- The session is created directly via `frappe.get_doc({
  "doctype": "Optimus Session", "session_uuid": ..., "title": ...,
  "user": "Administrator", "status": "Ready", "started_at": now,
  "findings": [{"doctype": "Optimus Finding",
  "finding_type": "Slow Query", "severity": "high",
  "title": "test finding", "llm_fix_json": ""}]}).insert(...)`. We
  deliberately avoid the full `api.start → analyze` pipeline because
  the refusal gate at `api.py:1333` only needs `status="Ready"` +
  `findings[0].finding_type IN exclusion-list`; standing up a real
  workload would be extra surface area for no test value.

### Docs

- `optimus/tests_integration/README.md` — row 3 of the extraction
  roadmap ticked. 4 rows remain
  (`test_regenerate_reports_idempotent.py`,
  `test_phase2_tool_orphan_recovery.py`,
  `test_safe_report_self_contained_on_real_bench.py`,
  `test_janitor_sweeps_actually_delete.py`).

### Unchanged

- `optimus/api.py` — the endpoint under test stays exactly as-is.
- `optimus/ai_fix.py` — the helpers under test (`is_finding_type_excluded`,
  `is_available`, `AI_ELIGIBLE_FINDING_TYPES`) stay as-is.
- `optimus/settings.py` — resolve / cache logic unchanged.
- `Optimus Settings` / `Optimus Session` / `Optimus Finding` DocType
  JSONs — schemas are the v0.9.0 / pre-existing shapes.
- All v0.9.0 unit-suite ai-privacy tests
  (`optimus/tests/test_ai_privacy.py`) — they stay as the pure-pytest
  backstop.

### Compatibility

No behaviour change. Pure test addition. Integration-suite total:
18 → 23 tests. Unit suite stays at 1818.

---

## [0.12.2] — 2026-05-25

**Integration test — telemetry flush → Optimus Telemetry Event DocType
end-to-end. Second row of the v0.11.0 deferred-tests table is now
ticked.**

The v0.8.0 release (`5529cde`) shipped opt-in failure telemetry: a
bounded in-process deque, a scheduled `flush()`, the `Optimus Telemetry
Event` DocType, a JSONL sink, and signature-based dedup. The
unit-suite tests (`optimus/tests/test_telemetry.py`) cover the emit
hot path, the bounded deque, signature dedup, path/context scrub,
settings clamps, and `flush()` against a **mocked** `frappe.db.sql`.
What they can't prove: that `flush()` actually writes a row that lands
in `tabOptimus Telemetry Event` with the right shape, that the
DocType's deterministic `name` (sha1 of `event_name|signature`) enforces
the upsert correctly, that toggling `telemetry_enabled` via the live
Settings doc invalidates the cached config, and that the PII-scrubbed
traceback round-trips through MariaDB unchanged. That gap is what this
integration test fills.

### Added

- **NEW `optimus/tests_integration/test_telemetry_flush_doctype_sink.py`**
  — 5 tests covering the v0.8.0 telemetry pipeline against real
  MariaDB + the live Optimus Settings cache invalidation:
  - `test_emit_then_flush_persists_doctype_row` — canonical
    round-trip. Emit once, flush, assert one row exists with the right
    `event_name`, `severity`, `count=1`, populated `first_seen` /
    `last_seen` / `optimus_version` / `python_version` /
    `frappe_version`.
  - `test_repeated_emits_dedup_to_single_row_with_count` — 5 emits
    with the same `event_name` + `exc=None` (deterministic signature)
    → one DocType row with `count=5`. Validates the signature-dedup
    grouping at flush time.
  - `test_flush_no_op_when_master_disabled` — toggle
    `telemetry_enabled` OFF via `frappe.get_single('Optimus Settings')
    → save`, emit, flush. Returns 0; no DocType row created. Confirms
    the master gate is honoured at flush time AND that the
    `on_update` cache-invalidation hook fires so `flush()` sees the
    new value.
  - `test_persisted_row_has_scrubbed_traceback` — raise a real
    `ValueError`, emit with that exception, flush. The persisted
    `last_traceback` must contain `<bench>/apps/optimus/` (the
    scrubbed marker for the optimus frame) AND must NOT contain
    `/Users/` / `/home/` / `/private/` raw absolute prefixes.
    Locks in the PII-scrub round-trip through MariaDB.
  - `test_second_flush_increments_count_via_upsert` — emit 3 + flush,
    emit 4 + flush; assert one row with `count=7`. Confirms the
    v0.8.0 `INSERT … ON DUPLICATE KEY UPDATE count = count +
    VALUES(count)` SQL path executes (not a duplicate-key error or a
    second-row insert).
- Workflow line in `.github/workflows/integration.yml` to run the new
  module against the bench-bootstrapped `test_site`.

### Per-test isolation

- **Unique `event_name` prefix per test** (`test.{frappe.generate_hash
  (length=10)}`) so concurrent runs / sibling tests can't collide on
  the shared `Optimus Telemetry Event` table.
- **`setUp` snapshots `telemetry_enabled` + `telemetry_sink_doctype`**
  from the live Settings doc, then forces both ON. `tearDown`
  restores whatever the original values were — the bench is left in
  the same state it was found in.
- **`tearDown` does `frappe.db.delete('Optimus Telemetry Event',
  {event_name: like prefix%})`** because `_write_doctype` uses direct
  SQL that auto-commits past the `FrappeTestCase` per-test
  transaction.
- **`setUp` + `tearDown` both call `telemetry.drain_for_test()`** so
  emit-deque state can't bleed across tests in the same Python
  process.

### Docs

- `optimus/tests_integration/README.md` — row 2 of the extraction
  roadmap ticked. 5 rows remain
  (`test_ai_privacy_exclusion_on_api.py`,
  `test_regenerate_reports_idempotent.py`,
  `test_phase2_tool_orphan_recovery.py`,
  `test_safe_report_self_contained_on_real_bench.py`,
  `test_janitor_sweeps_actually_delete.py`).

### Unchanged

- `optimus/telemetry.py` — the function under test. The integration
  test is exactly that — testing, not editing.
- `Optimus Telemetry Event` DocType JSON — schema is the same v0.8.0
  shape; the test asserts the columns that release defined.
- All v0.8.0 unit-suite telemetry tests
  (`optimus/tests/test_telemetry.py`) — they stay as the pure-pytest
  backstop that runs on every PR.

### Compatibility

No behaviour change. Pure test addition. Integration-suite total: 13 →
18 tests. Unit suite stays at 1818 (the new file in
`tests_integration/` is invisible to the pytest collector).

---

## [0.12.1] — 2026-05-25

**Integration test — atomic Lua merge under multi-worker contention.
First row of the v0.11.0 deferred-tests table is now ticked.**

The v0.11.0 release shipped the real-bench integration harness with two
pilot tests; `optimus/tests_integration/README.md` enumerated seven
deferred follow-ups. This is the first of those: a real-Redis + real-Lua
+ real-threading test that proves the v0.7.x bg-tracking trilogy
(`a356f64` → `0e4a270` → `f30f44e`) is lossless under genuine
multi-worker contention. The unit-suite version
(`test_session_jobs.py::TestAtomicMergeJobMetaConcurrent`) silently
`pytest.skip`s when Redis/Lua aren't available — under pure pytest,
that's every run. The integration version is the first-class CI gate.

### Added

- **NEW `optimus/tests_integration/test_atomic_lua_merge_concurrent.py`**
  — 5 tests covering the v0.7.x trilogy's invariants under real Redis
  + Lua:
  - `test_recording_uuid_and_status_race_is_lossless` — the canonical
    regression test. 50 distinct job_ids × 2 racing threads per job
    (one writes `recording_uuid`, one writes `status`), released
    simultaneously via `threading.Barrier`. Asserts every job has
    BOTH fields after the dust settles.
  - `test_concurrent_distinct_job_ids_dont_clobber` — 20 threads
    write to 20 distinct hash fields simultaneously; validates the
    per-field cjson isolation inside the Lua script.
  - `test_setdefault_first_writer_wins` — 20 threads race
    `_SETDEFAULT_JOB_META_LUA` after a known first-writer's seed; the
    first value must survive.
  - `test_fallback_path_writes_when_lua_unavailable` — single-thread
    test (`frappe.local` is main-thread only). Monkey-patches
    `frappe.cache.eval` to raise; asserts `_atomic_merge_job_meta`
    still writes via the Python read-modify-write fallback.
  - `test_atomic_merge_does_not_raise_when_lua_unavailable` —
    defensive lock-in: the wrapper catches eval failures silently and
    the host code never sees the underlying error.
- Workflow line in `.github/workflows/integration.yml` to run the new
  module against the bench-bootstrapped `test_site`.
- README row in `optimus/tests_integration/README.md` ticked with a
  pointer to the new file.

### Engineering

- 5 new integration tests; integration suite total 8 → 13. Unit suite
  unchanged at 1818 passing (the unit-suite backstop at
  `test_session_jobs.py::TestAtomicMergeJobMetaConcurrent` continues
  to skip under pure pytest).
- Each test owns a per-test fixture session_uuid + purges the jobs
  hash in setUp/tearDown — independent of the autouse
  `cleanup_session` fixture (which handles Optimus Session DocType
  rows but not arbitrary Redis hashes the test creates directly).
- The 4 race-test methods pre-compute the prefixed Redis key in the
  main thread (the same technique the unit-suite version uses;
  `frappe.local.conf` isn't initialised in non-main threads).

### Deferred

The remaining 6 rows of the integration-test extraction roadmap stay
deferred — `test_telemetry_flush_doctype_sink`,
`test_ai_privacy_exclusion_on_api`,
`test_regenerate_reports_idempotent`,
`test_phase2_tool_orphan_recovery`,
`test_safe_report_self_contained_on_real_bench`,
`test_janitor_sweeps_actually_delete`. Each is a similar-sized
follow-up PR using the same harness pattern.

Version: 0.12.0 → 0.12.1.

---

## [0.12.0] — 2026-05-24

**Redis schema versioning foundation — closes the architecture review's
mid-term refactor list.**

Pre-v0.12.0, every Redis key Optimus writes had been evolving across
the v0.5 → v0.11 sweep (the v0.7.x bg-tracking trilogy added per-job
hashes; v0.8.0 added the telemetry buffer; v0.5.1 split combined
frontend metrics into separate XHR/vitals lists) and **nothing in the
codebase carried a version tag** — not the key names, not the value
envelopes, not a startup sentinel. The HMAC envelope used for
``profiler:tree:*`` was bare ``sig | pickle``; a future change to the
pyinstrument tree shape would silently corrupt reads with no detection
layer. Five modules also built keys via inline f-strings, scattering
the schema surface across the codebase.

This release establishes the foundation. It does NOT retroactively
wrap every existing Redis value in a versioned envelope (that would
break in-flight benches); it makes every FUTURE schema change safe by
construction.

### Added

- **NEW `optimus/redis_keys.py`** — single source of truth for every
  Redis key Optimus writes. ~22 public builder functions, one per key
  pattern. ``SCHEMA_VERSION = 1`` constant + a ``KEY_PATTERNS`` tuple
  the audit test cross-references against the doc.
- **NEW `optimus/redis_schema.py`** — versioned-value envelope helpers
  (``wrap_value`` / ``unwrap_value``) + schema-version sentinel write/
  read (``write_schema_sentinel`` / ``read_schema_sentinel``). The
  helpers are opt-in for new code; legacy un-wrapped values continue
  to read exactly as today. On drift detection, ``unwrap_value``
  emits a ``redis.schema_drift`` telemetry event (via the v0.8.0
  pipeline) so operators see the mismatch in
  ``Optimus Telemetry Event``.
- **NEW `docs/REDIS-SCHEMA.md`** — full schema documentation. Every
  key pattern × value shape × encoding × TTL × lifecycle, with a
  versioning contract section + a contributor checklist. The audit
  asserts this doc stays in lock-step with ``KEY_PATTERNS``.
- **NEW `optimus/tests/test_redis_audit.py`** — drift-protection
  canary in the v0.11.1 audit-test style. Two assertions: no inline
  f-string keys inside ``frappe.cache.*`` calls (orphans are listed
  with file:line on failure); ``redis_keys.KEY_PATTERNS`` equals the
  documented inventory. Plus 4 round-trip tests for the envelope +
  sentinel helpers.
- **NEW schema-version sentinel write** at app import in
  ``optimus/__init__.py`` (after the existing ``_startup_probe_tool2``
  / ``_try_install_capture_wraps`` patches). Best-effort; a Redis
  hiccup never breaks app load.

### Migrated

15 inline ``f"profiler:..."`` / ``f"optimus:..."`` keys across 6 files
now resolve through ``redis_keys.*`` builders:
``optimus/api.py`` (5), ``optimus/hooks_callbacks.py`` (3),
``optimus/analyze.py`` (5 + delete sites in ``_cleanup_redis``),
``optimus/settings.py`` (1), ``optimus/janitor.py`` (1),
``optimus/session.py`` (3 ``delete_session_state`` cleanup lines),
``optimus/optimus/doctype/optimus_settings/optimus_settings.py`` (1).
Behaviour-preserving — the strings the builders return are identical
to the f-strings they replace; no deployed Redis state shifts.

### Untouched

- The HMAC ``sign_blob`` / ``unsign_blob`` envelope — adding a version
  tag inside the signed envelope is a follow-up with its own
  migration story.
- ``session.py`` / ``line_profile/capture.py``'s pre-v0.12.0 internal
  helpers (``_active_key``, ``_meta_key``, etc.) — those callers don't
  go through ``frappe.cache.X(literal_key, ...)``; they pass the
  helper's return value, which is invisible to the audit.
- Every existing Redis value envelope — no wrapping in this PR. The
  ``wrap_value`` / ``unwrap_value`` helpers are reserved for the next
  schema-version bump.

### Deferred

- **Wrap every existing value in a versioned envelope** (per-key
  rollout in v0.13.0+).
- **Add a version tag inside the HMAC envelope** (worth its own PR
  with a migration path for in-flight signed blobs).
- **Migrate the jobs hash to JSON-only encoding** to drop the dual-
  encoding hazard documented in REDIS-SCHEMA.md § 5.
- **Janitor proactive purge on schema-version mismatch** — reactive
  cleanup-on-read is sufficient for the foundation.
- **Unify the legacy ``session._meta_key`` etc. into ``redis_keys``**
  — could be done now but doubles the diff size; cleaner as its own
  follow-up.

### Engineering

- 7 new tests in ``test_redis_audit.py``. Full suite **1818 passing
  + 1 skipped** (1811 → 1818). Ruff clean.
- Two existing wiring tests (``test_analyze_run_v5_wiring`` +
  ``test_correlation_header``) updated to grep for the new
  ``redis_keys.X(`` builder calls instead of the migrated inline
  f-strings.

Version: 0.11.1 → 0.12.0.

---

## [0.11.1] — 2026-05-24

**Telemetry instrumentation sweep — closes the v0.8.0 deferral.**

v0.8.0 (`5529cde`) shipped opt-in failure telemetry with 15 hand-picked
top-of-stack migration sites and an explicit deferral: *"migration of
remaining ~65 log_error sites — picked once we see the v0.8.0 signal
shape."* This release closes that deferral with a sweep of **72 additional
sites** across 8 files, plus a **drift-protection audit test** that
forever-after fails CI if a new `frappe.log_error(...)` call lands
without a matching `telemetry.emit_failure(...)` within the next 16
lines.

### Added

- **NEW `optimus/tests/test_telemetry_audit.py`** — the forever-after
  drift-protection canary. Walks every `.py` file under `optimus/`,
  asserts every `frappe.log_error(` line has a `telemetry.emit_failure(`
  call within 16 lines after. Excludes `tests/`, `tests_integration/`,
  `patches/`, `renderer/_internal.py` (deferred per the renderer-split
  roadmap), and `telemetry.py` itself (its sink-failure handler can't
  recurse into emit). Lists orphans with file:line on failure so a new
  contributor knows exactly where to add the emit. The audit is the
  v0.11.1 contract — from this PR forward, drift is mechanically caught.

### Code

- `optimus/api.py` — 16 new emit sites (`api.set_draining_window`,
  `api.scheduler_check`, `api.inline_analyze.mark_failed`,
  `api.inline_analyze.run`, `api.frontend_metrics.{xhr,vitals}_{rpush,ltrim}`,
  `api.regenerate_reports.{fetch,ai_backfill}`, `api.suggest_fix.persist`,
  `api.humanize_steps.fetch`, `api.refill_indexes.per_table`,
  `api.phase2.{force_stop_redis_cleanup,force_stop_parent_save,scheduler_check}`).
- `optimus/analyze.py` — 22 new emit sites
  (`analyze.{custom_hook.{not_callable,load_failed},singleflight_reenqueue,
  bg_job_wait_reenqueue,auto_arm_phase2,missing_session,analyzer_failed,
  ai_auto_suggest_outer,ai_index_suggest_outer,pyi_tree.{load_failed_both_paths,
  signature_mismatch,deserialize},sidecar.load_failed,bg_job.{persist_row,
  persist_batch},ai_auto_suggest,ai_backfill,ai_index_suggest,humanize_steps,
  render_raw_report,save_report_file,cleanup_session_state}`). Dynamic
  identifiers (analyzer_name, recording_uuid, filename, table) go into
  `context=`, not the event_name, so the DocType-level dedup groups all
  failures of the same class under one row.
- `optimus/janitor.py` — 14 new emit sites covering the 7 outer
  sweep wrappers + the 7 inner-loop sites (per-old-session deletes,
  enqueue_failed, stopping re-enqueue, phase-2 cleanup ×2, stuck phase-2
  ×2).
- `optimus/infra_capture.py` — 6 new emit sites
  (`infra_capture.{process_metrics,system_metrics,loadavg,db_metrics,
  redis_metrics,rq_metrics}`).
- `optimus/install.py` — 5 new emit sites
  (`install.after_install.{auto_role,tracked_apps_seed}`,
  `install.on_user_role_change`, `install.before_uninstall.capture`,
  `install.uninstall.cleanup`).
- `optimus/hooks_callbacks.py` — 5 sites missed by the v0.8.0 sweep
  (`after_request.{infra_end,header_injection,pyi_dump,sidecar_dump}`,
  `after_job.infra_end`).
- `optimus/analyzers/explain_flags.py` — 1 new emit site
  (`analyzers.explain_flags.row_parse`).
- `optimus/analyzers/index_suggestions.py` — 2 new emit sites
  (`analyzers.index_suggestions.{optimizer_failure,dropped}`).
- `optimus/line_profile/analyzer.py` — 1 new emit site
  (`phase2.rerender_failed`).

Every migrated site keeps its existing `frappe.log_error(...)` call
unchanged — the emit is **additive**, never a replacement. Bare
`except Exception:` blocks were converted to `except Exception as exc:`
to feed the exception into `emit_failure`.

### Event-name taxonomy (forever-after convention)

- **Module prefix**: `api.`, `analyze.`, `janitor.`, `infra_capture.`,
  `install.`, `analyzers.`, `phase2.`, `after_request.`, `after_job.`
- **Phase suffix** where the failure has a natural inner identity
  (e.g. `analyze.pyi_tree.deserialize`).
- **Dynamic identity in context, not event_name** — the per-analyzer
  failures all share `event_name="analyze.analyzer_failed"` with the
  analyzer name in `context["analyzer"]`. Keeps the operator-facing
  Optimus Telemetry Event list view scannable as instrumentation grows.

### Operator notes

- Defaults preserve existing behavior — telemetry stays opt-in
  (`telemetry_enabled` default OFF). This release adds instrumentation,
  not surveillance.
- High-rate loops (frontend XHR/vitals rpush, per-finding AI auto-
  suggest, per-pyi-tree deserialize, per-analyzer, per-bg-job, per-table
  index, janitor per-old-session deletes) compress to a handful of
  unique signatures at flush time via the v0.8.0 signature dedup — one
  DocType row per `(event_name, signature)` with `count=N`. The deque's
  `maxlen=500` provides backpressure if a single 10-minute window
  somehow produces > 500 distinct signatures.

### Deferred

- `optimus/renderer/_internal.py` — graceful-degradation paths inside
  the 60+ silent excepts. Picked up in a follow-up PR using the
  renderer-split extraction recipe in `optimus/renderer/README.md`. The
  allowlist entry in `test_telemetry_audit.py` documents the deferral.
- Event-name standardization for the v0.8.0-migrated sites — they're
  shipped + stable; no reason to churn.
- A Frappe report ranking telemetry events by recent count — operator UX,
  not blocked by this PR.

### Engineering

- 1 new test (`test_telemetry_audit.py::test_no_orphan_log_error_sites`,
  the drift-protection canary). Unit suite: 1810 → 1811 passing,
  1 skipped. Ruff clean.

---

## [0.11.0] — 2026-05-24

**Real-bench integration-test foundation — CI workflow + harness + two
pilot tests.**

The pre-v0.11.0 CI (`.github/workflows/tests.yml`) is a fast (~6 s)
pure-pytest pipeline using the Frappe stub in `optimus/tests/conftest.py`
— great for logic regressions, blind to the integration layer:
`before_request` / `after_request` / `before_job` / `after_job` hooks,
`scheduler_events`, the atomic Lua merge for bg-job tracking (the v0.7.x
trilogy's `TestAtomicMergeJobMetaConcurrent` test is explicitly skipped
in pure-pytest because it needs a real Redis), the recording lifecycle
end-to-end, the `bench install-app` path, `bench migrate` idempotence,
and the line_profiler tool-2 startup probe.

This release stands up the foundation: a parallel CI workflow that
provisions a real Frappe v16 bench in GitHub Actions and runs an
integration suite against it. Two pilot tests prove the pattern; the
five-seven follow-up scenarios identified by the architecture review
each become a small follow-up PR using this harness.

### Added

- **NEW `.github/workflows/integration.yml`** — provisions a Frappe v16
  bench against MariaDB 10.6 + two Redis service containers; runs the
  integration suite via `bench run-tests --app optimus --module …`.
  Triggers on PRs to `main`, push to `main`, scheduled daily at 04:00
  UTC, and manual dispatch. Job timeout 25 minutes (expected wall-clock
  ~10-15 minutes cold, ~6-8 minutes with pip + yarn caches warm). Logs
  for both the test runs + the bench's own logs are uploaded as the
  `integration-logs` artifact on failure (14-day retention). No secrets
  required — a fork's CI runs identically.
- **NEW `.github/helper/install.sh`** — ~50-line bash script following
  the established Frappe / ERPNext community pattern. Runs `bench init
  --frappe-branch version-16`, points it at the runner's service
  containers, symlinks the optimus checkout as `apps/optimus`, creates
  a `test_site`, installs optimus, runs `bench migrate`. Idempotent +
  reusable locally for spinning up a clean test bench.
- **NEW `optimus/tests_integration/`** — sibling directory to
  `optimus/tests/`. Tests subclass `frappe.tests.utils.FrappeTestCase`
  and use real `frappe.db` / `frappe.cache` / `frappe.get_doc` calls.
  The pure-pytest workflow never traverses this directory; the Frappe
  test runner never traverses `optimus/tests/`. Clean separation.
- **NEW `tests_integration/conftest.py`** — bench-aware fixtures:
  `test_site` (current site name), `cleanup_session` (autouse —
  hard-deletes leftover Optimus Session rows + clears the per-user
  Redis active-session pointer; defence-in-depth on top of the per-test
  transaction rollback), `seeded_session` (start → yield uuid → stop +
  wait for terminal status).
- **NEW `test_install_smoke.py`** — 4 tests: `Optimus User` role
  exists, all 8 Optimus DocTypes registered, Optimus Settings Single
  doc readable, `bench migrate` idempotent (re-runs without raising +
  no schema drift).
- **NEW `test_recording_lifecycle_e2e.py`** — 4 tests covering the
  canonical capture → analyze → render pipeline: `api.start` creates
  the DocType row + Redis pointer; `api.stop` clears the pointer +
  marks the session for analyze; the full lifecycle reaches a terminal
  state within 60 s + the report file is attached; session totals are
  populated post-analyze. This is the canonical regression canary for
  the integration layer.
- **NEW `tests_integration/README.md`** — harness documentation, the
  "no flakiness" rule (quarantine in 24 h, never retry-on-failure), and
  the seven-row extraction roadmap for follow-up PRs.

### Modified

- **`CONTRIBUTING.md`** — added an "Integration tests (real bench)"
  section with the local + CI commands and a pointer at the
  tests_integration README.

### Untouched (the point of the two-track design)

- `.github/workflows/tests.yml` — the pure-pytest workflow stays the
  fast-feedback loop. Both workflows run in parallel on PRs; branch
  protection gates merge on both.
- `optimus/tests/` — the 1810-test pure-pytest suite continues
  unchanged. The Frappe stub in its `conftest.py` doesn't apply to
  `tests_integration/` (sibling directory, never imported under
  `pytest optimus/tests/`).
- `pyproject.toml` — no new pip dependencies. `frappe-bench` is
  installed inside the bench helper script, not declared as a
  project dep.
- The renderer package, the telemetry module, the AI fix path — every
  v0.7.x → v0.10.0 piece stays exactly as-is. Integration tests
  exercise them via the live bench, but the code under test doesn't
  change.

### Engineering

- 8 new integration tests (4 in `test_install_smoke.py`, 4 in
  `test_recording_lifecycle_e2e.py`). Unit suite stays at 1810
  passing.

### Deferred (each is a follow-up PR using this harness)

- `test_atomic_lua_merge_concurrent.py` — un-skip the v0.7.x trilogy's
  concurrent Redis test.
- `test_telemetry_flush_doctype_sink.py` — emit → flush → assert
  DocType row.
- `test_ai_privacy_exclusion_on_api.py` — live `api.suggest_fix` with
  an excluded type.
- `test_regenerate_reports_idempotent.py` — render → regenerate → diff.
- `test_phase2_tool_orphan_recovery.py` — leak `sys.monitoring` tool 2
  + verify the startup probe reclaims.
- `test_safe_report_self_contained_on_real_bench.py` — the canary on a
  real bench's File-served HTML.
- `test_janitor_sweeps_actually_delete.py` — janitor cron + retention.
- Cross-version matrix (Frappe v15 + v16), coverage reporting from the
  integration suite, parallel test sharding — all out of scope for the
  foundation.

---

## [0.10.0] — 2026-05-24

**Renderer refactor — foundation PR splitting the 4,958-line monolith
into a package, with a structural-snapshot canary.**

`optimus/renderer.py` (4,958 lines, 86 top-level defs, one 812-line
`render()` orchestrator) was the app's single biggest maintainability
hazard. Touching one helper rippled through 15+ callers spread over
3,000+ lines of context; code review became "find the section" before
"review the change"; new contributors faced a steep on-ramp.

This release converts the file into a package and extracts the four
lowest-coupling clusters as a proof-of-concept of the extraction
recipe. The remaining five clusters stay in `_internal.py` and are
staged for follow-up PRs — see `optimus/renderer/README.md` for the
roadmap.

### Architecture

`optimus/renderer.py` → `optimus/renderer/` (package). A backward-compat
shim in `__init__.py` walks `dir(_internal)` and re-exports every
non-dunder name (including underscore-prefixed internals), so every
existing `from optimus.renderer import X` and `optimus.renderer.X` call
site continues to work unchanged — `analyze.py`, `api.py`, the test
suite, and any third-party fork stay on the contract.

### Extracted modules (~538 LOC moved)

- `optimus/renderer/syntax.py` — Pygments highlighting + diff-block
  wrapper. `_ensure_pygments`, `_highlight_python_block_cached`,
  `_highlight_python_snippet`, `_highlight_all_snippets`,
  `_highlight_diff_html`, plus the `_PRE_BLOCK_RE` / `_diff_line_class`
  / `_looks_like_diff` internals.
- `optimus/renderer/source.py` — source-file I/O + bounded LRU cache.
  `_BoundedFileCache`, `_path_within_bench`, `_resolve_source_path`,
  `_read_source_snippet`, `_read_source_window`,
  `_SNIPPET_TRUNCATE_CHARS`, `_FILE_CACHE_MAX_ENTRIES`.
- `optimus/renderer/visualization.py` — donut chart + hot-frames table +
  frame-name redaction. `build_donut_data`, `build_donut_svg`,
  `build_hot_frames_table`, `redact_frame_name`, plus the
  `_DONUT_COLORS` palette.
- `optimus/renderer/time_format.py` — duration + datetime formatting.
  `_format_duration_ms`, `_format_datetime_display`,
  `_get_server_timezone`.

### Added

- **NEW `test_renderer_structure_snapshot.py`** — the structural canary.
  Pre-v0.10.0 tests asserted *content* ("the string '50× hits' appears
  in the HTML") but never *structure*. A refactor that renamed
  `<div class="finding-card">` to `<section class="finding">` would
  have passed every existing test and quietly broken the (frozen)
  template's CSS. The new test renders a synthetic fixture through
  `render_raw()`, computes a structural fingerprint (section IDs + CSS
  class multiset + per-tag count), and compares against
  `optimus/tests/fixtures/renderer_structure.json`. Regenerate via
  `REGENERATE_RENDERER_SNAPSHOT=1 pytest`. 14 tests total, including
  enumerated public-API resolution checks for the 10 named symbols that
  matter to external callers.
- **NEW `optimus/renderer/README.md`** — the future-author roadmap: why
  the package exists, the 5-step extraction recipe, the structural
  snapshot's role, the public-API stability promise, and the 5-cluster
  follow-up table with coupling estimates.

### Code

- `git mv optimus/renderer.py → optimus/renderer/_internal.py` to
  preserve blame; the four extractions are smaller moves that follow
  function-level via `git log --follow -L`.
- `_internal.py`: 4,958 → 4,420 lines after the extractions. Each
  submodule is imported back at the top of `_internal.py` so the bulk
  of the file resolves all four clusters' names unchanged.
- Updated tests that referenced `optimus/renderer.py` as a known file
  path (5 test files, ~14 sites) to point at the new
  `optimus/renderer/_internal.py`.
- `test_app_priority_split.py` — one `patch.object(renderer, X)` call
  rewritten to `patch.object(_renderer_internal, X)` because the
  package re-export trick doesn't apply when `_internal.py`'s own call
  sites resolve names through its own globals.

### Deferred (follow-up PRs)

- `call_tree_renderer` (~240 LOC, weak coupling).
- `line_drilldown` (~840 LOC, internal coupling) — single biggest
  remaining chunk.
- `doc_event_renderer` (~300 LOC, moderate coupling).
- `finding_enrichment` (~380 LOC, HIGH coupling) — tightly coupled to
  `analyze.py`; defer until the surrounding modules are extracted.
- `render()` orchestrator (~812 LOC, core) — keep integrated; an
  orchestrator isn't a section.

### Engineering

- 14 new pure-pytest tests in `test_renderer_structure_snapshot.py`:
  fingerprint match, self-containment invariant, section minimum,
  public-API resolution (10 named symbols), and a circular-import
  defense. Full suite **1810 passing + 1 skipped** (1796 → 1810).

---

## [0.9.0] — 2026-05-24

**AI privacy hardening — Critical Risk #2 of the architecture review.**

When AI fix suggestions are enabled, Optimus sends source code, normalized
and raw SQL (including table/column names and EXPLAIN output), and action
labels to the configured LLM provider. The pre-v0.9.0 controls were the
master switch (`ai_enabled`), the batch toggle (`ai_auto_suggest`), and the
per-pathway hard-off toggles — fine-grained enough to disable AI entirely
but with no opt-out short of that for an operator who wanted to keep
specific *categories* of finding off the wire.

This release adds three pieces, all additive:

### Added

- **NEW `docs/AI-FIXING.md`** — definitive data-flow documentation. Per-
  pathway inventory tables (finding-fix, humanize-steps, index-suggestion,
  connectivity probe) listing every field that crosses the wire with
  typical and maximum sizes. What does *not* leave the host. Provider
  matrix. Three local-LLM recipes (ollama, LM Studio, vLLM) with starting
  timeout recommendations and first-token latency expectations. Threat
  model + note for dev shops receiving a profile.
- **`ai_excluded_finding_types`** — new field in `Optimus Settings → AI →
  Privacy & Operations`. Multi-line, `#` comments, exact-match
  case-sensitive. Listed types are skipped in both auto-suggest and
  on-demand calls — the payload is never built and no request leaves the
  host. Mirrors the parsing pattern of the existing `skip_request_paths` /
  `sensitive_sql_columns` skip-lists.
- **`ai_request_timeout_seconds`** — new field with default 60, clamped
  10–600. Replaces the hardcoded 60-second `_HTTP_TIMEOUT` (which was
  fatal for local-LLM cold starts: ollama / vLLM first-token latency on a
  CPU-only host or first model load routinely exceeds 60s). Hosted
  providers still answer in seconds; the default is behavior-preserving.

### Code

- `optimus/ai_fix.py`: new pure helper `is_finding_type_excluded(type)`;
  `_http_post` now reads the configured timeout via `_resolve_timeout_seconds`;
  `suggest_fix` short-circuits with `AiFixError("excluded by
  ai_excluded_finding_types")` before any payload is built.
- `optimus/analyze.py:_enrich_findings_with_ai_suggestions` filters
  excluded types out of the eligible list before the budget loop. Emits a
  single `ai.auto_suggest_skipped_by_exclusion` telemetry event per run
  when the filter actually drops findings, so the operator sees the
  exclusion taking effect in `Optimus Telemetry Event` (when v0.8.0
  telemetry is enabled).
- `optimus/api.py:suggest_fix` throws a clear "this type is on the
  exclusion list" message pointing at the setting; emits
  `ai.fix_call_refused_by_exclusion` telemetry per refusal.

### Operator notes

- Defaults preserve existing behavior — every operator who isn't on the
  exclusion list path or doesn't need a longer timeout gets the same
  outcome as v0.8.0.
- `bench migrate` applies the v0_9_0 patch automatically.
- Read `docs/AI-FIXING.md` before deciding what to add to the exclusion
  list. The doc enumerates exactly what each finding type's payload
  contains.
- For data residency, the `OpenAI-compatible` provider already supported
  local LLMs but the 60s timeout made it impractical; raise
  `ai_request_timeout_seconds` to 180 for cold-start tolerance.

### Engineering

- 12 new pure-pytest tests in `test_ai_privacy.py`: exclusion parsing,
  exclusion application, on-demand refusal, timeout resolution + clamp,
  settings floor, and a doc-staleness check that compares the doc's
  eligible-types list against `AI_ELIGIBLE_FINDING_TYPES` byte-for-line.

### Deferred

- Per-finding consent dialog (JS modal). The on-demand button click is
  already explicit consent; a modal layer adds UX surface without
  changing data flow. Worth revisiting only on customer feedback.
- Per-row `excluded_from_ai` Check on `Optimus Finding`. Type-level
  exclusion covers most needs; child-table-grid UI for one checkbox is
  not a great trade.
- Per-severity exclusion / per-app exclusion. The existing `ignored_apps`
  already drops findings entirely before AI sees them.

---

## [0.8.0] — 2026-05-24

**Opt-in failure telemetry — Critical Risk #4 of the architecture review.**

The app previously had ~79 `frappe.log_error` call sites and ~200+ silent
`try/except` blocks but no aggregation: every failure landed in Frappe's
global `Error Log` as a one-off row, so an operator couldn't tell
*"fails 4000×/day from THIS code path"* from *"happened once and never
again"*.

This release adds an opt-in counter that aggregates failures by signature
(event name + last 5 traceback frames). Default **OFF** — per the
self-hosted product thesis, telemetry never phones home. When enabled the
default sink is a local `Optimus Telemetry Event` DocType the operator
inspects in their own Desk; a JSONL file sink (`<bench>/logs/optimus_telemetry.jsonl`)
is opt-in for log aggregators. An HTTPS endpoint field exists in Settings
for forward compatibility but its transport implementation is deferred to
a follow-up release.

### Added

- `optimus/telemetry.py` — bounded in-process buffer (`maxlen=500`) +
  lock-free `emit_failure()` hot path + scheduled `flush()` every 10 minutes.
  PII scrub: file paths under bench rewrite to `<bench>/apps/<app>/file.py:LINE`,
  frames outside `optimus/` collapse to `<user_code>:LINE`, context dicts
  cap at 8 keys × 200 chars/value.
- New DocType `Optimus Telemetry Event` (event_name, signature, count,
  first_seen, last_seen, scrubbed traceback, version metadata). Unique key
  via deterministic row name from `(event_name, signature)` enables atomic
  `INSERT … ON DUPLICATE KEY UPDATE` so multi-worker flushes converge.
- 15 high-leverage migration sites in `__init__.py`, `hooks_callbacks.py`,
  `line_profile/hooks.py`, `analyze.py`, and `ai_fix.py`. Telemetry is
  **additive** to the existing `frappe.log_error` calls — Error Log
  visibility is unchanged; misconfigured telemetry cannot regress it.
- Optimus Settings: new Telemetry tab with five additive fields
  (master toggle, DocType sink, JSONL sink, endpoint URL, retention days).
  All depend_on the master toggle so the form stays clean when OFF.
- Janitor: `_sweep_old_telemetry()` runs in the existing daily cron,
  deletes rows older than the configured retention (default 30 days),
  capped at 100 deletions per run.
- Patch `v0_8_0.add_telemetry_fields` reloads the new DocType + the
  modified Settings deterministically during `bench migrate`.

### Operator notes

- Master toggle defaults OFF; the feature is invisible until enabled.
- When enabled, the first rows appear within one 10-minute flush window.
- Inspect via `Desk → Optimus Telemetry Event` (System Manager only;
  read+delete permissions, no create/write — writes happen exclusively
  via the flush worker).
- The HTTPS endpoint field accepts a URL today but does nothing — the
  transport will ship in a follow-up release without requiring a schema
  change.

### Engineering

- 33 new pure-pytest tests covering emit hot path, signature dedup, path
  scrub, context cap, flush sink wiring, settings clamp, and janitor
  retention. Suite total now 1777 passing + 1 skipped.

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

### Install

**Fresh deploy only** — no upgrade path from a pre-v0.7.0
`frappe_profiler` install is supported. The 0.7.0 rename moves the
package directory, renames every DocType / Role / realtime channel /
HTTP header / `frappe.local.profiler_*` attribute / `frappe.conf
.get("profiler_*")` key, and changes the `tabModule Def` row name —
an in-place upgrade would need a substantial migration patch set,
which is intentionally out of scope for the 0.7.0 release.

Install fresh:

```bash
bench get-app https://github.com/Aerele-RnD/optimus.git
bench --site <yoursite> install-app optimus
```

### Renamed in 0.7.0

  - **DocTypes** (6): `Profiler Action / Finding / Phase Two Run /
    Session / Settings / Tracked App` → `Optimus X`.
  - **Role**: `Profiler User` → `Optimus User`.
  - **Realtime channels**: `profiler_session_*` → `optimus_session_*`,
    `profiler_progress` → `optimus_progress`.
  - **HTTP correlation header**: `X-Profiler-Recording-Id` →
    `X-Optimus-Recording-Id`.
  - **`frappe.local.profiler_*` attributes** + **`frappe.conf.get
    ("profiler_*")` keys** → `optimus_*`.
  - **GitHub repo**: `Aerele-RnD/frappe_profiler` →
    `Aerele-RnD/optimus`. GitHub auto-redirects the old git URL.

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
