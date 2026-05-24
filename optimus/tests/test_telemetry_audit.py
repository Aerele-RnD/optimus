# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Drift-protection audit for the v0.8.0+ telemetry contract.

From v0.11.1 forward, every ``frappe.log_error(...)`` call in the
``optimus/`` package MUST have a ``telemetry.emit_failure(...)`` call
within the immediately following :data:`LOOKAHEAD_LINES` lines.

The v0.8.0 release shipped opt-in failure telemetry with 15 hand-picked
top-of-stack migration sites; v0.11.1 swept the remaining 63 sites. This
test is the forever-after canary: a future contributor who adds a new
error-logging site without the matching telemetry emit will fail this
test with the file:line of the orphan printed in the message.

The audit is intentionally simple — a line-walker, not an AST parser.
The v0.8.0 pattern fits in 8-10 lines (log_error + try + from-import +
emit_failure + except: pass); the :data:`LOOKAHEAD_LINES` window of 16
has margin for slightly more verbose blocks — comments between the
log_error and the emit, multi-line ``emit_failure(...)`` arg lists,
etc. If a real case ever needs > 16 lines, bump
:data:`LOOKAHEAD_LINES` — but treat that as a code-smell first.

Excluded:
  * ``optimus/tests/`` and ``optimus/tests_integration/`` — the
    log_error calls there are test-fixture stubs, not real instrumentation.
  * ``optimus/patches/`` — one-shot migration scripts; instrumenting them
    would emit on every ``bench migrate`` even for benign cases.
  * ``optimus/renderer/_internal.py`` — graceful-degradation paths
    inside 60+ silent excepts (the renderer-split README's deferred
    extraction roadmap covers this; a follow-up PR removes this
    allowlist entry once the renderer's log_error sites are evaluated
    individually).
"""

from __future__ import annotations

from pathlib import Path

LOOKAHEAD_LINES = 16

EXCLUDED_DIRS = (
	"optimus/tests/",
	"optimus/tests_integration/",
	"optimus/patches/",
)

EXCLUDED_FILES = (
	# Renderer's many silent log_error calls are explicitly deferred —
	# graceful-degradation paths, low signal-to-noise per the v0.8.0
	# plan. A follow-up PR (using the renderer-split extraction recipe
	# in optimus/renderer/README.md) can evaluate them site-by-site.
	"optimus/renderer/_internal.py",
	# The telemetry module's own log_error calls fire when a sink (the
	# DocType insert / the JSONL append) raises during flush. Emitting
	# telemetry from telemetry's own failure handler would recurse into
	# the same broken sink — log_error stays as the only signal there.
	"optimus/telemetry.py",
)


def _repo_root() -> Path:
	"""Resolve the repository root (the ``apps/optimus`` checkout) from
	this test file's location. Works regardless of cwd."""
	# This file lives at optimus/tests/test_telemetry_audit.py; root is
	# two levels up.
	return Path(__file__).resolve().parent.parent.parent


def _is_excluded(posix_path: str) -> bool:
	if posix_path in EXCLUDED_FILES:
		return True
	return any(posix_path.startswith(d) for d in EXCLUDED_DIRS)


def _find_orphans() -> list[str]:
	"""Walk every .py file under optimus/ outside the exclusion list.
	For each ``frappe.log_error(`` line, scan the next :data:`LOOKAHEAD_LINES`
	lines for ``telemetry.emit_failure(``. Return a list of "file:line  <line content>"
	strings for sites that have no matching emit."""
	root = _repo_root()
	orphans: list[str] = []
	for path in sorted((root / "optimus").rglob("*.py")):
		posix = path.relative_to(root).as_posix()
		if _is_excluded(posix):
			continue
		try:
			lines = path.read_text(encoding="utf-8").splitlines()
		except Exception:
			# A read failure shouldn't break the audit — fail loud so
			# the contributor sees the file path.
			orphans.append(f"{posix}  (could not read: encoding error)")
			continue
		for i, line in enumerate(lines):
			if "frappe.log_error(" not in line:
				continue
			window = lines[i : i + LOOKAHEAD_LINES]
			if any("telemetry.emit_failure(" in w for w in window):
				continue
			orphans.append(f"{posix}:{i + 1}  {line.strip()}")
	return orphans


class TestEveryLogErrorHasTelemetry:
	"""The drift-protection canary.

	If this fails, you added a ``frappe.log_error(...)`` site without a
	``telemetry.emit_failure(...)`` call within the next 12 lines. Add
	the emit beside the log_error using the v0.8.0 pattern:

	    except Exception as exc:
	        frappe.log_error(title="optimus <name>")
	        try:
	            from optimus import telemetry
	            telemetry.emit_failure("<event_name>", exc, context={...})
	        except Exception:
	            pass

	See ``optimus/telemetry.py`` for the emit API and v0.8.0's CHANGELOG
	for the event-naming conventions.
	"""

	def test_no_orphan_log_error_sites(self):
		orphans = _find_orphans()
		assert not orphans, (
			"log_error sites without a telemetry.emit_failure call within "
			f"{LOOKAHEAD_LINES} lines:\n  " + "\n  ".join(orphans)
		)
