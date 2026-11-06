# -*- coding: utf-8 -*-
"""Unit tests for the A-share paper trading loop."""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.services import paper_trading_service as pt
from src.services.portfolio_service import PortfolioConflictError


_NEXT_TRADING_DAY = {
    date(2026, 10, 27): date(2026, 10, 28),
    date(2026, 10, 28): date(2026, 10, 29),
    date(2026, 10, 29): date(2026, 10, 30),
    date(2026, 10, 30): date(2026, 11, 2),
    date(2026, 10, 31): date(2026, 11, 2),
}
_SESSIONS = [
    date(2026, 10, 26),
    date(2026, 10, 27),
    date(2026, 10, 28),
    date(2026, 10, 29),
    date(2026, 10, 30),
    date(2026, 11, 2),
]


def _fake_next_trading_day(_market: str, day: date):
    return _NEXT_TRADING_DAY.get(day)


def _fake_trading_dates(_market: str, start: date, end: date):
    return [day for day in _SESSIONS if start <= day <= end]


@pytest.fixture(autouse=True)
def fake_calendar():
    with patch.object(pt, "get_next_trading_date", side_effect=_fake_next_trading_day), patch.object(
        pt, "get_trading_dates", side_effect=_fake_trading_dates
    ):
        yield


class _FakeRepo:
    def __init__(self) -> None:
        self.trades = []
        self.corporate_actions = []

    def list_trades(self, account_id, as_of):
        return [trade for trade in self.trades if trade.trade_date <= as_of]

    def list_corporate_actions(self, account_id, as_of):
        return [action for action in self.corporate_actions if action.effective_date <= as_of]


class _FakePortfolio:
    def __init__(self, *, accounts=None, cash=100_000.0, equity=None, positions=None) -> None:
        self.repo = _FakeRepo()
        self._accounts = list(accounts or [])
        self._cash = cash
        self._equity = cash if equity is None else equity
        self._positions = list(positions or [])
        self.created = []
        self.deposits = []
        self.written = []
        self.conflict_uids = set()
        self.snapshot_calls = []
        self.corporate_actions = []

    def list_accounts(self, include_inactive: bool = False):
        return list(self._accounts)

    def get_portfolio_snapshot(self, *, account_id, as_of, include_realtime=True, read_only=False):
        self.snapshot_calls.append(as_of)
        return {
            "accounts": [
                {
                    "total_cash": self._cash,
                    "total_equity": self._equity,
                    "data_quality": "ok",
                    "positions": list(self._positions),
                }
            ]
        }

    def create_account(self, **kwargs):
        account = {
            "id": 42,
            "name": kwargs["name"],
            "market": "cn",
            "base_currency": "CNY",
            "is_active": True,
        }
        self._accounts.append(account)
        self.created.append(kwargs)
        return account

    def record_cash_ledger(self, **kwargs):
        self.deposits.append(kwargs)
        return {"id": len(self.deposits)}

    def record_trade(self, **kwargs):
        if kwargs.get("trade_uid") in self.conflict_uids:
            raise PortfolioConflictError("duplicate trade_uid")
        self.written.append(kwargs)
        self.repo.trades.append(
            SimpleNamespace(
                id=len(self.written),
                trade_date=kwargs["trade_date"],
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                note=kwargs.get("note"),
                trade_uid=kwargs.get("trade_uid"),
            )
        )
        return {"id": len(self.written)}

    def record_corporate_action(self, **kwargs):
        self.corporate_actions.append(kwargs)
        self.repo.corporate_actions.append(
            SimpleNamespace(
                symbol=kwargs["symbol"],
                effective_date=kwargs["effective_date"],
                action_type=kwargs["action_type"],
            )
        )
        return {"id": len(self.corporate_actions)}


class _FakeSignals:
    def __init__(self, items):
        self.items = list(items)

    def list_signals(self, *, market, status, page, page_size):
        start = (page - 1) * page_size
        return {
            "items": self.items[start:start + page_size],
            "total": len(self.items),
            "page": page,
            "page_size": page_size,
        }

    def get_signal(self, signal_id):
        for item in self.items:
            if item.get("id") == signal_id:
                return item
        raise KeyError(signal_id)


class _Service(pt.PaperTradingService):
    def __init__(self, *, latest, bars, portfolio=None, signals=None, dividend_fetcher=None, stock_repo=None):
        super().__init__(
            portfolio_service=portfolio or _FakePortfolio(),
            signal_service=signals or _FakeSignals([]),
            stock_repo=stock_repo or object(),
            dividend_fetcher=dividend_fetcher,
        )
        self._latest = latest
        self._bars = bars

    def _latest_market_date(self):
        return self._latest

    def _bar(self, code, day):
        return self._bars.get((pt._plain_code(code), day))


