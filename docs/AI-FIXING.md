# AI Fix Suggestions — data flow & privacy

This document inventories **exactly** what data leaves your host when Optimus's AI fix suggestion feature is enabled, where it goes, and how to keep everything on-box with a local LLM. It's written for operators making the consent decision and for reviewers (compliance / security / a dev shop receiving a profile) who need to audit the wire.

If you're picking up Optimus for the first time: every AI feature here is **off by default**. The rest of this doc only matters once an operator explicitly turns one on.

---

## 1. TL;DR — default OFF

Optimus ships with `ai_enabled = 0` and `ai_auto_suggest = 0` in `Optimus Settings`. No request leaves your host until a System Manager flips the master toggle AND configures a provider (or points at a local LLM). With AI off:

- The analyze pipeline runs to completion as it always did.
- Findings are rendered with the profiler's own deterministic fix hints.
- Nothing is sent to any LLM endpoint, hosted or otherwise.

When AI is enabled, three knobs gate every outbound call:

| Knob (Optimus Settings → AI) | Default | What it does |
|---|---|---|
| `ai_enabled` | OFF | Master gate. OFF → zero LLM calls ever. |
| `ai_auto_suggest` | OFF | Batch mode. OFF → AI runs only when the operator clicks **Suggest a fix (AI)** on one finding. ON → analyze.run sends the top-N eligible findings during the background analyze pass. |
| `ai_excluded_finding_types` | empty | One finding type per line (v0.9.0+). Listed types are skipped in **both** auto-suggest and on-demand — the request body is never built and never sent. |

The combination of `ai_enabled=OFF`, on-demand-only (`ai_auto_suggest=OFF`), and the exclusion list gives the operator three independent axes of consent: feature-level (the master), event-level (the click), and type-level (the exclusion).

---

## 2. What data leaves the host

There are four outbound request shapes. Each table lists every distinct field that crosses the network boundary, the typical size, and the source.

### 2.1 Finding fix suggestion (`/v1/messages` or `/chat/completions`)

Triggered by **Suggest a fix (AI)** (on-demand) or auto-suggest at analyze time. Built by `ai_fix._build_messages(finding)` in `optimus/ai_fix.py`.

**System prompt** (static template, ~2.3 KB): Frappe / ERPNext idiom rules, source verbatim-copy discipline, SQL-equivalence rules, metadata-column guardrails, output-format spec (Markdown: Diagnosis / Fix / Why it works / Verify). Static text; identical across every call for a given finding type.

**User message** (per-finding, ~2–8 KB typical, **18 KB hard cap**):

| Field | Source | Typical size | Notes |
|---|---|---|---|
| `finding_type` | Optimus Finding | ~15 chars | One of the 10 eligible types (§ 5). |
| `severity` | Optimus Finding | 4–6 chars | High / Medium / Low. |
| `title` | Optimus Finding | 60–200 chars | Profiler-generated label (e.g. "`frappe.get_doc` on line 12 loops 18×"). |
| `customer_description` | Optimus Finding | 0–500 chars | Optional user-facing description. |
| `estimated_impact_ms` | Optimus Finding | ~5 chars | Profiler's impact estimate. |
| `affected_count` | Optimus Finding | ~3 chars | Occurrence count. |
| `callsite.filename` | technical_detail_json | 40–80 chars | Relative path under your bench, e.g. `apps/myapp/myapp/forms/invoice.py`. |
| `callsite.lineno` | technical_detail_json | 3–5 chars | Line number. |
| `callsite.function` | technical_detail_json | 20–60 chars | Function name from your code. |
| **`source_window`** | `renderer._read_source_window` | **1–2 KB typical** | Up to 80 lines of your source: 24 before / 24 after the callsite, capped at `_MAX_SOURCE_WINDOW_LINES`. Verbatim — including comments, strings, variable names. **This is the largest field in a typical request.** |
| `phase2_hotline` | Phase 2 line-profile | 100–400 chars | When available, the hottest single line from line-profiling (line number, content, total ms, hit count). |
| `technical_detail.function` | technical_detail_json | 30–80 chars | Hot function name (for Slow-Hot-Path-type findings). |
| `technical_detail.cumulative_ms` | technical_detail_json | ~5 chars | Time in that function. |
| `technical_detail.action_wall_time_ms` | technical_detail_json | ~5 chars | Action's total wall time. |
| `technical_detail.normalized_query` | technical_detail_json | 100–2400 chars | **Capped 2400.** Normalized SQL — table names, column names, WHERE clause structure preserved; literals replaced by `?` (then redacted again by `optimus.redaction` if they match sensitive column names). |
| `technical_detail.suggested_ddl` | technical_detail_json | 50–300 chars | Profiler's heuristic DDL pick, e.g. `ALTER TABLE tabBOMItem ADD INDEX ...`. |
| `technical_detail.explain_row` | technical_detail_json | 100–800 chars | `EXPLAIN` output, capped — type / rows / key / Extra etc. |
| `technical_detail.fix_hint` | technical_detail_json | 50–200 chars | Profiler's deterministic suggestion. |
| `technical_detail.validation_note` | technical_detail_json | 0–300 chars | Caveats. |
| **`technical_detail.example_queries`** | live recording (Redis) | **0–4800 chars** | Up to 2 of the slowest **raw** SQL queries from this action's recording, each capped at 2400 chars. These are real production queries — table names, column names, WHERE values are preserved (after `password = '…'`-style literal redaction at capture time, see `optimus.redaction`). |

