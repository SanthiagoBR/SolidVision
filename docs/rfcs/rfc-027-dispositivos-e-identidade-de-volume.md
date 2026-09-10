# RFC-027 — Dispositivos e Identidade de Volume

**Status:** Implementado
**Depende de:** RFC-019 (repositório PostgreSQL), RFC-020 (metadados incrementais), RFC-021 (worker), RFC-024 (pipeline), RFC-025 (busca)
**Migration:** sim — `devices`, `images.device_id`, `images.relative_path`, e uma **reescrita de chave primária** (§6)
**Medição:** `experiments/rfc-027-devices/planner_check.py` — medido (§9.1)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir* durante a implementação e devia ser escrito de volta aqui depois. **Isso foi feito:** não resta nenhum `TBM`, e cada número abaixo é acompanhado da escala em que foi medido. Onde a medição não respondeu à pergunta inteira — §9.1 é o caso — o que ficou por medir está dito, em vez de ser preenchido por dedução.

---

## 1. Contexto

Um mockup de interface no Figma desenhou uma barra lateral com quatro dispositivos — `HD1 100% indexado`, `HD2 50% indexado`, `HD3 10% indexado`, `HD4 não indexado` — e a pergunta que o originou era se aquilo fazia sentido para o projeto. A resposta imediata foi que não havia API por trás: `Collection` é uma entidade placeholder de uma linha, e `ImageModel` não tem coluna nenhuma que aponte para um disco.

Isso está correto e é a parte menos importante. A investigação que o mockup provocou encontrou um **defeito já presente no código**, que não tem nada a ver com interface, e que o conceito de dispositivo é o único jeito honesto de corrigir.

O RFC-021 derivou a identidade de uma imagem do seu caminho:

```python
SOLIDVISION_PATH_NAMESPACE = uuid.UUID("5dc64f53-522e-4303-951e-ee6123b10dd8")

def compute_image_id(path: ImagePath) -> ImageId:
    return ImageId(uuid.uuid5(SOLIDVISION_PATH_NAMESPACE, str(path)))
```

A docstring do módulo declara a limitação que conhecia: *"Renomear ou mover um arquivo muda seu caminho e portanto seu id."* Aceitável, e declarado. O que ela não previu é que **o caminho de um arquivo parado muda sozinho.**

## 2. Problema

### 2.1 Letra de unidade não é identidade

No Windows, a letra atribuída a um volume removível é função da *ordem de montagem*, não do volume. O mesmo HD externo monta como `D:` hoje e `F:` amanhã, dependendo do que mais estava plugado no boot. Nada no disco mudou.

Para o SolidVision de hoje isso significa:

| | antes | depois |
| --- | --- | --- |
| caminho armazenado | `D:/fotos/2018/DJI_0042.JPG` | `F:/fotos/2018/DJI_0042.JPG` |
| `uuid5` derivado | `a1b2…` | `9f0e…` — **outro id** |
| linha antiga | permanece, órfã, apontando para um caminho inexistente | |
| linha nova | criada do zero | |
| `UniqueConstraint("path")` | **não impede nada** — são duas strings diferentes | |
| embeddings | recomputados para o disco inteiro | |

O custo não é cosmético. O RFC-024 mediu a indexação a 2,2 imagens/segundo em CPU. Um HD com 40.000 fotos custa aproximadamente **5 horas de inferência** para ser reindexado — e é reindexado toda vez que a letra troca, sem nenhum aviso, produzindo em seguida uma tabela com duas linhas por foto, ambas com embedding, ambas elegíveis à busca.

### 2.2 O `--root` também entra no id

O mesmo defeito tem uma segunda porta de entrada, independente do sistema operacional. `FilesystemImageProvider` produz `path` a partir do `root` recebido, e o worker recebe `--root` da linha de comando. Portanto:

```
--root D:\fotos          →  D:/fotos/2018/DJI_0042.JPG   →  id A
--root D:\fotos\         →  D:/fotos/2018/DJI_0042.JPG   →  id A     (Path normaliza)
--root .\fotos           →  fotos/2018/DJI_0042.JPG      →  id B     (relativo)
```

