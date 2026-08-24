# RFC-024 — Pipeline de Embeddings

**Status:** Implementado
**Depende de:** RFC-020 (metadados incrementais), RFC-021 (worker de indexação), RFC-022 (dataset de demonstração), RFC-023 (adaptador CLIP)
**Migration:** `26058b9e1d9a_add_image_content_hash`
**Benchmark:** `scripts/benchmark_indexing.py`

---

## 1. Contexto

O RFC-021 deu ao SolidVision um worker de indexação. O RFC-023 lhe deu um modelo de embedding real. Entre os dois, o pipeline funcionava — e funcionava uma imagem por vez:

```
descobrir -> construir entidade -> SELECT metadados -> codificar -> SELECT linha -> INSERT -> COMMIT
```

para cada arquivo, em sequência, com um `try/except` em torno de cada iteração.

Essa forma é correta e não escala. `ARCHITECTURE.md` §22 define uma meta de mais de 100.000 imagens indexadas, e o RFC-023 §14 mediu o custo sequencial em 0,464 s/imagem — 12,90 horas para essa coleção, antes de contar qualquer trabalho de banco por arquivo.

Três coisas no pipeline existente faziam trabalho evitável:

- **Todo mtime alterado forçava um reembedding completo.** `ARCHITECTURE.md` §16 especifica uma checagem incremental de três passos, em ordem crescente de custo. O RFC-020 construiu os passos 1 e 2 (existência, depois tamanho/mtime) e parou aí, então uma cópia, uma restauração de backup ou um `git checkout` reembutia uma coleção inteira cujos bytes não haviam mudado.
- **O modelo era chamado uma vez por imagem.** Se isso importava era algo não medido no checkpoint que de fato foi para produção.
- **O banco era consultado uma vez por imagem, duas vezes.** Em uma reindexação em que a maioria dos arquivos está inalterada — o caso comum em escala — isso é um round trip por arquivo descoberto antes de a inferência sequer rodar.

Também não havia como executar o pipeline. `AI_Context.md` prometia `python -m infrastructure.workers.indexing_worker`; nenhum entrypoint desse tipo existia, e a validação do RFC-023 precisou de um script descartável escrito à mão.

## 2. Problema

Transformar o indexador em um pipeline que seja **incremental, com batching onde a medição justificar, observável e resiliente** — sem perder o isolamento de erro por imagem, e sem vazar preocupações de modelo ou de batching para o Domain ou a Application.

A tensão que dá forma ao RFC inteiro: **batching e isolamento de erro puxam em direções opostas.** Um lote de 8 contendo um JPEG truncado falha como unidade. A implementação ingênua perde 7 imagens boas e não consegue dizer qual arquivo foi o culpado. Qualquer design de batching que não responda a isso não é aceitável, por mais rápido que seja.

---

## 3. Benchmark

`scripts/benchmark_indexing.py` (novo; `ARCHITECTURE.md` §22 reserva `scripts/` exatamente para isso) mede o `ClipEmbeddingModel` real contra o corpus de demonstração real do RFC-022. Todos os números abaixo vêm deste repositório, nesta máquina, em CPU. Nada é herdado.

**Máquina:** Windows 10, inferência apenas em CPU, PostgreSQL 17 + pgvector em Docker no mesmo host.
**Corpus:** 45 fotos de demonstração commitadas, ~1024×768.
**Checkpoint:** `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`.

### 3.1 Varredura de tamanho de lote

| tamanho do lote | total s | s/imagem | img/s | ganho vs. 1 | pico RSS MB | crescimento RSS MB | h est. para 100k |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 21,53 | 0,4785 | 2,09 | 1,00× | 732,2 | 14,4 | 13,29 |
| 2 | 18,15 | 0,4033 | 2,48 | 1,19× | 733,8 | 12,4 | 11,20 |
| 4 | 17,63 | 0,3918 | 2,55 | 1,22× | 737,2 | 14,5 | 10,88 |
| **8** | **16,51** | **0,3670** | **2,73** | **1,30×** | **747,0** | **22,7** | **10,19** |
| 16 | 15,59 | 0,3463 | 2,89 | 1,38× | 770,7 | 41,5 | 9,62 |
| 32 | 15,63 | 0,3473 | 2,88 | 1,38× | 817,0 | 76,3 | 9,65 |
| 64 | 15,51 | 0,3447 | 2,90 | 1,39× | 845,2 | 94,1 | 9,57 |

A última coluna é apenas inferência, e é a coluna que responde à pergunta que `ARCHITECTURE.md` §22 de fato faz. O número do lote de 1 (13,29 h) corrobora as 12,90 h medidas independentemente no RFC-023 §14.

Foram feitas três execuções. O lote 8 mediu 1,30×, 1,29× e 1,31×; o lote 32 mediu 1,38×, 1,40× e 1,46×. A dispersão entre execuções é de alguns por cento, então diferenças abaixo de ~5% nesta tabela não devem ser lidas como reais.

### 3.2 Memória — o eixo que antes não era medido

A coluna "crescimento RSS" é o pico de memória residente durante a varredura menos o RSS estabilizado antes dela, amostrado em uma thread de fundo a cada 20 ms (verificar entre lotes perderia o pico inteiramente, já que a alocação interessante existe apenas durante um forward pass).

O crescimento é **linear na contagem do lote e independente da resolução da fonte**, que é a estratégia de memória de §9 funcionando como projetada. A partir do lote 8, roda em aproximadamente 1,2–1,5 MB por imagem em voo. Isso é o tensor de entrada pré-processado (224×224×3 float32 ≈ 602 KB) mais ativações transitórias — *não* a foto decodificada.

Este é o número que importa para o risco que §9 levanta. Um lote de 32 fotos de 24 megapixels **não** custa 2,3 GB, porque apenas uma decodificação em tamanho real está viva por vez: o lote contém 32 tensores de tamanho fixo (~19 MB) mais uma decodificação de 72 MB em andamento. O número de 2,3 GB descreve a implementação que este RFC deliberadamente não escreveu.

O pico absoluto de RSS é dominado pelo checkpoint carregado (~730 MB de linha de base), não pelo batching.

### 3.3 Equivalência dos embeddings

O batching deve mudar *como* o trabalho é agendado, nunca *o que* é computado.

| tamanho do lote | cosseno médio vs. lote de 1 | cosseno mín. | delta máx. de componente |
| --- | --- | --- | --- |
| 2 | 1,000000 | 1,000000 | 0,0 |
| 4 | 1,000000 | 1,000000 | 1,19e-07 |
| 8 | 1,000000 | 1,000000 | 1,19e-07 |
| 16 | 1,000000 | 1,000000 | 1,19e-07 |
| 32 | 1,000000 | 1,000000 | 1,19e-07 |
| 64 | 1,000000 | 1,000000 | 1,19e-07 |

