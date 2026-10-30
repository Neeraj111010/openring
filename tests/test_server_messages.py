"""Vapi's server messages: status-update, conversation-update, end-of-call-report, assistant-request."""

import asyncio
import json
import time
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from openring import server_messages
from openring.api import create_app
from openring.config import Settings
from openring.server_messages import AssistantRequestError, Outbox, request_assistant, server_of, wants
from openring.store import Store

from test_api import ASSISTANT, AUTH

SERVER = {"url": "https://hooks.example.com/vapi", "secret": "s3cret", "headers": {"X-Team": "a"}}


def test_server_of_reads_vapis_server_and_older_server_url():
    assert server_of({"server": SERVER}) == SERVER
    assert server_of({"serverUrl": "https://old.example.com", "serverUrlSecret": "x"}) == {
        "url": "https://old.example.com", "secret": "x"}
    assert server_of({}) is None and server_of(None) is None


def test_assistant_chooses_its_messages():
    assert wants({}, "status-update") and wants({}, "end-of-call-report")
    assert not wants({"serverMessages": ["end-of-call-report"]}, "status-update")
    assert not wants({"serverMessages": []}, "end-of-call-report")


def capture(monkeypatch, delays=None):
    """Swap the HTTP post for a recorder; `delays` holds a delay per message type to test ordering."""
    sent = []

    async def fake_post(server, payload, **_kw):
        kind = payload["message"]["type"]
        await asyncio.sleep((delays or {}).get(kind, 0))
        sent.append((server["url"], payload["message"]))
        return httpx.Response(200)

    monkeypatch.setattr(server_messages, "post", fake_post)
    return sent


def test_outbox_delivers_in_order_without_holding_the_call(monkeypatch):
    sent = capture(monkeypatch, delays={"status-update": 0.2})
    assistant = {"server": SERVER}

    async def scenario():
        box = Outbox(assistant, lambda: {"id": "c1"})
        started = time.monotonic()
        box.send("status-update", status="in-progress")
        box.send("conversation-update", messages=[{"role": "user", "message": "hi"}])
        assert time.monotonic() - started < 0.1  # sending never waits for the server
        await box.flush()

    asyncio.run(scenario())
    assert [m["type"] for _, m in sent] == ["status-update", "conversation-update"]
    assert sent[0][1]["status"] == "in-progress" and sent[0][1]["call"] == {"id": "c1"}
    assert sent[1][1]["messages"] == [{"role": "user", "message": "hi"}]


def test_outbox_skips_what_the_assistant_did_not_ask_for(monkeypatch):
    sent = capture(monkeypatch)

    async def scenario():
        box = Outbox({"server": SERVER, "serverMessages": ["end-of-call-report"]}, lambda: {})
        box.send("status-update", status="in-progress")
        await box.flush()
        box = Outbox({}, lambda: {})  # no server at all
        box.send("status-update", status="in-progress")
        await box.flush()

    asyncio.run(scenario())
    assert sent == []


def test_send_uses_the_servers_headers_and_secret():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await server_messages.post(SERVER, server_messages.message("status-update", {"id": "c1"}, status="ended"),
                                   client=client)

    asyncio.run(scenario())
    assert seen[0].headers["X-Vapi-Secret"] == "s3cret" and seen[0].headers["X-Team"] == "a"
    body = json.loads(seen[0].content)["message"]
    assert body["type"] == "status-update" and body["status"] == "ended" and body["timestamp"] > 0


def ask(responder):
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        return await request_assistant(SERVER, {"id": "c1", "customer": {"number": "+5511"}}, {"number": "+1555"},
                                       client=client)

    return asyncio.run(scenario())


