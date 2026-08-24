# RFC-022 — Dataset de Demonstração

**Status:** Proposto
**Autor:** SolidVision
**Depende de:** RFC-019 (Implementações de Repositório), RFC-020 (Metadados Incrementais), RFC-021 (Worker de Indexação)
**Bloqueia:** Integração do modelo de embedding real, busca vetorial, RFC de benchmark
**Última atualização:** 2026-08-14

---

## 1. Contexto

O pipeline de indexação está completo de ponta a ponta no nível do código: `FilesystemImageProvider` descobre arquivos, `IndexingWorker` conduz `IndexOrUpdateImageUseCase`, e `PostgresImageRepository.save_indexed()` grava a linha da imagem junto com seu embedding em uma coluna `vector(1152)` indexada por HNSW.

O que não existe é **qualquer corpus reprodutível contra o qual executá-lo**.

O único dado de imagem no repositório é `backend/test-images/`, com três arquivos sem relação entre si (um gato, um carro, uma foto nomeada por UUID), sem manifesto, sem atribuição e sem expectativas registradas. Ele não consegue responder nenhuma das perguntas que o projeto agora precisa responder:

- O worker pula arquivos inalterados, como o RFC-020 especifica?
- Um arquivo corrompido é isolado sem abortar a execução?
- Extensões não suportadas são de fato filtradas?
- Quando existir um modelo de embedding real, a qualidade da recuperação será boa o bastante?
- O sistema atende à meta de 100.000 imagens de `ARCHITECTURE.md` §22?

Este RFC define o dataset que as responde.

---

## 2. Decisão Orientadora

A decisão central de projeto é que **"dataset de demonstração" não é um artefato, mas três**, unificados por um único formato de manifesto.

Colapsá-los em uma pasta de JPEGs produz um corpus que é simultaneamente pequeno demais para benchmark, grande demais para commitar, e indocumentado demais para servir de base a asserções.

| Corpus | Tamanho | Commitado | Propósito |
|---|---|---|---|
| **Fixture** | ~15 arquivos | Não — gerado em tempo de teste | Testes unitários e de integração determinísticos |
| **Demo** | 40–60 fotos reais | Sim | Demos, E2E, QA manual, avaliação semântica futura |
| **Scale** | 1k → 100k | Não — gerado, no gitignore | Benchmarks contra as metas de desempenho de §22 |

O ativo duradouro é o **manifesto**, não as imagens. Imagens são substituíveis; o manifesto codifica expectativas, procedência, licenciamento e — quando existir um modelo real — o *ground truth* de recuperação. O ferramental é escrito uma vez contra o formato do manifesto e serve aos três corpora.

---

## 3. Objetivos

- Fornecer um corpus reprodutível que exercite o pipeline inteiro: sistema de arquivos → worker → caso de uso → PostgreSQL → pgvector.
- Tornar testável o comportamento de skip incremental do RFC-020, que hoje não é (ver §7.2).
- Cobrir casos estruturais de borda que o isolamento de erro por arquivo do worker deveria tratar.
- Entregar *ground truth* de recuperação semântica **agora**, dormente, para que no dia em que um modelo de embedding real chegar ele se torne um conjunto de avaliação com zero retrabalho.
- Registrar licenciamento e atribuição de toda imagem commitada.
- Não introduzir nenhum conceito novo de Domain, nenhuma porta nova, nenhuma abstração nova dentro de `app/`.

### Não-objetivos

- Integrar um modelo de embedding real. Fora de escopo; este RFC prepara o terreno.
- Implementar busca por similaridade vetorial. `SearchImagesUseCase` continua um stub.
- Executar o benchmark de 100k. Bloqueado por §7.4; adiado para um RFC de benchmark.
- Qualquer coisa que toque `Collection` além de reservar um campo no manifesto.

---

## 4. Layout

```text
backend/
├── dataset/                        # só dados — nada de Python, não é pacote
│   └── demo/
│       ├── manifest.json           # construído
│       ├── queries.json
│       └── images/
│           ├── aerial/rural/       # construído — 21 fotos
│           ├── aerial/urban/       # construído — 19 fotos
│           └── everyday/           # construído — 5 fotos, Commons CC0/CC-BY (§9.5)
│
├── dataset_tools/                  # Python — parsing de manifesto, materialização
│   ├── __init__.py                 # construído
│   ├── reencode.py                 # construído
│   ├── manifest.py
│   ├── materialize.py
│   ├── seed_demo.py                # python -m dataset_tools.seed_demo
│   └── generators/
│       ├── __init__.py             # construído
│       ├── hard_cases.py           # construído — gerado, nunca commitado
│       ├── fixture_corpus.py
│       └── scale_corpus.py
│
└── tests/dataset/
    ├── test_hard_cases.py          # construído
    ├── test_manifest.py
    └── test_demo_corpus_indexing.py

data/                               # área de trabalho em runtime, no gitignore (indexing_root_path)
```

### 4.1 Por que `dataset_tools/` e não `datasets/`

