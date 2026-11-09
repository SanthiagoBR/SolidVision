# RFC-048 — As Conferências num Comando Só, com as Versões Fixadas

**Status:** Implementado, como está, com as correções de §17. A medição de §16, nesta máquina, num clone novo com `core.autocrlf=true`, sem `.env` e sem `runtime\models`, em 08/11/2026: o venv do `make_venv.py`, só dos dois lockfiles e no Python 3.12.9, em **390,3 s** com o cache do `pip` frio; o comando inteiro **passa**, as nove etapas, em **524,4 s** com os caches frios e **241,7 s** com eles quentes, dentro do `embedded run --` (o `mypy` frio é 230,8 s e a suíte, 203,7 s, com 3 540 testes passando); o `mypy` com **zero erros**; cada um dos **nove defeitos plantados** fez a etapa dele ser a primeira vermelha; e, com o banco parado, o `pytest` falha na conexão de teste em **3,8 s**, sem começar a suíte. O `frontend/` sai LF de um checkout com `autocrlf=true`. A medição achou dois defeitos: o lockfile das ferramentas apagava o `--no-binary langdetect` do outro (§17.1), e 13 testes da suíte padrão só passavam com `runtime\models` (§17.11). Montar de novo o venv desta máquina não mudou nenhum vetor, bit a bit (§17.7). As seções 1 a 15 são o rascunho como foi aprovado
**Depende de:** RFC-003 (as ferramentas: Ruff, Black, `mypy --strict` e pytest), RFC-033 §11 (o `embedded run --`), RFC-037 §4.2 e §4.3 (os comandos do frontend e a deriva do OpenAPI conferida pelos dois lados), RFC-041 §5.2 e §13 (o lockfile do Python, o `vendor_python.py --lock` e a regra de mudar o venv e o lockfile juntos), [roadmap de CI/CD](ROADMAP-ci-cd.md) (D5 e D6)
**Bloqueia:** RFC-049 e RFC-050, que chamam este comando em vez de repetir a lista em YAML; RFC-052, que monta o venv do mesmo jeito
**Migration:** nenhuma
**Medição:** o piloto de §2, rodado à mão em 08/11/2026 nesta máquina (NVMe, Windows 11, o `.venv` no Python 3.12.5), sobre o commit `21e0af5`. A da implementação, `experiments/rfc-048-conferencias/measure_check.py` (+ `.json`; o `.log`, fora do git), num clone novo (§10), rodada em 08/11/2026 (§16), e o `vectors_by_venv.py` (+ `.json`), que comparou os vetores dos dois venvs desta máquina (§17.7)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*, e vira número quando a implementação o escrever de volta, ao lado da frase. Os números sem marca vêm do piloto de §2 ou de outros RFCs, e dizem de qual.

---

## 1. Contexto

O projeto tem oito conferências que valem a cada mudança, quatro do backend e quatro do frontend, e nenhum jeito único de rodá-las. O README as lista em cinco linhas, e o resultado depende da máquina:

- `ruff check backend` **corrige** em vez de conferir, porque o `pyproject.toml` tem `fix = true`.
- `black --check backend` não roda no venv desta máquina, porque o black se recusa a rodar no Python 3.12.5.
- `pip install -r backend/requirements.txt` instala a versão mais nova de cada pacote no dia, porque o arquivo não fixa nenhuma. O lockfile que vai no instalador ([RFC-041 §5.2](rfc-041-empacotamento-e-instalador.md)) diz uma coisa, e o venv de cada máquina diz outra.
- O `pytest` precisa de um banco rodando, e o README não diz o que acontece sem ele.

O roadmap de CI/CD começa aqui porque um runner só pode chamar o que um desenvolvedor já roda, do mesmo jeito. Um CI que repete a lista em YAML confere uma coisa que ninguém roda na própria máquina, e as duas listas se separam na primeira mudança. Este RFC não toca no GitHub. Ele entrega um comando, os lockfiles que fixam o que esse comando usa e o jeito de montar um venv só a partir deles.

## 2. O que o piloto mediu

Todas as conferências rodaram nesta máquina em 08/11/2026, no checkout de todo dia, com o `.venv` do repositório e o `.env` dela. Um venv descartável, montado só do lockfile, rodou o `mypy` de novo (§2.5). O roadmap partiu da memória do projeto, e o piloto corrigiu quatro premissas dele (§15).

### 2.1 O venv saiu do lockfile em 36 dos 57 pacotes, e não em dois

O roadmap falava do SQLAlchemy (2.1.1 contra 2.0.51) e do `torch` (2.14 contra 2.13). Comparado pacote por pacote com o `scripts/python-win-64.lock`:

| | pacotes |
|---|---|
| No lockfile | 57 |
| Com outra versão no venv | 35. Entre eles, `transformers` 5.17.0 contra 5.15.0, `fastapi` 0.141.1 contra 0.139.2, `numpy` 2.5.3 contra 2.5.2 e `torchvision` 0.29.0+cpu contra 0.28.0 |
| Ausentes do venv | 1, o `greenlet`, que o SQLAlchemy 2.1 deixou de pedir |

Hoje, só duas coisas mostram essa diferença: o `test_every_version_is_the_venvs`, que falha com a lista inteira, e o `mypy`, que dá 8 erros que a outra máquina não dá. **O venv de cada máquina tem as versões que o `pip` resolveu no dia em que ele foi montado.**

### 2.2 Os erros do ruff e do black estão todos em `experiments/`

| conferência | `backend/` e `scripts/` | `experiments/` |
|---|---|---|
| `ruff check --no-fix` | 0 | 279 em 7 pastas: 272 `E501`, 6 `I001` e 1 `UP031` |
| `black --check` (26.5.1, num venv descartável do Python 3.14) | 0 em 394 arquivos | 13 arquivos |

Os scripts de `experiments/` são o registro de medições que já rodaram, e os RFCs apontam para eles como estão. O escopo do comando é `backend/` e `scripts/`, que já passam (§4.1).

### 2.3 O `format:check` reprova hoje 11 arquivos, e os 11 são CRLF

No checkout desta máquina (`core.autocrlf=true`), 14 arquivos de `frontend/` estão CRLF na cópia de trabalho e LF no índice. O Prettier reprova os 11 que ele confere. Os outros três são o `.prettierignore`, o `build/installer.nsh` e o `package-lock.json`, que ele não confere. O `frontend/.gitattributes` que resolvia isso foi criado em 21/10, nunca foi commitado e não existe mais nesta cópia. Todo arquivo que um `checkout` ou um `stash pop` reescreve volta CRLF.

