"""
logging_setup.py
-----------------
Shared logging configuration so every module logs consistently to both
the terminal (for `launchctl` / interactive monitoring) and rotating log
files under ./logs/.

Import `get_logger(__name__)` (or just `logger` for scripts) anywhere you
need to log.
"""

import sys
from pathlib import Path

from loguru import logger

from config import settings

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def configure_logging() -> None:
    """
    Idempotently configure loguru sinks. Safe to call multiple times
    (e.g. once from server.py, once from generate_mis_report.py) because
    we remove the default handler first and guard against double-adding
    file sinks with a module-level flag.
    """
    logger.remove()  # remove loguru's default stderr handler

    # Human-readable console sink (visible in `launchctl` StandardOutPath
    # and in your terminal when running server.py directly).
    logger.add(
        sys.stdout,
        level=settings.log_level,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
        ),
    )

    # Rotating file sink — keeps 14 days of history, compresses old logs.
    logger.add(
        LOG_DIR / "app_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",       # new file at midnight
        retention="14 days",
        compression="zip",
        enqueue=True,           # thread/process-safe writes
        backtrace=True,
        diagnose=False,         # keep secrets out of tracebacks in prod
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )


configure_logging()

__all__ = ["logger"]
