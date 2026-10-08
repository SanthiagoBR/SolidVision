# RFC-032 — Geolocalização e Busca por Proximidade

**Status:** Implementado
**Depende de:** RFC-020 (metadados incrementais, `NULL` é desconhecido), RFC-025 (busca semântica), RFC-026 (API de busca), RFC-027 (`SearchFilters`, dispositivos), RFC-028 (extração de EXIF na varredura, cadeia de fonte, escrita condicional), RFC-030 (resposta da busca), RFC-031 (API para a UI)
**Migration:** sim — `images.latitude`, `images.longitude`, `images.position_source` (`6d77379a36a1`, `down_revision = 'f4b9e2d7c615'`)
**Medição:** `experiments/rfc-032-geolocation/measure_gps_cost.py` (§2.2, §2.3, §4.2) e `experiments/rfc-032-geolocation/planner_check.py` (§6.1, §6.2, §7) — medidos, com `.log` ao lado de cada script. Antes do documento: `experiments/rfc-032-geolocation/pilot_exif_gps_check.py` (+ `.log`), sobre 40 arquivos reais do acervo, com script independente do projeto (§2.3).

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir* durante a implementação e devia ser escrito de volta aqui depois. **Isso foi feito:** cada `TBM` virou um número, com a escala e as condições em que foi medido, à vista da frase original. Onde a medição não respondeu à pergunta inteira — a cobertura de GPS do acervo **inteiro** (§2.3: só a pasta disponível foi lida) e a leitura a frio de um disco externo (§8.1) — o que ficou por medir está dito, em vez de ser preenchido por dedução. Onde a medição mudou uma decisão ou corrigiu o próprio método de medir, a correção está registrada em §14.

---

## 1. Contexto

O pedido, nas palavras do usuário: *"a pesquisa é muito básica para o acervo em que vai ser utilizado"*, e o que ele quer é *"pesquisar fotos por localização, com mapa no frontend, para poder pesquisar por localização próxima"*. Junto veio a ressalva que define o desenho inteiro: *"as fotos de drone DJI já possuem o EXIF que mostra as coordenadas do lugar, mas saber a exata coordenada é muito difícil em um cenário real."*

O RFC-028 §11 já havia nomeado este trabalho e, mais importante, a condição para ele existir:

> *"extração de GPS — está no mesmo cabeçalho e, para fotografia aérea, habilita 'fotos tiradas perto daqui', um filtro tão forte quanto data. Não entra aqui porque nada consumiria coordenadas ainda, e um dado indexado sem leitor é o `minimum_similarity` do RFC-025 §13.2 de novo."*

**A condição está satisfeita por este RFC, e só por ele:** o leitor chega junto com o dado — o filtro espacial na busca (§6) e o endpoint de agregação sem o qual um mapa não pode ser desenhado (§7). Extrair coordenadas sem os dois seria repetir exatamente o erro que o RFC-028 recusou cometer.

O estado atual da busca são três eixos: texto (RFC-025), dispositivo (RFC-027) e intervalo de data (RFC-028). Este RFC acrescenta o quarto, e é o mais forte dos quatro para o acervo em questão — §2.1 mostra por quê.

## 2. Problema

### 2.1 O CLIP não sabe *onde*, e para este acervo isso é o que mais importa

A busca semântica compara conteúdo. Duas fotos aéreas de propriedades rurais com um lago, tiradas a 200 km uma da outra, são vizinhas no espaço de embeddings — é isso que o RFC-023 escolheu o modelo para fazer. Nenhum ajuste de modelo, nenhum *fine-tuning* de domínio e nenhuma reformulação de consulta transformam *"perto de onde eu voei na fazenda do cliente"* em uma consulta que o CLIP possa responder, porque a resposta não está nos pixels: está no cabeçalho do arquivo.

A medição-piloto de §2.3 mostra a forma do problema com dados reais: **40 fotos distribuídas por uma caixa de 59,6 km × 47,0 km, em 24 agrupamentos distintos de ~100 m.** São poucos locais, visitados em voos distintos, e do alto eles se parecem. Localização é o eixo **ortogonal** ao que o modelo sabe — o mesmo argumento que o RFC-028 §8.1 fez para data (*"eu sei que é de 2018"*), com uma diferença de grau: a data de um voo o usuário às vezes esquece, mas **o lugar é o que ele contratou para fotografar**.

### 2.2 A coordenada exata não é o que o usuário tem — nem o que o arquivo diz

Duas coisas distintas se escondem na ressalva do pedido, e as duas empurram na mesma direção.

**Primeira: o usuário não sabe coordenadas.** Ele sabe reconhecer um lugar num mapa. Uma interface que peça latitude e longitude para buscar está pedindo o dado que o usuário não tem, para encontrar o dado que ele tem. Logo a consulta não pode ser um ponto: tem de ser **um ponto e um raio** — um clique no mapa e uma distância —, e o raio é parte da pergunta, não um detalhe de implementação.

**Segunda: a coordenada do EXIF não é a coordenada do que aparece na foto.** O GPS registra onde a *aeronave* estava. Com a câmera apontada obliquamente, o que foi fotografado fica longe do ponto registrado. Isso não é especulação: os arquivos de §2.3 trazem, no XMP, `RelativeAltitude="+28.20"` e `GimbalPitchDegree="-17.90"` na mesma foto. Se a convenção do gimbal for 0° no horizonte (**TBM** — não verificado contra documentação da DJI), o ponto no solo fica a `28,20 / tan(17,9°) ≈ 87 m` do ponto gravado. A ordem de grandeza — **dezenas a centenas de metros** — é o que o raio tem de absorver.

> **Medido na implementação.** A convenção **0° = horizonte, −90° = para baixo** foi conferida nos próprios arquivos, não deduzida: o quadro com −17,9° (`DJI_0013`) mostra o horizonte junto à borda superior; o de −6,7° (`DJI_0028`) é quase nivelado, com céu. A mesma convenção aparece na documentação de terceiros que lê esses campos (fórum do Litchi); documentação oficial da DJI não foi encontrada. E a medição achou o que a frase não previa: **a câmera FC3682 grava `+0.00` em todos os seus 20 quadros**, inclusive em quadros que visivelmente olham para baixo (`DJI_0122`, `DJI_0434`) — o campo é um placeholder nesse modelo. Esses quadros ficam fora da distribuição do deslocamento, contados à parte. Sobre os 20 quadros que gravam o ângulo (FC300S e FC300SE): deslocamento **mínimo 24 m, mediana 96 m, p90 243 m, p95 274 m, máximo 358 m** (`measure_gps_cost.log`). A ordem de grandeza da frase original se confirma — dezenas a centenas de metros.

A consequência de desenho é forte e é melhor dizê-la agora: **um raio pequeno é uma máquina de resposta errada.** Um filtro de 50 m sobre coordenadas de aeronave esconde, com confiança total e sem erro nenhum, exatamente a foto que o usuário está procurando. O raio mínimo que a interface oferece é, por isso, uma decisão de produto e não de UI (§6, `MIN_RADIUS_M`).

### 2.3 A informação já está lá — medido, inclusive o que não está

Antes de escrever este RFC, um script independente do projeto leu 40 arquivos reais do acervo. **Esta é a única seção com números medidos, e ela existe porque a pergunta "esses dados do EXIF são realmente usáveis?" precede todo o resto.**

**Condições** (`experiments/rfc-032-geolocation/pilot_exif_gps_check.py`, saída completa no `.log` ao lado): 40 arquivos `.JPG` de uma pasta do acervo fornecida pelo usuário; Python 3.12.9, Pillow 12.3.0, Windows 10; script autônomo, sem imports do projeto, lendo somente cabeçalho via `Image.open()` preguiçoso mais 2 MB de bytes crus por arquivo para procurar XMP; cache de sistema de arquivos frio. O script foi validado contra cinco arquivos sintéticos — EXIF válido em `S`/`W`, placeholder `0,0`, XMP sem EXIF, sem GPS e um `.HEIC` que o Pillow não abre — **antes** de ser apontado para os arquivos reais.

| o que foi medido | resultado |
| --- | --- |
| câmeras | DJI `FC3682` (20 arquivos), DJI `FC300S` (10), DJI `FC300SE` (10) |
| GPS do EXIF válido | **40 de 40 (100%)** |
| placeholder `0,0` | **0** |
| coordenada fora de faixa | **0** |
| GPS ausente | **0** |
| GPS no XMP | **0 de 40** — ver abaixo |
| caixa envolvente | lat `-26.64800`..`-26.22519`, lon `-49.41636`..`-48.81620` → **59,6 km × 47,0 km** |
| células distintas de ~100 m | **24**; as 5 mais cheias concentram **45%** das posições |
| exemplo verificável | `DJI_0013.JPG` → `-26.321406, -48.816307` |

**Um resultado negativo, que mata uma suposição deste RFC antes de ela custar código.** A primeira versão da análise supôs que a DJI grava a posição também em XMP (namespace `drone-dji`), e que o XMP serviria de fonte secundária. **Os 40 arquivos têm pacote XMP com o namespace `drone-dji`, e nenhum tem latitude ou longitude.** Os campos presentes são `AbsoluteAltitude`, `RelativeAltitude`, `GimbalRoll/Yaw/PitchDegree`, `FlightRoll/Yaw/PitchDegree`, `FlightX/Y/ZSpeed`, `CamReverse` e `GimbalReverse`. Logo: **não existe fallback de XMP neste RFC** (§9). A suposição foi barata de matar porque foi verificada com 40 arquivos em vez de implementada.

**O custo medido (109 ms por arquivo) não é o custo de produção**, e registrá-lo sem essa ressalva seria pior que não medir: foi cache frio e o script lê 2 MB por arquivo procurando XMP, que §4.2 não vai fazer. O RFC-028 §6 mediu a leitura de cabeçalho em **0,39–0,40 ms** por arquivo com cache quente, e é esse o caminho que §4.2 reusa. O custo real da extração de GPS é **TBM** e tem de ser medido com o script do projeto.

