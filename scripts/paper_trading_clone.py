# -*- coding: utf-8 -*-
"""Clone one paper account into a new branch account at a fork date.

多账户 Phase 1 的建分叉工具：把源纸面账户在 ``--fork-date`` 时点的事件
（成交 / 现金流水 / 公司行动）原样复制到一个新账户，并写下 branch 行，使
新账户从这一天起走自己的策略档，历史与源账户逐字段一致。

安全口径：
- 默认 **dry-run**：先把真实库只读备份到临时库，在临时库里完成整条
  「建账户 + 复制 + 校验」链路并打印比对结果，真实库一个字节都不写。
- 只有显式 ``--apply`` 才对真实库写入，且写入前打印确认提示。
- 复制走一个事务（``portfolio_write_session``，BEGIN IMMEDIATE）：建账户、
  复制三表、写 branch 行一起提交，行级自检不通过就整体回滚。
- 提交后再做快照校验（源账户与克隆账户各跑一次
  ``get_portfolio_snapshot(as_of=fork_date)``）；不通过则补偿删除新账户的
  全部行并报错退出。
- 不复制派生表（positions / lots / daily_snapshots），它们由回放重算。
- 不使用 ``record_trade`` 重放：重放会触发卖出数量校验而拒写历史行。

Usage:
    python scripts/paper_trading_clone.py --name 纸面策略B --fork-date 2026-11-13
    python scripts/paper_trading_clone.py --source-account-id 2 --name 纸面策略B \\
        --fork-date 2026-11-10 --profile-name branch-a --initial-cash 50000 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env before deciding which database to back up: DATABASE_PATH is commonly
# configured there, and the dry-run backup, the --apply banner and the actual
# write must all point at the same file.
from src.config import setup_env  # noqa: E402

setup_env()

# 克隆是本地账务操作：清掉代理变量，httpx（经 litellm 导入）会把 NO_PROXY 里的
# "[::1]" 解析错，而这里根本不需要任何网络请求。
for _proxy_key in (
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
):
    os.environ.pop(_proxy_key, None)

from sqlalchemy import func  # noqa: E402

from src.core.trading_calendar import get_next_trading_date, get_trading_dates  # noqa: E402
from src.repositories.portfolio_repo import (  # noqa: E402
    DEFAULT_PAPER_PROFILE_NAME,
    DEFAULT_PAPER_PROFILE_VERSION,
    list_paper_accounts_in_session,
)
from src.services.paper_trading_service import to_cent  # noqa: E402
from src.storage import (  # noqa: E402
    PaperAccountBranch,
    PaperAccountRun,
    PaperStrategyProfile,
    PortfolioAccount,
    PortfolioCashLedger,
    PortfolioCorporateAction,
    PortfolioTrade,
    empty_paper_policy_config,
)

SOURCE_ENV_KEY = "DATABASE_PATH"
DEFAULT_DB = ROOT / "data" / "stock_analysis.db"
# 派生表一律不复制，回放会重算（与 _replay_account 的真相来源保持一致）。
DERIVED_TABLES = (
    "portfolio_positions",
    "portfolio_position_lots",
    "portfolio_daily_snapshots",
)
SNAPSHOT_MONEY_KEYS = ("total_cash", "total_equity", "realized_pnl", "fee_total", "tax_total")
POSITION_KEYS = ("quantity", "avg_cost", "total_cost")


class CloneError(Exception):
    """可预期的失败：打印原因并以非零码退出，不留下半个账户。"""


# ----------------------------------------------------------------------
# 环境 / 库
# ----------------------------------------------------------------------
def live_db_path() -> Path:
    raw = (os.environ.get(SOURCE_ENV_KEY) or "").strip()
    path = Path(raw) if raw else DEFAULT_DB
    return path if path.is_absolute() else (Path.cwd() / path)


def backup_to_temp() -> Tuple[str, Path]:
    """只读备份真实库到临时文件，返回 (临时目录, 临时库路径)。"""
    source = live_db_path()
    if not source.exists():
        raise CloneError(f"找不到数据库：{source}（先跑一次分析/纸面交易建库）")
    temp_dir = tempfile.mkdtemp(prefix="paper-clone-dryrun-")
    temp_db = Path(temp_dir) / "clone_dryrun.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(temp_db)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.environ[SOURCE_ENV_KEY] = str(temp_db)
    from src.config import Config
    from src.storage import DatabaseManager

    Config.reset_instance()
    DatabaseManager.reset_instance()
    return temp_dir, temp_db


# ----------------------------------------------------------------------
# 日期归一
# ----------------------------------------------------------------------
def normalize_fork_date(market: str, requested: date) -> Tuple[date, str]:
    """把 fork_date 归一到「<= 它的最后一个交易日」。

    交易日历不可用时不猜：保留原始值并在输出里说明，因为后续校验比较的是同一个
    as_of 口径（源账户与克隆账户都用归一后的日期回放），不会因为日历缺失而失真。
    """
    sessions = get_trading_dates(market, requested - timedelta(days=45), requested)
    if sessions is None or len(sessions) == 0:
        return requested, f"交易日历不可用，沿用原始日期 {requested.isoformat()}"
    last = sessions[-1]
    normalized = last.date() if hasattr(last, "date") else last
    if normalized == requested:
        return requested, f"{requested.isoformat()} 本身即交易日，无需归一"
    return normalized, f"{requested.isoformat()} -> {normalized.isoformat()}（<= 原始值的最后一个交易日）"


# ----------------------------------------------------------------------
# 读取 / 校验
# ----------------------------------------------------------------------
def read_source_account(repo: Any, account_id: int) -> Dict[str, Any]:
    """源账户元数据（取成普通 dict，离开 session 后也能安全读）。"""
    with repo.db.get_session() as session:
        row = repo.get_account_in_session(session=session, account_id=account_id, include_inactive=True)
        if row is None:
            raise CloneError(f"源账户不存在：account_id={account_id}")
        if str(row.broker or "").strip().lower() != "paper":
            raise CloneError(
                f"源账户 {row.name}(id={account_id}) 的 broker={row.broker!r}，不是纸面账户，拒绝克隆"
            )
        return {
            "id": int(row.id),
            "name": row.name,
            "owner_id": row.owner_id,
            "broker": row.broker,
            "market": str(row.market or "cn").strip().lower(),
            "base_currency": row.base_currency,
            "is_active": bool(row.is_active),
        }


def read_events(session: Any, account_id: int, fork_date: date) -> Dict[str, List[Dict[str, Any]]]:
    """截断口径与 ``_replay_account(as_of=fork_date)`` 同构。"""

    def _rows(model: Any, column: Any) -> List[Dict[str, Any]]:
        rows = session.execute(
            model.__table__.select()
            .where((model.account_id == account_id) & (column <= fork_date))
            .order_by(column.asc(), model.id.asc())
        ).mappings()
        return [dict(row) for row in rows]

    return {
        "trades": _rows(PortfolioTrade, PortfolioTrade.trade_date),
        "cash": _rows(PortfolioCashLedger, PortfolioCashLedger.event_date),
        "corporate_actions": _rows(PortfolioCorporateAction, PortfolioCorporateAction.effective_date),
    }


def read_name_taken(repo: Any, name: str) -> Optional[Any]:
    """账户名查重：portfolio_accounts.name 没有唯一约束，必须显式检查。"""
    with repo.db.get_session() as session:
        return session.execute(
            PortfolioAccount.__table__.select().where(func.lower(PortfolioAccount.name) == name.strip().lower())
        ).first()


def build_clone_rows(events: Dict[str, List[Any]], account_id: int) -> List[Any]:
    """裸 ORM 批量插入：保留 float 原值、trade_uid、日期与 created_at，不做二次舍入。"""
    rows: List[Any] = []
    for item in events["trades"]:
        rows.append(
            PortfolioTrade(
                account_id=account_id,
                trade_uid=item["trade_uid"],
                symbol=item["symbol"],
                market=item["market"],
                currency=item["currency"],
                trade_date=item["trade_date"],
                side=item["side"],
                quantity=item["quantity"],
                price=item["price"],
                fee=item["fee"],
                tax=item["tax"],
                note=item["note"],
                dedup_hash=item["dedup_hash"],
                created_at=item["created_at"],
            )
        )
    for item in events["cash"]:
        rows.append(
            PortfolioCashLedger(
                account_id=account_id,
                event_date=item["event_date"],
                direction=item["direction"],
                amount=item["amount"],
                currency=item["currency"],
                note=item["note"],
                created_at=item["created_at"],
            )
        )
    for item in events["corporate_actions"]:
        rows.append(
            PortfolioCorporateAction(
                account_id=account_id,
                symbol=item["symbol"],
                market=item["market"],
                currency=item["currency"],
                effective_date=item["effective_date"],
                action_type=item["action_type"],
                cash_dividend_per_share=item["cash_dividend_per_share"],
                split_ratio=item["split_ratio"],
                note=item["note"],
                created_at=item["created_at"],
            )
        )
    return rows


def row_level_diff(events: Dict[str, List[Any]], written: Sequence[Any]) -> List[str]:
    """提交前的行级自检：源行与刚写入的行必须逐字段相同。"""
    diffs: List[str] = []
    by_model: Dict[str, List[Any]] = {"trade": [], "cash": [], "action": []}
    for row in written:
        if isinstance(row, PortfolioTrade):
            by_model["trade"].append(row)
        elif isinstance(row, PortfolioCashLedger):
            by_model["cash"].append(row)
        elif isinstance(row, PortfolioCorporateAction):
            by_model["action"].append(row)

    def _compare(label: str, source: List[Any], copied: List[Any], fields: Tuple[str, ...]) -> None:
        if len(source) != len(copied):
            diffs.append(f"{label} 行数不一致：源 {len(source)} vs 克隆 {len(copied)}")
            return
        for index, (left, right) in enumerate(zip(source, copied)):
            for field in fields:
                a, b = left.get(field), getattr(right, field)
                if a is None and b is None:
                    continue
                if isinstance(a, float) or isinstance(b, float):
                    ok = (a is not None and b is not None) and float(a) == float(b)
                else:
                    ok = a == b
                if not ok:
                    diffs.append(f"{label}[{index}].{field} 不一致：源 {a!r} vs 克隆 {b!r}")

    _compare("trade", events["trades"], by_model["trade"], (
        "trade_uid", "symbol", "market", "currency", "trade_date", "side",
        "quantity", "price", "fee", "tax", "note", "dedup_hash",
    ))
    _compare("cash", events["cash"], by_model["cash"], (
        "event_date", "direction", "amount", "currency", "note",
    ))
    _compare("corporate_action", events["corporate_actions"], by_model["action"], (
        "symbol", "market", "currency", "effective_date", "action_type",
        "cash_dividend_per_share", "split_ratio", "note",
    ))
    return diffs


def read_snapshot(portfolio_service: Any, account_id: int, as_of: date) -> Dict[str, Any]:
    """只读回放：跳过实时行情与快照写表，两次读取口径完全由库内日线决定。"""
    snapshot = portfolio_service.get_portfolio_snapshot(
        account_id=account_id,
        as_of=as_of,
        include_realtime=False,
        read_only=True,
    )
    accounts = snapshot.get("accounts") or []
    if not accounts:
        raise CloneError(f"账户 {account_id} 在 {as_of} 取不到快照（账户不存在或已停用？）")
    return accounts[0]


def positions_map(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(row.get("symbol")): {key: row.get(key) for key in POSITION_KEYS}
        for row in (payload.get("positions") or [])
    }


def snapshot_diff(source: Dict[str, Any], clone: Dict[str, Any]) -> List[str]:
    """字段级 diff：money 位 round(.,6) 精确相等 + to_cent 二次断言，持仓位 round(.,8)。"""
    diffs: List[str] = []
    for key in SNAPSHOT_MONEY_KEYS:
        a = float(source.get(key) or 0.0)
        b = float(clone.get(key) or 0.0)
        if round(a, 6) != round(b, 6):
            diffs.append(f"{key} 不一致：源 {a!r} vs 克隆 {b!r}")
        elif to_cent(a) != to_cent(b):
            diffs.append(f"{key} 分位断言失败：源 {to_cent(a)} vs 克隆 {to_cent(b)}")
    left, right = positions_map(source), positions_map(clone)
    for code in sorted(set(left) - set(right)):
        diffs.append(f"持仓缺失：源有 {code}，克隆没有")
    for code in sorted(set(right) - set(left)):
        diffs.append(f"持仓多余：克隆有 {code}，源没有")
    for code in sorted(set(left) & set(right)):
        for field in POSITION_KEYS:
            a = left[code].get(field)
            b = right[code].get(field)
            if a is None and b is None:
                continue
            if a is None or b is None or round(float(a), 8) != round(float(b), 8):
                diffs.append(f"持仓 {code}.{field} 不一致：源 {a!r} vs 克隆 {b!r}")
            elif to_cent(float(a)) != to_cent(float(b)):
                diffs.append(f"持仓 {code}.{field} 分位断言失败：源 {to_cent(float(a))} vs 克隆 {to_cent(float(b))}")
    return diffs


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def resolve_profile(repo: Any, *, profile_id: Optional[int], profile_name: Optional[str]) -> Dict[str, Any]:
    if profile_id is not None and profile_name:
        raise CloneError("--profile-id 与 --profile-name 只能二选一")
    if profile_id is not None:
        row = repo.get_profile_by_id(int(profile_id))
        if row is None:
            raise CloneError(f"策略档 id={profile_id} 不存在")
        return {"id": int(row.id), "name": row.name, "version": int(row.version)}
    if profile_name:
        row = repo.get_profile_by_name_version(profile_name)
        if row is None:
            raise CloneError(f"策略档 name={profile_name} 不存在（先确认 --profile-name 拼写）")
        return {"id": int(row.id), "name": row.name, "version": int(row.version)}
    # 缺省用默认档。老库还没有这一行时只返回「待创建」描述：真正的写入
    # 与账户、事件行、branch 行放在同一个事务里，克隆失败不会残留孤立档。
    row = repo.get_profile_by_name_version(DEFAULT_PAPER_PROFILE_NAME)
    if row is not None:
        return {"id": int(row.id), "name": row.name, "version": int(row.version)}
    return {
        "id": None,
        "name": DEFAULT_PAPER_PROFILE_NAME,
        "version": int(DEFAULT_PAPER_PROFILE_VERSION),
        "config": empty_paper_policy_config(),
    }


def clone_account(
    *,
    repo: Any,
    portfolio_service: Any,
    source_account_id: int,
    name: str,
    requested_fork_date: date,
    profile: Dict[str, Any],
    initial_cash: Optional[float] = None,
    apply: bool = False,
    allow_future_fork: bool = False,
    printer: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Build one cloned paper account; returns the comparison report.

    注意 ``apply`` 只决定输出与返回状态：本函数写入的是 ``repo`` 当前指向的库。
    真正的 dry-run 隔离由 :func:`main` 先把真实库只读备份到临时副本、再把
    ``DATABASE_PATH`` 指过去完成 —— 因此直接调用本函数做验证时，务必确认
    repo 指向的是临时库。
    """
    if not str(name).strip():
        raise CloneError("--name 不能为空")
    if initial_cash is not None and float(initial_cash) <= 0:
        raise CloneError("--initial-cash 必须 > 0")

    source = read_source_account(repo, source_account_id)
    market = source["market"]
    fork_date, fork_note = normalize_fork_date(market, requested_fork_date)

    existing = read_name_taken(repo, name)
    with repo.db.get_session() as session:
        last_trade = repo.get_last_trade_date_in_session(session=session, account_id=source_account_id)
        first_activity = repo.get_first_activity_date_in_session(
            session=session,
            account_id=source_account_id,
            as_of=date.today(),
        )
        existing_branch = (
            repo.get_branch_in_session(session=session, account_id=int(existing.id)) if existing is not None else None
        )

    if last_trade is None and first_activity is None:
        raise CloneError(f"源账户 {source['name']}(id={source_account_id}) 没有任何成交/流水，没有可克隆的历史")

    # 幂等：同一 (name, parent, fork_date) 已经存在 -> 报已存在，退出码 0
    if existing is not None and existing_branch is not None:
        same = (
            int(existing_branch.parent_account_id or 0) == int(source_account_id)
            and existing_branch.fork_date == fork_date
        )
        if same:
            printer(
                f"已存在同名克隆：账户 id={existing.id} name={existing.name} "
                f"parent={source_account_id} fork={fork_date.isoformat()}，无需重复创建"
            )
            return {
                "status": "exists",
                "clone_account_id": int(existing.id),
                "fork_date": fork_date,
                "note": "幂等命中，未做任何写入",
            }

    # 拒绝两类输入：晚于源账户最后成交日、早于最早活动日
    if last_trade is not None and fork_date > last_trade and not allow_future_fork:
        raise CloneError(
            f"fork_date 晚于源账户最后成交日 {last_trade.isoformat()}（归一后 {fork_date.isoformat()}）。"
            "确认要从未来时点起一个空仓账户请加 --allow-future-fork"
        )
    if first_activity is not None and fork_date < first_activity:
        raise CloneError(
            f"fork_date {fork_date.isoformat()} 早于源账户最早活动日 {first_activity.isoformat()}，"
            "克隆将没有任何历史，拒绝"
        )
    if existing is not None and existing_branch is None:
        raise CloneError(
            f"账户名 {existing.name!r} 已被 id={existing.id} 占用且不是本克隆（portfolio_accounts.name 无唯一约束）。"
            "请换一个 --name"
        )
    if existing is not None:
        raise CloneError(f"账户名 {existing.name!r} 已被 id={existing.id} 占用，请换一个 --name")

    with repo.db.get_session() as session:
        events = read_events(session, source_account_id, fork_date)
    counts = {key: len(value) for key, value in events.items()}
    total_events = sum(counts.values())

    cash_day: Optional[date] = None
    cash_note = "未指定 --initial-cash，不注入额外现金"
    if initial_cash is not None:
        cash_day = get_next_trading_date(market, fork_date)
        if cash_day is None:
            cash_day = fork_date + timedelta(days=1)
            cash_note = (
                f"注入 {float(initial_cash):.2f} 现金流入，日期 {cash_day.isoformat()}"
                "（交易日历不可用，退化为 fork 次日）"
            )
        else:
            cash_note = f"注入 {float(initial_cash):.2f} 现金流入，日期 {cash_day.isoformat()}（fork 之后下一个交易日）"

    printer("=" * 72)
    printer(
        f"源账户      : {source['name']} (id={source['id']}, market={market}, "
        f"broker={source['broker']}, 币种={source['base_currency']})"
    )
    printer(f"新账户      : {name}（auto_run=0，不自动跑批）")
    printer(f"fork_date   : 原始 {requested_fork_date.isoformat()} -> 归一 {fork_date.isoformat()}（{fork_note}）")
    profile_label = (
        f"id={profile['id']} name={profile['name']} version={profile['version']}"
        if profile.get("id") is not None
        else f"待创建 name={profile['name']} version={profile['version']}（与账户同事务）"
    )
    printer(f"策略档      : {profile_label}")
    printer(f"复制事件    : 成交 {counts['trades']} / 现金 {counts['cash']} / 公司行动 {counts['corporate_actions']}"
            f"（合计 {total_events}），派生表 {', '.join(DERIVED_TABLES)} 一律不复制")
    printer(f"分叉后现金  : {cash_note}")
    printer(f"写入模式    : {'APPLY —— 直接写真实库' if apply else 'DRY-RUN —— 只读备份到临时库，真实库不写'}")
    printer("=" * 72)

    source_snapshot = read_snapshot(portfolio_service, source_account_id, fork_date)
    printer(
        "源账户 fork 时点快照: "
        + " | ".join(f"{key}={source_snapshot.get(key)!r}" for key in SNAPSHOT_MONEY_KEYS)
        + f" | 持仓={sorted(positions_map(source_snapshot))}"
    )

    new_account_id: Optional[int] = None
    # 单事务：建账户 + 复制三表 + 写 branch 行 + 行级自检，任一失败整体回滚。
    with repo.portfolio_write_session() as session:
        account = PortfolioAccount(
            owner_id=source["owner_id"],
            name=name,
            broker=source["broker"],
            market=source["market"],
            base_currency=source["base_currency"],
            is_active=source["is_active"],
        )
        session.add(account)
        session.flush()
        new_account_id = int(account.id)
        # 行级自检只比对「从源账户复制过来的行」；--initial-cash 注入的现金行不是复制品，
        # 必须排除在自检之外，否则源/克隆行数天然不等。
        copied_rows = build_clone_rows(events, new_account_id)
        clone_rows = list(copied_rows)
        if cash_day is not None:
            clone_rows.append(
                PortfolioCashLedger(
                    account_id=new_account_id,
                    event_date=cash_day,
                    direction="in",
                    amount=float(to_cent(Decimal(str(initial_cash)))),
                    currency=source["base_currency"],
                    note=f"克隆分叉注资（fork {fork_date.isoformat()} 之后）",
                )
            )
        for row in clone_rows:
            session.add(row)
        profile_id = profile.get("id")
        if profile_id is None:
            created_profile = PaperStrategyProfile(
                name=str(profile["name"]),
                version=int(profile["version"]),
                config_json=json.dumps(profile.get("config") or {}, ensure_ascii=False),
                is_active=True,
            )
            session.add(created_profile)
            session.flush()
            profile_id = int(created_profile.id)
        session.add(
            PaperAccountBranch(
                account_id=new_account_id,
                parent_account_id=int(source_account_id),
                fork_date=fork_date,
                profile_id=int(profile_id),
                label=name,
                auto_run=False,
                role="control",
            )
        )
        session.flush()
        diffs = row_level_diff(events, copied_rows)
        if diffs:
            for line in diffs:
                printer(f"  [行级自检] {line}")
            raise CloneError(f"行级自检失败，事务已回滚（未留下账户）：{len(diffs)} 处差异")

    clone_snapshot = read_snapshot(portfolio_service, new_account_id, fork_date)
    diffs = snapshot_diff(source_snapshot, clone_snapshot)
    printer(
        "克隆账户快照:   "
        + " | ".join(f"{key}={clone_snapshot.get(key)!r}" for key in SNAPSHOT_MONEY_KEYS)
        + f" | 持仓={sorted(positions_map(clone_snapshot))}"
    )
    for code, values in sorted(positions_map(source_snapshot).items()):
        printer(f"  持仓 {code}: {values} vs 克隆 {positions_map(clone_snapshot).get(code)}")

    if diffs:
        for line in diffs:
            printer(f"  [快照校验] {line}")
        deleted = delete_clone(repo=repo, account_id=new_account_id)
        raise CloneError(
            f"快照校验失败：克隆账户 id={new_account_id} 已补偿删除（{deleted}），真实账务未被污染。"
            f"共 {len(diffs)} 处字段级差异，见上方 [快照校验] 明细"
        )

    printer("-" * 72)
    printer(
        f"校验通过：{len(SNAPSHOT_MONEY_KEYS)} 个金额字段 + {len(positions_map(source_snapshot))} 个持仓 "
        f"× {len(POSITION_KEYS)} 个字段逐位相等（金额 round(.,6)/分位、持仓 round(.,8)/分位双重断言）"
    )
    if apply:
        printer(f"已写入真实库：新账户 id={new_account_id}，事件 {total_events} 行 + branch 行 1 条")
    else:
        printer("dry-run：以上结果来自真实库的临时副本，真实库未写入任何行")
    return {
        "status": "applied" if apply else "dry_run_ok",
        "source_account_id": int(source_account_id),
        "clone_account_id": new_account_id,
        "fork_date": fork_date,
        "counts": counts,
        "initial_cash_date": cash_day,
        "diffs": diffs,
    }