`ImagePath` normaliza separadores, mas não resolve caminho relativo para absoluto. Rodar o worker de dentro de outro diretório de trabalho, com a mesma intenção, produz um segundo conjunto completo de identidades.

### 2.3 O dispositivo desconectado não tem como ser representado

Este é o requisito de produto, e é o menor dos três problemas em gravidade mas o maior em valor.

O `ARCHITECTURE.md` §2 descreve o usuário-alvo: fotógrafos aéreos com centenas de milhares de imagens acumuladas ao longo de anos. Esse acervo não mora em um disco. Mora em uma gaveta de discos externos, dos quais tipicamente zero ou um está plugado em qualquer momento.

Uma busca que encontre a foto certa em um disco desconectado e devolva um resultado quebrado falhou. Uma busca que devolva *"está no HD3, que não está conectado"* **resolveu o problema do usuário** — ele agora sabe qual dos vinte discos pegar. Isso não é degradação graciosa; é a funcionalidade. E é impossível de expressar enquanto o sistema não souber que discos existem.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Nova entidade de Domain | `Device` — um volume físico, com identidade estável (§4) |
| Novo value object | `DeviceId` (UUID interno) e `VolumeIdentity` (o que o SO reporta) |
| Nova porta | `DeviceRepository` no Domain, ao lado de `ImageRepository` |
| Identidade de volume no Windows | `\\?\Volume{GUID}\` via `GetVolumeNameForVolumeMountPoint` (§4.1) |
| Caminho armazenado | `(device_id, relative_path)` substitui `path` absoluto (§5) |
| Identidade da imagem | `uuid5(ns, f"{device_id}/{relative_path}")` (§6) |
| Estado de conexão | **Nunca persistido.** Resolvido em tempo de requisição (§7) |
| `% indexado` | Derivado, com denominador declarado (§8) |
| Filtro de dispositivo na busca | `SearchFilters` na porta, primeiro filtro (§9) |
| `Device` vs `Collection` | Coisas diferentes; `Collection` permanece placeholder (§10) |
| Migration | Sim, incluindo reescrita de PK — e **é agora ou nunca** (§6.2) |

## 4. `Device` como entidade

```python
@dataclass(frozen=True)
class Device:
    id: DeviceId
    volume_identity: VolumeIdentity   # o que o SO reporta, estável
    label: str                        # "HD2" — escolhido pelo usuário
    filesystem_label: str | None      # o rótulo do volume, informativo
    total_bytes: int | None
    first_seen_at: datetime
    last_seen_at: datetime
    last_scan_at: datetime | None      # §8
    last_scan_file_count: int | None   # §8 — o denominador declarado
