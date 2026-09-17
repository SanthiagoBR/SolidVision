# Prompt de aplicação — RFC-030: Acesso ao Arquivo (caminho, thumbnail, revelação)

> Documento de trabalho. Entrada para quem (ou o que) vai implementar a RFC-030.
> A autoridade é `docs/rfcs/rfc-030-acesso-ao-arquivo.md`; este arquivo não a
> substitui — ele diz **como aplicá-la neste código**, aponta onde a RFC
> contradiz o código de hoje (ou presume algo que não existe nele), e fixa as
> decisões que faltavam.
>
> A RFC-030 foi escrita depois das RFCs 027, 028 e 029 estarem implementadas, e
> a maior parte do que ela presume já existe — mais do que qualquer RFC
> anterior chegou a encontrar pronto. Mas ela também tem uma premissa central
> que não se sustenta contra o código real (§4.1), um status HTTP que o
> handler não sabe emitir (§4.4), e um trecho de código para o `/reveal` que
> quebra com caminho contendo espaço (§4.12). Leia a seção 4 antes de
> escrever qualquer linha.

---

## 0. Missão

Implementar a RFC-030 no `develop` do SolidVision: o caminho (relativo sempre,
absoluto quando o disco está conectado) na resposta de busca e em
`GET /api/v1/images/{id}`, a geração de thumbnails durante a indexação, o
endpoint que as serve com cache condicional por `ETag`, e `POST
/api/v1/images/{id}/reveal` atrás de dois guardas independentes.

O critério de aceite é o mesmo das RFCs 027, 028 e 029: código com a mesma
densidade de docstrings explicando *por que*, `mypy --strict` com zero erros,
as implementações de repositório indistinguíveis sob teste de contrato, e cada
número medido escrito de volta no documento.

### 0.1 Pré-condição

**Nenhuma.** `git status` na raiz está limpo e a RFC-029 já está commitada
(`22ab053`). Isto é diferente da RFC-029, que teve que esperar a RFC-028 ser
commitada primeiro — não há trabalho pendente para este prompt herdar.

Linha de base, verificada nesta sessão:

- `alembic heads`: **`e7a2c9b41f30`** (único).
- `mypy app`: **zero erros em 108 arquivos**.
- `pytest`: **1245 testes esperados, 58 deselected.** A execução completa deu
  `1208 passed, 37 errors` porque o contêiner do PostgreSQL estava parado
  durante a maior parte dela; os 37 erros eram todos de testes `[postgres]`
  (`test_device_repository_contract.py`, `test_indexing_job_concurrency.py`,
  `test_indexing_job_repository_contract.py` e afins), e com o banco no ar
  `tests/infrastructure/persistence`, `test_alembic_migrations.py` e
  `tests/presentation` deram `455 passed`. A suíte completa levou ~29 min,
  quase todo esse tempo em timeouts de conexão.
- `pytest -m slow`: não rodado nesta preparação.

**Suba o Postgres (`docker compose up -d`) antes de medir, rode `pytest` e
`pytest -m slow` e anote os números reais** — os de cima são a expectativa,
não um substituto da execução. Um erro de conexão no meio da suíte não é
"linha de base com 37 erros".

---

## 1. Leia isto antes de escrever qualquer linha

| arquivo | por quê |
| --- | --- |
| `docs/rfcs/rfc-030-acesso-ao-arquivo.md` | a especificação |
| `docs/rfcs/rfc-027-dispositivos-e-identidade-de-volume.md` §2.3, §7 | por que "desconectado" é uma pergunta feita agora, nunca uma coluna; a origem de `absolute_path: None` como resposta válida |
| `backend/app/domain/entities/image.py` | `absolute_path`, `display_path`, `require_absolute_path()` — **já escritos, citando a RFC-030 por nome**, antes deste prompt existir |
| `backend/app/domain/services/device_locator.py`, `mounted_device_locator.py` | o port que resolve mount point e "conectado" a cada chamada, sem cache — é o que os dois novos endpoints e o `/reveal` vão usar, sem escrever nada novo |
| `backend/app/application/use_cases/create_indexing_job.py` | o único lugar hoje que já combina `DeviceRepository` + `DeviceLocator` numa Application use case — o molde para tudo que esta RFC precisa resolver (§4.3) |
| `backend/app/infrastructure/ai/clip_embedding_model.py` `_preprocess()` | onde a imagem é de fato decodificada com Pillow durante a indexação — e por que essa decodificação **não** está acessível de onde a RFC presume (§4.1) |
| `backend/app/infrastructure/filesystem/exif_capture_date.py`, `sha256_content_hasher.py` | os outros dois lugares que já abrem o mesmo arquivo, cada um para seu próprio fim estreito — o padrão que a geração de thumbnail vai seguir, não quebrar |
| `backend/app/presentation/error_handlers.py` | o despacho por `STATUS_BY_BASE`; hoje só sabe 404 e 409, e a RFC quer um terceiro código (§4.4) |
| `backend/app/domain/exceptions/image_errors.py` | `ImageNotFoundError` já existe — e está na base errada para o que a RFC precisa (§4.5) |
| `backend/app/domain/value_objects/index_metadata.py` | o precedente exato para carregar um campo "não é sinal de mudança" no prefetch em lote — é como `capture_source` chegou lá na RFC-028, e é o molde para o status de thumbnail (§4.7) |
| `backend/app/application/use_cases/index_or_update_images.py` `_flush()`, `_persist_batch()` | onde a geração de thumbnail entra no pipeline, e por que tem que ser isolada por arquivo como tudo mais ali (RFC-024 §6) |
| `backend/app/infrastructure/workers/capture_date_backfill.py` | o molde inteiro para `thumbnail_backfill.py` — mesma composição, mesmo argumento `--root`, mesma ausência do modelo |
| `ARCHITECTURE.md` §15 (`Images`, "Thumbnail Serving") | **já documenta `thumbnail_path` e a decisão de servir como arquivo estático** — escrito antes deste prompt, antes até da RFC-030 ser proposta (§4.9) |

