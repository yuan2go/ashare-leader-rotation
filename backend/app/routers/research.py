from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..schemas import Capital, Drawdown, FactorCourt, Placebo, Portfolio, ResearchCoverage, Scenario, Window, YearReturn
from ..services import research_service as service
from ..workbench_schemas import Multialpha

router = APIRouter(prefix="/research",tags=["Research"])
Phase = Literal["4b","4c","6a"]


@router.get("/multialpha", response_model=Multialpha, operation_id="getMultialpha", summary="Phase 9–10 多引擎与跨资产")
def multialpha(request: Request):
    return service.multialpha(request.app.state.settings)


class ArchiveFilter(BaseModel):
    phase: Phase | None = None
    key: str | None = None
    pool: str | None = None


class PortfolioFilter(ArchiveFilter):
    window: Window = "W1"
    capital_rmb: Capital = Capital.MAIN
    scenario: Scenario = "2bp"


class DrawdownFilter(ArchiveFilter):
    scope: Literal["W1","IS","OOS","W_Bear","2026H2"] = "W1"


@router.get("/portfolios",response_model=list[Portfolio],operation_id="getResearchPortfolios",summary="11 年签发组合与窗口比较")
def portfolios(request: Request, filters: Annotated[PortfolioFilter,Query()]):
    options = filters.model_dump(exclude={"capital_rmb"})
    return service.portfolios(request.app.state.settings,capital=filters.capital_rmb,**options)


@router.get("/yearly-returns",response_model=list[YearReturn],operation_id="getResearchYearlyReturns",summary="签发逐年收益")
def yearly(request: Request, filters: Annotated[ArchiveFilter,Query()]):
    return service.years(request.app.state.settings,**filters.model_dump())


@router.get("/drawdowns",response_model=list[Drawdown],operation_id="getResearchDrawdowns",summary="回撤事件和恢复期")
def drawdowns(request: Request, filters: Annotated[DrawdownFilter,Query()]):
    return service.drawdowns(request.app.state.settings,**filters.model_dump())


@router.get("/factor-court",response_model=list[FactorCourt],operation_id="getResearchFactorCourt",summary="Phase 4A 因子法庭")
def factor_court(request: Request, pool: Literal["PIT300","PIT300_ex688"]="PIT300_ex688",
                 window: Literal["IS","OOS"]="IS", factor: str | None=None):
    return service.factor_court(request.app.state.settings,pool,window,factor)


@router.get("/placebos",response_model=list[Placebo],operation_id="getResearchPlacebos",summary="M200 与目标波动率置换检验")
def placebo(request: Request, phase: Phase | None=None, window: Window="OOS",
            key: str | None=None, include_draws: bool=False):
    return service.placebos(request.app.state.settings,phase,window,key,include_draws)


@router.get("/coverage",response_model=ResearchCoverage,operation_id="getResearchCoverage",summary="签发 PIT 行情覆盖与验收范围")
def coverage(request: Request):
    return service.coverage(request.app.state.settings)
