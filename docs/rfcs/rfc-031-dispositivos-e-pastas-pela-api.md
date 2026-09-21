# RFC-031 — Dispositivos, Volumes e Pastas pela API

**Status:** Implementado
**Depende de:** RFC-026 (camada HTTP), RFC-027 (dispositivos e identidade de volume), RFC-029 (jobs e escopos), RFC-030 (localização por requisição, guardas de ação local)
**Migration:** não — nenhuma tabela nova, nenhuma coluna nova; `alembic heads` continua `f4b9e2d7c615`
**Medição:** `experiments/rfc-031-devices-api/` — `measure_device_list.py` (§4.1, §4.2, §16.1–16.3) e `measure_folder_listing.py` (§8.2, §8.3, §15, §16.4–16.7), cada um com o `.log` ao lado

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*
> durante a implementação e devia ser escrito de volta aqui depois. **Isso foi
> feito:** não resta nenhum `TBM`, e cada número abaixo vem com a escala e as
> condições em que foi medido. O que a medição **não** cobriu — leitura fria de
> HD mecânico externo, em ambos os scripts — está dito onde aparece, em vez de
> preenchido por dedução.

> **Cinco afirmações deste documento estavam erradas contra o código ou contra
> a plataforma, e a implementação as corrigiu.** São §5 (`/volumes` como
> "projeção direta de `mounted_volumes()`" — a rota precisa de dois campos que
> aquele método não conhece), §6/§13 (`register_device()` "migra" para a
> Application — impossível na forma escrita, porque a assinatura importa
> Infrastructure três vezes), §13 (`list_covering` no port de jobs, e
> `main.py` registrando routers), §14 (o teste de assinatura "no molde de
> `test_reveal_guards.py`", que afirma `body_params == []` numa rota que tem
> corpo) e §15 (o índice B-tree que atenderia o `LIKE` ancorado à esquerda —
> **medido, e não atende**). Cada uma está marcada **Correção** na seção
> correspondente, com a frase original preservada à vista — a convenção do
> [README](README.md).
>
> **Duas medições contrariaram o desenho sem invalidá-lo**, e estão marcadas
> **Registro**: `has_children` é 98% do tempo de `/folders` (§8.3), e o
> `GROUP BY` de §8.2 faz `Seq Scan` — não por causa da colação, mas porque um
> dispositivo com 100 mil fotos *é* a tabela inteira (§15).

> **De onde veio.** A Sprint 5 nasceu de um mockup no Figma ([README](README.md)),
> e a revisão da segunda versão desse mockup encontrou o buraco que este RFC
> fecha: **as três telas desenhadas não têm de onde desenhar**. A barra lateral
> lista discos que nenhuma rota devolve; a tela de pastas lista pastas que
> nenhuma rota devolve; e o quarto disco do desenho só poderia ter sido
> registrado por linha de comando. O backend da Sprint 5 resolveu tudo o que
> acontece *depois* que um dispositivo existe, e nada sobre como ele passa a
> existir para um cliente que não é um terminal.

---

## 1. Contexto

Ao fim da Sprint 5 o sistema sabe: que discos existem (RFC-027), quando cada
foto foi tirada (RFC-028), como indexar pastas escolhidas sem bloquear a API
(RFC-029) e onde cada arquivo está agora, com thumbnail servida mesmo com o
disco na gaveta (RFC-030).

O que um cliente HTTP consegue fazer com isso hoje:

| o cliente quer | rota | existe? |
| --- | --- | --- |
| Buscar fotos | `GET /api/v1/images/search` | ✅ RFC-026 |
| Ver uma foto | `GET /api/v1/images/{id}` + `/thumbnail` | ✅ RFC-030 |
| Abrir no Explorer | `POST /api/v1/images/{id}/reveal` | ✅ RFC-030 |
| Enfileirar indexação | `POST /api/v1/jobs` | ✅ RFC-029 |
| Acompanhar / cancelar | `GET /api/v1/jobs/{id}`, `POST /jobs/{id}/cancel` | ✅ RFC-029 |
| **Listar os discos** | — | ❌ |
| **Registrar um disco** | — | ❌ (só `python -m app.infrastructure.workers.indexing_worker`) |
| **Renomear um disco** | — | ❌ (só `--label` num worker) |
| **Listar as pastas de um disco** | — | ❌ |
| **Saber se `/reveal` existe nesta instalação** | — | ❌ (§9) |

`POST /api/v1/jobs` recebe `{device_id, scopes[]}`. **Não há rota que devolva um
`device_id`, e não há rota que devolva um `scope` válido.** A rota mais
importante da Sprint 5 só é chamável por quem já consultou o banco à mão.

## 2. Problema

### 2.1 O dispositivo nasce num worker

`register_device()` mora em `infrastructure/workers/indexing_worker.py` e é
compartilhado com `device_reconcile`. É bom código e a decisão de compartilhá-lo
está certa (RFC-027 §14). Mas o único caminho até ele é um `argparse`:

```
python -m app.infrastructure.workers.indexing_worker --root D:\fotos --label HD2
```

Um produto cuja premissa é *"o fotógrafo aponta para os discos dele"* não pode
exigir um terminal para que um disco exista. E a consequência não é só de
usabilidade: **o registro é hoje um efeito colateral de indexar**. Registrar o
disco e escolher o que indexar são duas decisões do usuário separadas por
minutos ou dias, e estão fundidas numa invocação.

### 2.2 "Nenhum caminho entra" parece proibir registrar um disco

O RFC-030 §5.1 transformou uma convenção em propriedade de segurança:

> *"`/reveal` nunca aceita string de caminho. Teste de assinatura: handler,
> *dependant* e OpenAPI sem parâmetro além do id."*

Registrar um disco parece exigir exatamente o contrário — o usuário precisa
apontar para *algum lugar*, e `register_device()` recebe um `root: Path`. Tratar
as duas coisas como a mesma é o erro que este RFC precisa evitar, e é o mesmo
formato de erro que o RFC-030 §2 evitou ao separar os dois argumentos do RFC-026.

A propriedade do RFC-030 é sobre **agir sobre um arquivo do usuário a partir de
uma string que o cliente escolheu**. Não é sobre nomear um volume. §5 mostra que
a propriedade pode ser preservada inteira: o servidor enumera os volumes
montados e cunha um identificador opaco para cada um; o cliente devolve um
desses identificadores. O cliente nunca compõe um caminho, e um identificador
inventado não resolve para lugar nenhum.

### 2.3 A tela de pastas não tem fonte de dados — e nem poderia ter a que o desenho supõe

