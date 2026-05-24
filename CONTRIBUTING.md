# Contributing to Optimus

Optimus is the Aerele-maintained Frappe performance profiler. It
ships as an installable Frappe v16 app (not a SaaS, no separate
frontend bundler). This guide covers the day-to-day contribution
flow.

## Local setup

```bash
# In your Frappe bench:
bench get-app https://github.com/Aerele-RnD/optimus.git
bench --site <yoursite> install-app optimus
bench --site <yoursite> migrate

# Optimus 0.7.x is fresh-deploy-only. If you have the legacy
# ``frappe_profiler`` installed, uninstall it first:
bench --site <yoursite> uninstall-app frappe_profiler
```

## Test gate

Every PR must pass the full test suite and `ruff check`:

```bash
# Full suite (~6 seconds, 1450+ tests):
cd <bench-root> && env/bin/python -m pytest apps/optimus/optimus/tests/ -q

# Lint:
env/bin/python -m ruff check apps/optimus/optimus/
```

The `test_pdf_export.py` 3-test module requires a writable
`logs/cssutils.log` to run; CI ignores it via
`--ignore=apps/optimus/optimus/tests/test_pdf_export.py`. Adjust as
needed for your bench setup.

## Integration tests (real bench)

The unit suite above runs in seconds against a Frappe stub. The
integration suite at `optimus/tests_integration/` runs against a real
bench — real MariaDB, real Redis, real RQ — and catches regressions in
the inter-component handoff the unit stubs can't reach.

In CI: `.github/workflows/integration.yml` provisions a fresh Frappe
v16 bench via `.github/helper/install.sh`, installs optimus, and runs
the integration modules. It runs on pull requests to `main`, on push
to `main`, daily at 04:00 UTC, and on manual dispatch.

Locally:

```bash
cd <bench-root>
bench --site <your-site> run-tests --app optimus \
    --module optimus.tests_integration.test_install_smoke

bench --site <your-site> run-tests --app optimus \
    --module optimus.tests_integration.test_recording_lifecycle_e2e
```

Adding a new integration test: read `optimus/tests_integration/README.md`
for the harness pattern, the deferred-test extraction roadmap, and the
"no flakiness" rule. Integration tests must justify the bench cost —
prefer a unit test when feasible.

## Pre-commit

Install the hook once per clone:

```bash
pip install pre-commit
pre-commit install
```

`.pre-commit-config.yaml` runs `ruff check --fix` on every commit.
Format-checking is intentionally NOT included - a one-shot reformat
is a separate, deliberate commit.

## Branch + commit conventions

- **Branch names** follow `<type>/<short-description>`:
  `feat/sticky-nav`, `fix/em-dash-sweep`, `chore/rate-limit`.
- **Commit messages** follow Conventional Commits with a `v0.7.x`
  scope hint:
  ```
  feat: v0.7.x sticky nav-pills + GitHub-Light syntax theme

  - Drop K.1 / K.3 / K.4 reverts; keep K.0 (highlighting) + K.2.
  - Adjust hot-line opacity for the new bg.
  - 1450 tests green; ruff clean.
  ```
- **No `Co-Authored-By: Claude`** trailer.
- One sprint per commit when changes are coherent; split commits
  per concern when they're cross-cutting.

## Memory + plans

This repo uses a local `/Users/navin/.claude/...` memory + plan
folder to track work across sessions. External contributors don't
need to interact with these; they're maintainer artifacts.

## Frozen surfaces

These areas are deliberately stable and should NOT be modified
without a separate design discussion:

- **Capture pipeline** (`capture.py`, `recorder.py`,
  `infra_capture.py`, `analyze.py`) - was frozen at v0.3.0; bug
  fixes only. New behaviour belongs at render time
  (`renderer.py`, `report_context.py`).
- **DocType field schemas** - additive only; column removals
  require a versioned patch in `optimus/patches/v_*`.
- **Public API surface** in `api.py` - whitelisted endpoints are
  load-bearing for the floating widget and external CLIs.

## Areas that welcome contribution

- `renderer.py` UX polish (panels, layout, accessibility).
- New analyzers in `analyzers/` (each is a pure function that
  consumes the analyze-context and emits findings).
- Test coverage gaps - especially around rare data shapes.
- Documentation, CHANGELOG entries, README clarifications.

## Reporting security issues

See [SECURITY.md](SECURITY.md) for the responsible-disclosure flow.
**Do not file public issues for security bugs.**