### 2.4 Sem banco, a suíte leva 5 minutos para dar 331 erros

O `.env` desta máquina diz `DATABASE_SOURCE=embedded`, e o cluster de desenvolvimento estava parado:

| | |
|---|---|
| Passaram | 3 159 |
| Erros | 331, todos `The embedded cluster ... is not ready to be connected to: stopped` |
| Falharam | 6 |
| Fora da seleção (`slow` e `embedded`) | 686 |
| Tempo | 5 min 05 s |

Das 6 falhas, duas são do banco parado, duas são o lockfile de §2.1 (`test_vendor_python.py`) e duas são o `WARM_UP_MODELS=true` do `.env`. Uma é o `test_resolving_the_embedding_model_downloads_nothing`, que o README já conhece. A outra é o `test_health_answers_503_and_not_500_when_the_cluster_is_absent`, que espera `search: on_demand` e recebe `loading`. **São dois testes, e não um**, como dizem o README e o roadmap.

Um clone novo não tem `.env`, então vai para o `localhost:5432` do Docker (`DATABASE_SOURCE=external`, o default). Sem o Docker rodando, o resultado é o mesmo. E todo checkout desta máquina usa o mesmo cluster embarcado, `%LOCALAPPDATA%\SolidVision-dev\postgres` (`layout.py`). Um clone novo que suba o cluster pelo default sobe o de desenvolvimento, e o `start` aplica nele as migrations do clone.

### 2.5 Um venv montado só do lockfile

Num venv novo do Python 3.12.5 desta máquina, com o `pip` 24.2 que vem com ele, numa pasta descartável:

| tentativa | resultado |
|---|---|
| O lockfile inteiro numa chamada, com `--no-deps --require-hashes --no-build-isolation`, como o `vendor_python.py` instala | Falha em 11 s: `No module named 'setuptools'`. O `langdetect` 1.0.9 não tem roda, o sdist dele é construído sem isolamento, e um venv do Python 3.12 nasce só com o `pip` |
| A linha do `setuptools` do lockfile primeiro, sozinha e com o hash, e depois o lockfile inteiro | Passa, em 317 s com o cache do `pip` vazio. Os 57 pacotes ficam na versão do lockfile, e não há nenhum outro além do `pip` |

O `vendor_python.py` nunca viu isso porque instala com o `pip` do venv de desenvolvimento, que já tem um `setuptools`. Com o `mypy` e o `pytest` instalados por cima, nas versões do venv de hoje, o mesmo venv deu o resultado de §8.

### 2.6 O `pytest-mock` não é usado por nenhum teste

O roadmap pôs o `pytest-mock` no lockfile das ferramentas. Nenhum teste usa o `mocker`, e o pacote nem está no venv. Ele vem do `requirements.txt` da raiz, que é do primeiro commit (`9c58be8`), nunca mudou e não é lido por nada. O README e o `vendor_python.py` usam o `backend/requirements.txt`.

### 2.7 As ferramentas dividem cinco pacotes com o runtime

As cinco ferramentas (`pytest`, `pytest-cov`, `black`, `ruff` e `mypy`) puxam, no venv, 19 pacotes. Cinco deles já estão no lockfile do runtime: `click`, `colorama`, `packaging`, `pygments` e `typing-extensions`. Os outros 14 são só de desenvolvimento. Se o lockfile das ferramentas repetisse os cinco, poderia dar outra versão a eles, e um `pip install --no-deps` dos dois arquivos instalaria a que viesse por último, sem erro.

Fixar só as cinco ferramentas não basta. No piloto de §2.5, o `pip` instalou o `mypy` 2.3.1 e o `pytest` 9.1.1, as versões do venv, e puxou seis dependências em versões que o venv não tem: `ast-serialize` 0.13.1 (0.11.2 no venv), `librt` 0.16.0 (0.15.0), `mypy-extensions` 1.2.0 (1.1.0), `pluggy` 1.7.0 (1.6.0), `iniconfig` 2.3.1 (2.3.0) e `coverage` 7.17.2 (7.16.2). O lockfile tem de fixar o fecho inteiro.

### 2.8 Os tempos de hoje

Nesta máquina, com os caches quentes. As etapas do frontend são de duas rodadas com a máquina livre:

| etapa | tempo |
|---|---|
| `ruff check --no-fix backend scripts` | 1,5 s |
| `black --check backend scripts` (Python 3.14) | 7,7 s |
| `mypy` (385 arquivos) | 7,6 s. Com o cache vazio, no venv do piloto: **370 s** |
| `npm run lint` | 19 a 20 s |
| `npm run typecheck` | 10 a 11 s |
| `npm run format:check` | 5,7 s |
| `npm test` (378 testes em 36 arquivos) | 10 a 12 s |
| `pytest` | 5 min 05 s, sem banco (§2.4). Com banco, 3 a 6 min (memória do projeto) |

Com os caches quentes, a análise estática inteira leva pouco mais de um minuto. Com o cache vazio, só o `mypy` leva seis, e a suíte leva vários.

## 3. Decisões

1. **Um comando, `python scripts/check.py`**, que roda as conferências numa ordem fixa e para na primeira falha. Com `--keep-going`, roda todas e resume. O CI chama este comando (RFC-049 e RFC-050) e não repete a lista.
2. **As etapas têm nome, e há grupos**, para o CI dividir as conferências em jobs sem escrever os comandos de novo.
3. **Uma conferência do ambiente vem antes de tudo**: o venv contra os dois lockfiles, versão por versão e sem rede, o Python 3.12.9 e o Node do `.node-version`. Ela confere só o que as etapas escolhidas usam.
4. **O escopo do Python é `backend/` e `scripts/`.** `experiments/` fica fora do ruff e do black (§2.2). O `mypy` continua com os `files` do `pyproject.toml`.
5. **O pytest vem por último, depois de uma conexão de teste com o banco**, e com `WARM_UP_MODELS=false` no ambiente dele. O comando não sobe banco nenhum.
6. **Um lockfile das ferramentas, `scripts/python-dev-win-64.lock`**, escrito pelo mesmo `vendor_python.py --lock`, só com os pacotes que o lockfile do runtime não tem.
7. **Um venv montado só dos dois lockfiles, por `scripts/make_venv.py`**, no Python 3.12.9 e sem resolver nada.
8. **O `frontend/.gitattributes` é commitado**, com `* text=auto eol=lf`.
9. **Os 8 erros do `mypy` somem com a versão do lockfile, e o erro que ela mostra é corrigido no código** (§8). Um erro que só uma versão mais nova do SQLAlchemy mostrar fica para quem atualizar o lockfile, no mesmo commit.
10. **O `requirements.txt` da raiz sai** (§2.6).

