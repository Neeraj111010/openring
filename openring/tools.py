"""Assistant tools in Vapi's format: functions on your server, ending the call and transferring it.

An assistant lists tools in `model.tools`, or saves them with `POST /tool` and lists their ids in
`model.toolIds`. When the model calls a `function` tool, OpenRing POSTs Vapi's `tool-calls` message
to the tool's server (or the assistant's) and hands the model the `results` it answers with, so a
server written for Vapi works unchanged.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from .store import Store

# Tool types OpenRing runs. Assistants imported with other types still load; those tools are skipped.
SUPPORTED = ("function", "endCall", "transferCall")
DEFAULT_TIMEOUT = 20  # seconds, like Vapi's server.timeoutSeconds default


class ToolError(ValueError):
    """A tool definition OpenRing cannot use, or a tool server that failed."""


def tool_name(tool: dict[str, Any]) -> str:
    """The function name the model sees."""
    if tool.get("type") == "function":
        return str((tool.get("function") or {}).get("name") or "")
    return str(tool.get("type"))  # Vapi exposes endCall and transferCall under their type names


def check_tool(tool: dict[str, Any]) -> None:
    """Refuse a tool that cannot work; unknown types are allowed (and skipped on calls)."""
    kind = tool.get("type")
    if not kind:
        raise ToolError("tool.type is required, e.g. 'function', 'endCall' or 'transferCall'")
    if kind == "function":
        fn = tool.get("function") or {}
        if not fn.get("name"):
            raise ToolError("a function tool needs function.name")
        if not str(fn["name"]).replace("_", "").replace("-", "").isalnum():
            raise ToolError("function.name may only have letters, digits, '_' and '-'")
    if kind == "transferCall":
        if not any(d.get("number") for d in tool.get("destinations") or []):
            raise ToolError("a transferCall tool needs destinations with a phone number")


def assistant_tools(assistant: dict[str, Any], store: Store) -> list[dict[str, Any]]:
    """The tools a call can use: `model.tools` plus the saved tools in `model.toolIds`."""
    model = assistant.get("model") or {}
    tools = list(model.get("tools") or [])
    for tool_id in model.get("toolIds") or []:
        if doc := store.get("tools", str(tool_id)):
            tools.append(doc)
        else:
            logger.warning("tool {} was deleted; the call runs without it", tool_id)
    usable = []
    for tool in tools:
        if tool.get("type") not in SUPPORTED:
            logger.warning("tool type '{}' is not supported yet; the call runs without it", tool.get("type"))
            continue
        if tool_name(tool) in {tool_name(t) for t in usable}:
            continue  # the model needs unique names
        usable.append(tool)
    return usable


def function_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """name, description and JSON-schema parameters, as the model sees the tool."""
    if tool["type"] == "function":
        fn = tool["function"]
        params = fn.get("parameters") or {}
        return {
            "name": fn["name"],
            "description": fn.get("description") or "",
            "properties": params.get("properties") or {},
            "required": list(params.get("required") or []),
        }
    if tool["type"] == "endCall":
        return {
            "name": "endCall",
            "description": (tool.get("function") or {}).get("description")
            or "End the call. Use it after saying goodbye, when the conversation is over.",
            "properties": {},
            "required": [],
        }
    destinations = [d for d in tool.get("destinations") or [] if d.get("number")]
    options = "; ".join(f"{d['number']}: {d.get('description') or 'no description'}" for d in destinations)
    return {
        "name": "transferCall",
        "description": (tool.get("function") or {}).get("description")
        or f"Transfer the caller to a person or another line. Destinations: {options}",
        "properties": {
            "destination": {
                "type": "string",
                "enum": [d["number"] for d in destinations],
                "description": "The number to transfer the call to.",
            }
        },
        "required": ["destination"],
    }


def tool_message(tool: dict[str, Any], kind: str) -> str | None:
    """What the assistant says at a moment of the tool call, e.g. 'request-start' or 'request-failed'."""
    for message in tool.get("messages") or []:
        if message.get("type") == kind and message.get("content"):
            return str(message["content"])
    return None


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_calls_message(
    tool: dict[str, Any], tool_call_id: str, arguments: dict[str, Any], *, call: dict | None, assistant: dict | None
) -> dict[str, Any]:
    """Vapi's server message for one tool call."""
    tool_call = {"id": tool_call_id, "type": "function", "function": {"name": tool_name(tool), "arguments": arguments}}
    return {
        "message": {
            "type": "tool-calls",
            "timestamp": int(time.time() * 1000),
            "toolCallList": [tool_call],
            "toolWithToolCallList": [{**tool, "toolCall": tool_call}],
            "call": call,
            "assistant": assistant,
        }
    }


