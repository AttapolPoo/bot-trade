from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Tuple

from gold_mt5_bot.bot.utils import ExecutionConfig, Signal


class ExecutionManager:
    def __init__(self, mt5_client, config: ExecutionConfig, logger) -> None:
        self.mt5_client = mt5_client
        self.config = config
        self.logger = logger
        if self.config.journal_csv:
            Path(self.config.journal_csv).parent.mkdir(parents=True, exist_ok=True)

    def _journal(self, symbol: str, signal: Signal, lot: float, price: float, sl: float, tp: float, result: str) -> None:
        if not self.config.journal_csv:
            return
        exists = Path(self.config.journal_csv).exists()
        with open(self.config.journal_csv, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if not exists:
                writer.writerow(["timestamp", "symbol", "side", "lot", "price", "sl", "tp", "result"])
            writer.writerow([time.time(), symbol, signal.value, lot, price, sl, tp, result])

    def execute_trade(self, symbol: str, signal: Signal, tick, symbol_info, params: Tuple[float, float, float]) -> None:
        lot, sl, tp = params
        price = tick.ask if signal == Signal.BUY else tick.bid
        lot = self.mt5_client.normalize_volume(symbol_info, lot)
        sl, tp = self.mt5_client.validate_stops(symbol_info, signal.value, price, sl, tp)
        price = self.mt5_client.normalize_price(symbol_info, price)
        if self.config.dry_run:
            self.logger.info("[DRY-RUN] %s %s lot=%.2f price=%.2f sl=%.2f tp=%.2f", signal.value, symbol, lot, price, sl, tp)
            self._journal(symbol, signal, lot, price, sl, tp, result="dry_run")
            return

        attempt = 0
        while attempt <= self.config.retries:
            try:
                order = self.mt5_client.place_order(
                    symbol=symbol,
                    side=signal.value,
                    lot=lot,
                    sl=sl,
                    tp=tp,
                    magic=self.config.magic,
                    deviation=self.config.deviation,
                    comment=self.config.comment,
                )
                self.logger.info("Order sent: %s", order)
                result_code = order.get("result", {}).get("retcode") if isinstance(order, dict) else "sent"
                self._journal(symbol, signal, lot, price, sl, tp, result=str(result_code))
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Order attempt %s failed: %s", attempt + 1, exc)
                attempt += 1
                time.sleep(self.config.retry_sleep)
        self.logger.error("All order attempts failed for %s", symbol)
        self._journal(symbol, signal, lot, price, sl, tp, result="failed")
