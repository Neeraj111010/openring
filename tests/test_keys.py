import asyncio
import os

import httpx
import pytest

from openring import keys

from test_api import AUTH, DASH, make_client

GROQ_KEY = "gsk_" + "a" * 40


def signed_in(**overrides):
    client = make_client(password="correct horse", **overrides)
    assert client.post("/auth/login", json={"password": "correct horse"}).status_code == 200
    return client


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("# OpenRing settings\nOPENRING_API_KEY=abc\nGROQ_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("OPENRING_ENV_FILE", str(path))
    for env in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        monkeypatch.delenv(env, raising=False)
    return path


def run_check(provider, values, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await keys.check(provider, values, client)
    asyncio.run(go())


def test_write_env_keeps_comments_and_replaces_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep me\nA=1\n# B=commented\nB=\n", encoding="utf-8")
    keys.write_env(path, {"B": "two", "C": "three"})
    assert path.read_text(encoding="utf-8") == "# keep me\nA=1\n# B=commented\nB=two\nC=three\n"
    keys.write_env(path, {"B": ""})
    assert "B=\n" in path.read_text(encoding="utf-8")


def test_values_that_would_break_the_env_file_are_refused():
    with pytest.raises(keys.ConnectError):
        keys.clean_values("groq", {"GROQ_API_KEY": GROQ_KEY + "\nOPENRING_API_KEY=x"})
    with pytest.raises(keys.ConnectError):
        keys.clean_values("groq", {"GROQ_API_KEY": ""})
    with pytest.raises(keys.ConnectError):
        keys.clean_values("kokoro", {})
    assert keys.clean_values("groq", {"GROQ_API_KEY": f"  {GROQ_KEY} "}) == {"GROQ_API_KEY": GROQ_KEY}


def test_check_asks_each_provider_with_its_own_auth():
    seen = []

    def ok(request):
        seen.append((request.url.host, request.url.path, request.headers.get("authorization"),
                     request.headers.get("xi-api-key")))
        return httpx.Response(200, json={})

    run_check("groq", {"GROQ_API_KEY": "k1"}, ok)
    run_check("openai", {"OPENAI_API_KEY": "k2"}, ok)
    run_check("deepgram", {"DEEPGRAM_API_KEY": "k3"}, ok)
    run_check("11labs", {"ELEVENLABS_API_KEY": "k4"}, ok)
    run_check("openrouter", {"OPENROUTER_API_KEY": "k5"}, ok)
    run_check("twilio", {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t"}, ok)
    assert seen[0] == ("api.groq.com", "/openai/v1/models", "Bearer k1", None)
    assert seen[1] == ("api.openai.com", "/v1/models", "Bearer k2", None)
    assert seen[2] == ("api.deepgram.com", "/v1/projects", "Token k3", None)
    assert seen[3] == ("api.elevenlabs.io", "/v1/user", None, "k4")
    assert seen[4] == ("openrouter.ai", "/api/v1/key", "Bearer k5", None)
    assert seen[5][:2] == ("api.twilio.com", "/2010-04-01/Accounts/AC1.json")
    assert seen[5][2].startswith("Basic ")


def test_every_provider_with_a_key_can_be_connected():
    from openring.providers import ALIASES, LLM_PROVIDERS, LOCAL_LLMS, STT_PROVIDERS, TTS_PROVIDERS

    envs = {**STT_PROVIDERS, **TTS_PROVIDERS, **{p: e for p, (_, e) in LLM_PROVIDERS.items() if p not in LOCAL_LLMS}}
    missing = {p for p, env in envs.items() if env and p not in ALIASES and p not in keys.CONNECTORS}
    assert missing == set()
    assert all(keys.CONNECTORS[p]["keysUrl"].startswith("https://") for p in keys.CONNECTORS)


def test_newer_providers_are_checked_their_own_way():
    seen = []

    def record(request):
        seen.append((request.url.host, request.url.path, dict(request.headers)))
        return httpx.Response(200, json={})

    run_check("anthropic", {"ANTHROPIC_API_KEY": "a1"}, record)
    run_check("google", {"GOOGLE_API_KEY": "g1"}, record)
    run_check("cartesia", {"CARTESIA_API_KEY": "c1"}, record)
    run_check("assembly-ai", {"ASSEMBLYAI_API_KEY": "s1"}, record)
    assert seen[0][:2] == ("api.anthropic.com", "/v1/models") and seen[0][2]["x-api-key"] == "a1"
    assert seen[1][:2] == ("generativelanguage.googleapis.com", "/v1beta/openai/models")  # no double slash
    assert seen[2][2]["x-api-key"] == "c1" and "cartesia-version" in seen[2][2]
    assert seen[3][2]["authorization"] == "s1"

    # Google and xAI answer a bad key with 400 instead of 401.
    with pytest.raises(keys.ConnectError, match="rejected"):
        run_check("google", {"GOOGLE_API_KEY": "bad"},
                  lambda r: httpx.Response(400, json={"error": {"message": "Please pass a valid API key"}}))
    # Rime's check can only spot a rejected key; anything else counts as working.
    run_check("rime-ai", {"RIME_API_KEY": "r1"}, lambda r: httpx.Response(405))
    with pytest.raises(keys.ConnectError, match="rejected"):
        run_check("rime-ai", {"RIME_API_KEY": "bad"}, lambda r: httpx.Response(401))

    def unreachable(request):
        raise AssertionError("Smallest AI has no endpoint to check a key")
    asyncio.run(_check_with("smallest-ai", {"SMALLEST_API_KEY": "s" * 10}, unreachable, expect=False))


async def _check_with(provider, values, handler, expect):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keys.check(provider, values, client) is expect


def test_check_explains_rejections():
    with pytest.raises(keys.ConnectError, match="rejected"):
        run_check("groq", {"GROQ_API_KEY": "bad"}, lambda r: httpx.Response(401))
    with pytest.raises(keys.ConnectError, match="500"):
        run_check("groq", {"GROQ_API_KEY": "k"}, lambda r: httpx.Response(500))

    def offline(request):
        raise httpx.ConnectError("no network")
    with pytest.raises(keys.ConnectError, match="could not reach Groq"):
        run_check("groq", {"GROQ_API_KEY": "k"}, offline)
    # A restricted ElevenLabs key cannot read the account but still speaks.
    run_check("11labs", {"ELEVENLABS_API_KEY": "k"},
              lambda r: httpx.Response(401, json={"detail": {"status": "missing_permissions"}}))


def test_connect_checks_then_saves_and_applies_at_once(env_file, monkeypatch):
    checked = []

    async def fake_check(provider, values, client=None):
        checked.append((provider, values))
        if values.get("GROQ_API_KEY") == "gsk_" + "b" * 40:
            raise keys.ConnectError("Groq rejected this key; copy it again from Groq")
        return True

    monkeypatch.setattr(keys, "check", fake_check)
    client = signed_in()
    groq = next(p for p in client.get("/dashboard/info", headers=DASH).json()["providers"] if p["id"] == "groq")
    assert groq["connect"]["fields"] == [{"env": "GROQ_API_KEY", "label": "API key", "set": False}]
    assert groq["connect"]["keysUrl"].startswith("https://console.groq.com")

    bad = client.post("/providers/groq/connect", json={"values": {"GROQ_API_KEY": "gsk_" + "b" * 40}}, headers=DASH)
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    assert "gsk_" not in env_file.read_text(encoding="utf-8")

    r = client.post("/providers/groq/connect", json={"values": {"GROQ_API_KEY": GROQ_KEY}}, headers=DASH)
    assert r.status_code == 200 and r.json()["connected"] and r.json()["saved"]
    assert os.environ["GROQ_API_KEY"] == GROQ_KEY
    assert f"GROQ_API_KEY={GROQ_KEY}" in env_file.read_text(encoding="utf-8")
    info = client.get("/dashboard/info", headers=DASH).json()
    assert GROQ_KEY not in str(info)  # never sent back to the page
    assert next(p for p in info["providers"] if p["id"] == "groq")["ready"] is True

    assert client.post("/providers/groq/test", headers=DASH).json() == {"ok": True, "checked": True}
    assert client.delete("/providers/groq/connect", headers=DASH).json()["connected"] is False
    assert "GROQ_API_KEY" not in os.environ
    assert "GROQ_API_KEY=\n" in env_file.read_text(encoding="utf-8")


def test_connecting_twilio_refreshes_the_live_settings(env_file, monkeypatch):
    async def fake_check(provider, values, client=None):
        return True

    monkeypatch.setattr(keys, "check", fake_check)
    client = signed_in(twilio_auth_token="")
    values = {"TWILIO_ACCOUNT_SID": "AC" + "1" * 32, "TWILIO_AUTH_TOKEN": "f" * 32}
    assert client.post("/providers/twilio/connect", json={"values": values}, headers=DASH).status_code == 200
    settings = client.app.state.settings
    assert settings.twilio_account_sid == values["TWILIO_ACCOUNT_SID"] and settings.twilio_auth_token == "f" * 32


def test_connecting_needs_a_dashboard_session(env_file):
    client = make_client()
    body = {"values": {"GROQ_API_KEY": GROQ_KEY}}
    assert client.post("/providers/groq/connect", json=body, headers=AUTH).status_code == 401
    assert client.delete("/providers/groq/connect", headers=AUTH).status_code == 401
    assert client.post("/providers/openrouter/oauth", json={"code": "c", "codeVerifier": "v"}).status_code == 401


def test_openrouter_sign_in_trades_the_code_for_a_key(env_file, monkeypatch):
    async def fake_exchange(code, verifier, client=None):
        assert (code, verifier) == ("the-code", "the-verifier")
        return "sk-or-v1-" + "c" * 40

    monkeypatch.setattr(keys, "exchange_openrouter_code", fake_exchange)
    client = signed_in()
    r = client.post("/providers/openrouter/oauth", json={"code": "the-code", "codeVerifier": "the-verifier"},
                    headers=DASH)
    assert r.status_code == 200 and r.json()["connected"]
    assert os.environ["OPENROUTER_API_KEY"].startswith("sk-or-v1-")
    assert "sk-or-v1-" not in r.text
    assert client.post("/providers/openrouter/oauth", json={}, headers=DASH).status_code == 400


def test_openrouter_exchange_sends_the_pkce_verifier():
    async def go(handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await keys.exchange_openrouter_code("c0de", "v3rifier", client)

    sent = []

    def ok(request):
        sent.append(request.read())
        return httpx.Response(200, json={"key": "sk-or-v1-x"})

    assert asyncio.run(go(ok)) == "sk-or-v1-x"
    assert b'"code_verifier":"v3rifier"' in sent[0].replace(b" ", b"")
    with pytest.raises(keys.ConnectError, match="10 minutes"):
        asyncio.run(go(lambda r: httpx.Response(403, json={"error": "expired"})))
