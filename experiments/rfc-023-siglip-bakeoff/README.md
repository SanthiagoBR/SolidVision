# RFC-023 pre-implementation bake-off

Exploration tooling and results, not application code. This directory
exists to answer, empirically, which embedding model `backend/app`'s real
adapter should use -- the question started out as "which SigLIP 2
checkpoint," but the CLIP comparisons below reopened the question of
model *family*, not just checkpoint, and changed the answer. See the
decision below before reading the rest of this document as a historical
log of how it was reached, not as the current recommendation on its own.

Kept off `develop` deliberately -- see the branch this lives on
(`explore/siglip-bakeoff`). Nothing here is imported by `backend/app/`, and
none of it is meant to be merged as-is. Whatever RFC-023 actually needs
(a chosen checkpoint, a verified dimension, measured latency figures) gets
written into the RFC and the real adapter on `develop`; this directory is
the working notes behind that decision.

## Decisão final: adapter CLIP, não SigLIP

Depois de todo o bake-off do SigLIP abaixo e dos experimentos de CLIP em
`clip_jina_bakeoff.py`, `clip_small_dim_bakeoff.py`,
`clip_pt_translation_bakeoff.py` e `clip_template_bakeoff.py`, a decisão
para o RFC-023 é:

- **Modelo:** `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` (512-dim) -- não
  nenhum checkpoint do SigLIP, apesar de todo o trabalho abaixo ter
  começado assumindo que seria um.
- **Template de consulta de texto:** `"a photo of {query}"` aplicado
  antes de codificar (ver `clip_template_bakeoff.py`). **Não** usar
  `"an aerial photo of {query}"` -- esse template ajudou o SigLIP so400m
  no bake-off original, mas piorou este modelo CLIP em todos os testes.
- **Suporte a português:** tradução PT→EN antes de codificar
  (`Helsinki-NLP/opus-mt-ROMANCE-en`, tag `>>por<<`), não suporte
  multilíngue nativo -- ver `clip_pt_translation_bakeoff.py`. O
  `Helsinki-NLP/opus-mt-pt-en` original não existe mais no Hub, verificado
  antes de adotar o `ROMANCE-en` como alternativa.
- **Por quê:** ~10x mais rápido que o `siglip2-base-patch16-384` pra
  indexar o acervo (0,46-0,70s/imagem medido vs ~4,96s/imagem), e com o
  template acima chega a 60,0% de acurácia estrita em inglês -- empatando
  com o SigLIP base -- e 56,0% em português traduzido.
- **Trade-off aceito conscientemente:** o gap de português contra o
  SigLIP nativo (64,0%) não fecha totalmente mesmo com tradução e
  template (fica em ~56,0%, ainda 8 pontos abaixo). Essa perda de
  acurácia em português foi aceita em troca da velocidade de indexação.
- **Fine-tuning:** ainda esperado como trabalho futuro, quando houver um
  acervo real maior que as 45 imagens de demonstração -- fine-tuning
  nesse corpus pequeno teria risco sério de overfitting, então não faz
  parte desta decisão nem do RFC-023 em si. Ver `build-prompt.md`.

Próximo passo: `build-prompt.md`, nesta mesma pasta, é o prompt para pedir
a um agente que implemente essa decisão em `backend/app/`.

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

## `clip_jina_bakeoff.py` -- is SigLIP even the right family?

Everything above compares SigLIP 2 checkpoints against each other. This
script asks a different question: now that `base` (768-dim) is the
leading SigLIP 2 checkpoint, does a same-dimension model from a
*different* family beat it? Three 768-dim, non-SigLIP candidates, run
against the same corrected 25-query ground truth and scoring predicate as
`quantization_check_results.json`'s fp32 baseline (the fairest
apples-to-apples comparison available, since the original bake-off table
above used the pre-correction 9-query ground truth):

- `openai/clip-vit-large-patch14` -- the canonical CLIP ViT-L/14
- `laion/CLIP-ViT-L-14-laion2B-s32B-b82K` -- OpenCLIP retrain of the same
  architecture on LAION-2B
- `jinaai/jina-clip-v2` -- multilingual, natively 1024-dim, truncated to
  768 via Matryoshka `truncate_dim=` to match the other two

### Results (`clip_jina_bakeoff_results.json`)

