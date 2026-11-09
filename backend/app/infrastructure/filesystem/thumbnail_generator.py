"""Implementação em Pillow da porta de thumbnails (RFC-030 §7.2).

**Uma decodificação nova do arquivo, não um subproduto do embedding.** O
RFC-030, como proposto, esperava que as thumbnails saíssem quase de graça, da
imagem que o CLIP já tinha decodificado. Nesta base de código essa imagem nunca
sai de `ClipEmbeddingModel._preprocess()`, então este módulo abre o arquivo de
novo -- a quarta abertura independente por imagem indexada, depois do hash do
conteúdo, do cabeçalho EXIF e do modelo. O quanto isso custa está medido em
`experiments/rfc-030-file-access/measure_thumbnail_cost.py`, contra a inferência
ao lado da qual ela fica, e não contra zero.

Desde o RFC-045, a decodificação pode vir de bytes já em memória -- os que a
leitura antecipada leu para o hash -- em vez do arquivo. Continua sendo uma
decodificação própria; só muda a origem dos bytes, e a thumbnail é a mesma,
byte a byte.
"""

from __future__ import annotations

import io

from PIL import Image as PILImage
from PIL import ImageOps

from app.domain.entities.image import Image
from app.domain.services.thumbnail_generator_port import ThumbnailGeneratorPort

THUMBNAIL_FORMAT = "JPEG"
THUMBNAIL_SUFFIX = ".jpg"
"""A única codificação que toda thumbnail tem, e o sufixo pelo qual o store a nomeia.

JPEG porque thumbnails de fotos são fotos: é o menor formato amplamente
decodificável para elas, todo navegador o renderiza, e as alternativas que o
batem em tamanho (WebP, AVIF) economizam bytes numa implantação local-first que
não tem rede onde economizá-los. A transparência é achatada sobre branco
(`_flatten()`), que é a única coisa de que a escolha abre mão.
"""

JPEG_QUALITY = 85
"""Não é configuração, pela mesma razão que `READ_CHUNK_BYTES` não é.

Nada no produto muda entre 80 e 90 que um usuário fosse escolher; o que importa
é que seja fixo, para que duas execuções produzam os mesmos bytes. O tamanho
resultante por thumbnail está medido no RFC-030 §9.
"""

_SIXTEEN_BIT_MODES = frozenset({"I;16", "I;16L", "I;16B", "I;16N"})


class PillowThumbnailGenerator(ThumbnailGeneratorPort):
    """Decodifica, reduz, orienta, achata e codifica uma imagem como JPEG."""

    def generate(
        self, image: Image, max_edge: int, content: bytes | None = None
    ) -> bytes:
        """Devolve bytes JPEG cujo lado maior tem no máximo `max_edge` pixels.

        A ordem dos passos é o desempenho, e cada um está aí por um caso que os
        casos difíceis do RFC-022 no corpus de demonstração de fato contêm.

        1. **`thumbnail()` antes de qualquer coisa forçar uma decodificação
           completa.** Para um JPEG, ele pede ao decodificador um rascunho em
           escala reduzida (escala DCT para 1/2, 1/4 ou 1/8) antes de
           redimensionar, então uma foto de drone de 20 MP nunca é decodificada
           em resolução cheia. `exif_transpose()` ou `convert()` antes
           carregariam todos os pixels e jogariam isso fora. Ele nunca amplia
           uma origem menor.
        2. **A orientação EXIF é aplicada à imagem pequena.** Uma câmera segura
           na vertical grava pixels em paisagem mais uma tag de orientação;
           ignorar a tag mostra ao usuário uma foto deitada. A tag sobrevive a
           `thumbnail()`, e um giro de 90 graus mantém o resultado dentro do
           quadrado de `max_edge`. A saída não leva EXIF, então nenhum
           visualizador pode aplicar a rotação uma segunda vez.
        3. **Tudo vira RGB de 8 bits.** CMYK e tons de cinza convertem direto;
           TIFFs de 16 bits são reescalados antes, porque a conversão direta do
           Pillow corta todo valor acima de 255 e deixa uma foto de 16 bits
           quase inteiramente branca; a transparência é achatada sobre branco,
           já que JPEG não tem alfa e uma conversão simples poria as regiões
           transparentes sobre preto.

        Um arquivo de zero bytes ou truncado levanta exceção no decodificador,
        e esse é o desfecho correto: quem chama relata uma falha de thumbnail e
        a imagem continua indexada sem ela.

        `content` são os bytes do arquivo, quando quem chama já os leu
        (RFC-045 §5.3); eles são decodificados no lugar do arquivo, que então
        nem é aberto.
        """
        if max_edge < 1:
            raise ValueError(f"max_edge must be at least 1, got {max_edge}")

        source = (
            io.BytesIO(content)
            if content is not None
            else image.require_absolute_path().value
        )
        with PILImage.open(source) as opened:
            opened.thumbnail((max_edge, max_edge))
            oriented = ImageOps.exif_transpose(opened)
            picture = _flatten(_to_eight_bit(oriented))

            buffer = io.BytesIO()
            picture.save(buffer, format=THUMBNAIL_FORMAT, quality=JPEG_QUALITY)

        return buffer.getvalue()


def _to_eight_bit(picture: PILImage.Image) -> PILImage.Image:
    """Reescala uma imagem de 16 bits e um canal para 8 bits, em vez de cortá-la."""
    if picture.mode not in _SIXTEEN_BIT_MODES:
        return picture
    return picture.convert("I").point(lambda value: value / 257).convert("L")


def _flatten(picture: PILImage.Image) -> PILImage.Image:
    """Devolve uma imagem RGB, compondo qualquer transparência sobre branco."""
    has_alpha = picture.mode in ("RGBA", "LA", "PA") or (
        picture.mode == "P" and "transparency" in picture.info
    )
    if not has_alpha:
        return picture.convert("RGB")

    rgba = picture.convert("RGBA")
    background = PILImage.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background
