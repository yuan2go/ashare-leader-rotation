from uuid import UUID
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from ..schemas import CollectionStatus, Error, Job, JobLog, JobRequest
from ..services import ops_service

router = APIRouter(prefix="/ops",tags=["Operations"])


@router.get("/collection-status",response_model=list[CollectionStatus],operation_id="getCollectionStatus",
            summary="采集状态及原始更新时间")
def collection(request: Request):
    return ops_service.collection(request.app.state.settings)


@router.get("/jobs",response_model=list[Job],operation_id="listJobs",summary="最近运维任务")
def jobs(request: Request,limit: int=Query(50,ge=1,le=200)):
    return ops_service.reconcile(request.app.state.settings)[:limit]


def require_writable(request: Request):
    if request.app.state.settings.api_read_only:
        raise HTTPException(403, "backend is running in read-only mode")


def referer_origin(value):
    try:
        url = urlsplit(value)
        if url.scheme in ("http", "https") and url.hostname and url.username is None:
            return f"{url.scheme}://{url.netloc}"
    except ValueError:
        pass
    return ""


def site_origin(request, settings):
    if settings.trust_proxy_headers:
        protocols = request.headers.getlist("x-forwarded-proto")
        hosts = request.headers.getlist("x-forwarded-host")
        if protocols or hosts:
            if len(protocols) == len(hosts) == 1 and protocols[0] in ("http", "https"):
                proto, authority = protocols[0], hosts[0]
                try:
                    url = urlsplit(f"{proto}://{authority}")
                    hostname = url.hostname
                    if hostname and ":" in hostname:
                        hostname = f"[{hostname}]"
                    port = url.port
                    if hostname in settings.allowed_hosts and authority.lower() in {
                        hostname, f"{hostname}:{port}",
                    }:
                        default_port = 80 if proto == "http" else 443
                        endpoint = f"{hostname}:{port}" if port is not None and port != default_port else hostname
                        return f"{proto}://{endpoint}"
                except ValueError:
                    pass
            raise HTTPException(403, "forwarded origin not allowed")
    return str(request.base_url).rstrip("/")


def validate_write_origin(request: Request, x_shadow_ops: str):
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is None and referer is not None:
        origin = referer_origin(referer)
    allowed = (*settings.cors_origins, site_origin(request, settings))
    if (x_shadow_ops != "1" or request.headers.get("sec-fetch-site", "").lower() == "cross-site"
            or origin is not None and origin not in allowed):
        raise HTTPException(403,"operation origin or header not allowed")


@router.post("/jobs",response_model=Job,status_code=202,operation_id="submitJob",summary="启动一个受控运维任务",
             responses={403:{"model":Error},409:{"model":Error}}, dependencies=[Depends(require_writable)])
def submit(payload: JobRequest, request: Request, x_shadow_ops: str=Header()):
    validate_write_origin(request, x_shadow_ops)
    settings = request.app.state.settings
    if settings.database!=settings.shadow_dir/"shadow_v7a.sqlite3":
        raise HTTPException(409,"operation requires the configured Phase 7A ledger path")
    return ops_service.submit(settings,payload)


@router.get("/jobs/{job_id}",response_model=Job,operation_id="getJob",summary="轮询运维任务",
            responses={404:{"model":Error}})
def job(job_id: UUID, request: Request):
    for value in ops_service.reconcile(request.app.state.settings):
        if value.id==job_id:
            return value
    raise HTTPException(404,"task not found")


@router.get("/jobs/{job_id}/logs",response_model=JobLog,operation_id="getJobLog",summary="读取任务日志尾部",
            responses={404:{"model":Error}})
def logs(job_id: UUID, request: Request,max_bytes: int=Query(32768,ge=256,le=262144)):
    try:
        return ops_service.read_log(request.app.state.settings,job_id,max_bytes)
    except FileNotFoundError as exc:
        raise HTTPException(404,"task not found") from exc
