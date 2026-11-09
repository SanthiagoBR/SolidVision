"""Contrato abstrato para gerar uma thumbnail dos bytes de uma imagem (RFC-030)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities.image import Image


class ThumbnailGeneratorPort(ABC):
    """Gera uma cópia pequena e exibível da foto para a qual uma `Image` aponta.

    Com a forma de `ContentHasherPort`, e pela mesma razão: decodificar uma
    fotografia é I/O mais uma biblioteca de imagem, e as duas coisas são da
    Infrastructure, enquanto *decidir quando vale a pena gerar uma thumbnail*
    é do pipeline que chama isto.

    **Toda chamada decodifica o arquivo do zero.** O RFC-030 §7.2, como foi
    proposto, dizia que uma thumbnail viria "da imagem já decodificada" para o
    CLIP, ao custo de um redimensionamento e uma codificação. Neste código,
    não tem como: a imagem decodificada é uma variável local de
    `ClipEmbeddingModel._preprocess()`, que fica atrás de
    `EmbeddingModelPort` e só devolve um tensor de tamanho fixo. Devolver a
    imagem decodificada por essa porta foi considerado e recusado -- daria
    ao adaptador de embedding uma responsabilidade que nada tem a ver com
    embeddings, e faria todo dublê de embedding aprender a desenhar
    thumbnails. Então esta é uma quarta abertura independente do arquivo, ao
    lado do hash, da leitura do cabeçalho EXIF e da decodificação do modelo,
    cada uma para um propósito estreito.

    **Desde o RFC-045, não precisa ser uma quarta leitura do disco.** O
    chamador pode entregar os bytes que já leu para o hash (`content`), e a
    implementação decodifica esses. Continua sendo uma decodificação própria
    -- a recusa acima continua de pé -- só a origem dos bytes muda.

    As implementações leem `Image.require_absolute_path()`, que levanta
    `DeviceNotConnectedError` para um dispositivo desmontado, e deixam os
    erros de decodificação e de I/O se propagarem em vez de devolver um
    marcador -- o chamador decide se uma thumbnail que falhou vale ser
    informada, e o RFC-030 decidiu que ela nunca derruba a indexação da
    imagem.
    """

    @abstractmethod
    def generate(
        self, image: Image, max_edge: int, content: bytes | None = None
    ) -> bytes:
        """Devolve os bytes codificados da thumbnail de `image`, decodificada do zero.

        O lado maior do resultado tem no máximo `max_edge` pixels, e a
        proporção é preservada. Uma origem menor que isso nunca é ampliada:
        uma thumbnail ampliada custa bytes e não mostra nada que o original
        não mostrasse.

        `content`, quando dado, são os bytes do arquivo, já lidos (RFC-045
        §5.3), e o resultado precisa ser o que o arquivo teria dado. `None`
        lê a imagem do local dela, como antes.
        """
