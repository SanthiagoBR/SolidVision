"""The probe of RFC-047 section 4.3: every lexicon form through Marian.

Each form of `backend/app/infrastructure/ai/query_lexicon.toml` is translated
by the product's own `MarianQueryTranslator`, in four contexts:

- alone ("morro");
- in the neutral sentence of section 4.3 ("morro vista de cima");
- followed by an adjective, in both genders ("morro amarelo", "morro
  amarela"; "amarelos"/"amarelas" after a plural), so that a `troca` of the
  other gender -- "casarão amarelo" sent as "casa grande amarelo" -- is
  tried in the agreement the user typed (section 5). The adjective's own
  English ("yellow") is taken out before comparing: *large yellow house* is
  *large house*, right.

A form **fails** when its translation alone or in the sentence contains none
of its `en`, as whole words. For a failing form, the candidates of
`CANDIDATES` are tried in the same four contexts; the `troca` the lexicon
records is one that passes alone and in the sentence -- the first, unless
`NOT_THE_FIRST` says why another. The probe reads the lexicon, never the
evaluation queries (section 4.4).

A place name (a form written with a capital) is checked in one sentence,
"vista aérea de {name}", and once more with the sentence rewritten by a
lexicon without it: the protection is needed when that second one breaks.

    .venv\\Scripts\\python.exe ^
        experiments\\rfc-047-traducao-das-consultas\\probe_lexicon.py
    ... probe_lexicon.py --forms morro estádio    (only these, no .json)
    ... probe_lexicon.py --forms morro --candidates more.json

Writes `probe_lexicon.json` next to this script when run on the whole lexicon;
its `.log` stays outside git.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path[:0] = [str(REPOSITORY / "backend")]

from app.infrastructure.ai.device import resolve_device  # noqa: E402
from app.infrastructure.ai.model_runtime import (  # noqa: E402
    TRANSLATOR,
    default_runtime,
)
from app.infrastructure.ai.query_translator import (  # noqa: E402
    LexiconForm,
    MarianQueryTranslator,
    QueryLexicon,
)

GATE = ("sozinha", "frase")
"""The contexts that decide whether a form fails, and whether a troca fixes it."""

ADJECTIVES = ("adjetivo m", "adjetivo f")
NAME_SENTENCE = "vista aérea de {}"
"""Marian keeps a place name inside a sentence and not alone: "Mato Grosso"
alone is *Fat Kill*, "vista aérea de Mato Grosso" is *aerial view of Mato
Grosso*."""

# Candidate replacements for the forms the first run found failing, in the
# order they were tried, over three rounds. Every one stays, so that the
# probe records why it was or was not chosen ("sítio" for "chácara" became
# *a place*, RFC-047 section 2.4).
CANDIDATES: dict[str, tuple[str, ...]] = {
    "açude": ("lago artificial", "pequena represa", "reservatório"),
    "represa": ("barragem", "reservatório", "lago de barragem"),
    "cascata": ("cachoeira",),
    "queda d'água": ("cachoeira",),
    "corredeira": ("águas agitadas", "águas brancas", "rápidos", "águas turbulentas"),
    "corredeiras": ("águas agitadas", "águas brancas", "rápidos", "águas turbulentas"),
    "barrento": ("lamacento", "marrom"),
    "barrenta": ("lamacenta", "marrom"),
    "enseada": ("baía pequena", "pequena baía", "angra"),
    "orla": ("beira-mar", "costa", "beira da praia", "beira d'água", "margem"),
    "ilhota": ("pequena ilha", "ilha pequena"),
    "foz": ("desembocadura", "estuário"),
    "mangue": ("manguezal",),
    "brejo": ("pântano", "alagado"),
    "quebra-mar": (
        "quebra mar",
        "barreira contra ondas",
        "molhe de pedras",
        "quebra-ondas",
        "dique marítimo",
    ),
    "marina": (
        "marina de barcos",
        "porto de barcos",
        "ancoradouro de barcos",
        "marina de iates",
        "porto de iates",
        "porto de lanchas",
    ),
    "farol": (
        "farol marítimo",
        "torre de farol",
        "torre do farol",
        "farol costeiro",
        "farol de navegação",
        "farol na costa",
        "farol de praia",
    ),
    "atracadouro": ("cais", "píer"),
    "jangada": ("balsa de madeira", "jangada de pesca"),
    "guarda-sol": ("sombrinha", "guarda-chuva de praia", "sombreiro"),
    "guarda-sóis": ("sombrinhas", "guarda-chuvas de praia", "sombreiros"),
    "calçadão": ("calçada larga", "passeio à beira-mar", "passeio"),
    "falésia": ("penhasco", "falésia costeira"),
    "falésias": ("penhascos",),
    "duna": ("duna de areia", "duna costeira", "morro de areia", "duna arenosa"),
    "sanga": ("córrego", "riacho"),
    "sangas": ("córregos", "riachos"),
    "banhado": ("pântano", "área alagada", "terreno alagado"),
    "banhados": ("pântanos", "áreas alagadas", "terrenos alagados"),
    "várzea": ("planície alagável", "terreno alagadiço", "baixada"),
    "trapiche": ("píer", "píer de madeira"),
    "trapiches": ("píeres", "píers"),
    "molhe": (
        "quebra-mar",
        "quebra mar",
        "barreira de pedras",
        "píer de pedras",
        "quebra-ondas",
        "píer de rochas",
    ),
    "molhes": (
        "quebra-mares",
        "quebra mares",
        "barreiras de pedras",
        "píeres de pedras",
        "quebra-ondas",
        "píeres de rochas",
    ),
    "costão": ("costa rochosa", "costão rochoso", "rochas da costa"),
    "cômoro": (
        "duna de areia",
        "duna",
        "duna costeira",
        "morro de areia",
        "duna arenosa",
    ),
    "cômoros": ("dunas", "dunas de areia"),
    "morro": ("colina", "monte"),
    "morros": ("colinas", "montes"),
    "serra": ("cadeia de montanhas", "cordilheira", "montanhas"),
    "serras": ("cadeias de montanhas", "cordilheiras", "montanhas"),
    "vale": ("vale entre montanhas", "vale do rio", "vale montanhoso", "vale fluvial"),
    "chapada": ("planalto",),
    "encostas": ("declives", "encostas de morro", "lados do morro"),
    "ladeira": ("rua íngreme", "subida", "rampa"),
    "pedreira": (
        "jazida de pedra",
        "mina de pedra",
        "extração de pedra",
        "cava de pedra",
        "pedreira de granito",
        "pedreira de brita",
    ),
    "geleira": ("glaciar",),
    "nascer do sol": ("amanhecer", "alvorada", "sol nascente"),
    "mata": ("floresta", "bosque"),
    "mato": ("vegetação", "arbustos", "matagal"),
    "coqueiro": ("palmeira de coco", "coqueiral", "árvore de coco"),
    "coqueiros": ("palmeiras de coco", "árvores de coco"),
    "araucárias": ("pinheiros araucária", "araucária", "pinheiros"),
    "cerros": ("colinas", "montes"),
    "coxilha": ("colina", "colinas suaves"),
    "coxilhas": ("colinas", "colinas suaves"),
    "capão": (
        "bosque",
        "capão de mato",
        "grupo de árvores",
        "pequeno bosque",
        "bosque pequeno",
    ),
    "capões": ("bosques", "grupos de árvores", "pequenos bosques"),
    "macega": ("capim alto", "vegetação rasteira", "mato alto"),
    "sítio": ("pequena fazenda", "chácara"),
    "roça": ("lavoura", "pequena lavoura", "plantação pequena"),
    "plantação": ("lavoura", "plantio", "cultivo"),
    "plantações": ("lavouras", "plantios", "cultivos"),
    "pasto": ("pastagem", "campo de pasto"),
    "pomar": ("pomar de frutas", "árvores frutíferas"),
    "horta": ("horta de legumes", "plantação de verduras", "canteiros de horta"),
    "vinhedo": ("vinhedos", "plantação de uva", "parreiras", "vinha"),
    "parreiral": ("vinhedo de uvas", "plantação de uva", "parreiras"),
    "canavial": ("plantação de cana", "plantação de cana-de-açúcar", "cana-de-açúcar"),
    "milharal": ("plantação de milho", "lavoura de milho", "campo de milho"),
    "bois": ("gado", "bois de gado"),
    "curral": (
        "cercado de gado",
        "curral de gado",
        "cercado",
        "cercado para gado",
        "curral para gado",
        "curral de madeira",
    ),
    "chiqueiro": (
        "pocilga",
        "criação de porcos",
        "cercado de porcos",
        "granja de porcos",
        "pocilga de porcos",
    ),
    "galinheiro": ("criação de galinhas", "casa de galinhas"),
    "aviário": ("granja de frangos", "criação de frangos", "galpão de frangos"),
    "estufas": ("estufas agrícolas", "casas de vegetação"),
    "porteira": ("portão de fazenda", "portão de madeira"),
    "cerca": ("cerca de arame", "cercado"),
    "cerca viva": (
        "sebe",
        "cerca de arbustos",
        "fileira de arbustos",
        "cerca verde",
        "sebe viva",
    ),
    "cercas vivas": ("sebes", "cercas de arbustos"),
    "cata-vento": ("moinho de vento",),
    "caixa d'água": ("reservatório de água", "tanque de água", "torre de água"),
    "estrada de terra": (
        "estrada não pavimentada",
        "estrada sem asfalto",
        "estrada de barro",
        "estrada de cascalho",
        "estrada de terra batida",
        "caminho de terra",
        "estrada rural",
    ),
    "estrada de chão": (
        "estrada não pavimentada",
        "estrada sem asfalto",
        "estrada de barro",
        "estrada de cascalho",
        "estrada de terra batida",
        "caminho de terra",
        "estrada rural",
    ),
    "chácara": ("pequena fazenda", "sítio"),
    "chácaras": ("pequenas fazendas", "sítios"),
    "estância": ("fazenda de gado", "fazenda"),
    "estâncias": ("fazendas de gado", "fazendas"),
    "potreiro": ("pastagem", "pasto cercado", "piquete de gado"),
    "potreiros": ("pastagens", "pastos cercados"),
    "piquete": (
        "pasto cercado",
        "pequeno pasto",
        "cercado",
        "pastagem pequena",
        "pequena pastagem",
        "pastagem",
    ),
    "invernada": ("pastagem", "pastagem de inverno", "pasto"),
    "taipa": (
        "dique",
        "dique de terra",
        "barreira de terra",
        "muro de terra",
        "pequeno dique",
        "dique de arrozal",
    ),
    "taipas": ("diques", "diques de terra", "barreiras de terra", "muros de terra"),
    "casarão": ("casa grande", "mansão"),
    "casarões": ("casas grandes", "mansões"),
    "sobrado": ("casa de dois andares", "casa de dois pavimentos"),
    "conjunto habitacional": ("complexo habitacional", "conjunto de casas populares"),
    "loteamento": (
        "lotes de terreno",
        "terrenos loteados",
        "terreno dividido em lotes",
    ),
    "quintal": ("pátio dos fundos", "jardim dos fundos", "pátio"),
    "estádio": ("estádio de futebol", "estádio esportivo", "arena esportiva"),
    "autódromo": ("pista de corrida", "circuito de corrida"),
    "chafariz": (
        "fonte",
        "fonte de água",
        "fonte decorativa",
        "fonte ornamental",
        "fonte de praça",
        "fonte luminosa",
    ),
    "feira": ("feira livre", "mercado de rua", "feira de rua"),
    "comércios": ("lojas", "comércio"),
    "oficina": (
        "oficina mecânica",
        "oficina de carros",
        "oficina de conserto",
        "oficina de reparos",
        "garagem de conserto",
    ),
    "oficinas": (
        "oficinas mecânicas",
        "oficinas de carros",
        "oficinas de conserto",
        "oficinas de reparos",
    ),
    "aerogerador": ("turbina eólica",),
    "torre de transmissão": (
        "torre de alta tensão",
        "torre de energia",
        "torre elétrica",
    ),
    "depósito": ("armazém", "galpão de depósito"),
    "obra": ("construção", "obra de construção"),
    "canteiro de obras": ("local de construção", "construção", "obra de construção"),
    "terreno baldio": (
        "terreno vazio",
        "lote vazio",
        "terreno abandonado",
        "lote vago",
        "terreno desocupado",
        "terreno sem construção",
        "terreno vago",
    ),
    "terraplenagem": ("movimentação de terra", "nivelamento de terreno"),
    "lixão": ("depósito de lixo", "aterro de lixo", "lixeira a céu aberto"),
    "fortaleza": ("forte", "fortificação", "forte militar"),
    "mirante": ("ponto de observação", "observatório", "belvedere"),
    "paralelepípedos": (
        "pedras de calçamento",
        "calçamento de pedra",
        "pedras da rua",
        "pedras de pavimentação",
        "pavimento de pedra",
        "calçamento de paralelepípedos",
    ),
    "viaduto": ("viaduto rodoviário", "elevado", "ponte viaduto"),
    "rotatória": (
        "rótula",
        "rotunda",
        "rotatória de trânsito",
        "círculo de trânsito",
        "rotunda de trânsito",
        "praça rotatória",
    ),
    "trevo": (
        "trevo rodoviário",
        "entroncamento",
        "trevo de estradas",
        "interseção de rodovias",
        "entroncamento rodoviário",
        "cruzamento de rodovias",
    ),
    "faixa de pedestres": (
        "travessia de pedestres",
        "faixa de travessia",
        "faixa de segurança",
        "faixa para pedestres",
        "passagem de pedestres",
    ),
    "engarrafamento": ("congestionamento", "trânsito parado"),
    "heliponto": ("heliporto",),
    "rodoviária": ("estação rodoviária", "terminal de ônibus"),
    "trilhos": ("trilhos de trem", "trilhos ferroviários", "linha férrea"),
    "pires": ("pires de xícara", "pratinho"),
    "encosta": ("declive", "encosta de morro", "encosta do morro"),
    "estradas de terra": (
        "estradas de cascalho",
        "estradas de terra batida",
        "caminhos de terra",
        "estradas rurais",
    ),
}

# Where the lexicon's troca is not the first candidate that passes, why.
NOT_THE_FIRST: dict[str, str] = {
    "açude": "*small dam* nomeia a barragem; o açude da foto é a água",
    "represa": "*dam* nomeia a barragem; a represa da foto é o lago",
    "enseada": "'baía pequena' vira *bay small view from above* na frase",
    "orla": "'beira-mar' faria mar da orla de um lago",
    "foz": "sozinha, *mouth* é uma boca",
    "guarda-sol": "'sombrinha' é guarda-chuva de mão em boa parte do Brasil",
    "guarda-sóis": "como 'guarda-sol'",
    "duna": "'duna de areia' vira *sand sand dunes*",
    "cômoro": "como 'duna'",
    "molhe": "a do plural: 'píeres de pedras' vira *Stone stones*",
    "vale": "'vale do Itajaí' viraria 'vale entre montanhas do Itajaí'",
    "canavial": "'plantação de cana' sozinha é *cane planting*",
    "milharal": "'plantação de milho' sozinha é *maize planting*",
    "caixa d'água": "'reservatório de água' oscila entre *reservoir* e *tank*",
    "estrada de terra": "cascalho é outra estrada",
    "estradas de terra": "como 'estrada de terra'",
    "estrada de chão": "como 'estrada de terra'",
    "potreiros": "o potreiro é pasto cercado",
    "taipas": "a do singular, onde 'dique de terra' vira *earth dam*",
    "estádio": "'estádio de futebol' digitado viraria 'estádio de futebol de futebol'",
    "feira": "uma feira não é um mercado",
    "trilhos": "'trilhos de trem' digitado viraria 'trilhos de trem de trem'",
}

# Forms of the first draft of the lexicon that the probe took out of it,
# with why and the `en` they had. They are still translated, with their
# candidates, so that the record says what was tried.
DROPPED: dict[str, tuple[str, tuple[str, ...]]] = {
    "quadra": (
        "quadra esportiva ou quarteirão: nenhuma troca serve aos dois sentidos",
        ("court", "block", "pitch"),
    ),
    "aterro": (
        "aterro sanitário ou aterro de terra: nenhuma troca serve aos dois sentidos",
        ("landfill", "embankment", "fill", "levee"),
    ),
    "turva": (
        "adjetivo solto: sozinha vira *cloud*; nenhuma troca tentada",
        ("murky", "cloudy", "turbid"),
    ),
    "cerca": (
        "'cerca de' é 'aproximadamente': uma troca estragaria 'cerca de dez casas'",
        ("fence",),
    ),
    "depósito": (
        "'depósito de lixo', 'depósito bancário': a troca 'armazém' estraga o resto",
        ("depot", "warehouse", "storehouse", "storage", "store"),
    ),
    "fortaleza": (
        "Fortaleza, a cidade: a troca a estragaria",
        ("fortress", "fort", "stronghold", "citadel"),
    ),
    "pires": ("nenhuma candidata acerta", ("saucer",)),
    "rotatória": (
        "a única candidata que passa, 'praça rotatória', vira *rotary square*",
        ("roundabout", "traffic circle", "rotary"),
    ),
    "quebra-mar": ("nenhuma candidata acerta", ("breakwater",)),
    "piquete": ("nenhuma candidata acerta", ("paddock", "pen", "enclosure")),
    "oficina": (
        "sozinha vira *office*, mas em frase o Marian acerta (*workshop*, nos"
        " prompts do RFC-035), e toda troca testada piora a frase",
        ("workshop", "garage", "repair shop"),
    ),
    "oficinas": (
        "como 'oficina'",
        ("workshops", "garages", "repair shops"),
    ),
    "curral": (
        "em frase o Marian acerta (*corral*); a única candidata que passa,"
        " 'curral de madeira', acrescenta o que a foto pode não ter",
        ("corral", "pen", "cattle pen", "stockyard", "yard"),
    ),
    "chiqueiro": ("nenhuma candidata acerta", ("pigsty", "pig pen", "pigpen", "sty")),
    "chafariz": ("nenhuma candidata acerta", ("fountain",)),
    "paralelepípedos": (
        "nenhuma candidata acerta",
        ("cobblestones", "cobblestone", "cobbles", "paving stones"),
    ),
    "faixa de pedestres": (
        "nenhuma candidata acerta",
        ("crosswalk", "zebra crossing", "pedestrian crossing"),
    ),
}


def plain(text: str) -> str:
    """Casefolded, without accents or punctuation: to compare English outputs."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", letters))


