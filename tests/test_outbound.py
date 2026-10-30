"""Outbound calls (POST /call). Twilio's REST API is replaced by a stub that records each request."""

import xml.etree.ElementTree as ET

import pytest

from openring import twilio
from openring.twilio import TwilioError, compute_signature

from test_api import ASSISTANT, AUTH, make_client


@pytest.fixture
def placed(monkeypatch):
    calls = []

    async def create_call(account_sid, auth_token, *, to, from_, url, status_callback):
        calls.append({"to": to, "from": from_, "url": url, "status_callback": status_callback})
        return "CA-out-1"

    monkeypatch.setattr(twilio, "create_call", create_call)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    return calls


def _setup(**overrides):
    client = make_client(twilio_account_sid="AC1", **overrides)
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    n = client.post("/phone-number", json={"number": "+1 (555) 123-4567", "assistantId": a["id"]}, headers=AUTH).json()
    return client, a, n


def _call(client, n, number="+55 11 91234-5678"):
    return client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": number}}, headers=AUTH)


def _signed(client, path, form):
    sig = compute_signature("tok", "https://ring.example.com" + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": sig})


def test_outbound_call_is_placed_and_answered_by_its_assistant(placed):
    client, a, n = _setup()
    r = _call(client, n)
    assert r.status_code == 201, r.text
    call = r.json()
    assert (call["type"], call["status"], call["phoneCallProviderId"]) == ("outboundPhoneCall", "queued", "CA-out-1")
    assert placed == [{
        "to": "+5511912345678",
        "from": "+15551234567",
        "url": f"https://ring.example.com/twilio/voice?callId={call['id']}",
        "status_callback": f"https://ring.example.com/twilio/status?callId={call['id']}",
    }]
    # Answered: Twilio fetches the call's URL (signed with its query string) and gets the media stream.
    path = f"/twilio/voice?callId={call['id']}"
    twiml = _signed(client, path, {"CallSid": "CA-out-1", "To": "+5511912345678"}).text
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    assert {p.get("name"): p.get("value") for p in stream.findall("Parameter")} == {
        "assistantId": a["id"], "callId": call["id"],
    }
    # The same URL for some other Twilio call does not connect.
    assert "<Stream" not in _signed(client, path, {"CallSid": "CA-someone-else"}).text


def test_outbound_call_needs_a_number_to_call_twilio_and_public_url(placed):
    client, _, n = _setup()
    assert client.post("/call", json={"phoneNumberId": n["id"]}, headers=AUTH).status_code == 400
    assert client.post("/call", json={"phoneNumberId": "nope"}, headers=AUTH).status_code == 404
    for overrides, missing in (({"twilio_auth_token": ""}, "TWILIO_AUTH_TOKEN"), ({"public_url": ""}, "PUBLIC_URL")):
        client, _, n = _setup(**overrides)
        r = _call(client, n)
        assert r.status_code == 400 and missing in r.json()["detail"]
    assert placed == []


def test_refused_outbound_call_is_recorded(monkeypatch, placed):
    async def refuse(*_args, **_kwargs):
        raise TwilioError("Trial accounts may only make calls to verified numbers. (Twilio error 21219)")

    monkeypatch.setattr(twilio, "create_call", refuse)
    client, _, n = _setup()
    r = _call(client, n)
    assert r.status_code == 400 and "verified numbers" in r.json()["detail"]
    call = client.get("/call", headers=AUTH).json()[0]
    assert (call["status"], call["endedReason"]) == ("ended", "twilio-failed-to-connect-call")


def test_unanswered_outbound_call_is_closed_by_the_status_callback(placed):
    client, _, n = _setup()
    call = _call(client, n).json()
    path = f"/twilio/status?callId={call['id']}"
    assert client.post(path, data={"CallStatus": "no-answer"}).status_code == 403  # unsigned
    assert _signed(client, path, {"CallSid": "CA-out-1", "CallStatus": "no-answer"}).status_code == 204
    ended = client.get(f"/call/{call['id']}", headers=AUTH).json()
    assert (ended["status"], ended["endedReason"]) == ("ended", "customer-did-not-answer")
