# Roadmap — CI/CD

Este roadmap cobre o trabalho entre o projeto de hoje, em que toda conferência roda à mão numa das duas máquinas de desenvolvimento, e um SolidVision em que **todo push é conferido por uma máquina que não é a de ninguém, e uma tag produz um rascunho de release com o instalador já testado**. Ele acaba aí. Assinatura de código, atualização automática e as medições do acervo real ficam de fora (ver [Fora deste roadmap](#fora-deste-roadmap)).

São sete RFCs curtos, do 048 ao 054, no formato do [roadmap do frontend](ROADMAP-frontend.md#o-prazo-e-o-que-ele-obriga): cada um escrito no começo da etapa, com as decisões, no máximo três critérios `TBM` e o que ficou de fora. Status segue as [convenções](README.md#convenções): todo número é `TBM` até a implementação escrevê-lo de volta.

Escrito em 2026-11-08, sobre o commit `21e0af5` da branch `frontend`.

**Cortado em 2026-11-11:** o roadmap para no RFC-050, e a primeira release sai à mão ([O corte de 11/11/2026](#o-corte-de-11112026)).

---

## Por que agora

As conferências existem, e são boas: `mypy --strict`, a suíte padrão, os testes de arquitetura, a deriva do OpenAPI conferida pelos dois lados, e o `check_package.py`, que confere árvore, lockfile, DLLs e vetores antes de o instalador existir. O que falta é **quem as rode sempre, no mesmo ambiente**. Hoje cada uma depende da máquina de quem roda, e as duas máquinas já discordam: a memória do projeto registra erros de `mypy` que só uma delas mostra, duas falhas do `test_vendor_python.py` que vêm do venv e não do código, e um `format:check` que falha num checkout com `core.autocrlf=true`.

Um runner do CI é uma terceira máquina, limpa a cada execução. Ele não acha defeito novo no código. Acha o que só funciona aqui.

---

## Ponto de partida

### O que já existe

| Conferência | Comando | Onde roda hoje |
|---|---|---|
| Lint do backend | `ruff check` | À mão. O `pyproject.toml` tem `fix = true`, então o comando **corrige** em vez de falhar, e um CI precisa de `--no-fix` |
| Formatação do backend | `black --check` | À mão, e não no venv desta máquina: o black se recusa a rodar no Python 3.12.5 |
| Tipos do backend | `mypy` (strict, `backend/app`, `tests`, `dataset_tools`) | À mão |
| Suíte padrão | `pytest` (sem `slow` e sem `embedded`) | À mão. 3 a 6 min nesta máquina, contra o Postgres do `.env` |
| Postgres embarcado | `pytest -m embedded` | À mão. Pula sem `runtime\postgres` |
| Modelos reais | `pytest -m slow` | À mão. Pula sem `runtime\models`. Confere os vetores do RFC-035 **bit a bit** |
| Frontend | `npm run lint`, `typecheck`, `format:check`, `test` | À mão |
| Contrato da API | O `pytest` confere o `openapi.json`, o `npm test` confere os tipos gerados dele | Dentro das duas suítes |
| Instalador | `npm run dist`, com o `check_package.py` no meio ([RFC-041 §7.3](rfc-041-empacotamento-e-instalador.md)) | À mão. 37 min e 1,03 GB |

Não existe `.github/`, nem outro arquivo de CI. O remote é `github.com/SanthiagoBR/SolidVision`, e o repositório é **público**: os runners padrão do GitHub Actions, Windows incluído, não custam minutos.

### O que impede de ligar hoje

Cada item vem da memória do projeto ou do repositório, e cada um faria o primeiro CI nascer vermelho por um motivo que não é defeito do código:

- **O venv saiu do lockfile.** O `backend/requirements.txt` não fixa versão nenhuma. O venv desta máquina tem SQLAlchemy 2.1.1, e o `scripts/python-win-64.lock` diz 2.0.51. Daí vêm os 8 erros de `mypy` que a outra máquina não mostra. O mesmo vale para o `torch` (2.14 aqui, 2.13 no lockfile), e daí vêm as duas falhas do `test_vendor_python.py`.
- **As ferramentas de desenvolvimento não têm versão.** `pytest`, `ruff`, `mypy` e `black` não aparecem em lockfile nenhum. O lockfile do Python é só o do runtime.
- **O `frontend/.gitattributes` nunca foi commitado.** Num checkout com `core.autocrlf=true`, que é o padrão do Git no Windows e dos runners Windows, todo arquivo do `frontend/` sai CRLF e o Prettier reprova.
- **Dois testes do launcher são instáveis sob carga** (`test_the_last_fifty_lines_are_kept` e `TestTheStateFile::test_a_reader_never_sees_half_a_file`). Passam sozinhos. Num CI, um teste instável ensina a apertar *re-run*.
- **A `main` do GitHub está 70 commits atrás da `frontend`.** Isso importa para o CI, porque o gatilho `schedule` do GitHub Actions só roda na branch padrão (D7).

Um item que **não** atrapalha: o teste que falha com `WARM_UP_MODELS=true` no `.env`. Sem `.env`, como num runner, o default de um checkout é `false` (`settings.py`), e o teste passa.

### O que o CI herda em tamanho e tempo

| Peça | Tamanho ou tempo | Fonte |
|---|---|---|
| `runtime\` inteiro, nesta máquina | 1,0 GB | `du`, 2026-11-08 |
| Runtime do PostgreSQL (16.x + pgvector 0.8.6, conda-forge win-64) | 123 MB | [RFC-033a](rfc-033a-linha-de-base-do-postgres-embarcado.md) |
| Pesos dos dois modelos | 881,1 MB | [RFC-035](rfc-035-modelos-offline.md) |
| Instalador NSIS | 1,03 GB, em 37 min | [RFC-041](rfc-041-empacotamento-e-instalador.md) |
| Suíte padrão | 3 a 6 min nesta máquina; ~70 s nos RFCs | Memória do projeto |

### O que depende do Windows

16 módulos de `backend/app` chamam APIs do Windows (`windll`, `winreg`, identidade de volume, o cluster embarcado, o launcher, o Explorer), e 28 arquivos de teste têm ramos por plataforma. O produto só existe para Windows. Isso decide o runner do backend (D2).

---

## Decisões a travar

| # | Decisão | Recomendação | Por quê |
|---|---|---|---|
| D1 | Onde roda | **GitHub Actions** | O remote já é o GitHub, e o repositório público não paga minutos, Windows incluído. Não há servidor para manter |
| D2 | O runner do backend e do instalador | **`windows-2025`**, com a imagem fixada, nunca `windows-latest` | O produto é só Windows, e 28 arquivos de teste têm ramos por plataforma. **Rejeitado: Linux com o Windows simulado**, que testaria os dublês e não o produto. Uma imagem `-latest` muda sozinha, como a tag `pg16` que o [RFC-033a](rfc-033a-linha-de-base-do-postgres-embarcado.md) recusou |
| D3 | O runner do frontend | **`ubuntu-24.04`** | O `vitest`, o `eslint` e o `tsc` não tocam API do Windows, e o runner Linux sobe mais rápido. Se algum teste presumir caminhos do Windows, o job vai para o `windows-2025` (medido no RFC-049) → **Fica no `ubuntu-24.04`.** Um teste presumia o Windows sem precisar dele, e foi corrigido no teste; no Linux, passam os mesmos 378 testes ([RFC-049 §5 e §14.4](rfc-049-primeiro-workflow.md#144-o-frontend-no-linux)). A regra ficou assim: um teste que presume o Windows sem precisar dele é corrigido no teste, e só um teste que precisa de uma API do Windows leva o job para lá |
| D4 | O banco no CI | **O Postgres embarcado**, materializado do `postgres-win-64.lock` e guardado em cache pelo hash do lockfile | No GitHub Actions, *service containers* só existem em runner Linux. E o embarcado é a versão que vai no instalador. O Docker continua sendo o banco do dia a dia |
| D5 | As versões do Python no CI | **Python 3.12.9**, o venv montado do `python-win-64.lock`, mais um lockfile novo para as ferramentas de desenvolvimento | O CI confere as versões que vão para o usuário, e não as de um venv que derivou. O 3.12.9 é o do pacote ([RFC-041 §2.8](rfc-041-empacotamento-e-instalador.md)), e o black roda nele |
| D6 | O que roda quando | **A cada push e PR:** análise estática, suíte padrão, frontend. **À noite e sob demanda:** `embedded` e `slow`. **Numa tag:** instalador, teste de fumaça e rascunho de release | O que leva minutos roda sempre. O que baixa 1 GB ou leva 40 min roda quando vale a pena |
| D7 | A branch padrão | **A decidir pelo usuário.** Recomendação: levar a `frontend` para a `main` antes do RFC-051 | O `schedule` só roda na branch padrão, e a `main` está 70 commits atrás (72 no piloto do RFC-049). E, sem o workflow na `main`, toda branch nova começa com os caches frios ([RFC-049 §4.4](rfc-049-primeiro-workflow.md#44-o-que-fica-guardado-entre-execuções)). A outra saída é trocar a branch padrão do repositório → **Decidida em 11/11/2026: a `frontend` vai para a `main`**, por fast-forward ([O corte de 11/11/2026](#o-corte-de-11112026)) |
| D8 | Quem publica a release | **Uma pessoa.** O CI cria o rascunho | O instalador não é assinado ([RFC-041 §7.5](rfc-041-empacotamento-e-instalador.md#75-sem-assinatura-o-smartscreen)), e publicar é uma ação para fora que não se desfaz por completo |

---

## RFC-048 — As conferências num comando só, com as versões fixadas

Nada no GitHub ainda. O CI só pode chamar o que um desenvolvedor já roda, do mesmo jeito, na própria máquina.

**Escopo**
- Um comando, por exemplo `python scripts/check.py`, que roda na ordem: `ruff check --no-fix`, `black --check`, `mypy`, `pytest`, e no `frontend/` `lint`, `typecheck`, `format:check` e `test`. Para na primeira falha, ou com `--all` roda tudo e resume. O CI vai chamar este comando, e não repetir a lista em YAML.
- Um lockfile das ferramentas de desenvolvimento (`pytest`, `pytest-cov`, `pytest-mock`, `ruff`, `mypy`, `black`), no formato do `python-win-64.lock`.
- Um modo de montar um venv de CI do zero: o lockfile do runtime mais o de desenvolvimento, no Python 3.12.9.
- Commitar o `frontend/.gitattributes` (`* text=auto eol=lf`).
- Os 8 erros de `mypy`: medir se somem com o SQLAlchemy 2.0.51 do lockfile. Se não somem, o RFC decide entre corrigi-los e prender a versão.

**Critérios de pronto**
- Num clone novo, no Windows, com `core.autocrlf=true` e o venv montado dos dois lockfiles, o comando passa inteiro. Tempo: `TBM` → **524,4 s com os caches frios e 241,7 s com eles quentes**, dentro do `embedded run --`; o venv, 390,3 s com o cache do `pip` frio ([RFC-048 §16](rfc-048-conferencias-num-comando-so.md#16-validação)).
- `mypy` com zero erros nesse venv. Quantos dos 8 sumiram só pela versão: `TBM` → **os 8**, e o 1 que a versão do lockfile mostra foi corrigido no teste (RFC-048 §8 e §16.3).
- Um defeito plantado em cada etapa (uma linha mal formatada, um import sem uso, um tipo errado, um teste falhando, um arquivo CRLF no `frontend/`) faz o comando falhar na etapa certa. Testado, e não presumido. → **Um em cada uma das nove etapas, e os nove falharam na etapa certa** (RFC-048 §16.4).

**Tamanho estimado:** meio dia a um dia.

**Implementado em 08/11/2026.** O que o RFC mudou neste escopo está no [§15 dele](rfc-048-conferencias-num-comando-so.md#15-o-que-isto-muda-no-roadmap-e-o-que-passa-adiante): o `pytest` vai para o fim, o `--all` virou `--keep-going` e o `pytest-mock` saiu. A medição achou 13 testes da suíte padrão que só passavam com `runtime\models`, o risco que este roadmap punha no RFC-050, e eles foram corrigidos ali ([§17.11](rfc-048-conferencias-num-comando-so.md#1711-treze-testes-da-suíte-padrão-pediam-o-runtimemodels)).

---

## RFC-049 — O primeiro workflow: frontend e análise estática

As conferências que levam segundos, a cada push. É o primeiro sinal verde no GitHub.

**Escopo**
- `.github/workflows/ci.yml`, disparado por `push` em qualquer branch e por `pull_request`, com `concurrency` que cancela a execução anterior da mesma branch.
- Job `frontend` no `ubuntu-24.04`: Node 24, `npm ci` com cache, `ELECTRON_SKIP_BINARY_DOWNLOAD=1` (nenhuma dessas conferências abre o Electron), e a parte do frontend do `check.py`.
- Job `estatico` no `windows-2025`: Python 3.12.9, o venv dos lockfiles com cache do `pip` pela chave dos dois lockfiles, e `ruff`, `black` e `mypy` pelo `check.py`.
- Permissões mínimas: `contents: read`.

**Critérios de pronto**
- Do push ao resultado dos dois jobs, com cache quente: `TBM` min → **1,5 min: 88 s, a mediana de três execuções; com os caches frios, 205 s** ([RFC-049 §14.2](rfc-049-primeiro-workflow.md#142-do-push-ao-resultado)). O alvo, escrito antes do número, é 10 min.
- Os defeitos plantados do RFC-048, um por commit numa branch de teste, deixam o job certo vermelho. → **Oito, um por branch, e cada um deixou vermelho o job certo, e só ele, com a primeira etapa vermelha na etapa do defeito** (RFC-049 §14.3).
- O frontend no Linux dá o mesmo número de testes do `npm test` local, no mesmo commit. Se não der, a diferença é explicada, ou o job vai para o Windows (D3). → **378 de 378.** Sem a correção do teste do `stagingDirectory`, 1 falha, a que a leitura previa (RFC-049 §14.4).

**Tamanho estimado:** meio dia.

**Implementado em 09/11/2026.** O que o RFC mudou neste escopo está no [§13 dele](rfc-049-primeiro-workflow.md#13-o-que-isto-muda-no-roadmap-e-o-que-passa-adiante): o `ELECTRON_SKIP_BINARY_DOWNLOAD=1` saiu, porque não faz nada no Electron 44; o cache do `pip` deu lugar ao do venv inteiro, salvo logo depois do `make_venv.py`, e o `.mypy_cache` também é guardado; o D3 ganhou a distinção entre um teste que presume o Windows e um que precisa dele; e os defeitos plantados são oito, sem o do `pytest`, com o do Prettier numa formatação, porque um CRLF não chega a um commit. A medição achou o caso frio em cerca de 4 min, e não 13, e o `setup-python` como o passo mais longo do job quente: ele baixa o 3.12.9 a cada execução, de 40 a 58 s. Isso passa para o RFC-050 ([§15.2](rfc-049-primeiro-workflow.md#152-o-setup-python-é-o-passo-mais-longo-do-job-quente)).

---

## RFC-050 — A suíte padrão do backend no Windows, com o Postgres embarcado

É a etapa de maior risco do roadmap, e o motivo de ela vir cedo: é aqui que aparecem os testes que só passam numa máquina de desenvolvimento.

**Escopo**
- Job `backend` no `windows-2025`: o venv do RFC-048, o `runtime\postgres` materializado pelo `vendor_postgres.py` e guardado em cache pelo hash do `postgres-win-64.lock`, e o `pytest` padrão com `DATABASE_SOURCE=embedded`, sem `.env`.
- Os dois testes instáveis do launcher. A causa de um já é conhecida (ignora o retorno do `drain()`). A correção é pequena, e cada uma é proposta antes de ser feita. **Rejeitado: `pytest-rerunfailures`**, que esconde a instabilidade em vez de removê-la.
- Tornar os três jobs obrigatórios na branch padrão, por um *ruleset* do GitHub. É uma configuração do repositório, e quem a liga é o usuário.

**Critérios de pronto**
- A suíte passa no runner com o mesmo número de testes que passam e que pulam no mesmo commit numa máquina de desenvolvimento. Cada diferença é explicada.
- Dez execuções seguidas sem nenhuma falha, sem *re-run*: `TBM` de 10.
- Tempo do job, com o cache do runtime frio e quente: `TBM` e `TBM` min.

**Riscos**
- A primeira subida do cluster no runner inclui o `initdb`: 24,7 s num HDD e 9,8 s num NVMe. Num runner, `TBM`.
- Algum teste pode presumir algo desta máquina (um drive `D:`, um usuário com acento, um volume com GUID real). Cada caso entra no RFC.

**Tamanho estimado:** um a dois dias.

---

## RFC-051 — Os testes `embedded` e `slow`, à noite e sob demanda

**Escopo**
- `.github/workflows/noturno.yml`, com `schedule` (uma vez por noite) e `workflow_dispatch`.
- `runtime\models` materializado pelo `vendor_models.py` e guardado em cache pelo hash do lockfile dos modelos (881 MB, mais os 123 MB do Postgres, dentro do limite de cache do repositório, que é de 10 GB por padrão).
- `pytest -m embedded` e `pytest -m slow`, com `HF_HUB_OFFLINE=1`, como no app.

**Critérios de pronto**
- Os vetores de referência do RFC-035 (`rfc035_reference_embeddings.npz`), bit a bit, **na CPU do runner**: iguais ou não, `TBM`. Se não forem, o RFC não afrouxa a comparação. Ele mede o tamanho da diferença, e a decisão vai para um RFC próprio, porque se o vetor depende da CPU, ele depende da CPU do usuário também, e a regra é que vetor que muda recomeça o índice.
- Tempo do job noturno, com cache quente: `TBM` min.
- Uma falha noturna avisa alguém. Por onde (e-mail do GitHub, issue aberta pelo workflow): `TBM`, decidido no RFC.

**Riscos**
- Depende de D7: sem a `frontend` na branch padrão, o `schedule` roda o código da `main` velha.
- Em repositório público, o GitHub desliga os workflows agendados depois de 60 dias sem atividade no repositório.

**Tamanho estimado:** um dia.

---

## RFC-052 — O instalador como artefato

**Escopo**
- `.github/workflows/instalador.yml`, por `workflow_dispatch`. O RFC-054 o chama numa tag.
- O `npm run dist` no `windows-2025`. O `dist.mjs` usa o Python do venv de desenvolvimento, então o job monta o venv dos lockfiles antes, com o mesmo passo do RFC-050 (uma *composite action* compartilhada).
- O `check_package.py` roda como roda hoje, e qualquer conferência que falhe falha o job.
- O instalador e o `SHA-256` dele sobem como artefato do workflow.

**Critérios de pronto**
- Tempo do build no runner: `TBM` min (37 min numa máquina de desenvolvimento).
- Pico de disco do job: `TBM` GB, contra o espaço livre do runner.
- As seis conferências do `check_package.py` passam no runner, incluindo os vetores bit a bit. Se os vetores divergirem, vale o que o RFC-051 decidiu.

**Tamanho estimado:** um dia.

---

## RFC-053 — O instalador testado numa máquina que nunca o viu

O [RFC-041](rfc-041-empacotamento-e-instalador.md) deixou pendente o teste numa conta limpa do Windows. Um runner é uma VM nova a cada execução, mas **não é uma máquina limpa**: a imagem já traz Python, Node e os redistribuíveis do Visual C++. Este teste não substitui a conferência estática das DLLs. Ele prova o resto.

**Escopo**
- Depois do RFC-052, no mesmo workflow: instalar em silêncio (`/S`, por usuário, sem elevação), subir o launcher da árvore instalada, esperar o evento `ready`, conferir o `/health` com a busca `ready`, parar pelo fim do stdin, desinstalar.
- Conferir pela lista de processos, e não pelo log, que nada ficou rodando. Conferir que a desinstalação removeu `%LOCALAPPDATA%\Programs\SolidVision` e deixou a raiz de dados, a política do [RFC-041 §7.4](rfc-041-empacotamento-e-instalador.md).
- Uma busca real exige fotos indexadas, e o dataset não vai no pacote. Se cabe indexar as 45 imagens do demo, a partir do checkout, pela API do app instalado, é decidido no RFC.

**Critérios de pronto**
- Da instalação ao `ready`, a frio, no runner: `TBM` s (35,0 s numa instalação simulada, [RFC-036](rfc-036-launcher-e-supervisao-de-processos.md)).
- Zero processos do app depois da parada e depois da desinstalação.
- Um instalador quebrado de propósito (um `.pyd` removido da montagem) faz o teste falhar.

**Tamanho estimado:** um a dois dias.

---

## RFC-054 — Release por tag: o CD

**Escopo**
- Uma tag `vX.Y.Z` dispara o RFC-052 e o RFC-053, e só depois deles cria um **rascunho** de GitHub Release com o instalador, o `SHA-256` e as notas a partir de um modelo. Uma pessoa publica (D8).
- Antes do build, conferir que os três lugares que dizem a versão ([RFC-041 §2.11](rfc-041-empacotamento-e-instalador.md)) batem com a tag.
- Conferir que o instalador cabe no limite de 2 GiB por arquivo de release do GitHub. Hoje ele tem 1,03 GB.
- `contents: write` só neste job, pelo `GITHUB_TOKEN`. Nenhum segredo novo.

**Critérios de pronto**
- Da tag ao rascunho: `TBM` min.
- Uma tag com versão diferente do `package.json` falha antes do build, em menos de 1 min. Testado.
- O instalador do rascunho é, byte a byte, o artefato que passou no teste de fumaça (o `SHA-256` confere nos dois).

**Tamanho estimado:** meio dia.

---

## Ordem e dependências

```
048 ──► 049 ──► 050 ──► 052 ──► 053 ──► 054
                  │
                  └──► 051  (depende de D7)
```

O 051 não bloqueia ninguém e pode andar em paralelo com o 052. O total estimado é de **seis a nove dias de trabalho**, e esse número também é uma estimativa.

**Se o tempo apertar**, o corte já decidido é: o 051 fica só com `workflow_dispatch`, sem `schedule` (o que tira a dependência de D7), e o 053 fica só com instalar, `/health` e desinstalar, sem busca.

## O corte de 11/11/2026

A entrega foi antecipada para 11/11/2026, e os seis a nove dias não cabem. O corte de "Se o tempo apertar" não basta, porque ainda pede o 051, o 052 e o 054. **O roadmap para no RFC-050, e a primeira release sai à mão.**

**Entra**

- **O RFC-050**, com o job `backend` verde no runner. É ele que faz valer o item 1 de [Pronto](#pronto).
- **D7, decidida:** a `frontend` vai para a `main` por fast-forward, porque a `main` não tem nenhum commit que a `frontend` não tenha. O workflow passa a existir na branch padrão.
- **A release `v0.1.0`, feita à mão**, com a D8 mantida:
  - o `npm run dist` numa máquina de desenvolvimento, com as seis conferências do `check_package.py`;
  - um teste à mão, na mesma máquina: instalar por cima da instalação que já existe, abrir com uma raiz de dados temporária, conferir o `/health` com a busca `ready`, fechar e ver que nenhum processo ficou rodando;
  - a tag `v0.1.0` no commit da `main`;
  - um **rascunho** de GitHub Release com o instalador e o `SHA-256`, criado pela API. Quem publica é uma pessoa.

**Fica adiado**, sem data:

| RFC | O que cobria | O que fica no lugar até ele voltar |
|---|---|---|
| 051 | `embedded` e `slow` à noite | Os dois rodam à mão. Os vetores bit a bit são conferidos pelo `check_package.py` a cada `npm run dist` |
| 052 | O instalador gerado no runner | O instalador é gerado numa máquina de desenvolvimento |
| 053 | O teste de fumaça numa máquina nova | O teste à mão acima, numa máquina que não é limpa. A desinstalação não é testada |
| 054 | Tag → rascunho de release | A tag e o rascunho são criados à mão |

O item 2 de [Pronto](#pronto) depende de importar o `.github/rulesets/ci.json`, o que é um ato do usuário. Os itens 3 e 4 ficam para quando o roadmap voltar, a partir do RFC-051.

**Os números da `v0.1.0`:**

| Número | Valor |
|---|---|
| Commit | `TBM` |
| Tempo do `npm run dist` nesta máquina | `TBM` min (37 min na máquina do RFC-036) |
| Tamanho do instalador | `TBM` GB |
| `SHA-256` | `TBM` |

## Pronto

O roadmap termina quando:

1. Todo push mostra, em `TBM` min, se o lint, os tipos, a suíte padrão e o frontend passam, num ambiente montado só dos lockfiles.
2. A branch padrão não aceita merge com um desses checks vermelho.
3. Os testes `embedded` e `slow` rodam sem ninguém pedir, e uma falha avisa alguém.
4. Uma tag produz, sem nenhum passo à mão, um rascunho de release com um instalador que foi instalado, aberto, fechado e desinstalado numa máquina nova.

## Fora deste roadmap

- **Assinatura de código.** É ela que tira o aviso do SmartScreen ([RFC-041 §7.5](rfc-041-empacotamento-e-instalador.md#75-sem-assinatura-o-smartscreen)), mas depende de comprar um certificado ou validar uma identidade num serviço de assinatura, e não de engenharia. Quando existir, entra no RFC-054 como um passo e um segredo.
- **Atualização automática** (`electron-updater` ou parecido). Depende de uma política de migrations entre versões do app, que o [roadmap sem Docker](ROADMAP-standalone.md#fora-deste-roadmap) já deixou de fora.
- **Atualização automática de dependências** (Dependabot, Renovate). Para `torch`, `transformers`, `torchvision` e `Pillow`, uma versão nova pode mudar os vetores, e vetor que muda recomeça o índice do usuário. Se entrar, entra com esses pacotes excluídos.
- **As medições dos RFCs.** Os números de indexação vêm do HD externo e do cartão SD do usuário, e um runner não tem nenhum dos dois. O CI confere correção, e não desempenho.
- **GPU, Linux e macOS.** O produto não roda neles hoje.
