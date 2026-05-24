# `optimus.tests_integration` — real-bench integration tests

A sibling to `optimus/tests/`. The unit suite (`optimus/tests/`) runs in
~6 seconds against a Frappe stub and ships with the pure-pytest CI
workflow. This directory holds tests that need a **live Frappe bench**
— real MariaDB, real Redis, real RQ workers — and runs in CI via
`.github/workflows/integration.yml` against a bench provisioned by
`.github/helper/install.sh`.

## Why a separate directory

The unit suite's `conftest.py` installs a Frappe **stub** at collection
time so `from optimus import …` works without a bench. Integration
tests need the **real** Frappe; running them under the stub would
explode. Keeping the two suites in sibling directories means:

* `pytest optimus/tests/` collects ONLY unit tests (the integration
  directory is never traversed by the unit workflow).
* `bench --site test_site run-tests --app optimus --module
  optimus.tests_integration.<name>` collects ONLY integration tests
  (Frappe's test runner already skips `tests/` because it expects each
  test class to subclass `frappe.tests.utils.FrappeTestCase`).

## Running locally

You need a Frappe bench with optimus installed. The CI helper script
provisions one from scratch; a local developer with an existing bench
can run directly:

```
cd ~/frappe-bench
bench --site optimus.local run-tests \
    --app optimus \
    --module optimus.tests_integration.test_install_smoke

bench --site optimus.local run-tests \
    --app optimus \
    --module optimus.tests_integration.test_recording_lifecycle_e2e
```

Both modules complete in well under a minute on a warm bench.

To run BOTH modules in one go:

```
bench --site optimus.local run-tests --app optimus
```

(That picks up every `FrappeTestCase` subclass under
`optimus/tests_integration/`. The pure-pytest unit suite in
`optimus/tests/` is NOT a `FrappeTestCase` subclass, so it's not
picked up here.)

## Fixtures (`conftest.py`)

* **`test_site`** — yields `frappe.local.site` (the site the runner
  connected to). Tests rarely need it explicitly but it's useful for
  shelling out to `bench --site {test_site} …`.
* **`cleanup_session`** — autouse. After every test, hard-deletes any
  `Optimus Session` rows for the current user + clears the user's
  Redis active-session pointer. `FrappeTestCase` already rolls back
  per-test, but the analyze pipeline writes through a background-worker
  connection that escapes the rollback in production flows — same for
  Redis state. The cleanup is defence-in-depth.
* **`seeded_session`** — convenience wrapper. Calls `api.start`, yields
  the `session_uuid`, then on teardown calls `api.stop` + waits up to
  60 s for the session to land on a terminal state (`Ready` /
  `Failed`).

## The "no flakiness" rule

A flaky integration test gets **quarantined**, not retried.

If a test fails intermittently in CI:

1. Within 24 hours, add `@pytest.mark.skip(reason="quarantined: see #N")`
   on the test method.
2. File a GitHub issue with the CI logs (uploaded as the
   `integration-logs` artifact on failure).
3. The next PR that comes through fixes the root cause OR removes the
   test if the underlying behaviour can't be made deterministic.

Retry-on-failure is OFF. We want flakiness to surface, not get masked.

## Adding a new integration test

The harness pattern from the existing two tests:

```python
# optimus/tests_integration/test_<feature>.py
import frappe
from frappe.tests.utils import FrappeTestCase


class TestMyFeature(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")  # or another fixture user

    def test_my_invariant(self):
        # Direct frappe.db / frappe.cache / api calls — no mocks.
        ...
```

Add the new module to `.github/workflows/integration.yml`'s "Run the
integration suite" step (one `bench run-tests --module …` line per
file). Each module gets its own log artifact on failure.

## Extraction roadmap

The architecture review identified seven high-ROI integration scenarios
beyond what this directory ships today. Each is a separate follow-up PR
using the harness above:

| Test | What it adds | Catches |
|---|---|---|
| ✓ `test_atomic_lua_merge_concurrent.py` (done in v0.12.1) | real-Redis + real-Lua concurrent test exercising the v0.7.x trilogy's invariants (recording+status race, distinct job_ids, setdefault first-writer-wins, fallback path) | Field loss under worker contention |
| `test_telemetry_flush_doctype_sink.py` | enables telemetry, triggers a failure, flushes, asserts a row | Settings → DocType wiring + flush logic |
| `test_ai_privacy_exclusion_on_api.py` | hits the live `api.suggest_fix` endpoint with an excluded type | API surface respects the exclusion + telemetry logs the refusal |
| `test_regenerate_reports_idempotent.py` | renders, regenerates, diffs the two HTML outputs | Re-render path is byte-stable when recordings cached |
| `test_phase2_tool_orphan_recovery.py` | leaks `sys.monitoring` tool 2, simulates worker respawn, verifies the startup probe reclaims it | The v0.7.x `fbf3179` fix holds across real worker bounces |
| `test_safe_report_self_contained_on_real_bench.py` | renders a real session, downloads the safe-report HTML file, asserts no remote-fetch URLs | The self-containment canary holds when assets come through real bench paths |
| `test_janitor_sweeps_actually_delete.py` | seeds a stale session, runs `optimus.janitor.sweep_stale_sessions`, verifies cleanup | Janitor cron + retention logic |

Each is ~100-200 LOC. Pick the highest-impact one when you're picking
work.

## Justification rule

Integration tests cost ~5 seconds of CI wall time each (cheap compared
to the bench bootstrap) but they're harder to debug than unit tests,
they're harder to keep deterministic, and they raise the bar to
contributing.

**Before adding an integration test, ask: could a unit test have
caught this?** If yes, write the unit test instead. The integration
suite is reserved for behaviour that genuinely needs the inter-
component handoff (Redis ↔ MariaDB ↔ RQ ↔ Optimus's own hooks).
