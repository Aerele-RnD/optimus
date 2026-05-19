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


def test_primary_actionable_sections_are_open_by_default():
	"""v0.7.x: only the actionable / narrative sections expand by default.
	Heavy reference + diagnostic sections (Phase 2 drill-down, Server
	Resource, Frontend, Hot frames, Top queries, DB tables, Doc-event
	lifecycle, Full recordings, Queries per action) ship collapsed so the
	first scroll shows the punch list, not a wall of tables."""
	template = _read_template()
	# Spot-check each primary section heading is still present in the
	# template (the open/closed flip doesn't remove sections).
	for section_heading in (
		"Summary",
		"Per-action breakdown",
		"Findings - what to fix",
		"Server Resource",
		"Frontend",
		"Hot frames",
		"Top",               # "Top {{ top_queries|length }} slowest queries"
		"Queries per action",
		"Time spent per database table",
		"Full recordings",
		"Doc-event lifecycle",
		"RQ Jobs",
	):
		assert section_heading in template, (
			f"section heading {section_heading!r} missing from template"
		)

	# The narrative spine — these MUST stay open so the report reads as a
	# document for the first scroll. Anchor on the literal
	# <summary><h2>HEADING</h2></summary> form so we don't accidentally
	# match a Jinja comment elsewhere (e.g. the {# v0.5.0: Steps to
	# Reproduce ... #} comment above the section).
	for summary in (
		'<summary><h2 style="margin-top: 0;">Steps to Reproduce</h2></summary>',
		"<summary><h2>Findings - what to fix</h2></summary>",
		"<summary><h2>Per-action breakdown</h2></summary>",
		"<summary><h2>RQ Jobs</h2></summary>",
	):
		idx = template.index(summary)
		details_open = template.rindex("<details", 0, idx)
		details_tag = template[details_open:idx]
		assert " open" in details_tag, (
			f"section {summary!r} must be open by default; "
			f"got: {details_tag[:200]!r}..."
		)


def test_heavy_reference_sections_are_collapsed_by_default():
	"""v0.7.x: heavy reference / diagnostic sections ship collapsed so the
	report reads as a digest, not a wall of SQL. Originally only Full
	recordings + Queries per action were closed; v0.7.x extends this to
	the broader diagnostic set."""
	template = _read_template()
	# Anchor on the literal <summary><h2>HEADING</h2></summary> form so we
	# don't accidentally match prose elsewhere (e.g. "open it in VS Code"
	# in the How-to-read block).
	for summary in (
		"<summary><h2>Full recordings</h2></summary>",
		"<summary><h2>Queries per action</h2></summary>",
		"<summary><h2>Summary</h2></summary>",
		"<summary><h2>Doc-event lifecycle</h2></summary>",
		"<summary><h2>Server Resource</h2></summary>",
		"<summary><h2>Frontend</h2></summary>",
		"<summary><h2>Time spent per database table</h2></summary>",
	):
		idx = template.index(summary)
		details_open = template.rindex("<details", 0, idx)
		details_tag = template[details_open:idx]
		assert " open" not in details_tag, (
			f"section {summary!r} must be collapsed; got: {details_tag!r}"
		)
	# Hot frames + Top queries have dynamic summary text; anchor on a
	# substring unique to each.
	for fragment in (
		"Hot frames (top",
		"- your app",
	):
		idx = template.index(fragment)
		details_open = template.rindex("<details", 0, idx)
		details_tag = template[details_open:idx]
		assert " open" not in details_tag, (
			f"section containing {fragment!r} must be collapsed; got: {details_tag!r}"
		)


def test_report_has_navigation_aids():
	"""v0.6.0: a 'How to read this report' orientation block and a compact
	in-page 'Jump to:' nav near the top."""
	template = _read_template()
	assert "How to read this report" in template
	assert "Jump to" in template
	# The jump links point at sections that carry matching ids.
	for anchor in ("#findings", "#per-action", "#top-queries", "#db-tables", "#how-to-read"):
		assert f'href="{anchor}"' in template, f"jump link {anchor} missing"
		assert f'id="{anchor[1:]}"' in template, f"section id {anchor[1:]} missing"


