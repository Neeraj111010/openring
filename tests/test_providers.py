"""Provider names are Vapi's: every one OpenRing claims resolves and builds its Pipecat service."""

import pytest

from openring.config import Settings
from openring.providers import (
    LLM_PROVIDERS,
    STT_PROVIDERS,
    TTS_PROVIDERS,
    ProviderError,
    build_services,
    provider_status,
    resolve_plan,
)

KEYS = {e for e in [*STT_PROVIDERS.values(), *TTS_PROVIDERS.values(), *(e for _, e in LLM_PROVIDERS.values())] if e}


@pytest.fixture
def keys(monkeypatch):
    for env in KEYS:
        monkeypatch.setenv(env, "fake")


def assistant(transcriber=None, model=None, voice=None):
    return {
        "transcriber": transcriber or {"provider": "deepgram"},
        "model": model or {"provider": "groq", "model": "openai/gpt-oss-20b"},
        "voice": voice or {"provider": "11labs", "voiceId": "v1"},
    }


@pytest.mark.parametrize("name", ["openai", "anthropic", "google", "groq", "openrouter", "deepseek", "deep-seek", "xai",
                                  "cerebras", "together-ai", "deepinfra", "perplexity-ai", "mistral", "fireworks",
                                  "ollama", "omniroute"])
def test_every_llm_provider_resolves_to_its_openai_compatible_api(keys, name):
    plan = resolve_plan(assistant(model={"provider": name, "model": "m"}))
    base_url, env = LLM_PROVIDERS[plan.llm_provider]
    assert plan.llm_base_url == base_url and plan.llm_api_key_env == env
    if name not in ("openai", "ollama", "omniroute"):
        assert plan.llm_base_url.startswith("https://")


def test_local_llms_need_no_key(monkeypatch):
    for env in KEYS:
        monkeypatch.delenv(env, raising=False)
    for env in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(env, "fake")
    resolve_plan(assistant(model={"provider": "ollama", "model": "llama3.2"}))
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        resolve_plan(assistant(model={"provider": "anthropic", "model": "claude-sonnet-4-5"}))


STT_CLASSES = {
    "deepgram": "DeepgramSTTService",
    "assembly-ai": "AssemblyAISTTService",
    "gladia": "GladiaSTTService",
    "soniox": "SonioxSTTService",
    "cartesia": "CartesiaSTTService",
    "11labs": "ElevenLabsRealtimeSTTService",
}
TTS_CLASSES = {
    "11labs": "ElevenLabsTTSService",
    "cartesia": "CartesiaTTSService",
    "deepgram": "DeepgramTTSService",
    "openai": "OpenAITTSService",
    "rime-ai": "RimeTTSService",
    "neuphonic": "NeuphonicTTSService",
    "smallest-ai": "SmallestTTSService",
    "inworld": "InworldTTSService",
}


@pytest.mark.parametrize("name", sorted(STT_CLASSES))
def test_every_transcriber_builds_its_service(keys, name):
    stt, _, _ = build_services(resolve_plan(assistant(transcriber={"provider": name, "language": "pt-BR"})))
    assert type(stt).__name__ == STT_CLASSES[name]


@pytest.mark.parametrize("name", sorted(TTS_CLASSES))
def test_every_voice_builds_its_service(keys, name):
    _, _, tts = build_services(resolve_plan(assistant(voice={"provider": name, "voiceId": "voice-1"})))
    assert type(tts).__name__ == TTS_CLASSES[name]


def test_every_claimed_provider_is_tested():
    assert set(STT_PROVIDERS) - {"whisper"} == set(STT_CLASSES)  # whisper has its own tests
    assert set(TTS_PROVIDERS) - {"elevenlabs", "kokoro"} == set(TTS_CLASSES)  # kokoro has its own tests


