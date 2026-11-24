"""Draw the UML use case diagrams, and write their tables in docs/casos-de-uso.md.

    python scripts/use_case_diagrams.py              # write the SVGs and the tables
    python scripts/use_case_diagrams.py --check      # exit 1 if any of them is stale
    python scripts/use_case_diagrams.py --png DIR    # also rasterize them, with Edge

The use case model lives here, as data: the actors, the use cases with the
functional requirements each one covers, and the relationships between them
(association, «include», «extend» and generalization). A diagram is a view of
that model: it places some of its elements, by hand, and draws every
relationship whose two ends it placed. The tables of the document -- the
actors, the catalog and the traceability -- come from the same data, between
`<!-- BEGIN GENERATED: name -->` and `<!-- END GENERATED: name -->`; the prose
around them is written by hand.

The layout is by hand, and the drawing is plain SVG from the standard library:
nothing to install, and no automatic layout crossing the lines of a diagram
with fifteen use cases. Text is measured with the widths of Arial, the font
the SVGs ask for.

The script also reads docs/requisitos.md, and fails when a functional
requirement there has no use case, or a use case cites one that is not there.

The output has no date: the same model gives the same files, byte for byte,
and `--check` compares them. The PNGs of `--png` are not compared, nor kept in
the repository: they are for pasting in a document.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[1]
DOCUMENT = Path("docs") / "casos-de-uso.md"
FIGURES = Path("docs") / "casos-de-uso"
REQUIREMENTS = Path("docs") / "requisitos.md"

# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    key: str
    name: str
    primary: bool
    system: bool
    description: str
    label: str = ""

    @property
    def lines(self) -> list[str]:
        return (self.label or self.name).split("\n")


@dataclass(frozen=True)
class UseCase:
    id: str
    name: str
    package: str
    requirements: tuple[str, ...]
    description: str
    label: str = ""
    abstract: bool = False

    @property
    def lines(self) -> list[str]:
        return (self.label or self.name).split("\n")


@dataclass(frozen=True)
class Relation:
    """`source` is the actor of an association, the base of an «include», the
    extension of an «extend», and the child of a generalization."""

    kind: str
    source: str
    target: str
    condition: str = ""


ASSOCIATION = "association"
INCLUDE = "include"
EXTEND = "extend"
GENERALIZATION = "generalization"

PACKAGES = (
    "Instalação e execução",
    "Dispositivos",
    "Indexação",
    "Busca",
    "Resultados",
    "Mapa",
    "Linha de comando e manutenção",
)

ACTORS: tuple[Actor, ...] = (
    Actor(
        "fotografo",
        "Fotógrafo",
        primary=True,
        system=False,
        description=(
            "O dono do acervo, que usa o app pela interface gráfica, na própria "
            "máquina. É o único usuário: não há contas nem perfis."
        ),
    ),
    Actor(
        "suporte",
        "suporte",
        primary=True,
        system=False,
        description=(
            "Quem usa a linha de comando e o arquivo de configuração. Especializa "
            "o Fotógrafo: faz tudo o que ele faz, e mais a manutenção."
        ),
    ),
    Actor(
        "windows",
        "Windows",
        primary=False,
        system=True,
        description=(
            "O sistema operacional. Informa os volumes montados e a conexão e a "
            "desconexão de um disco, abre o Explorer, encerra a sessão (logoff) "
            "e executa a desinstalação."
        ),
    ),
    Actor(
        "osm",
        "Tiles do OpenStreetMap",
        label="Tiles do\nOpenStreetMap",
        primary=False,
        system=True,
        description=(
            "O servidor das imagens do mapa. Dá o fundo do mapa quando há rede e "
            "o fotógrafo não o desligou."
        ),
    ),
    Actor(
        "nominatim",
        "Nominatim",
        primary=False,
        system=True,
        description=(
            "O serviço de endereços do OpenStreetMap. Converte o endereço "
            "digitado numa coordenada, só quando o fotógrafo pede."
        ),
    ),
)

USE_CASES: tuple[UseCase, ...] = (
    # Instalação e execução
    UseCase(
        "UC-01",
        "Instalar o SolidVision",
        "Instalação e execução",
        ("RF-61",),
        "Instala o programa na conta do usuário, sem pedir administrador, com o "
        "Python, o PostgreSQL e os modelos de IA; cria os atalhos e abre o app "
        "ao terminar.",
    ),
    UseCase(
        "UC-02",
        "Abrir o aplicativo",
        "Instalação e execução",
        ("RF-54", "RF-55", "RF-57", "RF-64"),
        "Abre a janela, mostra as fases da subida e chega à tela inicial. Abrir "
        "de novo traz a janela existente para a frente; se a API reinicia, a "
        "janela mostra *Reconectando…* e volta sozinha. A interface só oferece "
        "o que a instalação suporta.",
    ),
    UseCase(
        "UC-03",
        "Fechar o aplicativo",
        "Instalação e execução",
        ("RF-59", "RF-60"),
        "Fecha a janela e para tudo em ordem; com um job em andamento, avisa "
        "antes que a indexação pausa e continua na próxima abertura. O logoff "
        "do Windows faz a mesma parada.",
    ),
    UseCase(
        "UC-04",
        "Desinstalar o SolidVision",
        "Instalação e execução",
        ("RF-62",),
        "Remove o programa e os atalhos por *Configurações > Aplicativos*, com o "
        "app aberto ou fechado, e preserva os dados: reinstalar reaproveita o "
        "acervo indexado.",
    ),
    UseCase(
        "UC-05",
        "Iniciar os serviços locais",
        "Instalação e execução",
        ("RF-54", "RF-58"),
        "Sobe o banco embarcado, aplica as migrations e sobe a API e o executor "
        "de jobs. Supervisiona os três e reinicia o que morrer, com espera "
        "crescente e um limite de tentativas.",
    ),
    UseCase(
        "UC-06",
        "Encerrar os serviços em ordem",
        "Instalação e execução",
        ("RF-59", "RF-62"),
        "Para a API e o executor de jobs, e depois o banco, sem deixar processo "
        "para trás. Um job em andamento fica com o ponto de retomada.",
        label="Encerrar os serviços\nem ordem",
    ),
    UseCase(
        "UC-07",
        "Abrir a pasta de logs",
        "Instalação e execução",
        ("RF-56",),
        "Abre no Explorer a pasta de logs, oferecida pela tela de erro da subida, "
        "que mostra a causa, e pelo painel de um job que teve falhas.",
    ),
    # Dispositivos
    UseCase(
        "UC-08",
        "Registrar dispositivo",
        "Dispositivos",
        ("RF-01", "RF-02", "RF-03"),
        "Escolhe um dos volumes montados que o Windows informa e o registra com "
        "um rótulo único, como *HD2*. O registro usa a identidade do volume, e "
        "não a letra da unidade; registrar o mesmo volume de novo devolve o "
        "mesmo dispositivo.",
    ),
    UseCase(
        "UC-09",
        "Renomear dispositivo",
        "Dispositivos",
        ("RF-04",),
        "Troca o rótulo de um dispositivo, o único campo editável.",
    ),
    UseCase(
        "UC-10",
        "Acompanhar os dispositivos",
        "Dispositivos",
        ("RF-05", "RF-06", "RF-07", "RF-08", "RF-11", "RF-35", "RF-69", "RF-72"),
        "Mostra, para cada dispositivo, o rótulo, a conexão, as fotos indexadas, "
        "o percentual com a data do scan, o job ativo e quantas fotos o último "
        "scan não encontrou; na tela inicial, o resumo do acervo e o aviso dos "
        "discos nunca indexados. A conexão e a desconexão aparecem pela "
        "mensagem de volume do Windows, e um disco com outra letra continua "
        "reconhecido.",
    ),
    UseCase(
        "UC-11",
        "Navegar pelas pastas",
        "Dispositivos",
        ("RF-09", "RF-10"),
        "Percorre as pastas de um disco conectado, um nível por vez, com o estado "
        "de indexação e as fotos indexadas de cada uma. Com o disco "
        "desconectado, pede para conectá-lo.",
    ),
    UseCase(
        "UC-41",
        "Remover do índice as fotos ausentes",
        "Dispositivos",
        ("RF-69",),
        "Pelo botão *Remover do índice* do cartão, apaga do índice as fotos "
        "ausentes do disco e as miniaturas delas, depois de uma confirmação com "
        "o número de fotos. O botão fica desligado enquanto o disco tem um job. "
        "Não precisa do disco conectado, e não toca nele.",
    ),
    # Indexação
    UseCase(
        "UC-12",
        "Indexar dispositivo",
        "Indexação",
        (
            *("RF-12", "RF-13", "RF-14", "RF-18", "RF-19", "RF-25", "RF-26"),
            *("RF-65", "RF-71"),
        ),
        "Pede a indexação do disco, que roda em segundo plano, num job: no "
        "máximo um ativo por disco. Os arquivos sem mudança são pulados, e a "
        "lixeira, a `System Volume Information` e o cache de miniaturas ficam "
        "de fora. Sem o executor de jobs, a interface diz que a indexação está "
        "indisponível. Também pela linha de comando (`index`).",
    ),
    UseCase(
        "UC-13",
        "Escolher as pastas",
        "Indexação",
        ("RF-12",),
        "Restringe a indexação às pastas marcadas; a pasta mãe absorve as filhas.",
    ),
    UseCase(
        "UC-14",
        "Acompanhar a indexação",
        "Indexação",
        ("RF-15",),
        "Mostra o job do disco: na fila, lendo a lista de arquivos, ou os "
        "contadores de descobertos, processados, pulados e com falha e o último "
        "arquivo salvo. O job não exibe percentual.",
    ),
    UseCase(
        "UC-15",
        "Cancelar a indexação",
        "Indexação",
        ("RF-16",),
        "Pede o fim do job. Um job na fila para na hora; um em andamento mostra "
        "*Cancelando…* até parar no próximo ponto salvo.",
    ),
    UseCase(
        "UC-16",
        "Retomar a indexação",
        "Indexação",
        ("RF-17",),
        "Continua um job interrompido do último ponto salvo, sem perder nem "
        "repetir fotos: pelo *Indexar de novo*, depois de um cancelamento ou "
        "de uma falha, ou sozinho, na abertura seguinte do app.",
    ),
    UseCase(
        "UC-17",
        "Extrair data e posição do EXIF",
        "Indexação",
        ("RF-21", "RF-22", "RF-23"),
        "Lê do EXIF a data de captura e a posição GPS e registra de onde vieram. "
        "Distingue a foto que não tem o dado da que nunca foi examinada.",
    ),
    UseCase(
        "UC-18",
        "Gerar a miniatura",
        "Indexação",
        ("RF-24",),
        "Gera a miniatura de 512 px no cache do app, sem escrever no disco do "
        "acervo.",
    ),
    UseCase(
        "UC-19",
        "Gerar o embedding da imagem",
        "Indexação",
        ("RF-20",),
        "Converte a imagem num vetor de 512 dimensões com o CLIP, em lotes.",
    ),
    UseCase(
        "UC-40",
        "Marcar as fotos ausentes",
        "Indexação",
        ("RF-70",),
        "Ao fim do job, compara as fotos indexadas do disco com a lista de "
        "arquivos que o job leu: marca as que não estão mais lá, que saem da "
        "busca, e desmarca as que voltaram, sem gerar o embedding de novo. Nada "
        "é marcado se o disco saiu ou mudou de letra durante o job, nem dentro "
        "de uma pasta que não pôde ser lida.",
    ),
    # Busca
    UseCase(
        "UC-20",
        "Encontrar fotos",
        "Busca",
        ("RF-28", "RF-29", "RF-40", "RF-41", "RF-42", "RF-72"),
        "O que as quatro formas de encontrar fotos têm em comum: a grade de "
        "resultados, 40 por vez e *Mostrar mais*, com a miniatura, o "
        "dispositivo, o nome e a similaridade, mesmo com o disco desconectado; "
        "e os filtros, que se combinam. As fotos marcadas como ausentes "
        "(UC-40) não aparecem. Abstrato: só existe pelas especializações.",
        abstract=True,
    ),
    UseCase(
        "UC-21",
        "Buscar por texto",
        "Busca",
        ("RF-27", "RF-30", "RF-33", "RF-34", "RF-35", "RF-65"),
        "Busca por uma descrição em português ou em inglês, ao pressionar "
        "Enter; recusa a consulta vazia. Mostra o estado do modelo enquanto ele "
        "carrega e oferece consultas de exemplo. Também pela linha de comando "
        "(`search`).",
    ),
    UseCase(
        "UC-22",
        "Traduzir a consulta",
        "Busca",
        ("RF-27",),
        "Traduz a consulta para o inglês antes de ela chegar ao modelo.",
    ),
    UseCase(
        "UC-23",
        "Buscar por foto de exemplo",
        "Busca",
        ("RF-32", "RF-34", "RF-65"),
        "Busca as fotos parecidas com uma imagem solta na barra de busca (JPEG "
        "ou PNG, até 50 MB), que não é guardada. Também pela linha de comando "
        "(`search-image`).",
        label="Buscar por foto\nde exemplo",
    ),
    UseCase(
        "UC-24",
        "Buscar fotos parecidas",
        "Busca",
        ("RF-31",),
        "Busca as fotos parecidas com uma foto já indexada, pelo embedding "
        "guardado: funciona sem o modelo e com o disco desconectado.",
    ),
    UseCase(
        "UC-25",
        "Ver as fotos de um lugar por data",
        "Busca",
        ("RF-53",),
        "Lista todas as fotos de um círculo pela data de captura, sem consulta e "
        "sem o modelo: as mais recentes primeiro, ou as mais antigas, e as sem "
        "data no fim.",
        label="Ver as fotos de um\nlugar por data",
    ),
    UseCase(
        "UC-26",
        "Filtrar por dispositivo",
        "Busca",
        ("RF-36", "RF-40", "RF-47"),
        "Restringe as fotos a um ou mais dispositivos.",
    ),
    UseCase(
        "UC-27",
        "Filtrar por data de captura",
        "Busca",
        ("RF-37", "RF-40", "RF-47"),
        "Restringe as fotos a um intervalo de datas de captura e diz quantas "
        "ficaram de fora por não terem data.",
    ),
    UseCase(
        "UC-28",
        "Filtrar por proximidade",
        "Busca",
        ("RF-38", "RF-39", "RF-40"),
        "Restringe as fotos a um círculo, de 300 m a 100 km, com o centro numa "
        "foto, num ponto do mapa, em coordenadas ou num endereço, e diz quantas "
        "ficaram de fora por não terem posição.",
    ),
    # Resultados
    UseCase(
        "UC-29",
        "Ver o detalhe da foto",
        "Resultados",
        ("RF-42", "RF-43"),
        "Mostra a miniatura, a data e a posição com as origens, o dispositivo e "
        "o caminho. Com o disco desconectado, diz onde a foto está, sem erro.",
    ),
    UseCase(
        "UC-30",
        "Mostrar no Explorer",
        "Resultados",
        ("RF-44", "RF-64"),
        "Abre o Explorer com a foto selecionada. Se o disco não está conectado, "
        "ou o arquivo não está mais nele, diz qual dos dois.",
    ),
    UseCase(
        "UC-31",
        "Copiar o caminho",
        "Resultados",
        ("RF-45",),
        "Copia o caminho da foto para a área de transferência.",
    ),
    # Mapa
    UseCase(
        "UC-32",
        "Explorar o mapa",
        "Mapa",
        ("RF-46", "RF-47", "RF-48", "RF-50", "RF-51", "RF-72"),
        "Mostra quantas fotos há em cada célula da área visível, numa precisão "
        "que acompanha o zoom, com a legenda, as 20 células mais cheias e o "
        "aviso das fotos sem posição; as marcadas como ausentes não entram nas "
        "contagens. O fundo vem dos tiles do OpenStreetMap ou, sem rede, do "
        "contorno dos estados.",
    ),
    UseCase(
        "UC-33",
        "Consultar uma célula",
        "Mapa",
        ("RF-49",),
        "Abre o balão de uma célula, com a contagem e as ações *Buscar perto "
        "daqui*, *Aproximar* e *Ver todas as fotos daqui, por data*.",
    ),
    UseCase(
        "UC-34",
        "Localizar endereço",
        "Mapa",
        ("RF-52", "RF-64"),
        "Envia o endereço digitado ao Nominatim e leva até ele o mapa, ou o "
        "centro do *Perto de*, com o nome do lugar encontrado. Só quando o "
        "fotógrafo pede, e pode ser desligado na configuração.",
    ),
    # Linha de comando e manutenção
    UseCase(
        "UC-35",
        "Iniciar pelo launcher",
        "Linha de comando e manutenção",
        ("RF-66",),
        "Sobe tudo pela linha de comando, sem a janela.",
    ),
    UseCase(
        "UC-36",
        "Consultar o estado",
        "Linha de comando e manutenção",
        ("RF-66", "RF-68"),
        "Mostra o que está de pé e onde (`status`), e o estado da busca, pela "
        "rota de saúde, que consulta o banco de verdade.",
    ),
    UseCase(
        "UC-37",
        "Parar pelo launcher",
        "Linha de comando e manutenção",
        ("RF-66",),
        "Para tudo pela linha de comando (`stop`).",
    ),
    UseCase(
        "UC-38",
        "Completar miniaturas e metadados",
        "Linha de comando e manutenção",
        ("RF-67",),
        "Completa, sem carregar o modelo, as miniaturas e a data e a posição das "
        "fotos indexadas antes dessas funções existirem, com `--force` e "
        "`--dry-run`.",
    ),
    UseCase(
        "UC-39",
        "Ajustar a configuração",
        "Linha de comando e manutenção",
        ("RF-63", "RF-64"),
        "Edita o arquivo opcional `solidvision.env`, com as chaves do "
        "`.env.example`. A interface passa a oferecer só o que a configuração "
        "habilita.",
    ),
)


def _associations(actor: str, *use_cases: str) -> list[Relation]:
    return [Relation(ASSOCIATION, actor, uc) for uc in use_cases]


RELATIONS: tuple[Relation, ...] = (
    *_associations(
        "fotografo",
        *("UC-01", "UC-02", "UC-03", "UC-04", "UC-08", "UC-09", "UC-10"),
        *("UC-11", "UC-12", "UC-14", "UC-20", "UC-32"),
    ),
    *_associations("windows", "UC-03", "UC-04", "UC-08", "UC-10", "UC-30"),
    *_associations("osm", "UC-32"),
    *_associations("nominatim", "UC-34"),
    *_associations("suporte", "UC-35", "UC-36", "UC-37", "UC-38", "UC-39"),
    Relation(GENERALIZATION, "suporte", "fotografo"),
    Relation(INCLUDE, "UC-02", "UC-05"),
    Relation(INCLUDE, "UC-03", "UC-06"),
    Relation(INCLUDE, "UC-04", "UC-06"),
    Relation(INCLUDE, "UC-12", "UC-17"),
    Relation(INCLUDE, "UC-12", "UC-18"),
    Relation(INCLUDE, "UC-12", "UC-19"),
    Relation(INCLUDE, "UC-23", "UC-19"),
    Relation(INCLUDE, "UC-35", "UC-05"),
    Relation(INCLUDE, "UC-37", "UC-06"),
    Relation(INCLUDE, "UC-38", "UC-17"),
    Relation(INCLUDE, "UC-38", "UC-18"),
    Relation(EXTEND, "UC-07", "UC-02", "a subida falhou"),
    Relation(EXTEND, "UC-07", "UC-14", "o job teve falhas"),
    Relation(EXTEND, "UC-13", "UC-12", "só algumas pastas"),
    Relation(EXTEND, "UC-15", "UC-14", "job na fila ou em andamento"),
    Relation(EXTEND, "UC-16", "UC-14", "job cancelado ou parado"),
    Relation(EXTEND, "UC-16", "UC-02", "havia um job interrompido"),
    Relation(EXTEND, "UC-22", "UC-21", "consulta em português"),
    Relation(EXTEND, "UC-24", "UC-29", '"Parecidas com esta"'),
    Relation(EXTEND, "UC-25", "UC-33", '"Ver todas as fotos daqui"'),
    Relation(EXTEND, "UC-26", "UC-20"),
    Relation(EXTEND, "UC-27", "UC-20"),
    Relation(EXTEND, "UC-28", "UC-20"),
    Relation(EXTEND, "UC-26", "UC-32"),
    Relation(EXTEND, "UC-27", "UC-32"),
    Relation(EXTEND, "UC-28", "UC-29", '"Buscar perto daqui"'),
    Relation(EXTEND, "UC-28", "UC-33", '"Buscar perto daqui"'),
    Relation(EXTEND, "UC-29", "UC-20", "abre um resultado"),
    Relation(EXTEND, "UC-30", "UC-29", "disco conectado e função habilitada"),
    Relation(EXTEND, "UC-31", "UC-29"),
    Relation(EXTEND, "UC-33", "UC-32", "clique numa célula"),
    Relation(EXTEND, "UC-34", "UC-32", "digita um endereço"),
    Relation(EXTEND, "UC-34", "UC-28", "digita um endereço"),
    Relation(EXTEND, "UC-40", "UC-12", "job do disco inteiro concluído"),
    Relation(EXTEND, "UC-41", "UC-10", "o disco tem fotos ausentes"),
    Relation(GENERALIZATION, "UC-21", "UC-20"),
    Relation(GENERALIZATION, "UC-23", "UC-20"),
    Relation(GENERALIZATION, "UC-24", "UC-20"),
    Relation(GENERALIZATION, "UC-25", "UC-20"),
)

ACTOR_BY_KEY = {actor.key: actor for actor in ACTORS}
USE_CASE_BY_ID = {uc.id: uc for uc in USE_CASES}

# --------------------------------------------------------------------------
# The diagrams
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """A package drawn in a diagram: a folder with its name on the tab."""

    title: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Note:
    text: str
    x: float
    y: float


@dataclass(frozen=True)
class Route:
    """How one relationship is drawn, when the straight line will not do."""

    via: tuple[tuple[float, float], ...] = ()
    at: float = 0.5
    dx: float = 0.0
    dy: float = 0.0


@dataclass(frozen=True)
class Diagram:
    slug: str
    title: str
    width: float
    height: float
    owns: tuple[str, ...]
    boundary: tuple[float, float, float, float]
    places: dict[str, tuple[float, float]]
    frames: tuple[Frame, ...] = ()
    notes: tuple[Note, ...] = ()
    routes: dict[tuple[str, str, str], Route] = field(default_factory=dict)
    hide: frozenset[tuple[str, str, str]] = frozenset()


Places = dict[str, tuple[float, float]]


def _left(left: float, y: float, *ids: str, step: float = 58) -> Places:
    """Use cases one under the other, their left ends on `left`.

    An actor's lines end at the end of the ellipse on its side, so that, with
    the ends of a column in line, no line from the actor crosses an ellipse
    it is not going to, however steep it is."""
    return {
        uc: (left + ellipse_of(USE_CASE_BY_ID[uc], (0, 0)).rx, y + i * step)
        for i, uc in enumerate(ids)
    }


def _overview() -> Diagram:
    tip, side = 400.0, 830.0
    left, right = tip - 24, 960.0
    places: Places = {
        "fotografo": (110, 561),
        "suporte": (110, 1250),
        "windows": (1100, 390),
        "osm": (1100, 920),
        "nominatim": (1100, 1100),
    }
    places |= _left(tip, 150, "UC-01", "UC-02", "UC-03", "UC-04")
    places |= _left(tip, 428, "UC-08", "UC-09", "UC-10", "UC-11")
    places |= _left(tip, 706, "UC-12", "UC-14")
    places |= _left(tip, 868, "UC-20") | {"UC-29": (side, 868)}
    places |= _left(tip, 972, "UC-32") | {"UC-34": (side, 1030)}
    places |= _left(tip, 1134, "UC-35", "UC-36", "UC-37", "UC-38", "UC-39")
    return Diagram(
        slug="uc-01-visao-geral",
        title="Visão geral",
        width=1200,
        height=1450,
        owns=PACKAGES,
        boundary=(340, 56, 650, 1370),
        places=places,
        frames=(
            Frame("Instalação e execução", left, 100, right - left, 260),
            Frame("Dispositivos", left, 378, right - left, 260),
            Frame("Indexação", left, 656, right - left, 144),
            Frame("Busca", left, 818, 720 - left, 86),
            Frame("Resultados", 736, 818, right - 736, 86),
            Frame("Mapa", left, 922, right - left, 144),
            Frame("Linha de comando e manutenção", left, 1084, right - left, 318),
        ),
    )


def build_diagrams() -> tuple[Diagram, ...]:
    return (
        _overview(),
        Diagram(
            slug="uc-02-dispositivos-e-indexacao",
            title="Dispositivos e indexação",
            width=1390,
            height=1000,
            owns=("Dispositivos", "Indexação"),
            boundary=(280, 56, 910, 918),
            places={
                "fotografo": (100, 488),
                "windows": (1290, 204),
                **_left(330, 146, "UC-08", "UC-09", "UC-10", "UC-11"),
                "UC-41": (900, 320),
                **_left(330, 540, "UC-12"),
                # UC-13 above and UC-40 below: the extensions on the diagonals,
                # the inclusions to the right.
                "UC-13": (760, 440),
                "UC-17": (1010, 470),
                "UC-18": (1010, 540),
                "UC-19": (1010, 610),
                "UC-40": (760, 690),
                **_left(330, 830, "UC-14"),
                "UC-15": (780, 770),
                "UC-16": (780, 840),
                "UC-07": (780, 910),
            },
            frames=(
                Frame("Dispositivos", 306, 96, 854, 260),
                Frame("Indexação", 306, 376, 854, 580),
            ),
        ),
        Diagram(
            slug="uc-03-busca",
            title="Busca",
            width=1410,
            height=810,
            owns=("Busca",),
            boundary=(200, 56, 1000, 726),
            places={
                "fotografo": (100, 260),
                "nominatim": (1320, 470),
                "UC-20": (640, 260),
                "UC-26": (1010, 170),
                "UC-27": (1010, 260),
                "UC-28": (1010, 350),
                "UC-34": (1100, 470),
                "UC-21": (290, 560),
                "UC-23": (540, 560),
                "UC-24": (790, 560),
                "UC-25": (1040, 560),
                "UC-22": (290, 720),
                "UC-19": (540, 720),
            },
        ),
        Diagram(
            slug="uc-04-resultados",
            title="Resultados e detalhe da foto",
            width=1360,
            height=560,
            owns=("Resultados",),
            boundary=(200, 56, 940, 480),
            places={
                "fotografo": (100, 300),
                "windows": (1260, 140),
                "UC-20": (330, 300),
                "UC-29": (640, 300),
                "UC-30": (960, 140),
                "UC-31": (960, 240),
                "UC-24": (960, 360),
                "UC-28": (960, 460),
            },
            routes={
                (EXTEND, "UC-30", "UC-29"): Route(at=0.4),
                (EXTEND, "UC-28", "UC-29"): Route(at=0.4),
            },
            # Drawn in the diagram of the search, they would cross this one.
            hide=frozenset(
                {(GENERALIZATION, "UC-24", "UC-20"), (EXTEND, "UC-28", "UC-20")}
            ),
        ),
        Diagram(
            slug="uc-05-mapa",
            title="Mapa",
            width=1370,
            height=740,
            owns=("Mapa",),
            boundary=(200, 56, 960, 660),
            places={
                "fotografo": (100, 330),
                "osm": (1270, 190),
                "nominatim": (1270, 640),
                "UC-32": (420, 330),
                "UC-26": (320, 150),
                "UC-27": (600, 150),
                "UC-33": (720, 470),
                "UC-28": (1030, 400),
                "UC-25": (1030, 520),
                "UC-34": (720, 640),
            },
            # Drawn in the diagram of the search; here it would cross UC-25's.
            hide=frozenset({(EXTEND, "UC-34", "UC-28")}),
        ),
        Diagram(
            slug="uc-06-instalacao-e-execucao",
            title="Instalação e execução",
            width=1200,
            height=850,
            owns=("Instalação e execução",),
            boundary=(270, 56, 730, 770),
            places={
                "fotografo": (100, 445),
                "windows": (1100, 640),
                **_left(320, 130, "UC-01"),
                **_left(320, 290, "UC-02"),
                "UC-05": (840, 200),
                "UC-07": (840, 290),
                "UC-16": (840, 380),
                **_left(320, 520, "UC-03"),
                **_left(320, 760, "UC-04"),
                # Between the two that include it, so that the Windows' lines
                # to them pass above and below it.
                "UC-06": (640, 640),
            },
        ),
        Diagram(
            slug="uc-07-linha-de-comando",
            title="Linha de comando e manutenção",
            width=1100,
            height=820,
            owns=("Linha de comando e manutenção",),
            boundary=(260, 56, 760, 740),
            places={
                "fotografo": (100, 130),
                "suporte": (100, 450),
                **_left(330, 230, "UC-35", "UC-36", "UC-37", step=100),
                **_left(330, 550, "UC-38"),
                **_left(330, 670, "UC-39"),
                "UC-05": (800, 230),
                "UC-06": (800, 430),
                "UC-17": (800, 520),
                "UC-18": (800, 600),
            },
            notes=(
                Note(
                    "Pela linha de comando, o suporte também\n"
                    "indexa (UC-12) e busca por texto (UC-21) e por\n"
                    "foto de exemplo (UC-23): casos de uso que ele\n"
                    "herda do Fotógrafo.",
                    640,
                    660,
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

FONT = "Arial, Helvetica, sans-serif"

# Advance widths of Arial, in thousandths of an em. Accented letters take
# the width of their base letter.
_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584,
    "?": 556, "@": 1015, "[": 278, "\\": 278, "]": 278, "_": 556, "`": 333,
    "{": 334, "|": 260, "}": 334, "~": 584, "«": 556, "»": 556, "…": 1000,
    "—": 1000, "–": 556,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
    "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
    "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
}  # fmt: skip
_BOLD = {
    "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 611, "c": 556, "d": 611, "e": 556, "f": 333, "g": 611,
    "h": 611, "i": 278, "j": 278, "k": 556, "l": 278, "m": 889, "n": 611,
    "o": 611, "p": 611, "q": 611, "r": 389, "s": 556, "t": 333, "u": 611,
    "v": 556, "w": 778, "x": 556, "y": 556, "z": 500,
}  # fmt: skip


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = 0
    for char in text:
        base = unicodedata.normalize("NFD", char)[0]
        if char.isdigit():
            total += 556
        elif bold and base in _BOLD:
            total += _BOLD[base]
        else:
            total += _WIDTHS.get(base, 556)
    return total * size / 1000


def number(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def text_element(
    x: float,
    y: float,
    content: str,
    size: float,
    fill: str,
    *,
    anchor: str = "middle",
    bold: bool = False,
    italic: bool = False,
) -> str:
    style = ""
    if bold:
        style += ' font-weight="bold"'
    if italic:
        style += ' font-style="italic"'
    return (
        f'<text x="{number(x)}" y="{number(y)}" font-family="{FONT}" '
        f'font-size="{number(size)}" fill="{fill}" text-anchor="{anchor}"{style}>'
        f"{escape(content)}</text>"
    )


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

INK = "#2F3442"
LINE = "#3A3F4B"
UC_FILL, UC_STROKE, UC_TEXT, UC_ID = "#EEF1FB", "#3F4C8A", "#1F2328", "#56608A"
REF_FILL, REF_STROKE, REF_TEXT, REF_ID = "#F2F2F2", "#A3A3A3", "#6A6A6A", "#8C8C8C"
PACKAGE_FILL, PACKAGE_STROKE = "#F6F7F9", "#9AA3B5"
NOTE_FILL, NOTE_STROKE = "#FFFCEB", "#B9A64F"

NAME_SIZE, ID_SIZE = 13.0, 10.5
NAME_STEP, ID_STEP = 15.0, 12.0
LABEL_SIZE = 11.0

Point = tuple[float, float]


@dataclass(frozen=True)
class Ellipse:
    cx: float
    cy: float
    rx: float
    ry: float


def ellipse_of(uc: UseCase, at: Point) -> Ellipse:
    lines = uc.lines
    width = max(text_width(line, NAME_SIZE) for line in lines)
    width = max(width, text_width(uc.id, ID_SIZE))
    height = ID_STEP + NAME_STEP * len(lines)
    ry = height / 2 + 10
    rx = (width / 2) / math.sqrt(1 - ((height / 2) / ry) ** 2) + 8
    return Ellipse(at[0], at[1], rx, ry)


Box = tuple[float, float, float, float]


def actor_boxes(actor: Actor, at: Point) -> tuple[Box, Box]:
    """The figure of an actor, and its name under it: a line stops at both."""
    x, y = at
    lines = (["«sistema»"] if actor.system else []) + actor.lines
    half = max(text_width(line, NAME_SIZE) for line in lines) / 2 + 3
    bottom = y + 46 + 14 * (len(lines) - 1) + 5
    return (x - 17, y - 36, x + 17, y + 30), (x - half, y + 33, x + half, bottom)


def clip_to_ellipse(shape: Ellipse, toward: Point) -> Point:
    dx, dy = toward[0] - shape.cx, toward[1] - shape.cy
    scale = 1 / math.sqrt((dx / shape.rx) ** 2 + (dy / shape.ry) ** 2)
    return (shape.cx + dx * scale, shape.cy + dy * scale)


def leaves_box(origin: Point, toward: Point, box: Box) -> float | None:
    """Where the line from `origin` to `toward` last leaves the box, as a
    fraction of the line, or None when it does not cross it."""
    low, high = -math.inf, math.inf
    for o, d, near, far in (
        (origin[0], toward[0] - origin[0], box[0], box[2]),
        (origin[1], toward[1] - origin[1], box[1], box[3]),
    ):
        if d == 0:
            if not near <= o <= far:
                return None
            continue
        a, b = (near - o) / d, (far - o) / d
        low, high = max(low, min(a, b)), min(high, max(a, b))
    return high if low <= high and high > 0 else None


def draw_actor(actor: Actor, at: Point) -> list[str]:
    x, y = at
    stroke = f'stroke="{INK}" stroke-width="1.6" stroke-linecap="round"'
    out = [
        f'<circle cx="{number(x)}" cy="{number(y - 24)}" r="9" fill="#FFFFFF" '
        f"{stroke}/>",
        f'<path d="M{number(x)} {number(y - 15)}V{number(y + 8)}'
        f"M{number(x - 15)} {number(y - 7)}H{number(x + 15)}"
        f"M{number(x - 12)} {number(y + 28)}L{number(x)} {number(y + 8)}"
        f'L{number(x + 12)} {number(y + 28)}" fill="none" {stroke}/>',
    ]
    baseline = y + 46
    if actor.system:
        out.append(
            text_element(x, baseline, "«sistema»", LABEL_SIZE, "#555B6A", italic=True)
        )
        baseline += 14
    for line in actor.lines:
        out.append(text_element(x, baseline, line, NAME_SIZE, UC_TEXT))
        baseline += 14
    return out


def draw_use_case(uc: UseCase, shape: Ellipse, reference: bool) -> list[str]:
    fill, stroke, ink, id_ink = (
        (REF_FILL, REF_STROKE, REF_TEXT, REF_ID)
        if reference
        else (UC_FILL, UC_STROKE, UC_TEXT, UC_ID)
    )
    out = [
        f'<ellipse cx="{number(shape.cx)}" cy="{number(shape.cy)}" '
        f'rx="{number(shape.rx)}" ry="{number(shape.ry)}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1.3"/>'
    ]
    lines = uc.lines
    top = shape.cy - (ID_STEP + NAME_STEP * len(lines)) / 2
    out.append(text_element(shape.cx, top + 9, uc.id, ID_SIZE, id_ink))
    for i, line in enumerate(lines):
        baseline = top + ID_STEP + 11 + NAME_STEP * i
        out.append(
            text_element(shape.cx, baseline, line, NAME_SIZE, ink, italic=uc.abstract)
        )
    return out


def draw_frame(frame: Frame) -> list[str]:
    tab = text_width(frame.title, 12, bold=True) + 20
    style = f'fill="{PACKAGE_FILL}" stroke="{PACKAGE_STROKE}" stroke-width="1.1"'
    return [
        f'<rect x="{number(frame.x)}" y="{number(frame.y)}" width="{number(tab)}" '
        f'height="22" {style}/>',
        f'<rect x="{number(frame.x)}" y="{number(frame.y + 22)}" '
        f'width="{number(frame.w)}" height="{number(frame.h - 22)}" {style}/>',
        text_element(
            frame.x + 10,
            frame.y + 15.5,
            frame.title,
            12,
            INK,
            anchor="start",
            bold=True,
        ),
    ]


def note_size(note: Note) -> tuple[float, float]:
    lines = note.text.split("\n")
    width = max(text_width(line, 11.5) for line in lines) + 26
    return width, 15 * len(lines) + 16


def draw_note(note: Note) -> list[str]:
    width, height = note_size(note)
    x, y, fold = note.x, note.y, 10
    out = [
        f'<path d="M{number(x)} {number(y)}H{number(x + width - fold)}'
        f"L{number(x + width)} {number(y + fold)}V{number(y + height)}"
        f'H{number(x)}Z" fill="{NOTE_FILL}" stroke="{NOTE_STROKE}" '
        f'stroke-width="1.1"/>',
        f'<path d="M{number(x + width - fold)} {number(y)}'
        f'V{number(y + fold)}H{number(x + width)}" fill="none" '
        f'stroke="{NOTE_STROKE}" stroke-width="1.1"/>',
    ]
    for i, line in enumerate(note.text.split("\n")):
        out.append(
            text_element(x + 12, y + 20 + 15 * i, line, 11.5, INK, anchor="start")
        )
    return out


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def path_point(points: Sequence[Point], at: float) -> tuple[Point, Point]:
    """The point at a fraction of the path's length, and the direction there."""
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:], strict=False)]
    goal = sum(lengths) * at
    for (a, b), length in zip(
        zip(points, points[1:], strict=False), lengths, strict=True
    ):
        if goal <= length or (a, b) == (points[-2], points[-1]):
            t = goal / length if length else 0
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), (
                (b[0] - a[0]) / length,
                (b[1] - a[1]) / length,
            )
        goal -= length
    raise AssertionError("an empty path")