> **Medido na implementação** (`measure_gps_cost.py`, com `read_exif_facts()` do projeto, sobre a **mesma pasta de 40 arquivos** — a única parte do acervo disponível nesta máquina): **40 de 40 com `exif_gps`**, 40 de 40 com `exif_original`, nenhum `0,0`, mediana de 1,9 ms por arquivo na primeira passada e 1,9 ms na segunda (arquivos de 5–7 MB, já em cache). O custo de produção está em §4.2. **A fração do acervo inteiro com coordenada continua não medida**: o script aceita `--root` e `--sample N` para uma amostra aleatória, e falta apontá-lo para os discos. A ressalva acima, de que 40 arquivos de uma pasta não são o acervo, vale sem alteração.

**O que a amostra não prova**, dito em vez de deduzido:

- **40 arquivos de uma pasta não são o acervo.** A pasta foi escolhida pelo usuário; os casos ruins podem estar precisamente fora dela. A composição declarada do acervo é **~95% DJI e ~4% smartphone**, e nenhum smartphone aparece na amostra. O número que importa — a fração do acervo inteiro com coordenada válida — é **TBM**;
- **`0,0` não apareceu, e a regra continua existindo.** Um drone sem fixo de satélite grava zeros ou nada, e isso é o tipo de caso que uma amostra pequena não contém e um acervo de anos contém. A regra de §4.3 é mantida por argumento, não por frequência observada;
- **DNG e HEIC não apareceram.** `SUPPORTED_IMAGE_EXTENSIONS` tem `.jpg`, `.jpeg`, `.png`, `.tiff`, `.bmp` e `.webp`. Se os ~4% de smartphone forem iPhone, eles estão em HEIC e **não estão indexados hoje** — um defeito que não é deste RFC e que ele não esconde (§11).

### 2.4 "Filtro mais robusto" significa mais eixos, não uma linguagem de consulta

O pedido falou em *"sistema de filtragem muito mais robusto"*, e há duas leituras. A primeira é uma linguagem de consulta genérica — predicados compostos, `AND`/`OR`, campos arbitrários — enviada pelo cliente. A segunda é **mais eixos nomeados**, cada um com sua cláusula, seu parâmetro e sua contagem de excluídos.

Este RFC escolhe a segunda, e a razão é a mesma que mantém `SearchFilters` auditável desde o RFC-027: cada campo nomeado tem um `WHERE` que se pode ler, um plano que se pode medir e um teste de contrato que obriga os três repositórios a concordar. Uma linguagem de consulta move a semântica do filtro para dentro do cliente, e com ela a responsabilidade de não construir a consulta que devolve nada em silêncio. O mecanismo já existe e já foi estendido uma vez (RFC-028); **este RFC o estende pela segunda vez, o que é a evidência de que a estrutura serve** — a mesma prova que o RFC-024 deu ao `IndexMetadata` do RFC-020.

Os próximos eixos ficam baratos depois deste, e §10 lista por que nenhum deles entra aqui.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Novas colunas | `images.latitude`, `images.longitude` — `double precision`, nulas juntas (§5) |
| Nova coluna | `images.position_source` — de onde a posição veio, `String` e não `ENUM` nativo (§4.4) |
| Fonte | **Só EXIF GPS**, IFD `0x8825`. Sem fallback de XMP, medido em §2.3 |
| Vocabulário | `position` / `latitude` / `longitude`; **nunca** `location`, que no projeto já significa *onde o arquivo está* (§4.5) |
| Onde é extraída | Na varredura, na **mesma abertura de arquivo** que já lê a data (§4.2) |
| Efeito na decisão incremental | **Nenhum.** A escada de custo do RFC-020 fica intacta (§8) |
| Escrita para linhas puladas | **Condicional**, só quando `position_source` é `NULL`, nos dois ramos que pulam inferência (§8) |
| Backfill | **Um só**, que escreve data e posição na mesma leitura; supersede `capture_date_backfill` (§8.1) |
| Altitude, rumo, gimbal | **Não guardados.** Nada os consome (§10) |
| Correção para o ponto fotografado | **Não feita.** A posição gravada é a da aeronave, e o raio absorve o deslocamento (§2.2, §10) |
| Filtro na busca | `SearchFilters.taken_within: GeoCircle \| None` (§6) |
| Unidade | **Metros, em toda a pilha** — domínio, SQL e HTTP (§6) |
| Distância | Haversine exata, com caixa envolvente só como pré-filtro descartável (§6) |
| Extensão de banco | **Nenhuma.** Sem PostGIS, sem `earthdistance` (§5.1) |
| Índice espacial | **Não criado.** B-tree em `latitude` e GiST sobre `point(longitude, latitude)` medidos; nenhum atendeu ao critério (§6.1) |
| Mitigação do resultado curto sob HNSW | **Decidida aqui**, com critério declarado antes do número (§6.1): **critério não atingido por nenhuma configuração, nenhuma mitigação aplicada** — o resultado curto continua possível, como o contrato diz desde o RFC-025 |
| `MIN_RADIUS_M` | **300 m**, medido: p95 do deslocamento aeronave–solo, arredondado para cima em 50 m (§6) |
| `MAX_MAP_CELLS` | **2000**, medido: maior candidato cuja pior resposta cabe em 256 KB (§7) |
| Contagem de excluídos sem posição | `excluded_unknown_position` na resposta (§6.2) |
| Endpoint para o mapa | `GET /api/v1/images/map`, agregação por célula sobre o universo dos filtros (§7) |
| Mapa no frontend | **Não-objetivo**, com o contrato que ele consome definido aqui (§11) |

## 4. A extração

### 4.1 O GPS mora em outra IFD, e este projeto já pagou por essa lição

As coordenadas **não** estão na sub-IFD de EXIF (`0x8769`), de onde o RFC-028 lê `DateTimeOriginal`. Estão na IFD apontada pela tag `0x8825` (`GPSInfo`) da IFD0. O docstring de `EXIF_IFD_POINTER`, em `exif_capture_date.py`, registra o que acontece quando isso é ignorado:

> *"código escrito dessa forma passa contra fixtures feitas à mão que põem a tag no lugar errado e devolve `unknown` para um rolo de filme inteiro."*

A mesma armadilha existe uma IFD ao lado, e a defesa é a mesma: **os testes de extração usam a estrutura real de um arquivo DJI**, com a IFD de GPS no lugar onde ela realmente fica, e não um dicionário montado na IFD0. Sem isso, a extração passa em todo teste e devolve `unknown` para 95% do acervo.

Dentro da IFD de GPS, uma coordenada são **quatro** valores, não dois: `GPSLatitude` (1) e `GPSLongitude` (4) são triplas de racionais grau/minuto/segundo, e `GPSLatitudeRef` (2) e `GPSLongitudeRef` (3) são `N`/`S` e `E`/`W`, que carregam o **sinal**. Perder a referência não produz erro: produz uma foto do Paraná no hemisfério norte. O piloto de §2.3 já exercitou os dois sinais negativos (`S`, `W`).

### 4.2 Uma abertura, dois fatos

A varredura já abre todo arquivo candidato para ler a data. Abrir de novo para ler o GPS **dobra o custo medido** — 0,39 ms viram ~0,8 ms por arquivo, ~40 s viram ~80 s por 100.000 arquivos — para reler exatamente os mesmos bytes de cabeçalho.

**Decisão: uma leitura, dois fatos.** `exif_capture_date.py` passa a expor uma função que abre o arquivo uma vez e devolve as duas coisas; `read_capture_date()` continua existindo como o leitor da data, e a nova `read_exif_facts()` é quem a varredura chama. As três saídas que o RFC-028 §4.3 estabeleceu são **preservadas por fato**, e isto é o detalhe que um refactor apressado perde: um arquivo que não pôde ser lido devolve *não examinado* para os dois fatos, enquanto um arquivo lido sem GPS e com data devolve `unknown` para a posição e uma data para a data. **As duas fontes são independentes e nenhuma é derivada da outra.**

`DiscoveredImageFile` ganha `position: Position | None`, ao lado de `capture_date`, com a mesma semântica de `None` (*não examinado*). A extração fica atrás de `Settings.extract_gps`, no molde de `extract_capture_date`: desligada significa "não examinado", nunca "sem posição".

Um teste conta aberturas de arquivo com um espião, porque "uma abertura" é uma afirmação de custo e as afirmações de custo deste projeto são verificadas (§13).

> **Medido na implementação** (`measure_gps_cost.py`; Python 3.12.9, Pillow 12.3.0, Windows 10, Intel família 6 modelo 158; JPEGs com sub-IFD Exif e IFD de GPS reais, 3 repetições após aquecimento, cache quente). Mediana por arquivo, em 3.000 quadros de 1920×1080 e 200 de 4000×3000:
>
> | leitura | 1920×1080 | 4000×3000 |
> | --- | --- | --- |
> | só a data (`read_capture_date()`, o caminho do RFC-028) | 0,506 ms | 0,708 ms |
> | **data e posição, uma abertura** (`read_exif_facts()`) | **0,570 ms** | **0,555 ms** |
> | data e posição, duas aberturas (o desenho recusado) | 1,071 ms | 0,975 ms |
>
> Ler a posição na mesma abertura custa **o que o ruído da medição deixa ver: entre −0,14 e +0,06 ms por arquivo** conforme o corpus; uma segunda abertura custaria **+0,36 a +0,57 ms**, quase o dobro, como a frase acima previa. Projetado para 100.000 arquivos: **~57 s** de leitura de EXIF por varredura completa com os dois fatos (o caminho só-data mediu ~71 s na mesma sessão; a diferença é ruído, não ganho), contra **~107 s** com duas aberturas. A máquina mediu o caminho só-data mais devagar que no RFC-028 (0,51–0,71 ms contra 0,39–0,40 ms): mesma máquina, outra sessão — por isso a comparação é sempre dentro da mesma execução. O espião confirma **3.000 chamadas de `Image.open()` para 3.000 arquivos** descobertos com os dois fatos ligados; com os dois desligados, zero (o arquivo nem é aberto). O mesmo limite do RFC-028 vale aqui: **leitura a frio de disco externo não medida**.

