# RFC-046 — Fotos Ausentes do Disco

**Status:** Implementado, como está, com as correções de §16. A medição de §15, nesta máquina, pelo `job_runner` de `build_runner()` contra um PostgreSQL 16 embarcado só dela, em 31/10/2026: os cinco ensaios na árvore do `C:` passam — mover uma pasta de 200 fotos dá **200 marcadas e 200 novas**, e a busca acha uma foto, e não duas; desfazer dá **200 desmarcadas, 200 marcadas e zero inferências**; a pasta ilegível, **0 marcadas** e o WARNING com o nome; a lixeira, **20 marcadas** e nenhuma linha nela; a purga, **220 linhas e 220 miniaturas a menos**. A varredura de 116 396 linhas sintéticas, **3,4–3,8 s e 31 MB**, depois de a primeira medição achar 78,7 MB e levar `list_presence()` a ler em fluxo (§16.2); com os 115 051 caminhos reais do `E:`, **4,1–4,2 s e 30,6 MB**, medidos de novo em 03/11 com a máquina livre — a primeira medição, com outro processo disputando a CPU, dera 5,1–6,1 s (§15.5). A listagem do `E:` dá as mesmas 124 285 entradas que o `rglob`, e a fase de listagem do job ficou **~45% mais rápida** — depois de a medição achar três custos de `pathlib` que a deixavam mais lenta (§16.12). A busca com o filtro, **−3,6%** no p95; uma nova varredura do `E:` sem mudanças, **2,3 h**. O pen drive tirado da USB no meio de um job deu o job **`failed` e 0 marcadas** (§15.6). As seções 1 a 14 são o rascunho como foi aprovado
**Depende de:** ADR-003 (o sistema de arquivos como fonte de verdade, e o custo dessa escolha), RFC-020 (que deixou a detecção de arquivos removidos como trabalho futuro), RFC-021 §3.1 (o id vem do caminho: mover é criar uma imagem nova), RFC-027 (§6.3, as linhas de um disco ausente nunca são apagadas em silêncio; §7, a conexão é resolvida quando perguntada), RFC-029 (§10, a ordem da descoberta e o checkpoint; §15, o disco que some no meio de um job; um job por vez), RFC-030 (§7.2, a thumbnail servida sem o arquivo), RFC-031 (§4.3, as contagens por dispositivo; §10, apagar um dispositivo, que continua fora), RFC-039 (§4.1, o `listed()` e o denominador; §5.2, a porcentagem do cartão), RFC-045 (§5.4, os eventos da descoberta pela fila; §14.6, o dispositivo sintético; §14.10, a lixeira indexada)
**Bloqueia:** nada. Prepara o reaproveitamento do vetor de um arquivo movido (§13) e o *"esquecer um disco"* do RFC-031 §10
**Migration:** uma, aditiva: `images.missing_since`, sem backfill
**Medição:** o piloto de §2, rodado à mão em 31/10/2026; a da implementação, `experiments/rfc-046-fotos-ausentes/measure_missing.py` (+ `.json`; o `.log`, fora do git), rodada em 31/10/2026 (§15)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*. Todos viraram número em §15; o que ficou de fora é a listagem do `rglob()` a frio, que pede administrador ou tirar o disco (§15.4). Os números sem marca vêm do piloto de §2 ou de outros RFCs, e dizem de qual. As seções 1 a 14 ficam como o rascunho as deixou; o que a implementação mudou nelas está em §16.

---

## 1. Contexto

O sistema de arquivos é a fonte de verdade, e o banco guarda o que deriva dele (ADR-003). O ADR registrou o custo dessa escolha: *"um arquivo removido continua no índice — não há detecção de exclusão hoje"*. O RFC-020 deixou a detecção como trabalho futuro. Hoje, nenhuma linha de `images` sai do banco, a não ser pelo recomeço do RFC-045.

O efeito cresce com o uso:

- **Uma foto apagada continua na busca, com miniatura.** A thumbnail é do cache do app e é servida sem o arquivo (RFC-030 §7.2). Quem descobre é o *"Mostrar no Explorer"*, com o 410.
- **Mover ou renomear uma pasta duplica as fotos dela.** O id vem do caminho (RFC-021 §3.1): o caminho novo é uma imagem nova, com inferência, e o antigo fica. Cada foto da pasta aparece duas vezes na busca, e uma das duas não existe.
- **O cartão do disco perde o número.** Com mais fotos indexadas que listadas, a porcentagem passaria de 100%, e o cartão diz *"o disco mudou desde o scan de 12/03"* (`scanShare()`, RFC-039 §5.2). As contagens da tela de pastas também contam fotos que não estão mais lá.
- **Apagar pelo Explorer não tira a foto da busca, nem com uma detecção.** O Explorer move a foto para a `$RECYCLE.BIN` do volume, e a varredura do disco inteiro a indexa lá, como foto nova. O banco de desenvolvimento tinha 1 268 linhas do `E:` nessa pasta (RFC-045 §14.10).

A razão de nada disso ter sido feito é boa: o risco de apagar demais. As linhas de um disco desplugado são *"o registro de um disco que o usuário talvez precise plugar"* (RFC-027 §6.3), e uma varredura cujo disco sumiu termina em silêncio (RFC-029 §15). O piloto de §2 achou um terceiro caminho para o mesmo erro, e é ele que decide o desenho.

## 2. O que o piloto mediu

A listagem do disco é `sorted(scope_root.rglob("*"))`, em `FilesystemImageProvider._discover_under()`. No Python 3.12.5 do `.venv`, lido no código-fonte, o `rglob("*")` faz duas coisas:

1. acha as pastas com `Path.walk()`, sem `on_error`;
2. lista cada uma de novo com `scandir()`, dentro de um `except OSError: pass`.

Os dois passos engolem o erro.

**O piloto** (31/10/2026, nesta máquina). Uma árvore temporária com `ok\a.jpg`, `bloqueada\b.jpg` e `bloqueada\sub\c.jpg`, com a leitura de `bloqueada` negada ao usuário por `icacls /deny (RD)`:

| listagem | o que devolveu | erro |
|---|---|---|
| `sorted(root.rglob("*"))`, a de hoje | `bloqueada`, `ok`, `ok\a.jpg` | nenhum |
| `root.walk(on_error=errors.append)` | `bloqueada`, `ok`, `ok\a.jpg` | `PermissionError` em `bloqueada` |

**Hoje, uma pasta que não pode ser lida some da listagem sem erro, com tudo o que tem dentro.** Isso já acontece e não faz mal: as fotos dela não são indexadas nessa passada, e as linhas antigas ficam. Com uma varredura que esconde o que a listagem não achou, o mesmo silêncio esconderia a pasta inteira da busca. Num HD externo velho, uma pasta ilegível por um setor ruim é plausível.

O `walk()` com `on_error` dá a mesma listagem e diz qual pasta falhou. O erro passa a ser **observado**, e não deduzido. O piloto testou permissão, e não um setor ruim. Os dois chegam ao `on_error` pela mesma porta, o `scandir()` da pasta, e um teste com um `scandir()` que falha cobre o segundo (§9).

## 3. Decisões

