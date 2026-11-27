"""Typed payloads for the Phase 11 workbench."""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .schemas import Account, Capital, Side, TrendState

Code = Annotated[str, StringConstraints(pattern=r"^[0-9]{6}\.(SH|SZ)$")]
Preset = Literal["all", "valuediv50", "ch4_50", "held"]
TradeStatus = Literal["open", "suspended", "up_limit", "down_limit"]
Benchmark = Literal["E4M", "DEF50"]


class StockRow(BaseModel):
    code: Code
    name: str
    industry: str
    close: float | None
    change_pct: float | None
    pe_ttm: float | None
    dv_ttm: float | None
    valuediv_rank: int | None
    valuediv_score: float | None
    ivol_60d: float | None
    neg_max_20d: float | None
    ch4_rank: int | None
    amount_20d_avg_rmb: float | None
    status: TradeStatus
    held_in: list[Account]
    in_buffer: bool


class Universe(BaseModel):
    as_of: date
    basic_as_of: date | None
    pool: str
    pool_size: int
    industries: list[str]
    items: list[StockRow]


class Bar(BaseModel):
    day: date
    open: float
    high: float
    low: float
    close: float
    vol: float | None
    amount: float | None
    ma20: float | None
    ma60: float | None
    ma200: float | None


class FillMarker(BaseModel):
    day: date
    side: Side
    account: Account
    price_rmb: float
    chart_price_rmb: float | None
    shares: float
    reason: str


class MarketEvent(BaseModel):
    day: date
    kind: str
    announced: date | None
    cash_per_share: float | None
    shares_per_share: float | None
    pay_date: date | None


class InstrumentCard(BaseModel):
    code: Code
    name: str
    asset: Literal["stock", "etf"]
    pool: str
    industry: str
    valuediv_rank: int | None
    ch4_rank: int | None
    held_in: list[Account]


class Kline(BaseModel):
    as_of: date
    price_as_of: date | None
    bars: list[Bar]
    fills: list[FillMarker]
    events: list[MarketEvent]
    instrument: InstrumentCard


class ETFQuote(BaseModel):
    code: Code
    name: str
    asset_class: str
    as_of: date | None
    close: float | None
    mom_240_ex20_pct: float | None
    mom_rank: int | None
    vol_20d_ann_pct: float | None


class QuarantinedETF(BaseModel):
    code: Code
    name: str
    status: str
    reason: str


class RadarTiming(BaseModel):
    code: str
    as_of: date
    close: float
    ma200: float
    state: TrendState


class Radar(BaseModel):
    as_of: date
    items: list[ETFQuote]
    quarantined: list[QuarantinedETF]
    timing: list[RadarTiming]
    x3_state: Literal["RISK_ON", "BOND_HEDGE"] | None
    x3_as_of: date | None
    x3_slots: list[Code]


class CorrelationPair(BaseModel):
    code_a: Code
    name_a: str
    code_b: Code
    name_b: str
    corr: float
    common_days: int


class Correlation(BaseModel):
    as_of: date
    lookback: int
    codes: list[Code]
    names: list[str]
    matrix: list[list[float | None]]
    high_pairs: list[CorrelationPair]


class WalletPositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Code
    shares: int = Field(gt=0, le=100_000_000, strict=True)
    avg_cost: float = Field(ge=0, le=10_000_000, allow_inf_nan=False)


class WalletSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: date
    cash_rmb: float = Field(ge=0, le=10_000_000_000, allow_inf_nan=False)
    positions: list[WalletPositionInput] = Field(max_length=1000)

    @model_validator(mode="after")
    def unique_positions(self):
        if len({p.code for p in self.positions}) != len(self.positions):
            raise ValueError("A holding code may appear only once")
        return self


class WalletPosition(WalletPositionInput):
    name: str
    close: float | None
    price_as_of: date | None
    market_value_rmb: float | None
    weight_pct: float | None
    unrealized_pnl_rmb: float | None


class WalletDiff(BaseModel):
    code: Code
    name: str
    actual_shares: int
    benchmark_shares: float
    share_gap: float
    actual_weight_pct: float | None
    benchmark_weight_pct: float | None
    kind: Literal["extra", "missing", "quantity", "matched"]


class WalletPoint(BaseModel):
    day: date
    wallet_nav_rmb: float | None
    shadow_nav_rmb: float | None
    wallet_return_pct: float | None
    shadow_return_pct: float | None


class WalletSummary(BaseModel):
    snapshot_day: date | None
    as_of: date | None
    benchmark_account: Benchmark
    capital_rmb: Capital
    cash_rmb: float | None
    nav_rmb: float | None
    benchmark_nav_rmb: float | None
    tracked_interval_count: int
    tracking_error_pp: float | None
    excess_return_pp: float | None
    cash_gap_pp: float | None
    positions: list[WalletPosition]
    differences: list[WalletDiff]
    points: list[WalletPoint]


class WalletMutation(BaseModel):
    day: date
    status: Literal["saved", "deleted"]


class ResearchResult(BaseModel):
    key: str
    model: str | None = None
    level: int
    clock: str | None = None
    window: str
    capital: int
    first: date | None = None
    last: date | None = None
    sessions: int | None = None
    cagr_pct: float | None = None
    max_dd_pct: float | None = None
    sharpe: float | None = None
    calmar: float | None = None
    cash_mean_pct: float | None = None
    waterfall_base_pct: float | None = None
    waterfall_gain_pp: float | None = None
    waterfall_loss_pp: float | None = None
    status: str | None = None


class MainboardFactor(BaseModel):
    factor: str
    window: Literal["IS", "OOS"]
    freq: Literal["weekly", "monthly"]
    horizon: int
    weighting: str
    signals: int
    rank_ic: float | None
    icir: float | None
    q5_q1_pp: float | None
    spread_t_hac: float | None


class VpbTrial(BaseModel):
    oos_calmar: float
    bear_max_dd_pct: float
    oos_sharpe: float
    oos_novol_sharpe: float
    oos_sharpe_delta: float
    oos_e4m_correlation: float
    conditions: dict[str, bool]
    passed: bool


class VpbVerdict(BaseModel):
    status: str
    live_weight: float | None
    cases: dict[str, VpbTrial]


class EngineCorrelation(BaseModel):
    window: str
    level: int
    engine: str
    other_engine: str
    period: str
    sessions: int
    correlation: float | None


class Multialpha(BaseModel):
    phase9a: list[ResearchResult]
    phase9b: list[ResearchResult]
    factor_court: list[MainboardFactor]
    vpb_verdict: VpbVerdict
    vpb_results: list[ResearchResult]
    blends: list[ResearchResult]
    correlations: list[EngineCorrelation]
