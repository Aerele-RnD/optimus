# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.5.2 round 4 PDF preprocess that force-opens every
<details> block before handing HTML to wkhtmltopdf.

Background: wkhtmltopdf's old QtWebKit engine doesn't reliably honor
@media print overrides on the <details> disclosure element, so the
Observations subsection, Analyzer notes section, and any collapsed
app buckets would silently disappear from the generated PDF. We add
the ``open`` attribute to every <details> before rendering so the PDF
matches the browser's expanded state.
"""

import sys
import types

import pytest

# pdf_export.py imports frappe at module top. Stub it for this
# pure-string-transformation test — scoped to a per-test fixture so the
# stub doesn't leak to other test files (was a suite-wide pollution
# source before the conftest fence; even with the fence, doing this
# via fixture makes the contract explicit and auto-restoring).


@pytest.fixture(autouse=True)
def _frappe_stub(monkeypatch):
	monkeypatch.setitem(sys.modules, "frappe", types.ModuleType("frappe"))


# Import is safe under any sys.modules state because pdf_export only
# touches frappe inside its functions; we deferred the import below
# the fixture for clarity.
from optimus.pdf_export import _expand_collapsible_sections  # noqa: E402


class TestExpandCollapsibles:
	def test_adds_open_to_plain_details(self):
		html = "<details><summary>x</summary>body</details>"
		assert _expand_collapsible_sections(html) == (
			"<details open><summary>x</summary>body</details>"
		)

	def test_adds_open_to_details_with_class(self):
		html = '<details class="section"><summary>x</summary>body</details>'
		out = _expand_collapsible_sections(html)
		assert 'open' in out
		# Must keep the class attribute intact.
		assert 'class="section"' in out
		# Shape sanity: one <details> tag, one `open` attribute.
		assert out.count("<details") == 1
		assert out.count(" open>") == 1

	def test_adds_open_to_details_with_id(self):
		html = '<details id="analyzer-notes"><summary>x</summary>body</details>'
		out = _expand_collapsible_sections(html)
		assert 'id="analyzer-notes"' in out
		assert ' open>' in out

	def test_idempotent_on_already_open_details(self):
		"""<details open> should pass through unchanged (not double-add)."""
		html = "<details open><summary>x</summary>body</details>"
		assert _expand_collapsible_sections(html) == html

	def test_idempotent_on_class_and_open(self):
		html = '<details class="section" open><summary>x</summary>body</details>'
		out = _expand_collapsible_sections(html)
		# No duplicate `open`, no attribute reordering side-effects.
		assert out.count("open") == 1

	def test_handles_multiple_details_blocks(self):
		html = (
			'<details class="section"><summary>A</summary>a</details>'
			'<details class="subsection"><summary>B</summary>b</details>'
			'<details class="section" open><summary>C</summary>c</details>'
		)
		out = _expand_collapsible_sections(html)
		# All three end up open. The already-open one stays unchanged.
		assert out.count(" open>") == 3

	def test_does_not_touch_other_tags(self):
		html = '<div><summary>x</summary><p>y</p></div>'
		assert _expand_collapsible_sections(html) == html

	def test_preserves_case_insensitive_open_check(self):
		"""Defensive: <details OPEN> is valid HTML and shouldn't get a
		second open attribute."""
		html = "<details OPEN><summary>x</summary>body</details>"
		out = _expand_collapsible_sections(html)
		# Original OPEN preserved, no lowercase `open` appended.
		assert out == html, (
			f"Already-OPEN details must pass through unchanged; got: {out!r}"
		)


class TestFullReportPreprocess:
	"""End-to-end: feed a snippet that mirrors the report template
	structure and verify every section ends up open."""

	def test_nested_details_all_opened(self):
		html = """
		<details class="section">
		  <summary>Findings</summary>
		  <details class="subsection">
		    <summary>myapp</summary>
		    findings here
		  </details>
		  <details class="subsection">
		    <summary>Observations</summary>
		    <details class="subsection">
		      <summary>frappe</summary>
		      observations here
		    </details>
		  </details>
		</details>
		<details class="section" id="analyzer-notes">
		  <summary>Analyzer notes</summary>
		  bullets
		</details>
		""".strip()
		out = _expand_collapsible_sections(html)
		# 5 <details> in the source → 5 ` open>` in the output.
		assert out.count(" open>") == 5
		# The id attribute survives.
		assert 'id="analyzer-notes"' in out
