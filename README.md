# SolidVision
> **Plataforma Inteligente de Recuperação de Imagens para Acervos Fotográficos Baseada em Embeddings**

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Framework-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![License](https://img.shields.io/badge/License-CC0_1.0-lightgrey.svg)](LICENSE)

O **SolidVision** é uma plataforma de busca semântica desenvolvida para resolver o desafio de localizar imagens em grandes volumes de dados não estruturados. Com **Inteligência Artificial Multimodal**, o sistema permite que fotógrafos profissionais recuperem fotografias por descrições em linguagem natural, sem depender de metadados manuais ou de nomes de arquivos.

---

## Estado atual

O backend está implementado até o **RFC-035**. Cada mudança do projeto é descrita em um RFC numerado, escrito antes da implementação e atualizado com as medições depois dela.

| Etapa | RFCs | Situação |
|---|---|---|
| Fundação, domínio, persistência, indexação | 001-021 | Pronto |
| IA e busca semântica (CLIP, pgvector, API de busca) | 022-026 | Pronto |
| Acervo real: dispositivos, datas, jobs, arquivos, geolocalização | 027-032 | Pronto |
| Sem Docker: PostgreSQL embarcado, configuração do usuário, modelos offline | 033-035 | Pronto |
| Launcher e supervisão de processos | 036 | Pendente |
| Frontend | - | Não iniciado |

Documentação: [índice dos RFCs](docs/rfcs/README.md), [roadmap sem Docker](docs/rfcs/ROADMAP-standalone.md) e a [arquitetura](ARCHITECTURE.md).

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
*   **Engenharia:** Clean Architecture em quatro camadas, princípios SOLID, TDD, Ruff, Black e `mypy --strict`.

---

## Como funciona

1.  **Varredura:** identificação dos arquivos de imagem nos diretórios selecionados.
2.  **Extração:** o modelo multimodal converte cada imagem em um vetor (**embedding**).
3.  **Persistência:** vetores e metadados (caminho, data, posição) vão para o banco vetorial.
4.  **Recuperação:** a consulta textual vira um vetor e é comparada aos das imagens, gerando um ranking de relevância.

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

### Verificações de qualidade
```powershell
ruff check backend
black --check backend
mypy backend
pytest
```
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