1,19e-07 é um ULP de float32 em magnitude unitária — a aritmética é a mesma, reassociada. A similaridade de cosseno, que é sobre o que o índice HNSW ranqueia, é inalterada até a sexta casa decimal. O batching não pode reordenar um resultado de busca.

### 3.4 Resultados dos critérios de corte

| critério | limiar | medido | resultado |
| --- | --- | --- | --- |
| Batching de inferência (§5) | ≥1,3× | 1,30× no lote 8; 1,38× no lote 32 | **aprovado — implementado** |
| Prefetch de metadados em lote (§7.1) | nenhum; obrigatório | — | **implementado** |
| Escritas upsert em lote (§7.2) | banco >~5% do tempo de execução | **9,2%** | **aprovado — implementado** |

### 3.5 O tamanho de lote padrão — onde este RFC discorda do seu próprio briefing

O briefing do RFC-024 determinava um padrão de 8, raciocinando a partir de uma medição herdada em que a curva "estabiliza forte depois de 8", com o lote 8 capturando 87% de todo o ganho disponível e o lote 32 comprando "quase nada" a 4× a memória de imagem decodificada.

**Nenhuma das duas metades dessa premissa se reproduziu aqui, e ambas são registradas em vez de silenciosamente adotadas:**

- O lote 8 captura **77%** do ganho disponível até 64 ((1,30−1)/(1,39−1)), não 87%. O lote 32 é mensuravelmente mais rápido que o lote 8 — 0,3473 vs. 0,3670 s/imagem, cerca de 5%, o que são 0,5 h a menos em uma execução de 100.000 imagens. É uma diferença real, não ruído, ainda que modesta.
- O lote 32 **não** quadruplica a memória de imagem decodificada em relação ao lote 8. §3.2 mediu +54 MB, e essa memória são tensores pré-processados, cujo tamanho não depende em nada da foto de origem. A preocupação levantada pelo briefing é precisamente aquela que a estratégia de decodificação de §9 remove.

**O padrão entregue é, ainda assim, 8.** Com a curva tão plana entre 8 e 64, o argumento remanescente não é velocidade, mas folga: 8 é a configuração que se comporta previsivelmente na menor máquina em que alguém possa rodar isso, e `BATCH_SIZE` existe para que uma máquina com memória sobrando possa ser instruída a usar mais. Um leitor deste RFC que queira os últimos 5% deve definir `BATCH_SIZE=32` e pode fazê-lo sabendo o que isso custa (+54 MB), que é a informação que a tabela herdada não podia fornecer.

---

## 4. Hash de conteúdo SHA-256

### 4.1 O passo que faltava

`ARCHITECTURE.md` §16 especifica uma checagem em ordem crescente de custo. O RFC-024 a completa:

1. Existe uma linha para este arquivo?
2. `file_size` ou `file_modified_at` mudaram?
3. **Somente se o passo 2 disser "possivelmente": fazer hash dos bytes e comparar.**

Implementado como `ContentHasherPort` (Domain) com `Sha256ContentHasher` (Infrastructure). Fazer hash é I/O de sistema de arquivos, então não pode viver na Application; a porta espelha `EmbeddingModelPort` exatamente, e a camada Application depende apenas do contrato. O hasher lê o arquivo em blocos de 1 MiB e nunca chama `read()` sem limite — `Path.read_bytes()` produziria um digest idêntico e alocaria o arquivo inteiro, imediatamente antes da inferência em lote, que já é o estágio faminto por memória.

A decisão em si vive em `plan_indexing()`, uma função pura de (candidato, metadados armazenados, hasher). Ela retorna uma de três ações:

| ação | quando | o que acontece |
| --- | --- | --- |
| `SKIP_UNCHANGED` | tamanho e mtime batem | nada; o arquivo nunca é aberto |
| `REFRESH_METADATA` | metadados mudaram, o hash bate | atualiza os metadados, **mantém o embedding** |
| `EMBED` | arquivo novo, ou hash diferente | codifica e escreve uma linha completa |

**Um arquivo novo tem seu hash calculado mesmo que o passo 1 já o tenha condenado ao embedding.** Isso não é uma violação de "nunca faça hash antes das checagens de timestamp e tamanho" — essas checagens já rodaram e já retornaram "processe isto". É o que torna o passo 3 possível na *próxima* execução: uma linha persistida sem hash é lida de volta como `NULL`, e pulá-lo aqui deixaria o mecanismo inteiro permanentemente dormente para todo arquivo que o sistema indexar.

Um hash armazenado como `NULL` nunca bate com um calculado. Linhas escritas antes deste RFC carregam `NULL` e deliberadamente não receberam backfill (§14), então "desconhecido" custa um reembedding em vez de arriscar um que fosse pulado por engano.

### 4.2 A restrição de identidade

O RFC-022 §7.1 torna `ImageId` derivado do **caminho** de propósito, para que gêmeos byte a byte idênticos produzam duas linhas. `content_hash` é um campo de detecção de mudança e nada mais. Não é único, não é indexado, e nunca é usado para busca, deduplicação ou identidade.

Três coisas impõem isso em vez de meramente documentá-lo:

- `test_the_hash_does_not_participate_in_identity` escreve duas imagens com o mesmo digest e garante que ambas as linhas existem.
- `test_identical_content_at_two_paths_produces_two_rows` (RFC-022, inalterado) continua passando.
- `test_rfc_024_migration_leaves_content_hash_unindexed_and_not_unique` falha se uma migration posterior adicionar uma constraint única ou um índice — o primeiro passo no caminho de o hash adquirir semântica de identidade.

### 4.3 O que isso economiza — medido

Indexar o corpus de demonstração a frio, depois aplicar `utime` em todo arquivo para um novo mtime sem alterar um byte, e então reindexar:

| execução | tempo | indexadas | puladas (inalteradas) | puladas (hash) |
| --- | --- | --- | --- | --- |
| indexação a frio | 23,274 s | 45 | 0 | 0 |
| nova varredura, intocado | 0,024 s | 0 | 45 | 0 |
| **depois de tocar todos os mtimes** | **0,292 s** | **0** | **0** | **45** |
| nova varredura após o toque | 0,019 s | 0 | 45 | 0 |

Antes deste RFC, aquela terceira linha era uma reindexação completa: **~22,0 s de inferência para 45 imagens**, e 12,9 horas para 100.000. O passo de hash a reduz a 0,292 s — uma **economia de 75× naquele caminho** — e a quarta linha mostra que os metadados atualizados foram gravados de volta, então o custo é pago uma vez em vez de a cada execução subsequente.

