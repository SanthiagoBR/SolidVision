# Prompt de aplicação — RFC-031: Dispositivos, Volumes e Pastas pela API

> Documento de trabalho. Entrada para quem (ou o que) vai implementar a RFC-031.
> A autoridade é `docs/rfcs/rfc-031-dispositivos-e-pastas-pela-api.md`; este
> arquivo não a substitui — ele diz **como aplicá-la neste código**, aponta onde
> a RFC contradiz o código de hoje (ou presume algo que não existe nele), e fixa
> as decisões que faltavam.
>
> A RFC-031 é a primeira da Sprint 6 e foi escrita depois de 027–030 estarem
> implementadas. A maior parte do que ela presume existe. Mas ela tem **duas
> premissas centrais que não se sustentam contra o código real** — `/volumes`
> não é "uma projeção direta de `mounted_volumes()`" (§4.1) e "`register_device`
> migra para a Application" é impossível na forma escrita, porque a assinatura
> da função importa Infrastructure inteira (§4.2) — mais uma rota que faria N
> enumerações onde a própria RFC exige uma (§4.3), um teste de assinatura que
> não pode ser o mesmo do `/reveal` (§4.7) e um `422` que o Pydantic não dá de
> graça (§4.8). **Leia a seção 4 antes de escrever qualquer linha.**

---

## 0. Missão

Implementar a RFC-031 no `develop` do SolidVision: as seis rotas que dão a um
cliente HTTP um `device_id` e um `scope` válidos sem consultar o banco à mão —
`GET /api/v1/devices`, `GET /api/v1/volumes`, `POST /api/v1/devices`,
`PATCH /api/v1/devices/{id}`, `GET /api/v1/devices/{id}/folders` e
`GET /api/v1/capabilities`.

O critério de aceite é o das RFCs 027–030: código com a mesma densidade de
docstrings explicando *por que*, `mypy --strict` com zero erros, as
implementações de repositório indistinguíveis sob teste de contrato, e cada
número medido escrito de volta no documento.

**Nenhuma migration.** Isto é uma afirmação a verificar, não a presumir: se
alguma decisão desta implementação exigir coluna nova, ela é a decisão errada
(§3 invariante 5).

### 0.1 Pré-condição

`git status` na raiz **não está limpo**, e é preciso saber o que é o quê antes
de começar:

```
M docs/rfcs/README.md                                    ← Sprint 6 + linha do RFC-031
?? docs/rfcs/rfc-031-dispositivos-e-pastas-pela-api.md   ← a própria RFC
?? FRONTEND_SCAFFOLD.md                                  ← não é entregável desta RFC
?? logs/                                                 ← lixo de execução
```

Os dois primeiros são a RFC e o índice que a anuncia; eles entram no mesmo
commit desta implementação. `FRONTEND_SCAFFOLD.md` e `logs/` **não são desta
RFC** — não os inclua no stage e não os "arrume" de passagem.

Linha de base, medida nesta sessão, no `backend/`:

- `alembic heads`: **`f4b9e2d7c615`** (único).
- `mypy app`: **zero erros em 124 arquivos.**
- `pytest --collect-only -q`: **1429 testes coletados, 58 deselected**
  (~5 min só para coletar — o import do torch domina).
- `pytest` completo: **não rodado nesta preparação**, e o Docker estava
  parado, portanto todo teste `[postgres]` teria dado erro de conexão.
  `pytest -m slow`: não rodado.

**Suba o Postgres (`docker compose up -d`) antes de qualquer coisa, rode
`pytest` e `pytest -m slow` e anote os números reais.** Um erro de conexão no
meio da suíte não é "linha de base com N erros".

---

## 1. Leia isto antes de escrever qualquer linha

| arquivo | por quê |
| --- | --- |
| `docs/rfcs/rfc-031-dispositivos-e-pastas-pela-api.md` | a especificação |
| `experiments/rfc-030-file-access/build-prompt.md` | o molde deste documento, e a convenção de escrever a correção de volta na RFC com a frase original à vista |
| `backend/app/infrastructure/filesystem/volume_identity_provider.py` | `mounted_volumes()` devolve `dict[VolumeIdentity, Path]` — **e mais nada**. É a origem de §4.1 |
| `backend/app/domain/services/device_locator.py` | o port de Domain que já existe para "onde está este disco"; o molde do port novo, e o motivo de ele **não** ser o port novo (§4.2) |
| `backend/app/application/use_cases/resolve_image_location.py` | o precedente exato de "pergunte ao SO uma vez por disco distinto", com o teste que conta chamadas — e o exemplo de por que agrupar por dispositivo **não basta** aqui (§4.3) |
| `backend/tests/application/test_application_architecture.py` | 30 linhas de `ast.walk` que proíbem `app.application` de importar `app.infrastructure`. É o que torna §4.2 obrigatório, não opcional |
| `backend/app/infrastructure/workers/indexing_worker.py` `register_device()` | a função que a RFC manda migrar, e a assinatura que impede a migração literal |
| `backend/app/infrastructure/workers/device_reconcile.py` | o segundo chamador de `register_device()`, com `persist=False` |
| `backend/tests/infrastructure/workers/test_cli_job_equivalence.py` | **existe** (a RFC §15 está certa sobre isso); é a rede de segurança da migração |
| `backend/app/application/use_cases/create_indexing_job.py` | a validação de escopo que `/folders` tem que aplicar **igual**, porque o que `/folders` devolve é o que o cliente manda de volta em `scopes[]` |
| `backend/app/domain/value_objects/job_scope.py` | `contains()` — a relação de cobertura de §8.2 já está escrita, testada, e é de Domain (§4.9) |
| `backend/app/presentation/local_file_actions.py` | os dois guardas; `/capabilities` leva **só** `require_loopback_client` |
| `backend/tests/presentation/test_reveal_guards.py` | o molde do teste de assinatura — e o que nele **não** dá para copiar (§4.7) |
| `backend/app/presentation/schemas/job_schema.py` | o estilo dos schemas, e a decisão de `scopes: list[str]` em vez de um tipo validado — o precedente direto de §4.6 |
| `backend/app/presentation/error_handlers.py` | `NotFoundError`→404, `ConflictError`→409, `GoneError`→410, resto→400. Esta RFC **não acrescenta base nenhuma** |
| `ARCHITECTURE.md` §15 | `last_scan_file_count` é o denominador declarado, e quem o exibe mostra a data — a origem de §4.2 da RFC |

---

## 2. O que já existe (verificado, não presumido)

