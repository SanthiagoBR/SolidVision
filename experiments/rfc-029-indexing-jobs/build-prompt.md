# Prompt de aplicação — RFC-029: Jobs de Indexação e Indexação Seletiva

> Documento de trabalho. Entrada para quem (ou o que) vai implementar a RFC-029.
> A autoridade é `docs/rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md`;
> este arquivo não a substitui — ele diz **como aplicá-la neste código**, aponta
> onde a RFC contradiz o código que existe hoje e fixa as decisões que faltavam.
>
> A RFC-029 foi escrita **antes** de as RFCs 027 e 028 serem implementadas. Várias
> das suas premissas foram verificadas contra o código enquanto este prompt era
> escrito, e quatro delas não se sustentam (§4.1, §4.3, §4.4, §4.9). Não as
> implemente ao pé da letra.

---

## 0. Missão

Implementar a RFC-029 no `develop` do SolidVision: as tabelas `indexing_jobs` e
`indexing_job_scopes`, a API assíncrona de jobs (`POST` → 202, polling,
cancelamento cooperativo), um processo que sonda a tabela e executa jobs com
heartbeat e reaper, a retomada por checkpoint, e o CLI de hoje reescrito como
cliente da mesma máquina de jobs. Depois, as medições, e a RFC reescrita sem
nenhum `TBM` restante.

O critério de aceite é o mesmo das RFCs 027 e 028: código com a mesma densidade de
docstrings explicando *por que*, `mypy --strict` com zero erros, as implementações
de repositório indistinguíveis sob teste de contrato, e cada número medido escrito
de volta no documento.

### 0.1 Pré-condição

**A RFC-028 precisa estar commitada antes da primeira linha desta.** Enquanto este
prompt era escrito, ela estava inteira no working tree, sem commit. Começar a
RFC-029 por cima produziria um diff que mistura as duas e uma mensagem de commit
que não consegue dizer o que cada uma entregou. Se `git status` mostrar os arquivos
da RFC-028 modificados, pare e peça que ela seja commitada primeiro.

Linha de base esperada depois do commit da RFC-028: `917 passed, 58 deselected`, e
`pytest -m slow` com `58 passed`; head do Alembic `c5d1e8f24a90`. Confirme — não
copie esses números.

---

## 1. Leia isto antes de escrever qualquer linha

| arquivo | por quê |
| --- | --- |
| `docs/rfcs/rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md` | a especificação |
| `docs/rfcs/rfc-027-dispositivos-e-identidade-de-volume.md` §4, §7 | identidade de volume, "conectado" como pergunta feita agora, e por que o ponto de montagem nunca é persistido |
| `docs/rfcs/rfc-028-data-de-captura-e-filtro-temporal.md` §6 | a varredura agora lê EXIF por arquivo — isso muda o custo da fase de descoberta que §4.5 deste prompt precisa cobrir com heartbeat |
| `docs/rfcs/rfc-024-pipeline-de-embeddings.md` §6, §7.2, §13 | isolamento de falha por lote, e por que o trabalho de um lote é durável antes do próximo começar |
| `docs/rfcs/rfc-026-api-de-busca.md` §8, §9, §10 | o handler de erro de domínio, `def` vs `async def`, e o check-then-act que o índice único de §9 existe para não repetir |
| `backend/app/application/use_cases/index_or_update_images.py` | `execute()`, `windowed()`, e o buffer `pending` que **atravessa janelas** — o centro de §4.4 |
| `backend/app/infrastructure/workers/indexing_worker.py` | `discovered_candidates()`, `register_device()` e o composition root que o CLI vai virar |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | o `sorted(self._root.rglob("*"))` de que a retomada passa a depender |
| `backend/app/infrastructure/filesystem/volume_identity_provider.py` | `mounted_volumes()` — a única resposta honesta para "o disco está conectado?" |
| `backend/app/presentation/error_handlers.py` | hoje **todo** `DomainError` vira 400; a RFC exige 404 e 409 (§4.7) |
| `backend/app/infrastructure/workers/device_reconcile.py`, `capture_date_backfill.py` | os moldes de worker-CLI já no repositório |
| `docker-compose.yml` | tem **um** serviço, e o backend não tem `Dockerfile` (§4.1) |
| `AI_Context.md`, `ARCHITECTURE.md` §15 (`IndexingJobs`) e §16 | regras de camada, a tabela a atualizar, e a definição de checkpoint que a RFC corrige |

---

## 2. O que já existe (verificado, não presumido)

```
indexing_worker.main()                         (composition root, CLI)
   │  register_device()  ── WindowsVolumeIdentityProvider.resolve(root)
   ▼
IndexingWorker.run()
   │  discovered_candidates()  ── FilesystemImageProvider(root).discover()
   │                               sorted(rglob) + stat + EXIF, em streaming
   ▼
IndexOrUpdateImagesUseCase.execute(candidates)
   │  for window in windowed(candidates, metadata_prefetch_size=512):
   │      prefetch em lote → plan_indexing() → SKIP / REFRESH / EMBED
   │      EMBED vai para `pending`; flush a cada batch_size=8
   ▼
IndexingSummary (contadores; format_report())
```