1. **Marcar, e não apagar.** Uma coluna `images.missing_since`: NULL é presente; uma data é *"não estava no scan desta data"*. A linha, o vetor e a thumbnail ficam.
2. **Esconder as linhas marcadas de toda leitura** (busca, listagem, mapa e contagens), junto do filtro que já exige `embedding IS NOT NULL`.
3. **Marcar só ao fim de um job do disco inteiro concluído, com o disco no ponto de montagem em que foi listado** (§5.4). Um job de pastas não marca nada.
4. **Desmarcar o que voltou**, na mesma varredura.
5. **Listar com `Path.walk(on_error=...)`** e nunca marcar uma linha que esteja dentro de uma pasta que não pôde ser lida (§2).
6. **Não indexar a `$RECYCLE.BIN` nem a `System Volume Information`** do volume. As linhas que já estão lá saem pela própria varredura (§5.6).
7. **Apagar de vez só por um botão**, por disco, com o número de fotos na confirmação.
8. **Sem limiar.** Nenhuma proporção de ausentes impede a marca nem pede confirmação para ela (§7).

## 4. Quem é ausente

Uma linha do dispositivo é **ausente** quando o id dela não está entre os ids da listagem do disco inteiro, e o caminho dela não está dentro de uma pasta ilegível.

- **Por id, e não por texto.** Cada caminho da listagem vira um id pela mesma `compute_image_id(device_id, ImagePath(relative))` da indexação. Comparar ids herda as regras de maiúsculas e separadores que o id já tem, sem uma segunda normalização que possa divergir dela. A tradução de um arquivo listado para um id fica numa função só, chamada pela varredura e por `discovered_candidates()`, pela razão que esta já dá para existir: uma segunda cópia é uma segunda chance de calcular outro id para o mesmo arquivo.
- **"Dentro de uma pasta ilegível"** compara partes, e não prefixos de texto: `2018` não contém `2018b` (RFC-029 §10, `JobScope.contains()`).
- **A listagem é a de hoje:** as entradas com extensão suportada, fora das pastas excluídas. Na retomada, ela inclui os arquivos anteriores ao checkpoint, porque é sempre da árvore inteira (RFC-039 §4.1). Um arquivo listado que falhou na indexação (EXIF, hash, decodificação) existe, e não é ausente.

Os casos, um a um:

| no disco | antes | depois da varredura |
|---|---|---|
| foto apagada de vez | linha visível | marcada |
| foto apagada pelo Explorer | linha visível, e outra nova na `$RECYCLE.BIN` | marcada; a lixeira deixa de ser listada (§5.6) |
| pasta movida ou renomeada | linhas no caminho antigo | antigas marcadas; novas indexadas, com inferência (§13) |
| a pasta volta ao caminho antigo | antigas marcadas, novas visíveis | antigas desmarcadas, **sem inferência**, porque o tamanho e a data são os mesmos; novas marcadas |
| uma pasta ilegível | linhas visíveis | continuam visíveis; um WARNING com o nome da pasta |
| o disco desplugado no meio do job | linhas visíveis | job `failed`, nada marcado |
| o disco na gaveta | linhas visíveis, com *"conecte o HD3"* | nada muda: não há job |
| uma extensão tirada de `supported_extensions` | linhas visíveis | marcadas (§8) |

## 5. O desenho

### 5.1 A coluna

`missing_since TIMESTAMPTZ NULL` em `images`. É uma data, e não um booleano, por dois motivos:

- o cartão diz desde quando: *"não encontradas no scan de 12/03"*;
- uma purga por prazo, se um dia existir, não pede outra migration.

A migration é aditiva e sem backfill: toda linha começa presente. O `downgrade()` remove a coluna. Não há índice: o filtro descarta quase nada (§6), e as contagens por dispositivo já leem o dispositivo inteiro. Um índice parcial `WHERE missing_since IS NOT NULL` só entra se a contagem do cartão aparecer na medição.

O `save_indexed()` escreve a linha inteira, e passa a escrever `missing_since = NULL`: uma foto que acabou de ser indexada existe.

### 5.2 A listagem

`_discover_under()` troca `sorted(scope_root.rglob("*"))` por uma função `_list_tree(scope_root)`. Ela percorre a árvore com `Path.walk(on_error=...)` e devolve as mesmas entradas do `rglob` (pastas e arquivos, sem a raiz), mais as pastas que não puderam ser lidas.

- **A ordem não muda.** Ela vem do `sorted()` com `discovery_sort_key`, que continua depois da listagem, então o checkpoint do RFC-029 §10 não muda.
- **Os links também não.** O `follow_symlinks=False` é o mesmo que o `rglob` de hoje usa por dentro.
- **Vale para todo job, de pastas também.** Muda uma coisa fora da varredura: uma pasta ilegível vira um WARNING no log do job, com o nome dela. Hoje ela não deixa rastro.
- **O `walk()` sabe o que é pasta sem um `stat`.** O `file_count` passa a contar só arquivos, e some a imprecisão que o RFC-039 §4.1 declarou: uma pasta chamada `x.jpg` deixa de contar.

Dois comportamentos do `walk()` do 3.12.5, lidos no código-fonte, entram nos testes:

- um erro **no meio** da leitura de uma pasta, depois de aberta, não vai ao `on_error`. Ele sobe, encerra a varredura e o job falha. Nada é marcado;
- um `is_dir()` que falha conta a entrada como arquivo, e a pasta não é percorrida. No Windows, o tipo vem da própria listagem do diretório, e essa chamada não vai ao disco (§8).

O `walk()` lê cada pasta uma vez, e o `rglob` lê duas (§2). O tempo de listar o `E:` a frio pelos dois é `TBM` (§10).

### 5.3 O `listed()` leva a listagem

`IndexingObserver.listed(file_count)` passa a receber um `RootListing`, um objeto de valor no Domain, com três campos:

- `file_count`: o número de hoje, só de arquivos (§5.2);
- `files`: os caminhos relativos à raiz das entradas suportadas, como texto, na forma posix que o `ImagePath` guarda. Texto, e não `PurePath`, pela memória, que tem critério em §10;
- `unreadable`: as pastas ilegíveis, relativas à raiz.

O provider continua sem saber de dispositivos e ids. Quem cunha os ids é o `job_runner`, no fim do job (§5.5).

As três implementações acompanham a mudança:

- o `NullIndexingObserver`, que continua não fazendo nada;
- o observador da fila do RFC-045, em que o evento atravessa a fila como os outros (RFC-045 §5.4);
- o `JobProgressObserver`, cujo `relay_listing_to()` passa a entregar o `RootListing` ao `_record_scan()`.

O `_record_scan()` grava o denominador como hoje e **guarda a listagem para o fim do job, só quando o disco está no ponto de montagem do começo**. É a mesma conferência que hoje decide se o denominador é gravado.

### 5.4 Quando a varredura roda

No `JobRunner.run_claimed()`, depois do `if summary.stopped` e antes do `_finish()` que conclui o job. Ela roda só se as quatro condições valerem:

1. o job não tem escopos;
2. a listagem foi guardada pelo `_record_scan()` (§5.3);
3. o pipeline terminou sem exceção, cancelamento ou interrupção, e o job não foi tomado por outro executor (`observer.lost`);
4. o disco está **no mesmo ponto de montagem** em que foi listado. O `_completion_of()` de hoje só pergunta se o disco está conectado. Para esta conferência, um disco que voltou sob outra letra é outro disco, pela razão que o `_record_scan()` já dá.

Se uma condição falha, nada é marcado, e o log diz qual falhou, em INFO.

**Se a varredura falhar, o job não falha.** A indexação terminou, e o próximo job do disco inteiro varre de novo. O erro vai ao log em ERROR, como o `_record_scan()` faz com o denominador.

**Nenhuma escrita concorrente.** O executor roda um job por vez (RFC-029), então nenhum outro job escreve entre a listagem e a varredura. A rota de purga é a única outra escritora, e recusa enquanto o disco tem um job ativo (§5.7).

### 5.5 A varredura

Um caso de uso novo, `SweepMissingImages`, em Application, testável com o repositório em memória:

1. lê a presença das linhas do dispositivo: `ImageRepository.list_presence(device_id)`, com o id, o `relative_path` e se a linha está marcada. É uma consulta, no fim do job, para que as linhas criadas por ele também entrem. O retrato do RFC-045 §5.2 não serve: é do começo do job, e só existe com a leitura antecipada ligada;
2. **ausentes** são as linhas visíveis que não estão nos ids listados nem dentro de uma pasta ilegível;
3. **voltaram** são as linhas marcadas que estão nos ids listados;
4. chama `mark_missing(ausentes, agora)` e `mark_present(voltaram)`, numa transação. Uma linha já marcada fica com a data em que foi marcada;
5. devolve três números (marcadas, desmarcadas e protegidas por pasta ilegível) para uma linha INFO no log do job. As pastas ilegíveis vão em WARNING, cada uma com quantas linhas protegeu.

### 5.6 A lixeira

`$RECYCLE.BIN` e `System Volume Information`, na raiz do volume, entram nas pastas excluídas de todo job, ao lado do cache de thumbnails (RFC-030). O `job_runner` as junta pelo ponto de montagem: `mount / "$RECYCLE.BIN"`. A comparação já é de caminhos, sem diferença de maiúsculas no Windows (`_is_excluded()`), então a `$Recycle.Bin` do `C:` também cai.

Consequências:

- uma foto apagada pelo Explorer some da busca no próximo scan do disco inteiro, como uma apagada de vez;
- as linhas que já estão na lixeira (1 268 do `E:` no banco de desenvolvimento) deixam de ser listadas, e a primeira varredura as marca. Nenhuma migration precisa conhecê-las;
- os arquivos `$I…`, os índices da lixeira, que levam a extensão do arquivo apagado, deixam de falhar na indexação (RFC-045 §14.10);
- o denominador do cartão diminui na mesma medida, porque a contagem também respeita as pastas excluídas.

Restaurar uma foto da lixeira a devolve ao caminho original, e o scan seguinte a desmarca, sem inferência.

### 5.7 A purga

`DELETE /api/v1/devices/{id}/missing-images`:

- apaga as linhas marcadas do dispositivo numa transação, e só depois as thumbnails delas do cache. Se o processo morrer entre os dois passos, o que fica para trás é uma thumbnail sem linha, que é só lixo no cache; o contrário seria uma linha apontando para o nada;
- responde 200 com `{"purged": N}`, e 404 para um dispositivo que não existe;
- responde **409 enquanto o disco tem um job pendente ou em execução**: o job pode desmarcar uma linha que a purga apagaria. É o 409 do RFC-030: o pedido é legítimo, e o estado não permite;
- não pede o disco conectado. As marcas vieram de um scan conferido, e esquecer o que não está mais no disco não precisa do disco.

`ImageRepository.purge_missing(device_id)` devolve os caminhos das thumbnails (`DELETE … RETURNING thumbnail_path`), e a `ThumbnailStorePort` ganha `delete(location)`. O `ImageRepository.delete()` por linha, que nenhum caso de uso chama hoje, não serve: no pior caso, seriam 116 mil idas ao banco.

**Sem os guardas do RFC-030.** Eles protegem o sistema de arquivos do usuário, e esta rota não toca nele: apaga só o que o próprio app criou, as linhas e o cache.

### 5.8 A API e a interface

- **O dispositivo.** `DeviceDetailSchema` ganha `missing_images` e `missing_since`, a data mais antiga entre as marcadas, ou null. `indexed_images` passa a contar só as linhas visíveis: o numerador da porcentagem é o que a busca acha. Uma `count_missing_by_device()` nova traz o número e a data num `GROUP BY` só, pela razão que o RFC-031 §4.3 deu para o `count_by_device()`.
- **A foto.** `GET /images/{id}` continua respondendo para uma linha marcada. É o que um link antigo pede, e o 410 do Explorer continua dizendo o que aconteceu.
- **O cartão** (`DeviceCards.tsx` e `card.ts`). Com `missing_images > 0`, aparece uma linha *"312 fotos não encontradas no scan de 12/03"* e o botão *"Remover do índice"*. Ele abre a confirmação: *"Remover 312 fotos do HD3 do índice? Os arquivos não estão mais no disco. Se voltarem, serão indexados de novo."* O botão fica desligado enquanto o disco tem um job.
- **Sem tela que liste as ausentes** (§12).

## 6. As leituras com o filtro novo

Todas as leituras de `PostgresImageRepository` que servem a interface já exigem `embedding IS NOT NULL`. São seis:

- `search_statement()`;
- `listing_statement()`;
- `summary_statement()`;
- as duas contagens de desconhecidos;
- `aggregate_statement()`.

A condição nova entra num helper único, `_searchable()`, que as seis chamam. Assim, uma leitura nova não pode esquecer uma das duas condições. `count_by_device()` e `count_by_path_prefixes()` ganham a mesma condição. O repositório em memória faz o mesmo, e o teste de contrato confere que os dois escondem as mesmas linhas.

O custo depende do plano:

- **No PostgreSQL 16 embarcado**, o da instalação, a busca não usa o índice HNSW: o desempate por `id` faz o 16 varrer a tabela, e a ordem é exata (RFC-038 §14.1). A condição nova é mais um filtro nessa varredura. O p95 da busca com e sem ela é `TBM`.
- **No PostgreSQL 17**, o índice HNSW serve a busca. Ali, uma linha marcada gasta o orçamento do `ef_search` como uma tupla morta: é achada, filtrada depois, e a página pode voltar curta (o mecanismo do RFC-025 §7.3). Com poucas linhas marcadas, isso é desprezível. Depois de uma reorganização grande ainda não purgada, não é. O 17 não é o runtime da instalação; o caso fica nos riscos, e a purga é o remédio.

## 7. Alternativas consideradas

