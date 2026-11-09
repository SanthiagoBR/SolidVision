"""O que a listagem de um disco inteiro achou, entregue ao observador (RFC-046)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RootListing:
    """Os arquivos suportados de uma raiz inteira, e as pastas que não deu para ler.

    O que `IndexingObserver.listed()` recebe desde o RFC-046 §5.3, no lugar
    da contagem simples que o RFC-039 §4.1 lhe dava. Três campos:

    * `file_count` -- o denominador do "% indexado", contando **só
      arquivos**: a varredura distingue uma pasta de um arquivo sem um
      `stat`, então uma pasta chamada `x.jpg` não conta mais (RFC-046 §5.2);
    * `files` -- cada um desses arquivos relativo à raiz, como texto na forma
      posix que `ImagePath` guarda. Texto, e não `PurePath`, pela memória: um
      disco de 116.396 fotos guarda esta lista da listagem até o fim do job
      (RFC-046 §5.3 e §8);
    * `unreadable` -- as pastas que a varredura não conseguiu ler, relativas
      à raiz, na mesma forma. Nada abaixo delas foi listado, então nada
      abaixo delas pode ser chamado de ausente (§2).

    **Sem dispositivo e sem id.** O provider que constrói isto não conhece
    nenhum dos dois; o job runner cunha os ids a partir de `files` no fim do
    job, com a mesma função que os cunha durante a indexação (§5.5).

    `file_count` é um campo, e sempre `len(files)`: os dois são o mesmo
    conjunto, e uma listagem cuja contagem e cujos arquivos discordam é
    recusada na construção.
    """

    file_count: int
    files: tuple[str, ...]
    unreadable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.file_count != len(self.files):
            raise ValueError(
                f"A listing of {len(self.files)} files cannot count {self.file_count}"
            )