---

## 2. O que já existe (verificado, não presumido)

Mais coisas do que em qualquer RFC anterior — a 027, a 028 e a 029 deixaram
ganchos deliberados:

- **`Image.absolute_path: ImagePath | None`, `Image.display_path`,
  `Image.require_absolute_path()`** já existem e a docstring do último já cita
  a RFC-030 pelo nome: *"e depois a RFC-030, que o resolve por requisição para
  a resposta de busca"*. Levanta `DeviceNotConnectedError` quando o dispositivo
  não está montado — pronto para o `/reveal` usar sem escrever checagem
  própria (§4.6).
- **`DeviceLocator.mount_point(device) -> Path | None`** já é exatamente a
  pergunta que a RFC precisa fazer para `connected` e para `absolute_path`.
  Nada de novo a construir aqui — só a decisão de *onde* chamá-lo (§4.3).
- **`error_handlers.py` já despacha por hierarquia**, não por registro por
  classe: `NotFoundError` → 404, `ConflictError` → 409, resto → 400. Um novo
  erro de domínio que herde da base certa não precisa tocar neste arquivo.
- **`DeviceNotConnectedError(ConflictError)` já existe** e já é o que
  `require_absolute_path()` levanta. É literalmente o erro que a RFC-030 §5.1
  quer para "`/reveal` com disco desconectado → 409" — reaproveitar, não
  recriar.
