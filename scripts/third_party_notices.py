"""Write THIRD-PARTY-NOTICES.md: what the installer redistributes, and its license.

    python scripts/third_party_notices.py           # write the file at the root
    python scripts/third_party_notices.py --check   # exit 1 if the file is stale

SolidVision's own code is CC0, and the installer carries a great deal that is
not: the Python runtime and 57 packages, PostgreSQL with pgvector and the
libraries it links, two models, the JavaScript libraries the interface is
built from, and Electron. Each one's license is read from what was
materialized, never typed by hand:

- the Python packages: `runtime/python`, the `METADATA` and the license files
  of each `.dist-info`, which go into the installer with the package;
- PostgreSQL: `runtime/postgres/conda-meta` for the license each package
  declares, and `runtime/postgres/licenses`, the texts `vendor_postgres.py`
  keeps from the packages;
- the models: `models.lock.json` (RFC-035);
- the interface: `frontend/package-lock.json`, the packages that are not for
  development, and their license files in `node_modules`, whose texts go in
  the appendix because the build bundles the code and drops the files;
- Electron, whose `LICENSE.electron.txt` and `LICENSES.chromium.html`
  electron-builder places beside `SolidVision.exe`;
- the map's outline: the `source` member of `brazil-states.json`.

The output has no date and is sorted, so that the same trees give the same
file, byte for byte: `--check` is what `npm run dist` runs before staging, and
what the test suite runs when the runtimes are there.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUTPUT_NAME = "THIRD-PARTY-NOTICES.md"

PYTHON = Path("runtime", "python")
SITE_PACKAGES = PYTHON / "Lib" / "site-packages"
PYTHON_MANIFEST = PYTHON / "SOLIDVISION-PYTHON.json"
POSTGRES = Path("runtime", "postgres")
POSTGRES_LICENSES = POSTGRES / "licenses"
MODELS_LOCK = Path("backend", "app", "infrastructure", "ai", "models.lock.json")
FRONTEND = Path("frontend")
MAP_OUTLINE = FRONTEND / "src" / "map" / "brazil-states.json"

LICENSE_FILE_HINTS = ("licen", "copying", "notice", "copyright", "authors")
"""Names a license text goes by, matched in lower case."""

APACHE_MARKERS = ("Apache License", "Version 2.0, January 2004", "END OF TERMS")
MIT_MARKER = "Permission is hereby granted, free of charge"


class NoticesError(Exception):
    """Something the notices are read from is missing. The message says what."""


@dataclass(frozen=True)
class Component:
    """One redistributed thing: what it is, its license, and its texts."""

    name: str
    version: str
    license: str
    texts: tuple[str, ...]
    """Where its license texts are, relative to the folder named by the section."""
    source: str = ""


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def is_license_file(path: Path) -> bool:
    lowered = path.name.lower()
    return "licenses" in path.parts or any(
        hint in lowered for hint in LICENSE_FILE_HINTS
    )


def declared_license(metadata: dict[str, str], classifiers: Sequence[str]) -> str:
    """The license a wheel declares: its SPDX expression, a short `License`
    field, or its classifiers, in that order of trust."""
    expression = metadata.get("License-Expression", "").strip()
    if expression:
        return expression
    field = metadata.get("License", "").strip()
    if field and "\n" not in field and len(field) <= 40:
        return field
    if classifiers:
        return "; ".join(classifiers)
    return "não declarada"


def python_packages(root: Path) -> list[Component]:
    site = root / SITE_PACKAGES
    if not site.is_dir():
        raise NoticesError(
            f"{site} does not exist: materialize it with "
            "`python scripts/vendor_python.py`."
        )
    packages = []
    for dist_info in sorted(site.glob("*.dist-info"), key=lambda p: p.name.lower()):
        text = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
        message = Parser().parsestr(text, headersonly=True)
        metadata = {key: str(value) for key, value in message.items()}
        classifiers = [
            str(value).split("::")[-1].strip()
            for value in message.get_all("Classifier") or []
            if str(value).startswith("License ::")
        ]
        texts = tuple(
            sorted(
                path.relative_to(dist_info).as_posix()
                for path in dist_info.rglob("*")
                if path.is_file() and is_license_file(path.relative_to(dist_info))
            )
        )
        packages.append(
            Component(
                name=metadata.get("Name", dist_info.name),
                version=metadata.get("Version", "?"),
                license=declared_license(metadata, classifiers),
                texts=tuple(f"{dist_info.name}/{name}" for name in texts),
            )
        )
    return packages


def postgres_packages(root: Path) -> list[Component]:
    meta = root / POSTGRES / "conda-meta"
    licenses = root / POSTGRES_LICENSES
    if not meta.is_dir():
        raise NoticesError(
            f"{meta} does not exist: materialize the runtime with "
            "`python scripts/vendor_postgres.py`."
        )
    if not licenses.is_dir():
        raise NoticesError(
            f"{licenses} does not exist: the runtime predates the license texts. "
            "Materialize it again with `python scripts/vendor_postgres.py`."
        )
    packages = []
    for record_path in sorted(meta.glob("*.json")):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        stem = f"{record['name']}-{record['version']}-{record['build']}"
        folder = licenses / stem
        texts = tuple(
            sorted(
                f"{stem}/{path.relative_to(folder).as_posix()}"
                for path in folder.rglob("*")
                if path.is_file()
            )
        )
        packages.append(
            Component(
                name=str(record["name"]),
                version=str(record["version"]),
                license=str(record.get("license") or "não declarada"),
                texts=texts,
                source=f"https://github.com/conda-forge/{record['name']}-feedstock",
            )
        )
    return packages


MODEL_LICENSES = {"mit": "MIT", "apache-2.0": "Apache-2.0"}
"""The Hub's license tags, spelled as SPDX spells them."""


