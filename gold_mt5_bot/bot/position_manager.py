from __future__ import annotations

from typing import Optional

from gold_mt5_bot.bot.utils import AppConfig, Signal


class PositionManager:
    def __init__(self, mt5_client, config: AppConfig, risk_manager, logger) -> None:
        self.mt5_client = mt5_client
        self.config = config
        self.risk_manager = risk_manager
        self.logger = logger

    def can_open_new_position(self, symbol: str) -> bool:
        positions = self.mt5_client.get_positions(symbol=symbol)
        if len(positions) >= self.config.risk.max_open_positions:
            self.logger.info("Max open positions reached (%s)", self.config.risk.max_open_positions)
            return False
        return True

    def manage_positions(self, symbol: str, tick, atr: float) -> None:
        positions = self.mt5_client.get_positions(symbol=symbol)
        if not positions:
            return
        for pos in positions:
            side = Signal.BUY if pos.type == 0 else Signal.SELL
            entry = pos.price_open
            price = tick.ask if side == Signal.BUY else tick.bid
            rr = (price - entry) / (atr * self.config.risk.atr_sl_mult) if side == Signal.BUY else (entry - price) / (atr * self.config.risk.atr_sl_mult)

            if self.config.risk.trailing.enabled:
                new_sl = self._calc_trailing_sl(side, price, atr, pos, self.config.risk.trailing.atr_mult)
                if new_sl and ((side == Signal.BUY and new_sl > pos.sl) or (side == Signal.SELL and new_sl < pos.sl)):
                    self.mt5_client.modify_position_sl_tp(pos.ticket, new_sl, pos.tp)
                    self.logger.info("Trailing stop updated for %s to %.2f", pos.ticket, new_sl)

            if rr >= self.config.risk.breakeven_rr and pos.sl != pos.price_open:
                be_sl = entry
                self.mt5_client.modify_position_sl_tp(pos.ticket, be_sl, pos.tp)
                self.logger.info("Moved SL to breakeven for %s", pos.ticket)

    def _calc_trailing_sl(self, side: Signal, price: float, atr: float, position, atr_mult: float) -> Optional[float]:
        if side == Signal.BUY:
            return price - atr * atr_mult
        return price + atr * atr_mult
