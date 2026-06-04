// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt
//
// Optimus Session form script (Phase 5).
//
// Customizes the detail view to feel like a "report" rather than a raw
// data form. The customer-facing summary HTML is rendered prominently at
// the top, the analyzer findings are listed in a friendly format, and the
// two report files get prominent download buttons (raw is gated to admins).

frappe.ui.form.on("Optimus Session", {
	refresh(frm) {
		render_status_indicator(frm);
		render_drain_progress(frm);
		render_download_buttons(frm);
		render_retry_button(frm);
		render_regenerate_report_button(frm);
		render_findings_summary(frm);
		render_phase2_button(frm);
		render_ai_buttons(frm);
		subscribe_phase2_events(frm);
		subscribe_session_progress(frm);
	},
});

// Single AI button: "Refresh AI suggestions". Replaces five legacy
// buttons (Suggest a fix / Generate AI fixes / Re-evaluate AI fixes /
// Humanize Steps / Suggest an index). One server endpoint
// (api.refill_ai_suggestions) runs all three operations per-section
// and re-renders the report once. The master AI switch still gates
// whether the button appears at all; per-section toggles are honored
// inside the server endpoint (a toggle-off section is skipped silently).
function render_ai_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	frappe.call({
		method: "optimus.api.ai_capabilities",
		callback: (r) => {
			const c = (r && r.message) || {};
			if (!c.enabled) return; // master switch off → no AI button
			render_ai_refill_button(frm);
		},
		// Read failed (e.g. not configured / no perm) → render nothing.
		error: () => {},
	});
}

// Show a live headline on the form while analyze is running (the floating
// widget shows the same progress, but if you're sitting on the Profiler
// Session form you shouldn't have to stare at a static "Analyzing" status
// — especially when AI fix suggestions are being generated, which can take
// a while). Cleared + reloaded when the session reaches Ready / Failed.
function subscribe_session_progress(frm) {
	if (frm.is_new()) return;
	if (frm._progress_subscribed) return;
	frm._progress_subscribed = true;

	const mine = (p) => p && p.session_uuid === frm.doc.session_uuid;

	frappe.realtime.on("optimus_progress", (p) => {
		if (!mine(p)) return;
		const pct = typeof p.percent === "number" ? Math.round(p.percent) : null;
		const desc = frappe.utils.escape_html(p.description || "Analyzing…");
		frm.dashboard.set_headline(
			'<span class="text-muted">' +
				'<i class="fa fa-spinner fa-spin" style="margin-right:6px;"></i>' +
				(pct !== null ? __("Preparing report — {0}% · {1}", [pct, desc]) : desc) +
				"</span>"
		);
	});
	frappe.realtime.on("optimus_session_ready", (p) => {
		if (!mine(p)) return;
		frm.dashboard.clear_headline();
		frappe.show_alert({ message: __("Report ready"), indicator: "green" });
		setTimeout(() => frm.reload_doc(), 800);
	});
	frappe.realtime.on("optimus_session_failed", (p) => {
		if (!mine(p)) return;
		frm.dashboard.clear_headline();
		setTimeout(() => frm.reload_doc(), 800);
	});
	// v0.7.x: auto-arm fires server-side during analyze (off-form). Tell the
	// user a pass is armed and what to do next; the reload re-runs
	// render_phase2_button → the in-form banner + Stop button appear.
	frappe.realtime.on("optimus_phase2_armed", (p) => {
		if (!p || p.docname !== frm.doc.name) return;
		const fns = (p.functions || []).join(", ");
		frappe.show_alert(
			{
				message: __(
					"Line profiling armed for {0} hot path(s){1}. Re-run your " +
					"flow, then click Stop Phase 2 Run to capture the exact lines.",
					[p.count || 0, fns ? " (" + frappe.utils.escape_html(fns) + ")" : ""]
				),
				indicator: "orange",
			},
			12
		);
		setTimeout(() => frm.reload_doc(), 800);
	});
}