def _result_text(body: Any, tool_call_id: str, raw: str) -> str:
    """The text the model reads back, from Vapi's {"results": [{"toolCallId", "result" | "error"}]}."""
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        results = [r for r in body["results"] if isinstance(r, dict)]
        match = next((r for r in results if r.get("toolCallId") == tool_call_id), results[0] if results else None)
        if match is None:
            return "The tool returned no result."
        if match.get("error") is not None:
            raise ToolError(str(match["error"]))
        result = match.get("result")
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return raw.strip() or "Done."


async def call_server(
    tool: dict[str, Any],
    tool_call_id: str,
    arguments: dict[str, Any],
    *,
    default_url: str | None,
    call: dict | None = None,
    assistant: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Run a function tool on its server and return what the model should read."""
    server = tool.get("server") or {}
    url = server.get("url") or default_url
    if not url:
        raise ToolError(f"tool '{tool_name(tool)}' has no server.url, and the assistant has none either")
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    if server.get("secret"):
        headers.setdefault("X-Vapi-Secret", str(server["secret"]))  # the header Vapi servers check
    payload = tool_calls_message(tool, tool_call_id, arguments, call=call, assistant=assistant)
    timeout = float(server.get("timeoutSeconds") or DEFAULT_TIMEOUT)
    owned = client is None
    client = client or httpx.AsyncClient()
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ToolError(f"could not reach {url}: {exc.__class__.__name__}") from exc
    finally:
        if owned:
            await client.aclose()
    if r.status_code >= 400:
        raise ToolError(f"{url} answered HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        body = None
    return _result_text(body, tool_call_id, r.text)


@dataclass
class CallControls:
    """What tools can do to the live call; run_call fills these in."""

    speak: Callable[[str], Awaitable[None]]
    end_call: Callable[[], Awaitable[None]]
    # Transfers need a phone line; browser test calls have none.
    transfer: Callable[[str, str | None], Awaitable[None]] | None
    current_call: Callable[[], dict | None]
    assistant: dict[str, Any]
    default_url: str | None


def make_handler(tool: dict[str, Any], controls: CallControls):
    """The Pipecat function-call handler for one tool."""
    from pipecat.frames.frames import FunctionCallResultProperties

    async def run_function(params) -> None:
        if start := tool_message(tool, "request-start"):
            await controls.speak(start)
        try:
            result = await call_server(
                tool,
                params.tool_call_id,
                _arguments(params.arguments),
                default_url=controls.default_url,
                call=controls.current_call(),
                assistant=controls.assistant,
            )
        except ToolError as exc:
            logger.warning("tool {} failed: {}", tool_name(tool), exc)
            if failed := tool_message(tool, "request-failed"):
                await controls.speak(failed)
            result = f"The tool failed: {exc}. Tell the caller you could not complete it."
        await params.result_callback(result)

    async def end_call(params) -> None:
        if start := tool_message(tool, "request-start"):
            await controls.speak(start)
        await params.result_callback("The call is ending.", properties=FunctionCallResultProperties(run_llm=False))
        await controls.end_call()

    async def transfer_call(params) -> None:
        destinations = [d for d in tool.get("destinations") or [] if d.get("number")]
        wanted = str(_arguments(params.arguments).get("destination") or "")
        destination = next((d for d in destinations if d["number"] == wanted), None)
        if destination is None and len(destinations) == 1:
            destination = destinations[0]
        if destination is None:
            await params.result_callback(f"Unknown destination '{wanted}'. Choose one of the listed numbers.")
            return
        if controls.transfer is None:
            await params.result_callback(
                "Transfers only work on Twilio phone calls for now. Tell the caller you cannot transfer."
            )
            return
        await params.result_callback(
            "Transferring the call.", properties=FunctionCallResultProperties(run_llm=False)
        )
        message = destination.get("message") or tool_message(tool, "request-start")
        await controls.transfer(destination["number"], message)

    return {"function": run_function, "endCall": end_call, "transferCall": transfer_call}[tool["type"]]


def register_tools(llm: Any, tools: list[dict[str, Any]], controls: CallControls) -> Any:
    """Give the model its tools; returns the ToolsSchema for the call's LLM context (None without tools)."""
    if not tools:
        return None
    from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
    from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema

    schemas = []
    for tool in tools:
        schemas.append(FunctionSchema(**function_schema(tool)))
        timeout = float((tool.get("server") or {}).get("timeoutSeconds") or DEFAULT_TIMEOUT) + 5
        # Vapi's async tools don't hold the conversation while the server works.
        llm.register_function(
            tool_name(tool),
            make_handler(tool, controls),
            cancel_on_interruption=not tool.get("async"),
            timeout_secs=timeout,
        )
    return ToolsSchema(standard_tools=schemas)