## 4. O comando

### 4.1 As etapas

| etapa | comando | pasta | grupo |
|---|---|---|---|
| `env` | interno (§4.3) | — | sempre |
| `ruff` | `python -m ruff check --no-fix backend scripts` | raiz | `python` |
| `black` | `python -m black --check backend scripts` | raiz | `python` |
| `mypy` | `python -m mypy` | raiz | `python` |
| `eslint` | `npm run lint` | `frontend/` | `frontend` |
| `tsc` | `npm run typecheck` | `frontend/` | `frontend` |
| `prettier` | `npm run format:check` | `frontend/` | `frontend` |
| `vitest` | `npm test` | `frontend/` | `frontend` |
| `pytest` | `python -m pytest` (§4.4) | raiz | — |

O `python` da tabela é o interpretador que roda o `check.py` (`sys.executable`). Nenhuma etapa depende de um venv ativado nem do `PATH` do Python. O `npm` é o que o `shutil.which` acha, que no Windows é o `npm.cmd`.

Sem argumentos, o comando roda tudo. Com nomes, roda só as etapas pedidas, sempre na ordem da tabela, em qualquer ordem que elas sejam digitadas:

```powershell
python scripts/check.py                  # tudo
python scripts/check.py python           # ruff, black e mypy
python scripts/check.py frontend         # eslint, tsc, prettier e vitest
python scripts/check.py mypy pytest      # duas etapas
python scripts/check.py --keep-going     # tudo, sem parar na primeira falha
```

Um nome desconhecido é recusado antes de qualquer etapa rodar.

### 4.2 A ordem

As etapas rápidas vêm primeiro. A análise estática leva segundos, e o pytest leva minutos (§2.8). Assim, um erro de tipo aparece em 10 s, e não depois da suíte inteira. O roadmap listava o pytest antes do frontend. Ele passa para o fim por essa razão, e porque é a única etapa que precisa de banco.

### 4.3 O ambiente

A etapa `env` roda sempre primeiro, e confere só o que as etapas escolhidas usam:

- **Se alguma etapa do Python foi escolhida:** o `check.py` roda num venv, o Python é o 3.12.9 (D5), e cada pacote dos dois lockfiles está instalado na versão do lockfile. Uma versão diferente ou um pacote ausente reprova, com o nome e as duas versões. Um pacote que não está em nenhum lockfile passa, e é listado numa linha. Um pacote a mais não muda a versão dos outros, e um venv de desenvolvimento pode ter, por exemplo, o `psutil` do `benchmark_indexing.py`.
- **Se alguma etapa do frontend foi escolhida:** a versão maior do `node` é a do `frontend/.node-version` (24), e `frontend/node_modules` existe. Se não existir, a mensagem manda rodar `npm ci`.

A comparação lê os metadados instalados (`importlib.metadata`), sem rede. É a mesma que o `make_venv.py` faz no fim e que o `test_every_version_is_the_venvs` faz para o lockfile do runtime, numa função só.

A parte do frontend roda com qualquer Python 3 e sem venv. O job do frontend do RFC-049 é Linux e não monta o venv.

### 4.4 O pytest e o banco

Antes do pytest, o `check.py` abre uma conexão com o banco que a configuração aponta, pelo engine do app. Se ela falha, a etapa falha ali, em segundos e sem rodar a suíte (§2.4: sem essa conexão, foram 5 min 05 s até os 331 erros), e a mensagem dá os dois caminhos:

```
pytest: o banco da configuração não responde (DATABASE_SOURCE=embedded).
  Suba o Docker (docker compose up -d), ou rode as conferências dentro do cluster embarcado,
  com backend no PYTHONPATH:
  python -m app.infrastructure.database.embedded run -- python scripts/check.py
```

O comando não sobe banco nenhum. O `embedded run --` já existe ([RFC-033 §11](rfc-033-postgres-embarcado.md)): ele sobe o cluster, roda o comando e deixa o cluster como o achou. É por ele que o RFC-050 vai chamar o `check.py` no runner. Se o `check.py` subisse o cluster por conta própria, subiria o de desenvolvimento, que todo checkout da máquina divide e cujo `start` aplica as migrations (§2.4).

O pytest recebe `WARM_UP_MODELS=false` no ambiente, que o `pydantic-settings` põe acima do `.env` (RFC-004). É a instrução que o README já dá, agora feita pelo comando, e cobre os dois testes de §2.4. O defeito dos dois testes, que dependem do `.env` de quem os roda, fica como está (§14).

### 4.5 A saída

Cada etapa imprime uma linha com o comando antes, a saída da ferramenta como ela é, e uma linha com o resultado e o tempo depois. No fim vem um resumo com cada etapa, o resultado e os segundos. O código de saída é 0 se tudo passou, 1 se alguma etapa falhou e 2 para um erro de uso.

## 5. O lockfile das ferramentas

`scripts/python-dev-win-64.lock` tem o formato do `python-win-64.lock`: um pacote por linha, `===`, o `sha256` e o mesmo cabeçalho de índices. Ele é escrito pelo `python scripts/vendor_python.py --lock`, que passa a escrever os dois arquivos na mesma execução:

- **O do runtime não muda.** Continua sendo a primeira parte do `backend/requirements.txt`, mais o `torchvision`.
- **O das ferramentas** vem da segunda e da terceira partes do mesmo arquivo (`pytest`, `pytest-cov`, `black`, `ruff` e `mypy`), resolvidas com cada pacote preso à versão do venv, como no do runtime. Dele sai todo pacote que já está no do runtime (§2.7), e os cinco compartilhados ficam num lugar só, na versão do runtime. Com o venv de hoje, são 14 pacotes.

Assim, instalar os dois juntos não resolve nada, e nenhum pacote aparece com duas versões. O `pip` continua fora dos dois. O lockfile do runtime continua sem nenhuma ferramenta (`test_no_tool_is_in_it`), porque é ele que vai para dentro do instalador.

Mudar uma ferramenta segue a regra que o [RFC-041 §13](rfc-041-empacotamento-e-instalador.md) deu para os pacotes do runtime: muda-se o venv, roda-se o `--lock`, e os dois arquivos mudam no mesmo commit.

## 6. O venv a partir dos lockfiles

`scripts/make_venv.py` roda com o Python que vai ser o do venv, chamado pelo caminho:

```powershell
C:\<pasta do Python 3.12.9>\python.exe scripts/make_venv.py           # cria .venv
C:\<pasta do Python 3.12.9>\python.exe scripts/make_venv.py D:\outro  # em outro lugar
```

1. Recusa um Python que não seja o 3.12.9 (D5) e uma pasta que já exista. Ele nunca apaga nem mistura um venv.
2. Cria o venv com o `pip` que vem com o Python (o `ensurepip`), sem atualizá-lo.
3. Instala o `setuptools` do lockfile do runtime, sozinho e com o hash. Sem ele, o sdist do `langdetect` não é construído (§2.5).
4. Instala os dois lockfiles numa chamada, com `--no-deps --require-hashes --no-build-isolation`, como o `vendor_python.py` instala o do runtime.
5. Confere o venv contra os dois lockfiles, com a função da etapa `env`, e reprova se algo divergir.

O README troca o `pip install --upgrade pip` e o `pip install -r backend/requirements.txt` por este comando. O `backend/requirements.txt` continua: é a lista dos nomes que o `--lock` resolve.

Nesta máquina, isso pede o Python 3.12.9 instalado ao lado do 3.12.5 de `C:\Python312`, e é também o que faz o black rodar no venv do projeto de novo.

## 7. O `frontend/.gitattributes`

O arquivo tem uma linha, `* text=auto eol=lf`, e é commitado. Com ele, um checkout com `core.autocrlf=true`, que é o padrão do Git no Windows e o dos runners Windows, escreve LF em `frontend/`. Os 14 arquivos CRLF desta cópia (§2.3) são reescritos por um `git checkout` deles depois do commit. Nenhum tem mudança local.

O backend não precisa do mesmo arquivo. Ele já está CRLF nesta cópia, e o ruff, o black e a suíte passam assim (§2.2 e §2.4). Do lado do Node, o teste da deriva já ignora o CRLF do `schema.d.ts` (`schema-types.test.mjs`).

## 8. Os erros do `mypy`

No piloto (§2.5), com o venv montado só do lockfile do runtime e o `mypy` e o `pytest` nas versões do venv de hoje:

| | |
|---|---|
| Erros no `.venv` desta máquina (SQLAlchemy 2.1.1) | 8, em 5 arquivos |
| Erros no venv do lockfile (SQLAlchemy 2.0.51) | 1 |
| Dos 8, os que somem só pela versão | os 8 |

O erro que aparece é o mesmo defeito visto pelo outro lado. O `test_restart_index_migration.py:146`, escrito nesta máquina no RFC-046, compara o `.all()` de um `execute()` (`Sequence[Row[Any]]`) com uma lista de tuplas, e o `mypy` diz que os dois tipos não se cruzam (`comparison-overlap`). O SQLAlchemy 2.1 tipa a linha de outro jeito, e o `mypy` desta máquina não viu. O código passou nas conferências de uma máquina e reprovaria nas da outra.

A decisão 9, então: os 8 erros somem com a versão, sem mudança no código, e o erro novo é corrigido no teste, que passa a comparar as linhas convertidas em tuplas. O roadmap deixava em aberto prender a versão do SQLAlchemy. O lockfile já a prende, e o `mypy` passa a conferir a versão que vai para o usuário.

## 9. Alternativas consideradas

| alternativa | por que não |
|---|---|
| A lista de comandos no YAML do RFC-049 | O CI conferiria uma coisa que nenhum desenvolvedor roda do mesmo jeito, e as duas listas se separariam na primeira mudança. É a razão do roadmap para este RFC existir |
| `nox` ou `tox` | Mais uma ferramenta para fixar, com um venv por sessão resolvido por ela. O venv dos lockfiles já é o ambiente, e um script de cem linhas o percorre |
| `Makefile`, `just` ou um script do PowerShell | O `make` e o `just` não vêm no Windows. Um `.ps1` não é a linguagem dos outros scripts de `scripts/`, e os testes deles são em pytest |
| `pre-commit` | Roda nos arquivos do commit, e não na árvore, cada hook num ambiente próprio, com as versões do próprio arquivo de configuração. Seria um segundo lugar para as versões |
| `pip-tools` ou `uv` para o lockfile das ferramentas | Mais uma ferramenta. E eles fixam o que o índice tem no dia, enquanto o `vendor_python.py --lock` fixa o que o venv rodou, confere cada roda contra o `RECORD` instalado e já é o caminho do lockfile do runtime |
| Um lockfile só, com o runtime e as ferramentas | O lockfile do runtime é instalado inteiro no `runtime\python` do instalador, e as ferramentas iriam para a máquina do usuário. O `test_no_tool_is_in_it` existe para impedir isso |
| Corrigir os 279 erros do ruff e os 13 arquivos do black de `experiments/` | São registros de medições que já rodaram. Reescrevê-los muda a evidência versionada e não ganha nada. Ficam fora pelos caminhos do comando, e o `pyproject.toml` não muda |
| Qualquer Python 3.12 | O black se recusa a rodar no 3.12.5, e o pacote leva o 3.12.9 (RFC-041 §2.8). O CI usaria um, as máquinas outro |
| O `check.py` sobe o banco | Subiria o cluster de desenvolvimento, que todos os checkouts da máquina dividem e cujo `start` aplica migrations (§2.4). Qual banco usar é decisão de quem roda, e o `embedded run --` já existe para isso |
| Deixar o pytest descobrir sozinho que não há banco | 5 min 05 s até 331 erros (§2.4), contra uma conexão |
| Corrigir os dois testes que leem o `WARM_UP_MODELS` do `.env` | É uma mudança nos testes, e não nas conferências. O comando aplica o que o README já manda, e a correção fica para quando alguém pedir |

## 10. Critérios de pronto

**Onde medir:** nesta máquina, num clone novo do commit da implementação em `%TEMP%`, feito com `git clone -c core.autocrlf=true`. O venv vem do `make_venv.py` no Python 3.12.9. O `npm ci` roda com `ELECTRON_SKIP_BINARY_DOWNLOAD=1`, e o `runtime\postgres` vem do `vendor_postgres.py` do clone. O banco é o do `embedded run --`, com `EMBEDDED_POSTGRES_DIRECTORY` numa pasta temporária, e **nunca o cluster de desenvolvimento** (§2.4). Sem `.env` e sem `runtime\models`, como num runner: os testes que pedem os modelos pulam. *(Não pulavam: 13 testes da suíte padrão falhavam sem os modelos, e foram corrigidos, §17.11.)* Os critérios foram escritos antes dos números.

