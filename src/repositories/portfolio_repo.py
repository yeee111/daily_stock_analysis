# -*- coding: utf-8 -*-
"""Portfolio repository.

Provides DB access helpers for portfolio account/events/snapshot tables.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from src.storage import (
    DatabaseManager,
    PaperAccountBranch,
    PaperAccountRun,
    PaperStrategyProfile,
    PortfolioAccount,
    PortfolioCashLedger,
    PortfolioCorporateAction,
    PortfolioDailySnapshot,
    PortfolioFxRate,
    PortfolioPosition,
    PortfolioPositionLot,
    PortfolioTrade,
    StockDaily,
    empty_paper_policy_config,
)

logger = logging.getLogger(__name__)

# Multi-account skeleton (Phase 1): the shipped default policy profile keeps
# every section empty, which means "fall back to the current env/default path".
# Live account parameters must never be baked into this profile.
DEFAULT_PAPER_PROFILE_NAME = "default"
DEFAULT_PAPER_PROFILE_VERSION = 1
DEFAULT_PAPER_ACCOUNT_NAMES = ("纸面交易",)

# Branch roles store only the two governance states; the four display states
# are ``live`` / ``control`` / ``shadow`` (control + auto_run) / ``retired``
# (control + not auto_run).
PAPER_BRANCH_ROLES = ("live", "control")


def paper_branch_state(role: Optional[str], auto_run: bool) -> str:
    """Display state derived from the stored role and ``auto_run``."""
    if role == "live":
        return "live"
    return "shadow" if auto_run else "retired"


def paper_branch_role(role: Optional[str], parent_account_id: Optional[int]) -> str:
    """Effective role; legacy/garbage values fall back to trunk/fork defaults."""
    if role in PAPER_BRANCH_ROLES:
        return role
    return "live" if parent_account_id is None else "control"


class DuplicateTradeUidError(Exception):
    """Raised when trade_uid conflicts with existing record in one account."""


class DuplicateTradeDedupHashError(Exception):
    """Raised when dedup hash conflicts with existing record in one account."""


class PortfolioBusyError(Exception):
    """Raised when SQLite write serialization cannot acquire the ledger lock."""


class PortfolioRepository:
    """DB access layer for portfolio P0 domain."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------
    def create_account(
        self,
        *,
        name: str,
        broker: Optional[str],
        market: str,
        base_currency: str,
        owner_id: Optional[str] = None,
    ) -> PortfolioAccount:
        with self.db.get_session() as session:
            row = PortfolioAccount(
                owner_id=owner_id,
                name=name,
                broker=broker,
                market=market,
                base_currency=base_currency,
                is_active=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_account(self, account_id: int, include_inactive: bool = False) -> Optional[PortfolioAccount]:
        with self.db.get_session() as session:
            return self.get_account_in_session(
                session=session,
                account_id=account_id,
                include_inactive=include_inactive,
            )

    def list_accounts(self, include_inactive: bool = False) -> List[PortfolioAccount]:
        with self.db.get_session() as session:
            query = select(PortfolioAccount)
            if not include_inactive:
                query = query.where(PortfolioAccount.is_active.is_(True))
            rows = session.execute(query.order_by(PortfolioAccount.id.asc())).scalars().all()
            return list(rows)

    def get_account_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        include_inactive: bool = False,
    ) -> Optional[PortfolioAccount]:
        conditions = [PortfolioAccount.id == account_id]
        if not include_inactive:
            conditions.append(PortfolioAccount.is_active.is_(True))
        return session.execute(
            select(PortfolioAccount).where(and_(*conditions)).limit(1)
        ).scalar_one_or_none()

    def update_account(self, account_id: int, fields: Dict[str, Any]) -> Optional[PortfolioAccount]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioAccount).where(PortfolioAccount.id == account_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            return row

    def deactivate_account(self, account_id: int) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioAccount).where(PortfolioAccount.id == account_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.is_active = False
            row.updated_at = datetime.now()
            session.commit()
            return True

    # ------------------------------------------------------------------
    # Event writes
    # ------------------------------------------------------------------
    @contextmanager
    def portfolio_write_session(self):
        session = self.db.get_session()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as exc:
            session.close()
            if self._is_sqlite_locked_error(exc):
                raise PortfolioBusyError("Portfolio ledger is busy; please retry shortly.") from exc
            raise

        try:
            yield session
            session.commit()
        except OperationalError as exc:
            session.rollback()
            if self._is_sqlite_locked_error(exc):
                raise PortfolioBusyError("Portfolio ledger is busy; please retry shortly.") from exc
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def add_trade(
        self,
        *,
        account_id: int,
        trade_uid: Optional[str],
        symbol: str,
        market: str,
        currency: str,
        trade_date: date,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        note: Optional[str] = None,
        dedup_hash: Optional[str] = None,
    ) -> PortfolioTrade:
        with self.portfolio_write_session() as session:
            row = self.add_trade_in_session(
                session=session,
                account_id=account_id,
                trade_uid=trade_uid,
                symbol=symbol,
                market=market,
                currency=currency,
                trade_date=trade_date,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                tax=tax,
                note=note,
                dedup_hash=dedup_hash,
            )
            session.expunge(row)
            return row

    def add_cash_ledger(
        self,
        *,
        account_id: int,
        event_date: date,
        direction: str,
        amount: float,
        currency: str,
        note: Optional[str] = None,
    ) -> PortfolioCashLedger:
        with self.portfolio_write_session() as session:
            row = self.add_cash_ledger_in_session(
                session=session,
                account_id=account_id,
                event_date=event_date,
                direction=direction,
                amount=amount,
                currency=currency,
                note=note,
            )
            session.expunge(row)
            return row

    def add_corporate_action(
        self,
        *,
        account_id: int,
        symbol: str,
        market: str,
        currency: str,
        effective_date: date,
        action_type: str,
        cash_dividend_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
        note: Optional[str] = None,
    ) -> PortfolioCorporateAction:
        with self.portfolio_write_session() as session:
            row = self.add_corporate_action_in_session(
                session=session,
                account_id=account_id,
                symbol=symbol,
                market=market,
                currency=currency,
                effective_date=effective_date,
                action_type=action_type,
                cash_dividend_per_share=cash_dividend_per_share,
                split_ratio=split_ratio,
                note=note,
            )
            session.expunge(row)
            return row

    def delete_trade(self, trade_id: int) -> bool:
        with self.portfolio_write_session() as session:
            return self.delete_trade_in_session(session=session, trade_id=trade_id)

    def delete_cash_ledger(self, entry_id: int) -> bool:
        with self.portfolio_write_session() as session:
            return self.delete_cash_ledger_in_session(session=session, entry_id=entry_id)

    def delete_corporate_action(self, action_id: int) -> bool:
        with self.portfolio_write_session() as session:
            return self.delete_corporate_action_in_session(session=session, action_id=action_id)

    def has_trade_uid(self, account_id: int, trade_uid: Optional[str]) -> bool:
        """Return True when trade_uid already exists in the account."""
        uid = (trade_uid or "").strip()
        if not uid:
            return False
        with self.db.get_session() as session:
            return self.has_trade_uid_in_session(session=session, account_id=account_id, trade_uid=uid)

    def has_trade_dedup_hash(self, account_id: int, dedup_hash: Optional[str]) -> bool:
        """Return True when dedup hash already exists in the account."""
        hash_value = (dedup_hash or "").strip()
        if not hash_value:
            return False
        with self.db.get_session() as session:
            return self.has_trade_dedup_hash_in_session(
                session=session,
                account_id=account_id,
                dedup_hash=hash_value,
            )

    def has_trade_uid_in_session(self, *, session: Any, account_id: int, trade_uid: str) -> bool:
        row = session.execute(
            select(PortfolioTrade.id).where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.trade_uid == trade_uid,
                )
            ).limit(1)
        ).scalar_one_or_none()
        return row is not None

    def has_trade_dedup_hash_in_session(self, *, session: Any, account_id: int, dedup_hash: str) -> bool:
        row = session.execute(
            select(PortfolioTrade.id).where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.dedup_hash == dedup_hash,
                )
            ).limit(1)
        ).scalar_one_or_none()
        return row is not None

    def add_trade_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        trade_uid: Optional[str],
        symbol: str,
        market: str,
        currency: str,
        trade_date: date,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        tax: float,
        note: Optional[str] = None,
        dedup_hash: Optional[str] = None,
    ) -> PortfolioTrade:
        row = PortfolioTrade(
            account_id=account_id,
            trade_uid=trade_uid,
            symbol=symbol,
            market=market,
            currency=currency,
            trade_date=trade_date,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            note=note,
            dedup_hash=dedup_hash,
        )
        session.add(row)
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=account_id,
            from_date=trade_date,
        )
        try:
            session.flush()
        except IntegrityError as exc:
            raise self._translate_trade_integrity_error(
                exc=exc,
                account_id=account_id,
                trade_uid=trade_uid,
                dedup_hash=dedup_hash,
            ) from exc
        session.refresh(row)
        return row

    def add_cash_ledger_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        event_date: date,
        direction: str,
        amount: float,
        currency: str,
        note: Optional[str] = None,
    ) -> PortfolioCashLedger:
        row = PortfolioCashLedger(
            account_id=account_id,
            event_date=event_date,
            direction=direction,
            amount=amount,
            currency=currency,
            note=note,
        )
        session.add(row)
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=account_id,
            from_date=event_date,
        )
        session.flush()
        session.refresh(row)
        return row

    def add_corporate_action_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        symbol: str,
        market: str,
        currency: str,
        effective_date: date,
        action_type: str,
        cash_dividend_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
        note: Optional[str] = None,
    ) -> PortfolioCorporateAction:
        row = PortfolioCorporateAction(
            account_id=account_id,
            symbol=symbol,
            market=market,
            currency=currency,
            effective_date=effective_date,
            action_type=action_type,
            cash_dividend_per_share=cash_dividend_per_share,
            split_ratio=split_ratio,
            note=note,
        )
        session.add(row)
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=account_id,
            from_date=effective_date,
        )
        session.flush()
        session.refresh(row)
        return row

    def delete_trade_in_session(self, *, session: Any, trade_id: int) -> bool:
        row = session.execute(
            select(PortfolioTrade).where(PortfolioTrade.id == trade_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=int(row.account_id),
            from_date=row.trade_date,
        )
        session.delete(row)
        session.flush()
        return True

    def delete_cash_ledger_in_session(self, *, session: Any, entry_id: int) -> bool:
        row = session.execute(
            select(PortfolioCashLedger).where(PortfolioCashLedger.id == entry_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=int(row.account_id),
            from_date=row.event_date,
        )
        session.delete(row)
        session.flush()
        return True

    def delete_corporate_action_in_session(self, *, session: Any, action_id: int) -> bool:
        row = session.execute(
            select(PortfolioCorporateAction).where(PortfolioCorporateAction.id == action_id).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return False
        self._invalidate_account_cache_in_session(
            session=session,
            account_id=int(row.account_id),
            from_date=row.effective_date,
        )
        session.delete(row)
        session.flush()
        return True

    # ------------------------------------------------------------------
    # Event reads
    # ------------------------------------------------------------------
    def list_trades(self, account_id: int, as_of: date) -> List[PortfolioTrade]:
        with self.db.get_session() as session:
            return self.list_trades_in_session(session=session, account_id=account_id, as_of=as_of)

    def list_trades_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> List[PortfolioTrade]:
        rows = session.execute(
            select(PortfolioTrade)
            .where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.trade_date <= as_of,
                )
            )
            .order_by(PortfolioTrade.trade_date.asc(), PortfolioTrade.id.asc())
        ).scalars().all()
        return list(rows)

    def list_cash_ledger(self, account_id: int, as_of: date) -> List[PortfolioCashLedger]:
        with self.db.get_session() as session:
            return self.list_cash_ledger_in_session(session=session, account_id=account_id, as_of=as_of)

    def list_cash_ledger_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> List[PortfolioCashLedger]:
        rows = session.execute(
            select(PortfolioCashLedger)
            .where(
                and_(
                    PortfolioCashLedger.account_id == account_id,
                    PortfolioCashLedger.event_date <= as_of,
                )
            )
            .order_by(PortfolioCashLedger.event_date.asc(), PortfolioCashLedger.id.asc())
        ).scalars().all()
        return list(rows)

    def list_corporate_actions(self, account_id: int, as_of: date) -> List[PortfolioCorporateAction]:
        with self.db.get_session() as session:
            return self.list_corporate_actions_in_session(session=session, account_id=account_id, as_of=as_of)

    def list_corporate_actions_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> List[PortfolioCorporateAction]:
        rows = session.execute(
            select(PortfolioCorporateAction)
            .where(
                and_(
                    PortfolioCorporateAction.account_id == account_id,
                    PortfolioCorporateAction.effective_date <= as_of,
                )
            )
            .order_by(PortfolioCorporateAction.effective_date.asc(), PortfolioCorporateAction.id.asc())
        ).scalars().all()
        return list(rows)

    def get_first_activity_date(self, *, account_id: int, as_of: date) -> Optional[date]:
        """Return earliest event date (trade/cash/corporate action) for one account."""
        with self.db.get_session() as session:
            return self.get_first_activity_date_in_session(
                session=session,
                account_id=account_id,
                as_of=as_of,
            )

    def get_first_activity_date_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: date,
    ) -> Optional[date]:
        """``get_first_activity_date`` on an caller-owned session (no new connection)."""
        first_trade = session.execute(
            select(func.min(PortfolioTrade.trade_date)).where(
                and_(
                    PortfolioTrade.account_id == account_id,
                    PortfolioTrade.trade_date <= as_of,
                )
            )
        ).scalar_one()
        first_cash = session.execute(
            select(func.min(PortfolioCashLedger.event_date)).where(
                and_(
                    PortfolioCashLedger.account_id == account_id,
                    PortfolioCashLedger.event_date <= as_of,
                )
            )
        ).scalar_one()
        first_action = session.execute(
            select(func.min(PortfolioCorporateAction.effective_date)).where(
                and_(
                    PortfolioCorporateAction.account_id == account_id,
                    PortfolioCorporateAction.effective_date <= as_of,
                )
            )
        ).scalar_one()

        candidates = [item for item in (first_trade, first_cash, first_action) if item is not None]
        if not candidates:
            return None
        return min(candidates)

    def get_last_trade_date_in_session(
        self,
        *,
        session: Any,
        account_id: int,
        as_of: Optional[date] = None,
    ) -> Optional[date]:
        """Latest ``trade_date`` (optionally ``<= as_of``) of one account, in-session."""
        conditions = [PortfolioTrade.account_id == account_id]
        if as_of is not None:
            conditions.append(PortfolioTrade.trade_date <= as_of)
        return session.execute(
            select(func.max(PortfolioTrade.trade_date)).where(and_(*conditions))
        ).scalar_one()

    def query_trades(
        self,
        *,
        account_id: Optional[int],
        date_from: Optional[date],
        date_to: Optional[date],
        symbols: Optional[List[str]],
        side: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[PortfolioTrade], int]:
        with self.db.get_session() as session:
            conditions = []
            if account_id is not None:
                conditions.append(PortfolioTrade.account_id == account_id)
            if date_from is not None:
                conditions.append(PortfolioTrade.trade_date >= date_from)
            if date_to is not None:
                conditions.append(PortfolioTrade.trade_date <= date_to)
            if symbols:
                conditions.append(PortfolioTrade.symbol.in_(symbols))
            if side:
                conditions.append(PortfolioTrade.side == side)

            data_query = select(PortfolioTrade).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioTrade.account_id,
            )
            count_query = select(func.count()).select_from(PortfolioTrade).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioTrade.account_id,
            )
            conditions.append(PortfolioAccount.is_active.is_(True))
            if conditions:
                where_clause = and_(*conditions)
                data_query = data_query.where(where_clause)
                count_query = count_query.where(where_clause)

            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                data_query
                .order_by(PortfolioTrade.trade_date.desc(), PortfolioTrade.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            return list(rows), total

    def query_cash_ledger(
        self,
        *,
        account_id: Optional[int],
        date_from: Optional[date],
        date_to: Optional[date],
        direction: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[PortfolioCashLedger], int]:
        with self.db.get_session() as session:
            conditions = []
            if account_id is not None:
                conditions.append(PortfolioCashLedger.account_id == account_id)
            if date_from is not None:
                conditions.append(PortfolioCashLedger.event_date >= date_from)
            if date_to is not None:
                conditions.append(PortfolioCashLedger.event_date <= date_to)
            if direction:
                conditions.append(PortfolioCashLedger.direction == direction)

            data_query = select(PortfolioCashLedger).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCashLedger.account_id,
            )
            count_query = select(func.count()).select_from(PortfolioCashLedger).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCashLedger.account_id,
            )
            conditions.append(PortfolioAccount.is_active.is_(True))
            if conditions:
                where_clause = and_(*conditions)
                data_query = data_query.where(where_clause)
                count_query = count_query.where(where_clause)

            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                data_query
                .order_by(PortfolioCashLedger.event_date.desc(), PortfolioCashLedger.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            return list(rows), total

    def query_corporate_actions(
        self,
        *,
        account_id: Optional[int],
        date_from: Optional[date],
        date_to: Optional[date],
        symbols: Optional[List[str]],
        action_type: Optional[str],
        page: int,
        page_size: int,
    ) -> Tuple[List[PortfolioCorporateAction], int]:
        with self.db.get_session() as session:
            conditions = []
            if account_id is not None:
                conditions.append(PortfolioCorporateAction.account_id == account_id)
            if date_from is not None:
                conditions.append(PortfolioCorporateAction.effective_date >= date_from)
            if date_to is not None:
                conditions.append(PortfolioCorporateAction.effective_date <= date_to)
            if symbols:
                conditions.append(PortfolioCorporateAction.symbol.in_(symbols))
            if action_type:
                conditions.append(PortfolioCorporateAction.action_type == action_type)

            data_query = select(PortfolioCorporateAction).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCorporateAction.account_id,
            )
            count_query = select(func.count()).select_from(PortfolioCorporateAction).join(
                PortfolioAccount,
                PortfolioAccount.id == PortfolioCorporateAction.account_id,
            )
            conditions.append(PortfolioAccount.is_active.is_(True))
            if conditions:
                where_clause = and_(*conditions)
                data_query = data_query.where(where_clause)
                count_query = count_query.where(where_clause)

            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                data_query
                .order_by(PortfolioCorporateAction.effective_date.desc(), PortfolioCorporateAction.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            return list(rows), total

    # ------------------------------------------------------------------
    # Price / FX
    # ------------------------------------------------------------------
    def get_latest_close(self, symbol: str, as_of: date) -> Optional[float]:
        close = self.get_latest_close_with_date(symbol=symbol, as_of=as_of)
        return close[0] if close is not None else None

    def get_latest_close_with_date(self, symbol: str, as_of: date) -> Optional[Tuple[float, date]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code == symbol,
                        StockDaily.date <= as_of,
                    )
                )
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            if row is None or row.close is None:
                return None
            return float(row.close), row.date

    def save_fx_rate(
        self,
        *,
        from_currency: str,
        to_currency: str,
        rate_date: date,
        rate: float,
        source: str = "manual",
        is_stale: bool = False,
    ) -> None:
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioFxRate).where(
                    and_(
                        PortfolioFxRate.from_currency == from_currency,
                        PortfolioFxRate.to_currency == to_currency,
                        PortfolioFxRate.rate_date == rate_date,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    PortfolioFxRate(
                        from_currency=from_currency,
                        to_currency=to_currency,
                        rate_date=rate_date,
                        rate=rate,
                        source=source,
                        is_stale=is_stale,
                    )
                )
            else:
                existing.rate = rate
                existing.source = source
                existing.is_stale = is_stale
                existing.updated_at = datetime.now()
            session.commit()

    def get_latest_fx_rate(
        self,
        *,
        from_currency: str,
        to_currency: str,
        as_of: date,
    ) -> Optional[PortfolioFxRate]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PortfolioFxRate)
                .where(
                    and_(
                        PortfolioFxRate.from_currency == from_currency,
                        PortfolioFxRate.to_currency == to_currency,
                        PortfolioFxRate.rate_date <= as_of,
                    )
                )
                .order_by(desc(PortfolioFxRate.rate_date))
                .limit(1)
            ).scalar_one_or_none()
            return row

    def list_daily_snapshots_for_risk(
        self,
        *,
        as_of: date,
        cost_method: str,
        account_id: Optional[int] = None,
        lookback_days: int = 180,
    ) -> List[PortfolioDailySnapshot]:
        """Load snapshot rows in ascending date order for risk monitoring."""
        with self.db.get_session() as session:
            query = (
                select(PortfolioDailySnapshot)
                .join(
                    PortfolioAccount,
                    PortfolioAccount.id == PortfolioDailySnapshot.account_id,
                )
                .where(
                    and_(
                        PortfolioDailySnapshot.snapshot_date <= as_of,
                        PortfolioDailySnapshot.cost_method == cost_method,
                        PortfolioAccount.is_active.is_(True),
                    )
                )
            )
            if account_id is not None:
                query = query.where(PortfolioDailySnapshot.account_id == account_id)
            rows = session.execute(
                query.order_by(
                    PortfolioDailySnapshot.snapshot_date.asc(),
                    PortfolioDailySnapshot.account_id.asc(),
                )
            ).scalars().all()
            if lookback_days <= 0:
                return list(rows)
            # Keep only the latest N calendar days window for risk calculations.
            cutoff_ordinal = as_of.toordinal() - lookback_days
            return [row for row in rows if row.snapshot_date.toordinal() >= cutoff_ordinal]

    def list_cached_position_identities(
        self,
        *,
        account_id: Optional[int] = None,
    ) -> List[Tuple[str, str]]:
        """Return market/symbol identities from cached non-zero positions only."""
        with self.db.get_session() as session:
            query = (
                select(PortfolioPosition.market, PortfolioPosition.symbol)
                .join(PortfolioAccount, PortfolioPosition.account_id == PortfolioAccount.id)
                .where(
                    PortfolioPosition.quantity > 0,
                    PortfolioAccount.is_active.is_(True),
                )
            )
            if account_id is not None:
                query = query.where(PortfolioPosition.account_id == account_id)
            rows = session.execute(
                query.order_by(
                    PortfolioPosition.market.asc(),
                    PortfolioPosition.symbol.asc(),
                )
            ).all()
            seen = set()
            identities: List[Tuple[str, str]] = []
            for market, symbol in rows:
                market_text = str(market or "").strip().lower()
                symbol_text = str(symbol or "").strip()
                identity = (market_text, symbol_text)
                if market_text and symbol_text and identity not in seen:
                    seen.add(identity)
                    identities.append(identity)
            return identities

    # ------------------------------------------------------------------
    # Snapshot / position cache
    # ------------------------------------------------------------------
    def replace_positions_and_lots(
        self,
        *,
        account_id: int,
        cost_method: str,
        positions: Iterable[Dict[str, Any]],
        lots: Iterable[Dict[str, Any]],
        valuation_currency: str,
    ) -> None:
        with self.db.get_session() as session:
            session.execute(
                delete(PortfolioPosition).where(
                    and_(
                        PortfolioPosition.account_id == account_id,
                        PortfolioPosition.cost_method == cost_method,
                    )
                )
            )
            session.execute(
                delete(PortfolioPositionLot).where(
                    and_(
                        PortfolioPositionLot.account_id == account_id,
                        PortfolioPositionLot.cost_method == cost_method,
                    )
                )
            )

            for item in positions:
                session.add(
                    PortfolioPosition(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=item["symbol"],
                        market=item["market"],
                        currency=item["currency"],
                        quantity=float(item["quantity"]),
                        avg_cost=float(item["avg_cost"]),
                        total_cost=float(item["total_cost"]),
                        last_price=float(item["last_price"]),
                        market_value_base=float(item["market_value_base"]),
                        unrealized_pnl_base=float(item["unrealized_pnl_base"]),
                        valuation_currency=valuation_currency,
                    )
                )

            for lot in lots:
                session.add(
                    PortfolioPositionLot(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=lot["symbol"],
                        market=lot["market"],
                        currency=lot["currency"],
                        open_date=lot["open_date"],
                        remaining_quantity=float(lot["remaining_quantity"]),
                        unit_cost=float(lot["unit_cost"]),
                        source_trade_id=lot.get("source_trade_id"),
                    )
                )

            session.commit()

    def _invalidate_account_cache_in_session(self, *, session: Any, account_id: int, from_date: date) -> None:
        session.execute(
            delete(PortfolioPositionLot).where(PortfolioPositionLot.account_id == account_id)
        )
        session.execute(
            delete(PortfolioPosition).where(PortfolioPosition.account_id == account_id)
        )
        session.execute(
            delete(PortfolioDailySnapshot).where(
                and_(
                    PortfolioDailySnapshot.account_id == account_id,
                    PortfolioDailySnapshot.snapshot_date >= from_date,
                )
            )
        )

    @staticmethod
    def _is_sqlite_locked_error(exc: OperationalError) -> bool:
        err_text = str(getattr(exc, "orig", exc)).lower()
        return any(
            token in err_text
            for token in (
                "database is locked",
                "database schema is locked",
                "database table is locked",
            )
        )

    @staticmethod
    def _translate_trade_integrity_error(
        *,
        exc: IntegrityError,
        account_id: int,
        trade_uid: Optional[str],
        dedup_hash: Optional[str],
    ) -> Exception:
        err_text = str(getattr(exc, "orig", exc)).lower()
        if trade_uid and ("uix_portfolio_trade_uid" in err_text or "unique" in err_text):
            return DuplicateTradeUidError(
                f"Duplicate trade_uid for account_id={account_id}: {trade_uid}"
            )
        if dedup_hash and (
            "uix_portfolio_trade_dedup_hash" in err_text
            or "portfolio_trades.account_id, portfolio_trades.dedup_hash" in err_text
            or ("unique" in err_text and "dedup_hash" in err_text)
        ):
            return DuplicateTradeDedupHashError(
                f"Duplicate dedup_hash for account_id={account_id}: {dedup_hash}"
            )
        return exc

    def upsert_daily_snapshot(
        self,
        *,
        account_id: int,
        snapshot_date: date,
        cost_method: str,
        base_currency: str,
        total_cash: float,
        total_market_value: float,
        total_equity: float,
        unrealized_pnl: float,
        realized_pnl: float,
        fee_total: float,
        tax_total: float,
        fx_stale: bool,
        payload: str,
    ) -> None:
        with self.db.get_session() as session:
            existing = session.execute(
                select(PortfolioDailySnapshot).where(
                    and_(
                        PortfolioDailySnapshot.account_id == account_id,
                        PortfolioDailySnapshot.snapshot_date == snapshot_date,
                        PortfolioDailySnapshot.cost_method == cost_method,
                    )
                ).limit(1)
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    PortfolioDailySnapshot(
                        account_id=account_id,
                        snapshot_date=snapshot_date,
                        cost_method=cost_method,
                        base_currency=base_currency,
                        total_cash=total_cash,
                        total_market_value=total_market_value,
                        total_equity=total_equity,
                        unrealized_pnl=unrealized_pnl,
                        realized_pnl=realized_pnl,
                        fee_total=fee_total,
                        tax_total=tax_total,
                        fx_stale=fx_stale,
                        payload=payload,
                    )
                )
            else:
                existing.base_currency = base_currency
                existing.total_cash = total_cash
                existing.total_market_value = total_market_value
                existing.total_equity = total_equity
                existing.unrealized_pnl = unrealized_pnl
                existing.realized_pnl = realized_pnl
                existing.fee_total = fee_total
                existing.tax_total = tax_total
                existing.fx_stale = fx_stale
                existing.payload = payload
                existing.updated_at = datetime.now()
            session.commit()

    def replace_positions_lots_and_snapshot(
        self,
        *,
        account_id: int,
        snapshot_date: date,
        cost_method: str,
        base_currency: str,
        total_cash: float,
        total_market_value: float,
        total_equity: float,
        unrealized_pnl: float,
        realized_pnl: float,
        fee_total: float,
        tax_total: float,
        fx_stale: bool,
        payload: str,
        positions: Iterable[Dict[str, Any]],
        lots: Iterable[Dict[str, Any]],
        valuation_currency: str,
    ) -> None:
        """Atomically refresh position cache and daily snapshot in one transaction."""
        with self.db.get_session() as session:
            session.execute(
                delete(PortfolioPosition).where(
                    and_(
                        PortfolioPosition.account_id == account_id,
                        PortfolioPosition.cost_method == cost_method,
                    )
                )
            )
            session.execute(
                delete(PortfolioPositionLot).where(
                    and_(
                        PortfolioPositionLot.account_id == account_id,
                        PortfolioPositionLot.cost_method == cost_method,
                    )
                )
            )

            for item in positions:
                session.add(
                    PortfolioPosition(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=item["symbol"],
                        market=item["market"],
                        currency=item["currency"],
                        quantity=float(item["quantity"]),
                        avg_cost=float(item["avg_cost"]),
                        total_cost=float(item["total_cost"]),
                        last_price=float(item["last_price"]),
                        market_value_base=float(item["market_value_base"]),
                        unrealized_pnl_base=float(item["unrealized_pnl_base"]),
                        valuation_currency=valuation_currency,
                    )
                )

            for lot in lots:
                session.add(
                    PortfolioPositionLot(
                        account_id=account_id,
                        cost_method=cost_method,
                        symbol=lot["symbol"],
                        market=lot["market"],
                        currency=lot["currency"],
                        open_date=lot["open_date"],
                        remaining_quantity=float(lot["remaining_quantity"]),
                        unit_cost=float(lot["unit_cost"]),
                        source_trade_id=lot.get("source_trade_id"),
                    )
                )

            existing = session.execute(
                select(PortfolioDailySnapshot).where(
                    and_(
                        PortfolioDailySnapshot.account_id == account_id,
                        PortfolioDailySnapshot.snapshot_date == snapshot_date,
                        PortfolioDailySnapshot.cost_method == cost_method,
                    )
                ).limit(1)
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    PortfolioDailySnapshot(
                        account_id=account_id,
                        snapshot_date=snapshot_date,
                        cost_method=cost_method,
                        base_currency=base_currency,
                        total_cash=total_cash,
                        total_market_value=total_market_value,
                        total_equity=total_equity,
                        unrealized_pnl=unrealized_pnl,
                        realized_pnl=realized_pnl,
                        fee_total=fee_total,
                        tax_total=tax_total,
                        fx_stale=fx_stale,
                        payload=payload,
                    )
                )
            else:
                existing.base_currency = base_currency
                existing.total_cash = total_cash
                existing.total_market_value = total_market_value
                existing.total_equity = total_equity
                existing.unrealized_pnl = unrealized_pnl
                existing.realized_pnl = realized_pnl
                existing.fee_total = fee_total
                existing.tax_total = tax_total
                existing.fx_stale = fx_stale
                existing.payload = payload
                existing.updated_at = datetime.now()

            session.commit()

    # ------------------------------------------------------------------
    # Paper trading multi-account: profiles / branches / run audit
    # ------------------------------------------------------------------
    def get_profile_by_id(self, profile_id: int) -> Optional[PaperStrategyProfile]:
        with self.db.get_session() as session:
            return self.get_profile_by_id_in_session(session=session, profile_id=profile_id)

    def get_profile_by_id_in_session(
        self,
        *,
        session: Any,
        profile_id: Optional[int],
    ) -> Optional[PaperStrategyProfile]:
        if profile_id is None:
            return None
        return session.execute(
            select(PaperStrategyProfile).where(PaperStrategyProfile.id == profile_id).limit(1)
        ).scalar_one_or_none()

    def get_profile_by_name_version(
        self,
        name: str,
        version: int = DEFAULT_PAPER_PROFILE_VERSION,
    ) -> Optional[PaperStrategyProfile]:
        with self.db.get_session() as session:
            return self.get_profile_by_name_version_in_session(
                session=session,
                name=name,
                version=version,
            )

    def get_profile_by_name_version_in_session(
        self,
        *,
        session: Any,
        name: str,
        version: int = DEFAULT_PAPER_PROFILE_VERSION,
    ) -> Optional[PaperStrategyProfile]:
        return session.execute(
            select(PaperStrategyProfile)
            .where(
                and_(
                    PaperStrategyProfile.name == name,
                    PaperStrategyProfile.version == int(version),
                )
            )
            .order_by(PaperStrategyProfile.id.asc())
            .limit(1)
        ).scalar_one_or_none()

    def list_active_profiles(self) -> List[PaperStrategyProfile]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PaperStrategyProfile)
                .where(PaperStrategyProfile.is_active.is_(True))
                .order_by(PaperStrategyProfile.name.asc(), PaperStrategyProfile.version.asc())
            ).scalars().all()
            return list(rows)

    def create_profile(
        self,
        *,
        name: str,
        config: Optional[Dict[str, Any]] = None,
        version: int = DEFAULT_PAPER_PROFILE_VERSION,
        is_active: bool = True,
    ) -> PaperStrategyProfile:
        """Insert one policy profile; duplicates of (name, version) raise."""
        payload = dict(config or {})
        with self.portfolio_write_session() as session:
            row = PaperStrategyProfile(
                name=name,
                version=int(version),
                config_json=json.dumps(payload, ensure_ascii=False),
                is_active=is_active,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def get_branch(self, account_id: int) -> Optional[PaperAccountBranch]:
        with self.db.get_session() as session:
            return self.get_branch_in_session(session=session, account_id=account_id)

    def get_branch_in_session(
        self,
        *,
        session: Any,
        account_id: int,
    ) -> Optional[PaperAccountBranch]:
        return session.execute(
            select(PaperAccountBranch)
            .where(PaperAccountBranch.account_id == account_id)
            .limit(1)
        ).scalar_one_or_none()

    def list_branch_accounts(
        self,
        *,
        auto_run_only: bool = False,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Branch rows joined with their account and policy profile.

        Returns one dict per branch, ready for ``run_all_accounts``. Accounts
        without a branch row are not listed here; the caller decides the
        fallback (single trunk account).
        """
        with self.db.get_session() as session:
            statement = (
                select(PaperAccountBranch, PortfolioAccount, PaperStrategyProfile)
                .join(PortfolioAccount, PortfolioAccount.id == PaperAccountBranch.account_id)
                .outerjoin(PaperStrategyProfile, PaperStrategyProfile.id == PaperAccountBranch.profile_id)
                .order_by(PaperAccountBranch.account_id.asc())
            )
            if auto_run_only:
                statement = statement.where(PaperAccountBranch.auto_run.is_(True))
            if not include_inactive:
                statement = statement.where(PortfolioAccount.is_active.is_(True))
            rows = session.execute(statement).all()
            return [
                self._branch_view(branch=branch, account=account, profile=profile)
                for branch, account, profile in rows
            ]

    @staticmethod
    def _branch_view(
        *,
        branch: PaperAccountBranch,
        account: PortfolioAccount,
        profile: Optional[PaperStrategyProfile],
    ) -> Dict[str, Any]:
        role = paper_branch_role(branch.role, branch.parent_account_id)
        return {
            "account_id": int(branch.account_id),
            "account_name": account.name,
            "market": account.market,
            "base_currency": account.base_currency,
            "account_is_active": bool(account.is_active),
            "parent_account_id": branch.parent_account_id,
            "fork_date": branch.fork_date,
            "label": branch.label,
            "auto_run": bool(branch.auto_run),
            "role": role,
            "state": paper_branch_state(role, bool(branch.auto_run)),
            "profile_id": branch.profile_id,
            "profile_name": profile.name if profile is not None else None,
            "profile_version": profile.version if profile is not None else None,
            "config": profile.config if profile is not None else empty_paper_policy_config(),
        }

    def upsert_branch(
        self,
        *,
        account_id: int,
        fork_date: date,
        parent_account_id: Optional[int] = None,
        profile_id: Optional[int] = None,
        label: Optional[str] = None,
        auto_run: bool = False,
        role: Optional[str] = None,
    ) -> PaperAccountBranch:
        """Insert or update the branch row of one account (PK is account_id).

        ``role`` is only written when given, so a routine upsert never undoes a
        promotion; a new row defaults to the trunk role when it has no parent.
        """
        if role is not None and role not in PAPER_BRANCH_ROLES:
            raise ValueError(f"role must be one of {PAPER_BRANCH_ROLES}")
        with self.portfolio_write_session() as session:
            row = self.get_branch_in_session(session=session, account_id=account_id)
            if row is None:
                row = PaperAccountBranch(account_id=account_id)
                session.add(row)
                row.role = role or ("live" if parent_account_id is None else "control")
            row.parent_account_id = parent_account_id
            row.fork_date = fork_date
            row.profile_id = profile_id
            row.label = label
            row.auto_run = bool(auto_run)
            if role is not None:
                row.role = role
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def promote_branch(self, account_id: int) -> Dict[str, Any]:
        """Promote one branch to ``live`` and demote every other live branch.

        Everything happens in one ``BEGIN IMMEDIATE`` transaction. ``auto_run``
        is deliberately untouched: promotion changes governance role, not
        whether the branch keeps trading. Re-running is idempotent, and a
        post-update self-check guarantees exactly one live row survives.
        """
        target_id = int(account_id)
        with self.portfolio_write_session() as session:
            rows = session.execute(select(PaperAccountBranch)).scalars().all()
            target = next((row for row in rows if int(row.account_id) == target_id), None)
            if target is None:
                raise ValueError(f"account {target_id} has no paper branch row")

            demoted: List[int] = []
            for row in rows:
                row_id = int(row.account_id)
                if row_id == target_id:
                    continue
                if paper_branch_role(row.role, row.parent_account_id) == "live":
                    row.role = "control"
                    demoted.append(row_id)
            target.role = "live"
            session.flush()

            live_ids = sorted(
                int(row.account_id)
                for row in rows
                if paper_branch_role(row.role, row.parent_account_id) == "live"
            )
            if live_ids != [target_id]:
                raise RuntimeError(
                    f"promotion self-check failed: live branches are {live_ids}, expected [{target_id}]"
                )

        return {
            "account_id": target_id,
            "live_account_id": target_id,
            "demoted_account_ids": sorted(demoted),
        }

    def record_account_run(
        self,
        *,
        account_id: int,
        batch_key: str,
        profile_id: Optional[int] = None,
        status: str,
        decisions: Optional[Dict[str, Any]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> PaperAccountRun:
        """Append one batch-run audit row; failures propagate to the caller."""
        with self.portfolio_write_session() as session:
            row = PaperAccountRun(
                account_id=account_id,
                batch_key=batch_key,
                profile_id=profile_id,
                status=status,
                decisions_json=json.dumps(decisions or {}, ensure_ascii=False, default=str),
                orders_json=json.dumps(orders or [], ensure_ascii=False, default=str),
                error=error,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def update_account_run(
        self,
        *,
        run_id: int,
        status: str,
        decisions: Optional[Dict[str, Any]] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> Optional[PaperAccountRun]:
        """Finalize a row opened before execution; a missing row returns ``None``.

        ``None`` means the row disappeared (e.g. an older database pruned it);
        the caller then falls back to inserting the terminal row instead of
        losing the audit.
        """
        with self.portfolio_write_session() as session:
            row = session.get(PaperAccountRun, int(run_id))
            if row is None:
                return None
            row.status = status
            if decisions is not None:
                row.decisions_json = json.dumps(decisions, ensure_ascii=False, default=str)
            if orders is not None:
                row.orders_json = json.dumps(orders, ensure_ascii=False, default=str)
            row.error = error
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row

    def list_runs(self, account_id: int, limit: int = 20) -> List[PaperAccountRun]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PaperAccountRun)
                .where(PaperAccountRun.account_id == account_id)
                .order_by(desc(PaperAccountRun.id))
                .limit(int(limit))
            ).scalars().all()
            return list(rows)


def list_paper_accounts_in_session(
    *,
    session: Any,
    account_names: Iterable[str],
    include_inactive: bool = False,
) -> List[PortfolioAccount]:
    """Paper accounts matching the cost-audit filter (broker=paper or name)."""
    names = sorted({str(name or "").strip() for name in account_names if str(name or "").strip()})
    conditions = [func.lower(func.coalesce(PortfolioAccount.broker, "")) == "paper"]
    if names:
        conditions.append(PortfolioAccount.name.in_(names))
    query = select(PortfolioAccount).where(or_(*conditions))
    if not include_inactive:
        query = query.where(PortfolioAccount.is_active.is_(True))
    rows = session.execute(query.order_by(PortfolioAccount.id.asc())).scalars().all()
    return list(rows)


def ensure_paper_branch_defaults(
    *,
    repo: Optional[PortfolioRepository] = None,
    profile_name: str = DEFAULT_PAPER_PROFILE_NAME,
    account_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Self-healing bootstrap for the multi-account tables (safe to re-run).

    1. insert the ``default`` policy profile when missing; its config stays
       ``{"entry": {}, "exit": {}, "risk": {}}`` so behaviour keeps falling back
       to the current env/default path (no live parameters are baked in);
    2. add a branch row for every active paper account that has none, with
       ``auto_run=True`` so the trunk keeps running exactly as before.

    Everything happens in one transaction, and a row that already exists is
    left untouched, so repeated calls never duplicate profiles or branches.
    """
    repository = repo or PortfolioRepository()
    names = list(account_names) if account_names else list(DEFAULT_PAPER_ACCOUNT_NAMES)
    today = date.today()
    summary: Dict[str, Any] = {
        "profile_id": None,
        "profile_created": False,
        "branch_created": [],
        "branch_skipped": [],
    }

    with repository.portfolio_write_session() as session:
        profile = repository.get_profile_by_name_version_in_session(
            session=session,
            name=profile_name,
            version=DEFAULT_PAPER_PROFILE_VERSION,
        )
        if profile is None:
            profile = PaperStrategyProfile(
                name=profile_name,
                version=DEFAULT_PAPER_PROFILE_VERSION,
                config_json=json.dumps(empty_paper_policy_config(), ensure_ascii=False),
                is_active=True,
            )
            session.add(profile)
            session.flush()
            summary["profile_created"] = True
        summary["profile_id"] = int(profile.id)

        for account in list_paper_accounts_in_session(session=session, account_names=names):
            if repository.get_branch_in_session(session=session, account_id=int(account.id)) is not None:
                summary["branch_skipped"].append(int(account.id))
                continue
            if str(account.market or "cn").strip().lower() != "cn":
                # The paper loop only supports A-shares; do not fake a branch day.
                summary["branch_skipped"].append(int(account.id))
                continue
            fork_date = repository.get_last_trade_date_in_session(session=session, account_id=int(account.id))
            if fork_date is None:
                fork_date = repository.get_first_activity_date_in_session(
                    session=session,
                    account_id=int(account.id),
                    as_of=today,
                ) or today
            session.add(
                PaperAccountBranch(
                    account_id=int(account.id),
                    parent_account_id=None,
                    fork_date=fork_date,
                    profile_id=int(profile.id),
                    label=account.name,
                    auto_run=True,
                    role="live",
                )
            )
            summary["branch_created"].append(int(account.id))

    return summary
