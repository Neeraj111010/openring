"""Provider fallback during a call, as in Vapi's fallback plans: when a speech-to-text, model or voice provider
fails in a way a retry won't fix (a rejected key, no credit left, the provider down), the next one in the
assistant's list takes over for the rest of the call. Imported only when an assistant lists fallbacks.
"""

from __future__ import annotations

from typing import Any

from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual
from pipecat.utils.errors import ErrorCategory

# Failures worth moving to another provider for. Pipecat itself only switches on the first three (the ones
# that can never succeed); on a phone call, running out of credit, a rate limit or an outage are just as final.
SWITCH_ON = {
    ErrorCategory.AUTHENTICATION,
    ErrorCategory.AUTHORIZATION,
    ErrorCategory.INVALID_REQUEST,
    ErrorCategory.QUOTA,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.SERVER,
    ErrorCategory.CONNECTIVITY,
}


class Failover(ServiceSwitcherStrategyManual):
    """Moves to the next provider in the list; a provider that failed is not used again during the call."""

    def __init__(self, services: list[Any]):
        super().__init__(services)
        self._failed: set[int] = set()

    async def handle_error(self, error: Any) -> Any:
        failed = error.processor or self.active_service
        if failed.is_usable and error.category not in SWITCH_ON:
            return None  # a hiccup the provider carries on from
        self._failed.add(id(failed))
        for candidate in self.services:
            if id(candidate) not in self._failed and candidate.is_usable:
                return await self._set_active_if_available(candidate)
        return None  # nothing left to switch to: the error goes on as usual


def switcher(services: list[Any], *, llm: bool = False) -> Any:
    """One processor that runs the first service and falls back to the others in order."""
    if llm:
        return LLMSwitcher(services, strategy_type=Failover)
    return ServiceSwitcher(services, strategy_type=Failover)
