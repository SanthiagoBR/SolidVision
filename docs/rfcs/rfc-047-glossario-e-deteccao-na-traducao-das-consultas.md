# RFC-047 — Glossário e Detecção de Idioma na Tradução das Consultas

**Status:** Implementado em duas rodadas. A primeira (§15, §16) fez o léxico e provou cada troca sozinha e em "{forma} vista de cima"; a validação mostrou o que essa prova não via (trocas que acrescentam palavras, como "estrada de terra batida" → *dirt road hit*), e a segunda (§17) refez o léxico contra frases e o mediu de novo. Com o léxico final, **419 formas, 125 com troca**, a medição de §9, na validação do RFC-042, passa nos cinco critérios: as 178 consultas em inglês ficam com o mesmo prompt; das 31 consultas em português cujo prompt mudou, **10 sobem e 1 desce**; o recall@1 em português vai de **46,6% a 49,4%** (+2,8 [+0,6; +5,6]) e o recall@5 de **78,1% a 79,2%** (+1,1 [−1,1; +3,4]); as 30 palavras soltas do piloto chegam certas ao CLIP; o léxico e os marcadores custam **0,04 a 0,15 ms** por consulta. Nos termos do sul, o recall@1 vai de 30,4% a 43,5% e o @5 de 65,2% a 78,3% — um teto, por §4.4. **Os números da segunda rodada não são limpos**: ela foi feita depois de ver a validação (§17.1), e a comparação com a primeira está à parte, em §17.5. Fica de fora um risco medido e não corrigido (§17.6): os cinco empréstimos do inglês ("lighthouse", "kayak", "kiosk", "canyon", "ferry") foram provados em sete contextos, e só neles. As seções 1 a 14 são o rascunho como foi aprovado; o que a implementação mudou nelas está em §16 e §17
**Depende de:** RFC-023 (§7, o tradutor e a detecção de idioma; §22, a errata do `>>por<<`), RFC-035 (o tradutor é a entrada `translator` do lockfile, e §7.2 guarda os prompts de referência), RFC-042 (§14, a medição que achou a perda; o conjunto de avaliação congelado e `measure_baseline.py`, que medem este RFC)
**Bloqueia:** nada. Vem antes do nível A do RFC-042: o adaptador do nível A ajusta o vetor da consulta que sai do tradutor, e convém que esse tradutor já seja o definitivo quando ele for treinado
**Migration:** não. Só a consulta muda; os vetores das imagens, o banco e o índice ficam como estão
**Medição:** o piloto de §2, `experiments/rfc-047-traducao-das-consultas/pilot.py` (+ `.json`; o `.log`, fora do git), rodado em 2026-11-03 nesta máquina (NVMe, Windows 11), em CPU. A da implementação, em 2026-11-03 e 04, na mesma máquina, em CPU: a sonda de §4.3, `probe_lexicon.py` (+ `.json`), e §9 por `measure_baseline.py` do RFC-042, antes e depois, lido por `measure.py` (+ `.json`); na segunda rodada, `probe_frames.py` (+ `.json`) com as frases e as regras de `backend/tests/infrastructure/ai/lexicon_frames.py`, `candidates-rodada2.json` (o que foi tentado) e `query_lexicon-rodada1.toml` (o léxico da primeira rodada, de onde saem os números de §15); os `.log` e os `.json` do `measure_baseline.py`, fora do git (§15, §17)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*. Todos viraram número, ao lado da frase e em §15. Os números sem marca vêm do piloto de §2 ou de outros RFCs, e dizem de qual.

---

## 1. Contexto

