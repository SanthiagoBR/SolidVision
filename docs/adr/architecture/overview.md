# SolidVision — Status do Projeto

**Última atualização:** RFC-017c aplicado (tabela relacional `images` ativa no PostgreSQL) · RFC-018 (embedding + HNSW) pendente de execução · Sprint 3

> Este documento descreve o estado atual, verificado de fato, da implementação. Para decisões arquiteturais normativas e justificativas, ver `ARCHITECTURE.md`. Para convenções operacionais destinadas a assistentes de IA, ver `AI_CONTEXT.md`. Este documento deve ser atualizado sempre que um RFC concluído alterar a estrutura do projeto ou o schema persistido — as afirmações de status aqui devem ser verificadas contra o banco de dados/código real em execução, não presumidas a partir da intenção do RFC.

---

## 1. Visão Geral

SolidVision é um sistema para indexação e recuperação de fotografias aéreas.

O projeto foi projetado para permitir que fotografias armazenadas em um sistema de arquivos sejam indexadas e posteriormente recuperadas através de busca semântica. O sistema está sendo desenvolvido com uma arquitetura em camadas que separa regras de negócio, orquestração de aplicação, preocupações de infraestrutura e interfaces externas.

O projeto está atualmente em desenvolvimento ativo. Alguns componentes já estão implementados, enquanto outros existem como placeholders para desenvolvimento futuro.

---

## 2. Organização Arquitetural

O backend está organizado em quatro camadas principais:

```text
app/
├── domain/
├── application/
├── infrastructure/
└── presentation/
```

Cada camada tem uma responsabilidade diferente.

### Domain

A camada `domain` contém os conceitos e regras centrais do SolidVision.

Ela é independente de tecnologias externas como SQLAlchemy, PostgreSQL, FastAPI ou implementações de sistema de arquivos.

Os componentes de domínio atuais incluem:

* Entidades
* Value Objects
* Interfaces de repositório
* Portas de serviço (service ports)
* Exceções de domínio

A entidade principal atualmente implementada é `Image`.

---

### Application

A camada `application` contém os casos de uso da aplicação.

Ela coordena objetos e abstrações de domínio para realizar operações no nível da aplicação, sem conter implementações específicas de infraestrutura.

Os casos de uso atuais incluem:

* `IndexImage`
* `SearchImages`

Estão localizados em:

```text
app/application/use_cases/
```

---

### Infrastructure

A camada `infrastructure` contém implementações e integrações com tecnologias externas.

As áreas atuais incluem:

```text
infrastructure/
├── ai/
├── config/
├── database/
├── filesystem/
├── logging/
├── persistence/
└── workers/
```

Exemplos de responsabilidades de infraestrutura já representadas no projeto incluem:

* Implementações de modelo de IA/embedding (fake, para desenvolvimento/testes)
* Configuração de banco de dados e infraestrutura SQLAlchemy
* Persistência em memória
* Configuração da aplicação
* Logging
* Worker de indexação/processamento em segundo plano (fundação apenas — ainda não funcional)

A camada de infraestrutura pode depender do Domain, mas o Domain não pode depender da Infraestrutura.

---

### Presentation

A camada `presentation` expõe a aplicação a clientes externos.

O projeto atualmente contém componentes de API baseados em FastAPI, incluindo:

```text
presentation/
├── api.py
├── dependencies/
├── routes/
├── api/v1/routers/
└── schemas/
```

Uma rota de health check já está presente e funcional (`/health`).

Os routers versionados de API estão organizados em `presentation/api/v1/routers/`, mas atualmente contêm apenas placeholders (ex.: `collections.py`) — nenhum endpoint funcional além do `/health` existe ainda.

---

## 3. Modelo de Domínio

O Domain atual contém os seguintes conceitos principais.

### Image

`Image` representa uma imagem conhecida pelo sistema.

Sua estrutura atual é:

```text
Image
├── id: ImageId
├── path: ImagePath
├── filename: str
└── extension: str
```

A entidade de Domain não contém campos específicos de banco de dados, como colunas do SQLAlchemy.

Ela também não contém o vetor de embedding persistido. O armazenamento do embedding pertence à camada de persistência da Infraestrutura.

---

### Value Objects

O Domain atualmente contém:

```text
value_objects/
├── embedding_vector.py
├── image_id.py
└── image_path.py
```

