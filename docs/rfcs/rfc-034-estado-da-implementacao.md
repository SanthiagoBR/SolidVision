# RFC-034 — Estado da implementação

**Para quem é:** a sessão que vai terminar o [RFC-034](rfc-034-configuracao-e-diretorios-do-usuario.md). Este documento diz o que já está no código, o que falta, e o que foi decidido no caminho que o RFC não decidia. Ele não substitui o RFC: as seções citadas (§) são as dele.

**Sessão 1:** 2026-10-13, na máquina do piloto (Windows 10 Home, HDD, Python 3.12.9).
**Head de partida:** `03445f7` em `develop`.
**Nada foi commitado.** Tudo o que este documento descreve está na árvore de trabalho.

> **Sessão 2: 2026-10-14, na mesma máquina. O RFC está terminado.** Tudo o que §4 abaixo listava como faltando foi feito, e o que a medição achou está no próprio RFC: os números em §16, e o que mudou o código em §17. Este documento ficou como o registro da passagem entre as duas sessões; as seções abaixo descrevem o estado do fim da sessão 1, e as notas como esta dizem o que a sessão 2 fez com cada ponto.
>
> | o que faltava (§4) | feito |
> | --- | --- |
> | `scripts/stage_installation.py` | Com `--read-only`, `--no-compile`, `--unlock` e `--remove`, e `backend/tests/test_stage_installation.py` |
> | Os testes `embedded` | Os três de §10, mais um para §7.5 contra um servidor de verdade, em `test_embedded_integration.py` |
> | `measure_user_configuration.py` | As linhas de §16, menos as duas manuais, que foram feitas à mão. O log está ao lado, fora do git |
> | Os documentos | O RFC (cabeçalho, as notas, §16 e §17), `backend/README.md`, `docs/rfcs/README.md` e `ROADMAP-standalone.md` |
>
> **O que a sessão 2 mudou no código da sessão 1**, cada coisa pela medição ou por uma decisão que §5 abaixo pedia:
>
> - **As variáveis `PG*` saem do ambiente do processo antes de toda conexão com o cluster** (`discard_libpq_environment()`, em `database_setup.py`). Os três parâmetros explícitos não bastavam: 14 das 42 variáveis da libpq ainda derrubavam o login do adaptador, e 15 a engine (RFC §7.5, §17.1).
> - **`ROLLOVER_RETRY_SECONDS = 1.0`**, medido, e a palavra "Provisional" saiu (§3.7 abaixo; RFC §8.3).
> - **O valor vazio de um caminho é recusado** (a lacuna de §5 abaixo; RFC §6.3).
>
> **As duas verificações manuais foram feitas à mão depois da medição**, e as duas hipóteses se confirmaram (RFC §16): o Bloco de Notas grava `solidvision.env.txt` (§3.10 abaixo), e o que uma primeira execução elevada cria é usado depois sem elevação.

---

## 1. Resumo

O RFC foi dividido em duas partes. A parte 1 está feita: o código de §4 a §8 e os testes de §10 que rodam na suíte padrão. A parte 2 é a validação: a instalação simulada (§9), a medição (§16) e os documentos.

| | estado |
| --- | --- |
| §4 Layout e modo | **Feito** |
| §5 Fontes de configuração, avisos, recusa, relatório | **Feito** |
| §6 Diretórios do usuário, caminhos absolutos | **Feito** |
| §7 Banco: `database_source`, `locate()`, `build_engine()`, `run --` | **Feito** |
| §8 Log: lugar, um arquivo por programa, rotação | **Feito**, com um número provisório (§3.7 abaixo) |
| §9 Instalação simulada (`scripts/stage_installation.py`) | **Falta** |
| §10 Testes da suíte padrão | **Feito** |
| §10 Testes sob o marcador `embedded` | **Falta** (os que já existiam foram atualizados) |
| §16 Medição (`measure_user_configuration.py`) e os `TBM` do RFC | **Falta**, menos a primeira linha |
| `backend/README.md`, status no `README.md` e no `ROADMAP-standalone.md` | **Falta** |

