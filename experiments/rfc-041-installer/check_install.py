"""The reading of the clean-account test (RFC-041 section 9).

Run in the second account by the package's own Python, the only one it has,
from the public folder the development account copied it to:

    $py = "$env:LOCALAPPDATA\\Programs\\SolidVision\\resources\\runtime\\python\\python.exe"
    & $py C:\\Users\\Public\\SolidVision-test\\check_install.py steps
    & $py C:\\Users\\Public\\SolidVision-test\\check_install.py modules
    & $py C:\\Users\\Public\\SolidVision-test\\check_install.py after

    steps     the list of steps of the test, in order
    modules   with the app open, after a search: every module every process of
              the package has loaded, and where it came from (section 2.4)
    after     the first level of the four profile folders against the lists
              written before the installation; the openings of `shell.log`
              with their marks; the sizes on disk

Everything it prints also goes to `C:\\Users\\Public\\SolidVision-test\\<user>\\`,
where the development account reads it afterwards. Standard library only:
`ctypes` for the processes and their modules, as the RFC-033 measurements did.

The "before" lists are written by PowerShell, before the package -- and so
this Python -- exists (step 8 of `steps`): one name per line, one file per
folder, `before-USERPROFILE.txt` and so on.
"""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import re
import sys
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import Any

PUBLIC = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
OUT = PUBLIC / "SolidVision-test" / os.environ.get("USERNAME", "user")
FOLDERS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP")
LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
INSTALL = LOCAL / "Programs" / "SolidVision"
DATA = LOCAL / "SolidVision"
SHELL_LOG = DATA / "logs" / "shell.log"
SYSTEM_ROOT = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))

STEPS = r"""
O teste em conta limpa (RFC-041 §9). Cerca de uma hora, uma estimativa.

Na conta de desenvolvimento (preparo):
 1. `npm run dist` em frontend\ -> frontend\release\SolidVision-Setup-0.1.0.exe.
 2. Copiar o instalador para C:\Users\Public\Downloads e pôr nele a marca da
    internet, como um download teria:
      Set-Content -Path C:\Users\Public\Downloads\SolidVision-Setup-0.1.0.exe `
        -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
 3. Copiar o dataset: backend\dataset\demo\images -> C:\Users\Public\Pictures\SolidVision-demo
 4. Copiar este arquivo para C:\Users\Public\SolidVision-test\check_install.py
 5. Criar uma conta local PADRÃO, sem administrador (ato de administrador da
    máquina: Configurações > Contas > Família e outros usuários).
 6. Desligar a rede (cabo, Wi-Fi ou modo avião) antes de entrar na conta nova.

Na conta nova, com a rede desligada até o fim:
 7. Entrar na conta (o primeiro login cria o perfil).
 8. PowerShell, ANTES de instalar:
      $out = "C:\Users\Public\SolidVision-test\$env:USERNAME"; New-Item -ItemType Directory -Force $out
      foreach ($n in 'USERPROFILE','APPDATA','LOCALAPPDATA','TEMP') {
        Get-ChildItem -Force -Name ([Environment]::GetEnvironmentVariable($n)) |
          Set-Content -Encoding utf8 "$out\before-$n.txt" }
 9. Abrir C:\Users\Public\Downloads e dar dois cliques no instalador. Registrar
    cada tela do SmartScreen (Win+Shift+S): "Mais informações", "Executar
    assim mesmo". Nenhum pedido de elevação deve aparecer.
10. O app abre ao terminar: é a primeira abertura, a frio. Esperar a tela
    inicial.
11. Adicionar o disco C:, abrir a pasta Users\Public\Pictures\SolidVision-demo,
    "Indexar as pastas", e esperar as 45 indexadas.
12. Uma busca em português ("casa com piscina"), abrir um resultado e
    "Parecidas com esta". Abrir o Mapa: as células sobre o contorno dos
    estados e a faixa da pausa dos tiles.
13. Com o app aberto: `check_install.py modules`.
14. Fechar a janela. Abrir pelo atalho do Menu Iniciar, esperar a tela inicial
    e fechar; cinco vezes (as aberturas a quente).
15. Abrir o app, escolher uma pasta grande (C:\Windows) e "Indexar as pastas":
    com o job em andamento, desinstalar por Configurações > Aplicativos.
16. PowerShell, depois de desinstalar:
      Test-Path "$env:LOCALAPPDATA\Programs\SolidVision"          # False
      Get-ChildItem "$env:LOCALAPPDATA\SolidVision"               # os dados, inteiros
      Get-CimInstance Win32_Process | ? { $_.Path -like "*\Programs\SolidVision\*" }  # nada
      Test-Path "$env:USERPROFILE\Desktop\SolidVision.lnk"          # False
      Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\SolidVision.lnk"  # False
17. Instalar de novo pelo mesmo instalador: o acervo de antes, sem reindexar,
    e o job interrompido retomado.
18. `check_install.py after`, e sair da conta. Religar a rede.

Na conta de desenvolvimento: ler C:\Users\Public\SolidVision-test\<conta>\.
"""


