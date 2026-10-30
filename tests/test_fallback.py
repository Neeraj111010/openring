"""Failover between providers during a call: which errors move to the next one, and never back."""

import asyncio

from pipecat.frames.frames import ErrorFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.utils.errors import ErrorCategory

from openring.fallback import Failover


def error(service, category):
    frame = ErrorFrame(error="boom", processor=service)
    frame.category = category
    return frame


def test_failover_moves_on_and_never_returns_to_a_failed_provider():
    async def go():
        first, second, third = FrameProcessor(), FrameProcessor(), FrameProcessor()
        strategy = Failover([first, second, third])
        assert await strategy.handle_error(error(first, ErrorCategory.APPLICATION)) is None  # a hiccup: stay
        assert await strategy.handle_error(error(first, ErrorCategory.QUOTA)) is second  # out of credit
        assert strategy.active_service is second
        assert await strategy.handle_error(error(second, ErrorCategory.SERVER)) is third
        assert await strategy.handle_error(error(third, ErrorCategory.CONNECTIVITY)) is None  # nothing left
        assert strategy.active_service is third

    asyncio.run(go())


def test_failover_follows_an_unusable_provider_whatever_the_category():
    async def go():
        first, second = FrameProcessor(), FrameProcessor()
        strategy = Failover([first, second])
        await first.set_usable(False)
        assert await strategy.handle_error(error(first, ErrorCategory.UNKNOWN)) is second

    asyncio.run(go())


def test_a_failing_provider_hands_the_next_frames_to_its_fallback():
    from pipecat.frames.frames import TextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from openring.fallback import switcher

    class OutOfCredit(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TextFrame):
                await self.push_error("402 Payment Required", category=ErrorCategory.QUOTA)
            else:
                await self.push_frame(frame, direction)

    class Backup(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TextFrame):
                frame = TextFrame(text="backup: " + frame.text)
            await self.push_frame(frame, direction)

    async def go():
        down, _ = await run_test(
            switcher([OutOfCredit(), Backup()]),
            frames_to_send=[TextFrame(text="one"), SleepFrame(sleep=0.1), TextFrame(text="two")],
            expected_down_frames=[TextFrame],
        )
        return [f.text for f in down if isinstance(f, TextFrame)]

    assert asyncio.run(go()) == ["backup: two"]  # "one" was lost with the failed provider; "two" got through
