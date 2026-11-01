"""Model routes, like OmniRoute's combos: a saved list of models, in order, that assistants share.

An assistant with `model.routeId` gets the route's first model, with the others as its `fallbackModels`:
the next one takes over when a model fails (a rejected key, no credit left, a rate limit, an outage) or,
with `maxLatencyMs`, when it takes longer than that to start answering. A model that failed or was slow
rests for `cooldownMinutes`, across calls: the next calls start on the model after it, until it is back.

A route's `preset` sets how it behaves, like OmniRoute's mode packs, cut down to what a phone call needs:
answer fast, keep going, cost little. "fastest" puts the model with the quickest measured answers first,
"cheapest" the one with the lowest price, "reliable" keeps your order and rests a failing model longer.
"custom" leaves `order`, `maxLatencyMs` and `cooldownMinutes` to you.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from . import prices

DEFAULT_COOLDOWN = 5  # minutes
MAX_MODELS = 10
ORDERS = ("listed", "fastest", "cheapest")
PRESETS: dict[str, dict[str, Any]] = {
    "fastest": {"order": "fastest", "maxLatencyMs": 1500, "cooldownMinutes": 5},
    "cheapest": {"order": "cheapest", "maxLatencyMs": 3000, "cooldownMinutes": 5},
    "reliable": {"order": "listed", "maxLatencyMs": 2000, "cooldownMinutes": 15},
}
SETTINGS = ("order", "maxLatencyMs", "cooldownMinutes")  # what a preset decides; changing one means "custom"
SAMPLES = 20  # answer times kept per model, the most recent ones
SPEED_KEY = "model_speed"  # where they are saved (the store's settings table)
PROBE_TIMEOUT = 15  # seconds

# Resting models, (provider, model) -> when they may lead a route again (time.monotonic()).
_resting: dict[tuple[str, str], float] = {}
# Recent time to the first word of an answer, ms, per (provider, model): from calls and from `measure`.
_speed: dict[tuple[str, str], list[int]] = {}


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


def record_speed(provider: str, model: str, ms: float) -> None:
    samples = _speed.setdefault((provider, model), [])
    samples.append(round(ms))
    del samples[:-SAMPLES]


def speed(provider: str, model: str) -> int | None:
    """Median time to the first word of an answer, ms, over the recent ones; None before any."""
    samples = sorted(_speed.get((provider, model)) or [])
    if not samples:
        return None
    mid = len(samples) // 2
    return samples[mid] if len(samples) % 2 else round((samples[mid - 1] + samples[mid]) / 2)


def load_speed(store: Any) -> None:
    try:
        saved = json.loads(store.get_setting(SPEED_KEY) or "[]")
        for provider, model, samples in saved:
            _speed[(str(provider), str(model))] = [int(s) for s in samples][-SAMPLES:]
    except (ValueError, TypeError) as exc:
        logger.warning("saved model speeds ignored: {}", exc)


def save_speed(store: Any) -> None:
    store.set_setting(SPEED_KEY, json.dumps([[p, m, s] for (p, m), s in _speed.items()]))


def settings(route: dict[str, Any]) -> dict[str, Any]:
    """The route's order, latency limit and cooldown: its preset's, or its own when custom."""
    preset = PRESETS.get(str(route.get("preset")))
    if preset:
        return dict(preset)
    return {"order": route.get("order") or "listed", "maxLatencyMs": route.get("maxLatencyMs"),
            "cooldownMinutes": route.get("cooldownMinutes", DEFAULT_COOLDOWN)}


def ordered(route: dict[str, Any]) -> list[dict[str, Any]]:
    """The route's models in the order calls try them. Resting ones go last (still there in case every other
    one fails); "fastest" and "cheapest" put models not measured or priced yet after the others, as listed."""
    models = [m for m in route.get("models") or [] if isinstance(m, dict)]
    order = settings(route)["order"]

    def key(item: tuple[int, dict[str, Any]]) -> tuple[bool, bool, float, int]:
        i, m = item
        provider, model = str(m.get("provider")), str(m.get("model"))
        figure = speed(provider, model) if order == "fastest" else (
            prices.blended(provider, model) if order == "cheapest" else 0)
        return (resting(provider, model) > 0, figure is None, figure or 0, i)

    return [m for _, m in sorted(enumerate(models), key=key)]


def expand(assistant: dict[str, Any], lookup: Callable[[str], dict[str, Any] | None]) -> dict[str, Any]:
    """The assistant with its route's models filled in. Without a route (or when it was deleted) the assistant
    keeps its own provider and model; the dashboard saves the route's first one there."""
    model = assistant.get("model") or {}
    route = lookup(str(model["routeId"])) if model.get("routeId") else None
    if not route or not route.get("models"):
        return assistant
    first, *others = ordered(route)
    shared = {k: v for k, v in model.items() if k not in ("provider", "model", "url", "apiKey", "fallbackModels")}
    plan = {"id": route["id"], "name": route.get("name"), "preset": route.get("preset") or "custom",
            **settings(route)}
    return {**assistant, "model": {**shared, **first, "fallbackModels": others, "route": plan}}


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
    preset = body.get("preset") or "custom"
    if preset != "custom" and preset not in PRESETS:
        raise ValueError(f"preset is one of {', '.join([*PRESETS, 'custom'])}")
    if preset in PRESETS:  # the preset decides; what was sent for these is ignored
        return {**body, "name": name, "models": cleaned, "preset": preset, **PRESETS[preset]}
    order = body.get("order") or "listed"
    if order not in ORDERS:
        raise ValueError(f"order is one of {', '.join(ORDERS)}")
    latency = body.get("maxLatencyMs")
    if latency is not None:
        if not isinstance(latency, (int, float)) or not 300 <= latency <= 30_000:
            raise ValueError("maxLatencyMs goes from 300 to 30000, or null to switch on errors only")
        latency = int(latency)
    cooldown = body.get("cooldownMinutes", DEFAULT_COOLDOWN)
    if not isinstance(cooldown, (int, float)) or not 0 <= cooldown <= 1440:
        raise ValueError("cooldownMinutes goes from 0 (never rest a model) to 1440")
    return {**body, "name": name, "models": cleaned, "preset": "custom", "order": order,
            "maxLatencyMs": latency, "cooldownMinutes": cooldown}