def models(root: Path) -> list[Component]:
    lock = json.loads((root / MODELS_LOCK).read_text(encoding="utf-8"))
    found = []
    for role in sorted(lock):
        entry = lock[role]
        tag = str(entry.get("license", "não declarada"))
        found.append(
            Component(
                name=str(entry["repository"]),
                version=str(entry["revision"])[:8],
                license=MODEL_LICENSES.get(tag.lower(), tag),
                texts=(),
                source=f"https://huggingface.co/{entry['repository']}",
            )
        )
    return found


def frontend_packages(root: Path) -> list[tuple[Component, str]]:
    """The interface's runtime packages, with the text of each license."""
    lock_path = root / FRONTEND / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    found = []
    for path, entry in sorted(lock["packages"].items()):
        if not path or entry.get("dev") or entry.get("devOptional"):
            continue
        name = path.split("node_modules/")[-1]
        folder = root / FRONTEND / path
        files = (
            sorted(
                candidate
                for candidate in folder.iterdir()
                if candidate.is_file() and is_license_file(Path(candidate.name))
            )
            if folder.is_dir()
            else []
        )
        if not files:
            raise NoticesError(
                f"{name}: no license file in {folder}. Run `npm ci` in frontend/."
            )
        manifest = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        repository = manifest.get("repository")
        if isinstance(repository, dict):
            repository = repository.get("url", "")
        text = "\n\n".join(
            file.read_text(encoding="utf-8", errors="replace").strip() for file in files
        )
        found.append(
            (
                Component(
                    name=name,
                    version=str(entry.get("version", "?")),
                    license=str(entry.get("license", manifest.get("license", "?"))),
                    texts=tuple(file.name for file in files),
                    source=str(repository or manifest.get("homepage", "")),
                ),
                text,
            )
        )
    return found


def electron_version(root: Path) -> str:
    manifest = root / FRONTEND / "node_modules" / "electron" / "package.json"
    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])


def map_outline_source(root: Path) -> dict[str, str]:
    data = json.loads((root / MAP_OUTLINE).read_text(encoding="utf-8"))
    source = data.get("source", {})
    return {key: str(value) for key, value in source.items()}


