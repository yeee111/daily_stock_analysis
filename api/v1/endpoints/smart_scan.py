# -*- coding: utf-8 -*-
"""Smart scan endpoints — batch AI analysis + ranked buy list."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from api.v1.errors import api_error
from api.v1.schemas.smart_scan import (
    SmartScanAccepted,
    SmartScanRequest,
    SmartScanResult,
)
from src.services.smart_scan_service import SmartScanService
from src.services.task_queue import TaskStatus as QueueTaskStatus, get_task_queue

logger = logging.getLogger(__name__)

router = APIRouter()


class SmartScanHistoryItem(BaseModel):
    scan_id: str
    status: str
    message: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_stocks: int = 0
    succeeded: int = 0


@router.post(
    "/scan",
    response_model=SmartScanAccepted,
    status_code=202,
    summary="启动智能扫描",
    description="批量分析股票池，汇总 AI 决策信号，输出排名买入清单",
)
def start_smart_scan(request: SmartScanRequest) -> SmartScanAccepted:
    scan_id = uuid.uuid4().hex
    task_queue = get_task_queue()
    scan_service = SmartScanService()

    def run_scan() -> Dict[str, Any]:
        def report_progress(progress: int, message: str) -> None:
            task_queue.update_task_progress(scan_id, progress, message)

        return scan_service.run_scan(
            stock_codes=request.stock_codes,
            scan_id=scan_id,
            report_type=request.report_type,
            max_concurrent=request.max_concurrent,
            progress_callback=report_progress,
        )

    task = task_queue.submit_background_task(
        run_scan,
        stock_code="smart_scan",
        stock_name=f"智能扫描 ({len(request.stock_codes)} 只)",
        report_type="smart_scan",
        message="智能扫描已启动",
        task_id=scan_id,
        trace_id=scan_id,
    )

    return SmartScanAccepted(
        scan_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "智能扫描已启动",
        total_stocks=len(request.stock_codes),
    )


@router.get(
    "/scan/{scan_id}",
    response_model=SmartScanResult,
    summary="查询扫描进度与结果",
)
def get_smart_scan_result(scan_id: str) -> SmartScanResult:
    task = get_task_queue().get_task(scan_id)
    if task is None or task.report_type != "smart_scan":
        raise api_error(404, "smart_scan_not_found", f"扫描任务 {scan_id} 不存在或已过期")

    status_value = task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status)
    result = task.result if status_value == "completed" and isinstance(task.result, dict) else None

    if result:
        return SmartScanResult(**result)

    return SmartScanResult(
        scan_id=scan_id,
        status=status_value,
        progress=task.progress,
        message=task.message,
        error=task.error,
        created_at=task.created_at.isoformat() if task.created_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


@router.get(
    "/history",
    response_model=List[SmartScanHistoryItem],
    summary="历史扫描记录",
)
def list_smart_scan_history(
    limit: int = Query(20, ge=1, le=100),
) -> List[SmartScanHistoryItem]:
    task_queue = get_task_queue()
    all_tasks = task_queue.list_all_tasks(limit=200)

    scan_tasks = [t for t in all_tasks if t.report_type == "smart_scan"][:limit]
    items: List[SmartScanHistoryItem] = []
    for t in scan_tasks:
        result = t.result if isinstance(t.result, dict) else {}
        aggregation = result.get("aggregation", {})
        status_value = t.status.value if isinstance(t.status, QueueTaskStatus) else str(t.status)
        items.append(SmartScanHistoryItem(
            scan_id=t.task_id,
            status=status_value,
            message=t.message,
            created_at=t.created_at.isoformat() if t.created_at else None,
            completed_at=t.completed_at.isoformat() if t.completed_at else None,
            total_stocks=aggregation.get("total", 0),
            succeeded=aggregation.get("succeeded", 0),
        ))
    return items
