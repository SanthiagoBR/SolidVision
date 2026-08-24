# RFC-017 — Modelos SQLAlchemy

**Status:** Implementado
**Sprint:** 3
**Depende de:** RFC-006 (Base), RFC-009 (Image)
**Bloqueia:** RFC-018 (migrations), RFC-019 (repositório PostgreSQL)
**Commits:** `e0bbddc` (modelo), `16ebe2b` (RFC-017b: coluna vetor + índice HNSW + ADR-007)
**Última atualização:** 2026-08-24

---

## 1. Contexto

O Domain tem `Image`. O banco precisa de uma tabela. A questão é **como as duas coisas se relacionam** — e a resposta errada, que é o default de muitos projetos, é fazer da entidade de domínio o próprio modelo ORM.

Se `Image` herdasse de `Base`, o Domain importaria SQLAlchemy. Isso é proibido por `AI_Context.md` e destruiria a independência que o RFC-009 construiu: `Image` deixaria de ser um objeto puro para carregar estado de sessão, lazy loading e identidade de ORM.

## 2. Decisão

**Dois tipos separados, com tradução explícita nas duas direções.**

```
Domain            Infrastructure
Image      ←→     ImageModel(Base)
        to_domain()
        from_domain()
```

A tradução vive no **modelo ORM**, não na entidade. `ImageModel` importa `Image`; `Image` não sabe que `ImageModel` existe. A seta de dependência continua apontando para dentro.

## 3. O que foi entregue

```python
class ImageModel(Base):
    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
    )
    path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    extension: Mapped[str] = mapped_column(String, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSION), nullable=True
    )

    __table_args__ = (UniqueConstraint("path", name="uq_images_path"),)
```

*(Os campos `file_size`, `file_modified_at` e `content_hash` foram acrescentados pelos RFC-020 e RFC-024.)*

### 3.1 Tradução

```python
@classmethod
def from_domain(cls, image: Image) -> ImageModel:
    return cls(
        id=image.id.value,
        path=str(image.path),      # ImagePath.__str__ → POSIX
        filename=image.filename,
        extension=image.extension,
        embedding=None,
        ...
    )

def to_domain(self) -> Image:
    return Image(
        id=ImageId(self.id),
        path=ImagePath(self.path),
        filename=self.filename,
        extension=self.extension,
    )
```

`str(image.path)` usa a normalização POSIX do RFC-009. É o que garante que o mesmo arquivo, indexado no Windows e no Linux, produza a mesma string em `path` — e, por consequência, o mesmo `ImageId` derivado (RFC-021).

`from_domain()` deixa `embedding` e os metadados sem valor, **de propósito**: a entidade de domínio não os tem. Quem os preenche é o pipeline de indexação, via `IndexingRecord` (RFC-021).

### 3.2 `path` com `UNIQUE`

O caminho é a identidade natural do arquivo. A constraint nomeada (`uq_images_path`, graças à convenção do RFC-006) é o que permite que `PostgresImageRepository.save()` traduza um `IntegrityError` em `ImageAlreadyExistsError` — regra de domínio aplicada pelo banco, não por um `SELECT` antes do `INSERT` sujeito a corrida.

### 3.3 `embedding` nullable

Uma linha pode existir sem embedding: `save()` cria exatamente esse caso. Consequência que o RFC-025 teve de honrar — `search_similar()` **nunca** devolve uma imagem sem embedding, porque ela nunca foi comparada, e inventar uma pontuação a colocaria na frente de imagens realmente indexadas.

### 3.4 RFC-017b: coluna vetorial e ADR-007

Um segundo commit acrescentou a coluna `Vector(...)`, o índice HNSW e o **ADR-007**, que registra a restrição estrutural: *pgvector exige dimensão fixa por coluna*. Um `vector(1152)` não pode guardar um vetor de 512 dimensões; padding ou truncamento distorceriam a similaridade de cosseno.

Consequência: trocar de modelo de embedding exige **reindexação completa**, e busca entre modelos diferentes é impossível. Foi exatamente o que aconteceu no RFC-023 (1152 → 512).

### 3.5 A dimensão é constante de módulo, não `settings`

```python
EMBEDDING_DIMENSION = 512
```

Deliberado. A largura da coluna é **esquema físico**, fixado por uma migration; ela não pode seguir silenciosamente uma variável de ambiente para longe do que o banco de fato contém. Se `settings.embedding_dimension` mudasse sozinho, o modelo passaria a declarar uma largura que o banco não tem, e a falha apareceria como erro obscuro de driver.

O teste `test_image_model_embedding_column_matches_configured_dimension` verifica que os dois números continuam concordando — quem os aproxima é o desenvolvedor, ao escrever a migration, não o import.

## 4. Notas de projeto

**UUID como chave primária, não `serial`.** Ids são derivados deterministicamente do caminho (RFC-021), então podem ser calculados *antes* de tocar o banco. Isso é o que permite ao RFC-024 montar um lote inteiro e consultar metadados de 512 imagens em uma única query, sem round trip por arquivo.

**`DateTime(timezone=True)`** (acrescentado no RFC-020) — `timestamptz`, não `timestamp`. `FilesystemImageProvider` produz datetimes cientes de fuso (`tz=datetime.UTC`); guardar em coluna sem fuso perderia a informação e faria comparações de mtime falharem em torno do horário de verão.

## 5. Testes

| Arquivo | Cobre |
|---|---|
| `tests/infrastructure/test_image_model.py` | ida e volta `from_domain`/`to_domain`, normalização de path, nullabilidade, largura da coluna vetorial |
| `tests/infrastructure/test_sqlalchemy_models_architecture.py` | **`app/domain/` não importa `sqlalchemy`**; `Image` não é subclasse de `Base` |

O segundo é um teste de arquitetura: sem ele, nada impediria alguém de "simplificar" fundindo entidade e modelo.

## 6. Limitações

- Não há modelo `CollectionModel`. Coleções são previstas em `ARCHITECTURE.md` §15 e não implementadas.
- Não há coluna de thumbnail.
- Sem `created_at`/`updated_at`.