Esse é um ganho unitário maior que qualquer coisa que o batching ofereça, e vem de *ler* arquivos, não de deixar de lê-los.

---

## 5. O contrato de batching

### 5.1 Opções consideradas

| opção | veredito |
| --- | --- |
| **1. `encode_images()` em `EmbeddingModelPort`, abstrato** | Rejeitada. Toda implementação, incluindo `FakeEmbeddingModel` e todo duplo de teste, teria de crescer código para uma otimização que não tem. |
| **2. Manter a porta de imagem única e fazer batching dentro do adaptador** | Rejeitada. O adaptador não consegue saber quando um lote está completo sem um flush explícito, o que esconde latência e coloca estado de buffer em um componente que é, de resto, sem estado por chamada. |
| **3. `BatchEmbeddingModelPort` opcional e separado** | Rejeitada. Força uma sondagem `isinstance`/`hasattr` na camada Application — um teste de tipo em tempo de execução fazendo o papel de um contrato, mais um segundo caminho de código a manter correto. |
| **4. `encode_images()` na porta, com um default concreto** | **Escolhida.** |

### 5.2 A decisão

```python
def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
    return [self.encode_image(image) for image in images]
```

Concreto, não abstrato. Uma implementação com um caminho mais rápido o sobrescreve; uma sem ele herda um laço correto e não escreve nada. Isso dá a propriedade "implementações podem ignorar batching" da opção 3 sem sondagem e sem desvio na Application, e a honestidade da opção 1 sobre o que o contrato oferece.

`FakeEmbeddingModel` não foi modificado em nada — herda o default e continua sendo o duplo de teste, como o RFC-023 §18 exige.

A docstring da porta não nomeia tensores, dispositivos ou checkpoints; `test_the_batch_capability_did_not_leak_implementation_vocabulary` impõe isso contra as palavras específicas que uma API de lote convida.

### 5.3 Onde o batching acontece

`IndexOrUpdateImageUseCase.execute(image, ...)` fazia a checagem de skip, o embedding e a persistência para exatamente uma imagem em uma chamada. Essa assinatura não consegue fazer batching; chamá-la N vezes em um laço continua sendo N chamadas sequenciais de imagem única.

**A forma escolhida é decomposição (opção 1 do briefing).** A decisão de skip foi extraída para `plan_indexing()`, uma função pura, e um novo coordenador de Application monta lotes a partir dos sobreviventes dela.

#### Componente → responsabilidade depois deste RFC

| componente | responsabilidade |
| --- | --- |
| `FilesystemImageProvider` | Descoberta. Inalterado. Continua ordenando (§10). |
| `IndexingWorker` | Orquestração e logging. Transmite arquivos descobertos como `IndexCandidate`s, chama o coordenador uma vez, loga o resumo e cada falha. Também hospeda o composition root do CLI. |
| `plan_indexing()` *(novo)* | A decisão de skip em três passos, para um candidato. Pura: sem escritas, sem batching, sem logging. |
| `IndexOrUpdateImagesUseCase` *(novo)* | O pipeline. Janelas de prefetch → decisões de skip → lotes de inferência → lotes de persistência. Detém o isolamento de erro e o resumo da execução. |
| `IndexOrUpdateImageUseCase` *(mantido)* | O ponto de entrada de arquivo único. Construído sobre `plan_indexing()`, então nenhuma lógica é duplicada, mas **os erros propagam para o chamador** em vez de serem coletados. |
| `ClipEmbeddingModel` | Ganha `encode_images()`. Continua não engolindo nada. |
| `PostgresImageRepository` | Ganha `get_index_metadata_many()`, `save_indexed_many()`, `update_index_metadata()`. |

`IndexOrUpdateImageUseCase` foi **mantido, não aposentado**. É o contrato correto para indexar um arquivo conhecido: uma futura rota de reindexação de arquivo único ou um observador de sistema de arquivos quer ser informado de que seu único arquivo falhou, não receber um relatório dizendo que zero de um teve sucesso. Como os dois caminhos chamam `plan_indexing()`, existe exatamente uma implementação da checagem incremental.

`IndexingWorker` continua injetado por dependência e não constrói nada — `test_worker_does_not_construct_infrastructure_dependencies_internally` continua passando sem alteração.

---

## 6. Isolamento de erro sob batching

**O requisito de correção mais importante deste RFC.**

```
lote de N
    |  falha como unidade
    v
tentar os mesmos N individualmente
    |
    v
N-1 têm sucesso; 1 é reportado com seu caminho real e sua exceção real
```

O caso comum não paga nada: os lotes têm sucesso e a retentativa nunca roda. O caso de falha restaura exatamente o isolamento por arquivo que o RFC-021 oferecia.

Quatro propriedades, cada uma testada:

- **A exceção original, nunca um wrapper.** `IndexingFailure` carrega o objeto de exceção em si. Um operador que vê `RuntimeError("indexing failed")` não aprende nada acionável.
- **O caminho do arquivo problemático.** Carregado junto.
- **O lote degradado é registrado, não escondido.** Quando a retentativa então tem sucesso para todas as imagens, a execução de outra forma reportaria nenhuma falha, e o único traço de problema seria o tempo perdido. `BatchFallback` captura a exceção em nível de lote e os caminhos envolvidos; o worker a loga em WARNING.
- **Um lote de um pula a tentativa em lote inteiramente.** Ele não tem overhead por chamada a amortizar, e tentá-lo apenas executaria a mesma chamada falha duas vezes antes de reportar o mesmo erro.

`ClipEmbeddingModel` **não** recebeu um `try/except` amplo. A decisão do RFC-023 §4 de que o adaptador não engole nada se mantém, e este RFC depende dela: o adaptador falhando alto é o que permite ao coordenador descobrir qual arquivo foi o responsável.

**Teste de aceitação.** O corpus de casos difíceis do RFC-022 contém `zero_byte.jpg` e `truncated.jpg` com `expect_with_pixel_decoding=FAILED`. O teste lento `TestHardCasesWithRealPixelDecoding` agora é parametrizado sobre tamanhos de lote 1 e 8, de modo que toda asserção nele é uma comparação sequencial-vs-em-lote contra o checkpoint real. No lote 8, um único lote contém os dois arquivos quebrados, então o caminho de retentativa é genuinamente exercitado em vez de meramente disponível. As duas parametrizações produzem resultados idênticos para todos os 14 casos.

O isolamento também cobre os dois estágios que o batching não criou:

- **Hashing** lê o arquivo, então falha pelas mesmas razões pelas quais a indexação falha (apagado entre a descoberta e o processamento, permissões, mídia ilegível). Protegido por arquivo.
- **Persistência** é protegida por linha através do fallback de escrita em lote (§7.2).

Demonstração ao vivo, a partir do CLI contra um corpus de 4 arquivos contendo um JPEG de zero bytes:

```
Discovered:          4
Indexed:             3
Failed:              1
Inference:
  batches:           1
  fallbacks:         1   (batch failed, retried per image)
```

---

## 7. Persistência

### 7.1 Prefetch de metadados em lote — obrigatório, e uma correção de Big-O

Isto não é uma otimização de throughput e não pode ser confundido com uma. Em uma reindexação em que a maioria dos arquivos está inalterada, o caminho de skip emitia um SELECT de `get_index_metadata()` por arquivo descoberto. Com 100.000 imagens e 80.000 inalteradas, são 80.000 round trips **antes de a inferência rodar**, independentemente de quão rápido o modelo seja.

`get_index_metadata_many(image_ids)` responde por uma janela inteira em uma consulta, e a decisão de skip consulta o resultado em vez do banco.

Ela seleciona as quatro colunas de metadados pelo nome, em vez de hidratar linhas `ImageModel` inteiras. `embedding` é um vetor de 512 floats, então carregar linhas completas arrastaria cerca de dois kilobytes por imagem pela rede para responder a uma pergunta decidida por dois escalares — exatamente no caminho que existe para tornar barata a revarredura de uma coleção grande e inalterada.

Medido: uma revarredura do corpus inalterado de 45 imagens completa em **0,024 s** (0,53 ms/arquivo, incluindo descoberta e construção de entidade).

Ids ausentes são omitidos do resultado em vez de mapeados para `None`, de modo que "sem linha" (arquivo novo) permanece distinguível de "linha com metadados vazios" (escrita por `IndexImageUseCase.save()`), que a decisão de skip precisa tratar como alterado.

### 7.2 Escritas upsert em lote — condicionadas, depois implementadas, e a premissa do critério estava errada

O briefing determinava: implementar o batching de inferência e o prefetch sozinhos, medir, e construir escritas em lote só se o banco ainda fosse mais de ~5% do tempo de execução. **Era 9,2%, então foram construídas.** O que a medição de acompanhamento mostrou é mais interessante que o critério em si.

#### A primeira implementação mal ajudou

Colapsar 45 commits em 6 moveu a participação do banco de 9,8% para 9,5%. O profiling da execução explicou por quê:

| operação | medido |
| --- | --- |
| um único commit de 45 linhas preparadas | **2,5 ms** |
| todo o resto, por linha | **~48 ms** |

**O commit nunca foi o custo.** O custo era a metade de leitura: `session.get()` por registro emitia um SELECT por linha e — pior — cada um desses SELECTs disparava um autoflush das linhas preparadas até então, então o SQLAlchemy emitia um round trip de INSERT por registro de qualquer forma. "Um commit por lote" havia colapsado a única parte do trabalho que já era gratuita.

Duas hipóteses foram testadas e rejeitadas no caminho:

- **Manutenção do índice HNSW.** 45 inserts brutos de vetores de 512 dimensões em uma tabela *com* o índice HNSW de cosseno levaram 54,5 ms; em uma tabela idêntica *sem* ele, 53,7 ms. Manutenção de índice não é o custo.
- **Contenção de threads do torch após um forward pass.** A persistência mediu 48,2 ms/linha com `FakeEmbeddingModel` e 47,4 ms/linha com `ClipEmbeddingModel`. Nada a ver com torch.

#### A escrita em lote de verdade

`save_indexed_many()` agora carrega toda linha existente do lote em uma única consulta `IN (...)` e prepara o lote inteiro dentro de `no_autoflush`, de modo que um lote chega ao banco como **uma leitura, um flush, um commit**.

A/B no pipeline real, mesmo corpus, transações novas e desfeitas:

| caminho de escrita | persistência | por linha | participação do banco na execução |
| --- | --- | --- | --- |
| por linha (pré-RFC-024) | 2243,9 ms | 49,87 ms | **9,2%** |
| **em lote (RFC-024)** | **411,0 ms** | **9,13 ms** | **2,5%** |

**Persistência 5,5× mais rápida**, e o banco cai de uma fração visível da execução para dentro do ruído.

#### Semântica transacional vs. isolamento de erro

"Uma transação por lote" e "isolar erros por arquivo" genuinamente conflitam: um lote transacional estrito de 8 descarta as 8 linhas quando a linha 5 viola uma constraint, jogando fora embeddings que custaram ~3 s de CPU.

Resolvido do mesmo modo que §6: **tentar a escrita em lote; em caso de falha, degradar para escritas por linha dentro do lote**, de modo que apenas a linha genuinamente ruim seja perdida. O fallback é contado separadamente do fallback de inferência, porque "o modelo engasgou com um arquivo" e "o banco rejeitou uma linha" são problemas diferentes para quem estiver lendo o log.

**Isso descobriu um defeito latente.** `save_indexed()` nunca fazia rollback de um commit falho, então o SQLAlchemy deixava a sessão em estado de pending-rollback e a *próxima* linha falhava com `PendingRollbackError` em vez de ter sucesso — transformando uma linha ruim em uma cascata pelo resto da execução. O bug antecede este RFC (o laço por arquivo do RFC-021 também teria batido nele), mas o fallback por linha entra direto nele, então `save_indexed()` agora faz rollback e relança. `test_a_rejected_row_does_not_poison_the_next_save` o fixa.

### 7.3 Os dois tamanhos de lote são o mesmo? Sim, deliberadamente

O lote de persistência **é** o lote de inferência, e isso é uma decisão, não um acidente:

- As linhas a escrever são exatamente a saída do lote recém-codificado. Retê-las para preencher um lote de persistência maior manteria mais embeddings de 512 floats vivos sem ganho medido.
- Preserva a propriedade de que o trabalho de um lote é durável antes de o próximo começar, que é o que torna barato o reinício após uma queda sob o skip incremental (§13).
- Um botão é mais fácil de raciocinar que dois, e a medição de §7.2 não dá razão para querer o segundo.

Caso uma medição futura favoreça o desacoplamento, `Settings` é onde o segundo botão vai.

---

## 8. O skip incremental precede a montagem do lote

O modelo nunca pode receber uma imagem que já tenha um embedding válido.

```
1000 descobertas
    v
 800 inalteradas -> puladas antes de qualquer lote ser formado
    v
 200 candidatas  -> lotes de N
```

