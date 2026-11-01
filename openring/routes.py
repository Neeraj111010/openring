"""Model routes, like OmniRoute's combos: a saved list of models, in order, that assistants share.

An assistant with `model.routeId` gets the route's first model, with the others as its `fallbackModels`:
the next one takes over when a model fails (a rejected key, no credit left, a rate limit, an outage) or,
with `maxLatencyMs`, when it takes longer than that to start answering. A model that failed or was slow
rests for `cooldownMinutes`, across calls: the next calls start on the model after it, until it is back.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

DEFAULT_COOLDOWN = 5  # minutes
MAX_MODELS = 10
# Resting models, (provider, model) -> when they may lead a route again (time.monotonic()).
_resting: dict[tuple[str, str], float] = {}


def rest(provider: str, model: str, minutes: float) -> None:
    if minutes > 0:
        _resting[(provider, model)] = time.monotonic() + minutes * 60


def resting(provider: str, model: str) -> int:
    """Seconds left before this model may lead a route again; 0 when it can."""
    left = _resting.get((provider, model), 0) - time.monotonic()
    return max(round(left), 0)


def let_down(route: dict[str, Any] | None, service: Any) -> None:
    """A call's route moved off this model service: rest the model for the route's cooldown."""
    target = getattr(service, "openring_model", None)  # (provider, model), set by build_services
    if route and target:
        rest(*target, route.get("cooldownMinutes", DEFAULT_COOLDOWN))


def ordered(route: dict[str, Any]) -> list[dict[str, Any]]:
    """The route's models, the resting ones moved to the end (still there in case every other one fails)."""
    models = [m for m in route.get("models") or [] if isinstance(m, dict)]
    return sorted(models, key=lambda m: resting(str(m.get("provider")), str(m.get("model"))) > 0)


def expand(assistant: dict[str, Any], lookup: Callable[[str], dict[str, Any] | None]) -> dict[str, Any]:
    """The assistant with its route's models filled in. Without a route (or when it was deleted) the assistant
    keeps its own provider and model; the dashboard saves the route's first one there."""
    model = assistant.get("model") or {}
    route = lookup(str(model["routeId"])) if model.get("routeId") else None
    if not route or not route.get("models"):
        return assistant
    first, *others = ordered(route)
    shared = {k: v for k, v in model.items() if k not in ("provider", "model", "url", "apiKey", "fallbackModels")}
    settings = {"id": route["id"], "name": route.get("name"), "maxLatencyMs": route.get("maxLatencyMs"),
                "cooldownMinutes": route.get("cooldownMinutes", DEFAULT_COOLDOWN)}
    return {**assistant, "model": {**shared, **first, "fallbackModels": others, "route": settings}}


def clean(body: dict[str, Any]) -> dict[str, Any]:
    """A route as stored. Raises ValueError for anything a call would trip on."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    models = body.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError('models is required: in order, e.g. [{"provider": "groq", "model": "openai/gpt-oss-20b"}]')
    if len(models) > MAX_MODELS:
        raise ValueError(f"a route holds up to {MAX_MODELS} models")
    cleaned = []
    for i, m in enumerate(models):
        if not isinstance(m, dict) or not m.get("provider") or not m.get("model"):
            raise ValueError(f"models[{i}] needs a provider and a model")
        cleaned.append({k: m[k] for k in ("provider", "model", "url", "apiKey") if m.get(k)})
    latency = body.get("maxLatencyMs")
    if latency is not None:
        if not isinstance(latency, (int, float)) or not 300 <= latency <= 30_000:
            raise ValueError("maxLatencyMs goes from 300 to 30000, or null to switch on errors only")
        latency = int(latency)
    cooldown = body.get("cooldownMinutes", DEFAULT_COOLDOWN)
    if not isinstance(cooldown, (int, float)) or not 0 <= cooldown <= 1440:
        raise ValueError("cooldownMinutes goes from 0 (never rest a model) to 1440")
    return {**body, "name": name, "models": cleaned, "maxLatencyMs": latency, "cooldownMinutes": cooldown}


def with_status(route: dict[str, Any]) -> dict[str, Any]:
    """The route with how long each model still rests, for the dashboard."""
    return {**route, "models": [{**m, "restingSeconds": resting(str(m.get("provider")), str(m.get("model")))}
                                for m in route.get("models") or []]}
