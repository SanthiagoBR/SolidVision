# RFC-037 — Fundação e Casca do Frontend

**Status:** Implementado. A medição de §15 cumpriu os três critérios de §10, a quente, na máquina do RFC-036: a janela visível 767 ms depois de o processo do Electron ser criado e a tela inicial com os números da API em 6,42 s; do fechar ao `stopped`, 1,68 s, sem nada deixado para trás e sem nenhuma janela de console; e a segunda abertura trazendo a primeira de volta, com um launcher só. Dez minutos com a tela aberta não deixaram nenhuma linha de `/health` nos logs, o `Host` e o `Origin` recusaram o que deviam, e a página voltou 4,1 s depois de a API ser morta. A medição mostrou um defeito do RFC-036, que este RFC registra sem consertar (§16.1): fechar a janela enquanto o `job_runner` ainda carrega o modelo levou até 24 s. O Node 24 das ferramentas não está instalado nesta máquina (§16.1)
**Etapa:** 0 do [roadmap do frontend](ROADMAP-frontend.md#etapa-0--fundação-e-casca-rfc-037--ter-20-e-qua-21)
**Depende de:** RFC-036 (o contrato de [§14](rfc-036-launcher-e-supervisao-de-processos.md#14-o-que-passa-adiante) e os eventos de §4.4), RFC-030 §6 (as duas guardas, e o 404 que esconde uma rota desligada), RFC-031 (§9 o `/capabilities`, §12 as correções do mockup), RFC-034 (§6.2 a raiz de dados por modo, e os defaults de caminho pelo layout), RFC-036 §5.2 (o campo `search` do `/health`)
**Bloqueia:** os RFCs 038 a 041
**Migration:** não
**Medição:** nenhum piloto. Os números citados no corpo vêm do RFC-036 §16, dos logs do protótipo de setembro (`experiments/rfc-032-client-foundation/`) e de uma consulta ao registro do npm feita em 2026-10-19. Os da implementação vêm de `experiments/rfc-037-frontend-foundation/measure_shell.py`, rodado em 2026-10-20 na mesma máquina, com os logs ao lado (§15). Os fatos sobre o código vêm da leitura do `develop` em `191bde0`, e cada um diz o arquivo

> **Um RFC curto, como o roadmap pediu** ([O prazo](ROADMAP-frontend.md#o-prazo-e-o-que-ele-obriga)): as decisões, os critérios de pronto com no máximo três números `TBM`, e o que ficou de fora. A convenção de rascunho continua valendo: todo número marcado `TBM` é *a medir*, e o que não é medição diz que é escolha ou hipótese.

---

## 1. Contexto

O RFC-036 entregou um backend que sobe com um comando e diz, em JSON no `stdout`, quando está pronto. Ninguém lê esses eventos ainda: o contrato do §14 foi exercido por um programa que faz o que uma janela faria, e não por uma janela (RFC-036 §17.3). O diretório `frontend/` existe e está vazio. O `.gitignore` já ignora `frontend/node_modules/`, `release/` e `resources/` (`e9c8b3a`), do protótipo de setembro, cujo código não está no repositório.

Este RFC liga uma janela Electron ao launcher e põe a tela inicial do Figma (`44:458`) de pé com dados reais. Decide a estrutura do `frontend/`, como a API serve a interface, como a casca trata cada evento, e o que muda no backend para que uma interface possa existir sem abrir uma brecha nova.

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| `frontend/` com Vite, React, TypeScript, ESLint, Prettier e Vitest; tipos do OpenAPI com o teste de deriva | §4. A deriva muda de lugar (§2.5) |
| Os tokens e os componentes do shell, em duas ou três chamadas ao Figma | §8 |
| A interface servida pela API, com fallback para o `index.html` (D2) | §5.1. O fallback não é uma rota (§2.2) |
| O `Host` conferido contra `127.0.0.1` e `localhost` | §5.2. Com o `Origin` junto (§2.3) |
| O `/health` sem log INFO a cada chamada | §5.3. E o access log (§2.4) |
| Lock de instância única; o launcher com `windowsHide`, stdin e `stdout` em pipe, `stderr` num arquivo | §6.1, §6.2. O `stderr` também em pipe (§2.7) |
| A tela de subida, com o `initdb` dito como *"Preparando o banco pela primeira vez"* | §6.3 |
| Um destino para cada evento | §6.3. O `already_running` sem URL (§2.6) |
| Fechar: fechar o stdin e esperar `stopped`, com prazo; o diálogo da D3 | §6.4 |
| O shell e a tela inicial com dados reais; o estado da busca | §7 |
| *Riscos:* a API reiniciando; o caminho do Python | §6.3, §6.2 |

## 2. O que o estado atual muda no roadmap

O roadmap foi escrito hoje, a partir dos RFCs. A leitura do código e do ambiente encontrou oito pontos em que ele precisa de um ajuste. Nenhum muda uma das decisões D1 a D5.

### 2.1 O Node desta máquina não roda as ferramentas atuais

A D1 partiu de *"o Node 20 e o npm já estão nesta máquina"*. Estão: Node 20.12.2 e npm 10.5.0. O registro do npm, consultado hoje, diz o que as versões atuais exigem:

| pacote | versão atual | Node exigido (`engines`) |
| --- | --- | --- |
| `vite` | 8.3.4 | `^20.19.0 \|\| >=22.12.0` |
| `vitest` | 5.0.5 | `^22.12.0 \|\| ^24.0.0 \|\| >=26.0.0` |
| `react-router` | 8.5.0 | `>=22.22.0` |
| `eslint` | 10.13.0 | `^20.19.0 \|\| ^22.13.0 \|\| >=24` |
| `electron` | 44.9.0 | Traz o próprio Node. O pacote depende de `@types/node` `^24.9.0`, o que indica o 24 dentro dele |

E o Node 20 saiu de suporte em 2026-04-30. O protótipo de setembro rodou no 20.12.2 com versões que o repositório não guarda. **O Node das ferramentas passa a ser o 24 LTS**, o mesmo major que o Electron indica levar, declarado em `frontend/.node-version` e no `engines` do `package.json`. Ele não vai para o instalador: o Electron leva o próprio.

### 2.2 Um `Mount("/")` mudaria o 404 que esconde o reveal

A guarda 1 do RFC-030 §6 faz o `POST /api/v1/images/{id}/reveal` desligado responder 404, com o corpo de uma rota que não existe: quem sonda a API não aprende nada que um caminho errado não diria. O jeito óbvio de servir a interface, um `StaticFiles` montado em `/` depois dos routers, quebra isso. Toda requisição sem rota cai no mount, e o `StaticFiles` só atende `GET` e `HEAD`: para o resto, levanta 405 (lido no Starlette instalado, `staticfiles.py`). `POST /api/v1/qualquer-coisa` passaria de 404 a 405, e o reveal desligado seria o único `POST` a um caminho "inexistente" que responde 404: a guarda passaria a apontar a rota que existe para esconder. Uma rota curinga `/{path:path}` faz o mesmo, porque no Starlette uma rota que casa o caminho e não o método é 405.

**O fallback para o `index.html` fica no tratamento do 404**, e não numa rota nem num mount em `/` (§5.1). O roteamento da API não muda.

### 2.3 Conferir o `Host` fecha o DNS rebinding, e não o formulário

Uma página em `evil.example` cujo nome passa a resolver para `127.0.0.1` faz requisições com `Host: evil.example:<porta>` e lê as respostas como se fossem da mesma origem. Conferir o `Host` fecha isso, que é o que o roadmap pede.

Sobra o que uma página faz sem ler a resposta: um `<form method="post">` direto para `http://127.0.0.1:<porta>`, com o `Host` certo. As rotas com corpo JSON já recusam, porque um formulário não manda `application/json`, e o FastAPI só lê o corpo como JSON quando o tipo é `application/json` ou `+json` (lido no FastAPI instalado, `routing.py`); o resto chega como bytes e dá 422. Duas rotas `POST` não têm corpo: `/images/{id}/reveal`, que abre o Explorer, e `/jobs/{id}/cancel`. As duas pedem um id que a página não tem como saber: o da imagem deriva do GUID do volume (RFC-027), e o do job é `uuid4`. O risco é pequeno. Mas o navegador manda `Origin` em todo `POST` entre sites, e conferi-lo é uma linha a mais no mesmo middleware. **Os dois são conferidos** (§5.2). É o risco que o RFC-036 §13 listou como *"uma página aberta no navegador chamando a API"*. O que fica aberto é o processo local, e é do token por sessão, que continua fora.

### 2.4 O `/health` grava três linhas, e a maior parte não vai para o `api.log`

- **As duas linhas INFO** do roadmap estão em `routes/health.py` (*"Health check requested"* e *"Health check succeeded"*). Elas vão para o `api.log` e também para o handler de console (`logging.StreamHandler()`, no `stderr` da API). O `api.log` de desenvolvimento desta máquina tem 54 linhas *"Health check requested"*, todas de 2026-10-18.
- **O access log do uvicorn.** `api/__main__.py` monta o `uvicorn.Config` sem `access_log=False`, e o uvicorn escreve uma linha por requisição no `stdout` do filho.
- **O launcher repassa as duas saídas do filho ao próprio `stderr`** (RFC-036 §5.5), e a casca grava esse `stderr` num arquivo.

Um `/health` a cada 2 s dá cerca de 86 mil linhas por dia no `api.log` e cerca de 130 mil no log da casca. E com a D2 o access log não é só do `/health`: cada miniatura (até 100 por busca), cada arquivo da interface e cada consulta da tela inicial passam pela mesma API. **O `/health` fica em silêncio quando responde 200, e a API supervisionada roda sem access log** (§5.3).

### 2.5 A deriva do esquema não precisa mais de HTTP

Em setembro, gerar o OpenAPI por `app.openapi()`, sem servidor, levou 18,27 s, contra 1,61 s por HTTP (`measure_drift.log`), porque importar a API importava o `torch`. O teste ficou no HTTP, e o log registrou o problema: *"um `npm test` que só roda para quem tem o venv montado"*. O RFC-036 §5.3 tirou o `torch` da importação: mediana de 2,126 s (§16 dele). E a suíte do backend já importa o app.

**A deriva se divide em duas metades, cada uma na suíte que tem o ambiente dela** (§4.3): o `pytest` compara `app.openapi()` com o `openapi.json` versionado no `frontend/`, e o `npm test` compara os tipos com o que o `openapi-typescript` gera desse arquivo. Nenhuma das duas precisa de servidor de pé, nem do ambiente da outra linguagem.

### 2.6 O `already_running` nem sempre traz uma URL

`launcher/__main__.py` emite o `already_running` com a `url` do arquivo de estado, e um launcher em `starting` ou `stopping` ainda não tem URL: o evento sai com `"url": null`. O roadmap presumiu que ele sempre abre uma URL.

O lock de instância do Electron impede duas cascas, e a casca que fecha fica de pé até o `stopped` (§6.4). Então esse evento só chega em dois casos: um launcher iniciado num terminal, ou o launcher de uma casca que morreu, ainda parando pelo fim do stdin. O RFC-036 §16 mediu paradas de 1,25 a 2,13 s sem job, e de até 11,4 s com um. **Com `state: ready` e a URL, a casca abre a URL sem ser dona. Nos outros estados, tenta de novo** (§6.3).

### 2.7 O `stderr` num arquivo herda um handle, e o `windowsHide` depende disso

O roadmap põe o `stderr` do launcher num arquivo. No Node, um descritor de arquivo no `stdio` é um handle herdado. **Segundo o código do libuv** (`src/win/process.c`), que este RFC cita sem ter conferido nesta máquina, o `windowsHide` sempre cria o processo com a janela oculta (`SW_HIDE`), mas só acrescenta `CREATE_NO_WINDOW` quando nenhum handle do `stdio` é herdado. Com um handle herdado, o launcher ganharia uma janela de console criada oculta, em vez de nenhuma, e o RFC-036 §5.5 partiu de nenhuma. **Os três ficam em pipe, e a casca grava o `stderr` no arquivo** (§6.5). Isso também deixa as últimas linhas na memória da casca para a tela de erro.

### 2.8 O ambiente deste checkout

- O `README.md` cria o virtualenv em `.venv`, na raiz. Nesta máquina ele está em `backend\.venv`. A função que acha o Python tenta os dois (§6.2).
- O `.env` deste checkout não define `DATABASE_SOURCE`, que fica em `external` (o Docker), nem `API_PORT`. O primeiro critério de §10 pede `DATABASE_SOURCE=embedded`, e o `npm run dev` pede `API_PORT=8000` (§4.2). As duas são linhas no `.env`, que não é versionado. O `.env.example` já tem as duas, comentadas.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Ferramentas | Node 24 LTS; Vite, React e TypeScript; ESLint e Prettier; Vitest. Versões exatas no `package-lock.json` (§4.1) |
| Estilo | CSS Modules sobre variáveis CSS com os tokens do Figma. Nenhuma biblioteca de componentes nem de CSS (§4.1) |
| Dados | `openapi-typescript` para os tipos, `openapi-fetch` para as chamadas, TanStack Query para o cache e o polling (§4.3) |
| A deriva do esquema | `openapi.json` versionado. O `pytest` confere o backend contra ele, e o `npm test` confere os tipos contra ele (§2.5, §4.3) |
| Como a interface é servida | Pela API, na mesma origem (D2): `/assets` por `StaticFiles`, o `/` e o fallback de 404 com o `index.html`, só para `GET` fora de `/api/`. Um setting novo, `frontend_directory` (§5.1) |
| A brecha do navegador | Um middleware: o `Host` precisa ser `127.0.0.1` ou `localhost` (400); um método que escreve, com `Origin` de outro host, é recusado (403) (§5.2) |
| Os logs por requisição | O `/health` não grava nada num 200. A API supervisionada roda com `access_log=False` (§5.3) |
| A casca | Um processo principal Electron em TypeScript. Uma janela, aberta já na tela de subida local, que depois navega para a URL do launcher (§6.1) |
| O launcher | Iniciado com os três handles em pipe e `windowsHide`. O Python vem de uma função só (§6.2) |
| Fechar | Com um job em andamento, o diálogo da D3. Depois, *"Encerrando…"* até o `stopped`, com prazo de 60 s (§6.4) |
| O log da casca | `shell.log`, na pasta de logs da raiz de dados do modo, com os marcos de cada abertura (§6.5) |

## 4. O `frontend/`

### 4.1 Estrutura e ferramentas

```text
frontend/
  package.json, package-lock.json, .node-version
  vite.config.ts, tsconfig.json, eslint.config.js, .prettierrc
  electron/
    main.ts           ciclo de vida, lock de instância, a janela
    launcher.ts       iniciar o launcher, ler os eventos, parar
    events.ts         evento + estado -> ação; sem Electron, testado pelo Vitest
    python.ts         resolvePython(): a única função que muda na instalação
    preload.ts        a ponte para a página (§6.1)
    startup.html      subida, erro, reconectando e encerrando; sem React
  src/
    api/openapi.json  o esquema versionado (§4.3)
    api/schema.d.ts   gerado pelo openapi-typescript
    api/client.ts     openapi-fetch sobre os tipos
    theme/tokens.css  os tokens do Figma (§8)
    shell/            cabeçalho, barra lateral, layout
    home/             a tela inicial e as contas dela (§7)
  dist/               o build do Vite, servido pela API (fora do git)
  dist-electron/      o processo principal compilado (fora do git)
```

O processo principal é compilado pelo `tsc` para `dist-electron/`, sem plugin de Vite: são cinco arquivos, e o Vite não precisa saber deles. O roteamento das telas é o `react-router`. O protótipo mediu o custo dele em 24,55 KB em gzip (`measure_bundle.log`, §5), e numa interface servida em loopback isso não pesa. CSS Modules vêm com o Vite. Uma biblioteca de componentes traria um desenho que não é o do Figma.

### 4.2 Os comandos

| comando | o que faz |
| --- | --- |
| `npm run dev` | O Vite em `localhost:5173`, encaminhando `/api` e `/health` para `127.0.0.1:8000`, e o Electron, que sobe o launcher e, no `ready`, carrega o Vite em vez da URL. Pede `API_PORT=8000` no `.env` (§2.8). Se a porta do `ready` for outra, a casca mostra um erro que diz isso |
| `npm run dev:web` | Só o Vite, para usar a interface num navegador contra um launcher iniciado num terminal |
| `npm run build` | `tsc` nos dois lados e `vite build` |
| `npm start` | O Electron sobre o build: carrega a URL do `ready`, e a API serve o `dist/` (D2). É o caminho dos critérios |
| `npm test` | Vitest, e a conferência de que o `schema.d.ts` é o que o `openapi-typescript` gera do `openapi.json` |
| `npm run lint`, `npm run format:check`, `npm run typecheck` | ESLint, Prettier e `tsc --noEmit` |
| `npm run api:types` | Regenera o `openapi.json`, por `scripts/export_openapi.py`, e o `schema.d.ts`. É o único comando que precisa do Python |

### 4.3 Os tipos, e a deriva

`scripts/export_openapi.py` importa `app.presentation.api` e grava `app.openapi()` em `frontend/src/api/openapi.json`, com as chaves ordenadas. O arquivo é versionado. Um teste novo do backend, na suíte padrão, compara o objeto que `app.openapi()` devolve com o que o arquivo contém. Quem muda a API sem regenerar o esquema vê o `pytest` falhar. O `npm test` falha em seguida se o `schema.d.ts` não for regenerado.

O cliente é o `openapi-fetch`, com os caminhos e os corpos tipados pelo `schema.d.ts`. O TanStack Query cuida do cache, do polling e do `refetchOnWindowFocus`. Nenhuma URL é absoluta: a interface fala com a própria origem.

## 5. O backend

### 5.1 A interface servida pela API

- **`frontend_directory`**, um setting de caminho novo, o sexto de `PATH_DEFAULTS`, com o default pelo layout como os outros (RFC-034 §6.3). Num checkout é `frontend\dist`, na raiz do repositório. Na instalação é onde o RFC-041 puser, fora do `app.asar`, porque quem lê é o Python.
- **Com um `index.html` nesse diretório**, na criação do app: `StaticFiles` montado em `/assets`, que é onde o Vite põe os arquivos com hash; o `GET /` devolve o `index.html`; e um tratamento do 404 devolve o `index.html` para um `GET` ou `HEAD` cujo caminho não comece por `/api/` nem por `/assets/` (§2.2). O `index.html` vai com `Cache-Control: no-cache`, e os arquivos com hash ficam com o `ETag` do `StaticFiles`.
- **Sem o `index.html`**, nada é montado, e uma linha INFO na subida diz onde a interface foi procurada. O `npm run dev` não precisa dela, e a suíte do backend também não.
- **Não muda:** o `/health`, o `/docs` e o `/openapi.json` onde estão; o 404 JSON de um caminho desconhecido em `/api/`, e o 404 de um `POST` desconhecido; nenhum CORS.

Um `/assets/arquivo-que-não-existe.js` continua 404. Um `index.html` velho, que aponte para arquivos de outro build, falha visível, em vez de receber HTML no lugar de JavaScript.

### 5.2 O `Host` e o `Origin`

Um middleware só, na frente de tudo, inclusive do `/health` e da interface:

| regra | recusa |
| --- | --- |
| O nome no `Host`, sem a porta, é `127.0.0.1` ou `localhost` | 400, como o `TrustedHostMiddleware` do Starlette |
| Num método que não seja `GET`, `HEAD` ou `OPTIONS`, um `Origin` presente tem o esquema `http` e o nome `127.0.0.1` ou `localhost`. `Origin: null` é recusado | 403 |

Um `Origin` ausente passa: o `curl`, os scripts de medição, o `TestClient` e a sonda do launcher não mandam. O Electron manda o da própria origem. O proxy do `npm run dev` manda `localhost:5173`, que passa.

**O custo para a suíte:** o `TestClient` usa `testserver` como `Host`. Há 21 arquivos de teste que constroem um, e os que o constroem sobre o `app` passam a usar `base_url="http://127.0.0.1"`. É o que o `test_capabilities_api.py` já faz com o endereço do cliente para a guarda de loopback, em vez de a guarda aceitar `testclient`. Aceitar `testserver` em produção seria aceitar um nome sem domínio, que a rede local resolve (LLMNR, NetBIOS) e um atacante nela pode responder.

### 5.3 Os logs por requisição

- `routes/health.py` perde as duas linhas INFO. O 503 continua com a linha WARNING: uma falha do banco é o que esse log existe para dizer.
- `api/__main__.py` passa `access_log=False` ao `uvicorn.Config`. Com isso, a última linha que um filho deixa, a que o evento `child` leva no `detail` (RFC-036 §4.4), deixa de poder ser um `GET /health 200`. O `python -m uvicorn app.presentation.api:app` continua com o access log, para quem desenvolve a API à mão.

## 6. A casca

### 6.1 A instância e a janela

- **O nome do app** é `SolidVision` empacotado e `SolidVision-dev` no desenvolvimento, definido antes do lock. Cada um tem o seu lock e o seu perfil do Chromium, e os dois rodam lado a lado, como as raízes de dados do RFC-036 §8.
- **O lock:** `app.requestSingleInstanceLock()`. A segunda casca sai na hora. A primeira recebe `second-instance`, restaura e foca a janela.
- **Uma janela**, mostrada logo, com o `startup.html` local. No `ready`, ela navega para a URL. Com `contextIsolation`, `sandbox` e sem `nodeIntegration`. Uma navegação para fora da origem da API é bloqueada (`will-navigate`), e `window.open` é negado.
- **A ponte** (`preload.ts`) expõe duas coisas à página: `onLauncherEvent`, que repassa os eventos `child` e `address`, e `openLogsFolder`. A interface funciona sem a ponte, como no `npm run dev:web`.

### 6.2 O launcher

```text
<python> -m app.launcher --supervised
```

Com `spawn`, `windowsHide: true` e os três handles em pipe (§2.7). O `stdout` é lido linha a linha, e cada linha é um JSON. Uma linha que não é JSON vai para o `shell.log` como anomalia, e é ignorada. O `stderr` é esvaziado sempre (§6.5): um pipe que ninguém esvazia trava o launcher (RFC-036 §4.4).

**`resolvePython()`** é a única função que sabe onde está o Python, como o RFC-036 §5.5 fez com a linha de comando dos filhos:

| modo | o Python | o diretório e o ambiente |
| --- | --- | --- |
| Desenvolvimento | `SOLIDVISION_PYTHON`, se definida; senão `.venv\Scripts\python.exe` (o `README.md`); senão `backend\.venv\Scripts\python.exe` (esta máquina) | A raiz do repositório, com `PYTHONPATH=backend` (RFC-036 §9) |
| Instalado | O que o RFC-041 decidir. O ramo existe, e falha com uma linha que diz isso | O RFC-041 |

### 6.3 Cada evento, e o que a casca faz

| evento | o que a casca faz |
| --- | --- |
| `starting` | Confere `protocol` 1, como no `already_running`. Outro valor é uma tela de erro: a casca e o backend são de versões que não se entendem |
| `phase` | Troca o texto da tela de subida (abaixo) |
| `ready` | Navega para a `url`. No `npm run dev`, para o Vite. A casca passa a ser dona do launcher |
| `already_running`, com `state: ready` e a `url` | Navega para a `url`, sem ser dona: fechar a janela não para o launcher |
| `already_running`, em outro estado | Espera 1 s e inicia o launcher de novo, até 120 s, o prazo de pronto do próprio launcher (RFC-036 §17.2). Cada tentativa custa cerca de 520 ms (RFC-036 §16) (§2.6) |
| `failed` | Tela de erro com o `detail` e o botão *"Abrir a pasta de logs"* |
| `child` | Repassa à página. Com `api` em `restarting`, mostra *"Reconectando…"*; com `api` em `running`, recarrega a URL |
| `address` | Navega para a URL nova |
| `stopping`, `stopped` | *"Encerrando…"*, e então a casca sai |
| O processo sai sem `failed` nem `stopped` | Antes do `ready`: tela de erro com as últimas linhas do `stderr`. Depois: *"O SolidVision parou"*, com *"Abrir de novo"*, que inicia o launcher outra vez |

Um `did-fail-load` na URL da API, fora do que os eventos explicam (uma casca que não é dona não recebe os `child`), mostra *"Reconectando…"* e tenta de novo a cada 1 s. Depois de 30 s sem ser dona, a casca inicia o launcher, que ou vira dono ou diz onde está o outro. Os intervalos são escolhas.

**Os textos da tela de subida:**

| a última fase | o texto |
| --- | --- |
| Nenhuma ainda | *"Abrindo o SolidVision…"* |
| `initdb` | *"Preparando o banco pela primeira vez. Leva cerca de meio minuto."* O RFC-036 §16 mediu 35,0 s a frio |
| `starting` | *"Iniciando o banco…"* |
| `recovering` | *"Recuperando o banco depois de um encerramento inesperado…"* |
| `migrating` | *"Atualizando o banco…"* |
| `api`, com `database: external` | *"Iniciando o servidor…"* |

Com o banco embarcado, a fase `api` não troca o texto. Desde o segundo ramo do RFC-036 §5.1, a API é criada junto com o cluster, e o texto voltaria de *"Iniciando o servidor"* para *"Atualizando o banco"*.

### 6.4 Fechar a janela

1. **Sem ser dona**, a casca fecha a janela e sai.
2. **Sendo dona**, pergunta `GET /api/v1/jobs?status=running`. Com um job, o diálogo da D3: *"A indexação pausa e continua na próxima abertura"*, com *"Fechar"* e *"Continuar aberto"*.
3. A janela mostra *"Encerrando…"*, e a casca fecha o stdin do launcher.
4. Espera o `stopped`, ou a saída do processo, por até 60 s. A conta é a dos prazos do launcher: 30 s para o `job_runner`, ao mesmo tempo que os 15 s da API, mais o `stop()` do cluster, medido entre 0,8 e 9,2 s (RFC-036 §7.2 e §16). O 60 é uma escolha.
5. Passado o prazo, `kill()`, que no Windows é o `TerminateProcess`. O custo é o do RFC-036 §7.4: os filhos param pelo fim do stdin e o cluster fica, para a próxima subida adotar. Uma linha no `shell.log` diz isso.

A janela fica visível até o `stopped`, em vez de sumir antes. Quem reabre durante a parada cai no lock da casca que ainda está saindo, e a janela mostra por quê.

No fim da sessão do Windows, a casca não faz nada de especial. O launcher tem a própria janela oculta para o `WM_ENDSESSION` (RFC-036 §7.5), e o stdin que se fecha quando a casca sai é o mesmo pedido de parada.

### 6.5 O log da casca

- **`shell.log`**, na pasta `logs` da raiz de dados do modo: `%LOCALAPPDATA%\SolidVision\logs` empacotado e `SolidVision-dev\logs` no desenvolvimento (RFC-034 §6.2). A casca não lê a configuração: um `LOG_DIRECTORY` mudado no arquivo do usuário não muda onde ela grava. O `shell.log` já tem as linhas de todos os filhos, que passam pelo `stderr` do launcher.
- **O que entra:** o `stderr` do launcher, como chega; as linhas da própria casca; e os marcos de cada abertura, que são os números de §10: o processo criado, a janela visível, o `ready` e a tela inicial pintada com dados, que a página informa pela ponte.
- **A rotação:** na abertura, um `shell.log` acima de 10 MB vira `shell.log.1`, o limite dos logs do backend. Um processo só escreve nele, e a rotação do RFC-034 §8.3 não é necessária.
- **As últimas 50 linhas** ficam na memória, para a tela de erro. São as mesmas 50 do launcher (RFC-036 §5.5).

## 7. O shell e a tela inicial

**O shell** é o cabeçalho, a barra lateral e o conteúdo da `Page 1`, com as correções do RFC-031 §12. Nesta etapa, a barra lateral é uma lista dos dispositivos, com o rótulo e se o disco está conectado. Os cartões são a Etapa 2.

**A tela inicial** é o `44:458`: a barra de busca, as pílulas *"Dispositivos · todos (N)"* e *"Filtros"*, as três consultas de exemplo, a linha de resumo e o aviso de disco nunca indexado. A barra, as pílulas e as consultas de exemplo são desenhadas aqui e ligadas na Etapa 1.

| na tela | de onde vem |
| --- | --- |
| Dispositivos, e o N da pílula | O tamanho de `GET /devices` |
| Fotos indexadas | A soma de `indexed_images`, com `Intl.NumberFormat("pt-BR")`: *"1.204"* |
| Última indexação | O **maior** `finished_at` de `GET /jobs?status=completed`. A lista vem por `created_at` decrescente (`postgres_indexing_job_repository.py`), e um job criado antes pode terminar depois. Sem nenhum, *"nenhuma indexação ainda"* |
| O aviso | Os dispositivos com `indexed_images` 0 e `active_job` nulo. Um nomeado no aviso, e *"e mais N"* se houver outros. O botão *"Escolher pastas"* chega com a tela de pastas, na Etapa 2 |
| Nenhum dispositivo | *"Nenhum dispositivo registrado"*. Adicionar é a Etapa 2 |

**O estado da busca**, ao lado da barra, do `search` do `/health`:

| `search` | o texto |
| --- | --- |
| `loading` | *"Carregando o modelo de busca…"*. A quente, levou 17,5 s depois do `ready` neste HDD (RFC-036 §16) |
| `unavailable` | O `search_detail` |
| `ready`, `on_demand` | Nada |

**O polling:** o `/health` a cada 2 s enquanto a busca está em `loading`, e a cada 30 s depois. `/devices` e `/jobs` a cada 30 s e quando a janela volta ao foco. São escolhas: o polling da conexão dos discos, em segundos, é da Etapa 2.

## 8. O Figma

O orçamento do roadmap para esta etapa é de duas ou três chamadas. O MCP passou a pedir uma sessão aberta antes de qualquer leitura de arquivo (`create_session`), e ela conta como uma, porque nada aqui diz que não conta no limite do plano Starter.

| chamada | para quê |
| --- | --- |
| `create_session` | A sessão do arquivo `jtztdSy3VPU9BzDWXHdJtQ` |
| `get_variable_defs` em `44:458` | Os tokens: cores, tipografia, raios e espaçamentos, para `src/theme/tokens.css` |
| `get_design_context` em `44:458` | A tela inicial |
| `get_design_context` no shell da v2, se o link chegar | O cabeçalho e a barra lateral corrigidos. Sem o link, o shell sai da `Page 1` com as correções do RFC-031 §12, sem chamada nova |

Se o arquivo não tiver variáveis definidas, os tokens saem dos valores do contexto de design do `44:458`, e o `tokens.css` diz isso num comentário. O resultado do MCP é uma referência a adaptar aos componentes daqui, e não código a copiar.

## 9. Testes

**No backend, na suíte padrão:**

- **A interface servida**, num app próprio, com um `dist` de mentira num diretório temporário: `/` e `/qualquer/tela` devolvem o `index.html`; `/assets/x.js` devolve o arquivo; `/assets/falta.js` é 404; `GET /api/v1/nada` é o 404 JSON de sempre; `POST /api/v1/nada` é 404, e não 405; sem `index.html`, `/` é 404.
- **O middleware:** `Host: evil.example` é 400; `127.0.0.1:<porta>` e `localhost:<porta>` passam; `POST` com `Origin: http://evil.example` é 403, e com `Origin: null` também; `POST` sem `Origin` passa; `GET` com `Origin` de fora passa.
- **O `/health`** não grava nenhum registro num 200 (`caplog`), e o 503 continua com o WARNING.
- **A API supervisionada** monta o `uvicorn.Config` com `access_log=False`.
- **O snapshot do OpenAPI:** `app.openapi()` é igual ao `openapi.json` versionado.
- **Os arquivos que constroem um `TestClient` sobre o `app`**, com o `base_url` novo.

**No `frontend/`, no `npm test`:**

- **As contas da tela inicial:** a soma, o maior `finished_at` (e não o primeiro da lista), o aviso só para quem tem 0 e nenhum job ativo, as listas vazias.
- **O estado da busca** para o texto, nos quatro valores.
- **`events.ts`**, sem Electron: cada linha de §6.3, inclusive o `already_running` sem URL, a porta errada no `npm run dev`, o `protocol` desconhecido e a saída sem `stopped`.
- **`resolvePython()`:** a ordem dos candidatos, e a linha de erro quando nenhum existe.
- **O `schema.d.ts`** é o que o `openapi-typescript` gera do `openapi.json`.

## 10. Critérios de pronto

Medidos na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA), num checkout com `DATABASE_SOURCE=embedded` no `.env` e o Docker desligado, depois de um `npm run build`. "A quente" é como no RFC-036 §16: uma abertura anterior já pôs os arquivos no cache do sistema. Cada número é a mediana de cinco aberturas, lida dos marcos do `shell.log` (§6.5) por `experiments/rfc-037-frontend-foundation/measure_shell.py`, que fecha a janela como um clique fecharia (`WM_CLOSE`).

| critério | o número |
| --- | --- |
| **`npm start` chega à tela inicial com números reais**, iguais aos que `GET /devices` e `GET /jobs` dão no mesmo momento | Da criação do processo do Electron à janela visível: `TBM` ms. O protótipo de setembro mediu 802 ms, com o backend já de pé. Da criação do processo à tela inicial com os números: `TBM` s. O backend sozinho leva 4,46 s até o `ready`, e a janela sobe ao mesmo tempo que ele |
| **Fechar a janela não deixa nenhum processo para trás**, pelo critério de identidade do RFC-036 §16, com os processos do Electron somados à lista | Do `WM_CLOSE` ao `stopped`, sem job: `TBM` s. O launcher sozinho levou de 1,25 a 2,13 s |
| **A segunda abertura traz a primeira janela para a frente**, e a lista de processos continua com um launcher só | Sem número |

E três verificações que não têm número:

- Dez minutos com a tela inicial aberta não acrescentam nenhuma linha de `/health` ao `api.log` nem ao `shell.log`.
- Contra o app de verdade: um `curl` com `Host: evil.example` recebe 400, e um `POST` com `Origin: http://evil.example` recebe 403.
- Nenhuma janela de console aparece em nenhuma abertura, e nenhum `conhost.exe` tem janela visível (§2.7).

## 11. Riscos

| risco | situação |
| --- | --- |
| A API reiniciando, e a página perdendo a origem por alguns segundos | Os eventos `child` e o `did-fail-load` levam a *"Reconectando…"*, e não a uma página em branco (§6.3) |
| O caminho do Python mudar entre o desenvolvimento e a instalação | Uma função só, e o ramo instalado é do RFC-041 (§6.2) |
| O Node 24 não poder ser instalado nesta máquina | Qualquer Node a partir do 22.22 satisfaz todos os `engines` de §2.1. O 20.12.2 não satisfaz nenhum dos quatro |
| O primeiro `npm install` baixar o Electron, que tem centenas de MB | Uma vez por máquina, com rede. Fica dito no `README.md` |
| Uma casca que não é dona de um launcher de terminal | Fechar a janela não para o launcher, e isso é o certo (§6.3). Se o terminal parar o launcher, a casca reconecta ou inicia outro |
| O prazo de 60 s cortar um `stop()` lento do cluster | Declarado: o cluster fica e é adotado (§6.4) |
| O logoff com a janela aberta | A verificação manual do RFC-036 §17.3 continua por fazer. O frontend não espera por ela |
| Um default de rota que vem do `Settings` aparecer no OpenAPI, e o snapshot depender do `.env` de quem gera | O `export_openapi.py` e o teste rodam com os defaults, e não com o `.env` de quem roda |
| O `react-router` exigir um Node mais novo que o resto | É o mais exigente hoje (22.22). Se a próxima versão subir de novo, a versão fica no `package-lock.json` |

## 12. Fora deste RFC

- A busca, os filtros, o detalhe e a busca por imagem (RFC-038). Aqui a barra e as pílulas são só desenho.
- Os cartões de dispositivo, adicionar, renomear, as pastas e os jobs (RFC-039). O botão *"Escolher pastas"* do aviso.
- O mapa, e a Content-Security-Policy que os tiles do OpenStreetMap vão pedir (RFC-040).
- O empacotamento, o Python embutido, o lugar do `frontend_directory` instalado e o ramo instalado do `resolvePython()` (RFC-041).
- O token por sessão do RFC-036 §14. O `Host` e o `Origin` fecham o navegador, e não um processo local.
- A bandeja, iniciar com o Windows, o tema escuro e outros idiomas.

## 13. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-037-fundacao-e-casca-do-frontend.md` | Este documento |
| `frontend/` | A árvore de §4.1 |
| `backend/app/presentation/frontend.py` | A montagem da interface e o fallback do 404 (§5.1) |
| `backend/app/presentation/request_guards.py` | O `Host` e o `Origin` (§5.2) |
| `scripts/export_openapi.py` | O `openapi.json` (§4.3) |
| `backend/tests/presentation/test_frontend_static.py`, `test_request_guards.py`, `test_openapi_snapshot.py` | §9 |
| `experiments/rfc-037-frontend-foundation/measure_shell.py` (+ `.log`, fora do git) | §10 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/presentation/api/__init__.py` | O middleware e a interface |
| `backend/app/presentation/api/__main__.py` | `access_log=False` |
| `backend/app/presentation/routes/health.py` | Sem as linhas INFO |
| `backend/app/infrastructure/config/settings.py` | `frontend_directory`, em `PATH_DEFAULTS` |
| Os arquivos de teste com `TestClient` sobre o `app` (21 constroem um `TestClient`) | `base_url="http://127.0.0.1"` |
| `.gitignore` | `frontend/dist/` e `frontend/dist-electron/` |
| `.env.example` | O comentário do `API_PORT` diz que o `npm run dev` o pede |
| `README.md` | O Node 24, o `npm install` e o `npm start` em "Como rodar"; o frontend na tabela de estado |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-frontend.md` | O status deste RFC |

Não mudam: o Domain, a Application, as migrations, o launcher e os eventos dele (o protocolo continua o 1), o `job_runner` e o `docker-compose.yml`.

## 14. O que passa adiante

**Para o RFC-038 (busca):** o cliente tipado e o TanStack Query; a barra, as pílulas e as consultas de exemplo já desenhadas; os tokens. O `POST /images/search/by-image` vai da própria origem, e o `Origin` dele passa (§5.2). O `npm run api:types` regenera os tipos das rotas novas.

**Para o RFC-039 (dispositivos e indexação):** a ponte já repassa os eventos `child`, e a faixa *"A indexação está indisponível"* lê o `job_runner` em `unavailable` dali. A lista da barra lateral vira os cartões.

**Para o RFC-040 (mapa):** o middleware não toca nas requisições que a página faz para fora, como os tiles. A CSP é dele.

**Para o RFC-041 (instalador):** o `frontend_directory` fora do `app.asar`; o ramo instalado do `resolvePython()`; o nome `SolidVision` como `productName`, que é o do lock e o da pasta do `shell.log`. O Node 24 das ferramentas não vai no pacote.

## 15. Validação

Os `TBM` de §10 ficaram como foram escritos, à vista do que se esperava; os números que os respondem estão aqui, e o que mudou por causa deles, em §16.

**Declarado antes da medição**, em §10 e no script:

- **"A quente" é o do RFC-036 §16:** uma abertura anterior já pôs os arquivos no cache do sistema. Cada número é a mediana de cinco aberturas, lida dos marcos do `shell.log` contra o momento em que o script criou o processo do Electron, e cada janela é fechada por `WM_CLOSE`, como um clique no X a fecharia.
- **"Com números reais" é o marco `home` dizer o mesmo que `GET /devices` e `GET /jobs?status=completed`**, perguntados pelo script logo depois dele: quantos dispositivos, a soma de `indexed_images` e o maior `finished_at`.
- **"Nenhum processo para trás" é o critério de identidade do RFC-036 §16, com os processos do Electron:** a árvore de cada janela, amostrada a cada 100 ms, mais os PIDs do `launcher.json`, todos terminados; e nenhum processo do runtime do PostgreSQL que não existisse antes da medição. Um processo que já existia antes da abertura nunca entra na árvore dela (§16.1).
- **"Nenhuma janela de console" é nenhuma janela visível de um processo da árvore**, além da própria janela, e nenhuma janela de console nova em toda a máquina, na mesma amostragem.

> **Medido em 2026-10-20, na máquina do RFC-036**: Windows 10 Home (10.0.19045), Pentium G4560 (2 núcleos, 4 threads), 16 GB, HDD SATA, Python 3.12.9, Electron 44.9.0, processo sem elevação. Num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` no ambiente da janela, e não no `.env`, e uma raiz de dados própria num diretório temporário, apagado no fim. **O Docker estava de pé**, com o container ocioso (§16.3). **Dois logs** ao lado do script, fora do git: `measure_shell.log`, todas as verificações; e `measure_shell.rerun.log`, o `prepare`, o `early` e o `warm` refeitos depois de duas correções do próprio script (o PID reciclado de §16.1, e o `early` antes do `warm`, porque as primeiras aberturas depois de uma subida a frio acharam o disco ocupado: `ready` em 12,9 a 19,2 s, contra 5,6 a 7,6 s algumas aberturas depois). Uma primeira rodada completa foi interrompida quando mostrou que uma janela encoberta não pinta (§16.1): o log dela foi sobrescrito pela seguinte, e os números dela que §16.1 cita foram lidos durante a rodada, do `shell.log` do diretório de trabalho.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte do backend | `pytest` puro | **2780 passed, 91 deselected**, em 159,2 s, contra o PostgreSQL do Docker, como o `.env` deste checkout manda. Eram 2713 no `191bde0` (RFC-036 §16). Os 62 testes novos: a interface servida (24), o `Host` e o `Origin` (35) e o snapshot do OpenAPI (3); e mais três nos arquivos que já existiam: o `/health` sem log num 200 e com o WARNING no 503, e o `access_log` do programa da API. Numa rodada feita no começo da implementação, um teste que importa um módulo num subprocesso falhou porque o `settings.py` estava a meio de uma edição; não voltou |
| As verificações do frontend | `npm test`, `npm run lint`, `npm run typecheck`, `npm run format:check` | **77 testes em 6 arquivos**: a máquina de estados de §6.3 (43), o `resolvePython()` (9), o `shell.log` (7), as contas da tela inicial (11), o estado da busca (6) e a deriva do `schema.d.ts` (1). O ESLint, o Prettier e os dois `tsc` limpos |
| Estilo e tipos do backend | `black --check`, `ruff check`, `mypy` | **Limpos.** O `mypy` com a configuração do projeto, 330 arquivos, e o `export_openapi.py` e o script da medição com o `backend` no caminho |
| `npm start` chega à tela inicial com números reais | `warm`, cinco aberturas a quente (`rerun`) | **A janela visível em 767 ms** (726 a 918 ms), **e a tela inicial com os números em 6,42 s** (6,35 a 6,63 s); o `ready` em 5,88 s (5,65 a 6,15 s). Os números iguais aos da API em todas as aberturas das duas rodadas: 2 dispositivos, 45 fotos e a última indexação, ao milissegundo. O protótipo de setembro mostrou a janela em 802 ms com o backend já de pé, e o backend sozinho chega ao `ready` em 4,46 s (RFC-036): com a janela e o Chromium subindo ao lado dele, 1,4 s a mais |
| Fechar a janela não deixa nada para trás | `warm` (`rerun`); `early`, nas duas rodadas | **Do `WM_CLOSE` ao `stopped`, com o executor no laço, 1,68 s** (1,60 a 2,26 s), e a janela sai 0,3 s depois; o `job_runner` para em 1,0 a 1,7 s. A árvore, de até 28 processos (o Electron, o launcher, os dois filhos com os redirecionadores do venv, os `conhost.exe` e o cluster), some inteira de 0,11 a 0,17 s depois da saída da janela, e nada fica, em todas as aberturas. **Fechando 1 s depois da tela inicial**, com o executor ainda carregando o modelo: mediana de 1,42 s (1,18 a 1,93 s) numa rodada e de 1,33 s na outra, em que uma das cinco levou 16,5 s, 15,6 s deles do `job_runner`; na rodada interrompida, até 24,0 s (§16.1) |
| A segunda abertura traz a primeira para a frente | `second`: a primeira janela minimizada, e uma segunda aberta | **A segunda saiu com 0 em 387 ms**, sem janela nem linha no log; a primeira voltou do minimizado, e o `shell.log` dela diz *"A second opening brought this window to the front"*. Um launcher só, e os mesmos quatro `python.exe` antes e depois |
| Nenhuma janela de console | Toda abertura, amostrada a cada 100 ms | **Nenhuma**, nas 27 aberturas das duas rodadas e na segunda abertura |
| Dez minutos sem `/health` no log | `quiet`: a tela inicial aberta por 600 s, com `WARM_UP_MODELS=true` | **Nenhuma linha**, nem no `api.log` nem no `shell.log`: o polling da página, a cada 2 s enquanto o modelo carregou e a cada 30 s depois, e 40 perguntas do próprio script, todas 200. A única linha de `/health` dos dois logs é o WARNING de um 503 durante a subida a frio, que §5.3 mantém |
| O `Host` e o `Origin` contra o app | `guards`: 11 requisições à API da janela | **Como dito, as 11.** `Host: evil.example`, 400; `localhost`, 200. `POST` com `Origin: http://evil.example` e com `null`, 403; sem `Origin` e com o da janela, o 404 de um job que não existe. `POST /api/v1/nada`, 404. `/` e `/qualquer/tela`, o `index.html`, com `Cache-Control: no-cache`; o script do build, 200 com `ETag`; `/assets/falta.js`, 404 |
| A API reiniciando com a página aberta | `reconnect`: `TerminateProcess` na API, com a tela inicial aberta | **"Reconectando…", e a página de volta 4,11 s depois**, na mesma porta, sem `address`, com os números (as duas capturas). O primeiro risco de §11 |
| A busca carregando | `loading`, com `WARM_UP_MODELS=true` | **O texto ao lado da barra enquanto o `/health` diz `loading`, e nada depois**: o `search: ready` veio 21,0 s depois da tela inicial, e a captura seguinte já não tem o texto. O RFC-036 mediu 17,5 s depois do `ready` |
| A primeira abertura, a frio | `prepare`: a raiz de dados vazia | **A janela em 0,99 s, com *"Preparando o banco pela primeira vez"*** (a captura), e o `ready` em 39,0 s, depois do `initdb` (29,8 s no `rerun`; o RFC-036 mediu 35,0 s). As 45 imagens do RFC-022 indexadas pela API em 54,7 s (44,5 s) |
| O Figma | §8 | **Três chamadas:** `create_session`, `get_variable_defs` em `44:458`, que não devolveu nenhuma variável, e `get_design_context` em `44:458`. A captura da tela inicial bate com a do Figma, menos o botão *"Escolher pastas"* (§16.2). O shell saiu dos mesmos tokens, sem chamada nova |
| O build | `vite build` | **359,4 KB de JavaScript, 112,7 KB em gzip**, e 8,1 KB de CSS (2,2 KB). As fontes à parte: 218 KB em sete subconjuntos, dos quais a página carrega os que usa. O protótipo tinha 80 KB, com o React 18 e sem fontes |

## 16. Correções feitas durante a implementação

Registradas à vista do texto original, como os RFCs 033 a 036 fizeram com as suas. Nenhuma muda uma das decisões de §3. Duas mudam o caminho de uma delas: o `npm run dev` passa a dar a porta ao próprio launcher, e o `npm start` passa por um script. E uma é um defeito do RFC-036 que a medição deste mostrou, e que este RFC registra sem consertar, porque o `job_runner` está entre o que não muda (§13).

### 16.1 O que a medição e o ambiente contrariaram

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §2.1, §11 | O Node 24 nas ferramentas | **Não está instalado nesta máquina**, que continua com o 20.12.2 no `PATH`. A implementação usou o zip oficial do 24.22.0, o LTS de 2026-10-14, com o `sha256` conferido contra o `SHASUMS256.txt` do nodejs.org, numa pasta à parte, sem mexer no Node do sistema. Instalar o 24 é uma decisão de quem tem a máquina, e o `README.md` diz que ele é pedido |
| §2.1 | As versões atuais do registro | **O TypeScript atual é o 7.0.2**, e nem o `typescript-eslint` (`<6.1.0`) nem o `openapi-typescript` (`^5.x`) o aceitam como peer: ficou o 5.9.3. E três pacotes tinham saído no dia anterior (`react-router` 8.5.0, `typescript-eslint` 8.72.0, `@tanstack/react-query` 5.104.5): ficaram as versões anteriores, com dias de registro (8.4.0, 8.71.2 e 5.104.3). O `package.json` fixa versões exatas, e uma atualização passa a ser um ato deliberado. O Electron é o 44.9.0, o de §2.1; o 44.9.1 saiu na madrugada do dia da implementação |
| §11 | *"O primeiro `npm install` baixar o Electron"* | **O Electron 44 não baixa nada no `npm install`:** o pacote não tem script de instalação, e o binário vem na primeira vez que alguém pede o caminho dele, conferido contra o `checksums.json` do pacote. Na prática, no primeiro `npm start`, que levou 9 s a mais nesta rede. São 369 MB descompactados |
| §4.2 | `npm start` é `electron .` | **Um processo iniciado a partir do VS Code herda `ELECTRON_RUN_AS_NODE=1`**, e o `electron .` roda o `main.js` como um script de Node, sem `app` e sem janela (o `electron --version` deu a versão do Node). O `npm start` e o `npm run dev` passam por `scripts/electron.mjs`, que inicia o Electron sem essa variável |
| §4.1 | O `preload.ts` compilado pelo `tsc` | **Sob `module: node16`, o `tsc` tratou todo arquivo como módulo**, inclusive um sem `import` nem `export`, e o `preload.js` saiu com `Object.defineProperty(exports, ...)`, que um preload com `sandbox` não tem. `moduleDetection: "auto"`, explícito no `tsconfig.electron.json`, deixa o preload um script, e o `main.js` um módulo CommonJS |
| §10 | A medição lê os marcos do `shell.log` | **Uma janela aberta atrás do editor não pinta.** O Windows não deixa um processo em segundo plano trazer uma janela para a frente, e o Chromium trata uma janela encoberta como oculta: não produz quadro, não roda `requestAnimationFrame` e a página não faz polling. Na primeira rodada completa, o marco da tela inicial, que espera um quadro, chegou 134 s e 158 s depois do `ready` em duas aberturas, e a captura da janela saiu toda branca, até alguém trazê-la para a frente. A medição passou a iniciar o Electron com `--disable-features=CalculateNativeWinOcclusion`, um switch do Chromium, e não do app: quem abre o SolidVision está olhando para ele |
| §10, RFC-036 §16 | O critério de identidade: os processos cujo pai é da árvore | **Um processo de fora entrou na árvore pelo PID do pai.** Numa abertura, o `jusched.exe`, o agendador de atualização do Java, que roda desde o logon, apareceu como filho de um processo da janela: o pai dele morreu há horas, e o Windows deu o mesmo PID a um processo da janela. O `jucheck.exe` que ele abriu no meio da medição veio junto, e a verificação acusou dois processos deixados para trás. É o efeito que o RFC-036 §17.2 viu com dois `conhost.exe`. Um processo que já existia antes da abertura não entra mais na árvore dela |
| §10, RFC-036 §16 | Do `WM_CLOSE` ao `stopped`, sem job; o launcher sozinho, de 1,25 a 2,13 s | **Depende de quando se fecha.** O `job_runner` carrega o modelo antes de entrar no laço, e uma parada pedida durante essa carga só é atendida quando a importação em andamento volta ou quebra. Fechando 1 s depois da tela inicial, com o executor ainda carregando, a parada do `job_runner` levou de 1,0 a 24,0 s na primeira rodada completa, e numa delas a interrupção caiu no meio do `import numpy.dtypes` e terminou num `RecursionError` e num `ModuleNotFoundError` do `transformers`; nas duas rodadas seguintes, de 0,5 a 15,6 s, e o fechamento inteiro, de 1,15 a 16,5 s. Fechando com o executor já no laço, que é a parada que o RFC-036 mediu, é o número de §15: 1,68 s. A medição passou a separar os dois casos (`warm` e `early`). É do RFC-036, e fica para quem mexer no `job_runner`: carregar o modelo só quando um job chega, ou tornar a carga interrompível |

### 16.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §4.2, §2.8 | **O `npm run dev` dá `API_PORT=8000` ao próprio launcher**, pelo ambiente, que no desenvolvimento vale mais que o `.env` (RFC-034 §5.1), em vez de pedir a linha no `.env`. Escrita no `.env`, a porta fixa valeria também para o `npm start` e para um launcher de terminal, o que o RFC-036 §5.4 evitou de propósito. O `npm run dev:web`, que é só o Vite, continua pedindo um launcher na 8000, e o `.env.example` diz isso |
| §5.1 | **O `/` também é servido pelo fallback do 404**, e não por uma rota: com uma rota em `/`, um `POST /` seria 405, o argumento de §2.2. Um `GET /api` e um `GET /assets`, sem a barra, também ficam com o 404 da API |
| §5.1 | **O `StaticFiles` só é montado quando `assets\` existe.** Sobre um diretório que não existe, ele responde 500 a cada arquivo, e não 404 |
| §5.2 | **Uma recusa do `Host` ou do `Origin` deixa uma linha WARNING no log**, com os dois cabeçalhos em `%r`, para que uma página hostil não quebre linhas. Sem o access log (§5.3), seria a única pista de um `curl` recusado |
| §6.3 | **Uma API que responde 404 no `/` é um erro na tela**, *"A interface não foi construída"*: é o `npm start` antes do `npm run build`, que de outro modo abriria um JSON de 404 na janela. O `did-navigate` dá o código da resposta |
| §6.3 | **"Reconectando…" pergunta à URL antes de carregá-la**, a cada 1 s, pelo processo principal, em vez de recarregar a página a cada 1 s: uma carga que falha mostra, entre uma tentativa e outra, a página de erro do Chromium |
| §6.3, §6.5 | **O console da página e a queda do processo dela vão para o `shell.log`.** Os avisos e os erros do console, onde o Chromium diz que uma requisição falhou; e o `render-process-gone`, que deixa a janela branca e agora a leva a *"Reconectando…"* e a uma carga nova |
| §6.1 | **As permissões que a página pedir são negadas**, menos a escrita na área de transferência, que é o *"Copiar caminho"* do RFC-038 |
| §4.1 | **`electron/log.ts`** é um sexto arquivo: o `shell.log`, a rotação e os marcos, testados sem Electron |
| §4.3 | **O tipo do `/health` é escrito à mão** (`src/api/health.ts`): a rota declara `response_model=None`, porque o 503 tem outro formato, e o OpenAPI não diz nada da resposta |
| §4.3 | **O `pytest` compara o documento como objeto, e o `npm test` compara o texto sem o fim de linha**: este checkout usa `core.autocrlf`, e um checkout novo recebe os dois arquivos com CRLF |
| §5.2 | **15 arquivos de teste constroem um `TestClient` sobre o `app`**, e não 21: os outros constroem apps próprios. Os dois que avisam que o `TestClient` não é loopback ganharam a segunda metade do aviso |
| §7 | **A barra de busca, as pílulas e as consultas de exemplo estão desabilitadas**, em vez de aceitar uma consulta que não seria feita; o aviso de disco nunca indexado não tem o botão *"Escolher pastas"*, que chega com a tela de pastas (RFC-039) |
| §7 | **O polling também volta quando a janela recebe o foco.** O TanStack Query 5 só escuta o `visibilitychange`, que uma janela atrás de outra, sem estar minimizada, não dispara |
| §8 | **A Inter vai no build** (`@fontsource-variable/inter`, OFL): a interface não carrega nada de fora, e sem ela a tela cairia na Segoe UI. A tela de subida, que é um arquivo local fora do build, usa a Segoe UI |
| §4.1 | **Uma Content-Security-Policy no `index.html` do build**: `'self'` para tudo, e `data:` para imagens. Só no build, porque o servidor do Vite injeta um script inline. O RFC-040 a abre para os tiles |
| §10 | **O aviso do disco nunca indexado precisou de um dispositivo escrito no banco da medição.** Esta máquina tem um volume só, e o `POST /devices` só aceita um volume montado: a medição escreve um `HD4` desconectado, pelo motor do app, no banco do diretório de trabalho |

### 16.3 O que ficou sem medição

Dito aqui para não parecer medido em outro lugar.

- **O Docker estava de pé**, com o container ocioso, durante as medições: desligá-lo é decisão de quem usa a máquina. O modo embarcado não fala com ele.
- **O prazo de 60 s do fechamento** (§6.4) só foi exercido pelos testes da máquina de estados: nenhuma parada medida chegou perto dele.
- **A rotação do `shell.log`** em 10 MB tem teste, e nenhum log chegou lá.
- **O logoff com a janela aberta**, pelo motivo do RFC-036 §17.3.
- **Um monitor com escala diferente de 100%** e **o alto contraste** do Windows.
