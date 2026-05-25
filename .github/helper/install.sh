#!/usr/bin/env bash
# .github/helper/install.sh
#
# Provision a fresh Frappe v16 bench with optimus installed, for the
# real-bench integration test workflow. Mirrors the well-known pattern
# used by Frappe / ERPNext / community apps (see
# frappe/erpnext/.github/helper/install.sh for the canonical reference).
#
# Assumes ubuntu-latest GitHub Actions runner with MariaDB + 2 Redis
# service containers reachable on 127.0.0.1. Run from the repository
# root; the bench is created at ``$RUNNER_TEMP/frappe-bench`` (or, when
# RUNNER_TEMP isn't set, at ``./.bench-tmp`` for local development).

set -euo pipefail

# v0.12.27: explicit step echoes so a future failure surfaces the
# failing step in the job log without needing ``set -x`` (which
# floods the log with every variable expansion).
_step() {
	echo "::group::[install.sh] $*"
}
_end() {
	echo "::endgroup::"
}

# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-16}"
TEST_SITE="${TEST_SITE:-test_site}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-root}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
BENCH_DIR="${BENCH_DIR:-${RUNNER_TEMP:-${PWD}/.bench-tmp}/frappe-bench}"
OPTIMUS_REPO_DIR="${OPTIMUS_REPO_DIR:-${GITHUB_WORKSPACE:-${PWD}}}"

# ---------------------------------------------------------------------------
# System-level deps
# ---------------------------------------------------------------------------
# The GitHub ubuntu-latest runner already ships python3, node, redis-tools,
# and most build essentials. The mariadb-client is the one piece we routinely
# need that isn't pre-installed (for the ``bench new-site`` step's --no-mariadb-
# socket TCP path).
_step "apt-get install mariadb-client"
if ! command -v mariadb >/dev/null 2>&1; then
	sudo apt-get update -qq
	sudo apt-get install -qy --no-install-recommends mariadb-client
fi
_end

# v0.12.27: wkhtmltopdf was removed from Ubuntu 24.04 (the current
# ``ubuntu-latest`` runner base), so ``apt-get install wkhtmltopdf``
# fails with a package-not-found error and trips ``set -e``. The
# integration suite under ``optimus/tests_integration/`` does NOT
# exercise PDF rendering (it's a pure-pytest concern covered by
# ``optimus/tests/test_pdf_export.py``), so we skip the install
# entirely here. If a future integration test needs PDFs, swap to
# the explicit Debian package + ``--allow-downgrades`` install used
# by frappe/erpnext's CI.

# ---------------------------------------------------------------------------
# Bench init + optimus app symlink + test site
# ---------------------------------------------------------------------------
_step "pip install pip wheel frappe-bench"
python -m pip install --upgrade pip wheel
python -m pip install --quiet frappe-bench
_end

_step "bench init (FRAPPE_BRANCH=${FRAPPE_BRANCH})"
mkdir -p "$(dirname "${BENCH_DIR}")"
cd "$(dirname "${BENCH_DIR}")"

bench init \
	--skip-redis-config-generation \
	--skip-assets \
	--python "$(command -v python)" \
	--frappe-branch "${FRAPPE_BRANCH}" \
	"$(basename "${BENCH_DIR}")"

cd "${BENCH_DIR}"
_end

_step "bench set-mariadb-host + set-redis-{cache,queue}-host"
# Point bench at the runner's service containers. The cache + queue Redises
# are separate services on distinct ports so a future cache flush can't
# clobber pending RQ jobs (matches the production pattern).
bench set-mariadb-host 127.0.0.1
bench set-redis-cache-host "redis://127.0.0.1:13000"
bench set-redis-queue-host "redis://127.0.0.1:11000"
_end

_step "symlink optimus into apps/ + register in apps.txt"
# Symlink the optimus checkout into apps/. Using a symlink (rather than
# ``bench get-app``) lets the workflow test the EXACT working-tree state
# — including uncommitted changes — instead of whatever ``main`` happens
# to be at the moment.
ln -snf "${OPTIMUS_REPO_DIR}" apps/optimus
grep -qxF "optimus" sites/apps.txt || echo "optimus" >> sites/apps.txt
_end

_step "pip install -e apps/optimus"
# Install optimus's runtime deps into the bench's Python env. ``bench
# get-app`` would normally do this; with a symlink we run pip directly.
./env/bin/pip install --quiet -e apps/optimus
_end

_step "bench new-site ${TEST_SITE}"
# Create the test site. --no-mariadb-socket forces TCP (the runner's
# MariaDB service doesn't expose a unix socket).
bench new-site \
	--mariadb-user-host-login-scope=% \
	--admin-password "${ADMIN_PASSWORD}" \
	--db-root-password "${DB_ROOT_PASSWORD}" \
	--no-mariadb-socket \
	"${TEST_SITE}"
_end

_step "bench install-app optimus + set-config + migrate"
bench --site "${TEST_SITE}" install-app optimus
bench --site "${TEST_SITE}" set-config developer_mode 1
bench --site "${TEST_SITE}" set-config -p in_test true

# Optimus's startup probes + monkey-patches run at app import; restart
# isn't required for ``bench run-tests``, but doing it once flushes any
# cached schema state from before the symlink.
bench --site "${TEST_SITE}" migrate
_end

echo "bench provisioned at ${BENCH_DIR}; test site '${TEST_SITE}' ready."