- **`Device` deliberadamente não tem campo `connected` nem `mount_point`**
  (RFC-027, docstring da entidade: *"seria errado em toda leitura feita depois
  de o usuário desconectar o disco"*). `connected` no JSON de resposta **tem
  que ser computado por requisição**, nunca lido de um campo — a RFC-030 §4
  mostra `device.connected` no exemplo, mas a entidade que ela consome não
  carrega essa informação em lugar nenhum.
- **`ImageId = uuid5(namespace, f"{device_id}/{relative_path}")`**,
  confirmado em `image_identity.py` — exatamente o que a RFC-030 §7.2 usa
  para justificar por que o id não muda quando o conteúdo muda. A correção que
  a própria RFC registra (o `Cache-Control: immutable` do rascunho anterior
  estava errado) está certa contra o código real.
- **`content_hash` não está na entidade `Image`**, só em `IndexMetadata` e
  `IndexingRecord` — igual a `file_size` e `file_modified_at`, e pelo mesmo
  motivo que a docstring de `Image.captured_at` explica: são sinais de
  mudança, não atributos da fotografia. A RFC-030 §7.2 quer `content_hash`
  como `ETag`, o que é um terceiro motivo para um consumidor querer esse
  campo — mas não é motivo para promovê-lo à entidade (§4.5).
- **`FastAPI(...)` não chama `uvicorn.run()` em lugar nenhum do repositório**
  — o bind de host é inteiramente uma decisão do operador, fora deste código.
  Isso não é uma lacuna a preencher; é a confirmação de que o guarda de
  loopback da RFC §6 protege um cenário real, não hipotético.
- **`ARCHITECTURE.md` §15 já lista `thumbnail_path` na tabela `Images`** e já
  tem uma subseção "Thumbnail Serving" descrevendo servir como arquivo
  estático, nunca base64 — escrito antes da RFC-030 existir como documento.
  O que falta lá é só a estratégia de cache condicional do §7.2 (§4.9).

---

## 3. Invariantes que não podem ser quebrados

1. **Nenhum caminho entra numa rota.** Toda entrada é `id`. `/reveal` não
   aceita string de caminho no corpo, na query ou em header — é a propriedade
   de segurança inteira do endpoint (RFC §5.1).
2. **`absolute_path` nunca é persistido.** Computado por requisição a partir
   de `relative_path` + o ponto de montagem resolvido *agora* — a mesma regra
   que a RFC-027 já impõe para `Device.mount_point`.
3. **Thumbnails nunca ficam sob a raiz do dispositivo.** Só em
   `settings.thumbnail_directory`, que é um diretório do app. Um teste varre
   o disco de teste depois de indexar e falha se qualquer arquivo novo
   aparecer lá (RFC §7.1).
4. **`/reveal` nunca passa por um shell.** `subprocess.run([...], shell=False)`
   com lista de argumentos, nunca uma string montada e interpolada.
5. **Os dois guardas de `/reveal` são independentes.** Um teste que só
   desliga `allow_local_file_actions` tem que continuar barrado mesmo vindo
   de loopback com a configuração ligada por engano em outro teste (isolamento
   de fixture); um teste de cliente não-loopback tem que ser barrado mesmo
   com a configuração ligada (RFC §6).
6. **A geração de thumbnail nunca aborta a indexação de uma imagem.** Uma
   imagem cujo embedding foi calculado com sucesso e cuja thumbnail falhou ao
   renderizar continua sendo indexada, buscável, e sem thumbnail até o
   próximo backfill — nunca um `IndexingFailure` (§4.1, §4.8).
7. **`content_hash` não sobe para a entidade `Image`.** Quem precisar dele
   para o `ETag` busca `IndexMetadata` separadamente, exatamente como já se
   faz para a decisão incremental (§4.5).
8. **Nenhuma FK nova para `images` além do que a própria RFC-030 introduz.**
   A RFC-029 §11 já registrou que RFC-030 é quem fecha a janela barata de
   reescrita da PK (`thumbnail_path` é derivado de `images.id`); isso é
   esperado e assumido, não uma regressão a evitar.

---

## 4. As decisões que a RFC deixou em aberto — ou errou

**Toda decisão abaixo deve ser escrita de volta na RFC**, na seção
correspondente, com a frase original preservada e corrigida à vista quando a
RFC estava errada — a convenção do `docs/rfcs/README.md` que as RFCs 027, 028
e 029 já seguem.

### 4.1 "Thumbnail sai quase de graça da imagem já decodificada" — falso neste código

RFC §7.2: *"O pipeline do RFC-024 já abre e decodifica cada imagem com Pillow
para alimentar o CLIP. Uma thumbnail de 512 px sai dessa imagem já decodificada
em memória; o custo é redimensionar e codificar, sem nenhuma leitura de disco
a mais."*

Verificado contra `clip_embedding_model.py._preprocess()`: a imagem decodificada
com `PILImage.open(image.require_absolute_path().value)` é uma **variável
local desse método**, dentro do adaptador `ClipEmbeddingModel`, que vive atrás
de `EmbeddingModelPort`. `IndexOrUpdateImagesUseCase` — a Application use case
onde a RFC quer gerar a thumbnail — nunca vê essa imagem decodificada. Ela é
aberta, convertida para RGB e descartada inteiramente dentro do adaptador de
Infrastructure, um nível abaixo de onde a RFC presume que ela está disponível.

E não é o único arquivo já aberto por arquivo durante a indexação:
`exif_capture_date.read_capture_date()` abre o arquivo mas nunca chama
`.load()` — só lê o cabeçalho, de propósito, para ser barato (RFC-028 §6).
`Sha256ContentHasher.hash_image()` abre o arquivo e lê em streaming para o
hash, sem nunca decodificar como imagem. **Nenhum dos três opens existentes
produz um objeto Pillow decodificado reaproveitável** — cada um já segue o
padrão de abrir o arquivo para seu próprio fim estreito e descartar o resto,
exatamente como este projeto já faz em todo outro lugar.

**Decisão:** a geração de thumbnail é um **quarto open independente**,
consistente com o padrão já estabelecido, não uma exceção a ele. Um novo port
de Domain, `ThumbnailGeneratorPort` (ao lado de `ContentHasherPort`, mesma
forma):

```python
class ThumbnailGeneratorPort(ABC):
    @abstractmethod
    def generate(self, image: Image, max_edge: int) -> bytes:
        """Return encoded thumbnail bytes for `image`, decoding it fresh."""
```

com um adaptador `PillowThumbnailGenerator` em
`infrastructure/filesystem/thumbnail_generator.py` que abre
`image.require_absolute_path()`, chama `.thumbnail((max_edge, max_edge))` e
codifica (JPEG, qualidade a decidir e medir). **Corrija a RFC**: o custo não é
"redimensionar e codificar sobre uma imagem já em memória" — é um decode
completo mais redimensionamento mais encode, e é exatamente isso que
`experiments/rfc-030-file-access/measure_thumbnail_cost.py` tem que medir
contra as ~450 ms/imagem da inferência, não contra zero.

**Alternativa recusada:** fazer `EmbeddingModelPort.encode_image` devolver
também a imagem decodificada, ou os bytes da thumbnail. Misturaria uma
responsabilidade do CLIP adapter com uma que não tem nada a ver com
embeddings, e forçaria `FakeEmbeddingModel` a saber gerar thumbnails para
continuar sendo um duplo válido.

### 4.2 `device.connected` tem que ser computado, e em lote por dispositivo distinto

A RFC mostra `"device": { "id": "…", "label": "HD3", "connected": false }` no
JSON de resposta, mas `Device` não carrega esse campo (§2, e a docstring da
entidade explica por quê). **Decisão:** resolvido por requisição, chamando
`DeviceLocator.mount_point(device)` e testando `is not None`.

**E uma vez por dispositivo distinto na página de resultados, não uma vez por
hit.** `MountedDeviceLocator.mount_point()` chama
`VolumeIdentityProvider.mounted_volumes()` a cada invocação — nunca cacheia,
de propósito (RFC-027 §7) — e essa é uma enumeração real do sistema
operacional. Uma busca com 10 resultados em 2 discos deve custar 2
enumerações, não 10. Agrupe os hits por `device_id`, resolva cada um uma vez,
e aplique o resultado a todos os hits daquele dispositivo.

### 4.3 Onde essa resolução mora: nem na rota, nem em `SearchImagesUseCase`

Duas rotas precisam da mesma lógica — `GET /images/search` (existente,
estendida) e `GET /images/{id}` (nova, `GetImageDetailsUseCase`) — e nenhuma
das duas pode conter essa lógica diretamente:

- não pode ser a **rota**: `AI_Context.md` e `ARCHITECTURE.md` §24 proíbem
  regra de negócio em Presentation, e resolver "este disco está conectado
  agora" é exatamente esse tipo de regra — é o mesmo raciocínio que já mantém
  `search_images.py` livre de `SessionLocal` e de `ClipEmbeddingModel`;
- não pode ser um método novo dentro de **`SearchImagesUseCase`** sozinho,
  porque `GetImageDetailsUseCase` precisaria duplicar a mesma lógica ou
  importar a outra use case para reaproveitá-la, o que não é o padrão do
  projeto em nenhum outro lugar.

**Decisão:** uma função ou classe pequena em Application,
`app/application/use_cases/resolve_image_location.py`, seguindo exatamente o
molde de `CreateIndexingJobUseCase` (§1) — combina `DeviceRepository` +
`DeviceLocator`, recebe uma lista de `Image` (ou de `SearchHit`), devolve a
mesma lista com `absolute_path` preenchido via `dataclasses.replace()` mais um
mapa `DeviceId -> connected: bool`. Usada por `search_images.py` (a rota, não
a use case — a resolução de localização não é "buscar", é uma decoração da
resposta) e por `GetImageDetailsUseCase`. Wire-a em `dependencies.py` como uma
dependência própria, do mesmo jeito que `get_device_locator()` já existe.

### 4.4 410 Gone não existe em `error_handlers.py` — é preciso criar a base

RFC §5.1: *"arquivo ausente apesar do disco conectado → 410"*.
`STATUS_BY_BASE` em `error_handlers.py` hoje só conhece duas bases:
`NotFoundError` → 404 e `ConflictError` → 409. Não há caminho para 410 em
lugar nenhum do código.

**Decisão:** uma terceira base em `domain_error.py`, ao lado das outras duas:

```python
class GoneError(DomainError):
    """Raised when the thing referred to existed but no longer does.

    Distinct from NotFoundError: an id that never existed is 404; an id
    that named something real, which is now gone, is 410. The caller is
    not wrong to have asked -- the world changed under the request.
    """
```

registrada em `STATUS_BY_BASE` como `(GoneError, status.HTTP_410_GONE)`. O
novo erro de `file_access_errors.py` para "arquivo sumiu do disco apesar do
volume montado" herda dela. Escreva um teste que fixa os três status juntos —
404/409/410 — e outro que confirma que todo erro de domínio pré-existente
continua 400, seguindo exatamente a convenção que a RFC-029 já deixou.

### 4.5 `ImageNotFoundError` já existe, mas está na base errada para o que a RFC pede

`image_errors.py` já define `ImageNotFoundError(DomainError)` — mas
`DomainError` puro responde 400, e a RFC §4.2 exige 404 para
`GET /api/v1/images/{id}` com um id que não existe. Verificado: esta exceção
**nunca é levantada em código de produção hoje** — só é importada e testada
em `test_domain_exceptions.py`, que apenas confirma `issubclass(...,
DomainError)`. Ela é um resquício de uma RFC anterior à existência de
qualquer rota que pudesse levantá-la.

**Decisão:** rebase para `ImageNotFoundError(NotFoundError)`. Seguro — nada
depende do 400 atual porque nada a levanta ainda — e é exatamente o mesmo
movimento que a RFC-029 já fez para `DeviceNotFoundError`. Atualize o teste de
`issubclass` para `NotFoundError` também, e use esta exceção em
`GetImageDetailsUseCase` em vez de criar uma nova. **Registre isto na RFC**
como uma correção ao código existente descoberta durante a implementação, não
como algo que a RFC-030 "criou".

### 4.6 `/reveal`: reaproveite `require_absolute_path()`, não escreva uma checagem nova

`reveal_image.py` (a use case) não precisa perguntar "o dispositivo está
conectado?" e montar o erro à mão. `Image.require_absolute_path()` já faz
exatamente isso e já levanta `DeviceNotConnectedError` — o erro que a RFC
§5.1 quer para esse caso, já mapeado para 409. A use case:

```python
def execute(self, image_id: ImageId) -> None:
    image = self._repository.get(image_id)
    if image is None:
        raise ImageNotFoundError(...)
    image = _with_resolved_path(image, self._devices, self._locator)  # §4.3
    path = image.require_absolute_path()  # DeviceNotConnectedError -> 409
    if not path.value.exists():
        raise FileGoneError(...)  # GoneError -> 410, §4.4
    self._revealer.reveal(path)
```

`FileRevealerPort.reveal(path: ImagePath) -> None` é a única abstração nova
aqui — a porta que a RFC já lista como entregável, com `WindowsFileRevealer`
como única implementação. **A chamada ao `subprocess` não é a da RFC §5.1** —
veja §4.12. Teste com um `FakeFileRevealer` que só grava a chamada — nunca
deixe um teste da suíte padrão abrir o Explorer de verdade.

### 4.7 O status de thumbnail no backfill: mesmo padrão de `capture_source`

`thumbnail_backfill.py` (RFC §11) precisa pular arquivos que já têm thumbnail,
a menos que `--force`. Isso exige saber, para um lote de ids, quais já têm
thumbnail — a mesma forma de pergunta que `capture_date_backfill.py` já faz
para `capture_source`.

**Decisão:** siga o precedente exato de `IndexMetadata.capture_source`
(RFC-028): adicione um campo não-sinal-de-mudança,
`IndexMetadata.thumbnail_generated: bool = False`, populado pelo mesmo
`get_index_metadata_many()` que já busca `file_size`/`file_modified_at`/
`content_hash`/`capture_source` numa query só. Documente com a mesma frase de
aviso que `capture_source` já tem: **não é sinal de mudança, `plan_indexing()`
nunca lê isto**.

### 4.8 `IndexingRecord` ganha `thumbnail_path`, e a geração é isolada por arquivo

`IndexingRecord` hoje carrega `image`, `embedding`, `file_size`,
`file_modified_at`, `content_hash` — sem lugar para o caminho da thumbnail.
Adicione `thumbnail_path: str | None = None`, mesma forma opcional que
`content_hash` já usa.

A geração acontece dentro de `IndexOrUpdateImagesUseCase._encode_group()` ou
logo depois, para cada imagem que efetivamente chegou a `EMBED` com sucesso —
nunca para uma `SKIP_UNCHANGED` ou `REFRESH_METADATA` (essas já têm
thumbnail de uma indexação anterior, ou não têm e o backfill cobre). **A
geração precisa da mesma isolação por arquivo que a inferência já tem**
(invariante 6): uma falha ao gerar a thumbnail de uma imagem não pode
derrubar o embedding, que já foi pago. Envolva a chamada num `try/except`
que loga e conta (`summary.thumbnail_failures`), nunca que propaga para
`IndexingFailure`.

### 4.9 `ARCHITECTURE.md` já documenta a coluna — o que falta é a estratégia de cache

A tabela do entregáveis da RFC (§11) diz que `ARCHITECTURE.md` "ganha
`thumbnail_path`" como se fosse uma adição nova. **Já está lá** (§15, tabela
`Images`, mais a subseção "Thumbnail Serving") — escrita antes deste prompt.
O que essa subseção não tem é a decisão de `ETag`/`Cache-Control` do §7.2 da
RFC, que é nova. Edite a subseção existente para acrescentar isso, em vez de
duplicá-la.