- **`DeviceRepository.list()`** já existe e já devolve todos os dispositivos.
  `get_by_volume_identity()` também. Nada de novo no port de dispositivo.
- **`InvalidVolumeIdentityError` e `DeviceNotConnectedError` já existem**, com
  as bases certas: a primeira é `DomainError` puro (→400), a segunda é
  `ConflictError` (→409). São exatamente os dois erros que a RFC §6 pede, e
  **nenhum erro novo precisa ser criado para o registro**.
  `DeviceNotFoundError(NotFoundError)` (→404) idem, para o `PATCH`.
- **`VolumeIdentity.__post_init__` já recusa** valor vazio e `kind` que não seja
  um `VolumeKind`, levantando `InvalidVolumeIdentityError`. A validação de §6
  já está escrita — o que falta é **não** interceptá-la antes (§4.6).
- **`JobScope` + `DeviceLocator.resolve_scope()`** já fazem a validação inteira
  de §8: `..`, âncora de unidade, UNC, e a resolução de junction NTFS contra a
  raiz do dispositivo. `CreateIndexingJobUseCase._resolve()` é o chamador a
  copiar, literalmente.
- **`JobScope.contains()`** já implementa a relação de prefixo **em partes**, não
  em texto, com o teste que fixa `2018` *não* contendo `2018b`. A tabela de
  estados de §8.2 é essa relação nas duas direções e nada mais (§4.9).
- **`IndexingJobRepository.list(device_id=None, status=None)`** já existe, já
  devolve **mais novo primeiro** por contrato, e `IndexingJobModel.scopes` é
  `lazy="selectin"` — os escopos vêm junto, numa segunda consulta, sem N+1 e
  sem risco de lazy load depois da sessão fechar.
- **`JobStatus.is_active`** já nomeia `{pending, running}` num só lugar,
  compartilhado com o índice parcial do banco.
- **`relative_path` é gravado em posix** (`ImagePath.__str__` é
  `value.as_posix()`), então o `/` do prefixo de §8.2 é o separador que está
  mesmo na coluna. Não há `\` no banco a tratar.
- **`images` tem `UNIQUE (device_id, relative_path)`**
  (`uq_images_device_relative_path`), logo existe um índice B-tree sobre o par.
  Se ele serve ao `LIKE` de §8.2 é outra conversa, e a resposta provável é não
  (§4.11).
- **`app/presentation/api/v1/__init__.py` é quem inclui os routers**, não
  `main.py` — `main.py` tem quatro linhas e só reexporta `app` (§4.13).
- **`FakeDeviceLocator` é filesystem-backed** (`tmp_path` como ponto de
  montagem), não um dicionário de respostas declaradas. `list_folders()` no
  fake é um `iterdir()`, e é assim que ele continua dando a mesma resposta que
  o adaptador real daria.

---

## 3. Invariantes que não podem ser quebrados

1. **Nenhum caminho do cliente vira um caminho no servidor.** `POST /devices`
   recebe `volume_identity`, que é uma **chave opaca** — comparada por
   igualdade num mapa que o servidor acabou de produzir, nunca parseada, nunca
   concatenada. O `root: Path` do registro é o `mount_point` que o próprio
   servidor enumerou (RFC §5.1).
2. **`mount_point` e `connected` nunca são persistidos nem cacheados entre
   requisições.** Uma enumeração por requisição, resolvida contra a lista
   inteira (RFC-027 §7). O port novo herda a proibição do `DeviceLocator`,
   escrita na docstring.
3. **Nenhum campo de porcentagem, em nenhum corpo.** Nem
   `percent_indexed` no dispositivo, nem nada por pasta. A API devolve as
   partes e a data (RFC §4.2, §8.2).
4. **Nenhuma contagem de arquivos do disco por pasta.** `/folders` faz
   `readdir` para saber os *nomes*; não varre subárvore, não conta arquivos,
   não recursa (RFC §2.3, §8.3).
5. **Nenhuma migration, nenhuma coluna, nenhuma entidade nova em
   `domain/entities/`.** Se a implementação parecer precisar de uma, pare e
   releia §8.2 — a informação já está em `devices`, `images` e `indexing_jobs`.
6. **`GET /devices` faz exatamente uma enumeração do sistema de arquivos**,
   com N dispositivos e M volumes montados. Verificado por espião que conta
   chamadas, não por inspeção (RFC §4.1).
7. **`PATCH` só aceita `label`.** Qualquer outro campo no corpo é 422 e a linha
   fica intacta. Nada mais do `Device` é editável por um cliente (RFC §7).
8. **A Application não importa Infrastructure.** Vale para todo arquivo novo em
   `app/application/`, e é o que decide a forma de §4.2.
9. **Nenhuma rota indexa nada dentro da requisição.** A proibição do RFC-026 §3
   continua inteira; `/folders` lê nomes de diretório e nada mais.

---

## 4. As decisões que a RFC deixou em aberto — ou errou

**Toda decisão abaixo deve ser escrita de volta na RFC**, na seção
correspondente, com a frase original preservada e a correção à vista — a
convenção do `docs/rfcs/README.md` que as RFCs 027–030 já seguem.

### 4.1 `mounted_volumes()` não sabe o rótulo nem o tamanho — `/volumes` não é "uma projeção direta"

RFC §5: *"A rota é uma projeção direta de
`VolumeIdentityProvider.mounted_volumes()` cruzada com `DeviceRepository.list()`."*

Verificado: `mounted_volumes()` devolve `dict[VolumeIdentity, Path]`. O corpo de
`/volumes` pede `filesystem_label` e `total_bytes`, e **nenhum dos dois está
ali**. Quem os conhece é `ResolvedVolume`, que só sai de `resolve(path)` — uma
chamada por volume, com `GetVolumeInformationW` e `GetDiskFreeSpaceExW` cada.

Há duas saídas, e a escolha não é de gosto:

* **(a) enriquecer `mounted_volumes()`** para devolver `ResolvedVolume`. Faz
  *toda* chamada pagar rótulo e capacidade — e `mount_point()` é chamado em
  cada página de busca, via `ResolveImageLocationUseCase`. `GetDiskFreeSpaceExW`
  num HD externo em repouso pode **acordar o disco e bloquear por segundos**.
  Seria um custo novo no caminho quente para servir uma tela que quase nunca
  abre;
* **(b) pagar o detalhe só onde ele foi pedido.**

**Decisão: (b).** O port novo (§4.2) tem **dois** métodos, e existirem dois é a
decisão, não um acidente de desenho:

```python
class VolumeCatalog(ABC):
    @abstractmethod
    def mount_points(self) -> dict[VolumeIdentity, Path]:
        """Quem está montado agora, e onde. Barato: uma enumeração."""

    @abstractmethod
    def list_mounted(self) -> list[MountedVolume]:
        """Idem, com rótulo e capacidade -- uma chamada a mais por volume."""
