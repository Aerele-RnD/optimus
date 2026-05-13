# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Source-inspection guard: every finding_type string emitted by an
analyzer must exist in the Optimus Finding DocType's Select allowlist.

If an analyzer starts producing a new finding_type and the JSON isn't
updated, Frappe's Select-field validation raises ValidationError at
doc.save() time, destroying the whole analyze run.

This caught v0.5.1's ``Framework N+1`` type after it had already
shipped — once a session produced a Framework N+1 finding, analyze
crashed with:

    frappe.exceptions.ValidationError: Row #1: Type cannot be
    "Framework N+1". It should be one of "N+1 Query", ...

The test walks analyzers/*.py for ``"finding_type": <str>`` literals
and compares them against the options parsed from the DocType JSON.
"""

import json
import os
import re

HERE = os.path.dirname(__file__)


def _load_doctype_options() -> set[str]:
	jpath = os.path.join(
		HERE,
		"..",
		"optimus",
		"doctype",
		"optimus_finding",
		"optimus_finding.json",
	)
	with open(jpath) as f:
		meta = json.load(f)
	fields = meta.get("fields") or []
	target = next(
		(f for f in fields if f.get("fieldname") == "finding_type"),
		None,
	)
	assert target is not None, "finding_type field missing from Optimus Finding"
	assert target["fieldtype"] == "Select"
	opts = target.get("options") or ""
	return {o.strip() for o in opts.split("\n") if o.strip()}


def _scan_analyzers_for_finding_types() -> set[str]:
	"""Walk analyzers/*.py and extract every string literal that appears
	after a ``"finding_type":`` key. Regex-based, so it works without
	importing any analyzer module (which would drag in frappe).
	"""
	analyzers_dir = os.path.abspath(os.path.join(HERE, "..", "analyzers"))
	pattern = re.compile(
		r'["\']finding_type["\']\s*:\s*["\']([^"\']+)["\']'
	)
	emitted: set[str] = set()
	for name in os.listdir(analyzers_dir):
		if not name.endswith(".py"):
			continue
		with open(os.path.join(analyzers_dir, name)) as f:
			src = f.read()
		for m in pattern.finditer(src):
			emitted.add(m.group(1))
	return emitted


def test_every_emitted_finding_type_is_in_doctype_options():
	"""Every string used as `finding_type` by an analyzer must be
	listed in the Optimus Finding DocType's Select options.
	Otherwise doc.save() throws ValidationError at persist time."""
	options = _load_doctype_options()
	emitted = _scan_analyzers_for_finding_types()
	missing = emitted - options
	assert not missing, (
		f"Analyzer(s) emit finding_type values that aren't in "
		f"Optimus Finding's Select options: {sorted(missing)}. "
		f"Add them to optimus_finding.json. Current options: "
		f"{sorted(options)}"
	)


def test_framework_n_plus_one_is_in_options():
	"""Direct guard for the v0.5.1 regression that crashed analyze
	with 'Type cannot be "Framework N+1"'."""
	options = _load_doctype_options()
	assert "Framework N+1" in options, (
		"'Framework N+1' must be in Optimus Finding's select options "
		"— n_plus_one.py emits this finding type for pure-frappe "
		"stacks and Frappe's DocType validation will reject it "
		"otherwise"
	)
