# -*- coding: utf-8 -*-
"""多账户 Phase 1：克隆脚本与真实临时库验收测试。

覆盖团队负责人给的验收组：
- E 克隆校验（快照逐项相等 / 重复克隆幂等 / fork 后源账户变化不影响克隆 /
  fork 归一 / 未来 fork 与早于首笔活动被拒 / --initial-cash / 校验失败不留残余）
- C+D 的真实账务版本（两账户现金与持仓互不影响、同 trade_uid 跨账户不冲突、
  同账户同成交日重跑不新增 trades、account_failed 落审计表）
- G dry_run 零写入（paper_account_runs / portfolio_trades SQL 计数不变）
- 另外证明：三张新表由 create_all 自愈建表、ensure_paper_branch_defaults 幂等回填

全程使用临时 DATABASE_PATH，绝不碰 data/ 下的业务库，也不联网（信号与日历走本地）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.config import Config
from src.repositories.portfolio_repo import (
    PortfolioRepository,
    ensure_paper_branch_defaults,
)
from src.repositories.stock_repo import StockRepository
from src.services import paper_trading_service as pt
from src.services.portfolio_service import PortfolioService
from src.storage import (
    DatabaseManager,
    PaperStrategyProfile,
    PortfolioAccount,
    PortfolioTrade,
    empty_paper_policy_config,
)

ROOT = Path(__file__).resolve().parents[1]
CLONE_SCRIPT = ROOT / "scripts" / "paper_trading_clone.py"
LIVE_DB = ROOT / "data" / "stock_analysis.db"

# ----------------------------------------------------------------------
# 固定时间轴（都是真实 A 股交易日；11-07/11-08 是周末，用于验证归一）
# ----------------------------------------------------------------------
D_DEPOSIT = date(2026, 11, 2)
D_BUY_1 = date(2026, 11, 3)
D_BUY_2 = date(2026, 11, 4)
D_BUY_3 = date(2026, 11, 5)
D_SPLIT = date(2026, 11, 6)
D_SELL_1 = date(2026, 11, 9)
D_DIVIDEND = date(2026, 11, 10)
D_FORK = date(2026, 11, 10)  # 归一后仍是自己（交易日）
D_TRADE_AFTER = date(2026, 11, 11)  # fork 之后源账户新增的成交
D_LAST = date(2026, 11, 11)  # 源账户最后成交日
D_FORK_NEXT = date(2026, 11, 11)  # fork(11-10) 之后第一个交易日，--initial-cash 落在这天
D_NEXT_SESSION = date(2026, 11, 12)  # 再往后一个交易日，多账户跑批的成交日
D_NOT_A_SESSION = date(2026, 11, 8)  # 周日 -> 归一到 11-06
D_BEFORE_ALL = date(2026, 10, 30)  # 早于最早活动日

CODE_A = "600519"
CODE_B = "000001"
CODE_C = "600000"
CODE_LATE = "601668"
CODES = (CODE_A, CODE_B, CODE_C, CODE_LATE)
_CLOSES = {CODE_A: 100.0, CODE_B: 12.0, CODE_C: 20.0, CODE_LATE: 5.0}

SNAPSHOT_MONEY_KEYS = ("total_cash", "total_equity", "realized_pnl", "fee_total", "tax_total")
POSITION_KEYS = ("quantity", "avg_cost", "total_cost")

_PROXY_KEYS = (
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


# ----------------------------------------------------------------------
# 生产库未被写入的守卫：整个模块开始前拍指纹，结束后比对
# ----------------------------------------------------------------------
def _db_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    stat = path.stat()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0]): conn.execute(f"select count(*) from \"{row[0]}\"").fetchone()[0]  # noqa: S608
            for row in conn.execute("select name from sqlite_master where type='table' order by name")
        }
    finally:
        conn.close()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "tables": tables}


@pytest.fixture(scope="module", autouse=True)
def live_db_untouched():
    """本文件任何用例都不得写 data/ 下的业务库；写了就判失败。"""
    before = _db_fingerprint(LIVE_DB)
    yield
    after = _db_fingerprint(LIVE_DB)
    if before is None or after is None:
        assert before == after, f"业务库存在性发生变化：{LIVE_DB}"
        return
    watched = ("portfolio", "paper", "decision_signals")
    touched_before = {k: v for k, v in before["tables"].items() if k.startswith(watched)}
    touched_after = {k: v for k, v in after["tables"].items() if k.startswith(watched)}
    assert after["tables"] == before["tables"], (
        "业务库表行数被改动（说明有用例连到了真实库）：\n"
        f"before={touched_before}\nafter ={touched_after}"
    )
    new_tables = set(after["tables"]) - set(before["tables"])
    assert not new_tables, f"业务库被建出了新表：{sorted(new_tables)}"


@pytest.fixture(scope="module")
def clone_module():
    """按文件路径加载克隆脚本，导入前后还原代理环境变量（脚本会 pop）。"""
    saved = {key: os.environ.get(key) for key in _PROXY_KEYS}
    spec = importlib.util.spec_from_file_location("paper_trading_clone_under_test", CLONE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


# ----------------------------------------------------------------------
# 临时库 harness
# ----------------------------------------------------------------------
class Harness:
    """一个临时 SQLite 库 + 绑定的仓储/服务，附 SQL 计数工具。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db = DatabaseManager.get_instance()
        assert str(db_path.resolve()).lower() in str(self.db._db_url).lower().replace("/", "\\"), (
            f"数据库未绑定到临时库：{self.db._db_url} != {db_path}"
        )
        self.repo = PortfolioRepository(self.db)
        self.portfolio = PortfolioService(repo=self.repo)

    # -- SQL 计数 ----------------------------------------------------
    def _read(self, sql: str, params: Sequence[Any] = ()) -> List[Any]:
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def count(self, table: str, *, account_id: Optional[int] = None) -> int:
        if account_id is None:
            return int(self._read(f'select count(*) from "{table}"')[0][0])
        column = "id" if table == "portfolio_accounts" else "account_id"
        return int(self._read(f'select count(*) from "{table}" where {column}=?', (account_id,))[0][0])

    def table_names(self) -> List[str]:
        return [str(row[0]) for row in self._read("select name from sqlite_master where type='table' order by name")]

    def snapshot(self, account_id: int, as_of: date) -> Dict[str, Any]:
        payload = self.portfolio.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            include_realtime=False,
            read_only=True,
        )
        accounts = payload.get("accounts") or []
        assert accounts, f"账户 {account_id} 在 {as_of} 取不到快照"
        return accounts[0]

    # -- 账务写入 ----------------------------------------------------
    def create_paper_account(self, name: str, *, broker: str = "paper") -> int:
        account = self.portfolio.create_account(name=name, broker=broker, market="cn", base_currency="CNY")
        return int(account["id"])

    def deposit(self, account_id: int, *, on: date, amount: float, note: str = "初始资金") -> None:
        self.portfolio.record_cash_ledger(
            account_id=account_id, event_date=on, direction="in", amount=amount, currency="CNY", note=note
        )

    def trade(
        self,
        account_id: int,
        *,
        code: str,
        side: str,
        quantity: float,
        price: float,
        on: date,
        fee: float = 0.0,
        tax: float = 0.0,
        uid: Optional[str] = None,
        note: str = "paper trading: signal #7",
    ) -> int:
        created = self.portfolio.record_trade(
            account_id=account_id,
            symbol=code,
            trade_date=on,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            market="cn",
            currency="CNY",
            trade_uid=uid or f"seed-{on.isoformat()}-{code}-{side}",
            note=note,
        )
        return int(created["id"])

    def corporate_action(
        self,
        account_id: int,
        *,
        code: str,
        action_type: str,
        on: date,
        cash_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
    ) -> None:
        self.portfolio.record_corporate_action(
            account_id=account_id,
            symbol=code,
            effective_date=on,
            action_type=action_type,
            market="cn",
            currency="CNY",
            cash_dividend_per_share=cash_per_share,
            split_ratio=split_ratio,
            note=f"seed {action_type}",
        )

    def save_bar(self, code: str, on: date, *, close: float, open_price: Optional[float] = None) -> None:
        self.db.save_daily_data(
            pd.DataFrame(
                [
                    {
                        "date": on,
                        "open": close if open_price is None else open_price,
                        "high": close,
                        "low": close,
                        "close": close,
                        "volume": 1_000_000.0,
                        "amount": close * 1_000_000.0,
                        "pct_chg": 0.0,
                    }
                ]
            ),
            code=code,
            data_source="unit-test",
        )


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """临时 DATABASE_PATH + 纸面交易开关全部关掉网络相关分支。"""
    db_path = tmp_path / "paper_multiaccount.db"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "STOCK_LIST=600519",
                "GEMINI_API_KEY=test",
                "ADMIN_AUTH_ENABLED=false",
                f"DATABASE_PATH={db_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PAPER_TRADING_AUTO_RUN", "false")
    monkeypatch.setenv("PAPER_TRADING_AUTO_APPLY", "false")
    monkeypatch.setenv("PAPER_TRADING_FETCH_MISSING_BARS", "false")
    monkeypatch.setenv("PAPER_TRADING_CORPORATE_ACTIONS", "false")
    monkeypatch.setenv("PAPER_TRADING_ALLOWED_BOARDS", "main,gem")
    monkeypatch.setenv("PAPER_TRADING_HORIZONS", "3d,5d")
    monkeypatch.setenv("PAPER_TRADING_ACCOUNT_NAME", "纸面交易")
    # 克隆脚本在 import 时会 setup_env()，把仓库根 .env 读进 os.environ；这里把
    # 影响仓位计算的参数钉成默认口径，保证用例与运行顺序、外部 .env 无关。
    monkeypatch.setenv("PAPER_TRADING_MIN_SCORE", "60")
    monkeypatch.setenv("PAPER_TRADING_MAX_NAMES", "5")
    monkeypatch.setenv("PAPER_TRADING_MAX_WEIGHT", "0.2")
    monkeypatch.setenv("PAPER_TRADING_INITIAL_CASH", "100000")
    monkeypatch.setenv("PAPER_TRADING_RETRY_DAYS", "3")
    monkeypatch.setenv("PAPER_TRADING_CATCHUP_DAYS", "5")
    monkeypatch.setenv("PAPER_TRADING_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("PAPER_TRADING_MAX_ADV_PCT", "0")
    for key in _PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)

    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        yield Harness(db_path)
    finally:
        # 先放掉 SQLAlchemy 连接，Windows 上未释放的句柄会让临时目录删不掉
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _seed_history(h: Harness, *, name: str = "纸面交易") -> int:
    """造一个有买有卖、有分红有送转、fork 后还有新成交的源账户。"""
    for code in CODES:
        for day in (
            D_DEPOSIT,
            D_BUY_1,
            D_BUY_2,
            D_BUY_3,
            D_SPLIT,
            D_SELL_1,
            D_DIVIDEND,
            D_TRADE_AFTER,
            D_NEXT_SESSION,
        ):
            h.save_bar(code, day, close=_CLOSES[code])
    account_id = h.create_paper_account(name)
    h.deposit(account_id, on=D_DEPOSIT, amount=100_000.0)
    h.trade(account_id, code=CODE_A, side="buy", quantity=200, price=100.0, on=D_BUY_1, fee=5.6, tax=0.0)
    h.trade(account_id, code=CODE_B, side="buy", quantity=1000, price=12.0, on=D_BUY_2, fee=6.6, tax=0.0)
    h.trade(account_id, code=CODE_C, side="buy", quantity=500, price=20.0, on=D_BUY_3, fee=10.6, tax=0.0)
    h.corporate_action(account_id, code=CODE_C, action_type="split_adjustment", on=D_SPLIT, split_ratio=1.1)
    h.trade(account_id, code=CODE_A, side="sell", quantity=100, price=110.0, on=D_SELL_1, fee=6.1, tax=5.5)
    h.corporate_action(
        account_id, code=CODE_B, action_type="cash_dividend", on=D_DIVIDEND, cash_per_share=0.5
    )
    # fork 之后的一笔：克隆必须看不到
    h.trade(account_id, code=CODE_LATE, side="buy", quantity=1000, price=5.0, on=D_TRADE_AFTER, fee=5.5, tax=0.0)
    return account_id