Nota à parte, fora do escopo desta RFC: o §13 de `ARCHITECTURE.md` (pipeline
de indexação) ainda lista "SigLIP Image Encoder" no diagrama, e a tabela de
`Embeddings` (§15) ainda descreve uma tabela separada com `model_name: SigLIP`
que não existe — o `embedding` é uma coluna de `images` desde a RFC-023/024,
e o modelo é CLIP (`laion/CLIP-ViT-B-32`), não SigLIP. Isso é
dívida documental de antes desta RFC; não conserte de passagem — mencione no
PR se quiser, mas não é entregável do RFC-030.

### 4.10 Onde as rotas de imagem crescem

`images.py` já existe com `GET /search`. As três rotas novas (`GET
/{id}`, `GET /{id}/thumbnail`, `POST /{id}/reveal`) entram no mesmo router,
mesmo `prefix="/images"` — nenhum router novo, nenhuma mudança em
`api/v1/__init__.py` além do que já registra `images_router`.

`GET /{id}/thumbnail` **não é um mount `StaticFiles`**. Precisa: checar que o
id existe (404), checar `If-None-Match` contra `content_hash` (304 sem
corpo), e servir bytes com `Cache-Control: max-age=31536000` e `ETag`
(`FileResponse` com `headers=` explícito, ou uma resposta manual — `FileResponse`
é mais simples e já streama). Um arquivo de thumbnail ausente apesar da
coluna preenchida é o mesmo tipo de inconsistência que §4.4 cobre — decida se
vale um 410 aqui também ou um 404 tratado como "ainda não gerada"; a RFC §7.2
já diz que a UI mostra um placeholder no 404, então trate "coluna diz que tem
mas o arquivo sumiu do diretório do app" como 404 também, não 410 — 410 é
reservado para o arquivo *original* do usuário, que é o caso que justifica o
guarda de segurança do `/reveal`; o cache de thumbnail é gerenciado pelo
próprio app e sua ausência não é um evento de "o mundo mudou", é um bug ou uma
limpeza manual.

