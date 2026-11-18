# Roadmap — Fine-tuning do CLIP

Este roadmap cobre o trabalho entre o [RFC-042](rfc-042-fine-tuning-do-clip.md), que está proposto e sem nada medido, e uma de três saídas: **um adapter ou checkpoint ajustado no lockfile do RFC-035**, com o ganho medido num conjunto de avaliação que decide; **a troca por um checkpoint pronto** (RemoteCLIP ou GeoRSCLIP); ou **o resultado negativo registrado**. As três são um fim válido. O roadmap não promete que a busca melhora. Ele define a ordem em que se descobre se melhora, gastando o mínimo antes de cada decisão.

**Quando começa.** Depois da Etapa 4 do [roadmap do frontend](ROADMAP-frontend.md) (o RFC-041, o instalador, em 29 e 30/10), como o [RFC-044](rfc-044-fotos-por-localizacao-sem-consulta-textual.md) já registrou. Nada daqui compete com o frontend: o trabalho roda fora do app, e só a última etapa toca o código dele.

**Por que nesta ordem.** Treinar é barato. Caro é legendar e avaliar, e o custo de distribuir um encoder de imagem novo cai na máquina do usuário: a reindexação do acervo inteiro. Por isso vem primeiro um instrumento que mede, depois as alternativas baratas, e o treino caro só se elas falharem.

