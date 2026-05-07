// Copyright (c) 2026, Frappe Profiler contributors
// For license information, please see license.txt
//
// Profiler Session form script (Phase 5).
//
// Customizes the detail view to feel like a "report" rather than a raw
// data form. The customer-facing summary HTML is rendered prominently at
// the top, the analyzer findings are listed in a friendly format, and the
// two report files get prominent download buttons (raw is gated to admins).

frappe.ui.form.on("Profiler Session", {
	refresh(frm) {
		render_status_indicator(frm);
		render_download_buttons(frm);
		render_retry_button(frm);
		render_regenerate_report_button(frm);
		render_findings_summary(frm);
		render_analyzer_warnings(frm);
		render_baseline_buttons(frm);
		render_no_baseline_banner(frm);
		render_phase2_button(frm);
		subscribe_phase2_events(frm);
	},
});

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
					method: "frappe_profiler.api.force_stop_phase2",
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
		method: "frappe_profiler.api.get_phase2_candidates",
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

			d.hide();
			start_phase2(frm, picks);
		},
	});

	d.show();
}

function start_phase2(frm, picks) {
	frappe.call({
		method: "frappe_profiler.api.start_line_profile_pass",
		args: {
			session_uuid: frm.doc.session_uuid,
			picks: JSON.stringify(picks),
		},
		callback: function (r) {
			if (!r || !r.message) return;
			var run_uuid = r.message.run_uuid;
			frappe.show_alert({
				message: __(
					"Phase 2 recording started. Reproduce your flow now, then " +
					"click Stop on the floating widget."
				),
				indicator: "blue",
			});
			// The floating widget picks up the active flag on its next poll
			// (or via the phase_2_run_recording realtime event we emit
			// from start_line_profile_pass).
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

// v0.4.0: Pin / Unpin / Compare baseline buttons
function render_baseline_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	if (frm.doc.is_baseline) {
		frm.add_custom_button(__("Unpin baseline"), function () {
			frappe.call({
				method: "frappe_profiler.api.unpin_baseline",
				args: { session_uuid: frm.doc.session_uuid },
				callback: function () {
					frappe.show_alert({
						message: __("Baseline unpinned"),
						indicator: "blue",
					});
					frm.reload_doc();
				},
			});
		});
	} else {
		frm.add_custom_button(__("Pin as baseline"), function () {
			frappe.call({
				method: "frappe_profiler.api.pin_baseline",
				args: { session_uuid: frm.doc.session_uuid },
				callback: function () {
					frappe.show_alert({
						message: __("Pinned as baseline"),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		});
	}

	frm.add_custom_button(__("Compare with..."), function () {
		var d = new frappe.ui.Dialog({
			title: __("Compare with another Profiler Session"),
			fields: [
				{
					fieldname: "target",
					fieldtype: "Link",
					label: "Profiler Session",
					options: "Profiler Session",
					reqd: 1,
					get_query: function () {
						return { filters: { status: "Ready" } };
					},
				},
			],
			primary_action_label: __("Compare"),
			primary_action: function (values) {
				d.hide();
				frappe.call({
					method: "frappe_profiler.api.set_comparison",
					args: {
						session_uuid: frm.doc.session_uuid,
						compared_to: values.target,
					},
					callback: function () {
						frappe.show_alert({
							message: __("Comparison set; reloading report"),
							indicator: "green",
						});
						frm.reload_doc();
					},
				});
			},
		});
		d.show();
	});
}

function render_no_baseline_banner(frm) {
	// Only show if Ready, no baseline set, and not itself a baseline
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	if (frm.doc.compared_to_session) return;
	if (frm.doc.is_baseline) return;
	// Don't override an existing analyzer_warnings intro
	if (frm.doc.analyzer_warnings) return;
	frm.set_intro(
		__("No baseline set. Pin this session to compare future runs, or click 'Compare with...' to pick one now."),
		"blue",
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
					method: "frappe_profiler.api.retry_analyze",
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
				"Re-render the safe and raw HTML reports from stored "
				+ "session data? This does NOT re-run the analyzer — it "
				+ "only re-invokes the renderer, applying the current "
				+ "report template to this session. Takes a few seconds."
			),
			() => {
				frappe.call({
					method: "frappe_profiler.api.regenerate_reports",
					args: { session_uuid: frm.doc.session_uuid },
					freeze: true,
					freeze_message: __("Regenerating reports..."),
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

	if (frm.doc.safe_report_file) {
		frm.add_custom_button(
			__("Download Safe Report"),
			() => {
				window.open(frm.doc.safe_report_file, "_blank");
			},
			__("Reports"),
		);

		// v0.4.0: PDF download via lazy api.download_pdf
		frm.add_custom_button(
			__("Download Safe Report (PDF)"),
			() => {
				frappe.show_alert({
					message: __("Generating PDF..."),
					indicator: "blue",
				});
				frappe.call({
					method: "frappe_profiler.api.download_pdf",
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

	// Raw report is gated: only System Manager and the recording user can
	// see the button. The actual file is also gated server-side via
	// Frappe's private file permission system.
	if (frm.doc.raw_report_file && user_can_see_raw(frm.doc)) {
		frm.add_custom_button(
			__("Download Raw Report (admin)"),
			() => {
				frappe.confirm(
					__(
						"The raw report contains literal SQL values, request headers, and stack traces. Do not share it externally. Continue?",
					),
					() => {
						window.open(frm.doc.raw_report_file, "_blank");
					},
				);
			},
			__("Reports"),
		);
	}
}

function user_can_see_raw(doc) {
	const roles = frappe.user_roles || [];
	if (roles.includes("System Manager")) return true;
	if (doc.user === frappe.session.user) return true;
	return false;
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

