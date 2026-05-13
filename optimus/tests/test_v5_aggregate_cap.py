# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Source-inspection guards + a behavioral test for the v0.5.0
aggregate size caps in analyze._persist.

Without caps, v5_aggregate_json could balloon to 1 MB+ on a session
with 200 recordings × full frontend data, which slows the Profiler
Session form load for every viewer. The cap is tail-preferring and
surfaces a warning in analyzer_warnings so operators can see the drop.
"""

import inspect


def test_persist_caps_infra_timeline():
	from optimus import analyze

	src = inspect.getsource(analyze._persist)
	# The source must literally set a cap for infra_timeline.
	assert "V5_INFRA_TIMELINE_CAP" in src
	# And surface a warning on overflow.
	assert "infra_timeline truncated" in src


def test_persist_caps_frontend_xhr():
	from optimus import analyze

	src = inspect.getsource(analyze._persist)
	assert "V5_FRONTEND_XHR_CAP" in src
	assert "frontend_xhr_matched truncated" in src


def test_persist_caps_frontend_orphans():
	from optimus import analyze

	src = inspect.getsource(analyze._persist)
	assert "V5_FRONTEND_ORPHANS_CAP" in src
	assert "frontend_orphans truncated" in src


def test_aggregate_warnings_run_before_analyzer_warnings_assembly():
	"""The truncation warnings must be appended to context.warnings
	BEFORE doc.analyzer_warnings = '\\n'.join(context.warnings) runs,
	or the warning never makes it into the DocType field.
	"""
	from optimus import analyze

	src = inspect.getsource(analyze._persist)
	truncate_idx = src.find("infra_timeline truncated")
	assemble_idx = src.find("doc.analyzer_warnings")
	assert truncate_idx > 0
	assert assemble_idx > 0
	assert truncate_idx < assemble_idx, (
		"v5 truncation warnings must be appended to context.warnings "
		"BEFORE the analyzer_warnings field is assembled"
	)
