## lmrelay - ローカルの Ollama の隣に置く、資格情報つきリレー

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/wachawo/lmrelay/branch/main/graph/badge.svg)](https://codecov.io/gh/wachawo/lmrelay?branch=main)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

**Ollama** を使っていると、これに突き当たる。既定では localhost からしか到達できず、認証の仕組みを持たない。別のマシンから Ollama につなぐには、たいてい systemd の設定を変えるか、前段にリバースプロキシを置くことになる。**lmrelay** はそこを解決する。`pip` で入り、Ollama の隣でデーモンとして動く。自分のポートを待ち受け、必要であればアクセスに資格情報を要求する。

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | **[日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md)** | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay はクライアントをローカルの Ollama かホスト型プロバイダへ振り分ける](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### 要件

- Python 3.11 以上と、4 つの依存パッケージ: FastAPI、starlette、uvicorn、httpx。
- Linux と macOS はすべてのコマンドを実行できる。`serve`（デタッチ実行）と `enable` も含む。
  `enable` は Linux では systemd の `--user` ユニット、macOS では launchd エージェントで、
  どちらも入っていない環境では拒否する。
- Windows で動くのは `run` だけ。中途半端に起動せず、`serve` はこのプラットフォームに
  `os.fork` がないと報告し、`enable` は systemd も launchd もないと報告する。
- 11434 のローカル Ollama が既定のアップストリームだが、必須ではない。ホスト型プロバイダだけを
  設定したリレーも、`default_upstream` がそのいずれかを指していれば成立する。

### インストール

```bash
pip install lmrelay
```

あるいは現在の `main`。公開済みバージョンより先に進んでいることがある:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### クイックスタート

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama は 11434 を持ったままで、そのインストール状態にはまったく手を触れない。代わりにクライアント
の向き先を 11435 に変える。これが引き換えだ。既存の Ollama について何も変える必要がなく、リレーは
クライアントごとに任意で使える。

新しい state では認証はオフなので、ループバック上では Ollama の前に置いた透過プロキシになる。これ
は意図的だ。入れたばかりのリレーが、トークンを手にする前に自分の Ollama から締め出してはならない。
クライアントを 11435 に向ければ、それで動く:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### 動作確認

リレーにモデル一覧を要求する。どちらの方言でもよく、どちらも同じ Ollama に届く:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

次にモデルを動かす。ここでの `qwen3:8b` は、手元で `ollama list` が表示する名前に置き換える:

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

`qwen3` は回答の前に推論する。その切り替えがあるのは Ollama の方言だけで、上の `"think": false` がそれにあたる。`/v1/chat/completions` 経由では、推論は `<think>` ブロックとして content の中に届く。lmrelay は上流が生成したものをそのまま転送し、手を加えないからである。

認証を有効にすると、これらはいずれも資格情報を必要とする:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $TOKEN"
```

### 実運用で動かす

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

`enable` は Linux では systemd の `--user` ユニットを、macOS では launchd エージェントを登録し、
そのまま起動する。以後 `stop`、`restart`、`reload` は pidfile ではなくそのマネージャを経由するので、
プロセスの持ち主について両者の言い分が食い違うことはない。どちらのマネージャもない POSIX 環境では、
`lmrelay serve` がリレーをデタッチして動かす。

### 呼び出し側が要求できる量を絞る

```bash
lmrelay limits set total 1              # 同時に 1 リクエスト
lmrelay limits set total 1/60s          # 毎分 1 件、しかも同時には 1 件のまま
lmrelay limits set per_address 2 10/30m # 30 分あたり 10 件、同時には 2 件
lmrelay limits set per_token 0          # オフ
```

スコープは 3 つ、それぞれに数字がふたつ。`concurrent` は呼び出し側が同時に抱えられるリクエスト数、`rate` はどれくらいの頻度で 1 件開始できるかで、`件数/期間` と書く。リクエストは設定したすべてのスコープを通らなければならない。レートだけを渡すと同じ件数の同時上限も付いてくる。同時実行について何も言わずに「毎分 1 件」と言えば、それは同時に 1 件という意味だからだ。

**ひとつだけ設定するなら `total` を設定する。** マシンを守るのはこれで、10 人の呼び出し側がそれぞれ自分の上限の内側にいても同時に到着することはあり、呼び出し側ごとの上限にはそれが見えない。隣に置く `per_token` は、スレッドを 50 本張る 1 クライアントに全部を占有させないためのもの。

拒否された呼び出し側にはスコープ名の入った 429 が返り、リレーが正直に算出できるときは `Retry-After` も付く:

```text
lmrelay: the relay's rate limit is exceeded: 10/30m ([limits.total])
```

このコマンドは `lmrelay.toml` に書き込み、ファイルの残りはコメントごとそのまま残したうえで、動作中のリレーにシグナルを送る。

### 使い方

| コマンド | 動作 |
|---|---|
| `lmrelay init` | `~/.lmrelay/lmrelay.toml` を書く |
| `lmrelay run` | フォアグラウンドで実行する |
| `lmrelay serve` | デタッチして実行し、`lmrelay.log` に追記する |
| `lmrelay stop` | 動作中のリレーを停止する |
| `lmrelay restart` | 停止して、再びデタッチで起動する |
| `lmrelay reload` | 接続を落とさずに設定を読み直す |
| `lmrelay status` | 何が、どこで、どのアップストリームで動いているか |
| `lmrelay enable` | ログイン時に起動し、いま起動する |
| `lmrelay disable` | `enable` を取り消す |
| `lmrelay auth true\|false` | 呼び出し元に資格情報を要求する、またはしない |
| `lmrelay token gen [--label L]` | トークンを発行し、一度だけ表示する |
| `lmrelay token add TOKEN [--label L]` | 自分で選んだトークンを登録する |
| `lmrelay token list [--show]` | トークンを一覧する。`--show` がなければマスクされる |
| `lmrelay token delete ID` | `token list` が表示する id で 1 つ削除する |
| `lmrelay provider add NAME TOKEN` | アップストリームを追加、またはローテーションする |
| `lmrelay provider list [--show]` | ファイルと state の両方から、すべてのアップストリーム |
| `lmrelay provider delete NAME` | state が所有するプロバイダを削除する |
| `lmrelay limits set SCOPE N[/PERIOD] [N/PERIOD]` | あるスコープの上限を設定ファイルに書き込む |
| `lmrelay export [PATH]` | このリレーを再現するのに要るものを全部書き出す |
| `lmrelay import [PATH]` | 設定と state をバンドルで置き換える |

`run`、`serve`、`restart` は `--host` と `--port` を取る。`provider add` は `--base-url`、
`--dialect`、および繰り返し指定できる `--header K=V` を取る。名前が既知のもの（`openai`、
`anthropic`、`deepseek`、`grok`、`ollama`）なら、ベース URL、方言、ヘッダの形はプリセットから
来るので、`lmrelay provider add openai sk-...` だけでコマンドは終わる。`export` は
`--no-secrets` を取り、これと `import` はどちらも、すでにあるものへ書くための `--force` を取る。パスをまったく渡さなければバンドルは stdout へ出て stdin から読まれるので、`lmrelay export | ssh other-host lmrelay import` の一行でリレーを引っ越せる。`--config PATH` は設定
または state を読むすべてのコマンドが受け付ける。つまり `init` と `disable` 以外のすべてだ。
`init` は常に `~/.lmrelay/lmrelay.toml` を書き、`disable` はどちらも読まない。

### アップストリームの選択

パスの最初のセグメントがアップストリームを選ぶのは、それが `[upstream]` のキーと完全に一致する
場合に限る。一致しなければ `default_upstream` がリクエストを処理し、パスには手を触れない。

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

したがってクライアントが覚えるポートは一度きりで、別のプロバイダに向け直すのは 1 行で済む:

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

`GET /healthz` はアップストリームに触れず、資格情報も要求せずに `{"status": "ok"}` を返す。
`GET /metrics` は集計カウンタの Prometheus スクレイプを返し、こちらは資格情報を要求する。
リレーが生きていることだけでなく、どう使われているかを語るからだ。それ以外はすべてリレーを通る。

### 互換性

lmrelay はメソッド、パス、クエリ文字列、ボディのバイト列を**そのまま**転送し、API 方言のあいだで
変換はしない。

| クライアントが話す API | 使うパス | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama はネイティブの `/api/*` と並べて、`/v1/*` に OpenAI 互換の面を出している。実用上重要な
のはこのセルだ。OpenAI 形式のクライアントは、パスの接頭辞を変えるだけで ollama、openai、
deepseek、grok の**すべて**に届く。

動かない 4 つのケースと、それぞれがなぜ動くようにできないのかは、設定ドキュメントにある。

アップストリームにそのパスが確実に存在しないと lmrelay が判断できる場合、lmrelay は自分でそう言う。
プロバイダの 404 があなたの間違いのように見えるのを避けるためだ:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

lmrelay が生成するエラーはすべて `lmrelay: ` で始まるので、プロバイダが返したものと取り違えられる
ことはない。

**[設定とエラー](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - 設定ファイル、呼び出し元トークン、プロバイダ、自動起動、ストリーミングの挙動、そして各エラーの意味。

### テスト

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
pytest --cov=lmrelay --cov-report=term-missing
```

`python3 main.py run` はインストールせずにチェックアウトから直接リレーを起動する。それには `requirements.txt` だけで足りる。

スイートの大半は、記録用のアップストリームを相手にアプリをインプロセスで動かすので、ネットワークも
Ollama も要らない。例外は [`tests/test_streaming.py`](../tests/test_streaming.py) で、チャンクを
1 つずつ返すアップストリームの前でリレーを uvicorn 上で走らせる。確かめたい性質、すなわちアップストリーム
が最後の行を書き終える前に、呼び出し元が最初の行を受け取っていることは、インプロセスの
クライアント越しには観測できないからだ。

### なぜ nginx ではないのか

nginx はもともとリバースプロキシができる。だからデーモンは自分の居場所を正当化しなければ
ならない。手短に、項目ごとに:

- **プロバイダの鍵が `nginx.conf` の中に残る。** 各プロバイダに `location` と
  `proxy_set_header Authorization "Bearer sk-..."` が一つずつ、上流が TLS を話すなら
  `proxy_ssl_server_name on` も要る。ここではコマンド一つで済み、鍵は root 所有の `0644`
  ではなく `0600` のファイルに置かれる。
- **呼び出し側のトークンを nginx で検査すると、トークンも `nginx.conf` に入る。** `map` と
  `internal` な `location` を使えばバックエンドなしでできるが、そのとき各トークンは同じ
  root 所有ファイルの平文一行になり、追加も失効も編集と reload を要する。
- **`htpasswd` には id もローテーションもない。** `lmrelay token gen --label laptop`、
  `token list`、`token delete 1` にはある。
- **nginx の既定値がストリーミングを壊す。** `proxy_buffering` は有効で
  `proxy_read_timeout` は 60s だが、大きなローカルモデルは最初のトークンまでに一分以上
  考えることがある。どちらも見つけて切る必要があり、たいていは回答が途中で切れた後になる。
- **方言違いのパスは nginx 経由でプロバイダ自身の 404 を返す。** 認識できる形、たとえば
  Anthropic のパスを OpenAI の上流に送った場合、リレーは自分の言葉で 400 を返すので、
  その誤りがプロバイダの応答と取り違えられない。
- **nginx は macOS にも Windows にも同梱されない。** `pip install` はどちらでも同じに動く。
- **SDK を文書どおりのやり方で `auth_basic` に向けることはできない。** それは `Basic` だけを
  受け取り他を拒むが、どの SDK も鍵を `Authorization: Bearer` に入れる。URL に資格情報を
  入れれば通りはするものの、そのとき `api_key` は死に設定になる。httpx が URL の資格情報を
  同じヘッダに書き込み、bearer は外に出ないからである。プロバイダ自身の文書にある例は
  すべて書き換えることになる。

nginx が勝つところ: TLS、すでに入っていること、そして単一プロセスの外でも成り立つ
レート制限。前の二つは今後も持たない。ただし
[制限](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md#limits) はある。資格情報ごと、アドレスごと、リレー全体という三つの
範囲があり、最初のものは呼び出し元のトークンを鍵にする。nginx がトークン自体を抱え込まずに
行うことはできない。数えているのはこの一つのプロセスの中だけである。両者は競合ではなく
組み合わせるものである。TLS のために nginx を前に置き、トークンとプロバイダと制限は
こちらに残す。
### ライセンス

MIT License。[LICENSE](../LICENSE) を参照。
