import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.providers import ProviderError, build_services, resolve_plan
from openring.store import Store
from openring.twilio import compute_signature

AUTH = {"Authorization": "Bearer test-key"}
ASSISTANT = {
    "name": "Clinica Sorriso",
    "firstMessage": "Olá! Clínica Sorriso, como posso ajudar?",
    "model": {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "temperature": 0.4,
        "messages": [{"role": "system", "content": "Você é a recepcionista."}],
    },
    "voice": {"provider": "11labs", "voiceId": "voice123"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "pt-BR"},
    "server": {"url": "https://example.com/hook"},
}


def make_client(**overrides) -> TestClient:
    settings = Settings(
        api_key="test-key",
        public_url="https://ring.example.com",
        db_path=":memory:",
        twilio_auth_token="tok",
        validate_twilio_signature=True,
        **overrides,
    )
    return TestClient(create_app(settings, Store(":memory:")))


def test_auth_required():
    client = make_client()
    assert client.get("/assistant").status_code == 401
    assert client.get("/assistant", headers=AUTH).status_code == 200
    assert client.get("/health").status_code == 200


def test_assistant_crud():
    client = make_client()
    created = client.post("/assistant", json=ASSISTANT, headers=AUTH)
    assert created.status_code == 201
    a = created.json()
    assert a["id"] and a["createdAt"] and a["name"] == "Clinica Sorriso"

    patched = client.patch(f"/assistant/{a['id']}", json={"name": "Novo"}, headers=AUTH).json()
    assert patched["name"] == "Novo" and patched["id"] == a["id"]
    assert len(client.get("/assistant", headers=AUTH).json()) == 1
    assert client.delete(f"/assistant/{a['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/assistant/{a['id']}", headers=AUTH).status_code == 404


def test_unknown_provider_rejected():
    client = make_client()
    bad = {**ASSISTANT, "voice": {"provider": "nope"}}
    r = client.post("/assistant", json=bad, headers=AUTH)
    assert r.status_code == 400 and "nope" in r.json()["detail"]


def _signed_post(client, form):
    sig = compute_signature("tok", "https://ring.example.com/twilio/voice", form)
    return client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": sig})


def test_inbound_call_returns_stream_twiml():
    client = make_client()
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+1 (555) 123-4567", "assistantId": a["id"]}, headers=AUTH)

    form = {"To": "+15551234567", "From": "+5511999999999", "CallSid": "CA123"}
    r = _signed_post(client, form)
    assert r.status_code == 200
    stream = ET.fromstring(r.text).find("./Connect/Stream")
    assert stream.get("url") == "wss://ring.example.com/ws/twilio"
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert params["assistantId"] == a["id"]

    call = client.get(f"/call/{params['callId']}", headers=AUTH).json()
    assert call["type"] == "inboundPhoneCall" and call["customer"]["number"] == "+5511999999999"


def test_bad_twilio_signature_rejected():
    client = make_client()
    r = client.post("/twilio/voice", data={"To": "+1"}, headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403


def test_number_without_assistant_says_message():
    client = make_client()
    r = _signed_post(client, {"To": "+19999999999", "From": "+1", "CallSid": "CA1"})
    assert "<Say>" in r.text and "<Stream" not in r.text


def test_plan_maps_vapi_fields(monkeypatch):
    plan = resolve_plan(ASSISTANT, check_keys=False)
    assert plan.tts_provider == "elevenlabs"  # Vapi's "11labs" alias
    assert plan.llm_base_url == "https://api.groq.com/openai/v1"
    assert plan.messages[0]["role"] == "system"
    assert plan.server_url == "https://example.com/hook"
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GROQ_API_KEY"):
        resolve_plan(ASSISTANT)


def test_services_instantiate(monkeypatch):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    stt, llm, tts = build_services(resolve_plan(ASSISTANT))
    assert type(stt).__name__ == "DeepgramSTTService"
    assert type(llm).__name__ == "OpenAILLMService"
    assert type(tts).__name__ == "ElevenLabsTTSService"
