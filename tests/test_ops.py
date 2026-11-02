import importlib.util
import json
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


class _Answer:
    """What subprocess.run returns, for a fake that answers docker."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class _HealthyAPI:
    """A health endpoint that says ok, as a context manager like urlopen's."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"status": "ok"}'


class _Docker:
    """subprocess.run for a docker daemon that has (or has not) a worker.

    Records every argv so a test can assert what the watchdog actually invoked,
    which is the whole point of the compose finding: the command it ran was the
    defect, not the answer it got.
    """

    def __init__(self, names=("wortling-worker-1",), stdout="", stderr="", returncode=0):
        self.names, self.stdout, self.stderr, self.returncode = names, stdout, stderr, returncode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return _Answer(stdout="".join(name + "\n" for name in self.names))
        if argv[:2] == ["docker", "exec"]:
            return _Answer(self.stdout, self.stderr, self.returncode)
        raise AssertionError(f"the watchdog ran something unexpected: {argv}")


def _watching(monkeypatch, docker):
    module = monitor_module()
    monkeypatch.setattr(module.subprocess, "run", docker)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: _HealthyAPI())
    return module


def test_host_watchdog_detects_api_and_worker_outages(monkeypatch):
    module = monitor_module()

    def broken(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(module.urllib.request, "urlopen", broken)
    monkeypatch.setattr(module.subprocess, "run", broken)
    report = module.collect(".")
    assert report["issues"] == ["api_unreachable", "probe_cannot_run"]
    assert "probe_error" in report["metrics"]


def test_the_probe_does_not_go_through_compose(monkeypatch):
    """The outage: compose refuses to interpolate without BUILD_SHA, so a
    watchdog that exec'd through it went blind in a checkout on main while the
    worker answered fine. The probe now asks the daemon for the container, which
    needs no compose file and no build sha."""
    monkeypatch.delenv("BUILD_SHA", raising=False)
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    repo = Path("wortling")
    docker = _Docker(stdout=json.dumps({"issues": [], "metrics": {"jobs": {"done": 49}}}))
    module = _watching(monkeypatch, docker)

    report = module.collect(repo)

    assert report == {"issues": [], "metrics": {"jobs": {"done": 49}}}
    compose_calls = [
        call
        for call in docker.calls
        if call[0] == "docker-compose" or call[:2] == ["docker", "compose"]
    ]
    assert not compose_calls, compose_calls
    assert [
        "docker",
        "exec",
        "wortling-worker-1",
        "wortling",
        "ops-check",
        "--probe-provider",
    ] in docker.calls
    ps = next(call for call in docker.calls if call[:2] == ["docker", "ps"])
    assert f"label=com.docker.compose.project={Path(repo).resolve().name.lower()}" in ps, ps


def test_a_probe_that_cannot_run_names_the_toolchain_not_the_worker(monkeypatch):
    """Fourteen hours of "worker_probe_unavailable" with the worker healthy.
    A probe that never ran is a fact about the watchdog, and it has to carry the
    reason it could not run."""
    docker = _Docker(names=(), stdout="", stderr="")
    module = _watching(monkeypatch, docker)

    report = module.collect(".")

    assert report["issues"] == ["probe_cannot_run"]
    assert "no running worker container" in report["metrics"]["probe_error"]


def test_the_reason_the_probe_could_not_run_survives_into_the_report(monkeypatch):
    gate = "error while interpolating services.api.build.args.BUILD_SHA: required variable"
    docker = _Docker(stdout="", stderr=gate, returncode=1)
    module = _watching(monkeypatch, docker)

    report = module.collect(".")

    assert report["issues"] == ["probe_cannot_run"]
    assert "BUILD_SHA" in report["metrics"]["probe_error"]


def test_a_worker_that_reports_issues_is_still_the_workers_problem(monkeypatch):
    stdout = json.dumps({"issues": ["failed_jobs"], "metrics": {"jobs": {"failed": 1}}})
    docker = _Docker(stdout=stdout)
    module = _watching(monkeypatch, docker)

    report = module.collect(".")

    assert report["issues"] == ["failed_jobs"]
    assert report["metrics"] == {"jobs": {"failed": 1}}


def install_monitor_module():
    path = Path(__file__).resolve().parents[1] / "deploy" / "install-monitor.py"
    spec = importlib.util.spec_from_file_location("wortling_install_monitor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_installed_plist_carries_no_build_sha(tmp_path):
    """A commit id parked in a launchd plist is how the watchdog was unblinded
    on 2026-11-01, and how it would have been blinded again by the next
    install. The probe needs no build sha; the plist must not imply one."""
    import plistlib
    import re

    repo = tmp_path / "wortling"
    logs = tmp_path / "logs"
    payload = install_monitor_module().plist(repo, tmp_path, logs)

    assert "BUILD_SHA" not in payload.decode()
    assert not re.search(r"\b[0-9a-f]{40}\b", payload.decode())
    assert plistlib.loads(payload)["Label"] == "space.sublayer.wortling-monitor"


def test_the_probe_refuses_to_choose_between_two_worker_containers(monkeypatch):
    """Two workers for one project is not a question with an answer. Picking the
    first name docker prints is how a check on the wrong container passes."""
    repo = Path("wortling")
    docker = _Docker(names=("wortling-worker-1", "wortling-worker-2"), stdout="{}")
    module = _watching(monkeypatch, docker)

    report = module.collect(repo)

    assert report["issues"] == ["probe_cannot_run"]
    assert "several worker containers" in report["metrics"]["probe_error"]
    assert not [call for call in docker.calls if call[:2] == ["docker", "exec"]]
