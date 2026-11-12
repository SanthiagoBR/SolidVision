"""Registra a máquina e o ambiente em que os relatórios desta pasta foram medidos.

    .venv-gpu\\Scripts\\python.exe reports\\2026-11-12-rtx4070\\tools\\machine_info.py

Escreve `machine.json` na pasta do relatório. Sem segredos: nada do `.env`.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psutil
import torch

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent
REPO = REPORT.parents[1]


def _run(*args: str) -> str:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, check=False, cwd=REPO
        ).stdout.strip()
    except OSError as error:
        return f"<{error}>"


def _cpu_name() -> str:
    out = _run(
        "powershell",
        "-NoProfile",
        "-Command",
        "(Get-CimInstance Win32_Processor).Name",
    )
    return out or platform.processor()


def _volumes() -> str:
    return _run(
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-PhysicalDisk | Select FriendlyName,MediaType,BusType,"
        "@{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | ConvertTo-Json",
    )


def main() -> None:
    gpu = None
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        gpu = {
            "name": props.name,
            "vram_gb": round(props.total_memory / 1024**3, 2),
            "compute_capability": f"{props.major}.{props.minor}",
            "torch_cuda": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
            "nvidia_smi": _run(
                "nvidia-smi",
                "--query-gpu=driver_version,memory.used,utilization.gpu,"
                "power.limit,clocks.max.sm",
                "--format=csv",
            ),
        }

    info = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "git": {
            "branch": _run("git", "rev-parse", "--abbrev-ref", "HEAD"),
            "commit": _run("git", "rev-parse", "HEAD"),
            "dirty": bool(_run("git", "status", "--porcelain")),
        },
        "platform": platform.platform(),
        "cpu": _cpu_name(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "physical_disks": _volumes(),
        "python": sys.version,
        "torch": torch.__version__,
        "gpu": gpu,
        "deviations_from_the_project": [
            "Python 3.12.0, nao o 3.12.9 que o make_venv.py exige "
            "(o 3.12.9 nao esta instalado nesta maquina).",
            "torch 2.13.0+cu126 e torchvision 0.28.0+cu126 no lugar dos "
            "+cpu do scripts/python-win-64.lock, para usar a GPU; os demais "
            "pacotes sao os do lock, com hash, sem resolver.",
            "psutil instalado a parte (o benchmark_indexing.py o importa e "
            "nenhum lock o traz).",
            "Ambiente em .venv-gpu, nao em .venv.",
        ],
    }
    out = REPORT / "machine.json"
    out.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(info, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
