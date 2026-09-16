# RFC-029 — Jobs de Indexação e Indexação Seletiva

**Status:** Implementado
**Depende de:** RFC-021 (worker), RFC-024 (pipeline), RFC-026 (camada HTTP), RFC-027 (dispositivos), RFC-028 (data de captura)
**Migration:** sim — `indexing_jobs`, `indexing_job_scopes` (`e7a2c9b41f30`)
**Medição:** `experiments/rfc-029-indexing-jobs/` — `measure_heartbeat_gaps.py` (§9.1), `measure_queue_cancellation_resume.py` (§6.1, §8, §10) e `measure_throughput_and_api.py` (§2.1, §2.3), cada um com o `.log` ao lado

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*
> durante a implementação e devia ser escrito de volta aqui depois. **Isso foi
> feito:** não resta nenhum `TBM`, e cada número abaixo vem com a escala e as
> condições em que foi medido. Onde a medição não respondeu à pergunta inteira
> — §9.1 (varredura fria de disco mecânico) e §10 (a mesma limitação) — o que
> ficou por medir está dito, em vez de ser preenchido por dedução.

> **Cinco decisões deste documento estavam erradas, e a implementação as
> corrigiu.** São §6 (o worker em container), §7.1 (o status de "disco
> desconectado"), §9.1 (o que o *reaper* faz com um job abandonado), §10 (como
> o checkpoint é comparado e até onde ele pode avançar) e §12 (o que `--root`
> vira). Cada uma está marcada **Correção** na seção correspondente, com a frase
> original preservada à vista — a convenção do [README](README.md): decisões
> revertidas permanecem documentadas, porque o raciocínio que levou ao erro é
> a parte que se repete.

---

## 1. Contexto

O requisito chegou junto de uma suposição, e a suposição merece ser desfeita antes de qualquer decisão:

> *"Sempre achei que a indexação incremental automaticamente significava indexação via UI — o usuário escolhe que pastas indexar."*

São duas propriedades diferentes, e o sistema tinha exatamente uma delas.

| | o que é | estado antes desta RFC |
| --- | --- | --- |
| **Incremental** (RFC-020, RFC-024) | não recomputar o que não mudou | **existia** — `plan_indexing()`, escada de custo, hash SHA-256 |
| **Seletiva** | o usuário escolhe *o quê* e *quando* | **não existia** |

Elas são ortogonais. O que rodava era incremental e **não** seletiva: `python -m app.infrastructure.workers.indexing_worker --root PATH` varria tudo sob a raiz, pulava o que não mudou, e não oferecia nenhuma forma de dizer *"só esta pasta"* ou *"pare"*.

A junção das duas é o que o requisito realmente pedia, e a justificativa dada para ela é a melhor parte do pedido:

> *"Ele quer achar uma foto antiga que sabe que foi de 2018 — então, se não estão indexadas, ele seleciona apenas as pastas desse ano para indexar."*

## 2. Problema

### 2.1 Indexar tudo é uma barreira de horas antes do primeiro resultado

O RFC-024 mediu 2,2 imagens/segundo em CPU. Contra o acervo que o `ARCHITECTURE.md` §2 descreve:

| acervo | custo de indexação completa (CPU, medido pelo RFC-024) |
| --- | --- |
| 40.000 fotos (um HD) | **~5 h** |
| 100.000 fotos (a meta de §22) | **~12,6 h** |

Isso é o custo para o **primeiro** resultado útil, porque nada é pesquisável antes de existir embedding. Um usuário que quer testar se a ferramenta serve para ele paga meio dia antes de digitar a primeira consulta.

Com escopo seletivo, o mesmo usuário indexa `HD2/2018/` e busca. **Medido** (`measure_throughput_and_api.log`), com um job real do `POST` ao `completed`, executor em processo separado, PostgreSQL real, CLIP real em CPU:

| | |
| --- | --- |
| corpus | 2.000 imagens, um escopo |
| status final | `completed`, 2.000 indexadas, 0 falhas |
| relógio de parede | **851 s — 14,2 minutos** |
| throughput | **2,35 imagens/s** |
| por imagem | 0,426 s |

O número inclui o processo subir e carregar o modelo, que um executor de
vida longa paga uma vez. E ele confirma de forma independente a medição da
RFC-024 (2,2 imagens/s em CPU), o que é o melhor que se pode pedir de dois
experimentos separados sobre o mesmo gargalo.

Ou seja: **a pasta de 2018 sai em quinze minutos, não em cinco horas.**

A ferramenta passa a ser avaliável no mesmo dia em que é instalada. Isso não é conveniência: é a diferença entre um sistema demonstrável e um que precisa de uma noite de preparo para mostrar qualquer coisa.

**O que esse número não é.** O corpus medido é sintético — JPEGs 128×128 gerados para o teste — e mora no disco do sistema, quente em cache. O CLIP redimensiona tudo para 224×224, então o custo de *inferência* é representativo; o custo de *decodificar* uma foto real de 4000×3000 é maior, e a leitura fria de um HD mecânico externo não está aqui de forma alguma. O número é um limite superior de throughput, não uma promessa sobre um acervo real.

### 2.2 O CLI não pode ser a única porta

`--root PATH` exige linha de comando, um caminho digitado corretamente, e um terminal aberto durante horas. O mockup do Figma que originou esta discussão mostra a alternativa: uma lista de pastas com estado `Indexed` / `Not indexed` e um botão por pasta.

### 2.3 O que o RFC-026 realmente proibiu

O RFC-026 §3 listou:

> *"Indexação por HTTP: **Não neste RFC.** O CLI do worker continua sendo o único ponto de entrada."*

Isso é frequentemente lido como *"indexação não deve ser exposta por HTTP"*. Não é o que o documento diz nem o que o raciocínio dele sustenta. O RFC-026 §9 decidiu que o endpoint de busca é `def` e não `async def` porque *"tudo abaixo desta linha bloqueia"* — 90 ms de inferência já foram considerados suficientes para justificar um threadpool.

Uma rota que rodasse indexação **dentro da requisição** ocuparia um worker do threadpool por cinco horas e derrubaria a responsividade da API inteira, `/health` incluído. O que o RFC-026 proibiu foi essa forma, e a proibição continua correta. Esta RFC não a viola: entrega a forma assíncrona, que é a única que sempre foi compatível.

**E processos separados não garantem responsividade sozinhos — isso foi medido em vez de afirmado** (`measure_throughput_and_api.log`, executor real em subprocesso saturando um núcleo durante os 14 minutos de §2.1, API no mesmo computador, 671 amostras de cada):

| | ocioso | com o job rodando |
| --- | --- | --- |
| `GET /health`, mediana | 7,4 ms | **7,1 ms** |
| `GET /health`, p95 | 7,8 ms | 13,9 ms |
| `GET /health`, máxima | 11,1 ms | 94,2 ms |
| `GET /images/search`, mediana | 159,6 ms | **195,5 ms** |
| `GET /images/search`, p95 | 168,8 ms | 516,1 ms |
| `GET /images/search`, máxima | 368,8 ms | 1358,2 ms |

`/health` é indistinguível sob carga, que é a metade fácil: ele não toca nem o modelo nem uma transação longa.

