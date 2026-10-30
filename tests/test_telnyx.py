"""Telnyx: signed TeXML webhooks, the media stream and the dashboard connector."""

import asyncio
import base64
import json
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring import keys, telnyx
from openring.api import create_app
from openring.bot import keep_phone_line_open, telnyx_transport
from openring.config import Settings
from openring.providers import provider_status
from openring.store import Store

from test_api import ASSISTANT, AUTH

PRIVATE = Ed25519PrivateKey.generate()
PUBLIC = base64.b64encode(PRIVATE.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def sign(body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signature = base64.b64encode(PRIVATE.sign(ts.encode() + b"|" + body)).decode()
    return {"telnyx-timestamp": ts, "telnyx-signature-ed25519": signature}


def test_signature_check():
    body = b"From=%2B5511&To=%2B15551234567"
    headers = sign(body)
    ts, sig = headers["telnyx-timestamp"], headers["telnyx-signature-ed25519"]
    assert telnyx.is_valid_signature(PUBLIC, ts, body, sig)
    assert not telnyx.is_valid_signature(PUBLIC, ts, body + b"&x=1", sig)  # body changed
    old = sign(body, timestamp=int(time.time()) - 3600)
    assert not telnyx.is_valid_signature(PUBLIC, old["telnyx-timestamp"], body, old["telnyx-signature-ed25519"])
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)).decode()
    assert not telnyx.is_valid_signature(other, ts, body, sig)
    assert not telnyx.is_valid_signature(PUBLIC, "", body, sig)
    assert not telnyx.is_valid_signature("not base64!", ts, body, "nope")


def test_stream_texml():
    root = ET.fromstring(telnyx.stream_texml("wss://x.test/ws/telnyx?callId=c1&a=b"))
    stream = root.find("./Connect/Stream")
    assert stream.get("url") == "wss://x.test/ws/telnyx?callId=c1&a=b" and stream.get("bidirectionalMode") == "rtp"


def app(monkeypatch, **overrides):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(**{"api_key": "test-key", "public_url": "https://x.test", "db_path": ":memory:",
                           "telnyx_public_key": PUBLIC, "telnyx_api_key": "KEY123", **overrides})
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"], "provider": "telnyx"},
                headers=AUTH)
    return client, store, a


FORM = {"CallSid": "v3:abc", "From": "+5511999990000", "To": "+15551234567", "Direction": "inbound"}


def ring(client, form=FORM, headers=None, sign_it=True):
    body = urlencode(form).encode()
    h = {"content-type": "application/x-www-form-urlencoded", **(sign(body) if sign_it else {}), **(headers or {})}
    return client.post("/telnyx/voice", content=body, headers=h)


def test_incoming_call_streams_to_its_assistant(monkeypatch):
    client, store, a = app(monkeypatch)
    r = ring(client)
    assert r.status_code == 200
    stream = ET.fromstring(r.text).find("./Connect/Stream")
    url = urlsplit(stream.get("url"))
    assert (url.scheme, url.netloc, url.path) == ("wss", "x.test", "/ws/telnyx")
    call = store.get("calls", parse_qs(url.query)["callId"][0])
    assert (call["type"], call["status"], call["assistantId"]) == ("inboundPhoneCall", "queued", a["id"])
    assert (call["phoneCallProvider"], call["phoneCallProviderId"]) == ("telnyx", "v3:abc")
    assert call["customer"] == {"number": "+5511999990000"}


def test_json_webhooks_work_too(monkeypatch):
    client, store, _ = app(monkeypatch)
    body = json.dumps(FORM).encode()
    r = client.post("/telnyx/voice", content=body, headers={"content-type": "application/json", **sign(body)})
    assert r.status_code == 200 and ET.fromstring(r.text).find("./Connect/Stream") is not None


def test_unsigned_or_unverifiable_webhooks_are_refused(monkeypatch):
    client, store, _ = app(monkeypatch)
    assert ring(client, sign_it=False).status_code == 403
    assert ring(client, headers={"telnyx-signature-ed25519": base64.b64encode(b"x" * 64).decode()}).status_code == 403
    no_key, _, _ = app(monkeypatch, telnyx_public_key="")
    assert ring(no_key).status_code == 403
    assert store.list("calls") == []
    trusting, _, _ = app(monkeypatch, telnyx_public_key="", validate_telnyx_signature=False)
    assert ring(trusting, sign_it=False).status_code == 200