`pyproject.toml` define `pythonpath = [".", "backend"]`, então qualquer diretório de primeiro nível dentro de `backend/` se torna importável pelo nome puro. Um pacote chamado `datasets` sombrearia a biblioteca `datasets` do HuggingFace — que provavelmente será instalada junto com `transformers` quando o adaptador SigLIP chegar. A falha de import resultante seria confusa e apareceria longe da sua causa.

`dataset_tools` não tem essa colisão. O diretório de dados `dataset/` deliberadamente não contém `__init__.py` nem arquivos `.py`, então nunca é importável.

### 4.2 Por que este código não vive em `app/`

`dataset_tools/` é ferramental de desenvolvimento, não comportamento de aplicação entregue. Colocá-lo sob `app/infrastructure/` faria o pacote de produção crescer com código que só testes e demos invocam, e convidaria a uma abstração `DatasetPort` de que a arquitetura não precisa.

O caminho de *seeding* deliberadamente reutiliza os componentes de produção existentes em vez de reimplementá-los:

```text
manifest.json
      │
      ▼
materialize()  ──►  raiz temporária de demo em disco (mtimes carimbados)
                          │
                          ▼
                 FilesystemImageProvider
                          │
                          ▼
                  IndexingWorker  ──►  IndexOrUpdateImageUseCase
                          │
                          ▼
                 PostgresImageRepository
```

O seeding é, portanto, ele próprio um teste fim a fim do RFC-021, não uma implementação paralela dele.

---

## 5. Formato do Manifesto

### 5.1 Escolha de formato: JSON, não YAML

`backend/requirements.txt` não inclui PyYAML, e o projeto tem evitado consistentemente dependências de que não precisa. JSON é parseável com a biblioteca padrão e tipa de forma limpa sob `mypy --strict`.

**Trade-off:** JSON não tem comentários, então a justificativa por entrada precisa ir em um campo dedicado em vez de um comentário inline. Aceito.

### 5.2 `manifest.json`

```json
{
  "version": 1,
  "license_note": "All images are CC0 or CC-BY. Per-entry attribution below.",
  "images": [
    {
      "relative_path": "images/aerial/rural/lake_property_01.jpg",
      "collection": "demo-aerial",
      "file_modified_at": "2024-06-01T12:00:00Z",
      "caption": "Rural property with a house, outbuildings, and a small lake, with a horse grazing nearby",
      "tags": ["aerial", "rural", "property", "house", "water", "lake", "horse"],
      "source_url": null,
      "license": "proprietary-permitted",
      "attribution": "the project's photographer",
      "expect": "indexed",
      "burst_group": "lake_property"
    }
  ]
}
```

Contratos dos campos:

| Campo | Contrato |
|---|---|
| `relative_path` | Chave de identidade. Relativo à raiz do corpus, separadores posix. **Nunca um UUID** — ver §7.1. |
| `collection` | Reservado. Sem consumidor hoje; `Collection` ainda não existe. Evita reconstruir o manifesto quando existir. |
| `file_modified_at` | mtime autoritativo, carimbado no arquivo durante a materialização. Ver §7.2. |
| `caption` / `tags` | Semântica legível por humanos. Insumo para a autoria de `queries.json` e para avaliação futura. |
| `source_url` | URL do original, ou `null` para trabalho inédito do próprio fotógrafo. Ver §8. |
| `license` | `proprietary-permitted` para os originais do fotógrafo; `CC0` / `CC-BY-4.0` etc. para negativos vindos do Commons. Ver §8. |
| `attribution` | Obrigatório para toda imagem commitada, mas não precisa ser um nome legal completo — ver §8. |
| `expect` | `indexed` \| `ignored` \| `failed`. A asserção que a suíte de testes impõe em uma primeira execução — vocabulário definido em §6.2. |
| `burst_group` | Opcional. Agrupa quadros da mesma órbita/passagem de drone — vários foram fotografados como rajadas quase duplicadas de um mesmo local. Alimenta diretamente o caso de quase duplicatas de §6.1; entradas sem irmão omitem este campo. |

### 5.3 `queries.json`

```json
{
  "version": 1,
  "queries": [
    {
      "query": "rural property with a lake",
      "relevant": [
        "images/aerial/rural/lake_property_01.jpg",
        "images/aerial/rural/lake_property_02.jpg"
      ],
      "hard_negatives": [
        "images/aerial/rural/river_farmland_03.jpg"
      ]
    }
  ]
}
```

Este arquivo é entregue **dormente**. Seu teste consumidor está marcado com `xfail(strict=False)`, com um motivo que nomeia a dependência bloqueante, porque `FakeEmbeddingModel` não consegue satisfazê-lo (§7.3). Quando um modelo real chegar, remover o marcador ativa o conjunto de avaliação sem nenhuma mudança nos dados.

---

## 6. Plano de Conteúdo

### 6.1 Corpus de demonstração

Escolhido de modo que a qualidade de recuperação seja de fato *demonstrável*, em vez de trivialmente satisfeita:

- **Domínio-alvo, densamente amostrado** — fotografia aérea rural e de propriedades, correspondendo ao caso de uso declarado em `ARCHITECTURE.md` §1. É o grosso do corpus.
- **Negativos difíceis** — floresta aérea vs. campo agrícola aéreo; rio vs. lago. Um sistema que só separa gatos de plantações não prova nada. A qualidade do ranking só é visível nos quase-acertos.
- **Quase duplicatas** — a mesma cena de dois ângulos. Alimenta o item de detecção de duplicatas do roadmap (§23).
- **Negativos triviais** — um pequeno número de fotos cotidianas, absorvendo o conteúdo atual de `test-images/`.

**Como construído**, as 40 fotos entregues vieram como 8 rajadas de drone e se dividiram em 21 rurais / 19 urbanas, em vez do viés dominado por rural assumido acima. Duas consequências:

- O requisito de quase duplicatas é satisfeito generosamente — a maioria dos quadros são irmãos de órbita de um mesmo local, rastreados pelo campo `burst_group` do manifesto.
- O requisito de negativos difíceis é satisfeito por conteúdo que *emergiu*, e não por conteúdo que foi procurado: uma piscina, tanques artificiais de piscicultura e um lago natural aparecem todos como corpos d'água distintos em `hillside_property_pool_*`, `fish_ponds_*` e `lake_property_*`. Distinguir esses três é uma tarefa de recuperação genuinamente difícil e um teste melhor do que o par floresta-vs-plantação originalmente proposto.
- O **balde de negativos triviais** é populado separadamente de `test-images/`, que foi apagado por completo em vez de incorporado. `images/everyday/` agora contém 5 fotos CC0/CC-BY (gato, cachorro, bicicleta, xícara de café, notebook) obtidas do Wikimedia Commons via sua API — ver §9.5.

### 6.2 Casos difíceis

É onde o dataset ganha seu valor como teste de sistema inteiro.

**Estes são gerados, não commitados** (resolvendo a questão aberta 1 de §13). Todo caso pode ser sintetizado fielmente com Pillow, o que evita commitar binários deliberadamente corrompidos e evita depender de como um dado sistema de arquivos ou cliente git normaliza um nome de arquivo acentuado. São produzidos por `dataset_tools/generators/hard_cases.py` em uma raiz temporária em tempo de teste, e o corpus é determinístico byte a byte entre execuções.

As expectativas vivem no módulo gerador, ao lado do código que escreve os arquivos, e não em `manifest.json`, para que um caso e o comportamento asseverado não possam se afastar um do outro. Um teste garante que a árvore gerada e a tabela declarada descrevem exatamente o mesmo conjunto.

### Vocabulário de resultado

`expect` descreve uma **primeira** execução contra um banco vazio:

| Valor | Significado |
|---|---|
| `indexed` | Descoberto, e uma linha é gravada |
| `ignored` | Nunca descoberto — filtrado pela checagem de extensão em `FilesystemImageProvider.discover()`, então nunca chega ao caso de uso |
| `failed` | Descoberto, mas a indexação levantou exceção, capturada pelo `except` por arquivo de `IndexingWorker.run()` |

Isso substitui o trio anterior `indexed | skipped | failed`. "Skipped" era ambíguo entre *filtrado na descoberta* e *inalterado em uma execução posterior* — duas coisas sem relação. O resultado de skip incremental não é um valor de `expect` de forma alguma: é uma propriedade de uma segunda execução, uniforme para tudo que indexou na primeira, e é asseverado uma única vez pelo teste de duas execuções.

### Casos

| Caso | `expect` | Notas |
|---|---|---|
| JPEG CMYK, JPEG em tons de cinza, TIFF 16-bit, PNG RGBA | `indexed` | |
| JPEG com rotação EXIF (orientação 6) | `indexed` | Tratamento de orientação adiado para o RFC de thumbnails |
| Extensão `.JPG` em maiúsculas | `indexed` | `extension` é persistido em minúsculas |
| `fazenda São João.jpg` | `indexed` | Espaços e não-ASCII sobrevivem à ida e volta por `ImagePath` |
| Subdiretório profundamente aninhado | `indexed` | Alcançado apenas via `rglob` |
| Conteúdo idêntico em dois caminhos | `indexed` ×2 | Produz **duas** linhas — fixa o id derivado de caminho de §7.1 |
| Arquivo de zero bytes | `indexed` † | |
| JPEG truncado | `indexed` † | |
| `.gif`, `.txt` | `ignored` | Filtrados na descoberta; nunca chegam ao caso de uso |

† **Estes indexam limpamente hoje, e isso é a constatação, não um descuido.** `FakeEmbeddingModel.encode_image()` faz hash apenas do id e do caminho — nunca abre o arquivo (§7.3). Nada no pipeline atual decodifica pixels, então um arquivo cujos *bytes* estão quebrados ainda produz uma linha perfeitamente boa. O gerador registra o resultado futuro em um campo separado, `expect_with_pixel_decoding` (`failed` para ambos), que se ativa no dia em que um modelo de embedding real, gerador de thumbnail ou extrator de metadados abrir uma imagem pela primeira vez. Asseverar `failed` hoje falharia contra o comportamento real; asseverar `indexed` sem registrar a intenção perderia a informação.

