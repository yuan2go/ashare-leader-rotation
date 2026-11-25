from typing import Annotated

from fastapi import APIRouter, Query, Request

from ..db import read_snapshot
from ..schemas import AccountFilter, CorporateAction, Fill, Holding, NavPoint, Overview, Page, PagedAccountFilter, RiskSnapshot
from ..services import shadow_service as service

router = APIRouter(prefix="/shadow",tags=["Shadow"])


@router.get("/overview",response_model=Overview,operation_id="getShadowOverview",summary="六条路径的最新影子账本")
def overview(request: Request):
    return service.overview(request.app.state.settings)


@router.get("/nav",response_model=list[NavPoint],operation_id="getShadowNav",summary="逐日净值、现金、回撤和超额")
def nav(request: Request, filters: Annotated[AccountFilter,Query()]):
    return service.nav(request.app.state.settings,filters)


@router.get("/holdings",response_model=Page[Holding],operation_id="getShadowHoldings",summary="持仓和收盘权重")
def holdings(request: Request, filters: Annotated[PagedAccountFilter,Query()]):
    return service.holdings(request.app.state.settings,filters)


@router.get("/fills",response_model=Page[Fill],operation_id="getShadowFills",summary="成交、费用和滑点")
def fills(request: Request, filters: Annotated[PagedAccountFilter,Query()]):
    return service.fills(request.app.state.settings,filters)


@router.get("/corporate-actions",response_model=Page[CorporateAction],operation_id="getShadowCorporateActions",
            summary="公司行动和应收、到账记录")
def corporate_actions(request: Request, filters: Annotated[PagedAccountFilter,Query()]):
    return service.actions(request.app.state.settings,filters)


@router.get("/risk",response_model=RiskSnapshot,operation_id="getShadowRisk",summary="M200、V5 波动率和下一信号日")
def risk(request: Request):
    with read_snapshot(request.app.state.settings.database) as db:
        return service.risk_snapshot(db)