class _FakeStockRepo:
    """Only ``get_range`` is needed for the ADV lookback."""

    def __init__(self, rows):
        self._rows = list(rows)

    def get_range(self, code, start, end):
        return [
            row
            for row in self._rows
            if pt._plain_code(row.code) == pt._plain_code(code) and start <= row.date <= end
        ]


def _bar(open_price, close_price, *, high=None, low=None):
    return SimpleNamespace(
        open=open_price,
        close=close_price,
        high=close_price if high is None else high,
        low=close_price if low is None else low,
    )


def _signal(
    signal_id,
    code,
    action,
    *,
    score=70,
    created="2026-10-29T15:00:00",
    horizon="3d",
    name="测试",
    stop=None,
    target=None,
):
    return {
        "id": signal_id,
        "stock_code": code,
        "stock_name": name,
        "action": action,
        "score": score,
        "horizon": horizon,
        "created_at": created,
        "trace_id": f"trace-{signal_id}",
        "stop_loss": stop,
        "target_price": target,
    }


def _trade(trade_id, code, *, side="buy", day=date(2026, 10, 28), signal_id=7):
    return SimpleNamespace(
        id=trade_id,
        trade_date=day,
        symbol=code,
        side=side,
        note=f"paper trading: signal #{signal_id}",
        trade_uid=f"paper-{day.isoformat()}-{code}-{side}",
    )


def _position(code, quantity, price=10.0):
    return {
        "symbol": code,
        "market": "cn",
        "quantity": quantity,
        "market_value_base": quantity * price,
        "last_price": price,
        "price_available": True,
    }


def _account(account_id=1, name="纸面交易"):
    return {"id": account_id, "name": name, "market": "cn", "is_active": True}


# ----------------------------------------------------------------------
# Pure rules
# ----------------------------------------------------------------------
def test_compute_fees_matches_a_share_schedule():
    fee, tax = pt.compute_fees("buy", Decimal("10000"))
    assert fee == Decimal("5.10")
    assert tax == Decimal("0.00")

    fee, tax = pt.compute_fees("sell", Decimal("10000"))
    assert fee == Decimal("5.10")
    assert tax == Decimal("5.00")


def test_limit_helpers():
    assert pt.is_limit_up(Decimal("10"), Decimal("11.00"), Decimal("0.10"))
    assert not pt.is_limit_up(Decimal("10"), Decimal("10.99"), Decimal("0.10"))
    assert pt.is_limit_down(Decimal("10"), Decimal("9.00"), Decimal("0.10"))
    assert not pt.is_limit_down(Decimal("10"), Decimal("9.01"), Decimal("0.10"))
    assert pt.price_limit_pct("300750") == Decimal("0.20")
    assert pt.price_limit_pct("688981") == Decimal("0.20")
    assert pt.price_limit_pct("600519") == Decimal("0.10")


def test_lot_and_code_helpers():
    assert pt._plain_code("SH600519") == "600519"
    assert pt._plain_code("600519.SH") == "600519"
    assert pt.clamp_buy_qty("600519", 250) == 200
    assert pt.clamp_buy_qty("600519", 99) == 0
    assert pt.clamp_buy_qty("688981", 250) == 250
    assert pt.clamp_buy_qty("688981", 199) == 0


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------
def test_buy_plan_uses_reference_close_and_ranks_by_score():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals(
        [
            _signal(1, "600519", "buy", score=80),
            _signal(2, "000001", "buy", score=70),
            _signal(3, "600000", "watch", score=90),
        ]
    )
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 99.0),
        ("600519", date(2026, 10, 30)): _bar(100.0, 101.0),
        ("000001", date(2026, 10, 29)): _bar(0, 49.0),
        ("000001", date(2026, 10, 30)): _bar(50.0, 51.0),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(min_score=60, max_names=5, max_weight=0.2, dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    assert result["batch"] == {
        "signal_dates": ["2026-10-29"],
        "fill_date": "2026-10-30",
        "reference_date": "2026-10-29",
    }
    orders = result["orders"]
    assert [(order["stock_code"], order["side"], order["quantity"], order["price"]) for order in orders] == [
        ("600519", "buy", 200, 100.0),
        ("000001", "buy", 400, 50.0),
    ]
    assert result["portfolio"]["equity"] == 100_000.0
    assert result["portfolio"]["per_name"] == 20_000.0
    assert result["portfolio"]["held_names"] == 0


def test_min_score_and_max_names_limit_buys():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals(
        [
            _signal(1, "600519", "buy", score=80),
            _signal(2, "000001", "buy", score=70),
            _signal(3, "600000", "buy", score=65),
        ]
    )
    bars = {}
    for code in ("600519", "000001", "600000"):
        bars[(code, date(2026, 10, 29))] = _bar(0, 10.0)
        bars[(code, date(2026, 10, 30))] = _bar(10.0, 10.1)
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(min_score=68, max_names=1, max_weight=0.2, dry_run=True)

    assert [order["stock_code"] for order in result["orders"]] == ["600519"]
    reasons = {item["stock_code"]: item["reason"] for item in result["items"]}
    assert "score 65" in reasons["600000"]
    assert reasons["000001"].startswith("已持/已排 1 只")


def test_newer_pending_signal_does_not_starve_due_batch():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals(
        [
            _signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00"),
            _signal(2, "600519", "watch", score=80, created="2026-10-31T10:00:00"),
        ]
    )
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.1),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    assert [(order["stock_code"], order["side"]) for order in result["orders"]] == [("600519", "buy")]


