"""Central logging setup.

ScoreForge logs everything from ``DEBUG`` to ``CRITICAL``. The level is
chosen with ``SCOREFORGE_LOG_LEVEL`` (default ``INFO``) and can be turned up
to ``DEBUG`` for troubleshooting. Log records go to stdout and, when
``SCOREFORGE_LOG_FILE`` is set, to a file as well.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOGGER_NAME = "scoreforge"

LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def get_logger(name: str = "") -> logging.Logger:
    """Return a child logger of the ``scoreforge`` logger."""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def resolve_level(level: str | None = None) -> int:
    """Map a level name to its numeric value (unknown names mean INFO)."""
    name = (level or os.environ.get("SCOREFORGE_LOG_LEVEL") or "INFO").upper()
    return LEVELS.get(name, logging.INFO)


def setup_logging(
    level: str | None = None,
    *,
    log_file: str | Path | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure the ``scoreforge`` logger. Safe to call more than once."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured and not force:
        return logger

    logger.setLevel(resolve_level(level))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    target = log_file or os.environ.get("SCOREFORGE_LOG_FILE")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _configured = True
    logger.debug("logging configured at level %s", logging.getLevelName(logger.level))
    return logger
