from datetime import date

from fastapi import APIRouter, Depends, Header, Request

from ..schemas import Capital, Error
from ..services import wallet_service as service
from ..workbench_schemas import Benchmark, WalletMutation, WalletSnapshotInput, WalletSummary
from .ops import require_writable, validate_write_origin

router = APIRouter(prefix="/wallet", tags=["Wallet"])


@router.get("/summary", response_model=WalletSummary, operation_id="getWalletSummary", summary="实盘钱包与影子账户对账")
def summary(request: Request, benchmark_account: Benchmark = "E4M", capital_rmb: Capital = Capital.MAIN):
    return service.summary(request.app.state.settings, benchmark_account, capital_rmb)


@router.post("/snapshot", response_model=WalletMutation, operation_id="saveWalletSnapshot", summary="录入实盘账户快照",
             dependencies=[Depends(require_writable)], responses={403: {"model": Error}})
def save(request: Request, snapshot: WalletSnapshotInput, x_shadow_ops: str = Header()):
    validate_write_origin(request, x_shadow_ops)
    return service.save(request.app.state.settings, snapshot)


@router.delete("/snapshot/{day}", response_model=WalletMutation, operation_id="deleteWalletSnapshot", summary="删除指定实盘快照",
               dependencies=[Depends(require_writable)], responses={403: {"model": Error}, 404: {"model": Error}})
def delete(request: Request, day: date, x_shadow_ops: str = Header()):
    validate_write_origin(request, x_shadow_ops)
    return service.delete(request.app.state.settings, day.isoformat())
