"""Tiny SQLite document store for assistants, phone numbers and calls.

Each record is stored as a JSON document so the API can stay schema-compatible
with Vapi without a migration for every new field.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLLECTIONS = ("assistants", "phone_numbers", "calls")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            for name in COLLECTIONS:
                self._db.execute(
                    f"CREATE TABLE IF NOT EXISTS {name} ("
                    "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, doc TEXT NOT NULL)"
                )
            self._db.commit()

    def _check(self, collection: str) -> None:
        if collection not in COLLECTIONS:
            raise ValueError(f"unknown collection {collection}")

    def create(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        self._check(collection)
        ts = now_iso()
        doc = {**data, "id": str(uuid.uuid4()), "createdAt": ts, "updatedAt": ts}
        with self._lock:
            self._db.execute(
                f"INSERT INTO {collection} (id, created_at, doc) VALUES (?, ?, ?)",
                (doc["id"], ts, json.dumps(doc)),
            )
            self._db.commit()
        return doc

    def get(self, collection: str, item_id: str) -> dict[str, Any] | None:
        self._check(collection)
        with self._lock:
            row = self._db.execute(
                f"SELECT doc FROM {collection} WHERE id = ?", (item_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, collection: str, limit: int = 100) -> list[dict[str, Any]]:
        self._check(collection)
        with self._lock:
            rows = self._db.execute(
                f"SELECT doc FROM {collection} ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def update(self, collection: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        self._check(collection)
        current = self.get(collection, item_id)
        if current is None:
            return None
        protected = {"id", "createdAt"}
        doc = {**current, **{k: v for k, v in patch.items() if k not in protected}}
        doc["updatedAt"] = now_iso()
        with self._lock:
            self._db.execute(
                f"UPDATE {collection} SET doc = ? WHERE id = ?", (json.dumps(doc), item_id)
            )
            self._db.commit()
        return doc

    def delete(self, collection: str, item_id: str) -> dict[str, Any] | None:
        doc = self.get(collection, item_id)
        if doc is None:
            return None
        with self._lock:
            self._db.execute(f"DELETE FROM {collection} WHERE id = ?", (item_id,))
            self._db.commit()
        return doc

    def find_phone_number(self, number: str) -> dict[str, Any] | None:
        wanted = _normalize_number(number)
        for doc in self.list("phone_numbers", limit=10_000):
            if _normalize_number(doc.get("number", "")) == wanted:
                return doc
        return None


def _normalize_number(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())
