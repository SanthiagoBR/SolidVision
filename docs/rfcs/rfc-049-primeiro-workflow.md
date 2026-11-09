# RFC-049 — O Primeiro Workflow: Frontend e Análise Estática

**Status:** Proposto
**Depende de:** RFC-048 (o `check.py` e os grupos dele, o `make_venv.py`, os dois lockfiles e o `frontend/.gitattributes`), [roadmap de CI/CD](ROADMAP-ci-cd.md) (D1, D2, D3, D5 e D6)
**Bloqueia:** RFC-050, que acrescenta o job `backend` a este workflow e monta o venv pelo mesmo passo; RFC-052, que monta o venv do mesmo jeito para o build
**Migration:** nenhuma
**Medição:** o piloto de §2, nesta máquina (NVMe, Windows 11, Intel Model 126, 8 threads), em 09/11/2026, sobre o commit `8556d61`. A parte do venv saiu do `experiments/rfc-049-primeiro-workflow/pilot_venv_cache.py` (+ `.json`). A da implementação, `experiments/rfc-049-primeiro-workflow/measure_ci.py` (+ `.json`; o `.log`, fora do git), que lê as execuções do workflow pela API do GitHub (§8)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*, e vira número quando a implementação o escrever de volta, ao lado da frase. Os números sem marca vêm do piloto de §2 ou de outros RFCs, e dizem de qual.

---

## 1. Contexto

O RFC-048 juntou as conferências num comando só, com dois grupos pensados para um CI: `python scripts/check.py python` (ruff, black e mypy) e `python scripts/check.py frontend` (eslint, tsc, prettier e vitest). Nada no GitHub chama esse comando ainda. O repositório não tem `.github/`, e um push só é conferido se alguém tiver rodado as conferências na própria máquina antes.

Este RFC liga o primeiro workflow com os dois grupos, cada um num job. São as conferências que levam segundos com os caches quentes. A suíte do backend fica para o RFC-050, porque precisa do Postgres embarcado no runner, e o roadmap a marca como a etapa de maior risco. Separadas, as duas etapas dão um primeiro sinal verde que não depende desse risco.

Três perguntas ficaram para este RFC:

- **O que o runner precisa para chegar ao `check.py` como um desenvolvedor chega.** De um lado, o Node do `.node-version` e o `npm ci`. Do outro, o Python 3.12.9 e o venv dos dois lockfiles.
- **O que guardar entre uma execução e outra para caber no alvo de 10 min.** O roadmap pôs o cache do `pip`. O RFC-048 §15 deixou para cá a decisão sobre o `.mypy_cache`, porque o `mypy` com o cache vazio foi 46% do tempo das etapas.
- **Se o frontend roda no Linux** (D3).

## 2. O que o piloto mediu

Nesta máquina, em 09/11/2026, sobre o commit `8556d61`. Nada rodou no GitHub. O piloto leu o código e o lockfile do frontend, montou dois venvs dos lockfiles numa pasta temporária e consultou o repositório pela API pública. O roadmap partiu do que o RFC-048 sabia, e o piloto corrigiu três premissas dele (§13).

### 2.1 O `npm ci` não baixa o Electron, com ou sem a variável

O roadmap pôs `ELECTRON_SKIP_BINARY_DOWNLOAD=1` no job do frontend, para o `npm ci` não baixar o binário do Electron. Mas o pacote `electron` 44.9.0 não tem mais script de instalação: o `package.json` dele não tem `scripts`, e o `package-lock.json` não o marca com `hasInstallScript`. O binário é baixado na primeira vez que alguém faz `require("electron")` fora do Electron, pelo `index.js` do pacote. O `install.js`, que faz o download, não lê essa variável. E nenhum dos 36 arquivos de teste importa o módulo `electron`. Só o `scripts/electron.mjs`, do `npm start` e do `npm run dev`, faz isso.

No lockfile inteiro, só dois pacotes têm script de instalação. O `fsevents` é opcional e só existe no macOS. O `electron-winstaller` é uma dependência *peer* do `electron-builder-squirrel-windows`, e o npm não o instala. Então o `npm ci` do runner não roda script de pacote nenhum.

Isso vale também para a medição do RFC-048 (§10 e §16.1): lá, a variável também não fazia nada.

### 2.2 O lockfile do npm já traz os binários nativos do Linux

