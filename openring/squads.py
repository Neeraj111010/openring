"""Vapi's squads: several assistants on one call, each with its own prompt, tools, model and voice, handing the caller
to one another with a `handoff` tool. The caller stays on the line; the conversation goes along.

A squad lists its `members` (saved assistants or inline ones); the first answers the call. A plain assistant with a
`handoff` tool works too: everyone it can reach comes along to the call.
"""

from __future__ import annotations

from typing import Any

from . import overrides
from .providers import ProviderError
from .store import Store
from .tools import assistant_tools, destination_key

MAX_MEMBERS = 20
MAX_HANDOFFS = 20  # per call: two models passing a caller back and forth would spend tokens for nothing


class SquadError(ProviderError):
    """A squad that can't take calls."""


def name_of(assistant: dict[str, Any]) -> str:
    return str(assistant.get("name") or assistant.get("id") or "")


def is_squad(target: dict[str, Any]) -> bool:
    return isinstance(target.get("members"), list)


def _member(member: Any, store: Store) -> dict[str, Any]:
    if not isinstance(member, dict):
        raise SquadError("each member of a squad is an object with assistantId or assistant")
    if isinstance(member.get("assistant"), dict):
        return member["assistant"]
    assistant = store.get("assistants", str(member.get("assistantId") or ""))
    if assistant is None:
        raise SquadError(f"members: assistant {member.get('assistantId') or '(none given)'} not found")
    return assistant


def _with_destinations(assistant: dict[str, Any], destinations: Any) -> dict[str, Any]:
    """A member's `assistantDestinations` (Vapi's older way) as a handoff tool."""
    if not destinations:
        return assistant
    if not isinstance(destinations, list):
        raise SquadError("assistantDestinations must be a list")
    model = dict(assistant.get("model") or {})
    model["tools"] = [*(model.get("tools") or []), {"type": "handoff", "destinations": destinations}]
    return {**assistant, "model": model}


def cast(
    target: dict[str, Any], store: Store, extra: dict[str, Any] | None = None, customer: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Everyone a call can reach, the one who answers first. `target` is a squad or an assistant; `extra` is the
    call's own overrides. Variables stay as written in everyone but the first, for what a handoff adds to them."""
    variables = {"variableValues": (extra or {}).get("variableValues")} if (extra or {}).get("variableValues") else {}
    team: list[dict[str, Any]] = []
    if is_squad(target):
        if not target["members"]:
            raise SquadError("a squad needs members")
        shared = overrides.combine(target.get("membersOverrides"), target.get("memberOverrides"))
        for member in target["members"]:
            own = _with_destinations(_member(member, store), member.get("assistantDestinations"))
            changes = overrides.combine(shared, member.get("assistantOverrides"), extra)
            team.append(overrides.apply(own, changes, customer, keep_unknown=bool(team)))
    else:
        team.append(overrides.apply(target, extra, customer))
    # Whoever a handoff tool names by id or inline, in the squad or not, comes along.
    position = 0
    while position < len(team):
        for tool in assistant_tools(team[position], store):
            for destination in tool.get("destinations") or [] if tool.get("type") == "handoff" else []:
                if find(team, destination) is None:
                    found = destination.get("assistant") if isinstance(destination.get("assistant"), dict) \
                        else store.get("assistants", str(destination.get("assistantId") or ""))
                    if found:
                        team.append(overrides.apply(found, variables, customer, keep_unknown=True))
        position += 1
        if len(team) > MAX_MEMBERS:
            raise SquadError(f"a call can reach at most {MAX_MEMBERS} assistants")
    return team


def find(team: list[Any], destination: dict[str, Any]) -> int | None:
    """Which of `team` (assistants, or plans holding them) a handoff destination means."""
    wanted = destination_key(destination)
    for position, member in enumerate(team):
        assistant = member if isinstance(member, dict) else member.assistant
        if wanted in (assistant.get("name"), assistant.get("id")):
            return position
    return None


def check(target: dict[str, Any], store: Store) -> None:
    """Refuse a squad, or a team around an assistant, whose handoffs can't work."""
    team = cast(target, store)
    names = [name_of(a) for a in team if name_of(a)]
    if len(names) != len(set(names)) or len(names) != len(team):
        raise SquadError("each assistant in a squad needs a name of its own: handoffs find them by it")
    for assistant in team:
        for tool in assistant_tools(assistant, store):
            for destination in tool.get("destinations") or [] if tool.get("type") == "handoff" else []:
                if find(team, destination) is None:
                    raise SquadError(f"{name_of(assistant)} hands off to '{destination_key(destination)}', "
                                     "which isn't in the squad")


def carry_over(
    messages: list[dict[str, Any]], previous: list[dict[str, Any]], plan: dict[str, Any] | None,
    handoff_call: str | set[str],
) -> list[dict[str, Any]]:
    """The earlier conversation the next assistant gets, Vapi's `contextEngineeringPlan`. `messages` is everything
    but the system prompt; `previous` what came before the current assistant. The handoff itself isn't part of it."""
    handoffs = {handoff_call} if isinstance(handoff_call, str) else handoff_call
    kind = (plan or {}).get("type") or "all"
    if kind == "none":
        return []
    if kind == "previousAssistantMessages":
        return list(previous)
    kept: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") in handoffs:
            continue
        calls = message.get("tool_calls")
        if calls and any(c.get("id") in handoffs for c in calls):
            others = [c for c in calls if c.get("id") not in handoffs]
            message = {k: v for k, v in message.items() if k != "tool_calls"}
            message.update({"tool_calls": others} if others else {})
            if not others and not message.get("content"):
                continue
        kept.append(message)
    if kind == "userAndAssistantMessages":
        kept = [{k: v for k, v in m.items() if k != "tool_calls"} for m in kept
                if m.get("role") in ("user", "assistant") and m.get("content")]
    elif kind == "lastNMessages" and (plan or {}).get("maxMessages"):
        kept = kept[-int(plan["maxMessages"]):]
        while kept and kept[0].get("role") == "tool":  # a result without its call: models refuse that
            kept.pop(0)
    return kept
