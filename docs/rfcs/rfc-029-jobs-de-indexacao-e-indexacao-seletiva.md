# RFC-029 — Jobs de Indexação e Indexação Seletiva

**Status:** Proposto
**Depende de:** RFC-021 (worker), RFC-024 (pipeline), RFC-026 (camada HTTP), RFC-027 (dispositivos), RFC-028 (data de captura)
**Migration:** sim — `indexing_jobs`, `indexing_job_scopes`
**Medição:** `experiments/rfc-029-indexing-jobs/` — `TBM`

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Nada neste documento foi medido ainda.

---

## 1. Contexto

O requisito chegou junto de uma suposição, e a suposição merece ser desfeita antes de qualquer decisão:

> *"Sempre achei que a indexação incremental automaticamente significava indexação via UI — o usuário escolhe que pastas indexar."*

São duas propriedades diferentes, e o sistema hoje tem exatamente uma delas.

| | o que é | estado |
| --- | --- | --- |
| **Incremental** (RFC-020, RFC-024) | não recomputar o que não mudou | **existe** — `plan_indexing()`, escada de custo, hash SHA-256 |
| **Seletiva** | o usuário escolhe *o quê* e *quando* | **não existe** |

Elas são ortogonais. O que roda hoje é incremental e **não** seletiva: `python -m app.infrastructure.workers.indexing_worker --root PATH` varre tudo sob a raiz, pula o que não mudou, e não oferece nenhuma forma de dizer *"só esta pasta"* ou *"pare"*.

A junção das duas é o que o requisito realmente pede, e a justificativa dada para ela é a melhor parte do pedido:

> *"Ele quer achar uma foto antiga que sabe que foi de 2018 — então, se não estão indexadas, ele seleciona apenas as pastas desse ano para indexar."*

## 2. Problema

### 2.1 Indexar tudo é uma barreira de horas antes do primeiro resultado

O RFC-024 mediu 2,2 imagens/segundo em CPU. Contra o acervo que o `ARCHITECTURE.md` §2 descreve:

| acervo | custo de indexação completa (CPU, medido pelo RFC-024) |
| --- | --- |
| 40.000 fotos (um HD) | **~5 h** |
| 100.000 fotos (a meta de §22) | **~12,6 h** |

Isso é o custo para o **primeiro** resultado útil, porque nada é pesquisável antes de existir embedding. Um usuário que quer testar se a ferramenta serve para ele paga meio dia antes de digitar a primeira consulta.

Com escopo seletivo, o mesmo usuário indexa `HD2/2018/` — talvez 2.000 fotos, `TBM` minutos — e busca. A ferramenta passa a ser avaliável no mesmo dia em que é instalada. Isso não é conveniência: é a diferença entre um sistema demonstrável e um que precisa de uma noite de preparo para mostrar qualquer coisa.

### 2.2 O CLI não pode ser a única porta

`--root PATH` exige linha de comando, um caminho digitado corretamente, e um terminal aberto durante horas. O mockup do Figma que originou esta discussão mostra a alternativa: uma lista de pastas com estado `Indexed` / `Not indexed` e um botão por pasta.

### 2.3 O que o RFC-026 realmente proibiu

O RFC-026 §3 listou:

> *"Indexação por HTTP: **Não neste RFC.** O CLI do worker continua sendo o único ponto de entrada."*

Isso é frequentemente lido como *"indexação não deve ser exposta por HTTP"*. Não é o que o documento diz nem o que o raciocínio dele sustenta. O RFC-026 §9 decidiu que o endpoint de busca é `def` e não `async def` porque *"tudo abaixo desta linha bloqueia"* — 90 ms de inferência já foram considerados suficientes para justificar um threadpool.