def polyline(points: Sequence[Point], dashed: bool) -> str:
    d = "M" + "L".join(f"{number(x)} {number(y)}" for x, y in points)
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    return f'<path d="{d}" fill="none" stroke="{LINE}" stroke-width="1.2"{dash}/>'


def open_arrow(tip: Point, direction: Point) -> str:
    ux, uy = direction
    nx, ny = -uy, ux
    back = (tip[0] - 11 * ux, tip[1] - 11 * uy)
    a = (back[0] + 5.5 * nx, back[1] + 5.5 * ny)
    b = (back[0] - 5.5 * nx, back[1] - 5.5 * ny)
    return (
        f'<path d="M{number(a[0])} {number(a[1])}L{number(tip[0])} {number(tip[1])}'
        f'L{number(b[0])} {number(b[1])}" fill="none" stroke="{LINE}" '
        f'stroke-width="1.2"/>'
    )


def hollow_triangle(tip: Point, direction: Point) -> str:
    ux, uy = direction
    nx, ny = -uy, ux
    back = (tip[0] - 14 * ux, tip[1] - 14 * uy)
    a = (back[0] + 8 * nx, back[1] + 8 * ny)
    b = (back[0] - 8 * nx, back[1] - 8 * ny)
    return (
        f'<path d="M{number(tip[0])} {number(tip[1])}L{number(a[0])} {number(a[1])}'
        f'L{number(b[0])} {number(b[1])}Z" fill="#FFFFFF" stroke="{LINE}" '
        f'stroke-width="1.2"/>'
    )