O Vite 8 empacota com o rolldown, e o CSS passa pelo lightningcss. Os dois são binários nativos, publicados como um pacote opcional por plataforma. O `frontend/package-lock.json` foi gerado no Windows, mas lista as variantes de todas as plataformas, inclusive `@rolldown/binding-linux-x64-gnu` e `lightningcss-linux-x64-gnu`. O `npm ci` no Ubuntu acha no lockfile o binário de que precisa. Se o lockfile tivesse só as variantes do Windows, o primeiro `import` do Vite falharia no Linux.

### 2.3 Um teste do frontend presume o Windows, e outro passa sem conferir nada

Seis arquivos de teste do frontend têm caminhos do Windows. Lidos junto com o código que testam:

| arquivo | no Linux | por quê |
|---|---|---|
| `electron/python.test.ts` | passa | O código recebe a plataforma como argumento, e o teste passa `"win32"`. O `path.win32` é escolhido pela plataforma dada, e não pela da máquina |
| `electron/log.test.ts` | passa | O `LOCALAPPDATA` do teste é uma pasta do `os.tmpdir()`, absoluta nas duas plataformas |
| `electron/events.test.ts`, `src/search/paths.test.ts` e `src/devices/share.test.ts` | passam | Os caminhos são só texto, e o módulo `path` não entra |
| `scripts/electron-builder-config.test.mjs` | **um teste falha, e outro passa sem conferir nada** | Abaixo |

O `stagingDirectory()`, de `scripts/staging.cjs`, segue a regra do backend para o `LOCALAPPDATA`: usa a variável se ela for um caminho absoluto, e a pasta do perfil se não for. O teste passa `C:\Users\ana\AppData\Local`. No Windows, é um caminho absoluto. No Linux, o `path.isAbsolute()` do POSIX diz que não é. O código então cai na pasta do perfil (`/home/runner/AppData/Local/...`), e o teste, que espera a montagem sob `C:\Users\ana\AppData\Local`, falha.

O teste seguinte confere que a montagem nunca fica dentro de uma raiz de dados. No Linux, ele passa pela mesma razão, sem conferir nada: a montagem nunca esteve sob o `C:\...` com que ele compara.

Isso é uma previsão feita pela leitura, e não uma medição: esta máquina não tem um Linux pronto. O critério 3 de §8 a confere. No Windows, o `npm test` dá **378 testes em 36 arquivos, todos passando**, no `8556d61`.

### 2.4 O venv: o que custa montá-lo, e o que custa guardá-lo

O roadmap pôs o cache do `pip` na chave dos dois lockfiles. Mas o RFC-048 já tinha um indício contra ele: com o cache vazio, o `make_venv.py` levou 390,3 s (§16.1), e com o cache desta máquina, 389 s (§16.6). O piloto mediu as duas coisas com controle. Primeiro, dois `make_venv.py` seguidos com o mesmo `PIP_CACHE_DIR`, vazio no primeiro e cheio no segundo. Depois, o venv do segundo empacotado com `tar --zstd`, o formato do `actions/cache`, apagado e desempacotado no mesmo lugar.

Pelo `experiments/rfc-049-primeiro-workflow/pilot_venv_cache.py` (`pilot_venv_cache.json`), com o Python 3.12.9 do RFC-048 §17.4 e o `tar` do Windows:

| fase do `make_venv.py` | cache do `pip` vazio (A) | cache do `pip` cheio (B, o controle) |
|---|---|---|
| Criar o venv | 17,7 s | 10,9 s |
| O `setuptools`, sozinho | 15,6 s | 10,3 s |
| Os dois lockfiles, numa chamada | **313,6 s** | **246,3 s** |
| Conferir o venv contra os lockfiles | 1,7 s | 1,4 s |
| **O `make_venv.py` inteiro** | **349,1 s** | **269,2 s** |

| o venv guardado inteiro | |
|---|---|
| O cache do `pip` depois de A | 222,5 MB, em 322 arquivos |
| O venv de B | 958,8 MB, em 35 251 arquivos |
| Empacotar o venv com `tar --zstd` | 179,7 s, num arquivo de 239,0 MB |
| Apagar o venv | 11,7 s |
| **Desempacotar no mesmo lugar** | **28,1 s** |
| `check.py ruff` com o venv desempacotado | Passa. A etapa `env` acha os 71 pacotes nas versões dos lockfiles |

