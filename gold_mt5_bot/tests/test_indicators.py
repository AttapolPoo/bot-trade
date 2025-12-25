import pandas as pd

from gold_mt5_bot.bot.indicators import add_atr, add_ema, add_rsi


def test_indicators_shape():
    data = {
        "open": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "high": [1.1, 2.2, 3.3, 4.4, 5.5, 6.6, 7.7, 8.8, 9.9, 11],
        "low": [0.9, 1.8, 2.7, 3.6, 4.5, 5.4, 6.3, 7.2, 8.1, 9],
        "close": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    }
    df = pd.DataFrame(data)
    add_ema(df, 3)
    add_rsi(df, 3)
    add_atr(df, 3)
    assert "ema_3" in df.columns
    assert "rsi" in df.columns
    assert "atr" in df.columns
    assert not df[["ema_3", "rsi", "atr"]].isna().all().any()
