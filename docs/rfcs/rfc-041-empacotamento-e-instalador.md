# RFC-041 — Empacotamento e Instalador

**Status:** Implementado em 2026-10-26, **com o teste em conta limpa pendente**. O build da máquina do RFC-036 fez o instalador: **1,03 GB** (1 109 415 480 bytes), com 2,16 GB instalados, em 37 min, e as conferências de §7.3 passaram, com os vetores do CLIP bit a bit pelo Python do pacote (§15). O pacote foi aberto nesta máquina sem instalar, com um `LOCALAPPDATA` vazio: a tela inicial em 63,4 s, a primeira abertura, e nada fora de `<dados>`. Os critérios que pedem a segunda conta — instalar sem rede, a abertura a frio de uma conta nova, as aberturas a quente pelo atalho, a desinstalação com o app aberto e o SmartScreen — esperam quem a crie (§9, §15.3). Onde a implementação contrariou o texto está em §14
**Etapa:** 4 do [roadmap do frontend](ROADMAP-frontend.md#etapa-4--empacotamento-e-instalador-rfc-041--qui-29-e-sex-30)
**Depende de:** RFC-033 ([§5.1](rfc-033-postgres-embarcado.md#51-um-lockfile-não-três-pins) o lockfile do runtime, §2.3 e §5.3 o runtime do Visual C++ e o leitor de PE, [§10](rfc-033-postgres-embarcado.md#10-caminhos-com-espaço-e-acento) os nomes curtos, §16), RFC-034 ([§4](rfc-034-configuracao-e-diretorios-do-usuario.md#4-o-layout-e-o-modo) o layout e o modo, §5.2 o ambiente que não é fonte, [§9](rfc-034-configuracao-e-diretorios-do-usuario.md#9-a-instalação-simulada) a instalação simulada, §13 e §14), RFC-035 (§4.2 a árvore dos modelos, §7 o bit a bit e o arquivo de referência, §12, §13), RFC-036 (§5.5 o `sys.executable`, §7 a parada, §8 a instância, [§14](rfc-036-launcher-e-supervisao-de-processos.md#14-o-que-passa-adiante), §17.3 o antivírus), RFC-037 (§6.1 o nome e o perfil, §6.2 o `resolvePython()`, §6.4 o fechamento, §15 os marcos, §16.1 o `ELECTRON_RUN_AS_NODE`, [§14](rfc-037-fundacao-e-casca-do-frontend.md#14-o-que-passa-adiante)), RFC-038 §12, RFC-039 [§12](rfc-039-dispositivos-e-indexacao.md#12-o-que-passa-adiante), RFC-040 [§12](rfc-040-mapa.md#12-o-que-passa-adiante) e §14.3, RFC-043 §6 e §8.3, as decisões D1 a D5 do roadmap
**Bloqueia:** nada. É a última etapa do roadmap
**Migration:** não
**Medição:** nenhum piloto. Lidos do disco em 2026-10-26: o `site-packages` do virtualenv de desenvolvimento (`backend\.venv`), arquivo por arquivo, os tamanhos, os caminhos e as datas de instalação dos pacotes; o `torch` 2.13.0+cpu e o `transformers` 5.15.0 instalados; e os tamanhos de `runtime\` e do Electron. Lidos na web no mesmo dia: a página de downloads do Python para Windows, a documentação do Python 3.12 sobre o Windows, o `multiUser.nsh` e o `nsisOptions.d.ts` do `app-builder-lib` 26.15.3 (no unpkg), a página da wiki do NSIS sobre instaladores grandes e o registro do npm. Os fatos sobre o código vêm da leitura do branch `frontend` em `34c3642`, e cada um diz o arquivo

> **Um RFC curto, como o roadmap pediu** ([O prazo](ROADMAP-frontend.md#o-prazo-e-o-que-ele-obriga)): as decisões, os critérios de pronto com no máximo três números `TBM`, e o que ficou de fora. A convenção de rascunho continua valendo: todo número marcado `TBM` é *a medir*, e o que não é medição diz que é escolha, conta ou hipótese.

---

## 1. Contexto

O backend sobe com um comando (RFC-036), a janela o sobe e o supervisiona (RFC-037), e as telas das Etapas 1 a 3 estão de pé (RFCs 038 a 040), com dois RFCs de fora da sequência implementados depois delas (043 e 044). Tudo isso roda de um checkout: o Python é o virtualenv de desenvolvimento, o Electron é o do `node_modules`, e a função que acha o Python da instalação existe e falha com uma linha que nomeia este RFC (`resolvePython()`, em `electron/python.ts`).

A árvore instalada tem forma desde o RFC-034 §4.1, e o `scripts/stage_installation.py` a monta fora do repositório. O que ela nunca teve é um Python próprio: a instalação simulada roda com o interpretador do venv e o `PYTHONPATH` apontado para a cópia (*"Não é o instalador"*, RFC-034 §9). Este RFC dá a ela o próprio Python, a põe dentro do Electron e faz disso um instalador que um fotógrafo abre com dois cliques, sem terminal e sem rede.

As Etapas 0 a 3 foram implementadas até 22/10, e os RFCs 043 e 044 até 23/10. Este documento é escrito em 26/10, três dias úteis antes do calendário do roadmap (qui 29 e sex 30). A folga que o RFC-040 guardou para cá (§1 dele) vai para os riscos de §10.

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| O Python 3.12 na distribuição *embeddable* oficial, com as dependências numa pasta, sem congelar | §5. A versão é a do venv (§2.8). As dependências ganham um lockfile (§2.1), com o `torchvision`, que o app não importa (§2.2), e o runtime do Visual C++, que o pacote não traz (§2.3) |
| `runtime\postgres` e `runtime\models` dentro do pacote | §4, ao lado de `runtime\python` |
| electron-builder com NSIS, por usuário, em `%LOCALAPPDATA%\Programs`, sem elevação | §7.1 |
| Sem assinatura de código; o SmartScreen declarado | §7.5 |
| O teste numa segunda conta do Windows | §9. Ela testa o perfil, e não a máquina (§2.4) |
| O `README.md`, o índice dos RFCs e o "Fora deste roadmap" do roadmap sem Docker | §12 |
| *Critérios:* uma conta sem nada e sem rede; o tamanho do instalador; a primeira abertura a frio; a desinstalação, com a política dos dados escrita | §9, e a política em §7.4. O perfil do Chromium muda de lugar para a política caber num diretório só (§2.6) |
| *Riscos:* o antivírus; o tempo do build com mais de 1 GB | §10 |

## 2. O que o estado atual muda no roadmap

A leitura do código, do venv e das ferramentas achou onze pontos. Nenhum muda uma das decisões D1 a D5. O §2.2 e o §2.3 são defeitos que um pacote montado pelo óbvio entregaria sem aviso; o §2.4 muda o que o teste em conta limpa prova; o §2.5 e o §2.7 são limites do Windows e do NSIS que o payload encosta.

### 2.1 Nada fixa as dependências do Python

- **O `backend/requirements.txt` lista nomes sem versão**, e mistura o runtime com o `pytest`, o `black`, o `ruff` e o `mypy`. O `requirements.txt` da raiz é anterior a tudo e nem tem o `torch`. O RFC-035 §12 já tinha registrado: *"Nada no repositório fixa as versões"*.
- **O venv é a única fonte**, e não serve de pacote. São 46 391 arquivos e 1 030,5 MB no `site-packages`, com o que os experimentos instalaram: `onnxruntime` e `onnx` (42,5 e 34,5 MB, de 16/08), `timm`, `einops` e `torchvision` (de 20/08, o bake-off do RFC-023), `accelerate`, e as ferramentas (o `mypy` sozinho, com o `.pyd` do mypyc, 33,7 MB). E o `pyvenv.cfg` aponta para o Python desta máquina (`home = …\Python312`): um venv não se copia para outra.

**Um lockfile**, como os dois runtimes já têm (RFC-033 §5.1, RFC-035 §4.1): versões exatas e o `sha256` de cada roda, só com o que o app usa (§5.2).

### 2.2 O `torchvision` muda os vetores, e o app não o importa

O adaptador carrega o processador com `AutoProcessor.from_pretrained(directory, local_files_only=True)`, sem dizer qual (`clip_embedding_model.py`). No `transformers` 5.15 instalado, quem escolhe é `_resolve_backend()` (`models/auto/image_processing_auto.py`): sem um backend pedido, `torchvision` quando o pacote está instalado (0.27 ou mais novo), e `pil` quando não.

Nenhum módulo de `backend/app` importa o `torchvision`. Ele entrou no venv em 20/08, com o `timm` e o `einops`, e desde então **todo vetor de imagem do projeto saiu do processador do `torchvision`**: o acervo de desenvolvimento, as medições e o arquivo de referência do RFC-035 (`rfc035_reference_embeddings.npz`, de 15/10). Um pacote montado pelas importações do app ficaria sem ele, e o mesmo código passaria ao processador do PIL sem erro e sem aviso. Se os dois redimensionam de outro jeito, o que é provável e não foi medido, os vetores de imagem mudam: o pacote deixaria de ser o que a suíte e as medições testaram, e o bit a bit do RFC-035 §7 acusaria só se alguém o rodasse com o Python do pacote.

**O `torchvision` vai no lockfile, o adaptador passa a pedir o backend pelo nome** (§5.5), e o build confere os vetores de referência com o Python do pacote (§7.3).

### 2.3 O `torch` carrega o `msvcp140.dll` pelo nome, e nada no pacote o traz

- O `torch/__init__.py` (2.13.0+cpu, linhas 245 a 254) chama `ctypes.CDLL("vcruntime140.dll")` e `ctypes.CDLL("msvcp140.dll")` e, se falhar, diz que *"Microsoft Visual C++ Redistributable is not installed"*. O `torch\lib` não tem nenhuma das duas: as nove DLLs dele são a `c10`, a `libiomp5md`, a `libiompstubs5md`, a `shm`, a `torch`, a `torch_cpu`, a `torch_global_deps`, a `torch_python` e a `uv`. O `numpy` traz uma cópia própria do `msvcp140`, com nome trocado (`numpy.libs`), e o `psycopg_binary` traz as suas.
- **Nesta máquina, o `msvcp140.dll` vem do `System32`, onde um redistribuível o pôs**: o RFC-033 §2.3 conferiu que ele não é do Windows, porque não está sob a proteção de recursos do sistema.
- A documentação do Python 3.12 diz que a distribuição *embeddable* não traz o runtime de C da Microsoft, e que levá-lo é responsabilidade do instalador.

Uma máquina sem o redistribuível recusaria o `import torch`. **As DLLs do runtime do Visual C++ vão ao lado do `python.exe`** (§5.3), e o build confere as importações de todo binário do pacote (§7.3).

### 2.4 A segunda conta testa o perfil, e não a máquina

O roadmap escolheu uma segunda conta porque o Windows 10 Home não tem o Windows Sandbox. Ela divide com a conta de desenvolvimento tudo o que é por máquina: o `System32`, com o redistribuível do §2.3, as fontes, o Defender e o cache de disco. Uma conta nova acharia o `msvcp140.dll` onde esta acha, e o defeito do §2.3 passaria no teste.

O que ela prova bem é o que é por usuário: um `%LOCALAPPDATA%` vazio, com o `initdb`, a raiz de dados e o perfil do Chromium criados do zero; nenhum `~\.cache\huggingface`, a verificação que o RFC-035 §15 deixou por fazer; e, se for uma conta padrão, um usuário que não consegue elevar sem a senha de outra pessoa.

E o SmartScreen só olha um arquivo com a marca da internet (`Zone.Identifier`). Um instalador copiado de uma pasta local não tem a marca, e o teste não veria o aviso que um download vê.

**O que é por máquina é coberto por conferência estática** (§7.3), como o RFC-033 §5.3 fez com o runtime do PostgreSQL, e pela ordem de busca do carregador, que põe o diretório do `python.exe` antes do `System32`: a segunda conta carrega a cópia do pacote, e a medição confere de onde veio cada módulo (§9). **A marca da internet é posta no instalador antes do teste** (§9).

### 2.5 Um caminho do pacote passa de 260 caracteres

Instalado por usuário, o pacote mora dentro do perfil. Com um nome de conta de 20 caracteres, o maior que uma conta local do Windows aceita, `C:\Users\<nome>\AppData\Local\Programs\SolidVision\resources\` tem 75 caracteres, e `…\resources\runtime\python\Lib\site-packages\` tem 108.

O caminho relativo mais longo do `site-packages` tem 171 caracteres: um aviso de licença dentro do `dist-info` do `torch` (`torch-2.13.0+cpu.dist-info\licenses\third_party\kineto\…\duktape-1.5.2\LICENSE.txt`). Com o prefixo, 279, acima dos 259 que o `MAX_PATH` deixa. Cinco arquivos passam de 140 caracteres, e os cinco estão nessa árvore; fora dos `dist-info`, o mais longo tem 134 e é do `onnx`, que não vai no pacote. Nesta máquina, com `chapi`, sobra margem: o problema é de quem tem um nome longo.

Se a extração do NSIS, o desinstalador e o Python lidam com um caminho maior que isso, este RFC não sabe e não vai descobrir na conta de alguém. **O build confere um orçamento de caminho, e as árvores de licença que passam dele vão num zip dentro do mesmo `dist-info`** (§7.3).

### 2.6 O perfil do Chromium mora no `%APPDATA%`

O `main.ts` põe o `userData` em `app.getPath("appData")`, que é o `%APPDATA%`, o perfil *Roaming*: `SolidVision` instalado e `SolidVision-dev` no checkout. Ali ficam o `localStorage` (a escolha do fundo do mapa, RFC-040 §5.2), o cache HTTP, com os tiles da área que se olhou, o lock de instância do Electron e o resto do perfil.

O RFC-034 §14 deixou para o instalador *"**um** diretório por usuário a considerar"*. Com a casca, são dois. E o *Roaming* é o que um perfil de rede sincroniza: o cache dos tiles viajaria junto, a área que o RFC-040 §2.7 tirou dos logs.

**O perfil do Chromium vai para `<dados>\shell`** (§6.2).

### 2.7 O tamanho, contado antes de medir

| parte | tamanho | de onde |
| --- | --- | --- |
| `runtime\models` | 881,1 MB; 802,4 MB em LZMA2 | RFC-035 §15 |
| As dependências do Python | ~820 MB | Uma conta: o `site-packages` do venv sem o que o §2.1 tira. O `torch` sozinho tem 489,8 MB, dos quais 290,9 MB são o `torch_cpu.dll` |
| O Electron | 369 MB | RFC-037 §16.1, o binário descompactado |
| `runtime\postgres` | 123 MB | RFC-033a, podado dos `.pdb` |
| O Python, o backend e a interface | algumas dezenas de MB | O *embeddable* e os `.pyc`; a interface tem ~1 MB |

São ~2,2 GB descompactados, uma conta e não uma medição. **O NSIS compila instaladores de até 2 GB**, segundo a wiki dele, e o teto vale para os dados comprimidos. Os pesos quase não comprimem (RFC-035), e o resto não foi comprimido por ninguém ainda. O tamanho é um critério (§9), e o segundo ramo, se o build recusar, é o corte 2 do roadmap (§13).

E o custo é por conta do Windows: instalado por usuário, cada conta que instala leva os seus ~2,2 GB.

### 2.8 A versão do Python, e o `._pth`

A página de downloads do python.org, lida em 26/10, lista o 3.12.10 (de abril de 2025) como o último 3.12 com binários para Windows; os seguintes são só código-fonte. O venv é o 3.12.9 (`pyvenv.cfg`), o que roda a suíte.

**O pacote leva o 3.12.9**, o interpretador que a suíte e as medições rodam. **Rejeitado: o 3.12.10**, que seria o único lugar a rodá-lo. Trocar o venv e o pacote juntos é outra decisão, sem pressa: as correções do 3.12 hoje são de segurança, e chegam só como código-fonte.

E a distribuição *embeddable* vem com um `python312._pth`. Segundo a documentação, com ele o Python ignora os caminhos do registro e das variáveis de ambiente, e só importa o `site` se o arquivo disser `import site`. O `PYTHONPATH` que o `resolvePython()` monta no desenvolvimento não teria efeito: o caminho do código vai no `._pth` (§5.1). É também o que o RFC-034 §5.2 quer do modo instalado: o ambiente não é fonte.

### 2.9 Desinstalar com o app aberto

Um desinstalador fecha o app aberto antes de apagar os arquivos; o do electron-builder tem uma checagem para isso, cujo comportamento na 26.18.0 este RFC não leu. Fechar a janela não para o resto na hora: o launcher, os dois filhos e o cluster rodam `python.exe` e `postgres.exe` de dentro de `resources\runtime`, e levam 1,68 s até o `stopped` com o executor no laço, até 24 s com ele carregando o modelo (RFC-037 §15, §16.1), e até os 30 s do `job_runner` com um job. Arquivos em uso no meio de uma remoção deixam uma instalação pela metade. O RFC-036 §14 já tinha escrito: *"Uma atualização precisa parar o launcher (`stop`) antes de trocar o runtime"*.

**O instalador e o desinstalador pedem ao launcher que pare** antes de tocar num arquivo (§7.4).

### 2.10 O teste em conta limpa passa pelo `C:`, e o dataset não vai no pacote

O RFC-039 §12 escreveu o caminho do teste: adicionar o `C:`, abrir a pasta do dataset, *"Indexar as pastas"*. O dataset de demonstração são as 45 imagens de `backend/dataset/demo/images`, versionadas, e não fazem parte do produto: o instalador não as leva, e o teste as copia para uma pasta pública da máquina (§9).

O RFC-039 §12 também deixou para cá uma pergunta: o diálogo de adicionar lista o disco do sistema como qualquer outro, e *"se a conta limpa mostrar que isso confunde, a decisão é desse RFC"*. Nada muda antes do teste. Quem roda o teste registra o que viu, e uma mudança, se vier, é um RFC à parte.

### 2.11 Três lugares dizem a versão

`0.1.0` está no `package.json` (o User-Agent dos tiles, RFC-040 §5.6, e o nome do instalador), no default de `project_version` em `settings.py` (o User-Agent do Nominatim, RFC-043 §8.3, e o OpenAPI) e no `pyproject.toml`, que não vai no pacote. Nada os mantém iguais. **Um teste os mantém** (§8), e o `package.json` é a fonte do instalador.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| O Python | O *embeddable* oficial 3.12.9, com o `sha256` fixado, em `runtime\python`, materializado por `scripts/vendor_python.py`, como os outros dois runtimes (§2.8, §5.1) |
| As dependências | `scripts/python-win-64.lock`: as versões do venv e o `sha256` de cada roda, só o fecho do que o app importa, mais o `torchvision`; instaladas sem resolver (§2.1, §2.2, §5.2) |
| O `._pth` | A biblioteca padrão, o `site-packages`, o `backend` e o `import site` (§5.1) |
| O runtime do Visual C++ | As DLLs que as importações pedirem, ao lado do `python.exe`, tiradas do `vc14_runtime` que o lockfile do PostgreSQL já fixa (§2.3, §5.3) |
| Os `.pyc` | Compilados na montagem pelo Python do pacote, sem depender da data dos arquivos (§5.4) |
| O processador do CLIP | Pedido pelo nome, `torchvision`. Sem o pacote, carregar falha (§2.2, §5.5) |
| A árvore | `resources\` é a raiz do layout: `backend\`, `frontend\dist\` e `runtime\{python,postgres,models}`; o `app.asar` só com a casca. O backend não muda (§4) |
| A casca | O ramo instalado do `resolvePython()`; o perfil do Chromium em `<dados>\shell`, nos dois modos; três fusíveis do Electron desligados; um ícone (§6) |
| O instalador | electron-builder 26.18.0, NSIS de um clique, por usuário, sem elevação nunca, em `%LOCALAPPDATA%\Programs\SolidVision`; atalhos no Menu Iniciar e na área de trabalho; sem assinatura (§7.1, §7.5) |
| A montagem | `npm run dist`: o build, a instalação simulada como a pasta `resources`, as conferências, o NSIS (§7.2) |
| As conferências | A árvore, o lockfile, o orçamento de caminho, as DLLs, os vetores de referência e o `app.asar`. Cada uma falha o build (§7.3) |
| A desinstalação | O launcher parado antes; o programa removido; os dados ficam, inteiros, e isso fica escrito (§2.9, §7.4) |
| O teste | Uma conta padrão nova, a rede desligada, o instalador com a marca da internet (§2.4, §9) |

## 4. A árvore instalada

```text
%LOCALAPPDATA%\Programs\SolidVision\
    SolidVision.exe, as DLLs e os .pak do Electron
    locales\pt-BR.pak, en-US.pak
    Uninstall SolidVision.exe
    resources\                       a raiz do layout (RFC-034 §4.1)
        app.asar                     a casca: dist-electron\, electron\startup.html, package.json
        backend\                     app\, alembic\, alembic.ini; sem tests\
        frontend\dist\               a interface que a API serve (RFC-037 §5.1)
        runtime\
            python\                  python.exe, python312.dll, python312.zip, python312._pth,
                                     as DLLs do Visual C++ (§5.3)
                Lib\site-packages\   o lockfile (§5.2)
            postgres\                RFC-033
            models\                  RFC-035
```

`detect_layout()` acha a raiz em `parents[4]` do `layout.py`, que em `resources\backend\app\infrastructure\config\` é `resources\`. Sem `pyproject.toml` ali, o modo é `installed` (RFC-034 §4.2), e os defaults de caminho do layout já apontam para o lugar certo: `runtime\postgres`, `runtime\models` e `frontend\dist`. **Nenhum código do backend muda por causa da árvore**; só as docstrings do `layout.py`, que deixam de dizer que o lugar da interface é deste RFC.

Não vão: o `pyproject.toml`, o `.env`, os `tests\`, o `dataset_tools\`, o `backend\main.py`, o dataset, os experimentos, o código-fonte da interface e o `node_modules`. Quem garante as três primeiras é a guarda que o `stage_installation.py` já tem (`_refuse_a_checkout()`).

`runtime\python` vem ao lado dos outros dois runtimes, e não em `resources\python`, pela simetria: é materializado por um `vendor_*.py` a partir de um lockfile, mora em `runtime\` no checkout, fora do git, e a instalação simulada o copia como copia os outros (§7.2).

## 5. O Python

### 5.1 A distribuição e o `._pth`

`python-3.12.9-embed-amd64.zip`, do python.org, com o `sha256` numa constante do `vendor_python.py`, como o micromamba do RFC-033. A origem dos bytes não importa, e o hash decide (RFC-035 §4.3).

O `python312._pth` é reescrito:

```text
python312.zip
.
Lib\site-packages
..\..\backend
import site
```

- **`..\..\backend`** é o `PYTHONPATH` do desenvolvimento, dito no único lugar que o Python do pacote lê. Os caminhos de um `._pth` são relativos a ele.
- **`import site`**, porque as rodas foram instaladas pelo `pip` para um `site-packages` comum, e o `site` processa os `.pth` delas. O `._pth` deixa o `site` do usuário de fora.
- **O `sys.executable` é o `runtime\python\python.exe`**, e é ele que o launcher dá aos filhos (RFC-036 §5.5). A linha de comando dos filhos não muda.

**Rejeitado: congelar com o PyInstaller**, pelo motivo do roadmap. **Rejeitado: copiar o venv**, que não se copia (§2.1). **Rejeitado: o instalador completo do python.org**, rodado em silêncio para o usuário: ele escreve no registro, no `PATH` e no lançador `py`, e divide tudo isso com o Python que a pessoa já tiver.

### 5.2 O lockfile e o `vendor_python.py`

`scripts/python-win-64.lock` está no formato de requisitos do `pip`: uma linha `nome==versão --hash=sha256:…` por roda, e no cabeçalho os dois índices, o PyPI e o de CPU do PyTorch (`download.pytorch.org/whl/cpu`), de onde vêm o `torch` e o `torchvision` `+cpu`. É versionado.

O `vendor_python.py` segue as regras dos outros dois:

- **`--lock` escreve o lockfile.** Parte da lista do runtime (a primeira parte do `backend/requirements.txt`, mais o `torchvision`), fixa cada nome na versão que o venv tem, resolve o fecho e grava o `sha256` de cada roda para `cp312` e `win_amd64`. Uma versão do fecho diferente da do venv falha o `--lock`: os vetores de referência vieram do venv (§2.2).
- **Sem opção, materializa `runtime\python`.** Baixa e confere o zip, extrai, reescreve o `._pth`, e instala o lockfile com o `pip` do venv: `--target runtime\python\Lib\site-packages --no-deps --require-hashes --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --no-compile`. Nada se resolve na materialização. Depois, as DLLs de §5.3, os `.pyc` de §5.4 e um manifesto, `SOLIDVISION-PYTHON.json`: a versão do Python, o `sha256` do lockfile com o fim de linha normalizado (RFC-033 §19) e o tamanho. Tudo num diretório ao lado, movido de uma vez no fim, como o `vendor_models.py` faz.

O que fica de fora, pelos números de §2.1: as ferramentas, os testes e o que os experimentos instalaram. O lockfile diz quanto sobra (§9).

### 5.3 O runtime do Visual C++

As DLLs vão para `runtime\python`, ao lado do `python.exe`, e são tiradas de `runtime\postgres\Library\bin`: são as do pacote `vc14_runtime` do conda-forge, que o `postgres-win-64.lock` já fixa pelo `md5`, e que o RFC-033 §2.3 viu o servidor carregar dali. Vão as que a conferência de importações de §7.3 pedir, e nenhuma outra. A licença é a que o pacote do conda-forge traz.

**Rejeitado: o redistribuível da Microsoft como pré-requisito.** Ele se instala por máquina, e pede elevação, que este instalador nunca pede.

### 5.4 Os `.pyc`

Instalada por usuário, a árvore é gravável pelo próprio usuário, ao contrário da cópia trancada do RFC-034 §9: sem os `.pyc`, o Python os escreveria dentro da instalação na primeira abertura. O RFC-034 §16 não separou do ruído o que eles economizam, e compilar não custa nada.

**A montagem compila o `backend` e o `site-packages` com o `python.exe` do pacote, em `--invalidation-mode unchecked-hash`.** Um `.pyc` assim não é conferido contra a data do `.py`, e não depende de a extração do NSIS a preservar. Numa instalação, o `.py` só muda junto com o `.pyc`, na próxima instalação.

### 5.5 O processador do CLIP

O adaptador passa a pedir o backend `torchvision` pelo nome ao carregar o processador. Sem o `torchvision` instalado, carregar falha com a mensagem que o adaptador já tem para uma árvore que não carrega (`load_failed()`, RFC-035 §6.4), em vez de trocar de processador em silêncio. Como o argumento chega ao processador de imagem pelo `AutoProcessor` é da implementação; o teste de §8 e os vetores de §7.3 decidem.

## 6. A casca

### 6.1 O ramo instalado do `resolvePython()`

| | instalado |
| --- | --- |
| O Python | `runtime\python\python.exe`, debaixo do `process.resourcesPath` do Electron. A `SOLIDVISION_PYTHON` não vale: uma variável de ambiente não escolhe o interpretador de um app instalado (RFC-034 §5.2) |
| O diretório | `resources\` |
| O ambiente | O da janela, sem nada acrescentado. O `PYTHONPATH` não entra, porque o `._pth` o ignora (§2.8) |
| Sem o arquivo | Uma linha na tela de erro: *"A instalação está incompleta: falta …\python.exe. Reinstale o SolidVision."* |

O ramo de desenvolvimento não muda.

### 6.2 O perfil do Chromium em `<dados>\shell`

O `userData` passa a ser `%LOCALAPPDATA%\SolidVision\shell` instalado e `%LOCALAPPDATA%\SolidVision-dev\shell` no checkout, com a regra do `LOCALAPPDATA` que o `logDirectory()` já segue (`log.ts`), definido antes do lock, como hoje. O lock de instância do Electron mora no `userData`, e passa a ser, como o do launcher, um por raiz de dados (RFC-036 §8). E uma medição que aponta o `LOCALAPPDATA` para um diretório de trabalho passa a isolar também o perfil do Chromium.

O que custa: no checkout, o perfil antigo fica em `%APPDATA%\SolidVision-dev`, e pode ser apagado à mão. A escolha do fundo do mapa volta ao default uma vez. Nenhuma instalação existe ainda, e nada se migra.

### 6.3 Os fusíveis do Electron

No executável empacotado, três fusíveis desligados: `RunAsNode`, `EnableNodeOptionsEnvironmentVariable` e `EnableNodeCliInspectArguments`. O RFC-037 §16.1 viu um processo iniciado do VS Code herdar `ELECTRON_RUN_AS_NODE=1` e o Electron rodar como um Node, sem janela. Instalado, a mesma variável no ambiente da pessoa faria do `SolidVision.exe` um interpretador de Node. É a regra do RFC-034 §5.2 e das variáveis `PG*` do RFC-034 §7.5: o ambiente do usuário não muda o que o app instalado é. Vale pela opção `electronFuses` do electron-builder ou pelo `@electron/fuses` num `afterPack`, o que a versão fixada tiver. O desenvolvimento não muda: o `scripts/electron.mjs` continua tirando a variável.

### 6.4 O ícone

`frontend/build/icon.png`, de 256 × 256 px, desenhado uma vez a partir do `Logo.tsx`, que é o desenho do ícone do app (`d9d9dea`), e versionado. O electron-builder faz dele o ícone do executável, dos atalhos e do desinstalador. Sem chamada ao Figma: o orçamento acabou no RFC-039.

## 7. O instalador

### 7.1 O electron-builder

**A versão** é a 26.18.0, de 20/10, exata nas dependências de desenvolvimento. A 26.18.1 saiu hoje, e fica de fora pelo critério do RFC-037 §16.1, que preferiu versões com dias de registro. Ele roda no Node 24 das ferramentas (RFC-037 §2.1).

**A configuração** fica em `frontend/electron-builder.config.cjs`, que lê o lugar da montagem (§7.2):

| opção | valor, e por quê |
| --- | --- |
| `appId` | `io.github.santhiagobr.solidvision`, do endereço do projeto que os User-Agents já levam: o AppUserModelID da barra de tarefas e a chave da desinstalação |
| `files` | `dist-electron\`, `electron\startup.html` e o `package.json`, num `app.asar`. Nenhum `node_modules`: as dependências do `package.json` são da página, e o Vite já as pôs no `dist`. O processo principal importa só o Electron e o Node |
| `extraResources` | A montagem, em `resources\` (§4) |
| `electronLanguages` | `pt-BR` e `en-US`, o que o Chromium usa quando falta o idioma do sistema. A interface é só PT-BR |
| `win.target` | `nsis`, x64 |
| `nsis.oneClick` | `true`: sem páginas e sem escolha de pasta. É o default |
| `nsis.perMachine`, `nsis.allowElevation` | `false` e `false`. O primeiro é o default; o segundo não, e é ele que garante que o instalador nunca pede elevação (RFC-034 §13, RFC-036 §14) |
| `nsis.runAfterFinish` | `true`: o app abre ao terminar, com o token de quem instalou, que não é elevado. O problema que o RFC-036 §14 descreve é de um instalador elevado |
| Os atalhos | No Menu Iniciar e na área de trabalho, *"SolidVision"* |
| `nsis.deleteAppDataOnUninstall` | `false`, o default (§7.4) |
| `nsis.include` | `build/installer.nsh`, a parada de §7.4 |
| `artifactName` | `SolidVision-Setup-${version}.exe` |
| A publicação | Nenhuma, e sem *blockmap* de atualização diferencial: não há atualização automática |
| A compressão | A padrão. Os pesos não comprimem (§2.7), e a máxima custaria tempo de build por pouco |

**Onde instala:** o `multiUser.nsh` do electron-builder põe a instalação por usuário em `$LocalAppData\Programs`, ou no `FOLDERID_UserProgramFiles` quando o Windows o informa, que é a mesma pasta por padrão, seguido do nome do app. Os defaults citados aqui foram lidos no `app-builder-lib` 26.15.3; a implementação os confere na 26.18.0.

### 7.2 A montagem

`npm run dist`, no `frontend/`:

1. **`npm run build`**, como hoje.
2. **A instalação simulada, como a pasta `resources`.** O `stage_installation.py` passa a copiar também `runtime\python` e `frontend\dist`, compila com o Python do pacote (§5.4), e deixa de ser *"não é o instalador"*: a cópia roda com o próprio Python, e o `_how_to_run()` passa a dizer `runtime\python\python.exe -m app.launcher`, sem `PYTHONPATH`. O lugar é `%LOCALAPPDATA%\SolidVision-build\resources`: fora do repositório, como o script exige, e fora do `%TEMP%`, que o RFC-040 §14.1 viu ser varrido. O marcador `SOLIDVISION-STAGED.json`, que diz de onde a cópia veio, não vai no pacote.
3. **As conferências de §7.3.** Qualquer falha para aqui.
4. **`electron-builder --win nsis`**, com a montagem em `extraResources`. O instalador sai em `frontend/release/`, que o `.gitignore` já ignora.

`npm run dist:dir` para depois do passo 4 com `--dir`: a pasta pronta, sem o NSIS. É o corte 2 do roadmap (§13), e o jeito de olhar o pacote sem esperar a compressão.

### 7.3 As conferências do build

Feitas por `scripts/check_package.py`, sobre a montagem e sobre a pasta do passo 4. Cada uma falha o build:

| conferência | como |
| --- | --- |
| A árvore | Sem `pyproject.toml`, `.env` nem `tests\`, pela guarda do `stage_installation.py` |
| O lockfile | O que está em `Lib\site-packages` é o lockfile, nem a mais nem a menos, lido pelo `importlib.metadata` do Python do pacote |
| O orçamento de caminho | Todo arquivo da pasta final, o Electron incluído, cabe em 259 caracteres sob `C:\Users\` seguido de 20 caracteres e de `\AppData\Local\Programs\SolidVision\` (§2.5). Uma árvore `licenses` de um `dist-info` que passe do orçamento vira `licenses.zip` no mesmo lugar, com os caminhos de dentro preservados: o aviso continua distribuído, e nada no runtime lê essas pastas. Qualquer outro arquivo acima do orçamento falha |
| As DLLs | A tabela de importação de todo `.exe`, `.dll` e `.pyd` do pacote, pelo leitor de PE do RFC-033 (`pe_imports()`, hoje em `experiments/rfc-033-embedded-postgres/measure_adapter.py`, que passa para `scripts/`). Cada nome resolve num arquivo do próprio pacote, ou entre as DLLs do Windows: os conjuntos de API (`api-ms-win-*`, `ext-ms-*`), as `KnownDLLs` e as do `System32` sob a proteção de recursos. Uma DLL que só o `System32` desta máquina tem, sem ser do Windows, falha: é o caso do §2.3. Os `ctypes.CDLL` por nome não aparecem em tabela nenhuma, e a lista do `torch` (§2.3) entra na conferência pelo nome |
| Os vetores | `check_package.py --reference backend/tests/fixtures/rfc035_reference_embeddings.npz`, rodado pelo `python.exe` da montagem, no modo instalado: as 45 imagens e as 25 consultas do RFC-035 §15, bit a bit contra o arquivo de referência |
| O `app.asar` | A listagem dele: só `dist-electron\`, `electron\startup.html` e o `package.json` |

### 7.4 A parada, a desinstalação e a política dos dados

**A parada.** O `build/installer.nsh` substitui a checagem do app aberto do electron-builder pela macro que ele oferece para isso (`customCheckAppRunning`, um nome que este RFC não conferiu na 26.18.0). Quando `$INSTDIR\resources\runtime\python\python.exe` existe, ela roda `python.exe -X utf8 -m app.launcher stop`, sem janela e com o prazo de 60 s do fechamento da casca (RFC-037 §6.4). O `stop` sinaliza o launcher da raiz de dados de quem roda o desinstalador, que é a mesma pessoa, porque a instalação é por usuário (RFC-036 §8). O launcher para os filhos e o cluster, com o job devolvido à fila; a janela, que é a dona, sai no `stopped` (RFC-037 §6.3). Depois, a macro espera não haver nenhum processo rodando de dentro de `$INSTDIR`, e, passado o prazo, encerra o que sobrou, com o custo do RFC-037 §6.4: um cluster que fica é adotado na próxima subida. Vale para a desinstalação e para uma instalação por cima de outra.

**O que desinstalar remove:** `%LOCALAPPDATA%\Programs\SolidVision`, os atalhos e a entrada em *Aplicativos*.

**O que fica: `%LOCALAPPDATA%\SolidVision` inteira.** O cluster, as miniaturas, os logs, o `cache\`, o perfil do Chromium (§6.2), o `launcher.json` e o `solidvision.env`, se a pessoa o escreveu. Reinstalar a mesma versão reaproveita tudo: o mesmo cluster, as mesmas miniaturas, o acervo indexado. A razão é o custo do que se perderia: o acervo indexado é inferência em CPU, a ~450 ms por imagem (RFC-033 §8.1), horas por disco (RFC-027 §2), e desinstalar e instalar de novo, para consertar alguma coisa, não pode custar uma reindexação. Apagar os dados é à mão, com o app fechado: apagar a pasta. O `README.md` diz isso.

**Rejeitado: perguntar no desinstalador.** O instalador de um clique não tem páginas, e uma caixa de seleção custaria o instalador assistido inteiro. **Rejeitado: `deleteAppDataOnUninstall`**, que apagaria o `userData` do Electron, e não a raiz de dados.

### 7.5 Sem assinatura: o SmartScreen

O instalador, o executável e o desinstalador não são assinados. Num arquivo com a marca da internet, o SmartScreen mostra *"O Windows protegeu o computador"*, e a pessoa passa por *"Mais informações"* e *"Executar assim mesmo"*. O `README.md` diz isso, com as duas telas. O teste as registra (§9).

## 8. Testes

**No backend, na suíte padrão:**

- **O lockfile:** cada importação de terceiros de `backend/app`, lida pela árvore sintática, é coberta por uma entrada; o `torchvision` está nele; o `torch` e o `torchvision` são `+cpu`; nenhuma ferramenta (`pytest`, `black`, `ruff`, `mypy`) está; cada linha tem o `sha256`.
- **O `vendor_python.py`:** o `._pth` escrito, linha a linha; o manifesto; uma materialização interrompida não deixa `runtime\python` pela metade; uma versão do fecho diferente da do venv falha o `--lock`. Sobre um repositório em miniatura, como o `test_vendor_models.py`.
- **A instalação simulada:** a cópia leva `runtime\python` e `frontend\dist`; continua `installed` onde a origem é `development`; o marcador fica fora do que vai para o pacote.
- **O `check_package.py`:** o orçamento, com o prefixo de 75 caracteres, e o zip só para uma árvore `licenses` de um `dist-info`; a resolução das DLLs, sobre uma árvore pequena com um `System32` de mentira: um conjunto de API passa, uma DLL protegida passa, uma desprotegida que só o `System32` tem falha.
- **O adaptador:** o backend pedido pelo nome; sem o `torchvision` disponível, carregar falha com a mensagem do RFC-035 §6.4.
- **A versão:** o default de `project_version` é a `version` do `frontend/package.json` (§2.11).

**No `frontend/`, no `npm test`:**

- **`resolvePython()`, instalado:** o caminho a partir do `resourcesPath`; a `SOLIDVISION_PYTHON` ignorada; nenhum `PYTHONPATH` acrescentado; a linha quando o arquivo falta. O teste que hoje confere a falha que nomeia este RFC sai.
- **O `userData`:** `<dados>\shell` nos dois modos, com e sem `LOCALAPPDATA`.
- **A configuração do electron-builder:** o `oneClick`, o `perMachine` e o `allowElevation` de §7.1, e a lista do `files`.

## 9. Critérios de pronto

**O build** roda na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA). **O teste** roda numa segunda conta local, **padrão, sem administrador**, criada para isso. Criar a conta é um ato de administrador da máquina, e é decisão de quem a tem.

**O preparo**, feito da conta de desenvolvimento: o instalador copiado para `C:\Users\Public\Downloads`, com a marca da internet (`Zone.Identifier`, `ZoneId=3`), para que o SmartScreen o trate como um download (§2.4); o dataset de demonstração copiado para `C:\Users\Public\Pictures\SolidVision-demo`; e `experiments/rfc-041-installer/check_install.py` copiado para uma pasta pública. **A rede fica desligada** do login na segunda conta até o fim do teste.

**A leitura** é do `check_install.py`, rodado na segunda conta pelo `python.exe` do próprio pacote, o único Python que ela tem: os marcos do `shell.log` dela (RFC-037 §6.5), os módulos carregados pelos processos do pacote, e o primeiro nível de `%USERPROFILE%`, `%APPDATA%`, `%LOCALAPPDATA%` e `%TEMP%` antes da instalação e depois do teste. O teste é feito à mão, com uma lista de passos. **Quem o roda avisa antes**: ele pede uma conta nova, a troca de sessão e a rede desligada, e leva cerca de uma hora, uma estimativa.

| critério | o número |
| --- | --- |
| **Numa conta sem nada instalado e com a rede desligada: instalar, abrir pelo atalho, indexar o dataset de demonstração e buscar** | Sem número. Instalar sem nenhum pedido de elevação; o app abre ao terminar; fechado, abre de novo pelo atalho do Menu Iniciar; adicionar o `C:`, abrir a pasta do dataset e *"Indexar as pastas"*: as 45 indexadas; uma busca em português com resultados, e *"Parecidas com esta"* a partir de um deles |
| **O tamanho do instalador** | `TBM` MB, com o instalado em disco ao lado. A conta de §2.7 dá ~2,2 GB descompactados, e o teto do NSIS é de 2 GB |
| **A primeira abertura, a frio:** a primeira da conta, a que o instalador abre ao terminar, com a raiz de dados vazia e os arquivos recém-instalados, do início do processo da janela ao marco `home` | `TBM` s. Num checkout, o RFC-037 §15 mediu o `ready` em 39,0 s com o `initdb`; o RFC-036 §17.3 viu aquecimentos de 49,6 e 104,9 s logo depois de uma instalação nova. O `search: ready` vai ao lado, como informação |
| **A abertura a quente, pelo atalho** | `TBM` s, mediana de cinco, do início do processo ao marco `home`. Num checkout, 6,42 s (RFC-037 §15) |
| **Desinstalar remove o programa, e os dados ficam** | Sem número. Com o app aberto e um job em andamento, por *Configurações > Aplicativos*: depois, nenhum processo do pacote, nada em `%LOCALAPPDATA%\Programs\SolidVision`, nenhum atalho, e `%LOCALAPPDATA%\SolidVision` com o que tinha. Instalar de novo e abrir: o acervo de antes, sem reindexar, e o job interrompido retomado (RFC-036 §7) |

E cinco verificações que não têm número:

- **O SmartScreen.** As telas que apareceram, registradas como apareceram.
- **De onde veio cada módulo.** O `msvcp140.dll` dos processos Python vem de `runtime\python`, e nenhum módulo carregado por um processo do pacote vem de fora do pacote e do Windows (§2.4).
- **Nada fora de dois lugares.** No primeiro nível das quatro pastas do perfil, só aparecem `Programs\SolidVision` e `SolidVision`: o perfil do Chromium junto (§6.2), e os caches dos modelos onde o RFC-035 §5.4 os pôs.
- **As conferências do build** passaram, com os números delas: o caminho mais longo, as DLLs pedidas pelo §5.3, os vetores bit a bit e a listagem do `app.asar` (§7.3).
- **Duas verificações que ficaram para trás**, que a conta sem rede faz de passagem: a do RFC-035 §15, indexar e buscar numa conta que nunca alcançou o Hub; e a do RFC-040 §14.3, o mapa aberto sem rede de verdade, com as células sobre o contorno e a faixa da pausa.

E duas informações: o tempo do build, do `npm run dist` ao instalador, que é o segundo risco do roadmap; e o tamanho do `site-packages` do lockfile, contra a conta de §2.7.

## 10. Riscos

| risco | situação |
| --- | --- |
| O antivírus varrer os arquivos recém-instalados | O RFC-036 §17.3 viu aquecimentos de 49,6 e 104,9 s depois de uma instalação nova, sem separar a varredura da disputa de disco. A primeira abertura de §9 inclui o Defender da máquina, como a de um usuário incluiria. **Rejeitado: o instalador pôr exclusões no Defender**, que pede elevação e é o que um app não deve fazer |
| O payload passar do teto do NSIS | `TBM` em §9. O build falha, e não o instalador. O segundo ramo é o corte 2 (§13) |
| O tempo de cada build | Comprimir ~2,2 GB em dois núcleos, num HDD, a cada tentativa. O `npm run dist:dir` olha o pacote sem comprimir (§7.2). O número vai para a validação |
| Um caminho acima de 259 caracteres quebrar a extração ou a desinstalação | Hipótese, não testada. O orçamento de §7.3 impede o pacote de chegar lá |
| Um nome de conta fora da página de código, num volume sem nomes 8.3 | O runtime mora no perfil, e o adaptador recusa com uma mensagem que nomeia um setting (RFC-033 §10), que a pessoa não tem como usar bem. A saída seria instalar fora do perfil, o que pede elevação. Declarado |
| A cópia do runtime do Visual C++ ser mais velha que a que o `torch` pede | A conferência de §7.3 vê os nomes, e não as versões. A segunda conta carrega a cópia do pacote, antes do `System32`, e exercita isso (§9) |
| O SmartScreen afastar quem baixa | Declarado pelo roadmap. O `README.md` mostra as duas telas (§7.5) |
| Uma biblioteca ler o código-fonte da biblioteca padrão | O `python312.zip` só tem `.pyc`, e um `inspect.getsource()` sobre a biblioteca padrão falharia. Hipótese: os vetores de §7.3 e a abertura instalada exercitam a importação inteira |
| O `AutoProcessor` não repassar o backend pedido | O teste de §8 e os vetores de §7.3 acusam (§5.5) |
| A opção dos fusíveis não existir na 26.18.0 | O `@electron/fuses` num `afterPack` faz o mesmo (§6.3) |
| O primeiro build precisar de rede | O electron-builder baixa o NSIS e as ferramentas dele, e o `vendor_python.py` baixa o zip e as rodas. Uma vez por máquina, com os hashes conferidos onde o lockfile os tem |
| A instalação ser gravável pelo usuário | Qualquer processo da pessoa pode trocar um arquivo do app, como num app instalado por usuário qualquer. O token por sessão continua fora (RFC-036 §14) |
| O `launcher stop` falhar, com o `solidvision.env` inválido | Ele sai com 78 antes de parar alguém. A macro de §7.4 espera o prazo e encerra o que sobrou, e o cluster que fica é adotado (RFC-036 §7.4) |
| Instalar uma versão nova por cima de dados velhos | As migrations entre versões estão fora do roadmap. Hoje só existe a 0.1.0 |

## 11. Fora deste RFC

- A assinatura de código, as atualizações automáticas, as migrations entre versões do app e o `pg_upgrade`.
- A instalação por máquina, a instalação silenciosa ou administrada, e escolher a pasta.
- O dataset de demonstração no instalador (§2.10), e o disco do sistema no diálogo de adicionar, até o teste dizer alguma coisa.
- Podar o `torch`: os 39,5 MB de `.lib` e os 37,4 MB de `include\` ficam. São ~3% do pacote, e nenhum teste do projeto prova que o `torch` não os abre.
- Apagar os dados pelo desinstalador, e uma ferramenta para isso.
- O ícone de bandeja, iniciar com o Windows e o token por sessão do RFC-036 §14.
- O logoff com a janela aberta (RFC-036 §17.3), que a segunda conta poderia fazer e não faz aqui.
- GPU, ARM64, Linux e macOS, e outros idiomas.

## 12. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-041-empacotamento-e-instalador.md` | Este documento |
| `scripts/python-win-64.lock` | As rodas do runtime, com as versões e os hashes (§5.2) |
| `scripts/vendor_python.py` | `runtime\python`: o zip, o `._pth`, as rodas, as DLLs, os `.pyc` e o manifesto (§5) |
| `scripts/check_package.py`, `scripts/pe_imports.py` | As conferências de §7.3, com o leitor de PE do RFC-033 |
| `frontend/electron-builder.config.cjs` | §7.1 |
| `frontend/build/icon.png`, `frontend/build/installer.nsh` | O ícone (§6.4) e a parada (§7.4) |
| `backend/tests/test_vendor_python.py`, `backend/tests/test_check_package.py` | §8 |
| `experiments/rfc-041-installer/check_install.py` (+ `.log`, fora do git) | §9 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `scripts/stage_installation.py`, `backend/tests/test_stage_installation.py` | `runtime\python` e `frontend\dist`; a cópia roda com o próprio Python; a compilação em `unchecked-hash` (§5.4, §7.2) |
| `backend/app/infrastructure/ai/clip_embedding_model.py` e o teste dele | O backend do processador pelo nome (§5.5) |
| `backend/app/infrastructure/config/layout.py` | Só as docstrings: a árvore com `runtime\python`, e o lugar da interface (§4) |
| Um teste do backend | A versão nos dois lugares (§2.11) |
| `frontend/electron/python.ts`, `python.test.ts` | O ramo instalado (§6.1) |
| `frontend/electron/main.ts`, `log.ts` e os testes | O `userData` em `<dados>\shell` (§6.2) |
| `frontend/package.json`, `package-lock.json` | `electron-builder` 26.18.0 nas de desenvolvimento; `npm run dist` e `npm run dist:dir` |
| `README.md` | "Como instalar": o instalador, o SmartScreen, onde ficam o programa e os dados, e como apagá-los; o contato que a política dos tiles recomenda (RFC-040 §12); o estado |
| `docs/rfcs/README.md`, `ROADMAP-frontend.md`, `ROADMAP-standalone.md` | O status deste RFC, quando implementado; e o "Fora deste roadmap" do roadmap sem Docker: o runtime Python e o instalador saem da lista, com a assinatura e as atualizações ficando |

Não mudam: as migrations, as rotas, o launcher e o protocolo dos eventos, o `job_runner`, os adaptadores do cluster e dos modelos, as telas e o `docker-compose.yml`.

## 13. O que isto muda no roadmap, e o que passa adiante

**O calendário.** O roadmap pôs o Python embutido e o electron-builder na quinta, 29, e o teste em conta limpa, o README e o status na sexta, 30. Com este documento em 26/10, a ordem é a mesma, três dias antes: o `vendor_python.py` e as conferências primeiro, porque é deles que vêm os defeitos do §2.2 e do §2.3; depois o NSIS; o teste por último.

**O corte 2**, já decidido no roadmap, tem dois gatilhos: o NSIS recusar o payload (§2.7), ou o instalador falhar na segunda conta de um jeito que não se conserte até a sexta, 30. Ele é o `npm run dist:dir` sobre a mesma montagem, num `.zip`, com um atalho criado à mão. As conferências de §7.3 valem para os dois, e o critério da conta sem rede é feito sobre o que sair.

**Para quem fizer as atualizações:** a parada de §7.4 é o começo de uma, e o que falta é o que o RFC-036 §14 e o RFC-033 §16 já listaram: as migrations entre versões, o `pg_upgrade` e um runtime trocado com o cluster parado. A assinatura vem antes: um atualizador automático sem ela distribui código sem prova de origem.

**Para quem mexer nas dependências do Python:** o lockfile é o que o pacote leva, e o venv é o que a suíte roda. Atualizar um sem o outro é o §2.2 outra vez; o `--lock` recusa a diferença, e os vetores de referência acusam o resto.

**Para o [roadmap de fine-tuning](ROADMAP-fine-tuning.md)**, que começa depois desta etapa: um checkpoint novo entra pelo lockfile dos modelos (RFC-035 §4.4), e o instalador o leva sem mudança. O que muda é o arquivo de referência de §7.3, que passa a ser o do checkpoint novo.

## 14. Correções feitas durante a implementação

Registradas à vista do texto original, como os RFCs 033 a 040 fizeram com as suas. Nenhuma muda uma das decisões D1 a D5, nem o que §3 decidiu; duas mudam um detalhe dele (o `._pth` e o `+cpu` do `torchvision`).

### 14.1 O que a medição e o código contrariaram

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §3, §5.1 | O `._pth` com *"`import site`"*, e *"O `._pth` deixa o `site` do usuário de fora"* | **No 3.12.9, o `._pth` liga o modo isolado e deixa o `site` do usuário ligado**: medido com o Python do pacote, `sys.flags.isolated` 1 e `no_user_site` 0, e o `%APPDATA%\Python\Python312\site-packages` desta máquina, com os pacotes do Jupyter, no fim do `sys.path`. Com o `import site`, um pacote que a pessoa instalou com `pip --user` entraria no app, e os `.pth` dele rodariam no processo. **O `import site` saiu do `._pth`**: o `sys.path` é o que o arquivo diz, e nada mais. O único `.pth` do lockfile é o *shim* do `distutils` do `setuptools`, que nada que o app importa usa; nem o app nem as bibliotecas da API, do banco e dos modelos chamam o `exit()` e o `quit()` que o `site` define |
| §5.2, §8 | *"`--only-binary=:all: --platform win_amd64 …`"*; *"o `torch` e o `torchvision` são `+cpu`"* | **O `langdetect` 1.0.9 não tem roda**: só o sdist existe, e o venv o construiu. O lockfile o fixa pelo `sha256` do sdist, com `--no-binary langdetect`, e o `pip` o constrói com o `setuptools` do venv, sem buscar outro (`--no-build-isolation`). Sem `--platform`: o Python que materializa é o 3.12 de 64 bits do venv, a mesma plataforma, e o `--platform` exige que nada seja construído. **E o `torchvision` do venv é o 0.28.0 do PyPI**, que no Windows é uma build de CPU sem o rótulo `+cpu`; o índice de CPU do PyTorch tem um `0.28.0+cpu`, que um `==0.28.0` aceitaria e o `pip` preferiria. **O lockfile usa `===`**, a versão exata, e o teste confere que o `torch` é `+cpu` e o `torchvision` é o do venv |
| §5.5 | O backend pedido pelo nome; *"sem o `torchvision` instalado, carregar falha"* | **O `transformers` 5.15 volta ao PIL mesmo com o nome pedido**: sem o pacote, `_load_class_with_fallback()` tenta o outro backend. O argumento chega ao processador de imagem pelo `AutoProcessor` (conferido: `backend="pil"` dá o `CLIPImageProcessorPil`), e **o adaptador confere a classe depois de carregar**, pela ancestralidade (`TorchvisionBackend`), e falha com a linha do RFC-035 §6.4. E a hipótese de §2.2 foi medida: em dez imagens do dataset, os `pixel_values` dos dois processadores diferem em até 0,015 |
| §5.3 | As DLLs do Visual C++ que as importações pedirem | **Duas: o `msvcp140.dll` e o `msvcp140_atomic_wait.dll`**, que ninguém tinha previsto e que só o `torch_python.dll` importa. O `vcruntime140.dll` e o `vcruntime140_1.dll` já vêm no *embeddable*. Com o Python do pacote, as quatro carregam de `runtime\python`, e não do `System32`; o `msvcp140.dll` do `System32` desta máquina é, para a conferência, uma DLL que o Windows não protege |
| §7.1 | O instalador por usuário em `%LOCALAPPDATA%\Programs\SolidVision` | **O electron-builder 26.18.0 dá à pasta o `name` do `package.json`** num instalador de um clique por usuário (`getWindowsInstallationDirName()`), e o do npm é minúsculo: seria `Programs\solidvision`. **`extraMetadata.name` é `SolidVision`**, e nada em tempo de execução o lê: o nome do app é o do `app.setName()`. E duas opções que §7.1 não citou: `packElevateHelper: false`, porque o default põe o `elevate.exe` em `resources`, e `differentialPackage: false`, que é como a 26.18.0 tira o *blockmap* e o pacote diferencial |
| §5.1 | O `sha256` do *embeddable* conferido com o do python.org | **O python.org não publica o `sha256` deste arquivo**: a API de downloads dá o `md5` e o tamanho (`f34996cc…`, 11 098 289 bytes). O `sha256` fixado foi calculado do download cujo `md5` e tamanho são os publicados |
| §7.3 | *"Cada nome resolve num arquivo do próprio pacote, ou entre as DLLs do Windows"* | **O `pgxml.dll` do PostgreSQL importa o `libxslt.dll`, que o ambiente do conda-forge não tem**, e o primeiro build parou nele. É a extensão `xml2` do contrib, e o pacote `postgresql` não depende do `libxslt`. Não é o defeito que a conferência caça — uma DLL que esta máquina tem e a próxima não —: ela não carrega em máquina nenhuma, e o app não cria extensão além do `vector`. **A conferência a desculpa pelo caminho do arquivo e com o motivo**, só ela, e diz isso em todo build (`NEVER_LOADED_BY_POSTGRES`); o mesmo nome pedido por qualquer outro binário falha. O RFC-033 §5.3 tinha conferido o servidor e o `vector.dll`, e não os outros módulos do `Library\lib`. Podar o runtime do PostgreSQL continua fora (§11) |

### 14.2 O que o texto não previa

- **Os `.pyc` do `site-packages` são escritos uma vez**, pelo `vendor_python.py`, com o `python.exe` da árvore; a montagem os copia e compila só o `backend`, também com o Python do pacote. Compilar o `torch` a cada build custaria minutos, e o resultado seria o mesmo.
- **As árvores `licenses` viram zip na montagem**, e não no `runtime\python` do checkout, que nunca é instalado: o orçamento é o do lugar instalado, e quem o conhece é o `check_package.py`. O `runtime\python` do checkout fica como o `pip` o deixou.
- **O electron-builder roda duas vezes**: `--dir`, e as conferências da pasta, e depois `--prepackaged` sobre ela, para o NSIS. Assim a pasta que vira instalador é a que foi conferida, e uma falha para o build antes da compressão, como §7.2 pedia.
- **O `pip --target` escreve os *console scripts*** em `Lib\site-packages\bin`, como lançadores do Python do venv, com o caminho desta máquina. O app não roda nenhum, e eles saem.
- **O `--lock` confere os bytes, e não só as versões**: cada arquivo de cada roda baixada tem de ser, pelo `RECORD`, o arquivo instalado no venv. Uma versão nomeia uma publicação; os 12 060 arquivos do `torch` provam que a roda é a do venv.
- **As listas de "antes" do teste em conta limpa são do PowerShell**: antes de instalar, o Python do pacote ainda não existe na conta. O `check_install.py` as compara com as de depois, e imprime a lista de passos (`check_install.py steps`).
- **O *"`search: ready`"* de §9 vem da linha da API**, *"Embedding model warmed up in …"*, que o launcher repassa ao `shell.log` com a hora em segundos: nenhum evento da casca o marca.

## 15. Validação

Na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA), em 2026-10-26. Os logs ficam em `experiments/rfc-041-installer/`, fora do git: `vendor_python.lock.log`, `vendor_python.log`, `dist.log`, `smoke_installed.log` e `smoke_window.log`.

### 15.1 O build

`npm run dist`, do comando ao instalador, numa segunda tentativa inteira — a primeira parou nas conferências (§14.1, o `pgxml.dll`):

| passo | tempo |
| --- | --- |
| 1. `npm run build` | 14 s |
| 2. A montagem, com a anterior apagada antes | 251 s (173 s sem ter o que apagar) |
| 3. As conferências da montagem | 251 s, dos quais 174 s os vetores |
| 4. `electron-builder --dir` e as conferências da pasta | 438 s |
| 5. O NSIS | 1 263 s |
| **Total** | **2 217 s, 37 min** |

Antes, uma vez por lockfile: `vendor_python.py --lock` e a materialização, 7,0 min, com os downloads; a materialização sozinha, 6,2 min. O lockfile tem 57 pacotes e 182 MB de rodas, e o `torch` +cpu é 116 MB delas.

### 15.2 As conferências de §7.3

| conferência | o que passou |
| --- | --- |
| A árvore | Sem `pyproject.toml`, `.env` nem `tests\`; o backend, a interface e os três runtimes presentes |
| O lockfile | 57 pacotes, os do lockfile, lidos pelo `importlib.metadata` do Python do pacote. O `site-packages` tem **865,2 MB**, contra os ~820 MB da conta de §2.7. No `--lock`, cada arquivo de cada roda conferido com o instalado no venv: os 12 060 do `torch`, os 197 do `torchvision`, e assim por diante |
| O orçamento de caminho | O mais longo da pasta instalada tem **234 de 259** caracteres, um `.pyc` do `transformers`. A árvore `licenses` do `torch` virou `licenses.zip`: sem ela, o mais longo teria 279, e os cinco acima do orçamento eram todos dela, como §2.5 contou |
| As DLLs | Python: 110 binários, 26 nomes achados no runtime, 44 no Windows, nenhum faltando. PostgreSQL: 334 binários, 64 e 23, o `pgxml.dll` desculpado (§14.1). A casca: 7 binários, 1 e 65, nenhum faltando. As DLLs do Visual C++ ao lado do `python.exe`: `msvcp140.dll` e `msvcp140_atomic_wait.dll` |
| Os vetores | No modo instalado, pelo `python.exe` da montagem, e na máquina que gravou a referência: as 45 imagens uma a uma e em lotes de 8, **bit a bit**; as 50 consultas traduzidas para as mesmas 50 strings, e os 50 vetores **bit a bit**. O processador, o `CLIPImageProcessor` do `torchvision`. 174 s, com a carga dos dois modelos |
| O `app.asar` | 11 arquivos, 78 KB: os nove `.js` do `dist-electron`, o `startup.html` e o `package.json`. Nenhum `node_modules`, embora o electron-builder os tenha procurado |
| `resources` e a montagem | Os mesmos 34 941 arquivos, 1,85 GB, sem o `SOLIDVISION-STAGED.json` |
| Os fusíveis | Lidos do `SolidVision.exe`: `RunAsNode`, `EnableNodeOptionsEnvironmentVariable` e `EnableNodeCliInspectArguments` desligados; os outros, nos defaults do Electron |

### 15.3 Os critérios de §9

| critério | resultado |
| --- | --- |
| Numa conta sem nada e sem rede: instalar, abrir, indexar e buscar | **Pendente**: pede a segunda conta, e criá-la é ato do administrador da máquina. A lista de passos está no `check_install.py steps`. O pacote foi aberto nesta conta sem instalar (§15.4) |
| O tamanho do instalador | **1,03 GB** (1 109 415 480 bytes). Dentro dele, 34 960 arquivos e 2,32 GB descompactados, e a pasta instalada tem 2,16 GB. A conta de §2.7 dava ~2,2 GB; o teto do NSIS, 2 GB, ficou longe |
| A primeira abertura, a frio | **Pendente** (a segunda conta). Como informação, a pasta do build aberta com um `LOCALAPPDATA` vazio: o marco `home` em **63,4 s**, o `ready` em 59,6 s, com o `initdb`. Não é a frio no sentido de §9: os arquivos tinham acabado de ser escritos, e parte deles estava no cache do disco |
| A abertura a quente, pelo atalho | **Pendente** |
| Desinstalar remove o programa, e os dados ficam | **Pendente.** O comando que a macro de §7.4 roda foi medido sobre o pacote: `launcher stop` saiu com 0 em 17,6 s, e nenhum processo do pacote ficou. A macro está no script que o NSIS compilou, e não rodou |

Das cinco verificações, uma passou nesta máquina: **nada fora de dois lugares** — na abertura de §15.4, o `LOCALAPPDATA` vazio ganhou só o `SolidVision`, com o perfil do Chromium em `shell`, e o `%APPDATA%\SolidVision` não foi criado. **De onde veio cada módulo** foi conferido para o Python do pacote, fora da segunda conta: com o `torch` importado, o `msvcp140.dll`, o `msvcp140_atomic_wait.dll`, o `vcruntime140.dll` e o `vcruntime140_1.dll` carregaram de `runtime\python`, e não do `System32`, que tem o seu `msvcp140.dll`. **As conferências do build** são as de §15.2. O SmartScreen e as duas verificações que ficaram para trás são da segunda conta.

### 15.4 O pacote aberto sem instalar

Duas aberturas da pasta `release\win-unpacked`, cada uma com um `LOCALAPPDATA` vazio num diretório de trabalho, que isola a raiz de dados e, desde §6.2, o perfil do Chromium:

- **O backend, sem janela**: `resources\runtime\python\python.exe -m app.launcher`. O `ready` em 47,3 s, com o `initdb` e as migrations, e a busca pronta em 98,3 s; o `/health` com `environment: installed`; a interface servida em `/`, e o `/devices`, o `/capabilities` e uma busca em português com 200; 13 processos do pacote. `launcher stop`: 0, em 17,6 s, e nenhum processo ficou. Um `du` varria o disco ao mesmo tempo: os tempos são informação.
- **A janela**: o `SolidVision.exe`, com `ELECTRON_RUN_AS_NODE=1` e `SOLIDVISION_PYTHON` apontado para um arquivo que não existe, de propósito. Abriu como app, e não como Node (o fusível), e subiu o `python.exe` do pacote (a variável ignorada). Os marcos: `main` 6 470 ms, `launcher` 6 646, `window` 21 272, `ready` 59 619, `page` 62 324, `home` 63 433. Fechada por `WM_CLOSE`: do `close` ao `stopped`, 19,7 s, com o executor carregando o modelo (RFC-037 §16); nenhum processo ficou. O `main` em 6,5 s é a primeira execução de um `SolidVision.exe` de 247 MB recém-escrito, e o que dela é o Defender não foi separado.

### 15.5 Os testes

No backend, 75 testes novos ou reescritos (`test_vendor_python.py` 26, `test_check_package.py` 23, `test_stage_installation.py` 24, `test_version.py` 2) e três no adaptador, uma delas com três casos. No `frontend/`, 369 testes em 35 arquivos, com os do ramo instalado, do `userData` e da configuração do electron-builder. A suíte padrão do backend: 2 843 passaram; os 301 erros e as 2 falhas são todos `OperationalError` do PostgreSQL de desenvolvimento, no Docker, que estava desligado, e nenhum é de um arquivo que este RFC tocou.