**A busca é a metade honesta.** Ela é CPU-bound — uma passada pela torre de texto do CLIP — então disputa núcleos com o executor e fica ~22% mais lenta na mediana, com uma cauda bem pior. Isso é disputa de CPU, e não event loop bloqueado: a rota **continua respondendo**, que é exatamente o que indexar *dentro* da requisição não faria. A diferença entre "mais lento" e "indisponível por cinco horas" é a coisa toda que esta RFC entrega, e o documento não finge que a separação de processos torna a contenção nula.

Não medido: o mesmo teste com vários executores, ou numa máquina com GPU, onde a contenção se mudaria para outro lugar.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Nova tabela | `indexing_jobs` — o desenho do `ARCHITECTURE.md` §15, ajustado (§5) |
| Nova tabela | `indexing_job_scopes` — as pastas de um job (§5.2) |
| Quem executa | **Processo executor separado, no host**, sondando a tabela (§6) |
| Criar job | `POST /api/v1/jobs` → **202 Accepted**, corpo com o id (§7) |
| Acompanhar | `GET /api/v1/jobs/{id}` — polling, sem WebSocket (§7.2) |
| Cancelar | `POST /api/v1/jobs/{id}/cancel` — cooperativo (§8) |
| Estados | `pending → running → completed \| failed \| cancelled` (§5.1) |
| Concorrência | **Um job ativo por dispositivo**, imposto pelo banco (§9) |
| Job órfão (worker morreu) | Heartbeat por tempo + reaper por timeout, **devolve o job à fila** (§9.1) |
| Retomada | Checkpoint por caminho, comparado como caminho (§10) |
| FK para `images` | **Nenhuma** (§11) |
| Nova infraestrutura (Redis, Celery) | Não (§6.1) |
| O CLI | **Permanece**, vira cliente da mesma máquina de jobs, e não exige um segundo processo (§12) |

## 4. Arquitetura

A tabela de camadas do RFC-026 §4, com a linha que muda:

| camada | conhece | não conhece |
| --- | --- | --- |
| **Presentation** | que um job é criado e consultado por HTTP, que 202 significa aceito | que existe um processo executor, como um lote é montado |
| **Application** | o que é um escopo válido, que estados sucedem quais, que um job pode ser cancelado | a ordem de varredura, SQL, o modelo |
| **Domain** | `IndexingJob`, `JobStatus`, `JobScope`, e as transições legais | `rglob`, sondagem, FastAPI |
| **Infrastructure (workers)** | a sondagem, o laço, o checkpoint, o cancelamento cooperativo | por que este job foi criado |

O ponto estrutural: **a rota não indexa nada.** Ela escreve uma linha. Toda a diferença entre esta RFC e a coisa que o RFC-026 proibiu está aí.

Uma consequência de camada que a implementação tornou explícita: a porta de observação do pipeline (`IndexingObserver`, §7.3) vive no **Domain** e fala em `IndexingProgress` — quatro inteiros e um booleano — e **não** no `IndexingSummary` da Application. O rascunho deste documento supunha o contrário; um porta de Domain assinada com um tipo da Application faria o Domain importar a Application, que é a dependência ao contrário. O `IndexingSummary` continua sendo o que um humano lê no fim de uma execução; o `IndexingProgress` é o que uma barra de progresso precisa.

## 5. Esquema

### 5.1 `indexing_jobs`

| coluna | tipo | nota |
| --- | --- | --- |
| `id` | `UUID` PK | `uuid4` — um job é um evento, não um conteúdo derivável |
| `device_id` | `UUID` FK → `devices.id` | RFC-027 |
| `status` | `TEXT` NOT NULL | `pending`/`running`/`completed`/`failed`/`cancelled` |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |
| `started_at` / `finished_at` | `TIMESTAMPTZ` NULL | `started_at` é a **primeira** reivindicação, preservada numa retomada |
| `discovered_files` | `INT` NOT NULL default 0 | contador crescente; só é denominador quando `discovery_complete` |
| `discovery_complete` | `BOOLEAN` NOT NULL default false | o que transforma o contador acima em total |
| `processed_images` | `INT` NOT NULL default 0 | |
| `skipped_images` | `INT` NOT NULL default 0 | pulados pela decisão incremental |
| `failed_images` | `INT` NOT NULL default 0 | |
| `last_processed_relative_path` | `TEXT` NULL | o checkpoint (§10) |
| `error_message` | `TEXT` NULL | preenchido apenas em `failed` |
| `last_heartbeat_at` | `TIMESTAMPTZ` NULL | a prova de que alguém ainda está trabalhando neste job (§9.1) |
| `cancel_requested` | `BOOLEAN` NOT NULL default false | a rota pede; o worker age (§8) |
| `attempts` | `INT` NOT NULL default 0 | quantas vezes um worker levou este job e não terminou (§9.1) |

Diferenças em relação ao `ARCHITECTURE.md` §15, e por quê:

- **`device_id` em vez de `collection_id`.** `Collection` continua placeholder (RFC-027 §10); dispositivo é o que existe.
- **`last_processed_relative_path` em vez de `last_processed_path`.** O RFC-027 removeu o caminho absoluto do banco; um checkpoint absoluto seria a mesma instabilidade de novo.
- **`skipped_images` acrescentado.** Sem ele, um job que pulou 39.000 de 40.000 arquivos por já estarem indexados reporta `processed=1000` contra `discovered=40000` e parece travado. A distinção entre "pulado de graça" e "ainda não chegou lá" é a única coisa que torna a barra de progresso honesta.
- **`status` inclui `cancelled`.** §8.

> **Correção (§5.1 e §8).** O rascunho desta tabela **não tinha `cancel_requested`**, embora §8 já escrevesse `cancel_requested = true`. A coluna existe agora, e é uma flag separada em vez de um estado `cancelling` por um motivo: o worker é o único dono das transições a partir de `running`, e um estado colocaria a rota e o worker disputando a coluna `status` — além de liberar o índice único de §9 antes de o worker realmente soltar o disco.
>
> `discovery_complete` e `attempts` também não estavam no rascunho. O primeiro é a consequência de §15 (o denominador que só existe no fim); o segundo é o que limita o laço de recolocação do reaper (§9.1).

### 5.2 `indexing_job_scopes`

| coluna | tipo |
| --- | --- |
| `job_id` | `UUID` FK → `indexing_jobs.id` ON DELETE CASCADE |
| `relative_path` | `TEXT` NOT NULL |

Chave primária é o par, o que torna uma linha de escopo duplicada impossível.

Tabela filha em vez de um array ou JSON na linha do job. Um escopo é consultado (*"esta pasta está em algum job em execução?"*), e essa pergunta é um `WHERE` em uma tabela e um percurso em memória em qualquer das outras formas.

Zero linhas de escopo significa **o dispositivo inteiro**.

**Escopos sobrepostos são absorvidos na criação, no Domain.** Com `["2018/", "2018/junho"]`, o segundo está contido no primeiro e seria descoberto duas vezes — pagando duas leituras EXIF por arquivo e, muito pior, produzindo uma ordem de descoberta em que um caminho aparece duas vezes. O checkpoint de §10 é uma *posição* nessa ordem, e uma posição só significa alguma coisa numa sequência que visita cada arquivo uma vez. A resposta 202 devolve os escopos já normalizados, para que um cliente que pediu duas pastas veja que recebeu uma.

## 6. Quem executa: processo separado, sondando

Três formas foram consideradas.