Os lotes são montados **depois** da decisão de skip, apenas com os sobreviventes, e nunca são preenchidos com imagens já indexadas. Imposto por:

- `test_a_second_run_over_an_unchanged_corpus_invokes_the_model_zero_times` — a contagem de invocações do adaptador em uma segunda execução é zero, e `inference_batches` é 0.
- `test_batches_contain_only_the_survivors_of_the_skip_decision` — seis arquivos indexados, dois alterados, tamanho de lote 4: um lote de 2, não um de 4 completado com linhas já indexadas.
- `test_an_unchanged_candidate_is_never_hashed` — o caminho de skip nem sequer abre o arquivo.

---

## 9. Teto de memória

A estratégia de decodificação, imposta em `ClipEmbeddingModel._preprocess()`:

```
abrir -> decodificar -> converter para RGB -> pré-processar para o tamanho de entrada do modelo
       -> descartar a imagem decodificada em tamanho real -> acumular apenas o tensor pequeno
```

O pré-processamento é deliberadamente um laço por imagem em vez de uma única chamada `processor(images=[...])`. A imagem decodificada em tamanho real é uma variável local de `_preprocess()` e nada mais, então se torna inalcançável no momento em que o método retorna. O que se acumula ao longo de um lote são N tensores de tamanho fixo, cujo tamanho não depende em nada da foto de origem.

Os números que tornam isso real:

- o corpus de demonstração a 1024×768 decodifica para ~2,4 MB; um lote de 32 seria ~75 MB
- uma foto de 24 megapixels decodifica para ~72 MB; um lote de 32 **seria ~2,3 GB** se imagens decodificadas fossem acumuladas

**O corpus de demonstração não consegue expor esse bug** — um teste que usa apenas imagens 1024×768 passa de qualquer jeito, enquanto o caminho de produção esgota a memória nas fotos reais de um usuário. Dois testes miram nisso diretamente:

- `test_only_one_decoded_image_is_alive_at_a_time`, parametrizado sobre tamanhos de lote 2/4/8 em imagens sintéticas deliberadamente grandes, usa `weakref.finalize` para contar quantas imagens decodificadas em tamanho real estão vivas simultaneamente e garante que o pico é **1**.
- `test_the_batch_tensor_is_fixed_size_regardless_of_source_resolution` garante que a mesma invariante se mantém em duas resoluções de origem diferentes.

A asserção estrutural foi escolhida em vez de uma asserção sobre RSS dentro da suíte de testes, como o briefing permite, e é a mais forte das duas: RSS é ruidoso e dependente de plataforma, enquanto "nenhuma decodificação em tamanho real sobrevive ao seu próprio passo de pré-processamento" é exatamente a invariante que mantém acessível um lote de fotos grandes. RSS *é* medido, por tamanho de lote, pelo benchmark (§3.2), onde o ruído pode ser tolerado e os números absolutos são o ponto.

---

## 10. Estágios do pipeline

```
DESCOBERTA -> DECISÃO DE SKIP -> PRÉ-PROCESSAMENTO -> INFERÊNCIA EM LOTE -> PERSISTÊNCIA
```

Composto como um fluxo, com **nenhuma fila, nenhuma thread, nada de async, nenhuma maquinaria produtor/consumidor**. Composição de geradores é suficiente e mantém a memória limitada: `IndexingWorker._candidates()` é um gerador, o coordenador o consome preguiçosamente em janelas, e a coleção inteira nunca é materializada. `test_candidates_are_consumed_lazily` garante que o coordenador nunca roda mais de uma janela à frente do que já persistiu.

Dois tamanhos de janela aparecem, e são deliberadamente diferentes (§13): `metadata_prefetch_size` limita uma leitura de escalares; `batch_size` limita pixels decodificados em memória.

**Detalhe estrutural:** `FilesystemImageProvider.discover()` ordena seus resultados. O RFC-024 depende disso e não o altera — determinismo na ordem de descoberta é o que torna o batching reprodutível, e o que tornaria significativo qualquer checkpoint de caminho no futuro.

---

## 11. Métricas e observabilidade

Uma execução retorna um `IndexingSummary` — contadores e tempos como dados, para que um teste, um benchmark ou um futuro agendador possam fazer asserções sobre eles em vez de parsear saída de log. `format_report()` o renderiza:

```
Indexing finished

Discovered:         45
Skipped:             0   (unchanged)
Skipped (hash):      0   (mtime changed, content identical)
Indexed:            45
Failed:              0

Inference:
  images:           45
  batches:           6
  avg batch:     3.342s
  throughput:      2.2 img/s
  fallbacks:         0   (batch failed, retried per image)

Persistence:
  rows:             45
  batches:           6
  avg write:     0.071s
  fallbacks:         0   (bulk failed, degraded to per-row)
  total:          0.42s   (2.1% of elapsed)

Total:
  elapsed:       20.60s
  throughput:      2.2 img/s
```

Duas linhas merecem seu lugar especificamente:

- **`Skipped (hash)`** conta arquivos que teriam sido reembutidos antes deste RFC. É o número que prova que §4 está se pagando.
- **`(2.1% of elapsed)`** é o critério de §7.2, reportado por toda execução em vez de medido uma vez em um benchmark. Se uma mudança futura tornar o banco caro de novo, a próxima pessoa verá isso sem precisar ser avisada para olhar.

O resumo é retornado pela camada Application e logado pela Infrastructure. Isso mantém a fábrica de logging inteiramente fora da Application; a fábrica existente `app.infrastructure.logging` é usada, nenhum segundo mecanismo de logging foi introduzido, e nenhuma dependência de métricas ou telemetria foi adicionada.

---

## 12. Entrypoint CLI do worker

```
python -m app.infrastructure.workers.indexing_worker --root PATH
```

**Divergência de caminho, resolvida.** `AI_Context.md` prometia `infrastructure.workers.indexing_worker`, sem o prefixo `app.`. Essa forma nunca foi executável: o pacote é `app.infrastructure`, e `pyproject.toml` coloca `backend/` no `pythonpath`, não `backend/app/`. A documentação foi corrigida para corresponder ao código, já que mudar o código significaria reestruturar o pacote para satisfazer uma linha de prosa. `AI_Context.md` agora carrega o comando que funciona.

**`--root` é obrigatório e não tem default.** Nem mesmo `settings.indexing_root_path`, seguindo o raciocínio já documentado em `dataset_tools/seed_demo.py`: usar aquilo como padrão iniciaria silenciosamente a indexação da coleção real de fotos de um usuário na primeira vez que ele configurasse `INDEXING_ROOT_PATH` e rodasse o comando por hábito. Apontar o indexador para um diretório é uma decisão que o operador toma explicitamente, todas as vezes.

