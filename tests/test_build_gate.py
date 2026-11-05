import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "gate",
    "GIT_AUTHOR_EMAIL": "gate@example.invalid",
    "GIT_COMMITTER_NAME": "gate",
    "GIT_COMMITTER_EMAIL": "gate@example.invalid",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=main", "--quiet")
    (seed / "tracked.txt").write_text("first\n")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "--quiet", "-m", "first")
    bare = tmp_path / "origin.git"
    # Pin the bare repository's HEAD to main: a clone of a bare repo whose HEAD
    # points at a branch that does not exist checks out nothing, which made
    # these tests pass on a machine with init.defaultBranch=main and fail on a
    # runner with the default master.
    git(tmp_path, "init", "--bare", "--initial-branch=main", "--quiet", str(bare))
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "main")
    work = tmp_path / "work"
    git(tmp_path, "clone", "--quiet", str(bare), str(work))
    return work


def run_gate(work, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), "--check-only", "--repo", str(work), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )


def test_a_main_commit_passes(clone):
    result = run_gate(clone)
    sha = git(clone, "rev-parse", "HEAD")
    assert result.returncode == 0, result.stderr
    assert f"contains {sha}" in result.stdout


def test_an_unmerged_branch_head_is_refused(clone):
    git(clone, "switch", "--quiet", "-c", "local-only")
    (clone / "tracked.txt").write_text("unreviewed\n")
    git(clone, "commit", "--quiet", "-am", "never pushed to the forge")
    sha = git(clone, "rev-parse", "HEAD")

    result = run_gate(clone)

    assert result.returncode == 1
    assert f"refusing to build {sha}" in result.stderr
    assert "does not contain it" in result.stderr
    assert "never pushed to the forge" in result.stderr


def test_an_earlier_main_commit_passes_and_says_how_far_behind(clone):
    first = git(clone, "rev-parse", "HEAD")
    (clone / "tracked.txt").write_text("newer\n")
    git(clone, "commit", "--quiet", "-am", "later work on main")
    git(clone, "push", "--quiet", "origin", "main")
    git(clone, "reset", "--quiet", "--hard", first)

    result = run_gate(clone)

    assert result.returncode == 0, result.stderr
    assert "1 commit(s) behind" in result.stdout


def test_a_branch_diverged_from_main_is_refused(clone):
    git(clone, "switch", "--quiet", "-c", "side")
    (clone / "side.txt").write_text("side\n")
    git(clone, "add", "side.txt")
    git(clone, "commit", "--quiet", "-m", "side work")
    git(clone, "switch", "--quiet", "main")
    (clone / "tracked.txt").write_text("main moves\n")
    git(clone, "commit", "--quiet", "-am", "main work")
    git(clone, "push", "--quiet", "origin", "main")
    git(clone, "switch", "--quiet", "side")
    sha = git(clone, "rev-parse", "HEAD")

    result = run_gate(clone)

    assert result.returncode == 1
    assert f"refusing to build {sha}" in result.stderr
    assert "commits this build lacks: 1" in result.stderr


def test_a_dirty_checkout_is_refused(clone):
    (clone / "tracked.txt").write_text("uncommitted\n")

    result = run_gate(clone)

    assert result.returncode == 1
    assert "dirty" in result.stderr


def test_an_unreachable_forge_is_refused(clone):
    git(clone, "remote", "set-url", "origin", str(clone / "gone.git"))

    result = run_gate(clone)

    assert result.returncode == 1
    assert "could not fetch main" in result.stderr


def test_no_remote_is_refused(clone):
    git(clone, "remote", "remove", "origin")

    result = run_gate(clone)

    assert result.returncode == 1
    assert "no main remote" in result.stderr


# The two halves of the deploy path that have to agree. Compose interpolates the
# whole model before doing anything, so `up` fails on a required build argument
# even though it would not build: on 2026-10-26 that killed a deploy after the
# gate and the build had both succeeded, and nothing in this suite noticed.
ROOT = Path(__file__).resolve().parents[1]


def test_compose_refuses_a_model_without_the_commit():
    compose = (ROOT / "compose.yaml").read_text()
    assert "${BUILD_SHA:?" in compose, (
        "compose.yaml must refuse a model with no BUILD_SHA, or a bare "
        "`docker compose build` produces an image of unknown provenance"
    )


def test_every_compose_up_carries_the_commit_it_deploys():
    lines = [
        line
        for line in (ROOT / "scripts" / "deploy.sh").read_text().splitlines()
        if " up " in line and "docker-compose" in line and not line.lstrip().startswith("#")
    ]
    assert lines, "deploy.sh no longer starts the services; rewrite this control"
    for line in lines:
        assert "BUILD_SHA=" in line, (
            f"compose interpolates BUILD_SHA for `up` too, so this line fails on "
            f"any host whose shell does not export it: {line.strip()}"
        )
