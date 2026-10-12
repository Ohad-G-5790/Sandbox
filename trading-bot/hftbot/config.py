"""One config object for the whole bot, loadable from TOML (see config.example.toml)."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hftbot.execution import CostModel
from hftbot.features import FeatureConfig
from hftbot.loop import LoopConfig
from hftbot.policy import PolicyConfig
from hftbot.pricing import PricingConfig
from hftbot.risk import RiskLimits


@dataclass(frozen=True)
class BotConfig:
    starting_cash: float = 10_000.0
    activation_delay_blocks: int = 1
    markout_blocks: int = 20
    """How far after a fill to measure adverse selection."""
    jev_model: str | None = None
    jev_pinned_model: str | None = None
    features: FeatureConfig = field(default_factory=FeatureConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    costs: CostModel = field(default_factory=CostModel)
    loop: LoopConfig = field(default_factory=LoopConfig)


_SECTIONS = {
    "features": FeatureConfig,
    "policy": PolicyConfig,
    "pricing": PricingConfig,
    "risk": RiskLimits,
    "costs": CostModel,
    "loop": LoopConfig,
}


def load_config(path: str | Path) -> BotConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return config_from_dict(data)


def config_from_dict(data: dict[str, Any]) -> BotConfig:
    top = dict(data.get("bot", {}))
    unknown = set(data) - set(_SECTIONS) - {"bot"}
    if unknown:
        raise ValueError(f"unknown config sections: {sorted(unknown)}")
    for name, cls in _SECTIONS.items():
        section = dict(data.get(name, {}))
        fields = {f.name for f in dataclasses.fields(cls)}
        bad = set(section) - fields
        if bad:
            raise ValueError(f"unknown keys in [{name}]: {sorted(bad)}")
        for key, value in section.items():
            if isinstance(value, list):
                section[key] = tuple(value)
        top[name] = cls(**section)
    return BotConfig(**top)