def _do_clone(h: Harness, clone_module, **kwargs: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "repo": h.repo,
        "portfolio_service": h.portfolio,
        "name": "克隆验证",
        "requested_fork_date": D_FORK,
        "profile": {
            "id": kwargs.pop("profile_id", 1),
            "name": kwargs.pop("profile_name", "default"),
            "version": 1,
        },
        "apply": True,
        "printer": kwargs.pop("printer", lambda _line: None),
    }
    params.update(kwargs)
    return clone_module.clone_account(**params)


def _assert_snapshots_identical(source: Dict[str, Any], clone: Dict[str, Any]) -> List[str]:
    """测试侧独立复核：金额 round(.,6)+分位、持仓 round(.,8)+分位，逐项必须相等。"""
    diffs: List[str] = []
    for key in SNAPSHOT_MONEY_KEYS:
        a, b = float(source.get(key) or 0.0), float(clone.get(key) or 0.0)
        assert repr(a) == repr(b) or round(a, 6) == round(b, 6), f"{key} 不等：{a!r} vs {b!r}"
        assert pt.to_cent(a) == pt.to_cent(b), f"{key} 分位不等：{pt.to_cent(a)} vs {pt.to_cent(b)}"

    def _positions(payload: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        return {
            str(row["symbol"]): {key: float(row.get(key) or 0.0) for key in POSITION_KEYS}
            for row in (payload.get("positions") or [])
        }

    left, right = _positions(source), _positions(clone)
    assert set(left) == set(right), f"持仓集合不等：{sorted(left)} vs {sorted(right)}"
    for code in sorted(left):
        for key in POSITION_KEYS:
            a, b = left[code][key], right[code][key]
            assert round(a, 8) == round(b, 8), f"{code}.{key} 不等：{a!r} vs {b!r}"
            assert pt.to_cent(a) == pt.to_cent(b), f"{code}.{key} 分位不等：{a!r} vs {b!r}"
    return diffs


# ----------------------------------------------------------------------
# E 组：克隆校验
# ----------------------------------------------------------------------
def test_clone_snapshots_match_exactly(harness, clone_module):
    """克隆后快照逐项相等：5 个金额字段 + 每个持仓 3 个字段，金额/持仓双精度断言。"""
    source_id = _seed_history(harness)
    source_snapshot = harness.snapshot(source_id, D_FORK)

    report = _do_clone(harness, clone_module, source_account_id=source_id)
    clone_id = report["clone_account_id"]
    clone_snapshot = harness.snapshot(clone_id, D_FORK)

    assert report["status"] == "applied"
    assert report["counts"] == {"trades": 4, "cash": 1, "corporate_actions": 2}
    _assert_snapshots_identical(source_snapshot, clone_snapshot)
    # 快照本身不该是空的，否则「相等」没有意义
    assert clone_snapshot["total_equity"] > 0
    assert len(clone_snapshot["positions"]) == 3
    assert clone_snapshot["realized_pnl"] != 0
    assert clone_snapshot["fee_total"] > 0 and clone_snapshot["tax_total"] > 0
    # fork 之后的那笔（601668）不得被复制
    assert CODE_LATE not in {row["symbol"] for row in clone_snapshot["positions"]}
    assert harness.count("portfolio_trades", account_id=clone_id) == 4
    assert harness.count("portfolio_cash_ledger", account_id=clone_id) == 1
    assert harness.count("portfolio_corporate_actions", account_id=clone_id) == 2
    # 派生表不复制，由回放重算
    for table in clone_module.DERIVED_TABLES:
        assert harness.count(table, account_id=clone_id) == 0, f"{table} 不应被复制"
    # 克隆账户身份：auto_run=0、parent 指向源、fork_date 落库
    branch = harness.repo.get_branch(clone_id)
    assert branch is not None
    assert int(branch.parent_account_id) == source_id
    assert branch.fork_date == D_FORK
    assert bool(branch.auto_run) is False
    with harness.db.get_session() as session:
        account = session.query(PortfolioAccount).filter(PortfolioAccount.id == clone_id).one()
        assert account.broker == "paper" and account.market == "cn" and account.base_currency == "CNY"


def test_clone_copies_rows_in_source_id_order(harness, clone_module):
    """复制顺序必须按源 id 升序：同日多笔靠 id 决胜，乱序会让 fork 前曲线分叉。"""
    source_id = _seed_history(harness)
    # 同日（11-09）再压两笔，制造 id 决胜场景；它们的 id 晚于 fork 后那笔（11-11）
    harness.trade(source_id, code=CODE_B, side="buy", quantity=100, price=12.0, on=D_SELL_1, uid="same-day-1")
    harness.trade(source_id, code=CODE_C, side="sell", quantity=100, price=22.0, on=D_SELL_1, uid="same-day-2")

    report = _do_clone(harness, clone_module, source_account_id=source_id)
    clone_id = report["clone_account_id"]

    def _uid_and_day(account_id: int) -> List[Any]:
        return [
            (str(row[0]), date.fromisoformat(str(row[1])))
            for row in harness._read(
                "select trade_uid, trade_date from portfolio_trades where account_id=? order by id", (account_id,)
            )
        ]

    source_rows = _uid_and_day(source_id)
    expected = [uid for uid, day in source_rows if day <= D_FORK]
    assert [uid for uid, _day in _uid_and_day(clone_id)] == expected
    # fork 后那笔（id 在中间）被跳过，而 id 更晚的 fork 前两笔仍按 id 顺序留在末尾
    assert expected[-3:] == ["seed-2026-11-09-600519-sell", "same-day-1", "same-day-2"]
    _assert_snapshots_identical(harness.snapshot(source_id, D_FORK), harness.snapshot(clone_id, D_FORK))


def test_repeat_clone_is_idempotent(harness, clone_module):
    """同一 fork 点重复执行：不新建账户、不重复记账。"""
    source_id = _seed_history(harness)
    first = _do_clone(harness, clone_module, source_account_id=source_id)

    accounts_before = harness.count("portfolio_accounts")
    trades_before = harness.count("portfolio_trades")
    cash_before = harness.count("portfolio_cash_ledger")
    actions_before = harness.count("portfolio_corporate_actions")
    branches_before = harness.count("paper_account_branches")

    second = _do_clone(harness, clone_module, source_account_id=source_id)

    assert second["status"] == "exists"
    assert second["clone_account_id"] == first["clone_account_id"]
    assert harness.count("portfolio_accounts") == accounts_before
    assert harness.count("portfolio_trades") == trades_before
    assert harness.count("portfolio_cash_ledger") == cash_before
    assert harness.count("portfolio_corporate_actions") == actions_before
    assert harness.count("paper_account_branches") == branches_before


def test_same_name_different_fork_is_rejected(harness, clone_module):
    """重名但不是同一 fork 点：显式拒绝，不能悄悄建第二个。"""
    source_id = _seed_history(harness)
    _do_clone(harness, clone_module, source_account_id=source_id, name="已占用")

    with pytest.raises(clone_module.CloneError, match="已被"):
        _do_clone(
            harness,
            clone_module,
            source_account_id=source_id,
            name="已占用",
            requested_fork_date=D_SELL_1,
        )
    assert harness.count("portfolio_accounts") == 2


def test_clone_name_taken_by_unrelated_account_is_rejected(harness, clone_module):
    """同名账户已存在但没有 branch 行（不是本克隆）：拒绝，避免污染别的账户。"""
    source_id = _seed_history(harness)
    other_id = harness.create_paper_account("路人甲", broker="manual")

    with pytest.raises(clone_module.CloneError, match="无唯一约束|已被"):
        _do_clone(harness, clone_module, source_account_id=source_id, name="路人甲")
    assert harness.count("portfolio_trades", account_id=other_id) == 0


def test_non_paper_source_is_refused(harness, clone_module):
    source_id = _seed_history(harness, name="真实持仓")
    with harness.db.get_session() as session:
        row = session.query(PortfolioAccount).filter(PortfolioAccount.id == source_id).one()
        row.broker = "manual"
        session.commit()

    with pytest.raises(clone_module.CloneError, match="不是纸面账户"):
        _do_clone(harness, clone_module, source_account_id=source_id)


def test_source_trades_after_fork_do_not_move_clone(harness, clone_module):
    """fork 后源账户继续交易：克隆账户的快照与行数都不受影响。"""
    source_id = _seed_history(harness)
    report = _do_clone(harness, clone_module, source_account_id=source_id)
    clone_id = report["clone_account_id"]
    clone_before = harness.snapshot(clone_id, D_FORK)
    rows_before = (
        harness.count("portfolio_trades", account_id=clone_id),
        harness.count("portfolio_cash_ledger", account_id=clone_id),
        harness.count("portfolio_corporate_actions", account_id=clone_id),
    )

    harness.trade(source_id, code=CODE_B, side="sell", quantity=500, price=13.0, on=D_NEXT_SESSION, uid="after-1")
    harness.corporate_action(
        source_id, code=CODE_A, action_type="cash_dividend", on=D_NEXT_SESSION, cash_per_share=1.0
    )

    clone_after = harness.snapshot(clone_id, D_FORK)
    assert clone_after == clone_before
    assert (
        harness.count("portfolio_trades", account_id=clone_id),
        harness.count("portfolio_cash_ledger", account_id=clone_id),
        harness.count("portfolio_corporate_actions", account_id=clone_id),
    ) == rows_before
    # 源账户同一 as_of 的快照也一样（as_of=fork 之后事件被截断），但取到当前日就变了
    assert harness.snapshot(source_id, D_NEXT_SESSION) != clone_before


def test_fork_date_normalizes_to_last_session(harness, clone_module):
    """非交易日 fork（周日）归一到 <= 它的最后一个交易日，事件按归一值截断。"""
    source_id = _seed_history(harness)

    report = _do_clone(harness, clone_module, source_account_id=source_id, requested_fork_date=D_NOT_A_SESSION)

    assert report["fork_date"] == D_SPLIT  # 2026-11-06
    clone_id = report["clone_account_id"]
    assert harness.repo.get_branch(clone_id).fork_date == D_SPLIT
    # 11-09 的卖出与 11-10 的分红都在归一值之后 -> 不复制
    assert harness.count("portfolio_trades", account_id=clone_id) == 3
    assert harness.count("portfolio_corporate_actions", account_id=clone_id) == 1
    _assert_snapshots_identical(harness.snapshot(source_id, D_SPLIT), harness.snapshot(clone_id, D_SPLIT))


def test_future_fork_rejected_and_flag_allows_it(harness, clone_module):
    """默认拒绝晚于源账户最后成交日的 fork；--allow-future-fork 才放行。"""
    source_id = _seed_history(harness)
    too_late = D_NEXT_SESSION  # > 最后成交日 11-11

    with pytest.raises(clone_module.CloneError, match="晚于源账户最后成交日"):
        _do_clone(harness, clone_module, source_account_id=source_id, requested_fork_date=too_late)
    assert harness.count("portfolio_accounts") == 1

    report = _do_clone(
        harness,
        clone_module,
        source_account_id=source_id,
        requested_fork_date=too_late,
        allow_future_fork=True,
    )
    clone_id = report["clone_account_id"]
    assert report["fork_date"] == too_late
    assert harness.count("portfolio_trades", account_id=clone_id) == 5  # 含 11-11 那笔
    _assert_snapshots_identical(harness.snapshot(source_id, too_late), harness.snapshot(clone_id, too_late))


def test_fork_before_first_activity_rejected(harness, clone_module):
    source_id = _seed_history(harness)

    with pytest.raises(clone_module.CloneError, match="早于源账户最早活动日"):
        _do_clone(harness, clone_module, source_account_id=source_id, requested_fork_date=D_BEFORE_ALL)
    assert harness.count("portfolio_accounts") == 1


def test_empty_source_account_rejected(harness, clone_module):
    source_id = harness.create_paper_account("空账户")

    with pytest.raises(clone_module.CloneError, match="没有任何成交"):
        _do_clone(harness, clone_module, source_account_id=source_id)


def test_initial_cash_lands_on_next_session_without_breaking_validation(harness, clone_module):
    """--initial-cash 注入到 fork 后下一个交易日，fork 时点校验仍完全相等。"""
    source_id = _seed_history(harness)

    report = _do_clone(harness, clone_module, source_account_id=source_id, initial_cash=50_000.0)
    clone_id = report["clone_account_id"]

    assert report["initial_cash_date"] == D_FORK_NEXT  # fork 11-10 -> 下一个交易日 11-11
    assert harness.count("portfolio_cash_ledger", account_id=clone_id) == 2
    rows = harness._read(
        "select event_date, direction, amount from portfolio_cash_ledger where account_id=? order by id",
        (clone_id,),
    )
    assert rows[-1][0] == D_FORK_NEXT.isoformat()
    assert rows[-1][1] == "in"
    assert float(rows[-1][2]) == pytest.approx(50_000.0)
    # fork 时点快照：注入行在 as_of 之后，被截断，不影响校验
    _assert_snapshots_identical(harness.snapshot(source_id, D_FORK), harness.snapshot(clone_id, D_FORK))
    # 但按 fork 之后取值就能看到注入的现金
    assert harness.snapshot(clone_id, D_NEXT_SESSION)["total_cash"] == pytest.approx(
        harness.snapshot(source_id, D_NEXT_SESSION)["total_cash"] + 50_000.0 + 1_000 * 5.0 + 5.5, abs=1e-6
    )


def test_row_level_self_check_failure_leaves_nothing_behind(harness, clone_module, monkeypatch):
    """行级自检不通过 -> 整个事务回滚，连账户行都不留。"""
    source_id = _seed_history(harness)
    accounts_before = harness.count("portfolio_accounts")
    trades_before = harness.count("portfolio_trades")

    original_build = clone_module.build_clone_rows

    def _corrupt(events: Dict[str, List[Any]], account_id: int) -> List[Any]:
        rows = original_build(events, account_id)
        trade_row = next(row for row in rows if getattr(row, "price", None) is not None)
        trade_row.price = float(trade_row.price) + 0.01  # 篡改待写入行
        return rows

    monkeypatch.setattr(clone_module, "build_clone_rows", _corrupt)

    with pytest.raises(clone_module.CloneError, match="行级自检失败"):
        _do_clone(harness, clone_module, source_account_id=source_id)

    assert harness.count("portfolio_accounts") == accounts_before
    assert harness.count("portfolio_trades") == trades_before
    assert harness.count("paper_account_branches") == 0


def test_snapshot_validation_failure_compensates(harness, clone_module, monkeypatch):
    """提交后快照校验不通过 -> 补偿删除新账户全部自有行，账务回到原样。"""
    source_id = _seed_history(harness)
    baseline = {
        "accounts": harness.count("portfolio_accounts"),
        "trades": harness.count("portfolio_trades"),
        "cash": harness.count("portfolio_cash_ledger"),
        "actions": harness.count("portfolio_corporate_actions"),
        "branches": harness.count("paper_account_branches"),
    }

    def _broken_diff(_source: Dict[str, Any], _clone: Dict[str, Any]) -> List[str]:
        return ["total_cash 不一致：源 1.0 vs 克隆 2.0"]

    monkeypatch.setattr(clone_module, "snapshot_diff", _broken_diff)

    with pytest.raises(clone_module.CloneError, match="补偿删除"):
        _do_clone(harness, clone_module, source_account_id=source_id)

    assert harness.count("portfolio_accounts") == baseline["accounts"]
    assert harness.count("portfolio_trades") == baseline["trades"]
    assert harness.count("portfolio_cash_ledger") == baseline["cash"]
    assert harness.count("portfolio_corporate_actions") == baseline["actions"]
    assert harness.count("paper_account_branches") == baseline["branches"]


def test_clone_of_clone_keeps_fork_point_snapshot(harness, clone_module):
    """支持多分支：从克隆账户再 fork 一次，快照仍与源在同时点完全一致。"""
    source_id = _seed_history(harness)
    first = _do_clone(harness, clone_module, source_account_id=source_id, name="分支一")
    first_id = int(first["clone_account_id"])
    second = _do_clone(harness, clone_module, source_account_id=first_id, name="分支二", requested_fork_date=D_SPLIT)
    second_id = int(second["clone_account_id"])

    assert harness.count("portfolio_accounts") == 3
    origin = harness.snapshot(source_id, D_SPLIT)
    _assert_snapshots_identical(origin, harness.snapshot(first_id, D_SPLIT))
    _assert_snapshots_identical(origin, harness.snapshot(second_id, D_SPLIT))

    # 只有被克隆出来的账户才有 branch 行；克隆不会替主干补记录
    branches = harness.repo.list_branch_accounts()
    assert {row["account_id"] for row in branches} == {first_id, second_id}
    by_id = {row["account_id"]: row for row in branches}
    assert int(by_id[first_id]["parent_account_id"]) == source_id
    assert int(by_id[second_id]["parent_account_id"]) == first_id
    assert by_id[first_id]["fork_date"] == D_FORK
    assert by_id[second_id]["fork_date"] == D_SPLIT
    assert harness.repo.get_branch(source_id) is None


def test_branch_defaults_backfill_is_idempotent_and_profile_stays_empty(harness):
    """ensure_paper_branch_defaults：主干回填 auto_run=1，default 档三段全空，重复调用无副作用。"""
    source_id = _seed_history(harness)

    summary = ensure_paper_branch_defaults(repo=harness.repo)
    again = ensure_paper_branch_defaults(repo=harness.repo)

    assert summary["profile_created"] is True
    assert summary["branch_created"] == [source_id]
    assert again["profile_created"] is False
    assert again["branch_created"] == []
    assert again["branch_skipped"] == [source_id]
    assert harness.count("paper_strategy_profiles") == 1
    assert harness.count("paper_account_branches") == 1

    profile = harness.repo.get_profile_by_name_version("default")
    assert profile is not None
    assert profile.config == {"entry": {}, "exit": {}, "risk": {}} == empty_paper_policy_config()
    assert json.loads(profile.config_json) == empty_paper_policy_config()
    # 主干必须 auto_run=1，否则 18:05/19:00 会静默不再管理真实账户
    branch = harness.repo.get_branch(source_id)
    assert bool(branch.auto_run) is True
    assert branch.parent_account_id is None
    assert branch.fork_date == D_LAST  # 回填成最后成交日，不伪造分叉点


def test_malformed_profile_config_degrades_to_empty_sections(harness):
    """坏 JSON 的策略档不得让跑批崩掉。"""
    with harness.db.get_session() as session:
        session.add(PaperStrategyProfile(name="broken", version=1, config_json="{not json", is_active=True))
        session.commit()
        row = session.query(PaperStrategyProfile).filter(PaperStrategyProfile.name == "broken").one()
        row_id = int(row.id)

    profile = harness.repo.get_profile_by_id(row_id)
    assert profile.config == empty_paper_policy_config()
    assert pt.resolve_policy(profile.config).horizons == ("3d", "5d")


# ----------------------------------------------------------------------
# G 组 + C/D 的真实账务版本：多账户跑批
# ----------------------------------------------------------------------
class _StaticSignals:
    """信号仓储替身：只读，不碰库。"""

    def __init__(self, items: Sequence[Dict[str, Any]]) -> None:
        self.items = [dict(item) for item in items]
        self.calls = 0

    def list_signals(self, *, market: str, status: str, page: int, page_size: int) -> Dict[str, Any]:
        self.calls += 1
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


def _signal_row(signal_id: int, code: str, *, created: str, horizon: str = "3d", score: int = 80) -> Dict[str, Any]:
    return {
        "id": signal_id,
        "stock_code": code,
        "stock_name": f"标的{code}",
        "action": "buy",
        "score": score,
        "horizon": horizon,
        "created_at": created,
        "trace_id": f"trace-{signal_id}",
        "stop_loss": round(_CLOSES[code] * 0.9, 2),
        "target_price": round(_CLOSES[code] * 1.2, 2),
    }


def _multi_service(harness: Harness, signals: Sequence[Dict[str, Any]]) -> pt.PaperTradingService:
    return pt.PaperTradingService(
        portfolio_service=harness.portfolio,
        signal_service=_StaticSignals(signals),
        stock_repo=StockRepository(harness.db),
        dividend_fetcher=None,
    )


def _prepare_two_accounts(harness: Harness, clone_module) -> Dict[str, int]:
    """主干 + 一个克隆账户，并把克隆改成 auto_run=1（真实场景要人工开启）。"""
    source_id = _seed_history(harness)
    with harness.db.get_session() as session:
        account = session.query(PortfolioAccount).filter(PortfolioAccount.id == source_id).one()
        assert account.name == "纸面交易"
    clone_id = int(_do_clone(harness, clone_module, source_account_id=source_id)["clone_account_id"])
    harness.repo.upsert_branch(
        account_id=clone_id,
        fork_date=D_FORK,
        parent_account_id=source_id,
        profile_id=None,
        label="克隆验证",
        auto_run=True,
    )
    # 主干也要有 branch 行，才会被 run_all_accounts 选中
    ensure_paper_branch_defaults(repo=harness.repo)
    return {"source": source_id, "clone": clone_id}


def test_dry_run_writes_nothing_to_any_table(harness, clone_module):
    """G：干跑零写入 —— paper_account_runs / 账务三表行数一字不变。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    # 信号日 11-11 -> 成交日 11-12，与已插入的日线对齐
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    before = {
        "runs": harness.count("paper_account_runs"),
        "trades": harness.count("portfolio_trades"),
        "cash": harness.count("portfolio_cash_ledger"),
        "actions": harness.count("portfolio_corporate_actions"),
        "positions": harness.count("portfolio_positions"),
        "snapshots": harness.count("portfolio_daily_snapshots"),
    }

    result = service.run_all_accounts(dry_run=True)

    after = {
        "runs": harness.count("paper_account_runs"),
        "trades": harness.count("portfolio_trades"),
        "cash": harness.count("portfolio_cash_ledger"),
        "actions": harness.count("portfolio_corporate_actions"),
        "positions": harness.count("portfolio_positions"),
        "snapshots": harness.count("portfolio_daily_snapshots"),
    }
    assert after == before, f"干跑写库了：{before} -> {after}"
    assert harness.count("paper_account_runs") == 0
    assert {row["account_id"] for row in result["accounts"]} == {accounts["source"], accounts["clone"]}
    assert result["dry_run"] is True
    # 干跑的审计只在返回值里，内容应当与将要落库的一致
    assert all("audit" in row for row in result["accounts"])
    assert {row["audit"]["batch_key"] for row in result["accounts"]} == {result["batch_key"]}
    # 取数只发生一次（两个账户共用同一份快照）
    assert service.signals.calls == 1


def test_dry_run_without_branch_rows_writes_nothing(harness):
    """干跑在「还没有 branch/profile 行」时才最容易漏写：回填必须整体跳过。"""
    source_id = _seed_history(harness)
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    before = {
        "runs": harness.count("paper_account_runs"),
        "branches": harness.count("paper_account_branches"),
        "profiles": harness.count("paper_strategy_profiles"),
        "trades": harness.count("portfolio_trades"),
    }
    assert before["branches"] == 0 and before["profiles"] == 0

    result = service.run_all_accounts(dry_run=True)

    after = {
        "runs": harness.count("paper_account_runs"),
        "branches": harness.count("paper_account_branches"),
        "profiles": harness.count("paper_strategy_profiles"),
        "trades": harness.count("portfolio_trades"),
    }
    assert after == before, f"干跑回填了 branch/profile：{before} -> {after}"
    # 没有任何 branch 行 -> 按单账户主干跑，解析到的仍是真实账户（审计里的 account_id）
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["audit"]["account_id"] == source_id
    assert result["accounts"][0]["status"] == pt.STATUS_PLANNED
    assert result["accounts"][0]["orders"] == 1
    assert result["dry_run"] is True


def test_all_branches_paused_runs_nothing(harness, clone_module):
    """全部 auto_run=0 时不得回退到主干跑批：返回 multi_no_accounts 且零成交。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    for account_id in accounts.values():
        branch = harness.repo.get_branch(account_id)
        assert branch is not None
        harness.repo.upsert_branch(
            account_id=account_id,
            fork_date=branch.fork_date,
            parent_account_id=branch.parent_account_id,
            profile_id=branch.profile_id,
            label=branch.label,
            auto_run=False,
        )
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    before_trades = {
        account_id: harness.count("portfolio_trades", account_id=account_id)
        for account_id in accounts.values()
    }
    result = service.run_all_accounts(dry_run=False)

    assert result["status"] == "multi_no_accounts"
    assert result["accounts"] == []
    assert harness.count("paper_account_runs") == 0
    for account_id, count in before_trades.items():
        assert harness.count("portfolio_trades", account_id=account_id) == count


def test_executed_run_persists_one_audit_row_per_account(harness, clone_module):
    """执行态：paper_account_runs 每账户一行，含 batch_key / signal_ids / 策略档。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    result = service.run_all_accounts(dry_run=False)

    assert harness.count("paper_account_runs") == 2
    rows = harness.repo.list_runs(accounts["source"], limit=10)
    assert len(rows) == 1
    assert rows[0].batch_key == result["batch_key"]
    assert rows[0].status in {pt.STATUS_EXECUTED, pt.STATUS_NOTHING_TO_DO}
    decisions = json.loads(rows[0].decisions_json)
    assert decisions["policy"]["profile_name"] == "default"
    assert decisions["signal_ids"] == [1]
    assert decisions["horizons_union"] == ["3d", "5d"]
    assert json.loads(rows[0].orders_json)
    # 执行态的 audit 不再随结果返回（避免与落库行重复）
    assert all("audit" not in row for row in result["accounts"])


def test_two_accounts_cash_and_positions_stay_independent(harness, clone_module):
    """C 的真实账务版：两个账户各自成交后现金/持仓互不影响。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    source_id, clone_id = accounts["source"], accounts["clone"]
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    before = {
        account_id: harness.snapshot(account_id, D_NEXT_SESSION) for account_id in (source_id, clone_id)
    }
    result = service.run_all_accounts(dry_run=False)
    after = {
        account_id: harness.snapshot(account_id, D_NEXT_SESSION) for account_id in (source_id, clone_id)
    }

    assert [row["status"] for row in result["accounts"]] == [pt.STATUS_EXECUTED, pt.STATUS_EXECUTED]
    # 主干原有 5 笔（含 fork 后自购的 601668），克隆只有 fork 前的 4 笔；各加本次 1 笔
    expected_trades = {source_id: 6, clone_id: 5}
    for account_id, expected in expected_trades.items():
        trades = harness.count("portfolio_trades", account_id=account_id)
        assert trades == expected, f"账户 {account_id} 成交数应为 {expected}，实得 {trades}"

    # 每个账户的现金变动必须只等于「本账户这一天的成交」花掉的钱，
    # 不接受任何来自另一个账户的资金迁入或迁出。
    for account_id in (source_id, clone_id):
        new_fills = harness._read(
            "select quantity, price, fee, tax from portfolio_trades where account_id=? and trade_date=?",
            (account_id, D_NEXT_SESSION.isoformat()),
        )
        assert len(new_fills) == 1, f"账户 {account_id} 本次应只有 1 笔新成交，实得 {len(new_fills)}"
        spent = sum(float(q) * float(p) + float(f) + float(t) for q, p, f, t in new_fills)
        delta_cash = float(after[account_id]["total_cash"]) - float(before[account_id]["total_cash"])
        assert delta_cash == pytest.approx(-spent, abs=1e-6), (
            f"账户 {account_id} 现金变动 {delta_cash} 与自身成交金额 {-spent} 不一致"
        )
        held = [row for row in after[account_id]["positions"] if row["symbol"] == CODE_A]
        assert held, "新买入的标的必须出现在持仓里"
        assert len(after[account_id]["positions"]) == len(before[account_id]["positions"]) + (
            0 if any(row["symbol"] == CODE_A for row in before[account_id]["positions"]) else 1
        )

    # 主干在 fork 之后自购的 601668 不属于克隆的历史：差额必须一直是那一笔钱，
    # 跑批不会让两个账户的现金或持仓互相串用。
    late_cost = 1000 * 5.0 + 5.5
    for payload in (before, after):
        gap = float(payload[clone_id]["total_cash"]) - float(payload[source_id]["total_cash"])
        assert gap == pytest.approx(late_cost, abs=1e-6), f"两账户现金差额漂移：{gap} vs {late_cost}"
    assert CODE_LATE in {row["symbol"] for row in after[source_id]["positions"]}
    assert CODE_LATE not in {row["symbol"] for row in after[clone_id]["positions"]}


def test_rerun_on_same_fill_date_adds_no_trades(harness, clone_module):
    """D 的真实库版：同账户同成交日重跑，portfolio_trades 一行都不增。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    service.run_all_accounts(dry_run=False)
    per_account = {account_id: harness.count("portfolio_trades", account_id=account_id) for account_id in accounts.values()}
    runs_before = harness.count("paper_account_runs")

    again = service.run_all_accounts(dry_run=False)

    for account_id, count in per_account.items():
        assert harness.count("portfolio_trades", account_id=account_id) == count
    assert harness.count("paper_account_runs") == runs_before + 2  # 重跑本身要留审计
    assert all(row["status"] != pt.STATUS_EXECUTED for row in again["accounts"]), again["accounts"]


def test_trade_uid_is_unique_per_account_only(harness):
    """D：UNIQUE(account_id, trade_uid) —— 跨账户同 uid 可写，同账户重复必须冲突。"""
    source_id = _seed_history(harness)
    other_id = harness.create_paper_account("另一账户")
    uid = "paper-2026-11-12-600519-buy"

    harness.trade(source_id, code=CODE_A, side="buy", quantity=100, price=100.0, on=D_NEXT_SESSION, uid=uid)
    harness.trade(other_id, code=CODE_A, side="buy", quantity=100, price=100.0, on=D_NEXT_SESSION, uid=uid)

    with pytest.raises(IntegrityError):
        with harness.repo.portfolio_write_session() as session:
            session.add(
                PortfolioTrade(
                    account_id=source_id,
                    trade_uid=uid,
                    symbol=CODE_A,
                    market="cn",
                    currency="CNY",
                    trade_date=D_NEXT_SESSION,
                    side="buy",
                    quantity=100.0,
                    price=100.0,
                    fee=0.0,
                    tax=0.0,
                    note="dup",
                )
            )
            session.flush()

    assert harness.count("portfolio_trades", account_id=source_id) == 6
    assert harness.count("portfolio_trades", account_id=other_id) == 1


def test_failed_account_is_audited_and_others_still_fill(harness, clone_module, monkeypatch):
    """C：账户级异常不阻断其他账户，失败状态与原因都落 paper_account_runs。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    source_id, clone_id = accounts["source"], accounts["clone"]
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    original = service._resolve_account

    def _flaky(*, account_id: Optional[int], account_name: str, dry_run: bool):
        if account_id == clone_id:
            raise RuntimeError("模拟账户级故障")
        return original(account_id=account_id, account_name=account_name, dry_run=dry_run)

    monkeypatch.setattr(service, "_resolve_account", _flaky)

    result = service.run_all_accounts(dry_run=False)

    by_id = {row["account_id"]: row for row in result["accounts"]}
    assert by_id[clone_id]["status"] == pt.STATUS_ACCOUNT_FAILED
    assert "模拟账户级故障" in by_id[clone_id]["message"]
    assert by_id[source_id]["status"] == pt.STATUS_EXECUTED
    assert harness.count("portfolio_trades", account_id=source_id) == 6
    assert harness.count("portfolio_trades", account_id=clone_id) == 4, "失败账户不得留下半笔成交"

    # 两个账户各一行审计，失败那行带 status=account_failed 与 error 原因
    assert harness.count("paper_account_runs") == 2
    audits: Dict[int, Any] = {}
    for account_id in (source_id, clone_id):
        runs = harness.repo.list_runs(account_id, limit=5)
        assert len(runs) == 1, f"账户 {account_id} 应只落 1 行审计，实得 {len(runs)}"
        audits[account_id] = runs[0]
    assert audits[clone_id].status == pt.STATUS_ACCOUNT_FAILED
    assert "模拟账户级故障" in str(audits[clone_id].error)
    assert audits[source_id].status == pt.STATUS_EXECUTED
    assert json.loads(audits[source_id].orders_json)
    assert json.loads(audits[clone_id].orders_json) == []
    # 失败那行的审计里保留了本次批次与信号上下文，便于事后复盘
    failed_decisions = json.loads(audits[clone_id].decisions_json)
    assert failed_decisions["batch_key"] == result["batch_key"]
    assert failed_decisions["signal_ids"] == [1]


def test_auto_run_zero_accounts_are_skipped_in_real_db(harness, clone_module):
    """克隆默认 auto_run=0：真实库里它不参与自动跑批。"""
    source_id = _seed_history(harness)
    clone_id = int(_do_clone(harness, clone_module, source_account_id=source_id)["clone_account_id"])
    ensure_paper_branch_defaults(repo=harness.repo)
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    result = service.run_all_accounts(dry_run=True)

    assert [row["account_id"] for row in result["accounts"]] == [source_id]
    assert harness.repo.get_branch(clone_id) is not None
    assert harness.count("portfolio_trades", account_id=clone_id) == 4


def test_tables_are_self_created_by_metadata(harness):
    """三张新表由 Base.metadata.create_all 自愈建表，且旧表结构未被破坏。"""
    names = set(harness.table_names())
    assert {"paper_strategy_profiles", "paper_account_branches", "paper_account_runs"} <= names
    assert {
        "portfolio_accounts",
        "portfolio_trades",
        "portfolio_cash_ledger",
        "portfolio_corporate_actions",
        "portfolio_positions",
        "portfolio_position_lots",
        "portfolio_daily_snapshots",
        "stock_daily",
    } <= names
    columns = {str(row[1]) for row in harness._read('pragma table_info("paper_account_runs")')}
    assert {"account_id", "batch_key", "profile_id", "status", "decisions_json", "orders_json", "created_at"} <= columns
    branch_columns = {str(row[1]) for row in harness._read('pragma table_info("paper_account_branches")')}
    assert {"account_id", "parent_account_id", "fork_date", "profile_id", "label", "auto_run"} <= branch_columns
    ddl = str(harness._read('select "sql" from sqlite_master where name="portfolio_trades" and type="table"')[0][0])
    compact = "".join(ddl.lower().split())
    assert "unique(account_id,trade_uid)" in compact, f"portfolio_trades 建表语句缺少账户级唯一约束：{ddl}"


# ----------------------------------------------------------------------
# 脚本入口（子进程）：dry-run 真的不写库，--apply 才写
# ----------------------------------------------------------------------
def _run_script(
    args: Sequence[str],
    db_path: Path,
    env_file: Path,
    *,
    stdin_text: Optional[str] = "yes\n",
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_PATH"] = str(db_path)
    env["ENV_FILE"] = str(env_file)
    env["PYTHONIOENCODING"] = "utf-8"
    for key in _PROXY_KEYS:
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, str(CLONE_SCRIPT), *args],
        cwd=str(ROOT),
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


@pytest.fixture()
def seeded_file_db(tmp_path, monkeypatch):
    """独立临时库文件里造好历史，供子进程 CLI 用例使用（环境变量随用例还原）。"""
    db_path = tmp_path / "live_like.db"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "STOCK_LIST=600519",
                "GEMINI_API_KEY=test",
                "ADMIN_AUTH_ENABLED=false",
                f"DATABASE_PATH={db_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PAPER_TRADING_AUTO_RUN", "false")
    for key in _PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        local = Harness(db_path)
        source_id = _seed_history(local)
        return {
            "db_path": db_path,
            "env_file": env_file,
            "source_id": source_id,
            "fingerprint": _db_fingerprint(db_path),
        }
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def test_cli_dry_run_leaves_target_db_untouched(seeded_file_db):
    seeded = seeded_file_db
    fingerprint_before = _db_fingerprint(seeded["db_path"])

    proc = _run_script(
        [
            "--source-account-id",
            str(seeded["source_id"]),
            "--name",
            "CLI干跑",
            "--fork-date",
            D_FORK.isoformat(),
        ],
        seeded["db_path"],
        seeded["env_file"],
    )

    fingerprint_after = _db_fingerprint(seeded["db_path"])
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "DRY-RUN" in proc.stdout
    assert "校验通过" in proc.stdout
    assert "真实库未写入任何行" in proc.stdout
    assert "已写入真实库" not in proc.stdout  # dry-run 不得报「已写入真实库」
    assert fingerprint_after["tables"] == fingerprint_before["tables"], (
        f"dry-run 改了目标库行数：\nbefore={fingerprint_before['tables']}\nafter={fingerprint_after['tables']}"
    )
    assert "paper_account_branches" in fingerprint_after["tables"]
    assert fingerprint_after["tables"]["paper_account_branches"] == 0
    assert fingerprint_after["size"] == fingerprint_before["size"]


