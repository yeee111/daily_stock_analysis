# -*- coding: utf-8 -*-
"""多账户 Phase 1（git 分支式纸面账户）的服务层与触发层测试。

覆盖团队负责人给的验收组：
- A 共享 batch 只取一次信号（含 2 天补跑，旧实现在这里是 3 次全量扫描）
- B horizons 并集取数 + 每账户只看自己的信号
- C 账户隔离（A 成交不动 B 的现金/持仓；A 异常不阻断 B 且被记录）
- D 幂等（同账户同成交日重跑不新增成交；不同账户可各自同 trade_uid 成交）
- H 总开关（PAPER_TRADING_AUTO_RUN=false 时不取信号、不写库、返回 None）

本文件全部走 mock：不建库、不联网、不读 .env。真实 SQLite 的账务与克隆
校验在 tests/test_paper_trading_clone.py。
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

import pytest

from src.services import paper_trading_service as pt
from src.services.portfolio_service import PortfolioConflictError

# ----------------------------------------------------------------------
# 固定交易日历（禁止联网）
# ----------------------------------------------------------------------
_SESSIONS = [
    date(2026, 10, 26),
    date(2026, 10, 27),
    date(2026, 10, 28),
    date(2026, 10, 29),
    date(2026, 10, 30),
    date(2026, 11, 2),
    date(2026, 11, 3),
]
_NEXT_TRADING_DAY = {day: _SESSIONS[index + 1] for index, day in enumerate(_SESSIONS[:-1])}

_REF_DATE = date(2026, 10, 29)  # fill=2026-10-30 的参考日
_FILL_DATE = date(2026, 10, 30)


def _fake_next_trading_day(_market: str, day: date) -> Optional[date]:
    return _NEXT_TRADING_DAY.get(day)


def _fake_trading_dates(_market: str, start: date, end: date) -> List[date]:
    return [day for day in _SESSIONS if start <= day <= end]


# 纸面交易的可调参数在断言里是常量，先钉死，避免被外部环境（真实 .env / 其他用例）影响。
_PINNED_ENV = {
    "PAPER_TRADING_AUTO_RUN": "false",
    "PAPER_TRADING_AUTO_APPLY": "false",
    "PAPER_TRADING_FETCH_MISSING_BARS": "false",
    "PAPER_TRADING_CORPORATE_ACTIONS": "false",
    "PAPER_TRADING_MIN_SCORE": "60",
    "PAPER_TRADING_MAX_NAMES": "5",
    "PAPER_TRADING_MAX_WEIGHT": "0.2",
    "PAPER_TRADING_INITIAL_CASH": "100000",
    "PAPER_TRADING_HORIZONS": "3d,5d",
    "PAPER_TRADING_RETRY_DAYS": "3",
    "PAPER_TRADING_CATCHUP_DAYS": "5",
    "PAPER_TRADING_SLIPPAGE_BPS": "0",
    "PAPER_TRADING_MAX_ADV_PCT": "0",
    "PAPER_TRADING_ACCOUNT_NAME": "纸面交易",
    # 其他测试模块会把仓库 .env 读进 os.environ（会把可用板块收窄），
    # 这里显式钉成默认口径，保证本文件的用例与运行顺序无关。
    "PAPER_TRADING_ALLOWED_BOARDS": "main,gem",
}


@pytest.fixture(autouse=True)
def fake_env_and_calendar():
    """每个用例一套固定 env + 固定交易日历，互不串味。"""
    with patch.dict(os.environ, _PINNED_ENV, clear=False), patch.object(
        pt, "get_next_trading_date", side_effect=_fake_next_trading_day
    ), patch.object(pt, "get_trading_dates", side_effect=_fake_trading_dates):
        yield


# ----------------------------------------------------------------------
# 测试替身：按账户分账的组合服务 + 只数的信号库
# ----------------------------------------------------------------------
class CountingSignals:
    """活跃信号仓储替身：记录分页调用，供 A 组断言「只取一次」。"""

    def __init__(self, items: Sequence[Dict[str, Any]]) -> None:
        self.items = [dict(item) for item in items]
        self.list_calls: List[Dict[str, Any]] = []

    def list_signals(self, *, market: str, status: str, page: int, page_size: int) -> Dict[str, Any]:
        self.list_calls.append({"market": market, "status": status, "page": page, "page_size": page_size})
        start = (page - 1) * page_size
        return {
            "items": self.items[start:start + page_size],
            "total": len(self.items),
            "page": page,
            "page_size": page_size,
        }

    def get_signal(self, signal_id: int) -> Dict[str, Any]:
        for item in self.items:
            if item.get("id") == signal_id:
                return item
        raise KeyError(signal_id)


class _AccountState:
    """一个账户的现金/持仓/成交，用来证明账户之间互不影响。"""

    def __init__(self, *, cash: float, positions: Optional[Dict[str, int]] = None) -> None:
        self.cash = float(cash)
        self.positions: Dict[str, int] = dict(positions or {})
        self.trades: List[Any] = []
        self.uids: set = set()

    def equity(self, price_of) -> float:
        market_value = sum(qty * price_of(code) for code, qty in self.positions.items())
        return self.cash + market_value


class FakeRepo:
    """极简仓储：只有成交/公司行动/分支清单，没有 portfolio_write_session。

    刻意不提供 ``portfolio_write_session`` 与 ``record_account_run``：前者让触发层
    的回填在 mock 下直接短路（不许碰库），后者让审计只走返回值路径。
    """

    def __init__(self, *, branches: Optional[List[Dict[str, Any]]] = None) -> None:
        self.branches = [dict(row) for row in (branches or [])]
        self.list_branch_calls: List[Dict[str, Any]] = []

    # 成交/公司行动：按账户隔离
    def list_trades(self, account_id: int, *, as_of: date) -> List[Any]:
        state = _STATES.get(int(account_id))
        if state is None:
            return []
        return [trade for trade in state.trades if trade.trade_date <= as_of]

    def list_corporate_actions(self, account_id: int, *, as_of: date) -> List[Any]:
        del account_id, as_of
        return []

    # 分支清单：语义与 PortfolioRepository.list_branch_accounts 对齐
    def list_branch_accounts(self, *, auto_run_only: bool = False, include_inactive: bool = False) -> List[Dict[str, Any]]:
        self.list_branch_calls.append({"auto_run_only": auto_run_only, "include_inactive": include_inactive})
        rows = []
        for row in self.branches:
            if auto_run_only and not row.get("auto_run"):
                continue
            if not include_inactive and row.get("account_is_active") is False:
                continue
            rows.append(dict(row))
        return rows


# 账户状态表：mock 仓储与 mock 组合服务共用（用例开始前由 _reset_states 重建）
_STATES: Dict[int, _AccountState] = {}


class FakePortfolio:
    """按账户分账的 PortfolioService 替身。"""

    def __init__(self, *, accounts: Sequence[Dict[str, Any]], branches: Optional[List[Dict[str, Any]]] = None) -> None:
        self.accounts = [dict(row) for row in accounts]
        self.repo = FakeRepo(branches=branches)
        self.recorded: List[Dict[str, Any]] = []

    def list_accounts(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        del include_inactive
        return [dict(row) for row in self.accounts]

    def get_portfolio_snapshot(
        self,
        *,
        account_id: Optional[int],
        as_of: date,
        include_realtime: bool = True,
        read_only: bool = False,
    ) -> Dict[str, Any]:
        del include_realtime, read_only
        state = _STATES[int(account_id)]
        positions = [
            {
                "symbol": code,
                "market": "cn",
                "quantity": float(qty),
                "market_value_base": float(qty * _price_of(code)),
                "last_price": float(_price_of(code)),
                "price_available": True,
            }
            for code, qty in sorted(state.positions.items())
            if qty > 0
        ]
        return {
            "accounts": [
                {
                    "total_cash": state.cash,
                    "total_equity": state.equity(_price_of),
                    "data_quality": "ok",
                    "positions": positions,
                }
            ]
        }

    def record_trade(self, **kwargs: Any) -> Dict[str, Any]:
        account_id = int(kwargs["account_id"])
        state = _STATES.get(account_id)
        if state is None:
            raise LookupError(f"no such account {account_id}")
        uid = str(kwargs.get("trade_uid") or "")
        if uid and uid in state.uids:
            # 账户级唯一：同账户重复 uid 冲突，不同账户同 uid 必须各自成功
            raise PortfolioConflictError(f"duplicate trade_uid {uid}")
        state.uids.add(uid)
        gross = float(kwargs["quantity"]) * float(kwargs["price"])
        fee = float(kwargs.get("fee") or 0.0)
        tax = float(kwargs.get("tax") or 0.0)
        if str(kwargs["side"]).lower() == "buy":
            state.cash -= gross + fee + tax
            state.positions[kwargs["symbol"]] = state.positions.get(kwargs["symbol"], 0) + int(kwargs["quantity"])
        else:
            state.cash += gross - fee - tax
            state.positions[kwargs["symbol"]] = state.positions.get(kwargs["symbol"], 0) - int(kwargs["quantity"])
        row = SimpleNamespace(
            id=len(self.recorded) + 1,
            account_id=account_id,
            trade_date=kwargs["trade_date"],
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            note=kwargs.get("note"),
            trade_uid=uid,
        )
        state.trades.append(row)
        self.recorded.append({**kwargs, "row": row})
        return {"id": row.id}


def _price_of(code: str) -> float:
    """固定估值价：只为让权益/市值可预测。"""
    return _PRICES.get(pt._plain_code(code), 10.0)


_PRICES: Dict[str, float] = {}


class SpyService(pt.PaperTradingService):
    """服务替身：记录取数与计划入参，其余全部走真实实现。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fetch_calls: List[Dict[str, Any]] = []
        self.plan_calls: List[Dict[str, Any]] = []

    def _latest_market_date(self) -> Optional[date]:
        return getattr(self, "latest_data_date", None)

    def _bar(self, code: str, day: date):
        return self._bars.get((pt._plain_code(code), day))

    def _fetch_active_signals(self, *, market: str, horizons: Any, warnings: List[str]):
        horizon_set = sorted(str(item) for item in horizons)
        self.fetch_calls.append({"market": market, "horizons": horizon_set})
        return super()._fetch_active_signals(market=market, horizons=horizons, warnings=warnings)

    def _build_plan(self, **kwargs: Any):
        self.plan_calls.append(
            {
                "codes": sorted(kwargs["batch"].keys()),
                "fill_date": kwargs["fill_date"],
                "reference_date": kwargs["reference_date"],
                "min_score": kwargs["min_score"],
                "max_names": kwargs["max_names"],
                "per_name": kwargs["per_name"],
            }
        )
        return super()._build_plan(**kwargs)


