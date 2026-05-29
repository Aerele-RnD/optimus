// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt

// Populate the `Tracked Apps` child table's `app_name` Autocomplete
// with the bench's actual installed apps — same UX as picking a
// Module Def, but for the list of apps rather than modules.
//
// Why on refresh: the Autocomplete options live on the grid docfield,
// which Frappe re-creates whenever the form rebinds. Setting options
// on refresh survives reloads and "refresh" Ctrl-S cycles.

// The full set of fields the Sensitivity Profile governs. Kept in sync with
// optimus.settings._SENSITIVITY_KEYS — the preset numbers themselves are NOT
// duplicated here; they're fetched at runtime from
// optimus.api.get_config_profiles (single source of truth in settings.py).
// Fields missing from this list are silently NOT updated by the JS preset-
// write, even though the backend would still apply the preset at config-
// resolve time — they'd appear locked under a non-Custom profile but show
// stale values. So this MUST match _SENSITIVITY_KEYS exactly.
const OPTIMUS_SENSITIVITY_FIELDS = [
	// General → Session retention
	"session_retention_days",
	// Capture capacity
	"max_queries_per_recording",
	"pyinstrument_sampler_interval_ms",
	"background_job_wait_seconds",
	// Display filters
	"min_action_duration_ms",
	"large_duration_threshold_ms",
	// Analyzer thresholds (detection — the original nine)
	"redundant_doc_threshold",
	"redundant_cache_threshold",
	"redundant_perm_threshold",
	"n_plus_one_min_occurrences",
	"slow_query_threshold_ms",
	"slow_hot_path_pct_threshold",
	"slow_hot_path_min_ms",
	"hot_line_high_pct",
	"hot_line_high_min_ms",
	// Phase-2 UI knobs
	"phase2_max_runs_per_session",
	"auto_expand_max_depth",
	"auto_expand_min_ms",
	// AI auto-suggest cap
	"ai_auto_suggest_max",
];

frappe.ui.form.on("Optimus Settings", {
	refresh(frm) {
		frm.set_intro(
			__(
				"Optimus app-wide settings. Changes apply to new sessions; in-flight recordings keep the values they started with."
			),
			"blue"
		);

		frappe.call({
			method: "optimus.api.get_installed_apps_for_tracking",
			callback(r) {
				if (!r || !r.message) {
					return;
				}
				const apps = r.message;
				const grid = frm.fields_dict.tracked_apps.grid;
				if (!grid) {
					return;
				}
				// Autocomplete fieldtype accepts options as a
				// newline-separated string (Frappe's renderer splits
				// on \n). Passing an array also works in v14+, but
				// the string form is the safe cross-version shape.
				grid.update_docfield_property(
					"app_name",
					"options",
					apps.join("\n")
				);
			},
		});

		// "Test AI connection" — only when the feature is on. Saves the
		// operator a profiling round-trip just to find out the key/model
		// are wrong.
		// Also re-evaluate the "Test AI connection" button visibility
		// when the operator toggles ai_enabled (see the ai_enabled
		// handler below — it re-runs refresh() so this conditional
		// fires again).
		if (frm.doc.ai_enabled) {
			frm.add_custom_button(__("Test AI connection"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(
						__("Save your AI settings first, then test the connection.")
					);
					return;
				}
				frappe.show_alert({
					message: __("Pinging the AI provider…"),
					indicator: "blue",
				});
				frappe.call({
					method: "optimus.api.test_ai_connection",
					callback(r) {
						const m = (r && r.message) || {};
						frappe.msgprint({
							title: m.ok
								? __("AI connection OK")
								: __("AI connection failed"),
							indicator: m.ok ? "green" : "red",
							message:
								(m.model ? __("Model: {0}", [m.model]) + "<br>" : "") +
								frappe.utils.escape_html(m.message || ""),
						});
					},
					error() {
						frappe.show_alert({
							message: __("AI connection test failed"),
							indicator: "red",
						});
					},
				});
			});
		}

	},

	config_profile(frm) {
		// Sensitivity Profile changed. Under a named preset (Strict /
		// Recommended / Relaxed) the threshold fields are read-only and the
		// preset drives analysis at read time — but we ALSO fill the fields
		// with the preset numbers so the operator can see what they're getting
		// (and so they become the starting point if they later pick Custom).
		// On Custom we just unlock the fields and leave their current values.
		const profile = frm.doc.config_profile;
		const refresh_fields = () =>
			OPTIMUS_SENSITIVITY_FIELDS.forEach((f) => frm.refresh_field(f));

		if (!profile || profile === "Custom") {
			refresh_fields();
			return;
		}

		frappe.call({
			method: "optimus.api.get_config_profiles",
			callback(r) {
				const preset = (r && r.message && r.message[profile]) || null;
				if (preset) {
					OPTIMUS_SENSITIVITY_FIELDS.forEach((f) => {
						if (preset[f] !== undefined) {
							frm.set_value(f, preset[f]);
						}
					});
				}
				// Re-evaluate read_only_depends_on regardless, so the fields
				// lock immediately without a save + reload.
				refresh_fields();
			},
		});
	},

	ai_enabled(frm) {
		// Force the form to re-evaluate `depends_on` directives so the
		// AI subfields (provider / base URL / model / API key / the
		// per-section toggles / the Automatic Suggestions block) show or
		// hide immediately when the master checkbox flips, without
		// requiring a save + reload. Also re-fires refresh() so the
		// "Test AI connection" custom button appears / disappears in
		// step with the toggle.
		frm.refresh_field("ai_provider");
		frm.refresh_field("ai_base_url");
		frm.refresh_field("ai_model");
		frm.refresh_field("ai_api_key");
		frm.refresh_field("ai_sections_break");
		frm.refresh_field("ai_suggest_findings");
		frm.refresh_field("ai_suggest_indexes");
		frm.refresh_field("ai_humanize_steps");
		frm.refresh_field("ai_auto_section");
		frm.refresh_field("ai_auto_suggest");
		frm.refresh_field("ai_auto_suggest_max");
		// Clear and re-attach the custom button.
		frm.clear_custom_buttons();
		frm.trigger("refresh");
	},
});
