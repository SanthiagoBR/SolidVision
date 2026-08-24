# RFC-008 — Health API

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-004 (Settings), RFC-005 (Logging), RFC-006 (Session)
**Bloqueia:** RFC-016 (DI), RFC-026 (Search API)
**Commit:** `25fc543`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Ao fim da Sprint 1 existiam configuração, logging, engine e Alembic — mas nenhuma prova de que essas peças funcionavam **juntas** em um processo real. Um endpoint de health é a menor coisa possível que exercita a pilha inteira: FastAPI sobe, resolve uma dependência de sessão, executa SQL contra o PostgreSQL do Compose, e responde.

## 2. Decisão

Um endpoint `GET /health` que **verifica o banco de verdade** (`SELECT 1`) e responde `503` quando ele está inacessível.

Um health check que retorna `{"status": "ok"}` sem tocar em nada só prova que o processo Python está vivo — informação que quem fez a requisição já tinha.

## 3. O que foi entregue

```python
@router.get("/health", status_code=status.HTTP_200_OK, response_model=None)
def health_check(db: Session = Depends(get_db)) -> dict[str, Any] | JSONResponse:
    logger.info("Health check requested")
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Database health check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "database": "disconnected"},
        )
    logger.info("Health check succeeded")
    return {
        "status": "healthy",
        "database": "connected",
        "version": settings.project_version,
        "environment": settings.environment,
    }
```

Também entregues: `presentation/api/__init__.py` (a instância `app`, com `title` e `version` vindos do `Settings`) e `backend/main.py` (entrypoint que apenas reexporta `app`).

### 3.1 Respostas

**200 — saudável**
```json
{
  "status": "healthy",
  "database": "connected",
  "version": "0.1.0",
  "environment": "development"
}
```

**503 — banco inacessível**
```json
{ "status": "unhealthy", "database": "disconnected" }
```

## 4. Notas de projeto

**Captura `SQLAlchemyError`, não `Exception`.** A distinção é o ponto do tratamento: falha de conexão é a condição que este endpoint existe para relatar, e vira `503`. Qualquer outra exceção é um bug da aplicação, e deve subir como `500` — engolir tudo transformaria um `AttributeError` em "banco desconectado", que é uma mensagem falsa apontando para o lugar errado.

**503, não 500.** Semanticamente correto: o serviço está temporariamente indisponível por causa de uma dependência, não quebrado. É também o código que orquestradores (Kubernetes, Docker healthcheck) interpretam como "não envie tráfego ainda".

**`response_model=None`.** A função tem dois tipos de retorno — `dict` e `JSONResponse`. Sem essa anotação, o FastAPI tentaria inferir um modelo de resposta a partir do tipo de retorno e falharia sobre a união.

**Expõe versão e ambiente.** Torna o endpoint útil para diagnóstico ("qual versão está rodando aí?") sem exigir um segundo endpoint. Nenhum dos dois campos é sensível.

**`def`, não `async def`.** O FastAPI executa handlers síncronos em um threadpool, o que impede que o driver bloqueante do PostgreSQL trave o event loop. Mesma decisão que o RFC-026 §9 tomou para a busca — lá com uma consequência de concorrência analisada em detalhe.

## 5. Onde a rota mora — e a bifurcação que sobrou

Este RFC criou `presentation/routes/health.py`. O scaffold do RFC-001, porém, já havia criado `presentation/api/v1/routers/`. Ficaram duas árvores de rotas no repositório.

O RFC-026 resolveu a questão: a busca foi para `api/v1/routers/search.py` (com prefixo de versão), e `/health` **permanece** em `routes/`, sem versão. A justificativa está registrada no RFC-026 §6.1: health é um endpoint de infraestrutura, consumido por orquestradores, e versioná-lo (`/api/v1/health`) quebraria configurações externas por nenhum ganho — o contrato dele não evolui junto com a API de domínio.

## 6. Testes

`tests/presentation/test_health.py`

| Teste | Verifica |
|---|---|
| 200 com banco disponível | caminho feliz, contra PostgreSQL real |
| Corpo contém `status`, `database`, `version`, `environment` | contrato de resposta |
| 503 quando a sessão levanta `SQLAlchemyError` | via `app.dependency_overrides[get_db]` |
| `version`/`environment` refletem o `Settings` | configuração de fato chega à resposta |

A sobrescrita de dependência (`dependency_overrides`) é o mecanismo que torna o caso de falha testável sem derrubar o container — e é o mesmo mecanismo que o RFC-026 usa em escala para os testes da rota de busca.

## 7. Limitações

- Não verifica o modelo de embedding, o sistema de arquivos, nem espaço em disco.
- Não distingue *liveness* de *readiness*.
- Não retorna latência da checagem.
