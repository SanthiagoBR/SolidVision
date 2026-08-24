# RFC-023 — Adaptador de Embedding CLIP

**Status:** Proposto
**Autor:** SolidVision
**Depende de:** RFC-015 (Caso de Uso de Indexação), RFC-018 (Coluna de Embedding + HNSW), RFC-021 (Worker de Indexação), RFC-022 (Dataset de Demonstração)
**Bloqueia:** RFC de busca vetorial, RFC de benchmark, RFC de fine-tuning
**Última atualização:** 2026-08-20

---

## 1. Contexto

Toda camada do pipeline de indexação é real, exceto a que lhe dá significado.

`FilesystemImageProvider` descobre arquivos. `IndexingWorker` conduz `IndexOrUpdateImageUseCase`. `PostgresImageRepository.save_indexed()` grava a linha da imagem e seu embedding em uma coluna pgvector indexada por HNSW. O RFC-022 forneceu um corpus reprodutível de 45 imagens com 25 consultas de *ground truth* contra o qual executar tudo isso.

Mas a única implementação de `EmbeddingModelPort` na base de código é `FakeEmbeddingModel`, que faz hash SHA-256 do id e do caminho da imagem e nunca abre o arquivo. Ela produz vetores bem formados, com geometria correta e zero conteúdo semântico. Tudo a jusante — similaridade, ranking, recall — está medindo ruído.

Este RFC o substitui em produção por um modelo real.

### 1.1 O que foi o bake-off

`experiments/rfc-023-siglip-bakeoff/` comparou SigLIP2, vários checkpoints CLIP e jina-CLIP ao longo de dimensão de embedding, template de prompt de texto, tratamento de português, quantização INT8 e batching. Todo número da seção 3 é reproduzido dessas execuções.

**Ressalva sobre os artefatos, dita de saída:** os scripts Python do bake-off e seu README **não estão presentes no repositório**. `experiments/` é inteiramente não rastreado, e o `.gitignore` exclui `*.log`, então nada dele está no git tampouco. O que sobrevive em disco é o conjunto de logs de execução:

| Log | Cobre |
| --- | --- |
| `clip_small_dim_run_output.log` | Checkpoints CLIP de 512 dimensões: acurácia + throughput |
| `clip_jina_run_output.log` | CLIP de 768 dimensões + jina-clip-v2 |
| `clip_pt_translation_run_output.log` | Português: bruto vs. traduzido por máquina |
| `clip_template_run_output.log` | Variantes de template de prompt |
| `quantization_check_output.log` | SigLIP2 fp32 vs. INT8 dinâmico |
| `batching_check_output.log` | Tamanhos de lote de imagem/texto do SigLIP2 |
| `onnx_quantization_check_output.log`, `step1_export.log`, `step2_quantize*.log` | Exportação ONNX + tentativa de quantização estática |

Os números abaixo são citados desses logs textualmente. Qualquer coisa não estabelecida por eles é marcada explicitamente como decisão de implementação ou questão em aberto. Notavelmente, o log do bake-off isolado do SigLIP está faltando; os números de acurácia do SigLIP2 citados aqui vêm da linha de base fp32 dentro de `quantization_check_output.log`, que rodou sobre o mesmo corpus de 45 imagens.

---

## 2. Problema

Três coisas precisam ser verdadeiras ao mesmo tempo, e elas puxam umas contra as outras.

1. **O modelo precisa ser bom o suficiente para ranquear imagens aéreas de propriedades** a partir de consultas em português e em inglês.
2. **Precisa ser rápido o suficiente em CPU** para indexar rumo à meta de 100.000 imagens de `ARCHITECTURE.md` §22 sem orçamento de GPU.
3. **Não pode vazar para o Domain nem para a Application.** O modelo atual é um ponto de partida, não um compromisso; substituí-lo não pode tocar um único contrato acima de Infrastructure.

O terceiro é o que sobrevive a este RFC.

---

## 3. Seleção Empírica do Modelo

Todas as execuções: CPU, 45 imagens do corpus de demonstração do RFC-022, 25 consultas de *ground truth* de `queries.json`.

Métricas conforme reportadas pelos scripts do bake-off:
- **strict-EN / strict-PT** — o resultado top-1 é uma imagem declarada `relevant` para aquela consulta.
- **pairwise** — fração dos pares (relevante, negativo difícil) que o modelo ordena corretamente.
- **strict-PT(MT)** — consulta em português traduzida por máquina para o inglês antes.

### 3.1 Checkpoints CLIP de 512 dimensões

De `clip_small_dim_run_output.log`:

| modelo | dim | strict-EN | strict-PT | pairwise | s/img (média) | carga s |
| --- | --- | --- | --- | --- | --- | --- |
| openai/clip-vit-base-patch32 | 512 | 44,0% | 44,0% | 68,0% | 0,770 | 4,7 |
| openai/clip-vit-base-patch16 | 512 | 56,0% | 36,0% | 70,9% | 1,745 | 4,1 |
| **laion/CLIP-ViT-B-32-laion2B-s34B-b79K** | **512** | **56,0%** | **44,0%** | **74,3%** | **0,464** | **3,8** |

Tempo de parede projetado em CPU, processo único, para codificar 100.000 imagens:

| modelo | horas | dias |
| --- | --- | --- |
| openai/clip-vit-base-patch32 | 21,39 | 0,89 |
| openai/clip-vit-base-patch16 | 48,49 | 2,02 |
| laion/CLIP-ViT-B-32-laion2B-s34B-b79K | **12,90** | **0,54** |

O checkpoint LAION B-32 é o melhor em todos os eixos simultaneamente — acurácia, pairwise e velocidade. Não é um trade-off; ele domina.

### 3.2 Checkpoints de 768 dimensões

De `clip_jina_run_output.log`:

| modelo | dim | strict-EN | strict-PT | pairwise | s/img (média) | horas p/ 100k |
| --- | --- | --- | --- | --- | --- | --- |
| openai/clip-vit-large-patch14 | 768 | 56,0% | 44,0% | 72,5% | 7,694 | 213,71 |
| laion/CLIP-ViT-L-14-laion2B-s32B-b82K | 768 | 52,0% | 32,0% | 71,2% | 6,751 | 187,54 |
| jinaai/jina-clip-v2 | 768 | 16,0% | 16,0% | 56,6% | 48,674 | 1352,06 |

Dobrar a dimensão não comprou nada. O melhor modelo de 768 dimensões empatou com o LAION B-32 de 512 em strict-EN (56,0%) e perdeu em pairwise (72,5% vs. 74,3%), custando **16,6× mais tempo de CPU por imagem**. O `jina-clip-v2` foi catastrófico neste corpus — 16,0% de acurácia estrita, pior que vários de seus próprios negativos triviais, a 48,7 s/imagem.

### 3.3 Template de prompt

De `clip_template_run_output.log`, sobre o checkpoint selecionado:

| variante | template | strict-EN | strict-PT(MT) | pairwise-EN | pairwise-PT(MT) |
| --- | --- | --- | --- | --- | --- |
| raw | `{query}` | 56,0% | 52,0% | 78,8% | 72,5% |
| **a_photo_of** | **`a photo of {query}`** | **60,0%** | **56,0%** | **80,4%** | **75,1%** |
| aerial_photo_of | `an aerial photo of {query}` | 52,0% | 44,0% | 73,5% | 69,3% |

`a photo of {query}` vence nas quatro colunas. O resultado contraintuitivo é que **o template específico de imagens aéreas é o pior dos três** — apesar de este ser um produto de imagens aéreas — custando 8 pontos de strict-EN e 12 de strict-PT(MT) contra o template genérico. A mesma ordenação se manteve para `openai/clip-vit-base-patch16` (52,0% → 52,0% → 52,0% em strict-EN, mas 78,3% → 72,5% em pairwise-EN para o aéreo).

É por isso que a seção 7 proíbe o template aéreo nominalmente.

### 3.4 Português

De `clip_pt_translation_run_output.log` (tradutor: `Helsinki-NLP/opus-mt-ROMANCE-en`):

| modelo | strict-EN | strict-PT (bruto) | strict-PT (MT) |
| --- | --- | --- | --- |
| openai/clip-vit-large-patch14 | 56,0% | 44,0% | **64,0%** |
| laion/CLIP-ViT-L-14-laion2B-s32B-b82K | 52,0% | 32,0% | **60,0%** |
| openai/clip-vit-base-patch32 | 44,0% | 44,0% | **52,0%** |
| openai/clip-vit-base-patch16 | 56,0% | 36,0% | **52,0%** |
| laion/CLIP-ViT-B-32-laion2B-s34B-b79K | 56,0% | 44,0% | **52,0%** |

Traduzir antes melhorou todos os modelos — em 8 pontos no checkpoint selecionado (44,0% → 52,0%), e em até 28 pontos no `laion/CLIP-ViT-L-14`. A torre de texto do CLIP é apenas em inglês; alimentá-la com português é mensuravelmente pior que traduzir antes.

Custo de tradução, mesmo log: 25 consultas em 23,5 s no total, **média de 0,938 s/consulta, mediana 0,859**, carga do modelo 4,8 s. Isso é por *consulta*, não por imagem, e apenas para consultas de fato detectadas como português.

Traduções de exemplo registradas no log mostram que a qualidade é adequada, mas não perfeita:

| Português | Tradução automática | Inglês original |
| --- | --- | --- |
| propriedade rural com um pequeno lago | Rural property with a small lake | rural property with a small lake |
| pequeno lago natural em uma fazenda, nao um tanque artificial | Small natural lake on a farm, not an artificial **tank** | ...not an artificial **pond** |
| tanques artificiais de piscicultura em um vale | Artificial fish **tanks** in a valley | artificial fish farming **ponds** in a valley |

A confusão `tanque` → `tank`/`pond` é uma fonte real e visível da diferença remanescente em português.

### 3.5 Por que CLIP e não SigLIP

SigLIP2 era o candidato original — daí o nome do diretório. Da linha de base fp32 em `quantization_check_output.log`, `google/siglip2-base-patch16-384` sobre o mesmo corpus de 45 imagens:

| | SigLIP2-base-384 (fp32) | laion CLIP-ViT-B-32 (`a photo of`) |
| --- | --- | --- |
| strict-EN | 60,0% | **60,0%** |
| strict-PT | **64,0%** (nativo, sem tradução) | 56,0% (via tradução automática) |
| pairwise | 80,4% | **80,4%** (EN) |
| s/imagem (média, CPU) | 4,960 | **0,464** |
| 100k imagens | 137,78 h (5,74 dias) | **12,90 h (0,54 dias)** |

A decisão em uma linha: **acurácia idêntica em inglês e acurácia pairwise idêntica a cerca de um décimo do custo de CPU.**

O SigLIP2 vence genuinamente em português — 64,0% nativamente contra 56,0% do CLIP-mais-tradução, e sem precisar de um segundo modelo. Essa é uma vantagem real e está sendo abandonada deliberadamente. Ela não sobrevive ao contato com o requisito de throughput: a 4,96 s/imagem, indexar 100.000 imagens leva quase uma semana em um processo de CPU, contra meio dia para o CLIP. Para um produto cuja meta declarada é 100.000 imagens e cuja premissa de deploy é CPU, uma diferença de 8 pontos em português é o preço mais barato.

**Ressalva sobre comparabilidade:** os dois conjuntos de números foram produzidos por scripts de bake-off diferentes contra o mesmo corpus. As colunas `strict-PT` não estão medindo a mesma coisa — a do SigLIP2 é compreensão multilíngue nativa, a do CLIP é pós-tradução. `strict-EN` e `pairwise` são diretamente comparáveis; `strict-PT` é uma comparação de *abordagens*, não de modelos.

