# RFC-021 — Worker de Indexação

**Status:** Implementado (reestruturado pelo RFC-024)
**Sprint:** 3
**Depende de:** RFC-014, RFC-015, RFC-019, RFC-020
**Bloqueia:** RFC-022 (dataset), RFC-024 (pipeline em lote)
**Commit:** `0fcc8e7`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Todas as peças existem — Domain, portas, repositório PostgreSQL, metadados incrementais, modelo fake. Nenhuma delas foi ainda ligada em um pipeline que vá do **sistema de arquivos** até o **banco**.

`ARCHITECTURE.md` e o ADR-004 são categóricos: a indexação é executada pelo **Worker**, nunca pelo FastAPI. É trabalho longo, que pode falhar isoladamente e ser retomado, e não pode ficar preso ao ciclo de vida de uma requisição HTTP.

## 2. Decisão

Quatro peças novas, cada uma em sua camada:

| Peça | Camada | Papel |
|---|---|---|
| `FilesystemImageProvider` | Infrastructure | descobre arquivos e lê `stat()` |
| `compute_image_id()` | Infrastructure | deriva `ImageId` determinístico do caminho |
| `IndexOrUpdateImageUseCase` | Application | decide indexar, pular ou atualizar |
| `IndexingWorker` | Infrastructure | orquestra e registra em log |

## 3. O que foi entregue

### 3.1 Identidade determinística — a decisão mais consequente

```python
SOLIDVISION_PATH_NAMESPACE = uuid.UUID("5dc64f53-522e-4303-951e-ee6123b10dd8")

def compute_image_id(path: ImagePath) -> ImageId:
    return ImageId(uuid.uuid5(SOLIDVISION_PATH_NAMESPACE, str(path)))
```

`uuid5` é um UUID determinístico: mesmo namespace + mesmo nome → mesmo UUID, sempre, em qualquer máquina.

É isso que torna a indexação incremental possível. Se os ids fossem aleatórios (`uuid4`), a segunda execução do worker não teria como saber que `C:/fotos/gato.jpg` já está no banco — precisaria buscar por caminho, e o `ImageId` deixaria de ser útil como chave.

Também é o que permite ao RFC-024 calcular os ids de 512 arquivos **antes** de qualquer round trip e buscar todos os metadados em uma query só.

**O namespace é congelado permanentemente.** Regenerá-lo mudaria silenciosamente o `ImageId` de todo arquivo já indexado, e todos apareceriam como novos. O aviso está no topo do módulo, em maiúsculas conceituais: *"Never regenerate it."*

**Limitação aceita:** renomear ou mover um arquivo muda o caminho e, portanto, o id. O sistema o vê como imagem nova, e a antiga permanece. Rastrear renomeações exigiria identidade por conteúdo, que o RFC-022 §7.1 e o `ContentHasherPort` do RFC-024 explicitamente recusam — dois arquivos idênticos em dois caminhos são duas imagens.

### 3.2 `FilesystemImageProvider`

```python
def discover(self) -> Iterator[DiscoveredImageFile]:
    if not self._root.exists():
        return
    for path in sorted(self._root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in self._supported_extensions:
            continue
        stat = path.stat()
        yield DiscoveredImageFile(
            path=path,
            filename=path.stem,
            extension=path.suffix.lower().lstrip("."),
            file_size=stat.st_size,
            file_modified_at=datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.UTC),
        )
```

- **Gerador, não lista.** Uma coleção de 100 mil arquivos nunca é materializada em memória. Descoberta, hash, inferência e persistência se intercalam.
- **`sorted()`.** Ordem determinística de varredura. Torna logs comparáveis entre execuções e é pré-requisito de qualquer retomada futura.
- **`stat()` uma vez.** Tamanho e mtime saem da mesma chamada de sistema, junto da descoberta — não há segunda ida ao disco.
- **Raiz inexistente devolve vazio**, não erro. Um diretório ainda não criado é um resultado, não uma falha.
- **Não conhece repositório, embedding, banco nem caso de uso.** Só a biblioteca padrão.

### 3.3 `IndexingRecord` — o carrier de escrita

`ImageRepository.save()` não tem como transportar um embedding nem metadados. Em vez de acrescentar campos a `Image` (o que violaria a pureza do Domain estabelecida no RFC-009), foi criado um carrier:

```python
@dataclass(frozen=True)
class IndexingRecord:
    image: Image
    embedding: EmbeddingVector
    file_size: int | None
    file_modified_at: datetime.datetime | None
    content_hash: str | None = None      # RFC-024
```

