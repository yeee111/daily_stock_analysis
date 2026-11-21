# -*- coding: utf-8 -*-
"""Read-only comparison of paper-account branches and the 7 promotion gates.

DSA generates one signal output per batch; branches differ only in execution
discipline (whether to buy, sizing, stop/target, turnover). This service lines
up every branch's time-weighted equity curve on a common window, compares it
with the ``live`` reference and evaluates the shadowbranch promotion gates.

Gate thresholds mirror ``D:\\影子策略分支管理\\shadowbranch\\gating.py``. Gate 7
(parameter perturbation) is reported as ``na`` here: DSA branches do not expose
perturbable strategy parameters, their difference is the execution discipline.

The service is strictly read-only: ``build_equity_series`` is called with
``persist=False`` so the endpoint never writes daily snapshots.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_risk_service import PortfolioRiskService
from src.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)

DEFAULT_COMPARISON_THRESHOLDS: Dict[str, float] = {
    "min_days": 20.0,
    "min_trades": 8.0,
    "min_excess": 0.05,
    "max_mdd_ratio": 1.2,
    "min_rolling_win": 0.60,
    "min_top_days": 3.0,
    "rolling_window": 20.0,
    "perturb": 0.10,
}

GATE_NAMES = ("可比交易日", "成交笔数", "区间超额", "回撤比", "滚动胜率", "连续榜首", "参数稳健性")

# Absolute drawdown ceiling when the live reference barely drew down; the
# ratio is meaningless at that point (shadowbranch decision 13.8).
ABSOLUTE_MDD_CEILING = 0.10


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


class PaperBranchCompareService:
    """Compute per-branch TWR curves, excess metrics and promotion gates."""

    def __init__(
        self,
        *,
        repo: Optional[PortfolioRepository] = None,
        risk_service: Optional[PortfolioRiskService] = None,
        portfolio_service: Optional[PortfolioService] = None,
        thresholds: Optional[Mapping[str, float]] = None,
    ) -> None:
        self.repo = repo or PortfolioRepository()
        self.portfolio_service = portfolio_service or PortfolioService(repo=self.repo)
        self.risk_service = risk_service or PortfolioRiskService(
            repo=self.repo,
            portfolio_service=self.portfolio_service,
        )
        self.thresholds = dict(DEFAULT_COMPARISON_THRESHOLDS)
        if thresholds:
            self.thresholds.update({key: float(value) for key, value in thresholds.items()})

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def compare(
        self,
        *,
        as_of: Optional[date] = None,
        lookback_days: int = 180,
        cost_method: str = "fifo",
        account_ids: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        as_of_date = as_of or date.today()
        warnings: List[str] = []

        branches = self.repo.list_branch_accounts(auto_run_only=False, include_inactive=False)
        if not branches:
            return self._empty(as_of_date, cost_method, lookback_days, warnings)

        by_id = {int(row["account_id"]): row for row in branches}
        if account_ids:
            wanted = [int(item) for item in account_ids]
            unknown = [item for item in wanted if item not in by_id]
            if unknown:
                warnings.append("账户不在分支表内，已忽略：" + "、".join(str(item) for item in unknown))
            selected = [by_id[item] for item in wanted if item in by_id]
        else:
            selected = list(branches)
        if not selected:
            return self._empty(as_of_date, cost_method, lookback_days, warnings)

        reference = self._pick_reference(branches, warnings)
        reference_id = int(reference["account_id"]) if reference is not None else None
        if reference is None:
            warnings.append("没有 live 参照账户，只能展示各分支自身曲线，无法计算超额与转正门槛")

        # The reference takes part in the common-date intersection even when the
        # caller filters it out of ``selected``; excess needs both sides aligned.
        needed: Dict[int, Dict[str, Any]] = {int(row["account_id"]): row for row in selected}
        if reference is not None:
            needed.setdefault(int(reference["account_id"]), reference)

        curves: Dict[int, Dict[str, float]] = {}
        fx_stale = False
        for account_id in needed:
            navs, stale = self._twr_curve(
                account_id=account_id,
                as_of=as_of_date,
                cost_method=cost_method,
                lookback_days=lookback_days,
            )
            if not navs:
                warnings.append(f"账户 {account_id} 没有可用的历史净值快照，未参与对比")
            curves[account_id] = navs
            fx_stale = fx_stale or stale
        if fx_stale:
            warnings.append("部分汇率缺失，净值按最近可得汇率折算，对比结果可能偏低精度")

        window_start, window_end, common_dates = self._common_window(
            selected=selected,
            reference=reference,
            curves=curves,
        )
        if window_start is None or len(common_dates) < 2:
            warnings.append("分支之间没有足够的共同交易日，无法计算对比指标")
        elif window_start is not None:
            # Name the account that pushed the window forward, otherwise a
            # single late-starting branch silently shortens everyone's window.
            for row in needed.values():
                account_id = int(row["account_id"])
                navs = curves.get(account_id) or {}
                if not navs:
                    continue
                first_day = min(navs)
                if first_day > window_start.isoformat():
                    warnings.append(
                        f"账户 {account_id} 的快照从 {first_day} 才开始，公共窗口顺延至该日"
                    )

        rebased = {
            account_id: self._rebase(navs, common_dates)
            for account_id, navs in curves.items()
        }

        days = len(common_dates)
        returns = {
            account_id: self._window_return(rebased[account_id], common_dates)
            for account_id in needed
        }
        drawdowns = {
            account_id: self._window_drawdown(rebased[account_id], common_dates)
            for account_id in needed
        }
        trade_counts = {
            account_id: self._trade_count(
                account_id,
                start=window_start,
                end=as_of_date,
            )
            for account_id in needed
        }
        ref_navs = rebased.get(reference_id, {}) if reference_id is not None else {}
        pool_ids = [
            int(row["account_id"])
            for row in selected
            if reference_id is None or int(row["account_id"]) != reference_id
        ]

        rows: List[Dict[str, Any]] = []
        for row in selected:
            account_id = int(row["account_id"])
            is_reference = reference_id is not None and account_id == reference_id
            metrics = self._branch_metrics(
                account_id=account_id,
                is_reference=is_reference,
                days=days,
                ret=returns.get(account_id),
                ref_ret=returns.get(reference_id) if reference_id is not None else None,
                mdd=drawdowns.get(account_id),
                ref_mdd=drawdowns.get(reference_id) if reference_id is not None else None,
                trades=trade_counts.get(account_id, 0),
                branch_navs=rebased.get(account_id, {}),
                ref_navs=ref_navs,
                common_dates=common_dates,
                pool_ids=pool_ids,
                pool_navs=rebased,
            )
            gates: List[Dict[str, Any]] = []
            passed: Optional[bool] = None
            score: Optional[float] = None
            if not is_reference and reference_id is not None:
                gates = self._gates(metrics)
                passed = all(item["ok"] is not False for item in gates)
                score = self._score(metrics)

            rows.append({
                "account_id": account_id,
                "account_name": row.get("account_name"),
                "label": row.get("label"),
                "role": row.get("role"),
                "state": row.get("state"),
                "parent_account_id": row.get("parent_account_id"),
                "fork_date": row.get("fork_date").isoformat() if row.get("fork_date") else None,
                "profile_id": row.get("profile_id"),
                "profile_name": row.get("profile_name"),
                "profile_version": row.get("profile_version"),
                "auto_run": row.get("auto_run"),
                "is_reference": is_reference,
                "series": [
                    {
                        "date": day.isoformat(),
                        "nav": round(rebased.get(account_id, {}).get(day, 0.0), 6),
                    }
                    for day in common_dates
                    if day in rebased.get(account_id, {})
                ],
                "metrics": metrics,
                "gates": gates,
                "passed": passed,
                "score": score,
            })

        order = {row["account_id"]: index for index, row in enumerate(selected)}
        rows.sort(key=lambda item: (
            0 if item["is_reference"] else 1,
            order.get(item["account_id"], 0),
        ))

        return {
            "as_of": as_of_date.isoformat(),
            "cost_method": cost_method,
            "lookback_days": int(lookback_days),
            "thresholds": dict(self.thresholds),
            "window": {
                "start": window_start.isoformat() if window_start else None,
                "end": window_end.isoformat() if window_end else None,
                "days": days,
                "reference_account_id": reference_id,
            },
            "branches": rows,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Data assembly
    # ------------------------------------------------------------------
    def _twr_curve(
        self,
        *,
        account_id: int,
        as_of: date,
        cost_method: str,
        lookback_days: int,
    ) -> Tuple[Dict[str, float], bool]:
        """Time-weighted NAV (first snapshot = 100) plus an fx-stale flag.

        External cash flows (deposits/withdrawals) are removed from the daily
        return, so a cloned branch funded the day after its fork does not show
        a fake jump. ``persist=False`` keeps the call read-only.
        """
        series = self.risk_service.build_equity_series(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            lookback_days=lookback_days,
            include_realtime=False,
            persist=False,
        )
        points = series.get("points") or []
        if not points:
            return {}, bool(series.get("fx_stale"))

        flows = self._external_flows(account_id=account_id, as_of=as_of)
        navs: Dict[str, float] = {}
        previous_equity: Optional[float] = None
        nav = 1.0
        for point in points:
            day = str(point["date"])
            equity = float(point.get("equity") or 0.0)
            if previous_equity is None:
                navs[day] = 1.0
            else:
                flow = flows.get(day, 0.0)
                # A snapshot with no equity yet (branch created, cash not
                # injected) cannot produce a return; skip the division.
                daily = 0.0 if previous_equity <= 1e-9 else (equity - previous_equity - flow) / previous_equity
                nav *= 1.0 + daily
                navs[day] = nav
            previous_equity = equity
        return navs, bool(series.get("fx_stale"))

    def _external_flows(self, *, account_id: int, as_of: date) -> Dict[str, float]:
        flows: Dict[str, float] = {}
        for row in self.repo.list_cash_ledger(account_id, as_of):
            direction = str(getattr(row, "direction", "") or "").strip().lower()
            if direction not in {"in", "out"}:
                continue
            amount, _, _ = self.portfolio_service.convert_amount(
                amount=abs(float(getattr(row, "amount", 0.0) or 0.0)),
                from_currency=str(getattr(row, "currency", "CNY") or "CNY"),
                to_currency="CNY",
                as_of_date=row.event_date,
            )
            sign = 1.0 if direction == "in" else -1.0
            key = row.event_date.isoformat()
            flows[key] = flows.get(key, 0.0) + sign * amount
        return flows

    @staticmethod
    def _pick_reference(
        branches: List[Dict[str, Any]],
        warnings: List[str],
    ) -> Optional[Dict[str, Any]]:
        live = [row for row in branches if row.get("role") == "live"]
        if not live:
            return None
        if len(live) == 1:
            return live[0]
        trunk = [row for row in live if row.get("parent_account_id") is None]
        chosen = trunk[0] if trunk else live[0]
        warnings.append(
            "存在多个 live 分支，按账户 "
            f"{chosen.get('account_id')} 作为参照；请先修正分支角色"
        )
        return chosen

    @staticmethod
    def _common_window(
        *,
        selected: List[Dict[str, Any]],
        reference: Optional[Dict[str, Any]],
        curves: Dict[int, Dict[str, float]],
    ) -> Tuple[Optional[date], Optional[date], List[date]]:
        pool = list(selected)
        if reference is not None:
            pool.append(reference)
        fork_dates = [
            row["fork_date"]
            for row in pool
            if isinstance(row.get("fork_date"), date)
        ]
        # Branches funded on different days only become comparable once every
        # branch exists; the later fork date is the natural start of the window.
        start_floor = max(fork_dates) if fork_dates else None

        date_sets: List[set] = []
        for row in pool:
            account_id = int(row["account_id"])
            days = {date.fromisoformat(day) for day in curves.get(account_id, {})}
            date_sets.append(days)
        if not date_sets:
            return None, None, []
        common = set.intersection(*date_sets) if all(date_sets) else set()
        if start_floor is not None:
            common = {day for day in common if day >= start_floor}
        dates = sorted(common)
        if not dates:
            return None, None, []
        return dates[0], dates[-1], dates

    @staticmethod
    def _rebase(navs: Dict[str, float], common_dates: List[date]) -> Dict[date, float]:
        if not common_dates:
            return {}
        available = [day for day in common_dates if day.isoformat() in navs]
        if not available:
            return {}
        base = navs[available[0].isoformat()]
        if abs(base) < 1e-12:
            return {}
        return {day: navs[day.isoformat()] / base * 100.0 for day in available}

    @staticmethod
    def _window_return(rebased: Dict[date, float], common_dates: List[date]) -> Optional[float]:
        days = [day for day in common_dates if day in rebased]
        if len(days) < 2:
            return None
        return rebased[days[-1]] / rebased[days[0]] - 1.0

    @staticmethod
    def _window_drawdown(rebased: Dict[date, float], common_dates: List[date]) -> Optional[float]:
        days = [day for day in common_dates if day in rebased]
        if not days:
            return None
        peak = rebased[days[0]]
        worst = 0.0
        for day in days:
            value = rebased[day]
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, 1.0 - value / peak)
        return worst

    def _trade_count(self, account_id: int, *, start: Optional[date], end: date) -> int:
        if start is None:
            return 0
        trades = self.repo.list_trades(account_id, end)
        return sum(1 for trade in trades if start <= trade.trade_date <= end)

    @staticmethod
    def _daily_pairs(
        branch_navs: Dict[date, float],
        ref_navs: Dict[date, float],
        common_dates: List[date],
    ) -> List[Tuple[float, float]]:
        days = [day for day in common_dates if day in branch_navs and day in ref_navs]
        pairs: List[Tuple[float, float]] = []
        for previous, current in zip(days, days[1:]):
            b_prev, b_cur = branch_navs[previous], branch_navs[current]
            r_prev, r_cur = ref_navs[previous], ref_navs[current]
            b_ret = b_cur / b_prev - 1.0 if b_prev else 0.0
            r_ret = r_cur / r_prev - 1.0 if r_prev else 0.0
            pairs.append((b_ret, r_ret))
        return pairs

    def _rolling_win_rate(
        self,
        branch_navs: Dict[date, float],
        ref_navs: Dict[date, float],
        common_dates: List[date],
    ) -> Optional[float]:
        pairs = self._daily_pairs(branch_navs, ref_navs, common_dates)
        window = max(1, int(self.thresholds["rolling_window"]))
        pairs = pairs[-window:]
        if not pairs:
            return None
        return sum(1 for branch, ref in pairs if branch > ref) / len(pairs)

    def _tracking_deviation(
        self,
        branch_navs: Dict[date, float],
        ref_navs: Dict[date, float],
        common_dates: List[date],
    ) -> Optional[float]:
        pairs = self._daily_pairs(branch_navs, ref_navs, common_dates)
        if not pairs:
            return None
        diffs = [branch - ref for branch, ref in pairs]
        return math.sqrt(sum(diff * diff for diff in diffs) / len(diffs))

    @staticmethod
    def _top_streak(
        branch_id: int,
        branch_navs: Dict[date, float],
        pool_ids: Sequence[int],
        pool_navs: Dict[int, Dict[date, float]],
        common_dates: List[date],
    ) -> int:
        """Consecutive days the branch ranks first among same-origin forks.

        Ranking uses the cumulative return inside the common window (NAV at
        that day), exactly like the shadowbranch NAV ranking. Ties all count.
        """
        streak = 0
        for day in reversed(common_dates):
            if day not in branch_navs:
                break
            values = {
                account_id: pool_navs[account_id][day]
                for account_id in pool_ids
                if day in pool_navs.get(account_id, {})
            }
            if branch_id not in values:
                break
            if branch_navs[day] >= max(values.values()) - 1e-9:
                streak += 1
            else:
                break
        return streak

    # ------------------------------------------------------------------
    # Metrics and gates
    # ------------------------------------------------------------------
    def _branch_metrics(
        self,
        *,
        account_id: int,
        is_reference: bool,
        days: int,
        ret: Optional[float],
        ref_ret: Optional[float],
        mdd: Optional[float],
        ref_mdd: Optional[float],
        trades: int,
        branch_navs: Dict[date, float],
        ref_navs: Dict[date, float],
        common_dates: List[date],
        pool_ids: Sequence[int],
        pool_navs: Dict[int, Dict[date, float]],
    ) -> Dict[str, Any]:
        excess = None if is_reference or ref_ret is None or ret is None else ret - ref_ret
        rolling = None
        tracking = None
        top_days = 0
        if not is_reference and ref_navs:
            rolling = self._rolling_win_rate(branch_navs, ref_navs, common_dates)
            tracking = self._tracking_deviation(branch_navs, ref_navs, common_dates)
        if not is_reference and pool_ids:
            top_days = self._top_streak(
                account_id, branch_navs, pool_ids, pool_navs, common_dates
            )
        return {
            "days": days,
            "return_pct": round(ret * 100.0, 4) if ret is not None else None,
            "max_drawdown_pct": round(mdd * 100.0, 4) if mdd is not None else None,
            "trades": trades,
            "excess_pct": round(excess * 100.0, 4) if excess is not None else None,
            "excess": round(excess, 8) if excess is not None else None,
            "rolling_win": round(rolling, 6) if rolling is not None else None,
            "tracking_dev": round(tracking, 8) if tracking is not None else None,
            "top_days": top_days,
            "reference_max_drawdown_pct": (
                round(ref_mdd * 100.0, 4) if ref_mdd is not None else None
            ),
        }

    def _gates(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        cfg = self.thresholds
        days = int(metrics["days"] or 0)
        trades = int(metrics["trades"] or 0)
        excess = metrics.get("excess")
        branch_mdd = (
            metrics["max_drawdown_pct"] / 100.0
            if metrics.get("max_drawdown_pct") is not None
            else None
        )
        ref_mdd = (
            metrics["reference_max_drawdown_pct"] / 100.0
            if metrics.get("reference_max_drawdown_pct") is not None
            else None
        )
        rolling = metrics.get("rolling_win")
        top_days = int(metrics.get("top_days") or 0)

        items: List[Dict[str, Any]] = []

        ok1 = days >= int(cfg["min_days"])
        items.append(self._gate(
            GATE_NAMES[0], days, f"≥ {int(cfg['min_days'])}", ok1,
            (f"可比 {days} 个交易日，达标" if ok1
             else f"可比 {days} 个交易日，差 {int(cfg['min_days']) - days} 日"),
        ))

        ok2 = trades >= int(cfg["min_trades"])
        items.append(self._gate(
            GATE_NAMES[1], trades, f"≥ {int(cfg['min_trades'])}", ok2,
            (f"{trades} 笔成交，达标" if ok2
             else f"仅 {trades} 笔成交，差 {int(cfg['min_trades']) - trades} 笔（买/卖各计 1 笔）"),
        ))

        min_excess = float(cfg["min_excess"])
        ok3 = excess is not None and excess >= min_excess
        if excess is None:
            hint3 = "缺少可比的 live 净值"
        elif ok3:
            hint3 = f"超额 {_pct(excess)}，达标"
        else:
            hint3 = f"超额 {_pct(excess)}，差 {(min_excess - excess) * 100:.1f} 个百分点到 {min_excess * 100:.0f}%"
        items.append(self._gate(
            GATE_NAMES[2], excess, f"≥ {min_excess * 100:.0f}%", ok3, hint3,
        ))

        if branch_mdd is None or ref_mdd is None:
            items.append(self._gate(
                GATE_NAMES[3], None, f"≤ {float(cfg['max_mdd_ratio']):.2f}", False, "缺少回撤数据",
            ))
        elif ref_mdd < 1e-6:
            ok4 = branch_mdd <= ABSOLUTE_MDD_CEILING
            items.append(self._gate(
                GATE_NAMES[3], branch_mdd, "≤ 10%（live 回撤≈0）", ok4,
                (f"live 回撤≈0，按绝对值判定：分支回撤 {_pct(branch_mdd)}"
                 + ("，通过" if ok4 else "，超过 10% 上限")),
            ))
        else:
            ratio = branch_mdd / ref_mdd
            ok4 = ratio <= float(cfg["max_mdd_ratio"])
            items.append(self._gate(
                GATE_NAMES[3], ratio, f"≤ {float(cfg['max_mdd_ratio']):.2f}", ok4,
                (f"回撤比 {ratio:.2f}（分支 {_pct(branch_mdd)} / live {_pct(ref_mdd)}）"
                 + ("，达标" if ok4 else "，超过上限")),
            ))

        min_win = float(cfg["min_rolling_win"])
        ok5 = rolling is not None and rolling >= min_win
        if rolling is None:
            hint5 = "缺少可比的滚动净值"
        elif ok5:
            hint5 = f"最近 {int(cfg['rolling_window'])} 日胜率 {_pct(rolling)}，达标"
        else:
            hint5 = (f"最近 {int(cfg['rolling_window'])} 日胜率 {_pct(rolling)}，"
                     f"差 {(min_win - rolling) * 100:.1f} 个百分点")
        items.append(self._gate(
            GATE_NAMES[4], rolling, f"≥ {min_win * 100:.0f}%", ok5, hint5,
        ))

        ok6 = top_days >= int(cfg["min_top_days"])
        items.append(self._gate(
            GATE_NAMES[5], top_days, f"≥ {int(cfg['min_top_days'])} 天", ok6,
            (f"连续榜首 {top_days} 日，达标" if ok6
             else f"连续榜首 {top_days} 日（需 {int(cfg['min_top_days'])} 日），中断即归零"),
        ))

        items.append({
            "name": GATE_NAMES[6],
            "value": None,
            "threshold": f"扰动 ±{float(cfg['perturb']) * 100:.0f}% 超额仍 > 0",
            "ok": None,
            "status": "na",
            "hint": "DSA 分支差异在出场纪律而非策略参数，本门槛不适用，不影响通过判定",
        })
        return items

    @staticmethod
    def _gate(
        name: str,
        value: Optional[float],
        threshold: str,
        ok: bool,
        hint: str,
    ) -> Dict[str, Any]:
        return {
            "name": name,
            "value": value,
            "threshold": threshold,
            "ok": bool(ok),
            "status": "pass" if ok else "fail",
            "hint": hint,
        }

    def _score(self, metrics: Dict[str, Any]) -> float:
        cfg = self.thresholds
        min_excess = float(cfg["min_excess"])
        max_ratio = float(cfg["max_mdd_ratio"])
        min_trades = float(cfg["min_trades"])

        excess = metrics.get("excess") or 0.0
        n_excess = _clamp01(excess / (2 * min_excess)) if min_excess else 0.0

        branch_mdd = (
            metrics["max_drawdown_pct"] / 100.0
            if metrics.get("max_drawdown_pct") is not None
            else None
        )
        ref_mdd = (
            metrics["reference_max_drawdown_pct"] / 100.0
            if metrics.get("reference_max_drawdown_pct") is not None
            else None
        )
        if branch_mdd is not None and ref_mdd is not None and ref_mdd < 1e-6:
            n_dd = _clamp01(1.0 - branch_mdd / 0.20)
        elif branch_mdd is not None and ref_mdd is not None and max_ratio:
            ratio = branch_mdd / ref_mdd
            n_dd = _clamp01(1.0 - ratio / (2 * max_ratio))
        else:
            n_dd = 0.0

        rolling = metrics.get("rolling_win") or 0.0
        n_win = _clamp01((rolling - 0.5) / 0.3)
        # Gate 7 is N/A for DSA branches, so it contributes 0 to every score;
        # the ranking between candidates stays fair.
        n_rob = 0.0
        days = float(metrics.get("days") or 0)
        trades = float(metrics.get("trades") or 0)
        n_sample = (
            _clamp01(min(days / 40.0, trades / (2 * min_trades)))
            if min_trades
            else 0.0
        )
        return round(100.0 * (
            0.30 * n_excess + 0.20 * n_dd + 0.20 * n_win + 0.20 * n_rob + 0.10 * n_sample
        ), 1)

    def _empty(
        self,
        as_of: date,
        cost_method: str,
        lookback_days: int,
        warnings: List[str],
    ) -> Dict[str, Any]:
        warnings.append("还没有纸面账户分支，先创建分支账户或运行启动回填")
        return {
            "as_of": as_of.isoformat(),
            "cost_method": cost_method,
            "lookback_days": int(lookback_days),
            "thresholds": dict(self.thresholds),
            "window": {
                "start": None,
                "end": None,
                "days": 0,
                "reference_account_id": None,
            },
            "branches": [],
            "warnings": warnings,
        }