O CLIP do produto só entende inglês. O RFC-023 resolveu o português com dois passos antes do codificador de texto: o `langdetect` decide se a consulta é português, e o Marian (`Helsinki-NLP/opus-mt-ROMANCE-en`) traduz o que for ([§7](rfc-023-adaptador-de-embedding-clip.md#7-pipeline-de-texto)). O próprio RFC-023 registrou os limites: palavras soltas não são detectadas como português (§7.1), e a falta de acento piora a tradução (§22, "chácara com um galpão à esquerda" → *a log with a shed on the left*).

O RFC-042 mediu o quanto isso custa, com o conjunto de avaliação de 525 fotos e consultas escritas à mão ([§14](rfc-042-fine-tuning-do-clip.md#14-medição-da-porta-de-entrada-31)). Na validação (178 consultas), com o modelo atual:

| comparação, mesmas fotos | recall@1 | recall@5 | consultas |
| --- | --- | --- | --- |
| português traduzido contra inglês | −8,4 [−13,5; −3,9] | −5,6 [−10,1; −0,6] | 178 |
| termos do sul contra português padrão | −17,4 [−34,8; −4,3] | −30,4 [−47,8; −13,0] | 23 |

O tradutor custa mais do que qualquer troca de checkpoint ganhou ali, e mexer nele não reindexa nada. É o que este RFC faz, na versão mais simples: uma lista de termos que corrige as palavras que o Marian erra, e uma regra de detecção que não deixa português passar sem tradução.

## 2. O que o piloto mediu

`pilot.py`, rodado em 2026-11-03, usa o `MarianQueryTranslator` do produto e o `langdetect` com a semente do produto. Lê só as consultas de **validação** do RFC-042; o teste não é aberto.

### 2.1 Frases: quatro consultas em português passam como espanhol

| consultas | detectado como | traduzidas hoje |
| --- | --- | --- |
| 178 em português | `pt` 174, `es` 4 | 174 |
| 178 em inglês | `en` 177, `ro` 1 | 0 (a do `ro` não é traduzida, por não ser `pt`) |
| 23 com termos do sul | `pt` 23 | 23 |

As quatro tomadas por espanhol chegam ao CLIP em português: "campos verdes divididos por cercas vivas vistos do helicóptero", "floresta cortada por estradas de terra", "lava incandescente escorrendo de perto" e "capela pequena de pedra do lado de um estacionamento". A em inglês tomada por romeno é "colorful crop fields". O mesmo defeito aparece nos prompts de referência do RFC-035: "rua residencial ladeada por casas" vai ao CLIP como *a photo of rua residencial ladeada por casas*.

### 2.2 Palavras soltas: a detecção não funciona

Trinta palavras comuns em fotos aéreas, escritas para o piloto nas duas línguas, par a par ("fazenda"/*farm*, "ilha"/*island*, "estádio"/*stadium*…):

- **Português:** só 8 das 30 são detectadas como `pt`. As outras saem como `es`, `it`, `tr`, `tl`, `lt`, `sl`, `cy`, `so`…, e hoje não são traduzidas.
- **Inglês:** só 6 das 30 são detectadas como `en`.
- **O Marian também erra palavras soltas**, mesmo quando a detecção acerta: "morro" → *Death* (detectado `pt`, então isso já acontece no produto), "estádio" → *stage*, "represa" → *recovery*, "plantação" → *planting*.

O RFC-023 previa a falha da detecção nas palavras soltas e registrou o teste `test_single_word_portuguese_is_a_known_detection_gap` para que corrigi-la fosse uma mudança visível. É o que §6 propõe.

### 2.3 Inglês pelo Marian: estraga

As 178 consultas em inglês, passadas pelo Marian como se fossem português: **41 mudam**, muitas para pior. "ruins" vira *bad*, "lit" vira *bed*, "neighborhood" vira *nextborhood*, "tall buildings on a riverbank" vira *this is a riverbank building*. Nas palavras soltas, das 24 em inglês que a detecção não reconhece, o Marian estraga 6: *island* → *Iceland*, *stadium* → *stage*, *sea* → *or*, *shed* → *check*.

Isso descarta a regra "traduzir tudo o que não for detectado como inglês": nas palavras soltas, ela mandaria ao Marian 24 de 30 consultas em inglês. Também descarta pôr o termo em inglês no meio do português antes de traduzir (§10): o Marian não preserva inglês.

### 2.4 A troca antes da tradução: funciona, termo a termo

Os termos que o RFC-023 e o RFC-042 já tinham visto falhar, em frases escritas para o piloto, antes e depois de trocar o termo por um mais simples:

| frase | Marian | com a troca | Marian |
| --- | --- | --- | --- |
| casarão amarelo com jardim | *will marry yellow with garden* | casa grande amarela com jardim | *large yellow house with garden* |
| rio com corredeiras e pedras | *river with slides and stones* | rio com águas agitadas e pedras | *river with agitated waters and rocks* |
| trapiche de madeira no lago | *wooden trapiche in the lake* | píer de madeira no lago | *wood pier in the lake* |
| lagoa de água barrenta | *barren water pond* | lagoa de água marrom | *brown water pond* |
| chácara com um galpão à esquerda | *a log with a shed on the left* | sítio com um galpão à esquerda | *a place with a shed on the left* |
| chácara com piscina | *cabin with pool* | pequena fazenda com piscina | *small farm with pool* |

A troca resolve, mas **cada uma precisa ser conferida**. "Sítio" parecia o sinônimo óbvio de "chácara" e virou *a place*; "pequena fazenda" acertou. "Mato" depende da frase: "casa no meio do mato" sai *bush*, certo, e no RFC-042 "mato no costão" saiu *I kill*.

O Marian levou de 0,26 a 0,29 s por consulta nesta máquina, em CPU, em duas rodadas do piloto.

## 3. Decisões

1. **Um léxico versionado com o código** (§4): formas em português de coisas que aparecem em fotos aéreas, cada uma com o inglês que a tradução deve produzir. É a lista de onde saem as duas correções seguintes.
2. **Troca antes da tradução** (§5): quando o Marian erra um termo do léxico, o termo é trocado, antes de traduzir, por outro em português que ele traduz certo. A troca é conferida por teste com o checkpoint real.
3. **Detecção com marcadores** (§6): a consulta é traduzida quando o `langdetect` diz `pt`, como hoje, **ou** quando tem um marcador de português, isto é, uma palavra funcional de uma lista fechada ou uma forma do léxico.
4. **Nada além da consulta muda** (§7): nem o checkpoint, nem o template, nem os vetores das imagens, nem o lockfile. Não há chave de configuração nova.

## 4. O léxico

### 4.1 O que é

Um arquivo de dados ao lado do tradutor, `backend/app/infrastructure/ai/query_lexicon.toml`. Cada entrada é **uma forma, como se digita**, sem regra de plural ou de flexão: o plural que importa é outra linha. Isso deixa o casamento determinístico e cada linha auditável.

```toml
[[forma]]
pt = "casarão"
en = ["mansion", "large house", "big house"]   # a tradução deve conter um destes
troca = "casa grande"                          # só quando o Marian erra a forma
origem = "geral"                               # "geral" ou "sul"
```

- `en` diz o que conta como tradução certa. O teste lento de §12 confere cada linha.
- `troca` só existe quando o Marian erra a forma, e o teste também confere que ele erra: uma troca desnecessária é ruído.
- Uma forma que existe nas duas línguas e quer dizer o mesmo ("hotel") não entra como marcador de §6. O campo `tambem_ingles = true` a exclui da detecção.

Ele mora no código, e não em `Settings`, pela razão do template e do `DRAFT_TARGET_EDGE` do `ClipEmbeddingModel`: muda o vetor de algumas consultas, e isso é decisão de RFC, não preferência. Ao contrário deles, não muda nenhum vetor guardado. O vetor da consulta é calculado a cada busca (`SearchImagesUseCase` chama `encode_text()` e não guarda nada), então mexer no léxico não reindexa nada.

### 4.2 De onde vem

O léxico é escrito **antes** de medir o efeito no conjunto de avaliação, a partir de três fontes:

1. **As 30 palavras do piloto** (§2.2) e as consultas em português dos prompts de referência do RFC-035.
2. **Vocabulário de cena aérea, por tema:** água (rio, lago, represa, açude, cachoeira, praia, porto…), campo (fazenda, plantação, pasto, galpão, silo…), relevo e vegetação (morro, serra, vale, mata, mato…), cidade e construção (prédio, estádio, igreja, viaduto, estacionamento…). São os temas das categorias que o coletor do RFC-042 usou.
3. **Termos do sul do Brasil:** coxilha, potreiro, sanga, arroio, banhado, capão, macega, cerro, várzea, trapiche, molhes, costão, cômoros, estância, chácara, lavoura, taipa, cancha, barranca, estrada de chão…

Cada forma entra também sem acento quando o usuário costuma digitá-la assim ("galpao", "predio", "chacara"): o piloto mostra que "predios" sem acento vira *grounds*, e "galpao", *galpao*. O casamento é insensível a acento, e a forma que vai ao Marian é a acentuada (§5).

Tamanho: algumas centenas de formas (`TBM`). **Medido: 410 formas**, 373 gerais e 37 do sul, das quais 18 também são inglês (15 palavras e 3 nomes de lugar). O primeiro rascunho tinha 419; a sonda tirou 17 e a implementação acrescentou 8 (§16.3, §16.7, §16.8). A segunda rodada tirou 9 e acrescentou 18, e o léxico final tem **419 formas**: 382 gerais e 37 do sul, 125 com troca, 18 também inglês (15 palavras e 3 nomes de lugar) (§17.3).

### 4.3 A sonda

`experiments/rfc-047-traducao-das-consultas/probe_lexicon.py` passa cada forma pelo Marian, sozinha e numa frase neutra ("{forma} vista de cima"), e marca as que não produzem nenhum dos `en`. Para cada uma, a troca é escolhida e conferida do mesmo jeito. O `.json` da sonda registra, por forma, a tradução antes e depois. Quantas formas falham e quantas a troca resolve: `TBM`. **Medido:** no primeiro rascunho, 142 de 419 falhavam; no léxico da primeira rodada, **113 de 410 falham, e a troca resolve as 113** (§15.1) — sozinha e em "{forma} vista de cima". Em frases, a mesma sonda, com o critério final de §17.2, reprova **77 das 407 formas** desse léxico (50 trocas e 27 formas sem troca): a segunda rodada as refez, e o léxico final passa as 416 formas (sem os 3 nomes) nos sete contextos (§17.3).

### 4.4 O viés de quem escreveu

Quem escreve o léxico (o Claude) também escreveu os rascunhos das 525 consultas de avaliação e as 66 variantes do sul, e já viu os erros de §2.4. O vocabulário regional do léxico é, por construção, o mesmo das variantes do sul. Por isso:

- o ganho na validação é **otimista**, e no subconjunto do sul é um teto, e não uma estimativa;
- o número sem esse viés vem do teste do RFC-042, aberto uma vez só, na avaliação final daquele RFC, que já vai incluir este tradutor;
- nenhuma forma entra no léxico por ter falhado numa consulta de avaliação. A sonda roda sobre o léxico, e não sobre as consultas.

## 5. A troca

Roda dentro de `MarianQueryTranslator.to_english()`, **depois** de decidido que a consulta é português e **antes** do Marian. Uma consulta que não vai ser traduzida não passa pelo léxico.

1. **Casamento:** palavra inteira, sem diferenciar maiúsculas nem acentos ("Chacara" casa com "chácara"; "mato" não casa dentro de "matogrossense").
2. **Formas de várias palavras primeiro**, e o casamento mais longo vence: "estrada de chão" é trocada inteira, antes de "chão" sozinho.
3. **O que vai ao Marian:** a `troca`, quando existe; senão, a forma acentuada do léxico. "galpao" vai como "galpão".
4. **O resto do texto fica intocado**, inclusive a pontuação e o que não está no léxico.
5. **Determinístico:** mesma consulta, mesmo texto ao Marian, mesmo vetor, como o RFC-023 §6.2 exige.

A troca não refaz a concordância: "casarão amarelo" vai como "casa grande amarelo". Se isso estraga a tradução é `TBM`. **Medido: não estraga.** "casa grande amarelo" vira *large yellow house*. Das 113 trocas, nenhuma perde o `en` com o adjetivo no gênero do termo digitado; 4 o perdem em um dos gêneros ou nos dois pela palavra que o Marian escolhe junto do adjetivo (*yellow grazing* para "pastagem amarela"), e não pela concordância (§15.1). A sonda testa cada forma também com um adjetivo depois, e a troca é escolhida para resistir a isso.

## 6. A detecção

Uma consulta é traduzida quando:

1. o `langdetect` diz `pt` (a regra de hoje); **ou**
2. a consulta tem um **marcador de português**: uma palavra funcional da lista fechada abaixo, ou uma forma do léxico que não seja `tambem_ingles`.

A lista de palavras funcionais é fechada e exclui as que também são palavras em inglês ("a", "as", "do", "no", "on" ficam de fora):

> de, da, das, dos, com, um, uma, uns, umas, em, num, numa, na, nas, ao, aos, à, às, pelo, pela, pelos, pelas, por, para, entre, perto, sobre, sem, e, ou, nao, não

Contada sobre os textos, sem traduzir nada:

- das 178 consultas em português da validação, **177 têm palavra funcional**, as quatro de §2.1 entre elas ("por", "de"). A única sem ("plantações coloridas") o `langdetect` já reconhece;
- das 178 em inglês, **nenhuma tem**, nem "colorful crop fields". O mesmo vale para os prompts de referência do RFC-035: as 25 consultas em inglês não têm marcador, e as 25 em português têm todas, inclusive "rua residencial ladeada por casas";
- nas palavras soltas, as palavras funcionais não ajudam, e quem decide é o léxico. "fazenda", "lago", "piscina" e "morro" passam a ser traduzidas se forem formas dele, e as inglesas do piloto não, desde que nenhuma seja forma do léxico. Isso o teste de §12 confere.

A regra 2 não depende do `langdetect` acertar a família: palavras soltas saem como turco, tagalo ou lituano (§2.2), e o marcador decide mesmo assim. O `langdetect` continua: é ele que reconhece frases inteiras em português sem marcador nem termo do léxico.

## 7. O que não muda

- O checkpoint CLIP, o template `a photo of {query}` e o pré-processamento.
- O checkpoint do tradutor, a decodificação gulosa e o lockfile do RFC-035. Nenhum modelo é baixado.
- Os vetores das imagens, o banco e o índice HNSW. Não há migration nem reindexação.
- O caminho do inglês: uma consulta detectada como inglês e sem marcador chega ao CLIP exatamente como hoje.
- `Settings`: nenhuma chave nova.

## 8. O que isto muda em documentos e testes anteriores

- **RFC-023 §7.1.** A tabela de palavras soltas deixa de valer para as formas do léxico. `test_single_word_portuguese_is_a_known_detection_gap` passa a afirmar o contrário para elas ("fazenda" é traduzida), e continua afirmando a falha para uma palavra fora do léxico. É a "mudança visível e deliberada" que o RFC-023 pediu.
- **RFC-035 §7.2.** `test_reference_embeddings_slow` compara os prompts e os vetores de 50 consultas gravadas antes do RFC-035, 25 delas em português sem acento. Este RFC muda de propósito os prompts de algumas ("rua residencial ladeada por casas", as que têm "galpao" e "predios"; quantas, `TBM`). **Medido: o texto que vai ao Marian muda em 14 das 25, e o prompt em 9** — 8 para melhor e 1 para pior (§15.3). A gravação é a prova de que o carregamento por diretório deu o mesmo vetor que o carregamento por nome, e **não é regravada**. O teste passa a separar as duas coisas: o vetor de cada *prompt gravado* continua comparado bit a bit, o que prova o codificador, e o prompt de cada consulta é comparado com o gravado só quando este RFC não o toca. Quando toca, é comparado com o que o léxico manda.
- **RFC-042 §14.** A linha de base do nível A passa a ser a medida depois deste RFC.

## 9. Medição e critério de pronto

`measure_baseline.py`, do RFC-042, com o checkpoint atual (`--only`), na **validação**, antes (os números de §1) e depois. O critério é fixado aqui, antes de medir:

1. **Nenhuma consulta em inglês da validação muda de prompt** (178 de 178 iguais).
2. **Entre as consultas em português cujo prompt mudou, mais melhoram de posição do que pioram.** As que não mudaram têm o mesmo prompt por construção, e isso é conferido consulta a consulta.
3. **O recall@1 e o recall@5 em português não caem.** O intervalo do bootstrap é registrado, mas não se exige que fique acima de zero: o número de consultas tocadas é pequeno (`TBM`; **medido: 38 de 178** na primeira rodada e **31** na segunda, §17.5), e com 178 consultas o ruído é de ±6 pontos (RFC-042 §14).
4. **As 30 palavras soltas do piloto:** as em português que estão no léxico chegam ao CLIP com o `en` esperado (`TBM` quantas; **medido: as 30 estão no léxico, e as 30 chegam certas**; antes, 8 eram traduzidas), e nenhuma das 30 em inglês muda de prompt.
5. **Tempo:** o léxico e os marcadores custam menos de 1 ms por consulta (`TBM`; **medido: 0,12 a 0,15 ms de mediana e 0,32 a 0,36 ms no p95, em três rodadas da primeira; 0,04 ms e 0,07 ms na segunda, com a máquina livre**). Cada consulta que passa a ser traduzida custa o Marian, de 0,26 a 0,29 s nesta máquina (§2.4).

Medidos também, sem critério: o subconjunto do sul (`sul` e `pt (sul)`), lembrando §4.4; e a perda que sobra entre português e inglês, que diz quanto resta para um tradutor melhor.

O teste do RFC-042 continua fechado.

## 10. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Trocar o modelo de tradução | Download, entrada no lockfile, peso no instalador, licença e tempo por consulta. É o próximo passo se este não bastar, e §9 mede quanto sobra para ele. |
| Corrigir o inglês depois (trocar *they will marry* por *mansion*) | O erro muda com a frase: "casarão" deu *will marry* e *Old married* no piloto, e *they will marry* no RFC-042. Corrigir a saída é perseguir variações; corrigir a entrada é uma linha por termo. |
| Pôr o termo em inglês no meio do português ("casa *mansion* amarela") | O Marian não preserva inglês: estraga 41 de 178 frases em inglês (§2.3). |
| Traduzir tudo o que não for detectado como inglês | O `langdetect` só reconhece 6 de 30 palavras soltas em inglês, e o Marian estraga 6 das 24 restantes (§2.3). |
| Perguntar o idioma ao usuário | Muda a interface para resolver um problema que os marcadores resolvem sem perguntar. |
| Um CLIP multilíngue | Troca o codificador de texto, decisão do RFC-023 reaberta só por medição, e fora da versão simples. |
| Restaurar acentos em geral | Exige um dicionário inteiro. O léxico restaura só as suas formas, que são as que importam para as fotos. |

## 11. Riscos

1. **O viés de quem escreveu** (§4.4): o ganho medido na validação é otimista.
2. **Uma troca certa num contexto e errada em outro.** "Mato" dentro de "Mato Grosso", "capão" dentro de "Capão da Canoa": o nome próprio vira outra coisa. O CLIP não reconhece esses lugares de todo jeito, mas a consulta perde o sentido. Formas de várias palavras com nome próprio podem entrar no léxico sem troca, para que o casamento mais longo as proteja (`TBM` se for preciso). **Medido: é preciso em três** — "Mato Grosso", "Foz do Iguaçu" e "Capão da Canoa", que sem a linha viram *Vegetation Grosso*, *Estuary do Iguaçu* e *Canoa Tree Group*. Em outros nomes a troca até ajuda: "Morro de São Paulo" era *St. Paul's Death* e virou *Hill of São Paulo* (§15.5).
3. **Concordância** (§5): "casa grande amarelo".
4. **Falso português.** Uma consulta em inglês com "Rio de Janeiro" tem "de" e é traduzida. Se isso estraga o prompt é `TBM`; **medido** em 12 consultas em inglês com nome brasileiro, escritas para isso: 3 mudam de prompt, e 2 estragam — "Porto Alegre waterfront" vira *Port Alegre waterfront* e "harbor of Porto", *port port* ("porto" é forma do léxico). "beach in Rio de Janeiro" é traduzida e sai igual (§15.5); o critério 1 de §9 só cobre a validação.
5. **O sentido regional muda de região para região.** "Chácara" é sítio de lazer em uns lugares e pequena propriedade rural em outros. O léxico escolhe uma tradução, e a escolha fica registrada na linha.
6. **O léxico cresce à mão.** Cada linha nova passa pela sonda e pelo teste lento de §12. Uma linha sem prova de que o Marian erra não ganha `troca`.

## 12. Testes

**Rápidos** (com o Marian falso que os testes atuais já usam):

- a troca acontece antes do Marian, e o Marian recebe o texto trocado;
- palavra inteira; insensível a maiúsculas e acentos; o casamento mais longo vence;
- sem casamento, o texto chega ao Marian intocado;
- uma consulta em inglês não passa pelo léxico nem pelo Marian;
- cada marcador de §6 faz uma consulta curta ser traduzida, e uma palavra fora do léxico e da lista continua sem tradução;
- a lista de palavras funcionais não tem nenhuma das palavras inglesas de uma lista escrita no teste ("a", "as", "do", "no", "on", "in", "to", "or", "and", "the", "of", "at");
- o arquivo do léxico é válido: formas únicas depois de tirar acento e maiúscula, `en` não vazio, `origem` conhecida.

**Lentos** (`slow`, com o checkpoint real):

- para cada forma, a tradução do que vai ao Marian contém um dos `en`;
- para cada forma com `troca`, a tradução da forma original **não** contém nenhum dos `en`, e a troca é necessária;
- as 30 palavras inglesas do piloto não mudam de prompt;
- `test_reference_embeddings_slow`, como §8 descreve.

## 13. Entregáveis

1. `backend/app/infrastructure/ai/query_lexicon.toml`.
2. `query_translator.py`: o carregamento do léxico (uma vez, no import), a troca de §5 e a regra de §6.
3. Os testes de §12, e os de §8 ajustados.
4. `experiments/rfc-047-traducao-das-consultas/pilot.py` (+ `.json`), o piloto de §2.
5. `experiments/rfc-047-traducao-das-consultas/probe_lexicon.py` (+ `.json`), a sonda de §4.3.
6. A medição de §9, por `measure_baseline.py`, com os números escritos de volta aqui.
7. As notas de §8 no RFC-023 e no RFC-042.

## 14. Fora deste RFC

- Trocar o modelo de tradução (§10).
- Corretor ortográfico, ou restauração de acentos fora do léxico.
- Nomes de lugar: a busca por endereço é o RFC-043, e o CLIP não sabe onde fica nada.
- Um seletor de idioma na interface.
- Abrir o teste do RFC-042.

## 15. Medição (primeira rodada)

Tudo em 2026-11-03, nesta máquina, em CPU. O teste do RFC-042 não foi aberto. Esta seção é a primeira rodada, com o léxico de `experiments/rfc-047-traducao-das-consultas/query_lexicon-rodada1.toml`; a segunda, que corrigiu o defeito de §15.4, está em §17, e seus números não substituem estes.

### 15.1 A sonda

`probe_lexicon.py`, sobre o léxico final: 2 956 traduções, em 321 a 570 s conforme a rodada.

| | formas |
| --- | --- |
| no léxico | **410** (373 gerais, 37 do sul; 18 também inglês) |
| o Marian erra, sozinha ou em "{forma} vista de cima" | **113** |
| … e a troca acerta, nos dois contextos | **113** |
| troca onde o Marian acerta a forma | 0 |
| troca que não é a primeira candidata a passar | 22, cada uma com o motivo em `NOT_THE_FIRST` |
| troca que perde o `en` com um adjetivo depois | 4, em ambos os gêneros ou no que o termo não tem: "potreiro" e "invernada" → "pastagem" (*yellow grazing*), "torre de transmissão" → "torre de energia" (*yellow energy tower*), "terreno baldio" → "terreno vago" (*yellow vacant terrain*, só no feminino) |
| nome de lugar protegido | 3, e os 3 quebram sem a linha |

O primeiro rascunho, escrito antes de qualquer tradução, tinha 419 formas, e **142 falhavam**. Das 142, parte era do Marian e parte da lista `en`, curta demais: "navios" → *vessels*, "cais" → *docks*. Três rodadas de candidatas (todas guardadas em `CANDIDATES`, com o que o Marian fez de cada uma) levaram ao léxico final; o que mudou nele está em §16.

Exemplos, antes e depois da troca:

| forma | o Marian, sozinha | troca | o Marian |
| --- | --- | --- | --- |
| morro | *Death* | colina | *hill* |
| casarão | *will marry* | casa grande | *big house* |
| estádio | *stage* | estádio esportivo | *sports stadium* |
| viaduto | *viado* | ponte viaduto | *viaduct bridge* |
| chácara | *amber* | pequena fazenda | *small farm* |
| sanga | *blood* | córrego | *stream* |
| coxilha | *shell* | colina | *hill* |
| corredeiras | *rails* | águas brancas | *White waters* |

### 15.2 Os critérios de §9

`measure_baseline.py --only` na validação, com o código de antes deste RFC (`baseline-validation-20261103-132700.json`, que reproduz os números de §1 e de três rodadas anteriores) e com o deste (`baseline-validation-20261103-181000.json`), lidos por `measure.py`.

| critério | resultado | |
| --- | --- | --- |
| 1. nenhuma consulta em inglês muda de prompt | **178 de 178** iguais, nenhuma traduzida, ranks idênticos | passa |
| 2. entre as em português cujo prompt mudou, mais sobem do que descem | **38** mudaram (4 delas passaram a ser traduzidas): **11 sobem, 5 descem**, 22 ficam; as outras 140 têm o mesmo rank, consulta a consulta | passa |
| 3. recall@1 e @5 em português não caem | R@1 46,6 → **48,9** (+2,2 [0,0; +5,1]); R@5 78,1 → **78,7** (+0,6 [−1,7; +2,8]) | passa |
| 4. as 30 palavras soltas | as 30 em português estão no léxico e as **30 chegam ao CLIP certas** (antes, 8 eram traduzidas); as **30 em inglês** ficam com o prompt | passa |
| 5. léxico e marcadores, menos de 1 ms | **0,12 a 0,15 ms** de mediana e 0,32 a 0,36 ms no p95, em três rodadas de 87 800 chamadas; o máximo, uma chamada solta, de 2,6 a 7,5 ms | passa |

Recall, em %, antes → depois, com o intervalo de 95% do bootstrap pareado do RFC-042:

| idioma | R@1 | R@5 | MRR |
| --- | --- | --- | --- |
| en (178) | 55,1 → 55,1 | 83,7 → 83,7 | 67,9 → 67,9 |
| pt (178) | 46,6 → 48,9 (+2,2 [0,0; +5,1]) | 78,1 → 78,7 (+0,6 [−1,7; +2,8]) | 60,5 → 62,4 |
| pt, no subconjunto do sul (23) | 47,8 → 52,2 (+4,3 [0,0; +13,0]) | 95,7 → 91,3 (−4,3 [−13,0; 0,0]) | 65,2 → 67,3 |
| termos do sul (23) | 30,4 → **47,8** (+17,4 [+4,3; +34,8]) | 65,2 → **73,9** (+8,7 [−8,7; +26,1]) | 46,0 → 59,3 |

- **A perda que sobra** entre português e inglês, nas mesmas fotos: R@1 de −8,4 [−13,5; −3,9] para **−6,2 [−10,7; −2,2]**; R@5 de −5,6 [−10,1; −0,6] para **−5,1 [−9,6; −0,6]**. É o que resta para um tradutor melhor (§10). Entre os termos do sul e o português padrão: R@1 de −17,4 para −4,3 [−21,7; +8,7], R@5 de −30,4 para −17,4 [−34,9; 0,0] — com o viés de §4.4, um teto.
- **A queda do R@5 no português padrão do subconjunto do sul é uma consulta**: "morrinhos de terra laranja com uma estrada de terra", do 3º ao 7º lugar, por causa da troca de "estrada de terra" (§15.4).
- **Por cena** (R@5 em português): água 84,1 → 84,1; construído 74,0 → 76,0; campo 83,3 → 80,0 (30 consultas: uma a menos entre as 5 primeiras).
- **As que sobem** são as que §2 e o RFC-042 previam: "casarão de tijolos com colunas brancas no meio da mata", do 62º ao 2º; "capela pequena de pedra do lado de um estacionamento", tomada por espanhol, do 207º ao 1º; "campos verdes divididos por cercas vivas…", do 159º ao 21º; "rio com corredeiras…", do 24º ao 8º. No sul, "chácara com grama cortada em listras e um açude redondo", do 40º ao 2º.
- **As que descem**: "sítio arqueológico no topo de um morro", 2 → 4 (*small archaeological farm*, §15.4); "ilha coberta de mato com recife em volta", 5 → 10 (*forest* virou *vegetation*); "morrinhos… estrada de terra", 3 → 7; "barco de passeio… ao lado de uma marina", 1 → 2 (*marina of yachts*); "península… e calçadão…", 2 → 3.
- **Tempo do Marian**: as 4 consultas que passaram a ser traduzidas levaram 0,21 a 0,62 s cada, nas três rodadas. E o `langdetect` custa 8,6 a 9,4 ms de mediana: como os marcadores são conferidos antes dele, uma consulta com marcador agora o pula.

### 15.3 Os prompts de referência do RFC-035 (§8)

O texto que vai ao Marian muda em **14 das 25** consultas em português, e o prompt muda em **9**. Oito melhoram: "galpao ao lado de uma rodovia…" deixa de ser *galpao next to a highway* e vira *shed next to a highway*; "…complexo de predios" deixa de ser *grounds* e vira *buildings*; "rua residencial ladeada por casas", que não era traduzida, vira *residential street flanked by houses*. **Uma piora**: "terreno de construcao recem limpo com terra vermelha em terracos" era *clean new building land with red land on terraces* e virou *land of construction clean reme with red land on terracotta* — "construção" vai acentuada ao lado de "recem" e "terracos" sem acento, e o Marian se perde. `test_reference_embeddings_slow.py` foi separado como §8 descreve, e passa: os 50 prompts gravados dão os vetores gravados, dentro da tolerância do RFC-035 (a gravação é da outra máquina); as 36 consultas que este RFC não toca (as 25 em inglês e 11 em português) dão o prompt e o vetor gravados; as 14 tocadas dão o prompt de `CHANGED_BY_RFC_047`.

### 15.4 O que a sonda não viu: trocas que acrescentam palavras

As consultas que descem na validação apontam para o mesmo defeito, e frases escritas para conferir, fora do conjunto de avaliação, o confirmam (`measure.py`, `OUTSIDE_THE_PROBE`):

| frase | antes | depois |
| --- | --- | --- |
| estrada de terra perto do rio | *land road near the river* | *land road hit near the river* |
| uma estrada de terra entre duas fazendas | *a dirt road between two farms* | *a dirt road hit between two farms* |
| estrada de terra com árvores dos dois lados | *dirt road with trees on both sides* | *dirt road hit with trees on both sides* |
| sítio arqueológico | *sítio arqueológico* | *small archaeological farm* |
| sítio histórico no centro da cidade | *historical site in the city center* | *small historic farm in the city center* |
| farol no alto do morro | *Headlight on the top of the hill* | *beach lighthouse on the top of the hill* |
| marina com veleiros | *marina with sailboats* | *yacht marina with sailboats* |
| estádio de futebol lotado | *estádio de futebol lotado* (não traduzida) | *sports stadium football crowded* |

A sonda prova a troca sozinha, em "vista de cima" e com um adjetivo depois. Uma troca que **acrescenta** palavras ("batida", "de praia", "de iates", "esportivo") ou que troca uma palavra de **dois sentidos** ("sítio" é também *site*) passa nesses três contextos e erra em frases comuns. "estrada de terra" é o caso mais claro: o Marian já a traduzia certo dentro de frases (*dirt road*), e só errava sozinha e em "vista de cima" (*land road*); a troca acerta esses dois e estraga as frases com *hit*.

**Não foi corrigido aqui, nesta rodada** (corrigido na segunda, §17). O achado veio da validação, e §4.4 proíbe que o léxico mude por causa dela: corrigir agora tornaria o número de §15.2 ainda mais otimista. A correção é a próxima rodada do léxico, provada por uma sonda com mais contextos (a forma no meio de uma frase, com complemento antes e depois) e medida pelo teste do RFC-042 quando ele abrir. As candidatas óbvias: tirar a troca de "estrada de terra", "estradas de terra" e "estrada de chão" (ou trocar só "estrada de chão", que o Marian erra sempre: *ground road*), proteger "sítio arqueológico" e "sítio histórico", e rever "farol", "marina" e "estádio".

### 15.5 Os riscos de §11

- **Risco 2, nomes de lugar.** Medido em 10 consultas em português com nome, escritas para isso: com as 3 linhas de proteção, "fazenda no Mato Grosso", "praia em Capão da Canoa" e "cataratas em Foz do Iguaçu" saem como antes; "Morro de São Paulo" melhora (*St. Paul's Death* → *Hill of São Paulo*); "Serra Gaúcha" vira *Chain of mountains Gaucha* e "Chapada Diamantina", *Diamantine plateau*; "Porto Alegre", "Rio Grande do Sul" e "Porto Seguro" ficam como estavam, porque a maiúscula digitada é mantida (§16.1).
- **Risco 3, concordância.** Não estraga (§5, §15.1).
- **Risco 4, falso português.** 12 consultas em inglês com nome brasileiro: 3 mudam de prompt, 2 para pior (*Port Alegre waterfront*, *port port*). "Rio de Janeiro" tem "de" e é traduzida, mas sai igual.

## 16. Correções feitas durante a implementação

O texto de §1 a §14 fica como foi proposto. O que a implementação mudou ou decidiu está aqui, com a seção de origem. São as da primeira rodada; as da segunda estão em §17.3.

1. **§5, regra 3: a maiúscula digitada é mantida.** Mandar a forma do léxico como está (em minúscula) estraga nomes: o Marian lê "Porto Alegre vista de cima" como *Porto Alegre view from above* e "porto Alegre vista de cima" como *port Joy view from above*; "Rio Grande do Sul" vira *river Grande do Sul* em minúscula. Quando a forma foi digitada com maiúscula, a primeira letra do que vai ao Marian também vai maiúscula ("Galpao industrial" → "Galpão industrial"). Nunca se põe minúscula onde o usuário pôs maiúscula.
2. **§4.1 e §4.2: a forma sem acento não ganha linha própria.** O casamento já ignora acentos, então "galpao" casa com "galpão", e o teste de §12 exige formas únicas depois de tirar o acento — uma linha "galpao" seria recusada.
3. **§4.1: nomes de lugar.** Uma forma escrita com maiúscula ("Mato Grosso") é um nome de lugar, e está no léxico só para que o casamento mais longo a proteja (risco 2). O carregador exige que ela não tenha `troca` e seja `tambem_ingles` (um nome é igual nas duas línguas, e não é marcador). A sonda e o teste lento a conferem dentro de "vista aérea de {nome}", porque o Marian não preserva o nome sozinho ("Mato Grosso" é *Fat Kill*), e conferem também que, sem a linha, a frase quebra.
4. **§4.1: `tambem_ingles` cobre palavra inglesa de outro sentido.** "balsa" (a madeira, em inglês) e "vale" (*Okay.*, sozinha) estão marcadas, além das de mesmo sentido ("hotel", "canal", "píer", que sem acento é *pier*).
5. **§4.3: os contextos e a regra da sonda.** Além de sozinha e em "{forma} vista de cima", a sonda põe um adjetivo depois, nos dois gêneros ("{forma} amarelo/amarela"), e tira a palavra do adjetivo em inglês antes de comparar (*large yellow house* contém *large house*). A forma falha quando erra sozinha **ou** na frase; a troca precisa acertar nas duas. O `en` é conferido por palavras inteiras, sem acento nem maiúscula. As candidatas foram tentadas em três rodadas, e a troca escolhida é a primeira que passa, salvo em 22 formas, onde `NOT_THE_FIRST` diz por quê (a mais comum: a candidata repetiria o complemento que o usuário costuma digitar — "estádio de futebol" digitado viraria "estádio de futebol de futebol").
6. **§4.1: 30 listas `en` ampliadas depois da primeira rodada**, com sinônimos certos que o Marian usou e que o rascunho não tinha: *vessels* para "navios", *docks* para "cais", *coastal* para "litoral", *white waters* para "corredeiras", *group of trees* para "capão", *chicken farm* para "aviário"… O número não conta como erro: *herds* para "rebanho" e *skyscraper* para "arranha-céus" estão certos. Duas ampliações mudam o que §2 dizia: *planting* para "plantação", que §2.2 contou como erro do Marian, está certo pelo dicionário (área plantada), e a troca que a sonda achou ("lavoura") piorava as frases — "plantação de café" é *coffee plantation*, "lavoura de café", *coffee crop*; e *brown* para "barrento", que é a troca do piloto ("marrom"). A lista completa está na diferença entre o rascunho e o arquivo, e em `probe_lexicon.json`.
7. **§4.2: 17 formas saíram do léxico**, cada uma com o motivo em `DROPPED`, na sonda: as de dois sentidos que nenhuma troca serve ("quadra", esportiva ou quarteirão; "aterro"; "cerca", porque "cerca de" é "aproximadamente"; "depósito"; "fortaleza", que é também a cidade), as que nenhuma candidata acertou ("quebra-mar", "piquete", "chiqueiro", "chafariz", "paralelepípedos", "faixa de pedestres", "pires", e "rotatória", cuja única candidata que passa vira *rotary square*), "oficina" e "oficinas" (sozinhas viram *office*, mas em frase o Marian acerta *workshops* — nos prompts do RFC-035 — e toda troca piorava a frase), "curral" (idem, *corral* em frase) e "turva".
8. **§5: 5 formas entraram para proteger o que a troca manda**, além dos 3 nomes de lugar de §16.3 (8 ao todo): "estrada de terra batida" e o plural, porque a troca de "estrada de terra" é "estrada de terra batida", e digitada ela viraria "… batida batida"; "estrada de chão batido", pelo mesmo motivo; "encosta de morro" e "encosta do morro", porque a troca de "encosta" é "encosta de morro".
9. **§6: os marcadores são conferidos antes do `langdetect`.** O resultado é o mesmo da regra como escrita (um "ou"), e uma consulta com marcador deixa de pagar os ~9 ms do `langdetect`.
10. **§7: o tradutor recebe o léxico por parâmetro, opcional.** `MarianQueryTranslator(…, lexicon=…)`, para que os testes rápidos usem um léxico pequeno; o app usa o de `query_lexicon.toml`, carregado uma vez no import. O arquivo é lido como dado do pacote, por `importlib.resources`, como o lockfile do RFC-035: a primeira versão o achava por `__file__`, e o teste de arquitetura do RFC-034 recusou, porque só `layout.py` pode saber onde o código está. Nenhuma chave em `Settings`.
11. **§8 e §12: os nomes dos testes.** `test_single_word_portuguese_is_a_known_detection_gap` virou `test_single_word_portuguese_outside_the_lexicon_is_a_known_detection_gap` ("janela", "varanda") e ganhou o par `test_single_word_portuguese_in_the_lexicon_is_translated`. Em `test_reference_embeddings_slow.py`, `test_the_queries_land_on_the_same_vectors` virou `test_the_recorded_prompts_land_on_the_same_vectors` (os prompts gravados, codificados como estão) e `test_the_untouched_queries_land_on_the_same_vectors` (o caminho inteiro, nas 36 que este RFC não toca); `test_the_queries_rfc_047_touches_are_the_ones_listed` confere que `CHANGED_BY_RFC_047` é exatamente o efeito do léxico. Os testes lentos do léxico estão em `test_query_lexicon_slow.py` (563 testes lentos passam, com os de referência, em 7,4 min).
12. **§9: o "antes" foi medido de novo**, com o código de antes, em vez de reaproveitar o `.json` de §1. Deu os mesmos números, como as três rodadas anteriores do RFC-042.


## 17. Segunda rodada: o léxico contra frases

Em 2026-11-03 e 04, nesta máquina, em CPU. O teste do RFC-042 continua fechado.

### 17.1 Por que, e o que isto custa à medição

A validação da primeira rodada mostrou (§15.4) que a sonda de §4.3 provava cada troca **sozinha** e em "{forma} vista de cima", e que isso deixava passar o que a troca fazia a frases comuns: "estrada de terra" mandada como "estrada de terra batida" voltava *dirt road hit*, "sítio" → "pequena fazenda" fazia de "sítio arqueológico" uma *small archaeological farm*, "farol" ganhava uma praia. §15.4 deixou a correção para depois e disse como: uma sonda com mais contextos. Esta rodada é essa correção, e foi feita **depois de ver a validação**:

- o que motivou a rodada foi o que a validação mostrou, e não uma lista independente. O conjunto de avaliação não entrou em nenhuma conta: os contextos da sonda (§17.2) e as frases de `OUTSIDE_THE_PROBE` foram escritos para isto, e nenhuma forma ou troca entrou ou saiu por causa da posição de uma consulta;
- mas **uma decisão** teve a validação como parte da evidência: tirar as quatro formas de "estrada de terra" (§17.4). A tabela de §17.4, que não depende da validação, sozinha a justificaria; a consulta que foi do 3º ao 16º lugar com a troca por *gravel road* foi o que a fez sair em vez de ficar;
- por isso os números de §17.5 **não são limpos**, nem os do léxico de §15: o ganho na validação é otimista duas vezes, por §4.4 e por esta rodada. A comparação com a primeira rodada, que mede só o que esta rodada fez, está à parte (§17.5), e o número sem viés continua a ser o do teste do RFC-042.

### 17.2 A sonda de frases

`probe_frames.py` roda **o caminho do produto** — `lexicon.rewrite()` e depois o Marian — em sete contextos por forma, definidos em `backend/tests/infrastructure/ai/lexicon_frames.py` (o teste lento e a sonda importam o mesmo arquivo, para que o que a sonda escolheu seja o que o teste confere):

| contexto | texto, com a forma no lugar de {X} |
| --- | --- |
| sozinha | {X} |
| vista de cima | {X} vista de cima |
| frase 1 | {X} perto do rio |
| frase 2 | {X} com árvores dos dois lados |
| frase 3 | vista de cima de {X} num dia nublado |
| frase 4 | foto de {X} ao lado de uma casa branca |
| frase 5 | {X} no meio de um campo verde |

Sem artigo antes de {X}: o gênero da forma não é conhecido. Uma resposta está **certa** quando:

1. contém uma das `en` da forma, como palavras inteiras, sem plural (*waves* e *wave* são a mesma) e, numa `en` de várias palavras, com todas as palavras presentes em qualquer ordem — o Marian reordena em torno do substantivo ("foto de rua íngreme" é *street photo steep*);
2. **não tem nenhuma outra palavra** além das que a frase explica (`FRAME_WORDS`, escritas à mão), das palavras de todas as `en` que contém e de uma lista curta de palavras funcionais (`STOP`).

A segunda regra é o que a primeira rodada não tinha: *dirt road hit near the river* contém *dirt road*, e *hit* é o que a regra pega. Duas regras a mais: a forma digitada de novo não pode ser reescrita (a troca de "marina" era "marina de iates", e digitada virava "marina de iates de iates"), e as formas com outro sentido de uso comum (`OTHER_SENSES`: "sítio arqueológico", "sítio histórico") têm de o manter.

**Calibração.** O critério foi afrouxado três vezes depois de ver o que ele reprovava, e cada vez para um ruído do Marian e não para um erro dele: 406 das 407 formas reprovavam na primeira rodada da sonda, porque o controle por um substantivo neutro ("casa") era instável ("casa com árvores dos dois lados" vira *tree house*) e colidia com a "casa branca" da frase 4; trocado pelas palavras de cada frase escritas à mão, reprovavam 115; com o plural, o "'s" de *cat's view* e as variações de "vista de cima" (*upper view*, *over-sight*), 82; com a `en` de várias palavras em qualquer ordem e a união das `en` encontradas, 77. **Estes 77 são o número que vale:** o léxico da primeira rodada, medido com o critério final, reprova **77 das 407 formas** (50 com troca, 27 sem), das quais 12 por serem reescritas quando digitadas e 1 por um outro sentido ("sítio"). Nenhuma calibração viu as consultas de avaliação, mas é um julgamento meu e não está provada imparcial.

### 17.3 O que a rodada mudou

Do léxico de 410 formas ao de **419** (382 gerais, 37 do sul, 125 com troca, 18 também inglês): 9 saíram, 18 entraram, 36 trocas mudaram, 55 listas `en` foram ampliadas. As tentativas estão em `candidates-rodada2.json` (95 candidatas para 35 formas) e em `probe_lexicon.py` (`CANDIDATES`, da primeira rodada).

1. **`en` ampliada onde o modificador é fiel ao português.** A troca estava certa e a lista, curta: "pedreira" → *stone quarry*, "estádio" → *sports stadium*, "pomar" → *fruit orchard*, "duna" → *sand dunes*, "vale" → *mountain valley*, "estufas" → *agricultural greenhouses*, "serra" → *mountain chain*, "coqueiro" → *coconut palm tree*, "lava" → *volcanic lava*. O limite que usei: o modificador não pode acrescentar à cena o que a forma não diz (*mountain*, *stone* e *sports* dizem o que "vale", "pedreira" e "estádio" já dizem; *beach* não é o que "farol" diz). Uma lista foi reduzida: a de "obra" não aceita mais *work*, que o Marian dava sozinha e que não é o que uma foto aérea mostra.
2. **Troca nova onde a antiga acrescentava conteúdo.** "farol" → "farol de praia" (*beach lighthouse*) passou a "lighthouse"; "marina" → "marina de iates" (*yacht marina*, e digitada duplicava) a "porto de iates" (*yacht port*, 7 de 7); "açude" e "represa" → "reservatório" (*tank photo* na frase 4) a "barragem"; "araucária" → *peanut tree* a "pinheiro"; "capão" → "grupo de árvores" a "pequeno bosque"; "torre de transmissão" → "torre de energia" perdeu a troca (o Marian já acerta).
3. **Cinco empréstimos do inglês.** "farol" → "lighthouse", "caiaque" → "kayak", "quiosque" → "kiosk", "cânion" → "canyon", "balsa" → "ferry": o Marian repete a palavra inglesa, e ela acerta nos sete contextos, sem acrescentar nada à cena. É o contrário do que §10 rejeitou ("casa *mansion* amarela" estragava 41 de 178 *frases* em inglês); aqui é uma palavra solta dentro do português, e a diferença está medida só nos sete contextos (§17.6).
4. **Formas gêmeas.** 12 trocas eram reescritas quando digitadas. Nove ganharam uma forma gêmea, sem troca (ou com a troca da forma curta), para o casamento mais longo as proteger: "marina de iates", "duna arenosa" (que serve também a "cômoro"), "vale montanhoso", "pomar de frutas", "horta de legumes", "estufas agrícolas", "estádio esportivo", "feira de rua" e "ponte viaduto". "farol" e "parreiral" mudaram de troca. Quatro gêmeas entraram depois, pelo mesmo motivo, quando outras trocas mudaram: "balsa de madeira" (a troca de "jangada", depois que "balsa" virou "ferry"), "lava vulcânica", "estaleiro naval" e "prefeitura municipal". E "estádio de futebol", que o usuário digita e que viraria "estádio esportivo de futebol". Teste rápido: `test_a_troca_typed_by_the_user_is_not_rewritten_again`.
5. **O outro sentido de "sítio"**: "sítio arqueológico", "sítio histórico" e os plurais entram sem troca. "sítio" continua *small farm*, e o teste lento confere os dois sentidos.
6. **Erros do Marian em frase, em formas que não tinham troca** — 27 reprovavam com o critério final. As que tinham um erro real ganharam uma troca: "estaleiro" (*yard*) → "estaleiro naval", "lava" (*wash*) → "lava vulcânica", "povoado" (*villaged*) → "vilarejo", "aerogeradores" (*aero-generators*) → "turbinas eólicas", "cafezal" (*coffee shop*) → "plantação de café", "caiaque", "quiosque", "cânion", "balsa", "barranca", "neblina", "lavoura", "painel solar", "armazém" e "prefeitura". Algumas só precisavam de uma `en` maior ("reservatório" → *tank*, "soja" → *soya beans*, "subúrbio" → *suburban*). "bicicleta", "fonte" e "galpões" saíram (item 7).
7. **9 formas saíram**, cada uma com o motivo em `ROUND_2_DROPPED`, em `probe_frames.py`: "calçadão", "galpões", "conjunto habitacional" e "fonte" (nenhuma candidata acerta, ou acrescenta); "bicicleta" (o Marian some com o substantivo em "vista de cima de bicicleta", e a única troca que acerta, "bike", muda o prompt já certo de uma consulta do RFC-035); e as quatro de "estrada de terra" (§17.4).

### 17.4 Onde o critério mordeu, e a decisão que ele não tomou sozinho

`probe_frames.py --untouched` mede, para as 129 trocas de então, em quantos dos sete contextos o Marian já acertava **sem** a troca: **62 em nenhum**, 4 em um, 9 em dois, 8 em três, 11 em quatro, 10 em cinco e **25 em seis**. Das 129, 101 erram quando a forma vem sozinha, e 10 erram só aí. Quer dizer: a maioria das trocas conserta a palavra solta ("represa" → *recovery*, "falésia" → *sickle-*, "viaduto" → *viado*) e, em frase, só põe um sinônimo equivalente no lugar de uma resposta que o Marian já dava (*dam* por *dam*). O custo é pequeno e é o que paga a palavra solta, que é o caso do piloto (§2.2).

Uma troca é diferente das outras: a de "estrada de terra". O Marian acerta *dirt road* em 4 dos 7 contextos; a troca "estrada de cascalho" dá *gravel road* nos 7, **outro sentido**, e na validação uma consulta foi do 3º ao 16º lugar. A regra "a forma tem de passar nos sete contextos" aceitava a troca, e o julgamento foi meu: **as quatro formas saíram** ("estrada de terra", "estradas de terra", "estrada de terra batida" e o plural). Sem elas, a consulta "estrada de terra" sozinha volta a ser *land road*, e "estrada de terra batida entre as lavouras" volta a ser *land road hit between crops*, como antes deste RFC. "estrada de chão" e "estrada de chão batido" ficam com "estrada de cascalho", porque o Marian erra as duas sempre (*ground road*), e o sentido, que não é exato, é o melhor que passou.

### 17.5 Medição

`measure_baseline.py --only` na validação, com o código de antes do RFC (`baseline-validation-20261103-132700.json`) e com o léxico final (`baseline-validation-20261104-001130.json`), lidos por `measure.py`.

| critério | resultado | |
| --- | --- | --- |
| 1. nenhuma consulta em inglês muda de prompt | **178 de 178** iguais, nenhuma traduzida, ranks idênticos | passa |
| 2. entre as em português cujo prompt mudou, mais sobem do que descem | **31** mudaram (4 passaram a ser traduzidas): **10 sobem, 1 desce**, 20 ficam; as outras 147 têm o mesmo rank, consulta a consulta | passa |
| 3. recall@1 e @5 em português não caem | R@1 46,6 → **49,4** (+2,8 [+0,6; +5,6]); R@5 78,1 → **79,2** (+1,1 [−1,1; +3,4]) | passa |
| 4. as 30 palavras soltas | as 30 chegam certas ao CLIP (antes, 8 eram traduzidas); as 30 em inglês ficam com o prompt | passa |
| 5. léxico e marcadores, menos de 1 ms | **0,04 ms** de mediana, 0,07 ms no p95, 1,2 ms no máximo, em 87 800 chamadas (a máquina estava livre; na primeira rodada, 0,12 a 0,15) | passa |

| idioma | R@1 | R@5 | MRR |
| --- | --- | --- | --- |
| en (178) | 55,1 → 55,1 | 83,7 → 83,7 | 67,9 → 67,9 |
| pt (178) | 46,6 → **49,4** (+2,8 [+0,6; +5,6]) | 78,1 → **79,2** (+1,1 [−1,1; +3,4]) | 60,5 → 62,9 |
| pt, no subconjunto do sul (23) | 47,8 → 52,2 (+4,3 [0,0; +13,0]) | 95,7 → 95,7 | 65,2 → 68,1 |
| termos do sul (23) | 30,4 → **43,5** (+13,0 [0,0; +30,4]) | 65,2 → **78,3** (+13,0 [−4,3; +30,4]) | 46,0 → 57,4 |

- **A perda que sobra** entre português e inglês: R@1 de −8,4 [−13,5; −3,9] (antes do RFC) para **−5,6 [−10,1; −1,7]**; R@5 de −5,6 [−10,1; −0,6] para **−4,5 [−9,0; 0,0]**. Entre os termos do sul e o português padrão: R@1 de −17,4 para −8,7 [−21,7; 0,0], R@5 de −30,4 para −17,4 [−34,8; −4,3], com o viés de §4.4.
- **Pela primeira vez o intervalo do recall@1 em português fica acima de zero** (+0,6 no extremo baixo). Em 178 consultas é ±1 consulta, e vem de uma rodada feita depois de ver a validação (§17.1): lê-se como "não caiu e provavelmente subiu", e não como a estimativa do ganho.
- **Por cena** (R@5 em português): água 84,1 → 84,1; construído 74,0 → 76,0; campo 83,3 → 83,3.
- **Segunda rodada contra a primeira**, só o que esta rodada fez (**post hoc**, não é critério): 13 das 178 consultas em português mudaram de prompt entre as rodadas; 4 sobem, 2 descem, 7 ficam. R@1 48,9 → 49,4 (+0,6 [0,0; +1,7]), R@5 78,7 → 79,2 (+0,6 [0,0; +1,7]). Nos termos do sul, 8 das 23 mudaram: 1 sobe, 1 desce; R@1 47,8 → 43,5 (−4,3 [−13,0; 0,0]) e R@5 73,9 → 78,3 (+4,3 [0,0; +13,0]), uma consulta em cada. **A rodada corrigiu o texto que vai ao Marian ("beach lighthouse" → *lighthouse*, *small archaeological farm* → *archaeological site*, *dirt road hit* → *dirt road*) e mexeu pouco no recall.** O ganho de §15.2 vem quase todo da primeira rodada, e a segunda o conservou, com um léxico que erra menos em frases.
- **Os mesmos exemplos de §15.4, fora do conjunto de avaliação** (`OUTSIDE_THE_PROBE`, antes → depois): "sítio arqueológico" → *small archaeological farm* → *archaeological site*; "sítio histórico no centro da cidade" → *small historic farm…* → *historical site…*; "farol no alto do morro" → *beach lighthouse…* → *lighthouse…*; "estádio de futebol lotado" → *sports stadium football crowded* → *football stadium crowded*; "uma estrada de terra entre duas fazendas" → *a dirt road hit between two farms* → *a dirt road between two farms*; "marina com veleiros" → *yacht marina…* → *yacht port…*.
- **Os prompts de referência do RFC-035 (§8)**: o mesmo resultado da primeira rodada, 14 de 25 tocados e 9 com o prompt mudado; `CHANGED_BY_RFC_047` não mudou.
- **Riscos de §11, de novo, nas mesmas 22 consultas**: "Porto Alegre waterfront" segue *Port Alegre waterfront* e "harbor of Porto" segue *port port*, os dois por "porto" ser forma do léxico (risco 4, não corrigido).

### 17.6 O que continua errado

1. **Os empréstimos do inglês estão provados em sete contextos** (§17.3, item 3). Uma frase mais longa pode levar o Marian a trocar a palavra inglesa, como trocava *island* por *Iceland*. O teste lento confere cada uma nos sete contextos, e não na frase que alguém digitar.
2. **"estrada de terra" e "estrada de terra batida" voltaram ao Marian puro**: sozinhas, *land road*; "batida" em frase ainda vira *hit* (§17.4). "estrada de chão" virou *gravel road*, que não é *dirt road*.
3. **O critério é meu** (§17.2): as listas `en` e as palavras de `FRAME_WORDS` e `STOP` foram escritas e ampliadas por mim, olhando as respostas do Marian. 55 listas `en` foram ampliadas nesta rodada; cada ampliação aceita um modificador que me pareceu fiel, e outra pessoa traçaria a linha em outro lugar.
4. **Sete contextos não são a língua.** A sonda pega o que ela prevê. A primeira rodada previa três e deixou passar um defeito de §15.4; esta prevê sete, e o próximo defeito virá de um oitavo. O único número sem viés é o do teste do RFC-042.
5. **Nomes de lugar** (risco 2, §15.5) seguem como estavam: "Serra Gaúcha" → *Chain of mountains Gaucha*, porque "serra" é forma com troca e "Serra Gaúcha" não é um nome protegido.
6. **O Marian ainda erra "porto"** em "Porto Alegre waterfront" e "harbor of Porto" (risco 4).

### 17.7 Testes

**Rápidos**: `test_a_troca_typed_by_the_user_is_not_rewritten_again` (nenhuma troca é reescrita quando digitada) e `test_what_the_second_round_sends_for_what_the_first_got_wrong` (12 frases digitadas e o que o léxico manda ao Marian), sem modelo.

**Lentos** (`test_query_lexicon_slow.py`, com as regras de `lexicon_frames.py`): para cada forma, a resposta nos sete contextos tem uma `en` e nenhuma palavra a mais; as formas com outro sentido o mantêm; a troca é necessária, isto é, a forma sem ela erra em algum contexto; cada nome de lugar é mantido e precisaria da sua linha; as 30 palavras inglesas do piloto ficam com o prompt. Passam, com `test_reference_embeddings_slow.py` e os `TestRealTranslation`, em **586 testes**, em 12 min, sobre o léxico final.