A linha de caminho duplicado não é um bug sendo consagrado; é comportamento atual e deliberado, documentado em `image_identity.py`, e fixá-lo significa que qualquer mudança futura no esquema de ids falha alto em vez de silenciosamente.

---

## 7. Restrições Impostas pelo Código Existente

Quatro propriedades da implementação atual restringem diretamente este design. Cada uma é comportamento real de código commitado, não hipótese.

### 7.1 `ImageId` depende do caminho absoluto

`compute_image_id()` deriva o id como `uuid5(SOLIDVISION_PATH_NAMESPACE, str(path))`, e `IndexingWorker` lhe passa `discovered.path` direto do `rglob`, que é absoluto sempre que a raiz da varredura for.

A mesma imagem, portanto, recebe um **UUID diferente em cada máquina e em cada local de checkout**.

**Consequência:** o manifesto é chaveado por `relative_path` e nunca por UUID, e o carregador calcula os ids em tempo de carga a partir da raiz resolvida. Nenhum arquivo *golden* pode conter um id fixo.

Isso também torna concreta uma tensão latente de projeto: ids não são portáteis entre máquinas, o que importará para qualquer funcionalidade futura de exportação de coleção ou de múltiplas máquinas. Trazer essa pressão à superfície é um benefício secundário deste RFC; mudar o esquema está explicitamente fora de escopo e exigiria seu próprio ADR e uma estratégia de migração, como `image_identity.py` já adverte.

### 7.2 O Git não preserva mtimes

Um clone novo carimba toda imagem commitada com o horário do checkout. Qualquer teste de indexação incremental sobre o corpus de demonstração seria, portanto, não reprodutível — a primeira execução indexa, e se a segunda pula depende de temporização do sistema de arquivos em vez da lógica sob teste.

**Consequência:** `file_modified_at` é obrigatório no manifesto, e `materialize()` o aplica com `os.utime()` após a cópia. É precisamente isso que torna o caminho de skip-quando-inalterado do RFC-020 testável, e é o detalhe mais importante deste RFC.

### 7.3 `FakeEmbeddingModel` nunca lê pixels

`encode_image()` faz hash de `f"image::{image.id}::{image.path}"`. O conteúdo da imagem nunca é aberto.

**Consequência:** hoje este dataset consegue validar apenas comportamento de *pipeline* — descoberta, filtragem, detecção de mudança, skip/update, persistência, escrita de vetores. Não consegue validar qualidade de recuperação. `queries.json` é entregue dormente em vez de adiado para um RFC posterior, porque autorar *ground truth* é trabalho manual melhor feito enquanto se monta as imagens, e fazê-lo depois significaria manusear cada imagem duas vezes.

### 7.4 Os vetores do fake eram quase degenerados sob distância de cosseno (corrigido)

No `_build_embedding` original, o termo posicional `(index + 1) * 0.125` chegava a **144** na última dimensão, enquanto o termo derivado do conteúdo permanecia dentro de `(0, 1]`. Todo vetor produzido era a mesma rampa íngreme mais uma perturbação desprezível, então a similaridade de cosseno entre quaisquer duas imagens era ≈ 1,0.

**Consequência:** medições de **recall** do HNSW sobre um corpus de escala teriam sido sem sentido. Tempo de construção do índice e latência de consulta ainda seriam reais, mas qualquer número de recall seria artefato de entrada degenerada. É por isso que o benchmark de 100k continua adiado, independentemente desta correção.

**Corrigido.** `_build_embedding` agora expande a semente via SHA-256 em modo contador em `embedding_dimension` floats espalhados por `[-1, 1)`, centraliza na média e normaliza em L2 para um vetor unitário. SHA-256 foi escolhido em vez do `hash()` embutido do Python especificamente porque `hash()` é salgado por processo via `PYTHONHASHSEED` e não é reprodutível entre execuções ou máquinas — determinismo é requisito rígido aqui (`ARCHITECTURE.md` seção 20), e a normalização não o enfraquece: a mesma semente produz os mesmos bytes de hash sempre, em qualquer máquina.

Empiricamente, a similaridade de cosseno entre sementes não relacionadas (`"cat"`, `"dog"`, `"rural property with a lake"`, `"downtown office tower"`) agora se espalha por aproximadamente ±0,07, centrada em 0 — correspondendo ao comportamento teórico de vetores unitários aleatórios em um espaço de 1152 dimensões (desvio padrão esperado ≈ 1/√1152 ≈ 0,029). Uma semente idêntica ainda produz similaridade de cosseno 1,0 consigo mesma. Novos testes em `test_fake_embedding_model.py` fixam norma unitária, centralização na média e o espalhamento entre sementes diretamente, de modo que uma regressão ao comportamento antigo de rampa falha alto.

**Trade-off, concretizado:** isso mudou todos os vetores que o modelo fake produz. Nenhum teste existente asseverava um valor literal de embedding fake, então nada precisou de atualização além dos novos testes adicionados junto com a correção.

---

## 8. Licenciamento