def _build_service(
    *,
    signals: Sequence[Dict[str, Any]],
    accounts: Sequence[Dict[str, Any]],
    branches: Optional[List[Dict[str, Any]]] = None,
    latest: Optional[date] = _FILL_DATE,
    bars: Optional[Dict[Any, Any]] = None,
    cash_by_account: Optional[Dict[int, float]] = None,
    positions_by_account: Optional[Dict[int, Dict[str, int]]] = None,
) -> SpyService:
    global _STATES
    _STATES = {}
    for account in accounts:
        account_id = int(account["id"])
        _STATES[account_id] = _AccountState(
            cash=(cash_by_account or {}).get(account_id, 100_000.0),
            positions=(positions_by_account or {}).get(account_id, {}),
        )
    portfolio = FakePortfolio(accounts=accounts, branches=branches)
    service = SpyService(
        portfolio_service=portfolio,
        signal_service=CountingSignals(signals),
        stock_repo=object(),
        dividend_fetcher=None,
    )
    service.latest_data_date = latest
    service._bars = dict(bars or {})
    return service


# ----------------------------------------------------------------------
# 数据构造小工具
# ----------------------------------------------------------------------
def _signal(
    signal_id: int,
    code: str,
    action: str = "buy",
    *,
    score: int = 80,
    horizon: str = "3d",
    created: str = "2026-10-29T15:00:00",
    stop: Optional[float] = 9.0,
    target: Optional[float] = 12.0,
) -> Dict[str, Any]:
    return {
        "id": signal_id,
        "stock_code": code,
        "stock_name": f"标的{code}",
        "action": action,
        "score": score,
        "horizon": horizon,
        "created_at": created,
        "trace_id": f"trace-{signal_id}",
        "stop_loss": stop,
        "target_price": target,
    }