### 3.6 Por que 512 dimensões

Não escolhido por si mesmo — é a largura de projeção do checkpoint vencedor. Mas a seção 3.2 confirma que isso não custa nada: nenhum modelo de 768 dimensões no bake-off superou o LAION B-32 de 512 em acurácia pairwise, e todos foram de 14 a 105× mais lentos por imagem. Vetores menores significam adicionalmente um índice HNSW menor e menos I/O por linha.

### 3.7 Quantização e batching: medidos, depois rejeitados

**Quantização dinâmica INT8** (`quantization_check_output.log`, SigLIP2, `torch.quantization.quantize_dynamic` sobre `nn.Linear`, backend onednn):

| métrica | fp32 | int8 | variação |
| --- | --- | --- | --- |
| s/imagem (média) | 4,960 | 3,437 | 1,44× mais rápido |
| acurácia estrita (EN) | 60,0% | 48,0% | **−12 pts** |
| acurácia estrita (PT) | 64,0% | 40,0% | **−24 pts** |
| acurácia pairwise | 80,4% | 66,4% | **−14 pts** |

Desvio dos embeddings em relação ao fp32 sobre entrada idêntica: imagens com cosseno médio **0,7582** (mín. 0,6779), textos com média 0,9010 (mín. 0,7173). Um ganho de 1,44× que move os embeddings de imagem em 0,24 de cosseno e destrói 24 pontos de acurácia em português não é uma troca que valha a pena. **Quantização: rejeitada.**

**Batching** (`batching_check_output.log`, SigLIP2, CPU):

| tamanho do lote | s/imagem | ganho |
| --- | --- | --- |
| 1 | 3,850 | 1,00× |
| 4 | 3,412 | 1,13× |
| 32 | 3,282 | **1,17×** |

O desvio dos embeddings em todo tamanho de lote contra o lote de 1 foi exatamente 1,0000 — batching é numericamente seguro. Mas 1,17× no melhor tamanho de lote não justifica remodelar `EmbeddingModelPort`, `IndexOrUpdateImageUseCase` e `IndexingWorker` em torno de uma API de lote. O batching de texto foi mais promissor (0,788 → 0,291 s/texto, 2,7×), mas texto é codificado uma vez por *consulta*, não uma vez por imagem, então não é o gargalo. **Batching: fora de escopo**, e deixado como trabalho futuro na seção 17.

**ONNX + quantização estática** foi tentado e não se completou. `step1_export.log` mostra as duas torres exportando com sucesso (`vision_fp32.onnx`, `text_fp32.onnx`) via o exportador TorchScript legado. `step2_quantize_v2.log` mostra `quantize_static()` sendo iniciado com a nota de que ele "cresceu ilimitadamente antes (microsoft/onnxruntime#21979)" e não produziu resultados depois disso. Não existem números de ONNX, e nenhum é alegado aqui.

---

## 4. Decisão

| Aspecto | Decisão |
| --- | --- |
| Modelo | `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` |
| Dimensão do embedding | 512 |
| Codificação de imagem | Codificação nativa CLIP sobre pixels RGB |
| Template de texto | `a photo of {query}` — exatamente |
| Português | Detectar, traduzir para inglês, então aplicar o template |
| Modelo de tradução | `Helsinki-NLP/opus-mt-ROMANCE-en` com o token de origem `>>por<<` |
| Carregamento da tradução | `AutoTokenizer` + `AutoModelForSeq2SeqLM` |
| Detecção de idioma | `langdetect` com `DetectorFactory.seed = 0` |
| Quantização | Nenhuma |
| Batching | Nenhum |
| Dispositivo padrão | `auto` → CUDA quando disponível, senão CPU |
| Busca vetorial | Não neste RFC |
| Fine-tuning | Não neste RFC |

---

## 5. Arquitetura

### 5.1 A fronteira

```
Domain          EmbeddingModelPort  (ABC: encode_image, encode_text)
                        ▲
                        │ implementa
Infrastructure  ClipEmbeddingModel ──▶ MarianQueryTranslator
                        │                      │
                        ▼                      ▼
                  torch, transformers,  langdetect, Marian MT
                  Pillow, CLIP
```

`EmbeddingModelPort` **não foi modificado**. Ele não menciona checkpoint, tensor, tokenizador, processador ou idioma. `encode_text(text: str)` não ganhou parâmetro de idioma: a decisão de idioma é um detalhe de implementação do modelo atual, não um fato sobre o domínio, e um futuro modelo multilíngue teria de carregar para sempre um argumento sem sentido.

Tudo que é específico do modelo vive sob `backend/app/infrastructure/ai/`:

| Arquivo | Responsabilidade |
| --- | --- |
| `clip_embedding_model.py` | O adaptador: `EmbeddingModelPort` sobre CLIP |
| `query_translator.py` | ABC `QueryTranslator` + `MarianQueryTranslator` |
| `device.py` | `resolve_device()` — um único lugar, para todos os adaptadores |
| `fake_embedding_model.py` | Inalterado. Continua sendo o duplo de teste |

Isso é imposto, não apenas pretendido: `backend/tests/test_ai_layer_boundaries.py` percorre o código-fonte real de `app/domain/` e `app/application/` e falha diante de qualquer import de `torch`, `transformers`, `langdetect`, `PIL` ou `numpy`, ou de qualquer caminho de módulo contendo `clip`/`siglip`/`huggingface`. Também garante que o próprio texto da porta esteja livre de vocabulário de implementação.

### 5.2 `ClipEmbeddingModel` não é reexportado no `__init__` do pacote

`app/infrastructure/ai/__init__.py` deliberadamente exporta apenas `FakeEmbeddingModel`. A maior parte da suíte rápida de testes importa o fake, e um `__init__` de pacote que também puxasse o adaptador CLIP arrastaria `torch` e `transformers` para cada um desses imports sem nenhum benefício. O adaptador é importado pelo caminho completo do módulo.

