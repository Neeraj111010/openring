"""Telnyx helpers: webhook signatures and TeXML that streams a call's audio to OpenRing.

A Telnyx number answers through a TeXML Application whose voice URL is `$PUBLIC_URL/telnyx/voice`.
OpenRing answers with TeXML (Telnyx's TwiML-like XML) that connects the call to `/ws/telnyx`.
"""

from __future__ import annotations

import base64
import time
from xml.sax.saxutils import quoteattr

# Telnyx signs each webhook with Ed25519 over "<timestamp>|<raw body>". Older timestamps are replays.
SIGNATURE_TOLERANCE = 300  # seconds


def is_valid_signature(public_key: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None,
                       tolerance: int = SIGNATURE_TOLERANCE) -> bool:
    """Check the telnyx-signature-ed25519 and telnyx-timestamp headers against the account's public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        sent_at = int(timestamp)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        raw_signature = base64.b64decode(signature)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - sent_at) > tolerance:
        return False
    try:
        key.verify(raw_signature, timestamp.encode() + b"|" + body)
    except InvalidSignature:
        return False
    return True


def stream_texml(ws_url: str) -> str:
    """Connect the call's audio, both ways, to OpenRing's media websocket."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url={quoteattr(ws_url)} bidirectionalMode="rtp" /></Connect></Response>'
    )