```

> **Correção de rascunho.** As duas últimas linhas não estavam nesta lista
> quando o RFC foi escrito, e a §8 as exige — ela decide que o denominador de
> `% indexado` é *a última varredura* e diz, com todas as letras, que essa
> escolha "exige persistir o resultado da varredura separadamente do resultado
> da indexação — `devices.last_scan_file_count` e `devices.last_scan_at`". A
> §4 e a §5.1 simplesmente não tinham sido atualizadas para concordar com a
> §8. Foram implementadas, e a lista acima e a tabela da §5.1 passam a
> refleti-lo.

Congelada e comparando por `id`, seguindo `Image` (RFC-009). `label` é do usuário porque `filesystem_label` frequentemente é `Untitled` ou vazio, e um mockup que promete `HD2` precisa de um lugar para guardar `HD2`.

**Não há campo `mount_point` nem `drive_letter`.** Persistir a letra reintroduziria exatamente o dado instável que este RFC existe para eliminar. A letra é resolvida quando necessária (§7) e nunca armazenada.

### 4.1 O que serve como identidade de volume

Três candidatos, e o critério é: sobrevive à remontagem, sobrevive ao reboot, sobrevive a plugar em outra porta.

| candidato | estável? | veredito |
| --- | --- | --- |
| Letra de unidade (`D:`) | não — §2.1 | rejeitado |
| Número de série do volume (32 bits, `GetVolumeInformation`) | sim, até reformatar | insuficiente sozinho — 32 bits colidem |
| `\\?\Volume{GUID}\` (`GetVolumeNameForVolumeMountPoint`) | sim, até reformatar | **escolhido** |

O GUID de volume é o identificador que o próprio Windows usa internamente para o volume, independente de ponto de montagem. Obtido via `ctypes` sobre `kernel32`, sem dependência nova.

Reformatar o disco muda o GUID. Isso é aceitável e não precisa de tratamento: reformatar destrói as fotos, então a perda das linhas é a resposta correta, não um efeito colateral.

**Portabilidade.** `VolumeIdentity` é um value object com uma string opaca e um discriminador de plataforma (`windows-volume-guid`, `linux-fs-uuid`, `macos-volume-uuid`). O adaptador que a produz vive em `infrastructure/filesystem/` atrás de uma porta, como todo o resto. Este RFC entrega **apenas o adaptador Windows**, porque é a plataforma em que o projeto roda (`ARCHITECTURE.md` e o ambiente de desenvolvimento), e um adaptador Linux não testado seria uma afirmação não verificada. O discriminador existe para que o segundo adaptador não exija migration.

**A ressalva honesta:** um volume identificado por GUID não distingue um disco de sua cópia byte a byte. Clonar um HD produz dois volumes que o sistema considera o mesmo dispositivo. Isso é uma limitação declarada, não resolvida, e listada em §13.

## 5. Esquema

### 5.1 Tabela `devices`

| coluna | tipo | nota |
| --- | --- | --- |
| `id` | `UUID` PK | `uuid5` sobre a identidade de volume — determinística, como `ImageId` |
| `volume_identity` | `TEXT` NOT NULL UNIQUE | a string opaca |
| `volume_kind` | `TEXT` NOT NULL | o discriminador de plataforma |
| `label` | `TEXT` NOT NULL | do usuário |
| `filesystem_label` | `TEXT` NULL | informativo |
| `total_bytes` | `BIGINT` NULL | `CHECK >= 0` |
| `first_seen_at` / `last_seen_at` | `TIMESTAMPTZ` NOT NULL | |
| `last_scan_at` | `TIMESTAMPTZ` NULL | §8 |
| `last_scan_file_count` | `INTEGER` NULL | §8 — `CHECK >= 0`; NULL significa *nunca varrido*, nunca *zero* |

`last_seen_at` é histórico ("quando este disco esteve plugado pela última vez"), **não** estado de conexão. A distinção é §7.

### 5.2 Mudanças em `images`

```python
op.add_column("images", sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False))
op.add_column("images", sa.Column("relative_path", sa.String(), nullable=False))
op.create_foreign_key("fk_images_device_id", "images", "devices", ["device_id"], ["id"])
op.drop_constraint("uq_images_path", "images")
op.create_unique_constraint("uq_images_device_relative_path", "images", ["device_id", "relative_path"])
op.drop_column("images", "path")
```

`path` **sai**. Manter as duas formas convidaria a que uma delas ficasse desatualizada, e a absoluta é a que não pode ser mantida correta — ela muda sem que ninguém escreva nada.

O caminho absoluto continua existindo; passa a ser **computado**, juntando o ponto de montagem resolvido em §7 ao `relative_path`. Isso é o RFC-030, que é quem precisa dele.

## 6. A reescrita de identidade

### 6.1 O que muda

```python
def compute_image_id(device_id: DeviceId, relative_path: ImagePath) -> ImageId:
    return ImageId(uuid.uuid5(SOLIDVISION_PATH_NAMESPACE, f"{device_id}/{relative_path}"))