def draw_label(center: Point, lines: Sequence[tuple[str, bool]]) -> list[str]:
    widths = [text_width(text, LABEL_SIZE) for text, _ in lines]
    width, height = max(widths) + 8, 13 * len(lines) + 3
    x, y = center[0] - width / 2, center[1] - height / 2
    out = [
        f'<rect x="{number(x)}" y="{number(y)}" width="{number(width)}" '
        f'height="{number(height)}" fill="#FFFFFF" fill-opacity="0.92"/>'
    ]
    for i, (text, italic) in enumerate(lines):
        out.append(
            text_element(
                center[0], y + 11 + 13 * i, text, LABEL_SIZE, INK, italic=italic
            )
        )
    return out


class Canvas:
    """The shapes of one diagram, and where a line to each of them stops."""

    def __init__(self, diagram: Diagram) -> None:
        self.diagram = diagram
        self.ellipses = {
            key: ellipse_of(USE_CASE_BY_ID[key], at)
            for key, at in diagram.places.items()
            if key in USE_CASE_BY_ID
        }

    def center(self, key: str) -> Point:
        return self.diagram.places[key]

    def clip(self, key: str, toward: Point) -> Point:
        if key in self.ellipses:
            return clip_to_ellipse(self.ellipses[key], toward)
        at = self.center(key)
        exits = [
            leaves_box(at, toward, box) for box in actor_boxes(ACTOR_BY_KEY[key], at)
        ]
        t = max(e for e in exits if e is not None)
        return (at[0] + (toward[0] - at[0]) * t, at[1] + (toward[1] - at[1]) * t)

    def relation(self, relation: Relation) -> tuple[list[str], list[str]]:
        """The lines of a relationship, and its label, drawn last."""
        route = self.diagram.routes.get(
            (relation.kind, relation.source, relation.target), Route()
        )
        generalization = relation.kind == GENERALIZATION
        start, end = relation.source, relation.target
        if relation.kind == ASSOCIATION and end in self.ellipses:
            # The end of the ellipse on the actor's side: see `_left`.
            shape = self.ellipses[end]
            side = -1 if self.center(start)[0] < shape.cx else 1
            tip = (shape.cx + side * shape.rx, shape.cy)
            return [polyline([self.clip(start, tip), tip], dashed=False)], []
        middle = list(route.via)
        first = middle[0] if middle else self.center(end)
        last = middle[-1] if middle else self.center(start)
        points = [self.clip(start, first), *middle, self.clip(end, last)]
        tip = points[-1]
        direction = (tip[0] - points[-2][0], tip[1] - points[-2][1])
        length = math.hypot(*direction)
        unit = (direction[0] / length, direction[1] / length)
        lines: list[str] = []
        if generalization:
            base = (tip[0] - 14 * unit[0], tip[1] - 14 * unit[1])
            lines.append(polyline([*points[:-1], base], dashed=False))
            lines.append(hollow_triangle(tip, unit))
            return lines, []
        if relation.kind == ASSOCIATION:
            return [polyline(points, dashed=False)], []
        lines.append(polyline(points, dashed=True))
        lines.append(open_arrow(tip, unit))
        text = [(f"«{relation.kind}»", False)]
        if relation.condition:
            text.append((f"[{relation.condition}]", False))
        center, _ = path_point(points, route.at)
        center = (center[0] + route.dx, center[1] + route.dy)
        return lines, draw_label(center, text)


