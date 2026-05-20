// Optimus — Floating start/stop widget
//
// Injected into every Desk page via app_include_js in hooks.py.
// Renders a small floating button bottom-right that lets a user with the
// Optimus User (or System Manager) role start and stop a profiling session.
//
// State machine:
//
//   inactive  →  click → start dialog → start API → recording
//   recording →  click → stop API → stopping → analyzing → ready
//   ready     →  click → navigate to the Optimus Session detail view
//
// Polls /api/method/optimus.api.status every 5 seconds to reflect
// server-side state changes (auto-stop after 10 minutes, analyze completion).
// Also subscribes to the `optimus_session_ready` realtime event so the user
// is notified the moment their session report is ready.

(function () {
	"use strict";

	// v0.5.1 diagnostic: hardcoded build ID bumped on every widget fix.
	// This lets us confirm (from browser devtools) that the user is
	// actually running the latest JS, vs a stale cached copy from an
	// earlier page load. If the build ID in the console / in the
	// widget's title attribute doesn't match what we think is current,
	// the user is on cached JS and needs a hard refresh + bench restart.
	const WIDGET_BUILD_ID = "2026-04-16-hide-when-disabled";

	// Developer-only diagnostic. Defaults to silent in production so the
	// browser console isn't spammed on every Desk page. Set
	// ``window.OPTIMUS_DEBUG = true`` before page load (or from devtools)
	// to surface the per-step trace. Replaces the prior unconditional
	// ``console.log`` / ``console.warn`` / ``console.error`` calls.
	function _diag() {
		if (window.OPTIMUS_DEBUG !== true) return;
		if (typeof console === "undefined" || !console.log) return;
		try {
			console.log.apply(
				console,
				["[optimus]"].concat([].slice.call(arguments))
			);
		} catch (e) { /* swallow */ }
	}

	_diag(
		"floating_widget.js LOADED",
		"build=" + WIDGET_BUILD_ID,
		"at", new Date().toISOString()
	);

	// Only run inside Desk (not on web pages or guest sessions).
	if (typeof frappe === "undefined" || typeof frappe.session === "undefined") {
		_diag("[optimus] no frappe global, script exiting");
		return;
	}
	if (frappe.session.user === "Guest") {
		_diag("[optimus] user is Guest, script exiting");
		return;
	}
	_diag("[optimus] proceeding for user:", frappe.session.user);

	// v0.5.1: HTTP polling of optimus.api.status is GONE. The
	// widget now drives its state from realtime events pushed by the
	// server (optimus_session_stopping / analyzing / ready / failed)
	// plus a one-shot rehydrate fetch on page load and on tab visibility
	// change. This eliminates the continuous /api/method/optimus
	// .api.status traffic that was showing up as the top-QPS endpoint
	// on busy sites. POLL_INTERVAL_MS is kept as a legacy constant (some
	// external integrations may reference it) but no timer uses it.
	const POLL_INTERVAL_MS = 0;
	const REQUIRED_ROLES = ["System Manager", "Optimus User", "Administrator"];

	let widget = null;
	let elapsedHandle = null;
	let currentState = {
		active: false,
		session_uuid: null,
		docname: null,
		label: null,
		started_at: null,
		display: "inactive", // inactive | recording | stopping | analyzing | ready
	};

	function userHasRole() {
		// frappe.user_roles is set by Desk.set_globals(). On initial page
		// load our app_include_js script may run before set_globals, so
		// fall back to frappe.boot.user.roles which is populated inline
		// in the Desk HTML before any script executes.
		// Administrator is always allowed (matches api._require_profiler_user).
		if (frappe.session && frappe.session.user === "Administrator") {
			return true;
		}
		const roles =
			frappe.user_roles
			|| (frappe.boot && frappe.boot.user && frappe.boot.user.roles)
			|| [];
		return REQUIRED_ROLES.some((r) => roles.includes(r));
	}

	function init() {
		// v0.5.2 round 4: hide widget entirely when Optimus Settings ▸
		// Profiler Enabled is off. A disabled widget is a dead button
		// (before_request / before_job short-circuit), so showing it
		// misleads the user into thinking they can still record.
		// `frappe.boot.optimus_enabled` is populated by boot_session
		// and defaults to True on error — matching the settings
		// module's fail-open posture.
		const bootEnabled = (
			frappe.boot && frappe.boot.optimus_enabled
		);
		if (bootEnabled === false) {
			_diag(
				"[optimus] profiler is disabled in settings — "
				+ "widget will not mount"
			);
			return;
		}

		// Mount the widget UNCONDITIONALLY for any logged-in user. The
		// server-side `_require_profiler_user()` check in api.py is the
		// real permission gate; this client-side check is just a hint
		// and was causing race-condition bugs because `frappe.user_roles`
		// may not be populated when this script runs (see desk.js:329 —
		// set_globals runs after Desk class construction). We err on the
		// side of always mounting; users without the role will see a
		// permission error if they actually click Start.
		mountWidget();
		// v0.5.1: one-shot rehydrate on page load. Covers the case
		// where the user reloaded / navigated back to the Desk while
		// a session was already active. After this initial call,
		// state is driven entirely by realtime events + visibility
		// changes. No interval polling.
		try { refreshStatus(); } catch (e) { /* noop */ }
		try { subscribeRealtime(); } catch (e) { /* noop */ }
		try { subscribeVisibility(); } catch (e) { /* noop */ }
		try { maybeShowOnboardingToast(); } catch (e) { /* noop */ }
	}

	function maybeShowOnboardingToast() {
		// v0.4.0: one-time dismissible toast on first Desk visit after install.
		// Server decides whether to show (via api.check_onboarding_seen),
		// which suppresses for experienced users automatically.
		frappe.call({
			method: "optimus.api.check_onboarding_seen",
			callback: function (r) {
				var data = (r && r.message) || {};
				if (data.seen) return;
				renderOnboardingToast();
			},
		});
	}

	function renderOnboardingToast() {
		// Avoid double-rendering if init runs twice
		if (document.getElementById("frappe-profiler-onboarding-toast")) return;
		var toast = document.createElement("div");
		toast.id = "frappe-profiler-onboarding-toast";
		toast.style.cssText = [
			"position: fixed",
			"top: 20px",
			"right: 20px",
			"z-index: 2147483647",
			"max-width: 360px",
			"padding: 14px 18px",
			"border-radius: 10px",
			"border: 1px solid #16a34a",
			"background: #f0fdf4",
			"color: #166534",
			"font-family: -apple-system, BlinkMacSystemFont, sans-serif",
			"font-size: 0.9rem",
			"line-height: 1.4",
			"box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18)",
			"cursor: default",
		].join("; ");
		toast.innerHTML = [
			'<div style="margin-bottom: 8px; font-weight: 700;">Optimus is installed</div>',
			'<div style="margin-bottom: 12px;">',
			"Click the <strong>Profiler</strong> pill in the bottom-right corner ",
			"to record your first slow flow.",
			"</div>",
			'<button id="frappe-profiler-onboarding-dismiss" ',
			'style="background: #16a34a; color: #fff; border: none; padding: 6px 14px; ',
			'border-radius: 6px; cursor: pointer; font-weight: 600;">Got it</button>',
		].join("");
		document.body.appendChild(toast);

		var dismissed = false;
		function dismiss() {
			if (dismissed) return;
			dismissed = true;
			try {
				toast.remove();
			} catch (e) { /* noop */ }
			try {
				frappe.call({ method: "optimus.api.mark_onboarding_seen" });
			} catch (e) { /* noop */ }
		}
		var btn = document.getElementById("frappe-profiler-onboarding-dismiss");
		if (btn) btn.addEventListener("click", dismiss);
		// Auto-dismiss after 15 seconds
		setTimeout(dismiss, 15000);
	}

	function subscribeVisibility() {
		// v0.5.1: no more polling — but we still want to refresh state
		// ONCE when the tab becomes visible again, to catch TTL-based
		// auto-stop that happens silently (Redis active-pointer expired
		// while the tab was hidden) and any realtime events the Socket
		// .IO client might have missed during tab sleep. Browser
		// Background Fetch / Socket.IO idle throttling can occasionally
		// drop events; a one-shot rehydrate on return-to-tab covers it.
		document.addEventListener("visibilitychange", () => {
			if (!document.hidden) {
				refreshStatus();
			}
		});
	}

	function mountWidget() {
		_diag("[optimus] mountWidget() called");
		// Idempotent: don't double-mount on accidental re-init.
		const existing = document.getElementById("frappe-profiler-widget");
		if (existing) {
			_diag("[optimus] widget already in DOM, skipping mount");
			widget = existing;
			return;
		}
		widget = document.createElement("div");
		widget.id = "frappe-profiler-widget";
		widget.className = "fp-state-inactive";
		// Expose the build ID so a hovering user / dev-tools inspector
		// can confirm which JS is actually running without opening
		// the console.
		widget.title = "Optimus — build " + WIDGET_BUILD_ID;
		widget.setAttribute("data-build-id", WIDGET_BUILD_ID);
		widget.innerHTML = `
			<span class="fp-dot"></span>
			<span class="fp-label">Profiler</span>
			<span class="fp-elapsed"></span>
		`;
		widget.addEventListener("click", onClick);
		// Inline-style only the layout primitives that need to override
		// any Desk-side CSS regression (positioning + force-visible).
		// Visual styling (size, padding, border, colour, font) lives in
		// floating_widget.css so the pill stays mobile-overlay-sized.
		widget.style.cssText = [
			"position: fixed",
			"right: 20px",
			"bottom: 20px",
			"z-index: 2147483647",  /* max int32 — above everything */
			"display: block !important",
			"visibility: visible !important",
			"opacity: 1 !important",
			"pointer-events: auto",
		].join("; ");
		document.body.appendChild(widget);
		_diag("[optimus] widget appended to body, id=#frappe-profiler-widget");
		_diag("[optimus] widget element:", widget);
		_diag("[optimus] body has child count:", document.body.children.length);
	}

	function setDisplay(display, label, elapsed) {
		if (!widget) return;
		widget.classList.remove(
			"fp-state-inactive",
			"fp-state-recording",
			"fp-state-stopping",
			"fp-state-analyzing",
			"fp-state-ready",
		);
		widget.classList.add(`fp-state-${display}`);
		widget.querySelector(".fp-label").textContent = label;
		widget.querySelector(".fp-elapsed").textContent = elapsed || "";
	}

	function refreshStatus() {
		// If we're locally in a transient state (stopping/analyzing/ready),
		// don't override it with status() — server may not have reflected
		// our local action yet.
		if (currentState.display === "stopping" || currentState.display === "analyzing") {
			return;
		}
		frappe.call({
			method: "optimus.api.status",
			callback: (r) => {
				// v0.5.0 pass-4 fix: re-check the display state inside
				// the callback. The guard at the top of refreshStatus
				// only prevents NEW polls from firing during transient
				// states — it doesn't help an in-flight poll whose
				// frappe.call was already dispatched before the user
				// clicked Stop. Without this check, a late-arriving
				// status response would overwrite the "stopping"
				// display back to "recording" and break the state
				// machine.
				if (
					currentState.display === "stopping"
					|| currentState.display === "analyzing"
					|| currentState.display === "ready"
				) {
					return;
				}

				const data = r.message || {};
				if (data.active) {
					currentState.active = true;
					currentState.session_uuid = data.session_uuid;
					currentState.docname = data.docname;
					currentState.label = data.label;
					currentState.started_at = data.started_at;
					currentState.display = "recording";
					startElapsedTimer();
					setDisplay("recording", "Recording", computeElapsed());
					// v0.5.0: expose session UUID on the widget element so
					// optimus_frontend.js can tag its flush payloads without
					// a shared global.
					if (widget) {
						widget.setAttribute("data-session-uuid", data.session_uuid || "");
					}
				} else {
					if (currentState.display === "recording") {
						// We thought we were recording but server says no — auto-stop
						// fired or someone called stop from elsewhere. Reset.
						currentState.display = "inactive";
						currentState.active = false;
						stopElapsedTimer();
					}
					if (currentState.display !== "ready") {
						setDisplay("inactive", "Profiler", "");
					}
					if (widget) {
						widget.removeAttribute("data-session-uuid");
					}
				}
			},
		});
	}

	function onClick() {
		if (currentState.display === "inactive") {
			openStartDialog();
		} else if (currentState.display === "recording") {
			// v0.6.0: phase-2 recording uses a different stop API and a
			// different active-flag in Redis. Route the click accordingly
			// — confirmAndStop calls api.stop which only knows about
			// phase-1's flag and would no-op silently for phase 2.
			if (currentState.phase2 && currentState.run_uuid) {
				stopPhase2();
			} else {
				confirmAndStop();
			}
		} else if (currentState.display === "ready") {
			// Navigate to the session detail view
			if (currentState.docname) {
				frappe.set_route("Form", "Optimus Session", currentState.docname);
				currentState.display = "inactive";
				setDisplay("inactive", "Profiler", "");
			}
		}
		// stopping/analyzing: clicks are no-op until the state resolves
	}

	function stopPhase2() {
		const run_uuid = currentState.run_uuid;
		if (!run_uuid) return;
		// Optimistic UI: the realtime phase_2_run_analyzing event will
		// arrive within ~1s and confirm the transition.
		setDisplay("stopping", "Stopping phase 2…", "");
		frappe.call({
			method: "optimus.api.stop_line_profile_pass",
			args: { run_uuid: run_uuid },
			callback: () => {
				frappe.show_alert({
					message: __(
						"Phase 2 stopped — analyzing now. Open the session " +
						"to see the line-level report when it's ready."
					),
					indicator: "blue",
				});
			},
			error: () => {
				// Server rejected: surface the error and revert state.
				setDisplay("recording", "Phase 2 recording…", "click form to stop");
			},
		});
	}

	/**
	 * Derive a contextual default for the Session label field from
	 * the current Frappe Desk route. Used by openStartDialog() so the
	 * user can click Start without typing — the most common "what am
	 * I profiling" answer is already on screen.
	 *
	 * Route shapes handled (Frappe v16):
	 *   Form      ["Form", "Sales Invoice", "SINV-2026-001"]
	 *   List      ["List", "Sales Invoice"]
	 *   Report    ["List", "Sales Invoice", "Report"]
	 *   Kanban    ["List", "Sales Invoice", "Kanban", "<name>"]
	 *   Dashboard ["List", "Sales Invoice", "Dashboard"]
	 *   Tree      ["Tree", "Account"]
	 *   Query Rpt ["query-report", "<report name>"]
	 *   anything else → fallback "Profiling session"
	 */
	function getDefaultSessionLabel() {
		// Compose the route-derived label first, then suffix the
		// current time (HH:MM) so two sessions captured on the same
		// route are easy to tell apart at a glance in the list view.
		function _routeLabel() {
			try {
				const route = (typeof frappe !== "undefined" && frappe.get_route)
					? frappe.get_route()
					: [];
				if (!route || !route.length) {
					return "Profiling session";
				}
				const head = route[0];
				const doctype = route[1];
				const sub = route[2];
				const leaf = route[3];
				switch (head) {
					case "Form":
						return doctype && leaf
							? `${doctype} — ${leaf}`
							: doctype || "Form";
					case "List":
						if (!doctype) return "List";
						if (sub === "Kanban") {
							return leaf
								? `${doctype} kanban — ${leaf}`
								: `${doctype} kanban`;
						}
						if (sub === "Report") return `${doctype} report`;
						if (sub === "Dashboard") return `${doctype} dashboard`;
						if (sub === "Calendar") return `${doctype} calendar`;
						if (sub === "Gantt") return `${doctype} gantt`;
						return `${doctype} list`;
					case "Tree":
						return doctype ? `${doctype} tree` : "Tree";
					case "query-report":
						return doctype ? `Report — ${doctype}` : "Report";
					case "dashboard-view":
						return doctype ? `Dashboard — ${doctype}` : "Dashboard";
					case "modules":
					case "desk":
					case "app":
						return "Profiling session";
					default:
						if (head && doctype) return `${head} — ${doctype}`;
						if (head) return String(head);
						return "Profiling session";
				}
			} catch (e) {
				return "Profiling session";
			}
		}

		function _datetimeStamp() {
			try {
				const now = new Date();
				const y = String(now.getFullYear());
				const mo = String(now.getMonth() + 1).padStart(2, "0");
				const d = String(now.getDate()).padStart(2, "0");
				const hh = String(now.getHours()).padStart(2, "0");
				const mm = String(now.getMinutes()).padStart(2, "0");
				return `${y}-${mo}-${d} ${hh}:${mm}`;
			} catch (e) {
				return "";
			}
		}

		const route = _routeLabel();
		const t = _datetimeStamp();
		return t ? `${route} · ${t}` : route;
	}

	function openStartDialog() {
		const d = new frappe.ui.Dialog({
			title: "Start profiling session",
			fields: [
				{
					fieldname: "label",
					fieldtype: "Data",
					label: "Session label",
					reqd: 1,
					default: getDefaultSessionLabel(),
					description:
						"Give this session a name you'll recognize later — e.g. 'Sales Invoice flow with 50 items'.",
				},
				{
					fieldname: "warning_html",
					fieldtype: "HTML",
					options: `
						<div style="background: #fffbeb; border: 1px solid #fbbf24; border-radius: 4px; padding: 10px 12px; margin-top: 10px; font-size: 0.85rem; color: #92400e;">
							<strong>Note:</strong> Recording adds ~1.5–2× wall-clock overhead per request while it's running.
							Only your traffic will be captured — other users on this site are not affected.
							The session auto-stops after 10 minutes.
						</div>
					`,
				},
			],
			primary_action_label: "Start",
			primary_action: (values) => {
				d.hide();
				frappe.call({
					method: "optimus.api.start",
					args: {
						label: values.label || "",
						// v0.7 GA: tree capture is always on - users
						// who turn it off then file "no candidates"
						// bugs. The api.start kwarg still defaults
						// True so CLI / external callers can opt
						// out if they really need SQL-only capture.
						capture_python_tree: 1,
					},
					callback: (r) => {
						const data = (r && r.message) || {};
						if (data.session_uuid) {
							currentState.active = true;
							currentState.session_uuid = data.session_uuid;
							currentState.docname = data.docname;
							currentState.label = data.title;
							currentState.started_at = data.started_at;
							currentState.display = "recording";
							startElapsedTimer();
							setDisplay("recording", "Recording", "0:00");
							// v0.5.0: expose session UUID for optimus_frontend.js.
							if (widget) {
								widget.setAttribute("data-session-uuid", data.session_uuid || "");
							}
							frappe.show_alert({
								message: __("Profiler started: {0}", [data.title]),
								indicator: "green",
							});
						} else {
							// Server returned 200 but no session_uuid in the
							// response — unexpected. Surface something so
							// the user knows the click didn't land.
							frappe.show_alert({
								message: __("Profiler start returned no session — check Error Log"),
								indicator: "orange",
							});
						}
					},
					error: (r) => {
						// v0.5.1: frappe.call's success callback is NOT
						// invoked on server errors (4xx/5xx). Without
						// this error branch, a failed start (permission
						// denied, server exception, concurrent session,
						// etc.) would leave the user staring at a
						// silent inactive pill after the dialog closes —
						// the exact 'widget not working as expected'
						// failure mode they'd see if their account
						// lacked the Optimus User role.
						//
						// Frappe already surfaces server errors as its
						// own alert via _server_messages, so we just
						// need an additional best-effort toast so the
						// user sees SOMETHING happened. The dialog has
						// already been hidden at this point so we can't
						// keep the user on it for another try.
						const msg = (r && r._server_messages)
							? "Profiler start failed — see the Frappe error alert above"
							: "Profiler start failed — check your role and try again";
						frappe.show_alert({
							message: __(msg),
							indicator: "red",
						});
					},
				});
			},
		});
		d.show();
	}

	function confirmAndStop() {
		// No confirmation modal — keep it one-click. Just fire stop().
		// Update currentState.display BEFORE the API call so refreshStatus()'s
		// transient-state guard (line ~241) kicks in — otherwise the 5s poll
		// can race the stop API and flip the widget back to "Recording".
		//
		// v0.5.0: also flush any buffered frontend metrics before the stop
		// API fires, so analyze can join them to recordings. Best-effort —
		// a failed flush never blocks stop.
		_diag("[optimus] confirmAndStop: click received");
		setDisplay("stopping", "Stopping…", "");
		currentState.display = "stopping";
		stopElapsedTimer();

		try {
			if (window.optimus_frontend && window.optimus_frontend.flush) {
				window.optimus_frontend.flush({ sync: false });
			}
		} catch (e) { /* noop — frontend module missing or flush failed */ }

		frappe.call({
			method: "optimus.api.stop",
			callback: (r) => {
				const data = (r && r.message) || {};
				_diag("[optimus] stop callback:", data);

				// v0.5.1: handle the "no active session" case explicitly.
				// Stop API returns {stopped: false, reason: "no active session"}
				// when the session has already been cleared (auto-stop,
				// janitor sweep, or a retried click after a network blip
				// on the first stop). Previously we fell into the else
				// branch and transitioned to "Analyzing…" — wrong, because
				// there's nothing analyzing. The widget would hang on
				// Analyzing… forever because no realtime event would ever
				// fire for a session that no longer exists server-side.
				if (data.stopped === false) {
					currentState.display = "inactive";
					currentState.active = false;
					currentState.session_uuid = null;
					if (widget) {
						widget.removeAttribute("data-session-uuid");
					}
					setDisplay("inactive", "Profiler", "");
					frappe.show_alert({
						message: __("No active session — widget reset"),
						indicator: "gray",
					});
					return;
				}

				if (data.ran_inline) {
					// v0.5.0: scheduler was disabled and analyze ran
					// synchronously inside the stop request. The session
					// is already finalized (Ready or Failed) by the time
					// we get here — branch on data.status so a failed
					// inline analyze doesn't show "Report ready" to the
					// user. In both branches we transition to "ready"
					// state so the user can click the pill and land on
					// the session form (which shows Ready/Failed clearly).
					currentState.display = "ready";
					currentState.docname = data.docname;
					if (data.status === "Failed") {
						setDisplay("ready", "Analyze failed", "click to view");
						frappe.show_alert({
							message: __("Profiler analyze failed — click the pill to see details"),
							indicator: "red",
						});
					} else {
						setDisplay("ready", "Report ready", "click to view");
						frappe.show_alert({
							message: __("Profiler report ready"),
							indicator: "blue",
						});
					}
				} else {
					setDisplay("analyzing", "Analyzing…", "");
					currentState.display = "analyzing";
					frappe.show_alert({
						message: __("Profiler stopped — analyzing session…"),
						indicator: "orange",
					});
				}
			},
			error: (r) => {
				// Stop failed at the network or server level. Don't
				// unconditionally revert to Recording — we don't actually
				// know whether the stop succeeded on the server. It's
				// possible the session was already cleared (auto-stop,
				// janitor sweep) and the 'error' is a 400 / 500 we
				// can't easily distinguish from a real network blip.
				//
				// Safer: call status() to ask the server what it thinks.
				// If active → really revert to Recording. If inactive →
				// the session is gone; reset the widget to inactive.
				_diag("[optimus] stop error:", r);
				frappe.call({
					method: "optimus.api.status",
					callback: (sr) => {
						const sdata = (sr && sr.message) || {};
						if (sdata.active) {
							// Session is still live server-side — retry is
							// meaningful.
							currentState.display = "recording";
							setDisplay("recording", "Recording", computeElapsed());
							startElapsedTimer();
							frappe.show_alert({
								message: __("Failed to stop profiler — try again"),
								indicator: "red",
							});
						} else {
							// Server says the session is gone. Something
							// stopped it (the stop we just sent, an
							// auto-stop, or the janitor). Reset widget.
							currentState.display = "inactive";
							currentState.active = false;
							currentState.session_uuid = null;
							if (widget) {
								widget.removeAttribute("data-session-uuid");
							}
							setDisplay("inactive", "Profiler", "");
							frappe.show_alert({
								message: __("Session already stopped"),
								indicator: "gray",
							});
						}
					},
					error: () => {
						// Both stop AND status failed. Network is probably
						// down. Reset to recording so the user can retry
						// when the network comes back.
						currentState.display = "recording";
						setDisplay("recording", "Recording", computeElapsed());
						startElapsedTimer();
						frappe.show_alert({
							message: __("Network error — please retry"),
							indicator: "red",
						});
					},
				});
			},
		});
	}

	function subscribeRealtime() {
		// v0.5.1: realtime is now the PRIMARY state-transition channel
		// (polling is gone). Server emits:
		//
		//   optimus_session_stopping   — user clicked Stop on another tab
		//   optimus_session_analyzing  — analyze.run started
		//   optimus_progress           — percent + description during analyze
		//   optimus_session_ready      — analyze finished, report available
		//   optimus_session_failed     — analyze crashed
		//
		// The widget rehydrates state from these events so a session
		// driven from tab A is visible in tabs B, C, D without any tab
		// issuing an HTTP poll.
		if (!frappe.realtime || !frappe.realtime.on) {
			return;
		}

		// Helper: skip events for sessions the widget isn't tracking.
		function matchesCurrentSession(data) {
			if (!data || !data.session_uuid) return false;
			if (
				currentState.session_uuid
				&& data.session_uuid !== currentState.session_uuid
			) {
				return false;
			}
			return true;
		}

		frappe.realtime.on("optimus_session_stopping", (data) => {
			if (!matchesCurrentSession(data)) return;
			// Another tab clicked Stop. Transition this tab out of
			// Recording so the user doesn't see stale state.
			if (currentState.display === "recording") {
				currentState.display = "stopping";
				stopElapsedTimer();
				setDisplay("stopping", "Stopping…", "");
			}
		});

		frappe.realtime.on("optimus_session_analyzing", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "analyzing";
			currentState.docname = data.docname || currentState.docname;
			setDisplay("analyzing", "Analyzing…", "0%");
		});

		frappe.realtime.on("optimus_session_failed", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "ready";  // reuse ready slot so click opens the doc
			currentState.docname = data.docname || currentState.docname;
			setDisplay("ready", "Analyze failed", "click to view");
			frappe.show_alert({
				message: __("Profiler analyze failed — check Error Log"),
				indicator: "red",
			});
		});

		frappe.realtime.on("optimus_session_ready", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "ready";
			currentState.docname = data.docname;
			setDisplay("ready", "Report ready", "click to view");
			frappe.show_alert({
				message: __("Profiler report ready"),
				indicator: "blue",
			});
		});

		// Round 2 fix #17: show live progress during analyze
		frappe.realtime.on("optimus_progress", (data) => {
			if (!matchesCurrentSession(data)) return;
			if (currentState.display !== "analyzing") return;
			const pct = typeof data.percent === "number" ? Math.round(data.percent) : 0;
			setDisplay("analyzing", `Analyzing ${pct}%`, data.description || "");
		});

		// v0.6.0: phase-2 line-profile events. Widget reflects the same
		// state-machine slots as phase-1 (recording → analyzing → ready),
		// but the labels are prefixed with "Phase 2" so the user knows
		// which mode is running. Phase 2 is started/stopped from the
		// Optimus Session form, not the floating widget — clicking the
		// widget while Phase 2 is recording still means "stop" via the
		// existing Stop API path (api.stop_line_profile_pass), which the
		// form's history list reflects after refresh.
		frappe.realtime.on("phase_2_run_recording", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "recording";
			currentState.phase2 = true;
			currentState.run_uuid = data.run_uuid;
			currentState.session_uuid = data.session_uuid;
			setDisplay(
				"recording",
				"Phase 2 recording…",
				"click pill or form to stop"
			);
		});

		frappe.realtime.on("phase_2_run_analyzing", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "analyzing";
			currentState.phase2 = true;
			currentState.run_uuid = data.run_uuid || currentState.run_uuid;
			setDisplay("analyzing", "Phase 2 analyzing…", "");
		});

		frappe.realtime.on("phase_2_run_ready", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "ready";
			currentState.phase2 = false;
			currentState.run_uuid = null;
			currentState.docname = data.parent || currentState.docname;
			setDisplay("ready", "Phase 2 report ready", "click to view");
			frappe.show_alert({
				message: __("Phase 2 line-profile report ready"),
				indicator: "blue",
			});
		});

		frappe.realtime.on("phase_2_run_failed", (data) => {
			if (!matchesCurrentSession(data)) return;
			currentState.display = "ready";
			currentState.phase2 = false;
			setDisplay("ready", "Phase 2 failed", "click to view");
			frappe.show_alert({
				message: __("Phase 2 analyze failed: " + (data.error || "unknown")),
				indicator: "red",
			});
		});
	}

	function computeElapsed() {
		if (!currentState.started_at) return "";
		const start = new Date(currentState.started_at).getTime();
		if (isNaN(start)) return "";
		const seconds = Math.floor((Date.now() - start) / 1000);
		const m = Math.floor(seconds / 60);
		const s = seconds % 60;
		return `${m}:${s.toString().padStart(2, "0")}`;
	}

	function startElapsedTimer() {
		stopElapsedTimer();
		elapsedHandle = setInterval(() => {
			if (currentState.display === "recording") {
				const el = widget && widget.querySelector(".fp-elapsed");
				if (el) el.textContent = computeElapsed();
			}
		}, 1000);
	}

	function stopElapsedTimer() {
		if (elapsedHandle) {
			clearInterval(elapsedHandle);
			elapsedHandle = null;
		}
	}

	// Bootstrap: wait for document.body to exist, then mount the widget
	// UNCONDITIONALLY. We don't gate on frappe.user_roles or frappe.boot
	// because those may not be populated when our app_include_js script
	// runs (Desk.set_globals at desk.js:329 runs later, asynchronously).
	// The server-side _require_profiler_user check is the real gate.
	function bootstrap() {
		_diag("[optimus] bootstrap() called, document.body exists:", !!document.body);
		if (!document.body) {
			setTimeout(bootstrap, 50);
			return;
		}
		try {
			init();
		} catch (e) {
			_diag("[optimus] init failed:", e);
		}
	}

	_diag("[optimus] document.readyState:", document.readyState);
	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", bootstrap);
	} else {
		bootstrap();
	}
})();
