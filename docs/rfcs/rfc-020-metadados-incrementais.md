# RFC-020 — Metadados Incrementais

**Status:** Implementado (estendido pelo RFC-024 com `content_hash`)
**Sprint:** 3
**Depende de:** RFC-017 (modelo), RFC-018 (migrations), RFC-019 (repositório)
**Bloqueia:** RFC-021 (worker), RFC-024 (pipeline)
**Commit:** `edc31d3`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Indexar uma imagem custa uma inferência do modelo — a operação mais cara do sistema. Reindexar 100 mil fotos das quais 3 mudaram deve custar 3 inferências, não 100 mil.

Para isso o banco precisa lembrar **o que o sistema de arquivos dizia** da última vez. Sem essa memória, não existe indexação incremental: toda execução é uma execução completa.

## 2. Decisão

Persistir dois campos de metadados de arquivo por imagem, e comparar antes de embutir:

| Coluna | Tipo | Por quê |
|---|---|---|
| `file_size` | `BIGINT` | mudança de tamanho é sinal barato e forte de alteração |
| `file_modified_at` | `TIMESTAMPTZ` | mtime é o primeiro sinal, e é obtido junto com o `stat()` da varredura |

Sequência definida em `ARCHITECTURE.md` §16, deliberadamente do mais barato ao mais caro:

```
1. O arquivo existe?
2. Comparar file_modified_at.
3. Comparar file_size.
4. Só se necessário, calcular SHA-256.
5. Só então, gerar embedding.
```

*"Never calculate hashes before timestamp and size checks."*

## 3. O que foi entregue

### 3.1 Migration `cbd5647f61b7`

```python
op.add_column("images", sa.Column("file_size", sa.BigInteger(), nullable=True))
op.add_column("images", sa.Column("file_modified_at", sa.DateTime(timezone=True), nullable=True))
op.create_check_constraint("file_size_non_negative", "images", "file_size >= 0")
```

### 3.2 `IndexMetadata` — value object de leitura

```python
@dataclass(frozen=True)
class IndexMetadata:
    file_size: int | None
    file_modified_at: datetime.datetime | None
    content_hash: str | None = None      # acrescentado pelo RFC-024
```

Carrega **apenas** o que a comparação precisa. Não traz `Image` nem `embedding`: reconstruir um vetor de 512 floats para comparar dois escalares seria desperdício puro.

Vive no **Domain**, apesar de descrever estado de persistência, porque é parte do contrato da porta `ImageRepository` — mantê-lo aqui deixa a porta autocontida na própria camada, em vez de fazer o Domain depender da Application para descrever seu tipo de retorno.

## 4. Notas de projeto

### 4.1 `BIGINT`, não `INTEGER`

`INTEGER` vai até ~2,1 GB. Um TIFF de alta resolução ou um RAW passam disso. Um estouro silencioso de tamanho corromperia a decisão de skip de forma invisível.

### 4.2 `TIMESTAMPTZ`, não `TIMESTAMP`

`FilesystemImageProvider` produz `datetime.fromtimestamp(st_mtime, tz=datetime.UTC)` — ciente de fuso. Gravar em coluna sem fuso descartaria a informação, e a comparação de mtime falharia em torno das transições de horário de verão: arquivos apareceriam "modificados" por uma hora, duas vezes por ano.

### 4.3 CHECK em vez de validação na aplicação

```sql
CHECK (file_size >= 0)
```

Um tamanho negativo é impossível. Validar isso na aplicação exigiria confiar em **todo** caminho de escrita, presente e futuro. O banco garante para todos de uma vez.

A constraint tem nome explícito, graças à convenção do RFC-006 — o que torna o `downgrade()` da migration escrevível:

```python
op.drop_constraint("file_size_non_negative", "images", type_="check")
```

### 4.4 Colunas nullable, e o significado de `NULL`

Linhas escritas antes desta migration não têm metadados. A migration **não faz backfill** — não haveria de onde tirar os dados (o arquivo pode ter sumido).

A regra que dá segurança a isso está codificada em três lugares:

> `NULL` significa **desconhecido**, nunca **igual**.

- `get_index_metadata()` devolve `None` quando não há linha (arquivo novo);
- devolve `IndexMetadata(None, None)` quando há linha mas sem metadados — e o chamador **deve tratar como alterado**;
- `IndexMetadata.content_hash` (RFC-024) tem default `None` pelo mesmo motivo: todo ponto de construção anterior ao hash continua produzindo a resposta segura, em vez de alegar silenciosamente uma coincidência que nunca verificou.

O custo do erro é assimétrico: tratar "desconhecido" como "igual" **pula** uma imagem que mudou, e o sistema fica errado em silêncio até a próxima alteração. Tratar como "alterado" custa uma inferência desnecessária, uma vez.

## 5. Testes

### 5.1 `tests/infrastructure/test_alembic_migrations.py`

Entregue aqui, e vale por si: sobe o histórico até `head`, desce até a base, e sobe de novo — contra o PostgreSQL real. Verifica que colunas e constraints existem depois do `upgrade` e somem depois do `downgrade`.

É o teste que garante que a promessa do RFC-007 (*"todo `downgrade()` funciona"*) seja verdade, e não uma intenção.

### 5.2 Demais

- `test_image_model.py` — novas colunas, nullabilidade, tipos, ida e volta.
- `test_postgres_image_repository.py` — persistência e leitura dos metadados.
- `test_sqlalchemy_models_architecture.py` — as colunas novas não vazaram para o Domain.
- `tests/domain/test_index_metadata.py` — imutabilidade e semântica de `None`.

## 6. Limitações

- mtime não é confiável em toda situação: o Git não preserva mtimes (registrado no RFC-022 §7.2), e alguns programas reescrevem o arquivo sem alterar conteúdo. É por isso que a etapa 4 (hash) existe — e o RFC-024 a implementou.
- Não há detecção de arquivos **removidos**: uma imagem apagada do disco permanece indexada. Trabalho futuro.
- Não há `last_processed_path` para retomada de execuções interrompidas, apesar de `ARCHITECTURE.md` prevê-lo. O RFC-024 §20 adia isso explicitamente.
