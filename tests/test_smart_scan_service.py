# -*- coding: utf-8 -*-
"""Service tests for smart scan aggregation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

from src.services.smart_scan_service import SmartScanService


class _FakeAnalysisService:
    """Deterministic stand-in for AnalysisService.

    ``behavior`` maps stock code to one of:
    - ``None`` -> analysis returns None with an error
    - ``Exception`` -> analysis raises
    - any other value -> success with that action in the report summary
    """

    behavior: Dict[str, Any] = {}

    def __init__(self) -> None:
        self.last_error: Optional[str] = None

    def analyze_stock(self, stock_code: str, **_: Any) -> Optional[Dict[str, Any]]:
        outcome = self.behavior.get(stock_code)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            self.last_error = f"analysis failed for {stock_code}"
            return None
        return {
            "stock_code": stock_code,
            "stock_name": f"name-{stock_code}",
            "report": {"summary": {"action": outcome, "action_label": outcome}},
        }


class _FakeSignalService:
    def __init__(self, signals: List[Dict[str, Any]]) -> None:
        self.signals = signals

    def list_signals(self, *, trace_id: str, page: int, page_size: int) -> Dict[str, Any]:
        start = (page - 1) * page_size
        return {
            "items": self.signals[start:start + page_size],
            "total": len(self.signals),
            "page": page,
            "page_size": page_size,
        }


def _run(signals: List[Dict[str, Any]], behavior: Dict[str, Any], codes: List[str]) -> Dict[str, Any]:
    _FakeAnalysisService.behavior = behavior
    with patch(
        "src.services.smart_scan_service.AnalysisService",
        _FakeAnalysisService,
    ), patch(
        "src.services.smart_scan_service.DecisionSignalService",
        lambda: _FakeSignalService(signals),
    ):
        return SmartScanService().run_scan(codes, scan_id="scan-1")


def _signal(code: str, action: str, score: Optional[int], **extra: Any) -> Dict[str, Any]:
    payload = {
        "id": extra.pop("id", None),
        "stock_code": code,
        "stock_name": f"name-{code}",
        "action": action,
        "action_label": action,
        "score": score,
        "confidence": 0.8,
        "entry_low": 10.0,
        "entry_high": 11.0,
        "stop_loss": 9.0,
        "target_price": 13.0,
        "reason": "reason",
        "risk_summary": "risk",
        "catalyst_summary": "catalyst",
        "horizon": "5d",
    }
    payload.update(extra)
    return payload


def test_scan_ranks_buy_before_hold_and_records_duplicates() -> None:
    result = _run(
        signals=[
            _signal("600519", "hold", 90, id=1),
            _signal("000001", "buy", 60, id=2),
        ],
        behavior={"600519": "hold", "000001": "buy"},
        codes=["600519", "000001", "600519"],
    )

    assert result["status"] == "completed"
    assert [item["stock_code"] for item in result["ranked_list"]] == ["000001", "600519"]
    assert [item["rank"] for item in result["ranked_list"]] == [1, 2]

    assert [r["status"] for r in result["stock_results"]] == ["success", "success", "skipped"]
    assert result["stock_results"][2]["error"] == "duplicate_input"

    aggregation = result["aggregation"]
    assert aggregation["total"] == 3
    assert aggregation["succeeded"] == 2
    assert aggregation["failed"] == 0
    assert aggregation["skipped"] == 1
    assert aggregation["action_breakdown"] == {"buy": 1, "hold": 1}
    assert aggregation["avg_score"] == 75.0
    assert aggregation["median_score"] == 75.0
    assert aggregation["score_distribution"]["strong_buy_gte80"] == 1
    assert aggregation["score_distribution"]["buy_60_79"] == 1


def test_scan_falls_back_to_analysis_action_when_signal_missing() -> None:
    result = _run(
        signals=[],
        behavior={"600519": "watch"},
        codes=["600519"],
    )

    item = result["ranked_list"][0]
    assert item["action"] == "watch"
    assert item["score"] is None
    assert item["reason"] == "未生成决策信号"
    assert item["signal_id"] is None
    assert result["aggregation"]["action_breakdown"] == {"watch": 1}
    assert result["aggregation"]["avg_score"] is None


def test_scan_isolates_failures_per_stock() -> None:
    result = _run(
        signals=[],
        behavior={
            "600519": RuntimeError("llm down"),
            "000001": None,
            "300750": "buy",
        },
        codes=["600519", "000001", "300750"],
    )

    statuses = {r["stock_code"]: r["status"] for r in result["stock_results"]}
    assert statuses == {"600519": "failed", "000001": "failed", "300750": "success"}
    errors = {r["stock_code"]: r["error"] for r in result["stock_results"] if r["status"] == "failed"}
    assert "llm down" in errors["600519"]
    assert errors["000001"] == "analysis failed for 000001"
    assert result["aggregation"]["failed"] == 2
    assert result["aggregation"]["succeeded"] == 1
    assert [item["stock_code"] for item in result["ranked_list"]] == ["300750"]


def test_scan_prefers_most_actionable_signal_per_stock() -> None:
    result = _run(
        signals=[
            _signal("600519", "hold", 95, id=1),
            _signal("600519", "buy", 60, id=2),
        ],
        behavior={"600519": "hold"},
        codes=["600519"],
    )

    item = result["ranked_list"][0]
    assert item["action"] == "buy"
    assert item["score"] == 60
    assert item["signal_id"] == 2


def test_scan_pages_through_signals_beyond_page_size() -> None:
    signals = [
        _signal("000001", "hold", 10, id=1),
        _signal("000001", "hold", 20, id=2),
        _signal("000001", "hold", 30, id=3),
        _signal("000001", "buy", 88, id=4),
    ]
    _FakeAnalysisService.behavior = {"000001": "hold"}
    with patch(
        "src.services.smart_scan_service.AnalysisService",
        _FakeAnalysisService,
    ), patch(
        "src.services.smart_scan_service.DecisionSignalService",
        lambda: _FakeSignalService(signals),
    ), patch(
        "src.services.smart_scan_service._SIGNAL_PAGE_SIZE",
        2,
    ):
        result = SmartScanService().run_scan(["000001"], scan_id="scan-1")

    item = result["ranked_list"][0]
    assert item["signal_id"] == 4
    assert item["score"] == 88


def test_scan_with_blank_input_returns_completed_empty_result() -> None:
    result = _run(signals=[], behavior={}, codes=["", "   "])

    assert result["status"] == "completed"
    assert result["message"] == "无有效股票可分析"
    assert result["ranked_list"] == []
    assert result["aggregation"]["total"] == 0
    assert result["aggregation"]["skipped"] == 0


def test_scan_reports_monotonic_progress() -> None:
    progress: List[int] = []
    _FakeAnalysisService.behavior = {"600519": "buy"}
    with patch(
        "src.services.smart_scan_service.AnalysisService",
        _FakeAnalysisService,
    ), patch(
        "src.services.smart_scan_service.DecisionSignalService",
        lambda: _FakeSignalService([_signal("600519", "buy", 80)]),
    ):
        SmartScanService().run_scan(
            ["600519", "000001"],
            scan_id="scan-1",
            progress_callback=lambda pct, _msg: progress.append(pct),
        )

    assert progress[0] == 5
    assert progress[-1] == 100
    assert progress == sorted(progress)
