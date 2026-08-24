# RFC-012b — Consolidação da Camada de Domínio

**Status:** Implementado
**Sprint:** 2
**Depende de:** RFC-009, RFC-010
**Substitui:** RFC-011, RFC-012
**Commit:** `8a2d710`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Ao fim do RFC-012 a camada Domain estava em um estado inconsistente, com dois problemas independentes:

1. **Exceções ambíguas** — um módulo `domain/exceptions.py` (RFC-009) e uma tentativa de pacote `domain/exceptions/` (RFC-011) disputando o mesmo nome de import, com `__pycache__` residual tornando o comportamento dependente de máquina.
2. **Porta duplicada** — duas declarações de `ImageRepository` (RFC-010 e RFC-012), sem uma canônica.

Nenhum dos dois quebrava a suíte de testes. Ambos eram bombas-relógio: o primeiro falharia em outra máquina, o segundo falharia silenciosamente ao contrato ser estendido.

Este RFC não acrescenta funcionalidade. Ele existe para deixar a Sprint 2 em um estado sobre o qual os RFCs 013–016 possam ser construídos.

## 2. Decisão

**Uma coisa, um lugar.** Escolher uma forma canônica para cada conceito, remover a alternativa no mesmo commit, e escrever testes que impeçam a divergência de voltar.

## 3. O que foi entregue

### 3.1 Exceções como pacote

```
backend/app/domain/exceptions/
├── __init__.py        # superfície pública: reexporta tudo
├── domain_error.py    # DomainError — raiz da hierarquia
└── image_errors.py    # exceções relativas a imagem
```

```python
class DomainError(Exception):
    """Base class for all domain-layer exceptions."""
```

Hierarquia entregue:

| Exceção | Levantada quando |
|---|---|
| `ImageNotFoundError` | uma imagem esperada não existe |
| `ImageAlreadyExistsError` | criar uma imagem que já existe |
| `InvalidImageIdentifierError` | `ImageId` recebe algo que não é UUID |
| `InvalidImagePathError` | `ImagePath` recebe caminho vazio ou tipo errado |
| `UnsupportedImageExtensionError` | extensão fora da lista suportada |

O `domain/exceptions.py` antigo foi **removido no mesmo commit** — é o que torna a conversão atômica, e a razão pela qual o conflito do RFC-011 não pôde reaparecer.

### 3.2 Por que uma raiz `DomainError`

Porque ela permite que uma camada externa capture a família inteira em um lugar só. O RFC-026 §8 cobra esse investimento anos-luz adiante do commit que o fez:

```python
@app.exception_handler(DomainError)
def handle_domain_error(request, exc): ...
```

Sem raiz comum, cada nova exceção de domínio exigiria editar o tratamento de erros da API. Com ela, uma exceção nova é traduzida corretamente sem que nada na Presentation mude.

### 3.3 Por que arquivos separados, e não um `exceptions.py` grande

`domain_error.py` isolado permite que `image_errors.py` e, mais tarde, `search_errors.py` (RFC-025) importem a raiz sem importar uns aos outros. O `__init__.py` é a única superfície pública, o que significa que consumidores escrevem sempre:

```python
from app.domain.exceptions import ImageNotFoundError
```

independentemente de em qual arquivo a classe mora. Reorganizar internamente não quebra chamador nenhum — e de fato não quebrou quando o RFC-025 acrescentou `search_errors.py`.

### 3.4 Porta de repositório: uma só

Mantida `domain/repositories/image_repository.py` (RFC-010), conforme a convenção de `AI_Context.md`. A duplicata do RFC-012 foi removida. Não existe `domain/ports/` na árvore.

### 3.5 Mensagens default

```python
class ImageNotFoundError(DomainError):
    def __init__(self, message: str = "Image not found.") -> None:
        super().__init__(message)
```

Todas as exceções aceitam mensagem customizada e trazem um default útil. Um `raise ImageNotFoundError()` sem argumento produz uma mensagem legível, em vez de uma exceção vazia.

## 4. Como a hierarquia cresceu depois

O formato estabelecido aqui absorveu duas extensões sem alteração estrutural:

- **RFC-013** acrescentou `InvalidEmbeddingVectorError` (declarada no próprio `__init__.py`).
- **RFC-025** acrescentou `search_errors.py` com `EmptySearchQueryError`, `InvalidSearchLimitError` e `EmbeddingDimensionMismatchError` — todas herdando de `DomainError`, todas capturadas pelo mesmo handler HTTP do RFC-026.

Que um pacote criado para resolver um conflito de nomes tenha acomodado essas duas extensões sem discussão é a evidência de que a forma canônica escolhida estava certa.

## 5. Testes

`tests/domain/test_domain_exceptions.py`

- toda exceção de domínio herda de `DomainError`;
- `DomainError` herda de `Exception`;
- mensagens default aparecem em `str(exc)`;
- mensagens customizadas sobrescrevem o default;
- todas as classes são importáveis a partir de `app.domain.exceptions` (o teste que trava o conflito do RFC-011 de volta).

## 6. Lições

**Consolidar cedo é barato.** Este RFC custou um commit pequeno. Feito depois do RFC-019, teria exigido tocar em repositórios, casos de uso e testes.

**Um RFC de limpeza é um RFC legítimo.** Não entrega funcionalidade e mesmo assim é a diferença entre uma Sprint 3 previsível e uma sequência de bugs de import.

**Conflitos entre RFCs planejados são normais; o problema é descobri-los tarde.** Ver RFC-011 §4 e RFC-012 §4.
