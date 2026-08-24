# RFC-025 — Busca Semântica

**Status:** Implementado
**Depende de:** RFC-022 (dataset de demonstração), RFC-023 (adaptador CLIP), RFC-024 (pipeline de embeddings)
**Migration:** nenhuma — ver §5
**Medição:** `experiments/rfc-025-semantic-search/measure_retrieval.py`

---

## 1. Contexto

O RFC-023 deu ao SolidVision um modelo de embedding real. O RFC-024 transformou a indexação em um pipeline que preenche a coluna `embedding` incrementalmente, em lotes, a 2,2 imagens/segundo. Depois dos dois, um corpus de demonstração completo de 45 imagens fica no PostgreSQL com vetores CLIP de 512 dimensões normalizados em L2 e um índice HNSW construído para distância de cosseno.

Nada lia nada disso.

```python
def execute(self, query: str) -> list[Image]:
    """Return images from the repository for the supplied query."""
    self._embedding_model.encode_text(query)
    return self._repository.list()
```

Essa era a implementação inteira da busca: codificar a consulta, **descartar o vetor**, retornar todas as linhas em ordem de inserção. Os dois RFCs anteriores nomearam isto como problema do próximo RFC em vez de silenciosamente alargar o próprio escopo — o RFC-023 §17 lista "busca por similaridade vetorial" como não-objetivo e o RFC-023 §12.5 explica por que o teste de qualidade de recuperação permaneceu dormente; o RFC-024 §19 repete. `queries.json` havia sido entregue com 25 consultas de *ground truth* no RFC-022 e nunca havia sido pontuado contra um ranking real.

## 2. Problema

Fazer isso funcionar, de ponta a ponta, e provar com o *ground truth* que já existe:

```
"uma propriedade rural com um lago"
        ↓  langdetect + Marian
"a rural property with a lake"
        ↓  "a photo of {query}" + torre de texto do CLIP
   vetor de 512 dimensões normalizado em L2
        ↓  ImageRepository.search_similar()
   PostgreSQL / pgvector / cosseno
        ↓
   SearchHit[] ranqueados, com pontuações
```

A restrição que dá forma a toda decisão abaixo: **a ordenação pertence ao banco de dados.** Uma implementação correta que puxasse 100.000 vetores para dentro do Python para ordená-los satisfaria todo teste deste RFC e seria inútil na escala que `ARCHITECTURE.md` §22 mira.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Local da ordenação | Dentro do PostgreSQL, ordenado pelo `<=>` do pgvector |
| Pontuação | Similaridade de cosseno em **[-1, 1]**, convertida da distância na Infrastructure |
| Novo tipo de Domain | `SearchHit(image, similarity)` |
| Novo método da porta | `ImageRepository.search_similar(embedding, limit) -> list[SearchHit]` |
| Escopo de uma busca | Global sobre toda imagem conhecida (§8) |
| Tamanho da página | Default injetado de `settings.top_k_results`, teto rígido `MAX_SEARCH_LIMIT = 100` |
| Validação | Consulta em branco e limite fora de faixa levantam erros de domínio |
| Migration | Nenhuma. O esquema do RFC-023 já serve (§5) |
| `settings.minimum_similarity` | **Removido** (§13) |

## 4. Arquitetura

Cada camada detém exatamente a parte da busca que pode possuir sem saber que as outras existem:

| camada | conhece | não conhece |
| --- | --- | --- |
| **Domain** | que uma busca retorna `SearchHit`s ranqueados, que as pontuações são cosseno, que imagens não indexadas nunca aparecem | SQL, pgvector, HNSW, CLIP, tradução |
| **Application** | o que torna uma requisição utilizável — consulta não vazia, `limit` na faixa | como texto vira vetor, como um vetor vira um ranking |
| **Infrastructure (IA)** | langdetect, Marian, o template de prompt, a torre de texto | que uma busca existe |
| **Infrastructure (persistência)** | `<=>`, `vector_cosine_ops`, distância → similaridade, desempates | o que a consulta dizia, ou em que idioma |

O caso de uso tem nove linhas de trabalho real. Esse é o ponto: o pipeline de §2 atravessa quatro componentes especialistas, e o único lugar onde ele é *montado* é uma camada que não entende nenhum deles.

### 4.1 Por que `SearchHit` e não uma pontuação em `Image`

`Image` é congelada, compara por id, e significa a mesma coisa para todo chamador. Uma similaridade não significa nada sem a consulta que a produziu — a mesma imagem pontua de forma diferente para cada busca, e não carregaria pontuação alguma quando meramente listada. Colocar o número na entidade permitiria que dois valores `Image` do mesmo arquivo carregassem dados contraditórios enquanto ainda comparassem como iguais.