def contains_any(english: str, accepted: tuple[str, ...]) -> bool:
    """Whether `english` contains one of `accepted`, as whole words."""
    padded = f" {plain(english)} "
    return any(f" {plain(word)} " in padded for word in accepted)


def contexts(text: str, plural: bool) -> dict[str, str]:
    ending = "s" if plural else ""
    return {
        "sozinha": text,
        "frase": f"{text} vista de cima",
        "adjetivo m": f"{text} amarelo{ending}",
        "adjetivo f": f"{text} amarela{ending}",
    }


class Probe:
    def __init__(self, translator: MarianQueryTranslator) -> None:
        self._translator = translator
        self.cache: dict[str, str] = {}

    def translate(self, text: str) -> str:
        if text not in self.cache:
            self.cache[text] = self._translator._translate(text)
        return self.cache[text]

    def trial(self, text: str, plural: bool, en: tuple[str, ...]) -> dict[str, Any]:
        """`text` in every context: what Marian said, and whether it is right."""
        result: dict[str, Any] = {}
        for name, sentence in contexts(text, plural).items():
            english = self.translate(sentence)
            compared = english
            if name in ADJECTIVES:
                compared = re.sub(r"(?i)\byellow\w*\b", " ", english)
            result[name] = {
                "in": sentence,
                "out": english,
                "ok": contains_any(compared, en),
            }
        result["passes"] = all(result[name]["ok"] for name in GATE)
        result["agreement"] = all(result[name]["ok"] for name in ADJECTIVES)
        return result


