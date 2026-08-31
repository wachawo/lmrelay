## lmrelay - релей с проверкой учётных данных рядом с локальной Ollama

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

Если вы работаете с **Ollama**, то сталкиваетесь с тем, что по умолчанию она доступна только с
localhost и не имеет встроенной аутентификации. Если нужно подключаться к Ollama с другой машины,
обычно приходится менять systemd-конфигурацию или ставить перед ней reverse proxy. **lmrelay**
решает эту задачу. Он устанавливается через `pip` и запускается как демон рядом с Ollama: слушает
отдельный порт и, при необходимости, требует учётные данные для доступа.

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | **[Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md)** | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay направляет клиентов к локальной Ollama или к облачному провайдеру](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### Требования

- Python 3.11 или новее и четыре зависимости: FastAPI, starlette, uvicorn и httpx.
- Linux и macOS выполняют все команды, включая `serve` (в фоне) и `enable`: юнит systemd
  `--user` в Linux, агент launchd в macOS и отказ там, где не установлено ни то ни другое.
- Windows выполняет только `run`. `serve` сообщает, что на этой платформе нет `os.fork`, а
  `enable` — что нет ни systemd, ни launchd, вместо того чтобы запуститься наполовину.
- Локальная Ollama на 11434 — апстрим по умолчанию, но она не обязательна. Релей, где
  настроены только облачные провайдеры, — допустимая конфигурация, если `default_upstream`
  называет одного из них.

### Установка

```bash
pip install lmrelay
```

Или текущий `main`, который может опережать опубликованную версию:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### Быстрый старт

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama остаётся на 11434, её установку трогать не нужно. Вместо этого на 11435 переводятся
клиенты. В этом и состоит размен: в существующей Ollama менять ничего не приходится, а релей
подключается по выбору, для каждого клиента отдельно.

В свежем состоянии проверка учётных данных выключена, поэтому на loopback это прозрачное
проксирование перед Ollama. Так сделано намеренно: только что установленный релей не должен
отрезать вас от вашей же Ollama раньше, чем у вас появится токен. Направьте клиента на 11435 —
и он работает:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### Проверка работы

Запросите у релея список моделей. Подойдёт любой из двух диалектов — оба ведут к одной и той же Ollama:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

Затем нагрузите модель работой. `qwen3:8b` здесь — это то, что показывает `ollama list` на вашей машине:

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

`qwen3` рассуждает перед ответом, и переключатель для этого есть только в диалекте Ollama: `"think": false` выше. Через `/v1/chat/completions` рассуждение приходит внутри содержимого блоком `<think>`, потому что lmrelay передаёт то, что выдал апстрим, и не правит его.

При включённой авторизации каждый из этих запросов требует учётных данных:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $LMRELAY_TOKEN"
```

### Запуск всерьёз

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

`enable` регистрирует юнит systemd `--user` на Linux или агент launchd на macOS и сразу его
запускает. Дальше `stop`, `restart` и `reload` идут через этот менеджер, а не через pid-файл,
поэтому они не могут разойтись в том, кому принадлежит процесс. На POSIX-машине, где нет ни
того, ни другого менеджера, `lmrelay serve` запускает релей демоном.

### Использование

| Команда | Что делает |
|---|---|
| `lmrelay init` | записать `~/.lmrelay/lmrelay.toml` |
| `lmrelay run` | запустить на переднем плане |
| `lmrelay serve` | запустить в фоне, дописывая в `lmrelay.log` |
| `lmrelay stop` | остановить работающий релей |
| `lmrelay restart` | остановить и снова запустить в фоне |
| `lmrelay reload` | перечитать конфиг, не разрывая соединений |
| `lmrelay status` | что запущено, где и с какими апстримами |
| `lmrelay enable` | запускать при входе в систему и запустить сейчас |
| `lmrelay disable` | отменить `enable` |
| `lmrelay auth true\|false` | требовать учётные данные от клиента или не требовать |
| `lmrelay token gen [--label L]` | выпустить токен и напечатать его один раз |
| `lmrelay token add TOKEN [--label L]` | зарегистрировать токен, выбранный вами самими |
| `lmrelay token list [--show]` | список токенов; без `--show` они замаскированы |
| `lmrelay token delete ID` | удалить токен по id, который печатает `token list` |
| `lmrelay provider add NAME TOKEN` | добавить апстрим или сменить его токен |
| `lmrelay provider list [--show]` | все апстримы — из файла и из состояния |
| `lmrelay provider delete NAME` | удалить провайдера, которым владеет состояние |

`run`, `serve` и `restart` принимают `--host` и `--port`. `provider add` принимает
`--base-url`, `--dialect` и повторяемый `--header K=V`; для известного имени — `openai`,
`anthropic`, `deepseek`, `grok`, `ollama` — базовый URL, диалект и форма заголовка берутся из
пресета, так что `lmrelay provider add openai sk-...` — это вся команда целиком. `--config PATH`
принимает каждая команда, которая читает конфиг или состояние, то есть все команды, кроме
`init`, которая всегда пишет `~/.lmrelay/lmrelay.toml`, и `disable`, которая не читает ни
того ни другого.

### Выбор апстрима

Первый сегмент пути выбирает апстрим тогда и только тогда, когда он в точности совпадает с
ключом в `[upstream]`. Иначе запрос обрабатывает `default_upstream`, а путь остаётся нетронутым.

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

Клиенту достаточно один раз запомнить порт, а перенацелить его на другого провайдера — это одна
строка:

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

`GET /healthz` отвечает `{"status": "ok"}`, не обращаясь к апстриму и не требуя учётных данных.
Всё остальное идёт через релей.

### Совместимость

lmrelay передаёт метод, путь, строку запроса и байты тела **без изменений** и не переводит
между диалектами API.

| Клиент говорит на | Какой путь использует | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama отдаёт OpenAI-совместимую поверхность на `/v1/*` рядом со своей родной `/api/*`. Это
практически самая важная ячейка: клиент, написанный под OpenAI, достаёт до **всех** — ollama,
openai, deepseek и grok — сменой одного только префикса пути.

Четыре случая, которые не работают, и причина, по которой ни один из них заставить работать
нельзя, описаны в документе о конфигурации.

Там, где lmrelay может определить, что на апстриме такого пути заведомо нет, он говорит об этом
сам, а не позволяет чужому 404 выглядеть как ваша ошибка:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

Каждая ошибка, которую порождает сам lmrelay, начинается с `lmrelay: `, поэтому её невозможно
принять за ответ провайдера.

**[Конфигурация и ошибки](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - файл конфигурации, токены клиентов, провайдеры, автозапуск, поведение при стриминге и что означает каждая ошибка.

### Тестирование

```sh
pip install -e '.[test]'
pytest
```

Большая часть набора гоняет приложение внутри процесса против записывающего апстрима, поэтому
ей не нужны ни сеть, ни Ollama. Исключение — [`tests/test_streaming.py`](../tests/test_streaming.py):
он поднимает релей под uvicorn перед апстримом, который отвечает по одному фрагменту за раз,
потому что проверяемое свойство — что вызывающая сторона получает первую строку раньше, чем
апстрим дописал последнюю, — через внутрипроцессный клиент не увидеть.

### Почему не nginx?

nginx и так умеет reverse proxy, поэтому отдельный демон обязан оправдать своё место. Коротко,
по пунктам:

- **Заголовок Authorization уже занят, и именно это решает дело.** Каждый клиент присылает
  `Authorization: Bearer <key>` (SDK OpenAI, curl-примеры выше) или `x-api-key` (SDK
  Anthropic); а `auth_basic` в nginx требует, чтобы тот же самый заголовок нёс
  `Basic <base64>`, и отвергает всё остальное. Один заголовок, два владельца. Учётные данные
  в URL действительно проходят, но httpx записывает их в тот же самый заголовок: клиент с SDK
  OpenAI приходит как `Basic`, заменив собой тот bearer, который собирался отправить.
- **Проверка токена в nginx означает, что токены переезжают в `nginx.conf`.** `map` и
  `internal`-location справляются с этим без всякого бэкенда, но тогда каждый токен — строка
  открытым текстом в файле `0644`, принадлежащем root, а добавить или отозвать один стоит
  правки и reload.
- **Ключи провайдеров оказываются внутри `nginx.conf`.** По `location` и
  `proxy_set_header Authorization "Bearer sk-..."` на каждого, плюс `proxy_ssl_server_name on`,
  если апстрим говорит по TLS. Здесь это одна команда, а ключ лежит в файле с правами `0600`.
- **У `htpasswd` нет ни id, ни ротации.** У `lmrelay token gen --label laptop`,
  `token list` и `token delete 1` — есть.
- **Умолчания nginx ломают стриминг.** `proxy_buffering` включён, а `proxy_read_timeout` — 60
  секунд, тогда как крупная локальная модель может думать дольше минуты до первого токена.
  И то и другое приходится сначала найти и выключить — обычно уже после того, как ответ
  оборвался на половине.
- **Путь в чужом диалекте через nginx получает 404 от самого провайдера.** Для тех форм,
  которые релей распознаёт, — скажем, путь Anthropic, отправленный в апстрим OpenAI, — он
  отвечает 400 своими словами, поэтому ошибку не спутать с ответом провайдера.
- **nginx не поставляется ни с macOS, ни с Windows.** `pip install` одинаково работает на обеих.

В чём nginx выигрывает: TLS, настоящее ограничение частоты запросов и то, что он уже установлен.
У lmrelay нет ни одного из трёх, и не появится. Эти двое не конкурируют, а складываются: nginx
впереди ради TLS, токены и провайдеры — здесь.

### Лицензия

Лицензия MIT. См. [LICENSE](../LICENSE).