---

## 6. Extração Correta do Embedding

Esta é a parte do RFC-023 com maior probabilidade de ser feita errado, e foi verificada experimentalmente contra o **transformers 5.15.0** instalado, não copiada de um exemplo.

**`get_image_features()` e `get_text_features()` não retornam um tensor.**

Eles retornam um `BaseModelOutputWithPooling` cujo campo `.pooler_output` o método *sobrescreveu* com o embedding projetado:

```python
# transformers/models/clip/modeling_clip.py, v5.15.0
pooled_output = vision_outputs.pooler_output
vision_outputs.pooler_output = self.visual_projection(pooled_output)
return vision_outputs
```

O exemplo da docstring *nesse mesmo arquivo* ainda diz `image_features = model.get_image_features(**inputs)` — segui-lo produz um objeto, não um embedding. Medido diretamente:

| expressão | resultado |
| --- | --- |
| `type(get_image_features(...))` | `BaseModelOutputWithPooling` |
| `.pooler_output.shape` | `(1, 512)` ✅ |
| `.last_hidden_state.shape` | `(1, 50, 768)` ❌ espaço errado, tamanho errado |
| `config.projection_dim` | `512` |

O adaptador lê `.pooler_output` e valida a forma. Um teste unitário lhe passa um stub cujo `last_hidden_state` tem 768 de largura precisamente para que ler o campo errado falhe alto, em vez de persistir lixo em silêncio.

### 6.1 Normalização

**Medido: as features do CLIP não são normalizadas.** As normas L2 no checkpoint selecionado foram **~11,56 para imagens** e **~8,47 para texto**.

O índice HNSW é construído com `vector_cosine_ops`, e similaridade de cosseno é a métrica que a busca futura usará. O adaptador, portanto, normaliza em L2 dentro de `_to_embedding_vector()`, de modo que todo vetor armazenado tem norma unitária e a similaridade de cosseno se reduz a um produto escalar.

**A normalização deliberadamente não foi adicionada a `EmbeddingVector`.** Aquele value object é agnóstico ao modelo; embutir nele uma invariante de norma unitária L2 imporia a convenção de um espaço de embedding a todo modelo futuro, inclusive àqueles para os quais ela é errada. A convenção pertence ao adaptador que conhece o modelo.

### 6.2 Determinismo

Verificado: chamadas repetidas de `get_image_features()` sobre a mesma entrada são idênticas bit a bit, e a decodificação do Marian é gulosa (`num_beams=1, do_sample=False`), então a mesma consulta sempre produz a mesma string em inglês e, portanto, o mesmo embedding.

---

## 7. Pipeline de Texto

```
consulta do usuário
    ↓  langdetect (seed 0)
    ↓  português?  ──sim──▶  Marian, ">>por<< {query}"  ──▶  inglês
    ↓  não
"a photo of {query}"
    ↓  codificador de texto do CLIP
    ↓  .pooler_output → normalização L2
EmbeddingVector de 512 dimensões
```

O template é exatamente `a photo of {query}`. Sem variante aérea (seção 3.3). Sem *prompt ensembling* — o bake-off testou apenas templates isolados, e a média de múltiplos prompts não foi medida aqui.

### 7.1 Detecção de idioma

`langdetect` foi escolhido em vez de um framework de PLN pesado porque o projeto não tem tal dependência e este problema não justifica introduzir uma. É Python puro com uma dependência transitiva (`six`).

`DetectorFactory.seed = 0` é definido no import do módulo. Sem isso, o algoritmo probabilístico do `langdetect` não é reprodutível entre execuções — o mesmo requisito de determinismo que já descarta o `hash()` salgado do Python em `FakeEmbeddingModel`.

**Limitação conhecida, medida e documentada em vez de disfarçada.** `langdetect` precisa de várias palavras. Consultas em frase completa classificam corretamente nos dois idiomas, mas palavras isoladas não:

| consulta | detectado | traduziu? |
| --- | --- | --- |
| `propriedade rural com um pequeno lago` | `pt` | sim ✅ |
| `rural property with a small lake` | `en` | não ✅ |
| `vista aerea de uma cidade` | `pt` | sim ✅ |
| `fazenda` | `tr` | **não** ❌ |
| `lago` | `tl` | **não** ❌ |
| `piscina` | `it` | **não** ❌ |
| `farm` | `sv` | não (inofensivo) |

O modo de falha é benigno em formato: uma consulta curta mal classificada nunca é detectada como `pt`, então ela passa adiante sem tradução em vez de ser traduzida errado. Uma consulta de uma palavra em português chega ao CLIP em português e se comporta como a coluna PT-bruto da seção 3.4 prevê. Construir um detector melhor está fora de escopo; `test_single_word_portuguese_is_a_known_detection_gap` fixa o comportamento para que adotar um depois seja uma mudança visível e deliberada.

`langdetect` levanta `LangDetectException` sobre entrada sem características (strings vazias, apenas dígitos, pontuação). Isso é capturado e tratado como inglês — não há nada a traduzir, e reprovar uma consulta por isso seria absurdo.

### 7.2 Por que não `pipeline("translation")`

`Helsinki-NLP/opus-mt-ROMANCE-en` não é registrado de forma confiável sob essa tarefa genérica no transformers 5.15. O par explícito `AutoTokenizer` + `AutoModelForSeq2SeqLM` também mantém o token de origem `>>por<<` e os parâmetros de decodificação gulosa visíveis no código-fonte, em vez de enterrados em defaults de pipeline.

`opus-mt-ROMANCE-en` é muitos-para-um entre as línguas românicas, então o idioma de origem é selecionado por um token de destino inicial, não pelo checkpoint. Omitir `>>por<<` deixaria o modelo adivinhar.

---

## 8. Estratégia de Dispositivo

