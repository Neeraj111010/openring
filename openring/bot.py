"""Runs one live call, by phone or from the browser: audio in -> STT -> LLM -> TTS -> audio out."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket
from loguru import logger

from . import server_messages
from .config import Settings
from .providers import StackPlan, build_services
from .store import Store, now_iso
from .tools import CallControls, assistant_tools, register_tools

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


def transcript_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """The call's transcript: what each side said, plus the tools the assistant used and their results."""
    transcript: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            transcript.append({"role": role, "message": content})
        if role == "assistant" and m.get("tool_calls"):
            transcript.append({"role": "tool_calls", "toolCalls": m["tool_calls"]})
        elif role == "tool":
            result = _plain(content)
            transcript.append({"role": "tool_call_result", "toolCallId": m.get("tool_call_id"), "result": result})
    return transcript


def _plain(content: Any) -> Any:
    """Pipecat stores a text result JSON-encoded ('"Booked"'); keep the text itself."""
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except ValueError:
            return content
        if isinstance(decoded, str):
            return decoded
    return content


def keep_phone_line_open(transport: Any) -> None:
    """Pipecat's Twilio serializer hangs up when the pipeline stops; a transferred call must stay up."""
    serializer = getattr(getattr(transport, "_params", None), "serializer", None)
    if serializer is not None and hasattr(serializer, "_hangup_attempted"):
        serializer._hangup_attempted = True


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
    transfer: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Run a call until it ends. `transfer(number)` moves a phone call to another line (transferCall tool)."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        EndFrame,
        LLMRunFrame,
        TTSSpeakFrame,
    )
    from pipecat.observers.base_observer import BaseObserver
    from pipecat.observers.user_bot_latency_observer import MeasuredFrom, UserBotLatencyObserver
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )

    started = time.monotonic()  # the caller already marked the call in-progress
    ended_reason = "customer-ended-call"
    context = None
    turns: list[dict[str, int]] = []
    outbox = server_messages.Outbox(plan.assistant, lambda: store.get("calls", call_id))
    # The setup runs inside the try as well, so the call record ends however the call does: a caller
    # who hangs up while the services are still being built, or a provider that fails to start.
    try:
        # Loading a local voice model takes a moment; doing it off the event loop keeps other calls flowing.
        stt, llm, tts = await asyncio.to_thread(build_services, plan)
        speech = {"speaking": False, "last_stop": 0.0}

        async def speak(text: str) -> None:
            await task.queue_frames([TTSSpeakFrame(text, append_to_context=False)])

        async def end_call() -> None:
            nonlocal ended_reason
            ended_reason = "assistant-ended-call"
            await task.queue_frames([EndFrame()])  # after whatever is still being said

        async def wait_until_quiet(expect_speech: bool) -> None:
            """Until the assistant finishes talking, so a transfer never cuts a sentence."""
            asked = time.monotonic()
            while time.monotonic() - asked < 30:
                await asyncio.sleep(0.2)
                if speech["speaking"]:
                    continue
                if expect_speech and speech["last_stop"] < asked and time.monotonic() - asked < 15:
                    continue  # a local voice takes a moment to synthesize before it starts
                if time.monotonic() - max(speech["last_stop"], asked) >= 0.8:
                    break
            await asyncio.sleep(0.3)

        async def transfer_to(number: str, message: str | None) -> None:
            nonlocal ended_reason
            if message:
                await speak(message)
            await wait_until_quiet(expect_speech=bool(message))
            keep_phone_line_open(transport)
            ended_reason = "assistant-forwarded-call"
            logger.info("call {} transferred to {}", call_id, number)
            outbox.send("status-update", status="forwarding", destination={"type": "number", "number": number})
            await transfer(number)


        controls = CallControls(
            speak=speak,
            end_call=end_call,
            transfer=transfer_to if transfer else None,
            current_call=lambda: store.get("calls", call_id),
            assistant=plan.assistant,
            default_url=plan.server_url,
        )
        tools = register_tools(llm, assistant_tools(plan.assistant, store), controls)
        context = LLMContext(messages=list(plan.messages), **({"tools": tools} if tools else {}))
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

        class SpeechObserver(BaseObserver):
            async def on_push_frame(self, data):
                if isinstance(data.frame, BotStartedSpeakingFrame):
                    speech["speaking"] = True
                elif isinstance(data.frame, BotStoppedSpeakingFrame):
                    speech.update(speaking=False, last_stop=time.monotonic())

        @latency.event_handler("on_latency_breakdown")
        async def _on_latency(_observer, breakdown):
            if breakdown.measured_from == MeasuredFrom.USER_SILENCE:  # the greeting is not a turn
                turns.append(turn_latency(breakdown))
                logger.info("call {} turn latency {} ms", call_id, turns[-1]["turnLatency"])

        rates = {"audio_in_sample_rate": sample_rate, "audio_out_sample_rate": sample_rate} if sample_rate else {}
        task = PipelineTask(
            pipeline,
            params=PipelineParams(enable_metrics=True, **rates),
            observers=[latency, SpeechObserver(observe_every_push=False)],
        )

        @transport.event_handler("on_client_connected")
        async def _on_connected(_transport, _client):
            outbox.send("status-update", status="in-progress")
            if plan.first_message:
                # In the context (and transcript) right away, even if the voice fails to say it.
                context.add_message({"role": "assistant", "content": plan.first_message})
                await task.queue_frames([TTSSpeakFrame(plan.first_message, append_to_context=False)])
            else:
                await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def _on_disconnected(_transport, _client):
            await task.cancel()

        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def _conversation_update(_aggregator, _message):
            messages = [m for m in context.get_messages() if isinstance(m, dict)]
            outbox.send("conversation-update", messages=transcript_messages(messages), messagesOpenAIFormatted=messages)

        if plan.end_call_phrases:
            phrases = [p.lower() for p in plan.end_call_phrases]

            @aggregators.assistant().event_handler("on_assistant_turn_stopped")
            async def _maybe_hang_up(_aggregator, message):
                nonlocal ended_reason
                text = str(getattr(message, "content", message) or "").lower()
                if any(p in text for p in phrases):
                    ended_reason = "assistant-said-end-call-phrase"
                    await task.queue_frames([EndFrame()])

        await PipelineRunner(handle_sigint=False).run(task)
    except Exception as exc:  # keep the call record honest
        logger.exception("call {} failed", call_id)
        ended_reason = f"pipeline-error: {exc}"
    finally:
        transcript = transcript_messages(context.get_messages()) if context is not None else []
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
        if call:
            outbox.send("status-update", status="ended", endedReason=ended_reason)

            async def report() -> None:
                await outbox.flush()
                await server_messages.send(
                    plan.assistant, "end-of-call-report", call, endedReason=ended_reason,
                    artifact=call.get("artifact") or {"messages": transcript},
                )

            # When the caller hangs up, the server cancels this call's task; the report still goes out.
            reporting = asyncio.ensure_future(report())
            _reports.add(reporting)
            reporting.add_done_callback(_reports.discard)
            await asyncio.shield(reporting)


# End-of-call reports still on their way, kept so they are not collected before they finish.
_reports: set[asyncio.Future] = set()
