# RFC-039 — Dispositivos e indexação

**Status:** Implementado, **com dois critérios pendentes de um HD externo**. A medição de §13, na máquina do RFC-036, cumpriu o terceiro critério de §8: uma pasta com 5 000 subpastas, do clique à lista desenhada, em 861 ms, sem nenhuma tarefa longa da página. O primeiro foi ensaiado no `C:`, pela interface, do começo ao fim — registrar pelo diálogo, duas pastas, indexar, cancelar e indexar de novo, com os corpos das requisições sem caminho —, e o segundo não foi medido: não havia um HD externo à mão, o risco que §9 registrava, e com ele fica sem conferência a entrega do `WM_DEVICECHANGE` à janela (§2.4). Parada na tela de pastas por 10 minutos, a página pediu o `/devices` 60 vezes e nada mais. A implementação achou que o `ready` repete o estado do `job_runner` sem a linha que um `child` anterior trouxe, e a casca a guarda (§14.1); e a verificação visual achou um leitor de cartão vazio oferecido no diálogo de adicionar, corrigido na interface (§14.1)
**Etapa:** 2 do [roadmap do frontend](ROADMAP-frontend.md#etapa-2--dispositivos-e-indexação-rfc-039--seg-26-e-ter-27)
**Depende de:** RFC-037 (a casca, a ponte, o cliente tipado, o polling com o foco, o diálogo de fechar com um job em andamento, e [§16.1](rfc-037-fundacao-e-casca-do-frontend.md#161-o-que-a-medição-e-o-ambiente-contrariaram), o `job_runner` que carrega o modelo antes do laço), RFC-038 (o detalhe da foto, o `redactUrl()`, as mensagens por status e por rota e o padrão do marco; [§12](rfc-038-busca.md#12-o-que-passa-adiante)), RFC-027 §8 (o denominador da porcentagem), RFC-029 (§7 a API dos jobs, §8 o cancelamento cooperativo, §10 o checkpoint, §15 o disco desplugado no meio), RFC-031 (as rotas; §4.1 o custo do `/devices`, §8.3 as 5 000 subpastas, §10 o `WM_DEVICECHANGE`, §12 as correções do mockup), RFC-036 §4.4 (os eventos `ready` e `child`)
**Bloqueia:** o RFC-041, cujo teste em conta limpa indexa o dataset de demonstração pela interface, por estas telas
**Migration:** não. `last_scan_at` e `last_scan_file_count` existem desde o RFC-027
**Medição:** nenhum piloto. Os números citados vêm dos RFCs 023, 029, 031, 036, 037 e 038, cada um com a seção, e os do RFC-031 foram medidos noutra máquina (i7-7700, SSD, PostgreSQL 17 no Docker; RFC-031 §16). Os fatos sobre o código vêm da leitura do branch `frontend` em `c376fab`, e cada um diz o arquivo. A medição da implementação é `experiments/rfc-039-devices-indexing/measure_devices.py`, rodado em 2026-10-21 na mesma máquina, com o log ao lado (§13)

> **Um RFC curto, como o roadmap pediu** ([O prazo](ROADMAP-frontend.md#o-prazo-e-o-que-ele-obriga)): as decisões, os critérios de pronto com no máximo três números `TBM`, e o que ficou de fora. O que não é medição diz que é escolha ou hipótese.

---

## 1. Contexto

O RFC-037 pôs na barra lateral a lista de dispositivos com o rótulo e a conexão, e deixou os cartões do Figma para este RFC (`frontend/src/shell/Shell.tsx`). O RFC-038 ligou a busca. A API já registra e renomeia discos, lista as pastas um nível por vez com o estado de cada uma, e cria, acompanha e cancela jobs. Falta a interface de tudo isso: sem ela, o único jeito de pôr fotos no índice ainda é o terminal.

Este RFC desenha os cartões, o diálogo de adicionar um disco, a tela de pastas, o painel do job e a faixa do `job_runner` indisponível. A leitura do código achou que três itens do roadmap não funcionariam como escritos (§2.1 a §2.3), e que o motivo de uma decisão do RFC-031 deixou de existir (§2.4). O backend ganha uma mudança, sem migration e sem rota nova (§4).

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| Os cartões: o rótulo, a conexão por polling a cada poucos segundos, as fotos indexadas, a porcentagem só com `last_scan_file_count` e com a data do scan ao lado, e o job ativo | §5.2. Ninguém grava o denominador hoje (§2.1, §4.1). A conexão chega pela mensagem de volume da janela, com o polling como rede (§2.4, §5.3) |
| Adicionar: `GET /volumes`, um rótulo e o `POST /devices` idempotente | §5.4 |
| Renomear: `PATCH /devices/{id}` | §5.5 |
| As pastas: um nível por requisição com a trilha; os cinco estados, com *"parou em …"*; *"1.204 indexadas"* e a seta só com `has_children`; *"Conecte o HD2 para ver as pastas"* no 409 | §5.6 |
| Escolher pastas e indexar, com o 409 de job ativo | §5.6, §5.7. O mesmo 409 quer dizer outra coisa também (§2.5) |
| O progresso: `discovered_files` crescendo, sem porcentagem antes de `discovery_complete`; os contadores; o último arquivo | §5.7. A descoberta só termina junto com o job (§2.2) |
| Cancelar, com *"cancelando…"* até o fim | §5.7 |
| O aviso *"nunca indexado"* levando a esta tela | §5.11 |
| A faixa do `job_runner` `unavailable`, com a linha do evento | §5.8. A página não recebe o evento que veio antes dela (§2.3) |
| *Critérios:* o fluxo com um HD externo de verdade; o disco desplugado em `TBM` s; as 5 000 subpastas sem travar | §8 |

## 2. O que o estado atual muda no roadmap

A leitura do código achou nove pontos. Nenhum muda uma das decisões D1 a D5. O §2.1 acrescenta ao backend o que o roadmap não pedia; o §2.2 tira dele uma porcentagem que não existiria; o §2.3 e o §2.4 mudam a casca.

### 2.1 Ninguém grava o denominador da porcentagem

`last_scan_at` e `last_scan_file_count` são lidos pelo `GET /devices` e preservados pelo registro (`register_device.py`), e nenhum código os escreve: o `grep` acha as colunas, o mapeamento e a cópia do registro, e nenhuma atribuição. O RFC-027 §8 previu que *"uma varredura é barata (`stat`, sem inferência) e é o que o RFC-029 vai disparar de qualquer forma"*, e o RFC-029 não a ligou ao dispositivo. O rescan sob demanda saiu no RFC-031 §10 e continua fora (roadmap).

Sem mudança, a porcentagem do cartão, que o roadmap pede e o Figma desenha, nunca apareceria. **O job do disco inteiro grava o denominador assim que termina de listar a raiz** (§4.1). É o scan que ele já faz, e nada novo é lido do disco.

### 2.2 A descoberta só termina com o job, e fica muda enquanto lista

Três fatos do `filesystem_image_provider.py` e do `job_progress_observer.py`, que juntos mudam o painel de progresso:

- **`discovery_complete` só fica verdadeiro no fim.** O executor consome a descoberta aos poucos, em janelas de 512 arquivos (`metadata_prefetch_size`), e o `discover()` só chama `discovered(…, complete=True)` depois do último arquivo do último escopo. O `discovered_files` corre no máximo uma janela à frente dos processados. A porcentagem *"depois de `discovery_complete`"* do roadmap apareceria nos últimos 512 arquivos de um job de horas.
- **A listagem não dá sinal nenhum.** Cada escopo é um `sorted(scope_root.rglob("*"))`, que consome a árvore inteira antes do primeiro arquivo, e a docstring de `_discover_under()` diz: *"a cold walk of a large disk produces no callback at all until it finishes"*. Num HD frio e grande, o job fica `running` com tudo em zero por um tempo que ninguém mediu (RFC-029 §15).
- **Os contadores recomeçam do zero numa retomada.** O observador começa cada execução com `IndexingProgress()` zerado, e o `record_progress()` grava o que ele tem. Fechar a janela com um job em andamento devolve o job à fila (D3), e a próxima abertura o retoma do checkpoint com os contadores em zero.

**O painel mostra os contadores, e não uma porcentagem do job** (§5.7). A barra existe só para o disco inteiro, e vem do dispositivo, e não do job: `indexed_images` sobre o denominador que §4.1 grava no começo do mesmo job. Ela cresce a cada lote gravado, já existe numa retomada e não depende de `discovery_complete`. Um job de pastas não tem denominador nenhum, e não tem barra. Os contadores zerados na retomada são do RFC-029, e ficam como estão (§9).

### 2.3 A página não fica sabendo de um `job_runner` que caiu antes dela

O roadmap pede uma faixa *"quando o launcher manda um `child` com `job_runner` `unavailable`"*. A casca repassa os eventos `child` à página só quando ela está aberta (`perform()`, em `electron/main.ts`), e nenhuma página os escuta hoje: o `onLauncherEvent` da ponte não tem consumidor em `src/`. Há dois buracos:

- **Um executor que desistiu antes do `ready`** não manda `child` nenhum para a página: o estado vai no campo `indexing` do `ready` (`supervisor.py`), que a casca lê e não repassa. É o caso dos modelos ausentes ou corrompidos, em que o `job_runner` recusa a subida com o código 78 e o launcher não o reinicia (RFC-035 §6.5, RFC-036 §6.1).
- **Uma recarga perde a faixa.** O *"Reconectando…"* recarrega a página (RFC-037 §6.3), e um evento que já passou não volta.

**A casca guarda o último estado do `job_runner`, e a página pergunta por ele ao abrir** (§5.9), além de escutar os eventos seguintes.

### 2.4 O motivo do RFC-031 §10 para não ouvir o `WM_DEVICECHANGE` acabou

O RFC-031 §10 manteve a conexão por polling porque *"um `WM_DEVICECHANGE` é uma mensagem de janela num processo que não tem janela"*. Agora há uma. O Electron entrega as mensagens da janela ao processo principal (`BrowserWindow.hookWindowMessage`, só no Windows), e o Windows manda `DBT_DEVICEARRIVAL` e `DBT_DEVICEREMOVECOMPLETE` a toda janela de nível superior quando um volume chega ou sai, sem registro. **A casca avisa a página, e a página pergunta ao `/devices` na hora** (§5.9). A entrega das duas mensagens à janela do Electron é a hipótese de que o segundo critério depende, e a medição a confere (§8).

O polling continua, como rede, porque a página também roda num navegador (`npm run dev:web`) e numa janela que não é dona do launcher. Mas ele não precisa ser de poucos segundos: o `GET /devices` custou cerca de 87 ms com 100 000 fotos, quase todos de um `GROUP BY` (RFC-031 §4.1, noutra máquina), e durante uma indexação ele disputa a CPU com o CLIP. **10 s**, uma escolha, contra os 30 s de hoje (`src/api/queries.ts`).

### 2.5 O mesmo 409 quer dizer duas coisas, e as mensagens são em inglês

O `POST /jobs` responde 409 para *"o disco não está conectado"* e para *"já há um job ativo nesse disco"* (`create_indexing_job.py`), e o corpo dos dois é `{"detail": "<texto do domínio>"}` (`error_handlers.py`). O mesmo vale para o `error_message` de um job que falhou, que é texto livre de exceção, em inglês. A interface é só PT-BR e não lê texto da API para decidir nada (RFC-038 §2.8).

**Depois de um 409, a interface relê o `/devices` e diz o que ele mostra**: desconectado, ou com um job ativo (§5.10). As duas condições são estados que a API publica, e o 409 só diz que um deles mudou desde a última leitura. O `error_message` aparece como a linha de diagnóstico, embaixo de um título em português, como o `search_detail` do RFC-037.

**Rejeitado: um campo `code` no corpo dos erros.** Mudaria todas as respostas de erro e o contrato dos testes delas para resolver dois casos que a interface já resolve com uma leitura que ela faria de qualquer forma.

### 2.6 A seleção só inclui, e os arquivos da raiz só entram com o disco inteiro

Um escopo é uma pasta e tudo abaixo dela; `scopes` não tem exclusão; e `GET /devices/{id}/folders` lista pastas, e não arquivos (`device_schema.py`). Duas consequências para a tela:

- **Marcar uma pasta marca a subárvore**, e as subpastas de uma pasta marcada aparecem marcadas e travadas. Tirar uma subpasta de dentro de uma pasta marcada pediria uma exclusão que a API não tem.
- **As fotos soltas na raiz do disco** só entram por *"Indexar o disco inteiro"* (`scopes: []`), e a tela diz isso quando a raiz não tem subpastas ou quando o disco nunca foi indexado.

### 2.7 As pastas vêm na ordem do Python, e podem ser 5 000

`list_device_folders.py` ordena por `folder.name`, a ordem dos códigos de caractere: `Z` antes de `a`, e `10` antes de `2`. O Explorer põe `2` antes de `10`, sem distinguir maiúscula. Na busca, a ordem é o resultado, e a interface não toca nela (RFC-038 §5.3); aqui ela é só apresentação. **A interface reordena com `Intl.Collator("pt-BR", { numeric: true, sensitivity: "base" })`**.

E o RFC-031 §8.3 mediu uma pasta com 5 000 subpastas em 947 ms e 689 KB, noutra máquina. O tempo é do servidor, e o desenho de 5 000 linhas é da página. **A lista é janelada**, com linhas de altura fixa, e um filtro por nome aparece acima de 50 pastas (§5.6). A paginação continua fora (roadmap).

### 2.8 O rótulo não é único, e o default dele pode ser um GUID

A coluna `label` não tem `unique` (`device_model.py`), e um rótulo omitido no registro cai no rótulo do volume e, sem ele, na identidade, que é um GUID (`register_device.py`). Dois *"HD2"* na barra lateral, ou um cartão com a identidade do volume no lugar do nome, são a mesma falha vista de dois lados. **A interface sempre manda um rótulo, sugerido, e recusa um repetido antes de enviar** (§5.4, §5.5). A API não muda: a CLI continua podendo repetir, e isso fica declarado.

### 2.9 A máquina das medições tem um volume só

O RFC-037 §16.2 registrou que esta máquina tem só o `C:`. Dois critérios do roadmap pedem um disco de verdade nas mãos de alguém: registrar um HD externo e desplugá-lo com a tela aberta. **A medição pede que quem a roda pluge e despluge o disco na hora indicada**, e as 5 000 subpastas ficam numa árvore criada no `C:`, registrado como dispositivo na raiz de dados da medição (§8).

## 3. Decisão

| decisão | resultado |
| --- | --- |
| O denominador | O job do disco inteiro grava `last_scan_file_count` e `last_scan_at` ao terminar de listar a raiz, antes do primeiro arquivo, por um `UPDATE` das duas colunas (§4.1) |
| A porcentagem do cartão | `indexed_images` sobre o denominador, arredondada para baixo, sempre com a data do scan; nada sem denominador, e nenhum número acima de 100% (§5.2) |
| O progresso | Os contadores e o último arquivo salvo. A barra só no disco inteiro, vinda do dispositivo; nenhuma porcentagem do job (§5.7) |
| A conexão | A mensagem de volume da janela pede o `/devices` na hora; o polling cai para 10 s (§5.3, §5.9) |
| O `job_runner` | A casca guarda o último estado, a página pergunta ao abrir e escuta os eventos; *"Indexar"* desabilitado com `unavailable` (§5.8) |
| Os 409 | A interface relê o `/devices` e diz o que ele mostra (§5.10) |
| A tela de pastas | `/dispositivos/:id?pasta=…`, reordenada como o Explorer, janelada, com filtro acima de 50 pastas. Nenhum polling: relida ao navegar e quando o job do disco muda de estado (§5.3, §5.6) |
| A seleção | Um conjunto de caminhos canônicos por dispositivo, em que a pasta mãe absorve as filhas, perdido numa recarga (§5.6) |
| Adicionar e renomear | Um diálogo, o único lugar que pergunta ao `/volumes`; o rótulo obrigatório, sugerido e sem repetição; o renomear no cabeçalho da tela de pastas (§5.4, §5.5) |
| A casca | `hookWindowMessage` para os volumes, `indexing()` na ponte e um marco por listagem de pastas (§5.9) |

## 4. O backend

### 4.1 O scan do disco inteiro grava o denominador

- **Quando:** num job sem escopos, assim que o `sorted(rglob)` da raiz do disco termina, antes do primeiro arquivo. Não no fim do job: o denominador é o resultado da listagem, e a listagem já acabou.
- **O número:** as entradas da listagem com extensão suportada e fora das pastas excluídas, contadas sobre a lista que o `discover()` já tem na memória, sem `stat`. É o *"arquivos vistos na última varredura"* do RFC-027 §8, e numa retomada inclui os arquivos que o checkpoint pula, porque a listagem é sempre da árvore inteira. Uma pasta chamada `x.jpg` conta como arquivo: é o preço de não abrir nada, e fica declarado (§9).
- **O caminho:** o `IndexingObserver`, a porta do Domain, ganha `listed(file_count)`, com corpo vazio, para que o observador nulo e os dublês de teste não mudem. O `FilesystemImageProvider` a chama só quando percorre a raiz, sem escopos. O `JobProgressObserver` a repassa a uma função que o `JobRunner` lhe dá; o `JobRunner` confere que o disco continua montado no mesmo ponto em que o job começou, porque uma listagem sobre um volume que sumiu pode terminar em silêncio (o motivo do `_completion_of()`), e grava.
- **`DeviceRepository.record_scan(device_id, file_count, scanned_at)`** é um `UPDATE` das duas colunas, e não um `save()`: o `save()` reescreve a linha inteira a partir de uma cópia, e a cópia do executor é do começo do job, com o rótulo de antes de um renomear. Um id desconhecido não grava nada. Nos dois repositórios, com teste de contrato.
- **Nunca levanta.** Os callbacks do observador rodam dentro do gerador da descoberta, e uma exceção ali fecha o gerador e trunca o scan em silêncio (a docstring de `_write()`). Uma falha é um WARNING no `job_runner.log`, e o job continua.
- **Um job de pastas não grava nada:** a contagem de duas pastas não é o denominador do disco.
- **A API não muda.** Os dois campos já estão no `GET /devices`, e o `openapi.json` fica igual.

**Rejeitados:** um tipo de job *"só scan"*, que é uma coluna nova em `indexing_jobs` (RFC-031 §10) e o rescan que o roadmap deixou fora; e contar no `GET /devices/{id}/folders`, que andaria a subárvore a cada abertura da tela (RFC-031 §2.3).

## 5. A interface

### 5.1 As telas e a URL

| URL | o quê |
| --- | --- |
| `/dispositivos/:id` | A tela de pastas, na raiz do disco |
| `/dispositivos/:id?pasta=2018/junho` | Num nível abaixo. O valor é o `path` canônico que a API devolveu |

O diálogo de adicionar não tem URL: abre sobre a tela em que se está, e uma recarga o fecha. A faixa do `job_runner` é da casca da página, em toda tela. O `?pasta=` sai do `shell.log` pelo `redactUrl()` do RFC-038 §5.6, como a consulta.

### 5.2 Os cartões da barra lateral

Um cartão por dispositivo, na ordem da API (por rótulo), o do dispositivo aberto realçado. Um clique abre a tela de pastas. Embaixo da lista, *"+ Adicionar dispositivo"*. Sem dispositivos: *"Nenhum dispositivo registrado"* e o mesmo botão.

| no cartão | de onde | como |
| --- | --- | --- |
| O rótulo | `label` | |
| A conexão | `connected`, `mount_point` | O ponto verde e *"F:"*; o ponto cinza e *"desconectado"* |
| As fotos | `indexed_images` | *"24.104 fotos indexadas"*. Zero e sem job: *"nunca indexado"* |
| A porcentagem | `indexed_images`, `last_scan_file_count`, `last_scan_at` | *"50% no scan de 12/03"*, com o ano quando não é o atual, a regra do `lastIndexedText()`. Arredondada para baixo, para 100% querer dizer todos; *"menos de 1%"* entre zero e um. Sem denominador, nada. Com um denominador zero, *"o scan de 12/03 não achou fotos"*. Acima de 100%, que é um disco de que se apagaram fotos depois do scan, nenhum número: *"o disco mudou desde o scan de 12/03"* |
| O job | `active_job` e `GET /jobs/{id}` | *"Na fila"*, *"Indexando · 3.204 agora"* ou *"Cancelando…"* |

A conta é uma função pura, `scanShare()`, testada sem DOM (§7). É a única porcentagem da interface, como o RFC-031 §12 quer, e nunca sai sem a data.

### 5.3 O que se pergunta à API, e quando

| rota | quando | por quê |
| --- | --- | --- |
| `GET /devices` | A cada 10 s, quando a janela volta ao foco, e logo depois de cada mensagem de volume (§5.9) | A conexão aparece em toda tela. Os 10 s são a rede para quando a mensagem não vem (§2.4). A pílula de dispositivos da busca usa a mesma consulta |
| `GET /jobs/{id}` | A cada 2 s enquanto o job está `pending` ou `running`, e uma vez quando ele chega a um estado final | Os contadores são gravados a cada lote de 8 e a cada janela de 512 (RFC-029 §7.3), e o executor pergunta à fila a cada 2 s (`job_poll_interval`). Uma linha só, por id |
| `GET /jobs?device_id=` | Ao abrir a tela de pastas, depois de um `POST /jobs`, e quando o job ativo termina | O job mais recente do disco, para o painel. A lista vem do mais novo para o mais velho |
| `GET /devices/{id}/folders` | Ao navegar, quando o job do disco muda de estado, e quando o disco volta | Nem no foco nem por tempo: cada listagem lê o disco, um `readdir` por pasta para o `has_children` (RFC-031 §8.3), e no disco que está sendo indexado isso disputa com o job |
| `GET /volumes` | Ao abrir o diálogo, a cada mensagem de volume com ele aberto, e em *"Procurar de novo"* | É a rota cara: ler a capacidade pode acordar um disco que estacionou as cabeças (`devices.py`). Nunca por tempo |

Quando um job chega a um estado final, a página invalida o `/devices`, os níveis de pastas daquele disco, a lista de jobs dele e os jobs concluídos da tela inicial. As buscas em cache não mudam sozinhas: um gesto as refaz (RFC-038 §14.2).

### 5.4 Adicionar um dispositivo

O diálogo lista o `GET /volumes`, um volume por linha: o ponto de montagem (*"F:"*), o rótulo do volume e a capacidade (*"2 TB"*).

- **Um volume com `device_id`** aparece como *"já é o HD2"*, com *"Abrir"*, que leva à tela de pastas. Não há botão de adicionar: registrá-lo de novo não criaria nada (RFC-031 §6.1).
- **Um volume sem `device_id`** tem um campo de rótulo e *"Adicionar"*. O campo vem com o rótulo do volume, ou, sem ele, com *"HD"* e o menor número que nenhum dispositivo usa. Vazio, o botão fica desabilitado; repetido, sem distinguir maiúscula e sem os espaços das pontas, também, com *"Já existe um HD2"* (§2.8).
- **`POST /devices`** com `volume_identity`, `volume_kind` e o rótulo, e nada mais. O 201 e o 200 levam ao mesmo lugar: a tela de pastas do dispositivo, onde se escolhe o que indexar.
- **Nenhum volume**: *"Nenhum disco conectado. Conecte o HD e ele aparece aqui."* Um disco plugado com o diálogo aberto aparece sozinho, pela mensagem de volume.

### 5.5 Renomear

No cabeçalho da tela de pastas: o rótulo, e um botão *"Renomear"* que o troca por um campo. Enter salva por `PATCH /devices/{id}`; Esc cancela. As regras do campo são as de §5.4. Funciona com o disco desconectado (RFC-031 §7), e a resposta, que tem a forma de um item do `/devices`, entra direto no cache.

### 5.6 A tela de pastas

**O cabeçalho:** o rótulo, com o renomear; *"conectado em F:"* ou *"desconectado"*; e a linha do cartão, com as fotos e a porcentagem. *"Indexar o disco inteiro"* fica aqui. Abaixo, o painel do job mais recente do disco, se houver um (§5.7).

**A trilha:** *"HD2 › 2018 › junho"*, montada do `path` da resposta, cada parte um link para o seu nível. A raiz é o rótulo.

**A lista:** uma linha por pasta, com a caixa de marcar, o nome, *"1.204 indexadas"* (sem nada com zero) e o estado. O nome é um link para o nível de baixo só com `has_children`, com a seta; sem ele, é texto. A ordem é a do `Intl.Collator` de §2.7. A lista é janelada: linhas de 44 px, só as visíveis mais dez de cada lado no DOM, por uma função pura, `visibleRange()`, sem dependência nova; cada linha leva `aria-setsize` e `aria-posinset`. Acima de 50 pastas, um campo *"Filtrar pastas"*, sem distinguir maiúscula nem acento. A altura, o lado e o limite do filtro são escolhas.

**O estado de cada pasta**, do `state` e do `job` da resposta:

| `state` | `job.status` | a linha diz |
| --- | --- | --- |
| `indexing` | `running` | *"Indexando"* |
| `queued` | `pending` | *"Na fila"* |
| `indexed` | `completed` | *"Indexada em 14/09"*, do `finished_at` |
| `partial` | `cancelled` | *"Parcial — a indexação parou em …/junho/IMG_2201.JPG"*, do `last_processed_relative_path`, cortado à esquerda, com o caminho inteiro no `title`. Sem checkpoint: *"Parcial — cancelada antes do primeiro arquivo"* |
| `partial` | `failed` | *"Parcial — a indexação falhou em …/junho/IMG_2201.JPG"* |
| `partial` | `completed` | *"Parcial — só parte das subpastas foi indexada"*: o job cobria uma pasta de dentro |
| `partial` | nenhum | *"Parcial"*: fotos indexadas sem um job que as explique, como as da CLI |
| `never_indexed` | nenhum | *"Nunca indexada"* |

O checkpoint de um job do disco inteiro diz onde *o job* parou, e pode estar noutra pasta. O texto diz isso (*"a indexação parou em"*), e a interface não tenta decidir se a pasta foi coberta comparando caminhos: é a comparação que o RFC-029 §10 mostrou dar três respostas diferentes.

**A seleção** é um conjunto de caminhos canônicos, por dispositivo, guardado na tela enquanto se navega dentro do disco. Marcar uma pasta tira do conjunto as que estão abaixo dela, como o `normalize_scopes()` faria. Dentro de uma pasta marcada, as linhas aparecem marcadas e travadas, com *"incluída em 2018"* (§2.6). Uma recarga perde a seleção, e isso fica declarado (§9).

**A barra de ação**, presa embaixo: *"3 pastas selecionadas"*, *"Limpar"* e *"Indexar as pastas"*. Ela e *"Indexar o disco inteiro"* ficam desabilitadas, com o motivo escrito ao lado, quando o disco está desconectado, quando ele já tem um job ativo, e quando o `job_runner` está indisponível (§5.8).

**Os estados da tela:**

| quando | a tela mostra |
| --- | --- |
| O `/devices` não tem o id, ou a rota responde 404 | *"Esse dispositivo não está registrado."*, com um link para o início |
| O disco desconectado | O cabeçalho, o painel do job e *"Conecte o HD2 para ver as pastas."*, sem pedir a listagem. A seleção fica. Quando o disco volta, a listagem é pedida sozinha |
| A listagem em andamento há mais de 300 ms | *"Lendo as pastas do HD2…"* |
| Um 400 | *"A pasta junho não está mais no HD2."*, com *"Voltar para 2018"*, o `parent` |
| Nenhuma subpasta | *"Nenhuma subpasta em junho."* Na raiz: *"As fotos que estiverem direto na raiz entram com Indexar o disco inteiro."* |
| As pastas | A lista |

O disco desconectado vem do `/devices`, e não de um 409 da listagem: a tela muda quando o cartão muda, e não espera uma requisição que vai falhar. O 409, que só acontece numa corrida, leva à mesma tela e invalida o `/devices`.

### 5.7 Indexar, acompanhar e cancelar

**Indexar** é `POST /jobs` com o `device_id` e os caminhos da seleção, ou com `scopes: []` para o disco inteiro. O disco inteiro pede confirmação na página: *"Indexar o HD2 inteiro? Num disco com dezenas de milhares de fotos, isso leva horas. Dá para cancelar a qualquer momento, e o que já foi feito fica."*, com o número do último scan quando há um. O 202 entra no cache do job, a seleção é limpa, e a tela e o cartão passam a mostrá-lo.

**O painel** mostra o job mais recente do disco:

| o job | o painel diz |
| --- | --- |
| `pending` | *"Na fila."* Com outro disco indexando: *"Começa quando a indexação do HD3 terminar."* Senão: *"Começa em instantes."* Logo depois de abrir o app, o executor ainda carrega o modelo (RFC-037 §16.1), e a espera é essa |
| `running`, tudo em zero | *"Lendo a lista de arquivos do HD2…"*, com uma barra indeterminada, e *"Num disco grande, isto leva alguns minutos, e nada aparece até terminar."* (§2.2) |
| `running` | *"3.204 indexadas agora · 1.200 já estavam no índice · 3 falharam"* e *"Último salvo: …/junho/DJI_0042.JPG"*. No disco inteiro, com o denominador gravado (§4.1), a barra do dispositivo: *"25% do disco · 12.000 de 48.210 no scan de hoje"*. Num job de pastas, nenhuma barra. E *"Cancelar"* |
| `running`, `cancel_requested` | *"Cancelando… o lote em andamento termina primeiro."* Até cerca de 3,4 s, um lote inteiro a `batch_size` 8; uma observação deu 1,01 s (RFC-029 §8) |
| `completed` | *"Concluída às 14:02: 3.204 indexadas, 1.200 já estavam, 3 falharam."* Com falhas: *"Os arquivos que falharam estão no log."* e *"Abrir a pasta de logs"* |
| `cancelled` | *"Cancelada — parou em …/IMG_2201.JPG. O que foi indexado fica, e a próxima indexação pula."*, e *"Indexar de novo"* |
| `failed`, com o disco desconectado agora | *"A indexação parou porque o HD2 foi desconectado. O que foi indexado fica."* *"Indexar de novo"* espera o disco |
| `failed` | *"A indexação falhou."*, o `error_message` como linha de diagnóstico, *"Abrir a pasta de logs"* e *"Indexar de novo"* |

*"Indexar de novo"* manda os `scopes` do job, que são os normalizados. É o *"cancelar e indexar de novo"* do primeiro critério, e o segundo job anda depressa pelo que o primeiro já fez: as fotos voltam como *"já estavam no índice"* (RFC-029 §8). A barra do disco inteiro só aparece com o denominador porque o job a deixa de pé até sobre uma retomada, em que os contadores do job recomeçam do zero (§2.2).

**Cancelar** é `POST /jobs/{id}/cancel`. Um `pending` volta `cancelled` na hora; um `running` volta com `cancel_requested`, e o painel diz *"Cancelando…"* até o estado final, que o polling de 2 s traz. Um 409 quer dizer que o job terminou antes: a página relê o job, sem mensagem.

Nenhuma estimativa de tempo restante (§10).

### 5.8 O `job_runner` indisponível

O estado vem da casca (§5.9): `running`, `restarting` ou `unavailable`, com o `detail` do evento.

- **`unavailable`:** uma faixa no alto do conteúdo, em toda tela: *"A indexação está indisponível. A busca continua funcionando, e nenhum disco é indexado até o SolidVision ser aberto de novo."*, o `detail` como linha de diagnóstico e *"Abrir a pasta de logs"*. Os botões de indexar ficam desabilitados com esse motivo. O launcher não reinicia mais um filho que chegou aí (RFC-036 §6.1), e por isso o texto fala em abrir de novo.
- **`restarting`:** nenhuma faixa. Se havia um job `running`, o painel acrescenta *"O executor da indexação reiniciou. O job volta à fila sozinho, até 2 minutos depois do último sinal."*, e o texto fica até o job mudar de estado, e não até o executor voltar: o job continua `running` até o reaper o devolver, depois de 120 s sem heartbeat (`job_stale_timeout`, RFC-036 §6.3), e o `child` `running` do executor novo chega antes disso.
- **`null`:** num navegador e numa janela que não é dona do launcher (`already_running`), que não recebe eventos dele (RFC-037 §6.3). Nada aparece, e a interface não afirma que a indexação funciona.

### 5.9 A casca

- **As mensagens de volume.** `hookWindowMessage(0x0219)`, o `WM_DEVICECHANGE`, na janela principal. O `wParam` chega como um `Buffer`; `0x8000` (`DBT_DEVICEARRIVAL`) e `0x8004` (`DBT_DEVICEREMOVECOMPLETE`) viram um `devices-changed` para a página, agrupado em 250 ms, porque um disco pode ter mais de um volume. As outras são ignoradas. Uma linha no `shell.log`, sem a letra. O filtro e o agrupamento são uma função pura, num arquivo sem Electron, como o `events.ts`.
- **O estado do `job_runner`.** O processo principal o guarda: o `indexing` do `ready`, com `detail` nulo, e cada `child` com `name` `job_runner`, com o `state` e o `detail`. Um launcher novo o apaga. A página o pede por `ipcRenderer.invoke("indexing")` ao montar a casca, e os eventos seguintes chegam pelo `onLauncherEvent` que já existe (§2.3).
- **Um marco por listagem de pastas**, `reportFolders({ folders, responseMs, paintedMs })`, como o `reportSearch` do RFC-038 §5.6: do gesto à resposta e ao quadro seguinte à lista pintada. Sem o caminho. São os números do terceiro critério.
- **A ponte** ganha `onDevicesChanged`, `indexing` e `reportFolders`, os três opcionais, como o `reportSearch`: a interface funciona sem eles.

O diálogo de fechar com um job em andamento (D3) já existe e não muda (`requestClose()`, em `main.ts`).

### 5.10 As mensagens

| status | rota | a interface diz |
| --- | --- | --- |
| 409 | `POST /jobs` | Relê o `/devices`. Desconectado: *"O HD2 foi desconectado. Conecte o disco e tente de novo."* Com um job ativo: *"O HD2 já tem uma indexação em andamento."*, e o painel mostra esse job. Nenhum dos dois: *"Não deu para começar a indexação. Tente de novo."* (§2.5) |
| 400 | `POST /jobs` | *"Uma das pastas escolhidas não está mais no HD2."*, e a listagem é pedida de novo |
| 404 | `POST /jobs`, pastas, `PATCH` | *"Esse dispositivo não está mais registrado."* |
| 409 | pastas | A tela de *"Conecte o HD2…"* (§5.6) |
| 400 | pastas | *"A pasta junho não está mais no HD2."* |
| 409 | `POST /devices` | *"O F: foi desconectado. Conecte o disco e procure de novo."*, e o `/volumes` é pedido de novo |
| 400 | `POST /devices` | *"Não deu para adicionar esse disco."* A interface manda a identidade que a API deu, e um 400 aqui é um defeito: o status vai para o console, e dali para o `shell.log`, sem o `detail` |
| 422 | `PATCH` | *"O nome não pode ficar em branco."* Também um defeito: a interface não manda um rótulo vazio |
| 409 | `/cancel` | Nenhuma: o job terminou, e a página o relê |
| Sem resposta | qualquer | O *"Reconectando…"* da casca (RFC-037 §6.3) |

### 5.11 Os outros caminhos até a tela de pastas

- **O aviso da tela inicial** ganha o botão *"Escolher pastas"*, que o RFC-037 §16.2 deixou para cá: abre a tela do primeiro dispositivo nunca indexado. O `summarize()` passa a guardar os ids junto com os rótulos.
- **O detalhe da foto** mostra o dispositivo como um link, *"Ver no HD3"*, para `/dispositivos/:id?pasta=` com a pasta da foto, que é o `relative_path` sem o último segmento. É o *"caminho até a tela de pastas"* que o RFC-038 §12 deixou para este RFC. Com o disco na gaveta, a tela diz para conectá-lo; com a pasta apagada, o 400 de §5.6.

## 6. O Figma

O orçamento do roadmap é de seis a oito chamadas, e os RFCs 037 e 038 gastaram cinco. Este RFC não gastou nenhuma ao ser escrito, e reserva **duas** para a implementação: `create_session` e `get_design_context` no nó da tela de pastas da v2. Toda tela da v2 é um clone do `ShellTemplate`, e por isso esse nó traz também a barra lateral com os cartões, que o RFC-038 §6 viu no `43:401` e não deixou escrita (RFC-038 §14.2). **O nó precisa do link**, porque o MCP não lista a página da v2 (roadmap). Sem o link, a tela sai da tela *"quando clica no hd"* da `Page 1`, com as correções do RFC-031 §12, por um `get_metadata` que acha o nó: a terceira chamada, e a oitava do roadmap.

**O que já se sabe do desenho, sem chamada:**

| de onde | o quê | o que fica |
| --- | --- | --- |
| RFC-038 §6, no `43:401` | Na barra lateral, a porcentagem e a data do scan de cada dispositivo, *"+ Adicionar dispositivo"* e *"Configurações"* | Os dois primeiros (§5.2). *"Configurações"* não tem tela no roadmap, e sai |
| RFC-031 §12 | Nenhuma coluna de *"Arquivos do scan"* por pasta; *"1.204 indexadas"*; *"Parcial — parou em `…/IMG_2201.JPG`"* | §5.6 |
| RFC-031 §12 | O usuário escolhe pastas numa lista, e nunca digita um caminho | §5.4, §5.6: nenhuma tela tem um campo de caminho |

O diálogo de adicionar, o painel do job, a faixa do `job_runner` e os estados da tela de pastas não estão no Figma, e são construídos com os tokens do RFC-037.

## 7. Testes

**No backend, na suíte padrão:**

- **O provider:** percorrer a raiz chama `listed()` uma vez, com o número de arquivos suportados fora das pastas excluídas, antes do primeiro arquivo sair; um job de pastas nunca a chama; numa retomada, o número inclui os arquivos anteriores ao checkpoint.
- **O executor:** um job do disco inteiro grava `last_scan_file_count` e `last_scan_at`, com o relógio do teste; um de pastas não grava; o disco desmontado entre o começo e o fim da listagem não grava; um `record_scan()` que levanta deixa o job terminar, com o WARNING.
- **`record_scan()`**, no contrato dos dois repositórios: muda as duas colunas e nenhuma outra, e não faz nada com um id desconhecido.
- **De ponta a ponta:** o `GET /devices` depois de um job do disco inteiro com o modelo de mentira traz os dois campos, e o `openapi.json` não muda.

**No `frontend/`, no `npm test`**, no ambiente `node` do Vitest, sem DOM:

- **`scanShare()`:** o arredondamento para baixo, *"menos de 1%"*, o denominador nulo, o zero, acima de 100% e a data com e sem o ano.
- **O texto de cada pasta:** cada linha da tabela de §5.6, e o caminho cortado à esquerda.
- **A seleção:** a mãe absorve as filhas; uma filha de uma pasta marcada sai como *"incluída"*; desmarcar; os `scopes` que vão no `POST`.
- **A ordem e o filtro:** `2` antes de `10`, sem distinguir maiúscula; o filtro sem acento.
- **`visibleRange()`:** o começo, o fim, a lista vazia e a folga de dez.
- **O painel:** cada linha da tabela de §5.7, inclusive a falha com o disco desconectado.
- **Os 409:** a mensagem a partir do `/devices` relido, nos três casos.
- **O rótulo:** a sugestão, o vazio e o repetido.
- **Os intervalos:** o `/devices` em 10 s; o job em 2 s só enquanto ativo; as pastas sem refetch no foco.
- **A casca:** o estado do `job_runner` a partir do `ready` e dos `child`, ignorando os outros nomes, e apagado por um launcher novo; o filtro e o agrupamento das mensagens de volume.
- **A tela inicial:** o aviso com o id do dispositivo.

## 8. Critérios de pronto

Medidos na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA), num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` e uma raiz de dados própria, a quente. A janela é dirigida pelo protocolo de depuração do Chromium, como no RFC-038 §8. **A medição abre a janela no desktop e pede a quem a roda que pluge e despluge o HD externo na hora indicada**; quem a roda avisa antes, com quanto tempo leva.

| critério | o número |
| --- | --- |
| **Pela interface, sem terminal: registrar um HD externo de verdade, escolher duas pastas, indexar, cancelar e indexar de novo** | Sem número. Conferido pela API no fim: o job com os dois escopos; o cancelado com o checkpoint; o segundo com `skipped_images` maior que zero. E nenhum corpo de requisição com um caminho, lidos pelo protocolo de depuração: só ids, a identidade do volume, o rótulo e os caminhos que a listagem deu |
| **Desplugar o HD com a tela de pastas aberta:** do volume sumir da lista do Windows, que o script consulta a cada 50 ms, ao cartão dizer *"desconectado"* e a tela dizer *"Conecte o HD2 para ver as pastas"* | `TBM` s, mediana de cinco. Pela mensagem de volume (§5.9); sem ela, o polling de 10 s é o teto |
| **Uma pasta com 5 000 subpastas:** do clique na pasta mãe à lista pintada | `TBM` ms, mediana de cinco, lida do marco de §5.9. O RFC-031 §8.3 mediu 947 ms ponta a ponta e 689 KB, noutra máquina, num SSD quente |
| **A mesma pasta:** a tarefa mais longa da página nesse intervalo | `TBM` ms, por um `PerformanceObserver` de `longtask` posto na página pela medição. *"Não trava"* é este número |

A árvore das 5 000 subpastas é criada no `C:`, fora da raiz de dados, e apagada no fim; o `C:` é registrado como dispositivo na raiz de dados da medição (§2.9).

E cinco verificações que não têm número:

- **O denominador.** Um job do disco inteiro num disco pequeno, um pen drive com o dataset do RFC-022: a porcentagem aparece com a data de hoje antes da primeira foto, cresce e chega a 100%. Sem um disco pequeno à mão, isso fica com os testes de §7, e o registro diz.
- **A faixa do `job_runner`.** Com `MODEL_RUNTIME_DIRECTORY` numa pasta vazia, o executor recusa a subida (RFC-035 §6.5): a faixa aparece na primeira tela, com a linha, e continua depois de uma recarga forçada; os botões de indexar ficam desabilitados.
- **O disco desplugado no meio de um job:** o painel diz a falha de §5.7, e *"Indexar de novo"* espera o disco voltar.
- **Dez minutos na tela de pastas, parada:** as requisições por rota, contadas pelo protocolo de depuração. O `/devices` a cada 10 s, e nenhum `/volumes` nem `/folders`.
- **Os outros caminhos:** o botão do aviso da tela inicial e o link do detalhe da foto abrem a tela de pastas certa.

## 9. Riscos

| risco | situação |
| --- | --- |
| A janela do Electron não receber as mensagens de volume | O polling de 10 s cumpre o papel, e o segundo critério mostra o número. É a hipótese de §2.4 |
| O polling do `/devices` disputar a CPU com a indexação | Cerca de 87 ms por pergunta com 100 000 fotos (RFC-031 §4.1, noutra máquina), a cada 10 s. Não medido nesta máquina |
| A listagem fria de um HD grande, sem sinal nenhum | O painel diz por quê (§5.7). O tempo não foi medido (RFC-029 §15) |
| Os contadores de um job retomado recomeçarem do zero | Do RFC-029, e fica como está (§2.2). A barra do disco inteiro não depende deles; a de um job de pastas não existe |
| Um registro do mesmo disco no instante em que o job grava o scan | O `save()` do registro reescreve as duas colunas com a cópia que leu antes. A janela é de milissegundos, e o próximo job do disco inteiro corrige |
| Uma pasta chamada `x.jpg` contar no denominador | Declarado (§4.1). Rara, e só mexe na porcentagem |
| Rótulos repetidos pela CLI | A interface recusa os seus (§2.8); a API aceita |
| As 5 000 pastas custarem quase tudo no servidor | O `has_children` é 98% do tempo da rota (RFC-031 §8.3), e a página mostra *"Lendo as pastas…"*. A paginação continua fora |
| A listagem num HD externo frio | Nunca medida (RFC-031 §16). O fluxo do primeiro critério dá o primeiro número, como informação, em §13 |
| A seleção perdida numa recarga | Declarado (§5.6). O *"Reconectando…"* recarrega a página |
| Nenhum HD externo à mão no dia da medição | Os dois primeiros critérios não fecham sem um, e quem mede precisa trazê-lo (§2.9) |
| Um job criado logo depois de abrir o app esperar o modelo do executor | *"Na fila"*, com o texto de §5.7. A carga é do RFC-036 (RFC-037 §16.1) |

## 10. Fora deste RFC

- Apagar um dispositivo, o rescan sob demanda e a paginação de pastas (roadmap).
- O histórico de jobs de um disco: o painel mostra só o mais recente.
- A estimativa de tempo restante e a velocidade em fotos por segundo.
- Tirar uma subpasta de dentro de uma pasta marcada, que é uma exclusão nos `scopes`.
- Escolher arquivos, e não pastas.
- Agendar, vigiar pastas e indexar ao plugar o disco.
- A tela de *"Configurações"* do Figma.
- Um `code` nos corpos de erro da API (§2.5).
- Consertar os contadores de um job retomado, e carregar o modelo do executor só quando um job chega (RFC-036, RFC-037 §16.1).
- O mapa (RFC-040), o instalador (RFC-041), o token por sessão do RFC-036 §14, o tema escuro e outros idiomas.

## 11. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-039-dispositivos-e-indexacao.md` | Este documento |
| `frontend/src/devices/` | Os cartões, o diálogo de adicionar, o renomear, a tela de pastas, a seleção, a lista janelada, o painel do job, a faixa e os textos, com os testes |
| `frontend/electron/volumes.ts` | O filtro e o agrupamento das mensagens de volume, sem Electron, com teste |
| `backend/tests/infrastructure/workers/test_device_scan_record.py` | O executor e o provider de §7 |
| `experiments/rfc-039-devices-indexing/measure_devices.py` (+ `.log`, fora do git) | §8 e a validação |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/domain/services/indexing_observer.py` | `listed()`, com corpo vazio |
| `backend/app/infrastructure/filesystem/filesystem_image_provider.py` | A contagem depois da listagem da raiz |
| `backend/app/infrastructure/workers/job_progress_observer.py`, `job_runner.py` | O repasse e a gravação do scan, com a conferência do disco |
| `backend/app/domain/repositories/device_repository.py`, e os dois repositórios | `record_scan()` |
| `frontend/electron/main.ts`, `preload.ts` | O `hookWindowMessage`, o estado do `job_runner` e o `indexing`, e o marco |
| `frontend/src/shell/Shell.tsx`, `bridge.ts` | Os cartões e a faixa; os três métodos novos |
| `frontend/src/api/queries.ts`, `client.ts` | O `/devices` em 10 s e pelas mensagens de volume; as consultas novas |
| `frontend/src/App.tsx` | A rota `/dispositivos/:id` |
| `frontend/src/home/Home.tsx`, `summary.ts` | O botão *"Escolher pastas"*, com o id |
| `frontend/src/search/PhotoDetail.tsx` | O link para a pasta da foto |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-frontend.md` | O status deste RFC, quando implementado |

Não mudam: as migrations, as rotas e os schemas da API (o `openapi.json` fica igual), o launcher e o protocolo dos eventos, a busca e o `docker-compose.yml`.

## 12. O que passa adiante

**Para o RFC-040 (mapa):** o filtro de dispositivos lê o mesmo `/devices`, e passa a acompanhar a conexão pelas mensagens de volume sem fazer nada.

**Para o RFC-041 (instalador):** o teste em conta limpa *"indexa o dataset de demonstração"* por estas telas: adicionar o `C:`, abrir a pasta do dataset, *"Indexar as pastas"*. O `hookWindowMessage` vale igual no modo instalado. E o diálogo de adicionar lista o disco do sistema como qualquer outro: se a conta limpa mostrar que isso confunde, a decisão é desse RFC.

**Para quem mexer no `job_runner`:** os contadores que recomeçam numa retomada (§2.2) e a listagem que não dá sinal enquanto anda são as duas coisas que o painel explica em vez de mostrar. Resolver qualquer uma delas tira um texto de §5.7.

## 13. Validação

Os `TBM` de §8 ficaram como foram escritos, à vista do que se esperava; os números que os respondem estão aqui, e o que mudou por causa deles, em §14.

**Declarado antes da medição**, em §8 e no script:

- **"A lista pintada"** é o quadro seguinte àquele em que a lista desenhou o nível novo: dois `requestAnimationFrame` depois do efeito que recebe a resposta, lido do marco `folders` do `shell.log` (§5.9). A resposta é a que chegou depois do clique; um nível servido do cache enquanto é relido não encerra a medição.
- **A árvore das 5 000 subpastas** é `pasta 0001` a `pasta 5000`, uma em cada dez com uma subpasta dentro, para que algumas linhas tenham a seta, criada no `C:` dentro do diretório de trabalho e apagada no fim. Cada rodada recarrega a página no nível de cima — o cache do TanStack Query vazio, como numa janela que acabou de abrir — e clica na pasta mãe.
- **A tarefa mais longa** vem de um `PerformanceObserver` de `longtask` posto na página antes do clique. Para o Chromium, uma tarefa abaixo de 50 ms não é longa; nenhuma tarefa longa conta como zero.
- **A medição abre janelas no desktop**, a do SolidVision, várias vezes, por cerca de 25 minutos, dez deles parada numa tela, e isso foi dito antes de cada rodada.

> **Medido em 2026-10-21, na máquina do RFC-036**: Windows 10 Home (10.0.19045), Pentium G4560, 16 GB, HDD SATA, Python 3.12.9, Electron 44.9.0. Num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` e `WARM_UP_MODELS=true` no ambiente da janela, e uma raiz de dados própria num diretório temporário, apagado no fim. O banco é o PostgreSQL 16.15 embarcado. **Nenhum HD externo à mão:** o `D:` que apareceu nesta máquina é um leitor de cartão USB vazio, de 0 GB — e o `/volumes` o lista, o que a verificação visual achou e §14.1 corrige no diálogo. Os dois critérios que pedem um disco nas mãos de alguém ficaram como §2.9 e §9 previam, e o script os deixa prontos para quem trouxer um (`--disk X:`). **Um log** ao lado do script, fora do git: `measure_devices.log`, a rodada completa. Uma rodada curta antes dela, com três verificações, achou os defeitos do script, e não do produto, que §14.2 registra; os números dela não entram aqui.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte do backend | `pytest` puro | **2 895 passed, 93 deselected**, em 132,4 s, contra o PostgreSQL 16 embarcado, como o `.env` deste checkout manda. Eram 2 873 no `c376fab`. Os 22 testes novos: o provider, o executor e o `GET /devices` de ponta a ponta (16, em `test_device_scan_record.py`), e o `record_scan()` no contrato dos dois repositórios (6). O `openapi.json` não mudou, e o teste que o compara passou |
| As verificações do frontend | `npm test`, `npm run lint`, `npm run typecheck`, `npm run format:check` | **229 testes em 21 arquivos** (eram 134 em 15): o `scanShare()` e as palavras (10); as pastas, a ordem, o filtro, a URL, a trilha, a seleção e o `visibleRange()` (26); o painel, os 409, o rótulo e o cartão (22); a invalidação e os intervalos (13); as linhas do diálogo (4); as mensagens de volume (8); o estado do `job_runner` na casca (9); o marco das pastas (2); e o aviso com o id (1). O ESLint, o Prettier e os dois `tsc` limpos |
| Estilo e tipos do backend | `black --check`, `ruff check`, `mypy` | **Limpos.** O `mypy` com a configuração do projeto, 341 arquivos; o script da medição, à parte, também passa no `ruff`, no `black` e no `mypy --strict` |
| **Critério 1:** pela interface, registrar um HD externo, escolher duas pastas, indexar, cancelar e indexar de novo | §8, conferido pela API no fim | **Não fechado: não havia um HD externo.** **Ensaiado no `C:`, pela interface, do começo ao fim:** o `C:` registrado pelo diálogo como *"Sistema"*; duas pastas marcadas na lista (`copia-a` e `copia-b`, 45 fotos cada); *"Indexar as pastas"*; *"Cancelar"* com 16 fotos indexadas, e o job `cancelled` com o checkpoint em `…/copia-a/images/aerial/rural/hillside_property_pool_05.jpg`; *"Indexar de novo"*, e o segundo job `completed` com **74 indexadas e 16 que já estavam no índice**, sem falhas, sobre os mesmos dois escopos. O painel disse *"indexadas agora"*, *"Cancelando…"*, *"Cancelada"* e *"Concluída"*. **Nenhum corpo de requisição com um caminho que não veio da API:** o `POST /devices` levou só `volume_identity`, `volume_kind` e `label`, e os dois `POST /jobs`, só `device_id` e os `scopes` que a listagem deu |
| **Critério 2:** desplugar o HD com a tela de pastas aberta, até o cartão e a tela dizerem *"desconectado"* | §8, mediana de cinco | **Não medido: não havia um HD externo.** A entrega das mensagens de volume à janela, a hipótese de §2.4, continua sem conferência nesta máquina; sem ela, o teto é o polling de 10 s, conferido abaixo |
| **Critério 3:** uma pasta com 5 000 subpastas, do clique na pasta mãe à lista pintada | §8, o marco `folders`, mediana de cinco | **861 ms** (764 a 1 366 ms); **a resposta em 839 ms** (737 a 1 342 ms). O RFC-031 §8.3 mediu 947 ms ponta a ponta noutra máquina, num SSD; aqui o desenho custou de 22 a 27 ms depois da resposta. **18 linhas no DOM**, com `aria-setsize` 5000, e o filtro *"PASTA 4999"* deixou uma |
| **A mesma pasta:** a tarefa mais longa da página nesse intervalo | §8, `longtask` | **Nenhuma tarefa longa nas cinco rodadas**, ou seja, todas abaixo de 50 ms: a página não travou. Na rodada curta, a primeira abertura, feita logo depois de a janela abrir, com os dois modelos ainda carregando nos dois núcleos, teve uma de 276 ms; as quatro seguintes, nenhuma |
| O denominador | §8, num disco pequeno | **Não exercido: não havia um pen drive.** Fica com os testes de §7: o provider conta a listagem antes do primeiro arquivo, o executor grava as duas colunas com o relógio do teste e não grava num job de pastas nem com o disco desmontado, e o `GET /devices` traz os dois campos depois de um job do disco inteiro |
| A faixa do `job_runner` | §8, `MODEL_RUNTIME_DIRECTORY` numa pasta vazia | **A faixa na primeira tela, com a linha** (*"SolidVision cannot start: The models are not in C:\…"*), que chegou num `child` antes do `ready` e foi guardada pela casca (§14.1); **de novo depois de uma recarga forçada**; e *"Indexar o disco inteiro"* desabilitado, com *"A indexação está indisponível."* ao lado |
| O disco desplugado no meio de um job | §8 | **Não exercido: não havia um HD externo.** O texto do painel tem teste (§7) |
| Dez minutos na tela de pastas, parada | §8, as requisições por rota, pelo protocolo de depuração | **60 `GET /devices` em 10 minutos, um a cada 10,0 s, e nenhuma outra requisição**: nenhum `/volumes`, nenhum `/folders`, nenhum `/jobs`. A tela de pastas não pergunta o `/health`, que só a busca e a tela inicial usam |
| Os outros caminhos | §8 | **O *"Escolher pastas"* do aviso da tela inicial abriu a tela do HD4**, um dispositivo nunca indexado e desconectado escrito no banco da medição, como o RFC-037 fez, e ela disse *"Conecte o HD4 para ver as pastas."*; **o *"Ver no Sistema"* do detalhe de uma foto abriu a pasta dela**, com a trilha terminando em `rural` |
| O diálogo de adicionar | §5.4 | O `C:` listado, e registrado com o rótulo digitado; aberto de novo, *"já é o Sistema"*, com *"Abrir"*. **E o `D:`, o leitor sem cartão, oferecido para adicionar**, como *"HD1"*: achado na verificação visual, depois da rodada, e corrigido, com teste, sem medir de novo (§14.1) |
| A verificação visual | Onze capturas da janela pelo protocolo de depuração, depois da rodada | **O desenho conferido tela a tela**: os cartões, o diálogo, a confirmação do disco inteiro, o job na fila, rodando e cancelado, a lista de 5 000 rolada, o dispositivo que não existe e a faixa. **As linhas da lista a 44 px uma da outra**, posicionadas por `style` sob a CSP do build, que não registrou nenhuma recusa no `shell.log`; rolada até a linha 2 500, o DOM começava na 2 491, as dez de folga |
| Nenhum `?pasta=` no `shell.log` | §5.1 | **Nenhum valor de `?pasta=` e nenhum nome de pasta** nas 90 linhas da própria casca no `shell.log`, depois de 15 páginas carregadas, 10 delas com uma pasta na URL. As linhas dos filhos, que passam pelo mesmo arquivo, nomeiam os arquivos que leem, como no `job_runner.log` (§14.2) |
| O Figma | §6 | **Três chamadas**, a oitava do roadmap e o fim do orçamento: o link do nó da v2 não chegou, e a tela saiu da `Page 1` (§14.1) |
| O build | `vite build` | **425,1 KB de JavaScript, 131,7 KB em gzip** (eram 388,6 e 121,7 KB), e 33,8 KB de CSS (6,0 KB; eram 19,0 e 4,0 KB) |

## 14. Correções feitas durante a implementação

Registradas à vista do texto original, como os RFCs 033 a 038 fizeram com as suas. Nenhuma muda uma das decisões de §3.

### 14.1 O que a medição e o código contrariaram

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §5.9 | A casca guarda *"o `indexing` do `ready`, com `detail` nulo"* | **O `ready` repete o estado sem a linha.** Um executor que recusa a subida (os modelos ausentes, saída 78) diz isso num `child` antes de a API ficar pronta, e o `ready` vem depois com `indexing: unavailable` e sem `detail`: guardado como o texto dizia, a faixa perderia a linha que §8 pede. A casca mantém o `detail` do `child` quando o `ready` repete o mesmo estado, e o descarta quando o estado é outro; os testes de `events.test.ts` cobrem os dois casos, e a medição viu a linha na primeira tela |
| §6 | Duas chamadas ao Figma, no nó da tela de pastas da v2, se o link chegasse | **O link não chegou, e foram três**, como §6 previa: `create_session`, `get_metadata` na `Page 1` (`0:1`), que achou a tela *"quando clica no hd"* em `14:51`, e `get_design_context` nela. É a oitava chamada do roadmap: o orçamento acabou. O que o nó mostra e o que ficou está em §14.2 |
| §5.4 | O campo vem com o rótulo do volume, ou, sem ele, com *"HD"* e o menor número livre | **Um rótulo de volume que repete o de um dispositivo não é sugerido**: o campo começaria recusado, com *"Já existe um …"*. Nesse caso, e com um rótulo em branco, a sugestão é a do *"HD"* com o número |
| §5.4 | *"Um volume sem `device_id` tem um campo de rótulo e 'Adicionar'"* | **Um leitor de cartão vazio aparece no `/volumes`**, com identidade, sem rótulo e sem capacidade: o catálogo só pula o volume que não se deixa *nomear* (`volume_catalog.py`), embora a descrição da própria rota diga que um drive vazio é pulado. Adicionado, seria um disco cujas pastas não se leem — a raiz não existe para o sistema de arquivos — e cujo job termina sem nada. **O diálogo mostra um volume cuja capacidade não se lê sem *"Adicionar"*, com o motivo** (`dialog.ts`, com teste): o `GetDiskFreeSpaceExW` falha justamente sem mídia, e *"Procurar de novo"* pergunta outra vez depois de o cartão entrar. A API não mudou; o catálogo fica para quem mexer nele |
| §5.2, §5.7 | *"50% no scan de 12/03"*; no painel, *"… no scan de hoje"* | **"hoje" vale no cartão também**, e na linha da pasta (*"Indexada hoje"*): o exemplo do painel em §5.7 já dizia assim, e uma data de hoje escrita como dia e mês obriga a conferir o calendário |

### 14.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §6, §5.6 | **O nó `14:51` é o mockup original**: o shell roxo, um cartão por disco com o ícone do HD colorido pela porcentagem, uma barra fina e *"50% indexado"*; e as pastas como uma grade de blocos, separadas em *"Indexed"* e *"Not indexed"*, com um ícone de download nas não indexadas. **Ficaram** o cartão com a barra fina sob o rótulo e a porcentagem embaixo — agora sempre com a data do scan —, e o cabeçalho com o nome do disco e a parte indexada. **Não ficaram** as cores do shell, que o RFC-037 já trocou pelos tokens da v2; o ícone colorido, porque a conexão é o sinal mais forte do cartão (RFC-031), e esse sinal é o ponto; e a grade de blocos, porque a lista janelada de §5.6 precisa de linhas de altura fixa, de uma ordem só, a do Explorer, e de espaço para *"Parcial — a indexação parou em …"*. Os ícones do nó vêm de bibliotecas de design que o projeto não tem, e não entraram |
| §5.2 | **O ponto verde é um token novo**, `--color-connected`, escolhido: nenhuma cor do nó é uma variável. E `--size-bar`, a barra fina do cartão |
| §5.3 | **Dois lugares veem um job mudar de estado.** O `GET /jobs/{id}`, a cada 2 s, vê primeiro, e invalida as pastas do disco a cada mudança e, no fim, o `/devices`, os jobs do disco e os concluídos da tela inicial. O `GET /devices`, a cada 10 s, compara o `active_job` de cada disco com a leitura anterior: cobre um job que nenhuma tela acompanhava e um criado pela CLI. As duas regras são funções puras (`sync.ts`), com teste |
| §5.7 | **O job criado entra no `/devices` na hora**, como o `active_job` do disco, antes de o `/devices` responder de novo: o cartão, o painel e os botões desabilitados mudam junto com o 202, e um segundo clique não manda um segundo `POST` |
| §5.7 | **Um job terminado nunca é pedido de novo**: o painel semeia o `GET /jobs/{id}` com a cópia da lista, e um estado final não envelhece. É o *"uma vez quando ele chega a um estado final"* de §5.3 |
| §5.8 | **A nota do executor reiniciado é por execução do job**, o id e as `attempts`: o reaper devolve o job à fila gastando uma tentativa, e a execução seguinte, no executor novo, não herda a nota. Ela vale também para um painel aberto depois do reinício, porque a página a guarda junto com o estado do executor |
| §5.6 | **A tela de pastas ocupa a altura que sobra sob a faixa**: o conteúdo do shell virou uma coluna flexível, e a lista rola dentro da tela, que é o que a janela mede. As outras telas não mudaram |
| §5.6 | **O filtro e a espera de 300 ms recomeçam a cada nível**, e a lista volta ao topo a cada mudança do filtro |
| §5.10 | **Uma releitura do `/devices` que falha depois de um 409** vira *"Sem resposta do SolidVision…"*, em vez de uma promessa rejeitada sem mensagem |
| §4.1 | **O `UPDATE` atualiza a cópia que a sessão já tem.** As sessões do executor não expiram no commit (`session.py`), e um `get()` depois do `record_scan()` responderia da cópia antiga; a sincronização padrão do SQLAlchemy a atualiza |
| §8 | **O script achou três defeitos dele mesmo** na rodada curta: o *"Indexar de novo"* clicado antes de o `/devices` reler o disco, que agora espera o botão habilitar; a conferência dos logs, que lia também as linhas dos filhos — o `job_runner` nomeia cada arquivo que lê, no `shell.log` também, e isso não é da casca —, e agora lê só as da casca; e um comentário errado |

### 14.3 O que ficou sem medição

- **Os critérios 1 e 2 num HD externo de verdade**, e com eles a hipótese de §2.4: que a janela do Electron recebe `DBT_DEVICEARRIVAL` e `DBT_DEVICEREMOVECOMPLETE`. Quem trouxer um disco roda `measure_devices.py --disk X: --only flow,unplug,midjob`, e o script pede que o disco seja plugado e desplugado na hora certa.
- **O denominador num disco real**, que pede um disco pequeno (`--small-disk Y:`): um job do disco inteiro no `C:` listaria o disco do sistema inteiro.
- **A listagem fria de um HD externo grande**, que o RFC-031 §16 também não mediu.
- **O diálogo depois da correção do leitor sem cartão** (§14.1): a função tem teste, e a janela não foi aberta de novo para vê-la.
- **O custo do `/devices` a cada 10 s durante uma indexação**, que §9 tinha como risco: o fluxo do critério 1 indexou com o polling ligado, sem medir a disputa.