def test_cli_apply_creates_the_clone_account(seeded_file_db):
    seeded = seeded_file_db
    before = _db_fingerprint(seeded["db_path"])

    proc = _run_script(
        [
            "--source-account-id",
            str(seeded["source_id"]),
            "--name",
            "CLI实建",
            "--fork-date",
            D_FORK.isoformat(),
            "--apply",
        ],
        seeded["db_path"],
        seeded["env_file"],
    )

    after = _db_fingerprint(seeded["db_path"])
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "APPLY" in proc.stdout and "校验通过" in proc.stdout
    assert "已写入真实库" in proc.stdout
    assert "非交互终端" in proc.stdout  # 管道 stdin 下不再索要输入
    assert after["tables"]["portfolio_accounts"] == before["tables"]["portfolio_accounts"] + 1
    assert after["tables"]["portfolio_trades"] == before["tables"]["portfolio_trades"] + 4
    assert after["tables"]["paper_account_branches"] == before["tables"]["paper_account_branches"] + 1

    # 再跑一次：幂等命中，行数不再变化
    again = _run_script(
        [
            "--source-account-id",
            str(seeded["source_id"]),
            "--name",
            "CLI实建",
            "--fork-date",
            D_FORK.isoformat(),
            "--apply",
        ],
        seeded["db_path"],
        seeded["env_file"],
    )
    assert again.returncode == 0, again.stdout
    assert "已存在同名克隆" in again.stdout
    assert _db_fingerprint(seeded["db_path"])["tables"] == after["tables"]