Total user content is truncated to `_MAX_USER_CONTENT_CHARS = 18000` characters before sending (`ai_fix._truncate`).

### 2.2 Humanize "Steps to Reproduce" (`/v1/messages` or `/chat/completions`)

Triggered by `ai_humanize_steps`. Built by `_build_steps_messages` in `optimus/ai_fix.py`.

**System prompt** (static, ~2.9 KB): ERPNext workflow knowledge, Frappe API decoding rules, collapse rules, output spec (ordered Markdown list + one-sentence summary).

**User message** (per-session, ~1–8 KB, **8 KB hard cap**):

| Field per action | Source | Typical size | Notes |
|---|---|---|---|
| `label` | `per_action.humanized_label(recording)` | 20–100 chars | E.g. "Save Sales Order SO-0001". |
| `cmd` | recording.cmd | 20–80 chars | E.g. "frappe.desk.form.save.savedocs". |
| `path` | recording.path | 30–100 chars | URL path, when applicable. |
| `method` | recording.method | 4–8 chars | HTTP verb. |
| `doctype` | extracted from form_dict | 20–60 chars | E.g. "Sales Invoice". |
| `duration_ms` | recording.duration | ~5 chars | Wall time. |

Up to `_MAX_STEPS_ACTIONS = 60` actions; total truncated to `_MAX_STEPS_USER_CHARS = 8000` chars. `_is_reproducer_noise` pre-filters polling, form-load, and asset requests.

### 2.3 Index suggestion (`/v1/messages` or `/chat/completions`)

Triggered by **Suggest an index (AI)** (on-demand) or auto-suggest. Built by `_build_index_messages` in `optimus/ai_fix.py`.

**System prompt** (static, ~1.2 KB): DBA-perspective index design rules, Frappe metadata-column guardrails, write-hot-table warnings.

**User message** (per-table, ~2–10 KB, **10 KB hard cap**):

| Field | Source | Typical size | Notes |
|---|---|---|---|
| `table` | table_breakdown | 15–50 chars | E.g. `tabSales Invoice Item`. |
| `doctype` | table_breakdown | 20–60 chars | DocType label. |
| `read_count` / `write_count` | table_breakdown | ~3–4 chars each | Session counts. |
| `is_write_hot` | table_breakdown | 4–5 chars | Boolean. |
| `recommended_index` | table_breakdown | 50–200 chars | Profiler's heuristic pick (columns + together_count). |
| `candidates` | table_breakdown | 100–400 chars | Each candidate column + sources + hit count. |
| `framework_cols_filtered` | table_breakdown | 30–150 chars | Frappe metadata columns touched (the LLM is told NOT to index these). |
| `existing_indexes` | `SHOW INDEX` | 100–600 chars | Name / columns / unique flag per index already on the table. |
| **`sample_queries`** | live recording (Redis) | **0–9600 chars** | Up to 4 distinct normalized SELECT queries from the session that touched this table. Each capped 2400 chars. |

### 2.4 Connectivity probe (Optimus Settings → AI → "Test connection" button)

