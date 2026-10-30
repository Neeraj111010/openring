# OpenRing: project context for Claude

OpenRing is an open-source, self-hosted alternative to paid voice AI agent platforms. People run it on
their own machine or server with their own provider keys, so there is **no per-minute platform fee**.
Its API is **Vapi-compatible**: existing clients migrate by changing the base URL. Code, commits and
docs are in English (README.pt-BR.md is the exception). See CONTRIBUTING.md for setup and rules.

## Architecture

- `openring/api.py` FastAPI app: Vapi-style REST (`/assistant`, `/phone-number`, `/call`), Twilio
  voice webhook (`POST /twilio/voice`, validates `X-Twilio-Signature`, refuses calls while
  `TWILIO_AUTH_TOKEN` is unset unless `VALIDATE_TWILIO_SIGNATURE=false`), media websocket
  (`/ws/twilio`: one connection per call, it atomically claims the queued call via `Store.claim`;
  the assistant comes from the call record, never from stream parameters), `POST /call/web`
  (browser test call: answers the dashboard's WebRTC offer with Pipecat's `SmallWebRTCConnection`
  and runs the call as a background task; non-trickle ICE), `POST /call` (outbound, Vapi body:
  creates a queued `outboundPhoneCall`, Twilio REST places it with
  `Url=$PUBLIC_URL/twilio/voice?callId=...`; signatures cover the query string; `POST /twilio/status`
  ends calls that never connected with Vapi's endedReason), dashboard at `/`, `/health`,
  `GET /stats` (overview numbers, `openring/stats.py`).
- Dashboard login (`openring/auth.py`, like OmniRoute): one password per server, scrypt hash in the DB
  `settings` table; sessions in the `sessions` table as `HttpOnly`/`SameSite=Strict` cookies. Requests
  authorize with the Bearer API key OR session cookie + `X-OpenRing: dashboard` header (CSRF guard).
  `/auth/state|setup|login|logout|password`; the first password is chosen only from direct localhost
  or set by `OPENRING_PASSWORD`; `openring --reset-password` clears it. `/dashboard/info` (session only)
  gives the Settings/Providers pages the API key, URLs and which provider keys are set (never the keys).
  `GET /providers/{id}/balance` (session) reads what's left on accounts that report it (`keys.BALANCES`).
- `openring/bot.py` one live call: `run_call(transport, sample_rate=...)` runs Pipecat's pipeline
  transport -> STT -> LLM -> TTS; `twilio_transport` (8 kHz) and `web_transport` (WebRTC) build the
  transport. First message, end-call phrases, saves transcript and per-turn latency
  (`artifact.performanceMetrics`, Vapi's shape, in ms, from Pipecat's `UserBotLatencyObserver`),
  records usage in Vapi's `costBreakdown` fields (`llmPromptTokens`, `llmCompletionTokens`, `ttsCharacters`,
  plus `transcriberAudioSeconds`) and `performanceMetrics.firstWordLatency`, sends Vapi-style
  `end-of-call-report` to `server.url`. Services are built in a worker thread.
- `openring/providers.py` maps the Vapi assistant JSON to a stack using Vapi's provider names (`ALIASES`
  for other spellings). LLM: OpenAI-compatible APIs (`LLM_PROVIDERS`: openai, anthropic, google, groq,
  openrouter, deepseek, xai, cerebras, together-ai, deepinfra, perplexity-ai, mistral, fireworks, ollama,
  omniroute) + `custom-llm` with `model.url`. STT (`STT_SERVICES`): deepgram, assembly-ai, gladia, soniox,
  cartesia, 11labs. TTS (`TTS_SERVICES`): 11labs, kokoro (free, local, default), cartesia, deepgram (Vapi
  short voice names -> `aura-2-<name>-en`), openai, rime-ai, neuphonic, smallest-ai, inworld. Defaults
  (nova-3, af_heart) apply only to their own provider. Kokoro uses the
  int8 model (~120 MB with voices) in `~/.cache/pipecat/kokoro-onnx`: `warm_up()` at startup
  downloads it if missing (atomic `.part` rename) and loads it once; Pipecat's per-service model
  load is patched to share that instance. Calls refuse Kokoro with a progress message until ready.
- Groq's catalog changed in 2026: Llama 3.x is gone; use `openai/gpt-oss-20b` (fast) or
  `openai/gpt-oss-120b`. Groq's Cloudflare returns 403 to Python's default urllib User-Agent.