O padrão de `settings.device` mudou de `cpu` para **`auto`**, resolvido centralmente por `app/infrastructure/ai/device.py`:

| configurado | resultado |
| --- | --- |
| `auto` (padrão) | CUDA se `torch.cuda.is_available()`, senão CPU |
| `cuda` quando CUDA está ausente | **CPU, com um aviso** — não uma exceção |
| `cpu` | CPU, mesmo quando CUDA existe |
| qualquer outra coisa | repassado a `torch.device` textualmente |

CUDA é uma otimização, nunca um requisito. Uma máquina configurada para CUDA que a perde — uma wheel só de CPU, um container sem o runtime — ainda precisa iniciar e servir devagar, em vez de falhar ao construir o adaptador. Backends de GPU não-CUDA (ROCm, MPS, XPU) estão fora de escopo e não são detectados nem tratados de forma especial.

Tanto o modelo CLIP quanto o modelo de tradução recebem o mesmo dispositivo resolvido; o adaptador repassa seu dispositivo ao tradutor que constrói.

---

## 9. Ciclo de Vida dos Modelos

Duas restrições em tensão: os modelos não podem recarregar a cada chamada, e a suíte de testes nunca pode baixá-los.

**O padrão de singleton eager em nível de módulo do RFC-016 não é aceitável aqui.** `app.presentation.dependencies` é importado transitivamente pela maior parte da suíte (qualquer import de `app.presentation.api` o alcança). Um `ClipEmbeddingModel()` eager no momento do import seria uma mina terrestre.

Resolução, em duas camadas:

1. **`@lru_cache(maxsize=1)` sobre `get_embedding_model()`** — semântica de carregar-uma-vez/reusar-sempre, adiada até a primeira chamada.
2. **O `__init__` do adaptador é ele próprio gratuito.** Tanto o checkpoint CLIP quanto o modelo de tradução carregam no primeiro `encode_*`, não no construtor. O tradutor carrega apenas na primeira consulta de fato detectada como português, então um deploy só em inglês nunca paga por ele.

Juntos, isso significa que nem importar o módulo de dependências nem *chamar o provider* toca o Hugging Face. `test_resolving_the_embedding_model_downloads_nothing` garante exatamente isso.

O singleton eager existente de `InMemoryImageRepository` foi deixado em paz — ele não tem I/O caro.

**Verificado:** a suíte rápida inteira passa com `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` e `HF_HOME` apontando para um diretório vazio. 297 passaram.

---

## 10. Configuração

| configuração | antes | depois |
| --- | --- | --- |
| `embedding_model` | `google/siglip-base-patch16-224` | `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` |
| `embedding_dimension` | `1152` | `512` |
| `device` | `cpu` | `auto` |

`.env.example` atualizado para corresponder.

Os defaults antigos eram internamente inconsistentes — `siglip-base-patch16-224` produz 768 dimensões, não 1152 (1152 é do `siglip-so400m`). Nada dependia desse pareamento porque nenhum modelo real jamais foi carregado.

**Sobre não fixar 512 duas vezes.** `settings.embedding_dimension` é o contrato do lado do modelo, e o adaptador valida sua saída real contra ele. `ImageModel.embedding` e a migration carregam o literal `512` porque isso é *esquema físico* fixado por uma migration: não pode seguir silenciosamente uma variável de ambiente de runtime para longe do que o banco de fato contém. Os dois são mantidos honestos por `test_image_model_embedding_column_matches_configured_dimension`, que garante que concordam.

---

## 11. Migration de Banco

Revisão **`db526438ced5`**, `down_revision = cbd5647f61b7` (RFC-020). Um único head, cadeia linear preservada. Nenhuma migration existente foi editada.

A coluna é **removida e readicionada**, não alterada:

1. remover o índice `ix_images_embedding_hnsw`
2. remover a coluna `embedding`
3. adicionar `embedding` como `Vector(512)`, nullable
4. recriar `ix_images_embedding_hnsw` com `vector_cosine_ops`

O pgvector codifica a dimensionalidade no modificador de tipo da coluna, então mudá-la é uma mudança de tipo, e `ALTER COLUMN ... TYPE vector(512)` teria de lidar com um índice que depende da coluna. **Não existem embeddings de produção** — toda linha escrita até agora carregava ou `NULL` ou um vetor do `FakeEmbeddingModel` — então não há nada a preservar, e a reconstrução limpa é ao mesmo tempo mais simples e mais segura. `downgrade()` reverte simetricamente de volta para `Vector(1152)`.

Verificado contra o banco em execução após aplicar:

```
 embedding | vector(512) |  | |
Indexes:
    "ix_images_embedding_hnsw" hnsw (embedding vector_cosine_ops)
```

---

## 12. Estratégia de Testes

| camada | arquivo | quantidade | precisa de rede? |
| --- | --- | --- | --- |
| Unidade do adaptador | `tests/infrastructure/ai/test_clip_embedding_model.py` | 33 | não |
| Unidade do tradutor | `tests/infrastructure/ai/test_query_translator.py` | 20 | não |
| Dispositivo | `tests/infrastructure/ai/test_device.py` | 8 | não |
| Fronteiras de camada | `tests/test_ai_layer_boundaries.py` | 9 | não |
| Modelos reais | `tests/infrastructure/ai/test_clip_embedding_model_slow.py` | 26 | **sim, opt-in** |

### 12.1 O marcador `slow`

`markers = ["slow: requires downloading and running real AI models"]` está registrado em `pyproject.toml` (obrigatório — o projeto roda com `--strict-markers`), e `addopts` ganhou `-m 'not slow'`. Um `pytest` puro, portanto, permanece offline e determinístico; `pytest -m slow` faz o opt-in.

### 12.2 Como os testes unitários se mantêm honestos

