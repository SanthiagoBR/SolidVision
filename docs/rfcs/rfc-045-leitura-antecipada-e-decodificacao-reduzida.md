# RFC-045 — Leitura Antecipada e Decodificação Reduzida na Indexação

**Status:** Implementado, como está, com as quatro chaves e o draft. A medição de §13, na máquina dos pilotos, pelo `job_runner` de `build_runner()` contra um PostgreSQL 16 embarcado só dela, deu a primeira indexação do `E:` em 0,269 s/foto contra 0,383 desligada — **8,7 h contra 12,4 h, −29,8%**, abaixo dos 35% do critério: neste dia o disco estava mais rápido e a CPU mais lenta que nos pilotos, e o limite passou a ser a CPU (§13.1). A nova varredura quente de 2 000 fotos ficou ~0,5 s mais lenta (+18 a +30%), acima dos 5% (§13.2). Memória, janela de 32, retrato, 32 contra 64, vetores, demo, recomeço e cancelamento passam; desconectar o disco não foi medido (§13.4). A medição achou um defeito do RFC-029 no CLI, corrigido (§14.9). As seções 1 a 12 são o rascunho como foi aprovado
**Depende de:** RFC-024 (§5, o lote; §8, o skip antes do lote; §9, o teto de memória; §10, que recusou threads, revisto no §6), RFC-029 (§8, o cancelamento cooperativo; §10, o checkpoint), RFC-030 (§7.2, a thumbnail como abertura própria do arquivo, e a alternativa recusada lá), RFC-035 (§7.2, os vetores de referência), RFC-036 (§7.5, os repositórios `deferring`), RFC-039 (§4.1, o `listed()` chamado de dentro da descoberta), RFC-041 (§5.5, o processador `torchvision` pedido pelo nome)
**Bloqueia:** nada
**Migration:** uma, que recomeça o índice do zero: esvazia as imagens e os jobs, e mantém os dispositivos (§5.9)
**Medição:** os pilotos, em `experiments/thumbnails-in-indexing/` (oito scripts, cada um com o seu `.json`); a da implementação, `experiments/rfc-045-leitura-antecipada/measure_read_ahead.py` (+ `.json` e `_restart.json`; os `.log`, fora do git), rodada em 28/10/2026 (§13)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*, e **todos viraram número** em §13, menos o do disco desconectado (§13.4). Os números sem marca foram medidos nos pilotos, com o repositório em memória e sem o PostgreSQL, e dizem de qual experimento vêm. As seções 1 a 12 ficam como o rascunho as deixou: reescrevê-las apagaria o porquê de cada critério; o que a implementação mudou nelas está em §14.

---

## 1. Contexto

O acervo real do usuário está num HD externo USB, o `E:`: 116 396 arquivos suportados em 28/10, ~3,8 MB por foto, a maioria de drone DJI de 12 MP, que o Pillow abre como MPO. A primeira indexação dele pelo pipeline de hoje leva, projetada, **de 17,6 a 18,0 h** (0,546 a 0,559 s por foto, experimentos 4 e 6).

O pipeline é o do RFC-024 §10: um fluxo de geradores numa thread só. Para cada foto nova, nesta ordem e sem sobreposição:

| etapa | onde | o que lê | ms por foto (exp. 1) |
|---|---|---|---|
| descoberta | `FilesystemImageProvider._discover_under()` | o `stat` e o cabeçalho EXIF | 110 |
| hash | `Sha256ContentHasher` | o arquivo inteiro, e descarta | 164 |
| CLIP | `ClipEmbeddingModel._preprocess()` | o arquivo inteiro de novo, decodificado em 12 MP para virar 224 × 224 | 192 |
| thumbnail | `PillowThumbnailGenerator.generate()` | o arquivo inteiro outra vez | 76 |

Sem as thumbnails, **59% do tempo é leitura do disco** (descoberta e hash), e a CPU espera por ela; depois, o disco espera a CPU. O arquivo é aberto quatro vezes, como o RFC-030 §7.2 registrou, e o CLIP e a thumbnail só não pagam o disco de novo se o Windows ainda tiver as páginas em cache.

Este RFC propõe as duas otimizações que os pilotos mediram: **ler cada arquivo uma vez, numa thread à frente do caso de uso, e entregar os bytes ao CLIP e à thumbnail**; e **pedir ao decodificador JPEG uma resolução reduzida para o CLIP**, escolhida pelo tamanho final da foto.

## 2. O que os pilotos mediram

**A máquina:** Windows 11 Home, Intel Core i5-1035G1 (4 núcleos, 8 threads, 15 W, AVX-512), 7,7 GB de RAM, SSD NVMe, o acervo no HD externo; `torch` 2.14 `+cpu` com 4 threads, Pillow 12.3, lote 8. Os scripts montam o `IndexOrUpdateImagesUseCase` de produção, com o repositório em memória e as thumbnails num diretório temporário; nada foi escrito no banco nem nas fotos. As fotos são lidas a frio, em grupos alternados entre as configurações, com semente fixa.

| # | script | pergunta | resultado |
|---|---|---|---|
| 1 | `measure_pipeline_without_thumbnails.py` | quanto custam as thumbnails | 0,466 s/foto sem, 0,557 com (+19,5%); a leitura é 59% do tempo sem elas |
| 2 | `measure_prefetch_overlap.py` | uma thread que faz descoberta, EXIF e hash 64 arquivos à frente | 0,552 → 0,393 s/foto com thumbnails (−29%), mas a inferência subiu de 211 para 294 ms |
| 3 | `diagnose_overlap_slowdown.py` | por que a inferência subiu | com 64 à frente, 15–30% das fotos saíam do cache antes de o CLIP abri-las e eram relidas do HD, ~180 ms cada: com o modelo carregado sobram 0,4–0,7 GB de cache. Com 16 à frente, ~0% |
| 4 | `measure_prefetch_bytes.py` | entregar ao CLIP e à thumbnail os bytes já lidos | 0,546 → **0,304 s/foto** (9,8 h, −44%) com 64 à frente e ~310 MB retidos; 0,311 com 16 à frente e ~106 MB. Vetores idênticos aos de hoje (diferença 0) e thumbnails iguais byte a byte |
| 5 | `measure_clip_draft.py` | `draft()` a 1/2, 1/4 e 1/8 antes do pré-processamento do CLIP | a 1/2, preparo de 163 para 94 ms (1,44x no total), cosseno com o vetor de hoje 0,9992, mínimo 0,979, e o top-1 das 25 consultas igual. 1/4 e 1/8 ganham mais e mudam muito mais |
| 6 | `measure_bytes_draft.py` | as duas juntas, num 2 × 2 | 0,559 → **0,305 s/foto** (9,8 h, −45%). O draft soma só −4% aos bytes: a leitura vira o gargalo |
| 7 | `evaluate_draft_demo.py` | o draft 1/2 piora a busca? No demo, que tem gabarito | Recall@5 de 84 para 80% e par a par de 80,4 para 79,4%, top-1 igual: dentro dos pisos do teste, mas uma perda |
| 8 | `measure_draft_target_size.py` | o draft pelo tamanho final, `draft("RGB", (T, T))` | T = 1000: **o ganho do 1/2 fixo (1,43x) com cosseno 0,9997**, mínimo 0,996; no demo, nenhuma redução e a busca idêntica |

### 2.1 Por que os bytes, e não o cache

