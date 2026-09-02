# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.0.8] - 2026-09-02

### Added

- **`lmrelay version`**, the command, beside `--version`, the flag. It prints the same line
  and needs no config file to do it.

## [0.0.7] - 2026-09-01

### Added

- **`requirements.txt` and `requirements-dev.txt`** beside `pyproject.toml`, for the
  `pip install -r` workflow; the dev file adds pytest and ruff.
- **`main.py`** at the repository root, so `python3 main.py run` starts the relay from a
  checkout without installing the package.
- **`.pre-commit-config.yaml`**: ruff and the usual hygiene hooks on every commit, the test
  suite before a push. Install with `pre-commit install --hook-type pre-commit --hook-type pre-push`.

### Changed

- **The fail2ban filter is `contrib/fail2ban/filter.d/lmrelay.conf`**, named like the jail
  beside it, and the jail refers to it as `filter = lmrelay`. If you installed the previous
  `lmrelay-auth.conf`, copy the new file over and drop the old one.

### Fixed

- **A retired `[server]` limit key now points at a key that exists.** `rate_limit` and
  `rate_burst` are refused with a pointer to `[limits.*] rate`, `max_concurrent` with one to
  `[limits.*] concurrent`. The pointer used to name `requests`, which 0.0.6 retired in turn, so
  an operator following it walked into a second refusal.
- The test suite no longer emits `StarletteDeprecationWarning`: starlette 1.6 wants `httpx2`
  behind its `TestClient`, and `requirements-dev.txt` now installs it.

## [0.0.6] - 2026-09-01

### Changed

- **`[limits.*]` takes `concurrent` and `rate`, and they are two independent numbers.**
  `concurrent = 2` beside `rate = "10/30m"` is ten every half hour, of which two may run
  together. The one number `requests` and `period` shared could not say that: it forced the
  cap to equal the rate's count, so "ten every half hour" also meant "ten at once", which on
  a machine holding one model in memory is not what anybody means.

  The rate is one token, `count/period`, on the command line and in the file:

  ```bash
  lmrelay limits set total 1                # one at a time
  lmrelay limits set total 1/60s            # one a minute, and still one at a time
  lmrelay limits set per_address 2 10/30m   # ten every half hour, two at a time
  ```

  A rate on its own still carries a cap of its own count, because "one a minute" said with
  nothing about at-once means one at a time. In the file the two keys are independent, and a
  `rate` with no `concurrent` beside it limits only how often.

  There is still no burst. The bucket holds the rate's own count.

  **To upgrade:** `requests = N` with `period = "P"` becomes `concurrent = N` and
  `rate = "N/P"`, and then set the cap to what you actually want, which is usually lower. A
  config still carrying `requests` or `period` is refused by name at startup rather than
  read as off.

## [0.0.5] - 2026-09-01

### Added

- **Request limits, in three scopes.** `[limits.per_token]`, `[limits.per_address]` and
  `[limits.total]`, one number each: `requests` is how many a caller may have in flight at
  once, and with a `period` it is also how many they may start in that long. A request must
  pass every scope you set and is charged to all of them or to none, so a refusal costs
  nothing anywhere. Refusals are `429`, name the scope that refused, and carry `Retry-After`
  when it can be computed honestly.
- **`lmrelay limits set <scope> <requests> [period]`.** `limits set total 1` is one at a
  time; `limits set total 1 60s` is one a minute; `limits set per_address 10 30m` is ten
  every half hour. It edits the assignment in `lmrelay.toml` and leaves the rest of the file
  alone, comments included, and signals a running relay.
- **`GET /metrics`, in Prometheus text format.** Requests by upstream and status, time to
  first byte as a histogram, requests in flight, refusals by scope and measure,
  authentication failures, upstream errors by exception type, and the version on a
  `build_info` gauge. Aggregate only: no label names a caller. Requires a credential like
  every other route.
