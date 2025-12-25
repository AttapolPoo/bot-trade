from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from bot.strategy.base import StrategyBase
from bot.utils import Profile, Signal, timeframe_to_mt5


@dataclass
class TradeRecord:
    entry_time: datetime
    exit_time: datetime
    side: Signal
    entry: float
    exit: float
    sl: float
    tp: float
    r_multiple: float
    balance_after: float
    reason: Dict[str, float | str]


@dataclass
class BacktestReport:
    trades: List[TradeRecord]
    start_balance: float
    end_balance: float
    win_rate: float
    total_r: float
    max_drawdown: float


class Backtester:
    def __init__(self, mt5_client, strategy: StrategyBase, profile: Profile, logger, initial_balance: float = 10000.0) -> None:
        self.mt5_client = mt5_client
        self.strategy = strategy
        self.profile = profile
        self.logger = logger
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.trades: List[TradeRecord] = []

    def _fetch_range(self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime) -> pd.DataFrame:
        tf = timeframe_to_mt5(timeframe)
        rates = self.mt5_client.copy_rates_range(symbol, tf, date_from, date_to)
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df

    def _prepare_data(self, symbol: str, date_from: datetime, date_to: datetime) -> Dict[str, pd.DataFrame]:
        tf_map = {"signal": self.profile.timeframes.signal_tf, "trend": self.profile.timeframes.trend_tf}
        df_map = {
            "signal": self._fetch_range(symbol, tf_map["signal"], date_from, date_to),
            "trend": self._fetch_range(symbol, tf_map["trend"], date_from, date_to),
        }
        return self.strategy.compute_indicators(df_map)

    def run(self, symbol: str, date_from: datetime, date_to: datetime) -> BacktestReport:
        df_map = self._prepare_data(symbol, date_from, date_to)
        signal_df = df_map["signal"]
        trend_df = df_map["trend"]
        if signal_df.empty or trend_df.empty:
            raise RuntimeError("No data for backtest range")

        open_pos: Optional[TradeRecord] = None
        for idx in range(3, len(signal_df)):
            bar_time = signal_df.index[idx]
            # restrict trend data up to current bar time
            trend_slice = trend_df.loc[:bar_time]
            sig_slice = signal_df.iloc[: idx + 1]
            eval_map = {"signal": sig_slice, "trend": trend_slice}
            signal_result = self.strategy.generate_signal(eval_map)

            if open_pos:
                # evaluate stop/target hit on current bar
                bar = signal_df.iloc[idx]
                hit_sl = bar["low"] <= open_pos.sl if open_pos.side == Signal.BUY else bar["high"] >= open_pos.sl
                hit_tp = bar["high"] >= open_pos.tp if open_pos.side == Signal.BUY else bar["low"] <= open_pos.tp
                exit_price = None
                if hit_sl and hit_tp:
                    # choose worse-case first (conservative)
                    exit_price = open_pos.sl
                elif hit_tp:
                    exit_price = open_pos.tp
                elif hit_sl:
                    exit_price = open_pos.sl

                if exit_price is not None:
                    risk_per_trade = self.profile.risk.risk_per_trade
                    entry_risk = abs(open_pos.entry - open_pos.sl)
                    realized_r = (exit_price - open_pos.entry) / entry_risk if open_pos.side == Signal.BUY else (open_pos.entry - exit_price) / entry_risk
                    pnl = risk_per_trade * self.balance * realized_r
                    self.balance += pnl
                    self.trades.append(
                        TradeRecord(
                            entry_time=open_pos.entry_time,
                            exit_time=bar_time,
                            side=open_pos.side,
                            entry=open_pos.entry,
                            exit=exit_price,
                            sl=open_pos.sl,
                            tp=open_pos.tp,
                            r_multiple=realized_r,
                            balance_after=self.balance,
                            reason=open_pos.reason,
                        )
                    )
                    open_pos = None
                    continue

            if open_pos:
                continue

            if signal_result.signal == Signal.NONE:
                continue

            bar_prev = signal_df.iloc[idx - 1]
            atr = float(bar_prev.get("atr", 0.0))
            price = float(bar_prev["close"])
            sl, tp = self.strategy.initial_sl_tp(signal_result.signal, atr, price)
            open_pos = TradeRecord(
                entry_time=bar_time,
                exit_time=bar_time,
                side=signal_result.signal,
                entry=price,
                exit=price,
                sl=sl,
                tp=tp,
                r_multiple=0.0,
                balance_after=self.balance,
                reason=signal_result.reason,
            )

        wins = sum(1 for t in self.trades if t.r_multiple > 0)
        total_r = sum(t.r_multiple for t in self.trades)
        max_balance = self.initial_balance
        max_dd = 0.0
        for t in self.trades:
            max_balance = max(max_balance, t.balance_after)
            max_dd = max(max_dd, (max_balance - t.balance_after) / max_balance if max_balance else 0.0)
        win_rate = wins / len(self.trades) if self.trades else 0.0
        return BacktestReport(
            trades=self.trades,
            start_balance=self.initial_balance,
            end_balance=self.balance,
            win_rate=win_rate,
            total_r=total_r,
            max_drawdown=max_dd,
        )
