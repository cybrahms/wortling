#!/usr/bin/env python3
"""Install the station's independent, five-minute operational watchdog."""

import os
import plistlib
import subprocess
from pathlib import Path

home = Path.home()
repo = Path(__file__).resolve().parents[1]
label = "space.sublayer.wortling-monitor"
logs = home / "Library/Logs/wortling"
logs.mkdir(parents=True, exist_ok=True)
target = home / "Library/LaunchAgents" / (label + ".plist")
target.write_bytes(
    plistlib.dumps(
        {
            "Label": label,
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
)
domain = f"gui/{os.getuid()}"
subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
print("Installed", label, "(every 300 seconds)")