def test_section_id_aliases_for_mock_spec_names():
	"""v0.7.x Phase G: the mock spec uses shorter section IDs (#actions,
	#jobs, #resource, #queries, #db, #doc-events) alongside the original
	long-form names. The template carries both — original ID on the
	<details> tag so the Jump-to nav keeps working; alias as an empty
	<a id="..."></a> anchor inside each section so external links /
	docs that reference the mock names still scroll-to-anchor.

	Pin both forms exist."""
	template = _read_template()
	for original, alias in (
		("per-action", "actions"),
		("background-jobs", "jobs"),
		("server-resource", "resource"),
		("top-queries", "queries"),
		("db-tables", "db"),
		("doc-event-lifecycle", "doc-events"),
	):
		assert f'id="{original}"' in template, (
			f"original section id '{original}' must still exist on its "
			f"<details> tag for Jump-to back-compat"
		)
		assert f'id="{alias}"' in template, (
			f"mock-spec alias '{alias}' must exist as an empty <a> "
			f"anchor inside section #{original}"
		)


def test_net_new_section_ids_from_mock_spec():
	"""v0.7.x Phase I.1: the redesign mock spec introduced three
	anchors that don't have a long-form original — they're net-new
	IDs (not aliases). Pin their presence so a future template
	refactor doesn't quietly drop them.

	#repro    — Steps to Reproduce section
	#summary  — Summary section (bulleted recap)
	#hot-frames — Hot frames leaderboard section
	"""
	template = _read_template()
	for net_new in ("repro", "summary", "hot-frames"):
		assert f'id="{net_new}"' in template, (
			f"mock-spec section id '{net_new}' missing from template"
		)


def test_phase2_id_is_unique():
	"""v0.7.x Phase I.1: the phase2 anchor must appear at most once
	in the static template. Pre-Phase-I.1, the imperative-rendered
	Phase 2 panel emitted a details element carrying the phase2
	anchor while the surrounding template wrapped it in a redundant
	`<div id="phase2">` - duplicate IDs are invalid HTML. The
	wrapper was removed; this test pins that the duplicate doesn't
	come back. (v0.7.x Phase J.16: the renderer-emitted inner anchor
	is now ``id="line-drilldown"``; the template carries a single
	legacy ``<a id="phase2"></a>`` so external links resolve.)"""
	template = _read_template()
	# Count actual anchor tags carrying the id, not literal substrings
	# inside Jinja ``{# ... #}`` comments (which talk about the rename
	# but emit no DOM).
	count = template.count('<a id="phase2"')
	assert count <= 1, (
		f'<a id="phase2"... must appear at most once in the template; '
		f"found {count} occurrences. The imperative-rendered panel "
		f"emits its own ``id=\"line-drilldown\"`` anchor; only the "
		f"legacy ``<a id=\"phase2\"></a>`` alias should live in the "
		f"template."
	)


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
		"<summary><h2>Findings - what to fix</h2></summary>"
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
	"""v0.6.x: the Line-Level Drilldown is the report's most distinctive
	section - it must be hoisted above the actionable Findings list so
	readers see it immediately after the Summary.

	v0.7.x Phase J.16: anchor on the
	``report_data.line_drilldown_html | safe`` Jinja injection point
	(renamed from ``report_data.phase2_html | safe`` in J.2.6, itself
	renamed from the legacy top-level ``phase2_html``). The render-time
	markup still comes from ``_render_line_drilldown_panel`` and is
	exposed under the contract namespace."""
	template = _read_template()
	panel_injection = template.find("report_data.line_drilldown_html | safe")
	findings_anchor = template.find('id="findings"')
	assert panel_injection > 0, (
		"report_data.line_drilldown_html injection point missing from template"
	)
	assert findings_anchor > 0, "Findings anchor (id=\"findings\") missing from template"
	assert panel_injection < findings_anchor, (
		"Line-Level Drilldown (report_data.line_drilldown_html injection) "
		"must render BEFORE Findings (id=\"findings\") - it is the "
		"report's showcase section"
	)


