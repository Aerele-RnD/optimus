# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Doc-event lifecycle binding + per-DocType breakdown.

v0.6.x render-time enrichment that ties each action to the document it
touched (from ``form_dict``) and each finding to the doc-event lifecycle
hook (``validate`` / ``on_submit`` / etc.) the function was registered
under. Used downstream by the call-tree, finding-card, and "Doc-event
lifecycle" report sections.

Two public surfaces (both called from the render orchestrator in
``_internal.py``):

* ``_attach_action_context(actions, findings, recordings_by_uuid)`` —
  in-place enrichment of ``action["target_doc"]``,
  ``finding["technical_detail"]["target_doc"]``, and
  ``finding["technical_detail"]["hook_events"]``. Also rewrites
  ``action["action_label"]`` / ``action["path"]`` /
  ``action["entry_callsite"]["function"]`` / the matching
  ``finding["customer_description"]`` italicised phrase to carry the
  DocType suffix.
* ``_build_doc_event_breakdown(findings)`` — pure-function transform
  that groups findings by DocType → lifecycle event for the
  "Doc-event lifecycle" report section.

Extracted from ``_internal.py`` in v0.12.10 per the v0.10.0 renderer-
package roadmap. Self-contained: only external dependency is a lazy
``frappe.get_hooks`` lookup inside ``_doc_event_hook_index``; safe to
import without a running site (returns ``{}``).
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Frappe's doc-event lifecycle method names — a function whose bare name is one
# of these AND whose file is a controller (``.../doctype/<scrub>/<scrub>.py``)
# is a lifecycle override.
_LIFECYCLE_EVENTS = frozenset({
	"before_naming", "autoname", "before_insert", "after_insert",
	"before_validate", "validate", "before_save", "after_save", "on_update",
	"before_submit", "on_submit", "before_update_after_submit", "on_update_after_submit",
	"before_cancel", "on_cancel", "before_change", "on_change",
	"on_trash", "after_delete", "before_rename", "after_rename", "before_print",
})

# Doc-event "kinds" surfaced in the breakdown.
_KIND_DOC_EVENTS_HOOK = "doc_events hook"
_KIND_CONTROLLER_OVERRIDE = "controller override"

# Local copy of the severity-rank lookup. Kept here rather than imported
# from ``_internal`` to avoid the circular import (``_internal`` re-imports
# from this submodule). The name ``_SEVERITY_RANK`` is intentionally
# distinct from ``_internal``'s ``_GROUPING_SEVERITY_RANK`` (different
# value sets, different uses).
_SEVERITY_RANK = {"High": 3, "Medium": 2, "Low": 1}


# ---------------------------------------------------------------------------
# Filename / path helpers
# ---------------------------------------------------------------------------


def _module_from_filename(filename) -> str:
	"""``ugly_code/python/common.py`` → ``ugly_code.python.common`` (pyinstrument's
	short app-relative filename → its module dotted path). Empty on bad input."""
	if not filename:
		return ""
	name = str(filename).replace("\\", "/")
	if name.endswith(".py"):
		name = name[:-3]
	return ".".join(p for p in name.split("/") if p)


def _doctype_from_controller_path(filename) -> str | None:
	"""``erpnext/accounts/doctype/sales_invoice/sales_invoice.py`` → ``"Sales Invoice"``
	(the segment right after ``doctype/``, un-scrubbed). Works on app-relative,
	bench-relative, and absolute paths. ``None`` for non-controller paths. NB:
	``.title()`` mangles multi-cap names ("gl_entry" → "Gl Entry") — same as
	``frappe.unscrub``; accepted."""
	if not filename:
		return None
	parts = [p for p in str(filename).replace("\\", "/").strip("/").split("/") if p]
	try:
		i = parts.index("doctype")
	except ValueError:
		return None
	if i + 1 >= len(parts):
		return None
	slug = parts[i + 1].strip()
	if not slug:
		return None
	return slug.replace("_", " ").replace("-", " ").title()


