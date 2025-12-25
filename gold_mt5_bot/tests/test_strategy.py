import pandas as pd

from gold_mt5_bot.bot.strategy import Strategy
from gold_mt5_bot.bot.utils import Signal, StrategyConfig


def _make_df(close_values):
    idx = pd.date_range("2024-01-01", periods=len(close_values), freq="5T", tz="UTC")
    return pd.DataFrame(
        {
            "open": close_values,
            "high": [c + 0.5 for c in close_values],
            "low": [c - 0.5 for c in close_values],
            "close": close_values,
        },
        index=idx,
    )


def test_strategy_bias_and_signal():
    cfg = StrategyConfig(
        ema_fast=3,
        ema_slow=5,
        ema_trend_fast=3,
        ema_trend_slow=5,
        rsi_period=3,
        rsi_buy=45,
        rsi_sell=55,
        atr_period=3,
        atr_silence_threshold=0.01,
        spread_limit_points=100,
        max_candle_spread_ratio=3.0,
        news_blackout={"enabled": False, "windows": []},
        max_signal_per_bar=1,
        cooldown_minutes=0,
    )
    strat = Strategy(cfg, logger=None)
    trend_df = _make_df([1, 2, 3, 4, 5, 6, 7])
    signal_df = _make_df([1, 2, 3, 4, 5, 6, 7])
    trigger_df = _make_df([1, 2, 3, 4, 5, 6, 7, 8])
    data = {"H1": trend_df, "M15": signal_df, "M5": trigger_df}
    signal, reason = strat.generate_signal("XAUUSD", data, spread_points=0.1)
    assert signal in {Signal.BUY, Signal.NONE}