`main()` é um composition root — o único lugar autorizado a conhecer toda classe concreta ao mesmo tempo — e reutiliza `app.presentation.dependencies.get_embedding_model` em vez de construir um segundo mecanismo de DI, o que `AI_Context.md` proíbe. O tempo de vida da sessão segue o padrão estabelecido em `seed_demo.py` (`SessionLocal()` dentro de um `try/finally`), porque o `get_image_repository()` do contêiner retorna um repositório sem meio de fechar a sessão que abriu.

Os imports do provider dentro de `main()` são locais à função, e isso não é um truque para escapar de uma checagem de camada. A maior parte da suíte de testes importa `IndexingWorker`; um `from app.presentation.dependencies import ...` em nível de módulo arrastaria Presentation — e, através dela, `ClipEmbeddingModel`, torch e transformers — para cada um desses imports, por causa de um CLI que não está sendo executado.

---

## 13. Configuração

| configuração | antes | depois | por quê |
| --- | --- | --- | --- |
| `batch_size` | 16, **sem uso** | **8**, lido pelo CLI do worker e por `seed_demo` | Agora de fato ligado. Default vindo de §3.5. |
| `metadata_prefetch_size` | — | **512** *(novo)* | §7.1 |
| `worker_count` | 1, sem uso | inalterado, ainda sem uso | Multiprocesso está fora de escopo (§17) |

O `batch_size` existente foi reutilizado em vez de sombreado por um botão novo — adicionar uma segunda configuração sobreposta para o mesmo conceito deixaria um 16 obsoleto ao lado de um 8 medido.

`metadata_prefetch_size` é uma preocupação genuinamente separada, não um botão sobreposto, e `settings.py` diz por quê em detalhe: o prefetch lê quatro colunas escalares e quer uma janela grande; a inferência segura pixels decodificados e quer uma janela pequena. Acoplá-los forçaria um mau compromisso nas duas direções — 8 ids por SELECT é pouco melhor que os round trips por arquivo que §7.1 existe para remover, e 512 imagens decodificadas de uma vez esgotariam a memória em fotos reais.

Ambos são declarados em `.env.example` com o raciocínio anexado. Nenhum tamanho de lote está fixado em código em lugar algum; a camada Application recebe os dois como argumentos obrigatórios de construtor, então um composition root precisa fornecê-los a partir de `Settings` e não pode silenciosamente cair em um literal.

---

## 14. Migration de banco

`26058b9e1d9a_add_image_content_hash`, `down_revision = db526438ced5` (RFC-023, verificado como head antes de escrever).

```python
op.add_column(
    "images",
    sa.Column("content_hash", sa.String(length=SHA256_HEX_LENGTH), nullable=True),
)
```

64 caracteres porque um digest SHA-256 em hexadecimal tem exatamente esse tamanho — o comprimento é esquema físico, não um palpite. Nullable, não indexado, não único (§4.2). `downgrade()` remove apenas a coluna nova.

**Linhas existentes mantêm `content_hash = NULL` e não recebem backfill.** Um backfill teria de ler todo arquivo indexado do disco para poupar um reembedding único que a checagem incremental já absorve. `NULL` é lido como "desconhecido, não posso confirmar que está inalterado" e cai no reembedding.

Verificado contra o banco real após aplicar:

```
                            Table "public.images"
      Column      |           Type           | Collation | Nullable | Default
------------------+--------------------------+-----------+----------+---------
 id               | uuid                     |           | not null |
 path             | character varying        |           | not null |
 filename         | character varying        |           | not null |
 extension        | character varying        |           | not null |
 file_size        | bigint                   |           |          |
 file_modified_at | timestamp with time zone |           |          |
 embedding        | vector(512)              |           |          |
 content_hash     | character varying(64)    |           |          |
Indexes:
    "pk_images" PRIMARY KEY, btree (id)
    "ix_images_embedding_hnsw" hnsw (embedding vector_cosine_ops)
    "uq_images_path" UNIQUE CONSTRAINT, btree (path)
Check constraints:
    "ck_images_file_size_non_negative" CHECK (file_size >= 0)
```

`embedding` continua `vector(512)` e `ix_images_embedding_hnsw` continua usando `vector_cosine_ops`. `test_earlier_migrations_were_not_rewritten` foi estendido para garantir que a migration do RFC-023 não foi editada — ela é a revisão aplicada mais recente e, portanto, o lugar tentador para "só adicionar uma coluna".

---

## 15. Estratégia de testes

**+132 testes** (297 → 429 na suíte rápida; 26 → 48 lentos). A execução padrão de `pytest` continua offline e determinística, verificada com `HF_HUB_OFFLINE=1` e um `HF_HOME` vazio. Todo teste que toca um checkpoint real é marcado como `slow` e desselecionado por padrão.

| área | coberto |
| --- | --- |
| **Hashing** | o digest bate com `hashlib`; as leituras são limitadas (um wrapper de `Path.open` registra todo tamanho requisitado — a única diferença observável entre o laço em blocos e `read_bytes()`, já que ambos produzem um digest correto); arquivo vazio recebe hash em vez de falhar; mudança de um único byte altera o digest; arquivo ausente levanta exceção em vez de retornar um sentinela |
| **Decisão de skip** | metadados inalterados nunca chegam ao hasher; tocado-mas-idêntico pula o embedding *e* grava os metadados atualizados de volta; genuinamente alterado reembute; hash armazenado como `NULL` força reembedding; arquivos novos recebem hash para que a próxima execução possa usar a checagem; gêmeos byte a byte idênticos ainda produzem duas linhas |
| **Batching** | em lote e sequencial produzem embeddings equivalentes (nível de pipeline com fakes, nível de adaptador contra o checkpoint real em lotes 2/4/8); a ordem é preservada; um forward pass por lote; uma implementação sem suporte a lote ainda funciona; uma contagem de retorno errada interrompe a execução |
| **Isolamento de erro** | um lote com um arquivo corrompido indexa os outros N-1; a falha carrega o caminho real e o tipo e mensagem reais da exceção; o lote degradado é registrado; um lote de um não é tentado duas vezes; falhas de hashing e de persistência são isoladas por arquivo; **o corpus inteiro de casos difíceis do RFC-022 produz resultados idênticos em lote 1 e lote 8 contra o checkpoint real** |
| **Persistência** | o upsert em lote escreve toda linha; uma violação de constraint descarta o lote inteiro; a sessão sobrevive a um lote falho; o fallback por linha salva tudo exceto a linha ruim; uma linha rejeitada não envenena o próximo save; o prefetch em lote retorna exatamente o que N chamadas individuais de `get_index_metadata()` retornam |
| **Memória** | o pico de decodificações em tamanho real vivas permanece em 1 conforme o tamanho do lote cresce, em imagens sintéticas deliberadamente grandes |
| **Métricas** | contadores exatos contra um corpus com uma mistura conhecida de arquivos novos / inalterados / idênticos por hash / corrompidos |
| **CLI** | `--root` é obrigatório; nenhum default aponta para `indexing_root_path`; `main()` compõe o pipeline real e fecha sua sessão |
| **Fronteiras** | Domain e Application não importam biblioteca de IA; o método de lote não vazou vocabulário de implementação; a implementação do hasher não é importada acima de Infrastructure; casos de uso não importam `sqlalchemy`/`psycopg`/`pgvector`/`alembic` |

