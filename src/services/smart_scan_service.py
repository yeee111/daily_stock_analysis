# -*- coding: utf-8 -*-
"""Smart scan — batch AI analysis + decision signal aggregation."""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.services.analysis_service import AnalysisService
from src.services.decision_signal_service import DecisionSignalService

logger = logging.getLogger(__name__)

_ACTION_RANK = {"buy": 0, "add": 1, "watch": 2, "hold": 3, "reduce": 4, "sell": 5, "avoid": 6, "alert": 7}
_DEFAULT_ACTION = "hold"
_SIGNAL_PAGE_SIZE = 100
_MAX_SIGNAL_PAGES = 20

_SCORE_BUCKETS = (
    ("strong_buy_gte80", 80),
    ("buy_60_79", 60),
    ("neutral_40_59", 40),
    ("caution_20_39", 20),
    ("avoid_lt20", 0),
)


class SmartScanService:
    """Orchestrate batch analysis and aggregate decision signals into a ranked buy list."""

    def run_scan(
        self,
        stock_codes: List[str],
        *,
        scan_id: str,
        report_type: str = "detailed",
        max_concurrent: int = 3,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        normalized_input = [str(code or "").strip() for code in stock_codes]
        unique_codes = _dedupe_stock_codes(normalized_input)
        total = len(unique_codes)
        completed = 0
        failed = 0

        def report(pct: int, msg: str) -> None:
            if progress_callback:
                progress_callback(max(0, min(100, pct)), msg)

        report(5, f"智能扫描已启动，共 {total} 只股票待分析")
        if not unique_codes:
            return _build_empty_result(scan_id, _order_results(normalized_input, {}))

        def analyze_one(code: str) -> Dict[str, Any]:
            service = AnalysisService()
            result = service.analyze_stock(
                stock_code=code,
                report_type=report_type,
                trace_id=scan_id,
                query_id=f"{scan_id}-{code}",
                send_notification=False,
                query_source="smart_scan",
            )
            if result is None:
                error = service.last_error or "分析返回空结果"
                return {"stock_code": code, "stock_name": None, "status": "failed", "error": error}
            summary = (result.get("report") or {}).get("summary") or {}
            return {
                "stock_code": code,
                "stock_name": result.get("stock_name") or "",
                "status": "success",
                "error": None,
                # 决策信号缺失时回退到分析摘要里的动作，保证买入榜不会把已分析股票一律标成持有。
                "action": summary.get("action"),
                "action_label": summary.get("action_label"),
            }

        results_by_code: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            future_to_code = {executor.submit(analyze_one, code): code for code in unique_codes}
            for future in as_completed(future_to_code):
                code = future_to_code[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.warning("Smart scan: stock %s crashed: %s", code, exc)
                    result = {"stock_code": code, "stock_name": None, "status": "failed", "error": str(exc)[:200]}
                results_by_code[code] = result

                completed += 1
                if result["status"] == "failed":
                    failed += 1
                pct = 10 + int(completed / total * 80)
                report(pct, f"已完成 {completed}/{total}，当前: {code}")

        report(92, "正在汇总决策信号…")
        stock_results = _order_results(normalized_input, results_by_code)
        ranked_list, aggregation = _aggregate_signals(scan_id, stock_results)
        report(100, f"扫描完成，{aggregation['succeeded']}/{total} 成功")

        now_iso = datetime.now(timezone.utc).isoformat()
        return {
            "scan_id": scan_id,
            "status": "completed",
            "progress": 100,
            "message": f"扫描完成，{aggregation['succeeded']}/{total} 成功",
            "error": None,
            "aggregation": aggregation,
            "ranked_list": ranked_list,
            "stock_results": stock_results,
            "created_at": now_iso,
            "completed_at": now_iso,
        }


def _dedupe_stock_codes(codes: List[str]) -> List[str]:
    seen = set()
    result = []
    for code in codes:
        normalized = str(code or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _order_results(
    normalized_input: List[str],
    results_by_code: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    emitted = set()
    for code in normalized_input:
        if not code:
            continue
        if code not in emitted and code in results_by_code:
            ordered.append(results_by_code[code])
            emitted.add(code)
            continue
        ordered.append({
            "stock_code": code,
            "stock_name": None,
            "status": "skipped",
            "error": "duplicate_input",
        })
    return ordered


def _aggregate_signals(
    scan_id: str,
    stock_results: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    succeeded = [r for r in stock_results if r.get("status") == "success"]
    failed_count = sum(1 for r in stock_results if r.get("status") == "failed")
    skipped_count = sum(1 for r in stock_results if r.get("status") == "skipped")

    signal_by_code = _signals_by_code_for_scan(scan_id)

    ranked_items: List[Dict[str, Any]] = []
    for item in succeeded:
        code = item["stock_code"]
        sig = signal_by_code.get(code)
        if sig is None:
            ranked_items.append({
                "stock_code": code,
                "stock_name": item.get("stock_name"),
                "action": str(item.get("action") or _DEFAULT_ACTION).lower(),
                "action_label": item.get("action_label"),
                "score": None,
                "confidence": None,
                "entry_low": None,
                "entry_high": None,
                "stop_loss": None,
                "target_price": None,
                "reason": "未生成决策信号",
                "risk_summary": None,
                "catalyst_summary": None,
                "signal_id": None,
                "horizon": None,
            })
            continue
        ranked_items.append({
            "stock_code": code,
            "stock_name": sig.get("stock_name") or item.get("stock_name"),
            "action": sig.get("action", _DEFAULT_ACTION),
            "action_label": sig.get("action_label"),
            "score": sig.get("score"),
            "confidence": sig.get("confidence"),
            "entry_low": sig.get("entry_low"),
            "entry_high": sig.get("entry_high"),
            "stop_loss": sig.get("stop_loss"),
            "target_price": sig.get("target_price"),
            "reason": sig.get("reason"),
            "risk_summary": sig.get("risk_summary"),
            "catalyst_summary": sig.get("catalyst_summary"),
            "signal_id": sig.get("id"),
            "horizon": sig.get("horizon"),
        })

    ranked_items.sort(key=_ranked_item_sort_key)
    for idx, item in enumerate(ranked_items, start=1):
        item["rank"] = idx

    scores = [item["score"] for item in ranked_items if item.get("score") is not None]
    action_breakdown: Dict[str, int] = {}
    for item in ranked_items:
        action = str(item.get("action") or "unknown").lower()
        action_breakdown[action] = action_breakdown.get(action, 0) + 1

    aggregation = {
        "total": len(stock_results),
        "succeeded": len(succeeded),
        "failed": failed_count,
        "skipped": skipped_count,
        "action_breakdown": action_breakdown,
        "score_distribution": _score_distribution(scores),
        "avg_score": round(statistics.mean(scores), 1) if scores else None,
        "median_score": round(statistics.median(scores), 1) if scores else None,
    }
    return ranked_items, aggregation


def _signals_by_code_for_scan(scan_id: str) -> Dict[str, Dict[str, Any]]:
    """Return the most actionable signal per stock for this scan.

    A single analysis can persist several signals (different horizons), and
    ``list_signals`` caps ``page_size`` at 100, so page until ``total`` is
    covered instead of silently dropping the tail of a large scan.
    """
    service = DecisionSignalService()
    signals: List[Dict[str, Any]] = []
    for page in range(1, _MAX_SIGNAL_PAGES + 1):
        response = service.list_signals(trace_id=scan_id, page=page, page_size=_SIGNAL_PAGE_SIZE)
        batch = list(response.get("items") or [])
        signals.extend(batch)
        total = int(response.get("total") or 0)
        if not batch or len(batch) < _SIGNAL_PAGE_SIZE or len(signals) >= total:
            break
    else:
        logger.warning("Smart scan: signal paging stopped at %s pages for trace %s", _MAX_SIGNAL_PAGES, scan_id)

    signal_by_code: Dict[str, Dict[str, Any]] = {}
    for sig in signals:
        code = str(sig.get("stock_code") or "").strip()
        if not code:
            continue
        existing = signal_by_code.get(code)
        if existing is None or _signal_priority(sig) < _signal_priority(existing):
            signal_by_code[code] = sig
    return signal_by_code


def _signal_priority(sig: Dict[str, Any]) -> Tuple[int, float]:
    action = str(sig.get("action") or "").lower()
    score = sig.get("score")
    return (_ACTION_RANK.get(action, 99), -score if isinstance(score, (int, float)) else 0)


def _ranked_item_sort_key(item: Dict[str, Any]) -> Tuple[int, float]:
    action = str(item.get("action") or "").lower()
    score = item.get("score")
    return (_ACTION_RANK.get(action, 99), -score if isinstance(score, (int, float)) else 0)


def _score_distribution(scores: List[Any]) -> Dict[str, int]:
    distribution = {bucket: 0 for bucket, _ in _SCORE_BUCKETS}
    for score in scores:
        for bucket, floor in _SCORE_BUCKETS:
            if score >= floor:
                distribution[bucket] += 1
                break
    return distribution


def _build_empty_result(scan_id: str, stock_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "scan_id": scan_id,
        "status": "completed",
        "progress": 100,
        "message": "无有效股票可分析",
        "error": None,
        "aggregation": {
            "total": len(stock_results),
            "succeeded": 0,
            "failed": 0,
            "skipped": len(stock_results),
            "action_breakdown": {},
            "score_distribution": _score_distribution([]),
            "avg_score": None,
            "median_score": None,
        },
        "ranked_list": [],
        "stock_results": stock_results,
        "created_at": now_iso,
        "completed_at": now_iso,
    }