def standard_text(root: Path, markers: Iterable[str], start: str | None = None) -> str:
    """A license's standard text, taken from a package that ships it whole.

    For the components that declare a license and carry no text: two wheels
    without license files, and the models. The first `.dist-info` file, in
    name order, that holds every marker; from `start` on, when given, which
    leaves out a copyright line that belongs to that package.
    """
    wanted = tuple(markers)
    for path in sorted((root / SITE_PACKAGES).glob("*.dist-info/**/*")):
        if not path.is_file() or not is_license_file(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if all(marker in text for marker in wanted):
            if start is not None:
                text = text[text.index(start) :]
            return text.strip()
    raise NoticesError(f"no license file in {SITE_PACKAGES} holds {wanted}")


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def texts_cell(component: Component, missing: str) -> str:
    if not component.texts:
        return missing
    return "<br>".join(f"`{cell(name)}`" for name in component.texts[:4]) + (
        f"<br>e mais {len(component.texts) - 4}" if len(component.texts) > 4 else ""
    )


def fenced(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"


def render(root: Path) -> str:
    python_manifest = json.loads((root / PYTHON_MANIFEST).read_text(encoding="utf-8"))
    python = python_packages(root)
    postgres = postgres_packages(root)
    ai = models(root)
    interface = frontend_packages(root)
    electron = electron_version(root)
    outline = map_outline_source(root)
    apache = standard_text(root, APACHE_MARKERS)
    mit = standard_text(root, (MIT_MARKER,), start=MIT_MARKER)
    vc = python_manifest.get("vc_runtime", {}).get("package", {})

    lines = [
        "# Avisos de terceiros",
        "",
        "> Gerado por `python scripts/third_party_notices.py` a partir do que foi "
        "materializado (`runtime/`, `frontend/node_modules`, os lockfiles). Não edite "
        "à mão: rode o script de novo quando um lockfile mudar. O `npm run dist` "
        "confere que este arquivo é o que o script gera, antes de montar o instalador.",
        "",
        "O código e a documentação do SolidVision estão sob a CC0 1.0 (`LICENSE`). "
        "O instalador redistribui, junto com eles, os componentes abaixo, cada um "
        "sob a sua própria licença, que continua valendo. Os textos dessas licenças "
        "vão no programa instalado, nos lugares que cada seção diz; os que o "
        "programa não teria de outro jeito estão nos apêndices deste arquivo, que "
        "também vai no instalador (`resources\\THIRD-PARTY-NOTICES.md`).",
        "",
        "As imagens do dataset de demonstração (`backend/dataset/demo`) não vão no "
        "instalador e não são CC0: as licenças de cada uma estão no `manifest.json` "
        "de lá.",
        "",
        "## Resumo",
        "",
        "| Grupo | Componentes | Onde estão os textos, no programa instalado |",
        "|---|---|---|",
        f"| Python {python_manifest['python']} e os pacotes do app | {len(python) + 1} "
        "| `resources\\runtime\\python\\LICENSE.txt` e a pasta `.dist-info` de cada "
        "pacote, em `resources\\runtime\\python\\Lib\\site-packages` |",
        f"| PostgreSQL, pgvector e as bibliotecas que eles usam | {len(postgres)} "
        "| `resources\\runtime\\postgres\\licenses` |",
        f"| Modelos de IA | {len(ai)} | Os cartões dos modelos, nos endereços "
        "da tabela; as licenças padrão no Apêndice B |",
        f"| Bibliotecas da interface | {len(interface)} | Apêndice A |",
        f"| Electron {electron} e Chromium | 1 | `LICENSE.electron.txt` e "
        "`LICENSES.chromium.html`, na pasta do programa |",
        "| Dados de mapa e serviços online | 3 | Esta página, seção "
        "*Dados e serviços* |",
        "",
        "## Python",
        "",
        f"O Python {python_manifest['python']} *embeddable* do python.org, sob a "
        "PSF License (`resources\\runtime\\python\\LICENSE.txt`), com os DLLs do "
        "Visual C++ que ele e os pacotes carregam: "
        f"`{vc.get('name', 'vc14_runtime')}` {vc.get('version', '')}, "
        f"redistribuídos pela licença da Microsoft (`{vc.get('license', '')}`), cujo "
        "texto está com o mesmo pacote na seção do PostgreSQL. Os pacotes, na "
        "versão exata do `scripts/python-win-64.lock`; um caminho de licença longo "
        "demais vira `licenses.zip` no instalador (RFC-041 §2.5).",
        "",
        "| Pacote | Versão | Licença declarada | Textos, na pasta do pacote |",
        "|---|---|---|---|",
    ]
    no_text = "nenhum no pacote: o texto da licença está no Apêndice B"
    for package in python:
        lines.append(
            f"| {cell(package.name)} | {cell(package.version)} | "
            f"{cell(package.license)} | {texts_cell(package, no_text)} |"
        )
    lines += [
        "",
        "Dois pacotes têm licença LGPL-3.0, `psycopg` e `psycopg-binary`: são "
        "carregados como módulos à parte, e quem quiser pode trocá-los na pasta "
        "`site-packages`; o código-fonte está em https://github.com/psycopg/psycopg.",
        "",
        "## PostgreSQL embarcado",
        "",
        "Os pacotes do conda-forge do `scripts/postgres-win-64.lock`, com a licença "
        "que cada um declara. Os textos vêm dos próprios pacotes e ficam em "
        "`resources\\runtime\\postgres\\licenses\\<pacote>`. O código-fonte de cada "
        "um está na receita do conda-forge (a coluna *Origem*); a `libiconv`, sob a "
        "LGPL, é carregada como DLL à parte.",
        "",
        "| Pacote | Versão | Licença declarada | Textos, em `licenses` | Origem |",
        "|---|---|---|---|---|",
    ]
    metapackage = "nenhum: um metapacote, que não instala arquivos"
    for package in postgres:
        lines.append(
            f"| {cell(package.name)} | {cell(package.version)} | "
            f"{cell(package.license)} | {texts_cell(package, metapackage)} | "
            f"{package.source} |"
        )
    lines += [
        "",
        "## Modelos de IA",
        "",
        "Os pesos, na revisão do `models.lock.json`, conferidos por `sha256` "
        "(RFC-035). Os repositórios dos pesos não trazem um arquivo de licença: a "
        "licença é a "
        "declarada no cartão de cada modelo, no endereço da tabela, e o texto padrão "
        "dela está no Apêndice B.",
        "",
        "| Modelo | Revisão | Licença declarada | Origem |",
        "|---|---|---|---|",
    ]
    for model in ai:
        lines.append(
            f"| {cell(model.name)} | {model.version} | {cell(model.license)} | "
            f"{model.source} |"
        )
    lines += [
        "",
        "## Interface",
        "",
        "As bibliotecas que o build da interface empacota (`frontend/dist`), as do "
        "`package-lock.json` que não são de desenvolvimento. O build junta o código "
        "e deixa os arquivos de licença para trás; por isso os textos estão aqui, no "
        "Apêndice A.",
        "",
        "| Pacote | Versão | Licença | Origem |",
        "|---|---|---|---|",
    ]
    for package, _ in interface:
        lines.append(
            f"| {cell(package.name)} | {cell(package.version)} | "
            f"{cell(package.license)} | {cell(package.source)} |"
        )
    lines += [
        "",
        "## Electron",
        "",
        f"A janela é o Electron {electron} (MIT), com o Chromium e as bibliotecas "
        "dele. O electron-builder põe os dois textos ao lado do `SolidVision.exe`: "
        "`LICENSE.electron.txt` e `LICENSES.chromium.html`.",
        "",
        "## Dados e serviços",
        "",
        f"- **O contorno dos estados**, o fundo do mapa sem rede: "
        f"{outline.get('dataset', 'Natural Earth')}, versão "
        f"{outline.get('version', '?')}, de {outline.get('url', '?')}. "
        f"{outline.get('license', 'Domínio público.')} Filtrado e simplificado "
        "por `frontend/scripts/brazil-states.mjs`.",
        "- **Os tiles do mapa**, com rede: do OpenStreetMap, carregados na hora e "
        "não distribuídos. © colaboradores do OpenStreetMap, sob a ODbL; a "
        "atribuição fica na tela (RFC-040).",
        "- **A busca por endereço**, com rede e desligável (`ALLOW_ADDRESS_SEARCH`): "
        "o Nominatim do OpenStreetMap, pela política de uso dele (RFC-043). Dados © "
        "colaboradores do OpenStreetMap, sob a ODbL.",
        "",
        "## Apêndice A — licenças das bibliotecas da interface",
        "",
    ]
    for package, text in interface:
        lines += [
            f"### {package.name} {package.version} ({package.license})",
            "",
            fenced(text),
            "",
        ]
    lines += [
        "## Apêndice B — textos padrão",
        "",
        "Para os componentes que declaram uma licença e não trazem o texto dela: "
        "os pesos dos modelos e os pacotes Python marcados acima.",
        "",
        "### Apache License 2.0",
        "",
        fenced(apache),
        "",
        "### MIT License",
        "",
        "Sem uma linha de copyright: a dos pesos do CLIP é a do cartão do modelo.",
        "",
        fenced(mit),
        "",
    ]
    return "\n".join(lines)


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n")


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"exit 1 if {OUTPUT_NAME} is not what the trees give",
    )
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root: Path = args.root
    output = root / OUTPUT_NAME
    try:
        content = render(root)
    except (NoticesError, OSError, KeyError, ValueError) as error:
        print(f"FAILED: {error}")
        return 1
    if args.check:
        current = output.read_text(encoding="utf-8") if output.is_file() else ""
        if normalized(current) != normalized(content):
            print(
                f"{OUTPUT_NAME} is stale: run `python scripts/third_party_notices.py` "
                "and commit the result."
            )
            return 1
        print(f"{OUTPUT_NAME}: current")
        return 0
    output.write_text(content, encoding="utf-8", newline="\n")
    print(f"{OUTPUT_NAME}: written, {len(content.encode('utf-8')) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
