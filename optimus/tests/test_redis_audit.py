# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Drift-protection audit for the v0.12.0 Redis-keys centralization
contract.

From v0.12.0 forward, every ``frappe.cache.*`` call site in
``optimus/`` MUST use a builder from :mod:`optimus.redis_keys` for its
key argument. Inline f-string keys (``f"profiler:..."`` /
``f"optimus:..."``) inside a ``frappe.cache.X(...)`` call are orphans
and fail this test. The audit also asserts the doc inventory
(``docs/REDIS-SCHEMA.md``) matches the canonical
:data:`optimus.redis_keys.KEY_PATTERNS` tuple — drift in either
direction fails CI.

Excluded:
  * ``optimus/tests/`` and ``optimus/tests_integration/`` — test
    fixtures legitimately stub Redis keys.
  * ``optimus/patches/`` — one-shot DocType migration scripts; their
    literal-key uses are by design (cleaning up specific legacy keys
    by name).
  * ``optimus/redis_keys.py`` itself — the f-string definitions inside
    the builder bodies ARE the canonical strings.

The session.py and line_profile/capture.py modules carry their own
pre-v0.12.0 centralized helpers (``_active_key``, ``_meta_key`` etc.)
that internal callers use; the audit doesn't flag them because the
helpers are function returns, NOT ``frappe.cache.X(...)`` calls. The
audit only inspects key arguments at the call site.
"""

from __future__ import annotations

import re
from pathlib import Path

from optimus import redis_keys

# Excluded directories — every .py file under these paths is skipped.
EXCLUDED_DIRS = (
	"optimus/tests/",
	"optimus/tests_integration/",
	"optimus/patches/",
)

# Excluded files — the audit's own canonical-string definitions live
# here, so the f-strings inside :mod:`optimus.redis_keys` are legitimate.
EXCLUDED_FILES = (
	"optimus/redis_keys.py",
)

# Pattern matches a ``frappe.cache.<method>(`` call where ``<method>``
# is a key-taking cache operation. Excludes ``frappe.cache.get_redis_connection``
# / ``frappe.cache.make_key`` / similar that don't take a Redis key.
_CACHE_CALL_RE = re.compile(
	r"frappe\.cache\.(set_value|get_value|hset|hget|hgetall|hdel|"
	r"rpush|lpush|llen|lrange|ltrim|sadd|srem|smembers|"
	r"expire_key|delete_value|hkeys|hexists)\("
)

# Pattern matches an inline f-string key argument containing one of
# the Optimus namespace prefixes. The audit flags any call site whose
# line matches both _CACHE_CALL_RE AND _INLINE_KEY_RE.
_INLINE_KEY_RE = re.compile(
	r"""f"(?:profiler:|optimus:|optimus_settings_cached)|"""
	r"""f'(?:profiler:|optimus:|optimus_settings_cached)"""
)


def _repo_root() -> Path:
	"""Resolve the repository root (the ``apps/optimus`` checkout)
	from this test file's location. Works regardless of cwd."""
	return Path(__file__).resolve().parent.parent.parent


def _is_excluded(posix_path: str) -> bool:
	if posix_path in EXCLUDED_FILES:
		return True
	return any(posix_path.startswith(d) for d in EXCLUDED_DIRS)


def _find_orphan_inline_keys() -> list[str]:
	"""Walk every .py under optimus/ outside the exclusion list. For
	each line containing a ``frappe.cache.X(`` call AND an inline
	f-string key on the same line, record an orphan."""
	root = _repo_root()
	orphans: list[str] = []
	for path in sorted((root / "optimus").rglob("*.py")):
		posix = path.relative_to(root).as_posix()
		if _is_excluded(posix):
			continue
		try:
			lines = path.read_text(encoding="utf-8").splitlines()
		except Exception:
			orphans.append(f"{posix}  (could not read)")
			continue
		for i, line in enumerate(lines):
			if not _CACHE_CALL_RE.search(line):
				continue
			# If the key argument is inline on the same line AND it
			# starts with an Optimus namespace prefix, it's an orphan.
			# Multi-line cache calls (where the key is on the next line
			# via a continuation) require the f-string + the prefix to
			# co-occur with the cache call — i.e. the WRITER wrote
			# `frappe.cache.X(f"profiler:..."` inline. If the key was
			# extracted to a previous line via a helper call, this
			# pattern won't match — and that's the desired behaviour.
			if _INLINE_KEY_RE.search(line):
				orphans.append(f"{posix}:{i + 1}  {line.strip()}")
	return orphans


def _parse_documented_patterns(doc_text: str) -> list[str]:
	r"""Extract the key patterns from REDIS-SCHEMA.md's § 2 tables.
	Patterns appear as table-cell entries wrapped in backticks at the
	start of each data row. The regex captures everything between the
	first ``\``` and the matching ``\``` on lines that look like markdown
	table rows."""
	# A table row looks like: ``| `profiler:active:<user>` | string | …``
	# So the first backtick-wrapped token after the opening pipe is the key.
	row_re = re.compile(r"^\|\s*`([^`]+)`\s*\|")
	patterns: list[str] = []
	for line in doc_text.splitlines():
		m = row_re.match(line)
		if m:
			patterns.append(m.group(1).strip())
	return patterns


