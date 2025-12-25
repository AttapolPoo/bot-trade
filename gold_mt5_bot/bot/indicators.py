from __future__ import annotations

import pandas as pd
import ta


def add_ema(df: pd.DataFrame, period: int, column: str = "close", name: str | None = None) -> pd.Series:
    name = name or f"ema_{period}"
    df[name] = ta.trend.EMAIndicator(close=df[column], window=period).ema_indicator()
    return df[name]


def add_rsi(df: pd.DataFrame, period: int, column: str = "close", name: str = "rsi") -> pd.Series:
    df[name] = ta.momentum.RSIIndicator(close=df[column], window=period).rsi()
    return df[name]


def add_atr(df: pd.DataFrame, period: int, name: str = "atr") -> pd.Series:
    df[name] = ta.volatility.AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=period).average_true_range()
    return df[name]


def candle_body_size(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()
