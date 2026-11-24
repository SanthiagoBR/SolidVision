"""Tira os caminhos das fotos do acervo dos JSONs que vão para o git (repositório público).

    .venv-gpu\\Scripts\\python.exe reports\\2026-11-12-rtx4070\\tools\\anonymize.py

O original de cada arquivo fica ao lado, como `*.raw.json`, que o `.gitignore`
desta pasta deixa fora do git. A versão anonimizada fica no lugar dele.

- `search-*.json`: cada `relative_path` vira `foto-<sha256[:12]>.<ext>`. O hash
  é do caminho, então a mesma foto tem o mesmo nome em toda rodada, e o top-10
  continua comparável entre máquinas e versões sem dizer o que a foto é.
- `index-sample.json`: o nome de cada pasta vira `pasta N`.

Rodar de novo não faz nada: um arquivo que já tem `*.raw.json` ao lado é pulado.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from pathlib import Path

REPORT = Path(__file__).resolve().parent.parent


def _photo(relative_path: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    return f"foto-{digest}{PurePosixPath(relative_path).suffix.lower()}"


def _swap(path: Path) -> dict | None:
    raw = path.with_suffix(".raw.json")
    if raw.exists():
        print(f"skip {path.name}: {raw.name} already exists")
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    path.rename(raw)
    return data


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"anonymized {path.name}")


def main() -> None:
    for path in sorted(REPORT.glob("search-*.json")):
        if path.name.endswith(".raw.json"):
            continue
        data = _swap(path)
        if data is None:
            continue
        for query in data["queries"]:
            for hit in query["top"]:
                hit["relative_path"] = _photo(hit["relative_path"])
        data["anonymized"] = "relative_path = foto-<sha256(relative_path)[:12]>.<ext>"
        _write(path, data)

    path = REPORT / "index-sample.json"
    data = _swap(path) if path.exists() else None
    if data is not None:
        for number, job in enumerate(data["jobs"], start=1):
            job["root"] = f"pasta {number}"
        data["anonymized"] = "root = pasta N, na ordem em que foram indexadas"
        _write(path, data)


if __name__ == "__main__":
    main()
