# SolidVision na RTX 4070: a pipeline com o acervo real

Gerado por `tools/build_summary.py` a partir dos JSONs desta pasta. Achados e testes: [`pytest-gpu.md`](pytest-gpu.md).

## Conclusões

1. **Indexar o acervo inteiro levaria ~4,4 h nesta máquina**, contra ~8,4 h na máquina só de CPU do RFC-045 (1,9× mais rápido): 0,141 s/img contra 0,269, com a leitura antecipada ligada (o default).
2. **A indexação real confirma a estimativa:** 1756 fotos de duas pastas pela CLI, sem nenhuma falha, a 0,147 s/img.
3. **Nas fotos reais, a GPU acelera pouco o CLIP:** 84 contra 97 ms/img no lote 8 (1,16×), contra 3,3× no demo, que tem imagens pequenas. Com fotos de ~4 MB, quem domina é a decodificação do JPEG e o pré-processamento, que rodam na CPU e uma imagem por vez dentro do `encode_images`. **Paralelizar a decodificação é o próximo ganho de vazão, não uma GPU maior.**
4. **Logo depois vem o HD USB:** 25,9 MB/s em leituras espalhadas, cerca de 154 ms por foto só para ler. A leitura antecipada esconde boa parte disso, e o s/img estável fica perto desse piso.
5. **Os vetores da GPU são intercambiáveis com os da CPU:** o cosseno mínimo entre eles é 0.99998 nas 512 fotos do acervo. Na busca sobre a amostra indexada, o top-10 de 25 das 25 consultas é idêntico entre GPU e CPU. Os 5 testes `slow` que falham na GPU exigem `1e-5` contra referências de CPU, o que é uma tolerância de máquina e não de qualidade.
6. **Qualidade no demo, na GPU:** Recall@5 de 88% e top-1 estrito de 68%, contra 84% e 64% na linha de base. A diferença é 1 consulta de 25, ou seja, ruído de ponto flutuante, sem perda.
7. **Busca:** mediana de 62 ms na GPU e 117 ms na CPU, dos quais o banco (HNSW) é só ~7 ms. O resto é a tradução PT→EN (Marian): as consultas em inglês ficam em ~17 ms na GPU. Auto-recuperação: 20/20.
8. **Dois problemas achados no caminho**, detalhados em [`pytest-gpu.md`](pytest-gpu.md):
   - o `.env` desta máquina tem `SUPPORTED_EXTENSIONS` no formato JSON antigo, e com ele **todo scan acha 0 fotos, sem erro**. Nestas medições, a variável de ambiente corrige isso; o arquivo precisa ser corrigido;
   - `scripts/benchmark_indexing.py` não roda mais desde o RFC-027 (`Image(path=...)`). O `tools/clip_bench.py` daqui o substituiu nesta medição.

## Máquina

| | |
|---|---|
| CPU | AMD Ryzen 7 5700G with Radeon Graphics (8 núcleos / 16 threads) |
| RAM | 28 GB |
| GPU | NVIDIA GeForce RTX 4070 (12 GB), CUDA 12.6 |
| torch | 2.13.0+cu126 |
| Python | 3.12.0 |
| Acervo | HD Seagate Expansion. USB. 112.802 JPEGs |
| Commit | `frontend` @ `ac4df0a` |

Desvios do ambiente do projeto: Python 3.12.0, nao o 3.12.9 que o make_venv.py exige (o 3.12.9 nao esta instalado nesta maquina).; torch 2.13.0+cu126 e torchvision 0.28.0+cu126 no lugar dos +cpu do scripts/python-win-64.lock, para usar a GPU; os demais pacotes sao os do lock, com hash, sem resolver.; psutil instalado a parte (o benchmark_indexing.py o importa e nenhum lock o traz).; Ambiente em .venv-gpu, nao em .venv.

## CLIP: GPU × CPU (`clip-bench.json`)

Os bytes são lidos antes e passados ao adaptador, então o disco fica fora da vazão. O tempo medido é o do adaptador: decodificar com o rascunho JPEG, pré-processar, passar pelo modelo e normalizar.

### demo: 45 imagens, média de 0,17 MB por arquivo

Leitura do disco: 1.057,8 MB/s, 0,2 ms por imagem.

