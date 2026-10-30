"""Connect providers from the dashboard: check a key with the provider, then keep it in the settings file.

People paste a key (or sign in, for OpenRouter); OpenRing asks the provider whether the key works before
saving it, so a typo shows up here and not halfway through a call. Saved keys apply at once, without a
restart, and are never sent back to the browser.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

from .providers import LLM_PROVIDERS

# Provider id (as the dashboard lists it) -> the settings it needs and where people get them.
CONNECTORS: dict[str, dict[str, Any]] = {
    "deepgram": {"fields": [("DEEPGRAM_API_KEY", "API key")], "keysUrl": "https://console.deepgram.com/"},
    "openai": {"fields": [("OPENAI_API_KEY", "API key")], "keysUrl": "https://platform.openai.com/api-keys"},
    "groq": {"fields": [("GROQ_API_KEY", "API key")], "keysUrl": "https://console.groq.com/keys"},
    "openrouter": {"fields": [("OPENROUTER_API_KEY", "API key")], "keysUrl": "https://openrouter.ai/settings/keys",
                   "oauth": True},
    "deepseek": {"fields": [("DEEPSEEK_API_KEY", "API key")], "keysUrl": "https://platform.deepseek.com/api_keys"},
    "11labs": {"fields": [("ELEVENLABS_API_KEY", "API key")],
               "keysUrl": "https://elevenlabs.io/app/settings/api-keys"},
    "twilio": {"fields": [("TWILIO_ACCOUNT_SID", "Account SID"), ("TWILIO_AUTH_TOKEN", "Auth token")],
               "keysUrl": "https://console.twilio.com/"},
}
NAMES = {"deepgram": "Deepgram", "openai": "OpenAI", "groq": "Groq", "openrouter": "OpenRouter",
         "deepseek": "DeepSeek", "11labs": "ElevenLabs", "twilio": "Twilio"}
OPENROUTER_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
# Keys are single tokens; anything else (spaces, quotes, line breaks) would break the .env file.
VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:+/=]{8,512}$")
TIMEOUT = 15


class ConnectError(ValueError):
    """The values cannot be used: malformed, rejected by the provider, or the provider is unreachable."""


def clean_values(provider: str, values: dict[str, Any]) -> dict[str, str]:
    if provider not in CONNECTORS:
        raise ConnectError(f"{provider} cannot be connected from the dashboard")
    cleaned = {}
    for env, label in CONNECTORS[provider]["fields"]:
        value = str(values.get(env) or "").strip()
        if not value:
            raise ConnectError(f"enter the {label.lower()}")
        if not VALUE_RE.match(value):
            raise ConnectError(f"that doesn't look like a {NAMES[provider]} {label.lower()}; paste it again")
        cleaned[env] = value
    return cleaned


def current_values(provider: str) -> dict[str, str]:
    return {env: os.environ.get(env, "").strip() for env, _ in CONNECTORS[provider]["fields"]}


def _check_request(provider: str, values: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """The cheapest authenticated request each provider answers: (url, httpx options)."""
    if provider == "deepgram":
        token = {"headers": {"Authorization": f"Token {values[env_of(provider)]}"}}
        return "https://api.deepgram.com/v1/projects", token
    if provider == "11labs":
        return "https://api.elevenlabs.io/v1/user", {"headers": {"xi-api-key": values[env_of(provider)]}}
    if provider == "twilio":
        sid = values["TWILIO_ACCOUNT_SID"]
        return (f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                {"auth": (sid, values["TWILIO_AUTH_TOKEN"])})
    bearer = {"headers": {"Authorization": f"Bearer {values[env_of(provider)]}"}}
    if provider == "openrouter":  # its model list is public, so ask about the key itself
        return "https://openrouter.ai/api/v1/key", bearer
    return (LLM_PROVIDERS[provider][0] or "https://api.openai.com/v1") + "/models", bearer


def env_of(provider: str) -> str:
    """The setting that holds a one-key provider's key."""
    return CONNECTORS[provider]["fields"][0][0]


async def check(provider: str, values: dict[str, str], client: httpx.AsyncClient | None = None) -> None:
    """Raise ConnectError unless the provider accepts these values."""
    name = NAMES[provider]
    url, options = _check_request(provider, values)
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                res = await own.get(url, **options)
        else:
            res = await client.get(url, **options)
    except httpx.HTTPError as exc:
        raise ConnectError(f"could not reach {name} ({type(exc).__name__}); check your internet connection") from exc
    if res.status_code == 200:
        return
    # A restricted ElevenLabs key may not read the account, yet it still works for speech.
    if provider == "11labs" and res.status_code == 401 and "missing_permissions" in res.text:
        return
    if res.status_code in (401, 403):
        raise ConnectError(f"{name} rejected this key; copy it again from {name}")
    raise ConnectError(f"{name} answered with an error ({res.status_code}); try again in a minute")


async def exchange_openrouter_code(code: str, verifier: str, client: httpx.AsyncClient | None = None) -> str:
    """Trade the code from OpenRouter's sign-in page for an API key (OAuth PKCE)."""
    body = {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                res = await own.post(OPENROUTER_EXCHANGE_URL, json=body)
        else:
            res = await client.post(OPENROUTER_EXCHANGE_URL, json=body)
    except httpx.HTTPError as exc:
        raise ConnectError("could not reach OpenRouter; check your internet connection") from exc
    key = res.json().get("key") if res.status_code == 200 else None
    if not key:
        raise ConnectError("OpenRouter did not accept the sign-in (codes last 10 minutes); try again")
    return key


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, keeping its other lines and comments; empty values clear a key."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def env_file() -> Path | None:
    where = os.environ.get("OPENRING_ENV_FILE")  # set by the `openring` command
    return Path(where) if where else None


def apply(updates: dict[str, str]) -> bool:
    """Use the values now and, when there is a settings file, keep them for the next start."""
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    path = env_file()
    if path is None:
        return False
    write_env(path, updates)
    return True
