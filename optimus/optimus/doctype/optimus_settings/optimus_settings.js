// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt

// Populate the `Tracked Apps` child table's `app_name` Autocomplete
// with the bench's actual installed apps — same UX as picking a
// Module Def, but for the list of apps rather than modules.
//
// Why on refresh: the Autocomplete options live on the grid docfield,
// which Frappe re-creates whenever the form rebinds. Setting options
// on refresh survives reloads and "refresh" Ctrl-S cycles.

frappe.ui.form.on("Optimus Settings", {
	refresh(frm) {
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
});
