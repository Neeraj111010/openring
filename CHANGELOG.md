# Changelog

All notable changes to OpenRing are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Provider fallback (beta), in Vapi's fields: `transcriber.fallbackPlan.transcribers`, `model.fallbackModels`
  and `voice.fallbackPlan.voices`. When a provider fails in a way a retry won't fix (a rejected key, no
  credit, an outage, a rate limit), the next one takes over for the rest of the call. A fallback model can be
  on another provider (`{"provider": "openai", "model": "gpt-4.1-mini"}`), and when the model switches the new
  one answers the caller's last message. Each switch is recorded in the call's `providerFallbacks` and shown
  on its page.

## [0.1.2] - 2026-10-30

### Added

- The Providers page shows what's left on each connected account that reports it: dollars on OpenRouter,
  Deepgram, Twilio and Telnyx, characters on ElevenLabs, with a warning when it runs low.
- What each call used, in Vapi's `costBreakdown` fields: `llmPromptTokens`, `llmCompletionTokens` and
  `ttsCharacters`, plus `transcriberAudioSeconds`. `performanceMetrics.firstWordLatency` is how long the
  caller waited for the greeting.
- The overview shows the wait for the first word, tokens per call, the 95th-percentile response time and
  how many calls were transferred. Success rate, response time and first word get a traffic light (good,
  fair, poor) based on what callers expect, for example 90% or more of calls ending normally and replies
  within 1.2 s.
- Requests to OpenRouter carry OpenRing's name (`HTTP-Referer` and `X-Title`), so OpenRouter can list
  OpenRing among the apps that use it. Nothing else is sent; `OPENRING_ATTRIBUTION=false` turns it off.
- Incoming calls through Telnyx (beta): point a TeXML Application's voice URL at `/telnyx/voice`. Webhooks are
  checked against the account's Ed25519 public key (`TELNYX_PUBLIC_KEY`); `TELNYX_API_KEY` lets OpenRing
  hang up. Telnyx can be connected from the Providers page. Outbound calls and transfers still go through Twilio.

### Changed

- New look: a ring-and-wave logo, an orange palette in light and dark themes, and a quieter dashboard.
  Numbers are colored only when they need attention, labels are no longer in spaced capitals, and the
  fee you didn't pay leads the overview.
- Recent calls on the overview are links, so they work from the keyboard.
- A rewritten README, and [PRINCIPLES.md](PRINCIPLES.md): what OpenRing promises and how it decides what to build.

## [0.1.1] - 2026-10-30

### Changed

- Releases on npm are staged by CI and approved by a maintainer with two-factor authentication.

### Fixed

- A call whose caller hung up in its first seconds, while its speech, model and voice services were still
  starting, stayed "in-progress" for good; so did a call whose provider failed to start. Both now end, with
  `customer-ended-call` or `pipeline-error: ...`.

## [0.1.0] - 2026-10-30

The first public release. Everything below is new.

### Voice agents

- Vapi-compatible REST API for assistants (`/assistant`), phone numbers (`/phone-number`), calls
  (`/call`) and tools (`/tool`): existing Vapi clients work by changing the base URL.
- Inbound phone calls through Twilio, and outbound calls with `POST /call`.
- Talk to an assistant from the browser, with no phone number needed.
- Assistant tools in Vapi's format: `function`, `endCall` and `transferCall`.
- Import assistants, tools and phone numbers from a Vapi-compatible API.
- Per-turn latency on every call, in Vapi's `artifact.performanceMetrics` shape, and Vapi-style
  `end-of-call-report` webhooks.

### Providers

- Models from any OpenAI-compatible provider: OpenAI, Groq, OpenRouter, DeepSeek, OmniRoute or a
  custom URL.
- Speech to text with Deepgram; voices from ElevenLabs or Kokoro, a free voice that runs on your
  computer.
- Connect providers from the dashboard: each key is checked with the provider before it's saved, and
  OpenRouter offers one-click sign-in.

### Dashboard

- Overview with calls, minutes, success rate, response time and the platform fee you didn't pay.
- Assistants, phone numbers, calls with transcripts and latency, providers and settings, in light and
  dark themes.
- Sign-in with a password; the API keeps its Bearer key.

### Running it

- One command: `npx openring` installs everything, including Python, and opens the dashboard.
- Docker image and `docker-compose.yml`.
- Twilio webhooks are rejected unless their signature checks out.
