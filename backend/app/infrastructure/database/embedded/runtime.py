"""Os binários vendorizados do PostgreSQL, e o ambiente em que rodam (RFC-033 §5).

O `scripts/vendor_postgres.py` materializa o runtime a partir de um lockfile e
escreve um manifesto ao lado. Este módulo é a outra metade: acha os binários
por caminho absoluto -- nunca pelo `PATH` --, confere que o manifesto e os
quatro arquivos de que o cluster precisa estão lá, e monta o ambiente que todo
processo filho do PostgreSQL recebe.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .code_page import argv_safe
from .errors import RuntimeInvalidError

MANIFEST_NAME = "SOLIDVISION-RUNTIME.json"
MANIFEST_FORMAT = 1

REQUIRED_FILES = (
    "Library/bin/postgres.exe",
    "Library/bin/initdb.exe",
    "Library/bin/pg_ctl.exe",
    "Library/lib/vector.dll",
)
"""Conferidos quanto à presença a cada subida; nunca têm hash (RFC-033 §5.2).

Calcular o hash de 123 MB de um HD a cada subida custaria mais que a própria
subida. A integridade dos arquivos é responsabilidade do instalador.
"""

MATERIALIZE_HINT = "Materialize it with `python scripts/vendor_postgres.py`."


@dataclass(frozen=True)
class RuntimeManifest:
    """O que o `vendor_postgres.py` escreveu: as versões que o lockfile fixa."""

    postgresql: str
    pgvector: str
    lockfile_sha256: str

    @property
    def major(self) -> int:
        """`16` para `16.15`: a parte que nomeia o diretório do cluster (§6)."""
        return int(self.postgresql.split(".", 1)[0])


class PostgresRuntime:
    """Um diretório de runtime materializado, recusado a menos que esteja completo."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise RuntimeInvalidError(
                f"The runtime directory must be absolute, not {root}: a relative "
                "one would move with the working directory."
            )
        self._root = root
        self._command_root: Path | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def command_root(self) -> Path:
        """A raiz como os próprios executáveis conseguem recebê-la (§10).

        O `initdb` acha o `postgres.exe` a partir do próprio caminho, que lê na
        code page ANSI; sob um diretório fora dela, não acha a si mesmo. Então
        os executáveis são iniciados pela forma curta 8.3 da raiz sempre que a
        longa não cabe -- o que faz do diretório que o instalador escolhe uma
        restrição só num volume sem nomes curtos.
        """
        if self._command_root is None:
            self._command_root = (
                argv_safe(self._root, setting="EMBEDDED_POSTGRES_RUNTIME_DIRECTORY")
                if self._root.exists()
                else self._root
            )
        return self._command_root

    @property
    def bin_directory(self) -> Path:
        return self.command_root / "Library" / "bin"

    @property
    def initdb(self) -> Path:
        return self.bin_directory / "initdb.exe"

    @property
    def pg_ctl(self) -> Path:
        return self.bin_directory / "pg_ctl.exe"

    @property
    def postgres(self) -> Path:
        return self.bin_directory / "postgres.exe"

    def manifest(self) -> RuntimeManifest:
        """Lê o manifesto e confere os quatro arquivos, ou recusa o diretório.

        Barato por desenho -- um JSON pequeno e quatro `stat`s --, porque roda
        a cada subida.
        """
        path = self._root / MANIFEST_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuntimeInvalidError(
                f"{self._root} has no {MANIFEST_NAME}, so it is not a runtime "
                f"this project materialized. {MATERIALIZE_HINT}"
            ) from None
        except (OSError, ValueError) as error:
            raise RuntimeInvalidError(
                f"{path} could not be read ({error}). {MATERIALIZE_HINT}"
            ) from error
        if not isinstance(raw, dict) or raw.get("format") != MANIFEST_FORMAT:
            raise RuntimeInvalidError(
                f"{path} is not a format-{MANIFEST_FORMAT} manifest. "
                f"{MATERIALIZE_HINT}"
            )
        try:
            manifest = RuntimeManifest(
                postgresql=str(raw["postgresql"]),
                pgvector=str(raw["pgvector"]),
                lockfile_sha256=str(raw["lockfile_sha256"]),
            )
            if manifest.major < 1:
                raise ValueError(f"postgresql version {manifest.postgresql!r}")
        except (KeyError, ValueError) as error:
            raise RuntimeInvalidError(
                f"{path} is incomplete ({error!r}). {MATERIALIZE_HINT}"
            ) from error
        missing = [name for name in REQUIRED_FILES if not (self._root / name).is_file()]
        if missing:
            raise RuntimeInvalidError(
                f"{self._root} is missing {', '.join(missing)}. {MATERIALIZE_HINT}"
            )
        return manifest

    def contains(self, path: Path) -> bool:
        """Se um executável mora dentro deste runtime.

        Comparado depois do `realpath`, que devolve um nome curto 8.3 ao nome
        longo e normaliza a caixa, de modo que o mesmo arquivo alcançado por
        duas grafias é um arquivo só. É metade da regra de adoção (§8.4) e
        metade da regra dos órfãos (§9.3).
        """
        root = os.path.normcase(os.path.realpath(self._root))
        candidate = os.path.normcase(os.path.realpath(path))
        return candidate.startswith(root.rstrip("\\/") + os.sep)

    def child_environment(
        self, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """O ambiente do `initdb`, do `pg_ctl` e do servidor (§5.3).

        O `PATH` é montado, não herdado: a raiz do runtime, `Library\\bin`,
        `System32` e o diretório do Windows, nessa ordem, e nada do `PATH` do
        usuário -- um PostgreSQL 15 lá não pode emprestar ao servidor uma
        `libssl`. Toda variável `PG*` sai: `PGDATA`, `PGPORT` e `PGOPTIONS` no
        ambiente do usuário mudariam qual cluster o `pg_ctl` controla, ou as
        opções do servidor. Todo o resto passa, porque o próprio Winsock
        precisa de `SystemRoot`.
        """
        source = dict(os.environ if base is None else base)
        system_root = next(
            (value for name, value in source.items() if name.upper() == "SYSTEMROOT"),
            r"C:\Windows",
        )
        environment = {
            name: value
            for name, value in source.items()
            if not name.upper().startswith("PG") and name.upper() != "PATH"
        }
        environment["PATH"] = os.pathsep.join(
            [
                str(self.command_root),
                str(self.bin_directory),
                str(Path(system_root) / "System32"),
                system_root,
            ]
        )
        return environment
