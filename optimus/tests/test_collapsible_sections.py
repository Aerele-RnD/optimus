# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the report's section structure.

Top-level sections (``<section class="section">``) always render
expanded and non-collapsible — user direction from the redesign:
"make all section expanded by default not collapsible".

Framework subsections (the four ``actions_framework`` /
``background_jobs_framework`` / ``hot_frames_framework`` /
``slow_queries_framework`` blocks) are an explicit exception: they
hold data the developer can't act on (frappe/erpnext entry points),
so they wrap their tables in ``<details class="subsection">`` and
are collapsed by default. Per-app finding buckets and similar
non-framework subsections remain plain ``<section class="subsection">``.

Inline ``<details>`` elements elsewhere (call-tree drill-downs,
per-row sub-finding expanders, "Slowest queries for this job"
lookups) stay collapsible — those are row-level affordances, not
sections.
"""

import os
import re


def _read_template():
	here = os.path.dirname(__file__)
	tpath = os.path.join(here, "..", "templates", "report.html")
	with open(tpath) as f:
		return f.read()


def test_top_level_sections_are_non_collapsible():
	"""Top-level sections always render expanded — no ``<details
	class="section">`` allowed. The user explicitly requested
	primary report sections render without a toggle."""
	template = _read_template()
	assert '<details class="section"' not in template, (
		"Top-level sections must be plain <section class='section'> "
		"blocks — '<details class=\"section\"' means the section is "
		"still collapsible."
	)


def test_framework_subsections_are_collapsible():
	"""The four framework subsections (actions / background-jobs /
	hot-frames / slow-queries) wrap their tables in ``<details
	class="subsection">`` so the reader can collapse framework noise
	out of the way. Each one carries a ``framework`` data variable
	in its surrounding markup. None should have an ``open`` attribute
	— collapsed by default is the whole point."""
	template = _read_template()
	# Expect at least 4 <details class="subsection"> opens — one per
	# framework block. If a new framework subsection is added in the
	# future this count goes up; the assertion stays ``>= 4`` so
	# additive changes don't break the test.
	opens = len(re.findall(r'<details class="subsection"', template))
	assert opens >= 4, (
		f"Expected >=4 collapsible framework subsections, found {opens}. "
		"Each of actions_framework / background_jobs_framework / "
		"hot_frames_framework / slow_queries_framework should wrap "
		"its table in <details class=\"subsection\">."
	)
	# Collapsed-by-default check: none should carry an `open` attribute.
	assert not re.search(
		r'<details class="subsection"[^>]*\sopen[\s>]',
		template,
	), (
		"<details class=\"subsection\"> should be collapsed by default — "
		"no `open` attribute. Remove the `open` to restore the contract."
	)


def test_details_tags_are_balanced():
	"""Every <details> must have a matching </details>. Inline drill-
	downs (call-tree, sub-finding rows, slowest-queries lookups)
	still use ``<details>``; an unbalanced count means a stray tag.

	Strips HTML and CSS comments before counting so prose mentions
	of the tag inside a comment block don't inflate the open count.
	"""
	template = _read_template()
	# Drop HTML comments (<!-- ... -->) and CSS block comments
	# (/* ... */) before counting so prose references to <details>
	# inside a comment don't register as false opens.
	stripped = re.sub(r"<!--.*?-->", "", template, flags=re.DOTALL)
	stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
	# Match <details> opens whether followed by whitespace, `>`, or
	# Jinja ``{% ... %}`` directives (e.g. ``<details{% if … %} open{% endif %}>``).
	opens = len(re.findall(r"<details[\s>{]", stripped))
	closes = len(re.findall(r"</details>", stripped))
	assert opens == closes, (
		f"<details> tags unbalanced: {opens} open vs {closes} close"
	)


def test_primary_actionable_sections_render():
	"""Spot-check each named primary section heading is still present
	in the template. The collapse-by-default mechanism is gone, so
	this is purely a presence check now."""
	template = _read_template()
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
	section tag so the Jump-to nav keeps working; alias as an empty
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
			f"section tag for Jump-to back-compat"
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
	count = template.count('<a id="phase2"')
	assert count <= 1, (
		f'<a id="phase2"... must appear at most once in the template; '
		f"found {count} occurrences."
	)


def test_observations_subsection_exists():
	"""Framework-level observations live as a structurally-distinct
	``<section class="subsection">`` inside the Findings section.
	User direction kept the "subsection" container even after dropping
	collapsibility, because the visual separation (dashed border,
	muted <h3>) still serves the "actionable vs context" split."""
	template = _read_template()
	assert '<section class="subsection">' in template, (
		"Framework-level observations must live in a "
		"<section class='subsection'> block (visual containment "
		"separator, even though no longer collapsible)."
	)
	assert "<h3>Framework-level observations" in template, (
		"Observations subsection must carry an <h3> heading "
		"naming it 'Framework-level observations'."
	)


def test_observations_is_nested_inside_findings():
	"""The Observations ``<section class='subsection'>`` must appear
	between the Findings ``<h2>`` and its closing ``</section>``.
	Otherwise the 'move a sub-section' part of the original user
	request isn't satisfied."""
	template = _read_template()
	findings_heading_idx = template.find("<h2>Findings - what to fix</h2>")
	observations_heading_idx = template.find(
		"<h3>Framework-level observations"
	)
	assert findings_heading_idx > 0, "Findings heading not found"
	assert observations_heading_idx > 0, "Observations heading not found"
	# Walk back from the Observations <h3> to its opening
	# <section class="subsection"> tag.
	subsection_idx = template.rfind(
		'<section class="subsection">', 0, observations_heading_idx
	)
	assert subsection_idx > 0, "Observations subsection opening not found"
	assert subsection_idx > findings_heading_idx, (
		"Observations subsection must be nested after Findings' heading"
	)

	# And the Findings section's closing </section> must come AFTER the
	# subsection closes (proving containment, not just ordering).
	sub_close_idx = template.find("</section>", subsection_idx)
	assert sub_close_idx > 0
	findings_close_after_sub = template.find("</section>", sub_close_idx + 1)
	assert findings_close_after_sub > sub_close_idx, (
		"Findings <section> must wrap (contain) the Observations subsection"
	)


