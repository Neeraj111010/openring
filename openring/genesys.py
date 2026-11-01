"""Genesys Cloud: calls reach an assistant through an Audio Connector, over the AudioHook protocol.

Genesys opens a WebSocket to `/ws/genesys` with the integration's API key in `X-API-KEY` and an HTTP message
signature (`Signature-Input` / `Signature`, HMAC-SHA256 keyed with the base64-decoded client secret). This
module checks that signature; Pipecat's `GenesysAudioHookSerializer` speaks the protocol itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from collections.abc import Mapping

CLOCK_SKEW = 30  # seconds of clock difference tolerated on `created` and `expires`
MAX_AGE = 30  # seconds a signature stays good after `created`; Genesys signs right before connecting
# What the signature must cover, as in Genesys's reference server: this endpoint, this host, this session.
REQUIRED = ("@request-target", "@authority", "audiohook-organization-id", "audiohook-session-id",
            "audiohook-correlation-id", "x-api-key")
_STRING = r'"(?:[^"\\]|\\.)*"'
_PARAM = re.compile(rf";([a-z*][a-z0-9_\-.*]*)=({_STRING}|-?\d+)")


def number(value: str | None) -> str:
    """A participant's `ani` / `dnis` without its `tel:` or `sip:` scheme."""
    return re.sub(r"^(tel|sip):", "", str(value or "").strip(), flags=re.I)


def _unquote(raw: str) -> str:
    return re.sub(r"\\(.)", r"\1", raw[1:-1]) if raw.startswith('"') else raw


def _members(field: str) -> dict[str, str]:
    """The members of a structured-field dictionary (RFC 8941), label -> raw value. Commas in quotes or
    parentheses don't split."""
    members, part, depth, quoted, escaped = [], "", 0, False, False
    for ch in field:
        if quoted:
            quoted = escaped or ch != '"'
            escaped = not escaped and ch == "\\"
        elif ch == '"':
            quoted = True
        elif ch in "()":
            depth += 1 if ch == "(" else -1
        elif ch == "," and depth == 0:
            members.append(part)
            part = ""
            continue
        part += ch
    members.append(part)
    out = {}
    for member in members:
        label, sep, value = member.strip().partition("=")
        if sep:
            out[label] = value.strip()
    return out


def check_signature(
    headers: Mapping[str, str], target: str, authority: str, *, api_key: str, client_secret: str,
    now: float | None = None,
) -> str | None:
    """Why this connection request is not signed by Genesys with our client secret, or None when it is.
    `headers` has lowercase names; `target` is the request path with its query string; `authority` the host
    Genesys connected to (PUBLIC_URL's, since a tunnel may rewrite the Host header)."""
    try:
        key = base64.b64decode(client_secret, validate=True)
    except (binascii.Error, ValueError):
        return "GENESYS_CLIENT_SECRET is not base64"
    inputs, signatures = _members(headers.get("signature-input", "")), _members(headers.get("signature", ""))
    if not inputs or not signatures:
        return "the request is not signed"
    label, raw = next(iter(inputs.items()))
    shape = re.fullmatch(r"\(([^)]*)\)((?:;.*)?)", raw)
    sig = re.fullmatch(r":([A-Za-z0-9+/=]*):", signatures.get(label, ""))
    if not shape or not sig:
        return "malformed signature headers"
    params = {k: _unquote(v) for k, v in _PARAM.findall(shape.group(2))}
    if params.get("alg", "hmac-sha256") != "hmac-sha256":
        return f"unsupported signature algorithm {params['alg']}"
    if not hmac.compare_digest(params.get("keyid", "").encode(), api_key.encode()):
        return "the signature is for another API key"
    now = time.time() if now is None else now
    try:
        created = int(params["created"]) if params.get("created") else None
        expires = int(params["expires"]) if params.get("expires") else None
    except ValueError:  # text, or a number too long to read
        return "malformed signature times"
    if created is None:
        return "the signature has no creation time"
    if created > now + CLOCK_SKEW:
        return "the signature is from the future; check this server's clock"
    if created + MAX_AGE < now - CLOCK_SKEW or (expires is not None and expires < now - CLOCK_SKEW):
        return "the signature expired; check this server's clock"

    path, _, query = target.partition("?")
    derived = {
        "@request-target": target,
        "@authority": authority.lower(),
        "@method": "GET",
        "@path": path,
        "@query": "?" + query,
    }
    covered = [_unquote(c) for c in re.findall(_STRING, shape.group(1))]
    if missing := [name for name in REQUIRED if name not in covered]:
        return f"the signature does not cover {', '.join(missing)}"
    lines = []
    for name in covered:
        value = derived.get(name) if name.startswith("@") else headers.get(name)
        if value is None:
            return f"the signature covers {name}, which this request lacks"
        lines.append(f'"{name}": {value.strip()}')
    # The signature base ends with the signature's own parameters, exactly as Genesys sent them.
    lines.append(f'"@signature-params": {raw}')
    expected = hmac.new(key, "\n".join(lines).encode(), hashlib.sha256).digest()
    try:
        given = base64.b64decode(sig.group(1))
    except (binascii.Error, ValueError):
        return "malformed signature"
    if not hmac.compare_digest(expected, given):
        return "the signature does not match; check the client secret"
    return None