def relations_in(diagram: Diagram, hidden: bool = False) -> list[Relation]:
    """The relationships whose two ends the diagram places: those it draws,
    or, with `hidden`, those it leaves out."""
    return [
        r
        for r in RELATIONS
        if r.source in diagram.places
        and r.target in diagram.places
        and ((r.kind, r.source, r.target) in diagram.hide) == hidden
    ]


def render(diagram: Diagram) -> str:
    canvas = Canvas(diagram)
    w, h = diagram.width, diagram.height
    tag = f"uc {diagram.title}"
    tag_width = text_width(tag, 12.5, bold=True) + 26
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{number(w)}" '
        f'height="{number(h)}" viewBox="0 0 {number(w)} {number(h)}">',
        f"<title>{escape(tag)}: diagrama de casos de uso do SolidVision</title>",
        f'<rect width="{number(w)}" height="{number(h)}" fill="#FFFFFF"/>',
        f'<rect x="0.5" y="0.5" width="{number(w - 1)}" height="{number(h - 1)}" '
        f'fill="none" stroke="{INK}" stroke-width="1"/>',
        f'<path d="M0.5 0.5H{number(tag_width)}V18L{number(tag_width - 8)} 26'
        f'H0.5Z" fill="#FFFFFF" stroke="{INK}" stroke-width="1"/>',
        f'<text x="10" y="18" font-family="{FONT}" font-size="12.5" fill="{INK}">'
        f'<tspan font-weight="bold">uc</tspan> {escape(diagram.title)}</text>',
    ]
    bx, by, bw, bh = diagram.boundary
    out.append(
        f'<rect x="{number(bx)}" y="{number(by)}" width="{number(bw)}" '
        f'height="{number(bh)}" fill="#FFFFFF" stroke="{INK}" stroke-width="1.4"/>'
    )
    out.append(text_element(bx + bw / 2, by + 24, "SolidVision", 14, INK, bold=True))
    for frame in diagram.frames:
        out.extend(draw_frame(frame))
    labels: list[str] = []
    for relation in relations_in(diagram):
        lines, label = canvas.relation(relation)
        out.extend(lines)
        labels.extend(label)
    for key, shape in canvas.ellipses.items():
        uc = USE_CASE_BY_ID[key]
        out.extend(draw_use_case(uc, shape, uc.package not in diagram.owns))
    for key, at in diagram.places.items():
        if key in ACTOR_BY_KEY:
            out.extend(draw_actor(ACTOR_BY_KEY[key], at))
    for note in diagram.notes:
        out.extend(draw_note(note))
    out.extend(labels)
    out.append("</svg>")
    return "\n".join(out) + "\n"