| forma | por que não / por que sim |
| --- | --- |
| `BackgroundTasks` do FastAPI | Roda no processo da API. Um reinício mata o job em silêncio, o checkpoint perde o sentido, e o processo que deve responder `/health` em milissegundos passa a hospedar horas de inferência |
| Celery / RQ + Redis | Correto e desproporcional. Acrescenta um serviço que o usuário final precisa instalar e manter, em um produto local monousuário (§6.1) |
| **Processo executor separado, sondando o PostgreSQL** | **Escolhido** |

O executor sonda `indexing_jobs` em busca de `pending`, reivindica um com um `UPDATE` atômico (§9.2), executa, e escreve progresso.

Isso dá as três propriedades que as outras formas custam: a API permanece responsiva porque não faz o trabalho; matar o executor não perde nada porque o estado está no banco; e o checkpoint do `ARCHITECTURE.md` §16 passa a significar alguma coisa, porque existe um reinício de que se recuperar.

> **Correção (§6).** O rascunho dizia:
>
> > *"O worker é um segundo serviço no `docker-compose.yml` — mesma imagem, comando diferente."*
>
> **Isso não funciona neste código, e não funcionaria se funcionasse.** Três razões verificadas: não existe imagem do backend, porque não há `Dockerfile`; `WindowsVolumeIdentityProvider` levanta `VolumeIdentityError` fora de `win32`, de modo que um container Linux não consegue sequer identificar um disco; e mesmo com um *bind mount* um container vê um caminho montado, não o `\\?\Volume{GUID}\` de que a identidade da RFC-027 depende inteira.
>
> **O executor é um processo no host**, com o mesmo venv da API:
>
> ```
> python -m app.infrastructure.workers.job_runner
> ```
>
> O `docker-compose.yml` **não mudou**. O mesmo vale para a **API**: a validação "dispositivo conectado agora" (§7.1) chama `mounted_volumes()`, que só responde no host.
>
> **Isto não é provisório**, e está registrado em §14 como não-objetivo em vez de como dívida. O alvo de distribuição do projeto é processo nativo no Windows, empacotado como instalador, com só o PostgreSQL em container. Em container Linux sob Docker Desktop o sistema perderia a identidade de volume da RFC-027, a detecção de HD plugado a quente (bind mounts são fixos ao subir o container) e a abertura no Explorer da RFC-030, e a leitura de arquivos passaria pela fronteira da VM.
>
> A consequência para o código é o que torna a decisão barata de reverter: **nada fora do adaptador de volume depende de Windows.** A tabela de jobs, a reivindicação, o reaper e o checkpoint não sabem em que sistema rodam, de modo que um cenário de servidor Linux futuro é um adaptador novo, não uma reescrita.

### 6.1 Por que não há Redis, e o que a sondagem custa

O `docker-compose.yml` do RFC-002 tem um serviço: PostgreSQL. A fila deste sistema tem **um produtor, um consumidor e taxa de chegada medida em jobs por dia**. Uma tabela sondada atende isso com folga, e o banco já está lá, já tem transações, e já é o lugar onde o estado do job precisa estar de qualquer forma para sobreviver a um reinício.

Acrescentar Redis trocaria zero problemas atuais por um serviço a mais para o usuário final instalar.

O custo é declarado, e agora **medido** (`measure_queue_cancellation_resume.log`, intervalo de sondagem de 0,5 s, 12 jobs, um executor, chegadas em ponto uniformemente aleatório dentro do sono):

| | |
| --- | --- |
| latência de partida, mínima | 0,076 s |
| latência de partida, mediana | 0,259 s |
| latência de partida, média | 0,285 s |
| latência de partida, máxima | 0,531 s |

A média fica perto de metade do intervalo, que é o que se espera de uma chegada uniforme dentro do sono. **O máximo passa um pouco do intervalo em vez de ser limitado por ele**, e o excesso não é sono: um job criado logo depois de uma sondagem espera um intervalo inteiro e então paga o `UPDATE` de reivindicação e a visibilidade de `started_at` para quem lê. A frase original — *"latência de partida de até um intervalo"* — é otimista por alguns milissegundos; "um intervalo mais uma ida ao banco" é a versão honesta.

Custo de sondar ocioso, na mesma medição:

| | |
| --- | --- |
| reivindicação vazia | 4,10 ms |
| varredura do reaper | 1,46 ms |
| ciclo de trabalho, a 2 s de intervalo | **0,28%** |

Duas consultas indexadas contra resultado vazio. É esse o custo contra o qual o Redis foi pesado, e é por isso que a resposta foi uma tabela.

`settings.job_poll_interval` tem default **2,0 s**. Contra uma operação de minutos a horas (§2.1), tudo acima é ruído.

## 7. A API

### 7.1 Criar

```
POST /api/v1/jobs
{ "device_id": "…", "scopes": ["2018/", "2019/janeiro"] }

202 Accepted
{ "id": "…", "status": "pending", "device_id": "…", "scopes": ["2018", "2019/janeiro"], … }
```

**202, não 201.** 201 afirma que o recurso pedido existe e está pronto. O que existe é a *intenção* de indexar; o resultado não existe e vai levar minutos ou horas. 202 é a resposta que descreve isso, e a diferença é observável pelo cliente que precisa decidir se faz polling.

Validação, na Application: o dispositivo existe; está conectado agora (RFC-027 §7 — não se indexa um disco desplugado); os escopos são relativos, não escapam da raiz, e existem no disco.

| situação | erro de domínio | status |
| --- | --- | --- |
| job inexistente | `JobNotFoundError` | **404** |
| dispositivo inexistente | `DeviceNotFoundError` | **404** |
| dispositivo com job ativo | `DeviceBusyError` | **409** |
| cancelar job terminal | `IllegalJobTransitionError` | **409** |
| dispositivo desconectado | `DeviceNotConnectedError` | **409** |
| escopo absoluto, com `..`, UNC, ou que não existe | `InvalidJobScopeError` | **400** |

> **Correção (§7.1).** O rascunho dizia:
>
> > *"Cada falha é um erro de domínio mapeado para 400 pelo handler que o RFC-026 §8 já instalou."*
>
> 400 diz *o pedido precisa ser corrigido*. Para "disco desconectado" e "disco ocupado", nada no pedido precisa: os mesmos bytes são aceitos com o disco plugado, ou depois que o job atual terminar. É o **estado** que recusa, e é isso que 409 significa — o mesmo raciocínio que a RFC-030 §5.1 usa.
>
> O mapeamento não virou um registro de classes em Presentation. `DomainError` ganhou duas bases, `NotFoundError` e `ConflictError`, e `error_handlers.py` escolhe o status pela subclasse mais específica, com 400 como default. Um erro "não encontrado" futuro ganha 404 por herdar, e ninguém precisa lembrar de registrá-lo.
>
> **Nenhum erro que já respondia 400 deixou de responder.** `DeviceNotFoundError` e `DeviceNotConnectedError` são os dois únicos pré-existentes que mudaram, e isso é seguro por um motivo específico: até esta RFC **nenhuma rota conseguia levantá-los**. `test_error_handlers.py` enumera a hierarquia inteira em vez de uma lista mantida à mão, e é onde esse argumento vive.

**A violação do índice único vira `DeviceBusyError` dentro do repositório PostgreSQL**, capturando `IntegrityError` e conferindo o **nome da constraint** pelas diagnostics do driver — não o texto da mensagem, que é localizado e já foi reescrito entre versões, e não "qualquer `IntegrityError`", que reportaria uma FK ruim como disco ocupado. A rota não tem `try/except` (RFC-026 §8).

### 7.2 Acompanhar

```
GET /api/v1/jobs/{id}
{ "id": …, "status": "running", "discovered_files": 2431, "discovery_complete": false,
  "processed_images": 812, "skipped_images": 1200, "failed_images": 3,
  "cancel_requested": false, "attempts": 0, "started_at": …, "finished_at": null }
