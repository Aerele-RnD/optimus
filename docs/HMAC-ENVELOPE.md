# `sign_blob` / `unsign_blob` — HMAC envelope versioning

`optimus.session.sign_blob` / `unsign_blob` wrap every opaque payload
(currently pickled pyinstrument trees, but the API is bytes-in,
bytes-out) with a 32-byte HMAC-SHA256 signature so a Redis-poisoning
attacker can't slip a malicious pickle in and trigger a deserialization
RCE.

v0.12.14 added a 1-byte **scheme version** marker between the signature
and the payload as the extension point for future signing-scheme bumps.
This document is the design specification for future schemes, the
compatibility matrix between them, and the canary tests that lock the
contract.

## Current shape (scheme v1, v0.12.14+)

```
[32-byte HMAC-SHA256(\x01 + payload)] + [\x01] + [payload bytes]
└────────────── 32 ──────────────────┘   └ 1 ┘   └── len(payload) ─┘
```

The HMAC covers the 1-byte version marker AND the payload, so tampering
with the marker is detected (turns into an HMAC mismatch → `unsign_blob`
returns `None`).

## Pre-v0.12.14 shape (scheme v0, legacy)

```
[32-byte HMAC-SHA256(payload)] + [payload bytes]
└────────── 32 ──────────────┘   └── len(payload) ─┘
```

No version marker. The HMAC covers only the payload.

## Backward-compat read path

`unsign_blob` (v0.12.14+) does ONE HMAC verification step then
disambiguates on the body's first byte:

* HMAC verifies AND `body[0] == \x01` → v1 blob; strip marker, return
  `body[1:]`.
* HMAC verifies AND `body[0] != \x01` → v0 legacy blob; return `body`.
* HMAC mismatch → return `None`.

The single HMAC step handles both shapes correctly because for v1 the
HMAC was computed over `\x01 + payload` AND that's exactly what the
body is; for v0 the HMAC was computed over the payload AND the body
IS the payload.

### Why `\x01` for the marker

Pickle (the only producer of payloads for `sign_blob` today) never uses
`\x01` as a leading opcode:

* Pickle protocols 2-5 start with `\x80` (PROTO opcode).
* Pickle protocol 0 starts with printable ASCII opcodes (`(`, `c`,
  `[`, etc.).

So a legacy pre-v0.12.14 pickle payload's first byte can never
accidentally trigger the v1 strip branch in `unsign_blob`. Future
producers (msgpack, JSON, raw binary) need to avoid `\x01` as a leading
byte — see the producer compatibility table below.

### Producer compatibility table

| Producer | First-byte set | Safe for v0 detection? |
|---|---|---|
| Pickle protocol 0 | Printable ASCII opcodes | Yes |
| Pickle protocol 2-5 | `\x80` | Yes |
| msgpack (positive fixint) | `\x00`-`\x7f` — **collides with `\x01`** | NO (write through v1 path) |
| msgpack (map fixmap) | `\x80`-`\x8f` | Yes |
| JSON (top-level dict) | `{` (`\x7b`) | Yes |
| JSON (top-level list) | `[` (`\x5b`) | Yes |
| Raw bytes | arbitrary | NO if `\x01` is allowed |

If a new producer needs leading `\x01` capability, it MUST be wrapped
in v1 (or later) and never run through the v0 legacy code path. Today
that's just pickle, which never collides.

## Future schemes (extension points)

### Scheme v2 — HMAC-SHA512 (hypothetical)

**When to bump**: a credible cryptanalytic finding against SHA-256 in
the HMAC-style construction, OR a customer regulatory requirement
(FIPS 140-3 compliance, post-quantum readiness).

```
[64-byte HMAC-SHA512(\x02 + payload)] + [\x02] + [payload bytes]
└────────────── 64 ──────────────────┘   └ 1 ┘   └── len(payload) ─┘
```

**Sig length changes from 32 → 64.** This breaks the prefix-length
assumption in `unsign_blob`. Mitigation: read the FIRST BYTE after the
LEGACY 32-byte prefix; if it's `\x01` (v1) or absent-looking (v0), use
the 32-byte verify. If the first byte is `\x02`, peek the FIRST BYTE
after a 64-byte prefix and verify against SHA-512. Both verifies are
fast (sub-microsecond); the two-attempt design preserves backward
compat with both v0 and v1 blobs.

**Implementation sketch**:
```python
_HMAC_SCHEME_V1 = 0x01
_HMAC_SCHEME_V2 = 0x02

def unsign_blob(signed):
    # v0 / v1 path (32-byte SHA-256 sig)
    if len(signed) >= 32:
        sig32, body32 = signed[:32], signed[32:]
        expected32 = hmac.new(secret, body32, hashlib.sha256).digest()
        if hmac.compare_digest(sig32, expected32):
            if body32 and body32[0] == _HMAC_SCHEME_V1:
                return body32[1:]
            # v0: the body byte must NOT be a future-scheme marker
            if not body32 or body32[0] not in (_HMAC_SCHEME_V2,):
                return body32
    # v2 path (64-byte SHA-512 sig)
    if len(signed) >= 64:
        sig64, body64 = signed[:64], signed[64:]
        expected64 = hmac.new(secret, body64, hashlib.sha512).digest()
        if hmac.compare_digest(sig64, expected64):
            if body64 and body64[0] == _HMAC_SCHEME_V2:
                return body64[1:]
    return None
```

