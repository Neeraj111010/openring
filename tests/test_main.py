import socket

import pytest

from openring.__main__ import ensure_port_free, port_in_use


def test_busy_port_is_reported_with_a_way_out(capsys):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        assert port_in_use("127.0.0.1", port)
        with pytest.raises(SystemExit):
            ensure_port_free("127.0.0.1", port)
    out = capsys.readouterr().out
    assert f"Port {port} is already in use" in out and f"openring --port {port + 1}" in out
    assert not port_in_use("127.0.0.1", port)