def say(lines: list[str], line: str = "") -> None:
    print(line)
    lines.append(line)


def save(name: str, lines: list[str], data: Any) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    (OUT / f"check_install-{name}-{stamp}.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    (OUT / f"check_install-{name}-{stamp}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nwritten to {OUT}")


# --------------------------------------------------------------------------
# The processes of the package, and their modules
# --------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPMODULE = 0x8
TH32CS_SNAPMODULE32 = 0x10
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def processes() -> Iterator[tuple[int, str]]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            path = image_path(entry.th32ProcessID)
            if path:
                yield entry.th32ProcessID, path
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)


def image_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def modules(pid: int) -> list[str]:
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if not snapshot or snapshot == INVALID_HANDLE:
        return []
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    found = []
    try:
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append(entry.szExePath)
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def windows_protects(path: Path) -> bool:
    sfc: Any = ctypes.WinDLL("sfc")
    sfc.SfcIsFileProtected.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    return bool(sfc.SfcIsFileProtected(None, str(path)))


def origin(module: Path) -> str:
    """package, windows, windows-unprotected, driver-store, or outside."""
    resolved = str(module).lower()
    if resolved.startswith(str(INSTALL).lower() + "\\"):
        return "package"
    if resolved.startswith(str(SYSTEM_ROOT).lower() + "\\"):
        if "\\driverstore\\" in resolved:
            return "driver-store"
        if "\\winsxs\\" in resolved or windows_protects(module):
            return "windows"
        return "windows-unprotected"
    return "outside"


def command_modules() -> int:
    lines: list[str] = []
    data: dict[str, Any] = {"install": str(INSTALL), "processes": []}
    say(lines, f"the processes running from {INSTALL}")
    ours = [
        (pid, path)
        for pid, path in processes()
        if path.lower().startswith(str(INSTALL).lower() + "\\")
    ]
    if not ours:
        say(lines, "none: open the app, search once, and run this again")
        return 1
    flagged = 0
    for pid, path in sorted(ours, key=lambda item: item[1]):
        loaded = [Path(module) for module in modules(pid)]
        kinds: dict[str, list[str]] = {}
        for module in loaded:
            kinds.setdefault(origin(module), []).append(str(module))
        msvcp = [str(m) for m in loaded if m.name.lower() == "msvcp140.dll"]
        relative = Path(path).relative_to(INSTALL)
        say(
            lines,
            f"  {pid} {relative}: {len(loaded)} modules, "
            + ", ".join(f"{kind} {len(paths)}" for kind, paths in sorted(kinds.items())),
        )
        if msvcp:
            say(lines, f"      msvcp140.dll from {msvcp[0]}")
        for kind in ("windows-unprotected", "outside"):
            for module_path in kinds.get(kind, []):
                flagged += 1
                say(lines, f"      {kind}: {module_path}")
        data["processes"].append(
            {"pid": pid, "path": path, "modules": kinds, "msvcp140": msvcp}
        )
    say(
        lines,
        f"{flagged} modules from outside the package and Windows"
        + ("" if flagged else ": none"),
    )
    save("modules", lines, data)
    return 0


