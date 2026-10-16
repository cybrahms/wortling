"""Opt-in live extraction recording; every model call still uses the shared client."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from wortling.config import load_settings
from wortling.db import Database, now
from wortling.llm.client import LLMClient
from wortling.pipeline.schemas import Extracted

root = Path(__file__).resolve().parents[1]
settings = load_settings(root / "config.example.yaml")
settings.data_dir = root / "data" / "golden-recording"
db = Database(settings.data_dir)
db.initialize(settings)
user = settings.users[0]
records = yaml.safe_load((root / "tests/golden/sentences.yaml").read_text())
selected = set(os.environ.get("GOLDEN_IDS", "").split(",")) - {""}


def record(case):
    target = root / "tests/golden" / (case["id"] + ".json")
    if selected and case["id"] not in selected:
        return
    capture_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO captures(id,user_id,received_at,text,client_ts,payload_hash) "
            "VALUES (?,?,?,?,?,?)",
            (capture_id, user.id, now(), case["text"], now(), "fixture"),
        )
        conn.execute(
            "INSERT INTO processing_runs(id,capture_id,prompt_version,status,started_at) "
            "VALUES (?,?,?,'recording',?)",
            (run_id, capture_id, "extract.v1", now()),
        )
    result = LLMClient(settings, db).call(
        "extract", Extracted, {"sentences": [case["text"]]}, user, run_id
    )
    target.write_text(
        json.dumps(
            {
                "model": settings.llm.models["extract"],
                "prompt_version": settings.llm.prompt_versions["extract"],
                "recorded_at": now(),
                "response": result.model_dump(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(case["id"], flush=True)


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, records))