```

O namespace `5dc64f53-…` é **preservado**. A docstring do RFC-021 proíbe regenerá-lo, e a proibição continua valendo — o que muda é a string alimentada nele, não o namespace. Trocar o namespace *e* a string tornaria impossível reconhecer, olhando um id antigo, de que esquema ele veio.

Toda imagem já indexada recebe um id novo. A migration recalcula e faz `UPDATE` da chave primária **preservando `embedding`**, que é a única coluna cara. Nenhuma inferência é refeita.

### 6.2 Por que este RFC tem que vir primeiro

Hoje **nada referencia `images.id`.** Não há chave estrangeira apontando para a tabela, porque `IndexingJobs`, `SearchHistory` e thumbnails estão todos desenhados no `ARCHITECTURE.md` e nenhum foi construído.

Isso torna a reescrita de PK uma operação local: uma tabela, um `UPDATE`, sem cascata. No dia em que o RFC-029 criar `indexing_jobs` com FK para imagens, ou o RFC-030 gravar caminhos de thumbnail derivados do id, a mesma correção passa a ser uma migração coordenada de várias tabelas com risco proporcional.

**A janela para corrigir isso barato está aberta agora e fecha no próximo RFC que criar uma referência.** Essa é a razão de ordenação, e é mais forte que a de dependência.

### 6.3 O caminho da migration

O `relative_path` de uma linha existente não é derivável do `path` absoluto sem saber qual prefixo era a raiz — e a raiz veio do `--root`, que não foi armazenado em lugar nenhum. Não existe informação suficiente no banco para dividir `D:/fotos/2018/x.JPG` em (dispositivo, relativo) automaticamente.

Portanto a migration é **assistida**, e o `downgrade()` que o RFC-007 exige é real:

1. `upgrade()` cria `devices`, adiciona as colunas como `NULL`, e **não adivinha nada**.
2. Um comando de reconciliação (`python -m app.infrastructure.workers.device_reconcile --root PATH --label HD2`) resolve a identidade de volume da raiz informada, cria ou encontra o `Device`, e reescreve as linhas cujo `path` cai sob aquela raiz — preenchendo `device_id`, `relative_path` e o `id` novo, mantendo o embedding.
3. Uma segunda migration aplica `NOT NULL`, a FK e a nova unicidade, e falha em voz alta se sobrou linha não reconciliada.

Linhas não reconciliadas **não são apagadas silenciosamente**. Elas são o registro de um disco que o usuário talvez precise plugar; apagá-las jogaria fora embeddings que custaram horas.

O banco de desenvolvimento tinha 45 linhas (todas com embedding) quando este caminho foi exercitado, então na prática ele rodou contra um corpus pequeno: 45 casadas, 45 reconciliadas, 0 perdidas, e o `md5` do conjunto de embeddings inalterado antes e depois. Ele é escrito para o acervo real de quem já rodou o worker, não para o corpus de demonstração.

## 7. Estado de conexão nunca é persistido

A tentação é uma coluna `is_connected` em `devices`. Ela estaria errada em toda leitura feita depois que o usuário desplugou o disco, e não há evento para atualizá-la — o Windows não avisa este processo.

A conexão é resolvida **quando é perguntada**:

```
volumes montados agora  →  {VolumeIdentity: ponto de montagem}
Device.volume_identity ∈ esse conjunto  →  conectado, e aqui está a letra
```

A enumeração custa uma chamada de sistema e é cacheada por requisição, nunca entre requisições. Um resultado de busca sabe dizer *"HD3, desconectado"* porque a resolução aconteceu ao montar aquela resposta, e não porque alguém escreveu isso em uma tabela algum tempo atrás.

Custo medido da enumeração (`WindowsVolumeIdentityProvider.mounted_volumes()`,
200 chamadas, uma máquina com um volume montado): **mediana 0,12 ms, p95 0,24
ms**. Três ordens de grandeza abaixo do encode de uma consulta, que o RFC-025
§12 mediu em ~90 ms para inglês. O cache por requisição continua justificado
por correção — duas leituras dentro de uma mesma resposta têm de concordar —
e não por custo.

A ressalva é que a medição foi feita com **um** volume montado. O custo cresce
com o número de volumes, porque cada um custa uma chamada
`GetVolumeNameForVolumeMountPoint`; para os vinte discos do cenário do
`ARCHITECTURE.md` §2 — dos quais tipicamente zero ou um está plugado — isso
continua sendo ruído.

## 8. `% indexado` e o denominador que a UI não pode esconder

O mockup mostra `HD2 — 50% indexado`. Esse número é uma razão, e a única parte difícil é o denominador.

| candidato a denominador | o que a porcentagem passa a significar | problema |
| --- | --- | --- |
| linhas em `images` do dispositivo | fração das linhas conhecidas que têm embedding | nunca chega a menos de 100% depois de um pipeline completo; não vê arquivo nunca varrido |
| arquivos no disco agora | fração real do disco | exige varredura completa a cada leitura, e é impossível com o disco desplugado |
| arquivos vistos na última varredura | fração do que a última varredura encontrou | **escolhido** |

A escolha exige persistir o resultado da varredura separadamente do resultado da indexação — `devices.last_scan_file_count` e `devices.last_scan_at`. Uma varredura é barata (`stat`, sem inferência) e é o que o RFC-029 vai disparar de qualquer forma.

O número é, portanto, **"50% dos arquivos que a varredura de 12/03 encontrou têm embedding"**, e a UI precisa dizer isso, não `50%` sozinho. Um usuário que copiou 10.000 fotos novas depois da última varredura merece ver `50% (varredura de 12/03)` em vez de um número confiante e errado.

## 9. Filtro de dispositivo na busca

O RFC-025 §8 escopou a busca globalmente **de propósito**, e nomeou a condição para mudar isso: *"escopar a busca por coleção é uma mudança no que uma coleção significa, e precisa antes da tabela, da chave estrangeira e das regras de posse."* Este RFC entrega tabela e chave estrangeira. A condição está satisfeita.

```python
@dataclass(frozen=True)
class SearchFilters:
    device_ids: frozenset[DeviceId] = frozenset()   # vazio = todos
