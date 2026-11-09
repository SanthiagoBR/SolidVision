"""A configuração da aplicação: os campos, e de onde cada modo os lê.

Dois modos, lidos do disco por `layout.py` e nunca declarados (RFC-034 §4.2),
e uma precedência para cada um (§5.1), da mais forte para a mais fraca:

    development                         installed
    1  argumentos do construtor (testes) argumentos do construtor (testes)
    2  o ambiente do processo           --
    3  <repository>\\.env                %LOCALAPPDATA%\\SolidVision\\solidvision.env
    4  defaults                         defaults

Cada modo lê um arquivo, e nunca o do outro. Instalado, o ambiente do
processo não é uma fonte: ali ele é o que os outros programas da máquina
deixaram para trás, e um `DEBUG=*` destinado a um deles impediria este de
importar (§2.3).

A conexão com o cluster embarcado não está em nenhuma das colunas. O engine a
lê do cluster a cada nova conexão, e ela nunca é um campo aqui (§7).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated, Any, ClassVar, NamedTuple, Self

from dotenv import dotenv_values
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.infrastructure.process.supervision import refuse_to_start

from .constants import SUPPORTED_IMAGE_EXTENSIONS
from .layout import Environment, Layout, layout

CONFIG_FILE_ENCODING = "utf-8-sig"
"""UTF-8, com ou sem BOM (RFC-034 §5.3).

O "UTF-8 com BOM" do Bloco de Notas é o caso silencioso: lido como `utf-8`
simples, o BOM vira parte da primeira chave, e a primeira linha do arquivo é
descartada sem erro. Medido em §2.4.
"""

PATH_DEFAULTS: dict[str, Callable[[Layout], Path]] = {
    "thumbnail_directory": lambda where: where.thumbnail_directory,
    "log_directory": lambda where: where.log_directory,
    "embedded_postgres_directory": lambda where: where.postgres_directory,
    "embedded_postgres_runtime_directory": lambda where: where.postgres_runtime,
    "model_runtime_directory": lambda where: where.model_runtime,
    "frontend_directory": lambda where: where.frontend_directory,
}
"""Todo setting de caminho, e de onde vem o default dele: o layout.

Os seis são `Path`, e cada um sai de `Settings` absoluto (§6.3). Um teste
confere que nenhum outro campo é um caminho, e que nenhum destes é relativo.
"""

MODE_DEFAULTS: dict[str, Callable[[Layout], Any]] = {
    "warm_up_models": lambda where: where.environment is Environment.INSTALLED,
    "allow_local_file_actions": lambda where: where.environment
    is Environment.INSTALLED,
}
"""Os settings cujo default depende do modo, e não de um caminho.