Uma rota que rodasse indexação **dentro da requisição** ocuparia um worker do threadpool por cinco horas e derrubaria a responsividade da API inteira, `/health` incluído. O que o RFC-026 proibiu foi essa forma, e a proibição continua correta. Este RFC não a viola: entrega a forma assíncrona, que é a única que sempre foi compatível.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Nova tabela | `indexing_jobs` — o desenho do `ARCHITECTURE.md` §15, ajustado (§5) |
| Nova tabela | `indexing_job_scopes` — as pastas de um job (§5.2) |
| Quem executa | **Processo worker separado**, sondando a tabela (§6) |
| Criar job | `POST /api/v1/jobs` → **202 Accepted**, corpo com o id (§7) |
| Acompanhar | `GET /api/v1/jobs/{id}` — polling, sem WebSocket (§7.2) |
| Cancelar | `POST /api/v1/jobs/{id}/cancel` — cooperativo (§8) |
| Estados | `pending → running → completed \| failed \| cancelled` (§5.1) |
| Concorrência | **Um job em execução por dispositivo**, imposto pelo banco (§9) |
| Job órfão (worker morreu) | Heartbeat por lote + reaper por timeout, libera o dispositivo sozinho (§9.1) |
| Retomada | Checkpoint por caminho, válido porque a descoberta é ordenada (§10) |
| FK para `images` | **Nenhuma** (§11) |
| Nova infraestrutura (Redis, Celery) | Não (§6.1) |
| O CLI | **Permanece**, e passa a ser um cliente da mesma máquina de jobs (§12) |

## 4. Arquitetura

A tabela de camadas do RFC-026 §4, com a linha que muda:

| camada | conhece | não conhece |
| --- | --- | --- |
| **Presentation** | que um job é criado e consultado por HTTP, que 202 significa aceito | que existe um processo worker, como um lote é montado |
| **Application** | o que é um escopo válido, que estados sucedem quais, que um job pode ser cancelado | a ordem de varredura, SQL, o modelo |
| **Domain** | `IndexingJob`, `JobStatus`, `JobScope`, e as transições legais | `rglob`, sondagem, FastAPI |
| **Infrastructure (workers)** | a sondagem, o laço, o checkpoint, o cancelamento cooperativo | por que este job foi criado |

O ponto estrutural: **a rota não indexa nada.** Ela escreve uma linha. Toda a diferença entre este RFC e a coisa que o RFC-026 proibiu está aí.

## 5. Esquema

### 5.1 `indexing_jobs`

| coluna | tipo | nota |
| --- | --- | --- |
| `id` | `UUID` PK | `uuid4` — um job é um evento, não um conteúdo derivável |
| `device_id` | `UUID` FK → `devices.id` | RFC-027 |
| `status` | `TEXT` NOT NULL | `pending`/`running`/`completed`/`failed`/`cancelled` |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |
| `started_at` / `finished_at` | `TIMESTAMPTZ` NULL | |
| `discovered_files` | `INT` NOT NULL default 0 | o denominador do progresso |
| `processed_images` | `INT` NOT NULL default 0 | |
| `skipped_images` | `INT` NOT NULL default 0 | pulados pela decisão incremental |
| `failed_images` | `INT` NOT NULL default 0 | |
| `last_processed_relative_path` | `TEXT` NULL | o checkpoint (§10) |
| `error_message` | `TEXT` NULL | preenchido apenas em `failed` |
| `last_heartbeat_at` | `TIMESTAMPTZ` NULL | a prova de que alguém ainda está trabalhando neste job (§9.1) |

Diferenças em relação ao `ARCHITECTURE.md` §15, e por quê:

- **`device_id` em vez de `collection_id`.** `Collection` continua placeholder (RFC-027 §10); dispositivo é o que existe.
- **`last_processed_relative_path` em vez de `last_processed_path`.** O RFC-027 removeu o caminho absoluto do banco; um checkpoint absoluto seria a mesma instabilidade de novo.
- **`skipped_images` acrescentado.** Sem ele, um job que pulou 39.000 de 40.000 arquivos por já estarem indexados reporta `processed=1000` contra `discovered=40000` e parece travado. A distinção entre "pulado de graça" e "ainda não chegou lá" é a única coisa que torna a barra de progresso honesta.
- **`status` inclui `cancelled`.** §8.

### 5.2 `indexing_job_scopes`

| coluna | tipo |
| --- | --- |
| `job_id` | `UUID` FK → `indexing_jobs.id` ON DELETE CASCADE |
| `relative_path` | `TEXT` NOT NULL |

Tabela filha em vez de um array ou JSON na linha do job. Um escopo é consultado (*"esta pasta está em algum job em execução?"*), e essa pergunta é um `WHERE` em uma tabela e um percurso em memória em qualquer das outras formas.

Zero linhas de escopo significa **o dispositivo inteiro** — o que o CLI de hoje faz, expresso na mesma estrutura (§12).

## 6. Quem executa: processo separado, sondando

Três formas foram consideradas.

