#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Centralized logging configuration. Set at startup, and again by a reload that moves the level."""

import logging
import secrets

LOG_FORMAT  = "%(asctime)s.%(msecs)03d [%(levelname)s]: (%(name)s) [%(request_id)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Command output that is data (a status block, a token table) is a table only
# without a timestamp and a logger name in front of every row. No request id
# either: a command is not serving anybody's request.
PLAIN_FORMAT = "%(message)s"

# What a line that belongs to no request carries: startup, shutdown, a reload,
# and everything uvicorn says on its own account.
NO_REQUEST = "-"

# What this module's own handler is called, so that a second call can find the
# one the last call installed and replace exactly that.
HANDLER_NAME = "lmrelay"


def new_request_id() -> str:
    """A short id for one request, so its lines can be picked out of the log.

    Eight hex characters rather than a whole uuid4. This is one relay writing one
    file, not a trace across services, and the job is to tie an access line to
    the upstream failure it caused a few lines above it. Four random bytes
    collide at around sixty thousand requests by the birthday bound, which in a
    file an operator greps a page of is a coincidence, not a wrong answer, and
    the id is never used to look anything up.
    """
    return secrets.token_hex(4)


def supply_request_id(record: logging.LogRecord) -> bool:
    """Give a record with no request id the placeholder, so the format cannot raise.

    A plain function rather than a logging.Filter subclass: logging has accepted
    a callable here since 3.2, and this needs no state. Attached to the handler
    and not to a logger, because a filter on a logger is not consulted for the
    records its children propagate, and nearly every line here is propagated.

    Never overwrites an id that is already set. The id arrives by `extra=` from
    whatever is serving the request; this only covers the lines that have none,
    which without it would each raise a KeyError inside logging and be printed
    as a formatting error instead of as the line.
    """
    if not hasattr(record, "request_id"):
        record.request_id = NO_REQUEST
    return True


def setup_logging(level: str = "INFO", plain: bool = False) -> None:
    """Initialize logging. Safe to call again under a live process, as a reload does."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    # The filter goes on the handler this call installs, and the loop below drops
    # the handler the last call left, so a reload that moves the level ends with
    # exactly one of ours carrying exactly one copy of the filter.
    handler = logging.StreamHandler()
    handler.set_name(HANDLER_NAME)
    handler.addFilter(supply_request_id)
    handler.setFormatter(logging.Formatter(PLAIN_FORMAT if plain else LOG_FORMAT, LOG_DATEFMT))

    # Ours by name, rather than every root handler. `logging.basicConfig(...,
    # force=True)` said this in one word and said too much: it removes and closes
    # whatever else is attached, and under pytest that is the handler behind
    # caplog. A reload switches the level here and then writes several lines
    # about what it applied, so a test that asserted on any of them was reading
    # an empty capture and passing for the wrong reason.
    root = logging.getLogger()
    for previous in [one for one in root.handlers if one.get_name() == HANDLER_NAME]:
        root.removeHandler(previous)
        previous.close()
    root.addHandler(handler)
    root.setLevel(numeric_level)

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