def _bar(open_price: float, close_price: float) -> SimpleNamespace:
    return SimpleNamespace(
        open=open_price,
        close=close_price,
        high=close_price,
        low=open_price,
    )


def _account(account_id: int, name: str) -> Dict[str, Any]:
    return {"id": account_id, "name": name, "market": "cn", "base_currency": "CNY", "is_active": True}


def _branch(
    account_id: int,
    name: str,
    *,
    auto_run: bool = True,
    config: Optional[Dict[str, Any]] = None,
    profile_id: Optional[int] = 1,
    profile_name: str = "default",
    market: str = "cn",
) -> Dict[str, Any]:
    """与 PortfolioRepository._branch_view 输出同构的一行。"""
    return {
        "account_id": account_id,
        "account_name": name,
        "label": name,
        "market": market,
        "base_currency": "CNY",
        "account_is_active": True,
        "parent_account_id": None,
        "fork_date": date(2026, 10, 29),
        "auto_run": auto_run,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "profile_version": 1,
        "config": config if config is not None else {"entry": {}, "exit": {}, "risk": {}},
    }


def _flat_bars(codes: Sequence[str], *, ref_close: float = 10.0, fill_open: float = 10.0) -> Dict[Any, Any]:
    bars: Dict[Any, Any] = {}
    for code in codes:
        bars[(code, _REF_DATE)] = _bar(0.0, ref_close)
        bars[(code, _FILL_DATE)] = _bar(fill_open, fill_open)
    return bars


# ----------------------------------------------------------------------
# A 组：共享 batch 只取一次信号
# ----------------------------------------------------------------------
def test_two_accounts_share_one_signal_fetch():
    """A/B 两账户消费同一 batch：信号查询恰好一次。"""
    signals = [_signal(1, "600519"), _signal(2, "000001")]
    service = _build_service(
        signals=signals,
        accounts=[_account(11, "纸面主干"), _account(12, "克隆A")],
        branches=[_branch(11, "纸面主干"), _branch(12, "克隆A")],
        bars=_flat_bars(["600519", "000001"]),
    )

    result = service.run_all_accounts(dry_run=True)

    assert service.fetch_calls == [{"market": "cn", "horizons": ["3d", "5d"]}], "信号取数必须只发生一次"
    assert len(service.signals.list_calls) == 1
    assert result["status"] == "multi_planned"
    assert [row["account_id"] for row in result["accounts"]] == [11, 12]
    assert {row["status"] for row in result["accounts"]} == {pt.STATUS_PLANNED}
    # 同一批次：两个账户审计里的 batch_key 与汇总的一致
    assert {row["audit"]["batch_key"] for row in result["accounts"]} == {result["batch_key"]}
    assert result["batch"]["fill_date"] == _FILL_DATE.isoformat()


def test_catchup_two_days_still_fetches_signals_once():
    """带 2 天补跑：旧实现这里 1+2=3 次全量扫描，现在必须仍是 1 次。"""
    signals = [
        _signal(1, "600519", created="2026-10-28T15:00:00"),  # 成交日 10-29
        _signal(2, "000001", created="2026-10-29T15:00:00"),  # 成交日 10-30
        _signal(3, "600000", created="2026-10-30T15:00:00"),  # 成交日 11-02（本次主批次）
    ]
    bars = _flat_bars(["600519", "000001", "600000"])
    bars[("600519", date(2026, 10, 28))] = _bar(0.0, 10.0)
    bars[("600519", date(2026, 10, 29))] = _bar(10.0, 10.0)
    bars[("000001", date(2026, 10, 29))] = _bar(0.0, 10.0)
    bars[("000001", date(2026, 10, 30))] = _bar(10.0, 10.0)

    service = _build_service(
        signals=signals,
        accounts=[_account(11, "纸面主干"), _account(12, "克隆A")],
        branches=[_branch(11, "纸面主干"), _branch(12, "克隆A")],
        latest=date(2026, 11, 2),
        bars=bars,
    )

    result = service.run_all_accounts(dry_run=True)

    assert len(service.fetch_calls) == 1, f"补跑期间不得重复扫描信号：{service.fetch_calls}"
    assert len(service.signals.list_calls) == 1
    assert result["batch"]["fill_date"] == "2026-11-02"
    catchup_days = {tuple(row["audit"]["catchup_days"]) for row in result["accounts"]}
    assert catchup_days == {("2026-10-29", "2026-10-30")}, catchup_days
    # 补跑与主批次都走同一份快照：每个账户 3 次计划调用（2 个补跑日 + 1 个主成交日）
    per_account_plans = [row["audit"]["catchup_days"] for row in result["accounts"]]
    assert all(len(days) == 2 for days in per_account_plans)


