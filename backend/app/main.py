import base64
import binascii
import hmac
import logging
import sqlite3
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from core.integrity import IntegrityError, require_runtime_guard

from .config import Settings
from .routers import health, market, ops, research, shadow, tickets, wallet
from .schemas import Error
from .services.auto_refresh import lifespan
from .services.ops_service import Busy

VERSION = "1.0.0"
log = logging.getLogger(__name__)


class FrontendStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            url_path = scope["path"].strip("/")
            reserved = url_path in {"api", "assets"} or url_path.startswith(
                ("api/", "docs", "redoc", "openapi.json", "assets/")
            )
            if exc.status_code != 404 or scope["method"] not in ("GET", "HEAD") or reserved or "." in url_path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if full_path.endswith((".js", ".mjs")) and response.status_code != 304:
            # Windows may register .js as text/plain, which browsers reject for modules.
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        return response


def _error(status,code,message):
    return JSONResponse(status_code=status,content=Error(code=code,message=message).model_dump())


class IPv6TrustedHostMiddleware(TrustedHostMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not self.allow_any:
            host = Headers(scope=scope).get("host", "")
            if host.startswith("["):
                try:
                    # Starlette splits at the first colon, including inside an IPv6 literal.
                    url = urlsplit(f"http://{host}")
                    literal = f"[{url.hostname}]"
                    if literal in self.allowed_hosts and host.lower() in (literal, f"{literal}:{url.port}"):
                        return await self.app(scope, receive, send)
                except ValueError:
                    pass
        return await super().__call__(scope, receive, send)


class BasicAuthMiddleware:
    def __init__(self, app, *, settings):
        self.app = app
        self.user = settings.basic_auth_user.encode("utf-8")
        self.password = settings.basic_auth_password.encode("utf-8")
        self.health_public = settings.health_public

    def authenticated(self, headers):
        values = headers.getlist("authorization")
        if len(values) != 1:
            return False
        scheme, _, encoded = values[0].partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            user, separator, password = base64.b64decode(encoded, validate=True).partition(b":")
        except (ValueError, binascii.Error):
            return False
        user_matches = hmac.compare_digest(user, self.user)
        password_matches = hmac.compare_digest(password, self.password)
        return bool(separator) and user_matches and password_matches

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            public_health = (
                self.health_public and scope["method"] == "GET" and scope["path"] == "/api/v1/health"
            )
            if not public_health and not self.authenticated(Headers(scope=scope)):
                response = _error(401, "unauthorized", "authentication required")
                response.headers["WWW-Authenticate"] = 'Basic realm="A-Share Shadow Cockpit"'
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(settings=None):
    require_runtime_guard()
    app = FastAPI(title="A 股影子账本 API",version=VERSION,lifespan=lifespan,
                  description="Phase 7A 六线影子账本、签发研究归档与本地运维接口")
    app.state.settings = settings or Settings.from_env()
    settings = app.state.settings
    if bool(settings.basic_auth_user) != bool(settings.basic_auth_password):
        raise ValueError("PHASE7A_BASIC_AUTH_USER and PHASE7A_BASIC_AUTH_PASS must be configured together")
    if settings.basic_auth_user and ":" in settings.basic_auth_user:
        raise ValueError("PHASE7A_BASIC_AUTH_USER must not contain ':'")
    if not settings.basic_auth_user:
        app.add_middleware(IPv6TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(CORSMiddleware,allow_origins=settings.cors_origins,
                       allow_methods=["GET","POST","DELETE"],allow_headers=["Content-Type","X-Shadow-Ops"],
                       expose_headers=["Content-Disposition"])
    if settings.basic_auth_user:
        app.add_middleware(BasicAuthMiddleware, settings=settings)
        app.add_middleware(IPv6TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        message = "; ".join(f"{'.'.join(map(str,item['loc']))}: {item['msg']}" for item in exc.errors())
        return _error(422,"validation_error",message)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        code = {403:"forbidden",404:"not_found",409:"conflict"}.get(exc.status_code,"http_error")
        return _error(exc.status_code,code,str(exc.detail))

    @app.exception_handler(Busy)
    async def busy(request,exc):
        return _error(409,"job_busy",str(exc))

    @app.exception_handler(FileNotFoundError)
    @app.exception_handler(sqlite3.OperationalError)
    @app.exception_handler(IntegrityError)
    async def unavailable(request,exc):
        log.warning("Local input unavailable for %s (%s)",request.url.path,type(exc).__name__)
        return _error(503,"local_input_unavailable","local data unavailable or integrity verification failed")

    @app.exception_handler(Exception)
    async def unexpected(request,exc):
        log.error("Failed %s (%s)",request.url.path,type(exc).__name__,exc_info=exc)
        return _error(500,"internal_error","unexpected backend error")

    for module in (health,shadow,tickets,research,ops,market,wallet):
        app.include_router(module.router,prefix="/api/v1",
                           responses={422:{"model":Error},503:{"model":Error},500:{"model":Error}})
    dist = app.state.settings.frontend_dist
    if (dist/"index.html").is_file():
        # A mount keeps the history fallback outside the OpenAPI schema.
        app.mount("/",FrontendStaticFiles(directory=dist,html=True),name="frontend")
    return app


app = create_app()
