from datetime import date, datetime
from enum import StrEnum
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Account(StrEnum):
    DEF50 = "DEF50"
    E4M = "E4M"
    V5 = "V5"
    C3 = "C3"
    EW = "PIT300_ex688_EW_PROP"
    INDEX = "CSI300_Index"


class Role(StrEnum):
    CONSERVATIVE = "is_locked_conservative"
    FLAGSHIP = "is_locked_flagship"
    VOLCAP = "is_locked_volcap"
    OBSERVER = "exploratory_observer_only"
    EW = "benchmark_ew_prop"
    INDEX = "benchmark_index"


class Provenance(StrEnum):
    CATCHUP = "post_hoc_catchup"
    STRICT = "live_timely_verified"
    HISTORICAL = "historical_validation"


class Capital(StrEnum):
    MAIN = "450000"
    SCALE = "2000000"


MarketAsset = Literal["stock", "etf", "index"]
TrendState = Literal["strong", "weak"]
Side = Literal["buy", "sell"]
Window = Literal["W1", "IS", "OOS", "W_Bear"]
Scenario = Literal["2bp", "7bp", "T+2"]


class Error(BaseModel):
    code: str
    message: str


class DateRange(BaseModel):
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class AccountFilter(DateRange):
    account: Account | None = None
    capital_rmb: Capital | None = None


class PagedAccountFilter(AccountFilter):
    limit: int = Field(200, ge=1, le=2000)
    offset: int = Field(0, ge=0)


class AccountIdentity(BaseModel):
    account: Account
    capital_rmb: Literal[450000, 2000000]
    role_tag: Role
    risk_label: str


class NavPoint(AccountIdentity):
    day: date
    provenance_mode: Provenance
    nav_cents: int
    cash_cents: int
    receivable_cents: int
    stock_pnl_cents: int
    etf_pnl_cents: int
    stock_daily_pnl_cents: int
    etf_daily_pnl_cents: int
    return_pct: float
    drawdown_pct: float
    max_drawdown_pct: float
    cash_pct: float
    excess_ew_pp: float
    excess_csi300_pp: float
    lot_idle_mean_pct: float | None


class Holding(AccountIdentity):
    day: date
    asset: MarketAsset
    code: str
    name: str
    industry: str
    shares: int | float
    market_value_cents: int
    market_weight_pct: float


class Fill(AccountIdentity):
    day: date
    provenance_mode: Provenance
    seq: int
    asset: MarketAsset
    code: str
    side: Side
    shares: int | float
    price_rmb: float
    commission_cents: int
    stamp_cents: int
    slippage_cents: int
    cash_after_sleeve_cents: int
    signal_day: date
    reason: str
    execution_kind: str
    lot_idle_mean_pct: float | None


class CorporateAction(AccountIdentity):
    day: date
    provenance_mode: Provenance
    asset: MarketAsset
    code: str
    event_id: str
    entry: str
    effective_date: date
    shares: int | float
    amount_cents: int


class M200(BaseModel):
    state: TrendState
    weekly_as_of: date
    daily_state: TrendState
    flip: bool
    close: float
    ma200: float
    deviation_pct: float
    attack_cap_pct: float


class VolMonitor(BaseModel):
    target_etf_code: str | None
    reference_etf_code: str | None
    etf_vol20_ann_pct: float | None
    raw_attack_cap_pct: float | None
    applied_attack_cap_pct: float
    effective_attack_cap_pct: float
    distance_from_active_target_pp: float | None
    remaining_to_band_pp: float | None
    rebalance_band_pp: float
    resize_due: bool
    scaling_trigger_count: int
    last_trigger_signal_day: date | None


class SignalDates(BaseModel):
    weekly_defense: date | None = None
    month_end_etf: date | None = None
    month_start_core: date | None = None


class RiskSnapshot(BaseModel):
    latest_session: date | None
    latest_provenance_mode: Provenance | None
    m200: M200 | None
    vol_monitor: VolMonitor | None
    next_signals: SignalDates


class Overview(RiskSnapshot):
    strict_live_sessions: int
    catchup_sessions: int
    total_fills: int
    accounts: list[NavPoint]


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    offset: int
    limit: int
    has_more: bool


class Ticket(AccountIdentity):
    as_of: date | None
    state: Literal["NO_ACTION_DUE", "SIGNAL_DUE_NO_PROXY_FILL", "PROXY_ORDER"]
    provenance_mode: Provenance | None
    signal_day: date | None
    opening_day: date | None
    next_signal_day: date | None
    m200_state: TrendState | None
    m200_deviation_pct: float | None
    code: str | None
    name: str | None
    asset: MarketAsset | None
    side: Side | None
    reason: str | None
    proxy_price_rmb: float | None
    shares: int | None
    target_weight_pct: float | None
    estimated_cash_rmb: float | None


