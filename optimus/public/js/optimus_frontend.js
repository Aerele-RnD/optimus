// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt
//
// Optimus — browser-side metrics shim (v0.5.0)
//
// Wraps window.fetch + XMLHttpRequest.prototype to capture XHR timings,
// and uses PerformanceObserver to capture Web Vitals (FCP, LCP, CLS,
// navigation timing). Fires only when the server returns an
// X-Optimus-Recording-Id response header (i.e. there's an active
// profiler session). Buffers in memory and flushes to
// optimus.api.submit_frontend_metrics at stop time (via
// frappe.call) or at beforeunload (via navigator.sendBeacon).
//
// Design principle: wrap WHATWG primitives (window.fetch, XHR), not
// Frappe APIs, so instrumentation survives future Frappe upgrades.
// jQuery $.ajax goes through XHR internally and is caught automatically.

(function () {
	"use strict";

	if (typeof frappe === "undefined") return;
	if (frappe.session && frappe.session.user === "Guest") return;

	var SOFT_CAP_XHR = 500;
	var SOFT_CAP_VITALS = 200;
	var WATCHDOG_FLUSH_MS = 60000;
	var WATCHDOG_XHR_THRESHOLD = 200;

	var xhrBuffer = [];
	var vitalsBuffer = [];
	var currentRecordingId = null;
	var watchdogHandle = null;

	function recordXhr(entry) {
		xhrBuffer.push(entry);
		currentRecordingId = entry.recording_id;
		if (xhrBuffer.length > SOFT_CAP_XHR) xhrBuffer.shift();
	}

	function recordVital(data) {
		vitalsBuffer.push({
			name: data.name,
			value_ms: data.value_ms,
			value: data.value,
			dns_ms: data.dns_ms,
			tcp_ms: data.tcp_ms,
			ttfb_ms: data.ttfb_ms,
			dom_content_loaded_ms: data.dom_content_loaded_ms,
			load_ms: data.load_ms,
			page_url: window.location.pathname + window.location.search,
			timestamp: Date.now(),
			recording_id_hint: currentRecordingId,
		});
		if (vitalsBuffer.length > SOFT_CAP_VITALS) vitalsBuffer.shift();
	}

	// -----------------------------------------------------------------
	// 1. window.fetch wrap
	// -----------------------------------------------------------------
	if (typeof window.fetch === "function") {
		var origFetch = window.fetch;
		window.fetch = function (input, init) {
			var start = performance.now();
			var url = typeof input === "string"
				? input
				: (input && input.url) || "";
			var method = (init && init.method)
				|| (input && typeof input !== "string" && input.method)
				|| "GET";

			return origFetch.apply(this, arguments).then(function (response) {
				try {
					// Gate on active session: skip the header read entirely
					// when no session is running. See the matching gate in
					// the XHR wrap below for the full rationale.
					if (!currentSessionUuid()) return response;
					var recordingId = response.headers.get("X-Optimus-Recording-Id");
					if (recordingId) {
						var size = parseInt(
							response.headers.get("Content-Length") || "0", 10
						) || 0;
						recordXhr({
							recording_id: recordingId,
							url: url,
							method: method,
							duration_ms: Math.round(performance.now() - start),
							status: response.status,
							response_size_bytes: size,
							transport: "fetch",
							timestamp: Date.now(),
						});
					}
				} catch (e) { /* never break the caller */ }
				return response;
			});
		};
	}

	// -----------------------------------------------------------------
	// Byte-length helper: counts actual UTF-8 bytes, not JS characters.
	// responseText.length is a UTF-16 code unit count, which undercounts
	// multi-byte characters (emoji, non-ASCII text). TextEncoder gives
	// us the real wire byte count. For large responses this is O(n)
	// with an allocation — acceptable because it only runs when
	// Content-Length is missing (the common case on modern servers
	// already sets the header).
	// -----------------------------------------------------------------
	function measureBytes(str) {
		if (!str) return 0;
		if (typeof TextEncoder !== "undefined") {
			try {
				return new TextEncoder().encode(str).length;
			} catch (e) { /* fall through */ }
		}
		// Legacy browsers without TextEncoder (shouldn't exist in 2026
		// but defensive): use Blob as a second-choice accurate count.
		try {
			return new Blob([str]).size;
		} catch (e) { /* fall through */ }
		// Last resort: char count. Undercounts multi-byte chars but
		// at least doesn't crash.
		return str.length;
	}

	// -----------------------------------------------------------------
	// 2. XMLHttpRequest wrap (catches jQuery $.ajax too)
	// -----------------------------------------------------------------
	if (typeof XMLHttpRequest !== "undefined") {
		var XHRProto = XMLHttpRequest.prototype;
		var origOpen = XHRProto.open;
		var origSend = XHRProto.send;

		XHRProto.open = function (method, url) {
			this._fp_method = method;
			this._fp_url = url;
			return origOpen.apply(this, arguments);
		};

		XHRProto.send = function () {
			var xhr = this;
			var start = performance.now();
			xhr.addEventListener("loadend", function () {
				try {
					// Gate on active session — without this, Chromium
					// browsers log "Refused to get unsafe header
					// 'X-Optimus-Recording-Id'" on every API response
					// because Frappe stamps Access-Control-Allow-Origin
					// on /api/method/* responses (treats them as CORS),
					// and the header isn't in Access-Control-Expose-
					// Headers when no session is active (the server
					// only adds it during recording). The read returns
					// null harmlessly but spams the console. The gate
					// uses the same widget-data-attribute check the
					// flush path below already uses.
					if (!currentSessionUuid()) return;
					var recordingId = xhr.getResponseHeader("X-Optimus-Recording-Id");
					if (!recordingId) return;
					var size = parseInt(
						xhr.getResponseHeader("Content-Length") || "0", 10
					);
					if (!size && xhr.responseText) {
						size = measureBytes(xhr.responseText);
					}
					recordXhr({
						recording_id: recordingId,
						url: xhr._fp_url || "",
						method: xhr._fp_method || "GET",
						duration_ms: Math.round(performance.now() - start),
						status: xhr.status,
						response_size_bytes: size,
						transport: "xhr",
						timestamp: Date.now(),
					});
				} catch (e) { /* never break the caller */ }
			});
			return origSend.apply(this, arguments);
		};
	}

	// -----------------------------------------------------------------
	// 3. Web Vitals via PerformanceObserver
	// -----------------------------------------------------------------
	try {
		if (typeof PerformanceObserver !== "undefined") {
			new PerformanceObserver(function (list) {
				var entries = list.getEntries();
				for (var i = 0; i < entries.length; i++) {
					var entry = entries[i];
					if (entry.entryType === "paint" && entry.name === "first-contentful-paint") {
						recordVital({ name: "fcp", value_ms: entry.startTime });
					} else if (entry.entryType === "largest-contentful-paint") {
						recordVital({ name: "lcp", value_ms: entry.startTime });
					} else if (entry.entryType === "layout-shift" && !entry.hadRecentInput) {
						recordVital({ name: "cls", value: entry.value });
					} else if (entry.entryType === "navigation") {
						recordVital({
							name: "navigation",
							dns_ms: entry.domainLookupEnd - entry.domainLookupStart,
							tcp_ms: entry.connectEnd - entry.connectStart,
							ttfb_ms: entry.responseStart - entry.requestStart,
							dom_content_loaded_ms: entry.domContentLoadedEventEnd - entry.startTime,
							load_ms: entry.loadEventEnd - entry.startTime,
						});
					}
				}
			}).observe({
				entryTypes: ["paint", "largest-contentful-paint", "layout-shift", "navigation"],
				buffered: true,
			});
		}
	} catch (e) {
		// Older browsers without PerformanceObserver — degrade silently.
	}

	// -----------------------------------------------------------------
	// 4. Flush + watchdog
	// -----------------------------------------------------------------
	function currentSessionUuid() {
		// The floating widget sets data-session-uuid on its DOM element
		// while a session is active. We read it here rather than
		// depending on a shared global, so the two modules stay loosely
		// coupled.
		try {
			var widget = document.getElementById("frappe-profiler-widget");
			if (widget) {
				return widget.getAttribute("data-session-uuid") || null;
			}
		} catch (e) { /* noop */ }
		return null;
	}

	function flush(opts) {
		opts = opts || {};
		if (xhrBuffer.length === 0 && vitalsBuffer.length === 0) return;
		var session_uuid = currentSessionUuid();
		if (!session_uuid) return;

		var payload = {
			session_uuid: session_uuid,
			xhr: xhrBuffer.splice(0),
			vitals: vitalsBuffer.splice(0),
		};

		var body = JSON.stringify(payload);

		// CRITICAL: the server endpoint signature is
		//   submit_frontend_metrics(payload: str)
		// where `payload` is the stringified inner JSON.
		//
		// For frappe.call this is automatic: args:{payload: body}
		// becomes form-encoded kwargs and the function receives
		// payload="<json string>".
		//
		// For sendBeacon we're sending raw body bytes as
		// application/json. Frappe's request handler parses the
		// JSON body and spreads its top-level keys as form_dict
		// entries, which become kwargs on the endpoint. So we
		// MUST wrap our inner JSON as {"payload": body} at the
		// outer level, or the server would see
		// submit_frontend_metrics(session_uuid=..., xhr=..., vitals=...)
		// which doesn't match the signature and fails with
		// TypeError. Earlier versions shipped without this wrap
		// and the beacon path silently dropped every payload.
		var beaconBody = JSON.stringify({ payload: body });

		if (opts.sync && typeof navigator !== "undefined" && navigator.sendBeacon) {
			try {
				navigator.sendBeacon(
					"/api/method/optimus.api.submit_frontend_metrics",
					new Blob([beaconBody], { type: "application/json" })
				);
				return;
			} catch (e) { /* fall through to frappe.call */ }
		}

		try {
			frappe.call({
				method: "optimus.api.submit_frontend_metrics",
				args: { payload: body },
			});
		} catch (e) { /* last-ditch — nothing more we can do */ }
	}

	// Watchdog flush: runs every 60s but ONLY does work when there's an
	// active session AND the buffer has grown past the threshold. The
	// no-session early return keeps cost at ~1 microsecond per tick,
	// which is cheap enough to leave running. We intentionally do NOT
	// tear down the interval when the session ends, because sessions
	// can start and stop multiple times on the same page load and
	// re-creating the interval each time is more code for no benefit.
	function startWatchdog() {
		if (watchdogHandle) return;
		watchdogHandle = setInterval(function () {
			// Cheap gate: no active session → no work.
			if (!currentSessionUuid()) return;
			if (xhrBuffer.length > WATCHDOG_XHR_THRESHOLD) {
				flush({ sync: false });
			}
		}, WATCHDOG_FLUSH_MS);
	}

	window.addEventListener("beforeunload", function () {
		flush({ sync: true });
	});

	startWatchdog();

	// -----------------------------------------------------------------
	// 5. Public interface
	// -----------------------------------------------------------------
	window.optimus_frontend = {
		flush: flush,
		getState: function () {
			return {
				xhr_buffered: xhrBuffer.length,
				vitals_buffered: vitalsBuffer.length,
				current_recording_id: currentRecordingId,
			};
		},
	};
})();
