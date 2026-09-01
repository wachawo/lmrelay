## lmrelay - ein zugangsgeschütztes Relay neben einem lokalen Ollama

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/wachawo/lmrelay/branch/main/graph/badge.svg)](https://codecov.io/gh/wachawo/lmrelay?branch=main)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

Wer mit **Ollama** arbeitet, stößt darauf: standardmäßig ist sie nur von localhost aus erreichbar
und bringt keine eingebaute Authentifizierung mit. Ollama von einer anderen Maschine aus
anzusprechen heißt meist, ihre systemd-Konfiguration zu ändern oder einen Reverse Proxy
davorzusetzen. **lmrelay** löst das. Es wird mit `pip` installiert und läuft als Daemon neben
Ollama: es lauscht auf einem eigenen Port und verlangt, wenn gewünscht, Zugangsdaten für den
Zugriff.

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | **[Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md)** | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay leitet Clients an ein lokales Ollama oder an einen gehosteten Anbieter weiter](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### Voraussetzungen

- Python 3.11 oder höher und vier Abhängigkeiten: FastAPI, starlette, uvicorn und httpx.
- Linux und macOS führen jeden Befehl aus, auch `serve` (im Hintergrund) und `enable`: eine
  systemd-`--user`-Unit unter Linux, ein launchd-Agent unter macOS, und eine Absage dort, wo
  keiner von beiden installiert ist.
- Windows führt nur `run` aus. `serve` meldet, dass die Plattform kein `os.fork` hat, und
  `enable`, dass es weder systemd noch launchd gibt, statt halb zu starten.
- Ein lokales Ollama auf 11434 ist der Standard-Upstream, aber nicht erforderlich. Ein Relay,
  das nur gehostete Anbieter konfiguriert hat, ist gültig, solange `default_upstream` einen
  davon benennt.

### Installation

```bash
pip install lmrelay
```

Oder der aktuelle `main`, der der Veröffentlichung voraus sein kann:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### Schnellstart

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama behält 11434, und an seiner Installation bleibt alles genau so, wie es ist.
Stattdessen werden die Clients auf 11435 umgestellt. Das ist der Kompromiss: An einem
bestehenden Ollama muss sich nichts ändern, und das Relay ist pro Client zuschaltbar.

Im frischen State ist die Authentifizierung aus, auf Loopback ist das also ein transparenter
Proxy vor Ollama. Das ist Absicht: Ein gerade installiertes Relay soll den Weg zum eigenen
Ollama nicht versperren, solange noch kein Token existiert. Ein auf 11435 gerichteter Client
funktioniert sofort:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### Prüfen, ob es funktioniert

Frage den Relay nach der Modellliste. Beide Dialekte gehen; beide erreichen dasselbe Ollama:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

Dann lass ein Modell arbeiten. `qwen3:8b` steht hier für das, was `ollama list` auf der eigenen Maschine anzeigt:

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

`qwen3` denkt nach, bevor es antwortet, und nur Ollamas Dialekt hat dafür einen Schalter: das `"think": false` oben. Über `/v1/chat/completions` kommt die Argumentation als `<think>`-Block im Inhalt an, denn lmrelay leitet weiter, was der Upstream erzeugt hat, und bearbeitet es nicht.

Bei eingeschalteter Authentifizierung braucht jede dieser Anfragen die Zugangsdaten:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $TOKEN"
```

### Produktiver Betrieb

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

`enable` registriert unter Linux eine systemd-`--user`-Unit oder unter macOS einen
launchd-Agent und startet sie anschließend. Von da an laufen `stop`, `restart` und `reload`
über diesen Manager statt über die PID-Datei, sodass die beiden nicht uneins darüber werden
können, wem der Prozess gehört. Auf einem POSIX-System ohne beide Manager startet
`lmrelay serve` das Relay im Hintergrund.

### Was ein Aufrufer verlangen darf

```bash
lmrelay limits set total 1              # eine Anfrage gleichzeitig
lmrelay limits set total 1/60s          # eine pro Minute, und weiterhin eine gleichzeitig
lmrelay limits set per_address 2 10/30m # zehn pro halbe Stunde, zwei gleichzeitig
lmrelay limits set per_token 0          # aus
```

Drei Geltungsbereiche, je zwei Zahlen. `concurrent` ist, wie viele Anfragen ein Aufrufer
gleichzeitig offen haben darf, `rate` ist, wie oft er eine starten darf, geschrieben als
`Anzahl/Periode`, und eine Anfrage muss jeden gesetzten Bereich passieren. Eine Rate allein
bringt ihre eigene Obergrenze mit: "eine pro Minute" heißt, ohne weitere Angabe, eine
gleichzeitig.

**Wenn Sie eine einzige Zahl setzen, setzen Sie `total`.** Sie schützt die Maschine: zehn
Aufrufer, jeder innerhalb seines eigenen Limits, treffen trotzdem gemeinsam ein, und eine
Grenze pro Aufrufer kann das nicht sehen. `per_token` daneben verhindert, dass ein Client mit
fünfzig Threads alles belegt.

Ein abgewiesener Aufrufer bekommt ein 429, das den Bereich nennt, und ein `Retry-After`,
sobald das Relay ehrlich eines berechnen kann:

```text
lmrelay: the relay's rate limit is exceeded: 10/30m ([limits.total])
```

Der Befehl schreibt in `lmrelay.toml` und lässt den Rest der Datei unangetastet,
Kommentare eingeschlossen, und signalisiert danach ein laufendes Relay.

### Verwendung

| Befehl | Wirkung |
|---|---|
| `lmrelay init` | `~/.lmrelay/lmrelay.toml` schreiben |
| `lmrelay run` | im Vordergrund laufen |
| `lmrelay serve` | im Hintergrund laufen, hängt an `lmrelay.log` an |
| `lmrelay stop` | das laufende Relay stoppen |
| `lmrelay restart` | stoppen, dann wieder im Hintergrund starten |
| `lmrelay reload` | Konfiguration neu einlesen, ohne eine Verbindung zu verlieren |
| `lmrelay status` | was läuft, wo und mit welchen Upstreams |
| `lmrelay enable` | beim Login starten und jetzt starten |
| `lmrelay disable` | `enable` rückgängig machen |
| `lmrelay auth true\|false` | Zugangsdaten vom Aufrufer verlangen oder nicht |
| `lmrelay token gen [--label L]` | ein Token erzeugen und einmalig ausgeben |
| `lmrelay token add TOKEN [--label L]` | ein selbst gewähltes Token registrieren |
| `lmrelay token list [--show]` | Tokens auflisten, maskiert außer mit `--show` |
| `lmrelay token delete ID` | eines über die von `token list` ausgegebene ID entfernen |
| `lmrelay provider add NAME TOKEN` | einen Upstream hinzufügen oder rotieren |
| `lmrelay provider list [--show]` | alle Upstreams, aus der Datei und aus dem State |
| `lmrelay provider delete NAME` | einen Anbieter entfernen, der dem State gehört |
| `lmrelay limits set SCOPE N[/PERIOD] [N/PERIOD]` | die Limits eines Scopes in die Konfigurationsdatei schreiben |
| `lmrelay export [PATH]` | alles schreiben, was dieses Relay anderswo reproduziert |
| `lmrelay import [PATH]` | Konfiguration und State durch ein Bundle ersetzen |

`run`, `serve` und `restart` nehmen `--host` und `--port`. `provider add` nimmt `--base-url`,
`--dialect` und ein wiederholbares `--header K=V`; bei einem bekannten Namen (`openai`,
`anthropic`, `deepseek`, `grok`, `ollama`) stammen Basis-URL, Dialekt und Header-Form aus
einem Preset, sodass `lmrelay provider add openai sk-...` der ganze Befehl ist.
`export` nimmt `--no-secrets`, es und `import` nehmen `--force`, um über Vorhandenes zu
schreiben, und ganz ohne Pfad geht das Bundle nach stdout und wird von stdin gelesen, sodass
`lmrelay export | ssh other-host lmrelay import` ein Relay in einer Zeile umzieht.
`--config PATH` nimmt jeder Befehl an, der die Konfiguration oder den State liest, also
jeder Befehl außer `init`, das immer `~/.lmrelay/lmrelay.toml` schreibt, und `disable`, das
weder das eine noch das andere liest.

### Auswahl des Upstreams

Das erste Pfadsegment wählt den Upstream genau dann, wenn es exakt einem Schlüssel in
`[upstream]` entspricht. Andernfalls bearbeitet `default_upstream` die Anfrage, und der Pfad
bleibt unangetastet.

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

Ein Client muss den Port also nur einmal lernen, und ihn auf einen anderen Anbieter
umzuhängen ist eine einzige Zeile:

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

`GET /healthz` antwortet mit `{"status": "ok"}`, ohne einen Upstream anzufassen und ohne
Zugangsdaten. `GET /metrics` beantwortet einen Prometheus-Scrape mit aggregierten Zählern und
verlangt sehr wohl welche, denn es sagt, wie das Relay genutzt wird, und nicht nur, dass es
läuft. Alles andere geht durch das Relay.

### Kompatibilität

lmrelay leitet Methode, Pfad, Query-String und Body-Bytes **unverändert** weiter und
übersetzt nicht zwischen API-Dialekten.

| Der Client spricht | Verwendeter Pfad | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama stellt neben seinem nativen `/api/*` eine OpenAI-kompatible Oberfläche unter `/v1/*`
bereit. Das ist die praktisch wichtige Zelle: Ein Client im OpenAI-Format erreicht **alle**
(ollama, openai, deepseek und grok) allein über ein anderes Pfadpräfix.

Die vier Fälle, die nicht funktionieren, und der Grund, warum sich keiner davon zum
Funktionieren bringen lässt, stehen im Konfigurationsdokument.

Wo lmrelay erkennen kann, dass ein Pfad im Upstream sicher nicht existiert, sagt es das
selbst, statt den 404 des Anbieters wie einen Fehler des Aufrufers aussehen zu lassen:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

Jeder Fehler, den lmrelay erzeugt, beginnt mit `lmrelay: `, damit er nie für eine Aussage des
Anbieters gehalten wird.

**[Konfiguration und Fehler](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - die Konfigurationsdatei, Aufrufer-Tokens, Anbieter, Autostart, Streaming-Verhalten und was jeder Fehler bedeutet.

### Tests

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
pytest --cov=lmrelay --cov-report=term-missing
```

`python3 main.py run` startet das Relay direkt aus dem Checkout, ohne Installation; dafür genügt `requirements.txt` allein.

Der größte Teil der Suite treibt die Anwendung im selben Prozess gegen einen aufzeichnenden
Upstream und braucht daher weder Netzwerk noch Ollama.
[`tests/test_streaming.py`](../tests/test_streaming.py) ist die Ausnahme: Dort läuft das Relay
unter uvicorn vor einem Upstream, der Chunk für Chunk antwortet, denn die geprüfte
Eigenschaft, dass der Aufrufer die erste Zeile hat, bevor der Upstream die letzte
geschrieben hat, ist durch einen In-Process-Client nicht zu sehen.

### Warum nicht nginx?

nginx macht bereits Reverse Proxy, also muss sich ein Daemon seinen Platz verdienen. Kurz,
Punkt für Punkt:

- **Provider-Schlüssel landen in `nginx.conf`.** Ein `location` und ein
  `proxy_set_header Authorization "Bearer sk-..."` je Provider, dazu
  `proxy_ssl_server_name on`, wenn der Upstream TLS spricht. Hier ist es ein Befehl, und der
  Schlüssel liegt in einer `0600`-Datei statt in einer `0644`-Datei, die root gehört.
- **Ein Aufrufer-Token in nginx zu prüfen legt die Token ebenfalls in `nginx.conf`.** Ein
  `map` und ein `internal` `location` erledigen das ohne Backend, aber jedes Token wird dann
  zu einer Klartextzeile in derselben root-Datei, und eines hinzuzufügen oder zurückzuziehen
  kostet eine Bearbeitung und ein Reload.
- **`htpasswd` hat weder Ids noch Rotation.** `lmrelay token gen --label laptop`,
  `token list` und `token delete 1` haben beides.
- **nginx' Voreinstellungen brechen das Streaming.** `proxy_buffering` ist an und
  `proxy_read_timeout` steht auf 60s, und ein großes lokales Modell kann länger als eine
  Minute bis zum ersten Token denken. Beide müssen gefunden und abgeschaltet werden, meist
  nachdem eine Antwort schon in der Mitte abgeschnitten wurde.
- **Ein Pfad im falschen Dialekt bekommt durch nginx den 404 des Providers selbst.** Für die
  Formen, die er erkennt, etwa einen Anthropic-Pfad an einen OpenAI-Upstream, antwortet der
  Relay mit 400 in eigenen Worten, sodass der Fehler nicht für den des Providers gehalten
  wird.
- **nginx liegt weder macOS noch Windows bei.** `pip install` funktioniert auf beiden gleich.
- **Ein SDK lässt sich nicht auf dem dokumentierten Weg auf `auth_basic` richten.** Es
  akzeptiert `Basic` und weist alles andere ab, während jedes SDK seinen Schlüssel in
  `Authorization: Bearer` setzt. Zugangsdaten in der URL kommen zwar durch, aber dann ist
  `api_key` totes Gewicht: httpx schreibt die Zugangsdaten der URL in denselben Header, und
  der Bearer verlässt den Prozess nie. Jedes Beispiel aus der Provider-Dokumentation muss
  umgeschrieben werden.

Wo nginx gewinnt: TLS, bereits installiert zu sein, und Rate-Limiting, das über einen
einzelnen Prozess hinaus trägt. Die ersten beiden kommen nicht. lmrelay hat sehr wohl
[Limits](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md#limits) in drei Bereichen: pro Credential, pro Adresse und für das
ganze Relay; das erste hängt am Token des Aufrufers, was nginx nicht kann, ohne die Token
selbst zu halten. Gezählt werden sie in diesem einen Prozess. Die beiden ergänzen sich, statt
zu konkurrieren. Stell nginx für TLS davor, und lass Token, Provider und Limits hier.
### Lizenz

MIT License. Siehe [LICENSE](../LICENSE).
