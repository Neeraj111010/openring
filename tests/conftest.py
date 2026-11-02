import ipaddress
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def _on_this_computer_or_an_address(host: str) -> bool:
    if host in ("", "localhost"):
        return True
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Tests never use the internet: names don't resolve. A real provider answering a fake key made a test pass or
    fail by how fast the network was (it ends the call, if that comes before the test hangs up)."""
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if not _on_this_computer_or_an_address(name):
            raise socket.gaierror(socket.EAI_NONAME, f"tests don't use the internet: {name}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


SLOW_TOOL_SECONDS = 1.8


@pytest.fixture
def model_server():
    """A little OpenAI-style server on this computer, for Pipecat's real model service: it answers each path's requests
    from its script, in order, and keeps what it was asked. `/tool` is a function tool's server, and takes a while."""
    scripts, asked = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            who = self.path.split("/")[1]
            if who == "tool":
                time.sleep(SLOW_TOOL_SECONDS)
                results = [{"toolCallId": c["id"], "result": "Found it"} for c in body["message"]["toolCallList"]]
                answer = json.dumps({"results": results}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)
                return
            asked.append((who, body))
            if scripts.get("refuse") and scripts["refuse"](body):  # a provider stricter than OpenAI
                answer = json.dumps({"error": {"message": "invalid", "type": "invalid_request_error"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)
                return
            default = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "Okay."}, "finish_reason": "stop"}]}
            chunks = scripts[who].pop(0) if scripts.get(who) else [default]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", scripts, asked
    server.shutdown()


@pytest.fixture
def quiet_providers(monkeypatch):
    """A call runs on providers that pass everything along and say nothing. Returns their class."""
    from pipecat.processors.frame_processor import FrameProcessor

    from openring import bot

    class Quiet(FrameProcessor):
        def __init__(self, layer):
            super().__init__()
            self.openring_label, self.openring_price = layer, (layer, "none", None, None)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    monkeypatch.setattr(bot, "build_services", lambda plan: (Quiet("transcriber"), Quiet("model"), Quiet("voice")))
    return Quiet