def test_cli_refuses_to_overwrite_an_existing_name(seeded_file_db):
    """同名克隆已存在但 fork 点不同：CLI 退出码 1，且一行都不写。"""
    seeded = seeded_file_db
    base = [
        "--source-account-id",
        str(seeded["source_id"]),
        "--name",
        "CLI实建",
        "--apply",
    ]
    first = _run_script([*base, "--fork-date", D_FORK.isoformat()], seeded["db_path"], seeded["env_file"])
    assert first.returncode == 0, first.stdout

    before = _db_fingerprint(seeded["db_path"])
    proc = _run_script([*base, "--fork-date", D_SPLIT.isoformat()], seeded["db_path"], seeded["env_file"])

    assert proc.returncode == 1, proc.stdout
    assert "已被" in proc.stdout and "请换一个 --name" in proc.stdout
    assert _db_fingerprint(seeded["db_path"])["tables"] == before["tables"]


def test_cli_apply_skips_confirmation_when_stdin_is_not_a_tty(seeded_file_db):
    """契约记录：非 TTY（管道）下 --apply 不再索要确认，即使输入不是 yes 也会写入。

    这是实现有意为之（打印「显式 --apply 视为已确认」），本用例把行为钉住，
    以免以后有人误以为管道里输入 no 就能拦住写入。
    """
    seeded = seeded_file_db
    before = _db_fingerprint(seeded["db_path"])

    proc = _run_script(
        [
            "--source-account-id",
            str(seeded["source_id"]),
            "--name",
            "CLI实建",
            "--fork-date",
            D_FORK.isoformat(),
            "--apply",
        ],
        seeded["db_path"],
        seeded["env_file"],
        stdin_text="no\n",
    )

    assert proc.returncode == 0, proc.stdout
    assert _db_fingerprint(seeded["db_path"])["tables"] != before["tables"]


