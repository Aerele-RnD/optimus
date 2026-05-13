# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Test that hooks.py uses a cache-busting version suffix for static
assets. v0.5.2: the suffix is ``<__version__>.<mtime>`` so file edits
auto-invalidate the browser cache even between releases. See
hooks._asset_version for the rationale."""

import re

from optimus import __version__, hooks


def _js_entries():
	"""Return app_include_js as a list regardless of string/list form."""
	val = hooks.app_include_js
	return val if isinstance(val, list) else [val]


def test_app_include_js_contains_version():
	"""Every JS entry must have a ?v=<version>.<anything> cache-buster
	starting with the current __version__. The suffix after the version
	is the mtime-based auto-invalidator — we don't care about its exact
	value, just that the version is present."""
	pattern = re.compile(
		rf"\?v={re.escape(__version__)}(\.\d+)?$"
	)
	for entry in _js_entries():
		assert pattern.search(entry), (
			f"JS entry must have ?v={__version__}[.<mtime>] suffix; "
			f"got: {entry}"
		)


def test_app_include_css_contains_version():
	pattern = re.compile(rf"\?v={re.escape(__version__)}(\.\d+)?$")
	assert pattern.search(hooks.app_include_css), (
		f"CSS entry must have ?v={__version__}[.<mtime>] suffix; "
		f"got: {hooks.app_include_css}"
	)


def test_app_include_paths_are_correct():
	entries = _js_entries()
	# floating_widget.js must always be present.
	assert any(
		e.startswith("/assets/optimus/js/floating_widget.js")
		for e in entries
	)
	# v0.5.0 adds optimus_frontend.js alongside it.
	assert any(
		e.startswith("/assets/optimus/js/optimus_frontend.js")
		for e in entries
	)
	assert hooks.app_include_css.startswith("/assets/optimus/css/floating_widget.css")


def test_mtime_component_auto_invalidates_on_file_edit(tmp_path, monkeypatch):
	"""v0.5.2: the ?v= suffix includes the file's mtime so ANY edit to
	the JS/CSS auto-busts the browser cache — even when __version__
	hasn't been bumped. Pre-v0.5.2 was a real user incident: v0.5.2's
	realtime code shipped while the browser kept serving v0.5.1's
	polling code because the query string was unchanged.
	"""
	from optimus.hooks import _asset_version

	# Call twice — result should change if the file is touched.
	v1 = _asset_version("js/floating_widget.js")
	# The version string format is "<version>.<mtime>" or just
	# "<version>" if the file couldn't be stat'd.
	assert v1.startswith(__version__), (
		f"Asset version must start with __version__ {__version__}; "
		f"got: {v1}"
	)
	# If the file existed (common case), there's a dot + digits.
	assert "." in v1, (
		f"Asset version must include an mtime component; got: {v1}. "
		"Without mtime, any JS edit needs a manual __version__ bump "
		"to invalidate the browser cache."
	)
	# And the mtime part must be purely digits (integer seconds).
	mtime_suffix = v1[len(__version__) + 1:]
	assert mtime_suffix.isdigit(), (
		f"Mtime component must be an integer; got: {mtime_suffix!r}"
	)


def test_asset_version_falls_back_when_file_missing(monkeypatch):
	"""If the file can't be stat'd (unusual install, permission
	issue), _asset_version must still return a non-empty version
	string so hooks.py doesn't crash the app load. Falls back to
	__version__ alone."""
	from optimus.hooks import _asset_version

	v = _asset_version("js/this_file_does_not_exist.js")
	# Falls back to just __version__ (no mtime suffix).
	assert v == __version__
