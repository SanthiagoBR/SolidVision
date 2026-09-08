# RFC-028 — Data de Captura e Filtro Temporal

**Status:** Proposto
**Depende de:** RFC-020 (metadados incrementais), RFC-021 (worker), RFC-024 (pipeline), RFC-025 (busca), RFC-027 (`SearchFilters`, dispositivos)
**Migration:** sim — `images.captured_at`, `images.capture_source`
**Medição:** `experiments/rfc-028-capture-date/` — `TBM`

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Nada neste documento foi medido ainda.

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

O SolidVision já abre cada arquivo com Pillow para decodificar e alimentar o CLIP. O EXIF está no cabeçalho do mesmo arquivo, e `pillow` já está em `backend/requirements.txt`. **Nenhuma dependência nova, e nenhuma leitura de disco que já não aconteça.**

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Nova coluna | `images.captured_at`, `TIMESTAMP` **sem fuso** (§5) |
| Nova coluna | `images.capture_source` — de onde a data veio (§4.2) |
| Fonte primária | EXIF `DateTimeOriginal` |
| Cadeia de fallback | `DateTimeOriginal` → `DateTimeDigitized` → `NULL` (§4) — **mtime não entra** |
| Onde é extraída | Na varredura, junto com `stat()` — antes e independente da inferência (§6) |
| Efeito na decisão incremental | **Nenhum.** A escada de custo do RFC-020 fica intacta (§6.1) |
| Backfill | Sem recomputar embedding nenhum (§7) |
| Filtro na busca | `SearchFilters.captured_between` (§8) |
| GPS | Extraído? **Não neste RFC** (§11) |
| Migration | Sim, duas colunas, ambas `NULL` |

## 4. A cadeia de fallback

```
EXIF DateTimeOriginal      →  capture_source = 'exif_original'
EXIF DateTimeDigitized     →  capture_source = 'exif_digitized'
nada                       →  captured_at = NULL, capture_source = 'unknown'
```

**`mtime` não entra na cadeia**, e isso é uma reversão deliberada do rascunho original deste RFC, que o incluía como último recurso antes de desistir. §2.1 já tinha estabelecido, com evidência, que `mtime` é sistematicamente errado para este acervo — não ocasionalmente impreciso, *sistematicamente* errado, porque copiar entre discos é a operação central da vida desse acervo. Usá-lo como fallback silencioso teria devolvido exatamente o defeito que §2.1 existe para eliminar, só que disfarçado atrás de uma coluna com nome de `captured_at`: um usuário filtrando por 2018 receberia fotos rotuladas `2018` cuja única fonte é a data em que alguém copiou o arquivo, sem forma de saber que não é a data do disparo a menos que leia `capture_source` — e nada no filtro em si distingue as duas.

A alternativa e a decisão: **sem EXIF, `captured_at` fica `NULL`.** O sistema não afirma uma data que não pode sustentar. Uma foto sem `DateTimeOriginal` nem `DateTimeDigitized` não aparece em nenhum filtro por intervalo — comportamento idêntico ao de qualquer outra causa de `NULL`, sem um caso especial "mas eu tenho uma pista fraca" a implementar ou explicar. O custo é declarado: fotos sem EXIF (comum em capturas antigas ou reexportadas por software que descarta metadados) ficam invisíveis a um filtro de data até algum dia ganharem uma fonte melhor — e é exatamente esse custo, tornado explícito, que §11 lista como risco aceito em vez de escondido atrás de um mtime emprestado.

### 4.1 `NULL` significa desconhecido, como no RFC-020

O RFC-020 fixou a convenção: *"`NULL` significa desconhecido, nunca 'combina'."* Uma imagem com `captured_at = NULL` **não aparece** em uma busca filtrada por intervalo de datas, e isso é deliberado — não se pode afirmar que uma data desconhecida cai dentro de um intervalo. A UI precisa poder dizer quantas imagens foram excluídas por data desconhecida, senão o usuário conclui que a foto não existe (§10).

### 4.2 Por que `capture_source` é uma coluna e não um detalhe

Sem ela, `captured_at` é um `timestamp` cuja confiabilidade varia por linha e não é observável. Com ela:

- a UI pode distinguir *"data da câmera"* de *"data digitalizada"* em vez de mostrar as duas como se fossem a mesma certeza;
- um backfill futuro sabe quais linhas vale a pena revisitar caso uma fonte melhor apareça (todas as `unknown`, nunca as `exif_original`);
- e a pergunta *"que fração do acervo tem data conhecida?"* vira um `GROUP BY` em vez de uma reprocessada.

Custa um `TEXT` curto por linha e responde três perguntas que de outro modo exigiriam reler todos os arquivos.

## 5. Fuso horário: por que `TIMESTAMP` e não `TIMESTAMPTZ`

Esta é a decisão menos óbvia do RFC e a que erraria em silêncio.

`file_modified_at` é `TIMESTAMPTZ` e está correto assim: `st_mtime` é um instante absoluto (segundos desde a época), e `TIMESTAMPTZ` é exatamente o tipo para isso.

**`DateTimeOriginal` não é um instante absoluto.** O formato EXIF é `YYYY:MM:DD HH:MM:SS`, sem fuso. É a hora local do relógio da câmera no momento do disparo. O EXIF 2.31 acrescentou `OffsetTimeOriginal` para suprir isso, mas ele é frequentemente ausente — inclusive em equipamentos que gravam `DateTimeOriginal` perfeitamente.

Armazenar isso em `TIMESTAMPTZ` obriga a inventar um fuso. Duas escolhas, ambas erradas:

| escolha | erro |
| --- | --- |
| assumir UTC | uma foto de `31/12/2018 22:00` local vira `2018-12-31T22:00Z`, que exibida em UTC-3 é **31/12 19:00** — mesmo dia por sorte, mas um voo às 23:00 em UTC-3 cai no dia seguinte |
| assumir o fuso da máquina que indexou | a mesma foto ganha datas diferentes conforme o notebook que rodou o worker |

Ambas movem fotos através da fronteira da meia-noite, e portanto **através da fronteira do ano** para disparos de 31 de dezembro. O filtro `2018` passa a incluir ou excluir fotos por um artefato de fuso.

A decisão: `captured_at` é `TIMESTAMP WITHOUT TIME ZONE`, guardando a hora local da câmera como ela foi gravada. A pergunta que o usuário faz — *"fotos de 2018"*, *"daquela tarde"* — é feita em hora local da câmera, e é respondida sem conversão nenhuma.

O custo é declarado: fotos de dois fusos diferentes não são estritamente ordenáveis entre si. Para um acervo de fotografia aérea regional isso é teórico; se deixar de ser, `OffsetTimeOriginal` já está no arquivo para quem quiser resolver depois, e `capture_source` já distingue as linhas que precisariam ser revisitadas.

## 6. Onde a extração acontece

`DiscoveredImageFile` ganha `captured_at` e `capture_source`, preenchidos por `FilesystemImageProvider.discover()` no mesmo laço que já chama `stat()`.

Isso coloca a leitura de EXIF na **varredura**, não no pipeline de embedding, e a separação importa:

- a varredura já toca todo arquivo candidato, então não há travessia nova;
- a varredura roda mesmo para arquivos que serão **pulados** pela decisão incremental, então uma imagem já indexada ganha `captured_at` sem pagar inferência;
- e a varredura é o que o RFC-027 §8 usa para o denominador do `% indexado`, então a informação chega junto de quem já a esperava.

Custo por arquivo: `TBM`. A expectativa é que a leitura de EXIF via `Image.open()` + `getexif()` seja dominada pela abertura do arquivo e fique ordens de magnitude abaixo dos ~450 ms/imagem de inferência que o RFC-024 mediu — mas *expectativa* não é medição, e a varredura é a operação que atravessa 100.000 arquivos, então um custo por arquivo pequeno ainda pode ser um custo total grande. A medição é obrigatória e o número entra aqui.

### 6.1 A escada de custo do RFC-020 fica intacta

O RFC-020 fixou a ordem, do mais barato ao mais caro:

```
1. O arquivo existe?  2. mtime  3. file_size  4. SHA-256  5. embedding
```

`captured_at` **não entra nessa escada.** Não é sinal de mudança: é atributo pesquisável. Uma imagem cujo `captured_at` mudou (porque a extração melhorou, ou porque alguém editou o EXIF) tem exatamente os mesmos pixels, e recomputar o embedding dela seria gastar a operação mais cara do sistema por uma mudança de metadado.

