"""Turn a Vapi-style assistant document into a concrete STT / LLM / TTS stack.

`resolve_plan` only reads the assistant, the environment and the disk (no Pipecat
objects), so it is easy to test. `build_services` instantiates the Pipecat services
for a live call.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProviderError(ValueError):
    """Raised when an assistant asks for a provider we cannot build."""


# Provider names are Vapi's, so an assistant exported from Vapi keeps working; a few extras follow.
#
# LLMs, all reached through their OpenAI-compatible API: name -> (base_url, api key env var).
# Anything that speaks the OpenAI chat API can be added here in one line.
LLM_PROVIDERS: dict[str, tuple[str | None, str | None]] = {
    "openai": (None, "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GOOGLE_API_KEY"),  # Gemini
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "together-ai": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "deepinfra": ("https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY"),
    "perplexity-ai": ("https://api.perplexity.ai", "PERPLEXITY_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    # Local servers that need no key.
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), None),
    # OmniRoute: free local gateway to 150+ free model providers.
    "omniroute": (os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128/v1"), "OMNIROUTE_API_KEY"),
}
LOCAL_LLMS = {"ollama", "omniroute"}  # they run without a key
STT_PROVIDERS = {
    "deepgram": "DEEPGRAM_API_KEY",
    "assembly-ai": "ASSEMBLYAI_API_KEY",
    "gladia": "GLADIA_API_KEY",
    "soniox": "SONIOX_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "11labs": "ELEVENLABS_API_KEY",
}
TTS_PROVIDERS = {
    "11labs": "ELEVENLABS_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "kokoro": None,
    "cartesia": "CARTESIA_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "openai": "OPENAI_API_KEY",
    "rime-ai": "RIME_API_KEY",
    "neuphonic": "NEUPHONIC_API_KEY",
    "smallest-ai": "SMALLEST_API_KEY",
    "inworld": "INWORLD_API_KEY",
}
# Other spellings of the same provider.
ALIASES = {"deep-seek": "deepseek", "elevenlabs": "11labs", "gemini": "google", "assemblyai": "assembly-ai",
           "rime": "rime-ai", "smallest": "smallest-ai", "together": "together-ai", "perplexity": "perplexity-ai"}
PROVIDER_NAMES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google Gemini", "groq": "Groq", "openrouter": "OpenRouter",
    "deepseek": "DeepSeek", "xai": "xAI Grok", "cerebras": "Cerebras", "together-ai": "Together AI",
    "deepinfra": "DeepInfra", "perplexity-ai": "Perplexity", "mistral": "Mistral", "fireworks": "Fireworks AI",
    "ollama": "Ollama", "omniroute": "OmniRoute", "deepgram": "Deepgram", "assembly-ai": "AssemblyAI",
    "gladia": "Gladia", "soniox": "Soniox", "cartesia": "Cartesia", "11labs": "ElevenLabs", "kokoro": "Kokoro",
    "rime-ai": "Rime", "neuphonic": "Neuphonic", "smallest-ai": "Smallest AI", "inworld": "Inworld",
}


def _canonical(name: Any) -> str:
    name = str(name or "").lower()
    return ALIASES.get(name, name)

DEFAULT_TRANSCRIBER = {"provider": "deepgram", "model": "nova-3", "language": "multi"}
DEFAULT_VOICE = {"provider": "kokoro", "voiceId": "af_heart"}

# The free Kokoro voice runs from two model files (about 120 MB), kept in Pipecat's cache folder.
# At startup OpenRing downloads them if needed and loads the model once, in the background, so a
# call never waits for the download and does not reload the model (seconds on a laptop CPU).
# The int8 model is a quarter of the full one's size and a little faster on CPU.
KOKORO_DIR = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
KOKORO_FILES = {  # model first, then voices
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
_kokoro: dict[str, Any] = {"thread": None, "bytes": 0, "error": "", "model": None}
_kokoro_lock = threading.Lock()


def kokoro_installed() -> bool:
    return importlib.util.find_spec("kokoro_onnx") is not None


def kokoro_ready() -> bool:
    return all((KOKORO_DIR / name).exists() for name in KOKORO_FILES)


def warm_up() -> None:
    """Get the call machinery ready in the background, so the first call starts as fast as the rest."""

    def load_modules():  # importing them takes seconds on a laptop
        import pipecat.audio.vad.silero  # noqa: F401
        import pipecat.services.deepgram.stt  # noqa: F401
        import pipecat.services.openai.llm  # noqa: F401
        import pipecat.transports.smallwebrtc.transport  # noqa: F401

    threading.Thread(target=load_modules, name="warm-up", daemon=True).start()
    if kokoro_installed():
        prepare_kokoro()


def prepare_kokoro() -> None:
    """In the background: download the Kokoro model if it is missing, then load it. Safe to repeat."""
    thread = _kokoro["thread"]
    if thread and thread.is_alive():
        return
    _kokoro.update(bytes=0, error="")
    _kokoro["thread"] = threading.Thread(target=_prepare_kokoro, name="kokoro", daemon=True)
    _kokoro["thread"].start()


def _prepare_kokoro() -> None:
    from loguru import logger

    try:
        _download_kokoro()
        shared_kokoro().create("Hi", voice="af_heart", lang="en-us")  # the first synthesis is the slowest
        logger.info("the free Kokoro voice is ready")
    except Exception as exc:  # network errors, full disk...: report them and retry on the next call
        _kokoro["error"] = str(exc) or type(exc).__name__
        logger.error("preparing the Kokoro voice failed: {}", _kokoro["error"])


def _download_kokoro() -> None:
    import httpx

    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        dest = KOKORO_DIR / name
        if dest.exists():
            continue
        part = dest.with_name(name + ".part")  # renamed when complete, so a cut download starts over
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response, part.open("wb") as f:
            response.raise_for_status()
            for chunk in response.iter_bytes(1 << 20):
                f.write(chunk)
                _kokoro["bytes"] += len(chunk)
        part.replace(dest)


def shared_kokoro() -> Any:
    """The Kokoro model, loaded once per process and shared by every call."""
    with _kokoro_lock:
        if _kokoro["model"] is None:
            from kokoro_onnx import Kokoro

            model_path, voices_path = (str(KOKORO_DIR / name) for name in KOKORO_FILES)
            _kokoro["model"] = Kokoro(model_path, voices_path)
        return _kokoro["model"]


def kokoro_status() -> str:
    """Why a call with the Kokoro voice cannot start yet. Restarts a download that failed."""
    error = _kokoro["error"]
    prepare_kokoro()
    if error:
        return f"downloading the free Kokoro voice failed ({error}); trying again, check the internet connection"
    mb = _kokoro["bytes"] // 1_000_000
    return f"the free Kokoro voice is still downloading ({mb} of about 120 MB, one time only); try again in a minute"


def provider_status(settings: Any) -> list[dict[str, Any]]:
    """Each provider the dashboard lists, and whether it is ready to use. Keys are never returned."""
    def has(env: str | None) -> bool:
        return bool(env and os.environ.get(env, "").strip())

    rows = [{"id": pid, "name": PROVIDER_NAMES.get(pid, pid), "kind": "transcriber", "env": env, "ready": has(env)}
            for pid, env in STT_PROVIDERS.items()]
    for pid, (base_url, env) in LLM_PROVIDERS.items():
        row = {"id": pid, "name": PROVIDER_NAMES.get(pid, pid), "kind": "model", "env": env, "ready": has(env)}
        if pid in LOCAL_LLMS:  # a local server that works without a key, when it is running
            row.update(env=None, free=True, ready=None,
                       note=f"Works while {PROVIDER_NAMES[pid]} runs at {base_url}")
        rows.append(row)
    for pid, env in TTS_PROVIDERS.items():
        if env and pid not in ALIASES:
            rows.append({"id": pid, "name": PROVIDER_NAMES.get(pid, pid), "kind": "voice", "env": env,
                         "ready": has(env)})
    kokoro = {"id": "kokoro", "name": "Kokoro", "kind": "voice", "env": None, "free": True,
              "ready": kokoro_installed() and kokoro_ready()}
    if not kokoro_installed():
        kokoro["note"] = "not installed: pip install 'openring[kokoro]'"
    elif not kokoro_ready():
        kokoro["note"] = (f"download failed: {_kokoro['error']}" if _kokoro["error"]
                          else f"downloading, {_kokoro['bytes'] // 1_000_000} of about 120 MB")
    rows.append(kokoro)
    rows.append({"id": "twilio", "name": "Twilio", "kind": "telephony", "env": "TWILIO_AUTH_TOKEN",
                 "ready": bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Twilio can reach this server"})
    rows.append({"id": "telnyx", "name": "Telnyx", "kind": "telephony", "env": "TELNYX_API_KEY",
                 "ready": bool(settings.telnyx_api_key and settings.telnyx_public_key and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Telnyx can reach this server"})
    return rows


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
    assistant: dict[str, Any] = field(default_factory=dict)  # the saved document, for tools and webhooks


def _require_key(env_name: str | None, what: str) -> None:
    if env_name and not os.environ.get(env_name):
        where = os.environ.get("OPENRING_ENV_FILE")  # set by the `openring` command
        hint = f": add it to {where} and restart OpenRing" if where else " environment variable"
        raise ProviderError(f"{what} needs the {env_name}{hint}")


def _with_defaults(given: dict[str, Any], defaults: dict[str, Any], default_provider: str) -> dict[str, Any]:
    """Fill in the defaults, but only use the default model or voice with the default provider."""
    merged = {**defaults, **given}
    if _canonical(merged.get("provider")) != default_provider:
        for key in ("model", "voiceId"):
            if key not in given:
                merged.pop(key, None)
    return merged


def _supported(table: dict[str, Any], extra: tuple[str, ...] = ()) -> list[str]:
    return sorted([*(k for k in table if k not in ALIASES), *extra])


def resolve_plan(assistant: dict[str, Any], *, check_keys: bool = True) -> StackPlan:
    transcriber = _with_defaults(assistant.get("transcriber") or {}, DEFAULT_TRANSCRIBER, "deepgram")
    model = {**default_model(), **(assistant.get("model") or {})}
    voice = _with_defaults(assistant.get("voice") or {}, DEFAULT_VOICE, "kokoro")

    stt_provider = _canonical(transcriber["provider"])
    if stt_provider not in STT_PROVIDERS:
        raise ProviderError(f"transcriber provider '{stt_provider}' not supported yet: {_supported(STT_PROVIDERS)}")

    llm_provider = _canonical(model["provider"])
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
            f"model provider '{llm_provider}' not supported yet: {_supported(LLM_PROVIDERS, ('custom-llm',))}"
        )

    tts_provider = _canonical(voice["provider"])
    if tts_provider not in TTS_PROVIDERS:
        raise ProviderError(f"voice provider '{tts_provider}' not supported yet: {_supported(TTS_PROVIDERS)}")

    if check_keys:
        _require_key(STT_PROVIDERS[stt_provider], f"transcriber '{stt_provider}'")
        if llm_provider not in LOCAL_LLMS:  # local servers may run without a key
            _require_key(key_env, f"model provider '{llm_provider}'")
        _require_key(TTS_PROVIDERS[tts_provider], f"voice provider '{tts_provider}'")
        if tts_provider == "kokoro" and not kokoro_ready():
            raise ProviderError(kokoro_status())

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
        assistant=assistant,
    )


def _language(code: str | None):
    if not code or code == "multi":
        return code
    from pipecat.transcriptions.language import Language

    try:
        return Language(code)
    except ValueError:
        return code


# Speech-to-text services by provider: (module, class). Each takes api_key and Settings(model, language).
STT_SERVICES = {
    "assembly-ai": ("pipecat.services.assemblyai.stt", "AssemblyAISTTService"),
    "gladia": ("pipecat.services.gladia.stt", "GladiaSTTService"),
    "soniox": ("pipecat.services.soniox.stt", "SonioxSTTService"),
    "cartesia": ("pipecat.services.cartesia.stt", "CartesiaSTTService"),
    "11labs": ("pipecat.services.elevenlabs.stt", "ElevenLabsRealtimeSTTService"),
}
# Text-to-speech services by provider: (module, class, whether it takes the call's language).
TTS_SERVICES = {
    "cartesia": ("pipecat.services.cartesia.tts", "CartesiaTTSService", True),
    "deepgram": ("pipecat.services.deepgram.tts", "DeepgramTTSService", False),
    "openai": ("pipecat.services.openai.tts", "OpenAITTSService", False),
    "rime-ai": ("pipecat.services.rime.tts", "RimeTTSService", True),
    "neuphonic": ("pipecat.services.neuphonic.tts", "NeuphonicTTSService", True),
    "smallest-ai": ("pipecat.services.smallest.tts", "SmallestTTSService", True),
    "inworld": ("pipecat.services.inworld.tts", "InworldTTSService", True),
}
DEFAULT_VOICES = {"openai": "alloy", "deepgram": "aura-2-thalia-en"}


def _service_class(module: str, name: str, provider: str):
    import importlib

    try:
        return getattr(importlib.import_module(module), name)
    except ImportError as exc:  # an optional dependency of that provider
        raise ProviderError(f"provider '{provider}' needs an extra package: {exc}") from exc


def _build_stt(plan: StackPlan):
    lang = _language(plan.stt_language)
    if plan.stt_provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        return DeepgramSTTService(
            api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            settings=DeepgramSTTService.Settings(model=plan.stt_model or "nova-3", language=lang),
        )
    service = _service_class(*STT_SERVICES[plan.stt_provider], plan.stt_provider)
    settings: dict[str, Any] = {}
    if plan.stt_model:
        settings["model"] = plan.stt_model
    if lang and lang != "multi":  # without a language, these services detect it
        settings["language"] = lang
    return service(api_key=os.environ.get(STT_PROVIDERS[plan.stt_provider] or "", ""),
                   settings=service.Settings(**settings))


def _deepgram_voice(voice: str | None, model: str | None) -> str:
    """Deepgram names a voice with its model and language ('aura-2-thalia-en'); Vapi uses the short name."""
    if not voice:
        return DEFAULT_VOICES["deepgram"]
    return voice if voice.startswith("aura") else f"{model or 'aura-2'}-{voice}-en"


def _build_tts(plan: StackPlan):
    if plan.tts_provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        tts_settings: dict[str, Any] = {"voice": plan.tts_voice}
        if plan.tts_model:
            tts_settings["model"] = plan.tts_model
        return ElevenLabsTTSService(
            api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(**tts_settings),
        )
    if plan.tts_provider == "kokoro":  # free, runs locally on CPU
        try:
            import pipecat.services.kokoro.tts as kokoro_tts
            from pipecat.services.kokoro.tts import KokoroTTSService
        except ImportError as exc:  # optional dependency
            raise ProviderError("voice provider 'kokoro' needs: pip install 'pipecat-ai[kokoro]'") from exc

        # Pipecat loads a fresh model for every service it creates; hand it the shared one instead.
        kokoro_tts.Kokoro = lambda *_paths: shared_kokoro()

        kokoro_settings: dict[str, Any] = {"voice": plan.tts_voice}
        lang = _language(plan.tts_language)
        if lang and lang != "multi":
            kokoro_settings["language"] = lang
        model_path, voices_path = (str(KOKORO_DIR / name) for name in KOKORO_FILES)
        return KokoroTTSService(
            model_path=model_path,
            voices_path=voices_path,
            settings=KokoroTTSService.Settings(**kokoro_settings),
        )

    module, name, takes_language = TTS_SERVICES[plan.tts_provider]
    service = _service_class(module, name, plan.tts_provider)
    settings: dict[str, Any] = {}
    if plan.tts_provider == "deepgram":
        settings["voice"] = _deepgram_voice(plan.tts_voice, plan.tts_model)
    else:
        if plan.tts_voice or plan.tts_provider in DEFAULT_VOICES:
            settings["voice"] = plan.tts_voice or DEFAULT_VOICES[plan.tts_provider]
        if plan.tts_model:
            settings["model"] = plan.tts_model
    lang = _language(plan.tts_language)
    if takes_language and lang and lang != "multi":
        settings["language"] = lang
    return service(api_key=os.environ.get(TTS_PROVIDERS[plan.tts_provider] or "", ""),
                   settings=service.Settings(**settings))


def build_services(plan: StackPlan):
    """Instantiate Pipecat STT, LLM and TTS services for a call."""
    from pipecat.services.openai.llm import OpenAILLMService

    llm_settings: dict[str, Any] = {"model": plan.llm_model}
    if plan.temperature is not None:
        llm_settings["temperature"] = plan.temperature
    api_key = plan.llm_api_key or os.environ.get(plan.llm_api_key_env or "", "") or "not-needed"
    llm = OpenAILLMService(
        api_key=api_key,
        base_url=plan.llm_base_url,
        settings=OpenAILLMService.Settings(**llm_settings),
    )
    return _build_stt(plan), llm, _build_tts(plan)