| alternativa | por que não |
|---|---|
| Apagar as ausentes direto, sem marca | Um falso positivo, como uma pasta ilegível que escapou do `on_error` ou um disco que voltou sob outra letra, jogaria fora vetores que custaram horas (RFC-027 §6.3). Com a marca, o scan seguinte desfaz o erro sem inferência |
| Um booleano `missing` | O cartão não saberia desde quando, e uma purga por prazo pediria outra migration |
| Zerar o `embedding` das ausentes, para reaproveitar o filtro que já existe | É apagar o vetor com outro nome. E uma linha sem vetor já quer dizer outra coisa: a inferência que falhou |
| Um limiar: acima de X% de ausentes, não marcar e pedir confirmação | Não distingue uma reorganização grande de uma falha de leitura, porque nas duas muitas fotos somem de uma vez. A falha é observável diretamente (§2). E uma reorganização legítima ficaria esperando um botão que precisaria guardar a listagem ou escanear de novo, horas no `E:` |
| Varrer também nos jobs de pastas | Um job de pastas não chama o `listed()`, e uma pasta apagada já faz o job falhar antes de começar (*"is no longer a folder"*). Fica para quando houver um pedido (§12) |
| Conferir cada linha com um `stat`, sem depender da listagem | São 116 mil `stat` num HD frio, com a listagem já na memória. E não resolve a pasta ilegível: o `stat` de um arquivo dentro dela também falha |
| Vigiar o disco (`ReadDirectoryChangesW`) | Um HD externo passa a maior parte do tempo desplugado, e o app nem sempre está aberto. No máximo anteciparia a varredura, sem substituí-la |
| Identidade por conteúdo, para que um arquivo movido mantenha o id | O ADR-003 e o RFC-024 recusam: dois arquivos iguais em dois caminhos são duas imagens. O reaproveitamento do vetor (§13) dá o ganho sem mexer na identidade |
| Apagar a foto do disco pelo app | O app não é dono do acervo (ADR-003). O *"Mostrar no Explorer"* já leva o usuário ao lugar onde apagar é decisão dele |

## 8. Riscos

- **Uma pasta escondida por engano.** A listagem ainda pode perder uma pasta por um caminho que o `on_error` não vê: o `is_dir()` que falha (§5.2). No Windows, o risco é teórico. Se acontecer, o cartão mostra o número, e o scan seguinte desfaz a marca sem inferência.
- **Desfazer um falso positivo custa um scan do disco inteiro.** A descoberta lê o cabeçalho de cada foto (RFC-028), então uma nova varredura do `E:` sem mudanças, a frio, leva `TBM` h (§10). É o preço de não ter limiar (§7), e é menor que reindexar.
- **Uma extensão tirada da configuração** faz as fotos dela serem marcadas como não encontradas, e elas existem. O efeito na busca é o certo, porque o usuário tirou a extensão; só a mensagem do cartão fica imprecisa. Não vale um estado a mais.
- **O PostgreSQL 17** devolve páginas curtas depois de uma reorganização grande ainda não purgada (§6).
- **A memória da listagem.** Os caminhos e os ids de 116 mil arquivos ficam na memória até o fim do job, numa máquina que fica com ~1 GB livre com o modelo carregado (RFC-045 §8). O pico é `TBM`, com critério em §10.
- **A purga é irreversível.** Por isso ela vem com o número na confirmação, recusa enquanto o disco tem um job e apaga só as linhas marcadas daquele disco.
- **Reorganizar ainda custa inferência.** Depois deste RFC, a pasta movida deixa de aparecer em dobro, mas o caminho novo continua sendo inferido, como hoje (§13).

## 9. Testes

Sem o modelo real:

- **A listagem:**
  - `_list_tree()` devolve as mesmas entradas que `sorted(rglob("*"))`, numa árvore com espaços, maiúsculas, pastas aninhadas e uma pasta excluída;
  - com um `scandir()` que falha numa pasta, a pasta sai em `unreadable` e a subárvore dela não aparece;
  - no Windows, o mesmo com a permissão negada de verdade, como no piloto;
  - um erro no meio da leitura de uma pasta encerra a descoberta com exceção;
  - o `listed()` recebe um `RootListing` com os arquivos, a contagem e as pastas ilegíveis; uma pasta chamada `x.jpg` não conta;
  - uma varredura de pastas nunca chama o `listed()`;
  - `$RECYCLE.BIN`, `$Recycle.Bin` e `System Volume Information` na raiz ficam fora.
- **A identidade:** para cada arquivo de uma árvore, o id cunhado a partir da listagem é o id que a indexação grava.
- **A varredura** (`SweepMissingImages`, com o repositório em memória):
  - cada linha da tabela de §4;
  - uma linha já marcada mantém a data;
  - a proteção por pasta ilegível compara partes: `2018` ilegível não protege `2018b`.
- **O repositório** (contrato, no PostgreSQL e em memória):
  - as oito leituras de §6 escondem as linhas marcadas;
  - o `save_indexed()` desmarca;
  - o `purge_missing()` apaga só as linhas marcadas daquele dispositivo e devolve as thumbnails delas;
  - o `count_missing_by_device()` devolve o número e a data mais antiga.
- **O `job_runner`:**
  - a varredura não roda quando falta qualquer uma das condições de §5.4, com um teste por condição: escopos, cancelamento, exceção, job tomado, disco sob outra letra, listagem não guardada;
  - uma exceção na varredura não falha o job;
  - com a leitura antecipada ligada e desligada, o resultado é o mesmo.
- **A rota:**
  - 200 com o número, e as thumbnails fora do cache;
  - 404 para um dispositivo que não existe;
  - 409 com um job ativo;
  - os campos novos de `/devices`.
- **A migration:** o `upgrade()` cria a coluna nula, com as linhas existentes presentes; o `downgrade()` a remove.
- **A interface:**
  - o texto do cartão com e sem ausentes, com o ano quando não é o atual, pela regra do `lastIndexedText()`;
  - o texto da confirmação;
  - o botão desligado durante um job.

## 10. Critérios de pronto

**Onde medir:** nesta máquina (Windows 11, NVMe, o `E:` no USB), pelo `job_runner` montado por `build_runner()`, contra um PostgreSQL 16 embarcado só da medição, como no RFC-045. **Nada é escrito no `E:`.** Os ensaios que movem e apagam fotos rodam numa árvore no `C:`, registrada como dispositivo, como no RFC-039 §2.9. O disco desconectado é um pen drive com uma cópia dessa árvore. Os critérios foram escritos antes dos números.

| critério | o número |
|---|---|
| **A listagem do `E:` a frio**, `walk()` contra `rglob()`, alternados | `TBM` s cada. **Passa se as duas listagens forem iguais entrada a entrada e o `walk()` não for mais de 5% mais lento** |
| **A varredura** num dispositivo sintético de 116 396 linhas, com os caminhos reais do `E:` (RFC-045 §14.6) e 1% delas ausentes | `TBM` s e `TBM` MB a mais no pico. **Passa abaixo de 5 s e de 50 MB** |
| **A busca no PostgreSQL 16 embarcado**, nas 100 000 linhas do `measure_map.py --seed-rows` (RFC-040), com 1% delas marcadas | p95 `TBM` ms, com e sem o filtro. **Passa se a diferença for de até 5%** |
| **Mover uma pasta** de 200 fotos no `C:` e indexar o disco inteiro | **200 marcadas, 200 novas**, e a busca por uma delas devolve uma foto, e não duas |
| **Desfazer o movimento** e indexar de novo | **200 desmarcadas, 200 marcadas, zero inferências** |
| **Uma pasta ilegível** (`icacls /deny`) com 50 fotos indexadas | **0 marcadas**, e um WARNING com o nome da pasta |
| **Apagar 20 fotos pelo Explorer** e indexar | **20 marcadas**, e nenhuma linha na `$RECYCLE.BIN` |
| **A purga** das 220 marcadas | **220 linhas e 220 thumbnails a menos**, e o cartão sem a linha das ausentes |
| **Desplugar o pen drive** no meio da listagem | job `failed`, **0 marcadas** |
| **Uma nova varredura do `E:` sem mudanças**, a frio | `TBM` h. Sem critério: é o custo de desfazer um falso positivo (§8) |

