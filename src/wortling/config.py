from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(StrictModel):
    cefr: str | None = None
    l1: str = "English"
    notes: str = ""
    priority_domains: list[str] = Field(default_factory=list)


class KnownDeck(StrictModel):
    deck: str
    lemma_field: str
    rank_field: str | None = None


class Learner(StrictModel):
    id: str
    display_name: str
    timezone: str = "UTC"
    capture_token_env: str
    ankiweb_user_env: str
    ankiweb_pass_env: str
    target_deck: str = "Wortling"
    promote_new_cards: bool = True
    max_adds_per_run: int = Field(default=30, ge=1, le=1000)
    processing_enabled: bool = False
    dry_run_approved: bool = False
    writes_enabled: bool = False
    real_ankiweb_approved: bool = False
    profile: Profile = Field(default_factory=Profile)
    known_decks: list[KnownDeck] = Field(default_factory=list)
    monthly_budget_usd: float = Field(default=10, gt=0)

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
            raise ValueError("id must be a safe lowercase identifier")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("capture_token_env", "ankiweb_user_env", "ankiweb_pass_env")
    @classmethod
    def env_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            raise ValueError("expected an environment variable name")
        return value

    @model_validator(mode="after")
    def enabled_profile(self) -> Learner:
        if self.processing_enabled and not self.profile.cefr:
            raise ValueError("processing requires a learner CEFR profile")
        if self.writes_enabled and not self.dry_run_approved:
            raise ValueError("writes require human-approved dry-run output")
        if self.writes_enabled and not self.processing_enabled:
            raise ValueError("writes require processing_enabled")
        return self


class LLMSettings(StrictModel):
    base_url: str = "https://hyper.charm.land/v1"
    api_key_env: str | None = None
    models: dict[str, str] = Field(
        default_factory=lambda: {
            "normalize": "qwen3.8-flash",
            "extract": "qwen3.8-flash",
            "judge": "qwen3.8-max",
            "generate": "qwen3.8-max",
        }
    )
    prompt_versions: dict[str, str] = Field(
        default_factory=lambda: {
            stage: ({"normalize": "v6", "extract": "v6", "judge": "v3", "generate": "v2"}[stage])
            for stage in ("normalize", "extract", "judge", "generate")
        }
    )
    max_output_tokens: int = Field(default=6000, ge=256, le=16000)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    cost_per_million: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "qwen3.8-flash": (0.15, 0.47),
            "qwen3.8-max": (2, 6),
        }
    )

    @model_validator(mode="after")
    def complete_stages(self) -> LLMSettings:
        stages = {"normalize", "extract", "judge", "generate"}
        if set(self.models) != stages or set(self.prompt_versions) != stages:
            raise ValueError("all four LLM stages require model and prompt versions")
        for version in self.prompt_versions.values():
            if not re.fullmatch(r"v[0-9]+", version):
                raise ValueError("prompt versions must be v followed by digits")
        return self


class Settings(StrictModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    data_dir: Path = Path("data")
    users: list[Learner] = Field(min_length=1)
    capture_limit_per_minute: int = Field(default=30, ge=1)
    max_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    anki_sync_endpoint: str | None = "http://127.0.0.1:8082/"
    worker_poll_seconds: float = Field(default=5, ge=0.1)
    write_interval_hours: float = Field(default=4, gt=0)

    @model_validator(mode="after")
    def unique_users(self) -> Settings:
        for attribute in ("id", "capture_token_env"):
            values = [getattr(user, attribute) for user in self.users]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate user {attribute}")
        return self

    def learner(self, user_id: str) -> Learner:
        return next(user for user in self.users if user.id == user_id)


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("WORTLING_CONFIG", "config.yaml"))
    settings = Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if override := os.environ.get("WORTLING_DATA_DIR"):
        settings.data_dir = Path(override)
    return settings
