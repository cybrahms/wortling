#!/usr/bin/env python3
"""Independent host watchdog: API, worker, queue, scheduler, backups, provider.

Run every five minutes using deploy/install-monitor.py. Only aggregate signals
leave the host; the Telegram credential is read from the station's existing files.
"""

import argparse
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path


class ProbeCannotRun(RuntimeError):
    """The probe never got as far as asking the worker anything.

    This is not a statement about the worker. Reporting it as one is what paged
    the operator for fourteen hours with "worker_probe_unavailable" while the
    worker answered `ops-check` fine on every hand-run, and the reason - compose
    refusing to interpolate a missing BUILD_SHA - sat on a stderr nobody kept.
    """


def worker_container(timeout=30):
    """The running worker container, addressed without compose.

    `docker-compose exec` interpolates the whole compose file before it does
    anything, and compose.yaml requires BUILD_SHA (the build gate), so a
    watchdog that shells out through compose goes blind in a checkout that is on
    main - a requirement that has nothing to do with exec'ing into a container
    that is already running. Asking the docker daemon for the container skips
    the file entirely.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.service=worker",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeCannotRun(f"docker ps could not run: {exc}") from None
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not names:
        raise ProbeCannotRun(
            "no running worker container: "
            + (result.stderr.strip() or "docker ps returned nothing")
        )
    if "wortling-worker-1" in names:
        return "wortling-worker-1"
    if len(names) > 1:
        raise ProbeCannotRun(
            "several worker containers are running and none is wortling-worker-1: "
            + ", ".join(sorted(names))
        )
    return names[0]


def probe_worker(timeout=60):
    """The worker's own operational report, or ProbeCannotRun."""
    container = worker_container()
    try:
        result = subprocess.run(
            ["docker", "exec", container, "wortling", "ops-check", "--probe-provider"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeCannotRun(f"docker exec into {container} could not run: {exc}") from None
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise ProbeCannotRun(
            "the probe produced no report from "
            + container
            + ": "
            + (result.stderr.strip() or str(exc))
        ) from None
    return report, result.returncode, result.stderr.strip()


def collect(repo=None):
    issues = []
    metrics = {}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/v1/health", timeout=5) as response:
            if json.load(response).get("status") != "ok":
                issues.append("api_unhealthy")
    except Exception:
        issues.append("api_unreachable")
    try:
        report, returncode, stderr = probe_worker()
    except ProbeCannotRun as exc:
        # Distinct from a sick worker, and carrying the reason: a check that
        # could not run is a fact about the watchdog, never about the service.
        issues.append("probe_cannot_run")
        metrics = {"probe_error": str(exc)[:400]}
        return {"issues": sorted(set(issues)), "metrics": metrics}
    issues.extend(report["issues"])
    metrics = report["metrics"]
    if returncode and not report["issues"]:
        issues.append("worker_probe_failed")
        if stderr:
            metrics = {**metrics, "probe_warning": stderr[:400]}
    return {"issues": sorted(set(issues)), "metrics": metrics}


def send_alert(message):
    directory = Path.home() / ".config/iris-telegram"
    token = (directory / "token").read_text().strip()
    chat = (directory / "chat_id").read_text().strip()
    data = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage", data=data
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("Telegram did not accept the alert")


def notify(report, state_path, *, sender=send_alert, clock=time.time):
    try:
        previous = json.loads(state_path.read_text())
    except (FileNotFoundError, ValueError):
        previous = {}
    issues = report["issues"]
    changed = issues != previous.get("issues", [])
    due = clock() - previous.get("last_notified", 0) >= 3600
    if (issues and (changed or due)) or (not issues and previous.get("issues")):
        message = (
            "Wortling needs attention: " + ", ".join(issues)
            if issues
            else "Wortling recovered: operational checks are healthy."
        )
        sender(message)
        previous["last_notified"] = clock()
    previous.update(report, checked_at=clock())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(previous))
    temporary.chmod(0o600)
    temporary.replace(state_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = collect(Path(__file__).resolve().parents[1])
    print(json.dumps({"checked_at": time.time(), **report}), flush=True)
    if not args.dry_run:
        try:
            notify(report, Path.home() / ".local/state/wortling-monitor/state.json")
        except Exception:
            # urllib exceptions can embed the Telegram URL, which contains a secret.
            print("Wortling monitor alert delivery/state update failed", flush=True)
            raise SystemExit(2) from None
    raise SystemExit(1 if report["issues"] else 0)


if __name__ == "__main__":
    os.umask(0o077)
    main()
