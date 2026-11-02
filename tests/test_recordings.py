"""Call recordings: off unless asked for, written as the call goes, behind the API key, and deletable."""

import asyncio
import base64
import json
import os
import stat
import sys
import time
import wave

import pytest

from openring import recordings
from openring.providers import ProviderError, resolve_plan

from test_api import ASSISTANT, AUTH
from test_call_flow import _answered_call, _handshake, _pipeline_started, _wait_until_ended


@pytest.fixture(autouse=True)
def folder(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENRING_RECORDINGS_DIR", str(tmp_path / "recs"))
    return tmp_path / "recs"


def test_the_file_is_right_after_every_write_and_private(folder):
    recorder = recordings.Recorder("c1")
    recorder.write(b"\x01\x00\x02\x00" * 80, 8000, 2)
    with wave.open(str(folder / "c1.wav")) as wav:  # still open: a crash now loses nothing written
        assert (wav.getnchannels(), wav.getframerate(), wav.getnframes()) == (2, 8000, 80)
    recorder.write(b"\x01\x00\x02\x00" * 80, 8000, 2)
    assert recorder.close() is True
    with wave.open(str(folder / "c1.wav")) as wav:
        assert wav.getnframes() == 160
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(folder / "c1.wav").st_mode) == 0o600


def test_audio_that_comes_after_the_call_ended_cannot_start_the_file_over(folder):
    recorder = recordings.Recorder("c1")
    recorder.write(b"\x01\x00\x02\x00" * 80, 8000, 2)
    recorder.close()
    recorder.write(b"\x01\x00\x02\x00", 8000, 2)
    with wave.open(str(folder / "c1.wav")) as wav:
        assert wav.getnframes() == 80


def test_a_call_without_audio_leaves_no_file(folder):
    recorder = recordings.Recorder("c1")
    recorder.write(b"", 8000, 2)
    assert recorder.close() is False and not (folder / "c1.wav").exists()


def test_a_disk_problem_loses_the_recording_never_the_call(folder):
    folder.parent.mkdir(exist_ok=True)
    folder.write_text("a file where the folder should be")
    recorder = recordings.Recorder("c1")
    recorder.write(b"\x01\x00\x02\x00", 8000, 2)  # doesn't raise
    recorder.write(b"\x01\x00\x02\x00", 8000, 2)
    assert recorder.close() is False


def test_the_folder_sits_next_to_the_database_unless_told_otherwise(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENRING_RECORDINGS_DIR")
    monkeypatch.setenv("OPENRING_DB_PATH", str(tmp_path / "openring.db"))
    assert recordings.path_for("c1") == tmp_path / "recordings" / "c1.wav"


def test_recording_is_off_unless_the_assistant_asks():
    assert resolve_plan(ASSISTANT, check_keys=False).record is False
    on = {**ASSISTANT, "artifactPlan": {"recordingEnabled": True}}
    assert resolve_plan(on, check_keys=False).record is True
    off = {**ASSISTANT, "artifactPlan": {"recordingEnabled": False}}
    assert resolve_plan(off, check_keys=False).record is False
    assert resolve_plan({**ASSISTANT, "artifactPlan": {"recordingEnabled": "no"}}, check_keys=False).record is False
    with pytest.raises(ProviderError, match="artifactPlan"):
        resolve_plan({**ASSISTANT, "artifactPlan": "yes"}, check_keys=False)


def _call_with_audio(monkeypatch, enabled):
    """A simulated phone call on stub providers: a second of the caller's audio, then they hang up."""
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {"artifactPlan": {"recordingEnabled": enabled}})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(100):
            if store.get("calls", params["callId"])["status"] == "in-progress":
                break
            time.sleep(0.05)
        time.sleep(0.5)  # the pipeline is up and the caller connected
        payload = base64.b64encode(b"\x7f" * 160).decode()  # 20 ms of mu-law
        for _ in range(50):
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ1", "media": {"payload": payload}}))
        time.sleep(0.5)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    return client, _wait_until_ended(store, params["callId"])