`SearchHit` vive no Domain, ao lado de `IndexingRecord` e `IndexMetadata`, pela razão que a docstring de `IndexingRecord` já dá: é parte do contrato da própria porta `ImageRepository`, e uma porta cujo tipo de retorno vive na Application não é autocontida.

### 4.2 O contrato da porta

A docstring de `search_similar()` especifica comportamento sem nomear tecnologia: ordenado do melhor primeiro; imagens sem embedding nunca aparecem; menos que `limit` quando os candidatos acabam e `[]` quando não há nenhum; ordem determinística nos empates; escopo global; e uma incompatibilidade de dimensão é erro, não resultado vazio. Cada cláusula é um teste em `test_search_similar_contract.py`, executado contra as três implementações (§10.2).

## 5. Por que não há migration

O `db526438ced5` do RFC-023 já criou tudo de que a busca precisa:

```python
embedding = Vector(512)                       # nullable
ix_images_embedding_hnsw USING hnsw (embedding vector_cosine_ops)
```

A coluna guarda os vetores, o índice foi construído para o operador que este RFC usa na ordenação, e a nullabilidade é o que permite que uma linha não indexada exista e seja excluída. **Busca é uma leitura.** Ela não armazena nada, não precisa de coluna nova, tabela nova nem índice novo — então não ganha migration, e `alembic heads` continua reportando `26058b9e1d9a` como head único.

O literal `512` em `image_model.py` virou uma constante nomeada, `EMBEDDING_DIMENSION`, porque a busca agora precisa validar vetores de consulta contra o mesmo número e duas cópias de um fato de esquema físico são uma cópia a mais. A definição da coluna e a migration estão inalteradas.

## 6. A consulta

```python
distance = ImageModel.embedding.cosine_distance(list(embedding.values))
select(ImageModel.id, ImageModel.path, ImageModel.filename, ImageModel.extension,
       distance.label("distance"))
    .where(ImageModel.embedding.is_not(None))
    .order_by(distance, ImageModel.id)
    .limit(limit)
```

Quatro detalhes, cada um estrutural:

**`<=>`, e nada mais.** `cosine_distance` emite o operador para o qual o índice foi criado. Ordenar por L2 ou produto interno abandonaria o índice em silêncio *e* mudaria o ranking.

**`WHERE embedding IS NOT NULL`.** Uma linha escrita por `IndexImageUseCase.save()` não tem vetor. Tratar um vetor ausente como zeros, ou anexar linhas não ranqueadas quando as ranqueadas acabam, colocaria arquivos que ninguém indexou à frente de arquivos que alguém indexou.

**O desempate por id.** Sem ele, linhas equidistantes voltam na ordem que o plano produzir — o que muda entre uma varredura sequencial e uma varredura por índice, ou seja, conforme a tabela cresce — e `limit` então cortaria uma delas arbitrariamente. §7.2 mostra que o desempate não custa nada.

**Quatro colunas, não a entidade.** `select(ImageModel)` arrastaria um vetor de 512 floats por acerto para construir uma entidade de domínio que não tem campo de embedding. O mesmo argumento que o RFC-024 §7.1 fez para `get_index_metadata_many()`.

## 7. Aproximado ou exato? Medido, não presumido

HNSW é um índice de vizinhos mais próximos **aproximado** (ANN). Quando o planejador o usa, o top-K que ele retorna não tem garantia de ser o verdadeiro top-K, e este RFC não promete ranking exato como propriedade geral.

Na escala de hoje a questão é irrelevante por outra razão: o planejador não usa o índice de forma alguma — e §7.3 mostra o que acontece quando usa. Medido com `EXPLAIN ANALYZE` sobre vetores unitários sintéticos de 512 dimensões, com a consulta entregue (desempate incluído), desfeita em seguida:

| linhas | plano | execução |
| --- | --- | --- |
| 45 (o corpus de demonstração) | Seq Scan → top-N heapsort | **0,36 ms** |
| 2.000 | Seq Scan → top-N heapsort | **9,09 ms** |
| 10.000 | **Index Scan** `ix_images_embedding_hnsw` → Incremental Sort | **1,90 ms** |

Planos completos: `experiments/rfc-025-semantic-search/planner_explain_output.log`.

### 7.1 O que isso significa para o corpus de demonstração

Todo número de recuperação de §11 foi produzido por uma varredura sequencial, então esses rankings são **exatos** — 45 cálculos completos de cosseno por consulta, sem aproximação nenhuma. A medição é uma medição do *modelo*, não do recall do índice.

O corolário é declarado em vez de escondido: **o RFC-025 não validou o recall do HNSW.** Um "teste de sanidade de índice" com 45 linhas seria teatro — o planejador ignoraria o índice e o teste passaria independentemente de o índice estar correto, vazio, ou construído para o operador errado. Recall em escala precisa de um corpus grande o bastante para o planejador escolher o índice, e pertence ao benchmark de 100k que o RFC-024 §19 já adiou (RFC-026).