`backend/test-images/` atualmente contém fotografias vindas da Wikimedia sem atribuição registrada, uma delas com 2 MB. O repositório carrega um arquivo `LICENSE` e o histórico do git é permanente, o que torna isso digno de resolução agora, e não depois.

O corpus aéreo de demonstração de 40 imagens consiste inteiramente em trabalho original do fotógrafo do projeto — o mesmo fotógrafo aéreo descrito em `ARCHITECTURE.md` §1 — incluído neste repositório público com sua permissão explícita e confirmada. Não são imagens Creative Commons e não carregam `source_url`; `license` é registrado como `proprietary-permitted` por entrada do manifesto, e `attribution` é intencionalmente não identificadora (`"the project's photographer"`) em vez de um nome completo, por preferência do fotógrafo.

Como são propriedades reais e identificáveis, duas precauções adicionais se aplicam além do que um corpus de origem CC exigiria:

- `dataset_tools/reencode.py` aplica a orientação EXIF e então remove todo o EXIF, incluindo coordenadas de GPS, antes de a imagem ser commitada. Nenhuma imagem chega a `dataset/` com metadados de localização intactos.
- O consentimento cobre as 40 imagens específicas colocadas em `dataset/demo/`. Não se estende automaticamente a acréscimos futuros do mesmo fotógrafo — cada novo lote precisa de sua própria confirmação antes de ser commitado, re-encodado ou não.

Negativos do cotidiano (§6.1) continuam vindo do Wikimedia Commons ou de fontes com licenciamento similar, já que não carregam essa exigência de consentimento. Para esse conteúdo:

- Exigir `source_url`, `license` e `attribution` em toda entrada.
- Restringir a CC0 ou CC-BY.

Este RFC também:

- Re-encoda toda imagem commitada para cerca de 1024 px no lado maior, mirando menos de 200 KB cada, mantendo o peso total commitado na casa dos poucos megabytes.
- Incorpora ao corpus de demonstração os arquivos aproveitáveis de `test-images/` com atribuição adequada, e remove o diretório.

---

## 9. Entregáveis

| # | Entregável | Situação |
|---|---|---|
| 1 | `backend/dataset/demo/` — imagens licenciadas, `manifest.json`, `queries.json` | **Feito** — 45 imagens (40 aéreas + 5 cotidianas), `manifest.json`, `queries.json` |
| 2 | `dataset_tools/manifest.py` — modelo tipado e carregador; levanta exceção em violação de esquema | **Feito** |
| 3 | `dataset_tools/materialize.py` — copia um corpus para uma raiz-alvo e carimba mtimes | **Feito** |
| 4 | `dataset_tools/generators/fixture_corpus.py` — fixtures determinísticas geradas com Pillow | **Não construído — superado, ver 9.4** |
| 5 | `dataset_tools/generators/scale_corpus.py` — gera *N* imagens sintéticas em uma raiz no gitignore | **Feito** |
| 6 | `dataset_tools/seed_demo.py` — CLI ligando os componentes reais ao PostgreSQL | **Feito** — `python -m dataset_tools.seed_demo`; ver 9.3 para uma correção de destino padrão feita durante a construção |
| 7 | Fixture pytest `demo_corpus` materializando em `tmp_path` com mtimes corretos | **Feito** — `backend/tests/dataset/conftest.py` |
| 8 | Testes em `backend/tests/dataset/` asseverando todo valor de `expect`, mais um teste de duas execuções | **Feito** — 66 testes: validação de manifesto, correção de materialização, casos difíceis, validade estrutural de `queries.json` mais um teste de recuperação dormente, geração de corpus de escala, e o pipeline completo sobre Postgres incluindo um teste de duas execuções com skip e um teste de *touch* em um arquivo |
| 9 | Correção de normalização de `FakeEmbeddingModel` (§7.4) | **Feito** |
| 10 | Remoção de `backend/test-images/` | **Feito** |
| 11 | `dataset_tools/reencode.py` — re-encodar conforme a especificação de §8, remover EXIF/GPS | **Feito** (adicionado; não estava na lista original) |
| 12 | `dataset_tools/generators/hard_cases.py` — corpus gerado de casos de borda | **Feito** |

**Lacuna remanescente:** `images/everyday/` (o balde de negativos triviais de §6.1) ainda está vazio. É uma tarefa de obtenção de conteúdo (fotos cotidianas licenciadas em CC do Wikimedia Commons, conforme §8), não uma tarefa de código, e nada do que foi construído até aqui depende disso.

### 9.1 Pontos de contato com a configuração

- `pyproject.toml` — registrar qualquer marcador novo do pytest em `[tool.pytest.ini_options] markers`. `addopts` inclui `--strict-markers`, então um marcador não registrado é erro rígido, não aviso. *(Nenhum marcador necessário ainda; os testes de casos difíceis não exigem banco.)*
- `pyproject.toml` — **feito:** `backend/dataset_tools` adicionado a `[tool.mypy] files`.
- `pyproject.toml` — **feito:** `dataset_tools` adicionado a `[tool.ruff.lint.isort] known-first-party`, que antes listava apenas `app` e por isso ordenava o novo pacote como de terceiros.
- `.gitignore` — **feito:** adicionado `data/`. Cobre tanto a saída materializada de `seed_demo.py` (`data/demo/`) quanto a saída padrão de `scale_corpus.py` (`data/scale/`) — ambas ficam sob o já ignorado `data/`, então nenhuma entrada separada foi necessária para o corpus de escala afinal.
- Nenhuma mudança em `Settings` é necessária; `indexing_root_path` já existe e é o ponto de injeção para a raiz do corpus.

