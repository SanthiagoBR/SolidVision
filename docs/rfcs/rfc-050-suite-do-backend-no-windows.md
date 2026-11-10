# RFC-050 — A Suíte Padrão do Backend no Windows, com o Postgres Embarcado

**Status:** Proposto
**Depende de:** RFC-048 (o `check.py pytest`, a conexão de teste e o `make_venv.py`), RFC-049 (o `ci.yml`, o passo do venv e o `test_ci_workflow.py`), RFC-033 §5.1 e §11 (o `vendor_postgres.py` e o `embedded run --`), RFC-036 §5.5 e §8 (os dois testes do launcher), [roadmap de CI/CD](ROADMAP-ci-cd.md) (D2, D4, D5, D6 e D7)
**Bloqueia:** RFC-051, que guarda o `runtime\postgres` pela mesma chave e monta o venv pela mesma action; RFC-052, que monta o venv do build pela action deste RFC
**Migration:** nenhuma
**Medição:** o piloto de §2, nesta máquina (NVMe, Windows 11, Intel Model 126, 8 threads), em 10/11/2026, sobre o commit `9dbfcf6`, o da `origin/frontend`. Em `experiments/rfc-050-suite-do-backend/`: `pilot_flaky.py` e `repeat_launcher_tests.py` (os dois testes do launcher), `pilot_suite.py` (a suíte num clone montado como o runner) e `pilot_caches.py` (o `runtime\postgres` e o Python do NuGet), cada um com o `.json`; os `.log` ficam fora do git. A documentação do GitHub sobre *rulesets* e checks obrigatórios e o README da imagem `windows-2025` (versão 20261101.318.1) foram lidos no mesmo dia

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*, e vira número quando a implementação o escrever de volta, ao lado da frase. Os números sem marca vêm do piloto de §2 ou de outros RFCs, e dizem de qual.

---

## 1. Contexto

