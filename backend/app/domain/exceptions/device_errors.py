"""Exceções de domínio próprias dos dispositivos (RFC-027)."""

from app.domain.exceptions.domain_error import (
    ConflictError,
    DomainError,
    NotFoundError,
)


class InvalidDeviceIdentifierError(DomainError):
    """Levantada quando um identificador de dispositivo é inválido."""

    def __init__(self, message: str = "Device identifier must be a valid UUID") -> None:
        super().__init__(message)


class InvalidVolumeIdentityError(DomainError):
    """Levantada quando a identidade de um volume é vazia ou de plataforma desconhecida.

    A identidade é a única coisa entre um disco remontado e uma segunda
    reindexação completa (RFC-027 §2.1), então uma vazia ou não reconhecida
    tem de falhar de forma ruidosa na construção. A alternativa -- aceitar
    `""` e deixá-la chegar à restrição `UNIQUE` -- fundiria todo volume não
    identificável numa única linha de dispositivo.
    """

    def __init__(self, message: str = "Invalid volume identity.") -> None:
        super().__init__(message)


class DeviceNotFoundError(NotFoundError):
    """Levantada quando um dispositivo não é encontrado.

    Mudada de base, de `DomainError` para `NotFoundError`, pelo RFC-029, que
    trouxe a primeira rota capaz de levantá-la: `POST /api/v1/jobs` nomeia um
    dispositivo, e "esse id de dispositivo não é um dos meus" é um 404, e não
    um 400. Nada que já respondia 400 mudou de status, porque até o RFC-029
    nenhuma rota podia levantá-la.
    """

    def __init__(self, message: str = "Device not found.") -> None:
        super().__init__(message)


class DeviceNotConnectedError(ConflictError):
    """Levantada quando uma operação precisa de bytes de um volume não montado.

    A falha honesta para o caso que o RFC-027 §2.3 descreve: o sistema sabe
    exatamente qual imagem se quer e exatamente em que disco ela está, e esse
    disco está numa gaveta. Lê-lo é impossível e nenhuma quantidade de novas
    tentativas muda isso -- o que o usuário precisa é do nome do disco a
    plugar, que quem levanta a exceção deve pôr na mensagem.

    Distinta de um simples `FileNotFoundError` de propósito. "O arquivo
    sumiu" e "o disco está desplugado" pedem reações opostas: a primeira
    justifica descartar uma linha; a segunda nunca pode ter essa permissão.

    Uma `ConflictError` desde o RFC-029, que corrigiu a própria §7.1: o
    rascunho mandava "dispositivo desconectado" para 400, e 400 diz que o
    pedido precisa ser corrigido. Não precisa -- o pedido idêntico dá certo
    assim que o disco é plugado, que é exatamente o que 409 descreve.
    """

    def __init__(self, message: str = "Device is not connected.") -> None:
        super().__init__(message)