Smallest payload (`ai_fix.test_connection`). System: ~80 chars. User content: ~40 chars (`Reply with exactly: OK`). Total request ~150 bytes. Used only to verify the provider URL + key.

---

## 3. What does NOT leave the host

These items are **never** sent in any AI request body:

- **Sensitive SQL literals.** `password = '...'`, `api_key = '...'`, `token = '...'`, and the 12 other patterns in `optimus.redaction.DEFAULT_SENSITIVE_SQL_COLUMNS` are replaced with `<REDACTED>` at capture time, so even the `example_queries` field sees the redacted form. Operators can extend this list via `sensitive_sql_columns` in Optimus Settings.
- **HTTP form bodies.** `form_dict` keys matching `password` / `api_key` / `token` / etc. are redacted at capture (`optimus.redaction.DEFAULT_SENSITIVE_KEYS`), and form bodies themselves are never included in AI payloads — only summary fields like `cmd` / `doctype` reach the humanize-steps path.
- **Your API keys.** The provider API key is read from a Password field decrypted on demand (`frappe.utils.password.get_decrypted_password`). It is sent **only** in HTTP headers (`x-api-key` or `Authorization: Bearer …`), never echoed in the prompt, never logged, never returned to the client. The OpenAI-compatible provider with `needs_key=False` (local endpoints) omits the header entirely.
- **Recording UUIDs.** Internal Redis keys.
- **Your full DB schema.** Only tables observed in this profile's recordings are mentioned by name.
- **Other sessions / users / findings.** Each LLM call is one-shot per finding (or per table / per session). No cross-session context.
- **The signed pickle blobs in Redis.** These hold the raw recording state mid-pipeline; the AI payload reads only finalized analyzer output.

---

## 4. Where the request goes

`Optimus Settings → AI → Provider`:

| Provider | Protocol | Default base URL | Key needed? | Notes |
|---|---|---|---|---|
| `Anthropic` | Messages | `https://api.anthropic.com` | Yes | Default model: `claude-sonnet-4-6`. |
| `OpenAI` | Chat completions | `https://api.openai.com/v1` | Yes | Default model: `gpt-4.1-mini`. |
| `Kimi (Moonshot)` | Chat completions | `https://api.moonshot.ai/v1` | Yes | Default model: `kimi-k2-0905-preview`. |
| `Aerele` | Chat completions | `https://api.aerele.in/optimus/v1` | Yes | Managed pay-as-you-go service. See § 10. |
| `OpenAI-compatible` | Chat completions | (you set it) | No (configurable) | Use this for local LLMs and any other OpenAI-shaped server. |

`ai_base_url`, `ai_model`, and `ai_api_key` (Password) all override the provider defaults. The HTTP timeout is `ai_request_timeout_seconds` (v0.9.0+, default 60s, clamped 10–600s).

For an explicit data-residency choice, use `OpenAI-compatible` pointed at a process you run yourself — see § 6.

---

## 5. Eligible finding types

These ten finding types are the only ones for which Optimus builds an AI payload. All other types (Slow Hot Path, Hook Bottleneck, Repeated Hot Frame, infra / frontend findings, etc.) are skipped — they don't carry enough code/SQL context for the LLM to add value over the profiler's own deterministic suggestions.

- Filesort
- Framework N+1
- Full Table Scan
- Hot Line
- Low Filter Ratio
- Missing Index
- N+1 Query
- Redundant Call
- Slow Query
- Temporary Table

The exact set lives in `optimus/ai_fix.py::AI_ELIGIBLE_FINDING_TYPES`. A test (`test_ai_privacy.py::TestDocStaysFresh`) compares this section's list against the frozenset to make sure the doc never drifts from the code.

### 5.1 Per-type opt-out (`ai_excluded_finding_types`)

`Optimus Settings → AI → Privacy & Operations → Excluded finding types` is a multi-line list. Each line names one of the ten types above (exact match, case-sensitive). Lines starting with `#` are comments. Listed types are skipped in **both** auto-suggest and on-demand calls — no payload is built, no request is sent, the on-demand button surfaces a clear "excluded by Optimus Settings" message.

Use this when the SQL or source for a particular finding category embeds business logic you don't want flowing to a hosted provider:

```text
# We're fine sending N+1 patterns to Anthropic, but our pricing-rule SQL
# embeds margin formulas — skip Slow Query and Full Table Scan.
Slow Query
Full Table Scan
```

The list is empty by default. The exclusion list is **additive** — types not listed continue to flow normally.

---

## 6. Keep data on-box: local-LLM recipes

To keep finding context entirely on your bench host, run an OpenAI-compatible server locally and configure Optimus to point at it. No external network call leaves your machine. Three known-good stacks:

### 6.1 Ollama

```bash
# Pull a code-aware model. 8B fits on a 12 GB GPU; for 16 GB+ use 14B+.
ollama pull qwen2.5-coder:7b   # or llama3.1:8b, deepseek-coder-v2:16b

# Start the daemon (defaults to 127.0.0.1:11434).
ollama serve
```

Optimus Settings → AI:

- Provider: `OpenAI-compatible`
- Base URL: `http://localhost:11434/v1`
- Model: `qwen2.5-coder:7b` (the exact tag you pulled)
- API Key: leave blank
- `ai_request_timeout_seconds`: `180` (first-token cold-start can exceed 60s)

First call after restart pays the model-load cost (often 5–30s on CPU; <1s on a warm GPU). Subsequent calls are fast.

### 6.2 LM Studio

Open LM Studio, load a model, switch to the **Local Server** tab, **Start Server**. It listens on `http://localhost:1234/v1` by default.

Optimus Settings → AI:

- Provider: `OpenAI-compatible`
- Base URL: `http://localhost:1234/v1`
- Model: the model identifier shown in LM Studio (often a path-style name like `bartowski/Qwen2.5-Coder-7B-Instruct-GGUF`)
- API Key: leave blank
- `ai_request_timeout_seconds`: `180`

### 6.3 vLLM

```bash
# Production-grade local serving. Requires a GPU.
vllm serve Qwen/Qwen2.5-Coder-7B-Instruct \
    --host 0.0.0.0 --port 8000
```

Optimus Settings → AI:

- Provider: `OpenAI-compatible`
- Base URL: `http://localhost:8000/v1`
- Model: `Qwen/Qwen2.5-Coder-7B-Instruct`
- API Key: leave blank
- `ai_request_timeout_seconds`: `120` (vLLM is the fastest of the three once warm)

### 6.4 Picking a starting timeout

| Stack | Cold start (no GPU) | Cold start (GPU) | Warm call |
|---|---|---|---|
| Ollama (7-8B) | 10–30s | 1–3s | 1–5s |
| LM Studio (7B) | 5–20s | 1–2s | 1–4s |
| vLLM (7B) | n/a (GPU only) | <1s | <1s |

Default `ai_request_timeout_seconds = 60` is fine for hosted providers (Anthropic / OpenAI typically respond in 2–10s). For local stacks, **start at 180** and tune down once you've measured your warm-call P99. The setting is clamped to `[10, 600]` seconds.

---

## 7. Threat model

What this design protects against:

- **Accidental egress.** With `ai_enabled = OFF` (default) no request body is ever built — there's no code path that exfiltrates finding data.
- **Click-to-send.** With `ai_auto_suggest = OFF` (default) the LLM only sees a finding when the operator explicitly clicks the per-finding button. Every send is a deliberate, attributable action.
- **Category-level opt-out.** `ai_excluded_finding_types` lets you keep specific categories (e.g. Slow Query, where raw SQL flows verbatim) out of the wire entirely.
- **Network-residency.** The OpenAI-compatible provider + a local LLM keeps everything on your host. You can verify with `tcpdump` / `lsof` / `netstat` that no outbound socket opens during an AI call.

What this design does **not** protect against:

- **A compromised LLM provider.** If you're using Anthropic / OpenAI / a third-party, your finding context is at the mercy of their logging, retention, and abuse-monitoring policies. Read each provider's data-use policy.
- **On-disk caching by the LLM client.** Local servers (Ollama, LM Studio, vLLM) may log requests to disk depending on their flags. Check their docs and configure logging off if you're paranoid.
- **Backups and audit logs.** The AI suggestion (the response text) is persisted to `Optimus Finding.llm_fix_json`. Your DB backups include it. If a fix suggestion contains a paraphrase of sensitive code/SQL, it'll be in those backups.
- **The Optimus Telemetry Event DocType** (v0.8.0+). When telemetry is enabled, Optimus's own failures (including AI HTTP errors) are recorded with the provider name + endpoint label + status code — but never with the prompt or the payload. See `optimus/telemetry.py`.

