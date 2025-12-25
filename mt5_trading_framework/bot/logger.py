from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.utils import LoggingSettings


def setup_logging(config: LoggingSettings) -> logging.Logger:
    Path(config.file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mt5_framework")
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(
        config.file, maxBytes=config.max_bytes, backupCount=config.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
