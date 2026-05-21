# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.2 split of findings into
"Findings — what to fix" (actionable, concrete fixes) vs
"Observations" (framework/system/informational — no direct fix).

Per user feedback: "In Findings — what to fix, Show only the valid
fixes." The split keeps the main action-list tight and reads as a
punchlist while the Observations section preserves full-picture
transparency.
"""

import inspect

from optimus import renderer


def test_actionable_finding_types_has_concrete_fixes_only():
	"""_ACTIONABLE_FINDING_TYPES must include the finding types where
	the customer_description ends with a concrete, shippable fix
	(add THIS index, refactor THIS loop, trim THIS response).
	Informational / framework / system finding types must NOT be in
	the set."""
	expected_actionable = {
		"N+1 Query",
		"Missing Index",
		"Full Table Scan",
		"Filesort",
		"Temporary Table",
		"Low Filter Ratio",
		"Slow Query",
		"Slow Hot Path",
		"Hook Bottleneck",
		# v0.7.x: BG-job fallback finding points at the deepest
		# user-code frame inside a slow job — a concrete callsite
		# to investigate with Line-Level Drilldown.
		"Slow Background Job",
		"Redundant Call",
		"Slow Frontend Render",
		"Heavy Response",
		# v0.6.x: Phase-2 line-profile output points at a specific line
		# of code with a concrete refactor target — actionable.
		"Hot Line",
	}
	assert renderer._ACTIONABLE_FINDING_TYPES == expected_actionable, (
		"_ACTIONABLE_FINDING_TYPES drifted from the expected set. "
		"Adding a new actionable finding type? Add it to the "
		"expected set here AND verify its customer_description "
		"gives a concrete fix (add <index>, refactor <loop>). "
		"Adding an observational type (system metric, framework-"
		"level issue)? It should NOT be in this set."
	)

	# Explicit NEGATIVE assertions on known observation-only types.
	for informational in (
		"Framework N+1",              # loop inside frappe/*
		"Repeated Hot Frame",         # needs investigation, no shippable fix
		"Resource Contention",        # system CPU
		"Memory Pressure",            # worker RSS / swap
		"DB Pool Saturation",         # infra pool
		"Background Queue Backlog",   # infra queue
		"Network Overhead",           # client/proxy path
	):
		assert informational not in renderer._ACTIONABLE_FINDING_TYPES, (
			f"{informational!r} is informational — users can't act on it "
			"with a shippable fix. It must NOT be in _ACTIONABLE_FINDING_TYPES."
		)


def test_render_splits_findings_into_actionable_and_observational():
	"""Source-inspection guard: renderer.render must partition
	session_doc.findings into `findings` (actionable) and
	`observational_findings` (everything else) via the allowlist.
	Without this split, observational noise re-pollutes the main
	'Findings — what to fix' section."""
	src = inspect.getsource(renderer.render)

	# The partition must use _ACTIONABLE_FINDING_TYPES.
	assert "_ACTIONABLE_FINDING_TYPES" in src, (
		"render() must reference _ACTIONABLE_FINDING_TYPES to split "
		"findings into actionable + observational buckets."
	)

	# Both bucket variable names must appear.
	assert "actionable_findings" in src
	assert "observational_findings" in src

	# Template context must expose the observations separately.
	assert '"observational_findings": observational_findings' in src, (
		"render()'s template context must pass observational_findings "
		"as a separate key so the template can render the Observations "
		"section independently."
	)


def test_template_has_observations_subsection_inside_findings():
	"""The report template must include the Observations as a nested
	subsection INSIDE the Findings section (v0.5.2 restructure — user
	asked: 'If its a framework related issue then move a sub-section').
	Without it the information disappears from the report entirely."""
	import os
	here = os.path.dirname(__file__)
	tpath = os.path.join(here, "..", "templates", "report.html")
	with open(tpath) as f:
		template = f.read()

	# The Framework-level observations heading must exist as a subsection.
	assert "Framework-level observations" in template, (
		"report.html must have a Framework-level observations subsection heading"
	)
	# It must loop over observational_findings.
	assert "observational_findings" in template, (
		"report.html must iterate observational_findings"
	)
	# Observations lives as a <section class="subsection"> inside Findings.
	findings_idx = template.find("Findings - what to fix")
	obs_idx = template.find("Framework-level observations")
	assert findings_idx > 0 and obs_idx > 0
	assert obs_idx > findings_idx, (
		"Framework-level observations subsection must live inside the "
		"Findings section (between its <h2> and closing </section>)"
	)
	# The subsection is a plain <section> block (non-collapsible).
	subsection_marker = '<section class="subsection">'
	assert subsection_marker in template, (
		"Framework-level observations must be a <section class='subsection'> "
		"block — non-collapsible by user request"
	)


def test_render_drops_function_not_invoked_findings():
	"""'Function Not Invoked' ("X was picked but never invoked during phase 2")
	is non-actionable noise — the pick simply didn't run in the replay. render()
	must filter it from ``all_findings`` (not merely re-bucket it) so it leaves
	the Findings list, the Observations subsection, AND the severity counts with
	no phantom rows. The Line-Level Drilldown notes uninvoked picks concisely
	instead. Done at render so existing reports declutter on regenerate too."""
	src = inspect.getsource(renderer.render)
	assert '"Function Not Invoked"' in src
	assert 'f.get("finding_type") != "Function Not Invoked"' in src
	# Must be filtered out of all_findings BEFORE the severity-count / split
	# derivations so the "Issues found" card and Observations agree.
	drop_idx = src.find('!= "Function Not Invoked"')
	sev_idx = src.find('"severity_counts"')
	split_idx = src.find("_ACTIONABLE_FINDING_TYPES")
	assert 0 < drop_idx < sev_idx, "Function Not Invoked filter must precede severity_counts"
	assert 0 < drop_idx < split_idx, "Function Not Invoked filter must precede the actionable split"


def test_severity_counts_cover_all_findings():
	"""v0.6.0: `severity_counts` (the "Issues found" stat card's sub-line)
	must count ALL findings — actionable + observational — so the card's
	big number (total), its sub-line, and the Summary prose's count all
	agree. (Previously it counted actionable-only, which made the big
	number and the breakdown disagree.)"""
	src = inspect.getsource(renderer.render)
	assert "severity_counts" in src
	sc_idx = src.find('"severity_counts"')
	assert sc_idx > 0
	sc_block = src[sc_idx:sc_idx + 600]
	assert "for f in all_findings" in sc_block, (
		"severity_counts must iterate `all_findings` (the full list), so "
		"the 'Issues found' card's total and breakdown line up"
	)