Status segue as [convenções](README.md#convenções): todo número é `TBM` até a implementação escrevê-lo de volta. As durações são estimativas, e não medições.

---

## Ponto de partida

### O que já foi medido

| Medição | Valor | De onde |
|---|---|---|
| Checkpoint | `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`, revisão `1a25a446`, MIT, 605 MB em `safetensors`, carregado como `CLIPModel` do `transformers` | [`models.lock.json`](../../backend/app/infrastructure/ai/models.lock.json), RFC-035 |
| Benchmark | As 45 imagens do RFC-022 (40 aéreas e 5 do cotidiano) e 25 consultas em inglês, com `relevant` e `hard_negatives` | `backend/dataset/demo/` |
| Top-1 estrito, inglês | 60,0% no bake-off (numpy); 64,0% pelo caminho de produção | RFC-023 §3; [RFC-025 §11.3](rfc-025-busca-semantica.md) |
| Recall@1 / @5 / @10 | 64,0% / 84,0% / 96,0% | RFC-025 §11.1 |
| Pairwise | 80,4%, igual nos dois caminhos | RFC-025 §11.3 |
| Top-1 estrito, português traduzido | 56,0% no bake-off; e 56,0% pelo caminho de produção depois da errata, com Recall@5 de 88,0%, nas mesmas 25 consultas | RFC-023 §3.4; [RFC-025 §11.6](rfc-025-busca-semantica.md) |
| Onde falha | Lago natural contra tanques de piscicultura (consultas 2 e 4, as duas com negação), rua comercial e cidade de encosta (14 e 20) | RFC-025 §11.2 |
| Inferência em CPU | ≈ 450 ms por imagem | [Roadmap sem Docker](ROADMAP-standalone.md#fora-deste-roadmap) |

**A máquina desta pasta**, pelo `nvidia-smi` de 2026-10-23: uma GeForce GTX 1050 com 2 GB, 1 GB deles já ocupado pela tela, um Pentium G4560 de dois núcleos e 16 GB de RAM. **Ela não serve para o LoRA**, que pede 8 GB ou mais ([RFC-042 §6.1](rfc-042-fine-tuning-do-clip.md#61-onde-roda)). Serve para o nível A, que treina sobre embeddings já calculados, em CPU.

### O que está faltando ou divergente

- **O benchmark do RFC-023 não está no branch principal.** O RFC-042 §3.1 e §7.6 mandam rodar "o benchmark do RFC-023". Os scripts do bake-off estão no git, mas no branch `explore/siglip-bakeoff`, de propósito (o README de lá: *"Kept off `develop` deliberately"*): `siglip_bakeoff.py`, os `clip_*_bakeoff.py` e os resultados em JSON. A pasta local `experiments/rfc-023-siglip-bakeoff/` só tem os logs. O que está no branch principal é o [`measure_retrieval.py`](../../experiments/rfc-025-semantic-search/measure_retrieval.py) do RFC-025. Ele importa as métricas de `test_semantic_search_e2e.py`, mas passa pelo PostgreSQL e só conhece um modelo, o do lockfile.
- **Há duas linhas de base, e o RFC-042 cita a mais baixa.** 60,0% (bake-off) e 64,0% (produção) diferem em uma consulta, por virtualenvs e builds diferentes do `transformers`. Um ganho de 4 pontos sobre o número errado é diferença de instrumento, e não de modelo.
- **O português nunca foi medido depois da errata do tradutor** — *resolvido em 2026-10-26*. Os 56,0% eram do bake-off. A errata de 2026-09-27 ([RFC-023 §22](rfc-023-adaptador-de-embedding-clip.md#22-errata-2026-09-27)) tirou o prefixo `>>por<<`, que fazia frases inteiras degenerarem em pontos. As 25 consultas em português do bake-off não se perderam: estavam no `siglip_bakeoff.py` do branch `explore/siglip-bakeoff`. Agora são o campo `query_pt` de `queries.json`, com acento, e foram medidas pelo caminho de produção ([RFC-025 §11.6](rfc-025-busca-semantica.md)). O conjunto do RFC-035 (`PORTUGUESE_QUERIES` em `measure_offline_models.py`: cinco copiadas do log do bake-off e vinte novas, todas sem acento) continua só para comparar vetores.
- **Os conjuntos de avaliação 5.1.2 e 5.1.3 do RFC-042 não existem.** Sem o primeiro, nada decide (RFC-042 §11.3).
- **As 40 imagens aéreas do benchmark são do fotógrafo do projeto**, com permissão para uso público no repositório. Não são CC (o `license_note` do `manifest.json`). O RFC-042 §4.1 veda o acervo do dono para treino e avaliação, e o §5.1.1 usa o benchmark. A leitura que este roadmap adota: as 40 continuam só na avaliação, onde já estão, e nunca entram no treino. A permissão cobre o repositório, e não um modelo distribuído no instalador.
- **Trocar de checkpoint também reindexa.** O RFC-042 §3.1 trata a troca por RemoteCLIP ou GeoRSCLIP como "uma entrada no lockfile". Mas são pesos de imagem diferentes, e o efeito sobre o acervo é o mesmo do nível B ([§9 dele](rfc-042-fine-tuning-do-clip.md#9-efeito-sobre-o-acervo-já-indexado)). A ~450 ms por imagem, um acervo de 100 000 fotos custa ~12,5 h de CPU na máquina do usuário (estimativa, só a conta).
- **O formato dos checkpoints de sensoriamento remoto é `TBM`.** O carregador do RFC-023 lê só o `CLIPModel` do `transformers`, e o RFC-042 §3.3 propõe o código de treino do `open_clip`. Qualquer conversão entre os dois precisa de um teste de equivalência numérica.

**O que já serve e não precisa ser escrito de novo:** o `dataset_tools/reencode.py` (redimensiona e tira o EXIF); o formato do `manifest.json`, que já registra a URL de origem, a licença e a atribuição das imagens do Commons (a procedência do RFC-042 §4.4 é esse formato com um campo a mais, a fonte da legenda); as métricas de `test_semantic_search_e2e.py`; e o `rfc035_reference_embeddings.npz`, com o teste que o confere.

---

## Decisões a travar antes da Etapa 0

| # | Decisão | Recomendação | Por quê |
|---|---|---|---|
| D1 | A ordem entre a porta de entrada (RFC-042 §3.1) e o nível A | **Medir as duas antes de escolher.** Entre as que passam no critério, fica a mais barata de *distribuir* | A troca de checkpoint reindexa o acervo, e o nível A não. A porta de entrada é a mais barata de *medir*, e não de entregar. Isso muda o RFC-042 §3.1, que decide a troca antes de treinar |
| D2 | Com o que treinar | **O `CLIPModel` do `transformers`, carregado de `runtime\models`**, o mesmo do app. O nível A é um laço de PyTorch sobre embeddings já calculados. O B usa o `peft` sobre o mesmo modelo | Sem conversão de formato, e o pré-processamento e o template de prompt ficam iguais aos do app. Isso muda o RFC-042 §3.3, que propõe o `open_clip` |
| D3 | A língua das consultas de avaliação | **Português, como o usuário digita, com acento, e a mesma consulta em inglês, escrita à mão** | Se o português erra e o inglês acerta, o erro é do tradutor (`tanque` → `tank`, RFC-023 §16.3), e nenhum fine-tuning do CLIP o corrige. Só as duas versões separam um erro do outro |
| D4 | Quem legenda | **Um VLM cujos termos de uso permitam treinar com a saída e distribuir o modelo resultante.** 5 a 10% das legendas revisadas à mão ([RFC-042 §4.3](rfc-042-fine-tuning-do-clip.md#43-legendas)). O modelo escolhido é `TBM` | O checkpoint vai no instalador (RFC-042 §8.3). Um termo que proíba isso invalida o treino, como uma licença de imagem incompatível |
| D5 | Onde roda o LoRA | **Não precisa ser travada agora.** Só importa se o nível A falhar | Esta máquina não serve. As opções são a máquina de terceiros do RFC-042 §7 ou o Colab/Kaggle, que o RFC chama de plano B. Sem o acervo no treino, não há dado confidencial a proteger |

---

## Etapa 0 — O instrumento (RFC-042a) · 1 dia

Entrega um avaliador por onde qualquer candidato passa, e a linha de base única contra a qual tudo se compara. É a Etapa 0 pelo mesmo motivo da do [roadmap sem Docker](ROADMAP-standalone.md#etapa-0--reconstruir-o-que-se-perdeu-): sem ela, nenhuma etapa seguinte prova que ganhou alguma coisa.

**Escopo**
- `experiments/rfc-042-fine-tuning/evaluate.py`, **rastreado no git desde o primeiro commit**. O RFC-023 perdeu os scripts do bake-off por não rastreá-los.
- Recebe um diretório de modelo, um adapter de texto opcional e um conjunto de avaliação. Imprime o top-1 estrito, o Recall@1, @5 e @10, o pairwise e a contaminação@5, em inglês e em português pelo tradutor do app, e separados por categoria (rua, paisagem urbana, água, negação).
- Usa o pré-processamento e o template de prompt do `ClipEmbeddingModel`, e importa as métricas de `test_semantic_search_e2e.py`, como o `measure_retrieval.py` já faz. Os números não podem divergir dos pisos da suíte.
- Calcula o cosseno em memória, sem o PostgreSQL. No PostgreSQL 16 embarcado a busca já é uma varredura exata ([RFC-038 §14](rfc-038-busca.md#14-correções-feitas-durante-a-implementação)), então a ordem é a mesma. Uma execução cruzada com o `measure_retrieval.py` confere isso uma vez.
- Guarda os embeddings de imagem de cada checkpoint num cache em disco, fora do git, para que cada experimento recalcule só o lado do texto.
- As 25 consultas em português têm um lugar só, o `query_pt` de `queries.json`, lido pelo avaliador, pelo `measure_retrieval.py` e pela suíte `slow` (feito em 2026-10-26).
- Roda no `backend/.venv`, porque mede o código do app. O ambiente separado do RFC-042 §6.3 só é preciso no nível B.

**Critérios de pronto**
- Com o checkpoint do lockfile, o avaliador reproduz os 64,0% de top-1 estrito e os 80,4% de pairwise do RFC-025, ou explica a diferença.
- O top-1 estrito em português, nas 25 consultas, depois da errata: `TBM`. **Medido em 2026-10-26, pelo caminho de produção: 56,0%, com Recall@5 de 88,0%** ([RFC-025 §11.6](rfc-025-busca-semantica.md)). É comparável com os 56,0% do bake-off: são as mesmas 25 consultas, recuperadas do branch `explore/siglip-bakeoff`, e não as do RFC-035.
- Tempo de uma avaliação completa a partir do cache: `TBM`.

---

## Etapa 1 — Fontes e licenças · 1 a 2 dias

O primeiro passo da implementação, pelo próprio RFC-042 (§4.2 e §13). Pode rodar junto com a Etapa 0.

**Escopo**
- Para cada fonte do RFC-042 §4.2: a licença, se ela permite treinar e distribuir o modelo, o volume disponível e a proporção de vistas oblíquas e verticais.
- O Commons segue a regra que o `manifest.json` já escreve: CC0 ou CC-BY, verificado pela `extmetadata` da API, imagem por imagem, com um User-Agent próprio. Se o CC-BY-SA entra é uma decisão desta etapa, e só vale para o treino com a resposta escrita.
- A licença, o formato e a disponibilidade do RemoteCLIP e do GeoRSCLIP em ViT-B/32.
- Os termos do VLM da D4.

**Critério de pronto**
- Uma tabela por fonte, com volume (`TBM`), licença e a resposta para "o modelo treinado com isto pode ir no instalador?".

**Saída antecipada.** Se as fontes licenciadas não derem as imagens oblíquas do conjunto de avaliação (500 a 1 000, RFC-042 §5.1), o roadmap para aqui, com o registro. Sem instrumento, nada decide, e treinar seria presumir.

---

## Etapa 2 — Os conjuntos de avaliação (RFC-042a) · 3 a 5 dias

O caminho crítico do roadmap. É quase todo trabalho humano, e é ele que decide se o resto vale alguma coisa.

**Escopo**
- **O conjunto de oblíquas (RFC-042 §5.1.2).** De 500 a 1 000 imagens com licença limpa, passadas pelo `reencode.py`, com a procedência no formato do `manifest.json`.
- **As consultas.** Escritas à mão, em português e em inglês (D3), cada uma para uma imagem-alvo e com os negativos difíceis marcados, como no RFC-022. Ruas, paisagem urbana e negação, as falhas conhecidas, entram como categorias com nome, para serem lidas à parte.
- **Quem dá o poder estatístico é o número de consultas, e não o de imagens.** Com um acerto perto de 60%, o erro padrão do top-1 é de ~4,9 pontos com 100 consultas e de ~2,4 com 400 (estimativa, só a conta). O número de consultas sai desse cálculo (`TBM`), e a comparação é pareada: as mesmas consultas nos dois modelos.
- **A relevância por *pooling*.** Uma foto relevante que ninguém marcou conta como erro do modelo que a achou. O top-K de cada candidato é revisado à mão, sem saber de qual modelo veio.
- **O conjunto de regressão (RFC-042 §5.1.3).** Consultas gerais, fora do domínio aéreo, com imagens CC0 ou CC-BY do Commons, no mesmo processo do bloco `everyday/` do RFC-022.
- **As divisões.** Validação e teste ([RFC-042 §4.6](rfc-042-fine-tuning-do-clip.md#46-divisões)). As escolhas de configuração usam só a validação. O teste é aberto uma vez por candidato final.
- **A margem de aceitação do RFC-042 §5.3, escrita antes de qualquer número do teste.** O commit é a prova de que a margem veio antes.

**Critérios de pronto**
- O número de consultas (`TBM`, pelo cálculo acima) e a margem, no RFC-042a, num commit anterior a qualquer medição no teste.
- O resumo do conjunto de teste (um hash da lista de arquivos) registrado, para provar depois que ele não mudou.

**Riscos**
- **Quem escreve as consultas de teste ensina o próprio jeito de escrever.** Se a mesma pessoa escreve as legendas de propósito do RFC-042 §4.3 (item 3), o modelo pode aprender o fraseado dela, e não o domínio. Ou são pessoas diferentes, ou as legendas vêm depois de o teste estar congelado.
- **Poucas fotos oblíquas de propriedades brasileiras com licença aberta.** É o risco 1 do RFC-042 §11, e esta etapa o mede antes de qualquer treino.

---

## Etapa 3 — Checkpoints prontos (RFC-042 §3.1) · 1 a 2 dias

**Escopo**
- O RemoteCLIP e o GeoRSCLIP, nas variantes da Etapa 1, convertidos para o `CLIPModel` se preciso, com o teste de equivalência numérica.
- Os dois e a base passam pelo avaliador: o benchmark do RFC-022, a validação do conjunto de oblíquas e a regressão.
- A latência de codificar uma imagem, por segurança: com a mesma arquitetura, ela não deve mudar.

**Critérios de pronto**
- Uma tabela com os três checkpoints, em inglês e em português, por categoria. Nenhum número do teste ainda.
- A resposta à questão em aberto 2 do RFC-023 §19: com um conjunto que decide, o teto de ~60% é do modelo ou do corpus?

Pela D1, o resultado não decide sozinho. Ele segue para a comparação com o nível A.

---

## Etapa 4 — Dados de treino do nível A · 3 a 5 dias

Só o volume do nível A: de 2 a 5 mil pares ([RFC-042 §4.5](rfc-042-fine-tuning-do-clip.md#45-volume-e-composição)). O volume do LoRA (20 a 50 mil) só é coletado se a Etapa 5 falhar. Pode rodar junto com a Etapa 3.

**Escopo**
- Imagens das fontes aprovadas na Etapa 1, com as verticais abaixo de ~70% do conjunto (`TBM`).
- As legendas pelo VLM da D4, com o título do Commons como dica. 5 a 10% revisadas à mão.
- As legendas escritas de propósito para ruas, paisagem urbana e negação.
- A procedência por imagem: a fonte, o autor, a licença, a data e a fonte da legenda (humana, VLM ou original).
- **Nenhuma imagem do treino perto de uma da avaliação.** A conferência é por hash perceptual e pela origem: a mesma série de um autor, ou recortes da mesma ortofoto.

**Critérios de pronto**
- O número de pares (`TBM`) e a taxa de erro achada na revisão (`TBM`).
- Zero sobreposição com os conjuntos de avaliação, conferida por um script, e não à vista.

---

## Etapa 5 — Nível A: o adapter no texto (RFC-042) · 1 a 2 dias

O único nível que não toca o acervo: os vetores das imagens não mudam, e não há reindexação nem migration.

**Escopo**
- Os embeddings das imagens de treino calculados uma vez, pelo checkpoint do lockfile. A ~450 ms por imagem, 5 000 imagens levam ~38 min nesta máquina (estimativa).
- Uma matriz 512 × 512 sobre o embedding da consulta, começando na identidade, treinada com a perda contrastiva simétrica, com o lado da imagem congelado.
- **Uma hipótese a testar:** as legendas de treino passam pelo mesmo tradutor PT→EN do app, para que o adapter veja a consulta como o app a entrega ao CLIP, com os erros sistemáticos do tradutor junto.
- As configurações (taxa de aprendizado, épocas, regularização em direção à identidade) escolhidas na validação.
- Se a Etapa 3 achou um checkpoint melhor, o adapter também é treinado sobre ele.
- O candidato final medido uma vez no teste.

**Critérios de pronto**
- O critério do RFC-042 §5.3: supera a base por mais que a margem da Etapa 2, no teste do conjunto de oblíquas, sem piora na regressão. `TBM`.
- A busca por imagem (*"Parecidas com esta"* e a foto solta na barra) não muda: os vetores das imagens são os mesmos. Um teste confere isso bit a bit.
- O custo na consulta: `TBM` ms a mais, contra os 348 ms da busca em português (RFC-035). Uma multiplicação 512 × 512 deve ser desprezível.
- O tempo de treino: `TBM` (minutos em CPU, pela estimativa do RFC-042 §6.2).

**Se passa,** vai para a Etapa 7, e o nível B não é tentado. **Se não passa** e a Etapa 3 achou um checkpoint que passa, a escolha é entre a troca e o nível B, os dois com reindexação.

---

## Etapa 6 — Nível B: LoRA nos dois encoders (RFC-042) · 5 a 10 dias, só se A falhar

**Escopo**
- A Etapa 4 de novo, até 20 a 50 mil pares. A revisão de 5 a 10% vira de 1 000 a 5 000 legendas lidas à mão: é aqui que o custo humano cresce.
- O pacote do [checklist do RFC-042 §7](rfc-042-fine-tuning-do-clip.md#7-pré-requisitos-e-checklist): um diretório só, com os dados, os scripts e um `requirements` com versões fixas. O script de treino é testado nesta máquina, em CPU, com 50 a 100 pares, antes de ir para a GPU.
- A lista de configurações (posto do LoRA, taxa de aprendizado, épocas), para rodar várias numa visita. Checkpoints intermediários e o log da perda por época.
- A fusão do LoRA nos pesos base, com o teste de equivalência numérica da fusão.
- A mesma avaliação da Etapa 5. O nível C fica registrado e não é proposto (RFC-042 §3.2).

**Critérios de pronto**
- O critério do RFC-042 §5.3, como na Etapa 5.
- O tempo real de treino e o hardware (`nvidia-smi`): `TBM`.

**Se não passa,** o roadmap acaba com o resultado negativo escrito no RFC-042, que também é útil (§5.3 dele).

---

## Etapa 7 — Integração no produto (RFC-045) · 1 a 2 dias pelo nível A

Um RFC novo, com o próximo número livre, porque trocar o modelo é um RFC, e não uma opção ([RFC-035 §7](rfc-035-modelos-offline.md)).

**Escopo, pelo nível A**
- O adapter (~1 MB em `float32`) entra no lockfile do RFC-035 com o `sha256`, e o `ClipEmbeddingModel` o aplica antes da normalização. Tudo atrás da `EmbeddingModelPort`: nada muda no Domain nem na Application (RFC-023 §5).
- A referência do RFC-035: a metade das imagens continua valendo, e a das consultas é gravada de novo, com o adapter.
- Os pisos de `test_semantic_search_e2e.py` revistos com o número novo, pelo método do RFC-025 §11.4.
- A licença do adapter registrada junto com a procedência dos dados (RFC-042 §8.3).

**Escopo, pela troca ou pelo nível B**
- O checkpoint entra no lockfile, e o teste de determinismo do RFC-023 §6.2 é refeito sobre ele.
- O plano de reindexação que o RFC-042 §9 deixou para cá: o modelo versionado no registro, o que a busca faz durante a migração e o que o hash de conteúdo do RFC-024 reaproveita.

**Critérios de pronto**
- Numa instalação simulada, sem rede, a busca em português e em inglês usa o modelo novo, e a suíte e os testes `slow` passam.
- A diferença no tamanho do instalador: `TBM`.
- Pela troca ou pelo nível B: o tempo de reindexar o dataset de demonstração, e a estimativa para 100 000 fotos (`TBM`).
- O status do RFC-042 no [índice dos RFCs](README.md), onde ele ainda não aparece.

---

## Caminho e duração

| Etapa | Duração (estimativa) | O que pesa |
|---|---|---|
| 0. O instrumento | 1 dia | Código |
| 1. Fontes e licenças | 1 a 2 dias | Leitura de licenças. Junto com a 0 |
| 2. Conjuntos de avaliação | 3 a 5 dias | Consultas e julgamentos à mão |
| 3. Checkpoints prontos | 1 a 2 dias | Conversão de formato. Junto com a 4 |
| 4. Dados do nível A | 3 a 5 dias | VLM e revisão |
| 5. Nível A | 1 a 2 dias | CPU desta máquina |
| 7. Integração, pelo nível A | 1 a 2 dias | Código |
| **Pelo caminho A** | **11 a 19 dias úteis** | |
| 6. Nível B, se A falhar | mais 5 a 10 dias | Legendas e acesso a uma GPU |

As saídas antecipadas, em ordem:

1. **Sem imagens oblíquas licenciadas para a avaliação** (Etapa 1): para, com o registro.
2. **O nível A passa** (Etapa 5): integra o adapter, e o B não é tentado.
3. **Uma licença ou um termo de uso torna o modelo não distribuível** (Etapas 1 e 4): o treino não serve ao produto, mesmo que a acurácia suba (RFC-042 §8.3).
4. **O nível B não passa** (Etapa 6): o resultado negativo fica escrito.

---

## Fora deste roadmap

- **O tradutor PT→EN.** Se a Etapa 0 ou a 3 mostrar que o português perde mais pelo tradutor que pelo CLIP, a alavanca é outra (RFC-023 §16.3 e §19.4), com um RFC próprio. A D3 existe para que essa resposta saia daqui.
- Trocar a arquitetura ou o modelo base. O SigLIP só se reabre por medição (RFC-023 §3.5).
- O nível C, o ajuste completo.
- Quantização, ONNX e GPU na inferência do app.
- Treinar na máquina do usuário, ou continuamente a partir do uso, e um serviço de feedback da busca.
- O acervo do dono do produto, em qualquer etapa (RFC-042 §4.1).