O experimento 2 já tinha a leitura numa thread à frente, e perdeu parte do ganho: o CLIP e a thumbnail reabriam o arquivo pelo caminho, e com 64 à frente o Windows tinha despejado as páginas de 15 a 30% deles (experimento 3). Com 16 à frente as faltas sumiram, mas isso é uma margem, e não uma garantia, numa máquina que tem 0,4 a 0,7 GB de cache com o modelo carregado.

Guardar os bytes que a thread já leu para o hash e decodificar deles tira o cache da conta. No experimento 4, com 16 à frente, cache e bytes empataram (0,312 e 0,311 s/foto). Com 64 à frente, os bytes foram a configuração mais rápida (0,304), num tamanho de antecipação em que relendo do cache se perdiam fotos. **E o resultado é o de hoje:** a diferença entre os vetores foi 0, e as thumbnails saíram iguais byte a byte, porque cada consumidor continua decodificando por conta própria; só muda de onde vêm os bytes.

### 2.2 Por que o tamanho final, e não uma escala

O CLIP vê 224 × 224 px, e hoje decodifica as 12 MP inteiras para chegar lá. O decodificador JPEG pode entregar 1/2, 1/4 ou 1/8 da resolução direto da DCT, bem mais rápido, se for pedido antes do `convert()`. O preço é que o vetor muda um pouco, e quanto mais perto de 224 px a imagem reduzida fica, mais muda:

| no `E:`, 299 fotos (exp. 5 e 8) | lado menor depois da redução | ganho total | cosseno com hoje, médio / mínimo | mesmo vizinho mais próximo |
|---|---|---|---|---|
| 1/2 fixo | ~1 125 px | 1,42–1,44x | 0,9992 / 0,979 | 93–94% |
| 1/4 fixo | ~560 px | 1,58x | 0,9916 / 0,897 | 78% |
| 1/8 fixo | ~280 px | 1,78x | 0,983 / 0,872 | 66% |
| **alvo 1000** | ≥ 1 000 px | **1,43x** | **0,9997 / 0,996** | **95%** |
| alvo 500 | ≥ 500 px | 1,55x | 0,9925 / 0,897 | 79% |

O demo do RFC-022 mostra o mesmo padrão pelo outro lado. As imagens dele têm ~1024 × 576; a 1/2 ficam com ~288 px, e os vetores mudam como no 1/8 do `E:` (cosseno 0,982). É lá que a busca piorou um pouco (experimento 7). Na máquina do RFC-036 (Pentium G4560, sem AVX2), um teste do usuário com fotos de DJI de 9 a 12 MP deu, a 1/8, 1,62x, cosseno médio 0,978 e mínimo 0,926, e mudou o top-1 de 2 das 25 consultas: a mesma direção.

`draft("RGB", (1000, 1000))` resolve isso numa linha. O Pillow 12.3 escolhe a maior redução que deixa os dois lados com pelo menos 1 000 px, e nenhuma se o lado menor estiver abaixo de 2 000 px; em PNG e nos outros formatos a chamada não faz nada. Nas fotos de 12 MP isso é o 1/2, com o mesmo ganho. As fotos que ele deixa como estão são as pequenas, cuja decodificação já é barata, e eram elas o pior caso do 1/2 fixo: o cosseno mínimo dele, 0,979, era uma foto de 640 × 480 reduzida a 320 × 240. Nas fotos que não reduz, o vetor sai idêntico ao de hoje (diferença 0, experimento 8), e no demo nenhuma imagem é reduzida: os quatro números do `test_semantic_search_e2e.py` saíram iguais aos de hoje.

### 2.3 Por que as duas juntas rendem menos que a soma

| exp. 6, 8 grupos de 192 fotos frias | s/foto em regime | acervo | vs. hoje | espera do CLIP pela leitura |
|---|---|---|---|---|
| em série (hoje) | 0,559 | 18,0 h | — | — |
| em série + draft | 0,497 | 16,0 h | −11% | — |
| bytes, 64 à frente | 0,319 | 10,3 h | −43% | 3 ms/foto |
| **bytes + draft, 64 à frente** | **0,305** | **9,8 h** | **−45%** | **49 ms/foto** |

Com os bytes, a leitura (descoberta ~115 ms + leitura e hash ~165 ms) e o lado da CPU (CLIP + thumbnail, ~300 ms) correm em paralelo e quase empatam. O draft tira ~65 ms do lado da CPU, e quem limita passa a ser o disco: o CLIP ficou esperando a leitura ~50 ms por foto, e o tempo em regime bateu com o da leitura nas duas rodadas (0,270 s contra 272 ms; 0,341 s contra 326 ms). Em série, onde o CLIP não espera ninguém, o draft rendeu −11% nas duas rodadas.

**O draft entra mesmo assim** (§4): ele custa uma linha, não mexe nos vetores das fotos pequenas nem nos do demo, e devolve CPU, o que conta onde ela é o gargalo, como num disco mais rápido ou numa CPU mais fraca (a do RFC-036). Isso não foi medido. A segunda rodada do experimento 6 saiu com a CPU a ~2,0 GHz em vez de 2,4–2,5, e o efeito do draft sobre os bytes variou de −11% a +2% entre as rodadas: o número estável é a espera.

## 3. O que os pilotos não resolvem

Os pilotos provaram o ganho, mas não são o produto. Cinco coisas os separam dele:

1. **Eles leem tudo.** A thread do piloto calculava o hash de todo arquivo descoberto. O pipeline de hoje só lê o arquivo quando o tamanho ou a data de modificação mudaram (`plan_indexing()`, passo 2): uma nova varredura de um disco já indexado custa um `stat` por arquivo. Copiar o piloto faria cada varredura ler o disco inteiro, ~440 GB no `E:`.
2. **A janela de 512.** O caso de uso puxa 512 candidatos antes de decidir a janela (`metadata_prefetch_size`, RFC-024 §7.1). Com os bytes no candidato, seriam ~1,9 GB parados. Nos pilotos, a janela era do tamanho da antecipação (16 ou 64).
3. **O observador é chamado de dentro da descoberta.** `discovered()` e `listed()` gravam o progresso e o tamanho do disco pela sessão do job (`JobProgressObserver._write()`, RFC-039 §4.1). Com a descoberta numa thread, essas gravações iriam para ela, e a sessão do SQLAlchemy não é segura entre threads.
4. **O ciclo de vida de uma thread:** o cancelamento (RFC-029 §8), o erro no meio da descoberta, o disco desconectado, o Ctrl+C e a parada pelo launcher (RFC-036).
5. **Os vetores mudam.** O draft muda os vetores das fotos grandes, e o banco não registra com que pré-processamento cada vetor foi feito.

## 4. Decisões

1. **Uma thread de leitura por job**, à frente do caso de uso: descoberta, leitura do arquivo inteiro e hash, na ordem da descoberta, com a antecipação limitada em arquivos e em bytes. O caso de uso continua numa thread só e continua sendo quem decide.
2. **Decidir antes de ler.** A thread só lê um arquivo se a decisão incremental vai pedir os bytes dele. Ela consulta um retrato dos sinais de mudança do dispositivo (tamanho e data de cada linha), carregado uma vez, na thread principal, antes de ela começar.
3. **Os bytes viajam no candidato.** `IndexCandidate` ganha um `content` opcional, com os bytes e o hash. `plan_indexing()` usa esse hash; o CLIP e a thumbnail decodificam desses bytes. Sem `content`, tudo funciona como hoje, pelo caminho.
4. **A janela de metadados do job passa a ser a antecipação: 32 arquivos** (`TBM`, §10), e não 512.
5. **Os eventos da descoberta voltam para a thread do caso de uso**, na ordem, pela mesma fila dos candidatos.
6. **O CLIP decodifica com `draft("RGB", (1000, 1000))`**, uma constante do adaptador, como o template do texto, e não uma configuração.
7. **O índice recomeça do zero.** Um vetor feito com outro pré-processamento não se mistura com os antigos (decisão do produto, 28/10). Uma migration esvazia as imagens e os jobs, mantém os dispositivos, e o acervo é indexado de novo.
8. **O lote continua 8.** Com a leitura como gargalo, um lote menor não deixa o disco mais rápido.