| critério | o número |
|---|---|
| **O comando inteiro no clone novo** | Passa. Tempo de cada etapa e do total: `TBM` → **passa, as nove etapas: 499,9 s de etapas com os caches frios (o `mypy` 230,8 s, o `pytest` 203,7 s) e 236,8 s com eles quentes; com o `embedded run --` e o `initdb`, 524,4 s e 241,7 s** (§16.2). Tempo do `make_venv.py`, com o cache do `pip` frio: `TBM` → **390,3 s** (§16.1). Sem limite: são os números que o RFC-049 usa para o alvo dele. *Passou na terceira rodada, depois de §17.1 e §17.11: na segunda, 13 testes pediam os modelos* |
| **O `mypy` no venv do `make_venv.py`**, no Python 3.12.9 | **Zero erros**, depois da correção de §8. No piloto, no 3.12.5 e com as versões do lockfile, os 8 somem e aparece 1 → **zero erros em 387 arquivos; o arquivo de §8 sem a correção dá o 1 erro, na linha 146** (§16.3) |
| **Um defeito plantado por etapa**, um de cada vez, na lista abaixo | Cada um faz o comando falhar, e a primeira etapa vermelha é a dele. Testado, e não presumido → **os nove: saída 1, e a primeira etapa vermelha é a do defeito; o banco parado, 3,8 s, sem a suíte** (§16.4 e §16.5) |

Os defeitos plantados:

| etapa | defeito |
|---|---|
| `env` | O `sqlalchemy` 2.1.1 instalado por cima do lockfile |
| `ruff` | Um import sem uso em `backend/app` |
| `black` | Uma linha mal formatada em `scripts/` |
| `mypy` | Um `str` devolvido onde a assinatura diz `int` |
| `eslint` | Um hook do React chamado dentro de um `if` |
| `tsc` | Um `number` passado onde a função pede `string` |
| `prettier` | Um arquivo de `frontend/src` gravado com CRLF |
| `vitest` | Um `expect` que não vale |
| `pytest` | Um `assert` que não vale num teste do domínio. Numa segunda rodada, o banco parado: a etapa falha na conexão de teste, em menos de 30 s e sem rodar a suíte |

## 11. Testes

**`backend/tests/test_check.py`** (novo), com os comandos trocados por dublês, como o `test_vendor_python.py` troca os `Steps`:

- as etapas escolhidas por nome e por grupo rodam na ordem da tabela, em qualquer ordem que sejam digitadas;
- um nome desconhecido sai com 2, antes de qualquer etapa;
- a primeira falha para o comando, e com `--keep-going` todas rodam, o resumo nomeia cada falha e a saída é 1;
- a etapa `env`: uma versão diferente da do lockfile reprova com o nome e as duas versões; um pacote ausente reprova; um pacote a mais passa e é listado; outro Python que não o 3.12.9 reprova; com só o grupo `frontend`, nada do Python ou do venv é olhado;
- o `ruff` recebe `--no-fix`, e o `ruff` e o `black` recebem `backend` e `scripts`, e nada mais;
- o pytest não roda quando a conexão de teste falha, e a mensagem tem os dois caminhos de §4.4;
- o pytest recebe `WARM_UP_MODELS=false` no ambiente.

**`backend/tests/test_vendor_python.py`:**

- nenhum pacote está nos dois lockfiles;
- as cinco ferramentas estão no das ferramentas;
- cada linha do das ferramentas tem `===` e um `sha256`, e o cabeçalho tem os dois índices;
- cada versão do das ferramentas é a do venv, como o `test_every_version_is_the_venvs` faz com o do runtime.

**`backend/tests/test_make_venv.py`** (novo), com o `pip` trocado por um dublê:

- outro Python que não o 3.12.9 é recusado, antes de criar qualquer coisa;
- uma pasta que já existe é recusada, e nada dentro dela muda;
- o `setuptools` é instalado sozinho, com o hash, antes dos dois lockfiles;
- um venv que não bate com os lockfiles no fim faz o comando falhar.

## 12. Riscos

- **Montar de novo o venv desta máquina muda 36 pacotes**, entre eles o `torch` (2.14 para 2.13) e o `transformers` (5.17 para 5.15). Quem decide se os vetores mudam é o teste `slow` dos vetores de referência do RFC-035, bit a bit. Ele roda no venv novo antes de o velho ser apagado, e o velho fica renomeado até lá. Se os vetores mudarem, vale a regra de que vetor que muda recomeça o índice, e a decisão sai deste RFC.
- **Dois Python 3.12 na mesma máquina.** O lançador `py -3.12` escolhe um deles. Por isso o `make_venv.py` é chamado pelo caminho do interpretador (§6), e a etapa `env` confere a versão de qualquer jeito.
- **A outra máquina** tem o venv de onde o lockfile saiu em 26/10, no 3.12.9, mas pode ter mudado desde então. A etapa `env` diz isso na primeira execução.
- **Os dois testes instáveis do launcher** (`test_the_last_fifty_lines_are_kept` e `TestTheStateFile::test_a_reader_never_sees_half_a_file`) entram no comando como estão. Uma falha de um deles num `check.py` local é deles, e passa sozinha. A correção é do RFC-050.
- **Um plugin do pytest instalado à parte é carregado sozinho.** A etapa `env` lista os pacotes fora dos lockfiles, e o venv do `make_venv.py` não tem nenhum.
- **O cache do `mypy` vazio custa seis minutos** (§2.8). O tempo do clone novo, em §10, o inclui. Num runner sem cache, só o `mypy` come mais da metade do alvo de 10 min do RFC-049, e guardar o `.mypy_cache` é decisão daquele RFC.

## 13. Entregáveis

- `scripts/check.py` (novo): o comando de §4.
- `scripts/make_venv.py` (novo): o venv de §6.
- `scripts/python-dev-win-64.lock` (novo), escrito pelo `--lock`.
- `scripts/vendor_python.py`: o `--lock` escreve os dois lockfiles, e a comparação do venv com os lockfiles vira uma função que o `check.py` e o `make_venv.py` usam.
- `frontend/.gitattributes` (novo), e os 14 arquivos de §2.3 reescritos no checkout.
- `requirements.txt` da raiz: removido.
- `backend/tests/infrastructure/database/embedded/test_restart_index_migration.py`: a correção do erro de §8.
- `README.md`: o passo 1 de *"Como rodar"* passa a ser o `make_venv.py`, e *"Verificações de qualidade"* passa a ser o `check.py`, com a linha do banco de §4.4. `backend/README.md`: as seções do Ruff, do Black, do MyPy e do Pytest apontam para o comando.
- Os testes de §11.
- `experiments/rfc-048-conferencias/measure_check.py`, com o `.json`. O `.log` fica fora do git.
- Este documento, com os `TBM` escritos de volta, e a linha dele no [README](README.md).

