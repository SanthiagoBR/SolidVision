"""A pasta a que um job de indexação se restringe, e como várias se combinam.

Um escopo é um caminho *relativo ao ponto de montagem de um dispositivo*, que
é a única forma que sobrevive ao disco ser desplugado e voltar com outra
letra de unidade (RFC-027 §4). Tudo aqui existe para mantê-lo assim: um
caminho absoluto, uma âncora de unidade, um compartilhamento UNC e um
componente `..` são todos recusados na construção, e não higienizados,
porque cada um deles é um pedido para indexar algo fora do dispositivo que o
job nomeia.

**Os escopos são guardados como `parts`, e não como string.** Ordenação e
contenção são as duas operações que este módulo faz, e as duas são operações
de tupla sobre as partes: `("2018",)` contém `("2018", "junho")` porque é um
prefixo dela. Fazer o mesmo com texto faria `2018` parecer prefixo de
`2018b`, que é outra pasta (RFC-029 §10, e §4.4 do build prompt do RFC-029).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from app.domain.exceptions import InvalidJobScopeError


@dataclass(frozen=True)
class JobScope:
    """Uma pasta dentro de um dispositivo, como caminho relativo ao dispositivo.

    O escopo vazio -- sem parte nenhuma -- quer dizer *o dispositivo
    inteiro*, que é o que o RFC-029 §5.2 grava como zero linhas de escopo.
    `""` e `"."` se normalizam os dois para ele, então um cliente que manda
    qualquer um dos dois recebe o disco inteiro, e fica sabendo disso pelos
    escopos normalizados devolvidos no 202.
    """

    parts: tuple[str, ...]

    def __init__(self, value: str | PurePath | tuple[str, ...] = ()) -> None:
        object.__setattr__(self, "parts", self._normalize(value))

    @staticmethod
    def _normalize(value: str | PurePath | tuple[str, ...]) -> tuple[str, ...]:
        """Valida e divide um escopo, recusando tudo o que escapa.

        As verificações rodam nesta ordem porque a ordem é a garantia. `..` é
        recusado **antes** de qualquer coisa ser resolvida: resolver primeiro
        e verificar depois é como um escopo que escapa ganha uma chance de ser
        lido como caminho real, e isso também faria a recusa depender de a
        pasta existir. Um escopo que escapa é inválido esteja o disco plugado
        ou não.

        Os dois sabores de caminho são consultados no teste de âncora. Só o
        Windows não deixaria passar nada hoje, mas é o POSIX que pega uma `/`
        solta no início de um jeito que não depende da plataforma que roda
        este processo -- e o Domain não tem nada que saber qual é (RFC-029
        §4.1 do build prompt: nada fora do adaptador de volumes depende do
        Windows).
        """
        if isinstance(value, tuple):
            text = "/".join(value)
        elif isinstance(value, PurePath):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            raise InvalidJobScopeError(
                "Scope must be a string, a PurePath, or a tuple of parts"
            )

        # Os separadores são normalizados primeiro, para que os testes de
        # âncora abaixo vejam a mesma string que o chamador quis dizer. Um
        # cliente Windows manda `2018\junho`; recusá-lo por "conter uma barra
        # invertida" seria pedantismo, e lê-lo como uma parte só chamada
        # `2018\junho` seria pior -- passaria em toda verificação e não
        # casaria com nada.
        text = text.replace("\\", "/").strip()

        if PureWindowsPath(text).anchor or PurePosixPath(text).anchor:
            raise InvalidJobScopeError(
                f"Scope must be relative to the device, got {text!r}. A drive "
                "letter, a leading separator or a UNC share names a location "
                "outside the device this job is for."
            )

        parts = PurePosixPath(text).parts
        if any(part == ".." for part in parts):
            raise InvalidJobScopeError(
                f"Scope must not contain '..', got {text!r}. A scope that "
                "walks out of the device is not a scope of that device."
            )

        # `PurePosixPath` já descartou `.` e juntou separadores repetidos,
        # então o que sobra são as partes reais ou nada -- e nada é o
        # dispositivo inteiro, e não um erro.
        return parts

    def __str__(self) -> str:
        """Escreve como o caminho relativo ao dispositivo, `""` para o inteiro."""
        return "/".join(self.parts)

    @property
    def is_whole_device(self) -> bool:
        """Se este escopo cobre tudo no dispositivo."""
        return not self.parts

    def contains(self, other: JobScope) -> bool:
        """Se todo arquivo abaixo de `other` também está abaixo deste escopo.

        Verdadeiro para um escopo e ele mesmo, o que torna idempotente a
        absorção em `normalize_scopes()`. O escopo do dispositivo inteiro
        contém tudo, inclusive ele mesmo, pela mesma regra de prefixo -- uma
        tupla vazia é prefixo de toda tupla.
        """
        return other.parts[: len(self.parts)] == self.parts


def normalize_scopes(scopes: tuple[JobScope, ...]) -> tuple[JobScope, ...]:
    """Absorve os escopos contidos e devolve o que de fato será percorrido.

    `["2018/", "2018/junho"]` vira `["2018"]`: o segundo está dentro do
    primeiro, e percorrer os dois descobriria todo arquivo abaixo de
    `2018/junho` duas vezes -- pagando dois `stat` e duas leituras de EXIF
    por arquivo e, muito pior, produzindo uma ordem de descoberta em que um
    caminho aparece duas vezes. O checkpoint do RFC-029 §10 é uma *posição*
    nessa ordem, e uma posição só tem sentido numa sequência que visita cada
    arquivo uma vez.

    O resultado é o que o 202 devolve, para que um cliente que pediu duas
    pastas sobrepostas possa ver o job único que de fato recebeu.

    **A ordem não é a ordem de descoberta**, e não pode ser confundida com
    ela. As tuplas de partes são ordenadas aqui para que o resultado seja
    determinístico e estável de comparar num teste, mas a sequência que o
    scan percorre é estabelecida por `FilesystemImageProvider`, que ordena
    com a ordenação de caminhos da própria plataforma em que roda -- sem
    diferenciar maiúsculas de minúsculas no Windows, onde `a b` vem antes de
    `B`. Ordenar aqui com uma regra que o sistema de arquivos não compartilha
    poria a fronteira entre dois escopos num lugar diferente do que o scan
    põe, e a comparação da retomada atravessa essas fronteiras.

    Duplicatas saem de graça: um escopo contém a si mesmo, então a segunda
    cópia é absorvida pela primeira.
    """
    if any(scope.is_whole_device for scope in scopes):
        # Zero linhas já quer dizer "o dispositivo inteiro" (RFC-029 §5.2),
        # então um escopo explícito do dispositivo inteiro se reduz a essa
        # forma, em vez de ser gravado como uma linha que quer dizer a mesma
        # coisa. No caminho, ele absorve todos os outros escopos.
        return ()

    ordered = sorted(set(scopes), key=lambda scope: scope.parts)
    kept: list[JobScope] = []
    for scope in ordered:
        # Ordenados por partes, então qualquer escopo que contenha `scope` já
        # está em `kept` e é o último que poderia contê-lo: um contêiner é um
        # prefixo, e um prefixo vem antes do que ele contém.
        if kept and kept[-1].contains(scope):
            continue
        kept.append(scope)
    return tuple(kept)
