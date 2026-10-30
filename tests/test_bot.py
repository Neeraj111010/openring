from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    LatencyContribution,
    MeasuredFrom,
)

from openring.bot import Usage, performance_metrics, turn_latency


def _breakdown(total, **parts):
    return LatencyBreakdown(
        measured_from=MeasuredFrom.USER_SILENCE,
        total_secs=total,
        contributions=[
            LatencyContribution(
                key=key, label=key, owner="test", owner_kind="service", start_time=0, duration_secs=secs
            )
            for key, secs in parts.items()
        ],
    )


def test_turn_latency_uses_vapi_fields_in_ms():
    turn = turn_latency(_breakdown(
        1.05, endpointing_wait=0.2, turn_detection=0.05, transcription=0.12,
        llm_inference=0.34, speech_synthesis=0.3, output_transport=0.04,
    ))
    assert turn == {
        "endpointingLatency": 250, "transcriberLatency": 120, "modelLatency": 340,
        "voiceLatency": 300, "turnLatency": 1050,
    }


def test_performance_metrics_averages_each_field():
    turns = [turn_latency(_breakdown(0.9, llm_inference=0.3)), turn_latency(_breakdown(1.1, llm_inference=0.5))]
    metrics = performance_metrics(turns)
    assert metrics["turnLatencies"] == turns
    assert metrics["turnLatencyAverage"] == 1000 and metrics["modelLatencyAverage"] == 400
    assert performance_metrics([]) == {"turnLatencies": []}


def test_performance_metrics_keep_the_wait_for_the_first_word():
    assert performance_metrics([], first_word_ms=850) == {"turnLatencies": [], "firstWordLatency": 850}


def test_usage_adds_up_what_providers_bill_by():
    from pipecat.metrics.metrics import (
        LLMTokenUsage,
        LLMUsageMetricsData,
        STTUsage,
        STTUsageMetricsData,
        TTFBMetricsData,
        TTSUsageMetricsData,
    )

    usage = Usage()
    tokens = LLMTokenUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    usage.add([LLMUsageMetricsData(processor="llm", value=tokens), TTSUsageMetricsData(processor="tts", value=42)])
    usage.add([LLMUsageMetricsData(processor="llm", value=tokens), TTFBMetricsData(processor="llm", value=0.2),
               STTUsageMetricsData(processor="stt", value=STTUsage(audio_seconds=12.34))])
    assert usage.cost_breakdown() == {"llmPromptTokens": 240, "llmCompletionTokens": 60, "ttsCharacters": 42,
                                      "transcriberAudioSeconds": 12.3}
