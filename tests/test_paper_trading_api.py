# -*- coding: utf-8 -*-
"""API tests for the paper trading endpoint."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints import paper_trading as module


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(module.router, prefix="/paper-trading")
    return TestClient(app)


def _payload(dry_run: bool = True) -> Dict[str, Any]:
    return {
        "status": "planned" if dry_run else "executed",
        "dry_run": dry_run,
        "message": "ok",
        "market": "cn",
        "latest_data_date": "2026-10-30",
        "signal_counts": {"active": 2, "batch": 1},
        "account": {"id": None, "name": "纸面交易", "created": True, "initial_cash": 100000.0},
        "batch": {
            "signal_dates": ["2026-10-29"],
            "fill_date": "2026-10-30",
            "reference_date": "2026-10-29",
        },
        "portfolio": {
            "cash": 100000.0,
            "market_value": 0.0,
            "equity": 100000.0,
            "per_name": 20000.0,
            "held_names": 0,
        },
        "items": [
            {
                "signal_id": 1,
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "action": "buy",
                "score": 80,
                "side": "buy",
                "quantity": 200,
                "price": 10.0,
                "reference_price": 9.9,
                "fee": 5.2,
                "tax": 0.0,
                "status": "planned",
                "reason": "建仓",
            }
        ],
        "orders": [
            {
                "stock_code": "600519",
                "side": "buy",
                "quantity": 200,
                "price": 10.0,
                "fee": 5.2,
                "tax": 0.0,
                "signal_id": 1,
                "note": "paper trading: signal #1",
                "trade_uid": "paper-2026-10-30-600519-buy",
                "trade_id": None,
                "status": "planned",
                "reason": None,
            }
        ],
        "warnings": [],
    }


def test_run_defaults_to_dry_run(client: TestClient) -> None:
    with patch.object(module, "PaperTradingService") as service_cls:
        service_cls.return_value.run.return_value = _payload()
        response = client.post("/paper-trading/run", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "planned"
    assert body["dry_run"] is True
    assert body["orders"][0]["trade_uid"] == "paper-2026-10-30-600519-buy"
    kwargs = service_cls.return_value.run.call_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["force"] is False
    assert kwargs["market"] == "cn"


def test_run_forwards_parameters(client: TestClient) -> None:
    with patch.object(module, "PaperTradingService") as service_cls:
        service_cls.return_value.run.return_value = _payload(dry_run=False)
        response = client.post(
            "/paper-trading/run",
            json={
                "account_id": 3,
                "account_name": "模型A",
                "horizons": ["3d"],
                "min_score": 65,
                "max_names": 3,
                "max_weight": 0.25,
                "signal_date": "2026-10-31",
                "dry_run": False,
                "force": True,
                "initial_cash": 50000,
            },
        )

    assert response.status_code == 200
    kwargs = service_cls.return_value.run.call_args.kwargs
    assert kwargs["account_id"] == 3
    assert kwargs["account_name"] == "模型A"
    assert kwargs["horizons"] == ["3d"]
    assert kwargs["min_score"] == 65
    assert kwargs["max_names"] == 3
    assert kwargs["max_weight"] == 0.25
    assert kwargs["signal_date"].isoformat() == "2026-10-31"
    assert kwargs["dry_run"] is False
    assert kwargs["force"] is True
    assert kwargs["initial_cash"] == 50000


def test_validation_error_maps_to_400(client: TestClient) -> None:
    with patch.object(module, "PaperTradingService") as service_cls:
        service_cls.return_value.run.side_effect = ValueError("paper trading currently supports A-share market 'cn' only")
        response = client.post("/paper-trading/run", json={"market": "cn"})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "validation_error"


def test_invalid_weight_is_rejected(client: TestClient) -> None:
    response = client.post("/paper-trading/run", json={"max_weight": 1.5})
    assert response.status_code == 422


def test_run_response_carries_position_health(client: TestClient) -> None:
    payload = _payload()
    payload["position_health"] = {
        "as_of": "2026-10-30",
        "held_names": 2,
        "covered_names": 1,
        "no_stop_positions": [
            {"code": "600760", "quantity": 200, "last_price": 50.0, "signal_id": 8, "target_price": 53.0}
        ],
        "stop_distances": [
            {"code": "600519", "last_price": 10.0, "stop_loss": 9.5, "distance_pct": -5.0}
        ],
        "distance_distribution": {"between_3_5pct": 1},
    }
    with patch.object(module, "PaperTradingService") as service_cls:
        service_cls.return_value.run.return_value = payload
        response = client.post("/paper-trading/run", json={})

    assert response.status_code == 200
    health = response.json()["position_health"]
    assert health["held_names"] == 2
    assert health["no_stop_positions"][0]["code"] == "600760"
    assert health["stop_distances"][0]["distance_pct"] == -5.0


def _comparison_payload() -> Dict[str, Any]:
    return {
        "as_of": "2026-11-14",
        "cost_method": "fifo",
        "lookback_days": 180,
        "thresholds": {"min_days": 20.0, "min_trades": 8.0},
        "window": {
            "start": "2026-10-20",
            "end": "2026-11-14",
            "days": 18,
            "reference_account_id": 1,
        },
        "branches": [
            {
                "account_id": 1,
                "account_name": "live",
                "role": "live",
                "state": "live",
                "is_reference": True,
                "series": [],
                "metrics": {"days": 18, "return_pct": 1.0, "trades": 0},
                "gates": [],
                "passed": None,
                "score": None,
            }
        ],
        "warnings": [],
    }


def test_compare_branches_is_read_only_and_forwards_filters(client: TestClient) -> None:
    with patch.object(module, "PaperBranchCompareService") as service_cls:
        service_cls.return_value.compare.return_value = _comparison_payload()
        response = client.get(
            "/paper-trading/branches/comparison",
            params={
                "as_of": "2026-11-13",
                "lookback_days": 60,
                "cost_method": "avg",
                "account_ids": "1, 2",
            },
        )

    assert response.status_code == 200
    assert response.json()["window"]["reference_account_id"] == 1
    kwargs = service_cls.return_value.compare.call_args.kwargs
    assert kwargs["as_of"].isoformat() == "2026-11-13"
    assert kwargs["lookback_days"] == 60
    assert kwargs["cost_method"] == "avg"
    assert kwargs["account_ids"] == [1, 2]


def test_compare_branches_rejects_bad_account_ids(client: TestClient) -> None:
    with patch.object(module, "PaperBranchCompareService") as service_cls:
        response = client.get(
            "/paper-trading/branches/comparison",
            params={"account_ids": "1,abc"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "validation_error"
    service_cls.assert_not_called()


def test_compare_branches_rejects_empty_account_ids(client: TestClient) -> None:
    response = client.get(
        "/paper-trading/branches/comparison",
        params={"account_ids": " , "},
    )
    assert response.status_code == 400


def _multi_payload() -> Dict[str, Any]:
    return {
        "status": "multi_planned",
        "dry_run": True,
        "message": "多账户干跑：1 个账户",
        "market": "cn",
        "batch_key": "cn-2026-11-12-abc123",
        "batch": {
            "fill_date": "2026-11-12",
            "reference_date": "2026-11-11",
            "signal_dates": ["2026-11-11"],
            "latest_data_date": "2026-11-12",
        },
        "accounts": [
            {
                "account_id": 1,
                "account_name": "纸面交易",
                "label": "纸面交易",
                "status": "planned",
                "message": "干跑",
                "orders": 1,
                "profile_id": 1,
                "profile_name": "default",
                "profile_version": 1,
                "auto_run": True,
                "warnings": [],
            }
        ],
        "warnings": [],
    }


def test_all_accounts_forwards_force_without_conflict(client: TestClient) -> None:
    with patch.object(module, "PaperTradingService") as service_cls:
        service_cls.return_value.run_all_accounts.return_value = _multi_payload()
        response = client.post("/paper-trading/run", json={"all_accounts": True, "force": True})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "multi_planned"
    assert body["accounts"][0]["account_id"] == 1
    assert body["batch"]["signal_dates"] == ["2026-11-11"]
    kwargs = service_cls.return_value.run_all_accounts.call_args.kwargs
    assert kwargs["force"] is True
    assert kwargs["dry_run"] is True
    service_cls.return_value.run.assert_not_called()


def test_all_accounts_still_rejects_strategy_overrides(client: TestClient) -> None:
    with patch.object(module, "PaperTradingService") as service_cls:
        response = client.post("/paper-trading/run", json={"all_accounts": True, "min_score": 70})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "validation_error"
    assert "min_score" in response.text
    service_cls.return_value.run_all_accounts.assert_not_called()