### 4.3 O que conta como "nada utilizável"

Mesma estrutura do RFC-028 §4, aplicada a um dado com mais modos de falha:

| caso | resultado |
| --- | --- |
| IFD de GPS ausente (a maioria das fotos sem GPS) | `unknown` |
| latitude presente e longitude ausente, ou vice-versa | `unknown` — **nunca meia posição** |
| referência (`N`/`S`/`E`/`W`) ausente ou irreconhecível | `unknown` — não se adivinha hemisfério |
| racional com denominador zero, `NaN`, ou tripla de tamanho errado | `unknown` |
| `0,0` exato — o placeholder de "sem fixo de satélite" | `unknown`, reconhecido **por nome** |
| `\|latitude\| > 90` ou `\|longitude\| > 180` | `unknown` |
| arquivo não pôde ser lido agora | **`None`** — não examinado, a próxima varredura tenta de novo |

O `0,0` segue a mesma regra do `0000:00:00 00:00:00` do RFC-028 §4: reconhecido explicitamente, e não deixado para uma validação de faixa rejeitar por acidente. `0°/0°` é um ponto válido no Golfo da Guiné — a "Ilha Null", onde erros de coordenada se acumulam em bases de dados do mundo inteiro —, e "todas as fotos sem fixo de GPS num ponto no oceano" é o tipo de resposta que parece certa até alguém clicar nela. O limiar é `|lat| < 1e-4 e |lon| < 1e-4` (~11 m do ponto zero), e não igualdade exata, porque um fixo parcial grava zeros com ruído.

Nada aqui levanta exceção para a varredura: um JPEG corrompido em 100.000 não pode terminar um scan (RFC-028 §13).

### 4.4 Três estados, outra vez

| valor de `position_source` | significado |
| --- | --- |
| `NULL` | a linha **nunca foi examinada** — é anterior a este RFC, ou foi varrida com `extract_gps` desligada |
| `'unknown'` | foi examinada e o arquivo não tem posição utilizável (`latitude` e `longitude` são `NULL`) |
| `'exif_gps'` | a posição veio da IFD de GPS |

A distinção `NULL` / `'unknown'` é o que mantém o re-scan barato, pelo mesmo mecanismo do RFC-028 §4.2: a varredura escreve só linhas `NULL`, e depois da primeira passada um acervo inalterado não custa escrita nenhuma. Fotos sem GPS são numerosas justamente no acervo que este RFC quer servir, e se "examinado, sem posição" fosse `NULL` elas seriam relidas e reescritas para sempre.

`PositionSource` é um `StrEnum` de Domain persistido como `String`, com `precedence` para a regra de não-rebaixamento do backfill (§8.1). Dois membros futuros já têm nome e **não** entram agora: `'manual'` (o usuário marca o ponto no mapa) e `'subject_estimated'` (§10). Acrescentá-los não pode custar migration.

### 4.5 O nome `location` já está ocupado

No codebase, *location* significa **onde o arquivo está**: `ResolveImageLocationUseCase`, `LocatedImage`, `resolve_image_location.py` — tudo isso é ponto de montagem e caminho absoluto, entregue pelo RFC-030. Chamar a coordenada geográfica de `location` criaria duas perguntas com um nome, numa base de código onde `absolute_path` ser `None` já significa uma coisa precisa.

**Decisão de vocabulário, válida para colunas, value objects, campos, parâmetros e testes:** a coordenada é `position` (ou `latitude`/`longitude`, que não são ambíguas). Nada neste RFC se chama `location`, `ImageLocation` ou `location_source`. Um revisor que leia *location* neste projeto não deve precisar perguntar qual.

## 5. Onde fica guardado

```sql
ALTER TABLE images
    ADD COLUMN latitude        double precision,
    ADD COLUMN longitude       double precision,
    ADD COLUMN position_source varchar;

ALTER TABLE images
    ADD CONSTRAINT ck_images_position_pairing
        CHECK ((latitude IS NULL) = (longitude IS NULL)),
    ADD CONSTRAINT ck_images_latitude_range
        CHECK (latitude IS NULL OR (latitude BETWEEN -90 AND 90)),
    ADD CONSTRAINT ck_images_longitude_range
        CHECK (longitude IS NULL OR (longitude BETWEEN -180 AND 180));
```

`double precision` porque 1e-7 grau é ~1 cm e um `float8` tem ~15 dígitos significativos: a precisão perdida é ordens de magnitude menor que a do GNSS do drone, e muitas ordens menor que o deslocamento de §2.2. `NUMERIC` custaria aritmética decimal para guardar ruído.

**Duas colunas, não um `point`.** A entidade tem dois campos, a resposta publica dois números, e o `CHECK` de pareamento diz em SQL a mesma regra que `validate_capture_fields()` diz em Python. Um `point` guardaria o par atomicamente e, em troca, tornaria `latitude` um `(images.position)[1]` em toda consulta, log e migration. O índice de §6.1, se existir, é uma **expressão** sobre as duas colunas — não precisa que elas sejam um `point`.

Os três `CHECK`s são a parte da invariante que nenhum escritor futuro pode contornar. A validação de Domain continua existindo e continua sendo a que produz mensagem legível; o `CHECK` é o que impede que um `UPDATE` manual, um backfill mal escrito ou um teste com fixture ruim gravem meia posição.

### 5.1 Nenhuma extensão de banco

| opção | por que não |
| --- | --- |
| **PostGIS** | A imagem atual é `pgvector/pgvector:pg17` e **não** traz PostGIS. Adotá-la obriga a construir e distribuir uma imagem com as duas extensões, e o alvo de distribuição é processo nativo no Windows com instalador (RFC-029 §14). É peso no instalador para responder a uma pergunta que `sin`/`cos` respondem |
| **`cube` + `earthdistance`** | São contrib e provavelmente já estão disponíveis — `SELECT * FROM pg_available_extensions WHERE name IN ('cube','earthdistance')` é a verificação, e ela é **TBM**. Ainda assim: um `CREATE EXTENSION` novo no caminho de migration, para substituir uma expressão de seis linhas que não depende de nada |
| **Coluna de geohash ou célula H3** | Coluna derivada que precisa ser mantida em sincronia com duas outras. §7 agrega com `round()`, que não precisa dela |

> **Verificado na implementação:** na imagem de desenvolvimento (PostgreSQL 17.10, `pgvector/pgvector:pg17`), `cube` 1.5 e `earthdistance` 1.2 estão **disponíveis e não instalados**; PostGIS **não está disponível**. Disponibilidade no PostgreSQL embarcado do instalador Windows não foi verificada — e, pela decisão abaixo, não precisa ser.

A decisão é **zero dependência nova**: caixa envolvente sobre colunas comuns, distância exata por haversine em SQL (§6). Se algum dia a pergunta virar *"dentro do limite do município"*, aí PostGIS ganha por mérito próprio — polígonos não se fazem com `sin` e `cos` — e a troca fica registrada aqui como a condição que a justifica.

## 6. O filtro

`SearchFilters` ganha o **terceiro** campo, e `is_empty()` é atualizado junto — era para isso que ele existia:

```python
@dataclass(frozen=True)
class SearchFilters:
    device_ids: frozenset[DeviceId] = frozenset()
    captured_between: DateRange | None = None     # RFC-028
    taken_within: GeoCircle | None = None         # este RFC

    def is_empty(self) -> bool:
        return (
            not self.device_ids
            and self.captured_between is None
            and self.taken_within is None
        )
```

Os três campos restringem independentemente e se combinam com `AND`. Um `SearchFilters()` vazio continua produzindo, caractere por caractere, a consulta que o RFC-025 entregou — afirmado sobre o **SQL compilado** e não só sobre resultados, como `test_no_filter_and_an_empty_filter_emit_the_unfiltered_query` já faz.

**`GeoCircle`** é um value object de Domain: um centro (`Position`, com as mesmas validações de faixa do `CHECK`) e um raio em metros. Regras:

- **`radius_m > 0`, finito, obrigatório.** Um raio zero é recusado na construção, e nisso este RFC **diverge** do `DateRange` do RFC-028, que aceita `start == end` como intervalo vazio legítimo: um intervalo vazio é o que "de X até X" legitimamente calcula, enquanto um círculo de raio zero não é o que clique nenhum de mapa produz. Recusar é explícito; devolver vazio em silêncio não é;
- **`MIN_RADIUS_M`**, política de aplicação e não de domínio, é a resposta de §2.2: um raio abaixo dele é recusado com mensagem que **explica o deslocamento da aeronave**, em vez de aceitar um filtro que esconde a foto certa. O valor é **TBM** — depende da distribuição de `RelativeAltitude` e `GimbalPitchDegree` no acervo, que o script de §2.3 já sabe ler. Como `MAX_SEARCH_LIMIT`, mora na Application e é **injetado** pela raiz de composição a partir do `Settings`, nunca lido dele: a camada Application não importa `settings`, e `test_application_architecture.py` é quem garante isso (vale igualmente para `MAX_MAP_CELLS`, em §7);
- **sem limite superior.** "Em qualquer lugar do Brasil" é um pedido legítimo; o que um raio grande muda é seletividade (§6.1), não correção.

> **`MIN_RADIUS_M = 300 m`, medido.** Critério declarado no script antes do número: o **p95 do deslocamento aeronave–solo** (`RelativeAltitude / tan(|GimbalPitchDegree|)`) sobre os arquivos reais disponíveis, arredondado para cima até o próximo múltiplo de 50 m. Sobre os 20 quadros cuja câmera grava o ângulo, o p95 é 274 m (§2.2) → 300 m. Os 20 quadros da FC3682, cujo ângulo é um placeholder `0.00`, não entram — e o deslocamento real deles, que olham para baixo de 21 a 248 m de altura, não é calculável do arquivo. É política da Application, injetada a partir de `Settings.min_radius_m`; a mensagem de recusa nomeia o drone. Uma amostra maior do acervo pode movê-lo, e o script remede.