| forma | por que não / por que sim |
| --- | --- |
| `BackgroundTasks` do FastAPI | Roda no processo da API. Um reinício mata o job em silêncio, o checkpoint perde o sentido, e o processo que deve responder `/health` em milissegundos passa a hospedar horas de inferência |
| Celery / RQ + Redis | Correto e desproporcional. Acrescenta um serviço que o usuário final precisa instalar e manter, em um produto local monousuário (§6.1) |
| **Processo worker separado, sondando o PostgreSQL** | **Escolhido** |

O worker é um segundo serviço no `docker-compose.yml` — mesma imagem, comando diferente. Ele sonda `indexing_jobs` em busca de `pending`, reivindica um com `UPDATE ... WHERE status = 'pending' ... RETURNING` (atômico, sem corrida), executa, e escreve progresso.

Isso dá de graça as três propriedades que as outras formas custam: a API permanece responsiva porque não faz o trabalho; matar o worker não perde nada porque o estado está no banco; e o checkpoint do `ARCHITECTURE.md` §16 passa a significar alguma coisa, porque existe um reinício de que se recuperar.

### 6.1 Por que não há Redis

O `docker-compose.yml` do RFC-002 tem um serviço: PostgreSQL. A fila deste sistema tem **um produtor, um consumidor e taxa de chegada medida em jobs por dia**. Uma tabela sondada a cada `TBM` segundos atende isso com folga, e o banco já está lá, já tem transações, e já é o lugar onde o estado do job precisa estar de qualquer forma para sobreviver a um reinício.

Acrescentar Redis trocaria zero problemas atuais por um serviço a mais para o usuário final instalar. O RFC-025 §10.1 recusou um banco de testes dedicado com o mesmo tipo de argumento — resposta certa, RFC errado. Aqui é resposta errada para a escala.

O custo é declarado: sondagem tem latência de partida de até um intervalo, e um job criado logo depois de uma sondagem espera. Para uma operação que dura minutos ou horas, `TBM` segundos de latência de partida é ruído.

## 7. A API

### 7.1 Criar

```
POST /api/v1/jobs
{ "device_id": "…", "scopes": ["2018/", "2019/janeiro"] }

202 Accepted
{ "id": "…", "status": "pending", "device_id": "…", "scopes": [...] }
```

**202, não 201.** 201 afirma que o recurso pedido existe e está pronto. O que existe é a *intenção* de indexar; o resultado não existe e vai levar horas. 202 é a resposta que descreve isso, e a diferença é observável pelo cliente que precisa decidir se faz polling.

Validação, na Application: o dispositivo existe; está conectado agora (RFC-027 §7 — não se indexa um disco desplugado); os escopos são relativos, não escapam da raiz (`..`), e existem no disco. Cada falha é um erro de domínio mapeado para 400 pelo handler que o RFC-026 §8 já instalou.

### 7.2 Acompanhar

```
GET /api/v1/jobs/{id}
{ "id": …, "status": "running", "discovered_files": 2431,
  "processed_images": 812, "skipped_images": 1200, "failed_images": 3,
  "started_at": …, "finished_at": null }
```

Polling, não WebSocket nem SSE. O cliente é uma UI local perguntando por um número que muda a cada poucos segundos; um canal persistente acrescentaria gerenciamento de conexão, reconexão e um caminho de código assíncrono na API para economizar requisições que custam `TBM` ms cada em localhost. É trabalho futuro se e quando o custo aparecer numa medição.

`GET /api/v1/jobs?device_id=&status=` lista, para a UI conseguir mostrar o histórico do disco.

### 7.3 Progresso escrito por lote, não por imagem

Os contadores são atualizados uma vez por lote (`settings.batch_size`, default 8), junto da escrita dos embeddings que o RFC-024 §7 já faz em lote. Um `UPDATE` por imagem seria 40.000 escritas para acompanhar 40.000 imagens — dobrando o tráfego de escrita do job para melhorar a granularidade de uma barra de progresso em 8 unidades.

## 8. Cancelamento é cooperativo

Um usuário que dispara a indexação de 40.000 fotos e percebe que escolheu a pasta errada precisa poder parar. Sem isso, a única saída é matar o worker — que funciona, mas leva junto qualquer outro job e não deixa registro do que aconteceu.

```
POST /api/v1/jobs/{id}/cancel   →   202
```

A rota escreve `cancel_requested = true`. O worker verifica essa flag **entre lotes** e, se estiver marcada, escreve o checkpoint, marca `cancelled`, e sai. Um lote em andamento termina — abortar no meio de uma passada do modelo desperdiçaria a inferência já paga sem nada em troca.

