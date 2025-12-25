from __future__ import annotations

import argparse
import time
import os
from datetime import datetime, timezone
from typing import Dict, List

from dotenv import load_dotenv

from bot.data import MarketData
from bot.execution import ExecutionManager
from bot.logger import setup_logging
from bot.backtest import Backtester
from bot.mt5_client import MT5Client
from bot.portfolio_manager import PortfolioManager
from bot.position_manager import PositionManager
from bot.profiles import build_custom_profile, get_profile, list_profiles, summarize_profile
from bot.risk import RiskManager
from bot.strategy import BreakoutATR, EmaRsiPullback, MeanReversionBBands
from bot.utils import AppConfig, BotConfigError, Signal, load_config, timeframe_to_mt5


STRATEGY_MAP = {
    "ema_rsi_pullback": EmaRsiPullback,
    "breakout_atr": BreakoutATR,
    "mean_reversion_bbands": MeanReversionBBands,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MT5 Trading Framework")
    parser.add_argument("--profile", help="Profile name (scalp_fast/intraday/swing)")
    parser.add_argument("--symbols", help="Comma-separated symbols for custom run")
    parser.add_argument("--signal_tf", help="Signal timeframe for custom run")
    parser.add_argument("--trend_tf", help="Trend timeframe for custom run")
    parser.add_argument("--strategy", help="Strategy name for custom run")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    parser.add_argument("--backtest", action="store_true", help="Run backtest instead of live trading")
    parser.add_argument("--backtest-start", help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--backtest-end", help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--backtest-balance", type=float, default=10000.0, help="Starting balance for backtest")
    return parser.parse_args()


def choose_profile(args: argparse.Namespace, config: AppConfig):
    if args.profile and args.profile in config.profiles:
        return get_profile(config, args.profile)

    if args.symbols and args.signal_tf and args.trend_tf and args.strategy:
        base = config.profiles.get(args.profile or "intraday") or list(config.profiles.values())[0]
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        return build_custom_profile(symbols, args.signal_tf, args.trend_tf, args.strategy, base)

    names = list_profiles(config)
    print("Select profile:")
    for idx, name in enumerate(names, 1):
        print(f"[{idx}] {name}")
    choice = input("Enter number: ").strip()
    selected = names[int(choice) - 1]
    return get_profile(config, selected)


def build_strategy(strategy_name: str, params: Dict[str, float | int | str], logger):
    if strategy_name not in STRATEGY_MAP:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    return STRATEGY_MAP[strategy_name](params, logger)


def validate_profile(mt5_client: MT5Client, profile) -> None:
    mt5_client.validate_symbols(profile.symbols)
    for tf in [profile.timeframes.signal_tf, profile.timeframes.trend_tf]:
        timeframe_to_mt5(tf)


def resolve_profile_symbols(mt5_client: MT5Client, profile, fallbacks: List[str], logger):
    resolved = []
    for sym in profile.symbols:
        resolved_sym = mt5_client.resolve_symbol(sym, fallbacks or ["GOLD#", "XAUUSD", "XAUUSD#", "GOLD", "GOLDm", "XAUUSDm"])
        mt5_client.symbol_select(resolved_sym)
        resolved.append(resolved_sym)
    profile.symbols = resolved
    logger.info("Resolved symbols: %s", resolved)


def preflight_checks(mt5_client: MT5Client, profile, data_client: MarketData, logger) -> bool:
    ok = True
    for sym in profile.symbols:
        try:
            info = mt5_client.get_symbol_info(sym)
            if not mt5_client.is_tradeable(sym):
                logger.error("Symbol %s not tradeable (trade_mode=%s)", sym, info.trade_mode)
                ok = False
                continue
            tick = mt5_client.get_tick(sym)
            if tick.bid <= 0 or tick.ask <= 0:
                logger.error("Invalid tick for %s: bid=%s ask=%s", sym, tick.bid, tick.ask)
                ok = False
                continue
            spread_points = (tick.ask - tick.bid) / info.point
            if spread_points > profile.risk.spread_limit_points:
                logger.error("Preflight spread too wide for %s: %.2f > %.2f", sym, spread_points, profile.risk.spread_limit_points)
                ok = False
            tf_map = {"signal": profile.timeframes.signal_tf, "trend": profile.timeframes.trend_tf}
            data = data_client.get(sym, tf_map)
            for tf_name, df in data.items():
                if df.empty:
                    logger.error("No rates data for %s %s", sym, tf_name)
                    ok = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("Preflight failed for %s: %s", sym, exc)
            ok = False
    return ok


def run(config_path: str = "config.yaml") -> None:
    load_dotenv()
    config: AppConfig = load_config(config_path)
    logger = setup_logging(config.logging)
    args = parse_args()

    selected = choose_profile(args, config)
    if hasattr(selected.profile, "name"):
        selected.profile.name = selected.name

    mt5_client = MT5Client(logger=logger, config=config.mt5)
    mt5_client.initialize()
    mt5_client.login()
    resolve_profile_symbols(mt5_client, selected.profile, config.mt5.symbol_fallbacks, logger)
    validate_profile(mt5_client, selected.profile)
    logger.info("Profile selected: %s", summarize_profile(selected))

    strat_params = config.strategy_params.get(selected.profile.strategy, {})
    strategy = build_strategy(selected.profile.strategy, strat_params, logger)

    data_client = MarketData(mt5_client, config)
    risk_manager = RiskManager(selected.profile.risk, logger)
    executor = ExecutionManager(mt5_client, config.execution, logger)
    position_manager = PositionManager(mt5_client, selected.profile.risk, logger)
    portfolio = PortfolioManager(
        profile=selected.profile,
        strategy=strategy,
        profile_name=selected.name,
        mt5_client=mt5_client,
        data_client=data_client,
        risk_manager=risk_manager,
        executor=executor,
        position_manager=position_manager,
        loop_config=config.loop,
        logger=logger,
    )

    env_dry_run = os.getenv("DRY_RUN")
    if env_dry_run is not None:
        config.execution.dry_run = env_dry_run.lower() == "true"
    if args.dry_run:
        config.execution.dry_run = True

    if args.backtest:
        if not args.backtest_start or not args.backtest_end:
            raise BotConfigError("Backtest requires --backtest-start and --backtest-end (YYYY-MM-DD)")
        bt = Backtester(
            mt5_client=mt5_client,
            strategy=strategy,
            profile=selected.profile,
            logger=logger,
            initial_balance=args.backtest_balance,
        )
        report = bt.run(
            symbol=selected.profile.symbols[0],
            date_from=datetime.fromisoformat(args.backtest_start),
            date_to=datetime.fromisoformat(args.backtest_end),
        )
        logger.info(
            "Backtest done: trades=%s win_rate=%.2f%% total_r=%.2f start_balance=%.2f end_balance=%.2f max_dd=%.2f%%",
            len(report.trades),
            report.win_rate * 100,
            report.total_r,
            report.start_balance,
            report.end_balance,
            report.max_drawdown * 100,
        )
        for t in report.trades:
            logger.info(
                "Trade %s entry=%s exit=%s side=%s R=%.2f bal=%.2f reason=%s",
                t.entry_time,
                t.entry,
                t.exit,
                t.side.value,
                t.r_multiple,
                t.balance_after,
                t.reason,
            )
        return

    if not preflight_checks(mt5_client, selected.profile, data_client, logger):
        logger.error("Preflight checks failed; exiting.")
        return

    logger.info("Starting trading loop (dry_run=%s)", config.execution.dry_run)
    try:
        while True:
            loop_start = time.time()
            try:
                mt5_client.ensure_connected()
                portfolio.run_cycle()
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
    try:
        run()
    except BotConfigError as exc:
        print(f"Config error: {exc}")
