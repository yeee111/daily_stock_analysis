# -*- coding: utf-8 -*-
"""Counterfactual cost audit for the paper-trading ledger.

The live database is never written to. For each scenario the script copies the
DB (WAL-safe backup), re-prices every executed trade with the slippage model
(buys pay up, sells receive less, clamped to the daily price limit), rebuilds
fees from the new gross amount, and reruns the performance report on the temp
copy. Quantities are unchanged, so the baseline row reproduces the live ledger
exactly and the other rows isolate the cost impact.

A separate liquidity table reports how much of each symbol's 20-day average
volume every real order used, i.e. whether an ADV cap would have bound.

Usage:
    python scripts/paper_trading_cost_audit.py
    python scripts/paper_trading_cost_audit.py --account-id 2 --as-of 2026-11-06
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The audit is fully offline; httpx (imported via litellm) mis-parses NO_PROXY
# entries such as "[::1]" when they are left in the environment.
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

from src.services.paper_trading_service import apply_slippage, compute_fees  # noqa: E402

SOURCE_DB = ROOT / "data" / "stock_analysis.db"
SOURCE_ENV = ROOT / ".env"

_DERIVED_TABLES = (
    "portfolio_position_lots",
    "portfolio_positions",
    "portfolio_daily_snapshots",
)

_OVERRIDE_KEYS = {
    "DATABASE_PATH",
    "PAPER_TRADING_FETCH_MISSING_BARS",
    "PORTFOLIO_MARKET_DATA_SYNC_ENABLED",
    "SCHEDULE_ENABLED",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
}


def _backup_db(target_db: Path) -> None:
    source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    target = sqlite3.connect(target_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _write_env(target: Path, db_path: Path) -> None:
    kept: List[str] = []
    for raw in SOURCE_ENV.read_text(encoding="utf-8").splitlines():
        key = raw.split("=", 1)[0].strip()
        if key in _OVERRIDE_KEYS:
            continue
        kept.append(raw)
    kept.extend(
        [
            f"DATABASE_PATH={db_path}",
            "PAPER_TRADING_FETCH_MISSING_BARS=false",
            "PORTFOLIO_MARKET_DATA_SYNC_ENABLED=false",
            "SCHEDULE_ENABLED=false",
        ]
    )
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _reset_runtime() -> None:
    from src.config import Config
    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()
    Config.reset_instance()


def _discover_account_id() -> int:
    con = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select id from portfolio_accounts "
            "where lower(coalesce(broker, '')) = 'paper' or name = '纸面交易' "
            "order by id limit 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit("未找到纸面交易账户（broker=paper / name=纸面交易）")
    return int(row[0])


def _previous_close(con: sqlite3.Connection, symbol: str, day: str) -> Optional[float]:
    row = con.execute(
        "select close from stock_daily where code = ? and date < ? "
        "and close > 0 order by date desc limit 1",
        (symbol, day),
    ).fetchone()
    return float(row[0]) if row else None


def _reprice_trades(con: sqlite3.Connection, account_id: int, bps: float) -> None:
    rows = con.execute(
        "select id, symbol, side, trade_date, quantity, price from portfolio_trades "
        "where account_id = ? order by trade_date, id",
        (account_id,),
    ).fetchall()
    for trade_id, symbol, side, trade_day, quantity, price in rows:
        previous = _previous_close(con, symbol, trade_day)
        new_price = apply_slippage(
            float(price),
            side,
            code=symbol,
            previous_close=previous,
            bps=bps,
        )
        gross = Decimal(str(new_price)) * Decimal(str(quantity))
        fee, tax = compute_fees(side, gross)
        con.execute(
            "update portfolio_trades set price = ?, fee = ?, tax = ? where id = ?",
            (new_price, float(fee), float(tax), trade_id),
        )


def _run_scenario(
    *,
    label: str,
    account_id: int,
    as_of: date,
    slippage_bps: float,
) -> Dict[str, Any]:
    from src.services.portfolio_performance_service import PortfolioPerformanceService

    with tempfile.TemporaryDirectory(prefix="paper-cost-audit-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "audit.db"
        env_path = tmp_path / "audit.env"
        _backup_db(db_path)
        con = sqlite3.connect(db_path)
        try:
            if slippage_bps > 0:
                _reprice_trades(con, account_id, slippage_bps)
            for table in _DERIVED_TABLES:
                con.execute(f"delete from {table} where account_id = ?", (account_id,))
            con.commit()
        finally:
            con.close()

        _write_env(env_path, db_path)
        os.environ["ENV_FILE"] = str(env_path)
        os.environ["DATABASE_PATH"] = str(db_path)
        _reset_runtime()
        try:
            report = PortfolioPerformanceService().get_performance_report(
                account_id=account_id,
                as_of=as_of,
                include_realtime=False,
            )
        finally:
            _reset_runtime()
            os.environ.pop("ENV_FILE", None)
            os.environ.pop("DATABASE_PATH", None)

    closed = report["closed_trades"]
    summary = report["summary"]
    return {
        "label": label,
        "trades": summary["trade_count"],
        "closed": closed["count"],
        "wins": closed["wins"],
        "losses": closed["losses"],
        "win_rate": closed["win_rate_pct"],
        "profit_factor": closed["profit_factor"],
        "realized": summary["realized_pnl"],
        "unrealized": summary["unrealized_pnl"],
        "return_pct": summary["total_return_pct"],
        "max_dd": summary["max_drawdown_pct"],
        "fees": summary["fee_total"],
    }


def _liquidity_profile(account_id: int, window_days: int) -> List[Dict[str, Any]]:
    con = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    try:
        trades = con.execute(
            "select symbol, side, trade_date, quantity from portfolio_trades "
            "where account_id = ? order by trade_date, id",
            (account_id,),
        ).fetchall()
        profile: List[Dict[str, Any]] = []
        for symbol, side, trade_day, quantity in trades:
            bars = con.execute(
                "select volume from stock_daily where code = ? and date < ? "
                "and volume > 0 order by date desc limit ?",
                (symbol, trade_day, window_days),
            ).fetchall()
            if not bars:
                continue
            adv = sum(float(row[0]) for row in bars) / len(bars)
            profile.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "trade_date": trade_day,
                    "quantity": float(quantity),
                    "adv": adv,
                    "share_pct": float(quantity) / adv * 100.0 if adv > 0 else None,
                }
            )
        return profile
    finally:
        con.close()


def _fmt_pct(value: Optional[float], *, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}%"


def _fmt_num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _print_table(rows: List[Dict[str, Any]]) -> None:
    header = (
        f"{'场景':<18}{'成交':>4}{'平仓':>4}{'胜/负':>7}{'胜率':>8}"
        f"{'盈亏比':>8}{'已实现':>10}{'浮动':>10}{'总收益':>8}{'回撤':>7}{'费用':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        wins_losses = f"{row['wins']}/{row['losses']}"
        print(
            f"{row['label']:<18}"
            f"{row['trades']:>4}"
            f"{row['closed']:>4}"
            f"{wins_losses:>7}"
            f"{_fmt_pct(row['win_rate']):>8}"
            f"{_fmt_num(row['profit_factor']):>8}"
            f"{row['realized']:>10.2f}"
            f"{row['unrealized']:>10.2f}"
            f"{_fmt_pct(row['return_pct']):>8}"
            f"{_fmt_pct(row['max_dd']):>7}"
            f"{row['fees']:>9.2f}"
        )


def _print_liquidity(profile: List[Dict[str, Any]], window_days: int) -> None:
    if not profile:
        print("\n流动性：无可用的成交量数据")
        return
    shares = sorted(item["share_pct"] for item in profile if item["share_pct"] is not None)
    median = shares[len(shares) // 2] if shares else None
    worst = max(
        (item for item in profile if item["share_pct"] is not None),
        key=lambda item: item["share_pct"],
    )
    print(f"\n流动性占用（单笔数量 / {window_days} 日均量）：")
    print(
        f"  中位数 {_fmt_pct(median, digits=3)}，最大 {_fmt_pct(worst['share_pct'], digits=3)}"
        f"（{worst['symbol']} {worst['side']} {worst['trade_date']}，"
        f"{worst['quantity']:.0f} 股 / ADV {worst['adv']:.0f}）"
    )
    for cap in (1.0, 0.5):
        hit = [item for item in profile if item["share_pct"] is not None and item["share_pct"] > cap]
        print(f"  {cap:g}% 上限会裁剪的委托：{len(hit)} / {len(profile)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, default=None)
    parser.add_argument("--as-of", type=str, default=None)
    parser.add_argument("--adv-window-days", type=int, default=20)
    args = parser.parse_args()

    if not SOURCE_DB.exists() or not SOURCE_ENV.exists():
        raise SystemExit(f"缺少 {SOURCE_DB} 或 {SOURCE_ENV}")
    account_id = args.account_id or _discover_account_id()
    if args.as_of:
        as_of = date.fromisoformat(args.as_of)
    else:
        con = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            as_of = date.fromisoformat(
                con.execute("select max(date) from stock_daily where length(code) = 6").fetchone()[0]
            )
        finally:
            con.close()

    print(f"账户 {account_id}，截至 {as_of}（成交数量不变，仅重定价与重算费用）\n")
    scenarios = [
        ("现有账本 0bp", 0.0),
        ("滑点 5bp", 5.0),
        ("滑点 10bp", 10.0),
        ("滑点 20bp", 20.0),
    ]
    rows = [
        _run_scenario(
            label=label,
            account_id=account_id,
            as_of=as_of,
            slippage_bps=bps,
        )
        for label, bps in scenarios
    ]
    _print_table(rows)
    _print_liquidity(_liquidity_profile(account_id, args.adv_window_days), args.adv_window_days)


if __name__ == "__main__":
    main()