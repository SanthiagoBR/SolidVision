# RFC-001 — Estrutura do Projeto

**Status:** Implementado
**Sprint:** 1
**Depende de:** —
**Bloqueia:** todos os RFCs seguintes
**Commit:** `9c58be8` — *folder structure + architecture.md*
**Última atualização:** 2026-08-24

---

## 1. Contexto

O repositório existia apenas com `LICENSE` e um `README.md` de duas linhas. Antes de escrever qualquer código de negócio era preciso decidir **onde cada tipo de código mora**, porque essa decisão é a mais cara de reverter: mover um módulo depois que dez arquivos o importam custa muito mais do que colocá-lo no lugar certo na primeira vez.

## 2. Decisão

Adotar **Clean Architecture** com quatro camadas e uma regra de dependência unidirecional:

```
Presentation  →  Application  →  Domain
                                   ↑
                            Infrastructure
```

Infrastructure **implementa** contratos declarados por Domain e Application; nunca o contrário. O Domain não conhece FastAPI, SQLAlchemy, Torch ou PostgreSQL.

A estrutura foi criada **vazia e antecipadamente** (arquivos `__init__.py` com uma linha), em vez de crescer organicamente. O objetivo é que a pasta correta já exista quando o código chegar — assim não há a tentação de colocar um adaptador de banco dentro de `application/` "só por enquanto".

## 3. O que foi entregue

```
backend/app/
├── presentation/          # FastAPI: rotas, schemas, injeção de dependências
│   ├── api/v1/routers/
│   ├── schemas/
│   └── dependencies/
├── application/           # Casos de uso, orquestração
│   └── use_cases/
├── domain/                # Regras de negócio puras
│   ├── entities/
│   ├── value_objects/
│   ├── repositories/
│   └── services/
└── infrastructure/        # Adaptadores concretos
    ├── ai/
    ├── config/
    ├── filesystem/
    ├── persistence/
    └── workers/
backend/tests/             # Espelha a estrutura de produção
docs/                      # Documentação (ADRs, RFCs)
```

Também entregues neste RFC:

| Arquivo | Papel |
|---|---|
| `ARCHITECTURE.md` | Documento arquitetural de referência (~1.200 linhas): camadas, pipeline de indexação, estratégia de busca vetorial, metas de desempenho, ADR-001 a ADR-007. |
| `AI_Context.md` | Instruções operacionais para assistentes de IA: convenções de nomes, regras de camada, o que nunca fazer. |
| `.gitignore`, `.env.example`, `requirements.txt` | Higiene básica do repositório. |

## 4. Notas de projeto

**Uma entidade por arquivo, um value object por arquivo, um caso de uso público por arquivo.** Convenção registrada em `AI_Context.md`. O custo é ter muitos arquivos pequenos; o benefício é que o nome do arquivo é sempre uma resposta à pergunta "onde está X?".

**`tests/` espelha `app/`.** `app/domain/entities/image.py` → `tests/domain/test_image.py`. Isso torna a cobertura visualmente auditável: uma pasta de produção sem pasta de teste correspondente é um buraco visível.

**Placeholders que envelheceram mal.** O scaffold criou `application/ports/embedding_model_port.py` e `presentation/schemas/image_schema.py` antes de existir a decisão sobre onde as portas deveriam morar. O RFC-013 decidiu que portas de serviço pertencem ao **Domain**, e o RFC-015 apagou o placeholder órfão. Lição registrada: criar pastas antecipadamente é barato; criar *arquivos* antecipadamente cria fatos arquiteturais que ainda não foram decididos.

## 5. Testes

Nenhum. Este RFC não entregou comportamento executável. As invariantes que ele estabelece passaram a ser testadas depois, por testes de arquitetura:

- `tests/application/test_application_architecture.py` — a camada Application não importa SQLAlchemy nem `settings`.
- `tests/infrastructure/test_sqlalchemy_models_architecture.py` — o Domain não importa SQLAlchemy.
- `tests/test_ai_layer_boundaries.py` — os routers não importam Torch/Transformers.

## 6. Limitações

`backend/app/domain/entities/collection.py` e `presentation/api/v1/routers/collections.py` continuam sendo placeholders vazios. Coleções são um conceito descrito em `ARCHITECTURE.md` §11 e §15, mas ainda não implementado — a busca atual é global (ver RFC-025 §8).
