# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for v0.3.0 fields in api.export_session output shape."""

import inspect

from optimus import api


def test_export_session_source_includes_new_fields():
	src = inspect.getsource(api.export_session)
	# These field names should appear in the export logic
	assert "total_python_ms" in src
	assert "total_sql_ms" in src
	assert "hot_frames" in src
	assert "session_time_breakdown" in src
	assert "call_tree" in src


def test_install_before_uninstall_calls_uninstall_wraps():
	from optimus import install

	src = inspect.getsource(install.before_uninstall)
	assert "uninstall_wraps" in src
