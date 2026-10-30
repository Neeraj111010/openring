"""Numbers for the dashboard's overview, computed from the call records."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

# What hosted voice platforms charge per minute just for orchestration, on top of the providers.
PLATFORM_FEE_PER_MINUTE = 0.05
# endedReason prefixes that mean the call broke rather than finished.
FAILED_REASONS = ("pipeline-error", "provider-error", "assistant-not-found", "twilio-failed")
LATENCY_PARTS = ("endpointingLatency", "transcriberLatency", "modelLatency", "voiceLatency")


def _created(call: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(call.get("createdAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _failed(call: dict[str, Any]) -> bool:
    return str(call.get("endedReason") or "").startswith(FAILED_REASONS)


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else round((ordered[mid - 1] + ordered[mid]) / 2)


def summarize(calls: list[dict[str, Any]], days: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Totals for the last `days` days (every call when None), plus one entry per day for the chart."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).date() if days else None
    picked = [(c, _created(c)) for c in calls]
    picked = [(c, t) for c, t in picked if t and (start is None or t.date() >= start)]
    window = [c for c, _ in picked]

    ended = [c for c in window if c.get("status") == "ended"]
    failed = [c for c in ended if _failed(c)]
    seconds = sum(float(c.get("durationSeconds") or 0) for c in window)
    turns = [
        t
        for c in window
        for t in ((c.get("artifact") or {}).get("performanceMetrics") or {}).get("turnLatencies") or []
        if isinstance(t, dict) and t.get("turnLatency")
    ]
    minutes = seconds / 60

    first = start or min((t.date() for _, t in picked), default=now.date())
    first = max(first, now.date() - timedelta(days=89))  # the chart shows at most 90 days
    per_day: dict[date, dict[str, Any]] = {}
    d = first
    while d <= now.date():
        per_day[d] = {"date": d.isoformat(), "calls": 0, "minutes": 0.0, "failed": 0}
        d += timedelta(days=1)
    for c, t in picked:
        if t.date() in per_day:
            day = per_day[t.date()]
            day["calls"] += 1
            day["minutes"] += float(c.get("durationSeconds") or 0) / 60
            day["failed"] += int(_failed(c))
    for day in per_day.values():
        day["minutes"] = round(day["minutes"], 1)

    by_assistant: dict[str, dict[str, Any]] = {}
    for c in window:
        entry = by_assistant.setdefault(str(c.get("assistantId") or ""), {"calls": 0, "minutes": 0.0})
        entry["calls"] += 1
        entry["minutes"] += float(c.get("durationSeconds") or 0) / 60

    return {
        "days": days,
        "calls": len(window),
        "inProgress": sum(1 for c in window if c.get("status") in ("queued", "in-progress")),
        "failed": len(failed),
        "successRate": round(100 * (len(ended) - len(failed)) / len(ended)) if ended else None,
        "minutes": round(minutes, 1),
        "averageDurationSeconds": round(seconds / len(window)) if window else None,
        "turns": len(turns),
        "latencyMedianMs": _median([t["turnLatency"] for t in turns]),
        "latencyAverageMs": round(sum(t["turnLatency"] for t in turns) / len(turns)) if turns else None,
        "latencyParts": {p: round(sum(t.get(p, 0) for t in turns) / len(turns)) if turns else None
                         for p in LATENCY_PARTS},
        "platformFeeSaved": round(minutes * PLATFORM_FEE_PER_MINUTE, 2),
        "byType": dict(Counter(str(c.get("type") or "unknown") for c in window)),
        "endedReasons": dict(Counter(str(c.get("endedReason")) for c in ended).most_common(8)),
        "byAssistant": sorted(
            (
                {"assistantId": k, "calls": v["calls"], "minutes": round(v["minutes"], 1)}
                for k, v in by_assistant.items()
            ),
            key=lambda x: -x["calls"],
        )[:5],
        "perDay": list(per_day.values()),
    }
