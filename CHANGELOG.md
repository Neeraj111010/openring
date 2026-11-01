# Changelog

All notable changes to OpenRing are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Incoming calls through Twilio are no longer beta: confirmed on a real phone call (a caller in Brazil to a US
  number, with Deepgram, Groq and the Kokoro voice), including interruptions and the cost record. Outgoing calls
  through Twilio, and so campaigns, stay beta until a real one is confirmed.

### Fixed

- The local Whisper transcriber didn't start on Apple Silicon Macs (M1 and later) unless MLX Whisper was
  installed: Pipecat requires it just to load its Whisper module there, though OpenRing uses faster-whisper.
- Lists (calls, assistants...) created in the same instant could come back in either order; the newest is
  now always first.

### Security

- An example API key (`change-me` from `.env.example`, which the Docker setup copied) was accepted, so a
  server deployed that way answered anyone who knew the example. Example keys are now refused: `openring`
  replaces one with a generated key, saved so it stays the same across restarts, and the dashboard shows it
  under Settings. `.env.example` no longer carries one.
- A server without an API key answered API requests from anywhere. Its API now answers only the computer
  it runs on (not tunnels or the network); the dashboard keeps working through its sign-in.

## [0.1.3] - 2026-11-01

### Added

- Route presets, OmniRoute's mode packs cut down to what a call needs: **Fastest** (the model with the
  quickest measured answers first), **Cheapest** (the lowest price first, local models before paid ones),
  **Reliable** (your order, longer rest for a failing model) and **Custom** (`order`, `maxLatencyMs` and
  `cooldownMinutes`, all yours). Changing a setting on a preset route makes it Custom.
- Model speed: each call records how long its model took to start answering, and
  `POST /model-route/{id}/measure` (**Measure speed** on the route's page) times one short answer from each
  model. The route editor shows each model's speed and price, and the order the next call will try.
- Cost per call in dollars, at list prices: `cost` and `costBreakdown.llm`, `.stt`, `.tts`, `.total` (Vapi's
  fields), priced per provider when a call switched. Model prices ship in `openring/llm_prices.json` (from
  LiteLLM's price list and OpenRouter's), speech-to-text and voices in `openring/prices.py`;
  `~/.openring/prices.json` overrides them and `GET /prices` lists them. The dashboard shows the cost per
  call, per minute and in total.
- Model routes (beta), like OmniRoute's combos: a saved list of models, in order, that assistants share
  (`/model-route`, the **Model routes** page, and a route picker in the assistant editor). The next model
  takes over when one fails or, with `maxLatencyMs`, when it is slow to start answering: the slow request is
  cancelled and the next model answers the same turn. A model that let the route down rests for
  `cooldownMinutes`, so new calls start on the next one. Each switch shows its reason on the call's page.
- Fixed: after a model switch, the replaced model is stopped and the response it left open is closed, so the
  caller hears one answer and the new model's reply reaches the transcript.
- A 100% local, free stack: the new `whisper` transcriber (faster-whisper) runs on your computer, with
  Ollama for the model and Kokoro for the voice, so nothing is billed per minute and the audio stays on the
  machine. **Use the free local stack** in the assistant editor sets all three; the editor offers the models
  pulled into Ollama, and the Providers page says whether Ollama is running. Whisper's model (`tiny` to
  `large-v3-turbo`, `base` by default) downloads once in the background when an assistant uses it, is shared
  by every call, runs on a GPU when there is one, and gets phone audio at the 16 kHz it expects.
  `npx openring` now installs it.
- Genesys Cloud (beta): an assistant takes calls inside your Architect flows through Genesys's Audio Connector
  (`/ws/genesys`, the AudioHook protocol). Each connection must carry the API key and Genesys's HMAC
  signature made with the client secret, which the Providers page can generate. The flow picks the assistant
  with an `assistantId` input variable (or the called number does); other input variables fill the
  assistant's `{{variables}}`. Output variables hand the call back: `action` is `finished`, or `transfer`
  with `transferTo`.
- Campaigns (beta), with Vapi's `/campaign` API: one assistant calls a list of people from a Twilio number,
  `maxConcurrency` at a time, between `schedulePlan.earliestAt` and `latestAt`, with Vapi's counters and
  `PATCH {"status": "cancelled"}` to stop. A restarted server picks its campaigns back up without calling
  anyone twice. The new **Campaigns** page takes a pasted list or a CSV (its extra columns become variables),
  shows who was called and how each call ended, and asks you to confirm that everyone agreed to be called.
- Variables and `assistantOverrides`, as on Vapi: the first message and instructions can use
  `{{customer.name}}`, `{{customer.number}}` and any key of `variableValues`, on campaigns and `POST /call`.
- Callers in any language: with `"language": "multi"` the model is told to answer in the caller's language and
  to switch when they do. The assistant editor offers it as **Follow the caller**, next to common languages,
  and warns when the chosen voice speaks only one language (Kokoro, Deepgram); ElevenLabs and OpenAI voices
  read any.
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
