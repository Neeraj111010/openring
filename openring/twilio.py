"""Twilio helpers: request signature validation and TwiML for media streams."""

from __future__ import annotations

import base64
import hashlib
import hmac
from xml.sax.saxutils import escape, quoteattr


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature or "")


def stream_twiml(ws_url: str, parameters: dict[str, str]) -> str:
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v)} />" for k, v in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"
    )


def say_twiml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(text)}</Say><Hangup/></Response>'
