# RFC-036 — Launcher e Supervisão de Processos

**Status:** Implementado. A medição de §16 cumpriu os quatro critérios do roadmap numa instalação simulada com o Docker desligado: 4,46 s do comando ao `ready`, a quente, depois de o segundo ramo de §5.1 entrar (a ordem do texto deu 6,39 s, acima dos 5 s declarados); cada filho morto e recuperado; nenhum processo deixado para trás em nenhuma parada; o job longo parado de três jeitos e retomado do checkpoint, com o mesmo `attempts`. Três defeitos achados na implementação mudaram código (§17.1): a leitura bloqueante do stdin, que travava a carga das DLLs do `torch`; o mesmo caminho pelo `sys.stdin.buffer`, que derrubava a saída do interpretador; e o `run_forever` do RFC-029, que pegava de volta o job que acabara de devolver. O logoff, verificado depois à mão em oito encerramentos de sessão, derrubou a hipótese de §7.5 em duas partes, e mais três correções mudaram código (§17.1): o launcher pede ao Windows para ser encerrado antes dos filhos, e a parada do logoff não cria processo nenhum (o `job_runner` adia o Ctrl+C que cai numa chamada ao banco, e o cluster é parado pelo pipe de sinais do postmaster). Nos três últimos logoffs, o job voltou à fila com o mesmo `attempts` e o banco parou limpo
**Etapa:** 4 do [roadmap sem Docker](ROADMAP-standalone.md#rfc-036--launcher-e-supervisão-de-processos-)
**Depende de:** RFC-029 (o executor separado, o heartbeat, o reaper e o `release()` do Ctrl+C), RFC-008 (`/health`), RFC-026 §10 (o aquecimento na subida, e a corrida que ele fecha), RFC-030 §6 (a API só atende em loopback), RFC-033 (o adaptador do cluster; o [§16](rfc-033-postgres-embarcado.md#16-o-que-passa-adiante) dele é parte da lista de entrada), RFC-034 (o layout, a raiz de dados, a conexão perguntada a cada conexão, o log por programa; [§14](rfc-034-configuracao-e-diretorios-do-usuario.md#14-o-que-passa-adiante)), RFC-035 (os modelos, e a falha que não se conserta reiniciando; [§13](rfc-035-modelos-offline.md#13-o-que-passa-adiante))
**Bloqueia:** o frontend (como uma janela abre o app, e o que ela lê para saber que ele está pronto), o instalador (o comando que o atalho chama)
**Migration:** não
**Medição:** o piloto que precedeu este documento é `experiments/rfc-036-launcher/pilot_process_control_probe.py`, rodado em 2026-10-16 na máquina do piloto, com o log ao lado (§2). A medição da implementação é `measure_launcher.py`, na mesma pasta, rodada em 2026-10-18 na mesma máquina, com os logs ao lado (§16)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os números que §2 cita vêm de três fontes, e cada um diz qual: os RFCs 029 e 033 a 035, que mediram os programas que este RFC supervisiona; o piloto, que mediu como o Windows, o `pg_ctl`, o CPython e o uvicorn se comportam quando um processo pede a outro que pare; e a leitura do código instalado, que diz o arquivo. O piloto não mede nada do que este RFC propõe, porque não existe launcher. Onde nenhuma das três fontes cobre, o texto diz que é hipótese, e §16 diz como ela vai ser testada.
>
> **O piloto** tem quatro partes, e nenhuma toca o cluster, os modelos ou os dados do usuário. *Portas:* se a 8000 aceita um `bind` exclusivo, e as faixas que o Windows reservou. *Parada:* filhos Python criados como o launcher os criaria (`CREATE_NO_WINDOW`), com o thread principal em três estados, recebendo o pedido de parada por três caminhos, e com o pai morto em vez de pedir. *uvicorn:* o mesmo pedido dentro do uvicorn, com um app ASGI vazio, com uma requisição em andamento e durante um lifespan que bloqueia. *Job objects:* um "launcher" de mentira sobe um cluster de verdade, num diretório temporário, pelo adaptador do RFC-033, com e sem um job object próprio, e é morto. O cluster, o log de todo processo e a raiz de dados dos filhos ficaram num diretório temporário, apagado no fim. Só foram encerrados processos que o próprio script criou, cada um pelo PID que anotou ao criá-lo.
>
> **Três execuções.** A primeira tinha um defeito no filho de teste: a thread que vigiava o stdin escrevia o próprio registro antes de pedir a parada, e isso custou 8,5 s num dos cenários (§2.2). O log dela ficou como `pilot_process_control_probe.first-run.log`, porque é a medida do que acontece quando a ordem é a errada. A segunda parou com os registros do filho embaralhados por duas threads escrevendo no mesmo arquivo, e o script ganhou um lock. A terceira é o `pilot_process_control_probe.log`, que este documento cita.
>
> **A máquina** é a do piloto dos RFCs 034 e 035: Windows 10 Home (10.0.19045), Pentium G4560 (Intel Family 6 Model 158, 2 núcleos, 4 threads), 16 GB, HDD SATA `ST1000DM010-2EP102`, Python 3.12.9, `uvicorn` 0.51.0, `fastapi` 0.139.2, no head `f62365a`, processo sem elevação. O terminal em que o piloto rodou é o do editor.
>
> **Depois da implementação.** Os `TBM` foram medidos na mesma máquina, e os resultados estão em §16. O texto original ficou como estava. Uma decisão de §3 mudou pelo segundo ramo que o próprio texto declarou (a API criada junto com o cluster, §5.1), e o mecanismo de §7.1 mudou sem mudar a decisão: a thread pergunta ao pipe se ele acabou, em vez de lê-lo. O que a medição contrariou, o que a implementação acrescentou e o que ficou sem medição estão em §17.
>
> **O logoff, depois.** Verificado à mão de 2026-10-19 a 2026-10-21, na outra máquina do projeto: Windows 11 Home, Intel Model 126 (8 threads), NVMe `KINGSTON SNV2S1000G`, Python 3.12.5, sobre o head `191bde0`, com o launcher iniciado de um terminal e jobs sobre um HDD externo. Os resultados estão em §16, e o que mudou por causa deles, em §17.1.

---

## 1. Contexto

O RFC-033 tirou o banco do Docker, o RFC-034 tirou a configuração do checkout e o RFC-035 tirou os modelos da rede. O que sobra entre o backend e um app que se abre com um clique é quem sobe os três programas, em que ordem, quem percebe quando um deles morre, e quem os derruba sem perder trabalho.

Hoje isso é feito à mão. O `backend/README.md` descreve a sequência: `embedded start` num terminal, `python -m uvicorn app.presentation.api:app` noutro, `python -m app.infrastructure.workers.job_runner` num terceiro e, no fim, um Ctrl+C em cada um e um `embedded stop`. A instalação simulada do RFC-034 repete a mesma sequência a partir da cópia. Nenhum dos três RFCs anteriores decidiu quem faz isso, e os três deixaram para cá o que acharam pelo caminho.

Este RFC decide o comando, a ordem de subida, o que "pronto" quer dizer, o que acontece quando um filho morre, como um filho é parado sem perder o job que segura, quem para o banco, como uma segunda abertura acha a primeira e como uma janela, quando existir, abre o app. Não decide a janela, que é do frontend, nem como o código e o Python chegam à máquina, que é do instalador.

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| Ordem de subida: Postgres, migrations, depois uvicorn e `job_runner` em paralelo | §5.1 |
| Health checks antes de declarar pronto: `pg_isready` e a rota de health do RFC-008 | §5.2. Sem `pg_isready` (§2.6) |
| Política de reinício quando um filho morre | §6 |
| Encerramento ordenado: parar de aceitar jobs, cancelamento cooperativo, uvicorn, e por último o Postgres. Nenhum `postgres.exe` órfão | §7. O job é devolvido à fila, e não cancelado (§2.6) |
| Uma instância só por usuário; abrir o app duas vezes reaproveita a que está rodando | §8. Por raiz de dados (§2.6) |
| O mesmo comando no desenvolvimento e na instalação | §4.3, §9 |
| *Critério:* com o Docker desligado, um único comando deixa a API respondendo em `localhost`, em `TBM` s a quente | §16 |
| *Critério:* matar cada um dos três filhos, um de cada vez, resulta em recuperação, com o tempo medido | §6, §16 |
| *Critério:* fechar o launcher não deixa nenhum processo para trás, pela lista de processos | §7.2, §16 |
| *Critério:* um job longo interrompido pelo encerramento retoma na próxima subida | §7.1, §16 |
| *Decisão em aberto:* app de bandeja, serviço do Windows ou processo filho de uma janela | §4.1 |

**O que os RFCs anteriores passaram para cá:**

| de onde | o que passou | onde |
| --- | --- | --- |
| RFC-033 §16 | O `start()` diz se quem chamou subiu o cluster ou o adotou, e com isso o launcher decide quem o para | §7.3 |
| RFC-033 §16 | Um cluster sem dono é adotado na subida seguinte; matá-lo junto com o launcher, por job object, é uma escolha | §2.4, §7.4 |
| RFC-033 §16, corrigido | O `status()` sozinho não diz se o servidor aceita conexões; o `verify_login` distingue aceita, "ainda não" e recusa | §6.2 |
| RFC-033 §15 | O `pg_ctl` põe o postmaster num job object próprio? | §2.4 |
| RFC-033 §16, corrigido | Um programa de console criado sem `CREATE_NO_WINDOW` por um processo sem console abre uma janela | §5.5 |
| RFC-033 §14 | O encerramento no logoff | §7.5 |
| RFC-034 §14 | Um filho pode morrer na importação por causa da configuração, com uma linha no `stderr` que alguém precisa mostrar | §2.5, §6.1 |
| RFC-034 §14 | Os filhos não precisam de nada pelo ambiente | §5.5 |
| RFC-034 §14 | `DATABASE_SOURCE=embedded` no `.env` é o desenvolvimento sem Docker | §9 |
| RFC-034 §16 | A API e o `job_runner` sobrevivem a um reinício do cluster noutra porta | §6.2 |
| RFC-034 §16 | Com o cluster parado, o `/health` responde 503 em 6 ms | §5.1 |
| RFC-034 §14 | Cada programa tem o seu log, e dois processos do mesmo programa não rotacionam o arquivo | §5.5 |
| RFC-035 §13 | Um filho pode sair por causa dos modelos, e reiniciar não resolve | §2.5, §6.1 |
| RFC-035 §13 | A API sobe sem modelos e responde 503 na busca: "pronto" precisa dizer se isso conta | §5.2 |
| RFC-035 §13 | A subida da API instalada aquece os modelos: 18,7 s até o primeiro `/health` neste HDD | §2.3, §5.3 |

## 2. Problema

### 2.1 Hoje, três terminais e nenhuma ordem

Cada programa sabe parar quando recebe um Ctrl+C no próprio terminal, e é tudo o que existe de encerramento ordenado:

| programa | o que o Ctrl+C faz | onde |
| --- | --- | --- |
| `job_runner` com um job | `KeyboardInterrupt` em `run_claimed`, e `release()`: o job volta para `pending`, com o checkpoint e **sem gastar tentativa** | `job_runner.py`, `indexing_job.py` |
| `job_runner` entre jobs | `KeyboardInterrupt` em `run_forever`, e *"Job executor stopped by the operator"* | `job_runner.py` |
| A API | O uvicorn para de aceitar, espera as requisições em andamento e desliga o lifespan | `uvicorn/server.py` |
| O cluster | Não recebe: o `pg_ctl` o sobe com um console próprio (RFC-033 §8.3). Quem o para é `embedded stop` | RFC-033 §11 |

Ninguém percebe uma morte. Um `job_runner` que morre deixa o job em `running`, e é o reaper do próximo executor que o devolve à fila, depois de `job_stale_timeout` (120 s) sem heartbeat, gastando uma tentativa (RFC-029 §9.1). Um postmaster que morre fica morto, e a API responde 503 (RFC-034 §16) até alguém rodar `embedded start`.

A ordem importa em dois lugares: o cluster antes de qualquer programa que escreva, e os programas antes do cluster no fim, porque o `release()` também é uma escrita.

### 2.2 O Windows não tem um "pare, por favor"

Não há SIGTERM. Um processo que quer que outro pare tem três caminhos: `TerminateProcess`, que não roda `except` nem `finally` (o `release()` nunca acontece); um evento de console, que só chega a quem divide o console de quem o manda; ou alguma coisa que o próprio filho vigia. O piloto mediu os três do jeito que o launcher criaria os filhos, com `CREATE_NO_WINDOW`, que dá a cada filho um console que ninguém vê:

| como o pai pede | o que o filho faz | do pedido ao `except KeyboardInterrupt` do filho |
| --- | --- | --- |
| `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT)`, de um pai que não divide o console | Nada. A chamada devolveu sucesso, e o filho não recebeu evento nenhum em 5 s | — |
| Fecha o stdin; uma thread do filho vê o fim e chama `_thread.interrupt_main()` | Para pelo caminho do Ctrl+C | 0,175 s com o thread principal num laço de `time.sleep(0.2)`; 0,551 s com ele em chamadas C de 1,9 s; **58,98 s** com ele dentro de um `time.sleep(60)` |
| Fecha o stdin; a thread chama `signal.raise_signal(SIGINT)` | O mesmo | **0,000 s** no laço; 0,289 s nas chamadas C; **0,000 s** no `time.sleep(60)` |
| O pai é morto por `TerminateProcess`, sem fechar nada | O Windows fecha o lado do pai, e o filho vê o fim do stdin | 0,000 s, e o processo saiu 0,029 s depois do `TerminateProcess` |

Três leituras:

- **`interrupt_main()` não acorda um `time.sleep()` no Windows.** A explicação provável, que o piloto não isolou: ele só marca o sinal como pendente, e o `sleep` espera um evento que o handler C do SIGINT sinaliza, por onde o `raise_signal` passa e o `interrupt_main()` não. O `job_runner` dorme `job_poll_interval` (2 s) entre sondagens, e a diferença ali seria de até 2 s. Qualquer espera mais longa seria inteira.
- **A thread que vigia precisa do GIL, e espera quem o segura.** Uma chamada de extensão que não solta o GIL atrasa a parada em até uma chamada: o piloto usa `sum(range(...))`, de 1,9 s, e mediu 0,289 s. **A primeira execução mostrou quanto a ordem importa:** lá a thread escrevia o próprio registro *antes* de pedir a parada, e cada passo de entrada e saída devolvia o GIL a um thread principal que começava outra chamada de 1,9 s. A parada levou 8,48 s. A thread pede primeiro e não faz mais nada.
- **Matar o pai também é um pedido.** O fim do stdin chega do mesmo jeito, e é isso que faz este caminho cobrir também a morte do launcher (§7.4).

O uvicorn 0.51 instala o próprio handler para SIGINT, SIGTERM e SIGBREAK (`HANDLED_SIGNALS`, em `uvicorn/server.py`), e o `raise_signal(SIGINT)` de uma thread chega a ele:

| uvicorn, com o mesmo pedido | resultado |
| --- | --- |
| A porta aceita conexões | 0,474 s depois de o processo ser criado, com o app ASGI vazio |
| Parada com uma requisição de 2 s em andamento | A requisição respondeu 200 em 1,997 s, e o processo saiu 1,563 s depois do pedido, com código 0 |
| Parada 1 s depois do começo de um lifespan síncrono de 4 s | O lifespan terminou, a aplicação desligou, e o processo saiu 3,115 s depois do pedido, sem ter aceitado nenhuma conexão no intervalo |
| Depois de desligar | O uvicorn levanta de novo o sinal que capturou (`capture_signals`), e `Server.run()` termina em `KeyboardInterrupt`. Quem o chama precisa tratá-lo, como `uvicorn.main.run` trata |

### 2.3 A API só abre a porta depois de aquecer

Três fatos, cada um medido ou lido em outro lugar, que juntos dão o tempo que um app levaria para mostrar alguma coisa:

- **O uvicorn roda o lifespan antes de abrir a porta.** Em `Server.startup`, `await self.lifespan.startup()` vem antes de `loop.create_server`. O piloto viu a porta recusar conexões durante todo o lifespan.
- **O lifespan da API aquece os dois modelos**, quando `warm_up_models` está ligado, e ele vem ligado no modo instalado desde o RFC-035 §8.
- **Importar a API importa o torch.** `app/presentation/dependencies/__init__.py` importa no topo do módulo `ClipEmbeddingModel`, `MarianQueryTranslator` e `resolve_device`, e os três importam `torch` ou `transformers`. O RFC-034 §9 mediu a importação da API em 13 a 18 s neste HDD, quase toda no torch.

O resultado está no RFC-035 §15: **18,7 s até o primeiro `/health`** na instalação simulada, dos quais 4,5 s de aquecimento. Durante esse tempo nada responde: nem o `/health`, nem dispositivos, pastas, mapa, miniaturas ou jobs, e nenhuma dessas rotas usa modelo (RFC-035 §6.5). A meta de subida do `ARCHITECTURE.md` §22 é 5 s, e o RFC-026 registrou por que ela não cabe num processo que carrega dois modelos antes de responder. Ela pode caber num processo que responde antes de carregá-los.

Uma coisa mantém o aquecimento onde está: **o carregamento preguiçoso dos adaptadores não é protegido.** `_ensure_loaded`, em `clip_embedding_model.py` e em `query_translator.py`, confere `self._model is None` e carrega, sem lock. O RFC-026 §9.1 fechou a corrida de duas primeiras buscas aquecendo antes de aceitar qualquer requisição. O RFC-035 serializou a conferência dos hashes (`verified_directory`, em `model_runtime.py`, com um lock), e não o `from_pretrained`. Um aquecimento que não segura a porta reabre a corrida.

### 2.4 Todo processo daqui já está num job object

O processo que rodou o piloto estava num job object (`IsProcessInJob`). Antes do script, foi conferido à mão que um processo criado pelo WMI (`Win32_Process.Create`) também está num, e que um filho criado com `CREATE_BREAKAWAY_FROM_JOB` é criado sem erro e continua num job. **Um launcher não pode contar com subir fora de um job**, nem decidir isso por quem o iniciou.

O RFC-033 §15 perguntou se o `pg_ctl` põe o postmaster num job object próprio. O `pg_ctl.exe` do runtime importa `IsProcessInJob`, `CreateJobObjectA`, `SetInformationJobObject` e `AssignProcessToJobObject`, e traz o texto `PostgreSQL_%lu` (lido do binário). **Hipótese:** ele cria um job só quando o processo que acabou de criar não está em nenhum. Esse caso não pôde ser produzido aqui. O que o piloto viu é o que importa para um launcher: com o "launcher" num job próprio, o job continha o próprio launcher, o `cmd.exe` que o `pg_ctl` cria, um `conhost.exe` e os oito `postgres.exe`. O postmaster e os filhos dele herdam o job de quem chama.

| o "launcher" morto por `TerminateProcess` | o cluster | a subida seguinte, pelo adaptador |
| --- | --- | --- |
| Sem job próprio | Os processos do servidor continuam vivos 10 s depois | Adota o cluster em 0,053 s, sem recuperação |
| Num job com `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` | Todos os processos do job saem em 0,018 s | Sobe em 1,757 s, com recuperação pelo WAL |

Os dois são seguros para os dados: o RFC-033 §18 matou a árvore do servidor sob escrita e não perdeu nenhuma transação confirmada. A diferença está no que acontece aos outros dois filhos, e é §7.4.

### 2.5 Uma falha que não se conserta reiniciando sai como qualquer outra

O RFC-034 §5.5 e o RFC-035 §6.5 fizeram de "não posso subir" uma linha no `stderr`, `SolidVision cannot start: …`, e o código de saída 1. É também o código com que o Python sai de uma exceção que ninguém tratou, com traceback. Quem só vê o código não distingue "o arquivo de configuração é inválido", que reiniciar não conserta, de "um defeito derrubou o processo", que reiniciar pode consertar. Ler o texto faria do formato da linha um contrato que ninguém declarou.

O custo de não distinguir é concreto: um `job_runner` cujo peso do CLIP não confere importa o torch (14 a 40 s neste HDD, RFC-035 §16.2) e confere o hash (4,7 s a frio) antes de recusar, a cada reinício.

### 2.6 Sete coisas que o roadmap presumiu

**"`pg_isready` e a rota de health."** O `pg_isready` não é necessário: o `start()` do adaptador só volta depois de um login aceito (RFC-033 §8.3 e §16), o que é mais forte. O `/health` é a sonda da API, mas hoje o primeiro 200 dele vem depois do aquecimento (§2.3), e "pronto" tem dois níveis: a API respondendo, e a busca respondendo. O RFC-035 §13 perguntou qual deles conta (§5.2).

**"O launcher só precisa reiniciar o `job_runner`."** Duas correções. Reiniciar não devolve o job: o job de um executor que morreu fica em `running` até o reaper, 120 s depois do último heartbeat, e volta à fila gastando uma tentativa. O launcher sabe que o executor morreu, mas a linha do job não diz quem a segura, e a CLI também executa jobs no próprio processo (RFC-029 §12): devolver o job na palavra do launcher poderia pôr dois workers num disco (§6.3). E há saídas que não devem ser reiniciadas (§2.5).

**"Encerramento ordenado: parar de aceitar jobs, cancelamento cooperativo."** O cancelamento cooperativo do RFC-029 §8 termina o job como `cancelled`, um estado final, e um job cancelado não volta. Isso contradiz o quarto critério. O que o critério pede é o caminho do Ctrl+C: o `release()`, de volta a `pending`, com o checkpoint e sem gastar tentativa (§2.1). E não há o que "parar de aceitar": a fila é a tabela, e um job criado durante a parada é uma linha `pending` à espera da próxima subida.

**"Nenhum `postgres.exe` órfão."** Depois de um encerramento ordenado, nenhum (§7.2). Depois de um launcher morto, o cluster órfão é o resultado certo, e não uma falha: matá-lo junto com o launcher o mataria antes de o `job_runner` devolver o job (§7.4).

**"Uma instância só por usuário."** Por raiz de dados, que é por usuário e por modo (RFC-034 §6.2). Um checkout e um app instalado na mesma máquina têm raízes, clusters e portas diferentes, e podem rodar ao mesmo tempo. Duas contas do Windows têm um cada.

**"O mesmo comando no desenvolvimento e na instalação."** O comando é o mesmo. O que muda é o banco: num checkout, a fonte default é `external`, o Docker, e um launcher não sobe nem migra um banco que não é dele (§9). O desenvolvimento sem Docker é a linha que o RFC-034 §7.1 já deu.

**"Bandeja, serviço do Windows ou processo filho de uma janela."** As três opções misturam duas perguntas: que processo supervisiona o backend, e quem o mantém vivo. O serviço responde mal às duas (§11). As outras duas são o mesmo launcher com pais diferentes, e a escolha entre elas é do frontend (§4.1).

**O que o roadmap não pediu, e o piloto tornou necessário:** um canal de parada (§7.1), um código de saída para "não adianta reiniciar" (§6.1), a escolha da porta da API (§5.4), a API em dois tempos (§5.3) e o logoff (§7.5), que o RFC-033 passou para cá e o roadmap não listou.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| O que o launcher é | Um processo Python sem janela e sem torch, um por raiz de dados. Não é serviço do Windows nem a janela. Quem o mantém vivo é quem o iniciou: um terminal, ou uma janela que segura o stdin dele (§4.1) |
| O comando | `python -m app.launcher`, igual nos dois modos; `status` e `stop` para outra linha de comando (§4.3) |
| O que ele diz | Eventos em JSON no `stdout`, um por linha; texto para gente no `stderr` (§4.4) |
| A ordem | O lock; a configuração; o cluster e as migrations, quando a fonte é `embedded`; a API e o `job_runner` ao mesmo tempo (§5.1) |
| Pronto | `/health` 200. A busca é um segundo nível, que o `/health` passa a informar (§5.2) |
| A API | Sobe em dois tempos: a porta abre sem o torch, e o aquecimento roda numa thread depois. O carregamento dos modelos ganha um lock (§5.3) |
| A porta da API | A da última subida, se estiver livre; senão, uma que o sistema der. `API_PORT` a fixa. Sempre `127.0.0.1` (§5.4) |
| Como um filho é parado | O launcher fecha o stdin dele, e uma thread do filho chama `signal.raise_signal(SIGINT)`. É o Ctrl+C, pelo caminho que o RFC-029 já testou (§7.1) |
| A ordem da parada | A API e o `job_runner` juntos, com prazo; `TerminateProcess` só depois do prazo; o cluster por último (§7.2) |
| Quem para o cluster | Um launcher para o cluster que um launcher subiu, inclusive o deixado por um launcher que morreu. Nunca um que outro programa subiu (§7.3) |
| Se o launcher morrer | A API e o `job_runner` param sozinhos pelo stdin, e o job volta para a fila. O cluster fica, e é adotado. Sem job object (§7.4) |
| Reinício | Um filho que sai com 78 não é reiniciado. Os outros, com espera crescente e um limite de mortes. O cluster é subido de novo quando o postmaster morre, e só então (§6) |
| "Não adianta reiniciar" | Código de saída **78** nos programas que hoje saem com a linha `SolidVision cannot start` e código 1. A linha não muda (§6.1) |
| Instância única | Um arquivo com lock exclusivo na raiz de dados, e um arquivo de estado ao lado. A segunda abertura diz onde está a primeira e sai (§8) |
| O logoff | Uma janela oculta recebe o fim da sessão, e a parada ordenada roda nela (§7.5) |
| O desenvolvimento | O mesmo comando. Com a fonte `external`, o launcher não mexe no banco (§9) |

## 4. O launcher

### 4.1 Um processo, e quem o mantém vivo

O launcher é um processo sem janela que sabe subir, vigiar e parar os outros três, e mais nada: não serve HTTP, não carrega modelo e não importa o torch, o FastAPI ou o uvicorn (um teste mantém isso, §10). A vida dele é a de quem o iniciou:

| quem inicia | como ele para |
| --- | --- |
| Um terminal (desenvolvimento, suporte, a validação) | Ctrl+C no terminal, ou `python -m app.launcher stop` de outro |
| Uma janela, com `--supervised` | A janela fecha o stdin do launcher, ou morre, e o Windows o fecha por ela |

É assim que a decisão em aberto do roadmap se resolve. "Filho de uma janela" e "app de bandeja" são o mesmo launcher, com um pai que segura o stdin dele. A diferença é quando o pai solta: quando a janela fecha, ou quando o usuário escolhe "Sair" num ícone de bandeja, com a indexação continuando de janela fechada. Isso é uma decisão de produto, e é a casca da janela (Electron, Tauri ou WebView2) que a implementa. As duas funcionam com o contrato de §4.4 sem mudar uma linha do launcher. O serviço do Windows está descartado (§11): o cluster é por usuário (RFC-033 §13), e um serviço roda na sessão 0, de onde o reveal do RFC-030 não abre o Explorer na área de trabalho do usuário.

O que a janela precisa saber está em §4.4 e §14. O protótipo de cliente Electron medido em 2026-09-22 (`experiments/rfc-032-client-foundation/measure_cold_start.log`, de uma numeração anterior dos RFCs de frontend, cujo código não está no repositório) mostrou a janela em 802 ms e o primeiro `/health` em 875 ms, com o backend já de pé. Quem abre o app espera, portanto, pelo backend, e esse é o número deste RFC (§16).

### 4.2 O pacote

`backend/app/launcher/` é uma raiz de composição, como `app/cli/`:

| módulo | responsabilidade |
| --- | --- |
| `__main__.py` | Os comandos de §4.3. O único que lê o `Settings` |
| `supervisor.py` | A máquina de estados: ordem de subida, pronto, reinício e parada. Recebe os filhos, o cluster, o relógio e a sonda HTTP no construtor, para os testes de §10 |
| `children.py` | Um filho: a criação, o stdin, as duas threads que leem a saída dele, as últimas linhas, a parada e o `TerminateProcess` |
| `instance.py` | O lock, o arquivo de estado e o evento de parada (§8) |
| `events.py` | O protocolo de §4.4 |
| `session_end.py` | A janela oculta do logoff (§7.5) |

E um módulo fora do pacote: `app/infrastructure/process/supervision.py`, com `stop_on_stdin_eof()`, que a API, o `job_runner` e o próprio launcher chamam quando recebem `--supervised` (§7.1).

Nada no Domain nem na Application. Supervisionar processos é implantação, como o ciclo de vida do cluster (RFC-033 §4.1). O launcher depende de Windows em `children.py`, `instance.py` e `session_end.py`, como o adaptador do cluster depende em `windows_processes.py`.

### 4.3 Os comandos

```text
python -m app.launcher                 sobe tudo e supervisiona, até ser parado
python -m app.launcher --supervised    o mesmo, e para quando o stdin chegar ao fim
python -m app.launcher status          o que está de pé, e onde
python -m app.launcher stop            pede ao launcher de pé que pare, e espera
```

`stop` sem launcher de pé confere o arquivo de estado: se ele diz que um launcher subiu o cluster, e o cluster está de pé, para o cluster (§7.4). Senão, diz que não há nada a parar.

Num checkout, os comandos rodam da raiz do repositório com `backend` no caminho de módulos, como os do cluster (RFC-033 §11). Instalado, o atalho ou a janela chamam o mesmo módulo com o interpretador da instalação.

| código de saída | quando |
| --- | --- |
| 0 | Parou em ordem; ou já havia um launcher de pé, e este disse onde (§8) |
| 78 | Não pôde subir, e tentar de novo não muda isso: a configuração inválida, o cluster recusado (outra versão, outro runtime, a senha perdida), a API que desistiu de subir (§6.1) |
| 1 | Qualquer outra coisa, com o traceback no log |

### 4.4 Os eventos

O `stdout` do launcher só leva eventos, um objeto JSON por linha, gravados um a um. Quem iniciou o launcher o lê para saber quando abrir a janela. O `stderr` leva texto para gente: as linhas de log do launcher e a saída dos filhos (§5.5). É a separação do RFC-033 §11: o `stdout` fica para o leitor que é um programa.

```json
{"event": "starting", "protocol": 1, "pid": 4120, "mode": "installed"}
{"event": "phase", "phase": "starting"}
{"event": "phase", "phase": "migrating"}
{"event": "phase", "phase": "api"}
{"event": "ready", "url": "http://127.0.0.1:51234", "search": "loading", "indexing": "running"}
{"event": "child", "name": "job_runner", "state": "unavailable", "detail": "SolidVision cannot start: …"}
{"event": "stopping"}
{"event": "stopped"}
```

| evento | quando |
| --- | --- |
| `starting` | O primeiro, com a versão do protocolo |
| `phase` | `initdb`, `starting`, `recovering` e `migrating`, que são o `on_phase` do RFC-033; e `api` e `job_runner`, quando cada filho é criado |
| `ready` | Uma vez, quando o `/health` respondeu 200 (§5.2), com a URL e o estado da busca e da indexação |
| `already_running` | No lugar de todos os outros, quando outro launcher já está de pé: a URL e o estado dele. O processo sai com 0 (§8) |
| `child` | Um filho reiniciando, desistido, ou que recusou subir, com a última linha que deixou |
| `address` | A API voltou numa porta diferente (§5.4) |
| `stopping`, `stopped` | O começo e o fim da parada |
| `failed` | O launcher não pôde subir, com a linha. O processo sai com 78 |

`protocol` muda quando um evento muda de sentido; acrescentar um campo não o muda. **Quem não lê o `stderr` o manda para `NUL`:** um pipe que ninguém esvazia enche, e trava o launcher na escrita seguinte.

## 5. A subida

### 5.1 A ordem

1. **O lock da raiz de dados** (§8). Se outro launcher o segura, `already_running`, e o processo sai com 0, sem carregar a configuração nem tocar o cluster.
2. **A configuração**, por `load_settings`. Um arquivo inválido é `failed`, com a linha, e 78. O launcher recusa pelos filhos, que leriam o mesmo arquivo no mesmo modo e recusariam do mesmo jeito.
3. **O cluster e as migrations, quando a fonte é `embedded`**: `start(on_phase)` e `prepare(on_phase)`, e cada fase do adaptador vira um `phase`. Uma `EmbeddedPostgresError` é `failed` e 78. Com a fonte `external`, nada: o launcher não sobe, não migra e não para um banco que não é dele (§9).
4. **A API e o `job_runner` ao mesmo tempo** (§5.5). O `job_runner` carrega o CLIP antes de sondar a fila (RFC-029 §9.1), e ninguém espera por ele: um job criado antes de ele estar pronto espera na tabela.
5. **Pronto**: o primeiro 200 do `/health`, na URL da API (§5.2). O launcher pergunta a cada 100 ms. Uma conexão recusada é "ainda não", e um 503 também: com o cluster parado, o `/health` responde 503 em 6 ms (RFC-034 §16). O prazo é `TBM`, a partir da medição de §16, e depois dele vem `failed`.

A subida da API não espera o cluster por um motivo de cada filho: a API serviria requisições durante as migrations, e, na primeira execução, o `job_runner` chegaria ao reaper, a primeira consulta que faz, antes de o `initdb` terminar. O que se ganharia é a subida do cluster a quente, 1,11 s no NVMe do RFC-033 §18. É o segundo ramo, se o critério de §16 falhar.

### 5.2 O que é pronto

Dois níveis:

| nível | o que funciona | quem diz |
| --- | --- | --- |
| **Pronto** | Dispositivos, pastas, mapa, miniaturas, detalhes e jobs: tudo o que não usa modelo | `/health` 200, que exige o banco (RFC-008) |
| **A busca** | A busca por texto | O campo novo `search` do `/health` |

O launcher declara pronto no primeiro nível, e põe no evento `ready` o valor de `search` que viu. Quem desenha a caixa de busca continua perguntando ao `/health`; o launcher não acompanha o segundo nível.

O `/health` ganha dois campos:

| `search` | quando |
| --- | --- |
| `loading` | O aquecimento está rodando (§5.3) |
| `ready` | O aquecimento terminou, e os dois modelos estão carregados |
| `unavailable` | O aquecimento falhou com `ModelUnavailableError`. `search_detail` traz a linha do RFC-035 §6.4 |
| `on_demand` | O aquecimento está desligado, que é o default num checkout, ou falhou por outro motivo: a primeira busca carrega |

O código de resposta não muda. O `/health` continua respondendo pelo banco, e um 200 com `search: unavailable` é o caso do RFC-035 §6.5: uma API que mostra o acervo sem buscar. É a resposta à pergunta do RFC-035 §13: conta como pronto, e diz o que falta. A interface, pela régua do RFC-031 §9, desenha algo diferente para cada valor. Se isso também vai para o `/capabilities` é do frontend.

### 5.3 A API em dois tempos

Três mudanças, e nenhuma no que as rotas fazem:

- **O torch sai da importação da API.** As três importações de `app/presentation/dependencies/__init__.py` que trazem o `torch` e o `transformers` (§2.3) passam para dentro de `get_embedding_model()`, como a composição do `job_runner` já faz em `build_runner`. Importar `app.presentation.api` deixa de importar o `torch` e o `transformers`, e um teste confere isso num subprocesso (§10). A ordem do RFC-035 §5.2 não muda: o pacote `ai` continua sendo importado antes, por `model_runtime`, e é ele que liga o modo offline.
- **O aquecimento vai para uma thread.** O lifespan inicia o `_warm_up_models()` do RFC-026 numa thread daemon e volta. O uvicorn abre a porta logo em seguida (§2.3). A thread atualiza o estado que o `/health` lê. Uma falha continua sendo o que o RFC-035 §6.5 decidiu: vai para o log, e a API fica de pé.
- **O carregamento ganha um lock.** O `_ensure_loaded` de cada adaptador passa a carregar uma vez só, com a conferência repetida dentro do lock. É a corrida do RFC-026 §9.1, que o aquecimento bloqueante evitava e que a thread reabre: uma busca que chega durante o aquecimento passa a esperar o carregamento em andamento, em vez de começar um segundo.

**O que custa, declarado.** Uma busca que chega durante o aquecimento espera por ele, até o aquecimento inteiro: `TBM` em §16. O RFC-035 mediu 4,5 s de aquecimento neste HDD, e agora a importação do torch também está dentro da thread. A busca não responde 503: "carregando" não é "indisponível", e quem digitou espera segundos em vez de ver um erro. Também `TBM`: quanto a thread, importando o torch, atrasa as respostas do thread principal, que disputam o mesmo GIL (§2.2).

**O que ganha.** A porta abre no tempo da importação da API sem o torch: `TBM`. A única referência é parcial: o RFC-034 §9 mediu o tempo próprio dos módulos do projeto em cerca de 1,6 s, sem as bibliotecas de terceiros.

**O que não muda:** o `job_runner`, que carrega o CLIP antes de sondar a fila (RFC-029 §9.1); a CLI; e o default de `warm_up_models` por modo (RFC-035 §8). Com a thread, o default de desenvolvimento continua desligado pelo motivo do RFC-035: o `TestClient` da suíte rápida começaria a carregar 924 MB ao fundo.

### 5.4 A porta da API

A 8000 de hoje é o default do `python -m uvicorn`. O argumento é o que o RFC-033 §8.2 fez contra a 5432: qualquer número fixo pode estar ocupado, e a 8000 é uma das portas mais usadas por servidores de desenvolvimento. Nesta máquina ela está livre, e nenhuma das seis faixas reservadas a cobre (piloto). A máquina do RFC-033, com o Docker Desktop, tinha 16 faixas, e elas se mexiam.

A política é a do cluster:

1. A porta da última subida que chegou a `ready`, do arquivo de estado (§8), se um `bind` exclusivo a aceitar.
2. Se não, um `bind` na porta 0, e a porta que o sistema der.
3. A API recebe a porta na linha de comando. Entre a sonda e o `bind` do uvicorn há a importação da API (§5.3), e outro processo pode tomar a porta nesse intervalo. O uvicorn então sai com 3 (`STARTUP_FAILURE`, em `uvicorn/config.py`), antes do pronto. O launcher trata uma saída 3 da API antes do pronto como porta tomada, e volta ao passo 2, até 3 vezes, o número do RFC-033.

**Um setting novo, `api_port`, vazio por default.** Preenchido, o launcher usa essa porta ou recusa com 78 e uma linha, em vez de escolher outra: uma porta fixada é uma promessa a alguém, um script ou o proxy de um servidor de desenvolvimento do frontend. O `.env.example` ganha `# API_PORT=8000`, comentado.

**O endereço é sempre `127.0.0.1`**, sem setting. A segunda guarda do RFC-030 §6 parte disso, e servir a rede é um não-objetivo (§12). A porta escolhida não é uma proteção (§13).

Quando a API é reiniciada (§6.1), ela tenta a mesma porta primeiro. Se não puder, vai para outra, e o launcher emite `address`.

### 5.5 Os filhos

```text
<python> -X utf8 -m app.presentation.api --port 51234 --supervised
<python> -X utf8 -m app.infrastructure.workers.job_runner --supervised
```

`<python>` é o interpretador do próprio launcher (`sys.executable`). As duas linhas são montadas numa função só, a única a mudar quando o runtime for congelado, como o RFC-034 §4.1 fez com a raiz.

- **`app.presentation.api` ganha um `__main__`.** Ele roda o `uvicorn.Server` com o app já importado, a porta, `--supervised` e um `timeout_graceful_shutdown` (§7.2), e trata o `KeyboardInterrupt` que o uvicorn levanta de novo depois de desligar (§2.2). O nome do log sai da regra do RFC-034 §8.2: `api.log`. `python -m uvicorn app.presentation.api:app` continua funcionando, com o `uvicorn.log`, para quem quer `--reload` (§9).
- **`CREATE_NO_WINDOW` nos dois.** Cada filho ganha um console que ninguém vê. Nenhuma janela abre quando o launcher não tem console (RFC-033 §16, medido no Windows 11), e um Ctrl+C no terminal do desenvolvedor chega só ao launcher, que ordena a parada (§7.2). É o motivo que o RFC-033 §8.3 mediu para o servidor.
- **O stdin é um pipe** em que só o launcher escreve, e ele nunca escreve: o pipe existe para ser fechado (§7.1).
- **O `stdout` e o `stderr` são pipes**, cada um lido por uma thread do launcher que nunca para de ler. Cada linha vai para o `stderr` do launcher com o nome do filho na frente, e as últimas 50 linhas de cada filho ficam na memória, para o evento `child` e para o log do launcher quando o filho morre. Os filhos continuam gravando os próprios logs (RFC-034 §8.2); o `launcher.log` só recebe as últimas linhas de quem morreu.
- **Ninguém herda esses pipes.** O RFC-033 manteve a saída do servidor longe de pipes porque o servidor herdaria o pipe do `pg_ctl`. Aqui, nem a API nem o `job_runner` iniciam processos de longa duração. O único processo que a API cria é o Explorer do reveal, com os três handles em `DEVNULL` (`file_revealer.py`), e o `subprocess` do Python 3.12 no Windows só faz o filho herdar os handles que recebe (`close_fds`, o default). **Hipótese, a testar em §16:** quando um filho sai, as duas threads que o leem terminam.
- **`-X utf8`.** Num pipe, o Python escreve na página de código, 1252 nesta máquina, e um caminho fora dela numa linha de log vira um erro de codificação no handler de console. Com o modo UTF-8, o pipe leva tudo, e o launcher decodifica em UTF-8. **Hipótese, testada em §16** com o nome que o RFC-034 §7.5 usou: `Łódź/東京/✈.jpg`.
- **O ambiente é o do launcher, sem acréscimos.** Os filhos calculam o mesmo layout e leem o mesmo arquivo (RFC-034 §5.2). As variáveis `PG*` já saíram: o launcher as tirou do próprio ambiente ao chamar o adaptador (RFC-034 §14).

## 6. A supervisão

### 6.1 Os filhos

O launcher espera a saída de cada filho, pelo handle do processo. O que faz depende de quando e de como:

| a saída | o que o launcher faz |
| --- | --- |
| Durante a parada (§7) | Nada: é o esperado |
| Código 78, a qualquer momento | Não reinicia, e emite `child` com a última linha. Se é a API, para tudo e sai com 78: sem API não há app. Se é o `job_runner`, continua, com a indexação indisponível |
| Código 3 da API, antes do pronto | A porta foi tomada: outra porta (§5.4) |
| Qualquer outra | Reinicia, depois da espera da vez |

**78 quer dizer "não adianta reiniciar".** Os programas saem com 78 onde hoje saem com 1 e a linha: na `ConfigurationError` do singleton do `Settings` (RFC-034 §5.5), que vale para todo programa, e na `ModelUnavailableError` do `job_runner` e da CLI (RFC-035 §6.5). A linha não muda. 78 é o `EX_CONFIG` do `sysexits.h`. No Windows o número não significa nada para o sistema, e foi escolhido por não colidir com o que o Python e o uvicorn já usam: 1 para exceção, 2 para argumento, 3 para a falha de subida do uvicorn e `0xC000013A` para um Ctrl+C que ninguém tratou. A constante vai para `app/infrastructure/config/constants.py`, e os testes do RFC-034 e do RFC-035 que esperam 1 passam a esperar 78.

**A espera entre reinícios cresce:** zero para a primeira morte, depois 2, 5, 15 e 30 s, e volta a zero depois de o filho ficar dez minutos de pé. **O limite:** cinco mortes em dez minutos, e o launcher desiste daquele filho como se ele tivesse saído com 78. São escolhas, e não medições. O que elas precisam fazer é não encher o disco de tracebacks nem a CPU de importações do torch, de 14 a 40 s cada neste HDD, por um filho que morre na primeira imagem.

**Um filho que trava sem morrer não é detectado.** O launcher não reinicia a API por um 503: um 503 do `/health` é o banco, e o banco é §6.2. Uma API travada é um risco (§13).

### 6.2 O cluster

Duas mortes diferentes, e o launcher só age numa:

| o que morreu | o que acontece sozinho | o launcher |
| --- | --- | --- |
| Um processo do servidor, que não o postmaster | O postmaster encerra os outros e reinicializa. Por alguns segundos, toda conexão é recusada com *"in recovery mode"*, e o `postmaster.pid` continua dizendo `ready`. O `/health` ficou em 503 por 7,6 s (RFC-034 §16) | Nada |
| O postmaster | Nada. O cluster fica parado | `start()`, que recupera pelo WAL: de 4,0 a 8,0 s depois de uma morte sob escrita (RFC-033 §18) |

Para distinguir as duas, o `status()` basta. Ele lê o `postmaster.pid` e a tabela de processos, sem lock e sem conexão, e responde `stopped` quando o PID do postmaster está morto (RFC-033 §4.2). O RFC-033 §16 corrigiu a ideia de que o `status()` servia para supervisão, e a correção vale para a pergunta que respondia: se o servidor aceita conexões. O launcher pergunta outra coisa: se há servidor. Se ele aceita conexões, quem pergunta é o `start()` seguinte, com o `verify_login` e o prazo de conexão que o RFC-033 §16 mostrou ser necessário. O launcher chama o `status()` a cada 2 s, uma escolha. O `locate()`, que faz as mesmas leituras, custou 2,54 ms (RFC-034 §16).

**A API e o `job_runner` não são reiniciados junto com o cluster.** A engine pergunta a porta a cada conexão nova, e o RFC-034 §16 mediu a mesma API respondendo 200 depois de o cluster voltar noutra porta. O `job_runner` pode morrer no intervalo: uma consulta do reaper sem banco levanta uma exceção, e nada em `run_forever` a trata. É uma morte como as outras, e §6.1 o reinicia. Se ele segurava um job, vale §6.3.

O limite de reinícios de §6.1 vale também para o cluster. Passado o limite, o launcher para tudo e sai com 78, com o fim do `startup.log` que o adaptador já põe nos erros dele.

### 6.3 O job de um executor que morreu

O launcher sabe que o executor morreu. A linha em `indexing_jobs` não sabe: ela não registra quem segura o job, e o reaper só age depois de `job_stale_timeout` (120 s) sem heartbeat, gastando uma tentativa (RFC-029 §9.1). Devolver o job à fila na palavra do launcher erraria num caso que existe: `app.cli index` executa o job que criou no próprio processo (RFC-029 §12), e um launcher que devolvesse todo job `running` ao reiniciar o executor poria um segundo worker no disco da CLI.

O tempo para o job voltar depois da morte do executor é, portanto, o do reaper: até 120 s depois do último heartbeat, e uma tentativa. Isso está declarado, e é medido em §16. Dar à linha um dono, o processo e a hora em que ele começou, deixaria o reaper devolver na hora o job de um dono morto. É uma migration, e não é deste RFC (§12).

O caso comum, a parada ordenada, não passa por aqui. Ele devolve o job sem esperar e sem gastar tentativa (§7.1).

## 7. A parada

### 7.1 Pedir para parar: o stdin

`stop_on_stdin_eof()` inicia uma thread daemon que lê o stdin do processo até o fim, e então chama `signal.raise_signal(signal.SIGINT)`, antes de qualquer outra coisa. O thread principal recebe o que receberia de um Ctrl+C:

| programa | o que o SIGINT faz | desde |
| --- | --- | --- |
| `job_runner` com um job | `KeyboardInterrupt` em `run_claimed`, e `release()`: o job volta para `pending`, com o checkpoint e sem gastar tentativa | RFC-029 |
| `job_runner` entre jobs | `KeyboardInterrupt` em `run_forever`, e *"stopped by the operator"* | RFC-029 |
| `job_runner` carregando o modelo | `KeyboardInterrupt` em `warm_up_now`, sem job na mão. Hoje sai com traceback; passa a sair em silêncio, com 0 | Este RFC |
| A API | O handler do uvicorn: para de aceitar, espera as requisições em andamento e desliga o lifespan | uvicorn |
| O próprio launcher, com `--supervised` | A parada de §7.2 | Este RFC |

Por que este caminho, e não os outros que o piloto mediu (§2.2): um evento de console exige dividir o console, e dividir devolveria aos filhos o Ctrl+C do terminal do desenvolvedor, fora da ordem; o `interrupt_main()` não acorda um `sleep`; e o `TerminateProcess` pula o `release()`. E porque o fim do stdin chega também quando o launcher morre, sem ninguém pedir (§7.4).

A thread só é iniciada com `--supervised`. Sem o argumento, os programas se comportam como hoje: um desenvolvedor que roda o `job_runner` à mão num terminal não quer que ele pare quando o stdin acabar. É um argumento de linha de comando, e não uma variável de ambiente, porque no modo instalado o ambiente não é fonte de nada (RFC-034 §5.2), e um argumento aparece na lista de processos para quem investiga.

**Hipótese, a medir em §16:** com o `job_runner` no meio de um lote do CLIP de verdade, a interrupção chega em menos de um segundo, porque os operadores do torch soltam o GIL. O piloto mediu 0,289 s com chamadas de 1,9 s que o seguram.

### 7.2 A ordem

1. `stopping`, e o arquivo de estado passa a `stopping`.
2. O launcher fecha o stdin da API e do `job_runner`, ao mesmo tempo.
3. Espera cada um, com prazo: 15 s para a API, cujo uvicorn dá 5 s às requisições em andamento (`timeout_graceful_shutdown`), e 30 s para o `job_runner`. Passado o prazo, `TerminateProcess`, com o custo dito no log: para o `job_runner`, o job que ele segurava fica em `running` até o reaper (§6.3). Os prazos são escolhas, e §16 mede quanto da folga se usa.
4. O cluster, se o launcher for o dono (§7.3): `stop()`, que só volta quando os processos do servidor saíram (RFC-033 §8.5).
5. `stopped`. O arquivo de estado é apagado, e o lock se solta com a saída do processo.

A API e o `job_runner` param juntos porque nenhum depende do outro. O que os dois precisam é do banco, e por isso ele é o último: o `release()` é uma escrita.

Um segundo Ctrl+C durante a parada, ou um segundo `stop`, pula os prazos: `TerminateProcess` nos filhos que restam, e depois o `stop()` do cluster. É a regra do `run --` do RFC-033 §11: o primeiro é um pedido, o segundo é uma ordem.

### 7.3 Quem para o cluster

O `start()` diz se quem chamou subiu o cluster ou o adotou (RFC-033 §4.2), e o `run --` usa isso para deixar o cluster como o encontrou. O launcher precisa de um caso a mais: o cluster deixado por um launcher que morreu (§7.4), que o próximo adota e que ninguém mais pararia.

A regra: **um launcher para o cluster que um launcher subiu.** O arquivo de estado registra como o cluster chegou: `started`, `adopted` ou `external`. Um launcher que adota um cluster e acha o arquivo de um launcher que já não vive (o lock está livre) dizendo `started` herda a posse, e escreve `started` no próprio. Um cluster que um desenvolvedor subiu com `embedded start` é adotado e deixado de pé no fim, como o `run --` faz.

### 7.4 Se o launcher morrer

Pelo Gerenciador de Tarefas, por uma exceção, por um terminal fechado:

- **A API e o `job_runner` param sozinhos.** O Windows fecha o lado do launcher dos pipes de stdin, e cada filho vê o fim: o piloto mediu 0,029 s do `TerminateProcess` no pai até a saída do filho (§2.2). O `job_runner` devolve o job (§7.1), e para isso precisa do banco de pé.
- **O cluster fica.** É por isso. Com um job object `KILL_ON_JOB_CLOSE`, o cluster sairia em 0,018 s (§2.4), antes de o `job_runner` chegar ao `release()`. O job ficaria em `running` para o reaper (§6.3), e um launcher morto três vezes durante o mesmo job o levaria a `failed`.
- **O próximo launcher adota e herda** (§7.3): a adoção levou 0,053 s no piloto (§2.4). Sem um próximo, `python -m app.launcher stop` para o cluster, e o logoff o termina, para a subida seguinte recuperar.

O que sobra de um launcher morto é, portanto, um cluster sem clientes à espera de ser adotado. Está declarado.

### 7.5 O logoff

No fim da sessão, o Windows termina o que restar. Um `job_runner` terminado no meio de um job deixa o job para o reaper, e gasta uma tentativa (§6.3): quem encerra a sessão toda noite com uma indexação em andamento veria o job falhar na terceira noite.

Os eventos de console não resolvem. **Segundo a documentação do `SetConsoleCtrlHandler`**, um processo de console que carrega `user32.dll` ou `gdi32.dll` não recebe `CTRL_LOGOFF_EVENT` nem `CTRL_SHUTDOWN_EVENT`, e é tratado como aplicação de janela; o Python com `ctypes` e o torch carregam as duas. O caminho documentado é uma janela. O launcher cria uma janela de nível superior oculta (não uma janela só de mensagens, que, pela mesma documentação, não recebe mensagens difundidas), numa thread que processa `WM_QUERYENDSESSION` e `WM_ENDSESSION`. No `WM_ENDSESSION`, roda a parada de §7.2 com prazos menores, depois de registrar um motivo com `ShutdownBlockReasonCreate` ("SolidVision está devolvendo a indexação à fila").

**Hipótese, a verificar à mão em §16:** o launcher recebe o `WM_ENDSESSION` antes de o Windows terminar os filhos, e a parada cabe na paciência do sistema. Se não couber, o custo é o do primeiro parágrafo, e o reaper da sessão seguinte recupera o job. O RFC-033 deixou o logoff sem medição (§19.3 dele), e esta verificação o cobre também.

## 8. Uma instância por raiz de dados

- **O lock** é `<dados>\launcher.lock`, aberto com lock exclusivo (`msvcrt.locking`, como o `adapter.lock` do RFC-033) durante toda a vida do launcher. O Windows o solta quando o processo termina, de qualquer jeito, e não existe lock velho para limpar.
- **O estado** é `<dados>\launcher.json`, reescrito de uma vez (um arquivo temporário e uma troca) a cada mudança: o PID do launcher, o estado (`starting`, `ready`, `stopping`), a URL da API, os PIDs dos filhos e como o cluster chegou (§7.3). Ele nunca é a fonte da verdade sobre haver um launcher: o lock é. Um arquivo de estado com o lock livre é de um launcher que morreu, e serve para duas coisas só: a última porta da API (§5.4) e a posse do cluster (§7.3).
- **A segunda abertura** não consegue o lock, lê o arquivo de estado, emite `already_running` com a URL e o estado, e sai com 0, sem carregar a configuração nem tocar o cluster. Trazer para a frente a janela que já existe é da casca (o Electron, por exemplo, tem um lock de instância única próprio), e §14 diz isso.
- **O evento de parada** é um evento nomeado, `Local\SolidVision-launcher-<16 hexadecimais do sha256 da raiz de dados>`, que o launcher cria e espera. O `stop` o sinaliza e espera o lock ficar livre. É por sessão (`Local\`) e por raiz de dados: o `stop` de um checkout não para o app instalado.

Por raiz de dados, e não por usuário, porque é a raiz de dados que não pode ter dois donos: o cluster, os logs, as miniaturas. O checkout usa `SolidVision-dev`, e o app instalado `SolidVision` (RFC-034 §6.2). Os dois launchers rodam lado a lado, cada um com o seu cluster e a sua porta. O `/health` diz o modo (RFC-034 §14), e com isso uma casca pode conferir que a URL que recebeu é do modo que esperava.

## 9. No desenvolvimento

```powershell
$env:PYTHONPATH = "backend"
python -m app.launcher
```

- **Com `DATABASE_SOURCE=embedded` no `.env`**, tudo: o cluster de `SolidVision-dev`, a API e o `job_runner`, com o Docker desligado. Um `pytest` noutro terminal vai ao mesmo cluster pelo `locate()` (RFC-034 §7.3), como hoje vai ao contêiner que a API também usa.
- **Com `external`, o default de um checkout**, o launcher sobe a API e o `job_runner` e não mexe no banco: nem `start`, nem migrations, nem `stop`. O evento `starting` diz isso. O contêiner é de outra pessoa, como o Docker do desenvolvedor no RFC-033 §19.2.
- **O launcher não tem `--reload`.** Quem desenvolve a API com recarga automática roda `python -m uvicorn app.presentation.api:app --reload` à mão, com o cluster de pé por `embedded start`, como hoje.
- **`embedded stop` com um launcher de pé** para o cluster, e o launcher o sobe de novo em até 2 s (§6.2): ele não distingue o desenvolvedor de uma queda. O comando para parar tudo é `python -m app.launcher stop`, e o `backend/README.md` diz isso.

O default de `database_source` não muda, e o `docker-compose.yml` continua. Tirar o Docker do dia a dia é uma linha no `.env` (RFC-034 §7.1). Apagar o compose é outra decisão (§12).

## 10. Testes

Na suíte padrão, sem cluster, sem modelos e sem processos de verdade, exceto onde diz subprocesso:

- **A máquina de estados**, com filhos, cluster, relógio e sonda HTTP de mentira: a ordem de §5.1; o pronto só depois do 200; o 78 não reiniciado; a sequência de esperas e o limite; a saída 3 da API antes do pronto trocando de porta; a API desistida parando tudo com 78; a ordem de §7.2, com o cluster depois dos filhos; o prazo e o `TerminateProcess`; o segundo Ctrl+C.
- **Quem para o cluster:** subido, para; adotado, não para; adotado com o arquivo de um launcher morto dizendo `started`, para; `external`, nunca chama o adaptador.
- **A porta:** a última reaproveitada; ocupada, outra; `API_PORT` ocupada, 78 com a linha.
- **A instância:** um segundo launcher na mesma raiz emite `already_running` e sai com 0 sem importar o `Settings` (subprocesso); duas raízes convivem; o arquivo de estado é reescrito de uma vez.
- **O canal de parada**, em subprocesso: com `--supervised`, fechar o stdin para um `time.sleep(60)` em menos de 1 s, e o `except KeyboardInterrupt` roda; sem o argumento, nada acontece.
- **O `job_runner`** com `--supervised` e um indexador de mentira: fechar o stdin no meio de um job o deixa `pending`, com o checkpoint e o mesmo `attempts`.
- **A API em dois tempos:** importar `app.presentation.api` não põe `torch` nem `transformers` em `sys.modules` (subprocesso); o `/health` responde `on_demand`, `loading`, `ready` e `unavailable` com um aquecimento de mentira; duas primeiras cargas simultâneas de um adaptador chamam o `from_pretrained` uma vez só (com um dublê).
- **Arquitetura:** nenhum módulo de `app/launcher/` importa, nem transitivamente, `torch`, `transformers`, `fastapi` ou `uvicorn` (subprocesso, `sys.modules`).
- **O 78:** o singleton do `Settings` e o `job_runner` saem com 78 e a mesma linha. São os testes do RFC-034 e do RFC-035, atualizados.
- **Os eventos:** cada linha é JSON válido com `event`, e a primeira traz `protocol`.

**Sob o marcador `embedded`**, pulados sem `runtime\postgres`: um launcher com filhos de mentira (scripts que só dormem e respondem um `/health`), contra um cluster de verdade num diretório temporário. A subida, o pronto, a parada ordenada, e a lista de processos sem nada do cluster no fim. O launcher morto, o cluster adotado e herdado pelo seguinte.

**Sob o marcador `slow`**, pulados sem `runtime\models`: o launcher inteiro, com a API e o `job_runner` de verdade, e um job sobre as imagens do RFC-022 interrompido pela parada e retomado na subida seguinte.

## 11. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Serviço do Windows | Exige administrador para instalar, roda na sessão 0 com uma conta que não vê o `%LOCALAPPDATA%` do usuário, não abre o Explorer na área de trabalho dele (o reveal do RFC-030) e fica ligado com o app fechado. O cluster é por usuário (RFC-033 §13) |
| O launcher dentro da janela (o processo principal do Electron) | A supervisão seria escrita em outra linguagem, e o backend não subiria sem a janela: nem no desenvolvimento, nem na validação, nem num suporte por terminal |
| Um ícone de bandeja em Python (`pystray`, ou um laço de mensagens próprio) | Uma dependência para uma decisão de produto que a casca da janela toma melhor, e que o contrato de §4.4 deixa aberta |
| A API no processo do launcher | O supervisor importaria o FastAPI, e uma falha da API derrubaria quem a reinicia |
| supervisord, NSSM, pywin32 | Dependências novas, e nenhuma resolve o que é deste RFC: a ordem, o job devolvido e o dono do cluster |
| Um job object com `KILL_ON_JOB_CLOSE` | O cluster sairia antes de o `job_runner` devolver o job (§7.4) |
| Um job object só para o cluster, com a API e o `job_runner` fora dele (`CREATE_BREAKAWAY_FROM_JOB`) | O mesmo problema. E o job de quem iniciou o launcher pode não deixar sair (§2.4) |
| `CTRL_BREAK_EVENT`, com os filhos no console do launcher | Exige dividir o console, e no desenvolvimento isso devolveria aos filhos o Ctrl+C do terminal, fora da ordem. Sem dividir, o evento não chega (§2.2) |
| `AttachConsole` no console de cada filho, para mandar o evento | Um processo se prende a um console por vez, e um launcher num terminal teria de soltar o dele |
| `_thread.interrupt_main()` | Não acorda um `time.sleep()` no Windows (§2.2) |
| `TerminateProcess` como a parada | Pula o `release()`: o job espera o reaper e gasta uma tentativa. E corta as requisições em andamento |
| Uma rota HTTP de desligamento na API | Uma rota que qualquer processo da máquina, e qualquer página aberta no navegador, alcança em loopback. E o `job_runner` não tem HTTP |
| Um evento nomeado por filho | Não dispara quando o launcher morre. O fim do stdin dispara nos dois casos |
| Uma bandeira de parada conferida entre lotes, como o `cancel_requested` | Só é vista entre lotes e janelas, e a primeira janela materializa a lista inteira do disco antes de qualquer conferência, num tempo que num disco frio não foi medido (RFC-029 §9.1). O `KeyboardInterrupt` chega entre instruções |
| Cancelar os jobs na parada, como o roadmap disse | Um job cancelado não volta (§2.6) |
| Devolver na hora o job de um executor que morreu | Pode tirar o job de um `app.cli index` vivo (§6.3) |
| Reiniciar a API e o `job_runner` junto com o cluster | Desnecessário: a engine pergunta a porta a cada conexão (RFC-034 §16) |
| `pg_isready` | O `start()` já confere o login (RFC-033 §16) |
| Supervisionar a API pelo `/health` | Um 503 é o banco, que §6.2 já cuida. Reiniciar a API por ele a reiniciaria a cada reinicialização do servidor |
| Ler a linha `SolidVision cannot start` para não reiniciar | Faria do texto um contrato. O 78 diz o mesmo sem formato (§6.1) |
| A porta 8000 fixa | O argumento do RFC-033 §8.2 contra a 5432, para uma das portas mais usadas por servidores de desenvolvimento |
| O filho escolhe a porta e a informa ao launcher | Precisaria de um canal do filho para o launcher. A sonda e a troca de porta na saída 3 cobrem a corrida com o que já existe (§5.4) |
| O launcher abre o socket e o entrega à API (`socket.share`) | Fecha a corrida de §5.4 inteira, com uma passagem de handle entre processos que nenhum outro ponto do projeto tem |
| Subir a API antes do cluster | É o segundo ramo, se o critério de §16 falhar: ganha no máximo a subida do cluster, e a API serviria durante as migrations (§5.1) |
| Manter o aquecimento no lifespan | 18,7 s até o primeiro `/health` neste HDD, para rotas que não usam modelo (§2.3) |
| Responder 503 à busca durante o aquecimento | Quem digitou espera segundos em vez de ver um erro. O lock faz a busca esperar o carregamento em andamento (§5.3) |
| Um mutex nomeado para a instância única | Equivalente. O arquivo na raiz de dados tem o escopo certo, e fica à vista de quem procura (§8) |
| Uma variável `SOLIDVISION_SUPERVISED` no lugar de `--supervised` | O RFC-034 §11 a deixou como porta. O argumento serve, aparece na lista de processos e não reabre o ambiente como fonte no modo instalado |
| Os eventos por um pipe nomeado ou um socket | Toda casca de janela sabe ler o `stdout` de um processo que criou |
| Um executor por dispositivo (`worker_count`) | A inferência em CPU já usa todos os núcleos, e cada executor carrega 600 MB do CLIP. O campo continua sem leitor (RFC-029 §9) |
| Um processo só escrevendo o log de todos (RFC-034 §11) | Com um processo por programa, nenhum arquivo é dividido (§5.5) |

## 12. Não-objetivos

- **A janela**: a bandeja, a escolha entre Electron, Tauri e WebView2, e como a interface é servida (§14).
- **Autenticação entre a janela e a API** (§14).
- **O instalador**, o atalho, iniciar com o Windows e congelar o runtime.
- **Servir a rede.** A API só escuta em `127.0.0.1`.
- **Mais de um executor.**
- **`--reload` sob o launcher** (§9).
- **Um dono na linha do job** (§6.3), que é uma migration.
- **Um `job_runner` que sobrevive a uma queda do banco sem morrer** (§6.2).
- **Tirar o Docker do desenvolvimento**: o default de `database_source` e o `docker-compose.yml` ficam como estão.
- **Linux e macOS.**

## 13. Riscos

| risco | situação |
| --- | --- |
| O cluster órfão depois de um launcher morto | Declarado (§7.4). O próximo launcher o adota e herda; `stop` e o logoff o encerram |
| Uma chamada que segura o GIL atrasar a parada | O piloto mediu o atraso de uma chamada (§2.2). O prazo de §7.2 cobre o resto, com o custo dito |
| O logoff terminar os filhos antes do launcher | Hipótese (§7.5), verificada à mão em §16. *Aconteceu num de dois logoffs; com o launcher pedindo para ser encerrado antes, os filhos estavam vivos em seis de seis (§17.1)* |
| A documentação do `SetConsoleCtrlHandler` não valer como foi lida | A mesma verificação. *Nem confirmada nem desmentida: com o launcher encerrado antes, a parada chega aos filhos antes de o Windows chegar a eles (§17.3)* |
| *Um processo criado durante o logoff* | *Não estava na lista. Morre ao nascer (`0xC000026B`), e um processo do servidor que morre assim derruba o cluster. A parada do logoff não cria nenhum (§17.1)* |
| A primeira busca depois de abrir o app esperar o aquecimento | Declarado (§5.3). `TBM` |
| A thread do aquecimento deixar o `/health` lento enquanto importa o torch | `TBM` (§5.3) |
| Um neto herdar o pipe de saída de um filho | Hipótese (§5.5). A thread de leitura não terminaria, e §16 confere |
| A porta tomada entre a sonda e o `bind` | Coberto pela saída 3 (§5.4) |
| **Uma página aberta no navegador chamando a API** | Existe hoje, com a 8000 fixa, e a porta escolhida só a torna menos óbvia. É do frontend (§14) |
| O protocolo de eventos virar um contrato mal desenhado | Tem versão desde a primeira linha, e um consumidor só, o frontend, que ainda não existe |
| Um desenvolvedor parar o cluster com `embedded stop` e o launcher o subir de novo | Declarado (§9) |
| Um launcher elevado | Um aviso no log. O perfil errado é do instalador (§14) |
| A API travar sem morrer | Não detectado (§6.1) |

## 14. O que passa adiante

**Para o frontend:**

- **O contrato.** Iniciar `<python> -m app.launcher --supervised` com o stdin e o `stdout` em pipes e o `stderr` lido ou em `NUL`, sem janela (`CREATE_NO_WINDOW`; no Node, `windowsHide`). Ler eventos até `ready`, `already_running` ou `failed`. Abrir a janela na `url`. Acompanhar `search` no `/health`. Para fechar, fechar o stdin do launcher e esperar `stopped`.
- **Janela ou bandeja** é da casca: o que muda é quando ela fecha o stdin (§4.1).
- **Como a interface é servida.** Dos arquivos da própria casca, como no protótipo Electron (`app://solidvision`), que precisou de CORS, que a API não tem hoje; ou pela própria API, na mesma origem. O launcher dá uma URL nos dois casos.
- **A porta não é uma proteção.** Qualquer processo da máquina, e qualquer página aberta no navegador do usuário, pode mandar requisições a `127.0.0.1:<porta>`, e a segunda guarda do RFC-030 vê um chamador em loopback em todos esses casos. Uma porta escolhida torna o alvo menos óbvio, não inalcançável. A medida é um token por sessão, que o launcher gera e a janela envia. Ele muda todo cliente, e por isso é do frontend, que é o primeiro cliente que não é um terminal.
- **A segunda abertura:** trazer a janela que já existe para a frente é da casca. O `already_running` é a rede de segurança.
- **A indexação indisponível** chega como evento `child` (§6.1). Se ela também precisa estar no `/capabilities` é a régua do RFC-031 §9.

**Para o instalador:**

- O atalho chama `python -m app.launcher`, ou o que o runtime congelado puser no lugar (§5.5), pela janela.
- "Abrir o app ao terminar a instalação", com um instalador elevado, abre o launcher elevado, com o problema de perfil do RFC-034 §13. O launcher escreve um aviso no log quando o grupo Administradores está valendo no token dele. É o grupo que se lê, e não o `TokenElevation` (RFC-033 §8.3).
- Uma atualização precisa parar o launcher (`stop`) antes de trocar o runtime: o RFC-033 §16 mediu que trocar o runtime com o cluster de pé não falha sozinho.
- Iniciar com o Windows é uma decisão de produto, e o launcher não precisa de nada para ela.

**Para quem mexer nos jobs:** a coluna de dono, que deixaria o reaper devolver na hora o job de um executor morto (§6.3).

## 15. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-036-launcher-e-supervisao-de-processos.md` | Este documento |
| `backend/app/launcher/__init__.py`, `__main__.py`, `supervisor.py`, `children.py`, `instance.py`, `events.py`, `session_end.py` | O launcher (§4.2) |
| `backend/app/infrastructure/process/__init__.py`, `supervision.py` | `stop_on_stdin_eof()` (§7.1) |
| `backend/app/presentation/api/__main__.py` | A API como filho (§5.5) |
| `backend/tests/launcher/` | Os testes de §10 sobre o launcher |
| `backend/tests/test_launcher_imports_architecture.py` | Nada pesado no launcher; nada de torch ao importar a API (§10) |
| `experiments/rfc-036-launcher/measure_launcher.py` (+ `.log`, fora do git) | §16 |

O `pilot_process_control_probe.py` já existe na mesma pasta. É o que §2 cita.

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/workers/job_runner.py` | `--supervised`; 78 na falha dos modelos; a parada durante o carregamento sem traceback |
| `backend/app/infrastructure/config/settings.py` | O singleton sai com 78; `api_port` |
| `backend/app/infrastructure/config/constants.py` | `EXIT_CANNOT_START = 78` |
| `backend/app/cli/search.py`, `backend/app/infrastructure/workers/indexing_worker.py` | 78 na falha dos modelos |
| `backend/app/presentation/dependencies/__init__.py` | As importações dos modelos dentro de `get_embedding_model()` (§5.3) |
| `backend/app/presentation/api/__init__.py` | O aquecimento numa thread, e o estado que o `/health` lê |
| `backend/app/presentation/routes/health.py` | `search` e `search_detail` |
| `backend/app/infrastructure/ai/clip_embedding_model.py`, `query_translator.py` | O lock do carregamento |
| Os testes do RFC-034 e do RFC-035 que esperam o código 1 | 78 |
| `.env.example` | `# API_PORT=8000` |
| `backend/README.md` | O launcher no lugar dos três terminais (§9) |
| `README.md` | O comando de subida. O RFC-033a deixou esta mudança para quando o RFC-036 mudasse o comando |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-standalone.md` | O status deste RFC |

Não mudam: o Domain, a Application, as migrations, o adaptador do PostgreSQL e o CLI dele, o `model_runtime.py` e o `docker-compose.yml`.

## 16. Validação

Os `TBM` do corpo do texto ficaram como foram escritos, à vista do que se esperava; os números que os respondem estão aqui, e o que mudou por causa deles, em §17.

**Declarado antes da medição:**

- **"A quente" é do comando ao evento `ready`**, com o cluster parado e os arquivos já no cache do sistema por uma subida anterior, na máquina do piloto, na instalação simulada. **Critério: no máximo 5 s**, a meta do `ARCHITECTURE.md` §22. Acima disso, entra o segundo ramo: a API passa a ser criada junto com o cluster (§5.1), e o número é medido de novo. O tempo de cada fase sai dos eventos, para que a decomposição diga onde está o resto.
- **"Nenhum processo para trás" é conferido pela lista de processos, pela identidade:** os PIDs que o launcher criou, e todo `postgres.exe`, `cmd.exe` e `conhost.exe` cujo executável seja deste runtime ou cujo pai seja um deles. É o critério do RFC-033 §18, com os processos que o piloto achou no job (§2.4).
- **"Retoma" é o job voltar a `running` na subida seguinte, a partir do checkpoint, com o mesmo `attempts`**, e terminar com todas as imagens do escopo.
- **Toda verificação tem prazo** ([RFC-034 §2.1](rfc-034-configuracao-e-diretorios-do-usuario.md#21-o-backend-só-existe-dentro-de-um-checkout)).
- **O que o piloto já mediu não é medido de novo:** os caminhos de parada e os job objects, como comportamento do Windows (§2.2, §2.4). A medição abaixo é do launcher.

> **Medido em 2026-10-18, na máquina do piloto**, com o Docker desligado: Windows 10 Home (10.0.19045), Pentium G4560 (2 núcleos, 4 threads), 16 GB, HDD SATA `ST1000DM010-2EP102`, Python 3.12.9, processo sem elevação. Numa instalação simulada só para leitura (`stage_installation.py --read-only`), com um perfil vazio e o `%TEMP%` dos filhos vazio, cada launcher iniciado como uma janela o iniciaria: stdin e `stdout` em pipes, sem janela. **Quatro logs** ao lado do script, fora do git, cada um com a sua razão: `measure_launcher.first-branch.log`, a subida na ordem do texto, que falhou o critério e fez o segundo ramo entrar; `measure_launcher.log`, todas as verificações com o segundo ramo, antes de três correções; `measure_launcher.rerun.log`, as verificações que as correções mudaram; e `measure_launcher.job.log`, o job longo sobre o dobro do corpus, para caberem três paradas. Cada número abaixo diz de qual log vem quando não é do último que o mediu.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte | `pytest` puro; `-m embedded`; `-m slow` | **2713 passed, 91 deselected**, em 114,8 s: eram 2599 passed e 88 deselected no `f62365a`, contados antes de qualquer mudança. **`-m embedded`: 27 passed**, em 311,4 s; **`-m slow`: 64 passed**, em 225,5 s. As três contra um cluster embarcado num diretório temporário, por `embedded run --`, com o Docker desligado. Numa rodada anterior da suíte padrão, um teste do RFC-033 sensível a tempo (`test_an_orphan_that_leaves_by_itself_within_the_grace_is_not_ended`, com prazo de 0,2 s) falhou uma vez; passou nas cinco vezes seguintes, isolado, e nesta rodada |
| Um comando, Docker desligado, a frio | Instalação simulada só para leitura, raiz de dados vazia: a primeira subida, com `initdb` | **35,0 s** (`rerun`): o launcher em 0,66 s, o `initdb` de 1,21 a 28,93 s, as migrations em 30,69 s, a API e o `job_runner` criados em 31,77 s, e o `ready` em 34,96 s, com a busca em `loading`. Na ordem do texto, 68,0 s, com 45,5 s de `initdb`; com o segundo ramo antes do refinamento de §17.1, **109,0 s**, porque o aquecimento da API disputava o disco com o `initdb`, que levou 98,9 s |
| O mesmo, a quente | A segunda subida | **Mediana de 4,460 s** (4,437 a 4,620 s, cinco subidas): **dentro do critério.** O launcher diz `starting` em 0,61 a 0,68 s, cria a API em 1,11 a 1,22 s e sobe o cluster ao lado dela; cluster e migrations terminam entre 2,58 e 3,22 s, e o `ready` vem com o primeiro 200 da API, 3,3 s depois de ela ser criada. **Na ordem do texto, mediana de 6,390 s** (6,037 a 9,743 s, `first-branch`), acima do critério: 2,1 s de cluster e migrations, e só então os 3,6 s da API. Com o segundo ramo antes do refinamento, 4,989 s (`log`) |
| A busca depois do pronto | Do `ready` ao `search: ready`; e uma busca em português feita logo depois do `ready` | **Com o acervo indexado e o cache quente, 17,5 s do `ready` ao `search: ready`**, e a busca em português enviada no `ready` respondeu 200 em **15,0 s**, com 10 resultados: esperou o carregamento em andamento, sem 503. O `job_runner` entrou no laço 18,8 s depois do comando. Com o índice vazio, 17,4 a 23,9 s. **Logo depois da primeira subida a frio, 104,9 s** (`first-branch`): os dois processos importando o `torch` ao mesmo tempo, de um disco frio (§17.1) |
| O `/health` durante o aquecimento | A latência dele enquanto a thread importa o torch | **Mediana de 4 a 7 ms, máximo de 0,51 a 0,57 s**, em 73 a 227 perguntas a cada 200 ms por aquecimento. A thread disputa o GIL, e o pior caso é meio segundo |
| A importação da API sem o torch | O tempo, e `sys.modules` | **Mediana de 2,126 s** (1,850 a 2,418 s, cinco processos, instalada), **sem `torch` nem `transformers`** (`log`). Antes deste RFC, 20,2 s (`-X importtime`, no checkout), 17,7 s deles no adaptador do CLIP |
| Matar a API | `TerminateProcess` com o app pronto; o tempo até o `/health` voltar a 200 | **4,58 s**, na mesma porta, sem `address` (`log`). A primeira morte reinicia sem espera |
| Matar o `job_runner` com um job | O tempo até um executor novo entrar no laço; o tempo até o job voltar a `running`, e o `attempts` | **Um executor novo no laço em 12,9 s; o job de volta a `running` em 121,2 s, com `attempts` 1** (`rerun`; 122,2 s no `log`). É o custo que §6.3 declarou: o reaper, 120 s depois do último heartbeat, e uma tentativa |
| Matar o postmaster | O tempo até o launcher perceber, até o `start()` voltar e até o `/health` voltar a 200; a API e o `job_runner` com os mesmos PIDs | **Percebido em 1,66 s; o `start()` voltou e o `/health` deu 200 aos 18,6 s; a API e o `job_runner` com os mesmos PIDs** (`log`). A recuperação pelo WAL é a maior parte: o RFC-033 §18 mediu 4,0 a 8,0 s num SSD |
| Matar um processo do servidor | O launcher não faz nada, e o `/health` volta sozinho | **O launcher não tocou no cluster, e o `/health` voltou a 200 sozinho aos 20,7 s**, com o mesmo postmaster (`log`). O `job_runner` morreu duas vezes no intervalo, na consulta do reaper sem banco, e foi reiniciado com 0 e 2 s de espera: o que §6.2 previu |
| Um filho que sai com 78 | `runtime\models` renomeado: o `job_runner` não é reiniciado, e a API sobe com `search: unavailable`. Um `solidvision.env` inválido: o launcher sai com 78 e a linha | **Como dito, nos três casos** (`log`). Os modelos fora do lugar (por `MODEL_RUNTIME_DIRECTORY` no `solidvision.env`, porque a árvore instalada é só para leitura): `child` com `job_runner`, `unavailable`, 78 e a linha, sem reinício; `ready` com `search: unavailable`, e o `/health` com a linha em `search_detail`. Um `BATCH_SIZE=auto`: `starting`, `failed` com a linha, e 78. E `API_PORT` numa porta tomada: `failed` com a linha, e 78 |
| Um filho que morre em laço | Um `job_runner` que morre ao subir (um dublê): as esperas e a desistência | **Esperas de 0, 2, 5 e 15 s, e a desistência na quinta morte**, 92,6 s depois da primeira; a API de pé o tempo todo (`log`). O dublê é `DEVICE=nonsense` no `solidvision.env`: o `job_runner` levanta ao montar o modelo, depois de importar o `torch`, a cada subida |
| Parar com um job longo | Um job sobre as 45 imagens do RFC-022, parado no meio por `stop`, por Ctrl+C e pelo fim do stdin. O estado do job, o checkpoint, o `attempts`, a lista de processos e o tempo de cada etapa da parada | **Os três, como dito** (`job`, sobre o dobro do corpus: 90 imagens, para caberem três paradas de pelo menos um lote). Parado no meio por `stop` em 11,4 s, por Ctrl+C em 2,8 s e pelo fim do stdin em 2,4 s, do pedido à saída do launcher: o `job_runner` em 1,1 a 1,2 s, a API em 1,3 s, e o cluster depois dos dois, em 0,8 a 9,2 s. Antes de cada parada o job estava em `running`; na subida seguinte, em `pending`, com o checkpoint em que parou e `attempts` 0. A lista de processos, conferida depois de cada parada, não tinha nada do launcher, dos filhos nem do cluster. Com as 45 imagens, o mesmo no `log` e no `rerun` |
| Retomar | A subida seguinte: o job volta a `running` do checkpoint e termina com as 45 | **Retoma** (`job`): na quarta subida, o job estava em `pending`, com o checkpoint da parada anterior e `attempts` 0, voltou a `running` e terminou `completed`, com `attempts` 0 e as 90 imagens do escopo indexadas. O teste `slow` faz o mesmo com as 45, pela API |
| Matar o launcher com um job | `TerminateProcess` no launcher: os filhos saem, o job volta a `pending` e o cluster fica. A subida seguinte adota, herda, e para o cluster no fim | **Como dito** (`log`). Os filhos saíram 1,38 s depois do `TerminateProcess`, pelo fim do stdin, e o cluster ficou. A subida seguinte adotou o mesmo postmaster e o herdou (`started`); o job estava `pending`, com `attempts` 0 e o checkpoint, e terminou com `attempts` 0. No fim, o cluster parou, e nada ficou |
| A segunda abertura | Um segundo `python -m app.launcher` com o primeiro de pé, e com o primeiro ainda subindo | **Mediana de 520 ms** (468 a 828 ms) com o primeiro de pé: `already_running` com o PID, a URL e `ready`, e 0. **Com o primeiro subindo, 514 ms**, e `starting` com o PID (`rerun`); antes da correção de §17.1, o estado da parada anterior. Nenhum processo dela ficou para trás, em nove aberturas conferidas a 0, 0,5 e 2 s |
| Checkout e instalado lado a lado | Os dois launchers ao mesmo tempo | **Os dois de pé**, cada um com o seu cluster e a sua porta: o checkout pronto aos 38,6 s (com o `initdb` dele) e o instalado aos 9,3 s (`log`) |
| A porta | A última porta ocupada por outro socket: outra porta e `address`. `API_PORT` ocupada: 78 | **Como dito** (`rerun`). A última porta tomada na subida: outra, em silêncio, porque a URL ainda não tinha sido dada. Tomada durante o reinício da API: `address` com a porta nova, 5,26 s depois. `API_PORT` tomada: 78 (`log`) |
| O pipe e a página de código | Uma linha de log com `Łódź/東京/✈.jpg` pelo pipe | **Intacta**, sem nenhum caractere de substituição: as linhas do `job_runner` que nomeiam o arquivo chegaram ao stderr do launcher como foram escritas (`log`). O arquivo não é uma imagem, para que o `job_runner` o nomeasse ao recusá-lo |
| Os pipes terminam com os filhos | As threads de leitura de um filho que saiu | **Terminam.** Um teste espera as duas threads de um filho que saiu. Na medição, paradas inteiras de 1,25 a 2,13 s a quente (`rerun`) não caberiam se o launcher tivesse esperado os 2 s que dá às threads de cada filho |
| Nada gravado fora da raiz de dados | As comparações do RFC-034 §9, com o launcher | **Nada.** O perfil fora da raiz de dados, o `%TEMP%` dos filhos, a árvore instalada e o diretório de trabalho ficaram iguais em uma subida a frio, cinco a quente e as seis paradas. A raiz de dados ganhou `launcher.json` e `launcher.lock`, ao lado de `cache`, `logs` e `postgres` |
| O logoff | À mão: encerrar a sessão com um job em andamento. Na sessão seguinte, o estado do job, o `attempts` e o `startup.log` do cluster | **Feito depois, em oito logoffs, na outra máquina** (nota do início). *Com o código deste RFC, dois logoffs (2026-10-19):* no primeiro, os filhos estavam vivos e pararam em 0,3 s, mas um processo do servidor terminado pelo Windows (`0x40010004`) derrubou o cluster; no segundo, os filhos e o postmaster já estavam mortos quando a parada começou, e o job foi para o reaper, com uma tentativa a menos. *Com o launcher pedindo para ser encerrado antes, três (2026-10-20):* os filhos estavam vivos nos três, mas a devolução do job morreu em dois, num processo do servidor que não conseguiu nascer (`0xC000026B`), e o job chegou à tentativa 2 de 3; no terceiro a devolução foi feita, e o `pg_ctl stop` não nasceu. *Com a parada sem processos, três (2026-10-21):* **o job voltou à fila nos dois que tinham um, inclusive num logoff 20 s depois de o launcher subir, e terminou com `attempts` 0**; o `job_runner` saiu com 0 em 2,7 a 4,8 s; o cluster recebeu *"fast shutdown request"* e parou em 0,3 a 0,5 s; e as subidas seguintes começaram de *"database system was shut down"*, com `recovered: False`. A API levou 3 s ou mais para sair nos seis logoffs com o launcher encerrado antes, e foi terminada no prazo de 3 s em cinco (§17.3). Os detalhes, em §17.1 |
| Estilo e tipos | `black --check`, `ruff check`, `mypy` | **Limpos.** `black --check` e `ruff check` sobre `backend/app`, `backend/tests`, `backend/dataset_tools`, o `stage_installation.py` e o script da medição; `mypy` com a configuração do projeto, 325 arquivos |

## 17. Correções feitas durante a implementação

Registradas à vista do texto original, como os RFCs 033 a 035 fizeram com as suas. Uma muda uma decisão de §3 pelo segundo ramo que o próprio texto declarou (§5.1). Duas mudam o mecanismo de §7.1 sem mudar a decisão, porque o caminho que o piloto mediu falhou em dois casos que o piloto não tinha. E uma conserta um defeito do RFC-029 que §7.1 pressupunha resolvido. As três últimas linhas de §17.1 vieram depois, do logoff: a decisão de §7.5 (uma janela oculta, e a parada ordenada rodando nela) ficou, e o que mudou foi o que a parada pode fazer enquanto a sessão acaba.

### 17.1 O que a medição contrariou

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §5.1, §16 | A ordem: o cluster e as migrations, e depois a API e o `job_runner` ao mesmo tempo; o critério de 5 s a quente, e o segundo ramo se ele falhasse | **Falhou, e o segundo ramo entrou.** Na ordem do texto, mediana de 6,39 s (6,04 a 9,74 s, cinco subidas): 2,1 s de cluster e migrations e, só depois, 3,6 s da criação da API ao primeiro 200. A API passou a ser criada junto com o cluster, e o `job_runner` continua esperando as migrations, pelo motivo de §5.1: a primeira consulta dele é a do reaper. Medido de novo: **mediana de 4,46 s** (4,44 a 4,62 s). **E um refinamento, também medido:** na primeira execução, o aquecimento da API, que importa o `torch` e confere 924 MB de modelos, disputava o disco com o `initdb`, que foi de 45 s para 99 s, e a subida a frio de 68 s para 109 s. Sem cluster nenhum, a API espera o `initdb`, como na ordem do texto; com cluster, sobe junto. A subida a frio voltou a 35,0 s. Custa um `status()` antes da subida |
| §7.1, §2.2 | O canal de parada: uma thread que lê o stdin até o fim e então chama `signal.raise_signal(SIGINT)`, o caminho que o piloto mediu | **A leitura bloqueante falhou duas vezes, e a thread passou a perguntar ao pipe em vez de lê-lo.** (1) Uma leitura síncrona pendente no pipe do stdin faz o Windows segurar as outras chamadas sobre aquele handle, e carregar uma DLL cuja inicialização olha os handles padrão é uma delas: com a thread parada em `os.read(0, 1)`, `import numpy` travou por mais de 40 s na reprodução, contra 0,27 s sem a thread. Sob o launcher, o `job_runner` ficou 13 minutos dentro de `numpy._core.multiarray`, no `import torch` (pilha tirada com `py-spy`), até o stdin fechar: nunca chegou ao laço. O aquecimento da API, que importa o `torch` depois de o canal começar, travaria do mesmo jeito. (2) Lendo por `sys.stdin.buffer`, o lock do leitor fica preso durante a leitura, e um programa que termina sozinho não consegue fechar o stdin na finalização do interpretador: o 3 do uvicorn com a porta tomada virou `0xC0000005`, com *"Fatal Python error: _enter_buffered_busy"*, três vezes em três. A thread agora pergunta ao pipe com `PeekNamedPipe` a cada 100 ms, sem ler e sem esperar, e o fim do arquivo é o `ERROR_BROKEN_PIPE`: na reprodução, `numpy` e `torch` importados em 3,39 s, e a parada 0,479 s depois de o stdin fechar. Custa até 100 ms em cada parada. O piloto não viu nenhum dos dois porque os filhos dele não importavam nada depois de iniciar a thread, e só terminavam pelo fim do stdin. Os dois casos têm teste |
| §2.1, §7.1 | *"`job_runner` com um job: `KeyboardInterrupt` em `run_claimed`, e `release()`"*, desde o RFC-029 | **O job voltava à fila, e o mesmo executor o pegava de novo na volta seguinte do laço.** `run_claimed` captura o `KeyboardInterrupt` para devolver o job, e volta; `run_forever` seguia, e um Ctrl+C só parava o executor se caísse entre dois jobs. Um teste mostrou o job interrompido, devolvido, pego de novo e concluído pelo mesmo processo. Sob o launcher, a parada chegaria ao prazo de 30 s e ao `TerminateProcess`, com o job em `running` e uma tentativa a menos: exatamente o que §7.1 existe para evitar. Uma interrupção no meio de um job agora encerra o laço |
| §5.3 | *"Uma busca que chega durante o aquecimento espera por ele"*, `TBM` | **Espera, e o tempo depende do disco e do outro processo.** Com o cache quente, 17,5 s do `ready` ao `search: ready`, e uma busca em português enviada no `ready` respondeu em 15,0 s, com resultados. Logo depois da primeira subida a frio, 104,9 s: a API e o `job_runner` importavam o `torch` ao mesmo tempo, do mesmo HDD frio, e cada um levou mais de 90 s. O `/health` não sofre: mediana de 4 a 7 ms durante o aquecimento, e o pior caso, meio segundo |
| §7.2 | O prazo de 30 s do `job_runner` | **Estourou uma vez, a frio, sem job na mão.** Na primeira parada da primeira medição, o `job_runner` ainda importava o `torch` do disco frio, junto com a thread de aquecimento da API, e o pedido de parada só é atendido quando a chamada em C em andamento volta: uma carga de DLL passou do prazo, e ele foi terminado sem ter tirado nenhum job da fila. Com o cache quente, o mesmo pedido no meio da importação foi atendido em 0,38 a 0,51 s. A linha do log que acompanha o `TerminateProcess` afirmava que um job ficava preso; agora diz "se havia um" |
| §7.2 | *"O arquivo de estado é apagado"* no fim da parada | **Fica, reescrito como `stopped`, com a porta da última subida pronta.** Apagado, a regra de §5.4 (a porta da última subida, se estiver livre) nunca valeria depois de uma parada ordenada, que é o caso comum. O que ele dizia do cluster sai: um cluster parado em ordem não tem dono a herdar |
| §8 | A segunda abertura *"lê o arquivo de estado, emite `already_running` com a URL e o estado"* | **Durante a subida da primeira, o arquivo ainda era o da parada anterior:** `already_running` com `"state": "stopped"` e sem PID, para um launcher a meio segundo da subida (medido). O launcher agora escreve `starting` e o próprio PID logo depois de pegar o lock, antes de ler a configuração, e conserva o que o anterior deixou sobre o cluster e a porta |
| §4.1 | *"Ctrl+C no terminal"* para o launcher | **Só se o processo processar Ctrl+C, e esse atributo é herdado do pai.** Um launcher iniciado por um pai que o deixou ligado nunca recebeu o Ctrl+C mandado ao seu console, e recebeu o Ctrl+Break (medido; o processo da medição herdou o atributo da automação que o iniciou). O launcher restaura o processamento de Ctrl+C para si mesmo ao subir; os filhos têm consoles próprios, que nenhum Ctrl+C do dele alcança |
| §7.5 | **Hipótese:** *"o launcher recebe o `WM_ENDSESSION` antes de o Windows terminar os filhos"* | **Não é garantido, e falhou num de dois logoffs** (2026-10-19). No segundo, a API, o `job_runner` e o postmaster já estavam mortos quando a parada começou, a menos de um tick do laço de supervisão antes dela; o log dizia "stopped in 0.0 s", e o job foi para o reaper, com uma tentativa a menos. O Windows encerra os processos de uma sessão do nível de encerramento mais alto para o mais baixo, e todo processo começa no mesmo, 0x280, onde a ordem não é definida. **O launcher agora pede 0x300** (`SetProcessShutdownParameters`, em `session_end.py`), e o nível não passa aos filhos (testado: um filho criado depois está em 0x280). Nos seis logoffs seguintes, o `WM_QUERYENDSESSION` e o `WM_ENDSESSION` chegaram ao launcher com os filhos vivos, e o `job_runner` começou a devolver o job no mesmo segundo |
| §7.5 | *"No `WM_ENDSESSION`, roda a parada de §7.2"* | **A parada de §7.2 cria processos, e durante um logoff nenhum processo nasce:** morre na inicialização das DLLs com `0xC000026B` (`STATUS_DLL_INIT_FAILED_LOGOFF`). O PostgreSQL no Windows atende cada conexão num processo novo, e o postmaster toma um processo do servidor que morre por um crash: termina todos os outros, tenta recomeçar, o processo de recuperação morre do mesmo jeito, e o servidor desliga (*"shutting down due to startup process failure"*). Em 2026-10-20, com o launcher já encerrado antes: **em dois de três logoffs a devolução do job derrubou o cluster**, o job foi para o reaper, e um mesmo job chegou à tentativa 2 de 3, um logoff antes de virar `failed`; no terceiro, a devolução foi feita, e o `pg_ctl stop` não nasceu (*"Neither a fast nor an immediate shutdown stopped the cluster"*, com o `pg_ctl.log` vazio). A devolução precisava de uma conexão nova porque o Ctrl+C que cai numa consulta custa uma: o psycopg 3.3 manda um pedido de cancelamento ao servidor, que é uma conexão, e o SQLAlchemy invalida a conexão que a interrupção atravessou. **Duas mudanças:** o `job_runner` adia o Ctrl+C que chega durante uma chamada a um repositório até a chamada voltar, e o levanta ali, com a conexão inteira e livre para a devolução (`app/infrastructure/process/interrupts.py`; fora do banco, a interrupção continua imediata); e, no fim da sessão, o launcher para o cluster pelo pipe de sinais do postmaster, `\\.\pipe\pgsignal_<pid>`, como o `pg_ctl kill` faz por dentro, com `fast` e depois `immediate` (`stop_by_signal()`, em `cluster.py`, e `postmaster_signal.py`). Em 2026-10-21, três de três: o job devolvido nos dois logoffs que tinham um, `attempts` 0, e o cluster parado em *fast* em 0,3 a 0,5 s, sem recuperação na subida seguinte. Os dois têm teste: o sinal de verdade chegando no meio de uma gravação, com um controle sem o adiamento, que corta a gravação; e a parada pelo pipe contra um cluster de verdade, com um adaptador que falha se o `pg_ctl` for chamado, seguida de uma subida com `recovered: False` |
| §7.3, §7.5 | *"Um cluster que um desenvolvedor subiu com `embedded start` é adotado e deixado de pé no fim"* | **No fim da sessão, é parado também.** O Windows o terminaria logo depois, e a subida seguinte faria uma recuperação de crash. No primeiro logoff de 2026-10-19 o cluster era adotado, e a parada não passou por ele (o log não tem a linha `Database:`). A posse não muda: uma parada que falha não deixa o cluster como `started` no arquivo de estado |

### 17.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §5.3 | **O provider do modelo ganhou um lock, e perdeu o `lru_cache`.** Com as importações dentro de `get_embedding_model()`, a primeira chamada passa a importar o `torch` enquanto a API já responde, e o `lru_cache` executa a função de novo para uma segunda chamada que chega antes de a primeira voltar: a thread de aquecimento e a primeira busca construiriam dois adaptadores, e carregariam o checkpoint duas vezes. O lock dentro dos adaptadores, que o texto pede, não cobre isso, porque seriam dois adaptadores |
| §7.3 | **O PID do postmaster vai para o arquivo de estado**, e a herança vale para o mesmo servidor. Sem isso, um launcher que adotasse um cluster que o desenvolvedor subiu com `embedded start` depois da morte do launcher anterior herdaria a posse pela palavra do arquivo, e pararia o cluster de outra pessoa |
| §4.3, §7.1 | **O launcher trata o próprio Ctrl+C, o Ctrl+Break e o SIGINT do canal do stdin com um handler que conta pedidos**, e nunca vê um `KeyboardInterrupt`. O pedido é atendido no próximo ponto seguro: um `initdb`, uma recuperação ou uma migration não são cortados ao meio, e a parada começa quando a chamada volta |
| §4.4 | **Os eventos, nos detalhes que o texto deixou abertos.** `starting` sai depois de lida a configuração, para dizer `database` (§9); numa configuração recusada, sai `starting` e logo `failed`. `child` tem os estados `restarting` (com `exit_code` e `delay`), `running` e `unavailable`, e os nomes `api`, `job_runner` e `database`. `stopping` diz o motivo (`requested`, `session_end` ou `failure`, com `detail`), e `stopped`, o código de saída. `phase` de `api` e de `job_runner`, só na primeira criação de cada um. Depois de `failed`, a limpeza é silenciosa. O protocolo é o 1 |
| §6.1 | **A linha da recusa viaja na exceção.** `refuse_to_start()`, em `app/infrastructure/process/supervision.py`, escreve a linha no stderr e levanta uma subclasse de `SystemExit` com o código 78 e a linha. O launcher importa o próprio `Settings` dentro de um `try`, e põe a linha no `failed`. A CLI recusa em `app/cli/__main__.py`, e não em `search.py`, onde a lista de §15 a punha |
| §5.5 | **O `python.exe` de um virtualenv é um redirecionador:** o PID que o launcher conhece de cada filho é o dele, e o interpretador de verdade é um filho seu, num job object com `KILL_ON_JOB_CLOSE`. `TerminateProcess` no redirecionador termina os dois (observado), e a lista de processos da medição confere os dois |
| §4.4 | **O `stdout` do launcher é só dos eventos.** O fluxo real fica guardado para eles e `sys.stdout` passa a apontar para o stderr, para que um `print` perdido, do código ou de uma biblioteca, não quebre uma linha do protocolo. Os dois em UTF-8, porque as linhas dos filhos passam pelo stderr |
| §7.1 | `stop_on_stdin_eof()` é chamada dentro do `try` que trata o `KeyboardInterrupt`: com o stdin já no fim, o sinal chega antes de a função voltar (medido, com um traceback). E, depois que o programa começa a sair, o SIGINT é ignorado: um pedido tardio caía no `logging.shutdown` e saía como `0xC000013A` |
| §5.2 | O 503 do `/health` também traz `search` e `search_detail` |
| §9 | O `stage_installation.py` ensina `python -m app.launcher` onde ensinava dois comandos |
| §5.1 | O prazo do pronto ficou em 120 s, uma escolha folgada: a API mediu de 3,2 a 3,6 s da criação ao primeiro 200, quente e fria |
| §7.2, §7.5 | **O log da parada diz o código de saída de cada filho, e diz quando um filho já estava morto antes de a parada pedir** (*"exited with 3221225786 (0xC000013A) before the stop asked it to"*), em vez de *"stopped in 0.0 s"*. Um NTSTATUS sai também em hexadecimal. A janela registra o `WM_QUERYENDSESSION`. Sem isso, o segundo logoff de 2026-10-19 só se explicou por dedução, e não se sabia se uma devolução que falhou tinha saído com erro |
| §7.5 | **A parada pelo pipe espera os processos que listou antes do sinal.** Um processo saindo pode já não dizer qual é o seu executável, e a lista de processos do servidor, feita pelo executável, o contava como encerrado antes da hora (medido no teste). A espera é pelos objetos de processo, um a um, e cada uma das três esperas (o lock, o *fast*, o *immediate*) tem 6 s, para a parada inteira caber nos 30 s que a janela espera, junto com os 10 s do `job_runner` |
| §16 | Dois resultados da primeira medição completa eram da própria medição, e foram refeitos: a injeção do Ctrl+C (o atributo herdado de §17.1), e o "executor novo no laço em 0,2 s", que contava as últimas linhas do filho morto, repetidas no log do launcher. E a segunda abertura contou dois `conhost.exe` cujo PID de pai registrado coincidia com o PID reciclado dela, o efeito que o RFC-033 §18 já tinha medido; nove aberturas conferidas não deixaram processo nenhum |

### 17.3 O que ficou sem medição

Dito aqui para não parecer medido em outro lugar.

- ~~**O logoff** (§7.5).~~ *Feito depois, em oito logoffs (§16, §17.1). O que ele deixou sem resposta:*
  - **A saída lenta no logoff.** Com o launcher encerrado antes, o `job_runner` levou de 2,7 a 4,8 s para sair depois de devolver o job no mesmo segundo, e a API, 3 s ou mais, nos seis logoffs. No primeiro logoff de 2026-10-19, ainda no nível de encerramento padrão, os dois saíram em 0,3 s. A diferença é uma pista, e não uma explicação. Hoje não custa nada: o `job_runner` cabe nos 10 s, e a API terminada no logoff não tem requisição a perder.
  - **Só o "Sair".** Reiniciar e desligar passam pelo mesmo `WM_ENDSESSION`, e a troca rápida de usuário não encerra a sessão; nenhum dos três foi feito.
  - **Só um launcher iniciado de um terminal**, e não por uma janela com `--supervised` (§4.1), e só na outra máquina, com NVMe.
  - **A leitura do `SetConsoleCtrlHandler`.** Com o launcher encerrado antes, a parada chega aos filhos antes de o Windows chegar a eles, e o que o Windows faria com eles não aparece mais. O segundo logoff de 2026-10-19, o único em que os filhos morreram antes, foi antes de o log dizer o código de saída.
  - **A CLI no logoff.** O `app.cli index` executa o job no próprio processo (RFC-029 §12), que nenhum launcher supervisiona: no logoff, o job dele vai para o reaper, e um processo do servidor que o atende, terminado pelo Windows, derruba o cluster (`0x40010004`, no primeiro logoff de 2026-10-19). Está declarado, e não foi mudado.
- **Uma janela de verdade.** O contrato de §4.4 foi exercido por um programa que faz o que uma janela faria, e não por um Electron ou um Tauri: isso é do frontend.
- **Um `C:\Program Files` de verdade**, pelo motivo do RFC-034 §17.3, e **um antivírus**: os dois tempos de aquecimento muito acima do esperado logo depois de uma instalação nova (49,6 s e 104,9 s) podem ter um componente de varredura dos arquivos recém-copiados, que esta medição não separou da disputa de disco.
- **Um SSD.** Todos os números são do HDD do piloto, onde a importação do `torch` domina tudo o que vem depois do `ready`.
