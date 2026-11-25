import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from core.integrity import IntegrityError, require_runtime_guard

from .config import Settings
from .routers import health, ops, research, shadow, tickets
from .schemas import Error
from .services.ops_service import Busy

VERSION = "1.0.0"
log = logging.getLogger(__name__)


class FrontendStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if full_path.endswith((".js", ".mjs")) and response.status_code != 304:
            # Windows may register .js as text/plain, which browsers reject for modules.
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        return response


def _error(status,code,message):
    return JSONResponse(status_code=status,content=Error(code=code,message=message).model_dump())


def create_app(settings=None):
    require_runtime_guard()
    app = FastAPI(title="A 股影子账本 API",version=VERSION,
                  description="Phase 7A 六线影子账本、签发研究归档与本地运维接口")
    app.state.settings = settings or Settings.from_env()
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["localhost","127.0.0.1","[::1]"])
    app.add_middleware(CORSMiddleware,allow_origins=app.state.settings.cors_origins,
                       allow_methods=["GET","POST"],allow_headers=["Content-Type","X-Shadow-Ops"],
                       expose_headers=["Content-Disposition"])

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

    for module in (health,shadow,tickets,research,ops):
        app.include_router(module.router,prefix="/api/v1",
                           responses={422:{"model":Error},503:{"model":Error},500:{"model":Error}})
    dist = app.state.settings.frontend_dist
    if (dist/"index.html").is_file():
        def frontend_index():
            return FileResponse(dist/"index.html")

        for path in ("/tickets", "/portfolio", "/research", "/ops"):
            app.add_api_route(path,frontend_index,methods=["GET","HEAD"],include_in_schema=False)
        app.mount("/",FrontendStaticFiles(directory=dist,html=True),name="frontend")
    return app


app = create_app()
