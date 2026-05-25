# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for the safe-report self-containment canary.

The safe-report HTML is the **dev-shop interchange format**: a
self-contained file an operator can email / attach / archive without
needing a live Frappe bench to view it. Per
``[[feedback_safe_report_self_contained]]``: *"no CDN/remote fetches;
load-bearing offline guarantee with a canary acceptance test."*

The unit suite (``optimus/tests/test_report_a11y.py``
``test_report_is_self_contained_offline``) covers this against the
in-memory render of a stubbed finding via ``_render(findings=[...])``.
It cannot prove:

  * That the report fetched from the **on-disk File attachment**
    (after ``api.regenerate_reports`` writes it through Frappe's
    real ``file_manager``) is still self-contained. File encoding,
    Frappe's file-handling middleware, the post-write read path —
    all could theoretically introduce reference resolution that the
    unit-suite render doesn't see.
  * That the rendered HTML doesn't accidentally reference live-bench
    asset URLs (e.g. ``/assets/frappe/...``, ``/files/...``). Those
    would render in a browser opened from a live bench but break the
    moment the file is moved off-bench.
  * That the canary actually holds when the renderer is exercised
    through the full ``api.regenerate_reports`` boundary (not the
    unit suite's direct ``_render`` shortcut).

That gap is what this integration test fills. The 3 tests render a
minimal Optimus Session via the live ``api.regenerate_reports``
endpoint, read the on-disk attached HTML file, and grep for
remote-fetch patterns + Frappe asset URLs + inline `<script>` tags.

The regex patterns mirror those in
``optimus/tests/test_report_a11y.py:143-157`` so the on-disk file's
contract matches the in-memory render's. If the unit suite's canary
goes red on a renderer change, this integration test will go red the
same day. If only the integration test goes red, the regression is in
the persistence path (File write / file_manager / on-disk
encoding), not the renderer itself.
"""

from __future__ import annotations

import re

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from optimus import api

_SESSION_DOCTYPE = "Optimus Session"
_FILE_DOCTYPE = "File"


class TestSafeReportSelfContainedOnRealBench(FrappeTestCase):
	"""End-to-end: rendered report on disk has zero remote references."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		super().setUp()
		self._uuid = f"test-{frappe.generate_hash(length=12)}"
		self._session_doc = self._create_minimal_session(self._uuid)
		# Trigger a single render → raw_report_file populated.
		api.regenerate_reports(self._uuid)
		self._html = self._read_rendered_html(self._session_doc.name)
		assert self._html, "regenerate produced empty HTML"

	def tearDown(self):
		try:
			frappe.db.delete(
				_FILE_DOCTYPE,
				{
					"attached_to_doctype": _SESSION_DOCTYPE,
					"attached_to_name": self._session_doc.name,
				},
			)
			frappe.db.commit()
		except Exception:
			pass
		try:
			frappe.delete_doc(
				_SESSION_DOCTYPE,
				self._session_doc.name,
				force=1,
				ignore_permissions=True,
			)
			frappe.db.commit()
		except Exception:
			pass
		super().tearDown()

	# --- Helpers -------------------------------------------------------

	def _create_minimal_session(self, session_uuid: str):
		doc = frappe.get_doc(
			{
				"doctype": _SESSION_DOCTYPE,
				"session_uuid": session_uuid,
				"title": f"integration self-contained test {session_uuid}",
				"user": "Administrator",
				"status": "Ready",
				"started_at": now_datetime(),
				"stopped_at": now_datetime(),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _read_rendered_html(self, session_name: str) -> str:
		"""Read raw_report_file content via the File doc — exercises
		the same retrieval path a downstream consumer would use."""
		file_url = frappe.db.get_value(_SESSION_DOCTYPE, session_name, "raw_report_file")
		assert file_url, f"raw_report_file not set for {session_name!r}"
		file_doc = frappe.get_doc(_FILE_DOCTYPE, {"file_url": file_url})
		content = file_doc.get_content()
		if isinstance(content, bytes):
			content = content.decode("utf-8", errors="replace")
		return content

	# --- The 3 tests --------------------------------------------------

	def test_on_disk_report_has_no_remote_resource_urls(self):
		"""Mirrors the unit-suite canary (``test_report_a11y.py:143``)
		against the on-disk HTML. No external resource loads — no
		``https?:`` in ``src=``, no ``https?:`` in ``<link href=``,
		no ``@import``, no ``url(http``. ``data:`` URIs (the masthead
		logo) and anchor links (https://aerele.in) are explicitly
		allowed and don't count as remote fetches."""
		html = self._html
		# Strip whitespace / quote variants so url(http) is detected
		# regardless of the renderer's exact CSS formatting.
		flat = html.replace(" ", "").replace("'", "").replace('"', "")

		assert not re.search(r'src\s*=\s*["\']https?:', html), (
			"safe-report HTML contains a remote img/script src — breaks "
			"the offline / dev-shop-interchange guarantee"
		)
		assert not re.search(r'<link\b[^>]*href\s*=\s*["\']https?:', html), (
			"safe-report HTML loads a remote stylesheet — breaks the offline / dev-shop-interchange guarantee"
		)
		assert "@import" not in html, "safe-report HTML uses @import — could fetch over network"
		assert "url(http" not in flat, "safe-report HTML embeds url(http...) — breaks offline"
		# Sanity check: at least one HUMAN-facing anchor link exists
		# (so the negative checks above aren't trivially passing on an
		# empty / error page).
		assert re.search(r'<a [^>]*href="https?://', html), (
			"safe-report HTML has no human-facing anchor — the rendered "
			"page may be a stub or error, making the negative checks "
			"vacuously true"
		)

	def test_on_disk_report_has_no_inline_or_external_javascript(self):
		"""The renderer must not emit any ``<script>`` tag — neither
		inline JS nor an external ``<script src="...">``. This is a
		stronger constraint than the canary: the safe report is also
		a JS-free document, which is part of why it's safe to open in
		an arbitrary browser without consent."""
		# Case-insensitive substring check — the renderer might emit
		# uppercase or mixed-case tag names in some pre-existing
		# block (e.g., template comments). The contract is "no
		# script tag in any form."
		assert "<script" not in self._html.lower(), (
			"safe-report HTML contains a <script tag — should be JS-free "
			"per the self-contained-offline contract"
		)

	def test_on_disk_report_does_not_reference_live_bench_asset_urls(self):
		"""Bench asset URLs (``/assets/...``, ``/files/...``,
		``/api/method/...``) would render fine when opened from the
		live bench but break the moment the file is moved off-bench.
		The safe report must be **fully** self-contained — neither
		external NOR bench-local asset references."""
		html = self._html
		# These patterns scope to attribute-value positions so we don't
		# false-positive on the human-readable body text mentioning
		# "/assets" as part of a code snippet or finding description.
		assert not re.search(r'(?:src|href)\s*=\s*["\']/(?:assets|files)/', html), (
			"safe-report HTML references a bench-local asset path "
			"(/assets/... or /files/...) — breaks when the file is "
			"moved off-bench"
		)
		assert not re.search(r'(?:src|href)\s*=\s*["\']/api/method/', html), (
			"safe-report HTML references a bench API endpoint "
			"(/api/method/...) — breaks when opened off-bench"
		)
