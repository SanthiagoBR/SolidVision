"""The second round of RFC-047's probe: the lexicon held to sentences (section 17).

`probe_lexicon.py` proved each `troca` alone and in "{forma} vista de cima",
and the validation showed what that misses: "estrada de terra" sent as
"estrada de terra batida" is *dirt road hit near the river*. This runs every
form of the lexicon through the product's own path -- `lexicon.rewrite()`,
then Marian -- in the contexts of `tests/infrastructure/ai/lexicon_frames.py`
(alone, "vista de cima", five sentences), and judges the answers by the rules
there: one of the `en`, and no word that the frame, the `en` and a short list
of function words do not explain.

    .venv\\Scripts\\python.exe ^
        experiments\\rfc-047-traducao-das-consultas\\probe_frames.py

    ... probe_frames.py --candidates more.json   (try trocas, write nothing)

The audit writes `probe_frames.json` next to this script. With `--candidates`,
a JSON file of {form: [candidate trocas]}, each is put in the lexicon in the
place of the form's own and tried in the same contexts, and in a typed
round trip (a `troca` typed by the user must not be rewritten again).

The contexts are not evaluation queries and the forms are the lexicon's own
(section 4.4); nothing here reads `eval_set.jsonl`.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path[:0] = [str(REPOSITORY / "backend")]

from tests.infrastructure.ai.lexicon_frames import (  # noqa: E402
    CONTEXTS,
    KNOWN_MISSES,
    OTHER_SENSES,
    judge,
)

from app.infrastructure.ai.device import resolve_device  # noqa: E402
from app.infrastructure.ai.model_runtime import (  # noqa: E402
    TRANSLATOR,
    default_runtime,
    read_lockfile,
)
from app.infrastructure.ai.query_translator import (  # noqa: E402
    LexiconForm,
    MarianQueryTranslator,
    QueryLexicon,
)

ROUND_2_DROPPED: dict[str, str] = {
    "calçadão": "'calçada ampla' e 'calçada larga' viram *wide-story view* e"
    " *wide-scale photo* em frase; nenhuma outra candidata acerta",
    "galpões": "o Marian dá *ramparts* em 'vista de cima'; 'armazéns' perde o"
    " substantivo ali, 'sheds' vira *sheets* sozinha, 'galpões industriais' acrescenta",
    "conjunto habitacional": "'complexo habitacional' vira *complex* sem *housing*"
    " em frase; as outras candidatas viram *housing enterprise* ou *set of houses*",
    "fonte": "fonte d'água, nascente, chafariz e tipo de letra: 'fonte de água'"
    " é *water source* e 'fonte de praça' acrescenta a praça",
    "bicicleta": "o Marian some com o substantivo em 'vista de cima de bicicleta'; a"
    " única troca que acerta, 'bike', muda o prompt já certo (*bicycle*) das"
    " consultas do RFC-035",
    "estrada de terra": "o Marian dá *dirt road* em 4 dos 7 contextos; a troca"
    " 'estrada de cascalho' dá *gravel road* nos 7, outro sentido",
    "estradas de terra": "como 'estrada de terra'",
    "estrada de terra batida": "como 'estrada de terra'; sem a troca, 'batida'"
    " segue virando *hit* em frase, como antes do RFC-047",
    "estradas de terra batida": "como 'estrada de terra batida'",
}
"""Forms of the lexicon that round 2 took out, and why (section 17)."""


class Translations:
    """Marian, asked once per distinct text -- and, with a cache file, once
    per text for all runs: greedy decoding gives the same English for the same
    text, so a rerun after editing the lexicon pays only for the new texts.
    The file is tied to the lockfile of the checkpoint, and is not versioned."""

    def __init__(self, translator: MarianQueryTranslator, file: Path | None) -> None:
        self._translator = translator
        self._file = file
        self._key = read_lockfile().sha256
        self.cache: dict[str, str] = {}
        if file is not None and file.exists():
            stored = json.loads(file.read_text(encoding="utf-8"))
            if stored.get("lockfile") == self._key:
                self.cache = stored["translations"]

    def __call__(self, text: str) -> str:
        if text not in self.cache:
            self.cache[text] = self._translator._translate(text)
        return self.cache[text]

    def save(self) -> None:
        if self._file is not None:
            document = {"lockfile": self._key, "translations": self.cache}
            self._file.write_text(
                json.dumps(document, ensure_ascii=False), encoding="utf-8"
            )


def trial(
    lexicon: QueryLexicon,
    form: LexiconForm,
    translate: Translations,
) -> dict[str, Any]:
    """`form` typed as `form.pt` in every context, through the lexicon."""
    result: dict[str, Any] = {}
    for name, template in CONTEXTS.items():
        typed = template.format(X=form.pt)
        sent = lexicon.rewrite(typed)
        english = translate(sent)
        right, found, left = judge(english, name, form.en)
        result[name] = {
            "typed": typed,
            "sent": sent,
            "out": english,
            "found": found,
            "extras": left,
            "ok": right,
        }
    senses = []
    for phrase, word in OTHER_SENSES.get(form.pt, ()):
        english = translate(lexicon.rewrite(phrase))
        senses.append(
            {"typed": phrase, "out": english, "ok": word in english.casefold()}
        )
    result["other_senses"] = senses
    result["typed_again"] = lexicon.rewrite(form.sent) == form.sent
    # As the slow test: right everywhere but the known misses, which stay wrong.
    misses = KNOWN_MISSES.get(form.pt, {})
    for name in misses:
        result[name]["known_miss"] = True
    result["passes"] = (
        all(result[name]["ok"] == (name not in misses) for name in CONTEXTS)
        and all(s["ok"] for s in senses)
        and result["typed_again"]
    )
    return result


def with_troca(
    lexicon: QueryLexicon, form: LexiconForm, troca: str | None
) -> tuple[QueryLexicon, LexiconForm]:
    """The lexicon with `troca` in place of the form's own, and the new form."""
    changed = dataclasses.replace(form, troca=troca)
    forms = [changed if other is form else other for other in lexicon.forms]
    return QueryLexicon(forms), changed