# ---------------------------------------------------------------------------
# Target-doc extraction + doc-event hook indexing
# ---------------------------------------------------------------------------


def _extract_target_doc(form_dict) -> dict | None:
	"""Best-effort: pull ``{"doctype", "name"}`` out of a request's form_dict
	for doc-mutating endpoints — ``savedocs`` / ``frappe.client.save|insert|submit``
	(a ``doc`` JSON string or dict), ``run_doc_method`` (``dt``/``dn`` or a
	``docs`` JSON), ``apply_workflow`` (``doc``), or bare ``doctype``/``name``
	fields. ``name`` may be a temp name ("new-…") or ``None`` for an unsaved
	doc. Returns ``None`` when nothing doc-shaped is present. Never raises."""
	if not isinstance(form_dict, dict) or not form_dict:
		return None
	try:
		dt = form_dict.get("dt") or form_dict.get("doctype")
		dn = form_dict.get("dn") or form_dict.get("name") or form_dict.get("docname")
		if isinstance(dt, str) and dt.strip():
			return {"doctype": dt.strip(), "name": (dn.strip() if isinstance(dn, str) and dn.strip() else None)}
		for key in ("doc", "docs"):
			raw = form_dict.get(key)
			if raw is None:
				continue
			parsed = raw
			if isinstance(raw, str):
				try:
					parsed = json.loads(raw)
				except Exception:
					continue
			if isinstance(parsed, list):
				parsed = next((d for d in parsed if isinstance(d, dict) and d.get("doctype")), None)
			if isinstance(parsed, dict) and parsed.get("doctype"):
				nm = parsed.get("name")
				return {"doctype": str(parsed["doctype"]), "name": (str(nm) if nm else None)}
	except Exception:
		return None
	return None


def _build_doc_event_hook_index(doc_events) -> dict:
	"""Flatten Frappe's ``doc_events`` map — ``{doctype: {event: [paths]}}``
	(``doctype`` may be ``"*"``) — into ``{dotted_path: [(doctype, event), …]}``.
	Pure. Empty on bad input."""
	index: dict[str, list[tuple[str, str]]] = {}
	if not isinstance(doc_events, dict):
		return index
	for doctype, events in doc_events.items():
		if not isinstance(events, dict):
			continue
		for event, methods in events.items():
			if isinstance(methods, str):
				methods = [methods]
			if not isinstance(methods, (list, tuple)):
				continue
			for m in methods:
				if isinstance(m, str) and m:
					index.setdefault(m, []).append((str(doctype), str(event)))
	return index


def _doc_event_hook_index() -> dict:
	"""``_build_doc_event_hook_index(frappe.get_hooks("doc_events"))`` — or ``{}``
	when frappe isn't available (e.g. unit tests with no running site)."""
	try:
		import frappe
		return _build_doc_event_hook_index(frappe.get_hooks("doc_events"))
	except Exception:
		return {}


def _finding_hook_events(detail, hook_index, *, action_doctype: str | None = None) -> list[dict]:
	"""For a call-tree finding's ``technical_detail`` (``function`` + ``filename``),
	return the doc-event lifecycle hook(s) the function is registered for, as
	``[{"doctype", "event"}, …]``. A ``"*"`` (all-doctypes) hook is reported
	against ``action_doctype`` when known, else ``"*"``. Empty when the function
	isn't a registered doc-event hook (or its dotted path can't be rebuilt)."""
	if not isinstance(detail, dict) or not hook_index:
		return []
	func = str(detail.get("function") or "").strip()
	filename = detail.get("filename") or ""
	if not func or not filename:
		return []
	bare = func.rsplit(".", 1)[-1].rsplit(":", 1)[-1].strip()
	module = _module_from_filename(filename)
	if not module or not bare:
		return []
	pairs = hook_index.get(f"{module}.{bare}")
	if not pairs:
		return []
	out: list[dict] = []
	seen = set()
	for hd, ev in pairs:
		shown_dt = action_doctype if (hd == "*" and action_doctype) else hd
		if (shown_dt, ev) in seen:
			continue
		seen.add((shown_dt, ev))
		out.append({"doctype": shown_dt, "event": ev})
	return out


