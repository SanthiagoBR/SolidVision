# RFC-030 — Acesso ao Arquivo: caminho, thumbnail e revelação

**Status:** Proposto
**Depende de:** RFC-025 (busca), RFC-026 (camada HTTP), RFC-027 (dispositivos e ponto de montagem), RFC-028 (data de captura), RFC-029 (jobs)
**Migration:** sim — `images.thumbnail_path`
**Medição:** `experiments/rfc-030-file-access/` — `TBM`

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` é *a medir*. Nada neste documento foi medido ainda.
>
> **Sobre a numeração.** A docstring de `search_schema.py`, escrita pelo RFC-026, promete *"o `GET /images/{id}` que o RFC-027 vai acrescentar"*. O RFC-027 acabou sendo dispositivos e identidade de volume, porque a janela para reescrever a chave primária barato estava fechando (RFC-027 §6.2). Este é o RFC que cumpre aquela promessa, três números depois. O registro fica aqui em vez de a docstring ser corrigida em silêncio, seguindo a lição do RFC-012.

---

## 1. Contexto

O RFC-026 decidiu, deliberadamente, não devolver o caminho do arquivo:

> **`search_schema.py`:** *"`path` está deliberadamente ausente ainda que `SearchHit.image` carregue um. Publicá-lo vazaria o layout do sistema de arquivos do servidor para todo chamador, e entregaria aos clientes um identificador que muda sempre que um arquivo se move."*

O contra-argumento chegou como uma frase, e a frase está certa:

> *"Mostrar o caminho bruto é basicamente o coração do projeto. Não faz o menor sentido fazer todo um projeto de encontrar arquivos se não ajudar o usuário a acessar ele no final do processo."*

O `ARCHITECTURE.md` §1 descreve o sistema como *"recuperação de imagens"*. Recuperação que termina em um `filename` sem extensão e um UUID não recuperou nada: o usuário sabe que a foto existe e continua sem saber onde ela está. O último passo do fluxo está faltando, e ele é o passo que o produto promete.

## 2. Problema

O RFC-026 deu **dois** argumentos contra publicar o caminho. Eles têm destinos diferentes, e tratá-los como um só é o erro que este RFC precisa evitar.

### 2.1 Primeiro argumento: dissolve

> *"Vazaria o layout do sistema de arquivos do servidor para todo chamador."*

Correto para um servidor remoto multi-inquilino. O SolidVision não é um. O `README.md` define a premissa: *"os arquivos permanecem em seus locais originais sem necessidade de upload"*. O `ARCHITECTURE.md` §2 descreve um usuário indexando os próprios discos. E o próprio código já sabe disso — `routers/images.py`, ao justificar por que não loga a consulta:

> *"o texto é o histórico de busca de um usuário, e este é um produto local-first cuja premissa é que ele continue sendo dele."*

**O sistema de arquivos do servidor é o sistema de arquivos do usuário.** Devolver o caminho é devolver a ele um dado que já é dele, sobre arquivos que ele mesmo mandou indexar. A premissa do argumento não vale nesta implantação, então não há decisão a reverter — há uma condição a declarar, e a declaração precisa ser executável e não uma nota de rodapé (§6).

A premissa volta a valer no instante em que alguém sobe a API em `0.0.0.0`. É por isso que §6 é um guarda e não um parágrafo.

### 2.2 Segundo argumento: continua valendo, e molda o desenho

> *"Entregaria aos clientes um identificador que muda sempre que um arquivo se move — o RFC-024 deriva o id da imagem do caminho, então um caminho armazenado é ao mesmo tempo instável e inutilizável."*

Isto **não** dissolve. Continua verdadeiro, e o RFC-027 o tornou mais verdadeiro ainda: o caminho absoluto não existe mais no banco. Ele é computado juntando o ponto de montagem — resolvido em tempo de requisição, porque letra de unidade não é identidade — ao `relative_path`.

A conclusão que se preserva: **`id` é o identificador; o caminho é uma afordância de exibição e de ação.** Um cliente que guarde o caminho e o use depois para pedir alguma coisa ao servidor está errado, e a API não deve aceitar caminho em lugar nenhum — o que §5 transforma em propriedade de segurança em vez de convenção.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Caminho na resposta de busca | **Sim** — como exibição, nunca como identificador (§4) |
| Forma do caminho | Absoluto quando o dispositivo está conectado; `null` + rótulo do dispositivo quando não (§4.1) |
| `GET /api/v1/images/{id}` | Metadados completos de uma imagem — a promessa do RFC-026 (§4.2) |
| `GET /api/v1/images/{id}/thumbnail` | Bytes, de um cache gerenciado pelo app (§7) |
| `POST /api/v1/images/{id}/reveal` | Abre o gerenciador de arquivos com o arquivo selecionado (§5) |
| Entrada de qualquer rota | **Sempre `id`, nunca caminho** (§5.1) |
| Onde ficam as thumbnails | Diretório do app, **nunca no acervo** (§7.1) |
| Quando são geradas | Durante a indexação, com a imagem já decodificada (§7.2) |
| Ações locais | Atrás de `settings.allow_local_file_actions` **e** de um guarda de loopback (§6) |
| Plataforma | Windows neste RFC; portas declaradas (§5.3) |
| Migration | `images.thumbnail_path` |

## 4. O caminho na resposta

```json
{
  "id": "…",
  "filename": "DJI_0042",
  "similarity": 0.3117,
  "captured_at": "2018-06-12T14:30:00",
  "capture_source": "exif_original",
  "device": { "id": "…", "label": "HD3", "connected": false },
  "relative_path": "fotos/2018/junho/DJI_0042.JPG",
  "absolute_path": null
}
```

`relative_path` está **sempre** presente: é fato armazenado, independe de qualquer disco estar plugado, e já diz ao usuário a maior parte do que ele quer saber. `absolute_path` é derivado e só existe quando há ponto de montagem para derivá-lo.

### 4.1 O disco desconectado é a resposta, não a falha

Este é o comportamento que o RFC-027 §2.3 antecipou e que só se materializa aqui.

| situação | o que a resposta diz | o que o usuário faz |
| --- | --- | --- |
| HD3 plugado | `connected: true`, `absolute_path: "F:/fotos/2018/…"` | clica e abre |
| HD3 na gaveta | `connected: false`, `absolute_path: null`, `device.label: "HD3"` | **pega o HD3** |

A segunda linha não é degradação. Para alguém com vinte discos, *"está no HD3, na pasta 2018/junho"* é a resposta completa — o sistema resolveu o problema de "em qual dos vinte?", que é o problema caro. Uma API que devolvesse 404 ou um caminho quebrado teria transformado a resposta certa em erro.

E é por isso que as thumbnails de §7 importam mais do que o argumento de tamanho de payload sugere: com o disco desconectado, a thumbnail em cache é a única forma de o usuário **ver** a foto que ele acabou de encontrar e confirmar que é aquela antes de ir buscar o disco.

### 4.2 `GET /api/v1/images/{id}`

A rota que o RFC-026 prometeu. Devolve o mesmo objeto de §4 sem `similarity` — que não existe fora de uma consulta, pela razão que o RFC-025 §4.1 deu para `SearchHit` não ser um campo de `Image`.

404 quando o id não existe. **200 quando o dispositivo está desconectado** — a linha existe, e é isso que a rota descreve.

## 5. Revelar o arquivo

Mostrar `F:/fotos/2018/junho/DJI_0042.JPG` como texto é a forma mais fraca de atender §1. O que o fotógrafo quer é a pasta abrindo com o arquivo selecionado.

```
POST /api/v1/images/{id}/reveal   →   204 No Content
```

### 5.1 A propriedade de segurança: nenhum caminho entra

O corpo da requisição é **vazio**. O cliente manda um UUID na URL; o servidor busca a linha, resolve o ponto de montagem do dispositivo e monta o caminho ele mesmo.

Isso não é estilo. É o que torna uma classe inteira de ataque impossível em vez de filtrada: não existe string de caminho vinda do cliente para sanitizar, então não existe travessia de diretório, não existe `..`, não existe caminho UNC apontando para uma máquina remota. O único parâmetro é um UUID, e um UUID que não está na tabela é um 404.

A execução em si nunca passa por um shell:

```python
subprocess.run(["explorer", f"/select,{absolute_path}"], shell=False, check=False)
```

`shell=False` com lista de argumentos. Mesmo que o caminho contivesse metacaracteres — e ele vem do disco do próprio usuário, não da rede — não há shell para interpretá-los.

`explorer.exe` retorna código de saída não zero mesmo em sucesso, um comportamento conhecido dele; daí `check=False`, e o resultado é 204 quando o processo foi lançado, não quando ele "deu certo".

Erros de domínio: dispositivo desconectado → 409 (o pedido é legítimo, o estado não permite); arquivo ausente apesar do disco conectado → 410 (a linha existe, o arquivo não existe mais).

### 5.2 Por que é `POST`

`GET` é seguro e idempotente por contrato. Isto abre uma janela na máquina do usuário — um efeito colateral no mundo físico. Além do princípio: um `GET` seria pré-buscado por qualquer coisa que percorra links, e uma UI com dez resultados abriria dez janelas do Explorer sem ninguém ter clicado.

### 5.3 Plataforma

| SO | comando |
| --- | --- |
| Windows | `explorer /select,<path>` — **entregue** |
| macOS | `open -R <path>` — declarado, não entregue |
| Linux | `dbus-send` para `org.freedesktop.FileManager1`, com `xdg-open` na pasta pai como fallback — declarado, não entregue |

Atrás de uma porta, com o adaptador Windows como única implementação, pela razão do RFC-027 §4.1: um adaptador não testado é uma afirmação de portabilidade que ninguém verificou.

## 6. Os dois guardas

§2.1 mostrou que o argumento do RFC-026 dissolve **porque a implantação é local**. Uma condição que sustenta uma decisão de segurança precisa ser verificada em execução, não presumida na leitura.

**Guarda 1 — configuração.** `settings.allow_local_file_actions`, default **`False`**. Segue exatamente o precedente de `warm_up_models` (RFC-026 §10): *"o default seguro é o que não pode baixar 600 MB para dentro de um processo que não pediu"*. Aqui, o default seguro é o que não pode abrir janelas numa máquina que não pediu. Desligado, `/reveal` responde 404 — a rota não existe, em vez de existir e recusar.

**Guarda 2 — loopback.** Independente da configuração, `/reveal` recusa requisições cujo `request.client.host` não seja de loopback. Isso vale mesmo com o guarda 1 ligado, e mesmo se alguém subir a API em `0.0.0.0` sem perceber — o que é precisamente o cenário em que o argumento original do RFC-026 volta a valer.

Os dois são independentes de propósito. O primeiro é intenção do operador; o segundo é um fato sobre quem está chamando. Nenhum dos dois substitui o outro.

O caminho na resposta de busca (§4) **não** fica atrás do guarda 1 — ele é o coração do produto e o guarda 2 já cobre o cenário de exposição. Um sistema que exige configuração para cumprir sua função principal está com o default errado.

## 7. Thumbnails

O `ARCHITECTURE.md` já decidiu a forma, e a decisão continua certa:

> *"Thumbnails são geradas uma vez durante a indexação e armazenadas em disco. São servidas ao frontend como arquivos estáticos por um endpoint dedicado, nunca embutidas como base64 nas respostas da API."*

Este RFC acrescenta o que aquele parágrafo não cobria.

### 7.1 Onde elas ficam — e onde não ficam

Em um diretório gerenciado pelo app (`settings.thumbnail_directory`), endereçadas por `images.id`. **Nunca no acervo.** O RFC-028 §10 já declarou a regra: *"o sistema nunca escreve no acervo"*. Um usuário cujo HD de fotos ganha uma pasta `.solidvision/` que ele não criou perde a confiança que a premissa local-first depende.

A consequência é a propriedade de §4.1: as thumbnails vivem em um disco que está **sempre** conectado, então continuam servíveis quando o disco de origem está na gaveta. Guardá-las junto das fotos teria destruído exatamente o caso de uso que mais precisa delas.

### 7.2 Quando são geradas — de graça, quase

O pipeline do RFC-024 já abre e decodifica cada imagem com Pillow para alimentar o CLIP. Uma thumbnail de 512 px sai dessa imagem **já decodificada em memória**; o custo é redimensionar e codificar, sem nenhuma leitura de disco a mais.

Custo por imagem, e o quanto ele desloca as 2,2 imagens/s do RFC-024: `TBM`. A expectativa é que fique bem abaixo dos ~450 ms/imagem da inferência, mas o RFC-024 §17 tem uma seção inteira sobre hipóteses que os números mataram, e esta é uma hipótese.

Imagens indexadas antes deste RFC não têm thumbnail. O backfill é uma varredura sem modelo, como o do RFC-028 §7, e exige o dispositivo conectado.

`GET /api/v1/images/{id}/thumbnail` serve os bytes. 404 quando ainda não foi gerada; a UI mostra um placeholder e não um erro.

**Correção em relação a uma versão anterior deste RFC.** Um rascunho anterior justificava cache agressivo (`Cache-Control: immutable`) alegando que *"mudar a foto muda o caminho e portanto o id"*. Isso está errado, e vale registrar o erro em vez de apagá-lo: sob o RFC-027, `id = uuid5(ns, f"{device_id}/{relative_path}")` — depende só do **caminho**, nunca do conteúdo. Um usuário que sobrescreve a mesma foto no mesmo lugar (reexportar, editar e salvar por cima) produz o **mesmo id**, e uma resposta marcada `immutable` faria o navegador continuar servindo a thumbnail antiga da própria memória, sem nunca revalidar.

O que de fato acontece quando o conteúdo muda: a decisão incremental do RFC-020 compara `file_size`/`file_modified_at`/`content_hash` e, se algum diverge, marca o arquivo como candidato a reprocessamento — o mesmo evento que já dispara um novo embedding (RFC-024). §7.2 gera a thumbnail **nesse mesmo reprocessamento**, a partir da imagem já decodificada, e escreve por cima do arquivo endereçado pelo id de sempre. O mecanismo funciona; a frase anterior só descrevia o motivo errado.

Isso torna `Cache-Control: immutable` inválido — e a correção certa não é trocar por um TTL curto, que jogaria fora o benefício de cache sem necessidade, mas usar validação condicional: `ETag` calculado a partir de `images.content_hash`, que o RFC-024 já computa e armazena para a decisão incremental (§6.1 do RFC-028 já reaproveita esse mesmo campo pelo mesmo motivo — não pagar duas vezes por um dado que já existe). Com `Cache-Control: max-age=31536000` **e** `ETag: "<content_hash>"`, um cliente que já tem a thumbnail em cache manda `If-None-Match` e recebe `304` quando o conteúdo não mudou — sem baixar bytes de novo — e recebe a thumbnail nova, servida normalmente, no instante em que `content_hash` diverge. O ganho de desempenho do cache agressivo é preservado; a correção que faltava é a revalidação, não a ausência de cache.

### 7.3 A janela do RFC-027 fecha aqui

O RFC-027 §6.2 justificou sua prioridade pela ausência de referências a `images.id`, e o RFC-029 §11 verificou que jobs não criam nenhuma.

Este RFC cria: `thumbnail_path` é derivado de `images.id`, e os arquivos em disco são nomeados por ele. Reescrever a PK a partir daqui significa renomear arquivos junto, e o passo deixa de ser um `UPDATE` em uma tabela.

**Isto é registrado como consequência assumida, não descoberta depois.** É a última confirmação de que a ordenação dos quatro RFCs estava certa.

## 8. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| Manter o caminho fora da resposta | O produto não entrega recuperação sem ele (§1) |
| Caminho absoluto armazenado no banco | O RFC-027 o removeu porque muda sozinho; guardá-lo de novo desfaz aquilo (§2.2) |
| Caminho como identificador em rotas | Instável, e reabre travessia de diretório; o id já é estável (§2.2, §5.1) |
| `/reveal` aceitando o caminho no corpo | Cria uma superfície de injeção que o desenho por id torna inexistente (§5.1) |
| `subprocess` com `shell=True` | Um shell para interpretar metacaracteres onde nenhum é necessário (§5.1) |
| `GET /reveal` | Pré-busca abriria janelas sem clique (§5.2) |
| Só o guarda de loopback | Não expressa intenção do operador; um túnel local o satisfaz (§6) |
| Só o guarda de configuração | Não protege quem subiu em `0.0.0.0` sem perceber (§6) |
| Ações locais ligadas por default | O precedente de `warm_up_models` é o default que não age sem ser pedido (§6) |
| Caminho na busca atrás de configuração | Exigiria configurar o sistema para ele cumprir a função principal (§6) |
| Thumbnails no acervo | O sistema nunca escreve no acervo, e o disco offline deixaria de ter thumbnail (§7.1) |
| Thumbnails em base64 na resposta | Já rejeitado pelo `ARCHITECTURE.md`; infla o JSON e impede cache do navegador (§7) |
| Thumbnails sob demanda na primeira leitura | Exige o disco conectado exatamente quando ele costuma não estar (§7.1) |
| Servir a imagem original completa | Uma foto de drone tem dezenas de MB; a thumbnail resolve a visualização (§10) |
| `Cache-Control: immutable` sem `ETag` | Rascunho anterior deste RFC. Presumia que o id muda quando o conteúdo muda — não muda, é derivado do caminho (RFC-027). Serviria bytes obsoletos para sempre após uma foto ser sobrescrita no mesmo caminho (§7.2) |
| Renomear a thumbnail a cada reprocessamento (versionar por hash no nome do arquivo) | Resolveria a invalidação, mas exige limpar o arquivo antigo e complica o endereçamento por id que o resto do RFC padroniza; `ETag` com `content_hash` já existente resolve sem isso (§7.2) |

## 9. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **`/reveal` executa um processo na máquina do usuário** | Dois guardas independentes, entrada só por UUID, sem shell (§5.1, §6) |
| **A janela de reescrita barata da PK fecha aqui** (§7.3) | Consequência assumida; a ordem dos RFCs foi escolhida por isso |
| **Só Windows tem adaptador de revelação** (§5.3) | Deliberado; a porta existe |
| **Cache de thumbnails cresce sem política de limpeza** | Trabalho futuro; `TBM` KB por imagem × 100.000 é o número que decide se é urgente |
| **Uma thumbnail pode ficar obsoleta se o cache do cliente não revalidar** | Mitigado por `ETag` sobre `content_hash`, não por imutabilidade do id — o id **não** muda quando o conteúdo muda (§7.2, corrigido de uma versão anterior deste RFC) |
| **Backfill de thumbnails exige o dispositivo conectado** (§7.2) | Consequência do RFC-027 |
| Um caminho com caracteres fora do ASCII | `subprocess` com lista de argumentos trata; teste dedicado |

Trabalho futuro: política de retenção do cache; adaptadores macOS/Linux; servir a imagem original em resolução intermediária para um visualizador; e "abrir com o aplicativo padrão", que é a mesma superfície de segurança de `/reveal` e cabe atrás do mesmo guarda.

## 10. Não-objetivos

- Servir a imagem original completa (§8)
- Visualizador, lightbox, zoom, comparação lado a lado
- Escrever qualquer coisa no acervo — inclusive EXIF, renomeação, mover, apagar (§7.1)
- Copiar, exportar ou compartilhar arquivos a partir da UI
- Montar dispositivos automaticamente
- Autenticação e múltiplos usuários — os guardas de §6 são de implantação, não de identidade
- Adaptadores macOS e Linux (§5.3)
- Retenção/limpeza do cache de thumbnails (§9)
- A UI em si

## 11. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/domain/exceptions/file_access_errors.py` | Dispositivo desconectado, arquivo ausente |
| `backend/app/application/use_cases/get_image_details.py` | §4.2 |
| `backend/app/application/use_cases/reveal_image.py` | §5 |
| `backend/app/infrastructure/filesystem/file_revealer.py` | Porta + adaptador Windows (§5.3) |
| `backend/app/infrastructure/filesystem/thumbnail_generator.py` | §7.2 |
| `backend/app/infrastructure/workers/thumbnail_backfill.py` | §7.2 |
| `backend/app/presentation/schemas/image_schema.py` | O placeholder do RFC-026, finalmente preenchido |
| `backend/alembic/versions/*_add_image_thumbnail_path.py` | |
| `backend/tests/presentation/test_image_details_api.py` | |
| `backend/tests/presentation/test_reveal_guards.py` | Os dois guardas, e o 404 com o guarda 1 desligado (§6) |
| `backend/tests/infrastructure/filesystem/test_thumbnail_generator.py` | |
| `experiments/rfc-030-file-access/measure_thumbnail_cost.py` | A medição de §7.2 |
| `docs/rfcs/rfc-030-acesso-ao-arquivo.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/presentation/schemas/search_schema.py` | `device`, `relative_path`, `absolute_path`, `captured_at`; docstring reescrita com o resultado de §2 |
| `backend/app/presentation/api/v1/routers/images.py` | As três rotas novas; `/thumbnail` responde `ETag`/`If-None-Match` sobre `content_hash` (§7.2) |
| `backend/app/infrastructure/database/models/image_model.py` | `thumbnail_path` |
| `backend/app/application/use_cases/index_or_update_images.py` | Gera a thumbnail no lote já decodificado (§7.2) |
| `backend/app/infrastructure/config/settings.py` | `allow_local_file_actions`, `thumbnail_directory`, `thumbnail_max_edge` |
| `.env.example` | As três chaves novas |
| `ARCHITECTURE.md` | §15 `Images` ganha `thumbnail_path`; a seção de thumbnails ganha "onde não ficam" (§7.1) |

