from __future__ import annotations

from datetime import datetime
from typing import Dict, Tuple

import pandas as pd

from gold_mt5_bot.bot.indicators import add_atr, add_ema, add_rsi, candle_body_size
from gold_mt5_bot.bot.utils import Signal, StrategyConfig, candle_width_ok


class Strategy:
    def __init__(self, config: StrategyConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.last_signal_time: Dict[str, datetime] = {}

    def _prepare(self, data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        prepared: Dict[str, pd.DataFrame] = {}
        for tf, df in data.items():
            df = df.copy()
            add_ema(df, self.config.ema_fast)
            add_ema(df, self.config.ema_slow)
            add_rsi(df, self.config.rsi_period)
            add_atr(df, self.config.atr_period)
            prepared[tf] = df
        return prepared

    def _trend_bias(self, trend_df: pd.DataFrame) -> Signal:
        add_ema(trend_df, self.config.ema_trend_fast, name="ema_trend_fast")
        add_ema(trend_df, self.config.ema_trend_slow, name="ema_trend_slow")
        ref = trend_df.iloc[-2]
        if ref["ema_trend_fast"] > ref["ema_trend_slow"]:
            return Signal.BUY
        if ref["ema_trend_fast"] < ref["ema_trend_slow"]:
            return Signal.SELL
        return Signal.NONE

    def generate_signal(
        self, symbol: str, raw_data: Dict[str, pd.DataFrame], spread_points: float
    ) -> Tuple[Signal, Dict[str, float | str]]:
        data = self._prepare(raw_data)
        trend_tf, signal_tf, trigger_tf = data.keys()
        trend_df = data[trend_tf]
        signal_df = data[signal_tf]
        trigger_df = data[trigger_tf]

        for df_name, df in [("trend", trend_df), ("signal", signal_df), ("trigger", trigger_df)]:
            if len(df) < max(self.config.ema_slow, self.config.ema_trend_slow) + 5:
                return Signal.NONE, {"reason": f"not_enough_data_{df_name}"}

        if spread_points > self.config.spread_limit_points:
            return Signal.NONE, {"reason": "spread_guard"}

        bias = self._trend_bias(trend_df)
        if bias == Signal.NONE:
            return Signal.NONE, {"reason": "no_trend_bias"}

        signal_bar = signal_df.iloc[-2]
        trigger_bar = trigger_df.iloc[-2]
        trigger_prev = trigger_df.iloc[-3]

        if not candle_width_ok(candle_body_size(trigger_df).iloc[-2], trigger_bar["atr"], self.config.max_candle_spread_ratio):
            return Signal.NONE, {"reason": "wide_candle"}

        if trigger_bar["atr"] < self.config.atr_silence_threshold:
            return Signal.NONE, {"reason": "low_volatility"}

        if self.last_signal_time.get(trigger_tf) == trigger_bar.name:
            return Signal.NONE, {"reason": "one_signal_per_bar"}

        signal = Signal.NONE
        if bias == Signal.BUY:
            if (
                trigger_prev["rsi"] < self.config.rsi_buy
                and trigger_bar["rsi"] > self.config.rsi_buy
                and trigger_bar["close"] > trigger_bar["ema_fast"]
                and trigger_bar["close"] > trigger_bar["ema_slow"]
            ):
                signal = Signal.BUY
        elif bias == Signal.SELL:
            if (
                trigger_prev["rsi"] > self.config.rsi_sell
                and trigger_bar["rsi"] < self.config.rsi_sell
                and trigger_bar["close"] < trigger_bar["ema_fast"]
                and trigger_bar["close"] < trigger_bar["ema_slow"]
            ):
                signal = Signal.SELL

        if signal != Signal.NONE:
            self.last_signal_time[trigger_tf] = trigger_bar.name
            return signal, {
                "bias": bias.value,
                "rsi": float(trigger_bar["rsi"]),
                "atr": float(trigger_bar["atr"]),
                "spread": spread_points,
            }
        return Signal.NONE, {"reason": "no_trigger", "bias": bias.value}