Cancelar um job `pending` é imediato. Cancelar um `completed` é um erro de domínio, não um no-op silencioso: o chamador que pede isso está operando sobre uma suposição errada, e merece saber.

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

Dispositivos diferentes rodam em paralelo se houver mais de um worker (`settings.worker_count`, que existe e nunca teve consumidor).

### 9.1 Jobs abandonados: o que acontece quando o worker morre

O índice único de §9 resolve concorrência entre dois workers vivos. Ele cria um problema novo se um worker **morrer**: a linha fica em `running` para sempre, porque nada além do próprio worker escreveria `completed`, `failed` ou `cancelled` nela. Com a cláusula `WHERE status IN ('pending', 'running')` do índice, esse job fantasma **bloqueia permanentemente qualquer indexação futura daquele dispositivo** — o mesmo mecanismo que impede duas indexações concorrentes passa a impedir todas, incluindo a legítima, até alguém editar a linha manualmente no banco.

Isso não é um cenário de ponta. `docker kill`, falta de energia, ou o processo do worker sendo encerrado durante uma indexação de 5 horas são exatamente o tipo de evento que uma operação longa tem que sobreviver — e a retomada por checkpoint (§10) só ajuda quando **alguém percebe** que precisa reiniciar o worker. Nada neste RFC, até este ponto, detecta que ninguém mais está processando aquele job.

**Decisão:** o worker escreve `last_heartbeat_at = now()` a cada lote — a mesma escrita que já atualiza `processed_images` em §7.3, sem round-trip adicional. Um processo separado (o mesmo `job_runner.py`, entre uma sondagem e outra) varre por jobs `running` cujo `last_heartbeat_at` está há mais de `settings.job_stale_timeout` sem se mover, e os marca `failed` com `error_message = "heartbeat expirado; worker provavelmente morreu"`. Isso libera o índice único do §9 e o dispositivo volta a aceitar jobs novos, sem intervenção manual.

O timeout precisa ser maior que o tempo de um lote — senão um lote lento derruba um job saudável. `settings.batch_size` default 8 imagens a ~450 ms/imagem (RFC-024) é ~3,6 s por lote; um timeout de alguns minutos dá folga generosa sem deixar um dispositivo travado por horas depois de um crash real. O número exato é medição, não palpite: `TBM`.

Isso também redefine o que "worker reiniciado, retoma do checkpoint" (§17) significa: não é mais "o mesmo processo sobrevive e continua", é "o job voltou a `pending` (ou `failed`, exigindo recriação) depois do reaper agir, e um worker qualquer o pega de onde o checkpoint parou." O comportamento observável para o usuário — indexação retomada sem reprocessar o que já foi feito — é o mesmo; o mecanismo por trás é mais honesto sobre não depender de um processo específico voltar a existir.

## 10. O checkpoint depende de a descoberta ser ordenada

O `ARCHITECTURE.md` §16 define a retomada: continuar a partir de `last_processed_path` em vez de recomeçar. Isso só é correto sob uma condição que o documento não declara — **a ordem de descoberta tem que ser estável entre execuções.** Se a segunda varredura produzir os arquivos em outra ordem, "continuar depois de X" pula arquivos arbitrários.

A condição vale hoje, por acidente feliz:

```python
for path in sorted(self._root.rglob("*")):
```

`FilesystemImageProvider.discover()` ordena. Isso provavelmente foi escrito para tornar os testes determinísticos; a partir deste RFC, **é requisito de correção da retomada**. Um `sorted()` removido em nome de desempenho passaria em todos os testes existentes e quebraria a retomada de forma silenciosa e dependente do sistema de arquivos.

Portanto: um teste dedicado fixa que `discover()` produz ordem estável, e a docstring do método passa a dizer por quê. Essa é a lição do RFC-014 §3.2 — um bug que passava em todos os testes — aplicada antes de o bug existir.

A retomada é, de qualquer forma, uma otimização e não uma garantia: a decisão incremental do RFC-020 já torna um recomeço completo barato (`stat` + comparação, sem inferência) para tudo que foi processado. O checkpoint economiza a varredura, não a inferência.

## 11. Nenhuma chave estrangeira para `images`

O RFC-027 §6.2 justificou sua própria prioridade assim: a reescrita de identidade é barata *enquanto nada referenciar `images.id`*, e antecipou que **este** RFC poderia fechar essa janela ao criar `indexing_jobs` com FK para imagens.

Ele não cria. Um job referencia um dispositivo e um conjunto de escopos — nunca uma imagem individual. As contagens são agregados, e o checkpoint é um caminho, não um id.

