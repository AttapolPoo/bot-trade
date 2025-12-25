# MT5 Trading Framework

Modular, profile-driven MetaTrader 5 trading framework supporting multiple symbols, timeframes, and strategy styles (scalp/intraday/swing). Production-oriented with YAML config, .env credentials, rotating logs, risk controls, 24/7 runner with reconnect.

## Features
- Profile selector (CLI flags or interactive) with predefined scalp_fast, intraday, swing, xm_gold_intraday, xm_gold_scalp profiles.
- Strategy plugin system (EMA/RSI pullback, ATR breakout, BBands mean reversion). Config-driven parameters.
- Multi-symbol portfolio manager with per-symbol state (last candle processed per TF, cooldown by candle/seconds, open position guard), max total positions.
- MT5 wrapper with auto reconnect, symbol resolver (GOLD#/XAUUSD variants), rates/tick helpers, resilient order placement and stop modification.
- Risk controls: ATR-based sizing, daily loss/consecutive loss/kill switch, spread/slippage guard, session guard/time blacklist, dry-run mode, journal CSV with reasons.
- Logging: console + rotating file. Watchdog loop with backoff.

## Quickstart
1) Python 3.10+, MT5 terminal installed.
2) `python -m venv .venv && .venv\\Scripts\\activate`
3) `pip install -r requirements.txt`
4) Copy `.env.example` to `.env` and fill `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_PATH`.
5) Adjust `config.yaml` (profiles, risk, timeframes, strategy params). Defaults are conservative and dry-run.
6) Run: `python -m mt5_trading_framework.main --profile scalp_fast` (or `python mt5_trading_framework/main.py`).
   - XM GOLD# presets: `python -m mt5_trading_framework.main --profile xm_gold_intraday` or `python -m mt5_trading_framework.main --profile xm_gold_scalp --dry-run`.
   - For custom: `python -m mt5_trading_framework.main --symbols XAUUSD,EURUSD --signal_tf M5 --trend_tf H1 --strategy ema_rsi_pullback`.

## XM GOLD# setup
- Use the `xm_gold_intraday` or `xm_gold_scalp` profiles in `config.yaml`. Preferred symbol `GOLD#` resolves automatically with fallbacks (`GOLD#`, `XAUUSD`, `XAUUSD#`, `GOLD`, `GOLDm`, `XAUUSDm`).
- Time blacklist defaults to `22:50-23:20` to avoid rollover. Tune spread/ATR guards in the profile `guard` block.
- Dry-run first (`--dry-run` or `DRY_RUN=true`) to confirm preflight passes (symbol selected, tradeable mode, spreads acceptable).

## VPS/24-7
- Use screen/tmux/service; logs in `logs/framework.log`.
- Bot reconnects MT5 and backs off on errors; graceful Ctrl+C shutdown.

## Notes
- Dry-run by default; switch `execution.dry_run=false` only after demo testing.
- Signals use candle close; one position per symbol per strategy by default.
- Journal CSV (`journal.csv`) logs every decision/order for later analysis.
- Educational use; test on demo before live.

## Backtesting (all timeframes)
- Run offline backtests with the same profile/strategy params:  
  `python -m mt5_trading_framework.main --profile xm_gold_intraday --backtest --backtest-start 2024-01-01 --backtest-end 2024-02-01 --backtest-balance 10000`
- Reports show trades, R-multiples, win rate, start/end balance, and max drawdown in logs. Uses candle-close logic and ATR-based SL/TP like live mode.