// Single AI button: "Refresh AI suggestions" — replaces five legacy
// buttons (Suggest a fix / Generate AI fixes / Re-evaluate AI fixes /
// Humanize Steps / Suggest an index). One server endpoint runs all
// three AI operations server-side and re-renders the report once at
// the end. The per-section toggles still gate which operations run
// inside the endpoint — a toggle-off section is skipped silently.
function render_ai_refill_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	frm.add_custom_button(
		__("Refresh AI suggestions"),
		() => {
			frappe.confirm(
				__(
					"Refresh every AI-generated section of the report? " +
						"This re-runs fix suggestions on findings, the " +
						"humanized Steps to Reproduce, and index advice " +
						"for tables with a candidate — then re-renders " +
						"the report once. Calls the configured LLM for " +
						"each, so it can take a bit. If it doesn't " +
						"finish in one pass, run it again."
				),
				() => _refill_ai_call(frm)
			);
		},
		__("AI")
	);
}

function _refill_ai_call(frm) {
	frappe.call({
		method: "optimus.api.refill_ai_suggestions",
		args: { session_uuid: frm.doc.session_uuid },
		freeze: true,
		freeze_message: __("Refreshing AI suggestions & re-rendering the report…"),
		callback: (r) => {
			const m = (r && r.message) || {};
			const fx = m.fixes || {};
			const ix = m.indexes || {};
			const st = m.steps || {};
			const parts = [];
			if (fx.added) parts.push(__("{0} fix(es)", [fx.added]));
			if (st.updated) parts.push(__("steps rewritten"));
			if (ix.added) parts.push(__("{0} index suggestion(s)", [ix.added]));
			const msg = parts.length
				? __("Refreshed: {0}.", [parts.join(", ")])
				: __("Nothing to refresh.");
			const failed = (fx.failed || 0) + (ix.failed || 0);
			const skipped = (fx.skipped_time || 0) + (ix.skipped || 0);
			const indicator = failed ? "red" : parts.length ? "green" : "orange";
			frappe.show_alert({ message: msg, indicator: indicator });
			if (failed) {
				frappe.show_alert({
					message: __("{0} call(s) failed — old suggestions kept (see Error Log).", [failed]),
					indicator: "red",
				});
			}
			if (skipped) {
				frappe.show_alert({
					message: __("{0} skipped (time budget) — run it again for the rest.", [skipped]),
					indicator: "orange",
				});
			}
			setTimeout(() => frm.reload_doc(), 1200);
		},
		error: () => {
			frappe.show_alert({
				message: __("The AI refresh request failed — see the error popup for details."),
				indicator: "red",
			});
		},
	});
}

