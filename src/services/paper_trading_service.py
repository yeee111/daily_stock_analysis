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
ordering. A policy profile may instead pin ``exit.stop_loss_pct`` /
``exit.take_profit_pct``; those are measured from the latest buy price and
override the signal's absolute levels (per side, with the signal as fallback).

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

Multi-account (Phase 1 skeleton)
--------------------------------
Signals are a shared, global resource: ``build_signal_batches`` fetches the
active signals once per run (over the union of the participating accounts'
horizons) and freezes them into immutable :class:`SignalBatch` snapshots, one
per fill day. ``run_for_account`` then replays that snapshot for one account
under its own :class:`PaperPolicy`, and ``run_all_accounts`` walks the
``auto_run`` branches sequentially (one account failure never blocks the
others, and SQLite stays single-writer). ``run()`` is kept as the
single-account compatibility entry: it resolves one policy, builds one batch
and delegates.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select

from src.core.trading_calendar import get_next_trading_date, get_trading_dates
from src.repositories.portfolio_repo import (
    DEFAULT_PAPER_ACCOUNT_NAMES,
    ensure_paper_branch_defaults,
)
from src.repositories.stock_repo import StockRepository
from src.services import paper_risk_control as rc
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
# Written before executing orders and replaced by the terminal status once the
# run returns: if the process dies mid-execution the row stays ``running`` and
# proves the attempt started (trades may already be committed).
STATUS_RUNNING = "running"
# Per-account failure marker used only by ``run_all_accounts`` summaries: an
# account that raised must still show up in the batch result, and the value is
# deliberately not an order status so it cannot be confused with "failed".
STATUS_ACCOUNT_FAILED = "account_failed"

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


def _positive_int(value: Any) -> Optional[int]:
    """Integer configuration knob; ``None`` for anything that is not a positive int."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _effective_exit_levels(
    buy_price: Optional[float],
    signal: Mapping[str, Any],
    exit_config: Optional[Mapping[str, Any]],
) -> Tuple[Optional[float], Optional[float], str, str]:
    """Resting stop/target for one holding, plus where each side came from.

    A profile percentage is measured from the latest buy price and overrides the
    signal's absolute level; the side falls back to the signal when the profile
    leaves it empty (or the buy price is unusable). Sources are ``profile``,
    ``signal`` or ``""`` when that side has no level at all.
    """
    config = exit_config if isinstance(exit_config, Mapping) else {}
    stop_pct = _positive_float(config.get("stop_loss_pct"))
    target_pct = _positive_float(config.get("take_profit_pct"))

    def side(pct: Optional[float], fallback: Any, sign: int) -> Tuple[Optional[float], str]:
        if pct is not None and buy_price is not None and buy_price > 0 and 0 < pct < 1:
            return round(buy_price * (1.0 + sign * pct), 2), "profile"
        value = _positive_float(fallback)
        return (value, "signal") if value is not None else (None, "")

    stop, stop_source = side(stop_pct, signal.get("stop_loss"), -1)
    target, target_source = side(target_pct, signal.get("target_price"), 1)
    return stop, target, stop_source, target_source


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


# ----------------------------------------------------------------------
# Multi-account Phase 1 skeleton: policy profile + immutable signal batch
# ----------------------------------------------------------------------
DEFAULT_POLICY_LABEL = "env/default"
POLICY_CONFIG_SECTIONS: Tuple[str, ...] = ("entry", "exit", "risk")

# Which policy keys are honoured today. ``exit`` only carries the two
# percentage-based resting levels; trailing stop, partial take-profit and
# re-entry cooldown stay out of Phase 1.
_POLICY_ENTRY_KEYS = ("horizons", "min_score", "max_names", "max_weight", "initial_cash",
                      "min_rr", "rr_max_stop_width", "min_order_amount", "min_target_space")
_POLICY_EXIT_KEYS = ("stop_loss_pct", "take_profit_pct", "fallback_stop_loss_pct",
                     "max_holding_days")
_POLICY_RISK_KEYS = ("retry_days", "catchup_days", "slippage_bps", "max_adv_pct", "adv_window_days",
                     "stop_cooldown_scope", "stop_cooldown_days", "cash_reserve_pct",
                     "fallback_stop_enabled", "rr_filter_enabled", "cooldown_enabled",
                     "budget_enabled", "min_amount_enabled",
                     "bad_target_enabled", "target_space_enabled")


def _empty_policy_config() -> Dict[str, Any]:
    return {section: {} for section in POLICY_CONFIG_SECTIONS}


def _policy_config_value(
    config: Optional[Mapping[str, Any]],
    section: str,
    key: str,
) -> Any:
    if not config:
        return None
    rows = config.get(section)
    if not isinstance(rows, Mapping):
        return None
    return rows.get(key)


def _coerce_int(value: Any, *, fallback: int, name: str, warnings: List[str]) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        warnings.append(f"策略档 {name} 取值 {value!r} 不是整数，已回退默认 {fallback}")
        return fallback


def _coerce_float(value: Any, *, fallback: float, name: str, warnings: List[str]) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        warnings.append(f"策略档 {name} 取值 {value!r} 不是数字，已回退默认 {fallback}")
        return fallback


def _coerce_horizons(value: Any, *, fallback: Tuple[str, ...], warnings: List[str]) -> Tuple[str, ...]:
    if isinstance(value, str):
        parts: List[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        warnings.append(f"策略档 horizons 取值 {value!r} 不是列表，已回退默认 {list(fallback)}")
        return fallback
    items = tuple(str(part).strip() for part in parts if str(part).strip())
    return items or fallback


def _coerce_pct(value: Any, *, name: str, warnings: List[str]) -> Optional[float]:
    """A resting-level ratio must be in (0, 1); anything else is ignored.

    Out-of-range or non-numeric values degrade to ``None`` (fall back to the
    signal's own levels) instead of raising, so one bad profile row can never
    block the daily paper loop.
    """
    try:
        pct = float(value)
    except (TypeError, ValueError):
        warnings.append(f"策略档 {name} 取值 {value!r} 不是数字，已回退信号止损/止盈")
        return None
    if not 0 < pct < 1:
        warnings.append(f"策略档 {name} 取值 {value!r} 需在 (0, 1) 之间，已回退信号止损/止盈")
        return None
    return pct


@dataclass(frozen=True)
class PaperPolicy:
    """One account's trading discipline.

    Phase 1 only reads the knobs the executor already implements; every field
    falls back to ``env -> built-in default`` when the profile leaves it out, so
    the shipped ``default`` profile (all sections empty) behaves exactly like the
    current single-account loop. ``exit`` carries only the two percentage-based
    resting levels; when both are empty the signal's own stop/target is used.
    """

    profile_id: Optional[int] = None
    profile_name: str = DEFAULT_POLICY_LABEL
    profile_version: Optional[int] = None
    config: Mapping[str, Any] = field(default_factory=_empty_policy_config)
    horizons: Tuple[str, ...] = DEFAULT_HORIZONS
    min_score: int = DEFAULT_MIN_SCORE
    max_names: int = DEFAULT_MAX_NAMES
    max_weight: float = DEFAULT_MAX_WEIGHT
    initial_cash: float = DEFAULT_INITIAL_CASH
    retry_days: int = DEFAULT_RETRY_DAYS
    catchup_days: int = DEFAULT_CATCHUP_DAYS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    max_adv_pct: float = DEFAULT_MAX_ADV_PCT
    adv_window_days: int = DEFAULT_ADV_WINDOW_DAYS
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    max_holding_days: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe view used for the ``paper_account_runs`` audit payload."""
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "horizons": sorted(self.horizons),
            "min_score": self.min_score,
            "max_names": self.max_names,
            "max_weight": self.max_weight,
            "initial_cash": self.initial_cash,
            "retry_days": self.retry_days,
            "catchup_days": self.catchup_days,
            "slippage_bps": self.slippage_bps,
            "max_adv_pct": self.max_adv_pct,
            "adv_window_days": self.adv_window_days,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "max_holding_days": self.max_holding_days,
            "config": {section: dict(self.config.get(section) or {}) for section in POLICY_CONFIG_SECTIONS},
        }


