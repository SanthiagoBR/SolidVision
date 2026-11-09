"""Os dois modelos como runtime: um setting os acha, o código os confere (RFC-035).

O lockfile é código. `models.lock.json`, ao lado deste módulo, nomeia o
repositório, a revisão e cada arquivo do CLIP e do tradutor, com o tamanho e o
sha256 de cada um (§4.1). Trocar um modelo é um commit nele, revisado, e não
uma linha num arquivo de configuração.

`scripts/vendor_models.py` materializa o lockfile numa árvore, por default
`runtime\\models` ao lado do código (§4.2)::

    <runtime>\\
        SOLIDVISION-MODELS.json              o lockfile que fez a árvore
        clip-vit-b-32-laion2b-s34b-b79k\\     os 8 arquivos do snapshot do CLIP
        opus-mt-romance-en\\                  os 7 arquivos do tradutor

e este módulo é a outra metade: confere a árvore com o lockfile do código que
está rodando, e só então entrega um diretório a um adaptador.

O que é conferido, e quando (§6.1):

    no início de um processo       o manifesto; todo arquivo dos dois modelos
    que vai carregar modelos       presente, com o tamanho (`check_presence`)
    antes de cada carga de modelo  o mesmo para aquele modelo, e o sha256 de
                                   cada arquivo dele (`verified_directory`)

**O sha256 antes da carga, nunca depois.** É o que dá a mensagem certa: um
arquivo truncado carregado primeiro chega ao usuário como um traceback do
torch (§6.2). Não sobra pickle a proteger: o `pytorch_model.bin` do tradutor é
convertido para `safetensors` por `vendor_models.py`, que confere o hash do
pickle antes de desserializá-lo (o segundo ramo de §7.4), e a árvore não tem
pickle nenhum.

**O segundo ramo de §6.3.** Calcular o hash dos pesos custa várias vezes o que
custa carregá-los (RFC-035 §15: os 605 MB do CLIP dão o hash em 2,4 s a quente
e 4,7 s a frio, e carregam em 0,6 s e 2,0 s), então um arquivo cujo caminho,
tamanho, data de modificação e file id do NTFS são os de um arquivo já
conferido contra este lockfile não tem o hash calculado de novo. Esse
registro, `models-verified.json` na pasta de cache do usuário, é gravado
depois de cada hash bem-sucedido. Presença e tamanho continuam conferidos em
toda carga. Do que o ramo abre mão, declarado: uma corrupção do disco que não
muda nem o tamanho nem as datas só é pega na próxima verificação completa.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from app.infrastructure.config.layout import Environment, Layout, layout
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

LOCKFILE_NAME = "models.lock.json"
MANIFEST_NAME = "SOLIDVISION-MODELS.json"
MANIFEST_FORMAT = 1
RECORD_NAME = "models-verified.json"
RECORD_FORMAT = 1

CLIP = "clip"
TRANSLATOR = "translator"
"""As chaves do lockfile: o que o código pede pelo nome."""

REMEDIES = {
    Environment.DEVELOPMENT: (
        "Materialize the models with `python scripts/vendor_models.py`."
    ),
    Environment.INSTALLED: "Reinstall SolidVision.",
}
"""O que fazer, no fim de toda mensagem: o que dá para fazer depende do modo."""

ModelDirectory = Callable[[], Path]
"""O que um adaptador recebe no lugar de um nome: chamado na primeira carga,
devolve o diretório do modelo, conferido, ou levanta `ModelUnavailableError`.

