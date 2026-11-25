from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request

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


@router.post("/jobs",response_model=Job,status_code=202,operation_id="submitJob",summary="启动一个受控运维任务",
             responses={403:{"model":Error},409:{"model":Error}})
def submit(payload: JobRequest, request: Request, x_shadow_ops: str=Header()):
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if x_shadow_ops!="1" or origin and origin not in settings.cors_origins and origin!=str(request.base_url).rstrip("/"):
        raise HTTPException(403,"operation origin or header not allowed")
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
