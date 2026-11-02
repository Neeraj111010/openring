"""Knowledge search and a handoff through Pipecat's real model service, against a little OpenAI-style server on this
computer: what the model is really sent, turn by turn. Only speech-to-text and the voice are stubs."""

import json
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from pipecat.processors.frame_processor import FrameProcessor

from openring import providers
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import AUTH
from test_call_flow import _handshake


def _chunk(delta, finish=None):
    return {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _tool_call(name, arguments):
    call = {"index": 0, "id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}
    return [_chunk({"role": "assistant", "tool_calls": [call]}), _chunk({}, "tool_calls")]


def _text(words):
    return [_chunk({"role": "assistant", "content": words}), _chunk({}, "stop")]


@pytest.fixture
def model_server():
    """Answers each path's requests from its script, in order, and keeps what it was asked."""
    scripts, asked = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            who = self.path.split("/")[1]
            asked.append((who, body))
            chunks = scripts[who].pop(0) if scripts.get(who) else _text("Okay.")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", scripts, asked
    server.shutdown()


class Stub(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def _wait(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_call_searches_its_files_then_hands_off(monkeypatch, model_server):
    url, scripts, asked = model_server
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake")
    monkeypatch.setattr(providers, "_build_stt", lambda plan: Stub())
    monkeypatch.setattr(providers, "_build_tts", lambda plan: Stub())
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, Store(":memory:")))
    file = client.post("/file", files={"file": ("faq.txt", "Abrimos das 9h às 18h, de segunda a sexta.".encode())},
                       headers=AUTH).json()

    def assistant(name, path, **model):
        return {"name": name, "voice": {"provider": "deepgram", "voiceId": "thalia"},
                "model": {"provider": "custom-llm", "model": "m", "url": f"{url}/{path}",
                          "messages": [{"role": "system", "content": f"You are {name}. {{{{why}}}}"}], **model}}

    faq = {"type": "query", "knowledgeBases": [{"fileIds": [file["id"]]}]}
    tool = {"type": "handoff",
            "destinations": [{"type": "assistant", "assistantName": "Bia", "description": "billing"}],
            "function": {"name": "to_bia", "parameters": {"type": "object", "properties": {"why": {"type": "string"}}}}}
    squad = client.post("/squad", json={"name": "Team", "members": [
        {"assistant": assistant("Ana", "ana", tools=[faq, tool])},
        {"assistant": assistant("Bia", "bia"),
         "assistantOverrides": {"firstMessageMode": "assistant-speaks-first-with-model-generated-message"}},
    ]}, headers=AUTH)
    assert squad.status_code == 201, squad.text
    client.post("/phone-number", json={"number": "+15551234567", "squadId": squad.json()["id"]}, headers=AUTH)

    scripts["ana"] = [_tool_call("knowledge-search", {"query": "abrimos"}),
                      _tool_call("to_bia", {"destination": "Bia", "why": "a refund"})]
    scripts["bia"] = [_text("Hi, this is Bia.")]

    twiml = client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511999990000", "CallSid": "CA1"}).text
    params = {p.get("name"): p.get("value") for p in ET.fromstring(twiml).find("./Connect/Stream").findall("Parameter")}
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        assert _wait(lambda: [who for who, _ in asked].count("bia") >= 1), asked
        time.sleep(0.5)  # for Bia's answer to reach the record
    store = client.app.state.store
    assert _wait(lambda: store.get("calls", params["callId"])["status"] == "ended")

    first, second = [body for who, body in asked if who == "ana"][:2]
    assert {t["function"]["name"] for t in first["tools"]} == {"knowledge-search", "to_bia"}
    results = [m for m in second["messages"] if m["role"] == "tool"]
    assert "9h às 18h" in results[0]["content"]  # what the search found reached the model

    bia = next(body for who, body in asked if who == "bia")
    assert bia["messages"][0] == {"role": "system", "content": "You are Bia. a refund"}  # its prompt, with the argument
    assert not bia.get("tools")  # its own tools, not Ana's
    calls = [c["function"]["name"] for m in bia["messages"] for c in m.get("tool_calls") or []]
    assert calls == ["knowledge-search"]  # it sees the search, not the handoff

    call = store.get("calls", params["callId"])
    assert [(h["from"], h["to"]) for h in call["handoffs"]] == [("Ana", "Bia")]
    assert "providerFallbacks" not in call  # a handoff is not a provider failing
    assert any(m.get("message") == "Hi, this is Bia." for m in call["messages"])
    names = [c["function"]["name"] for m in call["messages"] if m["role"] == "tool_calls" for c in m["toolCalls"]]
    assert names == ["knowledge-search", "to_bia"]  # the record keeps it all
