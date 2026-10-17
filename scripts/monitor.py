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


def collect(repo):
    issues = []
    metrics = {}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/v1/health", timeout=5) as response:
            if json.load(response).get("status") != "ok":
                issues.append("api_unhealthy")
    except Exception:
        issues.append("api_unreachable")
    try:
        result = subprocess.run(
            ["docker-compose", "exec", "-T", "worker", "wortling", "ops-check", "--probe-provider"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        report = json.loads(result.stdout)
        issues.extend(report["issues"])
        metrics = report["metrics"]
        if result.returncode and not report["issues"]:
            issues.append("worker_probe_failed")
    except Exception:
        issues.append("worker_probe_unavailable")
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