Isso é uma escolha, não uma coincidência: uma tabela associativa job↔imagem daria um log perfeito de qual job indexou o quê, ao custo de 40.000 linhas por job e de uma FK sobre a tabela cuja PK o RFC-027 acabou de reescrever. O log não tem consumidor; o custo é imediato.

**A janela do RFC-027 continua aberta depois deste RFC.** Ela fecha no RFC-030, que grava thumbnails endereçados por `images.id`.

## 12. O CLI continua, como cliente

`python -m app.infrastructure.workers.indexing_worker --root PATH` não é removido nem duplicado. Ele passa a **criar um job** com escopo vazio (dispositivo inteiro) e acompanhá-lo até o fim, imprimindo o mesmo resumo de sempre.

Isso mantém um caminho de código, não dois. A alternativa — CLI indexando direto e API indexando por jobs — produziria dois lugares onde a semântica incremental pode divergir, e a divergência apareceria como "o CLI reindexa coisas que a UI pula", que é caro de diagnosticar.

Consequência declarada: o CLI passa a exigir o worker rodando. Para um projeto que já entrega `docker-compose up`, isso é uma linha a mais no compose, não um requisito novo de operação.

## 13. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Indexar dentro da requisição HTTP | Ocupa um worker do threadpool por horas; é exatamente o que o RFC-026 proibiu (§2.3) |
| `BackgroundTasks` do FastAPI | Um reinício mata o job em silêncio e esvazia o sentido do checkpoint (§6) |
| Celery/RQ + Redis | Um serviço a mais para o usuário final, para uma fila de um produtor e um consumidor (§6.1) |
| WebSocket/SSE para progresso | Gerenciamento de conexão para economizar requisições de `TBM` ms em localhost (§7.2) |
| `201 Created` na criação do job | Afirma que o resultado existe; ele vai levar horas (§7.1) |
| Escopos como array/JSON na linha do job | A pergunta feita sobre escopos é um `WHERE` (§5.2) |
| Contadores atualizados por imagem | Dobra o tráfego de escrita do job por 8 unidades de granularidade (§7.3) |
| Cancelamento abortando o lote em curso | Descarta inferência já paga sem nada em troca (§8) |
| Cancelar desfazendo o trabalho feito | Joga fora embeddings corretos; o incremental já os reaproveita (§8) |
| Verificar job ativo em Python antes do `INSERT` | Check-then-act, o mesmo padrão que o RFC-026 §10 corrigiu (§9) |
| Nenhum mecanismo para job órfão | O índice único de §9 vira uma trava permanente do dispositivo no primeiro crash de worker (§9.1) |
| Detectar worker morto via `docker events`/PID externo | Acopla o reaper a um orquestrador específico; o heartbeat na própria tabela funciona independente de como o worker é hospedado (§9.1) |
| Tabela associativa job↔imagem | 40.000 linhas por job para um log sem consumidor, e uma FK sobre a PK recém-reescrita (§11) |
| CLI indexando direto, API por jobs | Dois lugares onde a semântica incremental pode divergir (§12) |

## 14. Não-objetivos

- Agendamento (`cron`, "indexar toda noite")
- Monitoramento automático de pastas (`watchdog`, `ReadDirectoryChangesW`) — `ARCHITECTURE.md` §23, e depende disto, não o contrário
- Prioridade entre jobs, reordenação de fila, preempção
- Retomada automática de jobs `failed` — o usuário recria
- Progresso por WebSocket (§7.2)
- Múltiplos workers sobre o **mesmo** dispositivo (§9)
- Indexação disparada por resultado de busca ("indexe isto para mim")
- Qualquer mudança no pipeline de embedding, no modelo ou no ranking
- A UI em si — este RFC entrega a API que a tela do Figma exigiria

## 15. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **O checkpoint depende de `sorted()` em `discover()`** (§10) | Passa a ser requisito de correção, com teste e docstring dedicados |
| **Sondagem tem latência de partida de até um intervalo** (§6.1) | Ruído contra uma operação de minutos a horas; `TBM` |
| **O worker é um segundo processo que precisa estar rodando** (§12) | Uma linha no compose; declarado, não escondido |
| **Um disco desplugado no meio de um job** | O job falha com `error_message`; o trabalho feito permanece e o próximo job o pula |
| **O timeout do reaper é um chute até ser medido** (§9.1) | Curto demais mata jobs saudáveis num lote lento; longo demais deixa um dispositivo travado por mais tempo após um crash real. `TBM` |
| **`discovered_files` só é conhecido depois da varredura** | O progresso é indeterminado até lá; a UI precisa de um estado "varrendo" e não de `0%` |
| **Um lote em andamento atrasa o cancelamento** em até `batch_size` imagens (§8) | Aceito; `TBM` segundos com o default de 8 |
| Jobs acumulam no histórico sem política de retenção | Trabalho futuro; linhas pequenas, sem urgência |

