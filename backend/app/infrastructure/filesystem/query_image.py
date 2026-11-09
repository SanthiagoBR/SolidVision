"""Imagem enviada como consulta, decodificada antes que o modelo a veja (RFC-038 §4.2).

`POST /images/search/by-image` escreve os bytes que recebeu num arquivo
temporário e entrega o caminho ao CLIP. O adaptador o decodifica de novo em
`_preprocess` e deixa passar os erros do decodificador de propósito -- para um
arquivo indexado, eles são uma falha que merece um traceback. Para um arquivo
que o usuário acabou de soltar, são do usuário, e um JPEG cortado tem de ser um
400 que diga isso, não um 500.

Por isso este módulo abre o arquivo antes, com o mesmo Pillow, e recusa aquilo
em que o adaptador engasgaria: bytes que não são imagem, um formato diferente do
declarado, mais pixels que `Image.MAX_IMAGE_PIXELS` -- a proteção padrão do
Pillow contra bombas de descompressão, 89.478.485 -- e um arquivo cujos pixels
não decodificam até o fim. Decodificar duas vezes, aqui e no adaptador, é o
preço, e ele está no tempo medido do RFC-038 §8.

Infraestrutura, e não a dependência que a chama: `PIL` fica fora de
`app/presentation/api/` (`tests/test_ai_layer_boundaries.py`).
"""

from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.domain.exceptions import UnreadableQueryImageError

PILLOW_FORMATS = {"image/jpeg": ("JPEG", "MPO"), "image/png": ("PNG",)}
"""Os media types que uma imagem de consulta pode ter, e como o Pillow chama cada um.

**Uma foto DJI é `MPO` para o Pillow**, não `JPEG`: um JPEG com uma segunda
imagem, a prévia, depois da primeira (o Multi-Picture Format da CIPA). Todos os
40 arquivos DJI reais do RFC-032 abrem como `MPO`, e a primeira versão desta
checagem recusou todos -- as fotos que o produto existe para achar -- com um
400. A medição do RFC-038 o achou; o primeiro quadro é a foto, e é ele que o
adaptador lê."""


def inspect_query_image(path: Path, media_type: str) -> None:
    """Decodifica a imagem em `path`, ou levanta `UnreadableQueryImageError`.

    `media_type` é o que a requisição declarou, já checado contra os primeiros
    bytes; o formato que o Pillow identifica precisa concordar com ele. O limite
    de pixels é checado contra o tamanho no cabeçalho, antes de qualquer
    decodificação, então uma bomba custa a leitura de um cabeçalho.
    `MAX_IMAGE_PIXELS` é lido a cada chamada, como o próprio Pillow o lê.
    """
    expected = PILLOW_FORMATS.get(media_type)
    if expected is None:
        raise UnreadableQueryImageError(f"{media_type} is not a query image type.")
    try:
        with warnings.catch_warnings():
            # Acima do limite o Pillow só avisa, até o dobro dele; a checagem
            # abaixo recusa já no próprio limite.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as picture:
                if picture.format not in expected:
                    raise UnreadableQueryImageError(
                        f"The picture is {picture.format}, but was sent as "
                        f"{media_type}."
                    )
                width, height = picture.size
                limit = Image.MAX_IMAGE_PIXELS
                if limit is not None and width * height > limit:
                    raise UnreadableQueryImageError(
                        f"The picture has {width} x {height} pixels, more than "
                        f"the {limit} this decoder agrees to open."
                    )
                picture.load()
    except UnreadableQueryImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise UnreadableQueryImageError(
            f"The picture cannot be read: {error}"
        ) from error
    except (OSError, SyntaxError, ValueError) as error:
        # Um JPEG truncado levanta `OSError` em `load()`; um chunk PNG
        # corrompido, `SyntaxError` ou `ValueError`, conforme onde quebra.
        raise UnreadableQueryImageError(
            f"The picture cannot be read: {error}"
        ) from error
