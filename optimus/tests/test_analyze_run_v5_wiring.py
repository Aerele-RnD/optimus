# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Source-inspection regression guards for analyze.py's v0.5.0 wiring.

These protect against silent removal/renaming of the v0.5.0 integration
points. Since analyze.run is a large orchestrator that's hard to
exercise end-to-end without a running Frappe site, the cheapest
regression guard is to check that the wiring symbols literally appear
in the source.
"""

import inspect

from optimus import analyze


def test_analyze_imports_v5_analyzers():
	src = inspect.getsource(analyze)
	assert "infra_pressure" in src
	assert "frontend_timings" in src


def test_builtin_analyzers_list_includes_v5():
	# _BUILTIN_ANALYZERS is the list consumed by _get_analyzers which
	# drives the run loop. If this list loses the v0.5.0 analyzers,
	# they never fire.
	assert any(
		a.__module__.endswith("infra_pressure")
		for a in analyze._BUILTIN_ANALYZERS
	), "infra_pressure.analyze missing from _BUILTIN_ANALYZERS"
	assert any(
		a.__module__.endswith("frontend_timings")
		for a in analyze._BUILTIN_ANALYZERS
	), "frontend_timings.analyze missing from _BUILTIN_ANALYZERS"


def test_run_loads_frontend_data_into_context():
	src = inspect.getsource(analyze.run)
	# v0.12.0+: the Redis key is built via optimus.redis_keys (the inline
	# f-string ``"profiler:frontend:<uuid>"`` was migrated). Assert the
	# wiring still calls the canonical builder + assigns to the context.
	assert "frontend_legacy(" in src or "frontend_xhr(" in src
	assert "context.frontend_data" in src


def test_run_attaches_infra_to_recordings():
	src = inspect.getsource(analyze.run)
	# v0.12.0+: Per-recording infra dicts are read through
	# ``redis_keys.infra(uuid)`` (the literal ``profiler:infra:`` f-string
	# was migrated). Assert the wiring still calls the builder + attaches
	# the value as ``rec["infra"]`` before the analyzer loop runs.
	assert "redis_keys.infra(" in src or "_redis_keys.infra(" in src
	# The assignment can be spelled rec["infra"] or rec['infra']; accept either.
	assert 'rec["infra"]' in src or "rec['infra']" in src


def test_persist_writes_v5_aggregate_json():
	src = inspect.getsource(analyze._persist)
	# _persist must serialize the v0.5.0 aggregate into v5_aggregate_json
	# on the session doc, or the renderer gets nothing to work with.
	assert "v5_aggregate_json" in src
	# And it must read at least one of the v0.5.0 aggregate keys from context.
	assert (
		"infra_timeline" in src
		and "frontend_xhr_matched" in src
	)



def test_truncate_finding_titles_clamps_overlong():
	"""Safety net: _persist clamps any finding.title that exceeds the
	140-char Optimus Finding.title limit. Analyzers are supposed to
	produce short titles (via base.short_filename), but pathological
	inputs from future analyzers or unexpected data shapes could still
	push past the limit. Clamping prevents CharacterLengthExceededError
	from destroying the whole analyze pipeline."""
	findings = [
		# Under the limit — untouched.
		{"title": "Short title"},
		# Exactly at the limit — untouched.
		{"title": "A" * 140},
		# One over the limit — must be clamped to 140 and end with "...".
		{"title": "B" * 141},
		# Far over the limit — must be clamped to exactly 140.
		{"title": "C" * 500},
		# The production payload that started this bug.
		{
			"title": (
				"Same query ran 65× at "
				"jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
				"doctype/parent_manufacturing_order/"
				"parent_manufacturing_order.py:503"
			)
		},
	]
	analyze._truncate_finding_titles(findings)

	assert findings[0]["title"] == "Short title"
	assert findings[1]["title"] == "A" * 140
	assert len(findings[1]["title"]) == 140

	assert len(findings[2]["title"]) == 140
	assert findings[2]["title"].endswith("...")

	assert len(findings[3]["title"]) == 140
	assert findings[3]["title"].endswith("...")

	# Production payload — was 144 chars, must now be <= 140.
	assert len(findings[4]["title"]) <= 140


def test_truncate_finding_titles_ignores_missing_title_key():
	"""A finding dict missing the title key must not crash the
	truncator. It should treat None/empty as untouched."""
	findings = [{}, {"title": None}, {"title": ""}]
	# Must not raise.
	analyze._truncate_finding_titles(findings)
	# Missing / None / empty → unchanged.
	assert findings[0] == {}
	assert findings[1]["title"] is None
	assert findings[2]["title"] == ""