```

`search_similar(embedding, limit, filters)` ganha um terceiro parâmetro. Um `SearchFilters` vazio produz exatamente a consulta de hoje, então os 27 testes de contrato do RFC-025 §10.2 continuam descrevendo o comportamento não filtrado, e o filtro ganha os seus.

O RFC-028 estende esta mesma estrutura com data. Introduzir o mecanismo aqui, com um filtro, e estendê-lo lá, com o segundo, segue o que o RFC-024 fez ao acrescentar `content_hash` ao `IndexMetadata` do RFC-020.

### 9.1 O que isso faz com o HNSW — a medir, não deduzir

Um filtro sobre uma busca vetorial aproximada não é gratuito e não é obviamente benéfico. O PostgreSQL não empurra um predicado arbitrário para dentro da travessia do grafo HNSW: ele aplica o filtro sobre o que o índice devolveu, de modo que uma varredura que explorou `ef_search` candidatos pode entregar **menos que `limit` linhas** depois de filtrar. O RFC-025 §7.3 já observou exatamente esse comportamento por acidente, com tuplas mortas no lugar de linhas filtradas.

Há três regimes plausíveis, e qual deles vale em qual escala é uma **medição, não uma dedução**:

| seletividade do filtro | comportamento esperado | efeito |
| --- | --- | --- |
| muito seletivo (um disco de vinte) | planejador abandona o HNSW, varredura sequencial sobre o subconjunto | rápido **e exato** |
| pouco seletivo (dezenove de vinte) | HNSW + pós-filtro | quase sem custo |
| intermediário | HNSW + pós-filtro que descarta demais | **retorna menos que `limit`** |

A faixa intermediária é o risco real. Mitigações conhecidas, em ordem de custo: `hnsw.iterative_scan` (pgvector ≥ 0,8), aumentar `ef_search` quando há filtro, ou índices HNSW parciais por dispositivo — viável precisamente porque a contagem de dispositivos é pequena e estável, ao contrário de um filtro por data.

Nada disso é escolhido neste documento. `experiments/rfc-027-devices/planner_check.py`
mede os três regimes com `EXPLAIN ANALYZE`, seguindo o RFC-025 §7. Medido com
**20.000 imagens distribuídas igualmente entre 20 dispositivos**,
`hnsw.ef_search = 40`, `limit = 10`:

| filtro | plano escolhido | linhas | tempo |
| --- | --- | --- | --- |
| nenhum | índice HNSW (aproximado) | 10/10 | 4,6 ms |
| 1 de 20 | bitmap sobre `uq_images_device_relative_path` (**exato**) | 10/10 | 7,9 ms |
| 2 de 20 | varredura sequencial (**exata**) | 10/10 | 11,8 ms |
| 5 de 20 | varredura sequencial (**exata**) | 10/10 | 23,8 ms |
| 10 de 20 | índice HNSW (aproximado) | 10/10 | 2,8 ms |
| 15 de 20 | índice HNSW (aproximado) | 10/10 | 3,0 ms |
| 19 de 20 | índice HNSW (aproximado) | 10/10 | 4,6 ms |
| 20 de 20 | índice HNSW (aproximado) | 10/10 | 3,9 ms |

Os dois regimes das pontas se confirmaram. Um filtro seletivo faz o planejador
abandonar o HNSW e varrer o subconjunto, que é **exato** e custa alguns
milissegundos; um filtro pouco seletivo mantém o HNSW e o pós-filtro descarta
quase nada — e é de fato mais rápido que a busca sem filtro, porque o mesmo
`ef_search` é gasto sobre menos candidatos.

**A faixa intermediária não apareceu nesta escala, e isso não é o mesmo que
mostrar que ela não existe.** Nenhuma das oito execuções devolveu menos que
`limit`. A razão é que o planejador trocou para varredura sequencial
exatamente onde o pós-filtro começaria a descartar demais: com 20.000 linhas
uma varredura completa é barata, então ele a preferiu em 2/20 e 5/20 em vez de
usar o índice. Esse cálculo é função do tamanho da tabela — quanto maior ela
fica, mais cedo o HNSW volta a parecer barato — de modo que a faixa pode se
abrir num acervo grande. `CORPUS_SIZE` é parametrizável no script
(`RFC027_CORPUS_SIZE`) exatamente para que essa pergunta seja medida e não
deduzida quando houver um acervo maior à mão.

Uma tentativa de medir com 100.000 imagens foi abortada: semear a tabela custa
tempo superlinear porque cada `INSERT` também atualiza o grafo HNSW, e a
semeadura ainda não havia terminado depois de vinte minutos. Fica registrado
como não medido, em vez de estimado.

Nenhuma mitigação foi adotada. Não há evidência de que alguma seja necessária
nesta escala, e adotar `hnsw.iterative_scan` ou índices parciais por
dispositivo sem uma medição que os justifique seria exatamente a dedução que
esta seção recusa.

**Explicitamente: o filtro não é justificado por desempenho.** Ele é justificado por utilidade — "procure só no HD que está na minha mão". Se a medição mostrar que ele também é mais rápido, ótimo; se mostrar que custa, ele continua valendo a pena e o custo fica registrado.

## 10. `Device` não é `Collection`

`Collection` está desenhada no `ARCHITECTURE.md` §15 como *"uma pasta indexada"*, com `root_path`, `model_name` e `model_version`. Um dispositivo é um volume físico. As duas coisas se confundem porque hoje ambas são zero linhas de código.

Elas divergem no primeiro caso real: um HD contém várias pastas indexadas em momentos diferentes, e o modelo de embedding é propriedade de *quando* algo foi indexado, não de *onde* está guardado. Fundir as duas obrigaria um disco inteiro a compartilhar uma versão de modelo, o que o `ARCHITECTURE.md` §21 identifica como a decisão que força reindexação completa.

`Collection` permanece placeholder. Este RFC não a constrói e não a apaga.

## 11. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Manter `path` absoluto e aceitar o reindex | 5 h de inferência por troca de letra, mais linhas duplicadas com embedding, ambas buscáveis (§2.1) |
| Número de série de 32 bits como identidade | Colide; o GUID de volume não custa mais para obter (§4.1) |
| Coluna `is_connected` | Errada em toda leitura posterior a um desplugue, sem evento que a corrija (§7) |
| Coluna `drive_letter` / `mount_point` | Reintroduz o dado instável que o RFC existe para remover (§4) |
| Reconciliar automaticamente por prefixo de `path` | A raiz veio do `--root` e nunca foi armazenada; não há informação para dividir (§6.3) |
| Apagar linhas não reconciliadas | Joga fora embeddings que custaram horas (§6.3) |
| `Device` = `Collection` | Divergem no primeiro caso real (§10) |
| Adiar a mudança de identidade para depois dos jobs | A janela sem chaves estrangeiras fecha (§6.2) |
| Um adaptador Linux não testado | Uma afirmação de portabilidade que ninguém verificou (§4.1) |

## 12. Não-objetivos

- Montagem automática, notificação de plugue/desplugue, monitoramento de pastas
- Filtro por data — RFC-028
- Jobs de indexação, indexação seletiva pela UI — RFC-029
- Caminho absoluto na resposta de busca, thumbnails, abrir no explorador — RFC-030
- `Collection`, `collection_id`, versionamento de modelo por coleção (§10)
- Adaptadores de identidade de volume para Linux e macOS (§4.1)
- Deduplicação entre dispositivos (a mesma foto em dois HDs continua sendo duas linhas)
- Qualquer mudança no modelo, no template de prompt ou na tradução

## 13. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **A migration é assistida e não pode ser automática** (§6.3) | Declarado. O comando de reconciliação é entregável, não instrução de README |
| **Reescrever a PK é seguro só enquanto não houver FK** (§6.2) | É a razão de ordenação deste RFC. Deixa de valer no RFC-029 |
| **Um clone byte a byte é indistinguível do original** (§4.1) | Limitação declarada, não resolvida |
| **Filtro pode retornar menos que `limit` sob HNSW** (§9.1) | A medir; mitigações conhecidas e não escolhidas |
| **Só Windows tem adaptador de identidade** (§4.1) | Deliberado; o discriminador evita migration para o segundo |
| **`% indexado` é da última varredura, não do disco** (§8) | Resolvido dizendo o que o número é, não escondendo o denominador |
| Reformatar um volume descarta suas linhas | Aceito — reformatar destrói as fotos (§4.1) |

Trabalho futuro: adaptadores Linux/macOS; detecção de disco clonado; deduplicação entre dispositivos; e mover a reconciliação para a UI quando o RFC-029 tiver onde colocá-la.

## 14. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/entities/device.py` | `Device` |
| `backend/app/domain/value_objects/device_id.py` | `DeviceId`, `VolumeIdentity` |
| `backend/app/domain/value_objects/search_filters.py` | `SearchFilters` (§9) |
| `backend/app/domain/repositories/device_repository.py` | A porta |
| `backend/app/infrastructure/database/models/device_model.py` | `DeviceModel` |
| `backend/app/infrastructure/persistence/postgres_device_repository.py` | |
| `backend/app/infrastructure/persistence/in_memory_device_repository.py` | |
| `backend/app/infrastructure/filesystem/volume_identity_provider.py` | Porta + adaptador Windows (§4.1) |
| `backend/app/infrastructure/workers/device_reconcile.py` | O comando de §6.3 |
| `backend/alembic/versions/*_create_devices_table.py` | |
| `backend/alembic/versions/*_enforce_device_ownership.py` | O passo 3 de §6.3 |
| `backend/tests/infrastructure/persistence/test_device_repository_contract.py` | |
| `backend/tests/infrastructure/filesystem/test_volume_identity.py` | |
| `experiments/rfc-027-devices/planner_check.py` | A medição de §9.1 |
| `docs/rfcs/rfc-027-dispositivos-e-identidade-de-volume.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/filesystem/image_identity.py` | `compute_image_id(device_id, relative_path)` (§6.1) |
| `backend/app/infrastructure/database/models/image_model.py` | `device_id`, `relative_path`; `path` removido |
| `backend/app/domain/entities/image.py` | `path` → `device_id` + `relative_path` |
| `backend/app/domain/repositories/image_repository.py` | `search_similar(..., filters)` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | O `WHERE` do filtro |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Idem, em memória |
| `backend/app/infrastructure/workers/indexing_worker.py` | Resolve o dispositivo antes de varrer |
| `backend/app/presentation/api/v1/routers/images.py` | Query param `device_id` |
| `backend/tests/infrastructure/persistence/test_search_similar_contract.py` | Casos de filtro |
| `ARCHITECTURE.md` | §15 ganha `Devices`; §23 perde "aplicação desktop" como pré-requisito disso |

