"""Genesys Cloud calls through an Audio Connector (AudioHook protocol): the connection's signature, the
open handshake, which assistant answers, and how the call goes back to the Architect flow."""

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring import genesys, keys
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH

KEY = "genesys-test-api-key"
SECRET = base64.b64encode(b"a client secret of thirty-two by").decode()
CALLER = {"id": "p-1", "ani": "tel:+5511912345678", "aniName": "Maria", "dnis": "tel:+15551234567"}


def signed(*, key=KEY, secret=SECRET, created=None, session="s-1"):
    """Connection headers as Genesys signs them (HTTP message signature, HMAC-SHA256)."""
    created = int(time.time()) if created is None else created
    covered = {"@request-target": "/ws/genesys", "@authority": "ring.example.com",
               "audiohook-organization-id": "org-1", "audiohook-session-id": session,
               "audiohook-correlation-id": "corr-1", "x-api-key": key}
    params = ("(" + " ".join(f'"{n}"' for n in covered) + ")" + f';keyid="{key}";nonce="VGhpc0lzQVVuaXF1ZU5vbmNl"'
              + f';alg="hmac-sha256";created={created};expires={created + 3600}')
    base = "\n".join(f'"{n}": {v}' for n, v in covered.items()) + f'\n"@signature-params": {params}'
    sig = base64.b64encode(hmac.new(base64.b64decode(secret), base.encode(), hashlib.sha256).digest()).decode()
    return {"x-api-key": key, "audiohook-organization-id": "org-1", "audiohook-session-id": session,
            "audiohook-correlation-id": "corr-1", "signature-input": f"sig1={params}", "signature": f"sig1=:{sig}:"}


def opening(**variables):
    return {"version": "2", "type": "open", "seq": 1, "serverseq": 0, "id": "s-1", "position": "PT0S",
            "parameters": {"organizationId": "org-1", "conversationId": "conv-1", "participant": CALLER,
                           "media": [{"type": "audio", "format": "PCMU", "channels": ["external"], "rate": 8000}],
                           "inputVariables": variables}}


class Runs(list):
    """The calls the stub bot was asked to run; `then(step)` gives it something to do during the call."""

    steps: list

    def then(self, step):
        self.steps.append(step)


@pytest.fixture
def fake_bot(monkeypatch):
    """The live pipeline swapped for a stub that records each call."""
    runs = Runs()
    runs.steps = []

    async def run_call(websocket, *, plan, call_id, transfer=None, **_):
        runs.append((call_id, plan))
        for step in runs.steps:
            await step(websocket, transfer)
        await websocket.close()

    monkeypatch.setattr("openring.bot.genesys_transport", lambda websocket, _serializer: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    return runs


@pytest.fixture
def server(fake_bot):
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://ring.example.com", db_path=":memory:",
                        genesys_api_key=KEY, genesys_client_secret=SECRET)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json={**ASSISTANT, "firstMessage": "Oi {{customer.name}}, sobre {{motivo}}."},
                    headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "provider": "genesys", "assistantId": a["id"]},
                headers=AUTH)
    return client, store, a


def connect(client, message, headers=None):
    """Opens a session as Genesys does; returns the server's replies until it closes the line."""
    replies = []
    with client.websocket_connect("/ws/genesys", headers=headers or signed()) as ws:
        ws.send_text(json.dumps(message))
        try:
            while True:
                replies.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect:
            pass
    return replies


def test_the_signature_example_from_genesys_docs_verifies():
    headers = {
        "audiohook-organization-id": "d7934305-0972-4844-938e-9060eef73d05",
        "audiohook-session-id": "30b0e395-84d3-4570-ac13-9a62d8f514c0",
        "audiohook-correlation-id": "e160e428-53e2-487c-977d-96989bf5c99d",
        "x-api-key": "SGVsbG8sIEkgYW0gdGhlIEFQSSBrZXkh",
        "signature": "sig1=:NZBwyBHRRyRoeLqy1IzOa9VYBuI8TgMFt2GRDkDuJh4=:",
        "signature-input": 'sig1=("@request-target" "@authority" "audiohook-organization-id" "audiohook-session-id" '
                           '"audiohook-correlation-id" "x-api-key");keyid="SGVsbG8sIEkgYW0gdGhlIEFQSSBrZXkh";'
                           'nonce="VGhpc0lzQVVuaXF1ZU5vbmNl";alg="hmac-sha256";created=1641013200;expires=3282026430',
    }

    def check(h=headers, now=1641013205, key="SGVsbG8sIEkgYW0gdGhlIEFQSSBrZXkh",
              secret="TXlTdXBlclNlY3JldEtleVRlbGxOby0xITJAMyM0JDU="):
        return genesys.check_signature(h, "/api/v1/voicebiometrics/ws", "audiohook.example.com", api_key=key,
                                       client_secret=secret, now=now)

    assert check() is None
    assert "does not match" in check(h={**headers, "audiohook-session-id": "another-session"})
    assert "does not match" in check(secret=base64.b64encode(b"wrong secret").decode())
    assert "another API key" in check(key="some-other-key")
    assert "expired" in check(now=1641013200 + 3600)
    assert "future" in check(now=1641013200 - 3600)
    assert "not signed" in check(h={k: v for k, v in headers.items() if not k.startswith("signature")})


