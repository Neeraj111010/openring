"""Provider fallback during a call, as in Vapi's fallback plans: when a speech-to-text, model or voice provider
fails in a way a retry won't fix (a rejected key, no credit left, the provider down), the next one in the
assistant's list takes over for the rest of the call. Imported only when an assistant lists fallbacks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
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

    async def skip(self, service: Any) -> Any:
        """Leave `service` (too slow, say) for the next usable one. None when there is none."""
        self._failed.add(id(service))
        for candidate in self.services:
            if id(candidate) not in self._failed and candidate.is_usable:
                return await self._set_active_if_available(candidate)
        return None


def model_watch(max_latency_ms: int | None, on_slow: Callable[[float], Awaitable[None]]) -> Any:
    """A processor for right after a model switcher. When the switcher replaces a model, `replaced(old)` stops
    the old one and cleans up after it (Pipecat's switcher gates what goes into an inactive service, not what
    comes out), so the caller hears one answer and it reaches the transcript. With `max_latency_ms` it calls
    `on_slow(seconds)` when a model takes longer than that to start answering."""
    from pipecat.frames.frames import (
        FunctionCallInProgressFrame,
        FunctionCallsStartedFrame,
        InterruptionFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    output = (LLMTextFrame, LLMFullResponseEndFrame, FunctionCallsStartedFrame, FunctionCallInProgressFrame)

    class ModelWatch(FrameProcessor):
        def __init__(self):
            super().__init__(name="ModelWatch")
            self._open = 0  # responses started and not ended
            self._stale = False  # a replaced model's leftovers may still arrive, until the next model starts
            self._timer: asyncio.Task | None = None

        async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if self._stale and isinstance(frame, output):
                return  # the replaced model's: dropped
            if isinstance(frame, LLMFullResponseStartFrame):
                self._stale = False
                self._open += 1
                await self._disarm()
                if max_latency_ms:
                    self._timer = self.create_task(self._wait(), name="slow-model")
            elif isinstance(frame, (LLMTextFrame, FunctionCallsStartedFrame, InterruptionFrame)):
                await self._disarm()  # the model is answering, or the caller interrupted
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._open = max(self._open - 1, 0)
                await self._disarm()
            await self.push_frame(frame, direction)

        async def _wait(self) -> None:
            started = time.monotonic()
            await asyncio.sleep(max_latency_ms / 1000)
            self._timer = None
            await on_slow(time.monotonic() - started)

        async def _disarm(self) -> None:
            if self._timer is not None:
                timer, self._timer = self._timer, None
                await self.cancel_task(timer)

        async def replaced(self, old: Any) -> None:
            """The switcher moved off `old`: stop its generation, and end the response it left open."""
            await self._disarm()
            self._stale = True
            if old is not None:
                # Pipecat's own way to stop a generation (as for a withdrawn turn): it cancels the request.
                await old._start_interruption()
            while self._open:
                self._open -= 1
                await self.push_frame(LLMFullResponseEndFrame())

    return ModelWatch()


def switcher(services: list[Any], *, llm: bool = False) -> Any:
    """One processor that runs the first service and falls back to the others in order."""
    if llm:
        return LLMSwitcher(services, strategy_type=Failover)
    return ServiceSwitcher(services, strategy_type=Failover)