def test_sells_use_reference_date_positions_and_apply_t1():
    portfolio = _FakePortfolio(
        accounts=[_account()],
        cash=50_000.0,
        equity=60_000.0,
        positions=[_position("600519", 1000, price=10.0), _position("000001", 300, price=10.0)],
    )
    signals = _FakeSignals(
        [
            _signal(1, "600519", "sell", score=10),
            _signal(2, "000001", "reduce", score=30),
        ]
    )
    bars = {}
    for code in ("600519", "000001"):
        bars[(code, date(2026, 10, 29))] = _bar(0, 10.0)
        bars[(code, date(2026, 10, 30))] = _bar(10.2, 10.3)
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=True)

    sells = [order for order in result["orders"] if order["side"] == "sell"]
    assert [(order["stock_code"], order["quantity"]) for order in sells] == [
        ("000001", 100),
        ("600519", 1000),
    ]
    for order in sells:
        assert order["tax"] > 0


def test_limit_up_blocks_buy_and_limit_down_blocks_sell():
    portfolio = _FakePortfolio(
        accounts=[_account()],
        positions=[_position("600000", 1000, price=10.0)],
    )
    signals = _FakeSignals(
        [
            _signal(1, "600519", "buy", score=80),
            _signal(2, "600000", "sell", score=10),
        ]
    )
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(11.0, 11.0),
        ("600000", date(2026, 10, 29)): _bar(0, 10.0),
        ("600000", date(2026, 10, 30)): _bar(9.0, 9.0),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=True)

    assert result["orders"] == []
    reasons = {item["stock_code"]: item["reason"] for item in result["items"]}
    assert "涨停" in reasons["600519"]
    assert "跌停" in reasons["600000"]


def test_data_not_ready_when_fill_day_bar_is_missing():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80)])
    service = _Service(latest=date(2026, 10, 29), bars={}, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_DATA_NOT_READY
    assert "2026-10-30" in result["message"]
    assert result["latest_data_date"] == "2026-10-29"
    assert portfolio.written == []


def test_overdue_batch_is_replayed_at_its_own_open():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.8, 11.0),
        ("600519", date(2026, 11, 2)): _bar(12.0, 12.1),
    }
    service = _Service(latest=date(2026, 11, 2), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_NO_DUE_BATCH
    catchup = result["catchup"]
    assert catchup["days"] == ["2026-10-30"]
    day = catchup["results"][0]
    assert day["status"] == pt.STATUS_PLANNED
    assert day["orders"][0]["price"] == 10.8
    assert "补跑 1 个交易日" in result["message"]


def test_signal_date_parameter_backfills_a_batch():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals(
        [
            _signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00"),
            _signal(2, "600519", "watch", score=80, created="2026-10-31T10:00:00"),
        ]
    )
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.1),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(signal_date=date(2026, 10, 29), dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    assert result["batch"]["fill_date"] == "2026-10-30"


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------
def test_apply_creates_account_deposits_and_writes_trades():
    portfolio = _FakePortfolio(accounts=[])
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80)])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.1),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=False, initial_cash=100_000, max_names=5, max_weight=0.2)

    assert result["status"] == pt.STATUS_EXECUTED
    assert portfolio.created[0]["name"] == "纸面交易"
    assert portfolio.created[0]["broker"] == "paper"
    assert portfolio.deposits[0]["event_date"] == date(2026, 10, 29)
    assert portfolio.deposits[0]["amount"] == 100_000.0
    trade = portfolio.written[0]
    assert trade["account_id"] == 42
    assert trade["trade_date"] == date(2026, 10, 30)
    assert trade["trade_uid"] == "paper-2026-10-30-600519-buy"
    assert trade["fee"] == 5.20
    assert trade["tax"] == 0.0
    assert result["orders"][0]["status"] == "executed"


def test_apply_refuses_second_batch_on_same_day():
    account = _account(account_id=1)
    portfolio = _FakePortfolio(accounts=[account])
    portfolio.repo.trades.append(SimpleNamespace(trade_date=date(2026, 10, 30)))
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80)])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.1),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(account_id=1, dry_run=False)

    assert result["status"] == pt.STATUS_DUPLICATE_BATCH
    assert portfolio.written == []