## 15. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | **625 passados**, 58 desmarcados — a partir de 502 (o RFC dizia 471, desatualizado quando foi escrito) |
| `pytest -m slow` | **58 passados** — a partir de 56 |
| `black --check .` / `ruff check .` | **limpos** |
| `mypy` | **0 erros em 141 arquivos.** A linha de base de 5 erros já havia sido zerada em `ed1bdf4`; o critério "0 em código novo" está cumprido, e o projeto inteiro segue limpo |
| `alembic heads` | **head único**, `b8e4d2a13c75` |
| `alembic downgrade` até `26058b9e1d9a` e `upgrade` de volta | **exercitado** — ver a ressalva abaixo |
| Reconciliação sobre o banco de desenvolvimento | **45 linhas casadas, 45 reconciliadas, 0 perdidas**, `md5` do conjunto de embeddings idêntico antes e depois |
| Troca de letra simulada não produz id novo | **passa** — `test_a_drive_letter_change_does_not_produce_a_new_id` e, ponta a ponta, `test_a_drive_letter_change_does_not_reindex_anything` |
| Contrato de `search_similar` com filtro vazio | **passa** — os casos do RFC-025 continuam intactos nas três implementações, e `test_an_empty_filter_is_the_same_as_no_filter` fixa a equivalência |