Fatos que a RFC não sabia ou supôs diferente:

- **`execute()` não tem nenhum ponto de extensão.** Não recebe callback, não
  consulta cancelamento, não reporta progresso até devolver o `IndexingSummary`
  no fim. Todo o §7.3 e o §8 da RFC dependem de criar esse ponto (§4.6).
- **O buffer `pending` sobrevive entre janelas.** Uma imagem `EMBED` da janela 1
  pode só ser persistida durante a janela 3, depois de linhas `SKIP_UNCHANGED`
  das janelas 2 e 3 já terem sido tratadas. "Último caminho processado" não é o
  mesmo que "último caminho durável" (§4.4).
- **Os repositórios Postgres commitam por conta própria** (`self._session.commit()`
  em `postgres_image_repository.py` e `postgres_device_repository.py`). Não há
  unidade de trabalho externa. Isso decide a questão de sessões de §4.11.
- **`settings.worker_count` não tem nenhum leitor** — confirmado por busca. A RFC
  §9 está certa nisso.
- **`error_handlers.py` mapeia a hierarquia `DomainError` inteira para 400**, sem
  distinção por subclasse.
- **Portas vivem em `app/domain/repositories/` e `app/domain/services/`**
  (`ContentHasherPort`). `VolumeIdentityProvider` é uma ABC de **Infrastructure**
  — a Application não pode importá-la (§4.8).
- **`docker-compose.yml` tem só o PostgreSQL, e não existe `Dockerfile` do
  backend.** A API e o worker rodam hoje no host, com o venv.

---

## 3. Invariantes que não podem ser quebrados

1. **A rota não indexa nada.** Nenhuma rota de jobs abre arquivo, carrega modelo
   ou chama `IndexOrUpdateImagesUseCase`. Ela escreve linhas e lê linhas. É a
   diferença inteira entre esta RFC e o que a RFC-026 §3 proibiu (RFC §2.3).
2. **Um caminho de código de indexação, não dois.** O CLI e a API chegam à mesma
   função de execução de job. Nenhuma cópia de `discovered_candidates()`, de
   `register_device()` ou da política incremental (RFC §12).
3. **A decisão incremental não muda.** `plan_indexing()` não ganha parâmetro,
   não sabe que jobs existem. Escopo decide *quais arquivos são descobertos*,
   nunca *como um arquivo descoberto é decidido*.
4. **Um job ativo por dispositivo é imposto pelo banco** — índice único parcial —
   e nunca por um `SELECT` antes do `INSERT` (RFC §9).
5. **Nenhuma FK de `indexing_jobs` ou `indexing_job_scopes` para `images`.** Um
   teste lê o metadado do SQLAlchemy e falha se aparecer uma (RFC §11). A janela
   de reescrita barata da PK fecha na RFC-030, não aqui.
6. **Caminho absoluto não entra no banco.** Nem no escopo, nem no checkpoint, nem
   em `error_message` formatado a partir de `absolute_path` sem necessidade
   (RFC-027 §4). Escopos e checkpoint são relativos ao ponto de montagem.
7. **Cancelar não desfaz e não aborta um lote no meio** (RFC §8).
8. **O checkpoint nunca pula um arquivo não durável.** Um checkpoint adiantado
   demais é perda de dados silenciosa; um atrasado demais é só retrabalho barato,
   porque a decisão incremental pula o que já foi feito. Na dúvida, atrase (§4.4).
9. **Os repositórios de jobs são indistinguíveis sob teste de contrato** — o
   Postgres e o em memória, incluindo a unicidade por dispositivo e a reivindicação
   atômica, que o duplo em memória precisa **emular**, não ignorar.

---

## 4. As decisões que a RFC deixou em aberto — ou errou

