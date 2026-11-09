"""A decisão de pulo, extraída para que uma imagem e um lote possam compartilhá-la.

Antes do RFC-024, esta lógica vivia dentro de
`IndexOrUpdateImageUseCase.execute()`, fundida ao embedding e à persistência de
uma única imagem. Não dá para construir inferência em lote sobre essa forma --
chamar N vezes em loop um método de uma imagem continua sendo N chamadas de uma
imagem --, então a decisão foi extraída para uma função pura, que o caso de uso
de uma imagem e o coordenador de lotes chamam.

"Pura" é essencial aqui: dados o candidato, os metadados já lidos de volta da
persistência e um hasher, esta função decide e devolve. Ela não grava nada,
então quem chama fica livre para rodá-la primeiro sobre todos os arquivos
descobertos e só depois montar lotes com os sobreviventes (RFC-024 §8).
"""

from __future__ import annotations

import datetime
import enum
import weakref
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.value_objects.index_metadata import IndexMetadata


class FileContent:
    """Os bytes de um arquivo, lidos uma vez, e o seu SHA-256 (RFC-045 §5.3).

    Produzido pela thread de leitura antecipada, na infraestrutura, que leu o
    arquivo inteiro e calculou o hash do que leu; consumido aqui, onde o hash
    responde ao passo 3 de `plan_indexing()` e os bytes vão para o modelo de
    embedding e para a thumbnail, em vez de o arquivo ser aberto de novo. Cada
    consumidor continua decodificando por conta própria: o que isto carrega é
    de onde vêm os bytes, nunca uma imagem decodificada (RFC-030 §7.2).

    **Os bytes são liberados explicitamente**, pelo pipeline, assim que o
    arquivo não precisa mais deles (RFC-045 §5.5): uma decisão que o pula, um
    hash que falha, a gravação do lote em que ele entrou. Depois de
    `release()`, `data` levanta exceção em vez de devolver algo obsoleto.
    `on_release` fica sabendo quantos bytes saíram, uma vez, no primeiro entre
    `release()` e a coleta de lixo deste objeto -- um caminho que esquece de
    liberar ainda devolve os bytes, só que mais tarde. Ele é chamado só com o
    tamanho e não guarda referência a este objeto, e é isso que permite que a
    coleta aconteça.
    """

    __slots__ = ("__weakref__", "_data", "_finalizer", "content_hash", "size")

    def __init__(
        self,
        content_hash: str,
        data: bytes,
        on_release: Callable[[int], None] | None = None,
    ) -> None:
        self.content_hash = content_hash
        """O SHA-256 de `data`, como `Sha256ContentHasher` o escreve."""
        self.size = len(data)
        """Quantos bytes `data` tem; continua dizendo isso depois de liberado."""
        self._data: bytes | None = data
        self._finalizer = (
            weakref.finalize(self, on_release, self.size)
            if on_release is not None
            else None
        )

    @property
    def data(self) -> bytes:
        """Os bytes do arquivo; levanta `RuntimeError` depois de liberados."""
        if self._data is None:
            raise RuntimeError("The bytes of this file were already released")
        return self._data

    @property
    def released(self) -> bool:
        """Se `release()` foi chamado."""
        return self._data is None

    def release(self) -> None:
        """Descarta os bytes e os devolve a quem os conta. Idempotente."""
        self._data = None
        if self._finalizer is not None:
            self._finalizer()

    def __repr__(self) -> str:
        state = "released" if self.released else f"{self.size} bytes"
        return f"FileContent({self.content_hash[:12]}..., {state})"


@dataclass(frozen=True)
class IndexCandidate:
    """Um arquivo descoberto, como a camada de aplicação o vê.

    Carrega os fatos do sistema de arquivos com que a decisão de pulo compara,
    e nada sobre como o arquivo foi encontrado -- o tipo da infraestrutura que
    o produziu (`DiscoveredImageFile`) fica na infraestrutura.

    A data de captura que o scan leu (RFC-028) viaja em `image`, onde é um
    campo da entidade, em vez de vir como mais dois campos aqui ao lado de
    `file_size` e `file_modified_at` -- e a posição também (RFC-032). Esse
    lugar também é uma proteção: os campos desta classe são os sinais de
    mudança que `plan_indexing()` compara, e nem uma data de captura nem uma
    posição é um deles (RFC-028 §6.1, RFC-032 §8).
    """

    image: Image
    file_size: int | None
    file_modified_at: datetime.datetime | None
    content: FileContent | None = None
    """Os bytes do arquivo e o seu hash, quando a leitura antecipada os leu (RFC-045).

    `None` -- todo candidato montado antes do RFC-045, a CLI com a leitura
    antecipada desligada, os backfills, os testes -- significa que o pipeline
    funciona como funcionava: o hash e as decodificações leem o arquivo da sua
    localização. Também não é um sinal de mudança: `plan_indexing()` só tira o
    hash daqui depois de os passos 1 e 2 decidirem que um hash é necessário.
    """


