from __future__ import annotations

from typing import Dict

import pandas as pd

from bot.indicators import add_atr, add_ema, add_rsi, candle_body_size
from bot.strategy.base import SignalResult, StrategyBase
from bot.utils import Signal


class EmaRsiPullback(StrategyBase):
    def required_timeframes(self) -> Dict[str, str]:
        return {"signal": "M5", "trend": "H1"}

    def compute_indicators(self, df_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        signal_df = df_map["signal"].copy()
        trend_df = df_map["trend"].copy()
        add_ema(signal_df, int(self.params["ema_fast"]))
        add_ema(signal_df, int(self.params["ema_slow"]))
        add_rsi(signal_df, int(self.params["rsi_period"]))
        add_atr(signal_df, int(self.params["atr_period"]))
        add_ema(trend_df, int(self.params["ema_trend_fast"]), name="ema_trend_fast")
        add_ema(trend_df, int(self.params["ema_trend_slow"]), name="ema_trend_slow")
        df_map["signal"] = signal_df
        df_map["trend"] = trend_df
        return df_map

    def _trend_bias(self, trend_df: pd.DataFrame) -> Signal:
        ref = trend_df.iloc[-2]
        if ref["ema_trend_fast"] > ref["ema_trend_slow"]:
            return Signal.BUY
        if ref["ema_trend_fast"] < ref["ema_trend_slow"]:
            return Signal.SELL
        return Signal.NONE

    def generate_signal(self, df_map: Dict[str, pd.DataFrame]) -> SignalResult:
        signal_df = df_map["signal"]
        trend_df = df_map["trend"]
        if len(signal_df) < max(int(self.params["ema_slow"]), 30):
            return SignalResult(Signal.NONE, 0.0, {"reason": "insufficient_data"})
        bias = self._trend_bias(trend_df)
        if bias == Signal.NONE:
            return SignalResult(Signal.NONE, 0.0, {"reason": "no_trend"})
        bar = signal_df.iloc[-2]
        prev = signal_df.iloc[-3]
        atr = float(bar["atr"])
        if atr < float(self.params["atr_silence_threshold"]):
            return SignalResult(Signal.NONE, 0.0, {"reason": "low_vol"})
        body = candle_body_size(signal_df).iloc[-2]
        if body > atr * float(self.params["max_candle_body_atr"]):
            return SignalResult(Signal.NONE, 0.0, {"reason": "wide_candle"})

        signal = Signal.NONE
        if bias == Signal.BUY:
            if prev["rsi"] < float(self.params["rsi_buy"]) and bar["rsi"] > float(self.params["rsi_buy"]) and bar["close"] > bar["ema_fast"] > bar["ema_slow"]:
                signal = Signal.BUY
        elif bias == Signal.SELL:
            if prev["rsi"] > float(self.params["rsi_sell"]) and bar["rsi"] < float(self.params["rsi_sell"]) and bar["close"] < bar["ema_fast"] < bar["ema_slow"]:
                signal = Signal.SELL

        confidence = 0.7 if signal != Signal.NONE else 0.0
        return SignalResult(signal, confidence, {"bias": bias.value, "atr": atr, "rsi": float(bar["rsi"])})

    def initial_sl_tp(self, signal: Signal, atr: float, price: float) -> tuple[float, float]:
        sl_mult = float(self.params.get("atr_sl_mult", 1.8))
        tp_mult = float(self.params.get("atr_tp_mult", 2.4))
        sl = price - atr * sl_mult if signal == Signal.BUY else price + atr * sl_mult
        tp = price + atr * tp_mult if signal == Signal.BUY else price - atr * tp_mult
        return sl, tp