## 5. O desenho

### 5.1 A thread de leitura

`ReadAheadCandidates`, em `app/infrastructure/workers/read_ahead.py`: um gerador de candidatos que é também um gerenciador de contexto. Ao entrar, ele inicia uma thread que consome `discovered_candidates()`, a mesma função de hoje, e põe numa fila limitada a `read_ahead_files` itens:

- cada candidato, na ordem da descoberta, com `content` quando a thread leu o arquivo (§5.2);
- os eventos do observador da descoberta (§5.4);
- no fim, um marcador de fim, ou a exceção que encerrou a descoberta.

O lado do consumidor tira itens da fila, repassa os eventos ao observador real, entrega os candidatos ao caso de uso e, ao chegar a uma exceção, a levanta ali. **A ordem é a da descoberta**, e é dela que dependem o checkpoint (RFC-029 §10) e o `DurableFrontier`.

**O que a thread lê.** O arquivo inteiro, de uma vez, e o SHA-256 sobre os bytes lidos, o mesmo valor hexadecimal do `Sha256ContentHasher` (um teste compara os dois). A thread não lê, e manda o candidato sem `content`, quando:

- o retrato diz que o arquivo não mudou (§5.2);
- o arquivo passa de `read_ahead_max_file_bytes` (64 MB): um TIFF de 200 MB vai pelo caminho, em streaming, como hoje;
- ler o arquivo levaria os bytes retidos acima de `read_ahead_bytes` (512 MB, `TBM`). **A thread não espera o orçamento voltar**, porque o consumidor pode estar esperando por ela para fechar uma janela; ela manda o candidato sem `content`, e ele é lido pelo caminho, como hoje;
- a leitura falha. O erro não é registrado aqui: o hash pelo caminho vai falhar do mesmo jeito, no caso de uso, que já isola a falha por arquivo e a reporta com o erro verdadeiro (RFC-024 §6). Um lugar só para as falhas.

**O fim.** Ao sair do bloco `with`, por fim normal, cancelamento ou exceção, o consumidor sinaliza a parada, esvazia a fila para destravar a thread e espera por ela. A thread confere o sinal entre um arquivo e outro. O pior caso é terminar a leitura em curso, de até 64 MB. A thread é `daemon`, para que um processo que morre não fique preso nela. O tempo do cancelamento ao fim é `TBM`.

**Por que uma thread, e não um processo.** A leitura e o `hashlib` soltam o GIL. O que o segura é o EXIF da descoberta, em Python, que é pouco. Um processo copiaria os bytes entre processos e precisaria de outro mecanismo de cancelamento.

### 5.2 Decidir antes de ler

A regra é a do passo 2 de `plan_indexing()`, extraída para uma função pura ao lado dela, `is_unchanged(candidate, file_size, file_modified_at)`, que as duas chamam. Assim, a regra de "não mudou" continua num lugar só.

A thread precisa do tamanho e da data guardados de cada arquivo, e **não consulta o banco**:

- a sessão do repositório de imagens é a do caso de uso, e uma sessão do SQLAlchemy não é segura entre threads;
- uma segunda sessão, numa thread que não é a principal, abriria uma conexão no meio de uma interrupção, que é o que os repositórios `deferring` do RFC-036 §7.5 existem para evitar.

Por isso o job carrega, na thread principal, antes de iniciar a leitura, **o retrato dos sinais de mudança do dispositivo**: um método novo do `ImageRepository`, `list_change_signals(device_id)`, que devolve, para cada linha do dispositivo, o id, o tamanho e a data de modificação. É uma consulta por job. Com 116 396 linhas, o tempo e a memória dela são `TBM`; a estimativa é de alguns segundos e de 20 a 30 MB em Python.

**O retrato é uma previsão, e não a decisão.** O caso de uso continua consultando os metadados por janela e decidindo com `plan_indexing()`, como hoje. Se a previsão errar, o resultado continua certo:

- se a thread não leu um arquivo que a decisão manda calcular o hash, o hash é lido pelo caminho, como hoje;
- se leu um que a decisão pula, perdeu uma leitura, e os bytes são soltos.

Durante um job, a linha de um arquivo só é escrita por esse mesmo job, depois que ele passa pela decisão, e o executor roda um job por vez (RFC-029). Na prática, a previsão é a decisão.

### 5.3 Os bytes no candidato

Em `app/application/use_cases/indexing_plan.py`, ao lado de `IndexCandidate`:

```python
class FileContent:
    """The bytes of one file, read once, and their SHA-256 (RFC-045)."""

    content_hash: str
    data: bytes  # raises once released
    def release(self) -> None: ...
```

`IndexCandidate` ganha `content: FileContent | None = None`. Quem constrói candidatos sem ele (o CLI de hoje, os testes, o `exif_backfill`) não muda.

`plan_indexing()` usa `candidate.content.content_hash` no passo 3 quando há `content`, e o `content_hasher` quando não há. A decisão é a mesma, e o teste que já cobre os três desfechos roda nas duas formas.

**As portas recebem bytes, e não o `FileContent`**, porque o Domain não importa a Application:

- `EmbeddingModelPort.encode_image(image, content: bytes | None = None)`;
- `encode_images(images, contents: Sequence[bytes | None] | None = None)`, cuja implementação padrão continua sendo o laço de `encode_image()`;
- `ThumbnailGeneratorPort.generate(image, max_edge, content: bytes | None = None)` e `ThumbnailWriter.write(image, content=None)`.

O duplo do modelo e os fakes dos testes aceitam o argumento e o ignoram. **Não é a alternativa que o RFC-030 §7.2 recusou**, que era o adaptador de embeddings devolver a imagem decodificada para a thumbnail. Aqui, cada consumidor continua decodificando por conta própria; o que muda é de onde vêm os bytes.

**Um efeito colateral bom:** hoje, o hash e o vetor de um arquivo podem vir de versões diferentes dele, se ele mudar entre as duas leituras. Com os bytes no candidato, o CLIP e a thumbnail decodificam exatamente os bytes que tiveram o hash calculado.

### 5.4 Os eventos da descoberta

`FilesystemImageProvider` recebe, no lugar do observador do job, um observador da `ReadAheadCandidates` que põe `discovered(...)` e `listed(...)` na fila, entre os candidatos, na ordem em que aconteceram. O consumidor os repassa ao observador real quando os tira da fila, na thread do caso de uso, que é a dona da sessão do job.

O `listed()` chega antes do primeiro candidato, como hoje. O `discovered()` que mantém vivo um job de nova varredura (RFC-029 §9.1) continua chegando enquanto a thread examina arquivos. `should_stop()`, `window_decided()` e `batch_persisted()` já eram chamados pelo caso de uso, na thread dele, e não mudam.

### 5.5 Quando os bytes saem da memória

Os bytes de um arquivo vivem enquanto o `FileContent` dele for alcançável: na fila, na janela que o caso de uso está decidindo, ou no lote ainda não gravado. O caso de uso os solta explicitamente (`release()`) assim que o arquivo não precisa mais deles:

- ao decidir `SKIP_UNCHANGED` ou `REFRESH_METADATA`;
- quando o hash falha;
- depois da gravação do lote em que ele entrou, tenha ou não tido sucesso.

A `ReadAheadCandidates` conta os bytes retidos. Eles voltam ao orçamento no `release()`, ou quando o objeto é coletado (`weakref.finalize`), o que vier primeiro. Assim, um caminho que esqueça de soltar não vaza para sempre.

**O teto**, com uma janela de W arquivos, a fila de K e o lote de 8, é de (2W + K + 8) arquivos com bytes: a janela que está sendo puxada mais a anterior, ainda referenciada. Com W = K = 32 e 3,8 MB por foto, são ~400 MB, abaixo do orçamento de 512 MB. Por isso o orçamento quase nunca corta a antecipação, e quando corta, a foto vai pelo caminho. O pico do processo inteiro é `TBM`.

### 5.6 O CLIP

Em `ClipEmbeddingModel._preprocess()`:

```python
DRAFT_TARGET_EDGE = 1000
"""Fixed by RFC-045's measurement, not a tunable: a different value is different vectors."""

source = io.BytesIO(content) if content is not None else image.require_absolute_path().value
with PILImage.open(source) as opened:
    opened.draft("RGB", (DRAFT_TARGET_EDGE, DRAFT_TARGET_EDGE))
    pixels = opened.convert("RGB")
    ...
```

- A chamada vai sem condição em volta: é um no-op nos formatos que não são JPEG, e nas fotos com lado menor abaixo de 2 000 px (§2.2).
- O MPO é um `JpegImageFile`, e o draft vale para o primeiro quadro, que é o que o CLIP lê hoje.
- `POST /images/search/by-image` passa pelo mesmo `_preprocess()`, então a foto de uma consulta é reduzida pela mesma regra que as do índice.
- O `thumbnail()` da miniatura já pede a sua própria redução ao Pillow, e não muda.

### 5.7 A composição e a configuração

`build_runner()` monta a `ReadAheadCandidates` em volta dos candidatos de `JobRunner._index()`, depois de carregar o retrato. O CLI usa o mesmo `build_runner()`, e não muda à parte. O `thumbnail_backfill` e o `exif_backfill` ficam como estão. Quatro chaves novas no `Settings`:

| chave | padrão | o que faz |
|---|---|---|
| `indexing_read_ahead` | `true` | `false` é o pipeline de hoje, inteiro: sem thread, janela de `metadata_prefetch_size`, leitura pelo caminho. A saída de emergência |
| `read_ahead_files` | 32 (`TBM`) | o tamanho da fila e, com a leitura antecipada ligada, a janela de metadados do job |
| `read_ahead_bytes` | 512 MB (`TBM`) | o teto dos bytes retidos |
| `read_ahead_max_file_bytes` | 64 MB | o maior arquivo antecipado |

O draft não tem chave, pela mesma razão que `TEXT_PROMPT_TEMPLATE` e `IMAGE_PROCESSOR_BACKEND` não têm: trocá-lo troca os vetores.

### 5.8 O relatório

O `IndexingSummary` ganha três números, que tornam visível o que os pilotos tiveram de medir por fora:

- `candidate_wait_seconds`: o tempo que o caso de uso passou esperando o próximo candidato. Em série, é o tempo da descoberta; com a leitura antecipada, é a espera pela thread, a "espera do CLIP pela leitura" do §2.3. Se ele for alto, o gargalo é o disco.
- `contents_prefetched`: os arquivos cujos bytes vieram da thread.
- `contents_read_late`: os que precisaram de leitura pelo caminho, porque a previsão errou, o arquivo era grande demais ou o orçamento estava cheio.

`format_report()` ganha as três linhas.

### 5.9 Os vetores que já existem: o índice recomeça do zero

**Trocar o vetor recomeça o banco do zero.** É a regra do produto (28/10), e vale para este RFC: o draft muda o vetor das fotos grandes, e um índice com vetores das duas receitas não é aceito. Como o banco não guarda com que pré-processamento cada vetor foi feito, não há como saber, linha a linha, quais vetores são antigos. Por isso o recomeço é uma migration, e não uma decisão do caso de uso.

A migration:

- **apaga as linhas de `images`**, com os vetores, os hashes, as datas e as posições;
- **apaga `indexing_jobs` e `indexing_job_scopes`**: o histórico de varreduras de um índice que não existe mais;
- **mantém `devices`**, com o rótulo e a identidade do volume de cada disco, que não são vetores. Assim, recomeçar é indexar de novo, e não cadastrar os discos de novo. A porcentagem indexada de cada cartão (RFC-039) volta a 0%;
- **não toca nas miniaturas.** Elas são nomeadas pelo id da imagem, que é o mesmo depois do recomeço (o id vem do dispositivo e do caminho, RFC-027), e são sobrescritas pela reindexação. As de fotos que não existem mais ficam órfãs; limpá-las não é deste RFC;
- **tem um `downgrade()` que não devolve nada.** O RFC-007 o exige, e ele deixa o esquema como estava, mas os dados apagados não voltam.

Há precedente: a migration `db526438ced5`, que mudou o vetor para 512 dimensões, também descartou todos os vetores.

**O preço:** depois de atualizar, a busca, o mapa e a listagem por lugar ficam vazios até que os discos sejam indexados de novo. O `E:` leva `TBM` h com este RFC (o piloto projetou 9,8 h).

O que os pilotos mediram da mistura fica registrado como o número que teria sustentado a outra escolha (§7): nas fotos grandes, o cosseno com o vetor de hoje foi 0,9997 em média, com mínimo de 0,996. Um índice misturado manteve o mesmo vizinho mais próximo em 98,7% das fotos e o mesmo top-5 nas 25 consultas de texto. No demo, os vetores ficam idênticos, então o `test_semantic_search_e2e.py` e os vetores de referência do RFC-035 §7.2 não mudam.

## 6. O que isto revê em RFCs anteriores

- **RFC-024 §10, "nenhuma fila, nenhuma thread":** revisto, só no job. A decisão era certa para um disco onde ler é barato; num HD externo, a leitura é metade do tempo (§1). O caso de uso continua sendo um fluxo numa thread; a thread nova está na fonte dos candidatos, em Infrastructure.
- **RFC-024 §7.1 e §13, a janela de 512:** o job passa a decidir janelas de 32. São ~3 600 consultas de metadados por 116 000 fotos, contra ~230. O custo delas no PostgreSQL 16 embarcado é `TBM`, com o critério do §10. O CLI com a leitura antecipada desligada e os backfills ficam com 512.
- **RFC-024 §9, o teto de memória:** ganha o termo dos bytes retidos (§5.5).
- **RFC-030 §7.2:** a thumbnail continua sendo uma decodificação própria, e a alternativa recusada continua recusada. O que muda é que ela deixa de ser uma quarta abertura do arquivo.
- **RFC-029 §9.1:** o `discovered()` que mantém vivo um job de nova varredura chega pela fila (§5.4). O `job_stale_timeout` não muda.

## 7. Alternativas consideradas