Testes de banco usam a fixture `db_session` isolada por SAVEPOINT e não deixam linhas para trás.

---

## 16. Desempenho contra `ARCHITECTURE.md` §22

| meta | medido | veredito |
| --- | --- | --- |
| Throughput de indexação > 1 imagem/s em CPU | **2,2 img/s** de ponta a ponta no lote 8, hashing e persistência incluídos | ✅ atendida, margem de ~2× |
| Coleção suportada de 100.000+ imagens | **~10,5 h** para uma indexação a frio no lote 8 | ✅ viável |
| Tempo de inicialização < 5 s | modelos ainda carregam de forma preguiçosa | ✅ inalterado |
| Latência de busca < 1 s | não é este RFC (RFC-025) | — |

O número de ~10,5 h é construído a partir de custos por imagem em regime permanente, em vez de extrapolado do tempo de parede de 45 imagens, cujo primeiro lote carrega uma carga única de checkpoint de ~5 s: inferência 0,3670 s + persistência 0,0091 s + descoberta/hashing/contabilidade ~0,0029 s ≈ 0,379 s/imagem.

Contra a linha de base de 12,90 h de imagem única do RFC-023, isso é uma **redução de ~19%** para uma indexação a frio de uma coleção em que nada pode ser pulado — o caso que o pipeline menos consegue melhorar. O grande ganho está em outro lugar: uma revarredura de uma coleção inalterada agora custa **0,53 ms/arquivo** (~53 s para 100.000 imagens), e uma coleção cujos mtimes foram perturbados sem que os bytes mudassem custa 6,5 ms/arquivo em vez de um reembedding completo de 12,9 horas.

---

## 17. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| **`encode_images()` abstrato na porta** | Força toda implementação, incluindo duplos de teste, a escrever código de batching para uma otimização que não tem (§5.1). |
| **Sondagem de capacidade `BatchEmbeddingModelPort`** | Empurra `isinstance` para a Application e dobra os caminhos de código (§5.1). |
| **Batching dentro do adaptador atrás de um flush implícito** | Esconde latência; precisa de estado de buffer em um componente por chamada (§5.1). |
| **Aposentar `IndexOrUpdateImageUseCase`** | O contrato de arquivo único é genuinamente diferente: ele deve levantar exceção, não reportar. Mantido, compartilhando `plan_indexing()` (§5.3). |
| **`batch_size = 32` como padrão** | 5% mais rápido e +54 MB. Rejeitado em favor de folga em máquinas pequenas, com os dados registrados para que possa ser revisto (§3.5). |
| **Tamanho de lote de persistência desacoplado** | Nenhum benefício medido; custa um segundo botão e atrasa a durabilidade (§7.3). |
| **Backfill de `content_hash`** | Leria todo arquivo indexado para poupar um reembedding que a checagem incremental já absorve (§14). |
| **Threads / async / estágios produtor-consumidor** | Explicitamente fora de escopo. A composição de geradores já mantém a memória limitada e os estágios mensuráveis. |
| **`try/except` amplo dentro de `ClipEmbeddingModel`** | Esconderia qual arquivo falhou e por quê; o RFC-023 §4 se mantém (§6). |

### Rejeitados por medição

A coisa mais útil desta seção é o que os números mataram:

- **"Um commit por lote" como design da escrita em lote.** Construído, medido, e constatado que movia a participação do banco de 9,8% para 9,5%, porque o commit custava 2,5 ms e as leituras por linha custavam ~48 ms. Substituído por um design que colapsa as leituras (§7.2).
- **Manutenção do índice HNSW como o suspeito custo por linha.** 54,5 ms com o índice vs. 53,7 ms sem ele, para 45 inserts. Não é o custo.
- **Contenção de threads do torch como o suspeito custo por linha.** 48,2 ms/linha com `FakeEmbeddingModel` vs. 47,4 ms/linha com `ClipEmbeddingModel`. Não é o custo.
- **A curva herdada de "platô em 8".** Não se reproduziu; o lote 8 captura 77% do ganho disponível aqui, não 87% (§3.5).
- **A preocupação de que "o lote de 32 custa 2,3 GB em fotos de 24 MP".** Real para uma implementação que acumula imagens decodificadas; medido em ~1,2–1,5 MB por imagem em voo para a que não acumula (§3.2).

---

## 18. Riscos

| risco | mitigação |
| --- | --- |
| Uma falha de lote degrada silenciosamente o throughput em um corpus cheio de arquivos quebrados | `fallbacks` é contado e logado em WARNING a cada ocorrência |
| `batch_size` elevado imprudentemente em uma máquina com fotos grandes | O crescimento de memória é linear e documentado; `.env.example` diz o que o botão custa |
| Um adaptador que retorna o número errado de embeddings | Levanta exceção imediatamente em vez de degradar, para que a violação de contrato não possa se esconder |
| O fallback por linha mascarando um problema sistemático de banco | Fallbacks de persistência são contados separadamente dos de inferência |
| Hashing de conteúdo em arquivos muito grandes | Lê em blocos de 1 MiB; o hashing só é alcançado para arquivos já destinados a processamento |
| Um RFC futuro tornando `content_hash` único ou indexado | O teste da migration falha nos dois casos |
| Números de benchmark tomados em uma única máquina | O benchmark está commitado e é reprodutível; todo número aqui nomeia suas condições |

---

## 19. Não-objetivos

Confirmados como ausentes da implementação:

- busca por similaridade vetorial, ranking, top-K (RFC-025)
- tabela `Collections`, entidade `Collection`, chaves estrangeiras `collection_id`
- geração de thumbnails, extração de `width`/`height`
- fine-tuning, LoRA, adapters
- quantização, ONNX, backends alternativos de inferência
- workers multiprocesso ou distribuídos — `worker_count` continua sem uso
- backends de GPU além da autodetecção de CUDA do RFC-023
- mudanças em `FakeEmbeddingModel`
- a execução completa do benchmark de 100.000 imagens (RFC-026)

