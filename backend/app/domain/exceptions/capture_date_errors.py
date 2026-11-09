"""Exceções de domínio da data de captura (RFC-028)."""

from app.domain.exceptions.domain_error import DomainError


class InvalidCaptureDateError(DomainError):
    """Levantada quando uma data de captura e a sua fonte não descrevem um só fato.

    No mais das vezes: um `datetime` com fuso. O EXIF registra a hora local do
    relógio da câmera, sem fuso (RFC-028 §5), então um valor com fuso quer
    dizer que alguma camada inventou um, e o fuso inventado é exatamente o que
    leva uma foto do réveillon para o ano errado.
    """

    def __init__(self, message: str = "Invalid capture date.") -> None:
        super().__init__(message)


class InvalidDateRangeError(DomainError):
    """Levantada quando um intervalo de datas não pode ser avaliado como pedido.

    Uma subclasse de `DomainError`, para que a camada HTTP responda 400 sem
    um handler próprio: um intervalo invertido, ou um limite enviado com
    deslocamento de UTC, é um pedido bem formado que a aplicação recusa --
    que é o que 400 quer dizer lá, ao contrário do 422 do FastAPI para um
    pedido que nem é interpretável.
    """

    def __init__(self, message: str = "Invalid date range.") -> None:
        super().__init__(message)
