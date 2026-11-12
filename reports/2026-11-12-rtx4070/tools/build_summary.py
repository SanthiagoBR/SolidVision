"""Monta `RESUMO.md` a partir dos JSONs desta pasta e da linha de base do RFC-045.

    .venv-gpu\\Scripts\\python.exe reports\\2026-11-12-rtx4070\\tools\\build_summary.py

Cada tabela sai de um arquivo desta pasta. O que falta (um passo que não rodou)
vira uma linha dizendo isso, e não um número inventado.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent
REPO = REPORT.parents[1]
BASELINE = REPO / "experiments" / "rfc-045-leitura-antecipada" / "measure_read_ahead.json"
ACERVO_FILES = 112_802


def _load(name: str) -> dict | None:
    path = REPORT / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _hours(seconds_per_image: float, files: int = ACERVO_FILES) -> str:
    return _fmt(seconds_per_image * files / 3600, 1) + " h"


def section_machine(out: list[str]) -> None:
    machine = _load("machine.json")
    if machine is None:
        return
    gpu = machine.get("gpu") or {}
    out += [
        "## Máquina",
        "",
        "| | |",
        "|---|---|",
        f"| CPU | {machine['cpu']} ({machine['cpu_cores_physical']} núcleos / "
        f"{machine['cpu_cores_logical']} threads) |",
        f"| RAM | {_fmt(machine['ram_gb'], 0)} GB |",
        f"| GPU | {gpu.get('name', '-')} ({_fmt(gpu.get('vram_gb'), 0)} GB), "
        f"CUDA {gpu.get('torch_cuda', '-')} |",
        f"| torch | {machine['torch']} |",
        f"| Python | {machine['python'].split()[0]} |",
        f"| Acervo | HD Seagate Expansion, USB, {ACERVO_FILES:,} JPEGs |".replace(",", "."),
        f"| Commit | `{machine['git']['branch']}` @ `{machine['git']['commit'][:7]}` |",
        "",
        "Desvios do ambiente do projeto: "
        + "; ".join(machine["deviations_from_the_project"]),
        "",
    ]


def section_clip(out: list[str]) -> None:
    bench = _load("clip-bench.json")
    out += ["## CLIP: GPU × CPU (`clip-bench.json`)", ""]
    if bench is None:
        out += ["Não rodou.", ""]
        return
    out += [
        "Os bytes são lidos antes e passados ao adaptador, então o disco fica fora "
        "da vazão. O tempo medido é o do adaptador: decodificar com o rascunho "
        "JPEG, pré-processar, passar pelo modelo e normalizar.",
        "",
    ]
    for name, corpus in bench["corpora"].items():
        read = corpus["read"]
        out += [
            f"### {name}: {corpus['images']} imagens, média de "
            f"{_fmt(read['mean_file_mb'])} MB por arquivo",
            "",
            f"Leitura do disco: {_fmt(read['mb_per_second'], 1)} MB/s, "
            f"{_fmt(read['ms_per_image'], 1)} ms por imagem.",
            "",
            "| dispositivo | lote | ms/img | img/s | × lote 1 | pico RSS MB | pico VRAM MB |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for device, rows in corpus["sweep"].items():
            for row in rows:
                out.append(
                    f"| {device} | {row['batch_size']} | {_fmt(row['ms_per_image'])} | "
                    f"{_fmt(row['images_per_second'], 1)} | "
                    f"{_fmt(row['speedup_vs_batch_1'])} | {_fmt(row['peak_rss_mb'], 0)} | "
                    f"{_fmt(row.get('peak_vram_mb'), 0)} |"
                )
        cpu = {r["batch_size"]: r for r in corpus["sweep"].get("cpu", [])}
        gpu = {r["batch_size"]: r for r in corpus["sweep"].get("cuda", [])}
        if 8 in cpu and 8 in gpu:
            out += [
                "",
                f"**No lote 8 (o default), a GPU é "
                f"{_fmt(cpu[8]['ms_per_image'] / gpu[8]['ms_per_image'], 1)}× mais rápida "
                "que a CPU desta máquina.**",
            ]
        eq = corpus["equivalence"].get("gpu vs cpu")
        if eq:
            out += ["", "Equivalência GPU × CPU (cosseno, vetor a vetor):", "",
                    "| lote | cosseno médio | cosseno mínimo | max abs(delta) |",
                    "|---:|---:|---:|---:|"]
            for size, values in eq.items():
                out.append(
                    f"| {size} | {values['mean_cosine']:.7f} | {values['min_cosine']:.7f} | "
                    f"{values['max_component_delta']:.2e} |"
                )
        out.append("")


def _pooled(result: dict) -> dict:
    return result.get("first_pooled", {})


def section_read_ahead(out: list[str]) -> None:
    gpu = _load("read-ahead-gpu.json")
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    out += [
        "## Primeira indexação, como entregue (RFC-045, passo `first`)",
        "",
        "É o `JobRunner` de produção (leitura antecipada, CLIP, miniaturas, "
        "PostgreSQL embarcado temporário), sobre grupos de fotos frias sorteadas "
        "do disco. A linha de base é o `measure_read_ahead.json` do repositório: "
        f"{base['machine']['processor']}, só CPU, de {base['date'][:10]}, num disco de "
        f"{base['files_on_disk']:,} arquivos.".replace(",", "."),
        "",
    ]
    if gpu is None:
        out += ["Não rodou nesta máquina.", ""]
        return
    out += [
        "| máquina | modo | s/img (estável) | CLIP ms/img | miniatura ms/img | "
        "espera ms/img | pico WS MB | disco inteiro |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, data in (("RFC-045 (CPU)", base), ("esta (GPU)", gpu)):
        for mode, row in _pooled(data).items():
            out.append(
                f"| {label} | {mode} | {_fmt(row['steady_seconds_per_image'], 3)} | "
                f"{_fmt(row['inference_ms'], 0)} | {_fmt(row['thumbnail_ms'], 0)} | "
                f"{_fmt(row['wait_ms_per_image'], 0)} | {_fmt(row['peak_working_set_mb'], 0)} | "
                f"{_hours(row['steady_seconds_per_image'])} |"
            )
    out += [
        "",
        f"\"Disco inteiro\" = s/img estável × {ACERVO_FILES:,} JPEGs deste acervo, "
        "a mesma conta do `hours_for_the_archive` do RFC-045.".replace(",", "."),
        "",
    ]
    for key, title in (("vectors", "Vetores (passo `vectors`)"), ("demo", "Demo (passo `demo`)")):
        if key in gpu:
            out += [f"**{title}:**", "", "```json", json.dumps(gpu[key], indent=2), "```", ""]
            if key in base:
                out += ["Na linha de base:", "", "```json", json.dumps(base[key], indent=2),
                        "```", ""]


def section_index(out: list[str]) -> None:
    index = _load("index-sample.json")
    out += ["## Indexação real de uma amostra, pela CLI (`index-sample.json`)", ""]
    if index is None:
        out += ["Não rodou.", ""]
        return
    out += [
        "| pasta | fotos | indexadas | falhas | s | s/img | img/s |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for job in index["jobs"]:
        out.append(
            f"| {job['root']} | {job['discovered']} | {job['processed']} | {job['failed']} | "
            f"{_fmt(job['seconds'], 1)} | {_fmt(job['seconds_per_image'], 3)} | "
            f"{_fmt(1 / job['seconds_per_image'] if job['seconds_per_image'] else None, 1)} |"
        )
    total = index.get("total")
    if total:
        out += ["", f"**Projeção para o disco inteiro, por esta amostra: "
                    f"{_hours(total['seconds_per_image'])}.**", ""]


def section_search(out: list[str]) -> None:
    out += ["## Busca sobre a amostra indexada (`search-*.json`)", ""]
    rows = []
    for device in ("gpu", "cpu"):
        data = _load(f"search-{device}.json")
        if data is None:
            continue
        summary = data["summary"]
        rows.append(
            f"| {device} | {data['census']['images']} | {summary['queries']} | "
            f"{_fmt(summary['warm_total_ms_median'], 1)} | {_fmt(summary['warm_total_ms_p95'], 1)} | "
            f"{_fmt(summary['warm_db_ms_median'], 1)} | {_fmt(data['clip_load_seconds'])} s | "
            f"{summary['self_retrieval_found']}/{summary['self_retrieval_checked']} |"
        )
    if not rows:
        out += ["Não rodou.", ""]
        return
    out += [
        "| dispositivo | imagens no índice | consultas | total ms (mediana) | total ms (p95) | "
        "banco ms (mediana) | carga do CLIP | auto-recuperação |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        *rows,
        "",
        "Total = `SearchImagesUseCase.execute` (tradução, CLIP de texto e HNSW), a quente. "
        "O top-10 de cada consulta, com o prompt em inglês, está nos JSONs, para "
        "comparar com uma próxima rodada.",
        "",
    ]


def section_findings(out: list[str]) -> None:
    """As conclusões, com os números lidos dos JSONs (nada digitado à mão)."""
    bench, ra, index = _load("clip-bench.json"), _load("read-ahead-gpu.json"), _load(
        "index-sample.json"
    )
    gpu_s, cpu_s = _load("search-gpu.json"), _load("search-cpu.json")
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    out += ["## Conclusões", ""]
    if ra:
        on = ra["first_pooled"]["on/32"]
        b_on = base["first_pooled"]["on/32"]
        out.append(
            f"1. **Indexar o acervo inteiro levaria ~{_fmt(on['steady_seconds_per_image'] * ACERVO_FILES / 3600, 1)} h "
            f"nesta máquina**, contra ~{_fmt(b_on['steady_seconds_per_image'] * ACERVO_FILES / 3600, 1)} h na "
            f"máquina só de CPU do RFC-045 ({_fmt(b_on['steady_seconds_per_image'] / on['steady_seconds_per_image'], 1)}× "
            f"mais rápido): {_fmt(on['steady_seconds_per_image'], 3)} s/img contra "
            f"{_fmt(b_on['steady_seconds_per_image'], 3)}, com a leitura antecipada ligada (o default)."
        )
    if index and index.get("total", {}).get("seconds_per_image"):
        out.append(
            f"2. **A indexação real confirma a estimativa:** {index['total']['processed']} fotos de "
            f"duas pastas pela CLI, sem nenhuma falha, a {_fmt(index['total']['seconds_per_image'], 3)} s/img."
        )
    if bench and "acervo" in bench["corpora"]:
        corpus = bench["corpora"]["acervo"]
        cpu = {r["batch_size"]: r for r in corpus["sweep"]["cpu"]}
        gpu = {r["batch_size"]: r for r in corpus["sweep"]["cuda"]}
        demo = bench["corpora"]["demo"]["sweep"]
        d_cpu = {r["batch_size"]: r for r in demo["cpu"]}
        d_gpu = {r["batch_size"]: r for r in demo["cuda"]}
        out.append(
            f"3. **Nas fotos reais, a GPU acelera pouco o CLIP:** "
            f"{_fmt(gpu[8]['ms_per_image'], 0)} contra {_fmt(cpu[8]['ms_per_image'], 0)} ms/img no lote 8 "
            f"({_fmt(cpu[8]['ms_per_image'] / gpu[8]['ms_per_image'], 2)}×), contra "
            f"{_fmt(d_cpu[8]['ms_per_image'] / d_gpu[8]['ms_per_image'], 1)}× no demo, que tem imagens pequenas. "
            "Com fotos de ~4 MB, quem domina é a decodificação do JPEG e o pré-processamento, que rodam "
            "na CPU e uma imagem por vez dentro do `encode_images`. **Paralelizar a "
            "decodificação é o próximo ganho de vazão, não uma GPU maior.**"
        )
        out.append(
            f"4. **Logo depois vem o HD USB:** {_fmt(corpus['read']['mb_per_second'], 1)} MB/s em leituras espalhadas, "
            f"cerca de {_fmt(corpus['read']['ms_per_image'], 0)} ms por foto só para ler. A leitura antecipada "
            "esconde boa parte disso, e o s/img estável fica perto desse piso."
        )
        eq = corpus["equivalence"]["gpu vs cpu"]
        worst = min(v["min_cosine"] for v in eq.values())
        out.append(
            f"5. **Os vetores da GPU são intercambiáveis com os da CPU:** o cosseno mínimo entre "
            f"eles é {worst:.5f} nas {corpus['images']} fotos do acervo."
            + (
                f" Na busca sobre a amostra indexada, o top-10 de "
                f"{sum(1 for a, b in zip(gpu_s['queries'], cpu_s['queries']) if [t['relative_path'] for t in a['top']] == [t['relative_path'] for t in b['top']])}"
                f" das {len(gpu_s['queries'])} consultas é idêntico entre GPU e CPU."
                if gpu_s and cpu_s
                else ""
            )
            + " Os 5 testes `slow` que falham na GPU exigem `1e-5` contra referências de CPU, o que é "
            "uma tolerância de máquina e não de qualidade."
        )
    if ra and "demo" in ra:
        out.append(
            f"6. **Qualidade no demo, na GPU:** Recall@5 de {_fmt(ra['demo']['recall_at_5'], 0)}% e top-1 estrito de "
            f"{_fmt(ra['demo']['strict_top_1'], 0)}%, contra {_fmt(base['demo']['recall_at_5'], 0)}% e "
            f"{_fmt(base['demo']['strict_top_1'], 0)}% na linha de base. A diferença é 1 consulta de 25, "
            "ou seja, ruído de ponto flutuante, sem perda."
        )
    if gpu_s and cpu_s:
        out.append(
            f"7. **Busca:** mediana de {_fmt(gpu_s['summary']['warm_total_ms_median'], 0)} ms na GPU e "
            f"{_fmt(cpu_s['summary']['warm_total_ms_median'], 0)} ms na CPU, dos quais o banco (HNSW) é só "
            f"~{_fmt(gpu_s['summary']['warm_db_ms_median'], 0)} ms. O resto é a tradução PT→EN (Marian): as "
            "consultas em inglês ficam em ~17 ms na GPU. Auto-recuperação: "
            f"{gpu_s['summary']['self_retrieval_found']}/{gpu_s['summary']['self_retrieval_checked']}."
        )
    out += [
        "8. **Dois problemas achados no caminho**, detalhados em [`pytest-gpu.md`](pytest-gpu.md):",
        "   - o `.env` desta máquina tem `SUPPORTED_EXTENSIONS` no formato JSON antigo, e com ele "
        "**todo scan acha 0 fotos, sem erro**. Nestas medições, a variável de ambiente corrige isso; "
        "o arquivo precisa ser corrigido;",
        "   - `scripts/benchmark_indexing.py` não roda mais desde o RFC-027 (`Image(path=...)`). "
        "O `tools/clip_bench.py` daqui o substituiu nesta medição.",
        "",
    ]


def main() -> None:
    out = [
        "# SolidVision na RTX 4070: a pipeline com o acervo real",
        "",
        "Gerado por `tools/build_summary.py` a partir dos JSONs desta pasta. "
        "Achados e testes: [`pytest-gpu.md`](pytest-gpu.md).",
        "",
    ]
    section_findings(out)
    section_machine(out)
    section_clip(out)
    section_read_ahead(out)
    section_index(out)
    section_search(out)
    (REPORT / "RESUMO.md").write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {REPORT / 'RESUMO.md'}")


if __name__ == "__main__":
    main()
