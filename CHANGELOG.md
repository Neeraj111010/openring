# Changelog

All notable changes to OpenRing are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

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
