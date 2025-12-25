from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from bot.utils import AppConfig, Profile, Timeframes


@dataclass
class SelectedProfile:
    name: str
    profile: Profile


def list_profiles(config: AppConfig) -> List[str]:
    return list(config.profiles.keys())


def get_profile(config: AppConfig, name: str) -> SelectedProfile:
    if name not in config.profiles:
        raise ValueError(f"Profile not found: {name}")
    return SelectedProfile(name=name, profile=config.profiles[name])


def build_custom_profile(
    symbols: List[str], signal_tf: str, trend_tf: str, strategy: str, base_profile: Profile
) -> SelectedProfile:
    profile = Profile(
        symbols=symbols,
        timeframes=Timeframes(signal_tf=signal_tf, trend_tf=trend_tf),
        strategy=strategy,
        risk=base_profile.risk,
        guard=base_profile.guard,
        cooldown_seconds=base_profile.cooldown_seconds,
        cooldown_candles=base_profile.cooldown_candles,
    )
    return SelectedProfile(name="custom", profile=profile)


def summarize_profile(sel: SelectedProfile) -> Dict[str, str | int | float | list]:
    p = sel.profile
    return {
        "profile": sel.name,
        "symbols": p.symbols,
        "timeframes": p.timeframes.model_dump(),
        "strategy": p.strategy,
        "risk": p.risk.model_dump(),
        "cooldown_seconds": p.cooldown_seconds,
    }