## 11. Entregáveis

- `backend/alembic/versions/<revisão>_add_image_missing_since.py`: a migration de §5.1.
- `backend/app/infrastructure/database/models/image_model.py`: a coluna.
- `backend/app/domain/value_objects/root_listing.py` (novo): `RootListing`.
- `backend/app/domain/services/indexing_observer.py`: `listed(RootListing)`.
- `backend/app/domain/repositories/image_repository.py`:
  - `list_presence()`, `mark_missing()`, `mark_present()`, `count_missing_by_device()` e `purge_missing()`;
  - as implementações do PostgreSQL e em memória, com o `_searchable()` de §6;
  - o teste de contrato.
- `backend/app/domain/services/thumbnail_store_port.py` e `backend/app/infrastructure/filesystem/thumbnail_store.py`: `delete()`.
- `backend/app/application/use_cases/sweep_missing_images.py` e `purge_missing_images.py` (novos).
- `backend/app/infrastructure/filesystem/filesystem_image_provider.py`: `_list_tree()` e o `RootListing`.
- `backend/app/infrastructure/workers/`:
  - `job_runner.py`: as pastas de sistema excluídas, a listagem guardada e a varredura de §5.4;
  - `job_progress_observer.py` e `read_ahead.py`: o `RootListing`;
  - `indexing_worker.py`: a tradução de caminho para id numa função só.
- `backend/app/presentation/schemas/device_schema.py` e `backend/app/presentation/api/v1/routers/devices.py`: os campos e a rota. O caso de uso que monta `/devices`: as contagens.
- `frontend/src/devices/`: `card.ts`, `DeviceCards.tsx` e `api.ts`; `frontend/src/api/schema.d.ts`, regenerado.
- `docs/adr/adr-003-imagens-permanecem-no-sistema-de-arquivos.md` e `ARCHITECTURE.md`: o trade-off do arquivo removido e do arquivo movido passa a dizer como as linhas saem (§14).
- Os testes de §9.
- `experiments/rfc-046-fotos-ausentes/measure_missing.py`, com o `.json`. O `.log` fica fora do git.
- Este documento, com os `TBM` escritos de volta, e a linha dele no [README](README.md).

## 12. Fora deste RFC

- **A varredura nos jobs de pastas.** Ela pediria o `listed()` por escopo, e uma pasta apagada já faz o job falhar (§7).
- **Uma tela que liste as fotos ausentes** antes da purga. Seria a listagem do RFC-044 com um filtro de marcadas. Fica para quando o número no cartão não bastar.
- **A purga automática por prazo.** A coluna já é uma data por isso (§5.1).
- **Esquecer um disco inteiro** (RFC-031 §10). Este RFC entrega a peça: purgar as linhas de um disco é a mesma operação, sem a condição da marca. A decisão de produto continua sendo a do RFC-031.
- **Tirar o `is_file()` da descoberta.** Com o `walk()`, a listagem já sabe o que é arquivo, e o `stat` por entrada que o `_discover_under()` faz deixaria de ser necessário. É uma economia de leitura que este RFC não mede.
- **Outras pastas de sistema**, além das duas de §5.6.

## 13. O que passa adiante: o vetor de um arquivo movido

Depois deste RFC, uma pasta movida não aparece mais em dobro, mas o caminho novo continua sendo inferido: reorganizar o `E:` inteiro custaria uma indexação inteira, 8,7 h (RFC-045).

Um arquivo novo já recebe o SHA-256 antes da inferência (`plan_indexing()`). Se uma linha do mesmo dispositivo, marcada ou não, tiver o mesmo hash e um vetor, a indexação pode copiar o vetor e a thumbnail em vez de inferir. Isso não é identidade por conteúdo (ADR-003): os dois arquivos continuam sendo duas imagens, com dois ids. É um cache do vetor, porque os mesmos bytes, com o mesmo modelo e o mesmo pré-processamento, dão o mesmo vetor.

A consulta não pode depender da marca: o caminho novo é indexado durante o job, antes de a varredura marcar o antigo. O tema merece um RFC próprio porque dá à decisão de `plan_indexing()` um quarto desfecho e pede um índice em `content_hash`, cujo custo é uma medição.

## 14. O que isto muda em documentos anteriores

- **ADR-003, trade-offs.** *"Um arquivo removido continua no índice"* passa a ser: sai da busca no scan seguinte do disco inteiro, e do banco pela purga. *"Um arquivo movido ou renomeado aparece como imagem nova, e a antiga permanece indexada"* passa a ser: a antiga sai da busca.
- **RFC-020, trabalho futuro.** A detecção de arquivos removidos é este RFC.
- **RFC-039 §4.1 e §5.2.** O `listed()` passa a levar a listagem, e o `file_count` passa a contar só arquivos. O `indexed_images` passa a contar as linhas visíveis, e o caso *"mais indexadas que listadas"* fica restrito à janela entre uma mudança no disco e o scan seguinte.
- **RFC-045 §14.10.** A lixeira deixa de ser indexada aqui.

## 15. Validação

**Onde e como.** Nesta máquina (Windows 11, i5-1035G1, 8 threads, 7,7 GB, NVMe), em 31/10/2026: os passos do `C:` e do banco das 13:00 às 13:22; os do `E:`, ligado pela USB, das 13:40 às 13:55 (§15.5). `experiments/rfc-046-fotos-ausentes/measure_missing.py` (+ `.json`; o `.log`, fora do git), semente 20261031. Cada passo cria **um PostgreSQL 16.15 embarcado num diretório temporário só dele**, na revisão `d2a7e4c19b53` desta migration, e o remove no fim; o banco de desenvolvimento não é tocado. Os jobs são os do `JobRunner` de `build_runner()` — a listagem pelo `walk()`, a leitura antecipada de 32, o CLIP, as miniaturas, a varredura —, com uma peça trocada: o `DeviceLocator`, para que uma árvore do `C:` seja um disco (§16.10). O processo não tinha privilégio de administrador. **O `E:` foi ligado depois**, e os passos que pedem o disco rodaram com ele (§15.5); nada foi escrito nele.