**O cache do `pip` poupa 80 s de 349 s.** Com ele cheio, o `make_venv.py` ainda leva 269,2 s, quase todos instalando 35 mil arquivos, e não baixando 222 MB. **Guardar o venv inteiro troca esses 269 s por 28 s para desempacotar**, mais o download de 239 MB no runner. O custo de empacotar, 180 s aqui, só aparece quando não há venv guardado: quando um lockfile muda, ou numa branch que começa fria (§4.4). O venv restaurado no mesmo caminho funciona, e no runner o caminho é sempre o mesmo: a pasta do checkout.

O tempo de A, 349,1 s, fica abaixo dos 390,3 s do RFC-048 §16.1, medidos nesta mesma máquina e também com o cache vazio. É a variação entre dois dias. A comparação que vale é a de A com B, rodados um depois do outro.

### 2.5 O cache do `mypy`

O `.mypy_cache` desta máquina tem 122,7 MB, em 18 arquivos. No clone novo do RFC-048 (§16.2), o `mypy` levou 230,8 s com o cache vazio e 6,6 s com ele quente. O cache serve também de um commit para outro. O `mypy` confere cada arquivo contra o que guardou e só refaz o que mudou. No RFC-048 §16.4, com o cache quente e uma função errada plantada em `backend/app`, o comando inteiro levou 13,9 s até a falha.

### 2.6 O repositório e o runner

- O repositório `SanthiagoBR/SolidVision` é **público**, e a branch padrão é a `main`. A `main` está **72 commits** atrás da `frontend`. O roadmap dizia 70, mas os dois commits do RFC-048 entraram depois. Não há `.github/`.
- O manifesto do `setup-python` (`actions/python-versions`) tem o **3.12.9 para Windows x64**. A versão 3.12 mais nova do manifesto é a 3.12.16.
- As actions da organização `actions` têm, hoje, estas versões:

| action | tag mais nova | commit |
|---|---|---|
| `actions/checkout` | `v7.0.1` | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/setup-node` | `v7.1.0` | `949feb2413d6458794dcd2491c4babbbce0c15c1` |
| `actions/setup-python` | `v7.1.0` | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` |
| `actions/cache` | `v6.1.0` | `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` |

### 2.7 O defeito plantado do Prettier não chega a um commit

No RFC-048 §16.4, o defeito plantado do Prettier era o `frontend/src/main.tsx` gravado com CRLF. Num repositório de teste com o `frontend/.gitattributes` (`* text=auto eol=lf`) e `core.autocrlf=true`, um arquivo CRLF foi gravado **LF** no commit, e o `git add` avisou da troca. Então nenhum push leva um CRLF para dentro de `frontend/`, e o defeito plantado no CI tem de ser outra formatação (§8).

## 3. Decisões

1. **Um workflow, `.github/workflows/ci.yml`, com dois jobs**, o `frontend` e o `estatico`. Cada um chama um grupo do `check.py`, e mais nada. A lista das conferências fica só no `check.py` (RFC-048, decisão 1).
2. **Gatilhos:** `push` em qualquer branch e `pull_request`. O filtro `branches: ["**"]` deixa as tags de fora, porque elas são do RFC-054. Não há filtro de caminhos (§4.1).
3. **Uma execução por ref:** `concurrency` pelo `github.ref`, e a execução nova cancela a anterior.
4. **Só leitura:** `permissions: contents: read` no workflow, e nenhum job pede mais.
5. **Cada action fixada pelo commit**, com a tag num comentário, e nunca pela tag. Só entram actions da organização `actions` (§4.5).
6. **O frontend no `ubuntu-24.04`** (D3): o Node do `frontend/.node-version` pelo `setup-node`, o cache do npm, e sem `ELECTRON_SKIP_BINARY_DOWNLOAD` (§2.1).
7. **A análise estática no `windows-2025`** (D2): o Python 3.12.9 do `setup-python`, e o venv do `make_venv.py`.
8. **Guarda-se o venv inteiro, e não o cache do `pip`** (§2.4), na chave dos dois lockfiles, do `make_venv.py` e da versão do Python. Numa execução que não acha o venv, o `make_venv.py` o monta, e ele é salvo logo em seguida.
9. **Guarda-se o `.mypy_cache`**, uma entrada por commit. Cada execução restaura a entrada mais recente com os mesmos lockfiles.
10. **O teste do `stagingDirectory` passa a usar um caminho absoluto da máquina que o roda**, como o `log.test.ts` já faz, e o job do frontend fica no Linux (§5). O D3 mandava o job para o Windows se um teste presumisse caminhos do Windows. Este presume, mas não precisa do Windows.
11. **O `check.py` não muda** (§4.6).
12. **Um teste da suíte confere o YAML** (§6): as actions fixadas pelo commit, a versão do Python igual à do `check.py`, os grupos que o `check.py` conhece, e só leitura.
13. **Nenhum check é obrigatório ainda.** O *ruleset* que os torna obrigatórios é do RFC-050, com os três jobs.

