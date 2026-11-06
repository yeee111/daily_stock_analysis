# -*- coding: utf-8 -*-
"""Tests for the screening -> DecisionSignal bridge."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.config import Config
from src.services.decision_signal_service import DecisionSignalService
from src.services.screening_signal_extractor import (
    build_screening_signal_payload,
    persist_screening_signals,
)
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    db_path = tmp_path / "screening_signal_bridge.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_database_path


def _candidate(
    *,
    code: str = "600519",
    name: str = "贵州茅台",
    rank: int = 1,
    price: object = 100.0,
    score: object = 78.6,
    raw: dict | None = None,
    **overrides,
):
    raw_payload = {
        "code": code,
        "name": name,
        "price": price,
        "pullback_to_ma20_pct": 5.0,
        "breakout_20d_pct": 8.0,
        "atr_20_pct": 3.0,
        "price_above_ma20": True,
    }
    raw_payload.update(raw or {})
    candidate = {
        "rank": rank,
        "code": code,
        "name": name,
        "score": score,
        "screen_score": score,
        "price": price,
        "change_pct": 1.2,
        "industry": "白酒",
        "reason": "趋势确认",
        "reason_source": "factor",
        "reason_quality": "good",
        "risk_flags": ["估值偏高"],
        "llm_confidence": 0.8,
        "llm_thesis": "量价配合",
        "llm_catalysts": ["业绩预增"],
        "llm_risks": ["行业景气回落"],
        "llm_tags": ["消费"],
        "factor_scores": {"momentum": 80.0},
        "raw": raw_payload,
    }
    candidate.update(overrides)
    return candidate


def _build(candidate: dict, **kwargs):
    params = {
        "rank": candidate.get("rank", 1),
        "buy_top_n": 10,
        "horizon": "3d",
        "run_id": "run-1",
        "strategy": "momentum_quality",
        "strategy_version": "1.1",
        "ranking_mode": "llm",
        "llm_ranked": True,
        "selection_logic": "优先趋势与质量",
        "signal_date": "2026-11-03",
    }
    params.update(kwargs)
    return build_screening_signal_payload(candidate, **params)


def test_top_n_rank_becomes_buy_with_dynamic_plan() -> None:
    payload = _build(_candidate(rank=3), rank=3)

    assert payload is not None
    assert payload["action"] == "buy"
    assert payload["source_type"] == "screening"
    assert payload["market"] == "cn"
    assert payload["horizon"] == "3d"
    assert payload["score"] == 79
    assert payload["confidence"] == 0.8
    assert payload["trace_id"] == "screening-2026-11-03-600519"

    assert payload["entry_low"] == 95.24
    assert payload["entry_high"] == 100.0
    assert payload["stop_loss"] == 93.74
    assert payload["target_price"] == 104.5
    assert payload["plan_quality"] == "complete"

    metadata = payload["metadata"]
    assert metadata["screening_rank"] == 3
    assert metadata["screening_buy_top_n"] == 10
    assert metadata["screening_run_id"] == "run-1"
    assert metadata["plan_source"] == "dynamic_support_resistance"
    assert payload["data_quality_summary"]["atr_20_pct"] == 3.0


def test_rank_outside_top_n_becomes_watch() -> None:
    payload = _build(_candidate(rank=11), rank=11, buy_top_n=10)

    assert payload is not None
    assert payload["action"] == "watch"
    assert payload["invalidation"] is None
    assert payload["watch_conditions"]
    assert "95.24" in payload["watch_conditions"]


def test_prev_high_used_as_target_when_still_above_price() -> None:
    candidate = _candidate(raw={"breakout_20d_pct": -10.0})
    payload = _build(candidate)

    assert payload is not None
    assert payload["target_price"] == 111.11
    assert payload["metadata"]["plan_target_source"] == "prev_high_20d"


def test_below_ma20_support_falls_back_to_atr() -> None:
    candidate = _candidate(
        raw={"pullback_to_ma20_pct": -2.0, "price_above_ma20": False}
    )
    payload = _build(candidate)

    assert payload is not None
    assert payload["entry_low"] == 97.0
    assert payload["stop_loss"] == 95.5


def test_missing_atr_uses_mechanical_plan() -> None:
    candidate = _candidate(raw={"atr_20_pct": None})
    payload = _build(candidate)

    assert payload is not None
    assert payload["entry_low"] == 99.0
    assert payload["entry_high"] == 100.0
    assert payload["stop_loss"] == 95.0
    assert payload["target_price"] == 110.0
    assert payload["plan_quality"] == "minimal"
    assert payload["metadata"]["plan_source"] == "mechanical_fallback"


def test_missing_price_still_builds_signal_without_plan() -> None:
    candidate = _candidate(price=None, raw={"price": None})
    payload = _build(candidate)

    assert payload is not None
    assert payload["action"] == "buy"
    assert payload["entry_low"] is None
    assert payload["stop_loss"] is None
    assert payload["plan_quality"] == "unknown"
    assert payload["metadata"]["plan_source"] == "missing_price"


def test_unsupported_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="horizon"):
        _build(_candidate(), horizon="swing")


def test_missing_code_is_skipped() -> None:
    assert _build(_candidate(code="")) is None


def test_persist_writes_signals_and_dedupes_same_day(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    response = {
        "market": "cn",
        "run_id": "run-1",
        "strategy": "momentum_quality",
        "strategy_version": "1.1",
        "ranking_mode": "llm",
        "llm_ranked": True,
        "llm_selection_logic": "优先趋势与质量",
        "candidates": [
            _candidate(code="600519", rank=1, score=88.0),
            _candidate(code="000001", name="平安银行", rank=2, score=75.0),
            _candidate(code="300750", name="宁德时代", rank=3, score=61.4),
        ],
    }

    first = persist_screening_signals(response, service=service, buy_top_n=2, horizon="3d")
    assert first["candidates"] == 3
    assert first["created"] == 3
    assert first["errors"] == []

    items = service.list_signals(
        market="cn",
        source_type="screening",
        status="active",
        page=1,
        page_size=50,
    )["items"]
    by_code = {item["stock_code"]: item for item in items}
    assert set(by_code) == {"600519", "000001", "300750"}
    assert by_code["600519"]["action"] == "buy"
    assert by_code["000001"]["action"] == "buy"
    assert by_code["300750"]["action"] == "watch"
    assert all(item["horizon"] == "3d" for item in items)
    assert by_code["600519"]["entry_low"] == 95.24
    assert by_code["600519"]["stop_loss"] == 93.74
    assert by_code["600519"]["metadata"]["screening_strategy"] == "momentum_quality"

    second = persist_screening_signals(response, service=service, buy_top_n=2, horizon="3d")
    assert second["created"] == 0
    assert second["duplicate"] == 3
    assert second["errors"] == []


class _StubWriter:
    def __init__(self) -> None:
        self.calls = 0

    def create_signal_with_outcome(self, payload):
        self.calls += 1
        if payload["stock_code"] == "000001":
            raise RuntimeError("boom")
        return SimpleNamespace(disposition="created")


def test_persist_isolates_per_candidate_errors() -> None:
    writer = _StubWriter()
    response = {
        "market": "cn",
        "candidates": [
            _candidate(code="600519", rank=1),
            _candidate(code="000001", rank=2),
            _candidate(code="300750", rank=3),
        ],
    }

    summary = persist_screening_signals(response, service=writer, buy_top_n=2)

    assert writer.calls == 3
    assert summary["created"] == 2
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["code"] == "000001"
    assert "boom" in summary["errors"][0]["error"]


def _scheduler_config(**overrides):
    config = SimpleNamespace(
        screening_signal_bridge_enabled=True,
        screening_enabled=True,
        screening_signal_horizon="3d",
        screening_signal_max_results=15,
        screening_signal_buy_top_n=10,
        screening_signal_strategy="momentum_quality",
        screening_signal_market="cn",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_scheduler_bridge_screens_then_persists(monkeypatch) -> None:
    from src.services.runtime_scheduler import RuntimeSchedulerService

    calls = []

    class FakeScreeningService:
        def __init__(self, config):
            self.config = config

        def screen(self, **kwargs):
            calls.append(("screen", kwargs))
            return {"market": kwargs["market"], "candidates": []}

    def fake_persist(response, **kwargs):
        calls.append(("persist", kwargs))
        return {"candidates": 0, "created": 0, "refreshed": 0, "duplicate": 0, "skipped": 0, "errors": []}

    monkeypatch.setattr("src.services.screening_service.ScreeningService", FakeScreeningService)
    monkeypatch.setattr(
        "src.services.screening_signal_extractor.persist_screening_signals",
        fake_persist,
    )

    service = RuntimeSchedulerService(config_provider=_scheduler_config)
    service._run_screening_signal_bridge()

    assert [name for name, _ in calls] == ["screen", "persist"]
    assert calls[0][1] == {
        "strategy": "momentum_quality",
        "market": "cn",
        "max_results": 15,
    }
    assert calls[1][1]["buy_top_n"] == 10
    assert calls[1][1]["horizon"] == "3d"
    assert calls[1][1]["trigger_source"] == "schedule"


def test_scheduler_bridge_skips_when_disabled(monkeypatch) -> None:
    from src.services.runtime_scheduler import RuntimeSchedulerService

    calls = []

    class FakeScreeningService:
        def __init__(self, config):
            calls.append("screen")

    monkeypatch.setattr("src.services.screening_service.ScreeningService", FakeScreeningService)

    RuntimeSchedulerService(
        config_provider=lambda: _scheduler_config(screening_signal_bridge_enabled=False)
    )._run_screening_signal_bridge()
    RuntimeSchedulerService(
        config_provider=lambda: _scheduler_config(screening_enabled=False)
    )._run_screening_signal_bridge()

    assert calls == []
