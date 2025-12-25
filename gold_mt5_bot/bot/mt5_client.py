from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, Optional

import MetaTrader5 as mt5  # type: ignore

from gold_mt5_bot.bot.utils import BotConfigError, timeframe_to_mt5


class MT5Client:
    def __init__(self, logger, config) -> None:
        self.logger = logger
        self.config = config
        creds = config.credentials
        self.login_id = creds["login"]
        self.password = creds["password"]
        self.server = creds["server"]
        self.path = creds["path"]

    def initialize(self) -> None:
        if not self.path:
            common_paths = [
                r"C:\Program Files\MetaTrader 5\terminal64.exe",
                r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
            ]
            for candidate in common_paths:
                if os.path.exists(candidate):
                    self.path = candidate
                    self.logger.info("MT5_PATH not set; using detected terminal: %s", candidate)
                    break
        if not self.path:
            raise BotConfigError("MT5 terminal path is empty. Set MT5_PATH in .env to your terminal64.exe")
        if not mt5.initialize(self.path):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        self.logger.info("MT5 initialized with terminal %s", self.path)

    def login(self) -> None:
        # If terminal is already logged in, reuse that session.
        current = mt5.account_info()
        if current:
            self.logger.info("Using existing MT5 session: account=%s server=%s", current.login, current.server)
            return
        if not self.login_id or not self.password or not self.server:
            raise BotConfigError("Missing MT5 credentials in environment (MT5_LOGIN/MT5_PASSWORD/MT5_SERVER)")
        if not mt5.login(self.login_id, password=self.password, server=self.server):
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        self.logger.info("Logged into MT5 account %s (%s)", self.login_id, self.server)

    def ensure_connected(self) -> None:
        account_info = mt5.account_info()
        if account_info is None:
            self.logger.warning("MT5 account info unavailable; reconnecting...")
            mt5.shutdown()
            time.sleep(1)
            self.initialize()
            self.login()
        elif account_info.server != self.server:
            self.logger.warning("Server mismatch; relogin...")
            mt5.login(self.login_id, password=self.password, server=self.server)

    def shutdown(self) -> None:
        mt5.shutdown()
        self.logger.info("MT5 shutdown complete")

    def symbol_select(self, symbol: str) -> None:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Failed to select symbol {symbol}: {mt5.last_error()}")

    def resolve_symbol(self, preferred: str, fallbacks: Iterable[str]) -> str:
        candidates = [preferred] + [s for s in fallbacks if s != preferred]
        for sym in candidates:
            info = mt5.symbol_info(sym)
            if info is None:
                continue
            if not info.visible:
                self.symbol_select(sym)
            self.logger.info("Resolved symbol %s -> %s", preferred, sym)
            return sym
        available = mt5.symbols_get()
        similar = [s.name for s in available if preferred.lower() in s.name.lower() or s.name.lower().startswith(preferred.lower())]
        self.logger.error("Symbol %s not found. Similar: %s", preferred, similar)
        raise RuntimeError(f"Symbol not found: {preferred}")

    def get_symbol_info(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Symbol not found: {symbol}")
        return info

    def get_rates(self, symbol: str, timeframe: str, n: int):
        tf = timeframe_to_mt5(timeframe)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
        if rates is None:
            raise RuntimeError(f"Failed to get rates for {symbol} {timeframe}: {mt5.last_error()}")
        return rates

    def get_tick(self, symbol: str):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"Failed to get tick for {symbol}: {mt5.last_error()}")
        return tick

    def place_order(
        self,
        symbol: str,
        side: str,
        lot: float,
        sl: float,
        tp: float,
        magic: int,
        deviation: int,
        comment: str,
    ) -> Dict[str, Any]:
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"No tick data for {symbol}")
        price = tick.ask if side == "BUY" else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": deviation,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"Order send failed: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"Order rejected: {result}")
        return request | {"result": result._asdict()}  # type: ignore

    def close_position(self, position_id: int, deviation: int) -> None:
        pos = mt5.positions_get(ticket=position_id)
        if not pos:
            return
        position = pos[0]
        opposite = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            return
        price = tick.bid if opposite == mt5.ORDER_TYPE_SELL else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": opposite,
            "price": price,
            "deviation": deviation,
            "magic": position.magic,
            "comment": "close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"Close failed: {result}")

    def modify_position_sl_tp(self, ticket: int, sl: float, tp: float) -> None:
        position = mt5.positions_get(ticket=ticket)
        if not position:
            return
        pos = position[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": sl,
            "tp": tp,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.warning("Modify SL/TP failed for %s: %s", ticket, result)

    def get_positions(self, symbol: Optional[str] = None):
        positions = mt5.positions_get(symbol=symbol)
        return positions or []

    def get_account_info(self):
        return mt5.account_info()

    def get_daily_deals(self, date_from, date_to):
        return mt5.history_deals_get(date_from, date_to)

    def is_tradeable(self, symbol: str) -> bool:
        info = self.get_symbol_info(symbol)
        return info.trade_mode in (
            mt5.SYMBOL_TRADE_MODE_FULL,
            mt5.SYMBOL_TRADE_MODE_LONGONLY,
            mt5.SYMBOL_TRADE_MODE_SHORTONLY,
        )

    def normalize_price(self, symbol_info, price: float) -> float:
        return round(price, symbol_info.digits)

    def normalize_volume(self, symbol_info, lot: float) -> float:
        step = symbol_info.volume_step
        min_vol = symbol_info.volume_min
        max_vol = symbol_info.volume_max
        stepped = max(min_vol, min(max_vol, round(lot / step) * step))
        return round(stepped, 2)

    def validate_stops(self, symbol_info, side: str, price: float, sl: float, tp: float) -> tuple[float, float]:
        point = symbol_info.point
        min_dist_points = max(symbol_info.trade_stops_level, symbol_info.trade_freeze_level)
        min_dist = min_dist_points * point
        if min_dist_points <= 0:
            return sl, tp
        if side == "BUY":
            if sl and price - sl < min_dist:
                sl = price - min_dist
                self.logger.warning("Adjusted SL to respect stop level: %.5f", sl)
            if tp and tp - price < min_dist:
                tp = price + min_dist
                self.logger.warning("Adjusted TP to respect stop level: %.5f", tp)
        else:
            if sl and sl - price < min_dist:
                sl = price + min_dist
                self.logger.warning("Adjusted SL to respect stop level: %.5f", sl)
            if tp and price - tp < min_dist:
                tp = price - min_dist
                self.logger.warning("Adjusted TP to respect stop level: %.5f", tp)
        return sl, tp