def test_force_rerun_marks_existing_trades_as_duplicates():
    account = _account(account_id=1)
    portfolio = _FakePortfolio(accounts=[account])
    portfolio.repo.trades.append(SimpleNamespace(trade_date=date(2026, 10, 30)))
    portfolio.conflict_uids.add("paper-2026-10-30-600519-buy")
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80)])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.1),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(account_id=1, dry_run=False, force=True)

    assert result["status"] == pt.STATUS_NOTHING_TO_DO
    assert result["orders"][0]["status"] == "duplicate"
    assert portfolio.written == []


def test_rejects_non_a_share_market():
    service = _Service(latest=date(2026, 10, 30), bars={})
    with pytest.raises(ValueError, match="A-share"):
        service.run(market="us")


# ----------------------------------------------------------------------
# Missing-bar prefetch
# ----------------------------------------------------------------------
class _FakeBarRepo:
    """Stands in for StockRepository: DB handle plus a bar lookup."""

    def __init__(self, bars=None, *, held=None, allow_save=True):
        self.bars = dict(bars or {})
        self.held = list(held or [])
        self.saved = []
        self.save_calls = []
        if allow_save:
            self.db = self

    def list_cached_position_identities(self, account_id=None):
        return list(self.held)

    def get_daily_on_date(self, code, target_date):
        return self.bars.get((pt._plain_code(code), target_date))

    def save_daily_data(self, frame, code, data_source="Unknown"):
        self.save_calls.append((code, data_source))
        for day in frame:
            self.bars.setdefault((pt._plain_code(code), day), _bar(10.0, 10.1))
            self.saved.append((code, day))
        return len(frame)


class _RepoService(pt.PaperTradingService):
    def __init__(self, *, latest, repo, portfolio, signals):
        super().__init__(portfolio_service=portfolio, signal_service=signals, stock_repo=repo)
        self._latest = latest

    def _latest_market_date(self):
        return self._latest


class _FakeFrame(list):
    @property
    def empty(self):
        return len(self) == 0


def test_prefetch_fills_bars_for_batch_and_cached_positions():
    account = _account(account_id=1)
    portfolio = _FakePortfolio(accounts=[account], cash=100_000.0)
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")])
    repo = _FakeBarRepo(held=[("cn", "000001"), ("us", "AAPL")])
    portfolio.repo = repo
    service = _RepoService(
        latest=date(2026, 10, 30),
        repo=repo,
        portfolio=portfolio,
        signals=signals,
    )

    def fake_loader(code, *, days, target_date):
        assert days == pt._MISSING_BAR_FETCH_DAYS
        assert target_date == date(2026, 10, 30)
        return _FakeFrame([date(2026, 10, 29), date(2026, 10, 30)]), "fake_provider"

    with patch.dict(os.environ, {"PAPER_TRADING_FETCH_MISSING_BARS": "true"}, clear=False), patch(
        "src.services.history_loader.load_history_df", side_effect=fake_loader
    ) as loader:
        result = service.run(account_id=1, dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    assert [call.args[0] for call in loader.call_args_list] == ["000001", "600519"]
    assert {code for code, _source in repo.save_calls} == {"000001", "600519"}
    assert all(source == "fake_provider" for _code, source in repo.save_calls)
    assert all(entry["status"] == "planned" for entry in result["items"])


def test_prefetch_skips_repo_without_db_handle():
    portfolio = _FakePortfolio(accounts=[_account(account_id=1)])
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.1),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    with patch.dict(os.environ, {"PAPER_TRADING_FETCH_MISSING_BARS": "true"}, clear=False), patch(
        "src.services.history_loader.load_history_df"
    ) as loader:
        result = service.run(account_id=1, dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    loader.assert_not_called()


def test_prefetch_warns_and_keeps_going_when_provider_returns_nothing():
    portfolio = _FakePortfolio(accounts=[_account(account_id=1)])
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")])
    repo = _FakeBarRepo()
    portfolio.repo = repo
    service = _RepoService(
        latest=date(2026, 10, 30),
        repo=repo,
        portfolio=portfolio,
        signals=signals,
    )

    with patch.dict(os.environ, {"PAPER_TRADING_FETCH_MISSING_BARS": "true"}, clear=False), patch(
        "src.services.history_loader.load_history_df", return_value=(None, "none")
    ):
        result = service.run(account_id=1, dry_run=True)

    assert result["status"] == pt.STATUS_NOTHING_TO_DO
    assert repo.saved == []
    assert any("600519" in warning and "取数失败" in warning for warning in result["warnings"])


def test_prefetch_is_off_by_default():
    portfolio = _FakePortfolio(accounts=[_account(account_id=1)])
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")])
    repo = _FakeBarRepo()
    portfolio.repo = repo
    service = _RepoService(
        latest=date(2026, 10, 30),
        repo=repo,
        portfolio=portfolio,
        signals=signals,
    )

    with patch.dict(os.environ, {}, clear=False), patch(
        "src.services.history_loader.load_history_df"
    ) as loader:
        os.environ.pop("PAPER_TRADING_FETCH_MISSING_BARS", None)
        service.run(account_id=1, dry_run=True)

    loader.assert_not_called()


# ----------------------------------------------------------------------
# Exit rules (stop loss / take profit from the opening signal)
# ----------------------------------------------------------------------
def _exit_service(*, stop, target, fill_bar):
    """Holding 600519 from a signal-7 buy on 2026-10-28; runs on 2026-10-30."""
    portfolio = _FakePortfolio(
        accounts=[_account()],
        cash=50_000.0,
        equity=60_000.0,
        positions=[_position("600519", 1000, price=10.0)],
    )
    portfolio.repo.trades.append(_trade(1, "600519"))
    signals = _FakeSignals([_signal(7, "600519", "buy", created="2026-10-27T15:00:00", stop=stop, target=target)])
    bars = {
        ("600519", date(2026, 10, 27)): _bar(0, 10.0),
        ("600519", date(2026, 10, 28)): _bar(10.0, 10.0),
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): fill_bar,
    }
    return _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)