// v0.6.0: Phase-2 line-profile picker.
//
// Adds a "Run Line-Profile Pass" custom button when the session is Ready.
// Clicking opens a dialog that fetches curated candidates (top hot frames
// from phase-1) plus a free-form textbox for dotted paths the user types.
// Submission posts to api.start_line_profile_pass; realtime events drive
// the form's Phase-2 history child table updates.
function render_phase2_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	// If there's an in-flight Recording row, surface Stop as the primary
	// affordance — that's what the user is looking for after they've
	// reproduced their flow.
	var recording = (frm.doc.phase_2_runs || []).find(function (r) {
		return r.status === "Recording";
	});

	if (recording) {
		var stop_btn = frm.add_custom_button(
			__("Stop Phase 2 Run"),
			function () {
				frappe.call({
					method: "optimus.api.stop_line_profile_pass",
					args: { run_uuid: recording.run_uuid },
					freeze: true,
					freeze_message: __("Stopping phase 2..."),
					callback: function () {
						frappe.show_alert({
							message: __(
								"Phase 2 stopped. Analyzing now — the report " +
								"section will refresh when ready."
							),
							indicator: "blue",
						});
						frm.reload_doc();
					},
				});
			},
			__("Phase 2")
		);
		// Visually emphasize the stop action while a run is live.
		try {
			stop_btn.removeClass("btn-default").addClass("btn-warning");
		} catch (e) { /* noop */ }

		// v0.7.x: a Recording pass does nothing until the flow re-executes and
		// the pass is stopped. Auto-arm (and the picker) leave users staring at
		// a Stop button with no context — spell out the two steps.
		frm.set_intro(
			__(
				"🔬 Line profiling is armed. Re-run your flow now so the hot " +
				"path(s) execute again, then click \"Stop Phase 2 Run\" above — " +
				"the report will then pinpoint the exact hot line(s). " +
				"(Profiling has to re-execute your code; it can't replay the " +
				"original run.)"
			),
			"orange"
		);
	} else {
		// Clear the armed banner once no pass is Recording (e.g. after Stop).
		frm.set_intro(null);
	}

	// Surface a Retry button for any Phase 2 Run row stuck in Analyzing
	// or Failed. The most common cause of stuck Analyzing is a dev site
	// running without `bench start` — no RQ worker picks up the long
	// queue. retry_phase2_analyze runs inline so the click resolves
	// directly to Ready or Failed.
	var stuck_runs = (frm.doc.phase_2_runs || []).filter(function (row) {
		return row.status === "Analyzing" || row.status === "Failed";
	});

	// v0.6.x: when there are 2+ stuck runs, surface a SINGLE "Retry all
	// stuck Phase-2 runs" button that fires ONE batched server call
	// (addresses Lens-audit "frappe.call(...) inside a loop"). The
	// per-run buttons below stay — they let the operator retry one
	// specific run when only one is misbehaving.
	if (stuck_runs.length >= 2) {
		frm.add_custom_button(
			__("Retry all " + stuck_runs.length + " stuck Phase-2 runs"),
			function () {
				frappe.call({
					method: "optimus.api.retry_phase2_analyzes_batch",
					args: { run_uuids: stuck_runs.map(function (r) { return r.run_uuid; }) },
					freeze: true,
					freeze_message: __("Re-running phase-2 analyzers..."),
					callback: function (r) {
						var msg = (r && r.message) || {};
						var t = msg.tallies || {};
						frappe.show_alert({
							message: __(
								"Batch retry finished — " +
								(t.Ready || 0) + " Ready · " +
								(t.Failed || 0) + " Failed" +
								((t.Analyzing || 0) ? " · " + t.Analyzing + " still Analyzing" : "")
							),
							indicator: (t.Failed || 0) === 0 ? "green" : "orange",
						});
						frm.reload_doc();
					},
				});
			},
			__("Phase 2")
		);
	}

	stuck_runs.forEach(function (row) {
		frm.add_custom_button(
			__("Retry Phase 2 Analyze (" + row.run_uuid.slice(0, 8) + ")"),
			function () {
				frappe.call({
					method: "optimus.api.retry_phase2_analyze",
					args: { run_uuid: row.run_uuid },
					freeze: true,
					freeze_message: __("Re-running phase-2 analyzer..."),
					callback: function (r) {
						var msg = (r && r.message) || {};
						frappe.show_alert({
							message: __(
								"Retry finished — status: " +
								(msg.status || "unknown") +
								(msg.error ? " · " + msg.error : "")
							),
							indicator: msg.status === "Ready" ? "green" : "red",
						});
						frm.reload_doc();
					},
				});
			},
			__("Phase 2")
		);
	});

	frm.add_custom_button(__("Run Line-Profile Pass"), function () {
		open_phase2_picker(frm);
	}, __("Phase 2"));

	// Recovery hatch: force-clear a stuck phase-2 active flag if a
	// previous run never reached Stop (worker crash, tab close, etc.).
	// Idempotent — safe to click when nothing is stuck.
	frm.add_custom_button(__("Force Stop Stuck Run"), function () {
		frappe.confirm(
			__(
				"Clear any in-flight phase-2 active flag for your user " +
				"and mark stuck Recording rows as Failed? Use this if " +
				"the picker keeps reporting 'phase-2 already active'."
			),
			function () {
				frappe.call({
					method: "optimus.api.force_stop_phase2",
					callback: function (r) {
						var msg = r && r.message ? r.message : {};
						frappe.show_alert({
							message: __(
								"Phase 2 cleared — flag was " +
								(msg.cleared_active_flag ? "set" : "already clear") +
								"; " +
								(msg.rows_marked_failed || 0) +
								" rows marked Failed."
							),
							indicator: "blue",
						});
						frm.reload_doc();
					},
				});
			}
		);
	}, __("Phase 2"));
}

