#!/usr/bin/env bash
set -euo pipefail
# Keep a single source snapshot during long simulator runs. Bash otherwise reads
# later portions from disk after commands return, so edits can corrupt execution.
if [[ -z "${BASH_EXECUTION_STRING:-}" ]]; then
  exec bash -c "$(cat "$0")" "$0" "$@"
fi
umask 077
cd "$(dirname "$0")/../.."
command -v xcodegen >/dev/null
wortling_runtime=$(xcrun simctl list runtimes --json | python3 -c '
import json,sys
available=[r for r in json.load(sys.stdin)["runtimes"] if r.get("isAvailable") and r["name"].startswith("iOS ")]
assert available, "Install an iOS runtime: xcodebuild -downloadPlatform iOS -architectureVariant arm64"
print(available[-1]["identifier"])
')
wortling_sim_udid=$(xcrun simctl list devices available --json | python3 -c '
import json,sys
found=[d["udid"] for ds in json.load(sys.stdin)["devices"].values() for d in ds if d["name"]=="Wortling Shortcut Test"]
assert len(found)<=1, "More than one Wortling test simulator exists"
print(found[0] if found else "")
')
if [[ -z "$wortling_sim_udid" ]]; then
  wortling_sim_udid=$(xcrun simctl create 'Wortling Shortcut Test' com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro "$wortling_runtime")
fi
if ! xcrun simctl list devices booted --json | python3 -c 'import json,sys; sys.exit(not any(d["udid"]==sys.argv[1] for ds in json.load(sys.stdin)["devices"].values() for d in ds))' "$wortling_sim_udid"; then
  xcrun simctl boot "$wortling_sim_udid"
fi
xcrun simctl bootstatus "$wortling_sim_udid" -b
mkdir -p data/ios-shortcut-test data/ios-shortcut-test-project data/ios-shortcut-results
python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",8791)); s.close()'
wortling_python=$(uv run python -c 'import sys; print(sys.executable)')
"$wortling_python" scripts/ios/server.py > data/ios-shortcut-test/server.log 2>&1 &
wortling_server_pid=$!
trap 'kill "$wortling_server_pid" 2>/dev/null || true; wait "$wortling_server_pid" 2>/dev/null || true' EXIT
"$wortling_python" - <<'PY'
import json,time,urllib.request
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8791/test/info', timeout=1) as r:
            assert json.load(r)['service']=='wortling-isolated-ios-test'
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit('Test server failed to start; inspect data/ios-shortcut-test/server.log')
PY
sips -s format heic data/ios-shortcut-test/IMG_fixture.jpg --out data/ios-shortcut-test/IMG_fixture.heic >/dev/null
xcrun simctl addmedia "$wortling_sim_udid" data/ios-shortcut-test/IMG_fixture.heic
xcodegen generate --spec scripts/ios/project.yml --project data/ios-shortcut-test-project
wortling_result="data/ios-shortcut-results/$(date -u +%Y%m%dT%H%M%SZ).xcresult"
xcodebuild test -project data/ios-shortcut-test-project/WortlingShortcutTests.xcodeproj \
  -scheme Harness -destination "platform=iOS Simulator,id=$wortling_sim_udid" \
  -derivedDataPath data/ios-shortcut-derived -parallel-testing-enabled NO \
  -resultBundlePath "$wortling_result" CODE_SIGNING_ALLOWED=NO