Concretamente: `captured_at` é escrito por `UPDATE` no caminho de varredura, e nunca é consultado por `plan_indexing()`. Um teste fixa isso — mudar `captured_at` de uma linha indexada não a torna candidata a reindexação.

## 7. Backfill sem recomputar nada

As linhas já indexadas têm `captured_at = NULL`. Preenchê-las exige ler o EXIF de cada arquivo, o que exige o arquivo presente — e portanto, para um acervo em disco externo, o dispositivo conectado (RFC-027 §7).

O backfill é uma varredura comum com a indexação desligada: percorre, lê EXIF, escreve em lote, **não instancia o modelo**. Um HD de 40.000 fotos custa `TBM` em vez das ~5 horas que uma reindexação custaria.

Essa propriedade é o motivo de `captured_at` ser uma coluna nova em vez de um campo dentro de alguma estrutura já existente ligada ao ciclo de embedding: a separação entre "metadado que se corrige de graça" e "vetor que custa caro" é o que torna este RFC barato de aplicar a um acervo real.

## 8. O filtro

`SearchFilters`, introduzido pelo RFC-027 §9, ganha um campo:

```python
@dataclass(frozen=True)
class SearchFilters:
    device_ids: frozenset[DeviceId] = frozenset()
    captured_between: DateRange | None = None      # este RFC
```

`DateRange` é um value object de Domain com meia-abertura `[início, fim)` e validação de que o início não é posterior ao fim. Meia-abertura porque *"2018"* é `[2018-01-01, 2019-01-01)`, e essa forma não tem o problema de última-hora-do-dia que `<= 2018-12-31` tem.

Um `SearchFilters` sem `captured_between` produz a consulta do RFC-027 sem alteração, e os testes de contrato existentes continuam descrevendo o caminho não filtrado.

### 8.1 Data é um filtro pior que dispositivo, para o índice

O RFC-027 §9.1 estabeleceu que um filtro sobre busca vetorial aproximada precisa ser medido e não deduzido. Data é o caso mais difícil dos dois, e por uma razão estrutural:

| | dispositivo | data |
| --- | --- | --- |
| cardinalidade | baixa, estável (dezenas) | alta, contínua |
| índice HNSW parcial por valor | **viável** — um índice por disco | **inviável** — não há conjunto finito de intervalos |
| seletividade | conhecida de antemão | varia com o intervalo pedido |

A mitigação que o RFC-027 §9.1 lista como mais limpa para dispositivo simplesmente não existe para data. Restam `hnsw.iterative_scan` (pgvector ≥ 0,8) e elevar `ef_search` quando há filtro — ambos custam latência, ambos a medir.

O regime de risco continua sendo o intermediário: um intervalo que corta o acervo pela metade é grande demais para o planejador abandonar o HNSW e seletivo demais para o pós-filtro não descartar boa parte do que o índice devolveu, produzindo **menos que `limit` resultados sem erro nenhum**.

`experiments/rfc-028-capture-date/planner_check.py` mede intervalos de seletividade 1%, 10%, 50% e 90% sobre um corpus sintético grande o bastante para o planejador escolher o índice — o RFC-025 §7.1 já registrou que 45 linhas não servem para isso. Resultado: `TBM`.

**O filtro não é justificado por velocidade.** O pedido original supôs que filtrar por data aceleraria as consultas; o mecanismo real é o oposto na faixa intermediária, e onde há ganho ele vem de o planejador cair para varredura exata sobre um subconjunto pequeno — o que é rápido *e* mais exato que o HNSW, mas por uma razão diferente da suposta. A justificativa do filtro é utilidade: *"eu sei que é de 2018"* é a informação mais forte que o usuário tem, e ela é ortogonal ao que o CLIP sabe.

