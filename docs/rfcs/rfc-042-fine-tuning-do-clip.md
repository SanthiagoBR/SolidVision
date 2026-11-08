# RFC-042 — Fine-tuning do CLIP

**Status:** Nível A implementado e no produto (§16). A porta de entrada de §3.1 foi medida (§14): nenhum checkpoint aéreo supera o atual. O nível A foi treinado em fotos do acervo real e medido num conjunto de consultas escritas só pelo usuário e no teste fechado do acervo; o produto aplica a matriz em cada busca por texto, sem reindexar. Os níveis B e C não foram começados
**Depende de:** RFC-023 (o adaptador de embedding, o benchmark do bake-off e [§18](rfc-023-adaptador-de-embedding-clip.md#18-fine-tuning-e-trabalho-futuro), que adiou este assunto), RFC-025 (a busca semântica e a linha de base de [§11.2](rfc-025-busca-semantica.md)), RFC-035 (o lockfile de modelos, por onde um checkpoint novo entra)
**Bloqueia:** nada
**Migration:** não, se a dimensão do vetor continuar 512. Sim, se mudar (§9)
**Medição:** `experiments/rfc-042-fine-tuning/measure_baseline.py` (o `.json`, com o rank de cada consulta e cada tradução, fica fora do git), rodado em 2026-11-02 nesta máquina, em CPU, na divisão de validação de `eval_set.jsonl` (§14). O nível A: `train_adapter.py` e `measure_archive.py`, de 2026-11-05 a 2026-11-07, na mesma máquina, em CPU (§16)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Os números sem marca vêm de outros RFCs e dizem de qual. As estimativas de tempo de treino, de volume de dados e de ganho esperado de §4, §6 e §7 são regras gerais de adaptação de domínio do CLIP, e não foram medidas neste projeto. Este RFC não afirma que o fine-tuning vai melhorar a busca. Ele define como saber se melhorou.

---

## 1. Contexto

O RFC-023 escolheu o `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` por velocidade e declarou o checkpoint "um ponto de partida, não um compromisso arquitetural" ([§18](rfc-023-adaptador-de-embedding-clip.md#18-fine-tuning-e-trabalho-futuro)). Deixou o fine-tuning de fora ([§17](rfc-023-adaptador-de-embedding-clip.md#17-não-objetivos)) e listou-o como "a alavanca" para a acurácia modesta ([§16.6](rfc-023-adaptador-de-embedding-clip.md#16-riscos)). O RFC-025 repetiu: a qualidade restante está no domínio ([§11.2](rfc-025-busca-semantica.md)).

Este RFC decide como o fine-tuning seria feito: de onde vêm os dados, qual método, como se mede se valeu a pena, onde o treino roda, e como o checkpoint novo entra no app sem quebrar o acervo já indexado.

## 2. Problema

Do RFC-023 ([§16](rfc-023-adaptador-de-embedding-clip.md#16-riscos)):

- 60,0% de top-1 estrito em 25 consultas contra 45 imagens.
- Fraqueza consistente em consultas de nível de rua e de paisagem urbana, em todos os modelos testados.
- A negação não funciona: "not an artificial pond" recupera o tanque (RFC-025 §11.2).
- O conjunto de avaliação é pequeno: uma consulta vale 4 pontos percentuais, e diferenças abaixo de ~8 pontos são ruído.

Este último ponto limita o próprio RFC: **o benchmark atual não decide sozinho** se um checkpoint ajustado é melhor (§5).

## 3. Decisões

### 3.1 Porta de entrada: medir antes de treinar

Antes de qualquer treino, rodar no benchmark do RFC-023, sem ajuste nenhum, os checkpoints que já foram adaptados a imagem aérea (RemoteCLIP e GeoRSCLIP, nas variantes ViT-B/32 que existirem, `TBM` licença e disponibilidade). Se algum superar o checkpoint atual por mais que o ruído, o caminho passa a ser **trocar de checkpoint** (uma entrada no lockfile do RFC-035), e o treino próprio só continua se ainda houver lacuna.

### 3.2 Método

Três níveis, do mais barato ao mais caro. Cada nível só é tentado se o anterior ficar aquém do critério de §5.3:

| nível | o que treina | dados (`TBM`) | efeito sobre vetores já indexados |
| --- | --- | --- | --- |
| A. adapter linear no lado do texto | uma matriz sobre o embedding da consulta | milhares de pares | **nenhum**: os vetores das imagens não mudam, não há reindexação |
| B. LoRA nos dois encoders | matrizes de baixo posto nas camadas de atenção | dezenas de milhares de pares | os vetores mudam: reindexação completa |
| C. ajuste completo | todos os pesos | cem mil pares ou mais | idem B, e risco maior de perder o conhecimento geral |

O nível A merece ser o primeiro: é o único que não invalida o acervo. Ele alinha o vocabulário da consulta ao espaço das imagens, mas não corrige o que o encoder de imagem não representa. C fica registrado e **não é proposto**: sem dados na ordem de cem mil pares, ele costuma degradar o zero-shot (hipótese, a ser testada se um dia houver dados).

### 3.3 Função de perda

Contrastiva simétrica (InfoNCE), a mesma do CLIP original, com o checkpoint atual como ponto de partida e taxa de aprendizado baixa (`TBM`: ordem de 1e-5 a 1e-6 para pesos completos, mais alta para LoRA). A implementação de treino do `open_clip` serve de base.

### 3.4 Supervisão

O treino é supervisionado por **pares** imagem–texto (supervisão fraca por linguagem natural), e não por rótulos de classe. Aprendizado autossupervisionado só na imagem (SimCLR, DINO, MAE) foi considerado e **não é proposto**: ele move o encoder de imagem sem mover o de texto e quebra o alinhamento de que a busca depende (§10).

## 4. Dados

### 4.1 Restrições

- **O acervo do dono do produto não é usado**, nem para treino nem para avaliação.
- Só entra imagem com licença que permita o uso, incluindo o fato de que o checkpoint resultante é distribuído no instalador (§8).
- Cada imagem tem procedência registrada (§4.4).

### 4.2 Fontes candidatas

Levantadas sem verificação de licença; a verificação é o primeiro passo da implementação.

| fonte | tipo | legenda | observação |
| --- | --- | --- | --- |
| Wikimedia Commons | aérea e drone, vistas variadas | título e descrição irregulares | fonte principal de imagens oblíquas |
| RSICD, RSITMD, UCM, Sydney captions | sensoriamento remoto, vista vertical | sim | base de vocabulário aéreo |
| OpenAerialMap | ortomosaicos de drone (CC-BY) | não | só vista vertical, cortada em recortes |
| Flickr (filtro Creative Commons), Unsplash, Pexels | fotos aéreas e de drone | irregular | licença por foto |
| USGS NAIP e ortofotos públicas | vertical | não | domínio público nos EUA |

Não entram: o Google Earth/Maps (os termos proíbem), sites que hospedam conteúdo de terceiros sem licença clara, e datasets acadêmicos restritos a pesquisa (se o uso for comercial).

### 4.3 Legendas

As legendas do Commons são metadado de apoio, e não legenda de treino: costumam vir vazias, em vários idiomas ou descrever o lugar e não o conteúdo. Para as imagens sem legenda boa:

1. Um modelo de visão e linguagem (VLM) gera a legenda descritiva (cena, elementos, ângulo, contexto), com o título do Commons como dica de contexto.
2. Uma amostra de 5 a 10% é revisada à mão (`TBM`: taxa de erro encontrada).
3. Um conjunto de legendas é escrito de propósito para as falhas do bake-off: ruas, paisagem urbana e negação.

A quantidade de legendas por sessão de trabalho depende de quem as gera (`TBM`).

### 4.4 Procedência

Uma tabela por imagem: arquivo, URL de origem, autor, licença, data do download, fonte da legenda (humana, VLM, original). A atribuição de CC-BY depende dela.

### 4.5 Volume e composição

Na faixa de partida (`TBM`, regra geral e não medida): 20 a 50 mil pares para o LoRA, 2 a 5 mil para o adapter. A composição importa mais que o volume:

- as imagens de vista vertical (RSICD e afins) não podem dominar. Acima de ~70% do conjunto, o modelo pode melhorar na vista vertical e piorar nas fotos oblíquas do produto (`TBM`);
- a proporção de oblíquas e a de legendas de negação são parâmetros do experimento.

### 4.6 Divisões

Treino, validação e teste ficam congelados antes do treino, sem vazamento entre eles (inclusive de recortes da mesma ortofoto). O teste só é aberto na avaliação final.

## 5. Avaliação

### 5.1 Conjuntos

1. **O benchmark do RFC-023** (25 consultas × 45 imagens), como comparação direta com a linha de base de 60,0%.
2. **Um conjunto de oblíquas novo**, de 500 a 1.000 imagens com licença limpa e consultas escritas à mão, grande o bastante para que diferenças de poucos pontos deixem de ser ruído (`TBM`: o tamanho que dá poder estatístico).
3. **Um conjunto de regressão** em consultas gerais (não aéreas), para detectar se o ajuste degradou o conhecimento geral do modelo.

### 5.2 Métricas

Top-1 estrito e recall@K nas consultas, nos dois idiomas do produto (português pelo tradutor, inglês direto), e o desvio dos vetores em relação ao checkpoint base. A latência de inferência é medida por segurança: com a mesma arquitetura ela não deve mudar.

### 5.3 Critério de parada

Um nível de §3.2 é aceito se, no conjunto 5.1.2 e sem piora no 5.1.3, supera o checkpoint base por uma margem maior que o ruído medido. A margem exata (`TBM`) é fixada **antes** de abrir o conjunto de teste. Se nenhum nível passa, o RFC termina com o registro do resultado negativo, que também é útil.

## 6. Treino

### 6.1 Onde roda

O treino é um processo **offline**, fora do app e fora do instalador. Roda numa máquina com GPU NVIDIA (8 GB de VRAM ou mais para LoRA em ViT-B/32, `TBM`). O produto distribui só o checkpoint resultante. Isso não conflita com o alvo de implantação (Windows nativo com PostgreSQL embarcado): nada de treino chega à máquina do usuário.

### 6.2 Tempo (estimativa, `TBM`)

| cenário | hardware | ordem de grandeza |
| --- | --- | --- |
| ~10 mil pares, ~10 épocas, LoRA | GPU de 8 a 12 GB | 10 a 30 min |
| ~100 mil pares, ~10 épocas, LoRA | mesma | 1 a 3 h |
| adapter sobre embeddings pré-calculados | CPU comum | minutos, mais o cálculo único dos embeddings |
| LoRA ou ajuste completo | CPU | inviável na prática |

O gargalo costuma ser a leitura e decodificação das imagens, e não a GPU. As imagens são redimensionadas antes do treino. O custo humano maior é legendar e revisar, e não treinar.

### 6.3 Ambiente

Python e PyTorch com CUDA compatível com o driver da máquina, `open_clip`, `peft` (para LoRA). O ponto de partida é o checkpoint já presente no lockfile do RFC-035. Versões fixas num `requirements` próprio do experimento, separado do runtime do app.

## 7. Pré-requisitos e checklist

**Na máquina de treino:** GPU NVIDIA (anotar a saída de `nvidia-smi`: modelo, VRAM, driver), 30 a 50 GB livres, CUDA compatível, Windows ou Linux.

**Antes de ir à máquina de treino:**

1. Baixar as imagens do Commons e filtrar por licença.
2. Gerar as legendas e revisar uma amostra.
3. Baixar e organizar RSICD/RSITMD, se usados, conferida a licença.
4. Congelar as divisões treino, validação e teste.
5. Escrever o script de treino e testá-lo em CPU com 50 a 100 pares, para confirmar que roda sem erro.
6. Medir o checkpoint base no benchmark e registrar o número de referência.
7. Medir RemoteCLIP/GeoRSCLIP em zero-shot (§3.1).
8. Empacotar tudo num diretório só (dados, scripts, `requirements` com versões fixas), levado em SSD, para não depender de rede lá.
9. Definir onde o checkpoint treinado é salvo e em que formato (§8).
10. Levar a lista de configurações a testar (posto do LoRA, taxa de aprendizado, épocas), para rodar várias na mesma visita; salvar checkpoints intermediários e o log de perda por época.

**Plano B:** o mesmo pacote roda em Colab ou Kaggle.

## 8. Integração no produto

1. O checkpoint sai em `safetensors`, no formato que o carregador do RFC-023 já lê. Um LoRA é fundido aos pesos base antes de entrar no app (`TBM`: equivalência numérica da fusão).
2. Entra como **uma entrada nova no lockfile do RFC-035**, com checksum, e não como uma chave de configuração: trocar de modelo é um RFC, e não uma opção ([RFC-035 §7](rfc-035-modelos-offline.md)).
3. A licença do checkpoint resultante é registrada junto com a procedência dos dados (§4.4), porque o modelo é distribuído no instalador. Se alguma fonte de dados tornar o checkpoint não distribuível, o treino não serve ao produto, mesmo que a acurácia suba.
4. O RFC-023 §6.2 (determinismo) vale para o checkpoint novo: o teste de bit a bit é refeito sobre ele.

## 9. Efeito sobre o acervo já indexado

Qualquer mudança nos pesos do encoder de imagem invalida os vetores armazenados ([RFC-023 §18](rfc-023-adaptador-de-embedding-clip.md#18-fine-tuning-e-trabalho-futuro)). Por nível:

- **A (adapter no texto):** os vetores das imagens continuam válidos. O adapter só transforma a consulta. Sem migration, sem reindexação.
- **B e C:** reindexação completa e reconstrução do índice HNSW. Sem migration de esquema enquanto a dimensão continuar 512. O plano de reindexação (versionamento do modelo no registro, o que fazer durante a migração, reaproveitamento pelo hash de conteúdo do RFC-024) **não é projetado aqui**, e pertence ao RFC que de fato trocar o modelo, como o RFC-023 já dizia.

## 10. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Ajuste completo como primeiro passo | Exige dados na ordem de cem mil pares, degrada o zero-shot e é o mais caro. |
| Autossupervisionado só na imagem (SimCLR, DINO, MAE) | Move o encoder de imagem sem o de texto e quebra o alinhamento. |
| Autossupervisionado mais um adapter de re-alinhamento com poucos pares | Mais complexo, ganho incerto. Pode ser um experimento futuro. |
| Destilação (texto congelado, imagem treinada) | Mesmo problema de alinhamento, com custo de implementação maior. |
| Treinar na máquina do usuário | Fora do alvo de implantação e sem GPU garantida. |
| Usar o acervo do dono do produto | Vedado (§4.1). |
| Trocar para SigLIP ou outro modelo | Já decidido em contrário pelo RFC-023 por velocidade. Reabrível apenas por medição. |

## 11. Riscos

1. **Dados de domínio errado.** A maior parte dos conjuntos legendados é de vista vertical; as fotos do produto são oblíquas. O ganho pode ser menor do que o esperado.
2. **Legenda de VLM herda o viés do VLM**, e o modelo ajustado aprende a imitar o VLM em vez de aprender o domínio.
3. **Conjunto de avaliação pequeno.** O benchmark do RFC-023 mede ruído. Sem o conjunto 5.1.2, qualquer conclusão é frágil.
4. **Esquecimento catastrófico.** O conjunto 5.1.3 existe para detectá-lo.
5. **Licença do checkpoint.** Dados com licença incompatível podem tornar o modelo não distribuível (§8.3).
6. **Reindexação** de acervos grandes (níveis B e C) tem custo de tempo na máquina do usuário.
7. **A negação pode não melhorar.** É propriedade do modelo, e o fine-tuning com poucas legendas de negação pode não resolvê-la.
8. **Dependência de uma máquina de terceiros** para o treino. O plano B (Colab/Kaggle) mitiga, mas exige dados não confidenciais.

## 12. Não-objetivos

- trocar o modelo base ou a arquitetura
- quantização, ONNX ou outro backend de inferência
- treino na máquina do usuário ou treino contínuo a partir do uso
- plano de reindexação e versionamento de modelo no registro (§9)
- um serviço de coleta de feedback de busca
- busca por geolocalização (RFC-032), que resolve outro problema

## 13. O que este RFC deixa para a implementação

- Verificar a licença de cada fonte de §4.2.
- Medir RemoteCLIP e GeoRSCLIP (§3.1).
- Fixar a margem de aceitação de §5.3 antes de abrir o conjunto de teste.
- Medir tempo real de treino, tamanho de dados necessário e ganho (todos os `TBM`).
- Se algum nível passar: o RFC de troca de modelo e reindexação.

## 14. Medição da porta de entrada (§3.1)

Rodada em 2026-11-02, em CPU, por `measure_baseline.py`, na divisão de **validação** do conjunto de §5.1.2: 178 consultas contra as 525 fotos de avaliação. O teste (347 consultas) não foi aberto (§4.6).

**Como.** O checkpoint atual é medido pelo próprio `ClipEmbeddingModel` do app, carregado da árvore do lockfile, com o pré-processamento, o template `a photo of {query}` e o tradutor do produto. Os candidatos rodam pelo `open_clip` (3.3.0, numa pasta à parte, sem tocar no ambiente do app), cada um com o seu pré-processamento, e leem o mesmo inglês: o tradutor do produto, chamado uma vez por consulta. Cada candidato foi tentado com o template do produto e sem template, e com as duas ativações (GELU e QuickGELU), porque as instruções dos autores divergem sobre o nome da arquitetura. As métricas são recall@1 e recall@5 (a foto certa entre as K primeiras). A diferença para o atual vem com o intervalo de 95% de um bootstrap pareado sobre as consultas.

**Conferência do script.** No benchmark do RFC-023, o atual dá 64,0% de top-1 estrito e 80,4% de pareado. O 64,0% é o que o RFC-025 já mediu pelo caminho do app (+1 consulta sobre os 60,0% do bake-off). O 80,4% é idêntico ao do bake-off. O script mede o produto.

**Resultado.** A melhor variante de cada modelo, escolhida pelo recall@5 em português na validação:

| modelo | en R@1 | en R@5 | pt R@1 | pt R@5 | pt R@1 vs atual | pt R@5 vs atual | RFC-023 estrito / pareado |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **atual**, LAION ViT-B/32 | 55,1% | 83,7% | 46,6% | 78,1% | | | 64,0% / 80,4% |
| GeoRSCLIP ViT-B/32, QuickGELU, `a photo of` | 55,1% | 80,3% | 52,2% | 73,6% | +5,6 [−1,1; +11,8] | −4,5 [−10,1; +1,1] | 52,0% / 81,5% |
| RemoteCLIP ViT-B/32, QuickGELU, sem template | 26,4% | 48,9% | 22,5% | 47,2% | −24,2 [−32,0; −16,3] | −30,9 [−40,4; −21,3] | 32,0% / 73,5% |

- **Nenhum candidato supera o atual por mais que o ruído.** O caminho de §3.1 se aplica como escrito: não há troca de checkpoint, e o treino próprio continua, a começar pelo nível A.
- **GeoRSCLIP empata.** Ganha no recall@1 em português e perde no recall@5, e nos dois casos o intervalo cruza o zero. As outras três variantes do GeoRSCLIP perdem de 4,5 a 6,7 pontos de recall@5 em português.
- **RemoteCLIP perde por 25 a 40 pontos** em todas as variantes. Foi treinado em imagens de satélite, de cima, e as fotos daqui são oblíquas. É o risco 1 de §11, medido.
- **Licenças**, que deixaram de decidir: RemoteCLIP é Apache-2.0 no GitHub; GeoRSCLIP é MIT no GitHub, mas o cartão no Hugging Face diz só "cc", sem a variante. Os pesos medidos foram `chendelong/RemoteCLIP@bf1d8a3c`, `RemoteCLIP-ViT-B-32.pt` (SHA-256 `60014e39…`), e `Zilun/GeoRSCLIP@4920188e`, `ckpt/RS5M_ViT-B-32.pt` (SHA-256 `129bafaa…`).

**O ruído, medido (`TBM` de §5.1.2).** Com 178 consultas, o intervalo de 95% da diferença entre dois modelos parecidos tem cerca de ±6 pontos, no recall@1 e no recall@5. No teste, com 347, deve ficar perto de ±4,3 (a largura cai com a raiz do número de consultas; a confirmar quando o teste abrir). É a ordem da margem de §5.3, ainda a fixar antes de abrir o teste.

**O tradutor custa mais que o checkpoint.** No modelo atual e nas mesmas fotos, a consulta em português traduzida acerta menos que a mesma consulta escrita em inglês:

| comparação, modelo atual | R@1 | R@5 | consultas |
| --- | --- | --- | --- |
| português traduzido contra inglês | −8,4 [−13,5; −3,9] | −5,6 [−10,1; −0,6] | 178 |
| termos do sul contra português padrão, traduzidos | −17,4 [−34,8; −4,3] | −30,4 [−47,8; −13,0] | 23 |

Das 178, 15 acham a foto entre as 5 primeiras em inglês e não em português. As traduções explicam: "casarão" vira *they will marry*, "corredeiras" vira *slides*, "mato" vira *I kill*, "trapiche em água barrenta" vira *barren water trap*. Uma consulta ("campos verdes divididos por cercas vivas vistos do helicóptero") não foi detectada como português e chegou ao CLIP sem tradução. O nível A, que ajusta o lado da consulta, e uma correção do tradutor atacam essa perda **sem reindexar**: nenhum dos dois muda os vetores das imagens.

**Por cena** (recall@5 do atual, em português; o rótulo vem da primeira palavra de cena da consulta, §15): água 84,1% (44 consultas), construído 74,0% (104), campo 83,3% (30). Consultas que falam de água: 82,1% (78); as que não falam: 75,0% (100). A cena construída é a mais fraca, como o RFC-023 já tinha visto nas consultas de rua e de paisagem urbana.

> **Nota (RFC-047, 2026-11-04): a linha de base do nível A mudou.** O [RFC-047](rfc-047-glossario-e-deteccao-na-traducao-das-consultas.md) pôs um léxico e marcadores de português antes do tradutor. Medido por este `measure_baseline.py`, na validação, com o checkpoint atual e o léxico final do RFC-047 (§17): português R@1 **49,4%** (era 46,6) e R@5 **79,2%** (era 78,1); termos do sul R@1 **43,5%** (era 30,4) e R@5 **78,3%** (era 65,2); inglês sem mudança (55,1% / 83,7%). A perda do português contra o inglês caiu para −5,6 [−10,1; −1,7] no R@1 e −4,5 [−9,0; 0,0] no R@5. São esses os números contra os quais o nível A se mede (`baseline-validation-20261104-001130.json`). Os ganhos na validação são otimistas pelo viés que o RFC-047 §4.4 descreve, e mais ainda porque a segunda rodada do léxico foi feita depois de ver a validação (RFC-047 §17.1); o teste continua fechado, e a avaliação final deste RFC já vai incluir aquele tradutor.

## 15. Correções feitas durante a implementação

O texto acima fica como foi proposto. O que a implementação mudou ou mediu está aqui, com a seção de origem.

1. **§2 e §5.1.1: a linha de base do benchmark do RFC-023 é 64,0%, não 60,0%.** Os 60,0% são do bake-off. Pelo caminho do app, o mesmo checkpoint dá 64,0%, como o RFC-025 já registrou, e foi o que §14 reproduziu. A comparação deste RFC é contra 64,0%.
2. **§7, item 8: o pacote não vai em SSD.** O único SSD disponível é o interno do notebook de desenvolvimento. O pacote passa a ser baixado na máquina de treino: o código por `git clone`; os dados como shards WebDataset num dataset privado do Hugging Face, fixado por commit; o checkpoint base por `scripts/vendor_models.py`; e o PyTorch com CUDA escolhido pela saída de `nvidia-smi`. Voltam só os adaptadores LoRA e os logs; a fusão e a avaliação rodam em casa, em CPU. Um pendrive com o mesmo pacote é o plano B. Ainda não executado.
3. **§4.1 e §4.2: Commons verificado foto a foto, sem CC BY-SA por enquanto.** `collect_commons.py` lê a licença de cada arquivo e grava a procedência em `provenance.csv` (§4.4). A descoberta achou 83.600 fotos, das quais 69.095 são CC BY-SA, e a pergunta de §8.3 continua sem resposta. Sem elas, e com os filtros (de 1990 em diante, sem tons de cinza, proporção até 2:1, lado menor de pelo menos 224 px, no máximo 5 fotos por autor e dia), sobram **7.652** fotos aceitas, todas baixadas em 500 px (~72 KB cada, 565 MB). Com BY-SA seriam 30.379. O volume serve ao nível A (2 a 5 mil pares, §4.5), não ao B (20 a 50 mil).
4. **§4.3, `TBM` "taxa de erro encontrada": 5,1%.** O Claude (Sonnet 5.5) escreveu 216 legendas a partir das fotos, e o usuário revisou todas, não só 5 a 10%: 205 aprovadas, 11 corrigidas, nenhuma rejeitada. A revisão levou mediana de 16 s por legenda (média de 29 s); uma legenda escrita do zero levou ~65 s. As legendas das outras ~7,4 mil fotos ainda não foram geradas.
5. **§5.1.2: 525 fotos, cada consulta revisada à mão.** Uma foto por série (autor e dia), e nenhuma de série que tenha legenda de treino, para que o mesmo voo não apareça dos dois lados. O Claude escreveu os rascunhos das consultas em português a partir das fotos, e o usuário corrigiu cada um na página de revisão: 14 escritas do zero e 511 a partir de rascunho, das quais 359 aprovadas sem mudança (70,3%) e 152 editadas. Os rascunhos refeitos olhando as fotos de novo tiveram 61 de 65 aprovados. 66 consultas têm uma variante com termos do sul do Brasil, para medir o tradutor nesse vocabulário. As versões em inglês (§5.2: "nos dois idiomas") foram escritas pelo Claude a partir do português e conferidas por amostra.
6. **§4.6: as divisões estão congeladas em `eval_set.jsonl`** por `freeze_eval_set.py`. Um terço das consultas é validação (178) e o resto é teste (347), sorteados pela ordem `spread` do coletor dentro de cada estrato (tipo de cena e presença da variante do sul). Validação e teste usam a mesma galeria, as 525 fotos. O teste só abre com `--open-test`, e a primeira abertura fica registrada. O tipo de cena é um rótulo grosso, para ler resultados em separado, e não uma verdade: a primeira palavra da consulta que nomeia uma cena decide (água, construído, campo).
7. **§5.1.3, o conjunto de regressão, ainda não existe.** Ele não pesa na decisão de §3.1, que não treina nada, mas é pré-requisito do critério de §5.3 para qualquer nível.
8. **§5.1.2: o conjunto que decide passou a ser o acervo real do usuário** (§16.1). O usuário achou as fotos do Commons diferentes das suas, que são oblíquas próximas de propriedades em Santa Catarina. O conjunto do Commons seguiu como verificação de regressão em fotos aéreas, e o teste dele (347 consultas) continua fechado.
9. **§5.1.3: o conjunto de regressão existe.** São 1.000 fotos da validação do COCO 2017, com as cinco legendas de cada uma (`fetch_coco.py`), usadas só para medir (§16.2).
10. **§5.3: a margem foi fixada antes da medição, como uma regra** (§16.4): o R@5 e o MRR sobem com o intervalo acima de zero no conjunto escrito só pelo usuário, e o COCO não cai mais que ~1 ponto.
11. **§4.2 e §4.3: os pares de treino são do acervo real**, e não do Commons: 7.302 buscas escritas pelo Claude para 2.434 fotos de drone do usuário, de 2018 (§16.2).
12. **§8: o nível A não entra pelo lockfile.** A matriz fica no código, ao lado do léxico do RFC-047 (§16.5).

## 16. Nível A: o acervo real, o treino e a entrada no produto

As fotos do acervo e as consultas sobre elas mostram lugares privados. Ficam fora do repositório, numa pasta do usuário, e este texto registra só contagens e números. Os scripts estão em `experiments/rfc-042-fine-tuning/`.

### 16.1 O conjunto de decisão: o acervo real

- **As fotos.** Uma foto por dia de voo do HD do usuário, de 2010 em diante e fora de 2018, o ano do treino (`sample_archive.py`), em miniaturas de 1.600 px. São 300 fotos. Quatro foram tiradas do chão e uma ficou sem consulta, e as cinco ficaram de fora.
- **As consultas, 295 em português.** O usuário escreveu 32 do zero. As outras são rascunhos que o Claude escreveu olhando as fotos, na sessão, e que o usuário revisou: 117 aprovados como estavam e 146 editados. A origem fica registrada em cada consulta.
- **O congelamento** (`freeze_archive_set.py`). Um terço vai para a validação (100) e o resto para o teste (195). O sorteio é feito dentro de cada estrato de origem × "a até 300 m de uma foto de treino" (8 fotos na validação e 15 no teste estão nessa distância). Validação e teste compartilham uma galeria, as 295 fotos.
- **A linha de base do produto, na validação:** R@1 18,0%, R@5 46,0%, R@10 56,0%, MRR 31,8%. Depois das quatro formas do RFC-047 §18, ficou em R@1 18,0%, R@5 49,0% e R@10 62,0%.

### 16.2 Os dados de treino e a regressão

- **Fotos.** O usuário separou todo o ano de 2018: 2.474 fotos de drone. As 44 de celular ficaram de fora. Foram usadas em miniaturas de 768 px (`archive_training.py`).
- **Buscas.** O Claude (Sonnet 5.5, pela API de lotes) escreveu, para cada foto, três buscas em português: uma curta, uma média e uma longa, com medianas de 4, 10 e 16 palavras.
  - O prompt pede palavras do dia a dia, "no fundo" em vez de "ao fundo" e nada de direita e esquerda. Foi assim que o usuário corrigiu os rascunhos de §15.5.
  - O Claude pulou 39 fotos, tiradas do chão, e uma resposta veio com erro. Ficaram 7.302 pares em 2.434 fotos.
  - Custo: US$ 4,03.
- **Como entram no treino.** Cada busca passa pelo tradutor e pelo template do produto, como uma busca de verdade. Em cada época, o treino sorteia uma das três buscas de cada foto.
- **Procedência (§4.4).** As fotos são do próprio usuário, e os textos foram escritos pelo Claude. Nada disso sai da máquina; só a matriz treinada sai (§16.5).
- **Regressão (§5.1.3).** O conjunto é o COCO de §15.9: 5.000 legendas escritas por pessoas, cada uma buscada contra as 1.000 fotos.

### 16.3 O treino e a escolha, na validação

A matriz é treinada com a perda contrastiva simétrica, temperatura 100, lotes de 256, sobre os vetores das fotos e das buscas guardados em cache. Leva cerca de 1 s em CPU.

- **Os parâmetros iniciais decoraram o treino** (20 épocas, taxa de aprendizado 1e-3, decaimento de pesos 1e-3). A matriz saiu com norma 8,25, e tudo piorou:
  - acervo, R@1: 18,0 → 14,0%;
  - Commons em português, R@1: 49,4 → 42,1% (−7,3 [−14,0; −1,1]);
  - COCO, R@1: 58,5 → 52,9% (−5,6 [−6,6; −4,6]).
- **Uma grade de 18 configurações**, escolhida na validação do acervo e portanto depois de ver os números: épocas 1, 3 ou 10; taxa 1e-4, 3e-4 ou 1e-3; decaimento 1e-3 ou 1e-1.
  - O R@1 do acervo variou de −1,0 a +7,0 pontos, e o do COCO, de −3,4 a −0,2.
  - A escolhida foi **10 épocas, taxa 1e-4 e decaimento 1e-3**, com norma 1,41.

| Validação | Produto | Matriz | Diferença [IC 95%] |
|---|---|---|---|
| Acervo, R@1 (100) | 18,0% | 25,0% | +7,0 [0,0; +15,0] |
| Acervo, R@5 | 49,0% | 53,0% | +4,0 [−3,0; +11,0] |
| Commons pt, R@1 (178) | 49,4% | 50,6% | +1,1 [−2,8; +5,1] |
| Commons pt, R@5 | 79,2% | 80,9% | +1,7 [−2,2; +5,6] |
| Commons en, R@5 | 83,7% | 86,5% | +2,8 [−1,7; +7,3] |
| COCO, R@1 (5.000) | 58,5% | 57,9% | −0,6 [−1,2; 0,0] |
| COCO, R@5 | 85,1% | 84,3% | −0,7 [−1,2; −0,3] |

**A suspeita.** Na validação do acervo, as 11 consultas do usuário se moveram assim: 2 subiram, 8 caíram, e o R@5 foi de 45,5% para 18,2%. Os rascunhos do Claude tiveram 48 subindo e 18 caindo (Fisher, p ≈ 0,004). As legendas do COCO, escritas por pessoas, também caíram um pouco. A hipótese era que a matriz tivesse aprendido o jeito de escrever do modelo, que escreveu as buscas de treino e os rascunhos, e não o do usuário. Com essas 11 consultas, o conjunto não servia para decidir.

### 16.4 O conjunto só do usuário e o teste

**A regra, fixada antes da medição** (a margem de §5.3):
- a matriz já escolhida é medida uma única vez, sem nenhum ajuste, num conjunto novo de consultas escritas só pelo usuário;
- é aceita se o R@5 e o MRR sobem, com o intervalo acima de zero, e o COCO não cai mais que ~1 ponto;
- se for aceita, o teste do acervo é aberto uma única vez.

**O conjunto.** São 80 dias de voo novos, fora dos 300 de §16.1 e de 2018. O usuário escreveu as consultas do zero, sem rascunho: 78, porque duas fotos ficaram sem consulta. A galeria é a dessas 78 fotos, e sete delas estão a até 300 m de uma foto de treino. Congelado inteiro como teste (`freeze_archive_set.py --one-split test`).

| Só do usuário (78 × 78) | Produto | Matriz | Diferença [IC 95%] |
|---|---|---|---|
| R@1 | 28,2% | 32,1% | +3,8 [−2,6; +10,3] |
| R@5 | 50,0% | **60,3%** | **+10,3 [+2,6; +17,9]** |
| R@10 | 69,2% | 73,1% | +3,8 |
| MRR | 40,8% | 44,0% | +3,2 [−1,2; +8,0] |

- 34 consultas subiram, 19 caíram e 25 ficaram onde estavam.
- Longe das fotos de treino (71 consultas), o R@5 sobe +11,3 [+2,8; +19,7]: o ganho não vem de reconhecer lugares do treino.
- **A regra foi lida como cumprida pelo R@5.** O MRR subiu, mas o intervalo dele cruza zero. A ferramenta de medição só dá intervalo para R@1 e R@5; o do MRR foi calculado depois, pelo mesmo bootstrap pareado. Numa leitura estrita, que exija intervalo acima de zero também no MRR, a regra não passa.

| Teste do acervo, aberto uma vez (195 × 295) | Produto | Matriz | Diferença [IC 95%] |
|---|---|---|---|
| R@1 | 21,0% | 26,7% | +5,6 [+1,5; +10,3] |
| R@5 | 45,1% | 50,3% | +5,1 [0,0; +10,8] |
| R@10 | 56,9% | 63,1% | +6,2 |
| MRR | 32,4% | 38,4% | +6,0 [+3,0; +9,2] |

- No teste, 107 consultas subiram e 41 caíram.
- Por origem, o R@5:
  - rascunhos aprovados (77): +6,5 [0,0; +13,0];
  - rascunhos editados (97): +7,2 [−1,0; +15,5];
  - consultas do usuário (21): 6 subiram e 8 caíram, −9,5 [−28,6; +9,5].

**As consultas do usuário, todas juntas** (uma leitura feita depois de ver os números):
- As 32 escritas antes de existir qualquer rascunho (11 da validação e 21 do teste) tiveram 8 subindo e 16 caindo. Ano e câmera não explicam a diferença em relação às 78 novas.
- Juntando as consultas do usuário que nunca serviram para escolher nada (as 78 novas e as 21 do teste): R@5 +6,1 [−1,0; +13,1], MRR +3,2 [−0,9; +7,4], 40 subindo e 27 caindo.
- **Leitura:** o ganho é claro no conjunto e nos rascunhos. No que o usuário digita, é menor e incerto, provavelmente positivo.

**O benchmark do RFC-023 (§5.1.1)**, as 25 consultas contra as 45 fotos, ranqueadas contra os vetores de referência do RFC-035:
- R@5: 84,0 → 88,0%;
- top-1 estrito: 64,0 → 68,0%;
- ordem entre a foto certa e a quase-certa: 80,4 → 77,8%;
- quase-certas entre as 5 primeiras: 12,8 → 11,2%.

Para "a rural property with a lake", `lake_property_01` caiu do 4º para o 13º lugar. O teste de ponta a ponta que exigia essa foto entre as 5 primeiras (`test_a_full_sentence_portuguese_query_finds_the_same_images`) passou a exigir o que diz checar, que o tradutor está na busca: o português e o inglês trazem as mesmas 5 fotos. A consulta mandada sem tradução divide só 3 das 5 com a inglesa, e a sobreposição de 3 que o teste aceitava antes não pegaria esse caso.

**§5.2.** A matriz move pouco cada consulta. O cosseno mediano entre o vetor do CLIP e o ajustado é 0,972 nas 78 consultas do usuário (mínimo 0,945) e 0,965 nas 295 do acervo (mínimo 0,934). Ela acrescenta uma mediana de 62 µs por busca (p95 139 µs), contra 170 a 220 ms de uma busca inteira no app.

**A decisão.** Em 2026-11-07, o usuário testou o app de desenvolvimento com e sem a matriz e decidiu levá-la ao produto.

### 16.5 A entrada no produto (correções de §8)

1. **§8.2: não é uma entrada do lockfile.** A matriz (512 × 512, float32, 1 MB) fica em `backend/app/infrastructure/ai/query_adapter.safetensors`, ao lado do léxico. É versionada com o código e lida como dado do pacote (`query_adapter.py`). O lockfile do RFC-035 só conhece modelos baixados do Hugging Face, e não se quis publicar uma matriz tirada das fotos do usuário. O cabeçalho do arquivo registra:
   - o repositório e a revisão do CLIP. Com outro CLIP, a carga recusa a matriz;
   - o sha256 do léxico. Com outro léxico, um teste falha;
   - os hiperparâmetros, o número de pares e o sha256 da própria matriz.

   O cabeçalho não tem nenhum caminho nem consulta. `export_adapter.py` grava o arquivo a partir de um treino.
2. **§8.1:** o arquivo é safetensors, como pedido. Não há fusão de LoRA no nível A.
3. **§8.3:** a procedência é a de §16.2: as fotos são do usuário e os textos foram escritos pelo Claude. A matriz é distribuída com o app.
4. **§8.4, determinismo.** O teste de referência do RFC-035 compara o caminho inteiro do app com o vetor gravado passado pela matriz, uma linha por vez, como numa busca, bit a bit. O arquivo gravado não é reescrito, como no RFC-047 §8. `scripts/check_package.py` faz o mesmo.
5. **Onde a matriz entra.** `ClipEmbeddingModel.encode_text` calcula `normalize(t + A t)` depois da normalização. Os vetores das imagens, a busca por foto e "semelhantes" não passam pela matriz. Não há migration nem reindexação (§9).
6. **O que a matriz amarra.** Ela foi treinada com as traduções do léxico e do tradutor de hoje.
   - Mudar qualquer um dos dois faz `test_it_was_trained_with_this_lexicon` falhar até que a matriz seja treinada de novo (`train_adapter.py`), medida (validação do acervo e COCO) e gravada (`export_adapter.py`).
   - Trocar o CLIP (níveis B e C) invalida a matriz.
   - Os conjuntos limpos já foram gastos: o teste do acervo está aberto e o conjunto do usuário foi medido. Um novo treino com a mesma receita é conferido na validação e no COCO. Uma receita nova precisa de um conjunto limpo novo.