def probe_name(
    probe: Probe, form: LexiconForm, lexicon: QueryLexicon
) -> dict[str, Any]:
    """A place name: kept in a sentence, and broken there without it."""
    sentence = NAME_SENTENCE.format(form.pt)
    without = QueryLexicon([f for f in lexicon.forms if f is not form])
    unprotected = without.rewrite(sentence)
    english = probe.translate(lexicon.rewrite(sentence))
    english_unprotected = probe.translate(unprotected)
    kept = contains_any(english, form.en)
    broken = not contains_any(english_unprotected, form.en)
    return {
        "pt": form.pt,
        "en": list(form.en),
        "origem": form.origem,
        "tambem_ingles": form.tambem_ingles,
        "troca": None,
        "name": True,
        "original": {
            "frase": {"in": sentence, "out": english, "ok": kept},
            "sem proteção": {
                "in": unprotected,
                "out": english_unprotected,
                "ok": not broken,
            },
            "passes": kept,
            "agreement": True,
        },
        "fails": not kept,
        "protection_needed": broken,
        "candidates": [],
        "first_passing_candidate": None,
    }


def probe_form(probe: Probe, form: LexiconForm) -> dict[str, Any]:
    plural = form.pt.endswith("s")
    original = probe.trial(form.pt, plural, form.en)
    entry: dict[str, Any] = {
        "pt": form.pt,
        "en": list(form.en),
        "origem": form.origem,
        "tambem_ingles": form.tambem_ingles,
        "troca": form.troca,
        "original": original,
        "fails": not original["passes"],
    }
    tried = list(CANDIDATES.get(form.pt, ()))
    if form.troca is not None and form.troca not in tried:
        tried.append(form.troca)
    entry["candidates"] = [
        {"troca": candidate, **probe.trial(candidate, plural, form.en)}
        for candidate in tried
    ]
    first = next((c["troca"] for c in entry["candidates"] if c["passes"]), None)
    entry["first_passing_candidate"] = first
    if form.troca is not None:
        chosen = next(c for c in entry["candidates"] if c["troca"] == form.troca)
        entry["troca_passes"] = chosen["passes"]
        entry["troca_agreement"] = chosen["agreement"]
        entry["troca_needed"] = entry["fails"]
    return entry


