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


def test_a_provider_that_stops_working_mid_call_ends_the_call_with_the_reason(monkeypatch):
    """A rejected key (or no credit, or an outage) left the caller in silence until they hung up, and the call was
    recorded as 'customer-ended-call', a success."""
    from pipecat.frames.frames import StartFrame
    from pipecat.processors.frame_processor import FrameProcessor

    from openring import bot

    class RejectedTranscriber(FrameProcessor):
        openring_price = ("transcriber", "deepgram", "nova-3", "pt-BR")
        openring_label = "deepgram nova-3"

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):  # as Deepgram answers a bad key: right when the call starts
                await self.push_error("HTTP 401 (bad.py:12): Invalid credentials", force_treat_as_permanent=True)

    real = bot.build_services
    monkeypatch.setattr(bot, "build_services", lambda plan: (RejectedTranscriber(), *real(plan)[1:]))
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])  # the caller stays on the line
    assert call["status"] == "ended"
    assert call["endedReason"] == ("pipeline-error: the transcriber (deepgram nova-3) stopped working: "
                                   "HTTP 401: Invalid credentials")


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


@pytest.mark.parametrize("limits, reason", [((600, 1.0), "silence-timed-out"), ((1.0, 0), "exceeded-max-duration")])
def test_a_call_ends_itself_when_the_caller_is_silent_or_it_runs_too_long(monkeypatch, limits, reason):
    """Vapi's silenceTimeoutSeconds and maxDurationSeconds: the call hangs up by itself, with Vapi's endedReason
    (made a second long here; the dashboard and the API keep them at 10 s or more)."""
    from pipecat.processors.frame_processor import FrameProcessor

    from openring import bot

    class Quiet(FrameProcessor):  # a provider that passes everything along: nobody ever speaks
        def __init__(self, layer):
            super().__init__()
            self.openring_label = layer
            self.openring_price = (layer, "none", None, None)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    real = bot.CallLimits
    monkeypatch.setattr(bot, "CallLimits", lambda _duration, _silence, now: real(*limits, now))
    monkeypatch.setattr(bot, "build_services", lambda plan: (Quiet("transcriber"), Quiet("model"), Quiet("voice")))
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    started = time.monotonic()
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])  # the caller stays on the line
    assert call["endedReason"] == reason
    assert time.monotonic() - started < 10


def _scripted_call(monkeypatch, limits, on_start=(), frames=None):
    """A call through stub providers: the transcriber pushes `on_start` when the call starts, and the voice "says"
    each sentence at once, recording it (and its frame, in `frames`). Returns (client, store, params, sentences)."""
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, StartFrame, TTSSpeakFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    from openring import bot

    said = []

    class Stub(FrameProcessor):
        def __init__(self, layer):
            super().__init__()
            self.layer, self.openring_label, self.openring_price = layer, layer, (layer, "none", None, None)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if self.layer == "transcriber" and isinstance(frame, StartFrame):
                for early in on_start:
                    await self.push_frame(early)
            if self.layer == "voice" and isinstance(frame, TTSSpeakFrame):
                said.append(frame.text)
                if frames is not None:
                    frames.append(frame)
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    real = bot.CallLimits
    monkeypatch.setattr(bot, "CallLimits", lambda _duration, _silence, now: real(*limits, now))
    monkeypatch.setattr(bot, "build_services", lambda plan: (Stub("transcriber"), Stub("model"), Stub("voice")))
    _pipeline_started(monkeypatch)
    return (*_answered_call(monkeypatch), said)


def test_a_caller_who_is_talking_is_not_hung_up_on_for_silence(monkeypatch):
    from pipecat.frames.frames import UserStartedSpeakingFrame

    client, store, params, _said = _scripted_call(monkeypatch, (2.0, 0.5), on_start=[UserStartedSpeakingFrame()])
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "exceeded-max-duration"  # the silence limit (0.5 s) never fired


def test_a_quiet_caller_hears_the_idle_message_then_the_call_ends(monkeypatch):
    """Vapi's messagePlan: after the assistant speaks, a quiet caller hears an idle message, up to the limit;
    the end-call message comes last."""
    from openring import providers

    monkeypatch.setattr(providers, "IDLE_LIMITS", (("idleTimeoutSeconds", 0.1, 60),
                                                   ("idleMessageMaxSpokenCount", 1, 10)))
    client, store, params, said = _scripted_call(monkeypatch, (600, 2.0))
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {
        "endCallMessage": "Goodbye.",
        "messagePlan": {"idleMessages": ["Still there?"], "idleTimeoutSeconds": 0.2, "idleMessageMaxSpokenCount": 2}})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "silence-timed-out"
    assert said == [ASSISTANT["firstMessage"], "Still there?", "Still there?", "Goodbye."]


def test_a_voicemail_gets_the_message_and_the_call_hangs_up(monkeypatch):
    frames = []
    client, store, params, said = _scripted_call(monkeypatch, (600, 30), frames=frames)
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {"voicemailMessage": "Hi {{customer.name}}, please call us back."})
    store.update("calls", params["callId"], {"answeredBy": "machine_end_beep", "customer": {"name": "Maria"}})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "voicemail"
    assert said == ["Hi Maria, please call us back."]  # not the greeting
    assert frames[0].interruptible is False  # the beep or noise on the line must not cut it off
    assert [m["message"] for m in call["messages"]] == said
