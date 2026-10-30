"""`openring` command: first run creates ~/.openring with a generated API key, then starts the server."""

import os
import secrets
from pathlib import Path

HOME = Path(os.environ.get("OPENRING_HOME", Path.home() / ".openring"))
ENV_FILE = HOME / ".env"

ENV_TEMPLATE = """# OpenRing settings. Edit and restart `openring` to apply.
OPENRING_API_KEY={key}
# Public https URL Twilio can reach (e.g. from `ngrok http 8000`)
PUBLIC_URL=
TWILIO_ACCOUNT_SID=
# Required to answer calls: it proves each webhook really comes from Twilio
TWILIO_AUTH_TOKEN=
# Speech-to-text: Deepgram gives US$200 free credit to new accounts
DEEPGRAM_API_KEY=
# LLM: any of these (or run OmniRoute locally for free models)
OPENAI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
OMNIROUTE_BASE_URL=http://localhost:20128/v1
# Text-to-speech: leave empty to use Kokoro (free, local)
ELEVENLABS_API_KEY=
"""


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def first_run_setup() -> bool:
    if ENV_FILE.exists():
        return False
    HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(ENV_TEMPLATE.format(key=secrets.token_urlsafe(24)), encoding="utf-8")
    return True


def main() -> None:
    created = first_run_setup()
    load_env_file(Path.cwd() / ".env")  # a project-local .env takes precedence
    load_env_file(ENV_FILE)
    os.environ.setdefault("OPENRING_DB_PATH", str(HOME / "openring.db"))

    import uvicorn

    from .api import create_app
    from .providers import kokoro_installed, kokoro_ready, warm_up

    os.environ["OPENRING_ENV_FILE"] = str(ENV_FILE)  # error messages point people to it
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  OpenRing is running: http://localhost:{port}")
    print(f"  Settings: {ENV_FILE}" + ("  (created now)" if created else ""))
    if kokoro_installed() and not kokoro_ready():
        print("  Downloading the free Kokoro voice in the background (one time, about 120 MB).")
    warm_up()
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="warning")


if __name__ == "__main__":
    main()