O mockup mostra, por pasta, um estado (`Indexado` / `Parcial 30%` / `Indexando`
/ `Não indexado`) e uma contagem de *"arquivos do scan"*. A contagem por pasta
**não existe e não é barata**: obtê-la significa varrer a subárvore, que é a
leitura de disco que o RFC-029 §7.2 já identificou como o custo dominante num
HD mecânico frio, e é precisamente por isso que `discovered_files` é um contador
crescente e não um denominador.

`Parcial · 30%` tem o mesmo defeito do `50% indexado` que o ARCHITECTURE.md §15
proíbe, numa forma pior: no nível do dispositivo existe ao menos um denominador
declarado com data (`last_scan_file_count`, `last_scan_at`); no nível da pasta
não existe denominador nenhum. **§8.2 tira a porcentagem por pasta do desenho.**

## 3. Decisão

| decisão | resultado |
| --- | --- |
| `GET /api/v1/devices` | Lista os dispositivos com `connected` resolvido **agora** (§4) |
| Porcentagem no corpo | **Não** — a API devolve as três partes (`indexed_images`, `last_scan_file_count`, `last_scan_at`); quem exibe monta o número **com a data** (§4.2) |
| `GET /api/v1/volumes` | Volumes montados agora, cada um com a identidade que o servidor cunhou (§5) |
| `POST /api/v1/devices` | Registra um volume **por identidade, nunca por caminho** (§5.1, §6) |
| Idempotência do registro | O id é derivado da identidade: registrar de novo é **200 no mesmo dispositivo**, nunca um segundo (§6.1) |
| `PATCH /api/v1/devices/{id}` | Só `label`. Nada mais é editável (§7) |
| `DELETE /api/v1/devices/{id}` | **Fora deste RFC** (§10) |
| `GET /api/v1/devices/{id}/folders` | **Um nível por requisição**, com `?path=` (§8) |
| Estado de uma pasta | Derivado de jobs + contagem indexada, **sem porcentagem** (§8.2) |
| `GET /api/v1/capabilities` | Como a UI descobre se `/reveal` existe, atrás do guarda de loopback (§9) |
| Migration | **Nenhuma.** Toda a informação já está em `devices`, `images` e `indexing_jobs` |
| Rota nova que indexe dentro da requisição | **Nenhuma** — a proibição do RFC-026 §3 continua valendo inteira |

## 4. `GET /api/v1/devices`

```json
{
  "devices": [
    {
      "id": "4f1c…",
      "label": "HD2",
      "filesystem_label": "Seagate Backup",
      "connected": true,
      "mount_point": "F:/",
      "total_bytes": 2000398934016,
      "first_seen_at": "2026-03-12T09:14:02Z",
      "last_seen_at":  "2026-09-21T11:02:55Z",
      "last_scan_at":  "2026-03-12T09:41:10Z",
      "last_scan_file_count": 48210,
      "indexed_images": 24104,
      "active_job": { "id": "a31f…", "status": "running" }
    }
  ]
}
```

`connected` e `mount_point` são **derivados por requisição**, exatamente como o
RFC-030 §4 fez para a imagem: `mounted_volumes()` é chamado **uma vez por
requisição** e a lista inteira é resolvida contra esse mapa. Não há cache entre
requisições — a regra de `DeviceLocator` é que nada pode guardar essa resposta,
porque ninguém avisa este processo quando o cabo é puxado.

`mount_point` aparece na resposta e **não** no banco. A distinção é a de sempre
(RFC-027 §4): é um fato sobre este instante, válido até o usuário desconectar.

### 4.1 Uma enumeração por requisição, não uma por dispositivo

O RFC-030 mediu `mounted_volumes()` em **0,19 ms** de mediana com um volume
montado, e já agrupa resultados de busca por `device_id` para enumerar uma vez
por página. A mesma regra vale aqui, e com mais força: uma lista de vinte discos
faria vinte enumerações do mesmo sistema de arquivos para responder à mesma
pergunta.

Custo da rota, medido ponta a ponta com 100 mil linhas em `images` e o
banco no contêiner (`measure_device_list.log`):

| N acrescentado | linhas servidas | mediana | enumerações por requisição |
| --- | --- | --- | --- |
| 1 | 2 | 90,6 ms | **1,00** |
| 4 | 5 | 88,2 ms | **1,00** |
| 20 | 21 | 85,1 ms | **1,00** |

**O tempo não cresce com N**, que é o resultado que interessa: as três
rondas diferem por ruído, não por dispositivo. O que domina os ~87 ms é o
`GROUP BY` de §4.2 sobre 100 mil linhas (71,9 ms sozinho), e ele também
não depende de N.

A contagem de enumerações é `1,00` exata em 31 requisições de cada ronda,
e `list_mounted()` — o método caro de §5 — foi chamado **zero** vezes.
Isso é verificado por teste que conta chamadas
(`test_device_use_cases.py`, N=20), não por inspeção: o JSON é idêntico
com uma enumeração ou com vinte, então nada além de um espião perceberia.

### 4.2 A API não calcula a porcentagem

O corpo devolve `indexed_images`, `last_scan_file_count` e `last_scan_at`, e
**nenhum campo `percent_indexed`**. Isso é deliberado e é a tradução direta do
ARCHITECTURE.md §15:

> *"`last_scan_file_count` é o denominador declarado de '% indexado'. […]
> anything displaying it must show the scan date too."*

Um campo `percent_indexed` seria consumível sem a data ao lado, e o primeiro
cliente que o exibisse sozinho reintroduziria o número confiantemente errado.
Devolver as partes torna a data **impossível de não ter em mãos** no ponto em
que o número é montado. É a mesma escolha que o RFC-029 fez ao mandar
`discovery_complete` junto com `discovered_files` em vez de mandar uma
porcentagem pronta.

`indexed_images` é um `COUNT(*)` por dispositivo sobre `images`, e exige um
método novo na porta `ImageRepository` (§13) — **`count_by_device()`, que
devolve um mapa, não um inteiro**. Um inteiro por id seriam N consultas
atrás de uma assinatura que parece uma só, que é o mesmo defeito de §4.1
uma camada abaixo.

Medido com 100 mil linhas (`measure_device_list.log`): **71,9 ms de
mediana** para o `GROUP BY` único, contra **180,0 ms** para 20 `COUNT(*)`
separados — 2,5× mais barato a N=20, e a diferença cresce com N enquanto
o `GROUP BY` não se move.

### 4.3 O que a resposta deliberadamente não tem

| campo ausente | por quê |
| --- | --- |
| `is_connected` armazenado | Estaria errado a partir do instante em que o cabo sai, e nada o corrigiria (RFC-027 §7) |
| `drive_letter` | É função da ordem de montagem; persistir é o defeito que o RFC-027 existe para remover |
| `percent_indexed` | §4.2 |
| Contagem de arquivos por pasta | §2.3, §8.2 |