| Model | Dim | Strict-EN | Strict-PT | Pairwise | s/image (mean) | Load time | Projected 100k images |
|---|---|---|---|---|---|---|---|
| `openai/clip-vit-large-patch14` | 768 | 56.0% | 44.0% | 72.5% | 7.694s | 8.1s | 8.90 days |
| `laion/CLIP-ViT-L-14-laion2B-s32B-b82K` | 768 | 52.0% | 32.0% | 71.2% | 6.751s | 47.6s | 7.81 days |
| `jinaai/jina-clip-v2` | 768 | 16.0% | 16.0% | 56.6% | 48.674s | 60.7s | 56.34 days |
| **`siglip2-base-patch16-384`** (fp32, same ground truth) | 768 | **60.0%** | **64.0%** | **80.4%** | **4.960s** | -- | -- |

**Finding: SigLIP 2 `base` wins outright.** It beats every non-SigLIP
768-dim candidate tested here on strict accuracy (both languages),
pairwise accuracy, *and* speed -- there's no tradeoff to weigh, it
dominates on every axis measured. This is a real answer to "is SigLIP
the right family," not just "the right checkpoint within SigLIP": at
this dimension, on this corpus, nothing tried here comes close.

**Per-candidate notes:**
- Both CLIP variants underperform SigLIP on English strict accuracy by a
  meaningful margin (56.0%/52.0% vs 60.0%), and drop further in
  Portuguese (44.0%/32.0% vs 64.0%) since neither has a multilingual text
  tower -- expected, included as the standard baseline everyone compares
  against rather than a real contender for this product's PT requirement.
- `laion`'s LAION-2B retrain does *not* beat the original OpenAI weights
  on this corpus (52.0% vs 56.0% strict-EN, 32.0% vs 44.0% strict-PT) --
  the general finding that OpenCLIP retrains often beat OpenAI CLIP on
  public benchmarks doesn't transfer to this aerial-photography corpus's
  specific hard negatives.
- `jina-clip-v2` is the clear outlier: worst accuracy of all four
  (16.0% strict, both languages) *and* by far the slowest (48.7s/image,
  ~10x SigLIP `base` and even ~2x slower than SigLIP `so400m` at
  22.7s/image from the main bake-off) on this 2-core CPU. Its
  multilingual claim doesn't show up as an advantage here either -- PT
  and EN tied at 16.0%, not PT trailing EN like the CLIP variants, but
  also not PT *beating* EN the way it would need to for the multilingual
  training to read as paying off. Two caveats worth naming rather than
  concluding "jina-clip-v2 is just bad": (1) truncating its native
  1024-dim embedding to 768 via `truncate_dim=` is a real lossy step none
  of the other candidates have, so this may understate its
  full-dimension capability; (2) its EVA02-backbone vision tower is
  simply a heavier forward pass than a plain ViT-L/14 on CPU, which
  explains the latency gap independent of the accuracy question. Neither
  caveat changes the practical conclusion for this project -- both the
  accuracy and the latency independently rule it out at 768-dim on this
  hardware.
- Environmental note, same caveat as the quantization check: this run's
  SigLIP fp32 comparison point (4.960s/image) is from
  `quantization_check_results.json`, a different session than this
  script's own run -- cross-session latency comparisons on this machine
  have shown session-to-session variance before (3.66s vs 4.96s for the
  same checkpoint across two runs). The accuracy comparison is unaffected
  since accuracy doesn't depend on machine load; only the exact speed
  multiplier should be read as directional, not precise.

**What this doesn't test:** larger non-SigLIP models (a 1024-dim or
larger CLIP/OpenCLIP variant might close some of the accuracy gap, at a
dimension cost this bake-off's 768-dim constraint was specifically
avoiding), and jina-clip-v2 at its native 1024-dim rather than truncated.
Neither is planned unless the 768-dim question above becomes live again
-- SigLIP `base` already answers the question this script set out to
ask.

### Environment note

Unlike every earlier script in this directory, `clip_jina_bakeoff.py` was
run from its own isolated `.venv` in this directory (per the setup
instructions in `siglip_bakeoff.py`'s docstring and `requirements.txt`'s
header comment), not the shell's default Python. That default turned out
to resolve to `backend/.venv` -- the application's own virtual
environment -- which already had `torch`/`transformers`/`onnx`/etc.
installed in it from earlier sessions' bake-off work, despite
`requirements.txt`'s explicit instruction that none of this directory's
tooling should run inside the backend's venv. That pre-existing
contamination in `backend/.venv` was left alone rather than stripped out
mid-task; it's a separate cleanup decision from getting this comparison
running correctly.

