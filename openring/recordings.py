"""Call recordings, on your own server: one WAV file per call, the caller on the left, the assistant on the right.

Off unless the assistant asks for it with `artifactPlan.recordingEnabled` (Vapi records by default; recording someone
without telling them is against the law in many places). The files sit next to the database, and the API serves them
only with the API key or a dashboard session.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import BinaryIO

from loguru import logger

SAMPLE_WIDTH = 2  # bytes: 16-bit audio, what Pipecat's recorder hands over
# How much of the call waits in memory before it goes to the file: about 10 s of a phone call.
BUFFER_BYTES = 160_000


def directory() -> Path:
    """OPENRING_RECORDINGS_DIR, or a `recordings` folder next to the database."""
    chosen = os.environ.get("OPENRING_RECORDINGS_DIR", "").strip()
    if chosen:
        return Path(chosen)
    return Path(os.environ.get("OPENRING_DB_PATH", "data/openring.db")).parent / "recordings"


def path_for(call_id: str) -> Path:
    return directory() / f"{call_id}.wav"


def url_for(call_id: str) -> str:
    """Where to fetch a recording. Like Vapi's, it needs authorizing: the API key, or a dashboard session."""
    return f"{os.environ.get('PUBLIC_URL', '').strip().rstrip('/')}/call/{call_id}/recording"


class Recorder:
    """Writes a call's audio to its file as it comes, so a long call doesn't sit in memory and a crash loses little
    (the WAV header is right after every write). A full disk loses the recording, never the call."""

    def __init__(self, call_id: str):
        self.path = path_for(call_id)
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
                self._file = open(self.path, "wb")
                os.chmod(self.path, 0o600)  # conversations: for the server's own user only
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
        try:
            if self._wav is not None:
                self._wav.close()
            if self._file is not None:
                self._file.close()
        except (OSError, wave.Error) as exc:
            logger.error("recording of {} not finished: {}", self.path.name, exc)
        self._wav = self._file = None
        return self.recorded