def test_defaults_belong_to_the_default_provider(keys):
    plan = resolve_plan(assistant(transcriber={"provider": "assembly-ai"}, voice={"provider": "cartesia"}))
    assert plan.stt_model is None  # not Deepgram's nova-3
    assert plan.tts_voice is None  # not Kokoro's af_heart
    # Kokoro may not be downloaded on this machine (CI): the defaults are the point here, not readiness.
    plan = resolve_plan({"model": {"provider": "groq", "model": "m"}}, check_keys=False)
    assert (plan.stt_provider, plan.stt_model, plan.tts_provider, plan.tts_voice) == (
        "deepgram", "nova-3", "kokoro", "af_heart")


def test_vapi_deepgram_voice_names_become_deepgrams(keys):
    from openring.providers import _deepgram_voice

    assert _deepgram_voice("thalia", None) == "aura-2-thalia-en"
    assert _deepgram_voice("asteria", "aura") == "aura-asteria-en"
    assert _deepgram_voice("aura-2-luna-en", None) == "aura-2-luna-en"
    assert _deepgram_voice(None, None) == "aura-2-thalia-en"


def test_unknown_provider_lists_the_supported_ones(keys):
    with pytest.raises(ProviderError) as err:
        resolve_plan(assistant(voice={"provider": "playht", "voiceId": "x"}))
    assert "cartesia" in str(err.value) and "elevenlabs'" not in str(err.value)  # aliases are not listed


def test_provider_status_lists_each_provider_once(keys):
    rows = provider_status(Settings(db_path=":memory:"))
    ids = [r["id"] for r in rows]
    pairs = [(r["kind"], r["id"]) for r in rows]  # Deepgram transcribes and speaks: one row per job
    assert len(pairs) == len(set(pairs))
    assert {"anthropic", "assembly-ai", "cartesia", "rime-ai", "ollama", "twilio"} <= set(ids)
    assert "elevenlabs" not in ids and "deep-seek" not in ids
    by_id = {r["id"]: r for r in rows}
    assert by_id["ollama"]["ready"] is None and by_id["anthropic"]["ready"] is True
    assert {r["kind"] for r in rows} == {"transcriber", "model", "voice", "telephony"}


def test_openrouter_requests_credit_openring(keys):
    _, llm, _ = build_services(resolve_plan(assistant(model={"provider": "openrouter", "model": "m"})))
    headers = llm._client.default_headers
    assert headers["X-Title"] == "OpenRing" and "github.com/elifalanrocha/openring" in headers["HTTP-Referer"]
    _, other, _ = build_services(resolve_plan(assistant()))  # other providers get no extra headers
    assert "X-Title" not in other._client.default_headers


def test_openrouter_attribution_can_be_turned_off(keys, monkeypatch):
    monkeypatch.setenv("OPENRING_ATTRIBUTION", "false")
    _, llm, _ = build_services(resolve_plan(assistant(model={"provider": "openrouter", "model": "m"})))
    assert "X-Title" not in llm._client.default_headers


def test_fallbacks_use_vapis_fields_and_resolve_each_layer(keys):
    plan = resolve_plan(assistant(
        transcriber={"provider": "deepgram", "fallbackPlan": {"transcribers": [{"provider": "assembly-ai"}]}},
        model={"provider": "groq", "model": "openai/gpt-oss-20b", "temperature": 0.3,
               "fallbackModels": ["openai/gpt-oss-120b", {"provider": "openai", "model": "gpt-4.1-mini"}]},
        voice={"provider": "11labs", "voiceId": "v1",
               "fallbackPlan": {"voices": [{"provider": "deepgram", "voiceId": "thalia"}, {"provider": "playht"}]}},
    ))
    assert [p.stt_provider for p in plan.fallbacks["transcriber"]] == ["assembly-ai"]
    models = [(p.llm_provider, p.llm_model, p.temperature) for p in plan.fallbacks["model"]]
    assert models == [("groq", "openai/gpt-oss-120b", 0.3), ("openai", "gpt-4.1-mini", 0.3)]
    assert [p.tts_provider for p in plan.fallbacks["voice"]] == ["deepgram"]  # playht isn't supported: skipped
    assert any("playht" in w for w in plan.warnings)
    assert plan.fallbacks["model"][1].stt_provider == "deepgram"  # a fallback changes its own layer only
    assert resolve_plan(assistant()).fallbacks == {}


