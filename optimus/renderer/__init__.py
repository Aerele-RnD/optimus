# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Optimus renderer — turns a fully-analyzed ``Optimus Session`` row into
the self-contained safe-report HTML.

v0.10.0+ this module is a **package** that aggregates per-concern
submodules. Pre-v0.10.0 it was a single 4,958-line file; the file → package
conversion is the foundation for incremental section-by-section
extraction. See ``optimus/renderer/README.md`` for the extraction recipe,
the remaining cluster roadmap, and the structural-snapshot canary that
protects the template contract across follow-up PRs.

The public API (what ``analyze.py`` / ``api.py`` / the test suite import)
stays exactly as it was — every existing ``from optimus.renderer import X``
and ``optimus.renderer.X`` resolution continues to work. The wildcard
re-export from ``_internal`` covers names this module hasn't started
tracking explicitly; the explicit re-exports below are the names that
matter enough to lock with a unit test
(``test_renderer_structure_snapshot.py::TestPublicAPIPreserved``).
"""

from __future__ import annotations

# Re-export EVERY symbol from the legacy monolithic module — including
# underscore-prefixed internals like ``_OTHER_APP_LABEL``,
# ``_find_node_in_tree``, ``_build_executive_summary`` etc. that test files
# (and a few cross-module callers) import directly. ``from X import *``
# can't do this because PEP 8 hides underscore names; instead we walk
# ``dir(_internal)`` and copy each attribute into this module's globals.
#
# This is the file → package compatibility shim. It can be tightened to a
# hand-curated explicit re-export list once the section-by-section
# extractions in follow-up PRs have moved every name into a named
# submodule. For now, completeness wins.
from optimus.renderer import _internal as _renderer_internal

for _name in dir(_renderer_internal):
	if _name.startswith("__") and _name.endswith("__"):
		# Skip dunders — they're either Python-internal (``__name__``,
		# ``__file__``) or module re-imports we don't want shadowing
		# this package's own dunders.
		continue
	globals()[_name] = getattr(_renderer_internal, _name)
del _name
