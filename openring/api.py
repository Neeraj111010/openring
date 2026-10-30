"""FastAPI app: Vapi-compatible REST API + Twilio voice webhook + media websocket."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger

from . import __version__, auth, keys, providers, stats, telnyx, twilio
from .config import Settings, get_settings
from .importer import SourceError, import_account
from .providers import ProviderError, resolve_plan
from .server_messages import AssistantRequestError, request_assistant, server_of
from .store import Store, now_iso
from .tools import ToolError, check_tool
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

    sessions = auth.Sessions(store)
    throttle = auth.Throttle()
    if settings.password and not sessions.password_set:
        if problem := auth.password_problem(settings.password):
            logger.error("OPENRING_PASSWORD ignored: {}", problem)
        else:
            sessions.set_password(settings.password)

    def signed_in(request: Request) -> bool:
        """A dashboard session: the login cookie plus the header only the dashboard sends."""
        if request.headers.get(auth.DASHBOARD_HEADER) != "dashboard":
            return False
        return sessions.valid(request.cookies.get(auth.COOKIE, ""))

    def require_key(request: Request, authorization: str = Header(default="")) -> None:
        if not settings.api_key:
            return
        token = authorization.removeprefix("Bearer ").strip()
        if token and hmac.compare_digest(token.encode(), settings.api_key.encode()):
            return
        if signed_in(request):
            return
        raise HTTPException(status_code=401, detail="invalid or missing API key")

    def require_session(request: Request) -> None:
        if not signed_in(request):
            raise HTTPException(status_code=401, detail="sign in to the dashboard")

    auth_deps = [Depends(require_key)]

    def with_session_cookie(body: dict[str, Any], request: Request) -> JSONResponse:
        response = JSONResponse(body)
        response.set_cookie(
            auth.COOKIE,
            sessions.start(),
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    def _get_or_404(collection: str, item_id: str) -> dict[str, Any]:
        doc = store.get(collection, item_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"{collection[:-1].replace('_', ' ')} not found")
        return doc

    def _validate_assistant(doc: dict[str, Any]) -> None:
        try:
            resolve_plan(doc, check_keys=False)
            model = doc.get("model") or {}
            for tool in model.get("tools") or []:
                check_tool(tool)
        except (ProviderError, ToolError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for tool_id in (doc.get("model") or {}).get("toolIds") or []:
            if store.get("tools", str(tool_id)) is None:
                raise HTTPException(status_code=400, detail=f"model.toolIds: tool {tool_id} not found")

    def _validate_tool(doc: dict[str, Any]) -> None:
        try:
            check_tool(doc)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        # Revalidate every time, so an upgraded OpenRing never shows yesterday's dashboard.
        return FileResponse(Path(__file__).parent / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ---- Dashboard login ---------------------------------------------------------
    @app.get("/auth/state", include_in_schema=False)
    def auth_state(request: Request) -> dict[str, Any]:
        return {
            "passwordSet": sessions.password_set,
            "signedIn": signed_in(request),
            # Before a password exists, only someone at this computer may choose it: through a
            # tunnel, a stranger with the URL could otherwise take the dashboard first.
            "canSetUp": _is_direct_local_request(request),
            "version": __version__,
        }

    @app.post("/auth/setup", include_in_schema=False)
    def auth_setup(body: dict[str, Any], request: Request) -> JSONResponse:
        if sessions.password_set:
            raise HTTPException(status_code=409, detail="a password is already set; sign in instead")
        if not _is_direct_local_request(request):
            raise HTTPException(
                status_code=403,
                detail="choose the password from the server's own computer (http://localhost), "
                "or set OPENRING_PASSWORD and restart",
            )
        password = str(body.get("password") or "")
        if problem := auth.password_problem(password):
            raise HTTPException(status_code=400, detail=problem)
        sessions.set_password(password)
        return with_session_cookie({"signedIn": True}, request)

    @app.post("/auth/login", include_in_schema=False)
    def auth_login(body: dict[str, Any], request: Request) -> JSONResponse:
        who = request.client.host if request.client else "?"
        if throttle.blocked(who):
            raise HTTPException(status_code=429, detail="too many wrong passwords; wait a few minutes")
        if not sessions.check(str(body.get("password") or "")):
            throttle.fail(who)
            raise HTTPException(status_code=401, detail="wrong password")
        throttle.clear(who)
        return with_session_cookie({"signedIn": True}, request)

    @app.post("/auth/logout", include_in_schema=False)
    def auth_logout(request: Request) -> JSONResponse:
        sessions.end(request.cookies.get(auth.COOKIE, ""))
        response = JSONResponse({"signedIn": False})
        response.delete_cookie(auth.COOKIE)
        return response

    @app.post("/auth/password", include_in_schema=False, dependencies=[Depends(require_session)])
    def auth_change_password(body: dict[str, Any], request: Request) -> JSONResponse:
        if not sessions.check(str(body.get("current") or "")):
            raise HTTPException(status_code=400, detail="the current password is wrong")
        password = str(body.get("password") or "")
        if problem := auth.password_problem(password):
            raise HTTPException(status_code=400, detail=problem)
        sessions.set_password(password)  # signs out every other browser
        return with_session_cookie({"signedIn": True}, request)

    @app.get("/dashboard/info", include_in_schema=False, dependencies=[Depends(require_session)])
    def dashboard_info(request: Request) -> dict[str, Any]:
        """What the Settings and Providers pages show. Provider keys are reported as set or not, never shown."""
        base = settings.public_url or f"{request.url.scheme}://{request.url.netloc}"
        return {
            "version": __version__,
            "apiKey": settings.api_key,
            "publicUrl": settings.public_url,
            "baseUrl": base,
            "twilioVoiceUrl": base + "/twilio/voice",
            "telnyxVoiceUrl": base + "/telnyx/voice",
            "envFile": os.environ.get("OPENRING_ENV_FILE", ""),
            "providers": [_with_connect(p) for p in providers.provider_status(settings)],
        }

    def _with_connect(row: dict[str, Any]) -> dict[str, Any]:
        """Tell the Providers page what to ask for. Says which fields are set, never their values."""
        spec = keys.CONNECTORS.get(row["id"])
        if spec:
            set_now = keys.current_values(row["id"])
            row["connect"] = {
                "fields": [{"env": env, "label": label, "set": bool(set_now[env])} for env, label in spec["fields"]],
                "keysUrl": spec["keysUrl"],
                "oauth": bool(spec.get("oauth")),
                "balance": row["id"] in keys.BALANCES,
            }
        return row

    def _use(provider: str, values: dict[str, str]) -> dict[str, Any]:
        saved = keys.apply(values)
        if provider == "twilio":  # read once at startup, so refresh the live settings too
            settings.twilio_account_sid = values["TWILIO_ACCOUNT_SID"]
            settings.twilio_auth_token = values["TWILIO_AUTH_TOKEN"]
        if provider == "telnyx":
            settings.telnyx_api_key = values["TELNYX_API_KEY"]
            settings.telnyx_public_key = values["TELNYX_PUBLIC_KEY"]
        where = str(keys.env_file()) if saved else None
        return {"connected": bool(all(values.values())), "saved": saved, "envFile": where}

    @app.post("/providers/{provider}/connect", include_in_schema=False, dependencies=[Depends(require_session)])
    async def connect_provider(provider: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            values = keys.clean_values(provider, body.get("values") or {})
            checked = await keys.check(provider, values)
        except keys.ConnectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**_use(provider, values), "checked": checked}

    @app.post("/providers/{provider}/test", include_in_schema=False, dependencies=[Depends(require_session)])
    async def test_provider(provider: str) -> dict[str, Any]:
        if provider not in keys.CONNECTORS:
            raise HTTPException(status_code=404, detail="unknown provider")
        values = keys.current_values(provider)
        if not all(values.values()):
            raise HTTPException(status_code=400, detail=f"{keys.NAMES[provider]} is not connected")
        try:
            checked = await keys.check(provider, values)
        except keys.ConnectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "checked": checked}

    @app.get("/providers/{provider}/balance", include_in_schema=False, dependencies=[Depends(require_session)])
    async def provider_balance(provider: str) -> dict[str, Any]:
        """What's left on the provider account (money or characters); null when the provider won't say."""
        if provider not in keys.BALANCES:
            raise HTTPException(status_code=404, detail="this provider doesn't report a balance")
        values = keys.current_values(provider)
        if not all(values.values()):
            raise HTTPException(status_code=400, detail=f"{keys.NAMES[provider]} is not connected")
        return {"balance": await keys.balance(provider, values)}

    @app.delete("/providers/{provider}/connect", include_in_schema=False, dependencies=[Depends(require_session)])
    def disconnect_provider(provider: str) -> dict[str, Any]:
        if provider not in keys.CONNECTORS:
            raise HTTPException(status_code=404, detail="unknown provider")
        return _use(provider, {env: "" for env, _ in keys.CONNECTORS[provider]["fields"]})

    @app.post("/providers/openrouter/oauth", include_in_schema=False, dependencies=[Depends(require_session)])
    async def openrouter_oauth(body: dict[str, Any]) -> dict[str, Any]:
        code, verifier = str(body.get("code") or ""), str(body.get("codeVerifier") or "")
        if not code or not verifier:
            raise HTTPException(status_code=400, detail="the OpenRouter sign-in did not finish; try again")
        try:
            key = await keys.exchange_openrouter_code(code, verifier)
            values = keys.clean_values("openrouter", {"OPENROUTER_API_KEY": key})
        except keys.ConnectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _use("openrouter", values)

    @app.get("/stats", dependencies=auth_deps)
    def call_stats(days: int | None = 30) -> dict[str, Any]:
        """Totals for the dashboard's overview; `days=0` covers every call."""
        return stats.summarize(store.list("calls", limit=100_000), days or None)

    # ---- Assistants (same JSON shape as Vapi's /assistant) -------------------
    @app.post("/assistant", status_code=201, dependencies=auth_deps)
    def create_assistant(body: dict[str, Any]) -> dict[str, Any]:
        _validate_assistant(body)
        return store.create("assistants", body)

    @app.get("/assistant", dependencies=auth_deps)
    def list_assistants(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("assistants", limit)

    @app.get("/assistant/{assistant_id}", dependencies=auth_deps)
    def get_assistant(assistant_id: str) -> dict[str, Any]:
        return _get_or_404("assistants", assistant_id)

    @app.patch("/assistant/{assistant_id}", dependencies=auth_deps)
    def update_assistant(assistant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("assistants", assistant_id)
        _validate_assistant({**current, **body})
        return store.update("assistants", assistant_id, body)

    @app.delete("/assistant/{assistant_id}", dependencies=auth_deps)
    def delete_assistant(assistant_id: str) -> dict[str, Any]:
        _get_or_404("assistants", assistant_id)
        return store.delete("assistants", assistant_id)

    # ---- Tools (Vapi's /tool: saved once, used by assistants through model.toolIds) ----
    @app.post("/tool", status_code=201, dependencies=auth_deps)
    def create_tool(body: dict[str, Any]) -> dict[str, Any]:
        _validate_tool(body)
        return store.create("tools", body)

    @app.get("/tool", dependencies=auth_deps)
    def list_tools(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("tools", limit)

    @app.get("/tool/{tool_id}", dependencies=auth_deps)
    def get_tool(tool_id: str) -> dict[str, Any]:
        return _get_or_404("tools", tool_id)

    @app.patch("/tool/{tool_id}", dependencies=auth_deps)
    def update_tool(tool_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("tools", tool_id)
        _validate_tool({**current, **body})
        return store.update("tools", tool_id, body)

    @app.delete("/tool/{tool_id}", dependencies=auth_deps)
    def delete_tool(tool_id: str) -> dict[str, Any]:
        _get_or_404("tools", tool_id)
        return store.delete("tools", tool_id)

    # ---- Import from another Vapi-compatible API ------------------------------------
    @app.post("/import", dependencies=auth_deps)
    async def import_from_api(body: dict[str, Any]) -> dict[str, Any]:
        """Copy an account's tools, assistants and Twilio numbers. The key is used once, never stored."""
        try:
            return await import_account(str(body.get("baseUrl") or "https://api.vapi.ai"),
                                        str(body.get("apiKey") or ""), store)
        except SourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Phone numbers --------------------------------------------------------
    @app.post("/phone-number", status_code=201, dependencies=auth_deps)
    def create_phone_number(body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("number"):
            raise HTTPException(status_code=400, detail="number is required (E.164, e.g. +15551234567)")
        if body.get("assistantId"):
            _get_or_404("assistants", body["assistantId"])
        body.setdefault("provider", "twilio")
        return store.create("phone_numbers", body)

    @app.get("/phone-number", dependencies=auth_deps)
    def list_phone_numbers(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("phone_numbers", limit)

    @app.get("/phone-number/{number_id}", dependencies=auth_deps)
    def get_phone_number(number_id: str) -> dict[str, Any]:
        return _get_or_404("phone_numbers", number_id)

    @app.patch("/phone-number/{number_id}", dependencies=auth_deps)
    def update_phone_number(number_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _get_or_404("phone_numbers", number_id)
        if body.get("assistantId"):
            _get_or_404("assistants", body["assistantId"])
        return store.update("phone_numbers", number_id, body)

    @app.delete("/phone-number/{number_id}", dependencies=auth_deps)
    def delete_phone_number(number_id: str) -> dict[str, Any]:
        _get_or_404("phone_numbers", number_id)
        return store.delete("phone_numbers", number_id)

    # ---- Calls ------------------------------------------------------------------
    @app.get("/call", dependencies=auth_deps)
    def list_calls(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("calls", limit)

    @app.get("/call/{call_id}", dependencies=auth_deps)
    def get_call(call_id: str) -> dict[str, Any]:
        return _get_or_404("calls", call_id)

    @app.post("/call", status_code=201, dependencies=auth_deps)
    async def create_call(body: dict[str, Any]) -> dict[str, Any]:
        """Outbound phone call, Vapi-style: the assistant calls `customer.number` from one of your numbers."""
        number = _get_or_404("phone_numbers", str(body.get("phoneNumberId") or ""))
        if (number.get("provider") or "twilio") != "twilio":
            raise HTTPException(status_code=400, detail="outbound calls only go through Twilio numbers for now")
        customer = str((body.get("customer") or {}).get("number") or "")
        if not customer:
            raise HTTPException(status_code=400, detail="customer.number is required (E.164, e.g. +5511999999999)")
        transient = body.get("assistant") if isinstance(body.get("assistant"), dict) else None
        if transient is not None:
            _validate_assistant(transient)
            assistant = transient
        else:
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
                # A transient assistant lives in the call record, as on Vapi.
                **({"assistant": transient} if transient is not None else {"assistantId": assistant["id"]}),
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

    @app.post("/call/web", status_code=201, dependencies=auth_deps)
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
            logger.warning("refused a request to {}: invalid Twilio signature", url)
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        return form

    async def answer_inbound(
        caller: str, called: str, provider_call_id: str | None, provider: str
    ) -> dict[str, Any] | str:
        """Record an incoming call and pick its assistant. Returns the call, or what to tell the caller."""
        number = store.find_phone_number(called)
        assistant_id = (number or {}).get("assistantId")
        ask = server_of(number) if number and not assistant_id else None
        assistant_id = assistant_id or (None if ask else settings.default_assistant_id)
        if not ask and (not assistant_id or store.get("assistants", assistant_id) is None):
            return "No assistant is attached to this number."

        call = store.create(
            "calls",
            {
                "type": "inboundPhoneCall",
                "status": "queued",
                "assistantId": assistant_id,
                "phoneNumberId": (number or {}).get("id"),
                "customer": {"number": caller},
                "phoneCallProvider": provider,
                "phoneCallProviderId": provider_call_id,
            },
        )
        if ask:
            try:
                answer = await request_assistant(ask, call, number)
                if "assistantId" in answer and store.get("assistants", answer["assistantId"]) is None:
                    raise AssistantRequestError("assistant-request-returned-unknown-assistant",
                                                f"assistant {answer['assistantId']} not found")
                if "assistant" in answer:
                    _validate_assistant(answer["assistant"])
            except (AssistantRequestError, HTTPException) as exc:
                reason = getattr(exc, "ended_reason", "assistant-request-returned-invalid-assistant")
                logger.warning("call {}: assistant-request to {} failed: {}", call["id"], ask["url"],
                               getattr(exc, "detail", exc))
                store.update("calls", call["id"], {"status": "ended", "endedReason": reason, "endedAt": now_iso()})
                return "Sorry, this line cannot take calls right now."
            call = store.update("calls", call["id"], answer)
        logger.info("{} call {} from {} to {}; streaming it to {}", provider, call["id"], caller, called,
                    call.get("assistantId") or "a transient assistant")
        return call

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
                logger.warning("call {}: Twilio call {} is not this call; not connecting", call_id, form.get("CallSid"))
                return Response(say_twiml("Sorry, this call could not be connected."), media_type="application/xml")
            logger.info("call {} answered; streaming it to its assistant", call_id)
            params = {"callId": call_id, **({"assistantId": call["assistantId"]} if call.get("assistantId") else {})}
            return Response(stream_twiml(settings.public_ws_url + "/ws/twilio", params), media_type="application/xml")

        answer = await answer_inbound(form.get("From", ""), form.get("To", ""), form.get("CallSid"), "twilio")
        if isinstance(answer, str):
            return Response(say_twiml(answer), media_type="application/xml")
        params = {"callId": answer["id"]}
        if answer.get("assistantId"):
            params["assistantId"] = answer["assistantId"]
        return Response(stream_twiml(settings.public_ws_url + "/ws/twilio", params), media_type="application/xml")

    async def telnyx_form(request: Request) -> dict[str, str]:
        """The webhook's fields, once its Ed25519 signature proves it comes from Telnyx."""
        body = await request.body()
        if settings.validate_telnyx_signature:
            if not settings.telnyx_public_key:
                logger.error("refused a call: set TELNYX_PUBLIC_KEY so OpenRing can check it comes from Telnyx")
                raise HTTPException(status_code=403,
                                    detail="TELNYX_PUBLIC_KEY is not set, so the signature cannot be checked")
            timestamp = request.headers.get("telnyx-timestamp", "")
            signature = request.headers.get("telnyx-signature-ed25519", "")
            if not telnyx.is_valid_signature(settings.telnyx_public_key, timestamp, body, signature):
                logger.warning("refused a request to {}: invalid Telnyx signature", request.url.path)
                raise HTTPException(status_code=403, detail="invalid Telnyx signature")
        if request.headers.get("content-type", "").startswith("application/json"):
            data = json.loads(body or b"{}")
            return {str(k): str(v) for k, v in (data if isinstance(data, dict) else {}).items()}
        return {k: str(v) for k, v in (await request.form()).items()}

    @app.post("/telnyx/voice")
    async def telnyx_voice(request: Request) -> Response:
        """Voice URL of a Telnyx TeXML Application: connects an incoming call to its assistant."""
        form = await telnyx_form(request)
        if not settings.public_url:
            logger.error("PUBLIC_URL is not set; Telnyx cannot reach the media websocket")
            return Response(say_twiml("This line is not configured yet."), media_type="application/xml")
        answer = await answer_inbound(form.get("From", ""), form.get("To", ""),
                                      form.get("CallSid") or form.get("CallControlId"), "telnyx")
        if isinstance(answer, str):
            return Response(say_twiml(answer), media_type="application/xml")
        # Telnyx's stream start carries no custom parameters, so the call id rides in the websocket URL.
        url = f"{settings.public_ws_url}/ws/telnyx?callId={answer['id']}"
        return Response(telnyx.stream_texml(url), media_type="application/xml")

    @app.post("/twilio/status", status_code=204)
    async def twilio_status(request: Request) -> Response:
        """Final status of an outbound call. Records the calls that never reached the assistant."""
        form = await twilio_form(request)
        logger.info("call {}: Twilio status {}", request.query_params.get("callId"), form.get("CallStatus"))
        if reason := UNREACHED.get(form.get("CallStatus", "")):
            # Only a call still queued: one whose stream connected is closed by the bot itself.
            patch = {"status": "ended", "endedReason": reason, "endedAt": now_iso()}
            store.claim("calls", request.query_params.get("callId", ""), "queued", patch)
        return Response(status_code=204)

    async def media_stream(websocket: WebSocket, provider: str) -> None:
        from pipecat.runner.utils import parse_telephony_websocket

        from .bot import PHONE_SAMPLE_RATE, run_call, telnyx_transport, twilio_transport

        await websocket.accept()
        try:
            transport, call_data = await asyncio.wait_for(parse_telephony_websocket(websocket), HANDSHAKE_TIMEOUT)
        except TimeoutError:
            await websocket.close(code=1008)
            return
        except ValueError:  # closed before the handshake
            return
        if provider == "twilio":
            call_id = str((call_data.get("body") or {}).get("callId") or "")
        else:
            call_id = websocket.query_params.get("callId", "")
        # Claiming moves the call out of "queued" in one step, so each call gets exactly one
        # media stream: a replayed or second connection for the same call is refused.
        call = None
        if transport == provider and call_id:
            call = store.claim("calls", call_id, "queued", {"status": "in-progress", "startedAt": now_iso()})
        if call is None:
            logger.warning("refused media stream for call {}: unknown or already connected", call_id or "?")
            await websocket.close(code=1008)
            return
        logger.info("call {} media stream connected", call_id)
        # The assistant comes from the call record, never from the stream's parameters.
        assistant = call.get("assistant") or store.get("assistants", call.get("assistantId") or "")
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
        transfer = None
        call_sid = call.get("phoneCallProviderId") or call_data.get("call_id")
        if provider == "twilio" and settings.twilio_account_sid and settings.twilio_auth_token and call_sid:

            async def transfer(number: str) -> None:
                twiml = twilio.dial_twiml(_e164(number))
                await twilio.redirect_call(settings.twilio_account_sid, settings.twilio_auth_token, call_sid, twiml)

        build = twilio_transport if provider == "twilio" else telnyx_transport
        await run_call(
            build(websocket, dict(call_data), settings),
            plan=plan,
            call_id=call_id,
            store=store,
            sample_rate=PHONE_SAMPLE_RATE,
            transfer=transfer,
        )

    @app.websocket("/ws/twilio")
    async def twilio_media(websocket: WebSocket) -> None:
        await media_stream(websocket, "twilio")

    @app.websocket("/ws/telnyx")
    async def telnyx_media(websocket: WebSocket) -> None:
        await media_stream(websocket, "telnyx")

    return app