```

`GET /devices` usa `mount_points()`; `GET /volumes` e `POST /devices` usam
`list_mounted()`, porque o registro precisa gravar `filesystem_label` e
`total_bytes` na linha nova. **Meça os dois** (§7): o RFC-030 mediu
`mounted_volumes()` em 0,19 ms com um volume montado, e o número que falta é o
de `list_mounted()` — de preferência com um HD externo mecânico em repouso
plugado, que é o caso que justifica a decisão.

Corrija a RFC §5: não é uma projeção direta, é uma enumeração mais uma
identificação por volume, e o custo é diferente do de `/devices` de propósito.

### 4.2 "`register_device()` migra para a Application" — impossível na forma escrita

RFC §6 e §13: *"`register_device()` **migra** de
`infrastructure/workers/indexing_worker.py`"* para
`application/use_cases/register_device.py`.

A assinatura de hoje:

```python
def register_device(
    volume_provider: VolumeIdentityProvider,   # app.infrastructure.filesystem
    device_repository: DeviceRepository,
    root: Path,
    label: str,
    persist: bool = True,
) -> tuple[Device, ResolvedVolume]:            # app.infrastructure.filesystem
```

e o corpo chama `compute_device_id()`, de
`app.infrastructure.filesystem.device_identity`. São **três** importes de
Infrastructure — parâmetro, retorno e corpo. `test_application_architecture.py`
faz `ast.walk` sobre todo arquivo de `app/application/` e falha em qualquer
`from app.infrastructure...`. A migração literal quebra o teste que a própria
RFC §14 exige que continue passando.

**Decisão — o que migra é a regra, não a função inteira.** Separe as duas
coisas que hoje estão numa só:

1. **Nomear o volume** é uma pergunta ao sistema operacional, e os dois
   chamadores a fazem de formas **diferentes**: a CLI tem um caminho
   (`--root D:\fotos` → `resolve(root)`), a rota tem uma identidade
   (`volume_identity` → procurar na enumeração). Isto fica em Infrastructure,
   onde já está;
2. **Decidir que linha gravar** — a cascata de `label`, `first_seen_at`
   preservado, `last_seen_at` movido, os contadores de scan preservados — é a
   regra que a docstring diz que "diverge em duas cópias". **Esta** sobe para a
   Application, e é o que a RFC quer.

Concretamente:

* **`MountedVolume`** (`app/domain/value_objects/mounted_volume.py`): o que
  `ResolvedVolume` é, do lado do Domain — `identity`, `mount_point`,
  `filesystem_label`, `total_bytes`. A docstring repete o aviso de
  `ResolvedVolume`: `mount_point` nunca é persistido;
* **`VolumeCatalog`** (`app/domain/services/volume_catalog.py`): o port de
  §4.1, implementado por um adaptador fino sobre `WindowsVolumeIdentityProvider`
  em `app/infrastructure/filesystem/volume_catalog.py`;
* **`compute_device_id()` passa para o Domain**
  (`app/domain/services/device_identity.py`, ou um `DeviceId.for_volume()` —
  escolha uma e justifique). É `uuid5` sobre um value object de Domain, sem
  plataforma nenhuma dentro; o namespace UUID **vai junto, byte por byte**, e
  regenerá-lo órfã toda linha de `images`. A mudança é mais barata do que
  parece: em produção há **um único import** (`indexing_worker.py`); o resto é
  teste — `conftest.py`, `tests/application/fakes.py`,
  `test_image_identity.py`, `test_device_repository_contract.py`,
  `test_indexing_job_concurrency.py`, `test_indexing_job_repository_contract.py`,
  `test_search_similar_contract.py`. Mude o import, não deixe shim de reexport,
  e leve os casos de `test_image_identity.py` que são só sobre o device para
  `tests/domain/` junto com a função. **Não mova `compute_image_id` "por
  simetria"** — ele não é preciso aqui, fica em `image_identity.py` (que é de
  onde `test_image_identity.py` tira o resto dos casos), e um refactor gratuito
  nessa função é um refactor na identidade de toda imagem do sistema;
* **`RegisterDeviceUseCase`** (`app/application/use_cases/register_device.py`)
  recebe `VolumeCatalog` + `DeviceRepository`, e expõe os dois caminhos:

```python
def execute(self, identity: VolumeIdentity, label: str = "") -> RegisteredDevice:
    """Rota: acha a identidade na enumeração, ou 409."""

def register(self, volume: MountedVolume, label: str, persist: bool = True) -> Device:
    """A regra de merge, sem tocar no SO. O que a CLI chama."""
```

* **`indexing_worker.register_device()` fica onde está, encolhido**: resolve
  `root`, converte `ResolvedVolume` → `MountedVolume`, delega a
  `RegisterDeviceUseCase.register()`, devolve `(device, volume)` como hoje.

**A assinatura pública de `register_device()` não muda**, e isso é mais
importante do que a RFC §6 sugere ao falar em "um worker e um script": os
chamadores são **cinco**, e todos passam `root`, `label` e leem a tupla de
volta —

```
app/infrastructure/workers/indexing_worker.py      (main)
app/infrastructure/workers/device_reconcile.py     (com persist=False)
app/infrastructure/workers/capture_date_backfill.py
app/infrastructure/workers/thumbnail_backfill.py
dataset_tools/seed_demo.py
```

mais `test_indexing_worker.py` e `test_device_reconcile.py`, que a chamam
direto com argumentos posicionais. Se a refatoração exigir tocar em qualquer um
dos cinco, ela está errada — volte e encolha a função em vez de mudá-la.
`test_cli_job_equivalence.py` é a prova de que a CLI e o job continuam fazendo
a mesma coisa.

**Alternativa recusada:** mover `VolumeIdentityProvider` e `ResolvedVolume`
inteiros para o Domain. `WindowsVolumeIdentityProvider` é `ctypes` e `kernel32`
no mesmo módulo do port; separar os dois é um refactor de RFC-027 dentro de um
RFC de API, e o port de Domain que esta RFC precisa é mais estreito que ele
(duas perguntas, nenhuma sobre "que volume tem este caminho").

**Alternativa recusada:** um quarto método em `DeviceLocator`. O `DeviceLocator`
responde perguntas **sobre um dispositivo que já existe** e recebe um `Device`
em toda assinatura. A pergunta de `/volumes` é sobre a **máquina**, e sobre
volumes que ainda não são dispositivo nenhum — não há `Device` para passar.
Fundir as duas faria `FakeDeviceLocator` ter duas identidades.

### 4.3 `GET /devices` com uma enumeração: o agrupamento do RFC-030 **não** basta

RFC §4.1 exige uma única chamada a `mounted_volumes()` por requisição, e cita o
RFC-030 como precedente. Mas o que o RFC-030 fez foi agrupar **por dispositivo
distinto**: `ResolveImageLocationUseCase` chama `locator.mount_point(device)`
uma vez por disco da página. Com 10 fotos em 2 discos, 2 enumerações — ótimo
para busca, **e exatamente o bug aqui**: `/devices` tem N dispositivos
distintos por definição, então reusar esse caminho daria N enumerações, que é o
que a RFC proíbe por escrito.

**Decisão:** `ListDevicesUseCase` **não usa `DeviceLocator`**. Ele chama
`VolumeCatalog.mount_points()` **uma vez**, e resolve a lista inteira contra o
mapa:

```python
mounted = self._catalog.mount_points()          # uma enumeração, sempre
for device in self._devices.list():
    mount_point = mounted.get(device.volume_identity)   # sem I/O