## 4. O workflow

### 4.1 Quando roda

```yaml
on:
  push:
    branches: ["**"]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
```

**Sem `paths-ignore`.** Muitos commits deste repositório só mudam `docs/`: um RFC escrito, ou um número escrito de volta. Pular esses commits pouparia minutos que não custam nada, porque o repositório é público. E o RFC-050 torna os jobs obrigatórios. Um check obrigatório de um workflow que não rodou fica pendente para sempre, e o merge não acontece.

**Duas execuções num PR da mesma origem.** Um push numa branch que tem PR aberto dispara uma execução por evento, com o mesmo resultado. O PR de um fork só dispara a segunda. Fica assim: as execuções não custam nada, e evitar a duplicada pediria uma condição em cada job.

**Cancelar.** Dois pushes seguidos na mesma branch cancelam a execução do primeiro. O commit do primeiro fica sem resultado, e o segundo confere a árvore inteira, que contém o primeiro. Vale também para a branch padrão. Isso só pode mudar quando o D7 for decidido. É também por isso que os defeitos plantados de §8 vão um por branch.

**Limite de tempo.** `timeout-minutes: 15` no `frontend` e `30` no `estatico`. O default do GitHub é 360 min, e um passo travado comeria seis horas. Nesta máquina, o grupo `frontend` leva 48,2 s e o `npm ci`, 35,2 s (RFC-048 §16). O `estatico` frio leva perto de 13 min: 349 s para montar o venv e 180 s para empacotá-lo (§2.4), mais 248 s do grupo `python` com o `mypy` do zero (RFC-048 §16.2).

### 4.2 O job `frontend`

```yaml
  frontend:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1  # v7.1.0
        with:
          node-version-file: frontend/.node-version
          cache: npm
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
        working-directory: frontend
      - run: python3 scripts/check.py frontend
```

- O `python3` é o do Ubuntu 24.04. A parte do frontend do `check.py` só usa a biblioteca padrão e não importa o `vendor_python` (RFC-048 §4.3).
- O `.node-version` diz `24`, e o `setup-node` instala o 24.x mais novo do dia. A etapa `env` confere a versão maior, como numa máquina de desenvolvimento. O npm é o que vem com esse Node.
- O cache do `setup-node` é a pasta de pacotes do npm, na chave do `package-lock.json`, e não o `node_modules`: o `npm ci` apaga o `node_modules` antes de instalar.
- Um checkout no Linux escreve LF em tudo, com ou sem o `.gitattributes`.

### 4.3 O job `estatico`

```yaml
  estatico:
    runs-on: windows-2025
    timeout-minutes: 30
    env:
      PYTHON_VERSION: "3.12.9"
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0  # v7.1.0
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - id: venv
        uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
        with:
          path: .venv
          key: venv-${{ env.PYTHON_VERSION }}-${{ hashFiles('scripts/python-win-64.lock', 'scripts/python-dev-win-64.lock', 'scripts/make_venv.py') }}
      - if: steps.venv.outputs.cache-hit != 'true'
        run: python scripts/make_venv.py
      - if: steps.venv.outputs.cache-hit != 'true'
        uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
        with:
          path: .venv
          key: ${{ steps.venv.outputs.cache-primary-key }}
      - uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
        with:
          path: .mypy_cache
          key: mypy-${{ hashFiles('scripts/python-win-64.lock', 'scripts/python-dev-win-64.lock') }}-${{ github.sha }}
          restore-keys: mypy-${{ hashFiles('scripts/python-win-64.lock', 'scripts/python-dev-win-64.lock') }}-
      - run: .\.venv\Scripts\python.exe scripts/check.py python
      - if: ${{ !cancelled() }}
        uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9  # v6.1.0
        with:
          path: .mypy_cache
          key: mypy-${{ hashFiles('scripts/python-win-64.lock', 'scripts/python-dev-win-64.lock') }}-${{ github.sha }}
```