### 4.11 Teste do guarda de loopback: o `TestClient` default já não é loopback

Verificado: `starlette.testclient.TestClient.__init__` tem
`client: tuple[str, int] = ("testclient", 50000)`. **`TestClient(app)` sem
argumentos já não vem de loopback.** Isso inverte a dificuldade esperada dos
dois testes do guarda 2:

- o teste "cliente não-loopback é recusado" é o **caso default** — passa com
  `TestClient(app)` sem nenhum ajuste, e é fácil escrevê-lo sem perceber que
  não testou nada de especial;
- o teste do **caminho feliz** (`/reveal` com loopback e a config ligada)
  precisa de `TestClient(app, client=("127.0.0.1", 51234))` explícito, ou o
  guarda 2 barra toda a suíte e o teste "positivo" na verdade nunca exercita
  o caminho que libera.

Escreva os dois, e escreva o de loopback primeiro para garantir que ele de
fato passa pelo guarda 2 antes de o guarda 1 (configuração) entrar em jogo.

### 4.12 O `subprocess` da RFC §5.1 quebra com caminho que contém espaço

A RFC escreve `subprocess.run(["explorer", f"/select,{absolute_path}"],
shell=False, check=False)` e o §9 afirma que *"`subprocess` com lista de
argumentos trata"* caracteres especiais. No Windows não há `argv` de verdade:
uma lista vira **uma** linha de comando via `subprocess.list2cmdline()`, que
põe aspas em volta de qualquer argumento que tenha espaço. Verificado no
Python do projeto:

```
['explorer', r'/select,C:\fotos\ab\x.jpg']   ->  explorer /select,C:\fotos\ab\x.jpg
['explorer', r'/select,C:\fotos\a b\x.jpg']  ->  explorer "/select,C:\fotos\a b\x.jpg"
```

O segundo caso é o problema. O `explorer.exe` faz a própria leitura da linha
de comando e não reconhece `/select,` dentro de aspas — o comportamento
conhecido é abrir uma pasta padrão em vez da pasta do arquivo, e sair sem
erro, que com `check=False` vira um 204 mentiroso. Caminho com espaço é o
caso **comum** num acervo de fotos (`Fotos da Viagem`, `HD Externo`), não um
caso extremo. Confirme isso à mão, com um arquivo de verdade, antes de
escolher a correção — este prompt não abriu o Explorer para testar.

**Decisão:** o adaptador monta a linha de comando **ele mesmo**, com as aspas
só em volta do caminho, e a passa como string com `shell=False`:

```python
subprocess.run(f'explorer /select,"{path}"', shell=False, check=False)
```

Com `shell=False`, a string vai direto para `CreateProcess`, sem `cmd.exe` no
meio — a propriedade de segurança do §5.1 continua de pé. O caminho não pode
fechar as aspas: `"` é proibido em nome de arquivo no Windows, e o caminho vem
de `relative_path` juntado a um ponto de montagem resolvido, nunca do
cliente. Escreva isso numa docstring, porque é exatamente o tipo de linha que
alguém "conserta" de volta para a forma de lista.

Teste com um `subprocess.run` injetado (ou monkeypatch) afirmando a string
exata para um caminho com espaço e outro com acento (`Fotos/São Paulo`), e
registre na RFC, §5.1 e §9, a correção com a frase original à vista.

---

## 5. Plano de trabalho

Cada fase termina com `pytest`, `mypy`, `ruff` e `black` limpos.

**Fase 0 — linha de base.** Confirme §0.1: `pytest`, `pytest -m slow`,
`mypy`, `alembic heads`. Anote os números reais.

**Fase 1 — Domain.** `exceptions/domain_error.py` ganha `GoneError` (§4.4);
`exceptions/file_access_errors.py` com o erro de arquivo ausente
(`GoneError`) — reaproveite `DeviceNotConnectedError` para o caso de disco
desconectado, não crie um novo (§4.6); rebase `ImageNotFoundError` para
`NotFoundError` (§4.5) e atualize `test_domain_exceptions.py`; port
`services/thumbnail_generator_port.py` (§4.1); port
`services/file_revealer_port.py`; `value_objects/index_metadata.py` ganha
`thumbnail_generated: bool = False` (§4.7); `value_objects/indexing_record.py`
ganha `thumbnail_path: str | None = None` (§4.8).

**Fase 2 — schema e persistência.** `ImageModel` ganha `thumbnail_path:
Mapped[str | None]`; migration `*_add_image_thumbnail_path.py` (`downgrade()`
real, `alembic heads` único); `ImageRepository.get_index_metadata[_many]`
passam a preencher `thumbnail_generated`; `save_indexed[_many]` escrevem
`thumbnail_path`. Contrato Postgres/em-memória atualizado.

**Fase 3 — resolução de localização.**
`app/application/use_cases/resolve_image_location.py` (§4.3), agrupando por
`device_id` (§4.2). Teste: N hits em 2 dispositivos chamam
`DeviceLocator.mount_point()` exatamente 2 vezes (um `DeviceLocator` fake que
conta chamadas).

**Fase 4 — Application: geração de thumbnail.**
`ThumbnailGeneratorPort` chamado dentro de `IndexOrUpdateImagesUseCase`,
isolado por arquivo (§4.8), com `summary.thumbnail_failures`. Teste: uma
falha de geração não aparece em `IndexingFailure` e a imagem continua
indexada com `thumbnail_path=None`.

**Fase 5 — Infrastructure: adaptadores.**
`filesystem/thumbnail_generator.py` (Pillow, `settings.thumbnail_max_edge`,
formato a decidir — JPEG é o default razoável, meça o tamanho médio);
`filesystem/file_revealer.py` (porta + `WindowsFileRevealer`, §5.3 da RFC —
só Windows implementado); `workers/thumbnail_backfill.py`, seguindo
`capture_date_backfill.py` linha por linha na composição (§4.7).

**Fase 6 — Application: use cases de leitura.**
`get_image_details.py` (`GetImageDetailsUseCase`, usa §4.3 e §4.5);
`reveal_image.py` (usa §4.6).

