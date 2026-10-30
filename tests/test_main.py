import socket

import pytest

from openring.__main__ import ensure_port_free, port_in_use, reset_password
from openring.auth import Sessions
from openring.store import Store


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


def test_reset_password_lets_you_choose_a_new_one(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENRING_PASSWORD", raising=False)
    db = str(tmp_path / "openring.db")
    sessions = Sessions(Store(db))
    sessions.set_password("forgotten password")
    token = sessions.start()
    reset_password(db)
    after = Sessions(Store(db))
    assert not after.password_set and not after.valid(token)
    assert "choose a new one" in capsys.readouterr().out