def test_a_fallback_without_its_key_is_skipped_not_fatal(keys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    plan = resolve_plan(assistant(model={"provider": "groq", "model": "m",
                                         "fallbackModels": [{"provider": "openai", "model": "gpt-4.1-mini"}]}))
    assert "model" not in plan.fallbacks and "OPENAI_API_KEY" in plan.warnings[0]


def test_layers_with_fallbacks_become_switchers_in_order(keys):
    from pipecat.pipeline.llm_switcher import LLMSwitcher
    from pipecat.pipeline.service_switcher import ServiceSwitcher

    plan = resolve_plan(assistant(
        model={"provider": "groq", "model": "openai/gpt-oss-20b",
               "fallbackModels": [{"provider": "openai", "model": "x"}]},
        voice={"provider": "11labs", "voiceId": "v1", "fallbackPlan": {"voices": [{"provider": "deepgram"}]}},
    ))
    stt, llm, tts = build_services(plan)
    assert not isinstance(stt, ServiceSwitcher)  # no transcriber fallback: the plain service
    assert isinstance(llm, LLMSwitcher)
    assert [s.openring_label for s in llm.services] == ["groq openai/gpt-oss-20b", "openai x"]
    assert isinstance(tts, ServiceSwitcher) and tts.strategy.active_service is tts.services[0]


def test_multi_language_follows_the_caller(keys):
    from openring.providers import FOLLOW_LANGUAGE

    plan = resolve_plan(assistant(transcriber={"provider": "deepgram", "language": "multi"},
                                  voice={"provider": "11labs", "voiceId": "v1"}))
    assert plan.messages[-1] == {"role": "system", "content": FOLLOW_LANGUAGE} and not plan.warnings
    single = resolve_plan(assistant(transcriber={"provider": "deepgram", "language": "multi"},
                                    voice={"provider": "deepgram", "voiceId": "thalia"}))
    assert "one language" in single.warnings[0]
    fixed = resolve_plan(assistant(transcriber={"provider": "deepgram", "language": "pt-BR"}))
    assert all(m["content"] != FOLLOW_LANGUAGE for m in fixed.messages)


def test_call_limits_follow_vapi_fields_and_defaults():
    plan = resolve_plan(assistant(), check_keys=False)
    assert (plan.max_duration_seconds, plan.silence_timeout_seconds, plan.idle_messages) == (600, 30, [])
    plan = resolve_plan({**assistant(), "maxDurationSeconds": 120, "silenceTimeoutSeconds": 15,
                         "endCallMessage": "Bye for now.",
                         "messagePlan": {"idleMessages": ["Are you still there?", " "], "idleTimeoutSeconds": 8,
                                         "idleMessageMaxSpokenCount": 2,
                                         "idleMessageResetCountOnUserSpeechEnabled": True}}, check_keys=False)
    assert (plan.max_duration_seconds, plan.silence_timeout_seconds, plan.end_call_message) == (120, 15, "Bye for now.")
    assert (plan.idle_messages, plan.idle_timeout_seconds, plan.idle_max_spoken, plan.idle_reset_on_speech) == (
        ["Are you still there?"], 8, 2, True)
    assert resolve_plan({**assistant(), "silenceTimeoutSeconds": 5}, check_keys=False).silence_timeout_seconds == 5
    for change, words in (({"maxDurationSeconds": 5}, "maxDurationSeconds"),
                          ({"silenceTimeoutSeconds": "30"}, "silenceTimeoutSeconds"),
                          ({"silenceTimeoutSeconds": 4}, "silenceTimeoutSeconds"),
                          ({"endCallMessage": 123}, "endCallMessage"),
                          ({"messagePlan": {"idleTimeoutSeconds": 1}}, "idleTimeoutSeconds"),
                          ({"messagePlan": {"idleMessages": "hello"}}, "idleMessages")):
        with pytest.raises(ProviderError, match=words):
            resolve_plan({**assistant(), **change}, check_keys=False)
