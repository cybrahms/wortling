#!/usr/bin/env python3
"""Independent host watchdog: API, worker, queue, scheduler, backups, provider.

Run every five minutes using deploy/install-monitor.py. Only aggregate signals
leave the host. What it finds goes to the crew's Switchboard queue, where the
people who can fix it read it; the Telegram credentials stay only as the fallback
page for a queue that cannot be written. That is the harness's arrangement since
2026-10-21, and the opposite of paging the operator for two days over one
rejected AnkiWeb request.
"""

import argparse
import email.utils
import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Seconds of clock error worth naming as an issue in its own right. AnkiWeb
# refuses a sync long before this; the point of the number is that a crew member
# reads "0.2s" and knows the vendor's sentence was not the diagnosis.
CLOCK_SKEW_LIMIT = 30


class ProbeCannotRun(RuntimeError):
    """The probe never got as far as asking the worker anything.

    This is not a statement about the worker. Reporting it as one is what paged
    the operator for fourteen hours with "worker_probe_unavailable" while the
    worker answered `ops-check` fine on every hand-run, and the reason - compose
    refusing to interpolate a missing BUILD_SHA - sat on a stderr nobody kept.

    One worker fault lands here too, deliberately: a worker whose `ops-check`
    exits without a parsable report. The text of that crash rides in
    `metrics["probe_error"]`, and the issue is still named for the probe because
    that is what an operator has to look at first.
    """


def compose_project(repo):
    """The compose project a checkout belongs to, as compose itself derives it.

    `COMPOSE_PROJECT_NAME` wins; otherwise compose uses the directory name,
    lowercased with anything outside `[a-z0-9_-]` dropped. Scoping the docker
    query to this project is what stops a second learner's worker from answering
    for this one.
    """
    name = os.environ.get("COMPOSE_PROJECT_NAME") or Path(repo or ".").resolve().name
    return "".join(
        character for character in name.lower() if character.isalnum() or character in "-_"
    )


def worker_container(repo, timeout=30):
    """The running worker container for this checkout, addressed without compose.

    `docker-compose exec` interpolates the whole compose file before it does
    anything, and compose.yaml requires BUILD_SHA (the build gate), so a
    watchdog that shells out through compose goes blind in a checkout that is on
    main - a requirement that has nothing to do with exec'ing into a container
    that is already running. Asking the docker daemon for the container skips
    the file entirely.

    The question is scoped to the checkout's compose project and refused when it
    has more than one answer: another project's idle worker reporting zero
    running jobs is indistinguishable from this one's, and picking the first
    name docker prints is how a check on the wrong container passes.
    """
    project = compose_project(repo)
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
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
            f"no running worker container for project {project}: "
            + (result.stderr.strip() or "docker ps returned nothing")
        )
    if len(names) > 1:
        raise ProbeCannotRun(
            f"project {project} has several worker containers and none can be assumed: "
            + ", ".join(sorted(names))
        )
    return names[0]


def probe_worker(repo, timeout=60):
    """The worker's own operational report, or ProbeCannotRun."""
    container = worker_container(repo)
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


def collect(repo=None, *, skew=None):
    issues = []
    metrics = {}
    measured = (skew or clock_skew)()
    if measured is not None:
        metrics["clock_skew_seconds"] = measured
        if abs(measured) > CLOCK_SKEW_LIMIT:
            issues.append("host_clock_skew")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/v1/health", timeout=5) as response:
            if json.load(response).get("status") != "ok":
                issues.append("api_unhealthy")
    except Exception:
        issues.append("api_unreachable")
    try:
        report, returncode, stderr = probe_worker(repo)
    except ProbeCannotRun as exc:
        # Distinct from a sick worker, and carrying the reason: a check that
        # could not run is a fact about the watchdog, never about the service.
        issues.append("probe_cannot_run")
        metrics = {**metrics, "probe_error": str(exc)[:400]}
        return {"issues": sorted(set(issues)), "metrics": metrics}
    issues.extend(report["issues"])
    metrics = {**metrics, **report["metrics"]}
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