**A cláusula emitida tem duas partes, e a segunda é a que responde.** O pré-filtro é uma caixa envolvente — `latitude BETWEEN ... AND ...` e `longitude BETWEEN ... AND ...` — que existe só para um índice poder ser usado; a resposta é a distância haversine exata contra `radius_m`. A caixa é **descartável por correção**: o delta de longitude depende de `1 / cos(latitude)`, que explode perto dos polos, e um círculo que cruze o antimeridiano produz uma caixa invertida. Nos dois casos o pré-filtro é **omitido** e só a cláusula exata é emitida — mais lento, e certo. Para um acervo no Brasil isso nunca dispara, e é por isso mesmo que tem de estar escrito: o caso que nunca dispara é o que ninguém testa.

Nos dois doubles em memória, o predicado de `matches_filters()` precisa do `is None` **explícito**, exatamente como a data: no PostgreSQL uma comparação com `NULL` já é falsa e a imagem sem posição sai de graça; em Python, `haversine(None, ...)` levanta. **Posição desconhecida nunca casa com um círculo, por maior que ele seja** (RFC-020), e isso é fixado nos três repositórios por `test_an_unknown_position_never_matches`.

Na API:

```
GET /api/v1/images/search?q=telhado&near_lat=-26.3214&near_lon=-48.8163&radius_m=2000
```

Os três parâmetros são **tudo ou nada**: dois de três é um pedido malformado e responde **400** nomeando o que falta. Completar um raio ausente com um padrão seria inventar a intenção do usuário justamente na grandeza que §2.2 mostrou ser crítica. Coordenada fora de faixa, raio abaixo de `MIN_RADIUS_M` ou não positivo → **400** com mensagem de domínio; valor que não é número → **422** do FastAPI, como qualquer parâmetro malformado.

### 6.1 Espacial é o pior dos três filtros para o índice aproximado

O RFC-027 §9.1 estabeleceu que filtro sobre busca vetorial aproximada se mede, não se deduz. O RFC-028 §8.1 mediu e **confirmou** o regime ruim: entre 20% e 30% de seletividade sobre 20.000 imagens, o planejador mantém o HNSW, o pós-filtro descarta candidatos e a busca devolve **5 a 9 linhas de 10 pedidas, sem erro nenhum**.

| | dispositivo (027) | data (028) | **posição (este RFC)** |
| --- | --- | --- | --- |
| cardinalidade | baixa, estável | alta, contínua | alta, contínua |
| índice parcial por valor | viável | inviável | inviável |
| seletividade | conhecida de antemão | varia com o intervalo | varia com o raio **e com o lugar** |
| distribuição | uniforme entre discos | aproximadamente contínua | **fortemente agrupada** |

A última linha é a diferença que importa, e o piloto de §2.3 a mediu: **5 células de ~100 m concentram 45% das posições.** Um raio de poucos quilômetros em volta de um local muito fotografado não é um filtro seletivo — é um filtro que retém uma fração grande do acervo, e cai exatamente na faixa que o RFC-028 confirmou ser a ruim. **Para este filtro, o regime intermediário não é um canto: é o caso comum.**

Por isso a mitigação que o RFC-028 §8.1 registrou como risco aberto é **decidida aqui**, e o critério é declarado **antes** do número:

1. `experiments/rfc-032-geolocation/planner_check.py`, partindo do script do RFC-028, mede `EXPLAIN ANALYZE` sobre um corpus com posições **agrupadas** (não uniformes — uniformidade seria medir o problema que este acervo não tem), em seletividades que atravessem a faixa 10–50%, com e sem índice espacial, em plano customizado e genérico;
2. compara três configurações: baseline, `hnsw.iterative_scan = strict_order` e `ef_search` elevado apenas quando há filtro;
3. **critério de adoção:** a configuração mais barata que devolva `limit` de `limit` em toda a faixa medida, com p95 da busca filtrada **não pior que 2× a p95 da busca não filtrada**. Critério relativo e não um orçamento em milissegundos, porque a latência absoluta depende da máquina e este número vai ser lido em outra;
4. **aplicação:** `SET LOCAL` por transação, **só quando `filters.is_empty()` é falso**. A busca sem filtro continua emitindo a consulta do RFC-025 e preservando o comportamento que ele mediu — nenhuma mitigação nova pode mudar o caminho já validado;
5. `strict_order` é o ponto de partida em vez de `relaxed_order` porque a resposta publica `similarity` numa lista cuja ordem é parte do contrato (RFC-026).

Se o critério não for atingido, o resultado é registrado como tal e o contrato de `search_similar()` continua dizendo o que diz desde o RFC-025 — que um resultado curto é possível sob índice aproximado. **O que não é aceitável é adotar a mitigação sem medir, nem deixar o risco aberto por um terceiro RFC.**

O índice espacial é a segunda medição, com a mesma disciplina: B-tree em `latitude` contra GiST sobre `point(longitude, latitude)` — este último viável **sem extensão**, porque `point` e seu opclass GiST são do núcleo do PostgreSQL. **Só é criado se a medição o justificar** (RFC-028 mediu um B-tree em `captured_at`, não o justificou e não o criou — o precedente é esse, e não "índice é sempre bom").

**O filtro não é justificado por velocidade**, e repetir isso é mais honesto que torcer para que seja: o mecanismo real na faixa intermediária é adverso. A justificativa é utilidade — *"foi perto daqui"* é a informação mais forte que o usuário tem sobre uma foto aérea, e ela é ortogonal ao que o CLIP sabe (§2.1).

#### 6.1.1 Medido na implementação

**Condições** (`planner_check.py` + `.log`): PostgreSQL 17.10, pgvector 0.8.5, `hnsw.ef_search = 40`, `limit = 10`; **20.000 imagens** com vetores unitários aleatórios de 512 dimensões; 10% sem posição, 90% em torno de **40 locais** dentro da caixa do piloto, com pesos de Zipf (s = 0,85) e 60 m de dispersão por local — **os 5 locais mais cheios guardam 45,5% das posições** e 10% do corpus está a 82 m do mais cheio. O círculo é centrado no local mais cheio, com o raio buscado para cobrir 1%, 5%, 10%, 20%, 30%, 40%, 50% e 70% do corpus (raios de 21 m a 31 km). Cada célula da tabela: **20 vetores de consulta**, plano customizado (um plano por círculo) e plano genérico forçado (`PREPARE` + `plan_cache_mode = force_generic_plan`). A consulta medida é `PostgresImageRepository.search_statement()` — a da aplicação, com pré-filtro de caixa e haversine. Tudo numa transação desfeita.

**Latência = tempo de planejamento + execução no servidor** (`EXPLAIN (ANALYZE, TIMING OFF)`), e isso é uma correção do método (§14): a primeira execução cronometrou a ida e volta e achou um piso de ~47 ms em *toda* busca, causado pelo transporte de desenvolvimento — qualquer mensagem acima de ~8 KB para a porta publicada pelo Docker custa +43 ms nesta máquina, e um vetor de 512 floats serializado pelo pgvector tem ~10 KB.

**A busca sem filtro**: HNSW, **p95 = 3,0 ms** no servidor → teto do critério = **5,9 ms**.

**O resultado curto, reproduzido.** Sem índice espacial, plano customizado, linha de base: a **20%** (r = 6 km), **13 de 20 consultas voltaram curtas, com até 3 linhas de 10**; a **30%**, 3 de 20, com até 4. O RFC-028 mediu 5 a 9 linhas para data; para posição agrupada é pior, como §6.1 previa.

Pior p95 no servidor, em ms, por configuração, sobre toda a faixa e os dois tipos de plano (completo = 10/10 em todas as 320 consultas da configuração):

| índice espacial | linha de base | `strict_order` | `ef_search=100` | `ef_search=200` | `ef_search=400` |
| --- | --- | --- | --- | --- | --- |
| nenhum (como migrado) | **curto**, 136 | completo, 338 | completo, 225 | completo, 164 | completo, 127 |
| B-tree em `latitude` | **curto**, 137 | completo, 129 | completo, 286 | completo, 274 | completo, 317 |
| GiST em `point(longitude, latitude)` + `<@ box` | completo, 317 | completo, 177 | completo, 276 | completo, 202 | completo, 276 |

**Veredito: nenhuma configuração atinge o critério.** Toda mitigação torna todas as consultas completas; nenhuma fica abaixo de 2× a busca sem filtro, e a linha de base também não. O motivo não está no HNSW: nas seletividades baixas e sob plano genérico o planejador responde com **planos exatos** (varredura sequencial) — 23 a 65 ms a 5–10% em plano customizado, até ~340 ms em plano genérico a 50–70% —, e nenhum ajuste de `hnsw.*` toca esses planos. Onde o planejador usa o HNSW (20%–70%, plano customizado), `strict_order` custa pouco: p95 de 9 ms a 20%, 6 ms a 30%, 3 ms acima, contra 3 ms da linha de base **curta**.

**Consequência, pela regra deste RFC:** *"Se o critério não for atingido, o resultado é registrado como tal e o contrato de `search_similar()` continua dizendo o que diz desde o RFC-025 — que um resultado curto é possível sob índice aproximado."* É o que foi feito: **nenhum `SET LOCAL` foi adicionado**, e o docstring de `search_similar()` registra o veredito com estes números. O risco deixa de ser "aberto" — tem número e decisão —, mas a decisão é não mitigar **porque o critério, tal como escrito, não pode ser atingido por nenhuma configuração de HNSW**. Se o critério for reescrito para julgar só os planos que usam o índice (os únicos que a mitigação altera), `strict_order` o passa nos dados acima; essa reescrita é uma decisão de produto e fica para quem mantém este RFC, não para a implementação.

**Índice espacial: não criado.** Critério declarado no script: criar só se mudar o veredito, ou reduzir à metade o pior p95 da configuração escolhida, ou reduzir à metade a agregação do mapa a 100.000 linhas. Nenhum veredito mudou (o GiST tornou a própria linha de base completa, mas ela continua acima do teto); não há configuração escolhida; e no mapa o B-tree não ajuda: medido com aquecimento e alternância, **0,96×** na coleção inteira (o planejador nem o usa) e 0,82× numa *viewport* de 10 km (§7). Mesmo precedente do RFC-028.