def test_batch_is_immutable_and_does_not_leak_between_accounts():
    """批次不可变：A 账户的顺延/成交不得改变 B 账户看到的快照。"""
    signals = [_signal(1, "600519"), _signal(2, "000001")]
    service = _build_service(
        signals=signals,
        accounts=[_account(11, "纸面主干"), _account(12, "克隆A")],
        branches=[_branch(11, "纸面主干"), _branch(12, "克隆A")],
        bars=_flat_bars(["600519", "000001"]),
    )

    batch = service.build_signal_batch(market="cn", horizons_union=("3d", "5d"))
    signals_before = [dict(item) for item in batch.signals]
    by_fill_before = {
        day: {code: dict(signal) for code, signal in bucket.items()} for day, bucket in batch.by_fill.items()
    }

    first = service.run_for_account(batch, account_id=11, policy=pt.resolve_policy(None), dry_run=False)
    second = service.run_for_account(batch, account_id=12, policy=pt.resolve_policy(None), dry_run=False)

    assert [dict(item) for item in batch.signals] == signals_before, "快照里的信号不得被写路径改动"
    shape_after = {day: sorted(bucket) for day, bucket in batch.by_fill.items()}
    shape_before = {day: sorted(bucket) for day, bucket in by_fill_before.items()}
    assert shape_after == shape_before == {_FILL_DATE: ["000001", "600519"]}
    assert first["status"] == pt.STATUS_EXECUTED and second["status"] == pt.STATUS_EXECUTED
    assert [order["stock_code"] for order in first["orders"]] == [order["stock_code"] for order in second["orders"]]
    # 主批次仍是主批次（补跑用的 with_fill_date 不得回写它）
    assert batch.is_primary is True and batch.is_catchup is False


# ----------------------------------------------------------------------
# B 组：horizons 并集取数 + 每账户各自的切片
# ----------------------------------------------------------------------
def test_horizons_union_fetched_once_and_filtered_per_account():
    """A 配 3d/5d、B 配 5d/10d：取数用并集，各自计划只收到自己周期的标的。"""
    signals = [
        _signal(1, "600519", horizon="3d"),
        _signal(2, "000001", horizon="5d"),
        _signal(3, "600000", horizon="10d"),
    ]
    branch_a = _branch(11, "主干3d5d", config={"entry": {"horizons": ["3d", "5d"]}, "exit": {}, "risk": {}})
    branch_b = _branch(12, "分支5d10d", config={"entry": {"horizons": ["5d", "10d"]}, "exit": {}, "risk": {}})
    service = _build_service(
        signals=signals,
        accounts=[_account(11, "主干3d5d"), _account(12, "分支5d10d")],
        branches=[branch_a, branch_b],
        bars=_flat_bars(["600519", "000001", "600000"]),
    )

    result = service.run_all_accounts(dry_run=True)

    assert service.fetch_calls == [{"market": "cn", "horizons": ["10d", "3d", "5d"]}], "取数必须按并集且只一次"
    assert result["accounts"][0]["audit"]["horizons_union"] == ["10d", "3d", "5d"]
    assert result["accounts"][0]["audit"]["horizons"] == ["3d", "5d"]
    assert result["accounts"][1]["audit"]["horizons"] == ["10d", "5d"]

    # 每个账户的 _build_plan 只收到自己周期的标的
    plans_by_account = {}
    account_order = [11, 12]
    index = -1
    for call in service.plan_calls:
        if call["fill_date"] != _FILL_DATE:
            continue
        index += 1
        plans_by_account[account_order[index]] = call["codes"]
    assert plans_by_account[11] == ["000001", "600519"], plans_by_account
    assert plans_by_account[12] == ["000001", "600000"], plans_by_account

    # 审计里落库的实际读取集合与到期集合也要能区分
    assert result["accounts"][0]["audit"]["active_signal_ids"] == [1, 2]
    assert result["accounts"][1]["audit"]["active_signal_ids"] == [2, 3]


