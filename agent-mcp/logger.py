"""
logger.py - Centralised logging configuration.

Import this module once at the top of each entry-point script (pipeline.py,
mcp_server.py) before any other imports
that might trigger third-party loggers.

Usage:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("my message")
"""

import logging
import os
import sys

# Claim the root logger's handler list with a NullHandler so that
# logging.basicConfig() called by third-party libraries (e.g. FastMCP calls
# configure_logging → basicConfig on __init__) finds handlers already present
# and skips resetting the root level to INFO.
_root = logging.getLogger()
_root.addHandler(logging.NullHandler())
_root.setLevel(logging.WARNING)

# Our own logger hierarchy — "agents.*" — gets a dedicated handler so its
# output is visible regardless of the root level.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))

_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

_agents_logger = logging.getLogger("agents")
_agents_logger.setLevel(_level)
_agents_logger.addHandler(_handler)
_agents_logger.propagate = False  # don't bubble up to the silenced root


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger namespaced under 'agents'.

    Pass __name__ from each module:
        log = get_logger(__name__)

    If __name__ is '__main__' or already starts with 'agents', it is used
    as-is; otherwise it is prefixed with 'agents.'.
    """
    if name == "__main__":
        return logging.getLogger("agents.__main__")
    if name.startswith("agents"):
        return logging.getLogger(name)
    return logging.getLogger(f"agents.{name}")
