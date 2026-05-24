# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Server Script render-pipeline integration (display label + Desk link +
DB-read source snippet + own app bucket).

Frappe's ``safe_exec`` (apps/frappe/frappe/utils/safe_exec.py:49,118) compiles
Server Scripts with a synthetic filename ``<serverscript>: <scrubbed-name>``.
The capture + analyze halves of Optimus already treat these as user code
(call_tree.py:475-495 explicitly carves Server Scripts out of the plumbing
filter). The renderer is what was lacking — labels collapsed to a generic
``<server-script body>``, app bucketing went to ``[other]``, no source
snippet, no editor link.

This file covers the new ``optimus/server_script_source.py`` helper + the
wired-in label / bucketing / snippet / link behavior.
"""

import re
import types

import pytest

from optimus import server_script_source as sss
from optimus.analyzers import call_tree


@pytest.fixture
def fake_frappe_db(monkeypatch):
	"""Stub ``frappe.db.sql`` so ``get_server_script_record`` can be exercised
	without a real Frappe site. The fixture returns a mutable
	``{rows: ...}`` dict the test can set to drive the SQL response."""
	import frappe

	state = {"rows": []}

	def fake_sql(query, params=None, as_dict=False, **kw):
		return list(state["rows"])

	# frappe.db is a Werkzeug Local proxy per [[feedback_frappe_db_local_proxy]];
	# replace it wholesale rather than patching its attributes.
	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(sql=fake_sql), raising=False)
	return state


# ---------------------------------------------------------------------------
# extract_script_name + is_server_script_filename
# ---------------------------------------------------------------------------


class TestExtractScriptName:
	def test_parses_named_server_script_filename(self):
		assert sss.extract_script_name("<serverscript>: my_script") == "my_script"
		assert sss.extract_script_name("<serverscript>:  my_script ") == "my_script"

	def test_bare_server_script_returns_none(self):
		# Frappe writes a bare ``<serverscript>`` when no script_filename
		# is passed — nothing to look up, so callers skip.
		assert sss.extract_script_name("<serverscript>") is None
		assert sss.extract_script_name("<serverscript> ") is None

	def test_non_server_script_filename_returns_none(self):
		for f in (
			"apps/frappe/frappe/handler.py",
			"/Users/.../bench/apps/x/y.py",
			"<frozen importlib._bootstrap>",
			"<string>",
			"",
			None,
		):
			assert sss.extract_script_name(f) is None

	def test_is_server_script_filename_covers_named_and_bare(self):
		assert sss.is_server_script_filename("<serverscript>") is True
		assert sss.is_server_script_filename("<serverscript>: foo") is True
		assert sss.is_server_script_filename("apps/frappe/handler.py") is False
		assert sss.is_server_script_filename(None) is False


# ---------------------------------------------------------------------------
# get_server_script_record / get_server_script_lines / desk_url
# ---------------------------------------------------------------------------


class TestGetServerScriptRecord:
	def test_resolves_via_db(self, fake_frappe_db):
		fake_frappe_db["rows"] = [
			{"name": "My Server Script", "script": "x = 1\nfor i in range(10):\n    print(i)"}
		]
		rec = sss.get_server_script_record("my_server_script")
		assert rec is not None
		assert rec["name"] == "My Server Script"
		assert "for i in range(10)" in rec["script"]

	def test_returns_none_when_db_has_no_row(self, fake_frappe_db):
		fake_frappe_db["rows"] = []
		assert sss.get_server_script_record("nonexistent") is None

	def test_returns_none_for_empty_scrubbed_name(self, fake_frappe_db):
		# Don't even hit the DB for a blank name.
		assert sss.get_server_script_record("") is None
		assert sss.get_server_script_record(None) is None  # type: ignore[arg-type]

	def test_swallows_db_errors(self, monkeypatch):
		import frappe

		def boom(*a, **kw):
			raise RuntimeError("db is on fire")

		monkeypatch.setattr(frappe, "db", types.SimpleNamespace(sql=boom), raising=False)
		# Must NOT raise — best-effort guarantee.
		assert sss.get_server_script_record("my_script") is None

	def test_cache_memoizes_lookup(self, fake_frappe_db):
		fake_frappe_db["rows"] = [{"name": "Foo", "script": "pass"}]
		cache: dict = {}
		first = sss.get_server_script_record("foo", cache=cache)
		# Mutate the fixture; second call should NOT re-hit DB.
		fake_frappe_db["rows"] = [{"name": "DIFFERENT", "script": "x = 1"}]
		second = sss.get_server_script_record("foo", cache=cache)
		assert second == first  # cached value


class TestGetServerScriptLines:
	def test_splits_body_into_lines(self, fake_frappe_db):
		fake_frappe_db["rows"] = [{"name": "Foo", "script": "a\nb\nc"}]
		lines = sss.get_server_script_lines("foo")
		assert lines == ["a", "b", "c"]

	def test_none_when_record_missing(self, fake_frappe_db):
		fake_frappe_db["rows"] = []
		assert sss.get_server_script_lines("nope") is None

	def test_none_when_script_field_empty(self, fake_frappe_db):
		fake_frappe_db["rows"] = [{"name": "Foo", "script": ""}]
		assert sss.get_server_script_lines("foo") is None


class TestDeskUrl:
	def test_resolved_record_links_to_form(self, fake_frappe_db):
		fake_frappe_db["rows"] = [{"name": "My Server Script", "script": "x"}]
		assert sss.desk_url("my_server_script") == "/app/server-script/My Server Script"

	def test_unresolved_falls_back_to_list_page(self, fake_frappe_db):
		fake_frappe_db["rows"] = []
		assert sss.desk_url("nonexistent") == "/app/server-script"


# ---------------------------------------------------------------------------
# call_tree.py display label + app bucketing
# ---------------------------------------------------------------------------


class TestCallTreeDisplay:
	def test_display_label_includes_script_name_and_lineno(self):
		"""Named Server Script frames must surface their name + lineno in the
		call-tree display label, replacing the old opaque ``<server-script
		body>`` blob."""
		node = {
			"filename": "<serverscript>: my_script",
			"function": "",
			"lineno": 42,
		}
		label = call_tree._display_name_for_node(node)
		assert "my_script" in label, f"label missing script name: {label!r}"
		assert "42" in label, f"label missing lineno: {label!r}"
		# Should not still emit the literal "body" blob.
		assert label != "<server-script body>"

	def test_display_label_for_bare_server_script_keeps_fallback(self):
		"""Bare ``<serverscript>`` (no name) has nothing better to show — keep
		a generic label (no crash, no name confusion)."""
		node = {"filename": "<serverscript>", "function": "", "lineno": None}
		label = call_tree._display_name_for_node(node)
		# Tolerant assertion: anything that's not blank + identifies it as a
		# Server Script context is fine.
		assert "server" in label.lower()


class TestAppBucketing:
	def test_named_server_script_buckets_to_server_scripts(self):
		# Server Scripts have no meaningful ``function`` name (safe_exec
		# compiles with function=""). The filename carries the identity.
		assert call_tree._top_level_app("", "<serverscript>: my_script") == "Server Scripts"

	def test_bare_server_script_buckets_to_server_scripts(self):
		# Even without a name, it's still a Server Script — better than the
		# anonymous [other] bucket.
		assert call_tree._top_level_app("", "<serverscript>") == "Server Scripts"

	def test_other_angle_bracket_filenames_still_route_to_other(self):
		"""Regression guard: don't widen the carve-out beyond Server Scripts —
		``<string>`` / ``<frozen …>`` etc. must still go to ``[other]``."""
		assert call_tree._top_level_app("", "<string>") == "[other]"
		assert call_tree._top_level_app("", "<frozen importlib._bootstrap>") == "[other]"


# ---------------------------------------------------------------------------
# renderer.py snippet + callsite link integration
# ---------------------------------------------------------------------------


class TestRendererSnippetAndLink:
	def test_resolve_source_path_returns_server_script_sentinel(self, fake_frappe_db):
		"""``_resolve_source_path`` must distinguish Server Scripts from
		``None`` so downstream snippet readers + link builders can branch."""
		from optimus import renderer

		fake_frappe_db["rows"] = [{"name": "Foo", "script": "a\nb"}]
		resolved = renderer._resolve_source_path("<serverscript>: foo")
		# Tuple sentinel — distinguishes from str (real path) and None.
		assert resolved is not None
		assert resolved != "<serverscript>: foo"  # not just the raw filename
		# Sentinel shape: ("server_script", scrubbed_name)
		assert isinstance(resolved, tuple)
		assert resolved[0] == "server_script"
		assert resolved[1] == "foo"

	def test_read_function_body_snippet_reads_server_script_from_db(self, fake_frappe_db):
		from optimus import renderer

		fake_frappe_db["rows"] = [
			{
				"name": "Foo",
				"script": "def go():\n    for u in users:\n        do(u)\n",
			}
		]
		snippet = renderer._read_function_body_snippet("<serverscript>: foo", 1)
		assert snippet, "no snippet returned for a resolvable Server Script"
		# First entry is the def line; body should follow.
		contents = " ".join(r["content"] for r in snippet)
		assert "for u in users" in contents

	def test_read_source_snippet_returns_window_for_server_script(self, fake_frappe_db):
		from optimus import renderer

		body = "\n".join(f"line_{i}" for i in range(1, 11))  # 10 lines
		fake_frappe_db["rows"] = [{"name": "Foo", "script": body}]
		snippet = renderer._read_source_snippet("<serverscript>: foo", 5)
		assert snippet, "no ±2 window returned"
		linenos = [r["lineno"] for r in snippet]
		assert 5 in linenos
		assert min(linenos) >= 3 and max(linenos) <= 7  # ±2 window


# ---------------------------------------------------------------------------
# End-to-end: a Server Script finding renders with the snippet + Desk link.
# ---------------------------------------------------------------------------


class TestEndToEndServerScriptFinding:
	def test_finding_card_shows_snippet_and_desk_link(self, fake_frappe_db):
		"""Render a session with one finding whose callsite is a Server Script.
		The card must (a) link to the Desk Server Script form, (b) inline the
		Server Script's source body — not a vscode:// link, not a bare
		``<server-script body>`` blob."""
		from optimus import renderer

		fake_frappe_db["rows"] = [
			{
				"name": "Sales Invoice Submit Hook",
				"script": (
					"# A Server Script\n"
					"for u in frappe.db.get_all('User'):\n"
					"    frappe.get_doc('User', u.name)\n"
				),
			}
		]

		# Minimal finding + session doc shape (mirrors test_call_tree_render
		# / test_finding_card_ai_fix fixtures).
		finding = types.SimpleNamespace(
			finding_type="N+1 Query",
			severity="High",
			title="N+1 in Server Script",
			description="get_doc per row",
			customer_description="get_doc per row in a Server Script",
			actionable=True,
			estimated_impact_ms=400,
			affected_count=50,
			action_ref="",
			llm_fix_json=None,
			technical_detail_json=(
				'{"callsite": {"filename": "<serverscript>: sales_invoice_submit_hook", '
				'"lineno": 3, "function": "", "_abs": null}}'
			),
		)
		doc = types.SimpleNamespace(
			name="PS-TEST", title="T", session_uuid="t", user="a", status="Ready",
			started_at="2026-05-24", stopped_at="2026-05-24", notes=None,
			top_severity="High", total_duration_ms=1000, total_query_time_ms=0,
			total_queries=1, total_requests=1, summary_html=None,
			top_queries_json="[]", table_breakdown_json="[]", hot_frames_json="[]",
			session_time_breakdown_json="{}", total_python_ms=0, total_sql_ms=0,
			analyzer_warnings=None, v5_aggregate_json="{}",
			actions=[], findings=[finding], phase_2_runs=[],
		)
		html = renderer.render(doc, recordings=[])
		# Desk link present.
		assert "/app/server-script/Sales Invoice Submit Hook" in html, (
			"finding card must link to the Server Script's Desk form"
		)
		# NOT a vscode://file link to the synthetic filename.
		assert "vscode://file<serverscript>" not in html
		# Source snippet inlined from the DB. Pygments syntax-highlights the
		# script body and splits ``frappe.db.get_all`` across multiple spans,
		# so we check for pygments-tokenized fragments + the unmolested
		# comment text + the source-snippet container.
		assert "code-block" in html, "source-snippet <pre class='code-block'> missing"
		assert "A Server Script" in html, "first line of script body not rendered"
		assert "get_all" in html, "for-loop call (split by pygments) not rendered"
		assert "get_doc" in html, "inner get_doc call not rendered"
		# The old opaque "body" blob no longer dominates.
		# (Allow the literal string elsewhere in CSS comments, but not as the
		# finding's display label.)
		card_block = re.search(r'class="finding[^"]*".*?</article>', html, re.DOTALL)
		if card_block:
			assert "<server-script body>" not in card_block.group(0)
