from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from gold_mt5_bot import main


class BotRunner:
    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.running = False
        self.last_error: Optional[str] = None
        self.config_path: str = "config.yaml"

    def start(self, config_path: str = "config.yaml") -> None:
        if self.running:
            return
        self.stop_event.clear()
        cfg_path = Path(config_path)
        if not cfg_path.is_absolute():
            cfg_path = Path(__file__).resolve().parent.parent / cfg_path
        self.config_path = str(cfg_path)
        self.thread = threading.Thread(target=self._run_bot, daemon=True)
        self.thread.start()
        self.running = True

    def _run_bot(self) -> None:
        try:
            main.run(self.config_path, stop_event=self.stop_event)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
        finally:
            self.running = False

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        self.running = False

    def status(self) -> dict:
        return {
            "running": self.running,
            "config": self.config_path,
            "last_error": self.last_error,
        }


runner = BotRunner()
