# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Optimus Telemetry Event — one row per (event_name, signature).

Aggregated failure counter for the opt-in telemetry feature. Writes
flow exclusively through ``optimus.telemetry.flush`` via direct SQL
``INSERT … ON DUPLICATE KEY UPDATE`` — never through this controller
— so ``validate`` / ``on_update`` stay empty and the System Manager
permission is read+delete only.
"""

from frappe.model.document import Document


class OptimusTelemetryEvent(Document):
	pass
