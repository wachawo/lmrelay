#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command line interface: lmrelay serve, lmrelay init."""

import argparse
import logging
import os
from dataclasses import replace
from importlib import resources
from pathlib import Path

import uvicorn

# Local imports
from lmrelay import __version__
from lmrelay.config import (
    CONFIG_ENV_VAR,
    DEFAULT_LOG_LEVEL,
    HOME_CONFIG_PATH,
    ConfigError,
    check_exposure,
    load_config,
)
from lmrelay.logging_setup import setup_logging

logger = logging.getLogger(__name__)

EXAMPLE_CONFIG_NAME = "lmrelay.toml.example"


def serve_relay(args: argparse.Namespace) -> None:
    """Run the relay under uvicorn."""
    if args.config:
        # uvicorn imports app.py itself, and app.py loads the config again from
        # scratch, so the choice has to travel by environment, not by argument.
        os.environ[CONFIG_ENV_VAR] = str(Path(args.config).expanduser())

    # Loaded here as well so a broken config fails at the command line with a
    # readable message instead of inside a uvicorn worker.
    config = load_config()
    setup_logging(config.log_level)

    if args.host:
        # The app checks the configured host at startup, so an overridden one
        # would otherwise be exposed without the warning.
        exposure_warning = check_exposure(replace(config, host=args.host))
        if exposure_warning:
            logger.warning(exposure_warning)

    uvicorn.run(
        "lmrelay.app:app",
        host=args.host or config.host,
        port=args.port or config.port,
        log_config=None,     # uvicorn must not override our logging
        access_log=False,    # the middleware writes the request line itself
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def init_config(unused_args: argparse.Namespace) -> None:
    """Write the bundled example config to ~/.lmrelay/lmrelay.toml."""
    if HOME_CONFIG_PATH.exists():
        raise ConfigError(f"lmrelay: {HOME_CONFIG_PATH} already exists; not overwriting")
    example = resources.files("lmrelay").joinpath(EXAMPLE_CONFIG_NAME).read_text(encoding="utf-8")
    HOME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOME_CONFIG_PATH.write_text(example, encoding="utf-8")
    # The file is meant to hold a token and provider keys.
    HOME_CONFIG_PATH.chmod(0o600)
    logger.info(f"Wrote {HOME_CONFIG_PATH}. Edit it, then run 'lmrelay serve'.")


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser for both subcommands."""
    parser = argparse.ArgumentParser(
        prog="lmrelay",
        description="Credentialed passthrough in front of a local Ollama.",
    )
    parser.add_argument("--version", action="version", version=f"lmrelay {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the relay")
    serve_parser.add_argument("--host", default=None, help="override [server].host")
    serve_parser.add_argument("--port", type=int, default=None, help="override [server].port")
    serve_parser.add_argument("--config", default=None, help="path to lmrelay.toml")
    serve_parser.set_defaults(handler=serve_relay)

    init_parser = subparsers.add_parser("init", help=f"write {HOME_CONFIG_PATH}")
    init_parser.set_defaults(handler=init_config)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    setup_logging(DEFAULT_LOG_LEVEL)
    try:
        args.handler(args)
    except ConfigError as exc:
        # The message is already written for the operator; a traceback would
        # only bury it.
        logger.error(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
