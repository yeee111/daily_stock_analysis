# -*- coding: utf-8 -*-
"""Paper trading endpoints: turn due DecisionSignals into portfolio trades."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Query, Security
from fastapi.security import APIKeyCookie

from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.paper_trading import (
    PaperBranchComparisonResponse,
    PaperTradingAccountRun,
    PaperTradingBatch,
    PaperTradingRunAudit,
    PaperTradingRunRequest,
    PaperTradingRunsResponse,
    PaperTradingRunResponse,
)
from src.auth import COOKIE_NAME
from src.services.paper_branch_compare_service import PaperBranchCompareService
from src.services.paper_trading_service import PaperTradingService

logger = logging.getLogger(__name__)

admin_session_cookie = APIKeyCookie(
    name=COOKIE_NAME,
    scheme_name="AdminSessionCookie",
    auto_error=False,
)
router = APIRouter(dependencies=[Security(admin_session_cookie)])

AUTH_RESPONSE = {
    401: {
        "model": ErrorResponse,
        "description": "未登录或管理员会话无效（ADMIN_AUTH_ENABLED=true 时）",
    },
}

# ``all_accounts=true`` takes the policy from every account's own profile, so a
# request mixing the single-account overrides with it would silently ignore
# them — reject the combination instead.
_MULTI_ACCOUNT_EXCLUSIVE_FIELDS = (
    "account_id",
    "account_name",
    "horizons",
    "min_score",
    "max_names",
    "max_weight",
    "signal_date",
    "initial_cash",
)


def _multi_account_conflicts(request: PaperTradingRunRequest) -> List[str]:
    conflicts: List[str] = []
    for key in _MULTI_ACCOUNT_EXCLUSIVE_FIELDS:
        value = getattr(request, key, None)
        if value is None or value is False:
            continue
        conflicts.append(key)
    return conflicts


def _run_all_accounts_response(
    service: PaperTradingService,
    *,
    request: PaperTradingRunRequest,
) -> PaperTradingRunResponse:
    """Map :meth:`PaperTradingService.run_all_accounts` onto the run response.

    The service builds the one shared ``SignalBatch``, so the API never reads
    the signal store twice per request. Every pre-existing response field keeps
    its meaning; the per-account results arrive in the new ``accounts`` list and
    the plan-only fields (``items``/``orders``/``portfolio``/``account``) stay
    empty because each account has its own of those.
    """

    payload = service.run_all_accounts(dry_run=bool(request.dry_run), force=bool(request.force))
    batch: Dict[str, Any] = payload.get("batch") or {}
    batch_key = payload.get("batch_key")
    accounts = [
        PaperTradingAccountRun(
            account_id=row.get("account_id"),
            account_name=row.get("account_name"),
            label=row.get("label"),
            status=str(row.get("status") or ""),
            message=row.get("message"),
            orders=int(row.get("orders") or 0),
            batch_key=batch_key,
            profile_id=row.get("profile_id"),
            profile_name=row.get("profile_name"),
            profile_version=row.get("profile_version"),
            auto_run=row.get("auto_run"),
            warnings=list(row.get("warnings") or []),
        )
        for row in (payload.get("accounts") or [])
    ]
    fill_date = batch.get("fill_date")
    reference_date = batch.get("reference_date")
    return PaperTradingRunResponse(
        status=str(payload.get("status") or ""),
        dry_run=bool(payload.get("dry_run")),
        message=str(payload.get("message") or ""),
        market=str(payload.get("market") or "cn"),
        latest_data_date=batch.get("latest_data_date"),
        batch_key=batch_key,
        # no fill day decided yet -> no batch, same Optional as before
        batch=(
            PaperTradingBatch(
                signal_dates=[str(day) for day in (batch.get("signal_dates") or [])],
                fill_date=str(fill_date),
                reference_date=str(reference_date),
            )
            if fill_date and reference_date
            else None
        ),
        accounts=accounts,
        warnings=list(payload.get("warnings") or []),
    )


@router.post(
    "/run",
    response_model=PaperTradingRunResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "请求字段非法"},
        422: {"model": ErrorResponse, "description": "请求体校验失败"},
        500: {"model": ErrorResponse, "description": "纸面交易执行失败"},
    },
    summary="按到期决策信号生成纸面成交",
    description=(
        "读取 active DecisionSignal，按 created_at 的下一个交易日分批，"
        "先按成交日升序补跑已过期的未执行批次（PAPER_TRADING_CATCHUP_DAYS，默认 5），"
        "再处理成交日等于最新 A 股日线日期的那一批，按开盘价模拟成交。"
        "默认 dry_run=true 只返回计划；dry_run=false 时通过 PortfolioService.record_trade "
        "写入普通组合流水，并自动处理 T+1、整手、涨跌停与 A 股费用。"
        "持仓会按建仓信号里的 stop_loss/target_price 生成止盈止损委托；"
        "因涨跌停/停牌未成交的委托会在 PAPER_TRADING_RETRY_DAYS 内顺延重试；"
        "持仓期间的分红/送转按 PAPER_TRADING_CORPORATE_ACTIONS 自动登记；"
        "新开仓只允许 PAPER_TRADING_ALLOWED_BOARDS 内的板块（默认 main,gem），卖出不受限。"
        "成交使用 trade_uid 幂等；同一成交日已有流水时需显式 force=true。"
        "每次运行结果附带 position_health 持仓止损体检：无止损保护仓位清单与止损距离分布。"
        "all_accounts=true 时改为多账户运行：信号只取一次，每个 auto_run 分支账户按自己的策略档成账，"
        "结果放进新增的 accounts 列表（原有字段仍在，items/orders/portfolio/account 在多账户模式下为空）；"
        "多账户模式不接受单账户策略参数（account_id/account_name/horizons/min_score/max_names/"
        "max_weight/signal_date/initial_cash），这些由各自的策略档决定；"
        "force=true 对每个账户同样生效（只解除同日流水拒绝，trade_uid 幂等仍会跳过已成交委托）。"
    ),
    operation_id="runPaperTrading",
)
def run_paper_trading(request: PaperTradingRunRequest) -> PaperTradingRunResponse:
    service = PaperTradingService()
    try:
        if request.all_accounts:
            conflicts = _multi_account_conflicts(request)
            if conflicts:
                raise ValueError(
                    "all_accounts=true 时策略参数来自各账户自己的策略档，不能同时指定："
                    + "、".join(conflicts)
                )
            return _run_all_accounts_response(service, request=request)
        payload = service.run(
            account_id=request.account_id,
            account_name=request.account_name,
            market=request.market,
            horizons=request.horizons,
            min_score=request.min_score,
            max_names=request.max_names,
            max_weight=request.max_weight,
            signal_date=request.signal_date,
            dry_run=request.dry_run,
            force=request.force,
            initial_cash=request.initial_cash,
        )
        return PaperTradingRunResponse(**payload)
    except ValueError as exc:
        raise api_error(400, "validation_error", str(exc))
    except Exception as exc:  # noqa: BLE001 - API boundary
        logger.error("Run paper trading failed: %s", exc, exc_info=True)
        raise api_error(500, "internal_error", f"Run paper trading failed: {exc}")


def _run_audit(row: Any) -> PaperTradingRunAudit:
    """Decode one ``paper_account_runs`` row; a broken JSON blob never breaks the read."""

    decisions: Dict[str, Any] = {}
    raw = getattr(row, "decisions_json", None)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                decisions = parsed
        except (TypeError, ValueError):
            decisions = {}
    order_count = 0
    orders_raw = getattr(row, "orders_json", None)
    if orders_raw:
        try:
            orders = json.loads(orders_raw)
            order_count = len(orders) if isinstance(orders, list) else 0
        except (TypeError, ValueError):
            order_count = 0
    created_at = getattr(row, "created_at", None)
    return PaperTradingRunAudit(
        id=int(row.id),
        account_id=getattr(row, "account_id", None),
        batch_key=str(getattr(row, "batch_key", "") or ""),
        profile_id=getattr(row, "profile_id", None),
        status=str(getattr(row, "status", "") or ""),
        error=getattr(row, "error", None),
        created_at=created_at.isoformat(sep=" ") if created_at is not None else None,
        dry_run=decisions.get("dry_run"),
        message=decisions.get("message"),
        fill_date=decisions.get("fill_date"),
        reference_date=decisions.get("reference_date"),
        latest_data_date=decisions.get("latest_data_date"),
        signal_ids=[int(item) for item in (decisions.get("signal_ids") or []) if str(item).lstrip("-").isdigit()],
        due_signal_ids=[
            int(item) for item in (decisions.get("due_signal_ids") or []) if str(item).lstrip("-").isdigit()
        ],
        horizons=[str(item) for item in (decisions.get("horizons_union") or [])],
        exit_levels=dict(decisions.get("exit_levels") or {}),
        policy=dict(decisions.get("policy") or {}),
        orders=order_count,
    )


@router.get(
    "/runs",
    response_model=PaperTradingRunsResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "请求字段非法"},
        500: {"model": ErrorResponse, "description": "审计读取失败"},
    },
    summary="读取纸面账户的批次运行审计",
    description=(
        "只读返回 paper_account_runs 最近的行：跑了哪个信号批次、实际读到并计入的 signal_id、"
        "出场位与生效策略档、成交笔数与错误信息，按写入顺序倒序。"
        "dry-run 不写审计行（FIX-1），所以这里只会出现真正执行过的批次。"
    ),
    operation_id="listPaperTradingRuns",
)
def list_paper_trading_runs(
    account_id: int = Query(..., gt=0, description="组合账户 id"),
    limit: int = Query(20, ge=1, le=200, description="最多返回多少条，默认 20"),
) -> PaperTradingRunsResponse:
    service = PaperTradingService()
    repo = getattr(service.portfolio, "repo", None)
    lister = getattr(repo, "list_runs", None)
    if not callable(lister):
        raise api_error(500, "internal_error", "当前组合仓库不支持审计读取")
    try:
        rows = lister(int(account_id), int(limit)) or []
    except Exception as exc:  # noqa: BLE001 - API boundary
        logger.error("List paper trading runs failed for account %s: %s", account_id, exc, exc_info=True)
        raise api_error(500, "internal_error", f"List paper trading runs failed: {exc}")
    return PaperTradingRunsResponse(account_id=int(account_id), runs=[_run_audit(row) for row in rows])


def _parse_account_ids(raw: Optional[str]) -> Optional[List[int]]:
    if raw is None:
        return None
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise ValueError("account_ids 不能为空")
    account_ids: List[int] = []
    for token in tokens:
        if not token.isdigit() or int(token) <= 0:
            raise ValueError(f"account_ids 含非法账户 id：{token}")
        account_ids.append(int(token))
    return account_ids


@router.get(
    "/branches/comparison",
    response_model=PaperBranchComparisonResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "请求字段非法"},
        500: {"model": ErrorResponse, "description": "对比计算失败"},
    },
    summary="跨账户对比分支净值与转正门槛",
    description=(
        "只读接口，不写任何快照或审计行。把每个纸面分支账户的日度权益按外部现金流水还原为"
        "时间加权净值（TWR，首日归一为 100），取所有分支分叉日的最大值到 as_of 的共同交易日窗口，"
        "与 live 参照账户对比，计算区间收益、最大回撤、超额、最近 20 日滚动胜率、跟踪偏离与连续榜首天数。"
        "7 条转正门槛沿用影子框架阈值：可比≥20 日、成交≥8 笔、超额≥5%、回撤比≤1.2"
        "（live 回撤≈0 时改用分支回撤≤10%）、滚动胜率≥60%、连续榜首≥3 天；"
        "第 7 条参数稳健性在 DSA 分支上不适用（分支差异在出场纪律而非策略参数），返回 na 且不影响通过判定。"
        "净值序列来自已存在的 portfolio_daily_snapshots，因此依赖日常风控/纸面运行持续回填；"
        "接口本身不会为了补数据而写库。"
    ),
    operation_id="comparePaperBranches",
)
def compare_paper_branches(
    as_of: Optional[date] = Query(None, description="对比截止日，默认今天"),
    lookback_days: int = Query(180, ge=20, le=2000, description="净值回看的自然日窗口"),
    cost_method: Literal["fifo", "avg"] = Query("fifo", description="成本口径"),
    account_ids: Optional[str] = Query(
        None,
        description="只对比指定账户 id，逗号分隔；live 参照仍会加载用于计算超额",
    ),
) -> PaperBranchComparisonResponse:
    try:
        ids = _parse_account_ids(account_ids)
    except ValueError as exc:
        raise api_error(400, "validation_error", str(exc))
    service = PaperBranchCompareService()
    try:
        payload = service.compare(
            as_of=as_of,
            lookback_days=int(lookback_days),
            cost_method=cost_method,
            account_ids=ids,
        )
        return PaperBranchComparisonResponse(**payload)
    except ValueError as exc:
        raise api_error(400, "validation_error", str(exc))
    except Exception as exc:  # noqa: BLE001 - API boundary
        logger.error("Compare paper branches failed: %s", exc, exc_info=True)
        raise api_error(500, "internal_error", f"Compare paper branches failed: {exc}")
