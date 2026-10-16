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
if [[ -n "$(docker-compose ps --status running -q worker)" ]]; then
  docker-compose exec -T worker python -c '
import sqlite3
from wortling.config import load_settings
db=sqlite3.connect(load_settings().data_dir / "wortling.sqlite3")
assert db.execute("SELECT COUNT(*) FROM jobs WHERE status=?", ("running",)).fetchone()[0] == 0, "Wait for active captures before deploying"
'
fi
docker-compose build --build-arg BUILD_SHA="$head_sha" api
docker-compose up -d api worker
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
