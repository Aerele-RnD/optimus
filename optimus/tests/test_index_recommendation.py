# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.6.0 concrete index recommendation:

  * `table_breakdown` co-occurrence → `recommended_index` (a composite, ordered
    by usage frequency, capped, with the doctype derived) + `is_write_hot`;
  * the report's "Index candidate" panel rendering (recommendation + the
    `frappe.db.add_index` patch + caveats + the AI block);
  * `ai_fix._build_index_messages` / `ai_fix.suggest_index` (mocked HTTP);
  * `analyze` helpers (`_table_existing_indexes`, `_table_index_sample_queries`,
    `_ai_payload_for_table`, the auto-enrich gating);
  * the `api.suggest_index` endpoint contract (source-inspection);
  * `analyzers.base.is_write_hot_table`.
"""

import json
import os
import re
import types
from unittest.mock import patch

import pytest

# Imported at module top (collection time) so a later test that swaps
# sys.modules['frappe'] / drops optimus.* can't trigger a *re-import*
# of analyze.py — whose `from frappe.recorder import RECORDER_REQUEST_HASH`
# blows up under that pollution. The module object stays usable via this name.
from optimus import analyze as _analyze  # noqa: E402

# --------------------------------------------------------------------------
# table_breakdown — co-occurrence → recommended_index
# --------------------------------------------------------------------------

def _rec(calls):
	return {"calls": calls}


def _q(query, duration=1.0):
	return {"query": query, "duration": duration}


class TestRecommendedIndex:
	def _analyze(self, recordings):
		from optimus.analyzers import table_breakdown as tb

		res = tb.analyze(recordings, types.SimpleNamespace())
		return {t["table"]: t for t in res.aggregate.get("table_breakdown", [])}

	def test_picks_most_common_cofilter_combo(self):
		bd = self._analyze([_rec([
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ?"),
		])])
		rec = bd["tabSales Invoice"]["recommended_index"]
		assert set(rec["columns"]) == {"customer", "posting_date", "status"}
		assert rec["doctype"] == "Sales Invoice"
		assert rec["together_count"] == 3
		assert rec["read_count"] == 4

	def test_columns_ordered_by_usage_frequency(self):
		# customer in the most reads, then posting_date, then status — all
		# three filtered together in 2 reads (the dominant combo). The
		# composite is ordered by per-column hit frequency.
		bd = self._analyze([_rec([
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ?"),
		])])
		rec = bd["tabSales Invoice"]["recommended_index"]
		assert rec["columns"] == ["customer", "posting_date", "status"]

	def test_caps_composite_width(self):
		from optimus.analyzers.table_breakdown import MAX_RECOMMENDED_INDEX_COLS

		cols = ["c1", "c2", "c3", "c4", "c5", "c6"]
		where = " AND ".join(f"{c} = ?" for c in cols)
		bd = self._analyze([_rec([_q(f"SELECT name FROM `tabFoo` WHERE {where}")])])
		rec = bd["tabFoo"]["recommended_index"]
		assert len(rec["columns"]) == MAX_RECOMMENDED_INDEX_COLS

	def test_no_recommendation_for_meta_table(self):
		bd = self._analyze([_rec([_q("SELECT name FROM `tabDocType` WHERE module = ?")])])
		assert bd["tabDocType"]["recommended_index"] is None
		assert bd["tabDocType"]["is_meta_table"] is True

	def test_no_recommendation_for_non_doctype_table(self):
		bd = self._analyze([_rec([
			_q("SELECT column_name FROM information_schema.columns WHERE table_name = ? AND column_name = ?")
		])])
		assert bd["information_schema.columns"]["recommended_index"] is None

	def test_no_recommendation_when_only_metadata_cols_filtered(self):
		bd = self._analyze([_rec([_q("SELECT name FROM `tabFoo` WHERE parent = ? AND parenttype = ?")])])
		# parent / parenttype are Frappe metadata columns → excluded → no combo.
		assert bd["tabFoo"]["recommended_index"] is None
		assert "parent" in bd["tabFoo"]["framework_cols_filtered"]

	def test_is_write_hot_flag(self):
		bd = self._analyze([
			_rec([_q("SELECT name FROM `tabGL Entry` WHERE account = ? AND party = ?")]),
			_rec([_q("SELECT name FROM `tabSales Invoice` WHERE customer = ?")]),
		])
		assert bd["tabGL Entry"]["is_write_hot"] is True
		assert bd["tabSales Invoice"]["is_write_hot"] is False

	def test_also_filtered_lists_leftover_columns(self):
		bd = self._analyze([_rec([
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE territory = ? AND company = ?"),
		])])
		rec = bd["tabSales Invoice"]["recommended_index"]
		assert set(rec["columns"]) == {"customer", "status"}
		assert "territory" in rec["also_filtered"] and "company" in rec["also_filtered"]


# --------------------------------------------------------------------------
# analyzers.base — write-hot table truth table
# --------------------------------------------------------------------------

def test_is_write_hot_table_truth_table():
	from optimus.analyzers.base import is_write_hot_table

	for t in ("tabGL Entry", "tabStock Ledger Entry", "tabBin", "tabVersion", "`tabGL Entry`"):
		assert is_write_hot_table(t) is True
	for t in ("tabSales Invoice", "tabItem", "tabUser", "", None, "information_schema.columns"):
		assert is_write_hot_table(t) is False


# --------------------------------------------------------------------------
# report.html — the "Index candidate" panel
# --------------------------------------------------------------------------

def _doc(table_breakdown):
	return types.SimpleNamespace(
		name="PS-idx", session_uuid="idx-uuid", title="t", user="a@example.com",
		status="Ready", started_at="2026-05-12T00:00:00", stopped_at="2026-05-12T00:00:05",
		notes=None, top_severity="Low", summary_html=None, total_duration_ms=100,
		total_query_time_ms=80, total_queries=5, total_requests=1, top_queries_json="[]",
		table_breakdown_json=json.dumps(table_breakdown), hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
	)


def _table_entry(**kw):
	base = {
		"table": "tabSales Invoice", "duration_ms": 50.0, "queries": 5,
		"read_count": 4, "write_count": 1, "read_time_ms": 50.0, "write_time_ms": 1.0,
		"index_candidates": [{"column": "customer", "sources": ["WHERE"], "hits": 4}],
		"recommended_index": {
			"columns": ["customer", "posting_date"], "doctype": "Sales Invoice",
			"together_count": 3, "read_count": 4, "also_filtered": ["status"],
		},
		"framework_cols_filtered": [], "is_meta_table": False, "is_write_hot": False,
	}
	base.update(kw)
	return base


class TestRenderedIndexCandidatePanel:
	def test_renders_recommendation_and_patch(self):
		from optimus import renderer

		html = renderer.render_raw(_doc([_table_entry()]), recordings=[])
		assert "Index candidate" in html
		assert 'frappe.db.add_index("Sales Invoice", ["customer", "posting_date"])' in html
		assert "SHOW INDEX FROM" in html
		assert "only makes single-column indexes" in html
		assert "Other columns this session filtered on" in html and "status" in html

	def test_write_hot_warning(self):
		from optimus import renderer

		html = renderer.render_raw(_doc([_table_entry(
			table="tabGL Entry", write_count=1, is_write_hot=True,
			recommended_index={"columns": ["against_voucher_type", "against_voucher_no"], "doctype": "GL Entry", "together_count": 3, "read_count": 4, "also_filtered": []},
		)]), recordings=[])
		assert "write-hot core table" in html

	def test_renders_ai_index_block_when_present(self):
		from optimus import renderer

		entry = _table_entry()
		entry["ai_index"] = {
			"suggestion": "**Recommendation**\n\nNothing — `idx_customer_date` already covers it.",
			"model": "claude-sonnet-4-6", "provider": "Anthropic",
			"generated_at": "2026-05-12T00:00:00+00:00",
		}
		html = renderer.render_raw(_doc([entry]), recordings=[])
		# v0.7.x Phase I: AI index advice renders inside a `.fix-box`
		# (same component as the AI-fix on findings); "Index advice"
		# heading + the `AI · model` tag are now in separate spans
		# instead of the inline-paren form.
		assert "Index advice" in html and "claude-sonnet-4-6" in html
		assert "already covers it" in html

	def test_no_ai_index_block_when_indexes_section_toggle_off(self):
		# v0.6.x per-section hard off: even with ai_index populated on the
		# table entry, the renderer strips it when ai_suggest_indexes is off.
		from unittest.mock import patch

		from optimus import renderer, settings

		entry = _table_entry()
		entry["ai_index"] = {
			"suggestion": "**Recommendation**\n\nAdd an index.",
			"model": "claude-sonnet-4-6", "provider": "Anthropic",
			"generated_at": "2026-05-12T00:00:00+00:00",
		}
		with patch("optimus.settings.get_config",
		           return_value=settings.OptimusConfig(ai_suggest_indexes=False)):
			html = renderer.render_raw(_doc([entry]), recordings=[])
		assert "Index advice (AI" not in html
		assert "claude-sonnet-4-6" not in html

	def test_falls_back_to_flat_list_without_recommendation(self):
		from optimus import renderer

		entry = _table_entry(recommended_index=None)
		html = renderer.render_raw(_doc([entry]), recordings=[])
		assert "Index candidates - to speed up reads" in html
		assert ">customer</code>" in html


# --------------------------------------------------------------------------
# ai_fix — _build_index_messages / suggest_index
# --------------------------------------------------------------------------

class _FakeResp:
	def __init__(self, status_code=200, payload=None, text=""):
		self.status_code = status_code
		self._payload = payload or {}
		self.text = text

	def json(self):
		return self._payload


def _post_returning(resp):
	def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
		_fake_post.last = types.SimpleNamespace(url=url, headers=headers, body=json)
		return resp
	_fake_post.last = None
	return _fake_post


_PROVIDER = {"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt-4.1-mini", "needs_key": True, "api_key": "sk-test"}


class TestAiSuggestIndex:
	def test_build_index_messages_shape(self):
		from optimus import ai_fix

		system, messages = ai_fix._build_index_messages({
			"table": "tabGL Entry", "doctype": "GL Entry", "read_count": 6, "write_count": 1,
			"is_write_hot": True,
			"recommended_index": {"columns": ["against_voucher_type", "against_voucher_no"], "together_count": 3},
			"candidates": [{"column": "account", "sources": ["WHERE"], "hits": 2}],
			"framework_cols_filtered": ["delinked"],
			"existing_indexes": [{"name": "index_against_voucher", "columns": ["against_voucher_type", "against_voucher"], "unique": False}],
			"sample_queries": ["SELECT name FROM `tabGL Entry` WHERE against_voucher_type = ? AND against_voucher_no = ?"],
		})
		low = system.lower()
		assert "**recommendation**" in low and "**skip**" in low and "frappe.db.add_index" in system
		c = messages[0]["content"]
		assert "tabGL Entry" in c and "GL Entry" in c
		assert "CURRENT indexes" in c and "index_against_voucher" in c
		assert "write-hot core table" in c
		assert "against_voucher_type = ?" in c

	def test_build_index_messages_notes_missing_indexes(self):
		from optimus import ai_fix

		_, messages = ai_fix._build_index_messages({"table": "tabFoo", "existing_indexes": []})
		assert "not available" in messages[0]["content"]

	def test_suggest_index_happy_path(self, monkeypatch):
		import requests

		from optimus import ai_fix

		out_payload = {"choices": [{"message": {"content": "**Recommendation**\n\nAdd `(customer, posting_date)`."}}]}
		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, out_payload)))
		with patch("optimus.ai_fix._resolve_provider", return_value=dict(_PROVIDER)):
			out = ai_fix.suggest_index({"table": "tabSales Invoice", "doctype": "Sales Invoice"})
		assert "customer, posting_date" in out["suggestion"]
		assert out["model"] == "gpt-4.1-mini" and out["provider"] == "OpenAI" and out["generated_at"]

	def test_suggest_index_empty_table_raises(self):
		from optimus import ai_fix

		with pytest.raises(ai_fix.AiFixError):
			ai_fix.suggest_index({})

	def test_suggest_index_empty_response_raises(self, monkeypatch):
		import requests

		from optimus import ai_fix

		monkeypatch.setattr(requests, "post", _post_returning(_FakeResp(200, {"choices": [{"message": {"content": "  "}}]})))
		with patch("optimus.ai_fix._resolve_provider", return_value=dict(_PROVIDER)):
			with pytest.raises(ai_fix.AiFixError, match="empty"):
				ai_fix.suggest_index({"table": "tabFoo"})


# --------------------------------------------------------------------------
# analyze — helpers + auto-enrich gating
# --------------------------------------------------------------------------

class TestAnalyzeIndexHelpers:
	def test_table_existing_indexes_handles_unreadable_table(self):
		# In the test env frappe.db is unavailable / the table doesn't exist —
		# the helper must swallow it and return [].
		assert _analyze._table_existing_indexes("tabDefinitelyNotARealTable") == []

	def test_table_existing_indexes_groups_show_index_rows(self, monkeypatch):
		rows = [
			{"Key_name": "PRIMARY", "Seq_in_index": 1, "Column_name": "name", "Non_unique": 0},
			{"Key_name": "idx_cust_date", "Seq_in_index": 2, "Column_name": "posting_date", "Non_unique": 1},
			{"Key_name": "idx_cust_date", "Seq_in_index": 1, "Column_name": "customer", "Non_unique": 1},
		]
		# Patch the `frappe` reference inside the analyze module directly —
		# robust against the suite's sys.modules['frappe'] churn.
		monkeypatch.setattr(
			_analyze, "frappe",
			types.SimpleNamespace(db=types.SimpleNamespace(sql=lambda *a, **k: rows)),
			raising=False,
		)
		out = _analyze._table_existing_indexes("tabSales Invoice")
		by_name = {i["name"]: i for i in out}
		assert by_name["PRIMARY"]["columns"] == ["name"] and by_name["PRIMARY"]["unique"] is True
		# Columns ordered by Seq_in_index.
		assert by_name["idx_cust_date"]["columns"] == ["customer", "posting_date"]
		assert by_name["idx_cust_date"]["unique"] is False

	def test_table_index_sample_queries_picks_selects_on_table(self):
		recs = [{"calls": [
			{"query": "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"},
			{"query": "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"},  # dupe — deduped
			{"query": "UPDATE `tabSales Invoice` SET status = ? WHERE name = ?"},                 # not a SELECT
			{"query": "SELECT name FROM `tabItem` WHERE item_code = ?"},                          # other table
		]}]
		out = _analyze._table_index_sample_queries(recs, "tabSales Invoice", limit=5)
		assert len(out) == 1 and "tabSales Invoice" in out[0] and "SELECT" in out[0]

	def test_enrich_table_breakdown_noop_when_ai_disabled(self):
		from optimus.analyzers.base import AnalyzeContext

		ctx = AnalyzeContext(session_uuid="u", docname="d")
		ctx.aggregate["table_breakdown"] = [_table_entry()]
		with patch("optimus.settings.get_config",
		           return_value=types.SimpleNamespace(ai_enabled=False, ai_auto_suggest=True)):
			_analyze._enrich_table_breakdown_with_ai_suggestions(ctx, recordings=[])
		assert "ai_index" not in ctx.aggregate["table_breakdown"][0]

	def test_enrich_table_breakdown_adds_ai_index_when_enabled(self, monkeypatch):
		from optimus import ai_fix
		from optimus.analyzers.base import AnalyzeContext

		ctx = AnalyzeContext(session_uuid="u", docname="d")
		ctx.aggregate["table_breakdown"] = [_table_entry(), _table_entry(table="tabGL Entry", is_write_hot=True)]
		monkeypatch.setattr("optimus.settings.get_config",
		                    lambda: types.SimpleNamespace(ai_enabled=True, ai_auto_suggest=True))
		monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
		monkeypatch.setattr(ai_fix, "suggest_index",
		                    lambda payload: {"suggestion": f"advice for {payload['table']}", "model": "m", "provider": "p", "generated_at": "t"})
		monkeypatch.setattr(_analyze, "_publish_progress", lambda *a, **k: None)
		_analyze._enrich_table_breakdown_with_ai_suggestions(ctx, recordings=[])
		for t in ctx.aggregate["table_breakdown"]:
			assert t["ai_index"]["suggestion"] == f"advice for {t['table']}"


# --------------------------------------------------------------------------
# api.suggest_index — source-inspection contract
# --------------------------------------------------------------------------

def _api_src():
	with open(os.path.join(os.path.dirname(__file__), "..", "api.py")) as f:
		return f.read()


def _fn_body(src, name):
	start = src.index(f"def {name}(")
	after = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist|class )", src[after:])
	end = after + (nxt.start() if nxt else len(src) - after)
	return src[start:end]


class TestSuggestIndexApi:
	def test_whitelisted_and_signature(self):
		src = _api_src()
		assert re.search(r"@frappe\.whitelist\(\)\s*\ndef suggest_index", src)
		assert "def suggest_index(session_uuid: str, table_name: str)" in src

	def test_permission_gate_and_ready_and_ai_guard(self):
		body = _fn_body(_api_src(), "suggest_index")
		assert "_require_profiler_user()" in body
		assert 'row["user"] != user' in body and "frappe.PermissionError" in body
		assert 'row["status"] != "Ready"' in body
		assert "ai_fix.is_available()" in body and "AI Fix Suggestions" in body

	def test_calls_backfill_then_rerenders(self):
		body = _fn_body(_api_src(), "suggest_index")
		assert "_run_table_index_ai_backfill(doc, table_name=table_name)" in body
		assert "ai_fix.AiFixError" in body and "frappe.throw(str(e))" in body
		assert "regenerate_reports(session_uuid)" in body
