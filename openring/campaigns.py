"""Campaigns, as in Vapi's `/campaign`: one assistant calls a list of customers from your numbers, a few at a time,
from `schedulePlan.earliestAt` on and never after `schedulePlan.latestAt`. Each customer gets an ordinary outbound
call that carries the `campaignId`, so transcripts, reports and the Calls page work as for any call.

One server should run against a database: each server dials the campaigns it finds unfinished when it starts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from .store import Store, now_iso

MAX_CUSTOMERS = 10_000
DEFAULT_CONCURRENCY = 10
MAX_CONCURRENCY = 500
LIVE = ("scheduled", "in-progress")
TICK = 2.0  # seconds between looks at a campaign and its calls
DIAL_GAP = 1.0  # Twilio starts one outbound call per second by default
QUEUED_TIMEOUT = 300  # seconds before a call Twilio never reported back on stops holding a slot


class Stop(Exception):
    """The campaign cannot go on: its assistant or number was deleted, or a key was removed."""


def parse_time(value: Any) -> datetime | None:
    """An ISO 8601 time; one without a zone is UTC. Raises ValueError for anything else."""
    if not value:
        return None
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def dials(campaign: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Who to call and from which number, in order: `phoneNumberId` with `customers`, or Vapi's `dialPlan`."""
    if campaign.get("dialPlan"):
        return [(str(entry.get("phoneNumberId") or ""), customer)
                for entry in campaign["dialPlan"] for customer in entry.get("customers") or []]
    return [(str(campaign.get("phoneNumberId") or ""), customer) for customer in campaign.get("customers") or []]


def counters(campaign: dict[str, Any]) -> dict[str, int]:
    """Vapi's call counters. "Scheduled" counts the customers not called yet."""
    calls = list((campaign.get("calls") or {}).values())
    return {
        "callsCounterScheduled": max(len(dials(campaign)) - len(calls), 0),
        "callsCounterQueued": sum(c.get("status") == "queued" for c in calls),
        "callsCounterInProgress": sum(c.get("status") == "in-progress" for c in calls),
        "callsCounterEndedVoicemail": sum(c.get("endedReason") == "voicemail" for c in calls),
        "callsCounterEnded": sum(c.get("status") == "ended" for c in calls),
    }


def _entry(call: dict[str, Any]) -> dict[str, Any]:
    """What the campaign keeps about one of its calls."""
    entry = {"customer": call.get("customer"), "status": call.get("status")}
    if call.get("endedReason"):
        entry["endedReason"] = call["endedReason"]
    return entry


