# Roadmap

Where OpenRing is heading. Priorities change with what people need, so open an issue to argue for
something, or pick an item and say you're on it. Shipped work moves to the [changelog](CHANGELOG.md).

## Next (weeks after the first release)

- **Smarter provider fallback.** Fallback on failure is in (beta); next is switching when a provider gets
  slow, and routing by cost.
- **Faster local speech-to-text on a CPU:** streaming Moonshine for the languages it covers. The 100% local
  stack itself (Whisper, Ollama, Kokoro) is in.
- **Cost per call in dollars.** Calls already record their tokens, voice characters and transcribed
  seconds; next is turning them into what each provider charged, and what the same call would have cost
  on a hosted platform.
- **More telephony:** outbound calls through Telnyx and Genesys Cloud (incoming calls through both are in
  beta), and Plivo.
- **Campaigns, next steps:** retrying busy and unanswered numbers, calling hours per time zone, and Vapi's
  campaign webhooks. Campaigns themselves are in (beta).
- **Calls over WhatsApp.**
- **Deploy in one click:** a published Docker image and templates for Railway and Render.

## Later

- **Agencies:** several clients on one server, each with its own assistants, numbers, limits and
  branding.
- **Every Vapi webhook:** live transcripts and the remaining server messages, so existing integrations
  work unchanged.
- **Scale:** Postgres and load tests with many simultaneous calls.
- **Dashboard in Portuguese and Spanish.**
- **Desktop installers** for Windows and macOS, so nobody needs a terminal.
