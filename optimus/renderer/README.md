# `optimus.renderer` — package layout & extraction recipe

`optimus.renderer` turns a fully-analyzed `Optimus Session` row into the
self-contained safe-report HTML. v0.10.0+ it's a **package** that
aggregates per-concern submodules; pre-v0.10.0 it was a single 4,958-line
`optimus/renderer.py` file.

This README is the future-author roadmap: why the package exists, the
recipe to extract more clusters, the structural-snapshot canary that
protects the template contract, and what's still in `_internal.py`
waiting for a follow-up PR.

## Why a package

The monolith was the app's biggest maintainability hazard. Touching one
helper rippled across 15+ callers spread over 3,000+ lines of context;
code review became "find the section" before it became "review the
change"; new contributors faced a steep on-ramp; the safe-report layer
is exactly where a dev shop using Optimus would want to extend, and it
was the hardest place to extend safely.

The package split does **not** rewrite logic — it relocates self-contained
clusters into named modules. The output HTML stays byte-equivalent (and
the structural-snapshot test below enforces that across every
extraction).

## Current layout

```
optimus/renderer/
  __init__.py              # backward-compat re-export shim (every legacy
                           # `from optimus.renderer import X` still works)
  _internal.py             # the legacy bulk content (~4,400 LOC,
                           # was 4,958 before the v0.10.0 extractions)
  README.md                # this file
  source.py                # source-file I/O + _BoundedFileCache (LRU)
                           # — _path_within_bench, _resolve_source_path,
                           #    _read_source_snippet, _read_source_window
  syntax.py                # Pygments highlighting + diff-block wrapper
                           # — _ensure_pygments, _highlight_*, _highlight_diff_html
  time_format.py           # duration + datetime formatting
                           # — _format_duration_ms, _format_datetime_display,
                           #    _get_server_timezone
  visualization.py         # donut chart + hot-frames + frame-name redaction
                           # — build_donut_data, build_donut_svg,
                           #    build_hot_frames_table, redact_frame_name
  call_tree_renderer.py    # call-tree panel (nested <details> tree)
                           # — _render_call_tree_panel, _render_call_tree_node,
                           #    _ct_is_user_frame, _ct_is_sql_leaf,
                           #    _ct_is_other_frame
  doc_event_renderer.py    # doc-event lifecycle binding + per-DocType
                           # breakdown — _extract_target_doc,
                           #    _attach_action_context,
                           #    _build_doc_event_breakdown,
                           #    _doctype_from_controller_path, ...
```

All six submodules are imported back into `_internal.py` under their
original names so legacy call sites resolve unchanged. The package's
`__init__.py` walks `dir(_internal)` and re-exports every non-dunder name
— including underscore-prefixed internals — so external callers
(`analyze.py`, `api.py`, the test suite) see exactly the surface they saw
pre-split.

## The extraction recipe (for follow-up PRs)

1. **Identify a self-contained cluster.** Look for a group of functions
   that only call each other + standard helpers (no calls back into
   `_internal.py`). The Plan agent's analysis table at the bottom of this
   file lists the remaining candidates with coupling estimates.

2. **Create the new submodule.** Copy the cluster's functions + any
   module-level constants they depend on into `optimus/renderer/<name>.py`.
   Add the standard file header (copyright + a 1-paragraph docstring
   describing the module's purpose).

3. **Delete the originals from `_internal.py`.** Then add an explicit
   `from optimus.renderer.<name> import …` at the top of `_internal.py`
   that re-introduces every name the rest of `_internal.py` still uses.

4. **Run the structural snapshot test.** `pytest
   optimus/tests/test_renderer_structure_snapshot.py -v` must stay green.
   If the fingerprint changed, you've either changed the DOM (revisit the
   extraction) or you've legitimately added a new template section (the
   one snapshot regeneration recipe is below).

5. **Run the full suite.** `pytest optimus/tests/ -q
   --ignore=optimus/tests/test_pdf_export.py` must stay at the current
   passing count + any tests you add.

Add a one-line CHANGELOG entry under the current dev release noting which
cluster moved + the new submodule name.

## The structural snapshot canary

`optimus/tests/test_renderer_structure_snapshot.py` renders a synthetic
session through `render_raw()` and asserts a **structural fingerprint**
against a checked-in golden at `optimus/tests/fixtures/renderer_structure.json`:

