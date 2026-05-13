# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.5.3 _display_name_for_node fallback.

Production trigger: a Server Script with a 5M-iteration CPU loop
produced a Slow Hot Path finding titled "In savedocs:Save, 86% of
the time was spent in " — trailing blank because pyinstrument
returns ``function=""`` for Python code executed via ``exec()``.

The analyzer now walks a preference chain:
  1. Real function name (not in _UNINFORMATIVE_FUNCTION_NAMES)
  2. Type-aware label (Server Script body, exec'd code)
  3. short_filename:lineno
  4. "<unnamed code>"
"""

from optimus.analyzers.call_tree import _display_name_for_node


def _node(**kwargs):
	"""Build a minimal call-tree node dict."""
	base = {
		"function": "",
		"filename": "",
		"lineno": None,
		"kind": "python",
		"cumulative_ms": 100,
		"children": [],
	}
	base.update(kwargs)
	return base


class TestRealFunctionName:
	def test_normal_function_returns_as_is(self):
		assert _display_name_for_node(
			_node(function="validate", filename="apps/myapp/foo.py", lineno=10),
		) == "validate"

	def test_dotted_name_preserved(self):
		assert _display_name_for_node(
			_node(function="MyController.validate"),
		) == "MyController.validate"


class TestServerScriptDetection:
	def test_serverscript_filename_pattern(self):
		"""ERPNext Server Scripts run via exec(code, {}, filename='<serverscript-N>').
		Filename starts with '<serverscript'."""
		assert _display_name_for_node(
			_node(function="", filename="<serverscript-42>"),
		) == "<server-script body>"

	def test_serverscript_dash_variant(self):
		"""Cover the '<server-script' spelling in case Frappe changes
		its placeholder format."""
		assert _display_name_for_node(
			_node(function="", filename="<server-script-99>"),
		) == "<server-script body>"

	def test_serverscript_with_module_function_name(self):
		"""exec() may report function='<module>' for the top-level of
		the exec'd code. Filename still drives the label."""
		assert _display_name_for_node(
			_node(function="<module>", filename="<serverscript-7>"),
		) == "<server-script body>"


class TestExecDetection:
	def test_bare_string_filename(self):
		"""A plain ``exec(code)`` without a filename hint uses '<string>'."""
		assert _display_name_for_node(
			_node(function="", filename="<string>"),
		) == "<exec'd code>"


class TestFilenameFallback:
	"""``short_filename`` returns the last 2 path segments so the
	reader sees which app the file lives in — ``myapp/foo.py`` is
	more useful than just ``foo.py`` for navigation in a bench with
	many apps."""

	def test_module_scope_with_real_filename(self):
		"""pyinstrument reports ``function='<module>'`` for code
		running at module top-level. Use the filename to give a
		useful location instead of passing through "<module>"."""
		assert _display_name_for_node(
			_node(function="<module>", filename="apps/myapp/foo.py", lineno=42),
		) == "myapp/foo.py:42"

	def test_module_scope_without_lineno(self):
		assert _display_name_for_node(
			_node(function="<module>", filename="apps/myapp/bar.py"),
		) == "myapp/bar.py"

	def test_lambda_with_filename(self):
		"""Lambdas get a meaningful filename-based label."""
		assert _display_name_for_node(
			_node(function="<lambda>", filename="apps/myapp/utils.py", lineno=5),
		) == "myapp/utils.py:5"

	def test_listcomp_with_filename(self):
		assert _display_name_for_node(
			_node(function="<listcomp>", filename="apps/myapp/x.py", lineno=100),
		) == "myapp/x.py:100"

	def test_genexpr_with_filename(self):
		assert _display_name_for_node(
			_node(function="<genexpr>", filename="apps/myapp/x.py", lineno=50),
		) == "myapp/x.py:50"


class TestLastResort:
	def test_no_function_no_filename_returns_last_resort(self):
		assert _display_name_for_node(
			_node(function="", filename=""),
		) == "<unnamed code>"

	def test_synthetic_fn_without_usable_filename(self):
		"""If the filename is itself synthetic ('<unknown>') AND
		the function name is also synthetic, pass the synthetic
		function name through rather than going blank."""
		assert _display_name_for_node(
			_node(function="<module>", filename="<unknown>"),
		) == "<module>"

	def test_empty_function_with_question_filename(self):
		"""Some pyinstrument paths emit '?' for filename on C frames."""
		assert _display_name_for_node(
			_node(function="<lambda>", filename="?"),
		) == "<lambda>"


class TestProductionBug:
	"""The exact production scenario that motivated this fix —
	Server Script containing for-loop, surfaced as empty title."""

	def test_empty_function_with_serverscript_filename(self):
		node = _node(
			function="",
			filename="<serverscript-123>",
			lineno=None,
			cumulative_ms=1850,
		)
		name = _display_name_for_node(node)
		# Must NOT be empty or "<module>".
		assert name
		assert name != "<module>"
		# Must be something that READS AS A LOCATION.
		assert "server-script" in name.lower()


class TestIntegrationThroughFindingTitle:
	"""End-to-end: the Slow Hot Path title rendered from a
	server-script-body node produces a human-readable string."""

	def test_slow_hot_path_with_server_script_body(self):
		from optimus.analyzers import call_tree

		# Simulate: savedocs running an ERPNext Server Script with
		# a big CPU loop. The exec'd body has function="" and
		# filename="<serverscript-1>".
		tree = {
			"function": "<root>",
			"filename": "",
			"lineno": 0,
			"self_ms": 0,
			"cumulative_ms": 2000,
			"kind": "python",
			"children": [{
				"function": "",  # ← the bug-trigger
				"filename": "<serverscript-1>",
				"lineno": 1,
				"self_ms": 1900,
				"cumulative_ms": 1900,
				"kind": "python",
				"children": [],
			}],
		}
		findings = call_tree._emit_per_action_findings(
			tree,
			action_idx=0,
			action_label="frappe.desk.form.save.savedocs:Save",
			action_wall_time_ms=2000,
		)
		slow = [f for f in findings if f["finding_type"] == "Slow Hot Path"]
		assert len(slow) == 1
		title = slow[0]["title"]
		# Must not have the pre-fix trailing-blank shape.
		assert not title.endswith(" "), (
			f"Title must not end with a trailing blank (empty fn_name); "
			f"got: {title!r}"
		)
		# Must mention server-script so the reader understands where
		# the time went.
		assert "server-script" in title.lower(), (
			f"Title must surface the Server Script context; got: {title!r}"
		)
		# Customer description must also have a non-empty name.
		desc = slow[0]["customer_description"]
		assert "****" not in desc, (
			f"Description must not render empty ** ** around fn_name; "
			f"got: {desc!r}"
		)
