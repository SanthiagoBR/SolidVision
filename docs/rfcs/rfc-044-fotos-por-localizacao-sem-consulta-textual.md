# RFC-044 — Fotos por Localização, sem Consulta Textual

**Status:** Proposto. Nada aqui foi medido ou implementado.
**Depende de:** RFC-025 (`SearchHit`, cuja `similarity` obrigatória é o motivo de este RFC não reaproveitá-lo), RFC-027 (filtro por dispositivo), RFC-028 (filtro por data, e o porquê de uma data desconhecida nunca "combinar"), RFC-032 (o círculo de "perto de", e §11 item 1, que já pediu exatamente este RFC), RFC-038 (a paginação "Mostrar mais" e a grade de resultados), RFC-040 (o mapa: hoje um clique sem busca leva à tela inicial vazia)
**Bloqueia:** nada
**Migration:** nenhuma esperada na tabela `images`; uma possível migration de índice, se ordenar por `captured_at` sem filtro de igualdade se mostrar lenta sem um B-tree (`TBM`, §5 — pergunta diferente da que o RFC-028 §8.1 já respondeu para *filtrar* por data)
**Medição:** nenhuma ainda

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Este RFC não implementa nada — decide a forma, para que a implementação, quando vier, meça as perguntas certas.

---

## 1. Contexto

Caso de uso real, descrito pelo usuário: um cliente do profissional que usa o SolidVision pede para fotografar uma construção até ela ficar pronta — meses de fotos do mesmo lugar. Hoje, a única forma de ver "tudo que existe perto deste lugar" é buscar um texto e filtrar por "Perto de" (RFC-032, RFC-038 §5.2): a busca por *"construção"* é ranqueada por semelhança com essa palavra, e um prédio terminado — uma fachada pronta, não mais um canteiro de obras — pode não ranquear bem o suficiente para aparecer, mesmo estando exatamente no círculo certo. A ferramenta que existe responde "o que parece com X aqui", e a pergunta real é "o que existe aqui, no tempo".

O RFC-032 §11 já tinha nomeado isto como o primeiro item de trabalho futuro, antes de o mapa existir:

> *"Listar fotos de uma célula do mapa, sem consulta textual. É o primeiro clique que a UI de mapa vai querer e este RFC não entrega. Precisa decidir ordenação (data de captura, presumivelmente) e paginação — RFC próprio."*

O mapa (RFC-040) chegou e confirmou a lacuna pelo outro lado: hoje, "Buscar perto daqui" sem nenhuma busca ativa leva à tela inicial com o círculo pronto e a barra com foco — esperando um texto que, neste caso de uso, não existe para digitar.

## 2. Problema

Duas perguntas de produto, que nenhuma linha de código responde:

1. **A ordem.** Cronológica, mas em que sentido — mais recente primeiro (como toda galeria de fotos abre) ou mais antiga primeiro (a narrativa de "do buraco no chão ao prédio pronto")?
2. **A foto sem data.** RFC-020 e RFC-028 já decidiram que uma data desconhecida nunca "combina" com um filtro de intervalo. Numa *ordem* cronológica a mesma foto não tem onde entrar — mas "nunca aparece" e "nunca combina" não são a mesma garantia, e este RFC precisa escolher uma.

E uma restrição de desenho que o código já impõe, por decisão deliberada do RFC-025: `SearchHit.similarity` é um `float` obrigatório, documentado assim —

> *"Uma similaridade não significa nada sem a busca que a produziu (...) e inventar uma pontuação para ela [a devolveria] como se tivesse sido comparada."*

Isto não é um detalhe de tipagem: é a razão pela qual **esta listagem não pode ser "a busca de sempre, com a palavra vazia"**. Precisa do seu próprio contrato, sem um número que finge ser uma pontuação.

## 3. Decisões propostas (a validar antes de implementar)

### 3.1 Onde isto aparece

Em todo lugar que já oferece "Perto de" sem busca por texto — a tela inicial com um círculo (de uma foto ou de uma célula do mapa) e o balão de uma célula —, ao lado da pergunta que já existe, nunca no lugar dela. Buscar "construção" perto dali continua possível; a ação nova é *"Ver todas as fotos daqui, por data"*.

### 3.2 Ordenação: decrescente por padrão, com a opção de inverter

**Recomendação, a confirmar com quem usa o acervo real:** mais recente primeiro. É a convenção de toda galeria — inclusive a grade atual do SolidVision, cuja ordem é a da resposta, sem inversão (RFC-038) — e responde primeiro a "como está agora". A narrativa inversa ("do início ao fim") fica como um botão de inverter a ordem na mesma lista, não como um modo separado: é a mesma consulta, só troca `ASC`/`DESC`. `TBM` se a primeira versão já entrega os dois sentidos ou só o padrão.