**Os números desta sessão:**

| verificação | resultado |
| --- | --- |
| Suíte no head de partida (`pytest` puro, Docker de pé), antes de qualquer mudança | 2254 passed, 79 deselected, 258 s |
| Suíte depois da parte 1 (`pytest` puro, Docker de pé) | 2460 passed, 79 deselected, 101 s |
| Suíte pelo caminho novo (`run -- python -m pytest`, cluster criado na hora em diretório temporário) | 2460 passed, 79 deselected, 86 s |
| Testes `embedded` que já existiam (`pytest -m embedded`) | 21 passed, 2518 deselected, 250 s |
| `black --check`, `ruff check --no-fix`, `mypy` | Limpos |

A primeira linha é a que §16 pede *antes de qualquer mudança*. Ela já pode ser copiada para a tabela de §16 do RFC. As outras são conferências desta sessão, e não a medição de §16: não têm a prova do alvo nem a comparação de `xact_commit` que §16 pede.

A diferença entre as duas primeiras linhas é de 206 testes: 208 entraram e 2 saíram. Os dois que saíram estavam em `test_embedded_database_setup.py` e conferiam `ClusterConnection.environment()` e `Settings.database_url`, que deixaram de existir. Os dois foram reescritos em `test_embedded_state_machine.py`, contra `EmbeddedPostgres.environment()`. A contagem de `deselected` não mudou.

Os 258 s da primeira linha e os 101 s da segunda não são comparáveis. A primeira foi a primeira execução do dia, com o disco frio.

Entre os 21 testes `embedded` está o que exercita o mecanismo de §7 contra um servidor de verdade: `test_run_points_the_projects_own_settings_at_the_cluster_and_stops_it`. Um filho de `run --`, com o `settings` e a engine do projeto, alcança o cluster. A porta que a engine alcançou é a que o `locate()` leu, a URL da engine não tem porta nem senha, e a senha não está no ambiente do filho.

A suíte pelo caminho novo rodou com o Docker de pé ao mesmo tempo, e não prova sozinha contra qual banco rodou. O que sustenta que foi o embarcado é o teste do parágrafo anterior e o fato de a fonte `embedded` não ter fallback. A prova de §16 continua devida.

## 2. O que está no código

### 2.1 Arquivos novos

| arquivo | o que é |
| --- | --- |
| `backend/app/infrastructure/config/layout.py` | `Environment`, `Layout`, `layout_at()`, `local_app_data()`, `detect_layout()` e o singleton `layout` (§4, §6.2) |
| `backend/app/infrastructure/logging/program.py` | `program_name()`: o nome do programa, para o arquivo de log (§8.2) |
| `backend/tests/infrastructure/config/` | `conftest.py` e quatro arquivos: `test_layout.py`, `test_settings_sources.py`, `test_settings_paths.py`, `test_configuration_file.py` |
| `backend/tests/infrastructure/logging/` | `test_program_name.py`, `test_lossless_rotation.py`, `test_logger_configuration.py` |
| `backend/tests/infrastructure/persistence/test_engine_sources.py` | A engine nas duas fontes, e o `/health` com o cluster ausente |
| `backend/tests/test_path_anchors_architecture.py` | Nenhuma âncora fora do `layout.py` |

