# RFC-028 — Data de Captura e Filtro Temporal

**Status:** Implementado
**Depende de:** RFC-020 (metadados incrementais), RFC-021 (worker), RFC-024 (pipeline), RFC-025 (busca), RFC-027 (`SearchFilters`, dispositivos)
**Migration:** sim — `images.captured_at`, `images.capture_source` (`c5d1e8f24a90`)
**Medição:** `experiments/rfc-028-capture-date/measure_exif_cost.py` (§6) e `experiments/rfc-028-capture-date/planner_check.py` (§8.1) — medidos, com `.log` ao lado de cada script

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir* durante a implementação e devia ser escrito de volta aqui depois. **Isso foi feito:** não resta nenhum `TBM`, e cada número abaixo vem com a escala e as condições em que foi medido. Onde a medição não respondeu à pergunta inteira — §6 (leitura a frio de disco externo) e §8.1 (acervo maior e datas correlacionadas com conteúdo) — o que ficou por medir está dito, em vez de ser preenchido por dedução.

---

## 1. Contexto

O requisito veio formulado assim: *"filtrar por data é imprescindível até no MVP, porque são acervos locais de décadas de alimentação — o usuário quer achar uma foto antiga que sabe que foi tirada em 2018."*

O requisito está certo e é barato. A forma óbvia de atendê-lo está errada, e o motivo pelo qual está errada é específico exatamente da população de usuários que o projeto mira.

O banco já tem uma coluna temporal, entregue pelo RFC-020:

```python
file_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Ela é preenchida a partir de `stat().st_mtime`:

```python
file_modified_at=datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.UTC)
```

Filtrar por essa coluna atenderia a letra do pedido e responderia errado.

## 2. Problema

### 2.1 `mtime` não é quando a foto foi tirada

`st_mtime` é a última modificação do arquivo no sistema de arquivos onde ele está agora. Para um acervo fotográfico profissional, os eventos que o alteram são rotina:

| evento | o que acontece com `mtime` |
| --- | --- |
| copiar para um HD novo (`copy`, drag-and-drop, a maioria dos backups) | vira a data da cópia |
| restaurar de backup | vira a data da restauração |
| migrar de FAT32 para NTFS/exFAT | pode mudar por granularidade e fuso |
| exportar/reprocessar em outro software | vira a data da exportação |
| mover dentro do mesmo volume | tipicamente preservado |

O RFC-027 §2.3 descreve o usuário-alvo: alguém cujo acervo mora em uma gaveta de discos externos, migrado de disco em disco ao longo de anos conforme os discos enchem e morrem. **Copiar arquivos entre volumes é a operação central da vida desse acervo.** Um acervo de 2018 que passou por dois discos desde então tem `mtime` de 2022, e o filtro `2018` não devolveria nada — a resposta errada mais cara possível, porque é silenciosa e parece que a foto não existe.

O caso de uso citado no requisito — *"ele sabe que a foto é de 2018"* — é conhecimento sobre **quando a foto foi tirada**, e essa informação não está no sistema de arquivos. Está dentro do arquivo.

### 2.2 A informação já está lá e não é lida

`DateTimeOriginal` (tag EXIF `0x9003`) é gravada pela câmera no momento do disparo e viaja com o arquivo por qualquer número de cópias. Drones DJI, que o RFC-022 identificou como a fonte do domínio de validação, gravam esse campo de forma confiável, junto com GPS.

O EXIF está no cabeçalho do arquivo, e `pillow` já está em `backend/requirements.txt`. **Nenhuma dependência nova.**

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Nova coluna | `images.captured_at`, `TIMESTAMP` **sem fuso** (§5) |
| Nova coluna | `images.capture_source` — de onde a data veio, `String` e não `ENUM` nativo (§4.2) |
| Fonte primária | EXIF `DateTimeOriginal`, lida do **Exif IFD** (`0x8769`), não do IFD0 |
| Cadeia de fallback | `DateTimeOriginal` → `DateTimeDigitized` → `NULL` (§4) — **nem `mtime`, nem a tag `0x0132` `DateTime`** |
| Onde é extraída | Na varredura, junto com `stat()`, atrás de `Settings.extract_capture_date` (§6) |
| Efeito na decisão incremental | **Nenhum.** A escada de custo do RFC-020 fica intacta (§6.1) |
| Escrita para linhas puladas | **Condicional:** só quando `capture_source` é `NULL`, nos dois ramos que pulam inferência (§6.2) |
| Backfill | Mesmo caminho da varredura, sem modelo; modos `--only-unknown` e `--force` (§7) |
| Filtro na busca | `SearchFilters.captured_between: DateRange \| None` (§8) |
| Contagem de excluídos por data desconhecida | **Implementada**, `excluded_unknown_date` na resposta (§8.2) |
| Resposta HTTP | `captured_at` e `capture_source` publicados; caminho e dispositivo continuam sendo do RFC-030 (§12) |
| Índice em `captured_at` | **Não criado.** Medido, e a medição não o justifica sozinha (§8.1) |
| GPS | **Não neste RFC** (§11) |

## 4. A cadeia de fallback

```
EXIF DateTimeOriginal      →  capture_source = 'exif_original'
EXIF DateTimeDigitized     →  capture_source = 'exif_digitized'
nada utilizável            →  captured_at = NULL, capture_source = 'unknown'
```

**`mtime` não entra na cadeia**, e isso é uma reversão deliberada do rascunho original deste RFC, que o incluía como último recurso antes de desistir. §2.1 já tinha estabelecido, com evidência, que `mtime` é sistematicamente errado para este acervo — não ocasionalmente impreciso, *sistematicamente* errado, porque copiar entre discos é a operação central da vida desse acervo. Usá-lo como fallback silencioso teria devolvido exatamente o defeito que §2.1 existe para eliminar, só que disfarçado atrás de uma coluna com nome de `captured_at`.

Pelo mesmo motivo a tag EXIF `0x0132` (`DateTime`, no IFD0) também não entra: é a hora em que um software de edição salvou o arquivo — `mtime` com outra roupa. `CaptureSource` não tem membro para nenhuma das duas, e `test_there_is_no_member_for_the_filesystem_timestamp` e `test_the_ifd0_date_time_tag_is_ignored` fixam isso.

A alternativa e a decisão: **sem EXIF, `captured_at` fica `NULL`.** O sistema não afirma uma data que não pode sustentar. O custo é declarado: fotos sem EXIF ficam invisíveis a um filtro de data até ganharem uma fonte melhor — e é exatamente esse custo, tornado explícito, que §8.2 torna visível ao usuário e §11 lista como risco aceito.

O que conta como "nada utilizável", implementado em `backend/app/infrastructure/filesystem/exif_capture_date.py`:

- tag ausente, ou valor que não é texto ASCII;
- o placeholder `0000:00:00 00:00:00` e a variante em branco `"    :  :     :  :  "`, reconhecidos **por nome** e não pela exceção do `strptime`;
- qualquer outra coisa que `"%Y:%m:%d %H:%M:%S"` rejeite (mês 13, sufixo de fuso, texto truncado) — e nesse caso a cadeia **desce ao próximo elo**, não direto para `unknown`.

`NUL` e espaços de padding são removidos antes de parsear: o Pillow devolve o terminador como parte da string.

### 4.1 `NULL` significa desconhecido, como no RFC-020

O RFC-020 fixou a convenção: *"`NULL` significa desconhecido, nunca 'combina'."* Uma imagem com `captured_at = NULL` **não aparece** em uma busca filtrada por intervalo de datas, por mais largo que seja — nos três repositórios, identicamente (`test_an_unknown_capture_date_never_matches`). E a UI pode dizer quantas imagens foram excluídas por data desconhecida, senão o usuário conclui que a foto não existe; isso foi implementado e está em §8.2.

### 4.2 `NULL` e `'unknown'` são respostas diferentes

Sem `capture_source`, `captured_at` é um `timestamp` cuja confiabilidade varia por linha e não é observável. Com ela, a coluna tem três estados, e os dois primeiros **nunca** se fundem:

| valor de `capture_source` | significado |
| --- | --- |
| `NULL` | a linha **nunca foi examinada** — existe desde antes deste RFC, ou foi varrida com a extração desligada |
| `'unknown'` | foi examinada e o arquivo não tem data utilizável (`captured_at` é `NULL`) |
| `'exif_original'` / `'exif_digitized'` | a tag de onde a data veio |

Essa distinção é o que torna a escrita condicional de §6.2 possível: a varredura escreve só linhas `NULL`, e depois da primeira passada um re-scan de um acervo inalterado não escreve nada. Se "examinado, sem data" também fosse `NULL`, todo re-scan releria e reescreveria todo arquivo sem data, para sempre — e esses são a população mais numerosa dos casos ruins.

Ela também é o que dá sentido ao `--force` do backfill (§7), e a pergunta *"que fração do acervo tem data conhecida?"* vira um `GROUP BY` em vez de uma reprocessada.

`capture_source` é um `StrEnum` de Domain (`CaptureSource`) persistido como `String`: acrescentar uma fonte (`gps_derived`, `filename_parsed`) não pode custar uma migration.

### 4.3 Arquivo ilegível não é `'unknown'`

Esta regra não estava no rascunho e foi decidida na implementação. `read_capture_date()` tem **três** saídas, não duas:

- um `CaptureDate` com data e fonte EXIF;
- `CaptureDate.unknown()` quando os bytes foram lidos e não têm data — sem EXIF, placeholder, EXIF corrompido, arquivo truncado, arquivo que não é imagem;
- `None` quando o arquivo **não pôde ser lido agora** — apagado entre a listagem e a abertura, travado, permissão negada, disco removido no meio da varredura.

A distinção é a mesma de §4.2 aplicada a um caso que a regra ingênua ("todo erro vira `unknown`") colapsaria: gravar `unknown` para um arquivo apenas travado impediria toda varredura futura de olhá-lo de novo. `None` deixa a linha não examinada, e a próxima varredura tenta outra vez. A fronteira entre os dois casos é o `errno`: o Pillow reporta bytes malformados como `OSError("...")` sem `errno`, e o sistema operacional reporta falha de leitura com um (`test_a_read_error_mid_parse_is_not_examined`).

Nenhum dos três casos levanta exceção para a varredura (§13).

## 5. Fuso horário: por que `TIMESTAMP` e não `TIMESTAMPTZ`

Esta é a decisão menos óbvia do RFC e a que erraria em silêncio.

`file_modified_at` é `TIMESTAMPTZ` e está correto assim: `st_mtime` é um instante absoluto (segundos desde a época), e `TIMESTAMPTZ` é exatamente o tipo para isso.

**`DateTimeOriginal` não é um instante absoluto.** O formato EXIF é `YYYY:MM:DD HH:MM:SS`, sem fuso. É a hora local do relógio da câmera no momento do disparo. O EXIF 2.31 acrescentou `OffsetTimeOriginal` para suprir isso, mas ele é frequentemente ausente.

Armazenar isso em `TIMESTAMPTZ` obriga a inventar um fuso. Duas escolhas, ambas erradas:

| escolha | erro |
| --- | --- |
| assumir UTC | uma foto de `31/12/2018 22:00` local vira `2018-12-31T22:00Z`, que exibida em UTC-3 é **31/12 19:00** — mesmo dia por sorte, mas um voo às 23:00 em UTC-3 cai no dia seguinte |
| assumir o fuso da máquina que indexou | a mesma foto ganha datas diferentes conforme o notebook que rodou o worker |

Ambas movem fotos através da fronteira da meia-noite, e portanto **através da fronteira do ano** para disparos de 31 de dezembro.

A decisão: `captured_at` é `TIMESTAMP WITHOUT TIME ZONE`, guardando a hora local da câmera como ela foi gravada. **`captured_at` é naive em toda a pilha**, e cada camada o impõe:

| camada | como |
| --- | --- |
| extração | `strptime` sem fuso; nunca `replace(tzinfo=...)` |
| Domain | `CaptureDate`, `Image` e `DateRange` rejeitam `datetime` com `tzinfo` na construção (`InvalidCaptureDateError`, `InvalidDateRangeError`) |
| coluna | `DateTime(timezone=False)`, com um comentário no modelo explicando por que a coluna vizinha tem o tipo oposto |
| filtro | os limites do `DateRange` são naive; um limite com fuso não é convertido, é recusado |
| HTTP | um parâmetro com `Z`, offset ou timestamp Unix responde **400**; a resposta serializa `"2018-07-14T15:32:05"`, sem `Z` e sem offset |

Testes: `test_captured_at_is_a_timestamp_without_time_zone`, `test_a_naive_capture_date_survives_postgresql_unchanged`, `test_the_column_ignores_the_session_time_zone` (o valor lido não muda sob `SET TIME ZONE 'Pacific/Kiritimati'`), `test_a_bound_with_a_zone_is_refused_not_converted` e `test_captured_at_is_serialized_without_z_or_offset`.

O custo é declarado: fotos de dois fusos diferentes não são estritamente ordenáveis entre si. Se deixar de ser teórico, `OffsetTimeOriginal` já está no arquivo, e `capture_source` já distingue as linhas que precisariam ser revisitadas.

## 6. Onde a extração acontece

`DiscoveredImageFile` ganha `capture_date: CaptureDate | None`, preenchido por `FilesystemImageProvider.discover()` no mesmo laço que já chama `stat()`. Um campo e não dois, porque data e fonte são um único fato, e `CaptureDate` é onde as regras que os amarram vivem (§4.3, §5).

Isso coloca a leitura de EXIF na **varredura**, não no pipeline de embedding, e a separação importa:

- a varredura já toca todo arquivo candidato, então não há travessia nova;
- a varredura roda mesmo para arquivos que serão **pulados** pela decisão incremental, então uma imagem já indexada ganha `captured_at` sem pagar inferência;
- e a varredura é o que o RFC-027 §8 usa para o denominador do `% indexado`.

A leitura é só de cabeçalho: `Image.open()` é preguiçoso, nada chama `load()`, e o arquivo é aberto com `with`. Avisos do Pillow durante a leitura (`Corrupt EXIF data`, `DecompressionBombWarning`) são silenciados, porque o resultado já os reflete e uma varredura de acervo danificado imprimiria uma linha por arquivo.

A extração pode ser desligada com `Settings.extract_capture_date` (`EXTRACT_CAPTURE_DATE=false`). Desligada significa "não examinado" (`None`), nunca "sem data".

**Custo medido** (`measure_exif_cost.py`; Python 3.12.9, Pillow 12.3.0, Windows 10, Intel família 6 modelo 158; JPEGs com bloco EXIF real, 3 repetições após um passe de aquecimento):

| corpus | arquivos | tamanho | `stat()` mediana | `read_capture_date()` mediana | p95 | `discover()` sem / com extração |
| --- | --- | --- | --- | --- | --- | --- |
| 1920×1080 | 5.000 | 726 KB | 0,063 ms | **0,392 ms** | 0,761 ms | 0,195 / 0,710 ms por arquivo |
| 4000×3000 | 300 | 4.198 KB | 0,043 ms | **0,339 ms** | 0,697 ms | 0,137 / 0,559 ms por arquivo |

Três conclusões, e a terceira é uma ressalva:

1. **A leitura é de cabeçalho, confirmado.** Um arquivo quase seis vezes maior custa o mesmo — se `Image.open()` decodificasse pixels, o custo acompanharia o tamanho.
2. **A afirmação "ordens de magnitude abaixo" se confirma.** Mediana de 0,40 ms contra os ~450 ms/imagem de inferência do RFC-024: cerca de **1.100×**. Projetado para 100.000 arquivos, são **~40 s** de leitura de EXIF por varredura completa, contra ~12,5 h de inferência para os mesmos arquivos. O risco de §11 ("se for alto, a extração vira opcional") não se realizou nesta medição, e a opção existe mesmo assim.
3. **Não foi medida a leitura a frio de um HD externo mecânico.** Todo arquivo acima tinha acabado de ser escrito e estava no cache do sistema operacional; os números são custo de parsing, não de seek. O Windows não oferece forma sem privilégio de esvaziar o cache, e estimar uma penalidade de seek seria a dedução que este RFC recusa. É a próxima medição a fazer, com um disco real da gaveta.

A decisão pós-medição sobre extração preguiçosa (um callable em `DiscoveredImageFile`) é: **não fazer.** Nada na medição a pede, e ela tiraria o custo da varredura para onde quer que o primeiro consumidor o chamasse, que é como um custo deixa de ser visível.

### 6.1 A escada de custo do RFC-020 fica intacta

O RFC-020 fixou a ordem, do mais barato ao mais caro:

```
1. O arquivo existe?  2. mtime  3. file_size  4. SHA-256  5. embedding
```

`captured_at` **não entra nessa escada.** Não é sinal de mudança: é atributo pesquisável. Uma imagem cujo `captured_at` mudou tem exatamente os mesmos pixels, e recomputar o embedding dela seria gastar a operação mais cara do sistema por uma mudança de metadado.

Concretamente: `plan_indexing()` não ganhou parâmetro e não lê a data. O teste que fixa isso foi escrito no nível em que ele pode falhar. Variar a data no candidato não provaria nada — `plan_indexing()` nem recebe o campo, e o teste passaria por construção. O que chega à decisão é o `IndexMetadata` lido do banco, que agora carrega `capture_source` (§6.2); então `test_two_metadata_differing_only_in_capture_source_plan_identically` e os testes parametrizados de `test_indexing_plan.py` constroem metadados com `capture_source` diferente e `file_size`, `file_modified_at` e `content_hash` idênticos e exigem `SKIP_UNCHANGED` (e `REFRESH_METADATA`/`EMBED` nos outros ramos). Isso foi **verificado por mutação**: acrescentar `existing.capture_source == candidate.image.capture_source` à comparação faz quatro desses testes falharem.

### 6.2 A escrita para linhas que a decisão incremental pula

O rascunho dizia que `captured_at` "é escrito por `UPDATE` no caminho de varredura". Lido ao pé da letra, isso é um `UPDATE` por arquivo por varredura — 100.000 escritas a cada re-scan de um acervo que não mudou, destruindo a propriedade que torna o skip barato.

**Decisão: escrita condicional**, em `capture_date_to_write()` (`backend/app/application/use_cases/capture_date_plan.py`), uma função pura separada de `plan_indexing()` para que as duas decisões não possam se contaminar. Na varredura, a data é escrita só quando o `capture_source` lido do banco é `NULL`. Em regime permanente o custo de escrita é zero; na primeira varredura depois deste RFC, o acervo conectado inteiro é datado.

- **Vale para os dois ramos que pulam inferência.** `SKIP_UNCHANGED` é o óbvio; `REFRESH_METADATA` (mtime mexeu, hash bateu) é o que se esquece, e esquecê-lo deixaria sem data, para sempre, toda linha cujo arquivo foi copiado ou restaurado — as varreduras seguintes seriam `SKIP_UNCHANGED` sobre uma linha ainda sem data. `test_a_touched_identical_row_never_examined_gains_its_date` fixa isso.
- **O ramo `EMBED` não passa por aqui**: `save_indexed_many()` escreve a linha inteira a partir da entidade, data incluída. Se a extração estiver desligada, a data antiga é substituída por `NULL` — correto, porque ela foi lida de bytes que não existem mais.
- `capture_source` vem no **prefetch em lote** que já existia (`get_index_metadata_many()`), então não há consulta nova por arquivo. `IndexMetadata` documenta, em voz alta, que o campo não é sinal de mudança.
- As datas de uma janela de prefetch são escritas **num único `update_capture_date_many()`**, com degradação para escrita por linha se o lote falhar — o mesmo trade do RFC-024 §7.2.
- `update_index_metadata()` **não** escreve `capture_source`, mesmo recebendo um `IndexMetadata`: um refresh zeraria a data de uma linha examinada.
- `IndexingSummary.capture_dates_written` reporta quantas linhas foram datadas; num re-scan de acervo inalterado, é zero (`test_a_rescan_of_a_dated_collection_writes_nothing`).

**O que essa regra deliberadamente não cobre:** uma linha `'unknown'` nunca mais é reexaminada por uma varredura normal. Isso está correto enquanto o arquivo não muda — se o EXIF mudasse, os bytes mudariam, o hash mudaria e a linha cairia em `EMBED`. O caso que fica de fora é **a extração melhorar**, e é para ele que existe o `--force` de §7.

## 7. Backfill sem recomputar nada

```
python -m app.infrastructure.workers.capture_date_backfill --root PATH [--label HD2] [--dry-run] [--only-unknown | --force]
```

As linhas indexadas antes deste RFC têm `capture_source = NULL`. A próxima varredura comum do disco as data de qualquer jeito (§6.2), então o backfill não é pré-requisito de nada: é o mesmo caminho **com a indexação desligada** — o mesmo `discovered_candidates()` do worker, o mesmo prefetch por janela, a mesma `capture_date_to_write()` — sem hasher, sem modelo, sem escrita de embedding. Não é um segundo mecanismo com regras próprias.

A lógica de decisão está na Application (`BackfillCaptureDatesUseCase`); o módulo em `infrastructure/workers/` é só a raiz de composição, no molde de `device_reconcile.py`, reusando `register_device()`.

| modo | o que examina | propriedade |
| --- | --- | --- |
| `--only-unknown` (padrão) | só linhas com `capture_source` `NULL` | idempotente: a segunda execução não escreve nada |
| `--force` | também as já examinadas | existe para um caso só: a extração melhorou |

**`--force` nunca rebaixa uma fonte.** Uma fonte já afirmada só é substituída por outra igual ou mais forte na cadeia de §4 (`CaptureSource.precedence`): um `exif_original` sobrevive a um arquivo que hoje lê como `unknown`, seja por dano no disco, seja por regressão no leitor. A implementação ingênua — "releia tudo e grave o que vier" — é destrutiva de um jeito que passa em todo teste feito com arquivos saudáveis. `test_force_never_downgrades_exif_original` fixa a regra, e o sumário conta essas linhas em `kept_stronger` para o operador olhar.

Outras garantias: arquivo sem linha é contado como `not_indexed` e **nenhuma linha é criada**; arquivo ilegível fica `not_readable` e não examinado (§4.3); `--dry-run` lê tudo e não escreve nada, nem a linha do dispositivo.

**O backfill não importa o modelo, nem transitivamente**, e isso é provado no grafo de imports, não suposto por mock: `test_capture_date_backfill.py` importa o módulo — e, separadamente, todo módulo que ele importa em qualquer ponto, inclusive os imports locais de `main()` — num **interpretador novo**, e exige que `torch`, `transformers`, `langdetect`, o adaptador CLIP, o tradutor e `app.presentation.dependencies` estejam ausentes de `sys.modules`. Um terceiro teste guarda o guarda: os imports do `indexing_worker` *carregam* `torch`, então a detecção é real.

**Custo:** a medição de §6 dá ~0,40 ms por arquivo de leitura de EXIF com cache quente, o que para um HD de 40.000 fotos projeta **~16 s de leitura de EXIF** em vez das ~5 h de uma reindexação. Não foi medido: o backfill completo de um disco externo real (leitura a frio e escrita em lote no banco somadas).

Essa propriedade é o motivo de `captured_at` ser uma coluna nova em vez de um campo ligado ao ciclo de embedding: a separação entre "metadado que se corrige de graça" e "vetor que custa caro" é o que torna este RFC barato de aplicar a um acervo real.

## 8. O filtro

`SearchFilters`, introduzido pelo RFC-027 §9, ganhou um campo:

```python
@dataclass(frozen=True)
class SearchFilters:
    device_ids: frozenset[DeviceId] = frozenset()
    captured_between: DateRange | None = None      # este RFC

    def is_empty(self) -> bool:
        return not self.device_ids and self.captured_between is None
