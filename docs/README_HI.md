## lmrelay - लोकल Ollama के बगल में एक credential-आधारित relay

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

अगर आप **Ollama** के साथ काम करते हैं, तो इससे सामना होता है: डिफ़ॉल्ट रूप से वह सिर्फ़ localhost
से ही पहुँच में है, और उसमें कोई built-in authentication नहीं है। किसी दूसरी मशीन से Ollama से
जुड़ना हो, तो आमतौर पर उसकी systemd कॉन्फ़िगरेशन बदलनी पड़ती है, या उसके आगे एक reverse proxy
लगाना पड़ता है। **lmrelay** यही हल करता है। यह `pip` से इंस्टॉल होता है और Ollama के बगल में
daemon की तरह चलता है: अपने अलग port पर सुनता है और, ज़रूरत होने पर, पहुँच के लिए credential
माँगता है।

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | [Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md) | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | **[हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md)** | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay क्लाइंट को लोकल Ollama या hosted provider तक पहुँचाता है](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### आवश्यकताएँ

- Python 3.11 या उससे ऊपर, और चार dependencies: FastAPI, starlette, uvicorn और httpx।
- Linux और macOS पर हर command चलती है, `serve` (detached) और `enable` समेत। `enable` Linux पर
  systemd `--user` unit और macOS पर launchd agent बनाती है, और जहाँ इनमें से कोई भी इंस्टॉल
  नहीं है वहाँ मना कर देती है।
- Windows पर सिर्फ़ `run` चलती है। आधा-अधूरा शुरू होने के बजाय `serve` बताती है कि इस platform पर
  `os.fork` नहीं है, और `enable` बताती है कि न systemd है न launchd।
- 11434 पर चलता लोकल Ollama default upstream है, पर ज़रूरी नहीं। सिर्फ़ hosted providers के साथ
  configure किया गया relay भी वैध है, बशर्ते `default_upstream` उन्हीं में से किसी एक को नाम दे।

### इंस्टॉलेशन

```bash
pip install lmrelay
```

या मौजूदा `main`, जो प्रकाशित रिलीज़ से आगे हो सकती है:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### त्वरित शुरुआत

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

Ollama 11434 पर बना रहता है और उसकी installation जस की तस छोड़ दी जाती है। इसके बजाय clients को
11435 पर मोड़ा जाता है। सौदा यही है: मौजूदा Ollama में कुछ भी बदलने की ज़रूरत नहीं, और relay हर
client के लिए अलग से चुना जाता है।

नए state में auth बंद रहता है, इसलिए loopback पर यह Ollama के आगे एक transparent proxy है। यह
जान-बूझकर है: अभी-अभी इंस्टॉल किया गया relay आपको token मिलने से पहले आपके अपने Ollama से बाहर
नहीं कर देना चाहिए। किसी client को 11435 पर मोड़िए और वह चल जाता है:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### जाँचें कि यह काम करता है

relay से models की सूची माँगें। दोनों में से कोई भी dialect चलेगा; दोनों एक ही Ollama तक पहुँचते हैं:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

फिर किसी model से काम लें। यहाँ `qwen3:8b` वही है जो आपकी मशीन पर `ollama list` दिखाता है:

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

`qwen3` उत्तर देने से पहले तर्क करता है, और इसका switch केवल Ollama के dialect में है: ऊपर वाला `"think": false`। `/v1/chat/completions` से तर्क content के भीतर `<think>` ब्लॉक के रूप में आता है, क्योंकि lmrelay जो upstream ने बनाया उसे ज्यों का त्यों भेजता है और उसमें बदलाव नहीं करता।

auth चालू होने पर इनमें से हर अनुरोध को credential चाहिए:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $LMRELAY_TOKEN"
```

### असल में चलाना

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

`enable` Linux पर systemd `--user` unit या macOS पर launchd agent रजिस्टर करती है, फिर उसे शुरू
करती है। उसके बाद `stop`, `restart` और `reload` pidfile के बजाय उसी manager से होकर जाती हैं,
इसलिए दोनों के बीच इस बात पर मतभेद हो ही नहीं सकता कि process किसका है। जिस POSIX मशीन पर इनमें
से कोई manager नहीं है, वहाँ `lmrelay serve` relay को detached चलाती है।

### उपयोग

| कमांड | क्या करती है |
|---|---|
| `lmrelay init` | `~/.lmrelay/lmrelay.toml` लिखती है |
| `lmrelay run` | foreground में चलाती है |
| `lmrelay serve` | detached चलाती है, `lmrelay.log` में जोड़ते हुए |
| `lmrelay stop` | चल रहे relay को रोकती है |
| `lmrelay restart` | उसे रोकती है, फिर दोबारा detached शुरू करती है |
| `lmrelay reload` | कोई connection गिराए बिना config दोबारा पढ़ती है |
| `lmrelay status` | क्या चल रहा है, कहाँ, किन upstreams के साथ |
| `lmrelay enable` | login पर शुरू, और अभी शुरू |
| `lmrelay disable` | `enable` को पलटती है |
| `lmrelay auth true\|false` | caller credential माँगे, या न माँगे |
| `lmrelay token gen [--label L]` | token बनाकर उसे एक बार छापती है |
| `lmrelay token add TOKEN [--label L]` | आपका ख़ुद चुना हुआ token रजिस्टर करती है |
| `lmrelay token list [--show]` | tokens की सूची, `--show` के बिना masked |
| `lmrelay token delete ID` | `token list` जो id छापती है, उससे एक हटाती है |
| `lmrelay provider add NAME TOKEN` | upstream जोड़ती है या rotate करती है |
| `lmrelay provider list [--show]` | हर upstream, file से और state से |
| `lmrelay provider delete NAME` | state के अधिकार वाला provider हटाती है |

`run`, `serve` और `restart` `--host` और `--port` लेती हैं। `provider add` `--base-url`,
`--dialect` और दोहराया जा सकने वाला `--header K=V` लेती है; जाने-पहचाने नाम के साथ — `openai`,
`anthropic`, `deepseek`, `grok`, `ollama` — base URL, dialect और header का ढाँचा preset से आता
है, इसलिए पूरी command बस `lmrelay provider add openai sk-...` है। `--config PATH` उन सभी
commands में चलता है जो config या state पढ़ती हैं — यानी `init` और `disable` को छोड़कर हर command
में। `init` हमेशा `~/.lmrelay/lmrelay.toml` ही लिखती है, और `disable` इनमें से कुछ नहीं पढ़ती।

### upstream चुनना

path का पहला segment upstream तभी चुनता है जब वह `[upstream]` की किसी key से हूबहू मेल खाए। वरना
request `default_upstream` संभालता है और path अछूता रहता है।

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

इसलिए client को port सिर्फ़ एक बार सीखना पड़ता है, और उसे किसी दूसरे provider पर मोड़ना एक line का
काम है:

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

`GET /healthz` किसी upstream को छुए बिना और बिना credential के `{"status": "ok"}` लौटाता है।
बाक़ी सब कुछ relay से होकर जाता है।

### संगतता

lmrelay method, path, query string और body bytes को **बिना बदले** आगे भेजता है, और API dialects
के बीच अनुवाद नहीं करता।

| आपका client जो बोलता है | जो path वह इस्तेमाल करता है | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ Ollama अपने native `/api/*` के साथ-साथ `/v1/*` पर एक OpenAI-compatible surface भी देता है।
व्यवहार में यही सबसे अहम खाना है: OpenAI-आकार का client सिर्फ़ path prefix बदलकर ollama, openai,
deepseek और grok — इन **सब** तक पहुँच जाता है।

जो चार मामले काम नहीं करते, और हर एक को काम करने लायक क्यों नहीं बनाया जा सकता, वे configuration
दस्तावेज़ में हैं।

जहाँ lmrelay पक्के तौर पर जान सकता है कि upstream पर वह path है ही नहीं, वहाँ वह ख़ुद यह कह देता
है, बजाय इसके कि provider का 404 आपकी ग़लती जैसा दिखे:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

lmrelay जो भी error बनाता है वह `lmrelay: ` से शुरू होती है, इसलिए उसे कभी provider की कही बात
नहीं समझा जाता।

**[कॉन्फ़िगरेशन और errors](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - कॉन्फ़िग फ़ाइल, caller के tokens, providers, autostart, streaming व्यवहार, और हर error का मतलब क्या है।

### परीक्षण

```sh
pip install -e '.[test]'
pytest
```

suite का ज़्यादातर हिस्सा app को उसी process में एक recording upstream के सामने चलाता है, इसलिए
उसे न network चाहिए न Ollama। [`tests/test_streaming.py`](../tests/test_streaming.py) अपवाद है: वह
relay को uvicorn के नीचे ऐसे upstream के आगे चलाता है जो एक बार में एक chunk जवाब देता है, क्योंकि
जो गुण वह जाँचता है — कि upstream के आख़िरी line लिखने से पहले caller के पास पहली line आ चुकी हो —
वह in-process client से दिखता ही नहीं।

### nginx क्यों नहीं?

nginx पहले से ही reverse proxy का काम करता है, इसलिए किसी daemon को अपनी जगह कमानी पड़ती है।
संक्षेप में, बिंदुवार:

- **Authorization header पहले से ही किसी और के पास है, और फ़ैसला यही करता है।** हर client
  `Authorization: Bearer <key>` भेजता है (OpenAI SDK, ऊपर वाले curl उदाहरण) या `x-api-key`
  (Anthropic SDK); nginx के `auth_basic` को उसी header में `Basic <base64>` चाहिए, और बाक़ी सब
  वह ठुकरा देता है। एक header, दो दावेदार। URL में रखे credential निकल तो जाते हैं, पर httpx उन्हें
  उसी header में लिख देता है: तब OpenAI SDK वाला caller `Basic` बनकर पहुँचता है, और जो bearer वह
  भेजना चाहता था, उसकी जगह ले ली जाती है।
- **nginx में token जाँचने का मतलब है token को `nginx.conf` में रखना।** एक `map` और एक
  `internal` location यह बिना किसी backend के कर देते हैं, पर तब हर token root के मालिकाने वाली
  `0644` फ़ाइल में एक सादा-पाठ पंक्ति होता है, और एक token जोड़ने या रद्द करने में एक बदलाव और एक
  reload लगता है।
- **provider की keys `nginx.conf` के भीतर आ बैठती हैं।** हर एक के लिए एक `location` और एक
  `proxy_set_header Authorization "Bearer sk-..."`, और upstream TLS बोलता हो तो साथ में
  `proxy_ssl_server_name on`। यहाँ यह एक command है, और key `0600` फ़ाइल में रहती है।
- **`htpasswd` में न ids हैं, न rotation।** `lmrelay token gen --label laptop`,
  `token list` और `token delete 1` में हैं।
- **nginx के defaults streaming तोड़ देते हैं।** `proxy_buffering` चालू है और
  `proxy_read_timeout` 60s, जबकि बड़ा लोकल model अपना पहला token देने से पहले एक मिनट से ज़्यादा
  सोच सकता है। दोनों को ढूँढकर बंद करना पड़ता है, और आमतौर पर तब, जब कोई जवाब बीच से कट चुका
  होता है।
- **ग़लत dialect वाले path पर nginx से होकर provider का अपना 404 आता है।** जिन रूपों को relay
  पहचानता है — जैसे OpenAI upstream को भेजा गया Anthropic path — उनके लिए वह अपने शब्दों में 400
  लौटाता है, ताकि वह ग़लती provider की कही बात न समझी जाए।
- **nginx न macOS के साथ आता है, न Windows के साथ।** `pip install` दोनों पर एक जैसा चलता है।

nginx जहाँ जीतता है: TLS, असली rate limiting, और पहले से इंस्टॉल होना। lmrelay के पास इन तीनों
में से एक भी नहीं है, और होगा भी नहीं। ये दोनों मुक़ाबला नहीं करते, साथ बैठते हैं — TLS के लिए
आगे nginx, tokens और providers यहाँ।

### लाइसेंस

MIT License. देखें [LICENSE](../LICENSE)।
