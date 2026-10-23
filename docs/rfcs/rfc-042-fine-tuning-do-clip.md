# RFC-042 — Fine-tuning do CLIP

**Status:** Proposto. Rascunho para ser desenvolvido depois que os RFCs em andamento forem fechados. Nada aqui foi medido ou implementado
**Depende de:** RFC-023 (o adaptador de embedding, o benchmark do bake-off e [§18](rfc-023-adaptador-de-embedding-clip.md#18-fine-tuning-e-trabalho-futuro), que adiou este assunto), RFC-025 (a busca semântica e a linha de base de [§11.2](rfc-025-busca-semantica.md)), RFC-035 (o lockfile de modelos, por onde um checkpoint novo entra)
**Bloqueia:** nada
**Migration:** não, se a dimensão do vetor continuar 512. Sim, se mudar (§9)
**Medição:** nenhuma ainda

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
