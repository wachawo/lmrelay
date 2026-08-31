#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Operator-facing error types. The message is shown verbatim, never a traceback."""


class LmrelayError(ValueError):
    """Base for every error whose message is written for the operator."""


class ConfigError(LmrelayError):
    """An unusable lmrelay.toml."""


class StateError(LmrelayError):
    """An unusable or conflicting state.json edit."""


class BundleError(LmrelayError):
    """A bundle that cannot be read, or a transfer that would overwrite something."""


def main():
    pass


if __name__ == "__main__":
    main()
