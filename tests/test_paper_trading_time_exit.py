# -*- coding: utf-8 -*-
"""纸面交易「时间止盈」出口（exit.max_holding_days）的回归测试。

覆盖：
  - 策略档解析：正整数生效、非法值忽略并告警、空配置零行为变化；
  - 计划层：满 N 个交易日按开盘价卖出、未满不动、当日买入不动；
  - 互斥：当日已有信号卖出时不再重复时间止盈；
  - 边界：开盘跌停卖不出时保留持仓并给出原因；
  - 成交流水 note 追加 [time_exit] 标记（供冷却识别）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List

from src.services import paper_trading_service as pt

BUY_DAY = date(2026, 11, 16)      # 周一
REF_DAY = date(2026, 11, 18)      # 周三（参考日收盘）
FILL_DAY = date(2026, 11, 19)     # 周四（满 3 个交易日：17/18/19）
CODE = "600760"

SESSIONS: List[date] = [
    date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17),
    date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20),
]

SIGNALS: Dict[int, Dict[str, Any]] = {
    165: {"id": 165, "stock_code": CODE, "action": "buy", "score": 68,
          "stop_loss": None, "target_price": 53.0},
}


def _bar(open_price: float, close: float = 46.0, high: float = 47.0, low: float = 45.5):
    return SimpleNamespace(open=open_price, high=max(high, open_price), low=min(low, open_price), close=close)


class _Harness(pt.PaperTradingService):
    """只装配 _build_plan 需要的依赖，不连库、不建账户。"""

    def __init__(self, *, bars: Dict[Any, Any]) -> None:
        self._bars = bars
        self._sessions = SESSIONS
        self.signals = SimpleNamespace(get_signal=lambda sid: SIGNALS.get(int(sid), {}))

    def _bar(self, code: str, day: date):
        return self._bars.get((pt._plain_code(code), day))

    def _adv_cap_qty(self, **kwargs: Any):
        return None

    def _sessions_between(self, market: str, start: date, end: date) -> List[date]:
        return [day for day in self._sessions if start <= day < end]


def _trade(*, buy_day: date = BUY_DAY, symbol: str = CODE) -> Any:
    return SimpleNamespace(trade_date=buy_day, note="paper trading: signal #165",
                           price=48.97, symbol=symbol, side="buy", quantity=100, id=1)


def _bars(open_price: float = 45.0, ref_close: float = 46.0):
    return {
        (CODE, REF_DAY): _bar(ref_close, close=ref_close),
        (CODE, FILL_DAY): _bar(open_price, close=45.5),
    }


def _plan(*, exit_config: Dict[str, Any], positions=None, trades=None,
          batch=None, bars=None, cash: str = "1000") -> Any:
    service = _Harness(bars=_bars() if bars is None else bars)
    return service._build_plan(
        batch=batch or {},
        fill_date=FILL_DAY,
        reference_date=REF_DAY,
        cash=Decimal(cash),
        positions=positions if positions is not None else {
            CODE: {"quantity": 100, "market_value": 4897.0, "last_price": 48.97}
        },
        per_name=Decimal("5000"),
        max_names=10,
        min_score=60,
        warnings=[],
        trades=trades if trades is not None else [_trade()],
        exit_config=exit_config,
        carry_days={},
        allowed_boards=("main",),
        slippage_bps=0.0,
        max_adv_pct=0.0,
        adv_window_days=20,
    )


# ----------------------------------------------------------------------
# 策略档解析
# ----------------------------------------------------------------------
def test_policy_reads_positive_max_holding_days():
    policy = pt.resolve_policy({"exit": {"max_holding_days": 5}})
    assert policy.max_holding_days == 5
    assert policy.as_dict()["max_holding_days"] == 5


def test_policy_ignores_invalid_max_holding_days():
    warnings: List[str] = []
    policy = pt.resolve_policy({"exit": {"max_holding_days": 0}}, warnings=warnings)
    assert policy.max_holding_days is None
    assert any("max_holding_days" in message for message in warnings)


def test_empty_profile_keeps_time_exit_off():
    policy = pt.resolve_policy({})
    assert policy.max_holding_days is None


# ----------------------------------------------------------------------
# 计划层
# ----------------------------------------------------------------------
def test_time_exit_sells_at_open_when_due():
    items, orders, cash_after = _plan(exit_config={"max_holding_days": 3})
    assert len(orders) == 1
    order = orders[0]
    assert order["side"] == "sell"
    assert order["quantity"] == 100
    assert order["price"] == 45.0
    assert "[time_exit]" in order["note"]
    row = items[0]
    assert row["action"] == "time_exit"
    assert "持有满 3 个交易日" in row["reason"]
    assert cash_after > Decimal("1000")  # 卖出回款


def test_time_exit_waits_until_due():
    _, orders, _ = _plan(exit_config={"max_holding_days": 4})
    assert orders == []


def test_time_exit_off_by_default():
    _, orders, _ = _plan(exit_config={})
    assert orders == []


def test_time_exit_does_not_apply_on_buy_day():
    _, orders, _ = _plan(
        exit_config={"max_holding_days": 1},
        trades=[_trade(buy_day=FILL_DAY)],
    )
    assert orders == []


def test_signal_sell_wins_over_time_exit():
    batch = {CODE: {"id": 458, "stock_code": CODE, "action": "sell", "score": 28}}
    items, orders, _ = _plan(exit_config={"max_holding_days": 3}, batch=batch)
    assert len(orders) == 1
    assert orders[0]["side"] == "sell"
    assert "time_exit" not in orders[0]["note"]
    assert not any(row["action"] == "time_exit" for row in items)


def test_time_exit_skips_when_open_limit_down():
    bars = {
        (CODE, REF_DAY): _bar(50.0, close=50.0),
        (CODE, FILL_DAY): _bar(44.0, close=44.0),
    }
    items, orders, _ = _plan(exit_config={"max_holding_days": 3}, bars=bars)
    assert orders == []
    assert any("跌停" in (row["reason"] or "") for row in items)


def test_order_note_tags_time_exit():
    note = pt.PaperTradingService._order_note(
        {"signal_id": 165, "action": "time_exit"}
    )
    assert note == "paper trading: signal #165 [time_exit]"


def test_performance_exit_reason_recognizes_time_exit():
    """卖出复盘必须把 -time_exit 归到「持有到期」，不能按价格贴成止盈/止损。"""
    from src.services.portfolio_performance_service import PortfolioPerformanceService

    reason = PortfolioPerformanceService._classify_exit(
        uid="paper-2026-11-23-600760-sell-time_exit",
        signal={"action": "buy", "stop_loss": 46.82, "target_price": 51.17},
        price=45.2,
        has_signal_ref=True,
    )
    assert reason == "time_exit"