`jina-clip-v2`'s `trust_remote_code=True` implementation also needed a
small compatibility shim: its remote `modeling_clip.py` imports
`clip_loss` from `transformers.models.clip.modeling_clip`, a symbol
present when that model card was written but removed in the transformers
5.x rewrite this repo's SigLIP and CLIP candidates otherwise depend on
(see `siglip_bakeoff.py`'s own note on 5.x's `BaseModelOutputWithPooling`
change). `clip_jina_bakeoff.py` shims a `clip_loss` function into that
module's namespace before loading jina-clip-v2, rather than pinning an
older transformers globally, which would have broken the CLIP candidates'
`get_image_features` return type. The shim is only exercised by
jina-clip-v2's training-time loss computation, never by the
`encode_image`/`encode_text` paths this script actually calls.

## `clip_small_dim_bakeoff.py` -- does dropping below 768-dim change the CLIP picture?

`clip_jina_bakeoff.py` found SigLIP 2 `base` beating every 768-dim CLIP
candidate outright, no tradeoff to weigh. This script asks a narrower,
CLIP-only follow-up: since ViT-L/14 CLIP (768-dim) already lost on both
accuracy and speed, does dropping to ViT-B (512-dim) -- smaller and
faster in principle -- change that picture, or just make CLIP worse
along with making it smaller? Three 512-dim candidates, same corpus and
ground truth, no jina-clip-v2 this time (CLIP-only by design):

- `openai/clip-vit-base-patch32`
- `openai/clip-vit-base-patch16`
- `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` -- OpenCLIP retrain of patch32
  on LAION-2B, mirroring the OpenAI-vs-LAION split used for the 768-dim
  ViT-L/14 comparison above

Reuses `run_candidate_clip` from `clip_jina_bakeoff.py` unchanged -- it
already just takes a `model_id`, no dimension assumptions baked in.

### Results (`clip_small_dim_bakeoff_results.json`)

| Model | Dim | Strict-EN | Strict-PT | Pairwise | s/image (mean) | Load time | Projected 100k images |
|---|---|---|---|---|---|---|---|
| `openai/clip-vit-base-patch32` | 512 | 44.0% | 44.0% | 68.0% | 0.770s | 4.7s | 0.89 days |
| `openai/clip-vit-base-patch16` | 512 | 56.0% | 36.0% | 70.9% | 1.745s | 4.1s | 2.02 days |
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | 512 | **56.0%** | 44.0% | **74.3%** | **0.464s** | 3.8s | **0.54 days** |
| `siglip2-base-patch16-384` (fp32, 768-dim, for reference) | 768 | 60.0% | 64.0% | 80.4% | 4.960s | -- | -- |

**Finding: unlike the 768-dim comparison, this one is a real tradeoff, not
a rout.** `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` gives up 4 points of
strict-EN accuracy and 20 points of strict-PT accuracy relative to SigLIP
`base`, but runs roughly **10x faster** (0.464s/image vs 4.960s/image) --
under a day projected for 100k images versus ~5.7 days. That's a
meaningfully different shape of result than `clip_jina_bakeoff.py`, where
SigLIP won every axis with nothing to weigh. Whether that tradeoff is
worth taking depends on how hard the 100k-image throughput target from
ARCHITECTURE.md §22 actually binds -- a question this bake-off doesn't
answer on its own, since SigLIP `base` was never *rejected* on throughput,
just not the fastest option available.

**Per-candidate notes:**
- The LAION-2B retrain again beats the original OpenAI weights at the
  same architecture (patch32: 56.0%/44.0% strict vs 44.0%/44.0%), the
  opposite of what the 768-dim ViT-L/14 comparison found (where LAION's
  retrain *underperformed* OpenAI's). Small-sample caveat applies as
  always, but this at minimum rules out "LAION retrains are just
  categorically better/worse than OpenAI CLIP" as a fixed rule on this
  corpus -- it depends on which architecture size.
- `patch16` has the same strict-EN as the LAION patch32 model (56.0%) but
  costs ~3.8x the per-image latency (1.745s vs 0.464s) for a *worse* PT
  score (36.0% vs 44.0%) -- finer patches did not pay for themselves here.
- Portuguese accuracy across all three 512-dim candidates (44.0%, 36.0%,
  44.0%) sits well below SigLIP's 64.0%, the same English-only-text-tower
  gap seen in the 768-dim CLIP candidates -- consistent with, not an
  independent confirmation of, that earlier finding.
- Same cross-session latency caveat as before: the SigLIP reference row
  is from `quantization_check_results.json`, a different session than
  this run. Directional, not a precise multiplier.

**What this doesn't test:** whether the accuracy gap narrows or widens at
dimensions below 512, and whether SigLIP has a comparably small/fast
checkpoint of its own that would keep the comparison apples-to-apples on
speed as well as family -- neither was in scope for the two questions
this and the 768-dim comparison set out to answer (family, then
dimension), but would be the natural next question if 100k-image
throughput becomes a hard requirement rather than a target.

## `clip_pt_translation_bakeoff.py` -- does translating PT queries to English before encoding help?

Both CLIP comparisons above found every English-only CLIP candidate
trailing SigLIP badly on Portuguese (32-44% strict-PT vs SigLIP's 64%) --
expected, since none of those text towers were trained on Portuguese. A
phrasing template can't fix that (it's a language-coverage gap, not a
domain-phrasing gap), but translating the query to English *before*
encoding plausibly could: treat the fast CLIP models as
"translate-then-embed" rather than natively multilingual. This only adds
a *query-time* cost (translating one short string per search request) --
it does not touch bulk image encoding, which is where the CLIP
candidates' real speed advantage over SigLIP lives (see the small-dim
section above). Re-tests all five previously-run CLIP candidates against
three text variants per query: the original English, the native
Portuguese, and the Portuguese machine-translated back to English.

