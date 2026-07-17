# SolidVision 📷 🚀
> **Plataforma Inteligente de Recuperação de Imagens para Acervos Fotográficos Baseada em Embeddings**

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Framework-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-Container-2496ED.svg)](https://www.docker.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![License](https://img.shields.io/badge/License-CC_BY_4.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)

O **SolidVision** é uma plataforma de busca semântica desenvolvida para resolver o desafio de localizar imagens em grandes volumes de dados não estruturados [1, 2]. Utilizando **Inteligência Artificial Multimodal**, o sistema permite que fotógrafos profissionais recuperem fotografias através de descrições em linguagem natural, eliminando a dependência de metadados manuais ou nomes de arquivos [3-5].

---

## ✨ Funcionalidades Principais

*   🔍 **Busca Semântica:** Pesquise por contexto visual (ex: *"propriedade rural com lago"* ou *"casa com piscina"*) em vez de palavras-chave literais [4, 6].
*   📂 **Indexação Local:** Varredura automática de diretórios locais; os arquivos permanecem em seus locais originais sem necessidade de upload [5, 7].
*   🧠 **Zero-Shot Learning:** Capacidade de identificar conceitos visuais complexos mesmo sem etiquetas prévias, graças ao modelo CLIP [8, 9].
*   ⚡ **Alta Performance:** Recuperação rápida baseada em **similaridade de cosseno** diretamente no banco de dados [10].
*   ✈️ **Domínio Flexível:** Validado com acervos de **fotografia aérea por drones**, mas adaptável a qualquer nicho fotográfico [4, 11].

---

## 🛠️ Tecnologias e Padrões de Engenharia

O projeto destaca-se pelo rigor técnico e sustentabilidade arquitetural [12, 13]:

*   **IA:** Modelos multimodais pré-treinados **CLIP** e **SigLIP** baseados em Transformers [8, 14].
*   **Banco de Dados:** **PostgreSQL** com a extensão **pgvector** para persistência e busca vetorial [15, 16].
*   **Backend:** Desenvolvido em **Python** com o framework **FastAPI** para orquestração de APIs REST [16].
*   **Infraestrutura:** Containerização completa utilizando **Docker** [16].
*   **Qualidade de Software:** Aplicação de princípios **SOLID**, **TDD** (Test-Driven Development) e pipelines de **CI/CD** [12, 13].

---

## 🐍 Ambiente de Desenvolvimento Python

Este projeto requer Python 3.12 para desenvolvimento local.

### Criar e ativar um ambiente virtual
No PowerShell do Windows:
```bash
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

No Linux/macOS:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

### Instalar dependências
```bash
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
```

### Executar verificações de qualidade
```bash
ruff check backend
black --check backend
mypy backend
pytest
```

### Configuração de ambiente
Os valores de configuração são carregados a partir de um arquivo `.env` por meio da camada de settings em `backend/app/infrastructure/config/settings.py`.

### Logging
A infraestrutura de logging centraliza a criação de loggers em `backend/app/infrastructure/logging/`. Os módulos devem usar `get_logger(__name__)` para obter um logger configurado sem criar handlers manualmente.

### Formatar código
```bash
black backend
```

## ⚙️ Funcionamento do Pipeline

O sistema processa a informação em quatro fases fundamentais [7, 10, 14, 15]:

1.  **Varredura:** Identificação de arquivos de imagem em diretórios selecionados.
2.  **Extração:** O modelo multimodal converte imagens em vetores matemáticos (**embeddings**) de alta dimensão.
3.  **Persistência:** Armazenamento dos vetores e metadados (caminho, data) no banco vetorial.
4.  **Recuperação:** Consultas textuais são convertidas em vetores e comparadas aos das imagens para gerar um ranking de relevância.

---

## 📊 Métricas de Avaliação

O projeto monitora a eficiência da solução através de métricas como [17]:
*   **mAP (mean Average Precision):** Para validar a relevância dos resultados.
*   **Latência de Busca:** Tempo decorrido entre a consulta e a resposta.
*   **Tempo de Indexação:** Performance no processamento inicial do acervo.

---

## 👤 Autor
**Santhiago Chapiewski** – [santhiago.chapiewski@catolicasc.edu.br](mailto:santhiago.chapiewski@catolicasc.edu.br)  
*Centro Universitário Católica de Santa Catarina* [1, 18].

---

## � Infraestrutura Docker

### Pré-requisitos
- Docker Desktop ou Docker Engine instalado e em execução.
- Docker Compose disponível no ambiente.

### Iniciar o banco PostgreSQL com pgvector
```bash
docker compose up -d
```

### Parar o banco PostgreSQL
```bash
docker compose down
```

### Reconstruir os containers
```bash
docker compose up -d --build
```

### Conectar ao PostgreSQL
Use as credenciais padrão abaixo:
- Host: `postgres`
- Porta: `5432`
- Banco: `solidvision`
- Usuário: `solidvision`
- Senha: `solidvision`

Você também pode acessar o container com:
```bash
docker compose exec postgres psql -U solidvision -d solidvision
```

### Scripts de inicialização
Os scripts de inicialização do PostgreSQL ficam em:
```text
docker/postgres/init/
```
Atualmente, o script `init-pgvector.sql` cria a extensão `vector` automaticamente.

### Verificar se o pgvector está instalado
```bash
docker compose exec postgres psql -U solidvision -d solidvision -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Você também pode verificar o status do container e do healthcheck com:
```bash
docker compose ps
```

## �📄 Licença
Este projeto está licenciado sob a **Creative Commons Attribution 4.0 International License** [1, 18].