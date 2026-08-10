"""Pydantic models for whetstone.yaml."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Tier(StrEnum):
    quick = "quick"
    standard = "standard"
    deep = "deep"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForgeConfig(_Strict):
    kind: str = "github"
    repo: str | None = None
    base_branch: str = "main"


class ProjectConfig(_Strict):
    name: str
    forge: ForgeConfig = Field(default_factory=ForgeConfig)


class CommandsConfig(_Strict):
    install: str | None = None
    test: str | None = None
    lint: str | None = None
    build: str | None = None
    dev: str | None = None


class AppConfig(_Strict):
    """Only needed by browser lenses (M2). Modelled now so configs validate."""

    url: str | None = None
    ready_when: dict[str, Any] = Field(default_factory=dict)
    viewports: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    auth: dict[str, Any] | None = None


class EnvironmentConfig(_Strict):
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    app: AppConfig = Field(default_factory=AppConfig)


class BoundariesConfig(_Strict):
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=list)
    # never_touch is a WRITE barrier. Files listed here are still analysed and
    # still produce findings; Whetstone may never edit them.
    never_touch: list[str] = Field(default_factory=list)


class ContextConfig(_Strict):
    decisions: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)


class ModelConfig(_Strict):
    """Unused in M0 — no model calls exist yet."""

    provider: str | None = None
    model: str | None = None
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CeilingConfig(_Strict):
    usd_per_run: float | None = None
    calls_per_day: int | None = None


class BudgetConfig(_Strict):
    tier: Tier = Tier.quick
    ceiling: CeilingConfig = Field(default_factory=CeilingConfig)
    on_ceiling: str = "stop_and_report"


class LensConfig(_Strict):
    enabled: bool = True
    autonomy: int = 0
    trust: str | None = None
    only: list[str] | None = None
    severity_floor: str | None = None

    @field_validator("autonomy")
    @classmethod
    def _bounded(cls, value: int) -> int:
        if not 0 <= value <= 3:
            raise ValueError(
                "autonomy must be between 0 and 3; there is no level 4 "
                "because Whetstone never merges"
            )
        return value


class SinkConfig(_Strict):
    """Sinks will grow adapter-specific keys (Jira field IDs, GitHub labels).
    Those get modelled per adapter when the adapter lands. Until then a typo
    in a sink entry must fail, not be silently accepted as a future field.
    """

    kind: str


class WhetstoneConfig(_Strict):
    version: int = 1
    project: ProjectConfig
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    boundaries: BoundariesConfig = Field(default_factory=BoundariesConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    lenses: dict[str, LensConfig] = Field(default_factory=dict)
    sinks: list[SinkConfig] = Field(
        default_factory=lambda: [SinkConfig(kind="dashboard")]
    )
    state_dir: str | None = None