- O `make_venv.py` roda com o `python` que o `setup-python` pôs no `PATH`, o 3.12.9 base, como §6 do RFC-048 pede.
- O `check.py` roda pelo caminho do Python do venv, e não por um `python` achado no `PATH`. O RFC-048 §17.12 mostrou o que um `python` sem caminho resolve no Windows.
- O checkout no Windows sai com `core.autocrlf=true`, o padrão do Git do runner. O backend fica CRLF, e o RFC-048 §16.1 mediu que o ruff, o black e o `mypy` passam assim.
- **A versão do Python está em dois lugares**: no `PYTHON_VERSION` do job, que o `setup-python` e a chave do venv leem, e no `PYTHON_VERSION` do `check.py`. A etapa `env` reprova se os dois discordarem, e o teste de §6 acusa isso antes do push.

### 4.4 O que fica guardado entre execuções

**Salvar à parte.** O `actions/cache` inteiro só salva no fim, e só se o job inteiro passou. Um venv montado numa execução em que o `mypy` reprovou seria jogado fora, e a execução seguinte o montaria de novo. Por isso o workflow usa as duas metades da action, `restore` e `save`:

- **O venv é salvo logo depois do `make_venv.py`**, e só quando a execução o montou. O `make_venv.py` confere o venv contra os dois lockfiles no fim e falha se algo divergir (RFC-048 §6, passo 5). Então só um venv conferido é guardado.
- **O `.mypy_cache` é salvo depois do `check.py`, passando ou não**, porque o cache de um `mypy` que achou erro continua válido. Só não é salvo numa execução cancelada.

| cache | chave | tamanho |
|---|---|---|
| O venv (`.venv`) | `venv-<Python>-<hash dos dois lockfiles e do make_venv.py>` | 239,0 MB compactado, e 958,8 MB em 35 251 arquivos no disco (§2.4) |
| O `.mypy_cache` | `mypy-<hash dos dois lockfiles>-<commit>`. Cada execução restaura a entrada mais recente com o mesmo prefixo | 14,3 MB compactado por entrada, e 122,7 MB no disco, nesta máquina |
| Os pacotes do npm (`setup-node`) | O hash do `package-lock.json` | Os pacotes do lockfile, sem o binário do Electron (§2.1) |

**O limite.** O GitHub guarda até 10 GB de cache por repositório. Uma entrada sem acesso por 7 dias sai, e acima do limite saem primeiro as de acesso mais antigo. O venv é lido a cada execução, então as entradas que saem primeiro são as do `mypy` de commits velhos. Com 14,3 MB cada, cabem centenas delas. O RFC-051 vai somar perto de 1 GB: os 881 MB dos modelos e os 123 MB do Postgres.

**O escopo.** Uma execução lê as entradas da própria branch e as da branch padrão. A execução de um PR lê as da branch base, e não as da branch de origem. Enquanto a `main` não tiver este workflow (D7), ela não tem cache nenhum. Então **toda branch nova começa fria**, e a primeira execução de um PR também.

### 4.5 As actions e as versões delas

Cada `uses:` aponta para um commit de 40 caracteres, com a tag ao lado num comentário. Uma tag é um ponteiro que o dono do repositório da action pode mover, e uma action comprometida pode mover as tags para um código novo. Foi o que aconteceu com a `tj-actions/changed-files` em março de 2025. Um commit não se move. É a mesma regra dos lockfiles (`===` e o `sha256`) e a do D2 (a imagem fixada, e nunca a `-latest`).

Só entram actions da organização `actions`: `checkout`, `setup-node`, `setup-python` e `cache`, com as metades `cache/restore` e `cache/save` no mesmo commit. As versões são as de §2.6. Atualizar uma action é trocar o commit à mão, num commit que diz por quê. O teste de §6 recusa uma tag.

### 4.6 O `check.py` não muda

O RFC-048 §15 deixou os grupos do log do GitHub (`::group::`) para quando o log pedisse. Este RFC não os põe. O comando para na primeira falha, então a saída da ferramenta que falhou é sempre a última coisa antes do resumo, e o log de um passo que falhou abre expandido. Com grupos, essa saída ficaria dentro de um grupo fechado.

