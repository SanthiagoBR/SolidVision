"""Exceções de domínio da posição (RFC-032)."""

from app.domain.exceptions.domain_error import DomainError


class InvalidPositionError(DomainError):
    """Levantada quando uma latitude e uma longitude não descrevem um ponto da Terra.

    No mais das vezes: meia posição. Uma latitude sem a longitude não
    localiza nada, e o RFC-032 §4.3 se recusa a guardar uma -- a CHECK
    `ck_images_position_pairing` diz o mesmo em SQL, para quem escreve sem
    passar por aqui. Levantada também para uma coordenada fora de
    `[-90, 90]` x `[-180, 180]`, ou que não seja um número finito.

    Uma subclasse de `DomainError`, para que a camada HTTP responda 400 sem
    um handler próprio: `near_lat=91` é interpretado como número e recusado
    pelo que significa, que é o caso do 400, e não do 422 do FastAPI.
    """

    def __init__(self, message: str = "Invalid position.") -> None:
        super().__init__(message)


class InvalidGeoCircleError(DomainError):
    """Levantada quando um círculo de "perto daqui" não pode ser avaliado como pedido.

    Um raio que é zero, negativo, não finito ou -- por política da aplicação
    -- abaixo de `MIN_RADIUS_M`; ou um círculo dado só em parte, dois de
    `near_lat` / `near_lon` / `radius_m` sem o terceiro (RFC-032 §6).
    Recusado, em vez de respondido com um resultado vazio: uma resposta vazia
    para um círculo malformado se lê exatamente como "não há foto aqui", que
    é a resposta errada mais cara que este filtro pode dar.
    """

    def __init__(self, message: str = "Invalid search circle.") -> None:
        super().__init__(message)


class InvalidBoundingBoxError(DomainError):
    """Levantada quando uma área do mapa está invertida ou cruza o antimeridiano.

    `GET /images/map` recebe a área visível como cantos `min`/`max`, e
    `min > max` tem duas leituras que os quatro números não distinguem: um
    cliente que trocou os cantos, ou uma área visível que dá a volta por
    +/-180 graus. O RFC-032 §7 não suporta nenhuma das duas e o diz com um
    400, em vez de adivinhar qual era a intenção e responder a outra
    pergunta.
    """

    def __init__(self, message: str = "Invalid map area.") -> None:
        super().__init__(message)