def test_stop_loss_closes_position_at_stop_price():
    service = _exit_service(stop=9.0, target=12.0, fill_bar=_bar(10.0, 9.5, high=10.2, low=8.8))

    result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert (order["stock_code"], order["side"], order["quantity"], order["price"]) == ("600519", "sell", 1000, 9.0)
    assert order["trade_uid"] == "paper-2026-10-30-600519-sell-exit"
    exit_item = next(item for item in result["items"] if item["action"] == "stop_loss")
    assert exit_item["stock_code"] == "600519"
    assert "止损" in exit_item["reason"]
    assert exit_item["tax"] > 0


def test_take_profit_fills_at_open_when_gapping_above_target():
    service = _exit_service(stop=8.0, target=11.0, fill_bar=_bar(11.5, 11.6, high=11.8, low=11.2))

    result = service.run(dry_run=True)

    assert len(result["orders"]) == 1
    assert result["orders"][0]["price"] == 11.5
    assert result["items"][0]["action"] == "take_profit"
    assert "止盈" in result["items"][0]["reason"]


def test_exit_levels_are_ignored_until_touched():
    service = _exit_service(stop=9.0, target=12.0, fill_bar=_bar(10.0, 10.4, high=10.5, low=9.5))

    result = service.run(dry_run=True)

    assert result["orders"] == []


def test_limit_down_open_keeps_position_even_when_stop_is_hit():
    service = _exit_service(stop=9.0, target=12.0, fill_bar=_bar(9.0, 9.0, high=9.1, low=8.9))

    result = service.run(dry_run=True)

    assert result["orders"] == []
    exits = [item for item in result["items"] if item["action"] == "stop_loss"]
    assert exits and "跌停" in exits[0]["reason"]


# ----------------------------------------------------------------------
# Carry-over of unfilled orders
# ----------------------------------------------------------------------
def _carry_service(*, fill_ok_open=11.2, trades=None, extra_env=None):
    portfolio = _FakePortfolio(accounts=[_account()])
    portfolio.repo.trades.extend(trades or [])
    signals = _FakeSignals([_signal(1, "600519", "buy", created="2026-10-28T15:00:00")])
    bars = {
        ("600519", date(2026, 10, 28)): _bar(0, 10.0),
        ("600519", date(2026, 10, 29)): _bar(11.0, 11.0),
        ("600519", date(2026, 10, 30)): _bar(fill_ok_open, 11.4),
    }
    return _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)


def test_limit_up_blocked_order_is_carried_to_the_next_session():
    service = _carry_service()

    result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_PLANNED
    assert result["signal_counts"]["carried"] == 1
    assert result["batch"]["signal_dates"] == ["2026-10-28"]
    order = result["orders"][0]
    assert (order["stock_code"], order["side"]) == ("600519", "buy")
    assert order["trade_uid"] == "paper-2026-10-29-600519-buy"


def test_missed_tradable_session_is_filled_at_that_session_open():
    service = _carry_service(fill_ok_open=11.2)
    service._bars[("600519", date(2026, 10, 29))] = _bar(10.4, 10.5)

    result = service.run(dry_run=True)

    # 10-29 was tradable but never ran; catch-up must fill it at 10-29's open
    # instead of dropping the signal as a missed session.
    day = result["catchup"]["results"][0]
    assert day["fill_date"] == "2026-10-29"
    assert day["status"] == pt.STATUS_PLANNED
    assert day["orders"][0]["price"] == 10.4