### 15.1 O que o round trip do RFC-007 restaura, e o que não

`downgrade` até `26058b9e1d9a` funciona e restaura o **esquema**: `path`
volta NOT NULL e único, as colunas de dispositivo e suas restrições somem,
nenhuma linha é perdida e nenhum embedding é tocado (verificado pelo mesmo
`md5`).

Ele **não** restaura as *identidades* antigas, e não pode: recalcular um
`ImageId` pré-RFC-027 exige o caminho absoluto que este esquema deixou de
guardar, e o ponto de montagem de onde ele saiu nunca foi guardado (§6.3). Pela
mesma razão o `path` restaurado é relativo ao dispositivo, não absoluto.

A consequência prática é que `alembic upgrade head` sobre um banco com linhas
não reconciliadas **falha de propósito** e deixa o banco onde estava. O caminho
de volta é o de §6.3, em três passos, e foi o exercitado aqui:

```
alembic upgrade a7f3c1d20b64
python -m app.infrastructure.workers.device_reconcile --root PATH --label HD2
alembic upgrade head
```

Nenhuma inferência é refeita em nenhum dos três passos, que é a propriedade
que de fato importa.

### 15.2 Um defeito encontrado ao exercitar esse caminho

A primeira versão do comando de reconciliação **apagou as 45 linhas do banco de
desenvolvimento** durante justamente esse round trip, e o registro fica aqui
porque o modo de falha é instrutivo.

Depois de um `downgrade`, as linhas mantêm os ids *novos*. Ao subir de novo, o
id recalculado de uma linha é igual ao id que ela já tem — e o comando lia isso
como "outra linha já ocupa esta identidade", isto é, como a duplicata que §2.1
descreve, cuja correção é apagar a redundante. Não era uma colisão: a linha era
ela mesma. A verificação tem de ser `new_id != old_id` **antes** de qualquer
coisa ser chamada de duplicata.

Corrigido, e fixado por `test_a_row_whose_id_is_already_correct_is_not_deleted`.
O banco de desenvolvimento não foi repovoado, por decisão do autor.