## 5. O teste que presume o Windows

Os dois testes de *"the montagem's place"*, em `frontend/scripts/electron-builder-config.test.mjs`, passam a montar o `LOCALAPPDATA` com uma pasta absoluta da máquina que os roda, `join(tmpdir(), "ana", "AppData", "Local")`, como o `log.test.ts` faz para a mesma regra. O segundo teste troca o `"\\"` por `sep`. Assim os dois conferem a regra nas duas plataformas, e o segundo passa a conferir alguma coisa no Linux. O `staging.cjs` não muda: só o `npm run dist` o usa, e esse comando só roda no Windows.

O D3 dizia: *"Se algum teste presumir caminhos do Windows, o job vai para o `windows-2025`"*. Este teste não precisa do Windows. Ele precisa de um caminho absoluto, e por acaso escreveu esse caminho do jeito do Windows. Mudar o job de máquina esconderia o mesmo tipo de descuido no próximo teste. A regra fica assim: **um teste que presume o Windows sem precisar dele é corrigido no teste. Um teste que precisa de uma API do Windows leva o job para o `windows-2025`.** Pular o teste fora do Windows (`it.skipIf`) também foi descartado: a regra é a mesma nas duas plataformas, e pular confere menos e muda a contagem.

## 6. O YAML conferido pela suíte

`backend/tests/test_ci_workflow.py` lê o `.github/workflows/ci.yml` como texto e confere quatro coisas:

- cada `uses:` termina em `@` e um commit de 40 caracteres hexadecimais, com um comentário `# vX.Y.Z`;
- o `PYTHON_VERSION` do job é o do `check.py`;
- cada chamada do `scripts/check.py` só usa nomes que o `choose()` dele aceita;
- `permissions` tem `contents: read`, e o arquivo não tem nenhum `: write`.

O teste não usa um parser de YAML. O PyYAML está no venv, porque o `transformers` o pede, mas sem tipos, e o `mypy --strict` pediria uma exceção no `pyproject.toml` só para ele. E cada uma das quatro coisas cabe numa linha do arquivo. O teste roda na suíte padrão: na máquina de quem muda o YAML, e no runner a partir do RFC-050.

## 7. Alternativas consideradas

| alternativa | por que não |
|---|---|
| Um job só, no Windows, com os dois grupos | As conferências rodariam uma depois da outra, e o frontend esperaria um venv de que não precisa. Dois jobs começam juntos e dão duas respostas |
| Um passo do YAML por conferência | A lista ficaria em dois lugares, o que o RFC-048 existe para evitar. Mostraria cada conferência como um passo, mas §4.6 já põe a falha no fim do log |
| O cache do `pip` na chave dos lockfiles, como o roadmap dizia | §2.4: com ele cheio, o venv ainda leva 269,2 s para montar. Desempacotar o venv guardado leva 28,1 s |
| O venv e também o cache do `pip` | O cache do `pip` só serviria quando um lockfile muda. Pouparia 80 s numa montagem que raramente acontece, e somaria 222,5 MB ao limite de 10 GB |
| O `cache: pip` do `setup-python` | É o mesmo cache do `pip`, com a chave dos arquivos que se indicar |
| O venv numa *composite action* já agora | Só um job monta o venv. O RFC-050, que acrescenta o segundo, extrai a action, e o RFC-052 a usa |
| As actions pela tag (`@v7`) | §4.5 |
| Dependabot para as actions | O roadmap deixou as atualizações automáticas de fora. Os commits das actions mudam à mão, e o teste de §6 os confere |
| Um runner próprio, na máquina do usuário | O objetivo é uma máquina que não é a de ninguém. E, num repositório público, o PR de um fork rodaria o código de qualquer pessoa nessa máquina |
| `paths-ignore` em `docs/**` | §4.1: os checks obrigatórios do RFC-050 ficariam pendentes |
| `windows-latest` e `ubuntu-latest` | D2: uma imagem `-latest` muda sozinha |
| O frontend no `windows-2025`, como o D3 mandava | §5 |
| Pular o teste fora do Windows | §5 |
| `::group::` no `check.py` | §4.6 |
| O `act`, para rodar o workflow na máquina | Ele roda os jobs em containers Linux. O job `estatico` é do Windows, e o objetivo é justamente o runner |