**Sobre paralelismo.** Este RFC puxa uma de duas alavancas sobre o mesmo gargalo. O batching amortiza overhead fixo por chamada dentro de um processo; múltiplos processos worker permitiriam que a inferência limitada por CPU de fato rodasse concorrentemente, o que o batching sozinho não pode oferecer, e é plausivelmente o ganho maior. Também não foi medido, e criar processos ingenuamente em torno de uma carga PyTorch de CPU pode saturar a RAM e disputar os mesmos núcleos. Isso precisa do próprio benchmark e do próprio RFC. Excluí-lo aqui significa "ainda não medido", não "não importa".

---

## 20. Retomabilidade — deliberadamente adiada

`ARCHITECTURE.md` §15 define uma tabela `IndexingJobs` com `last_processed_path` como checkpoint. **Não implementada, e a medição sustenta o adiamento em vez de apenas desculpá-lo.**

O skip incremental do RFC-020 já oferece recuperação de queda: reexecutar após uma interrupção pula tudo que já foi indexado. Com o prefetch em lote de §7.1, essa revarredura custa **0,53 ms/arquivo — cerca de 53 segundos para 100.000 imagens**, contra uma indexação a frio de ~10,5 horas. `last_processed_path` pouparia uma fração desses 53 segundos. É uma *otimização* de reinício, não um requisito de correção.

Independentemente disso, `IndexingJobs` como especificada carrega uma chave estrangeira `collection_id` para uma tabela `Collections` que não existe e está fora de escopo (§19), então implementá-la significaria projetar uma tabela em torno de um relacionamento que este RFC não tem permissão de criar.

---

## 21. Trabalho futuro

- **Indexação multiprocesso.** A alavanca não puxada (§19). Precisa do próprio benchmark antes.
- **Tamanho de lote de persistência desacoplado**, se algum deploy um dia mostrar o banco voltando a importar. A linha do relatório em §11 é como isso seria notado.
- **Reduzir os ~9 ms/linha remanescentes.** A escrita em lote é 5,5× melhor, mas não gratuita; um `INSERT ... ON CONFLICT DO UPDATE` nativo do PostgreSQL pularia a leitura pelo ORM inteiramente.
- **`last_processed_path`**, quando `Collections` existir e se a revarredura de 53 segundos algum dia virar uma reclamação real.
- **Backfill de `content_hash`** para linhas anteriores ao RFC-024, se uma coleção for grande o bastante para que o reembedding único seja pior que uma passada completa de leitura.
- **Tamanho de lote adaptativo** a partir da memória disponível, em vez de um padrão fixo escolhido para a menor máquina plausível.

---

## 22. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/services/content_hasher_port.py` | `ContentHasherPort` |
| `backend/app/application/use_cases/indexing_plan.py` | `IndexCandidate`, `IndexAction`, `IndexPlan`, `plan_indexing()` |
| `backend/app/application/use_cases/index_or_update_images.py` | O coordenador de lotes, `IndexingSummary`, `IndexingFailure`, `BatchFallback` |
| `backend/app/infrastructure/filesystem/sha256_content_hasher.py` | Hasher SHA-256 com streaming |
| `backend/alembic/versions/26058b9e1d9a_add_image_content_hash.py` | A migration |
| `scripts/benchmark_indexing.py` | O benchmark por trás de todo número de §3 |
| `docs/rfcs/rfc-024-pipeline-de-embeddings.md` | Este documento |
| `backend/tests/domain/test_content_hasher_port.py` | Contrato da porta |
| `backend/tests/infrastructure/filesystem/test_sha256_content_hasher.py` | Hasher, incluindo streaming |
| `backend/tests/application/test_index_or_update_images.py` | Coordenador: batching, isolamento, prefetch, métricas |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/domain/services/embedding_model_port.py` | `encode_images()` com um default concreto |
| `backend/app/domain/repositories/image_repository.py` | `save_indexed_many()`, `get_index_metadata_many()`, `update_index_metadata()` |
| `backend/app/domain/value_objects/index_metadata.py` | `content_hash` |
| `backend/app/domain/value_objects/indexing_record.py` | `content_hash` |
| `backend/app/application/use_cases/index_or_update_image.py` | Reconstruído sobre `plan_indexing()`; recebe um `ContentHasherPort` |
| `backend/app/infrastructure/ai/clip_embedding_model.py` | `encode_images()`, `_preprocess()`, `_to_embedding_vectors()` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | Upsert em lote, prefetch em lote, atualização de metadados, rollback em escrita falha |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Novos métodos da porta, escrita em lote atômica |
| `backend/app/infrastructure/database/models/image_model.py` | Coluna `content_hash` |
| `backend/app/infrastructure/workers/indexing_worker.py` | Candidatos em streaming, logging do resumo, entrypoint CLI |
| `backend/app/infrastructure/config/settings.py` | `batch_size` 16 → 8; `metadata_prefetch_size` |
| `backend/dataset_tools/seed_demo.py` | Religado ao coordenador de lotes |
| `.env.example` | `BATCH_SIZE=8`, `METADATA_PREFETCH_SIZE=512` |
| `AI_Context.md` | Caminho do entrypoint do worker corrigido |
| 14 módulos de teste + `tests/application/fakes.py` | Religação mais a cobertura de §15 |

---

## 23. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | **429 passaram**, 48 desselecionados, 1 xfailed |
| `pytest -m slow` | **48 passaram** |
| `pytest` com `HF_HUB_OFFLINE=1` e `HF_HOME` vazio | **429 passaram** — nenhum download |
| `black --check .` | limpo (121 arquivos) |
| `ruff check .` | limpo |
| `mypy` | **5 erros em 4 arquivos** — a linha de base preexistente estabelecida, inalterada |
| `alembic heads` | `26058b9e1d9a (head)` — exatamente um |
| `alembic history` | linear, 5 revisões, nenhuma reescrita |
| Esquema real | `content_hash varchar(64)` nullable; `embedding vector(512)`; HNSW `vector_cosine_ops` intacto |
| Banco de desenvolvimento | **0 linhas** — nada deixado para trás |
| Imports de IA em Domain / Application | nenhum |
| Não-objetivos | sem busca vetorial, sem `Collections`, sem thumbnails, sem fine-tuning, sem quantização |

O CLI foi adicionalmente executado de verdade contra o PostgreSQL em um corpus de quatro arquivos contendo um JPEG de zero bytes; ele indexou 3, reportou 1 falha com seu caminho, registrou 1 fallback de inferência, escreveu `content_hash` em toda linha, e as linhas foram removidas depois.
