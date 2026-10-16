import os
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
    command(
        "docker-compose",
        """case "$1" in
ps) echo running-worker;;
exec) echo 'Wait for active captures before deploying' >&2; exit 1;;
build) touch "$BUILD_MARKER"; exit 97;;
*) exit 98;;
esac""",
    )
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
        },
    )
    assert result.returncode == 1
    assert "Wait for active captures" in result.stderr
    assert not marker.exists()
