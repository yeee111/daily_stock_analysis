# -*- coding: utf-8 -*-
"""跨账户分支对比服务的只读与门槛测试。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

import pytest
from sqlalchemy import func, select

from src.config import Config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.paper_branch_compare_service import PaperBranchCompareService
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager, PortfolioDailySnapshot


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "branch_compare.db"
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
    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        yield DatabaseManager.get_instance()
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()


def _create_branch(
    service: PortfolioService,
    repo: PortfolioRepository,
    *,
    name: str,
    parent_account_id: Optional[int] = None,
    fork_date: date = date(2026, 1, 5),
    role: str = "live",
) -> int:
    account = service.create_account(
        name=name,
        broker="paper",
        market="cn",
        base_currency="CNY",
    )
    repo.upsert_branch(
        account_id=int(account["id"]),
        fork_date=fork_date,
        parent_account_id=parent_account_id,
        auto_run=role == "control",
        role=role,
    )
    return int(account["id"])


def _deposit(service: PortfolioService, account_id: int, day: date, amount: float) -> None:
    service.record_cash_ledger(
        account_id=account_id,
        event_date=day,
        direction="in",
        amount=amount,
        currency="CNY",
    )


def _snapshots(db: DatabaseManager, account_id: int, values: Dict[date, float]) -> None:
    with db.get_session() as session:
        for day, equity in values.items():
            session.add(
                PortfolioDailySnapshot(
                    account_id=account_id,
                    snapshot_date=day,
                    cost_method="fifo",
                    base_currency="CNY",
                    total_cash=equity,
                    total_market_value=0.0,
                    total_equity=equity,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    fee_total=0.0,
                    tax_total=0.0,
                    fx_stale=False,
                )
            )
        session.commit()


def _snapshot_count(db: DatabaseManager) -> int:
    with db.get_session() as session:
        return int(
            session.execute(select(func.count()).select_from(PortfolioDailySnapshot)).scalar_one()
        )


def test_twr_removes_branch_funding_and_aligns_common_window(env) -> None:
    db = env
    service = PortfolioService()
    repo = PortfolioRepository()

    live = _create_branch(service, repo, name="live")
    control = _create_branch(
        service,
        repo,
        name="control",
        parent_account_id=live,
        role="control",
    )

    _deposit(service, live, date(2026, 1, 5), 100000.0)
    # The clone is funded the day after its fork, exactly like the clone script.
    _deposit(service, control, date(2026, 1, 6), 100000.0)

    _snapshots(db, live, {
        date(2026, 1, 5): 100000.0,
        date(2026, 1, 6): 101000.0,
        date(2026, 1, 7): 102000.0,
        date(2026, 1, 8): 101000.0,
    })
    _snapshots(db, control, {
        date(2026, 1, 5): 0.0,
        date(2026, 1, 6): 100000.0,
        date(2026, 1, 7): 102000.0,
        date(2026, 1, 8): 103000.0,
    })

    payload = PaperBranchCompareService(repo=repo).compare(as_of=date(2026, 1, 8))

    assert payload["window"]["reference_account_id"] == live
    assert payload["window"]["start"] == "2026-01-05"
    assert payload["window"]["days"] == 4

    rows = {row["account_id"]: row for row in payload["branches"]}
    assert rows[live]["is_reference"] is True
    assert rows[live]["gates"] == []
    assert rows[live]["metrics"]["return_pct"] == pytest.approx(1.0)

    candidate = rows[control]
    assert candidate["role"] == "control"
    assert candidate["state"] == "shadow"
    # A 0 -> 100000 deposit must not become an infinite return: the curve is
    # rebased to 100 at the common start and only real P&L shows up.
    assert [point["nav"] for point in candidate["series"]] == pytest.approx(
        [100.0, 100.0, 102.0, 103.0]
    )
    assert candidate["metrics"]["return_pct"] == pytest.approx(3.0)
    assert candidate["metrics"]["excess_pct"] == pytest.approx(2.0)
    assert candidate["metrics"]["max_drawdown_pct"] == pytest.approx(0.0)
    assert candidate["metrics"]["rolling_win"] == pytest.approx(2.0 / 3.0)
    assert candidate["metrics"]["top_days"] == 4
    assert candidate["passed"] is False  # only 4 days / 0 trades
    gate_names = [item["name"] for item in candidate["gates"]]
    assert gate_names == ["可比交易日", "成交笔数", "区间超额", "回撤比", "滚动胜率", "连续榜首", "参数稳健性"]
    assert candidate["gates"][6]["status"] == "na"
    assert candidate["gates"][6]["ok"] is None
    assert candidate["gates"][2]["ok"] is False  # 2% excess < 5% threshold
    assert candidate["gates"][3]["ok"] is True  # same-window drawdown ratio


def test_comparison_never_snapshots_on_read(env) -> None:
    db = env
    service = PortfolioService()
    repo = PortfolioRepository()

    live = _create_branch(service, repo, name="live")
    control = _create_branch(
        service,
        repo,
        name="control",
        parent_account_id=live,
        role="control",
    )
    day = date(2026, 11, 10)
    _deposit(service, live, day, 100000.0)
    _deposit(service, control, day, 100000.0)

    assert _snapshot_count(db) == 0
    payload = PaperBranchCompareService(repo=repo).compare(as_of=date(2026, 11, 14))

    # persist=False: a GET must never backfill portfolio_daily_snapshots.
    assert _snapshot_count(db) == 0
    assert any("没有可用的历史净值快照" in item for item in payload["warnings"])
    assert payload["branches"][0]["series"] == []


def test_all_gates_pass_and_score_matches_shadowbranch_formula(env) -> None:
    db = env
    service = PortfolioService()
    repo = PortfolioRepository()

    start = date(2026, 1, 5)
    days: List[date] = [start + timedelta(days=offset) for offset in range(25)]
    live = _create_branch(service, repo, name="live", fork_date=start)
    control = _create_branch(
        service,
        repo,
        name="control",
        parent_account_id=live,
        fork_date=start,
        role="control",
    )
    _deposit(service, live, start, 100000.0)
    _deposit(service, control, start, 100000.0)

    _snapshots(db, live, {day: 100000.0 for day in days})
    # Recording a trade invalidates snapshots from that date onward, so the
    # deterministic equity curve is written afterwards.
    for index in range(8):
        service.record_trade(
            account_id=control,
            symbol="600519",
            trade_date=days[index + 1],
            side="buy",
            quantity=1.0,
            price=1.0,
            market="cn",
            currency="CNY",
        )
    _snapshots(db, control, {
        day: round(100000.0 * (1.01 ** index), 2) for index, day in enumerate(days)
    })

    payload = PaperBranchCompareService(repo=repo).compare(as_of=days[-1])
    candidate = next(row for row in payload["branches"] if row["account_id"] == control)

    assert payload["window"]["days"] == 25
    assert candidate["metrics"]["trades"] == 8
    assert candidate["metrics"]["rolling_win"] == pytest.approx(1.0)
    assert candidate["metrics"]["top_days"] == 25
    assert candidate["passed"] is True
    assert [item["status"] for item in candidate["gates"]] == [
        "pass", "pass", "pass", "pass", "pass", "pass", "na",
    ]
    # 0.30*n_excess + 0.20*n_dd + 0.20*n_win + 0.20*0 + 0.10*n_sample
    assert candidate["score"] == pytest.approx(75.0)


def test_account_filter_keeps_live_reference_for_excess(env) -> None:
    db = env
    service = PortfolioService()
    repo = PortfolioRepository()

    live = _create_branch(service, repo, name="live")
    control = _create_branch(service, repo, name="control", parent_account_id=live, role="control")
    other = _create_branch(service, repo, name="other", parent_account_id=live, role="control")
    for account_id in (live, control, other):
        _deposit(service, account_id, date(2026, 1, 5), 100000.0)
    _snapshots(db, live, {
        date(2026, 1, 5): 100000.0,
        date(2026, 1, 6): 101000.0,
    })
    _snapshots(db, control, {
        date(2026, 1, 5): 100000.0,
        date(2026, 1, 6): 102000.0,
    })
    _snapshots(db, other, {
        date(2026, 1, 5): 100000.0,
        date(2026, 1, 6): 99000.0,
    })

    payload = PaperBranchCompareService(repo=repo).compare(
        as_of=date(2026, 1, 6),
        account_ids=[control],
    )

    assert [row["account_id"] for row in payload["branches"]] == [control]
    candidate = payload["branches"][0]
    assert payload["window"]["reference_account_id"] == live
    assert candidate["metrics"]["excess_pct"] == pytest.approx(1.0)
    # Ranking pool only holds the selected candidates, never the live row.
    assert candidate["metrics"]["top_days"] == 2
