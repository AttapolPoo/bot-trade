from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from gold_mt5_bot.bot.utils import LoggingConfig


def setup_logging(config: LoggingConfig) -> logging.Logger:
    Path(config.file).parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("gold_bot")
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    logger.handlers.clear()

    file_handler = RotatingFileHandler(
        config.file,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