| alternativa | por que não |
|---|---|
| a thread de leitura, relendo do cache (exp. 2 e 3) | o Windows despejou de 15 a 30% das fotos com 64 à frente; com 16, funcionou, mas como margem numa máquina com 0,4–0,7 GB de cache |
| ler todo arquivo descoberto, como os pilotos | uma nova varredura leria o disco inteiro, ~440 GB no `E:`, para pular arquivos que não mudaram |
| a thread consulta o banco por conta própria | uma sessão do SQLAlchemy numa segunda thread, e a interrupção do RFC-036 §7.5 numa conexão que não é a da thread principal. O retrato (§5.2) dá a mesma previsão sem banco na thread |
| o caso de uso decide a janela e só então pede os bytes à thread | separa no tempo as duas leituras do mesmo arquivo, o cabeçalho na descoberta e o resto depois, e num HD isso é uma busca a mais por foto. Não medido |
| antecipar só na primeira indexação de um disco | um disco parcialmente indexado, como um job cancelado e retomado ou uma pasta nova, não ganharia nada. O retrato cobre os dois casos |
| os bytes num depósito por id, como nos pilotos, sem mudar as portas | alguém tem de soltar os bytes, e o último consumidor varia: sem thumbnail, ou com o CLIP falhando, ninguém soltaria. No candidato, o tempo de vida é o do objeto |
| manter os vetores antigos, misturados com os novos | a regra do produto: trocar o vetor recomeça o banco (§5.9). O piloto mediu a mistura (98,7% do mesmo vizinho mais próximo), e é o número que teria sustentado esta opção |
| zerar só os vetores e manter as linhas, para o mapa e a listagem por lugar seguirem funcionando durante a reindexação | não é recomeçar do zero, e deixaria linhas sem vetor em todo caminho de leitura, que hoje não as espera |
| draft 1/2 fixo (exp. 5) | o mesmo ganho do alvo 1000, com cerca de três vezes a mudança nos vetores, e uma piora mensurável no demo (exp. 7) |
| alvo 500, ou 1/4 fixo | 1,55x em vez de 1,43x, mas o vizinho mais próximo muda em 21% das fotos, e o demo não mede o efeito disso, porque não é reduzido |
| lote menor | o lote não deixa o disco mais rápido, e com a leitura como gargalo o lado do CLIP tem ~50 ms de folga por foto. Não medido |
| mais de uma thread de leitura | num HD, leituras concorrentes viram buscas. Pode valer num SSD, e não foi medido |

## 8. Riscos

- **Memória.** A máquina dos pilotos tem 7,7 GB e ~1 GB livre com o modelo carregado. O orçamento (§5.5) e o teto por arquivo limitam os bytes; o pico do processo é `TBM`, com critério no §10.
- **Uma thread que não termina.** O cancelamento, um erro, o disco desconectado, o Ctrl+C no CLI e a parada pelo launcher passam todos pelo fim do bloco `with` (§5.1). Cada um tem um teste e um critério.
- **Uma gravação do observador fora da thread principal.** É o defeito que o §5.4 evita, e que nenhum teste de ponta a ponta pegaria, porque só aparece com corrida. Um teste confere que toda chamada ao observador real acontece na thread que criou a `ReadAheadCandidates`.
- **CPU disputada.** A inferência fica mais lenta com a leitura em paralelo: no exp. 3, o clock caiu 10–15% com a carga extra, e no exp. 6 a segunda rodada desceu a ~2,0 GHz. O ganho líquido medido já inclui isso, mas a variação entre rodadas é grande: a validação alterna as configurações e roda mais de uma rodada.
- **O recomeço apaga o índice** (§5.9). Quem atualizar fica sem busca até reindexar os discos, ~10 h no `E:`. A migration roda sozinha na subida do launcher (RFC-036), e a interface não avisa antes. Um aviso na tela, ou uma confirmação antes de apagar, não é deste RFC; se o produto o quiser, entra antes da migration.
- **Outras máquinas.** Num SSD, a leitura é barata, a thread rende pouco e o draft rende mais; numa CPU sem AVX2, o draft rende mais. `indexing_read_ahead = false` é a saída. A segunda máquina do projeto entra na validação se estiver à mão (§10).

## 9. Testes

Sem o modelo real, salvo onde marcado `slow`:

- **A thread:**
  - mantém a ordem da descoberta;
  - lê só os arquivos que o retrato diz que mudaram, e nenhum quando nada mudou;
  - não lê acima do teto por arquivo, e manda o candidato sem `content` quando o orçamento está cheio, sem travar com uma janela maior que a fila;
  - levanta, na posição certa, a exceção que encerrou a descoberta;
  - termina ao sair do `with` no meio da fila, sem deixar thread viva (`threading.enumerate()`);
  - entrega os eventos do observador na thread do consumidor, na ordem, com o `listed()` antes do primeiro candidato.
- **O hash:** o de `FileContent` é igual ao de `Sha256ContentHasher` para os mesmos arquivos.
- **A decisão:** `plan_indexing()` dá os mesmos três desfechos com e sem `content`. `is_unchanged()` é a regra do passo 2: o teste de mutação que já existe passa a cobri-la.
- **A liberação dos bytes:**
  - o caso de uso solta o `content` nos três caminhos do §5.5;
  - o orçamento volta a zero no fim de um job;
  - um `content` esquecido volta ao orçamento quando é coletado.
- **As portas:** o vetor de `encode_images(images, contents)` é igual ao de `encode_images(images)` (`slow`, com o CLIP real, diferença 0), e a thumbnail dos bytes é igual byte a byte à do caminho.
- **O draft:**
  - um JPEG sintético de 2 400 × 1 600 é reduzido a 1 200 × 800;
  - um de 1 600 × 1 200 não é, e dá o mesmo vetor de antes do RFC;
  - um MPO de duas imagens tem o primeiro quadro reduzido;
  - um PNG não muda.
- **Nada muda para o demo:** o `test_semantic_search_e2e.py` e os vetores de referência do RFC-035 passam sem nenhuma tolerância nova (`slow`).
- **O job:** com a leitura antecipada ligada, o checkpoint, o cancelamento e a falha de um arquivo dão o mesmo resultado que com ela desligada, sobre a mesma árvore de arquivos.
- **A migration:** num banco com imagens, jobs e dispositivos, o `upgrade` deixa zero imagens e zero jobs, e os dispositivos intactos. O `downgrade` volta o esquema, e o `upgrade` seguinte não falha.

## 10. Critérios de pronto

**Onde medir:** na máquina dos pilotos, com o `E:` e o PostgreSQL embarcado, pelo `job_runner` montado por `build_runner()`, com thumbnails. As configurações são alternadas em grupos de fotos frias, com semente nova, como nos pilotos. Na máquina do RFC-036, se houver um HD externo à mão, as duas primeiras linhas se repetem. Os critérios foram escritos antes dos números.