### 7.2 O desempate não custa o índice

Este era o único risco real em §6, e foi medido em vez de deduzido: a varredura ordenada por índice do pgvector satisfaz `ORDER BY <=>`, e uma segunda chave de ordenação poderia ter forçado o planejador a abandoná-la e ordenar tudo.

Não força. Com 10.000 linhas, o planejador escolheu `Index Scan` alimentando um **Incremental Sort** — o índice fornece a ordem de distância, e a ordenação só desempata dentro de grupos de distância igual. Determinismo e índice não estão em tensão.

### 7.3 O que muda quando o índice *é* usado — medido por acidente

O experimento do planejador acima deixou o banco de desenvolvimento com um `pg_class.reltuples` inflado (o ANALYZE o atualiza no lugar, então uma transação desfeita não o reverte) e um índice HNSW contendo milhares de entradas mortas ainda não removidas pelo vacuum. O planejador, portanto, escolheu a varredura por índice sobre uma tabela com **3 linhas vivas** — e os testes de contrato, verdes minutos antes, falharam:

```
test_limit_is_respected_exactly[postgres]
    assert len(repository.search_similar(one_hot(0), limit=2)) == 2
E   AssertionError: assert 1 == 2
```

A consulta retornou **1 de 3 linhas vivas**. Não é um bug na consulta, e não é transitório: é como uma varredura HNSW se comporta. A varredura explora no máximo `hnsw.ef_search` candidatos (40 por padrão), tuplas mortas são gastas desse orçamento antes de o MVCC filtrá-las, e o que sobreviver é a resposta. `VACUUM ANALYZE images` restaurou tanto as estatísticas quanto os testes.

Duas conclusões que valem mais que o acidente que as produziu:

**O "menos que `limit` apenas quando há menos candidatos" do contrato da porta é exato na escala de varredura sequencial e melhor-esforço sob ANN.** Essa não é uma promessa que o banco possa cumprir, então a garantia é documentada onde ela é qualificada — na docstring de `PostgresImageRepository.search_similar()` — em vez de silenciosamente presumida em toda parte.

**Manutenção de índice vira superfície operacional em escala.** Uma coleção com alta rotatividade acumula entradas mortas de índice que custam recall até o autovacuum alcançá-las. Nada neste RFC precisa agir sobre isso com 45 linhas; quem rodar o benchmark de 100k precisa, junto com `ef_search`.

---

## 8. Escopo global, deliberadamente

A busca cobre a tabela `images` inteira. Não há filtro de coleção, e isso é uma decisão, não uma omissão: `Collection` é uma entidade placeholder de uma linha, `ImageModel` não tem `collection_id`, e nenhuma migration cria um. Escopar a busca por coleção é uma mudança no que uma coleção *significa*, e precisa antes da tabela, da chave estrangeira e das regras de posse — a mesma razão pela qual o RFC-024 §20 recusou construir `IndexingJobs` em torno de um relacionamento que não tinha permissão de criar.

Um filtro por caminho também foi considerado e rejeitado. Ele teria tornado os testes de banco trivialmente isoláveis (§10.1) ao escopar toda busca ao diretório do próprio teste — uma preocupação de Application inventada para servir a um teste, dentro de um contrato que vai sobreviver a ele.

## 9. O que a pontuação significa

`SearchHit.similarity` é **similaridade de cosseno em [-1, 1]**.

O `<=>` do pgvector retorna *distância* de cosseno em [0, 2]; o repositório retorna `1 - distance`. A conversão acontece na Infrastructure, então nada acima dela sabe que uma distância existiu. O valor **não é limitado nem reescalado**.

### 9.1 A distribuição medida

Sobre todos os 1.125 pares (consulta, imagem) — 25 consultas × 45 imagens, CLIP real:

| estatística | valor |
| --- | --- |
| mínimo | **-0,1183** |
| máximo | **+0,3737** |
| média | +0,1524 |
| melhor acerto por consulta, mín. | +0,2352 |
| melhor acerto por consulta, máx. | +0,3737 |
| melhor acerto por consulta, média | **+0,3003** |

Duas coisas que isso torna concretas. **Similaridades negativas são reais** — ocorrem em um corpus comum com uma consulta comum, então um contrato de [0, 1] teria estado errado no primeiro dia, não em algum caso extremo futuro. E **as similaridades do CLIP são comprimidas**: um acerto de topo *correto* pontua ~0,30, não ~0,9. Quem construir uma UI ou um limiar sobre isto precisa ler a distribuição em vez da intuição — que é exatamente por que §13 remove a configuração `minimum_similarity` em vez de chutar um default para ela.