Um callable, e não um caminho, para que construir um adaptador continue de
graça (RFC-023 §9) e a verificação aconteça na carga, não antes.
"""


class ModelUnavailableError(Exception):
    """Um modelo não pode ser carregado, e a mensagem é uma linha dizendo por quê.

    O modelo, o arquivo, o problema e o que fazer (§6.4), e nunca o conteúdo
    de arquivo nenhum: nomes, tamanhos e o prefixo de um hash. Quem a mostra
    decide o resto (§6.5): o job runner e a CLI a imprimem e saem com 78, o
    código do RFC-036 §6.1; a API responde 503 com ela nas rotas que precisam
    de um modelo. Reiniciar não resolve nenhum dos casos.
    """


@dataclass(frozen=True)
class LockedFile:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class LockedModel:
    key: str
    repository: str
    revision: str
    directory: str
    license: str
    files: tuple[LockedFile, ...]


@dataclass(frozen=True)
class Lockfile:
    """O lockfile, interpretado, e o digest que o nomeia num manifesto."""

    models: Mapping[str, LockedModel]
    sha256: str

    @classmethod
    def parse(cls, content: bytes) -> Lockfile:
        """O lockfile a partir dos bytes, ou `ValueError` dizendo o que está errado."""
        raw = json.loads(content.decode("utf-8"))
        if not isinstance(raw, dict) or not raw:
            raise ValueError("the lockfile names no model")
        models = {}
        for key, entry in raw.items():
            files = entry.get("files") if isinstance(entry, dict) else None
            if not isinstance(files, dict) or not files:
                raise ValueError(f"{key} lists no files")
            models[key] = LockedModel(
                key=key,
                repository=str(entry["repository"]),
                revision=str(entry["revision"]),
                directory=str(entry["directory"]),
                license=str(entry.get("license", "")),
                files=tuple(
                    LockedFile(name, int(spec["size"]), str(spec["sha256"]))
                    for name, spec in sorted(files.items())
                ),
            )
        return cls(models=models, sha256=lockfile_digest(content))

    def model(self, key: str) -> LockedModel:
        try:
            return self.models[key]
        except KeyError:
            raise ModelUnavailableError(
                f"The lockfile names no model {key!r}; this is a defect of this "
                "version of SolidVision."
            ) from None


def lockfile_digest(content: bytes) -> str:
    """O sha256 do lockfile, fins de linha normalizados, como o manifesto o nomeia.

    O Git faz o checkout do arquivo com CRLF ou LF conforme `core.autocrlf`; o
    digest tem de ser o mesmo em toda máquina. `vendor_models.py` o calcula do
    mesmo jeito.
    """
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


@functools.cache
def read_lockfile() -> Lockfile:
    """O lockfile do código em execução, lido como dado deste pacote.

    Por `importlib.resources`, e não por um caminho subido a partir de um
    arquivo: o lockfile viaja com o código, e um app instalado não tem
    `scripts\\`.
    """
    content = (resources.files("app.infrastructure.ai") / LOCKFILE_NAME).read_bytes()
    try:
        return Lockfile.parse(content)
    except (ValueError, KeyError, TypeError) as error:
        raise ModelUnavailableError(
            f"The lockfile of this version, {LOCKFILE_NAME}, could not be read "
            f"({error!r}); this is a defect of this version of SolidVision."
        ) from None


@functools.cache
def default_runtime() -> ModelRuntime:
    """A árvore que `settings.model_runtime_directory` nomeia, para este processo.

    Conferida com o lockfile do código em execução, nunca tida como confiável:
    apontar o setting para outra árvore só consegue apontá-lo para uma cópia
    idêntica (RFC-035 §4.5). Construí-la lê o lockfile, que é pequeno, e mais
    nada.

    Uma por processo, para que uma recusa de que ela se lembra -- um arquivo
    cujo hash não bateu -- valha para todo chamador. Aqui, e não na
    composition root, porque um processo que só confere a presença no início
    -- o job runner, a CLI -- tem de poder recusar sem importar `torch`
    antes, o que leva mais tempo que tudo o que ele faz até ali.
    """
    from app.infrastructure.config.settings import settings

    return ModelRuntime.of(settings.model_runtime_directory, layout)


def huggingface_is_offline() -> bool:
    """Se o `huggingface_hub` recusaria toda requisição.

    Importado aqui, na primeira carga, e não no topo: a essa altura o
    `__init__` do pacote já rodou, então um primeiro import aqui vê a
    variável.
    """
    import huggingface_hub

    return bool(huggingface_hub.is_offline_mode())


def load_failed(directory: Path) -> ModelUnavailableError:
    """A linha seis de §6.4: todo arquivo conferiu, e a carga falhou mesmo assim.

    Chamada dentro de um bloco `except`: o traceback vai para o log, e a
    linha devolvida não o repete.
    """
    logger.exception("Loading the model in %s failed", directory)
    return ModelUnavailableError(
        f"The model in {directory} could not be loaded; the traceback is in the log."
    )


class ModelRuntime:
    """Uma árvore de modelos, recusada a não ser que seja a que o lockfile descreve."""

    def __init__(
        self,
        root: Path,
        lockfile: Lockfile,
        environment: Environment,
        record_file: Path | None,
        offline: Callable[[], bool] = huggingface_is_offline,
    ) -> None:
        if not root.is_absolute():
            raise ValueError(f"The model runtime must be absolute, not {root}.")
        self._root = root
        self._lockfile = lockfile
        self._remedy = REMEDIES[environment]
        self._record_file = record_file
        self._offline = offline
        self._lock = threading.Lock()
        self._refused: dict[tuple[str, str], str] = {}
        """Arquivos cujo hash já foi recusado, por caminho e identidade.

        Para que uma árvore corrompida custe um hash, e não um por requisição
        de busca respondida com 503 -- enquanto um arquivo substituído desde
        então, e que por isso tem outra identidade, tem o hash calculado de
        novo: consertar a árvore não exige reiniciar.
        """

    @classmethod
    def of(cls, root: Path, where: Layout) -> ModelRuntime:
        """A árvore em `root`, conferida com o lockfile do código em execução."""
        return cls(
            root,
            read_lockfile(),
            environment=where.environment,
            record_file=where.cache_directory / RECORD_NAME,
        )

    def loader(self, key: str) -> ModelDirectory:
        """O que um adaptador recebe: o diretório conferido, na primeira carga."""
        return functools.partial(self.verified_directory, key)

    def check_presence(self) -> None:
        """A verificação do início de um processo: barata, e dos dois modelos.

        O manifesto, e todo arquivo de todo modelo presente com o tamanho: um
        JSON pequeno e quinze `stat`s. Acha uma instalação quebrada quando o
        processo começa, e não na primeira busca em português.
        """
        self._check_manifest()
        for model in self._lockfile.models.values():
            self._stat_files(model)

    def verified_directory(self, key: str) -> Path:
        """O diretório de um modelo, depois de conferidos por inteiro os arquivos dele.

        Uma thread por vez: duas primeiras requisições que achem o adaptador
        descarregado calculam o hash uma vez, e a segunda acha o registro que
        a primeira gravou.
        """
        model = self._lockfile.model(key)
        with self._lock:
            self._require_offline(model)
            self._check_manifest()
            stats = self._stat_files(model)
            self._verify_contents(model, stats)
        return self._root / model.directory

    # ------------------------------------------------------------------

    def _require_offline(self, model: LockedModel) -> None:
        """§5.2: a variável só funciona se veio antes do import.

        Algo que importou `huggingface_hub` antes deste pacote -- um
        `sitecustomize`, uma ferramenta, um teste -- o deixou lendo o ambiente
        como estava naquela hora. Carregar mesmo assim carregaria com a rede
        ligada.
        """
        if not self._offline():
            raise ModelUnavailableError(
                f"{model.repository} was not loaded: huggingface_hub was imported "
                "before app.infrastructure.ai turned its offline mode on, so it "
                "would reach the network. Import app.infrastructure.ai before "
                "anything that imports transformers or huggingface_hub."
            )

    def _check_manifest(self) -> None:
        path = self._root / MANIFEST_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ModelUnavailableError(
                f"The models are not in {self._root}. {self._remedy}"
            ) from None
        except (OSError, ValueError) as error:
            raise ModelUnavailableError(
                f"{path} could not be read ({error}). {self._remedy}"
            ) from None
        if (
            not isinstance(raw, dict)
            or raw.get("format") != MANIFEST_FORMAT
            or not isinstance(raw.get("lockfile_sha256"), str)
        ):
            raise ModelUnavailableError(
                f"{path} is not a manifest this version of SolidVision can read. "
                f"{self._remedy}"
            )
        found = raw["lockfile_sha256"]
        if found != self._lockfile.sha256:
            raise ModelUnavailableError(
                f"The models in {self._root} are from another version of "
                f"SolidVision: they were materialized from lockfile {found[:12]}, "
                f"and this version's is {self._lockfile.sha256[:12]}. {self._remedy}"
            )

    def _stat_files(self, model: LockedModel) -> dict[str, os.stat_result]:
        directory = self._root / model.directory
        stats = {}
        for locked in model.files:
            path = directory / locked.name
            try:
                found = path.stat()
            except FileNotFoundError:
                found = None
            except OSError as error:
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model, in "
                    f"{directory}, cannot be read ({error.strerror}). An "
                    f"antivirus may be holding it. {self._remedy}"
                ) from None
            if found is None or not stat.S_ISREG(found.st_mode):
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model is missing "
                    f"from {directory}. An antivirus may have quarantined it. "
                    f"{self._remedy}"
                )
            if found.st_size != locked.size:
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model has "
                    f"{found.st_size} bytes, and this version expects "
                    f"{locked.size}: the copy in {directory} is incomplete. "
                    f"{self._remedy}"
                )
            stats[locked.name] = found
        return stats

    def _verify_contents(
        self, model: LockedModel, stats: Mapping[str, os.stat_result]
    ) -> None:
        directory = self._root / model.directory
        vouched = self._read_record()
        pending = []
        for locked in model.files:
            path = str(directory / locked.name)
            identity = _identity(stats[locked.name], locked.sha256)
            if vouched.get(path) == identity:
                continue
            refusal = self._refused.get((path, json.dumps(identity, sort_keys=True)))
            if refusal is not None:
                raise ModelUnavailableError(refusal)
            pending.append((locked, path, identity))
        if not pending:
            return
        for locked, path, identity in pending:
            try:
                with open(path, "rb") as handle:
                    found = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError as error:
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model, in "
                    f"{directory}, cannot be read ({error.strerror}). An "
                    f"antivirus may be holding it. {self._remedy}"
                ) from None
            if found != locked.sha256:
                refusal = (
                    f"{locked.name} of the {model.repository} model is not the "
                    f"file this version expects (sha256 {found[:12]}..., expected "
                    f"{locked.sha256[:12]}...): its content was altered or "
                    f"corrupted. {self._remedy}"
                )
                self._refused[(path, json.dumps(identity, sort_keys=True))] = refusal
                raise ModelUnavailableError(refusal)
            vouched[path] = identity
        logger.info(
            "Checked the sha256 of %d file(s) of %s against the lockfile",
            len(pending),
            model.repository,
        )
        self._write_record(vouched)

    def _read_record(self) -> dict[str, Any]:
        """Os arquivos já conferidos contra este lockfile, por caminho absoluto.

        Um registro que não pode ser lido, ou que foi gravado para outro
        lockfile, não garante nada: o custo é um hash a mais.
        """
        if self._record_file is None:
            return {}
        try:
            raw = json.loads(self._record_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning(
                "The verification record %s could not be read (%s); the models "
                "are hashed again",
                self._record_file,
                error,
            )
            return {}
        if (
            not isinstance(raw, dict)
            or raw.get("format") != RECORD_FORMAT
            or raw.get("lockfile_sha256") != self._lockfile.sha256
            or not isinstance(raw.get("files"), dict)
        ):
            return {}
        files: dict[str, Any] = raw["files"]
        return files

    def _write_record(self, files: Mapping[str, Any]) -> None:
        """Substitui o registro num único rename, para nenhum leitor ver metade dele.

        Dois processos gravando ao mesmo tempo perdem uma das duas
        atualizações, o que custa aos arquivos daquele processo um hash a
        mais, e nada além disso. Um registro que não pode ser gravado vai para
        o log e é ignorado: os modelos estão certos, e a próxima carga calcula
        o hash deles de novo.
        """
        if self._record_file is None:
            return
        record = {
            "format": RECORD_FORMAT,
            "lockfile_sha256": self._lockfile.sha256,
            "files": dict(sorted(files.items())),
        }
        temporary = self._record_file.with_name(
            f"{self._record_file.name}.{os.getpid()}.tmp"
        )
        try:
            self._record_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self._record_file)
        except OSError as error:
            logger.warning(
                "The verification record %s could not be written (%s); the next "
                "load hashes the models again",
                self._record_file,
                error,
            )


def _identity(found: os.stat_result, sha256: str) -> dict[str, Any]:
    """O que não pode ter mudado para um hash anterior ainda garantir um arquivo.

    O tamanho, a data de modificação e o file id -- no Windows, `st_ino` é o
    file id do NTFS, e `st_dev` o número de série do volume -- e o sha256 com
    que o arquivo foi conferido. Um arquivo substituído por uma cópia tem um
    id novo; um arquivo reescrito no lugar tem uma data de modificação nova.
    """
    return {
        "size": found.st_size,
        "mtime_ns": found.st_mtime_ns,
        "file_id": f"{found.st_dev}:{found.st_ino}",
        "sha256": sha256,
    }
