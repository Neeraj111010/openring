# Roadmap

Where OpenRing is heading. Priorities change with what people need, so open an issue to argue for
something, or pick an item and say you're on it. Shipped work moves to the [changelog](CHANGELOG.md).

## Next (weeks after the first release)

- **Automatic provider fallback.** When a speech, model or voice provider fails or slows down, switch
  to the next one mid-call, and route by cost.
- **100% local, free stack.** Local speech-to-text (Whisper or Moonshine), models through Ollama and the
  Kokoro voice: no per-minute cost at all apart from telephony.
- **Cost per call in dollars.** Calls already record their tokens, voice characters and transcribed
  seconds; next is turning them into what each provider charged, and what the same call would have cost
  on a hosted platform.
- **More telephony:** outbound calls through Telnyx (incoming Telnyx calls are in beta), and Plivo; both
  usually cheaper than Twilio.
- **Outbound campaigns:** call a list of numbers, in Vapi's format.
- **Calls over WhatsApp.**
- **Multilingual preset:** speech recognition and a voice that follow the caller's language.
- **Deploy in one click:** a published Docker image and templates for Railway and Render.

## Later

- **Agencies:** several clients on one server, each with its own assistants, numbers, limits and
  branding.
- **Every Vapi webhook:** live transcripts and the remaining server messages, so existing integrations
  work unchanged.
- **Scale:** Postgres and load tests with many simultaneous calls.
- **Dashboard in Portuguese and Spanish.**
- **Desktop installers** for Windows and macOS, so nobody needs a terminal.