| critério (§10) | o número | passa? |
|---|---|---|
| **A listagem do `E:` a frio**, `walk()` contra `rglob()` | **124 285 entradas, iguais uma a uma**. A frio só o `walk()`: **30,9 s**, antes da correção de §16.12. A quente, alternados, depois dela: `walk()` **0,5–0,7 s**, `rglob()` 1,0–1,2 s. A fase inteira do job até o primeiro arquivo — a listagem, a ordenação e a contagem ou o `RootListing` —, a de antes contra a deste RFC: **3,7–4,2 s contra 2,1–2,5 s** (§15.5) | **sim**, nas entradas, a quente e na fase do job. A frio, o `rglob()` não foi medido: esvaziar o cache pede administrador ou tirar o disco (§15.4) |
| **A varredura** num dispositivo sintético de 116 396 linhas, 1% ausentes | Caminhos sintéticos (§15.2): **3,40, 3,79 e 3,36 s** (cada uma a primeira leitura depois de reiniciar o servidor), das quais 2,0–2,1 s são cunhar os 115 233 ids da listagem; **31,3 MB** de pico em Python (`tracemalloc`), 16,6–37,2 MB de bytes privados; 1 163 marcadas em cada. Os **115 051 caminhos reais do `E:`**, com a máquina livre (03/11): **4,09, 4,20 e 4,21 s** (cunhar, 2,4–2,6 s) e **30,6 MB**; com outro processo disputando a CPU (31/10), 5,06, 5,34 e 6,14 s (§15.5) | **sim** (< 5 s, < 50 MB), com os sintéticos e com os reais |
| **A busca no PostgreSQL 16 embarcado**, 100 000 linhas do `measure_map.py`, 1 000 marcadas | intercaladas consulta a consulta, 500 de cada: com o filtro **p95 819 ms** (p50 573), sem **p95 850 ms** (p50 580): **−3,6%**. Os dois planos são o mesmo — `Parallel Seq Scan`, `Sort`, `Gather Merge` —, com um filtro a mais por linha | **sim** (até 5%) — §15.3 |
| **Mover uma pasta** de 200 fotos e indexar o disco inteiro | **200 marcadas, 200 novas** (200 inferidas, 70 inalteradas), em 23,5 s. *"Parecidas com esta"* sobre uma foto movida: **1** resultado idêntico (cosseno > 0,9999), o caminho novo; o seguinte, 0,908. A linha antiga continua no banco, marcada | **sim** |
| **Desfazer o movimento** e indexar de novo | **200 desmarcadas, 200 marcadas, 0 inferências** (270 inalteradas), em 0,7 s | **sim** |
| **Uma pasta ilegível** (`icacls /deny (RD)`) com 50 fotos indexadas | **0 marcadas**; 220 listadas; dois WARNINGs: *"Could not read the folder bloqueada; nothing under it was listed"*, da listagem, e *"the folder bloqueada could not be read; 50 of its photos stay visible"*, da varredura | **sim** |
| **Apagar 20 fotos pelo Explorer** e indexar | **20 marcadas**, **0 linhas** na `$RECYCLE.BIN`, nenhuma falha (os `$I…` não são listados). O Explorer emulado (§16.10) | **sim** |
| **A purga** das 220 marcadas | `DELETE /devices/{id}/missing-images` pela rota, com as dependências apontadas para o cluster da medição: **200 `{"purged": 220}`** em 165 ms; **220 linhas e 220 miniaturas a menos**; o `/devices`, antes `missing_images` 220 e `indexed_images` 250, depois **0** e `missing_since` nulo | **sim** |
| **Desplugar o pen drive** no meio | tirado da USB sem ejetar, no meio do segundo job, 2,9 s depois do pedido: o job terminou **`failed`** — *"The device was disconnected before the scan finished"* —, a varredura não rodou (*"the disk was listed at E:\\ and is now not connected"*) e **0 marcadas**, das 98 linhas. No meio do job, e não da listagem (§15.6) | **sim** |
| **Uma nova varredura do `E:` sem mudanças**, a frio | **2,3 h** para as 115 051 fotos: a listagem a frio (30,9 s) e o custo de um arquivo sem mudança — o `stat` e o cabeçalho EXIF — em 2 000 fotos frias sorteadas, **72,7 ms** em média (mediana 56,0) | sem critério |

A primeira indexação da árvore, 270 fotos de 1 024 px recortadas das do demo, levou 41,5 s.

### 15.1 A varredura: a lista era a memória

A primeira medição da varredura deu 15,7–18,1 s e **78,7 MB**, e as duas coisas tinham causas diferentes.

- **O tempo era o `tracemalloc`.** A primeira rodada cronometrou com ele ligado, e ele cobra cada alocação: cunhar os ids levou 10,4–12,3 s ali, e 2,0–2,1 s sem ele (13,7 µs por id, medido à parte). O script passou a cronometrar três rodadas sem ele e a medir a memória numa quarta, como o RFC-045 fez.
- **A memória era a lista de presença.** `list_presence()` devolvia uma lista com as 116 396 linhas do dispositivo — cada uma com o id, um `UUID` e o caminho —, além do conjunto dos 115 233 ids da listagem. Ela passou a ler as linhas por um cursor do servidor, de 10 000 em 10 000, e a entregá-las conforme chegam (§16.2): o pico caiu para **31,3 MB**, quase todo o conjunto dos ids da listagem.

A listagem que o job guarda até o fim — os 116 396 caminhos como texto — ocupa 11,5 MB e não entra nesse pico: ela já existe desde a listagem.

### 15.2 Os caminhos sintéticos, e depois os reais

O critério pedia *"os caminhos reais do `E:` (RFC-045 §14.6)"*, e na primeira rodada o `E:` não estava ligado. A medição gravou 116 396 caminhos com a forma de um acervo de drone — `Drone/2019/2019-03-12 Voo 2 - Obra 07/DCIM/100MEDIA/DJI_0123.JPG`, 64 caracteres em média —, os vetores aleatórios de 512 dimensões, e a tabela ficou com 365 MB (a do RFC-045, com os caminhos reais, com 384 MB). Com o `E:` ligado, o passo foi repetido com os 115 051 caminhos dele (§15.5).

### 15.3 A busca: os blocos mediam a máquina

A primeira rodada alternou blocos de 50 consultas com e sem o filtro e deu **+16,9%** no p95 (683 contra 584 ms), com +2,9% no p50 — sobre dois planos idênticos, em que a diferença é um teste de nulo por linha. Era a máquina mudando entre os blocos. Intercalando as duas consulta a consulta, com a ordem alternada e 500 de cada, a diferença foi de **−3,6%** no p95 e −1,2% no p50: o filtro não aparece acima do ruído. Os ~0,6 s por consulta são do RFC-038 §14.1: no 16, o desempate por `id` faz a busca varrer as 100 000 linhas e seus vetores.

### 15.4 O que não foi medido

