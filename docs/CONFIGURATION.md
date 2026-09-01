# Configuration and Errors

The [README](https://github.com/wachawo/lmrelay/blob/main/README.md) covers installation and
usage. This document covers the config file, the state file, autostart, the limits,
moving a whole configuration between machines, what a Prometheus scrape of `/metrics`
holds, how to read `lmrelay.log`, the behaviour that is not obvious from the outside, what
every message the relay prints means, and how to have fail2ban act on refused credentials.

**A setting is a path, and a path has two spellings.** `limits.total.requests` is
`[limits.total] requests` in the file and `"limits": {"total": {"requests": …}}` in an
export. Learn the path once and both follow from it.

**There is no third spelling in the environment.** Settings come from `lmrelay.toml` and
from `state.json`, and from nowhere else. `$LMRELAY_CONFIG` names which file to read, and
that is the whole of what the environment decides.

## Files on disk

Four files live in that config's directory:

| File | Written by | Holds |
|---|---|---|
| [`lmrelay.toml`](https://github.com/wachawo/lmrelay/blob/main/lmrelay/lmrelay.toml.example) | you | server settings, limits and hand-written upstreams |
| `state.json` | the CLI | caller tokens, the auth switch, CLI-added providers |
| `lmrelay.pid` | the relay | the pid, the address it bound, and its `connect_timeout` |
| `lmrelay.log` | the relay | stdout and stderr of a detached relay |

The split exists so that the CLI never has to rewrite a file you are editing: your comments
in `lmrelay.toml` survive forever. State is JSON rather than a second TOML file because it
is machine-owned, and because `tomllib` reads but cannot write.

Two commands write `lmrelay.toml`, and neither of them rewrites it. `lmrelay limits set`
replaces the value on one assignment and leaves every other byte of the file alone,
comments and all. `lmrelay import` replaces the whole file, and moves the existing
pair to `lmrelay.toml.bak` and `state.json.bak` before it does. Everything else the CLI
writes goes to `state.json`.

`lmrelay init` writes `lmrelay.toml` with mode 0600, because the file is meant to hold
provider keys. So do `import` and `export`.

## Where the config is looked for

The config is looked for in three places, first hit wins, no merging:
`$LMRELAY_CONFIG` (also what a command's `--config PATH` sets), then `./lmrelay.toml`,
then `~/.lmrelay/lmrelay.toml`.

If none exists the relay refuses to start, rather than serving 404s from an empty
configuration. Run `lmrelay init`.

`state.json` is looked for beside whichever config was found, or at `$LMRELAY_STATE` when that
is set.

## The config file

```toml
# host, port and connect_timeout are read at startup only; changing any of them
# needs 'lmrelay restart'. Everything else is picked up by 'lmrelay reload'.
[server]
host             = "127.0.0.1"
port             = 11435
default_upstream = "ollama"
connect_timeout  = 10
log_level        = "INFO"

# Three scopes, one number each, every one 0 (off) by default. 'requests' is
# how many at once; with a 'period' it is also how many in that long. A request
# must pass every scope you set, and is charged to all of them or to none of
# them. If you set one number, set [limits.total].

# Per credential. Skipped entirely with auth off, since there is no credential.
[limits.per_token]
requests = 2
period   = "120s"

# Per client address. The only scope that identifies anyone with auth off, and
# only as trustworthy as whatever sets X-Forwarded-For in front of the relay.
[limits.per_address]
requests = 4
period   = "60s"

# The relay as a whole, whoever is asking. This is the one that protects the
# upstream: ten callers each inside their own limit still arrive together.
[limits.total]
requests = 6
period   = "0s"

# One extra valid caller credential, for a non-interactive install. It does not
# turn checking on; only 'lmrelay auth true' does.
[auth]
token = "CHANGE-ME"

# Local Ollama, exactly where it already listens. Needs no credential, so it
# has no headers at all.
[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"

# OpenAI. Wants a bearer token.
[upstream.openai]
base_url = "https://api.openai.com"
dialect  = "openai"
headers  = { Authorization = "Bearer ${OPENAI_API_KEY}" }

# Anthropic. Wants x-api-key plus a dated version header, and speaks its own
# dialect on /v1/messages, not /v1/chat/completions.
[upstream.anthropic]
base_url = "https://api.anthropic.com"
dialect  = "anthropic"
headers  = { "x-api-key" = "${ANTHROPIC_API_KEY}", "anthropic-version" = "2023-06-01" }

# DeepSeek and Grok are OpenAI-compatible on the wire.
[upstream.deepseek]
base_url = "https://api.deepseek.com"
dialect  = "openai"
headers  = { Authorization = "Bearer ${DEEPSEEK_API_KEY}" }

[upstream.grok]
base_url = "https://api.x.ai"
dialect  = "openai"
headers  = { Authorization = "Bearer ${XAI_API_KEY}" }
# 'lmrelay provider add openai sk-...' writes these same three fields into
# state.json, with the preset base URL, dialect and header shape.
```

The complete commented file ships as
[`lmrelay/lmrelay.toml.example`](https://github.com/wachawo/lmrelay/blob/main/lmrelay/lmrelay.toml.example)
and is what `lmrelay init` copies. Two things differ there from the sample above, and both
are so that a freshly written config starts and refuses nobody: the hosted blocks are
commented out, so a fresh config has Ollama alone, and every limit is `0`. Uncomment a
hosted block once its variable is exported; an unset `${VAR}` is a startup error.

## Schema notes

- `headers` is an arbitrary string-to-string table, sent onward verbatim. There is no
  `api_key` field and no per-provider code path: OpenAI's bearer, Anthropic's two headers
  and Ollama's nothing are all the same mechanism, so adding a provider is four lines of
  TOML.
- `${VAR}` in any header **value** is expanded from the process environment, so keys can
  stay out of the file under Docker or systemd. An unset variable is a startup error that
  names the variable.
- `base_url` is prepended verbatim to the forwarded path, so it is normally a host root and
  never ends in `/v1`, because the caller's path already carries that. A path prefix is allowed
  and simply concatenated, which is what makes an endpoint hosted under a subpath work
  without any code.
- `dialect` (`ollama`, `openai` or `anthropic`, default `openai`) never changes what is
  sent. Its only job is the refusal in the [Errors](#errors) table below.
- TOML forbids `-` in bare keys, so `"x-api-key"` and `"anthropic-version"` are quoted
  while `Authorization` is not. That is not a typo.
- An upstream may not be named `api` or `v1`; either would shadow the path root every
  Ollama and OpenAI client already sends to, and the breakage would be hard to diagnose.
- There is no auth switch in this file. An `[auth] token` is accepted as one *additional*
  valid caller credential, so an install that never runs the CLI can write one down without
  invalidating the tokens in `state.json`; it does not turn checking on. Caller tokens are
  otherwise `lmrelay token …`, and the switch is `lmrelay auth true|false`.
- A provider added with `lmrelay provider add` wins over an `[upstream.<name>]` of the same
  name. The startup log names any upstream that was overridden, since this file is hand-written
  and its author deserves to hear that a command shadowed it.
- `[limits.<scope>]` takes exactly two keys, `requests` and `period`, in each of three
  scopes, `per_token`, `per_address` and `total`. Uniform on purpose: a table with a rate in
  one scope and a count in another is a second thing to learn and the first thing to get
  wrong. One sentence covers it, and it is in the [Limits](#limits) section below.
## Caller tokens and the auth switch

Auth is off in a fresh state, so on loopback a new install is a transparent proxy in front
of Ollama. `lmrelay auth true` requires a credential and `lmrelay auth false` reopens the
relay; nothing in `lmrelay.toml` moves that switch.

```bash
lmrelay token gen --label laptop   # prints the token once
lmrelay auth true                  # now start requiring it
lmrelay token list                 # masked unless --show
lmrelay token delete 1             # by the id token list prints
```

`token gen` prints the token in full once and never again. It does not turn auth on: minting
a credential and requiring one are two decisions, and a relay already serving other callers
should not start refusing them because a token was created. It does say that auth is still
off and names the command that changes it, so nobody is left believing the relay closed
itself. The config loader repeats that warning on every start for as long as tokens exist
and none of them is required. `auth true` with no tokens is refused the other way round,
because it would 401 every request including yours.

`[auth] token` is accepted as one *additional* valid caller credential, so an install that
never runs the CLI can write one down without invalidating the tokens in `state.json`. It
does not turn checking on, and it counts towards the token set that `auth true` requires.

The switch itself is `lmrelay auth true|false`, and it lives in `state.json`. Nothing in
`lmrelay.toml` moves it.

`token list` masks tokens unless `--show` is passed. Token ids are monotonic, so an id keeps
meaning the same token after an unrelated delete.

## Limits

A setting is a path, and a path has two spellings. `limits.total.requests` is
`[limits.total] requests` in the file and `"limits": {"total": {"requests": …}}` in an
export. Learn the path once.

**Three scopes, one number each, `0` (off) by default.**

```bash
lmrelay limits set total 1              # one at a time
lmrelay limits set total 1 60s          # one a minute, and still one at a time
lmrelay limits set per_address 10 30m   # ten every half hour
lmrelay limits set per_token 1 120s
lmrelay limits set total 0              # off
```

| | `requests` | `period` |
|---|---|---|
| `[limits.per_token]` | in flight at once for one credential, and per period | how long that period is, or `"0s"` |
| `[limits.per_address]` | the same, for one client address | the same |
| `[limits.total]` | the same, for the relay as a whole | the same |

**`requests` is how many a caller may have in flight at once. Add a `period` and the same
number is also how many they may start in that long.** `requests = 10` with
`period = "30m"` is ten every half hour, of which ten may arrive together.

One number rather than three. "Ten at a time" and "ten every half hour" are the same
operator saying how much of this machine one caller gets, and the table that spelled them
separately, with a burst beside them, was three numbers that could disagree with each other
and one of them, the burst, whose only job was to be wrong. There is no burst now: the
bucket holds `requests`, which is what "ten a minute" means to the person who wrote it.

A period is a whole number and a unit, one of `s`, `m` or `h`. A bare number is refused,
because `period = 30` is half an hour to whoever wrote it about as often as it is half a
minute, and the unit costs one character.

The rate half is a token bucket rather than a counter per fixed window, because a window
lets a caller spend its whole allowance in the last instant of one window and again in the
first instant of the next, which is twice the rate across the boundary and exactly the
burst a limit is meant to prevent.

### Setting them from the command line

`lmrelay limits set <scope> <requests> [period]` writes into `[limits.<scope>]` in the
config file and signals a running relay, so the change is live without a restart.

It reports the scope before and after, in the words `status` prints and the reload log
uses, so there is nothing to go and check afterwards:

```
[limits.total] off -> 10 per 30m, 10 at once in /home/you/.lmrelay/lmrelay.toml.
Signalled the running relay to re-read it (SIGHUP).
```

**It sets a scope, not a key.** Running it again without a period clears the period, so the
same command means the same thing on every machine.

**It edits, it does not rewrite.** Only the two assignments change; the comments, the key
order, the alignment and any note you wrote beside a number all stay exactly as they were.
A key the scope has not got is added to it, and a scope the file has not got is appended
whole. **And the period is written as you typed it**: `60s` does not come back as `1m`, in
the file or in a refusal a caller sees.

**Nothing is written until the result has been checked.** The edit is made to a string, and
that string is parsed and confirmed to carry the numbers you asked for before it reaches
the disk. So a file this cannot edit, `total = { requests = 1 }` written as an inline table
say, ends as a refusal with the file untouched rather than as a config the relay will not
start from. Edit that one by hand.

Values go through the same readers the file goes through, so a bad number is refused in the
words the same number in the TOML is refused in. There is no `limits show`: `lmrelay status`
prints what is in force, which is the same question asked of the relay rather than of the
file.

### If you set one number, set `[limits.total]`

That is the one that protects the machine. The other two apportion what it admits.

**A per-caller cap does not protect the upstream.** Ten callers each inside their own limit
still arrive together, and the per-caller scopes cannot see that they did. Only a limit on
the relay as a whole can.

What arriving together costs is not a refusal. **Ollama never refuses for lack of memory.**
It evicts a resident model, loads the one this request names, and makes the caller wait,
sending no byte at all, not even response headers, until the model is loaded and the first
token is produced. Measured here: **0.73s warm against 11.9s cold on a 3B**, and about
**17s** to swap an 8B in. The [Ollama section](#when-ollama-has-no-room-to-load-a-model)
below has the full treatment, and the number to size `[limits.total] requests` against.

**Without `[limits.per_token]` beside it, `total` is first come first served**, and one
client with fifty threads owns all of it. That is the whole argument for having more than
one scope. So: **set `total` for the machine, `per_token` for fairness.**

Two findings from the same investigation are worth having here, because each one stops an
operator configuring against a problem they do not have:

- **Concurrency on one model was never the problem.** Six simultaneous callers for one cold
  model cost **one** load, shared between all six.
- **No limit fixes thrashing.** Two models that cannot coexist paid a full reload on every
  swap even when the requests were strictly serialised, one at a time, never overlapping.
  What costs is the sequence of model names over time, not the overlap between requests.
  `OLLAMA_MAX_LOADED_MODELS` is the answer to that, and it is not a relay setting.

### They apply together, and nothing wins

A request passes every configured scope or it is refused. There is no precedence and no
override, and a generous scope cannot rescue a request a tighter one refused.

The scopes protect different things, which is why a precedence rule would let the wrong one
lose. `total` protects the upstream; `per_token` and `per_address` apportion what `total`
admits. If a generous `per_token` could win, configuring fairness would disable the
protection.

They are also not alternatives. With auth on, one request is charged against its token, its
address **and** the total, all three. That is not double counting, it is passing three
ceilings: leave the ones you do not want at `0`.

### A refused request costs nothing anywhere

Admission is all or nothing, in three phases:

1. Every configured rate limiter is asked what it would do, spending nothing.
2. The in-flight slots are taken, and any slot already taken is given back if a later scope
   refuses.
3. Only once every scope has said yes is every rate bucket charged.

So a caller refused by `total` does not also have its own bucket drained. Without this, a
refusal would leave an operator with "I was refused, and now I am rate limited as well" and
no way to see why one caused the other.

The same set of slots comes back through one release, which is called from the body
generator's `finally`, so a streamed answer holds every slot it took until its last byte and
gives all of them back together. That release is idempotent, because a slot released twice
would decrement a count belonging to another live request and let that caller past the cap.

### The refusal names the narrowest scope

Scopes are evaluated `per_token`, then `per_address`, then `total`, and the first to refuse
is the one named. When several would refuse at the same instant, the caller is told the most
specific true thing, which is also the number their operator raises first. Being told "the
relay is full" while you personally are the reason is the wrong answer even though it is
true.

### What a refused caller sees

**429 for all six**, and never 503. Nothing is wrong with the relay or with the upstream,
and the same request from another caller is served at that instant. A 503 would say the
service is unavailable, which is untrue, and it is the status clients retry hardest against.

The message names the scope and the key, because `429` on its own leaves an operator
guessing which of six numbers to raise:

```json
{"error": "lmrelay: rate limit exceeded for your token: 2 per 120s ([limits.per_token])"}
```

```text
lmrelay: rate limit exceeded for your token: 2 per 120s ([limits.per_token])
lmrelay: rate limit exceeded for your address: 4 per 60s ([limits.per_address])
lmrelay: the relay's rate limit is exceeded: 6 per 1h ([limits.total])
lmrelay: your token already has 2 requests in flight ([limits.per_token]); one of yours must finish first
lmrelay: your address already has 4 requests in flight ([limits.per_address]); one of yours must finish first
lmrelay: the relay is already carrying 6 requests ([limits.total]); one of them must finish first
```

The last one says "one of **them**", not "one of yours". At the total scope the request that
has to end may be anybody's, and telling a caller to wait for something of their own that
does not exist would be a wrong answer arriving at a new scope.

`Retry-After` is set on the three rate refusals and computed from the scope that refused: it
is the time until one token has refilled in **that** bucket, rounded up to whole seconds
because the header takes no fractions and rounding down would invite a retry that is refused
again.

There is deliberately **no `Retry-After` on the three concurrency refusals**. A slot frees
when a model finishes answering, there is no read timeout, and with none the relay does not
know when that will be. A guessed number would be a lie, and three scopes cannot guess it
any better than one could.

The period is quoted **in the spelling the operator wrote**. `60s` is not answered with
`1m`: this is the one place a caller ever sees the limit at all, and quoting it back in a
spelling nobody used sends them asking their operator about a setting that is not there.

### With auth off, `per_token` is skipped rather than refused

With auth off there is no credential, so `per_token` matches nothing: no bucket is created
and no slot is held. `per_address` and `total` still apply, and `per_address` is doing the
whole job.

Configuring `per_token` with auth off is legal, because turning auth on later makes it live.
It is said once at startup rather than doing nothing quietly:

```text
lmrelay: [limits.per_token] is configured but auth is off, so nothing is keyed by a token.
[limits.per_address] and [limits.total] still apply. Run 'lmrelay auth true'.
```

With auth on there is no third case: every request has passed auth, so every request has a
token.

Limits are checked **after** authentication, one rule with no exception, so a guessed
credential never spends the allowance of the caller being guessed at. The consequence is
that credential guessing is not rate limited at all. Charging the address bucket before auth
was considered and dropped: it would make the rule "the address scope is before auth and the
other five are after", and a forged `X-Forwarded-For` defeats it anyway, which the
[fail2ban section](#the-jail-ships-disabled) already measures.

**The address key is only as trustworthy as whatever is in front of the relay.** The relay
runs uvicorn with `proxy_headers` on and every forwarder trusted, so the address it counts
against is taken from `X-Forwarded-For` when that header is present. Behind an nginx that
sets it, that is the point: the count follows the real client rather than the proxy.
Reachable directly by anyone, it is a header the caller writes, and rotating it defeats
`per_address` entirely. Measured, with auth off and `rate = 1`: three requests with no header
gave `200, 429, 429`, while six requests each carrying a different made-up `X-Forwarded-For`
all gave `200`, and left six buckets behind. `[limits.total]` is the scope that forgery
cannot move, which is another reason to set it.

So `per_address` bounds a cooperating client, not an adversarial one. If the per-caller
scopes have to hold against callers you do not trust, turn auth on and let them key on the
token, which a caller cannot choose.

### Admission happens in one place, in the relay route

The whole decision is one call, made after the upstream has been selected and after the
dialect check. Nothing about limits happens in the middleware, which keeps authentication
and the access log. Three things follow:

- **Every refusal names its upstream** in the access log. There is no longer one shape for a
  rate refusal and another for a concurrency refusal.
- **Nothing forwarded is nothing charged.** A dialect refusal spends nothing, where it used
  to spend a rate token. One rule instead of an exception.
- **`GET /healthz` is exempt structurally**, by being a different route rather than by a
  path-and-method check.

The cost, stated because it is a choice: a client looping against a wrong-dialect path is no
longer rate limited. It costs the relay microseconds per 400 and cannot touch a model, and
fail2ban is the answer if it ever matters.

Refusals are one line each in the access log, naming the kind and the scope:

```text
203.0.113.7 POST /api/chat -> ollama: 429 (rate, per_token)
203.0.113.7 POST /api/chat -> ollama: 429 (concurrent, total)
```

The fail2ban filter that ships with the source matches neither. A rate-limited caller is a
misconfigured client far more often than an attacker, and it is already being refused.

### A slot is held until the last byte

A concurrency slot is released when the response **body ends**, not when its headers arrive.
That is the whole point: the relay streams, so a request whose headers came back in 30ms may
still be delivering tokens two minutes later, and a slot freed at the headers would bound
nothing. A client that hangs up mid-stream releases its slots too.

The relay never cuts a stream to reclaim a slot. Admission is the only lever, because
interrupting a response in flight would break the guarantee the rest of this document rests
on.

Nor is "in flight" the same as "receiving something". A request waiting its turn inside
Ollama, with `OLLAMA_NUM_PARALLEL` already spent, holds its relay slot the whole time it is
receiving nothing. That is the intent, since it is occupying the upstream either way, but it
is what makes `[limits.total]` above
`OLLAMA_NUM_PARALLEL x OLLAMA_MAX_LOADED_MODELS` buy nothing: past that point Ollama queues
internally, and the queued request holds a relay slot while it waits. Set it below that
product and the relay refuses work Ollama could have done.

### The counts live in this process, in memory

There is no database, no Redis and no shared state. That has consequences worth stating:

- **More uvicorn workers are not a way around it, because the relay does not run that way.**
  Started with `--workers 3`, the first worker to reach the pidfile claims it and the rest
  refuse with `already running`, at which point uvicorn stops the parent too. The result is no
  relay at all rather than three sets of counts, so the number you wrote is the number one
  caller gets.
- **Two relays are.** Separate processes, separate config directories, separate counts: a
  caller that can reach both has both allowances. That now includes `[limits.total]`, which is
  worse than it was when only per-caller limits existed, because `total` is the scope
  protecting the machine and a pair of relays doubles it. If something is balancing across a
  pair, either give each the whole limit and accept the doubling, or put the limit in front of
  them.
- **A restart forgets everything.** Every caller starts with a full bucket and no slots held,
  in every scope.
- **The tables do not grow without bound.** The concurrency counter holds an entry only while
  that caller has something in flight, and the last release deletes it. The bucket tables need
  a sweep, which runs at most once a minute and drops any caller idle long enough that its
  bucket has refilled to full: forgetting a full bucket changes no decision, because a caller
  with no bucket starts with one. A bucket that has *not* refilled is kept however idle it is,
  since dropping that one would be handing back an allowance nobody waited for. So a table
  holds roughly the callers seen in the last five minutes, not every address that ever
  knocked. `total` is one bucket and one count, so it has nothing to sweep.
- **A reload applies new numbers without disturbing anything in flight.** Changing a scope's
  numbers rebuilds its bucket table, which starts every caller full, so it is done only when
  that scope actually moved rather than on every unrelated reload. The in-flight counter
  needs no such care: the cap is read per request, so a live request keeps the slots it holds
  and the new number applies to the next arrival.

### Reading back what is in force

`lmrelay status` prints one `limits` line, naming every scope that asks for anything and
both halves of any scope that has a period, since the one number is doing two jobs:

```text
auth         on, 2 tokens
limits       per_address 4 per 60s, 4 at once; total 6 at once
```

A relay with nothing set says `limits       off` once, rather than three lines saying off
about scopes nobody configured. It is the same question either way: is anything limited here,
and by how much. Before this line the only way to read the effective numbers was
`lmrelay export -`, which is the whole configuration and a file full of credentials.

### The keys that used to be here

`[server] rate_limit`, `[server] rate_burst` and `[server] max_concurrent` are gone, and so
are `rate`, `burst` and `concurrent` inside a `[limits.*]` scope. None of them was ever in a
published release, so nothing is being broken, and each is **refused** rather than ignored:

```text
lmrelay: [server] rate_limit was replaced by [limits.per_token] requests,
[limits.per_address] requests, [limits.total] requests. Pick the scope you meant; see
docs/CONFIGURATION.md.

lmrelay: [limits.total] rate, burst were replaced by requests and period. 'requests' is how
many at once, and with a 'period' it is also how many in that long: requests = 10,
period = "30m". See docs/CONFIGURATION.md.
```

Silently ignoring one would leave an operator believing a limit is on when it is off, which
is the exact failure this shape exists to avoid. The refusal is due to be deleted after
0.0.5.

For the same reason, a scope or a key inside `[limits]` that lmrelay does not recognise is
refused and named rather than ignored. A misspelt `[limits.per_tokens]` is indistinguishable
from a limit that is on, right up to the moment it fails to refuse anybody.

`lmrelay limits set` writes into that same file rather than into `state.json`, which is why
it edits one assignment instead of rewriting the file: limits are settings, settings live in
`lmrelay.toml`, and a limit stored in the state would shadow the `[limits.*]` table without
appearing in it.

## When Ollama has no room to load a model

Ollama keeps models resident and evicts one to load another. Interleaving requests for models
that cannot coexist costs a full reload every time, and with enough interleaving the machine
spends its time loading rather than answering.

**Ollama never refuses for lack of memory.** It evicts, loads, and makes the caller wait, and
it sends no byte at all in the meantime, not even response headers, until the model is loaded
and the first token has been produced. So this cost never appears as an error anywhere: it
appears as a request that took twenty times as long as the identical one before it.

The tax is large. Measured here, a 3B answered in 0.73s warm and 11.9s cold, and swapping an
8B in cost about 17s; an earlier run of smaller models put the same pair of figures at
0.18-0.65s warm against 5.9-7.0s cold. Treat the ratio as the finding and the seconds as this
machine's: in one run of two models alternating, individual swaps of the same pair ranged from
8.1s to 13.7s, and a "cold" load after an unload still reads the weights out of the page
cache, so a first load after boot is worse again. Ollama's own `load_duration` under-reports
what the caller waits by 3.6-5.1x, because it excludes the queueing and the prompt evaluation
around it. Do not size anything from it.

### First find out which of the two constraints you are hitting

This matters more than any setting below, because the usual advice fixes one of them and does
nothing whatever for the other. Send a request for each model in rotation, then ask
`curl 127.0.0.1:11434/api/ps` what stayed:

- **More than one model resident, but fewer than you rotate through:** the model *count*
  binds. The table below is exactly the fix.
- **One model resident, however high you set the count:** *VRAM* binds. The models you are
  alternating cannot coexist on the card at the contexts you are asking for, and no Ollama
  setting will make them. The table below will not help, and half of it will hurt.

**When the count binds, raise it:**

| Variable | Set it to |
|---|---|
| `OLLAMA_MAX_LOADED_MODELS` | at least the number of models in regular rotation |
| `OLLAMA_NUM_PARALLEL` | how many callers one model serves at once, but see the warning below |

Measured, three small models that comfortably fit together, alternating strictly serially:
with `OLLAMA_MAX_LOADED_MODELS=2` each request cost 5.10s on average and one model was evicted
per cycle; at `3` it cost 0.18s and nothing was evicted. That is a 28x difference, and it is
the whole of what this setting does.

**When VRAM binds, it does nothing.** Same test with two models that cannot coexist, an 8B at
16k context and a 3B at 32k on a 12GB card: `OLLAMA_MAX_LOADED_MODELS=0` (the default) cost
10.08s per swap, and `4` cost 9.88s. Two percent apart, with one model resident either way.
The count was never the constraint, so raising it changed nothing.

> **`OLLAMA_NUM_PARALLEL` makes the VRAM case worse, not better.** Each parallel slot gets its
> own KV cache, so raising it makes every model larger and coexistence strictly less likely.
> Measured on the same card: at `1` the 8B held 9.57 GB and the 3B 4.84 GB; at `2` they held
> 10.34 GB and 7.23 GB. The 3B grew by 49%. Read the table as two independent knobs, and turn
> this one up only when a single model is serving several callers at once.

If VRAM is what binds, the levers are smaller contexts (`num_ctx` is usually the largest term
and the easiest to overpay for), smaller quantisations, fewer models in rotation, or a bigger
card. None of them are relay settings.

`ollama serve` prints both variables at startup, which is the quickest way to see what they
resolved to. Note that an unset `OLLAMA_MAX_LOADED_MODELS` prints as `0`, not as the number it
will use: `0` is the sentinel for "decide automatically", which Ollama resolves internally to
3 per GPU and never prints. So `OLLAMA_MAX_LOADED_MODELS:0 ... OLLAMA_NUM_PARALLEL:1` is what
an untouched install looks like, and a `0` there does **not** mean no model may be loaded.

**What the relay does:** it bounds how many requests are in flight at once, per credential, per
address, and for the relay as a whole. `[limits.total]` is the one that applies
here, and its ceiling comes off this page: above
`OLLAMA_NUM_PARALLEL x OLLAMA_MAX_LOADED_MODELS` it buys nothing, because past that point
Ollama queues internally and the queued request holds its relay slot the whole time it is
receiving nothing. Below that product, the relay refuses work Ollama could have done.

**What the relay does not do:** it does not know which model a request names, does not queue by
model, and cannot prevent an eviction. The model name is in the request body, the two SDKs
serialise it last, and the body is a stream the relay forwards without reading.

Two results are worth stating because they are counter-intuitive, and because each one stops
an operator configuring against a problem they do not have.

**Concurrency on one model was never the problem.** Six simultaneous callers for one cold model
cost one load, shared: all six came back reporting the same `load_duration` to within 0.04s,
and all six finished in 9.12s against 5.95s for a single cold request on its own. Two models
that both fit alternate for free, at 0.18s a turn.

**No concurrency limit can fix thrashing.** What costs is the sequence of model names over
time, not the overlap between requests: alternating two models that cannot coexist, strictly
serially, one at a time, never overlapping, still paid a full cold load on every swap. No
admission decision can reorder that, which is why the relay documents
`OLLAMA_MAX_LOADED_MODELS` instead of trying. `[limits.total]` is not a thrashing
setting and will not be made into one.

## Providers

`lmrelay provider add NAME TOKEN` adds or rotates an upstream. With a known name (`openai`,
`anthropic`, `deepseek`, `grok`, `ollama`) the base URL, dialect and header shape come from
a preset, so `lmrelay provider add openai sk-...` is the whole command. For anything else,
`--base-url` is required and `--dialect` and a repeatable `--header K=V` shape the request.

```bash
lmrelay provider add openai sk-...
lmrelay provider add local tok \
  --base-url http://10.0.0.5:8000 --dialect openai
lmrelay provider list          # from the file and from state
lmrelay provider delete local
```

A provider added by the CLI wins over an `[upstream.<name>]` of the same name, and the
startup log names what was shadowed. `provider delete` refuses a name that only
`lmrelay.toml` owns: `provider list` shows such a name, and deleting nothing while reporting
success would read as a delete that worked. Remove its `[upstream.<name>]` section by hand
instead.

## Export and import

```bash
lmrelay export [PATH] [--no-secrets] [--force]
lmrelay import [PATH] [--force]
```

`export` writes a bundle, `import` replaces both files the relay reads, and `--config PATH`
works on both, as everywhere else.

**No path means the terminal**, so moving a relay is one pipe:

```bash
lmrelay export | ssh other-host lmrelay import
lmrelay export relay.toml            # or to a file, written 0600
cat relay.toml | lmrelay import      # or from one
```

The bundle goes to stdout and every word about it to stderr, so what the pipe carries is the
file and nothing else. `-` is still accepted in place of a path, and reads as deliberate
where a bare command does not.

An `import` with nothing piped in and no path is **refused** rather than left reading the
terminal: a command that hangs with no output reads as a relay that has locked up rather
than as a missing argument.

An `export` to a terminal carries every caller token and provider key in clear, into the
scrollback and into any screenshot of it, so it says so as a warning and names `--no-secrets`.

What is in effect right now, as opposed to what is in the file, is `lmrelay status` for the
bind, the upstreams and the limits, `lmrelay provider list` for the upstreams in full,
`lmrelay token list` for the credentials, and `lmrelay export` for all of it at once, in the
shape the relay resolved it to.

### The bundle holds config and state, and holds effective values

The state file holds the caller tokens, the auth switch and the CLI-added providers, so a
bundle without them would reproduce a relay that refuses every caller or has no providers at
all. The bundle is therefore both files in one.

It holds the **effective** configuration, after `${VAR}` expansion in header values, because
"export, then import on a clean machine, and get the relay that was exported" is only true of
the effective values. Exporting `${OPENAI_API_KEY}` verbatim would produce a bundle that
reproduces the relay only on a machine that exports the same variable.

Export moves and deletes nothing, and writes only the path it was given. What it will not do
is write that path over something, which used to be the one thing in this command set that
happened in silence:

- **The relay's own two files are refused outright**, with no flag that permits it. A bundle
  is not a `state.json`, but it is close enough to one to be read as an empty one: written
  there, it parses, `load_state` finds no tokens and no providers in it, and the relay drops
  to auth off without a word while the only copy of the credentials sits in a file the next
  `token gen` overwrites. Relative and absolute spellings of those paths are the same file to
  this check.
- **Anything else that already exists is refused until `--force`**, which is what `init` and
  `import` already do with the files they write.

If you want a copy of the file, you already have the file, and `cp` is that tool.

```text
lmrelay: /home/u/.lmrelay/state.json is this relay's own state file, and a bundle is not one.
Export to another path; nothing has been written.
lmrelay: relay.toml is already there, and an export would overwrite it. Pass --force to
replace it, or choose another path.
```

### TOML, versioned, and readable as the config it is

The config is TOML and the state is JSON, so a bundle has to pick one. **It is TOML**, and it
is an `lmrelay.toml` with the machine-owned half written into it: the same `[server]`, the
same `[limits.*]`, the same `[upstream.*]`, plus the `[auth]` block and the three metadata
keys at the top. An operator reading a bundle, hand-editing one to provision a machine, or
diffing two of them is reading the file they already know.

```toml
# lmrelay bundle, written by lmrelay 0.0.5 at 2026-08-31T18:04:11Z.

bundle_version = 1
written_by     = "lmrelay 0.0.5"
exported_at    = "2026-08-31T18:04:11Z"

[server]
host             = "127.0.0.1"
port             = 11435
default_upstream = "ollama"
connect_timeout  = 10
log_level        = "INFO"

[limits.per_token]
requests = 2
period   = "120s"

[limits.per_address]
requests = 4
period   = "60s"

[limits.total]
requests = 6
period   = "0s"

[auth]
enabled = true

[[auth.tokens]]
id         = 1
token      = "lmr_..."
label      = "laptop"
created_at = "2026-08-14T09:12:03Z"

[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
headers  = {}

[upstream.openai]
base_url = "https://api.openai.com"
dialect  = "openai"
headers  = { "Authorization" = "Bearer sk-..." }
```

The extension is not inspected: `relay.toml`, `relay.ini` or no extension at all are the
same file to both verbs. `.toml` is what the examples use because that is what is in it.

Two version fields, doing two jobs. `bundle_version` is load bearing and is what an import
checks. `written_by` is for the human reading a bundle six months later.

`period` travels as a string, in the spelling the file carried, so a bundle read by eye says
`"30m"` where the config it came from said `"30m"`. An import refuses one that is not a
whole number and a unit, for the same reason it refuses a `log_level` of `"VERBOSE"`: both
are the right type, and both write a pair of files the relay then refuses to start from, on
a machine whose working config the import has already moved aside.

A bundle written by a build from before the format changed is **named as JSON** rather than
reported as a syntax error: a TOML parser meets `{` and complains about line 1 column 1,
which reads as a corrupt file rather than as one in the previous format.

A bundle is a transfer format, not a replacement for your `lmrelay.toml`. **The comments in
your file are yours and are not carried**, which is the surprise, so the export command says
so on its third line. What an import writes in their place is a short header naming the bundle
it came from and saying that the upstreams and the tokens are in `state.json` beside it.

### Secrets are in by default, at `0600`

They have to be, because the round-trip requirement is absolute and a bundle without the
caller tokens and the provider keys does not reproduce the relay. The file is written through
the same temporary-file-then-rename path `save_state` uses, so it is `0600` from creation and
never briefly world readable, and the command says what it wrote:

```text
Wrote relay.toml (0600).
It contains 2 caller tokens and 3 provider keys in clear.
It carries settings, not comments: the notes in your lmrelay.toml stay here.
```

`--no-secrets` masks token values and header values instead. An export bundle full of API
keys is a file people attach to an issue without thinking, and the flag is the difference
between a shareable config and a leaked key. It says so in place of the line above:

```text
Wrote relay.toml (0600).
Token values and header values are masked, so it carries no secrets and the relay it imports
will need 'lmrelay token gen' and 'lmrelay provider add'.
It carries settings, not comments: the notes in your lmrelay.toml stay here.
```

Importing a masked bundle imports everything else and reports which fields were left blank,
naming those two commands, rather than pretending the relay is complete:

```text
Secrets were masked in it, so nothing was restored for: caller token 1 (laptop), upstream
myllm header Authorization.
Run 'lmrelay token gen' and 'lmrelay provider add <name> <key>' to replace them.
```

That second line is actionable for a custom endpoint as well as for a preset one:
`provider add` falls back to the `base_url` and `dialect` already in the state when no preset
carries the name, so re-keying an upstream the bundle brought with it needs no `--base-url`
the operator would otherwise have to go and find.

### Import replaces, and backs up first

**Replace, never merge.** A merge produces a third relay that is neither the exported one nor
the existing one, and nobody can predict which. "The relay after an import is the relay that
was exported, and nothing else" is one sentence, and it is checkable.

Import writes both files the relay reads: an `lmrelay.toml` holding `[server]` and
`[limits]`, and a `state.json` holding the auth switch, the caller tokens and every upstream.
Upstreams all land in state, which shadows the file, which is the precedence already
documented above, so no new rule is needed.

The guard, in order:

1. It refuses if a config or a state file already exists, unless `--force`. That is symmetric
   with `lmrelay init`, which already refuses to overwrite.
2. With `--force` it moves the existing pair to `lmrelay.toml.bak` and `state.json.bak`, and
   says where they went.
3. If a `.bak` already exists, it refuses and names it. Nothing is ever lost, and no
   timestamped debris accumulates.

Everything the bundle says is checked before any of that happens, so a bundle this relay will
not accept leaves the existing pair exactly as it was. What it then writes, it names:

```text
Moved /home/u/.lmrelay/lmrelay.toml to /home/u/.lmrelay/lmrelay.toml.bak.
Moved /home/u/.lmrelay/state.json to /home/u/.lmrelay/state.json.bak.
Imported relay.toml, written by lmrelay 0.0.5 at 2026-08-31T18:04:11Z.
Wrote /home/u/.lmrelay/lmrelay.toml and /home/u/.lmrelay/state.json (0600): 3 upstreams,
2 caller tokens, auth on.
```

The moves are announced one at a time as they happen rather than as a plan, because the four
filesystem operations cannot be made one: if the disk fails between them, what those lines
name is what is on disk.

Then it signals the running relay like every other mutating command, and says so whether or
not one is running:

```text
host, port and connect_timeout are read at startup only; run 'lmrelay restart' if the bundle
moved any of them.
```

Said unconditionally rather than only when they moved, because the operator who imports onto a
stopped relay is the one who will start it next, and a bundle from another machine is exactly
the thing likely to carry a different bind.

### A bundle from a newer lmrelay is refused

```text
lmrelay: relay.toml has bundle version 2; this lmrelay understands 1. It was written by a
newer lmrelay: upgrade, or export again from that machine with a matching version.
```

Refused rather than partially read, because a newer bundle may carry a limit scope this
version does not enforce, and importing it would produce a relay that looks configured and is
not. The wording follows the existing state version error, so it reads as the same kind of
thing.

An **unknown key** inside a known bundle version is refused and named too: at the same
version the two ends agree about the shape, so an unknown key means a hand edit or a wrong
version field. Forward compatibility comes from bumping `bundle_version`, which is one line
and which lmrelay controls at both ends.

An **older** bundle version is read, and the keys it does not carry take their defaults.

### The round trip

```bash
lmrelay export relay.toml
scp relay.toml other-host:
ssh other-host 'lmrelay import relay.toml'
```

Export, import into an empty config directory, and the relay is identical in behaviour: same
bind, same limits, same upstreams with the same headers, same caller tokens with the same
ids, same auth switch. That is one test in the suite, and it is what keeps the two files and
the bundle honest against each other as they change.

## Autostart

`lmrelay enable` registers a systemd `--user` unit on Linux or a launchd agent on macOS,
then starts it. Elsewhere it refuses: on a POSIX box with neither manager `lmrelay serve`
runs the relay detached, and on Windows only `lmrelay run` works.

From then on `stop`, `restart` and `reload` go through that manager instead of the pidfile,
so the two cannot disagree about who owns the process; each command says which path it took.

## Reload

`lmrelay reload` sends the running relay a SIGHUP, and it re-reads both sources in place:
`lmrelay.toml` and `state.json`. Nothing in flight is disturbed: connections stay open and a
stream already being relayed runs to its end. Every command that writes a change, whether
`token gen`, `auth true`, `provider add`, `import` or the rest, signals the relay for
you, so an explicit reload is what you run after editing `lmrelay.toml` by hand.

A `${VAR}` in a header value is read when the relay reads the file, so a key exported in your
shell after the relay started is not in the relay's environment and no reload will find it.
That one needs a restart, under whatever manager owns the process.

| Key | Applied by | Why |
|---|---|---|
| `[upstream.*]`, and providers added by `lmrelay provider add` | `lmrelay reload` | Base URLs and headers are read from the config on every request, so the next request uses the new set. |
| `default_upstream` | `lmrelay reload` | Chosen per request, out of that same config. |
| Caller tokens, and the auth switch | `lmrelay reload` | Also read per request, so `lmrelay auth true` starts requiring a credential as soon as the relay has re-read state. |
| `log_level` | `lmrelay reload` | Logging is reconfigured in place, and the new level governs the next line the relay writes. |
| `[limits.<scope>] requests`, `period` | `lmrelay reload` | That scope's limiter is rebuilt when either moves, and left alone when neither does: a fresh limiter starts every caller with a full bucket, which would clear the allowance of whoever was being limited at that moment. The how-many-at-once half needs no rebuild at all, since the cap is read per request, so an answer already streaming keeps the slots it holds and the new number governs the next arrival. |
| `host`, `port` | `lmrelay restart` | The socket is already bound, and a running server cannot move it. |
| `connect_timeout` | `lmrelay restart` | The shared httpx client is already open and carries the timeout; closing it to re-time would abort every stream being relayed through it. |

**`lmrelay reload` says this in your terminal**, and the relay says it again in
`lmrelay.log`. It names whichever of `host`, `port` and `connect_timeout` differs from what
the running relay started with, gives the old value and the new one, and says a restart
applies them. They are named individually, so a changed port does not hide an unchanged
timeout:

```console
$ lmrelay reload
lmrelay: signalled the running relay to re-read its config (SIGHUP).
lmrelay: port 11435 -> 8080, connect_timeout 10 -> 30 in /home/u/.lmrelay/lmrelay.toml but a reload cannot apply that: the socket is already bound and the client already open; restart to apply
```

Both values, because the process that bound the socket is the only thing that knows what it
started with. The file has the new number, so a warning naming only the key would send you
to the file to read the half of the answer the file already has.

The terminal line is worked out by the CLI rather than reported back by the relay. SIGHUP is
delivered, not acknowledged, so asking would mean polling for an answer that may not have
been written yet and deciding what its absence meant. Both halves are already on that side:
`lmrelay.pid` records what the process started with, and the config on disk is what it is
being asked to move to. One comparison, one sentence, and the same one in both places.

A pidfile written by a relay that predates this records no `connect_timeout`, so a `reload`
across an upgrade names the port and the host and stays quiet about the timeout rather than
inventing a value to compare against. A restart records all three.

The keys above them are applied without comment, except the three that say what they moved
from and to, so that a reload can be read back afterwards:

```text
lmrelay: log_level INFO -> DEBUG
lmrelay: [limits.total] off -> 2 per 120s, 2 at once (from the next request; answers in flight keep the slot they hold)
lmrelay: [limits.per_address] 4 at once -> 4 per 60s, 4 at once (from the next request; answers in flight keep the slot they hold)
```

One line per scope, because the number is one number doing two jobs and two lines about it
would read as two settings.

A config the relay cannot use is logged and discarded, and it carries on serving the one it
already had. That covers `state.json` as much as `lmrelay.toml`, and a value the file spells
wrongly (`port = "eleven"`, `log_level = "verbose"`) as much as a syntax error. A typo must
not take the relay down.

The CLI reports that it signalled the relay, never that the change took effect. SIGHUP is
delivered, not acknowledged, so `lmrelay reload` and every command that reloads on your
behalf stop at what they did. The outcome is in `lmrelay.log`: a discarded reload is logged
whatever the level, an accepted one at `INFO` or below.

## Metrics

`GET /metrics` answers a Prometheus scrape: aggregate counters in the text exposition format,
served as `text/plain; version=0.0.4; charset=utf-8`. That `0.0.4` is the version of the
exposition format, which Prometheus parses by, and not lmrelay's own. They were the same
number for one release, by coincidence, and 0.0.5 is where they part: the header still says
`0.0.4` and `lmrelay_build_info` says `0.0.5`.

Nothing configures it. There is no key in `lmrelay.toml` to turn it on, turn it off or move
it. It is one route, written by hand rather than with
`prometheus_client`, because the dependency count is a documented property of this project
and there are still four.

### It needs a credential, and `/healthz` does not

This is the one way the two endpoints differ, and the reason is what each one tells a
stranger. `/healthz` says a process is alive, which is nothing. `/metrics` says how the relay
is used, what it is in front of, how busy it is at this instant and how often it refuses
people. So it sits behind the same credential as everything else, and a scrape with no token
is refused with the same 401 and the same message any other request would get.

Prometheus takes a bearer token in a scrape job, so the credential costs one stanza of scrape
config, below.

With `lmrelay auth false` there is no credential to require, and `/metrics` is then as open as
every other path, to whoever can reach the port. That is the same fact the exposure warning
is already about, with one more thing behind it: on a non-loopback bind with auth off, a
stranger can now read how the relay is used as well as use it. Nothing new is exposed on
loopback, and nothing is exposed at all once auth is on.

Only `GET` is the scrape. Every other method on `/metrics` is relayed to the upstream like
any other path, exactly as with `/healthz`, because an upstream may well have a `/metrics` of
its own and the relay does not get to decide otherwise.

### What is in a scrape

| Metric | Type | Labels | Answers |
|---|---|---|---|
| `lmrelay_build_info` | gauge | `version` | Which lmrelay these numbers came from. Always `1`; the version is the label, which is how a version is exposed in this format. |
| `lmrelay_requests_total` | counter | `upstream`, `status` | How much traffic the relay answered, split by the upstream it chose and the status it returned. Includes the relay's own answers: a 400 for a wrong-dialect path, a 429 from a limit and a 500 from a fault in the relay are all requests it answered. A 401 is not here, because no upstream was chosen; it is in `lmrelay_auth_failures_total`. |
| `lmrelay_request_ttfb_seconds` | histogram | `upstream` | Seconds from the request arriving to the upstream's first byte, so it carries the relay's own admission work as well, which is microseconds. The same measure the access log prints in brackets, unrounded. Written as `_bucket`, `_sum` and `_count`, as the format requires. |
| `lmrelay_requests_in_flight` | gauge | none | How many answers are being relayed right now. A streamed answer counts until its last byte reaches the caller, which is the same lifetime `[limits.<scope>] concurrent` bounds. |
| `lmrelay_refusals_total` | counter | `scope`, `kind` | How often a limit turned somebody away. `scope` is `per_token`, `per_address` or `total`; `kind` is `rate` or `concurrent`, the two ways one scope's number can turn somebody away. |
| `lmrelay_auth_failures_total` | counter | none | How often a credential was missing or wrong. Unlabelled, and deliberately not also counted as a request: it never reached the point where an upstream is chosen. |
| `lmrelay_upstream_errors_total` | counter | `upstream`, `type` | How often the relay failed to reach an upstream at all, named by its httpx exception type, usually `ConnectError` or `ConnectTimeout`. A stream that breaks after the headers arrived is not here: the caller already had the upstream's own status, so that failure belongs to the answer rather than to getting there. |

The histogram's bounds are seconds, and they are not the Prometheus defaults:

```text
0.05  0.1  0.25  0.5  1.0  2.5  5.0  10.0  30.0  60.0  120.0  300.0  +Inf
```

The defaults stop at 10, and a local model that is not resident has to be read off disk
before it can produce a token, which is tens of seconds for a large one and minutes on a cold
cache. Under the default bounds almost every local answer lands in `+Inf`, where the
histogram has recorded that something took longer than ten seconds and nothing else at all.
The low end still has to resolve a hosted provider answering in well under a second, so the
range spans four orders of magnitude and is coarse in the middle on purpose. The questions it
is built for are "was that a hosted answer or a local one" and "did a model have to load",
not "3.4s or 3.6s".

Only a request an upstream actually answered is timed. A dialect refusal costs microseconds
and a refused request costs the price of refusing it, so counting either as a time to first
byte would pull the distribution down until the number you read is this relay's own overhead
rather than a model's first token. Both are still counted in `lmrelay_requests_total`; they
are simply not in the histogram. A connection that failed is not timed either, for the same
reason: that elapsed time is a connect timeout, not a first token. Nor is a 500 the relay
answered with before an upstream got a chance to; one raised after the upstream had already
answered is timed, because by then the elapsed time is a real first byte.

What the histogram measures is when the upstream's response headers arrive, and for one kind
of caller that is not the first token at all. An upstream streaming an answer sends its
headers with the first token. The same upstream answering a request with `"stream": false`
sends them only once the whole answer is finished, because until then it has nothing to send.
Measured against one Ollama, one model and one path: 0.15s streaming, 7.4s not. Both land in
the same `{upstream="ollama"}` histogram and nothing separates them, because the `stream`
flag is in the request body and the relay does not read request bodies, which is the same
reason there is no model label. So a distribution with two humps in it is two kinds of caller
before it is anything about load, and the 7.4s hump is not a model that had to be loaded.

A labelled series appears at its first sample rather than being declared up front, so a
status this relay has never returned has no line at all, instead of a zero that reads as
"measured, and it did not happen". The three families with no labels are always there:
`lmrelay_build_info`, `lmrelay_requests_in_flight` and `lmrelay_auth_failures_total`, the
last two starting at `0`. So the first scrape after a restart is this, and it describes what
the relay will measure rather than answering with a nearly empty page:

```text
# HELP lmrelay_build_info The version of the relay these counters came from, as a label on a constant 1.
# TYPE lmrelay_build_info gauge
lmrelay_build_info{version="0.0.5"} 1
# HELP lmrelay_requests_total Requests the relay answered, by the upstream chosen and the status returned.
# TYPE lmrelay_requests_total counter
# HELP lmrelay_request_ttfb_seconds Seconds from a request arriving to the upstream's first byte, by upstream.
# TYPE lmrelay_request_ttfb_seconds histogram
# HELP lmrelay_requests_in_flight Answers being relayed right now, counted until the last byte of each reaches the caller.
# TYPE lmrelay_requests_in_flight gauge
lmrelay_requests_in_flight 0
# HELP lmrelay_refusals_total Requests refused by a limit, by the scope that refused and which of its measures.
# TYPE lmrelay_refusals_total counter
# HELP lmrelay_auth_failures_total Requests refused for a missing or invalid credential.
# TYPE lmrelay_auth_failures_total counter
lmrelay_auth_failures_total 0
# HELP lmrelay_upstream_errors_total Failures reaching an upstream, by upstream and exception type.
# TYPE lmrelay_upstream_errors_total counter
```

A family with `# HELP` and `# TYPE` and nothing under it is valid, and Prometheus reads it as
a family it has seen no samples of.

### No caller is named anywhere

There is no per-token label, no per-address label and no per-caller label of any other kind.
Two reasons, and both are load-bearing.

It is what keeps **"No token accounting, usage database or budgets"** in [Not in
scope](#not-in-scope) true. That sentence is a promise about what this relay declines to
know, and a counter broken out by credential is exactly the accounting it forswears, whatever
it is called.

And a label per credential is unbounded cardinality. Every token that ever presented itself
would become its own time series, kept by whatever scrapes this, forever, including the ones
you revoked and the ones that were guesses. The aggregate answers the operational question
anyway: `lmrelay_refusals_total{scope="per_token"}` says a token limit is biting without
saying whose, and the caller you actually want is in `lmrelay.log`, with an address and a
request id.

There is no model name either, for a different reason: the model is in the request body, both
SDKs serialise it last, and the relay does not read request bodies. See [Why a client cannot
cross dialects](#why-a-client-cannot-cross-dialects).

### The counters reset when the relay restarts

They live in memory in this process and nowhere else. A restart puts every one of them back
to zero, and that is the intended behaviour, not a gap. Prometheus recognises a counter reset
and reads across it, so `rate()` and `increase()` stay correct over a restart.

The alternative is a number that survives a restart, which is a file on disk written on the
hot path, which is the usage database this project says it is not. `lmrelay_build_info`
carries the version, so a reset that coincides with a version change is legible as an
upgrade.

A reload does **not** reset them, unlike the rate limiters, which are rebuilt when their
numbers move. These are what a chart is drawn from, and a counter that went back to zero
because somebody edited a config file would read as a restart that never happened.

Counted in one process, exactly as the limits are. Two relays beside each other report two
sets of numbers and neither is the total. Under several uvicorn workers a scrape reaches one
worker and reports that worker.

### A scrape does not appear in its own numbers

A successful scrape is not counted in `lmrelay_requests_total`, is not timed, is not counted
as in flight, and writes no line to the access log. Otherwise every poll would move a counter
it is itself reporting, the series would grow by one on each poll with nothing behind it, and
`lmrelay.log` would fill with a line every fifteen seconds about the monitoring rather than
about the relay.

A scrape refused for a missing or bad credential **is** logged, and does count in
`lmrelay_auth_failures_total`. That one is about the relay: it is either a misconfigured job
or somebody who is not the monitoring.

Nor does a scrape spend anybody's allowance. `/metrics` is its own route, and admission
happens in the relay route, so a poll every fifteen seconds cannot use up the rate limit of
whichever address the monitoring happens to share. It reaches no upstream either.

### A scrape job

`bearer_token` holds a caller token, one of the values `lmrelay token list --show` prints:

```yaml
scrape_configs:
  - job_name: lmrelay
    scheme: http
    bearer_token: "REPLACE-WITH-A-CALLER-TOKEN"
    static_configs:
      - targets: ["127.0.0.1:11435"]
```

`bearer_token_file` takes a path instead, and is worth the extra file: it keeps the
credential out of `prometheus.yml`, which is usually world-readable, and lets you rotate the
token with `lmrelay token gen` without editing the scrape config.

```yaml
scrape_configs:
  - job_name: lmrelay
    scheme: http
    bearer_token_file: /etc/prometheus/lmrelay.token
    static_configs:
      - targets: ["127.0.0.1:11435"]
```

The default `metrics_path` is already `/metrics`, so there is nothing to set. If nginx sits
in front for TLS, set `scheme: https` and point `targets` at it; the relay is reached the same
way every other caller reaches it.

Check it by hand first, which is also how you read these numbers when there is no Prometheus:

```bash
curl http://127.0.0.1:11435/metrics -H "Authorization: Bearer $TOKEN"
```

Two scrapes of an unchanged relay are the same bytes, so `diff` between two of them is a
usable way to see what happened in between.

## The request id in the log

Every line the relay writes carries a short id in brackets, after the logger name and before
the message:

```text
2026-08-31 10:25:34.554 [INFO]: (lmrelay.app) [c4eacac4] 127.0.0.1 GET /api/tags -> ollama: 200 (0.00s)
```

It exists for one job: tying a caller's request to what that request caused, when both land
in `lmrelay.log` in the middle of everybody else's traffic. A 502 and the access line that
reports it share an id, so the upstream failure a few lines above a request line is provably
the one that request caused, rather than one that merely happened at about the same time.

```text
2026-08-31 10:25:34.552 [WARNING]: (lmrelay.app) [7b0e41d5] lmrelay: upstream 'ollama' at http://127.0.0.1:11434 is unreachable: ConnectError
2026-08-31 10:25:34.554 [INFO]: (lmrelay.app) [7b0e41d5] 127.0.0.1 GET /api/chat -> ollama: 502 (0.01s)
```

The two levels are not the same, and it matters for reading the pair. The access line is
`INFO` whatever status it carries, because it is the same line every served request writes;
only a limit refusal raises it to `WARNING`. So at `log_level = "WARNING"` the first of those
two lines is written and the second is not, and the id has nothing left to pair with. Run at
`INFO` if you want the pairing.

The relay's own 500 is the other way round, and pairs the same way: the access line comes
first at `INFO`, and the traceback that follows it is `ERROR`, written from above the
middleware on the way out.

Worth knowing about it:

- **It is not a distributed trace id.** It is generated by this relay, for this relay's log,
  and it is neither read from an inbound header nor sent to the upstream. Nothing correlates
  it with anything your client or your provider recorded. If you need that, you need tracing,
  which is a dependency and is not here.
- **It is not returned to the caller**, in a header or anywhere else, so a caller cannot
  quote it at you.
- **Eight hex characters**, from `secrets.token_hex(4)`. That is a page of log an operator
  greps, not a key: four random bytes collide at around sixty thousand requests by the
  birthday bound, which in that setting is a coincidence rather than a wrong answer, and the
  id is never used to look anything up.
- **A line the relay did not write for a request of its own carries `-`.** Startup, shutdown,
  a reload, the exposure warning, and everything uvicorn says on its own account. At
  `log_level = "DEBUG"` the connection-level chatter from `httpcore` carries it too, a dozen
  lines per request wrapped around the access line: those lines do belong to a request, but
  they come from inside the HTTP client, which is never told which. That is also all `DEBUG`
  adds, since the relay writes no debug lines of its own.
- **A scrape of `/metrics` and a `/healthz` check write no line at all**, so neither has an id
  to carry. A scrape refused for a bad credential does write one, and carries an id like any
  other refusal.
- **Command output has no id and no timestamp.** `lmrelay status` and `lmrelay token list` are
  tables, and a table is a table only without a timestamp and a logger name in front of every
  row. A command is not serving anybody's request.

The id is a field in the log format, which means it appears in `lmrelay.log` for every relay
started by `run`, `serve` or a service manager. Anything that parses that file by position,
a fail2ban filter above all, has to account for it: see [Banning repeat
offenders](#banning-repeat-offenders-with-fail2ban).

## Behaviour worth knowing

- **Nothing is buffered.** Request and response bodies stream in both directions; the
  response is never decoded or inspected, so a token-by-token stream stays token-by-token
  and prompts can never leak into the log.
- **There is no read timeout, on purpose.** A large local model can think for minutes
  before its first token, and a read timeout would kill it in a way that looks like a model
  fault. `connect_timeout` stays short because failing fast on an unreachable host is the
  useful half. Please do not "fix" this.
- **The caller's credential never leaves the relay.** `Authorization` and `x-api-key` are
  stripped from every forwarded request before the upstream's own headers are applied, so
  an upstream with no configured headers receives no credential at all.
- **The elapsed time in the access log is time to first byte**, not the duration of a
  streamed answer. The same measure is the histogram behind
  `lmrelay_request_ttfb_seconds`.
- **Every log line carries a request id**, and lines about one request share it. `-` marks a
  line the relay did not write for a request of its own.
- **`GET /metrics` is a Prometheus scrape and needs a credential**, which is the one way it
  differs from `/healthz`. It touches no upstream, spends no allowance and writes no access
  line.
- **The pidfile is written by the relay itself**, whether it was started by `run`, by
  `serve` or by a service manager, so `status`, `stop` and `reload` have one place to look
  regardless. A pidfile naming a dead process is overwritten silently; one naming a live
  process makes a second start refuse, rather than letting it fail later on the bind with a
  less useful message.
- **An unreachable upstream is a 502 that names it**, e.g.
  `lmrelay: upstream 'ollama' at http://127.0.0.1:11434 is unreachable: ConnectError`,
  usually meaning Ollama itself is not running.
- **Binding a non-loopback host with auth off logs a warning** rather than refusing, since
  running uncredentialed behind an authenticated nginx is legitimate.
- **`lmrelay status` exits 0 whether or not the relay is running.** A stopped relay prints
  the same block with `stopped` on the first line, because "what would it do if I started
  it" is the question a stopped relay raises. `status` reports, it does not assert.

## Why a client cannot cross dialects

- **An Ollama-API client cannot reach a hosted provider.** None of them serve `/api/chat`,
  and the body schema differs (`options` versus top-level sampling parameters, `format`
  versus `response_format`, a different streaming frame shape).
- **An OpenAI-shaped client cannot reach Anthropic.** `api.anthropic.com` has no
  `/v1/chat/completions`, and even against `/v1/messages` the body is wrong: Anthropic
  takes `system` as a top-level parameter, requires `max_tokens`, and answers with content
  blocks rather than `choices`.
- **Model names are not translated or validated.** `llama3` means nothing to OpenAI. Name a
  model the upstream you chose actually has.
- **Streaming frames are not converted.** Ollama emits newline-delimited JSON; OpenAI and
  Anthropic emit SSE with different event shapes. Your client gets exactly what the
  upstream produced.

## Errors

Every message below is emitted by lmrelay verbatim, with `<...>` standing for a value the
code fills in. Everything the relay itself writes, and every refusal a caller receives, begins
with `lmrelay: `, so grepping a log for that prefix finds everything lmrelay said and nothing
an upstream said. Some of what the CLI prints back at your own terminal does not: those lines
are answering a command you just typed, and are marked in the warnings table by not carrying
the prefix.

### Request-time errors

These are JSON bodies of the form `{"error": "..."}` returned to the caller.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: missing or invalid credential` | relay response, 401 | Auth is on and the request carried no credential, or one that matches no configured token. | Present a configured token. Both `Authorization: Bearer <token>` and `x-api-key: <token>` are accepted carriers. `lmrelay token list` shows which exist; `lmrelay auth false` reopens the relay. |
| `lmrelay: rate limit exceeded for your token: <n> per <period> ([limits.per_token])` | relay response, 429 | This credential has started more requests in that period than `[limits.per_token]` allows. The period is quoted in the spelling the config carries. | Wait: `Retry-After` is the whole number of seconds until one request has refilled in that bucket. Raise `requests`, or lengthen nothing and shorten `period`, whichever the traffic is. |
| `lmrelay: rate limit exceeded for your address: <n> per <period> ([limits.per_address])` | relay response, 429 | This client address has asked more often than `[limits.per_address]` allows. Behind NAT, that address is everyone sharing it. | As above, on `[limits.per_address]`. With auth on, `[limits.per_token]` is the scope that follows the caller rather than the network. |
| `lmrelay: the relay's rate limit is exceeded: <n> per <period> ([limits.total])` | relay response, 429 | The relay as a whole has been asked more often than `[limits.total]` allows, by everyone together. Your own scopes may be nowhere near their limits. | Wait, or raise `[limits.total]`. This is the scope that protects the upstream, so raise it against what the machine can do rather than against what one client wants. |
| `lmrelay: your token already has <N> requests in flight ([limits.per_token]); one of yours must finish first` | relay response, 429 | This credential already holds `[limits.per_token] concurrent` relayed requests whose bodies have not finished. | Wait for one of your own to finish, or raise it. There is deliberately no `Retry-After` on any of the three: a slot frees when an answer ends, and with no read timeout the relay cannot know when that will be. |
| `lmrelay: your address already has <N> requests in flight ([limits.per_address]); one of yours must finish first` | relay response, 429 | This client address already holds `[limits.per_address] concurrent`. | As above. Behind NAT the request that has to finish may be a colleague's, which is the NAT caveat arriving where it is least convenient. |
| `lmrelay: the relay is already carrying <N> requests ([limits.total]); one of them must finish first` | relay response, 429 | The relay as a whole is holding `[limits.total]` unfinished answers. | Wait, or raise it, up to `OLLAMA_NUM_PARALLEL x OLLAMA_MAX_LOADED_MODELS` and no further. It says "one of them", not "one of yours", because at this scope the request that has to end may be anybody's. |
| `lmrelay: upstream '<name>' speaks the <Dialect> API; '<path>' is an <Other>-dialect path. lmrelay forwards requests unchanged and does not translate between dialects.` | relay response, 400 | The path belongs to a dialect the chosen upstream does not serve. | Change the path prefix to an upstream of that dialect, or point the client at an endpoint the upstream has. |
| `lmrelay: method not allowed for <METHOD> <path>` | any method outside the relayed set, TRACE being the one a scanner tries | Starlette refused the request before the relay saw it. Phrased here so it carries the prefix and the `error` key every other refusal uses. | Nothing: the method is not one the relay forwards. |
| `lmrelay: upstream '<name>' at <base_url> is unreachable: ConnectError` | relay response, 502 | The connection to the upstream was refused or timed out. `ConnectTimeout` appears in place of `ConnectError` when `connect_timeout` expired. | For `ollama`, usually Ollama itself is not running: start it. Otherwise check `base_url` and the network. |
| `lmrelay: upstream '<name>' failed: <Type>` | relay response, 502 | Any other transport failure against the upstream, named by its httpx exception type. | Read `lmrelay.log`: the full message and traceback are there. |
| `lmrelay: <Type>: <message>` | relay response, 500 | An unhandled exception inside the relay. | Read `lmrelay.log` for the traceback and report it. |

One dialect refusal in full, as an OpenAI-shaped client aimed at `anthropic` receives it:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

### Startup and config errors

Raised by the relay before it binds, and by every CLI command that loads the config.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: no config found; looked at ./lmrelay.toml and <home path>. Run 'lmrelay init'.` | any command that loads the config | No config file exists at any of the three places. | Run `lmrelay init`, or point `--config PATH` at the file you have. |
| `lmrelay: cannot read <path>: <Type>: <detail>` | any command that loads the config | The file could not be opened, or TOML could not parse it. | Fix the syntax the detail names, or the permissions on the path. |
| `lmrelay: config has no [upstream.*] sections in <config> and no providers in <state>. Run 'lmrelay provider add' or add an [upstream.*] table.` | any command that loads the config | The config parses but defines no upstream at all, and state has none either. | Add an `[upstream.*]` table, or run `lmrelay provider add`. |
| `lmrelay: default_upstream '<name>' is not defined; known upstreams: <list>` | any command that loads the config | `[server] default_upstream` names an upstream that no source defines. | Set it to one of the names listed, or define the one it names. |
| `lmrelay: [<table>] <key> must be a whole number, got <value>` | any command that loads the config | A whole-number key holds something `int()` cannot read, usually a quoted number or a typo. `<table>` is `server` for `port` and `connect_timeout`, and `limits.<scope>` for `requests`. | Write it unquoted, as a number. Refused rather than coerced, so a reload discards it like any other unusable config instead of raising out of the signal handler. |
| `lmrelay: [<table>] <key> cannot be less than 0, got <value>` | any command that loads the config | `[limits.<scope>] concurrent` is negative. Worded as a minimum rather than as a sign because it goes through the same reader as `port`, which has no minimum at all. | Use `0` to turn that scope's cap off. |
| `lmrelay: [limits.<scope>] requests cannot be less than 0, got <value>` | any command that loads the config | The count is below zero. | Use `0` to turn the scope off. A negative is refused rather than read as off, because it is a typo, and admitting it as another spelling of "off" would hide the mistake behind the behaviour it causes. |
| `lmrelay: [limits.<scope>] period must be a whole number and a unit, one of s, m, h: "30s", "5m", "2h", or "0s" for no limit on how often. Got <value>` | any command that loads the config | The period is a bare number, carries an unknown unit, or is not a string. | Write the unit. `period = 30` is refused rather than read as seconds, because it is half an hour to whoever wrote it about as often as it is half a minute. `inf` and `nan` stop here too: a bucket built on `nan` compares false against every threshold, so it would refuse nobody while printing as though it were on. |
| `lmrelay: [server] <rate_limit\|rate_burst\|max_concurrent> was replaced by [limits.per_token] requests, [limits.per_address] requests, [limits.total] requests. Pick the scope you meant; see docs/CONFIGURATION.md.` | any command that loads the config | The per-caller limit key from before the three scopes existed. | Move the number into whichever scope you meant, usually `[limits.total]`. Refused rather than ignored, because a limit an operator believes is on and is not is the failure this shape exists to avoid. |
| `lmrelay: [limits.<scope>] <rate\|burst\|concurrent> was replaced by requests and period.` | any command that loads the config | The three keys one number replaced. | Write `requests`, and a `period` if the limit is a how-often as well. Named rather than reported as unrecognised, because an operator reading "no such key" would look for a typo in a key that no longer exists. |
| `lmrelay: [limits] must be a table of scope tables` | any command that loads the config | `[limits]` itself is a scalar or an array. | Write the section as `[limits.<scope>]` tables. |
| `lmrelay: [limits] has no scope <names>; expected [limits.per_token], [limits.per_address], [limits.total]` | any command that loads the config | A scope name lmrelay does not have, usually a misspelling. | Use one of the three. A misspelt scope is refused rather than ignored, because it is indistinguishable from a limit that is on until it fails to refuse anybody. |
| `lmrelay: [limits.<scope>] must be a table` | any command that loads the config | The scope is a scalar or an array. | Write it as a TOML table with `requests` and `period`. |
| `lmrelay: [limits.<scope>] has no key <names>; expected requests, period` | any command that loads the config | A key lmrelay does not have inside a scope that it does. | Use the two keys. Every scope takes the same two, so there is nothing else to reach for. |
| `lmrelay: [server] log_level '<value>' is not a logging level; expected DEBUG, INFO, WARNING, ERROR or CRITICAL` | any command that loads the config | The level is not one `logging` knows. | Use one of the five. Refused rather than quietly read as `INFO`, which would leave a reload announcing a level the relay was not running at. |
| `lmrelay: upstream '<name>' header '<header>' references ${VAR}, which is not set` | any command that loads the config | A header value in `lmrelay.toml` interpolates an environment variable that is absent. | Export the variable, or comment the upstream block out. This is why the shipped example has the hosted blocks commented. |
| `lmrelay: upstream '<name>' header '<header>' has a malformed ${...} reference: <detail>` | any command that loads the config | A `$` in a header value is not a well-formed `${VAR}`. | Write `$$` for a literal `$`, or store the key with `lmrelay provider add`, which does not expand. |
| `lmrelay: upstream name '<name>' is reserved: it would shadow the Ollama/OpenAI path root` | any command that loads the config | An `[upstream.api]` or `[upstream.v1]` table. | Rename the upstream. Those two segments are how every Ollama and OpenAI client addresses the default upstream. |
| `lmrelay: [upstream.<name>] must be a table` | any command that loads the config | `upstream.<name>` is a scalar or an array. | Write it as a TOML table. |
| `lmrelay: [upstream] must be a table of upstream tables` | any command that loads the config | `[upstream]` itself is not a table. | Write the section as `[upstream.<name>]` tables. |
| `lmrelay: upstream '<name>' needs a base_url starting with http:// or https://` | any command that loads the config | `base_url` is missing, is not a string, or has no scheme. | Give it a full origin, normally a host root with no `/v1`. |
| `lmrelay: upstream '<name>' has dialect '<dialect>'; expected one of ollama, openai, anthropic` | any command that loads the config | `dialect` is not one of the three. | Use `ollama`, `openai` or `anthropic`, or omit it to get `openai`. |
| `lmrelay: upstream '<name>' headers must be a table of strings` | any command that loads the config | `headers` is not a table. | Write it as `headers = { Name = "value" }`. |
| `lmrelay: already running (pid <N>); use 'lmrelay restart' or 'lmrelay stop'` | relay startup, `lmrelay serve` | The pidfile names a live process. | `lmrelay status` to see it, then `lmrelay restart` or `lmrelay stop`. |
| `lmrelay: the <manager> unit is active and already owns the port; run 'lmrelay stop' first, or 'lmrelay restart'` | `lmrelay run` | A systemd unit or launchd agent is already running the relay. | `lmrelay stop`, then `lmrelay run`; or just `lmrelay restart` to keep it under the manager. |
| `lmrelay: <path> already exists; not overwriting` | `lmrelay init` | `~/.lmrelay/lmrelay.toml` is already there. | Edit that file. `init` never overwrites a config you may have edited. |

### State file errors

`state.json` is machine-written. These appear when it has been hand-edited or written by
another version.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: cannot read <path>: <Type>: <detail>` | any command that loads state | `state.json` is unreadable or is not valid JSON. | Fix the JSON, or move the file aside, since a missing state file is the empty default, not an error. |
| `lmrelay: <path> is not a JSON object` | any command that loads state | The top level is an array or a scalar. | Restore an object, or move the file aside and re-add tokens and providers. |
| `lmrelay: <path> has a malformed token entry` | any command that loads state | A token record is missing an integer `id` or a string `token`. | Repair the entry. It is refused rather than skipped so a credential is never silently dropped. |
| `lmrelay: <path> has a malformed providers table` | any command that loads state | `providers` is not an object of objects. | Repair the table, or delete the providers key and re-add with `lmrelay provider add`. |
| `lmrelay: <path> has state version <N>; this lmrelay understands 1. It was written by a newer lmrelay: upgrade or move the file.` | any command that loads state | The state file came from a newer lmrelay. | Upgrade lmrelay, or move the file aside. |
| `lmrelay: cannot write <path>: <Type>: <detail>` | any command that saves state | The state file or its directory could not be written. | Check permissions and free space on the config directory. |

### Export and import errors

`<source>` is the path the bundle was read from, or `standard input`.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: <path> is this relay's own <config\|state> file, and a bundle is not one. Export to another path; nothing has been written.` | `lmrelay export` | The destination is `lmrelay.toml` or `state.json`, however it was spelled. | Export somewhere else. No flag permits this one: written over `state.json` a bundle is still readable, so the relay would keep running with no tokens and auth off, and the credentials would be in a file the next `token gen` overwrites. |
| `lmrelay: <path> is already there, and an export would overwrite it. Pass --force to replace it, or choose another path.` | `lmrelay export` | Something already exists at the destination. | Choose another path, or pass `--force`. Symmetric with `init` and `import`, which refuse to overwrite too. |
| `lmrelay: cannot read <source>: <Type>: <detail>` | `lmrelay import` | The bundle could not be opened. | Check the path and the permissions. |
| `lmrelay: <source> is not TOML: <detail>` | `lmrelay import` | The file is not TOML at all. | Point it at a file `lmrelay export` wrote. |
| `lmrelay: <source> is JSON, and a bundle is TOML.` | `lmrelay import` | A bundle from a build before the format changed. | Export again from that machine with a matching lmrelay. |
| `lmrelay: no bundle to read. Give a path, or pipe one in` | `lmrelay import` | No path, and stdin is a terminal. | `lmrelay import relay.toml`, or pipe a bundle in. Refused rather than left waiting, because a command that hangs with no output reads as a relay that has locked up. |
| `lmrelay: <source> has no bundle_version, so it is not an lmrelay export. 'lmrelay export' writes one.` | `lmrelay import` | Valid TOML, but not a bundle. An `lmrelay.toml` handed to `import` by mistake lands here. | Export one, or add the field if you are writing a bundle by hand to provision a machine. |
| `lmrelay: <source> has bundle_version <value>, which is not a version number` | `lmrelay import` | The field is not a positive whole number. | Write `1`. |
| `lmrelay: <source> has bundle version <N>; this lmrelay understands 1. It was written by a newer lmrelay: upgrade, or export again from that machine with a matching version.` | `lmrelay import` | The bundle came from a newer lmrelay. | Upgrade, or re-export from the other machine. Refused rather than partially read: a newer bundle may carry a setting this version does not enforce, and importing it would produce a relay that looks configured and is not. |
| `lmrelay: <source> has <what> this lmrelay does not know: <names>. At bundle version 1 both ends agree on the keys, so this is a hand edit or a bundle_version that is not the one it was written at.` | `lmrelay import` | An unknown key at the top level, in `server`, in `auth`, in a token record or in an upstream. | Remove the key, or set `bundle_version` to what the bundle was actually written at. Forward compatibility is what that field is for. |
| `lmrelay: <source> has a <server\|limits> section that is not an object` | `lmrelay import` | The settings half or the limits half is the wrong shape. | Repair it, or export again. |
| `lmrelay: <source> has an <auth\|upstreams> section that is not an object` | `lmrelay import` | The credential half or the upstream half is the wrong shape. | As above. Two rows rather than one, because the article changes with the word and this table is quoted verbatim. |
| `lmrelay: <source> has <server\|limits <scope>> <name> = <value>, which is not <a string\|a number\|a whole number>` | `lmrelay import` | A value would have written an `lmrelay.toml` that parses and then refuses to start. `nan` and `inf` are counted here too: TOML spells both, and neither is a limit. | Correct the type. The three wordings are the config file's own for those keys, so `requests = 2.5` is refused as not a whole number in both places rather than as two different mistakes. Checked here rather than at the next start, so an import never half applies. |
| `lmrelay: <source> has server log_level = <value>, which is not a logging level; expected DEBUG, INFO, WARNING, ERROR or CRITICAL` | `lmrelay import` | `log_level` is a string and still not a level. | Write one of the five. A type check cannot reach this one, and the pair of files written with it in them is a relay that refuses to start on every command afterwards, on a machine whose own config the import has already moved aside. |
| `lmrelay: <source> has a limits <scope> that is not an object` | `lmrelay import` | A scope under `limits` is not a table of the two keys. | Write it as `{"requests": 0, "period": "0s"}`, or leave the scope out to get those defaults. |
| `lmrelay: <source> has limits <scope> period = <value>, which is not a whole number and a unit` | `lmrelay import` | The period would have written a config the relay refuses to start from. | Write `"30s"`, `"5m"`, `"2h"` or `"0s"`. Checked here for the reason `log_level` is: both are the right type, and both half apply. |
| `lmrelay: <source> has limits <scope> <name> = <value>, and a limit cannot be negative` | `lmrelay import` | A limit below zero. | Use `0`, which is off. A negative is refused here for the same reason the config file refuses one. |
| `lmrelay: <source> has auth enabled = <value>, which is not true or false` | `lmrelay import` | The auth switch is not a boolean. | Write `true` or `false`. |
| `lmrelay: <source> has an auth tokens list that is not a list` | `lmrelay import` | `auth.tokens` is not an array. | Write it as an array of token objects. |
| `lmrelay: <source> has a token entry that is not an object` | `lmrelay import` | A member of `auth.tokens` is a bare string or a number. | Write each token as `{"token": "..."}`, with `id`, `label` and `created_at` optional. |
| `lmrelay: <source> has a token entry with no token in it` | `lmrelay import` | A token record carries no value. | Give it one, or remove the record. The id, the label and the time may be left out; the token itself names the credential and may not. |
| `lmrelay: <source> has a token with id <value>, which is not an id` | `lmrelay import` | An `id` that is not a whole number. | Write a whole number, or leave `id` out and let the import mint one. |
| `lmrelay: <source> has two tokens with id <N>` | `lmrelay import` | Two records share an id. | Renumber one. An id printed by `token list` must never come to name a second token. |
| `lmrelay: <source> has the same token twice` | `lmrelay import` | Two records carry the same credential. | Remove one. |
| `lmrelay: <source> has an upstream '<name>' that is not an object` | `lmrelay import` | An entry under `upstreams` is not a table. | Write it with `base_url`, `dialect` and `headers`. |
| `lmrelay: <source> has upstream '<name>' headers that are not an object` | `lmrelay import` | `headers` is not a table of strings. | Write it as an object, or `{}` for an upstream that needs no credential. |
| `lmrelay: <source> defines no upstreams, and a relay with none answers every request with a 404` | `lmrelay import` | The bundle carries an empty or absent `upstreams`. | Export from a relay that has one, or add the upstream by hand. |
| `lmrelay: <source> names default_upstream '<name>', which it does not define; it has: <list>` | `lmrelay import` | The default names an upstream the bundle does not carry. | Name one it has. Refused here rather than at the next start, so the import does not write a pair of files that cannot be loaded. |
| `lmrelay: <source> sets no default_upstream, so it would fall back to 'ollama', which it does not define; it has: <list>. Name one of them as default_upstream.` | `lmrelay import` | The bundle leaves the key out and defines no `ollama`. | Add `default_upstream` to its `server` section. The check covers the defaulted case as well as the spelled-out one: a hand-written bundle carrying a single upstream under any other name would otherwise import cleanly and refuse to start. |
| `lmrelay: <paths> <is\|are> already there, and an import replaces the whole configuration rather than merging into it. Pass --force to move it aside first.` | `lmrelay import` | A config or a state file already exists. | Read what is there first, then pass `--force`, which moves both aside before writing. Symmetric with `lmrelay init`, which refuses to overwrite too. |
| `lmrelay: <path>.bak is already there, and this import would overwrite it. Move or delete it first; nothing has been changed.` | `lmrelay import --force` | A previous import already left a backup. | Move or delete it. Nothing accumulates timestamped copies of files full of keys, and nothing silently replaces the one file you would reach for. |
| An upstream, dialect, base URL or reserved-name error naming the bundle's upstream | `lmrelay import` | An upstream in the bundle fails the same check a hand-written `[upstream.*]` table fails. | See the upstream rows in the startup and config table above; the messages are the same ones, because the bundle goes through the same parser. |

### Process control errors

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: this platform has no os.fork; run 'lmrelay run' in the foreground or start the relay from a service manager` | `lmrelay serve`, `restart` | Detaching is impossible on this platform, Windows in practice. | Use `lmrelay run`, or run it from a service manager. Refused up front rather than half-starting. |
| `lmrelay: autostart needs systemd (Linux) or launchd (macOS); everywhere else 'lmrelay serve' runs the relay detached on any POSIX system` | `lmrelay enable`, `disable` | No supported service manager was found. | Use `lmrelay serve` on POSIX, or write a unit for whatever manager this host does have. |
| `lmrelay: the relay did not start; see <log> for why` | `lmrelay serve`, `restart` | The detached process never reported a pid. | Read `lmrelay.log`. |
| `lmrelay: the relay exited during startup; see <log> for why` | `lmrelay serve`, `restart` | The detached process died before it recorded its pidfile. | Read `lmrelay.log`. The config parses in the child, so a bind failure lands here. |
| `lmrelay: no relay appeared within 10s; see <log> for why` | `lmrelay serve`, `restart` | The process is alive but never claimed the pidfile inside the start timeout. | Read `lmrelay.log`. |
| `lmrelay: pid <N> belongs to another user; stop it as its owner` | `lmrelay stop` | The running relay is another user's process. | Stop it as its owner. The pidfile is left in place rather than removed, which would only hide it. |
| `lmrelay: pid <N> belongs to another user; reload it as its owner` | `lmrelay reload` | The running relay is another user's process. | Reload it as its owner. |
| `lmrelay: <argv> failed: <detail>` | `lmrelay stop`, `restart`, `reload` under a manager | A `systemctl --user` or `launchctl` command exited non-zero. | Act on the detail, which is the manager's own words. |
| `lmrelay: '<argv>' failed with exit <N>: <detail>` | `lmrelay enable`, `disable` | Installing or removing the unit or agent failed. | Act on the detail. A quiet failure here would mean the relay does not come back after a reboot. |

### Token and provider errors

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: no tokens configured; run 'lmrelay token gen' first` | `lmrelay auth true` | Turning auth on with an empty token set would 401 every request, yours included. | Run `lmrelay token gen`, then `lmrelay auth true`. |
| `lmrelay: token is empty` | `lmrelay token add` | The token argument was blank or whitespace. | Pass the token. |
| `lmrelay: that token is already configured` | `lmrelay token add` | The same token value is already stored. | Nothing: it already works. `lmrelay token list --show` confirms it. |
| `lmrelay: no token with id <N>; known ids: <list>` | `lmrelay token delete` | No stored token carries that id. | Use an id from `lmrelay token list`. |
| `lmrelay: provider name '<name>' is reserved: it would shadow the Ollama/OpenAI path root` | `lmrelay provider add` | The name was `api` or `v1`. | Choose another name. |
| `lmrelay: '<name>' is not a known provider; pass --base-url to add it. Known providers: anthropic, deepseek, grok, ollama, openai` | `lmrelay provider add` | No preset carries that name, no `--base-url` was given, and the state holds no upstream of that name to take one from. | Add `--base-url`, and `--dialect` if it is not OpenAI-shaped. Re-keying an upstream the state already carries needs neither: the endpoint it is already pointed at is the one the rotation keeps. |
| `lmrelay: provider '<name>' needs a base_url starting with http:// or https://` | `lmrelay provider add` | `--base-url` has no scheme. | Give a full origin. |
| `lmrelay: provider '<name>' has dialect '<dialect>'; expected one of ollama, openai, anthropic` | `lmrelay provider add` | `--dialect` is not one of the three. | Use `ollama`, `openai` or `anthropic`. |
| `lmrelay: no provider '<name>' was added by the CLI; known: <list>` | `lmrelay provider delete` | State holds no provider under that name. | Use a name from the list. |

### Limit errors

`lmrelay limits set` is the one command that edits `lmrelay.toml`. Every refusal below
leaves the file exactly as it was.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: [limits.<scope>] in <path> would still read <x> rather than <y>` | `lmrelay limits set` | The edit parsed, but the scope did not end up carrying what was asked for, so something else in the file is defining it. | Edit that scope by hand. The file has not been written. |
| `lmrelay: no config at <path> to edit; limits live in lmrelay.toml. Run 'lmrelay init' first.` | `lmrelay limits set` | There is no config file to edit. | Run `lmrelay init`. Writing one here would produce a config with limits and no upstream, which the relay refuses to start from. |
| `lmrelay: editing [limits.<scope>] in <path> would not have left a file this relay can read (<detail>)` | `lmrelay limits set` | The edit was made, parsed, and would not have loaded. Almost always an inline table: `total = { requests = 1 }` is legal TOML that a line rewrite cannot reach. | Edit that scope by hand. The file has not been written. |

| `lmrelay: provider '<name>' was not added by the CLI; if it is defined in <config>, remove its [upstream.<name>] section by hand` | `lmrelay provider delete` | The name exists, but `lmrelay.toml` owns it and the CLI does not edit that file. | Delete the `[upstream.<name>]` section yourself, then `lmrelay reload`. |
| `lmrelay: --header expects NAME=VALUE, got '<pair>'` | `lmrelay provider add` | A `--header` argument had no `=`, or an empty name. | Write `--header Name=value`. Only the first `=` splits, so a value may contain more. |

### Warnings

Logged and then ignored. Nothing is refused and nothing stops.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: listening on <host> with auth off. Every caller that can reach this port can use the configured upstream credentials. Run 'lmrelay auth true'.` | relay startup, `lmrelay run --host`, `lmrelay reload` | A non-loopback bind demands no credential. On a reload it is asked about the host the socket is on, since `lmrelay auth false` can create the condition under a relay that is already listening. | Run `lmrelay auth true`, unless something in front of the relay already authenticates. |
| `lmrelay: <N> caller token(s) configured but auth is off; run 'lmrelay auth true' to require them` | any command that loads the config | Tokens exist but nothing checks them. | Run `lmrelay auth true`, or delete the tokens if the relay is meant to stay open. |
| `lmrelay: [limits.per_token] is configured but auth is off, so nothing is keyed by a token. [limits.per_address] and [limits.total] still apply. Run 'lmrelay auth true'.` | any command that loads the config | A per-credential limit is set on a relay where there is no credential to key it on. | Nothing, if auth is going on later: the scope becomes live the moment it does. Otherwise move the number to `[limits.per_address]` or `[limits.total]`. |
| `lmrelay: provider(s) <names> from state.json shadow the [upstream.*] of the same name in <config>` | any command that loads the config | A CLI-added provider is winning over a hand-written table. | Nothing, if that was the intent. Otherwise `lmrelay provider delete <name>`. |
| `lmrelay: <field> <old> -> <new>, ... in <config> but a reload cannot apply that: the socket is already bound and the client already open; restart to apply` | `lmrelay reload` | `host`, `port` or `connect_timeout` differs from what the running relay bound with, e.g. `lmrelay: port 11435 -> 8080, connect_timeout 10 -> 30 in ...`. | `lmrelay restart`. The fields are named individually, so a changed port does not hide an unchanged timeout, and each carries both values, because the running relay is the only thing that knows what it bound with. |
| `<error message>; keeping the running config` | relay log, on reload | The re-read config or state did not parse. The relay is still serving the one it already had. | Fix what the message names, then `lmrelay reload` again. |
| `lmrelay: pid <N> ignored SIGTERM for 10s; forcing it with SIGKILL` | `lmrelay stop`, `restart` | The relay did not exit on SIGTERM inside the stop timeout. | Nothing: the stop continues. A relay that needs SIGKILL every time is worth reading the log about. |
| `lmrelay: pid <N> is still there after SIGKILL` | `lmrelay stop`, `restart` | The process survived SIGKILL and the kernel has not finished tearing it down. | Check the process by hand before starting another relay on the same port. |
| `That was the last token and auth is on, so every request will now be refused. Add a token, or run 'lmrelay auth false'.` | `lmrelay token delete` | The token set is now empty while auth stays on. | Add a token, or run `lmrelay auth false`. |
| `Secrets were masked in it, so nothing was restored for: <names>.` | `lmrelay import` | The bundle was written with `--no-secrets`, so those caller tokens and provider headers arrived masked and were left out rather than restored as `***`. | Run `lmrelay token gen` and `lmrelay provider add <name> <key>`, which the next line names for you. A mask restored as a header would be sent to the provider and read as a wrong key. |
| `Auth is on and the bundle carried no usable token, so every request will now be refused. Run 'lmrelay token gen', or 'lmrelay auth false'.` | `lmrelay import` | The bundle turned auth on and carried no unmasked credential, usually a `--no-secrets` export. | Mint a token, or reopen the relay. |

### Notes

- The dialect refusal is a **400 and not a 404**, precisely so it cannot be mistaken for the
  provider's own 404 for a path that does not exist.
- The already-running refusal happens **before the bind**, because a failed bind names
  neither the other process nor a way out.
- The exposure warning is a **warning and not a refusal**, since running uncredentialed
  behind an authenticated nginx is legitimate.
- `lmrelay reload`, and `token gen`, `auth true`, `provider add` and the rest, report that
  they **signalled** the running relay rather than that the change is live. SIGHUP is
  delivered, not acknowledged, and a relay that cannot parse what it re-reads keeps the
  config it had. The outcome is in `lmrelay.log`.
- A 401 is written to the access log as `<client> <METHOD> <path> -> -: 401 (auth)`. No
  upstream was chosen, which is what the `-` says. **Every limit refusal names its upstream**,
  `-> ollama: 429 (rate, per_token)`, because admission happens in the relay route, after the
  upstream is selected and after the dialect check.
- All six limits refuse with **429 and not 503**. The relay is not out of capacity; some
  ceiling has been reached, and another caller's request at the same instant is served.
- A request refused by any scope is **charged to none of them**, so being turned away does
  not also drain an allowance the caller never spent.
- A **dialect refusal is charged nothing** either. Nothing forwarded is nothing charged, which
  costs the relay a client that can loop against a 400 without being rate limited, and buys
  one rule with no exception in it.
- Every line above carries a **request id** between the logger name and the message, and the
  lines about one request share it: `[c4eacac4] 127.0.0.1 GET /api/tags -> ollama: 200`. A
  line that belongs to no request, such as a reload or the exposure warning, carries `-`.
- A **scrape of `/metrics` writes no line**, and a scrape refused for a bad credential writes
  the ordinary 401 line. Everything the relay refuses is still counted in
  `lmrelay_auth_failures_total` and `lmrelay_refusals_total`, whether it was logged or not.

## Troubleshooting

| Symptom | Likely cause | Run |
|---|---|---|
| Every request comes back 401 | Auth is on and the token presented is not one of the configured ones | `lmrelay token list`, then present one of them; `lmrelay auth false` reopens the relay |
| Requests come back 429 | One of the six limits is set below what this client sends | The message names the scope, the measure and the number being enforced; raise that one, or set it to `0`. The access log line names the same pair, as `(rate, per_token)` |
| 429 on a relay whose own limits look generous | `[limits.total]` refused it: it is shared, so the traffic that filled it may be somebody else's | Raise `[limits.total] requests`, up to `OLLAMA_NUM_PARALLEL x OLLAMA_MAX_LOADED_MODELS`, or set `[limits.per_token]` so no one caller can fill it |
| A limit allows about twice what it says | Two relays are running and each counts its own callers | `lmrelay status` on each; every scope is per process, `[limits.total]` included, so put the limit in front of the pair or accept the doubling |
| A limit written as `rate_limit` is refused at startup | That key was replaced by three scopes | The message names all three; put the number in the scope you meant, usually `[limits.total]` |
| 502 naming `ollama` | Ollama is not running on 11434 | Start Ollama, then `lmrelay status` to confirm the upstream list |
| An occasional request takes tens of seconds before its first token | Ollama evicted a resident model to load the one this request named | `curl 127.0.0.1:11434/api/ps` first: if only one model stays resident, VRAM binds and `OLLAMA_MAX_LOADED_MODELS` will not help, so cut `num_ctx` or the rotation instead. If several stay but fewer than you rotate through, raise it to cover them. No relay setting affects either |
| 400 naming two dialects | The path belongs to a dialect the chosen upstream does not serve | Check the path prefix against the compatibility table in the README |
| A token or provider change had no effect | The relay was signalled but discarded what it re-read, or nothing was running | Read `lmrelay.log`, then `lmrelay reload` |
| A `host`, `port` or `connect_timeout` change had no effect | A reload cannot rebind a socket or re-time an open client | The warning names all three with both values, `port 11435 -> 8080`; then `lmrelay restart` |
| A Prometheus scrape comes back 401 | `/metrics` needs a credential, unlike `/healthz` | Give the job a `bearer_token` or `bearer_token_file` holding a value from `lmrelay token list --show`. A fail2ban jail will ban the polling host long before you notice otherwise |
| A labelled family shows `# HELP` and `# TYPE` and no numbers | The relay has served nothing of that kind since it started, so the series does not exist yet | Nothing: a labelled series is created at its first sample. Send one request and scrape again |
| `lmrelay_requests_total` carries a `status="500"` | A fault in lmrelay itself. An upstream that cannot be reached is a 502, and a status the upstream chose is the upstream's | Read `lmrelay.log`: the access line names the caller, the path and the upstream, and the traceback on the line after it carries the same request id |
| `lmrelay_request_ttfb_seconds` has two humps | Some callers send `"stream": false`, and an upstream answering one of those sends its headers only when the whole answer is done | Nothing to set. The relay cannot tell the two apart: the flag is in the request body, which it does not read |
| Every counter went back to zero | The relay restarted; they live in memory | Nothing. Prometheus reads across a counter reset. `lmrelay_build_info` says whether the version changed with it |
| `rate()` shows a drop that `lmrelay.log` does not | Two relays, or several uvicorn workers, each counting their own | Scrape each one as its own target; a scrape reaches one process and reports that process |
| `serve` reports that the relay did not start | The config or the bind failed inside the detached process | Read `lmrelay.log` |
| `status` says running but not responding | The pidfile names a live process, but `/healthz` did not answer on the recorded address | Read `lmrelay.log`, then `lmrelay restart` |
| A start refuses with `already running` | A relay, or a service manager unit, already owns the port | `lmrelay status` names the pid and the manager; then `lmrelay restart` |

## Banning repeat offenders with fail2ban

Every refused credential is one line in the relay's own log, carrying the caller's address:

```text
2026-08-31 10:25:34.595 [WARNING]: (lmrelay.app) [b9271105] 203.0.113.7 GET /api/tags -> -: 401 (auth)
```

The bracketed field before the address is the [request id](#the-request-id-in-the-log). It
sits between the logger name and the address, so a filter written against the older format,
which had the address immediately after `(lmrelay.app)`, matches nothing at all. Anything of
your own that reads this file by position needs the same edit.

A filter and a jail that read it ship with the source:

- [`contrib/fail2ban/filter.d/lmrelay-auth.conf`](../contrib/fail2ban/filter.d/lmrelay-auth.conf)
- [`contrib/fail2ban/jail.d/lmrelay.conf`](../contrib/fail2ban/jail.d/lmrelay.conf)

```bash
sudo cp contrib/fail2ban/filter.d/lmrelay-auth.conf /etc/fail2ban/filter.d/
sudo cp contrib/fail2ban/jail.d/lmrelay.conf /etc/fail2ban/jail.d/
fail2ban-regex ~/.lmrelay/lmrelay.log /etc/fail2ban/filter.d/lmrelay-auth.conf
```

The filter matches the relay refusing a credential and nothing else. A 401 that came from an
upstream and was relayed through, such as an expired provider key, is logged as a served request
and is deliberately not matched: the caller whose key stopped working is not an attacker. Nor
is a limit refusal matched, in any of the six scopes: a caller getting 429s is a misconfigured
client far more often than an attacker, and it is already being refused.

A Prometheus scrape with a wrong or missing token is refused like any other request, and its
line looks like any other, `GET /metrics -> -: 401 (auth)`. A job polling every fifteen
seconds hits the shipped `maxretry = 5` in seventy-five seconds, well inside the ten-minute
`findtime`, so a misconfigured `bearer_token` bans the monitoring host. Fix the job rather
than the filter: a scrape refused at the door is a credential the relay does not accept,
which is exactly what this jail is for.

### The jail ships disabled

`forwarded_allow_ips` is `"*"`, so uvicorn takes the client address from `X-Forwarded-For`
whenever that header is present, whoever sent it. Against a relay its callers reach
directly, anyone can pair a wrong token with a forged header and choose the address this
jail bans: a gateway, a colleague, the operator. A request carrying
`X-Forwarded-For: 198.51.100.42` and a bad token logs 198.51.100.42; that is measured, not
supposed.

The jail is therefore safe in one arrangement only: the relay bound to `127.0.0.1`, with a
trusted reverse proxy as the sole route in, so the header can only have come from that
proxy. A relay listening on `0.0.0.0` must not run it.

## Not in scope

No failover, retry or load balancing. No dialect translation. No model catalogue or
aliasing. No token accounting, usage database or budgets. No admin API or dashboard.
No caching. No TLS: put nginx in front.

**No token accounting, usage database or budgets** is why [`/metrics`](#metrics) carries no
per-token label. Aggregate counters that name nobody are not accounting, and that is the
line: the relay will say how often a token limit refused somebody, and will not say whose
token it was or how much anyone has spent.

The three scopes above are the whole of the limits subject, and these were considered and left
out:

- **A fourth scope, `[limits.upstream.<name>]`.** The relay does know the upstream from the
  path prefix, so it is buildable and the shape would take it without redesign. Three was the
  ask, and the expensive thing, which model a request names, stays unknowable either way.
- **Queueing instead of refusing.** A queue needs a timeout, turns a fast 429 into a slow one,
  and contradicts the commitment that admission is the only lever.
- **Anything per model.** The model is in the request body, both SDKs serialise it last, and
  the relay does not read request bodies. `OLLAMA_MAX_LOADED_MODELS` remains the answer.
- **Shared counters.** No Redis, no database, no second process. Every scope is counted in one
  process, `[limits.total]` included. So are the numbers behind `/metrics`.
- **`Retry-After` on a concurrency refusal**, and **503 anywhere**. Both for the reasons given
  in the limits section.
- **Live counters in `lmrelay status`.** The `limits` line says what the numbers are, read
  from the same files the relay reads, and `status` reports rather than asserts. What is in
  flight against them right now is a different question, and it is answered:
  `lmrelay_requests_in_flight` on [`/metrics`](#metrics). Putting it in `status` as well would
  make the CLI a second client of that endpoint, with a credential to find and a running
  relay to require, for a number `curl` already prints.
- **`lmrelay limits set`.** Limits are settings, settings live in the file, and the CLI does
  not edit `lmrelay.toml`. `lmrelay import` replaces it wholesale, after a backup, and
  says so.
