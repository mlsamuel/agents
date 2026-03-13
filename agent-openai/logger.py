"""
logger.py - Centralised logging configuration.

Import this module once at the top of each entry-point script (pipeline.py,
fine_tune.py) before any other imports that might trigger third-party loggers.

Usage:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("my message")
"""

import logging
import os
import sys

_root = logging.getLogger()
_root.addHandler(logging.NullHandler())
_root.setLevel(logging.WARNING)

_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))

_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

_agents_logger = logging.getLogger("agents")
_agents_logger.setLevel(_level)
_agents_logger.addHandler(_handler)
_agents_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger namespaced under 'agents'.

    Pass __name__ from each module:
        log = get_logger(__name__)
    """
    if name == "__main__":
        return logging.getLogger("agents.__main__")
    if name.startswith("agents"):
        return logging.getLogger(name)
    return logging.getLogger(f"agents.{name}")