Isso segue o RFC-023 §6.1, que registrou normas L2 medidas (~11,6 imagem, ~8,5 texto) em vez de presumir que o modelo normalizava suas saídas.

## 10. Testes

### 10.1 Isolamento de banco, e sua ressalva

Testes de busca são consultas top-K sobre a tabela inteira. Ao contrário de `test_demo_corpus_indexing.py`, eles não podem se defender fazendo asserções sobre ids que criaram: uma linha commitada no banco de desenvolvimento compartilhado por outro trabalho pode se colocar *entre* os resultados esperados, e um número de recall calculado sobre uma tabela contaminada é errado, não ruidoso.

`empty_db_session` executa `DELETE FROM images` dentro da transação externa que `db_session` já abre, faz commit no nível da sessão (de modo que um `session.rollback()` no código sob teste não possa ressuscitar as linhas), e é desfeito pelo rollback de teardown existente. Nenhuma infraestrutura nova, nada deixado para trás.

**A ressalva, dita em vez de enterrada.** Isso mantém uma trava de escrita em toda linha existente durante o teste e não protege contra um escritor concorrente em outro processo. Isso é aceitável porque a suíte é de processo único: `requirements.txt` fixa `pytest` e `pytest-mock`, sem `pytest-xdist`. Deixa de ser aceitável no dia em que a execução paralela de testes chegar.

Um banco de testes dedicado foi considerado e rejeitado como trabalho de outro RFC, não por estar errado: `EngineInstance` é um singleton de módulo construído a partir de `settings.database_url` no import, há um único `.env` e um único `POSTGRES_DB` em `docker-compose.yml`, e um banco novo precisaria de `CREATE EXTENSION vector` mais todas as cinco migrations antes do primeiro teste. Isso é uma mudança em como a suíte inteira alcança o PostgreSQL, e é trabalho futuro (§15).

### 10.2 Um contrato, três implementações

`tests/infrastructure/persistence/test_search_similar_contract.py` é parametrizado sobre `PostgresImageRepository`, `InMemoryImageRepository` e `FakeImageRepository` — 9 testes × 3 = **27**. Os duplos só são úteis enquanto forem indistinguíveis da coisa real, e cada cláusula do contrato da porta é verificada contra os três:

| comportamento | por que está aqui |
| --- | --- |
| idêntico / ortogonal / oposto ranqueiam nessa ordem | a própria ordenação |
| similaridade ≈ 1,0 / ≈ 0,0 / **≈ -1,0** | o caso oposto é o que prova [-1, 1]; um clamp ou um reescalonamento passa no teste de ordenação e falha neste |
| `limit` respeitado exatamente | |
| menos candidatos que `limit` | uma página curta não é erro |
| linhas com embedding NULL nunca aparecem, nem sendo a maioria | |
| tabela vazia → `[]` | |
| empates se resolvem identicamente em toda parte | ids semeados em ordem reversa, para que a ordem de inserção não possa ser o que produz a resposta |
| vetor de tamanho errado levanta exceção, **inclusive em repositório vazio** | §10.3 |

Os vetores são eixos one-hot de 512 dimensões, não brinquedos de 3 elementos. Um vetor de 3 dimensões passa na validação de `EmbeddingVector` (que só rejeita vazio), funciona bem em Python, e é rejeitado pela coluna `vector(512)` — um teste que passaria duas vezes e falharia uma, por razões sem relação com o que ele verifica. Vetores one-hot também tornam os cossenos esperados exatos em vez de aproximados: 1, 0 e -1 por aritmética.

### 10.3 Incompatibilidade de dimensão: a resposta errada silenciosa

O `zip` do Python trunca ao operando mais curto. Um cosseno feito à mão sobre uma consulta de 3 dimensões e linhas de 512 dimensões retorna um número *plausível* calculado a partir de três dimensões — uma resposta errada indistinguível de uma certa. As duas implementações em memória, portanto, levantam `EmbeddingDimensionMismatchError` explicitamente, espelhando o cuidado que a docstring de `save_indexed_many()` já toma com a atomicidade.

O PostgreSQL também rejeita um vetor de largura errada, mas de forma *não confiável*: a coluna `vector(512)` só reclama quando uma linha é de fato comparada, então a mesma chamada ruim levanta exceção contra uma tabela populada e retorna `[]` contra uma vazia. `PostgresImageRepository` valida de antemão contra `EMBEDDING_DIMENSION`, o que torna a falha idêntica nas três implementações — e o teste de contrato faz a asserção sobre um repositório vazio precisamente para fixar isso.

As duas implementações em memória compartilham uma única função `cosine_search()` em vez de cada uma ter seu laço de cosseno. Dois laços escritos à mão seriam duas chances de divergir do pgvector e uma da outra.

### 10.4 O teste dormente, quitado

