# RFC-003 — Ambiente Python e Ferramental de Qualidade

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-001
**Bloqueia:** todos os RFCs de código
**Commit:** `fb27cbc`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Um projeto de faculdade avaliado por terceiros precisa que `pytest` funcione na máquina do avaliador sem instruções orais. Além disso, boa parte das regras que este projeto declara — "o Domain não importa SQLAlchemy", "todo código tem type hints" — só vale alguma coisa se **uma ferramenta as verificar**. Regra não verificada é comentário.

## 2. Decisão

Centralizar toda a configuração de ferramental em um único `pyproject.toml`, exigir **Python 3.12+**, e rodar `mypy` em modo `strict` sobre todo o código de aplicação e de teste.

## 3. O que foi entregue

### 3.1 Python 3.12+

```toml
[project]
requires-python = ">=3.12"
```

Escolhido pela sintaxe de tipos moderna que o código usa de ponta a ponta: `int | None` em vez de `Optional[int]`, `list[float]` em vez de `List[float]`, `type[X]` genérico. `datetime.UTC` (usado em `FilesystemImageProvider`) também exige 3.11+.

### 3.2 Linter: Ruff

```toml
[tool.ruff]
line-length = 88
target-version = "py312"
fix = true

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP"]

[tool.ruff.lint.isort]
known-first-party = ["app", "dataset_tools"]
```

| Código | O que cobre |
|---|---|
| `E` | Estilo (pycodestyle) |
| `F` | Erros reais (pyflakes): import não usado, variável não definida |
| `I` | Ordenação de imports (isort) |
| `N` | Convenções de nomes (PEP 8) |
| `UP` | pyupgrade — obriga a sintaxe moderna a permanecer moderna |

`known-first-party` faz o Ruff separar visualmente imports do projeto dos de terceiros — o que torna uma violação de camada (`from sqlalchemy import ...` dentro de `domain/`) visível na própria diff.

`alembic/` está excluído: os arquivos de migration são gerados por template e não devem ser reformatados a cada geração.

### 3.3 Type checker: mypy strict

```toml
[tool.mypy]
python_version = "3.12"
strict = true
disallow_untyped_defs = true
disallow_any_generics = true
warn_unreachable = true
files = ["backend/app", "backend/tests", "backend/dataset_tools"]
```

`strict = true` é a decisão de maior impacto deste RFC. Consequência prática: **toda função precisa de assinatura tipada, incluindo as de teste**. O custo é escrever `-> None` centenas de vezes; o retorno é que erros de contrato entre camadas aparecem antes de executar qualquer coisa — passar `ImageId` onde se espera `UUID`, esquecer de tratar um `| None`, comparar tipos incompatíveis.

Uma única exceção, escopada:

```toml
[[tool.mypy.overrides]]
module = ["langdetect.*"]
ignore_missing_imports = true
```

`langdetect` (usado pelo RFC-023 para detecção de idioma) não publica informação de tipos. `torch` e `transformers` publicam `py.typed` e são checados normalmente. A exceção é limitada a um módulo em vez de global, para que o resto do código continue estrito.

### 3.4 Testes: pytest

```toml
[tool.pytest.ini_options]
testpaths = ["backend/tests"]
addopts = "-ra --strict-markers -m 'not slow'"
pythonpath = [".", "backend"]
markers = [
    "slow: requires downloading and running real AI models",
]
```

Três decisões embutidas aqui:

**`pythonpath = [".", "backend"]`** — é o que torna `import app.domain...` válido sem instalar o projeto como pacote. Note que é `backend/` que entra no path, **não** `backend/app/`; por isso o entrypoint do worker é `python -m app.infrastructure.workers.indexing_worker`, com o prefixo `app.`.

**`--strict-markers`** — um marcador digitado errado (`@pytest.mark.slwo`) vira erro em vez de ser silenciosamente ignorado. Sem isso, um teste marcado errado passaria a rodar quando não deveria.

**`-m 'not slow'`** — um `pytest` puro nunca baixa checkpoints do Hugging Face. Os testes que carregam CLIP de verdade são opt-in (`pytest -m slow`). Isso mantém a suíte padrão offline, rápida e determinística — propriedade da qual o RFC-023 e o RFC-026 dependem explicitamente.

### 3.5 Formatter: Black

`line-length = 88`, `target-version = ["py312"]`, mesmas exclusões do Ruff.

## 4. Notas de projeto

**Um `pyproject.toml` na raiz, não um por pasta.** Ruff, Black, mypy e pytest leem o mesmo arquivo, com os mesmos `exclude`. Configurações duplicadas divergem; esta não pode.

**`requirements.txt` continua existindo** ao lado do `pyproject.toml`. O `pyproject` aqui é configuração de ferramental, não gerenciamento de dependências — o projeto não é instalado como pacote (`pip install -e .`), é executado a partir do `pythonpath`.

## 5. Comandos

```bash
python -m venv backend/.venv
backend/.venv/Scripts/activate     # Windows
pip install -r requirements.txt

ruff check .          # lint (com --fix automático)
black .               # formatação
mypy                  # checagem de tipos (lê files= do pyproject)
pytest                # suíte rápida, sem modelos reais
pytest -m slow        # apenas os testes que carregam CLIP de verdade
pytest -m ""          # tudo
```

## 6. Limitações

- Não há lock file (`poetry.lock`, `requirements.lock`); versões não são fixadas de forma reprodutível bit a bit.
- Não há pipeline de CI configurada; as ferramentas são executadas manualmente.
