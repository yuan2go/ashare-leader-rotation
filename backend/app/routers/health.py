import platform
import sqlite3
from importlib.metadata import version

from fastapi import APIRouter, Request

from core.integrity import APPROVED_V4, inspect_protected_manifest, verify_frozen_locks

from ..config import ROOT
from ..db import read_snapshot
from ..schemas import Health, IntegritySummary

router = APIRouter(tags=["Health"])
DEPENDENCIES = ("fastapi","uvicorn","pydantic","httpx")


@router.get("/health",response_model=Health,operation_id="getHealth",summary="服务、运行时和冻结引擎")
def health(request: Request):
    settings = request.app.state.settings
    hashes = verify_frozen_locks(ROOT,settings.engine_lock)
    with read_snapshot(settings.database) as db:
        day = db.execute("SELECT MAX(day) FROM sessions").fetchone()[0]
    return dict(status="ok",api_version=request.app.version,python_version=platform.python_version(),
                sqlite_version=sqlite3.sqlite_version,sqlite_readonly=True,latest_session=day,
                dependency_versions={name:version(name) for name in DEPENDENCIES},
                frozen_sha256={name:hashes[name] for name in APPROVED_V4})


@router.get("/health/integrity",response_model=IntegritySummary,operation_id="getIntegrity",
            summary="实时核对 Phase 7A 受保护文件")
def integrity(request: Request):
    settings = request.app.state.settings
    frozen = verify_frozen_locks(ROOT,settings.engine_lock,exact_lock_bytes=True)
    actual, missing, changed = inspect_protected_manifest(ROOT, settings.manifest)
    return dict(ok=not missing and not changed,checked_files=len(actual),missing_files=missing,changed_files=changed,
                frozen_sha256={name:frozen[name] for name in APPROVED_V4})