## 5. `GET /api/v1/volumes`

A pergunta que antecede o registro: *o que está plugado nesta máquina agora, e o
que disso este sistema ainda não conhece?*

```json
{
  "volumes": [
    {
      "volume_identity": "\\\\?\\Volume{9f3a…}\\",
      "volume_kind": "windows-volume-guid",
      "mount_point": "G:/",
      "filesystem_label": "DJI_ARCHIVE",
      "total_bytes": 4000787030016,
      "device_id": null
    },
    {
      "volume_identity": "\\\\?\\Volume{1b77…}\\",
      "volume_kind": "windows-volume-guid",
      "mount_point": "F:/",
      "filesystem_label": "Seagate Backup",
      "device_id": "4f1c…"
    }
  ]
}
```

`device_id` não-nulo significa *"este volume já é um dispositivo registrado"* —
é o que permite à tela "Adicionar dispositivo" mostrar os discos já conhecidos
desabilitados, em vez de deixar o usuário registrar duas vezes e descobrir
depois que não aconteceu nada (§6.1).

A rota é uma projeção direta de `VolumeIdentityProvider.mounted_volumes()`
cruzada com `DeviceRepository.list()`. Volumes que se recusam a ser nomeados
(drive óptico vazio, volume BitLocker trancado) são pulados pelo adaptador, e
continuam pulados aqui — o RFC-027 já decidiu que um disco que não responde não
é motivo para deixar de reportar os que responderam.

> **Correção (§5).** A primeira frase acima está errada. `mounted_volumes()`
> devolve `dict[VolumeIdentity, Path]` e **não conhece nem
> `filesystem_label` nem `total_bytes`** — os dois campos que o corpo
> acima publica. Quem os conhece é `ResolvedVolume`, que só sai de
> `resolve(path)`, uma chamada por volume com um `GetVolumeInformationW`
> e um `GetDiskFreeSpaceExW` cada.
>
> Havia duas saídas. **Enriquecer `mounted_volumes()`** faria *toda*
> chamada pagar rótulo e capacidade — e `mount_point()` é chamado em cada
> página de busca, via `ResolveImageLocationUseCase`. Um
> `GetDiskFreeSpaceExW` num HD externo em repouso pode acordar o disco e
> bloquear por segundos: seria um custo novo no caminho quente para
> servir uma tela que quase nunca abre.
>
> **A implementação pagou o detalhe só onde ele foi pedido.** O port novo
> `VolumeCatalog` tem **dois** métodos, e existirem dois é a decisão:
> `mount_points()` para `GET /devices`, `list_mounted()` para
> `GET /volumes` e para o registro, que precisa gravar os dois campos na
> linha nova. Medido com um volume montado, quente
> (`measure_device_list.log`): `mount_points()` **0,18 ms** de mediana —
> o mesmo número que o RFC-030 mediu — contra **0,83 ms** para
> `list_mounted()`, **4,5×**. O fator cresce com o número de volumes, e o
> caso que justifica a separação, um HD mecânico externo em repouso, **não
> foi medido**: a máquina só tinha `C:` montado quando o script rodou.
>
> A rota é, portanto, uma enumeração **mais uma identificação por
> volume**, cruzada com `DeviceRepository.list()`. O resto do parágrafo
> continua valendo.

### 5.1 A propriedade do RFC-030 §5.1, preservada

O cliente recebe `volume_identity` e devolve `volume_identity`. Ele **nunca
compõe um caminho**, e o servidor **nunca aceita um**:

* `POST /devices` não tem parâmetro de caminho — nem no corpo, nem na query, nem
  no path. Isto é testado por assinatura, como o RFC-030 §5.1 testa `/reveal`:
  handler, *dependant* e OpenAPI;
* uma identidade inventada não resolve para nada. Ela é procurada no mapa que
  `mounted_volumes()` acabou de produzir, e o que não está lá é `409
  DeviceNotConnectedError` — um caminho inventado, por contraste, *é* um lugar,
  e essa é exatamente a diferença que torna esta rota segura e a outra forma não;
* o `root: Path` que `register_device()` exige é montado **pelo servidor**, a
  partir do `mount_point` que ele próprio acabou de enumerar.

O registro, portanto, não abre a superfície que o RFC-030 fechou. Ele usa o
mesmo desenho: **o servidor cunha o identificador, o cliente o devolve.**

## 6. `POST /api/v1/devices`

```
POST /api/v1/devices
{ "volume_identity": "\\\\?\\Volume{9f3a…}\\", "volume_kind": "windows-volume-guid", "label": "HD4" }

201 Created
{ "id": "7c2e…", "label": "HD4", "connected": true, "mount_point": "G:/", … }
```

Reusa `register_device()` sem reimplementá-lo, pelo motivo que o próprio
`device_reconcile` registra: as regras sobre quais campos sobrevivem a uma linha
existente são do tipo que diverge em duas cópias, e os dois chamadores mintam
ids a partir do dispositivo que produzem. A Application ganha
`RegisterDeviceUseCase`, e `register_device()` **migra do worker para a
Application** (§13) — hoje ele mora em Infrastructure e é importado por um
worker e por um script; com um terceiro chamador que é uma rota, a camada certa
deixa de ser discutível.

> **Correção (§6).** A conclusão está certa e a palavra "migra" está
> errada: a migração literal é **impossível**. A assinatura de
> `register_device()` recebe um `VolumeIdentityProvider`, devolve um
> `ResolvedVolume` e chama `compute_device_id()` — três importes de
> `app.infrastructure`, e `test_application_architecture.py` faz
> `ast.walk` sobre todo arquivo de `app/application/` e falha em qualquer
> um deles. Mover a função como está quebraria o teste que §14 exige que
> continue passando.
>
> **O que migrou foi a regra, não a função.** As duas coisas que estavam
> fundidas numa só foram separadas:
>
> 1. **nomear o volume** é uma pergunta ao sistema operacional, e os dois
>    chamadores a fazem de formas diferentes — a CLI tem um caminho
>    (`--root D:otos` → `resolve(root)`), a rota tem uma identidade
>    (procurar na enumeração). Ficou em Infrastructure, onde já estava;
> 2. **decidir que linha gravar** — a cascata de `label`, `first_seen_at`
>    preservado, `last_seen_at` movido, os contadores de scan intactos —
>    é a regra que a docstring dizia "divergir em duas cópias". Essa subiu
>    para `RegisterDeviceUseCase`, e é o que este RFC queria.
>
> `RegisterDeviceUseCase` expõe os dois caminhos: `execute()` acha a
> identidade na enumeração (ou 409), e `register()` aplica a regra de
> merge sobre um volume que outro já resolveu — o que a CLI chama.
> `MountedVolume` (Domain) e `VolumeCatalog` foram criados para isso, e
> `compute_device_id()` passou para `app/domain/services/device_identity.py`
> com o namespace UUID intacto, byte por byte.
>
> **A assinatura pública de `register_device()` não mudou**, e isso
> importa mais do que "um worker e um script" sugere: os chamadores são
> **seis** — `indexing_worker.main()`, `device_reconcile` (com
> `persist=False`), os dois backfills, `dataset_tools/seed_demo.py` e o
> script de medição do RFC-029 — e nenhum deles mudou uma linha.
> `test_cli_job_equivalence.py` é a rede que provou isso.

