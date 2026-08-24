# RFC-014 — FakeEmbeddingModel

**Status:** Implementado (corrigido pelo RFC-022 §7.4)
**Sprint:** 2
**Depende de:** RFC-013 (EmbeddingModelPort), RFC-004 (Settings)
**Bloqueia:** RFC-015, RFC-016, RFC-019, RFC-021, RFC-025
**Commit:** `53d6268`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Todo teste do pipeline de indexação e de busca precisa de embeddings. Se eles vierem do modelo real:

- a suíte baixa 600 MB e leva minutos;
- os testes exigem rede;
- a suíte deixa de ser determinística entre versões do checkpoint.

`AI_Context.md` é explícito: *"Never load SigLIP during unit tests. Use FakeEmbeddingModel instead."*

## 2. Decisão

Um adaptador que implementa `EmbeddingModelPort` **sem nenhuma biblioteca de IA**, produzindo vetores determinísticos derivados de SHA-256 da entrada.

Requisitos:

1. **Determinístico** — a mesma imagem ou o mesmo texto sempre produz o mesmo vetor, entre execuções, processos e máquinas.
2. **Dimensão configurável** — segue `settings.embedding_dimension`, então o fake continua compatível quando o modelo real muda de largura.
3. **Geometria realista** — vetores diferentes precisam produzir similaridades de cosseno *espalhadas*, não todas ≈ 1.

## 3. O que foi entregue

```python
class FakeEmbeddingModel(EmbeddingModelPort):
    def encode_image(self, image: Image) -> EmbeddingVector:
        return self._build_embedding(f"image::{image.id}::{image.path}".encode())

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._build_embedding(f"text::{text}".encode())

    def _build_embedding(self, seed: bytes) -> EmbeddingVector:
        raw = self._deterministic_floats(seed, settings.embedding_dimension)
        mean = sum(raw) / len(raw)
        centered = [v - mean for v in raw]
        norm = math.sqrt(sum(v * v for v in centered))
        return EmbeddingVector([v / norm for v in centered])
```

### 3.1 SHA-256 em modo contador, não `hash()`

```python
block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
for byte_value in block:
    values.append((byte_value / 127.5) - 1.0)
```

O `hash()` embutido do Python é **salgado por processo** via `PYTHONHASHSEED`. Usá-lo significaria que a mesma imagem produz vetores diferentes em execuções diferentes — o que quebraria qualquer teste que grave um embedding e o leia de volta, e violaria diretamente `ARCHITECTURE.md` §20.

SHA-256 em modo contador dá um fluxo de bytes reprodutível de comprimento arbitrário, mapeado para `[-1, 1)` por `(byte / 127.5) - 1.0`.

### 3.2 Centralizar na média e normalizar em L2

Esta é a parte **corrigida** do RFC, e a lição mais interessante que ele carrega.

**A versão original estava errada de um jeito que passava em todos os testes.** Ela somava um termo posicional que crescia linearmente com o índice da dimensão, chegando a ~144 na última — contra um termo de conteúdo confinado a `(0, 1]`. O resultado: todo vetor era essencialmente a mesma rampa, mais uma perturbação desprezível. A similaridade de cosseno entre **quaisquer** duas imagens era ≈ 1,0, independentemente do conteúdo.

Isso não quebrou nenhum teste de indexação — eles verificavam que um vetor era gravado, não que vetores diferentes fossem *diferentes*. O defeito só apareceu quando o RFC-022 planejou usar o fake para medir recall de HNSW e percebeu que a medição seria sobre vetores indistinguíveis.

A correção: centralizar na média (remove o componente constante que dominava o cosseno) e normalizar em L2 (todo vetor vira unitário). Agora a similaridade entre sementes diferentes se espalha por uma faixa realista.

**O conteúdo continua sem significado semântico** — isto não é um modelo de embedding. Mas a *geometria* deixou de derrotar trivialmente a busca por similaridade, e é a geometria que os testes de busca exercitam.

## 4. Notas de projeto

**Por que fica em `infrastructure/ai/` e não em `tests/`.** Ele é um adaptador legítimo: implementa uma porta e é usado para rodar a aplicação inteira antes de existir modelo real (RFC-016). Testes o importam de lá, o que garante que o duplo esteja sujeito às mesmas regras de tipo e lint que o código de produção.

**A semente inclui `id` e `path`.** Duas imagens diferentes nunca colidem. Textos usam prefixo `text::` para não colidir com imagens.

**Lê `settings.embedding_dimension`.** Quando o RFC-023 mudou 1152 → 512, o fake acompanhou sem alteração de código.

## 5. Testes

`tests/infrastructure/test_fake_embedding_model.py`

| Teste | Verifica |
|---|---|
| Determinismo | duas chamadas com a mesma entrada devolvem vetores idênticos |
| Dimensão | tamanho igual a `settings.embedding_dimension` |
| Imagens distintas | sementes diferentes → vetores diferentes |
| Separação imagem/texto | os prefixos evitam colisão |
| Norma unitária | `‖v‖ ≈ 1,0` |
| Espalhamento de similaridade | cossenos entre pares distintos não colapsam em ≈ 1 (o teste que o defeito original teria falhado) |
| Conformidade com a porta | é uma `EmbeddingModelPort` instanciável |

## 6. Limitações

- Sem semântica alguma: "gato" e "felino" produzem vetores tão distantes quanto "gato" e "caminhão". Testes de *qualidade* de recuperação exigem o modelo real e vivem sob o marcador `slow` (RFC-023, RFC-025).
- Não lê pixels. Duas fotos diferentes no mesmo caminho produzem o mesmo vetor — motivo pelo qual o RFC-022 §7.3 registra que expectativas de decodificação de imagem ficaram dormentes até o RFC-023.
