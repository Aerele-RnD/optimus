# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for ``api.regenerate_reports`` byte-stability.

``api.regenerate_reports(session_uuid)`` (``optimus/api.py:1121-1241``)
re-renders the safe-report HTML from an *already-analyzed* session
without re-running the analyze pipeline. Its purpose is to let an
operator pick up a renderer / template upgrade on a historical session
without paying the cost of re-analysis.

The re-render path is load-bearing for upgrades — every renderer or
template polish round (v0.7.0 polish day, v0.10.0 renderer split, the
v0.6.0 "single rendering path" round) shipped on the assumption that
operators could regenerate old sessions to see the new UI. The
pure-pytest unit test (``optimus/tests/test_regenerate_reports_api.py``)
does source inspection only — confirms the endpoint is whitelisted,
takes ``session_uuid``, doesn't call ``_enqueue_analyze``, calls
``clear_cached_pdf``, gates on permissions. It says nothing about the
output.

What that unit suite can't prove:

  * That two consecutive ``regenerate_reports`` calls on the same
    session produce **byte-identical** HTML. If non-determinism slips
    into the renderer (a fresh UUID, a dict-iteration order change, a
    ``time.time()`` snapshot in a stamp), the upgrade path silently
    starts producing diff'd HTML — which breaks ``regenerate`` as a
    way to roll forward, and breaks any safe-report diffing workflow
    a dev-shop might rely on.
  * That the endpoint actually attaches the rendered HTML to
    ``Optimus Session.raw_report_file`` (Attach field) and the
    attachment URL resolves to readable content.
  * That a session-data change (e.g., the operator edits the title)
    produces a **different** HTML — the canary's complement.
  * That regenerate honours its documented "Allowed on Ready OR
    Failed sessions" claim.

That gap is what this integration test fills. Each test uses a unique
``session_uuid`` for isolation; ``setUpClass`` patches
``renderer._internal._now_iso`` to a fixed string so the embedded
"Generated at" timestamp is deterministic (otherwise the two HTML
outputs would differ by their stamp alone).
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from optimus import api

_SESSION_DOCTYPE = "Optimus Session"
_FILE_DOCTYPE = "File"

# Fixed timestamp so two consecutive renders produce byte-identical
# HTML. Without this patch, ``_now_iso()`` returns ``datetime.now()``
# and the two outputs differ by their embedded stamp string. The patch
# is applied at the test-class level via ``mock.patch`` so all 4 tests
# see the same fixed timestamp; production behaviour is unaffected
# (the patch is scoped to this TestCase only).
_FIXED_NOW_ISO = "2026-05-25 12:00:00"