### 6.2 Quantas fotos o filtro escondeu por não ter posição

A resposta da busca ganha `excluded_unknown_position`, no molde exato de `excluded_unknown_date` (RFC-028 §8.2): um `COUNT(*)` das imagens **com embedding**, sob **as demais cláusulas do filtro**, com `latitude IS NULL`.

Sem isso, o filtro espacial responde com a mentira mais cara do produto: *"não há foto desse lugar"*, quando a verdade é *"há 1.200 fotos sem coordenada e eu não olhei nenhuma delas"*. Com os ~4% de smartphone, mais toda exportação que remove EXIF, mais o acervo indexado antes deste RFC, este número começa **grande** — e é por isso que ele é parte da entrega e não trabalho futuro.

As mesmas três regras do RFC-028: é uma **segunda consulta**, com custo próprio (**TBM**); conta sobre **a tabela inteira sob os filtros**, não sobre a vizinhança que o HNSW explorou; e vem `null` — não `0` — quando não houve filtro espacial, porque `0` diria "o filtro não escondeu nada" e `null` diz "não houve filtro". Sem `taken_within`, o repositório **não é chamado**.

> **Medido:** o `COUNT(*)` sobre **100.000 linhas** (10% sem posição) custa **16–22 ms** de ida e volta sem índice (duas execuções), ~9–10 ms com o B-tree em `latitude` medido em §6.1 — que não foi criado. **Onde a regra "não é chamado" mora mudou** (§14): ela está no use case (`SearchImagesUseCase.count_hidden_by_unknown_position()`), e o método do repositório, `count_unknown_position()`, **não** responde 0 sem círculo — ao contrário do seu gêmeo de data —, porque o mapa (§7) faz exatamente essa pergunta sem círculo nenhum e precisa do mesmo número. E a contagem de data do RFC-028 passou a aplicar o círculo como "demais cláusulas", como esta seção pede para a contagem de posição.

## 7. O mapa precisa de um endpoint próprio

Um mapa não pode ser desenhado com o que existe hoje, e isso não é uma questão de frontend. A busca devolve no máximo `MAX_SEARCH_LIMIT = 100` acertos ranqueados: desenhar o mapa a partir deles mostraria onde estão as cem fotos mais parecidas com a consulta, o que não responde *"onde estão minhas fotos"*. E buscar 100.000 pontos para o cliente agrupar é mover o acervo pela rede para responder a uma pergunta que o `GROUP BY` responde no servidor.

```
GET /api/v1/images/map?min_lat=-27&min_lon=-49.5&max_lat=-26&max_lon=-48.5&precision=3
```

| aspecto | decisão |
| --- | --- |
| Agregação | `GROUP BY` sobre coordenadas arredondadas a `precision` casas (3 ≈ 100 m), devolvendo centro da célula e contagem |
| Universo | **o universo dos filtros, não o do ranking** — `embedding IS NOT NULL` mais os filtros não espaciais. O endpoint **ignora `q`** |
| Filtros aceitos | `device_id`, `captured_from`, `captured_to` — os mesmos da busca, pelos mesmos parâmetros |
| Teto | `MAX_MAP_CELLS` (**TBM**); acima dele a precisão é **reduzida** e a resposta ecoa `precision_applied`, no molde do `limit` que ecoa o valor aplicado (RFC-026) |
| Sem posição | a resposta inclui a contagem de imagens sem coordenada sob os mesmos filtros — o mesmo número de §6.2, no lugar onde o mapa precisa dele |
| Caixa inválida | `min > max` → **400**; caixa cruzando o antimeridiano → **400** neste RFC, declarado em §11 |

**Que o endpoint ignore `q` é a decisão que precisa estar escrita**, porque a alternativa parece melhor e não é: um mapa que refletisse a consulta textual só poderia agregar o top-K, e o top-100 de um índice de 100.000 não diz nada sobre distribuição geográfica. O mapa mostra **onde estão as fotos que os filtros permitem**; o ranking semântico continua sendo a busca. São duas perguntas e dois endpoints.

**Armadilha de implementação, concreta e verificável:** em `images.py`, `/search` está declarado antes de `/{image_id}`, e a rota nova tem de ficar **antes de `/{image_id}` também** — ao lado de `/search`, no topo do arquivo. O FastAPI casa rotas na ordem de declaração: depois de `/{image_id}`, `/images/map` seria lida como `image_id="map"` e responderia 422 para uma rota que existe. Um teste pede `GET /images/map` e exige 200 — não 422 (§13).

> **Implementado e medido.**
>
> - **Resposta:** `{"precision_applied", "cells": [{"latitude", "longitude", "count"}], "excluded_unknown_position"}`. A contagem sem posição não se restringe à caixa: foto sem coordenada não está em caixa nenhuma.
> - **Precisão:** opcional, padrão **3**; negativa é 422 (não é um número de casas); acima de **6** (~0,1 m, abaixo do que o GNSS sabe) é servida a 6 e ecoada em `precision_applied` — no mesmo molde da redução por teto.
> - **Redução:** uma casa decimal por vez, pedindo `MAX_MAP_CELLS + 1` células para saber se passou; em precisão 0 (células de 1°) não há nada mais grosso, e todas as células voltam mesmo acima do teto — no máximo 181 × 361 pela geometria. Exceder o teto ali é a falha honesta; esconder fotos para cumpri-lo não seria.
> - **Arredondamento:** `round(latitude::numeric, p)` no PostgreSQL — os 15 dígitos significativos do `double`, metade para longe do zero, em decimal. Os dois doubles em memória reproduzem exatamente essa regra (`round_to_cell()`), não o `round()` binário do Python; uma foto sobre a fronteira de célula cai na mesma célula nos três repositórios, e o teste de contrato fixa isso.
> - **`MAX_MAP_CELLS = 2000`**: o maior de 500/1000/2000/5000 cuja **pior resposta** cabe em 256 KB, a **54,8 bytes por célula** medidos no JSON real de `MapResponseSchema` (2000 → ~107 KB; 5000 → ~268 KB). É a segunda versão do critério, escrita pela implementação (este RFC deixou o número como TBM sem critério); a primeira também limitava tempo e não selecionou nada, porque o tempo que media era o da varredura, não o do teto (§14).
> - **Custo a 100.000 linhas** (90% com posição, agrupadas): o agrupamento da coleção inteira na precisão padrão é uma varredura sequencial e custou **~300 ms a ~1 s no servidor**, conforme a execução — três execuções nesta máquina, com variação grande entre elas; uma *viewport* de 10 km, **~60 a ~300 ms**. A resposta inteira (agrupamento + contagem sem posição) mediu 550–680 ms de ida e volta. Não é rápido, e está registrado como risco (§11): é o preço de não ter índice nem coluna derivada.
> - **Índice: não ajuda o mapa.** Medido com aquecimento e alternância (quatro rodadas): B-tree em `latitude` **0,96×** na coleção inteira (o planejador nem o usa — varredura sequencial nos dois casos) e **0,82×** na *viewport* de 10 km, onde é usado e fica mais lento. Uma segunda execução sem aquecimento mostrou uma "redução à metade" que era só a ordem das medições (§14).

## 8. A escada de custo do RFC-020 fica intacta

`latitude`, `longitude` e `position_source` **não entram** na escada `existe? → mtime → file_size → SHA-256 → embedding`. Posição não é sinal de mudança: uma foto cuja coordenada foi lida agora tem exatamente os mesmos pixels, e reindexá-la gastaria a operação mais cara do sistema por um metadado.

`plan_indexing()` não ganha parâmetro e não lê posição. O teste que fixa isso é escrito no nível em que ele pode falhar — sobre o `IndexMetadata` lido do banco, que passa a carregar `position_source` (necessário para a escrita condicional) — e é **verificado por mutação**: acrescentar `existing.position_source == candidate.image.position_source` à comparação tem de fazer testes falharem, como o RFC-028 §6.1 fez com `capture_source`.

A escrita segue a política do RFC-028 §6.2, em `position_to_write()`, função pura separada de `capture_date_to_write()` para que as duas decisões não se contaminem: a posição é escrita só quando `position_source` lida do banco é `NULL`, **nos dois ramos que pulam inferência** — `SKIP_UNCHANGED` e `REFRESH_METADATA`. Esquecer o segundo deixaria sem coordenada, para sempre, toda linha cujo arquivo foi copiado entre discos, que no acervo-alvo é a operação central (RFC-027 §2.3). O ramo `EMBED` grava a linha inteira a partir da entidade, posição incluída.

As posições de uma janela de prefetch são escritas num único `update_position_many()`, com degradação para escrita por linha — o mesmo trade do RFC-024 §7.2. `update_index_metadata()` **não** escreve `position_source`, pelo mesmo motivo que não escreve `capture_source`: um refresh zeraria o exame.

### 8.1 Um backfill, não dois

O RFC-028 §7 entregou `capture_date_backfill`. Um `position_backfill` irmão leria **os mesmos bytes dos mesmos arquivos numa segunda passada** — dois seeks por arquivo num HD externo mecânico, para preencher duas colunas que vêm do mesmo cabeçalho.

**Decisão: um backfill de EXIF**, que escreve data e posição na mesma leitura, substituindo `capture_date_backfill`. É uma mudança de comando de operador, declarada aqui e nos entregáveis em vez de escondida atrás de um alias que ninguém vai remover. Tudo o que o RFC-028 §7 estabeleceu é preservado:

- os dois modos, `--only-unknown` (padrão, idempotente) e `--force` (existe para um caso só: a extração melhorou);
- **`--force` nunca rebaixa uma fonte**, agora para duas cadeias independentes: um `exif_gps` sobrevive a um arquivo que hoje lê como `unknown`, e isso vale por fato — um arquivo pode perder a data e manter a posição;
- `--dry-run` lê tudo e não escreve nada, nem a linha do dispositivo; arquivo sem linha é `not_indexed` e **nenhuma linha é criada**;
- **o backfill não importa o modelo, nem transitivamente**, provado no grafo de imports num interpretador novo, com o guarda do guarda que o RFC-028 escreveu.