`warm_up_models` fica ligado instalado e desligado num checkout (RFC-035 §8),
e `allow_local_file_actions` o acompanha (RFC-038 §2.3).
"""


def _layout_default(name: str) -> Callable[[], Path]:
    """O default de um setting de caminho, para o layout do código em execução."""
    return lambda: PATH_DEFAULTS[name](layout)


def _mode_default(name: str) -> Callable[[], Any]:
    """O default de um setting de modo, para o layout do código em execução."""
    return lambda: MODE_DEFAULTS[name](layout)


RETIRED_KEYS = {
    "ENVIRONMENT": "the mode is read from the installation itself -- development "
    "when pyproject.toml is beside the code -- and nothing sets it (RFC-034 "
    "section 4.2)",
    "INDEXING_ROOT_PATH": "indexing is per device and scopes (RFC-029), and "
    "nothing reads a root path",
    "EMBEDDING_MODEL": "the model is the lockfile's, materialized in "
    "runtime\\models (RFC-035)",
}
"""Chaves que já foram settings, com o que substituiu cada uma (§5.4)."""

DATABASE_KEYS = (
    "database_source",
    "database_host",
    "database_port",
    "database_name",
    "database_user",
    "database_password",
)
"""Sem efeito no arquivo do usuário: instalado, a conexão é sempre a do
cluster embarcado (§7.1)."""


class DatabaseSource(StrEnum):
    """De onde vem a conexão com o banco (RFC-034 §7.1)."""

    EXTERNAL = "external"
    EMBEDDED = "embedded"


class ConfigurationError(Exception):
    """A configuração não pode ser usada, e o processo não pode iniciar com ela.

    Uma linha: o arquivo, a chave e o problema (RFC-034 §5.5). Iniciar com o
    default no lugar de um valor inválido seria pior -- quem apontou
    `THUMBNAIL_DIRECTORY` para outro disco e errou o caminho receberia as
    thumbnails exatamente onde disse que não as queria.
    """


_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_PERCENT_VARIABLE = re.compile(r"%([A-Za-z_][A-Za-z0-9_()]*)%")


def absolute_path(value: str, base: Path) -> Path:
    """Um setting de caminho como sai de `Settings`: absoluto (RFC-034 §6.3).

    As cinco regras, em ordem. Nenhuma delas consulta o disco.
    """
    if not value.strip():
        # Antes das cinco: `THUMBNAIL_DIRECTORY=` é o caminho `.`, que a regra
        # 4 ancoraria na pasta do próprio arquivo -- instalado, as thumbnails
        # cairiam soltas no diretório de dados. Ninguém quer isso, e adivinhar
        # o default no lugar seria o silêncio de §5.5.
        raise ValueError(
            "the value is empty. Delete the line to use the default, or write "
            "the path after the equals sign."
        )
    if _CONTROL_CHARACTER.search(value):
        # Regra 1. Nenhum caminho do Windows tem um. É o rastro de aspas
        # duplas: o formato dotenv lê `\f`, `\r`, `\t` e `\n` dentro delas, e
        # `"D:\fotos\raw\thumbs"` chega em seis fragmentos (§2.4).
        raise ValueError(
            f"{ascii(value)} has a control character in it. Double quotes turn "
            "the \\f, \\n, \\r and \\t of a Windows path into one: write the "
            "path without quotes, spaces included."
        )
    variable = _PERCENT_VARIABLE.search(value)
    if variable:
        # Regra 2. O formato não expande esta forma, e o resultado seria uma
        # pasta chamada `%LOCALAPPDATA%`.
        name = variable.group(1)
        raise ValueError(
            f"{value!r} uses %{name}%, which is not expanded here. "
            f"Write ${{{name}}} instead."
        )
    path = Path(value)
    if not path.is_absolute():
        # Regra 3: as duas formas pela metade do Windows.
        if path.drive:
            raise ValueError(
                f"{value!r} names a drive and no root, so it is relative to "
                f"whatever that drive's current directory is. Write "
                f"{path.drive}\\... instead."
            )
        if path.root:
            raise ValueError(
                f"{value!r} starts with a slash and names no drive, so it "
                "inherits the drive of wherever it is read from. Name the drive."
            )
        # Regra 4: relativo à pasta do arquivo de configuração.
        path = base / path
    # Regra 5.
    return Path(os.path.normpath(path))


class _LayoutDefaults(PydanticBaseSettingsSource):
    """A fonte mais fraca: os defaults do layout que está sendo carregado.

    Os defaults de caminho e os de modo. Os defaults escritos nos campos são
    os do layout do código em execução. É isto que dá a qualquer outro layout
    os seus.
    """

    def __init__(self, settings_cls: type[BaseSettings], where: Layout) -> None:
        super().__init__(settings_cls)
        self._where = where

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        defaults = {**PATH_DEFAULTS, **MODE_DEFAULTS}
        return {name: default(self._where) for name, default in defaults.items()}


class _Without(PydanticBaseSettingsSource):
    """Outra fonte, menos as chaves que não têm efeito neste modo."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        source: PydanticBaseSettingsSource,
        dropped: Sequence[str],
    ) -> None:
        super().__init__(settings_cls)
        self._source = source
        self._dropped = frozenset(dropped)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in self._source().items()
            if name not in self._dropped
        }


