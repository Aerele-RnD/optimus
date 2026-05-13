# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for `analyze._build_summary_html` — the plain-language "Summary"
prose at the top of the report.

It must read for a non-developer: "operations" not "actions", humanized
action names ("Submit Sales Invoice") not raw `cmd:action` strings,
"high priority" not "high-severity", and a finding's raw `cmd:action`
reference swapped for the humanized form. The issue count must equal the
sum of the high/medium/low breakdown.
"""

import json
import re

from optimus import analyze
from optimus.analyzers.base import AnalyzeContext


def _ctx(actions, findings):
	ctx = AnalyzeContext(session_uuid="u", docname="d")
	ctx.actions = actions
	ctx.findings = findings
	return ctx


def _strip(html: str) -> str:
	return re.sub(r"<[^>]+>", "", html)


# A recording whose humanized label is "Submit Sales Invoice".
_SAVEDOCS_REC = {
	"uuid": "r-save",
	"cmd": "frappe.desk.form.save.savedocs",
	"form_dict": {"action": "Submit", "doc": json.dumps({"doctype": "Sales Invoice"})},
}


class TestSummaryProse:
	def test_humanizes_slowest_action_and_drops_jargon(self):
		ctx = _ctx(
			actions=[
				{"action_label": "frappe.desk.search.search_link", "recording_uuid": "r0", "duration_ms": 100},
				{"action_label": "frappe.desk.form.save.savedocs:Submit", "recording_uuid": "r-save", "duration_ms": 1554},
			],
			findings=[
				{"severity": "Medium", "action_ref": "1", "estimated_impact_ms": 751,
				 "title": "In frappe.desk.form.save.savedocs:Submit, 48% of the time was spent in looped_validate",
				 "finding_type": "Slow Hot Path"},
				{"severity": "High", "action_ref": "0", "estimated_impact_ms": 300,
				 "title": "Same query ran 50x", "finding_type": "N+1 Query"},
			],
		)
		html = analyze._build_summary_html(ctx, 1702, [{"uuid": "r0"}, _SAVEDOCS_REC])
		text = _strip(html)
		# Plain-English: "operations", not "actions".
		assert "operation" in text and " actions" not in text
		# The slowest action is shown by its human label, not the raw cmd.
		assert "Submit Sales Invoice" in text
		assert "frappe.desk.form.save.savedocs" not in html
		# The finding's title had the raw cmd swapped for the human label too.
		assert "looped_validate" in text  # the function name stays — it's the actionable bit
		# "priority", not "severity".
		assert "priority" in text and "severity" not in text.lower()
		# The query count is still there.
		assert "1702 database queries" in text

	def test_issue_count_equals_severity_breakdown(self):
		ctx = _ctx(
			actions=[{"action_label": "x", "recording_uuid": "r0", "duration_ms": 10}],
			findings=[
				{"severity": "High", "title": "a", "estimated_impact_ms": 5},
				{"severity": "High", "title": "b", "estimated_impact_ms": 4},
				{"severity": "Medium", "title": "c", "estimated_impact_ms": 3},
				{"severity": "Low", "title": "d", "estimated_impact_ms": 2},
			],
		)
		text = _strip(analyze._build_summary_html(ctx, 5, [{"uuid": "r0"}]))
		# 4 total = 2 high + 1 medium + 1 low.
		assert "4 potential issues" in text
		assert "2 high priority" in text and "1 medium" in text and "1 minor" in text
		assert "high-severity" not in text

	def test_no_findings_message(self):
		ctx = _ctx(
			actions=[{"action_label": "GET /app", "recording_uuid": "r0", "duration_ms": 50}],
			findings=[],
		)
		text = _strip(analyze._build_summary_html(ctx, 3, [{"uuid": "r0"}]))
		assert "nothing significant" in text.lower()
		assert "potential issue" not in text

	def test_works_without_recordings(self):
		# Recordings can TTL out before a re-render — fall back to the raw label.
		ctx = _ctx(
			actions=[{"action_label": "frappe.client.save:SO-0001", "recording_uuid": "gone", "duration_ms": 200}],
			findings=[{"severity": "High", "title": "Same query ran 30x", "estimated_impact_ms": 100, "action_ref": "0"}],
		)
		text = _strip(analyze._build_summary_html(ctx, 10, []))  # no recordings
		assert "frappe.client.save:SO-0001" in text  # raw fallback
		assert "1 potential issue" in text

	def test_single_operation_singular(self):
		ctx = _ctx(actions=[{"action_label": "GET /app", "recording_uuid": "r0", "duration_ms": 5}], findings=[])
		text = _strip(analyze._build_summary_html(ctx, 1, [{"uuid": "r0"}]))
		assert "1 operation " in text and "1 operations" not in text
