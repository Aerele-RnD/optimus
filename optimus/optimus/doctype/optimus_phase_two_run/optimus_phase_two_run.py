# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimusPhaseTwoRun(Document):
	# Child table on Optimus Session — orchestration logic lives in
	# optimus.line_profile.capture and .analyzer.
	pass
