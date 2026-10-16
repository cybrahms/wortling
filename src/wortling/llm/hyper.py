from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key
from filelock import FileLock


def hyper_token() -> str:
    if key := os.environ.get("HYPER_API_KEY"):
        return key
    secret_path = os.environ.get("WORTLING_HYPER_ENV_FILE")
    if not secret_path:
        raise RuntimeError("Set HYPER_API_KEY or WORTLING_HYPER_ENV_FILE for Hyper authentication")
    path = Path(secret_path)
    with FileLock(str(path) + ".lock", timeout=45):
        # This file is a dedicated Wortling env file, never Crush's rotating tokens.
        for key, value in dotenv_values(path).items():
            if value is not None:
                os.environ[key] = value
        if float(os.environ.get("HYPER_EXPIRES_AT", "0")) > time.time() + 120:
            return os.environ["HYPER_ACCESS_TOKEN"]
        refresh_token = os.environ.get("HYPER_REFRESH_TOKEN")
        if not refresh_token:
            raise RuntimeError("Wortling Hyper login required")
        response = httpx.post(
            "https://hyper.charm.land/token/exchange",
            json={"refresh_token": refresh_token},
            headers={"User-Agent": "wortling"},
            timeout=30,
        )
        if not response.is_success:
            raise RuntimeError(f"Hyper token refresh failed (HTTP {response.status_code})")
        data = response.json()
        values = {
            "HYPER_ACCESS_TOKEN": data["access_token"],
            "HYPER_REFRESH_TOKEN": data.get("refresh_token", refresh_token),
            "HYPER_EXPIRES_AT": str(int(time.time()) + data["expires_in"]),
        }
        for key, value in values.items():
            set_key(path, key, value)
            os.environ[key] = value
        path.chmod(0o600)
        return values["HYPER_ACCESS_TOKEN"]
