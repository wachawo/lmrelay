#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Centralized logging configuration, applied at startup and again on reload."""

import logging
import secrets

LOG_FORMAT  = "%(asctime)s.%(msecs)03d [%(levelname)s]: (%(name)s) [%(request_id)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Command output that is data (a status block, a token table) reads as a table
# only without a timestamp and logger name on every row.
PLAIN_FORMAT = "%(message)s"

# Request id for lines outside any request (startup, reload, uvicorn).
NO_REQUEST = "-"

# Name of our handler, so a later call replaces exactly that one.
HANDLER_NAME = "lmrelay"


def new_request_id() -> str:
    """Return a short id that ties one request's log lines together.

    Eight hex characters, not a uuid4: it only correlates nearby lines in one
    file and is never used as a lookup key, so a rare collision is harmless.
    """
    return secrets.token_hex(4)


def supply_request_id(record: logging.LogRecord) -> bool:
    """Give a record without a request id the placeholder, so the format cannot raise.

    Attached to the handler, not a logger: a logger's filter is not consulted for
    records propagated from its children. An id set via `extra=` is kept.
    """
    if not hasattr(record, "request_id"):
        record.request_id = NO_REQUEST
    return True


def setup_logging(level: str = "INFO", plain: bool = False) -> None:
    """Initialize logging. Safe to call again under a live process, as a reload does."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.set_name(HANDLER_NAME)
    handler.addFilter(supply_request_id)
    handler.setFormatter(logging.Formatter(PLAIN_FORMAT if plain else LOG_FORMAT, LOG_DATEFMT))

    # Remove only our previous handler, by name: other root handlers (such as
    # pytest's caplog) must survive a reload.
    root = logging.getLogger()
    for previous in [one for one in root.handlers if one.get_name() == HANDLER_NAME]:
        root.removeHandler(previous)
        previous.close()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Strip uvicorn's own handlers so every line shares the format above.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # The relay logs its own request line. Silenced here rather than via
    # `access_log=False`, which a bare `uvicorn lmrelay.app:app` does not pass.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False

    # httpx logs every outbound request at INFO, duplicating the request line.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    pass


if __name__ == "__main__":
    main()