# ---------------------------------------------------------------------------
# Action / finding context attachment (public)
# ---------------------------------------------------------------------------


def _attach_action_context(actions, findings, recordings_by_uuid) -> None:
	"""Enrich ``actions`` and ``findings`` (in place):

	  * ``action["target_doc"]`` — the document a save/submit-style action
	    touched (from the recording's form_dict), or ``None``.
	  * ``finding["technical_detail"]["target_doc"]`` — same, via the finding's
	    ``action_ref`` → action (key omitted when there's no doc).
	  * ``finding["technical_detail"]["hook_events"]`` — the doc-event lifecycle
	    hook(s) the finding's hot function fired in (``[{doctype,event}, …]``);
	    key omitted when the function isn't a registered ``doc_events`` hook.
	"""
	recordings_by_uuid = recordings_by_uuid or {}
	# Build a per-action finding index so the fallback path (below) can
	# infer doctype from a related finding's controller-path callsite when
	# the recording's form_dict isn't available (Redis TTL expired).
	_findings_by_action_idx: dict[int, list] = {}
	for _f in (findings or []):
		if not isinstance(_f, dict):
			continue
		_ref = (_f.get("action_ref") or "").strip()
		if _ref.isdigit():
			_findings_by_action_idx.setdefault(int(_ref), []).append(_f)
	for a in (actions or []):
		if not isinstance(a, dict):
			continue
		rec = recordings_by_uuid.get(a.get("recording_uuid") or "")
		td = _extract_target_doc(rec.get("form_dict") if isinstance(rec, dict) else None)
		# v0.7.x J.13: fallback when the recording isn't available
		# (Redis TTL expired between record-time and regenerate-time) —
		# infer the action's DocType from any related finding's callsite
		# filepath via the controller-path mapping
		# (e.g. ``erpnext/.../sales_invoice/sales_invoice.py`` → "Sales Invoice").
		# Bench-side reports for old sessions get the DocType suffix
		# without needing the recording or a fresh capture.
		if not td:
			for _rf in _findings_by_action_idx.get(a.get("idx"), []):
				_detail = _rf.get("technical_detail") or {}
				_cs = _detail.get("callsite") if isinstance(_detail, dict) else None
				_fname = _cs.get("filename") if isinstance(_cs, dict) else None
				_inferred = _doctype_from_controller_path(_fname or "")
				if _inferred:
					td = {"doctype": _inferred, "name": None}
					break
		a["target_doc"] = td
		# v0.7.x J.8: enrich the action_label with the DocType suffix so
		# every downstream consumer (TLDR composer, action-plan composer,
		# per-action table cell, waterfall row, queries-per-action heading,
		# related-finding inline link) shows the doctype inline without
		# requiring template edits. Idempotent: the `" (" not in label`
		# guard means re-renders pass through untouched.
		_td_doctype = (a.get("target_doc") or {}).get("doctype") if isinstance(a.get("target_doc"), dict) else None
		_a_label = a.get("action_label") or ""
		if _td_doctype and _a_label and " (" not in _a_label:
			a["action_label"] = f"{_a_label} ({_td_doctype})"
		# v0.7.x J.9: same suffix on the URL path so the per-action
		# `.action-meta` line ("POST /api/method/...") and the
		# queries-per-action heading carry the doctype too. Idempotent on
		# `" (" not in path` for re-renders.
		_a_path = a.get("path") or ""
		if _td_doctype and _a_path and " (" not in _a_path:
			a["path"] = f"{_a_path} ({_td_doctype})"
		# v0.7.x J.11: enrich the entry-callsite `function` so the small
		# `.action-meta` line under each action row reads
		# `…/save.py:16 · savedocs (Sales Invoice)` — keeps every filepath
		# surface in the report consistently tagged with its DocType.
		_ec = a.get("entry_callsite")
		if _td_doctype and isinstance(_ec, dict):
			_ec_fn = _ec.get("function") or ""
			if _ec_fn and " (" not in _ec_fn:
				_ec["function"] = f"{_ec_fn} ({_td_doctype})"
	by_idx = {a.get("idx"): a for a in (actions or []) if isinstance(a, dict)}
	hook_index = _doc_event_hook_index()
	for f in (findings or []):
		if not isinstance(f, dict):
			continue
		detail = f.get("technical_detail")
		if not isinstance(detail, dict):
			continue
		ref = (f.get("action_ref") or "").strip()
		td = None
		if ref.isdigit():
			act = by_idx.get(int(ref))
			td = act.get("target_doc") if isinstance(act, dict) else None
		if td:
			detail["target_doc"] = td
			# v0.7.x J.7: inject the DocType inline at the end of the
			# italicised action-label phrase so the reader sees
			# "During *frappe.desk.form.save.savedocs:Save (Sales Invoice)*"
			# instead of "During *frappe.desk.form.save.savedocs:Save*" —
			# keeps the technical label and the doctype emphasised together
			# inside one self-contained italic phrase. Guards on the
			# `"During *…*"` prose shape so self-referential / non-action
			# descriptions pass through untouched.
			_doctype = td.get("doctype") if isinstance(td, dict) else None
			if _doctype:
				_desc = f.get("customer_description") or ""
				if _desc.startswith("During *"):
					_close_idx = _desc.find("*", len("During *"))
					if _close_idx > 0:
						_action_label = _desc[len("During *"):_close_idx]
						_original_chunk = f"*{_action_label}*"
						_new_chunk = f"*{_action_label} ({_doctype})*"
						f["customer_description"] = _desc.replace(
							_original_chunk, _new_chunk, 1
						)
		hevs = _finding_hook_events(detail, hook_index, action_doctype=(td or {}).get("doctype"))
		if hevs:
			detail["hook_events"] = hevs


