# -*- coding: utf-8 -*-
"""Paper trading endpoints: turn due DecisionSignals into portfolio trades."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Security
from fastapi.security import APIKeyCookie

from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.paper_trading import PaperTradingRunRequest, PaperTradingRunResponse
from src.auth import COOKIE_NAME
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
    ),
    operation_id="runPaperTrading",
)
def run_paper_trading(request: PaperTradingRunRequest) -> PaperTradingRunResponse:
    service = PaperTradingService()
    try:
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