Esses objetos representam valores específicos do domínio e fornecem tipagem mais forte e encapsulamento do que o uso de valores primitivos em todo o Domain.

`EmbeddingVector` está implementado e testado, mas ainda não é utilizado por nenhum caminho de código em produção — ele aguarda o futuro pipeline de indexação (planejado em um RFC posterior).

---

### Interfaces de Repositório

O Domain define abstrações de repositório em `domain/repositories/`.

A interface de repositório atual é `image_repository.py`.

A interface pertence ao Domain para que a lógica de aplicação possa depender de uma abstração em vez de uma tecnologia de persistência específica.

A Infraestrutura fornece as implementações dessas abstrações. Atualmente, existe apenas uma implementação em memória (`InMemoryImageRepository`) — nenhuma implementação de repositório baseada em PostgreSQL existe ainda.

---

## 4. Arquitetura de Persistência

O projeto utiliza SQLAlchemy para persistência relacional.

A infraestrutura de persistência atual está organizada como:

```text
infrastructure/
├── database/
│   └── models/
│       └── image_model.py
│
└── persistence/
    ├── base.py
    ├── engine.py
    ├── session.py
    └── in_memory_image_repository.py
```

`ImageModel` é a representação SQLAlchemy da entidade de Domain `Image`.

O modelo ORM é intencionalmente separado da entidade de Domain.

Conceitualmente:

```text
Domain

Image
  │
  │ conversão
  ▼
Infrastructure

ImageModel
  │
  ▼
PostgreSQL
```

Essa separação evita que preocupações específicas de banco de dados se tornem parte do modelo de Domain.

**Estado atual verificado:** a tabela `images` existe no banco de dados PostgreSQL de desenvolvimento com as colunas `id`, `path`, `filename`, `extension` (chave primária em `id`, restrição de unicidade em `path`), aplicada através da migração inicial do Alembic. Ela ainda não contém uma coluna `embedding` — o armazenamento vetorial está definido no nível do `ImageModel`/migração como trabalho planejado, ainda não aplicado ao banco de dados em execução.

---

## 5. Arquitetura de Embeddings

O SolidVision utiliza embeddings como parte de sua funcionalidade planejada de recuperação semântica de imagens.

O Domain define a abstração para um modelo de embedding através de `domain/services/embedding_model_port.py`.

Uma implementação de infraestrutura já existe: `infrastructure/ai/fake_embedding_model.py` — uma implementação determinística, sem uso de IA real, utilizada para desenvolvimento e testes. Nenhum modelo de embedding real (ex.: SigLIP) foi integrado ainda.

O Domain também contém `domain/value_objects/embedding_vector.py`.

A representação de persistência dos embeddings está planejada para ser tratada separadamente pela infraestrutura de banco de dados. O design do banco de dados tem como alvo o PostgreSQL com pgvector, reservando uma coluna vetorial para embeddings de imagem na dimensão `1152` — essa coluna ainda não existe no banco de dados em execução no momento desta redação; sua criação é a próxima migração planejada.

Essa dimensão faz parte do schema do banco de dados e, portanto, não pode ser alterada com segurança apenas por meio de configuração da aplicação (ver `ARCHITECTURE.md`, ADR-007 quando formalizado).

---

## 6. Fluxo da Aplicação

Em alto nível, a arquitetura pretende seguir esta direção:

```text
Cliente Externo
      │
      ▼
Presentation
      │
      ▼
Application
      │
      ▼
Domain
      │
      ▼
Infrastructure
```

As dependências devem seguir os limites arquiteturais, em vez de permitir que preocupações de infraestrutura vazem para o Domain.

Por exemplo, uma implementação de banco de dados pode depender da interface de repositório do Domain:

```text
Domain
  │
  │ define
  ▼
ImageRepository
  ▲
  │ implementa
Infrastructure
```

O Domain, portanto, define o que a persistência deve fornecer, enquanto a Infraestrutura determina como a persistência é efetivamente realizada.

---

## 7. Estrutura Atual do Projeto

A estrutura relevante do backend atualmente é:

```text
app/
├── application/
│   └── use_cases/
│       ├── index_image.py
│       └── search_images.py
│
├── domain/
│   ├── entities/
│   │   ├── collection.py          # placeholder, sem RFC ainda
│   │   └── image.py
│   ├── exceptions/
│   │   ├── domain_error.py
│   │   └── image_errors.py
│   ├── repositories/
│   │   └── image_repository.py
│   ├── services/
│   │   └── embedding_model_port.py
│   └── value_objects/
│       ├── embedding_vector.py
│       ├── image_id.py
│       └── image_path.py
│
├── infrastructure/
│   ├── ai/
│   │   └── fake_embedding_model.py
│   ├── config/
│   │   ├── constants.py
│   │   └── settings.py
│   ├── database/
│   │   └── models/
│   │       └── image_model.py
│   ├── filesystem/                # vazio, placeholder
│   ├── logging/
│   ├── persistence/
│   │   ├── base.py
│   │   ├── engine.py
│   │   ├── in_memory_image_repository.py
│   │   └── session.py
│   └── workers/
│       └── indexing_worker.py     # placeholder, não funcional
│
└── presentation/
    ├── api.py                     # shim → api/__init__.py (funcional)
    ├── dependencies/               # funcional (RFC-016)
    ├── routes/
    │   └── health.py               # funcional
    ├── api/
    │   └── v1/
    │       └── routers/
    │           └── collections.py  # placeholder, sem RFC ainda
    └── schemas/
        ├── collection_schema.py    # placeholder, sem RFC ainda
        └── image_schema.py         # placeholder, sem RFC ainda
```

Os componentes marcados como placeholders existem como scaffolding vindo da estrutura inicial do projeto (RFC-001) e não são considerados código morto — eles aguardam funcionalidade planejada em RFCs futuros.

---

## 8. Princípios Arquiteturais

A arquitetura atual segue diversos princípios importantes.

### Separação de responsabilidades

Cada camada tem uma responsabilidade específica.

Regras de negócio pertencem ao Domain, orquestração de aplicação pertence ao Application, integrações externas pertencem à Infrastructure, e interfaces externas pertencem à Presentation.

### Inversão de dependência

O Domain define abstrações como interfaces de repositório e portas de serviço.

Implementações concretas são fornecidas pela Infraestrutura.

### Independência do Domain

O Domain não deve importar tecnologias de Infraestrutura.

Por exemplo, o Domain não deve depender diretamente de:

* SQLAlchemy
* PostgreSQL
* pgvector
* FastAPI
* implementações de sistema de arquivos

### Separação entre Domain e persistência

A entidade de Domain e o modelo ORM são representações diferentes do mesmo dado conceitual.

`Image` representa o conceito de negócio.

`ImageModel` representa sua representação de persistência.

---

## 9. Status Atual de Desenvolvimento (verificado)

O projeto está sendo implementado de forma incremental através de RFCs técnicos, rastreados individualmente em `docs/rfcs/` (ou equivalente).

**Confirmado como aplicado e funcional, na data acima:**

* Entidades de Domain e Value Objects (`Image`, `ImageId`, `ImagePath`, `EmbeddingVector`)
* Casos de uso de Application (`IndexImage`, `SearchImages`), testados com fakes
* Abstrações de repositório e serviço (`ImageRepository`, `EmbeddingModelPort`)
* Implementação de repositório em memória, conectada via Injeção de Dependência (RFC-016)
* `FakeEmbeddingModel` — determinístico, sem uso de IA real
* Fundação de persistência SQLAlchemy (`Base`, engine, session)
* `ImageModel` do SQLAlchemy (apenas campos relacionais, ainda sem coluna de embedding)
* Banco de dados PostgreSQL em execução com a extensão `vector` instalada
* Alembic configurado; migração inicial aplicada, criando a tabela `images`
* Endpoint de API `/health`, funcional
* Infraestrutura de logging

**Planejado, ainda não aplicado ao sistema em execução:**

* Coluna `embedding VECTOR(1152)` e índice HNSW (RFC-018, próximo a ser executado)
* Implementação de repositório baseada em SQLAlchemy (PostgreSQL)
* Integração de modelo de embedding real (ex.: SigLIP)
* Busca por similaridade vetorial
* Endpoints funcionais de `/search` e `/index`
* Worker de indexação (atualmente apenas placeholder)
* Entidade de Domain `Collection` e persistência relacionada

Este documento deve ser reverificado contra o banco de dados e o código em execução sempre que um novo RFC for concluído, em vez de atualizado apenas com base na intenção do RFC.