def test_empty_profile_config_falls_back_to_env_defaults():
    """默认档（三段全空）必须等价于现有 env/default 行为，不含任何写死的实盘参数。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干", config={"entry": {}, "exit": {}, "risk": {}})],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=True)
    policy = result["accounts"][0]["audit"]["policy"]

    assert policy["config"] == {"entry": {}, "exit": {}, "risk": {}}
    assert policy["horizons"] == ["3d", "5d"]
    assert policy["min_score"] == 60
    assert policy["max_names"] == 5
    assert policy["max_weight"] == 0.2
    assert policy["initial_cash"] == 100000.0
    assert policy["profile_name"] == "default"
    assert policy["stop_loss_pct"] is None and policy["take_profit_pct"] is None


def test_profile_overrides_only_affect_its_own_account():
    """策略档只作用于本账户：B 的 min_score/max_names 不影响 A 的计划。"""
    branch_a = _branch(11, "宽松档", config={"entry": {"min_score": 60, "max_names": 5}, "exit": {}, "risk": {}})
    branch_b = _branch(
        12,
        "收紧档",
        profile_id=2,
        profile_name="tight",
        config={"entry": {"min_score": 75, "max_names": 1}, "exit": {}, "risk": {}},
    )
    service = _build_service(
        signals=[_signal(1, "600519", score=80), _signal(2, "000001", score=70)],
        accounts=[_account(11, "宽松档"), _account(12, "收紧档")],
        branches=[branch_a, branch_b],
        bars=_flat_bars(["600519", "000001"]),
    )

    service.run_all_accounts(dry_run=True)

    # 主成交日的计划调用按账户顺序出现：A 先、B 后
    due_calls = [call for call in service.plan_calls if call["fill_date"] == _FILL_DATE]
    assert [call["min_score"] for call in due_calls] == [60, 75], due_calls
    assert [call["max_names"] for call in due_calls] == [5, 1], due_calls


def test_profile_exit_pct_is_resolved_and_validated():
    """策略档出场纪律：合法百分比生效；非法值只告警并回退信号口径。"""
    policy = pt.resolve_policy(
        {"entry": {}, "exit": {"stop_loss_pct": 0.05, "take_profit_pct": 0.2}, "risk": {}}
    )
    assert (policy.stop_loss_pct, policy.take_profit_pct) == (0.05, 0.2)
    assert policy.as_dict()["stop_loss_pct"] == 0.05
    assert policy.as_dict()["take_profit_pct"] == 0.2

    warnings: List[str] = []
    policy = pt.resolve_policy(
        {"entry": {}, "exit": {"stop_loss_pct": 1.5, "take_profit_pct": "abc"}, "risk": {}},
        warnings=warnings,
    )
    assert policy.stop_loss_pct is None and policy.take_profit_pct is None
    assert any("stop_loss_pct" in warning for warning in warnings)
    assert any("take_profit_pct" in warning for warning in warnings)

    warnings = []
    policy = pt.resolve_policy(
        {"entry": {}, "exit": {"trailing_stop_pct": 0.03}, "risk": {}},
        warnings=warnings,
    )
    assert policy.stop_loss_pct is None and policy.take_profit_pct is None
    assert any("trailing_stop_pct" in warning for warning in warnings)


# ----------------------------------------------------------------------
# C 组：账户隔离
# ----------------------------------------------------------------------
def test_account_a_fills_do_not_touch_account_b_state():
    """A 有信号、B 因自己的周期而无事可做：A 成交不得动 B 一分现金/一手持仓。"""
    _PRICES.update({"600519": 10.0, "000001": 10.0})
    branch_a = _branch(11, "主干3d", config={"entry": {"horizons": ["3d"]}, "exit": {}, "risk": {}})
    branch_b = _branch(12, "分支10d", config={"entry": {"horizons": ["10d"]}, "exit": {}, "risk": {}})
    service = _build_service(
        signals=[_signal(1, "600519", horizon="3d")],
        accounts=[_account(11, "主干3d"), _account(12, "分支10d")],
        branches=[branch_a, branch_b],
        bars=_flat_bars(["600519", "000001"]),
        cash_by_account={11: 100_000.0, 12: 80_000.0},
        positions_by_account={11: {}, 12: {"000001": 500}},
    )

    snapshot_b_before = service.portfolio.get_portfolio_snapshot(account_id=12, as_of=_FILL_DATE)
    result = service.run_all_accounts(dry_run=False)
    snapshot_b_after = service.portfolio.get_portfolio_snapshot(account_id=12, as_of=_FILL_DATE)

    by_id = {row["account_id"]: row for row in result["accounts"]}
    assert by_id[11]["status"] == pt.STATUS_EXECUTED
    assert by_id[12]["status"] == pt.STATUS_NO_ACTIVE_SIGNALS
    assert _STATES[11].positions.get("600519", 0) > 0
    assert _STATES[11].cash < 100_000.0
    assert snapshot_b_before == snapshot_b_after
    assert _STATES[12].cash == 80_000.0
    assert _STATES[12].positions == {"000001": 500}
    assert [row["account_id"] for row in service.portfolio.recorded] == [11]


def test_each_account_state_moves_only_by_its_own_orders():
    """两账户各自成交：现金/持仓的变化只能来自本账户的委托。"""
    _PRICES.update({"600519": 10.0, "000001": 10.0})
    branch_a = _branch(11, "主干3d", config={"entry": {"horizons": ["3d"]}, "exit": {}, "risk": {}})
    branch_b = _branch(12, "分支5d", config={"entry": {"horizons": ["5d"]}, "exit": {}, "risk": {}})
    service = _build_service(
        signals=[_signal(1, "600519", horizon="3d"), _signal(2, "000001", horizon="5d")],
        accounts=[_account(11, "主干3d"), _account(12, "分支5d")],
        branches=[branch_a, branch_b],
        bars=_flat_bars(["600519", "000001"]),
        cash_by_account={11: 100_000.0, 12: 80_000.0},
        positions_by_account={11: {}, 12: {}},
    )

    result = service.run_all_accounts(dry_run=False)

    assert [row["status"] for row in result["accounts"]] == [pt.STATUS_EXECUTED, pt.STATUS_EXECUTED]
    rows_by_account: Dict[int, List[Dict[str, Any]]] = {11: [], 12: []}
    for row in service.portfolio.recorded:
        rows_by_account[int(row["account_id"])].append(row)

    for account_id, expected_code, cash_before in ((11, "600519", 100_000.0), (12, "000001", 80_000.0)):
        rows = rows_by_account[account_id]
        assert rows and {row["symbol"] for row in rows} == {expected_code}
        spent = sum(
            float(row["quantity"]) * float(row["price"]) + float(row["fee"]) + float(row["tax"]) for row in rows
        )
        assert _STATES[account_id].cash == pytest.approx(cash_before - spent, abs=0.001)
        assert _STATES[account_id].positions == {expected_code: sum(int(row["quantity"]) for row in rows)}


def test_failing_account_does_not_block_others_and_is_recorded():
    """A 抛异常：B 照常完成，A 被标 account_failed 并带上失败原因。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干")],  # 999 不在账户表里 -> _resolve_account 抛错
        branches=[_branch(999, "不存在的账户"), _branch(11, "纸面主干")],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=False)

    by_id = {row["account_id"]: row for row in result["accounts"]}
    assert by_id[999]["status"] == pt.STATUS_ACCOUNT_FAILED
    assert "运行失败" in by_id[999]["message"] and "account_id=999 not found" in by_id[999]["message"]
    assert by_id[11]["status"] == pt.STATUS_EXECUTED
    assert _STATES[11].positions.get("600519", 0) > 0
    assert result["status"] == "multi_executed"
    # 执行态的失败审计只落库（paper_account_runs，见 test_paper_trading_clone.py），
    # 不随结果返回；干跑态不落库，审计才挂在 accounts[].audit 上。
    dry = service.run_all_accounts(dry_run=True)
    dry_by_id = {row["account_id"]: row for row in dry["accounts"]}
    assert dry_by_id[999]["status"] == pt.STATUS_ACCOUNT_FAILED
    assert dry_by_id[999]["audit"]["error"]
    assert dry_by_id[999]["audit"]["status"] == pt.STATUS_ACCOUNT_FAILED
    assert dry_by_id[999]["audit"]["policy"]["profile_name"] == "default"