Os stubs retornam **objetos `BaseModelOutputWithPooling` genuínos contendo tensores torch genuínos**, deliberadamente não normalizados, com um `last_hidden_state` de 768 de largura ao lado do `pooler_output` de 512. Isso significa que os testes unitários exercitam a lógica real do adaptador — Pillow abrindo o arquivo, conversão para RGB, seleção de campo, validação de dimensão, normalização, construção do `EmbeddingVector` — sem download. O que eles não conseguem verificar é que o contrato de hoje *é* o contrato de hoje; é para isso que servem os testes lentos.

### 12.3 Expectativas de decodificação de pixels do RFC-022: ativadas

O `HardCase` do RFC-022 carrega um campo dormente `expect_with_pixel_decoding`. `zero_byte.jpg` e `truncated.jpg` declaram `INDEXED` hoje e `FAILED` assim que algo de fato ler pixels — porque `FakeEmbeddingModel` faz hash do caminho e nunca abre o arquivo.

`TestHardCasesWithRealPixelDecoding` o ativa: roda o caminho de produção completo (`FilesystemImageProvider` → `IndexingWorker` → `IndexOrUpdateImageUseCase` → `ClipEmbeddingModel`) sobre o corpus gerado e garante que

- os dois arquivos estruturalmente quebrados agora falham ao indexar,
- **todos os demais casos continuam indexando**, incluindo os casos CMYK, tons de cinza, TIFF 16-bit, RGBA e com rotação EXIF,
- e que um arquivo quebrado ainda não aborta a execução.

Nenhum comportamento do RFC-022 foi alterado para fazer isso passar. O dataset já estava correto.

### 12.4 Propagação de erros

`encode_image()` não captura nada. `IndexingWorker.run()` já isola falhas por arquivo, e um segundo `except` genérico esconderia qual arquivo falhou e por quê. Os testes garantem que arquivos de zero bytes levantam `UnidentifiedImageError`, arquivos truncados levantam `OSError`, arquivos ausentes levantam `FileNotFoundError`, e que uma falha deixa o adaptador utilizável para o próximo arquivo.

### 12.5 Qualidade de recuperação: ainda dormente, e por quê

`queries.json` permanece dormente, e `test_relevant_images_rank_above_hard_negatives` permanece `xfail`.

O RFC-023 removeu um dos dois bloqueios — agora existe um modelo semântico real. O outro bloqueio está intocado e pertence a outro RFC: **`SearchImagesUseCase` ainda codifica a consulta, descarta o embedding e retorna `repository.list()` sem ranquear.** Não há busca por similaridade a medir. Implementar uma como efeito colateral do RFC-023 seria exatamente o tipo de expansão de escopo que este RFC existe para evitar.

O motivo do `xfail` foi reescrito para dizer isso, de modo que o próximo leitor não seja induzido a pensar que um modelo real era a peça que faltava.

### 12.6 Testes existentes atualizados para 512

`test_image_model.py`, `test_postgres_image_repository.py` e `test_sqlalchemy_models_architecture.py` carregavam literais `1152` e foram atualizados. `test_alembic_migrations.py` fixava uma cadeia exata de três revisões e agora espera quatro, mais novas asserções sobre a revisão do RFC-023. `FakeEmbeddingModel` não precisou de mudança — ele já lê `settings.embedding_dimension`.

---

## 13. Dependências

Adicionadas a `backend/requirements.txt`:

| pacote | por quê |
| --- | --- |
| `torch` | Runtime de inferência para os dois checkpoints |
| `transformers` | `CLIPModel`/`AutoProcessor`; `AutoTokenizer`/`AutoModelForSeq2SeqLM` |
| `sentencepiece` | Exigido por `MarianTokenizer` — o opus-mt distribui um vocabulário SentencePiece |
| `langdetect` | Detecção de idioma (seção 7.1) |

**`accelerate` deliberadamente não foi adicionado**, apesar de estar presente no ambiente do experimento. O adaptador carrega com um simples `from_pretrained(...)` + `.to(device)` e nunca usa `device_map` nem caminhos de baixa memória de CPU, então nada o exige.

`sacremoses` também não foi adicionado. O tokenizador Marian emite um aviso "Recommended: pip install sacremoses" sem ele; a tradução funciona corretamente de qualquer forma, como as próprias execuções do bake-off demonstram (elas emitiram o mesmo aviso).

Versões instaladas usadas na verificação: torch 2.13.0+cpu, transformers 5.15.0, sentencepiece 0.2.2, langdetect 1.0.9.

### 13.1 Uma nota sobre mypy

O transformers 5.15.0 distribui `py.typed`, mas suas anotações estão quebradas para três chamadas que este adaptador faz: `nn.Module.eval` não é anotado, `PreTrainedModel.to` está envolvido em um decorador cuja assinatura o mypy lê como recebendo o próprio modelo, e `PreTrainedModel` não declara `generate` de forma alguma. Quatro comentários `# type: ignore[...]` estreitamente escopados cobrem exatamente esses casos, cada um com uma explicação. `warn_unused_ignores` está ligado, então eles serão sinalizados no momento em que um release os tipar corretamente.

`langdetect` não distribui informação de tipos; uma sobrescrita escopada de `ignore_missing_imports` cobre apenas aquele módulo.

---

## 14. Desempenho

Na máquina do bake-off (apenas CPU), para o checkpoint selecionado:

| operação | custo |
| --- | --- |
| Carga do modelo CLIP | 3,8 s (uma vez por processo) |
| Carga do modelo de tradução | 4,8 s (uma vez, e apenas na primeira consulta em português) |
| Codificação de imagem | 0,464 s média, 0,541 s p95 |
| Codificação de texto | 0,098 s média |
| Tradução PT→EN | 0,938 s média por consulta |
| **100.000 imagens, processo único** | **12,90 h (0,54 dias)** |

A latência do caminho de consulta para uma consulta em inglês é dominada pela codificação de texto (~0,1 s). Uma consulta em português adiciona ~0,94 s de tradução, mais ~4,8 s uma única vez na primeira consulta desse tipo em um processo.