- **`lmrelay export` and `lmrelay import`.** One TOML file that reproduces a relay
  elsewhere, written `0600` because it carries caller tokens and provider keys. With no path
  the bundle goes to stdout and is read from stdin, so `lmrelay export | ssh other-host
  lmrelay import` is the whole of moving a relay.
- **A request id in each log line**, tying a caller's request to the upstream call it caused.
- **A `limits` line in `lmrelay status`**, naming every scope that asks for anything.

### Changed

- **Refusals from the framework now carry the relay's own shape.** `TRACE /api/tags` answered
  `{"detail": "Method Not Allowed"}`; it now answers
  `{"error": "lmrelay: method not allowed for TRACE /api/tags"}`, like every other refusal.
- **The reload log gives old and new values** for the settings a reload cannot apply:
  `port 11435 -> 8080, connect_timeout 10 -> 30`.
- **`lmrelay reload` names in the terminal what a reload cannot apply.** Editing `port` and
  reloading printed `signalled` and left the relay on the old port, with the reason in
  `lmrelay.log`. It now says `port 11435 -> 8080 ... restart to apply` where you typed the
  command. The pidfile records the `connect_timeout` the process started with, alongside the
  address it bound, so all three can be named.
- **The shipped fail2ban filter matches the request id** that log lines now carry. An
  installed copy from 0.0.2 or later stops matching after this upgrade and bans nobody.
  Copy `contrib/fail2ban/filter.d/lmrelay-auth.conf` over your own and reload fail2ban.

### Removed

- **`$LMRELAY_TOKEN` is no longer read.** It was an additional valid caller credential in
  0.0.4. Settings now come from `lmrelay.toml` and `state.json` and from nowhere else, so
  that the file says what the relay does. `$LMRELAY_CONFIG` and `$LMRELAY_STATE` still name
  which files to read, and `${VAR}` in a header value still keeps a provider key out of the
  config.

  **To upgrade:** run `lmrelay token add "$LMRELAY_TOKEN"`, or write the value into
  `[auth] token` in `lmrelay.toml`, before dropping the variable.

## [0.0.4] - 2026-08-31

### Fixed

- `lmrelay reload` applies `log_level`.
- The warning about settings that need a restart stopped fading after the first reload.
- A non-numeric `port` is an operator message rather than a traceback out of the signal
  handler.
- The exposure check runs on reload, not only at startup.

## [0.0.3] - 2026-08-31

### Changed

- The README diagram is an SVG. PyPI renders no mermaid and was showing the source.

## [0.0.2] - 2026-08-31

### Changed

- `lmrelay token gen` no longer turns authentication on by itself. Minting a credential and
  requiring one are two decisions; `lmrelay auth true` makes the second.

### Added

- A fail2ban filter and jail, under `contrib/`. The jail ships disabled.
- macOS in CI.

## [0.0.1] - 2026-08-31

### Added

- The relay: one config file, no database, no state beyond a token set. Streams are passed
  through byte for byte, with no read timeout.
- Process control: `run`, `serve`, `stop`, `restart`, `reload`, `status`.
- Autostart through a systemd user unit or a launchd agent: `enable`, `disable`.
- Caller tokens addressed by id, and providers by preset: `token`, `provider`, `auth`.

[Unreleased]: https://github.com/wachawo/lmrelay/compare/0.0.8...HEAD
[0.0.8]: https://github.com/wachawo/lmrelay/compare/0.0.7...0.0.8
[0.0.7]: https://github.com/wachawo/lmrelay/compare/0.0.6...0.0.7
[0.0.6]: https://github.com/wachawo/lmrelay/compare/0.0.5...0.0.6
[0.0.5]: https://github.com/wachawo/lmrelay/compare/0.0.4...0.0.5
[0.0.4]: https://github.com/wachawo/lmrelay/compare/0.0.3...0.0.4
[0.0.3]: https://github.com/wachawo/lmrelay/compare/0.0.2...0.0.3
[0.0.2]: https://github.com/wachawo/lmrelay/compare/0.0.1...0.0.2
[0.0.1]: https://github.com/wachawo/lmrelay/releases/tag/0.0.1
