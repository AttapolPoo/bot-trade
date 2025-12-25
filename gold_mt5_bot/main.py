from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from dotenv import load_dotenv

from gold_mt5_bot.bot.logger import setup_logging
from gold_mt5_bot.bot.mt5_client import MT5Client
from gold_mt5_bot.bot.data import MarketData
from gold_mt5_bot.bot.strategy import Strategy
from gold_mt5_bot.bot.risk import RiskManager
from gold_mt5_bot.bot.execution import ExecutionManager
from gold_mt5_bot.bot.position_manager import PositionManager
from gold_mt5_bot.bot.utils import AppConfig, Signal, load_config, within_blackout
from gold_mt5_bot.bot.indicators import add_atr


def run(config_path: str = "config.yaml", stop_event: Event | None = None) -> None:
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent / cfg_path
    config: AppConfig = load_config(str(cfg_path))
    logger = setup_logging(config.logging)

    mt5_client = MT5Client(logger=logger, config=config.mt5)
    mt5_client.initialize()
    mt5_client.login()
    symbol = mt5_client.resolve_symbol(config.mt5.symbol, config.mt5.symbol_fallbacks)
    mt5_client.symbol_select(symbol)

    # Pre-flight validation
    symbol_info = mt5_client.get_symbol_info(symbol)
    if not mt5_client.is_tradeable(symbol):
        logger.error("Symbol %s not tradeable (mode=%s)", symbol, symbol_info.trade_mode)
        return
    tick = mt5_client.get_tick(symbol)
    if tick.bid <= 0 or tick.ask <= 0:
        logger.error("Invalid tick for %s: bid=%s ask=%s", symbol, tick.bid, tick.ask)
        return
    spread_points = (tick.ask - tick.bid) / symbol_info.point
    if spread_points > config.strategy.spread_limit_points:
        logger.error("Spread too wide on startup: %.2f > %.2f", spread_points, config.strategy.spread_limit_points)
        return
    try:
        rates_check = mt5_client.get_rates(symbol, config.mt5.timeframes.trigger, 5)
        if rates_check is None or len(rates_check) == 0:
            logger.error("No rates data on startup for %s", symbol)
            return
    except Exception as exc:  # noqa: BLE001
        logger.error("Rates check failed: %s", exc)
        return

    data_client = MarketData(mt5_client, config)
    strategy = Strategy(config.strategy, logger)
    risk_manager = RiskManager(config.risk, logger)
    executor = ExecutionManager(mt5_client, config.execution, logger)
    position_manager = PositionManager(mt5_client, config, risk_manager, logger)

    logger.info("Gold MT5 bot started for %s", symbol)

    try:
        while True:
            loop_start = time.time()
            try:
                if stop_event and stop_event.is_set():
                    logger.info("Stop requested; exiting loop")
                    break
                mt5_client.ensure_connected()
                now = datetime.now(timezone.utc)

                if within_blackout(config.strategy.news_blackout, now, logger):
                    time.sleep(config.loop.poll_seconds)
                    continue

                symbol_info = mt5_client.get_symbol_info(symbol)
                if not mt5_client.is_tradeable(symbol):
                    logger.error("Symbol %s not tradeable; stopping loop", symbol)
                    time.sleep(config.loop.backoff_seconds)
                    continue
                tick = mt5_client.get_tick(symbol)
                spread_points = (tick.ask - tick.bid) / symbol_info.point
                if spread_points > config.strategy.spread_limit_points:
                    logger.debug("Spread guard hit: %.2f > limit %.2f", spread_points, config.strategy.spread_limit_points)
                    time.sleep(config.loop.poll_seconds)
                    continue

                data = data_client.get_timeframes(symbol)
                if any(df.empty for df in data.values()):
                    logger.error("No rate data for symbol %s; skipping cycle", symbol)
                    time.sleep(config.loop.backoff_seconds)
                    continue
                trigger_tf = config.mt5.timeframes.trigger
                trigger_df = data[trigger_tf]
                if "atr" not in trigger_df.columns:
                    add_atr(trigger_df, config.strategy.atr_period)
                if trigger_df.shape[0] < 2:
                    logger.debug("Not enough bars for ATR")
                    time.sleep(config.loop.poll_seconds)
                    continue
                atr_value = trigger_df["atr"].iloc[-2]

                halt_reason = risk_manager.should_halt_trading(mt5_client, symbol, now)
                if halt_reason:
                    logger.warning("Trading halted: %s", halt_reason)
                    time.sleep(config.loop.backoff_seconds)
                    continue

                position_manager.manage_positions(symbol, tick, atr_value)
                signal, reason = strategy.generate_signal(symbol, data, spread_points)
                logger.info("Signal: %s | reason=%s", signal.value, reason)

                if signal != Signal.NONE and position_manager.can_open_new_position(symbol):
                    order_params = risk_manager.build_order_params(signal, tick, symbol_info, atr_value)
                    if order_params:
                        executor.execute_trade(symbol, signal, tick, symbol_info, order_params)
                sleep_for = max(config.loop.poll_seconds - (time.time() - loop_start), 0)
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Loop error: %s", exc)
                time.sleep(config.loop.backoff_seconds)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received; shutting down...")
    finally:
        mt5_client.shutdown()


if __name__ == "__main__":
    run()