`tests/dataset/test_queries.py` carregava um laço de cosseno em `xfail` e uma docstring prometendo que *"quando a busca vetorial chegar, o teste a escrever é um que exercite `SearchImagesUseCase` de ponta a ponta — não este laço de cosseno feito à mão."*

Isso agora é `tests/dataset/test_semantic_search_e2e.py`. O laço se foi; as verificações estruturais sobre `queries.json` (caminhos existem, sem sobreposição, sem duplicatas, sem imagem aérea órfã) permanecem na suíte rápida e offline, porque dependem de dois arquivos JSON e de nenhum modelo.

### 10.5 De ponta a ponta, sob `slow`

CLIP real, PostgreSQL real, pgvector real, todas as 25 consultas. Marcado como `slow` e desselecionado pelo `addopts` padrão, seguindo o RFC-023 §12.1, para que um `pytest` puro permaneça offline.

O corpus é indexado **uma vez por módulo** (~47 s de inferência), não uma vez por teste. Escopo de módulo em vez de escopo de sessão é deliberado: a fixture mantém uma transação aberta sobre uma tabela `images` esvaziada, e o escopo de sessão esticaria essa trava por todos os testes de banco dos demais módulos.

Os testes fazem asserções sobre **agregados, nunca sobre resultados individuais**. O checkpoint alcança 64,0% de top-1 estrito neste corpus, então nove das 25 consultas *esperam* ranquear outra coisa primeiro — fixar `result[0] == "arquivo.jpg"` por consulta codificaria o ruído do modelo como requisito.

## 11. Resultados

Execução completa: `experiments/rfc-025-semantic-search/retrieval_run_output.log`. 45 imagens, 25 consultas, `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`, CPU, 2026-08-22.

### 11.1 Agregados

| métrica | medido |
| --- | --- |
| **Recall@5** (≥1 imagem relevante no top 5) | **84,0%** (21/25) |
| Recall@1 | 64,0% |
| Recall@10 | 96,0% |
| cobertura de relevantes@5 (fração média do conjunto relevante de uma consulta recuperada) | 70,0% |
| **contaminação por negativos difíceis@5** (posições do top-5 ocupadas por um quase-acerto declarado) | **12,8%** (16/125) |
| top-1 estrito (métrica do bake-off) | 64,0% |
| pairwise (métrica do bake-off) | 80,4% |

Duas métricas em vez de uma, porque falham de formas diferentes. Recall pergunta se a resposta certa está na página. Contaminação pergunta o que mais está dividindo a página — um índice degenerando em "qualquer coisa vagamente aérea" pode manter o recall estável enquanto preenche cada posição restante com respostas erradas plausíveis.

### 11.2 Onde falha, honestamente

As quatro consultas que erram no rank 5 são todas a mesma falha, e é a que o dataset foi construído para provocar: **o CLIP não consegue separar um lago natural de tanques artificiais de piscicultura** neste corpus.

| # | consulta | top-1 retornado |
| --- | --- | --- |
| 2 | "small natural lake on a farm, not an artificial pond" | `fish_ponds_02.jpg` |
| 4 | "swimming pool at a rural home, not a natural or artificial water body" | `fish_ponds_02.jpg` |
| 14 | "workshops and small businesses lining a commercial street" | `urban_suburban_overview` |
| 20 | "wide drone overview of a hillside town with a large..." | `urban_commercial_street` |

As consultas 2 e 4 também mostram uma limitação conhecida do modelo, e não da busca: **o CLIP não tem negação confiável.** Ambas explicitam o que *não* querem ("not an artificial pond", "not a natural or artificial water body") e ambas recuperam exatamente aquilo que excluíram. Uma torre de texto do tipo saco-de-conceitos lê "artificial pond" como um tópico, não como uma proibição.

Esta é a linha de base honesta de um checkpoint de propósito geral de 512 dimensões escolhido por velocidade, com fine-tuning explicitamente adiado (RFC-023 §3.6, §18) — não uma meta, e não um defeito na implementação da busca.

### 11.3 Verificação cruzada contra o bake-off do RFC-023

O bake-off ranqueou **estas 45 imagens** contra **estas 25 consultas** com um laço de cosseno em numpy, em um virtualenv separado. O RFC-025 recomputa as mesmas quantidades pelo caminho de produção — vetores normalizados em L2 em uma coluna `vector(512)`, ordenados por `<=>`.

| métrica | bake-off (numpy) | RFC-025 (pgvector) | delta |
| --- | --- | --- | --- |
| top-1 estrito | 60,0% | 64,0% | +4,0 pts = **1 consulta** |
| pairwise | 80,4% | **80,4%** | **0,0** |

