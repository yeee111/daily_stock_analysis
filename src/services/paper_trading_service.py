# -*- coding: utf-8 -*-
"""Paper trading runner: due DecisionSignal batches -> portfolio trades (A-share).

This module owns the A-share execution rules that the portfolio service
deliberately does not implement (T+1, 100-share lots, limit-up/down at open,
A-share fees). It never writes ``decision_signals`` and never bypasses
``PortfolioService.record_trade``; trades are ordinary portfolio events, so the
existing ``/portfolio`` page, snapshot replay and risk views keep working.

Timing model
------------
Signal day = ``created_at`` date (a natural day; weekend analysis is normal).
Fill day = first trading session strictly after the signal day, filled at the
opening price. A run processes only the batch whose fill day equals the latest
A-share daily-bar date, so newer daily signals cannot starve an older unfilled
batch. When the fill-day bar is not available yet the run refuses to guess a
price and reports ``data_not_ready``.

The open-price model can optionally be made more conservative (both default to
off, so existing ledgers stay comparable): ``PAPER_TRADING_SLIPPAGE_BPS`` moves
every fill against the account and clamps it to the daily price limit, while
``PAPER_TRADING_MAX_ADV_PCT`` caps an order at that percentage of the symbol's
``PAPER_TRADING_ADV_WINDOW_DAYS``-day average volume.

Position sizing uses only ``reference_date`` (= previous session) closes and
holdings as of ``reference_date``; fill-day prices are used solely to execute.
Sells therefore can never touch shares bought on the fill day (T+1).

Signal -> action (the only strategy assumption)
-----------------------------------------------
``buy`` / ``add``      open or increase to an equal-weight target
``reduce`` / ``alert`` sell half of the position (rounded down to 100 shares)
``sell`` / ``avoid``   close the whole position
``hold`` / ``watch``   no-op

Exit rules and carry-over
-------------------------
A position opened by a paper buy keeps that signal's ``stop_loss`` /
``target_price`` as resting exit levels: it is sold on the first session whose
low/high (or previous close, for a stop that was blocked) breaches a level,
filled at the level or the opening gap, whichever is worse. When both levels
trigger on the same bar the stop wins, because daily bars carry no intraday
ordering.

Orders blocked by market mechanics (opening at limit-up/limit-down, suspended)
are carried to the next session, but only while every skipped session was
unfillable and the wait stays within ``PAPER_TRADING_RETRY_DAYS`` sessions.
Carried orders keep their intended fill day in ``trade_uid`` so retries stay
idempotent.

Cash dividends and bonus/transfer shares are registered automatically for
symbols the account has traded, before planning. Daily bars are 前复权, so
without the corporate-action rows a dividend would silently disappear from the
account's P&L.

Board tradability
-----------------
New positions are limited to the boards a real brokerage account can actually
buy (``PAPER_TRADING_ALLOWED_BOARDS``, default ``main,gem``). STAR Market,
Beijing Stock Exchange and Hong Kong signals are reported and skipped instead
of being traded with A-share lot/limit rules that do not apply. Sells and
resting exits on existing holdings are never filtered.

Idempotency: every trade carries ``trade_uid = paper-<fill_date>-<code>-<side>``
scoped to the account, and a batch with existing same-day fills is refused
unless ``force=True``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select

from src.core.trading_calendar import get_next_trading_date, get_trading_dates
from src.repositories.stock_repo import StockRepository
from src.services.decision_signal_service import DecisionSignalService
from src.services.portfolio_service import (
    PortfolioConflictError,
    PortfolioOversellError,
    PortfolioService,
)
from src.storage import StockDaily

logger = logging.getLogger(__name__)

BUY_ACTIONS = frozenset({"buy", "add"})
REDUCE_ACTIONS = frozenset({"reduce", "alert"})
CLOSE_ACTIONS = frozenset({"sell", "avoid"})
NOOP_ACTIONS = frozenset({"hold", "watch"})
EXIT_ACTIONS = frozenset({"stop_loss", "take_profit"})

DEFAULT_HORIZONS: Tuple[str, ...] = ("3d", "5d")
DEFAULT_ACCOUNT_NAME = "纸面交易"
DEFAULT_INITIAL_CASH = 100_000.0
DEFAULT_MAX_NAMES = 5
DEFAULT_MAX_WEIGHT = 0.2
DEFAULT_MIN_SCORE = 60
DEFAULT_FETCH_MISSING_BARS = False
DEFAULT_RETRY_DAYS = 3
DEFAULT_CORPORATE_ACTIONS = True
DEFAULT_CATCHUP_DAYS = 5
DEFAULT_ALLOWED_BOARDS: Tuple[str, ...] = ("main", "gem")

_BOARD_LABELS = {
    "main": "主板",
    "gem": "创业板",
    "star": "科创板",
    "bse": "北交所",
    "hk": "港股",
    "unknown": "未知板块",
}

PAPER_TRADING_ACCOUNT_NAME_ENV = "PAPER_TRADING_ACCOUNT_NAME"
PAPER_TRADING_INITIAL_CASH_ENV = "PAPER_TRADING_INITIAL_CASH"
PAPER_TRADING_MIN_SCORE_ENV = "PAPER_TRADING_MIN_SCORE"
PAPER_TRADING_MAX_NAMES_ENV = "PAPER_TRADING_MAX_NAMES"
PAPER_TRADING_MAX_WEIGHT_ENV = "PAPER_TRADING_MAX_WEIGHT"
PAPER_TRADING_HORIZONS_ENV = "PAPER_TRADING_HORIZONS"
PAPER_TRADING_AUTO_RUN_ENV = "PAPER_TRADING_AUTO_RUN"
PAPER_TRADING_AUTO_APPLY_ENV = "PAPER_TRADING_AUTO_APPLY"
PAPER_TRADING_FETCH_MISSING_BARS_ENV = "PAPER_TRADING_FETCH_MISSING_BARS"
PAPER_TRADING_RETRY_DAYS_ENV = "PAPER_TRADING_RETRY_DAYS"
PAPER_TRADING_CORPORATE_ACTIONS_ENV = "PAPER_TRADING_CORPORATE_ACTIONS"
PAPER_TRADING_ALLOWED_BOARDS_ENV = "PAPER_TRADING_ALLOWED_BOARDS"
PAPER_TRADING_CATCHUP_DAYS_ENV = "PAPER_TRADING_CATCHUP_DAYS"
PAPER_TRADING_SLIPPAGE_BPS_ENV = "PAPER_TRADING_SLIPPAGE_BPS"
PAPER_TRADING_MAX_ADV_PCT_ENV = "PAPER_TRADING_MAX_ADV_PCT"
PAPER_TRADING_ADV_WINDOW_DAYS_ENV = "PAPER_TRADING_ADV_WINDOW_DAYS"

DEFAULT_SLIPPAGE_BPS = 0.0
DEFAULT_MAX_ADV_PCT = 0.0
DEFAULT_ADV_WINDOW_DAYS = 20

_MISSING_BAR_FETCH_DAYS = 45

STATUS_PLANNED = "planned"
STATUS_EXECUTED = "executed"
STATUS_NOTHING_TO_DO = "nothing_to_do"
STATUS_NO_ACTIVE_SIGNALS = "no_active_signals"
STATUS_NO_DUE_BATCH = "no_due_batch"
STATUS_DATA_NOT_READY = "data_not_ready"
STATUS_DUPLICATE_BATCH = "duplicate_batch"

_CENT = Decimal("0.01")
_COMMISSION_RATE = Decimal("0.00025")
_COMMISSION_MIN = Decimal("5.00")
_STAMP_RATE = Decimal("0.0005")
_TRANSFER_RATE = Decimal("0.00001")

_BOARD_LIMIT_PCT = {
    "main": Decimal("0.10"),
    "star": Decimal("0.20"),
    "gem": Decimal("0.20"),
    "bse": Decimal("0.30"),
    "unknown": Decimal("0.10"),
}


def _env_text(name: str, default: str) -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def to_cent(value: Any) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def compute_fees(side: str, gross: Decimal) -> Tuple[Decimal, Decimal]:
    """Return ``(fee, tax)`` for an A-share fill.

    ``fee`` is commission plus transfer fee; ``tax`` is the sell-side stamp
    duty. These are the two amounts ``PortfolioService.record_trade`` expects.
    """

    gross = to_cent(gross)
    commission = to_cent(gross * _COMMISSION_RATE)
    if commission < _COMMISSION_MIN:
        commission = to_cent(_COMMISSION_MIN)
    transfer = to_cent(gross * _TRANSFER_RATE)
    stamp = to_cent(gross * _STAMP_RATE) if side == "sell" else Decimal("0.00")
    return to_cent(commission + transfer), stamp


def _plain_code(symbol: str) -> str:
    """Strip exchange decoration so ``SH601668`` / ``601668.SH`` -> ``601668``."""

    raw = str(symbol or "").strip().upper()
    if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
        return raw[2:]
    if "." in raw:
        base, suffix = raw.rsplit(".", 1)
        if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
            return base
    return raw.zfill(6) if raw.isdigit() else raw


def _board_of(code: str) -> str:
    raw = str(code or "").strip().upper()
    if raw.startswith("HK") or raw.endswith(".HK"):
        return "hk"
    code = _plain_code(code)
    if code.startswith("688"):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith(("60", "00")):
        return "main"
    if code.startswith(("4", "8")):
        return "bse"
    return "unknown"


def price_limit_pct(code: str) -> Decimal:
    return _BOARD_LIMIT_PCT[_board_of(code)]


def allowed_boards() -> frozenset:
    """Boards the account can open positions on, from ``PAPER_TRADING_ALLOWED_BOARDS``."""

    raw = _env_text(PAPER_TRADING_ALLOWED_BOARDS_ENV, ",".join(DEFAULT_ALLOWED_BOARDS))
    boards = {part.strip().lower() for part in raw.split(",") if part.strip()}
    invalid = boards - set(_BOARD_LABELS)
    if invalid:
        logger.warning(
            "Unknown %s entries %s; allowed values are %s",
            PAPER_TRADING_ALLOWED_BOARDS_ENV,
            sorted(invalid),
            list(_BOARD_LABELS),
        )
    boards &= set(_BOARD_LABELS)
    return frozenset(boards) or frozenset(DEFAULT_ALLOWED_BOARDS)


def _boards_label(boards: Iterable[str]) -> str:
    return "/".join(label for board, label in _BOARD_LABELS.items() if board in boards)


def _limit_prices(prev_close: Decimal, limit_pct: Decimal) -> Tuple[Decimal, Decimal]:
    prev_close = to_cent(prev_close)
    up = to_cent(prev_close * (Decimal("1") + limit_pct))
    down = to_cent(prev_close * (Decimal("1") - limit_pct))
    return up, down


def is_limit_up(prev_close: Decimal, price: Decimal, limit_pct: Decimal) -> bool:
    up, _ = _limit_prices(prev_close, limit_pct)
    return to_cent(price) >= up


def is_limit_down(prev_close: Decimal, price: Decimal, limit_pct: Decimal) -> bool:
    _, down = _limit_prices(prev_close, limit_pct)
    return to_cent(price) <= down


def clamp_buy_qty(code: str, qty: int) -> int:
    """Round a buy quantity down to a legal board lot; 0 means not tradable."""

    qty = int(qty)
    board = _board_of(code)
    if board == "star":
        return qty if qty >= 200 else 0
    if board == "bse":
        return qty if qty >= 100 else 0
    return (qty // 100) * 100 if qty >= 100 else 0


def affordable_qty(code: str, cash: Decimal, price: Decimal) -> int:
    if cash <= 0 or price <= 0:
        return 0
    # Fees are a function of the gross amount; keep a small cushion here and
    # re-check the exact fee before committing the order.
    budget = to_cent(cash * Decimal("0.995"))
    return clamp_buy_qty(code, int(budget / to_cent(price)))


def slippage_bps() -> float:
    """Per-side slippage in basis points; 0 (default) keeps the open-price model."""

    bps = _env_float(PAPER_TRADING_SLIPPAGE_BPS_ENV, DEFAULT_SLIPPAGE_BPS)
    if bps < 0:
        logger.warning("Invalid %s=%r; using 0", PAPER_TRADING_SLIPPAGE_BPS_ENV, bps)
        return 0.0
    return bps


def max_adv_pct() -> float:
    """Max share of average daily volume per order, in percent; 0 disables the cap."""

    pct = _env_float(PAPER_TRADING_MAX_ADV_PCT_ENV, DEFAULT_MAX_ADV_PCT)
    if pct < 0:
        logger.warning("Invalid %s=%r; using 0", PAPER_TRADING_MAX_ADV_PCT_ENV, pct)
        return 0.0
    return pct


def apply_slippage(
    price: float,
    side: str,
    *,
    code: str = "",
    previous_close: Optional[float] = None,
    bps: float = 0.0,
) -> float:
    """Move the fill against us and keep it inside the daily price limit.

    Buys pay more, sells receive less. The result is quantized to the A-share
    tick (0.01) and clamped to the limit-up/limit-down price derived from the
    previous close, because a slippage model must never create an illegal fill.
    """

    if bps <= 0 or price is None or float(price) <= 0:
        return price
    factor = Decimal(str(bps)) / Decimal("10000")
    raw = Decimal(str(price))
    if str(side).lower() == "buy":
        raw *= Decimal("1") + factor
        if previous_close:
            up, _ = _limit_prices(Decimal(str(previous_close)), price_limit_pct(code))
            raw = min(raw, up)
    else:
        raw *= Decimal("1") - factor
        if previous_close:
            _, down = _limit_prices(Decimal(str(previous_close)), price_limit_pct(code))
            raw = max(raw, down)
    return float(to_cent(raw))


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _parse_signal_day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_iso_date(value: Any) -> Optional[date]:
    return _parse_signal_day(value)


def _positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _signal_id_from_note(note: Any) -> Optional[int]:
    match = re.search(r"signal #(\d+)", str(note or ""))
    return int(match.group(1)) if match else None


def _side_for_action(action: Any) -> Optional[str]:
    normalized = str(action or "").strip().lower()
    if normalized in BUY_ACTIONS:
        return "buy"
    if normalized in REDUCE_ACTIONS or normalized in CLOSE_ACTIONS:
        return "sell"
    return None


_INTERNAL_ORDER_KEYS = ("uid_date", "uid_tag")


def _public_order(order: Dict[str, Any]) -> Dict[str, Any]:
    """Drop planning-only keys so responses stay JSON-serializable."""
    return {key: value for key, value in order.items() if key not in _INTERNAL_ORDER_KEYS}


def _previous_trading_date(market: str, day: date) -> Optional[date]:
    sessions = get_trading_dates(market, day - timedelta(days=31), day - timedelta(days=1))
    if sessions is None or len(sessions) == 0:
        return None
    last = sessions[-1]
    return last.date() if hasattr(last, "date") else last


class PaperTradingService:
    """Turn due active decision signals into simulated A-share trades."""

    def __init__(
        self,
        *,
        portfolio_service: Optional[PortfolioService] = None,
        signal_service: Optional[DecisionSignalService] = None,
        stock_repo: Optional[StockRepository] = None,
        dividend_fetcher: Optional[Any] = None,
    ) -> None:
        self.portfolio = portfolio_service or PortfolioService()
        self.signals = signal_service or DecisionSignalService()
        self.stock_repo = stock_repo or StockRepository()
        self._dividend_fetcher = dividend_fetcher

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        account_id: Optional[int] = None,
        account_name: Optional[str] = None,
        market: str = "cn",
        horizons: Optional[Sequence[str]] = None,
        min_score: Optional[int] = None,
        max_names: Optional[int] = None,
        max_weight: Optional[float] = None,
        signal_date: Optional[date] = None,
        dry_run: bool = True,
        force: bool = False,
        initial_cash: Optional[float] = None,
        batch_fill_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Plan (and optionally apply) the paper loop for one run.

        ``batch_fill_date`` is internal: it pins the run to exactly that fill
        day and disables catch-up. The public catch-up pass calls ``run`` once
        per missed fill day that way, so every batch keeps its own open price,
        account state and idempotent ``trade_uid``.
        """
        market = (market or "cn").strip().lower()
        if market != "cn":
            raise ValueError("paper trading currently supports A-share market 'cn' only")
        if account_id is not None and int(account_id) <= 0:
            raise ValueError("account_id must be > 0")

        account_name = (account_name or _env_text(PAPER_TRADING_ACCOUNT_NAME_ENV, DEFAULT_ACCOUNT_NAME)).strip()
        horizon_set = {
            str(item).strip()
            for item in (horizons if horizons is not None else _default_horizons())
            if str(item).strip()
        }
        min_score = int(min_score if min_score is not None else _env_int(PAPER_TRADING_MIN_SCORE_ENV, DEFAULT_MIN_SCORE))
        max_names = int(max_names if max_names is not None else _env_int(PAPER_TRADING_MAX_NAMES_ENV, DEFAULT_MAX_NAMES))
        max_weight = float(
            max_weight if max_weight is not None else _env_float(PAPER_TRADING_MAX_WEIGHT_ENV, DEFAULT_MAX_WEIGHT)
        )
        initial_cash = float(
            initial_cash
            if initial_cash is not None
            else _env_float(PAPER_TRADING_INITIAL_CASH_ENV, DEFAULT_INITIAL_CASH)
        )
        if max_names < 1:
            raise ValueError("max_names must be >= 1")
        if not 0 < max_weight <= 1:
            raise ValueError("max_weight must be in (0, 1]")
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")

        warnings: List[str] = []
        retry_days = max(0, int(_env_int(PAPER_TRADING_RETRY_DAYS_ENV, DEFAULT_RETRY_DAYS)))
        auto_corporate_actions = _env_bool(PAPER_TRADING_CORPORATE_ACTIONS_ENV, DEFAULT_CORPORATE_ACTIONS)
        slippage = slippage_bps()
        adv_pct = max_adv_pct()
        adv_window_days = max(1, int(_env_int(PAPER_TRADING_ADV_WINDOW_DAYS_ENV, DEFAULT_ADV_WINDOW_DAYS)))

        active = self._fetch_active_signals(market=market, horizons=horizon_set, warnings=warnings)
        signal_counts = {"active": len(active), "batch": 0, "carried": 0}
        latest_data_date = self._latest_market_date()
        corporate_actions: List[str] = []

        # A run may still have work to do (exit rules) when no signal is due, so
        # the old early returns are captured here and only used once planning
        # produced nothing.
        early_result: Optional[Dict[str, Any]] = None
        catchup_payload: Optional[Dict[str, Any]] = None
        batch: Dict[str, Dict[str, Any]] = {}
        by_fill: Dict[date, Dict[str, Dict[str, Any]]] = {}
        fill_date: Optional[date] = None

        def finish(payload: Dict[str, Any]) -> Dict[str, Any]:
            return self._merge_catchup(payload, catchup_payload)

        if not active:
            early_result = self._result(
                STATUS_NO_ACTIVE_SIGNALS,
                f"没有活跃信号（market={market}, horizons={sorted(horizon_set)}）；先跑一次分析。",
                market=market,
                dry_run=dry_run,
                signal_counts=signal_counts,
                latest_data_date=latest_data_date,
                warnings=list(warnings),
            )
            if signal_date is not None or latest_data_date is None:
                return finish(early_result)
            fill_date = latest_data_date
        else:
            for signal in active:
                signal_day = _parse_signal_day(signal.get("created_at"))
                if signal_day is None:
                    warnings.append(f"信号 #{signal.get('id')} 缺少可解析的 created_at，已跳过")
                    continue
                fill_day = get_next_trading_date(market, signal_day)
                if fill_day is None:
                    warnings.append("交易日历不可用，无法确定成交日")
                    continue
                bucket = by_fill.setdefault(fill_day, {})
                code = _plain_code(signal.get("stock_code"))
                current = bucket.get(code)
                if current is None or str(signal.get("created_at") or "") > str(current.get("created_at") or ""):
                    bucket[code] = signal

            if signal_date is None and batch_fill_date is None and latest_data_date is not None:
                catchup_payload = self._run_catchup_batches(
                    by_fill=by_fill,
                    latest_data_date=latest_data_date,
                    account_id=account_id,
                    account_name=account_name,
                    market=market,
                    horizons=sorted(horizon_set),
                    min_score=min_score,
                    max_names=max_names,
                    max_weight=max_weight,
                    dry_run=dry_run,
                    force=force,
                    initial_cash=initial_cash,
                )

            if signal_date is not None:
                batch_signal_day = signal_date
                for signal in active:
                    if _parse_signal_day(signal.get("created_at")) != batch_signal_day:
                        continue
                    code = _plain_code(signal.get("stock_code"))
                    current = batch.get(code)
                    if current is None or str(signal.get("created_at") or "") > str(current.get("created_at") or ""):
                        batch[code] = signal
                if not batch:
                    return finish(self._result(
                        STATUS_NO_DUE_BATCH,
                        f"{batch_signal_day} 没有活跃信号（该批可能已过期）。",
                        market=market,
                        dry_run=dry_run,
                        latest_data_date=latest_data_date,
                        signal_counts=signal_counts,
                        warnings=warnings,
                    ))
                fill_date = get_next_trading_date(market, batch_signal_day)
            elif batch_fill_date is not None:
                fill_date = batch_fill_date
                batch = by_fill.get(fill_date, {})
                if not batch:
                    return finish(self._result(
                        STATUS_NO_DUE_BATCH,
                        f"{fill_date} 没有可补跑的活跃信号。",
                        market=market,
                        dry_run=dry_run,
                        latest_data_date=latest_data_date,
                        signal_counts=signal_counts,
                        warnings=warnings,
                    ))
            else:
                fill_date = latest_data_date
                batch = by_fill.get(fill_date, {}) if fill_date is not None else {}

                if not batch:
                    pending = sorted(day for day in by_fill if latest_data_date is None or day > latest_data_date)
                    overdue = sorted(day for day in by_fill if latest_data_date is not None and day < latest_data_date)
                    if pending and not overdue:
                        # 只有未来批次：不是「没信号」，而是那天的行情还没到。
                        count = sum(len(by_fill[day]) for day in pending)
                        return finish(self._result(
                            STATUS_DATA_NOT_READY,
                            f"成交日 {pending[0]} 的行情还没到（A 股日线最新 {latest_data_date}）—— 不下单。"
                            f"待成交 {count} 只；先跑一次分析更新日线，再重跑。",
                            market=market,
                            dry_run=dry_run,
                            latest_data_date=latest_data_date,
                            signal_counts=signal_counts,
                            warnings=warnings,
                        ))
                    hints: List[str] = []
                    if pending:
                        count = sum(len(by_fill[day]) for day in pending)
                        hints.append(f"待成交 {count} 只，最早成交日 {pending[0]}（等那天的行情到）")
                    if overdue:
                        count = sum(len(by_fill[day]) for day in overdue)
                        hints.append(
                            f"还有 {len(overdue)} 个交易日、{count} 只信号未补跑，超出单次上限，"
                            "下次运行继续"
                        )
                    early_result = self._result(
                        STATUS_NO_DUE_BATCH,
                        f"最新数据日 {latest_data_date} 没有到期的信号批次 —— 今天不操作。",
                        market=market,
                        dry_run=dry_run,
                        latest_data_date=latest_data_date,
                        signal_counts=signal_counts,
                        warnings=warnings + hints,
                    )
                    warnings = list(warnings) + hints

        if fill_date is None:
            return finish(self._result(
                STATUS_DATA_NOT_READY,
                "这批信号之后交易日历里没有新的交易日，无法确定成交日。",
                market=market,
                dry_run=dry_run,
                latest_data_date=latest_data_date,
                signal_counts=signal_counts,
                warnings=warnings,
            ))

        reference_date = _previous_trading_date(market, fill_date)
        if reference_date is None:
            return finish(self._result(
                STATUS_DATA_NOT_READY,
                "交易日历不可用，无法确定参考收盘日。",
                market=market,
                dry_run=dry_run,
                latest_data_date=latest_data_date,
                signal_counts=signal_counts,
                warnings=warnings,
            ))

        if latest_data_date is None or fill_date > latest_data_date:
            pending_days = sorted(
                {
                    day
                    for day in (_parse_signal_day(signal.get("created_at")) for signal in batch.values())
                    if day is not None
                }
            )
            return finish(self._result(
                STATUS_DATA_NOT_READY,
                f"成交日 {fill_date} 的行情还没到（A 股日线最新 {latest_data_date}）—— 不下单。"
                "先跑一次分析更新日线，再重跑。",
                market=market,
                dry_run=dry_run,
                latest_data_date=latest_data_date,
                signal_counts=signal_counts,
                batch={
                    "signal_dates": [day.isoformat() for day in pending_days],
                    "fill_date": fill_date.isoformat(),
                    "reference_date": reference_date.isoformat(),
                },
                warnings=warnings,
            ))

        account, account_created = self._resolve_account(
            account_id=account_id,
            account_name=account_name,
            dry_run=dry_run,
        )
        account_pk = account.get("id")

        # Dividends/splits first: the reference-date snapshot must already carry
        # the cash and share adjustments, otherwise position sizing uses a stale
        # equity base.
        if not dry_run and auto_corporate_actions and account_pk is not None:
            corporate_actions = self._sync_corporate_actions(
                account_id=int(account_pk),
                fill_date=fill_date,
                warnings=warnings,
            )

        paper_trades = self._paper_trades(account_pk, as_of=fill_date, warnings=warnings)
        carry_days: Dict[str, date] = {}
        # Carried orders and resting exits are evaluated per session; the
        # catch-up pass runs every missed session in order, so a blocked order
        # can retry on the next session instead of jumping straight to today.
        if signal_date is None:
            carry_days = self._merge_carried_signals(
                batch=batch,
                by_fill=by_fill,
                fill_date=fill_date,
                market=market,
                existing_uids={
                    str(getattr(trade, "trade_uid", "") or "") for trade in paper_trades
                },
                retry_days=retry_days,
                warnings=warnings,
            )
        signal_counts["carried"] = len(carry_days)

        batch_days = sorted(
            {
                day
                for day in (_parse_signal_day(signal.get("created_at")) for signal in batch.values())
                if day is not None
            }
        )
        signal_counts["batch"] = len(batch)
        batch_payload = {
            "signal_dates": [day.isoformat() for day in batch_days],
            "fill_date": fill_date.isoformat(),
            "reference_date": reference_date.isoformat(),
        }

        if _env_bool(PAPER_TRADING_FETCH_MISSING_BARS_ENV, DEFAULT_FETCH_MISSING_BARS):
            self._ensure_daily_bars(
                codes=self._batch_and_position_codes(account=account, batch=batch),
                dates=(reference_date, fill_date),
                warnings=warnings,
            )

        cash, positions, equity = self._load_state(
            account=account,
            reference_date=reference_date,
            market=market,
            initial_cash=initial_cash,
            warnings=warnings,
        )
        per_name = to_cent(equity * Decimal(str(min(1.0 / max_names, max_weight))))
        held_names = sum(1 for position in positions.values() if int(position["quantity"]) > 0)

        plan = self._build_plan(
            batch=batch,
            fill_date=fill_date,
            reference_date=reference_date,
            cash=cash,
            positions=positions,
            per_name=per_name,
            max_names=max_names,
            min_score=min_score,
            warnings=warnings,
            trades=paper_trades if signal_date is None else None,
            carry_days=carry_days,
            allowed_boards=allowed_boards(),
            slippage_bps=slippage,
            max_adv_pct=adv_pct,
            adv_window_days=adv_window_days,
        )
        items, orders, cash_after_plan = plan
        portfolio_payload = {
            "cash": float(to_cent(cash)),
            "market_value": float(to_cent(equity - cash)),
            "equity": float(to_cent(equity)),
            "per_name": float(per_name),
            "held_names": held_names,
            "cash_after_plan": float(to_cent(cash_after_plan)),
        }
        account_payload = {
            "id": account.get("id"),
            "name": account.get("name"),
            "created": account_created,
            "initial_cash": float(to_cent(initial_cash)) if account.get("id") is None else None,
        }

        base = {
            "market": market,
            "dry_run": dry_run,
            "latest_data_date": latest_data_date.isoformat() if latest_data_date else None,
            "signal_counts": signal_counts,
            "account": account_payload,
            "batch": batch_payload,
            "portfolio": portfolio_payload,
            "items": items,
            "warnings": warnings,
            "corporate_actions": corporate_actions,
        }

        if not orders:
            exit_items = [row for row in items if str(row.get("action")) in EXIT_ACTIONS]
            if early_result is not None and not exit_items:
                if corporate_actions:
                    early_result["corporate_actions"] = corporate_actions
                    early_result["message"] += f" 顺带自动登记分红送转 {len(corporate_actions)} 笔。"
                return finish(early_result)
            return finish(self._result(
                STATUS_NOTHING_TO_DO,
                "本批信号没有产生可执行委托。"
                + (f" 自动登记分红送转 {len(corporate_actions)} 笔。" if corporate_actions else ""),
                orders=[],
                **base,
            ))

        if dry_run:
            cost_note = ""
            if slippage > 0 or adv_pct > 0:
                parts = []
                if slippage > 0:
                    parts.append(f"滑点 {slippage:g}bp")
                if adv_pct > 0:
                    parts.append(f"单笔不超过 {adv_window_days} 日均量 {adv_pct:g}%")
                cost_note = "，含" + "、".join(parts)
            return finish(self._result(
                STATUS_PLANNED,
                f"干跑：本批 {len(orders)} 笔委托将按 {fill_date} 开盘价成交{cost_note}"
                "（加 dry_run=false 执行）。",
                orders=[
                    {
                        **_public_order(order),
                        "trade_uid": self._trade_uid(
                            order.get("uid_date") or fill_date,
                            order["stock_code"],
                            order["side"],
                            tag=str(order.get("uid_tag") or ""),
                        ),
                        "status": "planned",
                        "trade_id": None,
                        "reason": None,
                    }
                    for order in orders
                ],
                **base,
            ))

        if account_created:
            account = self._create_account(account_name=account_name, initial_cash=initial_cash, reference_date=reference_date)
            account_payload["id"] = account.get("id")
            base["account"] = account_payload

        existing_same_day = self._same_day_fills(int(account["id"]), fill_date)
        if existing_same_day and not force:
            return finish(self._result(
                STATUS_DUPLICATE_BATCH,
                f"账户在 {fill_date} 已有 {len(existing_same_day)} 笔成交 —— 拒绝重复下单。"
                "确认要再来一次请传 force=true（trade_uid 幂等的委托仍会跳过）。",
                orders=[],
                **base,
            ))

        executed = self._execute_orders(account_id=int(account["id"]), fill_date=fill_date, orders=orders, warnings=warnings)
        base["warnings"] = warnings
        self._refresh_snapshot(account_id=int(account["id"]), fill_date=fill_date, warnings=warnings)

        succeeded = sum(1 for order in executed if order["status"] == "executed")
        duplicates = sum(1 for order in executed if order["status"] == "duplicate")
        failed = sum(1 for order in executed if order["status"] == "failed")
        message = f"执行完成：成交 {succeeded} / 重复跳过 {duplicates} / 失败 {failed}。"
        if corporate_actions:
            message += f" 自动登记分红送转 {len(corporate_actions)} 笔。"
        return finish(self._result(
            STATUS_EXECUTED if succeeded else STATUS_NOTHING_TO_DO,
            message,
            orders=executed,
            **base,
        ))

    # ------------------------------------------------------------------
    # Signal + market data
    # ------------------------------------------------------------------
    def _fetch_active_signals(
        self,
        *,
        market: str,
        horizons: Iterable[str],
        warnings: List[str],
    ) -> List[Dict[str, Any]]:
        horizon_set = set(horizons)
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            payload = self.signals.list_signals(
                market=market,
                status="active",
                page=page,
                page_size=100,
            )
            batch = payload.get("items") or []
            items.extend(batch)
            total = int(payload.get("total") or 0)
            if not batch or len(items) >= total:
                break
            page += 1
        filtered = [
            item
            for item in items
            if not horizon_set or str(item.get("horizon") or "") in horizon_set
        ]
        dropped = len(items) - len(filtered)
        if dropped:
            warnings.append(f"按 horizon={sorted(horizon_set)} 过滤掉 {dropped} 条活跃信号")
        return filtered

    def _latest_market_date(self) -> Optional[date]:
        with self.stock_repo.db.get_session() as session:
            return session.execute(
                select(func.max(StockDaily.date)).where(func.length(StockDaily.code) == 6)
            ).scalar()

    def _bar(self, code: str, day: date) -> Optional[StockDaily]:
        return self.stock_repo.get_daily_on_date(code=_plain_code(code), target_date=day)

    def _adv_cap_qty(
        self,
        *,
        code: str,
        fill_date: date,
        window_days: int,
        pct: float,
        cache: Dict[str, Optional[int]],
    ) -> Optional[int]:
        """Largest board-lot quantity staying within ``pct``% of average volume.

        Bars up to the session before ``fill_date`` are used so the cap never
        looks at the fill day's own volume. ``None`` means "no usable volume
        data" and leaves the order untouched instead of silently blocking it.
        """

        if pct <= 0:
            return None
        normalized = _plain_code(code)
        if normalized in cache:
            return cache[normalized]
        start = fill_date - timedelta(days=max(window_days * 2, 30))
        rows = self.stock_repo.get_range(normalized, start, fill_date - timedelta(days=1)) or []
        bars = [
            row
            for row in rows
            if getattr(row, "date", None) is not None
            and getattr(row, "date") < fill_date
            and getattr(row, "volume", None)
            and float(row.volume) > 0
        ]
        bars.sort(key=lambda row: row.date)
        bars = bars[-window_days:]
        if not bars:
            cache[normalized] = None
            return None
        adv = sum(float(row.volume) for row in bars) / len(bars)
        cache[normalized] = (int(adv * pct / 100.0) // 100) * 100
        return cache[normalized]

    def _batch_and_position_codes(
        self,
        *,
        account: Dict[str, Any],
        batch: Dict[str, Dict[str, Any]],
    ) -> List[str]:
        """Codes whose bars this run may need: the due batch plus held names.

        Held names come from the cached position table because the batch may
        only contain ``sell`` signals while the position itself was opened by
        an earlier batch (the snapshot needs its reference-date close too).
        """

        codes = {_plain_code(signal.get("stock_code")) for signal in batch.values()}
        codes = {code for code in codes if code}
        account_id = account.get("id")
        if account_id is not None:
            repo = getattr(self.portfolio, "repo", None)
            lister = getattr(repo, "list_cached_position_identities", None)
            if callable(lister):
                try:
                    identities = lister(account_id=int(account_id)) or []
                except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
                    logger.warning("Paper trading could not list cached positions: %s", exc)
                    identities = []
                for market, symbol in identities:
                    if str(market or "").strip().lower() != "cn":
                        continue
                    code = _plain_code(symbol)
                    if code:
                        codes.add(code)
        return sorted(codes)

    def _ensure_daily_bars(
        self,
        *,
        codes: Iterable[str],
        dates: Iterable[date],
        warnings: List[str],
    ) -> None:
        """Prefetch bars the batch needs but ``stock_daily`` lacks.

        Analysis only persists bars for its watchlist, while screening picks
        can be any A-share name, so a missing bar would silently skip an
        otherwise valid order. Fetching is best-effort: every failure becomes a
        warning and the planner still decides per code.
        """

        db = getattr(self.stock_repo, "db", None)
        if db is None or not hasattr(db, "save_daily_data"):
            return
        wanted = sorted({day for day in dates if day is not None})
        if not wanted:
            return
        end = wanted[-1]
        missing = [code for code in codes if any(self._bar(code, day) is None for day in wanted)]
        if not missing:
            return

        from src.services.history_loader import load_history_df

        filled = 0
        for code in missing:
            try:
                frame, source = load_history_df(code, days=_MISSING_BAR_FETCH_DAYS, target_date=end)
                if frame is None or frame.empty:
                    warnings.append(f"纸面交易：{code} 缺少 {end} 前后日线，取数失败（{source}）")
                    continue
                db.save_daily_data(frame, code, source or "Unknown")
            except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
                warnings.append(f"纸面交易：{code} 日线补齐失败：{exc}")
                continue
            absent = [day.isoformat() for day in wanted if self._bar(code, day) is None]
            if absent:
                warnings.append(f"纸面交易：{code} 补齐日线后仍缺 {', '.join(absent)}")
            else:
                filled += 1
        logger.info("Paper trading prefetched daily bars: %d/%d codes", filled, len(missing))

    # ------------------------------------------------------------------
    # Exit rules, carry-over and corporate actions
    # ------------------------------------------------------------------
    def _paper_trades(self, account_id: Optional[int], *, as_of: date, warnings: List[str]) -> List[Any]:
        if account_id is None:
            return []
        lister = getattr(getattr(self.portfolio, "repo", None), "list_trades", None)
        if not callable(lister):
            return []
        try:
            return list(lister(int(account_id), as_of=as_of))
        except Exception as exc:  # noqa: BLE001 - exits and carry-over are best-effort
            warnings.append(f"读取纸面成交流水失败，跳过止损/止盈与顺延检查：{exc}")
            return []

    def _exit_levels(self, trades: Sequence[Any]) -> Dict[str, Dict[str, Any]]:
        """Latest paper buy per symbol -> the stop/target of the signal behind it.

        Re-reading the signal (instead of copying the levels into the trade)
        keeps one source of truth and survives signal reassessment.
        """
        latest_buy: Dict[str, Any] = {}
        for trade in trades:
            if str(getattr(trade, "side", "") or "").lower() != "buy":
                continue
            if _signal_id_from_note(getattr(trade, "note", None)) is None:
                continue
            if not isinstance(getattr(trade, "trade_date", None), date):
                continue
            code = _plain_code(getattr(trade, "symbol", ""))
            if not code:
                continue
            current = latest_buy.get(code)
            key = (trade.trade_date, getattr(trade, "id", 0) or 0)
            current_key = (
                (current.trade_date, getattr(current, "id", 0) or 0)
                if current is not None else None
            )
            if current_key is None or key > current_key:
                latest_buy[code] = trade

        levels: Dict[str, Dict[str, Any]] = {}
        getter = getattr(self.signals, "get_signal", None)
        if not callable(getter) or not latest_buy:
            return levels
        for code, trade in latest_buy.items():
            signal_id = _signal_id_from_note(getattr(trade, "note", None))
            try:
                signal = getter(int(signal_id or 0)) or {}
            except Exception:  # noqa: BLE001 - a deleted signal disables the rule
                continue
            stop = _positive_float(signal.get("stop_loss"))
            target = _positive_float(signal.get("target_price"))
            if stop is None and target is None:
                continue
            levels[code] = {"stop": stop, "target": target, "signal": signal}
        return levels

    @staticmethod
    def _sessions_between(market: str, start: date, end: date) -> List[date]:
        """Trading sessions ``start <= session < end``."""
        if start >= end:
            return []
        # get_trading_dates returns a pandas DatetimeIndex, which has no truth value.
        raw = get_trading_dates(market, start, end - timedelta(days=1))
        if raw is None:
            return []
        sessions = []
        for item in raw:
            day = item.date() if hasattr(item, "date") else item
            if isinstance(day, date) and start <= day < end:
                sessions.append(day)
        return sorted(set(sessions))

    def _session_blocked(self, market: str, code: str, side: str, session: date) -> bool:
        """Whether the market made the open unusable for this order on that session."""
        bar = self._bar(code, session)
        if bar is None or not bar.open:
            return True
        previous = _previous_trading_date(market, session)
        if previous is None:
            return False
        previous_bar = self._bar(code, previous)
        if previous_bar is None or not previous_bar.close:
            return False
        previous_close = Decimal(str(previous_bar.close))
        open_price = Decimal(str(bar.open))
        limit_pct = price_limit_pct(code)
        if side == "buy":
            return is_limit_up(previous_close, open_price, limit_pct)
        return is_limit_down(previous_close, open_price, limit_pct)

    def _merge_carried_signals(
        self,
        *,
        batch: Dict[str, Dict[str, Any]],
        by_fill: Dict[date, Dict[str, Dict[str, Any]]],
        fill_date: date,
        market: str,
        existing_uids: set,
        retry_days: int,
        warnings: List[str],
    ) -> Dict[str, date]:
        """Carry orders blocked by limit/suspension from earlier sessions into today.

        Only sessions where the open was genuinely unusable count; if any skipped
        session was tradable the run missed an execution (downtime) and the order
        is dropped instead of being filled at a stale price.
        """
        carried: Dict[str, date] = {}
        if retry_days <= 0:
            return carried
        for intended in sorted(day for day in by_fill if day < fill_date):
            for code, signal in sorted(by_fill[intended].items()):
                if code in batch:
                    continue
                side = _side_for_action(signal.get("action"))
                if side is None:
                    continue
                if self._trade_uid(intended, code, side) in existing_uids:
                    continue
                sessions = self._sessions_between(market, intended, fill_date)
                if not sessions:
                    warnings.append(
                        f"{code} 信号 #{signal.get('id')} 原成交日 {intended} 已过，交易日历无法核对，放弃顺延"
                    )
                    continue
                if len(sessions) > retry_days:
                    warnings.append(
                        f"{code} 信号 #{signal.get('id')} 已顺延 {len(sessions)} 个交易日，超过上限 {retry_days}，放弃"
                    )
                    continue
                blocked = [day for day in sessions if self._session_blocked(market, code, side, day)]
                if len(blocked) != len(sessions):
                    warnings.append(
                        f"{code} 信号 #{signal.get('id')} 原成交日 {intended} 之后有可成交却未成交的交易日（漏跑），不顺延"
                    )
                    continue
                batch[code] = signal
                carried[code] = intended
        return carried

    def _dividend_events(self, code: str, *, warnings: List[str]) -> List[Dict[str, Any]]:
        try:
            fetcher = self._dividend_fetcher
            if fetcher is None:
                from data_provider.fundamental_adapter import AkshareFundamentalAdapter

                fetcher = AkshareFundamentalAdapter().get_dividend_events
            return list(fetcher(code) or [])
        except Exception as exc:  # noqa: BLE001 - provider failures must not abort trading
            warnings.append(f"纸面交易：{code} 分红送转数据获取失败：{exc}")
            return []

    def _sync_corporate_actions(
        self,
        *,
        account_id: int,
        fill_date: date,
        warnings: List[str],
    ) -> List[str]:
        """Register dividends/splits that fall inside the account's holding windows.

        Daily bars are 前复权, so a dividend shows up as a jump between a stored
        trade price and the current adjusted close unless the cash/share event
        is recorded. Existing rows are matched on (symbol, date, type) so manual
        entries are never duplicated.
        """
        repo = getattr(self.portfolio, "repo", None)
        lister = getattr(repo, "list_trades", None)
        if not callable(lister):
            return []
        try:
            trades = list(lister(int(account_id), as_of=fill_date))
        except Exception as exc:  # noqa: BLE001 - best-effort
            warnings.append(f"纸面交易：读取成交流水失败，跳过分红入账：{exc}")
            return []

        first_buy: Dict[str, date] = {}
        for trade in trades:
            if str(getattr(trade, "side", "") or "").lower() != "buy":
                continue
            code = _plain_code(getattr(trade, "symbol", ""))
            day = getattr(trade, "trade_date", None)
            if not code or not isinstance(day, date):
                continue
            current = first_buy.get(code)
            if current is None or day < current:
                first_buy[code] = day
        if not first_buy:
            return []

        existing: set = set()
        list_actions = getattr(repo, "list_corporate_actions", None)
        if callable(list_actions):
            try:
                for action in list_actions(int(account_id), as_of=fill_date) or []:
                    existing.add(
                        (
                            _plain_code(getattr(action, "symbol", "")),
                            getattr(action, "effective_date", None),
                            str(getattr(action, "action_type", "") or "").lower(),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - dedupe is best-effort
                warnings.append(f"纸面交易：读取已有分红记录失败，按新增处理：{exc}")

        recorded: List[str] = []
        for code in sorted(first_buy):
            start = first_buy[code]
            for event in self._dividend_events(code, warnings=warnings):
                ex_date = _parse_iso_date(event.get("ex_dividend_date"))
                if ex_date is None or ex_date < start or ex_date > fill_date:
                    continue
                cash = float(event.get("cash_dividend_per_share") or 0.0)
                ratio = float(event.get("split_ratio") or 1.0)
                try:
                    if cash > 0 and (code, ex_date, "cash_dividend") not in existing:
                        self.portfolio.record_corporate_action(
                            account_id=int(account_id),
                            symbol=code,
                            effective_date=ex_date,
                            action_type="cash_dividend",
                            cash_dividend_per_share=cash,
                            note="paper trading auto dividend",
                        )
                        existing.add((code, ex_date, "cash_dividend"))
                        recorded.append(f"{code} {ex_date} 每股派 {cash:g} 元")
                    if abs(ratio - 1.0) > 1e-9 and (code, ex_date, "split_adjustment") not in existing:
                        self.portfolio.record_corporate_action(
                            account_id=int(account_id),
                            symbol=code,
                            effective_date=ex_date,
                            action_type="split_adjustment",
                            split_ratio=ratio,
                            note="paper trading auto split",
                        )
                        existing.add((code, ex_date, "split_adjustment"))
                        recorded.append(f"{code} {ex_date} 送转 {ratio:g} 倍")
                except Exception as exc:  # noqa: BLE001 - one bad event must not stop the run
                    warnings.append(f"纸面交易：{code} {ex_date} 分红送转登记失败：{exc}")
        return recorded

    # ------------------------------------------------------------------
    # Account / state
    # ------------------------------------------------------------------
    def _resolve_account(
        self,
        *,
        account_id: Optional[int],
        account_name: str,
        dry_run: bool,
    ) -> Tuple[Dict[str, Any], bool]:
        accounts = self.portfolio.list_accounts()
        if account_id is not None:
            for account in accounts:
                if int(account["id"]) == int(account_id):
                    if not account.get("is_active", True):
                        raise ValueError(f"account_id={account_id} is inactive")
                    if account.get("market") != "cn":
                        raise ValueError(f"account_id={account_id} is not an A-share account")
                    return account, False
            raise ValueError(f"account_id={account_id} not found")

        for account in accounts:
            if account.get("name") == account_name:
                if not account.get("is_active", True):
                    raise ValueError(f"paper account {account_name!r} is inactive")
                if account.get("market") != "cn":
                    raise ValueError(f"paper account {account_name!r} is not an A-share account")
                return account, False

        if dry_run:
            return {"id": None, "name": account_name, "market": "cn"}, True
        return {"id": None, "name": account_name, "market": "cn"}, True

    def _create_account(self, *, account_name: str, initial_cash: float, reference_date: date) -> Dict[str, Any]:
        account = self.portfolio.create_account(
            name=account_name,
            broker="paper",
            market="cn",
            base_currency="CNY",
        )
        self.portfolio.record_cash_ledger(
            account_id=int(account["id"]),
            event_date=reference_date,
            direction="in",
            amount=float(to_cent(initial_cash)),
            currency="CNY",
            note="纸面交易初始资金",
        )
        logger.info("Created paper trading account id=%s name=%s cash=%s", account.get("id"), account_name, initial_cash)
        return account

    def _load_state(
        self,
        *,
        account: Dict[str, Any],
        reference_date: date,
        market: str,
        initial_cash: float,
        warnings: List[str],
    ) -> Tuple[Decimal, Dict[str, Dict[str, Any]], Decimal]:
        if account.get("id") is None:
            cash = to_cent(initial_cash)
            return cash, {}, cash

        try:
            snapshot = self.portfolio.get_portfolio_snapshot(
                account_id=int(account["id"]),
                as_of=reference_date,
                include_realtime=False,
                read_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - report, then fall back to cash-only sizing
            warnings.append(f"读取参考日 {reference_date} 组合快照失败：{exc}")
            cash = to_cent(initial_cash)
            return cash, {}, cash

        account_snapshot = snapshot["accounts"][0]
        if account_snapshot.get("data_quality") == "partial":
            warnings.append(f"参考日 {reference_date} 组合快照数据质量不完整，仓位计算可能偏低")
        positions: Dict[str, Dict[str, Any]] = {}
        for row in account_snapshot.get("positions") or []:
            code = _plain_code(row.get("symbol"))
            if row.get("market") != market or float(row.get("quantity") or 0.0) <= 0:
                continue
            if row.get("price_available") is False:
                warnings.append(f"持仓 {code} 在参考日 {reference_date} 缺估值价，权益可能偏低")
            positions[code] = {
                "quantity": int(round(float(row.get("quantity") or 0.0))),
                "market_value": float(row.get("market_value_base") or 0.0),
                "last_price": float(row.get("last_price") or 0.0),
            }
        return (
            to_cent(account_snapshot.get("total_cash") or 0.0),
            positions,
            to_cent(account_snapshot.get("total_equity") or 0.0),
        )

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def _build_plan(
        self,
        *,
        batch: Dict[str, Dict[str, Any]],
        fill_date: date,
        reference_date: date,
        cash: Decimal,
        positions: Dict[str, Dict[str, Any]],
        per_name: Decimal,
        max_names: int,
        min_score: int,
        warnings: List[str],
        trades: Optional[Sequence[Any]] = None,
        carry_days: Optional[Dict[str, date]] = None,
        allowed_boards: Iterable[str] = DEFAULT_ALLOWED_BOARDS,
        slippage_bps: float = 0.0,
        max_adv_pct: float = 0.0,
        adv_window_days: int = DEFAULT_ADV_WINDOW_DAYS,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Decimal]:
        tradable = frozenset(allowed_boards)
        items: List[Dict[str, Any]] = []
        orders: List[Dict[str, Any]] = []
        fill_bars: Dict[str, Optional[StockDaily]] = {}
        ref_closes: Dict[str, Optional[float]] = {}
        adv_limits: Dict[str, Optional[int]] = {}
        carry_days = carry_days or {}
        exit_levels = self._exit_levels(trades) if trades else {}
        exited_today: set = set()

        def adv_cap(code: str) -> Optional[int]:
            return self._adv_cap_qty(
                code=code,
                fill_date=fill_date,
                window_days=adv_window_days,
                pct=max_adv_pct,
                cache=adv_limits,
            )

        def liquidity_reason(cap: int) -> str:
            return (
                f"流动性上限：单笔不超过 {adv_window_days} 日均量的 {max_adv_pct:g}%"
                f"（约 {cap} 股）"
            )

        def fill_bar(code: str) -> Optional[StockDaily]:
            if code not in fill_bars:
                fill_bars[code] = self._bar(code, fill_date)
            return fill_bars[code]

        def ref_close(code: str) -> Optional[float]:
            if code not in ref_closes:
                row = self._bar(code, reference_date)
                ref_closes[code] = float(row.close) if row is not None and row.close else None
            return ref_closes[code]

        def item(
            signal: Dict[str, Any],
            *,
            side: str = "",
            quantity: int = 0,
            price: Optional[float] = None,
            reference_price: Optional[float] = None,
            fee: Decimal = Decimal("0.00"),
            tax: Decimal = Decimal("0.00"),
            status: str = "skipped",
            reason: str = "",
            action: Optional[str] = None,
        ) -> Dict[str, Any]:
            return {
                "signal_id": signal.get("id"),
                "stock_code": _plain_code(signal.get("stock_code")),
                "stock_name": signal.get("stock_name"),
                "action": action or str(signal.get("action") or "").lower(),
                "score": signal.get("score"),
                "side": side,
                "quantity": int(quantity),
                "price": None if price is None else round(float(price), 4),
                "reference_price": None if reference_price is None else round(float(reference_price), 4),
                "fee": float(fee),
                "tax": float(tax),
                "status": status,
                "reason": reason or None,
            }

        def order_of(
            plan_item: Dict[str, Any],
            *,
            uid_date: Optional[date] = None,
            uid_tag: str = "",
        ) -> Dict[str, Any]:
            order = {
                "stock_code": plan_item["stock_code"],
                "side": plan_item["side"],
                "quantity": plan_item["quantity"],
                "price": plan_item["price"],
                "fee": plan_item["fee"],
                "tax": plan_item["tax"],
                "signal_id": plan_item["signal_id"],
                "note": self._order_note(plan_item),
            }
            if uid_date is not None:
                order["uid_date"] = uid_date
            if uid_tag:
                order["uid_tag"] = uid_tag
            return order

        buy_candidates: List[Tuple[str, Dict[str, Any]]] = []

        # Sells first: they free up cash and are sized against reference-date
        # holdings only, which is exactly the T+1 constraint.
        for code in sorted(batch):
            signal = batch[code]
            action = str(signal.get("action") or "").lower()
            if action in NOOP_ACTIONS:
                items.append(item(signal, reason=f"{action} → 不动"))
                continue
            if action in BUY_ACTIONS:
                buy_candidates.append((code, signal))
                continue
            if action not in CLOSE_ACTIONS and action not in REDUCE_ACTIONS:
                items.append(item(signal, reason=f"未知动作 {action!r}，不动"))
                continue

            position = positions.get(code)
            if not position or int(position["quantity"]) <= 0:
                items.append(item(signal, reason="未持仓，无需卖出" if action in CLOSE_ACTIONS else "未持仓，无需减仓"))
                continue
            available = int(position["quantity"])
            quantity = available if action in CLOSE_ACTIONS else (available // 2 // 100) * 100
            if quantity <= 0:
                items.append(item(signal, reason=f"可卖 {available} 股不足一手，无法减半"))
                continue
            previous_close = ref_close(code)
            if previous_close is None or previous_close <= 0:
                items.append(item(signal, reason=f"取不到参考日 {reference_date} 收盘价"))
                continue
            bar = fill_bar(code)
            open_price = float(bar.open) if bar is not None and bar.open else None
            if open_price is None or open_price <= 0:
                items.append(item(signal, reference_price=previous_close, reason="成交日无行情（停牌/数据未更新）"))
                continue
            if is_limit_down(Decimal(str(previous_close)), Decimal(str(open_price)), price_limit_pct(code)):
                items.append(
                    item(signal, reference_price=previous_close, price=open_price, reason="开盘跌停，无法卖出")
                )
                continue

            requested_qty = quantity
            cap = adv_cap(code)
            if cap is not None:
                quantity = min(quantity, cap)
                if quantity <= 0:
                    items.append(
                        item(
                            signal,
                            reference_price=previous_close,
                            price=open_price,
                            reason=liquidity_reason(0) + "，无法卖出",
                        )
                    )
                    continue
            fill_price = apply_slippage(
                open_price,
                "sell",
                code=code,
                previous_close=previous_close,
                bps=slippage_bps,
            )
            gross = to_cent(Decimal(str(fill_price)) * quantity)
            fee, tax = compute_fees("sell", gross)
            cash += gross - fee - tax
            position["quantity"] = available - quantity
            position["market_value"] = float(to_cent(Decimal(str(previous_close)) * position["quantity"]))

            if action in CLOSE_ACTIONS:
                reason = "清仓" if quantity >= available else "部分清仓（受流动性上限裁剪）"
            else:
                reason = "减半"
                if quantity < requested_qty:
                    reason += "（受流动性上限裁剪）"
            plan_item = item(
                signal,
                side="sell",
                quantity=quantity,
                price=fill_price,
                reference_price=previous_close,
                fee=fee,
                tax=tax,
                status="planned",
                reason=reason,
            )
            items.append(plan_item)
            orders.append(order_of(plan_item, uid_date=carry_days.get(code)))
            exited_today.add(code)

        # Stop-loss / take-profit resting orders from the signal that opened each
        # position. Intraday bars are used: a level is touched when the low/high
        # reaches it, and a gap through it fills at the open (worse for both).
        for code in sorted(exit_levels):
            position = positions.get(code)
            available = int(position["quantity"]) if position else 0
            if available <= 0:
                continue
            level = exit_levels[code]
            stop = level["stop"]
            target = level["target"]
            bar = fill_bar(code)
            if bar is None or not bar.open:
                items.append(
                    item(
                        level["signal"],
                        action="hold",
                        reference_price=ref_close(code),
                        reason="触发止盈止损检查，但成交日停牌，继续持有",
                    )
                )
                continue
            open_price = float(bar.open)
            low = float(bar.low) if bar.low else open_price
            high = float(bar.high) if bar.high else open_price
            if stop is not None and low <= stop:
                trigger, exit_price, label = "stop_loss", min(open_price, stop), "止损"
            elif target is not None and high >= target:
                trigger, exit_price, label = "take_profit", max(open_price, target), "止盈"
            else:
                continue
            previous_close = ref_close(code)
            if (
                previous_close
                and is_limit_down(Decimal(str(previous_close)), Decimal(str(open_price)), price_limit_pct(code))
            ):
                items.append(
                    item(
                        level["signal"],
                        action=trigger,
                        reference_price=previous_close,
                        price=open_price,
                        reason=f"触发{label}但开盘跌停，卖不出，继续持有",
                    )
                )
                continue

            quantity = available
            cap = adv_cap(code)
            if cap is not None:
                quantity = min(quantity, cap)
                if quantity <= 0:
                    items.append(
                        item(
                            level["signal"],
                            action=trigger,
                            reference_price=previous_close,
                            price=open_price,
                            reason=f"触发{label}但{liquidity_reason(0)}，无法卖出，继续持有",
                        )
                    )
                    continue
            fill_price = apply_slippage(
                exit_price,
                "sell",
                code=code,
                previous_close=previous_close,
                bps=slippage_bps,
            )
            gross = to_cent(Decimal(str(fill_price)) * quantity)
            fee, tax = compute_fees("sell", gross)
            cash += gross - fee - tax
            remaining = available - quantity
            positions[code]["quantity"] = remaining
            positions[code]["market_value"] = (
                float(to_cent(Decimal(str(previous_close)) * remaining))
                if previous_close and remaining > 0
                else 0.0
            )
            partial_note = "（受流动性上限部分成交，余量次日继续）" if remaining > 0 else ""
            plan_item = item(
                level["signal"],
                action=trigger,
                side="sell",
                quantity=quantity,
                price=fill_price,
                reference_price=previous_close,
                fee=fee,
                tax=tax,
                status="planned",
                reason=f"{label}（触发价 {exit_price:.2f}，持仓成本对应信号）{partial_note}",
            )
            items.append(plan_item)
            orders.append(order_of(plan_item, uid_tag="exit"))
            exited_today.add(code)

        buy_candidates.sort(key=lambda pair: (-(int(pair[1].get("score") or 0)), pair[0]))
        open_count = sum(1 for position in positions.values() if int(position["quantity"]) > 0)

        for code, signal in buy_candidates:
            board = _board_of(code)
            if board not in tradable:
                items.append(
                    item(
                        signal,
                        reason=(
                            f"{_BOARD_LABELS.get(board, board)}（{code}）不在可交易板块"
                            f"（{_boards_label(tradable)}），跳过"
                        ),
                    )
                )
                continue
            if code in exited_today:
                items.append(item(signal, reason="今日已卖出（信号/止盈止损），不重复开仓"))
                continue
            score = signal.get("score")
            if score is not None and int(score) < min_score:
                items.append(
                    item(signal, reason=f"score {score} < {min_score} → 不买", status="skipped")
                )
                continue
            position = positions.get(code)
            held_qty = int(position["quantity"]) if position else 0
            if held_qty <= 0 and open_count >= max_names:
                items.append(item(signal, reason=f"已持/已排 {open_count} 只，达上限 {max_names}"))
                continue

            reference_price = ref_close(code)
            if reference_price is None and position and position.get("last_price"):
                reference_price = float(position["last_price"])
            if reference_price is None or reference_price <= 0:
                items.append(item(signal, reason=f"取不到参考日 {reference_date} 收盘价"))
                continue

            target_qty = clamp_buy_qty(code, int(per_name / Decimal(str(reference_price))))
            if target_qty <= 0:
                items.append(
                    item(signal, reference_price=reference_price, reason=f"单票目标 {per_name} 元不足一手")
                )
                continue
            if position and Decimal(str(position["market_value"])) >= per_name * Decimal("0.8"):
                items.append(item(signal, reference_price=reference_price, reason="已足额，不加仓"))
                continue
            quantity = target_qty - held_qty
            if quantity <= 0:
                items.append(item(signal, reference_price=reference_price, reason="无需追加"))
                continue

            bar = fill_bar(code)
            open_price = float(bar.open) if bar is not None and bar.open else None
            if open_price is None or open_price <= 0:
                items.append(item(signal, reference_price=reference_price, reason="成交日无行情（停牌/数据未更新）"))
                continue
            if is_limit_up(Decimal(str(reference_price)), Decimal(str(open_price)), price_limit_pct(code)):
                items.append(
                    item(signal, reference_price=reference_price, price=open_price, reason="开盘涨停，买不进")
                )
                continue

            fill_price = apply_slippage(
                open_price,
                "buy",
                code=code,
                previous_close=reference_price,
                bps=slippage_bps,
            )
            requested_qty = quantity
            quantity = min(quantity, affordable_qty(code, cash, Decimal(str(fill_price))))
            if quantity <= 0:
                items.append(item(signal, reference_price=reference_price, price=fill_price, reason="现金不足一手"))
                continue
            cap = adv_cap(code)
            if cap is not None:
                quantity = min(quantity, cap)
                if quantity <= 0:
                    items.append(
                        item(
                            signal,
                            reference_price=reference_price,
                            price=fill_price,
                            reason=liquidity_reason(0),
                        )
                    )
                    continue
            gross = to_cent(Decimal(str(fill_price)) * quantity)
            fee, tax = compute_fees("buy", gross)
            if gross + fee + tax > cash:
                quantity = clamp_buy_qty(code, quantity - 100)
                if quantity <= 0:
                    items.append(item(signal, reference_price=reference_price, price=fill_price, reason="现金不足一手"))
                    continue
                gross = to_cent(Decimal(str(fill_price)) * quantity)
                fee, tax = compute_fees("buy", gross)
            cash -= gross + fee + tax

            if held_qty <= 0:
                open_count += 1
            positions[code] = {
                "quantity": held_qty + quantity,
                "market_value": float(gross),
                "last_price": fill_price,
            }

            buy_reason = "建仓" if held_qty <= 0 else "加仓"
            if quantity < requested_qty:
                buy_reason += "（受流动性上限裁剪）"
            plan_item = item(
                signal,
                side="buy",
                quantity=quantity,
                price=fill_price,
                reference_price=reference_price,
                fee=fee,
                tax=tax,
                status="planned",
                reason=buy_reason,
            )
            items.append(plan_item)
            orders.append(order_of(plan_item, uid_date=carry_days.get(code)))

        items.sort(key=lambda row: 0 if row["side"] == "sell" else 1)
        orders.sort(key=lambda row: 0 if row["side"] == "sell" else 1)
        return items, orders, cash

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _same_day_fills(self, account_id: int, fill_date: date) -> List[Any]:
        trades = self.portfolio.repo.list_trades(account_id, as_of=fill_date)
        return [trade for trade in trades if trade.trade_date == fill_date]

    def _execute_orders(
        self,
        *,
        account_id: int,
        fill_date: date,
        orders: List[Dict[str, Any]],
        warnings: List[str],
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for order in orders:
            trade_uid = self._trade_uid(
                order.get("uid_date") or fill_date,
                order["stock_code"],
                order["side"],
                tag=str(order.get("uid_tag") or ""),
            )
            payload = {
                **_public_order(order),
                "trade_uid": trade_uid,
                "status": "failed",
                "trade_id": None,
                "reason": None,
            }
            try:
                created = self.portfolio.record_trade(
                    account_id=account_id,
                    symbol=order["stock_code"],
                    trade_date=fill_date,
                    side=order["side"],
                    quantity=float(order["quantity"]),
                    price=float(order["price"]),
                    fee=float(order["fee"]),
                    tax=float(order["tax"]),
                    market="cn",
                    currency="CNY",
                    trade_uid=trade_uid,
                    note=order["note"],
                )
                payload["status"] = "executed"
                payload["trade_id"] = created.get("id")
            except PortfolioConflictError:
                payload["status"] = "duplicate"
                payload["reason"] = "同账户同交易日同标的同方向已有成交（幂等跳过）"
            except (PortfolioOversellError, ValueError) as exc:
                payload["reason"] = str(exc)
                warnings.append(f"{order['stock_code']} {order['side']} 未成交：{exc}")
            except Exception as exc:  # noqa: BLE001 - one bad order must not abort the batch
                payload["reason"] = str(exc)
                warnings.append(f"{order['stock_code']} {order['side']} 写入失败：{exc}")
                logger.exception("Paper trading order failed: %s", order)
            results.append(payload)
        return results

    def _refresh_snapshot(self, *, account_id: int, fill_date: date, warnings: List[str]) -> None:
        try:
            self.portfolio.get_portfolio_snapshot(
                account_id=account_id,
                as_of=fill_date,
                include_realtime=False,
                read_only=False,
            )
        except Exception as exc:  # noqa: BLE001 - the trades are already durable
            warnings.append(f"成交后刷新组合快照失败：{exc}")
            logger.exception("Paper trading snapshot refresh failed")

    # ------------------------------------------------------------------
    # Catch-up
    # ------------------------------------------------------------------
    def _run_catchup_batches(
        self,
        *,
        by_fill: Dict[date, Dict[str, Dict[str, Any]]],
        latest_data_date: date,
        account_id: Optional[int],
        account_name: str,
        market: str,
        horizons: Sequence[str],
        min_score: int,
        max_names: int,
        max_weight: float,
        dry_run: bool,
        force: bool,
        initial_cash: float,
    ) -> Optional[Dict[str, Any]]:
        """Replay missed sessions in ascending order, each at its own open.

        Every session goes through the normal pipeline (same fees, limits,
        suspensions, corporate actions and trade_uid idempotency), so a
        re-run never duplicates a fill and a blocked order can retry on the
        next session within the window.
        """

        due_days = sorted(day for day in by_fill if day < latest_data_date)
        if not due_days:
            return None
        max_days = max(1, int(_env_int(PAPER_TRADING_CATCHUP_DAYS_ENV, DEFAULT_CATCHUP_DAYS)))
        selected = due_days[:max_days]
        results: List[Dict[str, Any]] = []
        for day in selected:
            sub = self.run(
                account_id=account_id,
                account_name=account_name,
                market=market,
                horizons=horizons,
                min_score=min_score,
                max_names=max_names,
                max_weight=max_weight,
                dry_run=dry_run,
                force=force,
                initial_cash=initial_cash,
                batch_fill_date=day,
            )
            results.append({
                "fill_date": day.isoformat(),
                "status": sub.get("status"),
                "message": sub.get("message"),
                "orders": sub.get("orders") or [],
                "items": sub.get("items") or [],
                "warnings": sub.get("warnings") or [],
                "corporate_actions": sub.get("corporate_actions") or [],
            })
            logger.info(
                "Paper trading catch-up %s: status=%s orders=%d",
                day.isoformat(),
                sub.get("status"),
                len(sub.get("orders") or []),
            )
        return {
            "days": [day.isoformat() for day in selected],
            "pending_days": [day.isoformat() for day in due_days[len(selected):]],
            "results": results,
        }

    @staticmethod
    def _merge_catchup(
        result: Dict[str, Any],
        catchup: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Attach catch-up detail and prefix a one-line summary."""
        if not catchup:
            return result
        counts = {"executed": 0, "planned": 0, "duplicate": 0, "failed": 0}
        for row in catchup.get("results") or []:
            for order in row.get("orders") or []:
                status = str(order.get("status") or "")
                if status in counts:
                    counts[status] += 1
        days = "、".join(catchup.get("days") or [])
        summary = (
            f"补跑 {len(catchup.get('days') or [])} 个交易日（{days}）："
            f"成交 {counts['executed']}、干跑 {counts['planned']}、"
            f"重复跳过 {counts['duplicate']}、失败 {counts['failed']}。"
        )
        pending = catchup.get("pending_days") or []
        if pending:
            summary += f" 另有 {len(pending)} 个交易日待下次补跑。"
        result["catchup"] = catchup
        counts_payload = result.get("signal_counts")
        if isinstance(counts_payload, dict):
            counts_payload["catchup_days"] = len(catchup.get("days") or [])
        result["message"] = summary + " " + str(result.get("message") or "")
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _trade_uid(fill_date: date, code: str, side: str, tag: str = "") -> str:
        uid = f"paper-{fill_date.isoformat()}-{_plain_code(code)}-{side}"
        return f"{uid}-{tag}" if tag else uid

    @staticmethod
    def _order_note(order: Dict[str, Any]) -> str:
        signal_id = order.get("signal_id")
        return f"paper trading: signal #{signal_id}"[:255]

    @staticmethod
    def _result(status: str, message: str, **payload: Any) -> Dict[str, Any]:
        base = {
            "status": status,
            "dry_run": payload.pop("dry_run", True),
            "message": message,
            "market": payload.pop("market", "cn"),
            "latest_data_date": _iso_or_none(payload.pop("latest_data_date", None)),
            "signal_counts": payload.pop("signal_counts", {}),
            "account": payload.pop("account", None),
            "batch": payload.pop("batch", None),
            "portfolio": payload.pop("portfolio", None),
            "items": payload.pop("items", []),
            "orders": payload.pop("orders", []),
            "warnings": payload.pop("warnings", []),
            "corporate_actions": payload.pop("corporate_actions", []),
        }
        base.update(payload)
        return base


def _default_horizons() -> Tuple[str, ...]:
    raw = (os.getenv(PAPER_TRADING_HORIZONS_ENV) or "").strip()
    if not raw:
        return DEFAULT_HORIZONS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def maybe_run_after_analysis(*, trigger: str = "analysis_hook") -> Optional[Dict[str, Any]]:
    """Auto-run the paper loop when enabled.

    Called both after a scheduled analysis (any outcome) and from the
    independent paper-trading schedule. The two paths share the same idempotent
    execution layer, so a double trigger cannot duplicate a fill.

    Disabled by default. Set ``PAPER_TRADING_AUTO_RUN=true`` to enable and
    ``PAPER_TRADING_AUTO_APPLY=false`` to keep the automatic run as a dry run.
    """

    if not _env_bool(PAPER_TRADING_AUTO_RUN_ENV, False):
        logger.info(
            "Paper trading skipped (trigger=%s): %s is false",
            trigger,
            PAPER_TRADING_AUTO_RUN_ENV,
        )
        return None
    dry_run = not _env_bool(PAPER_TRADING_AUTO_APPLY_ENV, True)
    try:
        result = PaperTradingService().run(dry_run=dry_run)
        logger.info(
            "Paper trading auto-run (trigger=%s, dry_run=%s): status=%s %s",
            trigger,
            dry_run,
            result.get("status"),
            result.get("message"),
        )
        return result
    except Exception as exc:  # noqa: BLE001 - analysis must never fail because of paper trading
        logger.exception("Paper trading auto-run failed (trigger=%s): %s", trigger, exc)
        return None
