"""FastAPI app: Vapi-compatible REST API + Twilio voice webhook + media websocket."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger

from . import __version__, auth, campaigns, genesys, keys, overrides, prices, providers, routes, stats, telnyx, twilio
from .config import Settings, get_settings, is_placeholder_key
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


class Refused(Exception):
    """The phone provider would not place a call; the call is already recorded as ended."""

    def __init__(self, call: dict[str, Any], detail: str):
        super().__init__(detail)
        self.call = call
        self.detail = detail


# Campaign fields the server keeps up to date; a client's copy of them is ignored.
CAMPAIGN_OWNED = {"id", "orgId", "createdAt", "updatedAt", "status", "endedReason", "endedAt", "error", "calls",
                  "callsCounterScheduled", "callsCounterQueued", "callsCounterInProgress",
                  "callsCounterEndedVoicemail", "callsCounterEnded"}
CAMPAIGN_UNSUPPORTED = ("workflowId", "squadId", "squadOverrides", "predialPlan", "duplicateFromCampaignId")


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = store or Store(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        routes.load_speed(store)  # how fast each model answered on earlier calls, for "fastest" routes
        dialer.resume()
        for assistant in store.list("assistants", limit=10_000):  # their local models load in the background
            providers.prepare_local(assistant)
        yield
        await dialer.close()

    app = FastAPI(title="OpenRing", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store

    if settings.api_key and is_placeholder_key(settings.api_key):
        logger.error("OPENRING_API_KEY is an example value anyone can guess; it is ignored. Set a long random key.")
        settings.api_key = ""
    if not settings.api_key:
        logger.warning("OPENRING_API_KEY is not set: the REST API only answers requests from this computer.")
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
            # No key to check: only this computer may call the API (tunnels and the network may not).
            if _is_direct_local_request(request) or signed_in(request):
                return
            raise HTTPException(
                status_code=401,
                detail="this server has no API key (OPENRING_API_KEY), so its API only answers this computer",
            )
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

    def _plan(assistant: dict[str, Any], **options: Any) -> providers.StackPlan:
        """The assistant's stack, with its model route (if any) filled in."""
        return resolve_plan(routes.expand(assistant, lambda route_id: store.get("routes", route_id)), **options)

    def _validate_assistant(doc: dict[str, Any]) -> None:
        route_id = (doc.get("model") or {}).get("routeId")
        if route_id and store.get("routes", str(route_id)) is None:
            raise HTTPException(status_code=400, detail=f"model.routeId: model route {route_id} not found")
        try:
            _plan(doc, check_keys=False)
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
            "genesysUrl": re.sub(r"^http", "ws", base) + "/ws/genesys",
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
                "generate": bool(spec.get("generate")),
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
        if provider == "genesys":
            settings.genesys_api_key = values["GENESYS_API_KEY"]
            settings.genesys_client_secret = values["GENESYS_CLIENT_SECRET"]
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

    @app.get("/providers/ollama/models", include_in_schema=False, dependencies=[Depends(require_session)])
    async def ollama_models() -> dict[str, Any]:
        """The models pulled into the local Ollama, or `running: false` when it isn't up."""
        base = (providers.LLM_PROVIDERS["ollama"][0] or "").rstrip("/").removesuffix("/v1")
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                res = await client.get(base + "/api/tags")
                res.raise_for_status()
                found = res.json().get("models") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return {"running": False, "models": [], "url": base}
        return {"running": True, "models": [m["name"] for m in found if isinstance(m, dict) and m.get("name")],
                "url": base}

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
        providers.prepare_local(body)
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
        providers.prepare_local({**current, **body})
        return store.update("assistants", assistant_id, body)

    @app.delete("/assistant/{assistant_id}", dependencies=auth_deps)
    def delete_assistant(assistant_id: str) -> dict[str, Any]:
        _get_or_404("assistants", assistant_id)
        return store.delete("assistants", assistant_id)

    # ---- Model routes: models in order, shared by assistants (an OpenRing addition, like OmniRoute's combos)
    def _route_doc(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            doc = routes.clean({**(current or {}), **body})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for i, model in enumerate(doc["models"]):
            try:
                resolve_plan({"model": model}, check_keys=False, with_fallbacks=False)
            except ProviderError as exc:
                raise HTTPException(status_code=400, detail=f"models[{i}]: {exc}") from exc
        return doc

    @app.post("/model-route", status_code=201, dependencies=auth_deps)
    def create_route(body: dict[str, Any]) -> dict[str, Any]:
        return routes.with_status(store.create("routes", _route_doc(body)))

    @app.get("/model-route", dependencies=auth_deps)
    def list_routes() -> list[dict[str, Any]]:
        return [routes.with_status(r) for r in store.list("routes", limit=1000)]

    @app.get("/model-route/{route_id}", dependencies=auth_deps)
    def get_route(route_id: str) -> dict[str, Any]:
        return routes.with_status(_get_or_404("routes", route_id))

    @app.patch("/model-route/{route_id}", dependencies=auth_deps)
    def update_route(route_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("routes", route_id)
        if "preset" not in body and any(k in body for k in routes.SETTINGS):
            body = {**body, "preset": "custom"}  # changing what a preset decides makes the route custom
        return routes.with_status(store.update("routes", route_id, _route_doc(body, current)))

    @app.post("/model-route/{route_id}/measure", dependencies=auth_deps)
    async def measure_route(route_id: str) -> dict[str, Any]:
        """Ask each model of the route for one short answer and time its first word, as a call would (a few
        tokens each). "fastest" routes order by these times and by the ones calls measure."""
        route = _get_or_404("routes", route_id)
        models = route.get("models") or []
        plans = [resolve_plan({"model": m}, check_keys=False, with_fallbacks=False) for m in models]
        results = await asyncio.gather(*(routes.probe(m, p) for m, p in zip(models, plans)))
        routes.save_speed(store)
        return {**routes.with_status(route), "measured": list(results)}

    @app.get("/prices", dependencies=auth_deps)
    def price_list() -> dict[str, Any]:
        """The list prices calls are priced at: models per 1M tokens, speech-to-text per minute, voices per 1M
        characters (with ~/.openring/prices.json applied)."""
        return prices.table()

    @app.delete("/model-route/{route_id}", dependencies=auth_deps)
    def delete_route(route_id: str) -> dict[str, Any]:
        """Assistants on a deleted route go back to the model saved in their own `model`."""
        _get_or_404("routes", route_id)
        return store.delete("routes", route_id)

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

    def _outbound_number(number_id: str) -> dict[str, Any]:
        number = _get_or_404("phone_numbers", number_id)
        if (number.get("provider") or "twilio") != "twilio":
            raise HTTPException(status_code=400, detail="outbound calls only go through Twilio numbers for now")
        return number

    def _customer(value: Any, where: str) -> dict[str, Any]:
        """A Vapi customer with its number in E.164. Its `assistantOverrides` go to the call, not the record."""
        customer = dict(value) if isinstance(value, dict) else {}
        if customer.get("sipUri") and not customer.get("number"):
            raise HTTPException(status_code=400, detail=f"{where}.sipUri: SIP calls are not supported yet; use number")
        if not any(ch.isdigit() for ch in str(customer.get("number") or "")):
            raise HTTPException(status_code=400, detail=f"{where}.number is required (E.164, e.g. +5511999999999)")
        customer["number"] = _e164(str(customer["number"]))
        return customer

    def _check_outbound(assistant: dict[str, Any]) -> None:
        """Everything an outbound call needs before Twilio dials: a working assistant, Twilio, a public URL."""
        _validate_assistant(assistant)
        try:
            _plan(assistant)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise HTTPException(status_code=400, detail="outbound calls need TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
        if not settings.public_url:
            raise HTTPException(status_code=400, detail="outbound calls need PUBLIC_URL (where Twilio reaches you)")

    async def dial(number: dict[str, Any], customer: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        """Has Twilio call `customer` from `number`. `record` names the assistant (and overrides) for the call."""
        person = {k: v for k, v in customer.items() if k != "assistantOverrides"}
        call = store.create("calls", {"type": "outboundPhoneCall", "status": "queued", **record,
                                      "phoneNumberId": number["id"], "customer": person})
        try:
            sid = await twilio.create_call(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                to=person["number"],
                from_=_e164(number["number"]),
                url=f"{settings.public_url}/twilio/voice?callId={call['id']}",
                status_callback=f"{settings.public_url}/twilio/status?callId={call['id']}",
            )
        except (TwilioError, httpx.HTTPError) as exc:
            patch = {"status": "ended", "endedReason": "twilio-failed-to-connect-call", "endedAt": now_iso()}
            raise Refused(store.update("calls", call["id"], patch), str(exc)) from exc
        return store.update("calls", call["id"], {"phoneCallProviderId": sid})

    @app.post("/call", status_code=201, dependencies=auth_deps)
    async def create_call(body: dict[str, Any]) -> dict[str, Any]:
        """Outbound phone call, Vapi-style: the assistant calls `customer.number` from one of your numbers."""
        number = _outbound_number(str(body.get("phoneNumberId") or ""))
        customer = _customer(body.get("customer"), "customer")
        transient = body.get("assistant") if isinstance(body.get("assistant"), dict) else None
        if transient is not None:
            _validate_assistant(transient)
            assistant = transient
        else:
            assistant = _get_or_404("assistants", str(body.get("assistantId") or number.get("assistantId") or ""))
        extra = overrides.combine(customer.get("assistantOverrides"), body.get("assistantOverrides"))
        _check_outbound(overrides.apply(assistant, extra, customer))
        # A transient assistant lives in the call record, as on Vapi.
        record: dict[str, Any] = {"assistant": transient} if transient is not None else {"assistantId": assistant["id"]}
        if extra:
            record["assistantOverrides"] = extra
        try:
            return await dial(number, customer, record)
        except Refused as exc:
            raise HTTPException(status_code=400, detail=f"Twilio did not place the call: {exc.detail}") from exc

    # ---- Campaigns (Vapi's /campaign): one assistant calls a list of people --------------
    def _campaign_doc(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        """The campaign as stored: checked the way a run would trip on it, numbers in E.164."""
        doc = {k: v for k, v in {**(current or {}), **body}.items() if k not in CAMPAIGN_OWNED}
        if not str(doc.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="name is required")
        for key in CAMPAIGN_UNSUPPORTED:
            if doc.get(key):
                raise HTTPException(status_code=400, detail=f"{key} is not supported yet")
        if doc.get("dialPlan"):
            if doc.get("phoneNumberId") or doc.get("customers"):
                raise HTTPException(status_code=400, detail="use phoneNumberId with customers, or dialPlan, not both")
            if not isinstance(doc["dialPlan"], list):
                raise HTTPException(status_code=400, detail="dialPlan must be a list of {phoneNumberId, customers}")
            entries = [e if isinstance(e, dict) else {} for e in doc["dialPlan"]]
            doc["dialPlan"] = [
                {**entry, "phoneNumberId": str(entry.get("phoneNumberId") or ""),
                 "customers": [_customer(c, f"dialPlan[{i}].customers[{j}]")
                               for j, c in enumerate(entry.get("customers") or [])]}
                for i, entry in enumerate(entries)
            ]
        else:
            if not doc.get("phoneNumberId"):
                raise HTTPException(status_code=400, detail="phoneNumberId is required: the number that calls")
            if not isinstance(doc.get("customers"), list) or not doc["customers"]:
                raise HTTPException(status_code=400,
                                    detail='customers is required: who to call, e.g. [{"number": "+5511912345678"}]')
            doc["customers"] = [_customer(c, f"customers[{i}]") for i, c in enumerate(doc["customers"])]
        planned = campaigns.dials(doc)
        if not planned:
            raise HTTPException(status_code=400, detail="the campaign has nobody to call")
        if len(planned) > campaigns.MAX_CUSTOMERS:
            raise HTTPException(status_code=400, detail=f"a campaign calls at most {campaigns.MAX_CUSTOMERS:,} people")
        try:
            concurrency = int(doc.get("maxConcurrency") or campaigns.DEFAULT_CONCURRENCY)
        except (TypeError, ValueError):
            concurrency = 0
        if not 1 <= concurrency <= campaigns.MAX_CONCURRENCY:
            raise HTTPException(status_code=400, detail=f"maxConcurrency goes from 1 to {campaigns.MAX_CONCURRENCY}")
        doc["maxConcurrency"] = concurrency
        if (schedule := doc.get("schedulePlan")) is not None:
            if not isinstance(schedule, dict) or not schedule.get("earliestAt"):
                raise HTTPException(status_code=400, detail="schedulePlan.earliestAt is required")
            try:
                earliest = campaigns.parse_time(schedule["earliestAt"])
                latest = campaigns.parse_time(schedule.get("latestAt"))
            except ValueError as exc:
                raise HTTPException(status_code=400,
                                    detail="schedulePlan times are ISO 8601, e.g. 2026-11-03T13:00:00Z") from exc
            if latest and earliest and latest <= earliest:
                raise HTTPException(status_code=400, detail="schedulePlan.latestAt must come after earliestAt")
        base = doc.get("assistantOverrides")
        checked: set[str] = set()
        for number_id, customer in planned:
            number = _outbound_number(number_id)
            assistant_id = doc.get("assistantId") or number.get("assistantId")
            if not assistant_id:
                raise HTTPException(status_code=400,
                                    detail=f"assistantId is required: {number['number']} has no assistant attached")
            assistant = _get_or_404("assistants", str(assistant_id))
            if number_id not in checked:
                _check_outbound(overrides.apply(assistant, base))
                checked.add(number_id)
            if overrides.changes_assistant(customer.get("assistantOverrides")):
                _check_outbound(overrides.apply(assistant, overrides.combine(base, customer["assistantOverrides"]),
                                                customer))
        return doc

    async def dial_for_campaign(campaign: dict[str, Any], number_id: str, customer: dict[str, Any]) -> dict[str, Any]:
        try:
            number = _outbound_number(number_id)
            assistant = _get_or_404("assistants", str(campaign.get("assistantId") or number.get("assistantId") or ""))
            extra = overrides.combine(campaign.get("assistantOverrides"), customer.get("assistantOverrides"))
            _check_outbound(overrides.apply(assistant, extra, customer))
        except HTTPException as exc:
            raise campaigns.Stop(str(exc.detail)) from exc
        record = {"assistantId": assistant["id"], "campaignId": campaign["id"],
                  **({"assistantOverrides": extra} if extra else {})}
        try:
            return await dial(number, customer, record)
        except Refused as exc:
            logger.warning("campaign {}: Twilio did not call {}: {}", campaign["id"], customer["number"], exc.detail)
            return exc.call

    dialer = campaigns.Dialer(store, dial_for_campaign)
    app.state.dialer = dialer

    @app.post("/campaign", status_code=201, dependencies=auth_deps)
    async def create_campaign(body: dict[str, Any]) -> dict[str, Any]:
        doc = _campaign_doc(body)
        campaign = store.create("campaigns", {**doc, "status": "scheduled", "calls": {},
                                              **campaigns.counters({**doc, "calls": {}})})
        dialer.start(campaign["id"])
        return campaign

    @app.get("/campaign", dependencies=auth_deps)
    def list_campaigns(limit: int = 100, status: str | None = None) -> dict[str, Any]:
        """Paginated like Vapi's: `{results, metadata}`."""
        found = [c for c in store.list("campaigns", limit=campaigns.MAX_CUSTOMERS)
                 if not status or c.get("status") == status]
        results = found[:max(limit, 0)]
        meta = {"itemsPerPage": len(results), "totalItems": len(found), "currentPage": 1}
        return {"results": results, "metadata": meta}

    @app.get("/campaign/{campaign_id}", dependencies=auth_deps)
    def get_campaign(campaign_id: str) -> dict[str, Any]:
        return _get_or_404("campaigns", campaign_id)

    @app.patch("/campaign/{campaign_id}", dependencies=auth_deps)
    async def update_campaign(campaign_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Rename it, set `status` to "cancelled" (or "ended", Vapi's older word) to stop it, or change it before
        it starts."""
        current = _get_or_404("campaigns", campaign_id)
        changes = {k: v for k, v in body.items() if k not in CAMPAIGN_OWNED}
        if set(changes) - {"name"}:
            if current.get("status") != "scheduled":
                raise HTTPException(status_code=400, detail="a campaign that started can only be renamed or cancelled")
            changes = _campaign_doc(changes, current)
        elif "name" in changes and not str(changes["name"] or "").strip():
            raise HTTPException(status_code=400, detail="name is required")
        if "status" not in body:
            return store.update("campaigns", campaign_id, changes)
        if body["status"] not in ("cancelled", "ended"):
            raise HTTPException(status_code=400, detail='status: set "cancelled" to stop the campaign')
        # The dialer may start the campaign meanwhile, so the stop is claimed from the status it has now.
        for _ in range(2):
            status = (store.get("campaigns", campaign_id) or {}).get("status")
            if status not in campaigns.LIVE:
                raise HTTPException(status_code=400, detail=f"the campaign is already {status}")
            reason = f"campaign.{status}.ended-by-user"
            stop = {**changes, "status": "cancelled", "endedReason": reason, "endedAt": now_iso()}
            if stopped := store.claim("campaigns", campaign_id, status, stop):
                return stopped
        raise HTTPException(status_code=409, detail="the campaign changed while stopping it; try again")

    @app.delete("/campaign/{campaign_id}", dependencies=auth_deps)
    def delete_campaign(campaign_id: str) -> dict[str, Any]:
        """Deletes the campaign; it calls nobody else, and calls already going finish."""
        _get_or_404("campaigns", campaign_id)
        return store.delete("campaigns", campaign_id)

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
        # Variables have no caller to fill them here; they say nothing rather than "{{customer.name}}".
        assistant = overrides.apply(assistant, body.get("assistantOverrides"))
        if overrides.changes_assistant(body.get("assistantOverrides")):
            _validate_assistant(assistant)
        try:
            plan = _plan(assistant)
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

    async def run_phone_call(
        call: dict[str, Any],
        refuse: Callable[[str], Awaitable[None]],
        build: Callable[[], Any],
        transfer: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Run a claimed phone call with its assistant. `refuse(why)` hangs up when the call can't start."""
        from .bot import PHONE_SAMPLE_RATE, run_call

        call_id = call["id"]
        logger.info("call {} media stream connected", call_id)
        # The assistant comes from the call record, never from the stream's parameters.
        assistant = call.get("assistant") or store.get("assistants", call.get("assistantId") or "")
        if assistant is None:
            store.update("calls", call_id, {"status": "ended", "endedReason": "assistant-not-found"})
            await refuse("the call's assistant was deleted")
            return
        # This call's overrides and variables: a campaign's `{{customer.name}}`, an assistant-request's changes.
        assistant = overrides.apply(assistant, call.get("assistantOverrides"), call.get("customer"))
        try:
            plan = _plan(assistant)
        except ProviderError as exc:
            logger.error("call {} cannot start: {}", call_id, exc)
            store.update("calls", call_id, {"status": "ended", "endedReason": f"provider-error: {exc}"})
            await refuse(str(exc))
            return
        await run_call(build(), plan=plan, call_id=call_id, store=store, sample_rate=PHONE_SAMPLE_RATE,
                       transfer=transfer)

    async def media_stream(websocket: WebSocket, provider: str) -> None:
        from pipecat.runner.utils import parse_telephony_websocket

        from .bot import telnyx_transport, twilio_transport

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
        transfer = None
        call_sid = call.get("phoneCallProviderId") or call_data.get("call_id")
        if provider == "twilio" and settings.twilio_account_sid and settings.twilio_auth_token and call_sid:

            async def transfer(number: str) -> None:
                twiml = twilio.dial_twiml(_e164(number))
                await twilio.redirect_call(settings.twilio_account_sid, settings.twilio_auth_token, call_sid, twiml)

        async def hang_up(_why: str) -> None:
            await websocket.close()

        build = twilio_transport if provider == "twilio" else telnyx_transport
        await run_phone_call(call, hang_up, lambda: build(websocket, dict(call_data), settings), transfer)

    @app.websocket("/ws/twilio")
    async def twilio_media(websocket: WebSocket) -> None:
        await media_stream(websocket, "twilio")

    @app.websocket("/ws/telnyx")
    async def telnyx_media(websocket: WebSocket) -> None:
        await media_stream(websocket, "telnyx")

    # ---- Genesys Cloud (Audio Connector, AudioHook protocol) ------------------------
    def genesys_refusal(websocket: WebSocket) -> str | None:
        """Why this connection is not Genesys with our Audio Connector credentials, or None when it is."""
        if not (settings.genesys_api_key and settings.genesys_client_secret):
            return "set GENESYS_API_KEY and GENESYS_CLIENT_SECRET (Providers > Genesys Cloud)"
        given = websocket.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), settings.genesys_api_key.encode()):
            return "wrong X-API-KEY"
        target = websocket.url.path + (f"?{websocket.url.query}" if websocket.url.query else "")
        authority = urlsplit(settings.public_url).netloc or websocket.headers.get("host", "")
        return genesys.check_signature(dict(websocket.headers.items()), target, authority,
                                       api_key=settings.genesys_api_key, client_secret=settings.genesys_client_secret)

    @app.websocket("/ws/genesys")
    async def genesys_media(websocket: WebSocket) -> None:
        """A Genesys Cloud call. The Architect flow picks the assistant with an `assistantId` input variable, or
        the called number does; other input variables fill the assistant's {{variables}}. Back in Architect,
        output variables say what happened: `action` is "finished" or "transfer" (with `transferTo`)."""
        from pipecat.serializers.genesys import GenesysAudioHookSerializer

        from .bot import genesys_transport

        if problem := genesys_refusal(websocket):
            logger.warning("refused a Genesys connection: {}", problem)
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_text(), HANDSHAKE_TIMEOUT)
            opening = json.loads(first)
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValueError, KeyError):  # silence, or not JSON text
            await websocket.close(code=1008)
            return
        if not isinstance(opening, dict) or opening.get("type") != "open":
            await websocket.close(code=1008)
            return
        serializer = GenesysAudioHookSerializer()
        opened = await serializer.deserialize(first)  # also keeps the session's caller and variables
        await websocket.send_text(json.dumps(opened.message))

        async def refuse(why: str) -> None:
            """Ends the session with an error, so the Architect flow takes its failure path."""
            logger.warning("Genesys call {} refused: {}", serializer.conversation_id, why)
            message = serializer.create_disconnect_message(reason="error", action="finished", info=why)
            await websocket.send_text(json.dumps(message))
            await websocket.close()

        participant = serializer.participant or {}
        inputs = dict(serializer.input_variables or {})
        caller, called = genesys.number(participant.get("ani")), genesys.number(participant.get("dnis"))
        if wanted := str(inputs.pop("assistantId", "") or ""):
            if store.get("assistants", wanted) is None:
                await refuse(f"assistant {wanted} not found")
                return
            number = store.find_phone_number(called) if called else None
            call: dict[str, Any] | str = store.create("calls", {
                "type": "inboundPhoneCall", "status": "queued", "assistantId": wanted,
                "phoneNumberId": (number or {}).get("id"), "customer": {"number": caller},
                "phoneCallProvider": "genesys", "phoneCallProviderId": serializer.conversation_id,
            })
        else:
            call = await answer_inbound(caller, called, serializer.conversation_id, "genesys")
        if isinstance(call, str):
            await refuse(call)
            return
        customer = dict(call.get("customer") or {})
        if participant.get("aniName"):
            customer["name"] = participant["aniName"]
        patch: dict[str, Any] = {"customer": customer, "status": "in-progress", "startedAt": now_iso()}
        if inputs:
            patch["assistantOverrides"] = overrides.combine({"variableValues": inputs}, call.get("assistantOverrides"))
        claimed = store.claim("calls", call["id"], "queued", patch)
        if claimed is None:
            await refuse("the call is already connected")
            return
        serializer.set_output_variables({"action": "finished", "callId": claimed["id"]})

        async def transfer(number: str) -> None:
            """Genesys moves the call: the Architect flow reads `action` and `transferTo` when the connector ends."""
            serializer.set_output_variables({"action": "transfer", "transferTo": number, "callId": claimed["id"]})
            message = serializer.create_disconnect_message(reason="completed",
                                                           output_variables=serializer.output_variables)
            await websocket.send_text(json.dumps(message))

        await run_phone_call(claimed, refuse, lambda: genesys_transport(websocket, serializer), transfer)

    return app