```

Polling, não WebSocket nem SSE. O cliente é uma UI local perguntando por um número que muda a cada poucos segundos; um canal persistente acrescentaria gerenciamento de conexão, reconexão e um caminho de código assíncrono na API para economizar requisições que custam milissegundos em localhost (§2.3 mede `/health` em 7,1 ms de mediana **sob carga**). É trabalho futuro se e quando o custo aparecer numa medição.

`GET /api/v1/jobs?device_id=&status=` lista, para a UI conseguir mostrar o histórico do disco. Filtro omitido significa **tudo**, nunca *nada* — a armadilha que o `device_id` da busca já teve que evitar.

**`discovered_files` é um contador crescente, não um denominador.** §15 admitia que ele *"só é conhecido depois da varredura"* enquanto §5.1 o chamava de *"o denominador do progresso"*; as duas frases não cabiam juntas. A descoberta é um gerador (RFC-021), então o total só existe no fim, e uma pré-varredura para contar dobraria a leitura do disco — que num HD mecânico frio é o custo dominante. A resposta carrega `discovery_complete` para que o cliente mostre *"varrendo N arquivos"* em vez de uma porcentagem inventada.

### 7.3 Progresso escrito por lote e por janela, heartbeat por tempo

Os contadores são atualizados uma vez por lote (`settings.batch_size`, default 8) e uma vez por janela de prefetch (`settings.metadata_prefetch_size`, default 512), junto da escrita dos embeddings que o RFC-024 §7 já faz em lote. Um `UPDATE` por imagem seria 40.000 escritas para acompanhar 40.000 imagens.

O ponto de extensão que tornou isso possível é uma porta de Domain, `IndexingObserver`, com três operações: *janela decidida*, *lote persistido*, *devo parar*. `IndexOrUpdateImagesUseCase.execute()` a recebe como parâmetro **opcional**, com um observador nulo por default — de modo que toda a suíte da RFC-024 continua passando sem uma linha editada. A porta não sabe o que é job, heartbeat ou banco; o adaptador de Infrastructure (`JobProgressObserver`) é que sabe.

A alternativa recusada foi passar o `IndexingJobRepository` para dentro do use case, o que faria a decisão incremental conhecer jobs — e é assim que "o CLI reindexa o que a UI pula" vira possível.

## 8. Cancelamento é cooperativo

Um usuário que dispara a indexação de 40.000 fotos e percebe que escolheu a pasta errada precisa poder parar. Sem isso, a única saída é matar o worker — que funciona, mas leva junto qualquer outro job e não deixa registro do que aconteceu.

```
POST /api/v1/jobs/{id}/cancel   →   202
```

A rota escreve `cancel_requested = true`. O worker verifica essa flag **entre lotes** e entre janelas — nunca dentro de uma passada do modelo — e, se estiver marcada, faz flush do que já inferiu, escreve o checkpoint, marca `cancelled`, e sai.

Cancelar um job `pending` é imediato: a rota vai direto para `cancelled`, com um `UPDATE` condicional ao status ainda ser `pending`. Se esse `UPDATE` não pegar linha nenhuma porque um executor reivindicou no meio, o caso cai para a flag — e não para a sobrescrita de um job em execução com um `cancelled`, que deixaria um worker indexando um job que o banco diz ter acabado.

Cancelar um `completed` é um erro de domínio (409), não um no-op silencioso: o chamador que pede isso está operando sobre uma suposição errada, e um 202 a confirmaria.

**Medido** (`measure_queue_cancellation_resume.log`, CLIP real em CPU, `batch_size` 8):

| | |
| --- | --- |
| atraso `cancel_requested` → `cancelled` | **1,01 s** (uma observação) |
| limite superior, a 0,426 s por imagem | ~3,4 s (um lote inteiro) |

**Uma amostra, e a dispersão é o ponto.** O atraso é o que sobra do lote em curso quando a flag chega, então vai de ~0 s (a flag chega logo antes de um flush) até um lote inteiro. O número acima é um sorteio de dentro desse intervalo, não o pior caso.

A espera compra algo: um lote de inferência já paga termina e é escrito. Abortar no meio de uma passada do modelo desperdiçaria isso sem nada em troca.

O trabalho já feito **permanece**. Um job cancelado a 60% deixa 60% do escopo indexado e pesquisável, e o próximo job sobre o mesmo escopo pula essas imagens pela decisão incremental do RFC-020. Cancelar não desfaz; para.

## 9. Um job por dispositivo

Dois jobs simultâneos sobre o mesmo disco competem pelos mesmos arquivos, duplicam inferência sobre a interseção dos escopos, e disputam a mesma cabeça de leitura de um HD mecânico — que é o recurso mais lento envolvido.

Imposto pelo banco, não por convenção:

```sql
CREATE UNIQUE INDEX uq_one_active_job_per_device
    ON indexing_jobs (device_id)
    WHERE status IN ('pending', 'running');
