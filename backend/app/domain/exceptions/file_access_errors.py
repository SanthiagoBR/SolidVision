"""Exceções de domínio do acesso a arquivos (RFC-030).

O RFC-030 §11 lista este módulo como a casa de dois erros, "dispositivo
desconectado" e "arquivo ausente", e só um deles é novo. O primeiro já
existia como `DeviceNotConnectedError`, levantado por
`Image.require_absolute_path()` e mapeado para 409 desde o RFC-029; uma
segunda classe para o mesmo fato seriam dois nomes sobre os quais a camada
HTTP teria de concordar, então `/reveal` levanta o que já existe.
"""

from app.domain.exceptions.domain_error import GoneError, NotFoundError


class FileGoneError(GoneError):
    """Levantada quando falta um arquivo indexado, embora o disco esteja conectado.

    O 410 do RFC-030 §5.1: a linha existe, o dispositivo está montado agora,
    e não há nada no caminho que a linha descreve -- o usuário apagou, moveu
    ou renomeou o arquivo depois da indexação.

    Deliberadamente não levantada quando o *dispositivo* está ausente. Esse é
    o `DeviceNotConnectedError`, e os dois pedem reações opostas: "plugue o
    HD3" é um conselho que funciona, enquanto, para um arquivo apagado,
    mandaria o usuário buscar um disco que não tem a foto.

    Nada descarta a linha quando esta exceção é levantada. Decidir que um
    arquivo ausente quer dizer uma linha velha é uma questão de
    reconciliação, e respondê-la de dentro de um pedido para abrir uma pasta
    faria de clicar em "mostrar no Explorer" uma ação destrutiva.
    """

    def __init__(self, message: str = "File is no longer on its device.") -> None:
        super().__init__(message)


class ThumbnailNotFoundError(NotFoundError):
    """Levantada quando uma imagem existe, mas não tem thumbnail que possa ser servida.

    Um 404 em todas as variantes, e a razão de não ser uma `GoneError` é de
    quem é o arquivo ausente. O RFC-030 §7.2 faz a UI mostrar um placeholder
    no 404, o que cobre os casos comuns -- uma imagem indexada antes do
    RFC-030, ou uma cuja thumbnail não pôde ser gerada. Cobre também uma
    linha que nomeia uma thumbnail que o diretório do cache não tem mais:
    esse cache é desta aplicação, então a ausência dela é uma limpeza ou um
    bug deste lado, e não o mundo mudando por baixo do chamador. O 410 fica
    reservado para o arquivo do próprio usuário, que é o caso que `/reveal`
    existe para relatar.
    """

    def __init__(self, message: str = "Thumbnail not found.") -> None:
        super().__init__(message)
