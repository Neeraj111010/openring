"""End-to-end call lifecycle over a simulated Twilio media stream.

Providers point at fake keys, so they fail to connect; the test checks that the
call is still tracked from start to finish and the transcript is saved.
"""

import json
import time
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH


def test_call_lifecycle(monkeypatch):
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

    with client.websocket_connect("/ws/twilio") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "accountSid": "AC1", "customParameters": params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        time.sleep(2)
        assert store.get("calls", params["callId"])["status"] == "in-progress"
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))

    for _ in range(50):
        call = store.get("calls", params["callId"])
        if call["status"] == "ended":
            break
        time.sleep(0.1)
    assert call["status"] == "ended"
    assert call["messages"][0] == {"role": "assistant", "message": ASSISTANT["firstMessage"]}