class Settings(BaseSettings):
    """As configurações do SolidVision, com tipagem forte.

    `Settings()` lê as fontes do layout em que o código em execução foi
    achado. Um `Settings` de qualquer outro layout vem de `load_settings`.
    """

    model_config = SettingsConfigDict(
        env_file=layout.config_file,
        env_file_encoding=CONFIG_FILE_ENCODING,
        case_sensitive=False,
        # Uma chave que a classe não conhece é descartada aqui e nomeada no log
        # por `load_settings`. Recusar iniciar por causa dela deixaria um
        # arquivo opcional derrubar o app por um erro de digitação (RFC-034
        # §5.4).
        extra="ignore",
    )

    _layout: ClassVar[Layout] = layout

    project_name: str = Field(default="SolidVision", description="Application name")
    project_version: str = Field(default="0.1.0", description="Application version")
    debug: bool = Field(default=False, description="Enable debug mode")

    database_source: DatabaseSource = Field(
        default=DatabaseSource.EXTERNAL,
        description="Where the database connection comes from",
    )
    """`external` ou `embedded` (RFC-034 §7.1).

    `external` são os cinco campos `DATABASE_*` abaixo: o `.env` e o
    contêiner Docker. `embedded` é o cluster do usuário, cuja porta e cuja
    senha o engine pede ao adaptador a cada nova conexão; nenhum dos cinco
    campos é lido então, nem como fallback (§7.4).

    Instalado, isto é sempre `embedded`, diga o arquivo o que disser. Num
    checkout, `DATABASE_SOURCE=embedded` no `.env` é desenvolvimento sem
    Docker.
    """

    database_host: str = Field(default="localhost", description="Database host")
    database_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="Database port",
    )
    database_name: str = Field(default="solidvision", description="Database name")
    database_user: str = Field(default="solidvision", description="Database user")
    # Fora do `repr`, e portanto fora de toda linha de log e todo traceback que
    # imprime as configurações (RFC-034 §7.7). A senha do cluster embarcado nem
    # está aqui.
    database_password: str = Field(
        default="solidvision",
        description="Database password",
        repr=False,
    )

    # `embedding_model` ficava aqui e foi removido pelo RFC-035. Como os modelos
    # carregam de um diretório conferido contra um lockfile no código, o setting
    # só poderia concordar com esse diretório -- redundante -- ou discordar e
    # mentir. O modelo é parte da versão do código, como a migration que fixa
    # `vector(512)` (RFC-035 §4.4).
    embedding_dimension: int = Field(
        default=512,
        ge=1,
        description="Embedding vector size produced by the model of the lockfile",
    )
    """Um valor errado aqui não troca o espaço em silêncio: o adaptador recusa
    todo vetor, já na primeira imagem, nomeando este setting."""
    device: str = Field(
        default="auto",
        description="Inference device: 'auto' (CUDA when available, else CPU), "
        "'cpu', or an explicit torch device string",
    )

    default_collection_name: str = Field(
        default="default",
        description="Default collection name",
    )
    # Dois tamanhos de janela, separados de propósito (RFC-024 §5 e §7.1).
    #
    # `batch_size` é limitado pela memória: toda imagem de um lote é
    # decodificada e pré-processada antes da passada pelo modelo, então
    # aumentá-lo custa RAM na proporção das fotos de origem. 8 é o default
    # entregue porque a curva de ganho medida em CPU se achata ali -- o lote 8
    # captura ~87% de todo o ganho disponível até o lote 128, e o lote 64 foi,
    # de forma mensurável, *mais lento* que o 32, então maior não é melhor com
    # segurança. É um default, não um teto: aumente-o numa máquina com memória
    # sobrando.
    #
    # `metadata_prefetch_size` não tem limite comparável: controla quantos ids
    # entram num SELECT de quatro colunas escalares. Acoplar os dois forçaria
    # um mau compromisso nas duas direções -- 8 ids por consulta mal é melhor
    # que as idas e voltas por arquivo que isto existe para eliminar, e 512
    # imagens decodificadas de uma vez esgotariam a memória com fotos reais.
    batch_size: int = Field(
        default=8,
        ge=1,
        description="Images encoded per model forward pass by the indexing worker",
    )
    metadata_prefetch_size: int = Field(
        default=512,
        ge=1,
        description="Discovered files whose index metadata is read back per query",
    )
    # RFC-045: uma thread por job lê cada arquivo uma vez, à frente do
    # pipeline, e entrega os bytes ao hash, ao CLIP e à thumbnail. Quatro
    # chaves: a chave liga/desliga, o tamanho da fila -- que também é a janela
    # de metadados do job enquanto a leitura está ligada, no lugar de
    # `metadata_prefetch_size` -- e dois tetos para os bytes retidos. O
    # rascunho JPEG com que o CLIP decodifica, de propósito, não é uma delas:
    # mudá-lo muda os vetores.
    indexing_read_ahead: bool = Field(
        default=True,
        description="Read files ahead of the indexing pipeline, in a thread",
    )
    """`false` é o pipeline como era antes do RFC-045, inteiro: sem thread, a
    janela de metadados de `metadata_prefetch_size`, todo arquivo lido do seu
    caminho. A saída de emergência (§5.7), para uma máquina em que a thread
    não compensa -- um SSD rápido, ou memória de menos.
    """
    read_ahead_files: int = Field(
        default=32,
        ge=1,
        description="Files the read-ahead keeps queued, and the job's metadata window",
    )
    """Quanto a thread pode se adiantar ao pipeline, em arquivos (RFC-045 4.4).

    Também é a janela de metadados do job enquanto a leitura antecipada está
    ligada: uma janela de 512 candidatos reteria os bytes de 512 arquivos
    antes da primeira decisão, ~1,9 GB de fotos de drone (§3). A CLI com a
    leitura antecipada desligada e os backfills mantêm
    `metadata_prefetch_size`.
    """
    read_ahead_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1,
        description="Most bytes the read-ahead may hold at once",
    )
    """O teto dos bytes lidos e ainda não liberados (RFC-045 §5.5).

    A thread nunca espera ele baixar: um arquivo que o ultrapassaria segue
    sem os bytes, e é lido do seu caminho, como antes. Com 32 arquivos à
    frente e 3,8 MB por foto, o pipeline retém ~400 MB no pior caso, então
    isto raramente corta alguma coisa.
    """
    read_ahead_max_file_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        description="Largest file the read-ahead reads into memory",
    )
    """Um TIFF de 200 MB vai pelo caminho, em fluxo, como antes (RFC-045 5.1)."""
    # O RFC-028 §6 lê a data de captura do EXIF durante o scan, para todo
    # arquivo descoberto, inclusive os que a verificação incremental depois
    # pula -- é assim que uma foto já indexada ganha uma data sem pagar a
    # inferência. O §11 nomeia o risco: um custo pequeno por arquivo vezes
    # 100.000 arquivos ainda pode dar um total grande. A chave existe para que
    # um custo medido possa ser respondido com configuração, e não com uma
    # mudança de código. Desligada quer dizer "não examinado", nunca "sem
    # data": linhas escaneadas com ela desligada mantêm `capture_source = NULL`
    # e são pegas pelo próximo scan que a tenha ligada, ou por `exif_backfill`.
    extract_capture_date: bool = Field(
        default=True,
        description="Read each discovered file's EXIF capture date during the scan",
    )
    # RFC-032 §4.2: a posição GPS, lida na mesma abertura do arquivo que a
    # data de captura -- uma leitura do cabeçalho, dois fatos --, então
    # ligá-la custa interpretar o IFD de GPS, não uma segunda abertura. A
    # mesma regra da gêmea: desligada quer dizer "não examinado", nunca "sem
    # posição", e linhas escaneadas com ela desligada mantêm
    # `position_source = NULL` para o próximo scan com ela ligada, ou para
    # `exif_backfill`.
    extract_gps: bool = Field(
        default=True,
        description="Read each discovered file's EXIF GPS position during the scan",
    )
    worker_count: int = Field(
        default=1,
        ge=1,
        description="Indexing job executors this machine is expected to run",
    )
    """Ainda sem leitor no código, e o RFC-029 diz por quê.

    O RFC-029 §9 esperava que isto ganhasse um consumidor: vários
    dispositivos podem ser indexados ao mesmo tempo, um executor cada.
    Nada aqui os cria, porque nada aqui precisa -- um executor é
    `python -m app.infrastructure.workers.job_runner`, e rodar dois deles é
    rodar o comando duas vezes. As peças que quebrariam sob concorrência
    foram construídas e testadas para ela (o índice único parcial de §9, e
    a reivindicação com `SKIP LOCKED` de §4.10), então o setting documenta
    a expectativa enquanto o sistema operacional faz as vezes de
    gerenciador de processos.

    Um supervisor que lesse isto e fizesse fork seria um segundo jeito de
    iniciar o mesmo programa, e a primeira coisa a sair de sintonia com a
    forma como ele é de fato implantado -- um processo nativo do Windows,
    não um contêiner (RFC-029 §6).
    """

    # Os quatro ajustes do RFC-029. Os defaults abaixo foram escolhidos
    # contra as medições em `experiments/rfc-029-indexing-jobs/`, que é onde
    # estão os números por trás de cada um -- o RFC-029 §6.1 e §9.1
    # insistiram, os dois, que o timeout em particular viesse de uma
    # distribuição medida, e não de "alguns minutos".
    job_poll_interval: float = Field(
        default=2.0,
        gt=0.0,
        description="Seconds the job executor sleeps between polls of an empty queue",
    )
    """Latência de partida de um job, contra uma operação que dura minutos.

    O custo declarado de consultar em vez de ser avisado (RFC-029 §6.1): um
    job criado logo depois de uma consulta espera até este tanto. Baixá-lo
    troca consultas ociosas por uma espera menor, e a medição diz quanto de
    cada.
    """

    job_heartbeat_interval: float = Field(
        default=10.0,
        gt=0.0,
        description="Minimum seconds between heartbeat writes during a scan",
    )
    """Limite de frequência do heartbeat emitido *enquanto o disco é percorrido*.

    O progresso gravado no fim de uma janela ou de um lote não tem limite de
    frequência -- já é no máximo uma escrita a cada 512 arquivos ou a cada 8
    imagens (RFC-029 §7.3). Isto limita o único callback que dispara por
    arquivo, para que um scan não vire uma escrita por arquivo.
    """

    job_stale_timeout: float = Field(
        default=120.0,
        gt=0.0,
        description="Seconds without a heartbeat after which a job is abandoned",
    )
    """Quanto tempo o silêncio tem de durar antes de o reaper agir.

    Tem de passar do maior intervalo real entre heartbeats, que não é a
    duração de um lote: um scan a frio e uma retomada podem, os dois, passar
    muito tempo sem chegar a um. Curto demais, e o reaper mata jobs
    saudáveis; longo demais, e um dispositivo fica reservado depois de uma
    queda de verdade.
    """

    job_max_attempts: int = Field(
        default=3,
        ge=1,
        description="Times a job may be requeued by the reaper before it fails",
    )
    """O limite que impede um laço de recolocação na fila de rodar para sempre.

    O reaper põe um job abandonado de volta na fila com o checkpoint dele
    (RFC-029 §9.1, corrigido), e é isso que faz o checkpoint ter algum
    leitor. Sem um limite, um arquivo cujo decodificador derruba o processo
    inteiro mataria todo worker que retomasse em cima dele.
    """
    # `NoDecode` tira este campo do comportamento padrão do pydantic-settings
    # para tipos complexos: sem ele, a fonte dotenv/env passa a string crua por
    # `json.loads()` antes de a validação vê-la, e ".jpg,.jpeg,.png" não é
    # JSON -- todo `.env` entregue por este projeto falharia ao carregar. Com
    # ele, a string crua chega a `parse_supported_extensions` abaixo, que faz a
    # separação por vírgulas que `SUPPORTED_EXTENSIONS=.jpg,.jpeg,...` em
    # `.env.example` espera.
    supported_extensions: Annotated[tuple[str, ...], NoDecode] = Field(
        default=SUPPORTED_IMAGE_EXTENSIONS,
        description="Supported image extensions",
    )
    # `indexing_root_path` ficava aqui e foi removido pelo RFC-034, pelo
    # precedente que `minimum_similarity` abriu abaixo: não tinha leitor. O
    # worker de indexação exige `--root`, e os jobs indexam por dispositivo e
    # escopos (RFC-029). Se ficasse, o default relativo dele também seria o
    # único setting de caminho que mudava com o diretório de trabalho.

    # Injetado em `SearchImagesUseCase` pela composition root como o tamanho
    # de página padrão; o caso de uso em si nunca lê configurações.
    #
    # `minimum_similarity` ficava aqui e foi removido pelo RFC-025. Não tinha
    # leitor nenhum, e a faixa declarada, `ge=0.0, le=1.0`, estava errada para
    # as pontuações que a busca de fato produz: a similaridade de cosseno vai
    # de -1 a 1, então o limite teria rejeitado configuração legítima para um
    # filtro que não existia. Um piso de similaridade é trabalho futuro, e
    # precisa da distribuição medida das pontuações reais para escolher um
    # default -- não de uma faixa adivinhada antes de qualquer coisa ser
    # ranqueada.
    top_k_results: int = Field(
        default=10,
        ge=1,
        description="Default number of search results returned per query",
    )

    min_radius_m: float = Field(
        default=300.0,
        ge=0.0,
        description="Smallest search radius, in metres, a 'near here' search accepts",
    )
    """O piso de um círculo de busca (RFC-032 §2.2 e §6). Injetado.

    O GPS grava onde o drone estava, e uma foto oblíqua mostra um terreno a
    dezenas ou centenas de metros dele. Um círculo menor que esse
    deslocamento é uma máquina de respostas erradas: esconde, com total
    confiança e sem erro nenhum, justamente a foto que o usuário procura.
    Então isto é política de produto, não detalhe de interface -- e política
    da Application, injetada em `SearchImagesUseCase` pela composition root,
    nunca lida por ele.

    300 m é medido, por um critério declarado antes do número: o percentil
    95 do deslocamento no solo entre a aeronave e o assunto, `altitude /
    tan(|gimbal pitch|)`, sobre os arquivos DJI reais disponíveis,
    arredondado para cima até os próximos 50 m
    (`experiments/rfc-032-geolocation/measure_gps_cost.log`: p50 96 m, p95
    274 m, máx. 358 m, sobre os 20 quadros cuja câmera grava o pitch -- a
    FC3682 grava um marcador `0.00` e foi excluída). Uma amostra maior da
    coleção pode mudá-lo; o script mede de novo.
    """

    max_map_cells: int = Field(
        default=2000,
        ge=1,
        description="Most cells GET /images/map returns before coarsening its grid",
    )
    """O teto de uma resposta do mapa (RFC-032 §7). Injetado.

    Acima dele, a grade é engrossada uma casa decimal por vez e a resposta
    diz isso em `precision_applied`, em vez de truncar a lista -- quadrados
    mais grossos não escondem foto nenhuma, uma lista cortada esconde
    lugares inteiros.

    2000 é medido, por um critério que limita o que o teto existe para
    limitar -- o payload: o maior de 500/1000/2000/5000 cuja resposta no pior
    caso fica dentro de 256 KB, aos 54,8 bytes por célula medidos no JSON
    real de `MapResponseSchema` (2000 células -> ~107 KB; 5000 -> ~268 KB).
    `experiments/rfc-032-geolocation/planner_check.log` tem os números, e a
    primeira versão do critério, que também limitava o tempo e não escolheu
    nada porque o tempo que media era o do scan completo, não o do teto.
    """

    # Carregar o checkpoint CLIP no início da API fecha dois problemas de uma
    # vez (RFC-026 §9.1 e §10): a partida a frio de ~5 s que a primeira
    # consulta pagaria, e a corrida verificar-depois-agir dentro da carga sob
    # demanda do adaptador, que só existe porque um endpoint `def` roda num
    # threadpool e duas primeiras requisições concorrentes podem, as duas,
    # achar o modelo descarregado.
    #
    # O default vem do modo (RFC-035 §8): ligado instalado, desligado num
    # checkout. O RFC-026 escolheu `False` em todo lugar para que importar o
    # app nunca baixasse 600 MB; carregar não baixa mais nada, mas o exemplo
    # continua valendo por outro motivo: `TestClient(app)` usado como context
    # manager roda os eventos de startup, então um `True` num checkout faria
    # `tests/presentation/test_health.py` carregar e calcular o hash de 924 MB,
    # ou falhar sem `runtime\models`. Instalado, não há suíte, e ninguém
    # escreve o arquivo do usuário: um `False` ali deixaria a API fria para
    # sempre. O arquivo ainda pode mudá-lo nos dois modos.
    warm_up_models: bool = Field(
        default_factory=_mode_default("warm_up_models"),
        description="Load the embedding model at API startup "
        "instead of on the first request",
    )

    # RFC-030 §6, guarda 1. Com ela desligada, `/reveal` responde 404 -- a
    # rota se comporta como se não existisse, em vez de existir e recusar.
    #
    # O default vem do modo desde o RFC-038 (§2.3): ligado instalado,
    # desligado num checkout. O RFC-030 escolheu `False` em todo lugar pelo
    # precedente que `warm_up_models` abria então -- o default seguro é o que
    # não pode agir numa máquina que nunca pediu --, e esse precedente mudou
    # (RFC-035 §8): instalado, ninguém escreve o arquivo do usuário, e um
    # `False` significaria um botão "abrir no Explorer" que nunca aparece. Num
    # checkout continua desligado, para que nada na suíte abra uma janela; o
    # `.env` ou o ambiente o ligam. O arquivo decide nos dois modos.
    #
    # É *uma* de duas guardas, e não substitui a outra: seja qual for o valor
    # disto, `/reveal` recusa todo chamador que não esteja no loopback, e as
    # verificações de `Host` e `Origin` do RFC-037 ficam na frente das duas.
    # Esta é a intenção do operador; aquela é um fato sobre quem está
    # chamando. O caminho do arquivo nos resultados da busca, de propósito, não
    # fica atrás deste setting -- mostrar onde uma foto está é o trabalho
    # principal do produto.
    allow_local_file_actions: bool = Field(
        default_factory=_mode_default("allow_local_file_actions"),
        description="Allow POST /api/v1/images/{id}/reveal to open the file "
        "manager on this machine (loopback callers only)",
    )

    # RFC-043, opção B. Ligado por default: o endereço só sai da máquina quando
    # o usuário aperta Enter ao lado de uma linha que diz para onde ele vai.
    allow_address_search: bool = Field(
        default=True,
        description="Let the interface find a typed address by sending it to "
        "ADDRESS_SEARCH_URL, an online service",
    )
    # Um setting porque a política de uso do Nominatim exige poder trocar o
    # serviço sem uma atualização do software.
    address_search_url: str = Field(
        default="https://nominatim.openstreetmap.org/search",
        description="The Nominatim-compatible /search endpoint the address search asks",
    )

    api_port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="The port the launcher serves the API on; unset, it chooses one",
    )
    """Sem valor, o launcher do RFC-036 escolhe a porta (§5.4).

    A porta da última partida que chegou a `ready`, se ainda estiver livre,
    e senão uma que o sistema der; nunca um 8000 fixo, que é uma das portas
    que servidores de desenvolvimento mais usam. Com valor, é uma promessa a
    alguém -- um script, o proxy do servidor de desenvolvimento de um
    frontend --, e o launcher usa exatamente esta porta ou se recusa a
    iniciar, com uma linha dizendo isso, em vez de escolher outra. O
    endereço é sempre `127.0.0.1`, e não é um setting: servir a rede não é
    algo que esta API faça (RFC-030 §6).
    """

    # O default de cada um dos seis settings de caminho vem do layout, por
    # `PATH_DEFAULTS`, porque depende do modo. O que está escrito no campo é o
    # default para o layout do código em execução; para qualquer outro layout,
    # `load_settings` fornece o daquele layout. Todo caminho sai desta classe
    # absoluto, seja quem for que o escreveu, e como (RFC-034 §6.3).
    thumbnail_directory: Path = Field(
        default_factory=_layout_default("thumbnail_directory"),
        description="Directory the application keeps thumbnails in",
    )
    """O cache de thumbnails (RFC-030 §7.1). Nunca dentro de uma coleção.

    O default é `thumbnails` sob o diretório de dados do usuário --
    `%LOCALAPPDATA%\\SolidVision`, ou `SolidVision-dev` num checkout, para que
    as thumbnails de um desenvolvedor e as de um app instalado nunca dividam
    uma pasta: elas são nomeadas pelo id da imagem, e um id só quer dizer
    algo dentro de um banco (RFC-034 §2.5). Esteja onde estiver, os scans a
    pulam (`FilesystemImageProvider(excluded_directories=...)`), então
    indexar um disco de sistema inteiro não indexa as thumbnails.
    """

    thumbnail_max_edge: int = Field(
        default=512,
        ge=16,
        description="Longest side, in pixels, of a generated thumbnail",
    )
    """512, o tamanho que o RFC-030 §7.2 usou no seu exemplo.

    Mudá-lo não regera as thumbnails existentes -- para isso, rode o
    backfill de thumbnails com `--force`. O `ETag` delas também não muda,
    porque é o hash do conteúdo da *foto*: um cliente que já tem uma
    thumbnail no tamanho antigo a mantém até a própria foto mudar, o que dá
    uma imagem menor da foto certa, e não uma da foto errada.
    """

    embedded_postgres_runtime_directory: Path = Field(
        default_factory=_layout_default("embedded_postgres_runtime_directory"),
        description="Directory holding the vendored PostgreSQL runtime",
    )
    """Onde estão os binários embarcados (RFC-033 §5.2). Nunca o `PATH`.

    O default é `runtime\\postgres` sob a raiz do layout. Num checkout, essa é
    a raiz do repositório, e `scripts/vendor_postgres.py` materializa o
    runtime ali; instalado, é onde quer que o instalador ponha o código, com o
    runtime ao lado.

    `EMBEDDED_` mantém os dois nomes fora do namespace `POSTGRES_*`, que a
    imagem Docker lê do mesmo `.env`.
    """

    model_runtime_directory: Path = Field(
        default_factory=_layout_default("model_runtime_directory"),
        description="Directory holding the two models, materialized from the lockfile",
    )
    """De onde o CLIP e o tradutor são carregados (RFC-035 §4.5).

    O default é `runtime\\models` sob a raiz do layout, ao lado de
    `runtime\\postgres`: `scripts/vendor_models.py` o materializa num
    checkout, e o instalador o põe ao lado do código. Configurável pelo mesmo
    motivo do runtime do PostgreSQL: vários checkouts podem dividir uma árvore
    em vez de guardar 924 MB cada um. Apontá-lo para outro lugar não muda o
    modelo, porque qualquer árvore que ele nomeie é conferida com o lockfile
    do código antes de qualquer coisa nela ser carregada (§6).
    """

    embedded_postgres_directory: Path = Field(
        default_factory=_layout_default("embedded_postgres_directory"),
        description="Per-user directory the embedded PostgreSQL cluster lives in",
    )
    """O cluster do usuário (RFC-033 §6): `postgres` sob o diretório de dados.

    `LOCALAPPDATA` pelo motivo das thumbnails e mais um: o OneDrive não o
    sincroniza, e um cliente de sincronização que trava ou reescreve arquivos
    dentro de um cluster em funcionamento corrompe o cluster. O adaptador
    acrescenta a versão major abaixo disto (`postgres\\16\\data`), para que um
    `17\\` futuro caiba ao lado. O cluster de um checkout e o de um app
    instalado ficam separados por default desde o RFC-034; o setting é para
    testes, para diagnóstico e para um disco de sistema pequeno demais para o
    cluster.
    """

    frontend_directory: Path = Field(
        default_factory=_layout_default("frontend_directory"),
        description="Directory holding the interface's build, which the API serves",
    )
    """O que a API serve em `/` e `/assets` (RFC-037 §5.1).

    O default é `frontend\\dist` sob a raiz do layout: onde `npm run build`
    escreve num checkout. Sem um `index.html` ali, a API serve só a API, e
    diz onde procurou numa linha ao iniciar -- `npm run dev` serve a
    interface pelo Vite e não precisa de nada disto, nem a suíte.
    """

    log_level: str = Field(default="INFO", description="Logging level")
    log_directory: Path = Field(
        default_factory=_layout_default("log_directory"),
        description="Directory for log files",
    )
    """`logs` sob o diretório de dados (RFC-034 §8.1).

    O log do próprio servidor fica onde o RFC-033 o pôs, em
    `postgres\\16\\data\\log`: dois lugares, um por dono.
    """

    log_filename: str | None = Field(
        default=None,
        description="Log filename; unset, the name of the program that is running",
    )
    """Sem valor, cada programa escreve o seu arquivo (RFC-034 §8.2).

    `uvicorn.log`, `job_runner.log`, `pytest.log`: dois programas acrescentando
    a um mesmo arquivo rotativo perdem as linhas de quem escreve mais (§2.6).
    Definí-lo dá a todo processo o mesmo nome fixo, como antes.
    """

    @property
    def environment(self) -> Environment:
        """O modo do layout para o qual estas configurações foram carregadas.

        Não é mais um campo: um modo que alguém escreve tem um default que
        mente quando ninguém o escreve (RFC-034 §4.2). `/health` ainda o
        informa.
        """
        return type(self)._layout.environment

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """A precedência do RFC-034 §5.1, decidida pelo modo."""
        defaults = _LayoutDefaults(settings_cls, cls._layout)
        if cls._layout.environment is Environment.INSTALLED:
            user_file = _Without(settings_cls, dotenv_settings, DATABASE_KEYS)
            return (init_settings, user_file, defaults)
        return (init_settings, env_settings, dotenv_settings, defaults)

    @field_validator(*PATH_DEFAULTS, mode="before")
    @classmethod
    def anchor_path(cls, value: object) -> object:
        """Torna todo caminho absoluto aqui, uma vez, em vez de em cada consumidor."""
        if isinstance(value, str | Path):
            return absolute_path(str(value), cls._layout.config_base)
        return value

    @field_validator("database_source", mode="before")
    @classmethod
    def normalize_database_source(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def installed_is_always_embedded(self) -> Self:
        """Instalado, não há banco externo para onde apontar (§7.1)."""
        if type(self)._layout.environment is Environment.INSTALLED:
            self.database_source = DatabaseSource.EMBEDDED
        return self

    @field_validator("supported_extensions", mode="before")
    @classmethod
    def parse_supported_extensions(cls, value: object) -> object:
        """Separa a forma com vírgulas que `NoDecode` deixa intocada.

        Só strings precisam disto: o default (já uma tupla) e a construção
        programática (p. ex. nos testes) passam uma tupla direto para
        `validate_supported_extensions`.
        """
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("supported_extensions")
    @classmethod
    def validate_supported_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Garante que as extensões suportadas fiquem normalizadas em minúsculas."""
        return tuple(ext.lower() for ext in value)


@dataclass(frozen=True)
class ConfigurationReport:
    """De onde veio a configuração de um processo (RFC-034 §5.6).

    O que torna a precedência verificável na máquina de um usuário: o log diz
    qual arquivo foi lido. `settings` não pode registrá-lo -- o logger importa
    este módulo --, então ele é devolvido ao lado dos `Settings`, e o logger o
    escreve na primeira vez em que é configurado. A senha não está nele.
    """

    environment: Environment
    config_file: Path
    config_file_exists: bool
    data_directory: Path
    directories: tuple[tuple[str, Path], ...]
    database_source: DatabaseSource
    warnings: tuple[str, ...]
    """Chaves que não fazem o que parecem fazer (§5.4), pelo nome."""

    def lines(self) -> tuple[str, ...]:
        found = "read" if self.config_file_exists else "absent, defaults in use"
        return (
            f"Configuration: mode {self.environment.value}",
            f"Configuration: file {self.config_file} ({found})",
            f"Configuration: data directory {self.data_directory}",
            *(f"Configuration: {name} {path}" for name, path in self.directories),
            f"Configuration: database source {self.database_source.value}",
        )


class LoadedSettings(NamedTuple):
    settings: Settings
    report: ConfigurationReport


def load_settings(where: Layout, **overrides: Any) -> LoadedSettings:
    """As configurações de um layout, e o relatório de onde elas vieram.

    As fontes são decididas aqui, antes de qualquer `Settings` ser construído,
    porque dependem do modo (§5.2). `overrides` são argumentos do construtor,
    a fonte mais forte nos dois modos; existem para os testes.
    """
    file_keys = _keys_of(where.config_file)
    try:
        loaded = _settings_class(where)(**overrides)
    except ValidationError as error:
        raise ConfigurationError(_explain(error, where, file_keys)) from None
    return LoadedSettings(loaded, _report(loaded, where, file_keys))


@cache
def _settings_class(where: Layout) -> type[Settings]:
    """`Settings`, presa a um layout. Uma classe por layout, para que duas
    cargas do mesmo layout deem instâncias que possam ser comparadas."""
    if where == Settings._layout:
        return Settings

    class LayoutSettings(Settings):
        model_config = SettingsConfigDict(env_file=where.config_file)

        _layout: ClassVar[Layout] = where

    return LayoutSettings


def _keys_of(config_file: Path) -> tuple[str, ...]:
    """As chaves escritas no arquivo, como foram escritas. Vazio sem arquivo."""
    if not config_file.is_file():
        return ()
    try:
        return tuple(dotenv_values(config_file, encoding=CONFIG_FILE_ENCODING))
    except UnicodeDecodeError:
        raise ConfigurationError(
            f"{config_file}: the file is not UTF-8 text. Save it again as UTF-8 "
            "(Notepad: Save As, Encoding: UTF-8)."
        ) from None


def _explain(error: ValidationError, where: Layout, file_keys: Sequence[str]) -> str:
    """Uma linha: onde cada valor ruim foi escrito, a chave dele e o que há de errado.

    O valor em si fica de fora. Pode ser uma senha.
    """
    in_file = {key.lower() for key in file_keys}
    in_environment = {name.lower() for name in os.environ}
    problems = []
    for detail in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        name = str(detail["loc"][0]) if detail["loc"] else "settings"
        if where.environment is Environment.DEVELOPMENT and name in in_environment:
            origin = "environment variable"
        elif name in in_file:
            origin = str(where.config_file)
        else:
            origin = "constructor argument"
        message = detail["msg"].removeprefix("Value error, ")
        problems.append(f"{origin}: {name.upper()}: {message}")
    return "; ".join(problems)


def _report(
    loaded: Settings, where: Layout, file_keys: Sequence[str]
) -> ConfigurationReport:
    return ConfigurationReport(
        environment=where.environment,
        config_file=where.config_file,
        config_file_exists=where.config_file.is_file(),
        data_directory=where.data_directory,
        directories=tuple((name, getattr(loaded, name)) for name in PATH_DEFAULTS),
        database_source=loaded.database_source,
        warnings=_warnings(where, file_keys),
    )


def _warnings(where: Layout, file_keys: Sequence[str]) -> tuple[str, ...]:
    """§5.4: os três tipos de chave que, sem isto, falham em silêncio."""
    file = where.config_file
    installed = where.environment is Environment.INSTALLED
    warnings = []
    for key in file_keys:
        if key.upper() in RETIRED_KEYS:
            warnings.append(
                f"{file}: {key} is retired and was ignored: "
                f"{RETIRED_KEYS[key.upper()]}."
            )
        elif installed and key.lower() in DATABASE_KEYS:
            warnings.append(
                f"{file}: {key} has no effect here and was ignored: an installed "
                "SolidVision always uses its own embedded database."
            )
        elif key.lower() not in Settings.model_fields:
            warnings.append(
                f"{file}: {key} is not a setting and was ignored. "
                "Check the spelling against .env.example."
            )
    # O Bloco de Notas salva um arquivo novo chamado `solidvision.env` como
    # `solidvision.env.txt` e esconde a extensão, então o arquivo que o usuário
    # vê não é o arquivo que é lido (§5.3; uma hipótese até o §16 testá-la).
    mistaken = file.with_name(file.name + ".txt")
    if installed and not file.is_file() and mistaken.is_file():
        warnings.append(
            f"{mistaken} exists and {file.name} does not: the configuration file "
            f"was saved with a .txt extension and was not read. Rename it to "
            f"{file.name}."
        )
    return tuple(warnings)


def _load_or_exit() -> LoadedSettings:
    """As configurações do próprio processo, ou uma linha no stderr e o código 78.

    Sem traceback (§5.5): quem lê isto é um usuário ou o launcher do RFC-036,
    e a linha já nomeia o arquivo e a chave. 78 é o que diz ao launcher que
    reiniciar não vai ajudar (RFC-036 §6.1).
    """
    try:
        return load_settings(layout)
    except ConfigurationError as error:
        refuse_to_start(f"SolidVision cannot start: {error}")


settings, configuration_report = _load_or_exit()
