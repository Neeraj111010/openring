# OpenRing

[![CI](https://github.com/elifalanrocha/openring/actions/workflows/ci.yml/badge.svg)](https://github.com/elifalanrocha/openring/actions/workflows/ci.yml)

**Open-source, self-hosted voice AI agents with a Vapi-compatible API. Zero platform fee.**

Hosted voice platforms charge a per-minute platform fee on top of what you already pay for
speech-to-text, the LLM, text-to-speech and telephony. OpenRing runs on your own server, uses your
own provider keys, and keeps the same JSON you already use for Vapi assistants, so moving over is
mostly a base-URL change.

[Português](README.pt-BR.md)

## Status: v0.1 (early)

| Works today | On the roadmap |
|---|---|
| Inbound and outbound phone calls via Twilio | Telnyx, Plivo, Vonage |
| Browser test calls from the dashboard (WebRTC, no phone number needed) | Provider fallback and cost routing |
| Vapi-style `/assistant`, `/phone-number`, `/call`, `/tool` APIs | More tool types (DTMF, knowledge base) |
| Vapi tools: `function` (your server gets `tool-calls`), `endCall`, `transferCall` | |
| Vapi's provider names: 16 LLMs, 6 transcribers, 9 voices (incl. free local Kokoro, Ollama) | Agency mode (multi-client, white-label) |
| `firstMessage`, `endCallPhrases`, `end-of-call-report` webhook, per-turn latency | |

## Quick start

One command. It installs everything it needs (including Python), starts the server and opens the dashboard:

```bash
npx openring
```

You only need [Node.js](https://nodejs.org) 18+. Settings live in `~/.openring/.env`, created on the first run with a generated API key.
On the first visit, the dashboard asks you to choose its password. The API key stays for apps that call the API
(find it under **Settings**).

Talk to an assistant before buying a phone number: press **Test** next to it in the dashboard and
speak into your microphone. First connect a Deepgram key and a model key on the **Providers** page
(Groq's free tier works, for example with `openai/gpt-oss-20b`); each key is checked with the provider
before it's saved. The free Kokoro voice downloads itself on the first start (about 120 MB).

### With Docker

```bash
cp .env.example .env          # fill in your keys
docker compose up --build     # API on http://localhost:8000; set OPENRING_PASSWORD in .env for the dashboard
ngrok http 8000               # put the https URL in PUBLIC_URL and restart
```

Create an assistant (same shape as Vapi):

```bash
curl -X POST http://localhost:8000/assistant \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Front desk",
    "firstMessage": "Hi! How can I help you today?",
    "model": {"provider": "openai", "model": "gpt-4.1-mini",
              "messages": [{"role": "system", "content": "You are a friendly receptionist."}]},
    "voice": {"provider": "11labs", "voiceId": "YOUR_VOICE_ID"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"}
  }'
```

Attach your Twilio number, then point the number's **Voice webhook** (HTTP POST) to
`$PUBLIC_URL/twilio/voice`:

```bash
curl -X POST http://localhost:8000/phone-number \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"number": "+15551234567", "assistantId": "ASSISTANT_ID"}'
```

Call the number. Transcripts are at `GET /call`.

To have the assistant call someone, send the same body as Vapi's `POST /call` (or use **Call a phone**
in the dashboard). Only call people who expect it: automated calls are regulated (the TCPA in the US,
Anatel's rules in Brazil). A Twilio trial account can only call numbers verified in its console.

```bash
curl -X POST http://localhost:8000/call \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"phoneNumberId": "PHONE_NUMBER_ID", "customer": {"number": "+5511912345678"}}'
```

### Moving from Vapi

Copy every assistant, tool and Twilio number of an account in one request (or press **Import** on the
Assistants page). The key is used once and never stored; importing again updates what came in before.

```bash
curl -X POST http://localhost:8000/import \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"baseUrl": "https://api.vapi.ai", "apiKey": "YOUR_VAPI_PRIVATE_KEY"}'
```

The answer lists each assistant with anything that keeps it from taking calls here yet (for example a
provider OpenRing doesn't support yet). Documents keep their full JSON, so nothing is lost. Point each
imported Twilio number's voice webhook at `$PUBLIC_URL/twilio/voice`.

### Tools

Tools use Vapi's format, so a server written for Vapi works unchanged. Put them in `model.tools`, or
save them once with `POST /tool` and list their ids in `model.toolIds`:

```json
"model": {
  "provider": "groq", "model": "openai/gpt-oss-20b",
  "tools": [
    {"type": "function",
     "function": {"name": "book_appointment", "description": "Book a cleaning",
                  "parameters": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}},
     "server": {"url": "https://your-app.com/tools"},
     "messages": [{"type": "request-start", "content": "One moment, let me check."}]},
    {"type": "endCall"},
    {"type": "transferCall", "destinations": [{"type": "number", "number": "+15551230000",
      "description": "front desk", "message": "Transferring you now."}]}
  ]
}
```

When the model calls `book_appointment`, OpenRing POSTs `{"message": {"type": "tool-calls", "toolCallList":
[...], "call": {...}}}` to the tool's `server.url` (or the assistant's) and reads back
`{"results": [{"toolCallId": "...", "result": "..."}]}`. `server.headers`, `server.secret`
(sent as `X-Vapi-Secret`), `server.timeoutSeconds`, `async` and the `request-start` / `request-failed`
messages work as on Vapi. `endCall` hangs up after the goodbye; `transferCall` moves a phone call to the
chosen number (Twilio) once the assistant stops talking. Tool calls and results show up in the call's
transcript.

### Server messages

Your assistant's `server.url` gets Vapi's messages in Vapi's shape (`{"message": {"type": ..., "call": {...}}}`),
with `server.headers` and `server.secret` (as `X-Vapi-Secret`):

- `status-update` when the call starts (`in-progress`), is transferred (`forwarding`) and ends (`ended`, with
  `endedReason`);
- `conversation-update` after each answer, with the transcript so far;
- `end-of-call-report` with the transcript and per-turn latency.

Pick which ones with `serverMessages`, as on Vapi. A phone number without an assistant but with its own
`server.url` asks it on each incoming call (`assistant-request`) and uses the `assistantId` or the transient
`assistant` it answers. `POST /call` also takes a transient `assistant` instead of an `assistantId`.

## The free stack

| Layer | Free option |
|---|---|
| Speech-to-text | Deepgram: US$200 credit for new accounts |
| LLM | `"provider": "omniroute"` (free local gateway to 150+ free providers) or `"provider": "groq"` |
| Text-to-speech | `"provider": "kokoro"` runs locally on CPU, no key |
| Telephony | Twilio trial account, for testing |

## Providers

Provider names are Vapi's, so an assistant JSON moves between the two unchanged. Each needs its key in
`~/.openring/.env` (or connect it from the dashboard's Providers page).

| Layer | `provider` values | Key |
|---|---|---|
| LLM | `openai`, `anthropic`, `google` (Gemini), `groq`, `openrouter`, `deepseek` / `deep-seek`, `xai`, `cerebras`, `together-ai`, `deepinfra`, `perplexity-ai`, `mistral`, `fireworks` | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, ... (the provider's name) |
| LLM, local | `ollama`, `omniroute`, or `custom-llm` with `model.url` (any OpenAI-compatible endpoint) | none |
| Transcriber | `deepgram`, `assembly-ai`, `gladia`, `soniox`, `cartesia`, `11labs` | `DEEPGRAM_API_KEY`, `ASSEMBLYAI_API_KEY`, `GLADIA_API_KEY`, `SONIOX_API_KEY`, `CARTESIA_API_KEY`, `ELEVENLABS_API_KEY` |
| Voice | `11labs` / `elevenlabs`, `cartesia`, `deepgram` (Aura), `openai`, `rime-ai`, `neuphonic`, `smallest-ai`, `inworld` | the provider's key (`RIME_API_KEY`, `NEUPHONIC_API_KEY`, `SMALLEST_API_KEY`, `INWORLD_API_KEY`, ...) |
| Voice, local | `kokoro` (free, runs on CPU) | none |

Use `"language": "multi"` on Deepgram for multilingual calls. With a Deepgram key you already have a fast
cloud voice: `{"provider": "deepgram", "voiceId": "thalia"}`.

## Development

```bash
pip install -e ".[dev,kokoro]"
pytest
python -m openring
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to send a change, [ROADMAP.md](ROADMAP.md) for what's next and
[CHANGELOG.md](CHANGELOG.md) for what shipped.

## Security

Set `OPENRING_API_KEY` before exposing the server. The dashboard has its own password: the first one can
only be chosen from the server's own computer (`http://localhost`) or with `OPENRING_PASSWORD`, so a stranger
who finds your tunnel URL cannot claim it first. Sessions are `HttpOnly`, `SameSite=Strict` cookies, and
repeated wrong passwords are slowed down. Forgot it? `openring --reset-password`. Incoming calls need `TWILIO_AUTH_TOKEN`: every
Twilio webhook is checked against its `X-Twilio-Signature`, and calls are refused while the token is
missing (`VALIDATE_TWILIO_SIGNATURE=false` turns the check off, for local tests only). Each call
accepts a single media stream, so a replayed or second connection is dropped. Found a vulnerability?
Please report it privately: see [SECURITY.md](SECURITY.md).

## License

MIT. Not affiliated with Vapi, Retell or Twilio. OpenRing builds on [Pipecat](https://github.com/pipecat-ai/pipecat)
and other open-source projects, credited in [THIRD_PARTY.md](THIRD_PARTY.md).
