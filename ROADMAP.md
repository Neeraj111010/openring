# Roadmap

Where OpenRing is heading. Priorities change with what people need, so open an issue to argue for
something, or pick an item and say you're on it. Shipped work moves to the [changelog](CHANGELOG.md).

## Next (weeks after the first release)

- **Automatic provider fallback.** When a speech, model or voice provider fails or slows down, switch
  to the next one mid-call, and route by cost.
- **100% local, free stack.** Local speech-to-text (Whisper or Moonshine), models through Ollama and the
  Kokoro voice: no per-minute cost at all apart from telephony.
- **Cost per call.** What each provider charged for a call, and what the same call would have cost on a
  hosted platform.
- **More telephony:** Telnyx and Plivo, usually cheaper than Twilio.
- **More providers:** Gemini, Cartesia, Deepgram Aura voices and AssemblyAI.
- **Deploy in one click:** a published Docker image and templates for Railway and Render.

## Later

- **Agencies:** several clients on one server, each with its own assistants, numbers, limits and
  branding.
- **Every Vapi webhook:** `assistant-request`, `status-update` and live transcripts, so existing
  integrations work unchanged.
- **Scale:** Postgres and load tests with many simultaneous calls.
- **Dashboard in Portuguese and Spanish.**
- **Desktop installers** for Windows and macOS, so nobody needs a terminal.
