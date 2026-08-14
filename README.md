# lmrelay

A small HTTP relay that sits in front of a local [Ollama](https://ollama.com), requires a
credential from its callers, and can be pointed at a hosted provider — OpenAI, Anthropic,
DeepSeek, Grok — by prefixing one path segment. It has one config file, no database, and no
state.

## lmrelay is a credentialed passthrough, not a translator

lmrelay forwards the method, path, query string and body bytes **unchanged**. It does not
translate between API dialects. Everything below follows from that one sentence, so read
the table before you install anything.

| Your client speaks | Path it uses | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama serves an OpenAI-compatible surface at `/v1/*` alongside its native `/api/*`. This
is the practically important cell: an OpenAI-shaped client reaches **all** of ollama,
openai, deepseek and grok by changing only the path prefix.

What does not work, and why:

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

Where lmrelay can tell that a path certainly does not exist upstream, it says so itself
rather than letting the provider's 404 look like your mistake:

```json
{"error": "lmrelay: upstream 'anthropic' speaks the Anthropic API; '/v1/chat/completions' is an OpenAI-dialect path. lmrelay forwards requests unchanged and does not translate between dialects."}
```

Every error lmrelay generates begins with `lmrelay: `, so it is never mistaken for something
the provider said.

## Install

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

Python 3.11 or newer. Three dependencies: FastAPI, uvicorn and httpx.

## Configure

```bash
lmrelay init       # writes ~/.lmrelay/lmrelay.toml, mode 0600
```

The config is looked for in three places, first hit wins, no merging:

1. `$LMRELAY_CONFIG` (also what `lmrelay serve --config PATH` sets)
2. `./lmrelay.toml`
3. `~/.lmrelay/lmrelay.toml`

If none exists the relay refuses to start rather than serving 404s from an empty
configuration.

```toml
[server]
host             = "127.0.0.1"   # 0.0.0.0 only with a token set below
port             = 11434         # the port Ollama clients already expect
default_upstream = "ollama"      # used when the path has no upstream prefix
connect_timeout  = 10            # seconds to reach the upstream
log_level        = "INFO"

# The credential a CALLER must present to lmrelay. Sent as either
#   Authorization: Bearer <token>       (OpenAI SDKs, curl, Ollama clients)
#   x-api-key: <token>                  (Anthropic SDKs)
# It is stripped from the request and never forwarded upstream.
# LMRELAY_TOKEN overrides it. Leave unset to disable caller auth.
[auth]
token = "CHANGE-ME"

# Local Ollama. Needs no credential, so it has no headers at all.
[upstream.ollama]
base_url = "http://127.0.0.1:11435"
dialect  = "ollama"

# OpenAI. Wants a bearer token.
[upstream.openai]
base_url = "https://api.openai.com"
dialect  = "openai"
headers  = { Authorization = "Bearer ${OPENAI_API_KEY}" }

# Anthropic. Wants x-api-key plus a dated version header, and speaks its own
# dialect on /v1/messages — not /v1/chat/completions.
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

The complete commented file ships as `lmrelay/lmrelay.toml.example` and is what
`lmrelay init` copies.

Notes on the schema:

- `headers` is an arbitrary string-to-string table, sent onward verbatim. There is no
  `api_key` field and no per-provider code path: OpenAI's bearer, Anthropic's two headers
  and Ollama's nothing are all the same mechanism, so adding a provider is four lines of
  TOML.
- `${VAR}` in any header **value** is expanded from the process environment, so keys can
  stay out of the file under Docker or systemd. An unset variable is a startup error that
  names the variable.
- `base_url` is prepended verbatim to the forwarded path, so it is normally a host root and
  never ends in `/v1` — the caller's path already carries that. A path prefix is allowed
  and simply concatenated, which is what makes an endpoint hosted under a subpath work
  without any code.
- `dialect` (`ollama`, `openai` or `anthropic`, default `openai`) never changes what is
  sent. Its only job is the refusal shown above.
- TOML forbids `-` in bare keys, so `"x-api-key"` and `"anthropic-version"` are quoted
  while `Authorization` is not. That is not a typo.
- An upstream may not be named `api` or `v1`; either would shadow the path root every
  Ollama and OpenAI client already sends to, and the breakage would be hard to diagnose.

## Run

```bash
lmrelay serve                       # honours [server].host and [server].port
lmrelay serve --port 8080 --config ./lmrelay.toml
python -m lmrelay serve             # same thing
uvicorn lmrelay.app:app --port 11434
```

lmrelay defaults to port 11434 so that existing Ollama clients need no change, which means
the real Ollama has to move:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

The inverse — relay on 11435, Ollama left on 11434 — is less invasive but leaves an
uncredentialed Ollama listening, which defeats the point.

## Choosing an upstream

The first path segment selects the upstream if and only if it exactly matches a key in
`[upstream]`. Otherwise `default_upstream` handles the request and the path is untouched.

```
POST /api/chat                      -> ollama    , forwards /api/chat
POST /v1/chat/completions           -> ollama    , forwards /v1/chat/completions
POST /openai/v1/chat/completions    -> openai    , forwards /v1/chat/completions
POST /anthropic/v1/messages         -> anthropic , forwards /v1/messages
POST /deepseek/v1/chat/completions  -> deepseek  , forwards /v1/chat/completions
POST /grok/v1/chat/completions      -> grok      , forwards /v1/chat/completions
```

So a client that has never heard of lmrelay keeps working unchanged, and retargeting one is
a single line:

```python
from openai import OpenAI
from anthropic import Anthropic

OpenAI(base_url="http://relay:11434/openai/v1", api_key=RELAY_TOKEN)
OpenAI(base_url="http://relay:11434/v1",        api_key=RELAY_TOKEN)   # local Ollama
Anthropic(base_url="http://relay:11434/anthropic", api_key=RELAY_TOKEN)
```

```bash
curl http://127.0.0.1:11434/api/chat \
  -H "Authorization: Bearer $LMRELAY_TOKEN" \
  -d '{"model": "llama3", "messages": [{"role": "user", "content": "hi"}]}'
```

`GET /healthz` answers `{"status": "ok"}` without touching an upstream and without a
credential. Everything else goes through the relay.

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
- **An unreachable upstream is a 502 that names it**, e.g.
  `lmrelay: upstream 'ollama' at http://127.0.0.1:11435 is unreachable: ConnectError` —
  usually meaning Ollama was never moved off 11434.
- **Binding a non-loopback host with no token logs a warning** rather than refusing, since
  running uncredentialed behind an authenticated nginx is legitimate.

## Not in scope

No failover, retry or load balancing. No dialect translation. No model catalogue or
aliasing. No token accounting, usage database or budgets. No admin API, dashboard or
metrics. No caching or rate limiting. One caller token, not a key ring. No TLS — put nginx
in front. No config hot reload; restart the process.

## Tests

```sh
pip install -e '.[test]'
pytest
```

Most of the suite drives the app in process against a recording upstream, so it needs no
network and no Ollama. `tests/test_streaming.py` is the exception: it runs the relay under
uvicorn in front of an upstream that answers a chunk at a time, because the property it
checks — that the caller has the first line before the upstream has written the last —
cannot be seen through an in-process client.

## License

MIT. See [LICENSE](LICENSE).
