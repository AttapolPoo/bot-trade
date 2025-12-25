from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from gold_mt5_bot.bot.utils import RiskConfig, Signal


class RiskManager:
    def __init__(self, config: RiskConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.consecutive_losses = 0
        self.daily_anchor = datetime.now(timezone.utc)
        self.daily_loss_limit_hit = False
        self.start_equity: Optional[float] = None

    def _reset_daily(self, now: datetime) -> None:
        if now.date() != self.daily_anchor.date():
            self.consecutive_losses = 0
            self.daily_anchor = now
            self.daily_loss_limit_hit = False

    def _calc_lot(self, balance: float, sl_points: float, tick_value: float, lot_step: float, lot_min: float) -> float:
        risk_amount = balance * self.config.risk_per_trade
        if sl_points <= 0 or tick_value <= 0:
            return 0.0
        raw_lot = risk_amount / (sl_points * tick_value)
        stepped = max(lot_min, (raw_lot // lot_step) * lot_step)
        return round(stepped, 2)

    def build_order_params(self, signal: Signal, tick, symbol_info, atr: float) -> Optional[Tuple[float, float, float]]:
        sl_points = atr * self.config.atr_sl_mult / symbol_info.point
        tp_points = atr * self.config.atr_tp_mult / symbol_info.point
        price = tick.ask if signal == Signal.BUY else tick.bid
        lot = self._calc_lot(
            balance=symbol_info.margin_initial if symbol_info.margin_initial else symbol_info.trade_contract_size,
            sl_points=sl_points,
            tick_value=symbol_info.trade_tick_value,
            lot_step=symbol_info.volume_step,
            lot_min=symbol_info.volume_min,
        )
        if lot <= 0:
            self.logger.warning("Lot calculation resulted in 0; skipping trade")
            return None

        sl_price = price - atr * self.config.atr_sl_mult if signal == Signal.BUY else price + atr * self.config.atr_sl_mult
        tp_price = price + atr * self.config.atr_tp_mult if signal == Signal.BUY else price - atr * self.config.atr_tp_mult

        rr = self.config.atr_tp_mult / self.config.atr_sl_mult if self.config.atr_sl_mult else 0
        if rr < self.config.min_rr:
            self.logger.info("RR %.2f below min %.2f", rr, self.config.min_rr)
            return None
        return lot, sl_price, tp_price

    def should_halt_trading(self, mt5_client, symbol: str, now: datetime) -> Optional[str]:
        self._reset_daily(now)
        account = mt5_client.get_account_info()
        if account is None:
            return "no_account"
        if self.start_equity is None:
            self.start_equity = account.equity
        if self.config.kill_switch.enabled:
            drop_pct = (self.start_equity - account.equity) / self.start_equity if self.start_equity else 0
            if drop_pct >= self.config.kill_switch.equity_drop_pct:
                return "kill_switch_equity_drop"
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        deals = mt5_client.get_daily_deals(day_start, now)
        if deals:
            daily_pl = sum(d.profit for d in deals)
            if daily_pl < 0 and abs(daily_pl) >= account.balance * self.config.max_daily_loss_pct:
                self.daily_loss_limit_hit = True
                return "max_daily_loss"
        if self.daily_loss_limit_hit:
            return "daily_loss_halt"
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return "consecutive_losses"
        return None

    def record_trade_result(self, profit: float) -> None:
        if profit < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
