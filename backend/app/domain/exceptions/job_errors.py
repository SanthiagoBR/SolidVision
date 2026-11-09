"""Exceções de domínio dos jobs de indexação (RFC-029)."""

from app.domain.exceptions.domain_error import (
    ConflictError,
    DomainError,
    NotFoundError,
)


class JobNotFoundError(NotFoundError):
    """Levantada quando nenhum job de indexação tem o id pedido.

    Distinta de "o job existe e não começou", que é um job `pending`
    perfeitamente comum que um cliente está consultando. Juntar os dois faria
    um erro de digitação num id ficar indistinguível de uma fila que só está
    lenta (RFC-029 §7.2).
    """

    def __init__(self, message: str = "Indexing job not found.") -> None:
        super().__init__(message)


class InvalidJobIdentifierError(DomainError):
    """Levantada quando um identificador de job não é um UUID.

    Um 400, e não um 404, e a diferença é real: `GET
    /api/v1/jobs/banana` nunca nomeou um job, enquanto um id bem formado que
    não casa com nenhuma linha nomeou, e é um `JobNotFoundError`.
    """

    def __init__(self, message: str = "Job identifier must be a valid UUID") -> None:
        super().__init__(message)


class DeviceBusyError(ConflictError):
    """Levantada quando um dispositivo já tem um job de indexação pendente ou rodando.

    A face, do lado do Python, do índice único parcial do RFC-029 §9, e nunca
    um substituto dele. O repositório traduz a recusa do banco para esta
    exceção; nada pergunta ao banco se um job existe antes de inserir um,
    porque esse seria o check-then-act que o índice existe para evitar.
    """

    def __init__(self, message: str = "Device already has an active job.") -> None:
        super().__init__(message)


class IllegalJobTransitionError(ConflictError):
    """Levantada quando se pede a um job um movimento que o estado atual proíbe.

    Cancelar um job `completed` levanta esta exceção, em vez de voltar em
    silêncio, porque o RFC-029 §8 é explícito: um chamador que pede isso age
    sobre uma suposição errada e merece ficar sabendo. Uma operação nula
    silenciosa deixaria uma UI mostrar "cancelando..." sobre um job que
    terminou há uma hora.
    """

    def __init__(self, message: str = "Illegal indexing job transition.") -> None:
        super().__init__(message)


class InvalidJobScopeError(DomainError):
    """Levantada quando um escopo não é uma pasta utilizável dentro de um dispositivo.

    Uma `DomainError` simples, portanto 400: um escopo absoluto, um escopo com
    `..`, um caminho UNC ou um que nomeia uma pasta que não está no disco são
    todos *pedidos malformados*, e nenhum deles passaria a funcionar se o
    estado do mundo mudasse. É isso que os separa de `DeviceBusyError` e
    `DeviceNotConnectedError`, que são o mesmo pedido num mau momento
    (RFC-029 §7.1, corrigido).
    """

    def __init__(self, message: str = "Invalid indexing job scope.") -> None:
        super().__init__(message)