def show(label: str, result: dict[str, Any]) -> None:
    mark = "ok" if result["passes"] else "XX"
    print(f"  {mark} {label}")
    if result["passes"]:
        return
    for name in CONTEXTS:
        item = result[name]
        if not item["ok"]:
            why = f" extras {item['extras']}" if item["found"] else " no en"
            print(f"       {name:14} {item['sent']!r} -> {item['out']!r}{why}")
    for sense in result["other_senses"]:
        if not sense["ok"]:
            print(f"       sense          {sense['typed']!r} -> {sense['out']!r}")
    if not result["typed_again"]:
        print("       typed again    the troca is rewritten once more")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--forms", nargs="*", help="only these forms of the lexicon")
    parser.add_argument(
        "--lexicon",
        type=Path,
        help="audit this lexicon instead of the shipped one (writes"
        " probe_frames-<name>.json instead of probe_frames.json)",
    )
    parser.add_argument("--candidates", type=Path, help="{form: [trocas]} to try")
    parser.add_argument(
        "--cache", type=Path, help="a file of translations kept between runs"
    )
    parser.add_argument(
        "--untouched",
        action="store_true",
        help="for each form with a troca, in how many contexts Marian is right"
        " without it (a troca that mends 2 of 7 and changes the other 5 is not a gain)",
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    lexicon = QueryLexicon.load(args.lexicon)
    translator = MarianQueryTranslator(
        default_runtime().loader(TRANSLATOR), device=resolve_device()
    )
    translate = Translations(translator, args.cache)
    translate("aquecimento")  # load the checkpoint before timing

    started = time.perf_counter()
    if args.untouched:
        nothing = QueryLexicon([])
        rows = []
        for form in lexicon.forms:
            if form.troca is None or form.is_name:
                continue
            result = trial(nothing, form, translate)
            right = [name for name in CONTEXTS if result[name]["ok"]]
            rows.append(
                {
                    "pt": form.pt,
                    "troca": form.troca,
                    "right_without": len(right),
                    "contexts": len(CONTEXTS),
                    "wrong_in": [n for n in CONTEXTS if n not in right],
                    "outs": {n: result[n]["out"] for n in CONTEXTS},
                }
            )
        translate.save()
        rows.sort(key=lambda row: -row["right_without"])
        for row in rows:
            print(
                f"{row['right_without']}/{row['contexts']}  {row['pt']:24}"
                f" -> {row['troca']:26} wrong in {row['wrong_in']}"
            )
        (HERE / "probe_frames-untouched.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return
    if args.candidates:
        candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
        by_pt = {form.pt: form for form in lexicon.forms}
        report: dict[str, Any] = {}
        for pt, trocas in candidates.items():
            form = by_pt[pt]
            print(f"\n{pt}  (en: {', '.join(form.en)})")
            report[pt] = []
            for troca in trocas:
                variant, changed = with_troca(lexicon, form, troca)
                result = trial(variant, changed, translate)
                show(troca, result)
                report[pt].append({"troca": troca, **result})
        passing = {
            pt: [r["troca"] for r in rows if r["passes"]] for pt, rows in report.items()
        }
        print("\npassing:", json.dumps(passing, ensure_ascii=False))
        translate.save()
        (HERE / "probe_frames-candidates.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return

    forms = [f for f in lexicon.forms if not f.is_name]
    if args.forms:
        forms = [f for f in forms if f.pt in set(args.forms)]
    entries = []
    for form in forms:
        result = trial(lexicon, form, translate)
        entries.append(
            {
                "pt": form.pt,
                "troca": form.troca,
                "en": list(form.en),
                "origem": form.origem,
                **result,
            }
        )
        if not result["passes"]:
            print(f"\n{form.pt}  ->  {form.sent}  (en: {', '.join(form.en)})")
            show(form.sent, result)
    seconds = time.perf_counter() - started
    translate.save()

    failing = [e for e in entries if not e["passes"]]
    summary = {
        "forms": len(entries),
        "with_troca": sum(e["troca"] is not None for e in entries),
        "pass": len(entries) - len(failing),
        "fail": len(failing),
        "fail_with_troca": [e["pt"] for e in failing if e["troca"] is not None],
        "fail_without_troca": [e["pt"] for e in failing if e["troca"] is None],
        "fail_not_typed_again": [e["pt"] for e in failing if not e["typed_again"]],
        "translations": len(translate.cache),
        "seconds": round(seconds, 1),
    }
    print()
    for key, value in summary.items():
        print(f"{key}: {value}")
    if not args.forms:
        name = f"-{args.lexicon.stem.split('-')[-1]}" if args.lexicon else ""
        target = HERE / f"probe_frames{name}.json"
        target.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "dropped_in_round_2": ROUND_2_DROPPED,
                    "forms": entries,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