function open_phase2_picker(frm) {
	// First fetch the candidate list from the API. We wait for the data
	// before opening the dialog so the MultiCheck field populates with
	// real options rather than rendering empty and re-rendering later.
	frappe.call({
		method: "optimus.api.get_phase2_candidates",
		args: { session_uuid: frm.doc.session_uuid },
		freeze: true,
		freeze_message: __("Loading phase-1 hot frames..."),
		callback: function (r) {
			var data = r.message || {};
			if (!data.line_profiler_available) {
				frappe.msgprint({
					title: __("line_profiler not installed"),
					message: __(
						"Phase 2 needs the line_profiler package. Install it via " +
						"<code>bench pip install line_profiler</code> and restart."
					),
					indicator: "red",
				});
				return;
			}
			show_phase2_dialog(frm, data);
		},
	});
}

function show_phase2_dialog(frm, data) {
	var primary = data.candidates || [];
	var framework = data.observations || [];

	// Phase K v0.7 GA: build a collapsible <details> tree from the
	// flat DFS pre-order candidate list. Each candidate's ``depth``
	// field tells us where to nest it; the stack-based walk re-builds
	// parent-child structure in one pass. Browser-native <details>
	// handles expand/collapse - no custom toggle JS needed. Children
	// start collapsed (closed <details>) so the user sees just the
	// top-level entries by default and drills in on demand.
	function build_tree_html(candidates) {
		if (!candidates.length) return "";
		var roots = [];
		var stack = [{depth: -1, children: roots}];
		candidates.forEach(function (c) {
			var depth = c.depth || 0;
			while (stack[stack.length - 1].depth >= depth) stack.pop();
			var node = {c: c, children: []};
			stack[stack.length - 1].children.push(node);
			stack.push({depth: depth, children: node.children});
		});

		function esc(s) {
			return frappe.utils.escape_html(String(s == null ? "" : s));
		}
		function meta(c) {
			return (
				" <span style='color:#6b7280;font-size:0.85em;'>(" +
				(c.cumulative_ms || 0).toFixed(1) + "ms &middot; " +
				(c.hit_count || 0) + "&times; hits &middot; " +
				esc(c.app) +
				")</span>"
			);
		}
		function row(node) {
			var c = node.c;
			var dotted = esc(c.dotted_path);
			// v0.7.x (P2): pre-tick the recommended hot paths so the user can
			// run a line-profile pass in one click without hunting for them.
			var cb = (
				"<input type='checkbox' class='fp-pick'" +
				" data-pick=\"" + dotted + "\"" +
				(c.recommended ? " checked" : "") +
				" onclick='event.stopPropagation()'" +
				" style='margin-right:6px;vertical-align:middle;'>"
			);
			var body = cb +
				"<span class='fp-pick-label' " +
				"style='font-family:var(--font-mono);font-size:0.85em;'>" +
				dotted + "</span>" + meta(c);

			if (node.children.length === 0) {
				return (
					"<div style='padding:2px 0 2px 22px;'>" +
					"<label style='cursor:pointer;'>" + body + "</label>" +
					"</div>"
				);
			}
			var inner = node.children.map(row).join("");
			return (
				"<details style='margin:2px 0;'>" +
				"<summary style='cursor:pointer;list-style:revert;" +
				"padding:2px 0;'>" +
				"<label style='cursor:pointer;' " +
				"onclick='event.stopPropagation()'>" +
				body + "</label>" +
				"</summary>" +
				"<div style='padding-left:18px;border-left:1px dashed " +
				"#d1d5db;margin-left:6px;'>" +
				inner +
				"</div>" +
				"</details>"
			);
		}
		return (
			"<div class='fp-tree' style='max-height:340px;" +
			"overflow-y:auto;border:1px solid var(--border-color, #e5e7eb);" +
			"border-radius:4px;padding:8px 12px;'>" +
			roots.map(row).join("") +
			"</div>"
		);
	}

	// When there are no user-app frames at all (vanilla ERPNext or a
	// site without custom apps), the framework list IS the primary
	// list — the customer is profiling erpnext / frappe code. Promote
	// it to default-expanded so the dialog shows usable candidates
	// instead of an empty primary section.
	var no_user_app = primary.length === 0 && framework.length > 0;

	var fields = [
		{
			fieldname: "intro_html",
			fieldtype: "HTML",
			options:
				"<p style='margin-bottom:8px;'>Tick functions from phase-1's " +
				"top hot frames, or paste a dotted path below. " +
				"Phase 2 will instrument <strong>only</strong> these " +
				"functions during your next reproduction of the flow.</p>",
		},
	];

	// Phase K v0.7 GA: when the picker has zero curated candidates, show
	// a yellow callout explaining why (no actions / no call trees / all
	// filtered) instead of a silently empty dialog. The diagnostic dict
	// is populated by api.get_phase2_candidates; older callers without
	// it fall back to a generic message.
	if (primary.length === 0 && framework.length === 0) {
		var diag = data.diagnostic || {};
		var hint = diag.hint || (
			"No curated picks were found. Use the freeform textbox below " +
			"to type the dotted path of the function you want to profile."
		);
		fields.push({
			fieldname: "empty_state_html",
			fieldtype: "HTML",
			options: (
				"<div style='padding:10px 14px;background:#fef3c7;" +
				"border:1px solid #fbbf24;border-radius:6px;" +
				"margin-bottom:12px;'>" +
				"<strong>No curated functions available</strong><br>" +
				"<span style='font-size:0.85em;color:#92400e'>" +
				hint +
				"</span><br><br>" +
				"<code style='font-size:0.78em;color:#6b7280'>" +
				"actions=" + (diag.action_count || 0) +
				" &middot; with_tree=" + (diag.actions_with_call_tree_json || 0) +
				" &middot; parsed=" + (diag.trees_parsed_ok || 0) +
				" &middot; pre_filter=" + (diag.raw_candidates_before_filter || 0) +
				"</code></div>"
			),
		});
	}

	if (primary.length) {
		fields.push({
			fieldname: "curated_section",
			fieldtype: "Section Break",
			label: __("Hot frames from your apps"),
			description: __(
				"Click a row's chevron to expand its nested calls. " +
				"Tick the boxes you want to line-profile."
			),
		});
		fields.push({
			fieldname: "curated_html",
			fieldtype: "HTML",
			options: build_tree_html(primary),
		});
	}

	if (framework.length) {
		fields.push({
			fieldname: "framework_section",
			fieldtype: "Section Break",
			label: no_user_app
				? __("Hot frames (frappe / erpnext / framework code)")
				: __(
					"+ " +
					framework.length +
					" framework frames (frappe / erpnext) — actionable for " +
					"customizations or framework-level fixes"
				),
			collapsible: !no_user_app,
			collapsible_depends_on: no_user_app ? "" : "0",
		});
		fields.push({
			fieldname: "framework_html",
			fieldtype: "HTML",
			options: build_tree_html(framework),
		});
	}

	fields.push({
		fieldname: "section_break_freeform",
		fieldtype: "Section Break",
		label: __("Additional dotted paths"),
	});
	fields.push({
		fieldname: "freeform",
		fieldtype: "Small Text",
		label: __("One dotted path per line"),
		description: __(
			"e.g. <code>my_app.tasks.heavy_helper</code>. Paths that " +
			"can't be imported are rejected before phase 2 starts. Use " +
			"this when the curated list above doesn't surface the " +
			"function you want, or to disambiguate a class method."
		),
	});
	fields.push({
		fieldname: "section_break_options",
		fieldtype: "Section Break",
	});
	// v0.6.0 Round 6: default reads from Optimus Settings via the
	// candidates endpoint, so admins can flip the dialog default
	// without code changes.
	var auto_expand_default = data && data.default_auto_expand !== false ? 1 : 0;
	fields.push({
		fieldname: "auto_expand",
		fieldtype: "Check",
		label: __("Auto-expand hot chain (recommended)"),
		default: auto_expand_default,
		description: __(
			"For each curated pick, walks phase-1's call tree downward " +
			"following the hottest user-code child until it hits an ORM " +
			"call or framework wrapper. The run instruments the entire " +
			"chain so you see exactly which descendant line is the time " +
			"sink — no need to re-pick and re-record level by level."
		),
	});

	var d = new frappe.ui.Dialog({
		title: __("Phase 2: Pick Functions to Line-Profile"),
		size: "large",
		fields: fields,
		primary_action_label: __("Run Line-Profile Pass"),
		primary_action: function (values) {
			var picks = [];
			// Phase K v0.7 GA: collect ticked checkboxes from the
			// custom HTML trees (curated + framework). The legacy
			// MultiCheck arrays (values.curated / values.framework_picks)
			// no longer exist - the dialog now uses an HTML field
			// per section, and selected state lives on the DOM.
			d.$wrapper.find(".fp-tree input.fp-pick:checked").each(function () {
				var path = $(this).data("pick");
				if (path) picks.push({ dotted_path: String(path), source: "curated" });
			});
			(values.freeform || "")
				.split("\n")
				.map(function (line) {
					return line.trim();
				})
				.filter(function (line) {
					return line.length > 0;
				})
				.forEach(function (path) {
					picks.push({ dotted_path: path, source: "freeform" });
				});

			if (!picks.length) {
				frappe.msgprint(__("Pick at least one function to line-profile."));
				return;
			}

			var auto_expand = values.auto_expand !== 0;
			d.hide();
			start_phase2(frm, picks, auto_expand);
		},
	});

	d.show();
}