Translation model: `Helsinki-NLP/opus-mt-pt-en` (the direct PT-EN pair)
no longer resolves on the Hub -- verified, not assumed, before writing
this script. `Helsinki-NLP/opus-mt-ROMANCE-en` (multi-source
fr/es/it/pt/... -> en, tagged with the `>>por<<` prefix its multi-source
models use to disambiguate) does, and produced fluent, accurate
translations on manual inspection of the sample output.

### Results (`clip_pt_translation_bakeoff_results.json`)

| Model | Dim | Strict-EN | Strict-PT (native) | Strict-PT (via MT) | Pairwise-EN | Pairwise-PT(MT) |
|---|---|---|---|---|---|---|
| `openai/clip-vit-large-patch14` | 768 | 56.0% | 44.0% | **64.0%** | 80.4% | 81.5% |
| `laion/CLIP-ViT-L-14-laion2B-s32B-b82K` | 768 | 52.0% | 32.0% | 60.0% | 78.3% | 74.6% |
| `openai/clip-vit-base-patch32` | 512 | 44.0% | 44.0% | 52.0% | 75.1% | 72.5% |
| `openai/clip-vit-base-patch16` | 512 | 56.0% | 36.0% | 52.0% | 78.8% | 78.8% |
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | 512 | 56.0% | 44.0% | 52.0% | 78.8% | 72.5% |

**Finding: translation helps every candidate, but doesn't fully close the
gap for the models that are actually fast.** All five improve on their
native-PT strict accuracy, in some cases by a lot (`laion` ViT-L/14 goes
from 32.0% to 60.0%, nearly doubling). The standout: `openai/clip-vit-
large-patch14` translated (64.0%) not only beats its own native-English
score (56.0%) but lands on *exactly* SigLIP `base`'s native-PT strict
accuracy (64.0%) -- with the obvious small-sample caveat (25 queries,
9pp = roughly 2 queries flipping) applying with extra force to a
"beats its own English score" result this specific. But that candidate
is one of the two 768-dim, ~7.5s/image models already shown to lose to
SigLIP on speed -- translation buys accuracy there, not a speed win.

For the three fast 512-dim candidates -- the ones actually worth
`clip_small_dim_bakeoff.py`'s speed tradeoff -- translation lifts strict-
PT from 36.0-44.0% up to a flat **52.0%** across all three, a real and
consistent gain, but still 12 points short of SigLIP's native 64.0%.
Translating the query doesn't fully substitute for the model having seen
Portuguese during training, at least not with this translation model.

**The catch this doesn't solve on its own:** translation is a
*query-time* cost, invisible to bulk image indexing (the axis where the
fast CLIP candidates actually win), but it lands on every Portuguese
search request. Measured here at ~0.8-1.4s per query (`opus-mt-ROMANCE-
en`, one string at a time, matching how a live search request would
actually call it -- not a batch best case). ARCHITECTURE.md's own
performance target is **search latency < 1 second**; a ~0.8-1.4s
translation step alone is already close to or over that budget before
the CLIP text encode (~0.1-0.3s) even runs. This doesn't rule out
translate-then-embed, but it means the tradeoff isn't just
"accuracy vs. corpus-indexing speed" -- it's also "accuracy vs.
per-query latency," a different budget with a documented target already
at risk. Also structurally required by ARCHITECTURE.md §11/ADR-007:
images and queries must be embedded by the same model family for the
vectors to be comparable, so this only works as "fast CLIP indexes the
corpus, translated queries hit that same CLIP text tower" -- not as a
mix of SigLIP-for-queries-only bolted onto CLIP-for-images.