### 9.2 Bloqueio preexistente: o mypy não conseguia rodar no repositório inteiro (corrigido)

`python -m mypy` costumava falhar antes de checar qualquer coisa:

```text
backend\tests\application\fakes.py: error: Source file found twice under
different module names: "application.fakes" and "tests.application.fakes"
Found 1 error in 1 file (errors prevented further checking)
```

Causa raiz: `backend/app/__init__.py` existe mas `backend/__init__.py` não, então o mypy descobria `backend` como raiz de busca implícita ao percorrer os arquivos de `app/...` em `files =`. Essa mesma raiz permitia que `tests` resolvesse como pacote de namespace para os `from tests.application.fakes import ...` usados por quatro arquivos de teste. Mas `backend/tests/application/fakes.py` era *também* percorrido diretamente como parte de `files = [..., "backend/tests", ...]`, onde o algoritmo do mypy de subir através de `__init__.py` parava um nível cedo demais — em `backend/tests`, já que `tests/` em si não tinha `__init__.py` — produzindo um nome de módulo diferente para o mesmo arquivo. Dois nomes para um arquivo é parada dura para o mypy.

Isso antecede o RFC-022 (reproduz com a mudança de `files=` em `[tool.mypy]` revertida) e significava que a checagem estrita de tipos estava silenciosamente inoperante no repositório inteiro.

**Corrigido** adicionando `backend/tests/__init__.py`, correspondendo à própria resolução (a) sugerida pelo mypy na mensagem de erro. As duas computações de nome de módulo agora concordam em `tests.application.fakes`. Verificado: `mypy` checa 93 arquivos no repositório inteiro sem travar; `pytest` continua passando (167/167); `ruff`/`black` limpos (o ruff reordenou automaticamente três imports agora classificados de forma diferente, apenas cosmético).

Fazer isso aposentou o crash mas não corrigiu mais nada — trouxe à superfície 5 erros de tipo preexistentes em arquivos de teste que o mypy nunca havia alcançado: uma instanciação de classe abstrata em `test_embedding_model_port.py`, uma checagem `isinstance` inalcançável e um erro de tipo de argumento `list[float] | None` em `test_postgres_image_repository.py`, e uma incompatibilidade de tipo de retorno de fixture em `test_health.py`. Nenhum é novo; nenhum é tratado por este RFC.

### 9.3 O destino de `seed_demo.py` não pode usar `indexing_root_path` como padrão

A primeira implementação definia `--target` como `settings.indexing_root_path` por padrão — o diretório que um CLI de worker de produção eventualmente varrerá em busca da coleção real de fotos de um usuário. Esse padrão foi testado ao vivo contra o banco de desenvolvimento local antes desta seção ser escrita, o que é o que trouxe o problema à tona: `indexing_root_path` já termina em `images`, e todo `relative_path` do manifesto começa com `images/`, então a saída materializada foi parar em um duplicado `data/images/images/aerial/...`. Cosmético por si só, mas expôs a questão real por baixo — no momento em que existir um CLI de worker de produção, usar o mesmo diretório como padrão do seeder de demonstração significa que executar `seed_demo.py` por hábito misturaria silenciosamente 40 fotos de demo na coleção real e configurada de um usuário.

O padrão agora é `data/demo` (independente de `indexing_root_path`, não derivado dele, para que uma sobrescrita incomum de `INDEXING_ROOT_PATH` também não possa produzir um caminho derivado estranho). A saída materializada agora espelha exatamente o layout do corpus de origem: `data/demo/images/aerial/...`. Servir os dados de demonstração através da aplicação em execução exige apontar `INDEXING_ROOT_PATH` para `data/demo` explicitamente — uma mudança deliberada e visível no `.env`, não um padrão de script.

Verificado fim a fim contra o container PostgreSQL de desenvolvimento local: uma primeira execução indexou todas as 40 entradas do manifesto, e uma segunda execução imediata pulou todas as 40, confirmando que o caminho de skip incremental (RFC-022 seção 7, §10) se sustenta em uma invocação real, e não apenas sob o isolamento por SAVEPOINT da suíte de testes.

### 9.4 `fixture_corpus.py` não foi construído

