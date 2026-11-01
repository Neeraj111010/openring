"""Model routes (like OmniRoute's combos): models in order, shared by assistants; the next one takes over when
one fails or is slow to answer, and a model that let the route down rests for a while, across calls."""

import asyncio
import re

import pytest

from openring import api as api_module
from openring import routes
from openring.providers import ProviderError, resolve_plan

from test_api import ASSISTANT, AUTH, make_client

ROUTE = {"name": "Fast, then cheap", "maxLatencyMs": 2000, "cooldownMinutes": 5, "models": [
    {"provider": "groq", "model": "openai/gpt-oss-20b"},
    {"provider": "openrouter", "model": "openai/gpt-4.1-mini"},
    {"provider": "ollama", "model": "llama3.2"},
]}


@pytest.fixture(autouse=True)
def nothing_resting(monkeypatch):
    monkeypatch.setattr(routes, "_resting", {})
    monkeypatch.setattr(routes, "_speed", {})
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


def test_assistants_on_a_deleted_route_keep_their_own_model_and_stay_editable():
    client = make_client()
    route = client.post("/model-route", json=ROUTE, headers=AUTH).json()
    on_route = {**ASSISTANT, "model": {**ASSISTANT["model"], "routeId": route["id"]}}
    a = client.post("/assistant", json=on_route, headers=AUTH).json()
    client.delete(f"/model-route/{route['id']}", headers=AUTH)
    model = client.get(f"/assistant/{a['id']}", headers=AUTH).json()["model"]
    assert "routeId" not in model and model["model"] == ASSISTANT["model"]["model"]
    assert client.patch(f"/assistant/{a['id']}", json={"name": "Renamed"}, headers=AUTH).status_code == 200
    # One left pointing at a route deleted by an older version can still be edited, but not moved to a made-up one.
    client.app.state.store.update("assistants", a["id"], {"model": {**model, "routeId": "deleted-earlier"}})
    assert client.patch(f"/assistant/{a['id']}", json={"name": "Again"}, headers=AUTH).status_code == 200
    moved = client.patch(f"/assistant/{a['id']}", json={"model": {**model, "routeId": "made-up"}}, headers=AUTH)
    assert moved.status_code == 400


