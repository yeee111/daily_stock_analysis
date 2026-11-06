# -*- coding: utf-8 -*-
"""Tests for the portfolio performance report service and endpoint."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
from api.app import create_app
from src.config import Config, get_config
from src.services.portfolio_performance_service import PortfolioPerformanceService
from src.services.portfolio_risk_service import PortfolioRiskService
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class PortfolioPerformanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)
        self.env_path = data_dir / ".env"
        self.db_path = data_dir / "portfolio_performance_test.db"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    "PORTFOLIO_RISK_LOOKBACK_DAYS=60",
                    "PORTFOLIO_PERFORMANCE_BENCHMARK_SYMBOL=000300",
                    "PORTFOLIO_MARKET_DATA_SYNC_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()

        self.db = DatabaseManager.get_instance()
        self.service = PortfolioService()
        self.performance_service = PortfolioPerformanceService(portfolio_service=self.service)
        self.client = TestClient(create_app(static_dir=data_dir / "empty-static"))

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _save_close(self, symbol: str, on_date: date, close: float) -> None:
        df = pd.DataFrame(
            [
                {
                    "date": on_date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1.0,
                    "amount": close,
                    "pct_chg": 0.0,
                }
            ]
        )
        self.db.save_daily_data(df, code=symbol, data_source="performance-test")

    def _account(self) -> int:
        account = self.service.create_account(
            name="纸面交易", broker="paper", market="cn", base_currency="CNY"
        )
        return int(account["id"])

    def _trade(self, account_id: int, symbol: str, on_date: date, side: str, quantity: float, price: float) -> None:
        self.service.record_trade(
            account_id=account_id,
            symbol=symbol,
            trade_date=on_date,
            side=side,
            quantity=quantity,
            price=price,
            market="cn",
            currency="CNY",
            trade_uid=f"{on_date.isoformat()}-{symbol}-{side}",
        )
        self._save_close(symbol, on_date, price)

    def test_closed_round_trip_stats(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        self._trade(account_id, "600519", date(2026, 1, 15), "sell", 1000, 12.0)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 15), include_realtime=False
        )

        closed = report["closed_trades"]
        self.assertEqual(closed["count"], 1)
        self.assertEqual(closed["wins"], 1)
        self.assertEqual(closed["losses"], 0)
        self.assertEqual(closed["win_rate_pct"], 100.0)
        self.assertIsNone(closed["profit_factor"])
        self.assertEqual(closed["avg_holding_days"], 10.0)
        self.assertEqual(closed["max_holding_days"], 10)
        trade = closed["items"][0]
        self.assertEqual(trade["open_date"], "2026-01-05")
        self.assertEqual(trade["close_date"], "2026-01-15")
        self.assertAlmostEqual(trade["cost_basis"], 10_000.0, places=2)
        self.assertAlmostEqual(trade["proceeds"], 12_000.0, places=2)
        self.assertAlmostEqual(trade["pnl"], 2_000.0, places=2)
        self.assertAlmostEqual(trade["return_pct"], 20.0, places=3)
        self.assertAlmostEqual(report["summary"]["realized_pnl"], trade["pnl"], places=2)
        self.assertEqual(report["open_positions"]["count"], 0)

    def test_profit_factor_mixes_wins_and_losses(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=200_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        self._trade(account_id, "600000", date(2026, 1, 5), "buy", 1000, 10.0)
        self._trade(account_id, "600519", date(2026, 1, 12), "sell", 1000, 12.0)
        self._trade(account_id, "600000", date(2026, 1, 12), "sell", 1000, 9.0)
        self._save_close("600000", date(2026, 1, 12), 9.0)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 12), include_realtime=False
        )

        closed = report["closed_trades"]
        self.assertEqual(closed["count"], 2)
        self.assertEqual(closed["wins"], 1)
        self.assertEqual(closed["losses"], 1)
        self.assertEqual(closed["win_rate_pct"], 50.0)
        self.assertAlmostEqual(closed["profit_factor"], 2.0, places=3)
        self.assertEqual(closed["best"]["stock_code"], "600519")
        self.assertEqual(closed["worst"]["stock_code"], "600000")
        self.assertAlmostEqual(closed["avg_loss_pct"], -10.0, places=3)

    def test_partial_sell_keeps_remaining_lot_open(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        self._trade(account_id, "600519", date(2026, 1, 10), "sell", 400, 11.0)
        self._save_close("600519", date(2026, 1, 15), 11.5)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 15), include_realtime=False
        )

        self.assertEqual(report["closed_trades"]["count"], 1)
        self.assertEqual(report["closed_trades"]["items"][0]["quantity"], 400.0)
        open_positions = report["open_positions"]
        self.assertEqual(open_positions["count"], 1)
        position = open_positions["items"][0]
        self.assertEqual(position["open_date"], "2026-01-05")
        self.assertEqual(position["holding_days"], 10)
        self.assertEqual(position["quantity"], 600.0)
        self.assertGreater(position["unrealized_pnl"], 0.0)
        self.assertEqual(position["weight_pct"], 100.0)

    def test_summary_uses_net_deposits_and_drawdown(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 2), direction="in", amount=20_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        self._save_close("600519", date(2026, 1, 6), 9.0)
        self._save_close("600519", date(2026, 1, 7), 11.0)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 7), include_realtime=False
        )

        summary = report["summary"]
        self.assertEqual(summary["net_deposits"], 120_000.0)
        self.assertEqual(summary["first_activity_date"], "2026-01-01")
        self.assertEqual(summary["days_active"], 7)
        self.assertEqual(summary["trade_count"], 1)
        self.assertAlmostEqual(
            summary["total_return_pct"],
            summary["total_pnl"] / 120_000.0 * 100.0,
            places=4,
        )
        self.assertGreater(summary["max_drawdown_pct"], 0.0)
        self.assertEqual(report["equity_curve"]["max_drawdown_pct"], summary["max_drawdown_pct"])
        curve_points = report["equity_curve"]["points"]
        self.assertAlmostEqual(curve_points[0]["return_pct"], 0.0, places=4)
        self.assertAlmostEqual(curve_points[-1]["return_pct"], summary["total_return_pct"], places=4)
        self.assertTrue(any("胜率" in note or "已平仓" in note for note in report["notes"]))

    def test_time_weighted_return_ignores_late_deposit(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 1), "buy", 1000, 10.0)
        self._save_close("600519", date(2026, 1, 2), 11.0)
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 3), direction="in", amount=100_000.0
        )
        self._save_close("600519", date(2026, 1, 4), 12.0)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 4), include_realtime=False
        )

        summary = report["summary"]
        self.assertEqual(summary["net_deposits"], 200_000.0)
        # +1% on the first 100k, then the fresh 100k earns +1000/201000 ≈ +0.4975%.
        self.assertAlmostEqual(summary["total_return_pct"], 1.5025, places=3)
        curve_points = report["equity_curve"]["points"]
        self.assertAlmostEqual(curve_points[-1]["return_pct"], summary["total_return_pct"], places=4)
        simple_return = summary["total_pnl"] / summary["net_deposits"] * 100.0
        self.assertGreater(summary["total_return_pct"], simple_return)

    def test_benchmark_autosyncs_missing_index_bars(self) -> None:
        from dataclasses import replace

        from src.services.market_data_sync_service import (
            MarketDataSyncService,
            index_secid,
        )

        self.assertEqual(index_secid("000300"), "1.000300")
        self.assertEqual(index_secid("399001"), "0.399001")
        self.assertEqual(index_secid("sh000300"), "1.000300")

        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        self._save_close("600519", date(2026, 1, 6), 10.2)
        self._save_close("600519", date(2026, 1, 7), 10.5)

        def fake_fetch(*, secid: str, start: date, end: date):
            self.assertEqual(secid, "1.000300")
            return [
                {"date": date(2026, 1, 5), "open": 4000.0, "high": 4010.0, "low": 3990.0,
                 "close": 4000.0, "volume": 1.0, "amount": 4000.0, "pct_chg": 0.0},
                {"date": date(2026, 1, 6), "open": 4000.0, "high": 4060.0, "low": 4000.0,
                 "close": 4040.0, "volume": 1.0, "amount": 4040.0, "pct_chg": 1.0},
                {"date": date(2026, 1, 7), "open": 4040.0, "high": 4100.0, "low": 4040.0,
                 "close": 4100.0, "volume": 1.0, "amount": 4100.0, "pct_chg": 1.4851},
            ]

        service = PortfolioPerformanceService(
            portfolio_service=self.service,
            config=replace(get_config(), portfolio_market_data_sync_enabled=True),
            market_data_sync=MarketDataSyncService(db=self.db, fetch=fake_fetch),
        )
        report = service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 7), include_realtime=False
        )

        self.assertTrue(report["benchmark"]["available"])
        self.assertAlmostEqual(report["benchmark"]["return_pct"], 2.5, places=4)

        import sqlalchemy as sa

        with self.db.get_session() as session:
            stored = session.execute(
                sa.text(
                    "SELECT DISTINCT code FROM stock_daily "
                    "WHERE code IN ('000300', 'sh000300')"
                )
            ).scalars().all()
        # Index bars must not occupy the bare-code stock bucket (000001 = 平安银行).
        self.assertEqual(sorted(stored), ["sh000300"])

    def test_benchmark_uses_local_index_bars(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        for day, close in ((date(2026, 1, 5), 4000.0), (date(2026, 1, 6), 4040.0), (date(2026, 1, 7), 4100.0)):
            self._save_close("000300", day, close)
        self._save_close("600519", date(2026, 1, 6), 10.2)
        self._save_close("600519", date(2026, 1, 7), 10.5)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 7), include_realtime=False
        )

        benchmark = report["benchmark"]
        self.assertTrue(benchmark["available"])
        self.assertEqual(benchmark["symbol"], "000300")
        self.assertAlmostEqual(benchmark["return_pct"], 2.5, places=4)
        self.assertAlmostEqual(
            benchmark["excess_return_pct"],
            report["summary"]["total_return_pct"] - 2.5,
            places=4,
        )
        merged = {point["date"]: point for point in report["equity_curve"]["points"]}
        self.assertAlmostEqual(merged["2026-01-06"]["benchmark_return_pct"], 1.0, places=4)

    def test_benchmark_tops_up_when_index_bars_lag_stock_bars(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 2, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 2, 2), "buy", 1000, 10.0)
        self._save_close("600519", date(2026, 2, 3), 10.2)
        self._save_close("600519", date(2026, 2, 4), 10.5)
        # Two local benchmark points exist, so the old "<2 points" gate never refilled it.
        self._save_close("sh000300", date(2026, 2, 2), 4000.0)
        self._save_close("sh000300", date(2026, 2, 3), 4040.0)

        calls: List[Any] = []

        def fake_fetch(*, secid: str, start: date, end: date):
            calls.append((secid, start, end))
            return [
                {"date": date(2026, 2, 2), "open": 4000.0, "high": 4010.0, "low": 3990.0,
                 "close": 4000.0, "volume": 1.0, "amount": 4000.0, "pct_chg": 0.0},
                {"date": date(2026, 2, 3), "open": 4040.0, "high": 4050.0, "low": 4030.0,
                 "close": 4040.0, "volume": 1.0, "amount": 4040.0, "pct_chg": 1.0},
                {"date": date(2026, 2, 4), "open": 4060.0, "high": 4110.0, "low": 4050.0,
                 "close": 4100.0, "volume": 1.0, "amount": 4100.0, "pct_chg": 1.4851},
            ]

        from dataclasses import replace

        from src.services.market_data_sync_service import MarketDataSyncService

        service = PortfolioPerformanceService(
            portfolio_service=self.service,
            config=replace(get_config(), portfolio_market_data_sync_enabled=True),
            market_data_sync=MarketDataSyncService(db=self.db, fetch=fake_fetch),
        )
        report = service.get_performance_report(
            account_id=account_id, as_of=date(2026, 2, 4), include_realtime=False
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "1.000300")
        self.assertEqual(report["benchmark"]["series"][-1]["date"], "2026-02-04")
        self.assertAlmostEqual(report["benchmark"]["return_pct"], 2.5, places=4)

    def test_benchmark_top_up_is_throttled_when_upstream_stays_behind(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 3, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 3, 2), "buy", 1000, 10.0)
        self._save_close("600519", date(2026, 3, 3), 10.2)
        self._save_close("600519", date(2026, 3, 4), 10.5)
        self._save_close("600519", date(2026, 3, 5), 10.4)
        self._save_close("sh000300", date(2026, 3, 2), 4000.0)
        self._save_close("sh000300", date(2026, 3, 3), 4040.0)

        calls: List[Any] = []

        def fake_fetch(*, secid: str, start: date, end: date):
            calls.append((secid, start, end))
            return [
                {"date": date(2026, 3, 2), "open": 4000.0, "high": 4010.0, "low": 3990.0,
                 "close": 4000.0, "volume": 1.0, "amount": 4000.0, "pct_chg": 0.0},
                {"date": date(2026, 3, 3), "open": 4040.0, "high": 4050.0, "low": 4030.0,
                 "close": 4040.0, "volume": 1.0, "amount": 4040.0, "pct_chg": 1.0},
            ]

        from dataclasses import replace

        from src.services.market_data_sync_service import MarketDataSyncService

        service = PortfolioPerformanceService(
            portfolio_service=self.service,
            config=replace(get_config(), portfolio_market_data_sync_enabled=True),
            market_data_sync=MarketDataSyncService(db=self.db, fetch=fake_fetch),
        )
        for _ in range(2):
            report = service.get_performance_report(
                account_id=account_id, as_of=date(2026, 3, 5), include_realtime=False
            )

        # Upstream has no bar beyond 03-03, so the second report must not refetch.
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["benchmark"]["series"][-1]["date"], "2026-03-03")

    def test_benchmark_reports_missing_index_bars(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 5), include_realtime=False
        )

        benchmark = report["benchmark"]
        self.assertFalse(benchmark["available"])
        self.assertIsNone(benchmark["return_pct"])
        self.assertIn("000300", benchmark["note"])
        self.assertIn(benchmark["note"], report["notes"])
        self.assertTrue(
            all(point["benchmark_return_pct"] is None for point in report["equity_curve"]["points"])
        )

    def test_empty_account_returns_zeroed_report(self) -> None:
        account_id = self._account()

        report = self.performance_service.get_performance_report(
            account_id=account_id, as_of=date(2026, 1, 5), include_realtime=False
        )

        self.assertEqual(report["closed_trades"]["count"], 0)
        self.assertIsNone(report["closed_trades"]["win_rate_pct"])
        self.assertEqual(report["open_positions"]["count"], 0)
        self.assertIsNone(report["summary"]["total_return_pct"])
        self.assertTrue(any("入金" in note for note in report["notes"]))

    def test_performance_endpoint_returns_report(self) -> None:
        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)

        with patch.object(PortfolioRiskService, "_fetch_belong_boards", return_value=[]):
            response = self.client.get(
                "/api/v1/portfolio/performance",
                params={"account_id": account_id, "as_of": "2026-01-05", "include_realtime": False},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["currency"], "CNY")
        self.assertEqual(payload["summary"]["net_deposits"], 100_000.0)
        self.assertEqual(payload["open_positions"]["count"], 1)
        self.assertEqual(payload["open_positions"]["items"][0]["stock_code"], "600519")
        self.assertFalse(payload["benchmark"]["available"])
        self.assertEqual(payload["equity_curve"]["points"][-1]["date"], "2026-01-05")

    def test_market_data_refresh_endpoint_returns_sync_summary(self) -> None:
        payload = {
            "as_of": "2026-01-07",
            "positions": {"as_of": "2026-01-07", "synced": ["600519"], "failed": []},
            "benchmark": {
                "symbol": "000300",
                "synced": 3,
                "latest_date": "2026-01-07",
                "error": None,
            },
        }
        with patch(
            "api.v1.endpoints.portfolio.PortfolioPerformanceService"
        ) as service_class:
            service_class.return_value.refresh_market_data.return_value = payload
            response = self.client.post(
                "/api/v1/portfolio/market-data/refresh", params={"as_of": "2026-01-07"}
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["positions"]["synced"], ["600519"])
        self.assertEqual(body["benchmark"]["synced"], 3)
        self.assertEqual(body["benchmark"]["latest_date"], "2026-01-07")


    def test_refresh_market_data_syncs_only_positions_without_price(self) -> None:
        from src.services.market_data_sync_service import MarketDataSyncService

        account_id = self._account()
        self.service.record_cash_ledger(
            account_id=account_id, event_date=date(2026, 1, 1), direction="in", amount=100_000.0
        )
        self._trade(account_id, "600519", date(2026, 1, 5), "buy", 1000, 10.0)
        self.service.record_trade(
            account_id=account_id,
            symbol="600000",
            trade_date=date(2026, 1, 5),
            side="buy",
            quantity=100,
            price=8.0,
            market="cn",
            currency="CNY",
            trade_uid="2026-01-05-600000-buy",
        )

        class RecordingSync(MarketDataSyncService):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.requested_codes: Optional[List[str]] = None

            def sync_symbol_bars(self, codes, *, as_of=None):  # noqa: ANN001
                self.requested_codes = sorted(codes)
                return {
                    "synced": self.requested_codes,
                    "failed": [],
                    "as_of": (as_of or date(2026, 1, 5)).isoformat(),
                }

        index_rows = [
            {
                "date": date(2026, 1, day),
                "open": 4000.0 + day,
                "high": 4000.0 + day,
                "low": 4000.0 + day,
                "close": 4000.0 + day,
                "volume": 1.0,
                "amount": 1.0,
                "pct_chg": 0.0,
            }
            for day in (2, 3, 4)
        ]
        sync = RecordingSync(db=self.db, fetch=lambda **_kwargs: index_rows)
        service = PortfolioPerformanceService(portfolio_service=self.service, market_data_sync=sync)

        result = service.refresh_market_data(account_id=account_id, as_of=date(2026, 1, 5))

        self.assertEqual(sync.requested_codes, ["600000"])
        self.assertEqual(result["positions"]["synced"], ["600000"])
        self.assertEqual(result["benchmark"]["symbol"], "000300")
        self.assertEqual(result["benchmark"]["synced"], 3)

    def test_tencent_index_fetcher_maps_bars(self) -> None:
        from src.services.market_data_sync_service import (
            MarketDataSyncService,
            tencent_symbol_for_secid,
        )

        self.assertEqual(tencent_symbol_for_secid("1.000300"), "sh000300")
        self.assertEqual(tencent_symbol_for_secid("0.399001"), "sz399001")
        self.assertIsNone(tencent_symbol_for_secid("000300"))

        payload = {
            "data": {
                "sh000300": {
                    "day": [
                        ["2026-01-02", "4000.0", "4020.0", "4030.0", "3990.0", "1000.0"],
                        ["2026-01-05", "4020.0", "4100.0", "4110.0", "4015.0", "1200.0"],
                    ]
                }
            }
        }
        with patch(
            "src.services.market_data_sync_service.requests.get",
            return_value=SimpleNamespace(
                json=lambda: payload, raise_for_status=lambda: None
            ),
        ):
            rows = MarketDataSyncService._fetch_tencent_index_klines(
                secid="1.000300", start=date(2026, 1, 1), end=date(2026, 1, 6)
            )

        self.assertEqual([row["date"] for row in rows], [date(2026, 1, 2), date(2026, 1, 5)])
        self.assertEqual(rows[0]["close"], 4020.0)
        self.assertEqual(rows[1]["high"], 4110.0)
        self.assertIsNone(rows[0]["pct_chg"])
        self.assertAlmostEqual(rows[1]["pct_chg"], 1.9900497512, places=6)


if __name__ == "__main__":
    unittest.main()
