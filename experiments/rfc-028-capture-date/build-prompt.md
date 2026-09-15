# Prompt de aplicação — RFC-028: Data de Captura e Filtro Temporal

> Documento de trabalho. Entrada para quem (ou o que) vai implementar a RFC-028.
> A autoridade é `docs/rfcs/rfc-028-data-de-captura-e-filtro-temporal.md`; este
> arquivo não a substitui — ele diz **como aplicá-la neste código**, aponta o que
> a RFC deixou ambíguo e fixa as decisões que faltavam.

---

## 0. Missão

Implementar a RFC-028 no `develop` do SolidVision: `images.captured_at` e
`images.capture_source` extraídos do EXIF na varredura, `SearchFilters.captured_between`
com `DateRange` meia-aberto, um backfill que não instancia o modelo, as duas
medições, e a RFC reescrita sem nenhum `TBM` restante.

O critério de aceite não é "os testes passam". É **o que a RFC-027 entregou**:
código com a mesma densidade de docstrings explicando *por que*, `mypy --strict`
com zero erros, os três `ImageRepository` indistinguíveis sob o teste de contrato,
e cada número medido escrito de volta no documento.

---

## 1. Leia isto antes de escrever qualquer linha

Nesta ordem. Não pule — metade das decisões abaixo já está justificada nestes
arquivos, e reabri-las é retrabalho.

| arquivo | por quê |
| --- | --- |
| `docs/rfcs/rfc-028-data-de-captura-e-filtro-temporal.md` | a especificação |
| `docs/rfcs/rfc-027-dispositivos-e-identidade-de-volume.md` §9, §9.1 | de onde vem `SearchFilters`, e por que o filtro é medido e não deduzido |
| `docs/rfcs/rfc-020-metadados-incrementais.md` | a convenção `NULL` = *desconhecido* e a escada de custo |
| `docs/rfcs/rfc-024-pipeline-de-embeddings.md` §7.2, §8 | por que o prefetch de metadados é uma leitura em lote por janela |
| `backend/app/domain/value_objects/search_filters.py` | já traz um comentário dizendo o que a RFC-028 vai adicionar, e `is_empty()` existe **exatamente** para que o segundo campo não seja esquecido |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` (`_apply_filters`) | idem: *"RFC-028 adds its date range here, beside this clause and not instead of it"* |
| `backend/app/application/use_cases/indexing_plan.py` | a decisão incremental que a RFC-028 **não pode** tocar (§6.1) |
| `backend/app/application/use_cases/index_or_update_images.py` | onde a janela de prefetch e o lote de persistência acontecem |
| `backend/app/infrastructure/workers/device_reconcile.py` | o molde de um worker-CLI que corrige metadado sem recomputar embedding — o parente mais próximo do backfill de §7 |
| `backend/tests/infrastructure/persistence/test_search_similar_contract.py` | um teste, **três** implementações |
| `AI_Context.md`, `ARCHITECTURE.md` §15/§16 | as regras de camada e a tabela `Images` a atualizar |

---

## 2. O que já existe (e onde encostar)

```
discover()  ──►  DiscoveredImageFile  ──►  IndexingWorker._candidates()
(Infrastructure)                                    │
                                                    ▼
                                            IndexCandidate      (Application)
                                                    │
                                    plan_indexing() ─┴─ EMBED / SKIP_UNCHANGED / REFRESH_METADATA
                                                    │
                                            IndexingRecord  ──►  ImageRepository.save_indexed_many()
