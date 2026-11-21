# -*- coding: utf-8 -*-
"""Promote one paper branch to ``live`` and demote the rest (governance).

The comparison endpoint answers "who is better"; this script is the explicit
operator action that switches governance. It defaults to a read-only rehearsal:
the branch comparison is printed, nothing is written. ``--apply`` performs the
single-transaction swap (winner -> live, other live -> control). ``auto_run``
is never touched, so a demoted branch keeps running as the control group.

Usage:
    python scripts/paper_branch_promote.py --account-id 3
    python scripts/paper_branch_promote.py --account-id 3 --apply

The operation is idempotent: applying it twice leaves the same single live
branch, and the second run reports no demotions.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import setup_env  # noqa: E402

setup_env()

# Pure local ledger operation; drop proxies so imported HTTP clients cannot
# pick up a broken NO_PROXY entry on this machine.
for _proxy_key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_proxy_key, None)

from src.repositories.portfolio_repo import PortfolioRepository  # noqa: E402
from src.services.paper_branch_compare_service import PaperBranchCompareService  # noqa: E402


def _branch_line(row: Dict[str, Any]) -> str:
    return (
        f"#{row['account_id']} {row.get('account_name') or ''}"
        f"（role={row.get('role')} / state={row.get('state')}"
        f" / auto_run={row.get('auto_run')}）"
    )


def _print_comparison(
    service: PaperBranchCompareService,
    account_id: int,
    as_of: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    payload = service.compare(as_of=as_of, account_ids=[account_id])
    branch = next(
        (row for row in payload.get("branches") or [] if int(row["account_id"]) == account_id),
        None,
    )
    window = payload.get("window") or {}
    print(
        f"对比窗口 {window.get('start')} ~ {window.get('end')}"
        f"（{window.get('days')} 个交易日，参照 live=#{window.get('reference_account_id')}）"
    )
    if branch is None:
        return None
    metrics = branch.get("metrics") or {}
    print(
        "指标：区间收益 {ret} / 最大回撤 {mdd} / 超额 {excess} / 滚动胜率 {win} / 连续榜首 {top} 天".format(
            ret=_fmt_pct(metrics.get("return_pct")),
            mdd=_fmt_pct(metrics.get("max_drawdown_pct")),
            excess=_fmt_pct(metrics.get("excess_pct")),
            win=_fmt_ratio(metrics.get("rolling_win")),
            top=metrics.get("top_days", 0),
        )
    )
    if branch.get("is_reference"):
        print("该分支已是 live 参照，没有转正门槛。")
        return branch
    for gate in branch.get("gates") or []:
        mark = {"pass": "✓", "fail": "✗", "na": "—"}.get(str(gate.get("status")), "?")
        print(f"  {mark} {gate.get('name')}：{gate.get('hint')}")
    passed_text = "通过" if branch.get("passed") else "未通过"
    print(f"门槛结论：{passed_text}；综合分 {branch.get('score')}")
    return branch


def _fmt_pct(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}%"


def _fmt_ratio(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="把纸面分支转正为 live，其余 live 降为 control")
    parser.add_argument("--account-id", type=int, required=True, help="要转正的纸面分支账户 id")
    parser.add_argument("--as-of", type=str, default=None, help="对比截止日 YYYY-MM-DD，默认今天")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加时只读预演（打印对比与门槛，不修改任何数据）",
    )
    args = parser.parse_args(argv)

    if args.account_id <= 0:
        print("account-id 必须大于 0", file=sys.stderr)
        return 2
    as_of = date.today()
    if args.as_of:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"as-of 不是合法日期：{args.as_of}", file=sys.stderr)
            return 2

    repo = PortfolioRepository()
    branches = {
        int(row["account_id"]): row
        for row in repo.list_branch_accounts(auto_run_only=False, include_inactive=True)
    }
    target = branches.get(args.account_id)
    if target is None:
        print(f"账户 #{args.account_id} 没有分支记录，无法转正", file=sys.stderr)
        return 2

    print("当前分支：" + _branch_line(target))
    branch = _print_comparison(
        PaperBranchCompareService(repo=repo),
        args.account_id,
        as_of=as_of,
    )
    if branch is not None and not branch.get("is_reference") and not branch.get("passed"):
        print("提示：该分支未通过全部转正门槛，确认仍要转正请使用 --apply。")
    if not target.get("auto_run"):
        print("提示：该分支 auto_run=false，转正后不会自动参与跑批。")

    if not args.apply:
        print("\n[预演] 未写库。确认无误后加 --apply 执行转正（单事务：目标 -> live，其余 live -> control）。")
        return 0

    try:
        result = repo.promote_branch(args.account_id)
    except ValueError as exc:
        print(f"转正失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"转正失败：{exc}", file=sys.stderr)
        return 1

    demoted = result.get("demoted_account_ids") or []
    if demoted:
        print(
            "已转正：live 现在是 #{live}；降为 control：".format(live=result["live_account_id"])
            + "、".join(f"#{item}" for item in demoted)
        )
    else:
        print(f"live 已经是 #{result['live_account_id']}，无需降级其它分支。")
    print("auto_run 未改动：降级分支继续运行，作为后续验证的对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
