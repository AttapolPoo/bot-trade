from __future__ import annotations

from typing import Dict

import pandas as pd

from bot.indicators import add_atr, add_bbands, add_rsi
from bot.strategy.base import SignalResult, StrategyBase
from bot.utils import Signal


class MeanReversionBBands(StrategyBase):
    def required_timeframes(self) -> Dict[str, str]:
        return {"signal": "H1", "trend": "H4"}

    def compute_indicators(self, df_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        signal_df = df_map["signal"].copy()
        add_bbands(signal_df, int(self.params["bbands_period"]), float(self.params["bbands_std"]))
        add_rsi(signal_df, int(self.params["rsi_period"]))
        add_atr(signal_df, int(self.params["atr_period"]))
        df_map["signal"] = signal_df
        return df_map

    def generate_signal(self, df_map: Dict[str, pd.DataFrame]) -> SignalResult:
        df = df_map["signal"]
        if len(df) < int(self.params["bbands_period"]) + 5:
            return SignalResult(Signal.NONE, 0.0, {"reason": "insufficient_data"})
        bar = df.iloc[-2]
        atr = float(bar["atr"])
        if atr > float(self.params["atr_filter_max"]):
            return SignalResult(Signal.NONE, 0.0, {"reason": "high_vol"})

        signal = Signal.NONE
        if bar["close"] < bar["bb_low"] and bar["rsi"] < float(self.params["rsi_low"]):
            signal = Signal.BUY
        elif bar["close"] > bar["bb_high"] and bar["rsi"] > float(self.params["rsi_high"]):
            signal = Signal.SELL

        confidence = 0.55 if signal != Signal.NONE else 0.0
        return SignalResult(signal, confidence, {"atr": atr, "rsi": float(bar["rsi"]), "bb_high": float(bar["bb_high"]), "bb_low": float(bar["bb_low"])})

    def initial_sl_tp(self, signal: Signal, atr: float, price: float) -> tuple[float, float]:
        sl_mult = float(self.params.get("atr_sl_mult", 2.0))
        tp_mult = float(self.params.get("atr_tp_mult", 2.5))
        sl = price - atr * sl_mult if signal == Signal.BUY else price + atr * sl_mult
        tp = price + atr * tp_mult if signal == Signal.BUY else price - atr * tp_mult
        return sl, tp
