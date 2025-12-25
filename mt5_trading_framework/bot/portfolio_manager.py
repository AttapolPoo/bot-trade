from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict

from bot.strategy.base import StrategyBase
from bot.utils import Profile, Signal, should_trade_now, timeframe_seconds


@dataclass
class SymbolState:
    last_signal_bar: dict = field(default_factory=dict)
    cooldown_until: datetime | None = None
    cooldown_bar_until: datetime | None = None
    last_bar_time: dict = field(default_factory=dict)


class PortfolioManager:
    def __init__(
        self,
        profile: Profile,
        strategy: StrategyBase,
        profile_name: str,
        mt5_client,
        data_client,
        risk_manager,
        executor,
        position_manager,
        loop_config,
        logger,
    ) -> None:
        self.profile = profile
        self.profile_name = profile_name
        self.strategy = strategy
        self.mt5_client = mt5_client
        self.data_client = data_client
        self.risk_manager = risk_manager
        self.executor = executor
        self.position_manager = position_manager
        self.loop_config = loop_config
        self.logger = logger
        self.state: Dict[str, SymbolState] = {sym: SymbolState() for sym in profile.symbols}

    def total_positions(self) -> int:
        return len(self.mt5_client.get_positions())

    def run_cycle(self) -> None:
        for symbol in self.profile.symbols:
            try:
                self._process_symbol(symbol)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Symbol loop error for %s: %s", symbol, exc)
                time.sleep(self.loop_config.backoff_seconds)

    def _process_symbol(self, symbol: str) -> None:
        now = datetime.now(timezone.utc)
        state = self.state[symbol]
        if state.cooldown_until and now < state.cooldown_until:
            return
        if state.cooldown_bar_until and now < state.cooldown_bar_until:
            return

        symbol_info = self.mt5_client.get_symbol_info(symbol)
        tick = self.mt5_client.get_tick(symbol)
        spread_points = (tick.ask - tick.bid) / symbol_info.point

        halt_reason = self.risk_manager.should_halt_trading(self.mt5_client, symbol, now)
        if halt_reason:
            self.logger.warning("Trading halted: %s", halt_reason)
            return

        if self.total_positions() >= self.loop_config.max_total_positions:
            self.logger.info("Global max positions reached")
            return

        tf_map = {"signal": self.profile.timeframes.signal_tf, "trend": self.profile.timeframes.trend_tf}
        data_raw = self.data_client.get(symbol, tf_map)
        df_map = {"signal": data_raw[self.profile.timeframes.signal_tf], "trend": data_raw[self.profile.timeframes.trend_tf]}
        df_map = self.strategy.compute_indicators(df_map)

        if any(len(df) < 5 for df in df_map.values()):
            return

        bar_time = df_map["signal"].index[-2]
        trend_time = df_map["trend"].index[-2]
        if state.last_bar_time.get("signal") == bar_time:
            return

        atr_value = float(df_map["signal"]["atr"].iloc[-2]) if "atr" in df_map["signal"] else 0.0

        if not should_trade_now(
            now,
            self.profile.guard,
            spread_points,
            atr_value,
            self.profile.risk.spread_limit_points,
            self.mt5_client.config.timezone,
            self.logger,
        ):
            state.last_bar_time["signal"] = bar_time
            state.last_bar_time["trend"] = trend_time
            return

        self.position_manager.manage(symbol, tick, atr_value)

        signal_result = self.strategy.generate_signal(df_map)
        self.logger.info(
            "%s signal=%s conf=%.2f reason=%s", symbol, signal_result.signal.value, signal_result.confidence, signal_result.reason
        )

        if signal_result.signal == Signal.NONE:
            state.last_bar_time["signal"] = bar_time
            state.last_bar_time["trend"] = trend_time
            return

        if not self.position_manager.can_open(symbol, self.profile.risk.max_positions):
            state.last_bar_time["signal"] = bar_time
            state.last_bar_time["trend"] = trend_time
            return

        order_params = self.risk_manager.build_order_params(signal_result.signal, tick, symbol_info, atr_value)
        if not order_params:
            state.last_bar_time["signal"] = bar_time
            state.last_bar_time["trend"] = trend_time
            return

        self.executor.execute_trade(
            symbol,
            signal_result.signal,
            tick,
            order_params,
            meta={
                "profile": self.profile_name,
                "signal_tf": self.profile.timeframes.signal_tf,
                "trend_tf": self.profile.timeframes.trend_tf,
                "confidence": signal_result.confidence,
                "reason": signal_result.reason,
                "spread_points": spread_points,
            },
        )
        state.last_signal_bar["signal"] = bar_time
        state.last_bar_time["signal"] = bar_time
        state.last_bar_time["trend"] = trend_time
        state.cooldown_until = now + timedelta(seconds=self.profile.cooldown_seconds)
        if self.profile.cooldown_candles > 0:
            tf_seconds = timeframe_seconds(self.profile.timeframes.signal_tf)
            state.cooldown_bar_until = bar_time + timedelta(seconds=self.profile.cooldown_candles * tf_seconds)
