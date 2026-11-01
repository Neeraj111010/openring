"""End-to-end call lifecycle over a simulated Twilio media stream.

Providers point at fake keys, so they fail to connect; the test checks that the
call is still tracked from start to finish and the transcript is saved. The other
tests swap the pipeline for a stub to check which media streams are accepted.
"""

import json
import time
import xml.etree.ElementTree as ET

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH


def _answered_call(monkeypatch):
    """A server with one assistant on +15551234567 whose Twilio webhook already answered a call."""
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH)
    twiml = client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511", "CallSid": "CA1"}).text
    params = {p.get("name"): p.get("value") for p in ET.fromstring(twiml).find("./Connect/Stream").findall("Parameter")}
    return client, store, params


def _handshake(ws, params):
    ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
        "streamSid": "MZ1", "callSid": "CA1", "accountSid": "AC1", "customParameters": params,
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))


def _close_code(client, params) -> int:
    """Open a media stream the way Twilio does and return the code the server closes it with."""
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


@pytest.fixture
def fake_bot(monkeypatch):
    """Swap the live pipeline for a stub that records each call it is asked to run."""
    runs = []

    async def run_call(websocket, *, plan, call_id, **_):
        runs.append((call_id, plan))
        await websocket.close()

    # The stub gets the raw websocket instead of a Pipecat transport, so it can hang up.
    monkeypatch.setattr("openring.bot.twilio_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    return runs


def _pipeline_started(monkeypatch):
    """Records the server messages a call sends; the first one means its pipeline is running."""
    import httpx

    from openring import server_messages

    sent = []

    async def record(server, payload, **_kw):
        sent.append(payload["message"])
        return httpx.Response(200)

    monkeypatch.setattr(server_messages, "post", record)  # also keeps the tests off the network
    return sent


def test_call_lifecycle(monkeypatch):
    sent = _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)

    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(150):  # the first Pipecat import in a process takes a few seconds
            if sent:
                break
            time.sleep(0.1)
        assert store.get("calls", params["callId"])["status"] == "in-progress"
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))

    call = _wait_until_ended(store, params["callId"])
    assert call["status"] == "ended"
    assert call["messages"][0] == {"role": "assistant", "message": ASSISTANT["firstMessage"]}
    assert call["artifact"]["messages"] == call["messages"]
    assert call["artifact"]["performanceMetrics"]["turnLatencies"] == []  # nobody spoke
    assert set(call["costBreakdown"]) == {"llmPromptTokens", "llmCompletionTokens", "ttsCharacters",
                                          "transcriberAudioSeconds", "stt", "llm", "tts", "total"}
    assert call["cost"] == call["costBreakdown"]["total"]


def test_media_stream_is_single_use(monkeypatch, fake_bot):
    client, _, params = _answered_call(monkeypatch)
    assert _close_code(client, params) == 1000
    assert _close_code(client, params) == 1008  # a second connection for the same call
    assert [call_id for call_id, _ in fake_bot] == [params["callId"]]


def test_media_stream_uses_the_calls_assistant(monkeypatch, fake_bot):
    client, _, params = _answered_call(monkeypatch)
    other = client.post("/assistant", json={**ASSISTANT, "firstMessage": "Not me"}, headers=AUTH).json()
    _close_code(client, {**params, "assistantId": other["id"]})
    assert fake_bot[0][1].first_message == ASSISTANT["firstMessage"]


def test_media_stream_fills_the_calls_variables(monkeypatch, fake_bot):
    client, store, params = _answered_call(monkeypatch)
    client.patch(f"/assistant/{params['assistantId']}", json={"firstMessage": "Oi {{customer.name}}, {{motivo}}."},
                 headers=AUTH)
    store.update("calls", params["callId"], {"customer": {"number": "+5511", "name": "Maria"}, "assistantOverrides": {
        "variableValues": {"motivo": "sua consulta é amanhã"}}})
    _close_code(client, params)
    assert fake_bot[0][1].first_message == "Oi Maria, sua consulta é amanhã."


def test_media_stream_refuses_unknown_call_and_silence(monkeypatch, fake_bot):
    client, _, params = _answered_call(monkeypatch)
    assert _close_code(client, {**params, "callId": "made-up"}) == 1008

    monkeypatch.setattr("openring.api.HANDSHAKE_TIMEOUT", 0.2)
    with client.websocket_connect("/ws/twilio") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()  # never sends the handshake
    assert closed.value.code == 1008
    assert fake_bot == []


def _wait_until_ended(store, call_id, seconds=20):
    for _ in range(seconds * 10):
        call = store.get("calls", call_id)
        if call["status"] == "ended":
            return call
        time.sleep(0.1)
    return call


def test_caller_who_hangs_up_at_once_ends_the_call(monkeypatch):
    """Hanging up while the services are still being built used to leave the call 'in-progress' for good."""
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    call = _wait_until_ended(store, params["callId"])
    assert (call["status"], call["endedReason"]) == ("ended", "customer-ended-call")


def test_a_provider_that_fails_to_start_ends_the_call(monkeypatch):
    import asyncio

    from openring import bot
    from openring.providers import ProviderError, resolve_plan

    def broken(_plan):
        raise ProviderError("provider 'cartesia' needs an extra package")

    monkeypatch.setattr(bot, "build_services", broken)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    call = store.create("calls", {"type": "webCall", "status": "in-progress"})
    asyncio.run(bot.run_call(object(), plan=resolve_plan(ASSISTANT), call_id=call["id"], store=store))
    call = store.get("calls", call["id"])
    assert call["status"] == "ended" and call["endedReason"].startswith("pipeline-error: provider 'cartesia'")
    assert call["messages"] == []
