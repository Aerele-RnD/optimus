# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Capture module: pyinstrument integration and sidecar wraps.

Owns:
  1. Optional pyinstrument import (degrades gracefully if unavailable)
  2. Per-recording pyinstrument session start/stop helpers
  3. Three monkey-patched sidecar wraps on frappe.get_doc /
     frappe.cache.get_value / frappe.permissions.has_permission for
     argument capture (with PII-safe hashing)

Argument values that may contain user data are stored in two forms —
``identifier_raw`` (rendered in the report) and ``identifier_safe``
(a sha256[:12] hash, used as the bucket key for redundant-call
detection). Doctype names and ptypes are NOT hashed because they're
schema-level identifiers, not data.

Activation gate: the sidecar wraps and pyinstrument start are gated on
the presence of `frappe.local._profiler_active_session_id`. That flag is
set by hooks_callbacks.before_request / before_job only when the session
meta has `capture_python_tree=True`. So the wraps' hot-path check is a
single attribute lookup; they never read Redis.
"""

import hashlib
import sys as _sys

# Optional dependency — capture degrades gracefully if pyinstrument is
# not installed (e.g. air-gapped environments, broken pip cache).
try:
	import pyinstrument  # noqa: F401

	_PYINSTRUMENT_AVAILABLE = True
except ImportError:
	_PYINSTRUMENT_AVAILABLE = False


# v0.5.2: how many caller frames to capture for redundant-call attribution.
# Deep enough to walk past framework dispatchers (Document.save →
# run_method → composer → runner → fn → user's validate → wrapped call);
# shallow enough to keep overhead trivial (~1 µs per frame).
_CALLER_STACK_MAX_DEPTH = 20


def _capture_caller_stack() -> list:
	"""Return a list of ``{"filename","lineno","function"}`` dicts for
	the Python frames above this function, skipping the wrap and the
	frames that led into the wrap itself.

	Used ONLY by the sidecar-wrap entries (cache.get_value,
	frappe.get_doc, has_permission) to enable:

	  1. Filtering redundant-call findings whose callsite is inside
	     framework code (users can't act on framework loops).
	  2. Surfacing file:line in the finding detail so users CAN
	     navigate to the loop when the callsite IS user code.

	Implementation notes:

	- Uses ``sys._getframe`` (CPython-specific but fast, ~1 µs per
	  frame) instead of ``traceback.extract_stack`` which formats
	  source-line context that we don't need.
	- Depth-bounded at ``_CALLER_STACK_MAX_DEPTH`` so a runaway
	  recursion doesn't build a huge sidecar entry.
	- Returns [] on any failure (non-CPython interpreter, f_back
	  unexpectedly None) rather than raising — observability code
	  never breaks the host call.
	"""
	try:
		# Skip: _capture_caller_stack itself + the wrap function.
		frame = _sys._getframe(2)
	except (ValueError, AttributeError):
		return []

	stack: list = []
	try:
		while frame is not None and len(stack) < _CALLER_STACK_MAX_DEPTH:
			code = frame.f_code
			stack.append({
				"filename": code.co_filename,
				"lineno": frame.f_lineno,
				"function": code.co_name,
			})
			frame = frame.f_back
	except Exception:
		# Any unexpected walk failure → return what we collected so far.
		pass
	return stack


def _hash_identifier(value) -> str:
	"""Return a deterministic 12-char sha256 hex prefix of `value`.

	None passes through as None (used by has_permission when name is omitted).
	"""
	if value is None:
		return None
	return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _identify_args(fn_name: str, args: tuple, kwargs: dict):
	"""Build (identifier_raw, identifier_safe) for one captured call.

	Sub-shapes per wrapped function:
	  - get_doc("DocType", "name")     → ((doctype, name), (doctype, hash(name)))
	  - get_doc({"doctype": ..., ...}) → extract from dict; name may be missing
	    on unsaved docs ("__islocal"=1), in which case identifier is just doctype
	  - cache_get(self, key)           → (key, hash(key))
	  - has_permission(doctype, ptype="read", doc=None, ...)
	                                   → ((doctype, name, ptype),
	                                      (doctype, hash(name), ptype))
	    where `name` is extracted from `doc` (which may be a Document, dict,
	    or string)

	Both `identifier_raw` and `identifier_safe` are guaranteed to be
	hashable (strings, Nones, or tuples of those) so they can be used as
	dict keys in the redundant_calls bucketing logic.
	"""
	if fn_name == "get_doc":
		first = args[0] if len(args) > 0 else kwargs.get("doctype")
		# Dict-arg form: frappe.get_doc({"doctype": "X", "name": "Y", ...})
		if isinstance(first, dict):
			doctype = first.get("doctype")
			name = first.get("name") if not first.get("__islocal") else None
		else:
			doctype = first
			name = args[1] if len(args) > 1 else kwargs.get("name")
		# Coerce to strings/None so the result is always hashable
		doctype = str(doctype) if doctype is not None else None
		name = str(name) if name is not None else None
		return (doctype, name), (doctype, _hash_identifier(name))

	if fn_name == "cache_get":
		# This wraps RedisWrapper.get_value (a method), so args[0] is the
		# RedisWrapper instance (self) and args[1] is the actual key. We
		# wrap at the class level — not at frappe.cache — because
		# frappe.cache is None at app-import time (no site bound yet).
		key = args[1] if len(args) > 1 else kwargs.get("key")
		# Cache keys may be bytes (Frappe sometimes builds them with the
		# site prefix as bytes); coerce to str for hashability + display.
		if isinstance(key, bytes):
			key = key.decode("utf-8", errors="replace")
		key = str(key) if key is not None else None
		return key, _hash_identifier(key)

	if fn_name == "has_permission":
		# Frappe signature: has_permission(doctype, ptype="read", doc=None, ...)
		# args[0]=doctype, args[1]=ptype, args[2]=doc.
		doctype = args[0] if len(args) > 0 else kwargs.get("doctype")
		ptype = args[1] if len(args) > 1 else kwargs.get("ptype", "read")
		doc = args[2] if len(args) > 2 else kwargs.get("doc")
		# Extract a stable identifier from doc, which may be a Document,
		# a dict, or a string name.
		if doc is None:
			name = None
		elif hasattr(doc, "name"):
			name = getattr(doc, "name", None)
		elif isinstance(doc, dict):
			name = doc.get("name")
		else:
			name = doc
		# Coerce all components to hashable types
		doctype = str(doctype) if doctype is not None else None
		ptype = str(ptype) if ptype is not None else None
		name = str(name) if name is not None else None
		return (doctype, name, ptype), (doctype, _hash_identifier(name), ptype)

	# Unknown — return None tuples so the bucket key is hashable but
	# meaningless (the redundant_calls analyzer skips such entries).
	return (None, None), (None, None)


# Maximum entries per recording's sidecar list. Above this, additional
# wraps drop their entries silently and set a truncation flag on the
# request-local context. The analyze pipeline surfaces this as a warning.
SIDECAR_CAP_PER_RECORDING = 50_000


def _make_wrap(orig, fn_name: str, local_proxy=None):
	"""Build a sidecar-recording wrapper around `orig`.

	`local_proxy` is the request-local namespace where we read the
	activation flag and append entries. In production this is `frappe.local`;
	tests pass a stand-in object so the wrap can be exercised without a
	Frappe runtime.

	Properties:
	  - Passthrough when no active session (single attribute lookup).
	  - Records entries on success AND on exception (try/finally).
	  - Re-entrant call into another wrap from inside one wrap is a
	    passthrough (prevents double-counting `has_permission` → `get_doc`).
	  - Drops entries past SIDECAR_CAP_PER_RECORDING and flags truncation.
	  - Stores the original on `wrapped._profiler_original` so uninstall
	    can restore it. If `orig` is itself an already-wrapped function
	    (has `_profiler_original`), our wrap chains through `orig` —
	    we never double-wrap.
	"""
	def wrapped(*args, **kwargs):
		active = getattr(local_proxy, "_profiler_active_session_id", None)
		if not active:
			return orig(*args, **kwargs)

		in_wrap = getattr(local_proxy, "_profiler_in_wrap", False)
		if in_wrap:
			return orig(*args, **kwargs)

		# Set re-entrancy flag BEFORE doing any work so nested wrapped
		# calls (e.g. has_permission → get_doc) skip recording.
		local_proxy._profiler_in_wrap = True

		# Build the sidecar entry on a best-effort basis. A failure here
		# (malformed args, exotic types) MUST NOT prevent the user's call
		# from running — observability code never breaks the host call.
		try:
			identifier_raw, identifier_safe = _identify_args(fn_name, args, kwargs)
			entry = {
				"fn_name": fn_name,
				"identifier_raw": identifier_raw,
				"identifier_safe": identifier_safe,
				# v0.5.2: caller stack lets redundant_calls.analyze
				# find the user frame (vs filtering out framework-
				# only callsites) AND surface file:line in the
				# finding detail so users can actually navigate to
				# the loop they need to fix. Pre-v0.5.2 findings
				# showed only a hashed cache key with no callsite —
				# users couldn't act on them.
				"caller_stack": _capture_caller_stack(),
			}
		except Exception:
			entry = None

		try:
			return orig(*args, **kwargs)
		finally:
			local_proxy._profiler_in_wrap = False
			if entry is not None:
				sidecar = getattr(local_proxy, "optimus_sidecar", None)
				if sidecar is None:
					local_proxy.optimus_sidecar = [entry]
				elif len(sidecar) >= SIDECAR_CAP_PER_RECORDING:
					local_proxy.optimus_sidecar_truncated = True
				else:
					sidecar.append(entry)

	wrapped._profiler_original = orig
	return wrapped


# Default pyinstrument sample interval in milliseconds. Overridable via
# site_config.json: optimus_sampler_interval_ms. 1ms is pyinstrument's
# default and balances fidelity vs overhead well.
DEFAULT_SAMPLER_INTERVAL_MS = 1


def _start_pyi_session(local_proxy, interval_ms: float = DEFAULT_SAMPLER_INTERVAL_MS):
	"""Start a pyinstrument profiler scoped to this request.

	Stores the running profiler on `local_proxy.optimus_pyinstrument` so
	`after_request`/`after_job` can stop and serialize it. Returns the
	profiler instance, or None if pyinstrument is not available.

	Note: pyinstrument is imported inside the try-except so a broken
	install (rare, but possible in air-gapped environments) doesn't
	break app load. The module-level _PYINSTRUMENT_AVAILABLE flag is the
	authoritative check.
	"""
	if not _PYINSTRUMENT_AVAILABLE:
		return None
	try:
		from pyinstrument import Profiler

		# pyinstrument expects interval in seconds (float)
		prof = Profiler(interval=interval_ms / 1000.0, async_mode="enabled")
		prof.start()
		local_proxy.optimus_pyinstrument = prof
		return prof
	except Exception:
		# Any failure to start pyinstrument is non-fatal — degrade to
		# SQL-only capture for this recording.
		return None


def _force_stop_inflight_capture(local_proxy):
	"""Stop any in-flight pyinstrument session and clear all capture state.

	Called by api.start() (and the underlying _stop_session) before
	flipping the active flag, so a previous in-flight capture from the
	same worker doesn't leak into the new session.
	"""
	prof = getattr(local_proxy, "optimus_pyinstrument", None)
	if prof is not None:
		try:
			prof.stop()
		except Exception:
			pass
		try:
			delattr(local_proxy, "optimus_pyinstrument")
		except AttributeError:
			pass

	for attr in (
		"_profiler_active_session_id",
		"optimus_sidecar",
		"optimus_sidecar_truncated",
		"_profiler_in_wrap",
	):
		try:
			delattr(local_proxy, attr)
		except AttributeError:
			pass


# ----- Wrap installation on the real frappe modules -----------------------
#
# Installed once at app import time from optimus/__init__.py.
# install_wraps() is idempotent: calling it twice does not double-wrap,
# and pre-existing wraps from other apps are detected via the
# _profiler_original attribute convention.


def _wrap_targets():
	"""Return the list of (module, attr_name, fn_name) tuples to wrap.

	Lazy so that importing capture.py does not import frappe.permissions
	or frappe.utils.redis_wrapper (which would trigger circular imports
	at app load on some sites).

	Note about the cache target: we wrap `RedisWrapper.get_value` (a class
	method), NOT `frappe.cache.get_value`, because `frappe.cache` is None
	at app-import time (the per-site cache instance is bound only after
	`frappe.init(site)` runs). Wrapping the class method ensures every
	cache instance created later uses the wrapped version. Because this
	is a method wrap, the wrapper sees `self` as args[0] and the actual
	key as args[1] — handled in `_identify_args` for `cache_get`.
	"""
	import frappe
	import frappe.permissions
	import frappe.utils.redis_wrapper

	return [
		(frappe, "get_doc", "get_doc"),
		(frappe.utils.redis_wrapper.RedisWrapper, "get_value", "cache_get"),
		(frappe.permissions, "has_permission", "has_permission"),
	]


def install_wraps():
	"""Install all three sidecar wraps. Idempotent.

	If `frappe.get_doc` is already a `_profiler_is_our_wrap`-tagged wrapper,
	we do not double-wrap.
	"""
	import frappe

	for module, attr, fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			continue  # already wrapped by us
		new_wrap = _make_wrap(current, fn_name, local_proxy=frappe.local)
		new_wrap._profiler_is_our_wrap = True
		setattr(module, attr, new_wrap)


def uninstall_wraps():
	"""Restore originals. Used by before_uninstall and tests."""
	for module, attr, _fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			setattr(module, attr, current._profiler_original)