def test_cli_reports_bad_fork_date_without_writing(seeded_file_db):
    seeded = seeded_file_db
    before = _db_fingerprint(seeded["db_path"])

    proc = _run_script(
        ["--source-account-id", str(seeded["source_id"]), "--name", "坏日期", "--fork-date", "2026-13-45"],
        seeded["db_path"],
        seeded["env_file"],
    )

    assert proc.returncode == 2
    assert "解析失败" in proc.stdout
    assert _db_fingerprint(seeded["db_path"])["tables"] == before["tables"]


def test_cli_requires_explicit_source_when_multiple_paper_accounts(seeded_file_db):
    seeded = seeded_file_db
    _run_script(
        [
            "--source-account-id",
            str(seeded["source_id"]),
            "--name",
            "第二个纸面账户",
            "--fork-date",
            D_FORK.isoformat(),
            "--apply",
        ],
        seeded["db_path"],
        seeded["env_file"],
    )

    proc = _run_script(
        ["--name", "自动发现失败", "--fork-date", D_FORK.isoformat()],
        seeded["db_path"],
        seeded["env_file"],
    )

    assert proc.returncode == 1
    assert "请用 --source-account-id 指定源账户" in proc.stdout
    assert "2 个" in proc.stdout


def test_branch_role_starts_control_and_upsert_never_undoes_promotion(harness, clone_module):
    """role 只存 live/control；shadow/retired 由 auto_run 派生；upsert 不覆盖已转正角色。"""
    source_id = _seed_history(harness)
    ensure_paper_branch_defaults(repo=harness.repo)
    clone = _do_clone(harness, clone_module, source_account_id=source_id)
    clone_id = int(clone["clone_account_id"])

    branches = {row["account_id"]: row for row in harness.repo.list_branch_accounts()}
    assert (branches[source_id]["role"], branches[source_id]["state"]) == ("live", "live")
    assert (branches[clone_id]["role"], branches[clone_id]["state"]) == ("control", "retired")

    fork_date = harness.repo.get_branch(clone_id).fork_date
    # 开启自动跑批 => 分支参赛（shadow）
    harness.repo.upsert_branch(
        account_id=clone_id, fork_date=fork_date, parent_account_id=source_id, auto_run=True
    )
    row = next(r for r in harness.repo.list_branch_accounts() if r["account_id"] == clone_id)
    assert (row["role"], row["state"]) == ("control", "shadow")

    # 转正
    harness.repo.upsert_branch(
        account_id=clone_id, fork_date=fork_date, parent_account_id=source_id, auto_run=True, role="live"
    )
    row = next(r for r in harness.repo.list_branch_accounts() if r["account_id"] == clone_id)
    assert (row["role"], row["state"]) == ("live", "live")

    # 之后的普通 upsert（不带 role）不得把 live 改回 control
    harness.repo.upsert_branch(
        account_id=clone_id, fork_date=fork_date, parent_account_id=source_id, auto_run=False
    )
    row = next(r for r in harness.repo.list_branch_accounts() if r["account_id"] == clone_id)
    assert row["role"] == "live" and row["state"] == "live"

    with pytest.raises(ValueError, match="role must be one of"):
        harness.repo.upsert_branch(
            account_id=clone_id, fork_date=fork_date, parent_account_id=source_id, role="shadow"
        )


