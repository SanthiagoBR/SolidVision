# RFC-011 — Exceções de Domínio

**Status:** ⚠️ Superseded — absorvido pelo [RFC-012b](rfc-012b-consolidacao-do-dominio.md)
**Sprint:** 2
**Depende de:** RFC-009
**Substituído por:** RFC-012b (`8a2d710`)
**Commit próprio:** nenhum — o trabalho foi entregue dentro do commit do RFC-012b
**Última atualização:** 2026-08-24

---

## 1. Intenção original

Dar à camada Domain uma hierarquia de exceções própria, com uma raiz comum, para que:

- nenhuma exceção de domínio fosse `ValueError` ou `Exception` genérica;
- uma camada superior pudesse capturar `DomainError` e traduzir **toda** a família para uma resposta HTTP com um único handler (o que o RFC-026 §8 de fato faz);
- cada falha tivesse um nome que descreve o conceito violado, não o mecanismo.

## 2. O conflito

O RFC-009 já havia criado `backend/app/domain/exceptions.py` — um **módulo**, arquivo único, com as exceções de que `ImageId` e `ImagePath` precisavam para validar no construtor.

Este RFC tentou criar `backend/app/domain/exceptions/` — um **pacote**, diretório com `__init__.py`.

Em Python, um módulo `exceptions.py` e um pacote `exceptions/` não podem coexistir no mesmo diretório-pai: ambos ocupam o nome `app.domain.exceptions`. Qual dos dois é resolvido depende da ordem de varredura do importador, e o `__pycache__` do arquivo antigo continua presente mesmo depois de o `.py` ser removido, produzindo um estado em que o import funciona em uma máquina e falha em outra.

O sintoma foi exatamente esse: imports intermitentes de `InvalidImageIdentifierError`, dependendo de cache.

## 3. Resolução

A conversão foi feita como **substituição atômica**, dentro do commit do RFC-012b:

- `git rm backend/app/domain/exceptions.py`
- criação de `backend/app/domain/exceptions/` com `domain_error.py`, `image_errors.py` e um `__init__.py` que reexporta tudo;
- limpeza dos `__pycache__` residuais.

O resultado final está documentado no [RFC-012b](rfc-012b-consolidacao-do-dominio.md), que é a referência para a hierarquia de exceções.

## 4. Lição registrada

Converter um módulo em pacote com o mesmo nome **não é uma mudança aditiva**. Ela exige remover o arquivo antigo no mesmo commit; qualquer estado intermediário é ambíguo para o importador do Python.

O padrão mais amplo, que se repetiu no RFC-012: quando dois RFCs planejados independentemente tocam o mesmo nome, o conflito não aparece na revisão do plano — aparece no import. A mitigação adotada a partir daqui foi verificar a árvore real de arquivos antes de começar, em vez de confiar apenas no roadmap.
