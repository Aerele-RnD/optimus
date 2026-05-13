# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.2 collapsible report sections.

Each top-level report section is a native HTML5 <details class="section">
with an `open` attribute (so it's expanded by default but foldable).
Framework-level observations live as a nested <details class="subsection">
INSIDE Findings (collapsed by default so the main "what to fix" list reads
clean without burying user-actionable items under framework noise).

User request verbatim: "In report make each sections as collapsable. If
its a framework related issue then move a sub-section".
"""

import os
import re


def _read_template():
	here = os.path.dirname(__file__)
	tpath = os.path.join(here, "..", "templates", "report.html")
	with open(tpath) as f:
		return f.read()


def test_no_stray_section_div_tags():
	"""All top-level <div class="section"> blocks must be converted to
	<details class="section"> so every section is collapsible."""
	template = _read_template()
	assert '<div class="section">' not in template, (
		"Every section must use <details class='section'> — bare "
		"<div class='section'> means that section is not collapsible."
	)
	# Same for the <section class="section"> form, if any creeps in.
	assert '<section class="section' not in template, (
		"The <section class='section'> form must also be converted to "
		"<details class='section'> for collapsibility."
	)


def test_details_tags_are_balanced():
	"""Every <details> must have a matching </details>. An unbalanced
	tag set means the collapsible edit broke the template structure."""
	template = _read_template()
	opens = len(re.findall(r"<details[\s>]", template))
	closes = len(re.findall(r"</details>", template))
	assert opens == closes, (
		f"<details> tags unbalanced: {opens} open vs {closes} close"
	)


def test_primary_sections_are_open_by_default():
	"""Top-level report sections use `<details class="section" open>` —
	expanded by default so the report reads like a document, with the
	fold affordance as opt-in. Only the observations subsection is
	collapsed by default."""
	template = _read_template()
	# Spot-check each primary section we know about.
	for section_heading in (
		"Summary",
		"Per-action breakdown",
		"Findings &mdash; what to fix",
		"Server Resource",
		"Frontend",
		"Hot frames",
		"Top",               # "Top {{ top_queries|length }} slowest queries"
		"Queries per action",
		"Time spent per database table",
		"Full recordings",
	):
		assert section_heading in template, (
			f"section heading {section_heading!r} missing from template"
		)

	# Every open-by-default primary section uses the `open` attribute. The
	# attribute may be followed by `>` or by another attribute (e.g.
	# `id="..."` for in-page nav links), so don't require `>` right after.
	# Count: we expect ≥ 8 such sections (varies with conditional ones).
	open_sections = re.findall(r'<details class="section[^"]*" open[ >]', template)
	assert len(open_sections) >= 8, (
		f"expected at least 8 <details class='section' open ...> blocks, "
		f"found {len(open_sections)}"
	)


def test_heavy_reference_sections_are_collapsed_by_default():
	"""v0.6.0: the raw-dump sections ("Full recordings", "Queries per action")
	ship collapsed so the report reads as a digest, not a wall of SQL."""
	template = _read_template()
	for heading in ("Full recordings", "Queries per action"):
		# The <details> right before this <summary><h2>heading</h2> must NOT
		# carry the `open` attribute.
		idx = template.index(f"<summary><h2>{heading}</h2>")
		details_open = template.rindex("<details", 0, idx)
		details_tag = template[details_open:idx]
		assert " open" not in details_tag, (
			f"'{heading}' section must be collapsed by default (no `open`); got: {details_tag!r}"
		)


def test_report_has_navigation_aids():
	"""v0.6.0: a 'How to read this report' orientation block and a compact
	in-page 'Jump to:' nav near the top."""
	template = _read_template()
	assert "How to read this report" in template
	assert "Jump to:" in template
	# The jump links point at sections that carry matching ids.
	for anchor in ("#findings", "#per-action", "#top-queries", "#db-tables"):
		assert f'href="{anchor}"' in template, f"jump link {anchor} missing"
		assert f'id="{anchor[1:]}"' in template, f"section id {anchor[1:]} missing"


def test_observations_subsection_is_collapsed_by_default():
	"""Framework-level observations ship collapsed so they don't distract
	from the actionable list. User explicitly asked for them as a
	sub-section: 'If its a framework related issue then move a
	sub-section'."""
	template = _read_template()
	# Subsection is present…
	assert '<details class="subsection">' in template, (
		"Framework-level observations must use "
		"<details class='subsection'> (no `open` → collapsed by default)"
	)
	# …and it uses <h3> inside its summary (nested heading level).
	assert "<summary><h3>Framework-level observations" in template, (
		"Observations subsection must use an <h3> inside <summary>"
	)


def test_observations_is_nested_inside_findings():
	"""The Observations <details class='subsection'> must appear between
	the Findings <summary> and its closing </details>. Otherwise the
	'move a sub-section' part of the user's request isn't satisfied.

	v0.6.x: there are now multiple <details class='subsection'> blocks in
	the template (the per-action / hot-frames / background-jobs /
	top-queries sections each have a "framework items" sub-block). Find
	the Observations subsection specifically by anchoring on its <h3>.
	"""
	template = _read_template()
	findings_summary_idx = template.find(
		"<summary><h2>Findings &mdash; what to fix</h2></summary>"
	)
	# The Observations subsection's <summary> carries an <h3> labelled
	# "Framework-level observations". Walk back from there to its opening
	# <details class="subsection"> tag.
	observations_summary_idx = template.find(
		"<summary><h3>Framework-level observations"
	)
	assert findings_summary_idx > 0, "Findings summary not found"
	assert observations_summary_idx > 0, "Observations summary not found"
	subsection_idx = template.rfind(
		'<details class="subsection"', 0, observations_summary_idx
	)
	assert subsection_idx > 0, "Observations subsection opening not found"
	assert subsection_idx > findings_summary_idx, (
		"Observations subsection must be nested after Findings' <summary>"
	)

	# And the Findings closing </details> must come AFTER the subsection
	# (proving containment, not just ordering).
	# Walk forward: the subsection has its own </details>, and Findings
	# has its own </details> after that. Verify the subsection closes
	# BEFORE Findings closes.
	sub_close_idx = template.find("</details>", subsection_idx)
	assert sub_close_idx > 0
	# Findings close must be after (different) sub close.
	findings_close_after_sub = template.find("</details>", sub_close_idx + 1)
	assert findings_close_after_sub > sub_close_idx, (
		"Findings <details> must wrap (contain) the Observations subsection"
	)


def test_collapsible_css_is_present():
	"""The chevron + summary styling must ship in the template head so
	sections render consistently in standalone HTML files (the report
	is distributed as self-contained HTML)."""
	template = _read_template()
	# Chevron rotation when open.
	assert "details.section[open] > summary::before" in template, (
		"Chevron rotate-on-open CSS rule missing — sections will look "
		"static instead of animating"
	)
	# Subsection styling (dashed separator, grey h3).
	assert "details.subsection {" in template, (
		"Subsection CSS rule missing — framework observations will "
		"look identical to top-level sections"
	)


def test_phase2_section_appears_before_findings():
	"""v0.6.x: the Phase 2 line-level drill-down is the report's most
	distinctive section — it must be hoisted above the actionable
	Findings list so readers see it immediately after the Summary."""
	template = _read_template()
	phase2_anchor = template.find('id="phase2"')
	findings_anchor = template.find('id="findings"')
	assert phase2_anchor > 0, "Phase 2 anchor (id=\"phase2\") missing from template"
	assert findings_anchor > 0, "Findings anchor (id=\"findings\") missing from template"
	assert phase2_anchor < findings_anchor, (
		"Phase 2 (id=\"phase2\") must render BEFORE Findings (id=\"findings\") — "
		"it is the report's showcase section"
	)


def test_phase2_jump_nav_link_present():
	"""The Jump-to nav must include a Phase 2 link (conditional on the
	session having phase-2 runs)."""
	template = _read_template()
	assert 'href="#phase2"' in template, (
		"Jump-to nav missing #phase2 link — Phase 2 section won't be "
		"reachable from the top-of-report navigation"
	)
	# Conditional wrapper: the link only renders when phase2_html exists.
	assert "{% if phase2_html %}<a href=\"#phase2\"" in template, (
		"Phase 2 nav link must be wrapped in {% if phase2_html %} so "
		"sessions without runs don't show a dangling link"
	)
