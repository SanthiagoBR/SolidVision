"""Formatos de requisição e de resposta da API de dispositivos (RFC-031).

Todo caminho publicado aqui é **relativo ao dispositivo**, com `/` em toda
plataforma, a forma que o RFC-027 tornou a única durável e que o RFC-030 já
usa para imagens -- um cliente JSON não deveria ter de saber qual sistema
operacional respondeu para dividir um caminho em partes.

A única exceção é `mount_point`, que é uma localização absoluta e está na
resposta de propósito: é o que "plugado em F:" quer dizer, só vale para este
instante, e é exatamente o que o banco se recusa a guardar (RFC-027 §4). Ele
é escrito no estilo posix pelo mesmo motivo de todo o resto aqui.

**Nenhum campo é uma porcentagem, e nenhum é uma contagem de arquivos no
disco.** `indexed_images`, `last_scan_file_count` e `last_scan_at` viajam
como três valores separados para que quem desenha a razão não consiga
fazê-lo sem ter a data em mãos (RFC-031 §4.2); por pasta não há denominador
nenhum, então não há razão a desenhar (§8.2).
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.use_cases.describe_devices import DeviceSummary
from app.application.use_cases.list_device_folders import DeviceFolders, FolderListing
from app.application.use_cases.list_mounted_volumes import CatalogedVolume
from app.domain.entities.indexing_job import JobStatus
from app.domain.value_objects.folder_state import FolderState
from app.presentation.schemas.job_schema import JobSchema


class RegisterDeviceRequestSchema(BaseModel):
    r"""O que um cliente envia para registrar um disco que viu em `GET /volumes`.

    **Não há campo de caminho, e nunca pode haver.** A identidade é uma
    chave opaca que o servidor cunhou; o servidor a procura numa enumeração
    que acabou de fazer e usa o *seu próprio* ponto de montagem como raiz.
    Um cliente nunca compõe uma localização, e uma identidade que um cliente
    inventou não resolve para nada -- essa é a diferença entre isto e
    aceitar `{"path": "D:\\fotos"}`, e é todo o RFC-031 §5.1.
    `test_device_registration_takes_no_path.py` afirma que o conjunto de
    campos daqui é exatamente estes três.

    Os dois campos de identidade são `str` simples, e não `VolumeIdentity` e
    `VolumeKind`, e isso é deliberado. Declarar `volume_kind:
    VolumeKind` faria o FastAPI recusar um valor desconhecido com um `422`
    antes de o caso de uso sequer rodar, e o `400
    InvalidVolumeIdentityError` que o RFC-031 §6 especifica ficaria
    inalcançável. É o argumento que `CreateJobRequestSchema` já faz para
    `scopes`: o que torna um valor válido é uma regra de domínio, e
    responder a ela na Apresentação produz um 422 onde cabe um 400 com uma
    mensagem legível.

    Campos extras são **ignorados**, que é o default do Pydantic e fica
    como está aqui. Um cliente que contrabandeia um `path` recebe um
    registro bem-sucedido do volume que nomeou, e nenhum caminho é aberto,
    que é o que o teste de guarda afirma. Só `RenameDeviceRequestSchema`
    proíbe extras, e por um motivo específico dele.
    """

    volume_identity: str = Field(
        description=(
            "The opaque identity from GET /api/v1/volumes, sent back "
            "unchanged. Never parsed, never composed into a path."
        )
    )
    volume_kind: str = Field(
        description=(
            "Which platform's identifier scheme the identity came from, "
            "e.g. 'windows-volume-guid'. An unknown kind is a 400."
        )
    )
    label: str = Field(
        default="",
        description=(
            "What to call this disk -- 'HD2'. Optional: omitted, the "
            "device keeps the name it has, or falls back to the volume's "
            "own filesystem label and then to the identity. Sent on a "
            "second registration, it is applied, exactly as PATCH would."
        ),
    )


class RenameDeviceRequestSchema(BaseModel):
    """O que um cliente envia para renomear um disco. Só o rótulo, e estrito.

    **`extra="forbid"`, o único schema do projeto que o tem.** O default é
    `extra="ignore"`, e o default está errado aqui de um jeito invisível: um
    cliente que enviasse `last_scan_file_count` receberia um `200`,
    acreditaria ter editado o denominador declarado do número "%
    indexado", e teria sido ignorado em silêncio. Uma recusa é a única
    resposta honesta, porque a crença que a requisição carrega é falsa
    (RFC-031 §7).

    Ele não está sendo acrescentado aos outros schemas deste RFC. Esta é a
    rota em que um campo que um cliente plausivelmente enviaria é um campo
    que este sistema deliberadamente não deixa ninguém gravar, e é isso que
    torna o silêncio perigoso aqui e banal nos outros lugares.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        description="The new name for this disk. The only editable field.",
    )

    @field_validator("label")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        """Apara o rótulo e recusa um que era só espaços.

        `min_length=1` sozinho aceita `"   "`, e um disco com três espaços
        no nome é uma linha da barra lateral com uma linha em branco nela.
        Recusado como `422`, e não levantado como erro de domínio: ao
        contrário de um `volume_kind` desconhecido, cuja validade é uma regra
        que este sistema tem e para a qual já tem um erro, não há regra de
        domínio sobre rótulos em lugar nenhum -- um rótulo sem nenhum
        caractere é um corpo malformado, que é exatamente o que 422 quer
        dizer.
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("label must not be blank")
        return trimmed


class ActiveJobSchema(BaseModel):
    """O job que segura um dispositivo agora, como a barra lateral precisa dele.

    Dois campos, porque dois é o que a barra lateral desenha: algo está
    acontecendo, e se já começou. Um cliente que quer contadores consulta
    `GET /api/v1/jobs/{id}`, que é a rota que os publica, em vez de tê-los
    duplicados em toda lista de dispositivos (RFC-031 §4).
    """

    id: UUID = Field(description="The job holding this device")
    status: JobStatus = Field(description="pending (queued) or running")


class DeviceDetailSchema(BaseModel):
    """Um dispositivo, resolvido no momento em que a requisição chegou."""

    id: UUID = Field(description="Identifier of this device")
    label: str = Field(description="The user's name for this disk -- 'HD2'")
    filesystem_label: str | None = Field(
        description=(
            "The name the volume reports for itself. Informative, "
            "frequently empty or 'Untitled', and never an identifier."
        )
    )
    volume_identity: str = Field(
        description=(
            "The platform-stable identity this device is derived from. "
            "Opaque: compare it for equality, never parse it."
        )
    )
    volume_kind: str = Field(
        description="Which platform's identifier scheme the identity came from"
    )
    connected: bool = Field(
        description=(
            "Whether the disk was mounted when this request was served. "
            "**Resolved per request and never stored** -- it is false from "
            "the instant a cable is pulled, and nothing would correct a "
            "stored copy. false is an answer, not an error: the system "
            "still knows every photo on this disk."
        )
    )
    mount_point: str | None = Field(
        description=(
            "Where the disk is attached right now -- 'F:/' -- or null when "
            "it is not. Absent from the database on purpose: a drive "
            "letter is a function of mount order."
        )
    )
    total_bytes: int | None = Field(description="The volume's capacity, if known")
    first_seen_at: datetime.datetime | None = Field(
        description="When this disk was first registered"
    )
    last_seen_at: datetime.datetime | None = Field(
        description=(
            "When it was last observed plugged in. History, not state -- "
            "it never answers 'is it connected now'."
        )
    )
    last_scan_at: datetime.datetime | None = Field(
        description=(
            "When the last scan counted the files on this disk. **Render "
            "this wherever you render a percentage**: the count below is "
            "the declared denominator and it ages."
        )
    )
    last_scan_file_count: int | None = Field(
        description=(
            "How many supported files the last scan found. The denominator "
            "of '% indexed', and only meaningful beside last_scan_at."
        )
    )
    indexed_images: int = Field(
        description=(
            "How many images of this device are in the index and visible -- "
            "what the search can find. The numerator. Photos the last scan "
            "did not find are not counted here but in missing_images "
            "(RFC-046). **There is no percent_indexed field**, so that no "
            "client can show the ratio without having held the scan date "
            "(RFC-031 section 4.2)."
        )
    )
    active_job: ActiveJobSchema | None = Field(
        description="The pending or running job holding this disk, if any"
    )
    missing_images: int = Field(
        description=(
            "How many indexed photos of this disk a scan of the whole disk "
            "did not find (RFC-046). Hidden from every search, listing, map "
            "and count; still in the database, with their vectors and "
            "thumbnails, until DELETE /devices/{id}/missing-images -- or "
            "until a scan finds them again."
        )
    )
    missing_since: datetime.datetime | None = Field(
        description=(
            "The scan that first missed any of them: the oldest mark. Null "
            "when missing_images is 0."
        )
    )

    @classmethod
    def from_summary(cls, summary: DeviceSummary) -> DeviceDetailSchema:
        """Monta um dispositivo resolvido para a resposta.

        Um classmethod, e não `from_attributes`, seguindo
        `JobSchema.from_domain`: o resumo aninha a entidade, e escrever o
        mapeamento por extenso mantém o formato da resposta como uma decisão,
        e não um reflexo do que quer que a dataclass calhe de guardar.

        `mount_point` é escrito com `as_posix()` para que a resposta diga
        `F:/`, e não `F:\\`. Todo outro caminho desta API é separado por `/`,
        e um cliente não deveria ter de tratar um campo à parte por causa da
        plataforma do servidor.
        """
        device = summary.device
        return cls(
            id=device.id.value,
            label=device.label,
            filesystem_label=device.filesystem_label,
            volume_identity=device.volume_identity.value,
            volume_kind=device.volume_identity.kind.value,
            connected=summary.connected,
            mount_point=(
                summary.mount_point.as_posix()
                if summary.mount_point is not None
                else None
            ),
            total_bytes=device.total_bytes,
            first_seen_at=device.first_seen_at,
            last_seen_at=device.last_seen_at,
            last_scan_at=device.last_scan_at,
            last_scan_file_count=device.last_scan_file_count,
            indexed_images=summary.indexed_images,
            active_job=(
                ActiveJobSchema(
                    id=summary.active_job.id.value, status=summary.active_job.status
                )
                if summary.active_job is not None
                else None
            ),
            missing_images=summary.missing_images,
            missing_since=summary.missing_since,
        )


class PurgedImagesSchema(BaseModel):
    """O que `DELETE /devices/{id}/missing-images` removeu (RFC-046 §5.7)."""

    purged: int = Field(
        description=(
            "How many photos left the index: every one a scan had marked as "
            "not found on this disk. Their thumbnails left the cache too."
        )
    )


class DeviceListSchema(BaseModel):
    """Todo dispositivo que o sistema conhece, ordenado por rótulo."""

    devices: list[DeviceDetailSchema] = Field(
        description="Known devices, connected and not, ordered by label"
    )

    @classmethod
    def from_summaries(cls, summaries: list[DeviceSummary]) -> DeviceListSchema:
        return cls(
            devices=[DeviceDetailSchema.from_summary(summary) for summary in summaries]
        )


class VolumeSchema(BaseModel):
    """Um volume conectado a esta máquina agora."""

    volume_identity: str = Field(
        description=(
            "The identity to send back to POST /api/v1/devices. Opaque: it "
            "is a key, not a location, and the server never accepts a path."
        )
    )
    volume_kind: str = Field(
        description="Which platform's identifier scheme this identity came from"
    )
    mount_point: str = Field(description="Where it is attached right now -- 'G:/'")
    filesystem_label: str | None = Field(
        description="The name the volume reports for itself, if any"
    )
    total_bytes: int | None = Field(description="The volume's capacity, if known")
    device_id: str | None = Field(
        description=(
            "The device this volume already is, or null when this system "
            "has never seen it. Non-null is how an 'add a device' screen "
            "shows a disk as already added instead of letting a user "
            "register it twice to no effect (RFC-031 section 6.1)."
        )
    )

    @classmethod
    def from_cataloged(cls, entry: CatalogedVolume) -> VolumeSchema:
        return cls(
            volume_identity=entry.volume.identity.value,
            volume_kind=entry.volume.identity.kind.value,
            mount_point=entry.volume.mount_point.as_posix(),
            filesystem_label=entry.volume.filesystem_label,
            total_bytes=entry.volume.total_bytes,
            device_id=str(entry.device_id) if entry.device_id is not None else None,
        )


class VolumeListSchema(BaseModel):
    """O que está plugado nesta máquina, ordenado por ponto de montagem."""

    volumes: list[VolumeSchema] = Field(
        description=(
            "Mounted volumes, registered or not. Volumes the platform "
            "refuses to name -- an empty optical drive, a locked BitLocker "
            "volume -- are skipped rather than reported as errors."
        )
    )

    @classmethod
    def from_cataloged(cls, entries: list[CatalogedVolume]) -> VolumeListSchema:
        return cls(volumes=[VolumeSchema.from_cataloged(entry) for entry in entries])


class FolderSchema(BaseModel):
    """Uma pasta dentro de um dispositivo, e o que este sistema sabe dela."""

    name: str = Field(description="The folder's own name, as the filesystem spells it")
    path: str = Field(
        description=(
            "Device-relative path, '/'-separated. **This is the exact "
            "string to send in scopes[] on POST /api/v1/jobs** -- it is "
            "the filesystem's own spelling, not whatever spelling was "
            "asked for, because the job route compares path parts exactly."
        )
    )
    has_children: bool = Field(
        description=(
            "Whether this folder holds folders of its own -- draw an expand "
            "arrow only when true"
        )
    )
    indexed_images: int = Field(
        description=(
            "How many images anywhere under this folder are in the index. "
            "An exact count, and **not** a share of anything: there is no "
            "denominator per folder, because obtaining one would mean "
            "walking the subtree (RFC-031 section 2.3). Render '1,204 "
            "indexed', never a percentage."
        )
    )
    state: FolderState = Field(
        description=(
            "indexing, queued, partial, indexed or never_indexed, derived "
            "from the device's job history. partial means some of it was "
            "walked and nothing claims all of it was -- read `job` for "
            "where it stopped."
        )
    )
    job: JobSchema | None = Field(
        description=(
            "The job this state was read from, or null when no job "
            "explains it. For a cancelled or failed one, "
            "last_processed_relative_path is where the work stopped."
        )
    )

    @classmethod
    def from_listing(cls, listing: FolderListing) -> FolderSchema:
        return cls(
            name=listing.name,
            path=str(listing.scope),
            has_children=listing.has_children,
            indexed_images=listing.indexed_images,
            state=listing.state,
            job=(
                JobSchema.from_domain(listing.job) if listing.job is not None else None
            ),
        )


class FolderListSchema(BaseModel):
    """Um nível da árvore de pastas de um dispositivo.

    `path` e `parent` são ambos grafias canônicas, ambos separados por `/`,
    e `""` quer dizer a raiz do dispositivo em qualquer um dos dois. O pai
    da raiz é a raiz, então um cliente que sobe a árvore para ali, em vez
    de cair para fora.
    """

    device_id: UUID = Field(description="The device these folders are on")
    path: str = Field(
        description=(
            "The folder that was listed, as the filesystem spells it. "
            "Empty means the device root."
        )
    )
    parent: str = Field(
        description="One level up; empty at the root, and the root's parent is itself"
    )
    folders: list[FolderSchema] = Field(
        description=(
            "The folders directly inside `path`, sorted by name. Files are "
            "not listed: what goes back in scopes[] is a folder. Not "
            "paginated -- a folder with thousands of subfolders returns all "
            "of them, a declared debt (RFC-031 section 8.3)."
        )
    )

    @classmethod
    def from_domain(cls, folders: DeviceFolders) -> FolderListSchema:
        return cls(
            device_id=folders.device.id.value,
            path=str(folders.scope),
            parent=str(folders.parent),
            folders=[FolderSchema.from_listing(listing) for listing in folders.folders],
        )