**Custo projetado:** a leitura de cabeçalho medida pelo RFC-028 (0,39 ms, cache quente) mais o parsing da IFD de GPS (**TBM**) — contra ~5 h de reindexação por disco. A leitura a frio de um disco externo real continua **não medida**, e continua sendo a próxima medição a fazer (RFC-028 §6, conclusão 3).

> **Medido:** o parsing da IFD de GPS na mesma abertura não se distingue do ruído (§4.2): a leitura dos dois fatos custou 0,555–0,570 ms por arquivo com cache quente, o que para um disco de 40.000 fotos projeta **~23 s de leitura de EXIF**, contra ~5 h de reindexação — e duas aberturas, num backfill por coluna, custariam ~43 s. O comando é `python -m app.infrastructure.workers.exif_backfill --root PATH [--label] [--dry-run] [--only-unknown | --force]`; `capture_date_backfill` foi **removido**, junto com `BackfillCaptureDatesUseCase`, que `BackfillExifUseCase` substitui (§14). A prova de que o comando não carrega o modelo roda o grafo de imports num interpretador novo, e o guarda do guarda confirma que o mesmo teste detecta `torch` nos imports do executor de jobs. Contra o banco de desenvolvimento, `--dry-run` sobre os 40 arquivos reais está em §13. A leitura a frio de disco externo continua **não medida**.

## 9. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Fallback de XMP (`drone-dji:GpsLatitude`) | **Medido:** os 40 arquivos têm XMP `drone-dji` e nenhum tem latitude ou longitude (§2.3) |
| Inferir posição de fotos sem GPS pelas vizinhas no tempo do mesmo dispositivo | Põe uma coordenada plausível, confiante e errada numa coluna chamada `latitude` — o argumento do `mtime` do RFC-028 §2.1, com outra roupa |
| Corrigir a coordenada para o ponto fotografado, com gimbal e altitude | É um modelo com erro próprio, e `position_source = 'exif_gps'` passaria a afirmar uma precisão que não sustenta. O raio absorve o deslocamento (§2.2). Fica como `'subject_estimated'`, uma fonte **adicional**, se algum dia alguém a consumir |
| Guardar altitude, rumo e ângulos do gimbal agora | Nada os consome — é o `minimum_similarity` do RFC-025 §13.2 outra vez. Estão no arquivo e continuam lá |
| PostGIS | Imagem nova para o banco e peso no instalador, para o que `sin`/`cos` respondem (§5.1) |
| `cube` + `earthdistance` | `CREATE EXTENSION` no caminho de migration para substituir seis linhas de expressão (§5.1) |
| Coluna de geohash ou H3 | Coluna derivada a manter em sincronia; §7 agrega com `round()` |
| Uma coluna `point` em vez de duas | Transforma `latitude` em `(position)[1]` em toda consulta e log; perde o `CHECK` de pareamento legível (§5) |
| Só a caixa envolvente, sem distância exata | A caixa é um quadrado: devolve cantos a 1,41× o raio pedido. "Perto" com 41% de folga silenciosa |
| Só a distância exata, sem caixa | Correto e sem chance de usar índice; a caixa é pré-filtro descartável, e é descartada quando é errada (§6) |
| Raio em quilômetros no HTTP e metros no domínio | Duas unidades e uma conversão que nenhum teste pegaria quando invertesse. Metros em toda a pilha (§3) |
| Raio padrão quando só o ponto é enviado | Inventa a intenção do usuário na grandeza que §2.2 mostrou ser crítica |
| Aceitar raio de 50 m porque o GPS é preciso | O GPS é preciso sobre a **aeronave**; o alvo está a dezenas ou centenas de metros (§2.2) |
| Caixa arrastável no mapa em vez de círculo | A caixa é a *viewport* e é o que §7 agrega; "perto daqui" é um círculo. Duas formas, duas perguntas |
| Mapa alimentado pelos acertos da busca | O top-100 de um índice de 100.000 não descreve distribuição geográfica (§7) |
| Endpoint de mapa que respeite `q` | Só poderia agregar o top-K; ver acima (§7) |
| Ordenar resultados por distância | O ranking continua sendo similaridade. O RFC-028 fez a mesma escolha para data |
| Geocodificação de nome de lugar no backend | Exige internet e viola a premissa local-first; um gazetteer offline é outro RFC (§11) |
| Geocodificação reversa para guardar cidade e estado | Mesma dependência, e um nome é um filtro diferente, com cardinalidade e problemas próprios |
| Uma linguagem de consulta genérica para filtros | Move a semântica do filtro para o cliente e a auditabilidade do `WHERE` para fora do repositório (§2.4) |
| `SearchFilters` com os três campos opcionais num dict | Mata o teste de contrato que obriga os três repositórios a concordar |
| Dois backfills, um por coluna | Dois seeks por arquivo para ler o mesmo cabeçalho (§8.1) |
| Ler GPS numa segunda abertura do arquivo | Dobra o custo medido da varredura para reler os mesmos bytes (§4.2) |
| `position_source` como `ENUM` nativo | `'manual'` e `'subject_estimated'` custariam migration (§4.4) |
| Tratar `0,0` por validação de faixa | Daria a resposta certa pelo motivo errado, e a Ilha Null é um lugar plausível no mapa (§4.3) |
| Guardar meia posição quando só a latitude é legível | Uma coordenada pela metade não localiza nada e passaria pelos `CHECK`s se eles não existissem (§4.3, §5) |
| Índice espacial nesta migration, sem medir | O RFC-028 mediu um B-tree, não se justificou e não foi criado. Mesmo critério (§6.1) |
| Adotar `hnsw.iterative_scan` sem medir | É o que o RFC-028 §8.1 recusou fazer; recusar medir pela terceira vez seria deixar o risco virar política |
| Suportar HEIC junto, para alcançar os 4% de smartphone | Defeito real e separado: afeta a busca semântica inteira, não só o filtro espacial, e traz dependência nova (§11) |

## 10. Não-objetivos

- **O componente de mapa no frontend.** `frontend/` está vazia hoje, e não há RFC de UI: o mapa é consumidor do contrato que este RFC define (§7, §11)
- Tiles de mapa, tiles offline, escolha de provedor, agrupamento por nível de zoom
- Geocodificação de nome de lugar e geocodificação reversa (cidade, estado, bairro)
- Listar fotos de uma área **sem** consulta textual — o passo seguinte, e precisa de ordenação e paginação próprias (§11)
- Consultas por polígono: *"dentro do município"*, *"dentro da fazenda"* (é o caso que justificaria PostGIS, §5.1)
- Altitude, rumo, velocidade, ângulos de gimbal como colunas ou filtros
- Reconstrução de trajeto ou agrupamento por voo
- Estimar o ponto fotografado a partir de gimbal e altitude (§9)
- Marcar posição manualmente no mapa — `'manual'` já tem nome em `PositionSource` e nenhuma rota (§4.4)
- Suporte a HEIC, HEIF e DNG (§2.3, §11)
- Escrever EXIF em qualquer arquivo — o sistema **nunca** escreve no acervo
- Ordenação por distância como alternativa ao ranking semântico

## 11. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **Filtro espacial devolve menos que `limit` sob HNSW** | **Medido e decidido** (§6.1.1): a 20% de seletividade, 13 de 20 consultas voltaram curtas, com até 3 linhas de 10. Toda mitigação medida resolve o resultado curto, mas **nenhuma atinge o critério declarado**, porque os planos exatos que o planejador escolhe nas seletividades baixas e sob plano genérico ficam muito acima de 2× a busca sem filtro — com ou sem mitigação. Pela regra deste RFC, nenhuma mitigação foi aplicada e o contrato continua admitindo resultado curto. Reabrir exige mudar o critério, não a implementação |
| **100% de GPS válido vale para 40 arquivos, não para o acervo** | Remedido com o script do projeto: 40 de 40 na mesma pasta. **A fração do acervo inteiro continua não medida** — só essa pasta estava acessível; `measure_gps_cost.py --root <disco> --sample N` é a medição a fazer |
| **A coordenada é da aeronave, não do alvo** | Declarado (§2.2). Absorvido pelo raio e por `MIN_RADIUS_M`; nunca corrigido em silêncio (§9) |
| **Convenção do `GimbalPitchDegree`** | **Verificada nos arquivos** (0° = horizonte, §2.2). Achado novo: a **FC3682 grava `0.00` em todo quadro**, então o deslocamento dessa câmera não é calculável do arquivo, e `MIN_RADIUS_M` foi medido só sobre as outras duas |
| **Fotos sem coordenada ficam invisíveis sob filtro espacial** | Deliberado (RFC-020). Tornado visível por `excluded_unknown_position` (§6.2) e pela contagem do mapa (§7) |
| **Os ~4% de smartphone podem não estar indexados** | HEIC não está em `SUPPORTED_IMAGE_EXTENSIONS`. Defeito anterior a este RFC, maior que ele, e com dependência nova (`pillow-heif`). RFC próprio |
| **`0,0` não observado no piloto** | A regra de §4.3 é mantida por argumento. Se o acervo real tiver muitos, o número aparece em `position_source = 'unknown'` e é observável por `GROUP BY` |
| **Custo do endpoint de mapa em 100.000 linhas** | **Medido** (§7): ~0,3–1 s no servidor para a coleção inteira, ~60–300 ms para 10 km, com variação grande entre execuções nesta máquina. Índice não ajuda (0,96×). Se um mapa interativo pedir mais, a resposta é cache por *viewport* ou uma coluna de célula materializada — com a manutenção que §5.1 recusou —, não um índice |
| **Mesmo pedido, planos diferentes conforme a conexão** | Herdado do RFC-028 §8.1 (plano genérico vs customizado), agora com uma cláusula a mais |
| **Antimeridiano e polos** | Caixa cruzando ±180 → 400 (§7); no filtro, o pré-filtro é omitido e a resposta continua exata (§6) |
| **Tiles de mapa vazam a *viewport* para o provedor** | O produto é local-first e não registra consultas (RFC-026 §16.1). As **coordenadas das fotos** não saem da máquina, mas a área que o usuário olha sai, se o frontend usar tiles online. Decisão do RFC de UI, nomeada aqui para não ser descoberta depois |
| **Transporte de desenvolvimento acrescenta ~43 ms a toda busca** | Achado da medição (§6.1.1, §14): no Docker de desenvolvimento, toda mensagem acima de ~8 KB custa +43 ms, e toda busca vetorial manda ~10 KB. Não afeta o alvo de distribuição (PostgreSQL nativo, RFC-029 §14), mas afeta qualquer latência medida de ponta a ponta no ambiente de desenvolvimento. Fora do escopo deste RFC |