def resolve_policy(
    config: Optional[Mapping[str, Any]] = None,
    *,
    profile_id: Optional[int] = None,
    profile_name: Optional[str] = None,
    profile_version: Optional[int] = None,
    horizons: Optional[Iterable[str]] = None,
    min_score: Optional[int] = None,
    max_names: Optional[int] = None,
    max_weight: Optional[float] = None,
    initial_cash: Optional[float] = None,
    warnings: Optional[List[str]] = None,
) -> PaperPolicy:
    """Build one :class:`PaperPolicy`.

    Precedence is ``explicit call argument > profile config > env > default``,
    which keeps ``run()``/API/CLI overrides working for every account and keeps
    an empty profile behaviourally identical to today.
    """
    notes: List[str] = []
    normalized = {section: {} for section in POLICY_CONFIG_SECTIONS}
    if isinstance(config, Mapping):
        for section in POLICY_CONFIG_SECTIONS:
            rows = config.get(section)
            if isinstance(rows, Mapping):
                normalized[section] = dict(rows)
            elif section in config:
                notes.append(f"策略档 {section} 段不是对象，已忽略")
        supported = set(_POLICY_ENTRY_KEYS) | set(_POLICY_EXIT_KEYS) | set(_POLICY_RISK_KEYS)
        unknown = sorted(
            {
                key
                for section in POLICY_CONFIG_SECTIONS
                for key in normalized[section]
                if key not in supported
            }
        )
        if unknown:
            notes.append(f"策略档字段 {unknown} 在 Phase 1 未启用，已忽略")

    def entry(key: str):
        return normalized["entry"].get(key)

    def exit_rule(key: str):
        return normalized["exit"].get(key)

    def risk(key: str):
        return normalized["risk"].get(key)

    raw_horizons = horizons if horizons is not None else entry("horizons")
    horizon_items = (
        _coerce_horizons(raw_horizons, fallback=_default_horizons(), warnings=notes)
        if raw_horizons is not None
        else _default_horizons()
    )

    min_score_value = min_score if min_score is not None else entry("min_score")
    resolved_min_score = (
        _coerce_int(min_score_value, fallback=_env_int(PAPER_TRADING_MIN_SCORE_ENV, DEFAULT_MIN_SCORE), name="min_score", warnings=notes)
        if min_score_value is not None
        else _env_int(PAPER_TRADING_MIN_SCORE_ENV, DEFAULT_MIN_SCORE)
    )

    max_names_value = max_names if max_names is not None else entry("max_names")
    resolved_max_names = (
        _coerce_int(max_names_value, fallback=_env_int(PAPER_TRADING_MAX_NAMES_ENV, DEFAULT_MAX_NAMES), name="max_names", warnings=notes)
        if max_names_value is not None
        else _env_int(PAPER_TRADING_MAX_NAMES_ENV, DEFAULT_MAX_NAMES)
    )

    max_weight_value = max_weight if max_weight is not None else entry("max_weight")
    resolved_max_weight = (
        _coerce_float(max_weight_value, fallback=_env_float(PAPER_TRADING_MAX_WEIGHT_ENV, DEFAULT_MAX_WEIGHT), name="max_weight", warnings=notes)
        if max_weight_value is not None
        else _env_float(PAPER_TRADING_MAX_WEIGHT_ENV, DEFAULT_MAX_WEIGHT)
    )

    cash_value = initial_cash if initial_cash is not None else entry("initial_cash")
    resolved_cash = (
        _coerce_float(cash_value, fallback=_env_float(PAPER_TRADING_INITIAL_CASH_ENV, DEFAULT_INITIAL_CASH), name="initial_cash", warnings=notes)
        if cash_value is not None
        else _env_float(PAPER_TRADING_INITIAL_CASH_ENV, DEFAULT_INITIAL_CASH)
    )

    stop_loss_value = exit_rule("stop_loss_pct")
    stop_loss_pct = (
        _coerce_pct(stop_loss_value, name="stop_loss_pct", warnings=notes)
        if stop_loss_value is not None
        else None
    )
    take_profit_value = exit_rule("take_profit_pct")
    take_profit_pct = (
        _coerce_pct(take_profit_value, name="take_profit_pct", warnings=notes)
        if take_profit_value is not None
        else None
    )
    max_holding_days: Optional[int] = None
    max_holding_value = exit_rule("max_holding_days")
    if max_holding_value is not None:
        parsed_holding = _coerce_int(
            max_holding_value, fallback=0, name="max_holding_days", warnings=notes
        )
        if parsed_holding > 0:
            max_holding_days = parsed_holding
        else:
            notes.append(
                f"策略档 max_holding_days 取值 {max_holding_value!r} 需为正整数，已忽略（不做时间止盈）"
            )

    retry_value = risk("retry_days")
    retry_days = (
        _coerce_int(retry_value, fallback=_env_int(PAPER_TRADING_RETRY_DAYS_ENV, DEFAULT_RETRY_DAYS), name="retry_days", warnings=notes)
        if retry_value is not None
        else _env_int(PAPER_TRADING_RETRY_DAYS_ENV, DEFAULT_RETRY_DAYS)
    )
    catchup_value = risk("catchup_days")
    catchup_days = (
        _coerce_int(catchup_value, fallback=_env_int(PAPER_TRADING_CATCHUP_DAYS_ENV, DEFAULT_CATCHUP_DAYS), name="catchup_days", warnings=notes)
        if catchup_value is not None
        else _env_int(PAPER_TRADING_CATCHUP_DAYS_ENV, DEFAULT_CATCHUP_DAYS)
    )
    slippage_value = risk("slippage_bps")
    slippage = (
        _coerce_float(slippage_value, fallback=slippage_bps(), name="slippage_bps", warnings=notes)
        if slippage_value is not None
        else slippage_bps()
    )
    adv_value = risk("max_adv_pct")
    adv_pct = (
        _coerce_float(adv_value, fallback=max_adv_pct(), name="max_adv_pct", warnings=notes)
        if adv_value is not None
        else max_adv_pct()
    )
    adv_window_value = risk("adv_window_days")
    adv_window_days = (
        _coerce_int(adv_window_value, fallback=_env_int(PAPER_TRADING_ADV_WINDOW_DAYS_ENV, DEFAULT_ADV_WINDOW_DAYS), name="adv_window_days", warnings=notes)
        if adv_window_value is not None
        else _env_int(PAPER_TRADING_ADV_WINDOW_DAYS_ENV, DEFAULT_ADV_WINDOW_DAYS)
    )

    policy = PaperPolicy(
        profile_id=profile_id,
        profile_name=(str(profile_name).strip() if profile_name else DEFAULT_POLICY_LABEL),
        profile_version=profile_version,
        config=normalized,
        horizons=horizon_items,
        min_score=resolved_min_score,
        max_names=resolved_max_names,
        max_weight=resolved_max_weight,
        initial_cash=resolved_cash,
        retry_days=max(0, retry_days),
        catchup_days=catchup_days,
        slippage_bps=slippage,
        max_adv_pct=adv_pct,
        adv_window_days=adv_window_days,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        max_holding_days=max_holding_days,
    )

    if policy.max_names < 1:
        raise ValueError("max_names must be >= 1")
    if not 0 < policy.max_weight <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    if policy.initial_cash <= 0:
        raise ValueError("initial_cash must be > 0")

    if warnings is not None:
        warnings.extend(notes)
    return policy


def _policy_from_branch(row: Mapping[str, Any], warnings: List[str]) -> PaperPolicy:
    """Resolve the policy of one branch row returned by ``list_branch_accounts``."""
    return resolve_policy(
        row.get("config"),
        profile_id=row.get("profile_id"),
        profile_name=row.get("profile_name"),
        profile_version=row.get("profile_version"),
        warnings=warnings,
    )


def _horizon_union(
    *,
    accounts: Optional[Sequence[Any]] = None,
    horizons_union: Optional[Iterable[str]] = None,
) -> Tuple[str, ...]:
    """Union of the participating accounts' horizons (fetched globally once)."""
    if horizons_union is not None:
        items = {str(part).strip() for part in horizons_union if str(part).strip()}
        return tuple(sorted(items)) if items else tuple(sorted(_default_horizons()))
    collected: set = set()
    for account in accounts or []:
        horizons = None
        if isinstance(account, Mapping):
            horizons = (account.get("config") or {}).get("entry", {}).get("horizons") if isinstance(account.get("config"), Mapping) else None
            if horizons is None:
                horizons = account.get("horizons")
        elif isinstance(account, PaperPolicy):
            horizons = account.horizons
        if horizons is None:
            continue
        if isinstance(horizons, str):
            collected.update(part.strip() for part in horizons.split(",") if part.strip())
        else:
            collected.update(str(part).strip() for part in horizons if str(part).strip())
    return tuple(sorted(collected)) if collected else tuple(sorted(_default_horizons()))


def _signal_ids(bucket: Iterable[Mapping[str, Any]]) -> Tuple[int, ...]:
    ids: List[int] = []
    for signal in bucket:
        try:
            ids.append(int(signal.get("id")))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(ids))


def _batch_key(market: str, fill_date: Optional[date], signal_ids: Sequence[int]) -> str:
    """Stable batch identity: market + fill day + digest of the selected ids.

    Rebuilding the same inputs always yields the same key, which is what makes
    ``paper_account_runs`` rows comparable across accounts and re-runs.
    """
    joined = ",".join(str(item) for item in sorted(signal_ids))
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]
    day = fill_date.isoformat() if fill_date else "nodate"
    return f"{market}-{day}-{digest}"