def delete_clone(*, repo: Any, account_id: int) -> str:
    """补偿删除：只删本次新建账户自己的行。"""
    with repo.portfolio_write_session() as session:
        trades = session.execute(
            PortfolioTrade.__table__.delete().where(PortfolioTrade.account_id == account_id)
        ).rowcount
        cash = session.execute(
            PortfolioCashLedger.__table__.delete().where(PortfolioCashLedger.account_id == account_id)
        ).rowcount
        actions = session.execute(
            PortfolioCorporateAction.__table__.delete().where(PortfolioCorporateAction.account_id == account_id)
        ).rowcount
        session.execute(
            PaperAccountBranch.__table__.delete().where(PaperAccountBranch.account_id == account_id)
        )
        session.execute(
            PaperAccountRun.__table__.delete().where(PaperAccountRun.account_id == account_id)
        )
        session.execute(PortfolioAccount.__table__.delete().where(PortfolioAccount.id == account_id))
    return f"成交 {trades} / 现金 {cash} / 公司行动 {actions} / branch 与账户行 各 1"


def discover_source(repo: Any) -> int:
    """未显式给 --source-account-id 时，只在「唯一一个纸面账户」时自动发现。"""
    with repo.db.get_session() as session:
        rows = list_paper_accounts_in_session(session=session, account_names=(), include_inactive=False)
    if len(rows) != 1:
        names = "、".join(f"{row.name}(id={row.id})" for row in rows) or "（无）"
        raise CloneError(f"请用 --source-account-id 指定源账户，当前活动纸面账户有 {len(rows)} 个：{names}")
    return int(rows[0].id)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="克隆一个纸面账户到 fork 时点（默认 dry-run，--apply 才写真实库）"
    )
    parser.add_argument("--source-account-id", type=int, help="源纸面账户 id；省略时仅在唯一纸面账户时自动发现")
    parser.add_argument("--name", required=True, help="新克隆账户名（必须全局唯一）")
    parser.add_argument("--fork-date", required=True, help="分叉日 YYYY-MM-DD，会归一为 <= 它的最后一个交易日")
    parser.add_argument("--profile-id", type=int, help="策略档 id（与 --profile-name 互斥）")
    parser.add_argument("--profile-name", help="策略档 name（与 --profile-id 互斥）；都不给则用默认档")
    parser.add_argument("--initial-cash", type=float, help="分叉之后下一个交易日注入的现金；不传则不注入")
    parser.add_argument("--apply", action="store_true", help="真实写入业务库（默认 dry-run 只写临时副本）")
    parser.add_argument("--allow-future-fork", action="store_true", help="允许 fork_date 晚于源账户最后成交日")
    return parser.parse_args(argv)