# ---------------------------------------------------------------------------
# v0.6.x: "Doc-event lifecycle" section — re-group the slow call-tree findings
# by DocType → lifecycle event (validate / on_submit / …), tagging each as a
# registered ``doc_events`` hook vs a controller method override, and surfacing
# cascaded DocTypes (e.g. GL Entry touched during a Sales Invoice submit).
# Pure, render-time, derived from the findings already enriched by
# _attach_action_context.
# ---------------------------------------------------------------------------


def _finding_lifecycle_bindings(finding) -> list[tuple[str, str, str]]:
	"""Return ``[(doctype, event, kind), …]`` — the doc-event lifecycle slots a
	finding belongs to (usually 0 or 1). ``kind`` is ``_KIND_DOC_EVENTS_HOOK``
	(from the finding's already-resolved ``technical_detail.hook_events``) or
	``_KIND_CONTROLLER_OVERRIDE`` (function name is a lifecycle event AND its
	file is a controller). Deduped by ``(doctype, event)`` — hook bindings first.
	Empty when the finding isn't a lifecycle method (a generic Slow Hot Path on
	a helper, an N+1 with no controller callsite, …)."""
	if not isinstance(finding, dict):
		return []
	detail = finding.get("technical_detail") or {}
	if not isinstance(detail, dict):
		return []
	cs = detail.get("callsite") or {}
	out: list[tuple[str, str, str]] = []
	seen: set[tuple[str, str]] = set()

	for he in (detail.get("hook_events") or []):
		if not isinstance(he, dict):
			continue
		dt = (he.get("doctype") or "").strip()
		ev = (he.get("event") or "").strip()
		if dt and ev and (dt, ev) not in seen:
			seen.add((dt, ev))
			out.append((dt, ev, _KIND_DOC_EVENTS_HOOK))

	fn = (cs.get("function") if isinstance(cs, dict) else None) or detail.get("function") or ""
	ev = str(fn).rsplit(".", 1)[-1].rsplit(":", 1)[-1].strip()
	if ev in _LIFECYCLE_EVENTS:
		fname = (cs.get("filename") if isinstance(cs, dict) else None) or detail.get("filename") or ""
		dt = _doctype_from_controller_path(fname)
		if dt and (dt, ev) not in seen:
			seen.add((dt, ev))
			out.append((dt, ev, _KIND_CONTROLLER_OVERRIDE))
	return out