**What this doesn't test:** translation quality from a larger/different
MT model (only one was tried, chosen because it was verified to load,
not benchmarked against alternatives), and whether the <1s latency
concern is actually disqualifying or just tight -- both open questions
if translate-then-embed becomes a real candidate rather than an
exploratory data point.

## `clip_template_bakeoff.py` -- does a domain template help the fast CLIP candidates?

`siglip_template_check.py` found a domain-specific template
("an aerial photo of {query}") lifted SigLIP so400m from 33.3% to 55.6%
strict-EN, but left SigLIP base flat -- not a universal trick, and never
tested on any CLIP candidate. Since it costs nothing (no retraining, just
a different string before encoding), it's worth checking rather than
assuming either way. Tests the same three template variants
(`siglip_template_check.py`'s own, for comparability) on the three
512-dim candidates `clip_small_dim_bakeoff.py` found fast enough to be
worth considering, against both English and the Portuguese-via-machine-
translation queries `clip_pt_translation_bakeoff.py` validated. Native
Portuguese is skipped -- wrapping non-English text in an English template
phrase would just produce a code-mixed string, not a meaningful test.

### Results (`clip_template_bakeoff_results.json`)

| Model | Variant | Strict-EN | Strict-PT(MT) | Pairwise-EN | Pairwise-PT(MT) |
|---|---|---|---|---|---|
| `openai/clip-vit-base-patch32` | raw | 44.0% | 52.0% | 75.1% | 72.5% |
| `openai/clip-vit-base-patch32` | a photo of | 44.0% | 52.0% | 74.6% | 74.1% |
| `openai/clip-vit-base-patch32` | an aerial photo of | 44.0% | 40.0% | 70.4% | 65.6% |
| `openai/clip-vit-base-patch16` | raw | **56.0%** | **52.0%** | **78.8%** | **78.8%** |
| `openai/clip-vit-base-patch16` | a photo of | 52.0% | 44.0% | 78.3% | 76.7% |
| `openai/clip-vit-base-patch16` | an aerial photo of | 52.0% | 40.0% | 72.5% | 69.8% |
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | raw | 56.0% | 52.0% | 78.8% | 72.5% |
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | **a photo of** | **60.0%** | **56.0%** | **80.4%** | **75.1%** |
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | an aerial photo of | 52.0% | 44.0% | 73.5% | 69.3% |

**Finding: the template that helped SigLIP hurts every CLIP candidate
tested here.** "an aerial photo of {query}" -- the exact template that
lifted so400m by 22 points -- makes all three CLIP candidates worse,
sometimes substantially (patch32: -12pp strict-PT(MT); patch16: -16pp
strict-PT(MT); laion: -8pp strict-PT(MT)). A complete reversal from the
SigLIP result, and a clean demonstration that this lever is not
transferable between model families -- SigLIP and CLIP were trained on
different data with different loss functions, so they learned different
phrase distributions. Don't assume a template finding from one family
carries to another; re-test it.

The plain, generic "a photo of {query}" template tells a different story
per model: flat-to-slightly-negative for both `openai` checkpoints, but a
real, consistent gain for `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` --
strict-EN 56.0% -> 60.0%, strict-PT(MT) 52.0% -> 56.0%, both pairwise
numbers up too. That's the checkpoint `clip_small_dim_bakeoff.py` flagged
as ~10x faster than SigLIP `base` on this hardware. With this template,
its strict-EN now exactly matches SigLIP `base`'s 60.0%, and its
Portuguese-via-translation gap narrows from -12pp to -8pp relative to
SigLIP's native 64.0% -- for free, since only the query text changes, not
image encoding speed.

**Caveat, same as everywhere in this directory:** still the 45-image,
25-query demo corpus -- each point is worth roughly one query flipping.
Worth re-confirming this specific combination (`laion` ViT-B/32 + "a
photo of") before treating it as settled, especially since the effect
was clearly not consistent across the other two candidates tested
alongside it.

## Reproducing on different hardware

`siglip_bakeoff.py` is written to run unmodified wherever it's placed
inside a checkout of this repo. See the setup instructions in its own
docstring. A second results file was expected from a friend's NVIDIA GPU
machine as an additional hardware data point; add it here as
`results_<description>.json` if/when it lands, following the same naming
pattern as `results_cpu_laptop.json`.
