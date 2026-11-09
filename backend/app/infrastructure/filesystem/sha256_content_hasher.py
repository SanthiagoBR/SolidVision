"""Implementação SHA-256 da porta de hash de conteúdo (RFC-024 §4)."""

from __future__ import annotations

import hashlib

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort

READ_CHUNK_BYTES = 1024 * 1024
"""Quanto de um arquivo fica em memória de uma vez durante o hash.

Não é configuração, então não está em `Settings`: nada no produto muda se isto
for 512 KiB ou 4 MiB. O que importa é que seja *limitado*. `Path.read_bytes()`
seria mais curto e funcionaria perfeitamente no corpus de demonstração de
1,8 MB, e depois alocaria o arquivo inteiro para um TIFF de 200 MB -- e isto
roda imediatamente antes da inferência em lote, que já é a parte do pipeline
que mais consome memória (RFC-024 §9).
"""


class Sha256ContentHasher(ContentHasherPort):
    """Tira a impressão digital dos bytes de uma imagem com SHA-256, em fluxo.

    Mora em `infrastructure/filesystem/` e não ao lado do caso de uso porque
    fazer o hash de um arquivo é I/O de sistema de arquivos; a camada de
    aplicação depende de `ContentHasherPort` e nunca fica sabendo que um disco
    real esteve envolvido.

    Os erros se propagam intocados, como em `ClipEmbeddingModel`: um arquivo
    ausente ou ilegível é decisão de quem chama, e quem chama já isola as
    falhas por arquivo.
    """

    def hash_image(self, image: Image) -> str:
        """Devolve o digest SHA-256 em hexadecimal dos bytes da imagem.

        Lê `Image.require_absolute_path()` em vez de unir `relative_path` a
        alguma coisa: desde o RFC-027, uma imagem registra onde mora no seu
        *dispositivo*, e só quem resolveu o ponto de montagem desse dispositivo
        sabe onde isso fica agora. Uma imagem cujo dispositivo está desplugado
        levanta `DeviceNotConnectedError` aqui, que é a resposta verdadeira --
        não há bytes para o hash.
        """
        digest = hashlib.sha256()

        with image.require_absolute_path().value.open("rb") as stream:
            while chunk := stream.read(READ_CHUNK_BYTES):
                digest.update(chunk)

        return digest.hexdigest()


def sha256_of(data: bytes) -> str:
    """A impressão digital que `Sha256ContentHasher` daria a um arquivo com `data`.

    Para a leitura antecipada do RFC-045, que lê um arquivo inteiro de uma vez e
    faz o hash do que leu em vez de abrir o arquivo uma segunda vez. Aqui, ao
    lado da versão em fluxo, para que as duas não possam divergir no algoritmo
    nem na grafia -- hex minúsculo, como a coluna o guarda; um teste as obriga
    ao mesmo digest para os mesmos arquivos.
    """
    return hashlib.sha256(data).hexdigest()