def test_connections_without_the_key_or_signature_are_refused(server):
    client, _, _ = server
    for headers in ({}, {**signed(), "x-api-key": "wrong"}, {"x-api-key": KEY}, signed(secret=base64.b64encode(
            b"not our secret").decode()), signed(created=int(time.time()) - 3600)):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/genesys", headers=headers) as ws:
            ws.receive_text()
    unset = TestClient(create_app(Settings(api_key="k", public_url="https://ring.example.com", db_path=":memory:"),
                                  Store(":memory:")))
    with pytest.raises(WebSocketDisconnect), unset.websocket_connect("/ws/genesys", headers=signed()) as ws:
        ws.receive_text()


def test_a_call_reaches_the_numbers_assistant_with_architects_variables(server, fake_bot):
    client, store, a = server
    replies = connect(client, opening(motivo="sua fatura"))
    assert replies[0]["type"] == "opened" and replies[0]["parameters"]["media"][0]["format"] == "PCMU"
    call_id, plan = fake_bot[0]
    assert plan.first_message == "Oi Maria, sobre sua fatura."
    call = store.get("calls", call_id)
    assert (call["phoneCallProvider"], call["phoneCallProviderId"]) == ("genesys", "conv-1")
    assert call["assistantId"] == a["id"]
    assert call["customer"] == {"number": "+5511912345678", "name": "Maria"}


def test_architect_can_pick_the_assistant(server, fake_bot):
    client, store, _ = server
    other = client.post("/assistant", json={**ASSISTANT, "firstMessage": "Cobrança, bom dia."}, headers=AUTH).json()
    connect(client, opening(assistantId=other["id"]))
    assert fake_bot[0][1].first_message == "Cobrança, bom dia."
    assert "assistantOverrides" not in store.get("calls", fake_bot[0][0])  # assistantId is not a variable
    refused = connect(client, opening(assistantId="made-up"))
    assert refused[-1]["type"] == "disconnect" and refused[-1]["parameters"]["reason"] == "error"
    assert len(fake_bot) == 1


def test_a_number_without_an_assistant_hands_the_call_back(server, fake_bot):
    client, _, _ = server
    replies = connect(client, {**opening(), "parameters": {**opening()["parameters"],
                                                           "participant": {**CALLER, "dnis": "tel:+15559999999"}}})
    assert [r["type"] for r in replies] == ["opened", "disconnect"]
    assert "No assistant" in replies[1]["parameters"]["info"] and fake_bot == []


def test_a_transfer_goes_back_to_architect_with_the_number(server, fake_bot):
    client, _, _ = server

    async def transfer_now(_websocket, transfer):
        await transfer("+551130000000")

    fake_bot.then(transfer_now)
    replies = connect(client, opening())
    out = replies[-1]
    assert out["type"] == "disconnect" and out["parameters"]["reason"] == "completed"
    assert out["parameters"]["outputVariables"] == {"action": "transfer", "transferTo": "+551130000000",
                                                    "callId": fake_bot[0][0]}


def test_genesys_connects_from_the_dashboard():
    with pytest.raises(keys.ConnectError, match="base64"):
        keys.clean_values("genesys", {"GENESYS_API_KEY": KEY, "GENESYS_CLIENT_SECRET": "not-base64_secret"})
    assert keys.clean_values("genesys", {"GENESYS_API_KEY": KEY, "GENESYS_CLIENT_SECRET": SECRET})
    assert genesys.number("tel:+5511912345678") == "+5511912345678" and genesys.number("sip:+1555@x") == "+1555@x"