class IndexAction(enum.Enum):
    """O que a verificação incremental em três passos concluiu sobre um arquivo."""

    EMBED = "embed"
    """O conteúdo é novo ou mudou de fato; gerar um embedding."""

    SKIP_UNCHANGED = "skip_unchanged"
    """Tamanho e mtime batem; nada a fazer, e nada foi lido."""

    REFRESH_METADATA = "refresh_metadata"
    """O tamanho ou o mtime mudou, mas os bytes não.

    O embedding guardado continua correto, então só os metadados de sistema
    de arquivos que guiam a comparação da próxima execução precisam ser
    atualizados. Este é o resultado que o passo 3 do ARCHITECTURE.md §16 existe
    para produzir, e o que o RFC-024 conta à parte no seu resumo: todo arquivo
    que cai aqui teria o embedding refeito antes deste RFC.
    """


@dataclass(frozen=True)
class IndexPlan:
    """Uma decisão sobre um candidato, mais o hash que a justificou."""

    candidate: IndexCandidate
    action: IndexAction
    content_hash: str | None


def is_unchanged(
    candidate: IndexCandidate,
    file_size: int | None,
    file_modified_at: datetime.datetime | None,
) -> bool:
    """Passo 2 da verificação incremental: os dois sinais de mudança guardados batem.

    Extraído de `plan_indexing()` pelo RFC-045 §5.2, porque um segundo chamador
    precisa da mesma regra: a thread de leitura antecipada, que decide, a
    partir de um retrato dos sinais guardados, se a decisão vai pedir os bytes
    de um arquivo, antes de lê-los. Uma função só, para que a regra de "não
    mudou" fique num lugar só.

    Um `None` guardado nunca bate com um valor que o scan leu, o que é simples
    consequência de `None != int` -- uma linha que nunca teve as colunas é
    tratada como alterada, nunca como inalterada.
    """
    return (
        file_size == candidate.file_size
        and file_modified_at == candidate.file_modified_at
    )


def plan_indexing(
    candidate: IndexCandidate,
    existing: IndexMetadata | None,
    content_hasher: ContentHasherPort,
) -> IndexPlan:
    """Roda num arquivo a verificação de custo crescente do ARCHITECTURE.md §16.

    1. Nenhuma linha (`existing is None`) -> arquivo novo.
    2. A linha existe e tanto `file_size` quanto `file_modified_at` batem ->
       pular, sem ter lido nada além da entrada do diretório.
    3. Senão, o arquivo é *candidato* a reprocessamento, então -- e só então --
       ler os seus bytes e calcular o hash. Um hash guardado que bate significa
       que o conteúdo nunca mudou e o embedding pode ficar.

    O hash também é calculado para um arquivo ainda sem linha, que o passo 1
    já condenou ao embedding. Isso não viola o "nunca calcular o hash antes
    das verificações de data e tamanho": as verificações já rodaram e já
    responderam "processe isto". É o que torna o passo 3 possível na
    *próxima* execução -- uma linha persistida sem hash é lida de volta como
    `None`, que significa desconhecido, então pular o hash aqui deixaria o
    mecanismo inteiro adormecido para sempre para todo arquivo que o sistema
    indexar.

    **A data de captura não é consultada, e nunca deve ser** (RFC-028 §6.1).
    `existing.capture_source` está nos metadados lidos antecipadamente para a
    gravação condicional da data, não para esta decisão: uma data EXIF nova
    sobre pixels idênticos é uma mudança de metadados, e respondê-la refazendo
    o embedding gastaria nela a operação mais cara do sistema.
    `tests/application/test_indexing_plan.py` falha se uma comparação for
    acrescentada.

    **Nem a posição** (RFC-032 §8), pelo mesmo motivo:
    `existing.position_source` vem na leitura antecipada para a gravação
    condicional da posição. Uma coordenada lida hoje descreve os mesmos
    pixels que descrevia ontem. Os testes variam esse campo nos metadados
    guardados e foram verificados por mutação -- acrescentar
    `existing.position_source == candidate.image.position_source` à primeira
    condição os faz falhar.

    Um hash guardado `None` nunca bate com um calculado, o que é simples
    consequência de `None != str`, e não um caso especial. As linhas gravadas
    antes do RFC-024 trazem NULL e de propósito não passaram por backfill
    (RFC-024 §15), então "desconhecido" tem de custar um embedding refeito em
    vez de arriscar um pulo errado.

    **O hash do passo 3 vem de `candidate.content` quando ele existe**
    (RFC-045 §5.3): a leitura antecipada já leu o arquivo e calculou o hash
    desses bytes, com o mesmo SHA-256 que o hasher calcula. Sem ele, o hasher
    lê o arquivo, como antes. Os mesmos três resultados de um jeito ou de
    outro, e os bytes não são lidos para um arquivo que o passo 2 pula --
    `content` só é olhado depois dele.
    """
    if existing is not None and is_unchanged(
        candidate, existing.file_size, existing.file_modified_at
    ):
        return IndexPlan(
            candidate=candidate,
            action=IndexAction.SKIP_UNCHANGED,
            content_hash=existing.content_hash,
        )

    content_hash = (
        candidate.content.content_hash
        if candidate.content is not None
        else content_hasher.hash_image(candidate.image)
    )

    if existing is not None and existing.content_hash == content_hash:
        return IndexPlan(
            candidate=candidate,
            action=IndexAction.REFRESH_METADATA,
            content_hash=content_hash,
        )

    return IndexPlan(
        candidate=candidate,
        action=IndexAction.EMBED,
        content_hash=content_hash,
    )
