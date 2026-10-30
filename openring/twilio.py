"""Twilio helpers: request signature validation, TwiML for media streams, placing calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

import httpx


class TwilioError(Exception):
    """Twilio refused a request; the message is Twilio's own explanation."""


async def create_call(
    account_sid: str, auth_token: str, *, to: str, from_: str, url: str, status_callback: str
) -> str:
    """Start an outbound call. Twilio fetches `url` for instructions once it is answered. Returns the CallSid."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json",
            auth=(account_sid, auth_token),
            # Twilio requests both URLs with POST by default; trial accounts refuse the Method parameters.
            data={"To": to, "From": from_, "Url": url, "StatusCallback": status_callback},
        )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        raise TwilioError(f"{body.get('message') or r.text} (Twilio error {body.get('code', r.status_code)})")
    return body["sid"]


async def redirect_call(account_sid: str, auth_token: str, call_sid: str, twiml: str) -> None:
    """Give a live call new instructions; its media stream ends and Twilio follows `twiml`."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json",
            auth=(account_sid, auth_token),
            data={"Twiml": twiml},
        )
    if r.status_code >= 400:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        raise TwilioError(f"{body.get('message') or r.text} (Twilio error {body.get('code', r.status_code)})")


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    # Twilio signs some requests with the default port in the URL (":443") and some without, so check
    # both forms, as Twilio's own SDKs do.
    parts = urlsplit(url)
    host = parts.netloc.rsplit(":", 1)[0] if parts.port else parts.netloc
    port = parts.port or (443 if parts.scheme == "https" else 80)
    variants = {urlunsplit(parts._replace(netloc=host)), urlunsplit(parts._replace(netloc=f"{host}:{port}"))}
    return any(hmac.compare_digest(compute_signature(auth_token, u, params), signature or "") for u in variants)


def stream_twiml(ws_url: str, parameters: dict[str, str]) -> str:
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v)} />" for k, v in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"
    )


def dial_twiml(number: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial>{escape(number)}</Dial></Response>'


def say_twiml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(text)}</Say><Hangup/></Response>'