| critério | o número |
|---|---|
| **Primeira indexação**, ligada contra desligada, 4 grupos de 192 fotos cada | `TBM` s/foto em regime, e as horas projetadas para o acervo. **Passa se a ligada for pelo menos 35% mais rápida** (o piloto deu 45%) |
| **Nova varredura** de 2 000 fotos já indexadas, ligada contra desligada | `TBM` s. **Passa se a diferença for de no máximo 5% e o `contents_prefetched` for 0** |
| **Nova varredura com 10% dos arquivos tocados** (a data mudada, o conteúdo igual) | **passa se o `contents_prefetched` for igual ao número de arquivos tocados**, e se todos terminarem como `REFRESH_METADATA` |
| **O pico de memória** do `job_runner` na primeira indexação | `TBM` MB, ligada e desligada. **Passa se a diferença ficar abaixo de `read_ahead_bytes`** |
| **A janela de 32 no PostgreSQL 16 embarcado** | o tempo das consultas de metadados, `TBM`. **Passa se ficar abaixo de 2% do tempo do job** |
| **O retrato** das 116 396 linhas do `E:` | `TBM` s e `TBM` MB. **Passa abaixo de 5 s e de 50 MB** |
| **32 contra 64 à frente** | `TBM` s/foto. **32 fica se estiver a até 3% de 64**; se não, o padrão vira 64, e o orçamento é revisto |
| **Os vetores** de 50 fotos do `E:`, pelos bytes e pelo caminho, com o draft | diferença **0**; o cosseno com os de hoje, `TBM` (o piloto deu 0,9997) |
| **O demo** | `test_semantic_search_e2e.py` com os mesmos quatro números de hoje (64,0 · 84,0 · 12,8 · 80,4), e os vetores de referência exatos |
| **O recomeço**, no banco de desenvolvimento desta máquina | depois da migration, zero imagens e zero jobs, e os dispositivos com os mesmos rótulos. Reindexado o demo, os quatro números da linha acima |
| **Cancelar** no meio de uma primeira indexação | `TBM` s do pedido ao job `cancelled`, ligada e desligada. **Passa se a diferença for de até 2 s, e sem thread viva** |
| **Desconectar o disco** no meio de um job | o job termina com a mesma mensagem de hoje, em `TBM` s, sem travar |
| **A espera pela leitura** (`candidate_wait_seconds`) | `TBM` ms por foto. Sem critério: é o número que diz qual é o próximo gargalo |

## 11. Entregáveis

- `backend/app/application/use_cases/indexing_plan.py`: `FileContent`, `IndexCandidate.content`, `is_unchanged()`, e o hash vindo do `content` em `plan_indexing()`.
- `backend/app/application/use_cases/index_or_update_images.py`:
  - os bytes passados ao modelo e à thumbnail;
  - a liberação do §5.5;
  - os três números do §5.8.
- `backend/app/application/use_cases/thumbnail_writer.py`, e as portas `embedding_model_port.py` e `thumbnail_generator_port.py`: o argumento `content`.
- `backend/app/domain/repositories/image_repository.py`: `list_change_signals()`, com as implementações do PostgreSQL e em memória, e o teste de contrato.
- `backend/app/infrastructure/ai/clip_embedding_model.py`: os bytes e o draft. `fake_embedding_model.py` e `tests/application/fakes.py`: as assinaturas novas.
- `backend/app/infrastructure/filesystem/thumbnail_generator.py`: os bytes.
- `backend/app/infrastructure/workers/read_ahead.py` (novo): `ReadAheadCandidates`.
- `backend/app/infrastructure/workers/job_runner.py`: o retrato e a composição.
- `backend/app/infrastructure/config/settings.py`: as quatro chaves do §5.7.
- `backend/alembic/versions/<revisão>_restart_index_for_rfc_045.py`: a migration do §5.9.
- Os testes do §9.
- `experiments/rfc-045-leitura-antecipada/measure_read_ahead.py`, com o `.json`. Os oito scripts-piloto entram no git com este RFC, sem os `.log`.
- Este documento, com os `TBM` escritos de volta, e a linha dele no [README](README.md).

## 12. Fora deste RFC

- **O EXIF dos bytes já lidos.** Com a leitura como gargalo, a descoberta, que abre o arquivo para ler o EXIF (~115 ms por foto no HD externo), é a próxima alavanca. Na primeira indexação, ler o arquivo inteiro primeiro e tirar o EXIF da memória poderia economizar um acesso por foto. Isso mexe na ordem da descoberta e no `listed()`, e precisa de uma medição antes de um RFC: quanto daqueles 115 ms é disco.
- **O `torch` com CUDA.** A GPU da máquina dos pilotos está parada (o `torch` é `+cpu`). Com o disco como gargalo, a CUDA renderia pouco na primeira indexação deste acervo.
- **A leitura antecipada nos backfills** (`thumbnail_backfill`, `exif_backfill`), que também leem fotos uma a uma.
- **Uma marca da receita do vetor** (modelo e pré-processamento) gravada no banco, para que a próxima troca de vetor recomece o índice sozinha, sem uma migration escrita à mão (§5.9). O RFC-042 §9 já a deixou para quem trocar o modelo.
- **O tamanho do lote sob a leitura antecipada.** Fica 8 (§4); uma medição com 4 e 1 diria se a folga do CLIP permite outra coisa.

## 13. Validação

**Onde e como.** Na máquina dos pilotos (Windows 11, i5-1035G1, 7,7 GB, NVMe), com o `E:` ligado pela USB e na tomada, plano de energia Equilibrado, em 28/10/2026, das 11:54 às 12:16. `experiments/rfc-045-leitura-antecipada/measure_read_ahead.py` (+ `.json`; o `.log`, fora do git), semente 20261029. Cada job é o `JobRunner` de `build_runner()` — a `ReadAheadCandidates`, o caso de uso, os repositórios do PostgreSQL, o CLIP com o draft, as miniaturas —, contra **um PostgreSQL 16 embarcado criado num diretório temporário só para a medição**, e com o engine como o do executor (o pool do SQLAlchemy e o `pool_pre_ping`). A única peça trocada é a varredura: um provider com a assinatura de `FilesystemImageProvider` que devolve uma lista fixa de fotos frias, com o mesmo `stat`, a mesma leitura do EXIF e as mesmas chamadas ao observador, em ordem de caminho — como os pilotos fizeram. Nada foi escrito nas fotos.

Uma primeira rodada, com a semente 20261028, foi parada depois de quatro grupos: o engine dela não tinha pool, e cada transação depois de um commit abria um processo novo do servidor, o que o executor nunca faz. Os números abaixo são todos da segunda.

