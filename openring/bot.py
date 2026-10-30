"""Runs one live phone call: telephony websocket <-> STT -> LLM -> TTS."""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import WebSocket
from loguru import logger

from .config import Settings
from .providers import StackPlan, build_services
from .store import Store, now_iso


async def run_call(
    websocket: WebSocket,
    *,
    plan: StackPlan,
    call_data: dict[str, Any],
    call_id: str,
    store: Store,
    settings: Settings,
) -> None:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import EndFrame, LLMRunFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    has_creds = bool(settings.twilio_account_sid and settings.twilio_auth_token)
    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data.get("call_id"),
        account_sid=settings.twilio_account_sid or None,
        auth_token=settings.twilio_auth_token or None,
        # Auto hang-up uses the Twilio REST API, so it needs credentials.
        params=TwilioFrameSerializer.InputParams(auto_hang_up=has_creds),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt, llm, tts = build_services(plan)
    context = LLMContext(messages=list(plan.messages))
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000, enable_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        if plan.first_message:
            context.add_message({"role": "assistant", "content": plan.first_message})
            await task.queue_frames([TTSSpeakFrame(plan.first_message)])
        else:
            await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        await task.cancel()

    if plan.end_call_phrases:
        phrases = [p.lower() for p in plan.end_call_phrases]

        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def _maybe_hang_up(_aggregator, message):
            text = str(getattr(message, "content", message) or "").lower()
            if any(p in text for p in phrases):
                await task.queue_frames([EndFrame()])

    started = time.monotonic()  # the caller already marked the call in-progress
    ended_reason = "customer-ended-call"
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    except Exception as exc:  # keep the call record honest
        logger.exception("call {} failed", call_id)
        ended_reason = f"pipeline-error: {exc}"
    finally:
        transcript = [
            {"role": m.get("role"), "message": m.get("content")}
            for m in context.get_messages()
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        call = store.update(
            "calls",
            call_id,
            {
                "status": "ended",
                "endedAt": now_iso(),
                "endedReason": ended_reason,
                "durationSeconds": round(time.monotonic() - started, 1),
                "messages": transcript,
            },
        )
        if plan.server_url and call:
            await _post_end_of_call_report(plan.server_url, call)


async def _post_end_of_call_report(url: str, call: dict[str, Any]) -> None:
    """Vapi-style 'end-of-call-report' webhook so existing integrations keep working."""
    payload = {
        "message": {
            "type": "end-of-call-report",
            "endedReason": call.get("endedReason"),
            "call": call,
            "artifact": {"messages": call.get("messages", [])},
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("end-of-call-report to {} failed: {}", url, exc)