def test_measuring_a_model_that_breaks_a_newer_rule_reports_it(monkeypatch):
    client = make_client()
    route = client.post("/model-route", json=ROUTE, headers=AUTH).json()

    async def probe(model, plan):
        return {"firstTokenMs": 300}

    def refuse_ollama(assistant, **options):  # as if a rule added since refused how this model was saved
        if assistant["model"]["provider"] == "ollama":
            raise ProviderError("model.url: refused")
        return real(assistant, **options)

    real = api_module.resolve_plan
    monkeypatch.setattr(routes, "probe", probe)
    monkeypatch.setattr(api_module, "resolve_plan", refuse_ollama)
    r = client.post(f"/model-route/{route['id']}/measure", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["measured"] == [{"firstTokenMs": 300}, {"firstTokenMs": 300}, {"error": "model.url: refused"}]


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


def test_a_preset_decides_the_order_the_wait_and_the_rest():
    fastest = routes.clean({**ROUTE, "preset": "fastest", "maxLatencyMs": 9000})
    assert (fastest["order"], fastest["maxLatencyMs"], fastest["cooldownMinutes"]) == ("fastest", 1500, 5)
    reliable = routes.clean({**ROUTE, "preset": "reliable"})
    assert (reliable["order"], reliable["maxLatencyMs"], reliable["cooldownMinutes"]) == ("listed", 2000, 15)
    custom = routes.clean({**ROUTE, "order": "cheapest", "maxLatencyMs": 2500})
    assert (custom["preset"], custom["order"], custom["maxLatencyMs"]) == ("custom", "cheapest", 2500)
    for change, words in (({"preset": "turbo"}, "preset"), ({"order": "random"}, "order")):
        with pytest.raises(ValueError, match=words):
            routes.clean({**ROUTE, **change})
    plan = resolve_plan(routes.expand({**ASSISTANT, "model": {"routeId": "r1"}}, lookup(fastest)))
    assert plan.max_latency_ms == 1500 and plan.route["preset"] == "fastest"


def test_fastest_puts_the_quickest_measured_model_first():
    route = {**ROUTE, "preset": "fastest"}
    assert [m["provider"] for m in routes.ordered(route)] == ["groq", "openrouter", "ollama"]  # nothing measured
    for ms in (900, 700, 800):
        routes.record_speed("groq", "openai/gpt-oss-20b", ms)
    routes.record_speed("ollama", "llama3.2", 300)
    assert routes.speed("groq", "openai/gpt-oss-20b") == 800  # the median
    # Measured ones first, quickest first; the one not measured yet after them.
    assert [m["provider"] for m in routes.ordered(route)] == ["ollama", "groq", "openrouter"]
    routes.rest("ollama", "llama3.2", 5)  # resting still goes last
    assert [m["provider"] for m in routes.ordered(route)] == ["groq", "openrouter", "ollama"]
    for _ in range(routes.SAMPLES + 5):
        routes.record_speed("groq", "openai/gpt-oss-20b", 100)
    assert len(routes._speed[("groq", "openai/gpt-oss-20b")]) == routes.SAMPLES


def test_cheapest_puts_free_and_low_prices_first():
    route = {**ROUTE, "preset": "cheapest", "models": [
        {"provider": "openai", "model": "gpt-4.1-mini"},
        {"provider": "custom-llm", "model": "mine", "url": "https://llm.example.com/v1"},  # no known price
        {"provider": "groq", "model": "openai/gpt-oss-20b"},
        {"provider": "ollama", "model": "llama3.2"},
    ]}
    assert [m["provider"] for m in routes.ordered(route)] == ["ollama", "groq", "openai", "custom-llm"]
    status = routes.with_status({**route, "id": "r1"})
    assert status["next"] == [3, 2, 0, 1]
    assert status["models"][3]["price"] == {"input": 0.0, "output": 0.0} and status["models"][1]["price"] is None


def test_speeds_survive_a_restart():
    from openring.store import Store

    store = Store(":memory:")
    routes.record_speed("groq", "openai/gpt-oss-20b", 420)
    routes.save_speed(store)
    routes._speed.clear()
    routes.load_speed(store)
    assert routes.speed("groq", "openai/gpt-oss-20b") == 420


def test_changing_a_setting_makes_a_route_custom_and_measuring_times_its_models(monkeypatch):
    client = make_client()
    route = client.post("/model-route", json={**ROUTE, "preset": "fastest"}, headers=AUTH).json()
    assert (route["preset"], route["maxLatencyMs"]) == ("fastest", 1500)
    renamed = client.patch(f"/model-route/{route['id']}", json={"name": "Quick"}, headers=AUTH).json()
    assert renamed["preset"] == "fastest"  # a name isn't a setting
    tuned = client.patch(f"/model-route/{route['id']}", json={"maxLatencyMs": 1800}, headers=AUTH).json()
    assert (tuned["preset"], tuned["order"], tuned["maxLatencyMs"]) == ("custom", "fastest", 1800)

    async def probe(model, plan):
        if model["provider"] == "ollama":
            return {"error": "Connection error."}
        routes.record_speed(model["provider"], model["model"], 250 if model["provider"] == "openrouter" else 600)
        return {"firstTokenMs": 250 if model["provider"] == "openrouter" else 600}

    monkeypatch.setattr(routes, "probe", probe)
    measured = client.post(f"/model-route/{route['id']}/measure", headers=AUTH).json()
    assert [m.get("firstTokenMs") for m in measured["measured"]] == [600, 250, None]
    assert measured["measured"][2]["error"] == "Connection error."
    assert measured["next"] == [1, 0, 2]  # fastest first; the one that didn't answer after the measured ones
    assert [m["firstTokenMs"] for m in measured["models"]] == [600, 250, None]


def test_measuring_a_model_without_its_key_says_so(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    model = {"provider": "groq", "model": "openai/gpt-oss-20b"}
    plan = resolve_plan({"model": model}, check_keys=False, with_fallbacks=False)
    assert asyncio.run(routes.probe(model, plan)) == {"error": "no key: add GROQ_API_KEY"}


def test_measuring_times_the_first_word_of_the_answer(monkeypatch):
    import openai

    class Chunk:
        def __init__(self, text):
            self.choices = [type("Choice", (), {"delta": type("Delta", (), {"content": text})()})()]

    class Stream:
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            for text in ("", None, "Yes"):  # role and reasoning chunks come before the answer
                await asyncio.sleep(0.05)
                yield Chunk(text)

        async def close(self):
            self.closed = True

    asked = {}

    class Client:
        def __init__(self, **options):
            asked["options"] = options
            self.chat = type("Chat", (), {"completions": self})()

        async def create(self, **body):
            asked["body"] = body
            return Stream()

        async def close(self):
            pass

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    model = {"provider": "openrouter", "model": "openai/gpt-4.1-mini"}
    plan = resolve_plan({"model": model}, check_keys=False, with_fallbacks=False)
    result = asyncio.run(routes.probe(model, plan))
    assert 120 <= result["firstTokenMs"] < 1000
    assert asked["body"]["stream"] is True and asked["options"]["base_url"] == "https://openrouter.ai/api/v1"
    assert routes.speed("openrouter", "openai/gpt-4.1-mini") == result["firstTokenMs"]