class TestEveryRedisCallUsesKeyBuilder:
	"""Drift canary: no inline f-string keys inside ``frappe.cache.X(...)``
	calls outside the exclusion list. If this fails, you wrote a
	``frappe.cache.set_value(f"profiler:...", ...)`` call — refactor it
	to call ``optimus.redis_keys.<feature>(...)`` instead (add a builder
	if one doesn't exist)."""

	def test_no_orphan_inline_keys(self):
		orphans = _find_orphan_inline_keys()
		assert not orphans, (
			"frappe.cache calls with inline f-string keys (use "
			"optimus.redis_keys.* instead):\n  " + "\n  ".join(orphans)
		)


class TestKeyBuildersMatchDoc:
	"""Drift canary: ``redis_keys.KEY_PATTERNS`` must equal the patterns
	documented in ``docs/REDIS-SCHEMA.md``. Adding a key without
	documenting it (or documenting a key without a builder) fails
	here."""

	def test_redis_keys_match_documented_schema(self):
		root = _repo_root()
		doc_path = root / "docs" / "REDIS-SCHEMA.md"
		assert doc_path.exists(), f"REDIS-SCHEMA.md missing at {doc_path}"
		doc_text = doc_path.read_text(encoding="utf-8")
		documented = sorted(set(_parse_documented_patterns(doc_text)))
		canonical = sorted(set(redis_keys.KEY_PATTERNS))
		missing_in_doc = sorted(set(canonical) - set(documented))
		missing_in_code = sorted(set(documented) - set(canonical))
		assert documented == canonical, (
			"Drift between redis_keys.KEY_PATTERNS and docs/REDIS-SCHEMA.md:\n"
			f"  in code but not documented:   {missing_in_doc!r}\n"
			f"  in doc but not in code:       {missing_in_code!r}"
		)


class TestSchemaSentinel:
	"""The sentinel write/read pair is idempotent and round-trips
	cleanly through the Frappe-cache stub."""

	def test_write_sentinel_idempotent(self, monkeypatch):
		# Install a tiny frappe.cache stub that stores set_value /
		# get_value in a dict — exercises the real code path without
		# needing a bench.
		import sys
		from types import SimpleNamespace

		store: dict = {}

		class _FakeCache:
			def set_value(self, key, value, expires_in_sec=None):
				store[key] = value

			def get_value(self, key):
				return store.get(key)

		fake_frappe = SimpleNamespace(cache=_FakeCache())
		# Insert into sys.modules so the lazy import inside the helpers
		# sees the fake.
		monkeypatch.setitem(sys.modules, "frappe", fake_frappe)

		from optimus import redis_schema

		redis_schema.write_schema_sentinel()
		redis_schema.write_schema_sentinel()  # idempotent
		assert store.get("optimus:schema_version") == redis_schema.SCHEMA_VERSION


class TestWrapUnwrap:
	"""Versioned-value envelope round-trip + drift detection."""

	def test_wrap_roundtrip_current_version(self):
		from optimus import redis_schema

		payload = {"hello": "world", "count": 42}
		wrapped = redis_schema.wrap_value(payload)
		unwrapped, version = redis_schema.unwrap_value(wrapped)
		assert unwrapped == payload
		assert version == redis_schema.SCHEMA_VERSION

	def test_unwrap_legacy_returns_none_version(self):
		"""A bare dict (pre-v0.12.0 shape) flows through as-is — no
		envelope, no drift."""
		from optimus import redis_schema

		legacy = {"some": "raw_payload"}
		unwrapped, version = redis_schema.unwrap_value(legacy)
		assert unwrapped == legacy
		assert version is None

	def test_unwrap_unknown_version_returns_default(self):
		"""A future schema version → ``default`` + telemetry emit."""
		from optimus import redis_schema, telemetry

		# Snapshot the telemetry buffer before, then assert at least
		# one ``redis.schema_drift`` event landed after.
		telemetry.drain_for_test()  # start clean

		future_value = {"_v": 99, "data": {"future_field": True}}
		unwrapped, version = redis_schema.unwrap_value(
			future_value, expected=1, default="MISSING"
		)
		assert unwrapped == "MISSING"
		assert version == 99

		buffer = telemetry.drain_for_test()
		assert any(rec.get("event_name") == "redis.schema_drift" for rec in buffer), (
			"expected a redis.schema_drift telemetry event; "
			f"buffer contained: {[r.get('event_name') for r in buffer]!r}"
		)

	def test_unwrap_none_value_returns_default(self):
		"""Missing key (``get_value`` returned ``None``) → default."""
		from optimus import redis_schema

		unwrapped, version = redis_schema.unwrap_value(None, default=[])
		assert unwrapped == []
		assert version is None
