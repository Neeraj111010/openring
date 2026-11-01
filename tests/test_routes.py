"""Model routes (like OmniRoute's combos): models in order, shared by assistants; the next one takes over when
one fails or is slow to answer, and a model that let the route down rests for a while, across calls."""

import asyncio
import re

import pytest

from openring import routes
from openring.providers import resolve_plan

from test_api import ASSISTANT, AUTH, make_client

ROUTE = {"name": "Fast, then cheap", "maxLatencyMs": 2000, "cooldownMinutes": 5, "models": [
    {"provider": "groq", "model": "openai/gpt-oss-20b"},
    {"provider": "openrouter", "model": "openai/gpt-4.1-mini"},
    {"provider": "ollama", "model": "llama3.2"},
]}


@pytest.fixture(autouse=True)
def nothing_resting(monkeypatch):
    monkeypatch.setattr(routes, "_resting", {})
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")


def lookup(route):
    return lambda _id: {**route, "id": "r1"}


def test_an_assistant_on_a_route_gets_its_models_in_order():
    assistant = {**ASSISTANT, "model": {**ASSISTANT["model"], "routeId": "r1", "temperature": 0.4}}
    plan = resolve_plan(routes.expand(assistant, lookup(ROUTE)))
    assert (plan.llm_provider, plan.llm_model, plan.temperature) == ("groq", "openai/gpt-oss-20b", 0.4)
    assert [(p.llm_provider, p.llm_model) for p in plan.fallbacks["model"]] == [
        ("openrouter", "openai/gpt-4.1-mini"), ("ollama", "llama3.2")]
    assert plan.max_latency_ms == 2000 and plan.route["cooldownMinutes"] == 5
    assert plan.fallbacks["model"][0].messages == plan.messages  # the instructions follow every model
    gone = routes.expand(assistant, lambda _id: None)  # a deleted route: the assistant's own model
    assert gone["model"]["provider"] == ASSISTANT["model"]["provider"]


def test_a_model_that_let_the_route_down_rests_and_comes_back():
    routes.rest("groq", "openai/gpt-oss-20b", 5)
    assert 290 < routes.resting("groq", "openai/gpt-oss-20b") <= 300
    assert [m["provider"] for m in routes.ordered(ROUTE)] == ["openrouter", "ollama", "groq"]
    routes.rest("openrouter", "openai/gpt-4.1-mini", 0)  # cooldownMinutes 0: never rests
    assert routes.resting("openrouter", "openai/gpt-4.1-mini") == 0
    routes._resting[("groq", "openai/gpt-oss-20b")] = 0  # its time is up
    assert [m["provider"] for m in routes.ordered(ROUTE)] == ["groq", "openrouter", "ollama"]


def test_a_call_rests_the_model_its_route_moved_off():
    from openring.providers import build_services

    route = routes.expand({**ASSISTANT, "model": {"routeId": "r1"}}, lookup(ROUTE))
    _, llm, _ = build_services(resolve_plan(route))
    first, second = llm.services[0], llm.services[1]
    assert first.openring_model == ("groq", "openai/gpt-oss-20b")
    routes.let_down(None, first)  # not on a route: nothing rests
    assert routes.resting("groq", "openai/gpt-oss-20b") == 0
    routes.let_down(resolve_plan(route).route, first)
    assert routes.resting("groq", "openai/gpt-oss-20b") > 0 and routes.resting(*second.openring_model) == 0
    assert resolve_plan(routes.expand({**ASSISTANT, "model": {"routeId": "r1"}}, lookup(ROUTE))).llm_provider == (
        "openrouter")  # the next call starts on the next model


def test_route_bodies_are_checked():
    for change, words in (({"name": " "}, "name"), ({"models": []}, "models"), ({"models": [{"provider": "groq"}]},
                          "models[0]"), ({"maxLatencyMs": 50}, "maxLatencyMs"), ({"cooldownMinutes": -1}, "cooldown")):
        with pytest.raises(ValueError, match=re.escape(words)):
            routes.clean({**ROUTE, **change})
    assert routes.clean({**ROUTE, "maxLatencyMs": None})["maxLatencyMs"] is None  # errors only


def test_the_route_api_and_assistants_that_use_it():
    client = make_client()
    r = client.post("/model-route", json=ROUTE, headers=AUTH)
    assert r.status_code == 201, r.text
    route = r.json()
    assert [m["restingSeconds"] for m in route["models"]] == [0, 0, 0]
    bad = client.post("/model-route", json={**ROUTE, "models": [{"provider": "nope", "model": "x"}]}, headers=AUTH)
    assert bad.status_code == 400 and "models[0]" in bad.json()["detail"]
    on_route = {**ASSISTANT, "model": {**ASSISTANT["model"], "routeId": route["id"]}}
    assert client.post("/assistant", json=on_route, headers=AUTH).status_code == 201
    missing = {**ASSISTANT, "model": {**ASSISTANT["model"], "routeId": "made-up"}}
    assert client.post("/assistant", json=missing, headers=AUTH).status_code == 400
    renamed = client.patch(f"/model-route/{route['id']}", json={"name": "Renamed"}, headers=AUTH).json()
    assert renamed["name"] == "Renamed" and len(renamed["models"]) == 3
    assert [x["id"] for x in client.get("/model-route", headers=AUTH).json()] == [route["id"]]
    assert client.delete(f"/model-route/{route['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/model-route/{route['id']}", headers=AUTH).status_code == 404


@pytest.mark.parametrize("stubborn", [False, True])
def test_a_slow_model_hands_the_turn_to_the_next_one(stubborn):
    """Stopping the slow model cancels its request; a model that answers anyway (`stubborn`) is dropped."""
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TextFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.tests.utils import SleepFrame, run_test

    from openring.fallback import model_watch, switcher

    class Model(FrameProcessor):
        def __init__(self, name, delay):
            super().__init__(name=name)
            self.delay = delay

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if not isinstance(frame, TextFrame) or isinstance(frame, LLMTextFrame):
                await self.push_frame(frame, direction)
                return
            await self.push_frame(LLMFullResponseStartFrame())

            async def answer():
                await asyncio.sleep(self.delay)
                await self.push_frame(LLMTextFrame(text=self.name))
                await self.push_frame(LLMFullResponseEndFrame())

            if stubborn and self.delay:
                self.create_task(answer())  # out of reach of the interruption
            else:
                await answer()

    models = switcher([Model("slow", 0.5), Model("fast", 0.0)])
    slow_after = []

    async def too_slow(seconds):
        slow_after.append(seconds)
        old = models.strategy.active_service
        await models.strategy.skip(old)
        await watch.replaced(old)

    watch = model_watch(100, too_slow)

    async def go():
        down, _ = await run_test(
            Pipeline([models, watch]),
            frames_to_send=[TextFrame(text="hi"), SleepFrame(sleep=0.8), TextFrame(text="again")],
            expected_down_frames=None,
        )
        return [type(f).__name__ + (f":{f.text}" if isinstance(f, LLMTextFrame) else "") for f in down
                if isinstance(f, (LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame))]

    flow = asyncio.run(go())
    # The slow model's answer is dropped and its open response closed; the fast one answers the next turn.
    assert flow == ["LLMFullResponseStartFrame", "LLMFullResponseEndFrame", "LLMFullResponseStartFrame",
                    "LLMTextFrame:fast", "LLMFullResponseEndFrame"]
    assert len(slow_after) == 1 and slow_after[0] >= 0.08  # Windows timers are ~15 ms coarse