`label` é opcional. Ausente, vale a cascata que `register_device()` já
implementa: label existente → `filesystem_label` → o valor da identidade.

Erros: `409 DeviceNotConnectedError` quando a identidade não está montada agora;
`400 InvalidVolumeIdentityError` quando está vazia ou de um `kind` desconhecido.

### 6.1 Registrar duas vezes não cria dois

`DeviceId` é `uuid5` sobre a identidade do volume. Registrar um volume já
registrado é um **`200 OK` com o dispositivo existente**, não um `201` e não um
`409`:

* não é `201` porque nada foi criado, e um cliente que conte criações estaria
  contando errado;
* não é `409` porque o estado do sistema após a chamada é exatamente o que o
  chamador pediu. Um conflito descreveria um pedido que o estado atual recusa
  (`ConflictError`), e este não é recusado — já está satisfeito.

O `label` enviado numa segunda chamada **é aplicado**, porque `save()` é upsert
por desenho (RFC-027) e porque a alternativa — ignorar em silêncio — deixaria o
usuário renomeando um disco sem efeito. É o mesmo efeito que `PATCH` (§7), por
um caminho que o cliente já tinha.

## 7. `PATCH /api/v1/devices/{id}`

```
PATCH /api/v1/devices/4f1c…
{ "label": "HD2 — aéreas 2018" }   →   200
```

**Só `label`.** Todo o resto do `Device` é ou derivado (`id`), ou observado
(`filesystem_label`, `total_bytes`, `last_seen_at`), ou resultado de um scan
(`last_scan_at`, `last_scan_file_count`). Aceitar edição de qualquer um deles
seria deixar um cliente mentir sobre o mundo — e `last_scan_file_count` em
particular é o denominador de §4.2: editável, vira um número inventado com cara
de medição.

`404 DeviceNotFoundError` para id desconhecido. Não exige o disco conectado:
renomear é um fato sobre a nossa tabela, e o RFC-027 §2.3 é explícito que um
disco na gaveta continua sendo um dispositivo perfeitamente conhecido.

## 8. `GET /api/v1/devices/{id}/folders`

```
GET /api/v1/devices/4f1c…/folders?path=2018
```

```json
{
  "device_id": "4f1c…",
  "path": "2018",
  "parent": "",
  "folders": [
    { "name": "janeiro",   "path": "2018/janeiro",   "has_children": true,
      "indexed_images": 1204, "state": "indexed",
      "job": { "id": "b12c…", "status": "completed", "finished_at": "2026-03-14T02:10:00Z" } },
    { "name": "fevereiro", "path": "2018/fevereiro", "has_children": true,
      "indexed_images": 412,  "state": "indexing",
      "job": { "id": "a31f…", "status": "running",
               "processed_files": 412, "discovered_files": 980, "discovery_complete": true } },
    { "name": "março",     "path": "2018/marco",     "has_children": true,
      "indexed_images": 455,  "state": "partial",
      "job": { "id": "9d04…", "status": "cancelled",
               "last_processed_relative_path": "2018/marco/casamento/IMG_2201.JPG" } },
    { "name": "junho",     "path": "2018/junho",     "has_children": false,
      "indexed_images": 0,    "state": "never_indexed", "job": null }
  ]
}
```

`path` ausente ou vazio lista a raiz do dispositivo. Os caminhos saem com `/` em
qualquer plataforma, como o `ImagePath.__str__` do RFC-030 §4 — um cliente JSON
não deveria precisar saber qual sistema operacional respondeu para quebrar um
caminho em partes.

Exige o disco conectado: `409 DeviceNotConnectedError`. Aqui, ao contrário de
`PATCH`, precisamos de bytes — e o erro certo diz *qual disco plugar*, em vez de
dizer que a pasta não existe.

Validação do `path`: passa por `JobScope`, que já recusa `..`, âncora de unidade
e caminho UNC, e por `DeviceLocator.resolve_scope()`, que confirma que o
resultado está genuinamente **dentro** do dispositivo depois de resolver
junctions NTFS (RFC-029 §7.1). Um `path` que não resolve é `400
InvalidJobScopeError`. **Esta é a mesma validação que `POST /jobs` aplica**, e
tem que ser: o que esta rota devolve é literalmente o que o cliente vai mandar
de volta como `scopes[]`, então uma pasta listável e não-escopável seria um beco
sem saída na interface.

### 8.1 Um nível por requisição, não a árvore inteira nem uma lista plana

| alternativa | por que não |
| --- | --- |
| Lista plana do primeiro nível (o mockup v1) | Um acervo real é `2018/janeiro/casamento`. Indexar só pelo primeiro nível transforma "indexe o casamento" em "indexe 2018 inteiro" — 5 h de inferência para pegar 300 fotos |
| Árvore inteira numa resposta | Custo proporcional à árvore do disco, paga toda vez que a tela abre, para exibir quatro linhas. Num HD mecânico frio é a leitura que o RFC-029 §7.2 diz ser dominante |
| Um nível por requisição | O custo segue o que o usuário abriu. É um `readdir` por navegação |

### 8.2 O estado é derivado, e não tem porcentagem

`state` sai de um `enum` de cinco valores, calculado **no banco**, sem tocar no
disco além do `readdir` que lista os nomes:

| estado | como é derivado |
| --- | --- |
| `indexing` | Existe job `running` cujos `scopes` cobrem este caminho |
| `queued` | Existe job `pending` cujos `scopes` cobrem este caminho |
| `partial` | O job mais recente que cobriu o caminho terminou `cancelled` ou `failed` — o `last_processed_relative_path` dele vai na resposta, e é o que a UI usa para dizer *onde* parou |
| `indexed` | O job mais recente que cobriu o caminho terminou `completed` |
| `never_indexed` | Nenhum job cobriu o caminho **e** `indexed_images == 0` |