### 2.2 Arquivos modificados

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/config/settings.py` | `load_settings(layout)`, as fontes por modo, `absolute_path()`, `DatabaseSource`, `ConfigurationError`, `ConfigurationReport`. `log_directory` é `Path`, `log_filename` é opcional, a senha saiu do `repr`. Saíram `indexing_root_path`, o campo `environment` (virou propriedade), `database_url` e os três defaults com `os.environ` |
| `backend/app/infrastructure/logging/logger.py` | Saiu a âncora própria. Usa `settings.log_directory` como está, o nome por programa, e escreve o relatório de §5.6 na primeira configuração |
| `backend/app/infrastructure/logging/handlers.py` | `LosslessRotatingFileHandler` (§8.3) |
| `backend/app/infrastructure/persistence/engine.py` | `build_engine()`, o listener `do_connect`, os parâmetros da libpq e o `connect_timeout` |
| `backend/alembic/env.py` | Usa `build_engine()`, online e offline |
| `backend/app/infrastructure/database/embedded/cluster.py` | `locate()`, `environment()`, `ENVIRONMENT_VARIABLES`, `EMBEDDED_SOURCE` |
| `…/embedded/database_setup.py` | `ALEMBIC_INI` e `ALEMBIC_SCRIPTS` saem do `layout`. Saíram `ENVIRONMENT_VARIABLES` e `ClusterConnection.environment()` |
| `…/embedded/errors.py` | `ClusterNotRunningError` |
| `…/embedded/__main__.py` | O `run --` exporta `cluster.environment()` |
| `…/embedded/__init__.py`, `cluster_home.py` | Só os docstrings |
| `backend/app/infrastructure/workers/indexing_worker.py` | Só o texto de ajuda do `--root`, que citava o setting removido |
| `backend/dataset_tools/seed_demo.py` | O docstring e o texto de ajuda (§6.4) |
| `backend/tests/infrastructure/database/embedded/` | `test_embedded_state_machine.py` ganhou os testes de `locate()` e de `environment()`. `test_embedded_cli.py`, `test_embedded_database_setup.py` e a sonda de `test_embedded_integration.py` foram atualizados para as três variáveis novas |
| `backend/tests/conftest.py`, `test_embedded_cluster_home.py` | Só os docstrings que citavam `database_url` |
| `experiments/rfc-033-embedded-postgres/measure_adapter.py` | A sonda compara a porta que a engine alcançou com a do `locate()`, e não mais com `settings.database_port` |
| `.env.example` | Saíram `ENVIRONMENT`, `LOG_DIRECTORY=logs` e `LOG_FILENAME`. Entrou `DATABASE_SOURCE`, comentado, e um cabeçalho com as regras do arquivo |
| `.gitignore` | `/logs/` |

Não mudaram, como o RFC previa: `session.py`, os repositórios, as rotas, o `job_runner`, o `docker-compose.yml`, e nenhum arquivo do Domain ou da Application.

### 2.3 Onde cada teste de §10 está

| o que §10 pede | arquivo |
| --- | --- |
| Todo campo `Path` é absoluto, nos dois modos, nas três situações | `test_settings_paths.py` |
| Todo campo terminado em `_directory`, `_path` ou `_file` é `Path` | `test_settings_paths.py` |
| Dois subprocessos em diretórios de trabalho diferentes | `test_layout.py` |
| Nenhuma âncora fora do `layout.py` | `test_path_anchors_architecture.py` |
| O modo, com e sem `pyproject.toml` | `test_layout.py` |
| As fontes | `test_settings_sources.py` |
| O arquivo: BOM, UTF-16, ANSI, aspas, `%NOME%`, `D:thumbs`, `\thumbs`, relativo | `test_configuration_file.py`, e as regras uma a uma em `test_settings_paths.py` |
| As chaves desconhecida, aposentada e sem efeito | `test_settings_sources.py` |
| Toda chave do `.env.example` é um campo | `test_settings_sources.py` |
| A raiz de dados sem `LOCALAPPDATA` | `test_layout.py` |
| `locate()`, cada estado do `status()` | `test_embedded_state_machine.py` |
| A engine, o listener, o `OperationalError`, o `/health` em 503 | `test_engine_sources.py` |
| A senha fora do `repr` e do relatório | `test_settings_sources.py` |
| O handler de rotação, com um segundo processo de verdade | `test_lossless_rotation.py` |
| O nome do programa, cada linha da tabela de §8.2 | `test_program_name.py` |

## 3. Decisões tomadas que o RFC não tomava

O RFC diz o que o código faz. Estas são as escolhas de *como*, feitas nesta sessão. A sessão 2 pode revê-las, mas não precisa redescobri-las.

### 3.1 Como o `Settings` conhece o layout

O `Settings` tem um atributo de classe, `_layout`. Na classe base ele é o layout detectado, e por isso `Settings()` sem argumentos continua significando "recém-carregado para o código que está rodando". Os testes que já comparavam o singleton com `Settings()` não mudaram.

Para qualquer outro layout, `load_settings(layout)` cria uma subclasse com o `_layout` e o `env_file` daquele layout. A subclasse é guardada por layout (`functools.cache`), para que duas cargas do mesmo layout sejam comparáveis.

### 3.2 Os defaults dos quatro caminhos

Vêm de um lugar só, `PATH_DEFAULTS`, e chegam por dois caminhos:

- uma fonte do pydantic-settings, `_LayoutDefaults`, a mais fraca de todas, que dá os defaults do layout que está sendo carregado;
- um `default_factory` em cada campo, que dá os do layout detectado.

O segundo existe por causa do `mypy`: um campo sem default vira argumento obrigatório de `Settings()`, e os testes que chamam `Settings()` deixavam de passar na checagem de tipos. Em execução, a fonte sempre responde antes.

### 3.3 Instalado, as chaves do banco são descartadas antes de validar

`DATABASE_SOURCE` e as cinco `DATABASE_*` do `solidvision.env` não chegam ao `Settings`: a fonte do arquivo passa por um filtro (`_Without`). Um `DATABASE_PORT=abc` ali produz o aviso de "sem efeito" e **não** recusa a subida. O RFC não dizia qual dos dois acontecia. A escolha foi a que faz "sem efeito" ser literal.

### 3.4 Como a recusa de §5.5 chega ao processo

`load_settings()` levanta `ConfigurationError`, de uma linha: `<origem>: <CHAVE>: <problema>`. A origem é o caminho do arquivo, `environment variable` (só em desenvolvimento) ou `constructor argument`.

O singleton do módulo converte isso em `SystemExit` (`_load_or_exit`). O processo escreve `SolidVision cannot start: …` no `stderr` e sai com código 1, sem traceback. Um teste roda um processo de verdade e confere as três coisas.

A mensagem nunca repete o valor, porque ele pode ser uma senha.

### 3.5 O nome do programa vem de `sys.orig_argv`

O RFC dizia "do módulo passado ao `-m` ou do executável". Enquanto os pacotes de um `python -m pacote.modulo` ainda estão importando, `sys.argv[0]` é o texto `-m` e `__main__.__spec__` ainda não existe. É nesse momento que o logger é configurado. `sys.orig_argv` já tem a linha de comando inteira. Um teste roda um `python -m` de verdade e confere.

Para `python caminho\script.py`, o nome é o do script (`stage_installation.log`). A tabela de §8.2 só listava `-m`, `pytest`, `-c` e o REPL.

### 3.6 A rotação usa um nome intermediário

O arquivo corrente é renomeado para `<arquivo>.rotating`, os backups são deslocados, e só então `.rotating` vira `.1`. Se o processo morrer entre os dois passos, a rotação seguinte encontra o `.rotating` e o termina antes de começar a dela. Há um teste para isso.

### 3.7 O intervalo da nova tentativa é provisório

`ROLLOVER_RETRY_SECONDS = 5.0`, em `handlers.py`. §8.3 deixa o número como `TBM`. **A sessão 2 decide o valor pela medição e tira a palavra "Provisional" do docstring.**

### 3.8 `locate()` e os erros que não são "cluster parado"

- Cluster `ready` sem o arquivo `password`: `ClusterRefusedError`, a mesma exceção e o mesmo texto do `start()`.
- Runtime sem manifesto: `RuntimeInvalidError`, como no `status()`.

O listener da engine converte **qualquer** `EmbeddedPostgresError` em `psycopg.OperationalError`, e não só o `ClusterNotRunningError`. Assim o `/health` responde 503 também quando o runtime não existe.

O adaptador é construído na primeira conexão, e não quando a engine é construída. Importar `engine.py` não toca no disco.

### 3.9 `ENVIRONMENT_VARIABLES` mudou de dono

Os três nomes não são atributos da conexão, e sim do cluster. A constante foi para `cluster.py`, e quem monta o dicionário é `EmbeddedPostgres.environment()`. `ClusterConnection.environment()` saiu. O adaptador não lê `settings`, então o valor `embedded` está escrito lá (`EMBEDDED_SOURCE`), e um teste o confere contra `DatabaseSource`.

### 3.10 O aviso do `.txt` já existe

§5.3 condiciona o aviso de `solidvision.env.txt` à confirmação da hipótese do Bloco de Notas. O aviso foi implementado assim mesmo: ele só aparece quando o `.txt` existe e o arquivo certo não, e é inofensivo se a hipótese for falsa. **A hipótese continua por confirmar à mão (§16).**

### 3.11 O teste de arquitetura proíbe um pouco mais

Além de `__file__`, `Path.cwd()`, `os.getcwd()` e `Path.home()`, ele proíbe `expanduser` e a constante `"LOCALAPPDATA"`. É o que §3 do RFC afirma em prosa. Docstrings e comentários não contam.

### 3.12 O relatório de §5.6 vai para o console também

Ele é escrito pelo logger raiz do app, que tem os dois handlers do RFC-005. Cada processo passa a escrever 8 linhas no `stderr` ao subir, mais uma por aviso.

## 4. O que falta, na ordem em que eu faria

### 4.1 `scripts/stage_installation.py` (§9)

Copia `backend\app`, `backend\alembic`, `alembic.ini` e `runtime\postgres` para fora do repositório, **sem** `pyproject.toml`, `.env` e testes. Compila os `.pyc`. Com `--read-only`, tranca a árvore por `icacls` e pelo SID. `IcaclsDirectoryAcl` e `current_user_sid()` de `cluster_home.py` são o precedente.

Cuidado de §2.1: `tempfile.mkdtemp` apontado para um diretório só para leitura não falha, fica parado. Toda verificação tem prazo.

### 4.2 Os testes novos sob o marcador `embedded` (§10)

- `locate()` contra um cluster de verdade.
- A engine depois de um reinício em outra porta.
- Os clusters de desenvolvimento e de instalação de pé ao mesmo tempo.

As fixtures `runtime`, `template`, `home_root` e `cluster` de `test_embedded_integration.py` servem aos três.

### 4.3 `experiments/rfc-034-user-configuration/measure_user_configuration.py` (§16)

As 28 linhas da tabela de §16. Estado de cada uma:

| linha de §16 | estado |
| --- | --- |
| A suíte no head de partida | **Medida:** 2254 passed, 79 deselected |
| Primeira execução instalada; "grava tudo"; a miniatura; a raiz de verdade | Faltam. Dependem de 4.1 |
| `/health` instalado; com o cluster parado; com a fonte `external` e o banco parado | Faltam |
| Reinício em outra porta | Falta. É a hipótese de §7.3 |
| O custo do `locate()`; conexão nova com e sem o listener | Faltam. Critério: menos de 10 ms na mediana. Acima disso entra o segundo ramo, guardar a resposta |
| A suíte pelo caminho novo | Rodada nesta sessão sem a prova do alvo (§1). Falta a medição: porta da engine igual à linha 4 do `postmaster.pid`, e `xact_commit` dos dois lados |
| Docker continua funcionando | Rodada nesta sessão (§1). Falta só registrar na medição |
| Ambiente hostil, instalado | Coberto por testes de unidade. Falta de ponta a ponta |
| As variáveis `PG*` | Falta. O que a medição achar entra em `LIBPQ_OVERRIDES` |
| O arquivo do usuário | Coberto por testes de unidade. Falta de ponta a ponta |
| `solidvision.env.txt` | **Manual**, com o Bloco de Notas |
| Sem `LOCALAPPDATA`; dois diretórios de trabalho | Cobertos por testes com subprocessos. Falta registrar |
| Os `.pyc` | Falta. Depende de 4.1 |
| Log: dois programas; o mesmo programa duas vezes; um leitor | Faltam. O cenário do piloto está em `pilot_configuration_probe.py` |
| Os dois modos lado a lado | Falta |
| Primeira execução elevada | **Manual**, com um PowerShell de administrador |
| A senha não vaza | Coberto em parte por testes. Falta procurar nos logs e no ambiente do filho de `run --` |
| Arquitetura; estilo e tipos | Passam hoje. Falta registrar |

Se a medição for feita neste HDD, §16 manda medir de novo a linha de base do adaptador antes de qualquer comparação com os números do RFC-033, que são de um NVMe.

### 4.4 Os documentos

- Preencher os `TBM` do RFC-034, e o cabeçalho (`Status`, `Medição`).
- `backend/README.md`: os dois modos, a precedência, a árvore e §6.5.
- `docs/rfcs/README.md` e `ROADMAP-standalone.md`: o status. Os dois já tinham mudanças não commitadas antes desta sessão, que não foram tocadas.

## 5. O que a sessão 2 precisa saber antes de começar

**O Docker precisa estar de pé para o `pytest` puro.** O contêiner `solidvision-postgres` estava parado no começo desta sessão, foi iniciado para as medições e foi parado de novo no fim (`docker start solidvision-postgres` o traz de volta). Com ele parado, a suíte padrão falha nos testes que usam o banco.

**`%LOCALAPPDATA%\SolidVision-dev` passou a existir nesta máquina.** Foi criado pela suíte: o log do `pytest` agora vai para `SolidVision-dev\logs\pytest.log`. É o comportamento de §6.5. O `.env` desta máquina não tem `LOG_DIRECTORY`.

**`%LOCALAPPDATA%\SolidVision` não foi tocado.** O cluster, as miniaturas e o log antigos que §2.5 descreve continuam lá. Movê-los ou apagá-los é decisão do desenvolvedor (§6.5).

**O `.env` desta máquina produz cinco avisos em todo processo**, como §5.4 previa: `MODEL_NAME`, `MODEL_VERSION`, `COLLECTION_ROOT`, `THUMBNAIL_SIZE` e `EMBEDDING_BATCH_SIZE`. Não foi alterado.

**A pasta `logs\` na raiz do repositório** é de antes desta sessão. Agora está no `.gitignore`. Nada mais escreve nela, a não ser que o `.env` ganhe `LOG_DIRECTORY=logs`.

**Uma lacuna que o RFC não cobre.** Um valor vazio (`THUMBNAIL_DIRECTORY=`) vira o caminho `.`, é tratado como relativo e ancora na pasta do arquivo de configuração. Instalado, as miniaturas iriam direto para `%LOCALAPPDATA%\SolidVision`. As cinco regras de §6.3 não falam de valor vazio, e nada foi acrescentado. Vale decidir: recusar, ou tratar vazio como não escrito.

**`alembic/env.py` agora importa `engine.py`.** Isso constrói o `EngineInstance` do processo, que não abre conexão nenhuma até ser usado. Quando o adaptador roda as migrations numa conexão que ele mesmo passou, essa engine fica sem uso.

## 6. Como conferir o ponto de partida da sessão 2

Da raiz do repositório, com o virtualenv de `backend\.venv`:

```powershell
# a suíte padrão, contra o Docker
python -m pytest -q

# a suíte pelo caminho novo, sem tocar no perfil de verdade
$env:EMBEDDED_POSTGRES_DIRECTORY = "<um diretório temporário>\postgres"
$env:PYTHONPATH = "backend"
python -m app.infrastructure.database.embedded run -- python -m pytest -q

# os testes que sobem servidores de verdade
python -m pytest -q -m embedded

# estilo e tipos
python -m black --check backend/app backend/tests backend/dataset_tools
python -m ruff check --no-fix backend/app backend/tests backend/dataset_tools
python -m mypy
```

O `ruff` do projeto tem `fix = true`: sem `--no-fix` ele reescreve os arquivos.
