# RFC-034 — Configuração e Diretórios do Usuário

**Status:** Implementado. As duas verificações manuais de §16 foram feitas à mão depois da medição, e as duas hipóteses se confirmaram: o Bloco de Notas grava `solidvision.env.txt` (§5.3), e o que uma primeira execução elevada cria é usado depois sem elevação (§13). A medição mudou o código em dois pontos (§17.1): as variáveis `PG*` saem do ambiente antes de toda conexão com o cluster, e o intervalo da rotação ficou em 1 s
**Etapa:** 2 do [roadmap sem Docker](ROADMAP-standalone.md#rfc-034--configuração-e-diretórios-do-usuário-)
**Depende de:** RFC-033 (o adaptador devolve a conexão; o [§16](rfc-033-postgres-embarcado.md#16-o-que-passa-adiante) dele é a lista de entrada deste), RFC-004 (`Settings`, ambiente acima do `.env`), RFC-005 (logging), RFC-008 (`/health` devolve `environment`), RFC-029 (a indexação é por `device_id` e escopos), RFC-030 §7.1 (`%LOCALAPPDATA%\SolidVision`)
**Bloqueia:** RFC-035 (o modo instalado é o que liga o `HF_HUB_OFFLINE`, e a raiz da instalação é de onde os pesos são achados), RFC-036 (o launcher sobe processos que se configuram sozinhos)
**Migration:** não
**Medição:** `experiments/rfc-034-user-configuration/measure_user_configuration.py`, rodado em 2026-10-14 na máquina do piloto, com o log ao lado (§16). O piloto que precedeu este documento é `pilot_configuration_probe.py`, na mesma pasta, rodado em 2026-10-13 (§2)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os números medidos que este documento cita vêm do piloto de 2026-10-13, e cada um diz de onde veio. O piloto lê como o código **já** se comporta. Ele não mede nada do que este RFC propõe. Onde o texto afirma como o Windows, o pydantic-settings, o SQLAlchemy ou a biblioteca padrão se comportam sem ter medido, a frase diz que é hipótese, e §16 diz como ela vai ser testada.
>
> **A máquina do piloto** é a do RFC-033a: Windows 10 Home (10.0.19045), HDD SATA `ST1000DM010-2EP102`, Python 3.12.9, pydantic-settings 2.14.2, SQLAlchemy 2.0.51, página de código 1252, processo sem elevação. Não é a máquina em que o RFC-033 foi medido, que tem um NVMe.
>
> **Depois da implementação.** Os `TBM` foram medidos na mesma máquina, e cada resultado está numa nota ao lado do texto que o pedia, e em §16. O texto original ficou como estava. O que a medição contrariou, o que a implementação acrescentou e o que ficou sem medir estão em §17.

---

## 1. Contexto

O RFC-033 entregou o banco sem Docker. Um adaptador cria o cluster na primeira execução, sobe, adota, recupera e para, e guarda tudo em `%LOCALAPPDATA%\SolidVision\postgres\16\`. O que ele não entregou, de propósito, é quem usa esse banco sem ajuda. Para chegar ao cluster hoje é preciso passar por `run -- <comando>`, que exporta cinco variáveis `DATABASE_*` para o comando ([RFC-033 §11](rfc-033-postgres-embarcado.md#11-como-o-resto-do-sistema-encontra-o-banco-até-o-rfc-034)).

O resto do backend continua presumindo um checkout do repositório. O `Settings` procura o `.env` na raiz do repositório. O log vai para uma pasta ao lado do código. O modo de execução é um texto que alguém escreve no `.env`. As credenciais do banco têm um valor fixo no código.

Este RFC faz o backend funcionar sem checkout. Ele decide de onde a configuração vem em cada modo, onde ficam os diretórios do usuário, como um processo descobre a porta e a senha do cluster, e como tudo isso é testado numa máquina que só tem um checkout. Não decide quem sobe os processos (RFC-036), de onde vêm os modelos (RFC-035), nem como o código chega à máquina (o instalador, fora do roadmap).

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| Separar duas origens de configuração, com precedência documentada | §5 |
| Tornar `log_directory` absoluto e por usuário | §6.3, §8.1 |
| Remover o default relativo de `indexing_root_path` | §6.4. O campo sai inteiro (§2.2) |
| Fazer as credenciais do banco virem do RFC-033 | §7 |
| Ler `environment` de um único lugar | §4.2 |
| *Critério:* o backend sobe de um diretório qualquer, sem `.env`, e grava tudo em `%LOCALAPPDATA%\SolidVision` | §9, §16 |
| *Critério:* nenhum caminho relativo ao diretório de trabalho sobra no `Settings`, e um teste verifica isso | §6.3, §10 |

## 2. Problema

Antes de escrever este documento, um script leu o comportamento atual: `pilot_configuration_probe.py`. Ele não conecta em banco nenhum, só escreve em diretórios temporários e não imprime os valores do `.env`. O log dele fica ao lado do script e fora do git, como o de todo experimento: `pilot_configuration_probe.log` é a execução que este documento cita, e `pilot_configuration_probe.rehearsal.log` é o ensaio de onze minutos antes. Três observações desta seção foram lidas à mão e não estão nesse log: o conteúdo de `%LOCALAPPDATA%\SolidVision`, a saída do `git status` e a versão da libpq. Cada uma diz isso onde aparece.

### 2.1 O backend só existe dentro de um checkout

Quatro lugares do código acham o que precisam subindo quatro diretórios a partir do próprio arquivo (`Path(__file__).resolve().parents[4]`):

| onde | o que ancora | num app instalado |
| --- | --- | --- |
| `settings.py:19` | O `.env` | Não existe. O `Settings` cai inteiro nos defaults |
| `settings.py:422` | `runtime\postgres` | Funciona, se o instalador puser o runtime ao lado do código |
| `logger.py:22` | A pasta `logs` | Fica debaixo do diretório de instalação |
| `database_setup.py:50` | `alembic.ini` e `alembic\` | Funciona, se o instalador os levar |

A terceira linha derruba o processo. O `get_logger()` é chamado no topo de quase todo módulo, e a primeira chamada cria a pasta de log (`handlers.py:23`). Debaixo de `C:\Program Files`, um processo comum não cria pasta: o piloto tentou e recebeu `PermissionError: Acesso negado`. Um backend instalado ali morreria na importação.

O piloto achou um segundo jeito de falhar no mesmo lugar. O `tempfile.mkdtemp`, apontado para `C:\Program Files`, **não dá erro**: em 10 s não criou o diretório nem levantou exceção. A primeira versão da sonda usava essa função e não voltou; foi encerrada à mão depois de mais de cinco minutos. A explicação provável é que a função trata o acesso negado como colisão de nome e tenta outro nome, porque a checagem de escrita que ela faz não lê a ACL. Para este RFC a consequência é prática: num diretório de instalação só para leitura, uma falha pode aparecer como um processo parado, e não como um erro. Por isso toda verificação de §16 roda com prazo.

### 2.2 Cinco coisas que o roadmap presumiu

**"`log_directory` é relativo ao diretório de trabalho."** Não é. O `logger.py` ancora o caminho relativo na raiz do código, e o [RFC-005 §3.5](rfc-005-logging.md) diz por quê. O piloto pediu o diretório de log a partir de dois diretórios de trabalho e recebeu a mesma resposta nos dois: `<repositório>\logs`. O problema é o de §2.1, e não o de logs espalhados conforme o lugar de onde o processo sobe. O docstring de `thumbnail_directory` no `settings.py` repete a mesma presunção.

**"Remover o default relativo de `indexing_root_path`."** Seguido ao pé da letra, isso quebra o primeiro critério do próprio roadmap. Um campo `Path` sem default é obrigatório, e um `Settings` com campo obrigatório não sobe sem `.env`. O campo também não tem nenhum leitor. O `indexing_worker` exige `--root` e diz no texto de ajuda que não usa o setting (`indexing_worker.py:284`). O `job_runner` indexa por `device_id` e escopos (RFC-029). O único lugar que ainda o descreve como lido é o docstring do `seed_demo.py`, que ficou para trás. O precedente é o `minimum_similarity`, que o RFC-025 removeu por não ter leitor.

**"Só `thumbnail_directory` já usa `%LOCALAPPDATA%\SolidVision`."** Desde o RFC-033, o `embedded_postgres_directory` também usa. Os dois só usam quando a variável existe. O piloto montou um processo filho sem `LOCALAPPDATA` no ambiente, como um launcher que monta o ambiente dos filhos poderia montar, e os dois defaults foram para raízes **diferentes**: `~\.cache\solidvision\thumbnails` e `~\.local\share\solidvision\postgres`. No mesmo processo, o Windows respondeu a pasta certa quando perguntado diretamente (`SHGetKnownFolderPath`).

E o default absoluto não protege o valor configurado. `THUMBNAIL_DIRECTORY=thumbs` num arquivo é aceito e chega ao `Settings` como o caminho relativo `thumbs` (piloto). Quem o resolve depois é o consumidor, contra o diretório de trabalho. O critério "nenhum caminho relativo sobra" precisa valer para o que o usuário escreve, e não só para os defaults.

**"Fazer as credenciais virem do RFC-033, não de um campo fixo."** O campo fixo existe: sem `.env`, o piloto leu `postgresql+psycopg://solidvision:***@localhost:5432/solidvision`. A frase presume, porém, que credencial é algo que um `Settings` consegue guardar. A porta do cluster é escolhida a cada subida ([RFC-033 §8.2](rfc-033-postgres-embarcado.md#82-a-porta)): o adaptador tenta a última, e pega outra se ela estiver ocupada ou reservada. O `Settings` é lido uma vez por processo, e a engine é construída na importação. Uma porta lida nesse instante fica errada no dia em que o cluster reinicia em outra, com a API de pé. Trocar o campo fixo por um campo preenchido pelo adaptador troca um valor errado sempre por um valor errado às vezes (§7).

**"Ler `environment` de um único lugar."** Hoje ele já é lido de um lugar só: um campo de texto com default `development`. Sem `.env`, o piloto leu `'development'`. Um app instalado responderia `"environment": "development"` no `/health`. O defeito não é o número de lugares. É que o modo é uma **declaração** que alguém escreve, com um default que mente quando ninguém escreve.

### 2.3 O ambiente do usuário é fonte de configuração

O `Settings` lê variáveis de ambiente acima do `.env` (RFC-004), e os nomes não têm prefixo: `DEBUG`, `DEVICE`, `BATCH_SIZE`, `LOG_LEVEL`, `ENVIRONMENT`. No desenvolvimento, quem define essas variáveis é o desenvolvedor. Num app instalado, o ambiente é o que os outros programas da máquina deixaram. O piloto pôs uma variável de cada vez no ambiente e importou o `settings`:

| no ambiente | o que acontece |
| --- | --- |
| `DEBUG=*` | **Não importa.** `ValidationError`, `bool_parsing`, código de saída 1 |
| `DEBUG=release` | **Não importa.** O mesmo erro |
| `BATCH_SIZE=auto` | **Não importa.** `int_parsing` |
| `DEBUG=1` | Importa, com `debug = True`. A engine é criada com `echo=settings.debug`, e todo comando SQL passa a ir para o log |
| `ENVIRONMENT=Production`, com espaços em volta do valor | Importa, e o `/health` responde `production` |

`DEBUG=*` é a convenção de um pacote popular do Node. Uma variável que não tem nada a ver com o SolidVision o impede de abrir, e o erro é um traceback num processo que não tem janela.

O mesmo vale para a libpq, e o [RFC-033 §16](rfc-033-postgres-embarcado.md#16-o-que-passa-adiante) deixou o aviso: as conexões do adaptador ignoram `PGOPTIONS`, `PGSSLMODE` e `PGGSSENCMODE` de quem chama, e a engine do projeto não. O RFC-033 também mediu que, sem `connect_timeout`, uma tentativa de conexão numa porta em que ninguém escuta dura mais de dois minutos. O piloto repetiu isso com uma engine do SQLAlchemy sobre o psycopg, como a do projeto: com `connect_timeout=3`, a recusa vem em 3,0 s. Sem ele, a tentativa ainda estava em curso aos 25 s, quando a sonda parou de esperar. A engine de hoje não passa `connect_timeout`. Um `/health` com o banco parado leva esse tempo para responder 503.

### 2.4 Um arquivo editado à mão falha em silêncio

O roadmap pede um arquivo de configuração em `%LOCALAPPDATA%\SolidVision\`. Quem edita esse arquivo é o suporte ou o próprio usuário, no Bloco de Notas, sem teste e sem IDE. O piloto escreveu arquivos como essa pessoa escreveria.

**Chave com o nome quase certo.** `EMBEDDING_BATCH_SIZE=64` e `THUMBNAIL_SIZE=256` foram lidos sem erro e sem aviso, e os valores continuaram 8 e 512. O `extra="ignore"` do RFC-004 descarta o que não conhece. O `.env` desta máquina mostra que isso acontece de verdade: das 11 chaves dele, 5 não são campos do `Settings` (`MODEL_NAME`, `MODEL_VERSION`, `COLLECTION_ROOT`, `THUMBNAIL_SIZE` e `EMBEDDING_BATCH_SIZE`).

**Codificação.** As mesmas duas linhas, com `LOG_LEVEL=DEBUG` na primeira, salvas de quatro jeitos:

| salvo como | lido como `utf-8` (o que o projeto faz) | lido como `utf-8-sig` |
| --- | --- | --- |
| UTF-8 sem BOM | `log_level = 'DEBUG'` | igual |
| UTF-8 **com BOM** | `log_level = 'INFO'`: a primeira chave some, sem erro | `log_level = 'DEBUG'` |
| UTF-16 LE | `UnicodeDecodeError` | o mesmo erro |
| ANSI (1252), com `João` no caminho | `UnicodeDecodeError` | o mesmo erro |

O BOM é o caso ruim, porque é silencioso. As outras duas falham alto, com um traceback.

**Aspas e barras invertidas.** Um caminho do Windows dentro de aspas duplas é reescrito, porque o formato dotenv interpreta `\f`, `\r`, `\t` e `\n` ali dentro:

| a linha | o que chega ao `Settings` |
| --- | --- |
| `THUMBNAIL_DIRECTORY=D:\fotos\raw\thumbs` | `D:\fotos\raw\thumbs` |
| `THUMBNAIL_DIRECTORY="D:\fotos\raw\thumbs"` | `D:`, um *form feed*, `otos`, um *carriage return*, `aw`, uma tabulação, `humbs` |
| `THUMBNAIL_DIRECTORY='D:\fotos\raw\thumbs'` | `D:\fotos\raw\thumbs` |
| `THUMBNAIL_DIRECTORY=D:\Meus thumbs\novos` | `D:\Meus thumbs\novos`: o espaço não pede aspas |
| `THUMBNAIL_DIRECTORY="D:\Meus thumbs\novos"` | `D:\Meus thumbs`, uma quebra de linha, `ovos` |
| `THUMBNAIL_DIRECTORY=D:\thumbs # o disco grande` | `D:\thumbs`: um `#` depois de um espaço começa um comentário |
| `THUMBNAIL_DIRECTORY=%LOCALAPPDATA%\thumbs` | `%LOCALAPPDATA%\thumbs`, literal e relativo |
| `THUMBNAIL_DIRECTORY=${LOCALAPPDATA}\thumbs` | `C:\Users\…\AppData\Local\thumbs`: expandido do ambiente |

`fotos`, `raw`, `thumbs` e `novos` são nomes prováveis de pasta para um fotógrafo, e os quatro começam com uma letra que as aspas duplas reescrevem.

### 2.5 Desenvolvimento e instalação já dividem os mesmos diretórios

O [RFC-033 §15](rfc-033-postgres-embarcado.md#15-riscos) registrou o risco e passou a separação para este RFC. Nesta máquina ele já aconteceu. Lido à mão em 2026-10-13, `%LOCALAPPDATA%\SolidVision` tem:

- `postgres\16\data`, criado no mesmo dia, às 17h53, pelo adaptador rodando do checkout: 1311 arquivos, 63,9 MB. É o lugar onde um app instalado procuraria o cluster dele, e o que está lá foi migrado pelo código em desenvolvimento.
- `thumbnails\`, com 60 arquivos e 2,8 MB, gerados pelo backend do checkout contra o banco do Docker.
- `logs\application.log`, de 2026-09-22, a data do spike. O script que o produziu se perdeu (RFC-033a). O que sobrou mostra que o ramo absoluto do `logger.py` funciona.

As miniaturas são nomeadas pelo id da imagem, e o id só tem sentido dentro de um banco. Dois bancos, o do Docker e o embarcado, já dividem essa pasta.

### 2.6 Dois processos, um arquivo de log

A API e o `job_runner` são processos separados desde o [RFC-029 §6](rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md), e os dois escrevem em `logs\application.log` com um `RotatingFileHandler` (RFC-005). O piloto reproduziu isso com o handler do projeto e um arquivo menor, de 20 000 bytes em vez de 10 MB, para a rotação chegar em segundos. Um processo ocupado grava 600 registros, e cada diretório começa com cinco backups antigos.

| cenário | registros do processo ocupado | erros de logging | backups antigos que sobraram |
| --- | --- | --- | --- |
| Um processo só (controle) | 600 de 600 | 0 | 3 de 5: duas rotações de verdade empurraram dois para fora, como deve ser |
| Dois processos no mesmo arquivo | **219 de 600** | 381 | **1 de 5** |
| Um processo, e `Get-Content -Wait` seguindo o arquivo | 600 de 600 | 0 | 3 de 5 |

O erro é `PermissionError: [WinError 32] O arquivo já está sendo usado por outro processo`, na hora de renomear `application.log` para `application.log.1`. O Windows não deixa renomear um arquivo que outro processo mantém aberto sem ter pedido para compartilhar a exclusão, e o `open()` do Python não pede. O handler descarta o registro que disparou a rotação e tenta de novo no registro seguinte. Os 381 registros perdidos são todos os que o processo ocupado gravou depois de o arquivo encher. A rotação só aconteceu quando o processo quieto, que não disputava o arquivo naquele instante, fez a dele. O ensaio do mesmo dia deu os mesmos 219 de 600 e o mesmo backup único. Uma primeira execução, cujo log foi sobrescrito, deu 220.

Os backups somem por outro motivo. Lendo o `doRollover` da biblioteca padrão, ele desloca os backups (`.4` vira `.5`, `.3` vira `.4`…) **antes** de tentar o rename que falha. Cada tentativa fracassada empurra um backup para fora. Depois de quatro, sobrou um, parado no último lugar.

O leitor não é o problema: o `Get-Content -Wait` do PowerShell não impediu nenhuma rotação. O problema é um segundo **escritor**.

O RFC-005 abre dizendo para que o log serve: *"Quando ele processa 100 mil imagens e 12 falham, a única forma de descobrir quais e por quê é o log."* Um job grande passa dos 10 MB. As linhas perdidas são as do processo que está indexando.

Nesta máquina, o `git status` mostra `?? logs/` (lido à mão). O `.gitignore` cobre `*.log`, e o backup rotacionado se chama `application.log.1`.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Onde as coisas estão | Um módulo, `app/infrastructure/config/layout.py`. É o único de `backend/app` que usa `__file__`, o diretório de trabalho ou `LOCALAPPDATA` (§4.1) |
| O modo | `development` quando `<raiz>\pyproject.toml` existe. Senão, `installed`. É um fato lido, e não um texto configurado. Nenhuma variável e nenhum arquivo o troca (§4.2) |
| Fontes, em desenvolvimento | Como hoje: o ambiente acima do `.env` da raiz do repositório (RFC-004) |
| Fontes, instalado | `%LOCALAPPDATA%\SolidVision\solidvision.env` acima dos defaults. **O ambiente do processo não é fonte** (§5.2) |
| O arquivo do usuário | Formato dotenv, com as mesmas chaves do `.env.example`. UTF-8, com ou sem BOM. Opcional, e nunca criado pelo app (§5.3) |
| Chave desconhecida, aposentada ou sem efeito | Avisada no log, pelo nome (§5.4) |
| Configuração inválida | A subida é recusada, com uma mensagem que nomeia o arquivo e a chave (§5.5) |
| Raiz de dados | Instalado, `%LOCALAPPDATA%\SolidVision`. Em desenvolvimento, `%LOCALAPPDATA%\SolidVision-dev`. Sem a variável, a pasta é perguntada ao Windows (§6.2) |
| Caminhos | Todo setting de caminho é `Path` e sai do `Settings` absoluto. Um valor relativo ancora na pasta do arquivo de configuração. As duas formas pela metade do Windows são recusadas (§6.3) |
| `indexing_root_path` | Sai. Não tem leitor (§6.4) |
| De onde vem a conexão | `database_source`. Instalado, é sempre `embedded`. Em desenvolvimento, o default é `external`, o `.env` e o Docker de hoje (§7.1) |
| Credenciais do cluster | Lidas do cluster **a cada conexão nova**, por `locate()` e um listener `do_connect`. Nunca são campo do `Settings`, nem variável de ambiente (§7.2, §7.3) |
| Cluster não encontrado | Erro de conexão que diz isso. Nunca um fallback para `DATABASE_*` (§7.4) |
| libpq | A engine passa os mesmos parâmetros explícitos do adaptador, e um `connect_timeout`, nas duas fontes (§7.5) |
| `run --` | Exporta a fonte e os dois diretórios, e não mais host, porta e senha (§7.6) |
| A senha | Fora do `repr` do `Settings` (§7.7) |
| Log | `<dados>\logs`. Um arquivo por programa. Uma rotação que não descarta registro nem backup (§8) |
| Como testar "sem checkout" | Uma instalação simulada: o código copiado para fora do repositório, sem `.env`, só para leitura (§9) |
| Docker no desenvolvimento | **Continua.** Um `pytest` puro continua indo ao `.env` |

## 4. O layout e o modo

### 4.1 Um módulo sabe onde as coisas estão

`layout.py` concentra as quatro âncoras de §2.1 e os dois defaults de §2.2. Ele não importa `settings` nem o logger, e é importado pelos dois.

```python
class Environment(StrEnum):
    DEVELOPMENT = "development"
    INSTALLED = "installed"

@dataclass(frozen=True)
class Layout:
    environment: Environment
    root: Path             # a raiz do repositório, ou a da instalação
    data_directory: Path   # %LOCALAPPDATA%\SolidVision-dev, ou \SolidVision

    # derivados, sem tocar no disco:
    #   config_file        root\.env, ou data_directory\solidvision.env
    #   config_base        a pasta de config_file: a âncora dos caminhos relativos
    #   backend_directory  root\backend (alembic.ini e alembic\)
    #   postgres_runtime   root\runtime\postgres

layout = detect_layout()
settings = load_settings(layout)
```

A árvore é a mesma nos dois modos, que é o que o [RFC-033 §5.2](rfc-033-postgres-embarcado.md#52-onde-o-adaptador-procura) já presumia para o runtime:

```text
<raiz>\
    backend\           app\, alembic\, alembic.ini
    runtime\postgres\  RFC-033
    pyproject.toml     só no checkout
    .env               só no checkout
```

Um `Layout` pode ser construído à mão, com qualquer raiz e qualquer diretório de dados. É assim que os testes de unidade exercitam o modo instalado de dentro do checkout, sem estado global.

**A raiz num app congelado.** O RFC-033 deixou a pergunta para cá. A resposta deste RFC é reduzi-la a uma função: `detect_layout()` é o único lugar que calcula a raiz, e um teste de arquitetura impede que apareça um segundo (§10). Com Python embutido e o código em árvore, uma das duas opções que o roadmap lista, o `__file__` continua valendo e nada muda. Com um executável congelado, a raiz passa a sair do caminho do executável, e a mudança é nessa função. Este RFC não congela nada e não testa um app congelado (§12).

**O adaptador continua sem ler `settings`.** O `database_setup.py` passa a pegar `alembic.ini` e `alembic\` do `layout`, em vez de calcular a própria âncora. O layout é geometria de diretórios, e não configuração. A regra do [RFC-033 §4.1](rfc-033-postgres-embarcado.md#41-onde-fica-e-por-que-não-há-porta-no-domain) continua de pé.

### 4.2 O modo é lido, não declarado

`development` quando existe `pyproject.toml` na raiz. `installed` em qualquer outro caso.

O `pyproject.toml` é o que faz de um diretório um checkout: é dele que o `pytest`, o `mypy` e o `ruff` dependem, e um instalador não tem motivo para levá-lo. O `.git` não serve, porque um checkout exportado não o tem.

**O erro tem um lado seguro, e o default fica nele.** Se uma máquina de usuário for tomada por desenvolvimento, o app lê o ambiente, grava em `SolidVision-dev` e, no dia em que a detecção for corrigida, os dados do usuário parecem ter sumido. É silencioso e caro. Se uma máquina de desenvolvimento for tomada por instalada, o app ignora o `.env` e procura um cluster embarcado, e o desenvolvedor percebe na primeira execução. Por isso desenvolvimento precisa de prova, e instalado é o que sobra.

**Não há variável para trocar o modo.** Uma `SOLIDVISION_ENVIRONMENT` seria o segundo lugar que o roadmap pede para não existir, e voltaria a ser uma declaração. Quem precisa do modo instalado num teste constrói um `Layout`. Quem precisa dele de ponta a ponta usa a instalação simulada de §9.

`settings.environment` continua existindo, para o `/health` não mudar, mas deixa de ser um campo: é o modo do layout com que o `Settings` foi carregado. `ENVIRONMENT=` num arquivo vira chave aposentada (§5.4). O RFC-035 lê o modo daqui para ligar o `HF_HUB_OFFLINE`.

## 5. As fontes de configuração

### 5.1 A precedência

Da mais forte para a mais fraca:

| | desenvolvimento | instalado |
| --- | --- | --- |
| 1 | Argumentos do construtor (testes) | Argumentos do construtor (testes) |
| 2 | Variáveis de ambiente do processo | — |
| 3 | `<repositório>\.env` | `%LOCALAPPDATA%\SolidVision\solidvision.env` |
| 4 | Defaults | Defaults |

Cada modo lê **um** arquivo, e nunca o do outro. Um `.env` deixado no diretório de instalação não é lido. O arquivo do usuário não é lido de dentro de um checkout.

A conexão do banco embarcado não está nessa tabela. Ela não vem de nenhuma dessas fontes (§7).

### 5.2 Instalado, o ambiente não é fonte

É a resposta a §2.3. O piloto confirmou que o pydantic-settings aceita isso: uma subclasse cujas fontes são só o construtor e o arquivo, com `DEBUG=*` e `LOG_LEVEL=ERROR` no ambiente e `LOG_LEVEL=WARNING` no arquivo, carregou `debug = False` e `log_level = 'WARNING'`.

A fonte precisa ser decidida **antes** de o `Settings` ser construído. Com `DEBUG=*` no ambiente, o módulo `settings.py` de hoje nem importa, porque constrói o singleton no caminho. Por isso existe `load_settings(layout)`: o layout é detectado primeiro, e as fontes saem dele.

Duas leituras do ambiente continuam, e nenhuma delas é configuração do app:

- `LOCALAPPDATA`, lida pelo layout para achar o perfil (§6.2). É uma variável do sistema.
- `${NOME}` dentro de um valor do arquivo, que o formato dotenv expande do ambiente (piloto, §2.4). Só acontece quando quem escreveu o arquivo pediu.

O launcher do RFC-036 não precisa passar nada aos filhos pelo ambiente: cada processo calcula o mesmo layout e lê o mesmo arquivo. Se ele vier a precisar, um prefixo próprio é a porta (§11).

### 5.3 O arquivo do usuário

`%LOCALAPPDATA%\SolidVision\solidvision.env`.

- **Opcional.** Sem ele, o app sobe com os defaults. O app nunca o cria e nunca escreve nele.
- **Formato dotenv, com as mesmas chaves do `.env.example`.** O projeto já tem um parser, um vocabulário e um tratamento para o campo que não é texto simples (`supported_extensions`, corrigido no commit `322d946`). Um segundo formato seria um segundo caminho de leitura para os mesmos campos (§11).
- **UTF-8, com ou sem BOM.** O arquivo é lido como `utf-8-sig`, que aceita os dois (§2.4). O `.env` do desenvolvimento passa a ser lido do mesmo jeito.
- **Caminhos sem aspas.** O espaço não pede aspas, e as aspas duplas reescrevem o caminho (§2.4). As regras de §6.3 transformam o engano em erro, em vez de em pasta errada.

**Hipótese, a testar em §16:** o Bloco de Notas salva um arquivo novo chamado `solidvision.env` como `solidvision.env.txt`, com a extensão escondida. Se confirmar, o app avisa quando existe o `.txt` e não existe o arquivo certo (§5.4).

> **Confirmada, à mão (§16).** No Bloco de Notas desta máquina, um arquivo novo com `LOG_LEVEL=DEBUG`, salvo com o nome `solidvision.env` e o tipo deixado como estava, foi gravado como `solidvision.env.txt`: 15 bytes, UTF-8 sem BOM. O aviso já existia, implementado antes da confirmação porque é inofensivo se a hipótese for falsa. A medição o exercitou com um `solidvision.env.txt` escrito por ela: o processo subiu com os defaults e escreveu o aviso, com o nome do arquivo e o que fazer. Quem instruir um usuário a criar o arquivo deve dizer para escolher o tipo "Todos os arquivos" (§14).

### 5.4 Chaves que não fazem o que parecem

Três casos, e os três viram aviso no log, com o nome da chave e o do arquivo:

- **Desconhecida.** Não é campo do `Settings`. É o caso de §2.4.
- **Aposentada.** `ENVIRONMENT` e `INDEXING_ROOT_PATH`. O aviso diz o que a substituiu.
- **Sem efeito neste modo.** `DATABASE_SOURCE` e as cinco `DATABASE_*` no arquivo do usuário, porque instalado a conexão é sempre a do cluster embarcado (§7.1).

Os avisos valem para os dois arquivos. O `.env` desta máquina passaria a produzir cinco.

O `extra="ignore"` continua. Recusar a subida por uma chave a mais faria um arquivo opcional derrubar o app por um erro de digitação (§11).

> **Decidido na implementação (§17.2): "sem efeito" é literal.** Instalado, `DATABASE_SOURCE` e as cinco `DATABASE_*` do arquivo do usuário são descartadas **antes** da validação. Um `DATABASE_PORT=abc` ali produz o aviso, e não recusa a subida. O texto não dizia qual dos dois acontecia.

### 5.5 Configuração inválida recusa a subida

Um valor que não valida (`BATCH_SIZE=abc`), um arquivo que não é UTF-8 ou um caminho recusado por §6.3 levantam `ConfigurationError`, com uma mensagem de uma linha: o arquivo, a chave e o problema. O processo sai com código diferente de zero, sem traceback.

Subir com o default no lugar do valor inválido seria pior. Quem escreveu `THUMBNAIL_DIRECTORY` apontando para outro disco e errou o caminho teria as miniaturas gravadas exatamente onde disse que não queria.

Quem mostra essa linha ao usuário é o RFC-036 (§14). Um processo sem console não tem onde escrevê-la.

> **Feito (§17.2).** `load_settings()` levanta `ConfigurationError` com `<origem>: <CHAVE>: <problema>`, e o singleton do módulo a converte em `SystemExit`: o processo escreve `SolidVision cannot start: …` no `stderr` e sai com código 1. A origem é o caminho do arquivo, `environment variable` (só em desenvolvimento) ou `constructor argument`. A mensagem nunca repete o valor, que pode ser uma senha.

### 5.6 O relatório de configuração

Na primeira configuração do logger, cada processo escreve de onde veio a configuração dele: o modo, o arquivo e se ele existe, a raiz de dados, os diretórios resolvidos e a fonte do banco. Os avisos de §5.4 vêm em seguida. A senha não aparece.

O `settings.py` não pode logar, porque o logger o importa. `load_settings` devolve o relatório junto com o `Settings`, e quem o escreve é o logger.

É isso que torna a precedência de §5.1 verificável numa máquina de usuário: o log diz qual arquivo foi lido.

> **Feito, e vai para o console também (§17.2).** O relatório é escrito pelo logger raiz do app, que tem os dois handlers do RFC-005. Todo processo escreve oito linhas no `stderr` ao subir, mais uma por aviso.

## 6. Os diretórios do usuário

### 6.1 A árvore

```text
%LOCALAPPDATA%\SolidVision\
    solidvision.env             opcional; nunca criado pelo app (§5.3)
    logs\                       um arquivo por programa (§8)
    thumbnails\                 RFC-030
    postgres\                   RFC-033; DACL explícita
        16\
            data\               o log do servidor fica em data\log
            password, adapter.lock, startup.log, pg_ctl.log
```

Em desenvolvimento, a mesma árvore em `%LOCALAPPDATA%\SolidVision-dev\`, sem o `solidvision.env`.

Os pesos dos modelos não estão nela. Onde eles ficam é do RFC-035. Hoje o `transformers` grava em `~\.cache\huggingface`, fora desta árvore, e continua assim até lá (§12).

A raiz de dados herda a ACL do `%LOCALAPPDATA%`. A DACL explícita continua onde o RFC-033 a pôs, em `postgres\`, que é onde está a senha.

### 6.2 A raiz de dados

O layout resolve a pasta do perfil nesta ordem: a variável `LOCALAPPDATA`, quando existe e é absoluta. Sem ela, `SHGetKnownFolderPath(FOLDERID_LocalAppData)`. Fora do Windows, `~/.local/share`, uma raiz só, para um processo que não vai longe ali de qualquer jeito.

A variável vem primeiro porque os dois defaults de hoje já a obedecem, e porque é por ela que a validação aponta um processo para um diretório temporário sem tocar no perfil de verdade (§9). A pergunta ao Windows substitui os dois fallbacks divergentes de §2.2. No piloto ela respondeu a pasta certa num processo sem a variável, e custou menos de 0,1 ms por chamada (75 ms para mil chamadas; 60 ms no ensaio).

A raiz **não é configurável**. O arquivo de configuração mora dentro dela. Os três diretórios continuam configuráveis um a um (`THUMBNAIL_DIRECTORY`, `LOG_DIRECTORY`, `EMBEDDED_POSTGRES_DIRECTORY`), que é o que alguém com o disco do sistema pequeno precisa.

**`SolidVision-dev` é uma pasta irmã, e não uma subpasta.** É a separação que o RFC-033 pediu, estendida da pasta `postgres\` para a raiz inteira, porque as miniaturas e o log têm o mesmo problema (§2.5). Uma subpasta dentro de `SolidVision` cairia em qualquer política futura de desinstalação sobre os dados do usuário.

### 6.3 Todo caminho sai absoluto

Os quatro settings de caminho (`thumbnail_directory`, `log_directory`, `embedded_postgres_directory` e `embedded_postgres_runtime_directory`) são `Path` e passam pelo mesmo validador. O `log_directory` hoje é `str`, e por isso escaparia de qualquer verificação que procurasse campos `Path` (§10).

As regras, na ordem:

1. **Caractere de controle no valor: recusa.** Nenhum caminho do Windows tem um. É o rastro das aspas duplas de §2.4, e a mensagem diz isso.
2. **`%NOME%` no valor: recusa.** O formato não expande essa forma, e o resultado seria uma pasta chamada `%LOCALAPPDATA%`. A mensagem mostra a forma que funciona, `${NOME}`.
3. **Forma pela metade: recusa.** `D:thumbs` (unidade sem barra) é relativo ao diretório corrente daquela unidade, e continua relativo depois de ancorado. `\thumbs` (barra sem unidade) herda a unidade de onde for ancorado.
4. **Relativo: ancora em `config_base`**, a pasta do arquivo de configuração do modo. É a raiz do repositório em desenvolvimento e a raiz de dados quando instalado.
5. O resultado é normalizado, sem consultar o disco.

A regra 4 mantém o que os `.env` existentes já significam. `LOG_DIRECTORY=logs`, que o `.env.example` traz, continua sendo `<repositório>\logs`, como o RFC-005 §3.5 definiu. A diferença é que a conta passa a ser feita no `Settings`, uma vez, e não no consumidor.

> **Acrescentado na implementação (§17.2): o valor vazio é recusado, antes das cinco regras.** As regras não falavam dele. `THUMBNAIL_DIRECTORY=` chega ao `Settings` como o caminho `.`, que a regra 4 ancora na pasta do próprio arquivo: instalado, as miniaturas iriam soltas para `%LOCALAPPDATA%\SolidVision`. A medição mostrou exatamente isso numa cópia do código anterior à correção (§16). Tratar o vazio como "não escrito" seria escolher o default em silêncio, que é o que §5.5 recusa. A mensagem diz para apagar a linha.

Os defaults deixam de ser três funções com `os.environ` dentro do `settings.py` e passam a sair do layout.

### 6.4 `indexing_root_path` sai

O campo é removido, pelo motivo de §2.2. `INDEXING_ROOT_PATH=` num arquivo vira chave aposentada, e o aviso diz que a indexação é por dispositivo e escopos (RFC-029). O docstring do `seed_demo.py` é corrigido.

### 6.5 O que muda para quem já tem um checkout

Os defaults de desenvolvimento mudam de `SolidVision` para `SolidVision-dev`. O código **não move nem apaga nada**.

- **Miniaturas.** As que existem ficam em `SolidVision\thumbnails`. Basta mover a pasta: os caminhos no banco são relativos a `thumbnail_directory`. Ou rodar o `thumbnail_backfill`.
- **Cluster.** O que foi criado pelo checkout em `SolidVision\postgres` (§2.5) fica onde um app instalado vai procurar o dele. Cabe ao desenvolvedor apagá-lo ou movê-lo. Mover um cluster parado é válido ([RFC-033 §12](rfc-033-postgres-embarcado.md#12-testes)).
- **Log.** Quem tem `LOG_DIRECTORY=logs` no `.env` continua com `<repositório>\logs`. Quem não tem passa a ter o log em `SolidVision-dev\logs`.

O relatório de §5.6 mostra os diretórios em uso, que é como se confere a mudança.

## 7. O banco: a conexão vem do adaptador

### 7.1 `database_source`

| modo | fonte | de onde vem a conexão |
| --- | --- | --- |
| Instalado | `embedded`, sempre | Do cluster do usuário |
| Desenvolvimento, default | `external` | Dos cinco campos `DATABASE_*`: o `.env` e o Docker, como hoje |
| Desenvolvimento, com `DATABASE_SOURCE=embedded` | `embedded` | Do cluster de `SolidVision-dev` |

A terceira linha é o que o RFC-036 precisa para *"o dia a dia sair do Docker antes do frontend existir"*: uma linha no `.env`.

Os cinco campos `DATABASE_*` continuam com os defaults de hoje, que são os do `docker-compose.yml`. O que este RFC garante é que a fonte `embedded` nunca os lê (§7.4).

### 7.2 `locate()`: ler, sem subir

O adaptador ganha uma leitura:

```python
cluster.locate()   # -> ClusterConnection, ou ClusterNotRunningError
```

Ela não sobe nada, não toma o lock, não conecta. Devolve a porta que o servidor **está usando**, da linha 4 do `postmaster.pid`, e a senha do arquivo `password`, depois das mesmas conferências do `status()`: o PID está vivo, o executável é deste runtime e o `postmaster.pid` aponta para este `data\`. Com qualquer estado que não seja `ready`, levanta `ClusterNotRunningError`, dizendo o que encontrou e quem sobe o cluster.

`ready` no arquivo não é `ready` no servidor ([RFC-033 §8.4](rfc-033-postgres-embarcado.md#84-adoção)). O `locate()` não espera. Uma conexão aberta durante a reinicialização é recusada pelo servidor, e quem chamou recebe um erro de banco comum.

### 7.3 A engine pergunta ao conectar

A engine continua sendo um singleton de módulo, construído na importação. Com a fonte `embedded`, ela é construída **sem porta e sem senha**, e um listener `do_connect`, o mecanismo do SQLAlchemy para credenciais que mudam, preenche os dois a cada conexão física nova:

```python
@event.listens_for(engine, "do_connect")
def supply(dialect, record, cargs, cparams):
    found = cluster.locate()
    cparams.update(host=found.host, port=found.port, user=found.user,
                   password=found.password, dbname=found.database)
```

O piloto verificou três coisas sem usar um servidor PostgreSQL:

- A URL de uma engine assim é `postgresql+psycopg://solidvision@127.0.0.1/solidvision`, sem porta e sem senha.
- A porta posta pelo listener é a que o driver usa. Um socket da própria sonda ficou escutando, o listener recebeu a porta dele, e o driver ligou para lá e mandou os 8 primeiros bytes.
- Um listener que levanta `psycopg.OperationalError` faz o `engine.connect()` levantar `sqlalchemy.exc.OperationalError`, que é um `SQLAlchemyError`. É a exceção que o `/health` já captura para responder 503. O listener converte o `ClusterNotRunningError` nela.

O que o piloto não fez é abrir uma conexão de verdade por esse caminho. Isso é de §16.

O que isso resolve:

- **Importar não exige o cluster de pé.** A API sobe com o banco parado e responde 503, como o RFC-008 definiu.
- **A porta pode mudar.** Depois de um reinício do cluster em outra porta, as conexões do pool morrem, o `pool_pre_ping` as descarta, e a próxima conexão pergunta de novo. **Hipótese**, a medir em §16 com a API de pé.
- **A senha não passa pelo `Settings`**, nem pelo ambiente de processo nenhum.

Nada muda em `session.py`, nos repositórios, na API ou no `job_runner`. Muda `engine.py`, que passa a ter uma função `build_engine()`, e o `alembic/env.py`, que a usa.

> **Feito, com duas escolhas que o texto não fazia (§17.2).** O listener converte **qualquer** erro do adaptador em `psycopg.OperationalError`, e não só o `ClusterNotRunningError`: um cluster `ready` sem o arquivo `password` (`ClusterRefusedError`) e um runtime sem manifesto (`RuntimeInvalidError`) também viram 503 no `/health`, e não 500. E o adaptador é construído na primeira conexão, e não quando a engine é construída: importar `engine.py` não toca no disco.

O custo é um `locate()` por conexão física, e as conexões são reaproveitadas pelo pool. O custo dele é `TBM`, com o critério declarado em §16.

> **Medido (§16): as duas hipóteses se confirmaram, e o segundo ramo não entra.** Com a API de pé, o cluster parou e voltou noutra porta, com a antiga ocupada por um socket, e a requisição seguinte ao mesmo processo da API respondeu 200. O mesmo foi mostrado no nível da engine por um teste `embedded` (§10). O `locate()` custa 2,54 ms na mediana de 900 chamadas, contra o critério de 10 ms, e uma conexão física nova fica 3,6 ms mais cara com o listener (66,7 ms contra 63,2 ms). Antes de cada conexão a engine também tira as `PG*` do ambiente (§7.5), e esse custo está dentro dos 3,6 ms.

### 7.4 Sem fallback

Com a fonte `embedded`, cluster não encontrado é erro. Nunca vira uma tentativa em `localhost:5432`.

O [RFC-033a §4.2](rfc-033a-linha-de-base-do-postgres-embarcado.md) mostrou o que um fallback faz: um nome de variável errado manda a suíte para o banco do Docker, e ela imprime o mesmo `passed`. Num app instalado, o banco em `localhost:5432` seria um PostgreSQL de outra pessoa.

### 7.5 A libpq e o ambiente

Com a fonte `embedded`, a engine passa os parâmetros que o adaptador já passa nas conexões dele (`options=''`, `sslmode=disable`, `gssencmode=disable`), tirados da **mesma constante**. A libpq só consulta o ambiente para um parâmetro que não recebeu.

**Quais outras variáveis `PG*` alcançam uma conexão que já recebe host, porta, usuário, senha e banco é `TBM`.** As candidatas são `PGCLIENTENCODING`, `PGSERVICE`, `PGTZ`, `PGDATESTYLE`, `PGAPPNAME`, `PGTARGETSESSIONATTRS`, `PGREQUIREAUTH` e `PGCHANNELBINDING`. §16 define cada uma com um valor hostil e passa pela engine uma escrita e a leitura de um nome de arquivo fora da página 1252. O que a medição achar entra na constante. A libpq do cliente é a que vem com o psycopg (18.3 nesta máquina, lido à mão), e não a 16 do servidor: a lista de variáveis é a dela.

> **Medido: a constante não bastava, nem para o adaptador (§17.1).** A medição não testou só as oito candidatas: perguntou à própria libpq (`PQconndefaults`) quais variáveis ela lê, e são 42 na 18.3, contando as sete que não são default de parâmetro (`PGTZ`, `PGDATESTYLE`, `PGGEQO`, `PGSERVICEFILE`, `PGSYSCONFDIR`, `PGLOCALEDIR`, `PGREALM`). Cada uma recebeu um valor hostil, num processo próprio, contra três conexões: uma que só passa host, porta, usuário, senha e banco; o login do adaptador (um `start` que adota o cluster); e a engine do projeto, com a escrita e a leitura de `Łódź/東京/✈.jpg`.
>
> | | mudam ou derrubam a conexão |
> | --- | --- |
> | Só host, porta, usuário, senha e banco | 18 das 42 |
> | O login do adaptador, com os três parâmetros do RFC-033 | **14** |
> | A engine, com os mesmos três | **15** |
>
> Os três parâmetros resolvem `PGOPTIONS`, `PGSSLMODE` e `PGGSSENCMODE`, e só. Quatro das que sobram **não têm parâmetro que as anule**: `PGTZ`, `PGDATESTYLE` e `PGGEQO` vão para o servidor como configurações da sessão, e um valor inválido faz o servidor recusar a conexão (`FATAL: invalid value for parameter "TimeZone"`); um valor válido muda a sessão em silêncio (`TimeZone` de `America/Sao_Paulo` para `Pacific/Kiritimati`). `PGSERVICE` com um nome que não existe é erro antes de qualquer conexão, e um parâmetro `service` vazio não o cancela: a libpq passa a procurar um serviço de nome vazio (conferido à mão, `definition of service "" not found`). As outras onze teriam parâmetro, e três deles só existem na libpq 17 ou 18, segundo a documentação dela (`sslnegotiation`, `min_protocol_version`, `max_protocol_version`): uma constante com eles ficaria presa à versão da libpq. Entre as onze estão `PGCLIENTENCODING=LATIN1`, que fez a escrita do nome de arquivo falhar na engine, e `PGHOSTADDR`, que mandou a conexão para outro endereço até o `connect_timeout`.
>
> **O que mudou no código:** antes de toda conexão com o cluster, a do adaptador e a da engine, o processo tira do próprio ambiente toda variável `PG*` (`discard_libpq_environment()`, em `database_setup.py`) e registra os nomes no log, nunca os valores. É a regra que o RFC-033 já aplicava aos processos do servidor (`child_environment`). Do processo inteiro, e não em volta de uma conexão: a libpq lê as variáveis durante a conexão, e devolvê-las depois disputaria com qualquer outra thread conectando no mesmo instante. Com a fonte `external`, as `PG*` continuam sendo do desenvolvedor. A constante continua com os três parâmetros. Depois da correção, nenhuma das 42 alcança o adaptador nem a engine (§16).

**`connect_timeout` nas duas fontes**, com o valor que o adaptador já usa (10 s). É a correção do que §2.3 mediu.

Com a fonte `external`, as `PG*` continuam sendo do desenvolvedor.

### 7.6 `run --`, o Alembic e a propriedade `database_url`

**`run -- <comando>`** passa a exportar `DATABASE_SOURCE=embedded`, `EMBEDDED_POSTGRES_DIRECTORY` e `EMBEDDED_POSTGRES_RUNTIME_DIRECTORY`, e deixa de exportar host, porta, usuário, senha e banco. É a troca que o RFC-033 §11 anunciou. Ela tem uma consequência que vale mais do que a troca: a suíte inteira, rodada por `run -- python -m pytest`, passa pelo `locate()` e pelo `do_connect`. O mecanismo do app instalado deixa de ser coberto só pelos testes dele.

Os nomes continuam vindo de uma constante, e o teste que confere cada um contra os campos do `Settings` continua.

> **Uma consequência de §7.5 (§17.1).** O comando de `run --` também não recebe as `PG*` de quem chamou: o adaptador as tira do próprio processo antes de conectar, e o comando herda o que sobrou.
>
> **Feito (§17.2).** A constante `ENVIRONMENT_VARIABLES` mudou de dono: os três nomes não são atributos da conexão, e sim do cluster. Ela foi para `cluster.py`, e quem monta o dicionário é `EmbeddedPostgres.environment()`. Como o adaptador não lê `settings`, o valor `embedded` está escrito lá também (`EMBEDDED_SOURCE`), e um teste o confere contra `DatabaseSource`.

**`alembic/env.py`** deixa de montar a URL e usa `build_engine()`. `alembic upgrade head` na linha de comando continua igual.

**`Settings.database_url` sai.** Ficaria sem leitor, e deixaria dois lugares capazes de produzir uma URL. O `build_engine()` monta a da fonte `external` com `URL.create`, que escapa a senha. A interpolação de hoje não escapa ([RFC-033 §7.2](rfc-033-postgres-embarcado.md#72-a-senha)).

### 7.7 A senha fora do `repr`

`database_password` ganha `repr=False`. O piloto construiu um `Settings` com uma senha de teste e a encontrou no `repr`. É o aviso que o RFC-033 §16 deixou, aplicado ao campo que sobra, o da fonte `external`.

## 8. O log

O roadmap pede uma linha: *"tornar `log_directory` absoluto e por usuário"*. É §8.1. As outras duas subseções vão além dela, por causa de §2.6. Mover o log é o momento em que o conteúdo de `logs\` vira o que o suporte vai ler, e o piloto mostrou que o arranjo de hoje perde as linhas de quem mais escreve.

### 8.1 Onde

`<dados>\logs`, pelas regras de §6.3. O formato, o nível, os 10 MB e os cinco backups do RFC-005 não mudam.

O log do servidor PostgreSQL continua onde o RFC-033 o pôs, em `postgres\16\data\log`. São dois lugares, um por dono.

### 8.2 Um arquivo por programa

O default de `log_filename` deixa de ser `application.log` e passa a ser o nome do programa:

| como o processo foi iniciado | arquivo |
| --- | --- |
| `python -m uvicorn app.presentation.api:app` | `uvicorn.log` |
| `python -m app.infrastructure.workers.job_runner` | `job_runner.log` |
| `python -m app.infrastructure.database.embedded …` | `embedded.log` |
| `python -m app.cli …` | `cli.log` |
| `python -m pytest`, ou `pytest` | `pytest.log` |
| Qualquer outro (`python -c`, um REPL) | `python.log` |

O nome é derivado em um lugar só, do módulo passado ao `-m` ou do executável. Nenhum ponto de entrada precisa declarar nada, e isso importa: o logger é configurado na primeira chamada de `get_logger()`, durante a importação, antes de qualquer `main()` rodar.

> **Feito, lendo `sys.orig_argv` (§17.2).** Enquanto os pacotes de um `python -m pacote.modulo` ainda estão sendo importados, que é quando o logger é configurado, `sys.argv[0]` é o texto `-m` e `__main__.__spec__` ainda não existe. `sys.orig_argv` já tem a linha de comando inteira. A tabela ganhou uma linha que não listava: `python caminho\script.py` dá o nome do script (`stage_installation.log`).

`LOG_FILENAME` continua existindo, para quem quer o nome fixo de hoje.

### 8.3 Uma rotação que não perde

Um arquivo por programa tira o caso comum. Não tira dois processos do **mesmo** programa, e o `Settings` já descreve um: *"running two of them is running the command twice"*, sobre o `job_runner`. Também não tira o `LOG_FILENAME` fixo.

O handler passa a rotacionar na ordem inversa da biblioteca padrão:

1. Tenta primeiro o rename que pode falhar, o do arquivo corrente.
2. Se falhar, reabre o arquivo, **grava o registro** e marca a próxima tentativa para dali a `TBM` segundos. Nenhum backup foi tocado.
3. Só com o rename feito, desloca os backups.

Enquanto dois processos vivos dividirem um arquivo, ele não rotaciona e passa do teto. A primeira rotação depois que um deles sair o corta. É o custo declarado: o arquivo cresce em vez de perder linhas.

> **Medido: 1 s (§16).** O critério foi declarado antes da medição: o menor de 1, 5 e 30 s em que as tentativas que falham custam menos de 0,1% do tempo entre duas delas. Menor é melhor a partir daí, porque é quanto tempo o arquivo fica acima do teto depois que o outro processo saiu. Uma tentativa que falha (fechar, o rename que o Windows recusa, reabrir) custa 0,19 ms neste HDD, contra 0,01 ms de um registro. Feita a cada registro, ela deixou 600 registros dez vezes mais lentos (0,353 s contra 0,034 s). Uma vez por segundo, é 0,02% do tempo. A implementação usou 5 s, provisoriamente, até a medição.
>
> **Feito com um nome intermediário (§17.2).** O arquivo corrente é renomeado para `<arquivo>.rotating`, os backups são deslocados, e só então `.rotating` vira `.1`. Se o processo morrer entre os dois passos, a rotação seguinte acha o `.rotating` e termina a anterior antes de começar a dela.

## 9. A instalação simulada

O primeiro critério do roadmap, *"sobe a partir de um diretório qualquer, sem `.env`"*, não pode ser testado de dentro do checkout. O checkout tem um `.env`, tem um `pyproject.toml`, e apagar qualquer um dos dois para testar é mexer no ambiente de quem desenvolve.

`scripts/stage_installation.py` monta, num diretório fora do repositório, a árvore de §4.1 **sem** `pyproject.toml`, `.env` e testes: uma cópia de `backend\app`, `backend\alembic`, `alembic.ini` e `runtime\postgres`. Depois compila os `.pyc` e, com `--read-only`, deixa a árvore só para leitura e execução para o usuário, por `icacls` e pelo SID, como o RFC-033 fez com a DACL. É o que `C:\Program Files` é para um processo comum.

**Não é o instalador.** Quem roda o código é o interpretador do virtualenv de desenvolvimento, com `PYTHONPATH` apontando para a cópia. Congelar o runtime continua fora do roadmap.

> **Feito (§17.2).** `python scripts/stage_installation.py <destino> [--read-only] [--no-compile]`, e `--unlock` e `--remove` para devolver e apagar uma cópia trancada: o usuário é dono da árvore, e um dono pode reescrever a DACL mesmo sem poder escrever nos arquivos. O script recusa um destino dentro do repositório e um que já exista, e escreve na raiz da cópia um `SOLIDVISION-STAGED.json`, sem o qual `--unlock` e `--remove` recusam o diretório. Depois de trancar, ele confere por um processo filho, com prazo, que escrever na raiz, nas duas pastas de cima e na pasta mais funda do código é negado. A cópia leva 124,3 MB em 2826 arquivos, dos quais o runtime é quase tudo: de 71 a 82 s com o disco frio neste HDD, e 7 s com os arquivos no cache do sistema. `backend\tests\test_stage_installation.py` confere, sobre um repositório em miniatura, que a cópia nunca leva `pyproject.toml`, `.env` nem testes, e que ela é `installed` onde a origem é `development`.

Cada execução contra a cópia:

- parte de um diretório de trabalho vazio;
- recebe um `LOCALAPPDATA` temporário, de modo que a primeira execução é de verdade a primeira e o perfil real não é tocado;
- **prova o alvo**, como o RFC-033a ensinou: imprime `app.__file__`, que tem de estar debaixo da cópia, e `settings.environment`, que tem de ser `installed`;
- tem prazo (§2.1).

**O que "grava tudo em `%LOCALAPPDATA%\SolidVision`" consegue provar, e o que não consegue.** Prova: o que aparece debaixo da raiz de dados; o diretório de trabalho continua vazio; a árvore instalada continua com as mesmas entradas e as mesmas datas; e nada novo aparece no primeiro nível de `%USERPROFILE%`, `%APPDATA%` e `%TEMP%`. Isso é uma comparação de lugares escolhidos. Não é um rastreamento de toda escrita do processo, que pediria privilégio de administrador.

Duas coisas ficam de fora do "tudo", e são ditas aqui para não parecerem cobertas:

- **O cache dos modelos**, que é do RFC-035. A validação sobe a API sem carregar modelo (`WARM_UP_MODELS=false`, o default).
- **A miniatura de ponta a ponta.** Gerar uma miniatura por um job de indexação exige o modelo de embedding. O que §16 prova é mais estreito: o `get_thumbnail_store()` da raiz de composição, na instalação simulada, grava debaixo de `<dados>\thumbnails`.

**Hipótese, a medir:** numa árvore só para leitura, o Python não consegue gravar os `.pyc` e recompila os módulos do projeto a cada subida. O `stage_installation.py` compila antes de trancar, e §16 mede a importação dos dois jeitos. O número vai para o instalador.

> **Medido (§16): metade confirmada, e a outra metade não se separou do ruído.** Na árvore trancada o Python não gravou nenhum `__pycache__`, sem erro e sem aviso: ele recompila os módulos do projeto a cada subida. Quanto isso custa, este HDD não disse. O tempo próprio dos 137 módulos do projeto, na mediana de cinco importações, foi 1,66 s sem os `.pyc` e 1,63 s com eles na execução final; 1,61 s e 1,23 s na execução anterior; e ao contrário, 1,47 s e 1,70 s, numa rodada de desenvolvimento com outra carga no disco. A diferença ficou entre −0,23 e +0,39 s, dentro da variação entre rodadas. A importação inteira da API leva de 13 a 18 s, quase toda no `torch`. Para o instalador: compilar na instalação não custa nada e não faz mal, e o que ela economiza é no máximo alguns décimos de segundo por processo, perto de uma subida que é dominada pelo `torch`.

## 10. Testes

Na suíte padrão, sem banco e sem rede.

**O critério do roadmap, em quatro testes:**

- Todo campo do `Settings` do tipo `Path` é absoluto, nos dois modos, em três situações: sem arquivo; com um arquivo que dá um valor relativo a cada um; e, em desenvolvimento, com o ambiente dando valores relativos.
- Todo campo cujo nome termina em `_directory`, `_path` ou `_file` **é** do tipo `Path`. É o guarda contra o próximo `log_directory: str`.
- Dois subprocessos, em diretórios de trabalho diferentes, imprimem os mesmos caminhos.
- Nenhum módulo de `backend/app` fora de `layout.py` usa `__file__`, `Path.cwd()`, `os.getcwd()` ou `Path.home()`. É um teste de arquitetura sobre a árvore sintática, como os que o projeto já tem.

**O resto:**

- O modo: com e sem `pyproject.toml`, com um `Layout` em diretório temporário.
- As fontes: instalado, uma variável de ambiente com nome de setting não muda nada. O `.env` da raiz não é lido. Em desenvolvimento, o arquivo do usuário não é lido.
- O arquivo: BOM, UTF-16, ANSI, aspas duplas, `%NOME%`, `D:thumbs`, `\thumbs`, relativo. Cada um com o resultado de §5 e §6.3. Esses testes fixam o comportamento da biblioteca que o piloto leu: uma atualização que o mude quebra um teste.
- As chaves: desconhecida, aposentada e sem efeito aparecem no relatório.
- Toda chave do `.env.example`, comentada ou não, é um campo do `Settings`. É o guarda contra o vocabulário se afastar de novo (§2.4).
- A raiz de dados sem `LOCALAPPDATA`.
- `locate()`, com os dublês do RFC-033: cada estado do `status()`.
- A engine: o listener preenche porta e senha. Um `ClusterNotRunningError` vira `OperationalError`. O `/health` responde 503, e não 500, com o cluster ausente.
- A senha: fora do `repr(settings)` e do relatório.
- O handler de rotação: um rename que falha não perde o registro e não toca nos backups. Usa um segundo processo de verdade segurando o arquivo, como o teste do lock do RFC-033.
- O nome do programa, para cada linha da tabela de §8.2.

**Sob o marcador `embedded`:** `locate()` contra um cluster de verdade; a engine depois de um reinício em outra porta; os clusters de desenvolvimento e de instalação de pé ao mesmo tempo.

> **Feito (§17.2).** Os testes da suíte padrão estão em `backend/tests/infrastructure/config/` (o layout, as fontes, o arquivo, os caminhos), `backend/tests/infrastructure/logging/` (o nome do programa, a rotação, o relatório), `backend/tests/infrastructure/persistence/test_engine_sources.py` (a engine nas duas fontes e o `/health` com o cluster ausente), `backend/tests/test_path_anchors_architecture.py` e `backend/tests/test_stage_installation.py`. O teste de arquitetura proíbe um pouco mais do que a lista acima: também `expanduser`, que é o diretório do usuário por outro nome, e a constante `"LOCALAPPDATA"`, que é o que §3 afirma em prosa. Os três testes `embedded` estão em `test_embedded_integration.py`, sobre as fixtures que o arquivo já tinha. O da porta mostrou a hipótese de §7.3 no nível da engine: o pool guarda uma conexão com a porta antiga, o cluster para e sobe noutra porta com a antiga ocupada, e a mesma engine, sem ser reconstruída, alcança a porta nova.

## 11. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Uma variável (`SOLIDVISION_ENVIRONMENT`) para o modo | É um segundo lugar, e volta a ser uma declaração (§4.2) |
| Detectar pelo `.git` | Um checkout exportado não o tem |
| Um manifesto de instalação marcando o modo instalado | Põe o default do lado errado: sem o manifesto, uma máquina de usuário viraria desenvolvimento. E o formato dele é do instalador |
| `sys.frozen` | Só existe num app congelado, que está fora do roadmap. E o modo instalado precisa ser testável antes |
| O `.env` no diretório de instalação | É só para leitura, e é por máquina, não por usuário |
| TOML ou JSON para o arquivo do usuário | Os dois também interpretam `\` dentro de aspas, e as duas sintaxes pedem aspas em todo texto. Seria um segundo parser e um segundo vocabulário para os mesmos campos |
| INI | Os valores são literais, e é o formato que o Windows reconhece. Mas pede uma fonte nova para o pydantic-settings e um segundo caminho para `supported_extensions`, e mantém as aspas no valor, que é outro engano à espera |
| O Registro (`HKCU`) | Não se edita no Bloco de Notas, não se manda por e-mail para o suporte e não tem equivalente no desenvolvimento |
| A configuração no banco | A localização do banco é configuração. E uma tela de preferências, quando existir, é do frontend (§12) |
| Um prefixo `SOLIDVISION_` para o ambiente, no modo instalado | Resolve a colisão de nomes de §2.3, e mantém aberto um canal que ninguém usa hoje. Fica como a porta para o RFC-036, se o launcher precisar falar com os filhos |
| `extra="forbid"` | Um arquivo opcional derrubaria o app por um erro de digitação. O aviso diz a mesma coisa sem derrubar (§5.4) |
| Subir com o default quando um valor é inválido | Grava onde o usuário disse que não queria (§5.5) |
| Recusar todo caminho relativo | Quebraria na importação todo `.env` copiado do `.env.example`, que traz `LOG_DIRECTORY=logs` |
| Expandir `%NOME%` nos valores | Seria uma segunda regra de expansão ao lado da do formato. Recusar com a forma certa na mensagem custa menos |
| `%APPDATA%`, o perfil móvel | O motivo do RFC-030 e do RFC-033: ele acompanha o usuário entre máquinas, e um cluster não pode |
| A dependência `platformdirs` | Uma dependência para uma chamada. E o default dela usa `<autor>\<app>`, que moveria as miniaturas e o cluster que já existem |
| Perguntar ao Windows antes de ler a variável | Tiraria da validação o jeito de apontar um processo para um diretório temporário (§6.2) |
| A mesma raiz de dados para os dois modos | É o que existe hoje (§2.5) |
| A raiz de dados de desenvolvimento dentro do checkout | Um checkout pode estar numa pasta sincronizada, e um cliente de sincronização dentro de um cluster ativo o corrompe ([RFC-033 §6](rfc-033-postgres-embarcado.md#6-o-diretório-do-usuário)) |
| Continuar com as `DATABASE_*` no ambiente, exportadas pelo launcher | A porta fica velha (§2.2), a senha viaja no bloco de ambiente de cada filho, e a armadilha do nome errado continua |
| Uma fonte do pydantic-settings que lê o adaptador na importação | Porta velha do mesmo jeito, a importação falha com o cluster parado, e a senha vira um campo do `Settings` |
| `creator=` em vez de `do_connect` | Equivalente. O `do_connect` deixa o dialeto continuar montando os parâmetros |
| Porta fixa, para a URL ser constante | O RFC-033 §8.2 já a recusou |
| Guardar a resposta do `locate()` | É o segundo ramo de §16, se o custo medido passar do critério |
| Um processo só escrevendo o log, e os outros mandando registros para ele | É o desenho mais limpo, e depende de quem é o dono dos processos: o launcher. Fica como opção do RFC-036 (§14) |
| O PID no nome do arquivo de log | Nunca divide arquivo, e produz um arquivo por execução, sem teto |
| Uma biblioteca de log com lock entre processos | Uma dependência nova. A rotação de §8.3 são poucas linhas sobre o handler que já existe |
| Só o arquivo por programa, sem mudar a rotação | Dois `job_runner` voltam a §2.6 |
| Só a rotação nova, com um arquivo para todos | Com três processos vivos, ninguém rotaciona nunca. O teto do RFC-005 deixaria de existir |

## 12. Não-objetivos

- **O instalador**, a desinstalação e a política sobre os dados do usuário.
- **Congelar o runtime.** O layout deixa uma função para adaptar (§4.1), e nada mais.
- **O launcher** (RFC-036): quem sobe o cluster, quem mostra um erro de configuração, quem reinicia um filho.
- **Os modelos** (RFC-035): onde ficam os pesos, o `HF_HUB_OFFLINE` e o cache do Hugging Face.
- **Uma tela de preferências.** O app não escreve no `solidvision.env`.
- **Um app instalado contra um banco externo.** Instalado, a fonte é sempre `embedded` (§7.1).
- **Mover dados existentes.** Nem os do desenvolvedor (§6.5), nem os de um usuário entre discos.
- **O formato do log**, o nível por módulo e o log estruturado, que são as limitações do RFC-005 §6. Também o nome duplicado `app.app.…` nos loggers, que o piloto viu e este RFC não toca.
- **Uma DACL própria para a raiz de dados** (§6.1).
- **Linux e macOS.**
- **Tirar o Docker do desenvolvimento.**

## 13. Riscos

| risco | situação |
| --- | --- |
| O instalador levar o `pyproject.toml` | O app se tomaria por desenvolvimento (§4.2). A instalação simulada não o copia, e um teste confere. O `/health` e o relatório de §5.6 mostram o modo |
| As bibliotecas mudarem o que o piloto leu (BOM, aspas, `${NOME}`, fontes) | Os testes de §10 fixam cada comportamento |
| Uma pasta criada por um processo elevado | **Hipótese:** a raiz de dados criada por uma primeira execução elevada continua utilizável sem elevação, porque herda a ACL do `%LOCALAPPDATA%`, que nomeia o usuário. O RFC-033 §19.3 deixou o equivalente do cluster sem exercitar. Manual, em §16 |
| A elevação com a conta de outra pessoa | A raiz de dados sai do perfil de quem roda o processo. Um app aberto pelo instalador com a credencial de um administrador grava no perfil do administrador. Passa para o instalador (§14) |
| Perfil redirecionado para a rede | Um cluster num compartilhamento de rede não é suportado. A saída é `EMBEDDED_POSTGRES_DIRECTORY`. Não é medido aqui |
| `ready` no arquivo com o servidor reinicializando | O `locate()` devolve uma conexão que é recusada por alguns segundos (§7.2). Para quem chama, é um erro de banco. Quanto tempo o `/health` fica em 503 nesse intervalo é `TBM` |
| O custo do `locate()` | `TBM`, com critério e segundo ramo declarados (§16) |
| Variáveis `PG*` além das três conhecidas | `TBM` (§7.5). Até a medição, a engine tem a mesma proteção do adaptador, e não mais |
| O log passar do teto | Só enquanto dois processos vivos dividem um arquivo (§8.3). Está declarado |
| O nome do programa num app congelado | Todo processo teria o nome do executável. A derivação está num lugar só, e o RFC-036 a herda |
| Uma falha que vira espera, e não erro | Medido com o `tempfile` (§2.1). Toda verificação de §16 tem prazo. Código novo que crie arquivos temporários precisa apontar para a raiz de dados |
| O desenvolvedor não notar a mudança dos defaults | O relatório de §5.6 mostra os diretórios. §6.5 vai para o `backend/README.md` |
| A suíte quebrar pelo caminho novo | Seria um achado. O `run -- pytest` passa a exercitar o `do_connect` em todos os testes (§7.6) |

> **Como cada risco ficou, depois da medição (§16).**
>
> - **O `pyproject.toml` na instalação:** `test_stage_installation.py` confere que a cópia nunca o leva, e que ela é `installed` onde a origem é `development`.
> - **A pasta criada por um processo elevado:** a hipótese se confirmou, à mão (§16). A raiz de dados e o cluster criados por um `start` elevado ficaram com o grupo Administradores como dono, e foram usados depois sem elevação pelos três programas.
> - **`ready` no arquivo com o servidor reinicializando:** medido. Com um backend morto à força e a API de pé, o `/health` respondeu 503 por 7,6 s, e 200 a partir de 7,8 s, sem nada reiniciado.
> - **O custo do `locate()`:** 2,54 ms na mediana, abaixo do critério. O segundo ramo não entra.
> - **As variáveis `PG*`:** eram o risco maior, e se confirmaram (§7.5). Depois da correção, nenhuma das 42 alcança o adaptador nem a engine.
> - **Uma falha que vira espera:** nenhuma das verificações estourou o prazo. O `torch` cria uma pasta no `%TEMP%` (§17.1), e é o tipo de código que este risco nomeia: o RFC-035 decide onde ele escreve.
> - **A suíte pelo caminho novo:** não quebrou. 2492 passed, a mesma contagem do `pytest` puro contra o Docker, com a prova do alvo dos dois lados.

## 14. O que passa adiante

**Para o RFC-035:**

- O modo vem de `layout.environment`. É ele que decide o `HF_HUB_OFFLINE`.
- `layout.root` é a raiz da instalação, e `layout.data_directory` é a do usuário. O RFC-035 decide em qual das duas os pesos moram.
- O cache do Hugging Face continua fora da raiz de dados até lá (§9).

**Para o RFC-036:**

- **Um filho pode morrer na importação por causa da configuração** (§5.5). A mensagem é uma linha no `stderr`. O launcher precisa capturá-la e mostrá-la, como precisa para qualquer filho que morra antes de logar.
- **Os filhos não precisam de nada pelo ambiente.** Um filho criado com um ambiente montado do zero ainda acha a raiz de dados (§6.2).
- **`DATABASE_SOURCE=embedded` no `.env` é o desenvolvimento sem Docker** (§7.1). O mesmo comando serve aos dois modos.
- **A API e o `job_runner` sobrevivem a um reinício do cluster em outra porta**, se §16 confirmar a hipótese de §7.3. O launcher não precisaria reiniciá-los junto com o banco.
- **O `/health` responde 503 em `TBM` ms com o cluster parado**, e não em minutos (§7.5). Serve de sonda.
- **O `/health` diz o modo.** Serve para reconhecer uma instância que já está rodando.
- **Cada programa tem o seu arquivo de log** (§8.2). Como o launcher inicia os filhos decide os nomes.
- **Dois processos do mesmo programa não rotacionam o log enquanto os dois vivem** (§8.3). Um escritor único, que seria o launcher, resolveria isso (§11).
- **Código que crie arquivo temporário num diretório só para leitura pode parar em vez de falhar** (§2.1).

> **Completado pela medição (§16, §17.1).**
>
> **Para o RFC-035:** o `torch` cria `%TEMP%\torchinductor_<usuário>` quando o `job_runner` carrega o modelo. É o segundo lugar fora da raiz de dados em que o runtime dos modelos escreve, ao lado de `~\.cache\huggingface`, e o RFC-035 decide os dois juntos.
>
> **Para o RFC-036:**
>
> - A hipótese de §7.3 se confirmou com a API de pé: o cluster parou, voltou noutra porta com a antiga ocupada, e a mesma API respondeu 200 na requisição seguinte. **O launcher não precisa reiniciar a API e o `job_runner` junto com o banco.**
> - Com o cluster parado, o `/health` responde 503 em 6 ms. Com a fonte `external` e o banco fora do ar, em 10,0 s, que é o `connect_timeout`.
> - O processo do launcher, ao chamar o adaptador, perde as variáveis `PG*` do próprio ambiente (§7.5), e os filhos que ele criar depois disso não as herdam.
>
> **Para quem escrever as instruções do suporte:** o Bloco de Notas grava `solidvision.env.txt` se o tipo "Documentos de texto" ficar escolhido (§5.3). A instrução precisa dizer para escolher "Todos os arquivos". O app avisa no log quando acha o `.txt`, mas o usuário não lê o log.
>
> **Para o instalador:** o custo de §9 não se separou do ruído deste HDD. Compilar os `.pyc` na instalação continua valendo, porque não custa nada, mas economiza no máximo alguns décimos de segundo por processo, numa subida de 15 s dominada pelo `torch`.

**Para o instalador:**

- Não levar `pyproject.toml` nem `.env`.
- Levar `backend\alembic\` e `alembic.ini`: as migrations são lidas do disco em toda subida.
- Compilar os `.pyc` na instalação, se §16 confirmar o custo de §9.
- Não criar `%LOCALAPPDATA%\SolidVision` nem o `solidvision.env`. A raiz de dados é de cada usuário, e é criada na primeira execução dele.
- Uma política de desinstalação tem **um** diretório por usuário a considerar.

## 15. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-034-configuracao-e-diretorios-do-usuario.md` | Este documento |
| `backend/app/infrastructure/config/layout.py` | O modo, a raiz, a raiz de dados e os caminhos derivados (§4) |
| `scripts/stage_installation.py` | A instalação simulada (§9) |
| `backend/tests/infrastructure/config/` | Os testes de §10 sobre o layout, as fontes, o arquivo e os caminhos |
| `backend/tests/test_path_anchors_architecture.py` | Nenhum `__file__` nem diretório de trabalho fora do layout (§10) |
| `experiments/rfc-034-user-configuration/measure_user_configuration.py` (+ `.log`, fora do git) | §16 |

O `pilot_configuration_probe.py` já existe na mesma pasta. É o que §2 cita.

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/infrastructure/config/settings.py` | `load_settings(layout)`, as fontes por modo, o validador de caminhos, `database_source`, `log_directory` como `Path`, `repr=False` na senha. Saem `indexing_root_path`, o campo `environment`, `database_url` e os três defaults com `os.environ` |
| `backend/app/infrastructure/logging/logger.py`, `handlers.py` | Sai a âncora própria. Entram o nome por programa, a rotação de §8.3 e o relatório de §5.6 |
| `backend/app/infrastructure/persistence/engine.py` | `build_engine()`, o listener, os parâmetros da libpq e o `connect_timeout` (§7) |
| `backend/alembic/env.py` | Usa `build_engine()` |
| `backend/app/infrastructure/database/embedded/` | `locate()` e `ClusterNotRunningError`. O `database_setup.py` pega os caminhos do Alembic do layout. O `run --` exporta as três variáveis novas (§7.6) |
| `backend/dataset_tools/seed_demo.py` | O docstring (§6.4) |
| `experiments/rfc-033-embedded-postgres/measure_adapter.py` | A sonda que prova o alvo lia `settings.database_port`, que deixa de carregar a porta do cluster. Passa a ler a porta do servidor pela engine |
| `.env.example` | Saem `ENVIRONMENT`, `LOG_DIRECTORY=logs` e `LOG_FILENAME`. Entra `DATABASE_SOURCE`, comentado |
| `.gitignore` | `/logs/` (§2.6) |
| `backend/README.md` | Os dois modos, a precedência, a árvore e §6.5 |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-standalone.md` | O status deste RFC |

Não mudam: `session.py`, os repositórios, as rotas (o `/health` continua lendo `settings.environment`), o `job_runner`, o `docker-compose.yml`, e nenhum arquivo do Domain ou da Application.

> **Entregue, com quatro arquivos a mais (§17).** `app/infrastructure/logging/program.py`, o nome do programa (§8.2). `backend/tests/infrastructure/logging/` e `backend/tests/infrastructure/persistence/test_engine_sources.py`, os testes da rotação, do nome e da engine. `backend/tests/test_stage_installation.py`, o teste da instalação simulada. E `measure_user_configuration.pg-variables-before-the-fix.log`, ao lado da medição, fora do git como os outros logs: as `PG*` contra o código de antes da correção de §7.5. Também mudaram `errors.py` (`ClusterNotRunningError`), `cluster_home.py`, `__init__.py` do adaptador, o `indexing_worker.py` (o texto de ajuda do `--root`) e o `measure_adapter.py` do RFC-033, que provava o alvo lendo `settings.database_port`.

## 16. Validação

Tudo `TBM`. A máquina e as condições são declaradas na medição. Os números do [RFC-033 §18](rfc-033-postgres-embarcado.md#18-validação) são de um NVMe. Se esta medição for feita no HDD do piloto, a linha de base do adaptador é medida de novo aqui antes de qualquer comparação, como o RFC-033 fez.

**Declarado antes da medição:**

- **Nenhuma parcela do RFC-033 §18 piora.** Este RFC não toca em `start()`, `stop()` nem `prepare()`.
- **O `locate()` custa menos de 10 ms na mediana.** É um décimo da conexão que ele precede: o RFC-033 mediu 0,135 s para abrir uma. Acima disso entra o segundo ramo: guardar a resposta, e ler de novo só depois que uma conexão falhar.
- **A contagem da suíte no head de partida** é medida antes de qualquer mudança. A de depois é ela mais os testes novos.
- **Toda verificação tem prazo** (§2.1).

> **Medido em 2026-10-14, na máquina do piloto.** Windows 10 Home (10.0.19045), Intel Family 6 Model 158, 4 threads, HDD SATA `ST1000DM010-2EP102`, Python 3.12.9, libpq 18.3 (a do `psycopg-binary` 3.3.4), página de código 1252, processo sem elevação, o Docker de pé com o contêiner `pgvector/pgvector:0.8.6-pg16`, nenhuma variável de configuração nem `PG*` no ambiente. O log da execução final é `measure_user_configuration.log`, ao lado do script, rodado com `--with suite,docker-suite,adapter-baseline`. A execução anterior a ela, completa, está em `measure_user_configuration.first-final-run.log`: deu os mesmos resultados, menos três falhas que não eram do código deste RFC, e que estão em §17.2.
>
> **A linha de base do adaptador neste HDD**, pelo script do RFC-033 (`measure_adapter.py --only first-run,warm-start,adoption`), dentro da mesma execução. É contra ela, e contra o script do RFC-033a rodado nesta máquina em 2026-10-08, que a primeira regra se confere: os números do RFC-033 §18 são de um NVMe.
>
> | parcela | script do RFC-033a, nesta máquina, 2026-10-08 | adaptador, execução final | adaptador, execução anterior, o mesmo código |
> | --- | --- | --- | --- |
> | `initdb` | 24,7 s e 29,3 s; a faixa do RFC-033a é de 24,7 a 38,9 s | 31,0 s | 37,1 s |
> | `listening → ready` na primeira subida | 0,216 s e 0,235 s | **0,429 s** | 0,226 s |
> | migrations até o head | 2,4 s e 2,5 s (`alembic upgrade head`, num subprocesso) | 1,19 s em processo, mais 0,77 s da comparação de heads | 1,08 s, mais 0,24 s |
>
> **O `initdb` e as migrations não pioraram. O `listening → ready` da execução final ficou acima da faixa, e não é deste RFC.** É o tempo do próprio servidor, entre duas linhas do log dele, e nenhum código deste RFC roda ali: o servidor recebe o mesmo ambiente de antes (`child_environment`), e a única mudança nas conexões do adaptador (§7.5) acontece depois, no login. Com o mesmo código, a execução anterior deu 0,226 s, dentro da faixa; nas dez subidas a quente da execução final a mediana foi 0,366 s (de 0,247 a 0,883 s). É a variação deste HDD, que §17.3 registra. A subida a quente inteira levou 0,909 s na mediana de dez (0,766 s no `pg_ctl`), a adoção 0,077 s e a parada 0,737 s; na execução anterior, 0,877 s, 0,046 s e 0,572 s.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte no head de partida | `pytest` puro, antes de qualquer mudança | **2254 passed, 79 deselected**, em 2026-10-13, no `03445f7`, com o Docker de pé. É a contagem do RFC-033 §18 |
| Primeira execução instalada | Instalação simulada só para leitura, diretório de trabalho vazio, `LOCALAPPDATA` temporário; `start`, depois a API e o `job_runner` | **`start` em 49,9 s**, do diretório vazio ao banco migrado (o `initdb` é quase tudo; 27,4 s na execução anterior). **A API respondeu o primeiro `/health` 22,7 s** depois de criada, e **o `job_runner` entrou no laço 37,0 s** depois, já com o modelo carregado (15,5 s e 16,9 s na execução anterior: o disco frio ou quente decide quase tudo). `app.__file__` estava debaixo da cópia, e `settings.environment` era `installed`. O arquivo de configuração esperado, `<dados>\solidvision.env`, não existia, e os três subiram com os defaults |
| "Grava tudo" | As quatro comparações de §9, antes e depois | **Só a raiz de dados, com uma exceção do RFC-035.** Foi criado `SolidVision\` com `logs\` (`embedded.log`, `uvicorn.log`, `job_runner.log`, `python.log`), `postgres\` (46,3 MB) e `thumbnails\`. O diretório de trabalho continuou vazio; a árvore instalada, com as mesmas 3174 entradas, tamanhos e datas; nada novo no primeiro nível de `%USERPROFILE%` e `%APPDATA%`. No `%TEMP%` dos filhos, que é só deles (§17.2), apareceu `torchinductor_<usuário>`, uma pasta vazia que o `torch` cria quando o `job_runner` carrega o modelo (§17.1) |
| A miniatura | `get_thumbnail_store()` na instalação simulada grava um arquivo | **Gravada em `<dados>\thumbnails\<2 dígitos>\<id>.jpg`**, e achada de volta pelo `locate()` do mesmo store |
| A raiz de verdade | Sem `LOCALAPPDATA` temporário, só a leitura dos caminhos | **`C:\Users\chapi\AppData\Local\SolidVision`**, com os três diretórios do usuário debaixo dela, o runtime debaixo da cópia e o arquivo em `…\SolidVision\solidvision.env`. O `%LOCALAPPDATA%\SolidVision` de verdade ficou com as mesmas entradas e datas: o processo só importou o `settings`, que não escreve |
| `/health` instalado | `GET /health` | **200, `"environment": "installed"`**, em 4,9 ms na mediana de vinte |
| `/health` com o cluster parado | O mesmo, depois de `stop` | **503 em 6,2 ms** na primeira requisição, que acha morta a conexão do pool, e em 4,4 ms nas cinco seguintes |
| `/health` com a fonte `external` e o banco parado | Docker parado, ou uma porta fechada | **503 em 10,1 s**, nas três requisições: o `connect_timeout`. Uma porta fechada, numa API do checkout com `DATABASE_SOURCE=external`. Antes, a tentativa passava de 25 s (§2.3) |
| Reinício em outra porta | API de pé; `stop`; a última porta ocupada por um socket; `start`; nova requisição | **Funciona sem reiniciar a API.** O cluster foi da porta 63960 para a 64773, e o `/health` seguinte, do mesmo processo da API, respondeu 200 em 72 ms. É a hipótese de §7.3 |
| `/health` enquanto o servidor se reinicializa (acrescentada, §17.2) | API de pé; um backend morto à força; `/health` a cada 0,1 s | **503 por 7,6 s** (42 requisições), e **200 a partir de 7,8 s**, do mesmo processo da API, sem nada reiniciado. É o intervalo que §13 deixava `TBM` |
| O custo do `locate()` | Mediana de 900 chamadas com o cluster de pé | **2,54 ms na mediana** de 900 chamadas, em três processos (p95 8,89 ms); a primeira chamada de um processo, 7,55 ms. Na execução anterior, 2,38 ms. **Abaixo dos 10 ms: o segundo ramo não entra** |
| Conexão nova, com e sem o listener | A mesma engine, com `NullPool` | **66,7 ms com o listener e 63,2 ms sem**, quarenta de cada, alternadas: 3,6 ms a mais, que é o `locate()` (2,7 ms na execução anterior) |
| A suíte pelo caminho novo | `run -- python -m pytest`; a porta lida do servidor pela engine igual à linha 4 do `postmaster.pid`; `xact_commit` dos dois lados | **2492 passed, 83 deselected**, em 154 s: a de partida mais 238 testes na suíte padrão e 4 sob `embedded`. Antes da suíte, um filho de `run --` com o `settings` e a engine do projeto leu a fonte `embedded`, a porta 65433 na linha 4 do `postmaster.pid`, e a engine alcançou um servidor que respondeu a mesma porta (`PostgreSQL 16.15, compiled by Visual C++`), por uma URL sem porta e sem senha. Durante a suíte: +333 commits e +270 rollbacks no cluster; no banco do `.env`, +25 commits e nenhum rollback, que é a assinatura da suíte |
| Docker continua funcionando | `pytest` puro | **2492 passed, 83 deselected, a mesma contagem**, em 157 s, contra o contêiner `pgvector/pgvector:0.8.6-pg16` |
| Ambiente hostil, instalado | `DEBUG=*`, `BATCH_SIZE=auto`, `LOG_LEVEL`, `ENVIRONMENT` e `DATABASE_HOST` no ambiente | **Sobe igual.** Com essas cinco, mais `DATABASE_SOURCE=external`, `DATABASE_PORT` e `THUMBNAIL_DIRECTORY=thumbs`: todo setting igual ao de um processo sem elas, e a engine alcançou a porta do cluster. O controle: as mesmas variáveis num processo do checkout o impedem de importar, numa linha que nomeia `DEBUG` e `BATCH_SIZE` |
| As variáveis `PG*` | Cada candidata de §7.5 com um valor hostil; uma escrita e a leitura de `Łódź/東京/✈.jpg` pela engine | **Antes da correção de §7.5: 14 alcançavam o login do adaptador e 15 a engine. Depois: nenhuma.** As 42 variáveis que a libpq 18.3 lê, e não só as oito candidatas. Sem proteção nenhuma, 18 mudam ou derrubam a conexão: `PGSERVICE`, `PGCHANNELBINDING`, `PGHOSTADDR`, `PGCLIENTENCODING`, `PGOPTIONS`, `PGSSLMODE`, `PGSSLNEGOTIATION`, `PGSSLCERTMODE`, `PGREQUIREAUTH`, `PGMINPROTOCOLVERSION`, `PGMAXPROTOCOLVERSION`, `PGSSLMAXPROTOCOLVERSION`, `PGGSSENCMODE`, `PGTARGETSESSIONATTRS`, `PGLOADBALANCEHOSTS`, `PGDATESTYLE`, `PGTZ` e `PGGEQO`. Os resultados de antes estão em `measure_user_configuration.pg-variables-before-the-fix.log` |
| O arquivo do usuário | Os casos de §2.4 no `solidvision.env` | **22 casos, todos como §5 e §6.3 dizem: 13 lidos, 9 recusados.** UTF-8 com e sem BOM dão `log_level = DEBUG` (o BOM não engole mais a primeira chave); UTF-16 e ANSI são recusados com "the file is not UTF-8 text" e o caminho do arquivo; os dois caminhos entre aspas duplas, o `%LOCALAPPDATA%`, `D:thumbs`, `\thumbs`, o valor vazio e `BATCH_SIZE=abc` são recusados numa linha que nomeia o arquivo e a chave; aspas simples, o espaço sem aspas, o comentário depois de `#`, `${LOCALAPPDATA}` e o caminho relativo dão o caminho certo; as chaves quase certas, as aposentadas e as do banco viram avisos |
| `solidvision.env.txt` | Um arquivo novo criado pelo Bloco de Notas (manual) | **A hipótese se confirmou**, à mão, em 2026-10-14: o nome digitado foi `solidvision.env`, o tipo ficou "Documentos de texto", e o arquivo gravado foi `solidvision.env.txt`, em UTF-8 sem BOM. O aviso também funciona: com o `.txt` e sem o arquivo certo, o processo subiu com os defaults e escreveu o aviso com o nome do arquivo e o que fazer |
| Sem `LOCALAPPDATA` | Um filho com o ambiente montado sem a variável | **A mesma raiz**, `C:\Users\chapi\AppData\Local`, perguntada ao Windows: `SolidVision` instalado, `SolidVision-dev` no checkout |
| Dois diretórios de trabalho | Os caminhos impressos por dois subprocessos | **Iguais**, nos dois modos: os quatro diretórios, a raiz de dados e o arquivo |
| Os `.pyc` | Importação da API na árvore só para leitura, com e sem a compilação prévia | **1,66 s e 1,63 s** de tempo próprio dos 137 módulos do projeto (`-X importtime`), na mediana de cinco depois de uma importação de aquecimento. **A diferença não se separou do ruído:** na execução anterior foram 1,61 s e 1,23 s. O processo inteiro, que importa o `torch`, leva 16,8 s e 17,6 s. Na árvore trancada, o Python não gravou nenhum `__pycache__`: a recompilação a cada subida acontece, e custa menos que a variação deste disco |
| Log, dois programas | A API e o `job_runner` de pé, e 300 requisições ao `/health` | **Dois arquivos, nenhum registro perdido.** 300 requisições ao `/health` gravaram 600 de 600 registros em `uvicorn.log`; o `job_runner` gravou 12 em `job_runner.log`, os da subida. Nenhum registro no arquivo do outro, nenhum erro de logging |
| Log, o mesmo programa duas vezes | O cenário do piloto, com o handler novo | **600 de 600, e nenhum backup perdido.** Ficaram 4 dos 5 backups antigos: o quinto foi empurrado por uma rotação que aconteceu, feita pelo processo quieto depois que o ocupado saiu (§17.2). O controle, o handler do RFC-005 no mesmo dia: 233 de 600 e 3 backups perdidos. No piloto, 219 e 1 |
| Log, um leitor | `Get-Content -Wait` durante a rotação | **600 de 600**, sem erro, duas rotações de verdade. No piloto, com o handler de antes, 600 de 600 |
| Os dois modos lado a lado | O cluster do checkout e o da instalação simulada de pé ao mesmo tempo | **Dois postmasters, duas portas, duas raízes** (`SolidVision\postgres` e `SolidVision-dev\postgres`, no mesmo perfil), cada um com o `postgres.exe` do próprio runtime. Um segundo `start` de cada lado adotou o seu, e parar o do checkout deixou o da instalação `ready` |
| Primeira execução elevada | `start` de um PowerShell como administrador; depois os três processos sem elevação (manual) | **Funciona: a hipótese de §13 se confirmou.** Feita à mão em 2026-10-14, depois da medição, sobre uma instalação simulada só para leitura e um `LOCALAPPDATA` temporário criado pelo usuário sem elevação. Um PowerShell como administrador rodou `start` e `stop`: criou a raiz de dados, o `embedded.log` e o cluster, com o grupo `Administradores` como dono da raiz. Depois, de um PowerShell comum: o `start` subiu esse mesmo cluster (`created: False`), sem acesso negado; a API respondeu `healthy` e `installed` e criou o `uvicorn.log`; o `job_runner` escreveu `Job executor started` no `job_runner.log`; e o `start` e o `stop` comuns escreveram no `embedded.log` que o processo elevado tinha criado. É também o caso que o RFC-033 §19.3 deixou sem exercitar: um cluster criado por um processo elevado e usado depois sem elevação |
| A senha não vaza | A senha procurada no `repr(settings)`, nos logs, no relatório e no ambiente do filho de `run --` | **Zero ocorrências.** Nem no `repr(settings)`, nem no relatório, nem no ambiente de um filho de `run --` (que recebeu as três variáveis de §7.6, nenhuma credencial), nem nos 9587 arquivos de log e de dados do perfil, nem na saída dos 212 processos filhos da medição |
| Arquitetura | O teste das âncoras | **Passa**: 29 testes, o das âncoras e o da instalação simulada |
| Estilo e tipos | `black --check`, `ruff check`, `mypy` | **Limpos.** `black --check` e `ruff check` sobre `backend/app`, `backend/tests`, `backend/dataset_tools`, o `stage_installation.py` e o script da medição; `mypy` com a configuração do projeto, 291 arquivos |

## 17. Correções feitas durante a implementação

Registradas à vista do texto original, como o RFC-033 fez com as suas. Nenhuma muda uma decisão de §3. Duas mudaram **código** que o documento descrevia de outro jeito, e vêm primeiro.

### 17.1 O que a medição contrariou

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §7.5 | *"A engine passa os parâmetros que o adaptador já passa nas conexões dele […]. O que a medição achar entra na constante"* | **A constante não bastava, e o adaptador do RFC-033 também não estava protegido.** Das 42 variáveis que a libpq 18.3 lê, 18 mudam ou derrubam uma conexão que só recebe host, porta, usuário, senha e banco. Com os três parâmetros do RFC-033, ainda 14 derrubavam o login do adaptador e 15 a engine. Quatro não têm parâmetro que as anule (`PGTZ`, `PGDATESTYLE`, `PGGEQO`, `PGSERVICE`). Toda conexão com o cluster passou a tirar as `PG*` do ambiente do processo antes de conectar, e a constante ficou como estava. É a única mudança nas conexões do adaptador, e o `start()`, o `stop()` e o `prepare()` não mudaram fora dela |
| §9, "grava tudo" | Duas coisas ficam de fora do "tudo": *"O cache dos modelos, que é do RFC-035"* e a miniatura de ponta a ponta | **Uma terceira, do mesmo dono.** Quando o `job_runner` carrega o modelo, o `torch` cria `%TEMP%\torchinductor_<usuário>`, uma pasta vazia. Fora da raiz de dados, e fora do `%LOCALAPPDATA%\SolidVision` que o critério do roadmap nomeia. Passa para o RFC-035 (§14) |
| §8.3 | O intervalo da nova tentativa, `TBM` | **1 s.** A implementação usou 5 s até a medição |
| §9, os `.pyc` | *"Numa árvore só para leitura, o Python não consegue gravar os `.pyc` e recompila os módulos do projeto a cada subida. […] O número vai para o instalador"* | **Não grava, e o número não saiu.** A diferença entre importar com e sem os `.pyc` ficou entre −0,23 e +0,39 s em três rodadas, dentro da variação do disco. O que vai para o instalador é que compilar não custa nada e economiza, no máximo, alguns décimos de segundo por processo (§14) |

### 17.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §4.1 | **Como o `Settings` conhece o layout.** Um atributo de classe, `_layout`. Na classe base ele é o layout detectado, e por isso `Settings()` sem argumentos continua significando "recém-carregado para o código que está rodando". Para qualquer outro layout, `load_settings(layout)` cria uma subclasse com o `_layout` e o `env_file` dele, guardada por layout (`functools.cache`), para que duas cargas do mesmo layout sejam comparáveis |
| §6.3 | **Os defaults dos quatro caminhos vêm de um lugar só** (`PATH_DEFAULTS`) e chegam por dois caminhos: uma fonte do pydantic-settings, a mais fraca de todas, com os defaults do layout que está sendo carregado; e um `default_factory` em cada campo, com os do layout detectado. O segundo existe por causa do `mypy`, que trataria um campo sem default como argumento obrigatório de `Settings()`. Em execução, a fonte sempre responde antes |
| §6.3 | **O valor vazio é recusado**, antes das cinco regras |
| §5.4 | Instalado, as chaves do banco são descartadas **antes** da validação: `DATABASE_PORT=abc` no arquivo do usuário é aviso, e não recusa |
| §5.5 | `ConfigurationError`, convertida em `SystemExit` pelo singleton do módulo: uma linha no `stderr`, código 1, sem traceback. Um teste roda um processo de verdade |
| §5.6 | O relatório vai também para o console: oito linhas no `stderr` de todo processo, mais uma por aviso |
| §5.3 | O aviso do `solidvision.env.txt` foi implementado antes de a hipótese do Bloco de Notas ser conferida: ele só aparece quando o `.txt` existe e o arquivo certo não, e seria inofensivo se a hipótese fosse falsa. Ela se confirmou depois, à mão |
| §7.2 | Um cluster `ready` sem o arquivo `password` é `ClusterRefusedError`, a mesma exceção e o mesmo texto do `start()`; um runtime sem manifesto é `RuntimeInvalidError`, como no `status()` |
| §7.3 | O listener converte qualquer erro do adaptador em erro de conexão. O adaptador é construído na primeira conexão |
| §7.5 | `discard_libpq_environment()` mexe em `os.environ` fora de `config/`, que o RFC-004 reserva para a leitura da configuração. Não é configuração do app: é o ambiente da libpq, e o `child_environment` do RFC-033 já o tratava no mesmo pacote. Os nomes retirados vão para o log, nunca os valores, porque `PGPASSWORD` é um deles |
| §7.6 | `ENVIRONMENT_VARIABLES` foi para `cluster.py`, e `EmbeddedPostgres.environment()` monta o dicionário. `ClusterConnection.environment()` saiu |
| §8.2 | O nome do programa sai de `sys.orig_argv`. Um script dá o nome do script |
| §8.3 | A rotação passa por um nome intermediário, `<arquivo>.rotating` |
| §9 | `--unlock` e `--remove`, um arquivo de marca na raiz da cópia, e a conferência da tranca por um processo filho com prazo |
| §10 | O teste de arquitetura proíbe também `expanduser` e a constante `"LOCALAPPDATA"`. Um teste a mais sob `embedded`, para §7.5 contra um servidor de verdade |
| §16, a primeira execução final | **Três falhas, nenhuma do código deste RFC, e a execução foi refeita inteira.** As duas suítes deram 4 failed, nos mesmos quatro testes de `test_layout.py`, da sessão 1: eles tomavam `os.environ["LOCALAPPDATA"]` como a resposta do Windows, e a medição roda os filhos com essa variável apontada para um diretório temporário, que é o que §6.2 manda fazer numa validação. O oráculo passou a ser o Windows, por uma API diferente da que o código usa (`SHGetFolderPathW`). A terceira foi o `ruff`, sobre duas linhas longas de uma verificação acrescentada ao script enquanto ele rodava: a do `/health` durante a reinicialização do servidor |
| §16, o método | **O `%TEMP%` dos filhos é um diretório só deles**, vazio no começo. O de verdade é dividido com todos os programas da máquina, e uma entrada nova nele não diria de quem é. É assim que o `torchinductor` de §17.1 apareceu com dono |
| §16, o arquivo do usuário | A medição rodou 22 casos, e não só os de §2.4: também as chaves aposentadas, as do banco, um valor que não valida, o `.txt` e o valor vazio. O caso do valor vazio rodou primeiro contra uma cópia do código anterior à correção, e as miniaturas foram para a raiz de dados, soltas |
| §16, as `PG*` | As 42 variáveis da libpq, e não as oito candidatas; e três conexões em vez de duas, porque o login do adaptador também é uma |
| §16, uma linha a mais | **O `/health` enquanto o servidor se reinicializa**, que §13 deixava `TBM` e nenhuma linha de §16 media: um backend morto à força com a API de pé, e quanto tempo o `/health` responde outra coisa que 200 |
| §16, a rotação | O cenário do mesmo programa duas vezes terminou com 4 dos 5 backups antigos, e não 5. O quinto foi empurrado por uma rotação que **aconteceu**, feita pelo processo quieto depois que o ocupado saiu, que é o comportamento declarado em §8.3. O critério do script conta backups perdidos, e não backups presentes |

### 17.3 O que ficou sem medição

Dito aqui para não parecer medido em outro lugar.

- **A primeira execução elevada com a conta de outra pessoa** (§13). A verificação de §16 apontou o `LOCALAPPDATA` dos dois shells para o mesmo diretório, e por isso não diz nada sobre qual perfil um processo elevado com a credencial de outro administrador usaria. Esse risco continua com o instalador (§14).
- **Um `C:\Program Files` de verdade.** A cópia é trancada com a mesma forma de DACL (o usuário com leitura e execução), mas não tem o `TrustedInstaller` como dono, nem a virtualização de arquivos que o Windows aplica a programas antigos.
- **O `job_runner` gravando muitos registros.** Sem um job, ele só escreve os da subida. O que a linha "dois programas" prova é que os dois arquivos são separados; a rotação sob carga está na linha "o mesmo programa duas vezes".
- **Tempos firmes neste HDD.** As duas execuções completas, com o mesmo código, variaram quase o dobro em tudo que lê o disco: a primeira execução instalada (27,4 s e 49,9 s), o `listening → ready` da primeira subida (0,226 s e 0,429 s), a importação do projeto com e sem `.pyc`. Os resultados de sim ou não (o que foi gravado e onde, o que subiu, o que foi recusado, o que vazou) foram os mesmos nas duas. Os tempos estão em §16 com as duas execuções, e não devem ser lidos com mais de uma casa de precisão.