- **O `rglob()` a frio.** Só a primeira listagem de cada rodada é a frio, e esvaziar o cache do Windows pede privilégio de administrador ou tirar o disco e ligá-lo de novo. A frio foi medido o `walk()`, e ainda com os custos de §16.12. As leituras do disco são as mesmas nas duas formas — o `rglob` lê cada pasta duas vezes, a segunda do cache —, e o que §16.12 tirou foi custo de Python.
- **O pen drive tirado no meio do job** foi medido depois, em 03/11 (§15.6). O passo `unplug` do script, com `--pen F:/`, recusa o que não for a raiz de um volume removível — um pen drive ou um cartão; o `C:` e o `E:` o Windows dá como fixos — e só escreve numa pasta própria, `rfc046-ensaio`, que cria. Grava nela 40 fotos, indexa o pen drive inteiro, apaga uma das 40 — que uma varredura marcaria — e grava mais 120; o segundo job, no primeiro lote, pede para tirar o pen drive e espera o volume sumir, com 120 fotos ainda por inferir. *"No meio da listagem"*, como o §10 pede, não se faz à mão: a listagem de uma árvore pequena leva milissegundos. O comportamento está nos testes: o job termina `failed` e nada é marcado, com a leitura antecipada ligada e desligada (`test_job_runner_sweep.py`).

### 15.5 O `E:`

Ligado pela USB, em 31/10 das 13:40 às 13:55, e em 03/11 às 12:21. **Em 31/10 havia outro processo rodando na máquina**, que chegou a ocupar 77% da CPU: os tempos daquele dia são da máquina disputada, e as comparações valem porque os dois lados foram alternados sob a mesma disputa. O tempo absoluto da varredura foi medido de novo em 03/11, com a máquina livre.

- **As entradas.** 124 285, iguais às do `rglob()` uma a uma, em 115 051 fotos fora das pastas excluídas — 116 396 com a lixeira.
- **A primeira listagem**, pelo `walk()` com a implementação de então, levou 2,6–4,7 s a quente contra 1,3–2,0 s do `rglob()`: duas vezes mais lenta. A medição separou o porquê, e não era o disco (§16.12). Corrigido, o `walk()` levou **0,5–0,7 s** contra 1,0–1,2 s do `rglob()`, e a fase do job até o primeiro arquivo, **2,1–2,5 s** contra 3,7–4,2 s da de antes do RFC — com três pastas excluídas, contra uma.
- **As pastas ilegíveis.** O `walk()` achou 111: as 99 pastas de outros usuários dentro da `$RECYCLE.BIN` (`S-1-5-21-…`, de cada máquina em que o disco já foi usado), a `System Volume Information` e **11 pastas `found.000` a `found.010`** na raiz, que o `chkdsk` cria quando recupera arquivos. As 100 primeiras estão em pastas excluídas, e §16.3 as deixa de fora. As `found.NNN` não: cada scan do `E:` terá **11 WARNINGs** por elas — o que o `rglob` escondia, e o que §5.2 pediu. Excluí-las é decidir *"outras pastas de sistema"*, que o §12 deixou fora deste RFC.
- **A nova varredura sem mudanças**: 72,7 ms por foto fria (mediana 56,0), **2,3 h** com a listagem a frio. O RFC-045 mediu a descoberta a frio do `E:` em 51–72 ms por foto; é a mesma leitura.
- **A varredura com os caminhos reais**, em 31/10: 5,06, 5,34 e 6,14 s, e 30,6 MB. Cunhar os ids levou 2,9–3,8 s, contra 2,0–2,1 s com os sintéticos, mais longos (64 caracteres contra 38 dos reais), e o resto, 2,1–2,4 s contra 1,3–1,7: as duas metades mais lentas na mesma proporção, que é o que a disputa de CPU faz, e não o tamanho do texto.
- **A varredura com os caminhos reais, de novo**, em 03/11, com a máquina livre — a carga da CPU durante o passo, amostrada a cada 2 s, com mediana de 24% e máxima de 60%, contando a própria medição e o PostgreSQL; o maior processo de fora era o Defender, com ~8%: **4,09, 4,20 e 4,21 s** (cunhar, 2,38–2,61 s) e **30,6 MB** de pico em Python; 1 150 marcadas em cada. Passa.

### 15.6 O pen drive

Em 03/11, às 12:45, com um pen drive de 14,5 GB ligado pela USB — um pendrive de instalação do Windows, que o sistema montou como `E:` e dá como removível; o HD externo não estava ligado —, pelo passo `unplug` (§15.4):

- o primeiro job do disco inteiro listou e indexou as **40** fotos que o passo gravou em `rfc046-ensaio`, as únicas fotos do pen drive, e terminou `completed`, com a varredura rodando: 0 marcadas;
- uma das 40 foi apagada e 120 foram gravadas; no primeiro lote do segundo job, o script pediu, por uma janela e no terminal, para tirar o pen drive. Ele foi tirado da USB, sem ejetar, **2,9 s** depois, e o job seguiu sozinho;
- o job terminou **`failed`**, em 23,3 s, com a mensagem do RFC-029 §15, e o log disse por que nada foi marcado: *"Job … marks no photo as not found: the disk was listed at E:\ and is now not connected"*. **0 marcadas** das 98 linhas — inclusive a da foto apagada, que uma varredura teria marcado.

**58 das 120 fotos novas foram indexadas depois de o pen drive sair.** A leitura antecipada (RFC-045) já tinha os bytes delas na memória — os 32 à frente e a janela em curso —, e o CLIP e as miniaturas decodificam dos bytes. As outras 62 o `is_file()` da descoberta deu como ausentes e pulou, sem falha. As 58 linhas ficam: são de arquivos lidos inteiros antes de o disco sair, e o próximo job as pula (*"the work already done is kept"*).

O §10 pedia o pen drive tirado *"no meio da listagem"*; a listagem de uma árvore pequena leva milissegundos, e nenhuma mão chega lá. O que o critério protege — um disco que some antes de o job terminar não marca nada — vale para qualquer instante depois da listagem, que é onde ele foi tirado; antes dela, ou no meio, o `_record_scan()` não guarda a listagem, e `test_job_runner_sweep.py` cobre esse caso.

## 16. Correções feitas durante a implementação

### 16.1 `mark_missing()` e `mark_present()` são um método só

O §5.5 pede as duas escritas *"numa transação"*, e o §11 lhes dá dois nomes. O repositório faz commit por chamada, e não expõe transações a quem o chama; dois métodos seriam duas transações. Ficou um: `mark_presence(missing, present, since)`, que faz os dois `UPDATE`s e um commit, ou nada. Um id nos dois conjuntos é recusado com `ValueError`, e não resolvido por uma ordem que ninguém escolheu.

### 16.2 `list_presence()` lê em fluxo

O §5.5 a descreve como *"uma consulta, no fim do job"*, e continua sendo uma: lida por um cursor do servidor (`yield_per`), de 10 000 em 10 000 linhas, e entregue conforme chega. Como lista, ela era a maior parte dos 78,7 MB de §15.1. A varredura consome tudo antes de escrever, e guarda só as linhas que marca ou desmarca.

### 16.3 As pastas ilegíveis dentro das excluídas não contam

O `walk()` percorre as pastas excluídas, como o `rglob` fazia — a listagem tem as mesmas entradas, e quem as descarta é o filtro de depois. Uma pasta ilegível dentro delas, porém, não vai a `RootListing.unreadable` nem ao log. Por dois motivos: a `System Volume Information` é ilegível em todo volume, e seria um WARNING em todo scan; e uma pasta da `$RECYCLE.BIN` ilegível protegeria da marca as linhas que já estão nela, contra o que o §5.6 promete (*"a primeira varredura as marca"*). Nada dentro de uma pasta excluída é listado, legível ou não.

