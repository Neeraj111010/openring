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


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty OpenRing home and no API key in the environment."""
    import openring.__main__ as cli

    monkeypatch.setattr(cli, "HOME", tmp_path)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("OPENRING_API_KEY", raising=False)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")  # no project-local .env
    return cli


def _start(cli):
    """What `openring` does with the API key at startup, without starting the server."""
    import os

    cli.load_settings()
    return os.environ.get("OPENRING_API_KEY", "")


def test_an_example_key_in_the_environment_is_replaced_and_kept(home, monkeypatch):
    # Docker: env_file .env (copied from .env.example) sets OPENRING_API_KEY=change-me.
    monkeypatch.setenv("OPENRING_API_KEY", "change-me")
    first = _start(home)
    assert first and first != "change-me" and len(first) >= 30
    monkeypatch.setenv("OPENRING_API_KEY", "change-me")  # the container restarts with the same env_file
    assert _start(home) == first  # the saved key, not a new one each time


def test_an_example_key_in_the_settings_file_is_replaced(home, monkeypatch):
    home.ENV_FILE.write_text("OPENRING_API_KEY=change-me\nPUBLIC_URL=\n", encoding="utf-8")
    key = _start(home)
    assert key and key != "change-me"
    assert f"OPENRING_API_KEY={key}" in home.ENV_FILE.read_text(encoding="utf-8")


def test_an_empty_environment_variable_does_not_hide_the_saved_key(home, monkeypatch):
    home.ENV_FILE.write_text("OPENRING_API_KEY=saved-key-1234567890\n", encoding="utf-8")
    monkeypatch.setenv("OPENRING_API_KEY", "")  # Docker's env_file with OPENRING_API_KEY=
    assert _start(home) == "saved-key-1234567890"


def test_a_deliberately_empty_key_stays_empty(home, monkeypatch):
    home.ENV_FILE.write_text("OPENRING_API_KEY=\n", encoding="utf-8")
    assert _start(home) == ""  # the API then answers only this computer
