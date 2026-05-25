# Optimus Redis schema

This doc inventories every Redis key Optimus writes — the key pattern,
the value's shape and encoding, the TTL, the lifecycle — and the
versioning contract that lets value shapes evolve safely across
releases.

If you're adding a new Redis-backed feature, read § "For the next
contributor" at the bottom first; the four bullets there walk you
through the steps.

---

## 1. TL;DR

Optimus uses Redis for **transient per-session state** (the recording
lifecycle, the per-job tracking hash, the line-profile run state) and
for **cross-session caches** (settings, EXPLAIN results, the janitor
backlog counter). The Frappe-managed MariaDB DocTypes
(`Optimus Session`, `Optimus Finding`, etc.) are the durable record;
Redis is the working memory.

Every key Optimus writes comes from a builder in
`optimus/redis_keys.py` — a single source of truth. The audit test
`optimus/tests/test_redis_audit.py` enforces this: a `frappe.cache.*`
call site whose key isn't a `redis_keys.*` call fails CI.

Schema versions are tracked by **two** mechanisms working together:

1. A constant `optimus.redis_keys.SCHEMA_VERSION` (currently `1`).
   Bumped together with `optimus.redis_schema.SCHEMA_VERSION`.
2. A sentinel key `optimus:schema_version` written at app import.
   Future migration paths read it at boot to detect upgrades and run
   per-version migrators.

A versioned-value envelope (`optimus.redis_schema.wrap_value` /
`unwrap_value`) is the opt-in contract for **new** value-shape
changes. Today (v0.12.0) NO existing value is wrapped — the helpers
are reserved for the next time a shape changes.

---

## 2. Key inventory

Every key pattern, in canonical placeholder form. Add to this table
when you add a new key; the audit test asserts this table matches
`redis_keys.KEY_PATTERNS` exactly.

### 2.1 Per-session lifecycle (Phase 1)

| Pattern | Value | Encoding | TTL | Notes |
|---|---|---|---|---|
| `profiler:active:<user>` | string (session UUID) | raw | 10 min (`SESSION_TTL_SECONDS`) | Refreshed on every `register_recording`. Cleared by `api.stop`. |
| `profiler:session:<session_uuid>:meta` | **enveloped** dict (v0.12.21+) | Frappe pickle around `{"_v": 1, "data": {session_uuid, docname, user, label, started_at, capture_python_tree, optional cap_warning, optional draining_until}}` | none (explicit delete) | Migrated to the v0.12.0 envelope in v0.12.21. `get_session_meta` unwraps; pre-v0.12.21 bare-dict writes still resolve via legacy-detection branch. A defensive `isinstance(dict)` check on the unwrapped payload normalises corrupt non-dict values to `None`. |
| `profiler:session:<session_uuid>:recordings` | set | raw UUIDs | none (explicit delete) | SADD per recording. |
| `profiler:session:<session_uuid>:pending_jobs` | set | raw RQ job IDs | none (explicit delete) | SADD on enqueue, SREM on completion. |
| `profiler:session:<session_uuid>:jobs` | hash → JSON | **raw JSON** (Lua-written via `_raw_redis`) | none (explicit delete) | See § 5 "Dual-encoding hazard". |

### 2.2 Per-recording artefacts

| Pattern | Value | Encoding | TTL | Notes |
|---|---|---|---|---|
| `profiler:tree:<recording_uuid>` | bytes | **HMAC-signed pickle** | `SESSION_TTL_SECONDS` | 32-byte HMAC-SHA256 prefix + pickled pyinstrument `Profiler.last_session`. See § 6 "HMAC envelope". |
| `profiler:sidecar:<recording_uuid>` | list[dict] | Frappe pickle | `SESSION_TTL_SECONDS` | Argument-log entries; optional `{"_truncated": True}` tail marker. |
| `profiler:infra:<recording_uuid>` | dict | Frappe pickle | `SESSION_TTL_SECONDS` | CPU / RAM / DB / RQ delta. |

