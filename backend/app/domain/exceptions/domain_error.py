"""A exceção raiz da camada de domínio, e as três bases que carregam um status.

`DomainError` sozinha bastava enquanto toda recusa queria dizer a mesma
coisa: *o pedido foi entendido e a aplicação o recusou*, que o
`error_handlers.py` respondia com 400. O RFC-029 introduz recusas que o 400
descreve mal -- um id de job que não nomeia nada, um disco que já está
ocupado --, então duas bases intermediárias foram acrescentadas, em vez de
uma tabela de classes de exceção na Presentation. O RFC-030 acrescentou a
terceira, para um arquivo que foi indexado e não está mais onde estava.

As bases estão aqui, ao lado de `DomainError`, e não num arquivo próprio: são
a raiz da hierarquia, e um chamador que importa `DomainError` é exatamente o
chamador que precisa saber que elas existem.

**Elas nomeiam um *tipo de recusa*, e não um código HTTP.** `NotFoundError`
quer dizer que a coisa referida não existe; `ConflictError`, que ela existe e
o estado atual não permite o que foi pedido; `GoneError`, que ela existiu e
desde então desapareceu. Que a Presentation as mapeie para 404, 409 e 410 é
problema da Presentation -- o Domain continua nunca devolvendo códigos HTTP
(`ARCHITECTURE.md` §18).
"""


class DomainError(Exception):
    """Classe base de todas as exceções da camada de domínio."""


class NotFoundError(DomainError):
    """Levantada quando uma operação nomeia algo que não existe.

    Deliberadamente *não* usada para um resultado vazio. "Nenhuma imagem casou
    com esta consulta" é uma resposta bem-sucedida; "não há job com este id" é
    um chamador agindo sobre uma suposição errada, e a distinção é a de que o
    RFC-029 §7 precisa para que consultar um job saiba diferenciar "ainda não
    terminou" de "nunca existiu".
    """


class ConflictError(DomainError):
    """Levantada quando o pedido é bem formado, mas o estado atual o recusa.

    O teste é se o mesmo pedido daria certo mais tarde, ou com o mundo em
    outro estado: criar um job para um disco que já tem um job ativo seria
    aceito quando esse job terminasse, e criar um para um disco desplugado
    seria aceito com o disco plugado. Nada no pedido precisa ser corrigido, e
    é justamente isso que separa este caso do 400.
    """


class GoneError(DomainError):
    """Levantada quando a coisa referida existiu, mas não existe mais.

    Distinta de `NotFoundError`: um id que nunca existiu é 404; um id que
    nomeava algo real, que agora sumiu, é 410. O chamador não errou ao
    perguntar -- o mundo mudou por baixo do pedido.

    Distinta de `ConflictError` também, e a diferença é se esperar ajuda. Um
    disco na gaveta é um conflito, porque o pedido idêntico dá certo assim que
    ele é plugado; um arquivo apagado de um disco que *está* plugado não volta
    sozinho, e dizer ao chamador para tentar de novo seria dizer algo falso
    (RFC-030 §5.1).

    Acrescentada pelo RFC-030, que precisava do status e não achou base que o
    carregasse: até então, o `error_handlers.py` conhecia o 404 e o 409, e
    nada mais.
    """