O número de 12,90 horas é de processo único. Indexação multiprocesso não está implementada e não faz parte deste RFC; note que cada processo worker manteria sua própria cópia do modelo em memória, o que é a razão de o singleton ser por processo e não por requisição.

### 14.1 Contra as metas de `ARCHITECTURE.md` §22

| meta | medido | veredito |
| --- | --- | --- |
| Throughput de indexação > 1 imagem/s em CPU | 0,464 s/img = **2,16 img/s** | ✅ atendida, margem de ~2× |
| Latência de busca < 1 s (consulta em inglês) | ~0,098 s de codificação de texto | ✅ atendida, com folga para a busca |
| Latência de busca < 1 s (consulta em português) | 0,938 s de tradução + 0,098 s de codificação ≈ **1,04 s** | ❌ **já acima do orçamento** |
| Tempo de inicialização < 5 s | modelos carregam de forma preguiçosa, não na inicialização | ✅ atendida — mas ver abaixo |

**O caminho de consulta em português não atende à meta documentada de latência de busca, e a excede antes de a busca vetorial ter sido implementada.** A tradução é um custo em tempo de consulta, ao contrário do ganho de velocidade de indexação que justificou escolher o CLIP em primeiro lugar, então o trade-off da seção 3.5 é mais precisamente *throughput de indexação em inglês comprado ao preço da latência de consulta em português*. Este é um problema real e medido que o RFC de busca vetorial herdará; está registrado aqui em vez de descoberto lá.

Mitigações existem e deliberadamente não estão implementadas neste RFC: cachear embeddings de consulta (consultas se repetem, e a codificação é determinística), fazer batching da decodificação de tradução, ou migrar para um modelo nativamente multilíngue quando o throughput permitir (seção 19, questão 1).

Sobre a inicialização: o carregamento preguiçoso mantém o start do processo abaixo da meta, mas o custo é adiado, não removido. A primeira requisição de um processo paga ~3,8 s de carga do CLIP, e a primeira consulta em português paga mais ~4,8 s de carga do modelo de tradução. Se a latência da primeira requisição importar mais que a de inicialização em um dado deploy, uma chamada de aquecimento no boot é a alavanca — o provider com `lru_cache` torna isso uma única linha.

---

## 15. Alternativas Consideradas

