## lmrelay - a credentialed relay beside a local Ollama

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

If you work with **Ollama**, you run into this: by default it is reachable only from localhost,
and it has no built-in authentication. Connecting to Ollama from another machine usually means
changing its systemd configuration, or putting a reverse proxy in front of it. **lmrelay** solves
that. It installs with `pip` and runs as a daemon beside Ollama: it listens on a port of its own
and, when you want it to, requires a credential for access.

**[English](https://github.com/wachawo/lmrelay/blob/main/README.md)** | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay routes clients to a local Ollama or to a hosted provider](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### Requirements

- Python 3.11 or higher, and four dependencies: FastAPI, starlette, uvicorn and httpx.
- Linux and macOS run every command, including `serve` (detached) and `enable` — a systemd
  `--user` unit on Linux, a launchd agent on macOS, and a refusal where neither is installed.
- Windows runs `run` only. `serve` reports that the platform has no `os.fork`, and `enable`
  that there is no systemd or launchd, rather than half-starting.
- A local Ollama on 11434 is the default upstream, but it is not required. A relay with only
  hosted providers configured is valid, as long as `default_upstream` names one of them.

### Installation

```bash
pip install lmrelay
```

Or the current `main`, which may be ahead of the release:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### Quick start

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama keeps 11434 and its installation is left exactly as it is. Clients are repointed at
11435 instead. That is the trade: nothing about an existing Ollama has to change, and the
relay is opt-in per client.

Auth is off in a fresh state, so on loopback this is a transparent proxy in front of Ollama.
That is deliberate: a relay you have just installed should not lock you out of your own
Ollama before you have a token. Point a client at 11435 and it works:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### Checking it works

Ask the relay for the model list. Either dialect will do; both reach the same Ollama:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

Then put a model to work. `qwen3:8b` here is whatever `ollama list` shows on your machine:

```bash
curl http://127.0.0.1:11435/api/generate -d '{
  "model": "qwen3:8b",
  "prompt": "Reply with exactly: it works",
  "stream": false,
  "think": false
}'
```

```bash
curl http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
  "model": "qwen3:8b",
  "messages": [{"role": "user", "content": "say ok"}]
}'
```

`qwen3` reasons before it answers, and only Ollama's dialect has a switch for that: the `"think": false` above. Through `/v1/chat/completions` the reasoning arrives inside the content as a `<think>` block, because lmrelay forwards what the upstream produced and does not edit it.

With auth on, every one of these needs the credential:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $LMRELAY_TOKEN"
```

### Running it for real

```bash
lmrelay token gen --label laptop   # printed once, never again
lmrelay auth true                  # now start requiring it
lmrelay enable                     # start at login, and start now
lmrelay status
```

```
lmrelay      running (pid 40213), healthy
listening    127.0.0.1:11435
config       /home/u/.lmrelay/lmrelay.toml
state        /home/u/.lmrelay/state.json
upstreams    anthropic, ollama, openai (default: ollama)
auth         on, 2 tokens
autostart    systemd: enabled, active
```

`enable` registers a systemd `--user` unit on Linux or a launchd agent on macOS, then starts
it. From then on `stop`, `restart` and `reload` go through that manager instead of the
pidfile, so the two cannot disagree about who owns the process. On a POSIX box with neither
manager, `lmrelay serve` runs the relay detached.

### Usage

| Command | Does |
|---|---|
| `lmrelay init` | write `~/.lmrelay/lmrelay.toml` |
| `lmrelay run` | run in the foreground |
| `lmrelay serve` | run detached, appending to `lmrelay.log` |
| `lmrelay stop` | stop the running relay |
| `lmrelay restart` | stop it, then start it detached again |
| `lmrelay reload` | re-read the config without dropping a connection |
| `lmrelay status` | what is running, where, with which upstreams |
| `lmrelay enable` | start at login, and start now |
| `lmrelay disable` | undo `enable` |
| `lmrelay auth true\|false` | require a caller credential, or do not |
| `lmrelay token gen [--label L]` | mint a token and print it once |
| `lmrelay token add TOKEN [--label L]` | register a token you chose yourself |
| `lmrelay token list [--show]` | list tokens, masked unless `--show` |
| `lmrelay token delete ID` | remove one by the id `token list` prints |
| `lmrelay provider add NAME TOKEN` | add or rotate an upstream |
| `lmrelay provider list [--show]` | every upstream, from the file and from state |
| `lmrelay provider delete NAME` | remove a provider that state owns |

`run`, `serve` and `restart` take `--host` and `--port`. `provider add` takes `--base-url`,
`--dialect` and a repeatable `--header K=V`; with a known name — `openai`, `anthropic`,
`deepseek`, `grok`, `ollama` — the base URL, dialect and header shape come from a preset,
so `lmrelay provider add openai sk-...` is the whole command. `--config PATH` is accepted by
every command that reads the config or the state — that is, every command except `init`,
which always writes `~/.lmrelay/lmrelay.toml`, and `disable`, which reads neither.

### Choosing an upstream

The first path segment selects the upstream if and only if it exactly matches a key in
`[upstream]`. Otherwise `default_upstream` handles the request and the path is untouched.

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

So a client only has to learn the port once, and retargeting one at a different provider is
a single line:

```python
from openai import OpenAI
from anthropic import Anthropic

OpenAI(base_url="http://relay:11435/openai/v1", api_key=RELAY_TOKEN)
OpenAI(base_url="http://relay:11435/v1", api_key=RELAY_TOKEN)  # Ollama
Anthropic(base_url="http://relay:11435/anthropic", api_key=RELAY_TOKEN)
```

```bash
curl http://127.0.0.1:11435/api/chat \
  -H "Authorization: Bearer $LMRELAY_TOKEN" \
  -d '{
  "model": "llama3",
  "messages": [{"role": "user", "content": "hi"}]
}'
```

`GET /healthz` answers `{"status": "ok"}` without touching an upstream and without a
credential. Everything else goes through the relay.

### Compatibility

lmrelay forwards the method, path, query string and body bytes **unchanged**, and it does not
translate between API dialects.

| Your client speaks | Path it uses | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama serves an OpenAI-compatible surface at `/v1/*` alongside its native `/api/*`. This
is the practically important cell: an OpenAI-shaped client reaches **all** of ollama,
openai, deepseek and grok by changing only the path prefix.

The four cases that do not work, and the reason each one cannot be made to work, are in the
configuration document.

Where lmrelay can tell that a path certainly does not exist upstream, it says so itself
rather than letting the provider's 404 look like your mistake:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

Every error lmrelay generates begins with `lmrelay: `, so it is never mistaken for something
the provider said.

**[Configuration and Errors](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - the config file, caller tokens, providers, autostart, streaming behaviour, and what every error means.

### Testing

```sh
pip install -e '.[test]'
pytest
```

Most of the suite drives the app in process against a recording upstream, so it needs no
network and no Ollama. [`tests/test_streaming.py`](tests/test_streaming.py) is the
exception: it runs the relay under uvicorn in front of an upstream that answers a chunk at a
time, because the property it checks — that the caller has the first line before the
upstream has written the last — cannot be seen through an in-process client.

### Why not nginx?

nginx already reverse-proxies, so a daemon has to earn its place. Briefly, point by point:

- **The Authorization header is already taken, and that is what decides it.** Every client
  sends `Authorization: Bearer <key>` (the OpenAI SDK, the curl examples above) or
  `x-api-key` (the Anthropic SDK); nginx's `auth_basic` needs that same header to carry
  `Basic <base64>`, and refuses everything else. One header, two owners. Credentials in the
  URL do get past it, but httpx writes them into that same header: an OpenAI-SDK caller then
  arrives as `Basic`, having replaced the bearer it meant to send.
- **Checking a token in nginx puts the tokens in `nginx.conf`.** A `map` and an `internal`
  location do it without a backend, but each token is then a plaintext line in a root-owned
  `0644` file, and adding or revoking one takes an edit and a reload.
- **Provider keys end up inside `nginx.conf`.** A `location` and a
  `proxy_set_header Authorization "Bearer sk-..."` for each one, plus
  `proxy_ssl_server_name on` when the upstream speaks TLS. Here it is one command, and the
  key lives in a `0600` file.
- **`htpasswd` has no ids or rotation.** `lmrelay token gen --label laptop`, `token list`
  and `token delete 1` do.
- **nginx's defaults break streaming.** `proxy_buffering` is on and `proxy_read_timeout` is
  60s, and a large local model can think for longer than a minute before its first token.
  Both have to be found and turned off, usually after an answer has been cut in half.
- **A wrong-dialect path gets the provider's own 404 through nginx.** For the shapes it
  recognises — an Anthropic path sent to an OpenAI upstream, say — the relay answers 400 in
  its own words, so the mistake is not misread as the provider's.
- **nginx ships with neither macOS nor Windows.** `pip install` works the same on both.

Where nginx wins: TLS, real rate limiting, and already being installed. lmrelay has none of
the three, and is not going to. The two compose rather than compete — nginx in front for
TLS, tokens and providers here.

### License

MIT License. See [LICENSE](LICENSE).
