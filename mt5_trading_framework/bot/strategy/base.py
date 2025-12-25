from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from bot.utils import Signal


@dataclass
class SignalResult:
    signal: Signal
    confidence: float
    reason: Dict[str, float | str]


class StrategyBase(ABC):
    def __init__(self, params: Dict[str, float | int | str], logger) -> None:
        self.params = params
        self.logger = logger

    @abstractmethod
    def required_timeframes(self) -> Dict[str, str]:
        """Return mapping of logical name to timeframe string (e.g., {"signal": "M5", "trend": "H1"})."""

    @abstractmethod
    def compute_indicators(self, df_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """Return df_map with indicators added."""

    @abstractmethod
    def generate_signal(self, df_map: Dict[str, pd.DataFrame]) -> SignalResult:
        """Generate trade signal from prepared data."""

    @abstractmethod
    def initial_sl_tp(self, signal: Signal, atr: float, price: float) -> tuple[float, float]:
        """Return (sl, tp) price levels for the signal."""

    def manage_open_position(self, position, df_map: Dict[str, pd.DataFrame]) -> Optional[Dict[str, float]]:
        """Optional hook to adjust SL/TP or exit; return modifications."""
        return None
