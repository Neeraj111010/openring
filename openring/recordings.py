"""Call recordings, on your own server: one WAV file per call, the caller on the left, the assistant on the right.

Off unless the assistant asks for it with `artifactPlan.recordingEnabled` (Vapi records by default; recording someone
without telling them is against the law in many places). The files sit next to the database, and the API serves them
only with the API key or a dashboard session. They're deleted after `retention_days` (365 unless changed).
"""

from __future__ import annotations

import os
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from loguru import logger

SAMPLE_WIDTH = 2  # bytes: 16-bit audio, what Pipecat's recorder hands over
# How much of the call waits in memory before it goes to the file: about 10 s of a phone call.
BUFFER_BYTES = 160_000
_writing: set[str] = set()  # the calls whose file is still open here
RETENTION_KEY = "recording_retention_days"  # in the store's settings table
DEFAULT_RETENTION_DAYS = 365
MAX_RETENTION_DAYS = 36_500
CLEANUP_SECONDS = 6 * 3600  # how often old recordings are looked for


def is_being_written(call_id: str) -> bool:
    return call_id in _writing


def directory() -> Path:
    """OPENRING_RECORDINGS_DIR, or a `recordings` folder next to the database."""
    chosen = os.environ.get("OPENRING_RECORDINGS_DIR", "").strip()
    if chosen:
        return Path(chosen)
    return Path(os.environ.get("OPENRING_DB_PATH", "data/openring.db")).parent / "recordings"


def path_for(call_id: str) -> Path:
    return directory() / f"{call_id}.wav"


def retention_days(store: Any) -> int:
    """How long recordings are kept; 0 keeps them for good."""
    saved = store.get_setting(RETENTION_KEY)
    try:
        return DEFAULT_RETENTION_DAYS if saved is None else int(saved)
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def usage() -> dict[str, Any]:
    """The folder and what's in it, for the dashboard."""
    files = list(directory().glob("*.wav"))
    return {"folder": str(directory()), "files": len(files), "bytes": sum(f.stat().st_size for f in files)}


def old_recordings(days: int, is_call: Callable[[str], bool], now: float | None = None) -> list[str]:
    """The calls whose recording is older than `days`. Only files named after a call of ours: the folder can be
    set to anywhere, and someone else's .wav there is none of our business."""
    if days <= 0:
        return []
    limit = (time.time() if now is None else now) - days * 86_400
    found = []
    for path in directory().glob("*.wav"):
        call_id = path.stem
        if call_id not in _writing and path.stat().st_mtime < limit and is_call(call_id):
            found.append(call_id)
    return found


def url_for(call_id: str) -> str:
    """Where to fetch a recording. Like Vapi's, it needs authorizing: the API key, or a dashboard session."""
    return f"{os.environ.get('PUBLIC_URL', '').strip().rstrip('/')}/call/{call_id}/recording"


class Recorder:
    """Writes a call's audio to its file as it comes, so a long call doesn't sit in memory and a crash loses little
    (the WAV header is right after every write). A full disk loses the recording, never the call."""

    def __init__(self, call_id: str):
        self.path, self.call_id = path_for(call_id), call_id
        self._file: BinaryIO | None = None
        self._wav: wave.Wave_write | None = None
        self._done = False  # a full disk, or the call is over: nothing more is written (nor the file started again)
        self.recorded = False

    def write(self, audio: bytes, sample_rate: int, channels: int) -> None:
        if self._done or not audio:
            return
        try:
            if self._wav is None:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Conversations: for the server's own user only, from the moment the file exists.
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
                self._file = os.fdopen(os.open(self.path, flags, 0o600), "wb")
                _writing.add(self.call_id)
                self._wav = wave.open(self._file, "wb")
                self._wav.setnchannels(channels)
                self._wav.setsampwidth(SAMPLE_WIDTH)
                self._wav.setframerate(sample_rate)
            self._wav.writeframes(audio)
            self._file.flush()
            self.recorded = True
        except (OSError, wave.Error) as exc:
            self._done = True
            logger.error("recording of {} stopped: {}", self.path.name, exc)

    def close(self) -> bool:
        """Finish the file. Returns whether the call has a recording."""
        self._done = True
        _writing.discard(self.call_id)
        for part in (self._wav, self._file):  # one failing still lets go of the other: an open file can't be deleted
            try:
                if part is not None:
                    part.close()
            except (OSError, wave.Error) as exc:
                logger.error("recording of {} not finished: {}", self.path.name, exc)
        self._wav = self._file = None
        return self.recorded
