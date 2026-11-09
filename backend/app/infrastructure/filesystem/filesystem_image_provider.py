"""Abstração de infraestrutura do sistema de arquivos para descobrir imagens candidatas.

**A ordem que este módulo produz é um requisito de correção, não uma
conveniência.** O RFC-029 §10 faz do checkpoint uma *posição* na sequência da
descoberta, então "continuar depois de X" só tem sentido se um segundo scan das
mesmas pastas visitar os mesmos arquivos na mesma ordem. Um `sorted()` removido
em nome do desempenho passaria em todos os outros testes da suíte e quebraria a
retomada em silêncio, de um jeito que depende do sistema de arquivos.

**E a ordem é a de `Path`, que não é a ordem de string.** No Windows, `sorted()`
sobre caminhos compara parte por parte e sem distinguir maiúsculas, então

    sorted(Path)  ->  a/x.jpg, a/Z.jpg, a b/x.jpg, B/y.jpg
    sorted(str)   ->  B/y.jpg, a b/x.jpg, a/Z.jpg, a/x.jpg

e a collation do PostgreSQL daria uma terceira resposta. Uma retomada escrita
como `relative_path > :checkpoint`, em SQL ou em Python, portanto pula e repete
arquivos arbitrários -- e passa em todo teste cujos nomes de fixture sejam
minúsculos e sem espaços, que é todo teste escrito sem saber disso.
`is_after_checkpoint()`, abaixo, é a única comparação, e compara caminhos com
caminhos.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath

from app.domain.services.indexing_observer import (
    IndexingObserver,
    NullIndexingObserver,
)
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.exif_capture_date import read_exif_facts
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


def discovery_sort_key(relative_path: PurePath) -> PurePath:
    """Devolve o valor pelo qual a ordem da descoberta é estabelecida.

    O próprio caminho. Parece não fazer nada, e é esse o ponto: `PurePath` já
    implementa a comparação de que este módulo precisa -- parte por parte, com
    a regra de maiúsculas da plataforma em que roda --, então a chave de
    ordenação é "um caminho do sabor da plataforma", e qualquer coisa que o
    transforme em texto antes de comparar deixou a ordem para trás.

    Existe como função nomeada para que os dois lugares que precisam concordar
    -- a ordenação em `discover()` e o teste de retomada abaixo -- sejam
    visivelmente a mesma decisão, e não duas chamadas independentes a
    `sorted()`.
    """
    return relative_path


@dataclass(frozen=True)
class ListedTree:
    """O que `_list_tree()` achou sob um diretório.

    `entries` é o que `sorted(root.rglob("*"))` dava -- pastas e arquivos, sem
    a própria raiz --, fora de ordem; `files` são as entradas que o walk
    classificou como arquivos, **os mesmos objetos** de `entries`; `unreadable`
    são as pastas cuja listagem falhou, absolutas, na ordem em que falharam.
    """

    entries: list[Path]
    files: list[Path]
    unreadable: list[Path]

    def file_identities(self) -> set[int]:
        """O `id()` de cada arquivo, para perguntar se uma entrada é arquivo.

        **Por identidade, não por caminho.** O hash de um `WindowsPath` passa o
        texto para minúsculas, toda vez: um `set` dos 116.396 caminhos do `E:`
        foi 1,5 s dos 2,2 s que a primeira listagem levou, e deixou o walk duas
        vezes mais lento que o `rglob` que ele substitui (RFC-046 §16.12). Os
        objetos são os de `entries`, vivos enquanto esta árvore viver, então os
        ids são deles.
        """
        return {id(path) for path in self.files}


def _list_tree(scope_root: Path) -> ListedTree:
    """Lista tudo sob `scope_root`, e diz quais pastas não puderam ser lidas.

    **`Path.walk(on_error=...)`, não `rglob("*")`** (RFC-046 §5.2). No Python
    3.12 desta instalação, `rglob("*")` acha as pastas com um `walk()` sem
    `on_error`, e lista cada uma de novo com um `scandir()` dentro de um
    `except OSError: pass` -- então uma pasta que não pode ser lida some da
    listagem, com tudo o que há nela, e nada o diz. O piloto de §2 mostrou isso
    com uma pasta negada por `icacls`. O walk dá as mesmas entradas e entrega a
    falha aqui.

    O que não muda, e por quê:

    * **as entradas**: os `dirnames` e `filenames` do walk em cada pasta são o
      que o `scandir()` do `rglob` listava;
    * **os links**: `follow_symlinks=False`, que é o que o `rglob` usava por
      dentro; um link para uma pasta é uma entrada e não é percorrido;
    * **a ordem**: nenhuma aqui. Ela vem do `sorted()` com `discovery_sort_key`
      depois da listagem, como antes.

    Dois comportamentos do `walk()` do 3.12 de que quem chama depende (§5.2):
    um erro **durante a leitura** de uma pasta que chegou a abrir não é passado
    a `on_error` -- ele se propaga, encerra o scan e o job falha, o que não
    marca nada; e um `is_dir()` que falha classifica a entrada como arquivo, e
    a pasta dela então não é percorrida. No Windows, o tipo vem da própria
    listagem.

    Uma leitura por pasta, onde o `rglob` fazia duas.
    """
    entries: list[Path] = []
    files: list[Path] = []
    unreadable: list[Path] = []

    def note(error: OSError) -> None:
        # `filename` é a pasta cujo `scandir()` falhou. Sem ele, a falha não
        # tem lugar, e a resposta conservadora é a raiz: tudo sob ela fica
        # então fora do alcance da varredura.
        failed = None if error.filename is None else Path(error.filename)
        if failed is None or not failed.is_relative_to(scope_root):
            failed = scope_root
        unreadable.append(failed)

    for folder, dirnames, filenames in scope_root.walk(on_error=note):
        for name in dirnames:
            entries.append(folder / name)
        for name in filenames:
            path = folder / name
            entries.append(path)
            files.append(path)
    return ListedTree(entries=entries, files=files, unreadable=unreadable)


def is_after_checkpoint(relative_path: PurePath, checkpoint: PurePath | None) -> bool:
    """Se `relative_path` ainda precisa ser visitado depois de `checkpoint`.

    `checkpoint` é o último arquivo que uma execução tornou durável, então o
    arquivo *igual* a ele já está feito e tudo o que for estritamente maior não
    está. `None` -- um job que nunca gravou checkpoint -- significa que tudo
    ainda está por fazer.

    Os dois operandos são caminhos do mesmo sabor, e é isso que faz desta a
    mesma comparação que ordenou o scan.
    """
    if checkpoint is None:
        return True
    return discovery_sort_key(relative_path) > discovery_sort_key(checkpoint)


class FilesystemImageProvider:
    """Descobre recursivamente os arquivos de imagem suportados sob uma raiz.

    Não sabe nada de repositórios, embeddings, PostgreSQL, workers ou casos de
    uso -- só lê o sistema de arquivos pela biblioteca padrão e, desde o
    RFC-028, o cabeçalho EXIF de cada arquivo pelo Pillow -- uma vez por
    arquivo, para a data e, desde o RFC-032, para a posição. Desde o RFC-029,
    também sabe percorrer *parte* de uma raiz e retomar de onde uma execução
    anterior parou, que são propriedades de um scan e não de um job: nada aqui
    sabe que jobs existem.
    """

    def __init__(
        self,
        root: Path,
        supported_extensions: Iterable[str],
        extract_capture_date: bool = True,
        scopes: tuple[JobScope, ...] = (),
        resume_after: PurePath | None = None,
        observer: IndexingObserver | None = None,
        report_every: int = 200,
        excluded_directories: tuple[Path, ...] = (),
        extract_gps: bool = True,
    ) -> None:
        """Configura um scan de `root`, ou de algumas pastas dentro dela.

        `extract_capture_date` é um argumento do construtor, e não uma leitura
        de `settings`, o que mantém esta classe livre de configuração: a raiz de
        composição passa `settings.extract_capture_date`. O padrão é ligado
        porque o RFC-028 §6 põe a extração no scan, e o argumento só existe
        porque o §11 aponta a única razão para desligá-la -- um custo medido por
        arquivo alto demais para uma coleção muito grande.

        `scopes` são relativos a `root` e por padrão não há nenhum, o que
        significa a raiz inteira -- e assim todo chamador escrito antes do
        RFC-029 mantém o comportamento que tinha. Espera-se que já venham
        normalizados (`normalize_scopes()`): um escopo aninhado dentro de outro
        teria os seus arquivos descobertos duas vezes, e uma sequência que
        visita um arquivo duas vezes não tem posições para um checkpoint
        nomear.

        `resume_after` é um caminho relativo a `root`, vindo do checkpoint de
        uma execução anterior. Os arquivos até ele, inclusive, são pulados, o
        que poupa o scan e não a inferência -- a decisão incremental já torna
        barato reprocessá-los (RFC-029 §10).

        `observer`, quando dado, é avisado de quanto o scan avançou a cada
        `report_every` arquivos examinados, e mais uma vez quando termina. Isso
        existe para o heartbeat: um job cujos arquivos são todos pulados pela
        decisão incremental nunca completa um lote, e um que retoma pula aqui
        a maior parte do seu scan sem produzir nada -- os dois parecem mortos
        para um reaper que espera lotes (RFC-029 §9.1). O provedor continua sem
        saber nada de jobs; ele reporta a uma porta.

        `excluded_directories` são diretórios absolutos cujo conteúdo nunca é
        produzido, onde quer que fiquem sob `root` (RFC-030). O único chamador
        que precisa disso passa o cache de thumbnails: indexar um disco de
        sistema inteiro descobriria, sem isso, cada thumbnail como uma foto,
        geraria o embedding dela, renderizaria uma thumbnail *dela* e a
        descobriria na execução seguinte -- uma coleção que cresce a cada scan.
        Os arquivos excluídos são pulados antes de contarem como examinados,
        então também não fazem parte da sequência que um checkpoint nomeia.

        `extract_gps` é o gêmeo de `extract_capture_date` para o RFC-032, no
        mesmo molde e pelas mesmas razões: vem de `settings.extract_gps`, é
        ligado por padrão, e desligado significa "não examinado", nunca "sem
        posição". Os dois são chaves separadas sobre **uma** leitura de
        cabeçalho: com qualquer um ligado, cada arquivo é aberto uma vez e os
        dois fatos pedidos saem dessa abertura; com os dois desligados, o
        arquivo nem é aberto. Acrescentado por último, como keyword, para que
        os argumentos posicionais de nenhum chamador existente mudem de lugar.
        """
        self._root = root
        self._supported_extensions = {ext.lower() for ext in supported_extensions}
        self._extract_capture_date = extract_capture_date
        self._extract_gps = extract_gps
        self._scopes = scopes
        self._resume_after = resume_after
        self._observer = observer or NullIndexingObserver()
        self._report_every = max(1, report_every)
        self._excluded = tuple(
            directory.resolve() for directory in excluded_directories
        )
        # A regra do próprio `pathlib`: a igualdade de `PurePath` ignora
        # maiúsculas onde a plataforma ignora (`_str_normcase`), e
        # `_is_excluded()` também.
        self._case_insensitive = PurePath("A") == PurePath("a")
        self._excluded_texts = tuple(
            (folded, folded if folded.endswith(os.sep) else folded + os.sep)
            for folded in (self._fold(str(directory)) for directory in self._excluded)
        )
        root_text = str(root)
        self._root_prefix = (
            root_text if root_text.endswith(os.sep) else root_text + os.sep
        )

    def discover(self) -> Iterator[DiscoveredImageFile]:
        """Produz os metadados de cada arquivo de imagem suportado que o scan cobre.

        A ordem é estável entre execuções, e o RFC-029 §10 depende disso -- ver
        a docstring deste módulo.

        Com vários escopos, cada um é percorrido por inteiro antes do seguinte,
        e os próprios escopos são percorridos na ordem dos caminhos. Essa ordem
        não é cosmética: é ela que mantém a sequência inteira na ordem dos
        caminhos *através* das fronteiras entre escopos, porque escopos
        normalizados são disjuntos e todo arquivo sob um escopo compartilha as
        partes iniciais dele. Percorra os escopos na ordem em que o cliente por
        acaso os mandou, e uma retomada que caia no meio da sequência vai
        comparar contra um checkpoint de outra ordenação.

        A data de captura e a posição são lidas na mesma passada que o
        `stat()`, de uma abertura do arquivo (RFC-032 §4.2), não num segundo
        percurso e não de forma preguiçosa. Um campo preguiçoso -- um callable
        em `DiscoveredImageFile` -- foi considerado e adiado até que uma
        medição o peça (RFC-028 §11): ele tiraria a leitura do scan e a poria
        em qualquer consumidor que por acaso a chamasse primeiro, que é como
        um custo deixa de ser visível.

        Um scan da raiz inteira -- sem escopos -- também diz ao observer
        quantos arquivos suportados a sua listagem contém, uma vez, antes de o
        primeiro arquivo sair (`IndexingObserver.listed()`, RFC-039 §4.1). Um
        scan de algumas pastas nunca diz.
        """
        yielded = 0
        examined = 0
        whole_root = not self._scopes
        for scope_root in self._scope_roots():
            if not scope_root.exists():
                continue
            for discovered in self._discover_under(scope_root, report=whole_root):
                examined += 1
                if discovered is not None:
                    yielded += 1
                    yield discovered
                if examined % self._report_every == 0:
                    self._observer.discovered(yielded, complete=False)

        self._observer.discovered(yielded, complete=True)

    def _scope_roots(self) -> list[Path]:
        """Devolve os diretórios a percorrer, na ordem em que serão percorridos.

        Ordenados pela mesma chave que ordena os arquivos, para que a fronteira
        entre dois escopos caia onde a ordem global dos caminhos a põe.
        """
        if not self._scopes:
            return [self._root]
        return sorted(
            (self._root / str(scope) for scope in self._scopes),
            key=discovery_sort_key,
        )

    def _discover_under(
        self, scope_root: Path, report: bool = False
    ) -> Iterator[DiscoveredImageFile | None]:
        """Percorre um diretório na ordem dos caminhos, pulando o que já foi feito.

        Produz `None` para um arquivo suportado que a retomada pulou, para que
        quem chama possa contá-lo como examinado sem passá-lo adiante. Não é
        contabilidade por si só: numa retomada, quase o scan inteiro são
        arquivos pulados, e um heartbeat movido só pelos arquivos *produzidos*
        ficaria em silêncio exatamente pelo tempo que a retomada poupa
        (RFC-029 §9.1).

        **O `sorted()` é o que isto não consegue cobrir.** Ele consome o
        percurso inteiro antes de o primeiro arquivo sair, então um percurso a
        frio de um disco grande não produz callback nenhum até terminar. Essa é
        a lacuna que `job_stale_timeout` precisa vencer, e é por isso que o
        timeout do RFC-029 §9.1 vem de uma distribuição medida que inclui a
        primeira janela, e não da duração de um lote.

        Com `report`, a listagem vai para `IndexingObserver.listed()` assim que
        existe e antes de o primeiro arquivo ser produzido: é quando o
        denominador do "% indexado" é conhecido (RFC-039 §4.1) e, desde o
        RFC-046, os próprios arquivos, que o job compara com as linhas do
        dispositivo quando termina (`RootListing`).

        **Uma pasta que não pôde ser lida é um WARNING, em todo scan** --
        também no de algumas pastas -- com o nome dela (RFC-046 §5.2). Antes,
        não deixava rastro. Uma pasta dentro de um diretório excluído não é
        relatada: nada ali é listado de qualquer jeito, e a
        `System Volume Information` é ilegível em todo disco.
        """
        tree = _list_tree(scope_root)
        unreadable = [
            folder for folder in tree.unreadable if not self._is_excluded(folder)
        ]
        for folder in unreadable:
            logger.warning(
                "Could not read the folder %s; nothing under it was listed",
                self._posix(folder),
            )
        listing = sorted(tree.entries, key=discovery_sort_key)
        if report:
            self._observer.listed(
                self._root_listing(listing, tree.file_identities(), unreadable)
            )
        for path in listing:
            if not path.is_file():
                continue
            if self._is_excluded(path):
                continue
            if path.suffix.lower() not in self._supported_extensions:
                continue
            if not is_after_checkpoint(self._relative(path), self._resume_after):
                yield None
                continue

            stat = path.stat()
            facts = read_exif_facts(
                path,
                capture_date=self._extract_capture_date,
                position=self._extract_gps,
            )
            yield DiscoveredImageFile(
                path=path,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                file_size=stat.st_size,
                file_modified_at=datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ),
                capture_date=facts.capture_date,
                position=facts.position,
            )

    def _root_listing(
        self, listing: list[Path], files: set[int], unreadable: list[Path]
    ) -> RootListing:
        """A listagem que um scan da raiz inteira relata: suas fotos, e seus buracos.

        Os arquivos são as entradas que o walk classificou como arquivos, com
        uma extensão suportada, fora dos diretórios excluídos -- os próprios
        testes do walk, e **nenhum `stat`**: o walk já distingue uma pasta de um
        arquivo, então uma pasta chamada `x.jpg` não conta mais (RFC-046 §5.2,
        que remove a imprecisão que o RFC-039 §4.1 declarou). Na ordem da
        listagem, como texto relativo à raiz.

        Todo arquivo está nela, também os de antes de um checkpoint: o
        denominador é o tamanho do disco, e a varredura compara o disco
        inteiro; uma retomada lista a árvore inteira de novo.
        """
        photographs = tuple(
            self._posix(path)
            for path in listing
            if path.suffix.lower() in self._supported_extensions
            and id(path) in files
            and not self._is_excluded(path)
        )
        return RootListing(
            file_count=len(photographs),
            files=photographs,
            unreadable=tuple(self._posix(folder) for folder in unreadable),
        )

    def _posix(self, path: Path) -> str:
        """`path` relativo à raiz do scan, na forma posix que `ImagePath` guarda.

        **O texto depois do da raiz, e não `relative_to()`.** Todo caminho de
        uma listagem é a raiz unida a nomes, então o texto dele começa com o da
        raiz; `relative_to()` chega à mesma resposta percorrendo os pais, e nos
        123.353 arquivos do `E:` isso eram 6,2 s por scan (RFC-046 §16.12). Um
        caminho que não comece com o texto da raiz -- a própria raiz, ou um
        escrito de outro jeito -- vai pelo caminho longo.
        """
        text = str(path)
        if text.startswith(self._root_prefix) and len(text) > len(self._root_prefix):
            return text[len(self._root_prefix) :].replace(os.sep, "/")
        return path.relative_to(self._root).as_posix()

    def _is_excluded(self, path: Path) -> bool:
        """Se `path` está dentro de um dos diretórios excluídos.

        Comparados como caminhos, então a regra de maiúsculas da plataforma se
        aplica: no Windows, `AppData/Local` e `appdata/local` são o mesmo
        diretório.

        **Como texto, pela regra que o `pathlib` usa** (RFC-046 §16.12): o
        texto do caminho, normalizado como o `pathlib` o normaliza para as suas
        próprias comparações, ou é o do diretório excluído ou começa com ele e
        um separador -- o que equivale a `is_relative_to()` para os caminhos
        absolutos e sem `..` que um walk produz, e o separador é o que impede
        `cache` de excluir `cache2`. `is_relative_to()` constrói cada pai do
        caminho para compará-lo; com os três diretórios do RFC-046, isso eram
        8,9 s nos arquivos do `E:`, duas vezes por scan.

        O caminho descoberto *não* é resolvido, embora os excluídos sejam.
        Resolver é uma chamada de sistema por arquivo no único laço que roda
        100.000 vezes, e não compra nada aqui: o walk parte de uma raiz
        resolvida e produz caminhos absolutos escritos como a listagem do
        diretório os escreve, que é como o diretório excluído também se
        resolve.
        """
        text = self._fold(str(path))
        return any(
            text == excluded or text.startswith(prefix)
            for excluded, prefix in self._excluded_texts
        )

    def _fold(self, text: str) -> str:
        """`text` na caixa em que o `pathlib` compara caminhos, nesta plataforma."""
        return text.lower() if self._case_insensitive else text

    def _relative(self, path: Path) -> PurePath:
        """Exprime um arquivo descoberto do jeito que um checkpoint o nomeia.

        Relativo à raiz do scan, que para um job é o ponto de montagem do
        dispositivo -- então o valor comparado aqui é o valor guardado, e a
        comparação é a que ordenou o scan.
        """
        return path.relative_to(self._root)