O design original (§2) pedia um corpus de "fixtures" gerado com Pillow, distinto dos casos estruturais de borda de `hard_cases.py`, para testes unitários/de integração determinísticos de propósito geral. Antes de construí-lo, uma checagem da suíte de testes existente (`test_filesystem_image_provider.py`, `test_indexing_worker.py`) mostrou que o padrão estabelecido e funcional para testes de pipeline/orquestração é `path.write_bytes(b"data")` — bytes de placeholder crus, sem Pillow algum. Isso está correto, não é atalho: `FilesystemImageProvider` só lê metadados de sistema de arquivos (extensão, tamanho, mtime), e `FakeEmbeddingModel` nunca abre os pixels de um arquivo (§7.3), então nada nesses testes se beneficia de conteúdo de imagem real.

Isso deixa nenhum consumidor concreto para um corpus genérico de "N imagens sintéticas válidas". Qualquer coisa que precise de estrutura de imagem real e abrível já é servida por `hard_cases.py` (CMYK, EXIF, 16-bit, alfa etc.); qualquer coisa que precise apenas de uma extensão suportada já é servida pelo padrão de bytes crus de uma linha acima. Construir `fixture_corpus.py` mesmo assim teria sido abstração prematura — um terceiro mecanismo duplicando o que os outros dois já cobrem, sem nenhum teste na suíte que o importasse. O entregável 4 está marcado como não construído em vez de feito; pode ser revisitado se um consumidor genuíno aparecer (por exemplo, um futuro gerador de thumbnails que precise de conteúdo de imagem válido-porém-arbitrário em volume).

### 9.5 Obtenção de `images/everyday/` no Wikimedia Commons

Cinco negativos triviais — gato, cachorro, bicicleta, xícara de café, notebook — foram obtidos do Commons via sua API `action=query` em vez de manualmente, usando `iiprop=extmetadata` para conseguir os campos legíveis por máquina `LicenseShortName`, `Artist`/`Credit` e `AttributionRequired` por candidato, exatamente como planejado quando este RFC foi rascunhado. Processo, não automatizado em um script commitado:

1. Consultar `generator=categorymembers` contra um punhado de categorias cotidianas (`Category:Domestic cats`, `Category:Dogs`, `Category:Bicycles`, `Category:Coffee cups`, `Category:Laptops`), filtrando resultados para `image/jpeg` e `LicenseShortName` em `{CC0, CC BY *, Public domain}` — nunca CC-BY-SA, conforme a restrição de §8 a CC0-ou-CC-BY para conteúdo que não seja do fotógrafo.
2. Buscar o `extmetadata` completo (incluindo `LicenseUrl`) para os títulos escolhidos, e baixar a partir do próprio campo `imageinfo.url` da API, textualmente, em vez de uma URL reconstruída à mão — uma primeira tentativa que redigitou três URLs manualmente sofreu uma falha silenciosa (ver abaixo).
3. Abrir todo arquivo baixado e confirmar visualmente que corresponde ao seu título antes que chegue perto do repositório. Não é formalidade: é a mesma disciplina de verificação de conteúdo aplicada às 40 fotos aéreas do fotógrafo, estendida à obtenção de terceiros.
4. Re-encodar através do `dataset_tools/reencode.py` existente — sem caminho de código separado para imagens vindas do Commons.
5. Adicionar entradas de manifesto com valores reais de `source_url`, `license` e `attribution` retirados diretamente do `extmetadata` obtido, marcadas com a tag `trivial-negative`.

**Duas falhas dignas de registro**, ambas apanhadas por verificação em vez de descartadas por suposição:

- Redigitar à mão três das cinco URLs de download (em vez de usar o campo `url` da própria API) produziu três arquivos que na verdade eram HTML genérico da Wikimedia — não JPEGs — apesar de baterem em tamanhos idênticos byte a byte nos três (uma bandeira vermelha em si: três assuntos *diferentes* não deveriam baixar com o *mesmo* tamanho). Inspecionar o conteúdo mostrou HTTP 429 "Too many requests", não um bug de codificação de URL — cinco requisições disparadas em sequência excederam um limite de taxa. Corrigido re-buscando com atrasos entre requisições, e preferindo daqui em diante a string de URL fornecida pela API em vez de reconstruí-la.
- O campo `license_note` de `manifest.json`, até esta seção, ainda afirmava "no third-party or stock imagery is included here" — verdadeiro quando escrito, falso no momento em que conteúdo do Commons foi adicionado. Atualizado para descrever os dois modelos de licença que o manifesto agora carrega lado a lado.

Total do manifesto: 45 entradas (40 aéreas + 5 cotidianas), confirmado contra o diretório com `Manifest.verify_matches_directory()`, e confirmado indexando corretamente fim a fim via `seed_demo.py` contra o container PostgreSQL local (45 linhas, correspondência exata).

Uma suposição de teste precisou de correção quando essas entradas chegaram: `test_every_manifest_entry_is_relevant_for_at_least_one_query` (§10) asseverava que toda entrada do manifesto aparece na lista `relevant` de alguma consulta. Negativos triviais devem, por design, ser irrelevantes para *toda* consulta — forçar um deles em uma lista `relevant` para satisfazer o teste contradiria seu propósito. Estreitado para excluir entradas marcadas como `trivial-negative`, com o raciocínio registrado no próprio teste.

---

## 10. Estratégia de Testes

