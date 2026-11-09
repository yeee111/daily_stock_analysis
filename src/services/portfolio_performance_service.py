# -*- coding: utf-8 -*-
"""Portfolio performance service: round-trip stats, equity curve and benchmark."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from src.config import Config, get_config
from src.repositories.portfolio_repo import PortfolioRepository
from src.repositories.stock_repo import StockRepository
from src.services.decision_signal_service import DecisionSignalService
from src.services.market_data_sync_service import MarketDataSyncService, index_canonical_id
from src.services.portfolio_risk_service import PortfolioRiskService
from src.services.portfolio_service import PortfolioService
from src.storage import StockDaily

logger = logging.getLogger(__name__)

_QUERY_PAGE_SIZE = 500
_MIN_CLOSED_TRADES_FOR_STATS = 30
EPS = 1e-9
# Auto catch-up only refreshes the benchmark series; it runs on report requests,
# so repeated attempts for the same lagging day must not hammer the upstream.
_BENCHMARK_SYNC_RETRY_SECONDS = 300
_benchmark_sync_attempts: Dict[Tuple[str, date, date], float] = {}
_benchmark_sync_attempts_lock = threading.Lock()

_SIGNAL_NOTE_RE = re.compile(r"signal #(\d+)")
EXIT_REASON_LABELS = {
    "take_profit": "止盈触发",
    "stop_loss": "止损触发",
    "signal_sell": "信号清仓",
    "signal_reduce": "信号减仓",
    "level_exit": "止盈止损（信号缺失）",
    "manual": "手动/其他",
    "unknown": "无法判断（信号缺失）",
}
EXIT_REASON_ORDER = tuple(EXIT_REASON_LABELS)
EXIT_VERDICT_LABELS = {
    "locked_gain": "落袋正确（卖后回落）",
    "sold_early": "卖飞（卖后继续涨）",
    "avoided_loss": "躲对下跌（卖后继续跌）",
    "sold_low": "卖在低点（卖后反弹）",
    "breakeven": "平盘退出",
}


class PortfolioPerformanceService:
    """Compute trading performance blocks on top of replayed snapshot data.

    Round trips are always matched FIFO: a sell closes the oldest remaining
    lots of the same symbol/market, and one round trip is emitted per sell.
    """

    def __init__(
        self,
        *,
        repo: Optional[PortfolioRepository] = None,
        portfolio_service: Optional[PortfolioService] = None,
        risk_service: Optional[PortfolioRiskService] = None,
        stock_repo: Optional[StockRepository] = None,
        config: Optional[Config] = None,
        market_data_sync: Optional[MarketDataSyncService] = None,
        signal_service: Optional[DecisionSignalService] = None,
    ):
        self.repo = repo or PortfolioRepository()
        self.portfolio_service = portfolio_service or PortfolioService(repo=self.repo)
        self.risk_service = risk_service or PortfolioRiskService(
            repo=self.repo,
            portfolio_service=self.portfolio_service,
            config=config,
        )
        self.stock_repo = stock_repo
        self.config = config or get_config()
        self.market_data_sync = market_data_sync or MarketDataSyncService()
        self.signal_service = signal_service

    def get_performance_report(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
        cost_method: str = "fifo",
        include_realtime: bool = True,
    ) -> Dict[str, Any]:
        as_of_date = as_of or date.today()
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of_date,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )

        trades = self._fetch_trades(account_id=account_id, as_of=as_of_date)
        deposit_events = self._fetch_deposit_events(account_id=account_id, as_of=as_of_date)
        deposits = sum(amount for _, amount in deposit_events)
        round_trips, open_lots = self._match_round_trips(trades)

        closed = self._summarize_closed(round_trips)
        open_positions = self._summarize_open(
            open_lots,
            snapshot,
            as_of_date=as_of_date,
        )
        lookback_days = int(getattr(self.config, "portfolio_risk_lookback_days", 180))
        equity_series = self.risk_service.build_equity_series(
            account_id=account_id,
            as_of=as_of_date,
            cost_method=cost_method,
            lookback_days=lookback_days,
            include_realtime=include_realtime,
        )

        first_activity = self._first_activity_date(account_id=account_id, as_of=as_of_date)
        curve_points, total_return_pct = self._attach_return_pct(
            equity_series["points"], deposit_events
        )
        summary = self._build_summary(
            snapshot=snapshot,
            deposits=deposits,
            closed=closed,
            open_positions=open_positions,
            equity_series=equity_series,
            first_activity=first_activity,
            as_of_date=as_of_date,
            trade_count=len(trades),
            total_return_pct=total_return_pct,
        )
        benchmark = self._build_benchmark(
            equity_series=equity_series,
            total_return_pct=total_return_pct,
            as_of_date=as_of_date,
        )
        notes = self._build_notes(
            closed=closed,
            open_positions=open_positions,
            benchmark=benchmark,
            deposits=deposits,
            equity_points=len(equity_series["points"]),
        )

        return {
            "as_of": as_of_date.isoformat(),
            "account_id": account_id,
            "cost_method": cost_method,
            "currency": snapshot["currency"],
            "summary": summary,
            "closed_trades": closed,
            "open_positions": open_positions,
            "equity_curve": {
                "points": self._merge_benchmark_points(curve_points, benchmark),
                "max_drawdown_pct": equity_series["max_drawdown_pct"],
                "current_drawdown_pct": equity_series["current_drawdown_pct"],
                "fx_stale": equity_series["fx_stale"],
            },
            "benchmark": benchmark,
            "notes": notes,
        }

    def get_sell_review(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Exit attribution for every closed round trip: why it was sold,
        what the entry thesis was, and how the price moved after the sell.

        Read-only: FIFO pairing is shared with the performance report, exit
        reasons are recovered from the trade uid/note plus the referenced
        signal, and post-sell drift only reads local daily bars.
        """
        as_of_date = as_of or date.today()
        trades = self._fetch_trades(account_id=account_id, as_of=as_of_date)
        round_trips, _ = self._match_round_trips(trades)
        signals = self.signal_service or DecisionSignalService()
        signal_cache: Dict[int, Optional[Dict[str, Any]]] = {}

        def signal_of(note: Any) -> Optional[Dict[str, Any]]:
            match = _SIGNAL_NOTE_RE.search(str(note or ""))
            if not match:
                return None
            signal_id = int(match.group(1))
            if signal_id not in signal_cache:
                try:
                    signal_cache[signal_id] = signals.get_signal(signal_id) or None
                except Exception:  # noqa: BLE001 - a deleted signal must not break the review
                    signal_cache[signal_id] = None
            return signal_cache[signal_id]

        stock_repo = self.stock_repo or StockRepository()
        items = [
            self._sell_review_item(trip, signal_of=signal_of, stock_repo=stock_repo)
            for trip in round_trips
        ]
        items.sort(key=lambda row: (row["close_date"], row["stock_code"]), reverse=True)

        closed = self._summarize_closed(round_trips)
        by_reason: List[Dict[str, Any]] = []
        for reason in EXIT_REASON_ORDER:
            group = [row for row in items if row["exit_reason"] == reason]
            if not group:
                continue
            wins = sum(1 for row in group if row["pnl"] > EPS)
            returns = [row["return_pct"] for row in group if row["return_pct"] is not None]
            by_reason.append(
                {
                    "reason": reason,
                    "label": EXIT_REASON_LABELS[reason],
                    "count": len(group),
                    "wins": wins,
                    "losses": sum(1 for row in group if row["pnl"] < -EPS),
                    "win_rate_pct": round(wins / len(group) * 100.0, 2),
                    "total_pnl": round(sum(row["pnl"] for row in group), 2),
                    "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
                    "avg_holding_days": round(
                        sum(row["holding_days"] for row in group) / len(group), 1
                    ),
                }
            )

        verdicts = [row["exit_verdict"] for row in items if row["exit_verdict"]]
        exit_quality = {"evaluated": len(verdicts)}
        for code in EXIT_VERDICT_LABELS:
            exit_quality[code] = verdicts.count(code)

        winners = [row for row in items if row["pnl"] > EPS]
        losses = [row for row in items if row["pnl"] < -EPS]
        top_winners = sorted(
            winners,
            key=lambda row: row["return_pct"] if row["return_pct"] is not None else float("-inf"),
            reverse=True,
        )[:3]
        top_losers = sorted(
            losses,
            key=lambda row: row["return_pct"] if row["return_pct"] is not None else float("inf"),
        )[:3]

        notes: List[str] = []
        if not items:
            notes.append("尚无已平仓交易，卖出复盘为空")
        elif len(items) < _MIN_CLOSED_TRADES_FOR_STATS:
            notes.append(
                f"已平仓仅 {len(items)} 笔（< {_MIN_CLOSED_TRADES_FOR_STATS} 笔），结论仅供方向性参考"
            )
        pending = len(items) - exit_quality["evaluated"]
        if pending > 0:
            notes.append(f"{pending} 笔卖出后本地日线不足，卖后走势待数据积累")
        notes.append(
            "退出原因按成交 uid（-exit 为止盈止损委托）与信号回读判定；"
            "卖后走势对比本地前复权日线，若期间除权除息可能有偏差"
        )

        return {
            "as_of": as_of_date.isoformat(),
            "account_id": account_id,
            "summary": {
                "count": closed["count"],
                "wins": closed["wins"],
                "losses": closed["losses"],
                "win_rate_pct": closed["win_rate_pct"],
                "realized_pnl": closed["realized_pnl"],
                "avg_return_pct": closed["avg_return_pct"],
                "avg_holding_days": closed["avg_holding_days"],
            },
            "by_reason": by_reason,
            "exit_quality": exit_quality,
            "top_winners": top_winners,
            "top_losers": top_losers,
            "items": items,
            "notes": notes,
        }

    def _sell_review_item(
        self,
        trip: Dict[str, Any],
        *,
        signal_of: Any,
        stock_repo: StockRepository,
    ) -> Dict[str, Any]:
        sell_note = str(trip.get("sell_note") or "")
        sell_signal = signal_of(sell_note)
        entry_signal = None
        for note in trip.get("open_notes") or []:
            entry_signal = signal_of(note)
            if entry_signal is not None:
                break
        reason = self._classify_exit(
            uid=str(trip.get("sell_trade_uid") or ""),
            signal=sell_signal,
            price=float(trip.get("avg_price") or 0.0),
            has_signal_ref=bool(_SIGNAL_NOTE_RE.search(sell_note)),
        )
        level_signal = entry_signal or sell_signal or {}
        drift = self._post_sell_drift(
            stock_repo=stock_repo,
            code=str(trip["stock_code"]),
            close_date=date.fromisoformat(trip["close_date"]),
            sell_price=float(trip.get("avg_price") or 0.0),
        )
        verdict, horizon = self._exit_verdict(pnl=float(trip["pnl"]), drift=drift)
        entry_reason = str((entry_signal or {}).get("reason") or "").strip()
        return {
            "stock_code": trip["stock_code"],
            "stock_name": (entry_signal or sell_signal or {}).get("stock_name"),
            "market": trip["market"],
            "open_date": trip["open_date"],
            "close_date": trip["close_date"],
            "holding_days": trip["holding_days"],
            "quantity": trip["quantity"],
            "avg_cost": trip["avg_cost"],
            "avg_price": trip["avg_price"],
            "pnl": trip["pnl"],
            "return_pct": trip["return_pct"],
            "exit_reason": reason,
            "exit_reason_label": EXIT_REASON_LABELS[reason],
            "entry_signal_id": (entry_signal or {}).get("id"),
            "entry_score": (entry_signal or {}).get("score"),
            "entry_reason": entry_reason[:160] or None,
            "stop_loss": self._positive_level(level_signal.get("stop_loss")),
            "target_price": self._positive_level(level_signal.get("target_price")),
            **drift,
            "exit_verdict": verdict,
            "exit_verdict_label": EXIT_VERDICT_LABELS.get(verdict) if verdict else None,
            "verdict_horizon": horizon,
        }

    @staticmethod
    def _positive_level(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @classmethod
    def _classify_exit(
        cls,
        *,
        uid: str,
        signal: Optional[Dict[str, Any]],
        price: float,
        has_signal_ref: bool,
    ) -> str:
        """Recover the exit mechanism from the fill, not from a stored label.

        Resting stop/target sells carry the ``-exit`` uid tag and reference the
        signal that opened the position; the fill side of the level separates
        stop-loss from take-profit (a stop fills at or below the stop, a target
        at or above the target, modulo slippage which only hurts the fill).
        """
        action = str((signal or {}).get("action") or "").strip().lower()
        if uid.endswith("-exit") or action in {"buy", "add"}:
            stop = cls._positive_level((signal or {}).get("stop_loss"))
            target = cls._positive_level((signal or {}).get("target_price"))
            if stop is not None and price <= stop + 1e-6:
                return "stop_loss"
            if target is not None:
                return "take_profit"
            if stop is not None:
                return "stop_loss"
            return "level_exit"
        if action in {"sell", "avoid"}:
            return "signal_sell"
        if action in {"reduce", "alert"}:
            return "signal_reduce"
        if not has_signal_ref:
            return "manual"
        return "unknown"

    @staticmethod
    def _post_sell_drift(
        *,
        stock_repo: StockRepository,
        code: str,
        close_date: date,
        sell_price: float,
    ) -> Dict[str, Any]:
        rows = stock_repo.get_range(
            code, close_date + timedelta(days=1), close_date + timedelta(days=45)
        ) or []
        bars = sorted(
            (
                row
                for row in rows
                if getattr(row, "date", None) is not None
                and row.date > close_date
                and getattr(row, "close", None)
                and float(row.close) > 0
            ),
            key=lambda row: row.date,
        )
        result: Dict[str, Any] = {"post_sell_sessions": len(bars)}
        for horizon in (5, 10, 20):
            key = f"post_sell_{horizon}d_pct"
            if len(bars) >= horizon and sell_price > 0:
                result[key] = round(
                    (float(bars[horizon - 1].close) / sell_price - 1.0) * 100.0, 2
                )
            else:
                result[key] = None
        return result

    @staticmethod
    def _exit_verdict(
        *, pnl: float, drift: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[int]]:
        """Judge the exit on the longest post-sell window available locally."""
        for horizon in (20, 10, 5):
            value = drift.get(f"post_sell_{horizon}d_pct")
            if value is None:
                continue
            if pnl > EPS:
                verdict = "locked_gain" if value < 0 else "sold_early"
            elif pnl < -EPS:
                verdict = "avoided_loss" if value < 0 else "sold_low"
            else:
                verdict = "breakeven"
            return verdict, horizon
        return None, None

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------
    def _fetch_trades(self, *, account_id: Optional[int], as_of: date) -> List[Any]:
        rows: List[Any] = []
        page = 1
        while True:
            batch, total = self.repo.query_trades(
                account_id=account_id,
                date_from=None,
                date_to=as_of,
                symbols=None,
                side=None,
                page=page,
                page_size=_QUERY_PAGE_SIZE,
            )
            rows.extend(batch)
            if not batch or page * _QUERY_PAGE_SIZE >= int(total or 0):
                break
            page += 1
        rows.sort(key=lambda row: (row.trade_date, row.id))
        return rows

    def _fetch_deposit_events(self, *, account_id: Optional[int], as_of: date) -> List[tuple[date, float]]:
        events: List[tuple[date, float]] = []
        page = 1
        while True:
            batch, total = self.repo.query_cash_ledger(
                account_id=account_id,
                date_from=None,
                date_to=as_of,
                direction=None,
                page=page,
                page_size=_QUERY_PAGE_SIZE,
            )
            for row in batch:
                amount = float(row.amount or 0.0)
                signed = amount if str(row.direction).lower() == "in" else -amount
                events.append((row.event_date, signed))
            if not batch or page * _QUERY_PAGE_SIZE >= int(total or 0):
                break
            page += 1
        events.sort(key=lambda item: item[0])
        return events

    @staticmethod
    def _attach_return_pct(
        points: List[Dict[str, Any]],
        deposit_events: List[tuple[date, float]],
    ) -> tuple[List[Dict[str, Any]], Optional[float]]:
        """Return per-point time-weighted return and the final period return.

        Daily chain-linking with cash flows taken at the day's close keeps the
        curve independent of deposit/withdrawal timing, which a simple
        equity/net-deposits ratio would distort. Points before the first flow
        have no base and stay ``None``.
        """
        flows_by_date: Dict[str, float] = {}
        for event_date, amount in deposit_events:
            key = event_date.isoformat()
            flows_by_date[key] = flows_by_date.get(key, 0.0) + amount

        merged: List[Dict[str, Any]] = []
        factor = 1.0
        prev_equity = 0.0
        started = False
        final_pct: Optional[float] = None
        for point in points:
            equity = float(point["equity"])
            flow = flows_by_date.get(point["date"], 0.0)
            if not started:
                if equity > EPS or abs(flow) > EPS:
                    started = True
                    return_pct: Optional[float] = 0.0
                else:
                    return_pct = None
            elif prev_equity > EPS:
                factor *= (equity - flow) / prev_equity
                return_pct = round((factor - 1.0) * 100.0, 4)
            else:
                return_pct = None
            if return_pct is not None:
                final_pct = return_pct
            row = dict(point)
            row["return_pct"] = return_pct
            merged.append(row)
            prev_equity = equity
        return merged, final_pct

    def _first_activity_date(self, *, account_id: Optional[int], as_of: date) -> Optional[date]:
        if account_id is not None:
            return self.repo.get_first_activity_date(account_id=account_id, as_of=as_of)
        candidates = [
            first
            for first in (
                self.repo.get_first_activity_date(account_id=int(account.id), as_of=as_of)
                for account in self.repo.list_accounts(include_inactive=False)
            )
            if first is not None
        ]
        return min(candidates) if candidates else None

    # ------------------------------------------------------------------
    # Round trips
    # ------------------------------------------------------------------
    def _match_round_trips(
        self,
        trades: List[Any],
    ) -> tuple[List[Dict[str, Any]], Dict[tuple[str, str], List[Dict[str, Any]]]]:
        lots: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        round_trips: List[Dict[str, Any]] = []

        for trade in trades:
            key = (str(trade.symbol or "").strip().upper(), str(trade.market or "").strip().lower())
            quantity = float(trade.quantity or 0.0)
            price = float(trade.price or 0.0)
            fee = float(trade.fee or 0.0)
            tax = float(trade.tax or 0.0)
            if quantity <= 0 or price <= 0:
                continue
            bucket = lots.setdefault(key, [])
            side = str(trade.side or "").strip().lower()

            if side == "buy":
                bucket.append(
                    {
                        "open_date": trade.trade_date,
                        "remaining_quantity": quantity,
                        "unit_cost": (quantity * price + fee + tax) / quantity,
                        "currency": str(trade.currency or "CNY"),
                        "note": str(trade.note or ""),
                    }
                )
                continue

            if side != "sell":
                continue

            proceeds_net = quantity * price - fee - tax
            remaining = quantity
            cost_basis = 0.0
            open_date = trade.trade_date
            currency = str(trade.currency or "CNY")
            open_notes: List[str] = []
            while remaining > EPS and bucket:
                lot = bucket[0]
                taken = min(remaining, float(lot["remaining_quantity"]))
                cost_basis += taken * float(lot["unit_cost"])
                open_date = min(open_date, lot["open_date"])
                currency = lot["currency"]
                if lot.get("note"):
                    open_notes.append(str(lot["note"]))
                lot["remaining_quantity"] = float(lot["remaining_quantity"]) - taken
                remaining -= taken
                if lot["remaining_quantity"] <= EPS:
                    bucket.pop(0)
            if remaining > EPS:
                # Oversell against the recorded lots; keep the round trip but
                # only attribute the cost we could actually match.
                logger.warning(
                    "Performance round trip for %s on %s exceeded available lots by %.4f",
                    key[0],
                    trade.trade_date,
                    remaining,
                )
            sold_quantity = quantity - remaining if remaining > EPS else quantity
            pnl_local = proceeds_net - cost_basis
            return_pct = (pnl_local / cost_basis * 100.0) if cost_basis > EPS else None
            pnl_cny, stale, _ = self.portfolio_service.convert_amount(
                amount=pnl_local,
                from_currency=currency,
                to_currency="CNY",
                as_of_date=trade.trade_date,
            )
            round_trips.append(
                {
                    "stock_code": key[0],
                    "market": key[1],
                    "currency": currency,
                    "open_date": open_date.isoformat(),
                    "close_date": trade.trade_date.isoformat(),
                    "holding_days": (trade.trade_date - open_date).days,
                    "quantity": round(sold_quantity, 6),
                    "avg_cost": round(cost_basis / sold_quantity, 6) if sold_quantity > EPS else None,
                    "avg_price": round(price, 6),
                    "cost_basis": round(cost_basis, 6),
                    "proceeds": round(proceeds_net, 6),
                    "pnl": round(pnl_cny, 6),
                    "return_pct": round(return_pct, 4) if return_pct is not None else None,
                    "fx_stale": bool(stale),
                    "sell_trade_uid": str(trade.trade_uid or ""),
                    "sell_note": str(trade.note or ""),
                    "open_notes": open_notes,
                }
            )

        open_lots = {key: [lot for lot in value if float(lot["remaining_quantity"]) > EPS] for key, value in lots.items()}
        return round_trips, {key: value for key, value in open_lots.items() if value}

    def _summarize_closed(self, round_trips: List[Dict[str, Any]]) -> Dict[str, Any]:
        count = len(round_trips)
        if count == 0:
            return {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": None,
                "avg_return_pct": None,
                "avg_win_pct": None,
                "avg_loss_pct": None,
                "profit_factor": None,
                "realized_pnl": 0.0,
                "avg_holding_days": None,
                "max_holding_days": None,
                "best": None,
                "worst": None,
                "items": [],
            }

        returns = [row["return_pct"] for row in round_trips if row["return_pct"] is not None]
        wins = [row for row in round_trips if row["pnl"] > EPS]
        losses = [row for row in round_trips if row["pnl"] < -EPS]
        win_pnl = sum(row["pnl"] for row in wins)
        loss_pnl = sum(row["pnl"] for row in losses)
        holding_days = [int(row["holding_days"]) for row in round_trips]
        best = max(round_trips, key=lambda row: (row["return_pct"] if row["return_pct"] is not None else row["pnl"]))
        worst = min(round_trips, key=lambda row: (row["return_pct"] if row["return_pct"] is not None else row["pnl"]))

        return {
            "count": count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / count * 100.0, 2),
            "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "avg_win_pct": round(
                sum(row["return_pct"] for row in wins if row["return_pct"] is not None) / len(wins), 4
            ) if wins else None,
            "avg_loss_pct": round(
                sum(row["return_pct"] for row in losses if row["return_pct"] is not None) / len(losses), 4
            ) if losses else None,
            "profit_factor": round(win_pnl / abs(loss_pnl), 4) if loss_pnl < -EPS else None,
            "realized_pnl": round(sum(row["pnl"] for row in round_trips), 6),
            "avg_holding_days": round(sum(holding_days) / count, 2),
            "max_holding_days": max(holding_days),
            "best": best,
            "worst": worst,
            "items": sorted(round_trips, key=lambda row: row["close_date"], reverse=True),
        }

    def _summarize_open(
        self,
        open_lots: Dict[tuple[str, str], List[Dict[str, Any]]],
        snapshot: Dict[str, Any],
        *,
        as_of_date: date,
    ) -> Dict[str, Any]:
        positions_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
        for account in snapshot.get("accounts", []):
            for position in account.get("positions", []):
                key = (
                    str(position.get("symbol") or "").strip().upper(),
                    str(position.get("market") or "").strip().lower(),
                )
                positions_by_key[key] = position

        total_market_value = float(snapshot.get("total_market_value") or 0.0)
        items: List[Dict[str, Any]] = []
        holding_days_list: List[int] = []
        for key, lots in open_lots.items():
            position = positions_by_key.get(key, {})
            earliest = min(lot["open_date"] for lot in lots)
            holding_days = (as_of_date - earliest).days
            holding_days_list.append(holding_days)
            quantity = float(position.get("quantity") or 0.0)
            market_value = float(position.get("market_value_base") or 0.0)
            cost_basis = float(position.get("total_cost") or 0.0)
            unrealized = float(position.get("unrealized_pnl_base") or 0.0)
            return_pct = position.get("unrealized_pnl_pct")
            items.append(
                {
                    "stock_code": key[0],
                    "market": key[1],
                    "currency": str(position.get("currency") or "CNY"),
                    "open_date": earliest.isoformat(),
                    "holding_days": holding_days,
                    "quantity": round(quantity, 6),
                    "avg_cost": position.get("avg_cost"),
                    "last_price": position.get("last_price"),
                    "cost_basis": round(cost_basis, 6),
                    "market_value": round(market_value, 6),
                    "unrealized_pnl": round(unrealized, 6),
                    "return_pct": round(float(return_pct), 4) if return_pct is not None else None,
                    "weight_pct": round(market_value / total_market_value * 100.0, 4)
                    if total_market_value > EPS
                    else None,
                    "price_stale": bool(position.get("price_stale")),
                }
            )

        items.sort(key=lambda row: (row["market_value"] or 0.0), reverse=True)
        returns = [row["return_pct"] for row in items if row["return_pct"] is not None]
        return {
            "count": len(items),
            "unrealized_pnl": round(float(snapshot.get("unrealized_pnl") or 0.0), 6),
            "avg_holding_days": round(sum(holding_days_list) / len(holding_days_list), 2)
            if holding_days_list
            else None,
            "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "items": items,
        }

    # ------------------------------------------------------------------
    # Summary / benchmark / notes
    # ------------------------------------------------------------------
    def _build_summary(
        self,
        *,
        snapshot: Dict[str, Any],
        deposits: float,
        closed: Dict[str, Any],
        open_positions: Dict[str, Any],
        equity_series: Dict[str, Any],
        first_activity: Optional[date],
        as_of_date: date,
        trade_count: int,
        total_return_pct: Optional[float],
    ) -> Dict[str, Any]:
        equity = float(snapshot.get("total_equity") or 0.0)
        cash = float(snapshot.get("total_cash") or 0.0)
        market_value = float(snapshot.get("total_market_value") or 0.0)
        total_pnl = equity - deposits
        return {
            "net_deposits": round(deposits, 6),
            "total_equity": round(equity, 6),
            "total_cash": round(cash, 6),
            "total_market_value": round(market_value, 6),
            "total_pnl": round(total_pnl, 6),
            "total_return_pct": total_return_pct,
            "realized_pnl": round(float(snapshot.get("realized_pnl") or 0.0), 6),
            "unrealized_pnl": round(float(snapshot.get("unrealized_pnl") or 0.0), 6),
            "fee_total": round(float(snapshot.get("fee_total") or 0.0), 6),
            "tax_total": round(float(snapshot.get("tax_total") or 0.0), 6),
            "invested_ratio_pct": round(market_value / equity * 100.0, 4) if equity > EPS else None,
            "first_activity_date": first_activity.isoformat() if first_activity else None,
            "days_active": (as_of_date - first_activity).days + 1 if first_activity else None,
            "max_drawdown_pct": equity_series["max_drawdown_pct"],
            "current_drawdown_pct": equity_series["current_drawdown_pct"],
            "position_count": int(open_positions["count"]),
            "closed_trade_count": int(closed["count"]),
            "trade_count": int(trade_count),
            "fx_stale": bool(snapshot.get("fx_stale")),
        }

    @staticmethod
    def _load_index_closes(
        stock_repo: StockRepository,
        symbol: str,
        start_date: date,
        as_of_date: date,
    ) -> List[tuple[date, float]]:
        """Read index closes, accepting either the configured code or its prefixed index key.

        Index bars are stored under the prefixed form (``sh000300``) because a bare
        index code collides with the stock bucket of the same digits (000001 = 平安银行).
        """
        keys = [str(symbol or "").strip()]
        index_key = index_canonical_id(symbol)
        if index_key:
            keys.append(index_key)
        for key in dict.fromkeys(item for item in keys if item):
            bars = stock_repo.get_range(key, start_date, as_of_date)
            closes = [
                (bar.date, float(bar.close))
                for bar in bars
                if getattr(bar, "close", None) and float(bar.close) > 0
            ]
            if closes:
                closes.sort(key=lambda item: item[0])
                return closes
        return []

    def refresh_market_data(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Force-sync benchmark index bars and backfill holdings with no local price."""
        as_of_date = as_of or date.today()
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of_date,
            cost_method="fifo",
            include_realtime=False,
        )
        missing_codes: List[str] = []
        for account in snapshot.get("accounts", []):
            for position in account.get("positions", []):
                code = str(position.get("symbol") or "").strip()
                if code and not bool(position.get("price_available", True)):
                    missing_codes.append(code)
        symbol = str(getattr(self.config, "portfolio_performance_benchmark_symbol", "") or "").strip()
        index_sync = (
            self.market_data_sync.sync_index_bars(symbol, as_of=as_of_date) if symbol else None
        )
        return {
            "as_of": as_of_date.isoformat(),
            "positions": self.market_data_sync.sync_symbol_bars(
                sorted(set(missing_codes)), as_of=as_of_date
            ),
            "benchmark": index_sync,
        }

    @staticmethod
    def _local_latest_stock_date(
        stock_repo: StockRepository,
        as_of_date: date,
    ) -> Optional[date]:
        """Latest A-share bar date in the local DB, bounded by the report's as_of."""
        try:
            with stock_repo.db.get_session() as session:
                return session.execute(
                    select(func.max(StockDaily.date)).where(
                        StockDaily.date <= as_of_date,
                        func.length(StockDaily.code) == 6,
                    )
                ).scalar()
        except Exception as exc:  # noqa: BLE001 - staleness check must not break reporting
            logger.warning("Local latest bar date lookup failed: %s", exc)
            return None

    def _benchmark_lags_local_bars(
        self,
        *,
        stock_repo: StockRepository,
        closes: List[Tuple[date, float]],
        as_of_date: date,
        symbol: str,
    ) -> bool:
        """True when the benchmark series ends before the newest local stock bar.

        Index and stock bars share the same trading calendar, so a benchmark that
        stops earlier is stale and worth one top-up fetch. Attempts are throttled
        per (symbol, as_of, local latest date) because the dashboard re-requests
        the report, while upstream may lag or be unreachable for a while.
        """
        local_latest = self._local_latest_stock_date(stock_repo, as_of_date)
        if local_latest is None:
            return False
        if closes and closes[-1][0] >= local_latest:
            return False
        key = (symbol, as_of_date, local_latest)
        now = time.monotonic()
        with _benchmark_sync_attempts_lock:
            last_attempt = _benchmark_sync_attempts.get(key)
            if last_attempt is not None and now - last_attempt < _BENCHMARK_SYNC_RETRY_SECONDS:
                return False
            _benchmark_sync_attempts[key] = now
        return True

    def _build_benchmark(
        self,
        *,
        equity_series: Dict[str, Any],
        total_return_pct: Optional[float],
        as_of_date: date,
    ) -> Dict[str, Any]:
        symbol = str(getattr(self.config, "portfolio_performance_benchmark_symbol", "") or "").strip()
        points = equity_series["points"]
        unavailable = {
            "available": False,
            "symbol": symbol or None,
            "return_pct": None,
            "excess_return_pct": None,
            "series": [],
            "note": None,
        }
        if not symbol:
            unavailable["note"] = "未配置基准指数（PORTFOLIO_PERFORMANCE_BENCHMARK_SYMBOL），不做对比"
            return unavailable
        if not points:
            unavailable["note"] = "暂无净值序列，无法对齐基准区间"
            return unavailable

        start_date = date.fromisoformat(points[0]["date"])
        stock_repo = self.stock_repo or StockRepository()
        closes = self._load_index_closes(stock_repo, symbol, start_date, as_of_date)
        sync_enabled = bool(getattr(self.config, "portfolio_market_data_sync_enabled", True))
        should_sync = len(closes) < 2
        if sync_enabled and not should_sync:
            should_sync = self._benchmark_lags_local_bars(
                stock_repo=stock_repo,
                closes=closes,
                as_of_date=as_of_date,
                symbol=symbol,
            )
        if sync_enabled and should_sync:
            sync = self.market_data_sync.sync_index_bars(symbol, as_of=as_of_date)
            if not sync["error"]:
                closes = self._load_index_closes(stock_repo, symbol, start_date, as_of_date)
        if len(closes) < 2:
            unavailable["note"] = f"本地无 {symbol} 的指数日线（{start_date} ~ {as_of_date}），基准对比暂缺"
            return unavailable

        base_close = closes[0][1]
        series = [
            {
                "date": bar_date.isoformat(),
                "close": round(close, 6),
                "return_pct": round((close / base_close - 1.0) * 100.0, 4),
            }
            for bar_date, close in closes
        ]
        benchmark_return = series[-1]["return_pct"]
        excess = (
            round(total_return_pct - benchmark_return, 4)
            if total_return_pct is not None
            else None
        )
        return {
            "available": True,
            "symbol": symbol,
            "return_pct": benchmark_return,
            "excess_return_pct": excess,
            "series": series,
            "note": None,
        }

    @staticmethod
    def _merge_benchmark_points(
        points: List[Dict[str, Any]],
        benchmark: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        by_date = (
            {row["date"]: row for row in benchmark.get("series", [])}
            if benchmark.get("available")
            else {}
        )
        merged: List[Dict[str, Any]] = []
        for point in points:
            row = dict(point)
            benchmark_row = by_date.get(row["date"])
            row["benchmark_return_pct"] = benchmark_row["return_pct"] if benchmark_row else None
            merged.append(row)
        return merged

    @staticmethod
    def _build_notes(
        *,
        closed: Dict[str, Any],
        open_positions: Dict[str, Any],
        benchmark: Dict[str, Any],
        deposits: float,
        equity_points: int,
    ) -> List[str]:
        notes: List[str] = []
        if deposits <= EPS:
            notes.append("未发现入金记录，收益率无法计算（分母为 0）")
        if closed["count"] == 0:
            notes.append("尚无已平仓交易，胜率/盈亏比/平均持有天数均为空")
        elif closed["count"] < _MIN_CLOSED_TRADES_FOR_STATS:
            notes.append(
                f"已平仓交易仅 {closed['count']} 笔（< {_MIN_CLOSED_TRADES_FOR_STATS} 笔），"
                "胜率与盈亏比暂无统计意义"
            )
        if open_positions["count"] == 0:
            notes.append("当前无持仓，浮动盈亏为 0")
        if equity_points < 2:
            notes.append("净值序列不足 2 个交易日，最大回撤暂不可靠")
        if benchmark.get("note"):
            notes.append(str(benchmark["note"]))
        notes.append("收益率为时间加权（日度链式，出入金按当日收盘计），不受出入金时点影响；总盈亏 = 总权益 − 净入金")
        notes.append("成交按 T+1 开盘价撮合，未模拟排队/滑点，实盘结果会偏保守")
        return notes
