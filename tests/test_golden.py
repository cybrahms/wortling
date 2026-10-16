import json
from pathlib import Path

import httpx
import pytest
import yaml

from wortling.lemma import lemma_key
from wortling.pipeline.schemas import Extracted

ROOT = Path(__file__).parent / "golden"
CASES = yaml.safe_load((ROOT / "sentences.yaml").read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_recorded_extraction(case, settings):
    record = json.loads((ROOT / (case["id"] + ".json")).read_text())
    result = Extracted.model_validate(record["response"])
    actual = {lemma_key(item.lemma) for item in result.candidates}
    for expected in case["expected"]:
        assert actual.intersection(lemma_key(option) for option in expected.split("|")), (
            expected,
            actual,
        )
    for item in result.candidates:
        assert item.sentence_id == 0
        assert item.surface

    # Replay the real recorded extraction through the actual client and pipeline.
    from test_pipeline import capture

    from wortling.db import Database
    from wortling.llm.client import LLMClient
    from wortling.pipeline.engine import Pipeline

    capture_id = capture(settings, case["text"])
    db = Database(settings.data_dir)

    def handler(request):
        body = json.loads(request.content)
        stage = body["response_format"]["json_schema"]["name"]
        source = json.loads(body["messages"][1]["content"][0]["text"])
        if stage == "normalize":
            value = {
                "mostly_german": True,
                "reason": "Recorded German example",
                "source_text": case["text"],
                "sentences": [case["text"]],
            }
        elif stage == "extract":
            value = record["response"]
        elif stage == "judge":
            value = {
                "judgments": [
                    {
                        "item_id": item["item_id"],
                        "keep": False,
                        "reason": "Fixture stops after extraction",
                    }
                    for item in source["items"]
                ]
            }
        else:
            raise AssertionError("Golden extraction replay should not generate cards")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}
        )

    client = LLMClient(
        settings, db, transport=httpx.MockTransport(handler), token_provider=lambda: "test"
    )
    Pipeline(settings, db, client).process(capture_id)
    with db.connect() as conn:
        stored = {row[0] for row in conn.execute("SELECT lemma_key FROM items")}
    for expected in case["expected"]:
        assert stored.intersection(lemma_key(option) for option in expected.split("|"))
