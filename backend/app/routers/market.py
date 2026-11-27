from fastapi import APIRouter, Query, Request
from datetime import date
from typing import Literal

from ..schemas import Capital
from ..services import market_service as service
from ..workbench_schemas import Code, Correlation, Kline, Preset, Radar, Universe

router = APIRouter(prefix="/market", tags=["Market"])


@router.get("/universe", response_model=Universe, operation_id="getMarketUniverse", summary="PIT 股票池与因子截面")
def universe(request: Request, preset: Preset = "all", industry: str = Query("", max_length=64),
             q: str = Query("", max_length=64), capital_rmb: Capital = Capital.MAIN):
    return service.universe(request.app.state.settings, preset, industry.strip(), q.strip(), capital_rmb)


@router.get("/kline/{code}", response_model=Kline, operation_id="getMarketKline", summary="前复权 K 线和影子成交")
def kline(request: Request, code: Code, limit: int = Query(240, ge=1, le=600), capital_rmb: Capital = Capital.MAIN,
          as_of: date | None = None):
    return service.kline(request.app.state.settings, code, limit, capital_rmb, as_of.isoformat() if as_of else None)


@router.get("/radar", response_model=Radar, operation_id="getMarketRadar", summary="ETF 动量雷达与指数择时")
def radar(request: Request):
    return service.radar(request.app.state.settings)


@router.get("/correlation", response_model=Correlation, operation_id="getMarketCorrelation", summary="股票收益相关性")
def correlation(request: Request, scope: Literal["held_and_top20"] = "held_and_top20",
                lookback: int = Query(120, ge=20, le=252), capital_rmb: Capital = Capital.MAIN):
    return service.correlation(request.app.state.settings, lookback, capital_rmb)