| critério (§10) | o número | passa? |
|---|---|---|
| **Primeira indexação**, ligada contra desligada, 4 grupos de 192 fotos frias cada, alternados | desligada **0,383 s/foto** (0,374–0,388), **12,4 h** para as 116 396; ligada (32) **0,269 s/foto** (0,261–0,273), **8,7 h**: **−29,8%** | **não**: o critério pedia 35%. A CPU, e não o disco, ficou sendo o limite (§13.1) |
| **Nova varredura** de 2 000 fotos já indexadas | `contents_prefetched` **0** em todas. Com o cache quente: desligada **2,9 s**, ligada **3,4 e 3,8 s** (+18 a +30%, ~0,5 s) | **não**, na diferença: o critério pedia 5% (§13.2). Sim, no `contents_prefetched` |
| **Nova varredura com 10% tocados** | 200 das 2 000 linhas com a data guardada movida (§14.3): `contents_prefetched` **200**, `REFRESH_METADATA` **200**, inalteradas 1 800, nenhuma lida pelo caminho, nenhuma falha; 23,8 s | **sim** |
| **O pico de memória** do `job_runner` | bytes privados: desligada **1 418 MB**, ligada **1 640 MB** (+222 MB); working set 1 102 e 1 116 MB. Os bytes retidos pela leitura antecipada chegaram a **267 MB** | **sim**: a diferença fica abaixo dos 512 MB de `read_ahead_bytes` |
| **A janela de 32 no PostgreSQL 16 embarcado** | **1,6 ms** por consulta de metadados; **0,017%** do tempo do job (6 consultas por 192 fotos) | **sim** (< 2%) |
| **O retrato** das 116 396 linhas | **1,80 s** logo depois de reiniciar o servidor, **1,09 s** quente (mediana de 5); **40,8 MB** retidos em Python, 57,5 MB de pico durante a consulta | **sim** (< 5 s, < 50 MB retidos) |
| **32 contra 64 à frente** | 64: **0,266 s/foto** (0,261–0,274), **8,6 h**; 32 está **1,1%** acima | **sim**: 32 fica |
| **Os vetores** de 50 fotos do `E:`, pelos bytes e pelo caminho, com o draft | diferença **0** (idênticos). O draft reduziu 40 das 50; as outras 10 saíram idênticas às de hoje. Cosseno com os de hoje: **0,9997** em média, mínimo **0,9986** | **sim** |
| **O demo** | **64,0 · 84,0 · 12,8 · 80,4**, os de hoje. Os 45 vetores do demo saem idênticos bit a bit com e sem o draft. Os vetores de referência do RFC-035 diferem de 1,22e-6, com e sem o draft — a mesma diferença de antes, porque foram gravados na outra máquina (§14.7) | **sim** |
| **O recomeço**, no banco de desenvolvimento desta máquina | antes: revisão `6d77379a36a1`, **24 111** imagens, 17 jobs, 13 escopos, 2 dispositivos (`DEMO`, que é o `E:`, e `C:`). Depois da migration, aplicada pelo `prepare()` do próprio cluster em 0,9 s: revisão `c3f81a5e7d24`, **0** imagens, **0** jobs, **0** escopos, os **mesmos dois dispositivos**, com os mesmos ids e rótulos. O demo, reindexado pelo CLI com a leitura antecipada (45 fotos em 7,2 s, as 45 com bytes da thread) e buscado nesse banco: **64,0 · 84,0 · 12,8 · 80,4** | **sim** |
| **Cancelar** no meio de uma primeira indexação | do pedido ao `cancelled`, com o pedido **aos 10 s** (a desligada ainda descobre a primeira janela): desligada 8,4 e 7,8 s, ligada **1,1 e 0,9 s**. **Aos 75 s** (as duas nos lotes): desligada 2,5 e 1,0 s, ligada **1,8 e 1,2 s**. **Nenhuma thread viva** depois de nenhum deles | **sim** (§13.3) |
| **Desconectar o disco** no meio de um job | **não medido**: pede a mão no cabo (§13.4) | — |
| **A espera pela leitura** (`candidate_wait_seconds`) | ligada (32): **21 ms/foto**; (64): 40 ms/foto. Desligada: 59 ms/foto, que é a própria descoberta | sem critério: com a leitura antecipada, o pipeline quase não espera o disco |

O `E:` leva, nestas condições, **8,7 h** com este RFC, contra 12,4 h sem ele; o piloto projetara 9,8 h contra 18,0 h (§5.9).

### 13.1 Por que a primeira indexação ficou em 30%, e não em 45%

O ganho é limitado pelo lado mais lento dos dois que passam a correr em paralelo, e neste dia o mais lento foi a CPU, por duas razões medidas:

- **O disco estava mais rápido que nos pilotos.** A descoberta custou 51–72 ms por foto (59 ms em média nos grupos desligados), contra 108–120 ms nos experimentos 1 e 6; e o resto do tempo desligado que não é CLIP, miniatura nem descoberta — a leitura inteira com o hash e a gravação — ficou em ~68 ms, contra 165 ms só de hash no experimento 6. A desligada, por isso, já começou em 0,383 s/foto, e não em 0,559.
- **A CPU estava mais lenta, e cai mais com a leitura em paralelo.** Desligada, 2,21 GHz em média; ligada, 1,98 GHz (os pilotos: 2,3–2,6 GHz). O lado do consumidor ligado — CLIP 176 ms + miniatura 92 ms, ~268 ms — é o tempo em regime (0,269 s), e a espera pela thread caiu a 21 ms por foto: o pipeline quase nunca espera o disco.

Com esses dois lados, o teto do ganho é 1 − 268/383 ≈ 30%, que é o que se mediu. Os pilotos estavam no outro regime: a leitura (~280 ms) era do tamanho do consumidor (~250–340 ms), e o paralelo rendia 43–45%. A leitura antecipada fez o que prometia — tirou o disco do caminho, e em 32 ou em 64 dá o mesmo —, mas o que sobra é CPU, e é o draft e a CPU que decidem o resto. O critério de 35% foi escrito a partir do regime dos pilotos; não se cumpre neste dia, e fica registrado como não cumprido.

### 13.2 A nova varredura quente: +0,5 s por 2 000 fotos

As duas configurações não podem passar a frio pelas mesmas fotos (§14.4). A passada de aquecimento, a frio, levou 42 s (21 ms por foto, `stat` e cabeçalho do HD); a primeira desligada depois dela, 18,4 s, ainda em parte fria; as três seguintes, já quentes, 3,8 s (ligada), 2,9 s (desligada) e 3,4 s (ligada). A diferença quente, +0,5 a +0,9 s em 2 000 fotos, é o custo fixo da máquina da leitura antecipada quando o disco não custa nada: a fila e a troca de thread por arquivo, e a janela de 32 — 63 consultas de metadados em vez de 4 (0,16–0,17 s contra 0,04–0,05 s). São ~0,3–0,4 ms por arquivo, que contra os 21 ms de uma varredura fria do HD seriam ~2%; isso é uma conta, não uma medição, porque um A/B frio sobre as mesmas fotos não existe. Medido como o critério pedia, com o cache quente, não passa.

### 13.3 O cancelamento

A tolerância de 2 s não chegou a ser usada contra a leitura antecipada: ela foi mais rápida nos dois instantes. Aos 10 s, a diferença é a do §14.5 — a desligada só vê o pedido depois de descobrir a primeira janela inteira (300 fotos, ~18 s), a ligada no fim da primeira janela de 32. Aos 75 s, as duas veem o pedido no fim do lote em curso, e o fim do bloco `with` — esvaziar a fila e esperar a thread terminar a leitura em curso — não apareceu acima do ruído de um lote. O "TBM" do §5.1 é este: **0,9 a 1,8 s** do pedido ao fim, com a thread terminada.

### 13.4 O que não foi medido

- **Desconectar o disco no meio de um job.** Pede alguém para tirar o cabo no meio da medição. O passo `unplug` do script faz isso: começa um job de 2 000 fotos frias, avisa quando tirar o disco e mede do sumiço do volume ao fim do job (`--steps unplug`). O comportamento está nos testes: o erro da descoberta é levantado na posição em que aconteceu (`test_read_ahead.py`), e o job ligado termina como o desligado — `failed`, com a mesma mensagem — com o disco tirado no meio (`test_job_runner_read_ahead.py`).
- **A máquina do RFC-036.** Não estava à mão; as duas primeiras linhas não foram repetidas lá.

## 14. Correções feitas durante a implementação

### 14.1 O exemplo do draft no §9 contradiz a regra do §2.2

O §9 pedia que *"um JPEG sintético de 2 400 × 1 600"* fosse *"reduzido a 1 200 × 800"*. A regra do próprio RFC (§2.2) diz o contrário: o Pillow escolhe a maior redução que deixa os dois lados com pelo menos 1 000 px, e nenhuma se o lado menor estiver abaixo de 2 000 px. Conferido no Pillow 12.3: 2 400 × 1 600 fica inteiro (800 < 1 000); 2 400 × 2 000 vira 1 200 × 1 000; 4 000 × 3 000 vira 2 000 × 1 500; 1 999 × 4 000 fica inteiro. O teste usa 2 400 × 2 000 → 1 200 × 1 000 e 4 000 × 3 000 → 2 000 × 1 500, e põe 2 400 × 1 600 entre os que não são reduzidos, com 1 600 × 1 200 e 640 × 480. A regra fica; o exemplo é que estava errado.

