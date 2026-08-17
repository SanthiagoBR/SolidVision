# RFC-023 pre-implementation bake-off

Exploration tooling and results, not application code. This directory
exists to answer one question before any RFC-023 (SigLIP Adapter) code is
written: **which SigLIP 2 checkpoint should `settings.embedding_model`
default to?**

Kept off `develop` deliberately -- see the branch this lives on
(`explore/siglip-bakeoff`). Nothing here is imported by `backend/app/`, and
none of it is meant to be merged as-is. Whatever RFC-023 actually needs
(a chosen checkpoint, a verified dimension, measured latency figures) gets
written into the RFC and the real adapter on `develop`; this directory is
the working notes behind that decision.

## Ground truth changed partway through -- numbers below aren't all on the same corpus

The bake-off and template-check results were measured against the
*original* RFC-022 corpus: 9 queries, and (unknown at the time) two
mislabeled images that made 2 of those 9 queries structurally unwinnable
regardless of model quality. Both were fixed and the query set expanded
to 25 (see the "Fix two mislabeled images" and "Expand RFC-023 bake-off
query set" commits on this branch). The quantization check below is the
first script run against the corrected, expanded ground truth -- its
fp32 baseline for `base` (60.0%/64.0%/80.4%) is not directly comparable
to `base`'s numbers in the bake-off table (44.4%/55.6%/79.3%) below; the
corrected number is the more trustworthy one of the two.

## Why 1152 (`vector(1152)`, migration `999b801e80f4`) constrains the choice

`embedding_dimension` is committed at 1152 and matches the SigLIP so400m
hidden size in both SigLIP generations. That already ruled out the base
(768-dim) and large (1024-dim) checkpoints on paper -- until it became
clear that changing the dimension today costs one Alembic migration and a
45-image re-seed, versus a full production reindex later. Cheap enough
now to be worth re-litigating with actual data instead of assuming so400m
by default. See `docs/rfcs/rfc-022-demo-dataset.md` §7.1/§7.4 for why the
demo corpus and `queries.json` exist in the first place -- this bake-off
is the first real consumer of both.

## `siglip_bakeoff.py`

Loads each of three SigLIP 2 checkpoints, encodes all 45 demo-corpus
images and 18 queries (9 English from `backend/dataset/demo/queries.json`
+ 9 hand-written Portuguese translations -- the product README advertises
Portuguese search), and scores against the committed ground truth using
the exact predicate already sitting dormant in
`backend/tests/dataset/test_queries.py::test_relevant_images_rank_above_hard_negatives`.

Auto-detects CUDA vs CPU and locates the repo root on its own, so it runs
unmodified on any machine inside any checkout.

### Results — CPU-only laptop (`results_cpu_laptop.json`)

| Checkpoint | Dim | Strict-EN | Strict-PT | Pairwise | s/image (mean) | Load time | Projected 100k images |
|---|---|---|---|---|---|---|---|
| `siglip2-so400m-patch14-384` | 1152 | 33.3% | 55.6% | 87.8% | 22.72s | 109.4s | 26.3 days |
| `siglip2-large-patch16-384` | 1024 | 33.3% | 44.4% | 81.1% | 11.11s | 97.4s | 12.9 days |
| `siglip2-base-patch16-384` | 768 | **44.4%** | 55.6% | 79.3% | **3.66s** | **8.8s** | **4.2 days** |

*Strict = every relevant image outranks every hard negative for that
query. Pairwise = fraction of individual relevant/hard-negative
comparisons ordered correctly -- softer, since several queries have up
to 6 relevant images against 2-3 hard negatives, so one weak match tanks
the strict score. 100k projection is single-process, unbatched, CPU --
not a claim about production throughput, which needs batching (explicit
future work, not RFC-023 scope).*

**Finding:** `base` had the *highest* strict accuracy of the three while
being ~6x faster than `so400m`. `so400m` only leads on the softer
pairwise metric. Bigger did not mean better at the metric that actually
matters here on this corpus.

**Caveats, not swept under the rug:**
- 9 English queries means each one is worth ~11 percentage points --
  the direction (base >= so400m/large on strict) is a real pattern in
  this run, the exact percentages are not precise.
- The same ~5-6 queries fail across all three models (the
  lake-vs-pond-vs-pool distinction, warehouse-vs-farmland, several urban
  categories). RFC-022 §6.1 built those hard negatives to be genuinely
  hard; consistent failure across checkpoints reads as the eval set
  doing its job, not a broken pipeline.
- Portuguese did not underperform English on any checkpoint -- doesn't
  prove PT is *better* (same small-sample caveat), but it does clear the
  concern that motivated picking SigLIP 2 (multilingual) over SigLIP 1
  (English-only) in the first place.

## `siglip_template_check.py`

Follow-up check on the two poles (`so400m` and `base`) from the main
bake-off, testing whether a CLIP-style caption template changes the
strict-accuracy numbers above. SigLIP's own paper claims no template is
needed, trained as it was on raw web captions unlike CLIP -- this either
confirms that claim against this project's corpus, or overturns an
assumption before it becomes load-bearing in the RFC.

### Results (`template_check_results.json`)

| Checkpoint | `{query}` (raw) | `"a photo of {query}"` | `"an aerial photo of {query}"` |
|---|---|---|---|
| `so400m` strict | 33.3% | 33.3% | **55.6%** |
| `so400m` pairwise | 85.4% | 85.4% | 89.0% |
| `base` strict | **44.4%** | 33.3% | 44.4% |
| `base` pairwise | 72.0% | 69.5% | 69.5% |

**This is not a clean confirmation of the "no template needed" claim.**
A generic CLIP-style template ("a photo of ...") does nothing for either
checkpoint -- flat or worse. But a *domain-specific* template ("an aerial
photo of ...") lifts `so400m` from 33.3% to 55.6% strict accuracy, which
is now the single best result across every checkpoint/template
combination tested, `base` included. `base` is unmoved by the aerial
template either way (44.4% with or without it).

This reopens the speed/quality tradeoff rather than closing it:
`so400m` + the aerial template now beats `base`'s best result on the
metric that matters, but still costs ~6.2x the per-image latency
(22.7s vs 3.7s) and ~12.4x the load time (109s vs 8.8s) measured in the
main bake-off. Adopting a hardcoded domain template also isn't free in
another sense -- it would mean `SearchImagesUseCase` wraps every user
query in "an aerial photo of ..." before encoding, which only makes
sense while the corpus stays aerial-photography-specific; ARCHITECTURE.md
§9's stated direction (future adapters, possibly future non-aerial
collections) makes that a real generality cost, not just an
implementation detail.

Same small-sample caveat as the main bake-off applies with more force
here: 9 queries means the aerial-template jump for `so400m` is exactly 2
queries flipping from fail to pass. Real, but not a lot of queries to
generalize a product decision from.

## `siglip_quantization_check.py`

Tests whether torch's built-in INT8 dynamic quantization
(`torch.quantization.quantize_dynamic`, `nn.Linear` layers) is a viable
CPU speed optimization for `base` -- the checkpoint the bake-off picked.
CPU-only by design: dynamic quantization has no meaningful CUDA path in
stock PyTorch, unlike static/QAT quantization or GPU toolchains (TensorRT
etc.), which are out of scope here. Auto-detects whichever quantized
backend the local torch build actually supports (fbgemm, qnnpack, or
oneDNN) rather than assuming one.

### Results (`quantization_check_results.json`)

| Metric | fp32 | INT8 | Change |
|---|---|---|---|
| s/image (mean) | 4.960s | 3.437s | 1.44x faster |
| s/text (mean) | 0.504s | 0.343s | 1.47x faster |
| strict accuracy (EN) | 60.0% | 48.0% | -12 points |
| strict accuracy (PT) | 64.0% | 40.0% | -24 points |
| pairwise accuracy | 80.4% | 66.4% | -14 points |
| 100k-image projection | 5.74 days | 3.98 days | saves 1.76 days |

Embedding drift (cosine similarity between the fp32 and INT8 embedding of
the *same* input, independent of any query): images mean 0.7582 (min
0.6779), texts mean 0.9010 (min 0.7173). For reference, unrelated content
in this corpus typically scores well below 0.5 cosine similarity -- 0.76
between two encodings of the *identical* image is a large, not subtle,
perturbation.

**Rejected.** The speedup (1.44x) is real but far short of the 2-4x
usually cited for dynamic quantization -- that figure holds for models
where only a few output logits need to survive the added noise
(classification). SigLIP's retrieval task depends on precise relative
angles across the entire embedding space, and `quantize_dynamic` only
touches `nn.Linear`, leaving attention/softmax/LayerNorm/GELU in fp32;
that mismatch compounds across a deep transformer in a way classification
doesn't expose. Trading 1.4x speed to break roughly 1 in 4 Portuguese
queries that worked in fp32 is not a good trade.

Not tested here, and still open if CPU throughput becomes a hard blocker
later: ONNX Runtime / OpenVINO export (`optimum` library), which
typically stacks graph-level fusion with quantization rather than relying
on `quantize_dynamic` alone, and might not show the same accuracy cliff --
but that is a heavier lift (new export pipeline, new runtime dependency)
that wasn't justified chasing after this result.

One environmental caveat: this run's fp32 baseline (4.96s/image) was
slower than the original bake-off's fp32 number for `base` (3.66s/image)
-- almost certainly machine load variance between sessions, not a
regression. The fp32-vs-INT8 comparison itself is unaffected, since both
ran in the same process under the same conditions; only the cross-session
comparison is unreliable.

## `siglip_batching_check.py`

Tests whether batched encoding (multiple images/texts per forward pass,
instead of one at a time as every other script here does) helps
throughput. Relevant only to bulk image indexing -- a live search query
is always a single text string, batching cannot help that path.

### Results (`batching_check_results.json`)

| Batch size | s/image | Speedup | 100k projection |
|---|---|---|---|
| 1 | 3.850 | 1.00x | 4.46 days |
| 2 | 3.435 | 1.12x | 3.98 days |
| 4 | 3.412 | 1.13x | 3.95 days |
| 8 | 3.492 | 1.10x | 4.04 days |
| 16 | 3.606 | 1.07x | 4.17 days |
| **32** | **3.282** | **1.17x** | **3.80 days** |

Text batching showed a much bigger win: 0.788s/text (batch=1) down to
0.291s/text (batch=25), a 2.7x speedup -- explained by text being
overhead-bound at batch=1 (Python call cost, tokenization, tensor
allocation dominate a short sequence's actual compute), which batching
amortizes away, whereas a 384px image's forward pass is compute-bound
from the start on this 2-core dev CPU, leaving little idle parallelism
for batching to fill.

Correctness confirmed, not assumed: cosine similarity between each
batch-of-1 embedding and its embedding when encoded as part of a larger
batch was exactly 1.0000 at every batch size tested. Batching is a
computational reorganization, not a model change, and this verifies it
behaves that way rather than silently corrupting embeddings through
padding or batch-dependent normalization.

**Kept: batch_size=32 for image encoding**, going forward into whatever
adapter or optimization scripts follow (including the ONNX
Runtime/OpenVINO check next). The gain (1.17x) is real but modest --
this CPU's low core count (2 physical cores) limits how much idle
parallelism batching can recover, and this result should not be read as
"batching solved the 100k-image target." It didn't: 3.8 days is still
far from viable production throughput. The more promising unexplored
lever remains calibrated static quantization via ONNX Runtime/OpenVINO,
targeting VNNI-capable hardware (the user's Intel Core i5-1035G1, Ice
Lake, has AVX-512 + DL Boost/VNNI) rather than better CPU utilization of
the same fp32 compute.

## `siglip_onnx_quantization_check.py` (abandonado)

Tentativa de testar quantização estática calibrada via ONNX Runtime
(`onnxruntime.quantization.quantize_static`), como alternativa à
quantização dinâmica ingênua já rejeitada acima. Diferente de
`quantize_dynamic` -- que estima os limites de ativação "no chute" e só
mexe em camadas `nn.Linear` -- a quantização estática calibra os limites
reais usando dados representativos (as próprias imagens do corpus de
demonstração) e quantiza o grafo ONNX inteiro, não só as camadas
lineares. Em teoria, uma técnica mecanicamente diferente o suficiente
para merecer sua própria medição, em vez de herdar a rejeição da
quantização dinâmica por associação.

Na prática, o processo travou a máquina de desenvolvimento (Pentium
G4560, 16GB de RAM) três vezes seguidas, e foi abandonado sem nunca
produzir um resultado de precisão utilizável.

### O que aconteceu

A arquitetura de duas torres do SigLIP (`get_image_features` /
`get_text_features` como pontos de entrada separados, sem um único
`forward()`) exigiu exportar cada torre manualmente para ONNX via
`torch.onnx.export`, envolvendo `vision_model` / `text_model` em um
wrapper fino. Essa parte funcionou perfeitamente e foi verificada, não
apenas assumida: a saída do ONNX Runtime em fp32 bateu com a do PyTorch
eager com similaridade de cosseno de ~1,0000001 (diferença máxima
absoluta de ~5e-7), inclusive com tamanhos de lote diferentes do usado
na exportação.

O problema apareceu na etapa seguinte, `quantize_static()`:

1. **Primeira tentativa** (torre de texto + torre de visão, tudo em um
   único script, quatro sessões do ONNX Runtime mantidas na memória ao
   mesmo tempo): a máquina travou de verdade durante a execução em
   segundo plano. Nada foi perdido -- o `git status` estava limpo antes
   de começar -- mas foi um travamento real do sistema, não só um script
   lento ou com erro.
2. Depois de reiniciar, o script foi reescrito para isolar cada fase
   pesada em seu próprio processo do sistema operacional (exportar,
   quantizar, codificar), já que a limpeza dentro do processo
   (`del`/`gc.collect()`) não garante que os alocadores nativos (C++) do
   PyTorch e do ONNX Runtime devolvam memória ao SO de forma confiável.
   A exportação isolada funcionou perfeitamente e liberou toda a memória
   ao terminar.
3. **Segunda tentativa** (só a torre de texto, isolada, 25 consultas
   para calibração): mesmo isolada, o processo cresceu para **16,19GB**
   de memória privada comprometida em cerca de 2 minutos, numa máquina
   de 16GB no total. Encerrado à força antes de repetir o travamento.
   Causa provável: o vocabulário multilíngue do SigLIP2 (~256 mil
   tokens, tokenizador Gemma) torna o arquivo ONNX da torre de texto
   1,05GB -- quase 3x o tamanho da torre de visão (357MB).
4. Dado que a quantização da torre de texto nunca foi o ponto principal
   (a busca por texto já é rápida, sub-segundo, uma vez por consulta --
   quem importa para a meta de indexação de 100 mil imagens é a torre de
   visão), a torre de texto foi abandonada e a torre de visão testada
   sozinha.
5. **Terceira tentativa** (só a torre de visão, isolada, 45 imagens de
   calibração): cresceu para **11,84GB** de memória privada. Encerrada à
   força de novo.
6. Pesquisa (ver `microsoft/onnxruntime#21979` no GitHub) revelou que
   isso é um **bug conhecido e não resolvido** na ferramenta de
   quantização estática do ONNX Runtime: o método `collect_data` do
   `Calibrator` retém todas as ativações intermediárias de cada imagem
   de calibração, para cada nó do grafo, simultaneamente -- sem calcular
   nada de forma incremental. A própria pessoa que reportou o bug disse
   só conseguir calibrar "com um casal de imagens". Sem versão de
   correção documentada depois de quase dois anos em aberto.
7. **Quarta tentativa, a última combinada com o usuário antes de desistir**
   (só a torre de visão, isolada, calibração reduzida para **8 imagens**
   -- um único lote, aplicando diretamente a solução alternativa
   documentada no issue): o crescimento de memória foi **igualmente
   rápido**, chegando a território perigoso antes mesmo do processo
   imprimir a primeira linha de log da chamada `quantize_static()` real.
   Um simples comando de verificação de memória do PowerShell chegou a
   travar por mais de 120 segundos -- sinal de que o sistema já estava
   sob pressão severa de memória -- e o processo foi encerrado à força
   via `taskkill` (mais confiável que os cmdlets do PowerShell nessas
   condições).

### Veredito final

**Abandonado.** Reduzir os dados de calibração em quase 6x (45 → 8
imagens), com base numa causa raiz real e documentada, não ajudou nada
-- se algo, a escalada foi mais rápida na quarta tentativa. Isso indica
que o diagnóstico do bug do GitHub provavelmente está correto mas é
incompleto: alguma coisa neste modelo específico, nesta versão do
`onnxruntime` (1.28.0), ou neste ambiente está causando um crescimento
de memória descontrolado que não é simplesmente proporcional ao volume
de dados de calibração. Três escaladas em direção a um travamento numa
única investigação é o ponto de parar de tentar variações, não de
insistir numa quinta.

Isso deixa as duas otimizações de CPU testadas neste diretório rejeitadas
-- por motivos diferentes, ambas válidas para o registro:
- `torch.quantization.quantize_dynamic`: rodou sem problemas, rejeitada
  por precisão (12-24 pontos de queda na precisão estrita por apenas
  1,44x de velocidade).
- Quantização estática via ONNX Runtime: rejeitada por segurança de
  execução, antes mesmo de produzir qualquer resultado de precisão.

O que resta validado para a RFC-023: `google/siglip2-base-patch16-384`,
`batch_size=32` para codificação de imagens. Essa é a resposta real,
comprovada, a ser levada adiante.

## Reproducing on different hardware

`siglip_bakeoff.py` is written to run unmodified wherever it's placed
inside a checkout of this repo. See the setup instructions in its own
docstring. A second results file was expected from a friend's NVIDIA GPU
machine as an additional hardware data point; add it here as
`results_<description>.json` if/when it lands, following the same naming
pattern as `results_cpu_laptop.json`.