Trabalho futuro: monitoramento automático de pastas, que é o passo natural depois de jobs existirem; retenção de histórico; e prioridade de fila, se algum dia houver mais de um consumidor por dispositivo.

## 16. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/entities/indexing_job.py` | `IndexingJob`, `JobStatus` e as transições legais |
| `backend/app/domain/value_objects/job_scope.py` | `JobScope`, validação de caminho relativo |
| `backend/app/domain/repositories/indexing_job_repository.py` | A porta |
| `backend/app/domain/exceptions/job_errors.py` | Estado ilegal, escopo inválido, dispositivo ocupado |
| `backend/app/application/use_cases/create_indexing_job.py` | |
| `backend/app/application/use_cases/cancel_indexing_job.py` | |
| `backend/app/infrastructure/database/models/indexing_job_model.py` | As duas tabelas |
| `backend/app/infrastructure/persistence/postgres_indexing_job_repository.py` | Inclui a reivindicação atômica (§6) |
| `backend/app/infrastructure/workers/job_runner.py` | O laço de sondagem, o heartbeat por lote e o reaper de jobs órfãos (§9.1) |
| `backend/app/presentation/api/v1/routers/jobs.py` | §7 |
| `backend/app/presentation/schemas/job_schema.py` | |
| `backend/alembic/versions/*_create_indexing_jobs.py` | Inclui o índice único parcial de §9 |
| `backend/tests/domain/test_indexing_job_transitions.py` | A máquina de estados |
| `backend/tests/infrastructure/persistence/test_indexing_job_repository_contract.py` | Inclui a corrida de reivindicação |
| `backend/tests/infrastructure/filesystem/test_discovery_order_is_stable.py` | O teste de §10 |
| `backend/tests/presentation/test_jobs_api.py` | |
| `docs/rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/workers/indexing_worker.py` | Passa a receber escopo; cria e acompanha um job (§12) |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | Varredura escopada; docstring sobre a ordem (§10) |
| `backend/app/application/use_cases/index_or_update_images.py` | Reporta progresso por lote (§7.3); checa cancelamento (§8) |
| `backend/app/presentation/api/v1/__init__.py` | Registra o router de jobs |
| `backend/app/infrastructure/config/settings.py` | Intervalo de sondagem; `job_stale_timeout` (§9.1); `worker_count` ganha consumidor (§9) |
| `docker-compose.yml` | O serviço worker (§6) |
| `ARCHITECTURE.md` | §15 `IndexingJobs` atualizado (§5.1); §16 declara a dependência de ordem (§10) |

## 17. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | `TBM` |
| `pytest -m slow` | `TBM` |
| `black --check .` / `ruff check .` | `TBM` |
| `mypy` | `TBM` — **0 erros em código novo** é o critério |
| `alembic heads` | `TBM` — head único |
| `alembic downgrade`/`upgrade` | `TBM` |
| Duas requisições concorrentes ao mesmo dispositivo | `TBM` — uma cria, a outra recebe 409; fixa §9 |
| Job `running` sem heartbeat além do timeout vira `failed` sozinho | `TBM` — o teste que fixa §9.1; sem ele, o teste seguinte não teria como recriar o job |
| Dispositivo libera para um job novo depois do reaper agir | `TBM` — fixa que §9.1 realmente desbloqueia o índice único de §9 |
| Worker morto no meio de um job, reiniciado | `TBM` — job volta a `pending`/`failed` via reaper, outro worker retoma do checkpoint, nada reprocessado; fixa §10 e §9.1 |
| Ordem de descoberta estável entre execuções | `TBM` — fixa a premissa de §10 |
| Cancelamento observado entre lotes | `TBM` — atraso ≤ `batch_size` imagens |
| API responde `/health` durante um job ativo | `TBM` — fixa a premissa de §2.3 |
| Job com escopo vazio ≡ comportamento do CLI de hoje | `TBM` — fixa §12 |
| Latência de partida por sondagem | `TBM` (§6.1) |