**Trabalho futuro, em ordem aproximada de valor:**

1. **Listar fotos de uma célula do mapa, sem consulta textual.** É o primeiro clique que a UI de mapa vai querer e este RFC não entrega. Precisa decidir ordenação (data de captura, presumivelmente) e paginação — RFC próprio, como o RFC-029 e o RFC-031 foram para os buracos equivalentes;
2. o RFC de UI que consome §6 e §7, com a decisão de tiles online ou offline;
3. HEIC e DNG, que valem para a busca inteira e não só para este filtro;
4. os eixos de filtro que ficam baratos depois deste: **pasta** (`relative_path` com prefixo, e o repositório já sabe comparar prefixos de pasta), **câmera** (`Make`/`Model` vêm da mesma IFD0 que §4.2 já abre, e o piloto já os leu), extensão e tamanho. Cada um é uma coluna, uma cláusula, um parâmetro e — se puder esconder linhas — um `excluded_*`;
5. medir a leitura a frio de um disco externo da gaveta, dívida de medição aberta desde o RFC-028 §6;
6. `'manual'` como fonte de posição, que é a resposta direta ao *"saber a exata coordenada é muito difícil"* para as fotos que não têm nenhuma.

## 12. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/value_objects/position.py` | `Position`, faixas validadas, naive de unidade (§5) |
| `backend/app/domain/value_objects/geo_circle.py` | `GeoCircle`, centro e raio em metros, raio positivo (§6) |
| `backend/app/domain/value_objects/position_source.py` | `PositionSource` e a precedência da cadeia (§4.4, §8.1) |
| `backend/app/domain/exceptions/position_errors.py` | `InvalidPositionError`, `InvalidGeoCircleError` |
| `backend/app/domain/services/haversine.py` | A distância, uma implementação, compartilhada pelos dois doubles (§6) |
| `backend/app/application/use_cases/position_plan.py` | `position_to_write()`, a escrita condicional (§8) |
| `backend/app/application/use_cases/aggregate_positions.py` | O universo e a agregação do mapa (§7) |
| `backend/app/application/use_cases/backfill_exif.py` | O backfill unificado (§8.1) |
| `backend/app/infrastructure/workers/exif_backfill.py` | O comando de §8.1, raiz de composição |
| `backend/app/presentation/schemas/map_schema.py` | A resposta de `GET /images/map`, com `precision_applied` (§7) |
| `backend/alembic/versions/<rev>_add_image_position.py` | Três colunas e três `CHECK`s; `down_revision = 'f4b9e2d7c615'`; `downgrade()` real |
| `backend/tests/infrastructure/filesystem/test_exif_position.py` | IFD de GPS real, sinais `S`/`W`, `0,0`, meia posição, denominador zero, fora de faixa, arquivo ilegível |
| `backend/tests/infrastructure/persistence/test_position_contract.py` | Escrita e leitura da posição, nos três repositórios |
| `backend/tests/infrastructure/persistence/test_position_filter_contract.py` | Círculo, posição desconhecida, filtros combinados, SQL não filtrado inalterado |
| `backend/tests/presentation/test_map_api.py` | Rota não sombreada por `/{image_id}`, teto de células, caixa inválida (§7) |
| `backend/tests/domain/test_geo_circle.py`, `test_position.py` | Value objects e `is_empty()` com três campos |
| `experiments/rfc-032-geolocation/pilot_exif_gps_check.py` (+ `.log`) | **Já entregue**: a medição-piloto de §2.3, que precedeu este documento e matou o fallback de XMP |
| `experiments/rfc-032-geolocation/measure_gps_cost.py` (+ `.log`) | O custo de §4.2 e a cobertura de §2.3 sobre o acervo inteiro |
| `experiments/rfc-032-geolocation/planner_check.py` (+ `.log`) | A medição e o critério de §6.1 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/domain/entities/image.py` | `latitude`, `longitude`, `position_source`; pareamento validado; fora da igualdade |
| `backend/app/domain/value_objects/search_filters.py` | `taken_within`; `is_empty()` com três campos |
| `backend/app/domain/value_objects/index_metadata.py` | `position_source`, documentado como **não** sinal de mudança |
| `backend/app/domain/repositories/image_repository.py` | `update_position`, `update_position_many`, `count_unknown_position`, `aggregate_positions`; contrato do filtro espacial |
| `backend/app/application/use_cases/search_images.py` | `count_hidden_by_unknown_position()` |
| `backend/app/application/use_cases/index_or_update_images.py`, `index_or_update_image.py` | Escrita condicional nos dois ramos; `positions_written` no sumário |
| `backend/app/infrastructure/filesystem/exif_capture_date.py` | `read_exif_facts()`: uma abertura, dois fatos, três saídas preservadas por fato (§4.2) |
| `backend/app/infrastructure/filesystem/discovered_image_file.py` | `position` |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | Extração no mesmo laço, atrás de `extract_gps` |
| `backend/app/infrastructure/config/settings.py` | `extract_gps`, `MAX_MAP_CELLS`, `MIN_RADIUS_M` |
| `backend/app/infrastructure/database/models/image_model.py` | As três colunas e os `CHECK`s em `__table_args__` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | Pré-filtro de caixa, haversine exata, contagem, agregação, escritas em lote, prefetch |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Idem, com `is None` explícito |
| `backend/tests/application/fakes.py` | **O terceiro repositório**, com as mesmas regras |
| `backend/app/presentation/api/v1/routers/images.py` | `near_lat`/`near_lon`/`radius_m`; `GET /images/map` **antes** de `/{image_id}` |
| `backend/app/presentation/schemas/search_schema.py` | `latitude`, `longitude`, `position_source`, `excluded_unknown_position` |
| `backend/app/infrastructure/workers/capture_date_backfill.py` | **Removido**, substituído por `exif_backfill` (§8.1) |
| `ARCHITECTURE.md` | §15 (tabela `Images`) e a seção de busca |
| `docs/rfcs/README.md` | Linha da Sprint 6 |

> **Entregue como listado**, com estas diferenças — cada uma explicada em §14:
>
> - `position.py` contém, além de `Position`, `PositionReading` (o fato examinado), `BoundingBox` (a *viewport* do mapa e o pré-filtro do círculo), `PositionCell` e o alias `PositionCells`;
> - `position_errors.py` tem uma terceira exceção, `InvalidBoundingBoxError`;
> - **removidos também** `backend/app/application/use_cases/backfill_capture_dates.py` e `backend/tests/application/test_backfill_capture_dates.py`; os testes migraram para `backend/tests/application/test_backfill_exif.py`, e `test_capture_date_backfill.py` virou `backend/tests/infrastructure/workers/test_exif_backfill.py`;
> - **novos também:** `backend/tests/application/test_aggregate_positions.py`;
> - **modificados também:** `job_runner.py` e `thumbnail_backfill.py` (passam `extract_gps`), `indexing_worker.py` (a posição entra na entidade), `indexing_plan.py` (docstrings), `presentation/dependencies/__init__.py` (injeção de `min_radius_m` e `max_map_cells`), `presentation/schemas/image_schema.py` (a posição na forma compartilhada), `tests/conftest.py` e `tests/dataset/test_semantic_search_e2e.py` (fixtures que apagavam dispositivos antes dos jobs), os testes de rota, de detalhes, de integração, de modelo, de migration, de porta e de exceções que fixavam formas ou listas, `.env.example` e `AI_Context.md`; `tests/cli/test_main.py` recebeu só uma linha reformatada pelo `black`.

## 13. Validação

Critérios de aceitação deste RFC, com o resultado de cada um.

| verificação | como | resultado |
| --- | --- | --- |
| `pytest`, `black --check`, `ruff check`, `mypy` | Limpos, com a contagem de testes escrita de volta | **2047 passed**, 58 deselected (eram 1629 antes deste RFC); `pytest -m slow`: **58 passed** (CLIP real, ponta a ponta); `black --check`, `ruff check` e `mypy` (estrito, 258 arquivos) limpos |
| `alembic heads` único; `downgrade` e `upgrade` exercitados **com dados** | Linhas com embedding sobrevivem; `md5` do conjunto de embeddings idêntico antes, depois do downgrade e depois do upgrade | Head único `6d77379a36a1`. Sobre as 60 linhas com embedding do banco de desenvolvimento, `md5` = `67cecd11d55b5ee3c6d2d24e966c7fc4` antes, depois do upgrade, depois do downgrade e depois do novo upgrade; os três `CHECK`s aparecem e somem com as colunas |
| Os três `CHECK`s recusam meia posição e coordenada fora de faixa | `INSERT` direto, sem passar pelo Domain | `TestThePositionChecksHoldWithoutTheDomain`: meia posição recusada por `ck_images_position_pairing`; 90,0001, −91, 180,0001 e −181 recusados pelos `CHECK`s de faixa; os quatro cantos do planeta aceitos |
| Posição desconhecida nunca casa com um círculo, nos três repositórios | `test_an_unknown_position_never_matches` | Passa nos três — **depois** de pegar um defeito real na rodada PostgreSQL (`LEAST` ignora `NULL`, §14) |
| Busca sem filtro emite a consulta do RFC-025 inalterada | Afirmado sobre o **SQL compilado** | `test_without_a_circle_the_unfiltered_query_is_unchanged` e o teste do RFC-028 continuam passando: `None` e `SearchFilters()` emitem o texto de antes, caractere por caractere |
| Busca sem círculo não dispara a contagem de §6.2 | No nível do use case: o repositório não é chamado | `TestCountHiddenByUnknownPosition`: sem círculo, nenhuma chamada registrada no repositório falso; a rota responde `null` |
| Dois de três parâmetros espaciais → 400; raio abaixo do mínimo → 400 | Teste de rota | `TestCircleParameters`: cada combinação incompleta é 400 e nomeia o que falta; raio 50 com mínimo 300 é 400 e a mensagem fala do drone; 300 é aceito; não-número é 422 |
| `GET /images/map` responde 200 e **não** 422 | A armadilha de ordem de rotas de §7 | `test_images_map_is_a_200_not_a_422`, e um teste que falha se `/{image_id}` for alcançado |
| Mapa ignora `q` e agrega o universo dos filtros | Mesmo universo que a contagem de §6.2 | `test_the_query_text_is_ignored` (resposta idêntica com e sem `q`); o mapa nem compõe o modelo; a contagem sem posição é a mesma de §6.2 |
| Uma única abertura de arquivo por arquivo varrido | Espião de `Image.open()` (§4.2) | `TestOneOpenPerFile`: 5 aberturas para 5 arquivos com os dois fatos; 5 com um só; 0 com os dois desligados. Em escala: 3.000 para 3.000 |
| `0,0` vira `unknown`, com sinal e referência corretos no caso válido | Sobre estrutura de IFD real, não fixture em IFD0 (§4.1) | `test_exif_position.py`: JPEGs gravados pelo Pillow com a IFD de GPS em `0x8825`; `S`/`W` negativos; `0,0` e zeros com ruído são `unknown`; tags na IFD0 ou na sub-IFD Exif **não** são achadas. Sobre os 40 arquivos reais, `DJI_0013.JPG` → −26,321406, −48,816307, como no piloto |
| `--force` não rebaixa `exif_gps`, e as duas cadeias são independentes | Um arquivo pode perder a data e manter a posição (§8.1) | `TestForceMode` e `TestTheTwoFactsAreIndependent`: os dois sentidos — perder a data e manter a posição, perder a posição e manter a data |
| Backfill não importa o modelo, nem transitivamente | Grafo de imports em interpretador novo, com o guarda do guarda | `TestTheModelIsNeverLoaded`: nem o módulo nem os imports de `main()` carregam `torch`, `transformers` ou `app.presentation.dependencies`; o guarda confirma que os imports do executor de jobs carregam |
| Mudar a fonte da posição não torna a linha candidata a reindexação | Verificado **por mutação** (§8) | Com `existing.position_source == candidate.image.position_source` injetado na primeira condição de `plan_indexing()`, **3 testes falharam**; removido, todos passam |
| Cobertura de GPS no acervo real | Amostra aleatória com o script do projeto (§2.3) | **Parcial**: 40 de 40 `exif_gps` na pasta disponível. A amostra aleatória do acervo inteiro não foi feita — os discos não estavam nesta máquina |
| Seletividade × plano × configuração de HNSW | O critério de §6.1, com os três cenários e o veredito | Medido (§6.1.1): resultado curto reproduzido; **critério não atingido por nenhuma configuração**; nenhuma mitigação aplicada; índice não criado |
| *(além do RFC)* backfill de ponta a ponta no banco de desenvolvimento | `exif_backfill` sobre a pasta real | Sobre os 40 arquivos reais, no banco de desenvolvimento: `--dry-run` → data 0 / posição 40 a escrever, nada escrito; execução real → **40 posições `exif_gps` escritas**, 40 datas deixadas como estavam (já examinadas pelo RFC-028), em 1 s; segunda execução → **0 escritas**. Depois, pela aplicação inteira sem substituições (PostgreSQL e CLIP reais): `GET /images/map` sobre a área do piloto → **24 células, 40 fotos** — as mesmas 24 células de ~100 m que o piloto mediu —, e `excluded_unknown_position = 20`; busca *"telhado"* num raio de 300 m de `DJI_0013` → as 10 fotos daquele local, com coordenadas; raio de 50 m → 400 com a mensagem do drone |

## 14. Correções feitas durante a implementação

Registradas à vista do texto original, como o RFC-028 fez com as suas. Nenhuma muda uma decisão de §3 sem dizer qual número a mudou.

| onde | o texto dizia | o que foi feito, e por quê |
| --- | --- | --- |
| §4.2, §6 | `DiscoveredImageFile.position: Position \| None`, e `GeoCircle` com centro `Position` | Um tipo não serve aos dois: o centro de um círculo é um ponto sem fonte; a leitura de um arquivo é uma fonte que pode não ter ponto. `Position` ficou sendo o ponto; o fato examinado é **`PositionReading(position, source)`**, com `PositionReading.unknown()` — o `CaptureDate` da posição. O campo de `DiscoveredImageFile` se chama `position`, como o texto pede, e tem esse tipo |
| §7 | `min > max` → 400; antimeridiano → 400 | A caixa do mapa é um `BoundingBox` de Domain que recusa os dois com **`InvalidBoundingBoxError`** (400). As quatro coordenadas não distinguem cantos trocados de uma *viewport* que cruza ±180 na longitude, e a mensagem diz isso |
| §7 | `precision` sem padrão nem limites | Padrão **3**; negativa é 422; acima de **6** é servida a 6 e ecoada em `precision_applied`; em 0 não há redução possível e todas as células voltam (§7) |
| §6.2 | "sem `taken_within`, o repositório não é chamado" | A regra está no **use case**. `count_unknown_position()` do repositório não responde 0 sem círculo, porque o mapa faz essa mesma pergunta sem círculo. E `count_unknown_capture_date()` passou a aplicar o círculo como "demais cláusulas" |
| §8.1 | remover `capture_date_backfill.py` | Removidos também o use case `BackfillCaptureDatesUseCase` e seu teste: sem o comando, ele seria exatamente o "alias que ninguém vai remover". `BackfillExifUseCase` decide os dois fatos em separado, e o relatório conta por fato |
| §12 (`search_schema.py`) | `latitude`, `longitude`, `position_source` na resposta da busca | Postos em `ImageSchema`/`image_fields()`, que a busca **herda**: o RFC-030 fez essa forma compartilhada justamente para que a busca e `GET /images/{id}` não descrevam a mesma foto de dois jeitos. `GET /images/{id}` ganhou os três campos também |
| §6 | haversine em SQL | Cada função tipada `double precision`; sem isso o SQLAlchemy tratava `radians(...) / 2` como divisão de desconhecidos e emitia `CAST(2 AS NUMERIC)`. `sin²` é `sin·sin` nos dois lados, Python e SQL |
| §6.1 | medir latência | A primeira execução cronometrou a ida e volta e achou ~47 ms sob toda busca: **toda mensagem acima de ~8 KB para o Docker desta máquina custa +43 ms** (8.000 bytes: 1,9 ms; 9.000: 44 ms), e todo vetor de consulta tem ~10 KB. A métrica passou a ser o tempo do servidor (`EXPLAIN (ANALYZE, TIMING OFF)`), com a ida e volta impressa ao lado |
| §6.1 | "5 células de ~100 m concentram 45%" como alvo do corpus | A estatística não muda de escala: 40 fotos enchem poucas células, 18.000 não. O corpus reproduz a propriedade — **os 5 locais mais cheios guardam 45,5%**, e 10% do corpus está a 82 m de um único local — e o log diz as duas coisas |
| §6.1 | veredito da mitigação | Critério aplicado como escrito: **não atingido**, nenhuma mitigação aplicada. O motivo é estrutural (planos exatos acima do teto em toda configuração, inclusive a linha de base) e está em §6.1.1, com o que mudaria se o critério julgasse só os planos HNSW |
| §7 | `MAX_MAP_CELLS` TBM, sem critério | A implementação declarou um critério antes do número, e ele **não selecionou nada**: o limite de tempo media a varredura de 100.000 linhas, não o teto, e o limite de tamanho nunca foi exercido. O critério foi corrigido para medir o que o teto limita — o tamanho da pior resposta — e as duas versões estão no log |
| §6.1, §7 | índice: "só criar se a medição justificar" | Uma segunda execução mostrou o B-tree "reduzindo à metade" o mapa, com o plano em varredura sequencial nos dois casos — logo o índice não podia ser a causa. Uma terceira, aquecida e alternada, deu 0,96×. A redução era a ordem das medições logo após uma carga de 100.000 linhas |
| §6 | "no PostgreSQL uma comparação com `NULL` já é falsa e a imagem sem posição sai de graça" | **Valia para comparações, não para `LEAST()`.** A primeira versão limitava o argumento do `asin` com `least(1, sqrt(h))`, e o `LEAST` do PostgreSQL **ignora `NULL`**: uma linha sem posição media π·R ≈ 20.015 km de qualquer centro, e um círculo sem caixa (polo, antimeridiano, ou raio desse tamanho) **casava fotos sem posição**. `test_an_unknown_position_never_matches` — o teste que §6 nomeia — pegou o defeito só na rodada PostgreSQL. O limite agora é um `CASE`, que propaga `NULL`, e um teste sobre o SQL compilado proíbe `least(` na expressão. As medições de §6.1.1 usaram a versão antiga; só linhas sem posição mudam de resultado, e todos os círculos medidos tinham caixa, então planos e números não mudam |
| §2.2 | convenção do gimbal TBM | Verificada nos arquivos, e com um achado: a FC3682 grava `0.00` sempre. `MIN_RADIUS_M` usa só as câmeras que gravam o ângulo |
| — (fora do RFC) | `tests/conftest.py` | `empty_db_session` apagava `devices` antes de `indexing_jobs`, que tem chave estrangeira para ele desde o RFC-029; passava só enquanto o banco de desenvolvimento nunca tinha rodado um job. Com um job real no banco, **toda** prova de contrato contra PostgreSQL falhava antes de chegar ao código. Corrigido: os jobs são apagados antes, dentro da mesma transação desfeita. A fixture de módulo de `tests/dataset/test_semantic_search_e2e.py` (testes `slow`) tinha o mesmo defeito e recebeu a mesma correção |