function start_phase2(frm, picks, auto_expand) {
	frappe.call({
		method: "optimus.api.start_line_profile_pass",
		args: {
			session_uuid: frm.doc.session_uuid,
			picks: JSON.stringify(picks),
			auto_expand: auto_expand ? 1 : 0,
		},
		callback: function (r) {
			if (!r || !r.message) return;
			var run_uuid = r.message.run_uuid;
			var resolved = r.message.resolved_picks || [];
			var instrumented = resolved.filter(function (p) { return p.eligible; }).length;
			var expansions = r.message.expansions || [];

			var msg = __(
				"Phase 2 recording started — instrumenting " +
				instrumented +
				" function" + (instrumented === 1 ? "" : "s") +
				". Reproduce your flow now, then click Stop on the floating widget."
			);
			if (expansions.length) {
				// Show the first expansion inline so the dev sees what was
				// added; remaining expansions appear in the form's run row.
				var first = expansions[0];
				msg += __(
					" Auto-expanded " + first.original.split(".").pop() +
					" → " + first.chain.length + " functions" +
					(expansions.length > 1 ? " (+ " + (expansions.length - 1) + " more)" : "")
				);
			}
			frappe.show_alert({ message: msg, indicator: "blue" });
			frm.dashboard.add_indicator(
				__("Phase 2 recording — run " + run_uuid.slice(0, 8) + "..."),
				"blue"
			);
		},
		error: function (xhr) {
			// Frappe surfaces validation errors through frappe.throw — they
			// already render as a modal; we just re-enable the button.
		},
	});
}

