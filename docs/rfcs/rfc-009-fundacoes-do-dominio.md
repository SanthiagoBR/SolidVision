# RFC-009 — Fundações do Domain (Image, ImageId, ImagePath)

**Status:** Implementado
**Sprint:** 2
**Depende de:** RFC-001
**Bloqueia:** RFC-010 a RFC-026 — tudo depende do Domain
**Commit:** `970982b`
**Última atualização:** 2026-08-24

---

## 1. Contexto

A camada Domain é a única que não pode importar nada de infraestrutura. É também a que define o vocabulário do sistema: o que é uma imagem, como ela é identificada, o que é um caminho válido. Toda decisão tomada aqui se propaga para as outras 17 RFCs.

## 2. Decisão

Três tipos, todos `@dataclass(frozen=True)`, sem nenhuma dependência externa:

| Tipo | Papel |
|---|---|
| `Image` | Entidade: tem identidade, compara por `id` |
| `ImageId` | Value object: UUID validado |
| `ImagePath` | Value object: caminho normalizado |

### 2.1 Escopo entregue além do previsto

O roadmap original previa que este RFC entregasse apenas a entidade `Image`, e que os value objects viessem no RFC-010. Na prática **não é possível entregar `Image` sem eles**: a entidade tem `id: ImageId` e `path: ImagePath` como campos. Entregá-la com `id: str` e substituir depois significaria escrever a entidade duas vezes.

Este RFC portanto absorveu o escopo de value objects, e o RFC-010 passou a ser a porta de repositório. Esse deslocamento é a origem da confusão de numeração que os RFCs 010–012b registram.

## 3. O que foi entregue

### 3.1 `Image` — entidade

```python
@dataclass(frozen=True)
class Image:
    id: ImageId
    path: ImagePath
    filename: str
    extension: str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Image):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
```

**Igualdade por identidade, não por valor.** É o que distingue uma *entidade* de um *value object* em DDD, e não é decoração: duas leituras da mesma imagem — uma vinda do banco, outra do sistema de arquivos — podem divergir em campos derivados e ainda assim serem a mesma imagem. Comparar por `id` é o que torna `SearchHit(image=..., similarity=...)` coerente com o `Image` que o repositório devolveu.

`__hash__` é redefinido junto porque redefinir `__eq__` em Python zera o hash herdado; sem isso, `Image` deixaria de funcionar em `set` e como chave de `dict`.

**A entidade não tem embedding.** Decisão que se sustentou por todo o projeto. Um embedding é um artefato de persistência e de modelo, não um atributo do que a imagem *é* — e o RFC-023 mudou sua dimensão de 1152 para 512 sem tocar em nenhuma linha do Domain. Os RFCs 021, 024 e 025 introduziram carriers separados (`IndexingRecord`, `IndexMetadata`, `SearchHit`) exatamente para não violar isso.

### 3.2 `ImageId` — value object

```python
@dataclass(frozen=True)
class ImageId:
    value: UUID

    def __init__(self, value: UUID | str) -> None:
        object.__setattr__(self, "value", self._normalize(value))
```

Aceita `UUID` ou `str`; string vazia, string não-UUID e qualquer outro tipo levantam `InvalidImageIdentifierError`. **Validação no construtor** significa que um `ImageId` que existe é necessariamente válido — nenhum consumidor precisa revalidar.

O `object.__setattr__` é a forma canônica de escrever em uma dataclass congelada durante a própria construção.

### 3.3 `ImagePath` — value object

```python
@dataclass(frozen=True)
class ImagePath:
    value: Path

    @staticmethod
    def _normalize(value: str | Path) -> Path:
        ...
        return Path(candidate.replace("\\", "/"))

    def __str__(self) -> str:
        return self.value.as_posix()
```

**A normalização para barra POSIX é o detalhe mais consequente deste RFC.** O projeto é desenvolvido no Windows, onde `Path` usa `\`. Sem normalizar:

- `C:\fotos\gato.jpg` e `C:/fotos/gato.jpg` seriam caminhos diferentes;
- e como o RFC-021 deriva o `ImageId` de `uuid5(namespace, str(path))`, seriam **imagens diferentes**, com ids diferentes, linhas diferentes no banco.

Normalizar aqui, uma vez, na fronteira do Domain, garante que a identidade derivada seja estável entre plataformas.

## 4. Notas de projeto

**`frozen=True` em toda parte.** Objetos de domínio imutáveis não podem ser alterados por acidente por uma camada que não deveria alterá-los, são seguros para compartilhar entre threads (relevante no threadpool do RFC-026), e são hasheáveis.

**Validar no construtor, não em um `validate()`.** Um método de validação separado é opcional por definição — e o chamador que esquecer de invocá-lo produz um objeto inválido que circula pelo sistema.

**`domain/exceptions.py` como arquivo.** Este RFC criou as exceções como um único módulo. O RFC-011 tentou convertê-lo em pacote e colidiu; o RFC-012b consolidou. Ver aqueles documentos.

## 5. Testes

| Arquivo | Cobre |
|---|---|
| `tests/domain/test_image.py` | igualdade por id, hash, imutabilidade, uso em `set`/`dict` |
| `tests/domain/test_image_id.py` | aceita `UUID` e `str`; rejeita vazio, não-UUID, tipo errado |
| `tests/domain/test_image_path.py` | normalização `\` → `/`, rejeição de vazio e de tipo inválido, `__str__` em POSIX |

## 6. Limitações

- Não há `Collection` implementada; o placeholder do RFC-001 continua vazio.
- `Image` não guarda dimensões, EXIF, nem thumbnail. `ARCHITECTURE.md` prevê thumbnails; ainda não foram implementados.
- Renomear ou mover um arquivo muda seu caminho e, portanto, seu `ImageId` — o sistema o vê como uma imagem nova. Limitação aceita e documentada em `image_identity.py` (RFC-021).
