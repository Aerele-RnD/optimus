// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt
//
// List view customization for Optimus Session.
// Adds a color-coded top-severity indicator so customers can
// see at a glance which sessions had the worst findings.

frappe.listview_settings["Optimus Session"] = {
	add_fields: ["status", "top_severity", "started_at"],
	get_indicator(doc) {
		const status = doc.status;
		const severity = doc.top_severity;

		// Sessions still in progress → color by status
		if (status === "Recording") return [__("Recording"), "green", "status,=,Recording"];
		if (status === "Stopping" || status === "Analyzing") {
			return [__(status), "orange", `status,=,${status}`];
		}
		if (status === "Failed") return [__("Failed"), "red", "status,=,Failed"];

		// Ready sessions → color by top severity
		if (status === "Ready") {
			if (severity === "High") return [__("High severity"), "red", "top_severity,=,High"];
			if (severity === "Medium") return [__("Medium severity"), "orange", "top_severity,=,Medium"];
			if (severity === "Low") return [__("Low severity"), "blue", "top_severity,=,Low"];
			return [__("No issues"), "green", "top_severity,=,None"];
		}

		return [__(status || "Unknown"), "gray", ""];
	},

	// Round 2 fix #26: quick time-range filters.
	// These appear as shortcut buttons in the list view sidebar, letting
	// a dev jump to "this week's sessions" without manually configuring
	// the filter each time.
	onload(listview) {
		if (!listview.page || !listview.page.add_menu_item) return;

		const add_filter = (label, days) => {
			listview.page.add_menu_item(label, () => {
				const cutoff = frappe.datetime.add_days(
					frappe.datetime.now_datetime(),
					-days,
				);
				listview.filter_area.clear();
				listview.filter_area.add([
					["Optimus Session", "started_at", ">", cutoff],
				]);
			});
		};

		add_filter(__("Last 24 hours"), 1);
		add_filter(__("This week"), 7);
		add_filter(__("This month"), 30);
	},
};
