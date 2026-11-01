# -*- coding: utf-8 -*-
"""Smart scan request/response schemas."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SmartScanRequest(BaseModel):
    stock_codes: List[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="股票代码列表，最多 50 只",
    )
    report_type: Literal["simple", "detailed", "full", "brief"] = Field(
        "detailed",
        description="分析报告类型",
    )
    max_concurrent: int = Field(
        3,
        ge=1,
        le=5,
        description="最大并发分析数",
    )


class SmartScanAccepted(BaseModel):
    scan_id: str
    trace_id: str
    status: str = "pending"
    message: str
    total_stocks: int


class SmartScanStockResult(BaseModel):
    stock_code: str
    stock_name: Optional[str] = None
    status: Literal["success", "failed", "skipped"]
    error: Optional[str] = None


class SmartScanRankedItem(BaseModel):
    rank: int
    stock_code: str
    stock_name: Optional[str] = None
    action: str
    action_label: Optional[str] = None
    score: Optional[int] = None
    confidence: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    reason: Optional[str] = None
    risk_summary: Optional[str] = None
    catalyst_summary: Optional[str] = None
    signal_id: Optional[int] = None
    horizon: Optional[str] = None


class SmartScanAggregation(BaseModel):
    total: int
    succeeded: int
    failed: int
    skipped: int = 0
    action_breakdown: Dict[str, int] = Field(default_factory=dict)
    score_distribution: Dict[str, int] = Field(default_factory=dict)
    avg_score: Optional[float] = None
    median_score: Optional[float] = None


class SmartScanResult(BaseModel):
    scan_id: str
    status: str
    progress: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    aggregation: Optional[SmartScanAggregation] = None
    ranked_list: Optional[List[SmartScanRankedItem]] = None
    stock_results: Optional[List[SmartScanStockResult]] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