def test_carry_over_respects_retry_day_setting():
    service = _carry_service()

    with patch.dict(os.environ, {"PAPER_TRADING_RETRY_DAYS": "0"}, clear=False):
        result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_NO_DUE_BATCH
    assert result["signal_counts"]["carried"] == 0


def test_carry_over_skips_orders_that_already_filled():
    filled = _trade(1, "600519", day=date(2026, 10, 29), signal_id=1)
    service = _carry_service(trades=[filled])

    result = service.run(dry_run=True)

    assert result["status"] == pt.STATUS_NO_DUE_BATCH
    assert result["orders"] == []


# ----------------------------------------------------------------------
# Catch-up of missed sessions
# ----------------------------------------------------------------------
def _catchup_service(*, signals, bars, latest=date(2026, 10, 30), portfolio=None):
    return _Service(
        latest=latest,
        bars=bars,
        portfolio=portfolio or _FakePortfolio(accounts=[_account()]),
        signals=_FakeSignals(signals),
    )


def test_catchup_replays_missed_sessions_in_ascending_order():
    signals = [
        _signal(1, "600519", "buy", score=80, created="2026-10-27T15:00:00"),
        _signal(2, "000001", "buy", score=70, created="2026-10-28T15:00:00"),
    ]
    bars = {
        ("600519", date(2026, 10, 27)): _bar(0, 10.0),
        ("600519", date(2026, 10, 28)): _bar(10.5, 10.6),
        ("600519", date(2026, 10, 29)): _bar(10.7, 10.8),
        ("600519", date(2026, 10, 30)): _bar(10.9, 11.0),
        ("000001", date(2026, 10, 28)): _bar(0, 20.0),
        ("000001", date(2026, 10, 29)): _bar(20.5, 20.6),
    }
    service = _catchup_service(signals=signals, bars=bars)

    result = service.run(dry_run=True)

    days = [row["fill_date"] for row in result["catchup"]["results"]]
    assert days == ["2026-10-28", "2026-10-29"]
    prices = {
        order["stock_code"]: order["price"]
        for row in result["catchup"]["results"]
        for order in row["orders"]
    }
    assert prices == {"600519": 10.5, "000001": 20.5}


def test_catchup_rerun_does_not_duplicate_trades():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = [_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")]
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.5, 10.6),
        ("600519", date(2026, 11, 2)): _bar(12.0, 12.1),
    }
    service = _catchup_service(
        signals=signals,
        bars=bars,
        latest=date(2026, 11, 2),
        portfolio=portfolio,
    )

    first = service.run(account_id=1, dry_run=False)
    assert first["catchup"]["results"][0]["status"] == pt.STATUS_EXECUTED
    written = len(portfolio.written)

    second = service.run(account_id=1, dry_run=False)

    assert len(portfolio.written) == written
    assert second["catchup"]["results"][0]["status"] == pt.STATUS_DUPLICATE_BATCH


def test_catchup_dry_run_never_writes():
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = [_signal(1, "600519", "buy", score=80, created="2026-10-29T15:00:00")]
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.5, 10.6),
        ("600519", date(2026, 11, 2)): _bar(12.0, 12.1),
    }
    service = _catchup_service(
        signals=signals,
        bars=bars,
        latest=date(2026, 11, 2),
        portfolio=portfolio,
    )

    result = service.run(dry_run=True)

    assert result["catchup"]["results"][0]["status"] == pt.STATUS_PLANNED
    assert portfolio.written == []


def test_catchup_respects_session_cap():
    signals = [
        _signal(1, "600519", "buy", score=80, created="2026-10-27T15:00:00"),
        _signal(2, "000001", "buy", score=70, created="2026-10-28T15:00:00"),
    ]
    bars = {
        ("600519", date(2026, 10, 27)): _bar(0, 10.0),
        ("600519", date(2026, 10, 28)): _bar(10.5, 10.6),
        ("000001", date(2026, 10, 28)): _bar(0, 20.0),
        ("000001", date(2026, 10, 29)): _bar(20.5, 20.6),
    }
    service = _catchup_service(signals=signals, bars=bars)

    with patch.dict(os.environ, {"PAPER_TRADING_CATCHUP_DAYS": "1"}, clear=False):
        result = service.run(dry_run=True)

    assert result["catchup"]["days"] == ["2026-10-28"]
    assert result["catchup"]["pending_days"] == ["2026-10-29"]


# ----------------------------------------------------------------------
# Corporate actions
# ----------------------------------------------------------------------
_DIVIDEND_EVENTS = [
    {"ex_dividend_date": "2026-10-30", "cash_dividend_per_share": 0.5, "split_ratio": 1.0},
    {"ex_dividend_date": "2026-10-29", "cash_dividend_per_share": 0.0, "split_ratio": 1.1},
]