"Cobrem" é uma relação de prefixo em ambas as direções: um job com escopo
`2018` cobre `2018/janeiro`, e um job com escopo `2018/janeiro/casamento` cobre
parcialmente `2018/janeiro` — este segundo caso entra como `partial`, porque é
literalmente o que ele é. `scopes == []` significa o dispositivo inteiro e cobre
tudo (RFC-029 §7.1).

**Nenhum campo de porcentagem por pasta**, pelo motivo de §2.3: não existe
denominador. `indexed_images` é uma contagem exata de um fato nosso — quantas
imagens daquela subárvore têm linha em `images` — e é o que a UI deve mostrar:
*"1.204 indexadas"* é verdadeiro; *"30% indexada"* não tem como ser.

`indexed_images` por pasta é um `COUNT(*)` com `relative_path LIKE 'prefixo/%'`
por linha listada. Isto é **N consultas para N pastas** se escrito
ingenuamente; a implementação deve fazer **uma** consulta agrupada por prefixo
de primeiro nível abaixo de `path`.

Foi o que ela fez, com
`split_part(substr(relative_path, :cut), '/', 1)` agrupado. Medido com
100 mil linhas e 40 pastas (`measure_folder_listing.log`): **247 ms** de
mediana para a consulta agrupada, contra **2 279 ms** para 40 `COUNT(*)`
separados — **9,2× mais barato**, e a razão cresce com o número de
pastas. Sobre o índice, veja a **Correção (§15)**: ele não é usado, e a
decisão de não criar outro está registrada lá com o plano na mão.

Duas armadilhas da consulta agrupada, ambas fixadas no teste de contrato
(`test_image_counting_contract.py`) porque as duas implementações do port
erram em direções diferentes:

* o `GROUP BY` traz **toda** subpasta do nível, inclusive as que o
  `readdir` não viu — uma pasta renomeada no disco continua com as linhas
  sob o nome velho. A resposta lista o que o `readdir` viu; as contagens
  órfãs são descartadas, e uma pasta sem linhas recebe `0`;
* imagens **direto** em `path` caem sob o próprio nome de arquivo, porque
  nem o SQL nem a versão em memória sabem distinguir arquivo de pasta sem
  tocar no disco. São descartadas pelo mesmo filtro, e isso é deliberado:
  decidir ali o que é diretório seria a consulta adivinhando algo que não
  enxerga.

> **Correção (§8.2 e §13).** O §13 pede
> `IndexingJobRepository.list_covering(device_id, paths)`. **Esse método
> não foi criado**, e não deve ser. Escrevê-lo em SQL significaria
> reproduzir a relação de cobertura **nas duas direções** — um job em
> `2018` cobre `2018/janeiro`, e um job em `2018/janeiro/casamento` cobre
> parcialmente `2018/janeiro` — em **duas** implementações do port, com a
> comparação feita em texto. Que é exatamente o erro que `JobScope` existe
> para não cometer: `2018` não é prefixo de `2018b`, mas
> `'2018b' LIKE '2018%'` é verdadeiro.
>
> A regra já estava escrita, em `JobScope.contains()`, testada em
> `test_job_scope.py`, e é regra de Domain. `ListDeviceFoldersUseCase`
> chama `list(device_id=...)` — um `SELECT` mais o `selectin` dos escopos
> — e deriva os cinco estados em Python, nas duas direções. A lista vem
> mais nova primeiro por contrato, e há no máximo um job ativo por
> dispositivo (índice parcial do RFC-029 §9), então o primeiro job que
> cobre o caminho é a resposta.
>
> **O risco a declarar:** o histórico de um disco cresce sem limite, e um
> dia essa lista fica grande. A mitigação não é SQL de prefixo, é um
> `limit` no port quando a medição mostrar que importa — dívida registrada
> em §8.3 junto com a paginação.
>
> **Um sexto estado caiu por exclusão e está implementado:** *nenhum job
> cobriu o caminho e `indexed_images > 0`*. A tabela acima exige as duas
> metades para `never_indexed`, então isso é `partial` — são linhas que a
> busca já devolve, e chamá-las de "nunca indexada" seria falso. É o caso
> de um disco indexado antes de os jobs existirem.

### 8.3 O que esta rota não faz

Não conta arquivos no disco, não recursa, não pagina. A ausência de paginação é
uma dívida declarada: uma pasta com milhares de subpastas devolve todas.
Medida antes de resolver, e os números dizem que ela vai precisar ser
resolvida (`measure_folder_listing.log`, disco quente, SSD do sistema):

| pasta | pastas devolvidas | mediana ponta a ponta | corpo JSON |
| --- | --- | --- | --- |
| raiz do dispositivo | 1 | 204 ms | 0,2 KB |
| `2018` | 41 | 145 ms | 5,0 KB |
| `2018/wide` | **5 000** | **947 ms** | **689 KB** |

689 KB e quase um segundo para desenhar uma lista é a justificativa de
paginar, e ela fica registrada em vez de ser implementada aqui: a
paginação é não-objetivo deste RFC (§10) e agora tem um número por trás.
O `list_folders()` sozinho, sem HTTP, custa **1 083 ms** nas mesmas 5 000
entradas — ou seja, o custo é o disco e o `has_children`, não a
serialização.

> **Registro (§8.3): `has_children` é 98% do tempo desta rota.** A seção
> acima diz "não recursa", e é verdade — mas o corpo de §8 tem
> `has_children` por pasta, e saber se `2018/janeiro` tem subpastas exige
> **abrir `2018/janeiro`**. Com 40 subpastas são 41 `readdir`, não 1, e
> §8.1 vende a rota como "um `readdir` por navegação". Isso não estava
> errado, estava **não declarado**.
>
> Medido sobre 41 pastas, quente: a listagem com `has_children` custa
> **10,95 ms**; a mesma listagem só com nomes custa **0,21 ms**. O campo
> acrescenta **10,75 ms**, ou **0,262 ms por pasta** — 98% do total.
>
> **O campo fica.** Sem ele a UI desenha uma seta de expansão numa pasta
> folha e o usuário descobre o erro ao clicar, que é um defeito visível
> trocado por um custo que, em números absolutos, ainda é 11 ms. O
> adaptador curto-circuita no primeiro subdiretório encontrado
> (`any(e.is_dir() for e in os.scandir(child))`), então uma pasta que tem
> subpasta custa uma entrada, não uma listagem. **A leitura fria de HD
> mecânico não foi medida**, e é justamente onde esses 0,262 ms por pasta
> podem virar outra coisa; se virarem, a discussão é do RFC-032, com este
> número como ponto de partida.

## 9. `GET /api/v1/capabilities`

```json
{ "local_file_actions": false, "platform": "win32", "version": "0.5.0" }
```

