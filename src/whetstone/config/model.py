"""Pydantic models for whetstone.yaml."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..severity import Severity


class Tier(StrEnum):
    quick = "quick"
    standard = "standard"
    deep = "deep"


class OnCeiling(StrEnum):
    """One member on purpose. Hitting the ceiling stops the run and reports what
    it skipped; a run that quietly examined half the surface reads as clean and
    is worse than no run. Anything else here would be that. More members land
    when there is a second behaviour that does not silently truncate.
    """

    stop_and_report = "stop_and_report"


class Trust(StrEnum):
    """Autonomy is earned by track record. `assumed` is the one opt-out: it skips
    probation for a lens the user already knows they want.
    """

    assumed = "assumed"


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
    on_ceiling: OnCeiling = OnCeiling.stop_and_report


class LensConfig(_Strict):
    enabled: bool = True
    autonomy: int = 0
    trust: Trust | None = None
    only: list[str] | None = None
    # Typed against the lens vocabulary, not `str`. `severity_floor: HIGH` used
    # to validate here and surface much later as a bare KeyError inside
    # severity_at_least.
    severity_floor: Severity | None = None

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
    # SecretStr rather than `Field(repr=False)`. `state_dir` may be written
    # `${env:...}`, and the loader resolves it to the real value before it lands
    # here, so this attribute holds a plaintext credential whenever someone
    # points state at a path built from one. `repr=False` would cover repr() and
    # nothing else, leaving model_dump(), model_dump_json() and every log line or
    # HTML report built from them exposed -- and serialising the config is
    # precisely what M1 adds. SecretStr masks repr(), str(), and JSON dumps at
    # once, and forces the one consumer to say get_secret_value(), which reads
    # as the marker it is. Whetstone's own default path never passes through
    # here: `state_dir` unset means None.
    state_dir: SecretStr | None = None