def _dividend_service():
    portfolio = _FakePortfolio(accounts=[_account()])
    portfolio.repo.trades.append(_trade(1, "600519"))
    signals = _FakeSignals([_signal(1, "600519", "buy", created="2026-10-27T15:00:00")])
    bars = {
        ("600519", date(2026, 10, 27)): _bar(0, 10.0),
        ("600519", date(2026, 10, 28)): _bar(10.0, 10.0),
    }
    return _Service(
        latest=date(2026, 10, 30),
        bars=bars,
        portfolio=portfolio,
        signals=signals,
        dividend_fetcher=lambda code: list(_DIVIDEND_EVENTS),
    )


def test_dividends_and_splits_are_registered_for_held_symbols():
    service = _dividend_service()

    result = service.run(account_id=1, dry_run=False)

    recorded = service.portfolio.corporate_actions
    assert [(row["action_type"], row["effective_date"]) for row in recorded] == [
        ("cash_dividend", date(2026, 10, 30)),
        ("split_adjustment", date(2026, 10, 29)),
    ]
    assert recorded[0]["cash_dividend_per_share"] == 0.5
    assert recorded[1]["split_ratio"] == pytest.approx(1.1)
    assert any("每股派 0.5 元" in line for line in result["corporate_actions"])
    assert any("送转 1.1 倍" in line for line in result["corporate_actions"])


def test_corporate_actions_are_not_registered_twice():
    service = _dividend_service()
    service.run(account_id=1, dry_run=False)

    result = service.run(account_id=1, dry_run=False)

    assert len(service.portfolio.corporate_actions) == 2
    assert result["corporate_actions"] == []




# ----------------------------------------------------------------------
# Board tradability
# ----------------------------------------------------------------------
def test_star_and_hk_signals_are_reported_but_not_bought(monkeypatch):
    # Other test modules load the repository .env, which narrows the boards
    # to main only; this case is about the documented default of main,gem.
    monkeypatch.delenv(pt.PAPER_TRADING_ALLOWED_BOARDS_ENV, raising=False)
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals(
        [
            _signal(1, "688286", "buy", score=90),
            _signal(2, "HK00700", "buy", score=88),
            _signal(3, "300750", "buy", score=70),
            _signal(4, "600519", "buy", score=65),
        ]
    )
    bars = {}
    for code in ("300750", "600519"):
        bars[(code, date(2026, 10, 29))] = _bar(0, 10.0)
        bars[(code, date(2026, 10, 30))] = _bar(10.0, 10.1)
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(min_score=60, max_names=5, max_weight=0.2, dry_run=True)

    assert [order["stock_code"] for order in result["orders"]] == ["300750", "600519"]
    reasons = {item["stock_code"]: item["reason"] for item in result["items"]}
    assert "科创板" in reasons["688286"]
    assert "港股" in reasons["HK00700"]
    assert "主板/创业板" in reasons["688286"]


def test_board_filter_follows_the_allowed_boards_env(monkeypatch):
    monkeypatch.setenv(pt.PAPER_TRADING_ALLOWED_BOARDS_ENV, "main,gem,star")
    portfolio = _FakePortfolio(accounts=[_account()])
    signals = _FakeSignals([_signal(1, "688286", "buy", score=90)])
    bars = {
        ("688286", date(2026, 10, 29)): _bar(0, 50.0),
        ("688286", date(2026, 10, 30)): _bar(50.0, 50.5),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(min_score=60, max_names=5, max_weight=0.2, dry_run=True)

    assert [(order["stock_code"], order["side"], order["quantity"]) for order in result["orders"]] == [
        ("688286", "buy", 400)
    ]


def test_untradable_board_holding_can_still_be_sold():
    portfolio = _FakePortfolio(
        accounts=[_account()],
        positions=[_position("688981", 200, price=50.0)],
    )
    signals = _FakeSignals([_signal(1, "688981", "sell", score=10)])
    bars = {
        ("688981", date(2026, 10, 29)): _bar(0, 50.0),
        ("688981", date(2026, 10, 30)): _bar(52.0, 52.5),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(dry_run=True)

    assert [(order["stock_code"], order["side"], order["quantity"]) for order in result["orders"]] == [
        ("688981", "sell", 200)
    ]


# ----------------------------------------------------------------------
# Slippage and liquidity (ADV) cost model
# ----------------------------------------------------------------------
def _volume_rows(code, *, count=20, volume=10_000.0, end=date(2026, 10, 28)):
    return [
        SimpleNamespace(code=code, date=end - timedelta(days=count - 1 - index), volume=volume)
        for index in range(count)
    ]


def test_slippage_never_breaks_daily_price_limits():
    assert pt.apply_slippage(10.99, "buy", code="600519", previous_close=10.0, bps=50) == 11.0
    assert pt.apply_slippage(9.01, "sell", code="600519", previous_close=10.0, bps=50) == 9.0
    assert pt.apply_slippage(10.0, "buy", code="600519", previous_close=None, bps=0) == 10.0


def test_slippage_moves_buy_up_and_sell_down(monkeypatch):
    monkeypatch.setenv(pt.PAPER_TRADING_SLIPPAGE_BPS_ENV, "50")
    portfolio = _FakePortfolio(
        accounts=[_account()],
        cash=1_000_000.0,
        equity=1_000_000.0,
        positions=[_position("000001", 1000, price=10.0)],
    )
    signals = _FakeSignals(
        [
            _signal(1, "600519", "buy", score=80),
            _signal(2, "000001", "sell", score=70),
        ]
    )
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.2),
        ("000001", date(2026, 10, 29)): _bar(0, 10.0),
        ("000001", date(2026, 10, 30)): _bar(10.0, 9.9),
    }
    service = _Service(latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals)

    result = service.run(min_score=60, max_names=5, max_weight=0.2, dry_run=True)

    orders = {order["stock_code"]: order for order in result["orders"]}
    assert orders["600519"]["side"] == "buy"
    assert orders["600519"]["price"] == 10.05
    assert orders["000001"]["side"] == "sell"
    assert orders["000001"]["price"] == 9.95