## 14. Fora deste RFC

- **O GitHub**: os workflows, os runners, o cache e os checks obrigatórios. São do RFC-049 e do RFC-050.
- **Os testes `slow` e `embedded`.** O comando roda a suíte padrão. Os outros são do RFC-051.
- **A correção dos dois testes instáveis do launcher** (RFC-050) e a dos dois testes que leem o `WARM_UP_MODELS` do `.env` (§4.4).
- **O `mypy` sobre `scripts/`.** Hoje ele confere `backend/app`, `backend/tests` e `backend/dataset_tools`, e não os scripts que os testes importam. Ampliar o escopo é outro trabalho, com outros erros.
- **Os arquivos de `experiments/`** no ruff e no black (§9).
- **Rodar a suíte em paralelo** (`pytest-xdist`). Seria um plugin novo, e os testes instáveis do launcher pioram sob carga.
- **O Docker.** Ele continua sendo o banco do dia a dia de quem o usa, e nada muda no `docker-compose.yml`.

## 15. O que isto muda no roadmap, e o que passa adiante

**No roadmap.** O pytest passa para o fim da ordem (§4.2). O `--all` vira `--keep-going`, porque sem argumentos o comando já roda tudo. O `pytest-mock` sai da lista do lockfile das ferramentas (§2.6). O venv divergia em 36 pacotes, e não em dois (§2.1). Os 8 erros do `mypy` somem pela versão, e aparece 1 que o SQLAlchemy 2.1 escondia (§8). O `WARM_UP_MODELS=true` derruba dois testes, e não um (§2.4). Num runner sem `.env` isso continua não atrapalhando, e numa máquina de desenvolvimento o comando o neutraliza.

**Para o RFC-049.** O job do frontend chama `python scripts/check.py frontend` no Ubuntu, sem venv. O job da análise estática chama `python scripts/check.py python` no `windows-2025`, com o Python 3.12.9 e o venv do `make_venv.py`. A chave do cache do `pip` é o hash dos dois lockfiles, `scripts/python-win-64.lock` e `scripts/python-dev-win-64.lock`. O `mypy` com o cache vazio levou 370 s nesta máquina (§2.8), e o alvo de 10 min depende de guardar o `.mypy_cache` também. Agrupar a saída de cada etapa no log do GitHub (`::group::`) é uma mudança pequena no `check.py`, e fica para quando o log pedir.

**Para o RFC-050.** O job do backend chama, com `backend` no `PYTHONPATH`, `python -m app.infrastructure.database.embedded run -- python scripts/check.py pytest`. No runner, o cluster padrão é novo a cada execução, e o `initdb` entra no tempo do job.

**Para o RFC-052.** O `dist.mjs` usa o Python do venv de desenvolvimento, e esse venv passa a ser o do `make_venv.py`. O instalador e a suíte usam as mesmas versões, por construção.

**Para quem mexer nas dependências do Python.** Os dois lockfiles mudam juntos, pelo `--lock`, no mesmo commit que mudar o venv. A etapa `env` acusa um venv que mudou sem o lockfile antes de qualquer outra conferência.

**Tamanho estimado:** meio dia a um dia, como no roadmap, contando a medição de §10 e o venv novo desta máquina.

## 16. Validação

Nesta máquina (NVMe, Windows 11, Intel Model 126, 8 threads), em 08/11/2026, pelo `experiments/rfc-048-conferencias/measure_check.py` (`measure_check.json`). Um clone novo em `%TEMP%`, feito com `git clone -c core.autocrlf=true` de um commit de preparo com a implementação (§17.8). O venv veio do `make_venv.py`, pelo Python 3.12.9 de §17.4, com o `PIP_CACHE_DIR` numa pasta vazia. O `npm ci` rodou com `ELECTRON_SKIP_BINARY_DOWNLOAD=1` e o cache do `npm` desta máquina, e o `runtime\postgres` veio do `vendor_postgres.py` do clone. O banco foi o do `embedded run --`, com o `EMBEDDED_POSTGRES_DIRECTORY` numa pasta temporária, e nunca o cluster de desenvolvimento. Sem `.env` e sem `runtime\models`.

Foram três rodadas. A primeira parou no `make_venv.py` (§17.1). A segunda chegou ao fim e achou 13 testes que pediam os modelos (§17.11, `measure_check-rodada2.json`). Os números abaixo são da terceira, com as duas correções, e nenhum critério foi reescrito depois deles.

### 16.1 O clone e o ambiente

| | |
|---|---|
| Arquivos CRLF na cópia de trabalho, `frontend/` | **0**, com o `.gitattributes` de §7 |
| Arquivos CRLF na cópia de trabalho, `backend/` | 409: o `autocrlf=true` vale, e o backend passa assim |
| `make_venv.py`, cache do `pip` frio | **390,3 s** (375,4 s na segunda rodada). Python 3.12.9, os 71 pacotes dos lockfiles e o `pip` 24.3.1, nada mais |
| `npm ci`, cache do `npm` quente | 35,2 s |
| `vendor_postgres.py` | 24,7 s |

### 16.2 O comando inteiro

`embedded run -- python scripts/check.py`, duas vezes seguidas no mesmo clone: a primeira com os caches vazios (o do `mypy` e o do `pytest`), a segunda com eles quentes.

| etapa | caches frios | caches quentes |
|---|---|---|
| `env` | 0,7 s | 0,5 s |
| `ruff` | 4,0 s | 1,9 s |
| `black` | 12,5 s | 1,8 s |
| `mypy` | **230,8 s** | 6,6 s |
| `eslint` | 19,6 s | 15,9 s |
| `tsc` | 11,0 s | 11,4 s |
| `prettier` | 5,7 s | 6,2 s |
| `vitest` | 11,9 s | 11,2 s |
| `pytest` | **203,7 s** | 181,3 s |
| **soma das etapas** | **499,9 s** | **236,8 s** |
| **com o `embedded run --`** | **524,4 s**, o `initdb` incluído | **241,7 s** |