```

- `FilesystemImageProvider.discover()` já chama `path.stat()` num laço — é ali que
  a extração de EXIF entra (§6).
- O prefetch `get_index_metadata_many()` já lê metadado em lote **uma vez por
  janela**; essa propriedade é da RFC-024 §8 e não pode virar N round-trips.
- `SearchFilters` já tem `is_empty()`; `_apply_filters()` já tem o lugar marcado.
- `ImageModel.file_modified_at` é `DateTime(timezone=True)` — o contraste com
  `captured_at` (sem fuso) é o ponto inteiro de §5 e merece um comentário no
  próprio modelo dizendo por que duas colunas temporais vizinhas têm tipos
  diferentes. Sem esse comentário, a próxima pessoa "corrige" a divergência.

---

## 3. Invariantes que não podem ser quebrados

1. **A escada de custo da RFC-020 continua intacta.** `plan_indexing()` não ganha
   parâmetro novo, não lê `captured_at`, e não muda de comportamento em nada.
   Um teste fixa isso (§6.1).
2. **`NULL` nunca significa "combina".** Imagem sem `captured_at` não aparece em
   busca filtrada por intervalo — nos três repositórios, identicamente.
3. **`filters=None` e `SearchFilters()` produzem a consulta da RFC-025 sem uma
   cláusula sequer.** Um `captured_between` ausente não pode virar um intervalo
   infinito emitido no SQL: a query não filtrada tem que continuar idêntica.
4. **`mtime` não entra na cadeia de fallback.** Nem como último recurso, nem como
   "dica", nem via tag EXIF `0x0132` (`DateTime`), que é hora de modificação
   disfarçada. §4 é uma reversão deliberada do rascunho anterior; não a
   reintroduza.
5. **`captured_at` é naive.** Sem `tzinfo`, em toda a pilha: extração, entidade,
   coluna, filtro, resposta HTTP. Um `datetime` com fuso em qualquer ponto é bug.
6. **EXIF corrompido nunca derruba a varredura.** Degrada para o próximo elo da
   cadeia e, no limite, para `unknown`.
7. **O backfill não importa o modelo de embedding.** Nem transitivamente.
8. **Os três `ImageRepository` continuam indistinguíveis** sob
   `test_search_similar_contract.py`. São três, não dois — veja §4.1.

   Com um limite que precisa ser entendido antes de alguém tentar "reforçar" esse
   teste: a paridade é exata **apenas na escala em que o teste roda**. O
   `PostgresImageRepository` ranqueia por HNSW, que é aproximado — a RFC-025 §7.3
   mediu a query devolvendo 1 de 3 linhas vivas com o índice inchado. Os três
   concordam ali porque o corpus é pequeno o bastante para o planejador escolher
   seq scan. Um teste de contrato que afirmasse paridade com 20.000 linhas seria
   instável por construção, e o filtro de data *alarga* a faixa em que o
   pós-filtro descarta candidatos do `ef_search` e devolve menos que `limit` sem
   erro nenhum. A divisão de trabalho é: o teste de contrato prova paridade em
   escala pequena, `planner_check.py` (§8.1) cobre o que acontece fora dela.
   Não funda os dois.

---

## 4. As decisões que a RFC deixou em aberto

A RFC-028 é uma proposta e não desce ao nível de assinatura de método. Estas são
as escolhas que aparecem na hora de escrever o código, com a resolução que este
prompt fixa. **Toda decisão adotada deve ser escrita de volta na RFC**, na seção
correspondente — a RFC é o registro, este arquivo não é.

### 4.1 Existe um terceiro repositório, e a RFC não o lista

`backend/tests/application/fakes.py::FakeImageRepository` é a terceira
implementação segurada pelo teste de contrato. A tabela "Modificados" da RFC §12
cita só `postgres_` e `in_memory_`. **Atualize as três.** Um double que ignora o
filtro temporal transforma toda a suíte da Application em evidência sobre o
double em vez de sobre o produto.

### 4.2 `captured_at` vai para a entidade `Image` de Domain — sim

**Decisão: sim.** `Image` ganha dois campos opcionais:

```python
captured_at: datetime.datetime | None = None
capture_source: CaptureSource | None = None
```

Por quê, já que `file_size`, `file_modified_at` e `content_hash` ficaram de fora:
aqueles três são **sinais de mudança**, consumidos só pela decisão incremental, e
por isso viajam em `IndexMetadata`/`IndexingRecord`. `captured_at` é um **atributo
pesquisável da fotografia**, que (a) precisa chegar à Presentation via `SearchHit`
para a resposta de §12, e (b) precisa ser visível ao predicado de filtro dos dois
doubles em memória, que hoje recebem um `Image` (`matches_filters(image, filters)`).
O argumento do docstring de `SearchHit` — *"a similarity means nothing without the
query that produced it"* — corta no sentido oposto aqui: a data de captura é a
mesma para todo chamador, é um fato da foto e não do par foto-consulta.

Consequências a não esquecer: `ImageModel.from_domain()` / `to_domain()`, o
default `None` para que nenhum site de construção existente quebre, e o
`__eq__`/`__hash__` por id que já existem (não mexa neles).

**Alternativa recusada:** manter os campos só na persistência e devolvê-los num
`SearchHit` estendido. Obrigaria os doubles a manter um dicionário lateral só para
filtrar, que é precisamente o tipo de divergência que o teste de contrato existe
para impedir.

### 4.3 Onde `captured_at` é escrito para uma linha que a decisão incremental pula

O ponto mais delicado da RFC. §6 diz que *"uma imagem já indexada ganha
`captured_at` sem pagar inferência"*; §6.1 diz que `captured_at` *"é escrito por
`UPDATE` no caminho de varredura"*. Lido ao pé da letra, isso é um `UPDATE` por
arquivo por varredura — 100.000 escritas a cada re-scan de um acervo que não
mudou, destruindo a propriedade que torna o skip barato.

**Decisão: escrita condicional.** Na varredura, uma linha só é atualizada quando
**ainda não foi examinada** — isto é, quando o `capture_source` lido do banco é
`NULL`. Em regime permanente o custo de escrita é zero; na primeira varredura após
esta RFC, todo o acervo conectado é preenchido. O backfill de §7 passa a ser o
mesmo caminho com a indexação desligada, e não um segundo mecanismo com regras
próprias.

**A condição vale para os dois ramos que pulam inferência, não só para um.**
`SKIP_UNCHANGED` é o caso óbvio; `REFRESH_METADATA` é o que se esquece — mtime
mexeu, hash bateu, e se essa linha nunca foi examinada ela precisa da escrita
exatamente como a outra. Deixar só o primeiro ramo condicionado significa que toda
linha cujo mtime mexeu sem o conteúdo mudar fica permanentemente sem
`captured_at`. O ramo `EMBED` não precisa de condição: ele já escreve a linha
inteira.

**O que essa regra deliberadamente não cobre.** Uma linha `'unknown'` nunca mais é
reexaminada por uma varredura normal. Isso está correto enquanto o arquivo não
muda: se o EXIF mudasse, os bytes mudariam, o hash mudaria e a linha cairia em
`EMBED`. O caso que fica de fora é **a extração melhorar** — código novo lendo uma
tag que antes era ignorada — que é precisamente o cenário que §4.2 da RFC prevê
("caso uma fonte melhor apareça"). É para ele que existe o `--force` do backfill
(Fase 8); sem essa flag, a distinção `NULL`/`'unknown'` fica sem nenhum consumidor
e vira decoração.

Isso exige que `capture_source` venha no prefetch. **Adicione o campo a
`IndexMetadata`** — a leitura já é em lote, um escalar a mais é de graça — com um
docstring alto e claro dizendo que **não é sinal de mudança e não participa da
decisão de `plan_indexing()`**, e com o teste de §6.1 fixando isso.

A distinção que vale ouro, e que precisa estar no docstring da coluna:

| valor de `capture_source` | significado |
| --- | --- |
| `NULL` | a linha **nunca foi examinada** (existe desde antes desta RFC) |
| `'unknown'` | foi examinada e o arquivo não tem data nenhuma |
| `'exif_original'` / `'exif_digitized'` | de onde a data veio |

É essa diferença que faz §4.2 da RFC (*"um backfill futuro sabe quais linhas vale
a pena revisitar"*) ser verdade. Se `NULL` e `'unknown'` colapsarem num só valor,
todo re-scan relê o EXIF de todo arquivo sem data — que é justamente a população
mais numerosa dos casos ruins — para sempre.

### 4.4 `capture_source` é um `StrEnum` de Domain, persistido como `String`

`CaptureSource` em `app/domain/value_objects/`, com `exif_original`,
`exif_digitized` e `unknown`. Coluna `String`, **não** um tipo `ENUM` do
PostgreSQL: acrescentar um valor (`gps_derived`, `filename_parsed` — o que §11
prevê como trabalho futuro) custaria uma migration, e o ganho de um enum nativo
aqui é nenhum.

### 4.5 `DateRange` aceita apenas `datetime` naive, e `start == end` é legal

- Meia-aberto `[start, end)`, campos `start` / `end` do tipo `datetime` naive.
- `tzinfo is not None` em qualquer um dos dois → erro de domínio na construção.
  Sem isso, a comparação com a coluna sem fuso falha no Postgres e, pior, levanta
  `TypeError` nos doubles — dois comportamentos diferentes para a mesma chamada,
  exatamente o que o teste de contrato existe para proibir.
- `start > end` → erro. `start == end` é um intervalo vazio válido que casa com
  zero imagens; teste isso explicitamente em vez de deixá-lo implícito.
- Extremos abertos: a API aceita `captured_from` e `captured_to`
  independentemente. Ausente vira `datetime.min` / `datetime.max` **na
  Presentation**, não dentro do `DateRange` — o value object permanece total, com
  dois campos obrigatórios, e não ganha um `None` que todo consumidor precisaria
  tratar. Se os dois vierem ausentes, `captured_between` é `None` e nenhuma
  cláusula é emitida (invariante 3).

### 4.6 A extração é eager na varredura, atrás de um `Settings`

§6 coloca a leitura em `discover()`, e §11 já avisa: *"se for alto, a extração
vira opcional na varredura"*. Implemente eager, **meça primeiro**
(`measure_exif_cost.py`), e exponha `Settings.extract_capture_date: bool = True`.
Se a medição mostrar custo alto, a discussão sobre extração preguiçosa é uma
decisão *pós-medição* para escrever na RFC — não a antecipe reformando
`DiscoveredImageFile` para carregar um callable.

### 4.7 A resposta HTTP: §12 da RFC colide com a RFC-030

§12 lista `search_schema.py` ganhando `captured_at` e `capture_source`, mas o
docstring de `images.py` diz que *"RFC-030 owns the response shape"*.
**Resolução:** publique os dois campos agora — são atributos da foto, não caminho
nem estado de disco, e são o que torna o resultado do filtro legível ("por que
essa foto apareceu, e com que confiança"). Não publique nada de caminho,
dispositivo ou conexão; isso continua sendo da RFC-030. Registre a resolução numa
nota em §12 da RFC.

Sério sobre serialização: garanta **por teste** que o JSON traz
`"2018-07-14T15:32:05"`, sem `Z` e sem offset. Um `Z` acidental desfaz a §5
inteira na saída, silenciosamente.

### 4.8 Métodos novos no port `ImageRepository`

A RFC não os nomeia, mas a escrita condicional e o backfill precisam deles.
Sugestão: `update_capture_date(image_id, captured_at, capture_source)` e o bulk
`update_capture_date_many(...)`. Toda adição ao ABC obriga as **três**
implementações e `backend/tests/domain/test_image_repository_port.py`. Documente
no port, como o resto do arquivo faz, o que a implementação deve à chamada —
inclusive que uma linha inexistente é no-op, o mesmo contrato de
`update_index_metadata()`.

### 4.9 A contagem de excluídos por data desconhecida: a RFC exige e não entrega

A lacuna mais séria do documento, e ela é sobre o produto, não sobre o código.
§4.1 diz que *"a UI precisa poder dizer quantas imagens foram excluídas por data
desconhecida, senão o usuário conclui que a foto não existe"*, e §11 repete como
risco aceito. Os entregáveis de §12 não produzem esse número em lugar nenhum:
`search_schema.py` ganha `captured_at` e `capture_source` da foto, e nada que
conte o que ficou de fora. A RFC afirma um requisito duas vezes e some com ele.

Isso importa porque é exatamente o defeito que §2.1 existe para eliminar,
reaparecido do outro lado: *"não achei a foto de 2018"* e *"a foto de 2018 está
indexada mas não tem EXIF"* ficam indistinguíveis na tela — silenciosos, e
parecendo que a foto não existe.

**Decisão: implemente a contagem**, e faça disso uma linha explícita na RFC.
Forma: um campo na resposta da busca (`excluded_unknown_date`, ou o nome que
couber no schema), alimentado por um método novo do port — um `COUNT(*)` com os
**mesmos** filtros da busca, mais `captured_at IS NULL`, emitido só quando
`captured_between` está presente. Custos e limites a documentar no port:

- é uma **segunda consulta**, não um subproduto da primeira; a busca vetorial
  devolve no máximo `limit` linhas e não tem como saber o que o `WHERE` descartou;
- ela conta sobre a tabela inteira sob os filtros, não sobre a vizinhança que o
  HNSW explorou — é a resposta que o usuário quer ("quantas fotos o filtro
  escondeu"), e não o mesmo universo do ranking. Diga isso no docstring, senão
  alguém vai "corrigir" a divergência aparente depois;
- sem `captured_between`, não é emitida e o campo vem `0` ou ausente — nunca uma
  consulta a mais na busca não filtrada (invariante 3).

**Se você decidir não implementar**, isso é legítimo, mas então §4.1 e §11 da RFC
precisam ser reescritas dizendo que a contagem ficou adiada e para onde foi. O que
não é aceitável é entregar a RFC com o requisito ainda afirmado e nada o
atendendo.

---

## 5. Plano de trabalho

Nesta ordem. Cada fase termina com `pytest`, `mypy`, `ruff` e `black` limpos — não
acumule dívida entre fases.

**Fase 0 — linha de base.** Rode `pytest` e anote quantos testes passam hoje. A
mensagem de commit da RFC-027 reporta `625 passed`; a sua vai reportar o antes e o
depois.

**Fase 1 — Domain.** `value_objects/date_range.py` (`DateRange`),
`value_objects/capture_source.py` (`CaptureSource`), os dois campos em
`entities/image.py`, `captured_between` em `SearchFilters` **e `is_empty()`
atualizado**. Testes de domínio para cada um, incluindo os casos de borda de §4.5.

**Fase 2 — extração de EXIF.** `infrastructure/filesystem/exif_capture_date.py`
com a cadeia de §4: uma função pura de `Path` para
`(datetime | None, CaptureSource)`. Testes em
`tests/infrastructure/filesystem/test_exif_capture_date.py` cobrindo
`DateTimeOriginal` presente; só `DateTimeDigitized`; nenhum dos dois; EXIF ausente
(PNG); EXIF corrompido/truncado; a data-placeholder `0000:00:00 00:00:00`; string
com NUL ou espaço sobrando; arquivo ilegível. As armadilhas da §6 deste prompt são
a razão de esse arquivo existir como unidade separada.

**Fase 3 — varredura.** `DiscoveredImageFile` ganha os dois campos;
`FilesystemImageProvider.discover()` chama a extração no mesmo laço do `stat()`,
atrás de `Settings.extract_capture_date`.
`tests/infrastructure/filesystem/test_filesystem_image_provider.py` ganha os casos.

**Fase 4 — persistência e migration.** `ImageModel.captured_at`
(`DateTime(timezone=False)`) e `capture_source` (`String`), ambas nullable;
migration `*_add_image_capture_date.py` com `downgrade()` real; teste em
`test_image_model.py` afirmando que a coluna é **sem fuso**
(`ImageModel.__table__.c.captured_at.type.timezone is False`) e que um naive faz
round-trip inalterado. Confira `alembic heads` — um só.

**Fase 5 — o caminho de escrita.** `IndexCandidate` e `IndexingRecord` ganham os
dois campos; `IndexMetadata` ganha `capture_source` (§4.3); os métodos novos do
port nas três implementações; a escrita condicional nos ramos `SKIP_UNCHANGED`
**e `REFRESH_METADATA`** de `IndexOrUpdateImagesUseCase` (§4.3 — esquecer o
segundo deixa sem `captured_at`, para sempre, toda linha cujo mtime mexeu sem o
conteúdo mudar); `IndexingWorker._candidates()` repassando o que a varredura
descobriu. `IndexingSummary` ganha um contador (`capture_dates_written`) — a
RFC-024 §11 estabeleceu que o run se reporta como dados, não como log.

O teste de §6.1 desta fase precisa ser escrito no nível certo para valer alguma
coisa. Mudar `captured_at` e chamar `plan_indexing()` não prova nada: a assinatura
nem recebe o campo, então o teste passa por construção e continuaria passando
depois de alguém quebrar a regra. O teste que **falha** se a comparação for
adicionada é sobre `IndexMetadata`: dois metadados com `capture_source` diferente
e `file_size` / `file_modified_at` / `content_hash` idênticos têm que produzir
`SKIP_UNCHANGED`.

**Fase 6 — o filtro.** `_apply_filters()` no Postgres
(`captured_at >= start AND captured_at < end`, que já exclui `NULL` de graça) e
`matches_filters()` nos doubles, onde o `is not None` precisa ser **explícito**.
Novos casos em `test_search_similar_contract.py`: intervalo que exclui; `NULL`
nunca casa; meia-abertura no limite exato (uma foto em `end` não entra, uma em
`start` entra); filtro de data combinado com filtro de dispositivo; intervalo
vazio; e o filtro não ressuscita imagem sem embedding.

**Fase 7 — API.** `captured_from` / `captured_to` em `images.py`,
`captured_at` / `capture_source` em `search_schema.py`, testes de rota e de
integração. Um valor com offset tem que ser recusado com erro claro, nunca
convertido em silêncio.

**Fase 8 — backfill.** `infrastructure/workers/capture_date_backfill.py`, molde em
`device_reconcile.py`: CLI com `--root` obrigatório (sem default — a mesma razão
do worker de indexação), `--label`, `--dry-run`, escrita em lote, sumário
formatado, reusando `register_device()`.

Mais duas flags, que são o motivo de a distinção `NULL` / `'unknown'` de §4.3
existir:

- `--only-unknown` (padrão): examina só as linhas com `capture_source` `NULL`, as
  que nunca foram olhadas. É o modo idempotente — rodar duas vezes seguidas sobre
  o mesmo disco não relê nada na segunda;
- `--force`: reexamina também as `'unknown'`. Existe para um caso e só um — a
  extração melhorou, e agora lê uma tag que antes ignorava. Nunca reescreve uma
  linha `exif_original` ou `exif_digitized` com um resultado pior: uma fonte já
  afirmada só é substituída por outra igual ou melhor na cadeia de §4. Diga isso
  no docstring, porque a implementação ingênua ("releia tudo e grave") é
  destrutiva de um jeito que passa em todo teste.

O teste que fixa §7 precisa **provar** a
ausência do modelo, não supô-la: siga `tests/test_ai_layer_boundaries.py`, que já
anda no grafo de imports, em vez de um mock que passaria mesmo com um `import
torch` no módulo.

**Fase 9 — medições e documentação.** Os dois scripts de
`experiments/rfc-028-capture-date/`, rodados, com `.log` de saída. Depois:
reescreva a RFC (§7 deste prompt), atualize `ARCHITECTURE.md` §15 (tabela
`Images`) e a seção de indexação incremental, e mude a linha da RFC-028 em
`docs/rfcs/README.md` de 📋 para ✅.

---

## 6. EXIF com Pillow: as armadilhas que vão te pegar

Específicas o bastante para custar horas se descobertas por tentativa e erro.

1. **`DateTimeOriginal` não está no IFD raiz.** `img.getexif()[0x9003]` devolve
   `None` na esmagadora maioria dos arquivos. As tags `0x9003`
   (`DateTimeOriginal`) e `0x9004` (`DateTimeDigitized`) vivem no **Exif IFD**,
   alcançado por `img.getexif().get_ifd(0x8769)`. Se o primeiro teste contra um
   JPEG real devolver `unknown`, é isto.
2. **`0x0132` (`DateTime`) não é fallback.** É hora de modificação gravada por
   software de edição — `mtime` com outra roupa, e §4 proíbe.
3. **`"0000:00:00 00:00:00"` é comum.** Câmeras e exportadores gravam esse
   placeholder. Tem que ser tratado como *ausente* — não como erro, e muito menos
   como data. `strptime` levanta nele, e engolir a exceção daria o resultado certo
   pelo motivo errado: filtre explicitamente, com o caso nomeado no teste.
4. **O formato é `"%Y:%m:%d %H:%M:%S"`**, com dois-pontos também na data. Faça
   `.strip()` e remova NUL à direita antes de parsear; strings EXIF vêm com lixo
   de padding.
5. **`Image.open()` é preguiçoso e deve continuar assim.** Lê o cabeçalho e não
   decodifica pixels — é disso que depende o argumento de custo de §6. **Não chame
   `.load()`**, não converta, não redimensione.
6. **Use `with Image.open(path) as img:`.** Uma varredura de 100.000 arquivos com
   handles vazando termina em `OSError: too many open files`.
7. **Capture largo na fronteira.** Sobre um arquivo corrompido, `Image.open` pode
   levantar `UnidentifiedImageError`, `OSError`, `struct.error`, `ValueError` ou
   `SyntaxError`, dependendo de onde está a corrupção. A função de extração
   devolve `(None, unknown)` para todos, e a varredura nunca vê a exceção (§13).
8. **PNG e BMP simplesmente não têm EXIF.** `unknown`, sem log de erro — não é
   anomalia, é o caso normal para parte das extensões suportadas.
9. **Nada de `PIL.ExifTags.TAGS` por nome.** Constantes numéricas nomeadas no
   módulo, como `SHA256_HEX_LENGTH` e `EMBEDDING_DIMENSION` já fazem.

---

## 7. As medições, e o que fazer com elas

**`measure_exif_cost.py` (§6).** Custo por arquivo da extração, isolado do resto da
varredura, sobre um corpus grande o bastante para não estar medindo cache de
disco. Reporte também o custo total projetado para 100.000 arquivos — é esse
número que decide se o risco de §11 se realiza. Compare com os ~450 ms/imagem de
inferência da RFC-024, que é a escala contra a qual §6 afirma "ordens de magnitude
abaixo": ou a afirmação se confirma medida, ou ela sai do documento.

**`planner_check.py` (§8.1).** `EXPLAIN ANALYZE` com intervalos de seletividade
1%, 10%, 50% e 90% sobre corpus sintético grande — `experiments/rfc-027-devices/planner_check.py`
já resolveu esse problema (20.000 imagens) e é o ponto de partida, não um arquivo
em branco. O que precisa aparecer no resultado: qual plano o planejador escolhe em
cada faixa **e quantas linhas voltam contra `limit`**. O regime intermediário de
§8.1 se manifesta como *menos que `limit` resultados sem erro nenhum*; um script
que só mede latência não o veria.

**Depois de medir**, reescreva a RFC como a RFC-027 foi reescrita: `Status:
Proposto` → `Implementado`, a linha `Medição` apontando para os scripts, a nota da
convenção de rascunho reformulada no passado ("isso foi feito"), zero `TBM`, e —
importante — onde a medição **não** respondeu à pergunta inteira, diga o que ficou
por medir em vez de completar por dedução.

---

## 8. Definição de pronto

- [ ] `pytest` verde; número de testes reportado antes/depois
- [ ] `pytest -m slow` verde
- [ ] `mypy` com **zero** erros (o critério é código novo, mas não deixe regressão)
- [ ] `ruff check .` e `black --check .` limpos
- [ ] `alembic heads` com head único; `downgrade` + `upgrade` exercitados de verdade
- [ ] Teste: `capture_source` diferente, com size/mtime/hash idênticos, ainda produz `SKIP_UNCHANGED` (§6.1 — no nível de `IndexMetadata`, não de `plan_indexing()`)
- [ ] Teste: uma linha `REFRESH_METADATA` nunca examinada também ganha `captured_at` (§4.3)
- [ ] Teste: `--force` não rebaixa uma linha `exif_original` (Fase 8)
- [ ] Teste: o backfill não instancia nem importa o modelo de embedding (§7)
- [ ] Teste: EXIF corrompido não derruba a varredura (§13)
- [ ] Teste: `captured_at = NULL` nunca aparece sob filtro de intervalo, nos três repositórios (§4.1)
- [ ] Teste: a coluna é `TIMESTAMP` sem fuso, e um naive faz round-trip inalterado (§5)
- [ ] Teste: o JSON de resposta não traz offset nem `Z`
- [ ] Teste: busca sem `captured_between` emite a consulta não filtrada inalterada, e **não** dispara a contagem de excluídos (§4.9)
- [ ] A contagem de excluídos por data desconhecida está implementada — ou §4.1 e §11 da RFC foram reescritas dizendo que ficou adiada (§4.9)
- [ ] `measure_exif_cost.py` e `planner_check.py` rodados, com `.log`
- [ ] RFC-028 sem nenhum `TBM`, status `Implementado`, decisões da §4 deste prompt registradas
- [ ] `ARCHITECTURE.md` §15 (tabela `Images`) e a seção de indexação incremental atualizadas
- [ ] `docs/rfcs/README.md`: RFC-028 📋 → ✅

---

## 9. Estilo

- **Docstrings e comentários de código em inglês; RFC, README e mensagens de
  commit em português.** É a convenção vigente do repositório; não a inverta.
- Densidade de comentário igual à do código em volta: o *porquê*, a alternativa
  recusada, e a referência à seção da RFC. Um docstring que só reafirma o nome do
  método está abaixo do padrão deste código.
- `from __future__ import annotations` no topo, como em todo módulo.
- Black 88 colunas; Ruff `E,F,I,N,UP`; `mypy --strict`.
- Nada de lógica de negócio em Presentation ou Infrastructure (`AI_Context.md`).

---

## 10. Git

**Nunca rode `git commit` ou `git push` sem pedir confirmação explícita para
aquela ação específica.** Preparar o diff, montar o stage e redigir a mensagem é
livre; executar não é. Vale mesmo que um commit anterior da mesma sessão tenha
sido aprovado.

Mensagem no molde do `60903a5` (RFC-027): título em português
(`Implementa RFC-028: ...`), corpo em tópicos com o que mudou e por quê, e a linha
final com contagem de testes, mypy e alembic. Encerre com:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

## 11. O que não fazer

- Não use `mtime` como fallback — nem escondido, nem "só para ordenar".
- Não guarde `captured_at` em `TIMESTAMPTZ`, e não normalize fuso nenhum.
- Não deixe `plan_indexing()` enxergar `captured_at`.
- Não recompute embedding por causa de metadado. Nunca.
- Não trate `NULL` como "combina".
- Não deixe o prefetch virar uma consulta por arquivo.
- Não escreva EXIF em arquivo nenhum do acervo — o sistema só lê (§10).
- Não implemente GPS, timeline, histograma, correção de relógio de câmera ou
  indexação seletiva por ano. São §10 e §11, e a última é a RFC-029.
- Não preencha um `TBM` por dedução. Se não mediu, ele fica lá e você diz isso.