```

Um índice único parcial: o banco recusa o segundo job ativo do mesmo dispositivo, e a corrida entre duas requisições concorrentes é resolvida onde ela realmente acontece. Uma verificação em Python antes do `INSERT` seria um check-then-act — exatamente o padrão que o RFC-026 §10 já teve que corrigir no carregamento preguiçoso do modelo.

`pending` está dentro do predicado de propósito: um job na fila já reservou o disco, e mantém a reserva através da recolocação do reaper (§9.1) — que é o que impede um job interrompido a 60% de perder a vez para o que foi pedido depois.

`settings.worker_count` continua sem leitor no código, e agora com um motivo escrito em vez de um esquecimento: um executor é `python -m app.infrastructure.workers.job_runner`, e rodar dois é rodar o comando duas vezes. As peças que quebrariam sob concorrência são construídas e testadas para ela (o índice acima e a reivindicação de §9.2); o supervisor é o sistema operacional. Um supervisor que lesse esse número e desse *fork* seria uma segunda forma de iniciar o mesmo programa, e a primeira coisa a ficar fora de sincronia com o modo como ele é de fato implantado.

### 9.1 Jobs abandonados: o que acontece quando o worker morre

O índice único de §9 resolve concorrência entre dois workers vivos. Ele cria um problema novo se um worker **morrer**: a linha fica em `running` para sempre, porque nada além do próprio worker escreveria `completed`, `failed` ou `cancelled` nela. Com a cláusula `WHERE status IN ('pending', 'running')`, esse job fantasma **bloqueia permanentemente qualquer indexação futura daquele dispositivo** — até alguém editar a linha manualmente no banco.

Isso não é um cenário de ponta. Falta de energia, um reboot, ou o processo do executor sendo encerrado durante uma indexação de 5 horas são exatamente o tipo de evento que uma operação longa tem que sobreviver.

**O heartbeat é por tempo, não por evento.** O worker escreve `last_heartbeat_at` junto dos contadores — a mesma escrita, sem round-trip adicional — e, durante a varredura, no máximo a cada `settings.job_heartbeat_interval` segundos. Um processo separado (o mesmo `job_runner.py`, entre uma sondagem e outra) varre por jobs `running` cujo heartbeat parou.

> **Correção (§9.1).** O rascunho tinha duas falhas aqui, e a segunda é a que importa.
>
> **(a) O heartbeat "a cada lote" mata o job mais saudável que existe.** O rascunho dimensionou o timeout a partir de um lote — *"`batch_size` default 8 imagens a ~450 ms/imagem é ~3,6 s por lote; um timeout de alguns minutos dá folga generosa"*. Mas um lote não é o maior intervalo que um job sadio passa sem reportar. Três execuções ordinárias passam muito mais: uma **retomada**, onde tudo antes do checkpoint é descartado dentro da varredura sem nada chegar ao pipeline; um **re-scan de disco já indexado**, onde nenhum lote é jamais liberado — que é o caso *mais comum de todos*; e a **primeira janela**, porque `sorted(rglob(...))` materializa a lista inteira de arquivos antes do primeiro sair.
>
> **(b) O reaper marcava `failed`, e isso esvaziava §10.** O rascunho decidia `failed` num parágrafo e, no seguinte, dizia que o job *"voltou a `pending` (ou `failed`, exigindo recriação)"*; §14 dizia que `failed` não é retomado; e §17 exigia *"outro worker retoma do checkpoint"*. As quatro afirmações não fecham: se o reaper marca `failed` e o usuário recria, o job novo é outra linha, com outro id e checkpoint `NULL` — e o checkpoint do job morto nunca é lido por ninguém.
>
> **O reaper devolve um job `running` com heartbeat vencido para `pending`**, preservando `last_processed_relative_path`, e incrementa `attempts`. Ao atingir `settings.job_max_attempts` (default 3), marca `failed`. Um job vencido com `cancel_requested = true` vai para `cancelled`, não `pending` — culpar o sistema por uma parada que o usuário escolheu seria o registro errado.
>
> Isso torna §10 e a linha de §17 verdadeiras em vez de decorativas; mantém o dispositivo reservado para o job interrompido em vez de liberar a vaga para um job qualquer passar na frente do que já estava 60% feito; e `attempts` impede o laço infinito do caso ruim real — um arquivo que derruba o processo inteiro mataria o worker a cada retomada, para sempre.
>
> §14 continua verdadeiro como está escrito: `failed` não é retomado automaticamente. O que mudou é que um worker morto não produz `failed` na primeira vez.

O timeout saiu de uma distribuição medida, não de "alguns minutos" (`measure_heartbeat_gaps.log`; maior intervalo entre heartbeats, por cenário):

| cenário | maior intervalo |
| --- | --- |
| indexação nova, modelo real (48 imagens, CPU) | **2,81 s** |
| indexação nova, modelo falso (3.000 arquivos) | 1,70 s |
| retomada a partir de checkpoint | 1,02 s |
| re-scan, 100% pulado | 1,15 s |

| | |
| --- | --- |
| pior intervalo observado, máquina ociosa | 2,81 s |
| `settings.job_stale_timeout` escolhido | **120 s** |
| margem sobre o pior observado | **~43×** |

A margem é deliberadamente grande, e há três razões concretas para isso — nenhuma delas "por via das dúvidas".

**Primeira: contenção de CPU multiplica os intervalos.** Uma execução anterior deste mesmo script, feita sem querer *enquanto* `measure_throughput_and_api.py` saturava um núcleo, reportou 17,88 s de pior intervalo contra os 2,81 s da máquina ociosa — **seis vezes maior, só por disputa de CPU**. E um usuário que busca enquanto um job roda cria exatamente essa disputa (§2.3 a mede pelo outro lado). O número ocioso é o que está medido sob condições controladas; o número sob contenção é o que acontece em uso real.

**Segunda: a assimetria do erro.** Um timeout curto demais mata um job sadio — muito provavelmente o re-scan 100% pulado, a execução mais comum de todas — enquanto um longo demais só atrasa a liberação de um dispositivo depois de um crash real.

**Terceira: a primeira janela de um disco mecânico frio não está medida.** `sorted(rglob(...))` materializa a lista inteira de arquivos antes do primeiro heartbeat, e nenhum callback pode disparar durante isso. Num HD externo de 40.000 arquivos essa espera é tempo de busca, que esta máquina não consegue produzir — e é a maior lacuna que a margem cobre. Quem rodar em disco lento o bastante deve aumentar `job_stale_timeout`.

Custo de tempo de carga do modelo, medido no mesmo script: **4,50 s** a frio. Por isso o executor **carrega o modelo antes da primeira sondagem**, e não depois de reivindicar um job — um processo que ainda está carregando não tem lote nenhum para emitir heartbeat, e com um timeout curto poderia ser ceifado antes de ter feito qualquer coisa. `JobRunner` é dono dessa ordem, em vez de deixá-la para o composition root lembrar.

### 9.2 A reivindicação é uma instrução só

```sql
UPDATE indexing_jobs SET status = 'running', started_at = coalesce(started_at, :now),
                         last_heartbeat_at = :now
