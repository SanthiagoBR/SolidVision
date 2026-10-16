# RFC-035 — Modelos Offline

**Status:** Implementado. A medição de §15 confirmou o critério do roadmap numa instalação simulada que nunca acessou o Hugging Face, e o bit a bit de §7.2 no mesmo processo. Dois segundos ramos declarados antes da medição entraram (§16.1): a conferência completa custou mais que o critério de §6.3, e um registro de verificação abona o arquivo já conferido; e o `safetensors` convertido do tradutor carregou 2,2 s mais rápido a frio, então a árvore não leva mais o pickle (§7.4). A verificação manual com a rede desligada de verdade ficou por fazer (§15)
**Etapa:** 3 do [roadmap sem Docker](ROADMAP-standalone.md#rfc-035--modelos-offline-)
**Depende de:** RFC-023 (os dois modelos, o template, a tradução, o determinismo de [§6.2](rfc-023-adaptador-de-embedding-clip.md#62-determinismo) e o ciclo de vida de [§9](rfc-023-adaptador-de-embedding-clip.md#9-ciclo-de-vida-dos-modelos)), RFC-024 (o hash de conteúdo: uma imagem que não mudou não é reprocessada), RFC-026 §10 (o aquecimento na subida), RFC-033 §5 (o runtime materializado por lockfile), RFC-034 (o layout, o modo, a raiz de dados, a instalação simulada, e a lista de entrada de [§14](rfc-034-configuracao-e-diretorios-do-usuario.md#14-o-que-passa-adiante))
**Bloqueia:** RFC-036 (o launcher sobe dois processos que carregam modelos, e precisa saber o que fazer quando o carregamento falha), o instalador (o tamanho dos pesos é a maior parcela do payload)
**Migration:** não
**Medição:** `experiments/rfc-035-offline-models/measure_offline_models.py`, rodado em 2026-10-14 e 2026-10-15 na máquina do piloto, com os logs ao lado (§15). O piloto que precedeu este documento é `pilot_hub_access_probe.py`, na mesma pasta, rodado em 2026-10-14, com o log ao lado (§2)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os números que §2 cita vêm de duas fontes, e cada um diz qual: os tamanhos de arquivo foram lidos do disco, e o comportamento na rede foi observado pelo piloto. Nenhum dos dois mede o que este RFC propõe: o piloto carrega os modelos como o código **já** carrega. Onde o texto afirma como o `transformers`, o `huggingface_hub`, o `torch` ou o Windows se comportam, a afirmação vem do piloto ou da leitura do código-fonte instalado, e diz o arquivo. Onde nenhum dos dois cobre, diz que é hipótese, e §15 diz como ela vai ser testada.
>
> **O piloto** carrega os dois modelos em quatro cenários, cada um num subprocesso com um `HF_HOME` e um `TEMP` vazios, num diretório temporário: o tradutor pelo nome com o cache vazio; o mesmo de novo, com o cache que o primeiro deixou; o CLIP pelo nome, sobre uma cópia do cache real; e os dois modelos pelo diretório do snapshot. Os carregamentos repetem as quatro chamadas de §2.1, sem importar o app. Toda requisição que passa pelo `httpx`, o cliente do `huggingface_hub`, é registrada, com o método, o caminho, o status, a thread e o tempo. Os bytes que o `hf_xet` transfere em Rust não passam pelo `httpx`, e as requisições de metadados em volta deles passam. A função que pediria ao Hub para abrir um pull request de conversão (§2.2) foi trocada por um dublê que só registra a chamada, para o piloto não ter efeito fora da máquina. O cache real foi conferido antes e depois, arquivo por arquivo, e não mudou. O diretório temporário foi apagado no fim.
>
> **A máquina** é a do piloto do RFC-034: Windows 10 Home (10.0.19045), HDD SATA `ST1000DM010-2EP102`, Python 3.12.9. As bibliotecas lidas são as do virtualenv do projeto: `torch` 2.13.0+cpu, `transformers` 5.15.0, `huggingface-hub` 1.27.0, `safetensors` 0.8.0, `tokenizers` 0.22.2, `sentencepiece` 0.2.2 e `langdetect` 1.0.9.
>
> **Depois da implementação.** Os `TBM` foram medidos na mesma máquina, e os resultados estão em §15. O texto original ficou como estava. Duas decisões de §3 mudaram por um segundo ramo que o próprio texto declarou (a conferência abonada por um registro, §6.3; o tradutor convertido para `safetensors`, §7.4), e o que a medição contrariou e o que a implementação acrescentou estão em §16. As licenças de §4.1 são `mit` (CLIP) e `apache-2.0` (tradutor).

---

## 1. Contexto

O RFC-033 tirou o banco do Docker, e o RFC-034 tirou a configuração do checkout. O que sobra entre o backend e uma máquina sem rede são os dois modelos do RFC-023: o CLIP (`laion/CLIP-ViT-B-32-laion2B-s34B-b79K`), que transforma imagens e consultas em vetores, e o tradutor Marian (`Helsinki-NLP/opus-mt-ROMANCE-en`), que passa as consultas em português para o inglês antes do CLIP.

Os dois são carregados hoje pelo nome no Hugging Face Hub. Na primeira vez, a biblioteca baixa os pesos para `~\.cache\huggingface`. Depois, lê de lá. Uma máquina que nunca teve rede não tem esse cache, e a primeira busca falha. A medição do RFC-034 achou o mesmo assunto pelo outro lado: o runtime dos modelos é o único que ainda escreve fora da raiz de dados ([RFC-034 §17.1](rfc-034-configuracao-e-diretorios-do-usuario.md#171-o-que-a-medição-contrariou)).

Este RFC decide de onde os pesos vêm, como o app prova que são os pesos certos, o que acontece quando não são, e como se garante que o acervo já indexado continua valendo. Não decide como os pesos chegam à máquina do usuário (o instalador, fora do roadmap), nem quem mostra o erro ao usuário (RFC-036).

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| Carregar o CLIP e o tradutor de um diretório local, e não pelo nome no Hub | §4, §5.1 |
| Ligar `HF_HUB_OFFLINE=1` no modo instalado | §5.2, §5.3. Nos dois modos (§2.5) |
| Verificar a integridade dos pesos com checksum, com uma mensagem clara | §6 |
| Garantir que o embedding produzido seja idêntico ao atual | §7 |
| *Critério:* com a rede desligada, indexar e buscar (texto em PT, texto em EN e imagem) funcionam numa máquina que nunca acessou o Hugging Face | §15. A busca por imagem só existe na CLI (§2.5) |
| *Critério:* tamanho dos pesos em disco | §2.1 (lido do cache), §15 (`TBM`, a árvore materializada) |
| *Critério:* embeddings bit a bit iguais aos do carregamento atual, num conjunto fixo de imagens | §7.2, §15 |

## 2. Problema

### 2.1 O que o app carrega, e de onde

Quatro chamadas, todas pelo nome:

| onde | o que carrega |
| --- | --- |
| `clip_embedding_model.py:182` | `CLIPModel.from_pretrained(self._model_name)`, com o nome vindo de `settings.embedding_model` |
| `clip_embedding_model.py:191` | `AutoProcessor.from_pretrained(self._model_name)` |
| `query_translator.py:140` | `AutoModelForSeq2SeqLM.from_pretrained(self._model_name)`, com o nome na constante `TRANSLATION_MODEL` |
| `query_translator.py:143` | `AutoTokenizer.from_pretrained(self._model_name)` |

Nenhuma passa `revision`. O que é carregado é o que o `main` de cada repositório aponta, resolvido pelo cache.

O cache desta máquina tem 18 GB em 16 repositórios de modelo, quase todos deixados pelo bake-off do RFC-023. O app usa dois, e de cada um só um snapshot:

| repositório | revisão (`refs/main`) | arquivos | bytes |
| --- | --- | --- | --- |
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | `1a25a446712ba5ee05982a381eed697ef9b435cf` | `model.safetensors` (605 157 884), `tokenizer.json`, `vocab.json`, `merges.txt`, `config.json`, `tokenizer_config.json`, `special_tokens_map.json`, `preprocessor_config.json` | **608 774 835** |
| `Helsinki-NLP/opus-mt-ROMANCE-en` | `e9ca9975e3972afd80732f08ce01d3a1339f47f8` | `pytorch_model.bin` (312 087 009), `vocab.json`, `source.spm`, `target.spm`, `config.json`, `generation_config.json`, `tokenizer_config.json` | **315 128 818** |

São **923 903 653 bytes (881 MiB)**, quase todos em dois arquivos. O número da árvore que o app vai carregar é `TBM` (§15), e deve sair igual, porque a árvore terá exatamente esses arquivos (§4.2).

Duas coisas do cache valem como registro, e não só como tamanho. A pasta `snapshots\<revisão>\` guarda os arquivos que o `transformers` pediu e que existiam. A pasta `.no_exist\<revisão>\` guarda os que ele pediu e que o Hub respondeu que não existem: para o CLIP, `processor_config.json`, `chat_template.jinja`, `chat_template.json`, `audio_tokenizer_config.json` e `added_tokens.json`; para o tradutor, `model.safetensors`, `model.safetensors.index.json`, `tokenizer.json`, `special_tokens_map.json`, `target_vocab.json`, `added_tokens.json` e `chat_template.jinja`. Juntas, as duas pastas dizem o que um carregamento consulta. É dessa lista que o lockfile de §4.1 sai.

Nesta máquina, a pasta `blobs\` dos dois repositórios está vazia, e os arquivos estão inteiros em `snapshots\`. A estrutura documentada do cache é outra: os arquivos em `blobs\`, e `snapshots\` com links para eles. Copiar um cache de uma máquina para outra copia a estrutura de quem baixou, e não a do Hub (§10).

O terceiro componente de linguagem, o `langdetect`, não baixa nada: os 55 perfis de idioma estão dentro do pacote instalado.

### 2.2 O tradutor carrega um pickle, e baixa o que não carrega

O `main` do tradutor não tem `model.safetensors`. É o que o `.no_exist` registra, e é por isso que o arquivo carregado é o `pytorch_model.bin`, um pickle do `torch`.

O cache tem, porém, um segundo snapshot do mesmo repositório: `refs\refs\pr\6` aponta para `ddfee805aaa57f4bd198f88e8832ba2b012f9ae2`, com um `model.safetensors` de 312 062 580 bytes. Nenhum código do projeto pede essa revisão.

O `transformers` 5.15 pede. Em `modeling_utils.py`, quando o arquivo de pesos resolvido é um `.bin` e o repositório não tem `safetensors`, o carregamento dispara uma thread em segundo plano, `Thread-auto_conversion`, *"para a próxima vez"*. A thread (`safetensors_conversion.py`) consulta a API do Hub, procura um pull request aberto pelo robô `SFconvertbot` e, se não achar, pede a um serviço remoto (`https://safetensors-convert.hf.space`) que abra um. Depois baixa o `model.safetensors` desse pull request para o cache. As condições para a thread rodar são quatro: não estar em modo offline, não ter `DISABLE_SAFETENSORS_CONVERSION` no ambiente, o modelo não ser código remoto e não estar numa subpasta. Nenhuma das quatro impede a thread no carregamento de hoje.

As datas dos arquivos batem com essa leitura. Em 2026-08-20, o dia em que o RFC-023 foi implementado, o `pytorch_model.bin` terminou de ser gravado às 13:28:24,09. O `refs\pr\6` foi criado 1,4 s depois, às 13:28:25,60. O `generation_config.json`, que o carregamento lê depois dos pesos, às 13:28:27,63. E o `model.safetensors` do pull request terminou às 13:28:34,95, em paralelo.

**O piloto reproduziu o cache.** O tradutor carregado pelo nome, com um `HF_HOME` vazio, deixou exatamente a estrutura que esta máquina tem: o snapshot `e9ca9975…` com os mesmos sete arquivos, o snapshot `ddfee805…` com o `model.safetensors` de 312 062 580 bytes, o `refs\pr\6`, as mesmas sete entradas em `.no_exist\` e `blobs\` vazia. O log do `transformers` diz qual arquivo foi carregado (`loading weights file pytorch_model.bin`) e o que a thread fez (`Attempting to create safetensors variant`, `Safetensors PR exists`). Das 38 requisições do carregamento, 6 são da `Thread-auto_conversion`: os dados do modelo, os commits do `main`, a lista de discussões, os commits do `refs/pr/6` e dois `HEAD` nos arquivos desse ramo. O pull request já existe, e por isso o pedido de conversão (o dublê) não foi chamado.

**E a thread roda a cada carregamento com rede**, e não só no primeiro. No segundo cenário, com o `safetensors` do pull request já no cache, ela fez as mesmas 6 requisições de novo, em 1,0 s, em segundo plano.

O roadmap já tinha, portanto, um exemplo do que pede para impedir: 312 MB baixados em silêncio, por uma thread que ninguém pediu, de pesos que o app nunca carrega.

### 2.3 `HF_HUB_OFFLINE` é lido uma vez, na importação

Em `huggingface_hub\constants.py:203`:

```python
HF_HUB_OFFLINE = _is_true(os.environ.get("HF_HUB_OFFLINE") or os.environ.get("TRANSFORMERS_OFFLINE"))
```

É uma constante do módulo, calculada quando o `huggingface_hub` é importado pela primeira vez. A função `is_offline_mode()`, que o `transformers` consulta, só devolve essa constante. **Pôr a variável no ambiente depois da importação não muda nada.** O mesmo vale para `HF_HOME` e `HF_HUB_CACHE`, nas linhas 169 a 193.

Mais três detalhes da mesma leitura:

- Só `1`, `ON`, `YES` e `TRUE` ligam o modo, sem diferenciar maiúsculas. `HF_HUB_OFFLINE=2` desliga.
- `TRANSFORMERS_OFFLINE` é sinônimo. `HF_HUB_OFFLINE=0` e `TRANSFORMERS_OFFLINE=1` ligam o modo.
- Com o modo ligado, o `transformers` força `local_files_only=True` em toda resolução de arquivo (`utils\hub.py:379`) e não dispara a conversão de §2.2.

**O `huggingface_hub` também pede ao Hub uma coisa que não é modelo** (piloto). A primeira requisição de um processo com um `HF_HOME` novo foi `GET /api/agent-harnesses`, e levou 3,3 s no primeiro cenário (o tempo inclui abrir a conexão) e 0,17 s no terceiro. É uma lista de agentes de IA que o `huggingface_hub` 1.27 usa para montar o `User-Agent` de toda requisição ao Hub (`utils\_detect_agent.py`, chamado por `utils\_headers.py:187`). A lista é buscada no máximo uma vez por dia e gravada em `HF_HOME\.agent_harnesses.json`, de 5 778 bytes. Com o modo offline, a busca não acontece (`_fetch_registry` devolve `None` antes de qualquer requisição). É a escrita em `HF_HOME` que §5.4 precisa cobrir, além do cache dos modelos e do `xet\`, onde o `hf_xet` guardou 131 758 bytes no primeiro cenário.

O `torch` faz diferente. O diretório que o RFC-034 achou em `%TEMP%` sai de `cache_dir()` em `torch\_inductor\runtime\cache_dir_utils.py`, que lê `TORCHINDUCTOR_CACHE_DIR` **a cada chamada**, usa `%TEMP%\torchinductor_<usuário>` quando a variável não existe, cria o diretório e escreve o valor de volta no ambiente. O piloto o viu aparecer, vazio, nos quatro cenários, inclusive nos que só carregam o tradutor. Não é coisa do CLIP nem do `job_runner`: é carregar um modelo pelo `transformers`.

A ordem importa, portanto, para o Hugging Face e não para o `torch`. Hoje só três módulos de `backend/app` importam `torch` ou `transformers`, e os três estão em `app/infrastructure/ai/`: `clip_embedding_model.py`, `query_translator.py` e `device.py`. Nenhum módulo do projeto importa o `huggingface_hub` diretamente.

### 2.4 Nada no banco diz qual modelo produziu um vetor

A tabela `images` guarda o vetor e não guarda de onde ele veio. Nenhuma migration e nenhum modelo de persistência tem uma coluna com o nome, a revisão ou uma impressão digital do modelo.

O único guarda é a dimensão: o adaptador recusa um vetor que não tenha `settings.embedding_dimension` posições. Ele não distingue dois modelos de 512 dimensões, e o bake-off do RFC-023 mediu três: `openai/clip-vit-base-patch32`, `openai/clip-vit-base-patch16` e o escolhido. Os três têm 512 dimensões e espaços diferentes.

Depois do RFC-034, `EMBEDDING_MODEL` é uma chave do arquivo do usuário. Escrever ali `openai/clip-vit-base-patch32` faria o próximo processo com rede baixar outro modelo, e os vetores novos entrariam no mesmo índice que os antigos. O RFC-024 não conserta isso sozinho: uma imagem cujo conteúdo não mudou não é reprocessada, e o acervo ficaria com dois espaços misturados, sem erro e sem aviso. A busca continuaria respondendo, com resultados piores, e nada diria por quê.

O mesmo vale, mais devagar, para o `main` do Hub. **Cada carregamento com rede resolve o `main` de novo, mesmo com todos os arquivos no cache** (piloto). Para cada arquivo, um `HEAD` em `resolve/main/<arquivo>` responde com um redirecionamento para `resolve-cache/…/<commit>/<arquivo>`, e é esse commit que decide qual snapshot é lido. Um commit novo no repositório traria pesos novos para o processo seguinte.

As requisições são em sequência, na thread que carrega. Com o cache cheio, o piloto mediu o tempo que cada carregamento passa esperando por elas:

| carregamento, com o cache cheio | requisições na thread que carrega | tempo somado delas |
| --- | --- | --- |
| CLIP (`CLIPModel` e `AutoProcessor`) | 31, uma delas a do registro de §2.3 | 3,2 s, dos quais 0,17 s do registro |
| Tradutor (modelo e tokenizador) | 11, mais 6 da conversão em segundo plano | 1,1 s, mais 1,0 s |

Cada `HEAD` ao Hub levou ~140 ms nesta conexão. O processador do CLIP sozinho pergunta cinco vezes por `processor_config.json` e quatro por `preprocessor_config.json`. **Carregar sem rede, portanto, não é só uma questão de funcionar offline:** com rede, é ~3,2 s a menos em cada subida da API aquecida e de cada `job_runner`, nesta conexão. O ganho de verdade depende da latência de cada máquina até o Hub, e §15 mede o carregamento dos dois jeitos.

### 2.5 Seis coisas que o roadmap presumiu

**"Carregar de um diretório local, e não pelo nome."** Feito ao pé da letra, sobra um setting, `embedding_model`, cujo valor deixa de decidir o que é carregado. Ou ele concorda com o diretório, e é redundante, ou discorda, e mente. O modelo passa a ser parte da versão do código, como a migration que fixa `vector(512)`, e não configuração (§4.4).

**"Ligar `HF_HUB_OFFLINE=1` no modo instalado."** Duas correções. A primeira é que a variável só tem efeito se estiver no ambiente antes da primeira importação do `huggingface_hub` (§2.3), e no modo instalado o RFC-034 fechou o ambiente como fonte de configuração ([§5.2](rfc-034-configuracao-e-diretorios-do-usuario.md#52-instalado-o-ambiente-não-é-fonte)): ninguém de fora pode passá-la ao processo, e é o próprio processo que precisa pô-la lá. A segunda é que ela não é a proteção principal. Um `from_pretrained` com um diretório local não consulta o Hub (`utils\hub.py`, o ramo `os.path.isdir`). O piloto carregou os dois modelos pelo diretório do snapshot, com rede, sem o modo offline e sem `local_files_only`, e o resultado foi **zero requisições**: nenhum `HEAD`, nenhuma thread de conversão, nenhuma busca do registro de agentes, e nenhuma pasta `hub\` criada no `HF_HOME`. A variável é a segunda linha, para qualquer caminho de código que alcance o Hub por outro lado. E, se ela é uma rede de segurança, o desenvolvimento é o lugar de descobrir o que cai nela: o modo offline vale nos dois modos (§5.3).

**"Verificar a integridade com checksum."** O RFC-033 decidiu o contrário para o runtime do PostgreSQL: conferir manifesto e presença a cada subida, e deixar os hashes para o instalador, porque *"123 MB lidos de um HDD a cada subida custariam mais que a subida inteira"* ([RFC-033 §5.2](rfc-033-postgres-embarcado.md#52-onde-o-adaptador-procura)). Aqui são 924 MB. Mas o carregamento lê os pesos inteiros de qualquer forma, e um dos dois arquivos é um pickle: conferir **antes** de carregar é o que garante que só o pickle esperado é desserializado (§6.2). O custo é `TBM`, com critério e segundo ramo declarados (§6.3).

**"Garantir que o embedding seja idêntico ao atual."** O "atual" não é fixo hoje (§2.4). Este RFC o fixa nas duas revisões de §2.1, que são as que o cache desta máquina resolve e as que produziram o acervo de desenvolvimento. E "bit a bit" só tem sentido quando a única coisa que muda é de onde os pesos vêm: no mesmo processo, na mesma máquina, com a mesma configuração de threads. Entre máquinas, nada no `torch` promete os mesmos bits, e o que se mede ali é o tamanho da diferença (§7.3).

**"Buscar por imagem."** Não existe rota HTTP para isso. O caso de uso tem `execute_similar_to()`, e o único chamador é a CLI de busca (`app/cli/search.py:212`). O critério é verificado pela CLI (§15). Uma rota é do frontend, e não deste RFC.

**O que o roadmap não pediu, e a medição tornou necessário.** Dois itens. O primeiro é o `torchinductor` em `%TEMP%`, que o RFC-034 passou para cá (§5.4). O segundo é o aquecimento. O `warm_up_models` tem default `False` desde o RFC-026, para que um `pytest` puro não baixasse 600 MB. Depois do RFC-034, o modo instalado não lê o ambiente, e o app nunca cria o arquivo do usuário. Sem mudança, uma API instalada nunca aquece, e a primeira busca de cada processo paga as duas partidas a frio que o RFC-026 mediu: ~4,95 s do CLIP e ~4,20 s do tradutor (§8).

## 3. Decisão

| decisão | resultado |
| --- | --- |
| De onde vêm os pesos | De `<raiz>\runtime\models\`, ao lado de `runtime\postgres`. Por máquina, só para leitura quando instalado, a mesma árvore nos dois modos (§4.2) |
| O que identifica um modelo | O lockfile `app/infrastructure/ai/models.lock.json`: repositório, revisão, e nome, tamanho e `sha256` de cada arquivo. **É código**, e viaja com o app (§4.1) |
| Como a árvore é feita | `scripts/vendor_models.py` materializa o lockfile, de qualquer origem: o Hub na revisão fixada, ou o cache local. O hash decide, e não a origem (§4.3) |
| `embedding_model` | Sai. `EMBEDDING_MODEL` vira chave aposentada (§4.4) |
| Onde a árvore é procurada | O setting `model_runtime_directory`, com default no layout. Configurável, porque o hash garante que só uma árvore idêntica é aceita (§4.5) |
| Como se carrega | `from_pretrained(<diretório>, local_files_only=True)`, nos quatro lugares de §2.1 (§5.1) |
| O ambiente da biblioteca | `HF_HUB_OFFLINE=1`, `HF_HOME` e `TORCHINDUCTOR_CACHE_DIR` sobrescritos pelo `__init__` do pacote `ai`, antes de qualquer importação do `transformers`. Nos dois modos (§5.2, §5.3) |
| Onde o runtime dos modelos escreve | `<dados>\cache\huggingface` e `<dados>\cache\torchinductor`. Nada fora da raiz de dados (§5.4) |
| O que é conferido | Presença e tamanho de todo arquivo, e o `sha256` de todo arquivo **antes** de cada carregamento (§6.1, §6.2) |
| Se conferir custar caro | O segundo ramo, um registro de verificação por arquivo. Critério declarado em §6.3 |
| Quando falha | Uma `ModelUnavailableError` com uma linha que diz o modelo, o arquivo, o problema e o que fazer (§6.4) |
| Onde a falha aparece | O `job_runner` recusa a subida. A API sobe e responde 503 nas rotas que precisam do modelo. A CLI sai com código 1 (§6.5) |
| O embedding | As revisões de §2.1, e bit a bit iguais ao carregamento pelo nome no mesmo ambiente. Um arquivo de referência no repositório e um teste `slow` mantêm isso (§7) |
| O tradutor | Continua no `pytorch_model.bin` do `main`. O `safetensors` do `refs/pr/6` não entra. Converter é o segundo ramo de §7.4 |
| O aquecimento | O default de `warm_up_models` passa a vir do modo: ligado instalado, desligado em desenvolvimento (§8) |
| O banco | Não muda. Nenhuma coluna de procedência nesta etapa (§10) |

## 4. Os modelos como runtime

### 4.1 O lockfile é código

```json
{
  "clip": {
    "repository": "laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
    "revision": "1a25a446712ba5ee05982a381eed697ef9b435cf",
    "directory": "clip-vit-b-32-laion2b-s34b-b79k",
    "license": "TBM",
    "files": {
      "model.safetensors": {"size": 605157884, "sha256": "…"},
      "config.json": {"size": 4355, "sha256": "…"}
    }
  },
  "translator": {
    "repository": "Helsinki-NLP/opus-mt-ROMANCE-en",
    "revision": "e9ca9975e3972afd80732f08ce01d3a1339f47f8",
    "directory": "opus-mt-romance-en",
    "license": "TBM",
    "files": {"pytorch_model.bin": {"size": 312087009, "sha256": "…"}}
  }
}
```

O exemplo está cortado. O arquivo de verdade lista os oito arquivos do CLIP e os sete do tradutor de §2.1, e nenhum outro.

**Por que no pacote `ai`, e não em `scripts/` como o do RFC-033.** O adaptador do PostgreSQL não confere hashes, e o lockfile dele só é lido por quem materializa o runtime. Aqui o app confere cada arquivo contra o lockfile antes de carregar (§6). O instalado não tem `scripts\` ([RFC-034 §4.1](rfc-034-configuracao-e-diretorios-do-usuario.md#41-um-módulo-sabe-onde-as-coisas-estão)), e por isso o lockfile precisa estar onde o código está. É o mesmo argumento que tira o `embedding_model` do `Settings` (§4.4): o modelo é parte da versão do código. Trocar uma revisão é um commit, revisado, e não uma linha num arquivo de configuração.

**JSON**, porque a biblioteca padrão o lê e o `pyproject.toml` não viaja com a instalação.

**A licença** de cada modelo é lida do model card pelo `--lock` (§4.3) e gravada ao lado. Até aqui, quem baixava os pesos era o usuário, direto do Hub. Com este RFC, o SolidVision passa a distribuí-los, e o instalador precisa das duas licenças (§13). Os valores são `TBM`: este documento não leu os model cards.

### 4.2 A árvore

```text
<raiz>\runtime\models\
    SOLIDVISION-MODELS.json              o sha256 do lockfile que a materializou, a data, os tamanhos
    clip-vit-b-32-laion2b-s34b-b79k\     os 8 arquivos do snapshot 1a25a446…
    opus-mt-romance-en\                  os 7 arquivos do snapshot e9ca9975…
```

Arquivos comuns, sem `blobs\`, sem links, sem `refs\` e sem `.no_exist\`. Cada diretório é o que o `from_pretrained` espera receber, e nada mais. Os nomes são curtos e minúsculos: dentro de `C:\Program Files\SolidVision\runtime\models\`, o caminho mais longo fica perto de 100 caracteres, longe do limite de 260 do Windows.

`runtime\` já está fora do git desde o RFC-033.

### 4.3 `scripts/vendor_models.py`

O equivalente do `vendor_postgres.py` do RFC-033, com as mesmas regras:

- **Sem opção, materializa.** Para cada modelo do lockfile, obtém cada arquivo, confere tamanho e `sha256`, e só então move o diretório inteiro para o lugar, de uma vez. Uma materialização interrompida não deixa uma árvore pela metade com cara de completa. Por último, grava o manifesto.
- **A origem dos bytes não importa, o hash decide.** O script baixa do Hub, na revisão fixada e nunca no `main`, por `hf_hub_download(..., revision=<commit>)`. Com `--from-cache`, aceita os arquivos do cache local do Hugging Face, se o snapshot da revisão estiver lá. Nesta máquina, isso evita baixar 924 MB que já estão no disco.
- **`--lock` escreve o lockfile.** Recebe o repositório e a revisão de cada modelo, lista os arquivos do snapshot e grava tamanho e `sha256` de cada um. Para os dois arquivos grandes, confere o `sha256` contra o que o próprio Hub declara no ponteiro LFS. **Hipótese:** o Hub publica esse valor para os dois repositórios, e ele é o `sha256` do conteúdo. Se não publicar, o lockfile tem uma fonte só.
- **O script importa o `huggingface_hub` diretamente, e nunca o pacote `app.infrastructure.ai`.** É o único processo do projeto que tem motivo para acessar a rede, e o pacote `ai` liga o modo offline ao ser importado (§5.2).

O manifesto não é a fonte da verdade. O app confere os arquivos contra o lockfile do código, e o manifesto só diz qual lockfile materializou a árvore, para a mensagem de erro poder dizer *"a árvore é de outra versão do SolidVision"* em vez de listar quinze arquivos divergentes.

### 4.4 `embedding_model` sai

O campo `embedding_model` sai do `Settings`, e `EMBEDDING_MODEL` entra nas chaves aposentadas do RFC-034 ([§5.4](rfc-034-configuracao-e-diretorios-do-usuario.md#54-chaves-que-não-fazem-o-que-parecem)), com o aviso *"o modelo é o do lockfile, materializado em runtime\models (RFC-035)"*. O `.env.example` perde a linha.

Mantê-lo, validado contra o lockfile, daria um setting com um único valor aceito. O precedente é o do RFC-034 com o `environment`: uma declaração que alguém escreve, com um default que mente quando discorda do que acontece.

`embedding_dimension` continua como está. Um valor errado nele não troca o espaço em silêncio: o adaptador recusa todo vetor, na primeira imagem, com uma mensagem que nomeia o setting. O teste do RFC-023 §10 continua mantendo o default igual ao literal da migration.

Trocar de modelo, quando acontecer (o fine-tuning do [RFC-023 §18](rfc-023-adaptador-de-embedding-clip.md#18-fine-tuning-e-trabalho-futuro)), passa a ser: uma entrada nova no lockfile, uma migration e um plano de reindexação. É um RFC, e não uma chave.

### 4.5 Onde a árvore é procurada

Um setting novo, `model_runtime_directory`, do tipo `Path`, com default `layout.model_runtime`, que é `<raiz>\runtime\models`. Entra no `PATH_DEFAULTS` do RFC-034, e os quatro testes de caminho do RFC-034 passam a cobri-lo sem mudança.

Configurável pela mesma razão do `embedded_postgres_runtime_directory`: um desenvolvedor com vários checkouts pode apontar todos para uma árvore só, em vez de guardar 924 MB por checkout. A diferença é que aqui apontar para outro lugar não troca o conteúdo: a árvore apontada passa pela mesma conferência contra o lockfile (§6). Num arquivo do usuário, a chave só consegue apontar para uma cópia idêntica.

## 5. Carregar sem rede

### 5.1 Três camadas

| camada | o que impede | onde |
| --- | --- | --- |
| Um diretório local no lugar do nome | A resolução pelo Hub nos quatro carregamentos de §2.1, e com ela a conversão de §2.2, que só existe no ramo remoto | `clip_embedding_model.py`, `query_translator.py` |
| `local_files_only=True` em cada `from_pretrained` | Que um arquivo opcional que não está no diretório seja procurado no Hub. Escrito na chamada, para não depender de a variável de ambiente ter chegado a tempo | Os mesmos |
| `HF_HUB_OFFLINE=1` | Qualquer outro caminho do `transformers` ou do `huggingface_hub` que consulte a rede: o `transformers` força `local_files_only` em toda resolução, e o cliente HTTP do `huggingface_hub` recusa as requisições | §5.2 |

A primeira camada basta para os quatro carregamentos de hoje: o piloto a mediu sozinha, com rede, e contou zero requisições (§2.5). As outras duas existem porque uma atualização do `transformers` pode acrescentar uma consulta que ninguém leu, como a de §2.2 foi acrescentada.

Os adaptadores deixam de receber um nome e passam a receber um diretório, já conferido (§6). Quem o resolve é um módulo novo, `app/infrastructure/ai/model_runtime.py`, que lê o lockfile, acha a árvore pelo setting e confere os arquivos. `get_embedding_model()` continua com o `lru_cache` do RFC-023, e o construtor do adaptador continua sem custo: a conferência acontece no primeiro carregamento, e não na construção.

### 5.2 Onde o ambiente é montado

O `__init__.py` do pacote `app.infrastructure.ai` chama `prepare_model_environment(layout)`, de um módulo novo, `environment.py`, antes de qualquer outra importação do pacote. A função **sobrescreve**, sem olhar o valor anterior:

| variável | valor |
| --- | --- |
| `HF_HUB_OFFLINE` | `1` |
| `HF_HOME` | `<dados>\cache\huggingface` |
| `TORCHINDUCTOR_CACHE_DIR` | `<dados>\cache\torchinductor` |

Sobrescreve porque, no modo instalado, o ambiente não é fonte de configuração (RFC-034 §5.2), e uma `HF_HUB_OFFLINE=0` deixada por outro programa não pode religar a rede. Em desenvolvimento, sobrescreve pela mesma regra, para os dois modos se comportarem igual (§5.3).

**Por que no `__init__` do pacote.** O Python executa o `__init__` de um pacote antes de qualquer submódulo dele. Como os únicos importadores de `torch` e `transformers` em `backend/app` estão dentro do pacote (§2.3), nenhum caminho do app chega ao `huggingface_hub` sem passar por ali. Um teste de arquitetura mantém isso: nenhum módulo de `backend/app` fora de `app/infrastructure/ai/` importa `torch`, `transformers`, `huggingface_hub` ou `safetensors` (§9).

**Uma conferência depois da importação.** Na primeira carga de um modelo, `model_runtime.py` confere que `huggingface_hub.is_offline_mode()` é verdadeiro. Se não for, alguém importou a biblioteca antes do pacote, por um caminho que o teste de arquitetura não vê: um `sitecustomize`, uma ferramenta de `dataset_tools`, um teste. O carregamento falha com uma mensagem que diz isso, em vez de seguir com a rede ligada.

**O custo do `__init__`.** Três atribuições em `os.environ`. A função não cria diretório e não toca o disco: o `__init__` roda em toda importação do `FakeEmbeddingModel` da suíte rápida ([RFC-023 §9](rfc-023-adaptador-de-embedding-clip.md#9-ciclo-de-vida-dos-modelos)). Quem cria `<dados>\cache\…` é a própria biblioteca, quando precisa.

**`os.environ` fora de `config/`.** O RFC-004 reserva a leitura do ambiente para `config/`. O precedente para esta exceção é o `discard_libpq_environment()` do RFC-034 ([§17.2](rfc-034-configuracao-e-diretorios-do-usuario.md#172-o-que-o-texto-não-previa)): não é configuração do app, é o ambiente de uma biblioteca, e mora no pacote do adaptador que a usa.

### 5.3 Os dois modos

O roadmap pede o modo offline instalado. Este RFC o liga nos dois, pelas razões de §2.5. Depois desta mudança, nenhum caminho de código do app deveria alcançar o Hub. Se algum alcançar, é melhor que quebre no desenvolvimento, onde alguém lê o traceback, do que divirja só na máquina do usuário.

O que muda para quem desenvolve:

- **Os modelos precisam ser materializados uma vez**: `python scripts/vendor_models.py --from-cache`, ou sem a opção numa máquina sem cache. É o mesmo passo que o `vendor_postgres.py` já exige para o cluster embarcado.
- **Os testes `slow` deixam de baixar.** Sem `runtime\models`, eles são pulados com o comando que materializa a árvore, como os testes `embedded` já fazem sem `runtime\postgres` ([RFC-033 §12](rfc-033-postgres-embarcado.md#12-testes)).
- **O `~\.cache\huggingface` deixa de ser lido pelo app.** Os experimentos continuam usando, em processos próprios, e o `--from-cache` o usa como origem (§4.3).

### 5.4 O que o runtime dos modelos escreve

O RFC-034 deixou duas escritas fora da raiz de dados e as passou para cá: `~\.cache\huggingface`, que o carregamento pelo nome usa, e `%TEMP%\torchinductor_<usuário>`, criado quando o `job_runner` carrega o modelo ([RFC-034 §17.1](rfc-034-configuracao-e-diretorios-do-usuario.md#171-o-que-a-medição-contrariou)).

Com §5.2, as duas vão para `<dados>\cache\`. A árvore do RFC-034 ganha uma pasta:

```text
%LOCALAPPDATA%\SolidVision\
    cache\
        huggingface\     o que o huggingface_hub gravar com o modo offline ligado: TBM, provavelmente nada
        torchinductor\   a pasta que o torch cria, vazia no RFC-034 e nos quatro cenários do piloto
```

`cache\` é o nome certo para as duas porque as duas podem ser apagadas sem perda: nada nelas é fonte de nada.

O "provavelmente nada" tem dois motivos do piloto. Carregando pelo diretório, a biblioteca não criou `hub\` no `HF_HOME`. E a única outra escrita que o piloto viu ali, o `.agent_harnesses.json` de §2.3, só acontece depois de uma busca no Hub, que o modo offline impede. O `HF_HOME` continua apontado para a raiz de dados para o caso de uma versão futura escrever outra coisa.

**Hipótese, a testar em §15:** com as duas variáveis, a carga dos dois modelos não escreve fora da raiz de dados, e o `%TEMP%` do processo fica vazio. O RFC-034 só olhou o primeiro nível de `%USERPROFILE%`, `%APPDATA%` e `%TEMP%`, e a medição deste RFC repete as mesmas comparações, agora carregando os modelos.

## 6. A integridade

### 6.1 O que é conferido, e quando

| quando | o que |
| --- | --- |
| Antes de cada carregamento de um modelo | O manifesto existe e foi feito por este lockfile; cada arquivo do lockfile existe com o tamanho esperado; o `sha256` de cada arquivo confere |
| Na subida de um processo que vai carregar modelos (a API com aquecimento, o `job_runner`, a CLI de busca e indexação) | Só presença e tamanho, dos dois modelos. É barato, e acha uma instalação quebrada na subida, e não na primeira busca em português |

Cada carregamento acontece uma vez por processo e por modelo (RFC-023 §9). A API aquecida carrega os dois e confere 924 MB. O `job_runner` carrega só o CLIP e confere 609 MB.

Arquivos que não estão no lockfile são ignorados, e não recusados. Um `desktop.ini` deixado pelo Explorer não pode derrubar a busca.

### 6.2 Antes, e não depois

O `pytorch_model.bin` é um pickle. Desserializar um pickle pode executar código, e o `torch` restringe isso hoje com `weights_only`. Conferir o `sha256` **antes** do `from_pretrained` faz com que o único pickle que o app desserializa seja o que está no lockfile, independentemente do que a versão instalada do `torch` faça com o resto.

É também a ordem que dá a mensagem certa. Um arquivo truncado conferido depois do carregamento chega primeiro como um traceback do `safetensors` ou do `torch`, que é o que o roadmap pede para não mostrar.

### 6.3 O custo, e o segundo ramo

Ler 924 MB e calcular o `sha256` custa `TBM` neste HDD. O carregamento lê os mesmos bytes logo em seguida, e por isso a pergunta não é quanto custa ler, mas quanto a conferência soma ao carregamento que já existe.

**Declarado antes da medição:** a conferência completa acontece em todo carregamento se somar **no máximo um quarto** ao tempo de carregamento do mesmo modelo, a frio e a quente, neste HDD. A partida a frio do CLIP medida pelo RFC-025 é ~4,95 s, o que dá um orçamento perto de 1,2 s.

**O segundo ramo**, se o custo passar do critério: um registro de verificação em `<dados>\cache\models-verified.json`, com o caminho, o tamanho, a data de modificação e o identificador de arquivo do NTFS de cada arquivo conferido, e o `sha256` do lockfile. O hash completo só é refeito quando um desses muda. Presença e tamanho continuam conferidos sempre. O que o segundo ramo perde, e está declarado: uma corrupção do disco que não muda tamanho nem data passa a ser detectada só na próxima verificação completa. É o caso mais raro dos quatro de §6.4, e o único que esse ramo deixa passar.

### 6.4 A mensagem

`ModelUnavailableError`, em `app/infrastructure/ai/`, com uma linha que diz o modelo, o arquivo, o problema e o que fazer:

| o problema | a linha, em essência |
| --- | --- |
| A árvore não existe | Os modelos não estão em `<diretório>`. Em desenvolvimento: `python scripts/vendor_models.py`. Instalado: reinstale o SolidVision |
| O manifesto é de outro lockfile | A árvore em `<diretório>` é de outra versão do SolidVision |
| Um arquivo falta | Falta `<arquivo>` do modelo `<nome>`. Um antivírus pode tê-lo posto em quarentena |
| Tamanho diferente | `<arquivo>` tem `<n>` bytes, e esta versão espera `<m>`: a cópia está incompleta |
| Hash diferente | `<arquivo>` não é o que esta versão espera. O conteúdo foi alterado ou corrompido |
| O `transformers` falha mesmo assim | `<nome>` não pôde ser carregado de `<diretório>`. O traceback vai para o log, e a linha não o repete |

O caso do antivírus vem nomeado porque é o mais provável dos quatro numa máquina de usuário. **Hipótese:** um antivírus pode tratar um pickle de 312 MB como suspeito. O [RFC-033 §15](rfc-033-postgres-embarcado.md#15-riscos) registrou o mesmo risco para o `postgres.exe`.

O texto da mensagem nunca inclui o conteúdo de arquivo nenhum, só nomes, tamanhos e o prefixo dos hashes.

### 6.5 Onde a falha aparece

| programa | o que faz | por quê |
| --- | --- | --- |
| `job_runner` | Recusa a subida: a linha no `stderr` e código 1, como a `ConfigurationError` do RFC-034 | Sem o CLIP, ele não tem nada a fazer. Ficar de pé pegaria jobs para falhar cada um |
| API | Sobe. O aquecimento registra a falha no log e não derruba o processo. As rotas que precisam do modelo, a busca hoje, respondem **503** com a linha em `detail`. `/health` continua respondendo pelo banco | A API serve dispositivos, pastas, mapa, miniaturas e detalhes sem modelo nenhum. Uma árvore quebrada não deve esconder o acervo |
| CLI (`search`, `indexing_worker`) | A linha no `stderr` e código 1 | É quem tem um terminal |

O 503, e não o 500, porque é o que o RFC-034 já usa para o banco ausente: o serviço está indisponível, e não com defeito. A conversão fica num handler registrado na aplicação, como o do RFC-026 §8.1 para os erros de domínio.

Reiniciar não conserta nenhum desses casos. O que o launcher faz com um filho que sai com essa linha é do RFC-036 (§13).

## 7. O embedding não muda

### 7.1 O que "atual" quer dizer

As duas revisões de §2.1. São as que o `refs\main` do cache desta máquina aponta hoje, as que estão no disco desde 2026-08-20 e, por isso, as que produziram todos os vetores do banco de desenvolvimento. O lockfile as fixa. Depois deste RFC, um commit novo no `main` do Hub não chega mais a processo nenhum.

Para o tradutor, "atual" é o `pytorch_model.bin` do `main`, e não o `safetensors` do `refs/pr/6`. O piloto confirmou pelo log do `transformers` que o `.bin` é o que o carregamento usa, e que o `safetensors` só é baixado (§2.2).

### 7.2 Bit a bit, onde isso tem sentido

O critério do roadmap compara dois carregamentos que diferem numa coisa só: o atual, pelo nome e pelo cache, e o novo, pelo diretório. Os dois no mesmo processo, na mesma máquina, com o mesmo número de threads. Se os bytes dos pesos são os mesmos, os vetores têm de ser os mesmos bits, porque o [RFC-023 §6.2](rfc-023-adaptador-de-embedding-clip.md#62-determinismo) já mediu que o CLIP é determinístico e que a decodificação do tradutor é gulosa.

**O conjunto fixo:**

- As 45 imagens do RFC-022, codificadas uma a uma e em lotes de 8, como o pipeline do RFC-024 faz.
- As 25 consultas de *ground truth* do RFC-022, em português e em inglês. Para as em português, a string traduzida é comparada antes do vetor: uma tradução diferente explicaria um vetor diferente, e o critério precisa dizer qual dos dois mudou.

A comparação é dos bytes dos `float32`, e não de uma tolerância. Uma diferença de um bit é uma falha do critério.

**O arquivo de referência.** A comparação de §15 roda uma vez. Para que o critério continue valendo depois, os vetores do carregamento atual vão para o repositório, em `backend/tests/fixtures/rfc035_reference_embeddings.npz`: 45 imagens e 50 consultas, 512 `float32` cada, ~190 KB. O teste `slow` de §9 compara cada carregamento com eles. Ele pega uma árvore errada, e também o que o lockfile não fixa: uma atualização do `transformers` que mude o pré-processamento, ou do `torch` que mude um kernel.

### 7.3 Entre máquinas

O instalador vai rodar em outros processadores, como o i5-1035G1 com AVX-512 em que o RFC-023 pensou para a otimização. **Hipótese:** o `torch` escolhe kernels diferentes por conjunto de instruções e por número de threads, e a ordem das somas muda os últimos bits. Nada nele promete os mesmos bits entre máquinas, e este RFC não promete.

O que importa para o acervo é o tamanho da diferença. Um vetor que difere na sétima casa decimal está no mesmo espaço. Um vetor de outro modelo não está (§2.4). §15 mede, como informação e não como critério: a mesma máquina com uma thread e com o default; e o carregamento novo contra os vetores já gravados no banco de desenvolvimento, que podem ter vindo de outra versão do `torch`. Nos dois casos, a maior diferença absoluta e o menor cosseno.

O teste do arquivo de referência (§7.2) é exato na máquina que o gerou, e usa uma tolerância nas outras. A tolerância é `TBM`, e sai dessas duas medições.

### 7.4 O tradutor fica no `.bin`

Três opções para os pesos do tradutor:

| opção | o que ganha | o que custa |
| --- | --- | --- |
| **O `pytorch_model.bin` do `main`** (escolhida) | É o que carrega hoje. O critério de §7.2 vale por construção | Um pickle, conferido antes (§6.2). Carregamento possivelmente mais lento que um `safetensors` |
| O `safetensors` do `refs/pr/6` | Sem pickle | É o ramo de um pull request aberto por um robô, e não um commit do `main`. Pode mudar ou sumir. Exige provar que os tensores são os mesmos do `.bin` |
| Converter o `.bin` em `safetensors` no `vendor_models.py` | Sem pickle, com um arquivo produzido por este projeto, a partir do commit fixado | O lockfile passa a ter um arquivo que o Hub não tem, e a conversão precisa ser reproduzível byte a byte |

**O segundo ramo:** §15 mede o carregamento do tradutor pelo `.bin` e por um `safetensors` convertido. Se o convertido carregar pelo menos **1 s** mais rápido a frio, a conversão entra, com os tensores comparados um a um e os vetores de §7.2 conferidos de novo. Abaixo disso, o pickle fica: ele já está atrás do hash.

## 8. O aquecimento instalado

O default de `warm_up_models` passa a vir do modo: `True` instalado, `False` em desenvolvimento. O arquivo do usuário continua podendo mudá-lo nos dois.

O RFC-026 escolheu o `False` para que importar o app nunca baixasse 600 MB num processo que não pediu, e deu como exemplo o `with TestClient(app)` da suíte rápida, que dispara a subida. O download deixa de existir: depois deste RFC, carregar não baixa nada. O exemplo continua valendo por outro motivo: com aquecimento, aquele `TestClient` carregaria 924 MB, conferiria os hashes e falharia sem `runtime\models`. No modo instalado não há suíte, e o `False` deixa a API sem aquecer para sempre, porque o ambiente não é mais fonte e ninguém cria o arquivo do usuário (§2.5).

O aquecimento continua sendo o do RFC-026: uma frase em português, que carrega os dois modelos. Com uma árvore quebrada, ele registra a falha e deixa a API subir (§6.5).

**O custo, declarado:** a subida da API instalada passa a incluir as duas partidas a frio e a conferência. O RFC-026 mediu 8,37 s de subida com aquecimento, contra a meta de 5 s do `ARCHITECTURE.md` §22, e registrou por que a meta não cabe num processo que carrega dois modelos. Este RFC soma a conferência a isso: `TBM`. Quem espera essa subida é o RFC-036, que decide quando a API está pronta.

## 9. Testes

Na suíte padrão, sem rede, sem modelos e sem `runtime\models`:

- **O ambiente:** importar o pacote `ai` deixa `HF_HUB_OFFLINE=1`, `HF_HOME` e `TORCHINDUCTOR_CACHE_DIR` debaixo da raiz de dados, mesmo com `HF_HUB_OFFLINE=0`, `TRANSFORMERS_OFFLINE=0` e um `HF_HOME` hostil no ambiente. Num subprocesso, porque a constante de §2.3 é do processo.
- **Arquitetura:** nenhum módulo de `backend/app` fora de `app/infrastructure/ai/` importa `torch`, `transformers`, `huggingface_hub` ou `safetensors`. Sobre a árvore sintática, como o teste das âncoras do RFC-034.
- **O lockfile:** é JSON válido, cada modelo tem revisão de 40 caracteres hexadecimais e cada arquivo tem tamanho e `sha256`. O `directory` de cada modelo é único.
- **A conferência:** sobre uma árvore em miniatura num diretório temporário, com um lockfile do teste: cada linha de §6.4 produz a sua mensagem. Um arquivo a mais é ignorado. A conferência acontece antes do carregamento: um `from_pretrained` substituído por um dublê não é chamado quando o hash falha.
- **Os adaptadores:** recebem um diretório e passam `local_files_only=True`. Com um dublê no lugar do `from_pretrained`, o argumento é conferido.
- **A falha:** o `job_runner` sai com código 1 e uma linha; a API sobe com o aquecimento falhando, a busca responde 503 com a linha, e `/health` responde 200.
- **As chaves:** `EMBEDDING_MODEL` aparece como aposentada no relatório do RFC-034. `model_runtime_directory` é absoluto nos dois modos, pelos testes de caminho que já existem.
- **O aquecimento:** o default é `True` num `Layout` instalado e `False` num de desenvolvimento.
- **O `vendor_models.py`:** com um "Hub" de mentira num diretório local, uma materialização interrompida não deixa árvore; um arquivo com hash errado é recusado; `--from-cache` aceita um snapshot com o hash certo e recusa um com o hash errado.

**Sob o marcador `slow`,** pulados sem `runtime\models`, com o comando no motivo:

- Os testes `slow` que já existem passam a carregar pela árvore (`test_clip_embedding_model_slow.py`, `test_semantic_search_e2e.py`, `test_search_api_e2e.py`).
- **O arquivo de referência** de §7.2: as 45 imagens e as 50 consultas, contra os vetores gravados.
- Um carregamento completo com o `huggingface_hub` impedido de abrir conexões, pelo meio de §15.

## 10. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Levar o cache do Hugging Face como está, com `HF_HUB_CACHE` apontando para ele | A estrutura do cache é a de quem baixou (§2.1): arquivos em `snapshots\` aqui, links para `blobs\` noutra máquina. E o carregamento continuaria passando pela resolução do Hub, por `refs\main` e `.no_exist\`, que é o código que este RFC quer fora do caminho |
| Baixar na primeira execução, para a raiz de dados | Contradiz o critério: a máquina nunca acessou o Hugging Face. E são 924 MB por usuário |
| Os pesos na raiz de dados, por usuário | São iguais para todos os usuários e não mudam. O lugar é ao lado do código, como o runtime do PostgreSQL |
| O lockfile em `scripts/`, como o do RFC-033 | O instalado não tem `scripts\`, e o app precisa dos hashes para conferir (§4.1) |
| Só o manifesto, com os hashes ao lado dos pesos | Quem escreve os pesos escreve o manifesto. O lockfile no código amarra os pesos à versão do código |
| Conferir só na instalação, como o RFC-033 | O pickle seria desserializado sem conferência. E o roadmap pede a conferência no app. O segundo ramo de §6.3 fica entre as duas |
| Conferir só tamanho | Não pega um arquivo de outra revisão com o mesmo tamanho, nem um bit trocado |
| Conferir com um hash mais rápido (xxHash, BLAKE3) | Uma dependência nova, e o `sha256` é o que o Hub publica no ponteiro LFS, que o `--lock` usa para conferir (§4.3). Se o custo de §6.3 passar do critério, o segundo ramo resolve sem trocar o algoritmo |
| Calcular o hash enquanto o modelo carrega, lendo os bytes uma vez | Exige carregar a partir de bytes em memória, e não do `from_pretrained` com um diretório. Para o `.bin`, desserializa antes de conferir (§6.2) |
| O `safetensors` do `refs/pr/6` | §7.4 |
| Pesos em `float16`, quantizados ou em ONNX | Metade do tamanho, ou menos, e outros vetores. O RFC-023 §3.7 mediu a quantização e a rejeitou. Viola o critério de §7.2 |
| `embedding_model` mantido, validado contra o lockfile | Um setting com um único valor aceito (§4.4) |
| O modo offline só no modo instalado, como o roadmap diz | O desenvolvimento não acharia os caminhos que alcançam a rede, e acharia o usuário (§5.3) |
| `TRANSFORMERS_OFFLINE` | É sinônimo, lido pela mesma constante (§2.3). Um nome só |
| Pôr as variáveis no launcher (RFC-036) | O modo instalado não lê o ambiente, por decisão do RFC-034. E um processo aberto à mão, a CLI, não passaria pelo launcher |
| Alterar `huggingface_hub.constants.HF_HUB_OFFLINE` depois da importação | Só funciona para quem lê o atributo do módulo, e não para quem copiou o valor. A variável antes da importação é o caminho documentado. A conferência de §5.2 transforma a ordem errada em erro |
| Bloquear sockets dentro do app | Invasivo, e acharia também as conexões legítimas com o PostgreSQL. Bloquear a rede é ferramenta da medição (§15), e não do produto |
| O aquecimento ligado nos dois modos | Um `TestClient` da suíte rápida carregaria 924 MB (§8) |
| Gravar a impressão digital do modelo no banco | Uma migration, para um app que só consegue carregar um modelo depois deste RFC. É necessária no dia em que existir um segundo, e é do RFC que o trouxer (§13) |
| Uma rota de busca por imagem, para o critério do roadmap | É do frontend. O critério é conferido pela CLI (§2.5) |

## 11. Não-objetivos

- **O instalador**, a compressão dos pesos no pacote e a assinatura.
- **Congelar o runtime Python.** Este RFC fixa os pesos, e não as versões do `torch` e do `transformers`, que nenhum arquivo do repositório fixa hoje: o `requirements.txt` nem os lista. O arquivo de referência de §7.2 detecta uma mudança de vetor causada por uma atualização, e não a impede (§12).
- **GPU.** O `device` do RFC-023 continua como está.
- **Outro modelo, fine-tuning, quantização, ONNX.**
- **A procedência no banco** (§10).
- **Uma rota de busca por imagem.**
- **Um detector de idioma melhor** (RFC-023 §7.1).
- **Linux e macOS.**
- **Tirar o Docker do desenvolvimento.**

## 12. Riscos

| risco | situação |
| --- | --- |
| Um antivírus pôr o `pytorch_model.bin` em quarentena | **Hipótese.** A conferência transforma isso numa mensagem que nomeia o arquivo e a causa provável (§6.4). Não é medido aqui: a máquina da medição tem o antivírus que tem |
| A revisão fixada sumir do Hub | Um repositório apagado ou com o histórico reescrito impede materializar a árvore numa máquina nova. O instalador carrega a cópia, e nada no app depende do Hub. Uma cópia da árvore fora do Hub é decisão do instalador (§13) |
| Uma atualização do `transformers` ou do `torch` mudar os vetores com os mesmos pesos | Nada no repositório fixa as versões (§11). O teste do arquivo de referência acusa a mudança, desde que alguém rode os testes `slow` antes de atualizar |
| Uma atualização do `transformers` deixar de carregar `.bin` | O segundo ramo de §7.4 é a saída, e o teste `slow` do tradutor acusa |
| Alguém importar o `huggingface_hub` antes do pacote `ai` | O teste de arquitetura cobre `backend/app`. Fora dele, a conferência de §5.2 recusa o carregamento |
| A conferência custar mais que o critério | `TBM`, com segundo ramo (§6.3) |
| A tolerância entre máquinas sair grande | `TBM` (§7.3). Uma diferença que passe de ruído numérico seria um achado, e mudaria a leitura do critério |
| A árvore materializada em desenvolvimento divergir da instalada | É a mesma árvore, do mesmo lockfile, conferida pelo mesmo código. A instalação simulada do RFC-034 passa a copiar `runtime\models` (§14) |
| As licenças não permitirem redistribuir | `TBM` (§4.1). Até o `--lock` as ler, distribuir os pesos é uma pergunta aberta, e não uma decisão |
| A subida da API instalada ficar mais lenta | Declarado em §8. A conferência soma `TBM` |

## 13. O que passa adiante

**Para o RFC-036:**

- **Um filho pode sair por causa dos modelos**, com uma linha no `stderr` e código 1 (§6.5). Diferente de uma morte qualquer, reiniciar não resolve. O launcher precisa distinguir os dois casos, ou pelo menos não reiniciar em laço.
- **A API sobe sem modelos** e responde 503 na busca (§6.5). "Pronto", para o launcher, precisa dizer se isso conta.
- **A subida da API instalada aquece os modelos**, e leva `TBM` s (§8). O health check do launcher espera esse tempo.
- **Nenhum filho precisa de variável de ambiente para os modelos.** Cada processo monta o próprio ambiente da biblioteca (§5.2).

**Para o instalador:**

- Levar `runtime\models\` inteiro, com o manifesto: `TBM` MB descomprimidos, `TBM` MB comprimidos (§15). É a maior parcela do payload, à frente dos 123 MB do runtime do PostgreSQL.
- As licenças do lockfile (§4.1).
- A pasta é por máquina e só para leitura, como `runtime\postgres`.
- `<dados>\cache\` pode ser apagada pela desinstalação sem perda.

**Para o frontend:**

- Busca por imagem não tem rota (§2.5).
- A falta dos modelos é algo que a interface desenha diferente: sem caixa de busca, com uma mensagem. Pela régua do RFC-031 §9, é candidata a um campo do `/capabilities`. Este RFC não o acrescenta.

**Para quem trocar de modelo:** o lockfile, uma migration, uma coluna de procedência e um plano de reindexação (§4.4, §10).

## 14. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-035-modelos-offline.md` | Este documento |
| `backend/app/infrastructure/ai/models.lock.json` | O lockfile (§4.1) |
| `backend/app/infrastructure/ai/model_runtime.py` | Achar a árvore, conferir, devolver os diretórios (§5.1, §6) |
| `backend/app/infrastructure/ai/environment.py` | `prepare_model_environment()` (§5.2) |
| `scripts/vendor_models.py` | Materializar e escrever o lockfile (§4.3) |
| `backend/tests/infrastructure/ai/` | Os testes de §9 sobre o ambiente, a conferência e os adaptadores |
| `backend/tests/test_model_imports_architecture.py` | Nenhum `torch`, `transformers`, `huggingface_hub` ou `safetensors` fora do pacote `ai` (§9) |
| `backend/tests/fixtures/rfc035_reference_embeddings.npz` | Os vetores do carregamento atual (§7.2) |
| `experiments/rfc-035-offline-models/measure_offline_models.py` (+ `.log`, fora do git) | §15 |

O `pilot_hub_access_probe.py` já existe na mesma pasta. É o que §2 cita.

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/ai/__init__.py` | Chama `prepare_model_environment()` antes de qualquer outra importação |
| `backend/app/infrastructure/ai/clip_embedding_model.py`, `query_translator.py` | Recebem um diretório, passam `local_files_only=True`. Sai o default de nome |
| `backend/app/infrastructure/config/layout.py` | `model_runtime` |
| `backend/app/infrastructure/config/settings.py` | `model_runtime_directory` no `PATH_DEFAULTS`; sai `embedding_model`; `EMBEDDING_MODEL` nas chaves aposentadas; o default de `warm_up_models` vem do modo |
| `backend/app/presentation/api/__init__.py` | O aquecimento não derruba a subida; o handler do 503 (§6.5) |
| `backend/app/infrastructure/workers/job_runner.py`, `app/cli/search.py`, `indexing_worker.py` | A falha dos modelos vira uma linha e código 1 |
| `backend/app/presentation/dependencies/` | `get_embedding_model()` passa os diretórios conferidos |
| `scripts/stage_installation.py` | Copia `runtime\models` (§12) |
| Os três testes `slow` | Carregam pela árvore, pulam sem ela |
| `.env.example` | Sai `EMBEDDING_MODEL`. Entra `MODEL_RUNTIME_DIRECTORY`, comentado. O comentário do `WARM_UP_MODELS` passa a dizer o default por modo |
| `backend/README.md` | O passo do `vendor_models.py` |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-standalone.md` | O status deste RFC |

Não mudam: o Domain, a Application, a `EmbeddingModelPort`, as migrations, o `docker-compose.yml`, o adaptador do PostgreSQL e o `device.py`.

## 15. Validação

A máquina e as condições estão declaradas abaixo, como no RFC-034. Os `TBM` do corpo do texto ficaram como foram escritos, à vista do que se esperava; os números que os respondem estão aqui, e o que mudou por causa deles, em §16.

**Declarado antes da medição:**

- **O critério de §7.2 é exato.** Um bit diferente, num vetor ou numa string traduzida, reprova.
- **A conferência soma no máximo um quarto ao carregamento**, a frio e a quente (§6.3). Acima disso, entra o segundo ramo.
- **Carregar pelo diretório não fica mais lento que pelo nome.** Com rede, o carregamento pelo nome espera 3,2 s pelo Hub no CLIP e 1,1 s no tradutor, nesta conexão (§2.4). Sem essa espera, deveria ficar mais rápido.
- **"Nunca acessou o Hugging Face" é provado por três ambientes vazios e um sentinela.** Os filhos rodam com `USERPROFILE`, `LOCALAPPDATA` e `TEMP` apontando para diretórios temporários vazios, de modo que nenhum cache do perfil real seja lido. `HTTP_PROXY` e `HTTPS_PROXY` apontam para um sentinela em loopback que registra toda tentativa de conexão e a recusa. O `huggingface_hub` usa o `httpx`, que lê essas variáveis por padrão. **Hipótese:** é o caminho de toda requisição do Hub. Uma conexão que ignore o proxy não aparece no sentinela, e por isso há também a verificação manual com a rede desligada.
- **Toda verificação tem prazo** ([RFC-034 §2.1](rfc-034-configuracao-e-diretorios-do-usuario.md#21-o-backend-só-existe-dentro-de-um-checkout)).
- **O que o piloto já mediu não é medido de novo**: a origem do `refs/pr/6` (§2.2), o `main` resolvido a cada carregamento (§2.4) e as zero requisições de um carregamento pelo diretório (§2.5). Essas três medidas são do código de hoje, e a medição abaixo é do código deste RFC.

> **Medido em 2026-10-14 e 2026-10-15, na máquina do piloto.** Windows 10 Home (10.0.19045), Pentium G4560 (Intel Family 6 Model 158, 2 núcleos, 4 threads, sem as instruções SHA), 16 GB, HDD SATA `ST1000DM010-2EP102`, Python 3.12.9, `torch` 2.13.0+cpu com 2 threads, `transformers` 5.15.0, `huggingface-hub` 1.27.0, `safetensors` 0.8.0, processo sem elevação. Quatro logs ao lado do script, fora do git: `measure_offline_models.reference.log` (o bit a bit com o `.bin`, uma thread, o banco), `measure_offline_models.loads.log` (o custo da conferência, os carregamentos e a primeira medida de §7.4), `measure_offline_models.log` (o resto, já com a conversão) e `measure_offline_models.reference-rewritten.log` (o arquivo de referência regravado contra o lockfile da conversão). **"A frio" é uma cópia escrita com `FILE_FLAG_NO_BUFFERING`**, cujas páginas nunca entraram no cache: as cópias frias foram lidas a 97–126 MB/s, a velocidade do disco (§16.2). Toda mediana é de cinco processos.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte no head de partida | `pytest` puro, antes de qualquer mudança | **2492 passed, 83 deselected**, em 196,6 s, no `e52372b`, em 2026-10-14, com o Docker de pé |
| A suíte no fim | `pytest` puro, e `pytest -m slow` | **2599 passed, 88 deselected**, em 110,6 s: a de partida mais 107 testes na suíte padrão, e 5 a mais sob `slow`, os do arquivo de referência. **`pytest -m slow`: 63 passed**, em 152,7 s, todos carregando pela árvore. As duas contra um cluster embarcado num diretório temporário, por `embedded run --`, porque o Docker Desktop caiu entre as sessões (§16.2). Antes de cair, a suíte padrão já tinha dado 2591 passed contra o contêiner, antes da conversão |
| Materializar | `vendor_models.py` num diretório vazio, do Hub e com `--from-cache` | **Do cache, 9,6 a 11,7 s; do Hub, 68,6 s**, os 923 879 112 bytes nas duas origens, com a conversão do tradutor feita na hora. **Os hashes do lockfile têm duas fontes:** o `--lock` conferiu os dois pesos contra o `sha256` do ponteiro LFS e os treze arquivos pequenos contra o id do blob do git, todos iguais (§16.2). O `--from-cache` não escreveu nada no cache do usuário |
| Tamanho em disco | `runtime\models` inteiro; e comprimido, em ZIP e em 7z | **881,1 MB** (923 879 942 bytes: os arquivos do lockfile e 830 bytes de manifesto); **813,8 MB em ZIP** (deflate nível 9, 92,4%) e **802,4 MB em LZMA2** (91,1%). O 7-Zip não está instalado nesta máquina; o LZMA2 do `xz`, no preset 5, é o método padrão dele. Pesos em `float32` quase não comprimem. São 24 541 bytes a menos que os 923 903 653 de §2.1: o `safetensors` do tradutor é menor que o `.bin` (§7.4) |
| Indexar e buscar sem rede, nunca tendo acessado o Hub | Instalação simulada só para leitura, os três ambientes vazios, o sentinela: um job indexando as 45 imagens; buscas em PT e em EN pela API; uma busca por imagem pela CLI | **Funciona, com zero tentativas no sentinela. É o critério do roadmap.** Numa cópia só para leitura de 1006,5 MB, com `USERPROFILE`, `LOCALAPPDATA`, `APPDATA` e `TEMP` vazios: o cluster criado em 39,9 s; `app.cli index` indexou as 45 imagens em 35,6 s (*45 indexed, 0 skipped, 0 failed*); o `job_runner` entrou no laço 13,5 s depois, com o CLIP carregado; a API respondeu as buscas *"propriedade rural com um pequeno lago"* (200, 348 ms), *"rural property with a small lake"* (200, 100 ms) e *"um gato preto descansando dentro de casa"* (200, 324 ms, a foto do gato em primeiro); e `app.cli search-image lake_property_01.jpg` devolveu a própria foto, com similaridade 1,000 |
| O mesmo, com a rede desligada de verdade | Modo avião, à mão | **Não feito.** É manual por natureza, e fica com quem tiver a máquina na mão; a linha `network-off` do script diz o que fazer |
| Bit a bit (§7.2) | As 45 imagens, uma a uma e em lotes de 8, e as 50 consultas, pelo nome e pelo diretório, no mesmo processo | **Bit a bit, três vezes.** Com o `.bin`, em 2026-10-14, e com o `safetensors` convertido, duas vezes em 2026-10-15: 45 de 45 vetores idênticos uma a uma, 45 de 45 em lotes, 50 de 50 consultas, e as 50 strings traduzidas iguais. Das 25 consultas em português, 24 foram reconhecidas e traduzidas (§16.1). O arquivo de referência tem 272 531 bytes |
| Uma thread contra o default (§7.3) | O mesmo conjunto, pelo diretório, com `torch.set_num_threads(1)` | **As imagens uma a uma e as consultas, idênticas.** Em lotes de 8, 40 de 45 idênticas, e a maior diferença 8,9e-8. O menor cosseno, 0,999999881, é o piso da norma de um vetor `float32`: dois vetores idênticos dão 0,999999821 |
| Contra o banco de desenvolvimento (§7.3) | Os vetores novos das 45 imagens contra os gravados | **O banco tem 20 das 45**, e mais 40 fotos DJI de outra pasta; as 60 com o arquivo no disco, sem mudança de conteúdo, entraram. **Nenhuma idêntica, a maior diferença 1,49e-7**, e o menor cosseno no mesmo piso, 0,999999821. O mesmo espaço, até o último bit do `float32`. É de onde sai a tolerância de outras máquinas no teste de referência, 1e-5, duas ordens acima |
| A conferência (§6.4) | Na instalação simulada: sem árvore; manifesto de outro lockfile; um arquivo apagado; um truncado; um byte trocado no `.safetensors` e no `.bin`; o `model.safetensors` de `openai/clip-vit-base-patch32` no lugar do certo | **Cada caso com a sua linha, e nenhum traceback** no `stderr` dos três programas nem no log da API. O `job_runner` saiu com código 1 e *"SolidVision cannot start: …"*; a CLI, com código 1 e a linha; a API ficou de pé, com `/health` em 200 e a busca em 503 com a linha em `detail`. **Uma diferença, de propósito:** com o peso do tradutor corrompido, o `job_runner` sobe, porque só carrega o CLIP; a CLI e a busca da API, que traduzem, recusam. O `.bin` do texto não está mais na árvore, e o byte trocado foi no `safetensors` do tradutor (§16.1). O CLIP da OpenAI tem o tamanho exato do certo, e só o hash o recusou |
| O custo da conferência (§6.3) | `sha256` de cada modelo, a frio e a quente, contra o carregamento do mesmo modelo | **Acima do critério nos dois modelos, e o segundo ramo entra.** CLIP: o hash leva 4,67 s a frio e 2,41 s a quente, contra 2,03 s e 0,645 s do carregamento, **+161% e +373%**. Tradutor, ainda pelo `.bin`: +4% a frio e **+47%** a quente (1,19 s de hash contra 2,54 s). Um carregamento abonado pelo registro custa **2,6 ms** pelos dois modelos, e a conferência de subida 1,4 ms |
| Carregar pelo diretório | CLIP e tradutor, a frio e a quente, mediana de cinco, contra o carregamento pelo nome | **Mais rápido, nos dois.** CLIP: **2,03 s a frio e 0,645 s a quente** pelo diretório, contra 5,75 s e 4,20 s pelo nome, com a rede, dos quais 3,34 s e 3,07 s esperando 32 e 30 requisições ao Hub. Tradutor: 4,74 s e 2,54 s pelo `.bin`, e **2,90 s e 2,09 s** pelo `safetensors` convertido, contra 7,52 s e 4,06 s pelo nome (19 e 11 requisições na thread que carrega, e mais 6 da conversão em segundo plano) |
| `.bin` contra `safetensors` convertido (§7.4) | O tradutor, a frio e a quente | **A conversão entra.** Primeira medida, com uma cópia do `save_pretrained`: 5,23 s contra 3,05 s a frio, **2,18 s** mais rápido; a quente, 2,58 s contra 2,15 s. Com o arquivo que o `vendor_models.py` grava: 4,92 s contra 2,90 s, **2,03 s**; a quente, 2,72 s contra 2,09 s. Os 259 tensores carregados de cada um, idênticos |
| "Grava tudo", com modelos | As comparações do RFC-034 §9, com o `job_runner` e a API aquecida carregando os dois modelos | **Nada fora da raiz de dados, e o `%TEMP%` dos filhos vazio.** O perfil fora da raiz de dados, o `%TEMP%` dos filhos, o diretório de trabalho e a árvore instalada ficaram iguais. A raiz de dados ganhou `cache\`, com o `models-verified.json` e uma pasta `torchinductor` vazia. **Nenhuma pasta `huggingface`**: o "provavelmente nada" de §5.4 foi nada |
| Ambiente hostil | `HF_HUB_OFFLINE=0`, `TRANSFORMERS_OFFLINE=0`, `HF_HOME`, `HF_HUB_CACHE`, `HF_ENDPOINT` e `TORCHINDUCTOR_CACHE_DIR` apontando para outro lugar | **`is_offline_mode()` verdadeiro, nada escrito nos lugares apontados**, zero tentativas no sentinela, para onde o `HF_ENDPOINT` apontava. `HF_HOME` e `TORCHINDUCTOR_CACHE_DIR` saíram na raiz de dados. O `HF_HUB_CACHE` continuou apontando para fora, porque o RFC sobrescreve três variáveis e essa não é uma delas; nada foi escrito lá, porque nada lê nem escreve o cache do Hub quando se carrega por diretório |
| A API instalada aquecida | Instalação simulada, sem arquivo do usuário | **18,7 s até o primeiro `/health`**, com o aquecimento ligado pelo modo: 4,5 s de aquecimento, que conferiu o hash do tradutor pela primeira vez (o do CLIP já estava no registro, da CLI). **A primeira busca em PT, 348 ms**; em EN, 100 ms. O resto dos 18,7 s é a importação do `torch` neste HDD (§16.2) |
| Arquitetura | O teste de importações | **Passa**: 38 testes, o de importações dos modelos, o das fronteiras do RFC-023 e o das âncoras do RFC-034 |
| Estilo e tipos | `black --check`, `ruff check`, `mypy` | **Limpos.** `black --check` e `ruff check` sobre `backend/app`, `backend/tests`, `backend/dataset_tools`, os três scripts mudados e o da medição; `mypy` com a configuração do projeto, 301 arquivos |

## 16. Correções feitas durante a implementação

Registradas à vista do texto original, como o RFC-033 e o RFC-034 fizeram com as suas. Duas mudam uma decisão de §3, e as duas por um segundo ramo que o próprio texto declarou antes da medição: a conferência completa não acontece em todo carregamento (§6.3), e o tradutor deixa o `.bin` por um `safetensors` convertido daqui (§7.4). As outras são o que o texto não previa, ou previa de outro jeito.

### 16.1 O que a medição contrariou

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §6.3 | *"a conferência completa acontece em todo carregamento se somar no máximo um quarto ao tempo de carregamento"* | **Somou muito mais, e o segundo ramo entrou.** Os números estão em §15. Uma pré-medição, antes de escrever a conferência, já decidia: o `sha256` em memória roda a 257 MB/s nesta CPU (um Pentium G4560, sem as instruções SHA), o que põe o CLIP em pelo menos 2,4 s de hash contra cerca de 1 s de carregamento a quente. O registro de verificação de §6.3 está em `model_runtime.py`, com os quatro dados que o texto pede e o `sha256` esperado de cada arquivo |
| §7.4 | *"O `pytorch_model.bin` do `main` (escolhida)"*, e o segundo ramo: a conversão entra se o convertido carregar pelo menos 1 s mais rápido a frio | **Entrou: 2,2 s mais rápido a frio** (§15), com os 259 tensores idênticos depois de carregar. O `vendor_models.py` confere o `sha256` do `.bin` contra o lockfile, só então o desserializa (`weights_only=True`), e grava um `model.safetensors` que o lockfile fixa; a árvore não leva pickle nenhum, e o app não desserializa pickle nenhum. O lockfile guarda os dois: o arquivo convertido em `files`, e o `.bin` de origem, com o hash dele e os nomes amarrados que a conversão descarta, em `converted`. Os vetores de §7.2 foram conferidos de novo com o arquivo convertido (§15) |
| §7.4 | *"a conversão precisa ser reproduzível byte a byte"* | **Só com metadado de uma chave.** O `safetensors` 0.8 grava um metadado de várias chaves em ordem aleatória: em seis processos, o mesmo arquivo saiu com cinco ordens diferentes. É o caso da conversão do próprio Hub (`refs/pr/6`), que guarda no metadado os dois nomes descartados, e por isso nem o robô reproduziria o arquivo dele byte a byte. A conversão daqui grava só `{"format": "pt"}`, e sai igual em todo processo; o `transformers` amarra os nomes descartados pela configuração do modelo, e não pelo metadado. Comparada com a do Hub, a conversão daqui tem o mesmo cabeçalho de tensores e a mesma seção de dados, byte a byte: só o metadado difere |
| §2.3 | *"Pôr a variável no ambiente depois da importação não muda nada"* | **Vale depois da importação de `huggingface_hub.constants`, e não do pacote.** O `huggingface_hub` 1.27 anexa os submódulos sob demanda: um `import huggingface_hub` sozinho não calcula a constante, e um processo que o fizesse antes do pacote `ai` ainda ficaria offline. Quem calcula a constante é o primeiro acesso a `constants`, que o `import transformers` faz. A conferência de §5.2 continua necessária, e o teste dela importa `huggingface_hub.constants` |
| §7.2 | *"45 imagens e 50 consultas, 512 `float32` cada, ~190 KB"* | **272 527 bytes.** As imagens em lotes de 8 têm vetores próprios no arquivo: diferem dos vetores uma a uma em até 1,19e-7 (o RFC-024 já dizia: iguais até a precisão do ponto flutuante, e não bit a bit), e o critério é bit a bit |
| §7.2 | *"As 25 consultas de ground truth do RFC-022, em português e em inglês"* | **O repositório só tem as 25 em inglês.** O conjunto em português do bake-off do RFC-023 se perdeu com os scripts dele, e o log guardou cinco. As cinco entram literais, sem acentos como o bake-off as escreveu; as outras vinte foram escritas para esta medição, no mesmo estilo, e estão em `measure_offline_models.py` e dentro do arquivo de referência. O `queries.json` do RFC-022 não mudou. Das 25, o `langdetect` reconhece 24 como português: *"rua residencial ladeada por casas"* chega ao CLIP sem tradução, a limitação do RFC-023 §7.1, e o arquivo de referência a registra como é |
| §6.4 | O caso do `openai/clip-vit-base-patch32` | **O arquivo errado tem exatamente o tamanho do certo**, 605 157 884 bytes: os dois são ViT-B/32 em `float32`. Só o hash o pega. É o caso que justifica conferir o conteúdo, e não só o tamanho (§10) |

### 16.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §5.1 | **Os adaptadores recebem uma função, e não um caminho.** O texto pede as duas coisas: que o adaptador receba um diretório *já conferido* e que o construtor continue sem custo, com a conferência no primeiro carregamento. Uma função as concilia: `ModelDirectory = Callable[[], Path]`, chamada no primeiro carregamento, que confere e devolve o diretório. `ModelRuntime.loader(chave)` a produz |
| §6.2, §12 | Com a conversão, o argumento de §6.2 muda de lugar sem mudar de forma: quem desserializa o pickle é o `vendor_models.py`, uma vez, na máquina de quem materializa, e só depois de conferir o hash dele. Na máquina do usuário não há pickle, e o risco de §12 de um antivírus tratar 312 MB de pickle como suspeitos sai com ele. A conferência antes do carregamento continua, pela mensagem: um arquivo truncado chega como uma linha, e não como um traceback |
| §5.1 | O `ClipEmbeddingModel` deixa de construir o próprio tradutor, porque o tradutor precisa do diretório dele: `translator` passou a ser obrigatório. O nome do tradutor, a constante `TRANSLATION_MODEL`, saiu junto com o `embedding_model` |
| §6.1 | **O acessor do runtime fica no pacote `ai`, e não na raiz de composição.** `default_runtime()`, em `model_runtime.py`, importa o `Settings` e nada do `torch`. Assim a conferência de subida vem antes da importação do `torch`, que leva de 14 a 40 s neste HDD: o `job_runner` e a CLI recusam uma árvore ausente em 1 a 2 s. O `get_embedding_model()` usa o mesmo acessor |
| §6.1 | A CLI de indexação confere a presença antes de registrar o dispositivo e criar o job: sem modelos, nenhum job fica para trás |
| §6.4 | **Um hash recusado é lembrado pelo processo**, pelo caminho e pela identidade do arquivo. Sem isso, cada busca respondida com 503 custaria um hash inteiro dos pesos. Um arquivo trocado depois tem outra identidade e é conferido de novo: consertar a árvore não exige reiniciar a API |
| §6.3 | A conferência de um modelo é feita por uma thread de cada vez. Duas primeiras buscas que acham o adaptador descarregado (a corrida do RFC-026 §9.1) fazem um hash só: a segunda acha o registro que a primeira escreveu |
| §6.4 | O registro que não pode ser lido, ou que foi escrito para outro lockfile, não abona nada; o que não pode ser escrito vai para o log, e o carregamento segue |
| §4.1 | **O lockfile é lido como dado do pacote**, por `importlib.resources`, e não por um caminho calculado a partir de um arquivo: o teste das âncoras do RFC-034 proíbe `__file__` fora de `layout.py`, e o instalador pode congelar o código |
| §4.3 | **O `--lock` confere os quinze arquivos no Hub, e não só os dois grandes.** Os dois pesos pelo `sha256` do ponteiro LFS; os treze pequenos, que o Hub não guarda no LFS, pelo id do blob do git, que é um `sha1` do conteúdo. A hipótese de §4.3 se confirmou para os dois grandes, e o lockfile tem duas fontes para todo arquivo |
| §4.1 | As licenças lidas dos model cards: **`mit`** para o CLIP e **`apache-2.0`** para o tradutor. As duas permitem redistribuir, com o aviso de copyright e a licença junto (§13) |
| §4.3 | O `vendor_models.py` aponta o `HF_HOME` para um diretório temporário antes de importar o `huggingface_hub`, pelo mesmo motivo do app: nada do que a biblioteca grava fica no perfil de quem materializa. O manifesto e o digest do lockfile são calculados do mesmo jeito nos dois lados, e um teste materializa uma árvore com o script e a confere com o app |
| §6.5 | A conversão para 503 é um handler registrado na aplicação, ao lado do dos erros de domínio, como o texto pede. O aquecimento registra a linha do `ModelUnavailableError` sem traceback; outro erro qualquer continua com o traceback, como antes |
| §9 | O `conftest.py` raiz importa o pacote `ai` antes de tudo. Um módulo de teste importa as bibliotecas de terceiros antes das do app, e sem isso a ordem dependeria de qual teste foi coletado primeiro |
| §9 | Os testes `slow` usam o adaptador do próprio processo, montado como o app o monta, e pulam com a linha do `ModelUnavailableError` quando a árvore não existe |
| §14 | `scripts/benchmark_indexing.py` construía `ClipEmbeddingModel()` sem argumentos, e passou a montá-lo pela raiz de composição. Os scripts de medição dos RFCs anteriores em `experiments/` fazem o mesmo e não foram mudados: medem o código do tempo deles |
| §15 | **A medição a frio não usa privilégio.** Esvaziar a lista de espera do Windows exige administrador, então um carregamento a frio é o de uma cópia escrita com `FILE_FLAG_NO_BUFFERING`, cujas páginas nunca entraram no cache. A medição diz a que velocidade cada cópia foi lida, que é o que prova que estava fria |
| §15 | **O carregamento pelo nome do tradutor, numa cópia do cache, dispararia a thread de conversão de §2.2** e baixaria de novo 312 MB a cada execução. A cópia leva também o `refs/pr/6` e o snapshot dele, por links para os arquivos do cache real, como esta máquina os tem |
| §15 | **O Docker Desktop caiu entre as duas sessões da medição**, e as suítes do fim rodaram contra um cluster embarcado criado num diretório temporário, por `embedded run --`, o caminho que o RFC-034 mediu como equivalente ao contêiner. O `run --` acha o executável do comando pelo `CreateProcess`, que procura primeiro na pasta do Python base, e não na do virtualenv: o comando precisa do caminho inteiro do `python.exe` do virtualenv. Não é deste RFC, e fica registrado para quem usar o `run --` |
| §5.4 | O ambiente hostil mostrou que o `HF_HUB_CACHE` sobrevive, porque o RFC sobrescreve três variáveis e essa não é uma delas. Nada é escrito lá, porque carregar por diretório não passa pelo cache do Hub; fica registrado como a quarta variável a acrescentar se um dia algo passar |

### 16.3 O que ficou sem medição

Dito aqui para não parecer medido em outro lugar.

- **A rede desligada de verdade** (§15). A instalação simulada provou o critério com o sentinela e os três ambientes vazios; o modo avião é à mão, e fica com quem tiver a máquina.
- **Outro processador.** A tolerância do teste de referência em outra máquina (1e-5) vem de duas medições nesta: uma thread contra duas, e o banco contra hoje. Um processador com AVX-512, como o i5-1035G1 de §7.3, pode escolher outros kernels; o teste vai dizer, na primeira vez que rodar lá.
- **Um antivírus.** O risco de §12 ficou menor com a conversão, que tira o pickle da árvore; o de quarentena de um arquivo qualquer continua, e a mensagem que o nomeia foi medida apagando o arquivo, e não com um antivírus de verdade.
- **Um `C:\Program Files` de verdade**, pelo mesmo motivo do RFC-034 §17.3: a cópia é trancada com a mesma forma de DACL, mas sem o `TrustedInstaller` como dono.
