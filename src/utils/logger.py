"""Loguru setup."""

from __future__ import annotations

import sys

from loguru import logger


def configure_logging(verbose: bool = False) -> None:
    """Configure default stderr logging."""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")


def get_logger():
    return logger