**O pairwise bateu exatamente.** É o mais forte dos dois resultados: ele lê o ranking inteiro de 45 imagens em vez de apenas a primeira posição — 3.150 comparações (relevante, negativo difícil) — e é o que um operador errado, uma normalização perdida ou um vetor truncado teria destruído. O top-1 estrito difere em uma consulta de 25, onde uma consulta vale 4 pontos; em um conjunto de 25 consultas essa é a resolução do instrumento, e os dois caminhos rodaram em virtualenvs diferentes contra builds diferentes do transformers.

Nenhuma divergência a explicar, e portanto nenhum bug de implementação a caçar.

### 11.4 Os pisos de regressão, escolhidos depois de medir

Medidos primeiro, escritos depois. Os pisos ficam a duas consultas da medição — 8 pontos, em um conjunto onde uma consulta vale 4:

| critério | medido | travado em | folga |
| --- | --- | --- | --- |
| Recall@5 | 84,0% | **≥ 76%** | 2 consultas |
| contaminação@5 | 12,8% | **≤ 20%** | 9 posições de 125 |
| top-1 estrito | 64,0% | **≥ 56%** | 2 consultas, e 1 abaixo do bake-off |
| pairwise | 80,4% | **≥ 72%** | verificação de implementação, não critério de qualidade |

Uma consulta virando é ruído — uma atualização de torch ou transformers movendo aritmética de ponto flutuante, um corpus rematerializado. Duas virando na mesma direção é sinal. Uma quebra genuína no pipeline (sem tradução, template errado, operador errado, resultados retornados sem ranquear) não custa duas consultas; custa dez.

### 11.5 Português

Uma consulta de fumaça, e é uma frase completa de propósito: o RFC-023 §7.1 mediu o `langdetect` chamando `fazenda` de turco e `lago` de tagalo, então uma consulta de uma palavra em português chega ao CLIP sem tradução e mediria o caminho PT-bruto enquanto alegasse medir a tradução.

`"uma propriedade rural com um lago"` traduz e recupera `lake_property_01.jpg` dentro do top 5, sobrepondo o top 5 da frase em inglês em ao menos 3 de 5 posições. Nenhum conjunto de *ground truth* em português foi criado: o bake-off já quantificou a diferença de idioma (56,0% strict-PT(MT) contra 60,0% strict-EN), e o que estava por provar até agora era apenas que o tradutor está de fato ligado ao caminho de *busca*.

Uma observação que vale registrar para um leitor futuro: o Marian emite esta frase específica com um `". "` inicial, então o prompt de fato entregue ao CLIP é `"a photo of . a rural property with a lake"`. A recuperação ainda funciona e o tradutor é componente do RFC-023, então nada foi mudado aqui — mas é um artefato real, visível no log, e uma coisa plausível de limpar quando o caminho de tradução for tocado da próxima vez.

## 12. Desempenho contra `ARCHITECTURE.md` §22

| meta | medido | veredito |
| --- | --- | --- |
| Latência de busca < 1 s | **~90 ms** de ponta a ponta, consulta em inglês com o processo aquecido | ✅ atendida |
| | ~400 ms com o processo aquecido, consulta em português (tradução incluída) | ✅ atendida |
| | participação do banco: 0,36 ms com 45 linhas, 1,90 ms com 10.000 | ✅ desprezível |
| Tempo de inicialização < 5 s | modelos ainda carregam de forma preguiçosa | ✅ inalterado |

Decomposição do número em inglês: `encode_text()` sobre as 25 consultas mediu **média 89,7 ms, mediana 87,5 ms, máx. 128,7 ms** em CPU; a consulta pgvector fica abaixo de 2 ms nos dois tamanhos testados. **A latência da busca é a torre de texto**, não o banco — um fato que vale saber antes de alguém otimizar o SQL.

**A ressalva é a partida a frio.** A primeira consulta de um processo paga ~4,95 s para carregar o checkpoint do CLIP, e a primeira consulta *em português* paga mais ~4,20 s pelo tradutor Marian. Ambas estouram a meta de 1 segundo uma vez por processo. Isso é o design de carregamento preguiçoso do RFC-023 funcionando como especificado (§10 de lá), e a correção — aquecer o modelo na inicialização — pertence ao RFC que introduzir a camada HTTP, onde "inicialização" passa a significar alguma coisa.

## 13. Configuração

### 13.1 `top_k_results` finalmente tem um consumidor

Ele é configurável e não lido desde que o módulo de settings foi escrito. Agora é o tamanho de página padrão, injetado em `SearchImagesUseCase` pelo composition root — nunca importado pelo caso de uso, o que seria uma dependência de Application sobre Infrastructure que `test_application_architecture.py` proíbe. Isso segue `batch_size` e `metadata_prefetch_size` exatamente (RFC-024 §13).