// Listen for phase-2 realtime events on this session and refresh the form
// so the Phase 2 Runs child table picks up status transitions without the
// user having to reload manually.
function subscribe_phase2_events(frm) {
	if (frm.is_new()) return;
	if (frm._phase2_subscribed) return;
	frm._phase2_subscribed = true;

	["phase_2_run_recording", "phase_2_run_analyzing", "phase_2_run_ready", "phase_2_run_failed"].forEach(
		function (event) {
			frappe.realtime.on(event, function (payload) {
				if (!payload || payload.session_uuid !== frm.doc.session_uuid) return;
				if (event === "phase_2_run_ready") {
					frappe.show_alert({
						message: __("Phase 2 report ready"),
						indicator: "green",
					});
				} else if (event === "phase_2_run_failed") {
					frappe.show_alert({
						message: __("Phase 2 analyze failed: " + (payload.error || "unknown")),
						indicator: "red",
					});
				}
				frm.reload_doc();
			});
		}
	);
}

function render_retry_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Failed") return;

	frm.add_custom_button(__("Retry Analyze"), () => {
		frappe.confirm(
			__("Re-run the analyze pipeline for this session?"),
			() => {
				frappe.call({
					method: "optimus.api.retry_analyze",
					args: { session_uuid: frm.doc.session_uuid },
					callback: (r) => {
						const data = r.message || {};
						if (data.retried) {
							frappe.show_alert({
								message: __("Analyze retry enqueued"),
								indicator: "orange",
							});
							setTimeout(() => frm.reload_doc(), 2000);
						} else {
							frappe.show_alert({
								message: data.reason || __("Retry skipped"),
								indicator: "gray",
							});
						}
					},
				});
			},
		);
	});
}