**Passa**, as nove etapas. A suíte deu **3 540 passados, nenhum pulado e nenhuma falha**, com 686 fora da seleção (`slow` e `embedded`). Os grupos: o `python` (com o `env`) em 248,0 s frio e 10,8 s quente, e o `frontend` em 48,2 s e 44,7 s. Com os caches frios, o `mypy` sozinho é 46% do tempo das etapas, e a suíte é 41%. São os números de que o alvo de 10 min do RFC-049 parte (§15).

### 16.3 O `mypy`

No venv dos dois lockfiles, no Python 3.12.9: **zero erros em 387 arquivos**, no clone novo e no venv-semente (§17.2). Dos 8 erros do `.venv` desta máquina (§8, o piloto), os 8 somem só pela versão do SQLAlchemy, sem mudança no código. O que aparecia no lugar deles, o `comparison-overlap` do `test_restart_index_migration.py:146`, foi corrigido no teste como §8 decidiu. O controle: a versão original desse arquivo, conferida no mesmo venv, dá **1 erro, na linha 146**, e a corrigida, nenhum.

### 16.4 Um defeito plantado por etapa

Cada defeito foi plantado sozinho no clone, o comando rodou sem argumentos, e o defeito foi desfeito antes do próximo. O do `pytest` rodou dentro do `embedded run --`. Os outros param antes do `pytest`, e rodaram contra o cluster temporário parado, para que um defeito que escapasse falhasse na conexão de teste, e não passasse.

| etapa | defeito | a primeira etapa vermelha | tempo até a falha |
|---|---|---|---|
| `env` | o `sqlalchemy` 2.1.1 instalado por cima do 2.0.51 do lockfile | **`env`** | 1,0 s |
| `ruff` | um `import json` sem uso em `backend/app` | **`ruff`** | 1,7 s |
| `black` | `PLANTED = {  'a':1 }` no fim de um arquivo de `scripts/` | **`black`** | 2,7 s |
| `mypy` | uma função `-> int` que devolve um `str`, em `backend/app` | **`mypy`** | 13,9 s |
| `eslint` | um `useState` dentro de um `if`, num componente novo de `frontend/src` | **`eslint`** | 30,5 s |
| `tsc` | `shout(42)`, onde `shout` pede um `string` | **`tsc`** | 34,2 s |
| `prettier` | o `frontend/src/main.tsx` gravado com CRLF | **`prettier`** | 39,9 s |
| `vitest` | `expect(1 + 1).toBe(3)` num teste novo | **`vitest`** | 46,7 s |
| `pytest` | `assert 1 + 1 == 3` num teste do domínio | **`pytest`** (1 falha, 3 540 passados) | 242,2 s |

Os nove fizeram o comando sair com 1, e em cada um a primeira etapa vermelha foi a do defeito.

### 16.5 O banco parado

`check.py pytest` contra o cluster temporário parado: a etapa falhou na conexão de teste em **3,8 s**, sem começar a suíte, com a mensagem de §4.4 (§17.5): a fonte (`DATABASE_SOURCE=embedded`), o erro do cluster e os dois caminhos. O critério era menos de 30 s. Sem a conexão de teste, o piloto levou 5 min 05 s até os 331 erros (§2.4).

### 16.6 O venv desta máquina

Os vetores decidiram antes (§17.7): iguais bit a bit no venv velho e no dos lockfiles. O `.venv` de 3.12.5 foi movido para fora do repositório, para `..\SolidVision-venv-3.12.5-antigo`, e não apagado, e o `make_venv.py` montou o novo em 389 s, com o cache do `pip` desta máquina. Fora dos lockfiles, o venv velho só tinha o `pip`: nenhum pacote se perdeu. No venv novo, a etapa `env` passa, o black roda de novo no venv do projeto, e as duas falhas do `test_vendor_python.py` que vinham do venv (§2.4) somem.

Na cópia de trabalho, com o `.env` desta máquina (`WARM_UP_MODELS=true`) e com o `runtime\models`, o comando inteiro, dentro do `embedded run --` com um cluster temporário, **passa as nove etapas em 465,8 s** (o `mypy` em 168,0 s, porque o cache dele era do venv velho), com os mesmos **3 540 testes passando** do clone. E o `test_reference_embeddings_slow.py` passa no venv novo, 7 de 7.

## 17. Correções feitas durante a implementação

### 17.1 Os dois lockfiles levam as mesmas opções

A primeira medição de §10 parou no `make_venv.py`, em 38,7 s: `No matching distribution found for langdetect===1.0.9`. O `pip` junta as opções de todos os arquivos de uma chamada, na ordem em que os lê. O lockfile das ferramentas, escrito com o cabeçalho de §5 (os índices e o `--only-binary :all:`), era lido depois do do runtime, e o `--only-binary :all:` dele apagava o `--no-binary langdetect` do outro. O `langdetect` 1.0.9 só tem sdist. O venv-semente de §17.2 não viu isso porque instalou os dois arquivos em chamadas separadas.

Os dois cabeçalhos agora têm as mesmas opções, o `--no-binary langdetect` incluído, com duas linhas de comentário que dizem por quê. O `test_the_two_lockfiles_carry_the_same_options` exige isso: ele falhou no arquivo da primeira versão antes de passar no da segunda. Um `pip install --dry-run --ignore-installed` dos dois arquivos numa chamada achou os 71 pacotes, e a segunda medição passou. Na falha, o `make_venv.py` deixou a pasta como estava e disse que ela não é um venv aceito, como §6 manda.

### 17.2 O primeiro lockfile das ferramentas saiu de um venv-semente

O `--lock` fixa as versões do venv que o roda. Rodado no `.venv` desta máquina, que diverge do lockfile do runtime em 36 pacotes (§2.1), ele reescreveria o lockfile do runtime com o `torch` 2.14. E o `make_venv.py` precisa do lockfile das ferramentas, que ainda não existia.

O primeiro saiu de um venv descartável no Python 3.12.9: o `setuptools` do lockfile, o lockfile do runtime e as cinco ferramentas, mais as nove dependências só delas, nas versões do `.venv` de hoje (§2.7), com `--no-deps`, e um `pip check` sem conflito nenhum. Desse venv, o `--lock` deixou o lockfile do runtime como estava e escreveu o das ferramentas com os 14 pacotes de §2.7, 24 MB. Os cinco compartilhados (`click`, `colorama`, `packaging`, `pygments` e `typing-extensions`) ficaram só no do runtime, nas versões dele, e as ferramentas aceitam essas versões. Cada roda das ferramentas foi conferida arquivo a arquivo contra o venv, como as do runtime.

### 17.3 Um lockfile em que só a data mudaria não é reescrito