def test_paper_branch_state_derives_four_states():
    from src.repositories.portfolio_repo import paper_branch_state

    assert paper_branch_state("live", True) == "live"
    assert paper_branch_state("live", False) == "live"
    assert paper_branch_state("control", True) == "shadow"
    assert paper_branch_state("control", False) == "retired"
    assert paper_branch_state(None, True) == "shadow"


def test_promote_branch_swaps_live_in_one_transaction(harness):
    """转正 = 目标 -> live，其余 live -> control；auto_run 不变，重复执行幂等。"""
    source_id = _seed_history(harness)
    ensure_paper_branch_defaults(repo=harness.repo)

    branch_a = int(
        harness.portfolio.create_account(
            name="分支A", broker="paper", market="cn", base_currency="CNY"
        )["id"]
    )
    harness.repo.upsert_branch(
        account_id=branch_a,
        fork_date=D_FORK,
        parent_account_id=source_id,
        auto_run=True,
        role="control",
    )

    result = harness.repo.promote_branch(branch_a)
    assert result == {
        "account_id": branch_a,
        "live_account_id": branch_a,
        "demoted_account_ids": [source_id],
    }
    rows = {row["account_id"]: row for row in harness.repo.list_branch_accounts()}
    assert rows[branch_a]["role"] == "live" and rows[branch_a]["state"] == "live"
    assert rows[source_id]["role"] == "control" and rows[source_id]["state"] == "shadow"
    # 降级只改治理角色，不改变是否继续跑批
    assert rows[source_id]["auto_run"] is True
    assert rows[branch_a]["auto_run"] is True
    assert sum(1 for row in rows.values() if row["role"] == "live") == 1

    # 幂等：再次转正同一分支不再产生降级
    again = harness.repo.promote_branch(branch_a)
    assert again["demoted_account_ids"] == []

    # 连续转正：旧的 live 被降级，仍只有一个 live
    branch_b = int(
        harness.portfolio.create_account(
            name="分支B", broker="paper", market="cn", base_currency="CNY"
        )["id"]
    )
    harness.repo.upsert_branch(
        account_id=branch_b,
        fork_date=D_FORK,
        parent_account_id=source_id,
        auto_run=False,
        role="control",
    )
    second = harness.repo.promote_branch(branch_b)
    assert second["demoted_account_ids"] == [branch_a]
    rows = {row["account_id"]: row for row in harness.repo.list_branch_accounts()}
    assert rows[branch_b]["role"] == "live"
    assert rows[branch_a]["role"] == "control"
    assert rows[branch_b]["auto_run"] is False  # 转正不改 auto_run
    assert sum(1 for row in rows.values() if row["role"] == "live") == 1

    with pytest.raises(ValueError, match="has no paper branch row"):
        harness.repo.promote_branch(999999)


