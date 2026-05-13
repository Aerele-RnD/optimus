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
		render_download_buttons(frm);
		render_retry_button(frm);
		render_regenerate_report_button(frm);
		render_findings_summary(frm);
		render_analyzer_warnings(frm);
		render_phase2_button(frm);
		render_ai_buttons(frm);
		subscribe_phase2_events(frm);
		subscribe_session_progress(frm);
	},
});

// The AI buttons are gated on the per-section LLM toggles in Profiler
// Settings (api.ai_capabilities). Off section → its button(s) don't render
// (the server enforces the same — this just keeps the form honest). The
// capabilities read is async, so the button renders happen in the callback.
function render_ai_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	frappe.call({
		method: "optimus.api.ai_capabilities",
		callback: (r) => {
			const c = (r && r.message) || {};
			if (!c.enabled) return; // master switch off → no AI buttons at all
			if (c.findings) {
				render_ai_fix_button(frm);
				render_ai_backfill_button(frm);
				render_ai_reevaluate_button(frm);
			}
			if (c.humanize) render_humanize_steps_button(frm);
			if (c.indexes) render_suggest_index_button(frm);
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
}

// v0.6.0: AI "suggest a fix" — on-demand only.
//
// Adds a "Suggest a fix (AI)" custom button when the session is Ready and
// has at least one eligible finding. Clicking opens a dialog: pick a
// finding, hit Generate, the server (api.suggest_fix) calls the configured
// LLM and stores the suggestion on the Optimus Finding row (so it's cached
// and shows up in the regenerated HTML report). Re-opening shows the cached
// suggestion with a "Regenerate" option.
//
// Mirrors AI_ELIGIBLE_FINDING_TYPES in optimus/ai_fix.py.
const AI_ELIGIBLE_FINDING_TYPES = new Set([
	"N+1 Query",
	"Framework N+1",
	"Slow Query",
	"Missing Index",
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
	"Redundant Call",
	"Slow Hot Path",
	"Hook Bottleneck",
	"Repeated Hot Frame",
	"Hot Line",
]);

function render_ai_fix_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	const eligible = (frm.doc.findings || []).filter((f) =>
		AI_ELIGIBLE_FINDING_TYPES.has(f.finding_type)
	);
	if (!eligible.length) return;

	frm.add_custom_button(
		__("Suggest a fix (AI)"),
		() => open_ai_fix_dialog(frm, eligible),
		__("AI")
	);
}

// Shared call: api.backfill_ai_fixes with the right freeze message + toast.
// `mode` is "generate" (fill missing only) or "reevaluate" (overwrite all).
function _ai_backfill_call(frm, mode) {
	const regenerate_all = mode === "reevaluate";
	frappe.call({
		method: "optimus.api.backfill_ai_fixes",
		args: { session_uuid: frm.doc.session_uuid, regenerate_all: regenerate_all ? 1 : 0 },
		freeze: true,
		freeze_message: regenerate_all
			? __("Re-evaluating AI fixes & re-rendering the report…")
			: __("Generating AI fixes & re-rendering the report…"),
		callback: (r) => {
			const m = (r && r.message) || {};
			let msg = regenerate_all
				? __("Re-evaluated {0} AI fix(es).", [m.added || 0])
				: __("Added {0} AI fix(es).", [m.added || 0]);
			if (m.failed) {
				msg += " " + __("{0} failed — old suggestion kept (see Error Log).", [m.failed]);
			}
			if (m.skipped_time) {
				msg +=
					" " +
					__("{0} skipped (hit the time budget) — run it again for the rest.", [
						m.skipped_time,
					]);
			}
			frappe.show_alert({ message: msg, indicator: m.added ? "green" : "orange" });
			setTimeout(() => frm.reload_doc(), 1200);
		},
		error: () => {
			frappe.show_alert({
				message: __("The AI fix request failed — see the error popup for details."),
				indicator: "red",
			});
		},
	});
}

// "Generate AI fixes" — fill in the suggestions for ALL eligible findings
// that don't have one yet (then re-render the report). The retry path for
// when the LLM was unavailable during analyze (the analyze still completes —
// the AI step is just skipped); also works when "Suggest AI fixes by
// default" is off. Shown only when there's something to do.
function render_ai_backfill_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	const pending = (frm.doc.findings || []).filter(
		(f) =>
			AI_ELIGIBLE_FINDING_TYPES.has(f.finding_type) &&
			!((f.llm_fix_json || "").toString().trim())
	);
	if (!pending.length) return;

	frm.add_custom_button(
		__("Generate AI fixes ({0})", [pending.length]),
		() => {
			frappe.confirm(
				__(
					"Generate AI fix suggestions for the {0} eligible finding(s) " +
						"that don't have one yet, then re-render the report? This " +
						"calls the configured LLM for each — it can take a bit, and " +
						"if it doesn't finish in one pass just run it again.",
					[pending.length]
				),
				() => _ai_backfill_call(frm, "generate")
			);
		},
		__("AI")
	);
}

