# -*- coding: utf-8 -*-
"""Tests for the runtime overview endpoint and scheduler job snapshot."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_config_dep, get_runtime_scheduler_service
from api.v1.endpoints.runtime import _service_entries
from src.services.runtime_scheduler import RuntimeSchedulerService


class _FakeJob:
    def __init__(self, next_run):
        self.next_run = next_run


class _FakeScheduler:
    def __init__(self):
        self._daily_jobs = [_FakeJob(datetime(2026, 1, 2, 18, 0))]
        self._extra_daily_tasks = [{
            "name": "paper_trading",
            "times": ["18:05", "19:00"],
            "jobs": [_FakeJob(datetime(2026, 1, 2, 18, 5))],
        }]
        self._background_tasks = [{
            "name": "agent_event_monitor",
            "interval_seconds": 300,
            "last_run": datetime(2026, 1, 2, 17, 30).timestamp(),
            "running": False,
        }]


class _FakeSchedulerService:
    def status(self):
        return {
            "enabled": True,
            "running": False,
            "schedule_times": ["18:00"],
            "paper_trading_schedule_times": ["18:05", "19:00"],
            "next_run_at": "2026-01-02T18:00:00",
            "last_run_at": None,
            "last_success_at": "2026-01-01T18:01:00",
            "last_error": None,
            "last_skipped_at": None,
            "last_skip_reason": None,
            "jobs": RuntimeSchedulerService._jobs_snapshot(
                None,
                _FakeScheduler(),
                ["18:00"],
                False,
            ),
        }


class SchedulerJobsSnapshotTestCase(unittest.TestCase):
    """The scheduler status must describe every registered job."""

    def test_snapshot_lists_analysis_paper_trading_and_background_jobs(self):
        jobs = RuntimeSchedulerService._jobs_snapshot(
            None,
            _FakeScheduler(),
            ["18:00"],
            running=True,
        )

        by_id = {job["id"]: job for job in jobs}
        self.assertEqual(set(by_id), {"analysis", "paper_trading", "agent_event_monitor"})
        self.assertEqual(by_id["analysis"]["next_run_at"], "2026-01-02T18:00:00")
        self.assertTrue(by_id["analysis"]["running"])
        self.assertEqual(by_id["paper_trading"]["schedule_times"], ["18:05", "19:00"])
        self.assertEqual(by_id["paper_trading"]["next_run_at"], "2026-01-02T18:05:00")
        self.assertEqual(by_id["agent_event_monitor"]["kind"], "interval")
        self.assertEqual(by_id["agent_event_monitor"]["interval_seconds"], 300)
        self.assertEqual(by_id["agent_event_monitor"]["last_run_at"], "2026-01-02T17:30:00")

    def test_snapshot_is_empty_without_a_live_scheduler(self):
        self.assertEqual(RuntimeSchedulerService._jobs_snapshot(None, None, ["18:00"], False), [])


class ServiceEntriesTestCase(unittest.TestCase):
    """Service entries must distinguish disabled services from running ones."""

    def test_disabled_schedule_and_monitor_are_reported_as_disabled(self):
        services = _service_entries(
            process={"pid": 1, "host": "127.0.0.1", "port": 8000, "uptime_seconds": 1},
            scheduler_status={"enabled": False, "running": False, "schedule_times": []},
            task_queue={"max_workers": 3, "stats": {}},
            event_monitor={"enabled": False, "active": False},
            paper_trading={"scheduled": False, "auto_run": False, "schedule_times": []},
        )

        by_id = {service["id"]: service for service in services}
        self.assertEqual(by_id["backend"]["status"], "running")
        self.assertEqual(by_id["scheduler"]["status"], "disabled")
        self.assertEqual(by_id["event_monitor"]["status"], "disabled")
        self.assertEqual(by_id["paper_trading"]["status"], "disabled")
        self.assertEqual(by_id["task_queue"]["status"], "idle")


class RuntimeOverviewEndpointTestCase(unittest.TestCase):
    """The overview endpoint aggregates process, scheduler and queue state."""

    @classmethod
    def setUpClass(cls):
        cls._temp_dir = tempfile.TemporaryDirectory()
        app = create_app(static_dir=Path(cls._temp_dir.name))
        app.dependency_overrides[get_runtime_scheduler_service] = lambda: _FakeSchedulerService()
        app.dependency_overrides[get_config_dep] = lambda: SimpleNamespace(
            agent_event_monitor_enabled=True,
            agent_event_monitor_interval_minutes=5,
            schedule_enabled=True,
        )
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls._temp_dir.cleanup()

    def test_overview_returns_process_and_service_snapshot(self):
        resp = self.client.get("/api/v1/runtime/overview")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("generated_at", body)
        self.assertIsInstance(body["process"]["pid"], int)
        self.assertIn("uptime_seconds", body["process"])
        self.assertIn("build", body["process"])

    def test_overview_lists_expected_services_and_scheduler_jobs(self):
        body = self.client.get("/api/v1/runtime/overview").json()

        services = {service["id"]: service for service in body["services"]}
        self.assertEqual(
            set(services),
            {"backend", "scheduler", "event_monitor", "task_queue", "paper_trading"},
        )
        self.assertEqual(services["backend"]["status"], "running")
        self.assertEqual(services["scheduler"]["status"], "running")
        self.assertEqual(services["paper_trading"]["status"], "scheduled")
        self.assertEqual(services["paper_trading"]["detail"]["schedule_times"], ["18:05", "19:00"])
        self.assertEqual(services["event_monitor"]["detail"]["interval_minutes"], 5)
        self.assertIn("jobs", body["scheduler"])
        self.assertEqual(len(body["scheduler"]["jobs"]), 3)

    def test_overview_reports_task_queue_counters(self):
        body = self.client.get("/api/v1/runtime/overview").json()

        self.assertIn("stats", body["task_queue"])
        self.assertIn("recent_tasks", body["task_queue"])


class RuntimeOverviewAuthTestCase(unittest.TestCase):
    """The endpoint follows the normal admin-auth gate."""

    @classmethod
    def setUpClass(cls):
        cls._patcher = patch("api.middlewares.auth.is_auth_enabled", return_value=True)
        cls._patcher.start()
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls.client = TestClient(create_app(static_dir=Path(cls._temp_dir.name)))

    @classmethod
    def tearDownClass(cls):
        cls._temp_dir.cleanup()
        cls._patcher.stop()

    def test_overview_requires_login_when_auth_enabled(self):
        resp = self.client.get("/api/v1/runtime/overview")

        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