### 14.2 Um Ctrl+C espera a thread por no máximo 2 s

O §5.1 diz que, ao sair do bloco `with`, o consumidor espera a thread, e que *"o pior caso é terminar a leitura em curso, de até 64 MB"*. Há um caso pior, que o rascunho não viu: a thread também faz a listagem do disco — o `sorted(rglob("*"))` de `_discover_under()` —, que não olha sinal nenhum e, no `E:` frio, leva dezenas de segundos (a listagem do script de medição, por `os.walk`, levou 54 s). No pipeline de antes, um Ctrl+C no meio dela interrompia na hora, porque a listagem rodava na thread principal; com a thread, esperá-la faria a parada durar até o fim da listagem, e o launcher (RFC-036 §7.2) mata quem não sai no prazo.

Por isso, só quando o bloco sai por uma interrupção (`KeyboardInterrupt`, que é como o Ctrl+C e a parada do launcher chegam), o consumidor espera a thread por no máximo `INTERRUPTED_JOIN_SECONDS` (2 s) e a deixa ao fim do processo — ela é `daemon`, como o §5.1 previa, e não toca o banco. O fim normal, o cancelamento e as exceções esperam até ela terminar, e os testes conferem que nenhuma fica viva.

### 14.3 "Tocados": a data mudada no banco, e não no disco

O §10 pedia uma nova varredura *"com 10% dos arquivos tocados (a data mudada, o conteúdo igual)"*. Mudar a data de modificação de fotos do `E:` é escrever no acervo, que o sistema nunca faz (RFC-028 §10, RFC-030 §7.1). A medição move para trás, no banco, a data guardada de 10% das linhas. Para a decisão é o mesmo caso — o tamanho igual, a data diferente da guardada, o hash calculado, o conteúdo igual, `REFRESH_METADATA` —; só muda qual dos dois lados mudou.

### 14.4 As varreduras de 2 000 fotos foram comparadas quentes

Ligada e desligada não podem passar as duas a frio pelas mesmas fotos: a primeira esquenta os cabeçalhos para a segunda. A medição faz uma passada de aquecimento e alterna desligada, ligada, desligada, ligada sobre os mesmos arquivos; a primeira desligada ainda saiu em parte fria (§13.2), e a comparação é entre as três seguintes.

### 14.5 O cancelamento depende de onde o pedido cai

O pipeline só olha `should_stop()` entre janelas e depois de cada lote, e a janela desligada é de 512. Um pedido que chega enquanto o job desligado ainda descobre a primeira janela espera a descoberta inteira dela antes de ser visto — 300 fotos frias, ~18 s, na medição; até 512 num job de verdade. A medição pede o cancelamento em dois instantes, durante essa descoberta (10 s) e durante os lotes (75 s), e a leitura antecipada foi mais rápida nos dois (§13.3).

### 14.6 O retrato das 116 396 linhas foi medido num dispositivo sintético

O banco de desenvolvimento tinha 23 625 linhas do `E:`, e não as 116 396; indexar o resto levaria horas. A medição grava, no cluster temporário dela, um dispositivo à parte com 116 396 linhas — os caminhos reais do `E:`, tamanhos e datas sintéticos, vetores aleatórios de 512 dimensões (a tabela ficou com 384 MB) — e mede `list_change_signals()` sobre ele. O índice HNSW desse cluster foi apagado antes de gravar: construí-lo para 116 396 vetores leva minutos, e a consulta do retrato não o lê. A memória retida, 40,8 MB, ficou acima da estimativa do §5.2 (20 a 30 MB) e abaixo do critério.

### 14.7 Os vetores de referência do RFC-035 foram gravados na outra máquina

O §10 pedia *"os vetores de referência exatos"*. O teste do RFC-035 só compara bit a bit na máquina que gravou a referência; nesta, compara com a tolerância de 1e-5, e a diferença já era de 1,22e-6 antes deste RFC. O draft não mexe nisso: com ele e sem ele, a diferença para a referência é a mesma, e os 45 vetores do demo saem idênticos bit a bit nas duas formas. É essa a igualdade exata que esta máquina pode mostrar.

### 14.8 O que o desenho ganhou de nome

- O retrato é um valor do domínio, `ChangeSignals` (o tamanho e a data), num `dict[ImageId, ChangeSignals]`: o `IndexMetadata` carrega mais quatro campos que o retrato não usa, e são 116 396 objetos.
- `FileContent` ganhou `size`, `released` e o `on_release`, uma função que recebe só o tamanho. O `weakref.finalize` do §5.5 mora nele, e não na `ReadAheadCandidates`, porque é ele que dispara a devolução tanto no `release()` quanto na coleta; a Application usa só a biblioteca padrão para isso.
- O SHA-256 dos bytes é `sha256_of()`, ao lado do `Sha256ContentHasher`, para os dois não divergirem.
- O `JobRunner` recebe a leitura antecipada como dois argumentos nomeados no fim da assinatura: `read_ahead` (os três limites, `ReadAheadLimits`) e `images` (o repositório do retrato). `None` é o pipeline de antes, e é o que todo teste anterior usa.
- O caso de uso também solta os bytes da parte de uma janela que uma parada abandona, além dos três caminhos do §5.5.
- O `contents_read_late` conta também a leitura pelo caminho que falhou: ela foi feita, em série com o modelo. O relatório ganhou uma seção *Reading* com os três números.
- Os oito pilotos entram no git como o registro do que mediram, e rodam contra o código de antes deste RFC: os duplos deles sobrescrevem `_preprocess()` e `generate()` com as assinaturas antigas, sem o `content`.

### 14.9 A medição achou um defeito do RFC-029, corrigido

A segunda reindexação do demo pelo CLI, no banco de desenvolvimento, ficou parada: o job foi dado como *"taken over"* no primeiro `record_progress()`, e o CLI passou a segui-lo para sempre, com a linha `running` e ninguém a rodá-la. **Não é da leitura antecipada**: reproduz-se com ela desligada.

As sessões do CLI e do executor são feitas com `expire_on_commit=False`. O CLI cria o job e o reivindica na mesma sessão; o modelo `pending` que o `create()` gravou fica no mapa de identidade dela enquanto estiver vivo, e o ciclo entre o job e os escopos o mantém vivo até o coletor de lixo passar. Sem `populate_existing`, o `session.get()` do `_claim()` e o `get(..., with_for_update=True)` do `record_progress()` devolvem essa cópia em vez da linha: o primeiro `record_progress()` lia `pending` e respondia `None`. Se acontecia ou não dependia de quando o coletor rodava — por isso nenhum teste o pegara. Com o coletor desligado, reproduz-se sempre, ligada e desligada.

A correção está em `PostgresIndexingJobRepository`: toda leitura pede `populate_existing` — as quatro por `get()`, inclusive o compara-e-grava de `save_if_status()`, por onde passam o fim do job e o reaper, e as duas consultas que devolvem modelos. `test_indexing_job_session_staleness.py` desliga o coletor e confere o `claim()`, o primeiro progresso e o fim de um job criado na mesma sessão; sem a correção, os dois primeiros falham.

### 14.10 Fora deste RFC: a lixeira do Windows é indexada

O banco de desenvolvimento tinha 1 268 linhas do `E:` em `$RECYCLE.BIN`, e duas das falhas da medição foram arquivos `$I…` de lá: os índices da lixeira, que o Windows grava com a extensão do arquivo apagado e que não são fotos. A varredura (RFC-029) não exclui as pastas de sistema de um volume. Não foi mexido aqui.
