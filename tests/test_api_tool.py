"""The apiRequest tool, in Vapi's format: the model fills in the body, OpenRing sends it to your URL."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

from openring import netguard, tools
from openring.store import Store
from openring.tools import CallControls, ToolError, assistant_tools, call_api, check_tool, function_schema

from test_api import ASSISTANT, AUTH, make_client

WEBHOOK = "https://hook.example.com/secret-path-123"
BOOK = {
    "type": "apiRequest",
    "name": "book_visit",
    "description": "Book a visit",
    "method": "POST",
    "url": WEBHOOK,
    "headers": {"type": "object", "properties": {"Authorization": {"type": "string", "value": "Bearer s3cret"},
                                                 "X-Call": {"type": "string", "value": "{{call.id}}"}}},
    "body": {"type": "object", "properties": {"day": {"type": "string"}, "source": {"type": "string"}},
             "required": ["day"]},
    "parameters": [{"key": "source", "value": "phone"}, {"key": "caller", "value": "{{customer.number}}"}],
}
CALL = {"id": "c1", "type": "inboundPhoneCall", "customer": {"number": "+15550000001", "name": "Maria/../x y"},
        "assistantOverrides": {"variableValues": {"plan": "gold"}}}


def run(tool, arguments=None, handle=None, call=CALL):
    """Send the tool through a stub server; returns (what the model reads, the requests that went out)."""
    seen = []

    def answer(request):
        seen.append(request)
        return handle(request) if handle else httpx.Response(200, text="Booked for Monday.")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await call_api(tool, arguments or {}, call=call, client=client)

    return asyncio.run(go()), seen


def test_the_model_fills_in_the_body_and_the_rest_is_yours():
    assert function_schema(BOOK) == {"name": "book_visit", "description": "Book a visit",
                                     "properties": BOOK["body"]["properties"], "required": ["day"]}
    text, [request] = run(BOOK, {"day": "Monday", "source": "the model says otherwise"})
    assert text == "Booked for Monday."
    assert (request.method, str(request.url)) == ("POST", WEBHOOK)
    # Static parameters win over the model, and can use the call's variables.
    assert json.loads(request.content) == {"day": "Monday", "source": "phone", "caller": "+15550000001"}
    assert request.headers["authorization"] == "Bearer s3cret" and request.headers["x-call"] == "c1"


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_a_request_with_no_body_carries_the_values_in_its_query(method):
    tool = {**BOOK, "method": method, "url": WEBHOOK + "?a=1", "parameters": []}
    _, [request] = run(tool, {"day": "Mon day", "count": 2, "skip": None})
    assert request.method == method and not request.content
    assert str(request.url) == WEBHOOK + "?a=1&day=Mon+day&count=2"


def test_variables_cannot_change_where_the_request_goes():
    tool = {**BOOK, "url": "https://hook.example.com/{{customer.name}}/{{plan}}?n={{customer.number}}"}
    _, [request] = run(tool)
    assert request.url.host == "hook.example.com"
    assert request.url.raw_path == b"/Maria%2F..%2Fx%20y/gold?n=%2B15550000001"  # a value can't add path segments
    with pytest.raises(ToolError, match="not in its host"):
        check_tool({**BOOK, "url": "https://{{customer.name}}.example.com/x"})


def test_the_model_cannot_choose_the_url_or_headers():
    _, [request] = run(BOOK, {"url": "https://evil.example.com", "headers": {"Authorization": "x"}, "day": "Mon"})
    assert request.url.host == "hook.example.com" and request.headers["authorization"] == "Bearer s3cret"


@pytest.mark.parametrize("change, words", [
    ({"name": ""}, "name"),
    ({"name": "has spaces"}, "name"),
    ({"method": "TRACE"}, "method"),
    ({"method": None}, "method"),
    ({"url": "ftp://x.example.com"}, "url"),
    ({"url": ""}, "url"),
    ({"timeoutSeconds": 0}, "timeoutSeconds"),
    ({"timeoutSeconds": 301}, "timeoutSeconds"),
    ({"timeoutSeconds": "20"}, "timeoutSeconds"),
    ({"body": "text"}, "body"),
    ({"headers": ["x"]}, "headers"),
    ({"parameters": {"a": 1}}, "parameters"),
    ({"parameters": [{"value": 1}]}, "parameters"),
    ({"credentialId": "cred-1"}, "credentialId"),
])
def test_a_tool_that_cannot_work_is_refused(change, words):
    with pytest.raises(ToolError, match=words):
        check_tool({**BOOK, **change})


def _error(tool=BOOK, **kwargs):
    with pytest.raises(ToolError) as caught:
        run(tool, **kwargs)
    return str(caught.value)


@pytest.mark.parametrize("status", [199, 301, 302, 404, 500])
def test_only_a_2xx_answer_counts_and_redirects_are_not_followed(status):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": "http://169.254.169.254/"}, text="the server's own words")

    error = _error(handle=handle)
    assert f"HTTP {status}" in error and "own words" not in error
    assert len(seen) == 1


def _raises(exc):
    def handle(request):
        raise exc
    return handle


@pytest.mark.parametrize("handle", [_raises(httpx.ConnectError(f"cannot reach {WEBHOOK}")),
                                    _raises(httpx.ReadTimeout(f"timeout on {WEBHOOK}")),
                                    _raises(httpx.InvalidURL(f"bad {WEBHOOK}"))])
def test_errors_never_carry_the_url_or_the_key(handle):
    error = _error(handle=handle)
    assert "could not send" in error
    assert "secret-path" not in error and "s3cret" not in error


def test_a_real_request_with_a_callers_name_in_a_header():
    """Against a real server, through the guarded client: any language, and no way to add a header."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(path=self.path, headers=self.headers, body=self.rfile.read(int(self.headers["Content-Length"])))
            answer = json.dumps({"booked": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        tool = {**BOOK, "url": f"http://127.0.0.1:{server.server_port}/visits",
                "headers": {"properties": {"X-Name": {"value": "{{customer.name}}"}}}}
        call = {**CALL, "customer": {"name": "João\r\nX-Admin: yes"}}
        text = asyncio.run(call_api(tool, {"day": "Mon"}, call=call))
    finally:
        server.shutdown()
        server.server_close()
    assert json.loads(text) == {"booked": True} and seen["path"] == "/visits"
    assert "X-Admin" not in seen["headers"]
    assert seen["headers"]["X-Name"].encode("latin-1").decode("utf-8") == "João X-Admin: yes"
    assert json.loads(seen["body"])["day"] == "Mon"


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/x"])
def test_cloud_metadata_addresses_are_refused(url):
    error = _error({**BOOK, "url": url})
    assert "refused" in error and "169.254" not in error


def test_every_request_goes_through_the_guard_when_the_tool_makes_its_own_client(monkeypatch):
    made = []
    real = netguard.client

    def client(**options):
        made.append(options)
        return real(**options)

    monkeypatch.setattr(netguard, "client", client)
    with pytest.raises(ToolError):
        asyncio.run(call_api({**BOOK, "url": "http://127.0.0.1:1/x"}, {}, call=CALL))
    assert made  # netguard.client checks each send, redirects included


def test_a_slow_server_gets_the_timeout_and_the_model_hears_it_may_have_gone_through():
    async def slow_stream():
        yield b"first"
        await asyncio.sleep(5)
        yield b"never"

    async def go():
        handle = lambda request: httpx.Response(200, content=slow_stream())  # noqa: E731
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await call_api({**BOOK, "timeoutSeconds": 0.1}, {}, call=CALL, client=client)

    with pytest.raises(ToolError, match="may or may not have gone through"):
        asyncio.run(asyncio.wait_for(go(), 3))


def test_a_huge_answer_is_not_downloaded_and_the_model_reads_a_little_of_it():
    sent = []

    async def endless():
        while True:
            sent.append(1)
            yield b"x" * 10_000

    text, _ = run(BOOK, handle=lambda request: httpx.Response(200, content=endless()))
    assert text.endswith("[answer cut short]") and len(text) < tools.MAX_RESULT_CHARS + 30
    assert len(sent) * 10_000 <= tools.MAX_RESPONSE_BYTES + 10_000


def test_a_binary_answer_stays_away_from_the_model():
    text, _ = run(BOOK, handle=lambda request: httpx.Response(200, content=b"\x89PNG\x00\x00\xff"))
    assert text == "Done. The answer is not text."


def test_an_empty_answer_is_done():
    assert run(BOOK, handle=lambda request: httpx.Response(204))[0] == "Done."


def _controls():
    log = []

    async def speak(text):
        log.append(text)

    async def nothing(*_):
        return None

    return CallControls(speak=speak, end_call=nothing, transfer=None, current_call=lambda: CALL,
                        assistant={"name": "Clinic"}, default_url=None), log


def _run_handler(tool, monkeypatch, api):
    """The live-call handler with `api` in place of the request. Returns (what's spoken, what the model reads)."""
    monkeypatch.setattr(tools, "call_api", api)
    controls, log = _controls()
    results = []

    async def result_callback(result, **_):
        results.append(result)

    params = SimpleNamespace(tool_call_id="t1", arguments={"day": "Mon"}, result_callback=result_callback)
    asyncio.run(tools.make_handler(tool, controls)(params))
    return log, results[0]


async def _fails(*_a, **_k):
    raise ToolError("the request failed with HTTP 500")


def test_a_failure_message_for_the_model_is_never_read_out(monkeypatch):
    message = "Apologize and offer to try again only after the caller agrees."
    tool = {**BOOK, "messages": [{"type": "request-failed", "role": "system", "content": message}]}
    log, result = _run_handler(tool, monkeypatch, _fails)
    assert log == []
    assert "HTTP 500" in result and message in result


def test_a_failure_message_for_the_caller_is_said(monkeypatch):
    tool = {**BOOK, "messages": [{"type": "request-start", "content": "One moment."},
                                 {"type": "request-failed", "content": "Sorry, that didn't work."}]}
    log, result = _run_handler(tool, monkeypatch, _fails)
    assert log == ["One moment.", "Sorry, that didn't work."]
    assert "HTTP 500" in result


@pytest.mark.parametrize("wait, spoken", [(0.3, ["Still checking."]), (0.0, [])])
def test_the_delayed_message_is_said_only_when_the_server_is_late(monkeypatch, wait, spoken):
    async def api(*_a, **_k):
        await asyncio.sleep(wait)
        return "Booked."

    tool = {**BOOK, "messages": [{"type": "request-response-delayed", "timingMilliseconds": 50,
                                  "content": "Still checking."}]}
    log, result = _run_handler(tool, monkeypatch, api)
    assert log == spoken and result == "Booked."


def test_function_tools_get_the_same_messages(monkeypatch):
    async def server(*_a, **_k):
        await asyncio.sleep(0.2)
        raise ToolError("the tool's server answered HTTP 500")

    monkeypatch.setattr(tools, "call_server", server)
    controls, log = _controls()
    results = []

    async def result_callback(result, **_):
        results.append(result)

    tool = {"type": "function", "function": {"name": "f"}, "messages": [
        {"type": "request-response-delayed", "timingMilliseconds": 20, "content": "Still checking."},
        {"type": "request-failed", "role": "system", "content": "Say sorry."}]}
    params = SimpleNamespace(tool_call_id="t1", arguments={}, result_callback=result_callback)
    asyncio.run(tools.make_handler(tool, controls)(params))
    assert log == ["Still checking."] and results[0].endswith("Say sorry.")


class FakeLLM:
    def __init__(self):
        self.registered = {}

    def register_function(self, name, handler, **options):
        self.registered[name] = options


def test_it_is_registered_like_any_tool_and_an_interruption_does_not_cancel_it():
    llm = FakeLLM()
    schema = tools.register_tools(llm, [{**BOOK, "timeoutSeconds": 40}], _controls()[0])
    assert [f.name for f in schema.standard_tools] == ["book_visit"]
    # Already sent, it may have gone through; cancelled, the model would just send it again.
    assert llm.registered["book_visit"] == {"cancel_on_interruption": False, "timeout_secs": 45.0}


def test_an_assistant_can_use_it_inline_or_saved():
    store = Store(":memory:")
    saved = store.create("tools", {**BOOK, "name": "saved_one"})
    assistant = {"model": {"tools": [BOOK], "toolIds": [saved["id"]]}}
    assert [tools.tool_name(t) for t in assistant_tools(assistant, store)] == ["book_visit", "saved_one"]


def test_the_api_takes_it_and_refuses_a_bad_one():
    client = make_client()
    assert client.post("/tool", json=BOOK, headers=AUTH).status_code == 201
    r = client.post("/tool", json={**BOOK, "method": "TRACE"}, headers=AUTH)
    assert r.status_code == 400 and "method" in r.json()["detail"]
    on_assistant = {**ASSISTANT, "model": {**ASSISTANT["model"], "tools": [BOOK]}}
    assert client.post("/assistant", json=on_assistant, headers=AUTH).status_code == 201
