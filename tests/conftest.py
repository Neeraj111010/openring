import ipaddress
import socket
import sys
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