| alternativa | por que não |
| --- | --- |
| SigLIP2 (`siglip2-base-patch16-384`) | 10,7× mais lento por imagem para acurácia strict-EN e pairwise idênticas (3.5). Vence em português nativo; perde decisivamente em throughput. |
| `jinaai/jina-clip-v2` | 16,0% de acurácia estrita neste corpus a 48,7 s/imagem (3.2). Não competitivo em nenhum eixo. |
| CLIP de 768 dimensões (ViT-L/14) | 16,6× mais lento sem ganho em pairwise (3.2). |
| `openai/clip-vit-base-patch32` | Estritamente dominado pelo checkpoint LAION em toda métrica (3.1). |
| Quantização dinâmica INT8 | 1,44× de velocidade por −12/−24/−14 pontos de acurácia e desvio médio de cosseno de 0,24 (3.7). |
| ONNX + quantização estática | Tentado; `quantize_static()` nunca se completou (onnxruntime#21979). Sem dados. |
| Batching | 1,17× no melhor tamanho de lote — não vale remodelar a porta e os casos de uso (3.7). |
| `an aerial photo of {query}` | Pior dos três templates, apesar do domínio (3.3). |
| Alimentar o CLIP diretamente com português | 8 pontos pior que traduzir antes no modelo selecionado (3.4). |
| `pipeline("translation")` | Não registrado de forma confiável para este checkpoint no transformers 5.15 (7.2). |
| Parâmetro de idioma em `encode_text()` | Vaza um detalhe específico de modelo para uma porta agnóstica a modelo (5.1). |
| Normalizar dentro de `EmbeddingVector` | Impõe a convenção de um espaço de embedding a todo modelo futuro (6.1). |
| Singleton eager (padrão do RFC-016) | Baixaria modelos no import do módulo (9). |
| Framework pesado de detecção de idioma | Desproporcional ao problema; nenhuma dependência dessas existe (7.1). |

---

## 16. Riscos

1. **O português é mensuravelmente mais fraco que o inglês** — 56,0% vs. 60,0% estrito, e a diferença aumenta para consultas curtas que o detector não pega. Aceito, com a alternativa SigLIP2 documentada para revisão futura.
2. **A latência de consulta em português já excede a meta de `ARCHITECTURE.md` §22 de < 1 segundo** — ~1,04 s para tradução mais codificação de texto, antes de qualquer busca vetorial rodar (seção 14.1). O problema não resolvido mais concreto que este RFC deixa para trás.
3. **A qualidade da tradução limita a acurácia em português.** `tanque` → `tank` em vez de `pond` é um erro real e observado em um corpus cheio de tanques.
4. **Consultas de uma palavra contornam a tradução inteiramente** (7.1). Usuários reais digitam consultas curtas.
5. **Uma atualização do transformers pode mudar o que `get_image_features()` retorna.** Isso já mudou uma vez. Os testes lentos são o fio de tropeço; os stubs dos testes unitários não pegariam.
6. **A acurácia absoluta é modesta.** 60,0% de top-1 estrito em 25 consultas contra 45 imagens. As próprias listas de falha do bake-off mostram fraqueza consistente em consultas de nível de rua e de paisagem urbana em *todos* os modelos testados — provavelmente uma propriedade do corpus e do fraseado das consultas, não apenas do modelo. Fine-tuning é a alavanca.
7. **Conjunto de avaliação pequeno.** 45 imagens, 25 consultas. Uma consulta vale 4 pontos percentuais. Trate diferenças abaixo de ~8 pontos como ruído.
8. **Mudar de modelo ou de dimensão exige reindexação completa** e uma migration de esquema (seção 18).
9. **O bake-off não é reproduzível a partir do repositório** — scripts e README estão ausentes e não rastreados (1.1). Os logs são a única evidência sobrevivente.

---

## 17. Não-objetivos

Explicitamente não entregues por este RFC, e verificados como ausentes:

- busca por similaridade vetorial (`SearchImagesUseCase` está intocado)
- batching de qualquer tipo
- fine-tuning, LoRA ou adapters
- quantização
- ONNX ou qualquer backend alternativo de inferência
- backends de GPU além de CUDA
- indexação multiprocesso ou distribuída
- prompt ensembling
- substituir `FakeEmbeddingModel` (ele continua sendo o duplo de teste)

---

## 18. Fine-tuning e Trabalho Futuro

O checkpoint escolhido é um **ponto de partida, não um compromisso arquitetural com CLIP**. Trabalho futuro pode incluir fine-tuning específico de domínio sobre imagens aéreas de propriedades, camadas LoRA ou adapters, um checkpoint CLIP diferente, SigLIP ou outro modelo multilíngue, ou um backend de inferência otimizado.

Todos esses são substituíveis atrás de `EmbeddingModelPort`. Nenhum exige mudança no Domain ou na Application. Esse é o objetivo inteiro da seção 5.

**A única consequência que não pode ser esquecida:** mudar o modelo de embedding — ou meramente sua dimensão — invalida todo vetor armazenado. Isso exige

1. uma nova migration do Alembic se a dimensão mudar,
2. uma reindexação completa de todas as imagens,
3. uma reconstrução do índice HNSW.

Isso é esperado e normal para sistemas de embedding. Não pode vazar para o Domain nem para a Application, e não vaza. Uma estratégia de migração para isso deliberadamente não é projetada aqui; ela pertence ao RFC que de fato trocar o modelo.

Também adiado: **cache de embeddings de texto**. Embeddings de consulta são determinísticos e o batching de texto mostrou ganho de 2,7× — mas nenhum dos dois importa até existir busca vetorial que torne a latência de consulta visível.

---

## 19. Questões em Aberto

1. O suporte a português deve migrar para um modelo nativamente multilíngue quando o throughput for resolvido (GPU, batching ou multiprocesso)? Os 64,0% de strict-PT nativo do SigLIP2 são a marca a superar.
2. O teto de ~60% de top-1 estrito é limitação do modelo ou artefato do corpus/fraseado das consultas? Todo modelo testado falhou nas mesmas consultas de nível de rua e paisagem urbana, o que aponta para a segunda hipótese.
3. Os scripts do bake-off devem ser commitados para que os números da seção 3 sejam reproduzíveis? Hoje estão ausentes e não rastreados.
4. Qual é a estratégia correta para consultas curtas — uma lista curada de termos em PT, um detector melhor, ou aceitar a lacuna?

---

## 20. Entregáveis

**Novos:**

- `backend/app/infrastructure/ai/clip_embedding_model.py`
- `backend/app/infrastructure/ai/query_translator.py`
- `backend/app/infrastructure/ai/device.py`
- `backend/alembic/versions/db526438ced5_change_embedding_to_512_dimensions.py`
- `backend/tests/infrastructure/ai/test_clip_embedding_model.py`
- `backend/tests/infrastructure/ai/test_clip_embedding_model_slow.py`
- `backend/tests/infrastructure/ai/test_query_translator.py`
- `backend/tests/infrastructure/ai/test_device.py`
- `backend/tests/test_ai_layer_boundaries.py`
- `docs/rfcs/rfc-023-adaptador-de-embedding-clip.md`

**Modificados:**

- `backend/app/infrastructure/ai/__init__.py` — documenta por que o adaptador não é reexportado
- `backend/app/infrastructure/config/settings.py` — defaults de modelo, dimensão e dispositivo
- `backend/app/infrastructure/database/models/image_model.py` — `Vector(512)`
- `backend/app/domain/value_objects/index_metadata.py` — docstring tornada agnóstica à dimensão
- `backend/app/presentation/dependencies/__init__.py` — singleton preguiçoso do CLIP
- `backend/requirements.txt` — torch, transformers, sentencepiece, langdetect
- `pyproject.toml` — marcador `slow`, `-m 'not slow'`, override do mypy para langdetect
- `.env.example` — novos defaults
- `backend/tests/dataset/test_queries.py` — dormência reexplicada
- `backend/tests/infrastructure/test_alembic_migrations.py` — cadeia de quatro revisões + asserções do RFC-023
- `backend/tests/infrastructure/test_image_model.py`, `test_sqlalchemy_models_architecture.py`, `tests/infrastructure/persistence/test_postgres_image_repository.py` — 1152 → 512
- `backend/tests/presentation/test_dependencies.py` — fiação do CLIP, preguiça, ausência de download

**Inalterados, deliberadamente:** `EmbeddingModelPort`, `EmbeddingVector`, `FakeEmbeddingModel`, `SearchImagesUseCase`, `IndexImageUseCase`, `IndexOrUpdateImageUseCase`, `IndexingWorker`, e todo arquivo de dataset do RFC-022.

---

## 21. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | 297 passaram, 26 desselecionados, 1 xfailed |
| `pytest -m slow` | 26 passaram |
| `pytest` com `HF_HUB_OFFLINE=1` + `HF_HOME` vazio | 297 passaram — nenhum download |
| `black --check .` | limpo |
| `ruff check .` | limpo |
| `mypy` | 5 erros, todos preexistentes e não relacionados (linha de base confirmada dando stash neste branch) |
| `alembic heads` | `db526438ced5 (head)` — exatamente um |
| `alembic current` | `db526438ced5 (head)` |
| esquema | `embedding vector(512)` |
| índice | `ix_images_embedding_hnsw hnsw (embedding vector_cosine_ops)` |
| linhas residuais de teste | `SELECT count(*) FROM images` → 0 |