**Fase 7 — API.** `presentation/schemas/image_schema.py` (o placeholder do
RFC-026, finalmente preenchido — `DeviceSchema` aninhado com `id`, `label`,
`connected`); `search_schema.py` estendido com `device`, `relative_path`,
`absolute_path` (docstring reescrita, RFC §4); as três rotas novas em
`images.py`; `/{id}/thumbnail` com `ETag`/`If-None-Match`/304 (§4.10); os
dois guardas de `/reveal` (`settings.allow_local_file_actions` +
`request.client.host`), com 404 quando o guarda 1 está desligado (a rota não
existe, não recusa — RFC §6).

**Fase 8 — configuração.** `settings.py`: `allow_local_file_actions` (bool,
default `False`), `thumbnail_directory` (`Path`, default fora de qualquer
pasta que a indexação varra), `thumbnail_max_edge` (int, default a decidir —
512 é o que a RFC usa como exemplo). `.env.example` com as três chaves e
comentário no mesmo estilo do resto do arquivo.

**Fase 9 — medições e documentação.** `experiments/rfc-030-file-access/
measure_thumbnail_cost.py` (§7). Depois: RFC reescrita com os `TBM`
preenchidos, `Status: Implementado`; `ARCHITECTURE.md` §15 com a estratégia
de `ETag` (§4.9); `docs/rfcs/README.md` RFC-030 📋 → ✅.

---

## 6. Armadilhas

1. **`TestClient(app)` sem `client=` não é loopback** (§4.11) — o teste do
   caminho permitido precisa do argumento explícito, ou testa o guarda
   errado sem falhar.
2. **A imagem decodificada do CLIP não está acessível fora do adaptador**
   (§4.1). Não tente importar ou refatorar `ClipEmbeddingModel` para expor
   `PILImage` — gere a thumbnail com seu próprio `Image.open()`.
3. **`mount_point()` não cacheia, e não deve passar a cachear** para
   resolver §4.2 — a resposta certa é agrupar as chamadas por dispositivo
   dentro de uma única requisição, não memorizar entre requisições (o que
   reintroduziria o bug que a RFC-027 §7 existe para evitar).
4. **`content_hash` não é um campo de `Image`.** Não adicione um para
   "facilitar" o `ETag` do endpoint de thumbnail — busque `IndexMetadata`
   separadamente (§4.5, §7 invariante).
5. **A forma de lista do `subprocess` não serve para o Explorer** (§4.12).
   `list2cmdline()` põe aspas em volta de `/select,C:\a b\x.jpg` inteiro, e o
   Explorer não entende isso. Um teste só com caminhos sem espaço passa com
   a forma errada.
6. **`explorer.exe` sai com código não-zero em sucesso.** `check=False`
   sempre; a resposta 204 significa "o processo foi lançado", nunca "o
   Explorer confirmou que abriu".
7. **O `FileResponse` do Starlette já gera um `ETag` — o errado.** Verificado
   na versão instalada (Starlette 1.3.1): `FileResponse` escreve o próprio
   `etag` a partir de `mtime` e tamanho do arquivo de thumbnail, e **não**
   trata `If-None-Match` nem responde 304. Duas consequências: (a) o `ETag`
   de `content_hash` precisa **sobrescrever** o gerado, e um teste afirma o
   valor exato do header, porque um `ETag` presente mas derivado do `mtime`
   passaria em qualquer teste que só checa "tem `ETag`"; (b) a comparação com
   `If-None-Match` é feita na rota, **antes** de montar o `FileResponse`,
   devolvendo `Response(status_code=304)` sem abrir o arquivo — considere as
   aspas em volta do valor e a lista separada por vírgula que o header pode
   carregar.
8. **Geração de thumbnail durante um `_encode_group()` que já falhou e caiu
   para `_encode_individually()`**: a thumbnail só deve ser tentada para
   imagens que **sobreviveram** ao encode, não para as que já viraram
   `IndexingFailure` ali dentro — gerar uma thumbnail para uma imagem cujo
   embedding falhou desperdiça o decode e deixa uma linha inconsistente
   (thumbnail sem embedding).
