#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual / cron entry for the DSA paper trading loop.

Examples:
    python scripts/paper_trading_run.py                 # dry run for the due batch
    python scripts/paper_trading_run.py --apply         # write paper trades
    python scripts/paper_trading_run.py --signal-date 2026-10-31
    python scripts/paper_trading_run.py --account-id 1 --min-score 65
    python scripts/paper_trading_run.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import setup_env  # noqa: E402

setup_env()

from src.services.paper_trading_service import (  # noqa: E402
    DEFAULT_HORIZONS,
    PaperTradingService,
)


def _print_result(result: dict) -> None:
    print("=" * 88)
    print("DSA 决策信号 → 纸面账户")
    print("=" * 88)
    print(f"状态：{result['status']}｜{'干跑' if result['dry_run'] else '执行'}")
    print(result["message"])
    if result.get("latest_data_date"):
        print(f"A 股日线最新：{result['latest_data_date']}")
    batch = result.get("batch")
    if batch:
        dates = ", ".join(batch.get("signal_dates") or [])
        print(f"信号日：{dates}｜成交日：{batch['fill_date']}｜参考日：{batch['reference_date']}")
    account = result.get("account")
    if account and account.get("name"):
        suffix = "（自动创建）" if account.get("created") else ""
        cash = f"｜初始资金 {account['initial_cash']:.2f}" if account.get("initial_cash") else ""
        print(f"账户：{account['name']}（{account.get('id')}）{suffix}{cash}")
    portfolio = result.get("portfolio")
    if portfolio:
        print(
            f"权益 {portfolio['equity']:.2f}（现金 {portfolio['cash']:.2f} + "
            f"市值 {portfolio['market_value']:.2f}）｜单票目标 {portfolio['per_name']:.2f}"
            f"｜持仓 {portfolio['held_names']} 只"
        )

    items = result.get("items") or []
    if items:
        print("")
        print(f"{'方向':<4}{'代码':<8}{'动作':<8}{'评分':>4}{'数量':>8}{'价格':>9}  说明")
        print("-" * 88)
        for row in items:
            side = {"buy": "买入", "sell": "卖出"}.get(row.get("side") or "", "—")
            score = row.get("score")
            price = row.get("price")
            print(
                f"{side:<4}{row['stock_code']:<8}{row.get('action') or '?':<8}"
                f"{str(score if score is not None else '-'):>4}{row.get('quantity') or '-':>8}"
                f"{(f'{price:.2f}' if price else '-'):>9}  {row.get('reason') or ''}"
            )

    orders = result.get("orders") or []
    if orders:
        print("")
        print(f"委托 {len(orders)} 笔：")
        for order in orders:
            print(
                f"  [{order.get('status')}] {order['side']} {order['stock_code']} "
                f"{order['quantity']} 股 @ {order.get('price')}"
                + (f" — {order['reason']}" if order.get("reason") else "")
            )

    corporate_actions = result.get("corporate_actions") or []
    if corporate_actions:
        print("")
        print(f"自动登记分红送转 {len(corporate_actions)} 笔：")
        for line in corporate_actions:
            print(f"  - {line}")

    warnings = result.get("warnings") or []
    if warnings:
        print("")
        for warning in warnings:
            print(f"⚠️  {warning}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DSA 决策信号 → 内置纸面账户（A 股）")
    parser.add_argument("--account-id", type=int, default=None, help="组合账户 ID")
    parser.add_argument("--account-name", default=None, help="纸面账户名（默认 PAPER_TRADING_ACCOUNT_NAME 或「纸面交易」）")
    parser.add_argument("--horizons", default=",".join(DEFAULT_HORIZONS), help="信号周期，逗号分隔（默认 3d,5d）")
    parser.add_argument("--min-score", type=int, default=None, help="买入最低评分（默认 60）")
    parser.add_argument("--max-names", type=int, default=None, help="最多持有只数（默认 5）")
    parser.add_argument("--max-weight", type=float, default=None, help="单票最大权重（默认 0.2）")
    parser.add_argument("--signal-date", default=None, help="只跑指定信号创建日（YYYY-MM-DD，补跑用）")
    parser.add_argument("--initial-cash", type=float, default=None, help="自动创建账户时的初始资金")
    parser.add_argument("--apply", action="store_true", help="真正写入纸面成交（默认干跑）")
    parser.add_argument("--force", action="store_true", help="同一成交日已有流水时仍继续")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args(argv)

    import datetime as _dt

    signal_date = _dt.date.fromisoformat(args.signal_date) if args.signal_date else None
    horizons = [part.strip() for part in args.horizons.split(",") if part.strip()]
    result = PaperTradingService().run(
        account_id=args.account_id,
        account_name=args.account_name,
        horizons=horizons,
        min_score=args.min_score,
        max_names=args.max_names,
        max_weight=args.max_weight,
        signal_date=signal_date,
        dry_run=not args.apply,
        force=args.force,
        initial_cash=args.initial_cash,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_result(result)

    if result["status"] == "data_not_ready":
        return 2
    if result["status"] == "duplicate_batch":
        return 3
    if any(order.get("status") == "failed" for order in result.get("orders") or []):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