## 8. Critérios de pronto

**Onde medir:** no GitHub Actions, no repositório `SanthiagoBR/SolidVision`, em branches de medição criadas a partir do commit da implementação e apagadas no fim. Os tempos vêm da API do GitHub, lidos pelo `measure_ci.py`: o `created_at` da execução e o `started_at` e o `completed_at` de cada job e de cada passo. O push é uma ação para fora, num repositório público. Quem o faz é o usuário, ou alguém com a autorização dele. Os critérios foram escritos antes dos números.

| critério | o número |
|---|---|
| **Do push ao resultado dos dois jobs, com os caches quentes** | `TBM` min, do `created_at` da execução ao `completed_at` do último job. É a mediana de três execuções seguidas, de commits vazios, numa branch cujo cache já foi aquecido. O alvo, escrito antes do número, é **10 min**. Sem ser critério, vêm junto a primeira execução, com os caches frios, e o tempo de cada passo. Entre eles, o `setup-python` e os tempos de restaurar o venv, de montá-lo com o `make_venv.py` e de salvá-lo. É a comparação de §2.4 feita no runner: se lá restaurar o venv custar mais que montá-lo, a decisão 8 se inverte |
| **Um defeito plantado por etapa, uma branch por defeito** | Cada defeito deixa vermelho o job dele, e só o dele, e a primeira etapa vermelha do `check.py` é a do defeito. Testado, e não presumido: `TBM` de 8 |
| **O frontend no Linux dá os testes do Windows** | O mesmo número de testes passando que o `npm test` local, no mesmo commit: `TBM`. Antes, numa branch de controle sem a correção de §5, o que §2.3 prevê: um teste falhando, o do `stagingDirectory`, e o resto passando: `TBM`. Se aparecer outra diferença, ela é explicada, ou o job vai para o `windows-2025` (D3) |

Os defeitos plantados. O do `pytest` não entra, porque o `pytest` é do RFC-050:

| etapa | defeito | job vermelho |
|---|---|---|
| `env` | Em dois pushes na mesma branch. O primeiro tira o `python-dev-win-64.lock` da chave do venv, e a execução salva o venv com essa chave. O segundo troca uma ferramenta de versão nesse lockfile, com o hash da outra versão. O venv restaurado é o velho | `estatico` |
| `ruff` | Um import sem uso em `backend/app` | `estatico` |
| `black` | Uma linha mal formatada em `scripts/` | `estatico` |
| `mypy` | Um `str` devolvido onde a assinatura diz `int` | `estatico` |
| `eslint` | Um hook do React chamado dentro de um `if` | `frontend` |
| `tsc` | Um `number` passado onde a função pede `string` | `frontend` |
| `prettier` | Uma constante exportada mal formatada num arquivo novo de `frontend/src`. O CRLF do RFC-048 não chega a um commit (§2.7) | `frontend` |
| `vitest` | Um `expect` que não vale | `frontend` |

Cada branch nasce do commit da implementação e, como §4.4 explica, começa fria. Então as oito execuções também medem o caso frio.

## 9. Testes

- **`backend/tests/test_ci_workflow.py`** (novo): as quatro conferências de §6. Cada uma falha num YAML com o defeito correspondente: uma action pela tag, outro Python, um grupo que o `check.py` não conhece e um `contents: write`.
- **`frontend/scripts/electron-builder-config.test.mjs`**: os dois testes de §5.
- **O workflow em si** é testado pelos critérios de §8, no runner. Não há como rodá-lo na máquina (§7, o `act`).

## 10. Riscos

