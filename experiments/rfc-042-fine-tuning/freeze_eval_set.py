"""Freeze the oblique evaluation set of RFC-042 section 5.1.2 into `eval_set.jsonl`.

One line per evaluation photo: its query in Portuguese (and the southern
variant, when there is one), the English version, the scene it shows, and
whether it lands in the validation or the test split.

    python freeze_eval_set.py --out C:\\Users\\me\\rfc042\\commons

Inputs, all next to this script except the collection:

- `captions.jsonl`: the queries as the user saved them in the review page
  (the last line per photo counts);
- `query_en.jsonl`: the English version of each query, keyed by `pageid`,
  written by Claude from the Portuguese and checked by sample. A query
  edited after it was translated makes this script stop, so the two never
  drift apart silently;
- `OUT/collection.sqlite`: where each photo is on disk, and its series.

**The split.** Section 4.6: validation and test are frozen before anything is
trained, and the test split is opened only for the final evaluation. A third
of the queries go to validation, the rest to test. The draw is the
collector's `spread(pageid)` order inside each stratum (scene, and whether a
southern variant exists), so each split gets its share of every scene and
of the regional queries, and every run draws the same split. Both splits
share one gallery, the 525 evaluation photos, so a query in either split
competes against the same images.

**The scene.** A rough label for reading results apart, not ground truth:
the first word of the query that names a scene decides it (`água`,
`construído`, `campo`; `outro` when none does), because the query names its
main subject first. `water` is set when any water word appears at all; it is
there for the question of whether the many water photos tilt the result.

The file is refused if it already exists: a frozen set is replaced on
purpose, with `--force`, never by a rerun.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
VALIDATION_SHARE = 3  # every third query of a stratum goes to validation

WATER = frozenset(
    """
    rio rios riacho riachos córrego arroio sanga lago lagos lagoa lagoas laguinho
    represa açude barragem mar praia praias baía costa orla ilha ilhas ilhota
    península cachoeira cataratas canal porto cais píer marina navio navios barco
    barcos veleiro balsa caiaques surfista onda ondas recife recifes farol
    quebra-mar quebra-mares alagado alagada alagando enchente água estuário
    pântano poço caverna
    """.split()
)
BUILT = frozenset(
    """
    cidade cidades bairro bairros prédio prédios rua avenida praça estacionamento
    estádio igreja capela templo mesquita castelo fábrica usina galpão galpões
    escola hospital supermercado loja lojas ponte pontes viaduto rodovia trevo
    estação trem ferrovia trilhos casa casas casinhas casarão mansão vila
    cidadezinha favela loteamento hotel teatro monumento estátua torre multidão
    pessoas pessoa obra construção guindaste guindastes ruína ruínas forte
    fortaleza sítio arqueológico telhado telhados chalés bangalôs moinho
    acampamento camping festa fachada viaturas carro caminhão trator
    conjunto pista centro polo terreno trailers aqueduto antenas pátio muro
    varais tanque
    """.split()
)
COUNTRY = frozenset(
    """
    campo campos plantação plantações fazenda floresta mata mato bosque pinheiros
    morro morros morrinhos colina colinas montanha montanhas serra vale deserto
    planície cânion trilha trilhas estrada pasto pastos pedreira mina lava lama
    árvore árvores vegetação rocha rochedo pedras círculo círculos pomares
    estufas turbinas mirante cerrado jardim parque gramado buraco faixa dunas
    """.split()
)
SCENES = (("água", WATER), ("construído", BUILT), ("campo", COUNTRY))


def load_collector() -> ModuleType:
    """`collect_commons.py`, for `spread` -- the order the whole experiment uses."""
    spec = importlib.util.spec_from_file_location(
        "collect_commons", HERE / "collect_commons.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("collect_commons.py not found next to this script")
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_commons"] = module
    spec.loader.exec_module(module)
    return module


def words(text: str) -> list[str]:
    return re.findall(r"[\wà-ÿ-]+", text.casefold())


def scene_of(text: str) -> str:
    for word in words(text):
        for name, vocabulary in SCENES:
            if word in vocabulary:
                return name
    return "outro"


def has_water(text: str) -> bool:
    return any(word in WATER for word in words(text))


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def latest_queries(ledger: Path) -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for record in read_jsonl(ledger):
        if record.get("kind") == "query":
            latest[int(record["pageid"])] = record
    return {pageid: r for pageid, r in latest.items() if not r.get("deleted")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="the collector's folder"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing frozen set"
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    target = HERE / "eval_set.jsonl"
    if target.exists() and not args.force:
        raise SystemExit(
            f"{target.name} exists and is frozen; pass --force to replace it"
        )

    collector = load_collector()
    queries = latest_queries(HERE / "captions.jsonl")
    english = {int(r["pageid"]): r for r in read_jsonl(HERE / "query_en.jsonl")}
    stale = [
        p
        for p, q in queries.items()
        if p in english and english[p]["pt_when_translated"] != q["pt"]
    ]
    missing = sorted(set(queries) - set(english))
    if stale or missing:
        raise SystemExit(
            f"English out of date for {stale[:5]} ({len(stale)}),"
            f" missing for {missing[:5]} ({len(missing)})"
        )

    db = sqlite3.connect(
        f"file:{(args.out / 'collection.sqlite').as_posix()}?mode=ro", uri=True
    )
    where = {
        int(pageid): (str(path), str(decision), downloaded is not None)
        for pageid, path, decision, downloaded in db.execute(
            "SELECT pageid, path, decision, downloaded_at FROM files"
        )
    }
    unusable = [
        p
        for p in queries
        if p not in where or where[p][1] != "accepted" or not where[p][2]
    ]
    if unusable:
        raise SystemExit(
            f"photos not downloaded or not accepted: {unusable[:5]} ({len(unusable)})"
        )

    rows = []
    for pageid, query in queries.items():
        south = query.get("pt_south", "")
        rows.append(
            {
                "pageid": pageid,
                "path": where[pageid][0],
                "series": query["series"],
                "scene": scene_of(query["pt"]),
                "water": has_water(query["pt"]),
                "pt": query["pt"],
                **({"pt_south": south} if south else {}),
                "en": english[pageid]["en"],
            }
        )

    strata: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for row in rows:
        strata.setdefault((row["scene"], "pt_south" in row), []).append(row)
    for members in strata.values():
        members.sort(key=lambda row: collector.spread(row["pageid"]))
        for index, row in enumerate(members):
            row["split"] = "validation" if index % VALIDATION_SHARE == 0 else "test"

    rows.sort(key=lambda row: collector.spread(row["pageid"]))
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            ordered = {"pageid": row["pageid"], "split": row.pop("split"), **row}
            handle.write(json.dumps(ordered, ensure_ascii=False) + "\n")

    table = Counter((r["split"], r["scene"]) for r in read_jsonl(target))
    for split in ("validation", "test"):
        line = ", ".join(
            f"{scene} {table[(split, scene)]}"
            for scene in ("água", "construído", "campo", "outro")
        )
        size = sum(n for (s, _), n in table.items() if s == split)
        south = sum(
            1 for r in read_jsonl(target) if r["split"] == split and "pt_south" in r
        )
        water = sum(1 for r in read_jsonl(target) if r["split"] == split and r["water"])
        print(f"{split}: {size} queries ({line}); with water {water};", end="")
        print(f" southern variant {south}")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
