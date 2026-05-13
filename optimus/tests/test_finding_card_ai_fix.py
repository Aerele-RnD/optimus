# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.6.0 "Suggested fix (AI)" block in the finding card.

Exercises renderer.render_raw end to end so the macro runs in its real
context: when a Optimus Finding row carries an ``llm_fix_json``, the card
renders the Markdown suggestion (sanitized) under a distinct header with a
"review before applying" footer; when it doesn't, no AI block appears.

The fixture supplies a ``source_snippet`` on the callsite so the renderer
never takes the lazy file-read path (which imports ``optimus.analyze``
and would be flaky under the test-suite's known module-reload pollution).
"""

import json
from types import SimpleNamespace

from optimus import renderer

_SNIPPET = [
	{"lineno": 41, "content": "for u in users:"},
	{"lineno": 42, "content": "    frappe.db.get_value('User', u)"},
	{"lineno": 43, "content": "    process(u)"},
]


def _finding(*, llm_fix=None, finding_type="N+1 Query"):
	detail = {
		"callsite": {
			"filename": "/abs/myapp/foo.py",
			"lineno": 42,
			"function": "bulk",
			"source_snippet": _SNIPPET,
		},
	}
	return SimpleNamespace(
		finding_type=finding_type,
		severity="High",
		title="Same query ran 50× at foo.py:42",
		customer_description="A query repeats inside a loop.",
		estimated_impact_ms=420.0,
		affected_count=50,
		action_ref="0",
		technical_detail_json=json.dumps(detail),
		llm_fix_json=json.dumps(llm_fix) if llm_fix is not None else None,
	)


def _doc(findings):
	return SimpleNamespace(
		name="PS-test",
		session_uuid="test-uuid",
		title="test",
		user="tester@example.com",
		status="Ready",
		started_at="2026-05-11T00:00:00",
		stopped_at="2026-05-11T00:00:05",
		notes=None,
		top_severity="High",
		summary_html=None,
		total_duration_ms=5000,
		total_query_time_ms=0,
		total_queries=0,
		total_requests=1,
		top_queries_json="[]",
		table_breakdown_json="[]",
		hot_frames_json=None,
		session_time_breakdown_json=None,
		total_python_ms=None,
		total_sql_ms=None,
		analyzer_warnings=None,
		v5_aggregate_json="{}",
		actions=[],
		findings=findings,
		phase_2_runs=[],
	)


_GOOD_SUGGESTION = (
	"**Diagnosis**\n\nThe query runs once per row.\n\n"
	"**Fix**\n\n```python\nrows = frappe.get_all('User', filters={'name': ('in', names)})\n```\n\n"
	"- batch the lookups\n- index `tabUser.name` (already PK)\n\n"
	"**Why it works**\n\nOne round-trip instead of N."
)


# --------------------------------------------------------------------------
# renderer._markdown_to_safe_html — unit (markdown → sanitized HTML)
# --------------------------------------------------------------------------

class TestMarkdownToSafeHtml:
	def test_converts_basic_markdown(self):
		out = renderer._markdown_to_safe_html("**bold** and a list:\n\n- one\n- two\n")
		# Either the real converter ran (preferred) or — under module
		# pollution — the escaped <pre> fallback; both are non-empty and
		# never contain a live <script>.
		assert out
		assert "<script" not in out.lower()

	def test_strips_script_tags(self):
		out = renderer._markdown_to_safe_html("ok\n\n<script>alert('x')</script>\n\ndone")
		assert "<script" not in out.lower()
		assert "alert('x')" not in out and "alert(&#39;x&#39;)" not in out

	def test_none_input_is_safe(self):
		assert renderer._markdown_to_safe_html(None) is not None

	def test_diff_fence_gets_per_line_highlight_spans(self):
		md = "**Fix**\n\n```diff\n-old_line()\n+new_line()\n```\n"
		out = renderer._markdown_to_safe_html(md)
		assert 'class="dh-line dh-del">-old_line()' in out
		assert 'class="dh-line dh-add">+new_line()' in out
		assert '<pre class="dh">' in out

	def test_non_diff_code_block_is_untouched(self):
		out = renderer._markdown_to_safe_html("```python\nx = compute()\nreturn x\n```\n")
		assert "dh-line" not in out
		assert 'pre class="dh"' not in out


class TestHighlightDiffHtml:
	def test_plain_diff_pre_block(self):
		html = '<pre><code class="diff language-diff">-a\n+b\n@@ ctx @@\n c\n</code></pre>'
		out = renderer._highlight_diff_html(html)
		assert '<span class="dh-line dh-del">-a</span>' in out
		assert '<span class="dh-line dh-add">+b</span>' in out
		assert '<span class="dh-line dh-meta">@@ ctx @@</span>' in out
		assert '<span class="dh-line dh-ctx"> c</span>' in out

	def test_unmarked_code_block_with_plus_minus_lines_is_detected(self):
		html = "<pre><code>-removed\n+added\n</code></pre>"
		out = renderer._highlight_diff_html(html)
		assert "dh-del" in out and "dh-add" in out

	def test_regular_code_block_left_alone(self):
		html = '<pre><code class="language-python">x = a - b\nreturn x\n</code></pre>'
		assert renderer._highlight_diff_html(html) == html

	def test_no_pre_blocks_is_noop(self):
		assert renderer._highlight_diff_html("<p>just text</p>") == "<p>just text</p>"


# --------------------------------------------------------------------------
# finding_card AI block (end-to-end via render_raw)
# --------------------------------------------------------------------------

class TestAiFixBlockRendering:
	def test_block_renders_when_llm_fix_present(self):
		doc = _doc([_finding(llm_fix={
			"suggestion": _GOOD_SUGGESTION,
			"model": "claude-sonnet-4-6",
			"provider": "Anthropic",
			"generated_at": "2026-05-11T00:00:00+00:00",
		})])
		html = renderer.render_raw(doc, recordings=[])
		# Header + model name.
		assert "Suggested fix (AI" in html
		assert "claude-sonnet-4-6" in html
		# The suggestion text made it into the page (converted-markdown or
		# escaped-pre fallback — both contain the words).
		assert "Diagnosis" in html and "Why it works" in html
		assert "batch the lookups" in html
		# Disclaimer footer.
		assert "review before applying" in html

	def test_no_block_when_llm_fix_absent(self):
		doc = _doc([_finding(llm_fix=None)])
		html = renderer.render_raw(doc, recordings=[])
		assert "Suggested fix (AI" not in html
		assert "review before applying" not in html

	def test_no_block_when_findings_section_toggle_off(self):
		# v0.6.x per-section hard off: even with llm_fix_json populated, the
		# renderer strips f.llm_fix when ai_suggest_findings is off so the
		# AI-fix block is omitted on re-render of a session analyzed earlier
		# while the section was on.
		from unittest.mock import patch

		from optimus import settings
		doc = _doc([_finding(llm_fix={
			"suggestion": _GOOD_SUGGESTION, "model": "claude-sonnet-4-6",
			"provider": "Anthropic", "generated_at": "2026-05-11T00:00:00+00:00",
		})])
		with patch("optimus.settings.get_config",
		           return_value=settings.OptimusConfig(ai_suggest_findings=False)):
			html = renderer.render_raw(doc, recordings=[])
		assert "Suggested fix (AI" not in html
		assert "review before applying" not in html
		# Sanity: with the toggle back on, the block IS there.
		with patch("optimus.settings.get_config",
		           return_value=settings.OptimusConfig(ai_suggest_findings=True)):
			html2 = renderer.render_raw(doc, recordings=[])
		assert "Suggested fix (AI" in html2

	def test_no_block_when_llm_fix_json_is_empty_object(self):
		doc = _doc([_finding(llm_fix={})])
		html = renderer.render_raw(doc, recordings=[])
		assert "Suggested fix (AI" not in html

	def test_no_block_when_suggestion_blank(self):
		doc = _doc([_finding(llm_fix={"suggestion": "   ", "model": "m"})])
		html = renderer.render_raw(doc, recordings=[])
		assert "Suggested fix (AI" not in html

	def test_script_in_suggestion_is_stripped(self):
		doc = _doc([_finding(llm_fix={
			"suggestion": "Here is a fix.\n\n<script>alert('xss')</script>\n\nThanks.",
			"model": "local-model",
			"provider": "OpenAI-compatible",
			"generated_at": "2026-05-11T00:00:00+00:00",
		})])
		html = renderer.render_raw(doc, recordings=[])
		assert "Suggested fix (AI" in html
		# The <script> tag is gone (sanitized server-side), in any rendering path.
		assert "<script" not in html.lower()
		assert "alert('xss')" not in html and "alert(&#39;xss&#39;)" not in html

	def test_no_external_urls_in_the_ai_block(self):
		doc = _doc([_finding(llm_fix={
			"suggestion": _GOOD_SUGGESTION, "model": "m", "provider": "OpenAI",
			"generated_at": "2026-05-11T00:00:00+00:00",
		})])
		html = renderer.render_raw(doc, recordings=[])
		# Bound the check to the AI block region (the report has vscode://
		# callsite links elsewhere).
		start = html.find("Suggested fix (AI")
		end = html.find("review before applying", start)
		assert start != -1 and end != -1
		block = html[start:end]
		assert "http://" not in block and "https://" not in block
		assert "<script" not in block.lower()
		assert "<link " not in block

	def test_only_eligible_card_gets_a_block(self):
		# Two findings, only one has a suggestion → exactly one AI block.
		doc = _doc([
			_finding(llm_fix={"suggestion": _GOOD_SUGGESTION, "model": "m",
			                  "provider": "OpenAI", "generated_at": "t"}, finding_type="N+1 Query"),
			_finding(llm_fix=None, finding_type="Slow Query"),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert html.count('class="ai-fix"') == 1

	def test_directional_caution_shown_when_source_unavailable(self):
		doc = _doc([_finding(llm_fix={
			"suggestion": _GOOD_SUGGESTION, "model": "m", "provider": "OpenAI",
			"generated_at": "t", "source_available": False,
		})])
		html = renderer.render_raw(doc, recordings=[])
		assert 'class="ai-fix-caution"' in html
		assert "directional guidance" in html

	def test_no_caution_when_source_available_or_legacy_row(self):
		# Explicit True — no caution div (the CSS rule is always in <style>,
		# so match on the element, not the class name).
		doc = _doc([_finding(llm_fix={
			"suggestion": _GOOD_SUGGESTION, "model": "m", "provider": "OpenAI",
			"generated_at": "t", "source_available": True,
		})])
		assert 'class="ai-fix-caution"' not in renderer.render_raw(doc, recordings=[])
		# Legacy row with no such key → defaults to "had context", no caution.
		doc2 = _doc([_finding(llm_fix={
			"suggestion": _GOOD_SUGGESTION, "model": "m", "provider": "OpenAI", "generated_at": "t",
		})])
		assert 'class="ai-fix-caution"' not in renderer.render_raw(doc2, recordings=[])