### 2.3 Frontend metrics (v0.5.0+ split lists)

| Pattern | Value | Encoding | TTL | Notes |
|---|---|---|---|---|
| `profiler:frontend:<session_uuid>:xhr` | list | Frappe pickle (entries) | `SESSION_TTL_SECONDS` (set via `expire_key`) | LTRIM'd to 1000 entries. |
| `profiler:frontend:<session_uuid>:vitals` | list | Frappe pickle (entries) | `SESSION_TTL_SECONDS` | LTRIM'd to 200 entries. |
| `profiler:frontend:<session_uuid>` | dict (legacy) | Frappe pickle | none | Pre-v0.5.1 combined shape. No code writes this any more; `delete_session_state` cleans it as a safety net for in-flight benches. |

### 2.4 Phase-2 line-profile run state

| Pattern | Value | Encoding | TTL | Notes |
|---|---|---|---|---|
| `profiler:lp:active:<user>` | string (run UUID) | raw | `SESSION_TTL_SECONDS` | Mutually exclusive with `profiler:active:<user>` at the API level. |
| `profiler:lp:<run_uuid>:picks` | hash | Frappe pickle | none (cleanup_run deletes) | Dotted function paths → metadata. |
| `profiler:lp:<run_uuid>:source` | hash | Frappe pickle | none (cleanup_run deletes) | Dotted paths → `[{lineno, content}]`. |
| `profiler:lp:<run_uuid>:samples` | list | Frappe pickle | none (cleanup_run deletes) | Per-line wall_ms samples. |
| `profiler:lp:budget_hit:<run_uuid>` | string ("1") | raw | 3600s | Sample-budget exceeded flag. |

### 2.5 Cross-session / app-level

| Pattern | Value | Encoding | TTL | Notes |
|---|---|---|---|---|
| `profiler:onboarding_seen:<user>` | **enveloped** string (v0.12.13+) | Frappe pickle around `{"_v": 1, "data": "1"}` | 1 year (`ONBOARDING_CACHE_TTL_SECONDS`) | Dismissed-toast marker. Migrated to the v0.12.0 envelope in v0.12.13. The reader (`check_onboarding_seen`) uses `unwrap_value` so legacy bare-string writes from pre-v0.12.13 still resolve. |
| `profiler:explain:<cache_key>` | **enveloped** list[dict] (v0.12.17+) | Frappe pickle around `{"_v": 1, "data": [<row>, ...]}` | TTL via `optimus_explain_cache_ttl_seconds` site_config (default 3600s) | EXPLAIN result hash; persists across sessions. Migrated to the v0.12.0 envelope in v0.12.17. Reads accept legacy bare-list shape for backward compat with pre-v0.12.17 writers. |
| `optimus:analyze:inflight` | string (heartbeat token) | raw | 300s (`_SINGLEFLIGHT_TTL_SECONDS`) | Single-flight guard; heartbeated by the live analyze; auto-clears on worker death. |
| `optimus:retention_backlog` | **enveloped** integer (v0.12.13+) | Frappe pickle around `{"_v": 1, "data": <int>}` | 3600s | Janitor backlog counter when daily sweep hits its per-run cap. Migrated to the v0.12.0 envelope in v0.12.13. Write-only inside the app — operator dashboards reading directly from Redis see the envelope shape. |
| `optimus_settings_cached` | **enveloped** dataclass dict (v0.12.11+) | Frappe pickle around `{"_v": 1, "data": {...}}` | none (invalidated by DocType `on_update`) | Pre-prefix legacy name; kept as-is to avoid a one-shot cache miss on upgrade. **First value migrated to the v0.12.0 versioned envelope (v0.12.11).** Reads still accept legacy bare-dict shape for backward compat with pre-v0.12.11 writers. |
| `optimus:schema_version` | integer | raw | none | v0.12.0+ sentinel. Written at app import. See § 4. |

---

## 3. The versioning contract

`SCHEMA_VERSION` is **a single integer** that captures the wire shape
of every value Optimus writes. v0.12.0 baseline is `1`.