### 16.4 As marcas e a purga passam pelo ORM, em pedaços de 10 000

As sessões do executor são feitas com `expire_on_commit=False`, e um `UPDATE` do Core por baixo delas deixa no mapa de identidade uma cópia velha da linha: um `save_indexed()` que escrevesse `missing_since = None` sobre um `None` velho não seria mudança para o ORM, e a linha recém-indexada continuaria marcada. É a classe de defeito do RFC-045 §14.9. As marcas e a purga são `UPDATE` e `DELETE … RETURNING` do ORM, que levam junto os objetos da sessão. E um disco reorganizado inteiro marcaria 116 396 linhas, mais parâmetros do que um comando aceita (65 535): os ids vão em pedaços de 10 000.

### 16.5 A listagem é guardada antes do denominador

O §5.3 diz que o `_record_scan()` grava o denominador e guarda a listagem sob a mesma conferência do ponto de montagem. A listagem é guardada antes da escrita: um denominador que não pôde ser gravado — o banco caiu por um instante — não é motivo para não comparar as linhas no fim.

### 16.6 Um erro sem caminho é a raiz

O `on_error` recebe o `OSError`, e a pasta ilegível é o `filename` dele. Sem um, ou com um fora da raiz, a pasta registrada é a raiz da listagem: tudo sob ela fica protegido. É a resposta conservadora para um erro que não diz onde aconteceu; nos testes e na medição, o Windows sempre disse.

### 16.7 A purga não falha por uma miniatura presa

No Windows, uma miniatura servida naquele instante não pode ser apagada. As linhas já saíram, numa transação, e são a resposta: a miniatura presa fica como lixo no cache, que é a falha que a ordem do §5.7 aceita, e a rota responde 200.

### 16.8 O cartão ganhou uma faixa, e não uma linha

O §5.8 põe a linha das ausentes e o botão *"Remover do índice"* no cartão. O cartão inteiro é um link para a tela de pastas, e um botão não pode ficar dentro de um link. A linha e o botão ficam numa faixa logo abaixo, colada ao cartão, que perde os cantos de baixo. Com uma foto, os textos vão ao singular: *"1 foto não encontrada no scan de 12/03"* e *"Remover 1 foto do HD3 do índice? O arquivo não está mais no disco. Se voltar, será indexado de novo."* Uma purga recusada (409, 404, sem resposta) fica escrita no diálogo, que continua aberto.

### 16.9 O que o desenho ganhou de nome

- Os casos de uso são `SweepMissingImagesUseCase` e `PurgeMissingImagesUseCase`, como os outros do projeto.
- A tradução de um arquivo listado para um id é `listed_identity()`, em `indexing_worker.py`, chamada por `discovered_candidates()` e pela varredura.
- Os valores novos do Domain: `RootListing` (os três campos, com `file_count` sempre igual a `len(files)`, conferido na construção), `ImagePresence`, `MissingImages` (o número e a marca mais antiga) e `PurgedImages`.
- O `_list_tree()` devolve um `ListedTree`: as entradas, as que o `walk()` classificou como arquivo e as pastas ilegíveis.
- O `JobRunner` varre com o repositório de imagens que já recebia para a leitura antecipada (`images`), que o `build_runner()` sempre passa. Sem ele, nenhum job varre, e o log diz por quê; o `World` dos testes do executor passou a passá-lo sempre.
- `GET /devices` ganhou `missing_images` e `missing_since`; a rota de purga responde `PurgedImagesSchema`.

### 16.10 A medição: a árvore do `C:` é um disco por um `DeviceLocator`

O §10 pede uma árvore no `C:`, *"registrada como dispositivo, como no RFC-039 §2.9"*. O RFC-039 registrou o próprio `C:`, e um job do disco inteiro do `C:` indexaria o sistema todo. A medição registra um dispositivo sintético e troca o `DeviceLocator` do executor por um que diz que ele está montado na raiz da árvore; tudo o mais é o `build_runner()`.

Pelo mesmo motivo, **o Explorer foi emulado**: num volume, ele move a foto para `$RECYCLE.BIN\<SID>\$R…` na raiz do volume e grava ao lado um `$I…` com a mesma extensão; no `C:`, a lixeira é a do `C:`, fora da árvore. A medição faz esse movimento dentro da árvore, que é a raiz do disco dela, e confere que nem as fotos nem os `$I…` viram linhas.

As fotos são 270 recortes das do demo, em lugares e escalas sorteados, para que nenhuma tenha o vetor de outra: a busca por uma foto movida pede um resultado idêntico, e um segundo seria a cópia antiga.

### 16.11 O teste da migration do RFC-045 grava a imagem em SQL

`test_restart_index_migration.py` gravava uma imagem pelo `PostgresImageRepository` na revisão anterior ao RFC-045. O modelo do ORM é o da última revisão, e desde esta migration ele lê `missing_since`, que aquela revisão não tem. A imagem é gravada por um `INSERT`, e o teste migra até a revisão do RFC-045, e não até a última.

### 16.12 A listagem do `E:` achou três custos de `pathlib`

A primeira listagem do `E:` pelo `walk()` saiu duas vezes mais lenta que o `rglob()`. Medido por partes, nos 123 353 arquivos dele, com o Python 3.12 desta instalação:

| o quê | custo |
|---|---|
| o `walk()` só com os nomes | 0,3 s |
| montar cada caminho, `folder / name` | +0,4 s |
| **guardar os arquivos num `set` de `Path`** | +1,5 s: o hash de um `WindowsPath` dobra as maiúsculas do texto, a cada vez |
| **`_is_excluded()` por `is_relative_to()`**, com as três pastas deste RFC | **8,9 s** (4,1 s com só a das miniaturas, a de antes), duas vezes por scan |
| **`relative_to().as_posix()`**, os caminhos do `RootListing` | **6,2 s** |

Os três passaram a comparar texto:

- os arquivos de `_list_tree()` são uma lista, e o `RootListing` pergunta *"esta entrada é um arquivo?"* pela identidade do objeto (`ListedTree.file_identities()`): as entradas e os arquivos são os mesmos objetos;
- `_is_excluded()` compara o texto do caminho, dobrado como o `pathlib` dobra o dele (`PurePath("A") == PurePath("a")` diz se dobra), com o de cada pasta excluída, igual ou seguido de um separador — que é o que impede `cache` de excluir `cache2`;
- o caminho relativo é o texto depois do da raiz, com `/`; um caminho que não começa com ele — a própria raiz — vai pelo `relative_to()`.

`test_list_tree.py` confere, entrada a entrada de uma árvore com espaços, maiúsculas e pastas de nome parecido, que cada uma dá o que o `pathlib` dá. O `_is_excluded()` já existia, na contagem do RFC-039 e no laço da descoberta, com uma pasta; este RFC o triplicou ao excluir a lixeira e a `System Volume Information`. O `relative_to()` é o do `RootListing`, novo. O `_relative()` do checkpoint, que faz o mesmo `relative_to()` por arquivo desde o RFC-029, ficou como estava: não é deste RFC.