`MAX_SEARCH_LIMIT = 100` é política de Application e vive com o caso de uso: uma configuração define um default, ela não autoriza uma página ilimitada.

Limites fora de faixa **levantam exceção** em vez de serem truncados. Um chamador que pede 10.000 e silenciosamente recebe 100 não consegue distinguir isso de um corpus contendo 100 imagens.

### 13.2 `minimum_similarity` removido

```python
minimum_similarity: float = Field(default=0.0, ge=0.0, le=1.0, ...)   # deletado
```

Zero leitores, e uma **faixa declarada que estava errada**: §9.1 mede pontuações a partir de -0,1183, então `ge=0.0` teria rejeitado configuração legítima para um filtro que não existia. Deixá-lo no lugar teria deixado uma chave de configuração que parece suportada, não é, e codifica uma afirmação falsa sobre a pontuação.

Um piso de similaridade pode muito bem valer a pena. Ele precisa da distribuição de §9.1 para escolher um default, e de uma decisão sobre o que "nenhum resultado" deve significar para um usuário — o que é uma questão de produto, não um campo esquecido. `TOP_K_RESULTS` mantém sua entrada em `.env.example`; `MINIMUM_SIMILARITY` foi removido de lá.

### 13.3 Value object `SearchQuery`: considerado, adiado

`embedding_vector.py` terminava com um comentário antecipando um value object `SearchQuery` "quando seu comportamento de domínio se tornar necessário". O RFC-025 o avaliou e o adiou, e o comentário foi reescrito para dizer isso, em vez de continuar apontando para um futuro indefinido que agora já foi examinado.

A validação é uma regra — rejeitar texto em branco — e vive no caso de uso. Um value object cujo único trabalho é carregar uma string já verificada não compra nada: todo ponto de construção continuaria a uma chamada de distância do único lugar que valida. Ele ganha seu lugar quando houver comportamento real a hospedar (normalização com a qual o repositório precise concordar, filtros estruturados, uma consulta que o modelo reescreva), e a validação se muda com ele nesse momento.

## 14. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Pontuação como campo em `Image` | A entidade é congelada e compara por id; um número por consulta em um tipo por imagem cria duas imagens "iguais" com dados diferentes (§4.1) |
| Reescalar a similaridade para [0, 1] | Destrói a distinção entre não relacionado (~0) e oposto (~-1), e o mínimo medido já é negativo (§9.1) |
| Retornar a distância bruta do pgvector | Vaza "menor é melhor" e a existência de `<=>` para toda camada acima da Infrastructure |
| Ranquear em Python | 100.000 × 512 floats por busca, e descarta o índice que o esquema já carrega |
| `search_similar(query: str)` na porta | Coloca o modelo de embedding atrás do repositório; o Domain passaria a depender de como texto vira vetor |
| Filtro de coleção ou de caminho na porta | Uma preocupação de Application inventada para tornar os testes isoláveis (§8) |
| Truncar um `limit` fora de faixa | Esconde a discordância dentro dos dados sobre os quais o chamador então raciocina (§13.1) |
| Banco de testes dedicado | Resposta certa, RFC errado — muda como a suíte inteira alcança o PostgreSQL (§10.1) |
| Reordenar os acertos no caso de uso | Passa por cima do único componente que viu os vetores armazenados |
| Um value object `SearchQuery` agora | Uma regra de validação não paga por um tipo (§13.3) |

## 15. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **O recall do HNSW não está validado.** Com 45 linhas o planejador nunca usa o índice (§7) | Aceito e declarado. Precisa do benchmark de 100k (RFC-026), não de um teste falso |
| **Os pisos vêm de um conjunto de 25 consultas.** Uma consulta vale 4 pontos | A folga tem duas consultas de largura, e os modos de falha que importam custam dez (§11.4) |
| **A negação não funciona.** "not an artificial pond" recupera o tanque (§11.2) | Uma propriedade do modelo. Fine-tuning é o RFC-023 §18 |
| **A partida a frio estoura a meta de latência** em ~5 s uma vez por processo (§12) | Pertence ao RFC da camada HTTP, onde o aquecimento tem onde morar |
| **O isolamento de testes trava a tabela** e assume uma suíte de processo único (§10.1) | Verdade hoje; revisitar antes de adotar `pytest-xdist` |
| **Uma varredura HNSW pode retornar menos acertos que `limit`**, e seu top-K é aproximado (§7.3) | Documentado na implementação que o qualifica; `ef_search` e política de vacuum pertencem ao RFC de escala |
| O `". "` inicial do Marian em uma tradução (§11.5) | Registrado, não alterado — componente do RFC-023 |

