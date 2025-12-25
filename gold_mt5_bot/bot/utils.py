from __future__ import annotations

import os
from datetime import datetime
from enum import Enum
from typing import Dict

import pytz
import yaml
from pydantic import BaseModel, Field, ValidationError


class Signal(str, Enum):
    NONE = "NONE"
    BUY = "BUY"
    SELL = "SELL"


class NewsBlackout(BaseModel):
    enabled: bool = False
    windows: list[str] = Field(default_factory=list)


class TimeframeConfig(BaseModel):
    trend: str = "H1"
    signal: str = "M15"
    trigger: str = "M5"


class MT5Config(BaseModel):
    login_env: str
    password_env: str
    server_env: str
    path_env: str
    symbol: str
    timeframes: TimeframeConfig
    timezone: str = "Etc/UTC"
    symbol_fallbacks: list[str] = Field(
        default_factory=lambda: ["GOLD#", "XAUUSD", "XAUUSD#", "GOLD", "GOLDm", "XAUUSDm"]
    )

    @property
    def credentials(self) -> Dict[str, str | int]:
        return {
            "login": int(os.getenv(self.login_env, 0)),
            "password": os.getenv(self.password_env, ""),
            "server": os.getenv(self.server_env, ""),
            "path": os.getenv(self.path_env, ""),
        }


class StrategyConfig(BaseModel):
    ema_fast: int
    ema_slow: int
    ema_trend_fast: int
    ema_trend_slow: int
    rsi_period: int
    rsi_buy: float
    rsi_sell: float
    atr_period: int
    atr_silence_threshold: float
    spread_limit_points: float
    max_candle_spread_ratio: float
    news_blackout: NewsBlackout
    max_signal_per_bar: int = 1
    cooldown_minutes: int = 0


class TrailingConfig(BaseModel):
    enabled: bool = True
    atr_mult: float = 1.0
    step: float = 0.5


class KillSwitchConfig(BaseModel):
    enabled: bool = True
    equity_drop_pct: float = 0.05


class RiskConfig(BaseModel):
    risk_per_trade: float
    max_open_positions: int
    max_daily_loss_pct: float
    max_consecutive_losses: int
    min_rr: float
    atr_sl_mult: float
    atr_tp_mult: float
    trailing: TrailingConfig
    breakeven_rr: float
    kill_switch: KillSwitchConfig


class ExecutionConfig(BaseModel):
    magic: int
    deviation: int
    slippage_guard_points: int
    volume_min: float
    volume_step: float
    comment: str
    retries: int
    retry_sleep: float
    dry_run: bool = True
    journal_csv: str


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/bot.log"
    max_bytes: int = 1_048_576
    backup_count: int = 5


class LoopConfig(BaseModel):
    poll_seconds: float = 15.0
    backoff_seconds: float = 30.0


class DataConfig(BaseModel):
    lookback: int = 300
    refresh_seconds: float = 5.0


class AppConfig(BaseModel):
    mt5: MT5Config
    data: DataConfig
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    logging: LoggingConfig
    loop: LoopConfig


class BotConfigError(RuntimeError):
    pass


def load_config(path: str) -> AppConfig:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise BotConfigError(f"Config file not found: {path}") from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise BotConfigError(f"Invalid config: {exc}") from exc


def timeframe_to_mt5(tf: str) -> int:
    import MetaTrader5 as mt5  # type: ignore

    mapping = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    tf_upper = tf.upper()
    if tf_upper not in mapping:
        raise BotConfigError(f"Unsupported timeframe: {tf}")
    return mapping[tf_upper]


def ensure_timezone(dt: datetime, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def within_blackout(blackout: NewsBlackout, now: datetime, logger) -> bool:
    if not blackout.enabled or not blackout.windows:
        return False
    for window in blackout.windows:
        try:
            start_str, end_str = window.split("/")
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if start <= now <= end:
                logger.info("Skipping due to blackout window %s", window)
                return True
        except ValueError:
            logger.warning("Invalid blackout window format: %s", window)
    return False


def candle_width_ok(body: float, atr: float, max_ratio: float) -> bool:
    return body <= atr * max_ratio