## 12. Validação

| verificação | resultado |
| --- | --- |
| `pytest` | `TBM` |
| `pytest -m slow` | `TBM` |
| `black --check .` / `ruff check .` | `TBM` |
| `mypy` | `TBM` — **0 erros em código novo** é o critério |
| `alembic heads` | `TBM` — head único |
| `alembic downgrade`/`upgrade` | `TBM` |
| `/reveal` com `allow_local_file_actions=false` | `TBM` — 404, e nenhum processo lançado |
| `/reveal` de cliente não-loopback | `TBM` — recusado mesmo com a configuração ligada |
| `/reveal` nunca aceita string de caminho | `TBM` — teste de assinatura, fixa §5.1 |
| Nenhuma escrita sob a raiz do dispositivo durante indexação | `TBM` — fixa §7.1 |
| Thumbnail servida com o dispositivo desconectado | `TBM` — fixa a premissa de §4.1 |
| Reprocessar uma imagem (conteúdo mudou, id igual) muda o `ETag` da thumbnail | `TBM` — o teste que fixa a correção de §7.2 |
| Requisição com `If-None-Match` do hash correto recebe `304` sem corpo | `TBM` — fixa que a revalidação realmente evita reenviar bytes |
| Busca com dispositivo desconectado devolve 200 e `absolute_path: null` | `TBM` — fixa §4.1 |
| Custo de thumbnail por imagem, e impacto nas 2,2 img/s | `TBM` (§7.2) |
| Tamanho médio da thumbnail | `TBM` (§9) |
