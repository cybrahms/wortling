import json

import httpx
import pytest

from wortling.db import Database
from wortling.llm.client import LLMClient
from wortling.pipeline.schemas import Normalized


@pytest.mark.parametrize("effort", [None, "none", "low"])
def test_reasoning_effort_is_explicit_only_when_configured(settings, effort):
    settings.llm.reasoning_effort = effort
    db = Database(settings.data_dir)
    db.initialize(settings)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "mostly_german": True,
                                    "reason": "German",
                                    "source_text": "",
                                    "sentences": ["Guten Tag."],
                                }
                            )
                        },
                    }
                ]
            },
        )

    client = LLMClient(
        settings, db, transport=httpx.MockTransport(respond), token_provider=lambda: "test"
    )
    client.call("normalize", Normalized, {"text": "Guten Tag."}, settings.users[0], None)
    if effort is None:
        assert "reasoning_effort" not in requests[0]
    else:
        assert requests[0]["reasoning_effort"] == effort


@pytest.mark.parametrize(
    "content", ["", '{"mostly_german":true,"reason":"ok","source_text":"","sentences":[]}']
)
def test_length_finish_is_reported_without_schema_retry(settings, content):
    db = Database(settings.data_dir)
    db.initialize(settings)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": content}}],
                "usage": {"completion_tokens": 6000},
            },
        )

    client = LLMClient(
        settings, db, transport=httpx.MockTransport(respond), token_provider=lambda: "test"
    )
    with pytest.raises(ValueError, match="output truncated.*reasoning_effort"):
        client.call("normalize", Normalized, {}, settings.users[0], None)
    assert len(requests) == 1
    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT outcome,tokens_out FROM llm_calls")] == [
            ("output_truncated", 6000)
        ]
