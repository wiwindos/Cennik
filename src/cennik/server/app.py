"""Private full-feature server, owner portal and isolated collector endpoint."""
from __future__ import annotations

import hashlib
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from cennik.collector.server import create_hub
from cennik.core.models import Model
from cennik.workspace.presentation import page
from cennik.workspace.web import create_app as create_workspace_app

from .auth import OwnerAuth
from .bridge import DiscoverCharacters, ExchangeRequest, PrivateBridge, RegisterDevice
from .catalog import Catalog, MarketProfile
from .collectors import ServerCollectors
from .hub_auth import verify_hub
from .pairing import PairApprove, Pairing, PairPoll, PairStart


class SaveMarket(Model):
    profile: MarketProfile
    ahl_source: bool = False
    enable_updates: bool | None = None


class CollectorInvite(Model):
    label: str = Field(min_length=1, max_length=80)
    profile_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")


def create_server(root: Path, owner_key: str, *, public_origin="http://127.0.0.1:8770", hub_secret=None):
    hub_secret = hub_secret if hub_secret is not None else os.environ.get("CENNIK_HUB_SECRET", "")
    if hub_secret and len(hub_secret) < 32:
        raise ValueError("CENNIK_HUB_SECRET должен содержать минимум 32 символа")
    address = urlparse(public_origin)
    if address.path.rstrip('/') or address.query or address.fragment or address.username or address.password or not address.hostname:
        raise ValueError("Нужен внешний адрес сервера без пути и параметров")
    if address.scheme != "https" and not (address.scheme == "http" and address.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("Публичному серверу нужен HTTPS; HTTP разрешён только на localhost")
    if (root / "workspace.json").exists() or (root / "market.duckdb").exists():
        raise ValueError("Укажите отдельную папку серверной версии, не локальную рабочую базу")
    auth = OwnerAuth(owner_key, public_origin)
    catalog = Catalog(root)
    intake = create_hub(root / "collector-archive", owner_key)
    collectors = ServerCollectors(catalog, intake.state.hub)
    bridge = PrivateBridge(catalog)
    pairing = Pairing(bridge)
    bridge.recover()
    mounted = set()
    analysis_slots = threading.Semaphore(1)
    static = Path(__file__).resolve().parents[2] / "ahledger/static"

    @asynccontextmanager
    async def lifespan(app):
        catalog.running = True
        for workspace in catalog.workspaces.values():
            workspace.thread.start()
        collectors.thread.start()
        try:
            yield
        finally:
            collectors.stop.set()
            collectors.thread.join(timeout=15)
            catalog.close()

    app = FastAPI(title="Cennik Server", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.catalog, app.state.bridge, app.state.hub, app.state.collectors = catalog, bridge, intake.state.hub, collectors

    def mount_profile(profile):
        if profile.id in mounted:
            return
        w = catalog.workspace(profile.id)
        w.collectors = collectors.adapter(w, profile)
        w.resolve_realm_names = lambda: catalog.resolve_realms(catalog.sources.markets()["items"])
        old_calculate = w.calculate
        def calculate():
            with analysis_slots:
                return old_calculate()
        w.calculate = calculate
        app.mount("/m/"+profile.id, create_workspace_app(w.root, workspace=w, remote=True))
        mounted.add(profile.id)

    @app.middleware("http")
    async def access(request: Request, call_next):
        path = request.url.path
        hub = verify_hub(request, hub_secret)
        hub_access = bool(hub and hub["access"] == "cennik")
        if hub_access:
            request.state.cennik_mount = "/cennik"
        pair_public = path in {"/pair/v1/start", "/pair/v1/poll", "/pair/v1/ack"}
        public = pair_public or path in {"/", "/login", "/healthz", "/auth/login"} or path.startswith("/static/") or path.startswith("/collect/")
        private_bridge = path.startswith("/bridge/v1/")
        if private_bridge:
            try:
                request.state.device = bridge.authenticate(request.headers.get("X-Bridge-Key", ""))
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        elif not public:
            if not hub_access and not auth.authenticated(request):
                return JSONResponse({"detail": "Нужен вход владельца"}, status_code=401)
            request.state.cennik_owner = True
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not private_bridge and not pair_public and not path.startswith("/collect/"):
            if origin != auth.origin and not request.headers.get("X-Owner-Key") and not hub_access:
                return JSONResponse({"detail": "Запрос должен исходить со страницы Cennik"}, status_code=403)
            if origin and origin != auth.origin:
                return JSONResponse({"detail": "Другой источник страницы"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH"} and not path.startswith("/collect/"):
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 64_000_000:
                    return JSONResponse({"detail": "Запрос превышает 64 МБ"}, status_code=413)
            request._body = bytes(data)
            if hub and hub.get("body_sha256") != hashlib.sha256(request._body).hexdigest():
                return JSONResponse({"detail": "Подпись тела запроса хаба не совпадает"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "cennik-server", "format_version": 1}

    @app.get("/")
    def portal(request: Request):
        if not getattr(request.state, "cennik_mount", "") and not auth.authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return page(static / "server.html", request)

    @app.get("/login")
    def login_page():
        return FileResponse(static / "server-login.html")

    @app.post("/auth/login")
    async def login(request: Request):
        data = await request.json()
        value = data.get("key", "") if isinstance(data, dict) else ""
        if not isinstance(value, str) or len(value) > 200:
            raise HTTPException(401, "Неверный ключ владельца")
        token = auth.login(value, request.client.host if request.client else "unknown")
        response = JSONResponse({"authenticated": True})
        response.set_cookie(auth.cookie, token, httponly=True, secure=address.scheme == "https", samesite="strict", max_age=43200)
        return response

    @app.post("/auth/logout")
    def logout(request: Request):
        auth.logout(request)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(auth.cookie)
        return response

    @app.get("/server/api/state")
    def state():
        return {"profiles": catalog.public(), "ahl_profile_id": catalog.config().get("ahl_profile_id"),
            "collector_url": auth.origin+"/collect", "worker_url": auth.origin,
            "pairing": pairing.pending(),
            "devices": [{k: v for k, v in d.items() if k != "token_hash"} for d in bridge.devices()]}

    @app.post("/server/api/markets")
    def save_market(value: SaveMarket):
        profile = catalog.save(value.profile, source=value.ahl_source)
        mount_profile(profile)
        if value.enable_updates is not None:
            w=catalog.workspace(profile.id)
            w.save_settings(w.settings().model_copy(update={"auto_refresh":value.enable_updates,"auto_recalculate":value.enable_updates}))
        return profile.public()

    @app.get("/server/api/ahl/markets")
    def ahl_markets():
        result = catalog.sources.markets()
        catalog.resolve_realms(result["items"])
        return result

    @app.post("/server/api/markets/{identity}/workers")
    def register_worker(identity: str, value: RegisterDevice):
        return bridge.register(identity, value)

    @app.post("/pair/v1/start")
    def pair_start(value: PairStart, request: Request):
        hub = verify_hub(request, hub_secret)
        return pairing.start(value, hub.get("address", "unknown") if hub else request.client.host if request.client else "unknown")

    @app.post("/pair/v1/poll")
    def pair_poll(value: PairPoll):
        return pairing.poll(value)

    @app.post("/pair/v1/ack")
    def pair_ack(value: PairPoll):
        return pairing.acknowledge(value)

    @app.post("/server/api/pairing/approve")
    def pair_approve(value: PairApprove):
        return pairing.approve(value)

    @app.delete("/server/api/workers/{identity}")
    def revoke_worker(identity: str):
        return bridge.revoke(identity)

    @app.post("/server/api/collectors")
    def invite_collector(value: CollectorInvite):
        if value.profile_id:
            catalog.profile(value.profile_id)
        return intake.state.hub.invite(value.label, profile_id=value.profile_id)

    @app.get("/bridge/v1/manifest")
    def manifest(request: Request):
        return bridge.manifest(request.state.device, extended=request.headers.get("X-Cennik-Worker")=="0.2")

    @app.post("/bridge/v1/characters")
    def discovered_characters(value: DiscoverCharacters, request: Request):
        return bridge.discover(request.state.device, value)

    @app.post("/bridge/v1/exchange", status_code=202)
    def exchange(value: ExchangeRequest, request: Request):
        return bridge.exchange(request.state.device, value)

    @app.get("/bridge/v1/exchanges/{client_id}")
    def exchange_state(client_id: str, request: Request):
        return bridge.status(request.state.device, client_id)

    for profile in catalog.profiles():
        mount_profile(profile)
    app.mount("/collect", intake)
    return app
