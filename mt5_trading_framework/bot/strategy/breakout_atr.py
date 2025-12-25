from __future__ import annotations

from typing import Dict

import pandas as pd

from bot.indicators import add_atr, add_donchian
from bot.strategy.base import SignalResult, StrategyBase
from bot.utils import Signal


class BreakoutATR(StrategyBase):
    def required_timeframes(self) -> Dict[str, str]:
        return {"signal": "M15", "trend": "H1"}

    def compute_indicators(self, df_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        signal_df = df_map["signal"].copy()
        add_atr(signal_df, int(self.params["atr_period"]))
        add_donchian(signal_df, int(self.params["breakout_lookback"]))
        df_map["signal"] = signal_df
        return df_map

    def generate_signal(self, df_map: Dict[str, pd.DataFrame]) -> SignalResult:
        df = df_map["signal"]
        if len(df) < int(self.params["breakout_lookback"]) + 5:
            return SignalResult(Signal.NONE, 0.0, {"reason": "insufficient_data"})
        bar = df.iloc[-2]
        atr = float(bar["atr"])
        if atr < float(self.params["atr_filter_min"]):
            return SignalResult(Signal.NONE, 0.0, {"reason": "atr_filter"})

        signal = Signal.NONE
        if bar["close"] > bar["donchian_high"]:
            signal = Signal.BUY
        elif bar["close"] < bar["donchian_low"]:
            signal = Signal.SELL
        confidence = 0.6 if signal != Signal.NONE else 0.0
        return SignalResult(signal, confidence, {"atr": atr, "donchian_high": float(bar["donchian_high"]), "donchian_low": float(bar["donchian_low"])})

    def initial_sl_tp(self, signal: Signal, atr: float, price: float) -> tuple[float, float]:
        sl_mult = float(self.params.get("atr_sl_mult", 1.8))
        tp_mult = float(self.params.get("atr_tp_mult", 2.4))
        sl = price - atr * sl_mult if signal == Signal.BUY else price + atr * sl_mult
        tp = price + atr * tp_mult if signal == Signal.BUY else price - atr * tp_mult
        return sl, tp
