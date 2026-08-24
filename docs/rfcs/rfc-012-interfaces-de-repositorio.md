# RFC-012 — Interfaces de Repositório (duplicata)

**Status:** ❌ Rejeitado — duplicava o [RFC-010](rfc-010-porta-de-repositorio.md); revertido pelo [RFC-012b](rfc-012b-consolidacao-do-dominio.md)
**Sprint:** 2
**Substituído por:** RFC-012b (`8a2d710`)
**Commit próprio:** nenhum — o trabalho foi desfeito antes de ser commitado
**Última atualização:** 2026-08-24

---

## 1. O que aconteceu

O roadmap listava "010 Value Objects" e "012 Repository Interfaces" como itens distintos. Mas:

- os value objects já haviam sido entregues no **RFC-009** (`Image` não existe sem `ImageId` e `ImagePath`);
- o RFC-010, ao encontrar seu escopo já feito, entregou a **porta de repositório** — que era, literalmente, o escopo do RFC-012.

Executado a partir do roadmap em vez do estado real do repositório, este RFC criou uma segunda declaração de `ImageRepository`. O projeto ficou, por um momento, com duas abstrações de repositório concorrentes, e nada dizia qual delas os casos de uso do RFC-015 deveriam importar.

Esta é a **causa raiz** do problema que o roadmap registra como "009 a 013".

## 2. Por que a duplicata é pior que ela parece

Duas ABCs com o mesmo nome e métodos parecidos não geram erro de sintaxe, nem de tipo, nem de import. O código compila, os testes passam, e o dano só aparece depois:

- `isinstance(repo, ImageRepository)` retorna `False` se as duas classes vierem de módulos diferentes — mesmo com implementação idêntica;
- um teste sobrescreve a dependência com um fake que implementa a *outra* porta, e a sobrescrita silenciosamente não vale;
- ao acrescentar um método ao contrato (o que os RFCs 021, 024 e 025 fizeram), há 50% de chance de editar a cópia sem consumidores — e a mudança "não faz efeito", sem nenhuma mensagem de erro.

É uma classe de bug que não falha alto: falha em silêncio, tarde, e longe da edição que a causou.

## 3. Resolução

O RFC-012b removeu a duplicata e fixou **uma única** porta canônica:

```
backend/app/domain/repositories/image_repository.py
```

O caminho escolhido é o que `AI_Context.md` já documentava como convenção (*Repository Interfaces → `domain/repositories/`*) e o que o RFC-010 havia de fato commitado. Nada em `domain/ports/` foi mantido — essa pasta não existe na árvore atual.

## 4. Lição registrada

**O roadmap descreve a intenção; o repositório descreve o fato.** Quando os dois divergem, o repositório vence — e o roadmap é corrigido, não a árvore.

A prática adotada a partir daqui: antes de iniciar um RFC, verificar o que já existe (`ls`, `git log --name-status`) em vez de assumir que o item anterior entregou exatamente o que seu título dizia. Os RFCs 022–026 registram, cada um, uma seção sobre o que encontraram no código antes de começar — hábito que nasceu deste erro.
