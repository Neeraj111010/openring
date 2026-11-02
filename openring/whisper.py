"""Whisper: free speech-to-text that runs on this computer (faster-whisper, on CTranslate2).

A model downloads once from Hugging Face and loads in the background, and every call shares the loaded
model, as with the Kokoro voice. With an NVIDIA GPU it runs on CUDA; otherwise on the CPU with 8-bit weights.
On a 2018 laptop CPU (i5-8265U), a 4-second sentence takes about 0.6 s with `tiny` and 1.2 s with `base`,
and about twice that when the language is left to detection (`"language": "multi"`).
"""

from __future__ import annotations

import importlib.util
import threading
from typing import Any

# Short name -> (faster-whisper model: a size or a Hugging Face repo, download size).
MODELS = {
    "tiny": ("tiny", "75 MB"),
    "base": ("base", "145 MB"),
    "small": ("small", "480 MB"),
    "medium": ("medium", "1.5 GB"),
    "large-v3": ("large-v3", "3 GB"),
    "large-v3-turbo": ("deepdml/faster-whisper-large-v3-turbo-ct2", "1.6 GB"),
}
DEFAULT = "base"
SAMPLE_RATE = 16000  # what Whisper listens at; phone calls arrive at 8 kHz

_state: dict[str, dict[str, Any]] = {"threads": {}, "models": {}, "errors": {}}
_lock = threading.Lock()


def installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def model_id(name: str) -> str:
    """The faster-whisper model for a short name; a Hugging Face repo id ("org/model") passes through."""
    if name in MODELS:
        return MODELS[name][0]
    if "/" in name:
        return name
    raise ValueError(f"Whisper model '{name}' unknown: use {', '.join(MODELS)} or a Hugging Face repo id")


def ready(name: str) -> bool:
    return model_id(name) in _state["models"]


def loading() -> list[str]:
    """The models downloading or loading right now."""
    return [name for name, (key, _) in MODELS.items()
            if key not in _state["models"] and (t := _state["threads"].get(key)) is not None and t.is_alive()]


def prepare(name: str) -> None:
    """In the background: download the model if needed and load it. Safe to repeat."""
    key = model_id(name)
    thread = _state["threads"].get(key)
    if key in _state["models"] or (thread and thread.is_alive()):
        return
    _state["errors"].pop(key, None)
    _state["threads"][key] = threading.Thread(target=_prepare, args=(key,), name=f"whisper-{name}", daemon=True)
    _state["threads"][key].start()


def _prepare(key: str) -> None:
    from loguru import logger

    try:
        load(key)
        logger.info("the free Whisper transcriber ({}) is ready", key)
    except Exception as exc:  # network errors, full disk...: report them and retry on the next call
        _state["errors"][key] = str(exc) or type(exc).__name__
        logger.error("preparing the Whisper model {} failed: {}", key, _state["errors"][key])


def load(key: str) -> Any:
    """The loaded model for a faster-whisper model id, loading it (and downloading it) the first time."""
    with _lock:
        if key not in _state["models"]:
            _state["models"][key] = _Greedy(_open(key))
        return _state["models"][key]


def _open(key: str) -> Any:
    import ctranslate2
    import numpy as np
    from faster_whisper import WhisperModel

    attempts = [("cpu", "int8")]
    if ctranslate2.get_cuda_device_count() > 0:
        attempts.insert(0, ("cuda", "int8_float16"))
    for device, compute_type in attempts:
        try:
            model = WhisperModel(key, device=device, compute_type=compute_type)
            list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")[0])  # first run is slowest
            return model
        except Exception:
            if device == "cpu":
                raise
            # A GPU without its CUDA libraries fails here; the CPU still works.
    raise RuntimeError("unreachable")


class _Greedy:
    """The shared model, decoding greedily: about 15% faster than beam search, same text in our tests."""

    def __init__(self, model: Any):
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def transcribe(self, audio: Any, **options: Any) -> Any:
        return self._model.transcribe(audio, beam_size=1, without_timestamps=True,
                                      condition_on_previous_text=False, **options)


def status(name: str) -> str:
    """Why a call with this Whisper model cannot start yet. Restarts a download that failed."""
    key = model_id(name)
    error = _state["errors"].get(key)
    prepare(name)
    if error:
        return f"getting the Whisper {name} model failed ({error}); trying again, check the internet connection"
    size = MODELS.get(name, (None, "a few hundred MB"))[1]
    return (f"the free Whisper {name} model is still downloading or loading (about {size}, one time only); "
            "try again in a minute")


_service: list[Any] = []


def _import_pipecat_whisper() -> Any:
    """Pipecat's Whisper module, also on Apple Silicon Macs without MLX Whisper.

    On macOS arm64, Pipecat 1.15 imports `mlx_whisper` when the module loads and fails without it, though
    only its MLX service uses it (and imports it again when used). OpenRing uses faster-whisper, so a
    stand-in lets the module load; it is removed right after, so the MLX service still fails clearly.
    """
    import platform
    import sys
    import types

    stand_in = None
    if (platform.system() == "Darwin" and platform.machine() == "arm64"
            and "mlx_whisper" not in sys.modules and importlib.util.find_spec("mlx_whisper") is None):
        stand_in = sys.modules["mlx_whisper"] = types.ModuleType("mlx_whisper")
    try:
        import pipecat.services.whisper.stt as whisper_stt
    finally:
        if stand_in is not None and sys.modules.get("mlx_whisper") is stand_in:
            del sys.modules["mlx_whisper"]
    return whisper_stt


def stt_service(name: str, language: Any, hotwords: str | None = None) -> Any:
    """A Pipecat speech-to-text service on the shared model. `language` None lets Whisper detect it;
    `hotwords` are words to listen for, separated by spaces."""
    if not _service:
        from pipecat.audio.utils import create_file_resampler

        whisper_stt = _import_pipecat_whisper()

        # Pipecat loads a fresh model for every service it creates; hand it the shared one instead.
        whisper_stt.WhisperModel = lambda key, **_options: load(key)

        class LocalWhisper(whisper_stt.WhisperSTTService):
            """Pipecat's Whisper service, given 16 kHz audio whatever the call's rate. Pipecat passes the
            pipeline's audio straight to Whisper, so a phone call's 8 kHz would sound twice as fast."""

            def __init__(self, **kwargs: Any):
                super().__init__(**kwargs)
                self._upsampler = create_file_resampler()

            async def run_stt(self, audio: bytes):
                if self.sample_rate and self.sample_rate != SAMPLE_RATE:
                    audio = await self._upsampler.resample(audio, self.sample_rate, SAMPLE_RATE)
                async for frame in super().run_stt(audio):
                    yield frame

        _service.append(LocalWhisper)
    service = _service[0]
    return service(settings=service.Settings(model=model_id(name), language=language, hotwords=hotwords))
