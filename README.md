# OpenRing

**Open-source, self-hosted voice AI agents with a Vapi-compatible API. Zero platform fee.**

Hosted voice platforms charge a per-minute platform fee on top of what you already pay for
speech-to-text, the LLM, text-to-speech and telephony. OpenRing runs on your own server, uses your
own provider keys, and keeps the same JSON you already use for Vapi assistants, so moving over is
mostly a base-URL change.

[Português](README.pt-BR.md)

## Status: v0.1 (early)

| Works today | On the roadmap |
|---|---|
| Inbound phone calls via Twilio | Outbound calls (`POST /call`) |
| Vapi-style `/assistant`, `/phone-number`, `/call` APIs | Telnyx, Plivo, Vonage |
| Deepgram STT; any OpenAI-compatible LLM; ElevenLabs or Kokoro (free, local) TTS | Provider fallback and cost routing |
| `firstMessage`, `endCallPhrases`, `end-of-call-report` webhook | Tool calls, dashboard, agency mode (multi-client, white-label) |

## Quick start

One command. It installs everything it needs (including Python), starts the server and opens the dashboard:

```bash
npx github:elifalanrocha/openring
```

You only need [Node.js](https://nodejs.org) 18+. Settings live in `~/.openring/.env`, created on the first run with a generated API key.

### With Docker

```bash
cp .env.example .env          # fill in your keys
docker compose up --build     # API on http://localhost:8000
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

## The free stack

| Layer | Free option |
|---|---|
| Speech-to-text | Deepgram: US$200 credit for new accounts |
| LLM | `"provider": "omniroute"` (free local gateway to 150+ free providers) or `"provider": "groq"` |
| Text-to-speech | `"provider": "kokoro"` runs locally on CPU, no key |
| Telephony | Twilio trial credit |

## Providers

- **LLM:** `openai`, `groq`, `openrouter`, `deepseek`, `omniroute`, or `custom-llm` with `model.url`
  pointing at any OpenAI-compatible endpoint.
- **Transcriber:** `deepgram` (use `"language": "multi"` for multilingual calls).
- **Voice:** `11labs` / `elevenlabs`, `kokoro`.

## Development

```bash
pip install -e ".[dev,kokoro]"
pytest
python -m openring
```

## Security

Set `OPENRING_API_KEY` before exposing the server. With `TWILIO_AUTH_TOKEN` set, every Twilio
webhook is checked against its `X-Twilio-Signature`.

## License

MIT. Not affiliated with Vapi, Retell or Twilio.
