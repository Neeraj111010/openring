"""Runs one live call, by phone or from the browser: audio in -> STT -> LLM -> TTS -> audio out."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import WebSocket
from loguru import logger

from .config import Settings
from .providers import StackPlan, build_services
from .store import Store, now_iso

# Pipecat's named parts of a user-to-bot turn, mapped to Vapi's performanceMetrics fields.
LATENCY_PARTS = {
    "endpointing_wait": "endpointingLatency",
    "turn_detection": "endpointingLatency",
    "transcription": "transcriberLatency",
    "llm_inference": "modelLatency",
    "speech_synthesis": "voiceLatency",
}
LATENCY_FIELDS = (*dict.fromkeys(LATENCY_PARTS.values()), "turnLatency")
# Phone audio is 8 kHz; browser calls keep Pipecat's defaults (16 kHz in, 24 kHz out).
PHONE_SAMPLE_RATE = 8000


def turn_latency(breakdown: Any) -> dict[str, int]:
    """One turn the way Vapi reports it: milliseconds per stage, plus the whole turn."""
    turn = dict.fromkeys(LATENCY_FIELDS, 0)
    for part in breakdown.contributions:
        if part.key in LATENCY_PARTS:
            turn[LATENCY_PARTS[part.key]] += round(part.duration_secs * 1000)
    turn["turnLatency"] = round(breakdown.total_secs * 1000)
    return turn


def performance_metrics(turns: list[dict[str, int]]) -> dict[str, Any]:
    """Vapi-style `artifact.performanceMetrics`: every turn and the average of each field."""
    metrics: dict[str, Any] = {"turnLatencies": turns}
    if turns:
        for name in LATENCY_FIELDS:
            metrics[name + "Average"] = round(sum(t[name] for t in turns) / len(turns))
    return metrics


def twilio_transport(websocket: WebSocket, call_data: dict[str, Any], settings: Settings) -> Any:
    """Audio of a phone call, carried by a Twilio media stream."""
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
    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )


def web_transport(connection: Any) -> Any:
    """Audio of a browser test call, over the WebRTC connection the dashboard opened."""
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )


async def run_call(
    transport: Any,
    *,
    plan: StackPlan,
    call_id: str,
    store: Store,
    sample_rate: int | None = None,
) -> None:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import EndFrame, LLMRunFrame, TTSSpeakFrame
    from pipecat.observers.user_bot_latency_observer import MeasuredFrom, UserBotLatencyObserver
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )

    # Loading a local voice model takes a moment; doing it off the event loop keeps other calls flowing.
    stt, llm, tts = await asyncio.to_thread(build_services, plan)
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
    latency = UserBotLatencyObserver()
    turns: list[dict[str, int]] = []

    @latency.event_handler("on_latency_breakdown")
    async def _on_latency(_observer, breakdown):
        if breakdown.measured_from == MeasuredFrom.USER_SILENCE:  # the greeting is not a turn
            turns.append(turn_latency(breakdown))
            logger.info("call {} turn latency {} ms", call_id, turns[-1]["turnLatency"])

    rates = {"audio_in_sample_rate": sample_rate, "audio_out_sample_rate": sample_rate} if sample_rate else {}
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, **rates),
        observers=[latency],
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        if plan.first_message:
            # In the context (and transcript) right away, even if the voice fails to say it.
            context.add_message({"role": "assistant", "content": plan.first_message})
            await task.queue_frames([TTSSpeakFrame(plan.first_message, append_to_context=False)])
        else:
            await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        await task.cancel()

    if plan.end_call_phrases:
        phrases = [p.lower() for p in plan.end_call_phrases]

        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def _maybe_hang_up(_aggregator, message):
            nonlocal ended_reason
            text = str(getattr(message, "content", message) or "").lower()
            if any(p in text for p in phrases):
                ended_reason = "assistant-said-end-call-phrase"
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
                "artifact": {"messages": transcript, "performanceMetrics": performance_metrics(turns)},
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
            "artifact": call.get("artifact") or {"messages": call.get("messages", [])},
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("end-of-call-report to {} failed: {}", url, exc)