```

O teste de §14 ("espião conta chamadas com N=20, falha em 2") espiona o
`VolumeCatalog`, e tem que existir com N ≥ 2 — com N=1 ele passa com a
implementação errada.

**E o mesmo raciocínio vale para as outras duas fontes da resposta**, que a RFC
não menciona:

* **`indexed_images`**: um `COUNT(*)` por dispositivo é N consultas.
  `ImageRepository.count_by_device()` tem que devolver **um mapa**
  (`dict[DeviceId, int]`, de um `GROUP BY device_id`), não um inteiro para um
  id. Nomeie no plural se ajudar a não errar;
* **`active_job`**: `IndexingJobRepository.list(device_id=...)` por dispositivo
  também é N consultas. Use o filtro que já existe **sem** `device_id`:
  `list(status=PENDING)` + `list(status=RUNNING)` são **duas** consultas
  independentes de N, e o índice parcial de RFC-029 §9 garante no máximo um
  job ativo por dispositivo, então o agrupamento em memória não tem ambiguidade
  a resolver. Não acrescente método ao port para isso.

Custo total de `GET /devices`: 1 enumeração + 1 `SELECT` de dispositivos + 1
`GROUP BY` + 2 `SELECT` de jobs (mais 2 de `selectin` para os escopos). Fixe
isso num teste que conta consultas ou chamadas, porque é a diferença entre a
tela abrir e a tela travar com 20 discos.

### 4.4 `/folders`: `has_children` é o custo real da rota, e §8.3 não o menciona

RFC §8.3: *"Não conta arquivos no disco, não recursa, não pagina."* Mas o corpo
de §8 tem `has_children: true` por pasta — e saber se `2018/janeiro` tem
subpastas exige **abrir `2018/janeiro`**. Com 40 subpastas, são 41 `readdir`,
não 1. Num HD mecânico frio é justamente a leitura que o RFC-029 §7.2 diz ser
dominante.

Isto não está errado, está **não declarado**, e a diferença importa porque o
§8.1 vende a rota como "um `readdir` por navegação".

**Decisão:** manter `has_children` — sem ele a UI desenha uma seta de expansão
em pasta folha e descobre o erro ao clicar — e **declarar e medir o custo**. A
porta devolve o dado pronto, não um nome cru:

```python
@dataclass(frozen=True)
class FolderEntry:
    name: str
    has_children: bool