O RFC-049 ligou o primeiro workflow, com dois jobs: o `frontend`, no Ubuntu, e o `estatico`, no Windows, com o ruff, o black e o `mypy`. A suíte do backend ficou de fora. Ela é a única conferência que precisa de banco, e o roadmap marcou esta etapa como a de maior risco: é aqui que aparecem os testes que só passam numa máquina de desenvolvimento. O RFC-048 já achou 13 deles num clone sem `runtime\models` ([§17.11](rfc-048-conferencias-num-comando-so.md#1711-treze-testes-da-suíte-padrão-pediam-o-runtimemodels)). O runner é a primeira máquina que não tem nada desta.

Quatro perguntas ficaram para este RFC:

- **O que o runner precisa para chegar ao `check.py pytest`.** O venv, o `runtime\postgres` e um cluster. O RFC-048 §15 já deu o comando: o `check.py pytest` dentro do `embedded run --`.
- **Os dois testes instáveis do launcher.** O roadmap supunha a causa de um deles e pedia que cada correção fosse proposta antes de ser feita. Este RFC é a proposta.
- **O Python base.** O RFC-049 §15.2 passou a pergunta para cá. O `setup-python` é o passo mais longo do job quente, de 40 a 58 s, e com o `backend` ele passaria a rodar em dois jobs.
- **Os checks obrigatórios.** O *ruleset* que exige os três jobs, e o que ele protege enquanto o D7 não é decidido.

## 2. O que o piloto mediu

Nesta máquina, em 10/11/2026, sobre o commit `9dbfcf6`. Nada rodou no GitHub. O piloto rodou os dois testes do launcher centenas de vezes, com a máquina livre e com todas as CPUs ocupadas. Rodou também a suíte padrão num clone montado como o runner, e o `vendor_postgres.py` e o Python do NuGet numa pasta temporária. O roadmap partiu do que se sabia antes, e o piloto corrigiu duas premissas dele (§14).

### 2.1 A suíte num clone montado como o runner

O `pilot_suite.py` dá ao clone o que o runner vai ter, até onde esta máquina consegue imitar:

- um clone do `9dbfcf6` com `core.autocrlf=true`, sem `.env` e sem `runtime\models`;
- o `runtime\postgres` copiado para dentro do clone, onde o cache vai pô-lo;
- o `TEMP` e o `TMP` num caminho curto 8.3 (`...\RFC050~1\RUNNER~1`), como os do runner, que são `C:\Users\RUNNER~1\...` porque `runneradmin` passa de oito letras;
- as três variáveis do PostgreSQL da imagem (`PGBIN`, `PGDATA` e `PGROOT`, §2.6);
- o cluster do `embedded run --` numa pasta nova, e nunca o de desenvolvimento.

E então `embedded run -- python scripts/check.py pytest`, com cada linha marcada com a hora:

| fase | tempo |
|---|---|
| `initdb` (de *"creating the cluster"* a *"starting the server"*) | **19,7 s** |
| Subir o servidor | 2,1 s |
| As migrations, até `d2a7e4c19b53` | 0,8 s |
| A etapa `env` do `check.py` | 2,9 s |
| O `pytest` | 203,9 s (196,8 s pelo próprio pytest) |
| **Do comando ao fim, com o cluster parado** | **237,0 s** |

A suíte deu **3 554 passados, nenhum pulado e nenhuma falha**, com 686 fora da seleção (`slow` e `embedded`). São os 3 540 do RFC-048 §16.2 mais os 12 do `test_ci_workflow.py` (RFC-049) e os 2 do `test_embedded_cli.py` (RFC-048 §17.12). O `TEMP` curto não mudou nada: o pytest resolve a pasta temporária (`Path(...).resolve()`) antes de criar o `tmp_path`, e nem `backend/app` nem `backend/tests` leem o `TEMP` por conta própria.

O roadmap dava 9,8 s para o `initdb` num NVMe. Hoje, nesta mesma máquina, foram 19,7 s. O número que vale é o do runner (§9, critério 3).

### 2.2 O primeiro teste instável: a ordem dos dois pipes, e não o `drain()`

O `test_the_last_fifty_lines_are_kept` (`backend/tests/launcher/test_launcher_children.py:104`) sobe um filho que escreve uma linha no `stderr`, sessenta no `stdout` e, por último, `stdin ended`. O launcher lê cada pipe numa thread, e as duas threads põem as linhas na mesma cauda de 50 ([RFC-036 §5.5](rfc-036-launcher-e-supervisao-de-processos.md)). O teste espera que a última da cauda seja `stdin ended`.

O roadmap dizia que a causa era conhecida: o teste ignora o que o `drain()` devolve. O piloto mediu o `drain()`:

| | sem carga | as 8 CPUs ocupadas |
|---|---|---|
| O corpo do teste, com contadores (`pilot_flaky.py`) | 0 falhas em 100. O `drain()` devolveu `True` nas 100, em no máximo 0,002 s | 0 falhas em 100. O mesmo |
| O teste como está, pelo pytest (`repeat_launcher_tests.py`) | **4 falhas em 386**: 1 numa primeira série que parou na sexta execução, e 0, 0, 2 e 1 em séries de 80, 100, 100 e 100 | 0 falhas em 40 |
| A asserção que falhou | as quatro: `assert 'a warning on stderr' == 'stdin ended'` | — |

**O `drain()` terminou sempre. Quem falha é a ordem:** a linha do `stderr`, escrita antes de todas as outras, entrou por último na cauda. Com dois pipes e duas threads, a ordem da cauda é a ordem em que as threads rodaram, e não a ordem em que o filho escreveu. Quando a saída inteira já está no pipe, a thread do `stdout` pode pôr as 62 linhas dela antes de a do `stderr` pôr a sua. O teste confere uma ordem que o código nunca prometeu. Ignorar o `drain()` é um defeito do mesmo teste, porque os outros testes do arquivo o conferem, mas não é ele que falha.

A carga de CPU não aumentou as falhas: foram 0 em 40. A falha depende de como as duas threads se alternam, e não da carga.

### 2.3 O segundo: o escritor não acompanha, e o leitor nunca vê meio arquivo

O `TestTheStateFile::test_a_reader_never_sees_half_a_file` (`backend/tests/launcher/test_launcher_instance.py:81`) põe uma thread reescrevendo o `launcher.json` (20 KB) sem parar, por um arquivo temporário e um `os.replace`. Por um segundo, o teste lê o arquivo o mais rápido que consegue. O teste tem três asserções. Cada leitura tem de ser um registro inteiro, que é a propriedade de [RFC-036 §8](rfc-036-launcher-e-supervisao-de-processos.md). No fim, `reads > 10` e `len(written) > 10`.

| | sem carga | as 8 CPUs ocupadas |
|---|---|---|
| O corpo, com contadores (`pilot_flaky.py`), 60 vezes | 50 falhas. Nenhuma leitura viu meio arquivo, e nenhuma escrita foi recusada. Nas falhas, de 7 a 10 escritas no segundo (mediana 9) | 53 falhas. O mesmo, com 2 a 10 escritas (mediana 6) |
| O teste como está, pelo pytest, 40 vezes | **13 falhas**, todas em `len(written) > 10`, com 8, 9 ou 10 escritas | **40 falhas**: 39 em `len(written) > 10`, com 5 a 8 escritas, e 1 em `reads > 10` |

**A propriedade valeu em todas as execuções. O que falha é a contagem.** O `os.replace` só funciona num instante em que o leitor não está com o arquivo aberto. O Python abre os arquivos sem `FILE_SHARE_DELETE`, e o `StateFile.write` tenta de novo a cada 20 ms, por até 2 s (`REPLACE_SECONDS`). Um leitor que reabre o arquivo sem parar deixa poucas janelas, e o escritor faz por volta de dez trocas por segundo. O limite de dez num segundo fixo é uma aposta na velocidade da máquina, e nesta máquina ela já está no limite: o teste falha um terço das vezes sozinho, e quase sempre com carga. Na suíte inteira do §2.1, os dois testes passaram.

O `status` do launcher lê o arquivo uma vez. No produto, nenhum leitor segura o arquivo como o teste segura.

### 2.4 O `runtime\postgres` guardado

Pelo `pilot_caches.py`: o `vendor_postgres.py` numa pasta nova, com os caches do conda vazios, e o resultado empacotado com `tar --zstd`, o formato do `actions/cache`, apagado e desempacotado.

| | |
|---|---|
| `vendor_postgres.py` (baixa o micromamba e os 16 pacotes do lockfile, confere cada md5) | 32,5 s. O RFC-048 §16.1 mediu 24,7 s |
| O runtime | 2 650 arquivos, 129,3 MB (os "123 MB" dos outros RFCs, em MiB) |
| Empacotado | **40,1 MB**, em 32,4 s |
| **Desempacotar** | **4,1 s** |

### 2.5 O Python base

O RFC-049 §15.2 mostrou por que o `setup-python` demora. A imagem traz o 3.12.10, e a cada execução ele baixa o 3.12.9, desinstala o 3.12.10 com os registros dele e roda o instalador do python.org: de 40 a 58 s no runner (mediana 49,5 s). O RFC-048 §17.4 achou outro caminho para esta máquina. A Python Software Foundation publica o pacote `python` no nuget.org, para máquinas de build: a mesma compilação, numa pasta, sem instalador e sem registro. É o Python base do `.venv` desta máquina desde então.

| o pacote `python` 3.12.9 do nuget.org | |
|---|---|
| Tamanho | **14,5 MB** |
| Baixar | **0,7 s** (1,1 s numa segunda vez, pelo `curl`) |
| O SHA-512 | igual ao que o nuget.org publica no catálogo |
| O SHA-256 | `b7240e3117d94dc2446c4b1250e31306adc817934706937d0992c78f937487ba` |
| Extrair com o `tar.exe` do Windows | **2,6 s**. A pasta `tools`: 1 321 arquivos, 39,3 MB, e `python.exe` diz 3.12.9 |
| A mesma pasta guardada em `tar --zstd` | 15,0 MB, desempacotada em 1,9 s |

Baixar e extrair levam 3,3 s, e restaurar do cache levaria 1,9 s mais a ida ao serviço de cache. Guardar o Python base não ganha nada que se veja.

Um piloto anterior, nesta mesma sessão, falhou ao resolver o nome `api.nuget.org` (`getaddrinfo failed`), e a tentativa seguinte, um minuto depois, passou. Foi a rede desta máquina, mas é um lembrete: um download a cada execução depende do site a cada execução (§11).

### 2.6 O runner, o repositório e o ruleset

**A imagem `windows-2025`** (README da versão 20261101.318.1):

- o Python da imagem é só o **3.12.10**;
- o **PostgreSQL 17.11** está instalado, com o serviço `postgresql-x64-17` **desabilitado** e parado. A imagem define `PGBIN`, `PGDATA` e `PGROOT`, mas não `PGUSER` nem `PGPASSWORD`. O `embedded run --` tira as variáveis `PG*` do próprio processo e do comando que roda (RFC-033 §11), e o piloto de §2.1 rodou com as três;
- o Docker 29.7.2 está instalado, e nada aqui o usa;
- um runner padrão de repositório público tem **4 vCPUs, 16 GB de RAM e 14 GB de SSD**. Esta máquina tem 8 threads.

**O repositório**, em 10/11/2026:

- a `origin/frontend` está em `9dbfcf6`, e a `main` está **75 commits** atrás dela. A execução do `ci.yml` nesse commit passou os dois jobs em 74 s;
- os caches somam **3,39 GB** em 30 entradas. Desses, 3,04 GB são das dez branches de medição do RFC-049, que saem sozinhas depois de 7 dias sem acesso, até 16/11;
- a API pública responde, sem token, `200` e uma lista vazia para `/rulesets` e para `/rules/branches/main`. Hoje, nenhuma regra vale para branch nenhuma.

**O que a documentação do GitHub diz dos checks obrigatórios:**

- o nome de um check de workflow é o nome do job, e o check não leva em conta o workflow nem o evento;
- um workflow pulado por filtro de branch, de caminho ou de mensagem deixa o check **pendente para sempre**, e o merge não acontece. É a razão do RFC-049 §4.1 para não ter `paths-ignore`;
- um push direto numa branch protegida, com os checks obrigatórios sem passar, é recusado (`GH006: Protected branch update failed`);
- para escolher um check na interface, ele tem de ter rodado no repositório nos últimos 7 dias;
- o check de uma execução por `workflow_dispatch` não vale para um ruleset;
- um app pode ser escolhido como a fonte esperada do check, e um status com o mesmo nome vindo de outra fonte não conta;
- a lista de *bypass* aceita o papel de admin do repositório.

As páginas lidas não dizem com todas as letras se uma conta pessoal no plano Free cria um ruleset num repositório público. A API já responde aos endpoints de rulesets do repositório. Se a criação for recusada, §6 diz o que fazer.

## 3. Decisões

1. **O job `backend` entra no mesmo `ci.yml`**, no `windows-2025`, ao lado dos outros dois e sem `needs`. Ele chama `check.py pytest` dentro do `embedded run --`, e mais nada.
2. **Uma *composite action*, `.github/actions/venv`, monta o Python base e o venv.** O `estatico` e o `backend` a usam, e o RFC-052 vai usá-la também.
3. **O Python base vem do pacote do nuget.org**, com o SHA-256 fixado, numa pasta fixa do runner, e nunca vai para o `PATH`. O `setup-python` sai (§4.3).
4. **O `runtime\postgres` é guardado** na chave do `postgres-win-64.lock` e do `vendor_postgres.py`, e salvo à parte logo depois do `vendor_postgres.py`, como o venv (RFC-049 §4.4).
5. **O cluster nasce a cada execução.** O `initdb` entra no tempo do job, e nenhum diretório de dados é guardado.
6. **Os dois testes instáveis são corrigidos nos testes** (§5). O código do launcher não muda.
7. **O `PYTHON_VERSION` sobe para o `env` do workflow**, e o SHA-256 do pacote do nuget.org fica ao lado dele. A versão continua também no `check.py`, e a etapa `env` e o teste de §7 conferem que as duas batem.
8. **O ruleset `ci`, na branch padrão** (`~DEFAULT_BRANCH`), exige os três checks pelo nome, vindos do GitHub Actions, sem ninguém na lista de *bypass*. Ele fica em `.github/rulesets/ci.json`, e quem o importa no repositório é o usuário (§6).
9. **O teste do YAML confere também a action, o arquivo do ruleset e os nomes dos jobs** (§7).
10. **O `check.py` não muda.**

## 4. O workflow

### 4.1 O job `backend`

```yaml
env:
  # Also check.py's PYTHON_VERSION: the env step fails if the two disagree.
  PYTHON_VERSION: "3.12.9"
  # python.3.12.9.nupkg on nuget.org (RFC-050 section 2.5).
  PYTHON_NUGET_SHA256: "b7240e3117d94dc2446c4b1250e31306adc817934706937d0992c78f937487ba"

jobs:
  # ...frontend e estatico, como no RFC-049; o estatico troca o setup-python
  # e os três passos do venv pela action de §4.2.

  backend:
    runs-on: windows-2025
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: ./.github/actions/venv
        with:
          python-version: ${{ env.PYTHON_VERSION }}
          nuget-sha256: ${{ env.PYTHON_NUGET_SHA256 }}
      - id: postgres
        uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
        with:
          path: runtime/postgres
          key: postgres-${{ hashFiles('scripts/postgres-win-64.lock', 'scripts/vendor_postgres.py') }}
      - if: steps.postgres.outputs.cache-hit != 'true'
        run: .\.venv\Scripts\python.exe scripts/vendor_postgres.py
      - if: steps.postgres.outputs.cache-hit != 'true'
        uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
        with:
          path: runtime/postgres
          key: ${{ steps.postgres.outputs.cache-primary-key }}
      - run: .\.venv\Scripts\python.exe -m app.infrastructure.database.embedded run -- .\.venv\Scripts\python.exe scripts/check.py pytest
        env:
          PYTHONPATH: backend
```

- **O banco.** O checkout não tem `.env`. O `run --` sobe o cluster padrão de um checkout, `%LOCALAPPDATA%\SolidVision-dev\postgres`, que num runner é sempre novo: faz o `initdb`, sobe o servidor, aplica as migrations, roda o comando com `DATABASE_SOURCE=embedded` e para o cluster no fim. A saída do job é a do comando. Numa máquina de desenvolvimento, o RFC-048 §10 precisou de uma pasta temporária para não tocar o cluster de todo dia. No runner, não existe outro.
- **O `check.py pytest`** roda a etapa `env`, que confere o venv contra os dois lockfiles, e o pytest com `WARM_UP_MODELS=false`, depois da conexão de teste (RFC-048 §4.4).
- **Sem `runtime\models`.** Os testes que pedem os modelos são `slow` e ficam fora da seleção. Os 13 do RFC-048 §17.11 trocam o modelo por um dublê.
- **O PostgreSQL da imagem** fica como está, com o serviço desabilitado (§2.6).
- **O `vendor_postgres.py`** só usa a biblioteca padrão, e roda pelo Python do venv, pelo caminho, como tudo o que vem depois da action (RFC-048 §17.12).
- **`timeout-minutes: 30`**, como o `estatico`. Nesta máquina, do comando ao fim foram 237,0 s (§2.1). No runner, o RFC-049 §15.1 achou cada passo frio de 4 a 16 vezes mais rápido que aqui.

### 4.2 A action do venv

`.github/actions/venv/action.yml`:

```yaml
name: venv
description: >-
  The base Python from nuget.org and the venv of the two lockfiles
  (RFC-049 section 4.3, RFC-050 section 4.2).
inputs:
  python-version:
    description: check.py's PYTHON_VERSION
    required: true
  nuget-sha256:
    description: The SHA-256 of python.<version>.nupkg
    required: true
runs:
  using: composite
  steps:
    - shell: pwsh
      env:
        VERSION: ${{ inputs.python-version }}
        SHA256: ${{ inputs.nuget-sha256 }}
      run: |
        $folder = Join-Path $env:RUNNER_TOOL_CACHE "python-nuget\$env:VERSION"
        $package = Join-Path $env:RUNNER_TEMP "python.$env:VERSION.nupkg"
        Invoke-WebRequest -OutFile $package `
          "https://api.nuget.org/v3-flatcontainer/python/$env:VERSION/python.$env:VERSION.nupkg"
        $found = (Get-FileHash -Algorithm SHA256 $package).Hash
        if ($found -ne $env:SHA256) { throw "python.$env:VERSION.nupkg is $found, not $env:SHA256" }
        New-Item -ItemType Directory -Force $folder | Out-Null
        & "$env:SystemRoot\System32\tar.exe" -xf $package -C $folder
        "BASE_PYTHON=$folder\tools\python.exe" >> $env:GITHUB_ENV
    - id: venv
      uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
      with:
        path: .venv
        key: venv-${{ inputs.python-version }}-${{ hashFiles('scripts/python-win-64.lock', 'scripts/python-dev-win-64.lock', 'scripts/make_venv.py', '.github/actions/venv/action.yml') }}
    - if: steps.venv.outputs.cache-hit != 'true'
      shell: pwsh
      run: '& $env:BASE_PYTHON scripts/make_venv.py'
    - if: steps.venv.outputs.cache-hit != 'true'
      uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
      with:
        path: .venv
        key: ${{ steps.venv.outputs.cache-primary-key }}
```

- **Os três passos do venv são os do RFC-049 §4.3**, agora num lugar só: restaurar, montar quando não achou, e salvar logo depois do `make_venv.py`, que confere o venv contra os lockfiles antes de terminar.
- **O Python base vem antes do venv, em toda execução**, porque o venv restaurado acha o Python base pelo caminho gravado no `pyvenv.cfg`.
- **A chave do venv inclui o arquivo da action.** Um venv montado sobre outro Python base, ou noutra pasta, não serve. A primeira execução depois deste RFC monta um venv novo, e os venvs guardados pelo RFC-049, que apontam para a pasta do `setup-python`, deixam de ser lidos e saem em 7 dias.
- **O `tar.exe` vai pelo caminho do Windows.** Ele lê o `.nupkg`, que é um zip. Um `tar` achado no `PATH` pode ser o GNU do Git, que não lê zip.
- **O `make_venv.py` roda pelo caminho do Python base**, e nenhum Python vai para o `PATH`.

### 4.3 Por que o nuget.org, e não o `setup-python`

O `setup-python` instala o Python pelo instalador do python.org, e o instalador é o que custa: de 40 a 58 s, em cada job Windows de cada execução (RFC-049 §15.2). O pacote do nuget.org é a mesma compilação, num zip de 14,5 MB que se extrai em 2,6 s (§2.5). Com dois jobs Windows, cada execução poupa perto de 45 s de cada um. Os dois rodam juntos, então o resultado chega perto de 45 s antes.

São três ganhos além do tempo. O Python base do runner passa a ser o desta máquina, o mesmo pacote (RFC-048 §17.4). O pacote fica fixado pelo SHA-256, como os lockfiles fixam cada roda e o RFC-049 fixa cada action pelo commit. E o workflow passa a usar só duas actions: `checkout` e `cache`.

A pasta é `%RUNNER_TOOL_CACHE%\python-nuget\<versão>`, ao lado de onde o `setup-python` instalava, e a mesma em toda execução. O Python base não é guardado em cache (§2.5).

### 4.4 O `runtime\postgres` e o cluster

O runtime segue o desenho do venv. É restaurado pela chave do lockfile e do script. Numa execução que não o acha, o `vendor_postgres.py` o materializa, e ele é salvo logo em seguida. O script confere o md5 de cada pacote e só põe o runtime no lugar, com o manifesto, no fim (RFC-033 §5.1). Então só um runtime completo é guardado. O script entra na chave porque o runtime depende dele também: é ele que tira os `.pdb` e escreve o manifesto.

**O cluster não é guardado.** Um diretório de dados de outra execução seria um cluster que o código deste commit não criou, e o `initdb` num diretório vazio é o que o app faz na primeira vez numa máquina nova. Ele custa 19,7 s nesta máquina (§2.1), e o número do runner é o critério 3.

### 4.5 Os caches

| cache | chave | por entrada |
|---|---|---|
| O venv (`.venv`) | `venv-<Python>-<hash dos lockfiles, do make_venv.py e da action>`, uma por branch, dividida pelo `estatico` e pelo `backend` | 240,5 MB no runner (RFC-049 §14.5) |
| O `.mypy_cache` | Como no RFC-049 | 13,0 MB por commit |
| Os pacotes do npm | Como no RFC-049 | 69,8 MB |
| O `runtime\postgres` | `postgres-<hash do lockfile e do vendor_postgres.py>` | 40,1 MB nesta máquina (§2.4) |

Uma branch nova soma perto de 360 MB. O limite do repositório é 10 GB, e hoje há 3,39 GB, dos quais 3,04 GB saem até 16/11 (§2.6). As medições de §9 ficam numa branch só para os critérios 2 e 3.

**Numa branch fria, os dois jobs Windows montam o venv.** O `estatico` e o `backend` começam juntos, nenhum acha o venv, e os dois rodam o `make_venv.py`. O primeiro a terminar salva. O `cache/save` do segundo acha a chave tomada e avisa, sem falhar o job. O custo é um `make_venv.py` a mais, só na primeira execução de cada branch (RFC-049 §4.4).

## 5. Os dois testes do launcher

As duas correções são só nos testes. O código do launcher e o RFC-036 não mudam. Os protótipos das duas estão em `repeat_launcher_tests.py` (`test_last_fifty_fixed` e `test_half_a_file_fixed`), escritos como ficariam na suíte, para serem rodados tantas vezes quanto os testes de hoje (§10).

### 5.1 As últimas cinquenta linhas, num pipe só

O teste passa a subir um filho que só escreve no `stdout`, e passa a conferir o `drain()`:

```python
ONLY_STDOUT = f"""
import sys
for number in range({TAIL_LINES + 10}):
    print("line", number, flush=True)
sys.stdin.buffer.read()
print("stdin ended", flush=True)
"""
"""Only stdout: across two pipes the tail keeps the order in which the two
readers ran, not the order the child wrote (RFC-050 section 2.2)."""


def test_the_last_fifty_lines_are_kept() -> None:
    child, _ = start(ONLY_STDOUT)
    child.request_stop()
    wait_for_exit(child)
    assert child.drain()

    lines = child.last_lines()
    assert len(lines) == TAIL_LINES
    assert lines[-1] == "stdin ended"
    assert last_line(lines) == "stdin ended"
```

Num pipe só, uma thread lê as linhas na ordem em que o filho as escreveu, e o teste confere o que se propõe a conferir: que a cauda guarda as últimas 50 e que a última é a última. O `stderr` e o nome do filho na frente de cada linha continuam conferidos pelo `test_every_line_reaches_the_launcher_with_the_childs_name`, que usa o `UNTIL_STDIN_ENDS` como está.

### 5.2 O arquivo de estado: até dez de cada, e não um segundo

O teste passa a ler até ver mais de dez leituras e mais de dez escritas, com um prazo de 30 s, em vez de ler por um segundo e contar depois:

```python
        thread.start()
        reads = 0
        deadline = time.monotonic() + DEADLINE  # 30 s
        try:
            while reads <= 10 or len(written) <= 10:
                assert time.monotonic() < deadline, (
                    f"{reads} reads and {len(written)} writes in {DEADLINE:.0f} s"
                )
                try:
                    text = (tmp_path / "launcher.json").read_text(encoding="utf-8")
                except PermissionError:
                    # Windows refuses an open at the instant of the rename;
                    # what must never happen is a file that does not parse.
                    continue
                record = InstanceRecord.from_json(json.loads(text))
                assert record.state is LauncherState.READY
                reads += 1
        finally:
            stop.set()
            thread.join(10)
```

As duas asserções do fim saem, porque viram a condição do laço. A propriedade é a mesma: toda leitura é um registro inteiro, enquanto o escritor troca o arquivo mais de dez vezes. O que muda é que o teste espera o trabalho de que precisa, em vez de apostar que ele cabe num segundo. Numa máquina rápida, ele termina antes do segundo de hoje. Com as 8 CPUs ocupadas, o piloto viu de 2 a 10 escritas por segundo, e o teste levaria uns poucos segundos. Ele só falha pelo prazo se o escritor não conseguir onze trocas em 30 s.

## 6. O ruleset

`.github/rulesets/ci.json`, no formato que o GitHub exporta e importa:

```json
{
  "name": "ci",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] }
  },
  "bypass_actors": [],
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "frontend", "integration_id": 15368 },
          { "context": "estatico", "integration_id": 15368 },
          { "context": "backend", "integration_id": 15368 }
        ]
      }
    }
  ]
}
```

- **`~DEFAULT_BRANCH`**, e não `main`. O ruleset segue o D7 sem ser editado.
- **Uma regra só, a dos três checks.** O roadmap não pede outras, como proibir o *force push* ou apagar a branch (§13).
- **Sem exigir a branch em dia com a base** (`strict`). Com uma pessoa no repositório, isso dobraria as execuções para se proteger de dois merges que se cruzam, coisa que não acontece aqui.
- **A fonte é o GitHub Actions** (o app de id 15368). Um status chamado `backend` postado por outra fonte não conta. O id é conferido na importação: a regra lida de volta pela API tem de trazê-lo (§10).
- **Ninguém na lista de *bypass*, nem o admin.** O admin é a única pessoa que faz push. Com ele na lista, a regra não valeria para ninguém.
- **Quem o importa é o usuário**, em *Settings → Rules → Rulesets → New ruleset → Import a ruleset*, ou com `gh api -X POST repos/SanthiagoBR/SolidVision/rulesets --input .github/rulesets/ci.json`. A importação vem depois de o job `backend` ter passado uma vez no repositório (§2.6). O arquivo no git é o registro do que foi ligado, e o teste de §7 o confere contra o `ci.yml`. O GitHub não o lê sozinho.

**O que ele faz enquanto o D7 não é decidido.** A branch padrão é a `main`, e ninguém faz push nela. O ruleset recusa um merge na `main` com um dos três checks vermelho, pendente ou ausente, e recusa também um push direto na `main` de um commit cujos checks não passaram. Na `frontend` e em qualquer outra branch, nada muda: o push é livre, e os checks rodam e informam. **Até o D7, o ruleset protege uma branch que ninguém usa.** Com o D7 como o roadmap recomenda, levando a `frontend` para a `main` por um PR, esse PR é o primeiro que o ruleset julga. Dali em diante, um commit só chega à `main` depois de passar os três checks noutra branch.

**Se o plano não deixar criar o ruleset** (§2.6), a mesma regra vira uma proteção de branch clássica na `main`, com *"Require status checks to pass"* e os três nomes. A proteção clássica não aceita `~DEFAULT_BRANCH`, e o D7 passa a pedir que ela seja refeita. O `ci.json` continua como registro.

## 7. O YAML conferido pela suíte

O `backend/tests/test_ci_workflow.py` passa a ler também a action e o ruleset. As conferências do RFC-049 §6 continuam, com quatro mudanças:

- **As actions.** O teste lê o `ci.yml` e cada `.github/actions/*/action.yml`. Um `uses: ./.github/actions/<nome>` é aceito quando a pasta existe, e o `action.yml` dela passa pelas mesmas regras: cada `uses:` da organização `actions`, fixado pelo commit, com a tag num comentário, e cada action num commit só, somando os dois arquivos.
- **O Python.** O `PYTHON_VERSION` do `env` do workflow é o do `check.py`, e cada `python-version:` do `ci.yml` lê `${{ env.PYTHON_VERSION }}`. A action não tem versão própria, e o `PYTHON_NUGET_SHA256` tem 64 caracteres hexadecimais.
- **Os jobs e o ruleset** (nova). Os jobs do `ci.yml` são exatamente `frontend`, `estatico` e `backend`. O `ci.json` exige exatamente esses três, do app 15368, na `~DEFAULT_BRANCH`, sem *bypass*. Renomear um job sem mudar o ruleset deixaria o check pendente para sempre (§2.6). Assim, o teste falha antes do push.
- **O ruleset é lido com o `json`** da biblioteca padrão. O YAML continua sendo lido como texto, pela razão do RFC-049 §6.

## 8. Alternativas consideradas

| alternativa | por que não |
|---|---|
| O banco num *service container* | Só existe em runner Linux, e não é a versão que vai no instalador (D4) |
| O Docker do runner Windows | A imagem do pgvector é Linux, e o Windows Server roda containers Windows |
| O PostgreSQL 17 da imagem, com o pgvector compilado | Outra versão, que muda com a imagem, e não a do instalador (RFC-033a) |
| Guardar o diretório de dados do cluster | §4.4 |
| `pytest-rerunfailures` | Esconde a instabilidade em vez de removê-la (roadmap) |
| Ler o `stderr` do filho pelo mesmo pipe do `stdout`, no launcher | Daria a ordem ao teste do §2.2, mas muda o produto e uma decisão do RFC-036 §5.5 para consertar um teste. O produto não promete a ordem entre os dois |
| Conferir só que `stdin ended` está na cauda | Deixaria de conferir que a última da cauda é a última que o filho escreveu, que é o que o teste existe para conferir |
| Aumentar a janela do teste do §2.3 de 1 s para 5 s | Continua sendo uma aposta na velocidade, só que maior. O laço até dez de cada não aposta |
| Baixar o limite do §2.3 de dez para uma escrita | Conferiria menos concorrência entre o leitor e o escritor |
| Aumentar o `REPLACE_SECONDS` ou mudar o `StateFile.write` | É código do produto, e no produto ninguém segura o arquivo como o teste segura (§2.3) |
| Manter o `setup-python` | De 40 a 58 s por job Windows em cada execução (§4.3) |
| Guardar o Python base em cache | 1,9 s para restaurar, contra 3,3 s para baixar e extrair (§2.5), com dois passos a mais |
| O Python 3.12.10 da imagem | O CI conferiria outro Python que não o do instalador (D5, RFC-041 §2.8), e a imagem troca o 3.12.x sozinha |
| `needs: estatico` no `backend` | O resultado da suíte esperaria a análise estática. Os dois são sinais independentes |
| O job `backend` num workflow próprio | O check é nomeado pelo job, e não pelo workflow. O `concurrency` e as permissões do `ci.yml` valem para os três sem repetição (RFC-049 §13) |
| O ruleset também na `frontend` | O push direto de todo dia seria recusado até o commit passar noutra branch. E, com o admin no *bypass*, a regra não valeria para ninguém |
| A proteção de branch clássica, desde já | Não segue a branch padrão (`~DEFAULT_BRANCH`), e o roadmap pediu um ruleset. Fica como saída se o plano não deixar (§6) |
| O ruleset só no texto do RFC, sem o `ci.json` | Sem o arquivo, o teste de §7 não teria contra o que conferir os nomes dos jobs |
| Rodar os testes `embedded` neste job, já que o runtime está lá | São do RFC-051, à noite (D6) |

## 9. Critérios de pronto

**Onde medir:** no GitHub Actions do `SanthiagoBR/SolidVision`, em branches de medição criadas a partir do commit da implementação e apagadas no fim. Os tempos e os logs vêm da API, pelo `measure_ci.py` do RFC-049, estendido para o job `backend`. Do log do `backend`, ele lê o resumo do pytest e a duração do `initdb`, pela hora de cada linha. A referência do critério 1 é o `pilot_suite.py` rodado nesta máquina, no mesmo commit. O push é uma ação para fora, num repositório público. Quem o faz é o usuário, ou alguém com a autorização dele. Os critérios foram escritos antes dos números.

| critério | o número |
|---|---|
| **A suíte no runner dá os números de uma máquina de desenvolvimento** | No mesmo commit, o pytest do job `backend` e o do `pilot_suite.py` nesta máquina: passados `TBM` e `TBM`, pulados `TBM` e `TBM`, falhas 0 e 0. No piloto, em `9dbfcf6`, esta máquina deu 3 554 passados, nenhum pulado e nenhuma falha (§2.1). Cada diferença é explicada, uma por uma. Um teste que presume esta máquina sem precisar dela é corrigido no teste, a regra do RFC-049 §5 |
| **Dez execuções seguidas sem falha** | Dez commits vazios numa branch de medição, cada um depois de a execução anterior terminar, para que um não cancele o outro: os três jobs verdes em `TBM` de 10, sem nenhum *re-run*. Uma falha não é repetida. Ela é explicada e corrigida, e a contagem recomeça do zero |
| **O tempo, frio e quente** | O job `backend`, do começo ao fim: frio, sem venv e sem runtime, `TBM` min; quente, a mediana das dez execuções do critério 2, `TBM` min. Vêm junto o tempo de cada passo: o Python do nuget.org, o venv, o runtime, o `initdb` e o pytest. **Do push ao resultado dos três jobs, com os caches quentes:** `TBM` min. O alvo, escrito antes do número, é **10 min**, e esse número é o do item 1 do *Pronto* do roadmap. Sem ser critério, vem o Python base no `estatico`, contra os 49,5 s do `setup-python` (RFC-049 §15.2) |

## 10. Testes

- **`test_launcher_children.py` e `test_launcher_instance.py`**: as correções de §5.
- **`experiments/rfc-050-suite-do-backend/repeat_launcher_tests.py`**: os dois testes de hoje e os dois protótipos de §5, nas condições de §2.2 e §2.3: 300 vezes sem carga e 40 com as CPUs ocupadas. Antes das correções, os números de §2.2 e §2.3 são o controle. Depois, o esperado é zero falhas nas quatro séries: `TBM`.
- **`test_ci_workflow.py`**: as conferências de §7, cada uma com o seu defeito plantado. Os defeitos são um job renomeado, um check a menos no `ci.json`, um ator na lista de *bypass*, um `uses:` pela tag dentro da action, um `PYTHON_NUGET_SHA256` que não é hexadecimal e um `python-version:` com uma versão própria.
- **O ruleset**, depois de importado. Pela API, sem token: `GET /repos/SanthiagoBR/SolidVision/rules/branches/main` devolve a regra com os três nomes, comparada com o `ci.json`. E um PR de uma branch de medição com um teste vermelho plantado, aberto contra a `main`, mostra o merge bloqueado (`mergeable_state: blocked`) e é fechado sem merge. O PR é uma ação para fora: quem o abre é o usuário, ou alguém com a autorização dele.
- **O workflow** é testado pelos critérios de §9, no runner.

## 11. Riscos

- **O runner tem 4 vCPUs, e esta máquina tem 8 threads.** Um teste que depende de tempo, além dos dois de §5, pode falhar lá e não aqui. O critério 2 existe para mostrar isso.
- **O `TEMP` curto do runner** (`RUNNER~1`). O piloto rodou com um `TEMP` curto, e nada mudou (§2.1). Um teste novo que compare um caminho tirado do `TEMP` com um caminho resolvido daria diferença.
- **O teste do `icacls`** (`test_a_folder_denied_for_real_is_reported`) nega a leitura ao usuário do `USERNAME`. No runner, esse usuário é o `runneradmin`, um admin. Se o `icacls` recusar, o teste pula, e a diferença aparece no critério 1.
- **Os testes de página de código e de nomes curtos** (`test_embedded_code_page.py`) pulam quando a página ANSI não é a 1252 ou quando o volume não gera nomes 8.3. O runner é en-US (1252), e o `RUNNER~1` mostra que o `C:` gera nomes curtos. Um pulo inesperado aparece no critério 1.
- **O nuget.org fora do ar** deixa os dois jobs Windows vermelhos no primeiro passo. O `setup-python` tinha a mesma dependência, do `actions/python-versions`. Se acontecer, a saída é guardar a pasta do Python base em cache (§8).
- **O conda-forge fora do ar**, com o cache do runtime frio, deixa o `backend` vermelho. O cache reduz a exposição às vezes em que o lockfile muda ou em que a entrada sai, depois de 7 dias sem acesso numa branch.
- **A pasta do Python base mudar numa imagem nova.** O venv restaurado não abre, como o RFC-049 §10 já dizia do `setup-python`. A saída é a mesma: apagar a entrada do venv.
- **Dois jobs montando o mesmo venv** numa branch fria (§4.5). Se o `cache/save` do segundo falhar o job em vez de avisar, o critério 3 do caso frio mostra isso.
- **O ruleset e o D7** (§6). Até o D7, o ruleset protege a `main`, onde ninguém faz push. Depois dele, um push direto na `main` só passa com um commit que já passou noutra branch.
- **Um check que não roda fica pendente para sempre** (§2.6). O `ci.yml` não tem filtro de caminho (RFC-049 §4.1). Os checks do `workflow_dispatch` do RFC-051 não valem para o ruleset, e não precisam valer: o noturno não é obrigatório.
- **A suíte cresce.** Com 237 s nesta máquina, o `timeout-minutes: 30` tem folga de sete vezes.

## 12. Entregáveis

- `.github/workflows/ci.yml`: o job `backend`, o `env` do workflow com a versão e o SHA-256, e o `estatico` usando a action.
- `.github/actions/venv/action.yml` (novo): §4.2.
- `.github/rulesets/ci.json` (novo): §6.
- `backend/tests/launcher/test_launcher_children.py` e `backend/tests/launcher/test_launcher_instance.py`: §5.
- `backend/tests/test_ci_workflow.py`: §7.
- `README.md`: em *"Verificações de qualidade"*, a suíte passa a rodar no CI, no Windows Server 2025 com o Postgres embarcado. Um parágrafo diz que os três checks são obrigatórios na branch padrão e como importar o `ci.json`.
- `experiments/rfc-050-suite-do-backend/`: os scripts do piloto, com os `.json`, e o `measure_ci.py` de §9, com o `.json`. Os `.log` ficam fora do git.
- O [roadmap](ROADMAP-ci-cd.md): os `TBM` do RFC-050 e o que §14 muda nele.
- Este documento, com os `TBM` escritos de volta, e a linha dele no [README](README.md).

## 13. Fora deste RFC

- **Os testes `embedded` e `slow`** (RFC-051). O runtime já está no job, e o noturno vai guardá-lo pela mesma chave.
- **A ordem das linhas na cauda do launcher** (§2.2). É um comportamento do produto, decidido no RFC-036 §5.5. Se um dia importar, é assunto de um RFC próprio.
- **Outras regras na branch padrão**, como proibir o *force push* ou apagar a branch. O roadmap pediu só os checks.
- **O D7, e o selo do CI no README.** O selo mostra a branch padrão, e a `main` não tem o workflow.
- **`pytest-xdist`, cobertura e relatórios JUnit.**
- **Os dois testes que leem o `WARM_UP_MODELS` do `.env`** (RFC-048 §4.4). No runner não há `.env`, e o `check.py` os neutraliza.

## 14. O que isto muda no roadmap, e o que passa adiante

**No roadmap.** A causa conhecida de um dos testes instáveis não é a que falha. O `drain()` sempre terminou, e o que falha é a ordem dos dois pipes na cauda (§2.2). O outro teste falha pela contagem do escritor, e não por um leitor que vê meio arquivo (§2.3). Os dois são corrigidos só nos testes. O `setup-python` sai, e o Python base vem do nuget.org, pela action que monta o venv. O `PYTHON_VERSION` sobe para o `env` do workflow. O ruleset vai na `~DEFAULT_BRANCH` e fica no git, como `ci.json`. A `main` está 75 commits atrás, e não 70.

**Para o RFC-051.** O noturno usa a mesma action para o venv e a mesma chave para o `runtime\postgres`, e soma o `runtime\models`. Os checks do `workflow_dispatch` não valem para o ruleset. O D7 continua aberto.

**Para o RFC-052.** O `dist.mjs` usa o Python do venv, e o venv vem da action. O Python base fica em `%RUNNER_TOOL_CACHE%\python-nuget\<versão>`.

**Para quem renomear um job.** Muda-se o `ci.json` no mesmo commit, e o usuário importa o ruleset de novo. O teste de §7 recusa um sem o outro.

**Tamanho estimado:** um a dois dias, como no roadmap, mais a espera das execuções de §9.