- `openring/tools.py` Vapi tools: `function` (POSTs Vapi's `tool-calls` message to `tool.server.url` or the
  assistant's `server.url`, reads `{"results": [{"toolCallId", "result"|"error"}]}`), `endCall`
  (endedReason `assistant-ended-call`), `transferCall` (waits until the bot stops speaking, keeps Pipecat's
  Twilio serializer from hanging up, then redirects the call with `<Dial>` TwiML; endedReason
  `assistant-forwarded-call`; browser calls can't transfer). `model.tools` + saved `/tool` docs via
  `model.toolIds`; unsupported types are skipped with a warning. Registered with `llm.register_function`
  and `ToolsSchema` on the LLMContext; the transcript keeps `tool_calls` / `tool_call_result` rows.
- `openring/importer.py` + `POST /import {baseUrl, apiKey}`: copies tools, assistants and Twilio numbers from
  any Vapi-compatible API (pages back with `createdAtLt`), drops source-owned fields and secrets, remaps
  `model.toolIds`, marks docs with `importedFrom` so a re-import updates them; reports per-assistant problems
  (unsupported providers/tools) without refusing the import. Dashboard: **Import** on the Assistants page.
- `openring/store.py` SQLite document store (JSON docs) for assistants, phone_numbers, calls, tools.
- `openring/static/index.html` single-file dashboard (vanilla JS, no build step), styled after OmniRoute:
  sidebar with sections, dark/light theme, big-number cards, tabs. Pages: Home (overview + get started),
  Assistants (tiles; editor with Setup/JSON tabs), Phone numbers, Calls (filters; detail with transcript,
  per-turn latency, JSON), Providers, Settings (API access, password).
- `openring/__main__.py` `openring` command: first run creates `~/.openring/.env` with a generated
  `OPENRING_API_KEY`, DB at `~/.openring/openring.db`, binds 127.0.0.1:8000.
- `bin/openring.js` + `package.json` npm launcher: installs uv if missing, runs
  `uv tool run --python 3.12 --from "openring[kokoro]==<package.json version>" openring` (the PyPI release
  matching the launcher), opens the browser. `OPENRING_SOURCE` (e.g. `file:///C:/openring` or a git URL)
  runs that source instead. Until the first PyPI release, `npx` needs `OPENRING_SOURCE`.
- Releases (`docs/releasing.md`): tag `vX.Y.Z` -> `.github/workflows/release.yml` checks the tag matches
  `pyproject.toml`, `openring/__init__.py` and `package.json`, tests, publishes to PyPI, then npm. Trusted
  publishing (OIDC), no stored tokens; npm's first version is published by hand (npm needs the package to
  exist before trusting a workflow).
- Built on Pipecat 1.15 (services use `Settings(...)` objects; VAD via
  `LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())`).

## Commands

```bash
pip install -e ".[dev,kokoro]"   # dev install
pytest                           # incl. a simulated Twilio call and a real local WebRTC call
python -m openring               # run server (reads ./.env and ~/.openring/.env)
npx openring                     # one-command run, once released on npm and PyPI
```

Only one server can use port 8000; stop the old one first, or pass `--port`.

## Phone testing notes

- A Twilio trial account calls only verified numbers, through Twilio's shared "Sandbox Number". That
  sandbox's proxy re-signs webhooks (they fail validation with the account token) and the media stream
  never opens, so callers hear silence. Test with an inbound call to a number you bought, or upgrade.
- The Calls API on trial rejects the `Method`/`StatusCallbackMethod` parameters.
- To reach a local server, use a tunnel (`cloudflared tunnel --url http://localhost:8000`). Quick-tunnel
  URLs change on every start: update `PUBLIC_URL` and the number's voice webhook.

## Conventions

- `PRINCIPLES.md` holds the project's promises (no platform fee, your keys, Vapi-compatible, one command,
  honest status) and its feature test; check new work against it.
- Keep the Vapi JSON shape for any new field; prefer adding over renaming.
- Every feature gets a test; `pytest` and `ruff check .` must pass before pushing.
- Never commit `.env` or keys. Don't use Vapi's name or logo except to say "Vapi-compatible".
- Third-party licenses are listed in THIRD_PARTY.md; update it when dependencies change.

Maintainers keep private notes (plans, status, working rules) outside git; Claude loads them when present:
@private/CLAUDE.md
