# -*- coding: utf-8 -*-
"""Bridge screening candidates into persisted DecisionSignal records.

Screening is a daily discovery pass: the top ``buy_top_n`` candidates become
``buy`` signals (paper trading may act on them), the rest become ``watch``
signals so they stay visible without opening positions.

Price plans use the daily features already computed by the screening engine
(MA20 distance, 20-day breakout, ATR) instead of a fixed percentage ladder.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any, Dict, Iterable, Mapping, Optional

from data_provider.base import normalize_stock_code
from src.core.trading_calendar import get_market_for_stock
from src.services.decision_signal_service import DecisionSignalService
from src.services.portfolio_service import VALID_MARKETS


logger = logging.getLogger(__name__)

DEFAULT_SCREENING_HORIZON = "3d"
SUPPORTED_SCREENING_HORIZONS = frozenset({"3d", "5d"})
SIGNAL_GENERATION_VERSION = "screening-bridge-v1"


def build_screening_signal_payload(
    candidate: Mapping[str, Any],
    *,
    rank: int,
    buy_top_n: int,
    horizon: str = DEFAULT_SCREENING_HORIZON,
    run_id: str = "",
    strategy: str = "",
    strategy_version: str = "",
    ranking_mode: str = "",
    llm_ranked: Optional[bool] = None,
    selection_logic: str = "",
    market_override: Optional[str] = None,
    trigger_source: str = "screening",
    signal_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build one DecisionSignal payload from a normalized screening candidate."""

    if horizon not in SUPPORTED_SCREENING_HORIZONS:
        raise ValueError(f"unsupported screening signal horizon: {horizon}")

    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else candidate
    code = str(
        candidate.get("code")
        or candidate.get("stock_code")
        or candidate.get("symbol")
        or raw.get("code")
        or ""
    ).strip()
    if not code:
        return None

    market = market_override or get_market_for_stock(normalize_stock_code(code))
    if not market:
        logger.warning("Skip screening signal: unrecognized market stock_code=%s", code)
        return None
    if market not in VALID_MARKETS:
        logger.info(
            "Skip screening signal: market=%s not wired for signals stock_code=%s",
            market,
            code,
        )
        return None

    price = _finite_positive(candidate.get("price"))
    if price is None:
        price = _finite_positive(raw.get("price"))
    plan = _dynamic_price_plan(price, raw) if price is not None else _mechanical_plan(None)

    score = _score_int(candidate.get("score"))
    confidence = _confidence(candidate.get("llm_confidence"))
    action = "buy" if rank <= max(0, int(buy_top_n)) else "watch"

    reason = _reason_text(candidate)
    risk_summary = _risk_text(candidate)
    invalidation, watch_conditions = _lifecycle_texts(action, plan)

    payload: Dict[str, Any] = {
        "stock_code": code,
        "stock_name": candidate.get("name") or raw.get("name"),
        "market": market,
        "source_type": "screening",
        "trace_id": _trace_id(code, signal_date),
        "trigger_source": trigger_source,
        "decision_profile": "balanced",
        "action": action,
        "confidence": confidence,
        "score": score,
        "horizon": horizon,
        "entry_low": plan["entry_low"],
        "entry_high": plan["entry_high"],
        "stop_loss": plan["stop_loss"],
        "target_price": plan["target_price"],
        "reason": reason,
        "risk_summary": risk_summary,
        "invalidation": invalidation,
        "watch_conditions": watch_conditions,
        "plan_quality": plan["plan_quality"],
        "evidence": {
            "screening": {
                "rank": rank,
                "score": _finite(candidate.get("score")),
                "screen_score": _finite(candidate.get("screen_score")),
                "factor_scores": candidate.get("factor_scores") or {},
                "industry": candidate.get("industry") or raw.get("industry") or "",
            }
        },
        "data_quality_summary": {
            "plan_source": plan["plan_source"],
            "price": price,
            "atr_20_pct": plan.get("atr_20_pct"),
            "pullback_to_ma20_pct": plan.get("pullback_to_ma20_pct"),
            "breakout_20d_pct": plan.get("breakout_20d_pct"),
        },
        "metadata": _metadata(
            candidate,
            rank=rank,
            buy_top_n=buy_top_n,
            run_id=run_id,
            strategy=strategy,
            strategy_version=strategy_version,
            ranking_mode=ranking_mode,
            llm_ranked=llm_ranked,
            selection_logic=selection_logic,
            horizon=horizon,
            plan=plan,
        ),
    }
    return payload