// v0.5.3: Regenerate Reports button. Re-renders the safe + raw HTML
// from the stored session data without re-running the analyzer. Shown
// on Ready / Failed sessions. Typical use: the report template was
// upgraded (e.g. noise filters or exec summary added) and the admin
// wants existing sessions to reflect the new layout — or the original
// render crashed and a fix was deployed.
function render_regenerate_report_button(frm) {
	if (frm.is_new()) return;
	// Only makes sense once the session has content to render.
	if (!["Ready", "Failed"].includes(frm.doc.status)) return;

	frm.add_custom_button(__("Regenerate Reports"), () => {
		frappe.confirm(
			__(
				"Re-render the HTML report from stored session data. This "
				+ "does NOT re-run the analyzer. Note: if \"Suggest AI fixes "
				+ "in the report by default\" is enabled, this also asks the "
				+ "LLM for fixes for any findings that don't have one yet — "
				+ "which can take a while."
			),
			() => {
				frappe.call({
					method: "optimus.api.regenerate_reports",
					args: { session_uuid: frm.doc.session_uuid },
					freeze: true,
					freeze_message: __(
						"Regenerating the report… (this can take a while if "
						+ "AI fix suggestions are enabled)"
					),
					callback: (r) => {
						const data = (r && r.message) || {};
						if (data.regenerated) {
							const rec = data.recordings_available;
							const total = data.actions_total;
							let msg = __("Reports regenerated.");
							if (total && rec < total) {
								msg += " "
									+ __(
										"Only {0} of {1} recordings were "
										+ "available (others expired from "
										+ "Redis); per-query drill-down "
										+ "may be partial.",
										[rec, total],
									);
							}
							frappe.show_alert({
								message: msg,
								indicator: "green",
							});
							setTimeout(() => frm.reload_doc(), 1500);
						} else {
							frappe.show_alert({
								message: __("Regeneration skipped"),
								indicator: "gray",
							});
						}
					},
				});
			},
		);
	});
}

function render_status_indicator(frm) {
	if (frm.is_new()) return;
	const status = frm.doc.status || "Recording";
	const colors = {
		Recording: "green",
		Stopping: "orange",
		"Capturing Background Jobs": "orange",
		Analyzing: "orange",
		Ready: "blue",
		Failed: "red",
	};
	frm.page.set_indicator(status, colors[status] || "gray");
}

// While the session drains the flow's background jobs after Stop, poll the
// pending count and show a live "Capturing background jobs… N still running"
// headline so it doesn't look stuck at "Stopping". Reloads the form once the
// status moves on (analyze.run takes over → Analyzing).
function render_drain_progress(frm) {
	if (frm._optimus_drain_timer) {
		clearInterval(frm._optimus_drain_timer);
		frm._optimus_drain_timer = null;
	}
	if (frm.is_new()) return;
	if (frm.doc.status !== "Capturing Background Jobs") return;

	const stop = () => {
		clearInterval(frm._optimus_drain_timer);
		frm._optimus_drain_timer = null;
	};
	const tick = () => {
		// Stop polling if the user navigated away from this form (no clean
		// per-form unload hook in Frappe — cur_frm is the active form).
		if (window.cur_frm !== frm) {
			stop();
			return;
		}
		frappe.call({
			method: "optimus.api.drain_progress",
			args: { session_uuid: frm.doc.session_uuid },
			callback: (r) => {
				const d = (r && r.message) || {};
				if (d.status && d.status !== "Capturing Background Jobs") {
					stop();
					frm.set_intro(null);
					frm.reload_doc();
					return;
				}
				const n = d.pending != null ? d.pending : 0;
				// set_intro REPLACES the banner in place each tick. (set_headline
				// APPENDS a dismissible message, so polling stacked a new bar
				// every 4s.)
				frm.set_intro(
					__("⏳ Capturing background jobs… {0} still running.", [n]),
					"orange"
				);
			},
			error: () => {},
		});
	};
	tick();
	frm._optimus_drain_timer = setInterval(tick, 4000);
}

