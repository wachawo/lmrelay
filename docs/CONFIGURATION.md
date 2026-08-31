# Configuration and Errors

The [README](https://github.com/wachawo/lmrelay/blob/main/README.md) covers installation and
usage. This document covers the config file, the state file, autostart, the per-caller limits,
the behaviour that is not obvious from the outside, what every message the relay prints means,
and how to have fail2ban act on refused credentials.

## Files on disk

Four files live in that config's directory:

| File | Written by | Holds |
|---|---|---|
| [`lmrelay.toml`](https://github.com/wachawo/lmrelay/blob/main/lmrelay/lmrelay.toml.example) | you | server settings and hand-written upstreams |
| `state.json` | the CLI | caller tokens, the auth switch, CLI-added providers |
| `lmrelay.pid` | the relay | the pid of the running process |
| `lmrelay.log` | the relay | stdout and stderr of a detached relay |

The split exists so that the CLI never has to rewrite a file you are editing: your comments
in `lmrelay.toml` survive forever. State is JSON rather than a second TOML file because it
is machine-owned, and because `tomllib` reads but cannot write.

`lmrelay init` writes `lmrelay.toml` with mode 0600, because the file is meant to hold
provider keys.

## Where the config is looked for

The config is looked for in three places, first hit wins, no merging:
`$LMRELAY_CONFIG` (also what a command's `--config PATH` sets), then `./lmrelay.toml`,
then `~/.lmrelay/lmrelay.toml`. If none exists the relay refuses to start rather than
serving 404s from an empty configuration.

## The config file

```toml
[server]
host             = "127.0.0.1"   # 0.0.0.0 only with auth on
port             = 11435         # beside Ollama, which keeps 11434
default_upstream = "ollama"      # used when the path has no upstream prefix
connect_timeout  = 10            # seconds to reach the upstream
log_level        = "INFO"
rate_limit       = 0             # requests per second per caller, 0 off
rate_burst       = 0             # how many may arrive at once, 0 off
max_concurrent   = 0             # simultaneous requests per caller, 0 off

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
```

The complete commented file ships as
[`lmrelay/lmrelay.toml.example`](https://github.com/wachawo/lmrelay/blob/main/lmrelay/lmrelay.toml.example)
and is what `lmrelay init` copies. There the hosted blocks are commented out, so a fresh
config starts with Ollama alone. Uncomment one once its variable is exported; an unset
`${VAR}` is a startup error.

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
- There is no auth switch in this file. A `[auth] token`, and `$LMRELAY_TOKEN`, are each
  accepted as one *additional* valid caller credential, so a container can inject one
  without invalidating yours; neither turns checking on. Caller tokens are otherwise
  `lmrelay token …` and the switch is `lmrelay auth true|false`.
- A provider added with `lmrelay provider add` wins over an `[upstream.<name>]` of the same
  name. The startup log names any upstream that was overridden, since this file is hand-written
  and its author deserves to hear that a command shadowed it.

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

`[auth] token` and `$LMRELAY_TOKEN` are each accepted as one *additional* valid caller
credential, so a container can inject one without invalidating yours; neither turns checking
on. Both count towards the token set that `auth true` requires.

`token list` masks tokens unless `--show` is passed. Token ids are monotonic, so an id keeps
meaning the same token after an unrelated delete.

## Per-caller limits

Two limits, both off unless you set them, and both counting the same caller:

| Keys | Count | Off when |
|---|---|---|
| `rate_limit`, `rate_burst` | requests per second, as a token bucket | `rate_limit = 0` |
| `max_concurrent` | requests in flight at the same moment | `max_concurrent = 0` |

They answer different questions. `rate_limit` is how often a caller may ask. `max_concurrent`
is how many requests it may have forwarded and not yet finished, which in front of one local
model is the one that decides whether anybody else gets a turn. Not "receiving at once": a
request waiting its turn inside Ollama, with `OLLAMA_NUM_PARALLEL` already spent, holds a
relay slot the whole time it is receiving nothing. That is the intent, since it is occupying
the upstream either way, but it matters when sizing this number against
`OLLAMA_NUM_PARALLEL`.

`rate_limit` is a token bucket rather than a counter per fixed window, because a window lets a
caller spend its whole allowance in the last instant of one window and again in the first
instant of the next, which is twice the rate across the boundary and exactly the burst a limit
is meant to prevent. `rate_burst` is how much may be spent at once; anything below 1 is read
as 1, and the key does nothing on its own while `rate_limit` is 0. Both are floats, so a limit
below one request per second can be written as one: `rate_limit = 0.5` is one request every
two seconds, where a whole number would have rounded it to 0 and turned the limit off.
`max_concurrent` is a whole number, because half a request cannot be in flight.

### What a caller is

Both key on the same thing: **the token when auth is on, the address otherwise**. One keying
rule to learn, not two.

The token comes first because it is what identifies a caller. Two machines sharing one token
are one caller and should share one allowance, while an address behind NAT is many callers
wearing one number, and limiting it limits the office rather than the offender. The address is
the fallback for a relay with auth off, where nothing else distinguishes anyone.

The check happens **after** the credential, so a wrong guess is a 401 and never spends the
allowance of the caller whose token was being guessed at.

`GET /healthz` is exempt from both, as it is from auth: it touches no upstream. A dialect
refusal spends a rate token, because the rate limit is charged before the path is examined,
but never a concurrency slot, because nothing was relayed.

**The address key is only as trustworthy as whatever is in front of the relay.** The relay
runs uvicorn with `proxy_headers` on and every forwarder trusted, so the address it counts
against is taken from `X-Forwarded-For` when that header is present. Behind an nginx that sets
it, that is the point: the count follows the real client rather than the proxy. Reachable
directly by anyone, it is a header the caller writes, and rotating it defeats both limits at
once. Measured, with auth off and `rate_limit = 1`: three requests with no header gave
`200, 429, 429`, while six requests each carrying a different made-up `X-Forwarded-For` all
gave `200`, and left six buckets behind. The same forgery lets a caller occupy a named
victim's concurrency slots.

So the address key bounds a cooperating client, not an adversarial one. If the limits have to
hold against callers you do not trust, turn auth on and let them key on the token, which a
caller cannot choose. This is the same caveat the fail2ban section below states for its own
reason, and it applies here for the same one.

### What a refused caller sees

Over the rate limit, `429` and a `Retry-After` in whole seconds:

```json
{"error": "lmrelay: rate limit of 2/s burst 5 exceeded"}
```

That header is computed, not guessed: it is the time until one token has refilled, rounded up
because the header takes no fractions and rounding down would invite a retry that is refused
again.

The two numbers in the message are the ones the limiter is **using**, which are not always the
ones in the file: `rate_burst` defaults to 0, meaning unset, and anything below 1 is read as 1.
So `rate_limit = 2` on its own is reported as `burst 1`, because that is what is being
enforced. A message quoting the configured `0` would be telling a caller no request is allowed
at the moment one has just been served.

Over the concurrency limit, `429` and **no** `Retry-After`:

```json
{"error": "lmrelay: too many simultaneous requests (limit 4); one of yours must finish first"}
```

There is deliberately no header here. A slot is freed when some other request finishes, and
with no read timeout a generation can run for minutes, so the relay does not know when that
will be. A guessed number would be a lie.

The clause after the semicolon is there because the limit is per caller: what has to end is
one of this caller's own requests, not somebody else's. That is exact when the key is a token.
When auth is off and the key is an address, "yours" is whatever shares that address, so behind
NAT the request that has to finish may be a colleague's, and the caller is being told to look
for something of its own that does not exist. It is the same fact as the NAT caveat two
sections up, arriving where it is least convenient.

Neither is a 503. Nothing is wrong with the relay or with the upstream, and the same request
from another caller is served at that instant.

Both are logged as one line naming the caller:

```text
203.0.113.7 POST /api/chat -> -: 429 (rate limit)
203.0.113.7 POST /api/chat -> ollama: 429 (concurrency)
```

The upstream is `-` on the first and named on the second, and that is not an inconsistency:
the rate limit is charged in the middleware, before any upstream has been chosen, while the
concurrency slot is taken in the relay route, after selection and after the dialect check. The
fail2ban filter that ships with the source matches neither. A rate-limited caller is a
misconfigured client far more often than an attacker, and it is already being refused.

### A slot is held until the last byte

A concurrency slot is released when the response **body ends**, not when its headers arrive.
That is the whole point: the relay streams, so a request whose headers came back in 30ms may
still be delivering tokens two minutes later, and a slot freed at the headers would bound
nothing. A client that hangs up mid-stream releases its slot too.

The relay never cuts a stream to reclaim a slot. Admission is the only lever, because
interrupting a response in flight would break the guarantee the rest of this document rests
on.

### The counts live in this process, in memory

There is no database, no Redis and no shared state. That has consequences worth stating:

- **More uvicorn workers are not a way around it, because the relay does not run that way.**
  Started with `--workers 3`, the first worker to reach the pidfile claims it and the rest
  refuse with `already running`, at which point uvicorn stops the parent too. The result is no
  relay at all rather than three sets of counts, so the number you wrote is the number one
  caller gets.
- **Two relays are.** Separate processes, separate config directories, separate counts: a
  caller that can reach both has both allowances. If something is balancing across a pair of
  relays, either give each the whole limit and accept the doubling, or put the limit in front
  of them.
- **A restart forgets everything.** Every caller starts with a full bucket and no slots held.
- **The tables do not grow without bound.** The concurrency counter holds an entry only while
  that caller has something in flight, and the last release deletes it. The bucket table needs
  a sweep, which runs at most once a minute and drops any caller idle long enough that its
  bucket has refilled to full: forgetting a full bucket changes no decision, because a caller
  with no bucket starts with one. A bucket that has *not* refilled is kept however idle it is,
  since dropping that one would be handing back an allowance nobody waited for. So the table
  holds roughly the callers seen in the last five minutes, not every address that ever knocked.
- **A reload applies new numbers without disturbing anything in flight.** Changing
  `rate_limit` or `rate_burst` rebuilds the bucket table, which starts every caller full, so
  it is done only when one of the two numbers actually moved rather than on every unrelated
  reload. `max_concurrent` needs no such care: it is read per request, so a live request keeps
  the slot it holds and the new number applies to the next arrival.

## When Ollama has no room to load a model

Ollama keeps models resident and evicts one to load another. Interleaving requests for models
that cannot coexist costs a full reload every time, and with enough interleaving the machine
spends its time loading rather than answering.

The tax is large. Measured here, a model answered in 0.18-0.65s warm and 5.9-7.0s cold, so a
swap costs between 9x and 24x what the same request costs without one. Treat the ratio as the
finding and the seconds as this machine's: in one run of two models alternating, individual
swaps of the same pair ranged from 8.1s to 13.7s, and a "cold" load after an unload still
reads the weights out of the page cache, so a first load after boot is worse again. Ollama's
own `load_duration` under-reports what the caller waits by
3.6-5.1x, because it excludes the queueing and the prompt evaluation around it. Do not size
anything from it.

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

**What the relay does:** it bounds how many requests one caller can have in flight, which is
`max_concurrent` above. **What the relay does not do:** it does not know which model a request
names, does not queue by model, and cannot prevent an eviction. The model name is in the
request body, and the body is a stream the relay forwards without reading.

One result is worth stating because it is counter-intuitive, and because it stops an operator
configuring against a problem they do not have: **concurrency on one model was never the
problem.** Six simultaneous callers for one cold model cost one load, shared: all six came
back reporting the same `load_duration` to within 0.04s, and all six finished in 9.12s against
5.95s for a single cold request on its own. Two models that both fit alternate for free, at
0.18s a turn. What costs is the sequence of model names over time, not the overlap between
requests, which is also why **no concurrency limit can fix thrashing**: alternating two models
strictly serially, one at a time, still paid a full cold load on every swap. `max_concurrent`
is not a thrashing setting and will not be made into one.

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

## Autostart

`lmrelay enable` registers a systemd `--user` unit on Linux or a launchd agent on macOS,
then starts it. Elsewhere it refuses: on a POSIX box with neither manager `lmrelay serve`
runs the relay detached, and on Windows only `lmrelay run` works.

From then on `stop`, `restart` and `reload` go through that manager instead of the pidfile,
so the two cannot disagree about who owns the process; each command says which path it took.

## Reload

`lmrelay reload` sends the running relay a SIGHUP, and it re-reads `lmrelay.toml` and
`state.json` in place. Nothing in flight is disturbed: connections stay open and a stream
already being relayed runs to its end. Every command that writes a change, whether `token gen`,
`auth true`, `provider add` or the rest, signals the relay for you, so an explicit reload
is what you run after editing `lmrelay.toml` by hand.

| Key | Applied by | Why |
|---|---|---|
| `[upstream.*]`, and providers added by `lmrelay provider add` | `lmrelay reload` | Base URLs and headers are read from the config on every request, so the next request uses the new set. |
| `default_upstream` | `lmrelay reload` | Chosen per request, out of that same config. |
| Caller tokens, and the auth switch | `lmrelay reload` | Also read per request, so `lmrelay auth true` starts requiring a credential as soon as the relay has re-read state. |
| `log_level` | `lmrelay reload` | Logging is reconfigured in place, and the new level governs the next line the relay writes. |
| `rate_limit`, `rate_burst` | `lmrelay reload` | The limiter is rebuilt when either number moves, and left alone when neither does: a fresh limiter starts every caller with a full bucket, which would clear the allowance of whoever was being limited at that moment. |
| `max_concurrent` | `lmrelay reload` | Read per request rather than built into anything, so a request already streaming keeps the slot it holds and the new number governs the next arrival. |
| `host`, `port` | `lmrelay restart` | The socket is already bound, and a running server cannot move it. |
| `connect_timeout` | `lmrelay restart` | The shared httpx client is already open and carries the timeout; closing it to re-time would abort every stream being relayed through it. |

The reload log names whichever of `host`, `port` and `connect_timeout` differs from what the
running relay started with, and says a restart applies them. They are named individually, so
a changed port does not hide an unchanged timeout.

The keys above them are applied without comment, except the three that say what they moved
from and to, so that a reload can be read back afterwards:

```text
lmrelay: log_level INFO -> DEBUG
lmrelay: rate limit off -> 2/s burst 5
lmrelay: concurrency off -> 4 in flight (from the next request; answers in flight keep the slot they hold)
```

A config the relay cannot use is logged and discarded, and it carries on serving the one it
already had. That covers `state.json` as much as `lmrelay.toml`, and a value the file spells
wrongly (`port = "eleven"`, `log_level = "verbose"`) as much as a syntax error. A typo must
not take the relay down.

The CLI reports that it signalled the relay, never that the change took effect. SIGHUP is
delivered, not acknowledged, so `lmrelay reload` and every command that reloads on your
behalf stop at what they did. The outcome is in `lmrelay.log`: a discarded reload is logged
whatever the level, an accepted one at `INFO` or below.

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
  streamed answer.
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
code fills in. Almost all of them begin with `lmrelay: `, so grepping a log for that prefix
finds everything lmrelay said and nothing an upstream said; the one exception is noted in the
warnings table.

### Request-time errors

These are JSON bodies of the form `{"error": "..."}` returned to the caller.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: missing or invalid credential` | relay response, 401 | Auth is on and the request carried no credential, or one that matches no configured token. | Present a configured token. Both `Authorization: Bearer <token>` and `x-api-key: <token>` are accepted carriers. `lmrelay token list` shows which exist; `lmrelay auth false` reopens the relay. |
| `lmrelay: rate limit of <rate>/s burst <burst> exceeded` | relay response, 429 | The caller has asked more often than `[server] rate_limit` allows. The two numbers are the ones being enforced, so the burst reads as `1` when `rate_burst` is unset or below 1. | Wait: `Retry-After` on the response is the whole number of seconds until one request has refilled. Raise `rate_limit`, or `rate_burst` if the traffic is bursty rather than fast. |
| `lmrelay: too many simultaneous requests (limit <N>); one of yours must finish first` | relay response, 429 | The caller already holds `[server] max_concurrent` relayed requests whose bodies have not finished. | Wait for one of your own to finish, or raise `max_concurrent`. There is deliberately no `Retry-After`: a slot frees when an answer ends, and with no read timeout the relay cannot know when that will be. |
| `lmrelay: upstream '<name>' speaks the <Dialect> API; '<path>' is an <Other>-dialect path. lmrelay forwards requests unchanged and does not translate between dialects.` | relay response, 400 | The path belongs to a dialect the chosen upstream does not serve. | Change the path prefix to an upstream of that dialect, or point the client at an endpoint the upstream has. |
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
| `lmrelay: no config found; looked at ./lmrelay.toml and <home path>. Run 'lmrelay init'.` | any command that loads the config | Neither `$LMRELAY_CONFIG`, nor `./lmrelay.toml`, nor `~/.lmrelay/lmrelay.toml` exists. | Run `lmrelay init`, or point `--config PATH` at the file you have. |
| `lmrelay: cannot read <path>: <Type>: <detail>` | any command that loads the config | The file could not be opened, or TOML could not parse it. | Fix the syntax the detail names, or the permissions on the path. |
| `lmrelay: config has no [upstream.*] sections in <config> and no providers in <state>. Run 'lmrelay provider add' or add an [upstream.*] table.` | any command that loads the config | The config parses but defines no upstream at all, and state has none either. | Add an `[upstream.*]` table, or run `lmrelay provider add`. |
| `lmrelay: default_upstream '<name>' is not defined; known upstreams: <list>` | any command that loads the config | `[server] default_upstream` names an upstream that no source defines. | Set it to one of the names listed, or define the one it names. |
| `lmrelay: [server] <port\|connect_timeout\|max_concurrent> must be a whole number, got <value>` | any command that loads the config | The key holds something `int()` cannot read, usually a quoted number or a typo. | Write it unquoted, as a number. Refused rather than coerced, so a reload discards it like any other unusable config instead of raising out of the signal handler. |
| `lmrelay: [server] <rate_limit\|rate_burst> must be a number, got <value>` | any command that loads the config | The key holds something `float()` cannot read, usually a quoted number. | Write it unquoted. A fraction is allowed here, which is why these two are not read as whole numbers. |
| `lmrelay: [server] <rate_limit\|rate_burst> cannot be negative, got <value>` | any command that loads the config | One of the two rate keys is below zero. | Use `0` to turn the limit off. A negative is refused rather than read as off, because it is a typo, and admitting it as another spelling of "off" would hide the mistake behind the behaviour it causes. |
| `lmrelay: [server] max_concurrent cannot be less than 0, got <value>` | any command that loads the config | `max_concurrent` is negative. Worded as a minimum rather than as a sign because it is a whole number read through the same reader as `port`, which has no minimum at all. | Use `0` to turn the limit off. |
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
| `lmrelay: '<name>' is not a known provider; pass --base-url to add it. Known providers: anthropic, deepseek, grok, ollama, openai` | `lmrelay provider add` | No preset carries that name and no `--base-url` was given. | Add `--base-url`, and `--dialect` if it is not OpenAI-shaped. |
| `lmrelay: provider '<name>' needs a base_url starting with http:// or https://` | `lmrelay provider add` | `--base-url` has no scheme. | Give a full origin. |
| `lmrelay: provider '<name>' has dialect '<dialect>'; expected one of ollama, openai, anthropic` | `lmrelay provider add` | `--dialect` is not one of the three. | Use `ollama`, `openai` or `anthropic`. |
| `lmrelay: no provider '<name>' was added by the CLI; known: <list>` | `lmrelay provider delete` | State holds no provider under that name. | Use a name from the list. |
| `lmrelay: provider '<name>' was not added by the CLI; if it is defined in <config>, remove its [upstream.<name>] section by hand` | `lmrelay provider delete` | The name exists, but `lmrelay.toml` owns it and the CLI does not edit that file. | Delete the `[upstream.<name>]` section yourself, then `lmrelay reload`. |
| `lmrelay: --header expects NAME=VALUE, got '<pair>'` | `lmrelay provider add` | A `--header` argument had no `=`, or an empty name. | Write `--header Name=value`. Only the first `=` splits, so a value may contain more. |

### Warnings

Logged and then ignored. Nothing is refused and nothing stops.

| Message | Where | Means | Do |
|---|---|---|---|
| `lmrelay: listening on <host> with auth off. Every caller that can reach this port can use the configured upstream credentials. Run 'lmrelay auth true'.` | relay startup, `lmrelay run --host`, `lmrelay reload` | A non-loopback bind demands no credential. On a reload it is asked about the host the socket is on, since `lmrelay auth false` can create the condition under a relay that is already listening. | Run `lmrelay auth true`, unless something in front of the relay already authenticates. |
| `lmrelay: <N> caller token(s) configured but auth is off; run 'lmrelay auth true' to require them` | any command that loads the config | Tokens exist but nothing checks them. | Run `lmrelay auth true`, or delete the tokens if the relay is meant to stay open. |
| `lmrelay: provider(s) <names> from state.json shadow the [upstream.*] of the same name in <config>` | any command that loads the config | A CLI-added provider is winning over a hand-written table. | Nothing, if that was the intent. Otherwise `lmrelay provider delete <name>`. |
| `lmrelay: <fields> changed in <config> but a reload cannot apply that: the socket is already bound and the client already open; restart to apply` | `lmrelay reload` | `host`, `port` or `connect_timeout` differs from what the running relay bound with. | `lmrelay restart`. The fields are named individually, so a changed port does not hide an unchanged timeout. |
| `<error message>; keeping the running config` | relay log, on reload | The re-read config or state did not parse. The relay is still serving the one it already had. | Fix what the message names, then `lmrelay reload` again. |
| `lmrelay: pid <N> ignored SIGTERM for 10s; forcing it with SIGKILL` | `lmrelay stop`, `restart` | The relay did not exit on SIGTERM inside the stop timeout. | Nothing: the stop continues. A relay that needs SIGKILL every time is worth reading the log about. |
| `lmrelay: pid <N> is still there after SIGKILL` | `lmrelay stop`, `restart` | The process survived SIGKILL and the kernel has not finished tearing it down. | Check the process by hand before starting another relay on the same port. |
| `That was the last token and auth is on, so every request will now be refused. Add a token, or run 'lmrelay auth false'.` | `lmrelay token delete` | The token set is now empty while auth stays on. | Add a token, or run `lmrelay auth false`. This is the one message with no `lmrelay: ` prefix. |

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
  upstream was chosen, which is what the `-` says. A refused rate limit takes the same shape
  for the same reason, `429 (rate limit)`. A refused concurrency slot **names its upstream**,
  `-> ollama: 429 (concurrency)`, because that check happens after the upstream is selected.
- Both limits refuse with **429 and not 503**. The relay is not out of capacity; this caller
  has used its share of it, and another caller's request at the same instant is served.

## Troubleshooting

| Symptom | Likely cause | Run |
|---|---|---|
| Every request comes back 401 | Auth is on and the token presented is not one of the configured ones | `lmrelay token list`, then present one of them; `lmrelay auth false` reopens the relay |
| Requests come back 429 | A per-caller limit is set below what this client sends | The message names which limit and what it is set to; raise `rate_limit`, `rate_burst` or `max_concurrent`, or set it to `0` |
| A limit allows about twice what it says | Two relays are running and each counts its own callers | `lmrelay status` on each; a limit is per process, so put it in front of the pair or accept the doubling |
| 502 naming `ollama` | Ollama is not running on 11434 | Start Ollama, then `lmrelay status` to confirm the upstream list |
| An occasional request takes tens of seconds before its first token | Ollama evicted a resident model to load the one this request named | `curl 127.0.0.1:11434/api/ps` first: if only one model stays resident, VRAM binds and `OLLAMA_MAX_LOADED_MODELS` will not help, so cut `num_ctx` or the rotation instead. If several stay but fewer than you rotate through, raise it to cover them. No relay setting affects either |
| 400 naming two dialects | The path belongs to a dialect the chosen upstream does not serve | Check the path prefix against the compatibility table in the README |
| A token or provider change had no effect | The relay was signalled but discarded what it re-read, or nothing was running | Read `lmrelay.log`, then `lmrelay reload` |
| A `host`, `port` or `connect_timeout` change had no effect | A reload cannot rebind a socket or re-time an open client | `lmrelay restart` |
| `serve` reports that the relay did not start | The config or the bind failed inside the detached process | Read `lmrelay.log` |
| `status` says running but not responding | The pidfile names a live process, but `/healthz` did not answer on the recorded address | Read `lmrelay.log`, then `lmrelay restart` |
| A start refuses with `already running` | A relay, or a service manager unit, already owns the port | `lmrelay status` names the pid and the manager; then `lmrelay restart` |

## Banning repeat offenders with fail2ban

Every refused credential is one line in the relay's own log, carrying the caller's address:

```text
2026-08-31 10:25:34.595 [WARNING]: (lmrelay.app) 203.0.113.7 GET /api/tags -> -: 401 (auth)
```

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
and is deliberately not matched: the caller whose key stopped working is not an attacker.

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
aliasing. No token accounting, usage database or budgets. No admin API, dashboard or
metrics. No caching. No TLS: put nginx in front. The per-caller limits above are the whole of
that subject: they are counted in one process rather than shared between several, and nothing
queues or schedules by model.