def with_status(route: dict[str, Any]) -> dict[str, Any]:
    """The route for the dashboard: per model, how long it still rests, its measured speed and its price,
    plus `next`, the order the next call tries them in."""
    def status(m: dict[str, Any]) -> dict[str, Any]:
        provider, model = str(m.get("provider")), str(m.get("model"))
        price = prices.llm(provider, model)
        return {**m, "restingSeconds": resting(provider, model), "firstTokenMs": speed(provider, model),
                "samples": len(_speed.get((provider, model)) or []),
                "price": {"input": price[0], "output": price[1]} if price else None}

    return {**route, "preset": route.get("preset") or "custom", **settings(route),
            "models": [status(m) for m in route.get("models") or []],
            "next": [i for m in ordered(route) for i, x in enumerate(route.get("models") or []) if x is m]}


async def probe(model: dict[str, Any], plan: Any) -> dict[str, Any]:
    """Time one short answer from a model: how long until its first word, as on a call. `plan` is the model's
    resolved stack (base URL and key)."""
    from openai import AsyncOpenAI

    from .providers import LOCAL_LLMS, _llm_headers

    key = plan.llm_api_key or os.environ.get(plan.llm_api_key_env or "", "")
    if not key and plan.llm_provider not in LOCAL_LLMS and plan.llm_api_key_env:
        return {"error": f"no key: add {plan.llm_api_key_env}"}
    client = AsyncOpenAI(api_key=key or "not-needed", base_url=plan.llm_base_url,
                         default_headers=_llm_headers(plan.llm_provider), timeout=PROBE_TIMEOUT, max_retries=0)
    started = time.monotonic()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            stream = await client.chat.completions.create(
                model=plan.llm_model, stream=True,
                messages=[{"role": "system", "content": "You are on a phone call. Answer in a few words."},
                          {"role": "user", "content": "Hi, can you hear me?"}],
            )
            first = None
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    first = time.monotonic()
                    break
            await stream.close()
    except TimeoutError:
        return {"error": f"no answer after {PROBE_TIMEOUT} s"}
    except Exception as exc:  # a rejected key, an unknown model, a server that isn't running
        return {"error": str(getattr(exc, "message", None) or exc)[:200]}
    finally:
        await client.close()
    ms = round(((first or time.monotonic()) - started) * 1000)
    record_speed(str(model.get("provider")), str(model.get("model")), ms)
    return {"firstTokenMs": ms}
