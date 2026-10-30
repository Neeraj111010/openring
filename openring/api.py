"""FastAPI app: Vapi-compatible REST API + Twilio voice webhook + media websocket."""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from loguru import logger

from . import __version__, twilio
from .config import Settings, get_settings
from .providers import ProviderError, resolve_plan
from .store import Store, now_iso
from .twilio import TwilioError, is_valid_signature, say_twiml, stream_twiml

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# How an outbound call that never reached the assistant ended, from Twilio's status to Vapi's endedReason.
UNREACHED = {
    "busy": "customer-busy",
    "no-answer": "customer-did-not-answer",
    "canceled": "customer-did-not-answer",
    "failed": "twilio-failed-to-connect-call",
    "completed": "twilio-failed-to-connect-call",  # answered, but the media stream never started
}
# Tunnels (ngrok, cloudflared) and reverse proxies connect from 127.0.0.1 too,
# so the peer address alone does not prove the request came from this computer.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip")
# Twilio sends its "connected" and "start" messages right after the media websocket opens.
HANDSHAKE_TIMEOUT = 10


def _is_direct_local_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    if peer not in LOOPBACK:
        return False
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    hostname = urlsplit("//" + request.headers.get("host", "")).hostname or ""
    return hostname in LOOPBACK


def _e164(number: str) -> str:
    return "+" + "".join(ch for ch in number if ch.isdigit())


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = store or Store(settings.db_path)
    app = FastAPI(title="OpenRing", version=__version__)
    app.state.settings = settings
    app.state.store = store

    if not settings.api_key:
        logger.warning("OPENRING_API_KEY is empty: the REST API is open. Set it before exposing this server.")
    if not settings.validate_twilio_signature:
        logger.warning("VALIDATE_TWILIO_SIGNATURE=false: /twilio/voice accepts requests from anyone. Local tests only.")

    def require_key(authorization: str = Header(default="")) -> None:
        if not settings.api_key:
            return
        token = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), settings.api_key.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    auth = [Depends(require_key)]

    def _get_or_404(collection: str, item_id: str) -> dict[str, Any]:
        doc = store.get(collection, item_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"{collection[:-1].replace('_', ' ')} not found")
        return doc

    def _validate_assistant(doc: dict[str, Any]) -> None:
        try:
            resolve_plan(doc, check_keys=False)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/local-session", include_in_schema=False)
    def local_session(request: Request) -> dict[str, str]:
        """Lets the dashboard connect automatically when opened on the same machine."""
        if not _is_direct_local_request(request):
            raise HTTPException(status_code=403, detail="only available from this computer")
        return {"apiKey": settings.api_key}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ---- Assistants (same JSON shape as Vapi's /assistant) -------------------
    @app.post("/assistant", status_code=201, dependencies=auth)
    def create_assistant(body: dict[str, Any]) -> dict[str, Any]:
        _validate_assistant(body)
        return store.create("assistants", body)

    @app.get("/assistant", dependencies=auth)
    def list_assistants(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("assistants", limit)

    @app.get("/assistant/{assistant_id}", dependencies=auth)
    def get_assistant(assistant_id: str) -> dict[str, Any]:
        return _get_or_404("assistants", assistant_id)

    @app.patch("/assistant/{assistant_id}", dependencies=auth)
    def update_assistant(assistant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("assistants", assistant_id)
        _validate_assistant({**current, **body})
        return store.update("assistants", assistant_id, body)

    @app.delete("/assistant/{assistant_id}", dependencies=auth)
    def delete_assistant(assistant_id: str) -> dict[str, Any]:
        _get_or_404("assistants", assistant_id)
        return store.delete("assistants", assistant_id)

    # ---- Phone numbers --------------------------------------------------------
    @app.post("/phone-number", status_code=201, dependencies=auth)
    def create_phone_number(body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("number"):
            raise HTTPException(status_code=400, detail="number is required (E.164, e.g. +15551234567)")
        if body.get("assistantId"):
            _get_or_404("assistants", body["assistantId"])
        body.setdefault("provider", "twilio")
        return store.create("phone_numbers", body)

    @app.get("/phone-number", dependencies=auth)
    def list_phone_numbers(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("phone_numbers", limit)

    @app.get("/phone-number/{number_id}", dependencies=auth)
    def get_phone_number(number_id: str) -> dict[str, Any]:
        return _get_or_404("phone_numbers", number_id)

    @app.patch("/phone-number/{number_id}", dependencies=auth)
    def update_phone_number(number_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _get_or_404("phone_numbers", number_id)
        if body.get("assistantId"):
            _get_or_404("assistants", body["assistantId"])
        return store.update("phone_numbers", number_id, body)

    @app.delete("/phone-number/{number_id}", dependencies=auth)
    def delete_phone_number(number_id: str) -> dict[str, Any]:
        _get_or_404("phone_numbers", number_id)
        return store.delete("phone_numbers", number_id)

    # ---- Calls ------------------------------------------------------------------
    @app.get("/call", dependencies=auth)
    def list_calls(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("calls", limit)

    @app.get("/call/{call_id}", dependencies=auth)
    def get_call(call_id: str) -> dict[str, Any]:
        return _get_or_404("calls", call_id)

    @app.post("/call", status_code=201, dependencies=auth)
    async def create_call(body: dict[str, Any]) -> dict[str, Any]:
        """Outbound phone call, Vapi-style: the assistant calls `customer.number` from one of your numbers."""
        number = _get_or_404("phone_numbers", str(body.get("phoneNumberId") or ""))
        customer = str((body.get("customer") or {}).get("number") or "")
        if not customer:
            raise HTTPException(status_code=400, detail="customer.number is required (E.164, e.g. +5511999999999)")
        assistant = _get_or_404("assistants", str(body.get("assistantId") or number.get("assistantId") or ""))
        try:
            resolve_plan(assistant)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise HTTPException(status_code=400, detail="outbound calls need TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
        if not settings.public_url:
            raise HTTPException(status_code=400, detail="outbound calls need PUBLIC_URL (where Twilio reaches you)")

        call = store.create(
            "calls",
            {
                "type": "outboundPhoneCall",
                "status": "queued",
                "assistantId": assistant["id"],
                "phoneNumberId": number["id"],
                "customer": {"number": _e164(customer)},
            },
        )
        try:
            sid = await twilio.create_call(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                to=_e164(customer),
                from_=_e164(number["number"]),
                url=f"{settings.public_url}/twilio/voice?callId={call['id']}",
                status_callback=f"{settings.public_url}/twilio/status?callId={call['id']}",
            )
        except (TwilioError, httpx.HTTPError) as exc:
            store.update("calls", call["id"], {"status": "ended", "endedReason": "twilio-failed-to-connect-call"})
            raise HTTPException(status_code=400, detail=f"Twilio did not place the call: {exc}") from exc
        return store.update("calls", call["id"], {"phoneCallProviderId": sid})

    # Browser calls outlive the request that starts them; holding the tasks keeps them from being collected.
    web_calls: set[asyncio.Task] = set()

    @app.post("/call/web", status_code=201, dependencies=auth)
    async def create_web_call(body: dict[str, Any]) -> dict[str, Any]:
        """Talk to an assistant from the browser: answers the page's WebRTC offer and starts the call."""
        from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

        from .bot import run_call, web_transport

        assistant = _get_or_404("assistants", str(body.get("assistantId") or ""))
        if not body.get("sdp"):
            raise HTTPException(status_code=400, detail="sdp is required: the browser's WebRTC offer")
        try:
            plan = resolve_plan(assistant)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        connection = SmallWebRTCConnection()
        try:
            await connection.initialize(sdp=body["sdp"], type=body.get("type") or "offer")
        except Exception as exc:  # aiortc raises several error types for a malformed offer
            raise HTTPException(status_code=400, detail=f"invalid WebRTC offer: {exc}") from exc
        call = store.create(
            "calls",
            {"type": "webCall", "status": "in-progress", "startedAt": now_iso(), "assistantId": assistant["id"]},
        )
        task = asyncio.create_task(run_call(web_transport(connection), plan=plan, call_id=call["id"], store=store))
        web_calls.add(task)
        task.add_done_callback(web_calls.discard)
        answer = connection.get_answer()
        return {**call, "answer": {"sdp": answer["sdp"], "type": answer["type"]}}

    # ---- Twilio -------------------------------------------------------------------
    async def twilio_form(request: Request) -> dict[str, str]:
        """The webhook's form fields, once the request is proven to come from Twilio."""
        form = {k: str(v) for k, v in (await request.form()).items()}
        if not settings.validate_twilio_signature:
            return form
        # Without the token there is no way to tell Twilio from anyone else, so refuse.
        if not settings.twilio_auth_token:
            logger.error("refused a call: set TWILIO_AUTH_TOKEN so OpenRing can check it comes from Twilio")
            raise HTTPException(
                status_code=403, detail="TWILIO_AUTH_TOKEN is not set, so the Twilio signature cannot be checked"
            )
        # Twilio signs the exact URL it requested, query string included.
        base = settings.public_url or f"{request.url.scheme}://{request.url.netloc}"
        url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not is_valid_signature(settings.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        return form

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        form = await twilio_form(request)
        if not settings.public_url:
            logger.error("PUBLIC_URL is not set; Twilio cannot reach the media websocket")
            return Response(say_twiml("This line is not configured yet."), media_type="application/xml")

        # An outbound call we placed (POST /call): the callee answered and Twilio asks what to play.
        if call_id := request.query_params.get("callId"):
            call = store.get("calls", call_id)
            if not call or call.get("phoneCallProviderId") not in (None, form.get("CallSid")):
                return Response(say_twiml("Sorry, this call could not be connected."), media_type="application/xml")
            params = {"assistantId": call["assistantId"], "callId": call_id}
            return Response(stream_twiml(settings.public_ws_url + "/ws/twilio", params), media_type="application/xml")

        number = store.find_phone_number(form.get("To", ""))
        assistant_id = (number or {}).get("assistantId") or settings.default_assistant_id
        if not assistant_id or store.get("assistants", assistant_id) is None:
            return Response(say_twiml("No assistant is attached to this number."), media_type="application/xml")

        call = store.create(
            "calls",
            {
                "type": "inboundPhoneCall",
                "status": "queued",
                "assistantId": assistant_id,
                "phoneNumberId": (number or {}).get("id"),
                "customer": {"number": form.get("From")},
                "phoneCallProviderId": form.get("CallSid"),
            },
        )
        twiml = stream_twiml(
            settings.public_ws_url + "/ws/twilio",
            {"assistantId": assistant_id, "callId": call["id"]},
        )
        return Response(twiml, media_type="application/xml")

    @app.post("/twilio/status", status_code=204)
    async def twilio_status(request: Request) -> Response:
        """Final status of an outbound call. Records the calls that never reached the assistant."""
        form = await twilio_form(request)
        if reason := UNREACHED.get(form.get("CallStatus", "")):
            # Only a call still queued: one whose stream connected is closed by the bot itself.
            patch = {"status": "ended", "endedReason": reason, "endedAt": now_iso()}
            store.claim("calls", request.query_params.get("callId", ""), "queued", patch)
        return Response(status_code=204)

    @app.websocket("/ws/twilio")
    async def twilio_media(websocket: WebSocket) -> None:
        from pipecat.runner.utils import parse_telephony_websocket

        from .bot import PHONE_SAMPLE_RATE, run_call, twilio_transport

        await websocket.accept()
        try:
            transport, call_data = await asyncio.wait_for(parse_telephony_websocket(websocket), HANDSHAKE_TIMEOUT)
        except TimeoutError:
            await websocket.close(code=1008)
            return
        except ValueError:  # closed before the handshake
            return
        call_id = str((call_data.get("body") or {}).get("callId") or "")
        # Claiming moves the call out of "queued" in one step, so each call gets exactly one
        # media stream: a replayed or second connection for the same call is refused.
        call = None
        if transport == "twilio" and call_id:
            call = store.claim("calls", call_id, "queued", {"status": "in-progress", "startedAt": now_iso()})
        if call is None:
            logger.warning("refused media stream for call {}: unknown or already connected", call_id or "?")
            await websocket.close(code=1008)
            return
        # The assistant comes from the call record, never from the stream's parameters.
        assistant = store.get("assistants", call.get("assistantId") or "")
        if assistant is None:
            store.update("calls", call_id, {"status": "ended", "endedReason": "assistant-not-found"})
            await websocket.close()
            return
        try:
            plan = resolve_plan(assistant)
        except ProviderError as exc:
            logger.error("call {} cannot start: {}", call_id, exc)
            store.update("calls", call_id, {"status": "ended", "endedReason": f"provider-error: {exc}"})
            await websocket.close()
            return
        await run_call(
            twilio_transport(websocket, dict(call_data), settings),
            plan=plan,
            call_id=call_id,
            store=store,
            sample_rate=PHONE_SAMPLE_RATE,
        )

    return app