§5 diz que o lockfile do runtime não muda. Reescrito, ele mudaria a linha da data e, com ela, o `sha256` que o manifesto do `runtime\python` guarda e que o `check_package.py` confere. O `--lock` agora compara o texto novo com o velho sem a linha da data, e só escreve se mais alguma coisa mudou. A primeira versão comparava só os pacotes, e não teria gravado a correção de §17.1, que muda só o cabeçalho.

### 17.4 O Python 3.12.9 desta máquina veio do pacote do python.org no NuGet

§6 pede o Python 3.12.9 ao lado do 3.12.5 de `C:\Python312`. O instalador do python.org registra o Python no Windows, e o `py -3.12` passaria a ter dois para escolher (§12). O pacote `python` 3.12.9 do nuget.org, que a Python Software Foundation publica para máquinas de build, é a mesma compilação numa pasta, sem instalador e sem registro. Ele foi extraído em `%LOCALAPPDATA%\Programs\Python\3.12.9`. O SHA-512 do pacote confere com o que o nuget.org publica, e o `python.exe`, o `python312.dll` e o lançador do `venv` têm a assinatura Authenticode válida da PSF. Ele traz o `venv` e o `ensurepip`, com o `pip` 24.3.1. O `py -0p` continua listando só o 3.12.5 e o 3.14.

### 17.5 As mensagens do `check.py` são em inglês

§4.4 escreveu a mensagem do banco em português. Toda a saída dos scripts de `scripts/` é em inglês, e a do `check.py` também. A mensagem tem as mesmas linhas: o que falhou, com o `DATABASE_SOURCE`, os dois caminhos e o comando inteiro do `embedded run --`.

### 17.6 A conexão de teste marca as linhas que o `check.py` lê

A conexão de teste roda num processo à parte, com o engine do app, e o logger do app pode escrever no console. Ela marca as duas linhas que o `check.py` lê, a fonte do banco e o erro, em vez de o `check.py` confiar na primeira linha da saída.

### 17.7 Os vetores desta máquina foram decididos por uma comparação direta

§12 deixava ao teste `slow` dos vetores de referência a decisão de montar de novo o venv desta máquina. Esse teste compara bit a bit só na máquina que gravou a referência, que é a outra (Intel Model 158, 4 núcleos, 2 threads). Aqui ele aceita até 1e-5, e não diria se o `torch` 2.13 dá os mesmos bits que o 2.14.

O `experiments/rfc-048-conferencias/vectors_by_venv.py` calculou, pelo adaptador do app sobre o `runtime\models`, o que a referência registra: as 45 imagens uma a uma e em lotes de 8, e os 50 prompts gravados, sem o tradutor e sem a matriz do RFC-042. Rodou no `.venv` velho (Python 3.12.5, `torch` 2.14.0, `transformers` 5.17.0, `numpy` 2.5.3) e no venv dos lockfiles (3.12.9, 2.13.0, 5.15.0, 2.5.2), nesta máquina e com as mesmas 4 threads. **Os 140 vetores são iguais bit a bit** (`vectors_by_venv.json`), e os dois ficam a no máximo 1,2e-6 da referência. Montar de novo o venv desta máquina não muda vetor nenhum, e a regra de que vetor que muda recomeça o índice não se aplica.

### 17.8 A medição clonou um commit de preparo

§10 pede um clone do commit da implementação. A implementação ainda não estava commitada quando foi medida, e quem commita é o usuário. O `measure_check.py` clonou um repositório de preparo: uma cópia do repositório com a cópia de trabalho commitada nela (`git --work-tree=<repositório> add -A`), sem tocar no índice nem nos commits do repositório. O conteúdo medido é o do commit da implementação, menos este documento com os números; o hash, não.

### 17.9 Os dois READMEs e o `conftest.py`

§13 pedia, no `backend/README.md`, só as seções do Ruff, do Black, do MyPy e do Pytest. As de criar o venv e instalar as dependências mandavam fazer o `pip install -r backend/requirements.txt` que §6 tira do README da raiz, e passaram a apontar para o `make_venv.py` também. E um comentário do `conftest.py` justificava o isolamento da suíte pelo `requirements.txt` da raiz, que saiu (§2.6). Agora ele cita o lockfile das ferramentas.

### 17.10 O `make_venv.py` não isola o `pip` da configuração

O `vendor_python.py` chama o `pip` com `--isolated`. O `make_venv.py` não, para que o `PIP_CACHE_DIR` valha: a medição de §10 o usa para o cache frio, e o cache do `pip` do RFC-049 também. Os índices, as rodas e os hashes vêm dos lockfiles.

### 17.11 Treze testes da suíte padrão pediam o `runtime\models`

§10 presumia que, sem `runtime\models`, "os testes que pedem os modelos pulam". Na segunda medição, a única que chegou ao fim antes desta correção (`measure_check-rodada2.json`), as sete etapas estáticas passaram no clone novo, e o `pytest` deu **13 falhas**, as mesmas nas duas rodadas do comando, todas pela falta dos modelos:

| arquivo | testes | o que falhava |
|---|---|---|
| `tests/cli/test_search.py` | 10 | `ModelUnavailableError` |
| `tests/infrastructure/workers/test_indexing_worker.py` | 2, os do `main()` | `CannotStart: 78` |
| `tests/presentation/test_api_startup.py` | 1, o do aquecimento | o modelo de mentira nunca recebeu a consulta |

Nenhum é `slow`, e os três trocam o modelo por um dublê: o `test_search.py` diz, na primeira linha, que nenhum checkpoint do CLIP é tocado. Mas o código que eles compõem confere, antes de chamar o modelo, se os arquivos dele estão no disco (`default_runtime().check_presence()`, [RFC-035 §6.1](rfc-035-modelos-offline.md)). Numa máquina de desenvolvimento, com `runtime\models`, a conferência passa e o teste também. Num clone novo, ou num runner, não.

A correção é só nos testes: onde o modelo já é um dublê, a conferência de presença também é (`ModelRuntime.check_presence` trocada por uma função que não faz nada). Nenhum dos três arquivos tinha teste que dependesse da conferência de verdade. No mesmo clone sem modelos, os três arquivos deram 13 falhas e 58 passagens na versão original, e 71 passagens na corrigida; na cópia de trabalho, com os modelos, 71 passagens.

É o tipo de teste que o roadmap esperava achar no RFC-050 ("algum teste pode presumir algo desta máquina"). Ele apareceu aqui porque §10 já mede num clone sem modelos, e foi corrigido aqui porque o critério de §10 pede o comando passando.
