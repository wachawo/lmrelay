## lmrelay - um relay com credenciais ao lado de um Ollama local

[![CI](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/wachawo/lmrelay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lmrelay.svg)](https://pypi.org/project/lmrelay/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wachawo/lmrelay/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://github.com/wachawo/lmrelay)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-informational.svg)](https://github.com/wachawo/lmrelay)
[![Dependencies](https://img.shields.io/badge/dependencies-4-brightgreen.svg)](https://github.com/wachawo/lmrelay/blob/main/pyproject.toml)

Se você trabalha com **Ollama**, esbarra nisto: por padrão ela só é acessível a partir do
localhost e não traz autenticação integrada. Conectar-se ao Ollama de outra máquina costuma exigir
mudar a configuração do systemd, ou colocar um reverse proxy à frente. **lmrelay** resolve isso.
Instala-se com `pip` e roda como daemon ao lado do Ollama: escuta em uma porta própria e, quando
você quiser, exige credenciais para o acesso.

[English](https://github.com/wachawo/lmrelay/blob/main/README.md) | [Español](https://github.com/wachawo/lmrelay/blob/main/docs/README_ES.md) | **[Português](https://github.com/wachawo/lmrelay/blob/main/docs/README_PT.md)** | [Français](https://github.com/wachawo/lmrelay/blob/main/docs/README_FR.md) | [Deutsch](https://github.com/wachawo/lmrelay/blob/main/docs/README_DE.md) | [Italiano](https://github.com/wachawo/lmrelay/blob/main/docs/README_IT.md) | [Русский](https://github.com/wachawo/lmrelay/blob/main/docs/README_RU.md) | [中文](https://github.com/wachawo/lmrelay/blob/main/docs/README_ZH.md) | [日本語](https://github.com/wachawo/lmrelay/blob/main/docs/README_JA.md) | [हिन्दी](https://github.com/wachawo/lmrelay/blob/main/docs/README_HI.md) | [한국어](https://github.com/wachawo/lmrelay/blob/main/docs/README_KR.md)

![lmrelay encaminha os clientes para um Ollama local ou para um provedor hospedado](https://raw.githubusercontent.com/wachawo/lmrelay/main/docs/diagram.svg)

### Requisitos

- Python 3.11 ou superior e quatro dependências: FastAPI, starlette, uvicorn e httpx.
- Linux e macOS executam todos os comandos, inclusive `serve` (em segundo plano) e `enable`:
  uma unidade systemd `--user` no Linux, um agente launchd no macOS, e uma recusa onde nenhum
  dos dois está instalado.
- O Windows executa apenas `run`. `serve` informa que a plataforma não tem `os.fork`, e
  `enable` que não há systemd nem launchd, em vez de iniciar pela metade.
- Um Ollama local na 11434 é o upstream padrão, mas não é obrigatório. Um relay configurado
  apenas com provedores hospedados é válido, desde que `default_upstream` nomeie um deles.

### Instalação

```bash
pip install lmrelay
```

Ou a `main` atual, que pode estar à frente da versão publicada:

```bash
pip install git+https://github.com/wachawo/lmrelay.git
```

### Início rápido

```bash
lmrelay init     # writes ~/.lmrelay/lmrelay.toml
lmrelay run      # foreground, port 11435
```

O Ollama fica com a 11434 e sua instalação permanece exatamente como está. Em vez disso, os
clientes são reapontados para a 11435. Essa é a troca: nada em um Ollama existente precisa
mudar, e o relay é opcional cliente a cliente.

A autenticação vem desligada em um estado novo, então em loopback isso é um proxy
transparente na frente do Ollama. Isso é deliberado: um relay que você acabou de instalar não
deve trancar você para fora do seu próprio Ollama antes de você ter um token. Aponte um
cliente para a 11435 e ele funciona:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama list
```

### Verificar que funciona

Peça ao relay a lista de modelos. Qualquer um dos dialetos serve; ambos chegam ao mesmo Ollama:

```bash
curl http://127.0.0.1:11435/api/tags    # Ollama's shape
curl http://127.0.0.1:11435/v1/models   # OpenAI's shape
```

Depois coloque um modelo para trabalhar. Aqui `qwen3:8b` é o que `ollama list` mostrar na sua máquina:

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

`qwen3` raciocina antes de responder, e só o dialeto do Ollama tem um interruptor para isso: o `"think": false` acima. Por `/v1/chat/completions` o raciocínio chega dentro do conteúdo como um bloco `<think>`, porque o lmrelay encaminha o que o upstream produziu e não o edita.

Com a autenticação ligada, todas estas requisições precisam da credencial:

```bash
curl http://127.0.0.1:11435/api/tags \
  -H "Authorization: Bearer $LMRELAY_TOKEN"
```

### Rodando de verdade

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

`enable` registra uma unidade systemd `--user` no Linux ou um agente launchd no macOS e então
a inicia. A partir daí, `stop`, `restart` e `reload` passam por esse gerenciador em vez do
pidfile, de modo que os dois não podem discordar sobre quem é dono do processo. Em uma
máquina POSIX sem nenhum dos dois gerenciadores, `lmrelay serve` executa o relay em segundo
plano.

### Uso

| Comando | Faz |
|---|---|
| `lmrelay init` | escreve `~/.lmrelay/lmrelay.toml` |
| `lmrelay run` | executa em primeiro plano |
| `lmrelay serve` | executa em segundo plano, anexando a `lmrelay.log` |
| `lmrelay stop` | para o relay em execução |
| `lmrelay restart` | para o relay e o inicia de novo em segundo plano |
| `lmrelay reload` | relê a configuração sem derrubar nenhuma conexão |
| `lmrelay status` | o que está rodando, onde e com quais upstreams |
| `lmrelay enable` | inicia no login e inicia agora |
| `lmrelay disable` | desfaz `enable` |
| `lmrelay auth true\|false` | exigir uma credencial de quem chama, ou não |
| `lmrelay token gen [--label L]` | gera um token e o imprime uma única vez |
| `lmrelay token add TOKEN [--label L]` | registra um token escolhido por você |
| `lmrelay token list [--show]` | lista os tokens, mascarados a menos que `--show` |
| `lmrelay token delete ID` | remove um pelo id que `token list` imprime |
| `lmrelay provider add NAME TOKEN` | adiciona ou rotaciona um upstream |
| `lmrelay provider list [--show]` | todos os upstreams, do arquivo e do estado |
| `lmrelay provider delete NAME` | remove um provedor que pertence ao estado |
| `lmrelay config export PATH` | escreve tudo o que é preciso para reproduzir este relay |
| `lmrelay config import PATH` | substitui a configuração e o estado por um pacote |

`run`, `serve` e `restart` aceitam `--host` e `--port`. `provider add` aceita `--base-url`,
`--dialect` e um `--header K=V` repetível; com um nome conhecido (`openai`, `anthropic`,
`deepseek`, `grok`, `ollama`) a URL base, o dialeto e o formato do header vêm de um preset,
então `lmrelay provider add openai sk-...` é o comando inteiro. `config export` aceita
`--no-secrets`, ambos os verbos `config` aceitam `--force` para escrever sobre o que já está
lá, e ambos aceitam `-` no lugar de um caminho para usar o terminal. `--config PATH` é aceito por
todo comando que lê a configuração ou o estado, ou seja, todos menos `init`, que sempre
escreve `~/.lmrelay/lmrelay.toml`, e `disable`, que não lê nenhum dos dois.

Cada chave do arquivo de configuração tem também uma forma de variável de ambiente,
`LMRELAY_` mais o caminho até a chave: `[limits.total] concurrent` é
`LMRELAY_LIMITS_TOTAL_CONCURRENT`. O ambiente ganha do arquivo, e um contêiner que define
assim os seus upstreams não precisa de arquivo de configuração nenhum.

### Escolhendo um upstream

O primeiro segmento do caminho seleciona o upstream se e somente se ele corresponder
exatamente a uma chave em `[upstream]`. Caso contrário, `default_upstream` atende a
requisição e o caminho fica intacto.

```
POST /api/chat                     -> ollama     /api/chat
POST /v1/chat/completions          -> ollama     /v1/chat/completions
POST /openai/v1/chat/completions   -> openai     /v1/chat/completions
POST /anthropic/v1/messages        -> anthropic  /v1/messages
POST /deepseek/v1/chat/completions -> deepseek   /v1/chat/completions
POST /grok/v1/chat/completions     -> grok       /v1/chat/completions
```

Assim, um cliente só precisa aprender a porta uma vez, e reapontá-lo para outro provedor é
uma única linha:

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

`GET /healthz` responde `{"status": "ok"}` sem tocar em nenhum upstream e sem credencial.
Todo o resto passa pelo relay.

### Compatibilidade

O lmrelay encaminha o método, o caminho, a query string e os bytes do corpo **sem alteração**,
e não traduz entre dialetos de API.

| Seu cliente fala | Caminho que ele usa | ollama | openai | deepseek | grok | anthropic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| Ollama API | `/api/chat`, `/api/generate`, `/api/tags` | yes | no | no | no | no |
| OpenAI API | `/v1/chat/completions`, `/v1/models` | yes¹ | yes | yes | yes | no |
| Anthropic API | `/v1/messages` | no | no | no | no | yes |

¹ O Ollama expõe uma superfície compatível com OpenAI em `/v1/*` ao lado do seu `/api/*`
nativo. Esta é a célula que importa na prática: um cliente no formato OpenAI alcança **todos**
(ollama, openai, deepseek e grok) mudando apenas o prefixo do caminho.

Os quatro casos que não funcionam, e a razão pela qual nenhum deles pode ser feito funcionar,
estão no documento de configuração.

Quando o lmrelay consegue determinar que um caminho certamente não existe no upstream, ele
mesmo diz isso, em vez de deixar o 404 do provedor parecer um erro seu:

```text
lmrelay: upstream 'anthropic' speaks the Anthropic API;
'/v1/chat/completions' is an OpenAI-dialect path. lmrelay
forwards requests unchanged and does not translate between
dialects.
```

Todo erro gerado pelo lmrelay começa com `lmrelay: `, então ele nunca é confundido com algo
que o provedor disse.

**[Configuração e Erros](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md)** - o arquivo de configuração, os tokens de quem chama, os provedores, o autostart, o comportamento de streaming e o que cada erro significa.

### Testes

```sh
pip install -e '.[test]'
pytest
```

A maior parte da suíte roda a aplicação no próprio processo contra um upstream que grava as
requisições, então ela não precisa de rede nem de Ollama.
[`tests/test_streaming.py`](../tests/test_streaming.py) é a exceção: ele executa o relay sob
uvicorn na frente de um upstream que responde um chunk por vez, porque a propriedade que ele
verifica, que quem chama já tem a primeira linha antes de o upstream ter escrito a última,
não pode ser observada através de um cliente no mesmo processo.

### Por que não nginx?

O nginx já faz reverse proxy, então um daemon precisa merecer seu lugar. Em resumo, ponto a
ponto:

- **As chaves dos provedores acabam dentro do `nginx.conf`.** Um `location` e um
  `proxy_set_header Authorization "Bearer sk-..."` para cada um, mais
  `proxy_ssl_server_name on` quando o upstream fala TLS. Aqui é um comando só, e a chave fica
  em um arquivo `0600` em vez de em um `0644` pertencente ao root.
- **Verificar o token de um chamador no nginx coloca os tokens no `nginx.conf` também.** Um
  `map` e um `location` `internal` fazem isso sem backend, mas cada token vira uma linha em
  texto puro nesse mesmo arquivo do root, e adicionar ou revogar um exige editar e recarregar.
- **`htpasswd` não tem ids nem rotação.** `lmrelay token gen --label laptop`, `token list` e
  `token delete 1` têm.
- **Os padrões do nginx quebram o streaming.** `proxy_buffering` está ligado e
  `proxy_read_timeout` é 60s, e um modelo local grande pode pensar por mais de um minuto
  antes do primeiro token. Os dois precisam ser encontrados e desligados, normalmente depois
  que uma resposta foi cortada ao meio.
- **Um caminho do dialeto errado recebe o 404 do próprio provedor através do nginx.** Para as
  formas que reconhece, como um caminho da Anthropic enviado a um upstream da OpenAI, o relay
  responde 400 com suas próprias palavras, então o erro não é lido como o do provedor.
- **O nginx não vem nem com macOS nem com Windows.** `pip install` funciona igual nos dois.
- **Um SDK não pode ser apontado para o `auth_basic` do jeito documentado.** Ele aceita
  `Basic` e recusa todo o resto, enquanto cada SDK põe sua chave em `Authorization: Bearer`.
  Credenciais na URL passam, mas aí `api_key` é peso morto: o httpx escreve as credenciais da
  URL nesse mesmo cabeçalho e o bearer nunca sai. Cada exemplo da documentação do provedor
  precisa ser reescrito.

Onde o nginx ganha: TLS, já estar instalado, e um limite de taxa que se sustenta fora de um
único processo. Os dois primeiros não vêm. O lmrelay tem, sim,
[limites](https://github.com/wachawo/lmrelay/blob/main/docs/CONFIGURATION.md#limits) em três escopos: por credencial, por endereço e para o relay
inteiro; o primeiro fica preso ao token de quem chama, o que o nginx não consegue fazer sem
guardar os tokens ele mesmo. São contados neste único processo. Os dois se compõem em vez de
competir. Ponha o nginx na frente para TLS, e deixe aqui os tokens, os provedores e os limites.
### Licença

Licença MIT. Veja [LICENSE](../LICENSE).