Trabalho futuro, em ordem aproximada de valor: um endpoint HTTP `/search`; o benchmark de 100k que de fato exercitaria o HNSW e o ajuste de `ef_search`; um filtro de similaridade mínima escolhido a partir da distribuição de §9.1; escopo por coleção quando `Collections` existir; um banco de testes dedicado; e fine-tuning de domínio, que §11.2 sugere ser onde a qualidade restante realmente está.

## 16. Não-objetivos

Confirmados como ausentes da implementação:

- **Endpoint HTTP `/search`.** Não há camada HTTP onde colocá-lo — o pacote de presentation contém placeholders de uma linha e uma rota `/health`
- frontend, thumbnails, paginação, autocompletar
- fine-tuning, LoRA, trocas de checkpoint, quantização, ONNX (RFC-023 §17)
- qualquer migration nova ou mudança de índice (§5)
- ajuste de HNSW: `m`, `ef_construction`, `ef_search`
- um benchmark de 100.000 imagens, e qualquer pretensão de cobertura de índice com 45 linhas (§7.1)
- `Collection`, `collection_id`, busca escopada por coleção (§8)
- `SearchHistory` (`ARCHITECTURE.md` §15)
- filtragem por similaridade mínima, reranking, busca híbrida, cross-encoders, busca imagem-para-imagem
- batching de consultas — uma consulta produz um embedding

## 17. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/value_objects/search_hit.py` | `SearchHit`, `SearchHits` |
| `backend/app/domain/exceptions/search_errors.py` | `EmptySearchQueryError`, `InvalidSearchLimitError`, `EmbeddingDimensionMismatchError` |
| `backend/tests/infrastructure/persistence/test_search_similar_contract.py` | O contrato de três implementações (§10.2) |
| `backend/tests/dataset/test_semantic_search_e2e.py` | CLIP real + PostgreSQL sobre as 25 consultas (§10.5) |
| `experiments/rfc-025-semantic-search/measure_retrieval.py` | A medição por trás de §9.1 e §11 |
| `experiments/rfc-025-semantic-search/planner_check.py` | A varredura de `EXPLAIN ANALYZE` por trás de §7 |
| `docs/rfcs/rfc-025-busca-semantica.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/domain/repositories/image_repository.py` | `search_similar()`; `from __future__ import annotations` |
| `backend/app/domain/value_objects/embedding_vector.py` | Comentário sobre `SearchQuery` reescrito (§13.3) |
| `backend/app/domain/exceptions/__init__.py` | Três novos exports |
| `backend/app/application/use_cases/search_images.py` | O caso de uso real, `MAX_SEARCH_LIMIT`, default injetado |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | `search_similar()`, guarda de dimensão |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Armazena embeddings; `search_similar()`; `cosine_search()` compartilhada |
| `backend/app/infrastructure/database/models/image_model.py` | Constante `EMBEDDING_DIMENSION` (§5) |
| `backend/app/infrastructure/config/settings.py` | `minimum_similarity` removido; `top_k_results` documentado |
| `backend/app/presentation/dependencies/__init__.py` | Injeta `settings.top_k_results` |
| `backend/tests/conftest.py` | Fixture `empty_db_session` (§10.1) |
| `backend/tests/application/fakes.py` | Armazenamento de embeddings, `seed_embedding()`, `search_similar()` |
| `backend/tests/application/test_search_images.py` | Reescrito: 15 testes de caso de uso |
| `backend/tests/dataset/test_queries.py` | Laço de cosseno dormente removido (§10.4) |
| `backend/tests/domain/test_image_repository_port.py` | `search_similar` no contrato da porta |
| `backend/tests/domain/test_domain_exceptions.py` | As três novas exceções |
| `backend/tests/presentation/test_dependencies.py` | Fiação de `top_k_results` |
| `.env.example` | `MINIMUM_SIMILARITY` removido |

## 18. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | **471 passaram**, 56 desselecionados — a partir de 429 passados + 1 xfailed no `HEAD` |
| `pytest -m slow` | **56 passaram** (a partir de 48), incluindo os 8 testes de busca de ponta a ponta |
| `black --check .` | limpo, 126 arquivos |
| `ruff check .` | limpo |
| `mypy` | **5 erros em 4 arquivos** — idênticos byte a byte à linha de base preexistente, verificado contra o `HEAD` em uma worktree de rascunho; **0 em código novo ou modificado pelo RFC-025** |
| `alembic heads` | `26058b9e1d9a (head)` — inalterado, nenhuma migration adicionada |
| Imports da camada Application | sem `app.infrastructure`, `sqlalchemy`, `fastapi`, `pydantic`, `alembic` |
| Banco de desenvolvimento | 3 linhas preexistentes, inalteradas — todo teste e medição desfeitos |
| Contrato do repositório | 27 testes = 9 comportamentos × 3 implementações, todos verdes |
| Verificação cruzada com o bake-off | pairwise idêntico ao RFC-023 (§11.3) |
