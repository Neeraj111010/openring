"""The document store's list order: newest first, even when two documents share a timestamp."""

from openring import store as store_module
from openring.store import Store


def test_documents_created_in_the_same_instant_list_newest_first(monkeypatch):
    # Windows' clock ticks about every 15 ms, so a burst of calls (a campaign) can share a createdAt.
    monkeypatch.setattr(store_module, "now_iso", lambda: "2026-11-01T12:00:00.000000Z")
    store = Store(":memory:")
    ids = [store.create("calls", {"n": i})["id"] for i in range(5)]
    assert [d["id"] for d in store.list("calls")] == ids[::-1]
    assert [d["id"] for d in store.list("calls", limit=2)] == ids[:2:-1][:2]
