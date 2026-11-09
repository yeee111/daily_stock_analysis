# -*- coding: utf-8 -*-
"""Paper trading API schemas."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Literal, Optional

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
