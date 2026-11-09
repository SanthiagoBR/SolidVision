"""Exceções de domínio próprias das imagens."""

from app.domain.exceptions.domain_error import (
    ConflictError,
    DomainError,
    NotFoundError,
)


class ImageNotFoundError(NotFoundError):
    """Levantada quando uma imagem não é encontrada.

    Mudada de base, de `DomainError` para `NotFoundError`, pelo RFC-030, que
    trouxe a primeira rota capaz de levantá-la: `GET /api/v1/images/{id}` com
    um id que não nomeia nenhuma linha é um 404 (RFC-030 §4.2), e uma
    `DomainError` simples responde 400. O mesmo movimento que o RFC-029 fez
    para `DeviceNotFoundError`, e seguro pela mesma razão -- até o RFC-030,
    nada em produção a levantava, então nenhum cliente jamais recebeu um
    status para ela.
    """

    def __init__(self, message: str = "Image not found.") -> None:
        super().__init__(message)


class ImageNotIndexedError(ConflictError):
    """Levantada quando uma imagem conhecida não tem embedding a comparar (RFC-038).

    *"Parecidas com esta"* ranqueia o índice contra o vetor guardado da foto,
    e uma linha escrita sem ele -- pelo `IndexImageUseCase`, ou antes de o
    arquivo ter sido lido -- não tem nenhum. Um conflito, e não um 400: o
    pedido idêntico dá certo assim que a foto é indexada, que é a doutrina do
    `error_handlers.py`. E não um 404, porque a foto existe.
    """

    def __init__(self, message: str = "This image has not been indexed yet.") -> None:
        super().__init__(message)


class ImageAlreadyExistsError(DomainError):
    """Levantada quando uma imagem já existe num contexto em que não deveria."""

    def __init__(self, message: str = "Image already exists.") -> None:
        super().__init__(message)


class InvalidImageIdentifierError(DomainError):
    """Levantada quando um identificador de imagem é inválido."""

    def __init__(self, message: str = "Image identifier must be a valid UUID") -> None:
        super().__init__(message)


class InvalidImagePathError(DomainError):
    """Levantada quando um caminho de imagem é inválido."""

    def __init__(self, message: str = "Invalid image path.") -> None:
        super().__init__(message)


class UnsupportedImageExtensionError(DomainError):
    """Levantada quando uma extensão de imagem não é suportada."""

    def __init__(self, message: str = "Unsupported image extension.") -> None:
        super().__init__(message)