DIAGRAMS = build_diagrams()


# --------------------------------------------------------------------------
# The document's tables
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    id: str
    section: str


def read_requirements(text: str) -> dict[str, Requirement]:
    """The functional requirements of requisitos.md, with their section."""
    found: dict[str, Requirement] = {}
    section = ""
    for line in text.splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
        match = re.match(r"\| (RF-\d+) \|", line)
        if match:
            found[match.group(1)] = Requirement(match.group(1), section)
    return found


def anchor(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    kept = "".join(c for c in heading.lower() if c.isalnum() or c in " -")
    return kept.replace(" ", "-")


def ids(items: Iterable[str]) -> str:
    return ", ".join(items)


def actors_table() -> str:
    rows = [
        "| Ator | Tipo | Papel |",
        "|---|---|---|",
    ]
    for actor in ACTORS:
        kind = "Primário" if actor.primary else "Secundário"
        kind += ", sistema externo" if actor.system else ", pessoa"
        rows.append(f"| {actor.name} | {kind} | {actor.description} |")
    return "\n".join(rows)


def relations_text(uc: UseCase) -> str:
    parts: list[str] = []

    def targets(kind: str) -> list[Relation]:
        return [r for r in RELATIONS if r.kind == kind and r.source == uc.id]

    def sources(kind: str) -> list[Relation]:
        return [r for r in RELATIONS if r.kind == kind and r.target == uc.id]

    if found := targets(INCLUDE):
        parts.append("inclui " + ids(r.target for r in found))
    if found := targets(EXTEND):
        parts.append(
            "estende "
            + ids(
                f"{r.target} [{r.condition}]" if r.condition else r.target
                for r in found
            )
        )
    if found := targets(GENERALIZATION):
        parts.append("especializa " + ids(r.target for r in found))
    if found := sources(INCLUDE):
        parts.append("incluído por " + ids(r.source for r in found))
    if found := sources(EXTEND):
        parts.append("estendido por " + ids(r.source for r in found))
    if found := sources(GENERALIZATION):
        parts.append("especializado por " + ids(r.source for r in found))
    if not parts:
        return "—"
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


def actors_of(uc: UseCase) -> str:
    direct = [
        ACTOR_BY_KEY[r.source]
        for r in RELATIONS
        if r.kind == ASSOCIATION and r.target == uc.id
    ]
    inside_another = any(
        (r.kind == EXTEND and r.source == uc.id)
        or (r.kind == INCLUDE and r.target == uc.id)
        for r in RELATIONS
    )
    if direct:
        names = ", ".join(actor.name for actor in direct)
        if inside_another and not any(actor.primary for actor in direct):
            names += ", mais os do caso base"
        return names
    parents = [
        r.target for r in RELATIONS if r.kind == GENERALIZATION and r.source == uc.id
    ]
    if parents:
        return "Herdados de " + ids(parents)
    return "Os do caso base"


def catalog() -> str:
    out: list[str] = []
    for package in PACKAGES:
        out.append(f"### {package}\n")
        out.append("| ID | Caso de uso | Atores | Descrição | Relações | Requisitos |")
        out.append("|---|---|---|---|---|---|")
        for uc in USE_CASES:
            if uc.package != package:
                continue
            name = f"*{uc.name}* (abstrato)" if uc.abstract else uc.name
            out.append(
                f"| {uc.id} | {name} | {actors_of(uc)} | {uc.description} | "
                f"{relations_text(uc)} | {ids(uc.requirements)} |"
            )
        out.append("")
    return "\n".join(out).rstrip()


def traceability(requirements: dict[str, Requirement]) -> str:
    covering: dict[str, list[str]] = {rf: [] for rf in requirements}
    for uc in USE_CASES:
        for rf in uc.requirements:
            covering.setdefault(rf, []).append(uc.id)
    rows = ["| Requisito | Grupo | Casos de uso |", "|---|---|---|"]
    for rf, requirement in requirements.items():
        section = requirement.section
        group = re.sub(r"^\d+(\.\d+)*\s+", "", section)
        link = f"[{rf}](requisitos.md#{anchor(section)})"
        rows.append(f"| {link} | {group} | {ids(covering[rf])} |")
    return "\n".join(rows)


GENERATED = re.compile(
    r"(<!-- BEGIN GENERATED: (?P<name>[\w-]+) -->\n)(?P<body>.*?)"
    r"(<!-- END GENERATED: (?P=name) -->)",
    re.S,
)


def fill_document(text: str, sections: dict[str, str]) -> str:
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in sections:
            raise ValueError(f"{DOCUMENT}: no generated section named {name!r}")
        seen.add(name)
        return f"{match.group(1)}{sections[name]}\n{match.group(4)}"

    filled = GENERATED.sub(replace, text)
    missing = sorted(set(sections) - seen)
    if missing:
        raise ValueError(f"{DOCUMENT}: missing the markers of {', '.join(missing)}")
    return filled


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def model_problems(requirements: dict[str, Requirement]) -> list[str]:
    problems: list[str] = []
    known = set(ACTOR_BY_KEY) | set(USE_CASE_BY_ID)
    for r in RELATIONS:
        for end in (r.source, r.target):
            if end not in known:
                problems.append(f"{r.kind} {r.source} -> {r.target}: unknown {end}")
    for uc in USE_CASES:
        if uc.package not in PACKAGES:
            problems.append(f"{uc.id}: unknown package {uc.package!r}")
        if not uc.requirements:
            problems.append(f"{uc.id}: covers no requirement")
        for rf in uc.requirements:
            if rf not in requirements:
                problems.append(f"{uc.id}: {rf} is not in {REQUIREMENTS}")
    covered = {rf for uc in USE_CASES for rf in uc.requirements}
    for rf in requirements:
        if rf not in covered:
            problems.append(f"{rf} has no use case")
    for diagram in DIAGRAMS:
        for key in diagram.places:
            if key not in known:
                problems.append(f"{diagram.slug}: places unknown {key}")
        drawn = {(r.kind, r.source, r.target) for r in relations_in(diagram)}
        for route in diagram.routes:
            if route not in drawn:
                problems.append(f"{diagram.slug}: a route for {route}, not drawn")
        hidden = {(r.kind, r.source, r.target) for r in relations_in(diagram, True)}
        for left_out in diagram.hide - hidden:
            problems.append(f"{diagram.slug}: hides {left_out}, not in the diagram")
    for uc in USE_CASES:
        homes = [d.slug for d in DIAGRAMS if uc.id in d.places and uc.package in d.owns]
        if not homes:
            problems.append(f"{uc.id}: no diagram draws it in full")
    return problems


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def outputs(root: Path) -> tuple[dict[Path, str], list[str]]:
    """Every file the model gives, and what is wrong with the model."""
    requirements = read_requirements((root / REQUIREMENTS).read_text(encoding="utf-8"))
    files = {root / FIGURES / f"{d.slug}.svg": render(d) for d in DIAGRAMS}
    document = root / DOCUMENT
    files[document] = fill_document(
        document.read_text(encoding="utf-8"),
        {
            "atores": actors_table(),
            "catalogo": catalog(),
            "rastreabilidade": traceability(requirements),
        },
    )
    return files, model_problems(requirements)


EDGE = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def rasterize(root: Path, target: Path) -> None:
    """Screenshot each SVG at twice its size, with a headless Edge of its own
    profile, so that an Edge already open is not involved."""
    edge = next((path for path in EDGE if path.exists()), None)
    if edge is None:
        raise FileNotFoundError("Microsoft Edge, for --png")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        for diagram in DIAGRAMS:
            svg = (root / FIGURES / f"{diagram.slug}.svg").resolve()
            png = (target / f"{diagram.slug}.png").resolve()
            subprocess.run(
                [
                    str(edge),
                    "--headless",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--user-data-dir={profile}",
                    "--force-device-scale-factor=2",
                    f"--window-size={int(diagram.width)},{int(diagram.height)}",
                    f"--screenshot={png}",
                    svg.as_uri(),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            print(f"wrote {png}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if any output is stale"
    )
    parser.add_argument(
        "--png", type=Path, metavar="DIR", help="also write PNGs to this folder"
    )
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    files, problems = outputs(args.root)
    for problem in problems:
        print(f"model: {problem}", file=sys.stderr)
    if args.check:
        stale = [
            path
            for path, content in files.items()
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        for path in stale:
            print(f"stale: {path.relative_to(args.root)}", file=sys.stderr)
        return 1 if stale or problems else 0
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(args.root)}")
    if args.png is not None:
        rasterize(args.root, args.png)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
