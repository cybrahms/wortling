import os
import re
import shutil
import subprocess
from pathlib import Path


def test_deployment_stops_before_build_when_worker_is_busy(tmp_path):
    def command(name, body):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    command("git", 'case "$1" in status) ;; rev-parse) echo head;; esac')
    command("forge", 'echo \'{"terminal":true,"succeeded":true}\'')
    command("uv", "exit 0")
    # The pre-flight asks docker, not compose: compose refuses to interpolate
    # without BUILD_SHA, which this script only sets on its `up` line, so the
    # busy-worker guard used to vanish silently in a shell that lacked it.
    command(
        "docker",
        """case "$1" in
ps) echo running-worker;;
exec) echo 'Wait for active captures before deploying' >&2; exit 1;;
*) exit 98;;
esac""",
    )
    command("docker-compose", 'touch "$COMPOSE_MARKER"; exit 99')
    marker = tmp_path / "built"
    script = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "BUILD_MARKER": str(marker),
            "COMPOSE_MARKER": str(tmp_path / "composed"),
        },
    )
    assert result.returncode == 1
    assert "Wait for active captures" in result.stderr
    assert not marker.exists()
    assert not (tmp_path / "composed").exists(), "the busy-worker guard went through compose"


def test_the_busy_worker_guard_survives_without_a_build_sha(tmp_path):
    """The guard that keeps a deploy from yanking the worker under an active
    capture must not depend on a variable this script only sets for `up`."""

    def command(name, body):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    command("git", 'case "$1" in status) ;; rev-parse) echo head;; esac')
    command("forge", 'echo \'{"terminal":true,"succeeded":true}\'')
    command("uv", "exit 0")
    command(
        "docker",
        """case "$1" in
ps) echo wortling-worker-1;;
exec) echo 'Wait for active captures before deploying' >&2; exit 1;;
*) exit 98;;
esac""",
    )
    script = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
    environment = {key: value for key, value in os.environ.items() if key != "BUILD_SHA"}
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={**environment, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
    )
    assert "BUILD_SHA" not in environment
    assert result.returncode == 1
    assert "Wait for active captures" in result.stderr


def test_the_busy_worker_guard_asks_about_this_project_and_refuses_to_guess(tmp_path):
    """Two workers answering one question is not an answer: another project's
    idle worker reports zero running jobs, so picking by position would pass this
    guard while this project's worker was busy."""

    def command(name, body):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    command("git", 'case "$1" in status) ;; rev-parse) echo head;; esac')
    command("forge", 'echo \'{"terminal":true,"succeeded":true}\'')
    command("uv", "exit 0")
    command(
        "docker",
        """echo "$@" >> "$DOCKER_LOG"
case "$1" in
ps) printf 'wortling-worker-1\\nwortling-worker-2\\n';;
exec) exit 1;;
*) exit 98;;
esac""",
    )
    script = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DOCKER_LOG": str(log),
        },
    )

    assert result.returncode == 1
    assert "refusing to guess" in result.stderr
    invocations = log.read_text().splitlines()
    ps = next(line for line in invocations if line.startswith("ps"))
    project = re.sub(r"[^a-z0-9_-]", "", Path(script).resolve().parents[1].name.lower())
    assert f"label=com.docker.compose.project={project}" in ps, ps
    assert not [line for line in invocations if line.startswith("exec")]


def test_the_project_name_is_derived_the_way_compose_derives_it(tmp_path):
    """Compose lowercases AND strips to [a-z0-9_-]: `Wortling.Test-1` becomes
    `wortlingtest-1`. Lowercasing alone queried a project that matched nothing,
    skipped the guard, and let `up` replace the real worker."""
    repo = tmp_path / "Wortling.Test-1"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "deploy.sh"
    shutil.copy(Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh", script)

    def command(name, body):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    command("git", 'case "$1" in status) ;; rev-parse) echo head;; esac')
    command("forge", 'echo \'{"terminal":true,"succeeded":true}\'')
    command("uv", "exit 0")
    command(
        "docker",
        """echo "$@" >> "$DOCKER_LOG"
case "$1" in
ps) echo a-worker;;
exec) exit 1;;
*) exit 98;;
esac""",
    )
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DOCKER_LOG": str(log),
        },
    )

    assert result.returncode == 1
    ps = next(line for line in log.read_text().splitlines() if line.startswith("ps"))
    assert "label=com.docker.compose.project=wortlingtest-1" in ps, ps


def test_the_guard_says_so_when_there_is_no_worker_to_wait_for(tmp_path):
    """A skip nobody can see is the failure this PR is about."""

    def command(name, body):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    command("git", 'case "$1" in status) ;; rev-parse) echo head;; esac')
    command("forge", 'echo \'{"terminal":true,"succeeded":true}\'')
    command("uv", "exit 0")
    command(
        "docker",
        """case "$1" in
ps) ;;
exec) exit 1;;
*) exit 98;;
esac""",
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        },
    )

    assert "no running worker in project" in result.stdout, result.stdout + result.stderr
