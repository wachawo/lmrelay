# Roadmap

What is done, what is being built, what is next, and what was decided against. The last
section is the point of this file: a decision that is not written down gets made again.

## Shipped

- [x] **0.0.1** The relay itself. One config file, no database, no state beyond a token set.
      Process control (`run`, `serve`, `stop`, `restart`, `reload`, `status`), autostart
      through systemd or launchd, caller tokens addressed by id, providers by preset.
- [x] **0.0.2** `token gen` stopped turning authentication on by itself: minting a credential
      and requiring one are two decisions. fail2ban filter and jail under `contrib/`.
      macOS joined CI.
- [x] **0.0.3** The README diagram became an SVG, because PyPI renders no mermaid and showed
      the source instead.
- [x] **0.0.4** `reload` applies `log_level`; the warning about keys that need a restart
      stopped fading after the first reload; a non-numeric `port` became an operator message
      rather than a traceback out of the signal handler; `check_exposure` runs on reload.

## In progress

- [ ] **Limits in three scopes.** Per token, per address, and for the relay as a whole. A
      per-caller cap does not protect the upstream, since callers each inside their own
      limit still arrive together; the global scope is the one that does.
- [ ] **Every setting readable from the environment.** So a container or a unit file
      configures the relay without a config file at all.
- [ ] **`config export` and `config import`.** One file that reproduces a relay elsewhere,
      written 0600 because it carries tokens and provider keys.

- [ ] **`GET /metrics`, in Prometheus text format.** Aggregate only, and built: requests by
      upstream and status, time to first byte as a histogram per upstream, requests in flight,
      refusals by scope and by measure, authentication failures, upstream errors by exception
      type, and the version on a `build_info` gauge. Written by hand rather than with
      `prometheus_client`, because the dependency count is a documented property of this
      project, and it is still four.

      Two decisions that came with it. It requires a credential like everything else rather
      than being exempt as `/healthz` is: `/healthz` tells a caller nothing, `/metrics` tells
      them how the relay is used, and Prometheus supports `bearer_token` in a scrape job.
      And it carries no per-token labels, which keeps `No token accounting, usage database or
      budgets` true and keeps Prometheus cardinality bounded: a label per token would add a
      time series per credential, forever.

      Counters live in memory and reset when the relay restarts. Prometheus understands a
      counter reset, and it is what keeps this from becoming the usage database above. A
      reload does not reset them, unlike the limiters, so a config edit cannot look like a
      restart on a chart.

      The histogram bounds run to 300s rather than the Prometheus default 10s, because a
      local model that has to be read off disk puts almost every answer in `+Inf` under the
      default. Only a request an upstream answered is timed; a refusal is counted and not
      timed, so the relay's own overhead cannot drag the distribution down.

- [ ] **A request id in the log line.** Enough to tie a caller's request to the upstream call
      it caused when both land in `lmrelay.log`. Eight hex characters in a field of the
      format, filled in by a callable filter on the handler, `-` for a line that belongs to no
      request. No dependency. Not a trace id: it is neither read from an inbound header nor
      sent upstream, and it is not returned to the caller.

- [ ] **Old and new values in the reload warning.** It named which of `host`, `port` and
      `connect_timeout` changed, but not to what, so an operator reading the log had to open
      the TOML to find out. Now `port 11435 -> 8080, connect_timeout 10 -> 30`, which cost one
      f-string.

## Next

Nothing agreed. Everything that was is in the section above, built and not yet released.

## Considered and declined

- **A model-aware queue, to stop Ollama thrashing between models.** Measured, and it is not
  a fix. Two models alternating strictly serially, which is a semaphore of one, still paid a
  full cold load on every swap: thrash follows the sequence of model names over time, not
  from requests overlapping. The model name is body-only, and the OpenAI and Anthropic SDKs
  serialise it last, so a bounded prefix goes blind on exactly the long-context requests that
  cost most to schedule. Residency is unknowable to the relay: `/api/ps` is stale on arrival
  and any caller can invalidate it with `"keep_alive": 0`. The documented answer is
  `OLLAMA_MAX_LOADED_MODELS`, which measured 28x on models that fit together and nothing at
  all when VRAM is what binds.

- **Per-token usage accounting persisted in `state.json`.** It would turn a settings file
  into a database with a disk write on the hot path, and it would cost the promise in
  `Not in scope`. Aggregate counters in memory, exposed through `/metrics`, answer the same
  question without either.

- **A fourth limit scope, per upstream.** Buildable: the relay does know the upstream from
  the path prefix. Three scopes were the ask, and the expensive thing, which model a request
  names, stays unknowable either way.

- **Queueing instead of refusing.** A queued caller cannot be told they are queued: no status
  line can precede the upstream's, and with no read timeout the wait is indistinguishable
  from a hang. Refuse immediately or admit immediately.

- **`Retry-After` on the concurrency refusal.** The relay cannot compute it. A slot frees
  when a generation ends, which may be minutes away, and a guessed number would be the one
  dishonest header in a file of honest ones. The rate limit's `Retry-After` is computed from
  when a token actually refills, and it stays.

- **TLS in the relay.** nginx does it better and is already installed where it matters.

- **Dialect translation.** The relay forwards method, path, query and body bytes unchanged.
  Everything else in the README follows from that one sentence.