- **Restaurar o venv no runner pode custar mais que no piloto.** O disco, a rede e o antivírus do runner não são os desta máquina. O critério 1 dá os dois tempos no runner, e a decisão 8 se inverte se restaurar custar mais que montar.
- **Um venv restaurado acha o Python base pelo caminho** (o `home` do `pyvenv.cfg`), que é a pasta onde o `setup-python` o instala. Se uma imagem nova mudar essa pasta, o Python do venv não abre, e o job falha antes da etapa `env`. A saída é apagar a entrada do cache, e a execução seguinte monta outro venv.
- **O cache do `mypy` pode não servir a um checkout novo.** Um checkout novo muda a data de todo arquivo. O que §2.5 descreve vale se o `mypy`, ao ver uma data diferente, comparar o conteúdo antes de refazer o arquivo. Se não comparar, o quente vira frio (230,8 s nesta máquina), e o critério 1 mostra isso pelo tempo da etapa.
- **Toda branch nova começa fria** enquanto a `main` não tiver o workflow (§4.4, D7).
- **O `setup-python` baixa o 3.12.9 a cada execução.** Se o `actions/python-versions` deixar de publicá-lo, há o pacote do python.org no NuGet, o mesmo do RFC-048 §17.4.
- **A imagem `windows-2025` é atualizada toda semana.** Se o Git dela mudar o `core.autocrlf`, o backend passa com LF ou com CRLF (RFC-048 §16.1), e o frontend é LF pelo `.gitattributes`.
- **Outro Node 24.x no runner.** A etapa `env` só confere a versão maior, uma decisão do RFC-048. Um teste que dependa do *patch* do Node aparece como diferença no critério 3.
- **O PR de um fork.** Num repositório público, o GitHub pede aprovação antes de rodar o workflow de quem contribui pela primeira vez. O token desse PR é só de leitura, e este workflow não usa segredo nenhum.
- **Um commit cancelado fica sem resultado** (§4.1).

## 11. Entregáveis

- `.github/workflows/ci.yml` (novo): o workflow de §4.
- `frontend/scripts/electron-builder-config.test.mjs`: a correção de §5.
- `backend/tests/test_ci_workflow.py` (novo): §6.
- `README.md`: em *"Verificações de qualidade"*, um parágrafo dizendo que cada push roda o `check.py frontend` no Ubuntu e o `check.py python` no Windows Server 2025, e que a suíte só vai rodar no CI com o RFC-050.
- `experiments/rfc-049-primeiro-workflow/measure_ci.py`, com o `.json`. O `.log` fica fora do git.
- O [roadmap](ROADMAP-ci-cd.md): os `TBM` do RFC-049 e o que §13 muda nele.
- Este documento, com os `TBM` escritos de volta, e a linha dele no [README](README.md).

## 12. Fora deste RFC

- **O `pytest`**, o job `backend`, o cache do `runtime\postgres` e os dois testes instáveis do launcher (RFC-050).
- **Os checks obrigatórios e o *ruleset*** (RFC-050). Os nomes dos jobs deste RFC, `frontend` e `estatico`, viram os nomes dos checks, e renomear um job depois quebra o *ruleset*.
- **A *composite action* do venv** (RFC-050, quando o segundo job a pedir).
- **Os testes `slow` e `embedded`** e o `schedule` (RFC-051).
- **Um selo do CI no README.** Fica para quando houver um check obrigatório.
- **O D7.** Este RFC funciona em qualquer branch, e §4.4 diz o que a decisão muda nos caches.

## 13. O que isto muda no roadmap, e o que passa adiante

**No roadmap.** O `ELECTRON_SKIP_BINARY_DOWNLOAD=1` sai do job do frontend, porque não faz nada no Electron 44 (§2.1). O cache do `pip` dá lugar ao do venv inteiro, e o `.mypy_cache` também passa a ser guardado (§2.4 e §2.5). O D3 ganha uma distinção: um teste que presume o Windows sem precisar dele é corrigido no teste, e só um teste que precisa do Windows leva o job para lá (§5). Os defeitos plantados são oito, e não nove, porque o `pytest` não está aqui, e o do Prettier é uma formatação, e não um CRLF (§2.7). A `main` está 72 commits atrás, e não 70.

**Para o RFC-050.** O job `backend` entra neste mesmo `ci.yml`, com o mesmo passo do venv do `estatico`. Com dois jobs usando o passo, ele vira uma *composite action*, e o RFC-052 a usa também. A chave do venv é a mesma, então os dois jobs dividem a entrada. O teste de §6 passa a valer para o job novo sem mudança. O *ruleset* exige `frontend`, `estatico` e `backend` pelo nome.

**Para o RFC-051.** São perto de 1 GB a mais no limite de 10 GB (§4.4). E, sem o D7, a `main` não tem caches, além de não ter o `schedule`.

**Para quem mexer nas actions.** O commit muda à mão, e o teste de §6 recusa uma tag.

**Tamanho estimado:** meio dia, como no roadmap, mais a espera das execuções de §8.