* the sorted set of `id="…"` values that appear (which sections rendered)
* the sorted multiset of `class="…"` tokens (which CSS classes were used)
* the per-tag count across the whole document (gross DOM-shape sanity)

The pre-v0.10.0 test suite locked **content** ("the string '50× hits'
appears in the HTML") but never **structure** — a refactor that renamed
`<div class="finding-card">` to `<section class="finding">` would have
passed every existing test and silently broken the (frozen) template's
CSS. The snapshot closes that gap.

**Regenerating the snapshot.** When a legitimate template change lands
(a new section, a renamed CSS class, an added data attribute), the
snapshot test fails with a focused diff. To accept the new shape:

```bash
REGENERATE_RENDERER_SNAPSHOT=1 \
  env/bin/python -m pytest \
  optimus/tests/test_renderer_structure_snapshot.py::TestStructureSnapshot::test_fingerprint_matches_golden
```

The test rewrites `fixtures/renderer_structure.json` and exits with a
skip. Commit the updated fixture alongside the template change. The
diff in code review tells the reviewer exactly which IDs / classes / tag
counts moved.

## The frozen template

Per the user's standing constraint
([[feedback_report_template_frozen]]),
`optimus/templates/report.html` is frozen — don't restyle markup, CSS,
labels, section IDs, or class names without an explicit user request.
Per [[feedback_safe_report_self_contained]], the rendered HTML must stay
fully offline-safe (no `<script>` tags, no CDN / remote `src=` /
`href=` resource loads, no `@import`, no `url(http…)` in CSS). Both
guarantees are locked by tests:

* `test_renderer_structure_snapshot.py::test_self_containment_invariant`
* `test_report_a11y.py::test_report_is_self_contained_offline`

## Public-API stability

The package's contract — the names that external callers
(`analyze.py`, `api.py`, the test suite, third-party forks) MUST keep
finding — is enumerated and locked by:

`test_renderer_structure_snapshot.py::TestPublicAPIPreserved`

Currently asserted: `render`, `render_raw`, `_finding_to_dict`,
`build_donut_svg`, `build_hot_frames_table`, `redact_frame_name`,
`_BoundedFileCache`, `_read_source_snippet`, `_markdown_to_safe_html`,
`_build_line_drilldown_callsite_index`.

Adding to that list is a deliberate stability promise; removing a name
is a breaking change requiring a major-version bump.

## Remaining clusters (the follow-up PR roadmap)

The Plan agent's structural map flagged five clusters in `_internal.py`
worth extracting. The first two (`call_tree_renderer`,
`doc_event_renderer`) shipped in v0.12.8 / v0.12.10 respectively.
Remaining three, listed in recommended order:

| Cluster | Approx LOC | Coupling | Notes |
|---|---|---|---|
| ✓ `call_tree_renderer` (done in v0.12.8) | 240 | Weak | `_render_call_tree_panel`, `_render_call_tree_node`, `_ct_is_user_frame`, `_ct_is_sql_leaf`, `_ct_is_other_frame`. Self-contained tree rendering. |
| ✓ `doc_event_renderer` (done in v0.12.10) | 376 | Moderate | `_extract_target_doc`, `_attach_action_context`, `_build_doc_event_breakdown`, plus 6 helpers (`_module_from_filename`, `_doctype_from_controller_path`, `_doc_event_hook_index` family, `_finding_lifecycle_bindings`) + constants (`_LIFECYCLE_EVENTS`, `_KIND_*`, `_SEVERITY_RANK`). Self-contained at module-import time despite "Moderate" coupling at analyze-time. |
| `line_drilldown` | 840 | Internal | `_render_line_drilldown_panel`, `_build_line_drilldown_callsite_index`, related helpers. Single biggest remaining chunk. Semi-public — `analyze.py` calls `_build_line_drilldown_callsite_index`. |
| `finding_enrichment` | 380 | HIGH | `_finding_to_dict`, `_attach_drilldown_chains`, `_attach_representative_callsites`, `_expand_self_time_snippets`, `_retarget_phase1_callsites_to_drilldown_leaf`, `_normalize_callsite`. Tightly coupled to `analyze.py` callers — defer until the surrounding modules are extracted and the coupling shape is clearer. |
| `render()` orchestrator | 812 | Core | The big function itself. Could be split into per-phase helpers within `_internal.py`, but a per-module split isn't natural — it's an orchestrator, not a section. Keep integrated. |

Each follow-up PR uses the recipe above. The structural snapshot is the
shared safety net; the public-API tests are the contract harness.