> **Correção (§9).** O `"0.5.0"` do exemplo é o número da sprint, não a
> versão do pacote. `settings.project_version` é **`0.1.0`** e o
> `pyproject.toml` concorda; a rota devolve o que a configuração diz, em
> vez de o projeto ser renumerado para bater com uma ilustração.
> `platform` é `sys.platform` — literalmente `win32` nesta máquina, que é
> o valor do exemplo.

A UI precisa saber se deve desenhar o botão *"abrir no Explorer"*. Hoje não
consegue: com `allow_local_file_actions=False`, `/reveal` responde **404 com o
corpo de uma rota inexistente** (RFC-030 §6), e descobrir isso tentando
significaria um botão que some depois de clicado.

### 9.1 Por que atrás do guarda de loopback

Uma rota que anuncia a configuração desfaz metade do 404 — o ponto dele é que
*"um cliente sondando a API não aprende nada que um caminho desconhecido não
lhe diria"*. A saída é que esta rota carregue **o guarda 2 e só ele**
(`require_loopback_client`): quem está nesta máquina — a UI local, que é o
único cliente legítimo — recebe a resposta; qualquer outro recebe 403 e não
aprende nada sobre a configuração.

O guarda 1 **não** vai aqui, e a distinção importa: se a rota também sumisse com
a configuração desligada, ela não conseguiria responder *"desligado"*, que é
justamente a resposta que a UI precisa.

## 10. Não-objetivos

| fora deste RFC | por quê |
| --- | --- |
| Rescan sob demanda (atualizar `last_scan_file_count`) | É minutos de leitura num HD frio, então é um job, e um job de tipo novo é coluna nova em `indexing_jobs` — migration que este RFC não quer. **Próximo RFC**, e a UI já mostra a data do scan justamente para o usuário saber quando ele envelheceu |
| `DELETE /api/v1/devices/{id}` | O repositório recusa enquanto houver imagens, e essa recusa está certa (RFC-027 §6.3). A rota precisa de um desenho de produto — *"esquecer o disco e os embeddings"* é destrutivo e caro de reverter — que não cabe junto com o resto |
| Notificação de montagem/desmontagem | Continua sendo polling do cliente. Um `WM_DEVICECHANGE` é uma mensagem de janela num processo que não tem janela |
| Paginação de pastas | §8.3 |
| A interface | É o RFC-032. Este RFC é a precondição dela |

## 11. Alternativas rejeitadas

| alternativa | por que não |
| --- | --- |
| `POST /devices {path: "D:\\fotos"}` | Reabre a superfície que o RFC-030 §5.1 fechou, e por um ganho nulo: o servidor já sabe enumerar os volumes (§5.1) |
| `GET /devices` devolvendo `percent_indexed` | §4.2 — um número consumível sem a data ao lado |
| Fundir `/volumes` em `/devices` | São dois conjuntos diferentes: um é o que este sistema conhece, o outro é o que está plugado agora. A interseção é útil e está em `/volumes.device_id`, mas a união numa lista só faria o cliente filtrar de novo |
| WebSocket para o estado de conexão | Mesma decisão do RFC-029 §7.2: polling em localhost custa quase nada, e um canal persistente adiciona reconexão e um caminho assíncrono pela API para economizar requisições baratas |
| Contagem de arquivos por pasta vinda do disco | §2.3 — é a varredura que o RFC-029 recusou fazer duas vezes |
| Expor `allow_local_file_actions` no `/health` | `/health` é a rota que um monitor externo chama; anunciar configuração ali é o oposto de §9.1 |

## 12. O que muda no mockup

Este RFC **corrige o desenho** em dois pontos, e o registro fica aqui porque a
correção veio da API e não do design:

1. **A coluna "Arquivos do scan" por pasta sai.** Não existe esse número (§2.3).
   No lugar: `1.204 indexadas`, que é exato.
2. **"Parcial · 30%" vira "Parcial — parou em `…/IMG_2201.JPG`".** O estado é
   verdadeiro e útil; a porcentagem não tem denominador (§8.2). A única
   porcentagem da interface continua sendo a do dispositivo, sempre com a data
   do scan ao lado.

O resto do mockup v2 sobrevive sem mudança, incluindo a decisão que ele já tinha
acertado sem saber: **o usuário escolhe pastas numa lista e nunca digita um
caminho**, que é a propriedade de §5.1 desenhada antes de ser especificada.

## 13. Arquivos

A tabela abaixo é a **entregue**, com as três linhas que o rascunho
errou marcadas *(corrigida)* e explicadas logo depois.

