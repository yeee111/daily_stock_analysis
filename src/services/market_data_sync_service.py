# -*- coding: utf-8 -*-
"""Best-effort daily bar syncing for benchmark indexes and held symbols.

The portfolio analysis pipeline only persists bars for its watchlist, so the
performance dashboard had no index bars to compare against, and held names can
lag until the scheduled analysis runs. Both gaps are filled here: index bars
come from the Tencent/Eastmoney kline endpoints (the shared fetchers treat bare
index codes as stocks and fail), while stock bars reuse the existing DB-first
history loader with provider fallback.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

EASTMONEY_KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HTTP_TIMEOUT_SECONDS = 10.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_INDEX_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59"
_DEFAULT_INDEX_DAYS = 250
_MISSING_BAR_FETCH_DAYS = 60


def normalize_index_symbol(symbol: str) -> str:
    return str(symbol or "").strip().lower().replace(".csi", "").replace("_", "")


def index_secid(symbol: str) -> Optional[str]:
    """Map an index symbol to its Eastmoney ``secid`` (market.code)."""
    code = normalize_index_symbol(symbol)
    if not code:
        return None
    if code.startswith(("sh", "sse")) and code[2:].isdigit():
        return f"1.{code[2:]}"
    if code.startswith(("sz", "szi")) and code[2:].isdigit():
        return f"0.{code[2:]}"
    if code.startswith("399"):
        return f"0.{code}"
    if code.isdigit():
        return f"1.{code}"
    return None


def index_canonical_id(symbol: str) -> Optional[str]:
    """Explicit index canonical id, so bare index codes are not stored as stocks."""
    secid = index_secid(symbol)
    if not secid:
        return None
    market, _, code = secid.partition(".")
    return f"{'sh' if market == '1' else 'sz'}{code}"


def tencent_symbol_for_secid(secid: str) -> Optional[str]:
    """Map an Eastmoney ``secid`` to the Tencent ``sh/sz + code`` form."""
    market, _, code = str(secid or "").partition(".")
    if market not in ("1", "0") or not code:
        return None
    return f"{'sh' if market == '1' else 'sz'}{code}"


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class MarketDataSyncService:
    """Sync daily bars into ``stock_daily``; every failure degrades to a warning."""

    def __init__(
        self,
        *,
        db: Any = None,
        fetch: Optional[Any] = None,
    ) -> None:
        self._db = db
        self._fetch = fetch or self._fetch_index_klines

    @property
    def db(self) -> Any:
        if self._db is None:
            from src.storage import get_db

            self._db = get_db()
        return self._db

    def sync_index_bars(
        self,
        symbol: str,
        *,
        as_of: Optional[date] = None,
        days: int = _DEFAULT_INDEX_DAYS,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "symbol": symbol,
            "synced": 0,
            "latest_date": None,
            "error": None,
        }
        secid = index_secid(symbol)
        if not secid:
            result["error"] = f"无法识别指数代码 {symbol}"
            return result
        end = as_of or date.today()
        start = end - timedelta(days=int(days * 1.6) + 15)
        try:
            rows = self._fetch(secid=secid, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - syncing must never break reporting
            logger.warning("Benchmark index fetch failed for %s: %s", symbol, exc)
            result["error"] = str(exc)
            return result
        if not rows:
            result["error"] = f"{symbol} 未返回日线数据"
            return result

        import pandas as pd

        frame = pd.DataFrame(rows)
        index_key = index_canonical_id(symbol)
        saved = self.db.save_daily_data(
            frame,
            code=index_key or str(symbol).strip(),
            data_source="IndexKline",
            canonical_id=index_key,
        )
        result["synced"] = int(saved or 0)
        result["latest_date"] = frame["date"].max().isoformat()
        return result

    def sync_symbol_bars(
        self,
        codes: Iterable[str],
        *,
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Backfill recent bars for the given stock codes via the shared history loader."""
        end = as_of or date.today()
        synced: List[str] = []
        failed: List[str] = []
        for raw_code in codes:
            code = str(raw_code or "").strip()
            if not code:
                continue
            try:
                from src.services.history_loader import load_history_df

                frame, source = load_history_df(code, days=_MISSING_BAR_FETCH_DAYS, target_date=end)
                if frame is None or frame.empty:
                    failed.append(f"{code}（{source}）")
                    continue
                self.db.save_daily_data(frame, code, source or "Unknown")
                synced.append(code)
            except Exception as exc:  # noqa: BLE001 - best effort per code
                logger.warning("Daily bar sync failed for %s: %s", code, exc)
                failed.append(f"{code}（{exc}）")
        return {"synced": synced, "failed": failed, "as_of": end.isoformat()}

    @classmethod
    def _fetch_index_klines(cls, *, secid: str, start: date, end: date) -> List[Dict[str, Any]]:
        """Try Tencent then Eastmoney; both serve index bars, and either can be unreachable."""
        errors: List[str] = []
        for name, fetcher in (
            ("Tencent", cls._fetch_tencent_index_klines),
            ("Eastmoney", cls._fetch_eastmoney_klines),
        ):
            try:
                rows = fetcher(secid=secid, start=start, end=end)
            except Exception as exc:  # noqa: BLE001 - next source gets a chance
                errors.append(f"{name}: {exc}")
                continue
            if rows:
                return rows
            errors.append(f"{name}: 返回空数据")
        raise RuntimeError("指数日线拉取失败（" + "；".join(errors) + "）")

    @staticmethod
    def _fetch_tencent_index_klines(*, secid: str, start: date, end: date) -> List[Dict[str, Any]]:
        symbol = tencent_symbol_for_secid(secid)
        if not symbol:
            return []
        params = {
            "param": (
                f"{symbol},day,{start:%Y-%m-%d},{end:%Y-%m-%d},"
                f"{max(1, (end - start).days + 5)},qfq"
            )
        }
        response = requests.get(
            TENCENT_KLINE_ENDPOINT,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = ((response.json() or {}).get("data") or {}).get(symbol) or {}
        bars = payload.get("qfqday") or payload.get("day") or []
        rows: List[Dict[str, Any]] = []
        previous_close: Optional[float] = None
        for bar in bars:
            if not isinstance(bar, (list, tuple)) or len(bar) < 6:
                continue
            bar_date = date.fromisoformat(str(bar[0])[:10])
            open_price = _to_float(bar[1])
            close_price = _to_float(bar[2])
            pct_chg = None
            if previous_close:
                pct_chg = (close_price - previous_close) / previous_close * 100.0
            previous_close = close_price
            rows.append(
                {
                    "date": bar_date,
                    "open": open_price,
                    "high": _to_float(bar[3]),
                    "low": _to_float(bar[4]),
                    "close": close_price,
                    "volume": _to_float(bar[5]),
                    "pct_chg": pct_chg,
                }
            )
        return rows

    @staticmethod
    def _fetch_eastmoney_klines(*, secid: str, start: date, end: date) -> List[Dict[str, Any]]:
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": _INDEX_FIELDS,
            "secid": secid,
            "klt": "101",
            "fqt": "1",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "rtntype": "6",
        }
        response = requests.get(
            EASTMONEY_KLINE_ENDPOINT,
            params=params,
            headers={"User-Agent": _USER_AGENT, "Referer": "https://quote.eastmoney.com/"},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        klines = ((payload or {}).get("data") or {}).get("klines") or []
        rows: List[Dict[str, Any]] = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 9:
                continue
            bar_date = date.fromisoformat(parts[0])
            open_price = _to_float(parts[1])
            close_price = _to_float(parts[2])
            rows.append(
                {
                    "date": bar_date,
                    "open": open_price,
                    "high": _to_float(parts[3]),
                    "low": _to_float(parts[4]),
                    "close": close_price,
                    "volume": _to_float(parts[5]),
                    "amount": _to_float(parts[6]),
                    "pct_chg": _to_float(parts[8]),
                }
            )
        return rows
