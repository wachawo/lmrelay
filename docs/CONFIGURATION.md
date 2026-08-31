# Configuration and Errors

The [README](https://github.com/wachawo/lmrelay/blob/main/README.md) covers installation and
usage. This document covers the config file, the state file, autostart, the behaviour that is
not obvious from the outside, what every message the relay prints means, and how to have
fail2ban act on refused credentials.

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
| `host`, `port` | `lmrelay restart` | The socket is already bound, and a running server cannot move it. |
| `connect_timeout` | `lmrelay restart` | The shared httpx client is already open and carries the timeout; closing it to re-time would abort every stream being relayed through it. |

The reload log names whichever of `host`, `port` and `connect_timeout` differs from what the
running relay started with, and says a restart applies them. They are named individually, so
a changed port does not hide an unchanged timeout. The keys above them are applied without
comment.

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
| `lmrelay: [server] <port\|connect_timeout> must be a whole number, got <value>` | any command that loads the config | The key holds something `int()` cannot read, usually a quoted number or a typo. | Write it unquoted, as a number. Refused rather than coerced, so a reload discards it like any other unusable config instead of raising out of the signal handler. |
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
  upstream was chosen, which is what the `-` says.

## Troubleshooting

| Symptom | Likely cause | Run |
|---|---|---|
| Every request comes back 401 | Auth is on and the token presented is not one of the configured ones | `lmrelay token list`, then present one of them; `lmrelay auth false` reopens the relay |
| 502 naming `ollama` | Ollama is not running on 11434 | Start Ollama, then `lmrelay status` to confirm the upstream list |
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
metrics. No caching or rate limiting. No TLS: put nginx in front.