class ResearchIdentity(BaseModel):
    source: str
    key: str
    pool: str
    selection_status: str | None = None
    account: Account | None = None
    risk_label: str | None = None


class Portfolio(ResearchIdentity):
    window: Window
    scenario: Scenario
    capital_rmb: int
    first: date
    last: date
    sessions: int
    net_return_pct: float
    cagr_pct: float
    max_dd_pct: float
    sharpe: float | None = None
    calmar: float | None = None
    ann_oneway_turnover_pct_true: float | None = None
    cash_mean_pct: float | None = None
    defense_net_rmb: float | None = None
    trend_net_rmb: float | None = None
    mean_attack_target_pct: float | None = None
    scaling_triggers: int | None = None


class YearReturn(ResearchIdentity):
    year: int
    first: date
    last: date
    sessions: int
    opening_nav_rmb: float
    closing_nav_rmb: float
    year_return_pct: float
    excess_vs_index_pp: float | None = None
    excess_vs_ew_pp: float | None = None


class Drawdown(ResearchIdentity):
    scope: str
    rank: int
    peak_date: date | None = None
    peak_origin: str
    trough_date: date
    recovery_date: date | None = None
    recovered: bool
    drawdown_pct: float
    peak_to_trough_sessions: int
    recovery_sessions: int | None = None
    underwater_sessions: int


class FactorCourt(BaseModel):
    pool: str
    freq: str
    window: Window
    horizon: str
    factor: str
    weeks: int
    ic: float | None = None
    icir: float | None = None
    ic_t_hac: float | None = None
    positive_ic_pct: float | None = None
    q1_pct: float | None = None
    q2_pct: float | None = None
    q3_pct: float | None = None
    q4_pct: float | None = None
    q5_pct: float | None = None
    q5_q1_pp: float | None = None
    monotone_increasing: bool | None = None
    spread_t_hac: float | None = None
    rank_autocorr: float | None = None
    top5_turnover_pct: float | None = None
    top5_excess_ew_pp: float | None = None
    top10_excess_ew_pp: float | None = None


class Placebo(BaseModel):
    source: str
    key: str
    window: Window
    kind: str
    metric: Literal["sharpe","calmar","max_dd_pct"] | None = None
    metric_unit: Literal["ratio","pct"] | None = None
    method: str | None = None
    timing: str | None = None
    actual: float | None = None
    inverse: float | None = None
    permutation_median: float | None = None
    p_upper: float | None = None
    max_dd_pct: float | None = None
    sharpe: float | None = None
    calmar: float | None = None
    draw: int | None = None
    seed: int | None = None


class EtfCoverage(BaseModel):
    first: date
    last: date
    availability: str
    last_signal_observation: date
    rows: int


class StockCoverage(BaseModel):
    first: date
    last: date
    membership_rows: int
    months: int
    sessions: int
    validated_stocks: int


class ResearchCoverage(BaseModel):
    stock_issues: int
    etf_issues: int
    stock_coverage: StockCoverage
    etf_coverage: dict[str, EtfCoverage]
    production_replay_status: str
    phase6a_research_replay: bool


class Health(BaseModel):
    status: Literal["ok"]
    api_version: str
    python_version: str
    sqlite_version: str
    sqlite_readonly: bool
    api_read_only: bool = False
    dependency_versions: dict[str, str]
    latest_session: date | None
    frozen_sha256: dict[str, str]


class IntegritySummary(BaseModel):
    ok: bool
    checked_files: int
    missing_files: list[str]
    changed_files: list[str]
    frozen_sha256: dict[str, str]


class CollectionStatus(BaseModel):
    day: date
    state: str
    reason: str | None = None
    provenance_mode: Provenance | None = None
    factor_staging: str | None = None
    market_http_requests: int
    updated_at: datetime
    source_profile: Literal["v5a", "v7a"]


class Action(StrEnum):
    DAILY = "DailyAll"
    COLLECT = "Collect"
    INGEST = "Ingest"
    VERIFY = "VerifyOffline"


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    day: date | None = None
    allow_market_http: bool = False

    @model_validator(mode="after")
    def valid_action(self):
        online = self.action in (Action.DAILY, Action.COLLECT)
        if online and not self.allow_market_http:
            raise ValueError("collection requires allow_market_http=true")
        if not online and (self.day is not None or self.allow_market_http):
            raise ValueError("day and allow_market_http apply only to collection actions")
        return self


class Job(BaseModel):
    id: UUID
    request: JobRequest
    state: Literal["queued", "running", "succeeded", "failed", "interrupted"]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None


class JobLog(BaseModel):
    id: UUID
    text: str
    truncated: bool