// "Re-evaluate AI fixes" — re-generate the suggestion for EVERY eligible
// finding, overwriting the existing ones, then re-render. Use after changing
// the AI model or prompt. A failure mid-run leaves the old suggestion in
// place. Shown whenever there's an eligible finding (so it's distinct from
// "Generate AI fixes", it's only offered when at least one already HAS a
// suggestion to re-evaluate).
function render_ai_reevaluate_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	const eligible = (frm.doc.findings || []).filter((f) =>
		AI_ELIGIBLE_FINDING_TYPES.has(f.finding_type)
	);
	const haveSome = eligible.some((f) => (f.llm_fix_json || "").toString().trim());
	if (!eligible.length || !haveSome) return;

	frm.add_custom_button(
		__("Re-evaluate AI fixes ({0})", [eligible.length]),
		() => {
			frappe.confirm(
				__(
					"Re-generate AI fix suggestions for ALL {0} eligible finding(s), " +
						"overwriting the ones that already have a suggestion, then " +
						"re-render the report? Useful after changing the AI model or " +
						"prompt. Calls the configured LLM for each — it can take a bit; " +
						"if it doesn't finish in one pass, run it again. (A failure on " +
						"any finding leaves its old suggestion in place.)",
					[eligible.length]
				),
				() => _ai_backfill_call(frm, "reevaluate")
			);
		},
		__("AI")
	);
}

// v0.6.0: "Humanize Steps (AI)" — rewrite the auto-generated "Steps to
// Reproduce" note into a friendly, human-readable flow via the LLM. Shown on
// Ready sessions; the server (api.humanize_steps) validates that AI is
// configured. If the note already has hand-written content, confirm first
// (the action overwrites it).
function render_humanize_steps_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	const hasNotes = (frm.doc.notes || "").toString().trim().length > 0;
	frm.add_custom_button(
		__("Humanize Steps (AI)"),
		() => {
			const run = () =>
				frappe.call({
					method: "optimus.api.humanize_steps",
					args: { session_uuid: frm.doc.session_uuid },
					freeze: true,
					freeze_message: __("Asking the AI to rewrite the steps to reproduce…"),
					callback: (r) => {
						if (!r || !r.message || !r.message.ok) return;
						frappe.show_alert({
							message: __("Steps to Reproduce updated"),
							indicator: "green",
						});
						frm.reload_doc();
					},
				});
			if (hasNotes) {
				frappe.confirm(
					__(
						"Replace the current “Steps to Reproduce” with an AI-drafted " +
							"version? Any text you've written there will be overwritten."
					),
					run
				);
			} else {
				run();
			}
		},
		__("AI")
	);
}

// v0.6.0: "Suggest an index (AI)" — pick one of the tables in the breakdown
// that has a heuristic index candidate, and let the LLM vet it (which
// composite, whether existing indexes already cover it, the write-cost call).
// Shown when the session is Ready and the breakdown has at least one such
// table; the server (api.suggest_index) validates that AI is configured.
function _tables_with_index_candidate(frm) {
	let breakdown = [];
	try {
		breakdown = JSON.parse(frm.doc.table_breakdown_json || "[]");
	} catch (e) {
		breakdown = [];
	}
	return (breakdown || []).filter(
		(t) => t && t.recommended_index && t.recommended_index.columns && t.recommended_index.columns.length
	);
}

function render_suggest_index_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	const tables = _tables_with_index_candidate(frm);
	if (!tables.length) return;

	frm.add_custom_button(
		__("Suggest an index (AI)"),
		() => open_suggest_index_dialog(frm, tables),
		__("AI")
	);
}

