"""Dashboard login: one password per server, kept as a scrypt hash, and session cookies.

The REST API keeps its Bearer API key (that is what Vapi clients send); people sign in
to the dashboard with a password instead, like OmniRoute's dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .store import Store

PASSWORD_KEY = "dashboard_password"
COOKIE = "openring_session"
# The dashboard sends this header with every request. A cross-site form or image cannot,
# so a session cookie alone never authorizes a request (CSRF).
DASHBOARD_HEADER = "x-openring"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD = 8
# scrypt cost: about 50 ms and 16 MB per check, cheap for people, slow for guessing.
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    scheme, _, rest = stored.partition("$")
    salt, _, digest = rest.partition("$")
    if scheme != "scrypt" or not salt or not digest:
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT)
    return hmac.compare_digest(got.hex(), digest)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"use at least {MIN_PASSWORD} characters"
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Password and sessions, stored in the database so they survive restarts."""

    def __init__(self, store: Store):
        self.store = store

    @property
    def password_set(self) -> bool:
        return bool(self.store.get_setting(PASSWORD_KEY))

    def set_password(self, password: str) -> None:
        self.store.set_setting(PASSWORD_KEY, hash_password(password))
        self.store.delete_sessions()  # a new password signs everyone out

    def reset_password(self) -> None:
        self.store.set_setting(PASSWORD_KEY, None)
        self.store.delete_sessions()

    def check(self, password: str) -> bool:
        stored = self.store.get_setting(PASSWORD_KEY)
        return bool(stored) and check_password(password, stored)

    def start(self) -> str:
        token = secrets.token_urlsafe(32)
        self.store.add_session(_token_hash(token), time.time() + SESSION_SECONDS)
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        expires = self.store.session_expiry(_token_hash(token))
        if expires is None:
            return False
        if expires < time.time():
            self.store.delete_sessions(_token_hash(token))
            return False
        return True

    def end(self, token: str) -> None:
        if token:
            self.store.delete_sessions(_token_hash(token))


class Throttle:
    """Slows down password guessing: a few failures per address, then a pause."""

    def __init__(self, max_failures: int = 10, window: float = 300):
        self.max_failures = max_failures
        self.window = window
        self.failures: dict[str, list[float]] = {}

    def _recent(self, who: str) -> list[float]:
        cutoff = time.monotonic() - self.window
        recent = [t for t in self.failures.get(who, []) if t > cutoff]
        self.failures[who] = recent
        return recent

    def blocked(self, who: str) -> bool:
        return len(self._recent(who)) >= self.max_failures

    def fail(self, who: str) -> None:
        self._recent(who).append(time.monotonic())

    def clear(self, who: str) -> None:
        self.failures.pop(who, None)