def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
```

O adaptador usa `os.scandir()` e **curto-circuita no primeiro subdiretório
encontrado** (`any(e.is_dir() for e in os.scandir(child))` sai na primeira
entrada que seja pasta), em vez de materializar a listagem do filho. `None`
significa "isto não é uma pasta deste dispositivo", igual a `resolve_scope()`.
Um `OSError` num filho — permissão, disco que sumiu no meio — vira
`has_children=False` e um log, nunca uma exceção que derruba a listagem
inteira: um diretório protegido não é motivo para não mostrar os que
responderam (é a mesma regra que `mounted_volumes()` já aplica a volumes que
se recusam a ser nomeados).

Se a medição de §7 mostrar que os 41 `readdir` custam caro num disco frio, a
saída é registrar o número na RFC e discutir `has_children` no RFC-032 — **não**
é otimizar por palpite agora.

### 4.5 `/folders` devolve a grafia canônica, não a que o cliente mandou

`DeviceLocator.resolve_scope()` devolve o escopo **como o sistema de arquivos o
escreve** — é metade da razão de o método existir (Windows é
case-insensitive, e `normalize_scopes()` compara partes exatamente). Um cliente
que pede `?path=FOTOS/2018` numa pasta chamada `Fotos/2018` precisa receber
`"path": "Fotos/2018"` de volta, porque é esse texto que ele vai mandar em
`scopes[]` no `POST /jobs`.

**Decisão:** o `path` da resposta é `str(resolved_scope)`, nunca o parâmetro
recebido. O `path` de cada filho é `f"{resolved}/{name}"` com `/`, montado a
partir da grafia canônica do pai e do nome que o `scandir` reportou. `parent` é
o mesmo caminho sem a última parte, e `""` na raiz — deriváveis de
`JobScope.parts`, sem manipulação de string.

`?path=` ausente, vazio ou `.` é a raiz, porque é o que `JobScope("")` já
decide (a normalização do `PurePosixPath` já derruba `.`). Não escreva um
segundo tratamento para isso na rota.

A ordem: ordene por nome, e diga na docstring que a ordem é da rota e não do
`scandir`, que é arbitrária. Não é a ordem de varredura do
`FilesystemImageProvider` e não deve ser confundida com ela.

### 4.6 `volume_kind` é `str` no schema, não `VolumeKind` — senão o 400 vira 422

RFC §6: *"`400 InvalidVolumeIdentityError` quando está vazia ou de um `kind`
desconhecido."* Se o schema Pydantic declarar `volume_kind: VolumeKind`, o
FastAPI recusa o valor desconhecido **antes** do use case com um 422 de
validação, e o erro que a RFC especifica nunca é levantado.

**Decisão:** `volume_identity: str` e `volume_kind: str` no schema, convertidos
no use case (`VolumeKind(volume_kind)` dentro de um `try`, levantando
`InvalidVolumeIdentityError`), exatamente como `CreateJobRequestSchema` mantém
`scopes: list[str]` "porque o que torna um escopo legal é uma regra de domínio,
e respondê-la em Presentation produziria um 422 onde um 400 com mensagem
legível cabe". Copie esse parágrafo de docstring adaptado; é o mesmo argumento.

Identidade vazia já é recusada por `VolumeIdentity.__post_init__` com o mesmo
erro — não escreva uma segunda checagem.

### 4.7 O teste de assinatura de `POST /devices` **não** é o do `/reveal`

RFC §14: *"Teste de assinatura sobre handler, dependant e OpenAPI, no molde de
`test_reveal_guards.py`."* O molde afirma, entre outras coisas:

```python
assert dependant.body_params == []
```

`POST /devices` **tem** corpo. Copiar o teste dá um teste que falha, e
"consertá-lo" tirando a linha dá um teste que não afirma nada.

**Decisão:** a propriedade aqui não é *"não há entrada além do id"*, é *"nenhuma
entrada é um caminho"*. O teste afirma:

* `inspect.signature(register_device_route).parameters` é exatamente
  `{request, use_case}`;
* `dependant.path_params == []`, `query_params == []`, `header_params == []`,
  `cookie_params == []` — a identidade entra **só** pelo corpo;
* os campos do modelo de corpo são **exatamente**
  `{"volume_identity", "volume_kind", "label"}` — uma igualdade de conjuntos,
  não um `in`, para que um `path` acrescentado depois quebre o teste;
* no OpenAPI, o schema do `requestBody` tem essas três propriedades e mais
  nenhuma;
* um teste de comportamento: um corpo com `"path": "D:\\fotos"` ou
  `"root": ...` a mais é **ignorado ou recusado**, e em nenhum caso algo é
  aberto — o `VolumeCatalog` espião não é chamado com nada derivado dele.

E o teste que a RFC pede e que é o mais valioso dos cinco: **uma identidade
inventada não resolve para nada.** Mande `volume_identity:
"\\\\?\\Volume{deadbeef-...}\\"` com a enumeração vazia → 409, e
`DeviceRepository.save()` nunca chamado.

### 4.8 `PATCH` recusar tudo menos `label` exige `extra="forbid"` — nenhum schema do projeto tem isso hoje

RFC §14: *"Corpo com `last_scan_file_count` → 422 e linha intacta."* O padrão do
Pydantic v2 é `extra="ignore"`: o campo desconhecido é descartado em silêncio e
a resposta é **200**. Verificado — não há um `ConfigDict` sequer em
`app/presentation/schemas/`.

**Decisão:** `RenameDeviceRequestSchema` leva
`model_config = ConfigDict(extra="forbid")`, com uma docstring dizendo por quê:
um cliente que manda `last_scan_file_count` acha que está editando o
denominador de §4.2, e um 200 que ignora o campo confirma essa crença. É o
único schema do projeto que precisa disso hoje — **não** saia adicionando
`extra="forbid"` aos outros de passagem.

`label` é obrigatório aqui (um `PATCH` sem corpo útil não é um `PATCH`), e
vazio/só-espaços é 400 ou 422 — decida e teste; um disco chamado `""` é uma
barra lateral com uma linha em branco.

### 4.9 `list_covering` não deve ser prefix-match em SQL

RFC §13 pede `IndexingJobRepository.list_covering(device_id, paths)` — *"os jobs
que cobrem um conjunto de caminhos"*. Escrever isso em SQL significa reproduzir
a relação de cobertura **nas duas direções** (§8.2: um job em `2018` cobre
`2018/janeiro`, e um job em `2018/janeiro/casamento` cobre parcialmente
`2018/janeiro`), em **duas** implementações do port, com a comparação feita em
texto — que é exatamente o erro que `JobScope` existe para não cometer
(`2018` não é prefixo de `2018b`, mas `'2018b' LIKE '2018%'` é verdadeiro).

E a regra já está escrita, em `JobScope.contains()`, testada em
`test_job_scope.py`, e é regra de Domain — o `AI_Context.md` não a quer num
adaptador.

**Decisão:** **não acrescente `list_covering` ao port.**
`ListDeviceFoldersUseCase` chama `self._jobs.list(device_id=device.id)` — um
`SELECT` mais o `selectin` dos escopos — e deriva os estados em Python com
`JobScope.contains()` nas duas direções. A lista vem mais nova primeiro por
contrato, então "o job mais recente que cobriu este caminho" é o primeiro
`match` do laço, e a tabela de §8.2 vira cinco linhas legíveis com `scopes ==
()` (dispositivo inteiro) caindo naturalmente como "contém tudo".

Escreva na RFC §13 que o método não foi criado e por quê. **O risco a declarar:**
o histórico de um disco cresce sem limite, e um dia essa lista fica grande. A
mitigação não é SQL de prefixo, é um `limit` no port quando a medição mostrar
que importa — registre isso como dívida em §8.3, junto com a paginação.

### 4.10 `count_by_path_prefixes`: uma consulta, e a forma dela

RFC §8.2 exige **uma** consulta agrupada para N pastas. A forma que funciona
com `relative_path` em posix:

```sql
SELECT split_part(substr(relative_path, :cut), '/', 1) AS folder, count(*)
FROM images
WHERE device_id = :device AND relative_path LIKE :prefix || '/%'
GROUP BY 1
```

com `:cut = length(prefix) + 2` e, na raiz, sem o `LIKE` e com `:cut = 1`. Duas
armadilhas dentro disso:

* o `GROUP BY` traz **toda** subpasta do nível, inclusive as que não aparecem no
  `readdir` (pasta renomeada no disco e ainda indexada com o nome velho). A
  resposta lista o que o `readdir` viu; as contagens órfãs são descartadas, e
  uma pasta sem linhas em `images` recebe `0`. Não deixe uma chave do mapa
  virar uma pasta na resposta;
* imagens **direto** em `path` (sem subpasta) não entram em nenhum grupo com o
  `LIKE` acima — o `split_part` de um nome de arquivo devolveria o próprio
  arquivo. Decida e teste: ou a consulta as exclui (`relative_path LIKE
  prefix || '/%/%'`), ou o mapa é filtrado pelos nomes do `readdir` e elas
  somem por consequência. A segunda é mais simples e é o que o descarte acima
  já faz — escreva que é deliberado.

Implemente nos dois repositórios (`postgres_image_repository.py` e
`in_memory_image_repository.py`) e estenda o teste de contrato: a versão em
memória tem que dar **a mesma resposta**, inclusive nos dois casos de borda
acima, ou os testes de Application viram evidência sobre o dublê.

### 4.11 O índice que o §15 presume provavelmente não serve — verifique com `EXPLAIN`

RFC §15: *"o `LIKE 'prefixo/%'` é ancorado à esquerda, que é o caso que um
índice B-tree sobre `(device_id, relative_path)` atende."*

Isso só vale se a coluna estiver numa colação `C` ou se o índice tiver
`text_pattern_ops`. O contêiner é `pgvector/pgvector:pg17`, cuja imagem base
inicializa o cluster em `en_US.utf8` salvo `POSTGRES_INITDB_ARGS` em contrário —
e o `.env` não o define. Com colação não-`C`, o PostgreSQL **não** usa um B-tree
comum para `LIKE 'x%'`, e o índice de
`uq_images_device_relative_path` fica de fora do plano.

**Não "verifique" isso lendo a migration.** Rode, com o banco populado da
medição:

```sql
SHOW lc_collate;
EXPLAIN (ANALYZE, BUFFERS) <a consulta de §4.10>;
```

e escreva o plano na RFC. Com 100 mil linhas um `Seq Scan` pode perfeitamente
ser rápido o bastante, e nesse caso **a decisão certa é não criar índice
nenhum** e registrar o número. Se não for, a opção é um índice
`(device_id, relative_path text_pattern_ops)` — que é coluna nenhuma e tabela
nenhuma, mas é migration, e migration esta RFC disse que não teria: se chegar
aí, é uma decisão a tomar explicitamente, não a contrabandear.

### 4.12 `/capabilities`: os três campos vêm de onde

O exemplo da RFC §9 mostra `"version": "0.5.0"`. `settings.project_version` é
**`0.1.0`**, e `pyproject.toml` concorda.

**Decisão:** `version` é `settings.project_version`, lido no router e passado
adiante — não invente `0.5.0` e não "atualize" a versão do projeto de passagem;
a numeração de sprint não é a versão do pacote. `platform` é `sys.platform`
(literalmente `win32` nesta máquina, que é o valor do exemplo). `local_file_actions`
é `settings.allow_local_file_actions`.

A rota leva **`require_loopback_client` e só ele**. Escreva na docstring o
motivo inteiro de §9.1, porque é o tipo de assimetria que alguém "conserta"
acrescentando o guarda 1: com o guarda 1 a rota sumiria justamente quando
precisa responder `false`.

Um caso a testar e fácil de esquecer: **`/capabilities` de cliente remoto é
403 e o corpo não contém a configuração** — nem o `false`, nem o `platform`.
Afirme o corpo, não só o status.

Esta rota é o único caso em que um router novo precisa de `tags` própria e de
uma linha em `app/presentation/api/v1/__init__.py` além do router de
dispositivos.

### 4.13 `main.py` não registra router nenhum

RFC §13: *"`backend/main.py` | Registra os dois routers"*. `main.py` é:

```python
from app.presentation.api import app
__all__ = ["app"]
```

Quem inclui routers é `app/presentation/api/v1/__init__.py`
(`api_v1_router.include_router(...)`), e quem os exporta é
`app/presentation/api/v1/routers/__init__.py`. Corrija a linha na RFC e mexa
nos dois arquivos certos; `main.py` não muda.

### 4.14 201 e 200 na mesma rota: como, e quem decide

RFC §6.1 exige `201` no primeiro registro e `200` no segundo, na mesma rota. O
`status_code` do decorador é um só.

**Decisão:** o use case devolve o fato, a rota devolve o status.

```python
@dataclass(frozen=True)
class RegisteredDevice:
    device: Device
    created: bool
    """False quando a identidade ja tinha linha -- nada foi criado (RFC §6.1)."""