def _build_doc_event_breakdown(findings) -> dict:
	"""Group the slow call-tree findings by DocType → lifecycle event. Pure.

	Returns ``{"doctypes": [ {doctype, is_save_target, touched_during,
	total_ms, method_count, events: [{event, total_ms, methods:
	[{function, filename, _abs, lineno, ms, count, kind, severity,
	finding_type}]}]} … ], "count": int, "method_count": int}``. Empty
	``{"doctypes": [], "count": 0, "method_count": 0}`` when nothing binds."""
	groups: dict[str, dict] = {}
	for f in (findings or []):
		bindings = _finding_lifecycle_bindings(f)
		if not bindings:
			continue
		detail = f.get("technical_detail") or {}
		cs = detail.get("callsite") or {}
		if not isinstance(cs, dict):
			cs = {}
		fn_name = cs.get("function") or detail.get("function") or "?"
		fname = cs.get("filename") or detail.get("filename") or ""
		abs_path = cs.get("_abs")
		lineno = cs.get("lineno") or detail.get("lineno")
		try:
			ms = float(detail.get("cumulative_ms") or f.get("estimated_impact_ms") or 0)
		except (TypeError, ValueError):
			ms = 0.0
		severity = f.get("severity") or "Low"
		ftype = f.get("finding_type") or ""
		action_dt = (detail.get("target_doc") or {}).get("doctype") if isinstance(detail.get("target_doc"), dict) else None

		for dt, ev, kind in bindings:
			g = groups.setdefault(dt, {"doctype": dt, "is_save_target": False, "touched_during": set(), "events": {}})
			if action_dt:
				if action_dt == dt:
					g["is_save_target"] = True
				else:
					g["touched_during"].add(action_dt)
			ev_bucket = g["events"].setdefault(ev, {})
			key = (fn_name, fname)
			rec = ev_bucket.get(key)
			if rec is None:
				ev_bucket[key] = {
					"function": fn_name, "filename": fname, "_abs": abs_path,
					"lineno": lineno, "ms": ms, "count": 1, "kind": kind,
					"severity": severity, "finding_type": ftype,
				}
			else:
				rec["ms"] += ms
				rec["count"] += 1
				if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(rec["severity"], 0):
					rec["severity"] = severity
				# A controller override is the more specific label — prefer it.
				if kind == _KIND_CONTROLLER_OVERRIDE:
					rec["kind"] = kind

	out_doctypes: list[dict] = []
	for g in groups.values():
		events_out: list[dict] = []
		for ev, bucket in g["events"].items():
			methods = sorted(bucket.values(), key=lambda m: -(m["ms"] or 0))
			events_out.append({"event": ev, "total_ms": sum(m["ms"] or 0 for m in methods), "methods": methods})
		events_out.sort(key=lambda e: -(e["total_ms"] or 0))
		out_doctypes.append({
			"doctype": g["doctype"],
			"is_save_target": g["is_save_target"],
			"touched_during": sorted(g["touched_during"]),
			"total_ms": sum(e["total_ms"] or 0 for e in events_out),
			"method_count": sum(len(e["methods"]) for e in events_out),
			"events": events_out,
		})
	# Sort: save-targets first, then by total time.
	out_doctypes.sort(key=lambda d: (0 if d["is_save_target"] else 1, -(d["total_ms"] or 0)))
	return {
		"doctypes": out_doctypes,
		"count": len(out_doctypes),
		"method_count": sum(d["method_count"] for d in out_doctypes),
	}
