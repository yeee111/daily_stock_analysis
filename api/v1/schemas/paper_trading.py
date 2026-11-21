# -*- coding: utf-8 -*-
"""Paper trading API schemas."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class PaperTradingRunRequest(BaseModel):
    account_id: Optional[int] = Field(None, gt=0, description="指定组合账户；不传则按账户名解析")
    account_name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        description="纸面账户名；不存在时会自动创建并注入初始资金（非 dry_run）",
    )
    market: Literal["cn"] = Field("cn", description="当前仅支持 A 股市场")
    horizons: Optional[List[Literal["intraday", "1d", "3d", "5d", "10d", "swing", "long"]]] = Field(
        None,
        description="参与交易的信号周期；默认 3d/5d",
    )
    min_score: Optional[int] = Field(None, ge=0, le=100, description="买入信号最低评分；默认 60")
    max_names: Optional[int] = Field(None, ge=1, le=50, description="最多持有只数；默认 5")
    max_weight: Optional[float] = Field(None, gt=0, le=1, description="单票最大权重；默认 0.2")
    signal_date: Optional[date] = Field(None, description="只处理指定创建日的信号批次（补跑）")
    dry_run: bool = Field(True, description="默认只生成计划，不写入成交")
    force: bool = Field(False, description="同一成交日已有流水时仍继续（trade_uid 仍幂等）")
    initial_cash: Optional[float] = Field(None, gt=0, description="自动创建纸面账户时的初始资金")
    all_accounts: bool = Field(
        False,
        description=(
            "true 时跑所有 auto_run 分支账户（信号只取一次，每账户按自己的策略档成账），"
            "响应新增 accounts 列表；默认 false，保持今天单账户的入参与返回语义"
        ),
    )


class PaperTradingPlanItem(BaseModel):
    signal_id: Optional[int] = None
    stock_code: str
    stock_name: Optional[str] = None
    action: str
    score: Optional[int] = None
    side: str = ""
    quantity: int = 0
    price: Optional[float] = None
    reference_price: Optional[float] = None
    fee: float = 0.0
    tax: float = 0.0
    status: str
    reason: Optional[str] = None


class PaperTradingOrder(BaseModel):
    stock_code: str
    side: str
    quantity: int
    price: Optional[float] = None
    fee: float = 0.0
    tax: float = 0.0
    signal_id: Optional[int] = None
    note: Optional[str] = None
    trade_uid: Optional[str] = None
    trade_id: Optional[int] = None
    status: str
    reason: Optional[str] = None


class PaperTradingAccount(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    created: bool = False
    initial_cash: Optional[float] = None


class PaperTradingBatch(BaseModel):
    signal_dates: List[str] = Field(default_factory=list)
    fill_date: str
    reference_date: str


class PaperTradingAccountRun(BaseModel):
    """One account's outcome inside an ``all_accounts=true`` run.

    Only returned in multi-account mode; a single-account run keeps leaving it
    empty, so existing clients see exactly the fields they see today.
    """

    account_id: Optional[int] = None
    account_name: Optional[str] = None
    label: Optional[str] = None
    status: str
    message: Optional[str] = None
    orders: int = Field(0, description="本账户本次生成的成交笔数")
    batch_key: Optional[str] = Field(None, description="本次共用的信号批次键")
    profile_id: Optional[int] = None
    profile_name: Optional[str] = None
    profile_version: Optional[int] = None
    auto_run: Optional[bool] = None
    warnings: List[str] = Field(default_factory=list)


class PaperTradingPortfolio(BaseModel):
    cash: float = 0.0
    market_value: float = 0.0
    equity: float = 0.0
    per_name: float = 0.0
    held_names: int = 0
    cash_after_plan: float = 0.0


class PaperTradingNoStopPosition(BaseModel):
    code: str
    quantity: int = 0
    last_price: Optional[float] = None
    signal_id: Optional[int] = None
    target_price: Optional[float] = None


class PaperTradingStopDistance(BaseModel):
    code: str
    last_price: Optional[float] = None
    stop_loss: float
    distance_pct: Optional[float] = Field(
        None,
        description="止损价相对现价的百分比距离（负值=止损在现价下方）；缺估值价时为 null",
    )


class PaperTradingPositionHealth(BaseModel):
    as_of: str
    held_names: int = 0
    covered_names: int = 0
    no_stop_positions: List[PaperTradingNoStopPosition] = Field(
        default_factory=list,
        description="建仓信号没有 stop_loss 的持仓（无止损保护）",
    )
    stop_distances: List[PaperTradingStopDistance] = Field(default_factory=list)
    distance_distribution: Dict[str, int] = Field(
        default_factory=dict,
        description="按 |距离| 分桶：within_3pct / between_3_5pct / between_5_8pct / over_8pct / unknown_price",
    )


class PaperTradingRunResponse(BaseModel):
    status: str
    dry_run: bool = True
    message: str
    market: str = "cn"
    latest_data_date: Optional[str] = None
    signal_counts: Dict[str, int] = Field(default_factory=dict)
    account: Optional[PaperTradingAccount] = None
    batch: Optional[PaperTradingBatch] = None
    portfolio: Optional[PaperTradingPortfolio] = None
    items: List[PaperTradingPlanItem] = Field(default_factory=list)
    orders: List[PaperTradingOrder] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    corporate_actions: List[str] = Field(
        default_factory=list,
        description="本次运行自动登记的分红/送转事件（人类可读）",
    )
    position_health: Optional[PaperTradingPositionHealth] = Field(
        None,
        description="持仓止损体检：无止损保护仓位清单与止损距离分布（每次运行自动附带）",
    )
    batch_key: Optional[str] = Field(
        None,
        description="本次信号批次键；仅 all_accounts=true 时返回，单账户运行保持为 null",
    )
    accounts: List[PaperTradingAccountRun] = Field(
        default_factory=list,
        description="多账户运行的逐账户结果；仅 all_accounts=true 时非空，单账户运行为空列表",
    )


class PaperTradingRunAudit(BaseModel):
    """One ``paper_account_runs`` row, decoded from its JSON columns.

    Read-only view of what a run actually consumed: the signal ids it read, the
    exit levels it planned and the policy profile in force. Dry runs write no
    row at all, so only executed batches show up here.
    """

    id: int
    account_id: Optional[int] = None
    batch_key: str
    profile_id: Optional[int] = None
    status: str
    error: Optional[str] = None
    created_at: Optional[str] = None
    dry_run: Optional[bool] = None
    message: Optional[str] = None
    fill_date: Optional[str] = None
    reference_date: Optional[str] = None
    latest_data_date: Optional[str] = None
    signal_ids: List[int] = Field(default_factory=list)
    due_signal_ids: List[int] = Field(default_factory=list)
    horizons: List[str] = Field(default_factory=list)
    exit_levels: Dict[str, Any] = Field(default_factory=dict)
    policy: Dict[str, Any] = Field(default_factory=dict)
    orders: int = Field(0, description="该批次写入的成交笔数（orders_json 长度）")


class PaperTradingRunsResponse(BaseModel):
    account_id: int
    runs: List[PaperTradingRunAudit] = Field(default_factory=list)


class PaperBranchSeriesPoint(BaseModel):
    date: str
    nav: float = Field(..., description="公共窗口首日归一为 100 的时间加权净值")


class PaperBranchMetrics(BaseModel):
    days: int = Field(0, description="公共窗口内的共同交易日数")
    return_pct: Optional[float] = Field(None, description="窗口区间收益率（%）")
    max_drawdown_pct: Optional[float] = Field(None, description="窗口内最大回撤（%）")
    trades: int = Field(0, description="窗口内成交笔数，买入/卖出各计 1 笔")
    excess_pct: Optional[float] = Field(None, description="相对 live 参照的区间超额（%）")
    excess: Optional[float] = Field(None, description="超额的小数形式，门槛判定用")
    rolling_win: Optional[float] = Field(None, description="最近 rolling_window 个交易日日收益跑赢参照的占比")
    tracking_dev: Optional[float] = Field(None, description="相对参照的日频跟踪偏离（标准差）")
    top_days: int = Field(0, description="在同类候选分支中连续排榜首的天数")
    reference_max_drawdown_pct: Optional[float] = Field(None, description="参照账户同窗口最大回撤（%）")


class PaperBranchGateItem(BaseModel):
    name: str
    value: Optional[float] = None
    threshold: str
    ok: Optional[bool] = Field(None, description="na 门槛为 null")
    status: Literal["pass", "fail", "na"]
    hint: str


class PaperBranchComparisonItem(BaseModel):
    account_id: int
    account_name: Optional[str] = None
    label: Optional[str] = None
    role: Optional[str] = Field(None, description="存储角色：live / control")
    state: Optional[str] = Field(None, description="派生状态：live / shadow / retired")
    parent_account_id: Optional[int] = None
    fork_date: Optional[str] = None
    profile_id: Optional[int] = None
    profile_name: Optional[str] = None
    profile_version: Optional[int] = None
    auto_run: Optional[bool] = None
    is_reference: bool = False
    series: List[PaperBranchSeriesPoint] = Field(default_factory=list)
    metrics: PaperBranchMetrics
    gates: List[PaperBranchGateItem] = Field(
        default_factory=list,
        description="候选分支的 7 条转正门槛；参照账户为空",
    )
    passed: Optional[bool] = Field(None, description="na 门槛不参与判定；参照账户为 null")
    score: Optional[float] = Field(None, description="0~100 综合分，仅候选分支有值")


class PaperBranchComparisonWindow(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    days: int = 0
    reference_account_id: Optional[int] = None


class PaperBranchComparisonResponse(BaseModel):
    as_of: str
    cost_method: str = "fifo"
    lookback_days: int = 180
    thresholds: Dict[str, float] = Field(default_factory=dict)
    window: PaperBranchComparisonWindow
    branches: List[PaperBranchComparisonItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