```

`created` sai de `get_by_volume_identity()`, que `register_device()` **já
chama** para preservar `first_seen_at`; não é uma consulta a mais. A rota
declara `status_code=201` (o caso normal, e o que o OpenAPI destaca), recebe
`response: Response` e faz `response.status_code = 200` quando
`created is False`, com `responses={200: {...}}` no decorador para que o
contrato publicado descreva os dois. O `label` da segunda chamada **é aplicado**
— é o mesmo efeito do `PATCH`, por um caminho que o cliente já tinha.

O teste de §14 tem que afirmar as três coisas de uma vez: 201 depois 200,
`DeviceRepository.list()` com **um** elemento, e o `label` do segundo `POST` na
linha.

---

## 5. Plano de trabalho

Cada fase termina com `pytest`, `mypy`, `ruff` e `black` limpos.

**Fase 0 — linha de base.** Suba o Postgres. Confirme §0.1: `pytest`,
`pytest -m slow`, `mypy app`, `alembic heads`. Anote os números reais.

**Fase 1 — Domain.** `value_objects/mounted_volume.py` (`MountedVolume`);
`services/volume_catalog.py` (`VolumeCatalog`, os dois métodos de §4.1, com a
proibição de cache na docstring); `services/device_locator.py` ganha
`list_folders()` e `FolderEntry` (§4.4); `compute_device_id` migra para o
Domain e os ~10 importes acompanham (§4.2); `repositories/image_repository.py`
ganha `count_by_device()` (mapa) e `count_by_path_prefixes()` (§4.3, §4.10).
**Nenhum `list_covering`** (§4.9). Nenhuma entidade nova.

**Fase 2 — Infrastructure.** `filesystem/volume_catalog.py` (adaptador sobre
`WindowsVolumeIdentityProvider`, `list_mounted()` chamando `resolve()` por ponto
de montagem, volumes que não respondem pulados como
`mounted_volumes()` já pula); `mounted_device_locator.list_folders()` com
`os.scandir` e curto-circuito (§4.4); as duas implementações de
`count_by_device`/`count_by_path_prefixes` e o teste de contrato estendido.
Rode o `EXPLAIN` de §4.11 aqui, não na fase de medição — o resultado pode mudar
a consulta.

**Fase 3 — Application.** `register_device.py` (`RegisterDeviceUseCase`, a regra
de merge + `RegisteredDevice`, §4.2 e §4.14); `list_devices.py` (uma enumeração,
um `GROUP BY`, duas consultas de job, §4.3); `list_mounted_volumes.py` (§4.1,
cruzado com `DeviceRepository.list()` para o `device_id`); `rename_device.py`;
`list_device_folders.py` (valida com `JobScope` + `resolve_scope()` exatamente
como `CreateIndexingJobUseCase._resolve()`, lista com `list_folders()`, deriva
os cinco estados com `JobScope.contains()`, §4.9). Os testes de Application vêm
com estas, com dublês que **contam chamadas**.

**Fase 4 — os cinco chamadores.** `indexing_worker.register_device()` encolhe
para o adaptador de §4.2, **assinatura pública intacta** — `device_reconcile`,
os dois backfills e `seed_demo.py` não mudam nem uma linha;
`FakeDeviceLocator.list_folders()` e um
`FakeVolumeCatalog` em `tests/application/fakes.py`. **`pytest
tests/infrastructure/workers/` inteiro verde antes de seguir** — é aqui que a
migração de §4.2 se prova ou não.

**Fase 5 — API.** `presentation/schemas/device_schema.py` (`DeviceDetailSchema`,
`VolumeSchema`, `FolderSchema`, `RegisterDeviceRequestSchema` com `str` para o
kind §4.6, `RenameDeviceRequestSchema` com `extra="forbid"` §4.8);
`routers/devices.py` (as cinco rotas, §4.14 para o 201/200);
`routers/capabilities.py` (§4.12); `dependencies/__init__.py` com
`get_volume_catalog()` e os cinco provedores; `routers/__init__.py` e
`api/v1/__init__.py` (§4.13). Os testes de assinatura de §4.7 entram aqui.

**Fase 6 — medições e documentação.** `experiments/rfc-031-devices-api/
measure_device_list.py` e `measure_folder_listing.py`, cada um com o `.log` ao
lado (§7). Depois: RFC reescrita com todo `TBM` preenchido,
`Status: Implementado`, `Medição` apontando para os scripts, e as correções de
§4.1, §4.2, §4.3, §4.4, §4.7, §4.9, §4.12, §4.13 registradas com a frase
original à vista; `docs/rfcs/README.md` RFC-031 📋 → ✅.

---

## 6. Armadilhas

1. **`DeviceLocator.mount_point()` numa lista de dispositivos é N
   enumerações** (§4.3). O caminho que o RFC-030 abençoou é o caminho errado
   aqui, e o teste que pega isso precisa de N ≥ 2.
2. **Um espião que conta chamadas só prova algo se o número for maior que 1.**
   Vale para as enumerações, para o `GROUP BY` de 40 pastas e para as consultas
   de job.
3. **`extra="forbid"` não é o padrão do Pydantic** (§4.8). Um teste de `PATCH`
   que manda um campo a mais e espera 422 falha silenciosamente como 200 sem
   ele.
4. **Declarar `volume_kind: VolumeKind` no schema troca o 400 por 422** (§4.6).
5. **`'2018b' LIKE '2018%'` é verdadeiro.** Toda comparação de cobertura passa
   por `JobScope.contains()`, que compara partes (§4.9). O `LIKE` de §4.10 é
   outra coisa — ali o `/` no fim do prefixo é o que o salva, e ele não pode
   ser esquecido.
6. **`resolve_scope()` devolve a grafia canônica, e é ela que vai na
   resposta** (§4.5). Ecoar o parâmetro do cliente dá uma pasta listável que o
   `POST /jobs` depois recusa ou normaliza para outra coisa.
7. **`GetDiskFreeSpaceExW` pode acordar um HD em repouso.** É o motivo de
   `mount_points()` e `list_mounted()` serem dois métodos (§4.1); fundi-los põe
   isso no caminho de toda busca.
8. **`register_device()` chama `get_by_volume_identity()` antes de montar a
   linha.** Não acrescente uma segunda consulta para descobrir `created` —
   ela já está ali (§4.14).
9. **`TestClient(app)` sem `client=` não é loopback** (`("testclient", 50000)`).
   O teste de `/capabilities` do caminho feliz precisa de
   `client=("127.0.0.1", 51234)` explícito, ou testa o 403 achando que testa o
   200. Está escrito no topo de `test_reveal_guards.py`.
10. **`GET /devices/{id}/folders` com o disco na gaveta é 409, e a mensagem tem
    que nomear o disco.** `DeviceNotConnectedError` já existe e a docstring
    dele diz que o raiser põe o nome na mensagem — o `PATCH`, ao contrário,
    funciona com o disco fora (RFC §7).
11. **Uma pasta sem linha em `images` é `0`, não ausente** (§4.10). Um `dict`
    vindo do `GROUP BY` acessado sem `.get(name, 0)` some com a pasta ou
    explode.
12. **Não cacheie o `VolumeCatalog` com `lru_cache` em `dependencies.py`.**
    `get_device_locator()` tem um parágrafo inteiro explicando por que ele é
    construído por requisição; o catálogo herda a regra, e o provedor novo
    deve dizer isso.

---

## 7. As medições, e o que fazer com elas

Em `experiments/rfc-031-devices-api/`, cada script com o `.log` ao lado. A
tabela de §16 da RFC, com o que esta análise acrescentou:

| `TBM` da RFC | o que medir | observação |
| --- | --- | --- |
| §4.1 / §16.1 | `GET /devices` com N = 1, 4, 20 dispositivos | ponta a ponta, com o banco populado |
| §16.2 | chamadas a `mount_points()` por requisição | alvo 1, e o teste é a prova — a medição só confirma |
| §16.3 | `count_by_device()` com 100 mil imagens | um `GROUP BY`, não N |
| **novo (§4.1)** | `mount_points()` **vs** `list_mounted()` | o RFC-030 mediu 0,19 ms para o primeiro; o segundo é o número que justifica os dois métodos. Com um HD externo mecânico plugado, se houver um |
| §16.4 / §16.5 | `/folders` na raiz e em `2018`, disco quente e disco **frio** | se o frio não for medido, **diga que não foi** |
| **novo (§4.4)** | quanto do tempo de `/folders` é o `has_children` | rode a listagem com e sem; é o número que decide se o campo sobrevive ao RFC-032 |
| §16.6 | contagem agrupada, 40 pastas / 100 mil imagens | junto com o `EXPLAIN (ANALYZE, BUFFERS)` e o `lc_collate` de §4.11 |
| §16.7 | `readdir` numa pasta com 5.000 entradas | mais o **tamanho do JSON** da resposta — é metade do argumento sobre paginação de §8.3 |

Depois de medir, reescreva a RFC como as 027–030: `Status: Implementado`,
`Medição` apontando para os scripts, zero `TBM`, e as correções de §4 à vista.

---

## 8. Definição de pronto

- [ ] `pytest` verde, contagem antes/depois (base: 1429 coletados, 58
      deselected); `pytest -m slow` verde
- [ ] `mypy app` zero erros; `ruff check .` e `black --check .` limpos
- [ ] `alembic heads` continua **um só** e continua `f4b9e2d7c615` — nenhuma
      migration nova (§3 invariante 5)
- [ ] Teste: `GET /devices` com N=20 faz **uma** enumeração (espião), **uma**
      consulta de contagem e **duas** de job (§4.3)
- [ ] Teste: `connected` reflete o momento — dois requests, locator mudando
      entre eles, `true` depois `false`
- [ ] Teste: nenhum corpo tem `percent_indexed` nem contagem de arquivos por
      pasta (afirme sobre o OpenAPI, não só sobre uma resposta)
- [ ] Teste: `/volumes` traz `device_id` preenchido para volume já registrado e
      `null` para o desconhecido
- [ ] Teste: `POST /devices` não aceita caminho — assinatura, `dependant`,
      OpenAPI e corpo com campo extra (§4.7)
- [ ] Teste: identidade inventada → 409 e `save()` nunca chamado (§4.7)
- [ ] Teste: registro duplicado → 201 depois 200, **um** dispositivo, `label` do
      segundo aplicado (§4.14)
- [ ] Teste: `volume_kind` desconhecido → **400**, não 422 (§4.6)
- [ ] Teste: `PATCH` com `last_scan_file_count` no corpo → **422** e linha
      intacta (§4.8)
- [ ] Teste: `PATCH` funciona com o disco na gaveta → 200
- [ ] Teste: `/folders` com o disco na gaveta → 409 e o corpo **nomeia** o
      dispositivo
- [ ] Teste: `/folders` recusa `..`, `C:\`, UNC e uma junction apontando para
      fora → 400, e nenhum `readdir` fora do dispositivo
- [ ] Teste: `/folders` ecoa a grafia canônica, não a grafia pedida (§4.5)
- [ ] Teste: os cinco estados de §8.2, incluindo `scopes == []`
- [ ] Teste: `partial` por cobertura parcial — job em
      `2018/janeiro/casamento` faz `2018/janeiro` ser `partial`
- [ ] Teste: 40 pastas produzem **uma** consulta de contagem (§4.10)
- [ ] Teste: pasta sem imagem indexada vem com `indexed_images: 0`, e uma
      contagem órfã (pasta renomeada no disco) não vira linha na resposta
- [ ] Teste: `/capabilities` de cliente remoto → 403 **com o corpo sem a
      configuração**; de loopback com a configuração desligada → 200 com
      `local_file_actions: false`, não 404 (§4.12)
- [ ] Teste: `test_application_architecture.py` e `test_ai_layer_boundaries.py`
      passam com `RegisterDeviceUseCase` na Application (§4.2)
- [ ] Teste: `test_cli_job_equivalence.py` e `tests/infrastructure/workers/`
      inteiros verdes — as duas CLIs não mudaram de comportamento (§4.2)
- [ ] Teste de contrato: Postgres e em-memória indistinguíveis em
      `count_by_device` e `count_by_path_prefixes`, bordas de §4.10 incluídas
- [ ] Medições de §7 rodadas, com `.log`, incluindo o `EXPLAIN` e o
      `lc_collate` de §4.11
- [ ] RFC-031 sem `TBM`, `Status: Implementado`, correções de §4 registradas com
      a frase original à vista
- [ ] `docs/rfcs/README.md` RFC-031 📋 → ✅

---

## 9. Estilo

- **Docstrings e comentários em inglês; RFC, README e mensagens de commit em
  português.**
- Densidade de comentário igual à do código em volta: o *porquê*, a alternativa
  recusada, a seção da RFC. Se um trecho existe por causa de uma medição, o
  número vai na docstring.
- `from __future__ import annotations` no topo de todo módulo.
- Black 88 colunas; Ruff `E,F,I,N,UP`; `mypy --strict`.
- Rotas síncronas (`def`, não `async def`), pelo motivo do RFC-026 §9 que
  `jobs.py` já documenta: tudo abaixo da linha é DBAPI bloqueante.
- Rota sem `try/except`: os erros de domínio já têm status
  (`error_handlers.py`), e esta RFC não acrescenta base nenhuma.
- Nada de lógica de negócio em Presentation ou Infrastructure
  (`AI_Context.md`). Derivar o estado de uma pasta é Application; decidir que
  `partial` existe é Domain; escolher o status HTTP já está decidido.

---

## 10. Git

**Nunca rode `git commit` ou `git push` sem pedir confirmação explícita para
aquela ação específica.** Preparar o diff, montar o stage e redigir a mensagem é
livre; executar não é. Vale mesmo que um commit anterior da mesma sessão tenha
sido aprovado.

Não inclua `FRONTEND_SCAFFOLD.md` nem `logs/` no stage (§0.1).

Mensagem no molde do `725617b` (RFC-030): título
`Implementa RFC-031: dispositivos, volumes e pastas pela API`, corpo em tópicos
com o que mudou e por quê, linha final com contagem de testes, mypy e alembic.

---

## 11. O que não fazer

- Não aceite caminho de arquivo em nenhuma rota, em nenhuma forma (§3
  invariante 1).
- Não devolva porcentagem em corpo nenhum, nem no dispositivo nem na pasta (§3
  invariante 3).
- Não conte arquivos do disco por pasta, nem recurse (§3 invariante 4).
- Não crie migration, coluna ou entidade (§3 invariante 5). Se parecer
  necessário, pare e releia §8.2 da RFC.
- Não mova `VolumeIdentityProvider` inteiro para o Domain, e não mova
  `compute_image_id` "por simetria" (§4.2).
- Não use `DeviceLocator.mount_point()` numa lista de dispositivos (§4.3).
- Não funda `mount_points()` e `list_mounted()` num método só (§4.1).
- Não acrescente `list_covering` ao port de jobs (§4.9).
- Não cacheie o `VolumeCatalog` entre requisições (§6 armadilha 12).
- Não implemente `DELETE /devices/{id}`, rescan sob demanda, notificação de
  montagem nem paginação de pastas — são os não-objetivos de §10 da RFC.
- Não implemente adaptador de volume para Linux ou macOS: os `VolumeKind`
  existem, o adaptador não, e continua assim (RFC-027 §4.1).
- Não comece a interface. É o RFC-032, e este RFC é a precondição dela.
- Não preencha um `TBM` por dedução. Se não mediu, ele fica lá e você diz isso.