def persist_screening_signals(
    response: Mapping[str, Any],
    *,
    service: Optional[DecisionSignalService] = None,
    buy_top_n: int = 10,
    horizon: str = DEFAULT_SCREENING_HORIZON,
    trigger_source: str = "screening",
) -> Dict[str, Any]:
    """Write screening candidates through the canonical DecisionSignal path.

    Failures are isolated per candidate so one bad row cannot drop the batch.
    Returns a summary suitable for logging and diagnostics.
    """

    if horizon not in SUPPORTED_SCREENING_HORIZONS:
        raise ValueError(f"unsupported screening signal horizon: {horizon}")

    candidates: Iterable[Any] = response.get("candidates") or []
    writer = service or DecisionSignalService()
    market_override = _response_market(response)
    run_id = str(response.get("run_id") or "")
    strategy = str(response.get("strategy") or "")
    strategy_version = str(response.get("strategy_version") or "")
    ranking_mode = str(response.get("ranking_mode") or "")
    llm_ranked = response.get("llm_ranked")
    llm_ranked = bool(llm_ranked) if isinstance(llm_ranked, bool) else None
    selection_logic = str(response.get("llm_selection_logic") or "")
    signal_date = date.today().isoformat()

    summary: Dict[str, Any] = {
        "candidates": 0,
        "created": 0,
        "refreshed": 0,
        "duplicate": 0,
        "skipped": 0,
        "errors": [],
    }

    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, Mapping):
            summary["skipped"] += 1
            continue
        summary["candidates"] += 1
        rank = _as_int(candidate.get("rank")) or index
        raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
        code = str(candidate.get("code") or raw.get("code") or "")
        try:
            payload = build_screening_signal_payload(
                candidate,
                rank=rank,
                buy_top_n=buy_top_n,
                horizon=horizon,
                run_id=run_id,
                strategy=strategy,
                strategy_version=strategy_version,
                ranking_mode=ranking_mode,
                llm_ranked=llm_ranked,
                selection_logic=selection_logic,
                market_override=market_override,
                trigger_source=trigger_source,
                signal_date=signal_date,
            )
            if payload is None:
                summary["skipped"] += 1
                continue
            outcome = writer.create_signal_with_outcome(payload)
            disposition = outcome.disposition
            if disposition == "created":
                summary["created"] += 1
            elif disposition == "refreshed":
                summary["refreshed"] += 1
            else:
                summary["duplicate"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate per-candidate failures
            error = {"code": code or f"rank{rank}", "error": f"{type(exc).__name__}: {exc}"}
            summary["errors"].append(error)
            logger.warning("Screening signal write failed code=%s: %s", error["code"], error["error"])

    logger.info(
        "Screening signal bridge: candidates=%s created=%s refreshed=%s duplicate=%s skipped=%s errors=%s",
        summary["candidates"],
        summary["created"],
        summary["refreshed"],
        summary["duplicate"],
        summary["skipped"],
        len(summary["errors"]),
    )
    return summary


def _dynamic_price_plan(price: Optional[float], raw: Mapping[str, Any]) -> Dict[str, Any]:
    atr_pct = _finite_positive(raw.get("atr_20_pct"))
    pullback_pct = _finite(raw.get("pullback_to_ma20_pct"))
    breakout_pct = _finite(raw.get("breakout_20d_pct"))
    above_ma20 = raw.get("price_above_ma20")

    if price is None or price <= 0 or atr_pct is None:
        return _mechanical_plan(price)

    atr = price * atr_pct / 100.0
    ma20 = _ma20_from_pullback(price, pullback_pct)
    prev_high = _prev_high_from_breakout(price, breakout_pct)

    if above_ma20 is True and ma20 is not None and 0 < ma20 < price:
        support = ma20
    else:
        support = price - atr
    if support <= 0 or support >= price:
        support = price - atr
    if support <= 0:
        return _mechanical_plan(price)

    stop = support - 0.5 * atr
    if stop <= 0:
        stop = price * 0.9

    if prev_high is not None and prev_high > price:
        target = prev_high
        target_source = "prev_high_20d"
    else:
        target = price + 1.5 * atr
        target_source = "atr_multiple"

    entry_low, entry_high, stop, target = _round_plan(support, price, stop, target)
    if not (0 < stop < entry_low <= entry_high < target):
        return _mechanical_plan(price)

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop,
        "target_price": target,
        "plan_quality": (
            "complete"
            if ma20 is not None and prev_high is not None
            else "partial"
        ),
        "plan_source": "dynamic_support_resistance",
        "target_source": target_source,
        "atr_20_pct": atr_pct,
        "pullback_to_ma20_pct": pullback_pct,
        "breakout_20d_pct": breakout_pct,
    }


def _mechanical_plan(price: Optional[float]) -> Dict[str, Any]:
    if price is None or price <= 0:
        return {
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "target_price": None,
            "plan_quality": "unknown",
            "plan_source": "missing_price",
            "target_source": None,
            "atr_20_pct": None,
            "pullback_to_ma20_pct": None,
            "breakout_20d_pct": None,
        }
    entry_low, entry_high, stop, target = _round_plan(
        price * 0.99,
        price,
        price * 0.95,
        price * 1.10,
    )
    if not (0 < stop < entry_low <= entry_high < target):
        return {
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "target_price": None,
            "plan_quality": "unknown",
            "plan_source": "missing_price",
            "target_source": None,
            "atr_20_pct": None,
            "pullback_to_ma20_pct": None,
            "breakout_20d_pct": None,
        }
    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop,
        "target_price": target,
        "plan_quality": "minimal",
        "plan_source": "mechanical_fallback",
        "target_source": "fixed_pct",
        "atr_20_pct": None,
        "pullback_to_ma20_pct": None,
        "breakout_20d_pct": None,
    }