def file_todo(title, payload, key, *, runner=subprocess.run):
    """Hand this to the crew as a Switchboard todo; raise if the queue cannot be written.

    The crew reads a queue; the operator reads a phone. Filing first is the same
    decision the harness made for its notify hook on 2026-10-21 - a queue item
    and a quiet phone, with the page kept only as the fallback for a queue that
    cannot be written. Two days of paging over one rejected AnkiWeb request is
    what the other order costs.

    `sbpush` is the station's one-line queue write, and deploy/install-monitor.py
    puts it on this job's PATH. The key is stable for one reported incident, so a
    repeated five-minute run does not mint a second todo.
    """
    result = runner(
        ["sbpush", "-key", key, "-title", title, "-payload", json.dumps(payload, sort_keys=True)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "sbpush failed").strip()[:400])
    return result.stdout


def incident_key(issues, moment):
    """One key per incident per day, so the queue hears about it once a day."""
    day = time.strftime("%Y%m%d", time.gmtime(moment))
    digest = hashlib.sha1("|".join(issues).encode()).hexdigest()[:12]
    return f"wortling-monitor-{day}-{digest}"


def incident_payload(issues, metrics):
    """What a crew member needs to start, as a work order rather than an alert."""
    return {
        "name": "wortling-monitor",
        "scheduled": True,
        "issues": issues,
        "metrics": metrics,
        "instructions": (
            "The wortling watchdog reported this. Read "
            "~/Library/Logs/wortling/monitor.log for the recent snapshots and "
            "~/.local/state/wortling-monitor/state.json for what was delivered, "
            "then run `uv run wortling ops-check --probe-provider` in the worker "
            "checkout for the same report on demand. An alert that its own "
            "success did not clear, or that outlived its horizon, is a defect "
            "worth a case: the alert kinds, their clearing paths and their "
            "horizons live in src/wortling/alerts.py. The operator is "
            "deliberately not paged for work the crew can do."
        ),
    }


def clock_skew(timeout=5):
    """This host's clock against a remote authority, in seconds, or None.

    AnkiWeb refuses a sync whose client clock is too far off, and its sentence
    about it reaches a reader as the whole story: "your clock is not set to the
    correct time". The number is what a crew member needs in order to decide
    whether to touch time at all, so measure it rather than relaying the words.
    """
    request = urllib.request.Request(
        "https://www.cloudflare.com", method="HEAD", headers={"User-Agent": "wortling-monitor"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            header = response.headers.get("Date")
    except Exception:
        return None
    if not header:
        return None
    try:
        authority = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return round(authority.timestamp() - time.time())


def notify(report, state_path, *, filer=None, sender=send_alert, clock=time.time):
    """Record the report where the crew reads it, and page only if that fails."""
    filer = filer or file_todo
    try:
        previous = json.loads(state_path.read_text())
    except (FileNotFoundError, ValueError):
        previous = {}
    issues = report["issues"]
    changed = issues != previous.get("issues", [])
    # A queue item is not a page: it waits until somebody works it, so a
    # repeating problem needs a fresh entry only once a day, not every hour.
    due = clock() - previous.get("last_filed", 0) >= 24 * 3600
    # A recovery is history, not work. Filing "operational checks are healthy"
    # handed the crew a queue entry with nothing in it to triage, and the state
    # file written below already records the moment the issue list emptied.
    if issues and (changed or due):
        title = "wortling needs attention: " + ", ".join(issues)
        key = incident_key(issues, clock())
        if previous.get("filed_key") == key:
            # This incident is already on the crew's queue today. Filing it
            # again is a treadmill, and paging is the thing being fixed.
            pass
        else:
            try:
                filer(title, incident_payload(issues, report.get("metrics", {})), key)
            except Exception:
                # The queue is the crew's path to this. When it cannot be
                # written the operator is the only reader left, so the page
                # survives here and nowhere else.
                sender(title)
            previous["filed_key"] = key
            previous["last_filed"] = clock()
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
