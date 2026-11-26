"""config.py — Configuration schemas and loader for the SLM pipeline.

Validated using Pydantic v2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
import yaml

from pydantic import BaseModel, ConfigDict, Field


class LlamaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:8080"
    connect_timeout_ms: float = 500.0
    request_timeout_ms: float = 5000.0
    n_probs: int = 10
    cache_prompt: bool = True
    seed: int = 0


class ThresholdGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_p: float
    min_margin: float


class ThresholdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_label_mass: float = 0.50
    none_clarify_p2: float = 0.30
    clarify_p2: float = 0.15
    deenergize: ThresholdGroup = Field(default_factory=lambda: ThresholdGroup(min_p=0.50, min_margin=0.20))
    energize: ThresholdGroup = Field(default_factory=lambda: ThresholdGroup(min_p=0.80, min_margin=0.50))
    info: ThresholdGroup = Field(default_factory=lambda: ThresholdGroup(min_p=0.50, min_margin=0.20))


class SlotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_mms: int | None = None
    max_mms: int | None = None
    rated_mms: int | None = None
    require_unit: bool = True


class TeeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_ttl_ms: int = 3000
    follow_ms: int = 15000


class BreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = 3
    open_s_initial: float = 5.0
    open_s_max: float = 60.0
    canary_interval_s: float = 30.0
    canary_timeout_ms: float = 3000.0
    degraded_latency_ms: float = 4000.0


class CandidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ttl_days: int = 14
    min_confirmations: int = 3


class ExplainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    deadline_ms: float = 8000.0
    max_tokens: int = 96
    snippet_chars: int = 200


class SlmConfig(BaseModel):
    """Main SLM pipeline configuration."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_total_ms: float = 6000.0
    ack_after_ms: float = 1200.0
    rate_limit_per_min: int = 12
    unavailable_speak_cooldown_s: float = 60.0
    decode_mode: Literal["label_logprob", "grammar_json"] = "label_logprob"
    llama: LlamaConfig = Field(default_factory=LlamaConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    speed: SlotConfig = Field(default_factory=SlotConfig)
    tee: TeeConfig = Field(default_factory=TeeConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    candidates: CandidateConfig = Field(default_factory=CandidateConfig)
    explain: ExplainConfig = Field(default_factory=ExplainConfig)

    @classmethod
    def from_yaml(cls, path_or_str: str | Path) -> SlmConfig:
        if isinstance(path_or_str, (str, Path)) and Path(path_or_str).is_file():
            with open(path_or_str, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        else:
            data = yaml.safe_load(str(path_or_str))

        if isinstance(data, dict) and "slm" in data:
            data = data["slm"]

        return cls.model_validate(data or {})

    @classmethod
    def default(cls) -> SlmConfig:
        return cls()
