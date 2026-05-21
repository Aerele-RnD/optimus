# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimusBackgroundJob(Document):
	# Child table on Optimus Session — one row per RQ job the recorded flow
	# enqueued, with its terminal status. Populated by analyze.run from the
	# per-session job-meta hash (see optimus.session + optimus.analyze).
	pass