def test_unparseable_profile_is_isolated_to_its_account():
    """坏策略档只影响本账户：其余账户继续跑。"""
    branch_bad = _branch(12, "坏档", config={"entry": {"max_names": 0}, "exit": {}, "risk": {}})
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干"), _account(12, "坏档")],
        branches=[_branch(11, "纸面主干"), branch_bad],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=True)

    by_id = {row["account_id"]: row for row in result["accounts"]}
    assert by_id[12]["status"] == pt.STATUS_ACCOUNT_FAILED
    assert by_id[12]["message"] == "策略档解析失败，本次未运行"
    assert by_id[11]["status"] == pt.STATUS_PLANNED
    assert any("策略档不可用" in warning for warning in result["warnings"]), result["warnings"]


def test_account_without_branch_row_is_not_run():
    """auto_run=0 的账户不参与自动跑批（克隆账户默认不自动下单）。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干"), _account(12, "手动克隆")],
        branches=[_branch(11, "纸面主干"), _branch(12, "手动克隆", auto_run=False)],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=True)

    assert [row["account_id"] for row in result["accounts"]] == [11]
    # 服务端要列出全部 branch 行，才能区分「没有 branch 行」（回退主干）与
    # 「branch 都在但 auto_run=0」（一笔都不跑）；筛选发生在应用层。
    assert service.portfolio.repo.list_branch_calls == [{"auto_run_only": False, "include_inactive": False}]
    assert _STATES[12].cash == 100_000.0


# ----------------------------------------------------------------------
# D 组：幂等
# ----------------------------------------------------------------------
def _seed_trade(
    account_id: int,
    code: str,
    *,
    day: date = _FILL_DATE,
    side: str = "buy",
    signal_id: int = 99,
) -> None:
    """预置一笔既有成交（模拟上一轮已下单），uid 按账户级唯一登记。"""
    state = _STATES[int(account_id)]
    uid = f"paper-{day.isoformat()}-{code}-{side}"
    state.uids.add(uid)
    state.trades.append(
        SimpleNamespace(
            id=len(state.trades) + 1,
            account_id=int(account_id),
            trade_date=day,
            symbol=code,
            side=side,
            note=f"paper trading: signal #{signal_id}",
            trade_uid=uid,
        )
    )


def test_same_day_fill_of_one_account_does_not_flag_the_other():
    """账户级幂等：只有当日已有成交的那个账户被拒，另一个照常下单。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干"), _account(12, "克隆A")],
        branches=[_branch(11, "纸面主干"), _branch(12, "克隆A")],
        bars=_flat_bars(["600519", "600000"]),
    )
    # 11 在成交日已有一笔别的标的的成交；12 干净
    _seed_trade(11, "600000")

    result = service.run_all_accounts(dry_run=False)

    by_id = {row["account_id"]: row for row in result["accounts"]}
    assert by_id[11]["status"] == pt.STATUS_DUPLICATE_BATCH
    assert "拒绝重复下单" in by_id[11]["message"]
    assert by_id[12]["status"] == pt.STATUS_EXECUTED
    assert [row["account_id"] for row in service.portfolio.recorded] == [12]


def test_force_rerun_is_idempotent_per_account():
    """force 重跑：同账户同 uid 的委托被判 duplicate，一行新的都不写。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干")],
        bars=_flat_bars(["600519"]),
    )

    first = service.run_all_accounts(dry_run=False)
    written_after_first = list(service.portfolio.recorded)
    assert first["accounts"][0]["status"] == pt.STATUS_EXECUTED

    second = service.run_all_accounts(dry_run=False)

    assert second["accounts"][0]["status"] in {pt.STATUS_DUPLICATE_BATCH, pt.STATUS_NOTHING_TO_DO}
    assert len(service.portfolio.recorded) == len(written_after_first)
    assert len(_STATES[11].trades) == len(written_after_first)
    assert _STATES[11].cash == pytest.approx(100_000.0 - sum(
        float(row["quantity"]) * float(row["price"]) + float(row["fee"]) + float(row["tax"])
        for row in written_after_first
    ), abs=0.001)
    # 批次键不变，所以「同批次重跑」始终可判定
    assert first["batch_key"] == second["batch_key"]


def test_two_accounts_may_fill_the_same_trade_uid():
    """trade_uid 是账户级唯一：两账户同 uid 必须各自成交成功，不互撞。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干"), _account(12, "克隆A")],
        branches=[_branch(11, "纸面主干"), _branch(12, "克隆A")],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=False)

    assert [row["status"] for row in result["accounts"]] == [pt.STATUS_EXECUTED, pt.STATUS_EXECUTED]
    assert [(row["account_id"], row["trade_uid"]) for row in service.portfolio.recorded] == [
        (11, f"paper-{_FILL_DATE.isoformat()}-600519-buy"),
        (12, f"paper-{_FILL_DATE.isoformat()}-600519-buy"),
    ]
    assert len(_STATES[11].trades) == 1 and len(_STATES[12].trades) == 1

    # 各自账户内再跑一次才会被幂等挡住
    again = service.run_all_accounts(dry_run=False)
    assert [row["status"] for row in again["accounts"]] == [pt.STATUS_NOTHING_TO_DO, pt.STATUS_NOTHING_TO_DO]
    assert len(service.portfolio.recorded) == 2