function open_suggest_index_dialog(frm, tables) {
	const options = tables.map((t) => {
		const rec = t.recommended_index;
		const cols = (rec.columns || []).join(", ");
		const had = t.ai_index ? " · already has AI advice" : "";
		return { label: `${t.table} — (${cols})${had}`, value: t.table };
	});
	const d = new frappe.ui.Dialog({
		title: __("Suggest an index (AI)"),
		fields: [
			{
				fieldname: "table_name",
				fieldtype: "Select",
				label: __("Table"),
				options: options.map((o) => o.value).join("\n"),
				default: options[0] && options[0].value,
				reqd: 1,
			},
			{
				fieldname: "hint",
				fieldtype: "HTML",
				options:
					'<p class="text-muted small">The LLM gets this table\'s candidate columns, a few of the actual queries, and the table\'s current indexes (<code>SHOW INDEX</code>), and recommends the smallest index that actually helps — or tells you an existing index already covers it. Result is written into the report\'s "Time spent per database table" section.</p>',
			},
		],
		primary_action_label: __("Generate"),
		primary_action: (values) => {
			d.hide();
			frappe.call({
				method: "optimus.api.suggest_index",
				args: { session_uuid: frm.doc.session_uuid, table_name: values.table_name },
				freeze: true,
				freeze_message: __("Asking the AI about indexes for {0}…", [values.table_name]),
				callback: (r) => {
					if (!r || !r.message || !r.message.ok) return;
					frappe.show_alert({
						message: __("Index advice added for {0}", [r.message.table || values.table_name]),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}

function open_ai_fix_dialog(frm, eligible) {
	const options = eligible.map((f) => ({
		label: `[${f.severity || "?"}] ${f.finding_type} — ${f.title || ""}`,
		value: f.name,
	}));

	const d = new frappe.ui.Dialog({
		title: __("Suggest a fix (AI)"),
		size: "large",
		fields: [
			{
				fieldname: "finding",
				fieldtype: "Select",
				label: __("Finding"),
				reqd: 1,
				options: options,
			},
			{
				fieldname: "privacy_note",
				fieldtype: "HTML",
				options:
					'<p class="text-muted small">This sends the selected finding\'s code snippet, callsite, and SQL to the AI provider configured in <b>Optimus Settings ▸ AI Fix Suggestions</b>. Use a local model (Ollama / LM Studio) there to keep everything on-box.</p>',
			},
			{
				fieldname: "result",
				fieldtype: "HTML",
				options:
					'<div class="ai-fix-result text-muted small">Pick a finding and click <b>Generate</b>.</div>',
			},
		],
		primary_action_label: __("Generate"),
		primary_action() {
			run_ai_suggest(frm, d, d.get_value("finding"), false);
		},
	});

	// When the user picks a finding that already has a cached suggestion,
	// show it immediately and flip the primary action to "Regenerate".
	d.fields_dict.finding.$input &&
		d.fields_dict.finding.$input.on("change", () => {
			const name = d.get_value("finding");
			const f = (frm.doc.findings || []).find((x) => x.name === name);
			let cached = null;
			if (f && f.llm_fix_json) {
				try {
					cached = JSON.parse(f.llm_fix_json);
				} catch (e) {
					cached = null;
				}
			}
			if (cached && cached.suggestion) {
				render_ai_result(d, cached, true);
				d.set_primary_action(__("Regenerate"), () =>
					run_ai_suggest(frm, d, name, true)
				);
			} else {
				d.fields_dict.result.$wrapper.html(
					'<div class="ai-fix-result text-muted small">No suggestion yet. Click <b>Generate</b>.</div>'
				);
				d.set_primary_action(__("Generate"), () =>
					run_ai_suggest(frm, d, name, false)
				);
			}
		});

	d.onhide = () => frm.reload_doc();
	d.show();
}

function run_ai_suggest(frm, d, finding_ref, regenerate) {
	if (!finding_ref) {
		frappe.msgprint(__("Pick a finding first."));
		return;
	}
	d.disable_primary_action();
	d.fields_dict.result.$wrapper.html(
		'<div class="ai-fix-result text-muted small"><i class="fa fa-spinner fa-spin"></i> ' +
			__("Asking the AI provider…") +
			"</div>"
	);
	frappe.call({
		method: "optimus.api.suggest_fix",
		args: {
			session_uuid: frm.doc.session_uuid,
			finding_ref: finding_ref,
			regenerate: regenerate ? 1 : 0,
		},
		callback(r) {
			d.enable_primary_action();
			const m = (r && r.message) || {};
			if (!m.ok || !m.suggestion) {
				d.fields_dict.result.$wrapper.html(
					'<div class="ai-fix-result text-danger small">' +
						__("No suggestion was returned.") +
						"</div>"
				);
				return;
			}
			render_ai_result(d, m, !!m.cached);
			d.set_primary_action(__("Regenerate"), () =>
				run_ai_suggest(frm, d, finding_ref, true)
			);
			if (!m.cached) {
				frappe.show_alert({
					message: __(
						"Saved on the finding. Run 'Regenerate Reports' to include it in the downloadable report."
					),
					indicator: "green",
				});
			}
		},
		error() {
			d.enable_primary_action();
			d.fields_dict.result.$wrapper.html(
				'<div class="ai-fix-result text-danger small">' +
					__("The AI request failed — see the error popup for details.") +
					"</div>"
			);
		},
	});
}

// Scoped CSS for the before/after diff highlighting in the dialog (the HTML
// report has its own copy in report.html). Injected with the result so it's
// torn down when the wrapper is re-rendered.
const _DIFF_STYLE =
	"<style>" +
	".dh-ai-result pre.dh{padding:6px 0;}" +
	".dh-ai-result pre.dh .dh-line{display:block;padding:0 10px;white-space:pre-wrap;word-break:break-word;}" +
	".dh-ai-result pre.dh .dh-add{background:#e6ffec;color:#116329;}" +
	".dh-ai-result pre.dh .dh-del{background:#ffebe9;color:#a40e26;}" +
	".dh-ai-result pre.dh .dh-meta{background:#f2effd;color:#6639ba;}" +
	"</style>";

function _diff_looks_like(code_attrs, lines) {
	if ((code_attrs || "").indexOf("diff") !== -1) return true;
	if (lines.some((l) => l.indexOf("@@") === 0)) return true;
	return lines.some((l) => l.indexOf("+") === 0) && lines.some((l) => l.indexOf("-") === 0);
}

function _diff_line_class(line) {
	if (line.indexOf("@@") === 0 || line.indexOf("+++") === 0 || line.indexOf("---") === 0) return "dh-meta";
	if (line.indexOf("+") === 0) return "dh-add";
	if (line.indexOf("-") === 0) return "dh-del";
	return null;
}

// Wrap +/-/@@ lines inside diff-looking <pre> blocks (frappe.markdown escapes
// HTML inside code fences, so the inner text is already safe to re-emit).
function highlight_diff_blocks(html) {
	return (html || "").replace(
		/<pre[^>]*>(?:\s*<code([^>]*)>)?([\s\S]*?)(?:<\/code>\s*)?<\/pre>/g,
		(whole, code_attrs, inner) => {
			code_attrs = code_attrs || "";
			let lines = (inner || "").split("\n");
			if (lines.length && lines[lines.length - 1] === "") lines = lines.slice(0, -1);
			if (!lines.length || !_diff_looks_like(code_attrs, lines)) return whole;
			const out = lines.map((ln) => {
				const cls = _diff_line_class(ln);
				return '<span class="dh-line ' + (cls || "dh-ctx") + '">' + (ln || "&#8203;") + "</span>";
			});
			const code_open = code_attrs ? "<code" + code_attrs + ">" : "<code>";
			return '<pre class="dh">' + code_open + out.join("") + "</code></pre>";
		}
	);
}

function render_ai_result(d, payload, cached) {
	const body = highlight_diff_blocks(frappe.markdown(payload.suggestion || ""));
	const meta = [
		payload.model ? __("Model: {0}", [frappe.utils.escape_html(payload.model)]) : "",
		payload.generated_at ? frappe.utils.escape_html(payload.generated_at) : "",
		cached ? __("(cached)") : "",
	]
		.filter(Boolean)
		.join(" · ");
	// `source_available === false` means the LLM only had the finding's title
	// + numbers (no source window, no SQL) — the suggestion is necessarily
	// directional, so flag it. Older cached rows lack the key → treat as true.
	const caution =
		payload.source_available === false
			? '<div class="small" style="color:#92400e;background:#fffbeb;border:1px solid #fde68a;border-radius:4px;padding:5px 8px;margin-bottom:8px;">' +
				__(
					"The profiler couldn't show the AI this finding's source code (or SQL) — treat this as directional guidance, not a verified code fix."
				) +
				"</div>"
			: "";
	d.fields_dict.result.$wrapper.html(
		_DIFF_STYLE +
			'<div class="ai-fix-result dh-ai-result" style="border-left:3px solid #7c3aed;background:#f5f3ff;padding:10px 12px;border-radius:0 4px 4px 0;">' +
			'<div class="text-muted small" style="margin-bottom:6px;">' +
			meta +
			"</div>" +
			caution +
			'<div class="ai-fix-body">' +
			body +
			"</div>" +
			'<div class="text-muted small" style="margin-top:6px;border-top:1px dashed #ddd6fe;padding-top:5px;">' +
			__("Machine-generated — review before applying.") +
			"</div>" +
			"</div>"
	);
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

	function to_option(c) {
		return {
			label:
				c.dotted_path +
				" (" +
				(c.cumulative_ms || 0).toFixed(1) +
				"ms · " +
				(c.hit_count || 0) +
				"× hits · " +
				c.app +
				")",
			value: c.dotted_path,
		};
	}

	var primary_options = primary.map(to_option);
	var framework_options = framework.map(to_option);

	// When there are no user-app frames at all (vanilla ERPNext or a
	// site without custom apps), the framework list IS the primary
	// list — the customer is profiling erpnext / frappe code. Promote
	// it to default-expanded so the dialog shows usable candidates
	// instead of an empty primary section.
	var no_user_app = primary_options.length === 0 && framework_options.length > 0;

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

	if (primary_options.length) {
		fields.push({
			fieldname: "curated",
			fieldtype: "MultiCheck",
			label: __("Hot frames from your apps"),
			options: primary_options,
			columns: 1,
		});
	}

	if (framework_options.length) {
		fields.push({
			fieldname: "section_break_framework",
			fieldtype: "Section Break",
			label: no_user_app
				? __("Hot frames (frappe / erpnext / framework code)")
				: __(
					"+ " +
					framework_options.length +
					" framework frames (frappe / erpnext) — actionable for " +
					"customizations or framework-level fixes"
				),
			collapsible: !no_user_app,
			collapsible_depends_on: no_user_app ? "" : "0",
		});
		fields.push({
			fieldname: "framework_picks",
			fieldtype: "MultiCheck",
			label: "",
			options: framework_options,
			columns: 1,
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
			(values.curated || []).forEach(function (path) {
				picks.push({ dotted_path: path, source: "curated" });
			});
			(values.framework_picks || []).forEach(function (path) {
				picks.push({ dotted_path: path, source: "curated" });
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

function render_analyzer_warnings(frm) {
	if (frm.is_new()) return;
	if (!frm.doc.analyzer_warnings) return;
	frm.set_intro(frm.doc.analyzer_warnings, "orange");
}

function render_status_indicator(frm) {
	if (frm.is_new()) return;
	const status = frm.doc.status || "Recording";
	const colors = {
		Recording: "green",
		Stopping: "orange",
		Analyzing: "orange",
		Ready: "blue",
		Failed: "red",
	};
	frm.page.set_indicator(status, colors[status] || "gray");
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
						"The report contains literal SQL values, request headers, and stack traces. Do not share it externally without redacting it yourself. Continue?",
					),
					() => {
						window.open(frm.doc.raw_report_file, "_blank");
					},
				);
			},
			__("Reports"),
		);

		frm.add_custom_button(
			__("Download Report (PDF)"),
			() => {
				frappe.show_alert({
					message: __("Generating PDF..."),
					indicator: "blue",
				});
				frappe.call({
					method: "optimus.api.download_pdf",
					args: { session_uuid: frm.doc.session_uuid },
					callback: (r) => {
						const data = (r && r.message) || {};
						if (data.file_url) {
							window.open(data.file_url, "_blank");
						} else {
							frappe.show_alert({
								message: __("PDF generation failed; download the HTML version instead"),
								indicator: "red",
							});
						}
					},
					error: () => {
						frappe.show_alert({
							message: __("PDF generation failed"),
							indicator: "red",
						});
					},
				});
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

