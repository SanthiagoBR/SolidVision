# Suíte do backend no ambiente da GPU

Máquina e ambiente: [`machine.json`](machine.json). Branch `frontend`, commit `ac4df0a`.

```
pytest -m "not embedded"        # inclui os `slow`, com os modelos reais na GPU
```

dentro de `python -m app.infrastructure.database.embedded run --`, com
`DATABASE_SOURCE=embedded` e `WARM_UP_MODELS=false`.

**Resultado bruto:** 4.200 passaram e 18 falharam, em 6min39s ([`pytest-gpu.xml`](pytest-gpu.xml), com a mensagem de cada falha).

**Depois de corrigir o `SUPPORTED_EXTENSIONS`:** os 18 ([`pytest-gpu-failed.txt`](pytest-gpu-failed.txt)) foram rodados de novo e 10 passaram.

Os logs completos (`pytest-gpu.log`, `pytest-gpu-rerun.log`) ficaram na máquina que mediu: o `.gitignore` da raiz exclui `*.log`.

## As 18 falhas, por causa

| # | Causa | Testes | Situação |
|---|---|---|---|
| 10 | **`.env` desatualizado nesta máquina** | `test_exif_backfill` (2), `test_indexing_worker` CLI (2), `test_thumbnail_backfill` (3), `test_thumbnails_stay_out_of_the_collection` (2), `test_launcher_slow` (1) | Corrigido por variável de ambiente; passam |
| 2 | Torch fora do lock (`+cu126` no lugar de `+cpu`) | `test_vendor_python` (2) | Esperado: é o desvio proposital para usar a GPU |
| 5 | Vetores da GPU ≠ referências da CPU além de `1e-5` | `test_clip_embedding_model_slow` (4), `test_reference_embeddings_slow` (1) | Esperado na GPU; o efeito na busca é medido no benchmark e no passo `demo` |
| 1 | Ordem dos testes | `test_dependencies::test_resolving_the_embedding_model_downloads_nothing` | Só falha com os `slow` antes dele, que já carregaram o modelo compartilhado; passa isolado, e o CI não roda `slow` |

### 1. O `.env` desatualizado (o achado que importa)

O `.env` desta máquina tem `SUPPORTED_EXTENSIONS` no formato JSON antigo,
`[".jpg", ".jpeg", ...]`. A branch espera uma lista separada por vírgulas
(`.env.example`, linha 56: `.jpg,.jpeg,.png,.tiff,.bmp,.webp`). O
`parse_supported_extensions` corta a string nas vírgulas e devolve
`('[".jpg"', '".jpeg"', ...)`. **Nenhuma extensão casa**, e todo scan acha 0
fotos sem erro nenhum: o job termina como `completed`, com `0 indexed`.

Sem a correção, a indexação do acervo teria terminado em segundos, "com
sucesso", e vazia.

O mesmo `.env` traz `ENVIRONMENT` e `EMBEDDING_MODEL`, que a configuração avisa
estarem aposentados.

- **Nesta medição:** `SUPPORTED_EXTENSIONS=.jpg,.jpeg,.png,.tiff,.bmp,.webp`
  passado como variável de ambiente em todos os comandos. Ela tem precedência
  sobre o `.env`, e o arquivo não foi tocado.
- **Correção na máquina:** trocar a linha do `.env` pela do `.env.example`, ou
  apagá-la, já que o default é o mesmo.
- **Sugestão para o projeto:** o `validate_supported_extensions` deveria recusar
  uma entrada que não comece com `.`, ou aceitar a forma JSON. Uma configuração
  que zera o scan em silêncio é exatamente o tipo de falha que o RFC-046
  tirou da listagem.

### 3. Vetores na GPU

As referências do RFC-035 foram calculadas em CPU, e os testes exigem
diferença máxima de `1e-5` por componente. Na GPU:

- lote contra uma imagem por vez: diferença máxima de `2,4e-5`, em 20 de 512 componentes;
- as 45 imagens de referência contra os vetores salvos: diferença máxima de `1,3e-3`.

A ordem de grandeza é a esperada para kernels diferentes (cuDNN/cuBLAS) em
float32. A pergunta que importa é se isso muda o ranking. O
`benchmark-indexing-*.json` (equivalência entre lotes) e o passo `demo` do
`read-ahead-gpu.json` (Recall@5 e top-1 com o adaptador de produção) respondem
a ela.
