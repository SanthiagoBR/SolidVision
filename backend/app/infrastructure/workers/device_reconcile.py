"""Passo assistido de migração do RFC-027: dar um dispositivo às linhas de imagem.

    python -m app.infrastructure.workers.device_reconcile --root PATH --label HD2

Este é o passo 2 da migração em três passos do RFC-027 §6.3, e é de propósito
um entregável, e não uma linha num README: o trabalho que ele faz não pode ser
feito por `upgrade()`.

**Por que uma migration não consegue fazê-lo.** O `relative_path` de uma linha
não pode ser derivado do `path` absoluto sem saber qual prefixo era a raiz do
scan, e a raiz chegou como o argumento `--root` do worker de indexação, que
nunca foi guardado. Não há como dividir `D:/fotos/2018/x.JPG` num dispositivo e
num caminho dentro dele de dentro do banco. Então um operador fornece a metade
que falta -- a raiz -- e este comando fornece todo o resto.

**O que ele reescreve, e o que ele protege.** Cada linha que casa ganha um
`device_id`, um `relative_path` e uma **chave primária nova**, porque `ImageId`
é `uuid5` sobre a localização e a localização mudou de forma (RFC-027 §6.1).
Ele não toca em `embedding`. É esse o ponto: o RFC-024 mediu a indexação em
2,2 imagens/segundo na CPU, então um disco de 40.000 imagens são cerca de cinco
horas de inferência, e este comando existe para que nada disso se repita.

**Reescrever a chave primária é seguro aqui e não será depois.** Nada no schema
referencia `images.id` hoje -- `IndexingJobs`, `SearchHistory` e as thumbnails
estão todos desenhados no `ARCHITECTURE.md` e nenhum está construído --, então
isto é uma tabela e um `UPDATE`, sem cascata. O RFC-029 fecha essa janela
(RFC-027 §6.2).

**As linhas que ele não casa ficam exatamente como estão.** Elas são o registro
de um disco que o operador ainda não reconciliou, e cada uma guarda um
embedding que custou tempo de verdade; a próxima migration se recusa a seguir
enquanto restar alguma, que é a falha ruidosa que o RFC-027 §6.3 pede.

O comando é idempotente: linhas que já têm dispositivo são puladas, então
rodá-lo duas vezes na mesma raiz não faz nada, e rodá-lo uma vez por disco é o
uso pretendido.

O próprio dispositivo é resolvido por `register_device()`, importado do worker
de indexação em vez de reimplementado aqui. Os dois comandos cunham ids de
imagem a partir do dispositivo que resolvem, então uma segunda cópia das regras
de "achar ou criar, e quais campos sobrevivem" seria uma segunda chance de os
dois discordarem sobre o mesmo disco.

**Ele fala com o banco em SQL puro, de propósito.** Ele roda *entre* duas
migrations, contra um schema que não corresponde nem ao `ImageModel` antigo nem
ao novo: `path` ainda existe, `device_id` e `relative_path` existem mas aceitam
nulo, e ainda não há chave estrangeira. Mapear essa forma de transição no model
do ORM significaria uma classe do ORM descrevendo um schema que existe pela
duração de uma sessão do operador.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.volume_identity_provider import ResolvedVolume
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.workers.indexing_worker import register_device

logger = get_logger(__name__)


@dataclass
class ReconcileSummary:
    """O que uma execução da reconciliação fez, como dado, e não como saída de log."""

    device_id: DeviceId
    matched: int = 0
    """Linhas cujo `path` caiu sob a raiz fornecida."""

    reconciled: int = 0
    """Linhas que ganharam um dispositivo, um caminho relativo e um id novo."""

    already_reconciled: int = 0
    """Linhas sob a raiz que uma execução anterior já tinha reivindicado."""

    merged_duplicates: int = 0
    """Linhas descartadas porque uma linha *diferente* já é o mesmo arquivo.

    O único caso em que uma linha é removida, e não é uma perda: estas são
    as duplicatas que o RFC-027 §2.1 descreve -- o mesmo arquivo no mesmo
    disco indexado duas vezes, com duas letras de unidade -- convergindo
    para uma identidade só, agora que a identidade não contém mais letra. A
    linha que sobrevive mantém o embedding, e cada fusão vai para o log com
    os dois caminhos antigos.

    "Diferente" sustenta o resto. Uma linha cujo id recalculado é igual ao
    próprio id atual não é duplicata; é uma linha que já foi reconciliada
    uma vez e só precisa das colunas do dispositivo preenchidas, que é o
    estado que `b8e4d2a13c75.downgrade()` deixa. Contar isso como fusão
    esvazia a tabela.
    """

    remaining_unreconciled: int = 0
    """Linhas em qualquer ponto da tabela que ainda não têm dispositivo.

    Relatadas para que o operador saiba que há outro disco a plugar *antes*
    de a próxima migration se recusar a rodar, e não pela mensagem de erro
    dela.
    """

    merges: list[tuple[str, str]] = field(default_factory=list)

    def format_report(self) -> str:
        return (
            f"Reconciled device {self.device_id}: matched={self.matched}, "
            f"reconciled={self.reconciled}, "
            f"already_reconciled={self.already_reconciled}, "
            f"merged_duplicates={self.merged_duplicates}, "
            f"remaining_unreconciled={self.remaining_unreconciled}"
        )


def reconcile(
    session: Session,
    device: Device,
    volume: ResolvedVolume,
    root: Path,
    dry_run: bool = False,
) -> ReconcileSummary:
    """Reescreve cada linha não reconciliada sob `root` para pertencer a `device`.

    **`relative_path` é relativo ao ponto de montagem do dispositivo, não a
    `root`.** A distinção é toda a segunda metade do RFC-027 §2.2: `--root`
    é como o operador diz *quais linhas estão neste disco*, e nada mais. Se
    o caminho relativo fosse medido a partir de `--root`, reconciliar
    `D:\\fotos` e depois indexar `D:\\fotos\\2018` descreveria o mesmo
    arquivo de dois jeitos e cunharia dois ids para ele -- reintroduzindo,
    por outra porta, a duplicação que este RFC inteiro remove.

    O casamento ignora maiúsculas e minúsculas porque os caminhos do Windows
    ignoram, e um `D:/fotos` guardado precisa casar com uma raiz que o
    operador digitou como `d:\\fotos`.
    """
    summary = ReconcileSummary(device_id=device.id)
    mount_prefix = _posix_prefix(volume.mount_point)
    root_prefix = _posix_prefix(root)
    relative_root = _strip_prefix(root_prefix, mount_prefix)

    for old_id, stored_path, has_device in _rows_under(
        session, root_prefix, relative_root
    ):
        summary.matched += 1
        if has_device:
            summary.already_reconciled += 1
            continue

        relative = _device_relative(stored_path, mount_prefix, relative_root)
        if relative is None:
            # A linha casou com a raiz, mas não está sob o ponto de montagem
            # para o qual a raiz foi resolvida. Só acontece se o operador
            # apontou para um disco e a tabela tem um caminho que apenas se
            # parece com ele -- melhor deixá-la como está do que adivinhar.
            logger.warning(
                "Skipping %s: it does not sit under mount point %s",
                stored_path,
                volume.mount_point,
            )
            continue

        new_id = compute_image_id(device.id, relative).value
        if dry_run:
            summary.reconciled += 1
            continue

        if new_id != old_id and _row_exists(session, new_id):
            summary.merged_duplicates += 1
            summary.merges.append((stored_path, str(relative)))
            logger.warning(
                "Merging duplicate row for %s: another row already holds "
                "device %s / %s, and keeps its embedding.",
                stored_path,
                device.id,
                relative,
            )
            session.execute(
                text("DELETE FROM images WHERE id = :old_id"), {"old_id": old_id}
            )
            continue

        session.execute(
            text(
                "UPDATE images SET id = :new_id, device_id = :device_id, "
                "relative_path = :relative_path WHERE id = :old_id"
            ),
            {
                "new_id": new_id,
                "device_id": device.id.value,
                "relative_path": str(relative),
                "old_id": old_id,
            },
        )
        summary.reconciled += 1

    if dry_run:
        session.rollback()
    else:
        session.commit()

    summary.remaining_unreconciled = _unreconciled_count(session)
    return summary


def _rows_under(
    session: Session, root_prefix: str, relative_root: str | None
) -> list[tuple[uuid.UUID, str, bool]]:
    """Devolve `(id, path, already_has_device)` de cada linha sob uma raiz.

    Dois prefixos são tentados, porque `path` pode estar em qualquer de duas
    formas. A normal é absoluta -- `D:/fotos/2018/x.JPG` --, escrita por uma
    indexação anterior ao RFC-027. A outra é relativa ao dispositivo, e é o
    que `b8e4d2a13c75.downgrade()` deixa: o rollback não consegue restaurar
    um caminho absoluto, já que o ponto de montagem de onde ele partiu nunca
    foi guardado, então faz o backfill de `path` a partir de
    `relative_path`. Casar só a forma absoluta deixaria um banco que passou
    por downgrade impossível de reconciliar, o que faria da ida e volta
    exigida pelo RFC-007 uma porta de mão única.

    Lido por inteiro antes de qualquer escrita, em vez de iterado durante a
    atualização. O `UPDATE` abaixo muda a chave primária, então um cursor
    ainda percorrendo a tabela poderia, legitimamente, devolver a mesma
    linha uma segunda vez com o id novo -- e a segunda visita calcularia um
    caminho relativo a partir de um valor de `path` que não está mais lá.
    """
    statement = text(
        "SELECT id, path, device_id IS NOT NULL AS has_device FROM images "
        "WHERE lower(path) = lower(:root) "
        "OR lower(path) LIKE lower(:root) || '/%' "
        "OR lower(path) = lower(:relative_root) "
        "OR lower(path) LIKE lower(:relative_root) || '/%' "
        "ORDER BY path"
    )
    # Uma raiz que não está sob o ponto de montagem não tem forma relativa,
    # e o segundo par de cláusulas então simplesmente repete o primeiro, em
    # vez de ser desligado com um NULL. O PostgreSQL não consegue inferir um
    # tipo para um parâmetro `NULL` puro em `:p IS NOT NULL`, e fazer cast
    # de um só para desligar um ramo é mais maquinário do que repetir um
    # predicado que já é verdadeiro ou já é falso.
    rows = session.execute(
        statement,
        {"root": root_prefix, "relative_root": relative_root or root_prefix},
    ).all()
    return [(row.id, row.path, bool(row.has_device)) for row in rows]


def _row_exists(session: Session, image_id: uuid.UUID) -> bool:
    """Diz se alguma linha já tem este id.

    Quem chama precisa antes verificar que o id não é o que a linha já tem.
    Uma linha cujo id calculado é igual ao id atual não é duplicata de nada
    -- é ela mesma, e isso acontece sempre que a identidade já estava certa
    e só as colunas do dispositivo precisam ser preenchidas, que é
    exatamente o estado que um downgrade deixa. Tratar isso como colisão
    apagaria cada linha da tabela, uma a uma, relatando a cada vez que
    "another row already holds" a identidade que está para destruir.
    """
    statement = text("SELECT 1 FROM images WHERE id = :image_id")
    return session.execute(statement, {"image_id": image_id}).first() is not None


def _unreconciled_count(session: Session) -> int:
    statement = text(
        "SELECT count(*) FROM images "
        "WHERE device_id IS NULL OR relative_path IS NULL"
    )
    return int(session.execute(statement).scalar_one())


def _posix_prefix(path: Path) -> str:
    """Normaliza um caminho para a forma posix que `ImagePath` guarda, sem barra final.

    Os caminhos guardados passaram por `ImagePath`, que troca barras
    invertidas por barras normais, então a comparação tem de acontecer nessa
    forma, ou uma raiz do Windows não casaria com nada.
    """
    return str(ImagePath(str(path))).rstrip("/")


def _device_relative(
    stored_path: str, mount_prefix: str, relative_root: str | None
) -> ImagePath | None:
    """Transforma um `path` guardado num caminho relativo ao ponto de montagem.

    Trata as duas formas que `_rows_under()` casa. Um caminho absoluto perde
    o ponto de montagem; um caminho que já é relativo ao dispositivo -- o
    que um downgrade deixa -- é devolvido sem mudança.

    Devolve `None` quando o caminho não é nenhum dos dois, o que quem chama
    relata em vez de contornar: um caminho que casou com a raiz mas não está
    sob nenhum prefixo reconhecido significa que os dois discordam, e
    adivinhar arquivaria uma foto no disco errado.
    """
    stripped = _strip_prefix(stored_path, mount_prefix)
    if stripped is not None:
        return ImagePath(stripped) if stripped else None
    if relative_root is not None and _is_under(stored_path, relative_root):
        return ImagePath(stored_path)
    return None


def _strip_prefix(path: str, prefix: str) -> str | None:
    r"""Remove um `prefix/` do início de `path`, ignorando maiúsculas e minúsculas.

    Ignora porque os caminhos do Windows ignoram, então um `D:/fotos`
    guardado tem de casar com um ponto de montagem que o Windows informou
    como `d:\`. Devolve `None` quando `path` não está sob `prefix` de jeito
    nenhum.
    """
    normalized = prefix.rstrip("/") + "/"
    if not path.lower().startswith(normalized.lower()):
        return None
    return path[len(normalized) :]


def _is_under(path: str, prefix: str) -> bool:
    """Diz se `path` é o próprio `prefix` ou está dentro dele."""
    return path.lower() == prefix.lower() or _strip_prefix(path, prefix) is not None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.device_reconcile",
        description=(
            "Give existing image rows a device and a device-relative path, "
            "preserving their embeddings (RFC-027 section 6.3)."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "The root that was passed to the indexing worker for this disk. "
            "Required, and required to be mounted: its volume identity is "
            "what the device is derived from, and there is no way to infer "
            "it from the stored paths."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        help=(
            "The user-facing name for the disk -- 'HD2'. Optional when the "
            "device already exists, in which case its current label stands."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report what would change and roll back. Worth having for an "
            "operation that rewrites primary keys over a real photo "
            "collection."
        ),
    )
    return parser


def main() -> None:
    """Monta a reconciliação real e a roda contra uma raiz nomeada."""
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.persistence.postgres_device_repository import (
        PostgresDeviceRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal

    args = _build_arg_parser().parse_args()
    root = args.root.resolve()

    session = SessionLocal()
    try:
        device, volume = register_device(
            volume_provider=WindowsVolumeIdentityProvider(),
            device_repository=PostgresDeviceRepository(session),
            root=root,
            label=args.label,
            persist=not args.dry_run,
        )
        logger.info(
            "Device %s (%s) resolved at %s", device.label, device.id, volume.mount_point
        )
        summary = reconcile(
            session=session,
            device=device,
            volume=volume,
            root=root,
            dry_run=args.dry_run,
        )
        logger.info(
            "%s%s", summary.format_report(), " (dry run)" if args.dry_run else ""
        )
        if summary.remaining_unreconciled:
            logger.warning(
                "%d image row(s) still belong to no device. Run this command "
                "once per indexed root before applying the next migration.",
                summary.remaining_unreconciled,
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