class Dialer:
    """Runs this server's live campaigns, one task each. `dial(campaign, phoneNumberId, customer)` places one
    call and returns its record (already ended if the phone provider refused it), or raises Stop."""

    def __init__(self, store: Store, dial: Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[dict[str, Any]]]):
        self.store = store
        self.dial = dial
        self.tasks: dict[str, asyncio.Task] = {}
        self.booted = now_iso()

    def start(self, campaign_id: str) -> None:
        task = self.tasks.get(campaign_id)
        if task is None or task.done():
            self.tasks[campaign_id] = asyncio.get_running_loop().create_task(self._run(campaign_id))

    def resume(self) -> None:
        """Picks up the campaigns a stopped server left scheduled or running."""
        for campaign in self.store.list("campaigns", limit=MAX_CUSTOMERS):
            if campaign.get("status") in LIVE:
                self.start(campaign["id"])

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    async def _run(self, campaign_id: str) -> None:
        try:
            while await self._tick(campaign_id):
                await asyncio.sleep(TICK)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bug must not leave the campaign looking alive
            logger.exception("campaign {} stopped on an error", campaign_id)
            self._stop(campaign_id, f"stopped on an error: {exc}")

    def _stop(self, campaign_id: str, problem: str) -> None:
        for status in LIVE:
            if self.store.claim("campaigns", campaign_id, status, {"status": "ended", "endedAt": now_iso(),
                                                                   "error": problem}):
                logger.warning("campaign {} stopped: {}", campaign_id, problem)
                return

    async def _tick(self, campaign_id: str) -> bool:
        """One look at the campaign: start it, dial into free slots, or end it. False once it is over."""
        campaign = self.store.get("campaigns", campaign_id)
        if campaign is None or campaign.get("status") not in LIVE:
            return False
        now = datetime.now(timezone.utc)
        schedule = campaign.get("schedulePlan") or {}
        earliest, latest = parse_time(schedule.get("earliestAt")), parse_time(schedule.get("latestAt"))
        if earliest and now < earliest:
            return True
        if campaign["status"] == "scheduled":
            campaign = self.store.claim("campaigns", campaign_id, "scheduled", {"status": "in-progress"})
            if campaign is None:
                return True  # changed meanwhile; the next look sees how
            logger.info("campaign {} started: {} customers", campaign_id, len(dials(campaign)))

        calls = self._refresh(campaign.get("calls") or {})
        active = sum(c.get("status") != "ended" for c in calls.values())
        todo = dials(campaign)[len(calls):]
        closed = latest is not None and now >= latest  # past the window: no new calls
        if not active and (closed or not todo):
            done = {"status": "ended", "endedReason": "campaign.ended.success", "endedAt": now_iso(), "calls": calls,
                    **counters({**campaign, "calls": calls})}
            if self.store.claim("campaigns", campaign_id, "in-progress", done):
                logger.info("campaign {} ended: {} calls", campaign_id, len(calls))
            return False

        free = 0 if closed else max(int(campaign.get("maxConcurrency") or DEFAULT_CONCURRENCY) - active, 0)
        for number_id, customer in todo[:free]:
            if (self.store.get("campaigns", campaign_id) or {}).get("status") != "in-progress":
                return True  # cancelled or deleted while dialing
            try:
                call = await self.dial(campaign, number_id, customer)
            except Stop as exc:
                self._save(campaign_id, campaign, calls)
                self._stop(campaign_id, str(exc))
                return False
            calls[call["id"]] = _entry(call)
            self._save(campaign_id, campaign, calls)  # saved per call, so a restart never calls anyone twice
            await asyncio.sleep(DIAL_GAP)
        if calls != (campaign.get("calls") or {}):
            self._save(campaign_id, campaign, calls)
        return True

    def _save(self, campaign_id: str, campaign: dict[str, Any], calls: dict[str, Any]) -> None:
        # Only these fields: a cancel that lands meanwhile keeps its status.
        self.store.update("campaigns", campaign_id, {"calls": calls, **counters({**campaign, "calls": calls})})

    def _refresh(self, calls: dict[str, Any]) -> dict[str, Any]:
        """The latest status of each call still going, closing the ones that can no longer end on their own."""
        out = {}
        for call_id, entry in calls.items():
            if entry.get("status") == "ended":
                out[call_id] = entry
                continue
            call = self.store.get("calls", call_id)
            if call is None:
                out[call_id] = {**entry, "status": "ended"}
                continue
            if call.get("status") == "in-progress" and (call.get("startedAt") or "") < self.booted:
                # It was live when the server stopped: its media stream died with the old server.
                call = self.store.claim("calls", call_id, "in-progress", {
                    "status": "ended", "endedReason": "worker-shutdown", "endedAt": now_iso()}) or call
            elif call.get("status") == "queued" and _age(call.get("createdAt")) > QUEUED_TIMEOUT:
                call = self.store.claim("calls", call_id, "queued", {
                    "status": "ended", "endedReason": "twilio-failed-to-connect-call", "endedAt": now_iso()}) or call
            out[call_id] = _entry(call)
        return out


def _age(iso: str | None) -> float:
    try:
        when = parse_time(iso)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - when).total_seconds() if when else 0.0