def mark(ok: bool) -> str:
    return "ok" if ok else "XX"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--forms", nargs="*", help="only these forms; writes no .json")
    parser.add_argument(
        "--candidates",
        type=Path,
        help="a JSON file of {form: [candidates]} to try after CANDIDATES's",
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.candidates:
        extra = json.loads(args.candidates.read_text(encoding="utf-8"))
        for form, more in extra.items():
            CANDIDATES[form] = (*CANDIDATES.get(form, ()), *more)

    lexicon = QueryLexicon.load()
    forms = list(lexicon.forms)
    if args.forms:
        wanted = set(args.forms)
        forms = [form for form in forms if form.pt in wanted]
        missing = wanted - {form.pt for form in forms}
        if missing:
            raise SystemExit(f"not in the lexicon: {sorted(missing)}")

    translator = MarianQueryTranslator(
        default_runtime().loader(TRANSLATOR), device=resolve_device()
    )
    translator._translate("aquecimento")  # load the checkpoint before timing
    probe = Probe(translator)

    started = time.perf_counter()
    entries = []
    for form in forms:
        if form.is_name:
            entry = probe_name(probe, form, lexicon)
            entries.append(entry)
            original = entry["original"]
            print(
                f"{mark(entry['protection_needed'] and not entry['fails'])}"
                f" {form.pt:24}{original['frase']['out']}"
                f" | without it: {original['sem proteção']['out']}"
            )
            continue
        entry = probe_form(probe, form)
        entries.append(entry)
        original = entry["original"]
        line = f"{mark(original['passes'])} {form.pt:24}"
        line += " | ".join(f"{original[name]['out']}" for name in GATE)
        if not original["agreement"]:
            line += "   [adjective: " + " / ".join(
                original[name]["out"] for name in ("adjetivo m", "adjetivo f")
            )
            line += "]"
        print(line)
        for candidate in entry["candidates"]:
            chosen = " <- troca" if candidate["troca"] == form.troca else ""
            row = f"     {mark(candidate['passes'])} {candidate['troca']:22}"
            row += " | ".join(
                candidate[name]["out"]
                for name in ("sozinha", "frase", "adjetivo m", "adjetivo f")
            )
            print(row + chosen)
    seconds = time.perf_counter() - started

    dropped = []
    if not args.forms:
        print("\ntaken out of the lexicon")
        for pt, (reason, en) in DROPPED.items():
            if any(form.pt == pt for form in lexicon.forms):
                raise SystemExit(f"{pt!r} is in DROPPED and in the lexicon")
            entry = probe_form(probe, LexiconForm(pt=pt, en=en, origem="geral"))
            entry["reason"] = reason
            dropped.append(entry)
            passing = entry["first_passing_candidate"]
            print(f"  {pt:20} {reason}; first passing candidate: {passing}")

    failing = [e for e in entries if e["fails"]]
    with_troca = [e for e in entries if e["troca"] is not None]
    names = [e for e in entries if e.get("name")]
    unexplained = []
    for entry in with_troca:
        first = entry["first_passing_candidate"]
        entry["not_the_first"] = NOT_THE_FIRST.get(entry["pt"])
        if first is not None and first != entry["troca"] and not entry["not_the_first"]:
            unexplained.append(entry["pt"])
    summary = {
        "forms": len(entries),
        "by_origin": {
            origin: sum(e["origem"] == origin for e in entries)
            for origin in sorted({e["origem"] for e in entries})
        },
        "tambem_ingles": sum(e["tambem_ingles"] for e in entries),
        "failing": len(failing),
        "failing_with_a_passing_troca": sum(
            bool(e.get("troca_passes")) for e in failing
        ),
        "failing_without_troca": [
            e["pt"] for e in failing if e["troca"] is None and not e.get("name")
        ],
        "names": [e["pt"] for e in names],
        "names_not_kept": [e["pt"] for e in names if e["fails"]],
        "names_not_needed": [e["pt"] for e in names if not e["protection_needed"]],
        "trocas": len(with_troca),
        "trocas_not_the_first_passing": [
            e["pt"]
            for e in with_troca
            if e["first_passing_candidate"] not in (None, e["troca"])
        ],
        "trocas_not_the_first_without_a_reason": unexplained,
        "trocas_not_needed": [e["pt"] for e in with_troca if not e["fails"]],
        "trocas_that_fail": [e["pt"] for e in with_troca if not e["troca_passes"]],
        "trocas_broken_by_agreement": [
            e["pt"] for e in with_troca if not e["troca_agreement"]
        ],
        "forms_broken_by_agreement": [
            e["pt"]
            for e in entries
            if e["troca"] is None and not e["fails"] and not e["original"]["agreement"]
        ],
        "trocas_with_agreement_checked": sum(e["troca_passes"] for e in with_troca),
        "dropped": [entry["pt"] for entry in dropped],
        "translations": len(probe.cache),
        "seconds": round(seconds, 1),
    }
    print()
    for key, value in summary.items():
        print(f"{key}: {value}")

    if not args.forms:
        target = HERE / "probe_lexicon.json"
        target.write_text(
            json.dumps(
                {"summary": summary, "forms": entries, "dropped": dropped},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