| Nível | Corpus | Exige PostgreSQL | Situação |
|---|---|---|---|
| Validação de esquema do manifesto | — | Não | Feito — 24 testes |
| Descoberta, filtragem, casos difíceis | Gerado (`hard_cases.py`) | Não | Feito — 20 testes |
| Correção da materialização (cópia + carimbo de mtime) | — | Não | Feito — 6 testes |
| Indexação completa + persistência | Demo (materializado) | Sim | Feito — 4 testes |
| Skip incremental na segunda execução | Demo (materializado) | Sim | Feito |
| Geração de corpus de escala (só estrutura, contagens pequenas) | Gerado (`scale_corpus.py`) | Não | Feito — 5 testes; a execução do benchmark de 100k em si permanece fora de escopo (§14) |
| Validade estrutural de `queries.json` | Demo + `queries.json` | Não | Feito — 6 testes |
| Qualidade de recuperação | Demo + `queries.json` | Não | Construído, **dormente — xfail** até existir um `EmbeddingModelPort` real |

Testes apoiados em banco usam a fixture `db_session` existente de `conftest.py`, que isola cada teste em um SAVEPOINT desfeito no teardown. Nenhum teste pode escrever no banco de desenvolvimento fora dessa transação.

Testes de nível unitário não podem exigir um corpus de demonstração materializado; é para isso que serve o corpus gerado por `hard_cases.py` (§9.4 explica por que um `fixture_corpus.py` separado também não foi construído).

---

## 11. Alternativas Consideradas

**Uma pasta plana de JPEGs commitados.** Rejeitada. Não alcança escala de benchmark sem inchar o histórico do git, e não carrega expectativas, então os testes precisariam fixar nomes de arquivo e se afastariam silenciosamente conforme a pasta mudasse.

**Gerar tudo, commitar nada.** Rejeitada. Imagens sintéticas não sustentam avaliação semântica, e a demonstração não tem valor como demonstração se as imagens forem quadrados de ruído. A divisão de §2 mantém a geração onde geração funciona.

**Baixar imagens em tempo de teste a partir de uma URL remota.** Rejeitada. Introduz dependência de rede e não determinismo na suíte de testes, e viola a premissa local-first do projeto.

**Manifestos em YAML.** Rejeitada — ver §5.1.

**Adiar `queries.json` até existir um modelo real.** Rejeitada — ver §7.3.

---

## 12. Riscos

| Risco | Mitigação |
|---|---|
| O manifesto se afasta dos arquivos em disco | Um teste garante que o manifesto e a árvore de diretórios descrevem exatamente o mesmo conjunto, nas duas direções |
| Imagens commitadas incham o tamanho do repositório | Teto rígido: ~1024 px no lado maior, <200 KB por imagem, imposto por teste |
| Fixtures de `hard_cases/` são dependentes de plataforma (nomes acentuados no Windows) | Geradas em vez de commitadas; verificado funcionando no ambiente Windows de desenvolvimento em que este RFC foi implementado |
| A normalização de §7.4 quebra testes existentes | Esperado e contabilizado em §9; apenas testes que asseveram valores literais de vetor são afetados |

---

## 13. Questões em Aberto

1. ~~`hard_cases/` deve ser commitado ou gerado?~~ **Resolvido: gerado.** Todo caso se mostrou sintetizável com Pillow, incluindo os casos CMYK e de orientação EXIF que motivaram a dúvida, então nada precisou ser commitado. A saída é determinística byte a byte entre execuções. Ver §6.2.
2. ~~O gerador de corpus de escala deve produzir JPEGs reais ou arquivos vazios com metadados plausíveis?~~ **Resolvido: JPEGs reais mínimos.** `scale_corpus.py` gera JPEGs minúsculos (8×8 px), porém genuinamente válidos e abríveis pelo Pillow, fragmentados em subdiretórios para evitar a degradação de sistema de arquivos que um único diretório de 100k entradas causaria em NTFS. Arquivos vazios teriam sido mais rápidos de produzir, e continuam suficientes para o `FakeEmbeddingModel` de hoje, que nunca abre pixels — mas o caso difícil de arquivo de zero bytes (§6.2) já demonstra concretamente o modo de falha dessa escolha: ele indexa limpamente hoje e espera-se que falhe assim que um modelo real decodificar pixels. Usar arquivos mínimos-porém-válidos significa que o corpus de escala não precisará ser regerado no dia em que um modelo de embedding real chegar.

Nenhuma das duas questões bloqueia o resto do RFC; ambas estão fechadas.

---

## 14. Fora de Escopo

- Integração de um modelo de embedding real.
- Busca por similaridade vetorial e um endpoint `/search` funcional.
- A execução do benchmark de 100.000 imagens em si. A correção de normalização de §7.4 removeu a razão pela qual ele seria *sem sentido*, e `scale_corpus.py` (§9) fornece o gerador, mas executar e reportar o benchmark medido é trabalho separado — `ARCHITECTURE.md` §22 reserva `scripts/benchmark.py` para isso.
- Qualquer mudança na derivação de `ImageId` (§7.1).
- Entidade, tabela ou persistência de `Collection`.
