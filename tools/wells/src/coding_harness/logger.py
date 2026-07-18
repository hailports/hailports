"""Persistent file logger for Wells. All errors go here with full tracebacks."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _get_log_path() -> Path:
    log_dir = Path.home() / ".wells" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "wells.log"


def _setup() -> logging.Logger:
    log = logging.getLogger("wells")
    if log.handlers:
        return log

    log.setLevel(logging.DEBUG)

    # Rotating file: 1 MB per file, keep 3
    fh = RotatingFileHandler(
        _get_log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    log.addHandler(fh)
    return log


logger = _setup()


def log_error(msg: str, exc: BaseException | None = None) -> None:
    """Log an error message, optionally with a full exception traceback."""
    if exc is not None:
        logger.error(msg, exc_info=exc)
    else:
        logger.error(msg)


def log_warning(msg: str) -> None:
    logger.warning(msg)


def log_info(msg: str) -> None:
    logger.info(msg)


def log_path() -> str:
    return str(_get_log_path())
