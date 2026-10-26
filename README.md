# SolidVision
> **Plataforma Inteligente de Recuperação de Imagens para Acervos Fotográficos Baseada em Embeddings**

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Framework-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![License](https://img.shields.io/badge/License-CC0_1.0-lightgrey.svg)](LICENSE)

O **SolidVision** é uma plataforma de busca semântica desenvolvida para resolver o desafio de localizar imagens em grandes volumes de dados não estruturados. Com **Inteligência Artificial Multimodal**, o sistema permite que fotógrafos profissionais recuperem fotografias por descrições em linguagem natural, sem depender de metadados manuais ou de nomes de arquivos.

---

## Estado atual

O backend está implementado até o **RFC-036**, e o frontend do **RFC-037** ao **RFC-041**, mais os RFCs 043 e 044: uma janela com a busca, os dispositivos e a indexação e o mapa, que se instala com um instalador, sem terminal e sem rede. Cada mudança do projeto é descrita em um RFC numerado, escrito antes da implementação e atualizado com as medições depois dela.

| Etapa | RFCs | Situação |
|---|---|---|
| Fundação, domínio, persistência, indexação | 001-021 | Pronto |
| IA e busca semântica (CLIP, pgvector, API de busca) | 022-026 | Pronto |
| Acervo real: dispositivos, datas, jobs, arquivos, geolocalização | 027-032 | Pronto |
| Sem Docker: PostgreSQL embarcado, configuração do usuário, modelos offline | 033-035 | Pronto |
| Launcher e supervisão de processos | 036 | Pronto |
| Frontend: a janela, a casca e a tela inicial | 037 | Pronto |
| Frontend: busca, dispositivos e indexação, mapa | 038-040, 043, 044 | Pronto |
| O instalador | 041 | Pronto; o teste numa conta limpa do Windows, pendente ([roadmap](docs/rfcs/ROADMAP-frontend.md)) |

Documentação: [índice dos RFCs](docs/rfcs/README.md), [roadmap sem Docker](docs/rfcs/ROADMAP-standalone.md), [roadmap do frontend](docs/rfcs/ROADMAP-frontend.md) e a [arquitetura](ARCHITECTURE.md).

---

## Funcionalidades

*   **Busca semântica:** pesquise por contexto visual (ex.: *"propriedade rural com lago"*) em vez de palavras-chave literais. As consultas em português são traduzidas para o inglês antes de chegarem ao modelo.
*   **Busca por imagem:** encontre as imagens do acervo mais parecidas com uma foto dada.
*   **Filtros:** por dispositivo, por intervalo de datas de captura (EXIF) e por proximidade geográfica.
*   **Indexação local:** varredura de diretórios e de HDs externos; os arquivos permanecem onde estão, sem upload.
*   **Zero-shot:** identifica conceitos visuais sem etiquetas prévias, graças ao modelo CLIP.
*   **Recuperação no banco:** similaridade de cosseno diretamente no PostgreSQL, com índice HNSW do pgvector.
*   **Funciona offline:** com o PostgreSQL embarcado e os modelos locais, não precisa de Docker nem de rede.

---

## Tecnologias

*   **IA:** CLIP LAION ViT-B/32 (embeddings de 512 dimensões), escolhido em uma comparação de quatro modelos (RFC-023).
*   **Banco de dados:** PostgreSQL 16 com a extensão pgvector, via Docker ou embarcado.
*   **Backend:** Python 3.12 com FastAPI, SQLAlchemy e Alembic.
*   **Frontend:** Electron, com React e TypeScript (Vite) e TanStack Query; tipos gerados do OpenAPI da API.
*   **Engenharia:** Clean Architecture em quatro camadas, princípios SOLID, TDD, Ruff, Black e `mypy --strict`.

---

## Como funciona

1.  **Varredura:** identificação dos arquivos de imagem nos diretórios selecionados.
2.  **Extração:** o modelo multimodal converte cada imagem em um vetor (**embedding**).
3.  **Persistência:** vetores e metadados (caminho, data, posição) vão para o banco vetorial.
4.  **Recuperação:** a consulta textual vira um vetor e é comparada aos das imagens, gerando um ranking de relevância.

---

## Como instalar

O SolidVision se instala com um arquivo só, `SolidVision-Setup-0.1.0.exe`, que se gera com `npm run dist` ([abaixo](#7-o-instalador)). Ele traz tudo o que o app usa — o Python, o PostgreSQL e os modelos de IA — e não precisa de rede, de Docker nem de um administrador ([RFC-041](docs/rfcs/rfc-041-empacotamento-e-instalador.md)). O instalador tem 1,03 GB, e o programa instalado ocupa 2,2 GB. Requer Windows 10 ou 11, 64 bits.

1. **Dois cliques no instalador.** Ele não é assinado, e o Windows avisa. Na tela azul *"O Windows protegeu o computador"*, clique em *"Mais informações"*; na mesma tela aparece o botão *"Executar assim mesmo"*. Nenhuma senha de administrador é pedida.
2. **O app abre sozinho ao terminar.** A primeira abertura prepara o banco de dados e leva cerca de um minuto; as seguintes, alguns segundos. Os atalhos ficam no Menu Iniciar e na área de trabalho.

**Onde as coisas ficam**, as duas na conta de quem instalou, e mais nenhuma pasta:

| O quê | Onde |
|---|---|
| O programa | `%LOCALAPPDATA%\Programs\SolidVision` |
| Os dados: o banco com o acervo indexado, as miniaturas, os logs, os caches e o perfil da janela | `%LOCALAPPDATA%\SolidVision` |

**Desinstalar** é por *Configurações > Aplicativos > SolidVision*, com o app aberto ou fechado: ele fecha o app, para a indexação em andamento e remove o programa e os atalhos. **Os dados ficam**, inteiros: instalar de novo reaproveita o acervo, sem reindexar — e indexar um disco leva horas. Para apagar os dados, feche o app e apague a pasta `%LOCALAPPDATA%\SolidVision`.

O mapa mostra os tiles do OpenStreetMap quando há rede, com a atribuição na tela e um User-Agent que identifica o app; sem rede, o contorno dos estados, que vai no instalador. Contato sobre o uso dos tiles: as [issues do projeto](https://github.com/SanthiagoBR/SolidVision/issues).

---

## Como rodar

Requer Python 3.12 e Windows. Os comandos abaixo partem da raiz do repositório, no PowerShell.

### 1. Ambiente Python
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
Copy-Item .env.example .env
```

### 2. Modelos de IA (uma vez por cópia do repositório)
Os modelos ficam em `runtime/models`, fora do git, e são conferidos por `sha256`:
```powershell
python scripts/vendor_models.py               # baixa do Hugging Face
python scripts/vendor_models.py --from-cache  # ou usa o cache local do Hugging Face
```

### 3. Banco de dados

**Opção A - Docker:**
```powershell
docker compose up -d
cd backend; python -m alembic upgrade head; cd ..
```
Conexão: host `localhost`, porta `5432`, banco, usuário e senha `solidvision`.

**Opção B - PostgreSQL embarcado, sem Docker:**
```powershell
python scripts/vendor_postgres.py    # uma vez: baixa os binários para runtime/postgres
$env:PYTHONPATH = "backend"
python -m app.infrastructure.database.embedded start
```
No `.env`, defina `DATABASE_SOURCE=embedded`. A primeira subida cria o cluster e aplica as migrations. Detalhes em [backend/README.md](backend/README.md).

### 4. Usar
```powershell
$env:PYTHONPATH = "backend"
python -m app.cli index --root "D:\Fotos" --label "HD2"      # indexa uma pasta
python -m app.cli search "casa com piscina" --limit 10       # busca por texto
python -m app.cli search-image foto.jpg                      # busca por imagem
python -m uvicorn app.presentation.api:app                   # API REST
```
Documentação interativa da API em `http://localhost:8000/docs`.

### 5. Subir tudo com o launcher
Um comando sobe o banco embarcado, aplica as migrations, sobe a API e o executor de jobs, supervisiona os três e para tudo em ordem ([RFC-036](docs/rfcs/rfc-036-launcher-e-supervisao-de-processos.md)):
```powershell
$env:PYTHONPATH = "backend"
python -m app.launcher            # até o Ctrl+C
python -m app.launcher status     # o que está de pé, e em que endereço
python -m app.launcher stop       # para o launcher de pé, de outro terminal
```
A API atende só em `127.0.0.1`, numa porta que o launcher escolhe e informa no `status`. Sem Docker, use `DATABASE_SOURCE=embedded` no `.env`.

### 6. A janela
A interface é um app Electron que sobe o launcher, mostra as fases da subida e abre a tela inicial quando a API está pronta ([RFC-037](docs/rfcs/rfc-037-fundacao-e-casca-do-frontend.md)). A própria API serve a interface, na mesma origem. Requer o **Node 24 LTS** (`frontend/.node-version`); o Node 20 não roda o Vite, o Vitest e o React Router atuais. A primeira abertura baixa o binário do Electron (cerca de 370 MB), uma vez por máquina.
```powershell
cd frontend
npm install
npm run build      # a interface em dist/, que a API serve, e a casca em dist-electron/
npm start          # a janela: sobe o launcher e abre a tela inicial
```
Sem Docker, use `DATABASE_SOURCE=embedded` no `.env`. Fechar a janela para o launcher e tudo o que ele subiu; abrir de novo com a janela aberta só a traz para a frente. A janela registra a própria subida e o que o launcher escreve em `%LOCALAPPDATA%\SolidVision-dev\logs\shell.log`.

Para trabalhar na interface, `npm run dev` abre a janela sobre o Vite, com recarga ao salvar, e sobe um launcher na porta 8000, para onde o Vite encaminha `/api`. `npm run api:types` regenera os tipos depois de uma mudança na API.

A janela guarda o perfil do Chromium em `%LOCALAPPDATA%\SolidVision-dev\shell`, ao lado dos logs. Um perfil de antes do RFC-041, em `%APPDATA%\SolidVision-dev`, não é mais lido e pode ser apagado.

### 7. O instalador
O instalador leva o Python embutido, com as dependências de `scripts/python-win-64.lock`, ao lado dos outros dois runtimes ([RFC-041](docs/rfcs/rfc-041-empacotamento-e-instalador.md)). Os três se materializam uma vez por cópia do repositório, com o Python do ambiente virtual:
```powershell
python scripts/vendor_postgres.py
python scripts/vendor_models.py
python scripts/vendor_python.py     # o Python 3.12.9 embeddable e as rodas do lockfile, em runtime/python
cd frontend
npm run dist        # o build, a montagem, as conferências e o NSIS: release\SolidVision-Setup-0.1.0.exe
npm run dist:dir    # o mesmo até a pasta release\win-unpacked, sem o NSIS
```
A montagem fica em `%LOCALAPPDATA%\SolidVision-build\resources`, e as conferências do `scripts/check_package.py` param o build se o pacote não for o que a suíte testou: os vetores do CLIP calculados pelo Python do pacote, bit a bit contra os de referência, as DLLs de cada binário, o orçamento de caminho e o que vai no `app.asar`. O primeiro build precisa de rede, para o electron-builder baixar o NSIS. Mudar uma dependência do Python é mudar o ambiente virtual e o lockfile juntos: `python scripts/vendor_python.py --lock` recusa uma versão diferente da do ambiente virtual.

### Verificações de qualidade
```powershell
ruff check backend
black --check backend
mypy backend
pytest
cd frontend; npm test; npm run lint; npm run typecheck; npm run format:check
```
O `pytest` confere que o `frontend/src/api/openapi.json` é o que a API produz, e o `npm test` confere que os tipos são os que esse arquivo gera: quem muda uma rota roda `npm run api:types`.
Com `WARM_UP_MODELS=true` no `.env` (o padrão do `.env.example`), um teste que confere que resolver o modelo não baixa nada falha. Rode com `WARM_UP_MODELS=false` para a suíte inteira passar.

---

## Métricas de avaliação

*   **Recall@5:** 84,0% medido sobre o dataset de demonstração (45 imagens, 25 consultas de referência), RFC-025.
*   **Latência de busca e tempo de indexação:** medidos em cada RFC, com a escala e as condições da medição.

---

## Autor
**Santhiago Chapiewski** - [santhiago.chapiewski@catolicasc.edu.br](mailto:santhiago.chapiewski@catolicasc.edu.br)  
*Centro Universitário Católica de Santa Catarina*

---

## Licença
Este projeto está sob a licença **CC0 1.0 Universal** (domínio público). Veja o arquivo [LICENSE](LICENSE).