### When to bump

Bump `SCHEMA_VERSION` (in both `optimus/redis_keys.py` and
`optimus/redis_schema.py` — they must stay in lock-step) when ANY of:

* A field is added or removed from a persisted dict (e.g. you add a
  `capture_phase2_too` flag to `session_meta`).
* The encoding of a list / hash changes (e.g. you switch the jobs
  hash from JSON to msgpack).
* The HMAC envelope contract for `profiler:tree:*` changes.
* The semantics of a TTL change in a way that affects readers.

Renaming a key, adding a NEW key, or changing only the helper's
build logic without changing the resulting string does NOT bump
`SCHEMA_VERSION` — those are non-breaking organisational changes.

### How to wrap a new value shape

When the new code writes a value whose shape is the breaking change,
wrap the payload via `optimus.redis_schema.wrap_value` BEFORE handing
it to `frappe.cache.set_value`:

```python
from optimus import redis_keys, redis_schema

frappe.cache.set_value(
    redis_keys.session_meta(uuid),
    redis_schema.wrap_value({"session_uuid": uuid, "docname": …}),
)
```

The wrapper produces `{"_v": SCHEMA_VERSION, "data": payload}`.

### How to read a (possibly-wrapped) value

On the read side, use `unwrap_value`:

```python
raw = frappe.cache.get_value(redis_keys.session_meta(uuid))
payload, observed_version = redis_schema.unwrap_value(raw, default={})
if observed_version is None:
    # Legacy un-wrapped value — payload is the bare dict (or whatever
    # the previous shape was). Code should still work on the old shape
    # OR have a migration path.
    ...
elif observed_version == redis_schema.SCHEMA_VERSION:
    # Current shape; use payload directly.
    ...
else:
    # `unwrap_value` already emitted a `redis.schema_drift` telemetry
    # event; payload is the caller's `default`. Future code can detect
    # and migrate; today the caller treats it as missing.
    ...
```

### What the unwrap helper does NOT do

* It doesn't migrate the value inline — each value's shape has its
  own migration rules, and coupling that into one helper would create
  a god-function. The contract is: when drift is detected, the helper
  emits telemetry + returns the default. Future code can read the
  legacy form via a per-key migrator.
* It doesn't rewrite the value on read. A future enhancement could
  add a "rewrite on first read" path; for now, in-flight values stay
  in their original shape until naturally expired or explicitly
  deleted.

---

## 4. The schema-version sentinel

At app import (in `optimus/__init__.py` after the existing startup
patches), `optimus.redis_schema.write_schema_sentinel()` writes the
current `SCHEMA_VERSION` to the `optimus:schema_version` key. Best-
effort: a Redis hiccup must never break app load.

On a fresh bench, the first boot writes the sentinel.

On an upgrade from a pre-v0.12.0 release, `read_schema_sentinel()`
returns `None` for the first boot (the key didn't exist before
v0.12.0); the write sets it to `1`. Future migration code can detect
this transition and run any one-shot migrators.

Today (v0.12.0) the sentinel is only **written**. No read path
consumes it. The next time a real schema change ships, a startup
hook will compare the sentinel against `SCHEMA_VERSION` and dispatch
migrators.

---

## 5. TTL discipline

Most keys carry an explicit TTL via `frappe.cache.set_value(..., expires_in_sec=N)` or `expire_key()`. The exceptions:

* `profiler:session:<uuid>:meta` / `:recordings` / `:pending_jobs` / `:jobs` — explicit delete by `delete_session_state` on analyze completion. If analyze crashes before that runs, the janitor's `sweep_orphan_redis_state` daily cron picks up the orphans.
* `profiler:lp:<uuid>:picks` / `:source` / `:samples` — explicit delete by `cleanup_run`. If a worker dies mid-run, the janitor's `sweep_stale_phase2_runs` 5-minute cron picks up the orphans.
* `profiler:explain:<cache_key>` — persistent read-through cache. Small per-entry; not worth a TTL.
* `optimus_settings_cached` — invalidated by the Optimus Settings DocType's `on_update` (Frappe's standard cache invalidation pattern, not a TTL).