**Toda decisão abaixo deve ser escrita de volta na RFC**, na seção correspondente,
com a frase original preservada e corrigida à vista quando a RFC estava errada —
é a convenção do `docs/rfcs/README.md` ("decisões revertidas permanecem
documentadas") e o que a RFC-030 §7.2 já fez com o próprio erro.

### 4.1 O worker não pode rodar em container — a RFC §6 está errada

A RFC diz: *"O worker é um segundo serviço no `docker-compose.yml` — mesma imagem,
comando diferente."* Isso não funciona neste código, por três razões verificadas:

1. não existe imagem do backend — não há `Dockerfile`;
2. `register_device()` usa `WindowsVolumeIdentityProvider`, cujo construtor
   levanta `VolumeIdentityError` fora de `win32`. Um container Linux não consegue
   nem identificar um disco;
3. mesmo com um bind mount, um container vê um caminho montado, não o
   `\\?\Volume{GUID}\` do volume — a identidade de que a RFC-027 depende inteira.

**Decisão:** o executor de jobs é um **processo no host**, com o mesmo venv da
API: `python -m app.infrastructure.workers.job_runner`. O `docker-compose.yml`
**não muda**. Reescreva RFC §6, §12 ("uma linha a mais no compose"), §15 e a
tabela de entregáveis §16 dizendo isso e por quê.

O mesmo vale para a **API**: a validação "dispositivo conectado agora" (RFC §7.1)
chama `mounted_volumes()`, que só responde no host. Declare na RFC que API e
executor rodam no host; um deploy em container é um não-objetivo até existir um
adaptador de volume que funcione lá.

**Isto não é provisório.** O alvo de distribuição do projeto é **processo nativo
no Windows, empacotado como instalador**, com só o PostgreSQL em container. Em
container Linux sob Docker Desktop, o sistema perderia a identidade de volume da
RFC-027, a detecção de HD plugado a quente (bind mounts são fixos ao subir o
container) e a abertura no Explorer da RFC-030, e a leitura de arquivos passaria
pela fronteira da VM. Registre isso em §14 da RFC como não-objetivo, com esse
motivo, e não o apresente como dívida a pagar.

A consequência para o código: **nada fora do adaptador de volume depende de
Windows.** A tabela de jobs, a reivindicação, o reaper e o checkpoint não sabem em
que sistema rodam, de modo que um cenário de servidor Linux futuro seja um
adaptador novo, não uma reescrita de jobs.

### 4.2 `cancel_requested` falta no esquema

§8 escreve `cancel_requested = true`; a tabela de §5.1 não tem a coluna. Adicione
`cancel_requested BOOLEAN NOT NULL DEFAULT false` e corrija §5.1.

Por que uma flag e não um estado `cancelling`: o worker é o único dono das
transições a partir de `running`. Uma flag separada deixa a rota escrever sem
disputar a coluna `status` com o worker, e o índice único parcial de §9 continua
valendo até o worker de fato sair — que é o correto, porque o disco continua em
uso até lá.

Cancelar um job `pending` é imediato (a rota vai direto para `cancelled`, por
`UPDATE ... WHERE status = 'pending'`); se o `UPDATE` não pegar linha nenhuma
porque o worker reivindicou no meio, caia para a flag. Teste essa corrida.

### 4.3 O reaper: `failed` ou `pending`? A RFC diz os dois

§9.1 decide que o reaper marca `failed`. O parágrafo seguinte diz que o job
*"voltou a `pending` (ou `failed`, exigindo recriação)"*. §14 diz que jobs
`failed` não são retomados automaticamente. E §17 exige *"outro worker retoma do
checkpoint"*.

Essas afirmações não fecham: se o reaper marca `failed` e o usuário recria, o job
novo é outra linha, com outro id e checkpoint `NULL` — o checkpoint do job morto
nunca é lido por ninguém, e §10 vira uma coluna sem consumidor.

**Decisão:** o reaper devolve um job `running` com heartbeat vencido para
**`pending`**, preservando `last_processed_relative_path`, e incrementa
`attempts INT NOT NULL DEFAULT 0` (coluna nova). Ao atingir
`settings.job_max_attempts` (default 3), marca `failed` com `error_message`.
Um job vencido com `cancel_requested = true` vai para `cancelled`, não `pending`.

Por que isso e não `failed` direto:

- torna §10 e a linha de §17 verdadeiras em vez de decorativas;
- mantém o dispositivo reservado para o job interrompido (o índice parcial cobre
  `pending`), em vez de liberar a vaga para um job qualquer passar na frente do
  que já estava 60% feito;
- `attempts` impede o laço infinito do caso ruim real — um arquivo que derruba o
  processo inteiro (falha nativa de decodificador) mataria o worker a cada
  retomada, para sempre.

§14 continua verdadeiro como está escrito: `failed` não é retomado. O que mudou é
que um worker morto não produz `failed` na primeira vez. Escreva a distinção.

### 4.4 O checkpoint: a comparação e a durabilidade estão ambas erradas na RFC

**(a) "Continuar depois de X" não pode ser uma comparação de string.** Verificado
no Python 3.12 do projeto:

```
sorted(PureWindowsPath)  ['a\\x.jpg', 'a\\Z.jpg', 'a b\\x.jpg', 'B\\y.jpg']
sorted(str)              ['B/y.jpg', 'a b/x.jpg', 'a/Z.jpg', 'a/x.jpg']
```

`sorted()` sobre `Path` no Windows compara **por partes e sem caixa**. Uma string
ordena por código de caractere, e a collation do PostgreSQL ordenaria de uma
terceira forma. Um `WHERE relative_path > :checkpoint`, ou um
`str(path) > checkpoint` em Python, pula e repete arquivos arbitrários — e passa
em todo teste cujos nomes sejam minúsculos e sem espaço, que é todo teste escrito
sem saber disto.

**Decisão:** a retomada compara no Python, com **a mesma chave** que ordenou a
descoberta. Reconstrua `PureWindowsPath(checkpoint)` e compare `Path` com `Path`,
ou exponha a chave de ordenação no `FilesystemImageProvider` e use-a nos dois
lugares. O teste de §10 usa nomes com maiúsculas, espaço e partes de prefixo comum
(`a/`, `a b/`) — são exatamente os casos da saída acima.

**(b) O checkpoint só avança até o último arquivo durável contíguo.** Como §2
mostra, uma imagem `EMBED` fica em `pending` enquanto linhas posteriores já foram
decididas. Se o checkpoint for "o último caminho visto", um crash com `pending`
não vazio perde aquelas imagens na retomada: o checkpoint já passou delas e elas
nunca foram escritas.

A regra: o checkpoint é o maior caminho `P` tal que **todo** arquivo descoberto
até `P`, inclusive, foi persistido, pulado, ou registrado como falha. Na prática:
depois de cada flush e ao fim de cada janela, o checkpoint é o caminho
imediatamente anterior ao **mais antigo** plano ainda em `pending` — ou o último
caminho da janela, se `pending` estiver vazio. Teste o crash com `pending` cheio:
retomar não pode perder nenhuma das imagens do buffer.

**(c) Escopos múltiplos e sobrepostos.** Com `["2018/", "2018/junho"]`, o
segundo está contido no primeiro e seria descoberto duas vezes. Normalize na
criação, no Domain: escopo contido em outro é **absorvido**, e a resposta 202
devolve os escopos já normalizados (o cliente vê o que vai de fato rodar). A
ordem global de descoberta é a ordem dos escopos normalizados, cada um ordenado
internamente — e como escopos disjuntos ordenados por `Path` também ficam em
ordem de `Path`, a comparação de (a) continua valendo através das fronteiras.
Diga isso numa docstring, porque é a condição que ninguém lembraria de preservar.

**(d) O checkpoint economiza a varredura, não a inferência** — a RFC já diz isso
em §10 e está certa. Mas a varredura agora lê EXIF por arquivo (RFC-028), então o
quanto ela economiza é medição, não opinião (§7).

### 4.5 Heartbeat "por lote" deixa job saudável ser morto

A RFC escreve o heartbeat junto do progresso, "a cada lote". Três situações
reais passam minutos sem lote nenhum:

1. **uma retomada**: tudo antes do checkpoint é pulado, sem flush;
2. **um re-scan de disco já indexado**: janelas inteiras de `SKIP_UNCHANGED`,
   nenhum `EMBED`, nenhum flush — o caso *mais comum* de todos;
3. **a varredura de um HD mecânico frio** antes da primeira janela de 512
   arquivos encher.

Com o timeout "de alguns minutos" da RFC, o reaper mataria jobs perfeitamente
vivos exatamente nesses casos.

**Decisão:** o heartbeat é **por tempo**, não por evento. O ponto de extensão de
§4.6 é chamado a cada janela e a cada flush, e escreve no banco no máximo a cada
`settings.job_heartbeat_interval` segundos. Escreva também um heartbeat a cada
`N` arquivos descobertos dentro do gerador, para cobrir (3).

**E o modelo é carregado antes da primeira sondagem**, não depois de reivindicar
um job. Carregar o CLIP leva segundos (RFC-026 §9.1 mediu ~5 s a frio) e não tem
lote nenhum para emitir heartbeat. Um processo que ainda está carregando o modelo
não deve estar segurando um job.

### 4.6 O ponto de extensão na Application

`IndexOrUpdateImagesUseCase` precisa aprender a reportar progresso e a parar, sem
aprender o que é um job.

**Decisão:** uma porta em `app/domain/services/` (ao lado de `ContentHasherPort`),
algo como:

```python
class IndexingObserver(ABC):
    def window_decided(self, summary: IndexingSummary, durable_through: ImagePath | None) -> None: ...
    def batch_persisted(self, summary: IndexingSummary, durable_through: ImagePath | None) -> None: ...
    def should_stop(self) -> bool: ...
```

- Parâmetro **opcional** de `execute()`, default um observador nulo: toda chamada
  existente e todo teste existente continua funcionando sem mudança.
- `should_stop()` é consultado **entre lotes** e entre janelas, nunca dentro de
  `_encode_batch()` (RFC §8). Quando devolve `True`, `execute()` faz flush do que
  está em `pending` — inferência já paga não é jogada fora — e retorna com
  `summary.stopped = True`, sem levantar exceção. Um cancelamento não é falha.
- A porta não sabe o que é heartbeat, banco ou job. O adaptador de Infrastructure
  (`JobProgressObserver`) é quem limita a escrita por tempo (§4.5) e lê
  `cancel_requested`.
- Note: `IndexingSummary` hoje não conhece o caminho durável; é o use case que
  sabe o que está em `pending`, então é ele que calcula `durable_through` (§4.4b).

**Alternativa recusada:** passar o `IndexingJobRepository` para dentro do use
case. Faria a decisão incremental conhecer jobs, que é o invariante 3.

### 4.7 HTTP: a RFC promete 404 e 409, o handler só sabe 400

**Decisão:** hierarquia de domínio com duas bases novas em `domain/exceptions/`:
`NotFoundError(DomainError)` → 404 e `ConflictError(DomainError)` → 409.
`error_handlers.py` escolhe o status pela **subclasse mais específica**, mantendo
400 como default. Um teste fixa que todo erro de domínio pré-existente continua
400 — mudar o status de uma rota que já existe é quebra de contrato.

| situação | erro | status |
| --- | --- | --- |
| job inexistente | `JobNotFoundError(NotFoundError)` | 404 |
| dispositivo inexistente | `DeviceNotFoundError(NotFoundError)` | 404 |
| dispositivo com job ativo | `DeviceBusyError(ConflictError)` | 409 |
| cancelar job terminal | `IllegalJobTransitionError(ConflictError)` | 409 |
| dispositivo desconectado | `DeviceNotConnectedError(ConflictError)` | 409 |
| escopo absoluto, com `..`, fora do volume, inexistente | `InvalidJobScopeError` | 400 |

A RFC §7.1 manda "dispositivo desconectado" para 400. Corrija para 409 — o pedido
está bem formado e seria aceito com o disco plugado; é o estado que não permite, o
mesmo raciocínio que a RFC-030 §5.1 usa. Registre a mudança.

**A violação do índice único vira `DeviceBusyError` dentro do repositório
Postgres**, capturando `IntegrityError` e checando o **nome da constraint** — não
o texto da mensagem, não "qualquer `IntegrityError`". A rota não tem `try/except`
(RFC-026 §8).

### 4.8 A validação de criação precisa de uma porta que não existe

"O dispositivo está conectado agora" e "o escopo existe no disco" são perguntas ao
sistema de arquivos, e a Application não pode importar `VolumeIdentityProvider`.

**Decisão:** uma porta de Domain, `DeviceLocator` (ou nome melhor), com um método
que devolve o ponto de montagem de um `Device` agora, ou `None`, e outro que diz
se um caminho relativo existe como diretório sob ele. O adaptador de
Infrastructure é uma casca sobre `mounted_volumes()` e **não cacheia entre
chamadas** — é a regra que a docstring de `mounted_volumes()` já escreve.

Regras de escopo (Domain, `JobScope`):

- relativo; rejeita âncora de unidade (`D:`), raiz (`/`, `\`) e UNC;
- rejeita qualquer parte `..` — **antes** de resolver, não depois;
- normaliza separadores; `""` ou `"."` significa o volume inteiro;
- no adaptador: `resolve()` o caminho absoluto e confira `is_relative_to(mount_point)`
  — uma junção NTFS dentro do escopo pode apontar para outro volume, e `..`
  literal não é a única forma de sair da raiz.

**O executor revalida ao reivindicar.** Um disco plugado no `POST` pode estar na
gaveta quando o job sai de `pending`. Nesse caso: `failed`, com mensagem, sem
consumir `attempts`.

### 4.9 "Escopo vazio ≡ CLI de hoje" é falso

RFC §12 e §17 dizem que o CLI vira um job com escopo vazio, *o dispositivo
inteiro*. Mas `--root D:\fotos\2018` hoje indexa **só `fotos\2018`** — `root` é
uma pasta, não um volume. Escopo vazio indexaria o disco inteiro: uma mudança de
comportamento de horas, disfarçada de refatoração.

**Decisão:** o CLI converte `--root` em escopo `root.relative_to(mount_point)`
(vazio só quando `root` **é** o ponto de montagem). O teste de §17 compara o
conjunto de linhas produzido pelo CLI novo contra o `IndexingWorker` de hoje sobre
uma **subpasta**, não sobre a raiz.

**E o CLI não exige um segundo processo rodando.** A RFC aceita que *"o CLI passa a
exigir o worker rodando"* — isso transforma `indexing_worker --root` num comando
que fica esperando para sempre em silêncio quando ninguém subiu o executor.
**Decisão:** o CLI cria o job e o **executa no próprio processo**, chamando a
mesma função `run_job(job_id)` que o laço de sondagem chama, depois de reivindicar
**aquele** id com o mesmo `UPDATE` atômico. Se um executor em paralelo o
reivindicar primeiro, o CLI acompanha por polling e imprime o mesmo resumo. Um
caminho de código (invariante 2), zero processos obrigatórios a mais. Corrija §12.

### 4.10 A reivindicação atômica

`UPDATE ... WHERE status = 'pending' RETURNING`, como a RFC escreve, precisa de
uma subconsulta para escolher *qual* job, e essa subconsulta é onde a corrida
mora:

```sql
UPDATE indexing_jobs SET status = 'running', started_at = ..., last_heartbeat_at = ...
WHERE id = (
    SELECT id FROM indexing_jobs
    WHERE status = 'pending'
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

Sem `SKIP LOCKED`, dois executores escolhem o mesmo id; sob `READ COMMITTED` o
segundo reavalia o `WHERE`, não pega linha nenhuma, e volta a dormir **mesmo com
outros jobs pendentes na fila**. Não é corrupção, é latência fantasma — e é
invisível com um executor só, que é como todo teste roda. O teste de contrato
dispara duas reivindicações concorrentes em duas sessões e afirma que cada uma
ganhou um job diferente.

`started_at` só é escrito na primeira reivindicação; numa retomada (§4.3) ele se
preserva.

### 4.11 Sessões

Os repositórios commitam sozinhos (§2). Use **uma sessão para o repositório de
jobs e outra para o de imagens** dentro do executor. Com uma só, o heartbeat e o
checkpoint compartilham transação com as escritas de imagem, e o `rollback()` do
fallback de persistência em lote (RFC-024 §6) desfaria progresso já reportado — ou
o commit do heartbeat commitaria meio lote.

Heartbeat com `clock_timestamp()`, não `now()`: `now()` é o início da transação.
Com commits por chamada isso hoje dá no mesmo, mas é exatamente o tipo de
equivalência que um refactor para unidade de trabalho quebra sem teste falhar. O
reaper compara com `clock_timestamp()` também.

### 4.12 Descoberta em streaming não tem denominador

§5.1 chama `discovered_files` de *"o denominador do progresso"* e §15 admite que
ele *"só é conhecido depois da varredura"*. As duas frases não cabem juntas: a
descoberta é um gerador (RFC-021), e o total só existe no fim.

**Decisão:** não faça uma pré-varredura para contar — dobraria a leitura do
disco, que num HD mecânico frio é o custo dominante. `discovered_files` é um
contador **crescente**, e a resposta ganha `discovery_complete: bool`. Enquanto
for `false`, o cliente mostra "varrendo N arquivos", não uma porcentagem.
Opcional e rotulado como estimativa: para escopo vazio, `devices.last_scan_file_count`
da RFC-027 como `estimated_total_files`. Não invente estimativa para escopo parcial.

### 4.13 Disco desplugado no meio: hoje isso termina como `completed`

A RFC §15 diz que o job *"falha com `error_message`"*. Nada no código faz isso:
`discover()` começa com `if not self._root.exists(): return`, e um `rglob` sobre
volume que sumiu pode simplesmente parar de produzir arquivos. O resultado seria
um job **`completed`** com metade do escopo — o pior desfecho possível, porque
parece sucesso.

**Decisão:** ao fim de `execute()`, antes de marcar `completed`, o executor
pergunta ao `DeviceLocator` se o volume continua montado **no mesmo ponto**. Se
não, `failed` com mensagem clara, checkpoint preservado. Teste com um locator
falso que "desconecta" no meio.

---

## 5. Plano de trabalho

Cada fase termina com `pytest`, `mypy`, `ruff` e `black` limpos.

**Fase 0 — linha de base.** Confirme §0.1. Anote os números.

**Fase 1 — Domain.** `entities/indexing_job.py` (`IndexingJob`, `JobStatus` como
`StrEnum`, transições legais como método da entidade, `attempts`,
`cancel_requested`); `value_objects/job_scope.py` (§4.8, e a normalização de
sobreposição de §4.4c); `exceptions/job_errors.py` e as bases `NotFoundError` /
`ConflictError` (§4.7); portas `repositories/indexing_job_repository.py`,
`services/device_locator.py`, `services/indexing_observer.py`. Teste da máquina de
estados **exaustivo**: toda combinação `(estado, evento)` ilegal levanta; não só as
legais passam.

**Fase 2 — schema e persistência.** Modelo SQLAlchemy das duas tabelas;
migration `*_create_indexing_jobs.py` com o índice único parcial
(`postgresql_where=`), `downgrade()` real, `alembic heads` único.
`postgres_indexing_job_repository.py` com a reivindicação de §4.10, o reaper de
§4.3 e a tradução de `IntegrityError` de §4.7; `in_memory_indexing_job_repository.py`
emulando unicidade e reivindicação. `test_indexing_job_repository_contract.py`
contra os dois. Teste de invariante 5 (sem FK para `images`).

**Fase 3 — ordem e escopo na descoberta.** `FilesystemImageProvider` aceita
escopos (ou o executor instancia um por escopo normalizado — escolha e
justifique), expõe a chave de ordenação, e ganha a docstring de RFC §10.
`test_discovery_order_is_stable.py` com os nomes de §4.4a. Retomada a partir de
checkpoint, com a comparação por `Path`.

**Fase 4 — Application.** `IndexingObserver` em `execute()` com default nulo
(§4.6), `durable_through` (§4.4b), `summary.stopped`; toda a suíte existente de
`test_index_or_update_images.py` passando **sem edição**. Teste do crash com
`pending` cheio. Use cases `create_indexing_job.py`, `cancel_indexing_job.py`,
`get_indexing_job.py`, `list_indexing_jobs.py`.

**Fase 5 — executor.** `workers/job_runner.py`: modelo carregado antes de sondar
(§4.5), laço de sondagem com `settings.job_poll_interval`, reaper entre sondagens,
`run_job(job_id)` como função pública (§4.9), `JobProgressObserver` com heartbeat
por tempo, revalidação de conexão ao reivindicar e ao terminar (§4.8, §4.13), duas
sessões (§4.11). `Ctrl+C` no Windows chega como `KeyboardInterrupt`: o executor
termina o lote, grava checkpoint e devolve o job a `pending` **sem** incrementar
`attempts` — uma parada limpa não é uma morte. Settings novos: `job_poll_interval`,
`job_heartbeat_interval`, `job_stale_timeout`, `job_max_attempts`; `worker_count`
ganha leitor ou a RFC diz por que continua sem.

**Fase 6 — CLI.** `indexing_worker --root` reescrito conforme §4.9. Teste de
equivalência sobre subpasta.

**Fase 7 — API.** `routers/jobs.py`, `schemas/job_schema.py`, registro em
`api/v1/__init__.py`, `error_handlers.py` com a tabela de §4.7. Rotas `def`, não
`async def` (RFC-026 §9). Testes: 202 com escopos normalizados; 409 na segunda
criação concorrente; 404; cancelar `pending`, `running`, terminal; lista filtrada;
todo erro de domínio pré-existente ainda 400.

**Fase 8 — medições e documentação.** §7. Depois: RFC reescrita, `ARCHITECTURE.md`
§15 (`IndexingJobs`: `device_id`, `last_processed_relative_path`, `skipped_images`,
`cancel_requested`, `attempts`, `last_heartbeat_at`) e §16 (a dependência de ordem
e a regra de durabilidade de §4.4b), `docs/rfcs/README.md` RFC-029 📋 → ✅.

---

## 6. Armadilhas

1. **Índice parcial no Alembic** precisa de `postgresql_where=sa.text(...)` no
   `create_index`, e o `downgrade()` precisa removê-lo antes da tabela. O
   autogenerate às vezes não o detecta — escreva à mão e confira com `\d`.
2. **Teste de concorrência com uma sessão só não testa nada.** A fixture
   `db_session` isola por SAVEPOINT (RFC-019); duas reivindicações concorrentes
   precisam de duas conexões reais e commitadas, com limpeza explícita. Marque
   `slow` se for lento.
3. **`TIMESTAMPTZ` aqui, e está certo.** `captured_at` é naive por decisão da
   RFC-028 §5; timestamps de job são instantes do sistema e são aware. Um
   comentário no modelo evita que alguém "uniformize" as duas coisas.
4. **`subprocess` e `SIGKILL` no Windows.** Um teste de "worker morto" não precisa
   matar processo: avance o relógio do reaper (injete o relógio) e afirme a
   transição. Deixe o crash real para a medição, não para a suíte.
5. **`PureWindowsPath` em teste rodando no Windows** é o comportamento de
   produção; não troque por `PurePosixPath` para "portabilidade" — é o `sorted()`
   de produção que precisa ser fixado.
6. **`TestClient` e rotas `def`** rodam no threadpool; a corrida de criação
   concorrente pelo HTTP precisa de threads de verdade, não de duas chamadas em
   sequência.
7. **O duplo em memória mente por omissão.** Se ele aceitar dois jobs ativos no
   mesmo dispositivo, toda a suíte da Application vira evidência sobre o duplo.
8. **Heartbeat dentro do gerador de descoberta** roda no meio do consumo lazy de
   `execute()`; ele não pode levantar para dentro do gerador — uma exceção ali
   fecha o gerador e trunca a varredura em silêncio, o mesmo problema que
   `discovered_candidates()` já documenta.

---

## 7. As medições, e o que fazer com elas

Em `experiments/rfc-029-indexing-jobs/`, cada script com `.log`.

| `TBM` da RFC | o que medir | observação |
| --- | --- | --- |
| §2.1 "2.000 fotos, `TBM` minutos" | job escopado real de ~2.000 imagens, do `POST` ao `completed` | CPU; declare a máquina |
| §6.1 intervalo de sondagem / latência de partida | distribuição do tempo `created_at → started_at` com o intervalo escolhido | e o custo de CPU/consulta de sondar ocioso |
| §9.1 `job_stale_timeout` | distribuição do **maior intervalo entre heartbeats** em: indexação nova, re-scan 100% skip, retomada, primeira janela | o timeout sai do p99 disso com folga declarada, não de "alguns minutos" |
| §15 atraso de cancelamento | tempo `cancel_requested → cancelled` com `batch_size=8` | |
| §17 `/health` durante job | latência de `/health` e de uma busca com o executor saturando CPU **na mesma máquina** | processos separados não garantem nada contra disputa de CPU; é isso que medir |
| §10 quanto o checkpoint economiza | retomada com checkpoint vs. recomeço com skip incremental, mesmo escopo | inclui a leitura de EXIF da RFC-028 |

**Não medido, e dito:** varredura fria de HD mecânico externo (mesma limitação que
a RFC-028 declarou) — a menos que um disco real seja usado, e aí diga qual.

Depois de medir, reescreva a RFC como as RFCs 027 e 028: `Status: Implementado`,
`Medição` apontando para os scripts, a nota de rascunho no passado, zero `TBM`, e
onde a medição não respondeu tudo, diga o que ficou por medir.

---

## 8. Definição de pronto

- [ ] RFC-028 commitada antes de começar; linha de base anotada (§0.1)
- [ ] `pytest` verde, contagem antes/depois; `pytest -m slow` verde
- [ ] `mypy` zero erros; `ruff check .` e `black --check .` limpos
- [ ] `alembic heads` único; `downgrade` + `upgrade` exercitados de verdade
- [ ] Teste: segunda criação concorrente no mesmo dispositivo → 409, pelo índice, não por `SELECT` (§4.7)
- [ ] Teste: duas reivindicações concorrentes pegam jobs diferentes (§4.10)
- [ ] Teste: heartbeat vencido → `pending` com checkpoint preservado; `attempts` esgotado → `failed`; vencido com cancelamento → `cancelled` (§4.3)
- [ ] Teste: dispositivo volta a aceitar job depois do reaper agir
- [ ] Teste: ordem de descoberta estável com maiúsculas, espaço e prefixo comum; retomada compara por `Path` (§4.4a)
- [ ] Teste: crash com `pending` cheio não perde nenhuma imagem na retomada (§4.4b)
- [ ] Teste: re-scan 100% skip mais longo que o timeout não é morto pelo reaper (§4.5)
- [ ] Teste: cancelamento observado entre lotes, lote em curso completo, `summary.stopped` (§4.6)
- [ ] Teste: disco desconectado no meio → `failed`, nunca `completed` (§4.13)
- [ ] Teste: `--root` numa subpasta ≡ `IndexingWorker` de hoje (§4.9)
- [ ] Teste: escopo com `..`, absoluto, UNC ou junção para fora → 400 (§4.8)
- [ ] Teste: nenhuma FK para `images` (invariante 5)
- [ ] Teste: todo erro de domínio pré-existente continua 400 (§4.7)
- [ ] Teste: `test_index_or_update_images.py` passa sem edição (§4.6)
- [ ] Medições de §7 rodadas, com `.log`
- [ ] RFC-029 sem `TBM`, `Implementado`, e as correções de §4.1, §4.3, §4.4, §4.7, §4.9 registradas à vista
- [ ] `ARCHITECTURE.md` §15 e §16 atualizados; `docs/rfcs/README.md` RFC-029 📋 → ✅

---

## 9. Estilo

- **Docstrings e comentários em inglês; RFC, README e mensagens de commit em
  português.**
- Densidade de comentário igual à do código em volta: o *porquê*, a alternativa
  recusada, a seção da RFC.
- `from __future__ import annotations` no topo de todo módulo.
- Black 88 colunas; Ruff `E,F,I,N,UP`; `mypy --strict`.
- Nada de lógica de negócio em Presentation ou Infrastructure (`AI_Context.md`).
  As transições de estado são do Domain; a rota não decide se um cancelamento é
  legal.

---

## 10. Git

**Nunca rode `git commit` ou `git push` sem pedir confirmação explícita para
aquela ação específica.** Preparar o diff, montar o stage e redigir a mensagem é
livre; executar não é. Vale mesmo que um commit anterior da mesma sessão tenha
sido aprovado.

Mensagem no molde do `60903a5` (RFC-027): título `Implementa RFC-029: ...`, corpo
em tópicos com o que mudou e por quê, linha final com contagem de testes, mypy e
alembic. Encerre com:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

## 11. O que não fazer

- Não indexe dentro de uma requisição, nem com `BackgroundTasks`.
- Não adicione Redis, Celery, RQ, nem um serviço ao `docker-compose.yml`.
- Não verifique "job ativo" com `SELECT` antes do `INSERT`.
- Não compare checkpoint com `>` de string, em Python ou em SQL.
- Não avance o checkpoint além de um plano ainda em `pending`.
- Não escreva heartbeat só no flush.
- Não deixe `plan_indexing()` ou `IndexOrUpdateImagesUseCase` saberem que jobs existem.
- Não crie FK para `images`, nem tabela job↔imagem.
- Não persista caminho absoluto em lugar nenhum.
- Não aborte um lote em curso para cancelar, e não desfaça trabalho cancelado.
- Não marque `completed` sem confirmar que o volume ainda está montado.
- Não implemente agendamento, monitoramento de pastas, prioridade de fila,
  WebSocket/SSE ou a UI — RFC §14.
- Não mexa no pipeline de embedding, no modelo ou no ranking.
- Não preencha um `TBM` por dedução. Se não mediu, ele fica lá e você diz isso.
