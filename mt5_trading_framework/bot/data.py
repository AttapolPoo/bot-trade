from __future__ import annotations

from typing import Dict

import pandas as pd

from bot.utils import AppConfig


class MarketData:
    def __init__(self, mt5_client, config: AppConfig) -> None:
        self.mt5_client = mt5_client
        self.config = config

    def _fetch(self, symbol: str, timeframe: str) -> pd.DataFrame:
        raw = self.mt5_client.get_rates(symbol, timeframe, self.config.data.lookback)
        df = pd.DataFrame(raw)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df

    def get(self, symbol: str, required_tfs: Dict[str, str]) -> Dict[str, pd.DataFrame]:
        return {tf: self._fetch(symbol, tf) for tf in required_tfs.values()}