def _round_plan(
    entry_low: float,
    entry_high: float,
    stop: float,
    target: float,
) -> tuple[float, float, float, float]:
    return (
        round(entry_low, 2),
        round(entry_high, 2),
        round(stop, 2),
        round(target, 2),
    )


def _ma20_from_pullback(price: float, pullback_pct: Optional[float]) -> Optional[float]:
    if pullback_pct is None:
        return None
    denominator = 1 + pullback_pct / 100.0
    if denominator <= 0:
        return None
    return price / denominator


def _prev_high_from_breakout(price: float, breakout_pct: Optional[float]) -> Optional[float]:
    if breakout_pct is None:
        return None
    denominator = 1 + breakout_pct / 100.0
    if denominator <= 0:
        return None
    return price / denominator


def _metadata(
    candidate: Mapping[str, Any],
    *,
    rank: int,
    buy_top_n: int,
    run_id: str,
    strategy: str,
    strategy_version: str,
    ranking_mode: str,
    llm_ranked: Optional[bool],
    selection_logic: str,
    horizon: str,
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else candidate
    metadata: Dict[str, Any] = {
        "report_type": "screening",
        "signal_generation_version": SIGNAL_GENERATION_VERSION,
        "screening_run_id": run_id,
        "screening_strategy": strategy,
        "screening_strategy_version": strategy_version,
        "screening_rank": rank,
        "screening_buy_top_n": buy_top_n,
        "screening_ranking_mode": ranking_mode,
        "screening_llm_ranked": llm_ranked,
        "screening_reason_source": candidate.get("reason_source") or "",
        "screening_reason_quality": candidate.get("reason_quality") or "",
        "screening_industry": candidate.get("industry") or raw.get("industry") or "",
        "screening_price": _finite(candidate.get("price")) or _finite(raw.get("price")),
        "screening_change_pct": _finite(candidate.get("change_pct")) or _finite(raw.get("change_pct")),
        "screening_llm_tags": _text_list(candidate.get("llm_tags")),
        "screening_llm_catalysts": _text_list(candidate.get("llm_catalysts")),
        "screening_llm_risks": _text_list(candidate.get("llm_risks")),
        "plan_source": plan.get("plan_source"),
        "plan_target_source": plan.get("target_source"),
        "atr_20_pct": plan.get("atr_20_pct"),
        "pullback_to_ma20_pct": plan.get("pullback_to_ma20_pct"),
        "breakout_20d_pct": plan.get("breakout_20d_pct"),
        "horizon": horizon,
    }
    if selection_logic:
        metadata["screening_selection_logic"] = _truncate(selection_logic, 600)
    return metadata


def _reason_text(candidate: Mapping[str, Any]) -> str:
    parts = []
    for key in ("reason", "ranking_reason"):
        text = str(candidate.get(key) or "").strip()
        if text:
            parts.append(text)
            break
    thesis = str(candidate.get("llm_thesis") or "").strip()
    if thesis:
        parts.append(f"逻辑：{thesis}")
    catalysts = _text_list(candidate.get("llm_catalysts"))
    if catalysts:
        parts.append(f"催化：{'；'.join(catalysts[:3])}")
    return _truncate(" ".join(parts), 800)


def _risk_text(candidate: Mapping[str, Any]) -> str:
    summary = str(candidate.get("risk_summary") or "").strip()
    if summary:
        return _truncate(summary, 400)
    flags = _text_list(candidate.get("risk_flags"))
    level = str(candidate.get("risk_level") or "").strip()
    parts = []
    if level:
        parts.append(f"风险等级：{level}")
    if flags:
        parts.append("风险：" + "；".join(flags))
    return _truncate(" ".join(parts), 400)


def _lifecycle_texts(action: str, plan: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    stop = plan.get("stop_loss")
    target = plan.get("target_price")
    support = plan.get("entry_low")
    if action == "buy":
        invalidation = (
            f"跌破止损位 {stop} 或有效跌破支撑 {support}"
            if stop and support
            else "跌破止损位"
        )
        watch = f"接近目标位 {target} 时评估止盈" if target else None
        return invalidation, watch
    watch_conditions = (
        f"回踩支撑 {support} 企稳或放量突破 {target} 后再评估买入"
        if support and target
        else "回踩支撑企稳后再评估买入"
    )
    return None, watch_conditions


def _response_market(response: Mapping[str, Any]) -> Optional[str]:
    market = str(response.get("market") or "").strip().lower()
    return market if market in VALID_MARKETS else None


def _trace_id(code: str, signal_date: Optional[str]) -> str:
    day = signal_date or date.today().isoformat()
    return f"screening-{day}-{normalize_stock_code(code)}"[:64]


def _score_int(value: Any) -> Optional[int]:
    number = _finite(value)
    if number is None:
        return None
    return max(0, min(100, int(round(number))))


def _confidence(value: Any) -> Optional[float]:
    number = _finite(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _finite(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_positive(value: Any) -> Optional[float]:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _as_int(value: Any) -> Optional[int]:
    number = _finite(value)
    return int(number) if number is not None else None


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _truncate(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