```

`is_empty()` foi atualizado junto — era para isso que ele existia (`test_a_date_filter_alone_is_not_empty`). Os dois campos filtram independentemente e se combinam com `AND`.

`DateRange` é um value object de Domain, meia-aberto `[start, end)`. Meia-abertura porque *"2018"* é `[2018-01-01, 2019-01-01)`, e essa forma não tem o problema de última hora do dia que `<= 2018-12-31` tem. Regras:

- `start` e `end` são **obrigatórios e naive**; um `tzinfo` em qualquer um deles é erro de domínio na construção. Sem isso, a comparação falharia dentro do PostgreSQL e levantaria `TypeError` nos doubles — dois comportamentos para a mesma chamada.
- `start > end` é erro; **`start == end` é um intervalo vazio válido**, que casa com nada.
- Extremo aberto é assunto da **Presentation**: `captured_from` ausente vira `datetime.min`, `captured_to` ausente vira `datetime.max`, antes de o `DateRange` ser construído. Os dois ausentes não são um intervalo infinito — são `captured_between = None`, e nenhuma cláusula é emitida, porque `[min, max)` excluiria em silêncio toda foto sem data.

No PostgreSQL a cláusula é `captured_at >= start AND captured_at < end`, que exclui `NULL` de graça (comparação com `NULL` nunca é verdadeira). Nos dois doubles em memória, `matches_filters()` precisa do `is not None` **explícito** — em Python `start <= None` levanta. Um `SearchFilters` sem `captured_between` produz a consulta do RFC-027 sem alteração, e isso é afirmado sobre o **SQL compilado**, não só sobre resultados (`test_no_filter_and_an_empty_filter_emit_the_unfiltered_query`).

Na API: `GET /api/v1/images/search?captured_from=2018-01-01&captured_to=2019-01-01`. Aceita data sozinha ou data e hora. Limite com fuso, offset ou timestamp Unix → **400** com mensagem nomeando o fuso; intervalo invertido → **400**; valor que não é data → **422** do FastAPI.

### 8.1 Data é um filtro pior que dispositivo, para o índice

O RFC-027 §9.1 estabeleceu que um filtro sobre busca vetorial aproximada precisa ser medido e não deduzido. Data é o caso mais difícil dos dois, por uma razão estrutural:

| | dispositivo | data |
| --- | --- | --- |
| cardinalidade | baixa, estável (dezenas) | alta, contínua |
| índice HNSW parcial por valor | **viável** — um índice por disco | **inviável** — não há conjunto finito de intervalos |
| seletividade | conhecida de antemão | varia com o intervalo pedido |

`experiments/rfc-028-capture-date/planner_check.py`, partindo do script do RFC-027, mede isso com `EXPLAIN ANALYZE` sobre **20.000 imagens** (vetores aleatórios unitários de 512 dimensões; 10% sem data; o resto com datas uniformes entre 2000 e 2020), pgvector 0.8.5, `hnsw.ef_search = 40`, `limit = 10`. Tudo dentro de uma transação desfeita no fim. Os 1%, 10%, 50% e 90% que este RFC nomeava foram medidos; **20%, 25% e 30% foram acrescentados** depois da primeira execução, porque um scan HNSW com pós-filtro explora 40 candidatos e só pode voltar curto quando menos de `limit / ef_search = 25%` deles sobrevivem — e nenhum dos quatro pontos originais caía nessa faixa enquanto o planejador usava o índice. Procurar o resultado curto só onde ele não pode existir teria sido concluir pela ausência.

**Plano customizado (planejado para os limites exatos), esquema como migrado:**

| intervalo | linhas que casam | plano escolhido | linhas devolvidas | tempo |
| --- | --- | --- | --- | --- |
| sem filtro | — | índice HNSW (aproximado) | 10/10 | 4,9 ms |
| 1% | 179 | varredura sequencial (**exata**) | 10/10 | 4,7 ms |
| 10% | 1.760 | varredura sequencial (**exata**) | 10/10 | 10,4 ms |
| 20% | 3.588 | índice HNSW + pós-filtro | **5/10** | 2,7 ms |
| 25% | 4.464 | índice HNSW + pós-filtro | **7/10** | 3,5 ms |
| 30% | 5.360 | índice HNSW + pós-filtro | **9/10** | 2,8 ms |
| 50% | 8.847 | índice HNSW + pós-filtro | 10/10 | 2,8 ms |
| 90% | 16.221 | índice HNSW + pós-filtro | 10/10 | 3,7 ms |

**O regime intermediário existe e foi observado nesta escala.** Diferente do filtro de dispositivo, que o RFC-027 mediu sem nenhum resultado curto, um intervalo de datas cobrindo 20–30% do acervo faz a busca devolver **metade a nove décimos do que foi pedido, sem erro nenhum**, com milhares de fotos casando o filtro. O planejador troca para a varredura exata só até ~10%; acima disso mantém o HNSW, e o pós-filtro descarta os candidatos. É exatamente o risco que o rascunho descrevia, agora com números.

**Plano genérico.** A primeira versão do script misturou planos por acidente: o psycopg prepara uma instrução no servidor depois de cinco execuções do mesmo texto, e o PostgreSQL pode então usar um plano *genérico*, custeado sem conhecer os limites. O repositório manda exatamente isso — um texto SQL para toda busca com data. Medido com `plan_cache_mode = force_generic_plan`, o plano genérico escolheu **varredura sequencial em todas as faixas** e devolveu **10/10 em todas**, mais devagar (5,4 ms a 1%, 22,8 ms a 30%, 73,2 ms a 90%). Ou seja: o mesmo pedido pode ser curto numa conexão nova e exato numa conexão reaproveitada. **Não foi medido** se `plan_cache_mode = auto` de fato troca para o plano genérico numa conexão de pool de longa duração.

**Com um B-tree em `captured_at`** (criado e desfeito dentro da transação):

| intervalo | plano customizado | linhas | plano genérico | linhas |
| --- | --- | --- | --- | --- |
| 1% | B-tree (exato), 2,1 ms | 10/10 | B-tree (exato), 3,3 ms | 10/10 |
| 10% | B-tree (exato), 7,8 ms | 10/10 | B-tree (exato), 15,8 ms | 10/10 |
| 20% | B-tree (exato), 14,2 ms | 10/10 | B-tree (exato), 29,2 ms | 10/10 |
| 25% | HNSW + pós-filtro, 3,5 ms | **7/10** | B-tree (exato), 17,7 ms | 10/10 |
| 30% | HNSW + pós-filtro, 4,1 ms | **9/10** | B-tree (exato), 19,1 ms | 10/10 |
| 50% | HNSW + pós-filtro, 3,8 ms | 10/10 | B-tree (exato), 30,1 ms | 10/10 |
| 90% | HNSW + pós-filtro, 4,7 ms | 10/10 | B-tree (exato), 59,7 ms | 10/10 |

O índice empurra a faixa exata de ~10% para ~20% e acelera a contagem de §8.2 (8,3 ms → 1,9 ms), **mas não elimina o resultado curto**: a 25% e 30% o planejador ainda prefere o HNSW.

**Decisão: nenhuma mitigação adotada neste RFC, nem o índice.** O B-tree não resolve o problema que motivaria criá-lo, e as mitigações que o resolvem de fato — `hnsw.iterative_scan` (disponível no pgvector 0.8.5 instalado) ou `ef_search` maior quando há filtro — trocam latência por recall e mudam o comportamento de toda busca filtrada, inclusive por dispositivo. É uma decisão com medição própria, e fica registrada como o risco técnico aberto deste RFC (§11). O contrato de `search_similar` continua dizendo, como desde o RFC-025, que um resultado curto é possível sob índice aproximado.

**O que ficou por medir**, dito em vez de deduzido:

- **acervo maior que 20.000.** O ponto em que o planejador abandona a varredura exata depende do tamanho da tabela; a faixa curta pode se deslocar ou alargar. `RFC028_CORPUS_SIZE` existe para isso;
- **datas correlacionadas com conteúdo.** No corpus sintético, data e vetor são independentes. Num acervo real as fotos de uma mesma data tendem a ser parecidas (a mesma sessão, o mesmo lugar), o que pode tornar o pós-filtro melhor ou pior — não se sabe qual;
- **`plan_cache_mode = auto` em produção**, como dito acima;
- a variação entre corpora: antes de os ids serem semeados, três execuções sobre corpora diferentes só nos UUIDs devolveram 6, 7 e 8 linhas para o mesmo intervalo de 20%. O número exato de linhas faltantes depende do grafo; a existência da faixa curta se repetiu em todas as execuções.

**O filtro não é justificado por velocidade.** O pedido original supôs que filtrar por data aceleraria as consultas; o mecanismo real é o oposto na faixa intermediária. A justificativa do filtro é utilidade: *"eu sei que é de 2018"* é a informação mais forte que o usuário tem, e ela é ortogonal ao que o CLIP sabe.

### 8.2 Quantas fotos o filtro escondeu por não ter data

§4.1 exigia que a UI pudesse dizer quantas imagens foram excluídas por data desconhecida, e o rascunho não entregava esse número em lugar nenhum. **Foi implementado.**

A resposta da busca ganhou `excluded_unknown_date`, alimentado por `ImageRepository.count_unknown_capture_date(filters)` através de `SearchImagesUseCase.count_hidden_by_unknown_date()`: um `COUNT(*)` das imagens **com embedding**, sob **as demais cláusulas do filtro** (hoje, dispositivos), com `captured_at IS NULL` — as nunca examinadas e as examinadas sem data.

- É uma **segunda consulta**, não subproduto da busca: a busca vetorial devolve no máximo `limit` linhas e não sabe o que o `WHERE` descartou. Medida em 8,3 ms sobre 20.000 imagens (§8.1).
- Conta sobre **a tabela inteira sob os filtros**, não sobre a vizinhança que o HNSW explorou. É a pergunta do usuário ("quantas fotos o filtro escondeu"), e deliberadamente não é o universo do ranking; os dois números não somam nada. O port documenta isso para que ninguém "corrija" a divergência.
- Sem `captured_between`, **a consulta não existe**: o campo vem `null` — não `0`, porque `0` diria "o filtro de data não escondeu nada" e `null` diz "não houve filtro de data". `test_a_search_without_a_date_range_never_triggers_the_count` prova no nível do use case que o repositório nem é chamado.

## 9. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Filtrar por `file_modified_at` | Sistematicamente errado para a população-alvo, e erra em silêncio (§2.1) |
| `captured_at` como `TIMESTAMPTZ` | Obriga a inventar um fuso; move fotos através da fronteira do ano (§5) |
| Assumir o fuso da máquina que indexou | A mesma foto ganha datas diferentes por notebook (§5) |
| Converter um limite com fuso em vez de recusá-lo | Exige escolher um fuso de destino, que é a pergunta sem resposta de §5 |
| Não guardar `capture_source` | A confiabilidade passa a variar por linha sem ser observável (§4.2) |
| Gravar "examinado, sem data" como `NULL` | Todo re-scan releria e reescreveria todo arquivo sem data, para sempre (§4.2) |
| Gravar `unknown` para arquivo ilegível | Um arquivo apenas travado nunca mais seria examinado (§4.3) |
| Ler EXIF no pipeline de embedding | Perde as imagens puladas pela decisão incremental, que são a maioria (§6) |
| `captured_at` como sinal de reindexação | Gasta a operação mais cara do sistema por uma mudança de metadado (§6.1) |
| `UPDATE` de data para todo arquivo em toda varredura | 100.000 escritas por re-scan de acervo inalterado (§6.2) |
| Condicionar só o ramo `SKIP_UNCHANGED` | Toda linha copiada ou restaurada fica sem data para sempre (§6.2) |
| Backfill via reindexação completa | ~5 h por disco contra ~16 s de leitura de EXIF medida com cache quente (§7) |
| `--force` que reescreve tudo o que relê | Apaga datas reais quando um arquivo lê pior hoje (§7) |
| Campos de data só na persistência, num `SearchHit` estendido | Os doubles precisariam de um dicionário paralelo só para filtrar — a divergência que o teste de contrato existe para impedir |
| `DateRange` com extremos opcionais | Todo consumidor trataria um `None`; os extremos abertos são resolvidos na Presentation (§8) |
| `captured_between` ausente como `[min, max)` | Exclui toda foto sem data e muda a consulta não filtrada (§8) |
| `NULL` tratado como "combina" | Contradiz o RFC-020; devolveria fotos sem data em toda busca datada (§4.1) |
| `mtime` ou a tag `0x0132` como fallback | Reintroduziria o defeito de §2.1 disfarçado atrás do nome `captured_at` (§4) |
| Intervalo fechado `<= fim` | O bug de última hora do dia; meia-abertura não tem (§8) |
| Índice B-tree em `captured_at` nesta migration | Medido: não elimina o resultado curto (§8.1) |
| Adotar `hnsw.iterative_scan` ou `ef_search` maior aqui | Muda toda busca filtrada; precisa de medição própria de latência × recall (§8.1) |
| Extração preguiçosa na varredura | A medição não a pede, e esconderia o custo (§6) |
| Extrair GPS junto | Escopo próprio, e nada consome coordenadas ainda (§11) |

## 10. Não-objetivos

- GPS, altitude, rumo, modelo de câmera e o resto do EXIF (§11)
- Timeline, agrupamento por evento, histograma de datas
- Correção de relógio de câmera (fotos com data claramente errada)
- `OffsetTimeOriginal` e normalização entre fusos (§5)
- Reescrever EXIF de qualquer arquivo — o sistema **nunca escreve** no acervo
- Filtro por tamanho, extensão, orientação ou qualquer outro metadado
- Ordenação por data como alternativa ao ranking semântico (o ranking continua sendo similaridade)
- Indexação seletiva por ano — RFC-029, que é quem torna o requisito original completo

## 11. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **Filtro de data devolve menos que `limit` sob HNSW** (§8.1) | **Confirmado** a 20–30% de seletividade sobre 20.000 imagens (5/10 a 9/10). Nenhuma mitigação adotada; `hnsw.iterative_scan` é a candidata, a medir. Escala maior e datas correlacionadas não medidas |
| **O mesmo pedido pode ter plano diferente conforme a conexão** (§8.1) | Plano genérico é exato e mais lento; se `auto` o escolhe em produção não foi medido |
| **Fotos sem EXIF ficam invisíveis sob filtro de data** (§4) | Deliberado. Tornado visível: `excluded_unknown_date` na resposta (§8.2) |
| **`TIMESTAMP` sem fuso não ordena entre fusos** (§5) | Declarado; `capture_source` marca as linhas revisitáveis |
| **Relógio de câmera errado produz data errada com alta confiança** | Não tratado. `capture_source = 'exif_original'` afirma a origem, não a correção |
| **Custo de EXIF em 100.000 arquivos** (§6) | Medido com cache quente: ~40 s por varredura. Leitura a frio de disco externo não medida |
| **Imagens acima de 2× o limite de pixels do Pillow** voltam `unknown` mesmo com EXIF | Declarado no código; afeta ortomosaicos muito grandes, que o `--force` pode revisitar se a leitura mudar |
| Backfill exige o dispositivo conectado (§7) | Consequência do RFC-027, não deste |

Trabalho futuro: medir e decidir a mitigação de §8.1; medir a leitura a frio num disco da gaveta; extração de GPS — está no mesmo cabeçalho e, para fotografia aérea, habilita *"fotos tiradas perto daqui"*, um filtro tão forte quanto data. Não entra aqui porque nada consumiria coordenadas ainda, e um dado indexado sem leitor é o `minimum_similarity` do RFC-025 §13.2 de novo.

## 12. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/value_objects/date_range.py` | `DateRange`, meia-aberto, naive (§8) |
| `backend/app/domain/value_objects/capture_source.py` | `CaptureSource` e a precedência da cadeia (§4.2, §7) |
| `backend/app/domain/value_objects/capture_date.py` | `CaptureDate` e as regras que amarram data e fonte (§4.3, §5) |
| `backend/app/domain/exceptions/capture_date_errors.py` | `InvalidCaptureDateError`, `InvalidDateRangeError` |
| `backend/app/application/use_cases/capture_date_plan.py` | `capture_date_to_write()`, a escrita condicional (§6.2, §7) |
| `backend/app/application/use_cases/backfill_capture_dates.py` | `BackfillCaptureDatesUseCase` (§7) |
| `backend/app/infrastructure/filesystem/exif_capture_date.py` | Extração e a cadeia de fallback (§4) |
| `backend/app/infrastructure/workers/capture_date_backfill.py` | O comando de §7 |
| `backend/alembic/versions/c5d1e8f24a90_add_image_capture_date.py` | As duas colunas, `downgrade()` real |
| `backend/tests/infrastructure/filesystem/test_exif_capture_date.py` | Cadeia, placeholders, padding, EXIF ausente/corrompido/truncado, arquivo ilegível |
| `backend/tests/infrastructure/persistence/test_capture_date_contract.py` | Escrita e leitura da data, nos três repositórios |
| `backend/tests/infrastructure/workers/test_capture_date_backfill.py` | CLI, ponta a ponta, e o grafo de imports sem modelo |
| `backend/tests/application/test_indexing_plan.py` | §6.1 no nível de `IndexMetadata`, e a política de escrita |
| `backend/tests/application/test_backfill_capture_dates.py` | Os dois modos, dry-run, nunca rebaixar |
| `backend/tests/domain/test_date_range.py`, `test_capture_date.py`, `test_search_filters.py` | Value objects e `is_empty()` |
| `experiments/rfc-028-capture-date/measure_exif_cost.py` (+ `.log`) | A medição de §6 |
| `experiments/rfc-028-capture-date/planner_check.py` (+ `.log`) | A medição de §8.1 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/domain/entities/image.py` | `captured_at`, `capture_source` (padrão `None`), fora da igualdade |
| `backend/app/domain/value_objects/search_filters.py` | `captured_between`; `is_empty()` |
| `backend/app/domain/value_objects/index_metadata.py` | `capture_source`, documentado como não sinal de mudança |
| `backend/app/domain/repositories/image_repository.py` | `update_capture_date`, `update_capture_date_many`, `count_unknown_capture_date`; contrato do filtro de data |
| `backend/app/application/use_cases/index_or_update_images.py` | Escrita condicional nos dois ramos; `capture_dates_written`; `windowed()` público |
| `backend/app/application/use_cases/index_or_update_image.py` | A mesma política no caminho de uma imagem |
| `backend/app/application/use_cases/search_images.py` | `count_hidden_by_unknown_date()` |
| `backend/app/infrastructure/database/models/image_model.py` | `captured_at` (`timezone=False`), `capture_source` |
| `backend/app/infrastructure/filesystem/discovered_image_file.py` | `capture_date` |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | Extração no laço de `stat()`, atrás de `extract_capture_date` |
| `backend/app/infrastructure/config/settings.py` | `extract_capture_date` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | `WHERE` temporal, contagem, escritas em lote, prefetch com `capture_source` |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Idem, com `is not None` explícito |
| `backend/tests/application/fakes.py` | **O terceiro repositório**, que o rascunho não listava: `FakeImageRepository` com as mesmas regras |
| `backend/app/infrastructure/workers/indexing_worker.py` | `discovered_candidates()` compartilhado com o backfill; extração configurável |
| `backend/app/presentation/api/v1/routers/images.py` | `captured_from` / `captured_to` |
| `backend/app/presentation/schemas/search_schema.py` | `captured_at`, `capture_source`, `excluded_unknown_date` |
| `backend/tests/infrastructure/persistence/test_search_similar_contract.py` | Filtro temporal e contagem, nos três repositórios |
| `ARCHITECTURE.md` | §15 (tabela `Images`) e §16 (indexação incremental) |

**Nota sobre a resposta HTTP e o RFC-030.** O docstring da rota dizia que *"o RFC-030 é dono do formato da resposta"*. A resolução: `captured_at` e `capture_source` foram publicados agora, porque são atributos da fotografia — iguais para todo chamador e toda consulta — e são o que torna legível um resultado filtrado por data ("por que esta foto apareceu, e quanto vale essa data"). Nada de caminho, dispositivo ou estado de conexão foi publicado; isso continua sendo do RFC-030.

**Nota sobre onde a data viaja.** A data foi para a entidade `Image`, e não para `IndexCandidate`/`IndexingRecord` como campos próprios. `file_size`, `file_modified_at` e `content_hash` são sinais de mudança e viajam naqueles portadores; a data é atributo pesquisável, precisa chegar à resposta via `SearchHit` e ser visível ao predicado de filtro dos doubles. Candidato e registro carregam a imagem, e com ela a data — e manter os campos de `IndexCandidate` restritos aos sinais que `plan_indexing()` compara é, por si, uma proteção de §6.1.

## 13. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | **917 passados**, 58 desmarcados — a partir de 625 |
| `pytest -m slow` | **58 passados** |
| `black --check .` / `ruff check .` | **limpos** |
| `mypy` | **0 erros em 157 arquivos** |
| `alembic heads` | **head único**, `c5d1e8f24a90` |
| `alembic downgrade b8e4d2a13c75` / `upgrade head` | **exercitado com dados**: 20 linhas com embedding, `md5` do conjunto de embeddings idêntico antes, depois do downgrade e depois do upgrade; as duas colunas somem no downgrade e voltam `NULL` ("nunca examinada") no upgrade — as datas moram nos arquivos e a próxima varredura as relê |
| Backfill não importa nem instancia o modelo de embedding | **passa** — `TestTheModelIsNeverLoaded`, sobre o grafo de imports num interpretador novo |
| Mudar a fonte da data não torna a linha candidata a reindexação | **passa** — `test_two_metadata_differing_only_in_capture_source_plan_identically`, verificado por mutação |
| Linha `REFRESH_METADATA` nunca examinada ganha data | **passa** — `test_a_touched_identical_row_never_examined_gains_its_date` |
| `--force` não rebaixa `exif_original` | **passa** — `test_force_never_downgrades_exif_original` |
| EXIF corrompido não derruba a varredura | **passa** — degrada para `unknown` (`test_a_corrupt_file_does_not_stop_the_scan`, `TestDamagedFilesNeverRaise`) |
| `captured_at = NULL` nunca aparece sob filtro, nos três repositórios | **passa** — `test_an_unknown_capture_date_never_matches` |
| Coluna sem fuso; naive faz round trip inalterado | **passa** — `test_a_naive_capture_date_survives_postgresql_unchanged` |
| JSON sem `Z` e sem offset | **passa** — `test_captured_at_is_serialized_without_z_or_offset` |
| Busca sem data emite a consulta não filtrada e não dispara a contagem | **passa** — `test_no_filter_and_an_empty_filter_emit_the_unfiltered_query`, `test_a_search_without_a_date_range_never_triggers_the_count` |
| Custo de EXIF por arquivo | **mediana 0,39–0,40 ms**, cache quente; ~40 s por 100.000 arquivos (§6) |
| Seletividade × plano de consulta | **medido** sobre 20.000 imagens; resultado curto confirmado a 20–30% (§8.1) |