def test_promote_branch_treats_invalid_role_trunk_as_live(harness):
    """老库遗留的非法 role 主干按 live 处理，转正 fork 时会被一起降级。"""
    source_id = _seed_history(harness)
    ensure_paper_branch_defaults(repo=harness.repo)
    fork_id = int(
        harness.portfolio.create_account(
            name="旧库分支", broker="paper", market="cn", base_currency="CNY"
        )["id"]
    )
    harness.repo.upsert_branch(
        account_id=fork_id,
        fork_date=D_FORK,
        parent_account_id=source_id,
        auto_run=True,
        role="control",
    )
    with harness.db.get_session() as session:
        session.execute(
            text("update paper_account_branches set role = 'legacy' where account_id = :aid"),
            {"aid": source_id},
        )
        session.commit()

    result = harness.repo.promote_branch(fork_id)
    assert result["demoted_account_ids"] == [source_id]
    rows = {row["account_id"]: row for row in harness.repo.list_branch_accounts()}
    assert rows[source_id]["role"] == "control"
    assert rows[fork_id]["role"] == "live"


# ----------------------------------------------------------------------
# 缺陷修复回归：克隆档与账户同事务、审计行原地收口、分支清单显式过滤
# ----------------------------------------------------------------------
def test_resolve_profile_defers_default_profile_creation(harness, clone_module):
    """老库缺 default 档时只返回「待创建」描述，解析本身不写库。"""
    assert harness.count("paper_strategy_profiles") == 0

    profile = clone_module.resolve_profile(harness.repo, profile_id=None, profile_name=None)

    assert profile["id"] is None
    assert profile["name"] == "default"
    assert profile["config"] == empty_paper_policy_config()
    assert harness.count("paper_strategy_profiles") == 0, "解析阶段不得留下孤立策略档"