def _latest_by_code(signals: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest signal per code (by ``created_at``) — one batch's working bucket."""
    bucket: Dict[str, Dict[str, Any]] = {}
    for signal in signals:
        code = _plain_code(signal.get("stock_code"))
        current = bucket.get(code)
        if current is None or str(signal.get("created_at") or "") > str(current.get("created_at") or ""):
            bucket[code] = dict(signal)
    return bucket


def _bucket_by_fill(
    *,
    market: str,
    signals: Sequence[Mapping[str, Any]],
    warnings: List[str],
) -> Dict[date, Dict[str, Dict[str, Any]]]:
    """Batch the active signals by fill day: day -> code -> latest signal."""
    by_day: Dict[date, List[Mapping[str, Any]]] = {}
    for signal in signals:
        signal_day = _parse_signal_day(signal.get("created_at"))
        if signal_day is None:
            warnings.append(f"信号 #{signal.get('id')} 缺少可解析的 created_at，已跳过")
            continue
        fill_day = get_next_trading_date(market, signal_day)
        if fill_day is None:
            warnings.append("交易日历不可用，无法确定成交日")
            continue
        by_day.setdefault(fill_day, []).append(signal)
    return {day: _latest_by_code(items) for day, items in by_day.items()}


@dataclass(frozen=True)
class SignalBatch:
    """Immutable active-signal snapshot shared by every account in one run.

    ``signals`` is the raw fetch result (horizons = the union of the
    participating accounts), ``by_fill`` is the batch layout computed from that
    union, and ``batch_key`` pins ``market + fill_date + selected signal ids``.
    Nothing here is mutated by a run: each account gets its own working copy of
    the bucket, so carried-over orders can never leak between accounts.
    """

    batch_key: str
    market: str
    fill_date: Optional[date]
    reference_date: Optional[date]
    latest_data_date: Optional[date]
    signals: Tuple[Mapping[str, Any], ...] = ()
    by_fill: Mapping[date, Mapping[str, Mapping[str, Any]]] = field(default_factory=dict)
    signal_ids: Tuple[int, ...] = ()
    horizons_union: Tuple[str, ...] = ()
    fetched_at: Optional[datetime] = None
    warnings: Tuple[str, ...] = ()
    signal_day: Optional[date] = None
    is_catchup: bool = False
    is_primary: bool = False

    def bucket(self) -> Dict[str, Dict[str, Any]]:
        """Mutable per-account copy of this fill day's ``code -> signal``."""
        return _latest_by_code((self.by_fill.get(self.fill_date) or {}).values())

    def with_fill_date(self, day: Optional[date], *, is_catchup: bool = True) -> "SignalBatch":
        """Re-pin this snapshot to another fill day (used by the catch-up pass)."""
        bucket = self.by_fill.get(day) or {}
        ids = _signal_ids(bucket.values())
        return replace(
            self,
            batch_key=_batch_key(self.market, day, ids),
            fill_date=day,
            reference_date=_previous_trading_date(self.market, day) if day is not None else None,
            signal_ids=ids,
            signal_day=None,
            is_catchup=is_catchup,
            is_primary=False,
        )


class _ExecutionFailed(RuntimeError):
    """Execution raised after its audit row was opened and finalized.

    ``run_all_accounts`` re-raises nothing here: the row already records the
    failure, so it must not append a second one for the same attempt.
    """


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
        record_audit: bool = False,
    ) -> Dict[str, Any]:
        """Single-account compatibility entry point (CLI/API keep using this).

        Arguments, validation and the returned dict are unchanged: it resolves
        one policy from the arguments/env, builds one shared ``SignalBatch`` (the
        signals are still fetched exactly once) and delegates to
        :meth:`run_for_account`.

        ``batch_fill_date`` is internal: it pins the run to exactly that fill
        day and disables catch-up. The public catch-up pass calls it once per
        missed fill day that way, so every batch keeps its own open price,
        account state and idempotent ``trade_uid``.

        A dry run no longer writes ``paper_account_runs``; its audit dict is
        returned under ``audit`` instead (``record_audit=True`` re-enables the
        row for debugging, and an executing run always writes it).
        """
        market = (market or "cn").strip().lower()
        if market != "cn":
            raise ValueError("paper trading currently supports A-share market 'cn' only")
        if account_id is not None and int(account_id) <= 0:
            raise ValueError("account_id must be > 0")

        policy = resolve_policy(
            None,
            horizons=horizons,
            min_score=min_score,
            max_names=max_names,
            max_weight=max_weight,
            initial_cash=initial_cash,
        )
        batch = self.build_signal_batch(
            market=market,
            horizons_union=policy.horizons,
            signal_date=signal_date,
            fill_date=batch_fill_date,
        )
        return self.run_for_account(
            batch,
            account_id=account_id,
            policy=policy,
            dry_run=dry_run,
            force=force,
            account_name=account_name,
            record_audit=record_audit,
        )

    # ------------------------------------------------------------------
    # Shared signal batch: fetched once, consumed by every account
    # ------------------------------------------------------------------
    def build_signal_batches(
        self,
        *,
        market: str = "cn",
        accounts: Optional[Sequence[Any]] = None,
        horizons_union: Optional[Iterable[str]] = None,
        signal_date: Optional[date] = None,
        fill_date: Optional[date] = None,
    ) -> Dict[Optional[date], SignalBatch]:
        """Fetch the active signals once and freeze them into per-fill-day batches.

        ``horizons_union`` defaults to the union of ``accounts``' horizons, so a
        multi-account run still scans the signal store exactly one time; the
        per-account horizons filter happens later in :meth:`run_for_account`.
        Signals whose fill day has not arrived yet land in a later bucket (the
        trading calendar decides, never the wall clock), which is what keeps a
        batch immutable once the 18:00 analysis produced it.
        """
        market = (market or "cn").strip().lower()
        if market != "cn":
            raise ValueError("paper trading currently supports A-share market 'cn' only")

        union = _horizon_union(accounts=accounts, horizons_union=horizons_union)
        warnings: List[str] = []
        active = self._fetch_active_signals(market=market, horizons=set(union), warnings=warnings)
        latest_data_date = self._latest_market_date()
        by_fill = _bucket_by_fill(market=market, signals=active, warnings=warnings)

        if signal_date is not None:
            primary_fill_date = get_next_trading_date(market, signal_date)
            primary_bucket = _latest_by_code(
                [
                    signal
                    for signal in active
                    if _parse_signal_day(signal.get("created_at")) == signal_date
                ]
            )
        elif fill_date is not None:
            primary_fill_date = fill_date
            primary_bucket = by_fill.get(fill_date) or {}
        else:
            primary_fill_date = latest_data_date
            primary_bucket = by_fill.get(latest_data_date) or {} if latest_data_date is not None else {}

        days: List[Optional[date]] = sorted(day for day in by_fill if day is not None)
        if primary_fill_date not in days:
            days.append(primary_fill_date)

        batches: Dict[Optional[date], SignalBatch] = {}
        for day in days:
            is_primary = day == primary_fill_date
            ids = _signal_ids((primary_bucket if is_primary else by_fill.get(day) or {}).values())
            batches[day] = SignalBatch(
                batch_key=_batch_key(market, day, ids),
                market=market,
                fill_date=day,
                reference_date=_previous_trading_date(market, day) if day is not None else None,
                latest_data_date=latest_data_date,
                signals=tuple(dict(signal) for signal in active),
                by_fill=by_fill,
                signal_ids=ids,
                horizons_union=union,
                fetched_at=datetime.now(),
                warnings=tuple(warnings),
                signal_day=signal_date if is_primary else None,
                is_primary=is_primary,
            )
        return batches

    def build_signal_batch(self, **kwargs: Any) -> SignalBatch:
        """Return only the due batch of :meth:`build_signal_batches`."""
        batches = self.build_signal_batches(**kwargs)
        primary = next((item for item in batches.values() if item.is_primary), None)
        if primary is None:
            raise ValueError("纸面交易：无法确定本次成交批次。")
        return primary

    # ------------------------------------------------------------------
    # Multi-account run
    # ------------------------------------------------------------------
    def run_all_accounts(
        self,
        batch: Optional[SignalBatch] = None,
        *,
        dry_run: Optional[bool] = None,
        only_account_ids: Optional[Sequence[int]] = None,
        force: bool = False,
        record_audit: bool = False,
    ) -> Dict[str, Any]:
        """Run every auto-run paper account over one shared signal batch.

        Phase 1 of the multi-account design: one fetch, one plan pass per
        account, each under its own policy profile. The accounts come from
        ``paper_account_branches`` (``auto_run`` only), the signals come from
        the union of their horizons and are read exactly once, and every
        account is judged on its own state — one account failing never stops
        the others. ``PAPER_TRADING_AUTO_RUN`` stays the global switch and is
        decided by the caller, not here.

        ``batch`` lets a scheduler hand the same snapshot to several services;
        when it is omitted one is built for this call. ``only_account_ids``
        narrows the run to specific accounts (manual trigger) and skips the
        ``auto_run`` filter. ``force`` is a safety override rather than a
        strategy parameter, so it applies to every account: same-day fills are
        accepted instead of refused, while ``trade_uid`` still skips orders
        that were already executed. ``dry_run=None`` means "plan only", the
        same safe default as the single-account entry point. A dry run writes
        nothing to ``paper_account_runs`` — each account's audit dict comes
        back under ``accounts[].audit`` instead — unless ``record_audit`` is
        set.
        """
        warnings: List[str] = []
        if dry_run is None:
            dry_run = True
        persist_audit = bool(record_audit) or not dry_run
        market = (batch.market if batch is not None else "cn").strip().lower() or "cn"
        if market != "cn":
            raise ValueError("paper trading currently supports A-share market 'cn' only")

        repo = getattr(self.portfolio, "repo", None)

        # Backfill the branch rows so a first run already sees the trunk
        # account. Idempotent, and a failure here must not stop the run: the
        # fallback below still reproduces today's single-account behaviour.
        # Only the real portfolio repository gets the call — the bootstrap
        # writes, so a fake repo (unit tests) must never open a database, and a
        # dry run must never write a row.
        if not dry_run and callable(getattr(repo, "portfolio_write_session", None)):
            trunk_name = _env_text(PAPER_TRADING_ACCOUNT_NAME_ENV, DEFAULT_ACCOUNT_NAME).strip()
            names = sorted({name for name in (trunk_name, *DEFAULT_PAPER_ACCOUNT_NAMES) if name})
            try:
                ensure_paper_branch_defaults(repo=repo, account_names=names)
            except Exception as exc:  # noqa: BLE001 - defaults are best-effort
                logger.warning("Paper trading branch defaults failed: %s", exc)
                warnings.append(f"纸面交易：账户分支默认值回填失败：{exc}")

        rows: List[Dict[str, Any]] = []
        lister = getattr(repo, "list_branch_accounts", None)
        wanted_ids: List[int] = []
        for item in only_account_ids or ():
            try:
                wanted_ids.append(int(item))
            except (TypeError, ValueError):
                warnings.append(f"纸面交易：忽略非法账户 id {item!r}")
        if callable(lister):
            # List every branch (not just auto_run) so "no branch rows at all"
            # can be told apart from "all branches switched off": the former
            # falls back to the legacy trunk run, the latter must run nothing.
            try:
                rows = list(lister(auto_run_only=False) or [])
            except Exception as exc:  # noqa: BLE001 - the fallback keeps the trunk running
                logger.warning("Paper trading could not list branch accounts: %s", exc)
                warnings.append(f"纸面交易：读取账户分支失败，退回单账户主干：{exc}")
                rows = []
        by_id = {int(row.get("account_id")): row for row in rows if row.get("account_id") is not None}
        if wanted_ids:
            accounts = [
                by_id.get(account_id)
                or {"account_id": account_id, "account_name": None, "config": None,
                    "auto_run": None, "role": None, "state": None}
                for account_id in wanted_ids
            ]
        else:
            accounts = [row for row in rows if row.get("auto_run")]
        if not accounts and not rows:
            # No branch rows at all (fresh DB, or a dry run that skipped the
            # backfill): run the trunk account with the env/default policy,
            # exactly like ``run()`` does today.
            accounts = [{
                "account_id": None,
                "account_name": _env_text(PAPER_TRADING_ACCOUNT_NAME_ENV, DEFAULT_ACCOUNT_NAME).strip(),
                "config": None,
                "auto_run": None,
                "role": "live",
                "state": "live",
            }]
        if not accounts:
            # Branches exist but every one of them has auto_run=0: the operator
            # paused paper trading and the fallback must not quietly restart it.
            return {
                "status": "multi_no_accounts",
                "dry_run": dry_run,
                "message": "没有可自动运行的纸面账户（paper_account_branches.auto_run 全部为 0）。",
                "market": market,
                "batch_key": batch.batch_key if batch is not None else None,
                "batch": {},
                "accounts": [],
                "warnings": warnings,
            }

        prepared: List[Tuple[Dict[str, Any], Optional[PaperPolicy]]] = []
        policies: List[PaperPolicy] = []
        for row in accounts:
            account_label = row.get("account_name") or row.get("label") or f"#{row.get('account_id')}"
            row_market = str(row.get("market") or "cn").strip().lower() or "cn"
            if row_market != "cn":
                warnings.append(f"纸面交易：账户 {account_label} 市场为 {row_market}，本期只支持 A 股，跳过")
                continue
            notes: List[str] = []
            try:
                policy = _policy_from_branch(row, notes)
            except Exception as exc:  # noqa: BLE001 - a bad profile must not stop the others
                logger.warning("Paper trading policy for %s failed: %s", account_label, exc)
                warnings.append(f"纸面交易：账户 {account_label} 的策略档不可用：{exc}")
                prepared.append((row, None))
                continue
            warnings.extend(f"纸面交易：账户 {account_label} {note}" for note in notes)
            policies.append(policy)
            prepared.append((row, policy))

        if batch is None:
            batch = self.build_signal_batch(
                market=market,
                horizons_union=_horizon_union(accounts=policies) if policies else None,
            )

        # One bar prefetch for the whole account set: the codes are the union of
        # the due batch and every account's cached positions, so nothing is
        # fetched per account afterwards.
        shared_prefetch = len(prepared) > 1 and _env_bool(
            PAPER_TRADING_FETCH_MISSING_BARS_ENV, DEFAULT_FETCH_MISSING_BARS
        )
        if shared_prefetch:
            try:
                due_bucket = batch.bucket()
                codes: set = set()
                for row, _policy in prepared:
                    codes.update(self._batch_and_position_codes(
                        account={"id": row.get("account_id")},
                        batch=due_bucket,
                    ))
                # Cover every fill day the accounts may touch, catch-up days
                # included, so the per-account runs can skip the prefetch.
                prefetch_dates: set = set()
                for day in batch.by_fill:
                    prefetch_dates.add(day)
                    previous = _previous_trading_date(batch.market, day)
                    if previous is not None:
                        prefetch_dates.add(previous)
                if batch.reference_date is not None:
                    prefetch_dates.add(batch.reference_date)
                if batch.fill_date is not None:
                    prefetch_dates.add(batch.fill_date)
                self._ensure_daily_bars(
                    codes=sorted(codes),
                    dates=sorted(prefetch_dates),
                    warnings=warnings,
                )
            except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
                logger.warning("Paper trading shared bar prefetch failed: %s", exc)
                warnings.append(f"纸面交易：行情补齐失败：{exc}")

        results: List[Dict[str, Any]] = []
        for row, policy in prepared:
            # The policy is the resolved truth about which profile is in use:
            # a branch without one (or a legacy fallback run) shows up here as
            # ``env/default`` instead of a blank field.
            summary = {
                "account_id": row.get("account_id"),
                "account_name": row.get("account_name") or row.get("label"),
                "label": row.get("label"),
                "profile_id": policy.profile_id if policy else row.get("profile_id"),
                "profile_name": policy.profile_name if policy else row.get("profile_name"),
                "profile_version": policy.profile_version if policy else row.get("profile_version"),
                "auto_run": row.get("auto_run"),
                "role": row.get("role"),
                "state": row.get("state"),
                "status": STATUS_ACCOUNT_FAILED,
                "message": "",
                "orders": 0,
                "warnings": [],
            }
            if policy is None:
                summary["message"] = "策略档解析失败，本次未运行"
                results.append(summary)
                continue
            try:
                payload = self.run_for_account(
                    batch,
                    account_id=row.get("account_id"),
                    account_name=row.get("account_name"),
                    policy=policy,
                    dry_run=dry_run,
                    force=force,
                    prefetch_missing_bars=not shared_prefetch,
                    record_audit=record_audit,
                )
            except _ExecutionFailed as exc:
                # The row was opened before execution and finalized as failed;
                # appending another one would double-count the same attempt.
                logger.error("Paper trading execution failed for account %s: %s", summary["account_name"], exc)
                summary["message"] = f"运行失败：{exc}"
                results.append(summary)
                continue
            except Exception as exc:  # noqa: BLE001 - one account must not break the batch
                logger.exception("Paper trading run failed for account %s", summary["account_name"])
                summary["message"] = f"运行失败：{exc}"
                results.append(summary)
                audit = self._record_account_run(
                    account_id=row.get("account_id"),
                    batch=batch,
                    policy=policy,
                    fill_date=batch.fill_date,
                    result=None,
                    decisions={
                        "status": STATUS_ACCOUNT_FAILED,
                        "message": summary["message"],
                        "dry_run": dry_run,
                        "batch_key": batch.batch_key,
                        "signal_ids": list(batch.signal_ids),
                        "horizons_union": sorted(batch.horizons_union),
                        "policy": policy.as_dict(),
                    },
                    error=str(exc),
                    persist=persist_audit,
                )
                if not persist_audit:
                    summary["audit"] = audit
                continue
            summary.update({
                "status": str(payload.get("status") or ""),
                "message": str(payload.get("message") or ""),
                "orders": len(payload.get("orders") or []),
                "warnings": list(payload.get("warnings") or []),
            })
            if payload.get("audit") is not None:
                # dry-run audit stays in the result only — nothing is written
                summary["audit"] = payload["audit"]
            results.append(summary)

        executed = sum(1 for row in results if row["status"] == STATUS_EXECUTED)
        failed = sum(1 for row in results if row["status"] == STATUS_ACCOUNT_FAILED)
        message = (
            f"多账户干跑：{len(results)} 个账户，批次 {batch.batch_key}。"
            if dry_run else
            f"多账户执行：{len(results)} 个账户，成交 {executed} 个，失败 {failed} 个。"
        )
        # Describe the batch that was actually planned, not every active
        # signal: signals whose fill day already passed belong to an older
        # (catch-up) batch, and listing them here overstates the window.
        batch_days = sorted({
            day
            for day in (_parse_signal_day(signal.get("created_at")) for signal in batch.bucket().values())
            if day is not None
        })
        return {
            "status": "multi_executed" if not dry_run else "multi_planned",
            "dry_run": dry_run,
            "message": message,
            "market": market,
            "batch_key": batch.batch_key,
            "batch": {
                "fill_date": _iso_or_none(batch.fill_date),
                "reference_date": _iso_or_none(batch.reference_date),
                "signal_dates": [day.isoformat() for day in batch_days],
                "latest_data_date": _iso_or_none(batch.latest_data_date),
            },
            "accounts": results,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Account run
    # ------------------------------------------------------------------
    def run_for_account(
        self,
        batch: SignalBatch,
        *,
        account_id: Optional[int] = None,
        policy: Optional[PaperPolicy] = None,
        dry_run: bool = True,
        force: bool = False,
        account_name: Optional[str] = None,
        prefetch_missing_bars: bool = True,
        record_audit: bool = False,
    ) -> Dict[str, Any]:
        """Replay one shared batch for one account under its own policy.

        Everything account-scoped lives here: the horizons filter, corporate
        actions, this account's catch-up, planning, execution and the snapshot
        refresh. The signal fetch belongs to the batch, and the daily-bar
        prefetch belongs to the caller when several accounts share one batch
        (``prefetch_missing_bars=False``), so nothing is fetched twice. The batch
        itself is never mutated: the working bucket is a private copy, which is
        what keeps a carried-over order from leaking into another account.

        ``record_audit`` is the debug escape hatch for the audit table: a dry
        run writes nothing to ``paper_account_runs`` unless it is set, and the
        audit dict is then only returned to the caller under ``audit``.
        """
        policy = policy or resolve_policy(None)
        # 独立风控闸门配置：空策略档 → 全关（零行为变化）；仅新增风控键显式配置才启用
        risk_cfg = rc.RiskConfig.from_profile(policy.config)
        persist_audit = bool(record_audit) or not dry_run
        market = batch.market
        account_name = (account_name or _env_text(PAPER_TRADING_ACCOUNT_NAME_ENV, DEFAULT_ACCOUNT_NAME)).strip()
        warnings: List[str] = []
        horizon_set = {str(item).strip() for item in policy.horizons if str(item).strip()}

        if tuple(sorted(horizon_set)) == tuple(sorted(batch.horizons_union)):
            # Same horizons as the snapshot: reuse it instead of filtering twice.
            active = [dict(signal) for signal in batch.signals]
            by_fill = {
                day: {code: dict(signal) for code, signal in bucket.items()}
                for day, bucket in batch.by_fill.items()
            }
            warnings.extend(batch.warnings)
        else:
            active = [
                dict(signal)
                for signal in batch.signals
                if not horizon_set or str(signal.get("horizon") or "") in horizon_set
            ]
            dropped = len(batch.signals) - len(active)
            if dropped:
                warnings.append(f"按 horizon={sorted(horizon_set)} 过滤掉 {dropped} 条活跃信号")
            by_fill = _bucket_by_fill(market=market, signals=active, warnings=warnings)

        signal_counts = {"active": len(active), "batch": 0, "carried": 0}
        latest_data_date = batch.latest_data_date
        corporate_actions: List[str] = []
        min_score = policy.min_score
        max_names = policy.max_names
        max_weight = policy.max_weight
        initial_cash = policy.initial_cash
        retry_days = policy.retry_days
        slippage = policy.slippage_bps
        adv_pct = policy.max_adv_pct
        adv_window_days = max(1, policy.adv_window_days)
        auto_corporate_actions = _env_bool(PAPER_TRADING_CORPORATE_ACTIONS_ENV, DEFAULT_CORPORATE_ACTIONS)

        # A run may still have work to do (exit rules) when no signal is due, so
        # the old early returns are captured here and only used once planning
        # produced nothing.
        early_result: Optional[Dict[str, Any]] = None
        catchup_payload: Optional[Dict[str, Any]] = None
        due: Dict[str, Dict[str, Any]] = {}
        fill_date: Optional[date] = batch.fill_date
        carry_days: Dict[str, date] = {}
        plan_audit: Dict[str, Any] = {}
        audit_account_id: Optional[int] = account_id
        run_state: Dict[str, Optional[int]] = {"run_id": None}

        def finish(payload: Dict[str, Any]) -> Dict[str, Any]:
            # Catch-up sub-runs pin their own fill day; the health check belongs
            # to the public run only and reflects the latest holdings.
            if not batch.is_catchup:
                try:
                    health = self._position_stop_health(
                        account_id=account_id,
                        account_name=account_name,
                        as_of=latest_data_date or date.today(),
                        market=market,
                        warnings=warnings,
                        risk_cfg=risk_cfg,
                        exit_config={
                            "stop_loss_pct": policy.stop_loss_pct,
                            "take_profit_pct": policy.take_profit_pct,
                            "max_holding_days": policy.max_holding_days,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - the check must never break the run
                    logger.warning("Paper trading stop-health check failed: %s", exc)
                    payload.setdefault("warnings", []).append(f"持仓止损体检失败：{exc}")
                else:
                    if health is not None:
                        payload["position_health"] = health
                        naked = [row["code"] for row in health["no_stop_positions"]]
                        if naked:
                            payload.setdefault("warnings", []).append(
                                f"持仓体检：{'、'.join(naked)} 的建仓信号没有止损价，属于无止损保护仓位"
                            )
            payload = self._merge_catchup(payload, catchup_payload)
            audit = self._record_account_run(
                account_id=audit_account_id,
                batch=batch,
                policy=policy,
                fill_date=fill_date,
                result=payload,
                persist=persist_audit,
                run_id=run_state.get("run_id"),
                decisions={
                    "status": payload.get("status"),
                    "message": payload.get("message"),
                    "dry_run": payload.get("dry_run"),
                    "account_id": audit_account_id,
                    "account_name": account_name,
                    "fill_date": _iso_or_none(fill_date),
                    "reference_date": _iso_or_none(batch.reference_date),
                    "latest_data_date": _iso_or_none(latest_data_date),
                    "batch_key": batch.batch_key,
                    "signal_ids": list(batch.signal_ids),
                    "horizons_union": sorted(batch.horizons_union),
                    "horizons": sorted(horizon_set),
                    "signal_day": _iso_or_none(batch.signal_day),
                    "is_catchup": batch.is_catchup,
                    "signal_counts": dict(signal_counts),
                    "active_signal_ids": list(_signal_ids(active)),
                    "due_signal_ids": list(_signal_ids(due.values())),
                    "carried": {code: day.isoformat() for code, day in carry_days.items()},
                    "exit_levels": plan_audit.get("exit_levels") or {},
                    "policy": policy.as_dict(),
                    "warnings": list(payload.get("warnings") or warnings),
                    "catchup_days": [
                        str(row.get("fill_date")) for row in ((catchup_payload or {}).get("results") or [])
                    ],
                },
            )
            if not persist_audit:
                # FIX-1: a dry run leaves no trace in the ledger tables; the
                # caller still gets the exact row it *would* have written.
                payload["audit"] = audit
            return payload

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
            if batch.signal_day is not None or latest_data_date is None:
                return finish(early_result)
            fill_date = latest_data_date
        else:
            if batch.signal_day is None and not batch.is_catchup and latest_data_date is not None:
                catchup_payload = self._run_catchup_batches(
                    batch=batch,
                    by_fill=by_fill,
                    latest_data_date=latest_data_date,
                    account_id=account_id,
                    account_name=account_name,
                    market=market,
                    policy=policy,
                    dry_run=dry_run,
                    force=force,
                    record_audit=record_audit,
                    prefetch_missing_bars=prefetch_missing_bars,
                )

            if batch.signal_day is not None:
                due = _latest_by_code(
                    [
                        signal
                        for signal in active
                        if _parse_signal_day(signal.get("created_at")) == batch.signal_day
                    ]
                )
                if not due:
                    return finish(self._result(
                        STATUS_NO_DUE_BATCH,
                        f"{batch.signal_day} 没有活跃信号（该批可能已过期）。",
                        market=market,
                        dry_run=dry_run,
                        latest_data_date=latest_data_date,
                        signal_counts=signal_counts,
                        warnings=warnings,
                    ))
                fill_date = get_next_trading_date(market, batch.signal_day)
            elif batch.is_catchup:
                due = _latest_by_code((by_fill.get(fill_date) or {}).values())
                if not due:
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
                due = _latest_by_code((by_fill.get(fill_date) or {}).values())

                if not due:
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
                    for day in (_parse_signal_day(signal.get("created_at")) for signal in due.values())
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
        if account_pk is not None:
            audit_account_id = int(account_pk)

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
        # Carried orders and resting exits are evaluated per session; the
        # catch-up pass runs every missed session in order, so a blocked order
        # can retry on the next session instead of jumping straight to today.
        if batch.signal_day is None:
            carry_days = self._merge_carried_signals(
                batch=due,
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
                for day in (_parse_signal_day(signal.get("created_at")) for signal in due.values())
                if day is not None
            }
        )
        signal_counts["batch"] = len(due)
        batch_payload = {
            "signal_dates": [day.isoformat() for day in batch_days],
            "fill_date": fill_date.isoformat(),
            "reference_date": reference_date.isoformat(),
        }

        if prefetch_missing_bars and _env_bool(PAPER_TRADING_FETCH_MISSING_BARS_ENV, DEFAULT_FETCH_MISSING_BARS):
            self._ensure_daily_bars(
                codes=self._batch_and_position_codes(account=account, batch=due),
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
            batch=due,
            fill_date=fill_date,
            reference_date=reference_date,
            cash=cash,
            positions=positions,
            per_name=per_name,
            max_names=max_names,
            min_score=min_score,
            warnings=warnings,
            trades=paper_trades,
            exit_config={
                "stop_loss_pct": policy.stop_loss_pct,
                "take_profit_pct": policy.take_profit_pct,
                "max_holding_days": policy.max_holding_days,
            },
            carry_days=carry_days,
            allowed_boards=allowed_boards(),
            slippage_bps=slippage,
            max_adv_pct=adv_pct,
            adv_window_days=adv_window_days,
            equity=equity,
            risk_cfg=risk_cfg,
            plan_audit=plan_audit,
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

        def pending_new_signal_note() -> str:
            """盘前干跑事实说明：信号日=最新收盘日的新信号属下一交易日 due 批。

            N 与日期全部来自 batch/信号真实状态（by_fill + created_at + 最新收盘日），
            不硬编码；避免"本批 0 笔新委托"被误读为信号断供。
            """
            if latest_data_date is None:
                return ""
            pending = [
                (day, signal)
                for day, bucket in batch.by_fill.items()
                if day is not None and day > fill_date
                for signal in bucket.values()
                if _parse_signal_day(signal.get("created_at")) == latest_data_date
            ]
            if not pending:
                return ""
            next_fill = min(day for day, _signal in pending)
            when = (
                "待今日收盘数据发布后于今晚跑批执行"
                if next_fill == date.today()
                else f"待 {next_fill.isoformat()} 收盘数据发布后跑批执行"
            )
            return f" 另有 {len(pending)} 条新信号（信号日 {latest_data_date.isoformat()}）{when}。"

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
                + (f" 自动登记分红送转 {len(corporate_actions)} 笔。" if corporate_actions else "")
                + (pending_new_signal_note() if dry_run else ""),
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
            # 干跑也做 trade_uid 去重标注（仅显示，执行阶段幂等跳过保持不动）：
            # 克隆账户继承的旧委托 uid 已存在于成交流水，标 duplicate 而非 planned，
            # 汇总笔数排除这些，避免"本批 N 笔将成交"误导。
            dry_existing_uids = {str(getattr(trade, "trade_uid", "") or "") for trade in paper_trades}
            public_orders: List[Dict[str, Any]] = []
            new_count = 0
            dup_count = 0
            for order in orders:
                uid = self._trade_uid(
                    order.get("uid_date") or fill_date,
                    order["stock_code"],
                    order["side"],
                    tag=str(order.get("uid_tag") or ""),
                )
                duplicated = uid in dry_existing_uids
                if duplicated:
                    dup_count += 1
                else:
                    new_count += 1
                public_orders.append({
                    **_public_order(order),
                    "trade_uid": uid,
                    "status": "duplicate" if duplicated else "planned",
                    "trade_id": None,
                    "reason": "重复跳过（克隆继承或已成交，trade_uid 幂等）" if duplicated else None,
                })
            dry_message = f"干跑：本批 {new_count} 笔委托将按 {fill_date} 开盘价成交{cost_note}（加 dry_run=false 执行）。"
            if dup_count:
                dry_message += f" 另有 {dup_count} 笔委托 trade_uid 已存在，执行时将重复跳过（克隆继承/已成交）。"
            if new_count == 0:
                dry_message += pending_new_signal_note()
            return finish(self._result(
                STATUS_PLANNED,
                dry_message,
                orders=public_orders,
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

        # The audit row opens before any trade is written: a killed process
        # then leaves a ``running`` row instead of no trace at all.
        if not dry_run:
            run_state["run_id"] = self._begin_account_run(
                account_id=audit_account_id,
                batch=batch,
                policy=policy,
                decisions={
                    "status": STATUS_RUNNING,
                    "message": "已开始执行，等待终态回填",
                    "dry_run": False,
                    "account_id": audit_account_id,
                    "account_name": account_name,
                    "fill_date": _iso_or_none(fill_date),
                    "reference_date": _iso_or_none(batch.reference_date),
                    "latest_data_date": _iso_or_none(latest_data_date),
                    "batch_key": batch.batch_key,
                    "signal_ids": list(batch.signal_ids),
                    "due_signal_ids": list(_signal_ids(due.values())),
                    "horizons_union": sorted(batch.horizons_union),
                    "horizons": sorted(horizon_set),
                    "signal_day": _iso_or_none(batch.signal_day),
                    "is_catchup": batch.is_catchup,
                    "policy": policy.as_dict(),
                },
            )
        try:
            executed = self._execute_orders(
                account_id=int(account["id"]), fill_date=fill_date, orders=orders, warnings=warnings
            )
            base["warnings"] = warnings
            self._refresh_snapshot(account_id=int(account["id"]), fill_date=fill_date, warnings=warnings)
        except Exception as exc:
            if run_state.get("run_id") is not None:
                self._record_account_run(
                    account_id=audit_account_id,
                    batch=batch,
                    policy=policy,
                    fill_date=fill_date,
                    result={"status": STATUS_ACCOUNT_FAILED, "orders": []},
                    decisions={"status": STATUS_ACCOUNT_FAILED, "message": f"执行失败：{exc}"},
                    error=str(exc),
                    persist=True,
                    run_id=run_state.get("run_id"),
                )
            raise _ExecutionFailed(str(exc)) from exc

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
    # Run audit
    # ------------------------------------------------------------------
    def _begin_account_run(
        self,
        *,
        account_id: Optional[int],
        batch: SignalBatch,
        policy: PaperPolicy,
        decisions: Dict[str, Any],
    ) -> Optional[int]:
        """Open the audit row *before* executing orders; returns its id.

        A run killed between the trade commit and the terminal audit write
        would otherwise leave no trace at all. The row starts as ``running``
        and :meth:`_record_account_run` replaces it with the terminal status,
        so a leftover ``running`` row is proof that the attempt started and
        the trades may already be durable.
        """
        if account_id is None:
            return None
        repo = getattr(self.portfolio, "repo", None)
        recorder = getattr(repo, "record_account_run", None)
        if not callable(recorder):
            return None
        try:
            row = recorder(
                account_id=int(account_id),
                batch_key=batch.batch_key,
                profile_id=policy.profile_id,
                status=STATUS_RUNNING,
                decisions=decisions,
                orders=[],
            )
        except Exception as exc:  # noqa: BLE001 - audit failures must not break the run
            logger.warning("Paper trading audit pre-write failed (account=%s): %s", account_id, exc)
            return None
        try:
            return int(getattr(row, "id"))
        except (TypeError, ValueError):
            return None

    def _record_account_run(
        self,
        *,
        account_id: Optional[int],
        batch: SignalBatch,
        policy: PaperPolicy,
        fill_date: Optional[date],
        result: Optional[Dict[str, Any]],
        decisions: Dict[str, Any],
        error: Optional[str] = None,
        persist: bool = False,
        run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Describe one run: returned as the ``audit`` dict, persisted on demand.

        Pure bookkeeping: it never changes the accounting, and an older
        database (or a fake repo in the unit tests) without the method simply
        skips the row. A write failure becomes a warning instead of turning a
        successful trading run into a failed one. Runs that resolved no account
        (a dry run against a name that does not exist yet) have nothing to
        attach the row to and are skipped.

        ``persist`` is the only thing that touches the database, and the
        callers set it from ``record_audit or not dry_run`` — a plan-only run
        therefore leaves no row behind and the same dict goes back to the
        caller inside the result.  The returned view also carries the row's
        ``orders`` column, so a dry run can be checked without the table.

        ``run_id`` finalizes the row opened by :meth:`_begin_account_run`
        instead of appending a second one. If that row is gone the terminal
        row is inserted anyway, so the audit is never lost.
        """
        orders = list((result or {}).get("orders") or [])
        status = str((result or {}).get("status") or decisions.get("status") or "")
        row_id = int(run_id) if run_id is not None else None
        if persist and account_id is not None:
            repo = getattr(self.portfolio, "repo", None)
            if row_id is not None:
                updater = getattr(repo, "update_account_run", None)
                if callable(updater):
                    try:
                        updated = updater(
                            run_id=row_id,
                            status=status,
                            decisions=decisions,
                            orders=orders,
                            error=error,
                        )
                    except Exception as exc:  # noqa: BLE001 - audit failures must not break the run
                        logger.warning("Paper trading audit update failed (run=%s): %s", row_id, exc)
                        if isinstance(result, dict):
                            result.setdefault("warnings", []).append(f"纸面交易：运行审计更新失败：{exc}")
                        updated = None
                    if updated is None:
                        row_id = None
            if row_id is None:
                recorder = getattr(repo, "record_account_run", None)
                if callable(recorder):
                    try:
                        recorder(
                            account_id=int(account_id),
                            batch_key=batch.batch_key,
                            profile_id=policy.profile_id,
                            status=status,
                            decisions=decisions,
                            orders=orders,
                            error=error,
                        )
                    except Exception as exc:  # noqa: BLE001 - audit failures must not break the run
                        logger.warning("Paper trading audit row failed (account=%s): %s", account_id, exc)
                        if isinstance(result, dict):
                            result.setdefault("warnings", []).append(f"纸面交易：运行审计写入失败：{exc}")
        audit = dict(decisions)
        audit["orders"] = orders
        if error:
            audit["error"] = error
        return audit

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

    @staticmethod
    def _latest_buy_by_code(trades: Sequence[Any]) -> Dict[str, Any]:
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
        return latest_buy

    def _signal_for_trade(self, trade: Any) -> Dict[str, Any]:
        """The signal that opened one buy, or ``{}`` when it was deleted/unreadable."""
        signal_id = _signal_id_from_note(getattr(trade, "note", None))
        getter = getattr(self.signals, "get_signal", None)
        if signal_id is None or not callable(getter):
            return {}
        try:
            return getter(int(signal_id)) or {}
        except Exception:  # noqa: BLE001 - a deleted signal disables the signal rule
            return {}

    def _exit_levels(
        self,
        trades: Sequence[Any],
        exit_config: Optional[Mapping[str, Any]] = None,
        risk_cfg: Optional[rc.RiskConfig] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Latest paper buy per symbol -> the resting stop/target to enforce.

        Re-reading the signal (instead of copying the levels into the trade)
        keeps one source of truth and survives signal reassessment. A profile's
        ``stop_loss_pct``/``take_profit_pct`` are converted from the latest buy
        price and override the signal's absolute levels.
        """
        latest_buy = self._latest_buy_by_code(trades)
        levels: Dict[str, Dict[str, Any]] = {}
        if not latest_buy:
            return levels
        for code, trade in latest_buy.items():
            signal = self._signal_for_trade(trade)
            buy_price = _positive_float(getattr(trade, "price", None))
            stop, target, stop_source, target_source = _effective_exit_levels(
                buy_price, signal, exit_config
            )
            # P0 兜底（仅补缺，第三优先级）：signal/profile 都没有止损才补，不动既有键语义
            if stop is None and risk_cfg is not None and risk_cfg.fallback_stop_enabled and buy_price:
                stop, stop_source = rc.fallback_stop(
                    rc.PositionView(code, 0, buy_price, None, ""), risk_cfg)
            if stop is None and target is None:
                continue
            levels[code] = {
                "stop": stop,
                "target": target,
                # A deleted signal still yields a valid identity for the plan row.
                "signal": signal or {"stock_code": code},
                "source": {"stop": stop_source, "target": target_source},
            }
        return levels

    def _position_stop_health(
        self,
        *,
        account_id: Optional[int],
        account_name: str,
        as_of: date,
        market: str,
        warnings: List[str],
        exit_config: Optional[Mapping[str, Any]] = None,
        risk_cfg: Optional[rc.RiskConfig] = None,
    ) -> Optional[Dict[str, Any]]:
        """Flag holdings without a stop_loss and bucket stop distances.

        Read-only: resolves the account without creating it and never writes.
        """
        account, _ = self._resolve_account(
            account_id=account_id,
            account_name=account_name,
            dry_run=True,
        )
        if account.get("id") is None:
            return None
        account_pk = int(account["id"])
        _, positions, _ = self._load_state(
            account=account,
            reference_date=as_of,
            market=market,
            initial_cash=1.0,
            warnings=warnings,
        )
        trades = self._paper_trades(account_pk, as_of=as_of, warnings=warnings)
        latest_buy = self._latest_buy_by_code(trades)
        getter = getattr(self.signals, "get_signal", None)

        no_stop: List[Dict[str, Any]] = []
        distances: List[Dict[str, Any]] = []
        distribution = {
            "within_3pct": 0,
            "between_3_5pct": 0,
            "between_5_8pct": 0,
            "over_8pct": 0,
            "unknown_price": 0,
        }
        for code, position in sorted(positions.items()):
            if int(position["quantity"]) <= 0:
                continue
            last_price = float(position.get("last_price") or 0.0)
            buy = latest_buy.get(code)
            signal_id = _signal_id_from_note(getattr(buy, "note", None)) if buy is not None else None
            signal: Dict[str, Any] = {}
            if signal_id is not None and callable(getter):
                try:
                    signal = getter(int(signal_id)) or {}
                except Exception:  # noqa: BLE001 - a deleted signal means no protection
                    signal = {}
            stop, target, _, _ = _effective_exit_levels(
                _positive_float(getattr(buy, "price", None)) if buy is not None else None,
                signal,
                exit_config,
            )
            # P0 兜底同步到体检链路（与 _exit_levels 一致），避免看板仍报"无止损保护"
            if stop is None and risk_cfg is not None and risk_cfg.fallback_stop_enabled and buy is not None:
                buy_price = _positive_float(getattr(buy, "price", None))
                if buy_price:
                    stop, _src = rc.fallback_stop(
                        rc.PositionView(code, 0, buy_price, None, ""), risk_cfg)
            if stop is None:
                no_stop.append({
                    "code": code,
                    "quantity": int(position["quantity"]),
                    "last_price": last_price or None,
                    "signal_id": signal_id,
                    "target_price": target,
                })
                continue
            distance_pct: Optional[float] = None
            if last_price > 0:
                distance_pct = round((stop / last_price - 1.0) * 100.0, 2)
                magnitude = abs(distance_pct)
                if magnitude <= 3.0:
                    distribution["within_3pct"] += 1
                elif magnitude <= 5.0:
                    distribution["between_3_5pct"] += 1
                elif magnitude <= 8.0:
                    distribution["between_5_8pct"] += 1
                else:
                    distribution["over_8pct"] += 1
            else:
                distribution["unknown_price"] += 1
            distances.append({
                "code": code,
                "last_price": last_price or None,
                "stop_loss": stop,
                "distance_pct": distance_pct,
            })

        held_names = len(no_stop) + len(distances)
        return {
            "as_of": as_of.isoformat(),
            "held_names": held_names,
            "covered_names": len(distances),
            "no_stop_positions": no_stop,
            "stop_distances": distances,
            "distance_distribution": distribution,
        }

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
        exit_config: Optional[Mapping[str, Any]] = None,
        carry_days: Optional[Dict[str, date]] = None,
        allowed_boards: Iterable[str] = DEFAULT_ALLOWED_BOARDS,
        slippage_bps: float = 0.0,
        max_adv_pct: float = 0.0,
        adv_window_days: int = DEFAULT_ADV_WINDOW_DAYS,
        equity: Optional[Decimal] = None,
        risk_cfg: Optional[rc.RiskConfig] = None,
        plan_audit: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Decimal]:
        tradable = frozenset(allowed_boards)
        items: List[Dict[str, Any]] = []
        orders: List[Dict[str, Any]] = []
        fill_bars: Dict[str, Optional[StockDaily]] = {}
        ref_closes: Dict[str, Optional[float]] = {}
        adv_limits: Dict[str, Optional[int]] = {}
        carry_days = carry_days or {}
        max_holding_days = _positive_int(
            exit_config.get("max_holding_days") if isinstance(exit_config, Mapping) else None
        )
        exit_levels = self._exit_levels(trades, exit_config, risk_cfg=risk_cfg) if trades else {}
        # 风控冷却输入：仅在启用冷却时组装流水视图与交易日历（默认全关零开销）。
        # exit_kind 只认 note 动作标记（_order_note 追加），不做价格推断（跳空/滑点会漏判）。
        risk_trades: List[rc.TradeView] = []
        risk_calendar: List[str] = []
        if risk_cfg is not None and risk_cfg.cooldown_scope in ("stop_only", "all") and trades:
            for t in trades:
                t_day = getattr(t, "trade_date", None)
                day_iso = t_day.isoformat() if hasattr(t_day, "isoformat") else str(t_day or "")
                risk_trades.append(rc.TradeView(
                    _plain_code(getattr(t, "symbol", "")), day_iso,
                    str(getattr(t, "side", "")), float(getattr(t, "quantity", 0) or 0),
                    float(getattr(t, "price", 0) or 0),
                    exit_kind=rc.resolve_exit_kind(note=getattr(t, "note", None)),
                ))
            # 日历窗口 ≥ cooldown_days+1 个交易日且含今日；失败降级空列表（模块会再告警）
            span = max(30, 3 * (risk_cfg.cooldown_days + 2))
            sessions = get_trading_dates("cn", fill_date - timedelta(days=span), fill_date)
            if sessions is None or len(sessions) == 0:
                warnings.append("交易日历获取失败，冷却未校验（fail-open）")
            else:
                risk_calendar = [s.date().isoformat() if hasattr(s, "date") else str(s) for s in sessions]
        exited_today: set = set()
        if plan_audit is not None:
            # D1: the exit rules keep reading the signal store (contract
            # unchanged), but the audit row records which signal and which
            # stop/target were actually read, so a silent reassessment is
            # visible after the fact instead of being guessed from the fills.
            plan_audit["exit_levels"] = {
                code: {
                    "stop": level.get("stop"),
                    "target": level.get("target"),
                    "signal_id": (level.get("signal") or {}).get("id"),
                    "source": dict(level.get("source") or {}),
                }
                for code, level in exit_levels.items()
            }
            plan_audit["signal_ids"] = list(_signal_ids(batch.values()))
            plan_audit["max_holding_days"] = max_holding_days

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

        # 时间止盈（持有到期）：exit.max_holding_days 打开时，持仓满 N 个交易日
        # 在下一交易日开盘卖出。语义是"信号 horizon 到期后不再无限期扛单"，
        # 与止损/止盈同层，但按开盘价成交（先于盘中触发），不受今日信号动作影响。
        if max_holding_days and trades:
            latest_buys = self._latest_buy_by_code(trades)
            calendar_gap_warned = False
            for code in sorted(positions):
                position = positions.get(code)
                if not position or int(position["quantity"]) <= 0:
                    continue
                if code in exited_today:
                    continue
                buy = latest_buys.get(code)
                buy_date = getattr(buy, "trade_date", None) if buy is not None else None
                if not isinstance(buy_date, date):
                    continue
                sessions = self._sessions_between(
                    "cn", buy_date + timedelta(days=1), fill_date + timedelta(days=1)
                )
                if not sessions:
                    if not calendar_gap_warned:
                        warnings.append("交易日历不可用，时间止盈（max_holding_days）未校验")
                        calendar_gap_warned = True
                    continue
                held = len(sessions)
                if held < max_holding_days:
                    continue
                signal_row = self._signal_for_trade(buy)
                if not signal_row:
                    signal_row = {"stock_code": code}
                bar = fill_bar(code)
                open_price = float(bar.open) if bar is not None and bar.open else None
                previous_close = ref_close(code)
                if open_price is None or open_price <= 0:
                    items.append(
                        item(
                            signal_row,
                            action="time_exit",
                            reference_price=previous_close,
                            reason=f"持有满 {held} 个交易日，但成交日无行情（停牌/数据未更新），继续持有",
                        )
                    )
                    continue
                if (
                    previous_close
                    and is_limit_down(Decimal(str(previous_close)), Decimal(str(open_price)), price_limit_pct(code))
                ):
                    items.append(
                        item(
                            signal_row,
                            action="time_exit",
                            reference_price=previous_close,
                            price=open_price,
                            reason=f"持有满 {held} 个交易日，但开盘跌停卖不出，继续持有",
                        )
                    )
                    continue
                quantity = int(position["quantity"])
                cap = adv_cap(code)
                if cap is not None:
                    quantity = min(quantity, cap)
                    if quantity <= 0:
                        items.append(
                            item(
                                signal_row,
                                action="time_exit",
                                reference_price=previous_close,
                                price=open_price,
                                reason=f"持有满 {held} 个交易日，但{liquidity_reason(0)}，无法卖出，继续持有",
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
                remaining = int(position["quantity"]) - quantity
                position["quantity"] = remaining
                position["market_value"] = (
                    float(to_cent(Decimal(str(previous_close)) * remaining))
                    if previous_close and remaining > 0
                    else 0.0
                )
                partial_note = "（受流动性上限部分成交，余量次日继续）" if remaining > 0 else ""
                plan_item = item(
                    signal_row,
                    action="time_exit",
                    side="sell",
                    quantity=quantity,
                    price=fill_price,
                    reference_price=previous_close,
                    fee=fee,
                    tax=tax,
                    status="planned",
                    reason=f"时间止盈：持有满 {held} 个交易日（上限 {max_holding_days}），按开盘价卖出{partial_note}",
                )
                items.append(plan_item)
                orders.append(order_of(plan_item, uid_tag="time_exit"))
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
            level_source = (level.get("source") or {}).get(
                "stop" if trigger == "stop_loss" else "target"
            )
            source_text = "策略档" if level_source == "profile" else "信号"
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
                reason=f"{label}（触发价 {exit_price:.2f}，持仓成本对应{source_text}）{partial_note}",
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
            # 独立风控闸门（RR/冷却/预算/最小金额）：默认全关时仅整手校验，主板数量不变
            if risk_cfg is not None:
                gate = rc.evaluate_entry(
                    rc.SignalView(signal.get("id"), code,
                                  stop_loss=signal.get("stop_loss"),
                                  target_price=signal.get("target_price"),
                                  entry_low=signal.get("entry_low"),
                                  entry_high=signal.get("entry_high")),
                    price=fill_price, intended_qty=quantity,
                    trades=risk_trades,
                    equity=float(equity) if equity is not None else 0.0,
                    cash=float(cash), today=fill_date.isoformat(),
                    trading_days=risk_calendar, config=risk_cfg)
                if not gate.passed:
                    items.append(item(signal, reference_price=reference_price, price=fill_price,
                                      reason=f"风控拦截[{gate.rule}]：{'；'.join(gate.reasons)}"))
                    continue
                quantity = gate.final_qty
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
        batch: SignalBatch,
        by_fill: Dict[date, Dict[str, Dict[str, Any]]],
        latest_data_date: date,
        account_id: Optional[int],
        account_name: str,
        market: str,
        policy: PaperPolicy,
        dry_run: bool,
        force: bool,
        record_audit: bool = False,
        prefetch_missing_bars: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Replay missed sessions in ascending order, each at its own open.

        Every session goes through the normal pipeline (same fees, limits,
        suspensions, corporate actions and trade_uid idempotency), so a
        re-run never duplicates a fill and a blocked order can retry on the
        next session within the window. The signals come from the shared
        snapshot: a catch-up session re-pins ``batch`` to its own fill day
        instead of scanning the signal store again, which is also what keeps
        every account in a multi-account run on the same batch.
        """

        due_days = sorted(day for day in by_fill if day < latest_data_date)
        if not due_days:
            return None
        max_days = max(1, int(policy.catchup_days))
        selected = due_days[:max_days]
        results: List[Dict[str, Any]] = []
        for day in selected:
            sub = self.run_for_account(
                batch.with_fill_date(day),
                account_id=account_id,
                account_name=account_name,
                policy=policy,
                dry_run=dry_run,
                force=force,
                record_audit=record_audit,
                prefetch_missing_bars=prefetch_missing_bars,
            )
            row = {
                "fill_date": day.isoformat(),
                "status": sub.get("status"),
                "message": sub.get("message"),
                "orders": sub.get("orders") or [],
                "items": sub.get("items") or [],
                "warnings": sub.get("warnings") or [],
                "corporate_actions": sub.get("corporate_actions") or [],
            }
            if sub.get("audit") is not None:
                # a dry-run catch-up persists nothing, so the audit travels here
                row["audit"] = sub["audit"]
            results.append(row)
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
        note = f"paper trading: signal #{signal_id}"
        # 出场动作标记：供风控冷却按 note 识别止损/止盈/持有到期（signal #id 解析不受影响）
        action = str(order.get("action") or "").lower()
        if action in ("stop_loss", "take_profit", "time_exit"):
            note += f" [{action}]"
        return note[:255]

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


def _bootstrap_paper_branches(service: "PaperTradingService") -> Optional[Dict[str, Any]]:
    """Backfill the ``default`` profile and the branch rows ahead of an auto-run.

    This is the idempotent bootstrap :meth:`PaperTradingService.run_all_accounts`
    already applies, hoisted to the trigger layer so the account set is settled
    before the single shared signal batch is built. Returns ``None`` when the
    repository cannot write at all — a fake repo in a unit test must never open
    a database.
    """

    repo = getattr(service.portfolio, "repo", None)
    if not callable(getattr(repo, "portfolio_write_session", None)):
        return None
    trunk_name = _env_text(PAPER_TRADING_ACCOUNT_NAME_ENV, DEFAULT_ACCOUNT_NAME).strip()
    names = sorted({name for name in (trunk_name, *DEFAULT_PAPER_ACCOUNT_NAMES) if name})
    return ensure_paper_branch_defaults(repo=repo, account_names=names)


def maybe_run_after_analysis(*, trigger: str = "analysis_hook") -> Optional[Dict[str, Any]]:
    """Auto-run every branch paper account over one shared signal batch.

    All four trigger points (the CLI/scheduler analysis hooks and the two
    independent paper-trading schedules) funnel through this single entry, so
    there is no separate one-account path left in the callers: the signals are
    read exactly once per trigger, each account then runs under its own policy
    profile, and one failing account never stops the rest. The batch key is
    idempotent, so a double trigger still cannot duplicate a fill.

    Disabled by default. Set ``PAPER_TRADING_AUTO_RUN=true`` to enable and
    ``PAPER_TRADING_AUTO_APPLY=false`` to keep the automatic run as a dry run —
    a dry run writes nothing at all, audit rows included.
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
        service = PaperTradingService()
        try:
            # The bootstrap writes rows, so a dry run must skip it entirely.
            bootstrap = None if dry_run else _bootstrap_paper_branches(service)
        except Exception as exc:  # noqa: BLE001 - run_all_accounts retries the bootstrap
            bootstrap = None
            logger.warning("Paper trading branch defaults failed (trigger=%s): %s", trigger, exc)
        result = service.run_all_accounts(dry_run=dry_run)
        accounts = list(result.get("accounts") or [])
        logger.info(
            "Paper trading auto-run (trigger=%s, dry_run=%s, batch=%s): %d 个账户 -> %s",
            trigger,
            dry_run,
            result.get("batch_key"),
            len(accounts),
            "; ".join(
                "{}={}（{} 单）".format(
                    row.get("account_name") or row.get("account_id") or "主干",
                    row.get("status"),
                    row.get("orders", 0),
                )
                for row in accounts
            )
            or "（无账户）",
        )
        if bootstrap and bootstrap.get("branch_created"):
            logger.info("Paper trading branch rows backfilled: %s", bootstrap["branch_created"])
        if result.get("warnings"):
            logger.warning("Paper trading auto-run warnings (trigger=%s): %s", trigger, result["warnings"])
        return result
    except Exception as exc:  # noqa: BLE001 - analysis must never fail because of paper trading
        logger.exception("Paper trading auto-run failed (trigger=%s): %s", trigger, exc)
        return None