def test_a_call_is_recorded_when_asked_and_only_the_key_gets_it(monkeypatch, quiet_providers, folder):
    client, call = _call_with_audio(monkeypatch, True)
    url = call["artifact"]["recordingUrl"]
    assert url.endswith(f"/call/{call['id']}/recording") and call["artifact"]["stereoRecordingUrl"] == url
    with wave.open(str(folder / f"{call['id']}.wav")) as wav:
        # All of the caller's second is there, though they hung up before anything went to the file.
        assert wav.getnchannels() == 2 and wav.getframerate() == 8000 and wav.getnframes() >= 8000 * 0.95

    path = f"/call/{call['id']}/recording"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.delete(path).status_code == 401
    got = client.get(path, headers=AUTH)
    assert got.status_code == 200 and got.headers["content-type"] == "audio/wav" and got.content[:4] == b"RIFF"
    assert got.headers["cache-control"] == "no-store"

    # Deleting removes the audio for good and leaves the transcript.
    left = client.delete(path, headers=AUTH)
    assert left.status_code == 200 and left.json()["artifact"]["recordingUrl"] is None
    assert left.json()["messages"] == call["messages"]
    assert not (folder / f"{call['id']}.wav").exists()
    assert client.get(path, headers=AUTH).status_code == 404
    assert client.delete(path, headers=AUTH).status_code == 404


def test_a_call_is_not_recorded_unless_asked(monkeypatch, quiet_providers, folder):
    client, call = _call_with_audio(monkeypatch, False)
    assert "recordingUrl" not in call["artifact"]
    assert not folder.exists()
    assert client.get(f"/call/{call['id']}/recording", headers=AUTH).status_code == 404


def test_an_unknown_call_has_no_recording_and_the_id_stays_a_call_id(monkeypatch, quiet_providers, folder):
    client, _ = _call_with_audio(monkeypatch, False)
    folder.mkdir(parents=True, exist_ok=True)
    (folder.parent / "secret.wav").write_bytes(b"RIFF")
    for bad in ("nope", "..%2Fsecret", "%2E%2E%2Fsecret"):
        assert client.get(f"/call/{bad}/recording", headers=AUTH).status_code == 404
        assert client.delete(f"/call/{bad}/recording", headers=AUTH).status_code == 404
    assert (folder.parent / "secret.wav").exists()


def test_deleting_a_recording_keeps_the_transcript_and_the_latency(monkeypatch, quiet_providers, folder):
    client, call = _call_with_audio(monkeypatch, True)
    assert client.delete(f"/call/{call['id']}/recording", headers=AUTH).status_code == 200
    artifact = client.get(f"/call/{call['id']}", headers=AUTH).json()["artifact"]
    assert artifact["messages"] == call["artifact"]["messages"]
    assert artifact["performanceMetrics"] == call["artifact"]["performanceMetrics"]
    assert artifact["recordingUrl"] is None and artifact["stereoRecordingUrl"] is None


def test_a_recording_still_being_written_cannot_be_fetched_or_deleted(monkeypatch, quiet_providers, folder):
    client, call = _call_with_audio(monkeypatch, True)
    url = f"/call/{call['id']}/recording"
    live = recordings.Recorder(call["id"])
    live.write(b"\x01\x00\x02\x00", 8000, 2)
    assert client.get(url, headers=AUTH).status_code == 409
    assert client.delete(url, headers=AUTH).status_code == 409
    assert (folder / f"{call['id']}.wav").exists()
    live.close()
    assert client.get(url, headers=AUTH).status_code == 200
    assert client.delete(url, headers=AUTH).status_code == 200


def test_a_recording_in_use_is_not_a_server_error(monkeypatch, quiet_providers, folder):
    from pathlib import Path

    client, call = _call_with_audio(monkeypatch, True)

    def in_use(_path):
        raise PermissionError("the file is open")

    monkeypatch.setattr(Path, "unlink", in_use)
    assert client.delete(f"/call/{call['id']}/recording", headers=AUTH).status_code == 409
    assert client.get(f"/call/{call['id']}", headers=AUTH).json()["artifact"]["recordingUrl"]  # nothing was deleted


def test_a_cancel_while_the_recording_finishes_still_ends_the_call(monkeypatch, quiet_providers, folder):
    """A server shutting down can cancel the call's task right there: the call must not stay "in-progress" forever."""
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

    real = AudioBufferProcessor.stop_recording

    async def cancelled_when_run_call_asks(self):
        await real(self)
        if sys._getframe(1).f_code.co_name == "run_call":
            raise asyncio.CancelledError

    monkeypatch.setattr(AudioBufferProcessor, "stop_recording", cancelled_when_run_call_asks)
    _client, call = _call_with_audio(monkeypatch, True)
    assert call["status"] == "ended" and call["endedReason"] == "customer-ended-call"