def test_clone_creates_the_deferred_default_profile_in_the_same_transaction(harness, clone_module):
    """待创建的 default 档随克隆事务落库，branch 行指向它。"""
    source_id = _seed_history(harness)
    profile = clone_module.resolve_profile(harness.repo, profile_id=None, profile_name=None)

    report = _do_clone(harness, clone_module, source_account_id=source_id, profile=profile)
    clone_id = int(report["clone_account_id"])

    assert harness.count("paper_strategy_profiles") == 1
    branch = harness.repo.get_branch(clone_id)
    stored = harness.repo.get_profile_by_id(int(branch.profile_id))
    assert stored is not None
    assert stored.name == "default"
    assert json.loads(stored.config_json) == empty_paper_policy_config()


def test_failed_clone_leaves_no_orphan_profile(harness, clone_module, monkeypatch):
    """行级自检失败整体回滚：随事务创建的 default 档也必须一起消失。"""
    source_id = _seed_history(harness)
    profile = clone_module.resolve_profile(harness.repo, profile_id=None, profile_name=None)

    original_build = clone_module.build_clone_rows

    def _corrupt(events: Dict[str, List[Any]], account_id: int) -> List[Any]:
        rows = original_build(events, account_id)
        trade_row = next(row for row in rows if getattr(row, "price", None) is not None)
        trade_row.price = float(trade_row.price) + 0.01
        return rows

    monkeypatch.setattr(clone_module, "build_clone_rows", _corrupt)

    with pytest.raises(clone_module.CloneError, match="行级自检失败"):
        _do_clone(harness, clone_module, source_account_id=source_id, profile=profile)

    assert harness.count("paper_strategy_profiles") == 0
    assert harness.count("paper_account_branches") == 0
    assert harness.count("portfolio_accounts") == 1  # 只剩源账户


def test_update_account_run_finalizes_the_open_row_in_place(harness):
    """先开的 running 行被原地改成终态，行数不增；不存在的行返回 None。"""
    source_id = _seed_history(harness)
    opened = harness.repo.record_account_run(
        account_id=source_id,
        batch_key="cn-2026-11-12-abc123",
        status=pt.STATUS_RUNNING,
        decisions={"status": pt.STATUS_RUNNING},
        orders=[],
    )

    updated = harness.repo.update_account_run(
        run_id=int(opened.id),
        status=pt.STATUS_EXECUTED,
        decisions={"status": pt.STATUS_EXECUTED, "message": "执行完成"},
        orders=[{"stock_code": CODE_A, "status": "executed"}],
    )

    assert updated is not None and int(updated.id) == int(opened.id)
    rows = harness.repo.list_runs(source_id, limit=10)
    assert len(rows) == 1
    assert rows[0].status == pt.STATUS_EXECUTED
    assert json.loads(rows[0].decisions_json)["message"] == "执行完成"
    assert json.loads(rows[0].orders_json) == [{"stock_code": CODE_A, "status": "executed"}]
    assert harness.repo.update_account_run(run_id=999999, status=pt.STATUS_EXECUTED) is None


def test_execution_failure_keeps_one_audit_row_per_account(harness, clone_module, monkeypatch):
    """成交阶段崩溃：running 行被改成 account_failed，不追加第二行。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    service = _multi_service(harness, [_signal_row(1, CODE_A, created="2026-11-11T15:00:00")])

    def _boom(**_kwargs: Any):
        raise RuntimeError("模拟执行中断")

    monkeypatch.setattr(service, "_execute_orders", _boom)
    result = service.run_all_accounts(dry_run=False)

    assert harness.count("paper_account_runs") == len(accounts)
    assert all(row["status"] == pt.STATUS_ACCOUNT_FAILED for row in result["accounts"])
    rows = harness.repo.list_runs(accounts["source"], limit=10)
    assert len(rows) == 1
    assert rows[0].status == pt.STATUS_ACCOUNT_FAILED
    assert "模拟执行中断" in (rows[0].error or "")
    assert json.loads(rows[0].decisions_json)["status"] == pt.STATUS_ACCOUNT_FAILED
    assert json.loads(rows[0].orders_json) == []


def test_list_branch_accounts_applies_each_filter_explicitly(harness, clone_module):
    """auto_run_only / include_inactive 各自生效，join 字段齐全。"""
    accounts = _prepare_two_accounts(harness, clone_module)
    source_id, clone_id = accounts["source"], accounts["clone"]

    rows = harness.repo.list_branch_accounts(auto_run_only=True)
    assert {row["account_id"] for row in rows} == {source_id, clone_id}
    by_id = {row["account_id"]: row for row in rows}
    assert by_id[source_id]["account_name"] == "纸面交易"
    assert by_id[source_id]["profile_name"] == "default"
    assert by_id[clone_id]["role"] == "control"

    harness.repo.upsert_branch(
        account_id=clone_id,
        fork_date=D_FORK,
        parent_account_id=source_id,
        profile_id=None,
        label="克隆验证",
        auto_run=False,
    )
    assert {row["account_id"] for row in harness.repo.list_branch_accounts(auto_run_only=True)} == {source_id}
    assert {row["account_id"] for row in harness.repo.list_branch_accounts()} == {source_id, clone_id}

    harness.repo.deactivate_account(clone_id)
    assert {row["account_id"] for row in harness.repo.list_branch_accounts()} == {source_id}
    assert {
        row["account_id"] for row in harness.repo.list_branch_accounts(include_inactive=True)
    } == {source_id, clone_id}