The `not in (_HMAC_SCHEME_V2,)` guard in the v0 branch prevents an
attacker from crafting a v0-shaped blob whose body happens to start
with `\x02` and trick the reader into accepting an unsigned `\x02 +
malicious_payload` as a v0 blob. (The v0 branch's HMAC over `body` ≠
the v0 HMAC over an attacker-crafted v0 payload, but defence-in-depth
matters.)

### Scheme v3 — AES-SIV authenticated encryption (hypothetical)

**When to bump**: when the operator wants to encrypt the payload at
rest in Redis (not just authenticate it). AES-SIV is the
deterministic-encryption variant that doesn't require a nonce, fitting
the "stash this blob, retrieve later, possibly multiple times" pattern.

```
[32-byte AES-SIV tag] + [\x03] + [AES-SIV-encrypted(payload)]
└────────── 32 ─────┘   └ 1 ┘   └── len(payload) ─┘
```

Same envelope shape as v1 (32-byte prefix + marker + body), but the
body is opaque-encrypted and the prefix is the SIV tag rather than an
HMAC. The reader's verify step is the SIV `decrypt-and-verify` API
which returns `None`/raises on tag mismatch. Same backward-compat
strategy: try v1 first, then v3 (the marker bytes differ).

### Scheme v4+ — key rotation

For key rotation (e.g., the operator rotates `encryption_key` and wants
to migrate without re-signing everything inline), the marker can carry
a key-id bit:

```
[v_marker = 0x04..0x07] [4-bit key_id]
```

Reader inspects the marker's low bits to pick which secret to verify
with. This is the operator-driven extension and requires explicit
support in `_hmac_secret` (return per-key-id secret) — out of scope
for the v0.12.14 baseline.

## Canary tests

`optimus/tests/test_hmac_envelope_versioning.py` (v0.12.14):

* `TestSignUnsignRoundTrip` (3 tests) — v1 round-trip happy path,
  empty payload, pickle-shaped payload.
* `TestLegacyShapeBackwardCompat` (2 tests) — pre-v0.12.14 v0
  blobs still verify, including pickle-shaped legacy blobs (their
  `\x80` first byte is NOT the v1 marker, so they go through the v0
  branch correctly).
* `TestNewShapeByteLayout` (1 test) — locks the byte layout
  (`32 + 1 + len(payload)`).
* `TestTamperingDetection` (3 tests) — tampered sig / version-byte /
  payload all return None.
* `TestEdgeCases` (4 tests) — too-short, non-bytes, sign rejects
  non-bytes, no-secret pass-through.

A future scheme v2 PR would add `TestSchemeV2RoundTrip` +
`TestV0V1V2Coexistence` + `TestV1ReaderRejectsV2Blob` (the v1 reader
must NOT verify a v2 blob's SHA-256 of `body64` — it would fail
trivially because the lengths differ). The existing 13 tests stay
green throughout (v0/v1 compat is forever).

## Operator notes

### Detecting which scheme is in use

The blob shape on disk tells you:

```python
import optimus.session as s

raw = frappe.cache.hget(s._jobs_key(uuid), job_id)  # or any signed blob
if len(raw) < 33:
    scheme = "v0"  # or corrupt
elif raw[32] == 0x01:
    scheme = "v1"
elif raw[32] == 0x02:
    scheme = "v2 (hypothetical)"
else:
    scheme = "v0 (legacy)"
```

### Forcing a re-sign

There's no inline migration; the v0 → v1 transition happens
opportunistically as workers write new blobs after the upgrade. Old
blobs stay valid for read forever (the v0 branch in `unsign_blob` is
permanent compatibility). If an operator wants to force a re-sign
for compliance reasons, they can:

1. Read every signed blob.
2. `unsign_blob` to get the payload.
3. `sign_blob` to get the new-scheme blob.
4. Write back.

The janitor's daily cron could do this automatically (one-time pass
after a scheme bump) but isn't wired up for v1. A future scheme v2
PR should ship the janitor wiring alongside the scheme code.

## Out of scope

* **Nonce-based authenticated encryption** (AES-GCM, ChaCha20-Poly1305)
  — the deterministic-retrieval pattern of these blobs doesn't fit a
  nonce-per-blob design without extra state (a per-key counter, a
  per-payload random nonce written alongside, etc.). AES-SIV (scheme
  v3) is the natural fit.
* **Per-payload-shape versioning** — that's the v0.12.0 `wrap_value`
  envelope's job (see `docs/REDIS-SCHEMA.md`). The HMAC scheme version
  is about the SIGNING scheme; the payload version is about the
  PAYLOAD shape. Both can evolve independently.
* **Asymmetric signatures** (Ed25519, etc.) — the cross-process
  shared-secret model fits HMAC's symmetric design; introducing
  asymmetric keys would mean two separate workflows (signing-key
  process, verifying-key process), which is overkill for the
  intra-bench trust model.

## See also

* `optimus/session.py:sign_blob` / `unsign_blob` — the implementation.
* `optimus/tests/test_hmac_envelope_versioning.py` — the canary
  test suite.
* `docs/REDIS-SCHEMA.md` — the value-shape envelope (different
  concern; lives one layer up).
* CHANGELOG.md v0.12.14 — the rollout entry.
