# Gold MT5 Bot

Production-oriented Python 3.10+ trading bot for XAUUSD (Gold Spot) on MetaTrader 5. Features modular design, YAML config, .env credentials, rotating logs, risk controls, and 24/7 reconnect logic.

## Features
- MT5 client wrapper with auto reconnect, symbol selection, resilient order submission, and tick/rates helpers.
- Multi-timeframe data (H1/M15/M5 by default) with EMA/RSI/ATR indicators.
- Strategy: trend filter (H1 EMA50/200), pullback + RSI momentum on lower TF, spread/ATR/news guards, one trade per bar.
- Risk: ATR-based SL/TP, min RR, balance-based sizing, kill switch, daily loss/consecutive loss caps, trailing stop + break-even.
- Execution: deviation + retry on requote, dry-run mode, trade journal CSV, magic/comment tagging.
- Logging: console + rotating file handler with structured messages.

## Quickstart
1) Python 3.10+, install MT5 terminal.
2) python -m venv .venv && .venv\\Scripts\\activate
3) pip install -r requirements.txt
4) Copy .env.example to .env and fill MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH.
5) Adjust config.yaml (symbol/timeframes/risk/loop intervals). Defaults are conservative (0.5% risk, one position).
6) Run: python -m gold_mt5_bot.main (or python gold_mt5_bot/main.py).

## Config overview
- mt5: credentials (from env), terminal path, symbol, timeframes, timezone.
- data: lookback bars, refresh seconds.
- strategy: EMA/RSI/ATR settings, spread/ATR guards, news blackout windows, per-bar cooldown.
- isk: risk per trade, max positions, max daily loss, consecutive loss cap, RR, trailing/breakeven, kill switch.
- execution: magic, deviation, slippage guard, retries, dry-run toggle, journal path.
- logging: level, rotating file path/size.
- loop: poll/backoff intervals.

## Notes
- Dry-run (execution.dry_run=true) logs signals without sending orders; switch to alse only after demo testing.
- Uses candle close for signals; ensures one trade per bar.
- Spread/ATR/news guards aim to avoid rollover/noise; tune to your broker.
- Always test on demo; this code is for educational use only.