| dispositivo | lote | ms/img | img/s | × lote 1 | pico RSS MB | pico VRAM MB |
|---|---:|---:|---:|---:|---:|---:|
| cuda | 1 | 19,37 | 51,6 | 1,00 | 1.190 | 596 |
| cuda | 2 | 14,53 | 68,8 | 1,33 | 1.194 | 596 |
| cuda | 4 | 12,15 | 82,3 | 1,59 | 1.197 | 596 |
| cuda | 8 | 11,40 | 87,7 | 1,70 | 1.202 | 607 |
| cuda | 16 | 12,35 | 81,0 | 1,57 | 1.209 | 622 |
| cuda | 32 | 14,23 | 70,3 | 1,36 | 1.236 | 660 |
| cuda | 64 | 13,41 | 74,6 | 1,44 | 1.241 | 690 |
| cuda | 128 | 13,61 | 73,5 | 1,42 | 1.242 | 690 |
| cpu | 1 | 65,55 | 15,3 | 1,00 | 1.590 | - |
| cpu | 8 | 38,16 | 26,2 | 1,72 | 1.570 | - |
| cpu | 32 | 37,74 | 26,5 | 1,74 | 1.647 | - |

**No lote 8 (o default), a GPU é 3,3× mais rápida que a CPU desta máquina.**

Equivalência GPU × CPU (cosseno, vetor a vetor):

| lote | cosseno médio | cosseno mínimo | max abs(delta) |
|---:|---:|---:|---:|
| 1 | 0.9999974 | 0.9999785 | 1.29e-03 |
| 8 | 1.0000000 | 0.9999999 | 4.31e-06 |
| 32 | 0.9999981 | 0.9999785 | 1.29e-03 |

### acervo: 512 imagens, média de 3,97 MB por arquivo

Leitura do disco: 25,9 MB/s, 153,6 ms por imagem.

| dispositivo | lote | ms/img | img/s | × lote 1 | pico RSS MB | pico VRAM MB |
|---|---:|---:|---:|---:|---:|---:|
| cuda | 1 | 116,14 | 8,6 | 1,00 | 3.676 | 596 |
| cuda | 2 | 98,96 | 10,1 | 1,17 | 3.625 | 592 |
| cuda | 4 | 88,02 | 11,4 | 1,32 | 3.623 | 594 |
| cuda | 8 | 84,05 | 11,9 | 1,38 | 3.636 | 607 |
| cuda | 16 | 75,86 | 13,2 | 1,53 | 3.646 | 622 |
| cuda | 32 | 69,98 | 14,3 | 1,66 | 3.658 | 660 |
| cuda | 64 | 69,72 | 14,3 | 1,67 | 3.680 | 736 |
| cuda | 128 | 69,27 | 14,4 | 1,68 | 3.737 | 886 |
| cpu | 1 | 125,40 | 8,0 | 1,00 | 3.636 | - |
| cpu | 8 | 97,18 | 10,3 | 1,29 | 3.652 | - |
| cpu | 32 | 93,21 | 10,7 | 1,35 | 3.688 | - |

**No lote 8 (o default), a GPU é 1,2× mais rápida que a CPU desta máquina.**

Equivalência GPU × CPU (cosseno, vetor a vetor):

| lote | cosseno médio | cosseno mínimo | max abs(delta) |
|---:|---:|---:|---:|
| 1 | 0.9999991 | 0.9999790 | 1.74e-03 |
| 8 | 1.0000000 | 0.9999999 | 2.65e-06 |
| 32 | 0.9999991 | 0.9999789 | 1.74e-03 |

## Primeira indexação, como entregue (RFC-045, passo `first`)

É o `JobRunner` de produção (leitura antecipada. CLIP. miniaturas. PostgreSQL embarcado temporário). sobre grupos de fotos frias sorteadas do disco. A linha de base é o `measure_read_ahead.json` do repositório: Intel64 Family 6 Model 126 Stepping 5. GenuineIntel. só CPU. de 2026-10-28. num disco de 116.396 arquivos.

