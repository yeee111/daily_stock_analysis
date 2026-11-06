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