def test_phase2_section_appears_before_findings():
	"""v0.6.x: the Line-Level Drilldown is the report's most distinctive
	section - it must be hoisted above the actionable Findings list so
	readers see it immediately after the Summary.

	v0.7.x Phase J.16: anchor on the
	``report_data.line_drilldown_html | safe`` Jinja injection point
	(renamed from ``report_data.phase2_html | safe`` in J.2.6, itself
	renamed from the legacy top-level ``phase2_html``)."""
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
	Summary section."""
	template = _read_template()
	tldr_idx = template.find('<div class="tldr">')
	stats_idx = template.find('<div class="kpis">')
	summary_idx = template.find("<h2>Summary</h2>")
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
	footer)."""
	template = _read_template()
	how_to_read_idx = template.find("<h2>How to read this report</h2>")
	db_tables_idx = template.find("<h2>Time spent per database table</h2>")
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
	the numeric columns off the right edge of the page."""
	template = _read_template()
	heading_idx = template.find("<h2>Per-action breakdown</h2>")
	assert heading_idx > 0, "Per-action breakdown heading missing"
	table_idx = template.find("<table", heading_idx)
	assert table_idx > 0, "Per-action breakdown table missing"
	tag_end = template.find(">", table_idx)
	open_tag = template[table_idx:tag_end]
	assert "tbl-clip" in open_tag and "per-action-table" in open_tag, (
		f"Per-action breakdown table must carry tbl-clip + "
		f"per-action-table classes; got: {open_tag!r}"
	)
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


def test_how_to_read_fixes_stale_refs_and_verbs():
	"""v0.7.x: How-to-read fixed — 'Click' (not 'Hover') to open callsites, and
	no references to sections that don't exist by those names / a non-existent
	'index candidate per table'."""
	template = _read_template()
	hr = template[template.find("<h2>How to read this report</h2>"):]
	hr = hr[:hr.find("</section>")]
	assert "Click any" in hr
	assert "Hover any" not in hr
	assert "full-recordings" not in hr
	assert "queries-per-action" not in hr
	assert "index candidate per table" not in hr
