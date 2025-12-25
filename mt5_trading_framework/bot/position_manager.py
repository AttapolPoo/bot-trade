from __future__ import annotations

from typing import Optional

from bot.utils import Signal


class PositionManager:
    def __init__(self, mt5_client, risk, logger) -> None:
        self.mt5_client = mt5_client
        self.risk = risk
        self.logger = logger

    def can_open(self, symbol: str, max_positions: int) -> bool:
        positions = self.mt5_client.get_positions(symbol=symbol)
        if len(positions) >= max_positions:
            self.logger.info("Max open positions reached for %s", symbol)
            return False
        return True

    def manage(self, symbol: str, tick, atr: float) -> None:
        positions = self.mt5_client.get_positions(symbol=symbol)
        if not positions:
            return
        for pos in positions:
            side = Signal.BUY if pos.type == 0 else Signal.SELL
            price = tick.ask if side == Signal.BUY else tick.bid
            new_sl = self._calc_trailing_sl(side, price, atr, self.risk.trailing_atr_mult)
            if new_sl and ((side == Signal.BUY and new_sl > pos.sl) or (side == Signal.SELL and new_sl < pos.sl)):
                self.mt5_client.modify_position_sl_tp(pos.ticket, new_sl, pos.tp)
                self.logger.info("Trailing stop updated for %s to %.2f", pos.ticket, new_sl)

            entry = pos.price_open
            rr = (price - entry) / (atr * self.risk.atr_sl_mult) if side == Signal.BUY else (entry - price) / (atr * self.risk.atr_sl_mult)
            if rr >= self.risk.breakeven_rr and pos.sl != pos.price_open:
                be_sl = entry
                self.mt5_client.modify_position_sl_tp(pos.ticket, be_sl, pos.tp)
                self.logger.info("Moved SL to breakeven for %s", pos.ticket)

    def _calc_trailing_sl(self, side: Signal, price: float, atr: float, atr_mult: float) -> Optional[float]:
        if side == Signal.BUY:
            return price - atr * atr_mult
        return price + atr * atr_mult
