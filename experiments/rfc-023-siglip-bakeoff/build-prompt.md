# Prompt: implementar o RFC-023 (adapter CLIP para SolidVision)

Use este texto como prompt para pedir a um agente que implemente o
RFC-023 de verdade em `backend/app/`, depois de todo o bake-off em
`experiments/rfc-023-siglip-bakeoff/` (branch `explore/siglip-bakeoff`).
Ver `README.md` nesta pasta, seção "Decisão final", para os números que
sustentam cada escolha abaixo.

---

## Contexto

RFC-023 começou como "adapter SigLIP" mas, depois do bake-off completo
(SigLIP2 vs. CLIP vs. jina-clip-v2, em várias dimensões, com e sem
tradução, com e sem template de consulta), a decisão final é usar CLIP,
não SigLIP. Implemente exatamente a configuração abaixo -- essas decisões
já foram tomadas com dados reais, não é para reabrir a escolha de modelo
durante a implementação.

## Decisão técnica (não reabrir)

- **Modelo:** `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`, 512 dimensões.
- **Template de consulta de texto:** `"a photo of {query}"`, aplicado
  antes de codificar qualquer texto de busca. Não usar
  `"an aerial photo of {query}"` -- piorou este modelo em todos os testes
  (ver `clip_template_bakeoff.py` / `clip_template_bakeoff_results.json`).
- **Suporte a português:** traduzir a consulta para inglês antes de
  codificar, via `Helsinki-NLP/opus-mt-ROMANCE-en` (tag `>>por<<` no
  início do texto, arquitetura MarianMT, `AutoTokenizer` +
  `AutoModelForSeq2SeqLM`, não o helper `pipeline()` -- esse modelo não
  registra sob a task genérica `"translation"`). O modelo
  `Helsinki-NLP/opus-mt-pt-en` (par direto PT-EN) não existe mais no Hub;
  isso já foi verificado, não precisa checar de novo.

## O que precisa mudar no código real

1. **Novo adapter** em `backend/app/infrastructure/ai/`, implementando
   `EmbeddingModelPort` (`encode_image`, `encode_text` --
   ver `backend/app/domain/services/embedding_model_port.py`).
   - Carregar o modelo via `transformers.AutoModel` /
     `AutoProcessor.from_pretrained(...)`. Nesta versão do `transformers`
     usada no projeto, `get_image_features` / `get_text_features` do
     `CLIPModel` retornam `BaseModelOutputWithPooling` -- o embedding é
     `output.pooler_output`, não o retorno direto da chamada. Isso já foi
     verificado lendo o source do `transformers` instalado, não assumido.
   - `encode_text` deve: (a) detectar se o texto está em português ou
     inglês -- **isso ainda não foi decidido, ver seção abaixo** -- (b) se
     português, traduzir com o `opus-mt-ROMANCE-en` antes de seguir; (c)
     aplicar o template `"a photo of {query}"`; (d) codificar com o CLIP.
   - `encode_image` não usa template nem tradução -- só o CLIP direto.
   - `FakeEmbeddingModel` continua existindo (é usado como test double em
     testes automatizados) -- não remover, só adicionar o adapter real ao
     lado dele.

2. **`backend/app/infrastructure/config/settings.py`:**
   - `embedding_model` -> `"laion/CLIP-ViT-B-32-laion2B-s34B-b79K"` (hoje
     está em `"google/siglip-base-patch16-224"`, que já estava
     desatualizado mesmo antes desta decisão -- era SigLIP v1, nunca foi
     atualizado para SigLIP2).
   - `embedding_dimension` -> `512` (hoje está em `1152`).

3. **Nova migração Alembic**, alterando `images.embedding` de
   `Vector(1152)` para `Vector(512)` e recriando o índice
   `ix_images_embedding_hnsw` na nova dimensão (ver
   `backend/alembic/versions/999b801e80f4_add_embedding_vector_and_hnsw_index.py`
   para o padrão da migração original). Como ainda não existe nenhum
   embedding real em produção -- só o `FakeEmbeddingModel` foi usado até
   aqui -- a migração não precisa preservar/re-embedar dados existentes.

4. **`backend/requirements.txt`** (o de verdade da aplicação, não o
   `experiments/rfc-023-siglip-bakeoff/requirements.txt` do bake-off):
   adicionar `transformers`, `torch` (build CPU), `sentencepiece`. Ao
   contrário das ferramentas de exploração, essas passam a ser
   dependências reais da aplicação a partir de agora.

## Decisão em aberto que este prompt não resolve

**Como decidir se uma consulta está em português ou inglês antes de
traduzir?** O bake-off nunca testou o que acontece se um texto em inglês
for passado pelo `opus-mt-ROMANCE-en` -- não dá pra assumir que "traduzir
sempre, mesmo o que já é inglês" é seguro sem verificar isso primeiro.
Resolva isso como parte da implementação (biblioteca de detecção de
idioma, heurística simples, ou expor a escolha pro usuário) e documente a
decisão no RFC.

## Fora de escopo para este RFC -- não implementar aqui

- **Busca vetorial de verdade.** Hoje `SearchImagesUseCase` calcula o
  embedding da consulta e descarta, retornando
  `repository.list()` sem nenhum ranqueamento; `ImageRepository` não tem
  método de busca por similaridade, mesmo com o índice HNSW já existindo
  no banco. Isso é um gap real e separado do adapter em si (ver
  ARCHITECTURE.md §10 -- `EmbeddingModelPort` é só o contrato de
  `encode_image`/`encode_text`). Trate como uma tarefa própria, a menos
  que o usuário peça explicitamente para incluir aqui.
- **Fine-tuning do modelo no domínio.** Esperado como trabalho futuro,
  mas não agora -- o corpus de demonstração (45 imagens, RFC-022) é
  pequeno demais pra fine-tuning sem risco sério de overfitting. Deixe
  isso documentado como trabalho futuro no RFC, não como parte desta
  implementação.

## Onde documentar

Escrever o RFC-023 de verdade em `docs/rfcs/`, seguindo o formato de
`docs/rfcs/rfc-022-demo-dataset.md`, citando os números do bake-off
(`experiments/rfc-023-siglip-bakeoff/README.md`). Fazer cherry-pick
apenas dos commits necessários para `develop` -- não mergear a branch
`explore/siglip-bakeoff` inteira (ver o próprio histórico da branch para
o padrão já seguido nas correções que já foram promovidas).