The `profiler:onboarding_seen:<user>` 1-year TTL is the policy lever; bump or shorten via `ONBOARDING_CACHE_TTL_SECONDS` in `api.py` if the user-research story changes.

---

## 6. The dual-encoding hazard

`profiler:session:<uuid>:jobs` is the one hash where the encoding is **deliberately raw JSON**, not Frappe's pickle wrapping. The v0.7.x trilogy (`a356f64` → `0e4a270` → `f30f44e`) shipped an atomic Lua script `_MERGE_JOB_META_LUA` that does HGET → cjson.decode → merge → cjson.encode → HSET in a single server-side step. Lua can't replicate Python's pickle, so the values are JSON bytes — and they're read/written through `_raw_redis()` in `session.py`, which bypasses Frappe's pickle wrapper.

**The hazard**: if any future code does `frappe.cache.hset(jobs_key, job_id, …)` directly, Frappe's `RedisWrapper.hset` pickles the value. The hash becomes heterogeneous (some JSON-encoded, some pickled), and `hgetall` returns garbage on the next read.

**The rule**: every read/write to `profiler:session:<uuid>:jobs` MUST go through `session._raw_redis()` (or the Lua script). The `_atomic_merge_job_meta` / `_read_job` / `_write_job` helpers already do this. Don't add a new write path.

A v0.13.0+ PR can eliminate this hazard by removing the Frappe-pickle fallback entirely (every write goes through Lua), but that's its own deferred work.

---

## 7. The HMAC envelope

`profiler:tree:<recording_uuid>` is the one place Optimus writes signed pickle bytes. The envelope is:

```
[ 32-byte HMAC-SHA256 signature ][ pickle.dumps(pyinstrument.Profiler.last_session) ]
```

The signature is derived from `frappe.conf.encryption_key`. See `optimus.session.sign_blob` / `unsign_blob`. The read side (in `analyze._fetch_recordings`) has a two-attempt fallback for **secret drift** (an unsigned-pickle path for when the key got rotated mid-session).

**Known limitation**: the envelope itself carries no version tag. If the pyinstrument `Profiler.last_session` shape changes (fields added/renamed), readers will unpickle the bytes into the old shape and code accessing the new fields will crash.

**Future migration path**: prefix the envelope with a version byte (`v<sig><pickle>`); the reader detects the byte and switches between v1/v2 unpickle paths. Out of scope for v0.12.0; the sentinel-key infrastructure landed here is the foundation for that PR.

---

## 8. For the next contributor

Adding a Redis-backed feature? Four steps:

1. **Add a builder to `optimus/redis_keys.py`.**
   - Pick a name (e.g. `feature_state(uuid)`).
   - Pattern: `def feature_state(uuid: str) -> str: return f"profiler:feature:{uuid}"`.
   - Add a docstring (1-3 sentences) explaining the value shape, encoding, TTL, lifecycle.
   - Append the placeholder pattern to `KEY_PATTERNS` (e.g. `"profiler:feature:<uuid>"`).

2. **Document the key in § 2 above.**
   - One row in the right subsection's table.
   - The audit test `test_redis_keys_match_documented_schema` will fail if you forget; it's a one-line edit.

3. **Decide the TTL.**
   - Per-session/per-recording artefacts: `SESSION_TTL_SECONDS` + delete in `delete_session_state` / `cleanup_run`.
   - Cross-session caches: an explicit `expires_in_sec` or a manual invalidation hook.
   - If neither applies, document the policy here.

4. **If the value SHAPE is version-controlled**, wrap it via `optimus.redis_schema.wrap_value` on write and unwrap via `unwrap_value` on read. Most new keys don't need this — they're either simple strings, hash-of-IDs, or write-once lists. Only wrap when the shape might evolve.

The audit test, the schema sentinel, the version-bump rule together form the safety net — but the discipline of routing keys through `redis_keys.py` is what makes them all work.
