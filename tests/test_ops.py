import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from wortling.db import Database, now
from wortling.llm.client import LLMClient
from wortling.ops import operational_status
from wortling.worker import heartbeat


@pytest.fixture
def healthy(settings):
    db = Database(settings.data_dir)
    db.initialize(settings)
    heartbeat(settings)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO backup_runs(id,created_at,status,path) "
            "VALUES ('backup',?,'complete','/backup')",
            (now(),),
        )
    return settings, db


def test_idle_worker_is_healthy_and_dead_worker_is_detected(healthy):
    settings, _ = healthy
    assert operational_status(settings)["status"] == "ok"
    path = settings.data_dir / "worker-heartbeat"
    os.utime(path, (0, 0))
    assert "worker_heartbeat_stale" in operational_status(settings)["issues"]
    path.unlink()
    assert "worker_heartbeat_missing" in operational_status(settings)["issues"]


@pytest.mark.parametrize(
    "status,issue",
    [
        ("pending", "pending_jobs_overdue"),
        ("running", "running_job_lease_expired"),
        ("failed", "failed_jobs"),
    ],
)
def test_queue_failures_page_even_with_a_live_worker(healthy, status, issue):
    from test_pipeline import capture

    settings, db = healthy
    cid = capture(settings, "Die Sitzung beginnt morgen.")
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?,next_attempt_at=?,lease_until=? WHERE capture_id=?",
            (status, old, old, cid),
        )
    report = operational_status(settings)
    assert issue in report["issues"]
    assert "Sitzung" not in str(report)


def test_application_alerts_backup_and_sync_failures(healthy):
    settings, db = healthy
    collection = settings.data_dir / "collections/test/collection.anki2"
    collection.parent.mkdir(parents=True)
    collection.touch()
    with db.connect() as conn:
        conn.execute("DELETE FROM backup_runs")
        conn.execute(
            "INSERT INTO alerts(kind,message,created_at) VALUES ('sync_failed','private',?)",
            (now(),),
        )
    report = operational_status(settings)
    assert set(report["issues"]) == {
        "application_alerts_unacknowledged",
        "backup_missing_or_stale",
        "scheduled_sync_overdue",
    }
    assert "private" not in str(report)


def test_synthetic_key_is_required_without_hyper_fallback(healthy, monkeypatch):
    settings, db = healthy
    settings.llm.api_key_env = "SYNTHETIC_API_KEY"
    monkeypatch.delenv("SYNTHETIC_API_KEY", raising=False)
    monkeypatch.setenv("HYPER_API_KEY", "old-key")
    with pytest.raises(RuntimeError, match="SYNTHETIC_API_KEY"):
        LLMClient(settings, db).configured_token()
    monkeypatch.setenv("SYNTHETIC_API_KEY", "new-key")
    assert LLMClient(settings, db).configured_token() == "new-key"


@pytest.mark.parametrize(
    "status,issue",
    [
        (401, "provider_auth_or_availability_failed"),
        (429, "provider_auth_or_availability_failed"),
        (503, "provider_auth_or_availability_failed"),
        (200, "configured_models_unavailable"),
    ],
)
def test_provider_failures_are_visible_and_redacted(healthy, monkeypatch, status, issue):
    settings, _ = healthy
    settings.llm.api_key_env = "SYNTHETIC_API_KEY"
    monkeypatch.setenv("SYNTHETIC_API_KEY", "private-key")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={"data": [], "error": "private-body"})
    )
    report = operational_status(settings, probe_provider=True, transport=transport)
    assert issue in report["issues"]
    assert "private" not in str(report)


def monitor_module():
    path = Path(__file__).resolve().parents[1] / "scripts/monitor.py"
    spec = importlib.util.spec_from_file_location("wortling_monitor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_pages_deduplicates_reminds_and_recovers(tmp_path):
    notify = monitor_module().notify
    state = tmp_path / "state.json"
    messages = []
    clock = [10000]
    bad = {"issues": ["failed_jobs"], "metrics": {}}
    good = {"issues": [], "metrics": {}}
    for report, advance in [(bad, 0), (bad, 300), (bad, 3600), (good, 300), (good, 300)]:
        clock[0] += advance
        notify(report, state, sender=messages.append, clock=lambda: clock[0])
    assert len(messages) == 3
    assert "failed_jobs" in messages[0]
    assert "recovered" in messages[-1]


def test_failed_alert_delivery_does_not_suppress_retry(tmp_path):
    notify = monitor_module().notify
    state = tmp_path / "state.json"

    def broken(message):
        raise RuntimeError("delivery failed")

    with pytest.raises(RuntimeError):
        notify({"issues": ["failed_jobs"], "metrics": {}}, state, sender=broken)
    assert not state.exists()
    sent = []
    notify({"issues": ["failed_jobs"], "metrics": {}}, state, sender=sent.append)
    assert len(sent) == 1


def test_host_watchdog_detects_api_and_worker_outages(monkeypatch):
    module = monitor_module()

    def broken(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(module.urllib.request, "urlopen", broken)
    monkeypatch.setattr(module.subprocess, "run", broken)
    assert module.collect(".")["issues"] == ["api_unreachable", "worker_probe_unavailable"]
