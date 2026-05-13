# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""v0.6.x: tests for the "custom prominent, framework collapsed" split.

The 4 main leaderboard sections (Per-action breakdown, Top queries,
Background jobs, Hot frames) all pre-split their rows in
``renderer.render()`` into ``<name>`` (custom-app rows) + ``<name>_framework``
(framework-app rows). The template renders the primary list in a normal
<table> and the framework list in a collapsed <details class="subsection">
below. These tests cover both the unit-level classifier and the render-level
behaviour for each section.
"""

import json
import types
from unittest.mock import patch

from optimus import renderer
from optimus.settings import OptimusConfig

# --------------------------------------------------------------------------
# _is_framework_app — the tiny classifier adapter
# --------------------------------------------------------------------------


class TestIsFrameworkApp:
	def test_bare_framework_app_name(self):
		assert renderer._is_framework_app("frappe") is True
		assert renderer._is_framework_app("erpnext") is True
		assert renderer._is_framework_app("hrms") is True

	def test_bare_custom_app_name(self):
		assert renderer._is_framework_app("myapp") is False
		assert renderer._is_framework_app("ugly_code") is False

	def test_full_framework_filename(self):
		assert renderer._is_framework_app("apps/frappe/frappe/handler.py") is True
		assert renderer._is_framework_app("apps/erpnext/erpnext/accounts/sales_invoice.py") is True

	def test_full_custom_filename(self):
		assert renderer._is_framework_app("apps/myapp/myapp/handlers.py") is False

	def test_empty_or_none_treated_as_custom(self):
		# Unattributable rows should NOT be buried in the framework block.
		assert renderer._is_framework_app("") is False
		assert renderer._is_framework_app(None) is False
		assert renderer._is_framework_app("   ") is False

	def test_tracked_apps_inclusion_mode_flips_semantics(self):
		# When tracked_apps is set: ONLY listed apps are custom. Everything
		# else (incl. erpnext) routes to framework.
		assert renderer._is_framework_app("myapp", tracked_apps=("myapp",)) is False
		assert renderer._is_framework_app("erpnext", tracked_apps=("myapp",)) is True
		assert renderer._is_framework_app("apps/myapp/foo.py", tracked_apps=("myapp",)) is False
		assert renderer._is_framework_app("apps/erpnext/foo.py", tracked_apps=("myapp",)) is True


class TestSplitByFrameworkApp:
	def test_preserves_order_within_each_bucket(self):
		rows = [
			{"name": "c1", "app": "myapp"},
			{"name": "f1", "app": "frappe"},
			{"name": "c2", "app": "myapp"},
			{"name": "f2", "app": "erpnext"},
		]
		custom, framework = renderer._split_by_framework_app(rows, lambda r: r["app"])
		assert [r["name"] for r in custom] == ["c1", "c2"]
		assert [r["name"] for r in framework] == ["f1", "f2"]

	def test_empty_input(self):
		assert renderer._split_by_framework_app([], lambda r: r) == ([], [])
		assert renderer._split_by_framework_app(None, lambda r: r) == ([], [])

	def test_key_raising_treated_as_custom(self):
		def boom(_):
			raise RuntimeError("bad row")

		rows = [{"name": "r1"}]
		custom, framework = renderer._split_by_framework_app(rows, boom)
		assert len(custom) == 1
		assert framework == []


# --------------------------------------------------------------------------
# Render-level: minimal fake session doc so the template runs end-to-end
# --------------------------------------------------------------------------


def _action_ns(**kw):
	base = {
		"action_label": "",
		"event_type": "HTTP Request",
		"http_method": "",
		"path": "",
		"recording_uuid": "",
		"duration_ms": 0,
		"queries_count": 0,
		"query_time_ms": 0,
		"slowest_query_ms": 0,
	}
	base.update(kw)
	return types.SimpleNamespace(**base)


def _doc(*, actions=None, top_queries=None, hot_frames=None):
	return types.SimpleNamespace(
		name="PS-split",
		session_uuid="split-uuid",
		title="split test",
		user="a@example.com",
		status="Ready",
		started_at="2026-05-12T00:00:00",
		stopped_at="2026-05-12T00:00:05",
		notes=None,
		top_severity="Low",
		summary_html=None,
		total_duration_ms=5000,
		total_query_time_ms=80,
		total_queries=5,
		total_requests=2,
		top_queries_json=json.dumps(top_queries or []),
		table_breakdown_json="[]",
		hot_frames_json=json.dumps(hot_frames or []),
		session_time_breakdown_json=None,
		total_python_ms=None,
		total_sql_ms=None,
		analyzer_warnings=None,
		v5_aggregate_json="{}",
		actions=actions or [],
		findings=[],
		phase_2_runs=[],
	)


class TestPerActionSplit:
	def test_custom_actions_above_framework_collapsed_block(self):
		# 1 custom action + 2 framework actions. Custom should render in the
		# primary <table>; the two framework rows should sit inside a
		# <details class="subsection">.
		doc = _doc(actions=[
			_action_ns(action_label="POST /api/method/myapp.handlers.recompute",
			           event_type="HTTP Request", http_method="POST",
			           path="/api/method/myapp.handlers.recompute",
			           recording_uuid="r0", duration_ms=300),
			_action_ns(action_label="POST /api/method/frappe.desk.form.save.savedocs",
			           event_type="HTTP Request", http_method="POST",
			           path="/api/method/frappe.desk.form.save.savedocs",
			           recording_uuid="r1", duration_ms=900),
			_action_ns(action_label="POST /api/method/frappe.client.get_value",
			           event_type="HTTP Request", http_method="POST",
			           path="/api/method/frappe.client.get_value",
			           recording_uuid="r2", duration_ms=120),
		])
		html = renderer.render_raw(doc, recordings=[])

		# Section heading still renders.
		assert "<h2>Per-action breakdown</h2>" in html
		# The framework sub-block exists and counts 2 actions.
		assert 'class="subsection"' in html
		assert "<strong>2</strong> framework actions" in html
		# All 3 actions still appear somewhere in the rendered HTML.
		assert "myapp.handlers.recompute" in html
		assert "frappe.desk.form.save.savedocs" in html
		assert "frappe.client.get_value" in html
		# The framework note copy is present (so the user understands why
		# they're buried).
		assert "the developer can't easily patch these" in html

	def test_no_framework_actions_no_collapsed_block(self):
		doc = _doc(actions=[
			_action_ns(action_label="POST /api/method/myapp.handlers.x",
			           event_type="HTTP Request", path="/api/method/myapp.handlers.x",
			           recording_uuid="r0", duration_ms=200),
		])
		html = renderer.render_raw(doc, recordings=[])
		# No subsection summary phrase for a section with 0 framework rows.
		assert "framework actions (click to expand)" not in html
		assert "framework action (click to expand)" not in html

	def test_tracked_apps_routes_erpnext_to_framework(self):
		# With tracked_apps=("myapp",): even erpnext is "framework". The
		# erpnext action should land in the collapsed sub-block.
		doc = _doc(actions=[
			_action_ns(action_label="POST /api/method/myapp.handlers.x",
			           event_type="HTTP Request", path="/api/method/myapp.handlers.x",
			           recording_uuid="r0", duration_ms=200),
			_action_ns(action_label="POST /api/method/erpnext.accounts.utils.recompute",
			           event_type="HTTP Request", path="/api/method/erpnext.accounts.utils.recompute",
			           recording_uuid="r1", duration_ms=400),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(tracked_apps=("myapp",)),
		):
			html = renderer.render_raw(doc, recordings=[])
		assert "<strong>1</strong> framework action " in html
		assert "erpnext.accounts.utils.recompute" in html


class TestTopQueriesSplit:
	def test_framework_callsite_query_routes_to_collapsed_block(self):
		# top_queries normally already excludes framework callsites at
		# analyze AND render time (``_filter_top_queries_for_display``).
		# Bypass the render-time filter so both queries reach the split —
		# this exercises the framework sub-block code path for the rare
		# case that a framework query slips through (older sessions).
		doc = _doc(top_queries=[
			{"callsite": "apps/frappe/frappe/db/__init__.py:42",
			 "duration_ms": 50.0, "normalized_query": "SELECT * FROM tabUser"},
			{"callsite": "apps/myapp/myapp/handlers.py:88",
			 "duration_ms": 80.0, "normalized_query": "SELECT * FROM tabSales Invoice"},
		])
		with patch.object(
			renderer, "_filter_top_queries_for_display",
			side_effect=lambda qs: qs,
		):
			html = renderer.render_raw(doc, recordings=[])

		# The myapp query is in the primary table; the frappe query is in
		# the collapsed framework block.
		assert "tabSales Invoice" in html
		assert "<strong>1</strong> framework query " in html
		assert "tabUser" in html


class TestBackgroundJobsSplit:
	def test_custom_and_framework_jobs_split(self):
		doc = _doc(actions=[
			_action_ns(action_label="Job: myapp.tasks.digest",
			           event_type="Background Job", path="myapp.tasks.digest",
			           recording_uuid="r1", duration_ms=400, queries_count=5),
			_action_ns(action_label="Job: frappe.email.queue.send",
			           event_type="Background Job", path="frappe.email.queue.send",
			           recording_uuid="r2", duration_ms=300, queries_count=3),
			_action_ns(action_label="Job: erpnext.accounts.utils.recompute",
			           event_type="Background Job", path="erpnext.accounts.utils.recompute",
			           recording_uuid="r3", duration_ms=200, queries_count=2),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>Background jobs</h2>" in html
		# 2 framework jobs collapsed.
		assert "<strong>2</strong> framework jobs" in html
		# All three methods still rendered somewhere.
		assert "myapp.tasks.digest" in html
		assert "frappe.email.queue.send" in html
		assert "erpnext.accounts.utils.recompute" in html

	def test_only_framework_jobs_shows_replacement_note(self):
		doc = _doc(actions=[
			_action_ns(action_label="Job: frappe.email.queue.send",
			           event_type="Background Job", path="frappe.email.queue.send",
			           recording_uuid="r1", duration_ms=300, queries_count=3),
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>Background jobs</h2>" in html
		assert "<strong>1</strong> framework job " in html
		# Replacement note when there are zero custom jobs.
		assert "all jobs ran in framework code" in html


class TestHotFramesSplit:
	def test_custom_above_framework_collapsed(self):
		doc = _doc(hot_frames=[
			{"function": "myapp/handlers.py::recompute", "total_ms": 200,
			 "occurrences": 5, "distinct_actions": 1},
			{"function": "frappe/model/document.py::run_method", "total_ms": 800,
			 "occurrences": 30, "distinct_actions": 12},
			{"function": "erpnext/accounts/sales_invoice.py::validate", "total_ms": 150,
			 "occurrences": 3, "distinct_actions": 1},
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "<h2>Hot frames" in html
		# 2 framework frames collapsed.
		assert "<strong>2</strong> framework frames" in html
		# All three function names still rendered.
		assert "recompute" in html
		assert "run_method" in html
		assert "validate" in html

	def test_no_framework_frames_no_collapsed_block(self):
		doc = _doc(hot_frames=[
			{"function": "myapp/handlers.py::foo", "total_ms": 50,
			 "occurrences": 2, "distinct_actions": 1},
		])
		html = renderer.render_raw(doc, recordings=[])
		assert "framework frames (click to expand)" not in html
		assert "framework frame (click to expand)" not in html
