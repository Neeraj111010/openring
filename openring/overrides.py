"""Vapi's `assistantOverrides`: per-call changes to an assistant, plus the `{{variables}}` its texts can use
(`{{customer.name}}`, or any key of `variableValues`), so one assistant can greet each person in a campaign by name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# `{{ name }}` or `{{customer.name}}`. Liquid filters (`{{ x | upcase }}`) are not supported and stay as written.
VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")
TEXT_FIELDS = ("firstMessage", "voicemailMessage", "endCallMessage")


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _lookup(context: dict[str, Any], path: str) -> str:
    value: Any = context
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return ""  # as on Vapi, an unknown variable says nothing rather than "{{name}}"
        value = value[part]
    return "" if value is None or isinstance(value, (dict, list)) else str(value)


def fill(text: str, context: dict[str, Any], encode: Callable[[str], str] = str) -> str:
    """The text with its variables' values in; `encode` makes a value safe where it lands (in a URL, say)."""
    return VARIABLE.sub(lambda m: encode(_lookup(context, m.group(1))), text)


def call_context(call: dict[str, Any] | None) -> dict[str, Any]:
    """The variables of a live call: `variableValues`, `{{customer.number}}` and `{{call.id}}`, `{{call.type}}`..."""
    call = call or {}
    values = (call.get("assistantOverrides") or {}).get("variableValues")
    return {**(values if isinstance(values, dict) else {}), "customer": call.get("customer") or {},
            "call": {k: v for k, v in call.items() if isinstance(v, (str, int, float))}}


def combine(*overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Later overrides win; `variableValues` add up (a campaign's, then each customer's)."""
    out: dict[str, Any] = {}
    for item in overrides:
        if isinstance(item, dict):
            out = _merge(out, item)
    return out


def changes_assistant(overrides: dict[str, Any] | None) -> bool:
    """Whether the overrides change more than variables, so the result needs checking like any assistant."""
    return any(key != "variableValues" for key in overrides or {})


def apply(
    assistant: dict[str, Any], overrides: dict[str, Any] | None = None, customer: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The assistant for one call: the overrides merged in, and its variables filled."""
    overrides = dict(overrides or {})
    values = overrides.pop("variableValues", None)
    out = _merge(assistant, overrides)
    context = {**(values if isinstance(values, dict) else {}), "customer": customer or {}}
    for key in TEXT_FIELDS:
        if isinstance(out.get(key), str):
            out[key] = fill(out[key], context)
    model = out.get("model")
    if isinstance(model, dict) and isinstance(model.get("messages"), list):
        out["model"] = {**model, "messages": [
            {**m, "content": fill(m["content"], context)} if isinstance(m, dict) and isinstance(m.get("content"), str)
            else m
            for m in model["messages"]
        ]}
    plan = out.get("messagePlan")
    if isinstance(plan, dict) and isinstance(plan.get("idleMessages"), list):
        out["messagePlan"] = {**plan, "idleMessages": [fill(m, context) if isinstance(m, str) else m
                                                       for m in plan["idleMessages"]]}
    return out
