## lmrelay - 本地 Ollama 旁的凭据中继

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/wachawo/lmrelay/branch/main/graph/badge.svg)](https://codecov.io/gh/wachawo/lmrelay?branch=main)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

如果你在用 **Ollama**，就会碰到这一点：默认情况下它只能从 localhost 访问，而且不带任何内置鉴权。要从另一台机器连上 Ollama，通常得改它的 systemd 配置，或者在前面架一个反向代理。**lmrelay** 解决的就是这件事。它用 `pip` 安装，作为守护进程跑在 Ollama 旁边：监听自己的端口，并在你需要时要求提供访问凭据。

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | **[中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md)** | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay 把客户端路由到本地 Ollama 或托管服务商](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### 环境要求

- Python 3.11 或更高版本，以及四个依赖：FastAPI、starlette、uvicorn 和 httpx。
- Linux 和 macOS 支持全部命令，包括 `serve`（后台运行）和 `enable`：在 Linux 上是 systemd
  `--user` 单元，在 macOS 上是 launchd agent；两者都没装的地方则直接拒绝。
- Windows 只支持 `run`。`serve` 会指出该平台没有 `os.fork`，`enable` 会指出既没有 systemd
  也没有 launchd，而不是启动到一半。
- 11434 上的本地 Ollama 是默认上游，但并非必需。只配置了托管服务商的中继同样可用，前提是
  `default_upstream` 指向其中之一。

### 安装

```bash
pip install lmrelay
```

或者当前的 `main`，它可能比已发布的版本更新：

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### 快速开始

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama 保留 11434，它的安装原封不动。改为把客户端指向 11435。这就是这里的取舍：现有的
Ollama 不必做任何改动，中继由各客户端自行决定是否接入。

全新状态下鉴权是关闭的，所以在回环地址上，它就是 Ollama 前面的一个透明代理。这是有意
为之：刚装好的中继不该在你拿到令牌之前，就把你挡在自己的 Ollama 之外。把客户端指向
11435 就能用：

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### 验证是否正常

向中继请求模型列表。两种方言都可以，它们都指向同一个 Ollama：

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

然后让模型干活。这里的 `qwen3:8b` 就是 `ollama list` 在你机器上列出的名字：

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

`qwen3` 会先推理再回答，而只有 Ollama 的方言提供了开关，即上面的 `"think": false`。经由 `/v1/chat/completions` 时，推理会作为 `<think>` 块出现在 content 里，因为 lmrelay 原样转发上游产生的内容，不做修改。

开启鉴权后，上面每一个请求都需要带上凭据：

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $TOKEN"
```

### 正式运行

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
limits       total 10/30m, 2 at once
autostart    systemd: enabled, active
```

`enable` 在 Linux 上注册一个 systemd `--user` 单元，在 macOS 上注册一个 launchd
agent，然后把它启动起来。此后 `stop`、`restart` 和 `reload` 都走该管理器而不是
pidfile，两者因此不会对进程归谁管产生分歧。在两种管理器都没有的 POSIX 机器上，用
`lmrelay serve` 以后台方式运行中继。

### 限制一个调用方能要多少

```bash
lmrelay limits set total 1              # 同时一个请求
lmrelay limits set total 1/60s          # 每分钟一个，而且仍是同时一个
lmrelay limits set per_address 2 10/30m # 每半小时十个，同时两个
lmrelay limits set per_token 0          # 关闭
```

三个作用域，每个两个数字。`concurrent` 是一个调用方同时可以有多少请求在飞行中，`rate` 是它多久可以发起一个，写成 `次数/周期`，而一个请求必须通过你设置的每一个作用域。单独给出的速率会自带一个并发上限：「每分钟一个」在没有另外说明并发的情况下，就是同时一个。

**如果只设一个数字，就设 `total`。** 它才是保护这台机器的那个：十个调用方各自都在自己的限额之内，仍然会同时到达，而按调用方计的上限看不见这一点。旁边的 `per_token` 才是让一个开五十个线程的客户端占不满全部的东西。

被拒绝的调用方收到一个点名作用域的 429，以及一个 `Retry-After`（当中继能诚实地算出来时）：

```text
lmrelay: the relay's rate limit is exceeded: 10/30m ([limits.total])
```

这条命令写入 `lmrelay.toml`，文件其余部分原样不动，注释也在，然后给正在运行的中继发信号。

### 用法

| 命令 | 作用 |
|---|---|
| `lmrelay init` | 写入 `~/.lmrelay/lmrelay.toml` |
| `lmrelay run` | 在前台运行 |
| `lmrelay serve` | 后台运行，日志追加到 `lmrelay.log` |
| `lmrelay stop` | 停止正在运行的中继 |
| `lmrelay restart` | 先停止，再重新以后台方式启动 |
| `lmrelay reload` | 重新读取配置，不断开任何连接 |
| `lmrelay status` | 正在运行什么、在哪里运行、用了哪些上游 |
| `lmrelay enable` | 登录时启动，并立即启动 |
| `lmrelay disable` | 撤销 `enable` |
| `lmrelay auth true\|false` | 要求调用方出示凭据，或不要求 |
| `lmrelay token gen [--label L]` | 生成一个令牌并打印一次 |
| `lmrelay token add TOKEN [--label L]` | 注册一个你自己选定的令牌 |
| `lmrelay token list [--show]` | 列出令牌，除非加 `--show`，否则打码 |
| `lmrelay token delete ID` | 按 `token list` 打印的 id 删除其中一个 |
| `lmrelay provider add NAME TOKEN` | 添加或轮换一个上游 |
| `lmrelay provider list [--show]` | 全部上游，来自配置文件和 state |
| `lmrelay provider delete NAME` | 删除由 state 拥有的服务商 |
| `lmrelay limits set SCOPE N[/PERIOD] [N/PERIOD]` | 把某个作用域的上限写入配置文件 |
| `lmrelay export [PATH]` | 写出在别处重建这个中继所需的一切 |
| `lmrelay import [PATH]` | 用一个打包文件替换配置和 state |

`run`、`serve` 和 `restart` 接受 `--host` 和 `--port`。`provider add` 接受
`--base-url`、`--dialect` 以及可重复的 `--header K=V`；如果名称是已知的那几个（
`openai`、`anthropic`、`deepseek`、`grok`、`ollama`），base URL、方言和请求头形态都来自
预设，所以 `lmrelay provider add openai sk-...` 就是完整的命令。`export` 接受
`--no-secrets`，它和 `import` 都接受 `--force` 以覆盖已经存在的文件；完全不给路径时，打包文件走 stdout 并从 stdin 读入，于是 `lmrelay export | ssh other-host lmrelay import` 一行就把一个中继搬走。
凡是读取配置或 state
的命令都接受 `--config PATH`，也就是除 `init` 和 `disable` 以外的全部命令；`init` 始终写入
`~/.lmrelay/lmrelay.toml`，而 `disable` 两者都不读。

### 选择上游

当且仅当第一个路径段与 `[upstream]` 中的某个键完全一致时，它才用于选择上游。否则由
`default_upstream` 处理该请求，路径原样保留。

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

因此客户端只需记住一次端口，把某个客户端改指到另一个服务商只是一行的事：

```python
from openai import OpenAI
from anthropic import Anthropic

OpenAI(base_url="http://relay:11435/openai/v1", api_key=RELAY_TOKEN)
OpenAI(base_url="http://relay:11435/v1", api_key=RELAY_TOKEN)  # Ollama
Anthropic(base_url="http://relay:11435/anthropic", api_key=RELAY_TOKEN)
```

```bash
curl http://127.0.0.1:11435/api/chat \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
  "model": "llama3",
  "messages": [{"role": "user", "content": "hi"}]
}'
```

`GET /healthz` 返回 `{"status": "ok"}`，既不接触上游，也不需要凭据。`GET /metrics` 以聚合
计数器回应 Prometheus 抓取，它确实需要凭据，因为它说明中继被如何使用，而不只是它还活着。
其余请求一律经由中继转发。

### 兼容性

lmrelay **原样**转发方法、路径、查询字符串和请求体字节，并且不在 API 方言之间做转换。

| 客户端使用的 API | 它使用的路径 | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`、`/api/generate`、`/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`、`/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama 在原生的 `/api/*` 之外，还在 `/v1/*` 上提供一套 OpenAI 兼容接口。这是实际中
最要紧的一格：一个 OpenAI 形态的客户端只要改路径前缀，就能访问 ollama、openai、
deepseek 和 grok **全部**四个上游。

四种行不通的情况，以及每一种为什么无法做通，都在配置文档里。

在 lmrelay 能确定某个路径在上游根本不存在的地方，它会自己讲明，而不是让服务商的 404
看起来像是你的错：

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

lmrelay 产生的每条错误都以 `lmrelay: ` 开头，所以绝不会被误当成服务商说的话。

**[配置与错误](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - 配置文件、调用方令牌、服务商、开机自启、流式行为，以及每条错误的含义。

### 测试

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
pytest --cov=lmrelay --cov-report=term-missing
```

`python3 main.py run` 直接从检出的源码启动 relay，无需安装；这种情况下只需 `requirements.txt`。

测试套件大部分在进程内驱动应用，对接一个记录型上游，因此不需要网络，也不需要 Ollama。
[`tests/test_streaming.py`](../tests/test_streaming.py) 是例外：它把中继跑在 uvicorn
下，前面接一个每次只答一块的上游，因为它要验证的性质，即调用方在上游写出最后一行之前
就已拿到第一行，透过进程内客户端是看不见的。

### 为什么不用 nginx？

nginx 本来就能做反向代理，所以一个守护进程得证明自己值得存在。简要地，逐条来说：

- **服务商密钥最终会写进 `nginx.conf`。** 每家一个 `location` 和一行
  `proxy_set_header Authorization "Bearer sk-..."`，上游走 TLS 时还要加
  `proxy_ssl_server_name on`。在这里只要一条命令，而密钥放在 `0600` 的文件里，
  而不是 root 所有的 `0644` 文件里。
- **在 nginx 里校验调用方的 token，会把 token 也塞进 `nginx.conf`。** 用 `map` 加一个
  `internal` 的 `location` 确实不需要后端，但每个 token 都会变成那个 root 文件里的一行明文，
  新增或吊销都得改文件再 reload。
- **`htpasswd` 没有 id，也没有轮换。** `lmrelay token gen --label laptop`、`token list`
  和 `token delete 1` 有。
- **nginx 的默认值会破坏流式输出。** `proxy_buffering` 是开的，`proxy_read_timeout` 是 60s，
  而一个大的本地模型在吐出第一个 token 之前可能要想上一分钟以上。这两项都得找出来关掉，
  通常是在一次回答被拦腰截断之后。
- **走错方言的路径经由 nginx 拿到的是服务商自己的 404。** 对于它能识别的形态，
  比如把 Anthropic 的路径发给 OpenAI 上游，中继会用自己的话回 400，
  这样这个错误就不会被当成服务商的回应。
- **macOS 和 Windows 都不自带 nginx。** `pip install` 在两者上都一样能用。
- **无法按文档的方式把 SDK 指向 `auth_basic`。** 它只接受 `Basic`，其他一律拒绝，
  而每个 SDK 都把密钥放进 `Authorization: Bearer`。把凭据放进 URL 确实能过，
  但那时 `api_key` 就成了废字段：httpx 会把 URL 里的凭据写进同一个头，bearer 根本发不出去。
  服务商文档里的每个示例都得重写。

nginx 胜出的地方：TLS、它已经装好了，以及在单个进程之外仍然成立的限流。前两样
lmrelay 将来也不会有。但它确有[限制](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md#limits)，分三个范围：按凭据、
按地址，以及整个中继；第一种以调用方的 token 为键，而 nginx 不自己保管 token 就做不到
这一点。这些计数只存在于这一个进程里。两者是搭配关系，而不是竞争关系。把 nginx 放在
前面做 TLS，token、服务商和限制留在这里。
### 许可证

MIT License。见 [LICENSE](../LICENSE)。
