# -*- coding: utf-8 -*-
"""Runtime status endpoints.

Expose a single read-only snapshot of the process that serves the Web UI so the
desktop shell and the Web UI can show which background services are actually
running: the API process itself, the runtime scheduler jobs (daily analysis,
paper trading, event monitor) and the in-process task queue.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request

from api.deps import get_config_dep, get_runtime_scheduler_service
from src.config import Config
from src.services.runtime_scheduler import RuntimeSchedulerService

router = APIRouter()

# Fallback used when the app lifespan has not recorded a start timestamp
# (for example a bare TestClient without lifespan execution).
_PROCESS_STARTED_AT = time.time()

_BUILD_INFO_PATH = Path(__file__).resolve().parents[3] / "static" / "build-info.json"
_build_info_cache: Optional[Dict[str, Any]] = None


def _read_build_info() -> Dict[str, Any]:
    """Return cached static build metadata, or an empty dict when unavailable."""
    global _build_info_cache
    if _build_info_cache is not None:
        return _build_info_cache

    payload: Dict[str, Any] = {}
    try:
        payload = json.loads(_BUILD_INFO_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    _build_info_cache = payload
    return payload


def _json_safe(value: Any) -> Any:
    """Coerce scheduler internals into JSON-serialisable values."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _process_snapshot(request: Request) -> Dict[str, Any]:
    started_at = float(getattr(request.app.state, "runtime_started_at", _PROCESS_STARTED_AT))
    now = time.time()
    build = _read_build_info()
    return {
        "pid": os.getpid(),
        "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
        "uptime_seconds": max(0, int(now - started_at)),
        "python_version": platform.python_version(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "argv": list(sys.argv),
        "host": request.url.hostname,
        "port": request.url.port,
        "desktop_mode": os.getenv("DSA_DESKTOP_MODE", "").strip().lower() in {"1", "true", "yes", "on"},
        "build": {
            "version": build.get("version"),
            "revision": build.get("revision"),
            "built_at": build.get("buildTime"),
        },
    }


def _task_queue_snapshot(limit: int = 8) -> Dict[str, Any]:
    from src.services.task_queue import get_task_queue

    queue = get_task_queue()
    try:
        stats = queue.get_task_stats()
    except Exception:  # pragma: no cover - defensive, status must never 500
        stats = {}
    try:
        recent_tasks = [task.to_dict() for task in queue.list_all_tasks(limit=limit)]
    except Exception:  # pragma: no cover - defensive, status must never 500
        recent_tasks = []
    return {
        "max_workers": int(getattr(queue, "max_workers", 0) or 0),
        "stats": _json_safe(stats),
        "recent_tasks": recent_tasks,
    }


def _event_monitor_snapshot(config: Config, scheduler_status: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool(getattr(config, "agent_event_monitor_enabled", False))
    interval_minutes = getattr(config, "agent_event_monitor_interval_minutes", None)
    active = False
    if enabled and scheduler_status.get("enabled"):
        jobs = scheduler_status.get("jobs") or []
        active = any(job.get("id") == "agent_event_monitor" for job in jobs)
    return {
        "enabled": enabled,
        "active": active,
        "interval_minutes": interval_minutes,
    }


def _paper_trading_snapshot(scheduler_status: Dict[str, Any]) -> Dict[str, Any]:
    times = list(scheduler_status.get("paper_trading_schedule_times") or [])
    auto_run = os.getenv("PAPER_TRADING_AUTO_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
    scheduled = bool(scheduler_status.get("enabled")) and bool(times)
    return {
        "scheduled": scheduled,
        "auto_run": auto_run,
        "schedule_times": times,
    }


def _service_entries(
    *,
    process: Dict[str, Any],
    scheduler_status: Dict[str, Any],
    task_queue: Dict[str, Any],
    event_monitor: Dict[str, Any],
    paper_trading: Dict[str, Any],
) -> List[Dict[str, Any]]:
    queue_stats = task_queue.get("stats") or {}
    scheduler_enabled = bool(scheduler_status.get("enabled"))
    services: List[Dict[str, Any]] = [
        {
            "id": "backend",
            "status": "running",
            "detail": {
                "pid": process.get("pid"),
                "host": process.get("host"),
                "port": process.get("port"),
                "uptime_seconds": process.get("uptime_seconds"),
            },
        },
        {
            "id": "scheduler",
            "status": "running" if scheduler_enabled else "disabled",
            "detail": {
                "enabled": scheduler_enabled,
                "running": bool(scheduler_status.get("running")),
                "next_run_at": scheduler_status.get("next_run_at"),
                "schedule_times": scheduler_status.get("schedule_times") or [],
            },
        },
        {
            "id": "event_monitor",
            "status": "running" if event_monitor.get("active") else (
                "idle" if (event_monitor.get("enabled") and scheduler_enabled) else "disabled"
            ),
            "detail": event_monitor,
        },
        {
            "id": "task_queue",
            "status": "busy" if int(queue_stats.get("processing") or 0) > 0 else "idle",
            "detail": {
                "max_workers": task_queue.get("max_workers"),
                "pending": int(queue_stats.get("pending") or 0),
                "processing": int(queue_stats.get("processing") or 0),
            },
        },
        {
            "id": "paper_trading",
            "status": "scheduled" if paper_trading.get("scheduled") else "disabled",
            "detail": paper_trading,
        },
    ]
    return services


@router.get(
    "/overview",
    summary="Get runtime service overview",
    description=(
        "Return a snapshot of the current Web/API/Desktop process: process info, "
        "runtime scheduler jobs, task queue stats and the status of the in-process "
        "background services."
    ),
)
def get_runtime_overview(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    """Return the runtime overview snapshot."""
    scheduler_status = scheduler.status()
    task_queue = _task_queue_snapshot()
    event_monitor = _event_monitor_snapshot(config, scheduler_status)
    paper_trading = _paper_trading_snapshot(scheduler_status)
    process = _process_snapshot(request)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "process": process,
        "scheduler": _json_safe(scheduler_status),
        "task_queue": _json_safe(task_queue),
        "event_monitor": _json_safe(event_monitor),
        "paper_trading": _json_safe(paper_trading),
        "services": _service_entries(
            process=process,
            scheduler_status=scheduler_status,
            task_queue=task_queue,
            event_monitor=event_monitor,
            paper_trading=paper_trading,
        ),
    }