Fica no Domain porque é parte do contrato da própria porta — mesma justificativa de `IndexMetadata` (RFC-020) e, depois, de `SearchHit` (RFC-025).

### 3.4 `IndexOrUpdateImageUseCase`

Caso de uso **separado** de `IndexImageUseCase` (RFC-015), não uma substituição. As políticas são diferentes: aquele cria uma vez e rejeita duplicata; este cria, atualiza ou pula.

Decisão implementada:

| Estado | Ação |
|---|---|
| Sem metadados persistidos | arquivo novo → indexar |
| Linha existe, ambos os campos `None` | tratado como **alterado** → indexar e preencher |
| Metadados batem exatamente | **pular**, sem abrir o arquivo |
| Metadados diferem, mas o hash prova que os bytes são iguais | atualizar metadados, **manter o embedding** *(RFC-024)* |
| Metadados diferem e o conteúdo mudou | reindexar |

Retorna `bool`: `True` se houve nova inferência, `False` caso contrário. Um valor de retorno, não uma linha de log — o que permite testar a decisão sem capturar saída.

### 3.5 `IndexingWorker`

```python
class IndexingWorker:
    def __init__(self, filesystem_provider, index_or_update_images_use_case): ...
    def run(self) -> IndexingSummary: ...
```

Componente de orquestração com dependências injetadas: não constrói seu provider nem seu caso de uso, e nunca toca em SQLAlchemy, pgvector ou internals de modelo.

**Devolve um `IndexingSummary`** com contadores e tempos, em vez de só escrever no log. Testes e benchmarks fazem asserção sobre o objeto; parsear log seria frágil e transformaria formatação em contrato.

**As falhas são registradas aqui, não no caso de uso.** A fábrica de logging é Infrastructure (RFC-005); a Application coleta as falhas e as devolve, o worker as escreve. É o que mantém a camada Application testável sem capturar log.

### 3.6 Entrypoint CLI

```bash
python -m app.infrastructure.workers.indexing_worker --root PATH
```

`--root` é **obrigatório e sem default**, de propósito: o comando nunca pode começar a indexar uma coleção de fotos real que ninguém apontou. Mesma filosofia que o RFC-026 aplicou a `warm_up_models` — o default seguro é aquele que não faz nada caro sozinho.

O prefixo `app.` é necessário porque o `pythonpath` inclui `backend/`, não `backend/app/`. `AI_Context.md` documentava o caminho sem o prefixo; o RFC-024 §12 corrigiu **a documentação**, não o código.

## 4. O que o RFC-024 mudou aqui

O `try/except` por arquivo vivia originalmente em `IndexingWorker.run()`. O RFC-024 o moveu para dentro do caso de uso, porque com lotes o isolamento passa a estar em risco em outro lugar: um lote que falha como unidade precisa ser reprocessado imagem a imagem, e só quem monta o lote pode fazer isso.

O que ficou no worker é a proteção em torno de **construir a entidade de domínio** — que acontece antes de o arquivo sequer virar candidato — e o registro em log de tudo que a execução reporta.

Detalhe fino que sobreviveu: essas falhas são **coletadas**, não relançadas. Lançar de dentro de um gerador o fecharia, truncando a varredura silenciosamente naquele arquivo.

## 5. Testes

| Arquivo | Cobre |
|---|---|
| `tests/infrastructure/filesystem/test_image_identity.py` | determinismo, estabilidade entre separadores, ids distintos para caminhos distintos |
| `tests/infrastructure/filesystem/test_filesystem_image_provider.py` | recursão, filtro de extensão, `stat()`, raiz inexistente, ordenação |
| `tests/application/test_index_or_update_image.py` | os cinco estados da tabela de decisão |
| `tests/infrastructure/workers/test_indexing_worker.py` | orquestração fim a fim com fakes, isolamento de falhas, contadores do summary |
| `tests/domain/test_indexing_record.py` | imutabilidade e campos opcionais |

Nenhum deles carrega modelo real: todos usam `FakeEmbeddingModel` e `InMemoryImageRepository`, exatamente como `AI_Context.md` exige.

## 6. Limitações

- Sem retomada. Uma execução interrompida recomeça do zero — barato, graças ao skip incremental, mas ainda uma varredura completa. Adiado explicitamente no RFC-024 §20.
- Sem detecção de arquivos removidos.
- Sem paralelismo. `worker_count` existe em `Settings` e não tem consumidor.
- Sem agendamento nem observação do sistema de arquivos; é execução manual.
