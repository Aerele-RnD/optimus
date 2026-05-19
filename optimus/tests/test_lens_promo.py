# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.7.x Aerele Lens companion-tool callout.

A small one-line promo block sits right under the Jump-to nav in the
report header area, pointing at lens.aerele.in. It's hardcoded in the
template (no Optimus Settings toggle by design — single focused
mention, not a configurable list).

These tests pin the block's presence, position relative to the Jump-to
nav, link target / safety attributes, copy fragments, and the
self-contained-HTML guarantee (no remote assets fetched at render
time)."""

from types import SimpleNamespace

from optimus import renderer


def _doc():
	return SimpleNamespace(
		name="PS-lens",
		session_uuid="lens-uuid",
		title="lens promo test",
		user="tester@example.com",
		status="Ready",
		started_at="2026-05-14T00:00:00",
		stopped_at="2026-05-14T00:00:05",
		notes=None,
		top_severity="Low",
		summary_html=None,
		total_duration_ms=1000,
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
		findings=[],
		phase_2_runs=[],
	)


class TestLensPromoRendering:
	def test_block_renders_exactly_once(self):
		"""Promo is hardcoded — should appear on every report, exactly once."""
		html = renderer.render_raw(_doc(), recordings=[])
		assert html.count('class="section lens-promo"') == 1

	def test_block_positioned_before_jump_to_nav(self):
		"""The Lens block must sit BEFORE the Jump-to nav (sibling, above
		it). v0.7.x Phase A: nav is `<nav class="nav-pills">`; anchor on
		that opening tag."""
		html = renderer.render_raw(_doc(), recordings=[])
		jump_idx = html.find('<nav class="nav-pills">')
		lens_idx = html.find('class="section lens-promo"')
		assert jump_idx != -1, "Jump-to nav-pills not found"
		assert lens_idx != -1, "Lens promo block not found"
		assert lens_idx < jump_idx, (
			"Lens promo block must render BEFORE the Jump-to nav, not after"
		)

	def test_link_target_and_safety(self):
		"""Link goes to https://lens.aerele.in/ and carries rel=noopener."""
		html = renderer.render_raw(_doc(), recordings=[])
		assert 'href="https://lens.aerele.in/"' in html
		# rel="noopener" must appear inside the same <a> tag.
		anchor_start = html.find('href="https://lens.aerele.in/"')
		anchor_end = html.find(">", anchor_start)
		anchor_tag = html[anchor_start:anchor_end]
		assert 'rel="noopener"' in anchor_tag, (
			"Lens link must carry rel=\"noopener\" to prevent window.opener "
			"leaks on click"
		)

	def test_copy_fragments_present(self):
		"""Spot-check the two load-bearing copy elements so an accidental
		edit doesn't quietly strip the brand or the value prop."""
		html = renderer.render_raw(_doc(), recordings=[])
		assert "Aerele Lens" in html
		# "Audit" anchors the value proposition phrase used in the hero.
		assert "Audit" in html

	def test_block_is_self_contained(self):
		"""The Lens block must be inert text + a single <a> tag. No
		<img>, <link rel="...">, or <script> — those would break the
		saved-HTML offline guarantee on click-free render."""
		html = renderer.render_raw(_doc(), recordings=[])
		start = html.find('<aside class="section lens-promo"')
		assert start != -1, "Lens promo block opening tag not found"
		end = html.find("</aside>", start)
		assert end != -1, "Lens promo block has no closing </aside> tag"
		block = html[start:end]
		assert "<img " not in block
		assert "<link " not in block
		assert "<script" not in block.lower()
		# The single anchor element is fine — it's inert until clicked.
		assert block.count("<a ") == 1