def test_phase2_jump_nav_link_present():
	"""The Jump-to nav must include a Line-Level Drilldown link
	(conditional on the session having phase-2 runs)."""
	template = _read_template()
	assert 'href="#line-drilldown"' in template, (
		"Jump-to nav missing #line-drilldown link - Line-Level Drilldown "
		"section won't be reachable from the top-of-report navigation"
	)
	# Conditional wrapper: the link only renders when the panel exists.
	assert (
		'{% if report_data.line_drilldown_html %}<a href="#line-drilldown"'
		in template
	), (
		"Line-Level Drilldown nav link must be wrapped in "
		"{% if report_data.line_drilldown_html %} so sessions without runs "
		"don't show a dangling link"
	)


def test_tldr_hero_renders_before_kpi_strip_and_summary():
	"""v0.7.x redesign Phase B: the 'At a glance' exec-summary card
	was replaced by the TL;DR hero (one composed headline keyed on
	the highest-impact finding). The hero lives RIGHT AFTER the
	masthead — first prominent block on the page. Pin: TL;DR comes
	before the KPI strip, and the KPI strip comes before the
	Summary section. (Old test asserted At-a-glance lived between
	stats and Summary; that block is gone.)"""
	template = _read_template()
	tldr_idx = template.find('<div class="tldr">')
	stats_idx = template.find('<div class="kpis">')
	summary_idx = template.find("<summary><h2>Summary</h2></summary>")
	for label, idx in (
		("TL;DR hero", tldr_idx),
		("KPI strip", stats_idx),
		("Summary section", summary_idx),
	):
		assert idx > 0, f"{label} missing from template"
	assert tldr_idx < stats_idx < summary_idx, (
		"Header-zone order must be: TL;DR hero → KPI strip → "
		"Summary section. Got indices: "
		f"tldr={tldr_idx} stats={stats_idx} summary={summary_idx}"
	)
	# And the old exec-summary card MUST be gone.
	assert "<h2>At a glance</h2>" not in template, (
		"Old exec-summary heading must be removed (TL;DR replaces it)"
	)


def test_how_to_read_section_appears_after_main_content():
	"""v0.7.x: 'How to read this report' was moved from above the
	executive summary to the bottom of the report (just before the
	footer). Repeat readers don't need orientation surfaced above the
	technical content; first-time readers reach it via the new
	'How to read' link in the Jump-to nav.

	Pin the position so a future refactor doesn't quietly move the
	section back up — that would re-introduce the orientation noise."""
	template = _read_template()
	how_to_read_idx = template.find("<summary><h2>How to read this report</h2></summary>")
	db_tables_idx = template.find("<summary><h2>Time spent per database table</h2></summary>")
	footer_idx = template.find('<div class="footer">')
	assert how_to_read_idx > 0, "'How to read this report' section missing from template"
	assert db_tables_idx > 0, "'Time spent per database table' section missing from template"
	assert footer_idx > 0, "footer block missing from template"
	assert how_to_read_idx > db_tables_idx, (
		"'How to read this report' must render AFTER 'Time spent per "
		"database table' (it was moved to the bottom of the report)"
	)
	assert how_to_read_idx < footer_idx, (
		"'How to read this report' must render BEFORE the footer — "
		"don't push it past the report's closing block"
	)