| arquivo | o que aconteceu |
| --- | --- |
| `backend/app/domain/value_objects/mounted_volume.py` | **Novo** *(não previsto)*. `MountedVolume`: o que `ResolvedVolume` é, do lado do Domain (§6, Correção) |
| `backend/app/domain/value_objects/folder_state.py` | **Novo** *(não previsto)*. Os cinco estados de §8.2 como `enum`; *quais* estados existem é regra de Domain, derivá-los é Application |
| `backend/app/domain/services/volume_catalog.py` | **Novo** *(não previsto)*. `VolumeCatalog`, com os dois métodos de §5 (Correção) |
| `backend/app/domain/services/device_identity.py` | **Movido** de `app/infrastructure/filesystem/`, com o namespace UUID intacto (§6, Correção). Sem shim de reexport |
| `backend/app/application/use_cases/register_device.py` | **Novo.** `RegisterDeviceUseCase` — a *regra* de `register_device()`, não a função (§6, Correção) |
| `backend/app/application/use_cases/describe_devices.py` | **Novo** *(não previsto)*. `DescribeDevicesUseCase`: uma enumeração e as contagens, para N dispositivos ou para um. Três rotas precisam da mesma decoração, no molde de `ResolveImageLocationUseCase` |
| `backend/app/application/use_cases/list_devices.py` | **Novo.** As linhas mais o describer, ordenadas por `label` (§4.1) |
| `backend/app/application/use_cases/list_mounted_volumes.py` | **Novo.** §5 |
| `backend/app/application/use_cases/rename_device.py` | **Novo.** §7 |
| `backend/app/application/use_cases/list_device_folders.py` | **Novo.** §8; o `readdir` entra por uma porta, não por `pathlib` |
| `backend/app/domain/services/device_locator.py` | `list_folders(device, scope) -> list[FolderEntry] \| None` *(corrigida)* — devolve `FolderEntry(name, has_children)`, não `str`, porque `has_children` só o adaptador pode pagar (§8.3, Registro) |
| `backend/app/domain/repositories/image_repository.py` | `count_by_device()` e `count_by_path_prefixes()` (§4.2, §8.2) |
| `backend/app/domain/repositories/indexing_job_repository.py` | **Intocado** *(corrigida)* — `list_covering` **não foi criado**; veja a Correção em §8.2 |
| `backend/app/infrastructure/filesystem/volume_catalog.py` | **Novo** *(não previsto)*. `MountedVolumeCatalog` sobre `WindowsVolumeIdentityProvider` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py`, `in_memory_image_repository.py` | As duas implementações dos métodos novos; `test_image_counting_contract.py` roda contra as duas mais o dublê de `tests/application/fakes.py` |
| `backend/app/infrastructure/filesystem/mounted_device_locator.py` | `list_folders()`, mais `_locate()` compartilhado com `resolve_scope()` — a checagem de contenção é a propriedade dos dois, e duas cópias dela seriam duas chances de perdê-la |
| `backend/app/infrastructure/workers/indexing_worker.py` | `register_device()` encolheu para um adaptador; **a assinatura pública não mudou** e os seis chamadores não foram tocados |
| `backend/app/presentation/api/v1/routers/devices.py` | **Novo.** §4, §5, §6, §7, §8 |
| `backend/app/presentation/api/v1/routers/capabilities.py` | **Novo.** §9 |
| `backend/app/presentation/schemas/device_schema.py` | **Novo.** `DeviceDetailSchema`, `VolumeSchema`, `FolderSchema`, `RegisterDeviceRequestSchema`, `RenameDeviceRequestSchema` |
| `backend/app/presentation/schemas/capabilities_schema.py` | **Novo** *(não previsto)*. `CapabilitiesSchema`; §9 não é um dispositivo |
| `backend/app/presentation/dependencies/__init__.py` | `get_volume_catalog()` e os seis provedores novos |
| `backend/app/presentation/api/v1/__init__.py`, `routers/__init__.py` | **Onde os routers entram** *(corrigida)* |
| ~~`backend/main.py`~~ | **Não mudou** *(corrigida)* |

> **Correção (§13).** Três linhas do rascunho:
>
> * *"`backend/main.py` | Registra os dois routers"* — `main.py` tem
>   quatro linhas e só reexporta `app`. Quem inclui routers é
>   `app/presentation/api/v1/__init__.py`, e quem os exporta é
>   `routers/__init__.py`; foram esses dois que mudaram;
> * *"`list_covering(device_id, paths)`"* — não foi criado, e a
>   justificativa está na Correção de §8.2;
> * *"`list_folders(device, path) -> list[str] | None`"* — devolve
>   `FolderEntry`, com `has_children` já resolvido. Uma lista de nomes
>   crus obrigaria a Application a abrir cada pasta para saber se tem
>   filhas, o que é tocar no sistema de arquivos de uma camada que não
>   pode.
>
> Os arquivos marcados *(não previsto)* são consequência das correções de
> §5 e §6 — o port novo, o value object que ele devolve e o adaptador que
> o implementa — mais o `enum` de §8.2 e o describer compartilhado por
> três rotas.

Nenhuma migration: `alembic heads` continua `f4b9e2d7c615`, um só.
Nenhum arquivo em `domain/entities/`.

## 14. Plano de testes

| o que | como |
| --- | --- |
| `GET /devices` faz **uma** enumeração | Espião no `DeviceLocator` conta chamadas com N=20 dispositivos; falha em 2 |
| `connected` reflete o momento | Mesmo dispositivo, dois requests, locator mudando entre eles → `true` depois `false` |
| Nenhuma rota aceita caminho no registro | Teste de assinatura sobre handler, *dependant* e OpenAPI, no molde de `test_reveal_guards.py` |
| Registro duplicado | Dois `POST` com a mesma identidade → 201 depois 200, **um** dispositivo, `label` do segundo aplicado (§6.1) |
| Registro de volume ausente | Identidade fora de `mounted_volumes()` → 409, e `save()` nunca chamado |
| `PATCH` recusa tudo menos `label` | Corpo com `last_scan_file_count` → 422 e linha intacta |
| `PATCH` funciona com disco na gaveta | Locator vazio → 200 |
| `/folders` com disco na gaveta | 409, e o corpo nomeia o dispositivo |
| `/folders` recusa escape | `..`, `C:\`, UNC, e uma junction apontando para fora → 400, nenhum `readdir` fora do dispositivo |
| Os cinco estados | Fixtures de job cobrindo cada linha da tabela de §8.2, incluindo `scopes == []` |
| `partial` por cobertura parcial | Job com escopo `2018/janeiro/casamento` → `2018/janeiro` é `partial` |
| Contagem agrupada | 40 pastas produzem **uma** consulta, não 40 |
| `/capabilities` de cliente remoto | 403, corpo sem a configuração |
| `/capabilities` com a configuração desligada | 200 de loopback, `local_file_actions: false` — não 404 (§9.1) |
| Camadas | `test_ai_layer_boundaries.py` e `test_application_architecture.py` continuam passando com `register_device` na Application |

Todas as linhas acima estão cobertas, em
`tests/application/test_device_use_cases.py` (os contadores),
`tests/presentation/test_devices_api.py`,
`tests/presentation/test_device_registration_guards.py`,
`tests/presentation/test_capabilities_api.py`,
`tests/infrastructure/filesystem/test_folder_listing.py`,
`tests/infrastructure/filesystem/test_volume_catalog.py` e
`tests/infrastructure/persistence/test_image_counting_contract.py`.
Suíte: **1 429 → 1 616 testes**, todos verdes, mais 58 `slow` verdes.

> **Correção (§14).** *"Teste de assinatura sobre handler, dependant e
> OpenAPI, no molde de `test_reveal_guards.py`."* O molde afirma, entre
> outras coisas, `assert dependant.body_params == []`. **`POST /devices`
> tem corpo** — é o que o cliente usa para dizer qual disco — então
> copiar o teste dá um teste que falha, e "consertá-lo" apagando a linha
> dá um teste que não afirma nada.
>
> A propriedade aqui não é *"não há entrada além do id"*, é **"nenhuma
> entrada é um lugar"**. O que
> `test_device_registration_guards.py` afirma:
>
> * os parâmetros do handler são exatamente `request`, `response`,
>   `use_case`, `describer` — os três últimos injetados pelo FastAPI, fora
>   do alcance do cliente;
> * `path_params`, `query_params`, `header_params` e `cookie_params` são
>   todos `[]`, na rota e nas dependências: a identidade entra **só** pelo
>   corpo;
> * os campos do modelo de corpo são **exatamente**
>   `{volume_identity, volume_kind, label}` — igualdade de conjuntos, não
>   `in`, para que um `path` acrescentado depois quebre o teste — e o
>   schema publicado no OpenAPI tem essas três propriedades e nenhuma
>   outra;
> * comportamento: um corpo com `"path"` e `"root"` a mais é ignorado e
>   nada é aberto; e **uma identidade inventada não resolve para nada** —
>   409 com `save()` nunca chamado. Esse último é o argumento inteiro:
>   um caminho inventado *é* um lugar, uma identidade inventada não está
>   no mapa que o servidor acabou de produzir.
>
> Dois testes que a tabela acima não pedia e que a implementação
> acrescentou: `extra="forbid"` **não** é o padrão do Pydantic v2 — sem
> ele o `PATCH` com `last_scan_file_count` responderia **200** e
> descartaria o campo em silêncio, então `RenameDeviceRequestSchema` é o
> único schema do projeto que o declara; e `volume_kind` é `str` no
> schema, porque declará-lo como `VolumeKind` trocaria o **400** que §6
> especifica por um 422 de validação levantado antes do use case.

## 15. Riscos

| risco | mitigação |
| --- | --- |
| `count_by_path_prefixes` lento sem índice adequado | Medido em §8.2 antes de decidir por índice; o `LIKE 'prefixo/%'` é ancorado à esquerda, que é o caso que um índice B-tree sobre `(device_id, relative_path)` atende |
| Pasta com milhares de subpastas | Declarado em §8.3; medir antes de paginar |
| Mover `register_device` quebra as duas CLIs | Uma mudança de import, coberta por `test_cli_job_equivalence.py`, que já existe para garantir que CLI e job façam a mesma coisa |
| `/capabilities` vira depósito de configuração | Três campos, e o critério para um quarto é *"a UI desenha diferente por causa dele"* |
| A UI tratar `connected: false` como erro | É o cenário que o RFC-030 §4.1 chama de "a resposta, não a falha"; o RFC-032 tem que desenhá-lo, e o mockup v2 já desenha |

> **Correção (§15): o índice não é usado, e a razão não é a que se
> esperava.** A primeira linha da tabela afirma que o `LIKE` ancorado à
> esquerda *"é o caso que um índice B-tree sobre
> `(device_id, relative_path)` atende"*. Isso foi verificado com
> `EXPLAIN (ANALYZE, BUFFERS)` e 100 mil linhas, não lendo a migration
> (`measure_folder_listing.log`), e o plano é **`Seq Scan`** nos dois
> casos — raiz e um nível abaixo.
>
> A suspeita inicial era a colação: o contêiner é `pgvector/pgvector:pg17`
> e o `.env` não define `POSTGRES_INITDB_ARGS`, então o cluster vem em
> `en_US.utf8` — confirmado, `datcollate = en_US.utf8` — e um B-tree comum
> nessa colação não atende `LIKE 'x%'` sem `text_pattern_ops`. Mas essa
> **não é a razão operante aqui**, e vale registrar a diferença: o filtro
> é `device_id = X` sobre um dispositivo que tem 100 000 das 100 020
> linhas da tabela. Nenhum índice ajuda a ler quase todas as linhas; o
> planejador escolheria `Seq Scan` mesmo com o índice perfeito.
>
> **Decisão: nenhum índice novo, e nenhuma migration.** Os 247 ms de
> mediana são de um dispositivo que *é* a tabela inteira, que é o pior
> caso possível e não o caso comum — com vários discos registrados o
> filtro por `device_id` passa a excluir a maior parte das linhas, e aí a
> pergunta sobre o índice fica interessante de verdade. Criar um
> `(device_id, relative_path text_pattern_ops)` agora seria uma migration
> que este RFC disse que não teria, contra um número medido no cenário
> em que ele não ajudaria. Fica como decisão do RFC-032, com o plano
> acima na mão.

## 16. Medições feitas

Tudo abaixo foi medido numa máquina só — Windows 10, Intel i7-7700
(Family 6 Model 158), Python 3.12.9, PostgreSQL 17.10 no contêiner,
disco de sistema SSD. Os `.log` ao lado dos scripts têm a saída
completa; como todo `*.log` deste repositório, eles ficam fora do
controle de versão (`.gitignore`), do mesmo jeito que os do RFC-030.

| # | o quê | arquivo | resultado |
| --- | --- | --- | --- |
| 1 | `GET /devices` com N=1, 4, 20 dispositivos | `measure_device_list.py` | **90,6 / 88,2 / 85,1 ms** de mediana — não cresce com N (§4.1) |
| 2 | Chamadas a `mounted_volumes()` por requisição | idem | **1,00** em 31 requisições de cada ronda; `list_mounted()` **zero** (§4.1) |
| 3 | `count_by_device()` com 100 mil imagens | idem | **71,9 ms** agrupado contra **180,0 ms** em 20 consultas — 2,5× (§4.2) |
| **novo** | `mount_points()` **vs** `list_mounted()` | idem | **0,18 ms** contra **0,83 ms**, 4,5×, com **um** volume montado e quente (§5, Correção) |
| 4 | `/folders` na raiz e em `2018`, disco quente | `measure_folder_listing.py` | **204 ms** (1 pasta) e **145 ms** (41 pastas) ponta a ponta (§8.3) |
| 5 | idem, HD mecânico **frio** | idem | **Não medido.** A árvore foi montada no diretório temporário do sistema, em SSD, e lida uma vez antes de cada cronometragem. O RFC-029 §7.2 achou a leitura fria dominante num HD externo; nada aqui confirma nem contraria isso |
| 6 | Contagem agrupada com 40 pastas / 100 mil imagens | idem | **247 ms** agrupada contra **2 279 ms** em 40 consultas — 9,2×; plano `Seq Scan` (§8.2, §15) |
| 7 | `readdir` numa pasta com 5.000 entradas | idem | **1 083 ms** no locator, **947 ms** ponta a ponta, **689 KB** de JSON (§8.3) |
| **novo** | Quanto de `/folders` é o `has_children` | idem | **10,75 ms de 10,95 ms — 98%**, ou 0,262 ms por pasta, quente (§8.3, Registro) |
| **novo** | Colação do cluster e plano do `GROUP BY` | idem | `datcollate = en_US.utf8`; `Seq Scan`, e o índice não ajudaria de qualquer forma (§15, Correção) |

**Linha de base da suíte**, tomada antes da primeira linha de código e
depois da última, com o Postgres no ar:

| | antes | depois |
| --- | --- | --- |
| `pytest` | 1 429 passaram, 58 deselecionados | **1 616 passaram, 58 deselecionados** |
| `pytest -m slow` | 58 passaram | 58 passaram |
| `mypy` | zero erros, 124 arquivos | zero erros, 138 arquivos |
| `alembic heads` | `f4b9e2d7c615` (único) | `f4b9e2d7c615` (único) |
