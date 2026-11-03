# RFC-042 — Fine-tuning do CLIP

**Status:** Em implementação. A porta de entrada de §3.1 foi medida (§14): nenhum checkpoint aéreo supera o atual, e o treino próprio segue. As fotos do Commons estão baixadas e o conjunto de §5.1.2 está congelado; faltam as legendas de treino e o treino (§15)
**Depende de:** RFC-023 (o adaptador de embedding, o benchmark do bake-off e [§18](rfc-023-adaptador-de-embedding-clip.md#18-fine-tuning-e-trabalho-futuro), que adiou este assunto), RFC-025 (a busca semântica e a linha de base de [§11.2](rfc-025-busca-semantica.md)), RFC-035 (o lockfile de modelos, por onde um checkpoint novo entra)
**Bloqueia:** nada
**Migration:** não, se a dimensão do vetor continuar 512. Sim, se mudar (§9)
**Medição:** `experiments/rfc-042-fine-tuning/measure_baseline.py` (o `.json`, com o rank de cada consulta e cada tradução, fica fora do git), rodado em 2026-11-02 nesta máquina, em CPU, na divisão de validação de `eval_set.jsonl` (§14)

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

## 15. Correções feitas durante a implementação

O texto acima fica como foi proposto. O que a implementação mudou ou mediu está aqui, com a seção de origem.

1. **§2 e §5.1.1: a linha de base do benchmark do RFC-023 é 64,0%, não 60,0%.** Os 60,0% são do bake-off. Pelo caminho do app, o mesmo checkpoint dá 64,0%, como o RFC-025 já registrou, e foi o que §14 reproduziu. A comparação deste RFC é contra 64,0%.
2. **§7, item 8: o pacote não vai em SSD.** O único SSD disponível é o interno do notebook de desenvolvimento. O pacote passa a ser baixado na máquina de treino: o código por `git clone`; os dados como shards WebDataset num dataset privado do Hugging Face, fixado por commit; o checkpoint base por `scripts/vendor_models.py`; e o PyTorch com CUDA escolhido pela saída de `nvidia-smi`. Voltam só os adaptadores LoRA e os logs; a fusão e a avaliação rodam em casa, em CPU. Um pendrive com o mesmo pacote é o plano B. Ainda não executado.
3. **§4.1 e §4.2: Commons verificado foto a foto, sem CC BY-SA por enquanto.** `collect_commons.py` lê a licença de cada arquivo e grava a procedência em `provenance.csv` (§4.4). A descoberta achou 83.600 fotos, das quais 69.095 são CC BY-SA, e a pergunta de §8.3 continua sem resposta. Sem elas, e com os filtros (de 1990 em diante, sem tons de cinza, proporção até 2:1, lado menor de pelo menos 224 px, no máximo 5 fotos por autor e dia), sobram **7.652** fotos aceitas, todas baixadas em 500 px (~72 KB cada, 565 MB). Com BY-SA seriam 30.379. O volume serve ao nível A (2 a 5 mil pares, §4.5), não ao B (20 a 50 mil).
4. **§4.3, `TBM` "taxa de erro encontrada": 5,1%.** O Claude (Sonnet 5.5) escreveu 216 legendas a partir das fotos, e o usuário revisou todas, não só 5 a 10%: 205 aprovadas, 11 corrigidas, nenhuma rejeitada. A revisão levou mediana de 16 s por legenda (média de 29 s); uma legenda escrita do zero levou ~65 s. As legendas das outras ~7,4 mil fotos ainda não foram geradas.
5. **§5.1.2: 525 fotos, cada consulta revisada à mão.** Uma foto por série (autor e dia), e nenhuma de série que tenha legenda de treino, para que o mesmo voo não apareça dos dois lados. O Claude escreveu os rascunhos das consultas em português a partir das fotos, e o usuário corrigiu cada um na página de revisão: 14 escritas do zero e 511 a partir de rascunho, das quais 359 aprovadas sem mudança (70,3%) e 152 editadas. Os rascunhos refeitos olhando as fotos de novo tiveram 61 de 65 aprovados. 66 consultas têm uma variante com termos do sul do Brasil, para medir o tradutor nesse vocabulário. As versões em inglês (§5.2: "nos dois idiomas") foram escritas pelo Claude a partir do português e conferidas por amostra.
6. **§4.6: as divisões estão congeladas em `eval_set.jsonl`** por `freeze_eval_set.py`. Um terço das consultas é validação (178) e o resto é teste (347), sorteados pela ordem `spread` do coletor dentro de cada estrato (tipo de cena e presença da variante do sul). Validação e teste usam a mesma galeria, as 525 fotos. O teste só abre com `--open-test`, e a primeira abertura fica registrada. O tipo de cena é um rótulo grosso, para ler resultados em separado, e não uma verdade: a primeira palavra da consulta que nomeia uma cena decide (água, construído, campo).
7. **§5.1.3, o conjunto de regressão, ainda não existe.** Ele não pesa na decisão de §3.1, que não treina nada, mas é pré-requisito do critério de §5.3 para qualquer nível.
