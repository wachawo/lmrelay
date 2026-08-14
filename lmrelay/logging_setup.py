#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Centralized logging configuration. Called once at startup."""

import logging

LOG_FORMAT  = "%(asctime)s.%(msecs)03d [%(levelname)s]: (%(name)s) %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """Initialize logging once. Safe to call multiple times (uses force=True)."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        handlers=[logging.StreamHandler()],
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        level=numeric_level,
        force=True,
    )
    # Uvicorn installs its own handlers and formats; stripping them makes every
    # line in the process share the format above.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # The relay writes its own request line, naming the chosen upstream, so
    # uvicorn's access log would only repeat it. Silenced here rather than via
    # `access_log=False` because that flag is not passed when the app is run as
    # a bare `uvicorn lmrelay.app:app`.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False

    # httpx announces every outbound request at INFO, which duplicates the
    # request line as well.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    pass


if __name__ == "__main__":
    main()