## 9. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Filtrar por `file_modified_at` | Sistematicamente errado para a população-alvo, e erra em silêncio (§2.1) |
| `captured_at` como `TIMESTAMPTZ` | Obriga a inventar um fuso; move fotos através da fronteira do ano (§5) |
| Assumir o fuso da máquina que indexou | A mesma foto ganha datas diferentes por notebook (§5) |
| Não guardar `capture_source` | A confiabilidade passa a variar por linha sem ser observável (§4.2) |
| Ler EXIF no pipeline de embedding | Perde as imagens puladas pela decisão incremental, que são a maioria (§6) |
| `captured_at` como sinal de reindexação | Gasta a operação mais cara do sistema por uma mudança de metadado (§6.1) |
| Backfill via reindexação completa | ~5 h por disco contra `TBM`, para um dado que não depende do modelo (§7) |
| `NULL` tratado como "combina" | Contradiz o RFC-020; devolveria fotos sem data em toda busca datada (§4.1) |
| `mtime` como último fallback antes de `NULL` | Rascunho original deste RFC. Reintroduziria o defeito de §2.1 disfarçado atrás do nome `captured_at`, e nada no filtro distinguiria uma data de câmera de uma data de cópia (§4) |
| Intervalo fechado `<= fim` | O bug de última hora do dia; meia-abertura não tem (§8) |
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
| **Filtro de data é hostil ao HNSW e não tem mitigação por índice parcial** (§8.1) | A medir; é o risco técnico central deste RFC |
| **Fotos sem EXIF ficam invisíveis sob filtro de data** (§4) | Deliberado, e mais amplo do que no rascunho anterior deste RFC — não há mais fallback para `mtime`. A UI precisa reportar a contagem de excluídos por data desconhecida |
| **`TIMESTAMP` sem fuso não ordena entre fusos** (§5) | Declarado; `capture_source` marca as linhas revisitáveis |
| **Relógio de câmera errado produz data errada com alta confiança** | Não tratado. `capture_source = 'exif_original'` afirma a origem, não a correção |
| **Custo de EXIF em 100.000 arquivos** (§6) | `TBM`. Se for alto, a extração vira opcional na varredura |
| Backfill exige o dispositivo conectado (§7) | Consequência do RFC-027, não deste |

Trabalho futuro: extração de GPS — está no mesmo cabeçalho, sai praticamente de graça, e para fotografia aérea habilita *"fotos tiradas perto daqui"*, que é um filtro tão forte quanto data e igualmente ortogonal ao CLIP. Não entra aqui porque nada consumiria coordenadas ainda, e um dado indexado sem leitor é o `minimum_similarity` do RFC-025 §13.2 de novo.

## 12. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/value_objects/date_range.py` | `DateRange`, meia-aberta (§8) |
| `backend/app/infrastructure/filesystem/exif_capture_date.py` | Extração e a cadeia de fallback (§4) |
| `backend/app/infrastructure/workers/capture_date_backfill.py` | O backfill de §7 |
| `backend/alembic/versions/*_add_image_capture_date.py` | |
| `backend/tests/infrastructure/filesystem/test_exif_capture_date.py` | Cadeia de fallback, EXIF ausente, EXIF corrompido |
| `experiments/rfc-028-capture-date/planner_check.py` | A medição de §8.1 |
| `experiments/rfc-028-capture-date/measure_exif_cost.py` | A medição de §6 |
| `docs/rfcs/rfc-028-data-de-captura-e-filtro-temporal.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/database/models/image_model.py` | `captured_at`, `capture_source` |
| `backend/app/infrastructure/filesystem/discovered_image_file.py` | Os dois campos novos |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | Chama a extração no laço de `stat()` |
| `backend/app/domain/value_objects/search_filters.py` | `captured_between` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | O `WHERE` temporal |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Idem |
| `backend/app/presentation/api/v1/routers/images.py` | Query params `captured_from` / `captured_to` |
| `backend/app/presentation/schemas/search_schema.py` | `captured_at` e `capture_source` na resposta |
| `backend/tests/infrastructure/persistence/test_search_similar_contract.py` | Casos de filtro temporal |
| `ARCHITECTURE.md` | §15, tabela `Images` |

## 13. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | `TBM` |
| `pytest -m slow` | `TBM` |
| `black --check .` / `ruff check .` | `TBM` |
| `mypy` | `TBM` — **0 erros em código novo** é o critério |
| `alembic heads` | `TBM` — head único |
| `alembic downgrade`/`upgrade` | `TBM` |
| Backfill não instancia o modelo de embedding | `TBM` — o teste que fixa §7 |
| Mudar `captured_at` não torna a linha candidata a reindexação | `TBM` — o teste que fixa §6.1 |
| EXIF corrompido não derruba a varredura | `TBM` — degrada para o fallback seguinte |
| Custo de EXIF por arquivo | `TBM` (§6) |
| Seletividade × plano de consulta | `TBM` (§8.1) |