# --------------------------------------------------------------------------
# After the test
# --------------------------------------------------------------------------

LINE_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| ")
API_LINE_TIME = re.compile(r"^api \| (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| ")
WARMED_UP = "Embedding model warmed up in"
"""The API's line when the warm-up ends (`search: ready`), to the second."""
MARK = re.compile(r"\| shell \| mark (\w+) \+(\d+) ms ?(.*)$")
OPENING = re.compile(r"\| shell \| SolidVision (\S+) \((\S+)\), Electron (\S+), PID")


def openings(log: Path) -> list[dict[str, Any]]:
    """Each opening of the window, with its marks and the moment the search
    became ready, in milliseconds since the process began."""
    found: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    start: datetime.datetime | None = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if OPENING.search(line):
            current = {"line": line.split(" | shell | ")[-1], "marks": {}}
            found.append(current)
            start = None
            continue
        if current is None:
            continue
        stamp = LINE_TIME.match(line)
        mark = MARK.search(line)
        if mark is not None:
            name, offset = mark.group(1), int(mark.group(2))
            current["marks"].setdefault(name, offset)
            if name == "main" and stamp is not None:
                when = datetime.datetime.fromisoformat(stamp.group(1))
                start = when - datetime.timedelta(milliseconds=offset)
        elif WARMED_UP in line and "search_ready" not in current and start is not None:
            api_stamp = API_LINE_TIME.match(line)
            if api_stamp is not None:
                when = datetime.datetime.fromisoformat(api_stamp.group(1))
                current["search_ready"] = round((when - start).total_seconds() * 1000)
    return found


def size_of(root: Path) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                pass
    return total


def command_after() -> int:
    lines: list[str] = []
    data: dict[str, Any] = {"folders": {}, "openings": []}
    say(lines, "the first level of the four folders, against the lists of step 8")
    for name in FOLDERS:
        folder = Path(os.environ.get(name, ""))
        now = sorted(entry.name for entry in folder.iterdir())
        before_file = OUT / f"before-{name}.txt"
        if not before_file.is_file():
            say(lines, f"  {name}: no {before_file.name}; step 8 was not done")
            continue
        before = sorted(
            line.strip().lstrip("\ufeff")
            for line in before_file.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        )
        new = sorted(set(now) - set(before))
        gone = sorted(set(before) - set(now))
        data["folders"][name] = {"new": new, "gone": gone}
        say(lines, f"  {name} ({folder}): new {new or 'nothing'}; gone {gone or 'nothing'}")
    say(lines)
    if SHELL_LOG.is_file():
        found = openings(SHELL_LOG)
        data["openings"] = found
        say(lines, f"the openings in {SHELL_LOG}: {len(found)}")
        for number, opening in enumerate(found, start=1):
            marks = opening["marks"]
            parts = [
                f"{name} {marks[name]} ms"
                for name in ("window", "ready", "page", "home")
                if name in marks
            ]
            if "search_ready" in opening:
                parts.append(f"search: ready {opening['search_ready']} ms")
            say(lines, f"  {number}. {', '.join(parts) or 'no marks'}")
        homes = [o["marks"]["home"] for o in found if "home" in o["marks"]]
        if len(homes) >= 2:
            warm = sorted(homes[1:])
            median = warm[len(warm) // 2]
            say(lines, f"  the first home: {homes[0]} ms; the median of the rest: {median} ms")
    else:
        say(lines, f"no {SHELL_LOG}")
    say(lines)
    for label, root in (("the installation", INSTALL), ("the data", DATA)):
        size = size_of(root) if root.exists() else 0
        data[label] = size
        say(lines, f"{label}: {root}, {size / 2**30:.2f} GB")
    save("after", lines, data)
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    command = sys.argv[1] if len(sys.argv) > 1 else "steps"
    if command == "steps":
        print(STEPS)
        return 0
    if command == "modules":
        return command_modules()
    if command == "after":
        return command_after()
    print(f"unknown command {command}: steps, modules or after")
    return 2


if __name__ == "__main__":
    sys.exit(main())
