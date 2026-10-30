"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # Bearer token required by the REST API. Empty = API is open (local dev only).
    api_key: str = field(default_factory=lambda: _env("OPENRING_API_KEY"))
    # Public https URL where telephony providers reach this server (e.g. an ngrok URL).
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL").rstrip("/"))
    db_path: str = field(default_factory=lambda: _env("OPENRING_DB_PATH", "data/openring.db"))
    # Assistant used when a call arrives on a number with no assistant attached.
    default_assistant_id: str = field(default_factory=lambda: _env("DEFAULT_ASSISTANT_ID"))

    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    validate_twilio_signature: bool = field(
        default_factory=lambda: _env("VALIDATE_TWILIO_SIGNATURE", "true").lower() != "false"
    )

    @property
    def public_ws_url(self) -> str:
        if self.public_url.startswith("https://"):
            return "wss://" + self.public_url[len("https://"):]
        if self.public_url.startswith("http://"):
            return "ws://" + self.public_url[len("http://"):]
        return self.public_url


def get_settings() -> Settings:
    return Settings()
