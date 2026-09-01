## lmrelay - un relay con credenziali accanto a un Ollama locale

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

Se lavori con **Ollama**, ci si imbatte in questo: di default è raggiungibile solo da localhost e
non ha alcuna autenticazione integrata. Collegarsi a Ollama da un'altra macchina di solito
significa cambiare la sua configurazione systemd, oppure mettergli davanti un reverse proxy.
**lmrelay** risolve questo. Si installa con `pip` e gira come demone accanto a Ollama: ascolta su
una porta propria e, quando lo si vuole, richiede credenziali per l'accesso.

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | **[Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md)** | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay instrada i client verso un Ollama locale o verso un provider hosted](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### Requisiti

- Python 3.11 o superiore e quattro dipendenze: FastAPI, starlette, uvicorn e httpx.
- Linux e macOS eseguono tutti i comandi, compresi `serve` (in background) ed `enable`: una
  unit systemd `--user` su Linux, un agent launchd su macOS, e un rifiuto dove non è
  installato nessuno dei due.
- Windows esegue solo `run`. `serve` segnala che la piattaforma non ha `os.fork`, ed `enable`
  che non c'è né systemd né launchd, invece di avviarsi a metà.
- Un Ollama locale sulla 11434 è l'upstream predefinito, ma non è obbligatorio. Un relay con
  configurati solo provider hosted è valido, purché `default_upstream` ne nomini uno.

### Installazione

```bash
pip install lmrelay
```

Oppure il `main` attuale, che può essere più avanti della versione pubblicata:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### Avvio rapido

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama mantiene la 11434 e la sua installazione resta esattamente com'è. Sono invece i client
a essere ripuntati sulla 11435. È questo il compromesso: nulla di un Ollama esistente deve
cambiare, e il relay si adotta client per client.

In uno stato appena creato l'autenticazione è disattivata, quindi su loopback questo è un
proxy trasparente davanti a Ollama. È voluto: un relay appena installato non deve precludere
l'accesso al proprio Ollama prima di avere un token. Basta puntare un client sulla 11435 e
funziona:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### Verificare che funzioni

Chiedi al relay l'elenco dei modelli. Va bene uno qualsiasi dei due dialetti; entrambi raggiungono lo stesso Ollama:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

Poi metti al lavoro un modello. Qui `qwen3:8b` è ciò che `ollama list` mostra sulla tua macchina:

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

`qwen3` ragiona prima di rispondere, e solo il dialetto di Ollama ha un interruttore per questo: il `"think": false` qui sopra. Attraverso `/v1/chat/completions` il ragionamento arriva dentro il contenuto come blocco `<think>`, perché lmrelay inoltra ciò che ha prodotto l'upstream e non lo modifica.

Con l'autenticazione attiva, ognuna di queste richieste richiede la credenziale:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $TOKEN"
```

### Metterlo in esercizio

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
limits       total 6 at once
autostart    systemd: enabled, active
```

`enable` registra una unit systemd `--user` su Linux o un agent launchd su macOS, poi la
avvia. Da quel momento `stop`, `restart` e `reload` passano per quel gestore anziché per il
pidfile, così i due non possono discordare su chi possiede il processo. Su una macchina POSIX
priva di entrambi i gestori, `lmrelay serve` esegue il relay in background.

### Utilizzo

| Comando | Cosa fa |
|---|---|
| `lmrelay init` | scrive `~/.lmrelay/lmrelay.toml` |
| `lmrelay run` | esegue in primo piano |
| `lmrelay serve` | esegue in background, accodando a `lmrelay.log` |
| `lmrelay stop` | arresta il relay in esecuzione |
| `lmrelay restart` | lo arresta, poi lo riavvia in background |
| `lmrelay reload` | rilegge la configurazione senza far cadere una connessione |
| `lmrelay status` | cosa è in esecuzione, dove, con quali upstream |
| `lmrelay enable` | avvia al login, e avvia subito |
| `lmrelay disable` | annulla `enable` |
| `lmrelay auth true\|false` | richiede una credenziale al chiamante, oppure no |
| `lmrelay token gen [--label L]` | genera un token e lo stampa una sola volta |
| `lmrelay token add TOKEN [--label L]` | registra un token scelto dall'utente |
| `lmrelay token list [--show]` | elenca i token, mascherati salvo `--show` |
| `lmrelay token delete ID` | ne rimuove uno tramite l'id stampato da `token list` |
| `lmrelay provider add NAME TOKEN` | aggiunge o ruota un upstream |
| `lmrelay provider list [--show]` | tutti gli upstream, dal file e dallo stato |
| `lmrelay provider delete NAME` | rimuove un provider di proprietà dello stato |
| `lmrelay limits set SCOPE [--rate R] [--burst B] [--concurrent N]` | scrive i limiti di un ambito nel file di configurazione |
| `lmrelay config export PATH` | scrive tutto ciò che serve a riprodurre questo relay |
| `lmrelay config import PATH` | sostituisce configurazione e stato con un pacchetto |

`run`, `serve` e `restart` accettano `--host` e `--port`. `provider add` accetta `--base-url`,
`--dialect` e un `--header K=V` ripetibile; con un nome noto (`openai`, `anthropic`,
`deepseek`, `grok`, `ollama`) l'URL di base, il dialetto e la forma degli header arrivano da
un preset, quindi `lmrelay provider add openai sk-...` è l'intero comando. `config export`
accetta `--no-secrets`, entrambi i verbi `config` accettano `--force` per scrivere sopra ciò
che c'è già, ed entrambi accettano `-` al posto di un percorso, per usare il terminale. `--config PATH` è
accettato da ogni comando che legge la configurazione o lo stato, cioè da ogni comando tranne
`init`, che scrive sempre `~/.lmrelay/lmrelay.toml`, e `disable`, che non legge né l'una né
l'altro.

### Scelta dell'upstream

Il primo segmento del percorso seleziona l'upstream se e solo se corrisponde esattamente a una
chiave in `[upstream]`. Altrimenti la richiesta è gestita da `default_upstream` e il percorso
resta intatto.

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

Così un client impara la porta una volta sola, e ridirigerlo verso un provider diverso è
questione di una riga:

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

`GET /healthz` risponde `{"status": "ok"}` senza toccare un upstream e senza credenziale.
`GET /metrics` risponde a uno scrape Prometheus con contatori aggregati e una la richiede,
perché dice come il relay viene usato e non solo che è vivo. Tutto il resto passa per il
relay.

### Compatibilità

lmrelay inoltra metodo, percorso, query string e byte del corpo **invariati**, e non traduce
tra dialetti di API.

| Il client parla | Percorso che usa | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| API Ollama | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| API OpenAI | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| API Anthropic | `/v1/messages` | no | no | no | no | yes |

¹ Ollama espone una superficie compatibile con OpenAI su `/v1/*` accanto alle sue `/api/*`
native. È la cella che conta nella pratica: un client in forma OpenAI raggiunge **tutti**
(ollama, openai, deepseek e grok) cambiando solo il prefisso del percorso.

I quattro casi che non funzionano, e il motivo per cui nessuno di essi può essere fatto
funzionare, stanno nel documento di configurazione.

Dove lmrelay può stabilire che un percorso a monte certamente non esiste, lo dichiara da sé
invece di lasciare che il 404 del provider sembri un errore dell'utente:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

Ogni errore generato da lmrelay inizia con `lmrelay: `, così non viene mai scambiato per
qualcosa detto dal provider.

**[Configurazione ed errori](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - il file di configurazione, i token dei chiamanti, i provider, l'avvio automatico, il comportamento dello streaming e il significato di ogni errore.

### Test

```sh
pip install -e '.[test]'
pytest
```

Gran parte della suite pilota l'applicazione in-process contro un upstream che registra le
richieste, quindi non serve né rete né Ollama. L'eccezione è
[`tests/test_streaming.py`](../tests/test_streaming.py): esegue il relay sotto uvicorn davanti a
un upstream che risponde un chunk alla volta, perché la proprietà che verifica, che il
chiamante abbia la prima riga prima che l'upstream abbia scritto l'ultima, non è osservabile
attraverso un client in-process.

### Perché non nginx?

nginx fa già da reverse proxy, quindi un demone deve guadagnarsi il posto. In breve, punto per
punto:

- **Le chiavi dei provider finiscono dentro `nginx.conf`.** Un `location` e un
  `proxy_set_header Authorization "Bearer sk-..."` per ciascuno, più
  `proxy_ssl_server_name on` quando l'upstream parla TLS. Qui è un solo comando, e la chiave
  sta in un file `0600` invece che in uno `0644` di proprietà di root.
- **Controllare il token di un chiamante in nginx mette anche i token in `nginx.conf`.** Un
  `map` e un `location` `internal` lo fanno senza backend, ma ogni token diventa una riga in
  chiaro in quello stesso file di root, e aggiungerne o revocarne uno richiede una modifica e
  un reload.
- **`htpasswd` non ha id né rotazione.** `lmrelay token gen --label laptop`, `token list` e
  `token delete 1` li hanno.
- **I default di nginx rompono lo streaming.** `proxy_buffering` è attivo e
  `proxy_read_timeout` è 60s, e un modello locale grande può pensare per più di un minuto
  prima del primo token. Vanno trovati entrambi e disattivati, di solito dopo che una risposta
  è stata tagliata a metà.
- **Un percorso del dialetto sbagliato riceve il 404 del provider stesso attraverso nginx.**
  Per le forme che riconosce, come un percorso Anthropic mandato a un upstream OpenAI, il
  relay risponde 400 con parole sue, così l'errore non viene scambiato per quello del
  provider.
- **nginx non arriva né con macOS né con Windows.** `pip install` funziona allo stesso modo su
  entrambi.
- **Un SDK non si può puntare su `auth_basic` nel modo documentato.** Accetta `Basic` e
  rifiuta tutto il resto, mentre ogni SDK mette la sua chiave in `Authorization: Bearer`. Le
  credenziali nell'URL passano, ma allora `api_key` è peso morto: httpx scrive le credenziali
  dell'URL in quella stessa intestazione e il bearer non esce mai. Ogni esempio della
  documentazione del provider va riscritto.

Dove nginx vince: TLS, essere già installato, e un rate limiting che regge fuori da un solo
processo. Le prime due non arriveranno. lmrelay ha invece
[limiti](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md#limits) su tre ambiti: per credenziale, per indirizzo e per l'intero
relay; il primo è legato al token di chi chiama, cosa che nginx non può fare senza custodire
lui stesso i token. Sono contati in questo unico processo. I due si compongono invece di
competere. Metti nginx davanti per il TLS, e lascia qui token, provider e limiti.
### Licenza

Licenza MIT. Vedi [LICENSE](../LICENSE).
