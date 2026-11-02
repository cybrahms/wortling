#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -n "$(git status --porcelain)" ]]; then
  echo 'Commit the reviewable change before deploying.' >&2
  exit 1
fi
head_sha=$(git rev-parse HEAD)
[[ "$head_sha" == "$(git rev-parse '@{upstream}')" ]] || {
  echo 'Push this commit before deploying.' >&2
  exit 1
}
forge checks --json | python3 -c '
import json,sys
state=json.load(sys.stdin)
assert state["terminal"] and state["succeeded"], state["detail"]
'
uv run ruff check .
uv run ruff format --check .
uv run playwright install firefox
uv run pytest -q
# Addressed with docker, not compose: compose interpolates the whole file
# before it does anything, and compose.yaml requires BUILD_SHA, which this script
# only sets on the `up` line below. Run under a shell without it and the compose
# call failed, the test saw an empty string, and this guard - the one that keeps
# a deploy from yanking the worker under an active capture - was silently
# skipped. Same coupling as the watchdog's, same fix.
#
# Scoped to this checkout's compose project, and refused when several workers
# answer: another project's idle worker reports zero running jobs, so picking
# one by position would pass this guard while this project's worker was busy.
# The project name is what compose would derive - COMPOSE_PROJECT_NAME, else the
# directory name, lowercased and stripped to [a-z0-9_-]. The strip matters: a
# checkout in a directory with a dot or a space (`Wortling.Test-1`) normalizes to
# `wortlingtest-1`, and lowercasing alone would query `wortling.test-1`, match
# nothing, and skip this guard silently - the same shape as the fourteen hours.
project=$(printf '%s' "${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")}" \
  | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g')
workers=$(docker ps \
  --filter "label=com.docker.compose.project=${project}" \
  --filter label=com.docker.compose.service=worker \
  --format '{{.Names}}')
count=0
worker=""
for name in $workers; do
  count=$((count + 1))
  worker=$name
done
if [[ "$count" -gt 1 ]]; then
  echo "deploy: project ${project} has ${count} worker containers running; refusing to guess which one to check" >&2
  exit 1
fi
if [[ "$count" -eq 0 ]]; then
  # Say it. A guard that skips without a word is indistinguishable from the one
  # that vanished here for fourteen hours.
  echo "deploy: no running worker in project ${project} - nothing to wait for"
fi
if [[ "$count" -eq 1 ]]; then
  docker exec "$worker" python -c '
import sqlite3
from wortling.config import load_settings
db=sqlite3.connect(load_settings().data_dir / "wortling.sqlite3")
assert db.execute("SELECT COUNT(*) FROM jobs WHERE status=?", ("running",)).fetchone()[0] == 0, "Wait for active captures before deploying"
'
fi
# The gate fetches main and refuses a commit it does not contain, then builds
# the image labelled with HEAD. Do not call docker-compose build directly here.
scripts/build.sh
# Compose interpolates the whole model before it does anything, so `up` fails
# on compose.yaml's required BUILD_SHA even though it would not build. Pass the
# commit this deploy is of; it is the same one build.sh just labelled the image
# with, so the value is a fact rather than a formality.
BUILD_SHA="$head_sha" docker-compose up -d api worker
python3 - "$head_sha" <<'PY'
import json,sys,time,urllib.request
expected=sys.argv[1]
for _ in range(20):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8787/v1/health', timeout=3) as response:
            body=json.load(response)
        if body.get('build') == expected and body.get('status') == 'ok':
            print('Deployed and verified', expected)
            break
    except OSError:
        pass
    time.sleep(0.5)
else:
    raise SystemExit('Deployment did not return the expected healthy build')
PY
