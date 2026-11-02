#!/usr/bin/env python3
"""Install the station's independent, five-minute operational watchdog."""

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "space.sublayer.wortling-monitor"


def plist(repo, home, logs):
    """The launchd job, as bytes.

    It deliberately carries no BUILD_SHA. On 2026-11-01 one was added here by
    hand to unblind the watchdog, and that is the shape of the trap: the value
    is a copy of a commit id in a file nothing regenerates, and the next install
    would have silently dropped it again. The probe now addresses the container
    with `docker exec`, so it needs no compose interpolation and no build sha -
    if a future probe does need one, derive it here rather than parking it.
    """
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": ["/usr/bin/python3", str(repo / "scripts/monitor.py")],
            "EnvironmentVariables": {
                "HOME": str(home),
                "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            },
            "StartInterval": 300,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "monitor.log"),
            "StandardErrorPath": str(logs / "monitor.log"),
        }
    )


def main():
    home = Path.home()
    repo = Path(__file__).resolve().parents[1]
    logs = home / "Library/Logs/wortling"
    logs.mkdir(parents=True, exist_ok=True)
    target = home / "Library/LaunchAgents" / (LABEL + ".plist")
    target.write_bytes(plist(repo, home, logs))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print("Installed", LABEL, "(every 300 seconds)")


if __name__ == "__main__":
    main()