class TestRegenerateReportsIdempotent(FrappeTestCase):
	"""End-to-end: two regenerate_reports calls → byte-identical HTML."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Patch the renderer's wall-clock-stamping helper for the
		# lifetime of the test class. Module-attribute lookup at
		# render-time (``_now_iso()`` call site in renderer/_internal.py)
		# means the patch takes effect immediately for every render.
		cls._now_iso_patcher = mock.patch(
			"optimus.renderer._internal._now_iso",
			return_value=_FIXED_NOW_ISO,
		)
		cls._now_iso_patcher.start()

	@classmethod
	def tearDownClass(cls):
		try:
			cls._now_iso_patcher.stop()
		except Exception:
			pass
		super().tearDownClass()

	# --- setUp / tearDown ---------------------------------------------

	def setUp(self):
		super().setUp()
		self._uuid = f"test-{frappe.generate_hash(length=12)}"
		self._session_doc = self._create_minimal_session(self._uuid)

	def tearDown(self):
		try:
			self._delete_session_and_attachments(self._session_doc.name)
		except Exception:
			pass
		super().tearDown()

	# --- Synthetic-session helper -------------------------------------

	def _create_minimal_session(self, session_uuid: str):
		"""Insert a minimal valid ``Optimus Session`` with status=Ready.

		Reqd fields: session_uuid, title, user, status, started_at.
		Optional analysis-data fields are left at defaults — the
		renderer is defensive against missing data, empty sections
		render as empty, the HTML is still valid."""
		doc = frappe.get_doc(
			{
				"doctype": _SESSION_DOCTYPE,
				"session_uuid": session_uuid,
				"title": f"integration regenerate test {session_uuid}",
				"user": "Administrator",
				"status": "Ready",
				"started_at": now_datetime(),
				"stopped_at": now_datetime(),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _delete_session_and_attachments(self, docname: str) -> None:
		"""Delete the session + every File row attached to it.

		``api.regenerate_reports`` creates a fresh File on each call
		(via ``_save_report_file``); ``force=1`` on
		``frappe.delete_doc("Optimus Session", ...)`` cascades to
		attached File rows in Frappe v16, but we wipe explicitly to
		catch any orphans."""
		try:
			frappe.db.delete(
				_FILE_DOCTYPE,
				{
					"attached_to_doctype": _SESSION_DOCTYPE,
					"attached_to_name": docname,
				},
			)
			frappe.db.commit()
		except Exception:
			pass
		try:
			frappe.delete_doc(
				_SESSION_DOCTYPE, docname, force=1, ignore_permissions=True
			)
			frappe.db.commit()
		except Exception:
			pass

	# --- HTML-extraction helper ---------------------------------------

	def _read_rendered_html(self, session_name: str) -> bytes:
		"""Fetch the latest ``raw_report_file`` content as bytes.

		``api.regenerate_reports`` rewrites ``raw_report_file`` on
		every call, so the URL reflects the most-recent render. The
		test calls ``_read_rendered_html`` AFTER each regenerate to
		snapshot the content for byte-diff."""
		file_url = frappe.db.get_value(
			_SESSION_DOCTYPE, session_name, "raw_report_file"
		)
		assert file_url, f"raw_report_file not set for session {session_name!r}"
		file_doc = frappe.get_doc(_FILE_DOCTYPE, {"file_url": file_url})
		content = file_doc.get_content()
		if isinstance(content, str):
			content = content.encode("utf-8")
		return content

	# --- The 4 tests --------------------------------------------------

	def test_two_consecutive_regenerates_produce_byte_identical_html(self):
		"""The canary. Call ``api.regenerate_reports`` twice on the
		same session (no intervening changes) → both rendered HTMLs
		must be byte-identical. Catches future non-determinism in the
		renderer (dict-order changes, fresh UUIDs in markup, extra
		``time.time()`` snapshots, etc.)."""
		# First render.
		result_1 = api.regenerate_reports(self._uuid)
		assert result_1.get("regenerated") is True
		html_1 = self._read_rendered_html(self._session_doc.name)
		assert html_1, "first render produced empty HTML"

		# Second render — same session, same data, same patched _now_iso.
		result_2 = api.regenerate_reports(self._uuid)
		assert result_2.get("regenerated") is True
		html_2 = self._read_rendered_html(self._session_doc.name)
		assert html_2, "second render produced empty HTML"

		# Byte-equality is the contract under test.
		assert html_1 == html_2, (
			f"regenerate_reports is NOT byte-stable across consecutive calls — "
			f"the upgrade path silently produces diff'd HTML. "
			f"len(html_1)={len(html_1)}, len(html_2)={len(html_2)}, "
			f"first 200-byte diff position: "
			f"{next((i for i, (a, b) in enumerate(zip(html_1, html_2, strict=False)) if a != b), None)!r}"
		)

	def test_regenerate_attaches_html_to_raw_report_file_and_url_resolves(self):
		"""Side-effect contract: after the call, ``raw_report_file``
		points at a private File row whose content begins with
		``<!DOCTYPE`` or ``<html`` (so it's an actual HTML document,
		not an error string or empty file)."""
		result = api.regenerate_reports(self._uuid)
		assert result.get("regenerated") is True

		file_url = frappe.db.get_value(
			_SESSION_DOCTYPE, self._session_doc.name, "raw_report_file"
		)
		assert file_url, "raw_report_file not set after regenerate"
		assert file_url.startswith("/private/files/"), (
			f"expected private File URL; got {file_url!r}"
		)

		file_doc = frappe.get_doc(_FILE_DOCTYPE, {"file_url": file_url})
		assert file_doc.is_private == 1, "report should be a private file"
		assert file_doc.attached_to_doctype == _SESSION_DOCTYPE
		assert file_doc.attached_to_name == self._session_doc.name

		content = file_doc.get_content()
		if isinstance(content, bytes):
			content_str = content.decode("utf-8", errors="replace")
		else:
			content_str = content
		# The renderer emits a complete HTML document.
		head = content_str.lstrip().lower()[:32]
		assert head.startswith("<!doctype") or head.startswith("<html"), (
			f"rendered content doesn't look like HTML; first 32 chars: {head!r}"
		)

	def test_regenerate_byte_diff_when_session_field_changes(self):
		"""The canary's complement. Render once, snapshot HTML.
		Mutate the session's ``title`` field via ``frappe.db.set_value``
		(the renderer reads it through ``build_report_context``).
		Render again, snapshot HTML. The two HTMLs must DIFFER — and
		the new title must appear in HTML 2 but not HTML 1. Catches
		silent caching that would return stale HTML on field
		changes."""
		original_title = self._session_doc.title
		new_title = f"MUTATED-{frappe.generate_hash(length=8)}"

		# Render 1 — original title.
		api.regenerate_reports(self._uuid)
		html_1 = self._read_rendered_html(self._session_doc.name)
		assert original_title.encode("utf-8") in html_1, (
			"original title should appear in HTML 1"
		)
		assert new_title.encode("utf-8") not in html_1, (
			"new title should NOT appear in HTML 1 (sanity check)"
		)

		# Mutate the title — direct DB write so we don't trigger the
		# session's on_update hooks (which could side-effect the test).
		frappe.db.set_value(
			_SESSION_DOCTYPE, self._session_doc.name, "title", new_title
		)
		frappe.db.commit()

		# Render 2 — new title.
		api.regenerate_reports(self._uuid)
		html_2 = self._read_rendered_html(self._session_doc.name)
		assert new_title.encode("utf-8") in html_2, (
			f"new title {new_title!r} should appear in HTML 2 — regenerate "
			f"did not pick up the field change (silent caching?)"
		)
		assert html_1 != html_2, (
			"HTMLs should differ after the title change — regenerate is "
			"silently caching the previous output"
		)

	def test_regenerate_works_on_failed_status_session(self):
		"""Validates the docstring claim: regenerate is allowed on
		Ready OR Failed sessions. A Failed session whose analyze
		partially completed should still re-render — that's often the
		whole reason for the feature ("unblock a demo when a
		render-time bug was fixed")."""
		# Demote the session from Ready to Failed.
		frappe.db.set_value(
			_SESSION_DOCTYPE, self._session_doc.name, "status", "Failed"
		)
		frappe.db.commit()

		# Should not raise.
		result = api.regenerate_reports(self._uuid)
		assert result.get("regenerated") is True
		assert result.get("session_uuid") == self._uuid

		# Side-effect should still happen.
		file_url = frappe.db.get_value(
			_SESSION_DOCTYPE, self._session_doc.name, "raw_report_file"
		)
		assert file_url, (
			"raw_report_file should be set even when regenerating a Failed session"
		)