def confirm_apply(*, name: str, source_id: int, fork_date: str) -> None:
    banner = (
        "*** 即将写入真实业务库 ***\n"
        f"  新建账户：{name}\n"
        f"  源账户  ：id={source_id}\n"
        f"  fork 日 ：{fork_date}（会归一为 <= 它的最后一个交易日）\n"
        "  写入内容：portfolio_accounts 1 行 + portfolio_trades/cash_ledger/corporate_actions 复制行 "
        "+ paper_account_branches 1 行（不复制派生表；校验不通过则整体补偿删除）"
    )
    print(banner)
    if sys.stdin.isatty():
        answer = input("确认写入请输入 yes：").strip().lower()
        if answer != "yes":
            raise CloneError("未确认，已退出（未写入任何行）")
    else:
        print("（非交互终端，跳过输入确认；显式 --apply 视为已确认）")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        requested = date.fromisoformat(str(args.fork_date).strip())
    except ValueError as exc:
        print(f"--fork-date 解析失败：{exc}（示例 2026-11-13）")
        return 2

    temp_dir: Optional[str] = None
    try:
        if args.apply:
            print(f"目标库（真实写入）：{live_db_path()}")
        else:
            source_db = live_db_path()
            temp_dir, temp_db = backup_to_temp()
            print(f"dry-run：真实库 {source_db} 只读备份 -> {temp_db}")
            print("           以下所有写入都发生在该临时副本，真实库不会被写一行")

        from src.repositories.portfolio_repo import PortfolioRepository
        from src.services.portfolio_service import PortfolioService
        from src.storage import DatabaseManager

        repo = PortfolioRepository(DatabaseManager.get_instance())
        portfolio_service = PortfolioService(repo=repo)
        source_id = int(args.source_account_id or discover_source(repo))
        profile = resolve_profile(repo, profile_id=args.profile_id, profile_name=args.profile_name)
        if args.apply:
            confirm_apply(name=args.name, source_id=source_id, fork_date=args.fork_date)
        clone_account(
            repo=repo,
            portfolio_service=portfolio_service,
            source_account_id=source_id,
            name=args.name,
            requested_fork_date=requested,
            profile=profile,
            initial_cash=args.initial_cash,
            apply=bool(args.apply),
            allow_future_fork=bool(args.allow_future_fork),
        )
        return 0
    except CloneError as exc:
        print(f"克隆失败：{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 需要把原因打印出来而不是抛栈
        print(f"克隆异常：{type(exc).__name__}: {exc}")
        return 1
    finally:
        if temp_dir is not None:
            import shutil

            # 先放掉连接，Windows 上 SQLite 句柄未释放会导致目录删不掉
            from src.storage import DatabaseManager

            DatabaseManager.reset_instance()
            shutil.rmtree(temp_dir, ignore_errors=True)
            print("临时副本已清理")


if __name__ == "__main__":
    raise SystemExit(main())