function render_download_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	// v0.6.0 Round 7: safe-mode reporting removed. Single admin-scoped
	// report — the raw HTML plus a lazy-generated PDF. Server-side
	// permission gating still applies (Optimus User role + per-File
	// permission hook).
	if (frm.doc.raw_report_file) {
		frm.add_custom_button(
			__("Download Report"),
			() => {
				frappe.confirm(
					__(
						"The report will be saved to your downloads folder and contains literal SQL values, request headers, and stack traces. Do not share it externally without redacting it yourself. Continue?",
					),
					() => {
						// Programmatic <a download="..."> click forces
						// the browser to save the file rather than navigate
						// to it. window.open serves the HTML inline because
						// the file's Content-Type is text/html — that's the
						// "Open Report" flow below; this button needs a
						// real save-to-disk.
						const link = document.createElement("a");
						link.href = frm.doc.raw_report_file;
						link.download = "";
						document.body.appendChild(link);
						link.click();
						document.body.removeChild(link);
					},
				);
			},
			__("Reports"),
		);

		frm.add_custom_button(
			__("Open Report"),
			() => {
				frappe.confirm(
					__(
						"The report opens in a new tab and contains literal SQL values, request headers, and stack traces. Do not share it externally without redacting it yourself. Continue?",
					),
					() => {
						// Frappe serves /private/files/*.html with
						// Content-Disposition: attachment, which triggers a
						// download dialog instead of rendering inline. Fetch
						// the content, wrap it in a blob URL with the right
						// MIME type, and window.open that — blob URLs are
						// not governed by the original response's
						// Content-Disposition, so the browser renders the
						// HTML inline. Works because the report HTML is
						// self-contained (no external asset references); see
						// product-thesis "safe report" guarantee.
						const showError = (msg) =>
							frappe.show_alert({
								message: __(msg),
								indicator: "red",
							});
						fetch(frm.doc.raw_report_file, {
							credentials: "same-origin",
						})
							.then((r) => {
								if (!r.ok) {
									throw new Error("HTTP " + r.status);
								}
								return r.text();
							})
							.then((html) => {
								const blob = new Blob([html], {
									type: "text/html",
								});
								const url = URL.createObjectURL(blob);
								const win = window.open(url, "_blank");
								if (!win) {
									showError(
										"Pop-up blocked; allow pop-ups for this site to open the report inline.",
									);
									URL.revokeObjectURL(url);
									return;
								}
								// Revoke the blob URL once the new tab has had
								// a chance to load it. The tab keeps a DOM
								// reference to the rendered content
								// independent of the URL.
								setTimeout(
									() => URL.revokeObjectURL(url),
									60000,
								);
							})
							.catch(() => {
								showError(
									"Could not load the report; try Download Report instead.",
								);
							});
					},
				);
			},
			__("Reports"),
		);
	}
}

function render_findings_summary(frm) {
	if (frm.is_new()) return;
	if (!frm.doc.findings || frm.doc.findings.length === 0) return;

	// Add a small color-coded badge dashboard above the form fields.
	const high = frm.doc.findings.filter((f) => f.severity === "High").length;
	const medium = frm.doc.findings.filter((f) => f.severity === "Medium").length;
	const low = frm.doc.findings.filter((f) => f.severity === "Low").length;

	const badges = [];
	if (high) badges.push(`<span class="indicator-pill red">${high} High</span>`);
	if (medium) badges.push(`<span class="indicator-pill orange">${medium} Medium</span>`);
	if (low) badges.push(`<span class="indicator-pill blue">${low} Low</span>`);

	if (badges.length === 0) return;

	frm.dashboard.add_section(
		`<div style="padding: 8px 0; font-size: 0.9rem;">
			<strong>${__("Findings")}:</strong> ${badges.join(" ")}
		</div>`,
		__("Performance issues"),
	);
}