def test_tbl_clip_word_break_rules_present():
	"""v0.7.x: tables that hold long unbreakable strings (dotted module
	paths, /api/method/... URLs, filesystem paths) opt into a shared
	``.tbl-clip`` class that fixes their layout and wraps cell content.
	Pin the load-bearing CSS rules so a future refactor doesn't quietly
	drop the wrapping guarantee."""
	template = _read_template()
	assert "table.tbl-clip { table-layout: fixed; }" in template, (
		".tbl-clip class must set table-layout: fixed so columns don't "
		"stretch past the page width"
	)
	# overflow-wrap: anywhere is what actually breaks the long strings
	# inside cells. Match the substring inside the .tbl-clip rule block.
	tbl_clip_idx = template.find("table.tbl-clip th,")
	assert tbl_clip_idx > 0, ".tbl-clip th/td rule missing"
	block_end = template.find("}", tbl_clip_idx)
	rule_body = template[tbl_clip_idx:block_end]
	assert "overflow-wrap: anywhere" in rule_body, (
		".tbl-clip cells must use overflow-wrap: anywhere so long "
		"unbreakable strings (paths, URLs) wrap mid-string"
	)


def test_per_action_table_has_fixed_layout_class():
	"""v0.7.x: the Per-action breakdown table must carry the shared
	``tbl-clip per-action-table`` class + a <colgroup> so long action
	labels and URL paths in the Action cell wrap instead of pushing
	the numeric columns off the right edge of the page. Both the
	tracked-apps table and the framework-apps sibling table need it."""
	template = _read_template()
	heading_idx = template.find("<summary><h2>Per-action breakdown</h2></summary>")
	assert heading_idx > 0, "Per-action breakdown heading missing"
	# Walk forward to the first table in the section.
	table_idx = template.find("<table", heading_idx)
	assert table_idx > 0, "Per-action breakdown table missing"
	# The opening tag fragment must contain the shared layout classes.
	tag_end = template.find(">", table_idx)
	open_tag = template[table_idx:tag_end]
	# v0.7.x Phase E: tables stack `.data` on top of the existing
	# `.tbl-clip` / `.per-action-table` classes. Check for both
	# critical layout hooks as substrings rather than the exact
	# class-attr value.
	assert "tbl-clip" in open_tag and "per-action-table" in open_tag, (
		f"Per-action breakdown table must carry tbl-clip + "
		f"per-action-table classes; got: {open_tag!r}"
	)
	# And a <colgroup> follows so column widths are defined.
	colgroup_idx = template.find("<colgroup>", table_idx)
	next_table_idx = template.find("<table", table_idx + 1)
	assert colgroup_idx > 0 and (next_table_idx < 0 or colgroup_idx < next_table_idx), (
		"Per-action breakdown table must include a <colgroup> defining "
		"column widths immediately after the <table> opening tag"
	)


def test_xhr_timing_table_has_fixed_layout_class():
	"""v0.7.x: the Per-action XHR timing table must carry
	``tbl-clip xhr-timing-table`` + a <colgroup> so /api/method/...
	URL strings wrap inside their cell instead of bleeding off the
	right edge of the page."""
	template = _read_template()
	heading_idx = template.find(
		'<h3 style="margin-bottom: 8px;">Per-action XHR timing</h3>'
	)
	assert heading_idx > 0, "Per-action XHR timing heading missing"
	table_idx = template.find("<table", heading_idx)
	assert table_idx > 0, "Per-action XHR timing table missing"
	tag_end = template.find(">", table_idx)
	open_tag = template[table_idx:tag_end]
	assert "tbl-clip xhr-timing-table" in open_tag, (
		f"Per-action XHR timing table must carry "
		f"'tbl-clip xhr-timing-table' in its class list; got: {open_tag!r}"
	)
	colgroup_idx = template.find("<colgroup>", table_idx)
	next_table_idx = template.find("<table", table_idx + 1)
	assert colgroup_idx > 0 and (next_table_idx < 0 or colgroup_idx < next_table_idx), (
		"Per-action XHR timing table must include a <colgroup> defining "
		"column widths immediately after the <table> opening tag"
	)
