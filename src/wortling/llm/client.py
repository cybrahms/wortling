from __future__ import annotations

import json
import os
from importlib.resources import files
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from wortling.config import Learner, Settings
from wortling.db import Database, now
from wortling.llm.hyper import hyper_token

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(self, settings: Settings, db: Database, *, transport=None, token_provider=None):
        self.settings = settings
        self.db = db
        self.transport = transport
        self.token_provider = token_provider or self.configured_token

    def configured_token(self) -> str:
        variable = self.settings.llm.api_key_env
        if variable:
            if key := os.environ.get(variable):
                return key
            raise RuntimeError(f"Missing model provider credential: {variable}")
        return hyper_token()

    def call(
        self,
        stage: str,
        schema: type[T],
        data: dict,
        user: Learner,
        run_id: str,
        *,
        image: tuple[str, str] | None = None,
        validator=None,
    ) -> T:
        config = self.settings.llm
        version = config.prompt_versions[stage]
        prompt = files("wortling").joinpath("prompts", f"{stage}.{version}.md").read_text()
        model = config.models[stage]
        content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
        if image:
            mime, encoded = image
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
        prompt += "\n\nRequired JSON schema (all fields and names are exact):\n"
        prompt += json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": content}]
        for attempt in range(2):
            body = {
                "model": model,
                "messages": messages,
                "max_tokens": config.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": stage,
                        "strict": True,
                        "schema": schema.model_json_schema(),
                    },
                },
            }
            try:
                with httpx.Client(transport=self.transport, timeout=120) as client:
                    response = client.post(
                        config.base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + self.token_provider()},
                        json=body,
                    )
            except httpx.HTTPError as exc:
                self.log(user, run_id, stage, model, version, {}, "transport_error")
                raise RuntimeError(f"LLM {stage} request failed: {type(exc).__name__}") from exc
            if not response.is_success:
                self.log(user, run_id, stage, model, version, {}, "http_error")
                raise RuntimeError(f"LLM {stage} call failed (HTTP {response.status_code})")
            result = response.json()
            usage = result.get("usage", {})
            output = ""
            try:
                output = result["choices"][0]["message"]["content"]
                parsed = schema.model_validate_json(output)
                if validator is not None:
                    validator(parsed)
            except (ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
                self.log(user, run_id, stage, model, version, usage, "schema_failure")
                if attempt:
                    raise ValueError(f"Invalid structured output after retry for {stage}") from exc
                messages.append({"role": "assistant", "content": output or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "Your output failed schema validation. "
                        "Return only JSON matching the supplied schema. " + str(exc)[:1200],
                    }
                )
            else:
                self.log(user, run_id, stage, model, version, usage, "ok")
                return parsed
        raise AssertionError("retry loop exhausted")

    def log(self, user, run_id, stage, model, version, usage, outcome):
        rate = self.settings.llm.cost_per_million.get(model, (0.0, 0.0))
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        reported = usage.get("cost", {})
        cost = reported.get("usd") if isinstance(reported, dict) else None
        if cost is None:
            cost = (tokens_in * rate[0] + tokens_out * rate[1]) / 1_000_000
        timestamp = now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO llm_calls(user_id,run_id,stage,model,prompt_version,tokens_in,"
                "tokens_out,cost_usd,outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    user.id,
                    run_id,
                    stage,
                    model,
                    version,
                    tokens_in,
                    tokens_out,
                    cost,
                    outcome,
                    timestamp,
                ),
            )
            month = timestamp[:7]
            spend = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE user_id=? AND created_at>=?",
                (user.id, month),
            ).fetchone()[0]
            alerted = conn.execute(
                "SELECT 1 FROM alerts WHERE user_id=? AND kind='monthly_budget' AND created_at>=?",
                (user.id, month),
            ).fetchone()
            if spend >= user.monthly_budget_usd and not alerted:
                conn.execute(
                    "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
                    (
                        user.id,
                        "monthly_budget",
                        f"Monthly LLM spend reached ${spend:.2f}",
                        timestamp,
                    ),
                )
