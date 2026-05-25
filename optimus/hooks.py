app_name = "optimus"
app_title = "Optimus"
app_publisher = ""
app_description = "Flow-aware performance profiler for Frappe and ERPNext"
app_email = ""
app_license = "mit"
app_logo_url = "/assets/frappe/images/framework.png"  # placeholder; prevents sidebar 404

# Apps
# ------------------

required_apps = ["frappe"]

# Desk assets (Phase 5)
# ---------------------
# The floating start/stop widget is injected into every Desk page. The widget
# JS itself checks for the System Manager / Optimus User role before showing,
# so users without permission see nothing.

# v0.5.2: cache-buster is now the file's mtime + __version__ combined.
# Pre-v0.5.2 we used __version__ alone, which meant any JS/CSS change
# between releases shipped invisibly to browsers still holding the
# previous version's cached file (Frappe's dev server sends
# Cache-Control: max-age=43200, 12h). A real user report showed
# realtime-event code shipped in v0.5.2 was still running the v0.5.1
# HTTP-polling code in the browser because the cache-buster URL
# /assets/.../floating_widget.js?v=0.5.1 was unchanged — the version
# wasn't bumped when JS was edited. Using mtime auto-invalidates on
# every file edit during development, and still includes __version__
# so release-to-release upgrades invalidate cleanly on production
# (where mtimes are stable but version differs).
import os as _os

from optimus import __version__ as _frappe_profiler_version


def _asset_version(relative_path: str) -> str:
	"""Return ``<__version__>.<mtime>`` for a file under public/, or
	just ``<__version__>`` if the file can't be stat'd (unlikely on a
	healthy install — defensive so the hooks file never fails to
	load).

	The mtime component means ANY edit to the JS/CSS auto-invalidates
	the browser cache without a manual __version__ bump. Production
	deploys stat the file at hooks.py import time, so bench restart
	after a deploy captures the new mtime automatically.
	"""
	try:
		full_path = _os.path.join(
			_os.path.dirname(__file__), "public", relative_path
		)
		mtime = int(_os.path.getmtime(full_path))
		return f"{_frappe_profiler_version}.{mtime}"
	except Exception:
		return _frappe_profiler_version


_widget_js_v = _asset_version("js/floating_widget.js")
_frontend_js_v = _asset_version("js/optimus_frontend.js")
_widget_css_v = _asset_version("css/floating_widget.css")

app_include_js = [
	f"/assets/optimus/js/floating_widget.js?v={_widget_js_v}",
	# v0.5.0: browser-side metrics shim (fetch/XHR wrap + Web Vitals).
	# Loads after floating_widget.js so the widget is already in the DOM
	# when optimus_frontend.js reads its data-session-uuid attribute.
	f"/assets/optimus/js/optimus_frontend.js?v={_frontend_js_v}",
]
app_include_css = f"/assets/optimus/css/floating_widget.css?v={_widget_css_v}"

# Installation
# ------------

after_install = "optimus.install.after_install"
before_uninstall = "optimus.install.before_uninstall"

# Boot session
# ------------
# Attaches `optimus_enabled` to `frappe.boot` so the floating widget
# can hide itself when the master kill-switch is off. Without this,
# an admin who disables Optimus Settings still sees the widget —
# which is a dead button (clicking Start does nothing because
# before_request short-circuits). See floating_widget.js for the
# corresponding client-side guard.
boot_session = "optimus.boot.boot_session"

# Request lifecycle (Phase 1)
# ---------------------------
# These hooks run AFTER frappe's own recorder hooks, so by the time
# `before_request` runs, frappe.recorder.record() has already been called
# (and is a no-op without the global flag). Our hook then decides per-user
# whether to force-activate the recorder for this request.
#
# `after_request` runs after frappe.recorder.dump(), so the recording is
# already in Redis by the time we register its UUID with our session.

before_request = [
	"optimus.hooks_callbacks.before_request",
	"optimus.line_profile.hooks.before_request_line_profile",
]
after_request = [
	"optimus.hooks_callbacks.after_request",
	"optimus.line_profile.hooks.after_request_line_profile",
]

# Background job lifecycle (Phase 2)
# ----------------------------------
# These mirror the request hooks. The frappe.enqueue monkey-patch in
# optimus/__init__.py injects `_profiler_session_id` into job
# kwargs at enqueue time, and `before_job` reads (and pops) it to decide
# whether to activate recording for this job.
#
# This is how a customer's "save Sales Invoice → submit" flow captures
# both the synchronous HTTP requests AND the background jobs that the
# submit triggers (GL postings, stock updates, etc.) under one session.

before_job = [
	"optimus.hooks_callbacks.before_job",
	"optimus.line_profile.hooks.before_job_line_profile",
]
after_job = [
	"optimus.hooks_callbacks.after_job",
	"optimus.line_profile.hooks.after_job_line_profile",
]

# Janitor (Phase 6)
# -----------------
# Sweep stale Recording sessions (started but never explicitly stopped)
# and stuck Analyzing sessions (worker crashed mid-analyze) every 5 minutes.

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"optimus.janitor.sweep_stale_sessions",
		],
		# v0.8.0: opt-in failure telemetry. Drains the in-process bounded
		# buffer to the configured sinks. No-op when telemetry is OFF
		# (the master gate is checked inside flush()), so this cron is
		# safe to leave active regardless of the operator's setting.
		"*/10 * * * *": [
			"optimus.telemetry.flush",
		],
	},
	"daily": [
		"optimus.janitor.sweep_old_sessions",
	],
}

# File permission gate (Phase 6)
# ------------------------------
# Server-side double-check that the raw profiler report can only be
# downloaded by System Manager + the recording user. The UI hides the
# download button from non-admins, but this gate also blocks direct URL
# access in case someone guesses the file name.

has_permission = {
	"File": "optimus.permissions.file_has_permission",
}

# v0.4.0: doc_events hooks.
# - User.validate auto-grants Optimus User to any user that has
#   System Manager. See install.on_user_role_change for the logic.
doc_events = {
	"User": {
		"validate": "optimus.install.on_user_role_change",
	},
}