def test_assistant_request_answers():
    seen = []

    def by_id(request):
        seen.append(json.loads(request.content)["message"])
        return httpx.Response(200, json={"assistantId": "a-1"})

    assert ask(by_id) == {"assistantId": "a-1"}
    assert seen[0]["type"] == "assistant-request" and seen[0]["phoneNumber"] == {"number": "+1555"}
    assert seen[0]["customer"] == {"number": "+5511"}
    transient = {"name": "Night shift", "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}}
    assert ask(lambda r: httpx.Response(200, json={"assistant": transient})) == {"assistant": transient}
    for responder, reason in [
        (lambda r: httpx.Response(200, json={"error": "closed today"}), "assistant-request-returned-error"),
        (lambda r: httpx.Response(200, text="not json"), "assistant-request-returned-invalid-json"),
        (lambda r: httpx.Response(200, json={}), "assistant-request-returned-no-assistant"),
        (lambda r: httpx.Response(500), "assistant-request-failed"),
    ]:
        with pytest.raises(AssistantRequestError) as err:
            ask(responder)
        assert err.value.ended_reason == reason


def inbound_app(monkeypatch, number_fields):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", **number_fields}, headers=AUTH)
    return client, store, a


def _ring(client):
    twiml = client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511999", "CallSid": "CA1"}).text
    root = ET.fromstring(twiml)
    stream = root.find("./Connect/Stream")
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")} if stream is not None else None
    return root, params


def test_inbound_call_asks_the_numbers_server_for_an_assistant(monkeypatch):
    client, store, a = inbound_app(monkeypatch, {"server": SERVER})
    asked = []

    async def fake_request(server, call, number):
        asked.append((server["url"], call["customer"], number["number"]))
        return {"assistantId": a["id"]}

    monkeypatch.setattr("openring.api.request_assistant", fake_request)
    _, params = _ring(client)
    assert asked == [(SERVER["url"], {"number": "+5511999"}, "+15551234567")]
    assert params["assistantId"] == a["id"]
    assert store.get("calls", params["callId"])["assistantId"] == a["id"]


def test_inbound_call_with_a_transient_assistant(monkeypatch):
    client, store, _ = inbound_app(monkeypatch, {"server": SERVER})
    transient = {**ASSISTANT, "name": "Night shift"}

    async def fake_request(*_a):
        return {"assistant": transient}

    monkeypatch.setattr("openring.api.request_assistant", fake_request)
    _, params = _ring(client)
    assert set(params) == {"callId"}  # nothing to look up: the assistant lives in the call record
    call = store.get("calls", params["callId"])
    assert call["assistant"]["name"] == "Night shift" and call["assistantId"] is None


def test_inbound_call_when_the_server_cannot_say(monkeypatch):
    client, store, _ = inbound_app(monkeypatch, {"server": SERVER})

    async def failing(*_a):
        raise AssistantRequestError("assistant-request-returned-error", "closed today")

    monkeypatch.setattr("openring.api.request_assistant", failing)
    root, params = _ring(client)
    assert params is None and root.find("./Say") is not None
    call = store.list("calls")[0]
    assert (call["status"], call["endedReason"]) == ("ended", "assistant-request-returned-error")

    async def unknown(*_a):
        return {"assistantId": "nope"}

    monkeypatch.setattr("openring.api.request_assistant", unknown)
    _ring(client)
    assert store.list("calls")[0]["endedReason"] == "assistant-request-returned-unknown-assistant"


def test_number_with_an_assistant_never_asks(monkeypatch):
    client, _, a = inbound_app(monkeypatch, {"server": SERVER})
    client.patch(f"/phone-number/{client.get('/phone-number', headers=AUTH).json()[0]['id']}",
                 json={"assistantId": a["id"]}, headers=AUTH)

    async def must_not_ask(*_a):
        raise AssertionError("asked the server although the number has an assistant")

    monkeypatch.setattr("openring.api.request_assistant", must_not_ask)
    _, params = _ring(client)
    assert params["assistantId"] == a["id"]


def test_outbound_call_with_a_transient_assistant(monkeypatch):
    from openring import twilio

    async def create_call(*_a, **_kw):
        return "CA-out-1"

    monkeypatch.setattr(twilio, "create_call", create_call)
    client, store, _ = inbound_app(monkeypatch, {})
    n = client.get("/phone-number", headers=AUTH).json()[0]
    settings_client = TestClient(client.app)
    client.app.state.settings.twilio_account_sid = "AC1"
    client.app.state.settings.twilio_auth_token = "tok"
    body = {"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"},
            "assistant": {**ASSISTANT, "name": "One-off"}}
    r = settings_client.post("/call", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["assistant"]["name"] == "One-off" and "assistantId" not in call
    bad = {**body, "assistant": {"model": {"provider": "nope"}}}
    assert settings_client.post("/call", json=bad, headers=AUTH).status_code == 400
    # Answered: the stream carries only the call id.
    twiml = settings_client.post(f"/twilio/voice?callId={call['id']}", data={"CallSid": "CA-out-1"}).text
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    assert {p.get("name") for p in stream.findall("Parameter")} == {"callId"}


def test_a_call_reports_its_status_to_the_assistants_server(monkeypatch):
    sent = capture(monkeypatch)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json={**ASSISTANT, "server": SERVER}, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH)
    _, params = _ring(client)

    with client.websocket_connect("/ws/twilio") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "accountSid": "AC1", "customParameters": params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        for _ in range(150):  # until the pipeline runs and says so (the first Pipecat import is slow)
            if sent:
                break
            time.sleep(0.1)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))

    for _ in range(100):
        if any(m["type"] == "end-of-call-report" for _, m in sent):
            break
        time.sleep(0.1)
    kinds = [(m["type"], m.get("status")) for _, m in sent if m["type"] != "conversation-update"]
    assert kinds == [("status-update", "in-progress"), ("status-update", "ended"), ("end-of-call-report", None)]
    report = sent[-1][1]
    assert report["endedReason"] == "customer-ended-call" and report["call"]["id"] == params["callId"]
    assert report["artifact"]["messages"][0]["message"] == ASSISTANT["firstMessage"]
    assert all(url == SERVER["url"] for url, _ in sent)