def test_trade_uid_conflict_is_scoped_to_one_account():
    """替身自身的账户级唯一约束：同账户重复 uid 冲突，跨账户同 uid 不冲突。"""
    _STATES.clear()
    _STATES[21] = _AccountState(cash=100_000.0)
    _STATES[22] = _AccountState(cash=100_000.0)
    portfolio = FakePortfolio(accounts=[_account(21, "甲"), _account(22, "乙")])
    kwargs = {
        "symbol": "600519",
        "trade_date": _FILL_DATE,
        "side": "buy",
        "quantity": 100.0,
        "price": 10.0,
        "fee": 5.0,
        "tax": 0.0,
        "trade_uid": "uid-shared",
        "note": "paper trading: signal #1",
    }

    portfolio.record_trade(account_id=21, **kwargs)
    portfolio.record_trade(account_id=22, **kwargs)
    with pytest.raises(PortfolioConflictError):
        portfolio.record_trade(account_id=21, **kwargs)

    assert len(_STATES[21].trades) == 1 and len(_STATES[22].trades) == 1


# ----------------------------------------------------------------------
# H 组：总开关与触发层
# ----------------------------------------------------------------------
def test_master_switch_off_fetches_nothing_and_returns_none():
    """PAPER_TRADING_AUTO_RUN=false：不建服务、不取信号、不写库，返回 None。"""
    with patch.object(pt, "PaperTradingService") as service_cls:
        assert pt.maybe_run_after_analysis(trigger="unit-test") is None
    assert service_cls.called is False


def test_master_switch_missing_is_treated_as_off():
    env = {key: value for key, value in _PINNED_ENV.items() if key != "PAPER_TRADING_AUTO_RUN"}
    with patch.dict(os.environ, env, clear=True), patch.object(pt, "PaperTradingService") as service_cls:
        assert pt.maybe_run_after_analysis(trigger="unit-test") is None
    assert service_cls.called is False


def _trigger_service(signals, branches, accounts, bars):
    return _build_service(
        signals=signals,
        accounts=accounts,
        branches=branches,
        bars=bars,
    )


def test_trigger_runs_all_auto_run_accounts_with_one_fetch():
    service = _trigger_service(
        [_signal(1, "600519"), _signal(2, "000001")],
        [_branch(11, "纸面主干"), _branch(12, "克隆A")],
        [_account(11, "纸面主干"), _account(12, "克隆A")],
        _flat_bars(["600519", "000001"]),
    )

    with patch.dict(os.environ, {"PAPER_TRADING_AUTO_RUN": "true", "PAPER_TRADING_AUTO_APPLY": "false"}), patch.object(
        pt, "PaperTradingService", return_value=service
    ):
        result = pt.maybe_run_after_analysis(trigger="unit-test")

    assert result is not None and result["status"] == "multi_planned"
    assert result["dry_run"] is True, "AUTO_APPLY=false 必须是干跑"
    assert [row["account_id"] for row in result["accounts"]] == [11, 12]
    assert len(service.fetch_calls) == 1
    # 干跑一个字节都不写：mock 仓储既无写会话也无审计写入
    assert service.portfolio.recorded == []
    assert not hasattr(service.portfolio.repo, "portfolio_write_session")


def test_trigger_with_auto_apply_executes_for_every_account():
    service = _trigger_service(
        [_signal(1, "600519")],
        [_branch(11, "纸面主干"), _branch(12, "克隆A")],
        [_account(11, "纸面主干"), _account(12, "克隆A")],
        _flat_bars(["600519"]),
    )

    with patch.dict(os.environ, {"PAPER_TRADING_AUTO_RUN": "true", "PAPER_TRADING_AUTO_APPLY": "true"}), patch.object(
        pt, "PaperTradingService", return_value=service
    ):
        result = pt.maybe_run_after_analysis(trigger="unit-test")

    assert result["dry_run"] is False
    assert [row["status"] for row in result["accounts"]] == [pt.STATUS_EXECUTED, pt.STATUS_EXECUTED]
    assert len(service.portfolio.recorded) == 2
    assert {row["account_id"] for row in service.portfolio.recorded} == {11, 12}


def test_trigger_never_raises_when_the_run_blows_up():
    """分析钩子不得因为纸面交易失败而抛异常：整体兜住并返回 None。"""
    service = _trigger_service([_signal(1, "600519")], [_branch(11, "纸面主干")], [_account(11, "纸面主干")], {})

    def _boom(*_args: Any, **_kwargs: Any):
        raise RuntimeError("日历挂了")

    with patch.dict(os.environ, {"PAPER_TRADING_AUTO_RUN": "true"}), patch.object(
        pt, "PaperTradingService", return_value=service
    ), patch.object(service, "build_signal_batch", side_effect=_boom):
        assert pt.maybe_run_after_analysis(trigger="unit-test") is None


def test_bootstrap_skips_fake_repo_without_write_session():
    """fake repo（无写会话）不得被回填触碰：这是 mock 层零写库的前提。"""
    service = _trigger_service([], [_branch(11, "纸面主干")], [_account(11, "纸面主干")], {})

    with patch.object(pt, "ensure_paper_branch_defaults") as bootstrap:
        assert service.run_all_accounts(dry_run=True) is not None
    assert bootstrap.called is False


