from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from typing import Dict, List, Optional

import pytz
import yaml
from pydantic import BaseModel, Field, ValidationError


class Signal(str, Enum):
    NONE = "NONE"
    BUY = "BUY"
    SELL = "SELL"


class Timeframes(BaseModel):
    signal_tf: str
    trend_tf: str


class GuardSettings(BaseModel):
    time_blacklist: List[str] = Field(default_factory=list)  # e.g. ["22:50-23:20"]
    atr_min: float = 0.0
    atr_max: float = 1e9


class RiskSettings(BaseModel):
    risk_per_trade: float
    max_positions: int
    spread_limit_points: float
    daily_loss_pct: float
    max_consecutive_losses: int
    kill_switch_pct: float
    atr_sl_mult: float
    atr_tp_mult: float
    min_rr: float
    trailing_atr_mult: float = 1.0
    breakeven_rr: float = 1.0


class Profile(BaseModel):
    symbols: List[str]
    timeframes: Timeframes
    strategy: str
    risk: RiskSettings
    guard: GuardSettings = GuardSettings()
    cooldown_seconds: int = 60
    cooldown_candles: int = 0


class MT5Settings(BaseModel):
    login_env: str
    password_env: str
    server_env: str
    path_env: str
    timezone: str = "Etc/UTC"
    symbol_fallbacks: List[str] = Field(default_factory=list)

    @property
    def credentials(self) -> Dict[str, str | int]:
        return {
            "login": int(os.getenv(self.login_env, 0)),
            "password": os.getenv(self.password_env, ""),
            "server": os.getenv(self.server_env, ""),
            "path": os.getenv(self.path_env, ""),
        }


class DataSettings(BaseModel):
    lookback: int = 500
    refresh_seconds: float = 5.0


class ExecutionSettings(BaseModel):
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


class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: str = "logs/framework.log"
    max_bytes: int = 1_048_576
    backup_count: int = 5


class LoopSettings(BaseModel):
    poll_seconds: float = 15.0
    backoff_seconds: float = 30.0
    max_total_positions: int = 5


class AppConfig(BaseModel):
    mt5: MT5Settings
    data: DataSettings
    profiles: Dict[str, Profile]
    strategy_params: Dict[str, Dict[str, float | int | str]]
    execution: ExecutionSettings
    logging: LoggingSettings
    loop: LoopSettings


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


def candle_body(df_row) -> float:
    return abs(df_row["close"] - df_row["open"])


def timeframe_seconds(tf: str) -> int:
    mapping = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }
    upper = tf.upper()
    if upper not in mapping:
        raise BotConfigError(f"Unsupported timeframe for duration: {tf}")
    return mapping[upper]


def _parse_window(window: str, tz_name: str) -> tuple[datetime, datetime]:
    tz = pytz.timezone(tz_name)
    today = datetime.now(tz).date()
    start_s, end_s = window.split("-")
    start_parts = [int(x) for x in start_s.split(":")]
    end_parts = [int(x) for x in end_s.split(":")]
    start_dt = tz.localize(datetime.combine(today, dtime(start_parts[0], start_parts[1])))
    end_dt = tz.localize(datetime.combine(today, dtime(end_parts[0], end_parts[1])))
    return start_dt, end_dt


def within_time_blackout(now: datetime, windows: List[str], tz_name: str, logger) -> bool:
    if not windows:
        return False
    for window in windows:
        try:
            start_dt, end_dt = _parse_window(window, tz_name)
            if start_dt <= now <= end_dt:
                logger.info("Skip due to time blacklist window %s", window)
                return True
        except Exception:  # noqa: BLE001
            logger.warning("Invalid time window format: %s", window)
    return False


def should_trade_now(now: datetime, guard: GuardSettings, spread_points: float, atr_value: float, spread_limit: float, tz_name: str, logger) -> bool:
    now_local = now.astimezone(pytz.timezone(tz_name))
    if within_time_blackout(now_local, guard.time_blacklist, tz_name, logger):
        return False
    if spread_points > spread_limit:
        logger.debug("Skip trade: spread %.2f > limit %.2f", spread_points, spread_limit)
        return False
    if atr_value < guard.atr_min:
        logger.debug("Skip trade: ATR %.5f below min %.5f", atr_value, guard.atr_min)
        return False
    if atr_value > guard.atr_max:
        logger.debug("Skip trade: ATR %.5f above max %.5f", atr_value, guard.atr_max)
        return False
    return True