9. **`ImagePath.__str__()` normaliza para posix** (barras `/`), e é isso que
   deve ir para `relative_path` na resposta JSON — não `str(Path)` puro no
   Windows, que devolveria `\`.

---

## 7. As medições, e o que fazer com elas

Em `experiments/rfc-030-file-access/`, cada script com `.log`.

| `TBM` da RFC | o que medir | observação |
| --- | --- | --- |
| §7.2 custo por imagem | decode + resize + encode de thumbnail sobre o corpus de demo, comparado a ~450 ms/imagem de inferência (RFC-024 §17) | é um custo real agora (§4.1), não "quase de graça" — meça honestamente |
| §9 tamanho médio da thumbnail | KB por imagem × 100.000, para decidir se a política de retenção é urgente | |
| §12 `/reveal` guardas | latência de 404 (guarda 1 desligado) vs 204 (caminho feliz) — deve ser desprezível, é um `subprocess.Popen` sem espera | |
| §12 `If-None-Match` | confirma que um `304` não lê os bytes do arquivo do disco | verifique com um contador de chamadas ao filesystem, não só pelo tempo |

Depois de medir, reescreva a RFC como as RFCs 027, 028 e 029: `Status:
Implementado`, `Medição` apontando para os scripts, zero `TBM`, e as
correções de §4.1, §4.4, §4.5 registradas à vista com a frase original
preservada.

---

## 8. Definição de pronto

- [ ] `pytest` verde, contagem antes/depois; `pytest -m slow` verde
- [ ] `mypy` zero erros; `ruff check .` e `black --check .` limpos
- [ ] `alembic heads` único; `downgrade` + `upgrade` exercitados de verdade
- [ ] Teste: busca com dispositivo desconectado devolve 200, `absolute_path:
      null`, `device.connected: false` (RFC §4.1)
- [ ] Teste: N hits em 2 dispositivos resolvem localização com exatamente 2
      chamadas a `DeviceLocator.mount_point()` (§4.2)
- [ ] Teste: `GET /images/{id}` inexistente → 404 via `ImageNotFoundError`
      rebaseado (§4.5)
- [ ] Teste: todo erro de domínio pré-existente continua 400; a mudança de
      base de `ImageNotFoundError` é a exceção documentada (§4.5)
- [ ] Teste: `/reveal` com `allow_local_file_actions=false` → 404, nenhum
      processo lançado
- [ ] Teste: `/reveal` de cliente não-loopback → recusado mesmo com a
      configuração ligada; `/reveal` de loopback explícito → 204 (§4.11)
- [ ] Teste: `/reveal` nunca aceita string de caminho — teste de assinatura
- [ ] Teste: a linha de comando do Explorer para um caminho com espaço e com
      acento é exatamente `explorer /select,"<caminho>"` (§4.12); conferido à
      mão uma vez com um arquivo real
- [ ] Teste: `/reveal` com disco desconectado → 409 via
      `DeviceNotConnectedError` reaproveitado (§4.6)
- [ ] Teste: `/reveal` com arquivo sumido apesar do disco conectado → 410 via
      `GoneError` novo (§4.4)
- [ ] Teste: nenhuma escrita sob a raiz do dispositivo durante indexação
      (varredura pós-indexação, invariante 3)
- [ ] Teste: falha na geração de thumbnail não aparece como
      `IndexingFailure`, e a imagem é indexada com `thumbnail_path=None`
      (§4.8)
- [ ] Teste: thumbnail servida com o dispositivo de origem desconectado
      (fixa a premissa central do §4.1 da RFC — thumbnails vivem num disco
      que está sempre lá)
- [ ] Teste: o `ETag` da thumbnail é exatamente `"<content_hash>"`, e não o
      que o `FileResponse` gera sozinho (armadilha 7)
- [ ] Teste: reprocessar uma imagem (conteúdo mudou, id igual) muda o `ETag`
      da thumbnail; `If-None-Match` do hash correto → 304 sem corpo
- [ ] Teste: `thumbnail_backfill.py --root PATH` pula linhas com
      `thumbnail_generated=true`, a menos que `--force` (§4.7)
- [ ] Medições de §7 rodadas, com `.log`
- [ ] RFC-030 sem `TBM`, `Implementado`, e as correções de §4.1, §4.4, §4.5,
      §4.9 e §4.12 registradas à vista
- [ ] `ARCHITECTURE.md` §15 atualizado com a estratégia de `ETag`;
      `docs/rfcs/README.md` RFC-030 📋 → ✅

---

## 9. Estilo

- **Docstrings e comentários em inglês; RFC, README e mensagens de commit em
  português.**
- Densidade de comentário igual à do código em volta: o *porquê*, a
  alternativa recusada, a seção da RFC.
- `from __future__ import annotations` no topo de todo módulo.
- Black 88 colunas; Ruff `E,F,I,N,UP`; `mypy --strict`.
- Nada de lógica de negócio em Presentation ou Infrastructure
  (`AI_Context.md`). Resolver "este disco está conectado" é Application
  (§4.3); decidir *se* isso vira um 409 é Domain.

---

## 10. Git

**Nunca rode `git commit` ou `git push` sem pedir confirmação explícita para
aquela ação específica.** Preparar o diff, montar o stage e redigir a
mensagem é livre; executar não é. Vale mesmo que um commit anterior da mesma
sessão tenha sido aprovado.

Mensagem no molde do `22ab053` (RFC-029): título `Implementa RFC-030: ...`,
corpo em tópicos com o que mudou e por quê, linha final com contagem de
testes, mypy e alembic. Encerre com:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

## 11. O que não fazer

- Não aceite caminho de arquivo em nenhuma rota, em nenhuma forma (§3
  invariante 1).
- Não persista `absolute_path` em lugar nenhum (§3 invariante 2).
- Não escreva thumbnail sob a raiz do dispositivo (§3 invariante 3).
- Não use `shell=True`, nem monte comando por interpolação de string, no
  `/reveal` (§3 invariante 4).
- Não deixe uma falha de geração de thumbnail derrubar a indexação de uma
  imagem (§3 invariante 6, §4.8).
- Não promova `content_hash` à entidade `Image` para "facilitar" o `ETag`
  (§4.5).
- Não cacheie `mount_point()` entre requisições para otimizar §4.2 — resolva
  por dispositivo distinto dentro de uma única requisição.
- Não implemente os adaptadores macOS/Linux de revelação — porta existe,
  Windows é a única implementação (RFC §5.3, §10).
- Não implemente política de retenção do cache de thumbnails — trabalho
  futuro (RFC §9).
- Não sirva a imagem original completa, nem um visualizador — não-objetivos
  da RFC (§10).
- Não preencha um `TBM` por dedução. Se não mediu, ele fica lá e você diz
  isso.