def test_adv_cap_trims_buy_quantity(monkeypatch):
    monkeypatch.setenv(pt.PAPER_TRADING_MAX_ADV_PCT_ENV, "1")
    monkeypatch.setenv(pt.PAPER_TRADING_ADV_WINDOW_DAYS_ENV, "20")
    portfolio = _FakePortfolio(accounts=[_account()], cash=1_000_000.0, equity=1_000_000.0)
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80)])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.2),
    }
    repo = _FakeStockRepo(_volume_rows("600519", volume=10_000.0))
    service = _Service(
        latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals, stock_repo=repo
    )

    result = service.run(min_score=60, max_names=5, max_weight=0.2, dry_run=True)

    assert len(result["orders"]) == 1
    assert result["orders"][0]["quantity"] == 100
    item = next(row for row in result["items"] if row["stock_code"] == "600519")
    assert "受流动性上限裁剪" in item["reason"]


def test_adv_cap_skips_order_when_limit_is_below_one_lot(monkeypatch):
    monkeypatch.setenv(pt.PAPER_TRADING_MAX_ADV_PCT_ENV, "1")
    portfolio = _FakePortfolio(accounts=[_account()], cash=1_000_000.0, equity=1_000_000.0)
    signals = _FakeSignals([_signal(1, "600519", "buy", score=80)])
    bars = {
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.2),
    }
    repo = _FakeStockRepo(_volume_rows("600519", volume=500.0))
    service = _Service(
        latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals, stock_repo=repo
    )

    result = service.run(min_score=60, max_names=5, max_weight=0.2, dry_run=True)

    assert result["orders"] == []
    item = next(row for row in result["items"] if row["stock_code"] == "600519")
    assert "流动性上限" in item["reason"]


def test_exit_uses_slippage_and_sells_partial_when_adv_is_capped(monkeypatch):
    monkeypatch.setenv(pt.PAPER_TRADING_SLIPPAGE_BPS_ENV, "50")
    monkeypatch.setenv(pt.PAPER_TRADING_MAX_ADV_PCT_ENV, "1")
    portfolio = _FakePortfolio(
        accounts=[_account()],
        cash=50_000.0,
        equity=60_000.0,
        positions=[_position("600519", 1000, price=10.0)],
    )
    portfolio.repo.trades.append(_trade(1, "600519"))
    signals = _FakeSignals([_signal(7, "600519", "buy", created="2026-10-27T15:00:00", stop=9.0, target=12.0)])
    bars = {
        ("600519", date(2026, 10, 27)): _bar(0, 10.0),
        ("600519", date(2026, 10, 28)): _bar(10.0, 10.0),
        ("600519", date(2026, 10, 29)): _bar(0, 10.0),
        ("600519", date(2026, 10, 30)): _bar(10.0, 10.4, high=12.5, low=9.8),
    }
    repo = _FakeStockRepo(_volume_rows("600519", volume=40_000.0))
    service = _Service(
        latest=date(2026, 10, 30), bars=bars, portfolio=portfolio, signals=signals, stock_repo=repo
    )

    result = service.run(dry_run=True)

    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert (order["side"], order["quantity"], order["price"]) == ("sell", 400, 11.94)
    item = next(row for row in result["items"] if row["action"] == "take_profit")
    assert "部分成交" in item["reason"]