# ----------------------------------------------------------------------
# 兼容：策略与出场位进审计（D5 约定：不落第四张表）
# ----------------------------------------------------------------------
def test_audit_records_signal_ids_and_exit_levels():
    service = _build_service(
        signals=[_signal(1, "600519", stop=9.0, target=12.0)],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干")],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=True)
    audit = result["accounts"][0]["audit"]

    assert audit["signal_ids"] == [1]
    assert audit["due_signal_ids"] == [1]
    assert audit["fill_date"] == _FILL_DATE.isoformat()
    assert audit["reference_date"] == _REF_DATE.isoformat()
    assert audit["policy"]["profile_name"] == "default"
    assert isinstance(audit["exit_levels"], dict)
    assert audit["policy"]["max_weight"] == 0.2
    # 干跑的审计随结果返回（不落库），委托明细也在里面
    assert [order["stock_code"] for order in audit["orders"]] == ["600519"]
    assert audit["dry_run"] is True


# ----------------------------------------------------------------------
# 修复回归：审计行先开后改 / 多账户 force / 批次展示只看到期桶
# ----------------------------------------------------------------------
class _AuditLedger:
    """只实现审计两方法的仓储替身：验证 running 行被原地改成终态。"""

    def __init__(self, *, keep_updates: bool = True) -> None:
        self.keep_updates = keep_updates
        self.rows: Dict[int, SimpleNamespace] = {}
        self.begin_statuses: List[str] = []
        self.update_calls: List[int] = []
        self._next_id = 1

    def record_account_run(
        self,
        *,
        account_id: int,
        batch_key: str,
        profile_id: Optional[int] = None,
        status: str,
        decisions: Optional[Dict[str, Any]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> SimpleNamespace:
        row = SimpleNamespace(
            id=self._next_id,
            account_id=int(account_id),
            batch_key=batch_key,
            profile_id=profile_id,
            status=status,
            decisions=decisions,
            orders=list(orders or []),
            error=error,
        )
        self.begin_statuses.append(status)
        self.rows[row.id] = row
        self._next_id += 1
        return row

    def update_account_run(
        self,
        *,
        run_id: int,
        status: str,
        decisions: Optional[Dict[str, Any]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> Optional[SimpleNamespace]:
        self.update_calls.append(int(run_id))
        row = self.rows.get(int(run_id))
        if row is None or not self.keep_updates:
            return None
        row.status = status
        row.decisions = decisions
        row.orders = list(orders or [])
        row.error = error
        return row


def _attach_audit_ledger(service: SpyService, *, keep_updates: bool = True) -> _AuditLedger:
    ledger = _AuditLedger(keep_updates=keep_updates)
    service.portfolio.repo.record_account_run = ledger.record_account_run
    service.portfolio.repo.update_account_run = ledger.update_account_run
    return ledger


def test_audit_row_opens_as_running_and_is_finalized_in_place():
    """执行成功后只有一行审计：先记 running，再原地改成 executed。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干")],
        bars=_flat_bars(["600519"]),
    )
    ledger = _attach_audit_ledger(service)

    result = service.run_all_accounts(dry_run=False)

    assert result["accounts"][0]["status"] == pt.STATUS_EXECUTED
    assert ledger.begin_statuses == [pt.STATUS_RUNNING]
    assert ledger.update_calls == [1]
    assert len(ledger.rows) == 1, "成功的一次尝试不得留下两行审计"
    row = ledger.rows[1]
    assert row.status == pt.STATUS_EXECUTED
    assert [order["stock_code"] for order in row.orders] == ["600519"]
    assert row.decisions["status"] == pt.STATUS_EXECUTED


def test_execution_failure_finalizes_the_open_row_once(monkeypatch):
    """成交阶段抛错：running 行被改成 account_failed，不再追加第二行。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干")],
        bars=_flat_bars(["600519"]),
    )
    ledger = _attach_audit_ledger(service)

    def _boom(**_kwargs: Any):
        raise RuntimeError("模拟执行中断")

    monkeypatch.setattr(service, "_execute_orders", _boom)
    result = service.run_all_accounts(dry_run=False)

    assert result["accounts"][0]["status"] == pt.STATUS_ACCOUNT_FAILED
    assert "模拟执行中断" in result["accounts"][0]["message"]
    assert len(ledger.rows) == 1, "同一尝试失败也只能有一行审计"
    row = ledger.rows[1]
    assert row.status == pt.STATUS_ACCOUNT_FAILED
    assert row.decisions["status"] == pt.STATUS_ACCOUNT_FAILED
    assert "模拟执行中断" in (row.error or "")


def test_multi_account_force_lifts_the_same_day_guard_per_account():
    """force 透传到每个账户：先被同日流水挡住，force 后成交，trade_uid 仍幂等。"""
    service = _build_service(
        signals=[_signal(1, "600519")],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干")],
        bars=_flat_bars(["600519", "600000"]),
    )
    _seed_trade(11, "600000")  # 同成交日已有另一标的的成交

    blocked = service.run_all_accounts(dry_run=False)
    assert blocked["accounts"][0]["status"] == pt.STATUS_DUPLICATE_BATCH
    assert service.portfolio.recorded == [], "未加 force 时一行都不能写"

    forced = service.run_all_accounts(dry_run=False, force=True)
    assert forced["accounts"][0]["status"] == pt.STATUS_EXECUTED
    assert [row["symbol"] for row in service.portfolio.recorded] == ["600519"]

    # force 只解除同日拦截，trade_uid 仍然兜底去重
    again = service.run_all_accounts(dry_run=False, force=True)
    assert again["accounts"][0]["status"] == pt.STATUS_NOTHING_TO_DO
    assert len(service.portfolio.recorded) == 1


def test_batch_display_lists_only_the_due_bucket():
    """批次展示只看本次到期桶：未来成交日的信号不得混进 signal_dates。"""
    service = _build_service(
        signals=[
            _signal(1, "600519", created="2026-10-29T15:00:00"),  # 成交日 10-30（到期）
            _signal(2, "000001", created="2026-10-31T15:00:00"),  # 成交日 11-02（未来）
        ],
        accounts=[_account(11, "纸面主干")],
        branches=[_branch(11, "纸面主干")],
        bars=_flat_bars(["600519"]),
    )

    result = service.run_all_accounts(dry_run=True)

    assert result["batch"]["fill_date"] == "2026-10-30"
    assert result["batch"]["signal_dates"] == ["2026-10-29"]