def test_number_without_assistant_says_so(monkeypatch):
    client, _, _ = app(monkeypatch)
    r = ring(client, form={**FORM, "To": "+19990000000"})
    assert ET.fromstring(r.text).find("./Say") is not None


@pytest.fixture
def fake_bot(monkeypatch):
    runs = []

    async def run_call(websocket, *, plan, call_id, **_):
        runs.append((call_id, plan))
        await websocket.close()

    monkeypatch.setattr("openring.bot.telnyx_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    return runs


def _connect(client, call_id):
    """Open a media stream the way Telnyx does and return the code the server closes it with."""
    with client.websocket_connect(f"/ws/telnyx?callId={call_id}") as ws:
        ws.send_text(json.dumps({"event": "connected", "version": "1.0.0"}))
        ws.send_text(json.dumps({"event": "start", "sequence_number": "1", "stream_id": "st-1", "start": {
            "call_control_id": "v3:abc", "from": FORM["From"], "to": FORM["To"],
            "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}}}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


def test_media_stream_runs_the_calls_assistant_once(monkeypatch, fake_bot):
    client, store, _ = app(monkeypatch)
    call_id = parse_qs(urlsplit(ET.fromstring(ring(client).text).find("./Connect/Stream").get("url")).query)[
        "callId"][0]
    assert _connect(client, call_id) == 1000
    assert _connect(client, call_id) == 1008  # a second connection for the same call
    assert _connect(client, "made-up") == 1008
    assert [(c, p.first_message) for c, p in fake_bot] == [(call_id, ASSISTANT["firstMessage"])]
    assert store.get("calls", call_id)["status"] == "in-progress"


def test_transport_hangs_up_through_telnyx_unless_transferred(monkeypatch):
    from pipecat.frames.frames import EndFrame

    settings = Settings(telnyx_api_key="KEY123", db_path=":memory:")
    call_data = {"stream_id": "st-1", "call_id": "v3:abc", "outbound_encoding": "PCMU"}
    transport = telnyx_transport(SimpleNamespace(), call_data, settings)
    serializer = transport._params.serializer
    hung_up = []

    async def hang_up():
        hung_up.append(True)

    monkeypatch.setattr(serializer, "_hang_up_call", hang_up)
    asyncio.run(serializer.serialize(EndFrame()))
    assert hung_up == [True]
    transport = telnyx_transport(SimpleNamespace(), call_data, settings)
    keep_phone_line_open(transport)
    monkeypatch.setattr(transport._params.serializer, "_hang_up_call", hang_up)
    asyncio.run(transport._params.serializer.serialize(EndFrame()))
    assert hung_up == [True]  # not hung up again
    # Without an API key OpenRing cannot hang up, so it doesn't try.
    telnyx_transport(SimpleNamespace(), call_data, Settings(db_path=":memory:"))


def test_dashboard_connects_telnyx(monkeypatch, tmp_path):
    assert keys.CONNECTORS["telnyx"]["fields"] == [("TELNYX_API_KEY", "API key"), ("TELNYX_PUBLIC_KEY", "Public key")]
    url, options = keys._check_request("telnyx", {"TELNYX_API_KEY": "KEY123", "TELNYX_PUBLIC_KEY": PUBLIC})
    assert url == "https://api.telnyx.com/v2/balance" and options["headers"]["Authorization"] == "Bearer KEY123"

    rows = {r["id"]: r for r in provider_status(Settings(db_path=":memory:", public_url="https://x.test",
                                                          telnyx_api_key="KEY123", telnyx_public_key=PUBLIC))}
    assert rows["telnyx"]["kind"] == "telephony" and rows["telnyx"]["ready"] is True
    assert provider_status(Settings(db_path=":memory:"))[-1]["ready"] is False


def test_outbound_calls_refuse_telnyx_numbers_for_now(monkeypatch):
    client, _, _ = app(monkeypatch)
    n = client.get("/phone-number", headers=AUTH).json()[0]
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}}, headers=AUTH)
    assert r.status_code == 400 and "Twilio" in r.json()["detail"]