WHERE id = (
    SELECT id FROM indexing_jobs
    WHERE status = 'pending'
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id;
```

`FOR UPDATE SKIP LOCKED` não é decoração. Sem ele, dois executores sondando ao mesmo tempo escolhem o mesmo id; sob `READ COMMITTED` o perdedor reavalia o `WHERE`, não pega linha nenhuma, e volta a dormir **mesmo com outros jobs pendentes na fila**. Não é corrupção — é latência fantasma, e é invisível com um executor só, que é como todo teste roda a menos que alguém escreva um para isso. `test_indexing_job_concurrency.py` segura o lock numa sessão e exige que a outra leve o *segundo* job, com `lock_timeout` para que uma regressão falhe em dois segundos em vez de travar.

`started_at` só é escrito na primeira reivindicação; numa retomada ele se preserva, porque `created_at → started_at` é a latência de fila que §6.1 mede.

Essa é a única linha de SQL que repete uma regra do Domain — uma reivindicação atômica é uma instrução por definição, então a transição não pode ser calculada em Python a partir de uma linha lida antes. O teste de contrato fixa que o resultado é exatamente `job.claim(now)` aplicado à linha como ela estava, para que as duas não se afastem em silêncio.

### 9.3 Todo write é condicional

Não existe `save()` incondicional no repositório de jobs. Um job não tem um dono único — uma rota escreve `cancel_requested` enquanto um worker escreve contadores, e o reaper pode tirar um job abandonado do worker que ainda o segura. "Escreva isto só se a linha ainda estiver no estado de que eu decidi" é o contrato, e um método que escrevesse incondicionalmente seria o primeiro que todo chamador futuro pegaria por engano.

O heartbeat é o caso mais sutil: ele escreve **apenas** as colunas de progresso. Uma escrita de entidade inteira levaria a cópia de `cancel_requested` que o worker leu quando reivindicou o job — `False`, minutos atrás — por cima de um cancelamento que a rota registrou desde então. O job simplesmente nunca pararia, e nada em lugar nenhum pareceria errado. A mesma chamada devolve a linha fresca, que é como o worker descobre que foi cancelado sem uma segunda consulta.

## 10. O checkpoint

O `ARCHITECTURE.md` §16 define a retomada: continuar a partir do checkpoint em vez de recomeçar. Isso só é correto sob condições que o documento não declarava.

**A ordem de descoberta tem que ser estável entre execuções.** `FilesystemImageProvider.discover()` ordena. Isso provavelmente foi escrito para tornar os testes determinísticos; a partir desta RFC, **é requisito de correção da retomada**. Um `sorted()` removido em nome de desempenho passaria em todos os testes existentes e quebraria a retomada de forma silenciosa e dependente do sistema de arquivos. Um teste dedicado fixa a ordem, e a docstring do método passa a dizer por quê — a lição do RFC-014 §3.2 (um bug que passava em todos os testes) aplicada antes de o bug existir.

> **Correção (§10).** O rascunho tratava "continuar depois de X" como uma comparação qualquer. São dois erros, e ambos perdem fotos.
>
> **(a) A ordem é de caminho, e não de string.** Verificado no Python 3.12 do projeto:
>
> ```
> sorted(Path)  ->  a/x.jpg, a/Z.jpg, a b/x.jpg, B/y.jpg
> sorted(str)   ->  B/y.jpg, a b/x.jpg, a/Z.jpg, a/x.jpg
> ```
>
> `sorted()` sobre `Path` no Windows compara **por partes e sem caixa**; uma string ordena por código de caractere; e a collation do PostgreSQL daria uma terceira resposta. Um `WHERE relative_path > :checkpoint`, ou um `str(path) > checkpoint` em Python, pula e repete arquivos arbitrários — **e passa em todo teste cujos nomes sejam minúsculos e sem espaço**, que é todo teste escrito sem saber disto. A retomada compara `Path` com `Path`, com a mesma chave que ordenou a descoberta, e `test_discovery_order_is_stable.py` usa nomes com maiúscula, espaço e prefixo comum — mais um teste que falha se esses nomes deixarem de ser o caso difícil.
>
> **(b) O checkpoint não pode passar do que é durável.** O buffer de lote sobrevive **entre janelas**: uma imagem `EMBED` da janela 1 pode continuar não escrita enquanto linhas das janelas 2 e 3 já foram puladas e contabilizadas. Um checkpoint igual a "o último caminho visto" passaria por cima desse buffer, e um crash perderia exatamente as imagens que estavam nele — em silêncio, e para sempre, porque a varredura retomada nunca mais olha para elas.
>
> A regra: o checkpoint é o maior caminho `P` tal que **todo** arquivo descoberto até `P`, inclusive, foi persistido, pulado, ou registrado como falha. Na prática, o caminho imediatamente anterior ao plano mais antigo ainda em buffer. Errar é assimétrico e isto erra para o lado seguro: um checkpoint atrasado custa reprocessar alguns arquivos que a decisão incremental pula de graça; um adiantado perde fotografias.

**O que o checkpoint economiza, medido** (`measure_queue_cancellation_resume.log`, 1.200 arquivos já indexados, nenhum dos dois paga inferência):

| | |
| --- | --- |
| re-scan completo | 1,34 s (1.200 pulados) |
| retomada a partir de 50% | 0,88 s (599 chegaram ao pipeline) |
| economizado | **0,46 s (34%)** |

**Leia os números absolutos, não a porcentagem.** Um re-scan completo de 1.200 arquivos já indexados custa 1,3 s no total; o checkpoint pode economizar no máximo isso, e economiza uma fração, porque o que ele pula é a parte mais barata da varredura — `sorted(rglob(...))` ainda percorre a árvore inteira, e só o `stat` e a leitura de EXIF por arquivo são evitados.

Então a afirmação honesta é a que §10 fazia no fim e subvendia: **o checkpoint é uma otimização sobre um caminho que já era barato.** É exatamente por isso que perder um para um crash custa tempo e nunca dados — e por isso a regra de (b), de nunca passar do buffer, importa mais do que a economia que ele entrega.

**Não medido:** um HD mecânico externo frio, onde as leituras por arquivo que o checkpoint pula são tempo de busca em vez de trabalho de CPU em cache. Esse é justamente o caso em que a economia poderia ser grande, e é o que esta máquina não consegue produzir.

## 11. Nenhuma chave estrangeira para `images`

O RFC-027 §6.2 justificou sua própria prioridade assim: a reescrita de identidade é barata *enquanto nada referenciar `images.id`*, e antecipou que **esta** RFC poderia fechar essa janela ao criar `indexing_jobs` com FK para imagens.

Ela não cria. Um job referencia um dispositivo e um conjunto de escopos — nunca uma imagem individual. As contagens são agregados, e o checkpoint é um caminho, não um id.

Isso é uma escolha, não uma coincidência: uma tabela associativa job↔imagem daria um log perfeito de qual job indexou o quê, ao custo de 40.000 linhas por job e de uma FK sobre a tabela cuja PK o RFC-027 acabou de reescrever. O log não tem consumidor; o custo é imediato. `test_indexing_job_model.py` lê o metadado do SQLAlchemy e falha se uma chave dessas aparecer — inclusive uma coluna `image_id` sem FK declarada, que referenciaria `images` de todo jeito que importa.

**A janela do RFC-027 continua aberta depois desta RFC.** Ela fecha na RFC-030, que grava thumbnails endereçados por `images.id`.

## 12. O CLI continua, como cliente

`python -m app.infrastructure.workers.indexing_worker --root PATH` não foi removido nem duplicado. Ele passa a **criar um job** e a executá-lo, imprimindo o mesmo resumo de sempre.

Isso mantém um caminho de código, não dois. A alternativa — CLI indexando direto e API indexando por jobs — produziria dois lugares onde a semântica incremental pode divergir, e a divergência apareceria como "o CLI reindexa coisas que a UI pula", que é caro de diagnosticar. `discovered_candidates()` e `IndexOrUpdateImagesUseCase` são os mesmos objetos nos dois caminhos, o que é onde a semântica incremental de fato mora.

> **Correção (§12).** Duas frases do rascunho estavam erradas, e cada uma piorava o comando.
>
> **(a) *"Ele passa a criar um job com escopo vazio (dispositivo inteiro)."*** Mas `--root D:\fotos\2018` sempre indexou **só `fotos\2018`** — `root` é uma pasta, não um volume. Escopo vazio indexaria o disco inteiro: uma mudança de comportamento de horas, disfarçada de refatoração. O CLI converte `--root` em `root.relative_to(mount_point)`, e escopo vazio sai apenas quando `root` **é** o ponto de montagem. `test_cli_job_equivalence.py` compara o conjunto de linhas produzido pelo caminho novo contra o `IndexingWorker` de antes sobre uma **subpasta** — e tem um segundo teste que falha se a subpasta deixar de conter menos que o disco, porque sobre a raiz os dois significados coincidem e a comparação não provaria nada.
>
> **(b) *"Consequência declarada: o CLI passa a exigir o worker rodando."*** Isso transformaria `indexing_worker --root` num comando que fica esperando para sempre em silêncio quando ninguém subiu o executor. O CLI cria o job e o **executa no próprio processo**, chamando a mesma `JobRunner.run_job(job_id)` que o laço de sondagem chama, depois de reivindicar aquele id com o mesmo `UPDATE` atômico. Se um executor em paralelo o reivindicar primeiro, o CLI acompanha por polling e imprime o mesmo resumo. Um caminho de código, zero processos obrigatórios a mais — e a frase do rascunho sobre *"uma linha a mais no compose"* cai junto com §6.

## 13. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Indexar dentro da requisição HTTP | Ocupa um worker do threadpool por horas; é exatamente o que o RFC-026 proibiu (§2.3) |
| `BackgroundTasks` do FastAPI | Um reinício mata o job em silêncio e esvazia o sentido do checkpoint (§6) |
| Celery/RQ + Redis | Um serviço a mais para o usuário final, para uma fila de um produtor e um consumidor (§6.1) |
| O executor como serviço em container | Perde a identidade de volume, o hot-plug e a abertura no Explorer; não-objetivo declarado (§6, §14) |
| WebSocket/SSE para progresso | Gerenciamento de conexão para economizar requisições de milissegundos em localhost (§7.2) |
| `201 Created` na criação do job | Afirma que o resultado existe; ele vai levar minutos ou horas (§7.1) |
| Mandar toda falha para 400 | "Disco na gaveta" e "disco ocupado" são o mesmo pedido num momento ruim, não um pedido malformado (§7.1) |
| Uma tabela de classes de exceção em Presentation | A herança já carrega o status; um registro é a coisa que alguém esquece de atualizar (§7.1) |
| Escopos como array/JSON na linha do job | A pergunta feita sobre escopos é um `WHERE` (§5.2) |
| Contadores atualizados por imagem | Dobra o tráfego de escrita do job por 8 unidades de granularidade (§7.3) |
| Passar o repositório de jobs para dentro do use case | Faria a decisão incremental conhecer jobs, que é como as duas portas divergem (§7.3) |
| Heartbeat escrito a cada lote | Um re-scan 100% pulado nunca libera um lote, e seria ceifado por estar saudável (§9.1) |
| Escrever a entidade inteira no heartbeat | Sobrescreveria `cancel_requested` com a cópia velha do worker, e o job nunca pararia (§9.3) |
| Reivindicar com `SELECT` e depois `UPDATE` | Dois executores escolhem a mesma linha e um volta a dormir com fila cheia (§9.2) |
| Reaper marcando `failed` de imediato | O checkpoint fica sem leitor, e o disco perde a vez do job que estava a 60% (§9.1) |
| Cancelamento abortando o lote em curso | Descarta inferência já paga sem nada em troca (§8) |
| Cancelar desfazendo o trabalho feito | Joga fora embeddings corretos; o incremental já os reaproveita (§8) |
| Verificar job ativo em Python antes do `INSERT` | Check-then-act, o mesmo padrão que o RFC-026 §10 corrigiu (§9) |
| Comparar checkpoint como string | Pula e repete arquivos, e passa em todo teste de nome minúsculo (§10) |
| Checkpoint no último caminho visto | Passa por cima do buffer de lote e perde fotos num crash (§10) |
| Pré-varredura para ter um denominador | Dobra a leitura do disco, que é o custo dominante num HD frio (§7.2) |
| Tabela associativa job↔imagem | 40.000 linhas por job para um log sem consumidor, e uma FK sobre a PK recém-reescrita (§11) |
| CLI indexando direto, API por jobs | Dois lugares onde a semântica incremental pode divergir (§12) |
| CLI exigindo um executor rodando | Um comando que espera para sempre em silêncio quando ninguém o subiu (§12) |
| `--root` virando escopo vazio | Indexaria o disco inteiro; horas de mudança disfarçadas de refatoração (§12) |

## 14. Não-objetivos

- Agendamento (`cron`, "indexar toda noite")
- Monitoramento automático de pastas (`watchdog`, `ReadDirectoryChangesW`) — `ARCHITECTURE.md` §23, e depende disto, não o contrário
- Prioridade entre jobs, reordenação de fila, preempção
- Retomada automática de jobs `failed` — o usuário recria. Um job abandonado por um worker morto é outra coisa, e volta à fila sozinho (§9.1)
- Progresso por WebSocket (§7.2)
- Múltiplos workers sobre o **mesmo** dispositivo (§9)
- Indexação disparada por resultado de busca ("indexe isto para mim")
- Qualquer mudança no pipeline de embedding, no modelo ou no ranking
- A UI em si — esta RFC entrega a API que a tela do Figma exigiria
- **Rodar a API ou o executor em container.** Não é dívida a pagar: é uma consequência do alvo de distribuição — processo nativo no Windows, empacotado como instalador, com só o PostgreSQL em container. Em container Linux o sistema perderia a identidade de volume da RFC-027, a detecção de HD plugado a quente e a abertura no Explorer da RFC-030 (§6)

## 15. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **O checkpoint depende de `sorted()` em `discover()`** (§10) | Requisito de correção, com teste e docstring dedicados |
| **O checkpoint depende de comparar caminhos como caminhos** (§10) | Uma função só faz a comparação, e o teste usa os nomes em que string e caminho discordam |
| **Sondagem tem latência de partida** (§6.1) | Medida: mediana 0,26 s e máxima 0,53 s a 0,5 s de intervalo. Ruído contra uma operação de 14 minutos |
| **Um disco desplugado no meio de um job** | O job falha com `error_message`; o trabalho feito permanece e o próximo job o pula. **Verificado ao terminar**, e não presumido: `discover()` retorna em silêncio quando a raiz some, então sem essa conferência o job seria marcado `completed` com metade do escopo — o pior desfecho possível, porque parece sucesso |
| **O timeout do reaper** (§9.1) | Medido: pior intervalo 2,81 s numa máquina ociosa e 17,88 s sob contenção de CPU; timeout 120 s, margem ~43× sobre o primeiro |
| **Varredura fria de HD mecânico externo** | **Não medida**, e é a lacuna que a margem do timeout cobre. Windows não oferece forma sem privilégio de esvaziar o cache de arquivos, e inventar um tempo de busca seria dedução (a mesma limitação que a RFC-028 declarou) |
| **`discovered_files` só é conhecido depois da varredura** | Resolvido em vez de tolerado: o contador é crescente e a resposta carrega `discovery_complete`, então a UI mostra "varrendo N arquivos" e não `0%` (§7.2) |
| **Um lote em andamento atrasa o cancelamento** (§8) | Medido: 1,01 s numa observação, com ~3,4 s de limite superior a `batch_size` 8 |
| **Busca fica mais lenta com um job rodando** | Medido (§2.3): mediana 159,6 → 195,5 ms, p95 168,8 → 516,1 ms. É disputa de CPU e não event loop bloqueado; processos separados não protegem contra isso, e o documento não finge que protegem |
| Jobs acumulam no histórico sem política de retenção | Trabalho futuro; linhas pequenas, sem urgência |

Trabalho futuro: monitoramento automático de pastas, que é o passo natural depois de jobs existirem; retenção de histórico; e prioridade de fila, se algum dia houver mais de um consumidor por dispositivo.

## 16. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/entities/indexing_job.py` | `IndexingJob`, `JobStatus`, `JobEvent` e a tabela de transições legais |
| `backend/app/domain/value_objects/job_id.py` | `JobId` |
| `backend/app/domain/value_objects/job_scope.py` | `JobScope`, validação e normalização de sobreposição |
| `backend/app/domain/value_objects/indexing_progress.py` | Os quatro contadores que uma barra de progresso precisa |
| `backend/app/domain/repositories/indexing_job_repository.py` | A porta, com writes condicionais (§9.3) |
| `backend/app/domain/services/device_locator.py` | "Onde está este disco agora", sem importar Infrastructure |
| `backend/app/domain/services/indexing_observer.py` | O ponto de extensão de §7.3 |
| `backend/app/domain/exceptions/job_errors.py` | Erros de job; `NotFoundError`/`ConflictError` em `domain_error.py` |
| `backend/app/application/use_cases/create_indexing_job.py` | §7.1 |
| `backend/app/application/use_cases/cancel_indexing_job.py` | §8 |
| `backend/app/application/use_cases/get_indexing_job.py` | §7.2 |
| `backend/app/application/use_cases/list_indexing_jobs.py` | §7.2 |
| `backend/app/infrastructure/database/models/indexing_job_model.py` | As duas tabelas |
| `backend/app/infrastructure/persistence/postgres_indexing_job_repository.py` | Reivindicação atômica (§9.2) e tradução de `IntegrityError` (§7.1) |
| `backend/app/infrastructure/persistence/in_memory_indexing_job_repository.py` | O duplo, que **emula** unicidade e reivindicação |
| `backend/app/infrastructure/filesystem/mounted_device_locator.py` | O adaptador sobre `mounted_volumes()` |
| `backend/app/infrastructure/workers/job_progress_observer.py` | Heartbeat por tempo e leitura do cancelamento |
| `backend/app/infrastructure/workers/job_runner.py` | O laço de sondagem, o reaper e `run_job()` |
| `backend/app/presentation/api/v1/routers/jobs.py` | §7 |
| `backend/app/presentation/schemas/job_schema.py` | |
| `backend/alembic/versions/e7a2c9b41f30_create_indexing_jobs.py` | Inclui o índice único parcial de §9 |
| `backend/tests/domain/test_indexing_job_transitions.py` | A máquina de estados, na grade `(estado, evento)` inteira |
| `backend/tests/domain/test_job_scope.py` | Validação e absorção de escopos |
| `backend/tests/infrastructure/persistence/test_indexing_job_repository_contract.py` | Um contrato, duas implementações |
| `backend/tests/infrastructure/persistence/test_indexing_job_concurrency.py` | `SKIP LOCKED` e o índice único, em conexões reais |
| `backend/tests/infrastructure/test_indexing_job_model.py` | Invariantes de esquema, inclusive a ausência de FK para `images` |
| `backend/tests/infrastructure/filesystem/test_discovery_order_is_stable.py` | §10 |
| `backend/tests/application/test_indexing_observer.py` | §7.3, e a retomada depois de um crash com buffer cheio |
| `backend/tests/application/test_indexing_job_use_cases.py` | §7 e §8 |
| `backend/tests/infrastructure/workers/test_job_runner.py` | Executor e reaper |
| `backend/tests/infrastructure/workers/test_cli_job_equivalence.py` | §12 |
| `backend/tests/presentation/test_jobs_api.py` | §7 |
| `backend/tests/presentation/test_error_handlers.py` | §7.1 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/workers/indexing_worker.py` | `main()` cria um job escopado e o executa aqui (§12) |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | Escopos, retomada, chave de ordenação e o heartbeat de varredura (§9.1, §10) |
| `backend/app/application/use_cases/index_or_update_images.py` | `IndexingObserver` opcional, `DurableFrontier`, `summary.stopped` (§7.3, §10) |
| `backend/app/domain/services/embedding_model_port.py` | `warm_up()`, concreto e no-op por default (§9.1) |
| `backend/app/domain/exceptions/` | `NotFoundError`, `ConflictError`, e `device_errors` re-baseados (§7.1) |
| `backend/app/presentation/error_handlers.py` | Status pela subclasse mais específica (§7.1) |
| `backend/app/presentation/api/v1/__init__.py` | Registra o router de jobs |
| `backend/app/presentation/dependencies/__init__.py` | Provedores de repositório de jobs, locator e os quatro use cases |
| `backend/app/infrastructure/config/settings.py` | Os quatro parâmetros de §6.1 e §9.1; `worker_count` ganha o motivo de não ter leitor |
| `ARCHITECTURE.md` | §15 `IndexingJobs` atualizado; §16 ganha "Resuming an Interrupted Job" |
| `AI_Context.md` | O que "indexação nunca pela FastAPI" significa agora, e o executor |
| `docker-compose.yml` | **Não mudou** (§6) |

## 17. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | **1245 passed**, 58 deselected (linha de base antes desta RFC: 917) |
| `pytest -m slow` | **58 passed** |
| `black --check .` / `ruff check .` | limpos |
| `mypy` | **0 erros** em 189 arquivos |
| `alembic heads` | `e7a2c9b41f30`, head único |
| `alembic downgrade`/`upgrade` | exercitados de verdade contra o PostgreSQL; `\d indexing_jobs` confere o índice parcial |
| Duas requisições concorrentes ao mesmo dispositivo | uma cria, a outra recebe 409 — em duas threads reais pelo HTTP, e em duas conexões reais no nível do repositório |
| Duas reivindicações concorrentes | pegam jobs diferentes; um teste determinístico segura o lock e exige que a outra leve o segundo job, e falha em 2 s (sem `SKIP LOCKED`) em vez de travar |
| Job `running` sem heartbeat além do timeout | volta a `pending` com o checkpoint preservado; com `attempts` esgotado vira `failed`; com cancelamento pedido vira `cancelled` |
| Dispositivo libera para um job novo depois do reaper agir | sim |
| Job que voltou a dar sinal entre a consulta e o veredito | **não é roubado** — o write condicional de §9.3 recusa |
| Re-scan 100% pulado, com um reaper varrendo o tempo todo | sobrevive e completa; o teste falha se o reaper não tiver de fato olhado para ele |
| Crash com o buffer de lote cheio | a retomada a partir do checkpoint reportado não perde nenhuma imagem, e nenhuma é indexada duas vezes. Um teste-irmão prova que o buffer estava mesmo cheio, para que o primeiro não passe por acidente |
| Ordem de descoberta estável, com maiúsculas, espaço e prefixo comum | sim, e um teste falha se esses nomes deixarem de ser o caso em que string e caminho discordam |
| Cancelamento observado entre lotes | sim; `summary.stopped`, lote em curso completo, trabalho feito preservado |
| Disco desconectado no meio | `failed` com mensagem, checkpoint preservado — **nunca `completed`** |
| `--root` numa subpasta ≡ `IndexingWorker` de antes | mesmo conjunto de `(ImageId, relative_path)`, sobre uma subpasta e não sobre a raiz |
| Escopo com `..`, absoluto, UNC, ou inexistente | 400 |
| Nenhuma FK para `images` | sim, lido do metadado do SQLAlchemy |
| Todo erro de domínio pré-existente continua 400 | sim, com as duas exceções de §7.1 nomeadas e justificadas |
| `test_index_or_update_images.py` passa sem edição | sim |
| Latência de partida por sondagem | mediana 0,26 s, máxima 0,53 s, a 0,5 s de intervalo (§6.1) |
| Custo de sondar ocioso | 0,28% de ciclo de trabalho a 2 s (§6.1) |
| Atraso de cancelamento | 1,01 s numa observação, ~3,4 s de limite (§8) |
| Maior intervalo entre heartbeats | 2,81 s (ocioso), 17,88 s (sob contenção); timeout 120 s (§9.1) |
| Quanto o checkpoint economiza | 34% de um re-scan de 1,3 s (§10) |
| Um job real de 2.000 fotos, do `POST` ao `completed` | 851 s (14,2 min), 2,35 img/s, 0 falhas (§2.1) |
| API responde `/health` durante um job ativo | mediana 7,1 ms sob carga contra 7,4 ms ocioso (§2.3) |
