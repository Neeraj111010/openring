"""Turn a Vapi-style assistant document into a concrete STT / LLM / TTS stack.

`resolve_plan` is pure (no network, no Pipecat objects) so it is easy to test.
`build_services` instantiates the Pipecat services for a live call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


class ProviderError(ValueError):
    """Raised when an assistant asks for a provider we cannot build."""


# OpenAI-compatible LLM providers: name -> (base_url, api key env var).
# Anything that speaks the OpenAI chat API can be added here in one line.
LLM_PROVIDERS: dict[str, tuple[str | None, str]] = {
    "openai": (None, "OPENAI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    # OmniRoute: free local gateway to 150+ free model providers.
    "omniroute": (os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128/v1"), "OMNIROUTE_API_KEY"),
}
STT_PROVIDERS = {"deepgram": "DEEPGRAM_API_KEY"}
# "11labs" is the provider name Vapi uses for ElevenLabs.
TTS_PROVIDERS = {"elevenlabs": "ELEVENLABS_API_KEY", "11labs": "ELEVENLABS_API_KEY", "kokoro": None}

DEFAULT_TRANSCRIBER = {"provider": "deepgram", "model": "nova-3", "language": "multi"}
DEFAULT_VOICE = {"provider": "kokoro", "voiceId": "af_heart"}


def default_model() -> dict[str, Any]:
    return {
        "provider": os.environ.get("DEFAULT_LLM_PROVIDER", "openai"),
        "model": os.environ.get("DEFAULT_LLM_MODEL", "gpt-4.1-mini"),
    }


@dataclass
class StackPlan:
    stt_provider: str
    stt_model: str | None
    stt_language: str | None
    llm_provider: str
    llm_model: str
    llm_base_url: str | None
    llm_api_key_env: str | None
    llm_api_key: str | None
    temperature: float | None
    tts_provider: str
    tts_voice: str | None
    tts_model: str | None
    tts_language: str | None
    messages: list[dict[str, str]] = field(default_factory=list)
    first_message: str | None = None
    end_call_phrases: list[str] = field(default_factory=list)
    server_url: str | None = None


def _require_key(env_name: str | None, what: str) -> None:
    if env_name and not os.environ.get(env_name):
        raise ProviderError(f"{what} needs the {env_name} environment variable")


def resolve_plan(assistant: dict[str, Any], *, check_keys: bool = True) -> StackPlan:
    transcriber = {**DEFAULT_TRANSCRIBER, **(assistant.get("transcriber") or {})}
    model = {**default_model(), **(assistant.get("model") or {})}
    voice = {**DEFAULT_VOICE, **(assistant.get("voice") or {})}

    stt_provider = str(transcriber["provider"]).lower()
    if stt_provider not in STT_PROVIDERS:
        raise ProviderError(f"transcriber provider '{stt_provider}' not supported yet: {sorted(STT_PROVIDERS)}")

    llm_provider = str(model["provider"]).lower()
    llm_api_key = None
    if llm_provider == "custom-llm":
        base_url = model.get("url")
        if not base_url:
            raise ProviderError("model.provider 'custom-llm' requires model.url (an OpenAI-compatible base URL)")
        key_env = None
        llm_api_key = model.get("apiKey") or "not-needed"
    elif llm_provider in LLM_PROVIDERS:
        base_url, key_env = LLM_PROVIDERS[llm_provider]
    else:
        raise ProviderError(
            f"model provider '{llm_provider}' not supported yet: {sorted([*LLM_PROVIDERS, 'custom-llm'])}"
        )

    tts_provider = str(voice["provider"]).lower()
    if tts_provider not in TTS_PROVIDERS:
        raise ProviderError(f"voice provider '{tts_provider}' not supported yet: {sorted(TTS_PROVIDERS)}")

    if check_keys:
        _require_key(STT_PROVIDERS[stt_provider], f"transcriber '{stt_provider}'")
        if llm_provider != "omniroute":  # a local OmniRoute may run without a key
            _require_key(key_env, f"model provider '{llm_provider}'")
        _require_key(TTS_PROVIDERS[tts_provider], f"voice provider '{tts_provider}'")

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in model.get("messages") or []
        if m.get("role") and m.get("content")
    ]
    if model.get("systemPrompt"):
        messages.insert(0, {"role": "system", "content": model["systemPrompt"]})

    server = assistant.get("server") or {}
    return StackPlan(
        stt_provider=stt_provider,
        stt_model=transcriber.get("model"),
        stt_language=transcriber.get("language"),
        llm_provider=llm_provider,
        llm_model=model["model"],
        llm_base_url=base_url,
        llm_api_key_env=key_env,
        llm_api_key=llm_api_key,
        temperature=model.get("temperature"),
        tts_provider="elevenlabs" if tts_provider == "11labs" else tts_provider,
        tts_voice=voice.get("voiceId"),
        tts_model=voice.get("model"),
        tts_language=voice.get("language") or transcriber.get("language"),
        messages=messages,
        first_message=assistant.get("firstMessage"),
        end_call_phrases=list(assistant.get("endCallPhrases") or []),
        server_url=server.get("url") or assistant.get("serverUrl"),
    )


def _language(code: str | None):
    if not code or code == "multi":
        return code
    from pipecat.transcriptions.language import Language

    try:
        return Language(code)
    except ValueError:
        return code


def build_services(plan: StackPlan):
    """Instantiate Pipecat STT, LLM and TTS services for a call."""
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.openai.llm import OpenAILLMService

    stt = DeepgramSTTService(
        api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
        settings=DeepgramSTTService.Settings(
            model=plan.stt_model or "nova-3", language=_language(plan.stt_language)
        ),
    )

    llm_settings: dict[str, Any] = {"model": plan.llm_model}
    if plan.temperature is not None:
        llm_settings["temperature"] = plan.temperature
    api_key = plan.llm_api_key or os.environ.get(plan.llm_api_key_env or "", "") or "not-needed"
    llm = OpenAILLMService(
        api_key=api_key,
        base_url=plan.llm_base_url,
        settings=OpenAILLMService.Settings(**llm_settings),
    )

    if plan.tts_provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        tts_settings: dict[str, Any] = {"voice": plan.tts_voice}
        if plan.tts_model:
            tts_settings["model"] = plan.tts_model
        tts = ElevenLabsTTSService(
            api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(**tts_settings),
        )
    else:  # kokoro: free, runs locally on CPU
        try:
            from pipecat.services.kokoro.tts import KokoroTTSService
        except ImportError as exc:  # optional dependency
            raise ProviderError("voice provider 'kokoro' needs: pip install 'pipecat-ai[kokoro]'") from exc

        kokoro_settings: dict[str, Any] = {"voice": plan.tts_voice}
        lang = _language(plan.tts_language)
        if lang and lang != "multi":
            kokoro_settings["language"] = lang
        tts = KokoroTTSService(settings=KokoroTTSService.Settings(**kokoro_settings))

    return stt, llm, tts
