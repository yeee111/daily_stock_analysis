# -*- coding: utf-8 -*-
"""API tests for smart scan endpoints."""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.v1.endpoints import smart_scan as module
from src.services.task_queue import TaskInfo, TaskStatus


class _FakeQueue:
    def __init__(self) -> None:
        self.tasks: Dict[str, TaskInfo] = {}
        self.submitted: List[Dict[str, Any]] = []

    def submit_background_task(self, run_task, **kwargs: Any) -> TaskInfo:
        self.submitted.append({"run_task": run_task, "kwargs": kwargs})
        task = TaskInfo(
            task_id=kwargs["task_id"],
            stock_code=kwargs["stock_code"],
            stock_name=kwargs.get("stock_name"),
            status=TaskStatus.PENDING,
            message=kwargs.get("message"),
            report_type=kwargs.get("report_type", "detailed"),
            trace_id=kwargs.get("trace_id"),
        )
        self.tasks[task.task_id] = task
        return task.copy()

    def get_task(self, task_id: str) -> Optional[TaskInfo]:
        task = self.tasks.get(task_id)
        return task.copy() if task else None

    def list_all_tasks(self, limit: int = 50) -> List[TaskInfo]:
        return list(self.tasks.values())[:limit]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(module.router, prefix="/smart-scan")
    return TestClient(app)


def _completed_result(scan_id: str = "scan-1") -> Dict[str, Any]:
    return {
        "scan_id": scan_id,
        "status": "completed",
        "progress": 100,
        "message": "扫描完成，1/1 成功",
        "error": None,
        "aggregation": {
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "action_breakdown": {"buy": 1},
            "score_distribution": {"buy_60_79": 1},
            "avg_score": 70.0,
            "median_score": 70.0,
        },
        "ranked_list": [
            {
                "rank": 1,
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "action": "buy",
                "action_label": "买入",
                "score": 70,
                "confidence": 0.8,
                "entry_low": 10.0,
                "entry_high": 11.0,
                "stop_loss": 9.0,
                "target_price": 13.0,
                "reason": "reason",
                "risk_summary": "risk",
                "catalyst_summary": "catalyst",
                "signal_id": 1,
                "horizon": "5d",
            }
        ],
        "stock_results": [
            {"stock_code": "600519", "stock_name": "贵州茅台", "status": "success", "error": None},
        ],
        "created_at": "2026-11-01T10:00:00+00:00",
        "completed_at": "2026-11-01T10:05:00+00:00",
    }


def _task(
    task_id: str = "scan-1",
    *,
    status: TaskStatus = TaskStatus.PENDING,
    report_type: str = "smart_scan",
    result: Optional[Dict[str, Any]] = None,
) -> TaskInfo:
    task = TaskInfo(task_id=task_id, stock_code="smart_scan", report_type=report_type, status=status)
    task.result = result
    task.progress = 100 if status == TaskStatus.COMPLETED else 20
    task.message = "done" if status == TaskStatus.COMPLETED else "running"
    return task


def test_start_scan_returns_accepted_and_queues_background_task(client: TestClient) -> None:
    queue = _FakeQueue()
    with patch.object(module, "get_task_queue", return_value=queue), patch.object(
        module, "SmartScanService"
    ) as service_cls:
        service = service_cls.return_value
        service.run_scan.return_value = _completed_result()
        response = client.post(
            "/smart-scan/scan",
            json={"stock_codes": ["600519", "000001"], "report_type": "simple", "max_concurrent": 2},
        )

    assert response.status_code == 202
    body = response.json()
    submitted = queue.submitted[0]
    scan_id = submitted["kwargs"]["task_id"]
    assert body["scan_id"] == scan_id
    assert body["trace_id"] == scan_id
    assert body["total_stocks"] == 2
    assert submitted["kwargs"]["report_type"] == "smart_scan"

    assert submitted["run_task"]() == _completed_result()
    service.run_scan.assert_called_once()
    kwargs = service.run_scan.call_args.kwargs
    assert kwargs["stock_codes"] == ["600519", "000001"]
    assert kwargs["scan_id"] == scan_id
    assert kwargs["report_type"] == "simple"
    assert kwargs["max_concurrent"] == 2
    assert callable(kwargs["progress_callback"])


def test_start_scan_rejects_invalid_requests(client: TestClient) -> None:
    queue = _FakeQueue()
    with patch.object(module, "get_task_queue", return_value=queue):
        assert client.post("/smart-scan/scan", json={"stock_codes": []}).status_code == 422
        assert client.post(
            "/smart-scan/scan",
            json={"stock_codes": [f"{i:06d}" for i in range(51)]},
        ).status_code == 422
        assert client.post(
            "/smart-scan/scan",
            json={"stock_codes": ["600519"], "max_concurrent": 6},
        ).status_code == 422
        assert client.post(
            "/smart-scan/scan",
            json={"stock_codes": ["600519"], "report_type": "unknown"},
        ).status_code == 422
    assert queue.submitted == []


def test_get_scan_result_returns_completed_payload(client: TestClient) -> None:
    queue = _FakeQueue()
    queue.tasks["scan-1"] = _task(status=TaskStatus.COMPLETED, result=_completed_result())
    with patch.object(module, "get_task_queue", return_value=queue):
        response = client.get("/smart-scan/scan/scan-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["ranked_list"][0]["stock_code"] == "600519"
    assert body["aggregation"]["succeeded"] == 1


def test_get_scan_result_before_completion_has_no_aggregation(client: TestClient) -> None:
    queue = _FakeQueue()
    queue.tasks["scan-1"] = _task(status=TaskStatus.PROCESSING)
    with patch.object(module, "get_task_queue", return_value=queue):
        response = client.get("/smart-scan/scan/scan-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["aggregation"] is None
    assert body["ranked_list"] is None


def test_get_scan_result_returns_failed_task_error(client: TestClient) -> None:
    queue = _FakeQueue()
    task = _task(status=TaskStatus.FAILED)
    task.error = "boom"
    queue.tasks["scan-1"] = task
    with patch.object(module, "get_task_queue", return_value=queue):
        response = client.get("/smart-scan/scan/scan-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "boom"


def test_get_scan_result_returns_404_for_unknown_or_non_scan_task(client: TestClient) -> None:
    queue = _FakeQueue()
    queue.tasks["detailed-1"] = _task("detailed-1", report_type="detailed")
    with patch.object(module, "get_task_queue", return_value=queue):
        missing = client.get("/smart-scan/scan/missing")
        other = client.get("/smart-scan/scan/detailed-1")

    for response in (missing, other):
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "smart_scan_not_found"


def test_history_only_lists_smart_scan_tasks(client: TestClient) -> None:
    queue = _FakeQueue()
    queue.tasks["scan-1"] = _task(status=TaskStatus.COMPLETED, result=_completed_result())
    queue.tasks["detailed-1"] = _task("detailed-1", report_type="detailed")
    queue.tasks["scan-2"] = _task("scan-2", status=TaskStatus.FAILED)
    with patch.object(module, "get_task_queue", return_value=queue):
        response = client.get("/smart-scan/history", params={"limit": 10})

    assert response.status_code == 200
    items = response.json()
    assert [item["scan_id"] for item in items] == ["scan-1", "scan-2"]
    assert items[0]["total_stocks"] == 1
    assert items[0]["succeeded"] == 1
    assert items[1]["total_stocks"] == 0