### 3.3 A foto sem data: fora da ordem, nunca escondida em silêncio

Proposta: uma foto sem `captured_at` não entra na lista ordenada — não há posição correta para ela numa linha do tempo — e é contada, não descartada, no mesmo padrão de `excluded_unknown_date` (RFC-028 §4.1) e `excluded_unknown_position` (RFC-032 §6.2): um número visível, nunca um resultado que parece completo e não é.

### 3.4 Um contrato novo, não `SearchHit` com um campo a menos

- **Repositório:** um método novo — por exemplo `list_by_filters(filters, limit, order) -> list[Image]` — sem embedding e sem ranking, reaproveitando a tradução de `SearchFilters` para `WHERE` que `search_similar()` e `aggregate_positions()` já têm (dispositivo, intervalo de data, círculo). O trabalho novo real é pequeno: troca a cláusula `ORDER BY` por `captured_at`, não o resto da consulta.
- **Caso de uso:** ao lado de `SearchImagesUseCase`, reaproveitando `validated_request()` (tamanho de página, piso do raio) e as duas funções de contagem (`count_hidden_by_unknown_date`, `count_hidden_by_unknown_position`), que já são genéricas — não pertencem à busca semântica, pertencem a `SearchFilters`.
- **Rota:** nova (`TBM` o nome — candidato: `GET /images/browse`), com um schema próprio, sem o campo `similarity`. Não estende `GET /images/search` com um `q` opcional: a rota literal antes de `/{image_id}` já é uma regra deste projeto (RFC-038 §4), e misturar os dois contratos sob a mesma rota reintroduziria exatamente o número fingido que o RFC-025 recusou.
- **Frontend:** um quarto tipo de busca, ao lado de `text`, `similar` e `image` (`search/params.ts`), acionado quando há um `near` sem texto. O cartão da grade mostra a data de captura no canto onde hoje mostra o cosseno — não um valor a menos, um valor diferente e mais útil aqui.

## 4. Critérios de pronto, se este RFC for retomado

Nenhum tem número ainda:

- `TBM`: uma área com fotos de datas diferentes volta na ordem certa, com "Mostrar mais" se comportando como na busca por texto (mesma política de página).
- `TBM`: fotos sem data aparecem no aviso novo, nunca somem sem explicação.
- **Critério qualitativo, sem medição:** funciona com a busca `loading` ou `unavailable` no `/health` — esta listagem não depende de CLIP, o que a busca por texto não cumpre hoje.
- `TBM`: tempo até a lista aparecer, numa área com N fotos — deve ser mais barato que a busca por texto equivalente, por não ter inferência nem travessia de índice vetorial.

## 5. Riscos

- **O teto de 100 (`MAX_SEARCH_LIMIT`) já existe para toda busca**, e o caso de uso descrito — meses de fotos do mesmo lugar — é exatamente o que mais rápido o alcança. A política atual ("Mostrar mais" repete com um `limit` maior) pode não bastar aqui antes de bastar em qualquer busca por texto. Paginação real, por `offset` ou cursor, fica `TBM` até alguém medir quantas fotos uma obra típica gera.
- **Ordenar por `captured_at` sem filtro de igualdade é uma pergunta diferente da que o RFC-028 §8.1 já respondeu.** Aquele RFC mediu um B-tree para *filtrar* por intervalo e não se justificou; um `ORDER BY` sem esse filtro pode usar um índice para evitar um *sort* em disco de um jeito que um `WHERE` não usava. Fica como medição própria, não herdada.
- **Listar sem nenhum filtro restritivo é outra pergunta.** Esta listagem só faz sentido com pelo menos um filtro de verdade — dispositivo, data ou, no caso de uso, um círculo. O acervo inteiro ordenado por data, sem filtro nenhum, está mais perto de uma "galeria" do que de uma busca, e fica fora deste RFC a menos que o produto peça por ela explicitamente.

## 6. Fora deste RFC

- Paginação por `offset`/cursor (§5 — só se a medição mostrar necessidade).
- Uma "galeria" sem filtro nenhum (§5).
- Mudar a ordem da busca por texto, que continua pela similaridade.
- Agrupar fotos por evento ou por dia (um "zoom" temporal sobre a mesma obra) — um RFC próprio, se for pedido depois de esta versão simples existir.

## 7. O que isto muda no roadmap

Nada, por ora. Fica proposto e parado, como o [RFC-043](rfc-043-busca-por-endereco-no-mapa.md), até alguém confirmar a ordenação padrão (§3.2) e, idealmente, medir quantas fotos uma obra real acumula na mesma área (§5). Não está no [roadmap do frontend](ROADMAP-frontend.md) e não compete com a Etapa 4 (RFC-041, o instalador, ainda por fechar até 31/10).
