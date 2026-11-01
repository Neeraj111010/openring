"""Security guards for the whole API: a new route must not be reachable without a key by accident."""

import re

from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

# Routes anyone may call. Webhooks prove who sends them inside the handler (Twilio and Telnyx signatures);
# the dashboard page, health check, sign-in and API docs are public on purpose.
PUBLIC_HTTP = {
    "/", "/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/auth/state", "/auth/setup", "/auth/login", "/auth/logout",
    "/twilio/voice", "/twilio/status", "/telnyx/voice",
}
# Media streams: each claims a queued call by id (Twilio, Telnyx) or checks a signature (Genesys).
PUBLIC_WS = {"/ws/twilio", "/ws/telnyx", "/ws/genesys"}


def app():
    return create_app(Settings(api_key="k" * 32, public_url="https://ring.example.com", db_path=":memory:",
                               twilio_auth_token="tok"), Store(":memory:"))


def test_every_route_but_the_public_ones_needs_a_key_or_a_session():
    a = app()
    client = TestClient(a)
    checked = 0
    for route in a.routes:
        if isinstance(route, APIWebSocketRoute):
            assert route.path in PUBLIC_WS, f"new websocket {route.path}: decide how it authenticates"
            continue
        if not isinstance(route, APIRoute) or route.path in PUBLIC_HTTP:
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in route.methods - {"HEAD"}:
            r = client.request(method, path, json={})
            assert r.status_code == 401, f"{method} {route.path} answered {r.status_code} without a key"
            r = client.request(method, path, json={}, headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401, f"{method} {route.path} answered {r.status_code} with a wrong key"
            checked += 1
    assert checked > 40  # the guard really walked the API


def test_webhooks_refuse_unsigned_requests():
    client = TestClient(app())
    for path in ("/twilio/voice", "/twilio/status?callId=x"):
        assert client.post(path, data={"CallSid": "CA1", "To": "+15551234567"}).status_code == 403
    assert client.post("/telnyx/voice", data={"To": "+15551234567"}).status_code == 403