---

## 8. For the dev shop receiving a profile

The "safe report" HTML file Optimus produces (the dev-shop interchange format) **does not** call any LLM at render or open time. When the operator sends you a profile:

- The report is fully self-contained — no CDN, no remote fetch on open.
- AI fix suggestions, if any, are **baked into the report** at analyze time. The HTML embeds the suggestion text as static markup; opening the report locally never triggers an AI call.
- The dev shop doesn't need an API key / provider configured to read the report — they need it only if they want to **regenerate** suggestions on their own bench.

This means: if you're worried about a profile shared with a third party leaking your code to their LLM provider, the answer is "the profile itself doesn't." But it also means: AI suggestions baked into the report carry the same content the LLM produced — review those before sharing if they paraphrase sensitive logic.

---

## 9. Where the code lives

| Concern | File | Symbol |
|---|---|---|
| Eligible-types frozenset | `optimus/ai_fix.py` | `AI_ELIGIBLE_FINDING_TYPES` |
| Provider matrix | `optimus/ai_fix.py` | `_PROVIDER_DEFAULTS` |
| Payload builders | `optimus/ai_fix.py` | `_build_messages`, `_build_steps_messages`, `_build_index_messages` |
| HTTP layer | `optimus/ai_fix.py` | `_http_post` |
| Per-type exclusion gate | `optimus/ai_fix.py` | `is_finding_type_excluded` |
| On-demand entry point | `optimus/api.py` | `suggest_fix`, `suggest_index`, `humanize_steps` |
| Auto-suggest entry point | `optimus/analyze.py` | `_enrich_findings_with_ai_suggestions`, `_enrich_table_breakdown_with_ai_suggestions` |
| Settings dataclass | `optimus/settings.py` | `OptimusConfig` |

---

## 10. Aerele Managed Provider (v0.14.x+)

`Aerele` is a hosted pay-as-you-go option for customers who don't want to bring their own Anthropic / OpenAI key. **Architecturally it is identical to the Anthropic / OpenAI / Kimi entries:** the operator picks `Aerele` as the provider, pastes the key Aerele issued into **API Key**, and every call hits Aerele's URL. There is no Optimus-side bookkeeping — no balance cache, no pre-call gate, no Refresh button, no daily sync. **All token validation and metering happens on Aerele's separate Frappe site** (the URL in the provider matrix above). The bench is a dumb client.

### 10.1 Onboarding

1. Sign up at [aerele.in/optimus/signup](https://aerele.in/optimus/signup) and top up tokens at [aerele.in/optimus/billing](https://aerele.in/optimus/billing).
2. In Optimus Settings ▸ AI Fix Suggestions:
   - Set **Provider** to `Aerele`.
   - Paste the issued key into **API Key**.
   - Save.

That is the entire integration. `ai_base_url` and `ai_model` use Aerele's defaults (`https://api.aerele.in/optimus/v1` + the upstream model Aerele has provisioned for the customer); leave them blank unless Aerele tells you otherwise.

### 10.2 Where balance lives

The customer manages their bucket entirely on `aerele.in` — sign-ups, top-ups, balance history, usage analytics. Optimus never sees, displays, or caches the balance. Each AI call is validated server-side on every request by Aerele's Frappe site; insufficient-balance and rate-limit refusals surface through `_http_post`'s existing 4xx handling with the response body's error text.

### 10.3 What additionally leaves the host

Compared to the per-pathway data inventory in § 2, picking `Aerele` adds nothing structural over what the other hosted providers already send:

- The customer's Aerele API key as `Authorization: Bearer <key>` on every call to `api.aerele.in`.
- The same OpenAI-shaped finding / steps / index payload from § 2.1–2.3.

What is **NOT** sent (matches § 3):

- The bench's `encryption_key` or any other site secret beyond the Aerele key itself.
- Cross-session correlation IDs, recording UUIDs, schema, or DocType names beyond what the finding-specific payload already includes.
- Heartbeat / metering / usage-counting calls. Aerele's billing meters from the actual `/chat/completions` traffic; the bench never pings out otherwise.