| máquina | modo | s/img (estável) | CLIP ms/img | miniatura ms/img | espera ms/img | pico WS MB | disco inteiro |
|---|---|---:|---:|---:|---:|---:|---:|
| RFC-045 (CPU) | off | 0,383 | 167 | 89 | 59 | 1.102 | 12,0 h |
| RFC-045 (CPU) | on/32 | 0,269 | 176 | 92 | 21 | 1.116 | 8,4 h |
| RFC-045 (CPU) | on/64 | 0,266 | 171 | 91 | 40 | 1.229 | 8,3 h |
| esta (GPU) | off | 0,303 | 77 | 51 | 57 | 1.341 | 9,5 h |
| esta (GPU) | on/32 | 0,141 | 72 | 49 | 43 | 1.489 | 4,4 h |
| esta (GPU) | on/64 | 0,151 | 74 | 51 | 64 | 1.585 | 4,7 h |

"Disco inteiro" = s/img estável × 112.802 JPEGs deste acervo. a mesma conta do `hours_for_the_archive` do RFC-045.

**Vetores (passo `vectors`):**

```json
{
  "photos": 50,
  "reduced_by_the_draft": 41,
  "bytes_vs_path_max_abs_difference": 0.0,
  "bytes_vs_path_identical": true,
  "cosine_with_today_mean": 0.9996128467221211,
  "cosine_with_today_min": 0.9969843959236622,
  "identical_to_today": 9,
  "cosine_with_today_mean_of_the_changed": 0.9995278654460956,
  "cosine_with_today_min_of_the_changed": 0.9969843959236622
}
```

Na linha de base:

```json
{
  "photos": 50,
  "reduced_by_the_draft": 40,
  "bytes_vs_path_max_abs_difference": 0.0,
  "bytes_vs_path_identical": true,
  "cosine_with_today_mean": 0.9996775186995471,
  "cosine_with_today_min": 0.9985533407692786,
  "identical_to_today": 10,
  "cosine_with_today_mean_of_the_changed": 0.9995969029929703,
  "cosine_with_today_min_of_the_changed": 0.9985533407692786
}
```

**Demo (passo `demo`):**

```json
{
  "strict_top_1": 68.0,
  "recall_at_5": 88.0,
  "contamination_at_5": 11.200000000000001,
  "pairwise": 77.77777777777779,
  "demo_vectors_identical_with_and_without_the_draft": true,
  "reference_max_abs_difference_without_the_draft": 0.0012933388352394104,
  "reference_machine_is_this_one": false,
  "reference_one_by_one_bit_identical": false,
  "reference_one_by_one_max_abs_difference": 0.0012933388352394104,
  "reference_batched_max_abs_difference": 3.4347176551818848e-06
}
```

Na linha de base:

```json
{
  "strict_top_1": 64.0,
  "recall_at_5": 84.0,
  "contamination_at_5": 12.8,
  "pairwise": 80.42328042328042,
  "demo_vectors_identical_with_and_without_the_draft": true,
  "reference_max_abs_difference_without_the_draft": 1.2218952178955078e-06,
  "reference_machine_is_this_one": false,
  "reference_one_by_one_bit_identical": false,
  "reference_one_by_one_max_abs_difference": 1.2218952178955078e-06,
  "reference_batched_max_abs_difference": 1.2032687664031982e-06
}
```

## Indexação real de uma amostra, pela CLI (`index-sample.json`)

| pasta | fotos | indexadas | falhas | s | s/img | img/s |
|---|---:|---:|---:|---:|---:|---:|
| pasta 1 | 843 | 843 | 0 | 122,5 | 0,145 | 6,9 |
| pasta 2 | 913 | 913 | 0 | 135,5 | 0,148 | 6,7 |

**Projeção para o disco inteiro, por esta amostra: 4,6 h.**

## Busca sobre a amostra indexada (`search-*.json`)

| dispositivo | imagens no índice | consultas | total ms (mediana) | total ms (p95) | banco ms (mediana) | carga do CLIP | auto-recuperação |
|---|---:|---:|---:|---:|---:|---:|---:|
| gpu | 1756 | 25 | 62,3 | 70,0 | 6,6 | 0,73 s | 20/20 |
| cpu | 1756 | 25 | 117,1 | 133,9 | 7,8 | 0,36 s | 20/20 |

Total = `SearchImagesUseCase.execute` (tradução, CLIP de texto e HNSW), a quente. O top-10 de cada consulta, com o prompt em inglês, está nos JSONs, para comparar com uma próxima rodada.
